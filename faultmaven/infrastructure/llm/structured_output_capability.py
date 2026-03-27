"""
Structured Output Capability Detection System

This module provides a provider-agnostic system for detecting and handling
LLM structured output capabilities. Instead of hardcoding model support in
individual providers, this centralizes capability detection and fallback logic.

Design Principles:
- Provider-agnostic: Works for OpenAI, Groq, Anthropic, local models, etc.
- Capability-based: Detects what a model can do, not what provider it's from
- Graceful degradation: Falls back to best available method
- Extensible: Easy to add new capabilities and providers

Architecture:
1. StructuredOutputCapability enum defines capability levels
2. StructuredOutputMode enum defines fallback strategies
3. Providers implement get_structured_output_capability() method
4. milestone_engine uses capability info to adjust prompts
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StructuredOutputCapability(Enum):
    """Levels of structured output support"""

    # STRICT: Model guarantees exact schema adherence (json_schema with strict:true)
    # - Field names guaranteed to match
    # - Types guaranteed to match
    # - No extra fields
    # - No missing required fields
    # Examples: OpenAI GPT-4o, Groq gpt-oss-20b/120b
    STRICT = "strict"

    # BEST_EFFORT: Model attempts schema adherence but not guaranteed (json_object mode)
    # - Usually follows schema if provided in prompt
    # - May rename fields (likelihood → confidence)
    # - May add/remove fields
    # - Validation errors possible
    # Examples: Groq Llama-3.3-70b, most local models
    BEST_EFFORT = "best_effort"

    # FUNCTION_CALLING: Model uses function/tool calling for structured output
    # - Uses tools parameter instead of response_format
    # - Good reliability for structured data
    # - Different API pattern than json_schema
    # Examples: Anthropic Claude, some OpenAI models
    FUNCTION_CALLING = "function_calling"

    # NONE: Model doesn't support structured output
    # - Must parse JSON from natural language response
    # - High failure rate
    # - Requires extensive prompt engineering
    # Examples: Some legacy/small models
    NONE = "none"


class StructuredOutputMode(Enum):
    """Modes for requesting structured output"""

    # Use response_format with json_schema and strict:true
    JSON_SCHEMA_STRICT = "json_schema_strict"

    # Use response_format with json_object (schema in prompt)
    JSON_OBJECT = "json_object"

    # Use tools/function calling with schema
    FUNCTION_CALLING = "function_calling"

    # No special mode, schema only in prompt
    PROMPT_ONLY = "prompt_only"


@dataclass
class StructuredOutputStrategy:
    """Strategy for handling structured output for a specific model"""

    # Capability level of the model
    capability: StructuredOutputCapability

    # Mode to use for requests
    mode: StructuredOutputMode

    # Whether to include full schema in prompt text
    include_schema_in_prompt: bool

    # response_format value to send (if applicable)
    response_format: dict[str, Any] | None

    # Additional provider-specific configuration
    extra_config: dict[str, Any] = None

    def __post_init__(self):
        if self.extra_config is None:
            self.extra_config = {}


def create_strategy_for_capability(
    capability: StructuredOutputCapability, schema: dict[str, Any]
) -> StructuredOutputStrategy:
    """
    Create appropriate strategy based on capability level.

    Args:
        capability: The model's capability level
        schema: The Pydantic schema to use

    Returns:
        StructuredOutputStrategy with appropriate configuration
    """
    if capability == StructuredOutputCapability.STRICT:
        # Model supports strict json_schema - use it!
        return StructuredOutputStrategy(
            capability=capability,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=True,  # Include for redundancy
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.get("title", "Response"),
                    "strict": True,
                    "schema": schema,
                },
            },
        )

    elif capability == StructuredOutputCapability.BEST_EFFORT:
        # Model only supports json_object - MUST include schema in prompt
        return StructuredOutputStrategy(
            capability=capability,
            mode=StructuredOutputMode.JSON_OBJECT,
            include_schema_in_prompt=True,  # CRITICAL: Schema not in response_format
            response_format={"type": "json_object"},
        )

    elif capability == StructuredOutputCapability.FUNCTION_CALLING:
        # Model uses function calling for structured output
        return StructuredOutputStrategy(
            capability=capability,
            mode=StructuredOutputMode.FUNCTION_CALLING,
            include_schema_in_prompt=False,  # Schema goes in tools parameter
            response_format=None,  # Don't use response_format
            extra_config={"use_tools": True},
        )

    else:  # NONE
        # Model doesn't support structured output - prompt engineering only
        return StructuredOutputStrategy(
            capability=capability,
            mode=StructuredOutputMode.PROMPT_ONLY,
            include_schema_in_prompt=True,  # Only way to convey schema
            response_format=None,
        )
