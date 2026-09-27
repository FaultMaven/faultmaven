"""Tests for scripts/refactor/poison_plugin.py.

Run as a real subprocess pytest invocation (the plugin only does anything
inside pytest's own fixture machinery), against a synthetic target module —
never anything from faultmaven. Both directions are required: a positive
control (the poisoned target IS reached -> that run must fail) and a
negative control (it is not reached -> that run must pass). A poison check
that cannot be observed to fail proves nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import SCRIPTS_DIR, write

pytestmark = pytest.mark.unit

TARGET_MODULE = "def bomb():\n    return 'boom'\n\n\ndef safe():\n    return 'safe'\n"


def _run(
    tmp_path: Path, test_filename: str, test_src: str
) -> subprocess.CompletedProcess:
    write(tmp_path / "target_mod.py", TARGET_MODULE)
    write(tmp_path / test_filename, test_src)
    env = {
        **os.environ,
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{SCRIPTS_DIR}",
        "POISON_TARGET": "target_mod.bomb",
        "POISON_KIND": "sync",
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "poison_plugin", "-q", test_filename],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestReachingThePoisonFails:
    def test_reaching_the_poisoned_target_fails_at_teardown(self, tmp_path):
        result = _run(
            tmp_path,
            "test_reach.py",
            "import target_mod\n\n\ndef test_reaches():\n    assert target_mod.bomb() == 'boom'\n",
        )

        assert result.returncode != 0, result.stdout + result.stderr
        assert "POISON reached: target_mod.bomb" in result.stdout


class TestNotReachingThePoisonPasses:
    def test_not_reaching_the_poisoned_target_passes(self, tmp_path):
        result = _run(
            tmp_path,
            "test_not_reach.py",
            "import target_mod\n\n\ndef test_not_reaches():\n    assert target_mod.safe() == 'safe'\n",
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
