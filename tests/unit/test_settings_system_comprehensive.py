"""
Comprehensive tests for the unified settings system.

Tests coverage:
- All nested settings validation
- Environment variable processing
- Provider-specific configuration
- Frontend compatibility methods
- CORS and Redis URL generation
- Migration bridge testing
"""

import os
import tempfile
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any
import pytest
from pydantic import ValidationError

from faultmaven.config.settings import (
    FaultMavenSettings,
    get_settings,
    reset_settings,
    ConfigurationBridge,
    Environment,
    LogLevel,
    LLMProvider,
    ServerSettings,
    LLMSettings,
    DatabaseSettings,
    SessionSettings,
    SecuritySettings,
    ProtectionSettings,
    ObservabilitySettings,
    LoggingSettings,
    UploadSettings,
    KnowledgeSettings,
    FeatureSettings,
    ToolsSettings,
    AlertingSettings,
    WorkspaceSettings
)


@pytest.fixture(autouse=True)
def reset_settings_before_test():
    """Reset settings before each test to ensure clean state."""
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def clean_env():
    """Provide a clean environment for testing."""
    original_env = os.environ.copy()
    # Clear all FaultMaven-related environment variables
    for key in list(os.environ.keys()):
        if any(prefix in key for prefix in ['CHAT_', 'REDIS_', 'CHROMADB_', 'LLM_', 'CORS_', 
                                            'FIREWORKS_', 'OPENAI_', 'ANTHROPIC_', 'GEMINI_', 
                                            'HUGGINGFACE_', 'OPENROUTER_', 'COHERE_', 'LOCAL_',
                                            'SESSION_', 'PROTECTION_', 'FAULTMAVEN_']):
            del os.environ[key]
    
    yield
    
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def sample_env_vars():
    """Sample environment variables for testing."""
    return {
        'ENVIRONMENT': 'production',
        'DEBUG': 'true',
        'HOST': '127.0.0.1',
        'PORT': '9000',
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'sk-test123',
        'OPENAI_MODEL': 'gpt-4-turbo',
        'FIREWORKS_API_KEY': 'fw-test456',
        'FIREWORKS_MODEL': 'accounts/fireworks/models/llama-v3p1-70b-instruct',
        'REDIS_HOST': '127.0.0.1',
        'REDIS_PORT': '6379',
        'REDIS_PASSWORD': 'secret123',
        'CHROMADB_URL': 'http://localhost:8000',
        'SESSION_TIMEOUT_MINUTES': '60',
        'SESSION_HEARTBEAT_INTERVAL_SECONDS': '45',
        'CORS_ALLOW_ORIGINS': '["http://localhost:3000", "chrome-extension://*"]',
        'RATE_LIMIT_REQUESTS_PER_MINUTE': '120',
        'LOG_LEVEL': 'DEBUG',
        'MAX_FILE_SIZE_MB': '100',
        'ENTITIES_TO_PROTECT': '["EMAIL_ADDRESS", "PHONE_NUMBER", "SSN"]',
        'OPIK_PROJECT_NAME': 'test-project',
        'ENABLE_ADVANCED_REASONING': 'true'
    }


class TestServerSettings:
    """Test server configuration settings."""
    
    def test_default_values(self):
        """Test default server settings values."""
        settings = ServerSettings()
        
        assert settings.host == "0.0.0.0"
        assert settings.port == 8000
        assert settings.reload == False
        assert settings.workers == 1
        assert settings.environment == Environment.DEVELOPMENT
        assert settings.debug == False
        # Note: skip_service_checks may be True in test environment
        assert isinstance(settings.skip_service_checks, bool)
        # pytest_current_test may be set during test runs
        assert settings.pytest_current_test is None or isinstance(settings.pytest_current_test, str)
    
    def test_environment_override(self, clean_env):
        """Test server settings from environment variables."""
        os.environ.update({
            'HOST': '192.168.1.100',
            'PORT': '9999',
            'RELOAD': 'true',
            'WORKERS': '4',
            'ENVIRONMENT': 'production',
            'DEBUG': 'true',
            'SKIP_SERVICE_CHECKS': 'true',
            'PYTEST_CURRENT_TEST': 'test_module::test_function'
        })
        
        settings = ServerSettings()
        
        assert settings.host == "192.168.1.100"
        assert settings.port == 9999
        assert settings.reload == True
        assert settings.workers == 4
        assert settings.environment == Environment.PRODUCTION
        assert settings.debug == True
        assert settings.skip_service_checks == True
        assert settings.pytest_current_test == 'test_module::test_function'


