"""Unit tests for configuration manager functionality.

This module provides comprehensive testing for the ConfigurationManager
class including initialization, validation, type conversion, and
specialized configuration getters.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from typing import Dict, Any

import pytest

from faultmaven.config.configuration_manager import ConfigurationManager, ConfigSection, get_config, reset_config
from faultmaven.exceptions import ConfigurationException


@pytest.fixture
def mock_environment():
    """Mock environment variables for testing."""
    return {
        "REDIS_HOST": "test.redis.com",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": "test_password",
        "CHAT_PROVIDER": "openai",
        "OPENAI_API_KEY": "test_openai_key",
        "LOG_LEVEL": "DEBUG",
        "SESSION_TIMEOUT_MINUTES": "45",
        "PRESIDIO_ANALYZER_URL": "http://test.analyzer.com",
        "OPIK_USE_LOCAL": "true",
        "REQUEST_TIMEOUT": "60"
    }


@pytest.fixture
def sample_config_file():
    """Create a temporary configuration file for testing."""
    config_data = {
        "REDIS_HOST": "file.redis.com",
        "REDIS_DB": "2",
        "CHAT_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "file_anthropic_key",
        "LOG_FORMAT": "json",
        "SANITIZATION_ENABLED": "false"
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_data, f)
        temp_path = Path(f.name)
    
    yield temp_path
    
    # Cleanup
    if temp_path.exists():
        temp_path.unlink()


@pytest.fixture
def sample_env_file():
    """Create a temporary .env file for testing."""
    env_content = """
# Test environment file
REDIS_HOST=env.redis.com
REDIS_PORT=6380
CHAT_PROVIDER=fireworks
FIREWORKS_API_KEY=env_fireworks_key
LOG_LEVEL=WARNING
SESSION_CLEANUP_BATCH_SIZE=25
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
        f.write(env_content)
        temp_path = Path(f.name)
    
    yield temp_path
    
    # Cleanup
    if temp_path.exists():
        temp_path.unlink()


