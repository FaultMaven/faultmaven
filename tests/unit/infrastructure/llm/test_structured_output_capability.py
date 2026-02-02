"""
Unit tests for Structured Output Capability Detection System

Tests the provider-agnostic capability detection and strategy creation.
"""

import pytest
from pydantic import BaseModel

from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
    create_strategy_for_capability,
    get_capability_for_provider_and_model,
)


class SimpleTestModel(BaseModel):
    """Simple model for testing"""

    name: str
    value: int


class TestCapabilityDetection:
    """Test capability detection for different providers"""

    def test_openai_gpt4o_strict_support(self):
        """OpenAI GPT-4o should support STRICT mode"""
        capability = get_capability_for_provider_and_model("openai", "gpt-4o")
        assert capability == StructuredOutputCapability.STRICT

    def test_openai_gpt4o_mini_strict_support(self):
        """OpenAI GPT-4o-mini should support STRICT mode"""
        capability = get_capability_for_provider_and_model("openai", "gpt-4o-mini")
        assert capability == StructuredOutputCapability.STRICT

    def test_openai_gpt4_turbo_strict_support(self):
        """OpenAI GPT-4-turbo should support STRICT mode"""
        capability = get_capability_for_provider_and_model("openai", "gpt-4-turbo")
        assert capability == StructuredOutputCapability.STRICT

    def test_openai_legacy_function_calling(self):
        """Older OpenAI models should use FUNCTION_CALLING"""
        capability = get_capability_for_provider_and_model("openai", "gpt-3.5-turbo")
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_groq_gpt_oss_20b_strict_support(self):
        """Groq gpt-oss-20b should support STRICT mode"""
        capability = get_capability_for_provider_and_model(
            "groq", "openai/gpt-oss-20b"
        )
        assert capability == StructuredOutputCapability.STRICT

    def test_groq_gpt_oss_120b_strict_support(self):
        """Groq gpt-oss-120b should support STRICT mode"""
        capability = get_capability_for_provider_and_model(
            "groq", "openai/gpt-oss-120b"
        )
        assert capability == StructuredOutputCapability.STRICT

    def test_groq_llama_best_effort(self):
        """Groq Llama models should use BEST_EFFORT mode"""
        capability = get_capability_for_provider_and_model(
            "groq", "meta-llama/Llama-3.3-70b-versatile"
        )
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_groq_mixtral_best_effort(self):
        """Groq Mixtral models should use BEST_EFFORT mode"""
        capability = get_capability_for_provider_and_model(
            "groq", "mixtral-8x7b-32768"
        )
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_anthropic_function_calling(self):
        """Anthropic Claude should use FUNCTION_CALLING"""
        capability = get_capability_for_provider_and_model(
            "anthropic", "claude-3-opus-20240229"
        )
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_gemini_2_strict_support(self):
        """Gemini 2.0 should support STRICT mode"""
        capability = get_capability_for_provider_and_model("gemini", "gemini-2.0-pro")
        assert capability == StructuredOutputCapability.STRICT

    def test_gemini_15_strict_support(self):
        """Gemini 1.5 should support STRICT mode"""
        capability = get_capability_for_provider_and_model("gemini", "gemini-1.5-pro")
        assert capability == StructuredOutputCapability.STRICT

    def test_gemini_legacy_best_effort(self):
        """Older Gemini models should use BEST_EFFORT"""
        capability = get_capability_for_provider_and_model("gemini", "gemini-1.0-pro")
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_cohere_best_effort(self):
        """Cohere should use BEST_EFFORT mode"""
        capability = get_capability_for_provider_and_model("cohere", "command-r")
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_local_model_best_effort(self):
        """Local models should default to BEST_EFFORT"""
        capability = get_capability_for_provider_and_model("local", "llama3-8b")
        assert capability == StructuredOutputCapability.BEST_EFFORT

    def test_local_functionary_function_calling(self):
        """Local functionary models should use FUNCTION_CALLING"""
        capability = get_capability_for_provider_and_model(
            "local", "functionary-7b-v2"
        )
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_local_hermes_function_calling(self):
        """Local Hermes models should use FUNCTION_CALLING"""
        capability = get_capability_for_provider_and_model("local", "hermes-2-pro-llama")
        assert capability == StructuredOutputCapability.FUNCTION_CALLING

    def test_unknown_provider_best_effort(self):
        """Unknown providers should default to BEST_EFFORT"""
        capability = get_capability_for_provider_and_model("unknown", "unknown-model")
        assert capability == StructuredOutputCapability.BEST_EFFORT


