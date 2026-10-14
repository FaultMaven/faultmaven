"""A knowledge item's title is scrubbed on create exactly as on update (#1981).

A title is shown to every reader of the tier its item lives in (team and global
included), in every list, and nothing ever reverses it. ``update_document``
redacted it; every create path stored it verbatim, so a title typed into an
upload form as ``Fix login (password=hunter2)`` was published as written, while
the same string typed into an edit was redacted.

These tests drive the REAL service over in-memory SQLite with a REAL
``DataSanitizer`` (regex half: ``regex_only_sanitizer``, no network, pinned
pseudonym key, so the result is the same under the Standalone and Cloud CI
profiles). Only the ChromaDB write is a double. Each create path is asserted on
the STORED row, and each names the mutation that must fail it.

Every path that writes a title:

====================================  =========  ==============================
writer                                scrubbed   test
====================================  =========  ==============================
``ingest_runbook`` (the one place a   yes        ``TestIngestRunbook``
``knowledge_items`` row is created)
``upload_document``                   yes        ``TestUploadDocument``
``verify_draft`` (publish)            yes, via   ``TestVerifyDraft``
                                      ingest
KB pack / ``kb_seed``                 NO, by     ``TestShippedPackTitle``
                                      decision
``update_document``                   yes        ``TestUpdateDocument``
``KnowledgeIngester`` (dead)          yes        ``TestDeadIngesterPaths``
====================================  =========  ==============================
"""

from __future__ import annotations

import ast
import pathlib
import re
import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    EnterpriseModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.infrastructure.security.redaction import RedactionUnavailableError
from faultmaven.models import KnowledgeBaseDocument
from faultmaven.modules.knowledge.domain.item_title import sanitize_item_title
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.ingestion import KnowledgeIngester
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

#: ``(label, the secret, a title carrying it)``. The first is the shape the
#: issue names; the others are the rest of the sanitizer's secret set that a
#: title can plausibly hold (an error message pasted into a title).
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
]
SECRET_IDS = [label for label, _, _ in SECRET_TITLES]

#: A title with nothing in it for the sanitizer to find.
PLAIN_TITLE = "Redis Out Of Memory Recovery"

#: What a redacted span becomes: ``<TYPE_<16 hex>>``.
PLACEHOLDER_RE = re.compile(r"<[A-Z_]+_[0-9a-f]{16}>")


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


def _service(session_factory, sanitizer=None) -> KnowledgeService:
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=sanitizer if sanitizer is not None else regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    service._index_document_in_vector_store = AsyncMock(return_value=5)
    return service


def _runbook_titles(session_factory):
    """The title column of every ``knowledge_items`` row."""

    async def _read() -> list[str]:
        async with session_factory() as session:
            return list(
                (await session.execute(select(KnowledgeItemModel.title))).scalars()
            )

    return _read()


def _assert_scrubbed(stored: str, secret: str) -> None:
    assert secret not in stored, f"the secret survived: {stored!r}"
    assert PLACEHOLDER_RE.search(stored), f"nothing was redacted: {stored!r}"


