"""Tests for scripts/refactor/extract_members.py and its independent checker verify_extract.py.

Each test writes a small owner class into a throwaway tree, extracts some of
its methods with the codemod (run as a subprocess, the way a lane runs it),
and asserts on the text it produced, on the errors it refused with, and on
whether verify_extract.py PASSes or FAILs the result. The mutation tests plant
a defect in a correct extraction and require verify_extract.py to fail on it:
a checker that has never been seen to fail proves nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import SCRIPTS_DIR, write

pytestmark = pytest.mark.unit

OWNER = '''"""Owner module."""

import logging

logger = logging.getLogger(__name__)

LIMIT = 3


class Engine:
    TOOL_MAX = 10

    def __init__(self, repo, llm):
        self.repo = repo
        self.llm = llm
        self._inflight = {}

    def run(self, x):
        y = self._clean(x)
        return self._fetch(y) + self._score(y)

    @staticmethod
    def _clean(x):
        return x.strip()

    def _score(self, y):
        return len(y) * LIMIT

    def _fetch(self, y):
        logger.info("fetch %s", y)
        return self.repo.get(y)

    def _remember(self, key):
        self._inflight[key] = True
        return self._fetch(key)
'''

SRC = "pkg/engine.py"


def _tree(base: Path, text: str = OWNER) -> Path:
    write(base / SRC, text)
    write(base / "pkg" / "__init__.py", '"""pkg."""\n')
    return base


def _spec(tmp_path: Path, groups, **extra) -> Path:
    spec = {"source": SRC, "class": "Engine", "groups": groups, **extra}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec))
    return p


def _run(python_exe, tool, *args, cwd=None):
    return subprocess.run(
        [python_exe, str(SCRIPTS_DIR / tool), *map(str, args)],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _extract(python_exe, root: Path, spec: Path, apply=True):
    args = ["--repo", root, "--spec", spec] + (["--apply"] if apply else [])
    return _run(python_exe, "extract_members.py", *args)


def _verify(python_exe, base: Path, head: Path, spec: Path):
    return _run(
        python_exe, "verify_extract.py", "--base", base, "--head", head, "--spec", spec
    )


@pytest.fixture
def base_and_head(tmp_path: Path):
    base = _tree(tmp_path / "base")
    head = tmp_path / "head"
    shutil.copytree(base, head)
    return base, head


class TestFunctions:
    def test_a_dependency_becomes_a_leading_parameter_passed_per_call(
        self, tmp_path, base_and_head, python_exe
    ):
        base, head = base_and_head
        spec = _spec(
            tmp_path,
            [{"module": "pkg/fetching.py", "kind": "functions", "members": ["_fetch"]}],
        )

        r = _extract(python_exe, head, spec)

        assert r.returncode == 0, r.stdout + r.stderr
        fetching = (head / "pkg" / "fetching.py").read_text()
        owner = (head / SRC).read_text()
        assert "def _fetch(repo, y):" in fetching
        assert "return repo.get(y)" in fetching
        assert "logger = logging.getLogger(__name__)" in fetching
        assert "_fetch(self.repo, y)" in owner
        assert "def _fetch" not in owner
        assert _verify(python_exe, base, head, spec).returncode == 0

    def test_a_call_back_into_the_owner_is_refused(
        self, tmp_path, base_and_head, python_exe
    ):
        base, head = base_and_head
        # run() calls _clean/_score, which stay on the owner: the boundary is wrong.
        spec = _spec(
            tmp_path,
            [{"module": "pkg/running.py", "kind": "functions", "members": ["run"]}],
        )

        r = _extract(python_exe, head, spec)

        assert r.returncode == 1
        assert "boundary is wrong" in r.stdout
        assert not (head / "pkg" / "running.py").exists(), "an error must write nothing"

    def test_a_module_level_name_the_moved_code_reads_needs_a_home(
        self, tmp_path, base_and_head, python_exe
    ):
        base, head = base_and_head
        homeless = _spec(
            tmp_path,
            [{"module": "pkg/scoring.py", "kind": "functions", "members": ["_score"]}],
        )
        r = _extract(python_exe, head, homeless, apply=False)
        assert r.returncode == 1 and "'LIMIT'" in r.stdout

        homed = _spec(
            tmp_path,
            [
                {
                    "module": "pkg/scoring.py",
                    "kind": "functions",
                    "members": ["_score"],
                    "module_names": ["LIMIT"],
                }
            ],
        )
        r = _extract(python_exe, head, homed)
        assert r.returncode == 0, r.stdout
        assert "LIMIT = 3" in (head / "pkg" / "scoring.py").read_text()
        assert "LIMIT = 3" not in (head / SRC).read_text()
        assert _verify(python_exe, base, head, homed).returncode == 0

    def test_a_dependency_that_would_shadow_a_local_is_refused(
        self, tmp_path, python_exe
    ):
        text = OWNER.replace(
            '        logger.info("fetch %s", y)\n        return self.repo.get(y)',
            "        repo = y\n        return self.repo.get(repo)",
        )
        head = _tree(tmp_path / "head", text)
        spec = _spec(
            tmp_path,
            [{"module": "pkg/fetching.py", "kind": "functions", "members": ["_fetch"]}],
        )

        r = _extract(python_exe, head, spec, apply=False)

        assert r.returncode == 1 and "would shadow" in r.stdout

    def test_a_function_cannot_rebind_instance_state(self, tmp_path, python_exe):
        # Mutating a passed object (``self._inflight[k] = v``) is fine: the
        # function receives the owner's one dict per call. REBINDING an
        # attribute is not something a function can do at all.
        text = OWNER.replace(
            "        self._inflight[key] = True\n",
            "        self._last = key\n",
        )
        head = _tree(tmp_path / "head", text)
        spec = _spec(
            tmp_path,
            [
                {
                    "module": "pkg/memo.py",
                    "kind": "functions",
                    "members": ["_remember", "_fetch"],
                }
            ],
        )
        r = _extract(python_exe, head, spec, apply=False)
        assert r.returncode == 1 and "writes instance state" in r.stdout


class TestCollaborators:
    def _spec(self, tmp_path):
        return _spec(
            tmp_path,
            [
                {
                    "module": "pkg/memory.py",
                    "kind": "collaborator",
                    "class": "Memory",
                    "attr": "memory",
                    "members": ["_remember", "_fetch"],
                    "owned_state": ["_inflight"],
                    "state_init": {"_inflight": "{}"},
                }
            ],
        )

    def test_the_owner_builds_it_and_an_externally_called_member_goes_public(
        self, tmp_path, base_and_head, python_exe
    ):
        base, head = base_and_head
        spec = self._spec(tmp_path)

        r = _extract(python_exe, head, spec)

        assert r.returncode == 0, r.stdout + r.stderr
        memory = (head / "pkg" / "memory.py").read_text()
        owner = (head / SRC).read_text()
        assert "class Memory:" in memory
        assert "def __init__(self, *, repo) -> None:" in memory
        assert "self._inflight = {}" in memory
        # run() (on the owner) calls _fetch, so it becomes public; nothing
        # outside the collaborator calls _remember, so it keeps its name.
        assert "def fetch(self, y):" in memory
        assert "def _remember(self, key):" in memory
        assert "self.memory = Memory(repo=self.repo)" in owner
        assert "self.memory.fetch(y)" in owner
        assert "self._inflight = {}" not in owner, "owned state leaves the owner"
        assert _verify(python_exe, base, head, spec).returncode == 0

    def test_a_cycle_between_collaborators_is_refused(self, tmp_path, python_exe):
        text = OWNER.replace(
            "    def _score(self, y):\n        return len(y) * LIMIT",
            "    def _score(self, y):\n        return self._fetch(y)",
        ).replace(
            '        logger.info("fetch %s", y)\n        return self.repo.get(y)',
            "        return self._score(y) and self.repo.get(y)",
        )
        head = _tree(tmp_path / "head", text)
        spec = _spec(
            tmp_path,
            [
                {
                    "module": "pkg/a.py",
                    "kind": "collaborator",
                    "class": "A",
                    "attr": "a",
                    "members": ["_score"],
                },
                {
                    "module": "pkg/b.py",
                    "kind": "collaborator",
                    "class": "B",
                    "attr": "b",
                    "members": ["_fetch"],
                },
            ],
        )
        r = _extract(python_exe, head, spec, apply=False)
        assert r.returncode == 1 and "collaborator cycle" in r.stdout


class TestHolder:
    HELD = OWNER.replace(
        "        self.repo = repo\n        self.llm = llm\n",
        "        self.deps = Deps(repo=repo, llm=llm)\n",
    ).replace("self.repo.get(y)", "self.deps.repo.get(y)")

    def test_a_holder_field_is_passed_as_its_own_value(self, tmp_path, python_exe):
        base = _tree(tmp_path / "base", self.HELD)
        head = tmp_path / "head"
        shutil.copytree(base, head)
        spec = _spec(
            tmp_path,
            [{"module": "pkg/fetching.py", "kind": "functions", "members": ["_fetch"]}],
            holder={"attr": "deps"},
        )

        r = _extract(python_exe, head, spec)

        assert r.returncode == 0, r.stdout
        assert "def _fetch(repo, y):" in (head / "pkg" / "fetching.py").read_text()
        assert "_fetch(self.deps.repo, y)" in (head / SRC).read_text()
        assert _verify(python_exe, base, head, spec).returncode == 0


class TestVerifierBites:
    """Plant one defect in a correct extraction; verify_extract.py must FAIL."""

    @pytest.fixture
    def extracted(self, tmp_path, base_and_head, python_exe):
        base, head = base_and_head
        spec = _spec(
            tmp_path,
            [
                {
                    "module": "pkg/fetching.py",
                    "kind": "functions",
                    "members": ["_fetch"],
                },
                {
                    "module": "pkg/scoring.py",
                    "kind": "functions",
                    "members": ["_score", "_clean"],
                    "module_names": ["LIMIT"],
                },
            ],
        )
        assert _extract(python_exe, head, spec).returncode == 0
        assert _verify(python_exe, base, head, spec).returncode == 0
        return base, head, spec

    def _mutate(self, path: Path, old: str, new: str):
        text = path.read_text()
        assert old in text, old
        path.write_text(text.replace(old, new, 1))

    def test_an_edited_body(self, extracted, python_exe):
        base, head, spec = extracted
        self._mutate(head / "pkg" / "scoring.py", "* LIMIT", "* LIMIT + 1")
        r = _verify(python_exe, base, head, spec)
        assert r.returncode == 1 and "_score" in r.stdout

    def test_a_wrong_dependency_argument(self, extracted, python_exe):
        base, head, spec = extracted
        self._mutate(head / SRC, "_fetch(self.repo, y)", "_fetch(self.llm, y)")
        r = _verify(python_exe, base, head, spec)
        assert r.returncode == 1 and "expected dependency args" in r.stdout

    def test_a_dropped_member(self, extracted, python_exe):
        base, head, spec = extracted
        self._mutate(head / "pkg" / "scoring.py", "def _clean(x):", "def _cleaned(x):")
        r = _verify(python_exe, base, head, spec)
        assert r.returncode == 1 and "_clean" in r.stdout

    def test_an_edited_owner_method(self, extracted, python_exe):
        base, head, spec = extracted
        self._mutate(head / SRC, "y = _clean(x)", "y = _clean(x[1:])")
        r = _verify(python_exe, base, head, spec)
        assert r.returncode == 1 and "run (owner)" in r.stdout
