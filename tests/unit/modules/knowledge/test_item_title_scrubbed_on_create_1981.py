"""A knowledge item's title is scrubbed on create exactly as on update (#1981).

A title is shown to every reader of the tier its item lives in (team and global
included), in every list, and nothing ever reverses it. ``update_document``
redacted it; every create path stored it verbatim, so a title typed into an
upload form as ``Fix login (password=hunter2)`` was published as written, while
the same string typed into an edit was redacted.

These tests drive the REAL services over in-memory SQLite with a REAL
``DataSanitizer`` (regex half: ``regex_only_sanitizer``, no network, pinned
pseudonym key, so the result is the same under the Standalone and Cloud CI
profiles). Only the ChromaDB write is a double. Each create path is asserted on
the STORED rows, at every scope, and each names the mutation that must fail it.

Every path that writes a title:

====================================  =========  ==============================
writer                                scrubbed   test
====================================  =========  ==============================
``ingest_runbook`` (the one place a   yes        ``TestIngestRunbook``
``knowledge_items`` row is created)
``upload_document``                   yes        ``TestUploadDocument``
``verify_draft`` (publish)            yes        ``TestVerifyDraft``
``create_runbook_from_template``      yes        ``TestDraftsAreNotAuthorOnly``
``_persist_job_rows`` (every          yes        ``TestDraftsAreNotAuthorOnly``
``conversion_drafts`` row)
KB pack / ``kb_seed``                 NO, by     ``TestShippedPackTitle``
                                      decision
``update_document_metadata``          yes        ``TestUpdateDocument``
====================================  =========  ==============================
"""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import re
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import FaultMavenSettings, env_var_spellings
from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.infrastructure.security.pseudonym_key import (
    PseudonymKeyUnavailableError,
)
from faultmaven.infrastructure.security.redaction import (
    DataSanitizer,
    RedactionUnavailableError,
)
from faultmaven.models import KnowledgeBaseDocument
from faultmaven.modules.knowledge.api import routes
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.item_title import (
    MAX_SCRUB_PASSES,
    fit_to_column,
    sanitize_item_title,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    CaseConversionRequest,
    ConversionDraft,
    ConversionStatus,
    DraftStatus,
    FailureModeAnalysis,
    PreprocessingResult,
    QualityScore,
    RedactionReport,
    SourceAssessment,
    SourceFileInfo,
    SourceType,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.job_persistence import (  # noqa: E501
    _persist_job,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.exceptions import ItemTitleScrubUnavailable
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

#: ``(label, the secret, a title carrying it)``. The first four are the
#: sanitizer's secret set as a title can hold it (an error message pasted into
#: one). The last two ABUT: a key glued after an address. On main the
#: ``password`` pattern needed a word boundary, which the digit or hex digit
#: before the ``p`` removed until the IP or MAC pattern had replaced the
#: address, so ONE pass left ``password=hunter2`` in place and a second took
#: it out, and a title scrubbed once and the same title scrubbed twice
#: differed. Since #1980 one pass takes it (the finder's masked rounds); the
#: shapes stay, since the helper's fixed point must not rest on that.
SECRET_TITLES = [
    ("password", "hunter2", "Login fails after rotation (password=hunter2)"),
    (
        "openai_key",
        "sk-" + "a1B2c3D4" * 4,
        "Calls 401 with sk-" + "a1B2c3D4" * 4 + " on the proxy",
    ),
    ("aws_key", "AKIAIOSFODNN7EXAMPLE", "S3 denied for AKIAIOSFODNN7EXAMPLE today"),
    (
        "database_url",
        "postgresql://app:Winter2024x@db.internal",
        "Pool exhausted at postgresql://app:Winter2024x@db.internal:5432/orders",
    ),
    ("ip_then_password", "hunter2", "Login fails (10.0.0.5password=hunter2)"),
    ("mac_then_password", "hunter2", "Login fails (aa:bb:cc:dd:ee:ffpassword=hunter2)"),
]
SECRET_IDS = [label for label, _, _ in SECRET_TITLES]
ABUTTING = [t for t in SECRET_TITLES if t[0].endswith("_then_password")]
ABUTTING_IDS = [label for label, _, _ in ABUTTING]

#: A title with nothing in it for the sanitizer to find.
PLAIN_TITLE = "Redis Out Of Memory Recovery"

#: What a redacted span becomes: ``<TYPE_<16 hex>>``.
PLACEHOLDER_RE = re.compile(r"<[A-Z_]+_[0-9a-f]{16}>")

#: Every tier a runbook can be written at. The scrub is not a property of the
#: personal tier: a team title is shown to the team and a global one to everyone.
SCOPES = ["personal", "team", "global"]

TEAM_ID = "11111111-1111-4111-8111-111111111111"


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


def _failing_sanitizer(error: Exception) -> MagicMock:
    sanitizer = MagicMock()
    sanitizer.asanitize = AsyncMock(side_effect=error)
    return sanitizer


def _cloud_sanitizer_without_a_key() -> DataSanitizer:
    """A real sanitizer for a cloud deployment that has no pseudonym key: it
    redacts nothing it does not have to, and raises ``PseudonymKeyUnavailableError``
    the moment it must write a placeholder."""
    with pytest.MonkeyPatch.context() as mp:
        for key in env_var_spellings(os.environ, "REDACTION_PSEUDONYM_KEY"):
            mp.delenv(key)
        mp.setenv("DEPLOYMENT_MODE", "cloud")
        mp.setenv("SKIP_SERVICE_CHECKS", "true")
        settings = FaultMavenSettings()
    assert settings.is_cloud and settings.protection.pseudonym_key is None
    return DataSanitizer(settings=settings)


def _knowledge(session_factory, sanitizer=None) -> KnowledgeService:
    service = KnowledgeService(
        sanitizer=sanitizer if sanitizer is not None else regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=5)
    return service


def _conversion(session_factory, knowledge=None, sanitizer=None) -> ConversionService:
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=session_factory,
        knowledge_service=knowledge,
        sanitizer=sanitizer if sanitizer is not None else regex_only_sanitizer(),
    )


async def _runbook_titles(session_factory) -> list[str]:
    """The title column of every ``knowledge_items`` row."""
    async with session_factory() as session:
        return list((await session.execute(select(KnowledgeItemModel.title))).scalars())


async def _draft_titles(session_factory) -> list[str]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(ConversionDraftModel.title))).scalars()
        )


async def _row_counts(session_factory) -> dict[str, int]:
    """How many rows each table a create touches holds."""
    async with session_factory() as session:
        return {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in (
                KnowledgeItemModel,
                ConversionDraftModel,
                ConversionJobModel,
                UploadedFileModel,
            )
        }


def _assert_scrubbed(stored: str, secret: str) -> None:
    assert secret not in stored, f"the secret survived: {stored!r}"
    assert PLACEHOLDER_RE.search(stored), f"nothing was redacted: {stored!r}"


