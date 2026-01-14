"""Authentication API Models

Purpose: Request/response models for authentication endpoints

This module provides Pydantic models for authentication API operations,
including login requests, token responses, and user profiles. These models
ensure proper validation and consistent API contracts.

Key Components:
- DevLoginRequest: Development login input validation
- AuthTokenResponse: Standard OAuth2-style token response
- UserProfile: Public user information for API responses
- AuthError: Structured error responses
"""

from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import Optional, List
from datetime import datetime
import re


class DevLoginRequest(BaseModel):
    """Request model for development login

    Validates user input for the dev-login endpoint.
    Supports username-based login with optional user details.
    """
    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        description="Username or email address (3-50 chars)"
    )
    email: Optional[str] = Field(
        None,
        description="Optional email address (will auto-generate if not provided)"
    )
    display_name: Optional[str] = Field(
        None,
        max_length=100,
        description="Optional display name (will auto-generate if not provided)"
    )

    @field_validator('username')
    @classmethod
    def validate_username(cls, v):
        """Validate username format (allows email addresses)"""
        # Allow email addresses OR traditional usernames
        email_pattern = r'^[^@]+@[^@]+\.[^@]+$'
        username_pattern = r'^[a-zA-Z0-9._-]+$'

        if not (re.match(email_pattern, v) or re.match(username_pattern, v)):
            raise ValueError('Username must be a valid email address or contain only letters, numbers, dots, underscores, and hyphens')
        return v.lower()  # Store usernames in lowercase for consistency

    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        """Validate email format if provided"""
        if v is not None:
            if not re.match(r'^[^@]+@[^@]+\.[^@]+$', v):
                raise ValueError('Invalid email format')
            return v.lower()  # Store emails in lowercase for consistency
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "john.doe",
                "email": "john.doe@faultmaven.local",
                "display_name": "John Doe"
            }
        }
    )


class UserProfile(BaseModel):
    """Public user profile information

    Represents user information safe for API responses.
    Excludes sensitive information like hashed passwords.
    """
    user_id: str = Field(
        ...,
        description="Unique user identifier"
    )
    username: str = Field(
        ...,
        description="Username"
    )
    email: str = Field(
        ...,
        description="Email address"
    )
    display_name: str = Field(
        ...,
        description="Display name"
    )
    created_at: str = Field(
        ...,
        description="Account creation timestamp (ISO format)"
    )
    is_dev_user: bool = Field(
        default=True,
        description="Development user flag"
    )
    roles: List[str] = Field(
        default=["user"],
        description="User roles for access control (e.g., ['user'], ['user', 'admin'])"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "username": "john.doe",
                "email": "john.doe@faultmaven.local",
                "display_name": "John Doe",
                "created_at": "2025-01-15T10:00:00Z",
                "is_dev_user": True,
                "roles": ["user", "admin"]
            }
        }
    )


class AuthTokenResponse(BaseModel):
    """Authentication token response

    Standard OAuth2-compatible token response format.
    Includes token, expiration, user information, and session ID.
    """
    access_token: str = Field(
        ...,
        description="Bearer access token"
    )
    token_type: str = Field(
        default="bearer",
        description="Token type (always 'bearer')"
    )
    expires_in: int = Field(
        ...,
        description="Token expiration time in seconds"
    )
    session_id: str = Field(
        ...,
        description="Session identifier for multi-turn conversations"
    )
    user: UserProfile = Field(
        ...,
        description="Authenticated user profile"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": "550e8400-e29b-41d4-a716-446655440000",
                "token_type": "bearer",
                "expires_in": 86400,
                "session_id": "session-550e8400-e29b-41d4-a716-446655440000",
                "user": {
                    "user_id": "550e8400-e29b-41d4-a716-446655440000",
                    "username": "john.doe",
                    "email": "john.doe@faultmaven.local",
                    "display_name": "John Doe",
                    "created_at": "2025-01-15T10:00:00Z",
                    "is_dev_user": True,
                    "roles": ["user", "admin"]
                }
            }
        }
    )


class LogoutResponse(BaseModel):
    """Logout response model"""
    message: str = Field(
        default="Logged out successfully",
        description="Logout confirmation message"
    )
    revoked_tokens: int = Field(
        ...,
        description="Number of tokens that were revoked"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "Logged out successfully",
                "revoked_tokens": 1
            }
        }
    )


class AuthError(BaseModel):
    """Authentication error response

    Structured error information for authentication failures.
    Follows RFC 6749 OAuth2 error response format.
    """
    error: str = Field(
        ...,
        description="Error code"
    )
    error_description: str = Field(
        ...,
        description="Human-readable error description"
    )
    correlation_id: Optional[str] = Field(
        None,
        description="Request correlation ID for debugging"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "invalid_request",
                "error_description": "Username is required and must be between 3-50 characters",
                "correlation_id": "550e8400-e29b-41d4-a716-446655440000"
            }
        }
    )


class TokenValidationError(AuthError):
    """Token validation error response"""
    def __init__(self, description: str, correlation_id: str = None):
        super().__init__(
            error="invalid_token",
            error_description=description,
            correlation_id=correlation_id
        )


class AuthenticationRequiredError(AuthError):
    """Authentication required error response"""
    def __init__(self, correlation_id: str = None):
        super().__init__(
            error="authentication_required",
            error_description="Authentication is required to access this resource",
            correlation_id=correlation_id
        )


# Response model for user info endpoint
class UserInfoResponse(UserProfile):
    """Extended user information response

    Includes additional metadata for the current user.
    """
    last_login: Optional[str] = Field(
        None,
        description="Last login timestamp (ISO format)"
    )
    token_count: int = Field(
        default=0,
        description="Number of active tokens for this user"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "username": "john.doe",
                "email": "john.doe@faultmaven.local",
                "display_name": "John Doe",
                "created_at": "2025-01-15T10:00:00Z",
                "is_dev_user": True,
                "roles": ["user", "admin"],
                "last_login": "2025-01-15T14:30:00Z",
                "token_count": 2
            }
        }
    )