class TestStrategyCreation:
    """Test strategy creation for different capability levels"""

    def test_strict_strategy_structure(self):
        """STRICT strategy should use json_schema with strict:true"""
        schema = SimpleTestModel.model_json_schema()
        strategy = create_strategy_for_capability(
            StructuredOutputCapability.STRICT, schema
        )

        assert strategy.capability == StructuredOutputCapability.STRICT
        assert strategy.mode == StructuredOutputMode.JSON_SCHEMA_STRICT
        assert strategy.include_schema_in_prompt is True  # Redundancy
        assert strategy.response_format is not None
        assert strategy.response_format["type"] == "json_schema"
        assert strategy.response_format["json_schema"]["strict"] is True
        assert strategy.response_format["json_schema"]["name"] == "SimpleTestModel"
        assert "schema" in strategy.response_format["json_schema"]

    def test_best_effort_strategy_structure(self):
        """BEST_EFFORT strategy should use json_object with schema in prompt"""
        schema = SimpleTestModel.model_json_schema()
        strategy = create_strategy_for_capability(
            StructuredOutputCapability.BEST_EFFORT, schema
        )

        assert strategy.capability == StructuredOutputCapability.BEST_EFFORT
        assert strategy.mode == StructuredOutputMode.JSON_OBJECT
        assert (
            strategy.include_schema_in_prompt is True
        )  # CRITICAL: Must include schema
        assert strategy.response_format is not None
        assert strategy.response_format == {"type": "json_object"}

    def test_function_calling_strategy_structure(self):
        """FUNCTION_CALLING strategy should use tools parameter"""
        schema = SimpleTestModel.model_json_schema()
        strategy = create_strategy_for_capability(
            StructuredOutputCapability.FUNCTION_CALLING, schema
        )

        assert strategy.capability == StructuredOutputCapability.FUNCTION_CALLING
        assert strategy.mode == StructuredOutputMode.FUNCTION_CALLING
        assert (
            strategy.include_schema_in_prompt is False
        )  # Schema goes in tools parameter
        assert strategy.response_format is None  # Don't use response_format
        assert strategy.extra_config.get("use_tools") is True

    def test_none_strategy_structure(self):
        """NONE strategy should use prompt-only approach"""
        schema = SimpleTestModel.model_json_schema()
        strategy = create_strategy_for_capability(
            StructuredOutputCapability.NONE, schema
        )

        assert strategy.capability == StructuredOutputCapability.NONE
        assert strategy.mode == StructuredOutputMode.PROMPT_ONLY
        assert (
            strategy.include_schema_in_prompt is True
        )  # Only way to convey schema
        assert strategy.response_format is None


class TestStrategyBehavior:
    """Test strategy behavior and configuration"""

    def test_strict_schema_includes_title(self):
        """STRICT strategy should use model title as schema name"""

        class CustomTitleModel(BaseModel):
            """Custom model with title"""

            field: str

        schema = CustomTitleModel.model_json_schema()
        strategy = create_strategy_for_capability(
            StructuredOutputCapability.STRICT, schema
        )

        assert strategy.response_format["json_schema"]["name"] == schema.get(
            "title", "Response"
        )

    def test_strategy_schema_passthrough(self):
        """Strategy should preserve full schema structure"""

        class ComplexModel(BaseModel):
            """Complex model with nested fields"""

            name: str
            nested: dict
            items: list

        schema = ComplexModel.model_json_schema()
        strategy = create_strategy_for_capability(
            StructuredOutputCapability.STRICT, schema
        )

        # Full schema should be preserved
        response_schema = strategy.response_format["json_schema"]["schema"]
        assert "properties" in response_schema
        assert "name" in response_schema["properties"]
        assert "nested" in response_schema["properties"]
        assert "items" in response_schema["properties"]

    def test_extra_config_initialization(self):
        """Strategy should initialize extra_config if not provided"""
        schema = SimpleTestModel.model_json_schema()
        strategy = create_strategy_for_capability(
            StructuredOutputCapability.STRICT, schema
        )

        assert strategy.extra_config is not None
        assert isinstance(strategy.extra_config, dict)


class TestCapabilityEnums:
    """Test capability enum values"""

    def test_capability_enum_values(self):
        """Test that capability enum has expected values"""
        assert StructuredOutputCapability.STRICT.value == "strict"
        assert StructuredOutputCapability.BEST_EFFORT.value == "best_effort"
        assert StructuredOutputCapability.FUNCTION_CALLING.value == "function_calling"
        assert StructuredOutputCapability.NONE.value == "none"

    def test_mode_enum_values(self):
        """Test that mode enum has expected values"""
        assert (
            StructuredOutputMode.JSON_SCHEMA_STRICT.value == "json_schema_strict"
        )
        assert StructuredOutputMode.JSON_OBJECT.value == "json_object"
        assert StructuredOutputMode.FUNCTION_CALLING.value == "function_calling"
        assert StructuredOutputMode.PROMPT_ONLY.value == "prompt_only"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
