# Configuration Management Abstraction Specification

## Overview
This specification defines the implementation of centralized configuration management to replace scattered environment variable handling throughout the FaultMaven codebase.

## Current State Analysis

### Identified Issues
- **Scattered Configuration**: Direct `os.getenv()` calls in 15+ files
- **No Validation**: Missing configuration validation at startup
- **Inconsistent Defaults**: Different default values across modules  
- **Poor Error Handling**: Silent failures when configuration is invalid
- **No Type Safety**: String-only configuration without type conversion

### Current Problematic Patterns
```python
# Found throughout codebase:
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
ENABLE_TRACING = os.getenv("OPIK_TRACK_DISABLE", "false").lower() == "false"
```

## Technical Requirements

### 1. Configuration Manager Implementation

**File**: `faultmaven/config/configuration_manager.py`

```python
from typing import Dict, List, Optional, Union, Any
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
import os
import logging
from pathlib import Path
import json

from faultmaven.models.interfaces import IConfiguration
from faultmaven.exceptions import ConfigurationException


@dataclass
class ConfigSection:
    """Represents a logical configuration section with validation."""
    name: str
    required_keys: List[str] = field(default_factory=list)
    optional_keys: Dict[str, Any] = field(default_factory=dict)
    validators: Dict[str, callable] = field(default_factory=dict)


class ConfigurationManager(IConfiguration):
    """Centralized configuration management with validation and type safety.
    
    This class replaces scattered os.getenv() calls throughout the codebase
    with a centralized, validated, and type-safe configuration system.
    
    Features:
        - Environment variable loading with validation
        - Type conversion and default value handling
        - Configuration file support (.env, JSON, YAML)
        - Startup validation with detailed error reporting
        - Hot-reload capability for development
        - Configuration documentation generation
    """
    
    def __init__(
        self, 
        config_file: Optional[Path] = None,
        validate_on_init: bool = True,
        allow_missing_optional: bool = True
    ):
        """Initialize configuration manager.
        
        Args:
            config_file: Optional configuration file path (.env, .json, .yaml)
            validate_on_init: Whether to validate configuration at initialization
            allow_missing_optional: Whether missing optional config is acceptable
        """
        self._config_data: Dict[str, Any] = {}
        self._config_sections: List[ConfigSection] = []
        self._validation_errors: List[str] = []
        
        # Load configuration from multiple sources
        self._load_environment_variables()
        if config_file:
            self._load_config_file(config_file)
            
        # Initialize configuration sections
        self._initialize_sections()
        
        if validate_on_init:
            if not self.validate():
                raise ConfigurationException(
                    f"Configuration validation failed: {self._validation_errors}"
                )
    
    def _initialize_sections(self) -> None:
        """Initialize all configuration sections with their requirements."""
        self._config_sections = [
            self._get_database_section(),
            self._get_llm_section(),
            self._get_logging_section(),
            self._get_session_section(),
            self._get_security_section(),
            self._get_observability_section(),
            self._get_performance_section()
        ]
    
    def _get_database_section(self) -> ConfigSection:
        """Database configuration section."""
        return ConfigSection(
            name="database",
            required_keys=["REDIS_HOST"],
            optional_keys={
                "REDIS_PORT": 6379,
                "REDIS_PASSWORD": None,
                "REDIS_DB": 0,
                "REDIS_SSL": False,
                "REDIS_TIMEOUT": 30,
                "CHROMADB_URL": "http://chromadb.faultmaven.local:30080",
                "CHROMADB_API_KEY": None
            },
            validators={
                "REDIS_PORT": lambda x: 1 <= int(x) <= 65535,
                "REDIS_TIMEOUT": lambda x: int(x) > 0,
                "CHROMADB_URL": lambda x: x.startswith(("http://", "https://"))
            }
        )
    
    def _get_llm_section(self) -> ConfigSection:
        """LLM provider configuration section."""
        return ConfigSection(
            name="llm",
            required_keys=["CHAT_PROVIDER"],
            optional_keys={
                "OPENAI_API_KEY": None,
                "OPENAI_MODEL": "gpt-4o",
                "ANTHROPIC_API_KEY": None,
                "ANTHROPIC_MODEL": "claude-3-5-sonnet-20241022",
                "FIREWORKS_API_KEY": None,
                "FIREWORKS_MODEL": "accounts/fireworks/models/llama-v3p1-70b-instruct",
                "LLM_REQUEST_TIMEOUT": 30,
                "LLM_MAX_RETRIES": 3
            },
            validators={
                "CHAT_PROVIDER": lambda x: x in ["openai", "anthropic", "fireworks", "local"],
                "LLM_REQUEST_TIMEOUT": lambda x: 0 < int(x) <= 300,
                "LLM_MAX_RETRIES": lambda x: 0 <= int(x) <= 10
            }
        )
    
    def _get_logging_section(self) -> ConfigSection:
        """Logging configuration section."""
        return ConfigSection(
            name="logging",
            required_keys=[],
            optional_keys={
                "LOG_LEVEL": "INFO",
                "LOG_FORMAT": "json",
                "LOG_DEDUPE": True,
                "LOG_BUFFER_SIZE": 100,
                "LOG_FLUSH_INTERVAL": 5,
                "LOG_FILE_PATH": None,
                "LOG_MAX_FILE_SIZE": "10MB"
            },
            validators={
                "LOG_LEVEL": lambda x: x.upper() in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                "LOG_FORMAT": lambda x: x in ["json", "text"],
                "LOG_BUFFER_SIZE": lambda x: int(x) > 0,
                "LOG_FLUSH_INTERVAL": lambda x: int(x) > 0
            }
        )
    
    def _get_session_section(self) -> ConfigSection:
        """Session management configuration section."""
        return ConfigSection(
            name="session",
            required_keys=[],
            optional_keys={
                "SESSION_TIMEOUT_MINUTES": 30,
                "SESSION_CLEANUP_INTERVAL_MINUTES": 15,
                "SESSION_MAX_MEMORY_MB": 100,
                "SESSION_CLEANUP_BATCH_SIZE": 50,
                "SESSION_ENCRYPTION_KEY": None
            },
            validators={
                "SESSION_TIMEOUT_MINUTES": lambda x: 1 <= int(x) <= 1440,  # 1 min to 24 hours
                "SESSION_CLEANUP_INTERVAL_MINUTES": lambda x: 1 <= int(x) <= 120,
                "SESSION_MAX_MEMORY_MB": lambda x: int(x) > 0,
                "SESSION_CLEANUP_BATCH_SIZE": lambda x: 1 <= int(x) <= 1000
            }
        )
    
    def _get_security_section(self) -> ConfigSection:
        """Security configuration section."""
        return ConfigSection(
            name="security",
            required_keys=[],
            optional_keys={
                "PRESIDIO_ANALYZER_URL": "http://presidio-analyzer.faultmaven.local:30080",
                "PRESIDIO_ANONYMIZER_URL": "http://presidio-anonymizer.faultmaven.local:30080",
                "PII_DETECTION_CONFIDENCE": 0.8,
                "SANITIZATION_ENABLED": True,
                "SANITIZATION_TIMEOUT": 10
            },
            validators={
                "PII_DETECTION_CONFIDENCE": lambda x: 0.0 <= float(x) <= 1.0,
                "SANITIZATION_TIMEOUT": lambda x: int(x) > 0,
                "PRESIDIO_ANALYZER_URL": lambda x: x.startswith(("http://", "https://"))
            }
        )
    
    def _get_observability_section(self) -> ConfigSection:
        """Observability configuration section."""
        return ConfigSection(
            name="observability",
            required_keys=[],
            optional_keys={
                "OPIK_USE_LOCAL": True,
                "OPIK_LOCAL_URL": "http://opik.faultmaven.local:30080",
                "OPIK_PROJECT_NAME": "FaultMaven Development",
                "OPIK_API_KEY": "local-dev-key",
                "OPIK_TRACK_DISABLE": False,
                "TRACING_SAMPLE_RATE": 1.0,
                "METRICS_ENABLED": True
            },
            validators={
                "OPIK_LOCAL_URL": lambda x: x.startswith(("http://", "https://")) if x else True,
                "TRACING_SAMPLE_RATE": lambda x: 0.0 <= float(x) <= 1.0
            }
        )
    
    def _get_performance_section(self) -> ConfigSection:
        """Performance configuration section."""
        return ConfigSection(
            name="performance",
            required_keys=[],
            optional_keys={
                "REQUEST_TIMEOUT": 30,
                "WORKER_POOL_SIZE": 4,
                "MAX_CONCURRENT_REQUESTS": 100,
                "CACHE_TTL_SECONDS": 300,
                "RATE_LIMIT_PER_MINUTE": 60
            },
            validators={
                "REQUEST_TIMEOUT": lambda x: int(x) > 0,
                "WORKER_POOL_SIZE": lambda x: 1 <= int(x) <= 32,
                "MAX_CONCURRENT_REQUESTS": lambda x: int(x) > 0,
                "CACHE_TTL_SECONDS": lambda x: int(x) >= 0
            }
        )

    # IConfiguration interface implementation
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key."""
        return self._config_data.get(key, default)
    
    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer configuration value with type conversion."""
        value = self.get(key, default)
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value with type conversion."""
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return default
    
    def get_str(self, key: str, default: str = "") -> str:
        """Get string configuration value."""
        value = self.get(key, default)
        return str(value) if value is not None else default
    
    def validate(self) -> bool:
        """Validate all configuration sections."""
        self._validation_errors.clear()
        
        for section in self._config_sections:
            self._validate_section(section)
        
        return len(self._validation_errors) == 0
    
    # Specialized configuration getters
    def get_database_config(self) -> Dict[str, Any]:
        """Get database configuration as a dictionary."""
        return {
            "host": self.get_str("REDIS_HOST"),
            "port": self.get_int("REDIS_PORT", 6379),
            "password": self.get_str("REDIS_PASSWORD"),
            "db": self.get_int("REDIS_DB", 0),
            "ssl": self.get_bool("REDIS_SSL", False),
            "timeout": self.get_int("REDIS_TIMEOUT", 30)
        }
    
    def get_llm_config(self) -> Dict[str, str]:
        """Get LLM provider configuration."""
        provider = self.get_str("CHAT_PROVIDER", "openai")
        
        config = {
            "provider": provider,
            "timeout": self.get_int("LLM_REQUEST_TIMEOUT", 30),
            "max_retries": self.get_int("LLM_MAX_RETRIES", 3)
        }
        
        # Add provider-specific configuration
        if provider == "openai":
            config.update({
                "api_key": self.get_str("OPENAI_API_KEY"),
                "model": self.get_str("OPENAI_MODEL", "gpt-4o")
            })
        elif provider == "anthropic":
            config.update({
                "api_key": self.get_str("ANTHROPIC_API_KEY"),
                "model": self.get_str("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
            })
        elif provider == "fireworks":
            config.update({
                "api_key": self.get_str("FIREWORKS_API_KEY"),
                "model": self.get_str("FIREWORKS_MODEL", "accounts/fireworks/models/llama-v3p1-70b-instruct")
            })
            
        return config
    
    def get_logging_config(self) -> Dict[str, Any]:
        """Get logging configuration."""
        return {
            "level": self.get_str("LOG_LEVEL", "INFO").upper(),
            "format": self.get_str("LOG_FORMAT", "json"),
            "dedupe": self.get_bool("LOG_DEDUPE", True),
            "buffer_size": self.get_int("LOG_BUFFER_SIZE", 100),
            "flush_interval": self.get_int("LOG_FLUSH_INTERVAL", 5),
            "file_path": self.get_str("LOG_FILE_PATH"),
            "max_file_size": self.get_str("LOG_MAX_FILE_SIZE", "10MB")
        }
    
    def get_session_config(self) -> Dict[str, Any]:
        """Get session management configuration."""
        return {
            "timeout_minutes": self.get_int("SESSION_TIMEOUT_MINUTES", 30),
            "cleanup_interval_minutes": self.get_int("SESSION_CLEANUP_INTERVAL_MINUTES", 15),
            "max_memory_mb": self.get_int("SESSION_MAX_MEMORY_MB", 100),
            "cleanup_batch_size": self.get_int("SESSION_CLEANUP_BATCH_SIZE", 50),
            "encryption_key": self.get_str("SESSION_ENCRYPTION_KEY")
        }


# Configuration singleton instance
_config_instance: Optional[ConfigurationManager] = None

def get_config() -> ConfigurationManager:
    """Get the global configuration instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigurationManager()
    return _config_instance

def reset_config() -> None:
    """Reset configuration instance (primarily for testing)."""
    global _config_instance
    _config_instance = None
```

