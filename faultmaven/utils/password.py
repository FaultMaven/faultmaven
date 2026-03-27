"""Password Utilities (TASK-018)

Purpose: Provide secure password hashing and validation functions.

This module provides:
- Password hashing using bcrypt with cost factor 12
- Password verification against bcrypt hashes
- Password strength validation

Design Reference: TASK-018 User Management Service
Security Notes:
- Uses bcrypt for adaptive hashing
- Cost factor 12 provides good balance of security and performance
- Passwords are encoded to UTF-8 before hashing
"""

import re

import bcrypt

from faultmaven.exceptions import ValidationException

# bcrypt cost factor (2^12 = 4096 iterations)
BCRYPT_COST_FACTOR = 12

# Password requirements
MIN_PASSWORD_LENGTH = 8
SPECIAL_CHARACTERS = r"!@#$%^&*()_+\-=\[\]{}|;:,.<>?"


def hash_password(plain_password: str) -> str:
    """Hash password using bcrypt.

    Uses bcrypt with a cost factor of 12, which provides a good
    balance of security and performance. The resulting hash includes
    the salt and can be used directly for verification.

    Note: bcrypt has a 72-byte limit. Passwords longer than 72 bytes
    (after UTF-8 encoding) are truncated. This is standard bcrypt behavior.

    Args:
        plain_password: Plain text password to hash

    Returns:
        Bcrypt hash string (includes salt, cost factor, and hash)

    Example:
        >>> hashed = hash_password("MySecureP@ssw0rd!")
        >>> hashed.startswith("$2b$12$")
        True
    """
    # bcrypt has a 72-byte limit - truncate to prevent ValueError
    password_bytes = plain_password.encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=BCRYPT_COST_FACTOR)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against bcrypt hash.

    Performs constant-time comparison to prevent timing attacks.

    Note: bcrypt has a 72-byte limit. Passwords longer than 72 bytes
    (after UTF-8 encoding) are truncated before verification.

    Args:
        plain_password: Plain text password to verify
        hashed_password: Bcrypt hash to verify against

    Returns:
        True if password matches, False otherwise

    Example:
        >>> hashed = hash_password("MySecureP@ssw0rd!")
        >>> verify_password("MySecureP@ssw0rd!", hashed)
        True
        >>> verify_password("wrong_password", hashed)
        False
    """
    try:
        # bcrypt has a 72-byte limit - truncate to match hash_password behavior
        password_bytes = plain_password.encode("utf-8")[:72]
        return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        # Invalid hash format or encoding error
        return False


def validate_password_strength(password: str) -> None:
    """Validate password meets strength requirements.

    Requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character (!@#$%^&*()_+-=[]{}|;:,.<>?)

    Args:
        password: Plain text password to validate

    Raises:
        ValidationException: Password does not meet requirements
            The exception message describes which requirement was not met.

    Example:
        >>> validate_password_strength("MySecureP@ssw0rd!")  # Valid
        >>> validate_password_strength("weak")  # Raises ValidationException
    """
    errors = get_password_validation_errors(password)
    if errors:
        raise ValidationException(errors[0])


def get_password_validation_errors(password: str) -> list[str]:
    """Get list of password validation errors.

    Useful when you want to show all validation errors at once
    rather than one at a time.

    Args:
        password: Plain text password to validate

    Returns:
        List of validation error messages (empty if password is valid)

    Example:
        >>> errors = get_password_validation_errors("weak")
        >>> len(errors) > 0
        True
    """
    errors: list[str] = []

    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )

    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter")

    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter")

    if not re.search(r"[0-9]", password):
        errors.append("Password must contain at least one digit")

    if not re.search(rf"[{re.escape(SPECIAL_CHARACTERS)}]", password):
        errors.append("Password must contain at least one special character")

    return errors


def is_password_strong(password: str) -> bool:
    """Check if password meets strength requirements.

    Convenience function that returns a boolean instead of raising.

    Args:
        password: Plain text password to check

    Returns:
        True if password meets all requirements, False otherwise
    """
    return len(get_password_validation_errors(password)) == 0
