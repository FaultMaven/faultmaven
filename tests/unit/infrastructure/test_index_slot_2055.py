"""The indexing slots: admission against the window, one encode at a time, and
a slot held until its thread settles (#2055).

Admission runs at Cloud's shape: window 600 s, 2 cores, the model resident
unless stated; the clock, the token counts, the cores and the model's
residency are patched. The slot tests hold fake encodes on
``threading.Event``s and wait for them on events, so no test loads the model
or depends on how fast a thread starts.
"""

import asyncio
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.core.preprocessing.vector_storage import (
    VectorIndexOutcome,
    store_in_vector_db_background,
)
from faultmaven.infrastructure import embedding_guard, index_slot
from faultmaven.infrastructure.embedding_guard import embed_texts_or_raise
from faultmaven.infrastructure.index_slot import IndexAdmission, index_max_tokens
from faultmaven.infrastructure.model_cache import model_cache
from faultmaven.models.exceptions import IndexAdmissionRefused, KnowledgeBaseError

pytestmark = [pytest.mark.unit]

WINDOW = 600.0
RUNBOOK_TOKENS = 29_746  # 371.825 s warm on 2 cores
REPO = Path(__file__).resolve().parents[3]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(index_slot, "_clock", fake)
    return fake


@pytest.fixture
def cloud(monkeypatch, clock):
    """2 cores, the model resident."""
    monkeypatch.setattr(
        "faultmaven.infrastructure.model_cache._cgroup_cpu_quota", lambda: 2.0
    )
    monkeypatch.setattr(model_cache, "peek_bge_m3_model", lambda: object())
    return clock


def _refused(admission, tokens, window=WINDOW) -> IndexAdmissionRefused:
    with pytest.raises(IndexAdmissionRefused) as refused:
        admission.admit(["c"], tokens, window_seconds=window)
    return refused.value


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_an_idle_runbook_is_admitted_at_its_estimate(cloud):
    ticket = IndexAdmission().admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)

    assert ticket.estimate_seconds == pytest.approx(371.825)


def test_a_second_concurrent_one_is_refused_503_with_the_pending_wait(cloud):
    admission = IndexAdmission()
    admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)

    refused = _refused(admission, RUNBOOK_TOKENS)

    assert (refused.status_code, refused.reason) == (503, "busy")
    assert refused.error_code == "KNOWLEDGE_INDEXING_BUSY"
    assert refused.retry_after_seconds == 372  # ceil(371.825)
    assert refused.message == index_slot.refusal("busy", wait=372).message
    assert admission.queued() == 1


@pytest.mark.parametrize(
    "cores,window", [(2.0, 600), (3.3, 67), (1.7, 601), (0.5, 60), (3.9, 3599)]
)
def test_the_413_line_is_one_integer_for_the_decision_and_the_sentence(
    cloud, monkeypatch, cores, window
):
    monkeypatch.setattr(
        "faultmaven.infrastructure.model_cache._cgroup_cpu_quota", lambda: cores
    )
    most = index_max_tokens(window)
    admission = IndexAdmission()

    refused = _refused(admission, most + 1, window)
    ticket = admission.admit(["c"], most, window_seconds=window)

    assert (refused.status_code, refused.reason) == (413, "too_large")
    assert refused.retry_after_seconds is None
    assert f"{most + 1:,} tokens" in refused.message
    assert f"at most {most:,} tokens" in refused.message
    assert ticket in admission._tickets


def test_a_cold_model_charges_the_load_to_the_first_ticket_only(cloud, monkeypatch):
    monkeypatch.setattr(model_cache, "peek_bge_m3_model", lambda: None)
    admission = IndexAdmission()

    first = admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)
    second = admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=0)

    assert first.estimate_seconds == pytest.approx(371.825 + 180)
    assert second.estimate_seconds == pytest.approx(371.825)


def test_an_overrun_counts_its_guard_bound_then_the_floor(cloud):
    admission = IndexAdmission()
    ticket = admission.admit_forced(["c"], 8_000)  # 100 s warm
    ticket.guard_seconds = 380.0
    ticket._started()

    cloud.now = 60.0
    assert admission.pending_seconds() == pytest.approx(40.0)
    cloud.now = 150.0
    assert admission.pending_seconds() == pytest.approx(230.0)  # 380 - 150
    cloud.now = 375.0
    assert admission.pending_seconds() == 10.0
    assert admission.encoding_for_seconds() == pytest.approx(375.0)


