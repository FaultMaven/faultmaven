"""The KB indexing slot: admission against the window, one encode at a time,
and a slot held until its thread settles (#2055).

Admission runs at Cloud's shape: window 600 s, 2 cores, the model resident
unless stated; the clock, the token counts, the cores and the model's
residency are patched. The slot tests hold a fake encode on a
``threading.Event``, so no test loads the model or reads the wall clock for an
assertion.
"""

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.core.preprocessing.vector_storage import (
    VectorIndexOutcome,
    store_in_vector_db_background,
)
from faultmaven.infrastructure import embedding_guard, index_slot
from faultmaven.infrastructure.embedding_guard import embed_texts_or_raise
from faultmaven.infrastructure.index_slot import IndexAdmission
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


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_an_idle_runbook_is_admitted_at_its_estimate(cloud):
    ticket = IndexAdmission().admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)

    assert ticket.estimate_seconds == pytest.approx(371.825)


def test_a_second_concurrent_one_is_refused_503_with_the_pending_wait(cloud):
    admission = IndexAdmission()
    admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)

    with pytest.raises(IndexAdmissionRefused) as refused:
        admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)

    assert refused.value.status_code == 503
    assert refused.value.error_code == "KNOWLEDGE_INDEXING_BUSY"
    assert refused.value.retry_after_seconds == 372  # ceil(371.825)
    assert admission.queued() == 1


def test_a_document_whose_warm_estimate_exceeds_the_window_is_413(cloud):
    admission = IndexAdmission()

    with pytest.raises(IndexAdmissionRefused) as refused:
        admission.admit(["c"], 48_001, window_seconds=WINDOW)  # 600.0125 s

    assert refused.value.status_code == 413
    assert refused.value.error_code == "KNOWLEDGE_DOCUMENT_TOO_LARGE"
    assert refused.value.retry_after_seconds is None
    assert "48,001 tokens" in refused.value.message
    assert "48,000 tokens" in refused.value.message
    assert "Split it" in refused.value.message
    assert admission.queued() == 0
    assert admission.admit(["c"], 48_000, window_seconds=WINDOW)


def test_a_cold_model_charges_the_load_to_the_first_ticket_only(cloud, monkeypatch):
    monkeypatch.setattr(model_cache, "peek_bge_m3_model", lambda: None)
    admission = IndexAdmission()

    first = admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)
    second = admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=0)

    assert first.estimate_seconds == pytest.approx(371.825 + 180)
    assert second.estimate_seconds == pytest.approx(371.825)


def test_a_running_ticket_past_its_estimate_still_counts_ten_seconds(cloud):
    """An overrun or hung encode never reports 0 s ahead."""
    admission = IndexAdmission()
    ticket = admission.admit(["c"], RUNBOOK_TOKENS, window_seconds=WINDOW)
    ticket._mark_started()

    cloud.now = 100.0
    assert admission.pending_seconds() == pytest.approx(271.825)
    assert admission.encoding_for_seconds() == pytest.approx(100.0)
    cloud.now = 1000.0
    assert admission.pending_seconds() == 10.0


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
# The slot
# ---------------------------------------------------------------------------


class Encodes:
    """``embed_texts_sync`` on the slot: each call records it started, and the
    first ``held`` calls wait for the test to release them."""

    def __init__(self, held: int = 1) -> None:
        self.started: list[str] = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self.held = held
        self.active = 0
        self.most_active = 0
        self._lock = threading.Lock()

    def __call__(self, texts):
        with self._lock:
            self.started.append(texts[0])
            self.active += 1
            self.most_active = max(self.most_active, self.active)
            held = len(self.started) <= self.held
        self.entered.set()
        if held:
            self.release.wait(30)
        with self._lock:
            self.active -= 1
        return [[0.1] for _ in texts]


@pytest.fixture
def encodes(monkeypatch, tmp_path):
    fake = Encodes()
    monkeypatch.setattr(model_cache, "embed_texts_sync", fake)
    monkeypatch.setattr(index_slot, "HOST_LOCK_PATH", str(tmp_path / "kb.lock"))
    monkeypatch.setattr(
        embedding_guard, "count_index_tokens", AsyncMock(return_value=0)
    )
    yield fake
    fake.release.set()


async def _settled(admission: IndexAdmission) -> None:
    while admission.queued():
        await asyncio.sleep(0.01)


async def _submitted(ticket) -> None:
    while ticket._future is None:
        await asyncio.sleep(0)


def _embed(ticket, text):
    return embed_texts_or_raise(
        [text], subject="Indexing", operation="t", ticket=ticket
    )