def _template(**override) -> dict:
    """``create_runbook_from_template`` arguments for a valid manual runbook."""
    fields = dict(
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
            "### Step 1: Check pool usage\n```bash\npsql -c 'select count(*) "
            "from pg_stat_activity'\n```\nLook for a count near the pool ceiling."
        ),
        causes=(
            "### Cause A: Pool leak\n"
            "**Statement:** connections are never returned to the pool.\n"
            "**Indicators:**\n- root: [Step 1] active connections pinned\n"
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
    fields.update(override)
    return fields


def _ingest_args(scope: str, **override) -> dict:
    args = dict(
        document_id="kb_0123456789ab",
        title=PLAIN_TITLE,
        content="# Body\n\nIncrease maxmemory.",
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope=scope,
        owner_id="u1",
        team_id=TEAM_ID if scope == "team" else None,
    )
    args.update(override)
    return args


class TestIngestRunbook:
    """``ingest_runbook`` builds the only ``KnowledgeItem`` the service creates;
    uploads, published drafts and the pack all reach it.

    Mutation: delete the ``sanitize_item_title`` call at the top of
    ``ingest_runbook``, or run it only for the personal tier. Every test here
    fails; the upload tests below still pass on their own rows (they scrub
    first), which is why each layer asserts its own observable.
    """

    @pytest.mark.parametrize("scope", SCOPES)
    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_the_stored_row_and_the_vectors_carry_the_scrubbed_title(
        self, session_factory, label, secret, title, scope
    ):
        service = _knowledge(session_factory)

        await service.ingest_runbook(**_ingest_args(scope, title=title))

        (stored,) = await _runbook_titles(session_factory)
        _assert_scrubbed(stored, secret)
        # The text around the secret is kept: this redacts, it does not blank.
        assert stored.split()[0] == title.split()[0]
        # The vector metadata is built from the same variable. A title that was
        # scrubbed in the row only would still be searchable (and shown in
        # every search result) in clear.
        indexed: KnowledgeBaseDocument = (
            service._index_document_in_vector_store.await_args.args[0]
        )
        assert indexed.title == stored

    @pytest.mark.parametrize("scope", SCOPES)
    async def test_an_ordinary_title_is_stored_as_written(self, session_factory, scope):
        service = _knowledge(session_factory)

        await service.ingest_runbook(**_ingest_args(scope))

        assert await _runbook_titles(session_factory) == [PLAIN_TITLE]

    @pytest.mark.parametrize("scope", SCOPES)
    @pytest.mark.parametrize(
        "sanitizer",
        [
            _failing_sanitizer(RedactionUnavailableError("down")),
            _failing_sanitizer(PseudonymKeyUnavailableError("no key")),
            _failing_sanitizer(RuntimeError("anything at all")),
            None,
        ],
        ids=["redaction_unavailable", "no_pseudonym_key", "any_error", "no_sanitizer"],
    )
    async def test_a_title_that_cannot_be_scrubbed_is_not_written(
        self, session_factory, sanitizer, scope
    ):
        """Fail closed, before any write: nothing to compensate."""
        service = _knowledge(session_factory)
        service._sanitizer = sanitizer

        with pytest.raises(ItemTitleScrubUnavailable):
            await service.ingest_runbook(
                **_ingest_args(scope, title="Login fails (password=hunter2)")
            )

        assert await _row_counts(session_factory) == dict.fromkeys(
            (
                "knowledge_items",
                "conversion_drafts",
                "conversion_jobs",
                "uploaded_files",
            ),
            0,
        )
        service._index_document_in_vector_store.assert_not_awaited()


class TestUploadDocument:
    """``upload_document`` writes the upload row and the draft row from the
    title BEFORE ``ingest_runbook`` runs, so it scrubs first, and the copies
    must be one string.

    Mutation: delete the ``sanitize_item_title`` call in ``upload_document``, or
    run it only for the personal tier. ``upload_document``'s own rows and
    response then carry the secret (``ingest_runbook`` still scrubs the
    ``knowledge_items`` row, which is why the assertions here read the others).
    """

    @pytest.mark.parametrize("scope", SCOPES)
    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_every_row_and_the_response_carry_one_scrubbed_title(
        self, session_factory, label, secret, title, scope
    ):
        service = _knowledge(session_factory)

        result = await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title=title,
            document_type="runbook",
            scope=scope,
            owner_id="u1",
            team_id=TEAM_ID if scope == "team" else None,
        )

        (item_title,) = await _runbook_titles(session_factory)
        (draft_title,) = await _draft_titles(session_factory)
        async with session_factory() as session:
            (filename,) = (
                await session.execute(select(UploadedFileModel.filename))
            ).scalars()
        indexed = service._index_document_in_vector_store.await_args.args[0]
        _assert_scrubbed(item_title, secret)
        # ONE text everywhere, and the same text a one-off scrub gives: each
        # copy is scrubbed to a fixed point, so five copies agree whatever the
        # sanitizer's own idempotence (see SECRET_TITLES).
        copies = {
            "draft": draft_title,
            "filename": filename,
            "response": result["metadata"]["title"],
            "vector": indexed.title,
        }
        assert {k: v for k, v in copies.items() if v != item_title} == {}
        assert all(secret not in v for v in copies.values()), copies

    async def test_an_ordinary_title_is_stored_as_written(self, session_factory):
        service = _knowledge(session_factory)

        result = await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title=PLAIN_TITLE,
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )

        assert await _runbook_titles(session_factory) == [PLAIN_TITLE]
        assert await _draft_titles(session_factory) == [PLAIN_TITLE]
        assert result["metadata"]["title"] == PLAIN_TITLE

    @pytest.mark.parametrize(
        "make_sanitizer",
        [
            lambda: _failing_sanitizer(RedactionUnavailableError("down")),
            _cloud_sanitizer_without_a_key,
        ],
        ids=["redaction_unavailable", "cloud_with_no_pseudonym_key"],
    )
    async def test_a_title_that_cannot_be_scrubbed_leaves_no_row_behind(
        self, session_factory, make_sanitizer
    ):
        """The cloud case runs the REAL sanitizer: with no pseudonym key it raises
        ``PseudonymKeyUnavailableError`` the moment it must write a placeholder.
        Failing OPEN on that error (publishing the raw title) is the mutation."""
        service = _knowledge(session_factory, make_sanitizer())
        before = await _row_counts(session_factory)

        with pytest.raises(ItemTitleScrubUnavailable):
            await service.upload_document(
                content=valid_runbook("Redis Notes For The Cache Tier"),
                title="Login fails (password=hunter2)",
                document_type="runbook",
                scope="personal",
                owner_id="u1",
            )

        assert await _row_counts(session_factory) == before
        service._index_document_in_vector_store.assert_not_awaited()

    async def test_the_stored_filename_and_draft_title_fit_their_columns(
        self, session_factory
    ):
        """Scrubbing makes a title LONGER (``password=x``, 10 characters, becomes a
        27-character placeholder) and both columns are ``String(255)``: SQLite
        keeps the overflow, PostgreSQL refuses it with a 500. Mutation: drop
        ``fit_to_column`` from ``upload_document``."""
        title = "x" * 250 + " password=a password=b password=c"
        service = _knowledge(session_factory)

        await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title=title,
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )

        async with session_factory() as session:
            (filename,) = (
                await session.execute(select(UploadedFileModel.filename))
            ).scalars()
        (draft_title,) = await _draft_titles(session_factory)
        assert len(filename) == len(draft_title) == 255
        assert "password=" not in filename + draft_title


