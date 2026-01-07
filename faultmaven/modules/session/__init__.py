"""Session Module - Authentication Session Management.

This module provides authentication session management for the FaultMaven platform,
including session lifecycle, multi-device support, and session stores.

Don't eagerly import to avoid circular imports.
Components will be imported directly when needed.
"""

__all__ = [
    "api",
    "domain",
    "infrastructure",
]
