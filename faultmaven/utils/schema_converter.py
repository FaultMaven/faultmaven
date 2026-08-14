"""Schema Converter - Pydantic to OpenAI Function Schema

This module provides utilities to convert Pydantic models into OpenAI-compatible
function calling schemas for structured output enforcement.

Design Reference: docs/architecture/RESPONSE_FORMAT_INTEGRATION_SPEC.md
"""

import copy
import inspect
import logging
from typing import Any, Dict, Type, get_args, get_origin

from pydantic import BaseModel

logger = logging.getLogger(__name__)


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


class StrictSchemaUnsupported(Exception):
    """A schema cannot be expressed under OpenAI's strict subset.

    Raised rather than returned-as-None so a caller cannot fall through to
    "``strict: true`` with the untransformed schema", which is the request the
    API rejects outright — and which fm#1051 found already being sent.
    """


#: JSON-Schema keywords OpenAI's strict subset does not accept. They are
#: descriptive rather than structural: dropping them narrows nothing about which
#: documents validate, so removing them cannot make a wrong response pass.
#: ``default`` in particular is meaningless once every property is required.
_STRICT_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "format",
        "examples",
    }
)


def to_strict_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite a Pydantic JSON schema into OpenAI's strict-mode subset.

    OpenAI enforces a schema natively only when the schema obeys three rules
    that Pydantic's output does not:

    1. every object carries ``additionalProperties: false``;
    2. every property appears in ``required`` — strict mode has no notion of an
       optional key, so a formerly-optional field becomes required-but-nullable
       (``anyOf: [T, {"type": "null"}]``);
    3. only a subset of validation keywords is accepted.

    **Why this is worth the transformation.** Without it the engine asks for
    enforcement it does not get: ``pydantic_to_openai_function`` emitted no
    ``strict`` key at all, so on a STRICT provider the schema tool was plain
    function calling — the model could omit a required field, the engine dropped
    the whole ``state_updates`` payload, and the turn advanced nothing. That is
    the BEST_EFFORT failure mode occurring on a provider documented as STRICT
    (fm#1051).

    Raises:
        StrictSchemaUnsupported: when the schema contains a construct the subset
            cannot express. The only one the project's schemas hit is a
            **free-form object** — a ``Dict[str, Any]`` field such as
            ``InternalReasoning.milestone_justifications``, which arrives as an
            object with no ``properties``. Strict mode would require
            ``additionalProperties: false`` on it, producing an object that can
            never hold a key — so the milestone justifications the engine gates
            on would be structurally guaranteed empty. Refusing keeps such a
            schema on the existing unenforced path, which is worse than strict
            but far better than silently emptied.
    """

    def convert(node: Any, path: str) -> Any:
        if isinstance(node, list):
            return [convert(item, path) for item in node]
        if not isinstance(node, dict):
            return node

        out = {k: v for k, v in node.items() if k not in _STRICT_UNSUPPORTED_KEYWORDS}

        if out.get("type") == "object" or "properties" in out:
            # The discriminator is the PRESENCE of `properties`, not whether it
            # is non-empty. A model with no fields emits `"properties": {}` and
            # is perfectly expressible — it accepts exactly `{}`. A free-form
            # `Dict[str, Any]` emits no `properties` key at all, and that is the
            # one strict mode cannot represent.
            if "properties" not in out:
                raise StrictSchemaUnsupported(
                    f"{path or 'root'} is a free-form object (no declared "
                    "properties). Strict mode requires additionalProperties: "
                    "false, which would make it permanently empty."
                )
            properties = out["properties"]
            required = set(out.get("required", []))
            converted: Dict[str, Any] = {}
            for prop_name, prop_schema in properties.items():
                child = convert(
                    prop_schema, f"{path}.{prop_name}" if path else prop_name
                )
                if prop_name not in required:
                    # Optional becomes required-but-nullable. `null` is the wire
                    # spelling of "the model had nothing for this"; the Python
                    # side must tolerate it (see schemas._coerce_null_to_default).
                    child = _nullable(child)
                converted[prop_name] = child
            out["properties"] = converted
            out["required"] = list(properties.keys())
            out["additionalProperties"] = False

        for key in ("items", "anyOf", "oneOf", "allOf", "prefixItems"):
            if key in out:
                out[key] = convert(out[key], path)

        return out

    return convert(copy.deepcopy(schema), "")


def _nullable(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Widen a property schema to admit ``null``, without double-wrapping."""
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if any(b.get("type") == "null" for b in branches if isinstance(b, dict)):
            return schema
        return {**schema, "anyOf": [*branches, {"type": "null"}]}

    if schema.get("type") == "null":
        return schema

    # Everything structural moves INTO the branch; only the annotations stay
    # outside, so they still describe the property as a whole.
    #
    # The split must be "annotations out" rather than "known structural keys
    # in": an allowlist of `type`/`enum`/`const`/`items` silently left
    # `properties`, `required` and `additionalProperties` as siblings of the
    # union, producing a branch of bare `{"type": "object"}` — an object with no
    # declared properties, which is the one shape strict mode rejects. Any
    # nested-model field with a default hit it.
    _ANNOTATIONS = ("title", "description")
    inner = {k: v for k, v in schema.items() if k not in _ANNOTATIONS}
    outer = {k: v for k, v in schema.items() if k in _ANNOTATIONS}
    if not inner:
        return schema
    return {**outer, "anyOf": [inner, {"type": "null"}]}


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


def pydantic_to_strict_openai_tools(
    model: Type[BaseModel],
    name: str = None,
    description: str = None,
) -> list[Dict[str, Any]]:
    """``pydantic_to_openai_tools`` with native schema enforcement requested.

    Adds ``strict: true`` to the function definition and rewrites its parameters
    into OpenAI's strict subset, so the provider constrains generation instead of
    merely being asked nicely in the prompt.

    Falls back to the ordinary non-strict tool when the schema cannot be
    expressed strictly (see :func:`to_strict_schema`) — the four
    ``InvestigationResponse_*`` schemas are in that category today, because their
    ``Dict[str, Any]`` fields have no strict representation. Returning a working
    unenforced tool is correct here: strict is an improvement to pursue where it
    is available, not a precondition for the turn running at all.
    """
    tools = pydantic_to_openai_tools(model, name, description)
    function = tools[0]["function"]
    try:
        function["parameters"] = to_strict_schema(function["parameters"])
    except StrictSchemaUnsupported as exc:
        logger.info(
            "strict_schema_unavailable: %s is not expressible in OpenAI's strict "
            "subset, so its tool stays unenforced (%s)",
            model.__name__,
            exc,
        )
        return tools
    function["strict"] = True
    return tools


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