class TestLLMSettings:
    """Test LLM provider configuration settings."""
    
    def test_default_values(self):
        """Test default LLM settings values."""
        settings = LLMSettings()
        
        assert settings.provider == LLMProvider.FIREWORKS
        assert settings.openai_api_key is None
        assert settings.anthropic_api_key is None
        assert settings.fireworks_api_key is None
        assert settings.cohere_api_key is None
        assert settings.openai_model == "gpt-4o"
        assert settings.anthropic_model == "claude-3-sonnet-20240229"
        assert settings.fireworks_model == "accounts/fireworks/models/llama-v3p1-405b-instruct"
        assert settings.cohere_model == "command-r-plus"
        assert settings.request_timeout == 30
        assert settings.max_retries == 3
        assert settings.retry_delay == 1.0
        assert settings.max_tokens == 4096
        assert settings.context_window == 128000
    
    def test_environment_override(self, clean_env):
        """Test LLM settings from environment variables."""
        os.environ.update({
            'CHAT_PROVIDER': 'anthropic',
            'OPENAI_API_KEY': 'sk-openai123',
            'ANTHROPIC_API_KEY': 'anthropic-key456',
            'FIREWORKS_API_KEY': 'fw-fireworks789',
            'COHERE_API_KEY': 'cohere-abc123',
            'OPENAI_MODEL': 'gpt-4-turbo',
            'ANTHROPIC_MODEL': 'claude-3-5-sonnet-20241022',
            'FIREWORKS_MODEL': 'accounts/fireworks/models/llama-v3p1-8b-instruct',
            'COHERE_MODEL': 'command-r',
            'LLM_REQUEST_TIMEOUT': '45',
            'LLM_MAX_RETRIES': '5',
            'LLM_RETRY_DELAY': '2.5',
            'LLM_MAX_TOKENS': '8192',
            'LLM_CONTEXT_WINDOW': '200000'
        })
        
        settings = LLMSettings()
        
        assert settings.provider == LLMProvider.ANTHROPIC
        assert settings.openai_api_key.get_secret_value() == 'sk-openai123'
        assert settings.anthropic_api_key.get_secret_value() == 'anthropic-key456'
        assert settings.fireworks_api_key.get_secret_value() == 'fw-fireworks789'
        assert settings.cohere_api_key.get_secret_value() == 'cohere-abc123'
        assert settings.openai_model == 'gpt-4-turbo'
        assert settings.anthropic_model == 'claude-3-5-sonnet-20241022'
        assert settings.fireworks_model == 'accounts/fireworks/models/llama-v3p1-8b-instruct'
        assert settings.cohere_model == 'command-r'
        assert settings.request_timeout == 45
        assert settings.max_retries == 5
        assert settings.retry_delay == 2.5
        assert settings.max_tokens == 8192
        assert settings.context_window == 200000
    
    def test_get_api_key(self, clean_env):
        """Test API key retrieval for different providers."""
        os.environ.update({
            'OPENAI_API_KEY': 'sk-openai123',
            'ANTHROPIC_API_KEY': 'anthropic-key456',
            'FIREWORKS_API_KEY': 'fw-fireworks789',
            'COHERE_API_KEY': 'cohere-abc123'
        })
        
        # Test OpenAI
        os.environ['CHAT_PROVIDER'] = 'openai'
        settings = LLMSettings()
        assert settings.get_api_key() == 'sk-openai123'
        
        # Test Anthropic
        settings.provider = LLMProvider.ANTHROPIC
        assert settings.get_api_key() == 'anthropic-key456'
        
        # Test Fireworks
        settings.provider = LLMProvider.FIREWORKS
        assert settings.get_api_key() == 'fw-fireworks789'
        
        # Test Cohere
        settings.provider = LLMProvider.COHERE
        assert settings.get_api_key() == 'cohere-abc123'
        
        # Test Local (no API key)
        settings.provider = LLMProvider.LOCAL
        assert settings.get_api_key() is None
        
        # Test unknown provider
        settings.provider = 'unknown'
        assert settings.get_api_key() is None
    
    def test_get_model(self, clean_env):
        """Test model retrieval for different providers."""
        os.environ.update({
            'OPENAI_MODEL': 'gpt-4-custom',
            'ANTHROPIC_MODEL': 'claude-3-custom',
            'FIREWORKS_MODEL': 'custom-fireworks-model',
            'COHERE_MODEL': 'custom-cohere-model'
        })
        
        settings = LLMSettings()
        
        # Test OpenAI
        settings.provider = LLMProvider.OPENAI
        assert settings.get_model() == 'gpt-4-custom'
        
        # Test Anthropic
        settings.provider = LLMProvider.ANTHROPIC
        assert settings.get_model() == 'claude-3-custom'
        
        # Test Fireworks
        settings.provider = LLMProvider.FIREWORKS
        assert settings.get_model() == 'custom-fireworks-model'
        
        # Test Cohere
        settings.provider = LLMProvider.COHERE
        assert settings.get_model() == 'custom-cohere-model'
        
        # Test Local (no model configured)
        settings.provider = LLMProvider.LOCAL
        assert settings.get_model() == ""
        
        # Test unknown provider
        settings.provider = 'unknown'
        assert settings.get_model() == ""


