"""An indexing embed is refused before it starts when its caller's budget cannot fit it (#2055).

Invariant: no indexing embed starts on a batch that cannot finish inside its
caller's budget at this deployment's rate. 413 when the batch alone exceeds the
budget, 503 + Retry-After when queued work or a cold model is in the way. Any
admitted batch is bounded by at least twice its estimate plus a model load, and
indexing embeds run one at a time.

No wall clock anywhere: the rate, the cores, the model's residency and the
guard's own clock are patched, and the encoder is a double.
"""

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.core.preprocessing.vector_storage import (
    VectorIndexOutcome,
    store_in_vector_db_background,
)
from faultmaven.infrastructure import embedding_guard as guard
from faultmaven.infrastructure.embedding_guard import (
    EMBED_BATCH_LOAD_SECONDS,
    INDEX_CORE_SECONDS_PER_BYTE,
    batch_embed_timeout,
    check_index_budget,
    embed_texts_or_raise,
    estimated_index_seconds,
    index_budget_refusal,
)
from faultmaven.models.exceptions import IndexBudgetRefused, KnowledgeBaseError

pytestmark = [pytest.mark.unit]

_CACHE = "faultmaven.infrastructure.model_cache"
_ENCODE = f"{_CACHE}.model_cache.aembed_texts"


@pytest.fixture
def one_core():
    """One core, the measured rate, the model already loaded."""
    with (
        patch(f"{_CACHE}.inference_cores", return_value=1.0),
        patch.object(guard, "_rate", guard._LearnedRate()),
        patch(f"{_CACHE}.model_cache.peek_bge_m3_model", return_value=object()),
    ):
        yield


async def _hang(texts):
    await asyncio.sleep(3600)


# --- the estimate -----------------------------------------------------------


def test_the_estimate_is_linear_in_bytes_and_divides_by_at_most_four_cores():
    with patch.object(guard, "_rate", guard._LearnedRate()):
        for cores, divisor in ((1.0, 1.0), (2.0, 2.0), (4.0, 4.0), (48.0, 4.0)):
            with patch(f"{_CACHE}.inference_cores", return_value=cores):
                one = estimated_index_seconds(["x" * 1000])
                assert one == pytest.approx(
                    INDEX_CORE_SECONDS_PER_BYTE * 1000 / divisor
                )
                assert estimated_index_seconds(["x" * 1000] * 3) == pytest.approx(
                    3 * one
                )


def test_a_fractional_quota_is_a_fraction_of_a_core_not_one_thread():
    """Staging's 250m pins one thread that runs a quarter of the time."""
    with (
        patch(f"{_CACHE}._cgroup_cpu_quota", return_value=0.25),
        patch.object(guard, "_rate", guard._LearnedRate()),
    ):
        assert estimated_index_seconds(["x" * 1000]) == pytest.approx(
            4 * INDEX_CORE_SECONDS_PER_BYTE * 1000
        )


def test_text_is_measured_in_utf8_bytes(one_core):
    cjk = "日本語のログ" * 100  # three bytes a character
    assert estimated_index_seconds([cjk]) == pytest.approx(
        estimated_index_seconds(["x" * len(cjk.encode())])
    )
    assert estimated_index_seconds([cjk]) == pytest.approx(
        3 * estimated_index_seconds(["x" * len(cjk)])
    )


def test_the_learned_rate_moves_after_a_batch_and_never_drops_below_the_floor():
    rate = guard._LearnedRate()
    rate.learn(elapsed=100.0, nbytes=1000, cores=1.0)  # 0.1 core-s a byte
    assert rate.core_seconds_per_byte == pytest.approx(
        0.2 * 0.1 + 0.8 * INDEX_CORE_SECONDS_PER_BYTE
    )
    for _ in range(50):
        rate.learn(elapsed=0.001, nbytes=1000, cores=1.0)
    assert rate.core_seconds_per_byte == INDEX_CORE_SECONDS_PER_BYTE
    assert rate.samples == 51


async def test_a_completed_warm_batch_teaches_the_rate_and_a_cold_one_does_not(
    one_core,
):
    ticks = [0.0, 100.0, 200.0, 300.0]
    clock = SimpleNamespace(monotonic=lambda: ticks.pop(0))
    with (
        patch(_ENCODE, new=AsyncMock(return_value=[[0.0]])),
        patch.object(guard, "time", clock),
    ):
        await embed_texts_or_raise(["x" * 1000], subject="t", operation="t")
        assert guard._rate.samples == 1
        assert guard._rate.core_seconds_per_byte > INDEX_CORE_SECONDS_PER_BYTE
        with patch(f"{_CACHE}.model_cache.peek_bge_m3_model", return_value=None):
            await embed_texts_or_raise(["x" * 1000], subject="t", operation="t")
    assert guard._rate.samples == 1, "a cold load is not a sample of the rate"


# --- the refusal -------------------------------------------------------------


async def test_the_refusal_fires_exactly_above_the_budget(one_core):
    texts = ["x" * 10_000]
    estimate = estimated_index_seconds(texts)

    assert index_budget_refusal(texts, budget_seconds=estimate) is None
    assert index_budget_refusal(texts, budget_seconds=None) is None
    refused = index_budget_refusal(texts, budget_seconds=estimate - 0.01)

    assert refused.retry_after_seconds is None
    assert refused.error_code == "KNOWLEDGE_DOCUMENT_TOO_LARGE"
    assert "about 10 KB" in refused.message and "Split it" in refused.message


