"""Evidence Tier 1 runs in a worker process its deadline kills (#2637).

``_extract_with_timeout`` ran each extractor on a thread under the 2 s
deadline. ``wait_for`` stopped waiting at 2 s and the caller fell back to the
preview, but the thread ran on (8.6 s at 4.2 MB, measured for #2596), and
native code holding the interpreter lock stalled the loop meanwhile. Under
Principle 14 each extraction runs in a worker process (``worker_process``),
killed and reaped at the deadline, which bounds ``extract`` alone: the wait for
a slot, the start and each transfer have bounds of their own.

The extractors here are classes in ``extraction_stubs``, which only the worker
runs. Every worker is a real process, and the deadlines are short. The
assertions are on state, never on elapsed time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from unittest.mock import MagicMock

import pytest

from faultmaven.container.providers.infrastructure import create_extractors
from faultmaven.modules.preprocessing import preprocessing_service as ps
from tests.unit.modules.preprocessing import extraction_stubs as stubs

pytestmark = [pytest.mark.unit]

_CONTENT = "2026-11-08T10:00:00Z ERROR payment-gateway upstream timed out\n" * 5


def _service() -> ps.PreprocessingService:
    return ps.PreprocessingService(
        classifier=MagicMock(), logs_extractor=stubs.CrimeScene()
    )


def _running(function) -> bool:
    """Whether a thread of this process is inside ``function`` now."""
    for frame in sys._current_frames().values():
        while frame is not None:
            if frame.f_code is function.__code__:
                return True
            frame = frame.f_back
    return False


def _slots(monkeypatch, count: int) -> None:
    monkeypatch.setattr(ps, "TIER1_SLOTS", count)
    monkeypatch.setattr(ps, "_tier1_slots_state", None)


@pytest.fixture
def on_entered(caplog):
    """Install ``then``, called on a stub's ``ENTERED`` record: from a worker
    it reaches this process through the frames; WARNING is forwarded."""
    caplog.set_level(logging.WARNING)
    logger, handlers = logging.getLogger(stubs.__name__), []

    class _OnEntered(logging.Handler):
        def __init__(self, then) -> None:
            super().__init__()
            self._then = then

        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == stubs.ENTERED:
                self._then()

    def install(then) -> None:
        handlers.append(_OnEntered(then))
        logger.addHandler(handlers[-1])

    yield install
    for handler in handlers:
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# The deadline stops the work, not only the wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stub",
    [
        pytest.param(stubs.HoldsTheLockInC, id="holds-the-lock-in-c"),
        pytest.param(stubs.LoopsInPython, id="loops-in-python"),
    ],
)
async def test_an_extraction_past_its_deadline_stops_at_it(
    monkeypatch, spawned, on_entered, stub
):
    """While the stub runs, this loop turns three times with its worker alive.
    The answer is the preview, and once it is given nothing of the extraction
    runs: no thread of this process is inside the stub (on a thread one still
    was), and its worker was killed."""
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 0.5)
    loop, alive_after_three_turns, tasks = asyncio.get_running_loop(), [], []

    async def three_turns() -> None:
        for _ in range(3):
            await asyncio.sleep(0.005)
        alive_after_three_turns.append([p.returncode is None for p in spawned])

    on_entered(
        lambda: loop.call_soon_threadsafe(
            lambda: tasks.append(loop.create_task(three_turns()))
        )
    )
    stubs.RELEASED.clear()
    try:
        result = await _service()._extract_with_timeout(
            stub(), _CONTENT, "hostile.log", principal="u1"
        )
        await asyncio.gather(*tasks)

        assert result.metadata == {"timeout_fallback": True}
        assert (result.method, result.content) == ("structure_extraction", _CONTENT)
        assert not _running(stub.extract), "the extraction runs on in this process"
        assert alive_after_three_turns == [[True]], "the loop did not run beside it"
        assert spawned[0].returncode == -signal.SIGKILL
    finally:
        stubs.RELEASED.set()


# ---------------------------------------------------------------------------
# The same answers as in process
# ---------------------------------------------------------------------------

#: An input per extractor the composition registers, by its registry name.
_SAMPLES = {
    "logs_extractor": _CONTENT
    + "Nov  8 10:00:01 bastion sshd[812]: Failed password for root from "
    "203.0.113.9 port 4022 ssh2\n" * 3
    + "2026-11-08T10:00:02Z INFO order-processor GET /orders/7 status=200\n" * 40,
    "config_extractor": "server:\n  port: 8080\n  workers: 4\n"
    "database:\n  host: db.internal\n  pool_size: 20\n  password: hunter22\n",
    "metrics_extractor": "timestamp,metric,value\n"
    + "".join(
        f"2026-11-08T10:{m:02d}:00Z,cpu_usage,{40 + m % 7 * 9}\n" for m in range(60)
    ),
    "text_extractor": "The deploy at noon raised the error rate.\n\n"
    "Error: connection refused to postgres:5432\n" * 4,
    "source_code_extractor": "import os\n\n\nclass Pool:\n"
    "    def acquire(self, timeout=5):\n"
    "        return os.environ.get('POOL')\n\n\ndef main():\n    Pool().acquire()\n",
    "visual_extractor": "screenshot.png image/png 1280x720 grafana panel 支付",
    "trace_extractor": json.dumps(
        {
            "data": [
                {
                    "traceID": "a1b2c3d4e5f60718",
                    "spans": [
                        {
                            "traceID": "a1b2c3d4e5f60718",
                            "spanID": f"{n:016x}",
                            "operationName": f"op{n % 3}",
                            "duration": 1000 * (n + 1),
                            "startTime": 1762600000000000 + n,
                            "process": {"serviceName": f"svc{n % 2}"},
                        }
                        for n in range(6)
                    ],
                }
            ]
        }
    ),
    "profiling_extractor": "   ncalls  tottime  percall  cumtime  percall "
    "filename:lineno(function)\n"
    + "".join(
        f"     {n * 10}    0.{n:03d}    0.001    1.{n:03d}    0.002 app.py:{n}(f{n})\n"
        for n in range(1, 30)
    ),
    "error_report_extractor": "Traceback (most recent call last):\n"
    '  File "/app/client.py", line 88, in call\n'
    "    raise TimeoutError('upstream timed out')\n"
    "TimeoutError: upstream timed out\n",
    "documentation_extractor": "# Runbook: API latency\n\n## Symptoms\n\n"
    "p99 above 2 s.\n\n## Steps\n\n1. Run `kubectl rollout restart deploy/api`.\n",
    "command_output_extractor": "NAME                   READY   STATUS"
    "             RESTARTS   AGE\n"
    "api-7d9f8b6c5-x2k4l    0/1     CrashLoopBackOff   12         34m\n"
    "worker-5c6d7e8f9-q8w7  1/1     Running            0          2d\n",
}


def test_every_registered_extractor_has_a_sample():
    assert set(create_extractors()) == set(_SAMPLES)


@pytest.mark.parametrize("name", sorted(create_extractors()))
async def test_the_worker_answers_as_the_extractor_does_in_process(monkeypatch, name):
    """Each extractor the composition registers gives, through the worker, the
    result it gives in this process. A loaded runner's start, transfers and
    extraction time are not under test here: each step's bound is relaxed."""
    for bound in (
        "TIER1_START_SECONDS",
        "TIER1_TRANSFER_BASE_SECONDS",
        "TIER1_TIMEOUT_SECONDS",
    ):
        monkeypatch.setattr(ps, bound, 60)
    extractor, content = create_extractors()[name], _SAMPLES[name]

    result = await _service()._extract_with_timeout(
        extractor, content, "sample", principal="u1"
    )

    in_process = type(extractor)().extract(content)
    assert result.metadata == {"extract_result_json": in_process.to_json()}
    assert (result.method, result.content) == (
        extractor.strategy_name,
        in_process.file_extract,
    )


