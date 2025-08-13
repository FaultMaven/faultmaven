"""Feature flags for FaultMaven configuration.

This module contains only active feature flags. Migration-related flags
have been removed as the refactored architecture is now the default.
"""

import os
from typing import Dict, Any

# Active feature flags (keep these)
ENABLE_LEGACY_COMPATIBILITY = os.getenv("ENABLE_LEGACY_COMPATIBILITY", "true").lower() == "true"
ENABLE_EXPERIMENTAL_FEATURES = os.getenv("ENABLE_EXPERIMENTAL_FEATURES", "false").lower() == "true"

# Performance and debugging flags
ENABLE_PERFORMANCE_MONITORING = os.getenv("ENABLE_PERFORMANCE_MONITORING", "true").lower() == "true"
ENABLE_DETAILED_TRACING = os.getenv("ENABLE_DETAILED_TRACING", "false").lower() == "true"

def get_active_flags() -> Dict[str, bool]:
    """Get all currently active feature flags."""
    return {
        "legacy_compatibility": ENABLE_LEGACY_COMPATIBILITY,
        "experimental_features": ENABLE_EXPERIMENTAL_FEATURES,
        "performance_monitoring": ENABLE_PERFORMANCE_MONITORING,
        "detailed_tracing": ENABLE_DETAILED_TRACING
    }


def log_feature_flag_status() -> None:
    """Log current feature flag status for debugging."""
    import logging
    logger = logging.getLogger(__name__)
    
    flags = get_active_flags()
    logger.info(f"Active feature flags: {flags}")