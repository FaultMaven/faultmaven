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
    """LLM provider configuration"""
    provider: LLMProvider = Field(default=LLMProvider.FIREWORKS, alias="CHAT_PROVIDER")
    
    # API Keys (SecretStr for security)
    openai_api_key: Optional[SecretStr] = Field(default=None, env="OPENAI_API_KEY")
    anthropic_api_key: Optional[SecretStr] = Field(default=None, env="ANTHROPIC_API_KEY")
    fireworks_api_key: Optional[SecretStr] = Field(default=None, env="FIREWORKS_API_KEY")
    cohere_api_key: Optional[SecretStr] = Field(default=None, env="COHERE_API_KEY")
    gemini_api_key: Optional[SecretStr] = Field(default=None, env="GEMINI_API_KEY")
    huggingface_api_key: Optional[SecretStr] = Field(default=None, env="HUGGINGFACE_API_KEY")
    openrouter_api_key: Optional[SecretStr] = Field(default=None, env="OPENROUTER_API_KEY")
    
    # Model configuration
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
    
    # Request configuration
    request_timeout: int = Field(default=30, env="LLM_REQUEST_TIMEOUT")
    max_retries: int = Field(default=3, env="LLM_MAX_RETRIES")
    retry_delay: float = Field(default=1.0, env="LLM_RETRY_DELAY")
    
    # Token limits
    max_tokens: int = Field(default=4096, env="LLM_MAX_TOKENS")
    context_window: int = Field(default=128000, env="LLM_CONTEXT_WINDOW")
    
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
    
    def get_model(self) -> str:
        """Get model for current provider"""
        model_map = {
            LLMProvider.OPENAI: self.openai_model,
            LLMProvider.ANTHROPIC: self.anthropic_model,
            LLMProvider.FIREWORKS: self.fireworks_model,
            LLMProvider.COHERE: self.cohere_model,
            LLMProvider.LOCAL: self.local_model,
        }
        return model_map.get(self.provider, "")

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


class FeatureSettings(BaseSettings):
    """Feature flags and toggles"""
    use_di_container: bool = Field(default=True, env="USE_DI_CONTAINER")
    use_refactored_services: bool = Field(default=True, env="USE_REFACTORED_SERVICES")
    use_refactored_api: bool = Field(default=True, env="USE_REFACTORED_API")
    enable_legacy_compatibility: bool = Field(default=False, env="ENABLE_LEGACY_COMPATIBILITY")
    
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