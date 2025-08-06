"""Feature Flags - Phase 7.1

Purpose: Safe migration control for architecture refactoring

This module provides feature flags to enable gradual migration from the original
monolithic architecture to the new interface-based, service-oriented architecture.

Feature Flags:
- USE_REFACTORED_SERVICES: Enable refactored service layer with DI container
- USE_REFACTORED_API: Enable refactored API routes with thin controllers  
- USE_DI_CONTAINER: Enable centralized dependency injection container
- ENABLE_MIGRATION_LOGGING: Extra logging during migration period

Usage:
    # Enable new architecture
    export USE_REFACTORED_SERVICES=true
    export USE_REFACTORED_API=true
    export USE_DI_CONTAINER=true
    
    # Gradual migration
    export USE_REFACTORED_SERVICES=true  # Backend only
    export USE_REFACTORED_API=false      # Keep old API
    
Architecture Migration Strategy:
1. Phase 7.1: Feature flags created (current)
2. Phase 7.2: Main app updated to respect flags
3. Phase 8: Validation with both configurations
4. Phase 9: Remove flags and old code
"""

import os
import logging

# Core architecture flags
USE_REFACTORED_SERVICES = os.getenv("USE_REFACTORED_SERVICES", "false").lower() == "true"
USE_REFACTORED_API = os.getenv("USE_REFACTORED_API", "false").lower() == "true"
USE_DI_CONTAINER = os.getenv("USE_DI_CONTAINER", "false").lower() == "true"

# Migration and debugging flags
ENABLE_MIGRATION_LOGGING = os.getenv("ENABLE_MIGRATION_LOGGING", "false").lower() == "true"
ENABLE_PARALLEL_VALIDATION = os.getenv("ENABLE_PARALLEL_VALIDATION", "false").lower() == "true"
MIGRATION_ROLLBACK_MODE = os.getenv("MIGRATION_ROLLBACK_MODE", "false").lower() == "true"

# Performance and compatibility flags
ENABLE_LEGACY_COMPATIBILITY = os.getenv("ENABLE_LEGACY_COMPATIBILITY", "true").lower() == "true"
STRICT_INTERFACE_VALIDATION = os.getenv("STRICT_INTERFACE_VALIDATION", "false").lower() == "true"
ENABLE_PERFORMANCE_MONITORING = os.getenv("ENABLE_PERFORMANCE_MONITORING", "false").lower() == "true"

logger = logging.getLogger(__name__)


def log_feature_flag_status():
    """Log current feature flag configuration for debugging"""
    if ENABLE_MIGRATION_LOGGING:
        logger.info("=== FaultMaven Architecture Migration Status ===")
        logger.info(f"USE_REFACTORED_SERVICES: {USE_REFACTORED_SERVICES}")
        logger.info(f"USE_REFACTORED_API: {USE_REFACTORED_API}")
        logger.info(f"USE_DI_CONTAINER: {USE_DI_CONTAINER}")
        logger.info(f"ENABLE_LEGACY_COMPATIBILITY: {ENABLE_LEGACY_COMPATIBILITY}")
        logger.info(f"STRICT_INTERFACE_VALIDATION: {STRICT_INTERFACE_VALIDATION}")
        logger.info("===============================================")


def get_migration_strategy() -> str:
    """
    Determine current migration strategy based on feature flags
    
    Returns:
        String describing current migration phase
    """
    if MIGRATION_ROLLBACK_MODE:
        return "rollback_mode"
    elif USE_REFACTORED_API and USE_REFACTORED_SERVICES and USE_DI_CONTAINER:
        return "full_new_architecture"
    elif USE_REFACTORED_SERVICES and USE_DI_CONTAINER:
        return "backend_refactored_api_legacy"
    elif USE_REFACTORED_API and not USE_REFACTORED_SERVICES:
        return "api_refactored_backend_legacy"  # Not recommended
    elif not any([USE_REFACTORED_SERVICES, USE_REFACTORED_API, USE_DI_CONTAINER]):
        return "full_legacy_architecture"
    else:
        return "partial_migration"


