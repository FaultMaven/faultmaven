"""
Unit tests for Provider-Specific Capability Override Methods

Tests that each provider correctly overrides get_structured_output_capability()
to return appropriate capability levels based on model-specific features.
"""

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.cohere_provider import CohereProvider
from faultmaven.infrastructure.llm.providers.fireworks_provider import FireworksProvider
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider
from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider
from faultmaven.infrastructure.llm.providers.huggingface import HuggingFaceProvider
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)


class TestOpenAIProviderOverride:
    """Test OpenAI provider capability detection override"""

    def test_gpt4o_returns_strict(self):
        """GPT-4o should support STRICT mode"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            models=["gpt-4o"],
            default_model="gpt-4o",
        )
        provider = OpenAIProvider(config)

        capability = provider.get_structured_output_capability("gpt-4o")
        assert capability == StructuredOutputCapability.STRICT

    def test_gpt4o_mini_returns_strict(self):
        """GPT-4o-mini should support STRICT mode"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            models=["gpt-4o-mini"],
            default_model="gpt-4o-mini",
        )
        provider = OpenAIProvider(config)

        capability = provider.get_structured_output_capability("gpt-4o-mini")
        assert capability == StructuredOutputCapability.STRICT

    def test_gpt4_turbo_returns_strict(self):
        """GPT-4-turbo should support STRICT mode"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            models=["gpt-4-turbo"],
            default_model="gpt-4-turbo",
        )
        provider = OpenAIProvider(config)

        capability = provider.get_structured_output_capability("gpt-4-turbo")
        assert capability == StructuredOutputCapability.STRICT

    def test_gpt35_turbo_returns_function_calling(self):
        """GPT-3.5-turbo should use FUNCTION_CALLING (legacy)"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            models=["gpt-3.5-turbo"],
            default_model="gpt-3.5-turbo",
        )
        provider = OpenAIProvider(config)

        capability = provider.get_structured_output_capability("gpt-3.5-turbo")
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_default_model_used_when_none(self):
        """Should use default model when model parameter is None"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            models=["gpt-4o"],
            default_model="gpt-4o",
        )
        provider = OpenAIProvider(config)

        capability = provider.get_structured_output_capability(None)
        assert capability == StructuredOutputCapability.STRICT


class TestAnthropicProviderOverride:
    """Test Anthropic provider capability detection override"""

    def test_claude_opus_returns_function_calling(self):
        """Claude Opus should use FUNCTION_CALLING"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.anthropic.com/v1",
            models=["claude-3-opus-20240229"],
            default_model="claude-3-opus-20240229",
        )
        provider = AnthropicProvider(config)

        capability = provider.get_structured_output_capability("claude-3-opus-20240229")
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_claude_sonnet_returns_function_calling(self):
        """Claude Sonnet should use FUNCTION_CALLING"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.anthropic.com/v1",
            models=["claude-3-5-sonnet-20241022"],
            default_model="claude-3-5-sonnet-20241022",
        )
        provider = AnthropicProvider(config)

        capability = provider.get_structured_output_capability(
            "claude-3-5-sonnet-20241022"
        )
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_all_claude_models_function_calling(self):
        """All Claude models should consistently return FUNCTION_CALLING"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.anthropic.com/v1",
            models=[
                "claude-3-opus-20240229",
                "claude-3-5-sonnet-20241022",
                "claude-3-haiku-20240307",
            ],
            default_model="claude-3-opus-20240229",
        )
        provider = AnthropicProvider(config)

        for model in config.models:
            capability = provider.get_structured_output_capability(model)
            assert capability == StructuredOutputCapability.FUNCTION_CALLING


