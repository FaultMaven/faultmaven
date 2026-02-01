"""Schema Converter - Pydantic to OpenAI Function Schema

This module provides utilities to convert Pydantic models into OpenAI-compatible
function calling schemas for structured output enforcement.

Design Reference: docs/architecture/RESPONSE_FORMAT_INTEGRATION_SPEC.md
"""

import inspect
from typing import Any, Dict, Type, get_args, get_origin

from pydantic import BaseModel


def pydantic_to_openai_function(
    model: Type[BaseModel],
    name: str = None,
    description: str = None,
) -> Dict[str, Any]:
    """Convert Pydantic model to OpenAI function calling schema

    Args:
        model: Pydantic model class
        name: Function name (defaults to model name)
        description: Function description (defaults to model docstring)

    Returns:
        OpenAI function schema dict

    Example:
        >>> schema = pydantic_to_openai_function(
        ...     ConsultantResponse,
        ...     name="respond_consultant",
        ...     description="Respond in consultant mode"
        ... )
        >>> # Use with OpenAI:
        >>> tools = [{"type": "function", "function": schema}]
    """
    if name is None:
        name = model.__name__

    if description is None:
        description = model.__doc__ or f"{model.__name__} response"

    # Get JSON schema from Pydantic
    schema = model.model_json_schema()

    # Convert to OpenAI function format
    function_schema = {
        "name": name,
        "description": description.strip(),
        "parameters": {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        },
    }

    # Add descriptions from field metadata if available
    if "$defs" in schema:
        function_schema["parameters"]["$defs"] = schema["$defs"]

    return function_schema


def pydantic_to_openai_tools(
    model: Type[BaseModel],
    name: str = None,
    description: str = None,
) -> list[Dict[str, Any]]:
    """Convert Pydantic model to OpenAI tools format

    This is a convenience wrapper that returns the format expected by
    the OpenAI API's tools parameter.

    Args:
        model: Pydantic model class
        name: Function name (defaults to model name)
        description: Function description (defaults to model docstring)

    Returns:
        List containing single tool dict in OpenAI format

    Example:
        >>> tools = pydantic_to_openai_tools(ConsultantResponse)
        >>> response = await llm_provider.generate(
        ...     prompt=prompt,
        ...     tools=tools,
        ...     tool_choice="required"
        ... )
    """
    function_schema = pydantic_to_openai_function(model, name, description)

    return [
        {
            "type": "function",
            "function": function_schema,
        }
    ]


def create_response_format_json_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    """Create response_format for JSON mode with strict schema enforcement

    Creates OpenAI-compatible structured output format with strict: True.
    This enforces exact schema adherence, preventing field name variations,
    type mismatches, and LLM hallucinations.

    IMPORTANT: This uses json_schema mode (NOT json_object), which:
    - Guarantees field names match schema exactly
    - Prevents type mismatches and validation errors
    - Requires no prompt engineering ("respond with JSON")
    - Supports Pydantic models with Optional fields

    Args:
        model: Pydantic BaseModel class defining the response structure

    Returns:
        Response format dict in OpenAI json_schema format:
        {
            "type": "json_schema",
            "json_schema": {
                "name": "ModelName",
                "strict": True,
                "schema": {...}  # Full Pydantic JSON schema
            }
        }

    Raises:
        None - Pydantic v2 handles optional fields correctly with anyOf pattern

    Example:
        >>> from faultmaven.core.investigation.schemas import InquiryResponse
        >>> response_format = create_response_format_json_schema(InquiryResponse)
        >>> response = await llm_provider.generate(
        ...     prompt="User reports login is down",
        ...     max_tokens=4000,
        ...     response_format=response_format
        ... )
        >>> result = InquiryResponse.model_validate_json(response.content)

    Notes:
        - Pydantic v2 automatically generates strict-mode compatible schemas
        - Optional fields use anyOf: [type, null] pattern (not 'default' keyword)
        - Compatible with OpenAI, Groq, and other OpenAI-compatible APIs
        - See docs/development/structured-output-guide.md for full details

    References:
        - https://platform.openai.com/docs/guides/structured-outputs
        - docs/development/structured-output-guide.md
    """
    schema = model.model_json_schema()

    return {
        "type": "json_schema",
        "json_schema": {
            "name": model.__name__,
            "strict": True,
            "schema": schema,
        },
    }


def create_json_mode_format() -> Dict[str, str]:
    """Create response_format for simple JSON mode

    This enables JSON mode without strict schema validation.
    Compatible with more models but less reliable than json_schema mode.

    Returns:
        Response format dict for JSON mode

    Example:
        >>> response_format = create_json_mode_format()
        >>> response = await llm_provider.generate(
        ...     prompt=prompt,
        ...     response_format=response_format
        ... )
    """
    return {"type": "json_object"}
