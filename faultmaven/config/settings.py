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

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Type, Union

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

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
    GEMINI = "gemini"
    HUGGINGFACE = "huggingface"
    COHERE = "cohere"
    OPENROUTER = "openrouter"
    LOCAL = "local"
    GROQ = "groq"
    NOT_SET = "NOT_SET"


# =============================================================================
# PROVIDER SELECTORS (Doc-aligned - PR #3)
# =============================================================================


class TenantProvider(str, Enum):
    """Tenant isolation strategy selector."""

    SINGLE = "single"
    MULTI = "multi"


class DbBackend(str, Enum):
    """Database backend selector."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


class CacheBackend(str, Enum):
    """Cache backend selector."""

    MEMORY = "memory"
    REDIS = "redis"


class VectorBackend(str, Enum):
    """Vector database backend selector."""

    CHROMA = "chroma"
    PINECONE = "pinecone"


class StorageBackend(str, Enum):
    """File storage backend selector."""

    FILESYSTEM = "filesystem"
    S3 = "s3"


class MetricsExporter(str, Enum):
    """Metrics exporter provider selector (PR #5).

    Controls how metrics are exposed for external collection:
    - none: No metrics endpoint (default, operationally neutral)
    - prometheus_http: Mount /metrics endpoint with Prometheus text format
    - otel: (future) OpenTelemetry exporter
    """

    NONE = "none"
    PROMETHEUS_HTTP = "prometheus_http"
    # OTEL = "otel"  # Future: OpenTelemetry exporter


# =============================================================================
# NESTED CONFIGURATION SECTIONS
# =============================================================================


class ServerSettings(BaseSettings):
    """Core server configuration"""

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8090)
    reload: bool = Field(default=False)
    workers: int = Field(default=1)

    # Environment and behavior
    environment: Environment = Field(default=Environment.DEVELOPMENT)
    debug: bool = Field(default=False)
    skip_service_checks: bool = Field(default=False)

    # Testing configuration
    pytest_current_test: Optional[str] = Field(default=None)

    # Scheduler configuration - opt-in for single-process convenience mode
    # When True, starts APScheduler in-process during app startup.
    # Default is False for operational neutrality (use external schedulers like cron/k8s).
    # Set RUN_SCHEDULER=true only for single-process development/convenience mode.
    run_scheduler: bool = Field(
        default=False,
        description="Enable in-process APScheduler. Default False for operational neutrality. "
        "Set to True only for single-process convenience mode (not recommended for production).",
    )

    # Debug endpoints configuration
    enable_debug_endpoints: bool = Field(
        default=False,
        description="Enable debug endpoints (development/testing only). "
        "Automatically enabled if ENVIRONMENT is development/testing/test.",
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class LLMSettings(BaseSettings):
    """LLM provider configuration with flexible multi-model support"""

    # Task-specific provider selection
    provider: LLMProvider = Field(
        default=LLMProvider.FIREWORKS, validation_alias="CHAT_PROVIDER"
    )
    multimodal_provider: Optional[LLMProvider] = Field(default=None)
    synthesis_provider: Optional[LLMProvider] = Field(default=None)
    classifier_provider: Optional[LLMProvider] = Field(default=None)
    code_provider: Optional[LLMProvider] = Field(default=None)
    da_provider: Optional[LLMProvider] = Field(default=None)
    knowledge_provider: Optional[LLMProvider] = Field(default=None)

    # API Keys (SecretStr for security)
    openai_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="OPENAI_API_KEY"
    )
    anthropic_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY"
    )
    fireworks_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="FIREWORKS_API_KEY"
    )
    cohere_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="COHERE_API_KEY"
    )
    gemini_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="GEMINI_API_KEY"
    )
    huggingface_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="HUGGINGFACE_API_KEY"
    )
    openrouter_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="OPENROUTER_API_KEY"
    )
    groq_api_key: Optional[SecretStr] = Field(
        default=None, validation_alias="GROQ_API_KEY"
    )

    # Flexible model configuration per provider and task
    # Per-task model overrides (all Optional — fall back to {provider}_model)
    # Only set these in .env if you want a DIFFERENT model for a specific task.
    # e.g., OPENAI_CODE_MODEL=gpt-4o for code, while OPENAI_MODEL=gpt-4o-mini for everything else.

    # OpenAI
    openai_chat_model: Optional[str] = Field(default=None)
    openai_multimodal_model: Optional[str] = Field(default=None)
    openai_synthesis_model: Optional[str] = Field(default=None)
    openai_classifier_model: Optional[str] = Field(default=None)
    openai_code_model: Optional[str] = Field(default=None)
    openai_da_model: Optional[str] = Field(default=None)
    openai_knowledge_model: Optional[str] = Field(default=None)

    # Anthropic
    anthropic_chat_model: Optional[str] = Field(default=None)
    anthropic_multimodal_model: Optional[str] = Field(default=None)
    anthropic_synthesis_model: Optional[str] = Field(default=None)
    anthropic_classifier_model: Optional[str] = Field(default=None)
    anthropic_code_model: Optional[str] = Field(default=None)
    anthropic_da_model: Optional[str] = Field(default=None)
    anthropic_knowledge_model: Optional[str] = Field(default=None)

    # Fireworks
    fireworks_chat_model: Optional[str] = Field(default=None)
    fireworks_multimodal_model: Optional[str] = Field(default=None)
    fireworks_synthesis_model: Optional[str] = Field(default=None)
    fireworks_classifier_model: Optional[str] = Field(default=None)
    fireworks_code_model: Optional[str] = Field(default=None)
    fireworks_da_model: Optional[str] = Field(default=None)
    fireworks_knowledge_model: Optional[str] = Field(default=None)

    # Google Gemini
    gemini_chat_model: Optional[str] = Field(default=None)
    gemini_multimodal_model: Optional[str] = Field(default=None)
    gemini_synthesis_model: Optional[str] = Field(default=None)
    gemini_classifier_model: Optional[str] = Field(default=None)
    gemini_code_model: Optional[str] = Field(default=None)
    gemini_da_model: Optional[str] = Field(default=None)
    gemini_knowledge_model: Optional[str] = Field(default=None)

    # Cohere
    cohere_chat_model: Optional[str] = Field(default=None)
    cohere_multimodal_model: Optional[str] = Field(default=None)
    cohere_synthesis_model: Optional[str] = Field(default=None)
    cohere_classifier_model: Optional[str] = Field(default=None)
    cohere_code_model: Optional[str] = Field(default=None)
    cohere_da_model: Optional[str] = Field(default=None)
    cohere_knowledge_model: Optional[str] = Field(default=None)

    # HuggingFace
    huggingface_chat_model: Optional[str] = Field(default=None)
    huggingface_multimodal_model: Optional[str] = Field(default=None)
    huggingface_synthesis_model: Optional[str] = Field(default=None)
    huggingface_classifier_model: Optional[str] = Field(default=None)
    huggingface_code_model: Optional[str] = Field(default=None)
    huggingface_da_model: Optional[str] = Field(default=None)
    huggingface_knowledge_model: Optional[str] = Field(default=None)

    # OpenRouter
    openrouter_chat_model: Optional[str] = Field(default=None)
    openrouter_multimodal_model: Optional[str] = Field(default=None)
    openrouter_synthesis_model: Optional[str] = Field(default=None)
    openrouter_classifier_model: Optional[str] = Field(default=None)
    openrouter_code_model: Optional[str] = Field(default=None)
    openrouter_da_model: Optional[str] = Field(default=None)
    openrouter_knowledge_model: Optional[str] = Field(default=None)

    # Groq
    groq_chat_model: Optional[str] = Field(default=None)
    groq_multimodal_model: Optional[str] = Field(default=None)
    groq_synthesis_model: Optional[str] = Field(default=None)
    groq_classifier_model: Optional[str] = Field(default=None)
    groq_code_model: Optional[str] = Field(default=None)
    groq_da_model: Optional[str] = Field(default=None)
    groq_knowledge_model: Optional[str] = Field(default=None)

    # Model configuration
    openai_model: str = Field(default="gpt-4o")
    anthropic_model: str = Field(default="claude-3-sonnet-20240229")
    fireworks_model: str = Field(
        default="accounts/fireworks/models/llama-v3p1-405b-instruct",
    )
    cohere_model: str = Field(default="command-r-plus")
    gemini_model: str = Field(default="gemini-2.0-flash")
    huggingface_model: str = Field(default="tiiuae/falcon-7b-instruct")
    openrouter_model: str = Field(default="openrouter-default")

    # Local provider configuration
    local_url: Optional[str] = Field(default=None, validation_alias="LOCAL_LLM_URL")
    local_model: Optional[str] = Field(default=None, validation_alias="LOCAL_LLM_MODEL")

    # Base URLs for each provider
    openai_base_url: str = Field(
        default="https://api.openai.com/v1", validation_alias="OPENAI_API_BASE"
    )
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com/v1", validation_alias="ANTHROPIC_API_BASE"
    )
    fireworks_base_url: str = Field(
        default="https://api.fireworks.ai/inference/v1",
        validation_alias="FIREWORKS_API_BASE",
    )
    cohere_base_url: str = Field(
        default="https://api.cohere.ai/v1", validation_alias="COHERE_API_BASE"
    )
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        validation_alias="GEMINI_API_BASE",
    )
    huggingface_base_url: str = Field(
        default="https://api-inference.huggingface.co/models",
        validation_alias="HUGGINGFACE_API_URL",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", validation_alias="OPENROUTER_API_BASE"
    )
    groq_base_url: str = Field(
        default="https://api.groq.com/openai/v1", validation_alias="GROQ_API_BASE"
    )

    # Request configuration
    request_timeout: int = Field(default=30, validation_alias="LLM_REQUEST_TIMEOUT")
    max_retries: int = Field(default=3, validation_alias="LLM_MAX_RETRIES")
    retry_delay: float = Field(default=1.0, validation_alias="LLM_RETRY_DELAY")

    # Provider behavior
    strict_provider_mode: bool = Field(
        default=True,  # Changed to True for predictability and transparency
        description="When enabled, only use the primary provider with no fallbacks. "
        "Default: True (single LLM for consistency). Set to false for automatic fallback to other providers.",
    )

    # Token limits
    max_tokens: int = Field(default=4096, validation_alias="LLM_MAX_TOKENS")
    context_window: int = Field(default=128000, validation_alias="LLM_CONTEXT_WINDOW")

    # Phase/Tool response limits (separate from provider limits)
    phase_response_max_tokens: int = Field(
        default=2000,
        validation_alias="LLM_PHASE_RESPONSE_MAX_TOKENS",
        ge=500,
        le=4096,
        description="Maximum tokens for phase handler and tool responses (OODA structure needs 400-1500 tokens)",
    )

    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, v, info):
        """Ensure max_tokens is reasonable and within context window"""
        if v < 100:
            raise ValueError("LLM_MAX_TOKENS must be >= 100 for useful responses")

        values = info.data
        context_window = values.get("context_window", 128000)
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
            task: Task type ('chat', 'multimodal', 'synthesis', 'classifier', 'code', 'da', 'knowledge')
        """
        return self._get_model_for_provider_and_task(self.provider, task)

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

    def get_da_provider(self) -> LLMProvider:
        """Get DA (directed analysis) provider (falls back to chat provider if not set)"""
        return self.da_provider or self.provider

    def get_da_model(self) -> str:
        """Get model for DA provider using task-specific configuration"""
        provider = self.get_da_provider()
        return self._get_model_for_provider_and_task(provider, "da")

    def get_knowledge_provider(self) -> LLMProvider:
        """Get knowledge provider for document conversion (falls back to chat provider if not set)"""
        return self.knowledge_provider or self.provider

    def get_knowledge_model(self) -> str:
        """Get model for knowledge provider using task-specific configuration"""
        provider = self.get_knowledge_provider()
        return self._get_model_for_provider_and_task(provider, "knowledge")

    def _get_model_for_provider_and_task(self, provider: LLMProvider, task: str) -> str:
        """Get model for a provider and task.

        Resolution order:
        1. Per-task override: {PROVIDER}_{TASK}_MODEL (e.g., GEMINI_CLASSIFIER_MODEL)
        2. Base provider model: {PROVIDER}_MODEL (e.g., GEMINI_MODEL)
        3. Empty string (no model configured)

        Per-task overrides are Optional[str] = None. When not set in .env,
        the base model is used for all tasks. Set a per-task override only
        when you need a different model for that specific capability.
        """
        if provider == LLMProvider.LOCAL:
            return self.local_model or ""

        # Base model per provider (the single source of truth from .env)
        base_models: Dict[LLMProvider, str] = {
            LLMProvider.OPENAI: self.openai_model,
            LLMProvider.ANTHROPIC: self.anthropic_model,
            LLMProvider.FIREWORKS: self.fireworks_model,
            LLMProvider.COHERE: self.cohere_model,
            LLMProvider.GEMINI: self.gemini_model,
            LLMProvider.HUGGINGFACE: self.huggingface_model,
            LLMProvider.OPENROUTER: self.openrouter_model,
            LLMProvider.GROQ: self.groq_chat_model or "",
        }

        base = base_models.get(provider, "")

        # Per-task override: {provider_name}_{task}_model attribute
        task_field = f"{provider.value}_{task}_model"
        per_task = getattr(self, task_field, None)

        return per_task or base or ""

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
        "extra": "ignore",
    }