class TestGroqProviderOverride:
    """Test Groq provider capability detection override"""

    def test_gpt_oss_20b_returns_strict(self):
        """Groq gpt-oss-20b should support STRICT mode"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.groq.com/openai/v1",
            models=["openai/gpt-oss-20b"],
            default_model="openai/gpt-oss-20b",
        )
        provider = GroqProvider(config)

        capability = provider.get_structured_output_capability("openai/gpt-oss-20b")
        assert capability == StructuredOutputCapability.STRICT

    def test_gpt_oss_120b_returns_strict(self):
        """Groq gpt-oss-120b should support STRICT mode"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.groq.com/openai/v1",
            models=["openai/gpt-oss-120b"],
            default_model="openai/gpt-oss-120b",
        )
        provider = GroqProvider(config)

        capability = provider.get_structured_output_capability("openai/gpt-oss-120b")
        assert capability == StructuredOutputCapability.STRICT

    def test_llama_returns_best_effort(self):
        """Groq Llama models should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.groq.com/openai/v1",
            models=["meta-llama/Llama-3.3-70b-versatile"],
            default_model="meta-llama/Llama-3.3-70b-versatile",
        )
        provider = GroqProvider(config)

        capability = provider.get_structured_output_capability(
            "meta-llama/Llama-3.3-70b-versatile"
        )
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_mixtral_returns_best_effort(self):
        """Groq Mixtral models should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.groq.com/openai/v1",
            models=["mixtral-8x7b-32768"],
            default_model="mixtral-8x7b-32768",
        )
        provider = GroqProvider(config)

        capability = provider.get_structured_output_capability("mixtral-8x7b-32768")
        assert capability == StructuredOutputCapability.BEST_EFFORT


class TestGeminiProviderOverride:
    """Test Gemini provider capability detection override"""

    def test_gemini_2_0_returns_strict(self):
        """Gemini 2.0 should support STRICT mode"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            models=["gemini-2.0-pro"],
            default_model="gemini-2.0-pro",
        )
        provider = GeminiProvider(config)

        capability = provider.get_structured_output_capability("gemini-2.0-pro")
        assert capability == StructuredOutputCapability.STRICT

    def test_gemini_1_5_returns_strict(self):
        """Gemini 1.5 should support STRICT mode"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            models=["gemini-1.5-pro"],
            default_model="gemini-1.5-pro",
        )
        provider = GeminiProvider(config)

        capability = provider.get_structured_output_capability("gemini-1.5-pro")
        assert capability == StructuredOutputCapability.STRICT

    def test_gemini_1_0_returns_best_effort(self):
        """Gemini 1.0 should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            models=["gemini-1.0-pro"],
            default_model="gemini-1.0-pro",
        )
        provider = GeminiProvider(config)

        capability = provider.get_structured_output_capability("gemini-1.0-pro")
        assert capability == StructuredOutputCapability.BEST_EFFORT


class TestCohereProviderOverride:
    """Test Cohere provider capability detection override"""

    def test_command_r_returns_best_effort(self):
        """Cohere Command-R should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.cohere.ai/v1",
            models=["command-r"],
            default_model="command-r",
        )
        provider = CohereProvider(config)

        capability = provider.get_structured_output_capability("command-r")
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_command_r_plus_returns_best_effort(self):
        """Cohere Command-R+ should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.cohere.ai/v1",
            models=["command-r-plus"],
            default_model="command-r-plus",
        )
        provider = CohereProvider(config)

        capability = provider.get_structured_output_capability("command-r-plus")
        assert capability == StructuredOutputCapability.BEST_EFFORT


class TestLocalProviderOverride:
    """Test Local provider capability detection override"""

    def test_functionary_returns_function_calling(self):
        """Functionary models should use FUNCTION_CALLING"""
        config = ProviderConfig(
            name="local",
            api_key=None,
            base_url="http://localhost:8080",
            models=["functionary-7b-v2"],
            default_model="functionary-7b-v2",
        )
        provider = LocalProvider(config)

        capability = provider.get_structured_output_capability("functionary-7b-v2")
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_hermes_returns_function_calling(self):
        """Hermes models should use FUNCTION_CALLING"""
        config = ProviderConfig(
            name="local",
            api_key=None,
            base_url="http://localhost:8080",
            models=["hermes-2-pro-llama-3-8b"],
            default_model="hermes-2-pro-llama-3-8b",
        )
        provider = LocalProvider(config)

        capability = provider.get_structured_output_capability(
            "hermes-2-pro-llama-3-8b"
        )
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_llama_returns_best_effort(self):
        """Standard Llama models should use BEST_EFFORT"""
        config = ProviderConfig(
            name="local",
            api_key=None,
            base_url="http://localhost:8080",
            models=["llama3-8b"],
            default_model="llama3-8b",
        )
        provider = LocalProvider(config)

        capability = provider.get_structured_output_capability("llama3-8b")
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_phi3_returns_best_effort(self):
        """Phi-3 models should use BEST_EFFORT"""
        config = ProviderConfig(
            name="local",
            api_key=None,
            base_url="http://localhost:8080",
            models=["phi3-mini"],
            default_model="phi3-mini",
        )
        provider = LocalProvider(config)

        capability = provider.get_structured_output_capability("phi3-mini")
        assert capability == StructuredOutputCapability.BEST_EFFORT


class TestHuggingFaceProviderOverride:
    """Test HuggingFace provider capability detection override"""

    def test_dialogpt_returns_best_effort(self):
        """HuggingFace DialoGPT should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api-inference.huggingface.co/models",
            models=["microsoft/DialoGPT-medium"],
            default_model="microsoft/DialoGPT-medium",
        )
        provider = HuggingFaceProvider(config)

        capability = provider.get_structured_output_capability(
            "microsoft/DialoGPT-medium"
        )
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_falcon_returns_best_effort(self):
        """HuggingFace Falcon should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api-inference.huggingface.co/models",
            models=["tiiuae/falcon-7b-instruct"],
            default_model="tiiuae/falcon-7b-instruct",
        )
        provider = HuggingFaceProvider(config)

        capability = provider.get_structured_output_capability(
            "tiiuae/falcon-7b-instruct"
        )
        assert capability == StructuredOutputCapability.BEST_EFFORT


class TestFireworksProviderOverride:
    """Test Fireworks provider capability detection override"""

    def test_llama_returns_best_effort(self):
        """Fireworks Llama models should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.fireworks.ai/inference/v1",
            models=["accounts/fireworks/models/llama-v3-70b-instruct"],
            default_model="accounts/fireworks/models/llama-v3-70b-instruct",
        )
        provider = FireworksProvider(config)

        capability = provider.get_structured_output_capability(
            "accounts/fireworks/models/llama-v3-70b-instruct"
        )
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_mixtral_returns_best_effort(self):
        """Fireworks Mixtral models should use BEST_EFFORT"""
        config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.fireworks.ai/inference/v1",
            models=["accounts/fireworks/models/mixtral-8x7b-instruct"],
            default_model="accounts/fireworks/models/mixtral-8x7b-instruct",
        )
        provider = FireworksProvider(config)

        capability = provider.get_structured_output_capability(
            "accounts/fireworks/models/mixtral-8x7b-instruct"
        )
        assert capability == StructuredOutputCapability.BEST_EFFORT