class TestIngestRunbook:
    """``ingest_runbook`` builds the only ``KnowledgeItem`` the service creates;
    uploads, published drafts and the pack all reach it.

    Mutation: delete the ``sanitize_item_title`` call at the top of
    ``ingest_runbook``. Every test here fails; the upload and verify tests
    below fail with it only on their ``knowledge_items`` assertions.
    """

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_the_stored_row_and_the_vectors_carry_the_scrubbed_title(
        self, session_factory, label, secret, title
    ):
        service = _service(session_factory)

        await service.ingest_runbook(
            document_id="kb_0123456789ab",
            title=title,
            content="# Body\n\nIncrease maxmemory.",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            owner_id="u1",
        )

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

    async def test_an_ordinary_title_is_stored_as_written(self, session_factory):
        service = _service(session_factory)

        await service.ingest_runbook(
            document_id="kb_0123456789ab",
            title=PLAIN_TITLE,
            content="# Body\n\nIncrease maxmemory.",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            owner_id="u1",
        )

        assert await _runbook_titles(session_factory) == [PLAIN_TITLE]

    async def test_a_title_that_cannot_be_scrubbed_is_not_written(
        self, session_factory
    ):
        """Fail closed, before any write: nothing to compensate."""
        sanitizer = MagicMock()
        sanitizer.asanitize = AsyncMock(side_effect=RedactionUnavailableError("down"))
        service = _service(session_factory, sanitizer)

        with pytest.raises(RedactionUnavailableError):
            await service.ingest_runbook(
                document_id="kb_0123456789ab",
                title="Login fails (password=hunter2)",
                content="# Body",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                scope="personal",
                owner_id="u1",
            )

        assert await _runbook_titles(session_factory) == []
        service._index_document_in_vector_store.assert_not_awaited()


class TestUploadDocument:
    """``upload_document`` writes the upload row and the draft row from the
    title BEFORE ``ingest_runbook`` runs, so it scrubs first.

    Mutation: delete the ``sanitize_item_title`` call in ``upload_document``.
    ``upload_document``'s own rows and response then carry the secret
    (``ingest_runbook`` still scrubs the ``knowledge_items`` row, which is why
    the assertions here read the other three).
    """

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_every_row_and_the_response_carry_one_scrubbed_title(
        self, session_factory, label, secret, title
    ):
        service = _service(session_factory)

        result = await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title=title,
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )

        (item_title,) = await _runbook_titles(session_factory)
        async with session_factory() as session:
            (draft_title,) = (
                await session.execute(select(ConversionDraftModel.title))
            ).scalars()
            (filename,) = (
                await session.execute(select(UploadedFileModel.filename))
            ).scalars()
        _assert_scrubbed(item_title, secret)
        # ONE text everywhere: upload_document scrubs, ingest_runbook scrubs
        # again, and the second pass must not change what the first wrote.
        assert draft_title == item_title
        assert filename == item_title
        assert result["metadata"]["title"] == item_title
        indexed = service._index_document_in_vector_store.await_args.args[0]
        assert indexed.title == item_title

    async def test_an_ordinary_title_is_stored_as_written(self, session_factory):
        service = _service(session_factory)

        result = await service.upload_document(
            content=valid_runbook("Redis Notes For The Cache Tier"),
            title=PLAIN_TITLE,
            document_type="runbook",
            scope="personal",
            owner_id="u1",
        )

        assert await _runbook_titles(session_factory) == [PLAIN_TITLE]
        assert result["metadata"]["title"] == PLAIN_TITLE

    async def test_a_title_that_cannot_be_scrubbed_leaves_no_row_behind(
        self, session_factory
    ):
        sanitizer = MagicMock()
        sanitizer.asanitize = AsyncMock(side_effect=RedactionUnavailableError("down"))
        service = _service(session_factory, sanitizer)

        with pytest.raises(RedactionUnavailableError):
            await service.upload_document(
                content=valid_runbook("Redis Notes For The Cache Tier"),
                title="Login fails (password=hunter2)",
                document_type="runbook",
                scope="personal",
                owner_id="u1",
            )

        async with session_factory() as session:
            for model in (KnowledgeItemModel, ConversionDraftModel, UploadedFileModel):
                assert (await session.execute(select(model))).first() is None, model