class TestVerifyDraft:
    """Publishing a draft (``verify_draft``) is the third creator of a
    ``knowledge_items`` row, and it reaches ``ingest_runbook`` ONCE: there is no
    earlier scrub to lean on, so an abutting title is the test of whether one
    pass is enough.

    Mutation: delete the call in ``ingest_runbook``: the published row carries
    the secret. Delete the call and the CAS ``title=`` in ``verify_draft``: a
    draft row from before this change keeps its raw title after publication.
    """

    @pytest.mark.parametrize("scope", ["personal", "global"])
    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_the_published_item_carries_the_scrubbed_title(
        self, session_factory, label, secret, title, scope, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        created = await conversion.create_runbook_from_template(
            **_template(title=title, scope=scope)
        )
        draft_id = created["draft"].draft_id
        async with session_factory() as session:
            (await session.get(ConversionDraftModel, draft_id)).validation_passed = True
            await session.commit()

        await conversion.verify_draft(
            created["conversion_id"], draft_id, "u1", is_platform_admin=True
        )

        (published,) = await _runbook_titles(session_factory)
        _assert_scrubbed(published, secret)
        indexed = knowledge._index_document_in_vector_store.await_args.args[0]
        assert indexed.title == published

    async def test_a_draft_row_that_still_holds_a_raw_title_is_scrubbed_at_publish(
        self, session_factory, tmp_path, monkeypatch
    ):
        """A draft written before titles were scrubbed (or by any path other than
        conversion's persistence) is listed to everyone after publication as
        before; publishing is the last place to scrub it."""
        monkeypatch.chdir(tmp_path)
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        created = await conversion.create_runbook_from_template(
            **_template(scope="global")
        )
        draft_id = created["draft"].draft_id
        async with session_factory() as session:
            row = await session.get(ConversionDraftModel, draft_id)
            row.title = "Login fails (password=hunter2)"
            row.validation_passed = True
            await session.commit()

        await conversion.verify_draft(
            created["conversion_id"], draft_id, "u1", is_platform_admin=True
        )

        (draft_title,) = await _draft_titles(session_factory)
        (item_title,) = await _runbook_titles(session_factory)
        _assert_scrubbed(draft_title, "hunter2")
        assert draft_title == item_title


class TestDraftsAreNotAuthorOnly:
    """A draft is not private to its author. ``GET /knowledge/drafts``
    (``list_all_drafts``) lists every GLOBAL-scope draft to every user, and a
    case's drafts (``list_drafts_for_case``) are listed to the case's readers.
    So a title the author typed or a model wrote is disclosed from the moment
    its ``conversion_drafts`` row exists, long before it is published.

    Mutation: delete the scrub in ``_persist_job_rows``; delete the one in
    ``create_runbook_from_template``.
    """

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_a_global_draft_is_listed_to_another_user_scrubbed(
        self, session_factory, label, secret, title, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        conversion = _conversion(session_factory, _knowledge(session_factory))

        created = await conversion.create_runbook_from_template(
            **_template(title=title, scope="global", user_id="admin1")
        )

        (listed,) = await conversion.list_all_drafts("some-other-user")
        _assert_scrubbed(listed["title"], secret)
        # Everything derived from the typed title: the draft's id slug (the
        # slug of ``password=hunter2`` is ``...-password-hunter2``), its text,
        # and the source file's name.
        assert secret not in listed["runbook_id"]
        assert secret not in created["draft"].content
        assert secret not in created["draft"].runbook_id
        async with session_factory() as session:
            (filename,) = (
                await session.execute(select(UploadedFileModel.filename))
            ).scalars()
        assert secret not in filename

    async def test_a_global_draft_stays_scrubbed_after_it_is_published(
        self, session_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        created = await conversion.create_runbook_from_template(
            **_template(
                title="Login fails (password=hunter2)", scope="global", user_id="admin1"
            )
        )
        draft_id = created["draft"].draft_id
        async with session_factory() as session:
            (await session.get(ConversionDraftModel, draft_id)).validation_passed = True
            await session.commit()

        await conversion.verify_draft(
            created["conversion_id"], draft_id, "admin1", is_platform_admin=True
        )

        (listed,) = await conversion.list_all_drafts("some-other-user")
        _assert_scrubbed(listed["title"], "hunter2")

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_a_case_draft_a_model_titled_is_listed_to_case_readers_scrubbed(
        self, session_factory, label, secret, title
    ):
        """The model-written title: the persistence choke covers a draft no
        template or upload ever saw."""
        conversion = _conversion(session_factory)
        draft_id = "draft_case_1981"
        draft = ConversionDraft(
            draft_id=draft_id,
            runbook_id="rb-case-1981",
            title=title,
            scope="personal",
            status=DraftStatus.DRAFT,
            source_type=SourceType.CASE,
            validation=ValidationResult(passed=True),
            quality_score=QualityScore(
                overall=80.0,
                grade="B",
                completeness=80.0,
                clarity=80.0,
                actionability=80.0,
                comprehensiveness=80.0,
            ),
            content_preview="preview",
            content="---\nid: rb-case-1981\n---\n\n# Case Runbook\n",
        )

        await _persist_job(
            session_factory,
            None,
            conversion_id="conv_case_1981",
            user_id="u1",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            team_id=None,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(
                filename="Case c1", size_bytes=1, content_type="x"
            ),
            analysis=AnalysisResult(
                is_actionable=True,
                failure_modes=[
                    FailureModeAnalysis(
                        id="case-x",
                        title="API 500s",
                        domain="application",
                        service="api",
                        symptom_class=["errors"],
                        severity="medium",
                        symptoms_summary="500s",
                        resolution_summary="pool exhausted",
                    )
                ],
                source_assessment=SourceAssessment(
                    content_type="resolved_case",
                    actionability_rating="high",
                    missing_information=[],
                ),
            ),
            drafts=[draft],
            created_at=datetime.now(timezone.utc),
            source_type="case",
            case_id="case-x",
            sanitizer=regex_only_sanitizer(),
        )

        (listed,) = await conversion.list_drafts_for_case("case-x")
        _assert_scrubbed(listed["title"], secret)

    async def test_an_ordinary_title_is_listed_as_written(
        self, session_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        conversion = _conversion(session_factory)

        await conversion.create_runbook_from_template(
            **_template(title=PLAIN_TITLE, scope="global", user_id="admin1")
        )

        (listed,) = await conversion.list_all_drafts("some-other-user")
        assert listed["title"] == PLAIN_TITLE

    @pytest.mark.parametrize(
        "make_sanitizer",
        [
            lambda: _failing_sanitizer(RedactionUnavailableError("down")),
            _cloud_sanitizer_without_a_key,
            lambda: None,
        ],
        ids=["redaction_unavailable", "cloud_with_no_pseudonym_key", "no_sanitizer"],
    )
    async def test_a_draft_title_that_cannot_be_scrubbed_writes_nothing(
        self, session_factory, make_sanitizer, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        conversion = ConversionService(
            llm_router=MagicMock(),
            settings=MagicMock(),
            db_session_factory=session_factory,
            sanitizer=make_sanitizer(),
        )
        before = await _row_counts(session_factory)

        with pytest.raises(ItemTitleScrubUnavailable):
            await conversion.create_runbook_from_template(
                **_template(title="Login fails (password=hunter2)")
            )

        assert await _row_counts(session_factory) == before


class TestShippedPackTitle:
    """The KB pack and ``kb_seed`` are the one exemption, by decision.

    A pack title is vendored, reviewed source (``resources/knowledge/pack``,
    built and checked in CI), not user or model text. The same function serves
    both, so the exemption is an explicit keyword (``shipped=True``) that
    ``kb_init`` states; a caller that says nothing gets the scrub.

    Why exempt at all: under ``TENANT_PROVIDER=multi`` the ``kb_seed`` job calls
    ``ingest_runbook`` for every shipped runbook, and a fail-closed Presidio
    error that begins AFTER a healthy start (the analyzer answering, then not)
    would raise out of the scrub and stop the platform corpus from seeding. A
    seed that STARTS with the analyzer down runs regex-only and does not raise,
    so the exemption covers the mid-seed outage and nothing more.

    Mutation: make ``ingest_runbook`` ignore ``shipped``: this fails. That
    ``kb_init`` states ``shipped=True`` for every pack runbook is pinned where
    its other ingest arguments are, in
    ``tests/unit/bootstrap/test_kb_init.py::test_bootstrap_ingests_new_runbook``,
    and ``TestOnlyKbInitIsShipped`` pins that nothing else does.
    """

    async def test_a_shipped_title_is_stored_verbatim_and_never_touches_the_sanitizer(
        self, session_factory
    ):
        sanitizer = _failing_sanitizer(RedactionUnavailableError("down"))
        service = _knowledge(session_factory, sanitizer)
        # Not a title a pack would carry; the point is that it is not rewritten.
        shipped_title = "Rotate the password=<NEW_PASSWORD> on the replica"

        await service.ingest_runbook(
            **_ingest_args("global", title=shipped_title, shipped=True)
        )

        assert await _runbook_titles(session_factory) == [shipped_title]
        sanitizer.asanitize.assert_not_awaited()


class TestUpdateDocument:
    """The path that always scrubbed, now through the one helper. The same
    input must land on the same placeholder as it does on create: one rule, not
    two that happen to agree."""

    async def test_update_and_create_apply_the_same_rule(self, session_factory):
        service = _knowledge(session_factory)
        title = "Login fails after rotation (password=hunter2)"
        await service.ingest_runbook(**_ingest_args("personal"))
        await service.ingest_runbook(
            **_ingest_args("personal", document_id="kb_0123456789ac", title=title)
        )

        await service.update_document_metadata("kb_0123456789ab", title=title)

        async with session_factory() as session:
            rows = {
                row.item_id: row.title
                for row in (await session.execute(select(KnowledgeItemModel))).scalars()
            }
        _assert_scrubbed(rows["kb_0123456789ab"], "hunter2")
        assert rows["kb_0123456789ab"] == rows["kb_0123456789ac"]

    @pytest.mark.parametrize("label,secret,title", ABUTTING, ids=ABUTTING_IDS)
    async def test_an_abutting_title_leaves_no_secret_after_one_update(
        self, session_factory, label, secret, title
    ):
        """An update scrubs once and does nothing else with the title, so the
        helper's own settling is all that stands between a one-pass sanitizer and
        a published secret. Mutation: make the helper return after one pass."""
        service = _knowledge(session_factory)
        await service.ingest_runbook(**_ingest_args("personal"))

        await service.update_document_metadata("kb_0123456789ab", title=title)

        (stored,) = await _runbook_titles(session_factory)
        _assert_scrubbed(stored, secret)

    async def test_an_ordinary_title_edit_is_unchanged(self, session_factory):
        service = _knowledge(session_factory)
        await service.ingest_runbook(**_ingest_args("personal", title="Old"))

        await service.update_document_metadata("kb_0123456789ab", title=PLAIN_TITLE)

        assert await _runbook_titles(session_factory) == [PLAIN_TITLE]


# ---------------------------------------------------------------------------
# Everything derived from a title, and the column cut
# ---------------------------------------------------------------------------

CASE_ID = "case_aa0000001981"

#: The key in ``SECRET_TITLES``, in 8-character blocks, so a fragment of it is
#: recognisable wherever a cut leaves one.
KEY_BLOCK = "a1B2c3D4"


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    return settings


def _model_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=10,
        stop_reason=StopReason.STOP,
    )


def _analysis_json(title: str) -> str:
    return json.dumps(
        {
            "is_actionable": True,
            "failure_modes": [
                {
                    "id": "pg-login",
                    "title": title,
                    "domain": "database",
                    "service": "postgresql",
                    "symptom_class": ["replication_lag"],
                    "severity": "high",
                    "symptoms_summary": "The replica login fails",
                    "resolution_summary": "Rotate the credentials",
                }
            ],
            "source_assessment": {
                "content_type": "troubleshooting_guide",
                "actionability_rating": "high",
                "missing_information": [],
            },
        }
    )


def _preprocessed(text: str) -> PreprocessingResult:
    return PreprocessingResult(
        extracted_text=text,
        source_metadata={"original_filename": "notes.md", "file_size_bytes": len(text)},
        redaction_report=RedactionReport(),
        warnings=[],
        is_rejected=False,
        rejection_reason=None,
        token_count=len(text.split()),
    )


def _runbook_titled(raw_title: str) -> str:
    """A valid runbook whose frontmatter title is ``raw_title`` (quoted)."""
    return valid_runbook("Placeholder Title For Replacement").replace(
        "title: Placeholder Title For Replacement\n", f'title: "{raw_title}"\n'
    )


def _squash(text: str) -> str:
    """Lower-case alphanumerics only: how a slug spells a title."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _data_dir(tmp_path: pathlib.Path):
    return patch.object(
        ConversionService,
        "_data_dir",
        new_callable=lambda: property(lambda self: tmp_path),
    )


class _CountingSanitizer:
    """Delegates to a real sanitizer and counts the runs."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    async def asanitize(self, text):
        self.calls += 1
        return await self.inner.asanitize(text)


async def _assert_every_stored_title_fits(session_factory) -> None:
    """What PostgreSQL would enforce and SQLite does not: no stored title or
    file name is past its column."""
    async with session_factory() as session:
        for column in (
            KnowledgeItemModel.title,
            ConversionDraftModel.title,
            UploadedFileModel.filename,
        ):
            for value in (await session.execute(select(column))).scalars():
                assert len(value) <= column.type.length, (column, len(value))


class TestEveryTextDerivedFromATitleIsScrubbed:
    """No text derived from a runbook title reaches a reader, a log line or the
    case transcript unscrubbed, on any convert path (#1981). The title is
    scrubbed ONCE where it enters as raw text (the model's failure-mode title,
    the case's title, the title the model writes into a case draft, a typed
    title; a scanned file's title enters at the persistence choke, which derives nothing
    from it first), assigned back to the object, and everything
    else (the draft's id slug, the draft, the stored analysis, the response, the
    prompt, the log line, the transcript notice) is derived from the scrubbed
    value. Scrubbing a finished slug does nothing: ``password-hunter2`` has no
    ``=`` left for a pattern to match.

    Each test asserts the returned objects and the persisted rows are one text.

    Mutation: drop the scrub in ``_scrub_failure_mode_titles`` (document), in
    ``_convert_from_case_impl`` (the case's title), in ``_scrub_content_title``
    (the model's); none of the later layers hide it. For the disk scan the
    backstop IS the entry: drop the assign-back or the scrub in
    ``_persist_job_rows`` and ``test_a_scanned_files_title`` fails.
    """

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_a_document_conversion_whose_model_titled_a_failure_mode_with_a_secret(
        self, session_factory, tmp_path, label, secret, title
    ):
        router = AsyncMock()
        router.route.side_effect = [
            _model_response(_analysis_json(title)),
            _model_response(valid_runbook("Postgres Login Failure Recovery")),
        ]
        conversion = ConversionService(
            llm_router=router,
            settings=_settings(),
            db_session_factory=session_factory,
            sanitizer=regex_only_sanitizer(),
        )
        source = tmp_path / "notes.md"
        source.write_text("# Notes\n\nThe replica login fails after rotation.\n")

        with (
            patch.object(
                conversion._preprocessor,
                "preprocess",
                return_value=_preprocessed(source.read_text()),
            ),
            _data_dir(tmp_path),
        ):
            result = await conversion.convert_document(
                file_path=source,
                content_type="text/markdown",
                original_filename="notes.md",
                scope="global",
                user_id="admin1",
                enterprise_id=None,
            )

        (draft,) = result.drafts
        # Nothing the caller is handed names the secret: draft, id, the analysis
        # it carries, the warnings.
        assert secret not in result.model_dump_json()
        assert _squash(secret) not in _squash(draft.runbook_id)
        _assert_scrubbed(draft.title, secret)
        # The model is shown the scrubbed title too: no secret leaves in a prompt.
        assert secret not in str(router.route.await_args_list)
        # Listed to ANOTHER user (a global draft is): the rows are the objects.
        (listed,) = await conversion.list_all_drafts("some-other-user")
        assert (listed["title"], listed["runbook_id"]) == (
            draft.title,
            draft.runbook_id,
        )
        async with session_factory() as session:
            stored = (
                await session.execute(select(ConversionJobModel.analysis_result))
            ).scalar_one()
        assert secret not in json.dumps(stored)

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_a_case_conversion_whose_case_title_and_model_title_carry_a_secret(
        self, session_factory, tmp_path, label, secret, title
    ):
        router = AsyncMock()
        router.route.return_value = _model_response(_runbook_titled(title))
        conversion = ConversionService(
            llm_router=router,
            settings=_settings(),
            db_session_factory=session_factory,
            sanitizer=regex_only_sanitizer(),
        )
        request = CaseConversionRequest(
            case_id=CASE_ID,
            title=title,
            description="The replica login fails after rotation",
            root_cause="The credentials were rotated on the primary only",
            scope="global",
        )

        with _data_dir(tmp_path):
            result = await conversion.convert_from_case(
                request, user_id="admin1", enterprise_id=STANDALONE_ENTERPRISE_ID
            )

        (draft,) = result.drafts
        assert secret not in result.model_dump_json()
        assert _squash(secret) not in _squash(draft.runbook_id)
        _assert_scrubbed(draft.title, secret)
        # The text the draft carries names the scrubbed title, not the model's.
        assert secret not in draft.content
        # Neither the case's title nor the model's reached the prompt unscrubbed.
        assert secret not in str(router.route.await_args_list)
        # Listed to the case's readers: the same text the caller was handed.
        (listed,) = await conversion.list_drafts_for_case(CASE_ID)
        assert (listed["title"], listed["runbook_id"]) == (
            draft.title,
            draft.runbook_id,
        )
        async with session_factory() as session:
            stored = (
                await session.execute(select(ConversionJobModel.analysis_result))
            ).scalar_one()
        assert secret not in json.dumps(stored)

    async def test_a_model_title_that_cannot_be_rewritten_refuses_the_draft(
        self, session_factory, tmp_path
    ):
        """Never keep a title it could not clean: the draft is refused (the
        mode's own error), not stored."""
        router = AsyncMock()
        router.route.return_value = _model_response(
            _runbook_titled("Login fails (password=hunter2)")
        )
        conversion = ConversionService(
            llm_router=router,
            settings=_settings(),
            db_session_factory=session_factory,
            sanitizer=regex_only_sanitizer(),
        )
        request = CaseConversionRequest(
            case_id=CASE_ID,
            title="Replica login",
            description="The replica login fails",
            root_cause="Credentials rotated on the primary only",
            scope="global",
        )

        with (
            _data_dir(tmp_path),
            patch.object(
                ConversionService, "_scrub_content_title", AsyncMock(return_value=None)
            ),
        ):
            result = await conversion.convert_from_case(
                request, user_id="admin1", enterprise_id=STANDALONE_ENTERPRISE_ID
            )

        assert result.status == ConversionStatus.FAILED and result.drafts == []
        assert await _draft_titles(session_factory) == []

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES[:1] + ABUTTING)
    async def test_the_case_transcript_notice_and_the_log_name_no_secret(
        self, session_factory, tmp_path, caplog, label, secret, title
    ):
        """``runbook_creation`` logs the draft's title at INFO and writes it into
        the case transcript for every reader of the case. It reads the draft the
        service returned, so the title it holds must already be clean."""
        from faultmaven.core.investigation.milestone_engine.engine import (
            MilestoneEngine,
        )
        from tests.unit.core.investigation.test_runbook_completion_and_summary_failure import (  # noqa: E501
            _committed_gate,
            _make_resolved_case,
        )

        case = _make_resolved_case()
        router = AsyncMock()
        router.route.return_value = _model_response(_runbook_titled(title))
        conversion = ConversionService(
            llm_router=router,
            settings=_settings(),
            db_session_factory=session_factory,
            sanitizer=regex_only_sanitizer(),
        )
        request = CaseConversionRequest(
            case_id=case.case_id,
            title=title,
            description="The replica login fails after rotation",
            root_cause="The credentials were rotated on the primary only",
            scope="global",
        )
        repo = MagicMock()
        repo.get = AsyncMock(return_value=case)
        repo.save = AsyncMock()
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())

        with _data_dir(tmp_path), caplog.at_level(logging.DEBUG):
            await engine.runbooks._run_runbook_conversion(
                conversion,
                request,
                "u1",
                STANDALONE_ENTERPRISE_ID,
                committed=_committed_gate(),
            )

        notice = case.messages[-1]["content"]
        assert "is ready" in notice, "control: the success notice was written"
        assert secret not in notice
        assert secret not in caplog.text and secret not in str(
            [r.__dict__ for r in caplog.records]
        )

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_a_scanned_files_title(
        self, session_factory, tmp_path, label, secret, title
    ):
        directory = tmp_path / "global" / "database"
        directory.mkdir(parents=True)
        (directory / "leaky.md").write_text(_runbook_titled(title))
        conversion = _conversion(session_factory)

        with _data_dir(tmp_path):
            result = await conversion.scan_for_runbooks(
                user_id=None, enterprise_id=None, is_platform_admin=True
            )

        assert result["discovered"] == 1, result
        assert secret not in json.dumps(result)
        (listed,) = await conversion.list_all_drafts("some-other-user")
        _assert_scrubbed(listed["title"], secret)
        assert result["drafts"][0]["title"] == listed["title"]

    async def test_the_title_returned_and_the_title_stored_are_one_text(
        self, session_factory
    ):
        """``_persist_job_rows`` assigns what it stores back to the draft it was
        given, so the object a caller returns is never the raw one."""
        draft = ConversionDraft(
            draft_id="draft_assign_back",
            runbook_id="rb-assign-back",
            title="Login fails (password=hunter2) " + "x" * 300,
            scope="personal",
            status=DraftStatus.DRAFT,
            source_type=SourceType.CASE,
            validation=ValidationResult(passed=True),
            quality_score=QualityScore(
                overall=80.0,
                grade="B",
                completeness=80.0,
                clarity=80.0,
                actionability=80.0,
                comprehensiveness=80.0,
            ),
            content_preview="p",
            content="---\nid: rb-assign-back\n---\n\n# R\n",
        )

        await _persist_job(
            session_factory,
            None,
            conversion_id="conv_assign_back",
            user_id="u1",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            team_id=None,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(filename="f", size_bytes=1, content_type="x"),
            analysis=AnalysisResult(
                is_actionable=True,
                failure_modes=[],
                source_assessment=SourceAssessment(
                    content_type="x",
                    actionability_rating="high",
                    missing_information=[],
                ),
            ),
            drafts=[draft],
            created_at=datetime.now(timezone.utc),
            sanitizer=regex_only_sanitizer(),
        )

        (stored,) = await _draft_titles(session_factory)
        assert draft.title == stored
        _assert_scrubbed(stored, "hunter2")
        assert len(stored) == 255


def _modes_json(modes: list[tuple[str, str, str, list[str]]]) -> str:
    """An analysis of several failure modes: ``(id, title, service, symptoms)``.

    Distinct ``(service, symptom_class)`` so none is collapsed into another by
    the partition, and distinct titles so none mints another's id.
    """
    return json.dumps(
        {
            "is_actionable": True,
            "failure_modes": [
                {
                    "id": mode_id,
                    "title": title,
                    "domain": "database",
                    "service": service,
                    "symptom_class": symptoms,
                    "severity": "high",
                    "symptoms_summary": "Failing",
                    "resolution_summary": "Fix it",
                }
                for mode_id, title, service, symptoms in modes
            ],
            "source_assessment": {
                "content_type": "troubleshooting_guide",
                "actionability_rating": "high",
                "missing_information": [],
            },
        }
    )


class _StagedRouter:
    """Answers by stage (temperature): the analysis once, a runbook per mode."""

    def __init__(self, analysis: str):
        self.analysis = analysis
        self.calls: list[dict] = []

    async def route(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("temperature") == 0.2:
            return _model_response(self.analysis)
        return _model_response(valid_runbook("Generated Runbook For The Mode"))


class TestEveryFailureModeIsScrubbedNotJustTheFirst:
    """A document is analysed into SEVERAL failure modes and every one of them is
    named, slugged and listed. The scrub covers all of them.

    Mutation: ``for failure_mode in failure_modes[:1]:`` in
    ``_scrub_failure_mode_titles``. Every surface of mode 2 and 3 then carries
    its secret: the draft's ``runbook_id``, ``conversion_drafts``,
    ``conversion_jobs.analysis_result`` and ``GET /knowledge/drafts``.
    """

    #: Mode 1 is clean on purpose: a scrub that handled only the first mode, or
    #: only a mode that needed it, would still pass on it.
    MODES = [
        ("m1", "Replica lag after failover", "postgresql", ["replication_lag"]),
        ("m2", "Login fails (password=SECOND9)", "redis", ["connection_refused"]),
        (
            "m3",
            "Pool exhausted at postgresql://app:Winter2024x@db.internal:5432/orders",
            "kafka",
            ["timeout"],
        ),
        ("m4", "Calls 401 with sk-" + KEY_BLOCK * 4, "nginx", ["latency"]),
        ("m5", "Login fails (10.0.0.5password=SECOND9)", "etcd", ["disk_full"]),
    ]
    SECRETS = ["SECOND9", "Winter2024x", KEY_BLOCK]

    @pytest.mark.parametrize("count", [3, 5], ids=["three-modes", "five-modes"])
    async def test_every_surface_of_every_mode_is_clean(
        self, session_factory, tmp_path, count
    ):
        router = _StagedRouter(_modes_json(self.MODES[:count]))
        conversion = ConversionService(
            llm_router=router,
            settings=_settings(),
            db_session_factory=session_factory,
            sanitizer=regex_only_sanitizer(),
        )
        source = tmp_path / "notes.md"
        source.write_text("# Notes\n\nSeveral things fail.\n")

        with (
            patch.object(
                conversion._preprocessor,
                "preprocess",
                return_value=_preprocessed(source.read_text()),
            ),
            _data_dir(tmp_path),
        ):
            result = await conversion.convert_document(
                file_path=source,
                content_type="text/markdown",
                original_filename="notes.md",
                scope="global",
                user_id="admin1",
                enterprise_id=None,
            )

        assert len(result.drafts) == count, "control: every mode became a draft"
        surfaces = {
            "response": result.model_dump_json(),
            "prompts": str(router.calls),
            "listing": json.dumps(await conversion.list_all_drafts("another-user")),
        }
        async with session_factory() as session:
            surfaces["draft rows"] = json.dumps(
                [
                    list(row)
                    for row in (
                        await session.execute(
                            select(
                                ConversionDraftModel.title,
                                ConversionDraftModel.runbook_id,
                            )
                        )
                    ).all()
                ]
            )
            surfaces["job analysis"] = json.dumps(
                (
                    await session.execute(select(ConversionJobModel.analysis_result))
                ).scalar_one()
            )
        for surface, text in surfaces.items():
            for secret in self.SECRETS:
                assert secret.lower() not in text.lower(), (surface, secret)
        # The id is a slug: `password-second9` has no `=` for a pattern to find.
        for draft in result.drafts:
            for secret in self.SECRETS:
                assert _squash(secret) not in _squash(
                    draft.runbook_id
                ), draft.runbook_id


class TestAMalformedAnalysisLogsNoTitle:
    """A pydantic ``ValidationError`` quotes the failing failure-mode dict
    (``input_value={'title': 'db password=hu...``), so logging its text puts
    about 14 characters of a raw model title into the log. The parse failure
    logs the exception's CLASS only, as ``_convert_single_failure_mode`` does
    (#836).

    Mutation: ``logger.error(f"Failed to parse analysis response: {e}")``.
    """

    #: `title` is the FIRST key and the secret near its start: pydantic quotes only
    #: the head and tail of the dict, so this is what its error text shows.
    TITLE = "db password=hunter2 login fails on replica"

    @staticmethod
    def _malformed() -> str:
        """A failure mode with a title and NO ``severity``: required, so the
        model's own dict is quoted in the error."""
        return json.dumps(
            {
                "is_actionable": True,
                "failure_modes": [
                    {
                        "title": TestAMalformedAnalysisLogsNoTitle.TITLE,
                        "id": "pg-login",
                        "domain": "database",
                        "service": "postgresql",
                        "symptom_class": ["connection_refused"],
                        "symptoms_summary": "Login fails",
                        "resolution_summary": "Rotate",
                    }
                ],
                "source_assessment": {
                    "content_type": "troubleshooting_guide",
                    "actionability_rating": "high",
                    "missing_information": [],
                },
            }
        )

    async def test_no_record_carries_any_of_the_title(self, tmp_path, caplog):
        from faultmaven.modules.knowledge.domain.services.conversion_service.errors import (
            ConversionRejectedError,
        )

        router = _StagedRouter(self._malformed())
        conversion = ConversionService(
            llm_router=router,
            settings=_settings(),
            db_session_factory=None,
            sanitizer=regex_only_sanitizer(),
        )
        source = tmp_path / "notes.md"
        source.write_text("# Notes\n\nThe replica login fails.\n")

        with (
            patch.object(
                conversion._preprocessor,
                "preprocess",
                return_value=_preprocessed(source.read_text()),
            ),
            _data_dir(tmp_path),
            caplog.at_level(logging.DEBUG),
        ):
            with pytest.raises(ConversionRejectedError) as raised:
                await conversion.convert_document(
                    file_path=source,
                    content_type="text/markdown",
                    original_filename="notes.md",
                    scope="global",
                    user_id="admin1",
                    enterprise_id=None,
                )

        # Control: the failure is the validation error this test means, and it
        # IS logged (so an empty log cannot pass), by class.
        assert type(raised.value.__cause__).__name__ == "ValidationError"
        assert "password=hu" in str(raised.value.__cause__), "control: it quotes"
        logged = [r.getMessage() for r in caplog.records]
        assert any(
            "Failed to parse analysis response (ValidationError)" in m for m in logged
        )
        everything = " ".join(logged) + " " + str([r.__dict__ for r in caplog.records])
        for fragment in ("hunter2", "password=hu", "db password"):
            assert fragment not in everything, fragment
        assert str(raised.value) == "LLM analysis response could not be parsed"


class TestTheColumnCutComesAfterTheScrub:
    """A secret that straddles a column's length is cut into a FRAGMENT no
    pattern matches (the first 21 characters of a 32-character key), so cutting
    before scrubbing publishes it. And a title the item's column cannot hold must
    not fail AFTER the draft, job and upload rows are committed, stranding a
    verified draft with no item listed to everyone at global scope.

    Mutation: cut before the scrub inside ``sanitize_item_title``; drop the
    item-column cut from ``ingest_runbook`` or from ``upload_document``.
    """

    #: 21 key characters survive a cut-first at the column's length.
    KEY = "sk-" + KEY_BLOCK * 4

    def _straddling(self, length: int) -> str:
        # The key after a space: a letter glued before `sk-` is a task id's
        # shape (`task-0123…`), not a key, since #1980.
        return "x" * (length - 3 - 21 - 1) + " " + self.KEY + " tail"

    @pytest.mark.parametrize("length", [255, 512], ids=["String(255)", "String(512)"])
    async def test_the_helper_scrubs_before_it_cuts(self, length):
        column = SimpleNamespace(type=SimpleNamespace(length=length))

        result = await sanitize_item_title(
            regex_only_sanitizer(), self._straddling(length), column
        )

        assert len(result) <= length and KEY_BLOCK not in result

    async def test_the_persistence_choke_scrubs_before_it_cuts(self, session_factory):
        draft = ConversionDraft(
            draft_id="draft_straddle",
            runbook_id="rb-straddle",
            title=self._straddling(255),
            scope="personal",
            status=DraftStatus.DRAFT,
            source_type=SourceType.CASE,
            validation=ValidationResult(passed=True),
            quality_score=QualityScore(
                overall=80.0,
                grade="B",
                completeness=80.0,
                clarity=80.0,
                actionability=80.0,
                comprehensiveness=80.0,
            ),
            content_preview="p",
            content="---\nid: rb-straddle\n---\n\n# R\n",
        )

        await _persist_job(
            session_factory,
            None,
            conversion_id="conv_straddle",
            user_id="u1",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            team_id=None,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(filename="f", size_bytes=1, content_type="x"),
            analysis=AnalysisResult(
                is_actionable=True,
                failure_modes=[],
                source_assessment=SourceAssessment(
                    content_type="x",
                    actionability_rating="high",
                    missing_information=[],
                ),
            ),
            drafts=[draft],
            created_at=datetime.now(timezone.utc),
            sanitizer=regex_only_sanitizer(),
        )

        (stored,) = await _draft_titles(session_factory)
        assert KEY_BLOCK not in stored and len(stored) <= 255

    @pytest.mark.parametrize("length", [255, 512], ids=["String(255)", "String(512)"])
    async def test_an_upload_whose_secret_straddles_a_cut_stores_no_fragment(
        self, session_factory, length
    ):
        service = _knowledge(session_factory)

        await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title=self._straddling(length),
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )

        async with session_factory() as session:
            filenames = list(
                (await session.execute(select(UploadedFileModel.filename))).scalars()
            )
        stored = await _runbook_titles(session_factory) + await _draft_titles(
            session_factory
        )
        assert KEY_BLOCK not in " ".join(stored + filenames)
        await _assert_every_stored_title_fits(session_factory)

    async def test_a_title_past_the_items_column_strands_nothing(self, session_factory):
        service = _knowledge(session_factory)

        result = await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title="Pool exhausted " + "x" * 590,
            document_type="runbook",
            scope="global",
            owner_id="u1",
        )

        (item_title,) = await _runbook_titles(session_factory)
        assert len(item_title) == 512
        # The response names what was stored, not what was sent.
        assert result["metadata"]["title"] == item_title
        counts = await _row_counts(session_factory)
        assert counts["knowledge_items"] == counts["conversion_drafts"] == 1
        await _assert_every_stored_title_fits(session_factory)

    async def test_ingest_runbook_cuts_to_the_items_column(self, session_factory):
        service = _knowledge(session_factory)

        await service.ingest_runbook(**_ingest_args("personal", title="x" * 600))

        (stored,) = await _runbook_titles(session_factory)
        assert len(stored) == 512

    async def test_an_update_cuts_to_the_items_column(self, session_factory):
        service = _knowledge(session_factory)
        await service.ingest_runbook(**_ingest_args("personal"))

        await service.update_document_metadata("kb_0123456789ab", title="y" * 600)

        (stored,) = await _runbook_titles(session_factory)
        assert len(stored) == 512

    async def test_a_manual_runbooks_filename_fits_its_column(
        self, session_factory, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        conversion = _conversion(session_factory)

        await conversion.create_runbook_from_template(
            **_template(title="T" * 240 + " password=a password=b password=c")
        )

        await _assert_every_stored_title_fits(session_factory)


class TestTheDeepestChainSettlesWithSlack:
    """``password`` waits on the IP, which waits on the MAC. Main's patterns
    settled one layer per call: three changing calls and one more to see
    nothing change, four, against a bound of six. Since #1980 the regex half
    settles the chain inside one call: one changing call and one to see
    nothing change, two."""

    CHAIN = "Login fails (00:1A:2B:3C:4D:5510.0.0.5password=hunter2)"

    async def test_it_settles_in_two_calls_inside_the_bound(self):
        counting = _CountingSanitizer(regex_only_sanitizer())

        result = await sanitize_item_title(counting, self.CHAIN)

        assert "hunter2" not in result and counting.calls == 2
        assert counting.calls < MAX_SCRUB_PASSES

    async def test_the_chain_survives_the_real_paths_too(self, session_factory):
        service = _knowledge(session_factory)

        await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title=self.CHAIN,
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )

        (draft_title,) = await _draft_titles(session_factory)
        assert "hunter2" not in draft_title
        assert draft_title == (await _runbook_titles(session_factory))[0]


class _LayerPeelingSanitizer:
    """A sanitizer that removes one ``<`` per run, the way the real one removes
    one layer of an abutting secret per run: a title with two layers is stable
    only after the second run changes it."""

    def __init__(self):
        self.calls = 0

    async def asanitize(self, text):
        self.calls += 1
        return text.replace("<", "", 1)


class TestTheHelper:
    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_it_is_idempotent_on_its_own_output(self, label, secret, title):
        """The property the copies rest on: scrubbing the result again changes
        nothing — held by the helper's loop, whatever the sanitizer's own
        idempotence (``test_it_runs_the_sanitizer_to_a_fixed_point``)."""
        sanitizer = regex_only_sanitizer()

        once = await sanitize_item_title(sanitizer, title)

        assert secret not in once
        assert await sanitize_item_title(sanitizer, once) == once

    async def test_it_runs_the_sanitizer_to_a_fixed_point(self):
        sanitizer = _LayerPeelingSanitizer()

        result = await sanitize_item_title(sanitizer, "a<<b")

        # Two layers peeled, then one more run to see nothing changed.
        assert result == "ab" and sanitizer.calls == 3

    async def test_a_clean_title_costs_one_call(self):
        sanitizer = _LayerPeelingSanitizer()

        assert await sanitize_item_title(sanitizer, PLAIN_TITLE) == PLAIN_TITLE
        assert sanitizer.calls == 1

    async def test_a_sanitizer_that_never_settles_is_refused_not_looped(self):
        sanitizer = MagicMock()
        sanitizer.asanitize = AsyncMock(side_effect=lambda text: text + "x")

        with pytest.raises(ItemTitleScrubUnavailable, match="did not settle"):
            await sanitize_item_title(sanitizer, "a")

        assert sanitizer.asanitize.await_count == MAX_SCRUB_PASSES

    @pytest.mark.parametrize("answer", [None, b"bytes", ["a"], 7])
    async def test_a_sanitizer_that_answers_something_else_than_text_is_refused(
        self, answer
    ):
        """Fail closed, as the case title's gate does: a non-``str`` answer is not
        a scrubbed title, and ``None`` is what a stub returns."""
        sanitizer = MagicMock()
        sanitizer.asanitize = AsyncMock(return_value=answer)

        with pytest.raises(ItemTitleScrubUnavailable, match="not text"):
            await sanitize_item_title(sanitizer, "Redis OOM")

    async def test_the_cause_is_chained_and_the_text_is_not_in_the_message(self):
        error = RedactionUnavailableError("down")

        with pytest.raises(ItemTitleScrubUnavailable) as raised:
            await sanitize_item_title(
                _failing_sanitizer(error), "Login fails (password=hunter2)"
            )

        assert raised.value.__cause__ is error
        assert "hunter2" not in str(raised.value)

    def test_fit_to_column_cuts_after_the_scrub_to_the_columns_length(self):
        assert fit_to_column("x" * 300, UploadedFileModel.filename) == "x" * 255
        assert fit_to_column("short", UploadedFileModel.filename) == "short"


def _client(service, *, raises=None) -> TestClient:
    """The real knowledge router with the app's real exception handlers."""
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.state.team_service = None
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)

    async def _service():
        return service

    async def _user():
        return SimpleNamespace(
            user_id="user-1",
            organization_id="org-1",
            enterprise_id="ent-1",
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[get_knowledge_service] = _service
    app.dependency_overrides[require_authentication] = _user
    return TestClient(app, raise_server_exceptions=False)


class TestAnUnavailableScrubAnswers503:
    """The check could not run: a retryable server-side condition, not a 500, with
    a detail that says nothing about the title (``/title`` answers it the same
    way, #1993). Mutation: delete the ``except ItemTitleScrubUnavailable`` clause
    in either route (the blanket 500 swallows it), or the handler registration."""

    GENERIC = (
        "The sensitive-data check on titles is temporarily unavailable. "
        "Nothing was saved; retry later."
    )

    def test_an_upload_answers_503(self):
        service = SimpleNamespace(
            upload_document=AsyncMock(
                side_effect=ItemTitleScrubUnavailable("the check failed (secret-x)")
            )
        )

        response = _client(service).post(
            "/api/v1/knowledge/documents",
            data={
                "title": "Login fails (password=hunter2)",
                "document_type": "runbook",
                "scope": "personal",
            },
            files={
                "file": ("rb.md", b"---\nid: x\n---\n\n# A runbook\n", "text/markdown")
            },
        )

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == self.GENERIC
        assert "hunter2" not in response.text and "secret-x" not in response.text

    def test_an_update_answers_503(self, monkeypatch):
        document = {"document_id": "kb_x", "owner_id": "user-1", "scope": "personal"}
        service = SimpleNamespace(
            get_document=AsyncMock(return_value=document),
            update_document_metadata=AsyncMock(
                side_effect=ItemTitleScrubUnavailable("the check failed (secret-x)")
            ),
        )

        monkeypatch.setattr(routes, "_authorize_document_write", AsyncMock())
        response = _client(service).put(
            "/api/v1/knowledge/documents/kb_x",
            json={"title": "Login fails (password=hunter2)"},
        )

        assert response.status_code == 503, response.text
        assert response.json()["detail"] == self.GENERIC
        assert "hunter2" not in response.text and "secret-x" not in response.text

    def test_a_service_failure_that_is_not_the_scrub_is_still_a_500(self):
        service = SimpleNamespace(
            upload_document=AsyncMock(side_effect=RuntimeError("db down"))
        )

        response = _client(service).post(
            "/api/v1/knowledge/documents",
            data={"title": "T", "document_type": "runbook", "scope": "personal"},
            files={
                "file": ("rb.md", b"---\nid: x\n---\n\n# A runbook\n", "text/markdown")
            },
        )

        assert response.status_code == 500