class TestConfigurationManager:
    """Test cases for ConfigurationManager class."""

    def test_initialization_with_valid_config(self, mock_environment):
        """Test configuration manager initialization with valid environment."""
        with patch.dict(os.environ, mock_environment):
            config = ConfigurationManager(validate_on_init=False)
            
            assert config.get("REDIS_HOST") == "test.redis.com"
            assert config.get_int("REDIS_PORT") == 6379
            assert config.get("CHAT_PROVIDER") == "openai"

    def test_initialization_with_invalid_config_fails(self):
        """Test invalid configuration raises appropriate exceptions."""
        invalid_env = {
            "REDIS_HOST": "",  # Empty required field
            "CHAT_PROVIDER": "invalid_provider",  # Invalid choice
            "REDIS_PORT": "not_a_number"  # Invalid type
        }
        
        with patch.dict(os.environ, invalid_env, clear=True):
            with pytest.raises(ConfigurationException):
                ConfigurationManager(validate_on_init=True)

    def test_initialization_without_validation(self):
        """Test initialization with validation disabled."""
        invalid_env = {
            "CHAT_PROVIDER": "invalid_provider"
        }
        
        with patch.dict(os.environ, invalid_env, clear=True):
            # Should not raise exception when validation disabled
            config = ConfigurationManager(validate_on_init=False)
            assert config.get("CHAT_PROVIDER") == "invalid_provider"

    def test_type_conversion_methods(self, mock_environment):
        """Test get_int(), get_bool(), get_str() type conversions."""
        with patch.dict(os.environ, mock_environment):
            config = ConfigurationManager(validate_on_init=False)
            
            # Test get_int
            assert config.get_int("REDIS_PORT") == 6379
            assert config.get_int("SESSION_TIMEOUT_MINUTES") == 45
            assert config.get_int("NONEXISTENT_INT", 100) == 100
            
            # Test get_bool  
            assert config.get_bool("OPIK_USE_LOCAL") is True
            assert config.get_bool("NONEXISTENT_BOOL", False) is False
            
            # Test get_str
            assert config.get_str("REDIS_HOST") == "test.redis.com"
            assert config.get_str("NONEXISTENT_STR", "default") == "default"

    def test_type_conversion_with_invalid_values(self):
        """Test type conversion handles invalid values gracefully."""
        invalid_env = {
            "INVALID_INT": "not_a_number",
            "INVALID_BOOL": "maybe",
            "NONE_VALUE": ""
        }
        
        with patch.dict(os.environ, invalid_env, clear=True):
            config = ConfigurationManager(validate_on_init=False)
            
            # Should return defaults for invalid conversions
            assert config.get_int("INVALID_INT", 42) == 42
            assert config.get_bool("INVALID_BOOL", True) is True  # "maybe" is not a valid bool, so returns default
            assert config.get_str("NONE_VALUE", "fallback") == ""

    def test_configuration_section_validation(self):
        """Test validation for each configuration section."""
        config = ConfigurationManager(validate_on_init=False)
        
        # Test database section validation
        database_section = config._get_database_section()
        assert "REDIS_HOST" in database_section.required_keys
        assert "REDIS_PORT" in database_section.optional_keys
        assert "REDIS_PORT" in database_section.validators
        
        # Test LLM section validation
        llm_section = config._get_llm_section()
        assert "CHAT_PROVIDER" in llm_section.required_keys
        assert "OPENAI_API_KEY" in llm_section.optional_keys
        assert "CHAT_PROVIDER" in llm_section.validators
        
        # Test session section validation
        session_section = config._get_session_section()
        assert "SESSION_TIMEOUT_MINUTES" in session_section.optional_keys
        assert "SESSION_TIMEOUT_MINUTES" in session_section.validators

    def test_specialized_config_getters(self, mock_environment):
        """Test get_database_config(), get_llm_config(), etc."""
        with patch.dict(os.environ, mock_environment):
            config = ConfigurationManager(validate_on_init=False)
            
            # Test database config
            db_config = config.get_database_config()
            assert db_config["host"] == "test.redis.com"
            assert db_config["port"] == 6379
            assert db_config["password"] == "test_password"
            assert db_config["db"] == 0  # Default
            assert db_config["ssl"] is False  # Default
            
            # Test LLM config
            llm_config = config.get_llm_config()
            assert llm_config["provider"] == "openai"
            assert llm_config["api_key"] == "test_openai_key"
            assert llm_config["model"] == "gpt-4o"  # Default
            assert llm_config["timeout"] == 30  # Default
            
            # Test logging config
            log_config = config.get_logging_config()
            assert log_config["level"] == "DEBUG"
            assert log_config["format"] == "json"  # Default
            assert log_config["dedupe"] is True  # Default
            
            # Test session config
            session_config = config.get_session_config()
            assert session_config["timeout_minutes"] == 45
            assert session_config["cleanup_interval_minutes"] == 15  # Default
            assert session_config["max_memory_mb"] == 100  # Default

    def test_environment_variable_precedence(self, sample_config_file):
        """Test env vars override file configuration."""
        env_overrides = {
            "REDIS_HOST": "env.override.com",
            "REDIS_DB": "5"
        }
        
        # Clear any environment variables that might interfere with the test
        env_to_clear = ["CHAT_PROVIDER", "ANTHROPIC_API_KEY"]
        with patch.dict(os.environ, env_overrides, clear=False):
            # Temporarily remove interfering environment variables
            for key in env_to_clear:
                os.environ.pop(key, None)
            
            config = ConfigurationManager(
                config_file=sample_config_file,
                validate_on_init=False
            )
            
            # Environment should override file
            assert config.get("REDIS_HOST") == "env.override.com"
            assert config.get_int("REDIS_DB") == 5
            
            # File values should be used when no env override
            assert config.get("CHAT_PROVIDER") == "anthropic"
            assert config.get("ANTHROPIC_API_KEY") == "file_anthropic_key"

    def test_config_file_loading_json(self, sample_config_file):
        """Test loading configuration from JSON file."""
        with patch.dict(os.environ, {}, clear=True):
            config = ConfigurationManager(
                config_file=sample_config_file,
                validate_on_init=False
            )
            
            assert config.get("REDIS_HOST") == "file.redis.com"
            assert config.get_int("REDIS_DB") == 2
            assert config.get("CHAT_PROVIDER") == "anthropic"

    def test_config_file_loading_env(self, sample_env_file):
        """Test loading configuration from .env file."""
        with patch.dict(os.environ, {}, clear=True):
            config = ConfigurationManager(
                config_file=sample_env_file,
                validate_on_init=False
            )
            
            assert config.get("REDIS_HOST") == "env.redis.com"
            assert config.get_int("REDIS_PORT") == 6380
            assert config.get("CHAT_PROVIDER") == "fireworks"

    def test_config_file_not_found(self):
        """Test handling of non-existent config file."""
        nonexistent_file = Path("/nonexistent/config.json")
        
        # Should not raise exception for missing file
        config = ConfigurationManager(
            config_file=nonexistent_file,
            validate_on_init=False
        )
        assert config is not None

    def test_validate_method(self, mock_environment):
        """Test configuration validation method."""
        with patch.dict(os.environ, mock_environment):
            config = ConfigurationManager(validate_on_init=False)
            
            # Should validate successfully with good config
            assert config.validate() is True
            assert len(config._validation_errors) == 0

    def test_validate_method_with_errors(self):
        """Test validation method with configuration errors."""
        invalid_env = {
            "CHAT_PROVIDER": "invalid_provider",
            "REDIS_PORT": "invalid_port",
            "SESSION_TIMEOUT_MINUTES": "0"  # Out of range
        }
        
        with patch.dict(os.environ, invalid_env, clear=True):
            config = ConfigurationManager(validate_on_init=False)
            
            # Should fail validation
            assert config.validate() is False
            assert len(config._validation_errors) > 0
            
            # Check specific error types
            errors = config._validation_errors
            assert any("REDIS_HOST" in error for error in errors)  # Missing required
            assert any("CHAT_PROVIDER" in error for error in errors)  # Invalid value

    def test_validator_functions(self):
        """Test individual validator functions work correctly."""
        config = ConfigurationManager(validate_on_init=False)
        
        # Test database validators
        db_section = config._get_database_section()
        port_validator = db_section.validators["REDIS_PORT"]
        
        assert port_validator("6379") is True
        assert port_validator("65535") is True
        assert port_validator("0") is False
        assert port_validator("99999") is False
        
        # Test LLM validators
        llm_section = config._get_llm_section()
        provider_validator = llm_section.validators["CHAT_PROVIDER"]
        
        assert provider_validator("openai") is True
        assert provider_validator("anthropic") is True
        assert provider_validator("invalid") is False
        
        # Test session validators
        session_section = config._get_session_section()
        timeout_validator = session_section.validators["SESSION_TIMEOUT_MINUTES"]
        
        assert timeout_validator("30") is True
        assert timeout_validator("1440") is True  # 24 hours
        assert timeout_validator("0") is False
        assert timeout_validator("2000") is False

    def test_section_validation_with_custom_section(self):
        """Test validation of custom configuration section."""
        config = ConfigurationManager(validate_on_init=False)
        
        # Create custom section
        custom_section = ConfigSection(
            name="custom",
            required_keys=["CUSTOM_REQUIRED"],
            optional_keys={"CUSTOM_OPTIONAL": "default"},
            validators={"CUSTOM_REQUIRED": lambda x: x.startswith("valid_")}
        )
        
        # Test with missing required key
        config._config_data = {"OTHER_KEY": "value"}
        config._validation_errors = []
        config._validate_section(custom_section)
        assert any("CUSTOM_REQUIRED" in error for error in config._validation_errors)
        
        # Test with invalid value
        config._config_data = {"CUSTOM_REQUIRED": "invalid_value"}
        config._validation_errors = []
        config._validate_section(custom_section)
        assert any("Invalid value" in error for error in config._validation_errors)
        
        # Test with valid value
        config._config_data = {"CUSTOM_REQUIRED": "valid_value"}
        config._validation_errors = []
        config._validate_section(custom_section)
        assert len(config._validation_errors) == 0

    def test_provider_specific_llm_config(self):
        """Test LLM config returns provider-specific settings."""
        # Test OpenAI config
        openai_env = {
            "CHAT_PROVIDER": "openai",
            "OPENAI_API_KEY": "test_openai_key",
            "OPENAI_MODEL": "gpt-4-turbo"
        }
        
        with patch.dict(os.environ, openai_env, clear=True):
            config = ConfigurationManager(validate_on_init=False)
            llm_config = config.get_llm_config()
            
            assert llm_config["provider"] == "openai"
            assert llm_config["api_key"] == "test_openai_key"
            assert llm_config["model"] == "gpt-4-turbo"
            assert "api_key" in llm_config
        
        # Test Anthropic config
        anthropic_env = {
            "CHAT_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "test_anthropic_key",
            "ANTHROPIC_MODEL": "claude-3-opus"
        }
        
        with patch.dict(os.environ, anthropic_env, clear=True):
            config = ConfigurationManager(validate_on_init=False)
            llm_config = config.get_llm_config()
            
            assert llm_config["provider"] == "anthropic"
            assert llm_config["api_key"] == "test_anthropic_key"
            assert llm_config["model"] == "claude-3-opus"

    def test_boolean_conversion_variations(self):
        """Test various boolean string representations."""
        bool_test_cases = {
            "true": True,
            "TRUE": True,
            "True": True,
            "1": True,
            "yes": True,
            "YES": True,
            "on": True,
            "ON": True,
            "false": False,
            "FALSE": False,
            "False": False,
            "0": False,
            "no": False,
            "NO": False,
            "off": False,
            "OFF": False,
            "": False,  # Empty string
            "maybe": False,  # Invalid value
        }
        
        for string_val, expected_bool in bool_test_cases.items():
            env = {"TEST_BOOL": string_val}
            with patch.dict(os.environ, env, clear=True):
                config = ConfigurationManager(validate_on_init=False)
                result = config.get_bool("TEST_BOOL")
                assert result == expected_bool, f"'{string_val}' should convert to {expected_bool}"

    def test_integer_conversion_edge_cases(self):
        """Test integer conversion with edge cases."""
        int_test_cases = {
            "0": 0,
            "42": 42,
            "-10": -10,
            "999999": 999999,
            "not_a_number": 42,  # Should return default
            "": 42,  # Empty string should return default
            "42.5": 42,  # Float string converts to int
        }
        
        for string_val, expected_int in int_test_cases.items():
            env = {"TEST_INT": string_val} if string_val else {}
            with patch.dict(os.environ, env, clear=True):
                config = ConfigurationManager(validate_on_init=False)
                result = config.get_int("TEST_INT", 42)
                assert result == expected_int, f"'{string_val}' should convert to {expected_int}"

    def test_configuration_sections_completeness(self):
        """Test all configuration sections are properly defined."""
        config = ConfigurationManager(validate_on_init=False)
        
        # Verify all expected sections exist
        expected_sections = [
            "database", "llm", "logging", "session", 
            "security", "observability", "performance"
        ]
        
        section_names = [section.name for section in config._config_sections]
        for expected in expected_sections:
            assert expected in section_names, f"Missing configuration section: {expected}"

    def test_configuration_defaults_are_reasonable(self):
        """Test configuration defaults are sensible."""
        with patch.dict(os.environ, {}, clear=True):
            config = ConfigurationManager(validate_on_init=False)
            
            # Database defaults
            db_config = config.get_database_config()
            assert db_config["port"] == 6379  # Standard Redis port
            assert db_config["db"] == 0  # Default Redis database
            assert db_config["timeout"] == 30  # Reasonable timeout
            
            # Session defaults
            session_config = config.get_session_config()
            assert 1 <= session_config["timeout_minutes"] <= 1440  # 1 min to 24 hours
            assert session_config["cleanup_interval_minutes"] > 0
            assert session_config["max_memory_mb"] > 0
            
            # Performance defaults
            perf_config = config.get_performance_config()
            assert perf_config["request_timeout"] > 0
            assert perf_config["worker_pool_size"] >= 1
            assert perf_config["max_concurrent_requests"] > 0


