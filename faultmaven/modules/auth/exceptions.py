"""Auth module exceptions.

This module defines exceptions specific to the authentication
and authorization module.
"""

from typing import Any

from faultmaven.exceptions import FaultMavenException


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
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.error_code = error_code
        super().__init__(message, details={**(details or {}), "error_code": error_code})


class TokenError(AuthException):
    """Raised when token operations fail.

    This exception is raised for token generation, validation,
    refresh, or revocation failures.
    """

    def __init__(
        self,
        message: str,
        token_type: str | None = None,
        error_code: str | None = None,
    ):
        self.token_type = token_type
        self.error_code = error_code
        super().__init__(
            message, details={"token_type": token_type, "error_code": error_code}
        )


class TokenExpiredError(TokenError):
    """Raised when a token has expired."""

    def __init__(
        self, message: str = "Token has expired", token_type: str | None = None
    ):
        super().__init__(message, token_type=token_type, error_code="TOKEN_EXPIRED")


class TokenInvalidError(TokenError):
    """Raised when a token is invalid."""

    def __init__(
        self,
        message: str = "Invalid token",
        token_type: str | None = None,
        reason: str | None = None,
    ):
        self.reason = reason
        super().__init__(message, token_type=token_type, error_code="TOKEN_INVALID")


class SessionError(AuthException):
    """Raised when session operations fail."""

    def __init__(
        self,
        message: str,
        session_id: str | None = None,
        error_code: str | None = None,
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
        user_id: str | None = None,
        email: str | None = None,
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
        operation: str | None = None,
        user_id: str | None = None,
    ):
        self.operation = operation
        self.user_id = user_id
        super().__init__(message, details={"operation": operation, "user_id": user_id})


class OrganizationError(AuthException):
    """Raised when organization operations fail."""

    def __init__(
        self,
        message: str,
        organization_id: str | None = None,
        error_code: str | None = None,
    ):
        self.organization_id = organization_id
        self.error_code = error_code
        super().__init__(
            message,
            details={"organization_id": organization_id, "error_code": error_code},
        )
