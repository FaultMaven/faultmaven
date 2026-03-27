"""
Configuration Presets for FaultMaven

This module provides predefined configuration presets for common deployment scenarios.
Presets enable zero-config defaults for local development and easy configuration
for production environments.

Design Principles:
- Zero-config for local development: Works out of the box with no .env file
- Presets are merged with environment variables (env vars take precedence)
- Provider-aware defaults: Required overrides depend on selected LLM provider
- Deployment-neutral: No deployment-specific endpoints in core (Prometheus, etc.)

Usage:
    # Via environment variable
    CONFIG_PRESET=local  # Uses in-memory storage, no external deps
    CONFIG_PRESET=standard  # Requires Redis, ChromaDB
    CONFIG_PRESET=enterprise  # Full production with PostgreSQL

    # Presets can be overridden by specific env vars
    CONFIG_PRESET=local
    CHAT_PROVIDER=openai  # Overrides the preset's default provider
    OPENAI_API_KEY=sk-...  # Required when using openai provider
"""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class PresetName(str, Enum):
    """Available configuration presets."""

    LOCAL = "local"  # Zero-config for local development
    MINIMAL = "minimal"  # Alias for local
    STANDARD = "standard"  # Basic production with Redis + ChromaDB
    ENTERPRISE = "enterprise"  # Full production with PostgreSQL


@dataclass
class PresetDefinition:
    """Definition of a configuration preset."""

    name: str
    description: str
    defaults: Dict[str, Any]
    required_overrides: Dict[str, Set[str]] = field(
        default_factory=dict
    )  # provider -> required keys
    optional_dependencies: List[str] = field(default_factory=list)


# =============================================================================
# PRESET DEFINITIONS
# =============================================================================

