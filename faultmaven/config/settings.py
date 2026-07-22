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
import secrets
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

    # Optional dotted path to a custom LLMRouter implementation (e.g. cloud's
    # MultiTenantLLMRouter). When unset, OSS LLMRouter is used.
    router_class: Optional[str] = Field(
        default=None, validation_alias="LLM_ROUTER_CLASS"
    )

    # Task-specific provider selection.
    # NOTE: this default is a non-load-bearing placeholder so Settings() can be
    # constructed in tests/tools without env. There is NO real default provider:
    # app startup requires CHAT_PROVIDER to be explicitly set AND its credential
    # present, and hard-fails otherwise (see config/llm_validation.py). Do not
    # rely on this value — a deployment that never sets CHAT_PROVIDER refuses to
    # boot rather than silently picking a provider for which it has no key.
    provider: LLMProvider = Field(
        default=LLMProvider.OPENAI, validation_alias="CHAT_PROVIDER"
    )
    multimodal_provider: Optional[LLMProvider] = Field(default=None)
    synthesis_provider: Optional[LLMProvider] = Field(default=None)
    classifier_provider: Optional[LLMProvider] = Field(default=None)
    code_provider: Optional[LLMProvider] = Field(default=None)
    da_provider: Optional[LLMProvider] = Field(default=None)
    knowledge_provider: Optional[LLMProvider] = Field(default=None)
    # Force structured-output investigation calls (Pydantic-schema generation
    # in milestone_engine) through a known-STRICT-capable provider regardless
    # of CHAT_PROVIDER. Companion to the capability-routing fix
    # (PR fix/structured-output-capability-routing): operators running a
    # weak-structured-output CHAT_PROVIDER (Fireworks/MiniMax, Local Ollama,
    # etc.) can route just the schema-bound calls to e.g. gemini-2.5-pro or
    # gpt-4o without changing their default chat provider. Cheap providers
    # still handle synthesis / chat / classification where strict schema
    # enforcement isn't needed.
    structured_output_provider: Optional[LLMProvider] = Field(default=None)

    # Allow the investigation engine to run on a model that does NOT support
    # tool calling. Tool calling powers Directed Analysis (search_file,
    # deep_analysis) — the engine's evidence-gathering. Without it the engine
    # can't reach the evidence yet still emits conclusions, which risks the
    # premature/incorrect conclusion FaultMaven guarantees against. So the
    # startup gate (config/investigation_capability.py) refuses to boot when the
    # resolved investigation model (DA → CHAT) is tool-incapable, UNLESS this is
    # explicitly set — a knowing opt-in to degraded/offline mode (e.g. a local
    # model), never an accident. The per-turn runtime fallback still applies.
    allow_toolless_investigation: bool = Field(
        default=False,
        validation_alias="ALLOW_TOOLLESS_INVESTIGATION",
    )

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
    groq_multimodal_model: Optional[str] = Field(default=None)
    groq_synthesis_model: Optional[str] = Field(default=None)
    groq_classifier_model: Optional[str] = Field(default=None)
    groq_code_model: Optional[str] = Field(default=None)
    groq_da_model: Optional[str] = Field(default=None)
    groq_knowledge_model: Optional[str] = Field(default=None)

    # Default chat model per provider (the effective default when the user does
    # not pin a model). Canonical set = docs/CLAUDE.md "Supported LLM Providers".
    # Keep this, registry.py default_model, and .env.example in sync —
    # scripts/check_env_example_sync.py enforces all three (CI + pre-commit).
    # Defaults are performance-weighted (token-usage billing → quality drives UX),
    # all tool-calling + large-context capable. HuggingFace is the exception: its
    # Inference API can't do tool calling, so it is kept but not recommended.
    openai_model: str = Field(default="gpt-5.4-mini")
    anthropic_model: str = Field(default="claude-sonnet-4-6")
    fireworks_model: str = Field(
        default="accounts/fireworks/models/deepseek-v4-flash",
    )
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    cohere_model: str = Field(default="command-r-plus")
    gemini_model: str = Field(default="gemini-3.5-flash")
    huggingface_model: str = Field(default="mistralai/Mistral-Large-Instruct-2411")
    openrouter_model: str = Field(default="anthropic/claude-sonnet-4-6")

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

    # Per-provider request_timeout overrides. Some providers/models are
    # systematically slower than others (e.g. Fireworks DeepSeek V4 Pro can
    # exceed 90s on schema-forced tool-loop iterations; local Ollama on
    # CPU can take 5+ min). Mapping {provider_name: timeout_seconds}; any
    # provider not listed falls back to ``request_timeout``.
    #
    # Set via env as JSON, e.g.:
    #   LLM_PROVIDER_TIMEOUT_OVERRIDES='{"fireworks": 180, "ollama": 600}'
    #
    # Surfaced as a tunable per a 2026-05-01 system code review (a
    # DeepSeek run on text-paste-stacktrace q6 hit the 90s ceiling).
    provider_timeout_overrides: Dict[str, int] = Field(
        default_factory=dict,
        validation_alias="LLM_PROVIDER_TIMEOUT_OVERRIDES",
        description=(
            "Per-provider timeout overrides in seconds. JSON object keyed "
            "by provider name (e.g. 'fireworks', 'gemini', 'ollama'). "
            "Empty default — providers fall back to request_timeout."
        ),
    )

    def timeout_for_provider(self, provider_name: Optional[str]) -> int:
        """Return the per-provider timeout if set, else the global default.

        Centralised so callers don't have to dict-lookup + fall back themselves.
        Empty / unknown provider names return ``request_timeout`` unchanged.
        """
        if not provider_name:
            return self.request_timeout
        return self.provider_timeout_overrides.get(provider_name, self.request_timeout)

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
        description="Maximum tokens for phase handler and tool responses",
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

    def get_structured_output_provider(self) -> LLMProvider:
        """Get provider for schema-bound investigation calls.

        Returns the explicit ``STRUCTURED_OUTPUT_PROVIDER`` override if set
        (lets operators route schema-bound calls to a known-STRICT-capable
        provider while keeping a cheaper CHAT_PROVIDER for everything else),
        otherwise falls back to the chat provider — preserving current
        behavior when the override is unset.
        """
        return self.structured_output_provider or self.provider

    def get_structured_output_model(self) -> str:
        """Get model for the structured-output provider using task-specific configuration."""
        provider = self.get_structured_output_provider()
        return self._get_model_for_provider_and_task(provider, "structured_output")

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
            LLMProvider.GROQ: self.groq_model,
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
    # ChromaDB Configuration
    # ============================================
    # Default "localhost" → the embedded PersistentClient (no network), matching
    # the documented "PersistentClient by default" local model and the KB/case
    # vector stores. Any non-"localhost" host opts the ingestion service into an
    # HTTP client: cloud/k8s deployments set CHROMADB_HOST to the in-cluster
    # ChromaDB service (see faultmaven-enterprise-infra configmap). Same core,
    # different configuration — local must not reach for a remote ChromaDB.
    chromadb_host: str = Field(default="localhost")
    chromadb_port: int = Field(default=8000)
    # Max seconds to wait for an external (HTTP) ChromaDB to answer the initial
    # connect/heartbeat. On timeout the ingestion service degrades (KB disabled)
    # instead of blocking startup. Ignored for the local PersistentClient.
    chromadb_connect_timeout: float = Field(
        default=5.0, validation_alias="CHROMADB_CONNECT_TIMEOUT"
    )
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

    # KB pack: a self-contained, replaceable bundle of shipped runbooks +
    # build-time embeddings (see docs + faultmaven/bootstrap/kb_pack.py). Empty
    # default → the baseline pack bundled in the image at
    # resources/knowledge/pack. Override (KB_PACK_DIR) points at an external,
    # replaceable pack so the KB can be updated offline WITHOUT rebuilding the
    # app image: local bind-mounts a host dir; cloud has an init container
    # populate it from object storage. Same core, different configuration.
    kb_pack_dir: str = Field(default="", validation_alias="KB_PACK_DIR")

    # KB cross-store repair bounds (bootstrap self-heals an orphaned row — a
    # knowledge_items row whose ChromaDB vectors went missing after a crash
    # between the SQL commit and the vector write — by re-embedding it with
    # BGE-M3). Both bounds cap that work; defaults live in
    # faultmaven/bootstrap/kb_init.py (KB_REPAIR_MAX_ROWS / KB_REPAIR_MAX_CHUNKS).
    kb_repair_max_rows: int = Field(
        default=25,
        validation_alias="KB_REPAIR_MAX_ROWS",
        description=(
            "Max orphaned KB rows to repair per boot before treating the set as a "
            "bulk-loss anomaly (repair nothing + warn; recover via a full pack "
            "re-ingest)."
        ),
    )
    kb_repair_max_chunks: int = Field(
        default=60,
        validation_alias="KB_REPAIR_MAX_CHUNKS",
        description=(
            "Per-boot embedding-work budget (chunks) for KB orphan repair. When the "
            "web-startup bootstrap runs the repair (single-tenant provider; under "
            "TENANT_PROVIDER=multi it runs off the readiness path in the kb_seed "
            "job), it embeds on the startup path before readiness — rows past this "
            "budget defer to the next boot. Lower it if a tight k8s startupProbe "
            "leaves too little time for the model load + re-embed."
        ),
    )

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


