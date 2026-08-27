"""Regression set for #1213 — the on-disk runbook filename was unsanitized.

``KnowledgeService.upload_document`` built the file it writes as::

    filename = f"{title.lower().replace(' ', '-')[:60]}-{hex}.md"
    file_path = target_dir / filename
    file_path.write_text(content)

``title`` reaches that expression straight from the caller: it is a form field
on ``POST /knowledge/documents`` and, since #1211, an LLM-generated
``suggested_title`` derived from case content. Only spaces were replaced, so a
title carrying path separators escaped ``data/knowledge`` entirely.

Two independent guards, because either alone is a single point of failure:

1. ``runbook_filename`` builds the name from an ALLOWLIST, so nothing outside
   ``[a-z0-9-]`` survives to be interpreted by the filesystem.
2. the write site asserts containment, which holds even if the slug rule is
   later loosened or a new caller mints its own name.
"""

import contextlib
import re

import pytest

from faultmaven.utils.runbook_id import runbook_filename

pytestmark = pytest.mark.unit

DOC_ID = "kb_abcdef0123456789"
SAFE = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")


class TestTheNameIsAlwaysASingleSafeComponent:
    @pytest.mark.parametrize(
        "title",
        [
            "Redis pool exhaustion",
            "../../../etc/pwned",
            "/absolute/path",
            "..\\..\\windows\\style",
            "....//....//evil",
            "a/b nested",
            "trailing/",
            "with\x00nul",
            "dots...everywhere",
            "..",
            ".",
        ],
    )
    def test_no_title_can_introduce_a_path_separator(self, title):
        name = runbook_filename(title, DOC_ID)

        assert "/" not in name
        assert "\\" not in name
        assert "\x00" not in name
        assert name not in (".", "..")
        # One component, and one the shell and the filesystem both read plainly.
        assert SAFE.match(name), f"unsafe filename {name!r} from title {title!r}"

    # NOTE: an earlier draft also asserted that
    # ``(Path("data/knowledge/global") / runbook_filename(t, ID)).resolve()``
    # stayed inside that directory. That was tautological — it re-derived the
    # name from the very function under test, so it could only fail when the
    # test above already had — and it resolved a RELATIVE path against whatever
    # directory pytest happened to run in. The real containment coverage is
    # ``TestTheWriteSiteItself`` below, which drives the actual write.


class TestItStaysUsable:
    def test_an_ordinary_title_is_still_readable(self):
        name = runbook_filename("Redis pool exhaustion", DOC_ID)

        assert name.startswith("redis-pool-exhaustion-")
        assert name.endswith(".md")

    def test_the_name_is_unique_for_one_title(self):
        """Two runbooks may share a title; the file must not collide."""
        a = runbook_filename("Redis pool exhaustion", "kb_1111111111111111")
        b = runbook_filename("Redis pool exhaustion", "kb_2222222222222222")

        assert a != b

    def test_a_title_with_no_usable_characters_still_yields_a_valid_name(self):
        """Punctuation-only, or a script the allowlist drops entirely.

        The name is then less readable but still valid and still unique, which
        is the right trade: an allowlist that admitted non-latin characters
        would have to reason about unicode look-alikes for the separator (e.g.
        FULLWIDTH SOLIDUS) and normalization, and that is exactly the class of
        analysis this avoids.
        """
        for title in ["...", "///", "!!!", "日本語のタイトル"]:
            name = runbook_filename(title, DOC_ID)
            assert SAFE.match(name), f"{title!r} -> {name!r}"
            # The id reaches the name in its FILTERED form: `kb_x` -> `kb-x`.
            # An earlier draft asserted `DOC_ID in name`, which can never be
            # true for that reason, so the check silently did nothing.
            assert "kb-abcdef0123456789" in name

    def test_a_very_long_title_is_bounded(self):
        name = runbook_filename("x" * 500, DOC_ID)

        assert len(name) <= 120
        assert SAFE.match(name)

    def test_runs_of_separators_collapse(self):
        name = runbook_filename("a   b---c", DOC_ID)

        # `--` anywhere would mean a run survived. The regex already forbids
        # it, which is the point: assert on the whole name rather than trying
        # to strip the suffix — an earlier draft stripped `-{DOC_ID}`, which
        # never matched (the id is hyphenated by the filter) and so tested
        # nothing.
        assert "--" not in name
        assert name.startswith("a-b-c-")
        assert SAFE.match(name)


