"""Legacy Auth Models (Stub for Backward Compatibility).

NOTE: This file is a stub that redirects imports to the correct module location.
All auth models are now in:
- faultmaven.modules.auth.domain.models

This stub exists solely to prevent import errors in legacy code that still imports from
faultmaven.models.auth. New code should import directly from the modules.

WARNING: This import is whitelisted in .importlinter contract #10 for backward compatibility.
New code should use DTOs from faultmaven.modules.auth.contracts instead.
"""

# Re-export all auth models from the correct location
# This is whitelisted in .importlinter contract #10 as a temporary exception
from faultmaven.modules.auth.domain.models import (
    AuthenticatedUser,
    AuthToken,
    DevUser,
    TokenClaims,
    TokenPair,
    TokenStatus,
    TokenValidationResult,
)

__all__ = [
    "AuthToken",
    "TokenPair",
    "TokenClaims",
    "TokenStatus",
    "DevUser",
    "TokenValidationResult",
    "AuthenticatedUser",
]
