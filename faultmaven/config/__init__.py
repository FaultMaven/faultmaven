"""Configuration Package

Purpose: Centralized configuration management for FaultMaven

This package contains all configuration-related modules including:
- Feature flags for safe migration
- Environment-based settings
- Runtime configuration options
- Configuration presets for zero-config deployment

Zero-Config Quick Start:
    # No .env file needed for local development
    # FaultMaven auto-detects and uses 'local' preset

    # Or explicitly set a preset:
    CONFIG_PRESET=local      # In-memory storage, local LLM
    CONFIG_PRESET=standard   # Redis + ChromaDB, cloud LLM
    CONFIG_PRESET=enterprise # Full production with PostgreSQL
"""

from .presets import (
    PresetName,
    get_current_preset_name,
    get_preset_info,
    list_available_presets,
    validate_preset_requirements,
)

__all__ = [
    "PresetName",
    "get_current_preset_name",
    "get_preset_info",
    "list_available_presets",
    "validate_preset_requirements",
]
