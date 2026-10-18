"""``call_external`` runs a sync ``call_func`` in the client's executor (#2056).

``asyncio.wait_for`` cannot interrupt synchronous code on the loop thread, and
for a sync ``call_func`` ``call_external`` did not try: it ran inline,
unbounded, with the loop held. A client that sets ``blocking_executor`` now
has its sync calls run there, where the loop stays live and ``timeout`` ends
the await. A client that sets none, which is every client but the ChromaDB
stores on the HTTP arm, keeps the old inline behaviour, and coroutine
``call_func``s are untouched.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, List

import pytest

from faultmaven.exceptions import ExternalCallTimeout
from faultmaven.infrastructure.base_client import BaseExternalClient

pytestmark = [pytest.mark.unit]

_PREFIX = "t-blocking"


class _Client(BaseExternalClient):
    def __init__(self) -> None:
        super().__init__(
            client_name="blocking_test_client",
            service_name="BlockingTestService",
            enable_circuit_breaker=False,
        )


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix=_PREFIX)
    try:
        yield pool
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


async def test_a_sync_call_runs_in_the_executor(executor: ThreadPoolExecutor) -> None:
    client = _Client()
    client.blocking_executor = executor

    ran_on = await client.call_external(
        operation_name="where", call_func=lambda: threading.current_thread().name
    )

    assert ran_on.startswith(_PREFIX)


async def test_a_timeout_ends_the_await_while_the_loop_ticks(
    executor: ThreadPoolExecutor,
) -> None:
    """The case ``wait_for`` could never bound before: a sync call that stalls.
    Off the loop it is bounded at the call's own budget, and the loop keeps
    ticking meanwhile."""
    client = _Client()
    client.blocking_executor = executor
    stalled = threading.Event()

    def _stall() -> None:
        stalled.wait(2.0)

    gaps: List[float] = []
    stop = asyncio.Event()

    async def _ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)
    started = time.monotonic()
    try:
        with pytest.raises(ExternalCallTimeout) as raised:
            await client.call_external(
                operation_name="stall", call_func=_stall, timeout=0.2
            )
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        await ticker
        stalled.set()

    assert isinstance(raised.value, asyncio.TimeoutError)
    assert elapsed < 1.0
    assert max(gaps) < 0.25


async def test_with_no_executor_a_sync_call_runs_inline_as_before() -> None:
    client = _Client()
    assert client.blocking_executor is None
    loop_thread = threading.current_thread()

    for timeout in (None, 5.0):
        ran_on = await client.call_external(
            operation_name="where",
            call_func=threading.current_thread,
            timeout=timeout,
        )
        assert ran_on is loop_thread


async def test_a_coroutine_call_is_unchanged(executor: ThreadPoolExecutor) -> None:
    """Awaited on the loop, and bounded by ``wait_for``, executor or not."""
    client = _Client()
    client.blocking_executor = executor
    loop_thread = threading.current_thread()

    async def _where() -> threading.Thread:
        return threading.current_thread()

    assert await client.call_external(operation_name="w", call_func=_where) is (
        loop_thread
    )

    async def _hang() -> None:
        await asyncio.sleep(2.0)

    started = time.monotonic()
    with pytest.raises(ExternalCallTimeout):
        await client.call_external(operation_name="h", call_func=_hang, timeout=0.2)
    assert time.monotonic() - started < 1.0


async def test_retries_still_apply_to_a_sync_call(
    executor: ThreadPoolExecutor,
) -> None:
    client = _Client()
    client.blocking_executor = executor
    attempts: List[str] = []

    def _flaky() -> str:
        attempts.append(threading.current_thread().name)
        if len(attempts) == 1:
            raise ConnectionError("transient")
        return "ok"

    result = await client.call_external(
        operation_name="flaky",
        call_func=_flaky,
        timeout=5.0,
        retries=1,
        retry_delay=0.0,
    )

    assert result == "ok"
    assert len(attempts) == 2
    assert all(name.startswith(_PREFIX) for name in attempts)


_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_REQUEST_ID", default="unset"
)


async def test_the_callers_context_reaches_the_executor(
    executor: ThreadPoolExecutor,
) -> None:
    """The request id the log lines carry lives in contextvars;
    ``run_in_executor`` alone would drop it, as ``asyncio.to_thread`` does not."""
    client = _Client()
    client.blocking_executor = executor
    token = _REQUEST_ID.set("req-2056")
    try:
        seen = await client.call_external(
            operation_name="ctx", call_func=_REQUEST_ID.get, timeout=5.0
        )
        direct = await client.run_blocking(_REQUEST_ID.get)
    finally:
        _REQUEST_ID.reset(token)

    assert seen == "req-2056"
    assert direct == "req-2056"