def ensure_local_jwt_secret_env() -> None:
    """Ensure an HS256 JWT secret exists for standalone (local-auth) startup.

    Local auth requires a JWT secret. Rather than make the user set one (or ship a
    shared dev secret — which would let every install forge each other's tokens),
    generate a unique secret once on first run, persist it to data/.jwt_secret, and
    export it as JWT_SECRET_KEY so the settings pick it up.

    Called once from get_settings() (after load_dotenv, before settings are
    constructed) — deliberately NOT a per-field default_factory, so there's no
    repeated or racy filesystem I/O on every settings instantiation.

    No-ops when:
      - JWT_SECRET_KEY is already set (an explicit value always wins), or
      - AUTH_MODE is not 'local' (OAuth uses RS256 key files, not this secret).
    On a filesystem error it logs a warning and returns — local auth then fails
    with a clear "JWT_SECRET_KEY not configured" message rather than the server
    crashing at import time.
    """
    if os.environ.get("JWT_SECRET_KEY"):
        return
    if os.environ.get("AUTH_MODE", "local").strip().lower() != "local":
        return

    logger = logging.getLogger(__name__)
    secret_path = Path(os.environ.get("JWT_SECRET_FILE", "data/.jwt_secret"))
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        value = (
            secret_path.read_text(encoding="utf-8").strip()
            if secret_path.exists()
            else ""
        )
        if not value:
            value = secrets.token_urlsafe(48)
            secret_path.write_text(value, encoding="utf-8")
            try:
                secret_path.chmod(0o600)
            except OSError as exc:
                logger.debug("Could not chmod %s to 0600: %s", secret_path, exc)
        os.environ["JWT_SECRET_KEY"] = value
    except OSError as exc:
        logger.warning(
            "Could not generate/persist a local JWT secret at %s (%s); set "
            "JWT_SECRET_KEY in .env if local auth fails to start.",
            secret_path,
            exc,
        )