class TestFireworksToolCallingSupport:
    """Test Fireworks provider tool calling support.

    All Fireworks models should report tool calling as supported (True),
    relying on Layer 2 runtime fallback (ToolCallingUnsupportedError) for
    models that genuinely can't handle tools.
    """

    @pytest.fixture
    def fireworks_config(self):
        return ProviderConfig(
            name="fireworks",
            api_key="test-key",
            base_url="https://api.fireworks.ai/inference/v1",
            models=["accounts/fireworks/models/deepseek-v3"],
            default_model="accounts/fireworks/models/deepseek-v3",
        )

    def test_deepseek_v3_supports_tool_calling(self, fireworks_config):
        """DeepSeek V3 on Fireworks supports OpenAI-compatible tool calling."""
        provider = FireworksProvider(fireworks_config)
        assert (
            provider.supports_tool_calling("accounts/fireworks/models/deepseek-v3")
            is True
        )

    def test_deepseek_r1_supports_tool_calling(self, fireworks_config):
        """DeepSeek R1 should not be pre-blocked — Layer 2 handles failures."""
        provider = FireworksProvider(fireworks_config)
        assert (
            provider.supports_tool_calling("accounts/fireworks/models/deepseek-r1")
            is True
        )

    def test_llama_supports_tool_calling(self):
        """Llama models on Fireworks support tool calling."""
        config = ProviderConfig(
            name="fireworks",
            api_key="test-key",
            base_url="https://api.fireworks.ai/inference/v1",
            models=["accounts/fireworks/models/llama-v3-70b-instruct"],
            default_model="accounts/fireworks/models/llama-v3-70b-instruct",
        )
        provider = FireworksProvider(config)
        assert provider.supports_tool_calling() is True

    def test_default_model_supports_tool_calling(self, fireworks_config):
        """Default model (None) should report tool calling supported."""
        provider = FireworksProvider(fireworks_config)
        assert provider.supports_tool_calling(None) is True

    def test_minimax_m2p7_does_not_support_tool_calling(self):
        """MiniMax M2P7 on Fireworks hangs on tool_choice=required and
        times out at the LLM_PROVIDER_TIMEOUT_OVERRIDES limit. Denylisted
        so Layer 1 (pre-check) routes to the non-tool path without
        wasting an API call. See 2026-05-20 Run 7 post-mortem."""
        config = ProviderConfig(
            name="fireworks",
            api_key="test-key",
            base_url="https://api.fireworks.ai/inference/v1",
            models=["accounts/fireworks/models/minimax-m2p7"],
            default_model="accounts/fireworks/models/minimax-m2p7",
        )
        provider = FireworksProvider(config)
        assert (
            provider.supports_tool_calling("accounts/fireworks/models/minimax-m2p7")
            is False
        )
        # Default-model resolution (model=None) must also pick up the
        # denylist — the same denial applies regardless of how the model
        # is selected.
        assert provider.supports_tool_calling(None) is False