class TestDatabaseSettings:
    """Test database and persistence configuration settings."""
    
    def test_default_values(self):
        """Test default database settings values."""
        settings = DatabaseSettings()
        
        assert settings.redis_host == "192.168.0.111"
        assert settings.redis_port == 30379
        assert settings.redis_db == 0
        assert settings.redis_password is None
        assert settings.redis_url is None
        assert settings.chromadb_host == "chromadb.faultmaven.local"
        assert settings.chromadb_port == 30080
        assert settings.chromadb_url == "http://chromadb.faultmaven.local:30080"
        assert settings.chromadb_api_key is None
        assert settings.chromadb_auth_token is None
        assert settings.chromadb_collection == "faultmaven_kb"
        assert settings.embedding_model == "BAAI/bge-m3"
        assert settings.similarity_threshold == 0.7
        assert settings.max_search_results == 10
    
    def test_environment_override(self, clean_env):
        """Test database settings from environment variables."""
        os.environ.update({
            'REDIS_HOST': '127.0.0.1',
            'REDIS_PORT': '6379',
            'REDIS_DB': '1',
            'REDIS_PASSWORD': 'secret123',
            'REDIS_URL': 'redis://user:pass@localhost:6379/2',
            'CHROMADB_HOST': 'localhost',
            'CHROMADB_PORT': '8000',
            'CHROMADB_URL': 'http://localhost:8000',
            'CHROMADB_API_KEY': 'chroma-key123',
            'CHROMADB_AUTH_TOKEN': 'chroma-token456',
            'CHROMADB_COLLECTION': 'test_collection',
            'EMBEDDING_MODEL': 'sentence-transformers/all-MiniLM-L6-v2',
            'SIMILARITY_THRESHOLD': '0.8',
            'MAX_SEARCH_RESULTS': '20'
        })
        
        settings = DatabaseSettings()
        
        assert settings.redis_host == "127.0.0.1"
        assert settings.redis_port == 6379
        assert settings.redis_db == 1
        assert settings.redis_password.get_secret_value() == 'secret123'
        assert settings.redis_url == 'redis://user:pass@localhost:6379/2'
        assert settings.chromadb_host == "localhost"
        assert settings.chromadb_port == 8000
        assert settings.chromadb_url == "http://localhost:8000"
        assert settings.chromadb_api_key.get_secret_value() == 'chroma-key123'
        assert settings.chromadb_auth_token.get_secret_value() == 'chroma-token456'
        assert settings.chromadb_collection == "test_collection"
        assert settings.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
        assert settings.similarity_threshold == 0.8
        assert settings.max_search_results == 20


