"""``DELETE /cases/{id}`` deletes a case completely, or fails and keeps it (#1016).

Every store that holds the case's data is cleared before the row, by one
``ICaseStoreEraser`` per store; the first that raises keeps the row, and the
route answers 500 ``DELETE_CASE_ERROR``. Each eraser is exercised here on the
real store it clears (a ChromaDB ``PersistentClient``, a filesystem backend,
fakeredis, SQLite); the SQL repositories' half is
``tests/integration/test_case_deletion_repository_1016.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock

import chromadb
import fakeredis.aioredis as fakeredis_aio
import pytest
from chromadb.errors import NotFoundError as ChromaNotFoundError
from fastapi import HTTPException, Response
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.container.providers.services import create_case_service
from faultmaven.exceptions import NotFoundError, ServiceError, ServiceException
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.security.case_redaction import (
    CaseRedactionRegistryEraser,
)
from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.case.api.routes.cases import delete_case, get_case
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.services.case_erasure import SessionPointerEraser
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.case_erasure import (
    RunbookConversionEraser,
)

pytestmark = [pytest.mark.unit]

ENTERPRISE = "ent_delete_01"
CREATOR = "u_creator"
READER = "u_reader"


@pytest.fixture(autouse=True)
def _tenant():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


class _Eraser:
    """Records its run in ``log``; raises when told to."""

    def __init__(self, name: str, log: List[str], removed: int = 1, fail=False):
        self.store_name, self.log, self.removed, self.fail = name, log, removed, fail

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        self.log.append(f"erase:{self.store_name}:{case_id}:{enterprise_id}")
        if self.fail:
            raise ConnectionError(f"{self.store_name} unreachable")
        return self.removed


class _RecordingRepository(InMemoryCaseRepository):
    def __init__(self, log: List[str]):
        super().__init__()
        self.log = log

    async def delete(self, case_id, *, deletion):
        self.log.append(f"delete:{case_id}")
        return await super().delete(case_id, deletion=deletion)


async def _world(*erasers_spec, shared_with_reader=False):
    log: List[str] = []
    repository = _RecordingRepository(log)
    erasers = [_Eraser(name, log, **kw) for name, kw in erasers_spec]
    teams = SimpleNamespace(list_all_user_team_ids=AsyncMock(return_value=["t1"]))
    case = await repository.save(
        Case(user_id=CREATOR, enterprise_id=ENTERPRISE, title="Disk full")
    )
    shares = SimpleNamespace(
        list_resource_ids=AsyncMock(
            return_value=[case.case_id] if shared_with_reader else []
        ),
        list_scopes_for_resources=AsyncMock(return_value={}),
    )
    service = CaseService(
        repository,
        team_service=teams,
        share_repository=shares,
        store_erasers=erasers,
        stores_not_configured=("case_vectors",),
    )
    return SimpleNamespace(service=service, repository=repository, case=case, log=log)


# ============================================================
# The service (test 1)
# ============================================================


class TestHardDeleteCase:
    async def test_every_store_is_cleared_before_the_row_and_audited(self):
        w = await _world(("uploads", {"removed": 3}), ("pii_redaction_registry", {}))

        assert await w.service.hard_delete_case(w.case.case_id, CREATOR) is True

        cid = w.case.case_id
        assert w.log == [
            f"erase:uploads:{cid}:{ENTERPRISE}",
            f"erase:pii_redaction_registry:{cid}:{ENTERPRISE}",
            f"delete:{cid}",
        ]
        (deletion,) = w.repository.deletions
        assert (deletion.case_id, deletion.actor_user_id) == (cid, CREATOR)
        assert deletion.cleared == {"uploads": 3, "pii_redaction_registry": 1}
        assert deletion.not_configured == ("case_vectors",)
        assert set(json_of(deletion)["not_cleared"]) == {
            "idempotency_replay_cache",
            "llm_response_cache",
        }

    async def test_a_store_that_cannot_be_cleared_keeps_the_case(self):
        w = await _world(("uploads", {}), ("case_vectors", {"fail": True}))

        with pytest.raises(ConnectionError):
            await w.service.hard_delete_case(w.case.case_id, CREATOR)

        assert not any(entry.startswith("delete:") for entry in w.log)
        assert await w.service.get_case(w.case.case_id, CREATOR) is not None
        assert w.repository.deletions == []

    async def test_a_database_error_is_not_answered_as_absence(self):
        w = await _world(("uploads", {}))
        w.repository.get = AsyncMock(side_effect=OSError("database unreachable"))

        with pytest.raises(OSError):
            await w.service.hard_delete_case(w.case.case_id, CREATOR)
        assert w.log == []

    async def test_an_unseen_case_is_not_found_and_a_reader_is_refused(self):
        w = await _world(("uploads", {}))
        with pytest.raises(NotFoundError):
            await w.service.hard_delete_case(w.case.case_id, READER)

        shared = await _world(("uploads", {}), shared_with_reader=True)
        assert (
            await shared.service.hard_delete_case(shared.case.case_id, READER) is False
        )
        assert shared.log == []


def json_of(deletion):
    return json.loads(deletion.audit_details())


# ============================================================
# The route (test 2)
# ============================================================


class TestDeleteRoute:
    async def test_a_failed_delete_answers_500_and_the_case_still_reads(self):
        w = await _world(("uploads", {"fail": True}))
        user = SimpleNamespace(user_id=CREATOR, enterprise_id=ENTERPRISE)

        with pytest.raises(HTTPException) as refused:
            await delete_case(
                case_id=w.case.case_id, case_service=w.service, current_user=user
            )
        assert refused.value.status_code == 500
        assert refused.value.detail["error"]["code"] == "DELETE_CASE_ERROR"
        detail = await get_case(
            w.case.case_id, Response(), case_service=w.service, current_user=user
        )
        assert detail.case_id == w.case.case_id

        w.service.store_erasers[0].fail = False
        answer = await delete_case(
            case_id=w.case.case_id, case_service=w.service, current_user=user
        )
        assert answer.status_code == 204


# ============================================================
# No save brings a deleted case back (test 4, in-memory arm)
# ============================================================


async def test_a_save_after_the_delete_is_refused_in_memory():
    w = await _world()
    loaded = await w.repository.get(w.case.case_id)
    await w.service.hard_delete_case(w.case.case_id, CREATOR)

    with pytest.raises(StaleCaseException):
        await w.repository.save(loaded)
    assert await w.repository.get(w.case.case_id) is None
    assert await w.repository.save(
        Case(user_id=CREATOR, enterprise_id=ENTERPRISE, title="New")
    )


async def test_an_update_that_loses_the_race_with_a_delete_is_not_found():
    """``update_case``'s retry reloads and finds no row (test 11): the answer
    an absent case gets (``False``, the route's 404), not a 500."""
    w = await _world()
    real_get = w.repository.get
    calls = 0

    async def get_then_vanish(case_id):
        nonlocal calls
        calls += 1
        return await real_get(case_id) if calls == 1 else None

    w.repository.get = get_then_vanish
    assert (
        await w.service.update_case(
            w.case.case_id, {"state": CaseState.INVESTIGATING.value}, CREATOR
        )
        is False
    )


# ============================================================
# The erasers, each on the store it clears (tests 5-9)
# ============================================================


class TestCaseVectors:
    async def test_absent_present_and_unreachable(self, tmp_path: Path):
        client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
        store = CaseVectorStore(client)
        assert await store.erase_case("case_none", ENTERPRISE) == 0

        client.get_or_create_collection(name="case_abc")
        assert await store.erase_case("abc", ENTERPRISE) == 1
        with pytest.raises(ChromaNotFoundError):
            client.get_collection(name="case_abc")

        class _Unreachable:
            def delete_collection(self, name):
                raise ConnectionError("chroma down")

        with pytest.raises(ConnectionError):
            await CaseVectorStore(_Unreachable()).erase_case("abc", ENTERPRISE)


class TestUploads:
    async def test_only_the_cases_own_prefix_is_erased(self, tmp_path: Path):
        backend = FilesystemStorageBackend(storage_root=str(tmp_path))
        storage = FileStorageService(backend=backend)

        async def store(enterprise, case_id, name):
            await storage.store_file(b"log", name, enterprise, case_id, "text/plain")

        await store("ent_a", "case_abc", "one.log")
        await store("ent_a", "case_abc", "two.log")
        await store("ent_a", "case_abcd", "sibling.log")
        await store("ent_b", "case_abc", "elsewhere.log")

        assert await storage.erase_case("case_abc", "ent_a") >= 4
        assert await backend.list_keys(prefix="ent_a/case_abc/") == []
        assert len(await backend.list_keys(prefix="ent_a/case_abcd/")) >= 2
        assert len(await backend.list_keys(prefix="ent_b/case_abc/")) >= 2
        assert await storage.erase_case("case_abc", "ent_a") == 0

    @pytest.mark.skipif(os.geteuid() == 0, reason="mode 000 does not stop root")
    async def test_an_unreadable_case_directory_raises(self, tmp_path: Path):
        """``rglob`` skipped it silently, so the delete reported 0 erased."""
        storage = FileStorageService(backend=FilesystemStorageBackend(str(tmp_path)))
        await storage.store_file(b"log", "one.log", "ent_a", "case_abc", "text/plain")
        day = next((tmp_path / "ent_a" / "case_abc").iterdir())
        day.chmod(0)
        try:
            with pytest.raises(ServiceError):
                await storage.erase_case("case_abc", "ent_a")
        finally:
            day.chmod(0o755)

    async def test_a_backend_error_raises(self):
        backend = SimpleNamespace(
            get_storage_type=lambda: SimpleNamespace(value="s3"),
            list_keys=AsyncMock(return_value=["ent_a/case_abc/x"]),
            delete_file=AsyncMock(side_effect=PermissionError("AccessDenied")),
        )
        with pytest.raises(ServiceError):
            await FileStorageService(backend=backend).erase_case("case_abc", "ent_a")


class TestRedisErasers:
    async def test_the_registry_of_this_case_only(self):
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        await redis.hset("redaction:registry:case_a", "k", "<EMAIL_ab>")
        await redis.hset("redaction:registry:case_b", "k", "<EMAIL_cd>")

        assert await CaseRedactionRegistryEraser(redis).erase_case("case_a", "e") == 1
        assert not await redis.exists("redaction:registry:case_a")
        assert await redis.exists("redaction:registry:case_b")

        broken = SimpleNamespace(delete=AsyncMock(side_effect=ConnectionError("down")))
        with pytest.raises(ConnectionError):
            await CaseRedactionRegistryEraser(broken).erase_case("case_a", "e")

    async def test_the_session_pointers_naming_this_case_only(self):
        store = RedisSessionStore(fakeredis_aio.FakeRedis(decode_responses=True))
        for session_id, case_id in (
            ("s1", "case_a"),
            ("s2", "case_a"),
            ("s3", "case_b"),
        ):
            await store.set(f"session:{session_id}:current_case_id", case_id)

        assert await SessionPointerEraser(store).erase_case("case_a", "e") == 2
        assert await store.get("session:s1:current_case_id") is None
        assert await store.get("session:s3:current_case_id") == "case_b"


#: Case A's job (two drafts, one published as ``ki``; a share; its source
#: row), case B's job, and a case-less document job.
_CONVERSION_SEED = """
INSERT INTO enterprises (enterprise_id, name, slug) VALUES ('e', 'e', 'e');
INSERT INTO users (user_id, enterprise_id, username, email, display_name)
  VALUES ('u', 'e', 'u', 'u@example.com', 'u');
INSERT INTO cases (case_id, enterprise_id, user_id, title) VALUES
  ('case_a', 'e', 'u', 'a'), ('case_b', 'e', 'u', 'b');
INSERT INTO knowledge_items (item_id, enterprise_id, scope, owner_id, title,
  content, item_type) VALUES ('ki', 'e', 'personal', 'u', 't', 'c', 'runbook');
INSERT INTO uploaded_files (file_id, enterprise_id, filename, size_bytes,
  upload_source) VALUES ('src_a', 'e', 'Case', 1, 'conversion_source'),
  ('src_b', 'e', 'Case', 1, 'conversion_source'),
  ('src_doc', 'e', 'doc.md', 1, 'conversion_source');
INSERT INTO conversion_jobs (id, enterprise_id, case_id, source_file_id, scope)
  VALUES ('job_a', 'e', 'case_a', 'src_a', 'personal'),
  ('job_b', 'e', 'case_b', 'src_b', 'personal'),
  ('job_doc', 'e', NULL, 'src_doc', 'personal');
INSERT INTO conversion_drafts (id, enterprise_id, conversion_id, runbook_id,
  title, content, status, knowledge_item_id) VALUES
  ('d1', 'e', 'job_a', 'r1', 't', 'c', 'verified', 'ki'),
  ('d2', 'e', 'job_a', 'r2', 't', 'c', 'draft', NULL),
  ('d3', 'e', 'job_b', 'r3', 't', 'c', 'draft', NULL);
INSERT INTO resource_shares (share_id, resource_type, resource_id, scope_type,
  scope_id, enterprise_id) VALUES ('sh', 'conversion_job', 'job_a', 'team', 't', 'e')
"""


class TestRunbookConversion:
    async def test_the_cases_jobs_go_and_the_published_runbook_stays(self, tmp_path):
        path = tmp_path / "conv.db"
        sync = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(sync)
        sync.dispose()
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

        @event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        async with engine.begin() as conn:
            for statement in _CONVERSION_SEED.strip().split(";\n"):
                await conn.execute(text(statement))
        Session = async_sessionmaker(engine, expire_on_commit=False)

        eraser = RunbookConversionEraser(Session)
        # A's job, its two drafts, its share row and its source row.
        assert await eraser.erase_case("case_a", "e") == 5
        assert await eraser.erase_case("case_a", "e") == 0

        async with engine.connect() as conn:

            async def ids(table, column="id"):
                rows = await conn.execute(text(f"SELECT {column} FROM {table}"))
                return {row[0] for row in rows}

            assert await ids("conversion_jobs") == {"job_b", "job_doc"}
            assert await ids("conversion_drafts") == {"d3"}
            assert await ids("uploaded_files", "file_id") == {"src_b", "src_doc"}
            assert await ids("resource_shares", "share_id") == set()
            assert await ids("knowledge_items", "item_id") == {"ki"}
        await engine.dispose()


# ============================================================
# Composition (test 10)
# ============================================================


class TestComposition:
    def test_an_eraser_that_cannot_be_built_fails_startup(self, tmp_path, monkeypatch):
        """Not the ``MinimalCaseService`` fallback, whose delete clears nothing."""
        from faultmaven.container.providers import services

        def refuse(**_):
            raise RuntimeError("cannot build the erasers")

        monkeypatch.setattr(services, "case_store_erasers", refuse)
        with pytest.raises(RuntimeError, match="cannot build the erasers"):
            self._build(tmp_path, skip=True, case_vector_store=None)

    def _build(self, tmp_path, *, skip, case_vector_store):
        from faultmaven.config.settings import get_settings

        settings = get_settings().model_copy(deep=True)
        settings.server.skip_service_checks = skip
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        storage = FileStorageService(backend=FilesystemStorageBackend(str(tmp_path)))
        return create_case_service(
            InMemoryCaseRepository(),
            RedisSessionStore(redis),
            case_vector_store,
            settings,
            lambda: pytest.fail("fell back to MinimalCaseService"),
            file_storage_service=storage,
            redis_client=redis,
        )

    def test_the_test_profile_names_case_vectors_not_configured(self, tmp_path):
        service = self._build(tmp_path, skip=True, case_vector_store=None)
        assert isinstance(service, CaseService)
        assert [e.store_name for e in service.store_erasers] == [
            "session_pointers",
            "runbook_conversion",
            "uploads",
            "pii_redaction_registry",
        ]
        assert service.stores_not_configured == ("case_vectors",)

    def test_production_binds_the_real_vector_eraser(self, tmp_path):
        vectors = CaseVectorStore(chromadb.PersistentClient(path=str(tmp_path / "c")))
        service = self._build(tmp_path, skip=False, case_vector_store=vectors)
        assert isinstance(service, CaseService)
        assert service.store_erasers[3] is vectors
        assert service.stores_not_configured == ()

    async def test_an_unconstructed_store_fails_the_delete(self, tmp_path):
        absent = self._build(tmp_path, skip=False, case_vector_store=None)
        assert absent.stores_not_configured == ()
        with pytest.raises(ServiceException, match="Cannot clear case_vectors"):
            await absent.store_erasers[3].erase_case("case_a", ENTERPRISE)
