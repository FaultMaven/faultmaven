"""The indexing slots: admission against the window, one encode at a time, and
a slot held until its thread settles (#2055).

Admission runs at window 600 s and 2 cores; the clock, the
token counts and the cores are patched. Every KB ticket is charged a worker's
load (``WORKER_LOAD_SECONDS``, #2851). The slot tests hold fake encodes (the
KB slot's ``encode_in_worker`` and the evidence slot's ``embed_texts_sync``)
on ``threading.Event``s and wait for them on events, so no test loads the
model or depends on how fast a thread starts.
"""

import asyncio
import fcntl
import math
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.core.preprocessing.vector_storage import (
    VectorIndexOutcome,
    store_in_vector_db_background,
)
from faultmaven.infrastructure import embedding_guard, index_slot
from faultmaven.infrastructure.embedding_guard import embed_texts_or_raise
from faultmaven.infrastructure.index_slot import (
    COUNT_ALLOWANCE_SECONDS,
    WORKER_LOAD_SECONDS,
    IndexAdmission,
    index_max_tokens,
)
from faultmaven.infrastructure.model_cache import model_cache
from faultmaven.models.exceptions import EmbedderUnavailable, IndexAdmissionRefused

pytestmark = [pytest.mark.unit]

WINDOW = 600.0
RUNBOOK_TOKENS = 29_746  # 371.825 s warm on 2 cores
USER = "user-1"
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
    """2 cores."""
    monkeypatch.setattr(
        "faultmaven.infrastructure.model_cache._cgroup_cpu_quota", lambda: 2.0
    )
    return clock


def _refused(admission, tokens, window=WINDOW) -> IndexAdmissionRefused:
    with pytest.raises(IndexAdmissionRefused) as refused:
        admission.admit(["c"], tokens, principal=USER, window_seconds=window)
    return refused.value


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_an_idle_runbook_is_admitted_at_its_estimate_and_the_load(cloud):
    ticket = IndexAdmission().admit(
        ["c"], RUNBOOK_TOKENS, principal=USER, window_seconds=WINDOW
    )

    ticket.close()

    assert ticket.estimate_seconds == pytest.approx(371.825 + 30)
    assert ticket.principal == USER


def test_a_second_concurrent_one_is_refused_503_with_the_pending_wait(cloud):
    admission = IndexAdmission()
    first = admission.admit(
        ["c"], RUNBOOK_TOKENS, principal=USER, window_seconds=WINDOW
    )

    refused = _refused(admission, RUNBOOK_TOKENS)
    queued = admission.queued()
    first.close()

    assert (refused.status_code, refused.reason) == (503, "busy")
    assert refused.error_code == "KNOWLEDGE_INDEXING_BUSY"
    assert refused.retry_after_seconds == 402  # ceil(371.825 + 30)
    assert refused.message == index_slot.refusal("busy", wait=402).message
    assert queued == 1


