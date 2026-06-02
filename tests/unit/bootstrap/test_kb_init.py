"""Unit tests for faultmaven.bootstrap.kb_init.

Covers the atomicity + idempotency guarantees the bootstrap was created
to enforce. Uses fakes for KnowledgeService and the DB session factory
so the tests run without ChromaDB or a real database — the bootstrap's
contract is fully describable in terms of those collaborators.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.bootstrap import kb_init

RUNBOOK_FRONTMATTER = """---
id: example-runbook
title: "Example Runbook"
domain: database
service: postgresql
symptom_class:
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-26"
verified_by: "test"
status: draft
tags: [postgres, latency]
---

# Body content for tests.
"""


@pytest.fixture
def project_root_with_one_runbook(tmp_path: Path) -> Path:
    """Create a fake project root containing a single runbook under
    data/knowledge/global/."""
    kb_dir = tmp_path / "data" / "knowledge" / "global"
    kb_dir.mkdir(parents=True)
    (kb_dir / "example.md").write_text(RUNBOOK_FRONTMATTER, encoding="utf-8")
    return tmp_path


class FakeSession:
    """Async-context-manager fake that returns no existing rows."""

    def __init__(self, existing_row: Optional[object] = None) -> None:
        self._existing = existing_row

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def execute(self, _stmt):
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=self._existing)
        return scalar_mock

    async def delete(self, _row):
        return None

    async def commit(self):
        return None


def _make_session_factory(existing_row=None):
    def factory():
        return FakeSession(existing_row)

    return factory


@pytest.fixture
def patch_model_cache():
    """Pretend BGE-M3 loaded successfully."""
    with patch.object(
        kb_init, "_enumerate_runbook_files", wraps=kb_init._enumerate_runbook_files
    ):
        with patch(
            "faultmaven.infrastructure.model_cache.model_cache"
        ) as model_cache_mock:
            model_cache_mock.get_bge_m3_model.return_value = MagicMock(name="bge_m3")
            yield model_cache_mock


@pytest.mark.asyncio
async def test_bootstrap_ingests_new_runbook(
    project_root_with_one_runbook, patch_model_cache
):
    """A fresh runbook (no DB row) is ingested and counted."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert len(result.ingested) == 1
    assert result.skipped_unchanged == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_awaited_once()
    # Verify deterministic item_id was passed (kb_<sha256(id)[:12]>).
    call_kwargs = knowledge_service.ingest_runbook.await_args.kwargs
    assert call_kwargs["document_id"] == kb_init._item_id_from_runbook_id(
        "example-runbook"
    )
    assert call_kwargs["title"] == "Example Runbook"
    # Platform-shipped runbooks carry COMMUNITY trust via verification_level,
    # NOT a fake verified_by — verified_by is an FK to users.user_id and must
    # be a real user or NULL (regression: verified_by="system" failed the FK
    # once foreign_keys=ON landed in #378).
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    assert call_kwargs["verified_by"] is None
    assert call_kwargs["verification_level"] == VerificationLevel.COMMUNITY


@pytest.mark.asyncio
async def test_bootstrap_skips_unchanged_runbook(
    project_root_with_one_runbook, patch_model_cache
):
    """Re-running with the same file content skips ingestion."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    # Existing row whose `content` matches the file on disk verbatim.
    existing = MagicMock()
    existing.content = (
        project_root_with_one_runbook / "data" / "knowledge" / "global" / "example.md"
    ).read_text(encoding="utf-8")

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert result.ingested == []
    assert len(result.skipped_unchanged) == 1
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_re_ingests_changed_runbook(
    project_root_with_one_runbook, patch_model_cache
):
    """Content drift on disk triggers re-ingestion (delete + re-create)."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    # Existing DB row whose content differs from disk.
    existing = MagicMock()
    existing.content = "STALE CONTENT — does not match disk"

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert len(result.ingested) == 1
    assert result.skipped_unchanged == []
    knowledge_service.ingest_runbook.assert_awaited_once()
    # Stale chunks should have been cleaned out of ChromaDB.
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_awaited()