def test_an_extractor_the_worker_cannot_import_is_refused_at_construction():
    class Local(stubs.CrimeScene):
        pass

    with pytest.raises(ValueError, match="cannot run in the Tier 1 worker"):
        ps.PreprocessingService(classifier=MagicMock(), logs_extractor=Local())


def test_the_worker_body_runs_the_extractor_it_names():
    """``extraction_worker``'s job, in this process (coverage does not follow
    a worker)."""
    from faultmaven.modules.preprocessing import extraction_worker

    def job(stub: type):
        request = {"extractor": f"{stub.__module__}:{stub.__name__}", "parts": ["x"]}
        return extraction_worker._extract(request, send=None)

    result = job(stubs.CrimeScene)()
    assert extraction_worker._parts(result) == [
        stubs.CrimeScene().extract("x").to_json(),
        stubs.CrimeScene.FILE_EXTRACT,
    ]
    with pytest.raises(TypeError, match="AnswersAString returned str"):
        job(stubs.AnswersAString)()


# ---------------------------------------------------------------------------
# The limits
# ---------------------------------------------------------------------------


async def test_a_worker_past_its_memory_limit_is_the_error_preview(spawned):
    service = _service()

    result = await service._extract_with_timeout(
        stubs.RunsOutOfMemory(), _CONTENT, "big.log", principal="u1"
    )

    assert result.metadata == {
        "error_fallback": True,
        "error": "the extraction worker passed its memory limit",
    }
    assert result.content == _CONTENT
    # The next job runs, in a process of its own.
    result = await service._extract_with_timeout(
        stubs.CrimeScene(), _CONTENT, "n.log", principal="u1"
    )
    assert stubs.marked(stubs.CrimeScene) in result.metadata["extract_result_json"]
    assert spawned[1].pid != spawned[0].pid


