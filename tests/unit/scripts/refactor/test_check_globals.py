"""Tests for scripts/refactor/check_globals.py.

check_globals compares an OLD worktree (a real checkout of the base
revision, not a git revision — the tool execs the old file directly) against
a HEAD worktree holding the split. Both are built under tmp_path by copying
files, not git.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from .conftest import load_module, write

pytestmark = pytest.mark.unit

OLD_SRC = """import math

SCALE = 2


def compute(x):
    return x * SCALE


class Thing:
    stamp = math.pi

    def when(self):
        return math.pi


handler = lambda: math.pi
"""


@pytest.fixture(scope="module")
def mod():
    return load_module("check_globals")


@pytest.fixture
def worktrees(tmp_path: Path):
    """base/pkg/mod.py (old module) and head/ (a copy, for the split to overwrite)."""
    base = tmp_path / "base"
    head = tmp_path / "head"
    write(base / "pkg" / "__init__.py", "")
    write(base / "pkg" / "mod.py", OLD_SRC)
    shutil.copytree(base, head)
    return base, head


def _split(head: Path, core_src: str) -> None:
    (head / "pkg" / "mod.py").unlink()
    write(head / "pkg" / "mod" / "__init__.py", '"""facade"""\n')
    write(head / "pkg" / "mod" / "core.py", core_src)


def _run(mod, monkeypatch, capsys, base: Path, head: Path, old_rel: str = "pkg/mod.py"):
    argv = [
        "check_globals.py",
        "--repo",
        str(head),
        "--base",
        str(base),
        "--old",
        old_rel,
    ]
    monkeypatch.setattr("sys.argv", argv)
    code = mod.main()
    return code, capsys.readouterr().out


class TestIdenticalPasses:
    def test_an_identical_split_passes(self, mod, monkeypatch, capsys, worktrees):
        base, head = worktrees
        _split(head, OLD_SRC)

        code, out = _run(mod, monkeypatch, capsys, base, head)

        assert code == 0, out
        assert "RESULT: PASS" in out


class TestRebound:
    """A submodule that rebinds an imported name to a look-alike object.

    The look-alike (`cmath` standing in for `math`) is read only inside a
    class body (`stamp = math.pi`) and a lambda (`handler = lambda:
    math.pi`) — never inside an ordinary function — which is the blind spot
    a global-read scan that stops at function boundaries misses. symtable
    does not stop there, so this must still be caught.
    """

    REBOUND_CORE = OLD_SRC.replace("import math", "import cmath as math")

    def test_a_rebound_lookalike_is_reported(self, mod, monkeypatch, capsys, worktrees):
        base, head = worktrees
        _split(head, self.REBOUND_CORE)

        code, out = _run(mod, monkeypatch, capsys, base, head)

        assert code == 1
        assert "REBOUND" in out
        assert "'math'" in out

    def test_the_rebound_is_caught_even_confined_to_a_lambda_and_class_body(
        self, mod, monkeypatch, capsys, worktrees
    ):
        base, head = worktrees
        # Same rebind, but with the ordinary-function read removed: only the
        # class-body assignment and the lambda still read the name.
        core = (
            "import cmath as math\n\nSCALE = 2\n\n\n"
            "def compute(x):\n    return x * SCALE\n\n\n"
            "class Thing:\n    stamp = math.pi\n\n\n"
            "handler = lambda: math.pi\n"
        )
        _split(head, core)

        code, out = _run(mod, monkeypatch, capsys, base, head)

        assert code == 1
        assert "REBOUND" in out
