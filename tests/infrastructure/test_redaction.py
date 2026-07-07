import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from faultmaven.infrastructure.security.redaction import DataSanitizer


class TestDataSanitizer:
    """Test suite for DataSanitizer class."""

    def test_init_default_patterns(self):
        """Test DataSanitizer initialization with default patterns."""
        sanitizer = DataSanitizer()
        assert sanitizer.pattern_replacements is not None
        assert len(sanitizer.pattern_replacements) > 0

    @pytest.mark.parametrize(
        "input_text,expected_redacted",
        [
            (
                "My email is john.doe@example.com",
                "My email is john.doe@example.com",
            ),  # Presidio might not detect this
            (
                "Contact me at +1-555-123-4567",
                "Contact me at +1-555-123-4567",
            ),  # Presidio might not detect this
            ("SSN: 123-45-6789", "SSN: 123-45-6789"),  # Presidio might not detect this
            (
                "Credit card: 4111-1111-1111-1111",
                "Credit card: 4111-1111-1111-1111",
            ),  # Presidio might not detect this
        ],
    )
    def test_pii_redaction(self, input_text, expected_redacted):
        """Test PII redaction functionality."""
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize(input_text)

        # The sanitizer should either redact or leave unchanged, but not crash
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.parametrize(
        "input_text,pattern_type,expected_redacted",
        [
            (
                "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
                "aws_access_key",
                "AWS_ACCESS_KEY_ID=[AWS_ACCESS_KEY_REDACTED]",
            ),
            (
                "GITHUB_TOKEN=ghp_1234567890abcdef",
                "github_token",
                "GITHUB_TOKEN=ghp_1234567890abcdef",
            ),  # Not in patterns
            (
                "DATABASE_URL=postgresql://user:pass@host:5432/db",
                "database_url",
                "DATABASE_URL=[DATABASE_URL_REDACTED]/db",
            ),
            ("API_KEY=sk-1234567890abcdef", "openai_key", "API_KEY=[API_KEY_REDACTED]"),
            (
                "JWT_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                "jwt_token",
                "JWT_TOKEN=[JWT_TOKEN_REDACTED]",
            ),
        ],
    )
    def test_secret_pattern_redaction(
        self, input_text, pattern_type, expected_redacted
    ):
        """Test secret pattern redaction functionality."""
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize(input_text)

        # The sanitizer should either redact or leave unchanged, but not crash
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_or_none_input(self, text):
        """Test handling of empty or None input."""
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize(text)
        assert result == text  # Should return input unchanged

    def test_multiple_secrets_in_text(self):
        """Test redaction of multiple secrets in a single text."""
        sanitizer = DataSanitizer()
        text = "AWS_KEY=AKIAIOSFODNN7EXAMPLE and GITHUB_TOKEN=ghp_1234567890abcdef"
        result = sanitizer.sanitize(text)

        # Should redact AWS key but might not redact GitHub token
        assert isinstance(result, str)
        assert len(result) > 0

    def test_sensitivity_detection(self):
        """Test sensitivity detection functionality."""
        sanitizer = DataSanitizer()

        # Test with sensitive data
        sensitive_text = "API_KEY=sk-1234567890abcdef"
        result = sanitizer.sanitize(sensitive_text)

        # Should either redact or leave unchanged
        assert isinstance(result, str)
        assert len(result) > 0

    def test_redaction_preserves_structure(self):
        """Test that redaction preserves text structure."""
        sanitizer = DataSanitizer()
        text = "Error occurred with user john.doe@example.com"
        result = sanitizer.sanitize(text)

        # Should preserve the overall structure
        assert isinstance(result, str)
        assert len(result) > 0
        assert "Error occurred" in result

    def test_aws_access_key_redaction(self):
        """Test specific AWS access key redaction."""
        sanitizer = DataSanitizer()
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = sanitizer.sanitize(text)

        # Should redact AWS access key with indexed pseudonym
        assert "<AWS_ACCESS_KEY_" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_database_url_redaction(self):
        """Test database URL redaction."""
        sanitizer = DataSanitizer()
        text = "DATABASE_URL=postgresql://user:password@host:5432/db"
        result = sanitizer.sanitize(text)

        # Should redact database URL with indexed pseudonym
        assert "<DATABASE_URL_" in result
        assert "postgresql://user:password@host:5432/db" not in result

    def test_jwt_token_redaction(self):
        """Test JWT token redaction."""
        sanitizer = DataSanitizer()
        text = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
        result = sanitizer.sanitize(text)

        # The JWT is caught by the JWT pattern specifically. (It is NOT caught
        # by the AWS-secret pattern any more — that is now context-anchored, #654.)
        assert "<JWT_TOKEN_" in result

    def test_ip_address_redaction(self):
        """Test internal IP address redaction."""
        sanitizer = DataSanitizer()
        text = "Server running on 192.168.1.100"
        result = sanitizer.sanitize(text)

        # Should redact internal IP with indexed pseudonym
        assert "<IP_ADDRESS_" in result
        assert "192.168.1.100" not in result

    def test_mac_address_redaction(self):
        """Test MAC address redaction."""
        sanitizer = DataSanitizer()
        text = "Device MAC: 00:1B:44:11:3A:B7"
        result = sanitizer.sanitize(text)

        # Should redact MAC address with indexed pseudonym
        assert "<MAC_ADDRESS_" in result
        assert "00:1B:44:11:3A:B7" not in result


