"""A KB indexing encode, and every BGE-M3 token count, runs in a Principle 14
worker its deadline kills; while one runs the event loop is free (#2851).

The workers are ``model_worker_stubs``, served in place of ``encode_worker``
and ``token_count_worker`` as ``gate_stubs`` stands in for the gate's: real
processes, through the real ``worker_process``, the KB slot and the guard.
No stub loads a model, and every stub ends by its own state or a kill, never
by a clock. The last test runs the real workers on the real model, and is
skipped without the snapshot.
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.infrastructure import (
    embedding_guard,
    encode_worker,
    index_slot,
    token_count_worker,
    worker_process,
)
from faultmaven.infrastructure.embedding_guard import (
    TOKEN_COUNT_SLOTS,
    count_index_tokens,
    embed_texts_or_raise,
)
from faultmaven.infrastructure.index_slot import (
    ENCODE_MEMORY_BYTES,
    IndexAdmission,
    encode_cpu_seconds,
    encode_phases,
    encode_threads,
)
from faultmaven.infrastructure.model_cache import (
    BGE_M3_MAX_SEQ_LENGTH,
    BGE_M3_MODEL_ID,
    ENCODE_BATCH_SIZE,
    model_cache,
)
from faultmaven.models.exceptions import EmbedderUnavailable, KnowledgeBaseError
from tests.unit.infrastructure import model_worker_stubs

pytestmark = [pytest.mark.unit]

USER = "user-1"
_STUBS = model_worker_stubs.__name__
_QUOTA = "faultmaven.infrastructure.model_cache._cgroup_cpu_quota"


@pytest.fixture
def spawned(monkeypatch):
    """Every worker process the test starts."""
    processes: list = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        processes.append(await spawn(*args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    return processes


@pytest.fixture
def encode_stub(monkeypatch):
    """Serve ``model_worker_stubs`` as the encode worker, its stub named by
    the returned setter, with 29 s for its work (``_one_second_of_work``
    leaves a stub that never ends one second)."""
    monkeypatch.setattr(index_slot, "_ENCODE_WORKER", _STUBS)
    monkeypatch.setenv("FM_MODEL_STUB_ROLE", "encode")
    monkeypatch.setattr(index_slot, "ENCODE_START_SECONDS", 30.0)
    monkeypatch.setattr(index_slot, "ENCODE_TRANSFER_SECONDS", 5.0)
    monkeypatch.setattr(index_slot, "REAP_MARGIN_SECONDS", 1.0)
    # The guard of a 0-token batch: the fixed steps (41 s) and the work.
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 70.0)
    return lambda stub: monkeypatch.setenv("FM_MODEL_STUB", stub)


def _one_second_of_work(monkeypatch) -> None:
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 42.0)


@pytest.fixture
def count_stub(monkeypatch):
    monkeypatch.setattr(embedding_guard, "_TOKEN_COUNT_WORKER", _STUBS)
    monkeypatch.setenv("FM_MODEL_STUB_ROLE", "count")
    return lambda stub: monkeypatch.setenv("FM_MODEL_STUB", stub)


@pytest.fixture
async def settled():
    """The slots settle before the next test: bounded, so a slot left blocked
    fails the teardown, not the run."""
    yield
    done = index_slot._EXECUTOR.submit(lambda: None)
    await asyncio.wait_for(asyncio.wrap_future(done), 60)
    assert index_slot.index_admission.queued() == 0, "a KB ticket leaked"


def _kb(texts, tokens=0):
    ticket = index_slot.index_admission.admit_forced(texts, tokens, principal=USER)
    return ticket, embed_texts_or_raise(
        texts, subject="Indexing", operation="t", ticket=ticket
    )


async def _encode(texts, tokens=0):
    ticket, embedding = _kb(texts, tokens)
    async with ticket:
        return await embedding


def _entered_pids(caplog) -> list[int]:
    prefix = model_worker_stubs.ENTERED + " "
    return [
        int(r.getMessage()[len(prefix) :])
        for r in caplog.records
        if r.getMessage().startswith(prefix)
    ]


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# ---------------------------------------------------------------------------
# Pin 2: the deadline kills the worker, and the slot frees
# ---------------------------------------------------------------------------


async def test_an_encode_past_its_phases_is_killed_and_the_next_is_admitted(
    encode_stub, spawned, settled, caplog, monkeypatch
):
    """The guard's bound less the reap margin: the worker is killed and
    reaped, the answer is ``KNOWLEDGE_EMBEDDER_TIMEOUT``, the ticket releases
    and the next encode runs. On ``main`` the encode ran on the slot's thread,
    which nothing stops: the slot was held until it returned."""
    caplog.set_level(logging.WARNING, logger=_STUBS)
    encode_stub("never_returns")
    _one_second_of_work(monkeypatch)

    with pytest.raises(KnowledgeBaseError) as timed_out:
        await _encode(["a"])
    await asyncio.wait_for(_until(lambda: not index_slot.index_admission.queued()), 30)
    encode_stub("vectors")
    monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 70.0)
    after = await _encode(["b"])

    assert timed_out.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert timed_out.value.reason == "encode_timeout"
    assert spawned[0].returncode == -signal.SIGKILL
    assert all(_dead(pid) for pid in _entered_pids(caplog))
    assert after == [[0.25] * 1024]


def test_the_phases_sum_to_the_guard_less_the_reap_margin(monkeypatch):
    monkeypatch.setattr(index_slot, "REAP_MARGIN_SECONDS", 7.0)

    for guard in (180.0, 1_000.0):
        phases = encode_phases(guard)
        total = phases.start + phases.request + phases.work + phases.result

        assert total == pytest.approx(guard - 7.0)


# ---------------------------------------------------------------------------
# Pin 3: the loop turns while a worker holds the interpreter lock
# ---------------------------------------------------------------------------


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


async def _three_turns_with_the_worker_alive(spawned, caplog, run) -> list:
    """Run ``run()``; once the stub logs it has entered, the loop turns three
    times and records whether every worker is alive."""
    loop, alive, tasks = asyncio.get_running_loop(), [], []

    async def three_turns() -> None:
        for _ in range(3):
            await asyncio.sleep(0.005)
        alive.append([p.returncode is None for p in spawned])

    class OnEntered(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # A KB encode's records are emitted on its slot thread's loop.
            if record.getMessage().startswith(model_worker_stubs.ENTERED):
                loop.call_soon_threadsafe(
                    lambda: tasks.append(loop.create_task(three_turns()))
                )

    logging.getLogger(_STUBS).addHandler(handler := OnEntered())
    logging.getLogger(_STUBS).setLevel(logging.WARNING)
    try:
        async with asyncio.timeout(120):
            outcome = await run()
    finally:
        logging.getLogger(_STUBS).removeHandler(handler)
    await asyncio.gather(*tasks)
    return [alive, outcome]


async def test_an_encode_holding_the_lock_in_c_leaves_the_loop_free(
    encode_stub, spawned, settled, caplog, monkeypatch
):
    encode_stub("holds_the_lock_in_c")
    _one_second_of_work(monkeypatch)

    async def run():
        with pytest.raises(KnowledgeBaseError) as ended:
            await _encode(["a"])
        return ended.value.error_code

    alive, code = await _three_turns_with_the_worker_alive(spawned, caplog, run)

    assert alive == [[True]], "the loop did not run beside the encode"
    assert code == "KNOWLEDGE_EMBEDDER_TIMEOUT"
    assert [p.returncode for p in spawned] == [-signal.SIGKILL]


async def test_a_count_holding_the_lock_in_c_leaves_the_loop_free(
    count_stub, spawned, caplog, monkeypatch
):
    count_stub("holds_the_lock_in_c")
    monkeypatch.setattr(
        embedding_guard,
        "TOKEN_COUNT_PHASES",
        worker_process.Phases(start=30.0, request=5.0, work=1.0, result=5.0),
    )

    async def run():
        return await count_index_tokens(["a b"], principal=USER)

    alive, counted = await _three_turns_with_the_worker_alive(spawned, caplog, run)

    assert alive == [[True]], "the loop did not run beside the count"
    assert counted is None
    assert [p.returncode for p in spawned] == [-signal.SIGKILL]


# ---------------------------------------------------------------------------
# How each end of an encode answers, and the transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stub,code",
    [
        ("raises", 1),
        ("panics", 1),
        ("allocates", worker_process.OUT_OF_MEMORY_EXIT),
        ("is_killed", -signal.SIGKILL),
        ("short", None),
        ("garbled", None),
    ],
)
async def test_every_other_end_is_unavailable_and_writes_nothing(
    encode_stub, spawned, settled, stub, code
):
    """A failure, a Rust panic (a ``BaseException``), the memory limit, a
    kill, and an answer of the wrong shape: ``KNOWLEDGE_EMBEDDER_UNAVAILABLE``,
    never vectors. (A worker that answered is reaped once read: its exit
    code is not asserted.)"""
    encode_stub(stub)

    with pytest.raises(KnowledgeBaseError) as ended:
        await _encode(["a", "b"])

    assert ended.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"
    assert ended.value.reason == "worker_failed"
    assert len(spawned) == 1
    if code is not None:
        assert spawned[0].returncode == code


@pytest.mark.parametrize("stub", ["no_model", "model_panics"])
async def test_a_worker_whose_model_would_not_load_says_so(
    encode_stub, spawned, settled, stub
):
    """#2461: the real encode job, its model cache unable to load the model
    (None, or a load that panics, a ``BaseException``): the worker's failure
    text names it, and the guard answers ``model_unavailable``, not the
    ``worker_failed`` of any other failure."""
    encode_stub(stub)

    with pytest.raises(KnowledgeBaseError) as ended:
        await _encode(["a"])

    assert ended.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"
    assert ended.value.reason == "model_unavailable"
    assert [p.returncode for p in spawned] == [1]


async def test_a_worker_that_cannot_start_is_unavailable(
    encode_stub, settled, monkeypatch
):
    encode_stub("vectors")

    async def not_started(*_a, **_k):
        raise OSError("no such file")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", not_started)

    with pytest.raises(KnowledgeBaseError) as ended:
        await _encode(["a"])

    assert ended.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"


async def test_the_vectors_arrive_as_the_worker_encoded_them(
    encode_stub, spawned, settled
):
    """Little-endian float32 through base64, one vector per text, in order;
    the worker ran with the thread count and caps the slot derives."""
    encode_stub("vectors")

    got = await _encode(["a", "b", "c"])

    assert got == [[0.25] * 1024, [1.25] * 1024, [2.25] * 1024]
    assert len(spawned) == 1


# ---------------------------------------------------------------------------
# A caller that goes away kills the worker
# ---------------------------------------------------------------------------


async def test_a_caller_gone_mid_encode_reaps_the_worker_and_frees_the_slot(
    encode_stub, spawned, settled, caplog
):
    caplog.set_level(logging.WARNING, logger=_STUBS)
    encode_stub("never_returns")
    caller = asyncio.create_task(_encode(["a"]))
    await asyncio.wait_for(_until(lambda: _entered_pids(caplog)), 60)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(_until(lambda: not index_slot.index_admission.queued()), 30)

    assert spawned[0].returncode == -signal.SIGKILL
    assert _dead(_entered_pids(caplog)[0])


async def test_a_caller_gone_before_the_task_exists_spawns_no_worker(spawned):
    """``abandon`` lands after the job holds the host lock but before its
    task exists: the task's first step sees it, and no worker starts."""
    admission = IndexAdmission()
    ticket = admission.admit_forced(["a"], 0, principal=USER)
    ticket._future = SimpleNamespace(cancel=lambda: False)  # submitted, started

    ticket.abandon()
    got = await asyncio.to_thread(index_slot.encode_in_worker, ticket, ["a"], 60.0)
    admission._release(ticket)

    assert got is None and spawned == []