class SecuritySettings(BaseSettings):
    """Security and authentication configuration"""

    # JWT configuration (RS256 for production-ready asymmetric encryption)
    # For development/testing, HS256 with jwt_secret_key is also supported
    jwt_algorithm: str = Field(default="RS256")
    jwt_private_key_path: Optional[str] = Field(default=None)
    jwt_public_key_path: Optional[str] = Field(default=None)
    jwt_private_key: Optional[SecretStr] = Field(default=None)
    jwt_public_key: Optional[str] = Field(default=None)
    # HS256 secret for local auth. In local mode get_settings() auto-generates and
    # persists it (data/.jwt_secret) via ensure_local_jwt_secret_env() before the
    # settings are built, so a standalone install needs no JWT_SECRET_KEY — set the
    # env var to override. OAuth/RS256 ignores this.
    jwt_secret_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias="JWT_SECRET_KEY",
        description="HS256 secret for local auth; auto-generated+persisted in local mode by get_settings() if unset (override via JWT_SECRET_KEY). Unused in OAuth/RS256 mode.",
    )
    jwt_access_token_expire_minutes: int = Field(default=15)
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

    # WorkOS AuthKit SSO (cloud/oauth only; absent in standalone). Selects and
    # configures the hosted IdP for the cloud sign-in flow (ADR-015). FaultMaven
    # mints its own session — these are consumed only to authenticate the user.
    workos_api_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias="WORKOS_API_KEY",
        description="WorkOS API key (secret). Cloud/oauth mode only.",
    )
    workos_client_id: Optional[str] = Field(
        default=None,
        validation_alias="WORKOS_CLIENT_ID",
        description="WorkOS client ID (public). Cloud/oauth mode only.",
    )
    workos_redirect_uri: Optional[str] = Field(
        default=None,
        validation_alias="WORKOS_REDIRECT_URI",
        description=(
            "Registered WorkOS redirect URI (the SSO callback). Must match the "
            "WorkOS dashboard entry exactly. Cloud/oauth mode only."
        ),
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
        default=15,
        validation_alias="JWT_ACCESS_TOKEN_EXPIRY",
        description="Access token expiry (minutes); short-lived per security posture (<30 min)",
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

    @property
    def sso_configured(self) -> bool:
        """True when WorkOS AuthKit SSO is fully configured for cloud/oauth mode.

        Gates whether the SSO provider is built and (later) whether the SSO
        router mounts and ``/auth/config`` advertises a hosted login URL. Standalone
        (``auth_mode=local``) is always False regardless of any WORKOS_* values.
        """
        return (
            self.auth_mode == AuthMode.OAUTH
            and self.workos_api_key is not None
            and bool(self.workos_api_key.get_secret_value())
            and bool(self.workos_client_id)
            and bool(self.workos_redirect_uri)
        )

    model_config = {"env_prefix": "", "extra": "ignore"}


class ProtectionSettings(BaseSettings):
    """Unified protection configuration - PII, behavioral, and ML protection"""

    # Basic Protection Control
    # COMMUNITY DEFAULT: Disabled (enterprise feature - requires Presidio)
    protection_enabled: bool = Field(default=False)
    # Fail-CLOSED by default (#654): when PII redaction is enabled but Presidio
    # is unavailable, refuse to pass un-analyzed text to an external provider
    # rather than leak it. Operators who prefer availability over the privacy
    # guarantee can set PROTECTION_FAIL_OPEN=true.
    fail_open: bool = Field(default=False, validation_alias="PROTECTION_FAIL_OPEN")
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

    # Indexing Job
    indexing_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Batch size for background indexing job",
    )

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


