"""Tests for scripts/refactor/verify_move.py.

Each test builds a tiny git repo: an "old" single-file module committed at
HEAD, then a hand-written package split in the working tree (uncommitted —
verify_move only needs the OLD side at a revision; the NEW side is read
straight off disk). Every test runs under --clean, the mode #1707 uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import commit_all, load_module, write

pytestmark = pytest.mark.unit

OLD_SRC = """import logging

logger = logging.getLogger("old.mod")


def foo(x):
    return x + 1


def bar(x):
    return x - 1
"""


@pytest.fixture(scope="module")
def mod():
    return load_module("verify_move")


def _base_split(repo: Path) -> str:
    """Commit OLD_SRC as old/mod.py, then lay out the clean split. Returns old_rel."""
    old_rel = "old/mod.py"
    write(repo / old_rel, OLD_SRC)
    commit_all(repo)
    (repo / old_rel).unlink()
    pkg = repo / "old" / "mod"
    write(pkg / "__init__.py", '"""Package facade."""\n')
    write(
        pkg / "foo.py",
        "import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef foo(x):\n    return x + 1\n",
    )
    write(
        pkg / "bar.py",
        "import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef bar(x):\n    return x - 1\n",
    )
    return old_rel


def _run(mod, monkeypatch, capsys, repo: Path, old_rel: str, *extra: str):
    argv = [
        "verify_move.py",
        "--repo",
        str(repo),
        "--base",
        "HEAD",
        "--old",
        old_rel,
        "--clean",
        *extra,
    ]
    monkeypatch.setattr("sys.argv", argv)
    code = mod.main()
    return code, capsys.readouterr().out


class TestIdenticalSplit:
    def test_an_identical_split_passes(self, mod, monkeypatch, capsys, repo):
        old_rel = _base_split(repo)

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 0
        assert "RESULT: PASS" in out


class TestChangedBody:
    def test_a_changed_body_fails(self, mod, monkeypatch, capsys, repo):
        old_rel = _base_split(repo)
        write(
            repo / "old/mod/bar.py",
            "import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef bar(x):\n    return x - 2\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "CHANGED" in out
        assert "RESULT: FAIL" in out


class TestDuplicatedDef:
    def test_a_duplicated_def_fails(self, mod, monkeypatch, capsys, repo):
        old_rel = _base_split(repo)
        # bar() also pasted into foo.py: now defined twice across the package.
        write(
            repo / "old/mod/foo.py",
            "import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef foo(x):\n    return x + 1\n\n\ndef bar(x):\n    return x - 1\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "DUPLICATED" in out


class TestMissingDef:
    def test_a_missing_def_fails(self, mod, monkeypatch, capsys, repo):
        old_rel = _base_split(repo)
        (repo / "old/mod/bar.py").unlink()

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "MISSING" in out
        assert "bar" in out


class TestLoggerUnderClean:
    def test_a_literal_logger_name_fails_under_clean(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        write(
            repo / "old/mod/__init__.py",
            '"""Package facade."""\nimport logging\n\nlogger = logging.getLogger("old.mod")\n',
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "LOGGER" in out
        assert "use __name__" in out


class TestImportOnlyBodyChange:
    def test_a_body_change_confined_to_a_function_local_import_passes_as_import_only(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        # bar() gained a function-local import and nothing else: a caller
        # update, not a body edit.
        write(
            repo / "old/mod/bar.py",
            "import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef bar(x):\n    import os\n\n    return x - 1\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 0, out
        assert "IMPORT-ONLY" in out
        assert "CHANGED" not in out

    def test_any_other_change_in_that_body_still_fails(
        self, mod, monkeypatch, capsys, repo
    ):
        old_rel = _base_split(repo)
        # Same added import line, but the return value also changed: not
        # import-only any more, so the whole body must be flagged CHANGED.
        write(
            repo / "old/mod/bar.py",
            "import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef bar(x):\n    import os\n\n    return x - 2\n",
        )

        code, out = _run(mod, monkeypatch, capsys, repo, old_rel)

        assert code == 1
        assert "CHANGED" in out