# ---------------------------------------------------------------------------
# The token count
# ---------------------------------------------------------------------------


async def test_a_count_waits_for_a_slot_and_past_its_hang_bound_is_queued_too_long(
    monkeypatch,
):
    """Two counts run; a third, of another user, waits for a slot. With none
    free within its hang bound it is ``queued_too_long`` (#2461), never
    ``IndexAdmissionRefused`` busy (#2851 ruling Q2 (a)); freed, a count
    runs."""
    release = asyncio.Event()
    running: list = []

    async def held_run(module, request, **kwargs):
        running.append(module)
        await release.wait()
        return worker_process.WorkerEnd(result=["3"], phase="result")

    monkeypatch.setattr(worker_process, "run", held_run)
    monkeypatch.setattr(embedding_guard, "_count_slots_state", None)
    first = asyncio.create_task(count_index_tokens(["a"], principal="u1"))
    second = asyncio.create_task(count_index_tokens(["b"], principal="u2"))
    await _until(lambda: len(running) == TOKEN_COUNT_SLOTS)
    monkeypatch.setattr(embedding_guard, "TOKEN_COUNT_HANG_SECONDS", 0.05)

    with pytest.raises(EmbedderUnavailable) as third:
        await count_index_tokens(["c"], principal="u3")
    release.set()

    assert third.value.reason == "queued_too_long"
    assert len(running) == TOKEN_COUNT_SLOTS
    assert (await first, await second) == (3, 3)
    assert await count_index_tokens(["d"], principal="u3") == 3


