"""A hung test names itself in the log (#3078).

At ``faulthandler_timeout`` (pytest.ini) ``tests/hang_report.py`` writes one
block naming the xdist worker, the test's nodeid, the await chain of each task
on its event loop and every thread's stack: faulthandler alone names no test,
and for an async test shows only the event loop's ``select``. Each report is
read from its own block, as one write keeps it whole beside other workers'.
"""

import faulthandler
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

import iniconfig
import pytest

from tests.hang_report import _Watch

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

_SYNC_FRAME = 'test_sync.py", line 5 in test_hangs_sync'
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
    (tmp_path / "test_fixture.py").write_text(
        "import asyncio\n\nimport pytest\n\n\n@pytest.fixture\nasync def stuck():\n"
        "    await asyncio.Event().wait()\n    yield\n\n\n"
        "async def test_hangs_in_its_fixture(stuck):\n    pass\n"
    )
    command = [sys.executable, "-m", "pytest", "-p", "tests.hang_report"]
    command += ["-p", "no:cacheprovider", "-n", "3", "--dist", "loadfile"]
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
                if len(_BLOCK.findall(stderr)) == 3:
                    break
        finally:
            guard.cancel()
            os.killpg(run.pid, signal.SIGKILL)
            run.wait()

    blocks = {m["nodeid"]: m for m in _BLOCK.finditer(stderr)}
    assert set(blocks) == {
        "test_sync.py::test_hangs_sync",
        "test_async.py::test_hangs_async",
        "test_fixture.py::test_hangs_in_its_fixture",
    }, stderr
    assert {b["worker"] for b in blocks.values()} == {"gw0", "gw1", "gw2"}
    assert "in test_hangs_async\n" in blocks["test_async.py::test_hangs_async"]["body"]
    assert "in stuck\n" in blocks["test_fixture.py::test_hangs_in_its_fixture"]["body"]
    assert "no asyncio loop reached" in blocks["test_sync.py::test_hangs_sync"]["body"]
    assert _SYNC_FRAME in blocks["test_sync.py::test_hangs_sync"]["body"]


def test_a_report_is_one_write_holding_its_thread_stacks(monkeypatch):
    read_end, write_end = os.pipe()
    writes: list[bytes] = []
    drained: list[bytes] = []
    written = threading.Event()
    real_write = os.write

    def record(fd, data):
        if fd != write_end:
            return real_write(fd, data)
        writes.append(bytes(data))
        n = real_write(fd, data)
        written.set()
        return n

    def drain():
        while chunk := os.read(read_end, 65536):
            drained.append(chunk)

    monkeypatch.setattr(os, "write", record)
    # This run's own fallback dump stays armed.
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", lambda: None)
    reader = threading.Thread(target=drain)
    reader.start()
    watch = _Watch(0, write_end)
    try:
        watch.arm("fake.py::test_fake")  # past its deadline at once
        assert written.wait(60), "no report written"  # a bound, not a measurement
    finally:
        watch.stop()
        os.close(write_end)
        reader.join()
        os.close(read_end)

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    assert len(writes) == 1 and b"".join(drained) == writes[0]
    report = writes[0].decode()
    assert report.startswith(f"\n=== hung test [{worker}] fake.py::test_fake:")
    assert report.endswith(f"=== end hung test [{worker}] fake.py::test_fake ===\n")
    # This thread waits above: its frame is in the stacks inside the block.
    assert re.search(
        rf'File "{re.escape(__file__)}", line \d+ in '
        r"test_a_report_is_one_write_holding_its_thread_stacks\n",
        report,
    ), report