def test_a_hung_encode_refuses_every_document_until_it_settles(cloud):
    admission = IndexAdmission()
    ticket = admission.admit_forced(["c"], 8_000)
    ticket.guard_seconds = 380.0
    ticket._started()
    cloud.now = 381.0

    refused = _refused(admission, 1)
    admission._release(ticket)

    assert (refused.status_code, refused.reason) == (503, "hung")
    assert refused.retry_after_seconds == 60
    assert "stuck" in refused.message
    assert admission.admit(["c"], 1, window_seconds=WINDOW)


def test_a_window_of_zero_refuses_nothing(cloud):
    admission = IndexAdmission()
    for tokens in (10_000_000, RUNBOOK_TOKENS, None):
        admission.admit(["c"], tokens, window_seconds=0)

    assert admission.queued() == 3


def test_with_a_window_an_uncountable_document_is_refused_unavailable(cloud):
    with pytest.raises(KnowledgeBaseError) as refused:
        IndexAdmission().admit(["c"], None, window_seconds=WINDOW)

    assert refused.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"


def test_a_forced_ticket_is_never_refused(cloud):
    admission = IndexAdmission()
    admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)

    assert admission.admit_forced(["c"], 10_000_000)
    assert admission.queued() == 2


# ---------------------------------------------------------------------------
# The cold band: a document that fits once the model has loaded
# ---------------------------------------------------------------------------


@pytest.fixture
def cold(cloud, monkeypatch):
    """A cold model whose load, on the slot, waits for ``loaded``."""
    state = SimpleNamespace(
        resident=False, loads=0, loading=threading.Event(), loaded=threading.Event()
    )

    def _load(triggered_by):
        state.loads += 1
        state.triggered_by = triggered_by
        state.loading.set()
        state.loaded.wait(30)
        state.resident = True

    monkeypatch.setattr(model_cache, "get_bge_m3_model", _load)
    monkeypatch.setattr(
        model_cache, "peek_bge_m3_model", lambda: object() if state.resident else None
    )
    yield state
    state.loaded.set()


@pytest.mark.parametrize(
    "window,tokens", [(WINDOW, 40_000), (120.0, 2_000)], ids=["600s", "under-180s"]
)
async def test_a_cold_band_refusal_starts_one_load_and_a_retry_after_it_fits(
    cold, clock, window, tokens
):
    admission = IndexAdmission()

    first = _refused(admission, tokens, window)
    await asyncio.to_thread(cold.loading.wait, 30)
    clock.now = 50.0
    second = _refused(admission, tokens, window)
    cold.loaded.set()
    await _settled(admission)
    ticket = admission.admit(["c"], tokens, window_seconds=window)

    assert (first.status_code, first.reason, first.retry_after_seconds) == (
        503,
        "loading",
        180,
    )
    assert "model is loading" in first.message
    assert (second.reason, second.retry_after_seconds) == ("loading", 130)
    assert (cold.loads, cold.triggered_by) == (1, "index-admission")
    assert ticket.estimate_seconds == pytest.approx(tokens * 0.025 / 2)


# ---------------------------------------------------------------------------
# The slot
# ---------------------------------------------------------------------------


