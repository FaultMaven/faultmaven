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
from typing import Any, Dict, Optional


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
    response_format: Optional[Dict[str, Any]]

    # Additional provider-specific configuration
    extra_config: Dict[str, Any] = None

    def __post_init__(self):
        if self.extra_config is None:
            self.extra_config = {}


def create_strategy_for_capability(
    capability: StructuredOutputCapability, schema: Dict[str, Any]
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


def get_capability_for_provider_and_model(
    provider_name: str, model_name: str
) -> StructuredOutputCapability:
    """
    Determine structured output capability for a provider/model combination.

    This is a centralized capability registry that can be updated as providers
    add support for new features.

    Args:
        provider_name: Provider name (openai, groq, anthropic, etc.)
        model_name: Model name

    Returns:
        StructuredOutputCapability level
    """
    # OpenAI: Full strict support for modern models
    if provider_name == "openai":
        if any(
            x in model_name.lower()
            for x in ["gpt-4o", "gpt-4-turbo", "gpt-4-2024", "gpt-3.5-turbo-0125"]
        ):
            return StructuredOutputCapability.STRICT
        # Older models use function calling
        return StructuredOutputCapability.FUNCTION_CALLING

    # Groq: Limited strict support
    elif provider_name == "groq":
        # Only these models support strict mode
        if model_name in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
            return StructuredOutputCapability.STRICT
        # All other Groq models (Llama, Mixtral, etc.)
        return StructuredOutputCapability.BEST_EFFORT

    # Anthropic: Uses function calling pattern
    elif provider_name == "anthropic":
        return StructuredOutputCapability.FUNCTION_CALLING

    # Gemini: Supports json_schema on newer models
    elif provider_name == "gemini":
        if "2.0" in model_name or "1.5" in model_name:
            return StructuredOutputCapability.STRICT
        return StructuredOutputCapability.BEST_EFFORT

    # Cohere: Best effort JSON mode
    elif provider_name == "cohere":
        return StructuredOutputCapability.BEST_EFFORT

    # Local models: Usually best effort
    elif provider_name == "local":
        # Some fine-tuned models might support strict mode
        if "functionary" in model_name.lower() or "hermes" in model_name.lower():
            return StructuredOutputCapability.FUNCTION_CALLING
        return StructuredOutputCapability.BEST_EFFORT

    # Unknown provider: Conservative default
    return StructuredOutputCapability.BEST_EFFORT