async def test_a_timed_out_caller_keeps_the_slot_until_its_thread_settles(
    encodes, monkeypatch
):
    admission = IndexAdmission()
    first = admission.admit_forced(["a"], 0)
    # Long enough for the encode to start, so the caller gives up mid-encode.
    monkeypatch.setattr(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 1.0)

    with pytest.raises(KnowledgeBaseError, match="timed out") as timed_out:
        await _embed(first, "a")
    assert timed_out.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert encodes.started == ["a"]

    monkeypatch.setattr(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 60.0)
    second = admission.admit_forced(["b"], 0)
    waiting = asyncio.create_task(_embed(second, "b"))
    await _submitted(second)

    assert admission.queued() == 2, "the timed-out caller released the slot"
    assert not second._future.running() and encodes.started == ["a"]

    encodes.release.set()
    assert await waiting == [[0.1]]
    assert encodes.started == ["a", "b"]
    await _settled(admission)


async def test_a_request_cancelled_while_queued_never_runs_and_frees_its_ticket(
    encodes,
):
    admission = IndexAdmission()
    first = admission.admit_forced(["a"], 0)
    running = asyncio.create_task(_embed(first, "a"))
    await _submitted(first)
    second = admission.admit_forced(["b"], 0)
    queued = asyncio.create_task(_embed(second, "b"))
    await _submitted(second)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    while admission.queued() != 1:
        await asyncio.sleep(0)

    encodes.release.set()
    await running
    await _settled(admission)
    assert second._future.cancelled()
    assert encodes.started == ["a"]


async def test_two_concurrent_indexing_calls_never_encode_at_once(
    encodes, monkeypatch, tmp_path
):
    """The slot's one worker alone, with no host lock to fall back on: a lock
    file that cannot be opened is logged and the encodes run without it."""
    monkeypatch.setattr(index_slot, "HOST_LOCK_PATH", str(tmp_path / "no" / "kb.lock"))
    encodes.held = 2
    admission = IndexAdmission()
    tickets = [admission.admit_forced([t], 0) for t in ("a", "b")]
    calls = [asyncio.create_task(_embed(t, t.chunks[0])) for t in tickets]
    for ticket in tickets:
        await _submitted(ticket)
    await asyncio.to_thread(encodes.entered.wait, 30)

    # A second worker would start the second encode well within this.
    await asyncio.sleep(0.3)
    second_started = len(encodes.started) > 1
    encodes.release.set()
    await asyncio.gather(*calls)
    await _settled(admission)

    assert not second_started
    assert encodes.most_active == 1
    assert sorted(encodes.started) == ["a", "b"]


async def test_a_request_timing_out_while_queued_says_it_waited_for_other_indexing(
    encodes, monkeypatch
):
    admission = IndexAdmission()
    first = admission.admit_forced(["a"], 0)
    future, _ = first.run(["a"], guard_seconds=0.0)
    await asyncio.to_thread(encodes.entered.wait, 30)
    monkeypatch.setattr(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    second = admission.admit_forced(["b"], 0)

    with pytest.raises(KnowledgeBaseError, match="for other indexing") as waited:
        await _embed(second, "b")

    assert waited.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert second._future.cancelled()
    encodes.release.set()
    await asyncio.wrap_future(future)
    await _settled(admission)
    assert encodes.started == ["a"]


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

def job(_texts):
    with open(log, "a") as out:
        out.write(f"start {name}\\n")
    time.sleep(0.3)
    with open(log, "a") as out:
        out.write(f"end {name}\\n")

index_slot._encode_under_host_lock(job, [], lambda: None)
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
# Evidence embeds through the guard, off the slot
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


async def test_a_hung_evidence_embed_is_failed_not_indexed(monkeypatch):
    async def _hang(_texts):
        await asyncio.sleep(3600)

    monkeypatch.setattr(model_cache, "aembed_texts", _hang)
    monkeypatch.setattr(embedding_guard, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    monkeypatch.setattr(
        embedding_guard, "count_index_tokens", AsyncMock(return_value=0)
    )
    store = MagicMock(add_documents=AsyncMock())

    assert await _vectorize(store) is VectorIndexOutcome.FAILED
    store.add_documents.assert_not_awaited()


async def test_an_unavailable_model_is_embedder_unavailable(monkeypatch):
    monkeypatch.setattr(model_cache, "aembed_texts", AsyncMock(return_value=None))

    assert await _vectorize() is VectorIndexOutcome.EMBEDDER_UNAVAILABLE


async def test_a_held_kb_slot_does_not_delay_an_evidence_embed(encodes):
    admission = IndexAdmission()
    kb = admission.admit_forced(["kb"], 0)
    holding = asyncio.create_task(_embed(kb, "kb"))
    await asyncio.to_thread(encodes.entered.wait, 30)

    # The evidence encode is the second call, which is not held.
    assert await _vectorize() is VectorIndexOutcome.INDEXED
    assert not holding.done() and admission.queued() == 1

    encodes.release.set()
    await holding
    await _settled(admission)