class Encodes:
    """``embed_texts_sync``: records each encode's text and slot thread, and
    holds those named in ``held`` until the test releases them."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.held: set[str] = set()
        self.entered: dict[str, threading.Event] = {}
        self.release = threading.Event()
        self.active: dict[str, int] = {}
        self.most_active: dict[str, int] = {}
        self._lock = threading.Lock()

    def event(self, text: str) -> threading.Event:
        return self.entered.setdefault(text, threading.Event())

    def __call__(self, texts):
        slot = threading.current_thread().name.split("_")[0]
        with self._lock:
            self.started.append(texts[0])
            self.active[slot] = self.active.get(slot, 0) + 1
            self.most_active[slot] = max(
                self.most_active.get(slot, 0), self.active[slot]
            )
        self.event(texts[0]).set()
        if texts[0] in self.held or "*" in self.held:
            self.release.wait(30)
        with self._lock:
            self.active[slot] -= 1
        return [[0.1] for _ in texts]


@pytest.fixture
async def encodes(monkeypatch):
    fake = Encodes()
    monkeypatch.setattr(model_cache, "embed_texts_sync", fake)
    monkeypatch.setattr(
        embedding_guard, "count_index_tokens", AsyncMock(return_value=0)
    )
    yield fake
    fake.release.set()
    for executor in (index_slot._KB_EXECUTOR, index_slot._EVIDENCE_EXECUTOR):
        # Bounded: a slot left blocked fails the teardown, not the run.
        await asyncio.wait_for(asyncio.wrap_future(executor.submit(lambda: None)), 30)
    assert index_slot.evidence_slot.queued() == 0, "an evidence ticket leaked"


async def _until(predicate) -> None:
    """Wait for ``predicate``, failing rather than hanging if it never holds."""

    async def _poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), 10)


async def _settled(slot) -> None:
    await _until(lambda: not slot.queued())


async def _entered(encodes, text) -> None:
    await asyncio.to_thread(encodes.event(text).wait, 30)


def _embed(ticket, text):
    return embed_texts_or_raise(
        [text], subject="Indexing", operation="t", ticket=ticket
    )


async def test_a_caller_that_gave_up_mid_encode_keeps_the_slot_until_it_settles(
    encodes,
):
    encodes.held = {"a"}
    admission = IndexAdmission()
    first = admission.admit_forced(["a"], 0)
    future, _ = first.run(["a"], guard_seconds=60.0)
    await _entered(encodes, "a")

    assert first.abandon() is False, "an encoding job cannot be stopped"
    second = admission.admit_forced(["b"], 0)
    waiting = asyncio.create_task(_embed(second, "b"))
    await _until(lambda: second._future is not None)

    assert admission.queued() == 2, "the abandoned ticket released the slot"
    assert not second._future.running() and encodes.started == ["a"]
    encodes.release.set()
    assert await waiting == [[0.1]]
    assert encodes.started == ["a", "b"]
    await _settled(admission)


async def test_a_request_cancelled_while_queued_never_runs_and_frees_its_ticket(
    encodes,
):
    encodes.held = {"a"}
    admission = IndexAdmission()
    first = admission.admit_forced(["a"], 0)
    running = asyncio.create_task(_embed(first, "a"))
    await _entered(encodes, "a")
    second = admission.admit_forced(["b"], 0)
    queued = asyncio.create_task(_embed(second, "b"))
    await _until(lambda: second._future is not None)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await _until(lambda: admission.queued() == 1)
    encodes.release.set()
    await running
    await _settled(admission)

    assert second._future.cancelled()
    assert encodes.started == ["a"]


async def test_two_concurrent_indexing_calls_never_encode_at_once(
    encodes, monkeypatch, tmp_path
):
    """The slot's one worker alone, with no host lock to fall back on: a lock
    file that cannot be opened is logged and reported, and the encodes run
    without it."""
    monkeypatch.setattr(index_slot, "HOST_LOCK_PATH", str(tmp_path / "no" / "kb.lock"))
    encodes.held = {"a", "b"}
    admission = IndexAdmission()
    tickets = [admission.admit_forced([t], 0) for t in ("a", "b")]
    calls = [asyncio.create_task(_embed(t, t.chunks[0])) for t in tickets]
    await _entered(encodes, "a")
    await _until(lambda: tickets[1]._future is not None)

    # A second worker would start the second encode well within this.
    second_started = await asyncio.to_thread(encodes.event("b").wait, 0.3)
    encodes.release.set()
    await asyncio.gather(*calls)
    await _settled(admission)

    assert not second_started
    assert encodes.most_active["kb-index"] == 1
    assert admission.host_lock_available is False


async def test_a_request_timing_out_while_queued_says_it_waited_for_other_indexing(
    encodes, monkeypatch
):
    encodes.held = {"a"}
    admission = IndexAdmission()
    first = admission.admit_forced(["a"], 0)
    future, _ = first.run(["a"], guard_seconds=0.0)
    await _entered(encodes, "a")
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    second = admission.admit_forced(["b"], 0)

    with pytest.raises(KnowledgeBaseError, match="for other indexing") as waited:
        await _embed(second, "b")

    assert waited.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert second._future.cancelled()
    encodes.release.set()
    await asyncio.wrap_future(future)
    await _settled(admission)
    assert encodes.started == ["a"]


@pytest.fixture
def held_host_lock():
    """The host lock, held by another holder until released."""
    fd = os.open(index_slot.HOST_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield lambda: fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@pytest.mark.parametrize("gives_up", ["cancelled", "times_out"])
async def test_a_caller_that_gives_up_behind_the_host_lock_never_encodes(
    encodes, held_host_lock, monkeypatch, gives_up
):
    admission = IndexAdmission()
    ticket = admission.admit_forced(["a"], 0)
    if gives_up == "times_out":
        monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    caller = asyncio.create_task(_embed(ticket, "a"))
    if gives_up == "cancelled":
        await _until(lambda: admission.waiting_for_host_lock)
        caller.cancel()

    with pytest.raises(BaseException) as gave_up:
        await caller
    held_host_lock()
    await _settled(admission)

    if gives_up == "times_out":
        assert gave_up.type is KnowledgeBaseError
        assert "for other indexing" in str(gave_up.value), gave_up.value
    assert encodes.started == [], "the job encoded after its caller gave up"
    assert admission.waiting_for_host_lock is False


# ---------------------------------------------------------------------------
# The host lock
# ---------------------------------------------------------------------------

_HOLDER = """
import sys, time
from pathlib import Path
from faultmaven.infrastructure import index_slot

