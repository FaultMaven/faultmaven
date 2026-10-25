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
    CONFIG_PRESET=local  # Local LLM and local stores, no external deps
    CONFIG_PRESET=standard  # Requires Redis, ChromaDB
    CONFIG_PRESET=enterprise  # Production settings, four workers; names no services
    CONFIG_PRESET=none  # No preset: none applied, and none auto-detected

    # Presets can be overridden by specific env vars
    CONFIG_PRESET=local
    CHAT_PROVIDER=openai  # Overrides the preset's default provider
    OPENAI_API_KEY=sk-...  # Required when using openai provider
"""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Set

logger = logging.getLogger(__name__)


class PresetName(str, Enum):
    """Available configuration presets."""

    LOCAL = "local"  # Zero-config for local development
    MINIMAL = "minimal"  # Alias for local
    STANDARD = "standard"  # Basic production with Redis + ChromaDB
    ENTERPRISE = "enterprise"  # Production settings, four workers; names no services


@dataclass
class PresetDefinition:
    """Definition of a configuration preset."""

    name: str
    description: str
    defaults: Dict[str, Any]
    optional_dependencies: List[str] = field(default_factory=list)


# =============================================================================
# PRESET DEFINITIONS
# =============================================================================

PRESETS: Dict[str, PresetDefinition] = {
    PresetName.LOCAL: PresetDefinition(
        name="local",
        description="Zero-config for local development. Uses a local LLM and the "
        "local stores, no external services required. Great for getting started "
        "quickly.",
        defaults={
            # Server
            "ENVIRONMENT": "development",
            "DEBUG": "true",
            "HOST": "127.0.0.1",
            "PORT": "8000",
            "RELOAD": "true",
            # NOTE: do NOT set SKIP_SERVICE_CHECKS here. That flag makes the DI
            # container skip *creating* the ChromaDB vector stores (returns None),
            # which silently disables the knowledge base — the embedded
            # PersistentClient this preset relies on never gets built, so the KB
            # pack cannot be ingested at startup. Redis already degrades to
            # FakeRedis and ChromaDB defaults to a local PersistentClient, so no
            # external service is required without skipping. SKIP_SERVICE_CHECKS
            # remains opt-in (e.g. pytest) for genuinely store-less runs.
            # Storage - minimal external deps (FakeRedis for sessions, local
            # ChromaDB PersistentClient for vectors). Cases and users go to
            # DATABASE_URL, the SQLite file by default, under every preset.
            "VECTOR_STORAGE_TYPE": "chromadb",
            # LLM - Default to local for true zero-config
            # Users can override with their preferred provider
            "CHAT_PROVIDER": "local",
            "LOCAL_LLM_URL": "http://localhost:11434/v1",  # Ollama default
            "LOCAL_LLM_MODEL": "llama2",
            # PII redaction only. Rate limiting is not configurable here (or
            # anywhere): the protection presets in config/protection.py own it.
            "PROTECTION_ENABLED": "false",
            "SANITIZE_PII": "false",
            # Observability - minimal for local
            "LOG_LEVEL": "DEBUG",
            "ENABLE_PERFORMANCE_MONITORING": "false",
            "ENABLE_DETAILED_TRACING": "false",
            # Session - relaxed timeouts for development
            "SESSION_TIMEOUT_MINUTES": "60",
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
            # Redis defaults (override with your actual values)
            "REDIS_HOST": "localhost",
            "REDIS_PORT": "6379",
            # ChromaDB defaults
            "CHROMADB_URL": "http://localhost:8001",
            # LLM - Fireworks as default cloud provider (good balance of cost/quality)
            "CHAT_PROVIDER": "fireworks",
            # PII redaction only — see the local preset's note on rate limiting.
            "PROTECTION_ENABLED": "true",
            "SANITIZE_PII": "true",
            # Observability
            "LOG_LEVEL": "INFO",
            "ENABLE_PERFORMANCE_MONITORING": "true",
            "ENABLE_DETAILED_TRACING": "false",
            # Session
            "SESSION_TIMEOUT_MINUTES": "30",
        },
        optional_dependencies=[
            "Redis (localhost:6379) for session storage",
            "ChromaDB (localhost:8001) for vector storage",
        ],
    ),
    PresetName.ENTERPRISE: PresetDefinition(
        name="enterprise",
        description="Production settings with four workers and vectors in "
        "ChromaDB. Names no service: set DATABASE_URL (PostgreSQL), REDIS_HOST "
        "and CHROMADB_URL.",
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
            # LLM - OpenAI as enterprise default
            "CHAT_PROVIDER": "openai",
            # PII redaction only — see the local preset's note on rate limiting.
            "PROTECTION_ENABLED": "true",
            "SANITIZE_PII": "true",
            "AUTO_SANITIZE_BASED_ON_PROVIDER": "true",
            # Observability - full monitoring
            "LOG_LEVEL": "INFO",
            "ENABLE_PERFORMANCE_MONITORING": "true",
            "ENABLE_DETAILED_TRACING": "true",
            # Session - standard timeouts
            "SESSION_TIMEOUT_MINUTES": "30",
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


def _settings_view() -> Dict[str, str]:
    """The variables the settings are about to read (see ``environment_settings_read``).

    Every decision in this module taken before the settings exist reads this one
    view, not ``os.environ``: the settings read the process environment OVER the
    dotenv file in the working directory, so a decision from the environment
    alone disagrees with them for any value set only in that file (#2008).
    Imported at call time: settings import this module's package.
    """
    from faultmaven.config.settings import environment_settings_read

    return environment_settings_read()


def preset_required_keys(preset: PresetDefinition, provider: str) -> Set[str]:
    """The variables ``preset`` leaves an operator to supply for ``provider``.

    What a provider needs comes from the provider registry; what the preset
    already supplies comes from its defaults. The difference is what is
    missing, so no per-preset table can disagree with either (#2006). An
    unknown provider needs nothing from this check; the settings reject it.
    """
    from faultmaven.infrastructure.llm.providers.registry import (
        PROVIDER_SCHEMA,
        required_env_vars,
    )

    if provider not in PROVIDER_SCHEMA:
        return set()
    return set(required_env_vars(provider)) - set(preset.defaults)


#: ``CONFIG_PRESET=none``, in any letter case: no preset. None is applied, and
#: the zero-config ``local`` preset is not auto-detected either (#2243).
NO_PRESET = "none"


def _preset_or_none(value: Optional[str]) -> Optional[str]:
    """A ``CONFIG_PRESET`` value as a preset name: ``None`` when blank or ``none``."""
    name = (value or "").lower().strip()
    return None if name in ("", NO_PRESET) else name


def preset_declined(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether ``CONFIG_PRESET=none`` declines every preset, the detected one too."""
    if environ is None:
        environ = _settings_view()
    return (environ.get("CONFIG_PRESET") or "").lower().strip() == NO_PRESET


def get_current_preset_name(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Get the currently configured preset name, as the settings will read it.

    ``None`` when no preset is configured, and for ``CONFIG_PRESET=none``.
    """
    if environ is None:
        environ = _settings_view()
    return _preset_or_none(environ.get("CONFIG_PRESET"))


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
    else:
        preset_name = _preset_or_none(preset_name)

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
    environ = _settings_view()
    preset_name = get_current_preset_name(environ)
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
        # "Already set" is asked of the view the settings read: a value only in
        # the working directory's .env is set, and writing the preset default
        # into os.environ would make that default beat the operator's file.
        if key not in environ:
            os.environ[key] = str(value)
            applied_count += 1
            logger.debug(f"  Set {key}={value} (from preset)")
        else:
            logger.debug(f"  Keeping {key} (operator override)")

    logger.info(f"Applied {applied_count} defaults from preset '{preset.name}'")


def validate_preset_requirements() -> List[str]:
    """
    Validate that required overrides are present for the current preset and provider.

    Returns:
        List of error messages for missing required configuration.
    """
    errors = []

    environ = _settings_view()
    preset_name = get_current_preset_name(environ)
    if not preset_name:
        return errors

    preset = get_preset(preset_name)
    if not preset:
        return errors

    # Get the active LLM provider
    provider = (
        environ.get("CHAT_PROVIDER") or preset.defaults.get("CHAT_PROVIDER", "")
    ).lower()

    for key in sorted(preset_required_keys(preset, provider)):
        if not environ.get(key):
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
    else:
        preset_name = _preset_or_none(preset_name)

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

    from faultmaven.infrastructure.llm.providers.registry import (
        get_valid_provider_names,
    )

    return {
        "active": True,
        "name": preset.name,
        "description": preset.description,
        "defaults_count": len(preset.defaults),
        "optional_dependencies": preset.optional_dependencies,
        "required_for_providers": {
            provider: sorted(preset_required_keys(preset, provider))
            for provider in get_valid_provider_names()
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
    preset when CONFIG_PRESET is not explicitly set. A deployment is
    zero-config only when it configures no LLM provider at all: any provider in
    the provider registry (an API key, or ``LOCAL_LLM_URL`` for the local one),
    Redis, ChromaDB or a non-development ``ENVIRONMENT`` declines the preset.

    Every one of those is read from the environment the settings are about to
    read (``environment_settings_read``): names case-insensitively, the process
    environment over the dotenv file ``FaultMavenSettings`` resolves from the
    working directory, and no file under ``FAULTMAVEN_SKIP_DOTENV``. A decision
    taken from fewer channels than the settings read applies the laptop preset
    over a configuration the settings then honour.

    Returns:
        Preset name to use, or None if environment should be used as-is.
    """
    # An operator who configured ANY provider the router supports has chosen
    # their LLM, so the laptop preset must not be applied over them. "Which
    # providers exist, and what configures each" is asked of the provider
    # registry — a hand-kept list here named five of the nine and sent a
    # Gemini/OpenRouter/HuggingFace/local-only deployment to 127.0.0.1:8000
    # with DEBUG on and protection off (#1984). Imported at call time because
    # the registry pulls in every provider adapter, and this runs before
    # settings exist (the same lazy shape as the other config -> infrastructure
    # reads).
    from faultmaven.config.settings import environment_settings_read
    from faultmaven.infrastructure.llm.providers.registry import (
        configured_provider_names,
    )

    environ = environment_settings_read()

    configured = configured_provider_names(environ)
    if configured:
        logger.debug(
            "Zero-config preset declined: LLM provider(s) configured: %s",
            ", ".join(configured),
        )
        return None

    # If Redis or ChromaDB is configured, don't auto-apply local preset
    if environ.get("REDIS_HOST") or environ.get("CHROMADB_URL"):
        return None

    # A deployed environment must not be silently reclassified as a dev box.
    #
    # The preset this would auto-apply is ``local``, and it carries
    # ``DEBUG=true``, ``SANITIZE_PII=false``, ``PROTECTION_ENABLED=false`` and
    # ``LOG_LEVEL=DEBUG`` — a posture that is acceptable on a genuine
    # development machine and nowhere else. Naming only ``production`` here made
    # the gate an enumeration of one: ``ENVIRONMENT=staging`` on an
    # under-configured box (no LLM key yet, no Redis) fell straight through to
    # the local preset, which is the same shape of hole fm#1023 fixed one layer
    # up — a deployed environment nobody classified getting development's
    # settings by omission.
    #
    # So the gate is now the inverse: any *explicitly named* environment other
    # than ``development`` declines the auto-preset, including a value nobody
    # recognises. An operator who names an environment has classified the box,
    # and "merely under-configured" is not a request for local defaults. Unset
    # still means zero-config, which is the whole point of this path.
    environment = environ.get("ENVIRONMENT", "").strip().lower()
    if environment and environment != "development":
        return None

    # Default to local preset for zero-config experience
    logger.info("Zero-config mode: applying 'local' preset for quick startup")
    return PresetName.LOCAL


def ensure_preset_applied():
    """
    Ensure a preset is applied for zero-config experience.

    This should be called early in application startup. It will:
    1. Apply nothing, and detect nothing, under CONFIG_PRESET=none
    2. Apply explicit CONFIG_PRESET if set
    3. Otherwise, detect and apply best preset for zero-config
    """
    environ = _settings_view()
    if preset_declined(environ):
        logger.debug("CONFIG_PRESET=none: no preset applied, none detected")
        return

    preset_name = get_current_preset_name(environ)

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
    "preset_required_keys",
    "NO_PRESET",
    "preset_declined",
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