### 2. Migration Strategy

#### 2.1 Replace Direct Environment Access

**Before (current problematic pattern):**
```python
# faultmaven/infrastructure/persistence/redis.py
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
```

**After (using configuration manager):**
```python
# faultmaven/infrastructure/persistence/redis.py
from faultmaven.config.configuration_manager import get_config

config = get_config()
db_config = config.get_database_config()
REDIS_HOST = db_config["host"]
REDIS_PORT = db_config["port"]
```

#### 2.2 Application Initialization

**File**: `faultmaven/main.py`

```python
# Early in application startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan with configuration validation."""
    # Startup
    logger.info("Validating configuration...")
    
    # Initialize and validate configuration
    config = get_config()
    if not config.validate():
        logger.error("Configuration validation failed")
        raise ConfigurationException("Invalid configuration")
    
    logger.info("Configuration validated successfully")
    
    # Make configuration available to app
    app.extra["config"] = config
    
    # Rest of existing startup code...
    yield
    
    # Shutdown code...
```

#### 2.3 Dependency Injection Integration

**File**: `faultmaven/container.py`

```python
# Add configuration to DI container
def _create_infrastructure_layer(self):
    """Create infrastructure layer with centralized configuration."""
    config = get_config()
    
    # Use configuration for service initialization
    llm_config = config.get_llm_config()
    self.llm_provider = self._create_llm_provider(llm_config)
    
    db_config = config.get_database_config()
    self.session_store = self._create_session_store(db_config)
```

