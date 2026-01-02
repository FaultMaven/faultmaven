"""
Settings Mapping Tests

Verifies that environment variables are correctly mapped to typed settings fields.
This is part of PR 2 "Settings becomes the single source of truth".

These tests ensure that:
1. Environment variables are correctly read by the settings system
2. Settings fields have correct types and default values
3. The settings system properly validates and transforms values
"""

import os
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def reset_settings():
    """Reset settings singleton before each test."""
    from faultmaven.config.settings import reset_settings
    reset_settings()
    yield
    reset_settings()


class TestSessionSettingsMapping:
    """Tests for session timeout configuration mapping."""

    def test_session_timeout_defaults(self):
        """Test that session timeout settings have correct defaults."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        assert settings.session.min_timeout_minutes == 60
        assert settings.session.max_timeout_minutes == 480
        assert settings.session.default_timeout_minutes == 180

    def test_session_timeout_from_env(self):
        """Test that session timeout settings are read from environment."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "SESSION_MIN_TIMEOUT_MINUTES": "30",
            "SESSION_MAX_TIMEOUT_MINUTES": "720",
            "SESSION_DEFAULT_TIMEOUT_MINUTES": "240",
        }):
            reset_settings()
            settings = get_settings()

            assert settings.session.min_timeout_minutes == 30
            assert settings.session.max_timeout_minutes == 720
            assert settings.session.default_timeout_minutes == 240


class TestCaseSettingsMapping:
    """Tests for case configuration mapping."""

    def test_case_title_fallback_default(self):
        """Test that case title fallback defaults to True."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        assert settings.case.title_generation_use_fallback is True

    def test_case_title_fallback_from_env(self):
        """Test that case title fallback is read from environment."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "TITLE_GENERATION_USE_FALLBACK": "false",
        }):
            reset_settings()
            settings = get_settings()

            assert settings.case.title_generation_use_fallback is False


class TestLoggingSettingsMapping:
    """Tests for logging configuration mapping."""

    def test_logging_defaults(self):
        """Test that logging settings have correct defaults."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        # Check defaults
        assert settings.logging.log_output_format == "json"
        assert settings.logging.log_dedupe is True
        assert settings.logging.log_buffer_size == 100
        assert settings.logging.log_flush_interval == 5.0
        assert settings.logging.log_human_readable is False

    def test_logging_from_env(self):
        """Test that logging settings are read from environment."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "LOG_OUTPUT_FORMAT": "console",
            "LOG_DEDUPE": "false",
            "LOG_BUFFER_SIZE": "200",
            "LOG_FLUSH_INTERVAL": "10.0",
            "LOG_HUMAN_READABLE": "true",
        }):
            reset_settings()
            settings = get_settings()

            assert settings.logging.log_output_format == "console"
            assert settings.logging.log_dedupe is False
            assert settings.logging.log_buffer_size == 200
            assert settings.logging.log_flush_interval == 10.0
            assert settings.logging.log_human_readable is True


class TestDatabaseSettingsMapping:
    """Tests for database configuration mapping."""

    def test_database_defaults(self):
        """Test that database settings have correct defaults."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        assert settings.database.database_url == "sqlite+aiosqlite:///./faultmaven.db"
        assert settings.database.database_echo is False
        assert settings.database.database_pool_size == 5
        assert settings.database.database_max_overflow == 10
        assert settings.database.database_pool_timeout == 30
        assert settings.database.database_pool_recycle == 1800

    def test_database_from_env(self):
        """Test that database settings are read from environment."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost/testdb",
            "DATABASE_ECHO": "true",
            "DATABASE_POOL_SIZE": "10",
            "DATABASE_MAX_OVERFLOW": "20",
            "DATABASE_POOL_TIMEOUT": "60",
            "DATABASE_POOL_RECYCLE": "3600",
        }):
            reset_settings()
            settings = get_settings()

            assert settings.database.database_url == "postgresql+asyncpg://user:pass@localhost/testdb"
            assert settings.database.database_echo is True
            assert settings.database.database_pool_size == 10
            assert settings.database.database_max_overflow == 20
            assert settings.database.database_pool_timeout == 60
            assert settings.database.database_pool_recycle == 3600


