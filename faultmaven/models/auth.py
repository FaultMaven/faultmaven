"""Legacy Auth Models (Stub for Backward Compatibility).

NOTE: This file is a stub that redirects imports to the correct module location.
All auth models are now in:
- faultmaven.modules.auth.domain.models

This stub exists solely to prevent import errors in legacy code that still imports from
faultmaven.models.auth. New code should import directly from the modules.
"""

# Re-export all auth models from the correct location
from faultmaven.modules.auth.domain.models import (
    AuthToken,
    TokenPair,
    TokenClaims,
    TokenStatus,
    DevUser,
    TokenValidationResult,
    AuthenticatedUser,
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
