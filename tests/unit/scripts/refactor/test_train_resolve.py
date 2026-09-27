"""Tests for scripts/refactor/train_resolve.py.

Each test writes a file already containing diff3-style conflict markers
(``<<<<<<<`` / ``|||||||`` / ``=======`` / ``>>>>>>>``) — as `git merge -c
merge.conflictstyle=diff3` would leave it — and checks how the file reads
after the tool rewrites it in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import load_module

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def mod():
    return load_module("train_resolve")


def _call(mod, paths):
    import sys

    old_argv = sys.argv
    sys.argv = ["train_resolve.py", *paths]
    try:
        return mod.main()
    finally:
        sys.argv = old_argv


class TestImportOnlyHunk:
    def test_an_import_only_hunk_on_one_side_takes_the_other_side(
        self, tmp_path: Path, mod
    ):
        # HEAD (ours) is unchanged from base; theirs both re-pointed the
        # import AND added a real function. Per the rule, ours being
        # import-only relative to base means the OTHER side (theirs) wins.
        f = tmp_path / "user.py"
        f.write_text(
            "<<<<<<< HEAD\n"
            "from pkg.mod import read_value\n"
            "||||||| BASE\n"
            "from pkg.mod import read_value\n"
            "=======\n"
            "from pkg.mod.other import read_value\n"
            "\n\n"
            "def helper():\n"
            "    return 2\n"
            ">>>>>>> theirs\n"
            "def use():\n"
            "    return read_value()\n"
        )

        code = _call(mod, [str(f)])

        assert code == 0
        text = f.read_text()
        assert "<<<<<<<" not in text
        assert "from pkg.mod.other import read_value" in text
        assert "def helper():" in text
        assert "from pkg.mod import read_value" not in text


class TestDocsHunk:
    def test_a_docs_hunk_with_edits_to_different_lines_merges_line_by_line(
        self, tmp_path: Path, mod
    ):
        f = tmp_path / "doc.md"
        f.write_text(
            "<<<<<<< HEAD\n"
            "# Title\n"
            "line one changed by ours\n"
            "line two\n"
            "||||||| BASE\n"
            "# Title\n"
            "line one\n"
            "line two\n"
            "=======\n"
            "# Title\n"
            "line one\n"
            "line two changed by theirs\n"
            ">>>>>>> theirs\n"
            "tail unchanged\n"
        )

        code = _call(mod, [str(f)])

        assert code == 0
        text = f.read_text()
        assert "<<<<<<<" not in text
        assert "line one changed by ours" in text
        assert "line two changed by theirs" in text
        assert "tail unchanged" in text


class TestNonImportBothSides:
    def test_a_hunk_with_non_import_edits_on_both_sides_is_left_unresolved(
        self, tmp_path: Path, mod
    ):
        f = tmp_path / "logic.py"
        original = (
            "<<<<<<< HEAD\n"
            "def use():\n"
            "    return 1\n"
            "||||||| BASE\n"
            "def use():\n"
            "    return 0\n"
            "=======\n"
            "def use():\n"
            "    return 2\n"
            ">>>>>>> theirs\n"
        )
        f.write_text(original)

        code = _call(mod, [str(f)])

        assert code == 1
        text = f.read_text()
        assert "<<<<<<<" in text
        assert text == original