@pytest.mark.parametrize(
    "cores,window", [(2.0, 600), (3.3, 67), (1.7, 601), (0.5, 60), (3.9, 3599)]
)
def test_the_413_line_takes_the_load_and_an_empty_queue_never_answers_busy(
    cloud, monkeypatch, cores, window
):
    """#2851 (Q2 (a)): at window ``W`` the line is
    ``index_max_tokens(W - WORKER_LOAD_SECONDS - COUNT_ALLOWANCE_SECONDS)``,
    the charge the fit check takes and the count's allowance, so on an empty
    queue every count up to it is admitted and the next is 413. On ``main`` the line took no charge: on an empty queue a count up
    to it found the cold model's load charge and was answered ``loading``, or
    was admitted, and the counts above the line less the charge were never
    413."""
    monkeypatch.setattr(
        "faultmaven.infrastructure.model_cache._cgroup_cpu_quota", lambda: cores
    )
    most = index_max_tokens(window - WORKER_LOAD_SECONDS - COUNT_ALLOWANCE_SECONDS)

    refused = _refused(IndexAdmission(), most + 1, window)
    step = max(1, most // 500)
    for tokens in [*range(0, most, step), most]:
        admission = IndexAdmission()
        ticket = admission.admit(["c"], tokens, principal=USER, window_seconds=window)
        assert ticket in admission._tickets, tokens
        ticket.close()  # the slots share one queue (#2920)

    assert (refused.status_code, refused.reason) == (413, "too_large")
    assert refused.retry_after_seconds is None
    assert refused.message == (  # prose: priced is plain, so ``most`` is M
        f"This document is {most + 1:,} tokens, and this server can index at "
        f"most {most:,} tokens of text like it in one request. Split it into "
        "smaller documents. Nothing was saved."
    )


def test_every_kb_ticket_is_charged_the_worker_load_and_none_says_loading(cloud):
    """A forced ticket too; the evidence slot's none (it encodes in this
    process). No refusal says the model is loading (#2851)."""
    admission = IndexAdmission()

    first = admission.admit(["c"], RUNBOOK_TOKENS, principal=USER, window_seconds=0)
    forced = admission.admit_forced(["c"], RUNBOOK_TOKENS, principal=USER)
    evidence = index_slot.evidence_slot.admit_forced(["c"], 8_000, principal=USER)
    for ticket in (first, forced, evidence):
        ticket.close()

    assert first.estimate_seconds == pytest.approx(371.825 + WORKER_LOAD_SECONDS)
    assert forced.estimate_seconds == pytest.approx(371.825 + WORKER_LOAD_SECONDS)
    assert evidence.estimate_seconds == pytest.approx(100.0)
    with pytest.raises(KeyError):
        index_slot.refusal("loading", wait=1)


def test_an_overrun_counts_its_guard_bound_then_the_floor(cloud):
    admission = IndexAdmission()
    ticket = admission.admit_forced(["c"], 8_000, principal=USER)  # 100 s + 30
    ticket.guard_seconds = 380.0
    ticket._started()

    cloud.now = 60.0
    assert admission.pending_seconds() == pytest.approx(70.0)
    cloud.now = 150.0
    assert admission.pending_seconds() == pytest.approx(230.0)  # 380 - 150
    cloud.now = 375.0
    assert admission.pending_seconds() == 10.0
    assert admission.encoding_for_seconds() == pytest.approx(375.0)
    ticket.close()


def test_a_hung_encode_refuses_every_document_until_it_settles(cloud):
    admission = IndexAdmission()
    ticket = admission.admit_forced(["c"], 8_000, principal=USER)
    ticket.guard_seconds = 380.0
    ticket._started()
    cloud.now = 381.0

    refused = _refused(admission, 1)
    admission._release(ticket)

    assert (refused.status_code, refused.reason) == (503, "hung")
    assert refused.retry_after_seconds == 60
    assert "stuck" in refused.message
    admission.admit(["c"], 1, principal=USER, window_seconds=WINDOW).close()


def test_a_window_of_zero_refuses_nothing(cloud):
    admission = IndexAdmission()
    tickets = [
        admission.admit(["c"], tokens, principal=USER, window_seconds=0)
        for tokens in (10_000_000, RUNBOOK_TOKENS, None)
    ]
    queued = admission.queued()
    for ticket in tickets:
        ticket.close()

    assert queued == 3


def test_with_a_window_an_uncountable_document_is_refused_unavailable(cloud):
    """A count that gave no answer: the worker failed, not the model (#2461)."""
    with pytest.raises(EmbedderUnavailable) as refused:
        IndexAdmission().admit(["c"], None, principal=USER, window_seconds=WINDOW)

    assert refused.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"
    assert refused.value.reason == "worker_failed"


def test_a_forced_ticket_is_never_refused(cloud):
    admission = IndexAdmission()
    first = admission.admit(
        ["c"], RUNBOOK_TOKENS, principal=USER, window_seconds=WINDOW
    )
    forced = admission.admit_forced(["c"], 10_000_000, principal=USER)
    queued = admission.queued()
    first.close()
    forced.close()

    assert queued == 2


# ---------------------------------------------------------------------------
# The slot
# ---------------------------------------------------------------------------


class Encodes:
    """Both slots' encodes (the KB slot's ``encode_in_worker``, the evidence
    slot's ``embed_texts_sync``): records each encode's text and slot thread,
    and holds those named in ``held`` until the test releases them."""

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
        index_slot, "encode_in_worker", lambda _t, texts, _g: fake(texts)
    )
    monkeypatch.setattr(
        embedding_guard, "count_index_tokens", AsyncMock(return_value=(0, 0))
    )
    yield fake
    fake.release.set()
    # Bounded: an order left blocked fails the teardown, not the run.
    done = index_slot._EXECUTOR.submit(lambda: None)
    await asyncio.wait_for(asyncio.wrap_future(done), 30)
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