PRESETS: Dict[str, PresetDefinition] = {
    PresetName.LOCAL: PresetDefinition(
        name="local",
        description="Zero-config for local development. Uses in-memory storage, "
        "no external services required. Great for getting started quickly.",
        defaults={
            # Server
            "ENVIRONMENT": "development",
            "DEBUG": "true",
            "HOST": "127.0.0.1",
            "PORT": "8000",
            "RELOAD": "true",
            "SKIP_SERVICE_CHECKS": "true",
            # Storage - minimal external deps (FakeRedis for sessions)
            "VECTOR_STORAGE_TYPE": "inmemory",
            "CASE_STORAGE_TYPE": "inmemory",
            "USER_STORAGE_TYPE": "inmemory",
            # LLM - Default to local for true zero-config
            # Users can override with their preferred provider
            "CHAT_PROVIDER": "local",
            "LOCAL_LLM_URL": "http://localhost:11434/v1",  # Ollama default
            "LOCAL_LLM_MODEL": "llama2",
            # Protection - lenient for development
            "PROTECTION_ENABLED": "false",
            "RATE_LIMIT_ENABLED": "false",
            "SANITIZE_PII": "false",
            # Observability - minimal for local
            "LOG_LEVEL": "DEBUG",
            "ENABLE_PERFORMANCE_MONITORING": "false",
            "ENABLE_DETAILED_TRACING": "false",
            # Session - relaxed timeouts for development
            "SESSION_TIMEOUT_MINUTES": "60",
            # OODA - light investigation for faster iteration
            "DEFAULT_OODA_INTENSITY": "light",
        },
        required_overrides={
            # Provider-specific required keys
            "openai": {"OPENAI_API_KEY"},
            "anthropic": {"ANTHROPIC_API_KEY"},
            "fireworks": {"FIREWORKS_API_KEY"},
            "groq": {"GROQ_API_KEY"},
            "cohere": {"COHERE_API_KEY"},
            # Local provider has no required keys
            "local": set(),
        },
        optional_dependencies=[
            "Ollama (http://localhost:11434) for local LLM",
        ],
    ),
    PresetName.STANDARD: PresetDefinition(
        name="standard",
        description="Standard production setup with Redis for sessions and "
        "ChromaDB for vector storage. Suitable for single-server deployments.",
        defaults={
            # Server
            "ENVIRONMENT": "production",
            "DEBUG": "false",
            "HOST": "0.0.0.0",
            "PORT": "8000",
            "RELOAD": "false",
            "SKIP_SERVICE_CHECKS": "false",
            # Storage - external services (Redis auto-selected via REDIS_HOST)
            "VECTOR_STORAGE_TYPE": "chromadb",
            "CASE_STORAGE_TYPE": "inmemory",  # In-memory until PostgreSQL configured
            "USER_STORAGE_TYPE": "inmemory",
            # Redis defaults (override with your actual values)
            "REDIS_HOST": "localhost",
            "REDIS_PORT": "6379",
            # ChromaDB defaults
            "CHROMADB_URL": "http://localhost:8001",
            # LLM - Fireworks as default cloud provider (good balance of cost/quality)
            "CHAT_PROVIDER": "fireworks",
            # Protection - enabled for production
            "PROTECTION_ENABLED": "true",
            "RATE_LIMIT_ENABLED": "true",
            "SANITIZE_PII": "true",
            "RATE_LIMIT_REQUESTS_PER_MINUTE": "60",
            # Observability
            "LOG_LEVEL": "INFO",
            "ENABLE_PERFORMANCE_MONITORING": "true",
            "ENABLE_DETAILED_TRACING": "false",
            # Session
            "SESSION_TIMEOUT_MINUTES": "30",
            # OODA
            "DEFAULT_OODA_INTENSITY": "medium",
        },
        required_overrides={
            "openai": {"OPENAI_API_KEY"},
            "anthropic": {"ANTHROPIC_API_KEY"},
            "fireworks": {"FIREWORKS_API_KEY"},
            "groq": {"GROQ_API_KEY"},
            "cohere": {"COHERE_API_KEY"},
            "local": {"LOCAL_LLM_URL", "LOCAL_LLM_MODEL"},
        },
        optional_dependencies=[
            "Redis (localhost:6379) for session storage",
            "ChromaDB (localhost:8001) for vector storage",
        ],
    ),
    PresetName.ENTERPRISE: PresetDefinition(
        name="enterprise",
        description="Full production setup with PostgreSQL for persistence, "
        "Redis for sessions/caching, ChromaDB for vectors. "
        "Suitable for multi-tenant, high-availability deployments.",
        defaults={
            # Server
            "ENVIRONMENT": "production",
            "DEBUG": "false",
            "HOST": "0.0.0.0",
            "PORT": "8000",
            "RELOAD": "false",
            "WORKERS": "4",
            "SKIP_SERVICE_CHECKS": "false",
            # Deployment mode
            "DEPLOYMENT_MODE": "multi-tenant",
            # Storage - full external services (Redis via REDIS_HOST)
            "VECTOR_STORAGE_TYPE": "chromadb",
            "CASE_STORAGE_TYPE": "database",
            "USER_STORAGE_TYPE": "database",
            # LLM - OpenAI as enterprise default
            "CHAT_PROVIDER": "openai",
            # Protection - strict for enterprise
            "PROTECTION_ENABLED": "true",
            "RATE_LIMIT_ENABLED": "true",
            "SANITIZE_PII": "true",
            "RATE_LIMIT_REQUESTS_PER_MINUTE": "120",
            "AUTO_SANITIZE_BASED_ON_PROVIDER": "true",
            # Observability - full monitoring
            "LOG_LEVEL": "INFO",
            "ENABLE_PERFORMANCE_MONITORING": "true",
            "ENABLE_DETAILED_TRACING": "true",
            # Session - standard timeouts
            "SESSION_TIMEOUT_MINUTES": "30",
            # OODA - full investigation for thorough analysis
            "DEFAULT_OODA_INTENSITY": "full",
        },
        required_overrides={
            "openai": {"OPENAI_API_KEY"},
            "anthropic": {"ANTHROPIC_API_KEY"},
            "fireworks": {"FIREWORKS_API_KEY"},
            "groq": {"GROQ_API_KEY"},
            "cohere": {"COHERE_API_KEY"},
            "local": {"LOCAL_LLM_URL", "LOCAL_LLM_MODEL"},
        },
        optional_dependencies=[
            "Redis for session storage and caching",
            "ChromaDB for vector storage",
            "PostgreSQL for case and user persistence",
        ],
    ),
}

# Alias: minimal -> local
PRESETS[PresetName.MINIMAL] = PRESETS[PresetName.LOCAL]


# =============================================================================
# PRESET LOADING LOGIC
# =============================================================================


def get_current_preset_name() -> Optional[str]:
    """Get the currently configured preset name from environment."""
    preset_name = os.getenv("CONFIG_PRESET", "").lower().strip()
    if not preset_name:
        return None
    return preset_name


def get_preset(preset_name: str) -> Optional[PresetDefinition]:
    """Get a preset definition by name."""
    try:
        preset_enum = PresetName(preset_name.lower())
        return PRESETS.get(preset_enum)
    except ValueError:
        logger.warning(
            f"Unknown preset: {preset_name}. Available: {[p.value for p in PresetName]}"
        )
        return None


def get_preset_defaults(preset_name: Optional[str] = None) -> Dict[str, str]:
    """
    Get default values from a preset.

    Args:
        preset_name: Preset name to load. If None, uses CONFIG_PRESET env var.
                     If no preset is specified, returns empty dict (pure env var mode).

    Returns:
        Dictionary of environment variable defaults from the preset.
    """
    if preset_name is None:
        preset_name = get_current_preset_name()

    if not preset_name:
        return {}

    preset = get_preset(preset_name)
    if not preset:
        return {}

    logger.info(f"Loading configuration preset: {preset.name}")
    logger.debug(f"Preset description: {preset.description}")

    return preset.defaults