async def test_a_worker_past_its_output_limit_is_reaped_and_the_error_preview(
    monkeypatch, spawned
):
    limits = ps.tier1_limits
    monkeypatch.setattr(ps, "tier1_limits", lambda mb: (*limits(mb)[:2], 64 * 1024))

    result = await _service()._extract_with_timeout(
        stubs.AnswersTooMuch(), _CONTENT, "x.log", principal="u1"
    )

    assert result.metadata == {
        "error_fallback": True,
        "error": "the worker's output passed its limit",
    }
    assert spawned[0].returncode is not None


async def test_a_slot_frees_after_more_kills_than_there_are_slots(monkeypatch, spawned):
    """With one slot, two extractions past the deadline in turn are each
    killed, and a third still gets the slot: no killed worker kept it."""
    _slots(monkeypatch, 1)
    monkeypatch.setattr(ps, "TIER1_START_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TRANSFER_BASE_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 0.5)
    service = _service()

    for n in range(2):
        result = await service._extract_with_timeout(
            stubs.NeverReturns(), _CONTENT, f"{n}.log", principal="u1"
        )
        assert result.metadata == {"timeout_fallback": True}
        assert spawned[n].returncode == -signal.SIGKILL

    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)
    result = await service._extract_with_timeout(
        stubs.CrimeScene(), _CONTENT, "ok.log", principal="u1"
    )
    assert stubs.marked(stubs.CrimeScene) in result.metadata["extract_result_json"]
    assert len(spawned) == 3
    assert not ps._tier1_slots().locked()


async def test_a_failure_in_the_worker_is_kept_as_the_error(spawned, caplog):
    """The extractor's own exception reaches the metadata and the log, as when
    it ran on a thread: ``str(e)[:200]``."""
    result = await _service()._extract_with_timeout(
        stubs.AnswersAString(), _CONTENT, "x.log", principal="u1"
    )

    error = "Extractor AnswersAString returned str, expected ExtractResult"
    assert result.metadata == {"error_fallback": True, "error": error}
    assert [m for m in caplog.messages if error in m and "x.log" in m]
    assert spawned[0].returncode == 1


async def test_a_worker_that_never_reports_ready_is_killed_at_its_start_bound(
    monkeypatch, spawned
):
    monkeypatch.setattr(ps, "_EXTRACTION_WORKER", stubs.__name__)
    monkeypatch.setattr(ps, "TIER1_START_SECONDS", 0.5)

    result = await _service()._extract_with_timeout(
        stubs.CrimeScene(), _CONTENT, "x.log", principal="u1"
    )

    assert result.metadata == {
        "error_fallback": True,
        "error": "the extraction worker ran past its start bound",
    }
    assert spawned[0].returncode == -signal.SIGKILL


