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
from copy import deepcopy
from datetime import datetime, timezone
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