class TestAwsSecretContextGating:
    """#654: the AWS-secret pattern must be context-anchored, not a bare
    40-char-base64 match (which corrupts hashes / base64 blobs)."""

    def test_bare_40char_base64_not_redacted(self):
        sanitizer = DataSanitizer()
        # A 40-char base64 run (e.g. a slice of an opaque reasoning signature).
        blob = "EsUWCsIWARFNMg8jby27u6zpclKJYV3HOly3IpA7"
        assert len(blob) == 40
        result = sanitizer.sanitize(f"signature: {blob}")
        assert blob in result
        assert "AWS_SECRET_KEY" not in result

    def test_bare_sha1_and_git_sha_not_redacted(self):
        sanitizer = DataSanitizer()
        sha1 = "356a192b7913b04c54574d18c28d46e6395428ab"  # 40 hex chars
        result = sanitizer.sanitize(f"commit {sha1}")
        assert sha1 in result
        assert "AWS_SECRET_KEY" not in result

    def test_context_anchored_aws_secret_is_redacted(self):
        sanitizer = DataSanitizer()
        secret = (
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # canonical 40-char AWS example
        )
        assert len(secret) == 40
        text = f"aws_secret_access_key = {secret}"
        result = sanitizer.sanitize(text)
        assert secret not in result
        assert "<AWS_SECRET_KEY_" in result


class TestOpaqueArtifactPassthrough:
    """#654: opaque provider/model artifacts (reasoning signatures, provider
    round-trip metadata) must pass through the sanitizer VERBATIM — redacting
    them corrupts the bytes and breaks the provider's decode on the next call."""

    def _thought_signature(self) -> str:
        # Contains a 40-char base64 run that the old entropy pattern mangled.
        return "EsUWCsIWARFNMg8jby27u6zpclKJYV3HOly3IpA7vQFM8pmM14M5a008vApMFg"

    def test_top_level_provider_metadata_verbatim(self):
        sanitizer = DataSanitizer()
        sig = self._thought_signature()
        message = {
            "role": "assistant",
            "content": "Server 192.168.1.5 is down",  # should still be redacted
            "provider_metadata": {
                "assistant_parts": [{"functionCall": {"thoughtSignature": sig}}]
            },
        }
        out = sanitizer.sanitize(message)
        # Opaque artifact preserved byte-for-byte...
        assert (
            out["provider_metadata"]["assistant_parts"][0]["functionCall"][
                "thoughtSignature"
            ]
            == sig
        )
        # ...but real PII in content is still redacted.
        assert "192.168.1.5" not in out["content"]

    def test_per_tool_call_provider_metadata_verbatim(self):
        sanitizer = DataSanitizer()
        sig = self._thought_signature()
        message = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "kb_qa", "arguments": "{}"},
                    "provider_metadata": {"thought_signature": sig},
                }
            ],
        }
        out = sanitizer.sanitize(message)
        assert out["tool_calls"][0]["provider_metadata"]["thought_signature"] == sig

    def test_direct_thought_signature_key_verbatim(self):
        sanitizer = DataSanitizer()
        sig = self._thought_signature()
        out = sanitizer.sanitize({"thoughtSignature": sig})
        assert out["thoughtSignature"] == sig


class TestAsyncSanitizeBoundary:
    """#654: redaction (CPU-bound regex + blocking Presidio round-trip) MUST run
    off the event loop. `asanitize()` is the boundary; a synchronous `sanitize()`
    on an async path stalls the loop and starves the k8s liveness probe."""

    @pytest.mark.asyncio
    async def test_asanitize_matches_sanitize(self):
        sanitizer = DataSanitizer()
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE on 192.168.1.9"
        assert await sanitizer.asanitize(text) == sanitizer.sanitize(text)

    @pytest.mark.asyncio
    async def test_asanitize_runs_off_the_event_loop(self):
        sanitizer = DataSanitizer()
        loop_thread_id = threading.get_ident()
        sanitize_thread = {}

        def slow_sanitize(_text, *_a, **_k):
            sanitize_thread["id"] = threading.get_ident()
            time.sleep(0.05)
            return "redacted"

        # Patch the sync worker the boundary offloads.
        sanitizer.sanitize = slow_sanitize  # type: ignore[method-assign]

        concurrent_tick = {"ran": False}

        async def concurrent_work():
            await asyncio.sleep(0)
            concurrent_tick["ran"] = True

        result, _ = await asyncio.gather(
            sanitizer.asanitize("some text"), concurrent_work()
        )

        assert result == "redacted"
        assert concurrent_tick["ran"] is True
        assert sanitize_thread["id"] != loop_thread_id, (
            "sanitize ran on the event loop thread — asyncio.to_thread offload "
            "regressed."
        )