class DatabaseSettings(BaseSettings):
    """Unified database and persistence configuration"""

    # ============================================
    # Primary Database Configuration (SQLite/PostgreSQL)
    # ============================================
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/faultmaven.db",
        description="Primary database URL (SQLite for dev, PostgreSQL for prod)",
    )
    database_echo: bool = Field(default=False, description="Echo SQL statements to log")
    database_pool_size: int = Field(default=5)
    database_max_overflow: int = Field(default=10)
    database_pool_timeout: int = Field(default=30)
    database_pool_recycle: int = Field(default=1800)

    # ============================================
    # Redis Configuration (K8s ClusterIP internal service)
    # ============================================
    redis_host: str = Field(default="faultmaven-redis-master")
    redis_port: int = Field(default=6379)
    redis_db: int = Field(default=0)
    redis_password: Optional[SecretStr] = Field(default=None)
    redis_url: Optional[str] = Field(default=None)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }

    # ============================================
    # ChromaDB Configuration (K8s Ingress for HTTP)
    # ============================================
    chromadb_host: str = Field(default="chromadb.faultmaven.local")
    chromadb_port: int = Field(default=30080)
    # Empty default = no external ChromaDB server configured → go straight to
    # local PersistentClient. Cloud deployments set CHROMADB_URL explicitly to
    # opt in to HttpClient.
    chromadb_url: str = Field(default="")
    chromadb_api_key: Optional[SecretStr] = Field(default=None)

    # ChromaDB Extended Configuration (merged from EnhancedDatabaseSettings)
    chromadb_auth_token: Optional[SecretStr] = Field(default=None)
    chromadb_collection: str = Field(default="faultmaven_kb")

    # ChromaDB Split Storage — separate instances for KB (permanent) and evidence (ephemeral)
    chromadb_kb_persist_dir: str = Field(default="./data/chroma-kb")
    chromadb_evidence_persist_dir: str = Field(default="./data/chroma-evidence")

    # Vector Database Settings
    embedding_model: str = Field(default="BAAI/bge-m3")
    similarity_threshold: float = Field(default=0.7)
    max_search_results: int = Field(default=10)

    # Vector chunking — one-shot deployment knobs.
    # Changing these AFTER the vector DB has been populated requires deleting
    # the existing ChromaDB collection(s) and re-ingesting all evidence + KB
    # content from source. Mixing chunk sizes in one collection silently
    # degrades retrieval quality. Leave at defaults unless you have a reason.
    vector_chunk_size_tokens: int = Field(
        default=500,
        validation_alias="VECTOR_CHUNK_SIZE_TOKENS",
        description="Max tokens per chunk when embedding structural indexes",
    )
    vector_chunk_overlap_tokens: int = Field(
        default=50,
        validation_alias="VECTOR_CHUNK_OVERLAP_TOKENS",
        description="Token overlap between adjacent chunks",
    )

    # ============================================
    # Pinecone Configuration (Optional Vector Backend)
    # ============================================
    pinecone_api_key: Optional[SecretStr] = Field(
        default=None,
        description="Pinecone API key (required if VECTOR_BACKEND=pinecone)",
    )
    pinecone_index: str = Field(
        default="faultmaven",
        description="Pinecone index name",
    )
    pinecone_environment: str = Field(
        default="us-east-1",
        description="Pinecone environment/region",
    )
    pinecone_dimension: int = Field(
        default=1536,
        description="Vector dimension for Pinecone index",
    )

    # ============================================
    # PostgreSQL Configuration (K8s Deployment)
    # ============================================

    # Storage adapter selection
    user_storage_type: str = Field(default="inmemory")
    case_storage_type: str = Field(default="database")

    # PostgreSQL - Auth Database (for user data)
    auth_db_host: str = Field(default="postgres.faultmaven.local")
    auth_db_port: int = Field(default=30432)
    auth_db_name: str = Field(default="auth_db")
    auth_db_user: str = Field(default="auth_service")
    auth_db_password: Optional[SecretStr] = Field(default=None)

    # PostgreSQL - Cases Database (for case data)
    cases_db_host: str = Field(default="postgres.faultmaven.local")
    cases_db_port: int = Field(default=30432)
    cases_db_name: str = Field(default="cases_db")
    cases_db_user: str = Field(default="case_service")
    cases_db_password: Optional[SecretStr] = Field(default=None)

    @property
    def auth_db_url(self) -> str:
        """Build PostgreSQL auth database URL"""
        password = (
            self.auth_db_password.get_secret_value() if self.auth_db_password else ""
        )
        return f"postgresql+asyncpg://{self.auth_db_user}:{password}@{self.auth_db_host}:{self.auth_db_port}/{self.auth_db_name}"

    @property
    def cases_db_url(self) -> str:
        """Build PostgreSQL cases database URL"""
        password = (
            self.cases_db_password.get_secret_value() if self.cases_db_password else ""
        )
        return f"postgresql+asyncpg://{self.cases_db_user}:{password}@{self.cases_db_host}:{self.cases_db_port}/{self.cases_db_name}"

    # ============================================
    # Session Storage Adapter Configuration
    # ============================================
    session_storage_type: str = Field(default="inmemory")

    # ============================================
    # Vector Storage Adapter Configuration
    # ============================================
    # "chromadb" (default) uses local ChromaDB PersistentClient, or HttpClient
    # when CHROMADB_URL is set. Legacy values ("inmemory", "") are silently
    # accepted and resolve to the same local PersistentClient — there is no
    # InMemoryVectorStore implementation anymore.
    vector_storage_type: str = Field(default="chromadb")

    model_config = {"env_prefix": "", "extra": "ignore"}


