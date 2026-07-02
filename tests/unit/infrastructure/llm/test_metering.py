"""Unit tests for LLM spend metering (faultmaven.infrastructure.llm.metering).

Covers the two correctness properties that a naive turn-tracker gets wrong:
1. Disjoint token buckets sum correctly and cost is accumulated from the same
   price table the dashboards use.
2. record_provider_call feeds the *active* per-turn tracker (so fallback
   attempts accrue) and is best-effort — a junk response must never raise.
"""

from dataclasses import dataclass
from typing import Optional

import pytest

from faultmaven.infrastructure.llm import metering
from faultmaven.infrastructure.llm.metering import (
    TurnTokenTracker,
    active_token_tracker,
    record_provider_call,
)


@dataclass
class _FakeResponse:
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    prompt_cache_hit: bool = False
    response_time_ms: int = 0


@pytest.mark.unit
class TestTurnTokenTracker:
    def test_total_tokens_sums_disjoint_buckets(self):
        t = TurnTokenTracker()
        t.add(_FakeResponse(input_tokens=100, output_tokens=50, cache_read_tokens=25))
        assert t.total_tokens == 175
        assert t.total_calls == 1

    def test_multiple_calls_accumulate(self):
        t = TurnTokenTracker()
        t.add(_FakeResponse(input_tokens=100, output_tokens=50), cost_usd=0.10)
        t.add(_FakeResponse(input_tokens=200, output_tokens=80), cost_usd=0.20)
        assert t.input_tokens == 300
        assert t.output_tokens == 130
        assert t.total_calls == 2
        assert t.cost_usd == pytest.approx(0.30)

    def test_add_is_not_idempotent(self):
        # By design add() has no id()-based de-dupe (id reuse after GC would
        # silently drop counts). The architecture feeds each call exactly once;
        # feeding the same object twice therefore counts twice.
        t = TurnTokenTracker()
        resp = _FakeResponse(input_tokens=100, output_tokens=50)
        t.add(resp, cost_usd=0.10)
        t.add(resp, cost_usd=0.10)
        assert t.total_calls == 2
        assert t.cost_usd == pytest.approx(0.20)

    def test_none_response_is_ignored(self):
        t = TurnTokenTracker()
        t.add(None)
        assert t.total_calls == 0

    def test_unpriced_calls_counted(self):
        t = TurnTokenTracker()
        t.add(_FakeResponse(input_tokens=100), cost_usd=0.0, priced=False)
        assert t.unpriced_calls == 1


@pytest.mark.unit
class TestRecordProviderCall:
    def test_feeds_active_tracker_with_cost(self):
        tracker = TurnTokenTracker()
        token = active_token_tracker.set(tracker)
        try:
            resp = _FakeResponse(
                provider="anthropic",
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,  # $3 at input rate
                output_tokens=0,
                tokens_used=1_000_000,
            )
            record_provider_call("anthropic", "claude-sonnet-4-6", resp, 12.0)
        finally:
            active_token_tracker.reset(token)

        assert tracker.total_calls == 1
        assert tracker.input_tokens == 1_000_000
        assert tracker.cost_usd == pytest.approx(3.0)
        assert tracker.unpriced_calls == 0

    def test_unknown_model_marks_unpriced_on_tracker(self):
        tracker = TurnTokenTracker()
        token = active_token_tracker.set(tracker)
        try:
            resp = _FakeResponse(
                provider="anthropic", model="mystery-model", input_tokens=1000
            )
            record_provider_call("anthropic", "mystery-model", resp, 1.0)
        finally:
            active_token_tracker.reset(token)
        assert tracker.unpriced_calls == 1
        assert tracker.cost_usd == 0.0

    def test_no_active_tracker_is_safe(self):
        # Outside a turn there is no tracker; metering must still not raise.
        assert active_token_tracker.get() is None
        record_provider_call("anthropic", "claude-sonnet-4-6", _FakeResponse(), 1.0)

    def test_junk_response_never_raises(self):
        # Best-effort contract: a metering failure must not break a request.
        class Broken:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        # Should swallow the error internally.
        record_provider_call("anthropic", "x", Broken(), 1.0)

    def test_low_confidence_outcome_label_accepted(self):
        # Just exercises the discarded-attempt path (no exception, tracker fed).
        tracker = TurnTokenTracker()
        token = active_token_tracker.set(tracker)
        try:
            record_provider_call(
                "openai",
                "gpt-5.4-mini",
                _FakeResponse(provider="openai", model="gpt-5.4-mini", input_tokens=10),
                1.0,
                outcome="low_confidence",
            )
        finally:
            active_token_tracker.reset(token)
        assert tracker.total_calls == 1
