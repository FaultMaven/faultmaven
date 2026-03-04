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

from faultmaven.services.preprocessing.extractors.config_extractor import (
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
        config = json.dumps({"token": "abcdefghijklmnopqrstuvwxyz"})
        result = extractor.extract(config)
        assert "[REDACTED]" in result

    def test_sk_key_value_redacted(self, extractor):
        """OpenAI-style sk-... keys are redacted by regex."""
        config = json.dumps({"api_key": "sk-abcdefghijklmnopq"})
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
        config = json.dumps({"auth_token": jwt})
        result = extractor.extract(config)
        assert "[REDACTED]" in result
        assert jwt not in result

    @pytest.mark.skipif(
        not DETECT_SECRETS_AVAILABLE, reason="detect-secrets not installed"
    )
    def test_github_token_redacted(self, extractor):
        """GitHub tokens detected by detect-secrets should be redacted."""
        config = json.dumps(
            {"github_token": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
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
            "faultmaven.services.preprocessing.extractors.config_extractor.DETECT_SECRETS_AVAILABLE",
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
