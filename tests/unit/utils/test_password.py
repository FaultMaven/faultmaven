"""Unit tests for password utilities (TASK-018).

Tests for:
- Password hashing with bcrypt
- Password verification
- Password strength validation

Coverage target: 100%
"""

import pytest

from faultmaven.exceptions import ValidationException
from faultmaven.utils.password import (
    BCRYPT_COST_FACTOR,
    MIN_PASSWORD_LENGTH,
    get_password_validation_errors,
    hash_password,
    is_password_strong,
    validate_password_strength,
    verify_password,
)


class TestHashPassword:
    """Tests for hash_password function."""

    def test_hash_returns_bcrypt_format(self):
        """Hash should return bcrypt format string."""
        hashed = hash_password("TestP@ssw0rd!")
        # bcrypt hashes start with $2a$, $2b$, or $2y$
        assert hashed.startswith("$2")

    def test_hash_starts_with_correct_cost_factor(self):
        """Hash should include the configured cost factor."""
        hashed = hash_password("TestP@ssw0rd!")
        # Check for cost factor 12 in the hash
        assert f"$2b${BCRYPT_COST_FACTOR}$" in hashed or f"$2a${BCRYPT_COST_FACTOR}$" in hashed

    def test_same_password_produces_different_hashes(self):
        """Same password should produce different hashes due to salt."""
        password = "TestP@ssw0rd!"
        hash1 = hash_password(password)
        hash2 = hash_password(password)
        assert hash1 != hash2

    def test_hash_is_string(self):
        """Hash should be a string."""
        hashed = hash_password("TestP@ssw0rd!")
        assert isinstance(hashed, str)

    def test_hash_length_is_valid(self):
        """Bcrypt hash should be 60 characters."""
        hashed = hash_password("TestP@ssw0rd!")
        assert len(hashed) == 60

    def test_empty_password_hashes(self):
        """Empty password should still hash (validation is separate)."""
        hashed = hash_password("")
        assert hashed.startswith("$2")

    def test_unicode_password_hashes(self):
        """Unicode passwords should hash correctly."""
        hashed = hash_password("Pässwörd!123")
        assert hashed.startswith("$2")

    def test_long_password_hashes(self):
        """Long passwords should hash (bcrypt truncates to 72 bytes)."""
        long_password = "A" * 100 + "@1"
        hashed = hash_password(long_password)
        assert hashed.startswith("$2")


class TestVerifyPassword:
    """Tests for verify_password function."""

    def test_verify_correct_password_returns_true(self):
        """Correct password should verify as true."""
        password = "TestP@ssw0rd!"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True

    def test_verify_wrong_password_returns_false(self):
        """Wrong password should verify as false."""
        password = "TestP@ssw0rd!"
        hashed = hash_password(password)
        assert verify_password("WrongP@ssw0rd!", hashed) is False

    def test_verify_similar_password_returns_false(self):
        """Similar but different password should verify as false."""
        password = "TestP@ssw0rd!"
        hashed = hash_password(password)
        assert verify_password("TestP@ssw0rd", hashed) is False

    def test_verify_case_sensitive(self):
        """Password verification should be case-sensitive."""
        password = "TestP@ssw0rd!"
        hashed = hash_password(password)
        assert verify_password("testp@ssw0rd!", hashed) is False

    def test_verify_invalid_hash_returns_false(self):
        """Invalid hash should return false, not raise."""
        assert verify_password("password", "invalid_hash") is False

    def test_verify_empty_hash_returns_false(self):
        """Empty hash should return false."""
        assert verify_password("password", "") is False

    def test_verify_empty_password_with_empty_hash(self):
        """Empty password with empty hash should return false."""
        assert verify_password("", "") is False

    def test_verify_unicode_password(self):
        """Unicode password should verify correctly."""
        password = "Pässwörd!123"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True