class SessionSettings(BaseSettings):
    """Session management configuration"""

    timeout_minutes: int = Field(
        default=30, validation_alias="SESSION_TIMEOUT_MINUTES", ge=1, le=1440
    )
    cleanup_interval_minutes: int = Field(
        default=15, validation_alias="SESSION_CLEANUP_INTERVAL_MINUTES"
    )
    max_memory_mb: int = Field(default=100, validation_alias="SESSION_MAX_MEMORY_MB")
    heartbeat_interval_seconds: int = Field(
        default=30, validation_alias="SESSION_HEARTBEAT_INTERVAL_SECONDS"
    )
    max_sessions_per_user: int = Field(default=10)

    # Session timeout bounds for validation (used by API routes)
    min_timeout_minutes: int = Field(
        default=60, validation_alias="SESSION_MIN_TIMEOUT_MINUTES", ge=1
    )
    max_timeout_minutes: int = Field(
        default=480, validation_alias="SESSION_MAX_TIMEOUT_MINUTES", le=1440
    )
    default_timeout_minutes: int = Field(
        default=180, validation_alias="SESSION_DEFAULT_TIMEOUT_MINUTES"
    )

    @field_validator("heartbeat_interval_seconds")
    @classmethod
    def validate_heartbeat_vs_timeout(cls, v, info):
        """Ensure heartbeat is less than timeout for frontend compatibility"""
        values = info.data
        timeout_seconds = values.get("timeout_minutes", 30) * 60
        if v >= timeout_seconds:
            raise ValueError(
                f"Heartbeat interval ({v}s) must be less than session timeout ({timeout_seconds}s)"
            )
        return v

    @field_validator("cleanup_interval_minutes")
    @classmethod
    def validate_cleanup_interval(cls, v, info):
        """Ensure cleanup interval is reasonable vs timeout"""
        values = info.data
        timeout = values.get("timeout_minutes", 180)

        if v > timeout:
            raise ValueError(
                f"SESSION_CLEANUP_INTERVAL_MINUTES ({v}) should not exceed "
                f"SESSION_TIMEOUT_MINUTES ({timeout}). "
                f"Cleanup should run at least as often as session expiration."
            )
        return v

    model_config = {"env_prefix": "", "extra": "ignore"}