lock, log, ready, go, name = sys.argv[1:]
index_slot.HOST_LOCK_PATH = lock
Path(ready).touch()
while not Path(go).exists():
    time.sleep(0.01)

def job():
    with open(log, "a") as out:
        out.write(f"start {name}\\n")
    time.sleep(0.3)
    with open(log, "a") as out:
        out.write(f"end {name}\\n")

ticket = index_slot.index_admission.admit_forced([], 0)
index_slot.index_admission._job(ticket, job, lambda: None)
"""


def test_two_processes_hold_the_host_lock_in_turn(tmp_path):
    """The real ``flock``: the worker processes of one container share a CPU
    quota, so they take turns."""
    lock, log, go = tmp_path / "kb.lock", tmp_path / "holds.log", tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    holders = []
    for name in ("A", "B"):
        ready, errors = tmp_path / f"ready-{name}", tmp_path / f"stderr-{name}"
        with errors.open("w") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-c", _HOLDER, str(lock), str(log)]
                + [str(ready), str(go), name],
                cwd=REPO,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        holders.append((ready, errors, process))
    while not all(ready.exists() for ready, _, _ in holders):
        for _, errors, process in holders:
            assert process.poll() is None, errors.read_text()
        threading.Event().wait(0.01)
    go.touch()
    for _, errors, process in holders:
        assert process.wait(60) == 0, errors.read_text()

    lines = log.read_text().split()
    first, second = lines[1], lines[5]
    assert {first, second} == {"A", "B"}
    assert lines == ["start", first, "end", first, "start", second, "end", second]


# ---------------------------------------------------------------------------
# Evidence: through the guard, one at a time on its own slot
# ---------------------------------------------------------------------------


async def _vectorize(case_vector_store=None):
    return await store_in_vector_db_background(
        case_id="case-1",
        evidence_id="ev-1",
        structural_index="## Errors\nOOMKilled at 03:12\n",
        data_type=UnifiedDataType.LOGS,
        metadata={},
        case_vector_store=case_vector_store or MagicMock(add_documents=AsyncMock()),
        max_chunk_tokens=500,
        overlap_tokens=50,
    )


async def test_a_hung_evidence_embed_is_failed_not_indexed(encodes, monkeypatch):
    encodes.held = {"*"}
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    store = MagicMock(add_documents=AsyncMock())

    assert await _vectorize(store) is VectorIndexOutcome.FAILED
    store.add_documents.assert_not_awaited()


async def test_an_unavailable_model_is_embedder_unavailable(monkeypatch):
    monkeypatch.setattr(model_cache, "embed_texts_sync", lambda _texts: None)

    assert await _vectorize() is VectorIndexOutcome.EMBEDDER_UNAVAILABLE


def _evidence(text):
    return embed_texts_or_raise([text], subject="Evidence", operation="t")


async def test_evidence_encodes_queue_one_at_a_time_beside_the_kb_slot(encodes):
    """The probed design: a re-trigger while an abandoned encode still runs
    queues behind it and, given up, never starts; the KB slot still encodes
    meanwhile; a later re-trigger runs once the first settles."""
    encodes.held = {"first"}
    slot = index_slot.evidence_slot
    first = asyncio.create_task(_evidence("first"))
    await _entered(encodes, "first")
    first.cancel()  # its caller gave up; the encode runs on
    with pytest.raises(asyncio.CancelledError):
        await first

    retrigger = asyncio.create_task(_evidence("retrigger"))
    await _until(lambda: slot.queued() == 2)
    kb = IndexAdmission().admit_forced(["kb"], 0)
    assert await _embed(kb, "kb") == [[0.1]], "the KB slot waited for evidence"
    retrigger.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retrigger
    encodes.release.set()
    await _settled(slot)
    later = await _evidence("later")

    assert later == [[0.1]]
    assert encodes.started == ["first", "kb", "later"]
    assert encodes.most_active["evidence-index"] == 1