def validate_feature_flag_combination():
    """
    Validate feature flag combinations and warn about problematic configs
    
    Raises:
        ValueError: If invalid combination is detected in strict mode
    """
    warnings = []
    errors = []
    
    # USE_REFACTORED_API without USE_REFACTORED_SERVICES is problematic
    if USE_REFACTORED_API and not USE_REFACTORED_SERVICES:
        warnings.append(
            "USE_REFACTORED_API=true with USE_REFACTORED_SERVICES=false may cause issues. "
            "Refactored API routes depend on refactored services."
        )
    
    # USE_REFACTORED_SERVICES without USE_DI_CONTAINER is suboptimal
    if USE_REFACTORED_SERVICES and not USE_DI_CONTAINER:
        warnings.append(
            "USE_REFACTORED_SERVICES=true with USE_DI_CONTAINER=false is suboptimal. "
            "Refactored services are designed to work with DI container."
        )
    
    # Rollback mode with other features enabled
    if MIGRATION_ROLLBACK_MODE and any([USE_REFACTORED_SERVICES, USE_REFACTORED_API, USE_DI_CONTAINER]):
        errors.append(
            "MIGRATION_ROLLBACK_MODE=true conflicts with other feature flags. "
            "In rollback mode, all new features should be disabled."
        )
    
    # Log warnings
    for warning in warnings:
        logger.warning(f"Feature flag warning: {warning}")
    
    # Handle errors
    for error in errors:
        logger.error(f"Feature flag error: {error}")
        if STRICT_INTERFACE_VALIDATION:
            raise ValueError(f"Invalid feature flag combination: {error}")


def is_migration_safe() -> bool:
    """
    Check if current feature flag combination is safe for production
    
    Returns:
        True if configuration is considered production-safe
    """
    try:
        validate_feature_flag_combination()
        strategy = get_migration_strategy()
        
        # Safe configurations
        safe_strategies = {
            "full_legacy_architecture",
            "full_new_architecture",
            "backend_refactored_api_legacy"
        }
        
        return strategy in safe_strategies and not MIGRATION_ROLLBACK_MODE
        
    except ValueError:
        return False


def get_container_type():
    """
    Determine which container to use based on feature flags
    
    Returns:
        Container instance (original or refactored)
    """
    if USE_DI_CONTAINER:
        if ENABLE_MIGRATION_LOGGING:
            logger.info("Using refactored DI container")
        from faultmaven.container_refactored import container
        return container
    else:
        if ENABLE_MIGRATION_LOGGING:
            logger.info("Using original container")
        from faultmaven.container import container
        return container


def get_service_dependencies(service_type: str):
    """
    Get appropriate service dependencies based on feature flags
    
    Args:
        service_type: Type of service ('agent', 'data', 'knowledge')
        
    Returns:
        Service instance (original or refactored)
    """
    container_instance = get_container_type()
    
    if USE_REFACTORED_SERVICES:
        if ENABLE_MIGRATION_LOGGING:
            logger.info(f"Using refactored {service_type} service")
        
        service_map = {
            'agent': 'get_agent_service',
            'data': 'get_data_service', 
            'knowledge': 'get_knowledge_service'
        }
        
        method_name = service_map.get(service_type)
        if method_name and hasattr(container_instance, method_name):
            return getattr(container_instance, method_name)()
        else:
            logger.warning(f"Refactored {service_type} service not available, falling back to original")
            return _get_original_service(service_type, container_instance)
    else:
        if ENABLE_MIGRATION_LOGGING:
            logger.info(f"Using original {service_type} service")
        return _get_original_service(service_type, container_instance)


def _get_original_service(service_type: str, container_instance):
    """Get original service from legacy container"""
    service_map = {
        'agent': 'agent_service',
        'data': 'data_service',
        'knowledge': 'knowledge_service'
    }
    
    attr_name = service_map.get(service_type)
    if attr_name and hasattr(container_instance, attr_name):
        return getattr(container_instance, attr_name)
    else:
        logger.error(f"Original {service_type} service not available")
        return None


# Initialize feature flags and log status
if __name__ == "__main__":
    log_feature_flag_status()
    print(f"Migration strategy: {get_migration_strategy()}")
    print(f"Migration safe: {is_migration_safe()}")
else:
    # Log on import if migration logging is enabled
    if ENABLE_MIGRATION_LOGGING:
        log_feature_flag_status()
    
    # Validate flags
    try:
        validate_feature_flag_combination()
    except ValueError as e:
        logger.error(f"Feature flag validation failed: {e}")
        if STRICT_INTERFACE_VALIDATION:
            raise