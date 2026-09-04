"""Auth module exceptions.

This module defines exceptions specific to the authentication
and authorization module.
"""

from typing import Any, Dict, Optional

from faultmaven.exceptions import FaultMavenException, ServiceError


class AuthException(FaultMavenException):
    """Base exception for authentication/authorization errors."""

    pass


class AuthenticationError(AuthException):
    """Raised when authentication fails.

    This exception is raised for invalid credentials, expired tokens,
    or other authentication failures.
    """

    def __init__(
        self,
        message: str = "Authentication failed",
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.error_code = error_code
        super().__init__(message, details={**(details or {}), "error_code": error_code})


class SSOAuthenticationError(AuthException):
    """Raised when an external IdP (e.g. WorkOS AuthKit) rejects or fails a code
    exchange.

    Carries no provider-specific detail by design: callers surface a uniform
    failure so the SSO callback cannot become an error oracle. See ADR-015.
    """


class SSOProvisioningError(AuthException):
    """Raised when the IdP half of a personal tenant cannot be provisioned.

    Distinct from :class:`SSOAuthenticationError` because the identity is not in
    doubt: the code exchange already succeeded and the subject is known. What
    failed is creating (or confirming) the IdP organization that holds that one
    member — a provider outage, a rejected request, or a provider with no such
    concept at all.

    Carries no provider-specific detail, for the same no-error-oracle reason.
    The login refuses; the next attempt can complete from where this one stopped
    because the IdP-side work is keyed on a deterministic external id.
    """


class TokenError(AuthException):
    """Raised when token operations fail.

    This exception is raised for token generation, validation,
    refresh, or revocation failures.
    """

    def __init__(
        self,
        message: str,
        token_type: Optional[str] = None,
        error_code: Optional[str] = None,
    ):
        self.token_type = token_type
        self.error_code = error_code
        super().__init__(
            message, details={"token_type": token_type, "error_code": error_code}
        )


class TokenExpiredError(TokenError):
    """Raised when a token has expired."""

    def __init__(
        self, message: str = "Token has expired", token_type: Optional[str] = None
    ):
        super().__init__(message, token_type=token_type, error_code="TOKEN_EXPIRED")


class TokenInvalidError(TokenError):
    """Raised when a token is invalid."""

    def __init__(
        self,
        message: str = "Invalid token",
        token_type: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        self.reason = reason
        super().__init__(message, token_type=token_type, error_code="TOKEN_INVALID")


class SessionError(AuthException):
    """Raised when session operations fail."""

    def __init__(
        self,
        message: str,
        session_id: Optional[str] = None,
        error_code: Optional[str] = None,
    ):
        self.session_id = session_id
        self.error_code = error_code
        super().__init__(
            message, details={"session_id": session_id, "error_code": error_code}
        )


class UserNotFoundError(AuthException):
    """Raised when a user is not found."""

    def __init__(
        self,
        message: str = "User not found",
        user_id: Optional[str] = None,
        email: Optional[str] = None,
    ):
        self.user_id = user_id
        self.email = email
        super().__init__(message, details={"user_id": user_id, "email": email})


class UserStoreError(AuthException):
    """Raised when user store operations fail.

    This exception is raised for database/persistence errors
    in user operations.
    """

    def __init__(
        self,
        message: str,
        operation: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.operation = operation
        self.user_id = user_id
        super().__init__(message, details={"operation": operation, "user_id": user_id})


class OrganizationError(AuthException):
    """Raised when organization operations fail."""

    def __init__(
        self,
        message: str,
        organization_id: Optional[str] = None,
        error_code: Optional[str] = None,
    ):
        self.organization_id = organization_id
        self.error_code = error_code
        super().__init__(
            message,
            details={"organization_id": organization_id, "error_code": error_code},
        )
