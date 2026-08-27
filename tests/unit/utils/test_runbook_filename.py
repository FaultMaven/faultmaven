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

    @pytest.mark.parametrize(
        "title",
        ["../../../etc/pwned", "/absolute/path", "....//....//evil", ".."],
    )
    def test_a_traversal_title_cannot_climb_out_of_its_directory(self, title):
        from pathlib import Path

        target = Path("data/knowledge/global")
        resolved = (target / runbook_filename(title, DOC_ID)).resolve()

        assert str(resolved).startswith(
            str(target.resolve())
        ), f"title {title!r} resolved to {resolved}, outside the knowledge tree"


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

    def test_a_title_with_no_usable_characters_falls_back_to_the_id(self):
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
            assert DOC_ID.replace("kb_", "") in name or DOC_ID in name

    def test_a_very_long_title_is_bounded(self):
        name = runbook_filename("x" * 500, DOC_ID)

        assert len(name) <= 120
        assert SAFE.match(name)

    def test_runs_of_separators_collapse(self):
        name = runbook_filename("a   b---c", DOC_ID)

        assert "--" not in name.replace(f"-{DOC_ID}", "")
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
        from unittest.mock import AsyncMock, MagicMock

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

        svc = KnowledgeService.__new__(KnowledgeService)
        svc.logger = MagicMock()
        # Fail immediately AFTER the write, so the test exercises the filename
        # and containment logic without needing a database.
        svc._db_session_factory = MagicMock(side_effect=RuntimeError("stop here"))
        svc.ingest_runbook = AsyncMock(return_value=1)

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