async def test_an_evidence_caller_that_gave_up_mid_encode_keeps_the_slot(
    encodes,
):
    """An evidence encode runs in this process: once started, nothing stops
    it. (A KB encode's worker is killed: #2851's tests.)"""
    encodes.held = {"a"}
    admission = index_slot.EncodeSlot()
    first = admission.admit_forced(["a"], 0, principal=USER)
    future, _ = first.run(["a"], guard_seconds=60.0)
    await _entered(encodes, "a")

    assert first.abandon() is False, "an encoding job cannot be stopped"
    second = admission.admit_forced(["b"], 0, principal=USER)
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
    first = admission.admit_forced(["a"], 0, principal=USER)
    running = asyncio.create_task(_embed(first, "a"))
    await _entered(encodes, "a")
    second = admission.admit_forced(["b"], 0, principal=USER)
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
    tickets = [admission.admit_forced([t], 0, principal=USER) for t in ("a", "b")]
    calls = [asyncio.create_task(_embed(t, t.chunks[0])) for t in tickets]
    await _entered(encodes, "a")
    await _until(lambda: tickets[1]._future is not None)

    # A second worker would start the second encode well within this.
    second_started = await asyncio.to_thread(encodes.event("b").wait, 0.3)
    encodes.release.set()
    await asyncio.gather(*calls)
    await _settled(admission)

    assert not second_started
    assert encodes.most_active["index-encode"] == 1
    assert admission.host_lock_available is False


async def test_a_request_timing_out_while_queued_says_it_waited_for_other_indexing(
    encodes, monkeypatch
):
    encodes.held = {"a"}
    admission = IndexAdmission()
    first = admission.admit_forced(["a"], 0, principal=USER)
    future, _ = first.run(["a"], guard_seconds=0.0)
    await _entered(encodes, "a")
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    second = admission.admit_forced(["b"], 0, principal=USER)

    with pytest.raises(EmbedderUnavailable, match="for other indexing") as waited:
        await _embed(second, "b")

    assert waited.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert waited.value.reason == "queued_too_long"
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
    ticket = admission.admit_forced(["a"], 0, principal=USER)
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
        assert gave_up.type is EmbedderUnavailable
        assert gave_up.value.reason == "queued_too_long"
        assert "for other indexing" in str(gave_up.value), gave_up.value
    assert encodes.started == [], "the job encoded after its caller gave up"
    assert admission.waiting_for_host_lock is False


# ---------------------------------------------------------------------------
# The host lock
# ---------------------------------------------------------------------------


def test_a_later_successful_lock_reports_the_host_lock_available_again(
    monkeypatch, tmp_path
):
    admission = IndexAdmission()
    monkeypatch.setattr(index_slot, "HOST_LOCK_PATH", str(tmp_path / "no" / "kb.lock"))
    failed = admission.admit_forced([], 0, principal=USER)
    admission._job(failed, lambda: None, lambda: None)
    after_failure = admission.host_lock_available
    monkeypatch.setattr(index_slot, "HOST_LOCK_PATH", str(tmp_path / "kb.lock"))
    locked = admission.admit_forced([], 0, principal=USER)
    admission._job(locked, lambda: None, lambda: None)
    failed.close()
    locked.close()

    assert (after_failure, admission.host_lock_available) == (False, True)


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

