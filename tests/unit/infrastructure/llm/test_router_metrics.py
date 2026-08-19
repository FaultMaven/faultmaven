"""Unit tests for LLMRouter Prometheus + SLA metrics recording.

Verifies that route() records llm_requests / llm_latency / llm_tokens
(shim metrics) and sla_tracker observations on the success, cached, and
error paths.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers import LLMResponse


def _make_response(**overrides):
    defaults = dict(
        content="test response",
        confidence=0.9,
        provider="openai",
        model="gpt-4",
        tokens_used=42,
        response_time_ms=250,
        cached=False,
    )
    defaults.update(overrides)
    return LLMResponse(**defaults)


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_available_providers.return_value = ["openai"]
    registry.get_fallback_chain.return_value = ["openai"]
    registry.route_request = AsyncMock(return_value=_make_response())
    return registry


@pytest.fixture
def router(mock_registry):
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry",
        return_value=mock_registry,
    ):
        from faultmaven.infrastructure.llm.router import LLMRouter

        r = LLMRouter()
        # Cache is mocked out so tests control hit/miss deterministically
        r.cache = MagicMock()
        r.cache.check = MagicMock(return_value=None)
        r.cache.store = MagicMock()
        # registry is a @property calling get_registry() each time,
        # so the patch must stay active for the test's lifetime.
        yield r


@pytest.fixture
def metrics(monkeypatch):
    """Replace the router module's metric shims + sla_tracker with mocks."""
    import faultmaven.infrastructure.llm.router as router_module

    mocks = SimpleNamespace(
        llm_requests=MagicMock(),
        llm_latency=MagicMock(),
        llm_tokens=MagicMock(),
        llm_stop_reasons=MagicMock(),
        sla_tracker=MagicMock(),
    )
    monkeypatch.setattr(router_module, "llm_requests", mocks.llm_requests)
    monkeypatch.setattr(router_module, "llm_latency", mocks.llm_latency)
    monkeypatch.setattr(router_module, "llm_tokens", mocks.llm_tokens)
    monkeypatch.setattr(router_module, "llm_stop_reasons", mocks.llm_stop_reasons)
    monkeypatch.setattr(router_module, "sla_tracker", mocks.sla_tracker)
    return mocks


@pytest.mark.unit
@pytest.mark.llm
class TestSuccessPathMetrics:
    """Successful route() calls record request, latency, tokens, and SLA."""

    @pytest.mark.asyncio
    async def test_success_records_all_metrics(self, router, metrics):
        await router.route(prompt="test", model="gpt-4")

        metrics.llm_requests.labels.assert_called_once_with(
            provider="openai", model="gpt-4", status="success"
        )
        metrics.llm_requests.labels.return_value.inc.assert_called_once_with()

        metrics.llm_latency.labels.assert_called_once_with(
            provider="openai", model="gpt-4"
        )
        metrics.llm_latency.labels.return_value.observe.assert_called_once_with(0.25)

        metrics.llm_tokens.labels.assert_called_once_with(
            provider="openai", model="gpt-4"
        )
        metrics.llm_tokens.labels.return_value.inc.assert_called_once_with(42)

        metrics.sla_tracker.record_request_metrics.assert_called_once_with(
            "llm_provider", 250, success=True
        )

    @pytest.mark.asyncio
    async def test_zero_tokens_skips_token_counter(
        self, router, mock_registry, metrics
    ):
        mock_registry.route_request = AsyncMock(
            return_value=_make_response(tokens_used=0)
        )

        await router.route(prompt="test", model="gpt-4")

        metrics.llm_tokens.labels.assert_not_called()
        # Other metrics are still recorded
        metrics.llm_requests.labels.assert_called_once_with(
            provider="openai", model="gpt-4", status="success"
        )
        metrics.llm_latency.labels.return_value.observe.assert_called_once()
        metrics.sla_tracker.record_request_metrics.assert_called_once()


@pytest.mark.unit
@pytest.mark.llm
class TestErrorPathMetrics:
    """Failed route() calls record an error metric and SLA failure."""

    @pytest.mark.asyncio
    async def test_error_records_failure_and_propagates(
        self, router, mock_registry, metrics
    ):
        mock_registry.route_request = AsyncMock(
            side_effect=RuntimeError("all providers down")
        )

        with pytest.raises(RuntimeError, match="all providers down"):
            await router.route(prompt="test", model="gpt-4")

        metrics.llm_requests.labels.assert_called_once_with(
            provider="unknown", model="gpt-4", status="error"
        )
        metrics.llm_requests.labels.return_value.inc.assert_called_once_with()

        metrics.sla_tracker.record_request_metrics.assert_called_once()
        call = metrics.sla_tracker.record_request_metrics.call_args
        assert call.args[0] == "llm_provider"
        assert call.args[1] >= 0.0  # elapsed ms
        assert call.kwargs == {"success": False}

        # No latency/token metrics on failure
        metrics.llm_latency.labels.assert_not_called()
        metrics.llm_tokens.labels.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_without_model_labels_unknown(
        self, router, mock_registry, metrics
    ):
        mock_registry.route_request = AsyncMock(side_effect=RuntimeError("down"))

        with pytest.raises(RuntimeError):
            await router.route(prompt="test")

        metrics.llm_requests.labels.assert_called_once_with(
            provider="unknown", model="unknown", status="error"
        )


