"""Tests for STRUCTURED_OUTPUT_PROVIDER capability override (Tier 2).

Covers:
  - Settings layer: ``LLMSettings.get_structured_output_provider()`` returns
    the explicit override when set, falls back to CHAT_PROVIDER when unset.
  - Registry layer: ``ProviderRegistry.route_request(..., provider_override=)``
    routes exclusively through the named provider when valid, and silently
    falls back to the normal chain when the override isn't initialized.

The end-to-end wiring (milestone_engine → router → registry) is exercised
implicitly by the registry tests because they cover the same dispatch
logic the higher layers pass through.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.config.settings import LLMProvider, LLMSettings
from faultmaven.infrastructure.llm.providers.registry import ProviderRegistry

# ============================================================
# Settings layer
# ============================================================


class TestStructuredOutputProviderSetting:
    """LLMSettings.get_structured_output_provider() honors the override
    when set; falls back to CHAT_PROVIDER otherwise."""

    def test_unset_falls_back_to_chat_provider(self):
        """When STRUCTURED_OUTPUT_PROVIDER is unset, use CHAT_PROVIDER.

        Preserves existing behavior — no change for operators who don't
        opt in to the override.
        """
        s = LLMSettings(CHAT_PROVIDER="anthropic")
        assert s.get_structured_output_provider() == LLMProvider.ANTHROPIC

    def test_explicit_override_takes_precedence(self):
        """When STRUCTURED_OUTPUT_PROVIDER is set, it wins over CHAT_PROVIDER.

        This is the Tier 2 use case: cheap CHAT_PROVIDER (Fireworks) for
        synthesis/chat, but force schema-bound calls through a known-
        STRICT provider (Gemini).
        """
        s = LLMSettings(
            CHAT_PROVIDER="fireworks",
            structured_output_provider=LLMProvider.GEMINI,
        )
        assert s.get_structured_output_provider() == LLMProvider.GEMINI

    def test_override_independent_of_other_capability_overrides(self):
        """Setting STRUCTURED_OUTPUT_PROVIDER doesn't affect synthesis /
        classifier / code overrides — each capability override is
        independent and falls back to CHAT_PROVIDER on its own.

        synthesis/classifier are passed None explicitly because they ship
        PINNED to gemini; None is the "unset" state whose fallback this test
        is about, and is what commenting the key out in .env produces."""
        s = LLMSettings(
            CHAT_PROVIDER="fireworks",
            structured_output_provider=LLMProvider.GEMINI,
            synthesis_provider=None,
            classifier_provider=None,
        )
        assert s.get_structured_output_provider() == LLMProvider.GEMINI
        assert s.get_synthesis_provider() == LLMProvider.FIREWORKS  # unchanged
        assert s.get_classifier_provider() == LLMProvider.FIREWORKS  # unchanged

    def test_shipped_classifier_synthesis_pins_survive_a_chat_flip(self):
        """The shipped pins are static: flipping CHAT_PROVIDER moves the
        anchor-following roles and leaves these two on gemini."""
        s = LLMSettings(CHAT_PROVIDER="fireworks")
        assert s.get_classifier_provider() == LLMProvider.GEMINI
        assert s.get_synthesis_provider() == LLMProvider.GEMINI
        assert s.get_structured_output_provider() == LLMProvider.FIREWORKS

    def test_get_structured_output_model_resolves_via_provider(self):
        """get_structured_output_model() looks up the *override* provider's
        base model — not CHAT_PROVIDER's model. This is what prevents
        sending the wrong model name to the override provider's API.
        """
        s = LLMSettings(
            CHAT_PROVIDER="fireworks",
            structured_output_provider=LLMProvider.GEMINI,
            gemini_model="gemini-2.5-pro",
            fireworks_model="accounts/fireworks/models/something-cheap",
        )
        assert s.get_structured_output_model() == "gemini-2.5-pro"


# ============================================================
# Registry layer — provider_override behavior
# ============================================================


class TestRegistryProviderOverride:
    """ProviderRegistry.route_request() should:
    - Use ONLY the override provider when set AND it's initialized.
    - Silently fall back to the normal fallback chain when the override
      is requested but not initialized (e.g. operator forgot the API key).
    """

    def _build_registry_with_providers(self, provider_names: list[str]):
        """Construct a minimal registry pre-populated with mock providers.

        Each provider is an AsyncMock whose .generate() returns a unique
        LLMResponse so tests can assert which provider got the call.
        """
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        registry = ProviderRegistry.__new__(ProviderRegistry)
        # Bypass __init__ side effects; set the minimum needed for routing.
        registry._initialized = True
        registry._routing_initialized = False
        registry._sticky_provider = None
        registry.logger = MagicMock()
        registry._providers = {}
        registry._provider_states = {}
        registry._provider_classes = {}
        registry._fallback_chain = list(provider_names)

        # Provider state objects (so route_request can update health).
        from faultmaven.infrastructure.llm.providers.registry import ProviderState

        for name in provider_names:
            mock_provider = MagicMock()

            async def _generate(*args, _name=name, **kwargs):
                return LLMResponse(
                    content=f"response_from_{_name}",
                    confidence=0.95,
                    provider=_name,
                    model="test-model",
                    tokens_used=0,
                    response_time_ms=0,
                )

            mock_provider.generate = _generate
            registry._providers[name] = mock_provider
            registry._provider_states[name] = ProviderState(name=name)

        return registry

    @pytest.mark.asyncio
    async def test_override_targets_named_provider_when_initialized(self):
        """provider_override='gemini' on a registry with both 'fireworks' and
        'gemini' initialized → call lands on gemini, not the
        CHAT_PROVIDER-first fallback chain."""
        registry = self._build_registry_with_providers(["fireworks", "gemini"])
        # Fallback chain has fireworks FIRST — without the override, that's
        # who'd be called.

        response = await registry.route_request(
            prompt="test", provider_override="gemini"
        )

        assert response.provider == "gemini"
        assert response.content == "response_from_gemini"

    @pytest.mark.asyncio
    async def test_no_override_uses_normal_fallback_chain(self):
        """When provider_override is None, behavior is unchanged — first
        provider in the chain is used."""
        registry = self._build_registry_with_providers(["fireworks", "gemini"])

        response = await registry.route_request(prompt="test")

        # First in chain wins
        assert response.provider == "fireworks"

    @pytest.mark.asyncio
    async def test_override_for_uninitialized_provider_falls_back_silently(
        self,
    ):
        """provider_override='anthropic' on a registry that DOESN'T have
        anthropic initialized → silently falls back to the normal chain
        (with a warning log) instead of failing.

        Rationale: a misconfigured STRUCTURED_OUTPUT_PROVIDER (forgot
        the API key) shouldn't break every investigation turn. The
        warning log surfaces the misconfiguration without making
        production fragile.
        """
        registry = self._build_registry_with_providers(["fireworks", "gemini"])

        response = await registry.route_request(
            prompt="test", provider_override="anthropic"  # not in registry
        )

        # Fell back to normal chain — first provider wins
        assert response.provider == "fireworks"
        # Should have logged a warning about the misconfiguration
        registry.logger.warning.assert_called()
        warning_msg = str(registry.logger.warning.call_args)
        assert "anthropic" in warning_msg
        assert "not initialized" in warning_msg

    @pytest.mark.asyncio
    async def test_override_skips_fallback_when_target_fails(self):
        """When the override target fails, do NOT fall through to other
        providers in the chain — that would defeat the purpose of the
        explicit routing. Surface the failure directly to the caller."""
        from faultmaven.infrastructure.llm.providers.base import LLMResponse

        registry = self._build_registry_with_providers(["fireworks", "gemini"])

        # Make gemini fail
        async def _failing_generate(*args, **kwargs):
            raise RuntimeError("gemini upstream error")

        registry._providers["gemini"].generate = _failing_generate

        # Fireworks would normally succeed via the fallback chain — but
        # provider_override locks us to gemini only.
        with pytest.raises(RuntimeError, match="gemini upstream error"):
            await registry.route_request(prompt="test", provider_override="gemini")


@pytest.mark.unit
class TestPinnedRoleProviderOffTheFallbackChain:
    """A pinned role provider is reachable even when it is not in the chain.

    ``route_request`` skips any provider with no entry in ``_provider_states``
    (``if not state: continue``). That map was built from the fallback chain
    alone, while ``provider_override`` legitimately names providers outside it
    — which is the entire point of a static role pin. Under
    STRICT_PROVIDER_MODE the chain is ONE provider, so a role pinned anywhere
    else matched no state, was skipped, and the loop fell out to "All providers
    failed with no error details": a hard failure, no attempt made, nothing
    naming the cause. Reachable straight from the shipped defaults, where
    classifier/synthesis/multimodal are pinned to gemini and a CHAT_PROVIDER
    flip puts every one of those calls off-chain.

    These build NO real settings object. An earlier version constructed
    ``LLMSettings`` with explicit values and still failed in CI, because this
    settings class puts environment ABOVE init kwargs — ``LLMSettings(
    STRICT_PROVIDER_MODE=True)`` yields False when the env says false — so any
    such test is at the mercy of whatever a previous test left in os.environ
    (CI resolved a three-provider chain where this box resolved one). The
    registry's chain logic needs exactly one settings value and a provider
    dict, so both are supplied directly and the test states only what it
    controls.
    """

    @staticmethod
    def _registry(strict=True, providers=("openai", "gemini")):
        from types import SimpleNamespace

        from faultmaven.infrastructure.llm.providers.registry import ProviderRegistry

        registry = ProviderRegistry(
            SimpleNamespace(llm=SimpleNamespace(strict_provider_mode=strict))
        )
        registry._providers = {name: object() for name in providers}
        registry._setup_fallback_chain("openai")
        # Lazy init would re-read settings this double does not carry; the
        # chain is already built, so mark it done.
        registry._initialized = True
        return registry

    def test_off_chain_pin_has_health_state(self):
        registry = self._registry()
        assert registry.get_fallback_chain() == ["openai"], "strict mode precondition"
        assert "gemini" in registry._provider_states, (
            "an initialized provider that a role pin can name must carry health "
            "state, or route_request skips it and reports 'All providers failed'"
        )
        assert set(registry._providers) <= set(
            registry._provider_states
        ), "the invariant generally: every initialized provider is routable"

    def test_chain_membership_still_governs_fallback(self):
        """Giving off-chain providers state must NOT smuggle them into the
        fallback order — they stay reachable only when named explicitly."""
        registry = self._registry()
        assert registry._get_routing_order() == ["openai"]

    def test_a_successful_pin_does_not_become_sticky_for_chat(self):
        """A pinned call that succeeds sets _sticky_provider. The sticky path
        must still refuse to front-run the chain with an off-chain provider,
        or one classifier call would silently redirect every later CHAT call
        and defeat STRICT_PROVIDER_MODE."""
        registry = self._registry()
        registry._sticky_provider = "gemini"  # as a successful pinned call leaves it
        assert registry._get_routing_order() == ["openai"]

    def test_routing_order_never_leaves_the_chain(self):
        """The general form, independent of strict mode: whatever the chain is,
        the routing order is a subset of it."""
        registry = self._registry(strict=False, providers=("openai", "gemini", "local"))
        chain = set(registry.get_fallback_chain())
        assert set(registry._get_routing_order()) <= chain
        registry._sticky_provider = "gemini"
        assert set(registry._get_routing_order()) <= chain
