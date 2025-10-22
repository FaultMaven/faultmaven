"""
Unified Configuration System for FaultMaven

Single source of truth for all configuration using pydantic-settings.
Replaces fragmented config.py and configuration_manager.py approaches.

ARCHITECTURAL PRINCIPLES:
- Only this module accesses environment variables directly
- All other modules receive configuration via dependency injection
- Type-safe validation with automatic conversion
- Frontend compatibility validation built-in
"""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings
from typing import Optional, Literal, List, Dict, Any, Union
from pathlib import Path
import logging
from enum import Enum


# =============================================================================
# ENVIRONMENT AND LOGGING ENUMS
# =============================================================================

class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LLMProvider(str, Enum):
    FIREWORKS = "fireworks"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    COHERE = "cohere"
    LOCAL = "local"
    GROQ = "groq"
    NOT_SET = "NOT_SET"


# =============================================================================
# NESTED CONFIGURATION SECTIONS
# =============================================================================

class ServerSettings(BaseSettings):
    """Core server configuration"""
    host: str = Field(default="0.0.0.0", env="HOST")
    port: int = Field(default=8000, env="PORT")
    reload: bool = Field(default=False, env="RELOAD")
    workers: int = Field(default=1, env="WORKERS")
    
    # Environment and behavior
    environment: Environment = Field(default=Environment.DEVELOPMENT, env="ENVIRONMENT")
    debug: bool = Field(default=False, env="DEBUG")
    skip_service_checks: bool = Field(default=False, env="SKIP_SERVICE_CHECKS")
    
    # Testing configuration
    pytest_current_test: Optional[str] = Field(default=None, env="PYTEST_CURRENT_TEST")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class LLMSettings(BaseSettings):
    """LLM provider configuration with flexible multi-model support"""
    
    # Task-specific provider selection
    provider: LLMProvider = Field(default=LLMProvider.FIREWORKS, alias="CHAT_PROVIDER")
    multimodal_provider: Optional[LLMProvider] = Field(default=None, env="MULTIMODAL_PROVIDER")
    synthesis_provider: Optional[LLMProvider] = Field(default=None, env="SYNTHESIS_PROVIDER")
    classifier_provider: Optional[LLMProvider] = Field(default=None, env="CLASSIFIER_PROVIDER")
    code_provider: Optional[LLMProvider] = Field(default=None, env="CODE_PROVIDER")

    # API Keys (SecretStr for security)
    openai_api_key: Optional[SecretStr] = Field(default=None, env="OPENAI_API_KEY")
    anthropic_api_key: Optional[SecretStr] = Field(default=None, env="ANTHROPIC_API_KEY")
    fireworks_api_key: Optional[SecretStr] = Field(default=None, env="FIREWORKS_API_KEY")
    cohere_api_key: Optional[SecretStr] = Field(default=None, env="COHERE_API_KEY")
    gemini_api_key: Optional[SecretStr] = Field(default=None, env="GEMINI_API_KEY")
    huggingface_api_key: Optional[SecretStr] = Field(default=None, env="HUGGINGFACE_API_KEY")
    openrouter_api_key: Optional[SecretStr] = Field(default=None, env="OPENROUTER_API_KEY")
    groq_api_key: Optional[SecretStr] = Field(default=None, env="GROQ_API_KEY")
    
    # Flexible model configuration per provider and task
    # OpenAI models
    openai_chat_model: str = Field(default="gpt-4o", env="OPENAI_CHAT_MODEL")
    openai_multimodal_model: str = Field(default="gpt-4o-vision", env="OPENAI_MULTIMODAL_MODEL")
    openai_synthesis_model: str = Field(default="gpt-4o-mini", env="OPENAI_SYNTHESIS_MODEL")
    openai_classifier_model: str = Field(default="gpt-4o-mini", env="OPENAI_CLASSIFIER_MODEL")
    openai_code_model: str = Field(default="gpt-4o", env="OPENAI_CODE_MODEL")
    
    # Anthropic models
    anthropic_chat_model: str = Field(default="claude-3-5-sonnet-20241022", env="ANTHROPIC_CHAT_MODEL")
    anthropic_multimodal_model: str = Field(default="claude-3-5-sonnet-20241022", env="ANTHROPIC_MULTIMODAL_MODEL")
    anthropic_synthesis_model: str = Field(default="claude-3-haiku", env="ANTHROPIC_SYNTHESIS_MODEL")
    anthropic_classifier_model: str = Field(default="claude-3-haiku", env="ANTHROPIC_CLASSIFIER_MODEL")
    anthropic_code_model: str = Field(default="claude-3-5-sonnet-20241022", env="ANTHROPIC_CODE_MODEL")
    
    # Fireworks models
    fireworks_chat_model: str = Field(default="accounts/fireworks/models/llama-v3p1-405b-instruct", env="FIREWORKS_CHAT_MODEL")
    fireworks_multimodal_model: str = Field(default="accounts/fireworks/models/llama-v3p1-405b-instruct", env="FIREWORKS_MULTIMODAL_MODEL")
    fireworks_synthesis_model: str = Field(default="accounts/fireworks/models/llama-v3p1-405b-instruct", env="FIREWORKS_SYNTHESIS_MODEL")
    fireworks_classifier_model: str = Field(default="accounts/fireworks/models/llama-v3p1-405b-instruct", env="FIREWORKS_CLASSIFIER_MODEL")
    fireworks_code_model: str = Field(default="accounts/fireworks/models/qwen3-coder-480b-a35b-instruct", env="FIREWORKS_CODE_MODEL")
    
    # Google Gemini models
    gemini_chat_model: str = Field(default="gemini-1.5-pro", env="GEMINI_CHAT_MODEL")
    gemini_multimodal_model: str = Field(default="gemini-1.5-pro", env="GEMINI_MULTIMODAL_MODEL")
    gemini_synthesis_model: str = Field(default="gemini-1.5-flash", env="GEMINI_SYNTHESIS_MODEL")
    gemini_classifier_model: str = Field(default="gemini-1.5-flash", env="GEMINI_CLASSIFIER_MODEL")
    gemini_code_model: str = Field(default="gemini-1.5-pro", env="GEMINI_CODE_MODEL")
    
    # Cohere models
    cohere_chat_model: str = Field(default="command-r-plus", env="COHERE_CHAT_MODEL")
    cohere_multimodal_model: str = Field(default="command-r-plus", env="COHERE_MULTIMODAL_MODEL")
    cohere_synthesis_model: str = Field(default="command-r-plus", env="COHERE_SYNTHESIS_MODEL")
    cohere_classifier_model: str = Field(default="command-r-plus", env="COHERE_CLASSIFIER_MODEL")
    cohere_code_model: str = Field(default="command-r-plus", env="COHERE_CODE_MODEL")
    
    # HuggingFace models
    huggingface_chat_model: str = Field(default="tiiuae/falcon-7b-instruct", env="HUGGINGFACE_CHAT_MODEL")
    huggingface_multimodal_model: str = Field(default="tiiuae/falcon-7b-instruct", env="HUGGINGFACE_MULTIMODAL_MODEL")
    huggingface_synthesis_model: str = Field(default="tiiuae/falcon-7b-instruct", env="HUGGINGFACE_SYNTHESIS_MODEL")
    huggingface_classifier_model: str = Field(default="tiiuae/falcon-7b-instruct", env="HUGGINGFACE_CLASSIFIER_MODEL")
    huggingface_code_model: str = Field(default="tiiuae/falcon-7b-instruct", env="HUGGINGFACE_CODE_MODEL")
    
    # OpenRouter models
    openrouter_chat_model: str = Field(default="openrouter-default", env="OPENROUTER_CHAT_MODEL")
    openrouter_multimodal_model: str = Field(default="openrouter-default", env="OPENROUTER_MULTIMODAL_MODEL")
    openrouter_synthesis_model: str = Field(default="openrouter-default", env="OPENROUTER_SYNTHESIS_MODEL")
    openrouter_classifier_model: str = Field(default="openrouter-default", env="OPENROUTER_CLASSIFIER_MODEL")
    openrouter_code_model: str = Field(default="openrouter-default", env="OPENROUTER_CODE_MODEL")
    
    # Groq models
    groq_chat_model: str = Field(default="meta-llama/Llama-4-Scout-17B-16E-Instruct", env="GROQ_CHAT_MODEL")
    groq_multimodal_model: str = Field(default="meta-llama/Llama-4-Scout-17B-16E-Instruct", env="GROQ_MULTIMODAL_MODEL")
    groq_synthesis_model: str = Field(default="meta-llama/Llama-4-Scout-17B-16E-Instruct", env="GROQ_SYNTHESIS_MODEL")
    groq_classifier_model: str = Field(default="meta-llama/Llama-4-Scout-17B-16E-Instruct", env="GROQ_CLASSIFIER_MODEL")
    groq_code_model: str = Field(default="meta-llama/Llama-4-Scout-17B-16E-Instruct", env="GROQ_CODE_MODEL")
    
    # Legacy model configuration (backward compatibility)
    openai_model: str = Field(default="gpt-4o", env="OPENAI_MODEL")
    anthropic_model: str = Field(default="claude-3-sonnet-20240229", env="ANTHROPIC_MODEL")
    fireworks_model: str = Field(default="accounts/fireworks/models/llama-v3p1-405b-instruct", env="FIREWORKS_MODEL")
    cohere_model: str = Field(default="command-r-plus", env="COHERE_MODEL")
    gemini_model: str = Field(default="gemini-1.5-pro", env="GEMINI_MODEL")
    huggingface_model: str = Field(default="tiiuae/falcon-7b-instruct", env="HUGGINGFACE_MODEL")
    openrouter_model: str = Field(default="openrouter-default", env="OPENROUTER_MODEL")
    
    # Local provider configuration
    local_url: Optional[str] = Field(default=None, alias="LOCAL_LLM_URL")
    local_model: Optional[str] = Field(default=None, alias="LOCAL_LLM_MODEL")
    
    # Base URLs for each provider
    openai_base_url: str = Field(default="https://api.openai.com/v1", env="OPENAI_API_BASE")
    anthropic_base_url: str = Field(default="https://api.anthropic.com/v1", env="ANTHROPIC_API_BASE")
    fireworks_base_url: str = Field(default="https://api.fireworks.ai/inference/v1", env="FIREWORKS_API_BASE")
    cohere_base_url: str = Field(default="https://api.cohere.ai/v1", env="COHERE_API_BASE")
    gemini_base_url: str = Field(default="https://generativelanguage.googleapis.com/v1beta", env="GEMINI_API_BASE")
    huggingface_base_url: str = Field(default="https://api-inference.huggingface.co/models", env="HUGGINGFACE_API_URL")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1", env="OPENROUTER_API_BASE")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1", env="GROQ_API_BASE")
    
    # Request configuration
    request_timeout: int = Field(default=30, env="LLM_REQUEST_TIMEOUT")
    max_retries: int = Field(default=3, env="LLM_MAX_RETRIES")
    retry_delay: float = Field(default=1.0, env="LLM_RETRY_DELAY")
    
    # Token limits
    max_tokens: int = Field(default=4096, env="LLM_MAX_TOKENS")
    context_window: int = Field(default=128000, env="LLM_CONTEXT_WINDOW")
    
    # Phase/Tool response limits (separate from provider limits)
    phase_response_max_tokens: int = Field(
        default=2000,
        env="LLM_PHASE_RESPONSE_MAX_TOKENS",
        ge=500,
        le=4096,
        description="Maximum tokens for phase handler and tool responses (OODA structure needs 400-1500 tokens)"
    )
    
    @field_validator('max_tokens')
    @classmethod
    def validate_max_tokens(cls, v, info):
        """Ensure max_tokens is reasonable and within context window"""
        if v < 100:
            raise ValueError("LLM_MAX_TOKENS must be >= 100 for useful responses")
        
        values = info.data
        context_window = values.get('context_window', 128000)
        if v > context_window:
            raise ValueError(
                f"LLM_MAX_TOKENS ({v}) cannot exceed LLM_CONTEXT_WINDOW ({context_window})"
            )
        return v
    
    def get_api_key(self) -> Optional[str]:
        """Get API key for current provider"""
        key_map = {
            LLMProvider.OPENAI: self.openai_api_key,
            LLMProvider.ANTHROPIC: self.anthropic_api_key,
            LLMProvider.FIREWORKS: self.fireworks_api_key,
            LLMProvider.COHERE: self.cohere_api_key,
            LLMProvider.LOCAL: None,  # Local doesn't use API key
        }
        key = key_map.get(self.provider)
        return key.get_secret_value() if key else None
    
    def get_model(self, task: str = "chat") -> str:
        """Get model for current provider and specific task
        
        Args:
            task: Task type ('chat', 'multimodal', 'synthesis', 'classifier', 'code')
        """
        provider = self.provider
        model_map = {
            LLMProvider.OPENAI: {
                "chat": self.openai_chat_model,
                "multimodal": self.openai_multimodal_model,
                "synthesis": self.openai_synthesis_model,
                "classifier": self.openai_classifier_model,
                "code": self.openai_code_model,
            },
            LLMProvider.ANTHROPIC: {
                "chat": self.anthropic_chat_model,
                "multimodal": self.anthropic_multimodal_model,
                "synthesis": self.anthropic_synthesis_model,
                "classifier": self.anthropic_classifier_model,
                "code": self.anthropic_code_model,
            },
            LLMProvider.FIREWORKS: {
                "chat": self.fireworks_chat_model,
                "multimodal": self.fireworks_multimodal_model,
                "synthesis": self.fireworks_synthesis_model,
                "classifier": self.fireworks_classifier_model,
                "code": self.fireworks_code_model,
            },
            LLMProvider.COHERE: {
                "chat": self.cohere_chat_model,
                "multimodal": self.cohere_multimodal_model,
                "synthesis": self.cohere_synthesis_model,
                "classifier": self.cohere_classifier_model,
                "code": self.cohere_code_model,
            },
            LLMProvider.GEMINI: {
                "chat": self.gemini_chat_model,
                "multimodal": self.gemini_multimodal_model,
                "synthesis": self.gemini_synthesis_model,
                "classifier": self.gemini_classifier_model,
                "code": self.gemini_code_model,
            },
            LLMProvider.HUGGINGFACE: {
                "chat": self.huggingface_chat_model,
                "multimodal": self.huggingface_multimodal_model,
                "synthesis": self.huggingface_synthesis_model,
                "classifier": self.huggingface_classifier_model,
                "code": self.huggingface_code_model,
            },
            LLMProvider.OPENROUTER: {
                "chat": self.openrouter_chat_model,
                "multimodal": self.openrouter_multimodal_model,
                "synthesis": self.openrouter_synthesis_model,
                "classifier": self.openrouter_classifier_model,
                "code": self.openrouter_code_model,
            },
            LLMProvider.GROQ: {
                "chat": self.groq_chat_model,
                "multimodal": self.groq_multimodal_model,
                "synthesis": self.groq_synthesis_model,
                "classifier": self.groq_classifier_model,
                "code": self.groq_code_model,
            },
            LLMProvider.LOCAL: {
                "chat": self.local_model,
                "multimodal": self.local_model,
                "synthesis": self.local_model,
                "classifier": self.local_model,
                "code": self.local_model,
            },
        }
        
        provider_models = model_map.get(provider, {})
        return provider_models.get(task, "")

    def get_multimodal_provider(self) -> LLMProvider:
        """Get multimodal provider (falls back to chat provider if not set)"""
        return self.multimodal_provider or self.provider

    def get_multimodal_api_key(self) -> Optional[str]:
        """Get API key for multimodal provider"""
        provider = self.get_multimodal_provider()
        key_map = {
            LLMProvider.OPENAI: self.openai_api_key,
            LLMProvider.ANTHROPIC: self.anthropic_api_key,
            LLMProvider.FIREWORKS: self.fireworks_api_key,
            LLMProvider.COHERE: self.cohere_api_key,
            LLMProvider.LOCAL: None,
        }
        key = key_map.get(provider)
        return key.get_secret_value() if key else None

    def get_multimodal_model(self) -> str:
        """Get model for multimodal provider using task-specific configuration"""
        provider = self.get_multimodal_provider()
        return self._get_model_for_provider_and_task(provider, "multimodal")
    
    def get_synthesis_provider(self) -> LLMProvider:
        """Get synthesis provider for QA sub-agent (falls back to chat provider if not set)"""
        return self.synthesis_provider or self.provider
    
    def get_synthesis_model(self) -> str:
        """Get model for synthesis provider using task-specific configuration"""
        provider = self.get_synthesis_provider()
        return self._get_model_for_provider_and_task(provider, "synthesis")
    
    def get_classifier_provider(self) -> LLMProvider:
        """Get classifier provider (falls back to chat provider if not set)"""
        return self.classifier_provider or self.provider
    
    def get_classifier_model(self) -> str:
        """Get model for classifier provider using task-specific configuration"""
        provider = self.get_classifier_provider()
        return self._get_model_for_provider_and_task(provider, "classifier")
    
    def get_code_provider(self) -> LLMProvider:
        """Get code analysis provider (falls back to chat provider if not set)"""
        return self.code_provider or self.provider
    
    def get_code_model(self) -> str:
        """Get model for code analysis provider using task-specific configuration"""
        provider = self.get_code_provider()
        return self._get_model_for_provider_and_task(provider, "code")
    
    def _get_model_for_provider_and_task(self, provider: LLMProvider, task: str) -> str:
        """Helper method to get model for any provider and task combination"""
        model_map = {
            LLMProvider.OPENAI: {
                "chat": self.openai_chat_model,
                "multimodal": self.openai_multimodal_model,
                "synthesis": self.openai_synthesis_model,
                "classifier": self.openai_classifier_model,
                "code": self.openai_code_model,
            },
            LLMProvider.ANTHROPIC: {
                "chat": self.anthropic_chat_model,
                "multimodal": self.anthropic_multimodal_model,
                "synthesis": self.anthropic_synthesis_model,
                "classifier": self.anthropic_classifier_model,
                "code": self.anthropic_code_model,
            },
            LLMProvider.FIREWORKS: {
                "chat": self.fireworks_chat_model,
                "multimodal": self.fireworks_multimodal_model,
                "synthesis": self.fireworks_synthesis_model,
                "classifier": self.fireworks_classifier_model,
                "code": self.fireworks_code_model,
            },
            LLMProvider.COHERE: {
                "chat": self.cohere_chat_model,
                "multimodal": self.cohere_multimodal_model,
                "synthesis": self.cohere_synthesis_model,
                "classifier": self.cohere_classifier_model,
                "code": self.cohere_code_model,
            },
            LLMProvider.GEMINI: {
                "chat": self.gemini_chat_model,
                "multimodal": self.gemini_multimodal_model,
                "synthesis": self.gemini_synthesis_model,
                "classifier": self.gemini_classifier_model,
                "code": self.gemini_code_model,
            },
            LLMProvider.HUGGINGFACE: {
            LLMProvider.GROQ: {
                "chat": self.groq_chat_model,
                "multimodal": self.groq_multimodal_model,
                "synthesis": self.groq_synthesis_model,
                "classifier": self.groq_classifier_model,
                "code": self.groq_code_model,
            },
                "chat": self.huggingface_chat_model,
                "multimodal": self.huggingface_multimodal_model,
                "synthesis": self.huggingface_synthesis_model,
                "classifier": self.huggingface_classifier_model,
                "code": self.huggingface_code_model,
            },
            LLMProvider.OPENROUTER: {
                "chat": self.openrouter_chat_model,
                "multimodal": self.openrouter_multimodal_model,
                "synthesis": self.openrouter_synthesis_model,
                "classifier": self.openrouter_classifier_model,
                "code": self.openrouter_code_model,
            },
            LLMProvider.LOCAL: {
                "chat": self.local_model,
                "multimodal": self.local_model,
                "synthesis": self.local_model,
                "classifier": self.local_model,
                "code": self.local_model,
            },
        }
        
        provider_models = model_map.get(provider, {})
        return provider_models.get(task, "")

    def get_multimodal_base_url(self) -> str:
        """Get base URL for multimodal provider"""
        provider = self.get_multimodal_provider()
        url_map = {
            LLMProvider.OPENAI: self.openai_base_url,
            LLMProvider.ANTHROPIC: self.anthropic_base_url,
            LLMProvider.FIREWORKS: self.fireworks_base_url,
            LLMProvider.COHERE: self.cohere_base_url,
            LLMProvider.LOCAL: self.local_url,
        }
        return url_map.get(provider, "")

    def get_synthesis_api_key(self) -> Optional[str]:
        """Get API key for synthesis provider"""
        provider = self.get_synthesis_provider()
        key_map = {
            LLMProvider.OPENAI: self.openai_api_key,
            LLMProvider.ANTHROPIC: self.anthropic_api_key,
            LLMProvider.FIREWORKS: self.fireworks_api_key,
            LLMProvider.COHERE: self.cohere_api_key,
            LLMProvider.GEMINI: self.gemini_api_key,
            LLMProvider.HUGGINGFACE: self.huggingface_api_key,
            LLMProvider.OPENROUTER: self.openrouter_api_key,
            LLMProvider.GROQ: self.groq_api_key,
            LLMProvider.LOCAL: None,
        }
        key = key_map.get(provider)
        return key.get_secret_value() if key else None

    def get_synthesis_base_url(self) -> str:
        """Get base URL for synthesis provider"""
        provider = self.get_synthesis_provider()
        url_map = {
            LLMProvider.OPENAI: self.openai_base_url,
            LLMProvider.ANTHROPIC: self.anthropic_base_url,
            LLMProvider.FIREWORKS: self.fireworks_base_url,
            LLMProvider.COHERE: self.cohere_base_url,
            LLMProvider.GEMINI: self.gemini_base_url,
            LLMProvider.HUGGINGFACE: self.huggingface_base_url,
            LLMProvider.OPENROUTER: self.openrouter_base_url,
            LLMProvider.GROQ: self.groq_base_url,
            LLMProvider.LOCAL: self.local_url,
        }
        return url_map.get(provider, "")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "env_prefix": "",
        "extra": "ignore"
    }