@pytest.mark.asyncio
async def test_bootstrap_raises_on_zero_chunks(
    project_root_with_one_runbook, patch_model_cache
):
    """If ingest_runbook reports 0 chunks (silent failure), the file is
    recorded as failed AND the orphan-cleanup path is invoked."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=0)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=project_root_with_one_runbook,
    )

    assert result.ingested == []
    assert len(result.failed) == 1
    failed_file, reason = result.failed[0]
    assert "example.md" in failed_file
    assert "0 chunks" in reason or "RuntimeError" in reason


@pytest.mark.asyncio
async def test_bootstrap_no_knowledge_dir_is_safe(tmp_path: Path, patch_model_cache):
    """If data/knowledge/ does not exist, bootstrap returns an empty result
    without attempting ingestion or raising."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=tmp_path,
    )

    assert result.ingested == []
    assert result.skipped_unchanged == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_aborts_when_bge_m3_unavailable(
    project_root_with_one_runbook,
):
    """If BGE-M3 fails to load, the bootstrap aborts without attempting
    any ingestion (the silent-0-chunks failure mode is unavoidable in this
    state, so the right behaviour is fail-fast)."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=4)

    with patch("faultmaven.infrastructure.model_cache.model_cache") as model_cache_mock:
        model_cache_mock.get_bge_m3_model.return_value = None

        result = await kb_init.bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=_make_session_factory(existing_row=None),
            organization_id="org-test",
            project_root=project_root_with_one_runbook,
        )

    assert result.ingested == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


def test_item_id_from_runbook_id_is_deterministic():
    assert kb_init._item_id_from_runbook_id("foo") == kb_init._item_id_from_runbook_id(
        "foo"
    )
    assert kb_init._item_id_from_runbook_id("foo") != kb_init._item_id_from_runbook_id(
        "bar"
    )
    assert kb_init._item_id_from_runbook_id("foo").startswith("kb_")


# ============================================================
# FK-ON regression: the composition seam #378 + kb_init broke
# ============================================================
#
# The unit tests above use FakeSession, so the FK on
# knowledge_items.verified_by → users.user_id is never exercised. That is
# exactly the dynamic-drift seam: #378 (foreign_keys=ON) and kb_init
# (verified_by="system") were each locally fine; only their composition
# failed — every shipped-runbook ingest hit "FOREIGN KEY constraint
# failed" because no users row has user_id="system".
#
# This test drives the REAL ingest_runbook SQL path against an in-memory
# SQLite with foreign_keys=ON and a seeded enterprise+org but NO "system"
# user — the fresh-install shape — and asserts a clean ingest. A negative
# control proves the FK is actually enforced in the test (so a green run
# isn't a false pass).


@pytest.fixture
async def fk_on_ingest_service():
    """Real KnowledgeService.ingest_runbook over FK-on SQLite.

    ChromaDB is the only thing stubbed (``_index_document_in_vector_store``
    returns a positive chunk count); the relational write — where the FK
    fires — is real.
    """
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from faultmaven.infrastructure.persistence.models import Base
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # replicates the #378 connect listener
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Seed enterprise + org so knowledge_items.organization_id FK is
        # satisfiable. Deliberately seed NO "system" user.
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES ('ent-1', 'Default', 'default')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                "VALUES ('org-1', 'ent-1', 'Org', 'org')"
            )
        )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = factory
    svc._index_document_in_vector_store = AsyncMock(return_value=3)
    svc._delete_knowledge_item_row = AsyncMock()

    yield svc, factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_runbook_succeeds_with_fk_enforced_fresh_install(
    fk_on_ingest_service,
):
    """The fix: verified_by=None + verification_level=COMMUNITY ingests
    cleanly under foreign_keys=ON with no 'system' user present."""
    from sqlalchemy import text

    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    svc, factory = fk_on_ingest_service

    chunks = await svc.ingest_runbook(
        document_id="kb_fkprobe",
        title="Probe Runbook",
        content="# body",
        organization_id="org-1",
        scope="global",
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
    )
    assert chunks == 3

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT verified_by, verification_level "
                    "FROM knowledge_items WHERE item_id='kb_fkprobe'"
                )
            )
        ).first()
    assert row is not None  # the row actually persisted (no FK rollback)
    assert row[0] is None  # verified_by NULL — never a sentinel
    assert row[1] == int(VerificationLevel.COMMUNITY)  # COMMUNITY trust kept


@pytest.mark.asyncio
async def test_ingest_runbook_with_sentinel_verified_by_fails_fk(
    fk_on_ingest_service,
):
    """Negative control: the old behaviour (verified_by='system') still
    violates the FK — proving foreign_keys=ON is genuinely active in this
    test, so the positive assertion above isn't a false pass."""
    svc, _ = fk_on_ingest_service

    with pytest.raises(Exception) as exc_info:
        await svc.ingest_runbook(
            document_id="kb_sentinel",
            title="Sentinel Runbook",
            content="# body",
            organization_id="org-1",
            scope="global",
            verified_by="system",  # no such users row → FK violation
        )
    assert "foreign key" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_ingest_runbook_coerces_numeric_tags_for_both_models(
    fk_on_ingest_service,
):
    """A numeric YAML tag (e.g. 503) must not break ingest at EITHER the
    relational KnowledgeItem OR the Pydantic KnowledgeBaseDocument.

    Regression: KnowledgeItem.__post_init__ coercion (#394) was not enough —
    ingest_runbook also builds a KnowledgeBaseDocument (List[str] tags, no
    coercion hook) which rejected the int and failed the whole ingest
    (unknown-case-260526-4.md, failed=1 after #394).
    """
    from sqlalchemy import text

    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    svc, factory = fk_on_ingest_service

    # Must not raise (pre-fix: ValidationError on KnowledgeBaseDocument.tags).
    chunks = await svc.ingest_runbook(
        document_id="kb_numtag",
        title="Numeric Tag Runbook",
        content="# body",
        organization_id="org-1",
        scope="global",
        tags=["istio", 503, "envoy"],
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
    )
    assert chunks == 3

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT tags FROM knowledge_items WHERE item_id='kb_numtag'")
            )
        ).first()
    assert row is not None  # persisted (no rollback)
    assert "503" in row[0]  # numeric tag coerced to "503"


