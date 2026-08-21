"""Centralized serialization utilities for FaultMaven.

This module provides consistent, robust serialization for all data types,
ensuring proper JSON compatibility across the entire application.

Key Features:
- Recursive datetime serialization (handles both timezone-aware and naive)
- Pydantic model serialization with proper datetime handling
- UUID serialization
- Extensible for additional types

Usage:
    from faultmaven.utils.serialization import to_json_compatible, safe_json_dumps

    # Serialize complex objects
    data = {"created_at": datetime.now(timezone.utc), "nested": {"timestamp": datetime.now(UTC)}}
    clean_data = to_json_compatible(data)

    # Direct JSON dumping
    json_str = safe_json_dumps(my_pydantic_model)
"""

import json
import math
from copy import deepcopy
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID


def decode_json_blob(value: Any, *, copy: bool = False) -> Optional[Dict[str, Any]]:
    """Decode a ``JsonBlob`` column value to a dict.

    The single implementation for the ``knowledge_items.metadata`` read
    (fm#1107). It is NOT yet the only reader of a ``JsonBlob`` column anywhere —
    the case module has its own for ``case_metadata``, ``inquiry``,
    ``working_conclusion`` and others — so treat this as the home to converge on,
    not a claim that convergence is finished.

    ``JsonBlob`` is ``Text().with_variant(JSONB, "postgresql")``, so what comes
    back depends on the backend AND on the writer:

    * a **JSON string** — SQLite TEXT, and also PostgreSQL when the writer bound
      an already-serialized ``json.dumps(...)`` value (which
      ``KnowledgeItemRepository`` does: JSONB stores that as a JSON *string
      scalar* and hands back the same ``str``);
    * an **already-decoded dict** — the documented JSONB contract, and what any
      writer binding a real object produces.

    Handling one shape and not the other loses the value **silently**, and that
    is not hypothetical: reading only the dict shape made the KB bootstrap's
    causes comparison return ``None`` on every deployment, so ``causes_unchanged``
    could never be true and every runbook re-ingested on every boot. Both
    branches together make the read independent of who wrote the row.

    Returns ``None`` when there is nothing usable — absent, empty, undecodable,
    or decoding to a non-dict. Note the dict check precedes the falsy one, so an
    empty ``{}`` round-trips to ``{}`` rather than collapsing to ``None``:
    "stored an empty object" and "stored nothing" are different facts, and one
    caller (the repository) distinguishes them. Callers that want a
    ``.get``-safe dict either way write ``decode_json_blob(v) or {}``.

    ``copy`` deep-copies the dict branch. Off by default because the read-only
    callers walk one value per row at startup and a copy per row is waste; ON for
    callers that hand the result out, because the dict branch would otherwise
    ALIAS a session-bound ORM attribute and a caller mutating it would dirty the
    row (a PostgreSQL-only bug that never reproduces on SQLite, where the value
    is a string and every decode is naturally fresh).

    One deliberate widening over the three implementations this replaced: they
    caught ``(JSONDecodeError, TypeError)``, so a ``bytes`` value that is not
    valid UTF-8 raised ``UnicodeDecodeError`` (a ``ValueError``) straight out of
    them; here it returns ``None`` like any other unusable value. Unreachable
    through the columns this reads — ``Text``/``JSONB`` hand back ``str`` or
    ``dict``, never raw bytes — but a decoder for a value that "might be
    anything" should not have one shape that escapes as an exception, so the
    behaviour is stated rather than left to be discovered.

    This was three near-copies of the ``knowledge_items.metadata`` read — in the
    KB bootstrap, the knowledge service, and the item repository — each
    duplicated to avoid a layering violation
    (bootstrap and a domain service may not reach into a repository's private
    helpers, and the repository, being infrastructure, may not import the domain
    service: ``lint-imports`` contract 4). A neutral utility is the home that
    breaks that stalemate. Three copies of a decode whose failure mode is SILENT
    LOSS meant the next divergence had three places to hide, and one of them sat
    under the KB cause seeder's integrity check — a divergence there reads as
    "no causes record" and disables the check for the affected shape (fm#1107).
    """
    if isinstance(value, dict):
        return deepcopy(value) if copy else value
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def to_json_compatible(obj: Any) -> Any:
    """Convert any object to JSON-compatible format.

    This is the single source of truth for serialization in FaultMaven.

    Handles:
    - datetime: UTC → ISO with 'Z', other timezones → ISO with offset, naive → ISO with 'Z'
    - UUID: string representation
    - Pydantic models: .model_dump() or .dict()
    - dict: recursive processing
    - list/tuple/set: recursive processing
    - Other types: returned as-is (int, str, float, bool, None)

    Unknown types are passed through for ``json.dumps`` to accept or reject, so
    this can still raise at encode time. On an error path — where a raise costs
    the caller a 500 — use :func:`to_json_safe` instead.

    Args:
        obj: Object to serialize

    Returns:
        JSON-compatible version of the object

    Examples:
        >>> from datetime import datetime, timezone
        >>> to_json_compatible(datetime(2025, 1, 1, 12, 0, 0))
        '2025-01-01T12:00:00Z'
        >>> to_json_compatible(datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        '2025-01-01T12:00:00Z'
    """
    # Handle None
    if obj is None:
        return None

    # Handle datetime - MOST COMMON CASE FIRST
    if isinstance(obj, datetime):
        if obj.tzinfo is not None:
            # Timezone-aware: use 'Z' for UTC, otherwise include offset
            if obj.tzinfo == timezone.utc or obj.utcoffset() == timezone.utc.utcoffset(
                None
            ):
                # UTC timezone: use 'Z' suffix for clean ISO 8601 format
                return obj.replace(tzinfo=None).isoformat() + "Z"
            else:
                # Non-UTC timezone: include offset (e.g., +05:00)
                return obj.isoformat()
        else:
            # Timezone-naive: assume UTC, add 'Z' suffix
            return obj.isoformat() + "Z"

    # Handle UUID
    if isinstance(obj, UUID):
        return str(obj)

    # Handle Pydantic models (check for model_dump first - Pydantic v2)
    if hasattr(obj, "model_dump"):
        # Pydantic v2: use model_dump() with mode='json' for automatic serialization
        model_dict = obj.model_dump(mode="json")
        # Note: mode='json' already handles datetime, but we still process
        # to ensure consistency with our format
        return to_json_compatible(model_dict)
    elif hasattr(obj, "dict"):
        # Pydantic v1: use dict()
        model_dict = obj.dict()
        return to_json_compatible(model_dict)

    # Handle dict
    if isinstance(obj, dict):
        return {key: to_json_compatible(value) for key, value in obj.items()}

    # Handle list/tuple
    if isinstance(obj, (list, tuple)):
        return [to_json_compatible(item) for item in obj]

    # Handle set
    if isinstance(obj, set):
        return [to_json_compatible(item) for item in obj]

    # Handle primitives (str, int, float, bool) and unknown types
    # Return as-is - json.dumps will handle or error appropriately
    return obj


