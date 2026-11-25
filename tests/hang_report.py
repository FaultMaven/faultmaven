"""Name a test that hangs (#3078).

``faulthandler_timeout`` (pytest.ini) makes pytest dump every thread's stack
when a test runs past it, but the dump names no test, and for an ``async def``
test the main thread's stack ends in the event loop's ``select`` with no frame
of the test. At that timeout this plugin writes one labelled block to stderr:
the xdist worker, the test's nodeid, the await chain of each task on the open
asyncio loops of pytest-asyncio's runners, so a hang in an async test or in an
async fixture's setup shows where it waits, and every thread's stack, dumped by
the plugin itself (``faulthandler.dump_traceback``, holding the GIL).

pytest's own dump is pushed ``_FALLBACK_S`` later, for a hang that holds the
GIL and so stops this thread. At the same instant it killed xdist workers
silently: faulthandler's C watchdog reads every thread's frames without the
GIL, and this thread was running Python. Under ``faulthandler_exit_on_timeout``
the plugin stays off, so pytest's dump exits on time.

A report is one write: faulthandler writes a frame line in several small
writes, so the stacks go to a temporary file first and are read back into the
block. A pipe keeps one write of up to PIPE_BUF (4096 bytes on Linux) whole;
a longer report can still interleave with another worker's output.

``tests/conftest.py`` registers it (``pytest_plugins``); ``-p tests.hang_report``
loads it elsewhere, with the repository on ``sys.path``.
"""

from __future__ import annotations

import asyncio
import faulthandler
import gc
import io
import linecache
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

import pytest


@dataclass
class _Running:
    nodeid: str
    deadline: float


class _Watch:
    """One thread per process, armed for the running test."""

    def __init__(self, timeout: float, fd: int) -> None:
        self.timeout = timeout
        self.fd = fd
        self._cond = threading.Condition()
        self._running: Optional[_Running] = None
        self._loops: list[asyncio.AbstractEventLoop] = []
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="hang-report", daemon=True
        )
        self._thread.start()

    def arm(self, nodeid: str) -> None:
        with self._cond:
            self._running = _Running(nodeid, time.monotonic() + self.timeout)
            self._cond.notify()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._cond:
            self._loops = [kept for kept in self._loops if not kept.is_closed()]
            if loop not in self._loops:
                self._loops.append(loop)

    def disarm(self) -> None:
        with self._cond:
            self._running = None
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._running = None
            self._cond.notify()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopped:
                    if self._running is None:
                        self._cond.wait()
                        continue
                    left = self._running.deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._cond.wait(left)
                if self._stopped:
                    return
                running, self._running = self._running, None  # once per test
                loops = list(self._loops)
            block = _block(running, self.timeout, loops, _thread_stacks())
            report = block.encode("utf-8", "replace")
            while report:
                report = report[os.write(self.fd, report) :]
            faulthandler.cancel_dump_traceback_later()  # the fallback, now redundant


def _thread_stacks() -> str:
    with tempfile.TemporaryFile() as dump:
        faulthandler.dump_traceback(dump, all_threads=True)
        dump.seek(0)
        return dump.read().decode("utf-8", "replace")


def _block(
    running: _Running,
    timeout: float,
    loops: list[asyncio.AbstractEventLoop],
    stacks: str,
) -> str:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    out = io.StringIO()
    out.write(
        f"\n=== hung test [{worker}] {running.nodeid}: running past {timeout:g}s ===\n"
    )
    open_loops = [loop for loop in loops if not loop.is_closed()]
    if not open_loops:
        out.write(
            "(no asyncio loop reached; the thread stacks follow from faulthandler)\n"
        )
    for loop in open_loops:
        try:
            tasks = list(asyncio.all_tasks(loop))
        except Exception as exc:  # the loop is another thread's: report, never raise
            out.write(f"(tasks of {loop!r} unreadable: {exc!r})\n")
            continue
        if not tasks:
            out.write(f"(no task on {loop!r})\n")
        for task in tasks:
            try:
                _write_await_chain(task, out)
            except Exception as exc:
                out.write(f"(stack of {task!r} unreadable: {exc!r})\n")
    out.write(stacks)
    out.write(f"=== end hung test [{worker}] {running.nodeid} ===\n")
    return out.getvalue()


def _write_await_chain(task: asyncio.Task, out: io.StringIO) -> None:
    # Task.print_stack shows only the task's own coroutine; follow what it
    # awaits down to the frame that waits, as a traceback reads.
    out.write(f"{task!r} awaits (most recent call last):\n")
    awaited: object = task.get_coro()
    for _ in range(100):
        frame = next(
            (getattr(awaited, a) for a in _FRAME if getattr(awaited, a, None)), None
        )
        if frame is not None:
            name, line = frame.f_code.co_filename, frame.f_lineno
            out.write(f'  File "{name}", line {line}, in {frame.f_code.co_name}\n')
            out.write(f"    {linecache.getline(name, line).strip()}\n")
        awaited = _awaits(awaited)
        if awaited is None:
            return


_FRAME = ("cr_frame", "ag_frame", "gi_frame")
_AWAITS = ("cr_await", "ag_await", "gi_yieldfrom")


def _awaits(obj: object) -> object:
    for attribute in _AWAITS:
        if hasattr(obj, attribute):
            return getattr(obj, attribute)
    # An async generator's __anext__() awaitable (how pytest-asyncio enters a
    # yield fixture) names its generator by no attribute, only by reference.
    if type(obj).__name__ in ("async_generator_asend", "async_generator_athrow"):
        return next((r for r in gc.get_referents(obj) if hasattr(r, "ag_frame")), None)
    return None


_WATCH = pytest.StashKey[_Watch]()
_FALLBACK_S = 30


def pytest_configure(config: pytest.Config) -> None:
    try:
        timeout = float(config.getini("faulthandler_timeout") or 0)
        if config.getini("faulthandler_exit_on_timeout"):
            timeout = 0.0  # pytest's own dump then exits the process: stay out
    except ValueError:  # `-p no:faulthandler` unregisters the option
        timeout = 0.0
    if timeout <= 0:
        return
    try:
        fileno = sys.stderr.fileno()
    except (AttributeError, ValueError):  # as pytest's faulthandler: xdist's stderr
        fileno = sys.__stderr__.fileno()
    config.stash[_WATCH] = _Watch(timeout, os.dup(fileno))


def pytest_unconfigure(config: pytest.Config) -> None:
    watch = config.stash.get(_WATCH, None)
    if watch is not None:
        watch.stop()
        os.close(watch.fd)
        del config.stash[_WATCH]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem):
    watch = item.config.stash.get(_WATCH, None)
    if watch is None:
        return (yield)
    watch.arm(item.nodeid)
    try:
        return (yield)
    finally:
        watch.disarm()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    # Inside pytest's faulthandler wrapper, after it armed its dump: re-arm
    # that one process-wide timer later, as the fallback the docstring names.
    watch = item.config.stash.get(_WATCH, None)
    if watch is not None:
        faulthandler.dump_traceback_later(watch.timeout + _FALLBACK_S, file=watch.fd)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    # pytest-asyncio runs async tests and async fixtures on an asyncio.Runner it
    # supplies as a fixture, entered before it is yielded, so get_loop() makes
    # no loop. Recorded here, its loop is known before any async setup runs.
    value = yield
    watch = request.config.stash.get(_WATCH, None)
    if watch is not None and isinstance(value, asyncio.Runner):
        try:
            watch.attach(value.get_loop())
        except RuntimeError:  # a closed runner
            pass
    return value