class CaseSettings(BaseSettings):
    """Case management configuration"""

    # Title generation settings
    title_generation_use_fallback: bool = Field(
        default=True,
        description="Use fallback title when LLM-generated title fails validation",
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class SecuritySettings(BaseSettings):
    """Security and authentication configuration"""

    # JWT configuration (RS256 for production-ready asymmetric encryption)
    # For development/testing, HS256 with jwt_secret_key is also supported
    jwt_algorithm: str = Field(default="RS256")
    jwt_private_key_path: Optional[str] = Field(default=None)
    jwt_public_key_path: Optional[str] = Field(default=None)
    jwt_private_key: Optional[SecretStr] = Field(default=None)
    jwt_public_key: Optional[str] = Field(default=None)
    # HS256 fallback secret key (for development/testing when RSA keys are not configured)
    jwt_secret_key: Optional[SecretStr] = Field(
        default=None,
        description="Secret key for HS256 algorithm. Only used as fallback when RS256 keys are not configured.",
    )
    jwt_access_token_expire_minutes: int = Field(default=60)
    jwt_refresh_token_expire_days: int = Field(default=7)

    # Refresh token rotation (security best practice)
    jwt_rotate_refresh_tokens: bool = Field(
        default=True,
        description="Rotate refresh tokens on use (one-time use tokens)",
    )
    jwt_issuer: str = Field(default="faultmaven-api")
    jwt_audience: str = Field(default="faultmaven-app")

    # Token revocation (Redis)
    token_revocation_prefix: str = Field(default="revoked:token:")

    # CORS configuration
    cors_allow_credentials: bool = Field(default=True)
    cors_allow_origins: List[str] = Field(
        default=["http://localhost:3333", "chrome-extension://*", "moz-extension://*"],
    )
    cors_expose_headers: List[str] = Field(
        default=[
            "Location",
            "X-Total-Count",
            "Link",
            "Deprecation",
            "Sunset",
            "X-Request-ID",
            "Retry-After",
            "X-RateLimit-Remaining",
        ],
    )

    # Rate limiting
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_requests_per_minute: int = Field(default=60)
    rate_limit_burst_size: int = Field(default=10)

    model_config = {"env_prefix": "", "extra": "ignore"}


class AuthMode(str, Enum):
    """Authentication mode selector.

    Per iam-design.md, FaultMaven supports two authentication modes:
    - local: Simple username authentication for self-hosted/single-user deployments
    - oauth: OAuth 2.0 + PKCE for cloud/multi-user deployments
    """

    LOCAL = "local"
    OAUTH = "oauth"

    # Backward compatibility alias
    @classmethod
    def _missing_(cls, value: object) -> "AuthMode | None":
        """Handle legacy 'dev-login' value for backward compatibility."""
        if value == "dev-login":
            import logging

            logging.getLogger(__name__).warning(
                "AUTH_MODE='dev-login' is deprecated. Use AUTH_MODE='local' instead."
            )
            return cls.LOCAL
        return None


class AuthSettings(BaseSettings):
    """Authentication configuration (deployment-agnostic).

    Per iam-design.md, FaultMaven supports two authentication modes:
    - local: Simple username authentication for self-hosted/single-user deployments
    - oauth: OAuth 2.0 + PKCE for cloud/multi-user deployments

    ARCHITECTURAL PRINCIPLE: Deployment-agnostic design
    - Configuration-driven mode selection (local vs oauth)
    - Storage abstraction (Redis vs PostgreSQL for OAuth codes)
    - Core auth logic remains independent of deployment environment
    """

    # Authentication mode selection
    auth_mode: AuthMode = Field(
        default=AuthMode.LOCAL,
        description="Authentication mode: 'local' (self-hosted) or 'oauth' (cloud)",
    )

    # OAuth Configuration (only used when auth_mode=oauth)
    oauth_enabled: bool = Field(
        default=False,
        description="Enable OAuth 2.0 + PKCE authentication (production mode)",
    )

    # Dashboard URL (OAuth IdP)
    dashboard_url: str = Field(
        default="https://app.faultmaven.ai",
        description="Dashboard URL (acts as IdP for OAuth flow)",
    )

    # OAuth authorization code settings
    oauth_code_expiry_seconds: int = Field(
        default=600,
        ge=60,
        le=1800,
        description="Authorization code expiry (10 minutes default, PKCE-protected)",
    )

    # OAuth storage configuration (cache layer only - codes are ephemeral)
    # Authorization codes are short-lived (10 min) and should use cache layer:
    # - Local: in-memory cache
    # - Cloud: Redis cache
    # Database persistence is optional for compliance/audit (not for code retrieval)
    oauth_use_cache: bool = Field(
        default=True,
        description="Use cache layer for OAuth codes (in-memory local, Redis cloud)",
    )

    oauth_persist_codes_to_db: bool = Field(
        default=False,
        description="Persist OAuth codes to database for audit trail (optional)",
    )

    # Allowed OAuth clients (extension IDs)
    oauth_allowed_clients: List[str] = Field(
        default=["faultmaven-copilot"],
        description="Allowed OAuth client IDs",
    )

    # OAuth redirect URI patterns (regex)
    oauth_redirect_uri_patterns: List[str] = Field(
        default=[
            r"^chrome-extension://[a-z]{32}/callback\.html$",
            r"^moz-extension://[a-f0-9-]{36}/callback\.html$",
        ],
        description="Allowed redirect URI patterns (regex) for OAuth",
    )

    # OAuth consent settings
    oauth_require_consent: bool = Field(
        default=True,
        description="Require user consent screen (production). Set false for auto-approval (dev/test only)",
    )

    # OAuth security settings
    oauth_require_https_redirect: bool = Field(
        default=True,
        description="Require HTTPS for redirect URIs (production security). Set false for local dev",
    )

    # Local mode settings (only used when auth_mode=local)
    local_token_expiry_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Local mode token expiry (hours)",
    )

    # JWT token expiry settings (common for both modes per iam-design.md)
    jwt_access_token_expire_minutes: int = Field(
        default=60,
        validation_alias="JWT_ACCESS_TOKEN_EXPIRY",
        description="Access token expiry (minutes)",
    )
    jwt_refresh_token_expire_days: int = Field(
        default=7,
        validation_alias="JWT_REFRESH_TOKEN_EXPIRY",
        description="Refresh token expiry (days). Default 7 days = 10080 minutes",
    )

    @field_validator("oauth_enabled")
    @classmethod
    def validate_oauth_consistency(cls, v, info):
        """Ensure oauth_enabled matches auth_mode"""
        values = info.data
        auth_mode = values.get("auth_mode", AuthMode.LOCAL)

        if auth_mode == AuthMode.OAUTH and not v:
            raise ValueError(
                "AUTH_MODE=oauth requires OAUTH_ENABLED=true. "
                "Set both or use AUTH_MODE=local for self-hosted deployments."
            )

        if auth_mode == AuthMode.LOCAL and v:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                "OAUTH_ENABLED=true but AUTH_MODE=local. "
                "OAuth will be available but local auth is the active mode. "
                "Set AUTH_MODE=oauth to use OAuth as primary authentication."
            )

        return v

    model_config = {"env_prefix": "", "extra": "ignore"}


class ProtectionSettings(BaseSettings):
    """Unified protection configuration - PII, behavioral, and ML protection"""

    # Basic Protection Control
    # COMMUNITY DEFAULT: Disabled (enterprise feature - requires Presidio)
    protection_enabled: bool = Field(default=False)
    fail_open: bool = Field(default=True, validation_alias="PROTECTION_FAIL_OPEN")
    basic_protection_enabled: bool = Field(default=False)
    intelligent_protection_enabled: bool = Field(default=False)

    # PII Sanitization Control
    # When True: Always sanitize PII before sending to LLM (safer, recommended for external LLMs)
    # When False: Skip PII sanitization (only use with local/self-hosted LLMs)
    # Note: This affects data sent to LLM providers. Disable only if using LOCAL provider
    #       or if you trust your external LLM provider with sensitive data.
    # COMMUNITY DEFAULT: Disabled (enterprise feature - requires Presidio libraries)
    sanitize_pii: bool = Field(default=False)

    # TTL for case-scoped redaction registries in Redis (hours).
    # Controls how long the bidirectional PII mapping is kept for a case.
    # After expiry, a new registry starts (placeholders may renumber).
    redaction_registry_ttl_hours: int = Field(default=168)  # 7 days

    # Presidio Configuration (K8s Ingress-based to avoid port conflicts)
    presidio_analyzer_url: str = Field(
        default="http://presidio-analyzer.faultmaven.local:30080",
    )
    presidio_anonymizer_url: str = Field(
        default="http://presidio-anonymizer.faultmaven.local:30080",
    )

    # PII Protection Settings
    min_score_threshold: float = Field(default=0.85)
    supported_languages: List[str] = Field(default=["en"])
    entities_to_protect: List[str] = Field(
        default=[
            "CREDIT_CARD",
            "CRYPTO",
            "EMAIL_ADDRESS",
            "IBAN_CODE",
            "PHONE_NUMBER",
            "MEDICAL_LICENSE",
            "US_BANK_NUMBER",
            "US_DRIVER_LICENSE",
            "US_ITIN",
            "US_PASSPORT",
            "US_SSN",
        ],
    )

    # Behavioral Analysis (merged from EnhancedProtectionSettings)
    behavioral_analysis_enabled: bool = Field(default=True)
    behavior_analysis_window: int = Field(default=3600)
    behavior_pattern_threshold: float = Field(default=0.8)

    # ML Anomaly Detection (merged from EnhancedProtectionSettings)
    ml_anomaly_detection_enabled: bool = Field(default=True)
    ml_model_path: str = Field(default="/tmp/faultmaven_ml")
    ml_training_enabled: bool = Field(default=True)
    ml_online_learning_enabled: bool = Field(default=True)

    # Circuit Breaker (merged from EnhancedProtectionSettings)
    smart_circuit_breakers_enabled: bool = Field(default=True)
    circuit_failure_threshold: int = Field(default=5)
    circuit_timeout_seconds: int = Field(default=60)

    # Reputation System (merged from EnhancedProtectionSettings)
    reputation_system_enabled: bool = Field(default=True)
    reputation_decay_rate: float = Field(default=0.05)
    reputation_recovery_threshold: float = Field(default=0.1)

    # Monitoring Intervals (merged from EnhancedProtectionSettings)
    protection_monitoring_interval: int = Field(default=300)
    protection_cleanup_interval: int = Field(default=3600)

    model_config = {"env_prefix": "", "extra": "ignore"}