class TestConfigurationManagerSingleton:
    """Test cases for configuration manager singleton behavior."""
    
    def test_get_config_singleton(self):
        """Test get_config() returns singleton instance."""
        # Reset singleton for clean test
        reset_config()
        
        with patch.dict(os.environ, {"REDIS_HOST": "singleton.test.com"}):
            config1 = get_config()
            config2 = get_config()
            
            # Should be same instance
            assert config1 is config2
            assert config1.get("REDIS_HOST") == "singleton.test.com"

    def test_reset_config_functionality(self):
        """Test reset_config() clears singleton."""
        with patch.dict(os.environ, {"REDIS_HOST": "reset.test.com"}):
            config1 = get_config()
            reset_config()
            config2 = get_config()
            
            # Should be different instances after reset
            assert config1 is not config2
            # But should have same config values
            assert config1.get("REDIS_HOST") == config2.get("REDIS_HOST")

    def test_singleton_thread_safety(self):
        """Test singleton behavior under concurrent access."""
        import threading
        import time
        
        reset_config()
        instances = []
        
        def get_config_instance():
            time.sleep(0.01)  # Small delay to increase chance of race condition
            instance = get_config()
            instances.append(instance)
        
        # Create multiple threads
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=get_config_instance)
            threads.append(thread)
        
        # Start all threads
        for thread in threads:
            thread.start()
        
        # Wait for completion
        for thread in threads:
            thread.join()
        
        # All instances should be the same object
        first_instance = instances[0]
        for instance in instances[1:]:
            assert instance is first_instance


