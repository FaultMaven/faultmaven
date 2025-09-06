"""Feature flags for FaultMaven configuration.

This module contains only active feature flags. Migration-related flags
have been removed as the refactored architecture is now the default.
"""

import os
from typing import Dict, Any, Optional

class FeatureFlagManager:
    """Manages feature flags with settings-based configuration."""
    
    def __init__(self, settings=None):
        """Initialize feature flag manager with settings or fallback to environment."""
        # Use unified settings system only
        if settings is None:
            from faultmaven.config.settings import get_settings
            settings = get_settings()
        
        # Configuration from unified settings system
        self._legacy_compatibility = settings.features.enable_legacy_compatibility
        self._experimental_features = settings.features.enable_advanced_reasoning  # Map to existing feature
        self._performance_monitoring = settings.observability.enable_performance_monitoring
        self._detailed_tracing = settings.observability.enable_detailed_tracing
    
    @property
    def legacy_compatibility(self) -> bool:
        """Enable legacy compatibility mode."""
        return self._legacy_compatibility
    
    @property
    def experimental_features(self) -> bool:
        """Enable experimental features."""
        return self._experimental_features
    
    @property
    def performance_monitoring(self) -> bool:
        """Enable performance monitoring."""
        return self._performance_monitoring
    
    @property
    def detailed_tracing(self) -> bool:
        """Enable detailed tracing."""
        return self._detailed_tracing
    
    def get_active_flags(self) -> Dict[str, bool]:
        """Get all currently active feature flags."""
        return {
            "legacy_compatibility": self.legacy_compatibility,
            "experimental_features": self.experimental_features,
            "performance_monitoring": self.performance_monitoring,
            "detailed_tracing": self.detailed_tracing
        }

# Global feature flag manager instance (for backward compatibility)
_feature_flags: Optional[FeatureFlagManager] = None

def get_feature_flag_manager(settings=None) -> FeatureFlagManager:
    """Get the global feature flag manager instance."""
    global _feature_flags
    if _feature_flags is None:
        _feature_flags = FeatureFlagManager(settings)
    return _feature_flags

def get_active_flags(settings=None) -> Dict[str, bool]:
    """Get all currently active feature flags (backward compatible function)."""
    manager = get_feature_flag_manager(settings)
    return manager.get_active_flags()

# Backward compatibility constants (will use environment fallback)
_fallback_manager = FeatureFlagManager(None)
ENABLE_LEGACY_COMPATIBILITY = _fallback_manager.legacy_compatibility
ENABLE_EXPERIMENTAL_FEATURES = _fallback_manager.experimental_features
ENABLE_PERFORMANCE_MONITORING = _fallback_manager.performance_monitoring
ENABLE_DETAILED_TRACING = _fallback_manager.detailed_tracing


def log_feature_flag_status(settings=None) -> None:
    """Log current feature flag status for debugging."""
    import logging
    logger = logging.getLogger(__name__)
    
    flags = get_active_flags(settings)
    logger.info(f"Active feature flags: {flags}")