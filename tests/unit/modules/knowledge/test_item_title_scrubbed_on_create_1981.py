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
``KnowledgeIngester`` (dead)          yes        ``TestDeadIngesterPaths``
====================================  =========  ==============================
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import FaultMavenSettings, env_var_spellings
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
    ConversionDraft,
    ConversionStatus,
    DraftStatus,
    FailureModeAnalysis,
    QualityScore,
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
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.exceptions import ItemTitleScrubUnavailable
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

#: ``(label, the secret, a title carrying it)``. The first four are the
#: sanitizer's secret set as a title can hold it (an error message pasted into
#: one). The last two ABUT: the sanitizer's ``password`` pattern needs a word
#: boundary, which a digit or hex digit before the ``p`` removes until the IP or
#: MAC pattern (later in its list) has replaced the address, so ONE pass leaves
#: ``password=hunter2`` in place and a second takes it out. A title scrubbed once
#: and the same title scrubbed twice then differ.
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
        knowledge_ingester=MagicMock(),
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
        # ONE text everywhere, and the same text a one-off scrub gives: the
        # sanitizer is not idempotent (see SECRET_TITLES), so five copies made
        # by a different number of passes would not agree.
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


class TestDeadIngesterPaths:
    """``KnowledgeIngester.ingest_document`` / ``ingest_document_object`` have no
    caller today (``write_scope`` pins them dead) but write a title into every
    chunk's metadata, and a revived one must not be the path that publishes it
    in clear. Built without ``__init__`` (which opens ChromaDB); the chunk
    writer is the double, and what it is handed is what is asserted.

    They also LOG what they ingest: by document id, never the raw title.

    Mutation: delete the call in either method; log ``title`` again.
    """

    @staticmethod
    def _ingester() -> KnowledgeIngester:
        ingester = KnowledgeIngester.__new__(KnowledgeIngester)
        ingester.logger = MagicMock()
        ingester.sanitizer = regex_only_sanitizer()
        ingester._process_and_store = AsyncMock()
        ingester._extract_text = AsyncMock(return_value="# Body\n\ntext")
        return ingester

    @staticmethod
    def _logged(ingester: KnowledgeIngester) -> str:
        return " ".join(
            str(call.args[0])
            for method in ("debug", "info", "warning", "error")
            for call in getattr(ingester.logger, method).call_args_list
        )

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_ingest_document(self, label, secret, title):
        ingester = self._ingester()

        await ingester.ingest_document(
            "/dev/null", title, scope="personal", owner_id="u1"
        )

        document = ingester._process_and_store.await_args.args[0]
        _assert_scrubbed(document.title, secret)
        assert secret not in self._logged(ingester)

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_ingest_document_object(self, label, secret, title):
        ingester = self._ingester()
        document = KnowledgeBaseDocument(
            document_id="d1",
            title=title,
            content="# Body",
            document_type="runbook",
            tags=[],
            scope="personal",
            owner_id="u1",
            created_at="2026-10-14T00:00:00+00:00",
            updated_at="2026-10-14T00:00:00+00:00",
        )

        await ingester.ingest_document_object(document)

        _assert_scrubbed(ingester._process_and_store.await_args.args[0].title, secret)
        assert secret not in self._logged(ingester)

    async def test_a_failed_ingest_does_not_log_the_title(self):
        ingester = self._ingester()
        ingester._process_and_store = AsyncMock(side_effect=RuntimeError("store down"))

        with pytest.raises(RuntimeError):
            await ingester.ingest_document(
                "/dev/null",
                "Login fails (password=hunter2)",
                scope="personal",
                owner_id="u1",
            )

        assert "hunter2" not in self._logged(ingester)

    async def test_an_ordinary_title_is_unchanged(self):
        ingester = self._ingester()

        await ingester.ingest_document(
            "/dev/null", PLAIN_TITLE, scope="personal", owner_id="u1"
        )

        assert ingester._process_and_store.await_args.args[0].title == PLAIN_TITLE


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
        nothing. The sanitizer alone does not have it (see ``SECRET_TITLES``)."""
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


# ---------------------------------------------------------------------------
# One creator of a knowledge item, and one place that says `shipped=True`
# ---------------------------------------------------------------------------

#: What constructs a knowledge item row. ``KnowledgeItem`` is the domain object,
#: ``KnowledgeItemModel`` the ORM row; either one in a new place is a create
#: path these tests do not cover.
ITEM_NAMES = {"KnowledgeItem", "KnowledgeItemModel"}

#: ``insert into knowledge_items`` in a string: raw SQL is a create path too.
RAW_INSERT_RE = re.compile(r"(?is)\binsert\b[^;]{0,60}?\binto\s+\"?knowledge_items\b")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: Scanned: the package, and the two trees outside it that run against the same
#: database (a script or a migration can write a row as surely as a service).
SCANNED_ROOTS = ("faultmaven", "scripts", "alembic")


def _parse(path: pathlib.Path) -> ast.AST | None:
    with warnings.catch_warnings():
        # A file elsewhere in the tree has an invalid escape in a string;
        # parsing it is all this does.
        warnings.simplefilter("ignore")
        try:
            return ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            return None


def item_creators(tree: ast.AST) -> list[tuple[str, str]]:
    """``(spelling, enclosing function)`` for every way a module can write a
    knowledge item row that this guard knows.

    Catches: a bare call; a call through an alias import (``import X as Y``) or
    an attribute (``models.KnowledgeItemModel(...)``); ``getattr(module, "X")(...)``;
    ``insert(KnowledgeItemModel)``, ``KnowledgeItemModel.__table__.insert()``; a
    raw ``INSERT INTO knowledge_items`` string; and any of those at module level.

    RESIDUAL, not caught: ``instance.__class__(...)``, ``dataclasses.replace(item,
    ...)`` (both build a new item from one that already exists, which a creator
    must first have made) and text assembled at run time into an INSERT. A
    reviewer is the guard for those.
    """
    aliases = set(ITEM_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] in ITEM_NAMES and alias.asname:
                    aliases.add(alias.asname)

    def names_an_item(node: ast.AST) -> bool:
        return any(
            (isinstance(x, ast.Name) and x.id in aliases)
            or (isinstance(x, ast.Attribute) and x.attr in ITEM_NAMES)
            or (isinstance(x, ast.Constant) and x.value in ITEM_NAMES)
            for x in ast.walk(node)
        )

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing(node: ast.AST) -> str:
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id in aliases) or (
                isinstance(func, ast.Attribute) and func.attr in ITEM_NAMES
            ):
                found.append(("call", enclosing(node)))
            elif (
                isinstance(func, ast.Call)
                and isinstance(func.func, ast.Name)
                and func.func.id == "getattr"
                and names_an_item(func)
            ):
                found.append(("getattr-call", enclosing(node)))
            elif (isinstance(func, ast.Name) and func.id == "insert") or (
                isinstance(func, ast.Attribute) and func.attr == "insert"
            ):
                if any(names_an_item(arg) for arg in node.args) or (
                    isinstance(func, ast.Attribute) and names_an_item(func.value)
                ):
                    found.append(("insert()", enclosing(node)))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "__table__"
            and names_an_item(node.value)
        ):
            found.append(("__table__", enclosing(node)))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and RAW_INSERT_RE.search(node.value)
        ):
            found.append(("raw-sql", enclosing(node)))
    return found


def _scan(roots=SCANNED_ROOTS) -> set[tuple[str, str, str]]:
    found = set()
    for root in roots:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            tree = _parse(path)
            if tree is None:
                continue
            for spelling, function in item_creators(tree):
                found.add((path.relative_to(REPO_ROOT).as_posix(), function, spelling))
    return found


def _creators(source: str) -> list[tuple[str, str]]:
    return item_creators(ast.parse(source))


class TestOneCreatorOfAKnowledgeItem:
    """The premise every test above rests on: ``ingest_runbook`` is the only
    place a ``knowledge_items`` row is created. A second creator would be a
    create path these tests do not cover, so it fails here until its author
    decides what its title does (and adds it to the table at the top).

    An AST walk, not a text search: it finds the CALL, in whatever function and
    under whatever spelling, and is not fooled by a comment or a docstring that
    names the class.
    """

    #: ``(file, function, spelling)``: the sites that may write the rows.
    ALLOWED = {
        # The one creator.
        (
            "faultmaven/modules/knowledge/domain/services/knowledge_service.py",
            "ingest_runbook",
            "call",
        ),
        # Read side: builds the domain object from a row it just selected.
        (
            "faultmaven/modules/knowledge/infrastructure/persistence/knowledge_item_repository.py",
            "_to_domain",
            "call",
        ),
        # The writer of the ``knowledge_items`` row ``ingest_runbook`` hands it.
        (
            "faultmaven/modules/knowledge/infrastructure/persistence/knowledge_item_repository.py",
            "create",
            "call",
        ),
        # Names the table, writes nothing: the ``has_table_privilege(..., 'UPDATE')``
        # probe the maintenance role check (#1966) reads the table's full name for.
        (
            "faultmaven/modules/knowledge/infrastructure/persistence/knowledge_item_repository.py",
            "may_update",
            "__table__",
        ),
    }

    def test_no_other_function_constructs_one(self):
        found = _scan()
        assert found, "the walk found nothing: this guard is vacuous"
        assert found == self.ALLOWED, (
            f"a new writer of a knowledge item: {sorted(found - self.ALLOWED)}; "
            f"a vanished one: {sorted(self.ALLOWED - found)}"
        )

    @pytest.mark.parametrize(
        "source",
        [
            "KnowledgeItem(item_id='x')",
            "from m import KnowledgeItemModel as Row\nRow(item_id='x')",
            "import m\ndef f():\n    return m.KnowledgeItemModel(item_id='x')",
            "def f():\n    return getattr(m, 'KnowledgeItemModel')(item_id='x')",
            "def f(s):\n    s.execute(insert(KnowledgeItemModel).values(a=1))",
            "def f(s):\n    s.execute(KnowledgeItemModel.__table__.insert())",
            "def f(s):\n    s.execute(text('INSERT INTO knowledge_items (a) VALUES (1)'))",
            "def f(op):\n    op.execute('insert into \"knowledge_items\" values (1)')",
        ],
        ids=[
            "module-level",
            "alias",
            "attribute",
            "getattr",
            "insert()",
            "__table__",
            "raw-sql",
            "raw-sql-quoted",
        ],
    )
    def test_the_guard_bites_each_spelling_it_claims(self, source):
        assert _creators(source), source

    @pytest.mark.parametrize(
        "source",
        [
            "def f(s):\n    return s.execute(select(KnowledgeItemModel))",
            "def f(x):\n    return isinstance(x, KnowledgeItem)",
            "def f(s):\n    s.execute(text('SELECT * FROM knowledge_items'))",
            "def f(s):\n    s.execute(insert(OtherModel))",
        ],
        ids=["select", "isinstance", "raw-select", "insert-other"],
    )
    def test_the_guard_does_not_flag_a_read(self, source):
        assert _creators(source) == [], source

    def test_the_residual_is_declared_not_hidden(self):
        """``__class__`` and ``replace`` are not caught, and the docstring says so."""
        assert _creators("def f(i):\n    return i.__class__(item_id='x')") == []
        assert "RESIDUAL" in (item_creators.__doc__ or "")


class TestOnlyKbInitIsShipped:
    """``shipped=True`` skips the title scrub, so it is stated in exactly one
    place: the pack's ingest. Any other call that passes it anything but the
    literal ``False`` is a user path that opted out. (``**{"shipped": ...}`` is
    the residual an AST keyword scan cannot see.)"""

    ALLOWED = {("faultmaven/bootstrap/kb_init.py", "_ingest_pack_runbook")}

    def test_nothing_else_passes_it(self):
        found = set()
        for root in SCANNED_ROOTS:
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                tree = _parse(path)
                if tree is None:
                    continue
                parents = {
                    c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)
                }
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and node.keywords):
                        continue
                    for keyword in node.keywords:
                        if keyword.arg != "shipped":
                            continue
                        if (
                            isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is False
                        ):
                            continue
                        scope, cursor = "<module>", node
                        while cursor in parents:
                            cursor = parents[cursor]
                            if isinstance(
                                cursor, (ast.FunctionDef, ast.AsyncFunctionDef)
                            ):
                                scope = cursor.name
                                break
                        found.add((path.relative_to(REPO_ROOT).as_posix(), scope))
        assert found == self.ALLOWED, sorted(found ^ self.ALLOWED)