ticket = index_slot.index_admission.admit_forced([], 0, principal="p")
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


async def test_an_abandoned_evidence_encode_holds_the_one_order(encodes):
    """A re-trigger while an abandoned encode still runs queues behind it and,
    given up, never starts; a KB encode waits for it too (#2920); a later
    re-trigger runs once the first settles."""
    encodes.held = {"first"}
    slot = index_slot.evidence_slot
    first = asyncio.create_task(_evidence("first"))
    await _entered(encodes, "first")
    first.cancel()  # its caller gave up; the encode runs on
    with pytest.raises(asyncio.CancelledError):
        await first

    retrigger = asyncio.create_task(_evidence("retrigger"))
    await _until(lambda: slot.queued() == 2)
    kb_ticket = IndexAdmission().admit_forced(["kb"], 0, principal=USER)
    kb = asyncio.create_task(_embed(kb_ticket, "kb"))
    await _until(lambda: kb_ticket._future is not None)
    retrigger.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retrigger
    assert not kb_ticket._future.done(), "the KB encode ran beside evidence"
    encodes.release.set()
    assert await kb == [[0.1]]
    await _settled(slot)
    later = await _evidence("later")

    assert later == [[0.1]]
    assert encodes.started == ["first", "kb", "later"]
    assert encodes.most_active == {"index-encode": 1}


# ---------------------------------------------------------------------------
# One order: both slots, in submit order (#2920)
# ---------------------------------------------------------------------------


def _submit(slot, text, **forced):
    ticket = slot.admit_forced([text], 0, **forced)
    future, ahead = ticket.run([text], guard_seconds=60.0)
    return ticket, future, ahead


async def test_a_kb_encode_runs_after_the_evidence_encodes_submitted_before_it(
    encodes, clock
):
    """E1 runs, E2 queues, then K1: E1, E2, K1, one at a time, and each
    one's wait covers the guard bounds ahead of it, of either slot."""
    encodes.held = {"E1"}
    e1, f1, ahead_e1 = _submit(index_slot.evidence_slot, "E1")
    await _entered(encodes, "E1")
    e2, f2, ahead_e2 = _submit(index_slot.evidence_slot, "E2")
    k1, fk, ahead_k1 = _submit(IndexAdmission(), "K1", principal=USER)

    assert (ahead_e1, ahead_e2, ahead_k1) == (0.0, 60.0, 120.0)
    assert not f2.done() and not fk.done()
    encodes.release.set()
    await asyncio.wait_for(asyncio.wrap_future(fk), 30)

    assert encodes.started == ["E1", "E2", "K1"]
    assert encodes.most_active == {"index-encode": 1}


async def test_a_forced_evidence_encode_runs_after_the_kb_encode_queued_before_it(
    encodes, clock
):
    encodes.held = {"K0"}
    admission = IndexAdmission()
    _, _, _ = _submit(admission, "K0", principal=USER)
    await _entered(encodes, "K0")
    _, fk1, _ = _submit(admission, "K1", principal=USER)
    _, fe, ahead_e = _submit(index_slot.evidence_slot, "E")

    assert ahead_e == 120.0
    encodes.release.set()
    await asyncio.wait_for(asyncio.wrap_future(fe), 30)

    assert encodes.started == ["K0", "K1", "E"]
    assert encodes.most_active == {"index-encode": 1}


async def test_with_the_host_lock_unwritable_no_two_encodes_overlap(
    encodes, monkeypatch, tmp_path
):
    """The order's one thread alone serialises both slots in-process."""
    monkeypatch.setattr(index_slot, "HOST_LOCK_PATH", str(tmp_path / "no" / "kb.lock"))
    encodes.held = {"K"}
    _, fk, _ = _submit(IndexAdmission(), "K", principal=USER)
    await _entered(encodes, "K")
    _, fe, _ = _submit(index_slot.evidence_slot, "E")

    assert not fe.done(), "the evidence encode ran beside the KB one"
    encodes.release.set()
    await asyncio.wait_for(asyncio.wrap_future(fe), 30)

    assert encodes.started == ["K", "E"]
    assert encodes.most_active == {"index-encode": 1}
    assert index_slot.index_admission.host_lock_available is False


