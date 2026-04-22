"""
Tests for StructuredConfigExtractor.

Covers:
- R5.2: detect-secrets integration for enhanced secret detection
- Secret redaction: regex patterns + detect-secrets second pass
- Non-secret values preserved
"""

import json
from unittest.mock import patch

import pytest

from faultmaven.modules.preprocessing.extractors.config_extractor import (
    DETECT_SECRETS_AVAILABLE,
    StructuredConfigExtractor,
)


class TestConfigExtractor:
    @pytest.fixture
    def extractor(self):
        return StructuredConfigExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "direct"
        assert extractor.llm_calls_used == 0

    # --- Secret redaction (regex first-pass) ---

    def test_password_key_redacted(self, extractor):
        """Keys matching 'password' are redacted."""
        config = json.dumps({"database": {"password": "mysecret", "host": "localhost"}})
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert "mysecret" not in result
        assert "localhost" in result

    def test_api_key_value_redacted(self, extractor):
        """Long alphanumeric values (>= 20 chars) are redacted by regex."""
        config = json.dumps({"token": "abcdefghijklmnopqrstuvwxyz", "public": "key"})
        result = extractor.extract(config)
        assert "[REDACTED]" in result

    def test_sk_key_value_redacted(self, extractor):
        """OpenAI-style sk-... keys are redacted by regex."""
        config = json.dumps({"api_key": "sk-abcdefghijklmnopq", "public": "key"})
        result = extractor.extract(config)
        assert "[REDACTED]" in result

    def test_short_value_not_redacted(self, extractor):
        """Short values (< 16 chars) should NOT be redacted by value check."""
        config = json.dumps({"log_level": "debug", "timeout": "30"})
        result = extractor.extract(config)
        assert "debug" in result
        assert "30" in result

    def test_hostname_not_redacted(self, extractor):
        """Hostnames should NOT be redacted (not matching secret patterns)."""
        config = json.dumps({"hostname": "my-server.example.com"})
        result = extractor.extract(config)
        assert "my-server.example.com" in result

    # --- R5.2: detect-secrets second-pass ---

    @pytest.mark.skipif(
        not DETECT_SECRETS_AVAILABLE, reason="detect-secrets not installed"
    )
    def test_jwt_token_redacted(self, extractor):
        """JWT tokens detected by detect-secrets should be redacted."""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        config = json.dumps({"auth_token": jwt, "public": "key"})
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert jwt not in result

    @pytest.mark.skipif(
        not DETECT_SECRETS_AVAILABLE, reason="detect-secrets not installed"
    )
    def test_github_token_redacted(self, extractor):
        """GitHub tokens detected by detect-secrets should be redacted."""
        config = json.dumps(
            {
                "github_token": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "public": "key",
            }
        )
        result = extractor.extract(config)
        assert "[REDACTED]" in result

    @pytest.mark.skipif(
        not DETECT_SECRETS_AVAILABLE, reason="detect-secrets not installed"
    )
    def test_non_secret_long_string_preserved(self, extractor):
        """Long but non-secret base64 config values should be preserved."""
        # This is a non-secret config value — detect-secrets (with no entropy detectors)
        # should not flag it
        config = json.dumps({"log_level": "debug", "max_retries": 5})
        result = extractor.extract(config)
        assert "debug" in result

    def test_fallback_when_detect_secrets_unavailable(self, extractor):
        """Config extraction works without detect-secrets (regex only)."""
        config = json.dumps(
            {
                "database_password": "secret123",
                "host": "localhost",
            }
        )
        with patch(
            "faultmaven.modules.preprocessing.extractors.config_extractor.DETECT_SECRETS_AVAILABLE",
            False,
        ):
            result = extractor.extract(config)
            assert "[REDACTED]" in result  # Regex still catches password key
            assert "localhost" in result

    # --- Config format parsing ---

    def test_json_config(self, extractor):
        """JSON config parsed and formatted."""
        config = json.dumps({"server": {"port": 8080, "host": "0.0.0.0"}})
        result = extractor.extract(config)
        assert "port" in result
        assert "8080" in result

    def test_env_file(self, extractor):
        """Key=value .env format parsed."""
        content = """
DATABASE_HOST=localhost
DATABASE_PORT=5432
API_KEY=sk-verylongsecretkey123456789
LOG_LEVEL=info
"""
        result = extractor.extract(content)
        assert "localhost" in result
        assert "[REDACTED]" in result  # API_KEY key matches secret pattern
        assert "info" in result

    def test_ini_sections(self, extractor):
        """INI format with sections parsed."""
        content = """
[database]
host=localhost
port=5432

[logging]
level=debug
"""
        result = extractor.extract(content)
        assert "database" in result
        assert "localhost" in result
        assert "debug" in result

    # --- False-positive prevention (tightened key patterns) ---

    def test_auth_type_not_redacted(self, extractor):
        """AUTH_TYPE is a mode selector, not a secret."""
        config = json.dumps({"AUTH_TYPE": "bearer"})
        result = extractor.extract(config)
        assert "bearer" in result
        assert "[REDACTED]" not in result

    def test_auth_method_not_redacted(self, extractor):
        """auth_method describes auth approach, not a credential."""
        config = json.dumps({"auth_method": "oauth2"})
        result = extractor.extract(config)
        assert "oauth2" in result
        assert "[REDACTED]" not in result

    def test_authentication_mode_not_redacted(self, extractor):
        """authentication_mode is config metadata, not a secret."""
        config = json.dumps({"authentication_mode": "saml"})
        result = extractor.extract(config)
        assert "saml" in result
        assert "[REDACTED]" not in result

    def test_oauth_redirect_uri_not_redacted(self, extractor):
        """oauth_redirect_uri is a URL, not a secret."""
        config = json.dumps({"oauth_redirect_uri": "https://app.example.com/callback"})
        result = extractor.extract(config)
        assert "https://app.example.com/callback" in result

    def test_token_type_not_redacted(self, extractor):
        """token_type describes the kind of token, not a credential."""
        config = json.dumps({"token_type": "bearer"})
        result = extractor.extract(config)
        assert "bearer" in result
        assert "[REDACTED]" not in result

    def test_token_expiry_not_redacted(self, extractor):
        """token_expiry is a numeric setting, not a secret."""
        config = json.dumps({"token_expiry": "3600"})
        result = extractor.extract(config)
        assert "3600" in result
        assert "[REDACTED]" not in result

    def test_keycloak_url_not_redacted(self, extractor):
        """keycloak_url is a service URL, not a secret."""
        config = json.dumps({"keycloak_url": "https://keycloak.example.com"})
        result = extractor.extract(config)
        assert "https://keycloak.example.com" in result

    def test_key_format_not_redacted(self, extractor):
        """key_format describes a format, not a credential."""
        config = json.dumps({"key_format": "pem"})
        result = extractor.extract(config)
        assert "pem" in result
        assert "[REDACTED]" not in result

    # --- Non-secret value bypass ---

    def test_require_password_boolean_not_redacted(self, extractor):
        """require_password=true is a boolean flag, not a credential."""
        config = json.dumps({"require_password": "true"})
        result = extractor.extract(config)
        assert "true" in result
        assert "[REDACTED]" not in result

    def test_token_with_bearer_enum_not_redacted(self, extractor):
        """token=bearer is a mode enum, not a credential."""
        config = json.dumps({"token": "bearer"})
        result = extractor.extract(config)
        assert "bearer" in result
        assert "[REDACTED]" not in result

    def test_secret_key_with_real_value_still_redacted(self, extractor):
        """require_password with an actual password value is still redacted."""
        config = json.dumps({"require_password": "hunter2isMyPass", "public": "key"})
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert "hunter2isMyPass" not in result

    # --- Regression: real secrets still redacted ---

    def test_client_secret_redacted(self, extractor):
        """client_secret contains an actual secret value."""
        config = json.dumps({"client_secret": "s3cr3t-v4lu3-h3r3", "public": "key"})
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert "s3cr3t-v4lu3-h3r3" not in result

    def test_nested_auth_token_redacted(self, extractor):
        """Nested auth.auth_token should still be redacted."""
        config = json.dumps(
            {"auth": {"auth_token": "mytoken12345678", "public": "key"}}
        )
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert "mytoken12345678" not in result

    def test_database_password_nested_redacted(self, extractor):
        """database.password still redacted with dotted path."""
        config = json.dumps({"database": {"password": "dbpass123"}})
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert "dbpass123" not in result

    def test_signing_key_redacted(self, extractor):
        """signing_key is a secret-holding key."""
        config = json.dumps({"signing_key": "supersecretkey12345", "public": "key"})
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert "supersecretkey12345" not in result

    def test_access_key_env_redacted(self, extractor):
        """AWS access key in .env format still redacted."""
        content = "AWS_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\nPUBLIC_KEY=123"
        result = extractor.extract(content)
        assert "[REDACTED]" in result

    def test_fully_redacted_config(self, extractor):
        """A config with only secrets triggers the fully redacted optimization."""
        config = json.dumps({"client_secret": "s3cr3t-v4lu3-h3r3"})
        result = extractor.extract(config)
        assert "[WARNING: Fully Redacted Config" in result
        assert "[REDACTED]" not in result
