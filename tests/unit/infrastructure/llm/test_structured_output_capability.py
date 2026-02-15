"""
Unit tests for Structured Output Capability Detection System

Tests the provider-agnostic strategy creation.

Note: Provider-specific capability detection is now tested in
test_provider_capability_overrides.py as each provider implements
its own get_structured_output_capability() method.
"""

import pytest
from pydantic import BaseModel

from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
    create_strategy_for_capability,
)


class SimpleTestModel(BaseModel):
    """Simple model for testing"""

    name: str
    value: int


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
        assert strategy.include_schema_in_prompt is True  # Only way to convey schema
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
        assert StructuredOutputMode.JSON_SCHEMA_STRICT.value == "json_schema_strict"
        assert StructuredOutputMode.JSON_OBJECT.value == "json_object"
        assert StructuredOutputMode.FUNCTION_CALLING.value == "function_calling"
        assert StructuredOutputMode.PROMPT_ONLY.value == "prompt_only"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