def test_a_stuck_evidence_encode_answers_a_kb_write_hung(cloud):
    stuck = index_slot.evidence_slot.admit_forced(["e"], 8_000)
    stuck.guard_seconds = 380.0
    stuck._started()
    cloud.now = 381.0

    refused = _refused(IndexAdmission(), 1)
    stuck.close()

    assert (refused.reason, refused.retry_after_seconds) == ("hung", 60)
    IndexAdmission().admit(["c"], 1, principal=USER, window_seconds=WINDOW).close()


async def test_a_pending_evidence_encode_counts_as_queue_ahead_of_a_kb_write(
    encodes, cloud
):
    """Charged at its UTF-8 bytes plus 2 a text (Q2): a document that fits an
    empty queue is refused ``busy`` beside it, with that wait, and admitted
    once it settles."""
    text = "x" * 2_000
    most = index_max_tokens(WINDOW - WORKER_LOAD_SECONDS - COUNT_ALLOWANCE_SECONDS)
    IndexAdmission().admit(["c"], most, principal=USER, window_seconds=WINDOW).close()
    encodes.held = {text}
    evidence = asyncio.create_task(_evidence(text))
    await _entered(encodes, text)

    refused = _refused(IndexAdmission(), most)
    encodes.release.set()
    await evidence
    await _settled(index_slot.evidence_slot)

    wait = math.ceil(index_slot.index_estimate_seconds(len(text) + 2))
    assert (refused.reason, refused.retry_after_seconds) == ("busy", wait)
    assert wait == 26  # 2,002 x 25 core-ms on 2 cores
    admitted = IndexAdmission().admit(
        ["c"], most, principal=USER, window_seconds=WINDOW
    )
    admitted.close()


async def test_a_reactive_vectorization_that_times_out_behind_a_kb_encode_never_runs(
    encodes, monkeypatch
):
    """Q3: queued behind a longer KB encode, it gives up for the turn, and its
    evidence ticket is released unstarted."""
    from faultmaven.config.settings import get_settings
    from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine

    encodes.held = {"kb"}
    kb, fk, _ = _submit(IndexAdmission(), "kb", principal=USER)
    await _entered(encodes, "kb")
    issued = []
    real_admit = index_slot.evidence_slot.admit_forced

    def _admit(*args, **kwargs):
        issued.append(real_admit(*args, **kwargs))
        return issued[-1]

    monkeypatch.setattr(index_slot.evidence_slot, "admit_forced", _admit)
    monkeypatch.setattr(
        get_settings().agent, "vectorization_reactive_timeout_seconds", 1
    )
    engine = MilestoneEngine(
        llm_provider=AsyncMock(),
        repository=MagicMock(save=AsyncMock()),
        investigation_tools=MagicMock(),
    )
    engine.vectorizer._vectorize_evidence = lambda _ev, _ctx: _vectorize()
    upload = MagicMock(size_bytes=1_000_000)
    case = MagicMock(
        evidence=[MagicMock(evidence_id="ev-1", source_file_id="f-1")],
        find_uploaded_file=MagicMock(return_value=upload),
    )
    context = MagicMock(in_memory_case=case, case_id="case-1", case_repository=None)

    text = await engine.vectorizer._reactive_vectorize(
        "ev-1", context, "before", "low_confidence"
    )

    assert text == "before"
    assert [t._future.cancelled() for t in issued] == [True]
    assert index_slot.evidence_slot.queued() == 1, "the evidence ticket held on"
    encodes.release.set()
    await asyncio.wait_for(asyncio.wrap_future(fk), 30)
    await _settled(index_slot.evidence_slot)
    assert encodes.started == ["kb"]
