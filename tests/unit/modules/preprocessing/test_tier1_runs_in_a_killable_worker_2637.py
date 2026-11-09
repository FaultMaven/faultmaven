"""Evidence Tier 1 runs in a worker process its deadline kills (#2637).

``_extract_with_timeout`` ran each extractor on a thread under the 2 s
deadline. ``wait_for`` stopped waiting at 2 s and the caller fell back to the
preview, but the thread ran on (8.6 s at 4.2 MB, measured for #2596), and
native code holding the interpreter lock stalled the loop meanwhile. Under
Principle 14 each extraction runs in a worker process (``worker_process``),
killed and reaped at the deadline, which covers the wait for a slot, the start
and the work.

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

from faultmaven.modules.preprocessing import preprocessing_service as ps
from faultmaven.modules.preprocessing.extractors import (
    CommandOutputExtractor,
    DocumentationExtractor,
    ErrorReportExtractor,
    LogsAndErrorsExtractor,
    MetricsAndPerformanceExtractor,
    ProfilingDataExtractor,
    SourceCodeExtractor,
    StructuredConfigExtractor,
    TraceDataExtractor,
    UnstructuredTextExtractor,
    VisualEvidenceExtractor,
)
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
def spawned(monkeypatch):
    """Every worker the test starts, to read how it ended."""
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        processes.append(await spawn(*args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    return processes


@pytest.fixture
def entered(caplog):
    """Set once a stub's ``ENTERED`` record reaches this process through its
    worker's frames; WARNING is forwarded."""
    caplog.set_level(logging.WARNING)
    event, logger = asyncio.Event(), logging.getLogger(stubs.__name__)

    class _OnEntered(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == stubs.ENTERED:
                event.set()

    handler = _OnEntered()
    logger.addHandler(handler)
    yield event
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
async def test_an_extraction_past_its_deadline_stops_at_it(monkeypatch, spawned, stub):
    """The answer is the preview, and once it is given nothing of the
    extraction runs: no thread of this process is inside the stub (on a
    thread one still was), and its worker was killed."""
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 0.5)

    result = await _service()._extract_with_timeout(stub(), _CONTENT, "hostile.log")

    assert result.metadata == {"timeout_fallback": True}
    assert (result.method, result.content) == ("structure_extraction", _CONTENT)
    assert not _running(stub.extract), "the extraction runs on in this process"
    assert spawned[0].returncode == -signal.SIGKILL


# ---------------------------------------------------------------------------
# The same answers as in process
# ---------------------------------------------------------------------------

_SAMPLES = {
    LogsAndErrorsExtractor: _CONTENT
    + "Nov  8 10:00:01 bastion sshd[812]: Failed password for root from "
    "203.0.113.9 port 4022 ssh2\n" * 3
    + "2026-11-08T10:00:02Z INFO order-processor GET /orders/7 status=200\n" * 40,
    StructuredConfigExtractor: "server:\n  port: 8080\n  workers: 4\n"
    "database:\n  host: db.internal\n  pool_size: 20\n  password: hunter22\n",
    MetricsAndPerformanceExtractor: "timestamp,metric,value\n"
    + "".join(
        f"2026-11-08T10:{m:02d}:00Z,cpu_usage,{40 + m % 7 * 9}\n" for m in range(60)
    ),
    UnstructuredTextExtractor: "The deploy at noon raised the error rate.\n\n"
    "Error: connection refused to postgres:5432\n" * 4,
    SourceCodeExtractor: "import os\n\n\nclass Pool:\n    def acquire(self, timeout=5):\n"
    "        return os.environ.get('POOL')\n\n\ndef main():\n    Pool().acquire()\n",
    VisualEvidenceExtractor: "screenshot.png image/png 1280x720 grafana panel",
    TraceDataExtractor: json.dumps(
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
    ProfilingDataExtractor: "   ncalls  tottime  percall  cumtime  percall "
    "filename:lineno(function)\n"
    + "".join(
        f"     {n * 10}    0.{n:03d}    0.001    1.{n:03d}    0.002 app.py:{n}(f{n})\n"
        for n in range(1, 30)
    ),
    ErrorReportExtractor: "Traceback (most recent call last):\n"
    '  File "/app/client.py", line 88, in call\n'
    "    raise TimeoutError('upstream timed out')\n"
    "TimeoutError: upstream timed out\n",
    DocumentationExtractor: "# Runbook: API latency\n\n## Symptoms\n\np99 above 2 s.\n\n"
    "## Steps\n\n1. Run `kubectl rollout restart deploy/api`.\n2. Watch the panel.\n",
    CommandOutputExtractor: "NAME                   READY   STATUS             RESTARTS   AGE\n"
    "api-7d9f8b6c5-x2k4l    0/1     CrashLoopBackOff   12         34m\n"
    "worker-5c6d7e8f9-q8w7  1/1     Running            0          2d\n",
}


@pytest.mark.parametrize("extractor", list(_SAMPLES), ids=lambda c: c.__name__)
async def test_the_worker_answers_as_the_extractor_does_in_process(
    monkeypatch, extractor
):
    """Each of the 11 extractors, through the worker, gives the result it gives
    in this process. A loaded runner's start is not under test here."""
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)
    content = _SAMPLES[extractor]

    result = await _service()._extract_with_timeout(extractor(), content, "sample")

    in_process = extractor().extract(content)
    assert result.metadata == {"extract_result_json": in_process.to_json()}
    assert (result.method, result.content) == (
        extractor().strategy_name,
        in_process.file_extract,
    )


