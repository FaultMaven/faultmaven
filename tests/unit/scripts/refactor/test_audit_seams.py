"""Tests for scripts/refactor/audit_seams.py, holderize.py and route_table.py.

audit_seams: a moved member leaves references behind in tests; some fail
loudly (a missing attribute) and some pass while exercising nothing (a stub
set on an attribute nothing reads). Each test builds the post-extraction state
by hand and asserts which finding the tool reports and what --rewrite does.

holderize: the shared-dependency-holder rewrite (E0).

route_table: only the comparison is tested here; building a real app is the
tool's own job and needs the whole package.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import SCRIPTS_DIR, load_module, write

pytestmark = pytest.mark.unit


def _run(python_exe, tool, *args):
    return subprocess.run(
        [python_exe, str(SCRIPTS_DIR / tool), *map(str, args)],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def extracted(tmp_path: Path):
    """An owner whose ``_fetch`` became a function and ``_plan`` a collaborator method."""
    write(tmp_path / "pkg" / "__init__.py", "")
    write(
        tmp_path / "pkg" / "engine.py",
        "class Engine:\n    def __init__(self, repo):\n        self.repo = repo\n",
    )
    write(
        tmp_path / "pkg" / "fetching.py",
        "def _fetch(repo, y):\n    return repo.get(y)\n",
    )
    write(
        tmp_path / "pkg" / "planning.py",
        "class Planner:\n    def plan(self, y):\n        return y\n",
    )
    spec = {
        "source": "pkg/engine.py",
        "class": "Engine",
        "groups": [
            {"module": "pkg/fetching.py", "kind": "functions", "members": ["_fetch"]},
            {
                "module": "pkg/planning.py",
                "kind": "collaborator",
                "class": "Planner",
                "attr": "planner",
                "members": ["_plan"],
            },
        ],
    }
    report = {
        "homes": {
            "_fetch": "pkg/fetching.py",
            "_plan": "pkg/planning.py",
            "__init__": None,
        },
        "deps": {"_fetch": ["repo"]},
        "collab_attr": {"_plan": "planner"},
        "collab_class": {"_plan": "Planner"},
        "rename": {"_plan": "plan"},
        "collab_deps": {"planner": []},
        "unread": [],
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "report.json").write_text(json.dumps(report))
    return tmp_path


def _audit(python_exe, root: Path, *extra):
    return _run(
        python_exe,
        "audit_seams.py",
        "--repo",
        root,
        "--spec",
        root / "spec.json",
        "--report",
        root / "report.json",
        "--paths",
        "tests",
        *extra,
    )


class TestAuditSeams:
    def test_a_clean_tree_reports_clean(self, extracted, python_exe):
        write(
            extracted / "tests" / "test_ok.py",
            "from pkg.fetching import _fetch\n\n\ndef test():\n    _fetch(None, 1)\n",
        )
        r = _audit(python_exe, extracted)
        assert r.returncode == 0 and "RESULT: CLEAN" in r.stdout

    def test_a_stale_call_is_rewritten_with_its_dependency_argument(
        self, extracted, python_exe
    ):
        t = extracted / "tests" / "test_x.py"
        write(
            t,
            "import pytest\n\n\ndef test(engine):\n    assert engine._fetch(1) == 2\n",
        )
        assert "STALE-ATTR" in _audit(python_exe, extracted).stdout

        _audit(python_exe, extracted, "--rewrite")

        text = t.read_text()
        assert "_fetch(engine.repo, 1)" in text
        assert "from pkg.fetching import _fetch" in text
        assert _audit(python_exe, extracted).returncode == 0

    def test_a_collaborator_member_is_reached_through_its_owner_attribute(
        self, extracted, python_exe
    ):
        t = extracted / "tests" / "test_y.py"
        write(
            t,
            "from unittest.mock import patch\n\n\ndef test(engine):\n"
            '    with patch.object(engine, "_plan", return_value=1):\n'
            "        engine._plan(3)\n",
        )
        _audit(python_exe, extracted, "--rewrite")
        text = t.read_text()
        assert 'patch.object(engine.planner, "plan"' in text
        assert "engine.planner.plan(3)" in text

    def test_a_stub_on_a_moved_function_is_vacuous_and_left_for_a_human(
        self, extracted, python_exe
    ):
        t = extracted / "tests" / "test_z.py"
        write(t, "def test(engine):\n    engine._fetch = lambda *a: 1\n")
        r = _audit(python_exe, extracted, "--rewrite")
        assert "VACUOUS-SET" in r.stdout
        assert (
            "engine._fetch = lambda" in t.read_text()
        ), "a function stub needs a human"

    def test_a_stub_on_a_collaborator_member_moves_onto_the_shared_instance(
        self, extracted, python_exe
    ):
        t = extracted / "tests" / "test_w.py"
        write(t, "def test(engine):\n    engine._plan = lambda *a: 1\n")
        _audit(python_exe, extracted, "--rewrite")
        assert "engine.planner.plan = lambda" in t.read_text()

    def test_holder_mode_rewrites_a_flat_dependency_and_seeds_new_built_owners(
        self, extracted, python_exe
    ):
        t = extracted / "tests" / "test_h.py"
        write(
            t,
            "from pkg.engine import Engine\n\n\ndef test():\n"
            "    engine = Engine.__new__(Engine)\n"
            "    engine.repo = 1\n"
            "    other.repo = 2\n",
        )
        _audit(
            python_exe,
            extracted,
            "--rewrite",
            "--holder",
            "deps",
            "--holder-fields",
            "repo",
            "--holder-class",
            "pkg.deps:Deps",
        )
        text = t.read_text()
        assert "engine.deps = Deps()" in text
        assert "engine.deps.repo = 1" in text
        assert "from pkg.deps import Deps" in text
        # a receiver not named like an engine is reported, never rewritten
        assert "other.repo = 2" in text
        r = _audit(python_exe, extracted, "--holder", "deps", "--holder-fields", "repo")
        assert "HOLDER-FLAT" in r.stdout and "other.repo" in r.stdout


class TestHolderize:
    OWNER = (
        "from typing import Any\n\n\n"
        "class Engine:\n"
        "    def __init__(self, repo: Any, llm=None):\n"
        "        self.repo = repo\n"
        "        self.llm = llm\n"
        "        self._locks = {}\n\n"
        "    def run(self, y):\n"
        "        return self.repo.get(y), self._locks\n"
    )

    def _holderize(self, python_exe, root, *extra):
        return _run(
            python_exe,
            "holderize.py",
            "--repo",
            root,
            "--source",
            "pkg/engine.py",
            "--class",
            "Engine",
            "--module",
            "pkg/deps.py",
            "--holder-class",
            "Deps",
            "--state",
            "_locks",
            *extra,
        )

    def test_dependencies_move_into_one_holder_and_reads_go_through_it(
        self, tmp_path, python_exe
    ):
        write(tmp_path / "pkg" / "engine.py", self.OWNER)
        r = self._holderize(python_exe, tmp_path, "--apply")
        assert r.returncode == 0, r.stdout + r.stderr
        owner = (tmp_path / "pkg" / "engine.py").read_text()
        deps = (tmp_path / "pkg" / "deps.py").read_text()
        assert (
            "self.deps = Deps(" in owner
            and "repo=repo," in owner
            and "llm=llm," in owner
        )
        assert "self.deps.repo.get(y)" in owner
        assert (
            "self._locks = {}" in owner and "self._locks" in owner.split("def run")[1]
        )
        assert "class Deps:" in deps and "repo: Any = None" in deps

    def test_rebinding_a_dependency_after_construction_is_refused(
        self, tmp_path, python_exe
    ):
        write(
            tmp_path / "pkg" / "engine.py",
            self.OWNER + "\n    def swap(self, r):\n        self.repo = r\n",
        )
        r = self._holderize(python_exe, tmp_path)
        assert r.returncode == 1 and "must not be rebound" in r.stdout


class TestRouteTableComparison:
    ROW = {
        "kind": "APIRoute",
        "path": "/a",
        "name": "a",
        "methods": ["GET"],
        "module": "m1",
    }

    def _main(self, monkeypatch, base, head):
        mod = load_module("route_table")
        tables = iter([base, head])
        monkeypatch.setattr(mod, "dump", lambda tree, py: next(tables))
        monkeypatch.setattr(
            sys, "argv", ["route_table.py", "--base", "b", "--head", "h"]
        )
        return mod.main()

    def test_only_a_changed_defining_module_is_identical(self, monkeypatch):
        moved = dict(self.ROW, module="m2")
        assert self._main(monkeypatch, [self.ROW], [moved]) == 0

    def test_a_reorder_differs(self, monkeypatch):
        other = dict(self.ROW, path="/b", name="b")
        assert self._main(monkeypatch, [self.ROW, other], [other, self.ROW]) == 1

    def test_a_changed_attribute_differs(self, monkeypatch):
        assert (
            self._main(monkeypatch, [self.ROW], [dict(self.ROW, methods=["POST"])]) == 1
        )


class TestAmbiguousReceivers:
    def test_a_name_another_class_still_defines_is_never_rewritten(
        self, extracted, python_exe
    ):
        # A sibling class (say, the other repository) still has its own _fetch.
        write(
            extracted / "tests" / "sibling.py",
            "class Sibling:\n    def _fetch(self, y):\n        return y\n",
        )
        t = extracted / "tests" / "test_s.py"
        write(t, "def test(repo):\n    assert repo._fetch(1)\n")
        r = _audit(python_exe, extracted, "--rewrite")
        assert "AMBIGUOUS" in r.stdout
        assert "repo._fetch(1)" in t.read_text()


class TestModuleAliases:
    def test_a_member_reached_through_its_new_home_module_is_clean(
        self, extracted, python_exe
    ):
        write(
            extracted / "tests" / "test_m.py",
            "from pkg import fetching as f\n\n\ndef test():\n    assert f._fetch(None, 1)\n",
        )
        assert _audit(python_exe, extracted).returncode == 0
