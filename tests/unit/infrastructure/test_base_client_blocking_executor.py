"""``call_external`` runs a sync ``call_func`` in the client's executor (#2056).

``asyncio.wait_for`` cannot interrupt synchronous code on the loop thread, and
for a sync ``call_func`` ``call_external`` did not try: it ran inline,
unbounded, with the loop held. A client that sets ``blocking_executor`` now
has its sync calls run there, where the loop stays live.

Such a call is awaited to its END, with no ``wait_for``. Ending the await
early would leave the call running, so a caller would see a timeout for a
write still landing and could undo it before it landed. The client bounds its
own requests instead. A client that sets no executor, which is every client
but the ChromaDB stores on the HTTP arm, keeps the old inline behaviour, and
coroutine ``call_func``s are untouched.

No test here reads a clock (#1579): each judges an order of events.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
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


async def test_a_running_sync_call_is_never_abandoned_by_its_timeout(
    executor: ThreadPoolExecutor,
) -> None:
    """R2: the caller sees the call's real outcome, never a timeout for a
    write that is still landing.

    The call is still running long after ``timeout`` has passed: the loop
    runs a coroutine that sleeps ten times the timeout before letting it
    finish. ``call_external`` returns what the call returned, and only once
    it has returned. That coroutine running at all, while the call runs, is
    the loop staying live.
    """
    client = _Client()
    client.blocking_executor = executor
    started = threading.Event()
    proceed = threading.Event()
    returned = threading.Event()

    def _write() -> str:
        started.set()
        proceed.wait(10)
        returned.set()
        return "landed"

    still_running_after_the_timeout = False

    async def _finish_it_well_after_the_timeout() -> None:
        nonlocal still_running_after_the_timeout
        while not started.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        still_running_after_the_timeout = not returned.is_set()
        proceed.set()

    finisher = asyncio.create_task(_finish_it_well_after_the_timeout())
    try:
        result = await client.call_external(
            operation_name="write", call_func=_write, timeout=0.05
        )
    finally:
        proceed.set()
        await finisher

    assert still_running_after_the_timeout
    assert returned.is_set()
    assert result == "landed"


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

    with pytest.raises(ExternalCallTimeout):
        await client.call_external(operation_name="h", call_func=_hang, timeout=0.2)


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