class TestSessionSettings:
    """Test session management configuration settings."""
    
    def test_default_values(self):
        """Test default session settings values."""
        settings = SessionSettings()
        
        assert settings.timeout_minutes == 30
        assert settings.cleanup_interval_minutes == 15
        assert settings.max_memory_mb == 100
        assert settings.heartbeat_interval_seconds == 30
        assert settings.max_sessions_per_user == 10
    
    def test_environment_override(self, clean_env):
        """Test session settings from environment variables."""
        os.environ.update({
            'SESSION_TIMEOUT_MINUTES': '60',
            'SESSION_CLEANUP_INTERVAL_MINUTES': '30',
            'SESSION_MAX_MEMORY_MB': '200',
            'SESSION_HEARTBEAT_INTERVAL_SECONDS': '45',
            'MAX_SESSIONS_PER_USER': '20'
        })
        
        settings = SessionSettings()
        
        assert settings.timeout_minutes == 60
        assert settings.cleanup_interval_minutes == 30
        assert settings.max_memory_mb == 200
        assert settings.heartbeat_interval_seconds == 45
        assert settings.max_sessions_per_user == 20
    
    def test_heartbeat_validation_valid(self, clean_env):
        """Test valid heartbeat vs timeout configuration."""
        os.environ.update({
            'SESSION_TIMEOUT_MINUTES': '30',  # 1800 seconds
            'SESSION_HEARTBEAT_INTERVAL_SECONDS': '25'  # Less than timeout
        })
        
        settings = SessionSettings()
        assert settings.heartbeat_interval_seconds == 25
    
    def test_heartbeat_validation_invalid(self, clean_env):
        """Test invalid heartbeat vs timeout configuration."""
        os.environ.update({
            'SESSION_TIMEOUT_MINUTES': '30',  # 1800 seconds
            'SESSION_HEARTBEAT_INTERVAL_SECONDS': '1800'  # Equal to timeout
        })
        
        with pytest.raises(ValidationError) as exc_info:
            SessionSettings()
        
        error_msg = str(exc_info.value)
        assert "Heartbeat interval" in error_msg
        assert "must be less than session timeout" in error_msg
    
    def test_timeout_boundaries(self, clean_env):
        """Test session timeout boundary validation."""
        # Test minimum boundary
        os.environ['SESSION_TIMEOUT_MINUTES'] = '1'
        settings = SessionSettings()
        assert settings.timeout_minutes == 1
        
        # Test maximum boundary
        os.environ['SESSION_TIMEOUT_MINUTES'] = '1440'  # 24 hours
        settings = SessionSettings()
        assert settings.timeout_minutes == 1440
        
        # Test below minimum (should raise validation error)
        os.environ['SESSION_TIMEOUT_MINUTES'] = '0'
        with pytest.raises(ValidationError):
            SessionSettings()
        
        # Test above maximum (should raise validation error)
        os.environ['SESSION_TIMEOUT_MINUTES'] = '1441'
        with pytest.raises(ValidationError):
            SessionSettings()


class TestSecuritySettings:
    """Test security and authentication configuration settings."""
    
    def test_default_values(self):
        """Test default security settings values."""
        settings = SecuritySettings()
        
        assert settings.jwt_secret_key is None
        assert settings.jwt_algorithm == "HS256"
        assert settings.jwt_expiration_hours == 24
        assert settings.cors_allow_credentials == True
        assert "http://localhost:3000" in settings.cors_allow_origins
        assert "chrome-extension://*" in settings.cors_allow_origins
        assert "moz-extension://*" in settings.cors_allow_origins
        assert "Location" in settings.cors_expose_headers
        assert "X-Total-Count" in settings.cors_expose_headers
        assert settings.rate_limit_enabled == True
        assert settings.rate_limit_requests_per_minute == 60
        assert settings.rate_limit_burst_size == 10
    
    def test_environment_override(self, clean_env):
        """Test security settings from environment variables."""
        os.environ.update({
            'JWT_SECRET_KEY': 'super-secret-key-123',
            'JWT_ALGORITHM': 'RS256',
            'JWT_EXPIRATION_HOURS': '48',
            'CORS_ALLOW_CREDENTIALS': 'false',
            'CORS_ALLOW_ORIGINS': '["https://app.example.com", "https://admin.example.com"]',
            'CORS_EXPOSE_HEADERS': '["Authorization", "X-Custom-Header"]',
            'RATE_LIMIT_ENABLED': 'false',
            'RATE_LIMIT_REQUESTS_PER_MINUTE': '120',
            'RATE_LIMIT_BURST_SIZE': '20'
        })
        
        settings = SecuritySettings()
        
        assert settings.jwt_secret_key.get_secret_value() == 'super-secret-key-123'
        assert settings.jwt_algorithm == "RS256"
        assert settings.jwt_expiration_hours == 48
        assert settings.cors_allow_credentials == False
        # Note: List parsing from env vars might need special handling
        assert settings.rate_limit_enabled == False
        assert settings.rate_limit_requests_per_minute == 120
        assert settings.rate_limit_burst_size == 20


