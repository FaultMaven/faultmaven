"""KB indexing is priced by each chunk's length, and long chunks encode alone
(#2920, PR 2 of the ruled split).

One rate priced every token alike, so a cap-length chunk was estimated at
about 1/1.6 of its cost: its admission, 413 line and guard bound understated
it. And the encode pads each batch of ``ENCODE_BATCH_SIZE`` to its longest
text, so a cap-length chunk batched with three short ones cost about 4x its
own price. Now ``priced_tokens`` charges a chunk over the breakpoint at a rate
rising to 40 core-ms at the cap, and ``encode_texts`` encodes it alone.

The count worker runs for real (``worker_process``) over a stand-in tokenizer
(``model_worker_stubs``' ``pins``, one token a word); no test loads BGE-M3,
and none times anything. The prose oracle was captured from ``main`` at
``519a45875`` by running its own ``IndexAdmission.admit`` and
``batch_embed_timeout`` (``admission_oracle_2920.json``); it passes on
``main`` by construction, and pins that prose decisions did not move.
"""

import asyncio
import json
import re
from pathlib import Path

import numpy as np
import pytest

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure import embedding_guard, index_slot, model_cache
from faultmaven.infrastructure.embedding_guard import (
    batch_embed_timeout,
    count_index_tokens,
    embed_texts_or_raise,
)
from faultmaven.infrastructure.index_slot import IndexAdmission, index_max_tokens
from faultmaven.infrastructure.model_cache import (
    BGE_M3_MAX_SEQ_LENGTH,
    ENCODE_BATCH_SIZE,
    INDEX_RATE_BREAKPOINT_TOKENS,
    priced_tokens,
)
from faultmaven.models.exceptions import IndexAdmissionRefused
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.unit.infrastructure import model_worker_stubs

pytestmark = [pytest.mark.unit]

B = INDEX_RATE_BREAKPOINT_TOKENS
USER = "user-1"
_ORACLE = Path(__file__).with_name("admission_oracle_2920.json")
_PACK = Path(__file__).parents[3] / "resources/knowledge/pack/pack.json"

#: A cap-length chunk, 8,192 tokens to the stand-in tokenizer, priced 13,108:
#: 40 core-ms a token at the base rate's 25, rounded up.
CAP = "a " * BGE_M3_MAX_SEQ_LENGTH
CAP_PRICED = 13_108


@pytest.fixture
def two_cores(monkeypatch):
    monkeypatch.setattr(model_cache, "_cgroup_cpu_quota", lambda: 2.0)


@pytest.fixture
def real_count(monkeypatch, two_cores):
    """The real count job in a real worker, over the stand-in tokenizer."""
    monkeypatch.setattr(
        embedding_guard, "_TOKEN_COUNT_WORKER", model_worker_stubs.__name__
    )
    monkeypatch.setenv("FM_MODEL_STUB_ROLE", "count")
    monkeypatch.setenv("FM_MODEL_STUB", "pins")
    monkeypatch.setattr(embedding_guard, "_count_slots_state", None)


@pytest.fixture
async def settled():
    yield
    done = index_slot._EXECUTOR.submit(lambda: None)
    await asyncio.wait_for(asyncio.wrap_future(done), 30)
    assert index_slot.index_admission.queued() == 0, "a KB ticket leaked"