async def test_no_slot_within_its_bound_is_the_busy_preview(
    monkeypatch, spawned, on_entered, caplog
):
    """A slot wait has its own bound and reason: the server was busy, and no
    worker starts for it."""
    _slots(monkeypatch, 1)
    monkeypatch.setattr(ps, "TIER1_START_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TRANSFER_BASE_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_SLOT_WAIT_SECONDS", 0.2)
    loop, entered, service = asyncio.get_running_loop(), asyncio.Event(), _service()
    on_entered(lambda: loop.call_soon_threadsafe(entered.set))
    holder = asyncio.create_task(
        service._extract_with_timeout(
            stubs.NeverReturns(), _CONTENT, "slow.log", principal="u1"
        )
    )
    await asyncio.wait_for(entered.wait(), 60)

    result = await service._extract_with_timeout(
        stubs.CrimeScene(), _CONTENT, "q.log", principal="u1"
    )

    assert result.metadata == {"busy_fallback": True}
    assert result.content == _CONTENT
    assert [m for m in caplog.messages if "no slot free" in m and "q.log" in m]
    assert len(spawned) == 1
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder


async def test_a_cancelled_extraction_kills_and_reaps_its_worker(
    monkeypatch, spawned, on_entered
):
    _slots(monkeypatch, 1)
    monkeypatch.setattr(ps, "TIER1_START_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TRANSFER_BASE_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)
    loop, entered = asyncio.get_running_loop(), asyncio.Event()
    on_entered(lambda: loop.call_soon_threadsafe(entered.set))

    extraction = asyncio.create_task(
        _service()._extract_with_timeout(
            stubs.NeverReturns(), _CONTENT, "x.log", principal="u1"
        )
    )
    await asyncio.wait_for(entered.wait(), 60)
    extraction.cancel()
    with pytest.raises(asyncio.CancelledError):
        await extraction

    assert spawned[0].returncode == -signal.SIGKILL
    assert not ps._tier1_slots().locked()


async def test_a_newcomer_gets_a_slot_beside_another_users_two_extractions(
    monkeypatch, spawned, on_entered
):
    """#2645's shape on two slots: A's two slow extractions hold one slot and
    queue the other, so B, arriving next, extracts instead of falling back to
    the busy preview (on a plain semaphore A's two held both)."""
    _slots(monkeypatch, 2)
    monkeypatch.setattr(ps, "TIER1_START_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TRANSFER_BASE_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_SLOT_WAIT_SECONDS", 30)
    loop, entered, service = asyncio.get_running_loop(), asyncio.Event(), _service()
    on_entered(lambda: loop.call_soon_threadsafe(entered.set))
    a = [
        asyncio.create_task(
            service._extract_with_timeout(stubs.NeverReturns(), _CONTENT, n, "a")
        )
        for n in ("a1.log", "a2.log")
    ]
    await asyncio.wait_for(entered.wait(), 60)

    result = await service._extract_with_timeout(
        stubs.CrimeScene(), _CONTENT, "b.log", "b"
    )

    assert stubs.marked(stubs.CrimeScene) in result.metadata["extract_result_json"]
    assert len(spawned) == 2, "A's second extraction started beside its first"
    for task in a:
        task.cancel()
    await asyncio.gather(*a, return_exceptions=True)
    assert not ps._tier1_slots().locked()


async def test_a_newcomer_gets_a_slot_beside_another_users_two_extractions(
    monkeypatch, spawned, on_entered
):
    """#2645's shape on two slots: A's two slow extractions hold one slot and
    queue the other, so B, arriving next, extracts instead of falling back to
    the busy preview (on a plain semaphore A's two held both)."""
    _slots(monkeypatch, 2)
    monkeypatch.setattr(ps, "TIER1_START_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TRANSFER_BASE_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(ps, "TIER1_SLOT_WAIT_SECONDS", 30)
    loop, entered, service = asyncio.get_running_loop(), asyncio.Event(), _service()
    on_entered(lambda: loop.call_soon_threadsafe(entered.set))
    a = [
        asyncio.create_task(
            service._extract_with_timeout(stubs.NeverReturns(), _CONTENT, n, "a")
        )
        for n in ("a1.log", "a2.log")
    ]
    await asyncio.wait_for(entered.wait(), 60)

    result = await service._extract_with_timeout(
        stubs.CrimeScene(), _CONTENT, "b.log", "b"
    )

    assert stubs.marked(stubs.CrimeScene) in result.metadata["extract_result_json"]
    assert len(spawned) == 2, "A's second extraction started beside its first"
    for task in a:
        task.cancel()
    await asyncio.gather(*a, return_exceptions=True)
    assert not ps._tier1_slots().locked()