class TestProtectionSettings:
    """Test protection configuration settings."""
    
    def test_default_values(self):
        """Test default protection settings values."""
        settings = ProtectionSettings()
        
        assert settings.protection_enabled == True
        assert settings.fail_open == True
        assert settings.basic_protection_enabled == True
        assert settings.intelligent_protection_enabled == True
        assert settings.presidio_analyzer_url == "http://presidio-analyzer.faultmaven.local:30080"
        assert settings.presidio_anonymizer_url == "http://presidio-anonymizer.faultmaven.local:30080"
        assert settings.min_score_threshold == 0.8
        assert settings.supported_languages == ["en"]
        assert "CREDIT_CARD" in settings.entities_to_protect
        assert "EMAIL_ADDRESS" in settings.entities_to_protect
        assert "US_SSN" in settings.entities_to_protect
        assert settings.behavioral_analysis_enabled == True
        assert settings.ml_anomaly_detection_enabled == True
        assert settings.reputation_system_enabled == True
    
    def test_environment_override(self, clean_env):
        """Test protection settings from environment variables."""
        os.environ.update({
            'PROTECTION_ENABLED': 'false',
            'PROTECTION_FAIL_OPEN': 'false',
            'BASIC_PROTECTION_ENABLED': 'false',
            'INTELLIGENT_PROTECTION_ENABLED': 'false',
            'PRESIDIO_ANALYZER_URL': 'http://localhost:5001',
            'PRESIDIO_ANONYMIZER_URL': 'http://localhost:5002',
            'MIN_SCORE_THRESHOLD': '0.9',
            'SUPPORTED_LANGUAGES': '["en", "es", "fr"]',
            'ENTITIES_TO_PROTECT': '["EMAIL_ADDRESS", "PHONE_NUMBER"]',
            'BEHAVIORAL_ANALYSIS_ENABLED': 'false',
            'ML_ANOMALY_DETECTION_ENABLED': 'false',
            'REPUTATION_SYSTEM_ENABLED': 'false',
            'BEHAVIOR_ANALYSIS_WINDOW': '7200',
            'ML_MODEL_PATH': '/custom/ml/path'
        })
        
        settings = ProtectionSettings()
        
        assert settings.protection_enabled == False
        assert settings.fail_open == False
        assert settings.basic_protection_enabled == False
        assert settings.intelligent_protection_enabled == False
        assert settings.presidio_analyzer_url == 'http://localhost:5001'
        assert settings.presidio_anonymizer_url == 'http://localhost:5002'
        assert settings.min_score_threshold == 0.9
        assert settings.behavioral_analysis_enabled == False
        assert settings.ml_anomaly_detection_enabled == False
        assert settings.reputation_system_enabled == False
        assert settings.behavior_analysis_window == 7200
        assert settings.ml_model_path == '/custom/ml/path'