class ObservabilitySettings(BaseSettings):
    """Unified observability and monitoring configuration"""

    # Core Opik configuration
    opik_project_name: str = Field(default="faultmaven")
    opik_url_override: Optional[str] = Field(default=None)
    opik_use_local: bool = Field(default=False)
    opik_local_url: str = Field(default="http://localhost:5173")
    opik_local_host: str = Field(default="opik-api.faultmaven.local")

    # Opik API and tracking controls (merged from EnhancedObservabilitySettings)
    # COMMUNITY DEFAULT: Disabled (enterprise feature)
    opik_api_key: Optional[SecretStr] = Field(default=None)
    opik_enabled: bool = Field(default=False)
    opik_track_disable: bool = Field(default=True)
    opik_track_users: str = Field(default="")
    opik_track_sessions: str = Field(default="")
    opik_track_operations: str = Field(default="")
    opik_log_raw_prompts: bool = Field(
        default=False,
        description="DANGER: Log raw LLM prompts bypassing sanitization. Only use for local debugging.",
    )

    # APM Integration (merged from EnhancedObservabilitySettings)
    # COMMUNITY DEFAULT: Disabled (enterprise feature)
    prometheus_enabled: bool = Field(default=False)
    prometheus_pushgateway_url: str = Field(default="http://localhost:9091")
    generic_apm_enabled: bool = Field(default=False)
    generic_apm_url: Optional[str] = Field(default=None)
    generic_apm_api_key: Optional[SecretStr] = Field(default=None)

    # Workspace integration (merged from WorkspaceSettings)
    comet_workspace: Optional[str] = Field(default=None)
    instance_id: str = Field(default="localhost:8090")

    # Performance monitoring (merged from EnhancedObservabilitySettings)
    # COMMUNITY DEFAULT: Basic monitoring only
    enable_performance_monitoring: bool = Field(default=False)
    enable_detailed_tracing: bool = Field(default=False)

    # Basic tracing configuration
    # COMMUNITY DEFAULT: Disabled (enterprise feature)
    tracing_enabled: bool = Field(default=False)
    trace_llm_calls: bool = Field(default=False)
    trace_agent_workflows: bool = Field(default=False)

    # Metrics
    # COMMUNITY DEFAULT: Disabled (enterprise feature)
    metrics_enabled: bool = Field(default=False)
    metrics_port: int = Field(default=9090)

    model_config = {"env_prefix": "", "extra": "ignore"}

    @model_validator(mode="after")
    def validate_raw_prompt_safety(self) -> "ObservabilitySettings":
        """Prevent raw prompt logging unless using a local Opik instance."""
        if self.opik_log_raw_prompts and not self.opik_use_local:
            raise ValueError(
                "OPIK_LOG_RAW_PROMPTS=true requires OPIK_USE_LOCAL=true. "
                "Raw prompt logging is only permitted with local Opik instances."
            )
        return self


class LoggingSettings(BaseSettings):
    """Logging configuration"""

    level: LogLevel = Field(default=LogLevel.INFO, validation_alias="LOG_LEVEL")
    format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        validation_alias="LOG_FORMAT",
    )

    # Structlog format type: 'json' or 'console'
    log_output_format: str = Field(default="json")

    # Log deduplication (prevents repeated log messages)
    log_dedupe: bool = Field(default=True)

    # Buffered logging configuration
    log_buffer_size: int = Field(default=100)
    log_flush_interval: float = Field(default=5.0)

    # Human-readable output (console renderer instead of JSON)
    log_human_readable: bool = Field(default=False)

    # File logging
    log_to_file: bool = Field(default=False)
    log_file_path: str = Field(default="logs/faultmaven.log")
    log_file_max_bytes: int = Field(default=10 * 1024 * 1024)  # 10MB
    log_file_backup_count: int = Field(default=5)

    # Structured logging
    structured_logging: bool = Field(default=True)
    include_trace_id: bool = Field(default=True)

    model_config = {"env_prefix": "", "extra": "ignore"}


class UploadSettings(BaseSettings):
    """File upload configuration"""

    max_upload_size_mb: int = Field(
        default=10,
        description="Maximum file size for uploads (also used as document processing limit)",
    )
    allowed_mime_types: List[str] = Field(
        default=[
            "text/plain",
            "text/csv",
            "application/json",
            "application/xml",
            "text/xml",
            "application/yaml",
        ],
    )
    upload_timeout_seconds: int = Field(default=300)  # 5 minutes
    temp_storage_path: str = Field(default="/tmp/faultmaven")

    model_config = {"env_prefix": "", "extra": "ignore"}


class KnowledgeSettings(BaseSettings):
    """Knowledge base and search configuration"""

    enable_web_search: bool = Field(default=True)
    serp_api_key: Optional[SecretStr] = Field(default=None)
    tavily_api_key: Optional[SecretStr] = Field(default=None)

    # Search limits
    max_search_results: int = Field(
        default=5, validation_alias="KNOWLEDGE_MAX_SEARCH_RESULTS"
    )
    search_timeout_seconds: int = Field(default=30)

    # Document processing (size limit now in UploadSettings.max_upload_size_mb)
    chunk_size: int = Field(default=1000, validation_alias="DOCUMENT_CHUNK_SIZE")
    chunk_overlap: int = Field(default=100, validation_alias="DOCUMENT_CHUNK_OVERLAP")

    model_config = {"env_prefix": "", "extra": "ignore"}