class TestVerifyDraft:
    """Publishing a draft (``verify_draft``) is the third creator of a
    ``knowledge_items`` row. The draft's title is the author's (or the model's)
    text; the published item's is what every reader sees.

    Mutation: delete the call in ``ingest_runbook``: the published row carries
    the secret. (The draft row, the author's private review queue, keeps what
    the author wrote: it is not disclosed to the tier. See the PR for why.)
    """

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_the_published_item_carries_the_scrubbed_title(
        self, session_factory, label, secret, title, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        knowledge = _service(session_factory)
        conversion = ConversionService(
            llm_router=MagicMock(),
            settings=MagicMock(),
            db_session_factory=session_factory,
            knowledge_service=knowledge,
        )
        created = await conversion.create_runbook_from_template(
            title=title,
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
        draft_id = created["draft"].draft_id
        async with session_factory() as session:
            (await session.get(ConversionDraftModel, draft_id)).validation_passed = True
            await session.commit()

        await conversion.verify_draft(created["conversion_id"], draft_id, "u1")

        (published,) = await _runbook_titles(session_factory)
        _assert_scrubbed(published, secret)
        indexed = knowledge._index_document_in_vector_store.await_args.args[0]
        assert indexed.title == published


class TestShippedPackTitle:
    """The KB pack and ``kb_seed`` are the one exemption, by decision.

    A pack title is vendored, reviewed source (``resources/knowledge/pack``,
    built and checked in CI), not user or model text. Sanitizing it would add
    a dependency the seed must not have: under ``TENANT_PROVIDER=multi`` the
    ``kb_seed`` job calls ``ingest_runbook`` for every shipped runbook, and a
    fail-closed Presidio outage would then stop the platform corpus from
    seeding. The same function serves both, so the exemption is an explicit
    keyword (``shipped=True``) that ``kb_init`` states; a caller that says
    nothing gets the scrub.

    Mutation: make ``ingest_runbook`` ignore ``shipped``: this fails. That
    ``kb_init`` states ``shipped=True`` for every pack runbook is pinned where
    its other ingest arguments are, in
    ``tests/unit/bootstrap/test_kb_init.py::test_bootstrap_ingests_new_runbook``.
    """

    async def test_a_shipped_title_is_stored_verbatim_and_never_touches_the_sanitizer(
        self, session_factory
    ):
        sanitizer = MagicMock()
        sanitizer.asanitize = AsyncMock(side_effect=RedactionUnavailableError("down"))
        service = _service(session_factory, sanitizer)
        # Not a title a pack would carry; the point is that it is not rewritten.
        shipped_title = "Rotate the password=<NEW_PASSWORD> on the replica"

        await service.ingest_runbook(
            document_id="kb_0123456789ab",
            title=shipped_title,
            content="# Body",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="global",
            shipped=True,
        )

        assert await _runbook_titles(session_factory) == [shipped_title]
        sanitizer.asanitize.assert_not_awaited()


class TestUpdateDocument:
    """The path that always scrubbed, now through the one helper. The same
    input must land on the same placeholder as it does on create: one rule, not
    two that happen to agree."""

    async def test_update_and_create_apply_the_same_rule(self, session_factory):
        service = _service(session_factory)
        title = "Login fails after rotation (password=hunter2)"
        await service.ingest_runbook(
            document_id="kb_0123456789ab",
            title=PLAIN_TITLE,
            content="# Body",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            owner_id="u1",
        )
        await service.ingest_runbook(
            document_id="kb_0123456789ac",
            title=title,
            content="# Body",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            owner_id="u1",
        )

        await service.update_document_metadata("kb_0123456789ab", title=title)

        async with session_factory() as session:
            rows = {
                row.item_id: row.title
                for row in (await session.execute(select(KnowledgeItemModel))).scalars()
            }
        _assert_scrubbed(rows["kb_0123456789ab"], "hunter2")
        assert rows["kb_0123456789ab"] == rows["kb_0123456789ac"]

    async def test_an_ordinary_title_edit_is_unchanged(self, session_factory):
        service = _service(session_factory)
        await service.ingest_runbook(
            document_id="kb_0123456789ab",
            title="Old",
            content="# Body",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="personal",
            owner_id="u1",
        )

        await service.update_document_metadata("kb_0123456789ab", title=PLAIN_TITLE)

        assert await _runbook_titles(session_factory) == [PLAIN_TITLE]


class TestDeadIngesterPaths:
    """``KnowledgeIngester.ingest_document`` / ``ingest_document_object`` have no
    caller today (``write_scope`` pins them dead) but write a title into every
    chunk's metadata, and a revived one must not be the path that publishes it
    in clear. Built without ``__init__`` (which opens ChromaDB); the chunk
    writer is the double, and what it is handed is what is asserted.

    Mutation: delete the call in either method.
    """

    @staticmethod
    def _ingester() -> KnowledgeIngester:
        ingester = KnowledgeIngester.__new__(KnowledgeIngester)
        ingester.logger = MagicMock()
        ingester.sanitizer = regex_only_sanitizer()
        ingester._process_and_store = AsyncMock()
        ingester._extract_text = AsyncMock(return_value="# Body\n\ntext")
        return ingester

    @pytest.mark.parametrize("label,secret,title", SECRET_TITLES, ids=SECRET_IDS)
    async def test_ingest_document(self, label, secret, title):
        ingester = self._ingester()

        await ingester.ingest_document(
            "/dev/null", title, scope="personal", owner_id="u1"
        )

        document = ingester._process_and_store.await_args.args[0]
        _assert_scrubbed(document.title, secret)

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

    async def test_an_ordinary_title_is_unchanged(self):
        ingester = self._ingester()

        await ingester.ingest_document(
            "/dev/null", PLAIN_TITLE, scope="personal", owner_id="u1"
        )

        assert ingester._process_and_store.await_args.args[0].title == PLAIN_TITLE


class TestTheHelper:
    async def test_it_is_idempotent_on_its_own_output(self):
        """``upload_document`` scrubs, then ``ingest_runbook`` scrubs the result."""
        sanitizer = regex_only_sanitizer()
        for _, secret, title in SECRET_TITLES:
            once = await sanitize_item_title(sanitizer, title)
            assert secret not in once
            assert await sanitize_item_title(sanitizer, once) == once


class TestOneCreatorOfAKnowledgeItem:
    """The premise every test above rests on: ``ingest_runbook`` is the only
    place a ``knowledge_items`` row is created. A second creator would be a
    create path these tests do not cover, so it fails here until its author
    decides what its title does (and adds it to the table above).

    An AST walk, not a text search: it finds the CALL, in whatever function, and
    is not fooled by a comment or a docstring that names the class.
    """

    #: ``(file, function)`` pairs allowed to construct each class.
    ALLOWED = {
        # The one creator.
        ("KnowledgeItem", "modules/knowledge/domain/services/knowledge_service.py", "ingest_runbook"),
        # Read side: builds the domain object from a row it just selected.
        ("KnowledgeItem", "modules/knowledge/infrastructure/persistence/knowledge_item_repository.py", "_to_domain"),
        # The writer of the ``knowledge_items`` row ``ingest_runbook`` hands it.
        ("KnowledgeItemModel", "modules/knowledge/infrastructure/persistence/knowledge_item_repository.py", "create"),
    }  # fmt: skip

    def test_no_other_function_constructs_one(self):
        root = pathlib.Path(__file__).resolve().parents[4] / "faultmaven"
        found: set[tuple[str, str, str]] = set()
        for path in root.rglob("*.py"):
            with warnings.catch_warnings():
                # A file elsewhere in the tree has an invalid escape in a
                # string; parsing it is all this does.
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(func):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in ("KnowledgeItem", "KnowledgeItemModel")
                    ):
                        found.add(
                            (node.func.id, path.relative_to(root).as_posix(), func.name)
                        )
        assert found, "the walk found nothing: this guard is vacuous"
        assert found == self.ALLOWED, (
            f"a new creator of a knowledge item: {sorted(found - self.ALLOWED)}; "
            f"a vanished one: {sorted(self.ALLOWED - found)}"
        )
