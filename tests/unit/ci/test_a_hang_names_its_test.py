"""A hung test names itself in the log (#3078).

At ``faulthandler_timeout`` (pytest.ini) ``tests/hang_report.py`` writes one
block naming the xdist worker, the test's nodeid, the await chain of each task
on its event loop and every thread's stack: faulthandler alone names no test,
and for an async test shows only the event loop's ``select``. A report is one
write made holding the run's report lock, so each is read from its own block.
"""

import errno
import faulthandler
import fcntl
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import iniconfig
import pytest

from tests import hang_report
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
        # TMPDIR: the run's report lock file stays in tmp_path.
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(tmp_path),
        },
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


class _Reports:
    """A ``_Watch`` past its deadline at once, writing to a drained pipe."""

    def __init__(self, monkeypatch, tmp_path):
        self.read_end, self.write_end = os.pipe()
        self.writes: list[bytes] = []
        self.lock_path = tmp_path / "faultmaven-hang-report-t3085.lock"
        self._drained: list[bytes] = []
        self._each = threading.Semaphore(0)
        real_write = os.write

        def record(fd, data):
            if fd != self.write_end:
                return real_write(fd, data)
            self.writes.append(bytes(data))
            n = real_write(fd, data)
            self._each.release()
            return n

        monkeypatch.setattr(os, "write", record)
        # This run's own fallback dump stays armed.
        monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", lambda: None)
        monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "t3085")  # a private lock
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        self._open = True
        self.watch = _Watch(0, self.write_end)

    def _drain(self):
        while chunk := os.read(self.read_end, 65536):
            self._drained.append(chunk)

    def report(self, nodeid):
        self.watch.arm(nodeid)
        assert self._each.acquire(timeout=60), "no report"  # a bound, not a measurement

    def close(self) -> str:
        if self._open:
            self._open = False
            self.watch.stop()
            os.close(self.write_end)
            self._reader.join()
            os.close(self.read_end)
        # Stopped before monkeypatch restores what the thread calls.
        assert not self.watch._thread.is_alive()
        return b"".join(self._drained).decode()


@pytest.fixture
def reports(monkeypatch, tmp_path):
    reports = _Reports(monkeypatch, tmp_path)
    yield reports
    reports.close()


def test_a_report_is_one_write_holding_its_thread_stacks(reports):
    reports.report("fake.py::test_fake")
    out = reports.close()

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    assert len(reports.writes) == 1 and out == reports.writes[0].decode()
    assert out.startswith(f"\n=== hung test [{worker}] fake.py::test_fake: ")
    assert out.endswith(f"=== end hung test [{worker}] fake.py::test_fake ===\n")
    # This thread waits in report(): its frame is in the stacks inside the block.
    assert re.search(
        rf'File "{re.escape(__file__)}", line \d+ in '
        r"test_a_report_is_one_write_holding_its_thread_stacks\n",
        out,
    ), out


def test_a_report_waits_for_another_workers_report(reports, monkeypatch):
    held = os.open(reports.lock_path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)  # another worker is writing its report
    polls = []

    def poll():
        polls.append(len(reports.writes))
        fcntl.flock(held, fcntl.LOCK_UN)  # that report is done

    monkeypatch.setattr(hang_report, "_lock_poll", poll)
    try:
        reports.report("fake.py::test_fake")
    finally:
        os.close(held)
    assert polls == [0]  # it waited, and wrote nothing while it waited
    assert "written unlocked" not in reports.close()


def test_a_report_held_back_by_a_hung_peer_is_written_unlocked(reports, monkeypatch):
    held = os.open(reports.lock_path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)  # held by a peer that never lets go
    monkeypatch.setattr(hang_report, "_LOCK_POLLS", 0)
    try:
        reports.report("fake.py::test_fake")
    finally:
        os.close(held)
    first_line = reports.close().splitlines()[1]
    assert first_line.endswith("(written unlocked: lock still busy after 0 waits) ===")


def test_a_report_survives_a_temporary_file_failure(reports, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(tempfile, "TemporaryFile", refuse)
    reports.report("fake.py::test_first")
    reports.report("fake.py::test_second")  # the watch thread lived on
    out = reports.close()

    for nodeid in ("fake.py::test_first", "fake.py::test_second"):
        assert f"] {nodeid}: running past 0s ===\n" in out
    assert out.count("(thread stacks unreadable from a temporary file: ") == 2
    # faulthandler's own dump follows each block.
    assert out.count("(most recent call first):") >= 2


def test_a_full_non_blocking_stderr_waits_rather_than_raising(monkeypatch):
    read_end, write_end = os.pipe()
    os.set_blocking(write_end, False)
    data = b"x" * (1 << 21)  # past any pipe's buffer: a write runs into EAGAIN
    drained: list[bytes] = []

    def drain():
        while chunk := os.read(read_end, 65536):
            drained.append(chunk)

    reader = threading.Thread(target=drain)
    real_select = select.select

    def wait_writable(*args):
        if reader.ident is None:  # drain only once the write has waited
            reader.start()
        return real_select(*args)

    monkeypatch.setattr(select, "select", wait_writable)
    try:
        hang_report._write_all(write_end, data)
    finally:
        os.close(write_end)
        if reader.ident is not None:
            reader.join()
        os.close(read_end)
    assert reader.ident is not None and b"".join(drained) == data
