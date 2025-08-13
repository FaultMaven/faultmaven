"""
Centralized configuration management.

This module provides a centralized configuration system that implements
the IConfiguration interface for type-safe configuration access.
"""

import os
from typing import Any, Dict, Optional
from dataclasses import dataclass
import logging
from faultmaven.models.interfaces import IConfiguration


@dataclass
class LLMConfig:
    """LLM provider configuration"""
    provider: str
    api_key: Optional[str]
    model: str
    max_tokens: int = 1000
    temperature: float = 0.7


@dataclass
class RedisConfig:
    """Redis configuration"""
    host: str
    port: int
    password: Optional[str]
    db: int = 0


@dataclass
class ChromaDBConfig:
    """ChromaDB configuration"""
    url: str
    api_key: Optional[str]
    collection_name: str = "faultmaven_knowledge"


@dataclass
class PresidioConfig:
    """Presidio configuration"""
    analyzer_url: str
    anonymizer_url: str
    timeout: float = 10.0


@dataclass
class LoggingConfig:
    """Logging configuration"""
    level: str
    format: str
    dedupe: bool
    buffer_size: int
    flush_interval: int


class Config(IConfiguration):
    """Centralized configuration management implementing IConfiguration"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._load_configuration()
            self._initialized = True
    
    def _load_configuration(self):
        """Load configuration from environment variables"""
        self.logger = logging.getLogger(__name__)
        
        # LLM Configuration
        self.llm = LLMConfig(
            provider=os.getenv("CHAT_PROVIDER", "openai"),
            api_key=os.getenv(f"{os.getenv('CHAT_PROVIDER', 'OPENAI').upper()}_API_KEY"),
            model=os.getenv(f"{os.getenv('CHAT_PROVIDER', 'OPENAI').upper()}_MODEL", "gpt-4o"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1000")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7"))
        )
        
        # Redis Configuration
        self.redis = RedisConfig(
            host=os.getenv("REDIS_HOST", "192.168.0.111"),
            port=int(os.getenv("REDIS_PORT", "30379")),
            password=os.getenv("REDIS_PASSWORD"),
            db=int(os.getenv("REDIS_DB", "0"))
        )
        
        # ChromaDB Configuration
        self.chromadb = ChromaDBConfig(
            url=os.getenv("CHROMADB_URL", "http://chromadb.faultmaven.local:30080"),
            api_key=os.getenv("CHROMADB_API_KEY", "faultmaven-dev-chromadb-2025"),
            collection_name=os.getenv("CHROMADB_COLLECTION", "faultmaven_knowledge")
        )
        
        # Presidio Configuration
        self.presidio = PresidioConfig(
            analyzer_url=os.getenv("PRESIDIO_ANALYZER_URL", 
                                  "http://presidio-analyzer.faultmaven.local:30080"),
            anonymizer_url=os.getenv("PRESIDIO_ANONYMIZER_URL",
                                    "http://presidio-anonymizer.faultmaven.local:30080"),
            timeout=float(os.getenv("PRESIDIO_TIMEOUT", "10.0"))
        )
        
        # Logging Configuration
        self.logging = LoggingConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            format=os.getenv("LOG_FORMAT", "json"),
            dedupe=os.getenv("LOG_DEDUPE", "true").lower() == "true",
            buffer_size=int(os.getenv("LOG_BUFFER_SIZE", "100")),
            flush_interval=int(os.getenv("LOG_FLUSH_INTERVAL", "5"))
        )
        
        # Application settings
        self.session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
        self.session_cleanup_interval_minutes = int(os.getenv("SESSION_CLEANUP_INTERVAL_MINUTES", "15"))
        self.session_max_memory_mb = int(os.getenv("SESSION_MAX_MEMORY_MB", "100"))
        self.session_cleanup_batch_size = int(os.getenv("SESSION_CLEANUP_BATCH_SIZE", "50"))
        self.skip_service_checks = os.getenv("SKIP_SERVICE_CHECKS", "false").lower() == "true"
        
        # Environment
        self.environment = os.getenv("ENVIRONMENT", "development")
        
        self.logger.info(f"Configuration loaded for environment: {self.environment}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key using dot notation"""
        keys = key.split('.')
        value = self
        
        for k in keys:
            if hasattr(value, k):
                value = getattr(value, k)
            else:
                return default
        
        return value
    
    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer configuration value"""
        value = self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value"""
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes', 'on')
        return bool(value)
    
    def get_str(self, key: str, default: str = "") -> str:
        """Get string configuration value"""
        value = self.get(key, default)
        return str(value) if value is not None else default
    
    def validate(self) -> bool:
        """Validate configuration completeness"""
        errors = []
        
        # Check required LLM configuration
        if not self.llm.api_key and self.llm.provider != "local":
            errors.append(f"Missing API key for LLM provider: {self.llm.provider}")
        
        # Check Redis connectivity
        if not self.redis.host:
            errors.append("Redis host not configured")
        
        # Check ChromaDB configuration
        if not self.chromadb.url:
            errors.append("ChromaDB URL not configured")
        
        # Check session configuration
        if self.session_timeout_minutes <= 0:
            errors.append("Session timeout must be positive")
        if self.session_cleanup_interval_minutes <= 0:
            errors.append("Session cleanup interval must be positive")
        if self.session_max_memory_mb <= 0:
            errors.append("Session max memory must be positive")
        if self.session_cleanup_batch_size <= 0:
            errors.append("Session cleanup batch size must be positive")
        
        # Log errors
        for error in errors:
            self.logger.error(f"Configuration error: {error}")
        
        return len(errors) == 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Export configuration as dictionary (for debugging)"""
        return {
            'llm': {
                'provider': self.llm.provider,
                'model': self.llm.model,
                'has_api_key': bool(self.llm.api_key)
            },
            'redis': {
                'host': self.redis.host,
                'port': self.redis.port
            },
            'chromadb': {
                'url': self.chromadb.url,
                'collection': self.chromadb.collection_name
            },
            'presidio': {
                'analyzer_url': self.presidio.analyzer_url,
                'anonymizer_url': self.presidio.anonymizer_url
            },
            'logging': {
                'level': self.logging.level,
                'format': self.logging.format,
                'dedupe': self.logging.dedupe
            },
            'environment': self.environment
        }


# Global configuration instance
config = Config()