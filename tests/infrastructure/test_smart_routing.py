"""
Integration tests for Smart Provider Routing with health tracking.

Tests the complete routing behavior including:
- Sticky routing (prefers last successful provider)
- Health state transitions (HEALTHY → DEGRADED → UNHEALTHY)
- Exponential backoff cooldown
- Recovery after cooldown (UNHEALTHY → UNKNOWN → HEALTHY)
- Low-confidence fallback
- Force primary retry when all providers down
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderHealth,
    ProviderRegistry,
    ProviderState,
)


@pytest.fixture
def mock_settings():
    """Mock settings for testing"""
    settings = MagicMock()
    settings.llm.provider = "gemini"
    settings.llm.strict_provider_mode = False  # Enable fallbacks
    settings.llm.gemini_api_key.get_secret_value.return_value = "test_key"
    settings.llm.gemini_model = "gemini-1.5-pro"
    settings.llm.gemini_base_url = "https://api.test.com"
    settings.llm.fireworks_api_key.get_secret_value.return_value = "test_key"
    settings.llm.fireworks_model = "test-model"
    settings.llm.fireworks_base_url = "https://api.test.com"
    settings.llm.max_retries = 1
    settings.llm.request_timeout = 30
    return settings


@pytest.fixture
def registry(mock_settings):
    """Create a registry with mocked providers"""
    reg = ProviderRegistry(settings=mock_settings)

    # Mock providers
    gemini_provider = MagicMock()
    gemini_provider.is_available.return_value = True
    gemini_provider.get_supported_models.return_value = ["gemini-1.5-pro"]
    gemini_provider.config = MagicMock(confidence_score=0.8)

    fireworks_provider = MagicMock()
    fireworks_provider.is_available.return_value = True
    fireworks_provider.get_supported_models.return_value = ["test-model"]
    fireworks_provider.config = MagicMock(confidence_score=0.9)

    reg._providers = {
        "gemini": gemini_provider,
        "fireworks": fireworks_provider,
    }
    reg._fallback_chain = ["gemini", "fireworks"]
    reg._provider_states = {
        "gemini": ProviderState(name="gemini"),
        "fireworks": ProviderState(name="fireworks"),
    }
    reg._initialized = True
    reg._routing_initialized = False

    return reg


class TestStickyRouting:
    """Test sticky provider behavior"""

    @pytest.mark.asyncio
    async def test_sticky_provider_stays_sticky_on_success(self, registry):
        """Sticky provider should be preferred on subsequent requests"""
        gemini = registry._providers["gemini"]

        # First request succeeds with gemini
        gemini.generate = AsyncMock(
            return_value=LLMResponse(
                content="response",
                confidence=0.9,
                model="gemini-1.5-pro",
                provider="gemini",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        response1 = await registry.route_request("test prompt")

        # Verify gemini was used and is now sticky
        assert response1.provider == "gemini"
        assert registry._sticky_provider == "gemini"
        assert registry._provider_states["gemini"].health == ProviderHealth.HEALTHY

        # Second request should try gemini FIRST (sticky)
        gemini.generate.reset_mock()
        response2 = await registry.route_request("test prompt 2")

        # Verify gemini was called (sticky routing)
        assert gemini.generate.called
        assert response2.provider == "gemini"
        assert registry._sticky_provider == "gemini"

    @pytest.mark.asyncio
    async def test_failure_triggers_fallback_and_new_sticky(self, registry):
        """Failed sticky provider should trigger fallback"""
        gemini = registry._providers["gemini"]
        fireworks = registry._providers["fireworks"]

        # Set gemini as sticky
        registry._sticky_provider = "gemini"
        registry._provider_states["gemini"].health = ProviderHealth.HEALTHY

        # Gemini fails
        gemini.generate = AsyncMock(side_effect=Exception("Gemini down"))

        # Fireworks succeeds
        fireworks.generate = AsyncMock(
            return_value=LLMResponse(
                content="response",
                confidence=0.9,
                model="test-model",
                provider="fireworks",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        response = await registry.route_request("test prompt")

        # Verify fallback to fireworks
        assert response.provider == "fireworks"
        assert registry._sticky_provider == "fireworks"
        assert registry._provider_states["gemini"].health == ProviderHealth.DEGRADED


class TestHealthTransitions:
    """Test health state transitions"""

    def test_success_resets_to_healthy(self):
        """Successful call should reset health to HEALTHY"""
        state = ProviderState(name="test")
        state.consecutive_failures = 2
        state.health = ProviderHealth.DEGRADED

        state.record_success(latency_ms=100.0)

        assert state.health == ProviderHealth.HEALTHY
        assert state.consecutive_failures == 0
        assert state.avg_latency_ms == 100.0

    def test_one_failure_degrades_health(self):
        """Single failure should set DEGRADED"""
        state = ProviderState(name="test")

        state.record_failure()

        assert state.health == ProviderHealth.DEGRADED
        assert state.consecutive_failures == 1

    def test_three_failures_marks_unhealthy(self):
        """Three failures should set UNHEALTHY"""
        state = ProviderState(name="test")

        state.record_failure()  # 1 -> DEGRADED
        state.record_failure()  # 2 -> DEGRADED
        state.record_failure()  # 3 -> UNHEALTHY

        assert state.health == ProviderHealth.UNHEALTHY
        assert state.consecutive_failures == 3

    def test_exponential_backoff_cooldown(self):
        """Cooldown should increase exponentially"""
        state = ProviderState(name="test")

        state.record_failure()  # 1st failure
        assert state._recovery_cooldown == 60.0

        state.record_failure()  # 2nd failure
        assert state._recovery_cooldown == 120.0

        state.record_failure()  # 3rd failure
        assert state._recovery_cooldown == 240.0

        state.record_failure()  # 4th failure
        assert state._recovery_cooldown == 480.0

        state.record_failure()  # 5th failure
        assert state._recovery_cooldown == 600.0  # max

        state.record_failure()  # 6th failure
        assert state._recovery_cooldown == 600.0  # capped


class TestRecoveryBehavior:
    """Test recovery after provider failures"""

    @pytest.mark.asyncio
    async def test_unhealthy_provider_skipped_until_cooldown(self, registry):
        """UNHEALTHY provider should be skipped until cooldown expires"""
        gemini = registry._providers["gemini"]
        fireworks = registry._providers["fireworks"]

        # Mark gemini as UNHEALTHY with recent failure
        gemini_state = registry._provider_states["gemini"]
        gemini_state.health = ProviderHealth.UNHEALTHY
        gemini_state.consecutive_failures = 3
        gemini_state.last_failure_time = time.monotonic()  # Just failed
        gemini_state._recovery_cooldown = 60.0

        # Fireworks succeeds
        fireworks.generate = AsyncMock(
            return_value=LLMResponse(
                content="response",
                confidence=0.9,
                model="test-model",
                provider="fireworks",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        response = await registry.route_request("test prompt")

        # Verify gemini was NOT tried (should_attempt returns False)
        assert not gemini.generate.called
        assert response.provider == "fireworks"

    def test_recovery_after_cooldown_transitions_to_unknown(self):
        """After cooldown, UNHEALTHY should transition to UNKNOWN"""
        state = ProviderState(name="test")
        state.health = ProviderHealth.UNHEALTHY
        state.consecutive_failures = 3
        state.last_failure_time = (
            time.monotonic() - 61.0
        )  # 61 seconds ago (> 60s cooldown)
        state._recovery_cooldown = 60.0

        # should_attempt will transition to UNKNOWN
        can_attempt = state.should_attempt()

        assert can_attempt is True
        assert state.health == ProviderHealth.UNKNOWN

    @pytest.mark.asyncio
    async def test_recovery_path_unknown_to_healthy(self, registry):
        """Recovery path: UNHEALTHY → UNKNOWN → HEALTHY"""
        gemini = registry._providers["gemini"]

        # Start as UNHEALTHY (past cooldown)
        gemini_state = registry._provider_states["gemini"]
        gemini_state.health = ProviderHealth.UNHEALTHY
        gemini_state.consecutive_failures = 3
        gemini_state.last_failure_time = time.monotonic() - 61.0  # Past cooldown
        gemini_state._recovery_cooldown = 60.0

        # Gemini succeeds on retry
        gemini.generate = AsyncMock(
            return_value=LLMResponse(
                content="response",
                confidence=0.9,
                model="gemini-1.5-pro",
                provider="gemini",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        response = await registry.route_request("test prompt")

        # Verify recovery to HEALTHY
        assert gemini_state.health == ProviderHealth.HEALTHY
        assert gemini_state.consecutive_failures == 0


class TestLowConfidenceFallback:
    """Test low-confidence response fallback"""

    @pytest.mark.asyncio
    async def test_returns_best_low_confidence_when_all_below_threshold(self, registry):
        """Should return best low-confidence response when all fail threshold"""
        gemini = registry._providers["gemini"]
        fireworks = registry._providers["fireworks"]

        # Both return low confidence (below 0.8 threshold)
        gemini.generate = AsyncMock(
            return_value=LLMResponse(
                content="gemini response",
                confidence=0.6,
                model="gemini-1.5-pro",
                provider="gemini",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        fireworks.generate = AsyncMock(
            return_value=LLMResponse(
                content="fireworks response",
                confidence=0.7,  # Better than gemini
                model="test-model",
                provider="fireworks",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        response = await registry.route_request("test prompt", confidence_threshold=0.8)

        # Should return fireworks (best low-confidence)
        assert response.confidence == 0.7
        assert "fireworks" in response.provider
        assert "(low-confidence)" in response.provider


class TestAllProvidersDown:
    """Test behavior when all providers fail"""

    @pytest.mark.asyncio
    async def test_force_primary_retry_when_all_unhealthy(self, registry):
        """Should force retry primary provider when all are UNHEALTHY"""
        gemini = registry._providers["gemini"]
        fireworks = registry._providers["fireworks"]

        # Mark both as UNHEALTHY (within cooldown)
        for state in registry._provider_states.values():
            state.health = ProviderHealth.UNHEALTHY
            state.consecutive_failures = 3
            state.last_failure_time = time.monotonic()  # Just failed
            state._recovery_cooldown = 60.0

        # Gemini succeeds on forced retry
        gemini.generate = AsyncMock(
            return_value=LLMResponse(
                content="response",
                confidence=0.9,
                model="gemini-1.5-pro",
                provider="gemini",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        response = await registry.route_request("test prompt")

        # Verify gemini was tried despite being UNHEALTHY (forced retry)
        assert gemini.generate.called
        assert response.provider == "gemini"

    @pytest.mark.asyncio
    async def test_raises_exception_when_all_fail_completely(self, registry):
        """Should raise exception when all providers fail with no low-confidence"""
        gemini = registry._providers["gemini"]
        fireworks = registry._providers["fireworks"]

        # Both fail completely
        gemini.generate = AsyncMock(side_effect=Exception("Gemini error"))
        fireworks.generate = AsyncMock(side_effect=Exception("Fireworks error"))

        with pytest.raises(Exception, match="Fireworks error"):
            await registry.route_request("test prompt")

    @pytest.mark.asyncio
    async def test_empty_fallback_chain_raises_a_described_error(self, registry):
        """fm#1287 — the "force primary retry" last resort must not index an
        EMPTY chain.

        With no provider initialised (every API key missing, or every configured
        name unknown) ``_fallback_chain`` is empty and ``routing_order =
        [self._fallback_chain[0]]`` raised a bare ``IndexError`` whose ``str()``
        is "list index out of range". That reached the engine as an
        unclassifiable failure and the user was shown "LLM error (IndexError)" —
        a Python traceback artefact standing in for the one diagnosis an
        operator actually needs.
        """
        from faultmaven.models.exceptions import LLMProviderError

        registry._fallback_chain = []
        registry._providers = {}
        registry._provider_states = {}

        with pytest.raises(LLMProviderError) as exc_info:
            await registry.route_request("test prompt")

        message = str(exc_info.value)
        assert "No LLM providers are configured" in message
        assert "CHAT_PROVIDER" in message

    @pytest.mark.asyncio
    async def test_non_empty_chain_still_forces_the_primary(self, registry):
        """POSITIVE CONTROL for the guard above: it must fire ONLY on an empty
        chain. A chain that still has a primary keeps the existing last-resort
        behaviour — otherwise the guard would have replaced a working recovery
        path with a hard failure and the test above would pass for the wrong
        reason."""
        for state in registry._provider_states.values():
            state.health = ProviderHealth.UNHEALTHY
            state.consecutive_failures = 3
            state.last_failure_time = time.monotonic()
            state._recovery_cooldown = 60.0

        registry._providers["gemini"].generate = AsyncMock(
            return_value=LLMResponse(
                content="response",
                confidence=0.9,
                model="gemini-1.5-pro",
                provider="gemini",
                tokens_used=100,
                response_time_ms=250.0,
            )
        )

        response = await registry.route_request("test prompt")
        assert response.provider == "gemini"


class TestRoutingOrder:
    """Test provider routing order logic"""

    def test_sticky_provider_comes_first(self, registry):
        """Sticky provider should be first in routing order"""
        registry._sticky_provider = "fireworks"
        registry._provider_states["fireworks"].health = ProviderHealth.HEALTHY

        order = registry._get_routing_order()

        assert order[0] == "fireworks"
        assert "gemini" in order

    def test_healthy_before_degraded(self, registry):
        """HEALTHY providers should come before DEGRADED"""
        registry._sticky_provider = None
        registry._provider_states["gemini"].health = ProviderHealth.DEGRADED
        registry._provider_states["fireworks"].health = ProviderHealth.HEALTHY

        order = registry._get_routing_order()

        # Fireworks (HEALTHY) should come before gemini (DEGRADED)
        assert order.index("fireworks") < order.index("gemini")

    def test_unhealthy_provider_excluded(self, registry):
        """UNHEALTHY provider should not be in routing order"""
        registry._sticky_provider = None
        gemini_state = registry._provider_states["gemini"]
        gemini_state.health = ProviderHealth.UNHEALTHY
        gemini_state.last_failure_time = time.monotonic()  # Just failed
        gemini_state._recovery_cooldown = 60.0

        order = registry._get_routing_order()

        # Gemini should not be in order (within cooldown)
        assert "gemini" not in order
        assert "fireworks" in order


class TestLatencyTracking:
    """Test latency measurement and averaging"""

    def test_latency_tracked_on_success(self):
        """Latency should be tracked on successful calls"""
        state = ProviderState(name="test")

        state.record_success(latency_ms=100.0)
        state.record_success(latency_ms=200.0)
        state.record_success(latency_ms=150.0)

        # Average of 100, 200, 150 = 150
        assert state.avg_latency_ms == 150.0

    def test_latency_window_size_limited(self):
        """Latency window should be limited to 10 entries"""
        state = ProviderState(name="test")

        # Add 15 latency measurements
        for i in range(15):
            state.record_success(latency_ms=float(i * 10))

        # Should only keep last 10
        assert len(state._latency_window) == 10
        # Average should be of last 10: 50, 60, 70, 80, 90, 100, 110, 120, 130, 140
        expected_avg = sum(range(50, 150, 10)) / 10
        assert state.avg_latency_ms == expected_avg
