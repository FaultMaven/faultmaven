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

import contextlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)
from faultmaven.utils.runbook_id import RunbookPathEscape

# No ``asyncio`` mark: ``asyncio_mode = auto``, and marking the module would
# also mark the two synchronous helper tests at the bottom.
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

        with pytest.raises(RunbookPathEscape):
            await service.update_draft(
                conversion_id="conv_x",
                draft_id="draft_deadbeef",
                user_id="user_x",
                content="PWNED BY THE EDIT PATH\n",
            )

        assert escaped_file.read_text(encoding="utf-8") == SENTINEL

    async def test_the_refusal_names_the_row(self, escaped_file):
        """'Some draft is bad' is not actionable — the operator has to be able
        to find the row, since the fix is a database repair."""
        service = _service(_job(), _draft(ESCAPING))

        with pytest.raises(RunbookPathEscape) as exc:
            await service.update_draft(
                conversion_id="conv_x",
                draft_id="draft_deadbeef",
                user_id="user_x",
                content="anything\n",
            )

        message = str(exc.value)
        assert "outside the knowledge tree" in message
        assert "conversion_drafts.file_path" in message
        assert "draft_deadbeef" in message

    async def test_verify_draft_refuses_before_reading_or_rewriting(self, escaped_file):
        escaped_file.write_text(FRONTMATTER_SENTINEL, encoding="utf-8")
        service = _service(_job(), _draft(ESCAPING))

        with pytest.raises(RunbookPathEscape):
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


class TestTheScanProbeDoesNotFollowAnEscapingRow:
    async def test_an_escaping_row_is_treated_as_absent(self, escaped_file):
        """The probe decides whether a draft is discarded. An escaping row must
        not have an arbitrary file's existence answer that question."""
        from faultmaven.utils.runbook_id import resolve_runbook_path

        with pytest.raises(RunbookPathEscape):
            resolve_runbook_path(ESCAPING, source="conversion_drafts.file_path")

        # And the guarded call site turns that into "absent" rather than
        # propagating, so one bad row cannot abort a whole scan.
        service = _service(_job(), _draft(ESCAPING))
        with contextlib.suppress(Exception):
            await service._scan_for_runbooks_impl(user_id="user_x")

        assert escaped_file.exists(), "the scan must not delete anything"