async def test_the_cold_band_is_503_and_only_a_document_that_never_fits_is_413(
    one_core,
):
    """A cold model used to flip a document that fits warm into "too large"."""
    texts = ["x" * 10_000]
    budget = estimated_index_seconds(texts) + EMBED_BATCH_LOAD_SECONDS / 2
    never = ["x" * 100_000]
    assert index_budget_refusal(texts, budget_seconds=budget) is None  # warm
    assert (
        index_budget_refusal(never, budget_seconds=budget).retry_after_seconds is None
    )

    loads = []
    with (
        patch(f"{_CACHE}.model_cache.peek_bge_m3_model", return_value=None),
        patch(
            f"{_CACHE}.model_cache.get_bge_m3_model",
            side_effect=lambda *_: loads.append(1),
        ),
    ):
        cold = index_budget_refusal(texts, budget_seconds=budget)
        index_budget_refusal(texts, budget_seconds=budget)
        assert (
            index_budget_refusal(never, budget_seconds=budget).retry_after_seconds
            is None
        )
        await guard._queue().warming

    assert cold.error_code == "KNOWLEDGE_INDEXING_BUSY"
    assert cold.retry_after_seconds == math.ceil(EMBED_BATCH_LOAD_SECONDS)
    assert loads == [1], "the refusal starts one load, not one per refusal"


def test_any_admitted_batch_is_bounded_by_twice_its_estimate_plus_a_load(one_core):
    admitted = 0
    for size in (100, 3_000, 50_000):
        texts = ["x" * size]
        for budget in (10.0, 60.0, 600.0):
            if index_budget_refusal(texts, budget_seconds=budget) is None:
                admitted += 1
                assert batch_embed_timeout(texts) >= (
                    2 * estimated_index_seconds(texts) + EMBED_BATCH_LOAD_SECONDS
                )
    assert admitted >= 3, "positive control: some batches are admitted"


async def test_window_zero_refuses_nothing_and_the_guard_still_bounds_the_embed(
    one_core, monkeypatch
):
    from faultmaven.config.settings import get_settings

    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 0)
    assert guard.request_window_seconds() is None

    with (
        patch(_ENCODE, new=_hang),
        patch.object(guard, "EMBED_BATCH_LOAD_SECONDS", 0.05),
        patch.object(guard._rate, "core_seconds_per_byte", 0.0),
        pytest.raises(KnowledgeBaseError) as timed_out,
    ):
        texts = ["x" * 1_000_000]
        check_index_budget(texts, budget_seconds=guard.request_window_seconds())
        await embed_texts_or_raise(texts, subject="Indexing", operation="t")
    assert timed_out.value.error_code == "KNOWLEDGE_EMBEDDER_TIMEOUT"


# --- the queue ---------------------------------------------------------------


async def test_indexing_embeds_run_one_at_a_time_and_queued_work_is_charged(
    one_core,
):
    gate, running, peak = asyncio.Event(), [], []

    async def _encode(texts):
        running.append(texts[0][0])
        peak.append(len(running))
        await gate.wait()
        running.remove(texts[0][0])
        return [[0.0]] * len(texts)

    first, second, third = ["a" * 10_000], ["b" * 10_000], ["c" * 1_000]
    estimate = estimated_index_seconds(first)
    with (
        patch(_ENCODE, new=_encode),
        patch.object(guard, "time", SimpleNamespace(monotonic=lambda: 1000.0)),
    ):
        a = asyncio.create_task(embed_texts_or_raise(first, subject="A", operation="t"))
        b = asyncio.create_task(
            embed_texts_or_raise(second, subject="B", operation="t")
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert running == ["a"], "the second encode waits for the first"

        with pytest.raises(IndexBudgetRefused) as busy:
            check_index_budget(third, budget_seconds=2 * estimate)
        assert busy.value.retry_after_seconds == math.ceil(2 * estimate)
        assert busy.value.error_code == "KNOWLEDGE_INDEXING_BUSY"
        assert (
            index_budget_refusal(
                third, budget_seconds=2 * estimate + estimated_index_seconds(third)
            )
            is None
        ), "the same document fits once the budget covers the queue"

        gate.set()
        await asyncio.gather(a, b)
    assert max(peak) == 1


# --- evidence vectorization ---------------------------------------------------


async def _vectorize(budget_seconds):
    store = SimpleNamespace(add_documents=AsyncMock())
    outcome = await store_in_vector_db_background(
        case_id="c",
        evidence_id="e",
        structural_index="=== Errors ===\n" + "OOM killed pid 4242\n" * 2000,
        data_type=UnifiedDataType.LOGS,
        metadata={},
        case_vector_store=store,
        max_chunk_tokens=500,
        overlap_tokens=50,
        budget_seconds=budget_seconds,
    )
    return outcome, store


async def test_evidence_too_large_for_its_budget_is_refused_before_any_embed(
    one_core,
):
    encode = AsyncMock(return_value=[])
    with patch(_ENCODE, new=encode):
        outcome, store = await _vectorize(budget_seconds=1.0)

    assert outcome is VectorIndexOutcome.TOO_LARGE
    encode.assert_not_awaited()
    store.add_documents.assert_not_awaited()


async def test_a_hung_evidence_embed_fails_instead_of_hanging(one_core):
    with (
        patch(_ENCODE, new=_hang),
        patch.object(guard, "EMBED_BATCH_LOAD_SECONDS", 0.05),
        patch.object(guard._rate, "core_seconds_per_byte", 0.0),
    ):
        outcome, store = await _vectorize(budget_seconds=None)

    assert outcome is VectorIndexOutcome.FAILED
    store.add_documents.assert_not_awaited()