class EmbeddingSettings(BaseSettings):
    """Embedding and vector search configuration for RAG system."""

    # OpenAI Embeddings
    embedding_model: str = Field(
        default="bge-m3",
        description="Embedding model for knowledge base and evidence vectorization",
    )
    embedding_dimensions: int = Field(
        default=1024,
        description="Embedding vector dimensions (1024 for bge-m3)",
    )

    # Embedding API configuration
    embedding_max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum retry attempts for embedding API calls",
    )
    embedding_retry_delay: float = Field(
        default=1.0,
        ge=0.1,
        le=30.0,
        description="Base delay between retries (exponential backoff)",
    )
    embedding_timeout: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Timeout for embedding API calls in seconds",
    )
    embedding_batch_size: int = Field(
        default=100,
        ge=1,
        le=2048,
        description="Number of texts per batch for embedding generation",
    )
    embedding_max_text_length: int = Field(
        default=8191,
        description="Maximum text length for embedding (OpenAI limit)",
    )

    # ChromaDB Vector Store
    chroma_persist_directory: str = Field(
        default="./data/chroma-kb",
        description="Directory for ChromaDB KB persistence",
    )
    chroma_collection_name: str = Field(
        default="knowledge_items",
        description="ChromaDB collection name for knowledge items",
    )

    # Hybrid Search
    semantic_search_weight: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Weight for semantic similarity in hybrid search",
    )
    text_search_weight: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Weight for text search in hybrid search",
    )

    # Indexing Job
    indexing_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Batch size for background indexing job",
    )

    @field_validator("text_search_weight")
    @classmethod
    def validate_weights(cls, v, info):
        """Ensure semantic + text weights don't exceed 1.0"""
        values = info.data
        semantic_weight = values.get("semantic_search_weight", 0.7)
        if semantic_weight + v > 1.0:
            raise ValueError(
                f"semantic_search_weight ({semantic_weight}) + text_search_weight ({v}) "
                f"cannot exceed 1.0"
            )
        return v

    # ML Model Loading Strategy
    lazy_load_ml_models: bool = Field(
        default=True,
        description="If True, ML models (BGE-M3, etc.) are loaded on first use. "
        "If False, models are pre-loaded at startup for warm starts. "
        "Lazy loading improves startup time but first request may be slower.",
    )
    preload_models: list = Field(
        default_factory=lambda: ["BAAI/bge-m3"],
        description="List of model names to pre-load at startup even with lazy_load_ml_models=True. "
        "Default: ['BAAI/bge-m3'] — preloaded so the first request path does not pay a "
        "cold-load penalty (see data-preprocessing-design-specification.md §5.7). "
        "Set to [] (via PRELOAD_MODELS='') to opt out for faster startup, accepting "
        "~60–120s first-request latency while the model loads.",
    )

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
        validation_alias="DEFAULT_INVESTIGATION_STRATEGY",
        description="active_incident (fast, 70% confidence) or post_mortem (thorough, 85% confidence)",
    )

    default_intensity: str = Field(
        default="medium",
        validation_alias="DEFAULT_OODA_INTENSITY",
        description="OODA cycle intensity: light (1-2 iterations), medium (2-4), full (3-6)",
    )

    # Memory Management (4-Tier Hierarchical System)
    hot_memory_tokens: int = Field(
        default=500,
        description="Hot tier: last 2 iterations, full fidelity",
    )

    warm_memory_tokens: int = Field(
        default=300,
        description="Warm tier: iterations 3-5, LLM-summarized",
    )

    cold_memory_tokens: int = Field(
        default=100,
        description="Cold tier: older iterations, key facts only",
    )

    persistent_memory_tokens: int = Field(
        default=100,
        description="Persistent tier: always accessible insights",
    )

    # Phase Control
    enable_phase_skip: bool = Field(
        default=True,
        description="Allow skipping phases in active incident strategy",
    )

    min_confidence_to_advance: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="Minimum confidence required to advance to next phase",
    )

    stall_detection_iterations: int = Field(
        default=3,
        ge=2,
        description="Number of iterations without progress before marking as stalled",
    )

    # Consultant Mode Settings
    problem_signal_threshold: str = Field(
        default="moderate",
        description="Threshold to offer investigation: weak|moderate|strong",
    )

    max_consultant_turns: int = Field(
        default=5,
        ge=1,
        description="Max turns in Consultant mode before suggesting Lead Investigator",
    )

    # Context Management (merged from ConversationThresholds)
    max_clarifications: int = Field(
        default=3,
        ge=0,
        description="Maximum clarifying-question turns before suggesting escalation/progress",
    )
    max_conversation_turns: int = Field(default=20)
    max_conversation_tokens: int = Field(default=4000)

    @field_validator("warm_memory_tokens")
    @classmethod
    def validate_memory_hierarchy_warm(cls, v, info):
        """Ensure WARM <= HOT for memory hierarchy"""
        values = info.data
        hot = values.get("hot_memory_tokens", 500)

        if v > hot:
            raise ValueError(
                f"WARM_MEMORY_TOKENS ({v}) cannot exceed HOT_MEMORY_TOKENS ({hot}). "
                f"Memory quality degrades from hot to cold."
            )
        return v

    @field_validator("cold_memory_tokens")
    @classmethod
    def validate_memory_hierarchy_cold(cls, v, info):
        """Ensure COLD <= WARM for memory hierarchy"""
        values = info.data
        warm = values.get("warm_memory_tokens", 300)

        if v > warm:
            raise ValueError(
                f"COLD_MEMORY_TOKENS ({v}) cannot exceed WARM_MEMORY_TOKENS ({warm}). "
                f"Memory quality degrades from hot to cold."
            )

        if v <= 0:
            raise ValueError(f"COLD_MEMORY_TOKENS must be > 0, got {v}")

        return v

    @field_validator("persistent_memory_tokens")
    @classmethod
    def validate_persistent_memory(cls, v, info):
        """Validate persistent memory is reasonable"""
        if v <= 0:
            raise ValueError(f"PERSISTENT_MEMORY_TOKENS must be > 0, got {v}")
        return v

    def model_post_init(self, __context):
        """Validate total memory budget after all fields loaded"""
        total = (
            self.hot_memory_tokens
            + self.warm_memory_tokens
            + self.cold_memory_tokens
            + self.persistent_memory_tokens
        )

        if total > 5000:
            raise ValueError(
                f"Total OODA memory ({total} tokens) exceeds reasonable budget (5000 tokens). "
                f"Consider reducing individual memory tier allocations."
            )

    model_config = {"env_prefix": "", "extra": "ignore"}


class FeatureSettings(BaseSettings):
    """Feature flags and toggles"""

    use_di_container: bool = Field(default=True)
    use_refactored_services: bool = Field(default=True)
    use_refactored_api: bool = Field(default=True)

    # Token-Aware Context Management
    enable_token_aware_context: bool = Field(default=True)
    enable_conversation_summarization: bool = Field(default=True)
    # Note: Token budgets and thresholds live under settings.ooda

    # DEPRECATED: Query Classification System (Replaced by OODA v3.2.0)
    # These settings are no longer used - OODA uses structured responses instead of classification
    # Will be removed in v4.0.0
    llm_classification_mode: str = Field(default="enhancement", deprecated=True)
    enable_multidimensional_confidence: bool = Field(default=True, deprecated=True)
    pattern_weighted_scoring: bool = Field(default=True, deprecated=True)
    pattern_exclusion_rules: bool = Field(default=True, deprecated=True)
    enable_structure_analysis: bool = Field(default=True, deprecated=True)
    enable_linguistic_analysis: bool = Field(default=True, deprecated=True)
    enable_entity_analysis: bool = Field(default=True, deprecated=True)
    enable_context_analysis: bool = Field(default=True, deprecated=True)
    enable_disambiguation_check: bool = Field(default=True, deprecated=True)

    # Note: Self-correction thresholds moved to ConversationThresholds class above
    # Deprecated: SELF_CORRECTION_THRESHOLD → use ConversationThresholds.self_correction_max_confidence
    # Deprecated: FORCED_CLARIFICATION_THRESHOLD → use ConversationThresholds.confidence_override_threshold

    # Job Runner Configuration
    job_runner_type: str = Field(
        default="inmemory",
        description="Background job scheduler type. Options: 'apscheduler' (production), "
        "'inmemory' (development). APScheduler requires the apscheduler package.",
    )

    # Experimental features
    enable_advanced_reasoning: bool = Field(default=False)
    enable_multi_agent: bool = Field(default=False)
    enable_workflow_optimization: bool = Field(default=False)

    model_config = {"env_prefix": "", "extra": "ignore"}


class ToolsSettings(BaseSettings):
    """Tools and external service configuration"""

    # Web search configuration
    web_search_api_key: Optional[SecretStr] = Field(default=None)
    web_search_api_endpoint: str = Field(
        default="https://www.googleapis.com/customsearch/v1",
    )
    web_search_engine_id: str = Field(default="")
    web_search_max_results: int = Field(default=3)

    model_config = {"env_prefix": "", "extra": "ignore"}


# EnhancedProtectionSettings merged into ProtectionSettings above


# EnhancedObservabilitySettings merged into ObservabilitySettings above


# EnhancedDatabaseSettings merged into DatabaseSettings above
# NOTE: Presidio configuration moved to ProtectionSettings to avoid duplication


class AlertingSettings(BaseSettings):
    """Email and webhook alerting configuration"""

    alert_from_email: Optional[str] = Field(default=None)
    alert_to_emails: str = Field(default="")
    alert_webhook_url: Optional[str] = Field(default=None)

    # SMTP Configuration
    smtp_host: str = Field(default="localhost")
    smtp_port: int = Field(default=587)

    model_config = {"env_prefix": "", "extra": "ignore"}


class WorkspaceSettings(BaseSettings):
    """Workspace and collaboration settings (comet_workspace moved to ObservabilitySettings)"""

    comet_api_key: Optional[SecretStr] = Field(default=None)

    # Feature toggles for experimental features
    enable_experimental_features: bool = Field(default=False)

    model_config = {"env_prefix": "", "extra": "ignore"}