def apply_preset_defaults():
    """
    Apply preset defaults to environment variables.

    Preset values are only set if the environment variable is not already defined.
    This allows environment variables to override preset values.

    Call this function early in application startup, before settings are loaded.
    """
    preset_name = get_current_preset_name()
    if not preset_name:
        # No preset specified, use pure environment variable mode
        return

    preset = get_preset(preset_name)
    if not preset:
        logger.warning(
            f"Preset '{preset_name}' not found, continuing without preset defaults"
        )
        return

    logger.info(f"Applying preset '{preset.name}': {preset.description}")

    applied_count = 0
    for key, value in preset.defaults.items():
        if os.getenv(key) is None:
            os.environ[key] = str(value)
            applied_count += 1
            logger.debug(f"  Set {key}={value} (from preset)")
        else:
            logger.debug(f"  Keeping {key}={os.getenv(key)} (env var override)")

    logger.info(f"Applied {applied_count} defaults from preset '{preset.name}'")


def validate_preset_requirements() -> List[str]:
    """
    Validate that required overrides are present for the current preset and provider.

    Returns:
        List of error messages for missing required configuration.
    """
    errors = []

    preset_name = get_current_preset_name()
    if not preset_name:
        return errors

    preset = get_preset(preset_name)
    if not preset:
        return errors

    # Get the active LLM provider
    provider = os.getenv(
        "CHAT_PROVIDER", preset.defaults.get("CHAT_PROVIDER", "")
    ).lower()

    if provider in preset.required_overrides:
        required_keys = preset.required_overrides[provider]
        for key in required_keys:
            if not os.getenv(key):
                errors.append(
                    f"Missing required configuration '{key}' for provider '{provider}' "
                    f"in preset '{preset.name}'"
                )

    return errors


def get_preset_info(preset_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get information about a preset for display/documentation.

    Args:
        preset_name: Preset name. If None, returns info about current preset.

    Returns:
        Dictionary with preset information.
    """
    if preset_name is None:
        preset_name = get_current_preset_name()

    if not preset_name:
        return {
            "active": False,
            "message": "No preset configured. Using environment variables only.",
        }

    preset = get_preset(preset_name)
    if not preset:
        return {
            "active": False,
            "error": f"Unknown preset: {preset_name}",
            "available_presets": [p.value for p in PresetName],
        }

    return {
        "active": True,
        "name": preset.name,
        "description": preset.description,
        "defaults_count": len(preset.defaults),
        "optional_dependencies": preset.optional_dependencies,
        "required_for_providers": {
            provider: list(keys) for provider, keys in preset.required_overrides.items()
        },
    }


def list_available_presets() -> List[Dict[str, Any]]:
    """List all available presets with their descriptions."""
    return [
        {
            "name": preset.name,
            "description": preset.description,
            "defaults_count": len(preset.defaults),
        }
        for preset_name, preset in PRESETS.items()
        if preset_name != PresetName.MINIMAL  # Skip alias
    ]


# =============================================================================
# AUTO-DETECTION FOR ZERO-CONFIG
# =============================================================================


def detect_zero_config_preset() -> Optional[str]:
    """
    Detect the best preset for zero-config startup.

    This function checks the environment to determine the most appropriate
    preset when CONFIG_PRESET is not explicitly set.

    Returns:
        Preset name to use, or None if environment should be used as-is.
    """
    # If any LLM API key is set, don't auto-apply preset
    llm_keys = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "FIREWORKS_API_KEY",
        "GROQ_API_KEY",
        "COHERE_API_KEY",
    ]
    if any(os.getenv(key) for key in llm_keys):
        return None

    # If Redis or ChromaDB is configured, don't auto-apply local preset
    if os.getenv("REDIS_HOST") or os.getenv("CHROMADB_URL"):
        return None

    # If in production environment, don't auto-apply local preset
    if os.getenv("ENVIRONMENT", "").lower() == "production":
        return None

    # Default to local preset for zero-config experience
    logger.info("Zero-config mode: applying 'local' preset for quick startup")
    return PresetName.LOCAL


def ensure_preset_applied():
    """
    Ensure a preset is applied for zero-config experience.

    This should be called early in application startup. It will:
    1. Apply explicit CONFIG_PRESET if set
    2. Otherwise, detect and apply best preset for zero-config
    """
    preset_name = get_current_preset_name()

    if preset_name:
        # Explicit preset specified
        apply_preset_defaults()
    else:
        # Check if we should auto-apply a preset for zero-config
        auto_preset = detect_zero_config_preset()
        if auto_preset:
            os.environ["CONFIG_PRESET"] = auto_preset
            apply_preset_defaults()


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "PresetName",
    "PresetDefinition",
    "PRESETS",
    "get_current_preset_name",
    "get_preset",
    "get_preset_defaults",
    "apply_preset_defaults",
    "validate_preset_requirements",
    "get_preset_info",
    "list_available_presets",
    "detect_zero_config_preset",
    "ensure_preset_applied",
]