def test_the_worker_body_runs_the_extractor_it_names():
    """``extraction_worker``'s job, in this process (coverage does not follow
    a worker)."""
    from faultmaven.modules.preprocessing import extraction_worker

    def job(stub: type):
        request = {"extractor": f"{stub.__module__}:{stub.__name__}", "content": "x"}
        return extraction_worker._extract(request, send=None)

    assert job(stubs.CrimeScene)() == stubs.CrimeScene().extract("x").to_json()
    with pytest.raises(TypeError, match="AnswersAString returned str"):
        job(stubs.AnswersAString)()


# ---------------------------------------------------------------------------
# The limits
# ---------------------------------------------------------------------------


async def test_a_worker_past_its_memory_limit_is_the_error_preview(spawned):
    service = _service()

    result = await service._extract_with_timeout(
        stubs.RunsOutOfMemory(), _CONTENT, "big.log"
    )

    assert result.metadata == {
        "error_fallback": True,
        "error": "the extraction worker passed its memory limit",
    }
    assert result.content == _CONTENT
    # The next job runs, in a process of its own.
    result = await service._extract_with_timeout(stubs.CrimeScene(), _CONTENT, "n.log")
    assert stubs.marked(stubs.CrimeScene) in result.metadata["extract_result_json"]
    assert spawned[1].pid != spawned[0].pid


async def test_a_worker_past_its_output_limit_is_reaped_and_the_error_preview(
    monkeypatch, spawned
):
    monkeypatch.setattr(ps, "TIER1_OUTPUT_LIMIT_BYTES", 64 * 1024)

    result = await _service()._extract_with_timeout(
        stubs.AnswersTooMuch(), _CONTENT, "x.log"
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
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 0.5)
    service = _service()

    for n in range(2):
        result = await service._extract_with_timeout(
            stubs.NeverReturns(), _CONTENT, f"{n}.log"
        )
        assert result.metadata == {"timeout_fallback": True}
        assert spawned[n].returncode == -signal.SIGKILL

    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)
    result = await service._extract_with_timeout(stubs.CrimeScene(), _CONTENT, "ok.log")
    assert stubs.marked(stubs.CrimeScene) in result.metadata["extract_result_json"]
    assert len(spawned) == 3
    assert not ps._tier1_slots().locked()


async def test_a_cancelled_extraction_kills_and_reaps_its_worker(
    monkeypatch, spawned, entered
):
    _slots(monkeypatch, 1)
    monkeypatch.setattr(ps, "TIER1_TIMEOUT_SECONDS", 60)

    extraction = asyncio.create_task(
        _service()._extract_with_timeout(stubs.NeverReturns(), _CONTENT, "x.log")
    )
    await asyncio.wait_for(entered.wait(), 60)
    extraction.cancel()
    with pytest.raises(asyncio.CancelledError):
        await extraction

    assert spawned[0].returncode == -signal.SIGKILL
    assert not ps._tier1_slots().locked()