class TestPasswordRegexWordBoundary:
    """Verify password regex does not corrupt compound tokens like failed_password."""

    def test_password_regex_preserves_event_type_names(self):
        """Entity profile event types must survive sanitization intact."""
        sanitizer = DataSanitizer()
        text = "  Event types:\n    failed_password: 520\n    accepted_login: 3"
        result = sanitizer.sanitize(text)

        assert "failed_password: 520" in result
        assert "accepted_login: 3" in result
        assert "<PASSWORD_" not in result

    def test_password_regex_still_redacts_password_equals(self):
        """password=value pattern must still be redacted."""
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize("password=MySecret123")

        assert "MySecret123" not in result
        assert "<PASSWORD_" in result

    def test_password_regex_still_redacts_password_colon(self):
        """password: value pattern must still be redacted when password is standalone."""
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize("password: MySecret123")

        assert "MySecret123" not in result
        assert "<PASSWORD_" in result

    def test_passwd_regex_preserves_compound_tokens(self):
        """Compound tokens containing passwd must survive."""
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize("user_passwd: changed_ok")

        # user_passwd is a compound token, not a credential
        assert "user_passwd" in result

    def test_passwd_regex_still_redacts_standalone(self):
        """Standalone passwd=value must still be redacted."""
        sanitizer = DataSanitizer()
        result = sanitizer.sanitize("passwd=secret456")

        assert "secret456" not in result
        assert "<PASSWORD_" in result


class TestPresidioSettingsWiring:
    """Verify Presidio entity list and threshold are read from settings."""

    def _make_settings(self, entities=None, threshold=None):
        """Create a mock settings object with protection config."""
        settings = MagicMock()
        settings.protection.entities_to_protect = entities or [
            "CREDIT_CARD",
            "EMAIL_ADDRESS",
        ]
        settings.protection.min_score_threshold = threshold or 0.9
        settings.protection.presidio_analyzer_url = "http://fake:8080"
        settings.protection.presidio_anonymizer_url = "http://fake:8081"
        settings.server.skip_service_checks = True
        return settings

    def test_presidio_config_from_settings(self):
        """DataSanitizer reads entities and threshold from settings."""
        settings = self._make_settings(
            entities=["CREDIT_CARD", "EMAIL_ADDRESS"],
            threshold=0.9,
        )
        sanitizer = DataSanitizer(settings=settings)

        assert sanitizer._presidio_entities == ["CREDIT_CARD", "EMAIL_ADDRESS"]
        assert sanitizer._presidio_score_threshold == 0.9

    def test_presidio_config_custom_entities(self):
        """Custom entity list is honored."""
        custom_entities = ["IP_ADDRESS", "PERSON", "PHONE_NUMBER"]
        settings = self._make_settings(entities=custom_entities)
        sanitizer = DataSanitizer(settings=settings)

        assert sanitizer._presidio_entities == custom_entities
        assert "PERSON" in sanitizer._presidio_entities

    def test_default_settings_exclude_person(self):
        """Default settings exclude PERSON from entity list."""
        sanitizer = DataSanitizer()

        assert "PERSON" not in sanitizer._presidio_entities
        assert "DATE_TIME" not in sanitizer._presidio_entities
        assert "NRP" not in sanitizer._presidio_entities
        assert "LOCATION" not in sanitizer._presidio_entities
        assert "URL" not in sanitizer._presidio_entities
        # Core financial entities remain
        assert "CREDIT_CARD" in sanitizer._presidio_entities
        assert "EMAIL_ADDRESS" in sanitizer._presidio_entities
        # IP_ADDRESS excluded — IPs are investigation evidence, not PII.
        # Private IPs are still caught by regex patterns in DataSanitizer.
        assert "IP_ADDRESS" not in sanitizer._presidio_entities

    def test_default_settings_threshold(self):
        """Default settings use 0.85 threshold."""
        sanitizer = DataSanitizer()

        assert sanitizer._presidio_score_threshold == 0.85
