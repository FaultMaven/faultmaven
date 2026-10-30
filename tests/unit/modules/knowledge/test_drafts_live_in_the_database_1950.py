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

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.exceptions import ConflictError, ValidationException
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    EnterpriseModel,
    UserModel,
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
from faultmaven.modules.knowledge.draft_content import DRAFT_CONTENT_MAX_CHARS
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI
from tests.runbook_samples import valid_runbook
from tests.utils import recording_ingest, regex_only_sanitizer

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
            sanitizer=regex_only_sanitizer(),
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


def _ingesting(session_factory) -> MagicMock:
    """A knowledge service whose ingest runs the verify's ``record`` (#2396)."""
    knowledge = MagicMock()
    knowledge.ingest_runbook = recording_ingest(session_factory, chunks=4)
    return knowledge


class TestTheOtherReplicaServesTheDraft:
    async def test_read_edit_and_verify_on_a_pod_that_never_saw_a_file(
        self, session_factory, tmp_path, monkeypatch
    ):
        pod_a = _Replica(tmp_path / "pod-a", session_factory)
        pod_b = _Replica(
            tmp_path / "pod-b", session_factory, _ingesting(session_factory)
        )

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
            sanitizer=regex_only_sanitizer(),
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


@pytest.fixture
async def file_session_factory(tmp_path):
    """A shared database on disk: concurrent sessions are separate connections,
    as two replicas' are (an in-memory engine shares ONE connection)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/shared.db")
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


def _knowledge(session_factory) -> KnowledgeService:
    """The real knowledge service on the shared database; only the vector
    store is a double."""
    vector_store = MagicMock()
    vector_store.delete_documents_by_parent_id = AsyncMock(return_value=3)
    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=vector_store,
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=3)
    return service


async def _published_items(session_factory) -> list[str]:
    from sqlalchemy import select

    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    async with session_factory() as session:
        return list(
            (await session.execute(select(KnowledgeItemModel.item_id))).scalars()
        )


def _at_length(title: str, length: int) -> str:
    """A valid runbook padded with a trailing comment to exactly ``length``."""
    body = valid_runbook(title)
    return body + "<!-- " + "x" * (length - len(body) - 10) + " -->\n"


class TestTheEditBound:
    """``DRAFT_CONTENT_MAX_CHARS`` bounds every write of a draft's text, so no
    draft exists that cannot be saved back (#1950)."""

    def test_an_edit_over_the_bound_is_refused(self):
        from pydantic import ValidationError

        from faultmaven.modules.knowledge.domain.models.conversion import (
            DraftUpdateRequest,
        )

        DraftUpdateRequest(content="x" * DRAFT_CONTENT_MAX_CHARS)
        with pytest.raises(ValidationError):
            DraftUpdateRequest(content="x" * (DRAFT_CONTENT_MAX_CHARS + 1))

    def test_the_row_refuses_text_over_the_bound(self):
        """The one enforcement point every ORM write passes through."""
        ConversionDraftModel(content="x" * DRAFT_CONTENT_MAX_CHARS)
        with pytest.raises(ValidationException):
            ConversionDraftModel(content="x" * (DRAFT_CONTENT_MAX_CHARS + 1))
        row = ConversionDraftModel(content="short")
        with pytest.raises(ValidationException):
            row.content = "x" * (DRAFT_CONTENT_MAX_CHARS + 1)

    def test_the_largest_shipped_runbook_fits(self):
        pack = Path(__file__).resolve().parents[4] / "resources/knowledge/pack"
        sizes = [
            len(p.read_text(encoding="utf-8")) for p in pack.rglob("runbooks/**/*.md")
        ]
        assert len(sizes) > 50, "corpus missing — the bound would be unmeasured"
        assert max(sizes) * 2 <= DRAFT_CONTENT_MAX_CHARS

    async def test_the_scan_refuses_a_file_over_the_bound(
        self, session_factory, tmp_path, monkeypatch
    ):
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

    async def test_a_template_over_the_bound_is_a_422_and_writes_nothing(
        self, session_factory, tmp_path, monkeypatch
    ):
        from faultmaven.api.exception_handlers import get_exception_handlers
        from faultmaven.modules.knowledge.api import conversion_routes as cr

        monkeypatch.chdir(tmp_path)
        # Refused BEFORE the gate spends its CPU on the oversized text.
        gate = AsyncMock(side_effect=AssertionError("the gate ran"))
        monkeypatch.setattr(service_module, "avalidate_and_score", gate)
        pod = _Replica(tmp_path, session_factory)
        app = FastAPI()
        app.include_router(cr.router, prefix="/api/v1")
        for exc_type, handler in get_exception_handlers().items():
            app.add_exception_handler(exc_type, handler)
        user = MagicMock(user_id="u1", enterprise_id=None)
        user.is_platform_admin.return_value = False
        app.dependency_overrides[cr._require_auth] = lambda: user
        app.dependency_overrides[cr._get_conversion_service] = lambda: pod.service
        body = {
            key: TEMPLATE[key]
            for key in (
                "title",
                "domain",
                "service_name",
                "severity",
                "scope",
                "symptom_recognition",
                "applicability",
                "diagnostic_steps",
                "causes",
            )
        }
        body["service"] = body.pop("service_name")
        body["symptom_class"] = ["service_unavailable"]
        body["prevention"] = "Alert early. " + "y" * DRAFT_CONTENT_MAX_CHARS

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/v1/knowledge/runbooks/create", json=body)

        assert response.status_code == 422, response.text
        assert f"at most {DRAFT_CONTENT_MAX_CHARS} characters" in (
            response.json()["detail"]
        )
        gate.assert_not_awaited()
        async with session_factory() as session:
            assert (await session.get(ConversionDraftModel, "x")) is None
            from sqlalchemy import func, select

            count = await session.execute(
                select(func.count()).select_from(ConversionDraftModel)
            )
            assert count.scalar_one() == 0

    async def test_one_oversized_conversion_mode_fails_alone(
        self, session_factory, tmp_path, monkeypatch
    ):
        """A per-mode refusal: the other mode of the document still yields."""
        from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
        from faultmaven.modules.knowledge.domain.models.conversion import (
            AnalysisResult,
            FailureModeAnalysis,
            PreprocessingResult,
            SourceAssessment,
        )

        monkeypatch.chdir(tmp_path)
        huge = _at_length("Redis Memory Exhausted", DRAFT_CONTENT_MAX_CHARS + 50)
        fine = valid_runbook("Postgres Pool Exhausted")

        async def route(**kwargs):
            user = next(m for m in kwargs["messages"] if m["role"] == "user")
            content = huge if "Redis Memory" in user["content"] else fine
            return LLMResponse(
                content=content,
                confidence=0.9,
                provider="t",
                model="m",
                tokens_used=1,
                response_time_ms=1,
                stop_reason=StopReason.STOP,
            )

        pod = _Replica(tmp_path, session_factory)
        pod.service._llm_router.route = AsyncMock(side_effect=route)
        pod.service._settings.llm.explicit_role_provider.return_value = None
        pod.service._preprocessor.preprocess = AsyncMock(
            return_value=PreprocessingResult(
                extracted_text="source material", source_metadata={}
            )
        )
        modes = [
            FailureModeAnalysis(
                id=f"fm-{i}",
                title=title,
                domain="database",
                service=service,
                symptom_class=[symptom],
                severity="high",
                symptoms_summary="s",
                resolution_summary="r",
            )
            for i, (title, service, symptom) in enumerate(
                [
                    ("Redis Memory Exhausted", "redis", "resource_exhaustion"),
                    ("Postgres Pool Exhausted", "postgresql", "latency"),
                ]
            )
        ]
        source = tmp_path / "doc.md"
        source.write_text("source")
        analysis = AnalysisResult(
            is_actionable=True,
            failure_modes=modes,
            source_assessment=SourceAssessment(
                content_type="doc",
                actionability_rating="high",
                missing_information=[],
            ),
        )
        monkeypatch.setattr(
            service_module, "_analyze_document", AsyncMock(return_value=analysis)
        )

        response = await pod.service.convert_document(
            source, "text/markdown", "doc.md", "personal", "u1", None
        )

        assert [d.title for d in response.drafts] == ["Postgres Pool Exhausted"]
        assert response.status.value == "partial"
        assert any(
            "fm-0" in w and f"at most {DRAFT_CONTENT_MAX_CHARS}" in w
            for w in response.warnings
        ), response.warnings

    async def test_an_upload_over_the_bound_is_refused_before_ingest(
        self, session_factory, tmp_path, monkeypatch
    ):
        from faultmaven.modules.knowledge.domain.services import knowledge_service

        monkeypatch.chdir(tmp_path)
        # Refused BEFORE the quality gate spends its CPU on the oversized text.
        gate = AsyncMock(side_effect=AssertionError("the gate ran"))
        monkeypatch.setattr(knowledge_service, "aenforce_runbook_quality", gate)
        knowledge = _knowledge(session_factory)
        knowledge.ingest_runbook = AsyncMock(return_value=3)

        with pytest.raises(ValidationException, match="at most 100000 characters"):
            await knowledge.upload_document(
                content=_at_length("Redis Notes", DRAFT_CONTENT_MAX_CHARS + 1),
                title="Redis Notes",
                document_type="runbook",
                scope="personal",
                owner_id="u1",
            )

        knowledge.ingest_runbook.assert_not_awaited()
        gate.assert_not_awaited()
        async with session_factory() as session:
            from sqlalchemy import func, select

            count = await session.execute(
                select(func.count()).select_from(ConversionDraftModel)
            )
            assert count.scalar_one() == 0

    def test_the_upload_route_answers_the_bound_as_422(self):
        from types import SimpleNamespace

        from fastapi.testclient import TestClient

        from faultmaven.api.exception_handlers import get_exception_handlers
        from faultmaven.api.v1.auth_dependencies import require_authentication
        from faultmaven.modules.knowledge.api.routes import (
            get_knowledge_service,
        )
        from faultmaven.modules.knowledge.api.routes import router as kb_router

        refusing = SimpleNamespace(
            upload_document=AsyncMock(
                side_effect=ValidationException(
                    "A runbook draft holds at most 100000 characters; this one "
                    "has 100001."
                )
            )
        )
        app = FastAPI()
        app.include_router(kb_router, prefix="/api/v1")
        for exc_type, handler in get_exception_handlers().items():
            app.add_exception_handler(exc_type, handler)
        app.state.team_service = None

        async def _service():
            return refusing

        async def _user():
            return SimpleNamespace(
                user_id="u1",
                organization_id=None,
                enterprise_id="e1",
                is_platform_admin=lambda: False,
            )

        app.dependency_overrides[get_knowledge_service] = _service
        app.dependency_overrides[require_authentication] = _user
        response = TestClient(app).post(
            "/api/v1/knowledge/documents",
            data={"title": "Big", "document_type": "runbook", "scope": "personal"},
            files={"file": ("big.md", b"---\nid: x\n---\n# big\n", "text/markdown")},
        )

        assert response.status_code == 422, response.text
        assert "at most 100000 characters" in response.json()["detail"]

    async def test_a_draft_at_the_bound_still_verifies(
        self, session_factory, tmp_path, monkeypatch
    ):
        """Verify's frontmatter rewrite may lengthen the text past the bound;
        it is exempt, so the user is never refused a verify of a draft they
        were allowed to save."""
        monkeypatch.chdir(tmp_path)
        pod = _Replica(tmp_path, session_factory, _ingesting(session_factory))
        created = await pod.service.create_runbook_from_template(**TEMPLATE)
        cid, did = created["conversion_id"], created["draft"].draft_id
        at_bound = _at_length("Pool Exhausted At The Bound", DRAFT_CONTENT_MAX_CHARS)
        assert len(at_bound) == DRAFT_CONTENT_MAX_CHARS
        edited = await pod.service.update_draft(cid, did, "u1", at_bound)
        assert edited.validation.passed, edited.validation.errors

        result = await pod.service.verify_draft(cid, did, "u1")

        assert result.status == "verified"
        row = await _row(session_factory, did)
        assert row.status == "verified"
        assert len(row.content) > DRAFT_CONTENT_MAX_CHARS


class TestEditAndVerifyDoNotLoseEachOther:
    async def test_an_edit_during_ingestion_survives_and_nothing_is_published(
        self, file_session_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        factory = file_session_factory
        knowledge = _knowledge(factory)
        entered, release = asyncio.Event(), asyncio.Event()
        real_index = knowledge._index_document_in_vector_store

        async def slow_index(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_index(*args, **kwargs)

        knowledge._index_document_in_vector_store = slow_index
        verifier = _Replica(tmp_path / "pod-a", factory, knowledge).service
        editor = _Replica(tmp_path / "pod-b", factory).service
        created = await editor.create_runbook_from_template(**TEMPLATE)
        cid, did = created["conversion_id"], created["draft"].draft_id
        async with factory() as session:
            (await session.get(ConversionDraftModel, did)).validation_passed = True
            await session.commit()

        verifying = asyncio.create_task(verifier.verify_draft(cid, did, "u1"))
        await entered.wait()
        edit = valid_runbook("Pool Exhausted Edited During Verify")
        await editor.update_draft(cid, did, "u1", edit)
        release.set()
        with pytest.raises(ConflictError) as changed:
            await verifying

        assert changed.value.conflict_reason == "changed_during_verification"
        row = await _row(factory, did)
        assert (row.status, row.content) == ("draft", edit)
        assert await _published_items(factory) == []
        # The swap is the completing transaction's record (#2396): its miss
        # published nothing, so nothing is withdrawn.
        knowledge._vector_store.delete_documents_by_parent_id.assert_not_awaited()

    async def test_a_verified_draft_is_not_edited(
        self, session_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        pod = _Replica(tmp_path, session_factory, _ingesting(session_factory))
        created = await pod.service.create_runbook_from_template(**TEMPLATE)
        cid, did = created["conversion_id"], created["draft"].draft_id
        async with session_factory() as session:
            (await session.get(ConversionDraftModel, did)).validation_passed = True
            await session.commit()
        await pod.service.verify_draft(cid, did, "u1")
        verified_text = (await _row(session_factory, did)).content
        # Refused at once, not after the gate: the first session sees it.
        gate = AsyncMock(side_effect=AssertionError("the gate ran"))
        monkeypatch.setattr(service_module, "avalidate_and_score", gate)

        with pytest.raises(ConflictError) as refused:
            await pod.service.update_draft(
                cid, did, "u1", valid_runbook("An Edit Too Late")
            )

        assert refused.value.conflict_reason == "already_verified"
        assert (await _row(session_factory, did)).content == verified_text

    async def test_a_draft_verified_while_the_edit_is_scored_is_not_overwritten(
        self, session_factory, tmp_path, monkeypatch
    ):
        """The edit's second session re-checks: a verify that finished during
        the gate stands."""
        monkeypatch.chdir(tmp_path)
        pod = _Replica(tmp_path, session_factory, _ingesting(session_factory))
        created = await pod.service.create_runbook_from_template(**TEMPLATE)
        cid, did = created["conversion_id"], created["draft"].draft_id
        async with session_factory() as session:
            (await session.get(ConversionDraftModel, did)).validation_passed = True
            await session.commit()
        real_gate = service_module.avalidate_and_score

        async def _gate_then_verify(content):
            verdict = await real_gate(content)
            await pod.service.verify_draft(cid, did, "u1")
            return verdict

        monkeypatch.setattr(service_module, "avalidate_and_score", _gate_then_verify)
        with pytest.raises(ConflictError) as refused:
            await pod.service.update_draft(cid, did, "u1", valid_runbook("Too Late"))

        assert refused.value.conflict_reason == "already_verified"
        row = await _row(session_factory, did)
        assert row.status == "verified"
        assert "Too Late" not in row.content


class TestAnUploadsConversionOpens:
    async def test_get_conversion_scores_a_row_with_no_stored_score(
        self, session_factory, tmp_path, monkeypatch
    ):
        """A KB upload stores no quality score; opening its conversion used to
        500 on ``QualityScore(**{})``."""
        from sqlalchemy import select

        monkeypatch.chdir(tmp_path)
        content = valid_runbook("Redis Evictions Under Memory Pressure")
        await _knowledge(session_factory).upload_document(
            content=content,
            title="Redis evictions",
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )
        async with session_factory() as session:
            row = (await session.execute(select(ConversionDraftModel))).scalar_one()
        assert row.quality_details is None

        opened = await _Replica(tmp_path, session_factory).service.get_conversion(
            row.conversion_id, "u1"
        )

        (draft,) = opened.drafts
        assert draft.content == content
        assert draft.quality_score.overall > 0
        assert draft.quality_score.grade


# ---------------------------------------------------------------------------
# Races, on SQLite AND PostgreSQL (#1961 review)
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
                    reason="PostgreSQL-only; set DATABASE_URL to a PG instance.",
                ),
            ],
        ),
    ]
)
async def race_factory(request, tmp_path):
    """A shared database where concurrent sessions are real connections: SQLite
    on disk with the production PRAGMAs, or a fresh PostgreSQL database."""
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")

        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        async for factory in _seeded(engine):
            yield factory
        return

    from tests.integration.security.conftest import database_of_its_own

    async with database_of_its_own(os.environ["DATABASE_URL"], "fm_1950_race") as url:
        engine = create_async_engine(url)
        async for factory in _seeded(engine):
            yield factory