class TestTheWriteSiteItself:
    """Drives ``upload_document`` far enough to reach the write, with the CWD
    moved into a tmp dir so ``data/knowledge`` is created there.

    The helper tests above pin the slug. This pins the thing that actually
    matters — that a traversal title does not put a file outside the tree — and
    it does so through the real code path rather than by re-deriving the name.
    """

    async def test_a_traversal_title_writes_inside_the_tree(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path
        from unittest.mock import MagicMock

        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            KnowledgeService,
        )

        monkeypatch.chdir(tmp_path)
        # Pre-create the directory the traversal targets. Without it the
        # unfixed code fails with FileNotFoundError and the test would pass for
        # the wrong reason — an accident of the fixture, not containment. With
        # it, the unfixed code SUCCEEDS in writing outside the tree, which is
        # the behaviour being pinned. (Verified by reverting: the escaped file
        # appears here and the assertion below catches it.)
        (tmp_path / "etc").mkdir()

        # Fail immediately AFTER the write, so the test exercises the filename
        # and containment logic without needing a database. Nothing else needs
        # stubbing: `upload_document` logs through the MODULE-level logger, and
        # `ingest_runbook` is never reached.
        svc = KnowledgeService.__new__(KnowledgeService)
        svc._db_session_factory = MagicMock(side_effect=RuntimeError("stop here"))

        # The call is stopped deliberately after the write; the FILE is what
        # this test is about, not the return value.
        with contextlib.suppress(RuntimeError):
            await svc.upload_document(
                content="# Runbook\n",
                title="../../../etc/pwned",
                document_type="runbook",
                scope="global",
            )

        escaped = list(tmp_path.glob("etc/*.md"))
        assert not escaped, f"content written outside the knowledge tree: {escaped}"

        written = list((tmp_path / "data" / "knowledge" / "global").glob("*.md"))
        assert len(written) == 1, f"expected one runbook in the tree, got {written}"
        assert "/" not in written[0].name
        for part in written[0].relative_to(tmp_path).parts:
            assert part != "..", "a traversal component survived into the path"


def _stopping_service():
    """A ``KnowledgeService`` that reaches the write and then stops."""
    from unittest.mock import MagicMock

    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = MagicMock(side_effect=RuntimeError("stop here"))
    return svc


def _tree_state(root):
    from pathlib import Path

    root = Path(root)
    inside = sorted(
        str(p.relative_to(root)) for p in root.glob("data/knowledge/**/*.md")
    )
    outside = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*.md")
        if "data/knowledge" not in str(p)
    )
    dirs = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir())
    return inside, outside, dirs


class TestTheScopeDirectoryCannotEscapeEither:
    """The hole the FIRST version of this fix left open.

    ``team_id``/``owner_id`` are interpolated into the directory name. The
    original guard anchored containment on ``target_dir`` — the directory those
    values are IN — so an escaped directory trivially contained its own child
    and the assertion passed. Measured on that version: the runbook was written
    to ``<cwd>/escaped/`` with every filename-level check green.

    These are the tests whose absence let that ship. They are worth more than
    the title cases: a title escape was already impossible once the slug was an
    allowlist, but nothing constrained the directory.
    """

    async def test_an_escaping_owner_id_writes_inside_the_tree(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "escaped").mkdir()

        with contextlib.suppress(RuntimeError):
            await _stopping_service().upload_document(
                content="# Runbook\n",
                title="pwned",
                document_type="runbook",
                scope="personal",
                owner_id="../../../../escaped",
            )

        inside, outside, dirs = _tree_state(tmp_path)
        assert not outside, f"content written outside the knowledge tree: {outside}"
        assert len(inside) == 1, inside
        assert "data/knowledge/user_.." not in dirs, (
            "mkdir(parents=True) materialised an escaping directory before any "
            "guard could fire"
        )

    async def test_an_escaping_team_id_writes_inside_the_tree(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "escaped").mkdir()

        with contextlib.suppress(RuntimeError):
            await _stopping_service().upload_document(
                content="# Runbook\n",
                title="pwned",
                document_type="runbook",
                scope="team",
                team_id="../../../../escaped",
            )

        inside, outside, dirs = _tree_state(tmp_path)
        assert not outside, f"content written outside the knowledge tree: {outside}"
        assert "data/knowledge/team_.." not in dirs

    async def test_an_ordinary_owner_id_still_gets_its_own_directory(
        self, tmp_path, monkeypatch
    ):
        """The sanitiser must not flatten every user into one folder."""
        monkeypatch.chdir(tmp_path)

        for owner in ("user-abc", "user-def"):
            with contextlib.suppress(RuntimeError):
                await _stopping_service().upload_document(
                    content="# Runbook\n",
                    title="notes",
                    document_type="runbook",
                    scope="personal",
                    owner_id=owner,
                )

        dirs = _tree_state(tmp_path)[2]
        assert "data/knowledge/user_user-abc" in dirs
        assert "data/knowledge/user_user-def" in dirs


class TestTheContainmentGuardIsLive:
    """The guard cannot fire through the public surface — the sanitisers make
    an escape unconstructible, which is the design. So it is exercised by
    defeating one of them, which is exactly the future this guard exists for:
    a loosened slug rule, or a new caller assembling its own path."""

    async def test_a_filename_that_escapes_is_refused(self, tmp_path, monkeypatch):
        import faultmaven.utils.runbook_id as runbook_id

        monkeypatch.chdir(tmp_path)
        # Defeat the filename sanitiser only; the guard is the last line.
        # Patched on the SOURCE module, because `upload_document` imports the
        # helper inside the function and so re-reads it on every call.
        monkeypatch.setattr(
            runbook_id, "runbook_filename", lambda title, doc_id: "../../../escaped.md"
        )
        (tmp_path / "escaped.md").parent.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="outside the knowledge tree"):
            await _stopping_service().upload_document(
                content="# Runbook\n",
                title="anything",
                document_type="runbook",
                scope="global",
            )

        _, outside, _ = _tree_state(tmp_path)
        assert not outside
