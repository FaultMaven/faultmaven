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
        self._experimental_features = settings.features.enable_advanced_reasoning  # Map to existing feature
        self._performance_monitoring = settings.observability.enable_performance_monitoring
        self._detailed_tracing = settings.observability.enable_detailed_tracing
    
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
            "experimental_features": self.experimental_features,
            "performance_monitoring": self.performance_monitoring,
            "detailed_tracing": self.detailed_tracing
        }


def log_feature_flag_status(settings=None) -> None:
    """Log current feature flag status for debugging."""
    import logging
    logger = logging.getLogger(__name__)
    
    manager = FeatureFlagManager(settings)
    logger.info(f"Active feature flags: {manager.get_active_flags()}")