class TestProviderOverrideConsistency:
    """Test consistency across provider override implementations"""

    def test_all_providers_implement_override(self):
        """Verify all providers have overridden the method"""
        from faultmaven.infrastructure.llm.providers.base import BaseLLMProvider

        providers = [
            OpenAIProvider,
            AnthropicProvider,
            GroqProvider,
            GeminiProvider,
            CohereProvider,
            LocalProvider,
            HuggingFaceProvider,
            FireworksProvider,
        ]

        for provider_class in providers:
            # Check that the method is overridden (not using base implementation)
            assert (
                provider_class.get_structured_output_capability
                != BaseLLMProvider.get_structured_output_capability
            ), f"{provider_class.__name__} should override get_structured_output_capability()"

    def test_providers_return_valid_capabilities(self):
        """Verify all providers return valid StructuredOutputCapability values"""
        valid_capabilities = {
            StructuredOutputCapability.STRICT,
            StructuredOutputCapability.FUNCTION_CALLING,
            StructuredOutputCapability.BEST_EFFORT,
            StructuredOutputCapability.NONE,
        }

        test_configs = [
            (
                OpenAIProvider,
                ProviderConfig(
                    name="openai",
                    api_key="test",
                    base_url="https://api.openai.com/v1",
                    models=["gpt-4o"],
                    default_model="gpt-4o",
                ),
            ),
            (
                AnthropicProvider,
                ProviderConfig(
                    name="anthropic",
                    api_key="test",
                    base_url="https://api.anthropic.com/v1",
                    models=["claude-3-opus-20240229"],
                    default_model="claude-3-opus-20240229",
                ),
            ),
            (
                GroqProvider,
                ProviderConfig(
                    name="groq",
                    api_key="test",
                    base_url="https://api.groq.com/openai/v1",
                    models=["openai/gpt-oss-20b"],
                    default_model="openai/gpt-oss-20b",
                ),
            ),
            (
                GeminiProvider,
                ProviderConfig(
                    name="gemini",
                    api_key="test",
                    base_url="https://generativelanguage.googleapis.com/v1beta",
                    models=["gemini-2.0-pro"],
                    default_model="gemini-2.0-pro",
                ),
            ),
            (
                CohereProvider,
                ProviderConfig(
                    name="cohere",
                    api_key="test",
                    base_url="https://api.cohere.ai/v1",
                    models=["command-r"],
                    default_model="command-r",
                ),
            ),
            (
                LocalProvider,
                ProviderConfig(
                    name="local",
                    api_key=None,
                    base_url="http://localhost:8080",
                    models=["llama3-8b"],
                    default_model="llama3-8b",
                ),
            ),
            (
                HuggingFaceProvider,
                ProviderConfig(
                    name="huggingface",
                    api_key="test",
                    base_url="https://api-inference.huggingface.co/models",
                    models=["microsoft/DialoGPT-medium"],
                    default_model="microsoft/DialoGPT-medium",
                ),
            ),
            (
                FireworksProvider,
                ProviderConfig(
                    name="fireworks",
                    api_key="test",
                    base_url="https://api.fireworks.ai/inference/v1",
                    models=["accounts/fireworks/models/llama-v3-70b-instruct"],
                    default_model="accounts/fireworks/models/llama-v3-70b-instruct",
                ),
            ),
        ]

        for provider_class, config in test_configs:
            provider = provider_class(config)
            capability = provider.get_structured_output_capability()
            assert (
                capability in valid_capabilities
            ), f"{provider_class.__name__} returned invalid capability: {capability}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
