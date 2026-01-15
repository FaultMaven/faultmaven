"""Datetime utility functions for FaultMaven.

This module provides centralized datetime utilities to avoid circular dependencies.
These functions are pure utilities with no domain model dependencies.

Key Functions:
- utc_timestamp(): Generate UTC timestamp in ISO format
- parse_utc_timestamp(): Parse UTC timestamp string to datetime object
"""

from datetime import datetime, timezone


def utc_timestamp() -> str:
    """Generate UTC timestamp with 'Z' suffix format required by API specification.

    Returns:
        str: UTC timestamp in ISO format with 'Z' suffix (e.g. "2024-01-15T14:30:00.123Z")
    
    Note:
        This function implements datetime serialization directly to avoid circular
        dependency with faultmaven.utils.serialization module.
    """
    # Direct implementation to avoid circular dependency
    # For UTC timezone-aware datetime, format as: YYYY-MM-DDTHH:MM:SS.ffffffZ
    dt = datetime.now(timezone.utc)
    return dt.replace(tzinfo=None).isoformat() + "Z"


def parse_utc_timestamp(timestamp_str: str) -> datetime:
    """Parse UTC timestamp string into timezone-aware datetime object.

    Handles multiple ISO 8601 formats:
    - '2025-10-17T04:02:59+00:00' (timezone-aware with +00:00)
    - '2025-10-17T04:02:59Z' (Zulu time suffix)
    - '2025-10-17T04:02:59' (naive, assumed UTC)
    - '2025-10-17T04:02:59+00:00Z' (CORRUPTED - legacy data only, auto-fixes on save)

    Args:
        timestamp_str: UTC timestamp string in various formats

    Returns:
        datetime: Timezone-aware datetime object in UTC

    Note:
        The corrupted format (both +00:00 and Z) is handled for backwards compatibility
        with old data. When cases are re-saved, timestamps are automatically standardized
        to the proper +00:00 format.
    """
    if timestamp_str.endswith("Z"):
        # Remove 'Z' suffix and parse
        # This also handles corrupted '+00:00Z' format by stripping Z, leaving valid '+00:00'
        dt = datetime.fromisoformat(timestamp_str[:-1])
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    else:
        # Parse ISO format (handles +00:00 automatically)
        dt = datetime.fromisoformat(timestamp_str)
        # If naive, assume UTC
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