class DatabaseSettings(BaseSettings):
    """Unified database and persistence configuration"""
    
    # Redis Configuration (K8s NodePort for TCP)
    redis_host: str = Field(default="192.168.0.111", env="REDIS_HOST")
    redis_port: int = Field(default=30379, env="REDIS_PORT")
    redis_db: int = Field(default=0, env="REDIS_DB")
    redis_password: Optional[SecretStr] = Field(default=None, env="REDIS_PASSWORD")
    redis_url: Optional[str] = Field(default=None, env="REDIS_URL")
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore"
    }
    
    # ChromaDB Configuration (K8s Ingress for HTTP)
    chromadb_host: str = Field(default="chromadb.faultmaven.local", env="CHROMADB_HOST")
    chromadb_port: int = Field(default=30080, env="CHROMADB_PORT")
    chromadb_url: str = Field(default="http://chromadb.faultmaven.local:30080", env="CHROMADB_URL")
    chromadb_api_key: Optional[SecretStr] = Field(default=None, env="CHROMADB_API_KEY")
    
    # ChromaDB Extended Configuration (merged from EnhancedDatabaseSettings)
    chromadb_auth_token: Optional[SecretStr] = Field(default=None, env="CHROMADB_AUTH_TOKEN")
    chromadb_collection: str = Field(default="faultmaven_kb", env="CHROMADB_COLLECTION")
    
    # Vector Database Settings
    embedding_model: str = Field(default="BAAI/bge-m3", env="EMBEDDING_MODEL")
    similarity_threshold: float = Field(default=0.7, env="SIMILARITY_THRESHOLD")
    max_search_results: int = Field(default=10, env="MAX_SEARCH_RESULTS")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class SessionSettings(BaseSettings):
    """Session management configuration"""
    timeout_minutes: int = Field(default=30, env="SESSION_TIMEOUT_MINUTES", ge=1, le=1440)
    cleanup_interval_minutes: int = Field(default=15, env="SESSION_CLEANUP_INTERVAL_MINUTES")
    max_memory_mb: int = Field(default=100, env="SESSION_MAX_MEMORY_MB")
    heartbeat_interval_seconds: int = Field(default=30, env="SESSION_HEARTBEAT_INTERVAL_SECONDS")
    max_sessions_per_user: int = Field(default=10, env="MAX_SESSIONS_PER_USER")
    
    @field_validator('heartbeat_interval_seconds')
    @classmethod
    def validate_heartbeat_vs_timeout(cls, v, info):
        """Ensure heartbeat is less than timeout for frontend compatibility"""
        values = info.data
        timeout_seconds = values.get('timeout_minutes', 30) * 60
        if v >= timeout_seconds:
            raise ValueError(f"Heartbeat interval ({v}s) must be less than session timeout ({timeout_seconds}s)")
        return v
    
    @field_validator('cleanup_interval_minutes')
    @classmethod
    def validate_cleanup_interval(cls, v, info):
        """Ensure cleanup interval is reasonable vs timeout"""
        values = info.data
        timeout = values.get('timeout_minutes', 180)
        
        if v > timeout:
            raise ValueError(
                f"SESSION_CLEANUP_INTERVAL_MINUTES ({v}) should not exceed "
                f"SESSION_TIMEOUT_MINUTES ({timeout}). "
                f"Cleanup should run at least as often as session expiration."
            )
        return v
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class SecuritySettings(BaseSettings):
    """Security and authentication configuration"""
    # JWT configuration
    jwt_secret_key: Optional[SecretStr] = Field(default=None, env="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", env="JWT_ALGORITHM")
    jwt_expiration_hours: int = Field(default=24, env="JWT_EXPIRATION_HOURS")
    
    # CORS configuration
    cors_allow_credentials: bool = Field(default=True, env="CORS_ALLOW_CREDENTIALS")
    cors_allow_origins: List[str] = Field(
        default=["http://localhost:3000", "chrome-extension://*", "moz-extension://*"],
        env="CORS_ALLOW_ORIGINS"
    )
    cors_expose_headers: List[str] = Field(
        default=[
            "Location", "X-Total-Count", "Link", "Deprecation", "Sunset",
            "X-Request-ID", "Retry-After", "X-RateLimit-Remaining"
        ],
        env="CORS_EXPOSE_HEADERS"
    )
    
    # Rate limiting
    rate_limit_enabled: bool = Field(default=True, env="RATE_LIMIT_ENABLED")
    rate_limit_requests_per_minute: int = Field(default=60, env="RATE_LIMIT_REQUESTS_PER_MINUTE")
    rate_limit_burst_size: int = Field(default=10, env="RATE_LIMIT_BURST_SIZE")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class ProtectionSettings(BaseSettings):
    """Unified protection configuration - PII, behavioral, and ML protection"""

    # Basic Protection Control
    protection_enabled: bool = Field(default=True, env="PROTECTION_ENABLED")
    fail_open: bool = Field(default=True, env="PROTECTION_FAIL_OPEN")
    basic_protection_enabled: bool = Field(default=True, env="BASIC_PROTECTION_ENABLED")
    intelligent_protection_enabled: bool = Field(default=True, env="INTELLIGENT_PROTECTION_ENABLED")

    # PII Sanitization Control
    # When True: Always sanitize PII before sending to LLM (safer, recommended for external LLMs)
    # When False: Skip PII sanitization (only use with local/self-hosted LLMs)
    # Note: This affects data sent to LLM providers. Disable only if using LOCAL provider
    #       or if you trust your external LLM provider with sensitive data.
    sanitize_pii: bool = Field(default=True, env="SANITIZE_PII")

    # Auto-detect: Only sanitize when using external LLM providers
    # When True: Automatically disable sanitization for LOCAL provider, enable for others
    # When False: Use sanitize_pii setting regardless of provider
    auto_sanitize_based_on_provider: bool = Field(default=True, env="AUTO_SANITIZE_BASED_ON_PROVIDER")
    
    # Presidio Configuration (K8s Ingress-based to avoid port conflicts)
    presidio_analyzer_url: str = Field(default="http://presidio-analyzer.faultmaven.local:30080", env="PRESIDIO_ANALYZER_URL")
    presidio_anonymizer_url: str = Field(default="http://presidio-anonymizer.faultmaven.local:30080", env="PRESIDIO_ANONYMIZER_URL")
    
    # PII Protection Settings
    min_score_threshold: float = Field(default=0.8, env="MIN_SCORE_THRESHOLD")
    supported_languages: List[str] = Field(default=["en"], env="SUPPORTED_LANGUAGES")
    entities_to_protect: List[str] = Field(
        default=[
            "CREDIT_CARD", "CRYPTO", "DATE_TIME", "EMAIL_ADDRESS",
            "IBAN_CODE", "IP_ADDRESS", "NRP", "LOCATION", "PERSON",
            "PHONE_NUMBER", "MEDICAL_LICENSE", "URL", "US_BANK_NUMBER",
            "US_DRIVER_LICENSE", "US_ITIN", "US_PASSPORT", "US_SSN"
        ],
        env="ENTITIES_TO_PROTECT"
    )
    
    # Behavioral Analysis (merged from EnhancedProtectionSettings)
    behavioral_analysis_enabled: bool = Field(default=True, env="BEHAVIORAL_ANALYSIS_ENABLED")
    behavior_analysis_window: int = Field(default=3600, env="BEHAVIOR_ANALYSIS_WINDOW")
    behavior_pattern_threshold: float = Field(default=0.8, env="BEHAVIOR_PATTERN_THRESHOLD")
    
    # ML Anomaly Detection (merged from EnhancedProtectionSettings)
    ml_anomaly_detection_enabled: bool = Field(default=True, env="ML_ANOMALY_DETECTION_ENABLED")
    ml_model_path: str = Field(default="/tmp/faultmaven_ml", env="ML_MODEL_PATH")
    ml_training_enabled: bool = Field(default=True, env="ML_TRAINING_ENABLED")
    ml_online_learning_enabled: bool = Field(default=True, env="ML_ONLINE_LEARNING_ENABLED")
    
    # Circuit Breaker (merged from EnhancedProtectionSettings)
    smart_circuit_breakers_enabled: bool = Field(default=True, env="SMART_CIRCUIT_BREAKERS_ENABLED")
    circuit_failure_threshold: int = Field(default=5, env="CIRCUIT_FAILURE_THRESHOLD")
    circuit_timeout_seconds: int = Field(default=60, env="CIRCUIT_TIMEOUT_SECONDS")
    
    # Reputation System (merged from EnhancedProtectionSettings)
    reputation_system_enabled: bool = Field(default=True, env="REPUTATION_SYSTEM_ENABLED")
    reputation_decay_rate: float = Field(default=0.05, env="REPUTATION_DECAY_RATE")
    reputation_recovery_threshold: float = Field(default=0.1, env="REPUTATION_RECOVERY_THRESHOLD")
    
    # Monitoring Intervals (merged from EnhancedProtectionSettings)
    protection_monitoring_interval: int = Field(default=300, env="PROTECTION_MONITORING_INTERVAL")
    protection_cleanup_interval: int = Field(default=3600, env="PROTECTION_CLEANUP_INTERVAL")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class ObservabilitySettings(BaseSettings):
    """Unified observability and monitoring configuration"""
    # Core Opik configuration
    opik_project_name: str = Field(default="faultmaven", env="OPIK_PROJECT_NAME")
    opik_url_override: Optional[str] = Field(default=None, env="OPIK_URL_OVERRIDE")
    opik_use_local: bool = Field(default=False, env="OPIK_USE_LOCAL")
    opik_local_url: str = Field(default="http://localhost:3001", env="OPIK_LOCAL_URL")
    opik_local_host: str = Field(default="opik-api.faultmaven.local", env="OPIK_LOCAL_HOST")
    
    # Opik API and tracking controls (merged from EnhancedObservabilitySettings)
    opik_api_key: Optional[SecretStr] = Field(default=None, env="OPIK_API_KEY")
    opik_enabled: bool = Field(default=True, env="OPIK_ENABLED")
    opik_track_disable: bool = Field(default=False, env="OPIK_TRACK_DISABLE")
    opik_track_users: str = Field(default="", env="OPIK_TRACK_USERS")
    opik_track_sessions: str = Field(default="", env="OPIK_TRACK_SESSIONS")
    opik_track_operations: str = Field(default="", env="OPIK_TRACK_OPERATIONS")
    
    # APM Integration (merged from EnhancedObservabilitySettings)
    prometheus_enabled: bool = Field(default=False, env="PROMETHEUS_ENABLED")
    prometheus_pushgateway_url: str = Field(default="http://localhost:9091", env="PROMETHEUS_PUSHGATEWAY_URL")
    generic_apm_enabled: bool = Field(default=False, env="GENERIC_APM_ENABLED")
    generic_apm_url: Optional[str] = Field(default=None, env="GENERIC_APM_URL")
    generic_apm_api_key: Optional[SecretStr] = Field(default=None, env="GENERIC_APM_API_KEY")
    
    # Workspace integration (merged from WorkspaceSettings)
    comet_workspace: Optional[str] = Field(default=None, env="COMET_WORKSPACE")
    instance_id: str = Field(default="localhost:8000", env="INSTANCE_ID")
    
    # Performance monitoring (merged from EnhancedObservabilitySettings)
    enable_performance_monitoring: bool = Field(default=True, env="ENABLE_PERFORMANCE_MONITORING")
    enable_detailed_tracing: bool = Field(default=False, env="ENABLE_DETAILED_TRACING")
    
    # Basic tracing configuration
    tracing_enabled: bool = Field(default=True, env="TRACING_ENABLED")
    trace_llm_calls: bool = Field(default=True, env="TRACE_LLM_CALLS")
    trace_agent_workflows: bool = Field(default=True, env="TRACE_AGENT_WORKFLOWS")
    
    # Metrics
    metrics_enabled: bool = Field(default=True, env="METRICS_ENABLED")
    metrics_port: int = Field(default=9090, env="METRICS_PORT")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class LoggingSettings(BaseSettings):
    """Logging configuration"""
    level: LogLevel = Field(default=LogLevel.INFO, env="LOG_LEVEL")
    format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        env="LOG_FORMAT"
    )
    
    # File logging
    log_to_file: bool = Field(default=False, env="LOG_TO_FILE")
    log_file_path: str = Field(default="logs/faultmaven.log", env="LOG_FILE_PATH")
    log_file_max_bytes: int = Field(default=10*1024*1024, env="LOG_FILE_MAX_BYTES")  # 10MB
    log_file_backup_count: int = Field(default=5, env="LOG_FILE_BACKUP_COUNT")
    
    # Structured logging
    structured_logging: bool = Field(default=True, env="STRUCTURED_LOGGING")
    include_trace_id: bool = Field(default=True, env="INCLUDE_TRACE_ID")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class UploadSettings(BaseSettings):
    """File upload configuration"""
    max_file_size_mb: int = Field(default=50, env="MAX_FILE_SIZE_MB")
    allowed_mime_types: List[str] = Field(
        default=[
            "text/plain", "text/csv", "application/json",
            "application/xml", "text/xml", "application/yaml"
        ],
        env="ALLOWED_MIME_TYPES"
    )
    upload_timeout_seconds: int = Field(default=300, env="UPLOAD_TIMEOUT_SECONDS")  # 5 minutes
    temp_storage_path: str = Field(default="/tmp/faultmaven", env="TEMP_STORAGE_PATH")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class KnowledgeSettings(BaseSettings):
    """Knowledge base and search configuration"""
    enable_web_search: bool = Field(default=True, env="ENABLE_WEB_SEARCH")
    serp_api_key: Optional[SecretStr] = Field(default=None, env="SERP_API_KEY")
    tavily_api_key: Optional[SecretStr] = Field(default=None, env="TAVILY_API_KEY")
    
    # Search limits
    max_search_results: int = Field(default=5, env="KNOWLEDGE_MAX_SEARCH_RESULTS")
    search_timeout_seconds: int = Field(default=30, env="SEARCH_TIMEOUT_SECONDS")
    
    # Document processing
    max_document_size_mb: int = Field(default=10, env="MAX_DOCUMENT_SIZE_MB")
    chunk_size: int = Field(default=1000, env="DOCUMENT_CHUNK_SIZE")
    chunk_overlap: int = Field(default=100, env="DOCUMENT_CHUNK_OVERLAP")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


