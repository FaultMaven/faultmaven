"""Tests for scripts/refactor/{phase_flow,extract_phase,verify_inline}.py.

A function-body split cuts inside one method, so a wrong cut changes behaviour
without changing any statement: an input that is only sometimes bound, an
output left unbound on one path, a read that now resolves to a global. These
tests pin each of those failure shapes (several are the counterexamples the
#1707 wave-3 design review found) and prove the verifier rejects them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .conftest import SCRIPTS_DIR, write

pytestmark = pytest.mark.unit

OWNER = """class Engine:
    def __init__(self, repo):
        self.repo = repo

    async def run(self, case, flag):
        total = 0
        seen = []
        for item in case:
            seen.append(item)
        total += len(seen)
        if flag:
            label = "yes"
        else:
            label = "no"
        stored = await self.repo.save(label, total)
        if stored is None:
            message = "nothing"
            return {"message": message, "total": total}
        reply = f"{label}:{stored}"
        return {"message": reply, "total": total}
"""


def _run(python_exe, tool, *args):
    return subprocess.run(
        [python_exe, str(SCRIPTS_DIR / tool), *map(str, args)],
        capture_output=True,
        text=True,
    )


def _flow(python_exe, path, *phases):
    return _run(
        python_exe,
        "phase_flow.py",
        "--file",
        path,
        "--func",
        "Engine.run",
        *(x for p in phases for x in ("--phase", p)),
    )


class TestPhaseFlow:
    def test_a_straight_phase_reports_inputs_and_live_outputs(
        self, tmp_path, python_exe
    ):
        p = tmp_path / "e.py"
        write(p, OWNER)
        r = _flow(python_exe, p, "count:8-10")
        assert r.returncode == 0, r.stdout
        assert "shape=STRAIGHT" in r.stdout
        assert "inputs  (3): case, seen, total" in r.stdout
        assert "outputs (1): total" in r.stdout

    def test_a_terminating_branch_body_is_a_tail(self, tmp_path, python_exe):
        p = tmp_path / "e.py"
        write(p, OWNER)
        r = _flow(python_exe, p, "nothing:17-18")
        assert r.returncode == 0, r.stdout
        assert "shape=TAIL" in r.stdout

    def test_a_block_that_returns_but_can_fall_through_is_refused(
        self, tmp_path, python_exe
    ):
        p = tmp_path / "e.py"
        write(p, OWNER)
        r = _flow(python_exe, p, "mixed:16-18")
        assert r.returncode == 1
        assert "not a terminating suffix" in r.stdout

    def test_an_augmented_assignment_reads_its_target(self, tmp_path, python_exe):
        p = tmp_path / "e.py"
        write(p, OWNER)
        r = _flow(python_exe, p, "aug:10-10")
        assert "inputs  (2): seen, total" in r.stdout

    def test_a_conditionally_rebound_output_becomes_an_input(
        self, tmp_path, python_exe
    ):
        p = tmp_path / "e.py"
        write(
            p,
            "class Engine:\n    async def run(self, case, flag):\n        y = 1\n"
            "        if flag:\n            y = 2\n        return y\n",
        )
        r = _flow(python_exe, p, "maybe:4-5")
        assert r.returncode == 0, r.stdout
        assert "inputs  (2): flag, y" in r.stdout

    def test_a_read_of_a_name_bound_only_later_is_refused(self, tmp_path, python_exe):
        p = tmp_path / "e.py"
        write(
            p,
            "class Engine:\n    async def run(self, case, flag):\n        q = later\n"
            "        later = 5\n        return q\n",
        )
        r = _flow(python_exe, p, "early:3-3")
        assert r.returncode == 1 and "bound only later" in r.stdout

    def test_a_name_an_except_handler_reads_stays_in_the_owner(
        self, tmp_path, python_exe
    ):
        p = tmp_path / "e.py"
        write(
            p,
            "class Engine:\n    async def run(self, case, flag):\n        done = False\n"
            "        try:\n            done = True\n            x = 1\n        except Exception:\n"
            "            print(done)\n        return 0\n",
        )
        r = _flow(python_exe, p, "flag:5-6")
        assert r.returncode == 1 and "except/finally handler reads" in r.stdout

    def test_overlapping_phases_are_refused(self, tmp_path, python_exe):
        p = tmp_path / "e.py"
        write(p, OWNER)
        r = _flow(python_exe, p, "a:6-10", "b:10-10")
        assert r.returncode == 1 and "OVERLAP" in r.stdout


class TestExtractAndVerify:
    PHASES = ("_count:8-10", "_save:15-15", "_reply:19-20")

    def _extract(self, python_exe, tmp_path):
        base = tmp_path / "base.py"
        head = tmp_path / "head.py"
        write(base, OWNER)
        write(head, OWNER)
        args = ["--file", head, "--func", "Engine.run", "--apply"]
        for ph in self.PHASES:
            args += ["--phase", ph]
        r = _run(python_exe, "extract_phase.py", *args)
        assert r.returncode == 0, r.stdout + r.stderr
        return base, head

    def _verify(self, python_exe, base, head):
        return _run(
            python_exe,
            "verify_inline.py",
            "--base",
            base,
            "--head",
            head,
            "--func",
            "Engine.run",
        )

    def _mutate(self, head: Path, *pairs: tuple[str, str]):
        text = head.read_text()
        for old, new in pairs:
            assert old in text, old
            text = text.replace(old, new, 1)
        head.write_text(text)

    def test_straight_and_tail_phases_re_inline_to_the_base(self, tmp_path, python_exe):
        base, head = self._extract(python_exe, tmp_path)
        text = head.read_text()
        assert "total = self._count(case=case, seen=seen, total=total)" in text
        assert "stored = await self._save(label=label, total=total)" in text
        assert "return self._reply(label=label, stored=stored, total=total)" in text
        assert "async def _save(self, *, label, total):" in text
        r = self._verify(python_exe, base, head)
        assert r.returncode == 0, r.stdout
        assert "PASS: 3 phase(s)" in r.stdout

    def test_extract_refuses_what_phase_flow_refuses(self, tmp_path, python_exe):
        head = tmp_path / "head.py"
        write(head, OWNER)
        r = _run(
            python_exe,
            "extract_phase.py",
            "--file",
            head,
            "--func",
            "Engine.run",
            "--phase",
            "_mixed:16-18",
            "--apply",
        )
        assert r.returncode == 1 and "REFUSED" in r.stdout
        assert head.read_text() == OWNER

    @pytest.mark.parametrize(
        ("pairs", "message"),
        [
            pytest.param(
                [("total += len(seen)", "total += len(seen) + 1")],
                "re-inlined method differs from the base",
                id="edited-body",
            ),
            pytest.param(
                [("total = self._count(", "self._count(")],
                "_count: a STRAIGHT phase returns early",
                id="dropped-output",
            ),
            pytest.param(
                [
                    (
                        "self._count(case=case, seen=seen, total=total)",
                        "self._count(case=case, seen=seen)",
                    ),
                    (
                        "def _count(self, *, case, seen, total):",
                        "def _count(self, *, case, seen):",
                    ),
                ],
                "_count: reads ['total'] (base locals) before binding them",
                id="dropped-input",
            ),
            pytest.param(
                [("stored = await self._save(", "stored = self._save(")],
                "_save: async method called without await",
                id="missing-await",
            ),
            pytest.param(
                [("async def _save(", "def _save(")],
                "_save: sync method called with await",
                id="sync-method-awaited",
            ),
            pytest.param(
                [("return self._reply(", "self._reply(")],
                "_reply: a STRAIGHT phase returns early",
                id="tail-not-returned",
            ),
        ],
    )
    def test_a_broken_extraction_fails(self, tmp_path, python_exe, pairs, message):
        base, head = self._extract(python_exe, tmp_path)
        self._mutate(head, *pairs)
        r = self._verify(python_exe, base, head)
        assert r.returncode == 1, r.stdout
        assert message in r.stdout


class TestVerifierCatchesAstEqualRuntimeBreaks:
    """Re-inline equality alone passes these; the binding checks must not."""

    BASE = (
        "class K:\n    def f(self, a, c):\n        x = 0\n        y = 1\n        z = None\n"
        "        x += a\n        w = x + 1\n        if c:\n            y = 2\n            z = 3\n"
        "        print(y, z, w)\n        return w\n"
    )
    BAD = (
        "class K:\n    def f(self, a, c):\n        x = 0\n        y = 1\n        z = None\n"
        "        w = self._p1(a=a)\n        y, z = self._p2(c=c)\n        print(y, z, w)\n        return w\n\n"
        "    def _p1(self, *, a):\n        x += a\n        w = x + 1\n        return w\n\n"
        "    def _p2(self, *, c):\n        if c:\n            y = 2\n            z = 3\n        return y, z\n"
    )

    def test_augmented_read_and_conditional_output_are_both_caught(
        self, tmp_path, python_exe
    ):
        write(tmp_path / "base.py", self.BASE)
        write(tmp_path / "bad.py", self.BAD)
        r = _run(
            python_exe,
            "verify_inline.py",
            "--base",
            tmp_path / "base.py",
            "--head",
            tmp_path / "bad.py",
            "--func",
            "K.f",
        )
        assert r.returncode == 1
        assert "_p1: reads ['x']" in r.stdout
        assert "_p2: returns ['y', 'z']" in r.stdout
