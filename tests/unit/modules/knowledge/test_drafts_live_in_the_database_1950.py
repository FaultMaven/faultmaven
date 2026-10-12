"""A runbook draft's text has one home: ``conversion_drafts.content`` (#1950).

The live API runs as two replicas with no shared disk. A draft whose text lived
in a file under the writing pod's ``data/knowledge`` was missing on the other
replica (``get_conversion`` served no content, ``verify`` 500ed), gone after a
rollout, and discarded by a scan on a pod that could not see it. These tests
drive the real service over one shared database from two "replicas" — two
service instances, each in a working directory of its own — and assert that
nothing about a draft depends on which one serves the request, and that no
draft is ever written to disk.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    EnterpriseModel,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    service as service_module,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    SCAN_UNAVAILABLE_MULTI_MSG,
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

TEMPLATE = dict(
    title="Connection Pool Exhausted On The API",
    domain="database",
    service_name="postgresql",
    symptom_class=["resource_exhaustion"],
    severity="high",
    scope="personal",
    tags=["postgres"],
    difficulty="intermediate",
    symptom_recognition="Requests fail with pool timeout errors under load.",
    applicability="Any service using a bounded PostgreSQL connection pool.",
    diagnostic_steps=(
        "### Step 1: Check pool usage\n```bash\npsql -c 'select count(*) from "
        "pg_stat_activity'\n```\nLook for a count near the pool ceiling."
    ),
    causes=(
        "### Cause A: Pool leak\n"
        "**Statement:** connections are never returned to the pool.\n"
        "**Indicators:**\n- root: [Step 1] active connections pinned at the cap\n"
        "**Interventions:**\n- **remediation** (root): fix the leak.\n"
        "  **Verification:** Re-run Step 1.\n\n"
        "### Cause Z: Unidentified\n"
        "**Statement:** none of the causes match.\n"
        "**Indicators:**\n- [Default]\n"
        "**Interventions:**\n- **mitigation** (D): consult an SME.\n"
    ),
    prevention="Alert on pool saturation above eighty percent.",
    user_id="u1",
    enterprise_id=STANDALONE_ENTERPRISE_ID,
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.commit()
    yield factory
    await engine.dispose()


class _Replica:
    """One API pod: its own working directory (its own ``data/knowledge``),
    the shared database."""

    def __init__(self, root: Path, session_factory, knowledge_service=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        settings = MagicMock()
        settings.llm.get_knowledge_model.return_value = "test-model"
        self.service = ConversionService(
            llm_router=MagicMock(),
            settings=settings,
            db_session_factory=session_factory,
            knowledge_service=knowledge_service,
        )

    def files(self) -> list[Path]:
        return sorted(self.root.rglob("*.md"))


async def _row(session_factory, draft_id: str) -> ConversionDraftModel:
    async with session_factory() as session:
        return await session.get(ConversionDraftModel, draft_id)


def _ingesting() -> MagicMock:
    knowledge = MagicMock()
    knowledge.ingest_runbook = AsyncMock(return_value=4)
    return knowledge


class TestTheOtherReplicaServesTheDraft:
    async def test_read_edit_and_verify_on_a_pod_that_never_saw_a_file(
        self, session_factory, tmp_path, monkeypatch
    ):
        pod_a = _Replica(tmp_path / "pod-a", session_factory)
        pod_b = _Replica(tmp_path / "pod-b", session_factory, _ingesting())

        monkeypatch.chdir(pod_a.root)
        created = await pod_a.service.create_runbook_from_template(**TEMPLATE)
        conversion_id = created["conversion_id"]
        draft_id = created["draft"].draft_id

        # Pod B: a different working directory, nothing on its disk.
        monkeypatch.chdir(pod_b.root)
        read = await pod_b.service.get_conversion(conversion_id, "u1")
        (draft,) = read.drafts
        assert draft.content == created["draft"].content
        assert draft.content_preview == created["draft"].content[:500]

        edited = valid_runbook("Connection Pool Exhausted Edited On Pod B")
        result = await pod_b.service.update_draft(conversion_id, draft_id, "u1", edited)
        assert result.validation.passed, result.validation.errors
        assert (await _row(session_factory, draft_id)).content == edited

        verified = await pod_b.service.verify_draft(conversion_id, draft_id, "u1")
        assert verified.status == "verified"
        row = await _row(session_factory, draft_id)
        assert row.status == "verified"
        assert frontmatter.loads(row.content).metadata["status"] == "verified"
        assert "Connection Pool Exhausted Edited On Pod B" in row.content
        published = pod_b.service._knowledge_service.ingest_runbook.await_args
        assert published.kwargs["content"] == row.content

        # And no pod wrote a draft file anywhere.
        assert pod_a.files() == [] and pod_b.files() == []


class TestVerifyCommitsTheTextWithTheStatus:
    async def test_a_failed_ingestion_leaves_the_row_exactly_as_it_was(
        self, session_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        knowledge = MagicMock()
        knowledge.ingest_runbook = AsyncMock(side_effect=RuntimeError("store down"))
        pod = _Replica(tmp_path, session_factory, knowledge)
        created = await pod.service.create_runbook_from_template(**TEMPLATE)
        draft_id = created["draft"].draft_id
        async with session_factory() as session:
            (await session.get(ConversionDraftModel, draft_id)).validation_passed = True
            await session.commit()

        with pytest.raises(RuntimeError, match="store down"):
            await pod.service.verify_draft(created["conversion_id"], draft_id, "u1")

        row = await _row(session_factory, draft_id)
        assert row.status == "draft"
        assert row.content == created["draft"].content
        assert frontmatter.loads(row.content).metadata["status"] == "draft"


class TestUpdateDraftWritesTheTextWithItsVerdict:
    async def test_a_draft_discarded_while_scoring_keeps_its_text(
        self, session_factory, tmp_path, monkeypatch
    ):
        """The text and the verdict are one commit; the discarded-while-scoring
        branch writes neither."""
        monkeypatch.chdir(tmp_path)
        pod = _Replica(tmp_path, session_factory)
        created = await pod.service.create_runbook_from_template(**TEMPLATE)
        draft_id = created["draft"].draft_id
        real_gate = service_module.avalidate_and_score

        async def _gate_then_discard(content):
            verdict = await real_gate(content)
            async with session_factory() as session:
                (await session.get(ConversionDraftModel, draft_id)).status = "discarded"
                await session.commit()
            return verdict

        monkeypatch.setattr(service_module, "avalidate_and_score", _gate_then_discard)
        result = await pod.service.update_draft(
            created["conversion_id"], draft_id, "u1", valid_runbook("Another Body")
        )

        assert result is None
        assert (await _row(session_factory, draft_id)).content == created[
            "draft"
        ].content


class TestTheScanIsStandaloneOnly:
    async def test_the_service_refuses_under_multi(
        self, session_factory, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            service_module, "requested_tenant_provider", lambda: BUILTIN_MULTI
        )
        pod = _Replica(tmp_path, session_factory)
        (tmp_path / "global").mkdir()
        (tmp_path / "global" / "x.md").write_text(valid_runbook("Placed By Hand"))
        monkeypatch.setattr(
            ConversionService, "_data_dir", property(lambda self: tmp_path)
        )

        with pytest.raises(ValidationException) as refused:
            await pod.service.scan_for_runbooks(user_id="u1", is_platform_admin=True)

        assert str(refused.value) == SCAN_UNAVAILABLE_MULTI_MSG
        async with session_factory() as session:
            assert (await session.get(ConversionDraftModel, "x")) is None

    async def test_the_route_answers_422_with_the_sentence(
        self, session_factory, tmp_path, monkeypatch
    ):
        from faultmaven.api.exception_handlers import get_exception_handlers
        from faultmaven.modules.knowledge.api import conversion_routes as cr

        monkeypatch.setattr(
            service_module, "requested_tenant_provider", lambda: BUILTIN_MULTI
        )
        pod = _Replica(tmp_path, session_factory)
        app = FastAPI()
        app.include_router(cr.router, prefix="/api/v1")
        for exc_type, handler in get_exception_handlers().items():
            app.add_exception_handler(exc_type, handler)
        user = MagicMock(user_id="u1", enterprise_id=None)
        user.is_platform_admin.return_value = True
        app.dependency_overrides[cr._require_auth] = lambda: user
        app.dependency_overrides[cr._get_conversion_service] = lambda: pod.service

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/v1/knowledge/scan")

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == SCAN_UNAVAILABLE_MULTI_MSG


class TestUploadWritesNoFile:
    async def test_even_a_traversal_title_writes_nothing_to_disk(
        self, session_factory, tmp_path, monkeypatch
    ):
        """The upload's text goes to the draft row and ``knowledge_items``; no
        title can reach a filesystem path, because there is none."""
        workdir = tmp_path / "app"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        knowledge = KnowledgeService(
            knowledge_ingester=MagicMock(),
            sanitizer=MagicMock(),
            tracer=MagicMock(),
            vector_store=MagicMock(),
            db_session_factory=session_factory,
        )
        knowledge._index_document_in_vector_store = AsyncMock(return_value=3)
        content = valid_runbook("Redis Evictions Under Memory Pressure")

        await knowledge.upload_document(
            content=content,
            title="../../../escaped/pwned",
            document_type="runbook",
            scope="personal",
            owner_id="../../../../escaped",
        )

        assert sorted(tmp_path.rglob("*.md")) == []
        async with session_factory() as session:
            from sqlalchemy import select

            (stored,) = (
                await session.execute(select(ConversionDraftModel.content))
            ).scalars()
        assert stored == content


class TestTheEditBound:
    """``DRAFT_CONTENT_MAX_CHARS`` bounds what an edit may store, and the scan
    imports nothing over it, so no draft is born that cannot be edited."""

    def test_an_edit_over_the_bound_is_refused(self):
        from pydantic import ValidationError

        from faultmaven.modules.knowledge.domain.models.conversion import (
            DRAFT_CONTENT_MAX_CHARS,
            DraftUpdateRequest,
        )

        DraftUpdateRequest(content="x" * DRAFT_CONTENT_MAX_CHARS)
        with pytest.raises(ValidationError):
            DraftUpdateRequest(content="x" * (DRAFT_CONTENT_MAX_CHARS + 1))

    def test_the_largest_shipped_runbook_fits(self):
        from faultmaven.modules.knowledge.domain.models.conversion import (
            DRAFT_CONTENT_MAX_CHARS,
        )

        pack = Path(__file__).resolve().parents[4] / "resources/knowledge/pack"
        sizes = [
            len(p.read_text(encoding="utf-8")) for p in pack.rglob("runbooks/**/*.md")
        ]
        assert len(sizes) > 50, "corpus missing — the bound would be unmeasured"
        assert max(sizes) * 2 <= DRAFT_CONTENT_MAX_CHARS

    async def test_the_scan_refuses_a_file_over_the_bound(
        self, session_factory, tmp_path, monkeypatch
    ):
        from faultmaven.modules.knowledge.domain.models.conversion import (
            DRAFT_CONTENT_MAX_CHARS,
        )

        pod = _Replica(tmp_path, session_factory)
        (tmp_path / "global").mkdir()
        body = valid_runbook("An Enormous Runbook")
        padding = "x" * (DRAFT_CONTENT_MAX_CHARS + 1 - len(body))
        (tmp_path / "global" / "huge.md").write_text(body + padding)
        (tmp_path / "global" / "fine.md").write_text(valid_runbook("A Fine Runbook"))
        monkeypatch.setattr(
            ConversionService, "_data_dir", property(lambda self: tmp_path)
        )

        result = await pod.service.scan_for_runbooks(
            user_id="u1", is_platform_admin=True
        )

        assert result["discovered"] == 1, result
        assert result["errors"] == [
            f"huge.md: too long ({DRAFT_CONTENT_MAX_CHARS + 1} chars; a draft "
            f"holds at most {DRAFT_CONTENT_MAX_CHARS})"
        ]
