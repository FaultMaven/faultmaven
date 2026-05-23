"""Tolerant Pydantic schema validation with field-level degradation.

Architectural rationale:

LLM-generated structured output deviates from strict Pydantic schemas in
many ways: vague-description misreads (prose where objects expected),
structural JSON malformation (missing braces), cross-field rule
violations (omitted conditional fields), null-where-typed-dict, and
provider-specific quirks (different per Fireworks / Gemini / Anthropic /
OpenAI). Patching each variant individually is unsustainable — every
new provider or new schema field brings new variants.

This module applies Postel's law to LLM contracts:
  - Be strict in what we send (schema descriptions stay strong)
  - Be tolerant in what we accept (parse what works, drop what doesn't,
    keep the turn alive)

``tolerant_validate`` iteratively strips invalid fields from a content
dict and re-validates. For each Pydantic error:

  - If the failing element is a list item (loc ends in an int): the
    item is removed from the list.
  - If the failing element is a dict field with a default value (i.e.
    Optional, default=, or default_factory=): the field is set to its
    declared default.
  - If the failing field is required and has no default: hard-fail.
    Critical fields like ``agent_response`` correctly stop the turn.

The conservative policy ("only Optional + explicit-default fields can
be stripped") means downstream engine code's existing None/empty
handling already covers the degraded cases.

Dropped fields are logged and recorded in a returned list for
observability — sustained drops on a field signal a real schema or
prompt-template issue worth fixing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

logger = logging.getLogger(__name__)


# Safety cap. Each iteration of the strip-and-retry loop strips at least
# one field (otherwise we hard-fail). 10 is generous — real cases hit 1-3.
_MAX_STRIP_ITERATIONS = 10


def tolerant_validate(
    content_obj: Any,
    schema_model: type[BaseModel],
) -> tuple[BaseModel, list[dict[str, Any]]]:
    """Validate ``content_obj`` against ``schema_model`` with field-level degradation.

    Strict by default. On ``ValidationError``, walks the errors() list,
    strips each "strippable" failing element (list item, or dict field
    with a default), and re-validates. Iterates until validation passes
    or no progress can be made — at which point the latest
    ``ValidationError`` is re-raised so the caller's existing error
    handling kicks in.

    Returns ``(parsed_model, drops)`` where ``drops`` is a list of dicts
    describing what was stripped (for metadata/observability).

    Args:
        content_obj: Already-decoded dict (typically from ``json.loads``).
        schema_model: The Pydantic model class to validate against.

    Returns:
        Tuple of (validated model instance, list of drop records).

    Raises:
        ValidationError: When validation fails on a non-strippable
            required field, or when ``content_obj`` is so degraded that
            no recoverable shape exists.
    """
    drops: list[dict[str, Any]] = []
    last_error: Optional[ValidationError] = None

    for _iteration in range(_MAX_STRIP_ITERATIONS):
        try:
            return schema_model.model_validate(content_obj), drops
        except ValidationError as e:
            last_error = e
            # Process errors in reverse loc-order so that list-index
            # removals don't invalidate later indices in the same pass.
            sorted_errors = sorted(
                e.errors(),
                key=lambda err: tuple(
                    (1, p) if isinstance(p, int) else (0, str(p)) for p in err["loc"]
                ),
                reverse=True,
            )
            stripped_any = False
            for err in sorted_errors:
                drop = _try_strip_error(content_obj, err, schema_model)
                if drop is not None:
                    drops.append(drop)
                    stripped_any = True
            if not stripped_any:
                # Nothing strippable — re-raise so caller's existing
                # error path (self-correction, fallback, etc.) takes over.
                raise

    # Hit the iteration cap. This means we keep stripping but each pass
    # produces NEW errors that are themselves strippable but never
    # converge. Pathological; re-raise the last error so the failure is
    # visible.
    logger.warning(
        f"tolerant_validate hit max iterations on {schema_model.__name__}; "
        f"giving up after {len(drops)} drops"
    )
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"tolerant_validate exhausted iterations without surfacing a ValidationError"
    )


def _try_strip_error(
    content_obj: Any,
    error: dict[str, Any],
    schema_model: type[BaseModel],
) -> Optional[dict[str, Any]]:
    """Try to strip the element responsible for ``error`` from ``content_obj``.

    Returns a drop-record dict on success, or None if the error isn't
    strippable under the conservative policy.
    """
    loc = error.get("loc", ())
    if not loc:
        # Whole-object error (e.g. root model_validator failure). Can't strip.
        return None

    parent_loc = loc[:-1]
    last = loc[-1]

    parent = _navigate(content_obj, parent_loc)
    if parent is None:
        return None

    drop_loc_str = ".".join(str(p) for p in loc)
    drop = {
        "loc": drop_loc_str,
        "error_type": error.get("type", "unknown"),
        "msg": (error.get("msg") or "")[:200],
    }

    # Case 1: list item
    if isinstance(last, int):
        if not isinstance(parent, list):
            return None
        if not (0 <= last < len(parent)):
            return None
        del parent[last]
        drop["action"] = "removed_list_item"
        return drop

    # Case 2: dict field
    if isinstance(last, str):
        if not isinstance(parent, dict):
            return None
        parent_type = _resolve_schema_at(schema_model, parent_loc)
        if parent_type is None or not _is_pydantic_model(parent_type):
            # Unknown parent type — be conservative, don't strip silently
            return None
        field_info = parent_type.model_fields.get(last)
        if field_info is None:
            # Field isn't declared on the model — Pydantic must be in
            # extra='ignore' mode and complaining about something else.
            # Don't touch.
            return None
        if field_info.is_required():
            # Required field — must NOT strip. Hard-fail at re-raise.
            return None

        # Replace with declared default
        if field_info.default_factory is not None:
            default_value = field_info.default_factory()
        elif field_info.default is not PydanticUndefined:
            default_value = field_info.default
        else:
            # Optional with no explicit default → None
            default_value = None
        parent[last] = default_value
        drop["action"] = "defaulted_dict_field"
        return drop

    return None


def _navigate(content_obj: Any, loc: tuple) -> Any:
    """Walk ``content_obj`` along ``loc`` and return the parent container.

    Returns None if any step doesn't resolve (e.g. the path doesn't
    exist in content_obj). Tolerates both dict-key and list-index parts.
    """
    current = content_obj
    for part in loc:
        if isinstance(part, int):
            if not isinstance(current, list) or not (0 <= part < len(current)):
                return None
            current = current[part]
        elif isinstance(part, str):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _resolve_schema_at(root_schema: type[BaseModel], loc: tuple) -> Optional[Any]:
    """Walk ``loc`` through ``root_schema``'s type annotations.

    Returns the type at the given location, or None if the path can't
    be resolved (e.g. crosses a non-model annotation, or hits a field
    that doesn't exist).

    Handles:
      - string parts: descend into a Pydantic model field (unwrapping
        Optional/Union to find the model type)
      - int parts: unwrap List[X] to X
    """
    current: Any = root_schema
    for part in loc:
        if isinstance(part, int):
            inner = _unwrap_list_item_type(current)
            if inner is None:
                return None
            current = inner
        elif isinstance(part, str):
            if not _is_pydantic_model(current):
                return None
            field_info = current.model_fields.get(part)
            if field_info is None:
                return None
            current = _unwrap_optional(field_info.annotation)
        else:
            return None
    return current


def _is_pydantic_model(tp: Any) -> bool:
    """Return True when ``tp`` is a Pydantic BaseModel subclass."""
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _unwrap_optional(tp: Any) -> Any:
    """Strip Optional[X] / Union[X, None] to X. Leaves other types unchanged."""
    origin = get_origin(tp)
    if origin is Union:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
        # Multi-type Union (other than Optional) — return as-is; we
        # don't try to be clever about which branch to pick.
        return tp
    return tp


def _unwrap_list_item_type(tp: Any) -> Optional[Any]:
    """Given List[X] (possibly wrapped in Optional), return X. Else None."""
    inner = _unwrap_optional(tp)
    origin = get_origin(inner)
    if origin in (list, tuple, set):
        args = get_args(inner)
        if args:
            return _unwrap_optional(args[0])
    return None