@pytest.mark.parametrize("stub,expected", [("words", 3), ("not_a_count", None)])
async def test_a_count_answers_its_count_or_nothing(count_stub, stub, expected):
    count_stub(stub)

    assert await count_index_tokens(["a b", "c"], principal=USER) == expected


async def test_a_count_runs_for_the_user_who_asked():
    with pytest.raises(ValueError):
        await count_index_tokens(["a"], principal="")


async def test_an_uncounted_kb_document_is_unavailable_naming_no_cause_it_cannot_know():
    """A count that gave no answer: the message says the tokens were not
    counted, never that a tokenizer would not load (owner's ruling on the
    #2919 review). On ``7c5318825`` it said the tokenizer could not be
    loaded."""
    with pytest.raises(KnowledgeBaseError) as refused:
        IndexAdmission().admit(["c"], None, principal=USER, window_seconds=600)

    assert refused.value.error_code == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"
    assert "tokens were not counted" in refused.value.message
    assert "tokenizer" not in refused.value.message


async def test_an_evidence_encode_counts_no_tokens_and_bounds_by_bytes(monkeypatch):
    """No count worker, no count slot: an evidence encode never waits behind
    KB counts (Q1 (b)). On ``7c5318825`` it counted under the user's slot."""
    count = AsyncMock(return_value=7)
    monkeypatch.setattr(embedding_guard, "count_index_tokens", count)
    seen = []
    monkeypatch.setattr(model_cache, "embed_texts_sync", lambda t: [[0.1]] * len(t))
    monkeypatch.setattr(
        embedding_guard, "batch_embed_timeout", lambda n: seen.append(n) or 5.0
    )

    await embed_texts_or_raise(["héllo"], subject="E", operation="t")

    count.assert_not_awaited()
    assert seen == [len("héllo".encode()) + 2]


