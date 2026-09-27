"""Tests for scripts/refactor/audit_patches.py.

audit_patches resolves patch targets at runtime by importing the package
from --repo, so each test builds a real importable package under tmp_path
(no git needed — the tool does not read git at all) and adds tmp_path to
sys.path is unnecessary: the tool inserts --repo onto PYTHONPATH for the
subprocess it spawns to do the import.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import load_module, write

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def mod():
    return load_module("audit_patches")


def _layout_reachable(repo: Path) -> None:
    """reader.py defines AND uses read_value: the patch bites."""
    write(repo / "pkg" / "__init__.py", "")
    write(repo / "pkg" / "mod" / "__init__.py", '"""facade"""\n')
    write(
        repo / "pkg" / "mod" / "reader.py",
        "def read_value():\n    return 1\n\n\ndef use_it():\n    return read_value() + 1\n",
    )
    write(
        repo / "tests_dir" / "test_a.py",
        "from unittest.mock import patch\n\n\n"
        "def test_use_it():\n"
        '    with patch("pkg.mod.reader.read_value", return_value=99):\n'
        "        from pkg.mod.reader import use_it\n\n"
        "        assert use_it() == 100\n",
    )


def _layout_moved(repo: Path) -> None:
    """use_it() moves to user.py, which imports read_value into ITS OWN globals:
    the patch on reader.read_value no longer has any reader that bypasses it
    only through reader — every reader is now user.py."""
    write(repo / "pkg" / "__init__.py", "")
    write(repo / "pkg" / "mod" / "__init__.py", '"""facade"""\n')
    write(repo / "pkg" / "mod" / "reader.py", "def read_value():\n    return 1\n")
    write(
        repo / "pkg" / "mod" / "user.py",
        "from pkg.mod.reader import read_value\n\n\ndef use_it():\n    return read_value() + 1\n",
    )
    write(
        repo / "tests_dir" / "test_a.py",
        "from unittest.mock import patch\n\n\n"
        "def test_use_it():\n"
        '    with patch("pkg.mod.reader.read_value", return_value=99):\n'
        "        pass\n",
    )


def _run(mod, monkeypatch, capsys, repo: Path, *extra: str):
    argv = [
        "audit_patches.py",
        "--repo",
        str(repo),
        "--module",
        "pkg.mod",
        "--scan",
        "tests_dir",
        "pkg",
        *extra,
    ]
    monkeypatch.setattr("sys.argv", argv)
    code = mod.main()
    return code, capsys.readouterr().out


class TestPatchThatReachesItsReader:
    def test_a_patch_that_reaches_its_reader_passes(
        self, mod, monkeypatch, capsys, tmp_path
    ):
        _layout_reachable(tmp_path)

        code, out = _run(mod, monkeypatch, capsys, tmp_path)

        assert code == 0
        assert "'OK': 1" in out
        assert "INERT" not in out
        assert "FLAG" not in out


class TestPatchGoesInertAfterAMove:
    def test_after_moving_the_reader_the_same_patch_is_reported_inert(
        self, mod, monkeypatch, capsys, tmp_path
    ):
        _layout_moved(tmp_path)

        code, out = _run(mod, monkeypatch, capsys, tmp_path)

        # audit_patches is an audit, not a gate: it always exits 0 without
        # --compare. The INERT verdict is what the test proves.
        assert code == 0
        assert "INERT" in out
        assert "pkg.mod.user" in out


class TestCompareCatchesTheRegression:
    def test_compare_reports_the_same_patch_getting_worse_after_the_move(
        self, mod, monkeypatch, capsys, tmp_path
    ):
        _layout_reachable(tmp_path)
        baseline = tmp_path / "baseline.json"
        _run(mod, monkeypatch, capsys, tmp_path, "--json", str(baseline))
        assert json.loads(baseline.read_text())["refs"], "baseline captured no refs"

        for p in (tmp_path / "pkg" / "mod").glob("*.py"):
            if p.name != "__init__.py":
                p.unlink()
        _layout_moved(tmp_path)

        code, out = _run(mod, monkeypatch, capsys, tmp_path, "--compare", str(baseline))

        assert code == 1
        assert "WORSE" in out
        assert "INERT" in out