class PreprocessingSettings(BaseSettings):
    """Data preprocessing and chunking configuration"""

    # Chunking thresholds
    chunk_trigger_tokens: int = Field(
        default=8000,
        description="Documents >8K tokens trigger map-reduce chunking",
    )

    # Chunking parameters
    chunk_size_tokens: int = Field(
        default=4000,
        description="Target chunk size for map-reduce (~16KB text)",
    )

    chunk_overlap_tokens: int = Field(
        default=200,
        description="Overlap between chunks for context preservation",
    )

    map_reduce_max_parallel: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum parallel LLM calls during MAP phase",
    )

    # Provider for chunking (defaults to synthesis provider)
    chunking_provider: str = Field(
        default="synthesis",
        description="LLM provider for chunking operations (synthesis, chat, or specific provider)",
    )

    @field_validator("chunk_size_tokens")
    @classmethod
    def validate_chunk_size(cls, v, info):
        """Ensure chunk size is less than trigger"""
        values = info.data
        trigger = values.get("chunk_trigger_tokens", 8000)

        if v >= trigger:
            raise ValueError(
                f"CHUNK_SIZE_TOKENS ({v}) must be < CHUNK_TRIGGER_TOKENS ({trigger}). "
                f"Trigger must activate before reaching chunk size."
            )
        return v

    @field_validator("chunk_overlap_tokens")
    @classmethod
    def validate_chunk_overlap(cls, v, info):
        """Ensure overlap is reasonable percentage of chunk size"""
        values = info.data
        chunk_size = values.get("chunk_size_tokens", 4000)

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


