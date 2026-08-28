"""Containment on every conversion path that touches a runbook file.

#1215 sanitised ``ConversionService._scope_dir``, so the *directory* a new
draft is written into cannot escape. It left the write sites themselves
unguarded, and the review recorded that as a follow-up on #1213:

- ``update_draft`` and ``verify_draft`` re-open ``conversion_drafts.file_path``
  — a value read straight back out of the DATABASE — and rewrite it with no
  re-validation. A row persisted BEFORE #1215, when an escape was
  constructible, is re-opened and rewritten unchecked.
- ``delete_draft`` ``unlink``s that same database value.
- ``get_conversion`` reads it into the API response.
- The scan's reconciliation probe lets it decide whether a draft survives.

The mint points are all sanitised now, so a NEW escaping row is
unconstructible. That is exactly why these tests seed the escaping row
directly: the danger is the row that already exists, and a test that could
only produce one through the current mint would test nothing.

Every case here asserts on the FILE, not on the return value — what matters is
whether bytes moved outside ``data/knowledge``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import ConflictError
from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.utils.runbook_id import RunbookPathEscape

ORG = SingleTenantProvider.DEFAULT_ORG_ID

# No ``asyncio`` mark: ``asyncio_mode = auto``, and marking the module would
# also mark the synchronous helper tests.
pytestmark = [pytest.mark.unit, pytest.mark.security]


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _make_session_factory(job, draft):
    """Async session factory whose two SELECTs return ``job`` then ``draft``.

    Matches the sequence every guarded method runs: load the conversion job,
    then load the draft row. Mirrors the scaffolding in
    ``test_conversion_service_verify_draft_exceptions.py``.
    """
    calls = {"n": 0}

    async def _execute(_stmt):
        calls["n"] += 1
        result = MagicMock()
        if calls["n"] == 1:
            result.scalar_one_or_none.return_value = job
        else:
            result.scalar_one_or_none.return_value = draft
            result.scalars.return_value.all.return_value = [draft] if draft else []
        return result

    session = AsyncMock()
    session.execute = _execute
    # ``get_conversion`` traverses the ``source_file_id`` FK. There is no
    # upload row here; ``None`` takes the documented "source upload missing"
    # branch, which is enough to build the response this test reads.
    session.get = AsyncMock(return_value=None)

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return None

    return _Factory()


def _job(scope: str = "personal"):
    j = MagicMock()
    j.id = "conv_x"
    j.user_id = "user_x"
    j.scope = scope
    j.status = "completed"
    j.case_id = None
    j.organization_id = "org_x"
    j.analysis_result = None
    j.source_file_id = None
    j.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return j


def _draft(file_path: str, *, status: str = DraftStatus.DRAFT.value):
    d = MagicMock()
    d.id = "draft_deadbeef"
    d.runbook_id = "some-runbook"
    d.title = "Some Runbook"
    d.status = status
    d.file_path = file_path
    d.validation_passed = True
    d.validation_errors = []
    d.validation_warnings = []
    d.quality_details = {
        "overall": 80,
        "grade": "B",
        "completeness": 80,
        "clarity": 80,
        "actionability": 80,
        "comprehensiveness": 80,
    }
    d.source_type = "document"
    return d


def _service(job, draft) -> ConversionService:
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_make_session_factory(job, draft),
        knowledge_service=None,
    )


#: --------------------------------------------------------------------------
#: Real-database scaffolding, for the scan tests.
#:
#: The scan runs several statements and mutates rows; a mocked session cannot
#: express that, and the version of these tests that tried raised before the
#: loop under test and swallowed it. A real in-memory SQLite is the only way
#: these assertions can fail for the right reason.
#: --------------------------------------------------------------------------


async def _real_db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from faultmaven.infrastructure.persistence.models import (
        Base,
        EnterpriseModel,
        OrganizationModel,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(EnterpriseModel(enterprise_id=ORG, name="E", slug="e"))
        await session.flush()
        session.add(
            OrganizationModel(
                organization_id=ORG, name="O", slug="o", enterprise_id=ORG
            )
        )
        await session.commit()
    return factory


async def _seed_draft(factory, *, draft_id, file_path, status="draft"):
    """One conversion job + one draft row carrying ``file_path`` verbatim.

    Seeded directly, because the mint points are sanitised now: an escaping
    row is exactly the pre-#1215 shape that can no longer be created through
    the service, and that is the row this whole change exists for.
    """
    from faultmaven.infrastructure.persistence.models import (
        ConversionDraftModel,
        ConversionJobModel,
        UploadedFileModel,
    )

    async with factory() as session:
        file_id = f"file_{draft_id}"
        session.add(
            UploadedFileModel(
                file_id=file_id,
                organization_id=ORG,
                filename="src.md",
                size_bytes=1,
                content_type="text/markdown",
                uploaded_by="u1",
            )
        )
        await session.flush()
        session.add(
            ConversionJobModel(
                id=f"conv_{draft_id}",
                organization_id=ORG,
                user_id="u1",
                status="completed",
                scope="global",
                source_file_id=file_id,
            )
        )
        await session.flush()
        session.add(
            ConversionDraftModel(
                id=draft_id,
                organization_id=ORG,
                conversion_id=f"conv_{draft_id}",
                runbook_id=f"rb-{draft_id}",
                title="T",
                file_path=file_path,
                status=status,
                validation_passed=True,
            )
        )
        await session.commit()


async def _draft_status(factory, draft_id):
    from sqlalchemy import select

    from faultmaven.infrastructure.persistence.models import ConversionDraftModel

    async with factory() as session:
        row = (
            await session.execute(
                select(ConversionDraftModel).where(ConversionDraftModel.id == draft_id)
            )
        ).scalar_one()
        return row.status


def _real_service(factory) -> ConversionService:
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=factory,
        knowledge_service=None,
    )


#: What an escaping row looks like. Relative, like every path this service
#: persists, and climbing one level out of ``data/knowledge`` — enough to be
#: outside the tree while staying inside the tmp dir, so a leak is contained
#: to the test's own sandbox.
ESCAPING = "data/knowledge/../escaped/pwned.md"

SENTINEL = "ORIGINAL CONTENT — MUST NOT BE OVERWRITTEN\n"

FRONTMATTER_SENTINEL = (
    "---\n"
    'id: "pwned"\n'
    'title: "Pwned"\n'
    "status: draft\n"
    'verified_by: ""\n'
    "---\n\n"
    "# Not a runbook this service owns\n"
)


@pytest.fixture
def escaped_file(tmp_path, monkeypatch):
    """A file OUTSIDE ``data/knowledge`` that an escaping row points at.

    The directory is pre-created deliberately. Without it the unguarded code
    raises ``FileNotFoundError`` and every test below passes for the wrong
    reason — an accident of the fixture rather than containment. With it, the
    unguarded code SUCCEEDS in writing outside the tree, which is the behaviour
    being pinned. (Verified by reverting: see the PR body.)
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "knowledge").mkdir(parents=True)
    target = tmp_path / "data" / "escaped" / "pwned.md"
    target.parent.mkdir(parents=True)
    target.write_text(SENTINEL, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# The database-sourced paths
# ---------------------------------------------------------------------------


class TestADatabasePathThatEscapesIsRefused:
    async def test_update_draft_refuses_and_leaves_the_file_alone(self, escaped_file):
        service = _service(_job(), _draft(ESCAPING))

        with pytest.raises(ConflictError):
            await service.update_draft(
                conversion_id="conv_x",
                draft_id="draft_deadbeef",
                user_id="user_x",
                content="PWNED BY THE EDIT PATH\n",
            )

        assert escaped_file.read_text(encoding="utf-8") == SENTINEL

    async def test_the_refusal_is_typed_names_the_row_and_leaks_no_path(
        self, escaped_file, tmp_path, caplog
    ):
        """Three properties in one place, because they trade off against each
        other: a raw ``ValueError`` is an unmapped 500, a message useful enough
        to repair the row is a message containing server paths, and this
        exception's ``str()`` reaches a response body two ways (the 409
        handler's ``detail`` and ``verify_batch``'s per-item ``error``)."""
        service = _service(_job(), _draft(ESCAPING))

        with caplog.at_level("ERROR"):
            with pytest.raises(ConflictError) as exc:
                await service.update_draft(
                    conversion_id="conv_x",
                    draft_id="draft_deadbeef",
                    user_id="user_x",
                    content="anything\n",
                )

        client_message = str(exc.value)
        assert "draft_deadbeef" in client_message
        assert exc.value.resource_type == "draft"
        assert exc.value.resource_id == "draft_deadbeef"
        assert exc.value.conflict_reason == "path_outside_knowledge_tree"
        # No server filesystem path in anything the client can see (#866).
        assert str(tmp_path) not in client_message
        assert "data/escaped" not in client_message
        # The detail an operator needs went to the log instead.
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert str(tmp_path) in logged
        assert "draft_deadbeef" in logged

    async def test_verify_draft_refuses_before_reading_or_rewriting(self, escaped_file):
        escaped_file.write_text(FRONTMATTER_SENTINEL, encoding="utf-8")
        service = _service(_job(), _draft(ESCAPING))

        with pytest.raises(ConflictError):
            await service.verify_draft(
                conversion_id="conv_x",
                draft_id="draft_deadbeef",
                user_id="user_x",
                username="alice",
            )

        # The frontmatter rewrite would have flipped this to ``verified``, and
        # the content would have been read back into the response.
        assert escaped_file.read_text(encoding="utf-8") == FRONTMATTER_SENTINEL

    async def test_delete_draft_refuses_to_unlink_outside_the_tree(
        self, escaped_file, caplog
    ):
        """The only guarded verb that continues rather than aborting.

        The dangerous half is the ``unlink``, and that is refused. Aborting the
        whole call as well would leave a bad row permanently undeletable, so
        the soft-delete still runs — refusing the filesystem operation while
        letting the operator clear the row."""
        service = _service(_job(), _draft(ESCAPING))

        with caplog.at_level("ERROR"):
            ok = await service.delete_draft(
                conversion_id="conv_x",
                draft_id="draft_deadbeef",
                user_id="user_x",
            )

        assert ok is True, "the row must still be discardable"
        assert escaped_file.exists(), "nothing outside the tree may be unlinked"
        assert any(
            "draft_deadbeef" in record.getMessage() for record in caplog.records
        ), "the refusal must be logged, naming the row"

    async def test_get_conversion_omits_the_content_instead_of_leaking_it(
        self, escaped_file
    ):
        """The one caller that degrades rather than refuses: one bad row must
        not deny a whole listing. It must still not put the file's bytes into
        the response."""
        service = _service(_job(), _draft(ESCAPING))

        response = await service.get_conversion(
            conversion_id="conv_x", user_id="user_x"
        )

        assert response is not None
        assert len(response.drafts) == 1
        assert response.drafts[0].content is None
        assert SENTINEL not in (response.drafts[0].content_preview or "")


# ---------------------------------------------------------------------------
# The success cases — without these the class above passes vacuously on a
# fixture that simply breaks the service.
# ---------------------------------------------------------------------------


class TestALegitimatePathStillWorks:
    async def test_update_draft_writes_a_contained_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inside = tmp_path / "data" / "knowledge" / "user_x" / "ok.md"
        inside.parent.mkdir(parents=True)
        inside.write_text(SENTINEL, encoding="utf-8")

        service = _service(_job(), _draft("data/knowledge/user_x/ok.md"))
        result = await service.update_draft(
            conversion_id="conv_x",
            draft_id="draft_deadbeef",
            user_id="user_x",
            content="# Updated\n",
        )

        assert result is not None
        assert inside.read_text(encoding="utf-8") == "# Updated\n"

    async def test_get_conversion_returns_contained_content(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        inside = tmp_path / "data" / "knowledge" / "user_x" / "ok.md"
        inside.parent.mkdir(parents=True)
        inside.write_text("# Readable\n", encoding="utf-8")

        service = _service(_job(), _draft("data/knowledge/user_x/ok.md"))
        response = await service.get_conversion(
            conversion_id="conv_x", user_id="user_x"
        )

        assert response.drafts[0].content == "# Readable\n"


# ---------------------------------------------------------------------------
# The assembled paths
# ---------------------------------------------------------------------------


class TestTheAssembledDraftPathIsGuardedToo:
    """``draft_path = scope_dir / f"{runbook_id}.md"`` at the two mint sites.

    Both components are sanitised, so the guard cannot fire through the public
    surface — which is the design, and the reason it is exercised by defeating
    the sanitiser. That is precisely the future it exists for: a loosened mint
    rule, or a new caller assembling its own path.
    """

    async def test_a_template_runbook_lands_inside_the_tree(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        service = _service(_job(), None)
        service._db_session_factory = None  # skip persistence, keep the write

        created = await service.create_runbook_from_template(
            title="../../../etc/pwned",
            domain="platform",
            service_name="../../..",
            symptom_class=["availability"],
            severity="high",
            scope="personal",
            tags=[],
            difficulty="medium",
            symptom_recognition="x",
            applicability="x",
            diagnostic_steps="x",
            causes="x",
            prevention="x",
            user_id="user_x",
        )

        written = list((tmp_path / "data" / "knowledge").rglob("*.md"))
        assert len(written) == 1, written
        assert not [
            p
            for p in tmp_path.rglob("*.md")
            if "data/knowledge" not in str(p.relative_to(tmp_path).as_posix())
        ]
        for part in written[0].relative_to(tmp_path).parts:
            assert part != ".."
        # The path PERSISTED stays relative: the scan pass matches these
        # strings against a walk of the same relative root, so an absolute one
        # would be a miss and would manufacture a duplicate draft.
        assert created["draft"].file_path.startswith("data/knowledge/")

    async def test_an_escaping_scope_dir_is_refused_and_creates_nothing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        service = _service(_job(), None)
        service._db_session_factory = None

        # Defeat the directory sanitiser only; the write helper is the last
        # line. Asserts on DIRECTORIES as well as files: ``mkdir(parents=True)``
        # running before the check is half of the defect this replaces, and it
        # materialises attacker-chosen directories even when no file is written.
        monkeypatch.setattr(
            ConversionService,
            "_scope_dir",
            lambda self, scope, team_id=None, user_id=None: tmp_path
            / "data"
            / "knowledge"
            / ".."
            / "escaped_dir",
        )

        with pytest.raises(RunbookPathEscape):
            await service.create_runbook_from_template(
                title="ok",
                domain="platform",
                service_name="svc",
                symptom_class=["availability"],
                severity="high",
                scope="personal",
                tags=[],
                difficulty="medium",
                symptom_recognition="x",
                applicability="x",
                diagnostic_steps="x",
                causes="x",
                prevention="x",
                user_id="user_x",
            )

        assert not (tmp_path / "data" / "escaped_dir").exists()
        assert not list(tmp_path.rglob("*.md"))


# ---------------------------------------------------------------------------
# The reconciliation probe
# ---------------------------------------------------------------------------


class TestTheHelperItself:
    """The two boundary shapes the call-site tests cannot reach."""

    def test_the_root_itself_is_refused(self, tmp_path):
        """It is a directory, so a write would fail anyway — but
        ``write_runbook_file`` would first ``mkdir`` its PARENT, which is
        outside the tree."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        root = tmp_path / "data" / "knowledge"
        root.mkdir(parents=True)
        with pytest.raises(RunbookPathEscape):
            resolve_runbook_path(root, source="probe", root=root)

    def test_a_symlink_out_of_the_tree_is_refused(self, tmp_path):
        """Containment is a property of where the path RESOLVES to, so a link
        inside the tree pointing out of it does not launder an escape."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        root = tmp_path / "data" / "knowledge"
        root.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text(SENTINEL, encoding="utf-8")
        link = root / "innocent.md"
        link.symlink_to(outside)

        with pytest.raises(RunbookPathEscape):
            resolve_runbook_path(link, source="probe", root=root)

    def test_an_unresolvable_path_is_refused_not_raised_raw(self, tmp_path):
        """``Path.resolve()`` raises ``RuntimeError`` on a symlink loop
        (measured, CPython 3.11.9) and ``OSError`` on other conditions.

        An unresolvable path is not a containable path, so it must arrive as
        ``RunbookPathEscape`` like any other refusal. Letting the raw
        ``RuntimeError`` through killed ``delete_draft`` before its soft-delete
        and aborted whole scans."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        root = tmp_path / "data" / "knowledge"
        root.mkdir(parents=True)
        a, b = root / "a.md", root / "b.md"
        a.symlink_to(b)
        b.symlink_to(a)

        with pytest.raises(RunbookPathEscape) as exc:
            resolve_runbook_path(a, source="probe", root=root)
        assert "unresolvable" in str(exc.value)
        assert isinstance(exc.value.__cause__, RuntimeError)

    def test_root_is_required(self):
        """The optional default was a bypass no test could see: every test
        redirects the root through ``_data_dir``, so a caller omitting ``root=``
        would check the real ``data/knowledge`` and pass every suite while
        guarding the wrong tree."""
        from faultmaven.utils.runbook_id import (
            resolve_runbook_path,
            write_runbook_file,
        )

        with pytest.raises(TypeError):
            resolve_runbook_path("x.md", source="probe")
        with pytest.raises(TypeError):
            write_runbook_file("x.md", "c", source="probe")


class TestASymlinkLoopDoesNotBreakTheDegradingPaths:
    """The two callers that must survive an unresolvable path rather than die
    on it. Both were killed by the raw ``RuntimeError`` before the helper
    converted it."""

    @pytest.fixture
    def looped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        kb = tmp_path / "data" / "knowledge"
        kb.mkdir(parents=True)
        a, b = kb / "a.md", kb / "b.md"
        a.symlink_to(b)
        b.symlink_to(a)
        return "data/knowledge/a.md"

    async def test_delete_draft_still_discards_the_row(self, looped):
        service = _service(_job(), _draft(looped))
        ok = await service.delete_draft(
            conversion_id="conv_x", draft_id="draft_deadbeef", user_id="user_x"
        )
        assert (
            ok is True
        ), "a row pointing at an unresolvable path must still be discardable"

    async def test_get_conversion_omits_content_rather_than_failing(self, looped):
        service = _service(_job(), _draft(looped))
        response = await service.get_conversion(
            conversion_id="conv_x", user_id="user_x"
        )
        assert response is not None
        assert response.drafts[0].content is None


class TestTheScanSkipsWhatItRefusesToTouch:
    """Escape is NOT absence.

    Treating it as absence discarded the row, which (a) trips the
    'would discard ALL active drafts' abort guard when every active draft
    escapes — so the promised self-repair never runs — and (b) mass-discards a
    legitimate symlinked layout on the first post-upgrade scan.

    Driven against a REAL database, unsuppressed. The previous version of this
    test used a mocked session that raised before the loop and swallowed it, so
    its only assertion could not fail.
    """

    async def test_the_scan_completes_and_does_not_discard_the_row(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "knowledge").mkdir(parents=True)
        outside = tmp_path / "data" / "escaped" / "pwned.md"
        outside.parent.mkdir(parents=True)
        outside.write_text(SENTINEL, encoding="utf-8")

        factory = await _real_db()
        await _seed_draft(factory, draft_id="d_escaping", file_path=ESCAPING)
        service = _real_service(factory)

        with caplog.at_level("WARNING"):
            result = await service.scan_for_runbooks(user_id="u1")

        assert result is not None, "the scan must complete, not abort"
        assert await _draft_status(factory, "d_escaping") == "draft", (
            "an escaping row must be SKIPPED, never discarded — discarding it "
            "is the data loss a symlinked layout would suffer"
        )
        assert outside.read_text(encoding="utf-8") == SENTINEL
        assert any(
            "d_escaping" in r.getMessage() and "skipping" in r.getMessage()
            for r in caplog.records
        ), "the skip must be logged, naming the row"

    async def test_an_only_escaping_deployment_does_not_abort_every_scan(
        self, tmp_path, monkeypatch
    ):
        """The regression that made the repair unreachable: with the escaping
        row counted as discardable and no other active draft, the abort guard
        fired on every scan."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "knowledge").mkdir(parents=True)

        factory = await _real_db()
        await _seed_draft(factory, draft_id="d_only", file_path=ESCAPING)
        service = _real_service(factory)

        result = await service.scan_for_runbooks(user_id="u1")
        assert result is not None
        assert await _draft_status(factory, "d_only") == "draft"

    async def test_a_missing_file_is_still_discarded(self, tmp_path, monkeypatch):
        """The success case that stops the two above from passing vacuously:
        a CONTAINED path whose file is gone is still reconciled away."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "knowledge").mkdir(parents=True)

        factory = await _real_db()
        await _seed_draft(
            factory, draft_id="d_gone", file_path="data/knowledge/gone.md"
        )
        await _seed_draft(factory, draft_id="d_escaping", file_path=ESCAPING)
        service = _real_service(factory)

        await service.scan_for_runbooks(user_id="u1")
        assert await _draft_status(factory, "d_gone") == "discarded"
        assert await _draft_status(factory, "d_escaping") == "draft"


class TestTheScanWalkRefusesASymlinkOutOfTheTree:
    """``rglob`` follows symlinks, so a link planted inside the tree was read
    and minted into a draft — the shape every other path here refuses. Both
    halves of the module must agree on what a runbook file is."""

    async def test_a_planted_symlink_is_not_minted_as_a_draft(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.chdir(tmp_path)
        kb = tmp_path / "data" / "knowledge" / "global"
        kb.mkdir(parents=True)
        secret = tmp_path / "outside" / "secret.md"
        secret.parent.mkdir(parents=True)
        secret.write_text(
            "---\nid: secret-runbook\ntitle: Secret\n---\n\n# Secret\n" + "x" * 300,
            encoding="utf-8",
        )
        (kb / "innocent.md").symlink_to(secret)
        # A legitimate sibling, so the assertion below distinguishes "refused
        # the symlink" from "the walk found nothing at all".
        (kb / "real.md").write_text(
            "---\nid: real-runbook\ntitle: Real\n---\n\n# Real\n" + "y" * 300,
            encoding="utf-8",
        )

        factory = await _real_db()
        service = _real_service(factory)
        with caplog.at_level("WARNING"):
            # ``is_platform_admin`` because the fixture writes into ``global/``
            # and a non-admin caller may not mint global-scope drafts — without
            # it BOTH files are skipped and the assertion below passes for the
            # wrong reason.
            result = await service.scan_for_runbooks(
                user_id="u1", is_platform_admin=True
            )

        # ``discovered`` is a COUNT; ``drafts`` is the list.
        minted = [d.get("runbook_id") for d in result.get("drafts", [])]
        assert "real-runbook" in minted, f"the walk must still work: {result}"
        assert (
            "secret-runbook" not in minted
        ), "a symlink out of the tree was read and minted as a draft"
        assert any("outside the tree" in r.getMessage() for r in caplog.records)


class TestTheEmptyIdFilename:
    """``runbook_id_from_parts`` returns ``""`` for a punctuation-only or
    non-latin ``(service, title)``, and ``scope_dir / ".md"`` passes containment
    while being a hidden file every such runbook in the scope shares."""

    def test_an_empty_id_does_not_produce_a_shared_dotfile(self):
        from faultmaven.utils.runbook_id import draft_filename

        assert draft_filename("") != ".md"
        assert draft_filename("...") != ".md"
        assert draft_filename("") != draft_filename("")
        for name in (draft_filename(""), draft_filename(None)):
            assert name.endswith(".md")
            assert not name.startswith(".")

    def test_an_ordinary_id_is_unchanged(self):
        """Without this the fallback could rename every file and still pass."""
        from faultmaven.utils.runbook_id import draft_filename

        assert draft_filename("checkout-api-pool-exhausted") == (
            "checkout-api-pool-exhausted.md"
        )

    async def test_two_punctuation_titled_runbooks_get_separate_files(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        service = _service(_job(), None)
        service._db_session_factory = None

        common = dict(
            domain="platform",
            symptom_class=["availability"],
            severity="high",
            scope="global",
            tags=[],
            difficulty="medium",
            symptom_recognition="x",
            applicability="x",
            diagnostic_steps="x",
            causes="x",
            prevention="x",
            user_id="user_x",
        )
        a = await service.create_runbook_from_template(
            title="???", service_name="...", **common
        )
        b = await service.create_runbook_from_template(
            title="!!!", service_name="___", **common
        )

        assert a["draft"].file_path != b["draft"].file_path
        written = sorted((tmp_path / "data" / "knowledge").rglob("*.md"))
        assert (
            len(written) == 2
        ), f"one file means the second overwrote the first: {written}"
        assert not any(p.name == ".md" for p in written)