class TestConfigSection:
    """Test cases for ConfigSection dataclass."""
    
    def test_config_section_creation(self):
        """Test ConfigSection can be created with various parameters."""
        section = ConfigSection(
            name="test_section",
            required_keys=["KEY1", "KEY2"],
            optional_keys={"KEY3": "default", "KEY4": 42},
            validators={"KEY1": lambda x: len(x) > 5}
        )
        
        assert section.name == "test_section"
        assert "KEY1" in section.required_keys
        assert "KEY3" in section.optional_keys
        assert "KEY1" in section.validators

    def test_config_section_defaults(self):
        """Test ConfigSection uses proper defaults."""
        section = ConfigSection(name="minimal")
        
        assert section.name == "minimal"
        assert section.required_keys == []
        assert section.optional_keys == {}
        assert section.validators == {}

    def test_config_section_validator_execution(self):
        """Test validators in ConfigSection work correctly."""
        def length_validator(value):
            return len(str(value)) >= 3
        
        section = ConfigSection(
            name="validation_test",
            validators={"TEST_KEY": length_validator}
        )
        
        validator = section.validators["TEST_KEY"]
        assert validator("abc") is True
        assert validator("ab") is False
        assert validator(123) is True  # "123" has length 3
        assert validator(12) is False  # "12" has length 2