class DeepAnalysisSettings(BaseSettings):
    """Interpreted search configuration.

    Controls LLM-assisted file interpretation during investigation.
    When the agent needs to reason over raw file sections (beyond keyword
    search), a dedicated LLM call interprets the relevant sections in
    isolation with the investigation context.

    The 'local' backend uses the already-configured CHAT_PROVIDER — no
    additional API keys or infrastructure needed. The 'external' backend
    calls a separate microservice (enterprise only).
    """

    backend: str = Field(
        default="local",
        validation_alias="DEEP_ANALYSIS_BACKEND",
        description="Interpreted search backend: external | local | disabled",
    )

    url: str = Field(
        default="",
        validation_alias="DEEP_ANALYSIS_URL",
        description="URL for external deep analysis backend",
    )

    api_key: str = Field(
        default="",
        validation_alias="DEEP_ANALYSIS_API_KEY",
        description="API key for external deep analysis backend",
    )

    timeout_seconds: int = Field(
        default=30,
        validation_alias="DEEP_ANALYSIS_TIMEOUT_SECONDS",
        ge=5,
        le=120,
        description="Timeout for deep analysis calls",
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class AgentSettings(BaseSettings):
    """Agent orchestration configuration (TASK-015).

    Controls the behavior of AI agent execution including:
    - LLM retry logic and timeouts
    - Tool execution limits
    - Token budget defaults
    - Agent-specific model selection

    Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
    """

    # LLM Provider for Agent Orchestration
    agent_provider: str = Field(
        default="anthropic",
        validation_alias="AGENT_LLM_PROVIDER",
        description="LLM provider for agent execution (anthropic, openai)",
    )

    agent_model: str = Field(
        default="claude-sonnet-4-20250514",
        validation_alias="AGENT_LLM_MODEL",
        description="Model to use for agent execution",
    )

    # Retry configuration
    max_retries: int = Field(
        default=3,
        validation_alias="AGENT_MAX_RETRIES",
        ge=0,
        le=10,
        description="Maximum retry attempts for LLM calls",
    )

    retry_initial_delay: float = Field(
        default=1.0,
        validation_alias="AGENT_RETRY_INITIAL_DELAY",
        ge=0.1,
        le=30.0,
        description="Initial delay for exponential backoff (seconds)",
    )

    # Tool execution configuration
    tool_timeout: int = Field(
        default=30,
        validation_alias="AGENT_TOOL_TIMEOUT",
        ge=5,
        le=300,
        description="Timeout for tool execution (seconds)",
    )

    max_parallel_tools: int = Field(
        default=5,
        validation_alias="AGENT_MAX_PARALLEL_TOOLS",
        ge=1,
        le=20,
        description="Maximum parallel tool executions",
    )

    # LLM Request configuration
    agent_max_tokens: int = Field(
        default=4096,
        ge=100,
        le=128000,
        description="Maximum tokens for agent responses",
    )

    agent_temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="Temperature for agent responses",
    )

    agent_request_timeout: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Request timeout for LLM calls (seconds)",
    )

    # Token budget defaults
    default_session_token_budget: Optional[int] = Field(
        default=None,
        description="Default token budget for new sessions (None = unlimited)",
    )

    # Execution limits
    max_tool_calls_per_execution: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum tool calls per single execution",
    )

    max_iterations_per_execution: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum LLM iterations (tool call loops) per execution",
    )

    # Vectorization configuration (scenario-driven data processing)
    vectorization_min_size_bytes: int = Field(
        default=50_000,
        ge=1000,
        le=10_000_000,
        description="Minimum file size in bytes for auto-vectorization eligibility",
    )

    vectorization_reactive_timeout_seconds: int = Field(
        default=180,
        ge=30,
        le=600,
        description=(
            "Upper bound for synchronous reactive vectorization inside a "
            "DA tool loop. Reactive vectorize is triggered on tool failure "
            "signals (timeout, repeated empty searches, low confidence); "
            "the agent waits for it before continuing. BGE-M3 encode on "
            "CPU for large structural indexes can take 120-180 s, so the "
            "default is sized generously but still below agent_request_timeout "
            "(120 s → note: operators on CPU-only hardware may need to raise "
            "both settings together). Proactive vectorization is "
            "intentionally UNBOUNDED at this layer — it's a background task "
            "and time-bounding it only guarantees wasted work when encode "
            "outlasts the bound (see _vectorize_evidence comment)."
        ),
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class ProviderSettings(BaseSettings):
    """Provider selection configuration (doc-aligned selectors - PR #3).

    Unified provider selection using deployment strategy vocabulary:
    - tenant_provider: Tenant isolation strategy (single/multi)
    - db_backend: Database backend (sqlite/postgres)
    - cache_backend: Cache backend (memory/redis)
    - vector_backend: Vector database backend (chroma/pinecone)
    - storage_backend: File storage backend (filesystem/s3)
    """

    # Tenant isolation strategy
    tenant_provider: TenantProvider = Field(
        default=TenantProvider.SINGLE,
        description="Tenant isolation: 'single' (local/community) or 'multi' (cloud/enterprise)",
    )

    # Database backend
    db_backend: DbBackend = Field(
        default=DbBackend.SQLITE,
        description="Database backend: 'sqlite' (local) or 'postgres' (production)",
    )

    # Cache backend
    cache_backend: CacheBackend = Field(
        default=CacheBackend.MEMORY,
        description="Cache backend: 'memory' (local) or 'redis' (production)",
    )

    # Vector database backend
    vector_backend: VectorBackend = Field(
        default=VectorBackend.CHROMA,
        description="Vector DB backend: 'chroma' (local/cloud) or 'pinecone' (cloud)",
    )

    # File storage backend
    storage_backend: StorageBackend = Field(
        default=StorageBackend.FILESYSTEM,
        description="File storage: 'filesystem' (local) or 's3' (cloud)",
    )

    # Metrics exporter (PR #5 - observability neutrality)
    metrics_exporter: MetricsExporter = Field(
        default=MetricsExporter.NONE,
        description="Metrics exporter: 'none' (default, no /metrics) or 'prometheus_http' (mount /metrics)",
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class EvidenceStorageSettings(BaseSettings):
    """Evidence storage configuration for file uploads and management.

    Controls the behavior of evidence artifact storage including:
    - Storage location (local filesystem)
    - File size limits
    - Allowed MIME types for security
    - Future: Cloud storage configuration (S3, Azure, GCS)

    Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
    """

    # Storage root directory for evidence files
    evidence_storage_root: str = Field(
        default="./data/evidence",
        description="Root directory for evidence file storage",
    )

    # Maximum file size (100MB default)
    max_evidence_file_size: int = Field(
        default=100 * 1024 * 1024,
        ge=1024,  # Minimum 1KB
        le=1024 * 1024 * 1024,  # Maximum 1GB
        description="Maximum evidence file size in bytes (default 100MB)",
    )

    # Allowed MIME types (empty list = allow all)
    allowed_evidence_mime_types: List[str] = Field(
        default=[],
        description="Allowed MIME types for evidence files (empty = allow all)",
    )

    # Common evidence MIME types (for reference/documentation)
    # Images: image/png, image/jpeg, image/gif, image/webp, image/svg+xml
    # Logs: text/plain, application/json, application/xml, text/csv
    # Archives: application/zip, application/gzip, application/x-tar
    # Network: application/octet-stream (for HAR, pcap files)
    # Videos: video/mp4, video/webm, video/quicktime
    # Documents: application/pdf, text/html

    # Cloud storage configuration (S3)
    # These settings are used when STORAGE_BACKEND=s3
    s3_bucket_name: Optional[str] = Field(
        default=None,
        description="S3 bucket name for evidence storage (required if STORAGE_BACKEND=s3)",
    )
    s3_region: str = Field(
        default="us-east-1",
        description="AWS region for S3 bucket",
    )
    s3_key_prefix: str = Field(
        default="evidence/",
        description="Key prefix for S3 object keys",
    )
    s3_endpoint_url: Optional[str] = Field(
        default=None,
        description="Custom S3 endpoint URL (for S3-compatible services like MinIO)",
    )

    # Future: Additional cloud storage backends
    # azure_container: Optional[str] = Field(default=None)
    # gcs_bucket: Optional[str] = Field(default=None)

    # Orphan-file cleanup (PLAN-evidence-failure-modes-implementation.md §M1)
    orphan_cleanup_enabled: bool = Field(
        default=False,
        validation_alias="ORPHAN_CLEANUP_ENABLED",
        description=(
            "When True, the storage_cleanup job deletes stored files whose "
            "sidecar metadata shows linked=False and uploaded_at older than "
            "orphan_file_ttl_hours. Default False — opt in explicitly after "
            "a 48h dry-run canary."
        ),
    )
    orphan_file_ttl_hours: int = Field(
        default=24,
        ge=1,
        le=720,  # 30 days max
        validation_alias="ORPHAN_FILE_TTL_HOURS",
        description=(
            "Age threshold in hours for orphan-file deletion. Files younger "
            "than this are never deleted, even if linked=False (they may be "
            "in-flight uploads)."
        ),
    )
    orphan_cleanup_dry_run: bool = Field(
        default=True,
        validation_alias="ORPHAN_CLEANUP_DRY_RUN",
        description=(
            "When True (default), cleanup logs 'would delete' without "
            "deleting. Flip to False only after a clean 48h dry-run period "
            "per the M1 canary protocol."
        ),
    )

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

    # Provider Selectors (PR #3 - doc-aligned vocabulary)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)

    # Nested configuration sections
    server: ServerSettings = Field(default_factory=ServerSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    case: CaseSettings = Field(default_factory=CaseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    protection: ProtectionSettings = Field(default_factory=ProtectionSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # OODA Framework v3.2.0
    ooda: OODASettings = Field(default_factory=OODASettings)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }
    upload: UploadSettings = Field(default_factory=UploadSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    preprocessing: PreprocessingSettings = Field(default_factory=PreprocessingSettings)
    deep_analysis: DeepAnalysisSettings = Field(default_factory=DeepAnalysisSettings)

    # Enhanced configuration sections merged into main sections above
    # enhanced_protection merged into protection above
    # enhanced_observability merged into observability above
    # enhanced_database merged into database above
    alerting: AlertingSettings = Field(default_factory=AlertingSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)

    # Evidence storage configuration (TASK-013)
    evidence_storage: EvidenceStorageSettings = Field(
        default_factory=EvidenceStorageSettings
    )

    # Agent orchestration configuration (TASK-015)
    agent: AgentSettings = Field(default_factory=AgentSettings)

    # Convenience properties for evidence storage (used by ServiceFactory)
    @property
    def evidence_storage_root(self) -> str:
        """Get evidence storage root directory."""
        return self.evidence_storage.evidence_storage_root

    @property
    def max_evidence_file_size(self) -> int:
        """Get maximum evidence file size in bytes."""
        return self.evidence_storage.max_evidence_file_size

    @property
    def allowed_evidence_mime_types(self) -> List[str]:
        """Get allowed evidence MIME types."""
        return self.evidence_storage.allowed_evidence_mime_types

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "validate_assignment": True,
        "use_enum_values": True,
        "extra": "ignore",  # Allow extra environment variables
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
        if self.session.heartbeat_interval_seconds >= (
            self.session.timeout_minutes * 60
        ):
            issues.append("Heartbeat interval must be less than session timeout")

        # CORS validation
        browser_origins = [
            "chrome-extension://*",
            "moz-extension://*",
            "http://localhost:3333",
        ]
        missing_origins = []
        for origin in browser_origins:
            if not any(
                origin in allowed for allowed in self.security.cors_allow_origins
            ):
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
            warnings.append(
                "Rate limiting disabled - frontend expects rate limit headers"
            )

        # Upload size warnings
        if self.upload.max_upload_size_mb > 50:
            warnings.append("Upload size > 50MB may cause timeout or processing issues")

        return {"compatible": len(issues) == 0, "issues": issues, "warnings": warnings}

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

    def get_active_preset(self) -> Optional[str]:
        """Get the name of the currently active configuration preset."""
        import os

        return os.getenv("CONFIG_PRESET")

    def get_configuration_summary(self) -> Dict[str, Any]:
        """Get a summary of the current configuration for debugging/display."""
        from .presets import get_preset_info

        return {
            "preset": get_preset_info(),
            "environment": self.server.environment.value,
            "tenant_provider": self.providers.tenant_provider,
            "storage": {
                "session": self.database.session_storage_type,
                "vector": self.database.vector_storage_type,
                "case": self.database.case_storage_type,
                "user": self.database.user_storage_type,
            },
            "llm_provider": self.llm.provider.value if self.llm.provider else "not_set",
            "protection_enabled": self.protection.protection_enabled,
            "rate_limit_enabled": self.security.rate_limit_enabled,
        }


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

    Configuration Loading Order:
    1. Load .env file (if exists)
    2. Apply preset defaults (if CONFIG_PRESET is set or zero-config detected)
    3. Environment variables override preset defaults
    4. Validate final configuration

    Zero-Config Mode:
    - If no .env file and no API keys set, auto-applies 'local' preset
    - Uses in-memory storage and local LLM (Ollama) for quick startup
    - No external dependencies required

    Raises:
        ConfigurationError: If settings validation fails
    """
    global _settings_instance
    if _settings_instance is None:
        try:
            # Ensure .env file is loaded before creating settings
            import os

            from dotenv import load_dotenv

            # Load .env without overriding existing environment variables.
            # This preserves the standard precedence order: OS env > .env.
            load_dotenv()

            # Apply preset defaults for zero-config experience
            # Presets are applied AFTER .env but BEFORE settings instantiation
            # This allows env vars to override preset values
            from .presets import (
                ensure_preset_applied,
                get_current_preset_name,
                validate_preset_requirements,
            )

            ensure_preset_applied()

            # Validate preset requirements (e.g., API keys for selected provider)
            preset_errors = validate_preset_requirements()
            if preset_errors:
                import logging

                logger = logging.getLogger(__name__)
                for error in preset_errors:
                    logger.warning(f"Preset configuration warning: {error}")

            import logging

            logger = logging.getLogger(__name__)
            preset_name = get_current_preset_name()
            if preset_name:
                logger.info(f"Settings loading with preset '{preset_name}'")

            # When running tests, allow bypassing .env file to prevent credential leakage
            if os.getenv("FAULTMAVEN_SKIP_DOTENV"):
                _settings_instance = FaultMavenSettings(_env_file=None)
            else:
                _settings_instance = FaultMavenSettings()
        except Exception as e:
            from faultmaven.models.exceptions import ConfigurationError

            raise ConfigurationError(
                f"Settings initialization failed: {e}",
                error_code="SETTINGS_INIT_ERROR",
                context={"original_error": str(e), "error_type": type(e).__name__},
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
            bridge.get("server.port") -> 8090
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