class TestValidatePasswordStrength:
    """Tests for validate_password_strength function."""

    def test_strong_password_passes(self):
        """Strong password should pass validation."""
        validate_password_strength("TestP@ssw0rd!")
        # No exception means success

    def test_password_too_short_raises(self):
        """Password under 8 characters should raise."""
        with pytest.raises(ValidationException) as excinfo:
            validate_password_strength("Te@1abc")
        assert "at least 8 characters" in str(excinfo.value)

    def test_password_missing_uppercase_raises(self):
        """Password without uppercase should raise."""
        with pytest.raises(ValidationException) as excinfo:
            validate_password_strength("testp@ssw0rd!")
        assert "uppercase" in str(excinfo.value)

    def test_password_missing_lowercase_raises(self):
        """Password without lowercase should raise."""
        with pytest.raises(ValidationException) as excinfo:
            validate_password_strength("TESTP@SSW0RD!")
        assert "lowercase" in str(excinfo.value)

    def test_password_missing_digit_raises(self):
        """Password without digit should raise."""
        with pytest.raises(ValidationException) as excinfo:
            validate_password_strength("TestP@ssword!")
        assert "digit" in str(excinfo.value)

    def test_password_missing_special_char_raises(self):
        """Password without special character should raise."""
        with pytest.raises(ValidationException) as excinfo:
            validate_password_strength("TestPassw0rd")
        assert "special character" in str(excinfo.value)

    def test_exactly_8_chars_passes(self):
        """Password with exactly 8 characters should pass."""
        validate_password_strength("Aa1!aaaa")

    def test_various_special_chars_pass(self):
        """Various special characters should be accepted."""
        special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
        for char in special_chars:
            password = f"TestPass0{char}"
            validate_password_strength(password)

    def test_unicode_special_chars_fail(self):
        """Unicode special chars that aren't in the list should fail."""
        with pytest.raises(ValidationException):
            validate_password_strength("TestPass0€")  # Euro sign not in list


class TestGetPasswordValidationErrors:
    """Tests for get_password_validation_errors function."""

    def test_strong_password_returns_empty_list(self):
        """Strong password should return empty error list."""
        errors = get_password_validation_errors("TestP@ssw0rd!")
        assert errors == []

    def test_multiple_issues_returns_all_errors(self):
        """Password with multiple issues should return all errors."""
        errors = get_password_validation_errors("weak")
        # Should have errors for: length, uppercase, digit, special char
        assert len(errors) >= 4

    def test_single_issue_returns_one_error(self):
        """Password with single issue should return one error."""
        errors = get_password_validation_errors("testp@ssw0rd!")  # Missing uppercase
        assert len(errors) == 1
        assert "uppercase" in errors[0]

    def test_returns_list_type(self):
        """Should always return a list."""
        errors = get_password_validation_errors("any")
        assert isinstance(errors, list)


class TestIsPasswordStrong:
    """Tests for is_password_strong function."""

    def test_strong_password_returns_true(self):
        """Strong password should return true."""
        assert is_password_strong("TestP@ssw0rd!") is True

    def test_weak_password_returns_false(self):
        """Weak password should return false."""
        assert is_password_strong("weak") is False

    def test_missing_one_requirement_returns_false(self):
        """Password missing one requirement should return false."""
        assert is_password_strong("testp@ssw0rd!") is False  # No uppercase

    def test_returns_boolean(self):
        """Should return boolean, not truthy value."""
        result = is_password_strong("TestP@ssw0rd!")
        assert result is True
        assert type(result) is bool


class TestEdgeCases:
    """Edge case tests for password utilities."""

    def test_hash_with_special_chars(self):
        """Password with many special chars should hash correctly."""
        password = "Te!@#$%^&*()st1"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True

    def test_hash_with_spaces(self):
        """Password with spaces should work."""
        password = "Test Pass 123!"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True
        # This password has uppercase (T), lowercase (est, ass), digit (123), and special (!)
        # So it should pass validation
        validate_password_strength(password)  # Should not raise

    def test_bcrypt_truncation(self):
        """Passwords over 72 bytes are truncated by bcrypt."""
        # bcrypt only uses first 72 bytes
        # Two passwords that differ only after byte 72 will hash the same
        base = "A" * 72  # Exactly 72 bytes
        password1 = base + "X"  # 73 bytes, but X is truncated
        password2 = base + "Y"  # 73 bytes, but Y is truncated
        hashed1 = hash_password(password1)
        # Due to truncation, password2 should verify against hashed1
        assert verify_password(password2, hashed1) is True
        # Both passwords verify as the same (first 72 bytes are identical)
        assert verify_password(password1, hashed1) is True

    def test_min_password_length_constant(self):
        """MIN_PASSWORD_LENGTH should be 8."""
        assert MIN_PASSWORD_LENGTH == 8

    def test_bcrypt_cost_factor_constant(self):
        """BCRYPT_COST_FACTOR should be 12."""
        assert BCRYPT_COST_FACTOR == 12