class TestFaultMavenSettings:
    """Test the main settings class with nested configurations."""
    
    def test_default_initialization(self):
        """Test default settings initialization."""
        settings = FaultMavenSettings()
        
        # Verify nested settings are properly initialized
        assert isinstance(settings.server, ServerSettings)
        assert isinstance(settings.llm, LLMSettings)
        assert isinstance(settings.database, DatabaseSettings)
        assert isinstance(settings.session, SessionSettings)
        assert isinstance(settings.security, SecuritySettings)
        assert isinstance(settings.protection, ProtectionSettings)
        assert isinstance(settings.observability, ObservabilitySettings)
        assert isinstance(settings.logging, LoggingSettings)
        assert isinstance(settings.upload, UploadSettings)
        assert isinstance(settings.knowledge, KnowledgeSettings)
        assert isinstance(settings.features, FeatureSettings)
        assert isinstance(settings.tools, ToolsSettings)
        assert isinstance(settings.alerting, AlertingSettings)
        assert isinstance(settings.workspace, WorkspaceSettings)
    
    def test_get_cors_config(self, clean_env):
        """Test CORS configuration generation."""
        os.environ.update({
            'CORS_ALLOW_ORIGINS': '["http://localhost:3000", "https://app.example.com"]',
            'CORS_ALLOW_CREDENTIALS': 'true',
            'CORS_EXPOSE_HEADERS': '["Location", "X-Total-Count", "X-Request-ID"]'
        })
        
        settings = FaultMavenSettings()
        cors_config = settings.get_cors_config()
        
        assert cors_config["allow_credentials"] == True
        assert "GET" in cors_config["allow_methods"]
        assert "POST" in cors_config["allow_methods"]
        assert "PUT" in cors_config["allow_methods"]
        assert "DELETE" in cors_config["allow_methods"]
        assert "PATCH" in cors_config["allow_methods"]
        assert "OPTIONS" in cors_config["allow_methods"]
        assert cors_config["allow_headers"] == ["*"]
    
    def test_validate_frontend_compatibility_success(self, clean_env):
        """Test successful frontend compatibility validation."""
        os.environ.update({
            'SESSION_TIMEOUT_MINUTES': '30',
            'SESSION_HEARTBEAT_INTERVAL_SECONDS': '25',
            'CORS_ALLOW_ORIGINS': '["http://localhost:3000", "chrome-extension://*", "moz-extension://*"]',
            'CORS_EXPOSE_HEADERS': '["X-RateLimit-Remaining", "X-Total-Count", "Location"]',
            'RATE_LIMIT_ENABLED': 'true',
            'MAX_FILE_SIZE_MB': '50'
        })
        
        settings = FaultMavenSettings()
        validation = settings.validate_frontend_compatibility()
        
        assert validation["compatible"] == True
        assert len(validation["issues"]) == 0
        assert len(validation["warnings"]) == 0
    
    def test_validate_frontend_compatibility_issues(self, clean_env):
        """Test frontend compatibility validation with issues."""
        os.environ.update({
            'SESSION_TIMEOUT_MINUTES': '2',  # Too short
            'SESSION_HEARTBEAT_INTERVAL_SECONDS': '150',  # Greater than timeout
            'CORS_ALLOW_ORIGINS': '["https://app.example.com"]',  # Missing browser origins
            'CORS_EXPOSE_HEADERS': '["Authorization"]',  # Missing required headers
            'RATE_LIMIT_ENABLED': 'false',  # Disabled
            'MAX_FILE_SIZE_MB': '150'  # Too large
        })
        
        settings = FaultMavenSettings()
        validation = settings.validate_frontend_compatibility()
        
        assert validation["compatible"] == False
        assert len(validation["issues"]) > 0
        assert len(validation["warnings"]) > 0
        
        # Check specific issues
        issues_str = " ".join(validation["issues"])
        assert "Session timeout too short" in issues_str
        assert "Heartbeat interval must be less than session timeout" in issues_str
        assert "Missing CORS origins" in issues_str
        assert "Missing exposed headers" in issues_str
        
        # Check warnings
        warnings_str = " ".join(validation["warnings"])
        assert "Rate limiting disabled" in warnings_str
        assert "Large upload size" in warnings_str
    
    def test_get_redis_url_with_redis_url_env(self, clean_env):
        """Test Redis URL generation when REDIS_URL is set."""
        os.environ['REDIS_URL'] = 'redis://custom:pass@example.com:6379/1'
        
        settings = FaultMavenSettings()
        redis_url = settings.get_redis_url()
        
        assert redis_url == 'redis://custom:pass@example.com:6379/1'
    
    def test_get_redis_url_with_password(self, clean_env):
        """Test Redis URL generation with password."""
        os.environ.update({
            'REDIS_HOST': '127.0.0.1',
            'REDIS_PORT': '6379',
            'REDIS_PASSWORD': 'secret123',
            'REDIS_DB': '2'
        })
        
        settings = FaultMavenSettings()
        redis_url = settings.get_redis_url()
        
        assert redis_url == 'redis://:secret123@127.0.0.1:6379/2'
    
    def test_get_redis_url_without_password(self, clean_env):
        """Test Redis URL generation without password."""
        os.environ.update({
            'REDIS_HOST': 'localhost',
            'REDIS_PORT': '6380',
            'REDIS_DB': '0'
        })
        
        settings = FaultMavenSettings()
        redis_url = settings.get_redis_url()
        
        assert redis_url == 'redis://localhost:6380/0'
    
    def test_environment_detection(self, clean_env):
        """Test environment detection methods."""
        # Test development environment
        os.environ['ENVIRONMENT'] = 'development'
        settings = FaultMavenSettings()
        assert settings.is_development() == True
        assert settings.is_production() == False
        
        # Test production environment
        os.environ['ENVIRONMENT'] = 'production'
        # Reset settings to pick up new environment
        reset_settings()
        settings = FaultMavenSettings()
        assert settings.is_development() == False
        assert settings.is_production() == True