class FeatureSettings(BaseSettings):
    """Feature flags and toggles"""

    # Token-Aware Context Management
    enable_token_aware_context: bool = Field(default=True)
    enable_conversation_summarization: bool = Field(default=True)

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

    # KB cause seeder: on the symptom-verified transition, instantiate a
    # retrieved runbook's metadata["causes"] chains as CANDIDATE causal-graph
    # nodes/edges/hypotheses (a prior, never VALIDATED without case evidence),
    # and switch the KNOWLEDGE & RUNBOOK AUTHORITY prompt to the
    # validate/refute-seeded-candidates variant. When False, the engine keeps the
    # flat "matched runbook → one hypothesis" prompt path and seeds nothing.
    # On by default: the flag-ON enabling eval cleared its soundness gate on the
    # hardest provider (candidate-only, evidence-less, provenance-blind seeds
    # cannot reach VALIDATED or collapse an investigation). The flag is retained
    # as the prod kill switch (FAULTMAVEN_KB_CAUSE_SEEDER=false) and as the tested
    # flag-OFF byte-identical no-op path; it is removed only as the final adoption
    # step. See docs/architecture/knowledge-and-ai/kb-cause-seeder.md.
    kb_cause_seeder_enabled: bool = Field(
        default=True,
        validation_alias="FAULTMAVEN_KB_CAUSE_SEEDER",
        description=(
            "Feature flag: seed retrieved runbook metadata['causes'] chains as "
            "CANDIDATE causal-graph nodes/hypotheses at the symptom-verified "
            "transition (+ the seeded-candidate AUTHORITY prompt variant)."
        ),
    )

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

    # Phase 1: Classifier confidence marker in agent context.
    # When True, the context_builder attaches `confidence="low"` to the
    # <evidence> XML tag for evidence whose classifier confidence falls
    # below LOW_CONFIDENCE_THRESHOLD (see
    # faultmaven.core.preprocessing.evidence_metadata). The agent prompt
    # instructs the model to treat low-confidence extractions as tentative.
    # Default OFF — ships dark; enable explicitly in deployment config.
    confidence_marker_enabled: bool = Field(
        default=False,
        validation_alias="FAULTMAVEN_PREPROCESSING_CONFIDENCE_MARKER",
        description=(
            "Phase 1 feature flag: surface classifier confidence as a "
            "low-confidence marker on the <evidence> XML tag."
        ),
    )

    # Phase 1.5: User-driven reclassification of persisted evidence.
    # When True, the PATCH
    # ``/api/v1/cases/{case_id}/evidence/{evidence_id}/classification``
    # endpoint is live and the ``reclassify_evidence`` agent tool is
    # registered + exposed to the LLM (with a prompt rule teaching the
    # model when to call it). When False, the endpoint returns 404 and
    # the tool is not registered — matching Phase 0 behaviour.
    # Default OFF — ships dark.
    reclassify_enabled: bool = Field(
        default=False,
        validation_alias="FAULTMAVEN_RECLASSIFY_ENABLED",
        description=(
            "Phase 1.5 feature flag: enable user-driven reclassification "
            "of persisted evidence (PATCH endpoint + reclassify_evidence "
            "agent tool + prompt rule)."
        ),
    )

    # Phase 2: Alt-extractor fallback on degenerate sanity-check output.
    # When True, ``PreprocessingService.classify_and_extract`` runs
    # ``ExtractionSanityCheck`` after each extraction and, on failure,
    # retries with alternative extractors from
    # ``classification.suggested_types`` (or a conservative fallback
    # chain when suggested_types is empty). Bounded at 2 retries; if all
    # candidates fail sanity, the direct-truncation fallback is used.
    # Each attempt is recorded in ``metadata.extractor.attempts`` for
    # observability. Default OFF — ships dark; single-shot dispatch
    # remains the default and the retry loop is opt-in per environment.
    extractor_retry_enabled: bool = Field(
        default=False,
        validation_alias="FAULTMAVEN_PREPROCESSING_EXTRACTOR_RETRY",
        description=(
            "Phase 2 feature flag: run a sanity check after each "
            "extraction and retry with an alternative extractor when "
            "the output looks degenerate."
        ),
    )

    # Phase 3c: Context-builder rerank on time-window queries. When
    # True, ``context_builder._build_evidence_context`` inspects the
    # user's current-turn message for a simple time-range pattern and,
    # if found, boosts Tier A ranking for evidence whose
    # ``coverage_*_ts`` intersects the window. Pure reordering — no
    # evidence is dropped, no new evidence is fetched. When OFF, the
    # Tier A ranking matches Phase 2 behaviour (recency-scored). Default
    # OFF — ships dark; enable explicitly after the Phase 3b tool has
    # produced a week of production data on what time patterns users
    # actually ask about.
    timeline_rerank_enabled: bool = Field(
        default=False,
        validation_alias="FAULTMAVEN_TIMELINE_RERANK",
        description=(
            "Phase 3c feature flag: boost Tier A ranking for evidence "
            "whose coverage window matches a time range mentioned in "
            "the current user turn."
        ),
    )

    # Phase 4: Case-level entity registry. When True, the preprocessor
    # runs the per-data-type ``EntityExtractor`` after each successful
    # extraction and writes the results into the ``case_entities`` table
    # via ``CaseRepository.upsert_case_entities``. Writes are bounded by
    # a hard cap of 500 rows per (evidence, entity_type); overflow is
    # recorded in ``evidence.metadata.entities.overflow_types`` so the
    # agent knows the registry is incomplete for that pair. When False,
    # no entity rows are written and the Phase 4c lookup tools / context
    # builder auto-inject find nothing — matching pre-Phase-4 behaviour.
    # Default OFF — ships dark; enable explicitly in deployment config.
    entity_registry_enabled: bool = Field(
        default=False,
        validation_alias="FAULTMAVEN_ENTITY_REGISTRY",
        description=(
            "Phase 4 feature flag: extract entities during preprocessing "
            "and write them to the case_entities registry."
        ),
    )

    # Phase 4: Hard cap on entity rows per (evidence, entity_type).
    # Bounds pathological growth on huge logs files — a single line-
    # storm of unique IPs shouldn't be able to balloon the registry.
    # When exceeded, the extractor truncates to the top-N by mention
    # count and records the overflow in ``evidence.metadata``.
    entity_registry_cap_per_type: int = Field(
        default=500,
        ge=1,
        le=10_000,
        validation_alias="FAULTMAVEN_ENTITY_REGISTRY_CAP",
        description=(
            "Phase 4: max rows the registry accepts per "
            "(evidence, entity_type). Excess rows are dropped and the "
            "type is listed under metadata.entities.overflow_types."
        ),
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


class InvestigationContextSettings(BaseSettings):
    """Token-budget caps for the LLM context window assembly.

    These knobs control how much evidence + extract content is packaged
    into each LLM turn. Defaults are tuned for prose / mixed pages; ops
    may want a larger ``max_chars_per_item`` for dashboard-style page
    captures with many small panels (a Grafana dashboard with 12 panels
    averaging 600 chars each loses half its content under the 4000-char
    default, since the per-item slice is content-shape-blind).

    Surfaced as a tunable per a 2026-05-01 system code review (review
    finding "MEDIUM 4: per-item cap content-shape-blind").
    """

    recent_count: int = Field(
        default=3,
        ge=1,
        le=20,
        validation_alias="EVIDENCE_CONTEXT_RECENT_COUNT",
        description="How many most-recent data evidence items get full file_extract (Tier A).",
    )

    max_chars_per_item: int = Field(
        default=4000,
        ge=500,
        le=200_000,
        validation_alias="EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM",
        description=(
            "Per-item cap on file_extract chars after rerank. Increase for "
            "dashboard-style captures with many small panels."
        ),
    )

    max_total_chars: int = Field(
        default=16000,
        ge=2000,
        le=400_000,
        validation_alias="EVIDENCE_CONTEXT_MAX_TOTAL_CHARS",
        description=(
            "Fallback hard cap on the entire <evidence_collected> block, used "
            "when the active provider/model is unknown. When the model IS known, "
            "the effective cap is model-aware (see evidence_budget_fraction)."
        ),
    )

    evidence_budget_fraction: float = Field(
        default=0.6,
        gt=0.0,
        le=1.0,
        validation_alias="EVIDENCE_CONTEXT_BUDGET_FRACTION",
        description=(
            "Fraction of the model's whole-prompt token budget "
            "(get_token_budget_for_provider) allotted to the <evidence_collected> "
            "block. Lets the evidence cap scale with the model's context window "
            "instead of a single fixed char count. Only applies when "
            "provider_name is supplied; otherwise max_total_chars is used."
        ),
    )

    current_turn_reserve_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias="EVIDENCE_CONTEXT_CURRENT_TURN_RESERVE_FRACTION",
        description=(
            "Fraction of the effective evidence budget reserved for "
            "current-turn items (files/evidence created this turn) so a fresh "
            "upload is always rendered in full and never evicted by historical "
            "evidence. See evidence-context-assembly.md INV-EC-1."
        ),
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class ModelContextSettings(BaseSettings):
    """Model context-window registry overrides (GAP-1).

    The built-in registry (``faultmaven.utils.model_context``) maps known
    model ids to their true context window + a recommended response reserve,
    from which the per-turn prompt budget is derived
    (``prompt_budget = window − reserve``). These knobs let operators correct
    or extend that registry without a code change — useful for self-hosted /
    local models, newly released models not yet in the built-in map, or models
    served with a non-default window.
    """

    window_overrides: Dict[str, Dict[str, int]] = Field(
        default_factory=dict,
        validation_alias="MODEL_CONTEXT_WINDOWS",
        description=(
            "JSON map of model-id → {context_window, response_reserve} that "
            "overrides/extends the built-in registry. Exact-id match, "
            "case-insensitive. Example: "
            '{"my-local-llama": {"context_window": 32768, '
            '"response_reserve": 4096}}. response_reserve is optional.'
        ),
    )

    # --- Flat prompt-budget target (the scarce-resource control) ---
    # Prompt tokens are a scarce resource budget-allocated programmatically.
    # FaultMaven targets a FLAT number of prompt tokens driven by what the
    # investigation task needs — NOT a fraction of the model window. The model
    # window enters only as a downward clamp (see ResolvedBudget.prompt_target):
    #
    #     prompt_budget = min(prompt_target_tokens, window − response_reserve)
    #
    # Flat across all big-window models (Claude/GPT/Gemini), clamped down only
    # for small/local models we happen to know. This protects fleet cost from
    # the 200K/1M models, degrades gracefully on local hardware, and forces the
    # agent to use RAG tools (search_file / KB / deep_analysis) instead of lazy
    # context-dumping. Default 32K is safe for the curated large-context cloud
    # models; LOCAL/smaller models MUST lower it to fit (see .env.example).
    prompt_target_tokens: int = Field(
        default=32000,
        ge=2000,
        le=1_000_000,
        validation_alias="PROMPT_TARGET_TOKENS",
        description=(
            "Flat target (tokens) for the whole assembled prompt, independent "
            "of the model window. The model window only clamps this down for "
            "small/local models. Raise it to exploit larger, more advanced "
            "models; lower it to fit a small/local model or cut per-turn cost."
        ),
    )

    model_config = {"env_prefix": "", "extra": "ignore"}


class PromptBudgetSettings(BaseSettings):
    """Whole-prompt token-budget allocation + compaction controls.

    Implements the allocator in
    ``docs/architecture/investigation-engine/prompt-token-budget-allocation.md``:
    the resolved budget (``PROMPT_TARGET_TOKENS`` clamped to the model window) is
    poured into a single jar — reserve first, then variable sections by
    strict-priority greedy fill up to per-section caps, each compacted to fit.
    This is the only prompt-assembly path.
    """

    # --- Reserve bounds (§6) — keep the never-trimmed reserve bounded ---
    user_message_max_tokens: int = Field(
        default=4000,
        ge=200,
        le=100_000,
        validation_alias="PROMPT_USER_MESSAGE_MAX_TOKENS",
        description=(
            "Cap on the reserved current user message. Oversized pasted DATA "
            "should be file-ified at intake (→ compactable evidence); the chat "
            "text itself is truncated with a marker beyond this."
        ),
    )
    last_exchange_max_tokens: int = Field(
        default=2000,
        ge=200,
        le=100_000,
        validation_alias="PROMPT_LAST_EXCHANGE_MAX_TOKENS",
        description="Cap on the reserved previous user+assistant exchange (continuity floor).",
    )
    journal_max_tokens: int = Field(
        default=1500,
        ge=200,
        le=50_000,
        validation_alias="PROMPT_JOURNAL_MAX_TOKENS",
        description=(
            "Strict cap on the (high-priority) investigation-journal section. "
            "Small by design — the journal is high-density anti-amnesia memory."
        ),
    )
    system_feedback_max_tokens: int = Field(
        default=1500,
        ge=200,
        le=50_000,
        validation_alias="PROMPT_SYSTEM_FEEDBACK_MAX_TOKENS",
        description="Cap on the reserved last-turn system feedback block.",
    )
    conversation_history_max_tokens: int = Field(
        default=8000,
        ge=200,
        le=100_000,
        validation_alias="PROMPT_CONVERSATION_HISTORY_MAX_TOKENS",
        description=(
            "Cap on the (priority #2) conversation-history section. Without it "
            "the section's cap defaults to the whole section_budget, so verbose "
            "old turns can starve the lower-priority journal / KB / hypotheses "
            "(§5.1: every variable section must be capped). The continuity floor "
            "(latest turn via compact history) is still honored below this cap."
        ),
    )

    # --- Backstop (§7) ---
    min_viable_tokens: int = Field(
        default=1500,
        ge=200,
        le=100_000,
        validation_alias="PROMPT_MIN_VIABLE_TOKENS",
        description=(
            "Starvation threshold: if the reserve leaves less than this for "
            "variable content, switch proactively to the minimal FALLBACK_* "
            "template (a usable degraded prompt rather than a near-empty one)."
        ),
    )
    overhead_margin_tokens: int = Field(
        default=256,
        ge=0,
        le=8000,
        validation_alias="PROMPT_OVERHEAD_MARGIN_TOKENS",
        description=(
            "Safety buffer subtracted from section_budget to absorb token-"
            "estimate error (matters mainly when target ≈ hard limit)."
        ),
    )

    # --- Per-turn spend (sum across the tool-loop calls) ---
    turn_token_ceiling: int = Field(
        default=150_000,
        ge=10_000,
        le=2_000_000,
        validation_alias="PROMPT_TURN_TOKEN_CEILING",
        description=(
            "Hard per-turn spend ceiling: once a turn's cumulative token spend "
            "(across all tool-loop calls) crosses this, the tool loop is forced "
            "to wrap up on the next iteration (schema-only) instead of running "
            "more expensive tool calls. A safety abort, not the normal budget."
        ),
    )
    turn_token_budget: int = Field(
        default=100_000,
        ge=0,
        le=2_000_000,
        validation_alias="PROMPT_TURN_TOKEN_BUDGET",
        description=(
            "Soft per-turn spend budget for observability. When > 0 and a turn's "
            "total token spend exceeds it, a WARNING (turn_token_budget_exceeded) "
            "is logged with the call breakdown — surfacing high-spend turns "
            "(measured normal is ~66K/turn) without changing behavior. Set 0 to "
            "disable the alert."
        ),
    )
    tool_observation_max_tokens: int = Field(
        default=16_000,
        ge=1_000,
        le=500_000,
        validation_alias="PROMPT_TOOL_OBSERVATION_MAX_TOKENS",
        description=(
            "Bounded scratchpad allowance for accumulated tool-loop observations "
            "(tool calls + results). Each tool-loop LLM call is hard-bounded to "
            "min(model_ceiling, prompt_target + this) — so no continuation call "
            "grows unbounded past the jar. When the accumulated tool exchanges "
            "would exceed it, the OLDEST are elided (with a marker; the agent can "
            "re-search), keeping the base task + newest observations."
        ),
    )

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

    max_tokens: int = Field(
        default=2000,
        validation_alias="DEEP_ANALYSIS_MAX_TOKENS",
        ge=256,
        le=16000,
        description=(
            "Maximum response tokens for deep-analysis LLM calls. Applied "
            "by the 'local' backend (LocalTier2Service); the 'external' "
            "backend governs its own response size on the server side."
        ),
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

    # Per-provider overrides for the agent-level (turn-wide) timeout. The
    # turn endpoint wraps the entire process_turn call in asyncio.wait_for
    # using this ceiling; providers that take longer per turn (e.g.
    # Fireworks DeepSeek V4 Pro on log-heavy cases, local Ollama on CPU)
    # need more headroom but raising the global default hurts faster
    # providers. Mirrors LLMSettings.provider_timeout_overrides; resolved
    # at call time in modules/case/api/routes.py.
    #
    # Set via env as JSON, e.g.:
    #   AGENT_PROVIDER_TIMEOUT_OVERRIDES='{"fireworks": 300, "ollama": 900}'
    #
    # Surfaced by ISS-058 — DeepSeek run on logs-windows q3 hit the 120s
    # ceiling. Pairs stylistically with ISS-054 (LLM-router timeout).
    provider_timeout_overrides: Dict[str, int] = Field(
        default_factory=dict,
        validation_alias="AGENT_PROVIDER_TIMEOUT_OVERRIDES",
        description=(
            "Per-provider agent-level timeout overrides in seconds. JSON "
            "object keyed by provider name (e.g. 'fireworks', 'gemini', "
            "'ollama'). Empty default — providers fall back to "
            "agent_request_timeout."
        ),
    )

    def timeout_for_provider(self, provider_name: Optional[str]) -> int:
        """Return the per-provider agent timeout if set, else the global default.

        Centralised so callers don't have to dict-lookup + fall back themselves.
        Empty / unknown provider names return ``agent_request_timeout`` unchanged.
        """
        if not provider_name:
            return self.agent_request_timeout
        return self.provider_timeout_overrides.get(
            provider_name, self.agent_request_timeout
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
        validation_alias="VECTORIZATION_REACTIVE_TIMEOUT_SECONDS",
        description=(
            "Upper bound for synchronous reactive vectorization inside "
            "the DA tool loop (MilestoneEngine._reactive_vectorize and "
            "AgentOrchestrationService's auto-vectorization trigger). "
            "Reactive vectorize is triggered on tool failure signals "
            "(tool timeout, repeated empty searches, low confidence); "
            "the agent waits for it before continuing. BGE-M3 encode on "
            "CPU for large structural indexes can take 120-180 s, so "
            "the default is sized generously. On CPU-only hardware you "
            "may need to raise this together with agent_request_timeout. "
            "Proactive vectorization is intentionally UNBOUNDED at the "
            "orchestration layer — it's a fire-and-forget background "
            "task and time-bounding it only guarantees wasted work when "
            "encode outlasts the bound."
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
        description="Tenant isolation: 'single' (standalone) or 'multi' (cloud)",
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

    # Orphan-file cleanup (evidence-failure-modes.md)
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


class DeploymentMode(str, Enum):
    """Canonical deployment architecture (ADR-004, faultmaven-doc-internal).

    The single source of truth for "am I standalone or cloud?". Auth mode,
    storage backends, and tenancy are CONSEQUENCES that must be coherent with
    this (enforced by ``faultmaven.config.deployment_coherence``) — never
    inputs that decide the mode.
    """

    STANDALONE = "standalone"  # single-process, single-user (default)
    CLOUD = "cloud"  # orchestrated (k8s), multi-tenant


class FaultMavenSettings(BaseSettings):
    """
    Unified configuration for FaultMaven system.

    Single source of truth that replaces:
    - config/config.py
    - config/configuration_manager.py
    - Direct os.getenv() calls throughout codebase

    All configuration access should go through this class via dependency injection.
    """

    # Canonical deployment switch (ADR-004). NOT derived from AUTH_MODE — auth,
    # storage, and tenancy must be coherent with this (boot-time gate enforces it).
    deployment_mode: DeploymentMode = Field(
        default=DeploymentMode.STANDALONE,
        validation_alias="DEPLOYMENT_MODE",
        description=(
            "Canonical deployment architecture: 'standalone' (single-process, "
            "single-user) or 'cloud' (k8s, multi-tenant)."
        ),
    )

    # Whether the app runs `alembic upgrade head` itself at startup. True for
    # self-contained deployments (docker-compose, local dev) where the app owns
    # its schema. MUST be False wherever an external migration Job owns schema and
    # the app connects as a NON-OWNER, no-DDL role (the K8s deployment + RLS
    # tenant-isolation roles) — otherwise a startup `alembic upgrade` that needs
    # DDL is permission-denied and crash-loops the pod. Decoupled from
    # DEPLOYMENT_MODE on purpose: the on-prem cluster runs the migration Job while
    # still in 'standalone' mode, so this is the migration Job's signal, not cloud's.
    run_startup_migrations: bool = Field(
        default=True,
        validation_alias="RUN_STARTUP_MIGRATIONS",
        description=(
            "Run Alembic migrations at app startup. Set False when an external "
            "migration Job owns schema and the app uses a non-owner DB role."
        ),
    )

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
    investigation_context: InvestigationContextSettings = Field(
        default_factory=InvestigationContextSettings
    )
    model_context: ModelContextSettings = Field(default_factory=ModelContextSettings)
    prompt_budget: PromptBudgetSettings = Field(default_factory=PromptBudgetSettings)
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

    @property
    def is_cloud(self) -> bool:
        """True iff DEPLOYMENT_MODE=cloud — the one canonical deployment check (ADR-004)."""
        val = getattr(self.deployment_mode, "value", self.deployment_mode)
        return str(val) == DeploymentMode.CLOUD.value

    @property
    def is_standalone(self) -> bool:
        """True iff DEPLOYMENT_MODE=standalone (not cloud)."""
        return not self.is_cloud

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

            # Standalone convenience: ensure a local JWT secret exists (once,
            # here — not on every settings construction) before building settings.
            ensure_local_jwt_secret_env()

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
