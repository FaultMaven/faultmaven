"""A hung test names itself in the log (#3078).

``faulthandler_timeout`` (pytest.ini) dumps every thread's stack when a test
runs past it, and ``tests/hang_report.py`` writes, at the same timeout, one
block naming the xdist worker, the test's nodeid and, for an async test, the
stacks of its loop's tasks: faulthandler alone names no test, and for an async
test shows only the event loop's ``select``.
"""

import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

import iniconfig
import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

_BLOCK = re.compile(
    r"=== hung test \[(?P<worker>gw\d)\] (?P<nodeid>\S+): running past 1s ===\n"
    r"(?P<body>.*?)"
    r"=== end hung test \[(?P=worker)\] (?P=nodeid) ===\n",
    re.DOTALL,
)


def test_the_suite_sets_the_timeout():
    raw = iniconfig.IniConfig(REPO_ROOT / "pytest.ini")["pytest"].get(
        "faulthandler_timeout"
    )
    assert raw is not None and float(raw) > 0, "pytest.ini sets no faulthandler_timeout"


def test_each_hung_test_is_named_with_its_worker_and_task_stacks(tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\nfaulthandler_timeout = 1\n"
    )
    # Each waits forever; the run is killed once both blocks are read.
    (tmp_path / "test_sync.py").write_text(
        "import threading\n\n\ndef test_hangs_sync():\n    threading.Event().wait()\n"
    )
    (tmp_path / "test_async.py").write_text(
        "import asyncio\n\n\nasync def test_hangs_async():\n"
        "    await asyncio.Event().wait()\n"
    )
    command = [sys.executable, "-m", "pytest", "-p", "tests.hang_report"]
    command += ["-p", "no:cacheprovider", "-n", "2", "--dist", "loadfile"]
    with subprocess.Popen(
        command,
        cwd=tmp_path,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as run:
        # A bound on a broken plugin, not a measurement: kill the run if the
        # blocks never come, and fail below on what was read.
        guard = threading.Timer(120, os.killpg, (run.pid, signal.SIGKILL))
        guard.start()
        stderr = ""
        try:
            for line in run.stderr:
                stderr += line
                if len(_BLOCK.findall(stderr)) == 2:
                    break
        finally:
            guard.cancel()
            os.killpg(run.pid, signal.SIGKILL)
            run.wait()

    blocks = {m["nodeid"]: m for m in _BLOCK.finditer(stderr)}
    assert set(blocks) == {
        "test_sync.py::test_hangs_sync",
        "test_async.py::test_hangs_async",
    }, stderr
    assert {b["worker"] for b in blocks.values()} == {"gw0", "gw1"}
    assert "in test_hangs_async\n" in blocks["test_async.py::test_hangs_async"]["body"]
    assert "no asyncio loop reached" in blocks["test_sync.py::test_hangs_sync"]["body"]