async def _seeded(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.flush()
        # PostgreSQL enforces the author/verifier foreign keys.
        session.add(
            UserModel(
                user_id="u1",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                username="u1",
                email="u1@example.com",
                display_name="U One",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _verifiable_draft(factory, root: Path) -> tuple[str, str]:
    created = await _Replica(root, factory).service.create_runbook_from_template(
        **TEMPLATE
    )
    cid, did = created["conversion_id"], created["draft"].draft_id
    async with factory() as session:
        (await session.get(ConversionDraftModel, did)).validation_passed = True
        await session.commit()
    return cid, did


async def _published(factory) -> dict:
    from sqlalchemy import select

    from faultmaven.infrastructure.persistence.models import KnowledgeItemModel

    async with factory() as session:
        rows = (await session.execute(select(KnowledgeItemModel))).scalars()
        return {row.item_id: row.content for row in rows}


def _hooked(factory, *, before_dml=None, before_commit=None):
    """A session factory whose sessions run ``before_dml`` just before their
    first conversion_drafts UPDATE executes, or ``before_commit`` just before a
    commit that follows one."""

    def make():
        session = factory()
        real_execute, real_commit = session.execute, session.commit
        state = {"dml": False}

        async def execute(statement, *args, **kwargs):
            is_update = getattr(statement, "is_dml", False) and (
                "conversion_drafts" in str(statement)
            )
            if is_update and not state["dml"] and before_dml is not None:
                await before_dml()
            result = await real_execute(statement, *args, **kwargs)
            state["dml"] = state["dml"] or is_update
            return result

        async def commit():
            if state["dml"] and before_commit is not None:
                await before_commit()
            return await real_commit()

        session.execute = execute
        session.commit = commit
        return session

    return make


class TestEditAndVerifyRaceOnBothDialects:
    async def test_a_verify_landing_before_the_edits_write_refuses_the_edit(
        self, race_factory, tmp_path, monkeypatch
    ):
        """The edit has read the row as a draft and scored its text; a verify
        commits before the edit writes. The edit's write is conditional on the
        row still being a draft, so it writes nothing: the row stays the
        published text, and the editor hears 409 instead of a silent loss."""
        monkeypatch.chdir(tmp_path)
        factory = race_factory
        cid, did = await _verifiable_draft(factory, tmp_path)
        knowledge = _knowledge(factory)
        verifier = _Replica(tmp_path / "a", factory, knowledge).service

        async def verify_now():
            await verifier.verify_draft(cid, did, "u1")

        editor = _Replica(tmp_path / "b", factory).service
        editor._db_session_factory = _hooked(factory, before_dml=verify_now)
        edit = valid_runbook("An Edit That Read The Draft Before Verify")

        with pytest.raises(ConflictError) as refused:
            await editor.update_draft(cid, did, "u1", edit)

        assert refused.value.conflict_reason == "already_verified"
        row = await _row(factory, did)
        published = await _published(factory)
        assert row.status == "verified"
        assert published == {row.knowledge_item_id: row.content}
        assert "An Edit That Read The Draft" not in row.content

    async def test_an_edit_against_verifys_uncommitted_write_is_refused(
        self, race_factory, tmp_path, monkeypatch
    ):
        """Verify's compare-and-swap has run but not committed when the edit
        writes. The edit waits for it (row lock on PostgreSQL, the write lock
        on SQLite), then finds the row verified and writes nothing."""
        monkeypatch.chdir(tmp_path)
        factory = race_factory
        cid, did = await _verifiable_draft(factory, tmp_path)
        swapped, resume = asyncio.Event(), asyncio.Event()

        async def hold_commit():
            swapped.set()
            await resume.wait()

        knowledge = _knowledge(factory)
        # The swap commits in the knowledge service's completing transaction.
        knowledge._db_session_factory = _hooked(factory, before_commit=hold_commit)
        verifier = _Replica(tmp_path / "a", factory, knowledge).service
        editor = _Replica(tmp_path / "b", factory).service

        verifying = asyncio.create_task(verifier.verify_draft(cid, did, "u1"))
        await swapped.wait()
        editing = asyncio.create_task(
            editor.update_draft(cid, did, "u1", valid_runbook("Edit Against Verify"))
        )
        done, _ = await asyncio.wait({editing}, timeout=1)
        assert not done, "the edit wrote while verify's write was uncommitted"
        resume.set()
        assert (await verifying).status == "verified"
        with pytest.raises(ConflictError) as refused:
            await editing

        assert refused.value.conflict_reason == "already_verified"
        row = await _row(factory, did)
        assert await _published(factory) == {row.knowledge_item_id: row.content}
        assert "Edit Against Verify" not in row.content

    async def test_two_verifies_publish_once_and_the_loser_is_already_verified(
        self, race_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        factory = race_factory
        cid, did = await _verifiable_draft(factory, tmp_path)
        knowledge = _knowledge(factory)
        both = asyncio.Barrier(2)
        real_index = knowledge._index_document_in_vector_store

        async def index_together(*args, **kwargs):
            await both.wait()
            return await real_index(*args, **kwargs)

        knowledge._index_document_in_vector_store = index_together
        one = _Replica(tmp_path / "a", factory, knowledge).service
        two = _Replica(tmp_path / "b", factory, knowledge).service

        results = await asyncio.gather(
            one.verify_draft(cid, did, "u1"),
            two.verify_draft(cid, did, "u1"),
            return_exceptions=True,
        )

        won = [r for r in results if not isinstance(r, Exception)]
        lost = [r for r in results if isinstance(r, Exception)]
        assert len(won) == 1 and len(lost) == 1, results
        assert isinstance(lost[0], ConflictError)
        assert lost[0].conflict_reason == "already_verified"
        assert "nothing was published by this request" in str(lost[0])
        row = await _row(factory, did)
        assert row.knowledge_item_id == won[0].knowledge_item_id
        assert await _published(factory) == {row.knowledge_item_id: row.content}

    async def test_a_delete_during_verify_wins_and_nothing_stays_published(
        self, race_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        factory = race_factory
        cid, did = await _verifiable_draft(factory, tmp_path)
        knowledge = _knowledge(factory)
        entered, release = asyncio.Event(), asyncio.Event()
        real_index = knowledge._index_document_in_vector_store

        async def slow_index(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_index(*args, **kwargs)

        knowledge._index_document_in_vector_store = slow_index
        verifier = _Replica(tmp_path / "a", factory, knowledge).service
        verifying = asyncio.create_task(verifier.verify_draft(cid, did, "u1"))
        await entered.wait()
        assert await _Replica(tmp_path / "b", factory).service.delete_draft(
            cid, did, "u1"
        )
        release.set()
        with pytest.raises(ConflictError) as lost:
            await verifying

        assert lost.value.conflict_reason == "discarded"
        assert (await _row(factory, did)).status == "discarded"
        assert await _published(factory) == {}


class TestALostSwapHasNothingToWithdraw:
    async def test_an_edit_during_verify_publishes_nothing_and_deletes_nothing(
        self, file_session_factory, tmp_path, monkeypatch, caplog
    ):
        """The swap lost to an edit. It is the completing transaction's record
        (#2396), so nothing was published, and the 409 says so. Replaces the
        test of a withdrawal that failed, whose withdrawal is gone."""
        monkeypatch.chdir(tmp_path)
        factory = file_session_factory
        cid, did = await _verifiable_draft(factory, tmp_path)
        knowledge = _knowledge(factory)
        entered, release = asyncio.Event(), asyncio.Event()
        real_index = knowledge._index_document_in_vector_store

        async def slow_index(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_index(*args, **kwargs)

        knowledge._index_document_in_vector_store = slow_index
        verifier = _Replica(tmp_path / "a", factory, knowledge).service
        verifying = asyncio.create_task(verifier.verify_draft(cid, did, "u1"))
        await entered.wait()
        await _Replica(tmp_path / "b", factory).service.update_draft(
            cid, did, "u1", valid_runbook("Edited While Verifying")
        )
        release.set()
        with caplog.at_level("ERROR"), pytest.raises(ConflictError) as lost:
            await verifying

        assert lost.value.conflict_reason == "changed_during_verification"
        assert "nothing was published by this request" in str(lost.value)
        assert await _published(factory) == {}
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