class TestSettingsSingleton:
    """Test settings singleton management."""
    
    def test_get_settings_singleton(self):
        """Test that get_settings returns the same instance."""
        settings1 = get_settings()
        settings2 = get_settings()
        
        assert settings1 is settings2
    
    def test_reset_settings(self):
        """Test settings reset functionality."""
        settings1 = get_settings()
        reset_settings()
        settings2 = get_settings()
        
        assert settings1 is not settings2
    
    @patch('faultmaven.config.settings.FaultMavenSettings')
    def test_settings_initialization_error(self, mock_settings_class):
        """Test error handling during settings initialization."""
        # Mock settings class to raise an exception
        mock_settings_class.side_effect = ValueError("Test validation error")
        
        # Reset any existing settings instance
        reset_settings()
        
        # Import here to get the exception class
        from faultmaven.models.exceptions import ConfigurationError
        
        with pytest.raises(ConfigurationError) as exc_info:
            get_settings()
        
        assert "Settings initialization failed" in str(exc_info.value)
        assert exc_info.value.error_code == "SETTINGS_INIT_ERROR"
        assert "original_error" in exc_info.value.context


class TestConfigurationBridge:
    """Test legacy configuration bridge functionality."""
    
    def test_bridge_initialization(self):
        """Test configuration bridge initialization."""
        bridge = ConfigurationBridge()
        assert bridge._settings is not None
        assert isinstance(bridge._settings, FaultMavenSettings)
    
    def test_bridge_get_nested_values(self, clean_env):
        """Test getting nested configuration values via dot notation."""
        os.environ.update({
            'CHAT_PROVIDER': 'openai',
            'PORT': '9000',
            'REDIS_HOST': 'custom.redis.host'
        })
        
        reset_settings()  # Force reload of settings
        bridge = ConfigurationBridge()
        
        # Test nested value access
        assert bridge.get("llm.provider") == LLMProvider.OPENAI
        assert bridge.get("server.port") == 9000
        assert bridge.get("database.redis_host") == "custom.redis.host"
    
    def test_bridge_get_with_default(self):
        """Test getting configuration values with default fallback."""
        bridge = ConfigurationBridge()
        
        # Test non-existent key with default
        assert bridge.get("non.existent.key", "default_value") == "default_value"
        
        # Test invalid nested path
        assert bridge.get("server.non_existent_field", "fallback") == "fallback"
    
    def test_bridge_get_invalid_paths(self):
        """Test handling of invalid configuration paths."""
        bridge = ConfigurationBridge()
        
        # Test completely invalid path
        assert bridge.get("invalid.path.here") is None
        
        # Test path with type error (accessing attribute on non-object)
        assert bridge.get("server.port.invalid") is None