class OODASettings(BaseSettings):
    """OODA Investigation Framework configuration (v3.2.0)

    Controls the behavior of the OODA (Observe-Orient-Decide-Act) investigation
    framework including engagement modes, phase management, and memory hierarchy.

    Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
    """

    # Investigation Strategy
    default_strategy: str = Field(
        default="active_incident",
        env="DEFAULT_INVESTIGATION_STRATEGY",
        description="active_incident (fast, 70% confidence) or post_mortem (thorough, 85% confidence)"
    )

    default_intensity: str = Field(
        default="medium",
        env="DEFAULT_OODA_INTENSITY",
        description="OODA cycle intensity: light (1-2 iterations), medium (2-4), full (3-6)"
    )

    # Memory Management (4-Tier Hierarchical System)
    hot_memory_tokens: int = Field(
        default=500,
        env="HOT_MEMORY_TOKENS",
        description="Hot tier: last 2 iterations, full fidelity"
    )

    warm_memory_tokens: int = Field(
        default=300,
        env="WARM_MEMORY_TOKENS",
        description="Warm tier: iterations 3-5, LLM-summarized"
    )

    cold_memory_tokens: int = Field(
        default=100,
        env="COLD_MEMORY_TOKENS",
        description="Cold tier: older iterations, key facts only"
    )

    persistent_memory_tokens: int = Field(
        default=100,
        env="PERSISTENT_MEMORY_TOKENS",
        description="Persistent tier: always accessible insights"
    )

    # Phase Control
    enable_phase_skip: bool = Field(
        default=True,
        env="ENABLE_PHASE_SKIP",
        description="Allow skipping phases in active incident strategy"
    )

    min_confidence_to_advance: float = Field(
        default=0.70,
        env="MIN_CONFIDENCE_TO_ADVANCE",
        ge=0.0,
        le=1.0,
        description="Minimum confidence required to advance to next phase"
    )

    stall_detection_iterations: int = Field(
        default=3,
        env="STALL_DETECTION_ITERATIONS",
        ge=2,
        description="Number of iterations without progress before marking as stalled"
    )

    # Consultant Mode Settings
    problem_signal_threshold: str = Field(
        default="moderate",
        env="PROBLEM_SIGNAL_THRESHOLD",
        description="Threshold to offer investigation: weak|moderate|strong"
    )

    max_consultant_turns: int = Field(
        default=5,
        env="MAX_CONSULTANT_TURNS",
        ge=1,
        description="Max turns in Consultant mode before suggesting Lead Investigator"
    )

    # Context Management (merged from ConversationThresholds)
    max_conversation_turns: int = Field(default=20, env="MAX_CONVERSATION_TURNS")
    max_conversation_tokens: int = Field(default=4000, env="MAX_CONVERSATION_TOKENS")

    @field_validator('warm_memory_tokens')
    @classmethod
    def validate_memory_hierarchy_warm(cls, v, info):
        """Ensure WARM <= HOT for memory hierarchy"""
        values = info.data
        hot = values.get('hot_memory_tokens', 500)
        
        if v > hot:
            raise ValueError(
                f"WARM_MEMORY_TOKENS ({v}) cannot exceed HOT_MEMORY_TOKENS ({hot}). "
                f"Memory quality degrades from hot to cold."
            )
        return v
    
    @field_validator('cold_memory_tokens')
    @classmethod
    def validate_memory_hierarchy_cold(cls, v, info):
        """Ensure COLD <= WARM for memory hierarchy"""
        values = info.data
        warm = values.get('warm_memory_tokens', 300)
        
        if v > warm:
            raise ValueError(
                f"COLD_MEMORY_TOKENS ({v}) cannot exceed WARM_MEMORY_TOKENS ({warm}). "
                f"Memory quality degrades from hot to cold."
            )
        
        if v <= 0:
            raise ValueError(f"COLD_MEMORY_TOKENS must be > 0, got {v}")
        
        return v
    
    @field_validator('persistent_memory_tokens')
    @classmethod
    def validate_persistent_memory(cls, v, info):
        """Validate persistent memory is reasonable"""
        if v <= 0:
            raise ValueError(f"PERSISTENT_MEMORY_TOKENS must be > 0, got {v}")
        return v
    
    def model_post_init(self, __context):
        """Validate total memory budget after all fields loaded"""
        total = (self.hot_memory_tokens + self.warm_memory_tokens + 
                 self.cold_memory_tokens + self.persistent_memory_tokens)
        
        if total > 5000:
            raise ValueError(
                f"Total OODA memory ({total} tokens) exceeds reasonable budget (5000 tokens). "
                f"Consider reducing individual memory tier allocations."
            )

    model_config = {"env_prefix": "", "extra": "ignore"}