def safe_json_dumps(obj: Any, **kwargs) -> str:
    """Safely serialize any object to JSON string.

    This combines to_json_compatible with json.dumps for convenience.

    Args:
        obj: Object to serialize
        **kwargs: Additional arguments to pass to json.dumps (indent, etc.)

    Returns:
        JSON string

    Raises:
        TypeError: If object contains types that can't be serialized

    Examples:
        >>> from datetime import datetime, timezone
        >>> safe_json_dumps({"created_at": datetime.now(timezone.utc)})
        '{"created_at": "2025-01-01T12:00:00Z"}'
        >>> safe_json_dumps({"data": [1, 2, 3]}, indent=2)
        '{\\n  "data": [\\n    1,\\n    2,\\n    3\\n  ]\\n}'
    """
    serializable = to_json_compatible(obj)
    return json.dumps(serializable, **kwargs)


def prepare_for_pydantic(data: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare data for Pydantic model instantiation.

    Converts ISO datetime strings back to datetime objects for Pydantic parsing.
    This is the inverse of to_json_compatible for datetime strings.

    Args:
        data: Dictionary potentially containing ISO datetime strings

    Returns:
        Dictionary with datetime strings converted to datetime objects

    Note:
        This is used when deserializing data from Redis/storage before
        passing to Pydantic models.
    """
    from faultmaven.utils.datetime import parse_utc_timestamp

    if not isinstance(data, dict):
        return data

    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            # Try to parse as datetime if it looks like ISO format
            if "T" in value and (
                value.endswith("Z") or "+" in value or "-" in value[-6:]
            ):
                try:
                    result[key] = parse_utc_timestamp(value)
                    continue
                except (ValueError, TypeError):
                    pass
        elif isinstance(value, dict):
            result[key] = prepare_for_pydantic(value)
            continue
        elif isinstance(value, list):
            result[key] = [
                prepare_for_pydantic(item) if isinstance(item, dict) else item
                for item in value
            ]
            continue

        result[key] = value

    return result


# Convenience functions for common patterns
def serialize_pydantic_model(model: Any) -> Dict[str, Any]:
    """Serialize a Pydantic model to JSON-compatible dict.

    Args:
        model: Pydantic model instance

    Returns:
        JSON-compatible dictionary
    """
    return to_json_compatible(model)


def serialize_for_redis(obj: Any) -> str:
    """Serialize object for Redis storage.

    Args:
        obj: Object to serialize (Pydantic model, dict, list, etc.)

    Returns:
        JSON string ready for Redis storage
    """
    return safe_json_dumps(obj)


# ---------------------------------------------------------------------------
# Total ("cannot raise") serialization — for error paths
# ---------------------------------------------------------------------------

# Strings and repr fallbacks are cut to this many characters. Long enough to
# diagnose a bad field, short enough that a response can never mirror a large
# request body back at its sender.
DEFAULT_SAFE_STRING_CHARS = 512

# Containers deeper than this are summarized instead of walked. `input` on a
# validation error is attacker-supplied JSON of arbitrary nesting, and this
# function runs *inside* an exception handler, where a RecursionError becomes
# the 500 the handler exists to prevent.
DEFAULT_SAFE_DEPTH = 6

_TRUNCATED = "... [truncated]"

# ints are rendered by json.dumps via str(), which raises above
# sys.get_int_max_str_digits() (4300 digits by default since 3.11). 256 bits is
# ~78 digits — comfortably below it, and above anything a real payload carries.
_SAFE_INT_BITS = 256


def _safe_text(text: str, limit: int) -> str:
    """Truncate to ``limit`` and strip anything UTF-8 cannot encode.

    Both halves are load-bearing. Starlette renders JSON with
    ``ensure_ascii=False`` and then ``.encode("utf-8")``, so a lone surrogate
    (``json.loads('"\\ud800"')`` produces one, from a *valid* JSON body) raises
    UnicodeEncodeError at render time — a str that looks entirely safe.
    """
    if len(text) > limit:
        text = text[:limit] + _TRUNCATED
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return text


def _safe_fallback(value: Any, limit: int) -> str:
    """Render an object of unknown type as a bounded string, never raising."""
    try:
        if isinstance(value, BaseException):
            # Keeps the pre-#1048 behaviour for the `ctx` case that handler
            # special-cased (a ValueError rendered as its message), widened to
            # every exception type rather than that one.
            text = str(value)
        elif isinstance(value, (datetime, date, time, UUID, Decimal)):
            text = str(value)
        else:
            text = repr(value)
    except Exception:
        try:
            return f"<unrepresentable {type(value).__name__}>"
        except Exception:
            return "<unrepresentable>"
    return _safe_text(text, limit)


def to_json_safe(
    obj: Any,
    *,
    max_string_chars: int = DEFAULT_SAFE_STRING_CHARS,
    max_depth: int = DEFAULT_SAFE_DEPTH,
) -> Any:
    """Convert ``obj`` into something ``JSONResponse`` can always render.

    The contract is **totality**: for any input, the result is composed only of
    ``None``/``bool``/``int``/finite ``float``/UTF-8-encodable ``str``/``list``/
    ``dict`` with ``str`` keys, and this function does not raise. Fidelity is
    given up wherever it conflicts with that — unknown objects become their
    ``repr``, long strings are truncated, deep structures are summarized.

    That is the difference from :func:`to_json_compatible`, and it is why both
    exist. ``to_json_compatible`` preserves the value (it is used to build
    payloads that get *stored*) and passes unknown types straight through for
    ``json.dumps`` to accept or reject. That is correct for a persistence path
    and wrong for an error path, where a raise means the handler itself fails
    and the caller gets a 500 instead of the error it was owed.

    "Whatever ``json.dumps`` accepts" is not the bar, either. Starlette renders
    with ``allow_nan=False`` and ``ensure_ascii=False`` + ``.encode("utf-8")``,
    so ``float('nan')`` and lone surrogates — both reachable from a *valid*
    JSON request body — raise there while plain ``json.dumps`` accepts them.
    This function targets Starlette's stricter encoder.

    Args:
        obj: Anything at all.
        max_string_chars: Cut strings (and repr fallbacks) to this length.
        max_depth: Summarize containers nested deeper than this.

    Returns:
        A JSON-encodable structure. See fm#1048 for the five request shapes
        that crashed the validation handler before this existed.
    """

    def convert(value: Any, depth: int) -> Any:
        # bool before int: bool IS an int, and False must stay False.
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return (
                value
                if value.bit_length() <= _SAFE_INT_BITS
                else f"<int: {value.bit_length()} bits>"
            )
        if isinstance(value, float):
            # allow_nan=False rejects these; repr gives 'nan'/'inf'/'-inf'.
            return value if math.isfinite(value) else repr(value)
        if isinstance(value, str):
            return _safe_text(value, max_string_chars)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return _safe_text(bytes(value).decode("utf-8", "replace"), max_string_chars)

        is_container = isinstance(value, (dict, list, tuple, set, frozenset))
        if is_container and depth >= max_depth:
            # Summarize rather than repr(): repr of a large container would
            # materialize the whole thing just to throw it away.
            return f"<{type(value).__name__}: {len(value)} items>"

        if isinstance(value, dict):
            return {_key(k): convert(v, depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [convert(v, depth + 1) for v in value]

        return _safe_fallback(value, max_string_chars)

    def _key(key: Any) -> str:
        # json.dumps coerces str/int/float/bool/None keys and rejects the rest —
        # and rejects a NaN key even with the coercion. Making every key a safe
        # str is what json would have produced anyway, minus the failure modes.
        if isinstance(key, str):
            return _safe_text(key, max_string_chars)
        converted = convert(key, max_depth)
        return converted if isinstance(converted, str) else str(converted)

    return convert(obj, 0)
