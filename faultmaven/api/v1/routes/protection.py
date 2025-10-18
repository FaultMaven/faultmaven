"""Protection API Routes

Purpose: Thin API layer for protection system operations with pure delegation pattern

This module follows clean API architecture principles by removing
all business logic from the API layer and delegating to the protection system.

Key Features:
- Removed all business logic (protection management, metrics calculation)
- Pure delegation to ProtectionSystem
- Proper dependency injection via DI container
- Clean separation of concerns (API vs Business logic)
- RESTful endpoint design

Architecture Pattern:
API Route (validation + delegation) → Protection System → Infrastructure Layer
"""

import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Request

from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException

router = APIRouter(prefix="/protection", tags=["protection"])

logger = logging.getLogger(__name__)


def get_protection_system(request: Request):
    """Dependency to get protection system from FastAPI app extra"""
    protection_system = request.app.extra.get("protection_system")
    if not protection_system:
        raise HTTPException(
            status_code=503, 
            detail="Protection system not available"
        )
    return protection_system


@router.get("/health")
@trace("api_protection_health")
async def get_protection_health(
    protection_system = Depends(get_protection_system)
) -> Optional[Dict[str, Any]]:
    """
    Get protection system health status
    
    Returns comprehensive health information for both basic and intelligent
    protection components including middleware status and configuration validation.
    
    Returns:
        Dict with protection system health status, active components, and validation results
        
    Raises:
        HTTPException: On service layer errors (503, 500)
    """
    logger.info("Retrieving protection system health status")
    
    try:
        # Pure delegation to protection system
        health_status = await protection_system.get_protection_status()
        
        # Handle None response gracefully
        if health_status is not None:
            logger.info(f"Protection health check completed: {health_status.get('overall_status')}")
        else:
            logger.info("Protection health check completed: None response from protection system")
        
        return health_status
        
    except ValidationException as e:
        logger.warning(f"Protection health validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except Exception as e:
        logger.error(f"Protection health check failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve protection system health"
        )


@router.get("/metrics")
@trace("api_protection_metrics")
async def get_protection_metrics(
    protection_system = Depends(get_protection_system)
) -> Optional[Dict[str, Any]]:
    """
    Get protection system metrics for monitoring
    
    Returns detailed metrics for rate limiting, request deduplication, 
    behavioral analysis, ML anomaly detection, and reputation system.
    
    Returns:
        Dict with protection system metrics including request counts, 
        protection rates, and component-specific statistics
        
    Raises:
        HTTPException: On service layer errors (503, 500)
    """
    logger.info("Retrieving protection system metrics")
    
    try:
        # Pure delegation to protection system
        metrics = await protection_system.get_protection_metrics()
        
        logger.info("Protection metrics retrieval completed")
        return metrics
        
    except ValidationException as e:
        logger.warning(f"Protection metrics validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except Exception as e:
        logger.error(f"Protection metrics retrieval failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve protection system metrics"
        )


@router.get("/config")
@trace("api_protection_config")
async def get_protection_config(
    protection_system = Depends(get_protection_system)
) -> Optional[Dict[str, Any]]:
    """
    Get current protection system configuration
    
    Returns sanitized configuration information (no sensitive data)
    for both basic and intelligent protection components.
    
    Returns:
        Dict with protection configuration including enabled features,
        rate limits, timeouts, and security settings (sanitized)
        
    Raises:
        HTTPException: On service layer errors (503, 500)
    """
    logger.info("Retrieving protection system configuration")
    
    try:
        # Get configuration from protection system's basic and intelligent configs
        last_update = protection_system.protection_status["last_update"]
        # Handle both datetime objects and string timestamps
        if hasattr(last_update, 'isoformat'):
            timestamp = to_json_compatible(last_update)
        else:
            # Already a string timestamp
            timestamp = str(last_update)
        
        config = {
            "timestamp": timestamp,
            "system_info": {
                "environment": protection_system.environment,
                "basic_protection_enabled": protection_system.basic_protection_enabled,
                "intelligent_protection_enabled": protection_system.intelligent_protection_enabled
            },
            "basic_protection": {
                "enabled": protection_system.basic_config.enabled,
                "rate_limiting_enabled": protection_system.basic_config.rate_limiting_enabled,
                "deduplication_enabled": protection_system.basic_config.deduplication_enabled,
                "timeouts_enabled": protection_system.basic_config.timeouts.enabled,
                "fail_open_on_redis_error": protection_system.basic_config.fail_open_on_redis_error
            },
            "intelligent_protection": {
                "behavioral_analysis": protection_system.intelligent_config.enable_behavioral_analysis,
                "ml_detection": protection_system.intelligent_config.enable_ml_detection,
                "reputation_system": protection_system.intelligent_config.enable_reputation_system,
                "smart_circuit_breakers": protection_system.intelligent_config.enable_smart_circuit_breakers
            }
        }
        
        logger.info("Protection configuration retrieval completed")
        return config
        
    except ValidationException as e:
        logger.warning(f"Protection config validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except Exception as e:
        logger.error(f"Protection config retrieval failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve protection system configuration"
        )