class TestRedisSettingsMapping:
    """Tests for Redis configuration mapping."""

    def test_redis_defaults(self):
        """Test that Redis settings have correct defaults."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        assert settings.database.redis_host == "192.168.0.111"
        assert settings.database.redis_port == 30379
        assert settings.database.redis_db == 0

    def test_redis_from_env(self):
        """Test that Redis settings are read from environment."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "REDIS_HOST": "redis.example.com",
            "REDIS_PORT": "6379",
            "REDIS_DB": "1",
            "REDIS_PASSWORD": "secret123",
        }):
            reset_settings()
            settings = get_settings()

            assert settings.database.redis_host == "redis.example.com"
            assert settings.database.redis_port == 6379
            assert settings.database.redis_db == 1
            assert settings.database.redis_password.get_secret_value() == "secret123"


class TestChromaDBSettingsMapping:
    """Tests for ChromaDB configuration mapping."""

    def test_chromadb_defaults(self):
        """Test that ChromaDB settings have correct defaults."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        assert settings.database.chromadb_host == "chromadb.faultmaven.local"
        assert settings.database.chromadb_port == 30080
        assert settings.database.chromadb_persist_dir == "./chroma_db"

    def test_chromadb_from_env(self):
        """Test that ChromaDB settings are read from environment."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "CHROMADB_HOST": "chromadb.example.com",
            "CHROMADB_PORT": "8000",
            "CHROMADB_PERSIST_DIR": "/data/chroma",
            "CHROMADB_AUTH_TOKEN": "token123",
        }):
            reset_settings()
            settings = get_settings()

            assert settings.database.chromadb_host == "chromadb.example.com"
            assert settings.database.chromadb_port == 8000
            assert settings.database.chromadb_persist_dir == "/data/chroma"
            assert settings.database.chromadb_auth_token.get_secret_value() == "token123"


class TestLLMSettingsMapping:
    """Tests for LLM provider configuration mapping."""

    def test_llm_defaults(self):
        """Test that LLM settings have correct defaults."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        assert settings.llm.request_timeout == 30
        assert settings.llm.max_retries == 3
        assert settings.llm.strict_provider_mode is False

    def test_llm_from_env(self):
        """Test that LLM settings are read from environment."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "LLM_REQUEST_TIMEOUT": "60",
            "LLM_MAX_RETRIES": "5",
            "STRICT_PROVIDER_MODE": "true",
        }):
            reset_settings()
            settings = get_settings()

            assert settings.llm.request_timeout == 60
            assert settings.llm.max_retries == 5
            assert settings.llm.strict_provider_mode is True


class TestSettingsSingleton:
    """Tests for settings singleton behavior."""

    def test_get_settings_returns_same_instance(self):
        """Test that get_settings returns the same instance."""
        from faultmaven.config.settings import get_settings

        settings1 = get_settings()
        settings2 = get_settings()

        assert settings1 is settings2

    def test_reset_settings_clears_singleton(self):
        """Test that reset_settings clears the singleton."""
        from faultmaven.config.settings import get_settings, reset_settings

        settings1 = get_settings()
        reset_settings()
        settings2 = get_settings()

        # After reset, should be a new instance
        assert settings1 is not settings2


class TestSettingsIntegration:
    """Integration tests for settings with actual infrastructure components."""

    def test_logging_config_uses_settings(self):
        """Test that LoggingConfig reads from settings."""
        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        with patch.dict(os.environ, {
            "LOG_LEVEL": "DEBUG",
            "LOG_OUTPUT_FORMAT": "console",
            "LOG_DEDUPE": "false",
        }):
            reset_settings()

            # Import after setting env vars
            from faultmaven.infrastructure.logging.config import LoggingConfig

            config = LoggingConfig()

            assert config.LOG_LEVEL == "DEBUG"
            assert config.LOG_FORMAT == "console"
            assert config.LOG_DEDUPE is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
