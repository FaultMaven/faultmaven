"""Name a test that hangs (#3078).

``faulthandler_timeout`` (pytest.ini) makes pytest dump every thread's stack
when a test runs past it, but the dump names no test, and for an ``async def``
test the main thread's stack ends in the event loop's ``select`` with no frame
of the test. At the same timeout this plugin writes one labelled block to the
same stderr: the xdist worker, the test's nodeid and, for a test run on an
asyncio loop, the stack of each task on that loop. The block is one
``os.write``, so two workers' blocks do not interleave (a pipe keeps a write of
up to PIPE_BUF whole). A hang that holds the GIL stops this thread; the
faulthandler dump, written from C, still comes.

``tests/conftest.py`` registers it (``pytest_plugins``); ``-p tests.hang_report``
loads it elsewhere, with the repository on ``sys.path``.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import pytest


@dataclass
class _Running:
    nodeid: str
    deadline: float
    loop: Optional[asyncio.AbstractEventLoop] = None


class _Watch:
    """One thread per process, armed for the running test."""

    def __init__(self, timeout: float, fd: int) -> None:
        self.timeout = timeout
        self.fd = fd
        self._cond = threading.Condition()
        self._running: Optional[_Running] = None
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
            if self._running is not None:
                self._running.loop = loop

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
            block = _block(running, self.timeout).encode("utf-8", "replace")
            while block:
                block = block[os.write(self.fd, block) :]


def _block(running: _Running, timeout: float) -> str:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    out = io.StringIO()
    out.write(
        f"\n=== hung test [{worker}] {running.nodeid}: running past {timeout:g}s ===\n"
    )
    try:
        tasks = list(asyncio.all_tasks(running.loop)) if running.loop else None
    except Exception as exc:  # the loop is another thread's: report, never raise
        tasks = None
        out.write(f"(asyncio tasks unreadable: {exc!r})\n")
    if tasks is None:
        out.write(
            "(no asyncio loop reached; the thread stacks follow from faulthandler)\n"
        )
    for task in tasks or ():
        try:
            task.print_stack(file=out)
        except Exception as exc:
            out.write(f"(stack of {task!r} unreadable: {exc!r})\n")
    out.write(f"=== end hung test [{worker}] {running.nodeid} ===\n")
    return out.getvalue()


_WATCH = pytest.StashKey[_Watch]()


def pytest_configure(config: pytest.Config) -> None:
    try:
        timeout = float(config.getini("faulthandler_timeout") or 0)
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


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    # pytest-asyncio runs an async test on an asyncio.Runner it supplies as a
    # fixture; its loop is the one whose tasks a hang leaves waiting.
    watch = pyfuncitem.config.stash.get(_WATCH, None)
    if watch is not None:
        for value in pyfuncitem.funcargs.values():
            if isinstance(value, asyncio.Runner):
                try:
                    watch.attach(value.get_loop())
                except RuntimeError:  # a closed runner
                    pass
                break
    return (yield)