# ---------------------------------------------------------------------------
# Caps and thread counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quota,cores,threads", [(2.0, 48, 2), (1.5, 48, 2), (None, 48, 4)]
)
def test_the_caps_are_the_stated_formulas_of_the_thread_count(
    monkeypatch, quota, cores, threads
):
    monkeypatch.setattr(_QUOTA, lambda: quota)
    monkeypatch.setattr(os, "cpu_count", lambda: cores)

    assert encode_threads() == threads
    for guard in (180.0, 923.65):
        assert encode_cpu_seconds(guard) == math.ceil(guard * threads) + 10
    assert ENCODE_MEMORY_BYTES == 12 * 1024**3


async def test_the_workers_pin_their_threads_before_the_load(monkeypatch, caplog):
    """Inside a real encode job over a stand-in model, on a node of 32 cores
    with no CPU quota: the load runs with torch at the pinned 2, so its pool
    holds at most the pinned threads and a few more; the Rust pool is the
    pinned size; the real count job's pool likewise. The encode is at
    ``ENCODE_BATCH_SIZE``. On ``7c5318825`` the load ran at 32 threads, and
    the count half set the pin itself, so it could not fail."""
    caplog.set_level(logging.WARNING, logger=_STUBS)
    monkeypatch.setenv("FM_MODEL_STUB", "pins")
    for role in ("encode", "count"):
        monkeypatch.setenv("FM_MODEL_STUB_ROLE", role)
        end = await worker_process.run(
            _STUBS,
            {"threads": 2},
            memory_bytes=ENCODE_MEMORY_BYTES,
            cpu_seconds=600,
            output_limit_bytes=16 * 1024 * 1024,
            phases=worker_process.Phases(120, 10, 120, 10),
            parts=tuple(["a a"] * 200),
        )
        assert end.result is not None, end

    encode, count = (
        json.loads(r.getMessage()[len("pins ") :])
        for r in caplog.records
        if r.getMessage().startswith("pins ")
    )
    assert encode["torch_at_load"] == 2
    assert encode["threads_after_load"] <= 2 + 4, encode
    assert encode["torch_at_encode"] == 2
    assert encode["batch_size"] == ENCODE_BATCH_SIZE
    assert 1 <= encode["rayon_pool"] <= 2
    assert 1 <= count["rayon_pool"] <= 2