@pytest.mark.unit
@pytest.mark.llm
class TestCachedPathMetrics:
    """Cache hits record a cached request and nothing else."""

    @pytest.mark.asyncio
    async def test_cached_records_only_cached_status(
        self, router, mock_registry, metrics
    ):
        router.cache.check = MagicMock(
            return_value=_make_response(content="cached", cached=True)
        )

        result = await router.route(prompt="test", model="gpt-4")

        assert result.cached is True
        mock_registry.route_request.assert_not_called()

        metrics.llm_requests.labels.assert_called_once_with(
            provider="openai", model="gpt-4", status="cached"
        )
        metrics.llm_requests.labels.return_value.inc.assert_called_once_with()

        # No latency, token, or SLA recording for cache hits
        metrics.llm_latency.labels.assert_not_called()
        metrics.llm_tokens.labels.assert_not_called()
        metrics.sla_tracker.record_request_metrics.assert_not_called()


@pytest.mark.unit
@pytest.mark.llm
class TestStopReasonMetric:
    """Truncation must be countable, not inferred (#1094).

    The issue that motivated this could only estimate the truncation rate by
    looking for token counts landing *exactly* on a cap — a proxy that misses
    every cut that stopped a token short. The router is the one chokepoint
    every routed call passes, so recording the reason here is what turns that
    estimate into a measurement.
    """

    @pytest.mark.asyncio
    async def test_the_reason_is_recorded_on_every_call(self, router, metrics):
        from faultmaven.infrastructure.llm.providers import StopReason

        await router.route(prompt="test", model="gpt-4")

        metrics.llm_stop_reasons.labels.assert_called_once_with(
            provider="openai", model="gpt-4", stop_reason=StopReason.UNKNOWN.value
        )

    @pytest.mark.asyncio
    async def test_a_truncated_response_is_labelled_max_tokens(
        self, router, mock_registry, metrics
    ):
        from faultmaven.infrastructure.llm.providers import StopReason

        mock_registry.route_request = AsyncMock(
            return_value=_make_response(stop_reason=StopReason.MAX_TOKENS)
        )

        await router.route(prompt="test", model="gpt-4")

        metrics.llm_stop_reasons.labels.assert_called_once_with(
            provider="openai", model="gpt-4", stop_reason="max_tokens"
        )

    @pytest.mark.asyncio
    async def test_a_truncated_response_is_also_logged(
        self, router, mock_registry, metrics, caplog
    ):
        """Metrics answer "how often"; the log answers "which call".

        Both are needed — a truncation rate with no way to find an instance is
        not actionable.
        """
        import logging

        from faultmaven.infrastructure.llm.providers import StopReason

        mock_registry.route_request = AsyncMock(
            return_value=_make_response(stop_reason=StopReason.MAX_TOKENS)
        )

        with caplog.at_level(logging.WARNING):
            await router.route(prompt="test", model="gpt-4")

        assert any("truncated" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.unit
@pytest.mark.llm
class TestTruncatedResponsesAreNotCached:
    """An incomplete answer is never worth replaying (#1094).

    Two failures, one write. Storing a cut body poisons the key until something
    happens to overwrite it — and because ``max_tokens`` is not part of the
    cache key, that stored body is what a retry at a BIGGER cap gets served
    instead of reaching the provider. "Retry with more room" silently becomes
    "return the same cut body", and the caller cannot tell the difference from
    a genuine second truncation.
    """

    @pytest.mark.asyncio
    async def test_a_truncated_response_is_not_stored(
        self, router, mock_registry, metrics
    ):
        from faultmaven.infrastructure.llm.providers import StopReason

        mock_registry.route_request = AsyncMock(
            return_value=_make_response(stop_reason=StopReason.MAX_TOKENS)
        )

        await router.route(prompt="test", model="gpt-4")

        router.cache.store.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_complete_response_is_still_stored(self, router, metrics):
        """Negative control — the cache must not be disabled wholesale."""
        from faultmaven.infrastructure.llm.providers import StopReason

        await router.route(prompt="test", model="gpt-4")

        router.cache.store.assert_called_once()
        assert (
            router.cache.store.call_args.args[2].stop_reason
            is not StopReason.MAX_TOKENS
        )

    @pytest.mark.asyncio
    async def test_an_unknown_stop_reason_is_still_stored(
        self, router, mock_registry, metrics
    ):
        """UNKNOWN is not evidence of a cut, and must not disable caching.

        The provider-blind half is covered elsewhere: a cut body from a
        provider that reports nothing IS stored here, the engine's parse-time
        test catches it, and ``bypass_cache`` keeps the retry off that entry.
        """
        from faultmaven.infrastructure.llm.providers import StopReason

        mock_registry.route_request = AsyncMock(
            return_value=_make_response(stop_reason=StopReason.UNKNOWN)
        )

        await router.route(prompt="test", model="gpt-4")

        router.cache.store.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_retry_at_a_bigger_cap_reaches_the_provider(self, router):
        """End to end, through a REAL cache — the failure the rule prevents.

        Mocking the cache would prove only that a method was not called. This
        drives the actual helper against the actual ``LLMResponseCache`` with a
        prompt-shaped call (the shape that arms it), and asserts the provider
        saw both attempts.
        """
        from faultmaven.infrastructure.llm.cache import LLMResponseCache
        from faultmaven.infrastructure.llm.providers import StopReason
        from faultmaven.infrastructure.llm.truncation import (
            generate_with_truncation_retry,
        )

        router.cache = LLMResponseCache()
        calls = []

        async def _route_request(**kwargs):
            calls.append(kwargs.get("max_tokens"))
            if len(calls) == 1:
                return _make_response(content="cut", stop_reason=StopReason.MAX_TOKENS)
            return _make_response(content="whole", stop_reason=StopReason.STOP)

        router.registry.route_request = _route_request

        async def _call(cap: int):
            return await router.route(prompt="test", model="gpt-4", max_tokens=cap)

        result = await generate_with_truncation_retry(_call, max_tokens=1000)

        assert calls == [1000, 2000], "the retry must reach the provider"
        assert result.content == "whole"
        assert result.is_truncated is False