class ConversationThresholds(BaseSettings):
    """DEPRECATED: Conversation management thresholds

    This class is deprecated in v3.2.0 and replaced by OODASettings.
    Kept for backward compatibility during migration.
    Will be removed in v4.0.0.
    """
    # Conversation history limits
    max_clarifications: int = Field(default=3, env="MAX_CLARIFICATIONS", deprecated=True)
    max_conversation_turns: int = Field(default=20, env="MAX_CONVERSATION_TURNS", deprecated=True)
    max_conversation_tokens: int = Field(default=4000, env="MAX_CONVERSATION_TOKENS", deprecated=True)

    # Token budgets for prompt assembly
    context_token_budget: int = Field(default=4000, env="CONTEXT_TOKEN_BUDGET", deprecated=True)

    # Classification confidence thresholds (no longer used)
    pattern_confidence_threshold: float = Field(default=0.7, env="PATTERN_CONFIDENCE_THRESHOLD", deprecated=True)
    confidence_override_threshold: float = Field(default=0.4, env="CONFIDENCE_OVERRIDE_THRESHOLD", deprecated=True)
    self_correction_min_confidence: float = Field(default=0.4, env="SELF_CORRECTION_MIN_CONFIDENCE", deprecated=True)
    self_correction_max_confidence: float = Field(default=0.7, env="SELF_CORRECTION_MAX_CONFIDENCE", deprecated=True)

    model_config = {"env_prefix": "", "extra": "ignore"}