class TestPruneOrphanBuiltins:
    """Bootstrap prunes built-in rows whose runbook id no longer maps to a
    file on disk (id-churn orphans), without touching authored/uuid items.

    Regression: id churn between runs left stale kb_<hash> rows (59 orphans
    vs 60 files in eval 2026-06-01); the dashboard double-listed once the
    Runbooks tab read knowledge_items.
    """

    @pytest.mark.asyncio
    async def test_prunes_builtin_orphan_not_on_disk(self):
        deleted: list[str] = []

        async def fake_delete(item_id, _svc, _factory):
            deleted.append(item_id)

        keep = {"kb_aaaaaaaaaaaa"}  # the one current on-disk runbook
        all_ids = [
            "kb_aaaaaaaaaaaa",  # on disk → keep
            "kb_bbbbbbbbbbbb",  # built-in, NOT on disk → prune
            "550e8400-e29b-41d4-a716-446655440000",  # authored uuid → never touch
        ]

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def execute(self, _stmt):
                return MagicMock(all=lambda: [(i,) for i in all_ids])

        with patch.object(kb_init, "_delete_existing", side_effect=fake_delete):
            pruned = await kb_init._prune_orphan_builtins(
                keep,
                knowledge_service=MagicMock(),
                db_session_factory=lambda: _Session(),
            )

        assert pruned == ["kb_bbbbbbbbbbbb"]
        assert deleted == ["kb_bbbbbbbbbbbb"]  # uuid + kept id untouched

    @pytest.mark.asyncio
    async def test_empty_keepset_prunes_nothing(self):
        """Safety guard: empty keep-set (disk/parse anomaly) removes nothing."""
        with patch.object(kb_init, "_delete_existing") as del_mock:
            pruned = await kb_init._prune_orphan_builtins(
                set(), knowledge_service=MagicMock(), db_session_factory=MagicMock()
            )
        assert pruned == []
        del_mock.assert_not_called()
