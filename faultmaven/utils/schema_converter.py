"""Schema Converter - Pydantic to OpenAI Function Schema

This module provides utilities to convert Pydantic models into OpenAI-compatible
function calling schemas for structured output enforcement.

Design Reference: docs/architecture/RESPONSE_FORMAT_INTEGRATION_SPEC.md
"""

import copy
import inspect
from typing import Any, Dict, Type, get_args, get_origin

from pydantic import BaseModel


def _inline_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve every ``$ref`` against a top-level ``$defs`` block and drop ``$defs``.

    Pydantic v2 emits nested-model schemas as ``$ref`` references into a
    sibling ``$defs`` map. Some OpenAI-compatible providers (Fireworks
    observed; their server-side ``referencing`` library raises
    ``AttributeError("'NoneType' object has no attribute 'lookup'")`` on
    these refs) fail to dereference these when the schema is passed as a
    tool's ``parameters``. Inlining is the universal fix: any JSON-Schema
    validator accepts a schema with no ``$ref``/``$defs`` at all.

    The function is non-mutating (returns a deep copy), handles arbitrary
    nesting (refs that reference defs that reference defs), and detects
    recursive references — raising rather than looping. None of the
    project's structured-output schemas are recursive; if that changes,
    the caller needs a different strategy (e.g., bounded inlining depth).
    """
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", None)
    if not defs:
        return schema

    def resolve(node: Any, in_progress: set[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                key = ref[len("#/$defs/") :]
                if key in in_progress:
                    raise ValueError(
                        f"Recursive $ref to '{key}' cannot be inlined. "
                        f"This schema is not safe for providers that require "
                        f"flat schemas. Consider a non-recursive design."
                    )
                target = defs.get(key)
                if target is None:
                    # Leave the $ref intact — caller's responsibility. We
                    # don't fabricate a placeholder; an unresolvable ref
                    # signals a malformed schema upstream.
                    return node
                # Merge: inline the resolved target, but preserve any
                # sibling keys on the $ref node (e.g. description overrides).
                resolved = resolve(target, in_progress | {key})
                if not isinstance(resolved, dict):
                    return resolved
                merged = {k: v for k, v in node.items() if k != "$ref"}
                # $ref siblings win over the resolved target (Pydantic
                # convention: title/description on the use-site override
                # the definition).
                out = {**resolved, **merged}
                return out
            return {k: resolve(v, in_progress) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(item, in_progress) for item in node]
        return node

    return resolve(schema, set())


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

    # Get JSON schema from Pydantic. Pydantic v2 emits nested-model
    # references via a sibling $defs block + $ref pointers. Some
    # OpenAI-compatible providers (Fireworks observed) fail to dereference
    # those when the schema is passed as a tool's parameters — their
    # server-side resolver raises an AttributeError on the lookup.
    # Inlining all refs eliminates that whole class of bug and keeps the
    # tool's parameters schema self-contained.
    schema = _inline_refs(model.model_json_schema())

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
    # Same rationale as pydantic_to_openai_function: inline all $defs so
    # downstream resolvers don't have to walk references. OpenAI's strict
    # mode accepts both inlined and $defs-based schemas; inlining is
    # universally valid and avoids provider-specific resolver bugs.
    schema = _inline_refs(model.model_json_schema())

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