class PromptSettings(BaseSettings):
    """DEPRECATED: Doctor/Patient prompt system configuration

    This class is deprecated in v3.2.0 and replaced by OODA Consultant/Lead Investigator modes.
    Kept for backward compatibility during migration.
    Will be removed in v4.0.0.
    """
    # Prompt version selection
    doctor_patient_version: str = Field(
        default="standard",
        env="DOCTOR_PATIENT_PROMPT_VERSION",
        deprecated=True,
        description="DEPRECATED: Prompt version (replaced by OODA modes)"
    )

    # Dynamic version selection (future enhancement)
    enable_dynamic_version_selection: bool = Field(
        default=False,
        env="ENABLE_DYNAMIC_PROMPT_VERSION",
        deprecated=True,
        description="DEPRECATED: Dynamic selection (not applicable to OODA)"
    )

    # Version selection rules (when dynamic enabled)
    minimal_threshold_tokens: int = Field(
        default=50,
        env="MINIMAL_PROMPT_THRESHOLD",
        deprecated=True,
        description="DEPRECATED: Threshold (not applicable to OODA)"
    )
    detailed_threshold_complexity: float = Field(
        default=0.7,
        env="DETAILED_PROMPT_THRESHOLD",
        deprecated=True,
        description="DEPRECATED: Complexity threshold (not applicable to OODA)"
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class FeatureSettings(BaseSettings):
    """Feature flags and toggles"""
    use_di_container: bool = Field(default=True, env="USE_DI_CONTAINER")
    use_refactored_services: bool = Field(default=True, env="USE_REFACTORED_SERVICES")
    use_refactored_api: bool = Field(default=True, env="USE_REFACTORED_API")
    enable_legacy_compatibility: bool = Field(default=False, env="ENABLE_LEGACY_COMPATIBILITY")

    # Token-Aware Context Management
    enable_token_aware_context: bool = Field(default=True, env="ENABLE_TOKEN_AWARE_CONTEXT")
    enable_conversation_summarization: bool = Field(default=True, env="ENABLE_CONVERSATION_SUMMARIZATION")
    # Note: Token budgets and thresholds moved to ConversationThresholds class above

    # DEPRECATED: Query Classification System (Replaced by OODA v3.2.0)
    # These settings are no longer used - OODA uses structured responses instead of classification
    # Will be removed in v4.0.0
    llm_classification_mode: str = Field(
        default="enhancement",
        env="LLM_CLASSIFICATION_MODE",
        deprecated=True
    )
    enable_multidimensional_confidence: bool = Field(
        default=True,
        env="ENABLE_MULTIDIMENSIONAL_CONFIDENCE",
        deprecated=True
    )
    pattern_weighted_scoring: bool = Field(
        default=True,
        env="PATTERN_WEIGHTED_SCORING",
        deprecated=True
    )
    pattern_exclusion_rules: bool = Field(
        default=True,
        env="PATTERN_EXCLUSION_RULES",
        deprecated=True
    )
    enable_structure_analysis: bool = Field(
        default=True,
        env="ENABLE_STRUCTURE_ANALYSIS",
        deprecated=True
    )
    enable_linguistic_analysis: bool = Field(
        default=True,
        env="ENABLE_LINGUISTIC_ANALYSIS",
        deprecated=True
    )
    enable_entity_analysis: bool = Field(
        default=True,
        env="ENABLE_ENTITY_ANALYSIS",
        deprecated=True
    )
    enable_context_analysis: bool = Field(
        default=True,
        env="ENABLE_CONTEXT_ANALYSIS",
        deprecated=True
    )
    enable_disambiguation_check: bool = Field(
        default=True,
        env="ENABLE_DISAMBIGUATION_CHECK",
        deprecated=True
    )

    # Note: Self-correction thresholds moved to ConversationThresholds class above
    # Deprecated: SELF_CORRECTION_THRESHOLD → use ConversationThresholds.self_correction_max_confidence
    # Deprecated: FORCED_CLARIFICATION_THRESHOLD → use ConversationThresholds.confidence_override_threshold

    # Experimental features
    enable_advanced_reasoning: bool = Field(default=False, env="ENABLE_ADVANCED_REASONING")
    enable_multi_agent: bool = Field(default=False, env="ENABLE_MULTI_AGENT")
    enable_workflow_optimization: bool = Field(default=False, env="ENABLE_WORKFLOW_OPTIMIZATION")

    model_config = {"env_prefix": "", "extra": "ignore"}


class ToolsSettings(BaseSettings):
    """Tools and external service configuration"""
    # Web search configuration
    web_search_api_key: Optional[SecretStr] = Field(default=None, env="WEB_SEARCH_API_KEY")
    web_search_api_endpoint: str = Field(
        default="https://www.googleapis.com/customsearch/v1", 
        env="WEB_SEARCH_API_ENDPOINT"
    )
    web_search_engine_id: str = Field(default="", env="WEB_SEARCH_ENGINE_ID")
    web_search_max_results: int = Field(default=3, env="WEB_SEARCH_MAX_RESULTS")
    
    model_config = {"env_prefix": "", "extra": "ignore"}


# EnhancedProtectionSettings merged into ProtectionSettings above


# EnhancedObservabilitySettings merged into ObservabilitySettings above


# EnhancedDatabaseSettings merged into DatabaseSettings above
# NOTE: Presidio configuration moved to ProtectionSettings to avoid duplication


class AlertingSettings(BaseSettings):
    """Email and webhook alerting configuration"""
    alert_from_email: Optional[str] = Field(default=None, env="ALERT_FROM_EMAIL")
    alert_to_emails: str = Field(default="", env="ALERT_TO_EMAILS")
    alert_webhook_url: Optional[str] = Field(default=None, env="ALERT_WEBHOOK_URL")
    
    # SMTP Configuration
    smtp_host: str = Field(default="localhost", env="SMTP_HOST")
    smtp_port: int = Field(default=587, env="SMTP_PORT")

    model_config = {"env_prefix": "", "extra": "ignore"}


class WorkspaceSettings(BaseSettings):
    """Workspace and collaboration settings (comet_workspace moved to ObservabilitySettings)"""
    comet_api_key: Optional[SecretStr] = Field(default=None, env="COMET_API_KEY")

    # Feature toggles for experimental features
    enable_experimental_features: bool = Field(default=False, env="ENABLE_EXPERIMENTAL_FEATURES")

    model_config = {"env_prefix": "", "extra": "ignore"}


class PreprocessingSettings(BaseSettings):
    """Data preprocessing and chunking configuration"""

    # Chunking thresholds
    chunk_trigger_tokens: int = Field(
        default=8000,
        env="CHUNK_TRIGGER_TOKENS",
        description="Documents >8K tokens trigger map-reduce chunking"
    )

    # Chunking parameters
    chunk_size_tokens: int = Field(
        default=4000,
        env="CHUNK_SIZE_TOKENS",
        description="Target chunk size for map-reduce (~16KB text)"
    )

    chunk_overlap_tokens: int = Field(
        default=200,
        env="CHUNK_OVERLAP_TOKENS",
        description="Overlap between chunks for context preservation"
    )

    map_reduce_max_parallel: int = Field(
        default=5,
        env="MAP_REDUCE_MAX_PARALLEL",
        ge=1,
        le=10,
        description="Maximum parallel LLM calls during MAP phase"
    )

    # Provider for chunking (defaults to synthesis provider)
    chunking_provider: str = Field(
        default="synthesis",
        env="CHUNKING_PROVIDER",
        description="LLM provider for chunking operations (synthesis, chat, or specific provider)"
    )

    @field_validator('chunk_size_tokens')
    @classmethod
    def validate_chunk_size(cls, v, info):
        """Ensure chunk size is less than trigger"""
        values = info.data
        trigger = values.get('chunk_trigger_tokens', 8000)
        
        if v >= trigger:
            raise ValueError(
                f"CHUNK_SIZE_TOKENS ({v}) must be < CHUNK_TRIGGER_TOKENS ({trigger}). "
                f"Trigger must activate before reaching chunk size."
            )
        return v
    
    @field_validator('chunk_overlap_tokens')
    @classmethod
    def validate_chunk_overlap(cls, v, info):
        """Ensure overlap is reasonable percentage of chunk size"""
        values = info.data
        chunk_size = values.get('chunk_size_tokens', 4000)
        
        if v >= chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP_TOKENS ({v}) must be < CHUNK_SIZE_TOKENS ({chunk_size}). "
                f"Overlap is a subset of the chunk."
            )
        
        if v > chunk_size * 0.5:
            raise ValueError(
                f"CHUNK_OVERLAP_TOKENS ({v}) should not exceed 50% of CHUNK_SIZE_TOKENS ({chunk_size}). "
                f"Recommended: 5-10% overlap for optimal context preservation."
            )
        
        if v < 0:
            raise ValueError(f"CHUNK_OVERLAP_TOKENS must be >= 0, got {v}")
        
        return v

    model_config = {"env_prefix": "", "extra": "ignore"}