def _window(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(
        get_settings().knowledge, "index_request_window_seconds", seconds
    )


# ---------------------------------------------------------------------------
# The price
# ---------------------------------------------------------------------------


def test_the_price_is_the_count_to_the_breakpoint_then_rises_to_40_core_ms():
    priced = [priced_tokens(n) for n in range(0, BGE_M3_MAX_SEQ_LENGTH + 1)]

    assert priced[: B + 1] == list(range(B + 1))
    assert priced[B + 1] > B + 1
    assert all(p >= n for n, p in enumerate(priced))
    assert all(a <= b for a, b in zip(priced, priced[1:]))
    assert priced[BGE_M3_MAX_SEQ_LENGTH] == CAP_PRICED  # ceil(8,192 x 8/5)
    assert priced[6000] == 7674  # 32.0 core-ms a token, measured 26.3


def test_every_shipped_pack_chunk_prices_as_its_count():
    """A token spans at least a byte, bar the two special tokens, so a chunk's
    UTF-8 bytes plus 2 bound its count: every pack chunk is at or below the
    breakpoint, and admitted, refused and bounded as on ``main``."""
    pack = json.loads(_PACK.read_text(encoding="utf-8"))
    chunks = [c["text"] for runbook in pack["runbooks"] for c in runbook["chunks"]]
    bound = max(len(text.encode("utf-8")) + 2 for text in chunks)

    assert len(chunks) > 1000 and bound <= B
    assert all(priced_tokens(n) == n for n in range(bound + 1))


async def test_the_count_worker_answers_the_count_and_the_priced_sum(real_count):
    counted = await count_index_tokens([CAP, "a a", "a " * 5000], principal=USER)

    assert counted == (8192 + 2 + 5000, CAP_PRICED + 2 + priced_tokens(5000))
    assert await count_index_tokens([], principal=USER) == (0, 0)


# ---------------------------------------------------------------------------
# Long chunks encode alone
# ---------------------------------------------------------------------------


class _Model:
    """Records each ``encode`` call; a text's vector is its word count."""

    def __init__(self):
        self.calls = []

    def tokenizer(self, texts, truncation, max_length):
        assert truncation and max_length == BGE_M3_MAX_SEQ_LENGTH
        return {"input_ids": [text.split()[:max_length] for text in texts]}

    def encode(self, texts, batch_size):
        self.calls.append(([len(t.split()) for t in texts], batch_size))
        return np.array([[float(len(t.split()))] * 2 for t in texts], dtype="f4")


def test_a_long_chunk_encodes_alone_and_the_vectors_keep_their_order(monkeypatch):
    """On ``main`` the six texts were one ``encode`` at batch 4, which pads
    the short ones to the longest."""
    model = _Model()
    monkeypatch.setattr(model_cache.model_cache, "get_bge_m3_model", lambda *a: model)
    texts = ["a " * n for n in (5, B + 1, 7, B, 9000, 3)]

    vectors = model_cache.model_cache.embed_texts_sync(texts)

    assert model.calls == [
        ([B + 1], 1),
        ([9000], 1),  # truncated at the cap by the model, not here
        ([5, 7, B, 3], ENCODE_BATCH_SIZE),
    ]
    assert [v[0] for v in vectors] == [5, B + 1, 7, B, 9000, 3]


def test_prose_encodes_as_one_batched_call(monkeypatch):
    model = _Model()
    monkeypatch.setattr(model_cache.model_cache, "get_bge_m3_model", lambda *a: model)

    vectors = model_cache.model_cache.embed_texts_sync(["a " * 10, "a " * B])

    assert model.calls == [([10, B], ENCODE_BATCH_SIZE)]
    assert [v[0] for v in vectors] == [10, B]


# ---------------------------------------------------------------------------
# Admission and the guard
# ---------------------------------------------------------------------------


def _decide(row) -> dict:
    """``row`` through this tree's admission, as the oracle recorded it."""
    admission = IndexAdmission()
    ahead = row["ahead"]
    held = admission.admit_forced(["x"], ahead, principal="u0") if ahead else None
    decided = {"guard": batch_embed_timeout(row["tokens"])}
    try:
        ticket = admission.admit(
            ["c"],
            row["tokens"],
            plain_tokens=row["tokens"],
            principal="u1",
            window_seconds=row["window"],
            size_limit_seconds=row["limit"],
        )
        decided.update(outcome="admitted", estimate=ticket.estimate_seconds)
        ticket.close()
    except IndexAdmissionRefused as e:
        decided.update(
            outcome=e.reason,
            status=e.status_code,
            retry_after=e.retry_after_seconds,
            figures=re.findall(r"\d[\d,]*", e.message),
        )
    if held:
        held.close()
    return decided


def test_prose_decisions_equal_mains(monkeypatch):
    """Priced equal to plain: every decision, 413 figure, ``Retry-After``,
    estimate and guard bound is ``main``'s at ``519a45875``, at whole and
    fractional cores, at ``M - 1``, ``M`` and ``M + 1``, with a ticket ahead
    and without, and on verify-batch's ``size_limit_seconds`` path (413 is a
    ``failed`` draft, 503 a ``deferred`` one)."""
    oracle = json.loads(_ORACLE.read_text(encoding="utf-8"))
    monkeypatch.setattr(index_slot, "_clock", lambda: 0.0)
    outcomes = set()
    for row in oracle["rows"]:
        monkeypatch.setattr(model_cache, "_cgroup_cpu_quota", lambda c=row["cores"]: c)
        expected = {k: v for k, v in row.items() if k not in _INPUTS}
        assert _decide(row) == expected, row
        outcomes.add(row["outcome"])

    assert oracle["commit"].startswith("519a45875")
    assert outcomes == {"admitted", "busy", "too_large"}


_INPUTS = ("cores", "window", "limit", "ahead", "tokens")


async def test_a_cap_length_document_under_the_plain_line_is_refused_413(
    monkeypatch, real_count, settled
):
    """Three cap-length chunks: 24,576 tokens, under the 400 s line of 29,440
    at 2 cores, but priced 39,324. On ``main`` it was admitted and estimated
    at 1/1.6 of its cost; now it is 413, naming the most of text like it."""
    _window(monkeypatch, 400)
    line = index_max_tokens(400 - 30 - 2)
    assert line == 29_440

    with pytest.raises(IndexAdmissionRefused) as refused:
        await KnowledgeService._index_ticket([CAP] * 3, principal=USER)

    most = 24_576 * line // (3 * CAP_PRICED)
    assert (refused.value.status_code, refused.value.reason) == (413, "too_large")
    assert refused.value.message == (
        f"This document is 24,576 tokens, and this server can index at most "
        f"{most:,} tokens of text like it in one request. Split it into "
        "smaller documents. Nothing was saved."
    )
    assert most < 24_576


async def test_the_ticket_holds_the_priced_count_and_the_guard_bounds_by_it(
    monkeypatch, real_count, settled
):
    """Admitted, the ticket's tokens, estimate and guard bound are the priced
    figure's; with no window the guard counts and takes the priced figure
    too. On ``main`` both were the plain count's."""
    bounds = []

    def encode(_ticket, texts, guard_seconds):
        bounds.append(guard_seconds)
        return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(index_slot, "encode_in_worker", encode)
    priced = CAP_PRICED + 2
    for window in (3600, 0):
        _window(monkeypatch, window)
        async with await KnowledgeService._index_ticket(
            [CAP, "a a"], principal=USER
        ) as ticket:
            await embed_texts_or_raise(
                [CAP, "a a"], subject="Indexing", operation="t", ticket=ticket
            )
        if window:
            assert ticket.tokens == priced
            assert ticket.estimate_seconds == pytest.approx(
                priced * 0.025 / 2 + index_slot.WORKER_LOAD_SECONDS
            )

    assert bounds == [batch_embed_timeout(priced)] * 2
    assert batch_embed_timeout(priced) > batch_embed_timeout(8192 + 2)


async def test_with_no_count_a_kb_bound_prices_the_bytes_and_evidence_does_not(
    monkeypatch, two_cores, settled
):
    """No window and a count that gave no answer: a KB ticket's guard prices
    each text's UTF-8 bytes plus 2, cut at the cap, so a cap-length chunk is
    not bounded 1.6x under its cost; an evidence encode's stays at plain bytes
    plus 2 (Q2)."""
    bounds = {}

    def kb(_ticket, texts, guard_seconds):
        bounds["kb"] = guard_seconds
        return [[0.0] * 1024 for _ in texts]

    def evidence(_slot, _ticket, texts, guard_seconds):
        bounds["evidence"] = guard_seconds
        return [[0.0] * 1024 for _ in texts]

    async def no_answer(*_args, **_kwargs):
        return None

    monkeypatch.setattr(index_slot, "encode_in_worker", kb)
    monkeypatch.setattr(index_slot.EncodeSlot, "_encode", evidence)
    monkeypatch.setattr(embedding_guard, "count_index_tokens", no_answer)
    async with IndexAdmission().admit_forced([CAP], None, principal=USER) as ticket:
        await embed_texts_or_raise([CAP], subject="I", operation="t", ticket=ticket)
    await embed_texts_or_raise([CAP], subject="I", operation="t")

    size = len(CAP.encode("utf-8")) + 2
    assert bounds == {
        "kb": batch_embed_timeout(CAP_PRICED),  # its bytes, cut at the cap, priced
        "evidence": batch_embed_timeout(size),
    }


async def test_with_no_count_a_kb_bound_prices_a_long_chunk_at_the_cap(
    monkeypatch, two_cores, settled
):
    """The count and the encode both truncate at the cap, so a 100,000-byte
    chunk's byte fallback is priced as 8,192 tokens, never extrapolated past
    the cap."""
    bounds = []

    def kb(_ticket, texts, guard_seconds):
        bounds.append(guard_seconds)
        return [[0.0] * 1024 for _ in texts]

    async def no_answer(*_args, **_kwargs):
        return None

    monkeypatch.setattr(index_slot, "encode_in_worker", kb)
    monkeypatch.setattr(embedding_guard, "count_index_tokens", no_answer)
    text = "a" * 100_000
    async with IndexAdmission().admit_forced([text], None, principal=USER) as ticket:
        await embed_texts_or_raise([text], subject="I", operation="t", ticket=ticket)

    assert bounds == [batch_embed_timeout(priced_tokens(BGE_M3_MAX_SEQ_LENGTH))]


def test_an_empty_count_under_a_short_limit_is_refused_413_as_on_main(two_cores):
    """A limit under the load and count allowances leaves a negative line, so
    even 0 tokens is over it: refused 413 naming that line, as on ``main``,
    never a division by zero."""
    line = index_max_tokens(20 - 30 - 2)

    with pytest.raises(IndexAdmissionRefused) as refused:
        IndexAdmission().admit(["c"], 0, principal=USER, window_seconds=20)

    assert line < 0 and refused.value.status_code == 413
    assert (
        refused.value.message
        == index_slot.refusal("too_large", tokens=0, most=line).message
    )