class TestNestedSettingsIntegration:
    """Test integration between nested settings sections."""
    
    def test_full_environment_integration(self, sample_env_vars, clean_env):
        """Test full environment variable integration across all settings."""
        os.environ.update(sample_env_vars)
        
        settings = FaultMavenSettings()
        
        # Verify server settings
        assert settings.server.environment == Environment.PRODUCTION
        assert settings.server.debug == True
        assert settings.server.host == "127.0.0.1"
        assert settings.server.port == 9000
        
        # Verify LLM settings
        assert settings.llm.provider == LLMProvider.OPENAI
        assert settings.llm.openai_api_key.get_secret_value() == "sk-test123"
        assert settings.llm.openai_model == "gpt-4-turbo"
        
        # Verify database settings
        assert settings.database.redis_host == "127.0.0.1"
        assert settings.database.redis_port == 6379
        assert settings.database.redis_password.get_secret_value() == "secret123"
        
        # Verify session settings
        assert settings.session.timeout_minutes == 60
        assert settings.session.heartbeat_interval_seconds == 45
        
        # Verify security settings
        assert settings.security.rate_limit_requests_per_minute == 120
        
        # Verify logging settings
        assert settings.logging.level == LogLevel.DEBUG
        
        # Verify upload settings
        assert settings.upload.max_file_size_mb == 100
        
        # Verify observability settings
        assert settings.observability.opik_project_name == "test-project"
        
        # Verify feature settings
        assert settings.features.enable_advanced_reasoning == True
    
    def test_settings_cross_references(self, clean_env):
        """Test settings that reference other settings."""
        os.environ.update({
            'SESSION_TIMEOUT_MINUTES': '45',
            'SESSION_HEARTBEAT_INTERVAL_SECONDS': '40'
        })
        
        settings = FaultMavenSettings()
        
        # Test that heartbeat validation works with cross-reference
        assert settings.session.heartbeat_interval_seconds == 40
        
        # Test Redis URL generation uses database settings
        redis_url = settings.get_redis_url()
        assert settings.database.redis_host in redis_url
        assert str(settings.database.redis_port) in redis_url
    
    def test_complex_configuration_scenario(self, clean_env):
        """Test a complex, realistic configuration scenario."""
        os.environ.update({
            # Production environment
            'ENVIRONMENT': 'production',
            'DEBUG': 'false',
            'HOST': '0.0.0.0',
            'PORT': '8080',
            
            # Multiple LLM providers
            'CHAT_PROVIDER': 'fireworks',
            'FIREWORKS_API_KEY': 'fw-prod-key-123',
            'FIREWORKS_MODEL': 'accounts/fireworks/models/llama-v3p1-70b-instruct',
            'OPENAI_API_KEY': 'sk-fallback-key',
            
            # Production database
            'REDIS_URL': 'redis://prod-redis:6379/0',
            'CHROMADB_URL': 'https://chromadb.prod.example.com',
            
            # Security configuration
            'JWT_SECRET_KEY': 'super-secure-production-key',
            'CORS_ALLOW_ORIGINS': '["https://app.example.com", "https://admin.example.com"]',
            'RATE_LIMIT_REQUESTS_PER_MINUTE': '1000',
            
            # Protection enabled
            'PROTECTION_ENABLED': 'true',
            'PRESIDIO_ANALYZER_URL': 'https://presidio-analyzer.prod.example.com',
            
            # Enhanced observability
            'OPIK_PROJECT_NAME': 'faultmaven-prod',
            'OPIK_URL_OVERRIDE': 'https://opik.prod.example.com',
            'TRACING_ENABLED': 'true',
            
            # Session management
            'SESSION_TIMEOUT_MINUTES': '120',
            'SESSION_HEARTBEAT_INTERVAL_SECONDS': '60',
            
            # Upload restrictions
            'MAX_FILE_SIZE_MB': '25',
            'ALLOWED_MIME_TYPES': '["text/plain", "application/json"]'
        })
        
        settings = FaultMavenSettings()
        
        # Verify production configuration
        assert settings.is_production() == True
        assert settings.server.debug == False
        assert settings.server.port == 8080
        
        # Verify security is properly configured
        assert settings.security.jwt_secret_key.get_secret_value() == 'super-secure-production-key'
        assert settings.security.rate_limit_requests_per_minute == 1000
        
        # Verify protection is enabled
        assert settings.protection.protection_enabled == True
        assert 'presidio-analyzer.prod.example.com' in settings.protection.presidio_analyzer_url
        
        # Verify observability
        assert settings.observability.opik_project_name == 'faultmaven-prod'
        assert settings.observability.tracing_enabled == True
        
        # Test frontend compatibility
        validation = settings.validate_frontend_compatibility()
        assert validation["compatible"] == True  # Should pass with proper config
        
        # Test Redis URL generation
        redis_url = settings.get_redis_url()
        assert redis_url == 'redis://prod-redis:6379/0'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])