# =============================================================================
# MAIN SETTINGS CLASS
# =============================================================================

class FaultMavenSettings(BaseSettings):
    """
    Unified configuration for FaultMaven system.
    
    Single source of truth that replaces:
    - config/config.py
    - config/configuration_manager.py  
    - Direct os.getenv() calls throughout codebase
    
    All configuration access should go through this class via dependency injection.
    """
    
    # Nested configuration sections
    server: ServerSettings = Field(default_factory=ServerSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    protection: ProtectionSettings = Field(default_factory=ProtectionSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # OODA Framework v3.2.0
    ooda: OODASettings = Field(default_factory=OODASettings)

    # DEPRECATED: Kept for backward compatibility
    thresholds: ConversationThresholds = Field(default_factory=ConversationThresholds)
    prompts: PromptSettings = Field(default_factory=PromptSettings)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore"
    }
    upload: UploadSettings = Field(default_factory=UploadSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    preprocessing: PreprocessingSettings = Field(default_factory=PreprocessingSettings)
    
    # Enhanced configuration sections merged into main sections above
    # enhanced_protection merged into protection above  
    # enhanced_observability merged into observability above
    # enhanced_database merged into database above
    alerting: AlertingSettings = Field(default_factory=AlertingSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "validate_assignment": True,
        "use_enum_values": True,
        "extra": "ignore"  # Allow extra environment variables
    }
    
    def get_cors_config(self) -> Dict[str, Any]:
        """
        Generate FastAPI CORS configuration.
        Critical for frontend compatibility.
        """
        return {
            "allow_origins": self.security.cors_allow_origins,
            "allow_credentials": self.security.cors_allow_credentials,
            "allow_methods": ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
            "allow_headers": ["*"],
            "expose_headers": self.security.cors_expose_headers,
        }
    
    def validate_frontend_compatibility(self) -> Dict[str, Any]:
        """
        Validate configuration for frontend compatibility.
        
        Returns:
            Dict with compatibility status and any issues found
        """
        issues = []
        warnings = []
        
        # Session timeout validation
        if self.session.timeout_minutes < 5:
            issues.append("Session timeout too short - frontend expects >= 5 minutes")
        
        # Heartbeat validation
        if self.session.heartbeat_interval_seconds >= (self.session.timeout_minutes * 60):
            issues.append("Heartbeat interval must be less than session timeout")
        
        # CORS validation
        browser_origins = [
            "chrome-extension://*", 
            "moz-extension://*",
            "http://localhost:3000"
        ]
        missing_origins = []
        for origin in browser_origins:
            if not any(origin in allowed for allowed in self.security.cors_allow_origins):
                missing_origins.append(origin)
        
        if missing_origins:
            issues.append(f"Missing CORS origins: {missing_origins}")
        
        # Required exposed headers for frontend
        required_headers = ["X-RateLimit-Remaining", "X-Total-Count", "Location"]
        missing_headers = []
        for header in required_headers:
            if header not in self.security.cors_expose_headers:
                missing_headers.append(header)
        
        if missing_headers:
            issues.append(f"Missing exposed headers: {missing_headers}")
        
        # Rate limiting validation
        if not self.security.rate_limit_enabled:
            warnings.append("Rate limiting disabled - frontend expects rate limit headers")
        
        # Upload size warnings
        if self.upload.max_file_size_mb > 100:
            warnings.append("Large upload size may cause timeout issues")
        
        return {
            "compatible": len(issues) == 0,
            "issues": issues,
            "warnings": warnings
        }
    
    def get_redis_url(self) -> str:
        """Build Redis connection URL"""
        if self.database.redis_url:
            return self.database.redis_url
        
        auth = ""
        if self.database.redis_password:
            password = self.database.redis_password.get_secret_value()
            auth = f":{password}@"
        
        return f"redis://{auth}{self.database.redis_host}:{self.database.redis_port}/{self.database.redis_db}"
    
    def is_development(self) -> bool:
        """Check if running in development mode"""
        return self.server.environment == Environment.DEVELOPMENT
    
    def is_production(self) -> bool:
        """Check if running in production mode"""
        return self.server.environment == Environment.PRODUCTION


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================

_settings_instance: Optional[FaultMavenSettings] = None


def get_settings() -> FaultMavenSettings:
    """
    Get global settings instance (singleton pattern).
    
    This is the ONLY function that should be used to access configuration
    throughout the application. All other modules should receive settings
    via dependency injection.
    
    Raises:
        ConfigurationError: If settings validation fails
    """
    global _settings_instance
    if _settings_instance is None:
        try:
            # Ensure .env file is loaded before creating settings
            from dotenv import load_dotenv
            import os

            # Force load .env file with override to ensure fresh values
            load_dotenv(override=True)

            # Debug: Log what we're loading
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Settings loading - CHAT_PROVIDER={os.getenv('CHAT_PROVIDER')}")
            logger.info(f"Settings loading - LOCAL_LLM_URL={os.getenv('LOCAL_LLM_URL')}")
            logger.info(f"Settings loading - LOCAL_LLM_MODEL={os.getenv('LOCAL_LLM_MODEL')}")

            _settings_instance = FaultMavenSettings()
        except Exception as e:
            from faultmaven.models.exceptions import ConfigurationError
            raise ConfigurationError(
                f"Settings initialization failed: {e}",
                error_code="SETTINGS_INIT_ERROR",
                context={"original_error": str(e), "error_type": type(e).__name__}
            )
    return _settings_instance


def reset_settings() -> None:
    """
    Reset settings instance (primarily for testing).
    
    Forces recreation of settings on next get_settings() call.
    """
    global _settings_instance
    _settings_instance = None


# =============================================================================
# LEGACY COMPATIBILITY BRIDGE
# =============================================================================

class ConfigurationBridge:
    """
    Temporary bridge for legacy configuration access during migration.
    
    Allows gradual migration from old config systems.
    This class should be REMOVED once migration is complete.
    """
    
    def __init__(self):
        self._settings = get_settings()
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation.
        
        Examples:
            bridge.get("llm.provider") -> "fireworks"
            bridge.get("server.port") -> 8000
        """
        try:
            parts = key.split(".")
            value = self._settings
            
            for part in parts:
                value = getattr(value, part)
            
            return value
        except (AttributeError, TypeError):
            return default


# Global bridge instance for legacy compatibility
config_bridge = ConfigurationBridge()