### 3. Testing Requirements

#### 3.1 Configuration Testing

```python
# tests/unit/test_configuration_manager.py
class TestConfigurationManager:
    def test_initialization_with_valid_config(self):
        """Test configuration manager initialization with valid configuration."""
        
    def test_initialization_with_invalid_config_fails(self):
        """Test that invalid configuration raises exception."""
        
    def test_type_conversion_methods(self):
        """Test get_int, get_bool, get_str type conversion."""
        
    def test_validation_comprehensive(self):
        """Test comprehensive configuration validation."""
        
    def test_section_specific_getters(self):
        """Test database_config, llm_config, etc. methods."""
        
    def test_configuration_file_loading(self):
        """Test loading configuration from files."""
        
    def test_environment_variable_precedence(self):
        """Test that environment variables override file config."""
```

#### 3.2 Migration Testing

```python
# tests/integration/test_configuration_migration.py
class TestConfigurationMigration:
    def test_replaced_environment_access(self):
        """Test that all direct os.getenv calls have been replaced."""
        
    def test_configuration_consistency(self):
        """Test that migrated configuration produces same values."""
        
    def test_application_startup_with_new_config(self):
        """Test application startup with configuration manager."""
```

## Implementation Steps

### Step 1: Core Configuration Manager (Days 1-3)
1. Implement `ConfigurationManager` class with all sections
2. Add type conversion and validation methods
3. Implement configuration file loading support

### Step 2: Replace Environment Access (Days 4-7)
1. Identify all `os.getenv()` calls in codebase
2. Replace with configuration manager calls
3. Update import statements and dependencies

### Step 3: Integration and Testing (Days 8-10)
1. Integrate with DI container
2. Update application startup process
3. Add comprehensive testing

### Step 4: Documentation and Validation (Days 11-14)
1. Update documentation with new configuration patterns
2. Add configuration validation to CI/CD
3. Create configuration migration guide

## Success Criteria

1. **Zero Direct Environment Access**: No `os.getenv()` calls in application code
2. **Startup Validation**: Application fails fast with clear errors for invalid configuration
3. **Type Safety**: All configuration values properly typed and validated
4. **Centralized Management**: Single source of truth for all configuration
5. **Developer Experience**: Clear error messages and configuration documentation

## Benefits

1. **Improved Reliability**: Configuration validation prevents runtime errors
2. **Better Developer Experience**: Clear configuration requirements and validation
3. **Enhanced Security**: Centralized sensitive data handling
4. **Easier Testing**: Configuration can be easily mocked and controlled
5. **Better Documentation**: Auto-generated configuration documentation