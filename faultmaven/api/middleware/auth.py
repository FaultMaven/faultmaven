"""JWT Authentication Middleware (TASK-017)

Purpose: FastAPI dependencies for JWT-based authentication and authorization.

This module provides reusable authentication dependencies:
- get_current_user: Extract and verify JWT from Authorization header
- get_current_user_optional: Optional authentication (returns None if no token)
- require_platform_admin: Require the cross-tenant operator role

There is deliberately no organization-role or permission dependency here. The
organization's roles are its management vocabulary and gate no data (ADR-017
D5); where they are enforced, organization management resolves them per request
against ``role_permissions`` (#1040, owner ruling 2026-09-24).

Usage:
    from faultmaven.api.middleware.auth import get_current_user

    @router.get("/cases")
    async def list_cases(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ):
        # current_user.user_id, current_user.organization_id verified
        ...

Design Reference: TASK-017 JWT Authentication & Authorization Middleware
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from faultmaven.api.exception_handlers import revocation_state_unknown_http_exception
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    AuthService,
    RevocationStateUnknownError,
    TokenRevocationError,
)

logger = logging.getLogger(__name__)

# HTTP Bearer security scheme for OpenAPI documentation
bearer_scheme = HTTPBearer(auto_error=False)

# Module-level test service for backward compatibility with tests
# This allows unit tests to set a mock service without going through FastAPI DI
_test_auth_service: Optional[AuthService] = None


async def get_auth_service(request: Request) -> AuthService:
    """Get AuthService instance from app.state (Composition Root).

    Args:
        request: FastAPI request object (automatically injected by FastAPI)

    Returns:
        The AuthService the composition root published on ``app.state``, or
        the module-level test service when one is set.

    Raises:
        HTTPException 503: No AuthService is composed. Never a stand-in built
            here (#2103): one would have no revocation store, so a revoked or
            logged-out token would keep working until it expired.
    """
    # Check test service first (for unit tests)
    if _test_auth_service is not None:
        return _test_auth_service

    try:
        auth_service = getattr(request.app.state, "auth_service", None)
    except AttributeError:
        auth_service = None
    if auth_service is None:
        logger.error("No AuthService is composed; refusing the request")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )
    return auth_service


def set_auth_service(
    service: Optional[AuthService], request: Optional[Request] = None
) -> None:
    """Set the AuthService instance (for testing/DI).

    Args:
        service: AuthService instance to use (or None to clear)
        request: Optional FastAPI request object (if provided, sets on app.state)

    Note:
        For testing, can be called without request to set module-level test service.
        For production DI, can be called with request to set on app.state.
    """

    if request is not None:
        try:
            request.app.state.auth_service = service
            logger.debug("AuthService set in app.state")
        except Exception as e:
            logger.warning(f"Failed to set AuthService in app.state: {e}")
    else:
        # Set module-level test service for backward compatibility
        global _test_auth_service
        _test_auth_service = service
        logger.debug("AuthService set in module-level test variable")


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    """Extract and verify JWT from Authorization header.

    Validates the token and returns AuthenticatedUser with verified claims.

    Header format: "Bearer <jwt_token>"

    Args:
        request: The incoming request, read for its Authorization header
        credentials: HTTPBearer credentials (for OpenAPI docs)
        auth_service: AuthService instance

    Returns:
        AuthenticatedUser with user_id, organization_id, email, roles, permissions

    Raises:
        HTTPException 401: Missing, malformed, expired or revoked token. Every
            authentication failure is a 401 — a credential this endpoint will
            not accept is not a 403, which is reserved for a valid credential
            lacking permission (see the revocation handler below).
    """
    # Extract token from header
    token = _extract_token(request.headers.get("authorization"), credentials)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        # Verify token and check revocation (if Redis configured)
        user = await auth_service.extract_user_from_token_with_revocation_check(token)
        return user

    except AuthenticationError as e:
        logger.debug(f"Authentication failed: {e.message} ({e.error_code})")

        if e.error_code == "TOKEN_EXPIRED":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired. Please refresh or re-authenticate.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Static, never `e.message` (#1859). `AuthService.verify_token` builds
        # that message from PyJWT's own text and from the token's `type`
        # claim, so echoing it hands the caller the library's diagnosis and
        # reflects attacker-chosen content. The reason stays in the debug log
        # above; the body says only that the token was refused.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token. Please re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    except TokenRevocationError:
        # 401, NOT 403. A revoked token is an invalid credential, which is what
        # 401 means (RFC 6750 §3.1 `invalid_token`); 403 means the credential is
        # good but the caller may not do this. Answering 403 tells a client "you
        # are signed in, you just can't do that" about a token that is dead, so
        # the client keeps it and keeps failing.
        #
        # This is a cross-route consistency fix, not only a spelling one. The
        # other authenticated path — `api/v1/auth_dependencies.py`
        # `get_current_user_optional` → `require_authentication` — already
        # answers 401 here, so the SAME revoked token produced 401 on case
        # routes and 403 on the routes reaching this dependency (admin). One
        # client can only reasonably implement one rule.
        #
        # Made urgent by account-scoped logout (this PR): revocation used to be
        # a rare admin action and is now what every deliberate sign-out does to
        # the user's OTHER client, so the copilot — which tears down on 401 and
        # treats 403 as a permission problem — rendered "Access Denied. Contact
        # your administrator." and kept its dead tokens instead of signing out.
        logger.debug("Token has been revoked")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please re-authenticate.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    except RevocationStateUnknownError as e:
        # 503 with its own error code, NOT the 401 above and NOT the catch-all
        # below (#1478). The store could not be read, so this token was never
        # judged — telling the caller "invalid token" would send the copilot
        # into the tear-down-and-re-authenticate path over a database blip that
        # a retry clears, and would make a revoked token and an unreadable
        # store indistinguishable to whoever is paged.
        #
        # Placed above `except Exception` deliberately: that handler answers
        # 401, so without this clause the distinct code would exist and never
        # reach anybody.
        logger.warning("Revocation state unknown (%s): refusing the request", e.kind)
        raise revocation_state_unknown_http_exception()

    except Exception as e:
        logger.error(f"Unexpected authentication error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed. Please try again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> Optional[AuthenticatedUser]:
    """Get current user if authenticated, or None if not.

    Use this for endpoints that work both authenticated and unauthenticated.

    Args:
        request: The incoming request, read for its Authorization header
        credentials: HTTPBearer credentials
        auth_service: AuthService instance

    Returns:
        AuthenticatedUser if authenticated, None otherwise

    Raises:
        HTTPException 503: The revocation store could not be read (#1478).
            The one failure here that is NOT answered with ``None``: "we could
            not find out whether this credential is live" is not the same
            statement as "no credential was presented", and degrading it to
            anonymous would hide a storage fault behind whatever the route
            does for an unauthenticated caller.
    """
    token = _extract_token(request.headers.get("authorization"), credentials)

    if not token:
        return None

    try:
        return await auth_service.extract_user_from_token_with_revocation_check(token)
    except (AuthenticationError, TokenRevocationError):
        return None
    except RevocationStateUnknownError as e:
        logger.warning("Revocation state unknown (%s): refusing the request", e.kind)
        raise revocation_state_unknown_http_exception()
    except Exception as e:
        logger.warning(f"Optional authentication error: {e}")
        return None


async def require_platform_admin(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require the cross-tenant operator role (see ``PLATFORM_ADMIN_ROLE``).

    Shortcut dependency for operator-only endpoints — those acting on the
    deployment as a whole rather than on one organization. The org-scoped
    ``Role.ADMIN`` does not satisfy it.

    Usage:
        @router.get("/admin/cases", dependencies=[Depends(require_platform_admin)])

    Returns:
        AuthenticatedUser if a platform admin

    Raises:
        HTTPException 403: User is not a platform admin
    """
    if not current_user.is_platform_admin():
        logger.debug(
            f"Platform admin required: user {current_user.user_id} is not a platform admin"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        )
    return current_user


def _extract_token(
    authorization: Optional[str],
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    """Extract JWT token from authorization header or credentials.

    Supports both:
    - Raw Authorization header: "Bearer <token>"
    - HTTPBearer credentials (for OpenAPI docs)

    Args:
        authorization: Raw Authorization header value, or None
        credentials: HTTPBearer credentials

    Returns:
        Token string or None
    """
    # Try HTTPBearer credentials first (cleaner)
    if credentials and credentials.credentials:
        return credentials.credentials

    # Fall back to raw header parsing
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:].strip()
            if token:
                return token
        else:
            logger.debug(
                "Invalid Authorization header format. Expected 'Bearer <token>'"
            )

    return None


# Backward compatibility aliases
get_authenticated_user = get_current_user
get_optional_user = get_current_user_optional