def test_the_evidence_encode_runs_in_this_process_at_batch_four(monkeypatch):
    calls = []

    class Model:
        def encode(self, texts, **kwargs):
            calls.append(kwargs)
            import numpy as np

            return np.zeros((len(texts), 2))

    monkeypatch.setattr(model_cache, "get_bge_m3_model", lambda *a: Model())

    assert model_cache.embed_texts_sync(["a", "b"]) == [[0.0, 0.0], [0.0, 0.0]]
    assert calls == [{"batch_size": ENCODE_BATCH_SIZE}] and ENCODE_BATCH_SIZE == 4


# ---------------------------------------------------------------------------
# The real model, behind its snapshot
# ---------------------------------------------------------------------------


def _snapshot_present() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        found = try_to_load_from_cache(BGE_M3_MODEL_ID, "tokenizer.json")
        weights = try_to_load_from_cache(BGE_M3_MODEL_ID, "pytorch_model.bin")
    except Exception:
        return False
    return all(isinstance(p, str) for p in (found, weights))


@pytest.mark.skipif(not _snapshot_present(), reason="no BGE-M3 snapshot here")
async def test_model_workers_on_the_real_model(monkeypatch):
    """The real workers on the real BGE-M3: the encode worker's vectors match
    an in-process encode within 1e-5; the Rust count is at least
    ``AutoTokenizer``'s and at most 2 over it a text, equal on stripped text.
    The test process's torch and transformers are stand-ins, so the in-process
    side runs in a subprocess of its own."""
    monkeypatch.setattr(worker_process, "run", worker_process.run.REAL_RUN)
    texts = [
        "# Restart nginx\n\nRun `systemctl restart nginx`, then check the logs.",
        "Check /var/log/nginx/error.log for upstream timeouts.",
        "﻿  edge whitespace and a zero-width​ space  ",
    ]
    ticket = IndexAdmission().admit_forced(texts, 0, principal=USER)
    async with ticket:
        vectors = await embed_texts_or_raise(
            texts, subject="Indexing", operation="t", ticket=ticket
        )
    counted = await count_index_tokens(texts, principal=USER)
    stripped = await count_index_tokens([t.strip() for t in texts[:2]], principal=USER)

    reference = await _in_process_reference(texts)
    assert len(vectors) == 3 and all(len(v) == 1024 for v in vectors)
    assert (
        max(
            abs(a - b)
            for v, r in zip(vectors, reference["vectors"])
            for a, b in zip(v, r)
        )
        < 1e-5
    )
    assert reference["tokens"] <= counted <= reference["tokens"] + 2 * len(texts)
    assert stripped == reference["stripped_tokens"]


_REFERENCE = """
import json, sys
from transformers import AutoTokenizer
from faultmaven.infrastructure.model_cache import (
    BGE_M3_MAX_SEQ_LENGTH, BGE_M3_MODEL_ID, model_cache,
)
texts = json.loads(sys.stdin.read())
tokenizer = AutoTokenizer.from_pretrained(BGE_M3_MODEL_ID, local_files_only=True)
def count(batch):
    ids = tokenizer(
        batch, add_special_tokens=True, truncation=True,
        max_length=BGE_M3_MAX_SEQ_LENGTH,
    )["input_ids"]
    return sum(len(i) for i in ids)
print(json.dumps({
    "vectors": model_cache.embed_texts_sync(texts),
    "tokens": count(texts),
    "stripped_tokens": count([t.strip() for t in texts[:2]]),
}))
"""


async def _in_process_reference(texts) -> dict:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _REFERENCE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        cwd=worker_process._PACKAGE_ROOT,
        env={**os.environ, "HF_HUB_OFFLINE": "1"},
    )
    out, _ = await proc.communicate(json.dumps(texts).encode())
    assert proc.returncode == 0
    return json.loads(out)


def test_the_real_worker_modules_are_the_defaults():
    assert index_slot._ENCODE_WORKER == encode_worker.__name__
    assert embedding_guard._TOKEN_COUNT_WORKER == token_count_worker.__name__
    assert BGE_M3_MAX_SEQ_LENGTH == 8192
