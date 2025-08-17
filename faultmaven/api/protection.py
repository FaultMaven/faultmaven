"""
Unified Protection System for FaultMaven

This module provides a comprehensive protection system that integrates:
- Basic protection: Rate limiting, request deduplication, timeouts
- Intelligent protection: Behavioral analysis, ML anomaly detection, reputation system
- Unified configuration and monitoring
- Graceful degradation and fallback mechanisms
"""

import logging
import os
from typing import Optional, Dict, Any
from datetime import datetime
from fastapi import FastAPI, APIRouter
from faultmaven.models.interfaces import ISessionStore

# Basic protection imports
from ..config.protection import (
    load_protection_settings,
    get_development_protection_settings,
    get_production_protection_settings,
    validate_protection_settings
)
from ..models.protection import ProtectionSettings
from ..infrastructure.protection import TimeoutHandler
from .middleware import RateLimitMiddleware, DeduplicationMiddleware

# Intelligent protection imports
from .middleware.intelligent_protection import IntelligentProtectionMiddleware
from ..infrastructure.protection.protection_coordinator import ProtectionConfig
from ..infrastructure.protection.rate_limiter import RedisRateLimiter
from ..infrastructure.protection.request_hasher import RequestHasher


logger = logging.getLogger(__name__)


class ProtectionSystem:
    """
    Unified Protection System for FaultMaven
    
    Provides both basic and intelligent protection mechanisms:
    - Basic: Rate limiting, request deduplication, timeouts
    - Intelligent: Behavioral analysis, ML anomaly detection, reputation system
    """

    def __init__(self, app: FastAPI, session_store: Optional[ISessionStore] = None):
        self.app = app
        self.session_store = session_store
        self.logger = logging.getLogger(__name__)
        
        # Configuration
        self.environment = os.getenv("ENVIRONMENT", "development")
        self.basic_protection_enabled = os.getenv("BASIC_PROTECTION_ENABLED", "true").lower() == "true"
        self.intelligent_protection_enabled = os.getenv("INTELLIGENT_PROTECTION_ENABLED", "true").lower() == "true"
        
        # Middleware instances
        self.rate_limit_middleware = None
        self.deduplication_middleware = None
        self.intelligent_middleware = None
        
        # Protection configurations
        self.basic_config = self._load_basic_config()
        self.intelligent_config = self._create_intelligent_config()
        
        # System status
        self.protection_status = {
            "basic_protection_active": False,
            "intelligent_protection_active": False,
            "total_requests": 0,
            "protected_requests": 0,
            "last_update": datetime.utcnow()
        }

    def _load_basic_config(self) -> ProtectionSettings:
        """Load basic protection configuration"""
        if self.environment == "production":
            return get_production_protection_settings()
        elif self.environment == "development":
            return get_development_protection_settings()
        else:
            return load_protection_settings()

    def _create_intelligent_config(self) -> ProtectionConfig:
        """Create intelligent protection configuration"""
        return ProtectionConfig(
            # Behavioral analysis
            enable_behavioral_analysis=os.getenv("BEHAVIORAL_ANALYSIS_ENABLED", "true").lower() == "true",
            behavioral_analysis_window=int(os.getenv("BEHAVIOR_ANALYSIS_WINDOW", "3600")),
            behavioral_pattern_threshold=float(os.getenv("BEHAVIOR_PATTERN_THRESHOLD", "0.8")),
            
            # ML anomaly detection
            enable_ml_detection=os.getenv("ML_ANOMALY_DETECTION_ENABLED", "true").lower() == "true",
            ml_model_path=os.getenv("ML_MODEL_PATH", "/tmp/faultmaven_ml_models"),
            ml_training_enabled=os.getenv("ML_TRAINING_ENABLED", "true").lower() == "true",
            ml_online_learning=os.getenv("ML_ONLINE_LEARNING_ENABLED", "true").lower() == "true",
            
            # Reputation system
            enable_reputation_system=os.getenv("REPUTATION_SYSTEM_ENABLED", "true").lower() == "true",
            reputation_decay_rate=float(os.getenv("REPUTATION_DECAY_RATE", "0.05")),
            reputation_recovery_threshold=float(os.getenv("REPUTATION_RECOVERY_THRESHOLD", "0.1")),
            
            # Smart circuit breakers
            enable_smart_circuit_breakers=os.getenv("SMART_CIRCUIT_BREAKERS_ENABLED", "true").lower() == "true",
            circuit_failure_threshold=int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "5")),
            circuit_timeout_seconds=int(os.getenv("CIRCUIT_TIMEOUT_SECONDS", "60")),
            
            # System monitoring
            monitoring_interval=int(os.getenv("PROTECTION_MONITORING_INTERVAL", "300")),
            cleanup_interval=int(os.getenv("PROTECTION_CLEANUP_INTERVAL", "3600"))
        )

    async def setup_protection_system(self) -> Dict[str, Any]:
        """
        Set up the complete unified protection system
        
        Returns:
            Dictionary with setup results and configuration
        """
        setup_results = {
            "protection_system": "FaultMaven Unified Protection",
            "basic_protection": {"enabled": False, "components": []},
            "intelligent_protection": {"enabled": False, "components": []},
            "middleware_order": [],
            "total_protections": 0
        }
        
        try:
            self.logger.info("Setting up FaultMaven Unified Protection System")
            
            # Basic Protection Setup
            if self.basic_protection_enabled:
                basic_result = await self._setup_basic_protection()
                setup_results["basic_protection"] = basic_result
                self.protection_status["basic_protection_active"] = basic_result["enabled"]
            
            # Intelligent Protection Setup
            if self.intelligent_protection_enabled:
                intelligent_result = await self._setup_intelligent_protection()
                setup_results["intelligent_protection"] = intelligent_result
                self.protection_status["intelligent_protection_active"] = intelligent_result["enabled"]
            
            # Calculate total protections
            total_protections = (
                len(setup_results["basic_protection"].get("components", [])) +
                len(setup_results["intelligent_protection"].get("components", []))
            )
            setup_results["total_protections"] = total_protections
            
            # Middleware order (important for proper protection)
            setup_results["middleware_order"] = [
                "CORSMiddleware",
                "IntelligentProtectionMiddleware",  # Advanced analysis first
                "DeduplicationMiddleware",  # Block duplicates
                "RateLimitMiddleware",  # Rate limiting
                "GZipMiddleware",
                "Other middlewares..."
            ]
            
            self.protection_status["last_update"] = datetime.utcnow()
            
            self.logger.info(f"Protection system setup complete: "
                           f"Basic: {setup_results['basic_protection']['enabled']}, "
                           f"Intelligent: {setup_results['intelligent_protection']['enabled']}, "
                           f"Total protections: {total_protections}")
            
            return setup_results
            
        except Exception as e:
            self.logger.error(f"Error setting up protection system: {e}")
            setup_results["error"] = str(e)
            return setup_results

    async def _setup_basic_protection(self) -> Dict[str, Any]:
        """Set up basic protection components (rate limiting, deduplication, timeouts)"""
        basic_result = {
            "enabled": True,
            "components": [],
            "configuration": self.basic_config,
            "middleware_added": []
        }
        
        try:
            # Validate settings
            validation = validate_protection_settings(self.basic_config)
            if not validation["valid"]:
                self.logger.error(f"Basic protection settings validation failed: {validation['errors']}")
                basic_result["enabled"] = False
                basic_result["validation_errors"] = validation["errors"]
                return basic_result

            if validation["warnings"]:
                self.logger.warning(f"Basic protection settings warnings: {validation['warnings']}")
                basic_result["warnings"] = validation["warnings"]

            # Check if protection is enabled
            if not self.basic_config.enabled:
                self.logger.info("Basic protection disabled by configuration")
                basic_result["enabled"] = False
                return basic_result

            # Add middleware in reverse order (FastAPI adds them as a stack)
            # Last added = first executed
            
            # 2. Request Deduplication (executed second)
            if self.basic_config.deduplication_enabled:
                self.app.add_middleware(
                    DeduplicationMiddleware,
                    settings=self.basic_config,
                    redis_url=self.basic_config.redis_url
                )
                basic_result["middleware_added"].append("deduplication")
                basic_result["components"].append("request_deduplication")
                self.logger.info("Added request deduplication middleware")
            
            # 1. Rate Limiting (executed first)
            if self.basic_config.rate_limiting_enabled:
                self.app.add_middleware(
                    RateLimitMiddleware,
                    settings=self.basic_config,
                    redis_url=self.basic_config.redis_url
                )
                basic_result["middleware_added"].append("rate_limiting")
                basic_result["components"].append("rate_limiting")
                self.logger.info("Added rate limiting middleware")
            
            # Timeout protection (handled at agent level)
            if self.basic_config.timeouts.enabled:
                basic_result["components"].append("timeout_management")
                self.logger.info("Timeout protection configured")
            
            if not basic_result["components"]:
                basic_result["enabled"] = False
                self.logger.warning("Basic protection disabled: no components available")
            
        except Exception as e:
            self.logger.error(f"Error setting up basic protection: {e}")
            basic_result["enabled"] = False
            basic_result["error"] = str(e)
        
        return basic_result

    async def _setup_intelligent_protection(self) -> Dict[str, Any]:
        """Set up intelligent protection components"""
        intelligent_result = {
            "enabled": True,
            "components": [],
            "configuration": self.intelligent_config.__dict__,
            "middleware_added": []
        }
        
        try:
            # Add intelligent protection middleware
            self.intelligent_middleware = IntelligentProtectionMiddleware(
                app=self.app,
                config=self.intelligent_config,
                session_store=self.session_store,
                enabled=True
            )
            
            self.app.add_middleware(
                IntelligentProtectionMiddleware,
                config=self.intelligent_config,
                session_store=self.session_store,
                enabled=True
            )
            
            intelligent_result["middleware_added"].append("IntelligentProtectionMiddleware")
            
            # Add components based on configuration
            if self.intelligent_config.enable_behavioral_analysis:
                intelligent_result["components"].append("behavioral_analysis")
                self.logger.info("✅ Behavioral analysis enabled")
            
            if self.intelligent_config.enable_ml_detection:
                intelligent_result["components"].append("ml_anomaly_detection")
                self.logger.info("✅ ML anomaly detection enabled")
            
            if self.intelligent_config.enable_reputation_system:
                intelligent_result["components"].append("reputation_system")
                self.logger.info("✅ Reputation system enabled")
            
            if self.intelligent_config.enable_smart_circuit_breakers:
                intelligent_result["components"].append("smart_circuit_breakers")
                self.logger.info("✅ Smart circuit breakers enabled")
            
            if not intelligent_result["components"]:
                intelligent_result["enabled"] = False
                self.logger.warning("⚠️ Intelligent protection disabled: no components enabled")
            else:
                self.logger.info(f"✅ Intelligent protection middleware added with {len(intelligent_result['components'])} components")
            
        except Exception as e:
            self.logger.error(f"Error setting up intelligent protection: {e}")
            intelligent_result["enabled"] = False
            intelligent_result["error"] = str(e)
        
        return intelligent_result

    async def get_protection_status(self) -> Dict[str, Any]:
        """Get comprehensive protection system status"""
        status = {
            "system": "FaultMaven Unified Protection",
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "overall_status": "active" if (
                self.protection_status["basic_protection_active"] or 
                self.protection_status["intelligent_protection_active"]
            ) else "inactive",
            "basic_protection": {
                "status": "active" if self.protection_status["basic_protection_active"] else "inactive",
                "components": []
            },
            "intelligent_protection": {
                "status": "active" if self.protection_status["intelligent_protection_active"] else "inactive",
                "components": []
            },
            "middleware_status": {},
            "statistics": self.protection_status.copy()
        }
        
        try:
            # Basic protection status
            if self.rate_limit_middleware:
                status["basic_protection"]["components"].append("rate_limiting")
            
            if self.deduplication_middleware:
                status["basic_protection"]["components"].append("request_deduplication")
            
            # Intelligent protection status
            if self.intelligent_middleware:
                try:
                    intelligent_status = await self.intelligent_middleware.get_middleware_status()
                    status["intelligent_protection"] = {
                        **status["intelligent_protection"],
                        **intelligent_status
                    }
                except Exception as e:
                    self.logger.error(f"Error getting intelligent middleware status: {e}")
            
        except Exception as e:
            self.logger.error(f"Error getting protection status: {e}")
            status["error"] = str(e)
        
        return status

    async def get_protection_metrics(self) -> Dict[str, Any]:
        """Get protection system metrics for monitoring"""
        metrics = {
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "basic_metrics": {},
            "intelligent_metrics": {},
            "combined_metrics": {
                "total_requests_processed": self.protection_status["total_requests"],
                "total_requests_protected": self.protection_status["protected_requests"],
                "protection_rate": 0.0
            }
        }
        
        try:
            # Calculate protection rate
            if self.protection_status["total_requests"] > 0:
                metrics["combined_metrics"]["protection_rate"] = (
                    self.protection_status["protected_requests"] / 
                    self.protection_status["total_requests"]
                ) * 100
            
            # Intelligent protection metrics
            if self.intelligent_middleware and self.intelligent_middleware.initialized:
                try:
                    coordinator_status = await self.intelligent_middleware.coordinator.get_system_status()
                    metrics["intelligent_metrics"] = coordinator_status.get("statistics", {})
                except Exception as e:
                    self.logger.error(f"Error getting intelligent protection metrics: {e}")
            
        except Exception as e:
            self.logger.error(f"Error getting protection metrics: {e}")
            metrics["error"] = str(e)
        
        return metrics

    def create_timeout_handler(self) -> TimeoutHandler:
        """Create a timeout handler instance"""
        return TimeoutHandler(self.basic_config.timeouts)

    async def shutdown_protection_system(self):
        """Graceful shutdown of the protection system"""
        try:
            self.logger.info("Shutting down unified protection system...")
            
            # Shutdown intelligent middleware
            if self.intelligent_middleware:
                await self.intelligent_middleware.shutdown()
            
            # Basic middleware doesn't need explicit shutdown
            
            self.protection_status["basic_protection_active"] = False
            self.protection_status["intelligent_protection_active"] = False
            
            self.logger.info("Unified protection system shutdown complete")
            
        except Exception as e:
            self.logger.error(f"Error during protection system shutdown: {e}")


# Legacy compatibility functions
def setup_protection_middleware(
    app: FastAPI,
    settings: Optional[ProtectionSettings] = None,
    environment: str = "development",
    session_store: Optional[ISessionStore] = None
) -> Dict[str, Any]:
    """
    Legacy compatibility function for basic protection middleware setup
    
    Args:
        app: FastAPI application instance
        settings: Protection settings (if None, loads from environment)
        environment: Environment name (development/production/testing)
        session_store: Optional session store for intelligent protection
        
    Returns:
        Dictionary with setup status and middleware information
    """
    
    # If session store is provided, use unified protection system
    if session_store is not None:
        protection_system = ProtectionSystem(app, session_store)
        protection_system.environment = environment
        
        # Override basic config if settings provided
        if settings is not None:
            protection_system.basic_config = settings
        
        # Setup and return comprehensive status
        import asyncio
        loop = asyncio.get_event_loop()
        setup_results = loop.run_until_complete(protection_system.setup_protection_system())
        
        # Transform to legacy format for compatibility
        legacy_result = {
            "protection_enabled": setup_results.get("basic_protection", {}).get("enabled", False),
            "middleware_added": setup_results.get("basic_protection", {}).get("middleware_added", []),
            "settings_source": "provided" if settings else "environment",
            "validation": {"valid": True, "warnings": [], "errors": []},
            "warnings": setup_results.get("basic_protection", {}).get("warnings", []),
            "advanced_protection": {
                "enabled": setup_results.get("intelligent_protection", {}).get("enabled", False),
                "components": setup_results.get("intelligent_protection", {}).get("components", [])
            }
        }
        
        # Add protection system to app for status endpoints
        app.extra["protection_system"] = protection_system
        
        return legacy_result
    
    # Otherwise, use basic protection only (legacy behavior)
    setup_info = {
        "protection_enabled": False,
        "middleware_added": [],
        "settings_source": "none",
        "validation": None,
        "warnings": []
    }
    
    try:
        # Load settings if not provided
        if settings is None:
            if environment == "production":
                settings = get_production_protection_settings()
                setup_info["settings_source"] = "production_defaults"
            elif environment == "development":
                settings = get_development_protection_settings()
                setup_info["settings_source"] = "development_defaults"
            else:
                settings = load_protection_settings()
                setup_info["settings_source"] = "environment"
        else:
            setup_info["settings_source"] = "provided"
        
        # Validate settings
        validation = validate_protection_settings(settings)
        setup_info["validation"] = validation
        
        if not validation["valid"]:
            logger.error(f"Protection settings validation failed: {validation['errors']}")
            return setup_info
        
        if validation["warnings"]:
            logger.warning(f"Protection settings warnings: {validation['warnings']}")
            setup_info["warnings"] = validation["warnings"]
        
        # Check if protection is enabled
        if not settings.enabled:
            logger.info("Protection middleware disabled by configuration")
            return setup_info
        
        setup_info["protection_enabled"] = True
        
        # Add middleware in reverse order (FastAPI adds them as a stack)
        # Last added = first executed
        
        # 2. Request Deduplication (executed second)
        if settings.deduplication_enabled:
            app.add_middleware(
                DeduplicationMiddleware,
                settings=settings,
                redis_url=settings.redis_url
            )
            setup_info["middleware_added"].append("deduplication")
            logger.info("Added request deduplication middleware")
        
        # 1. Rate Limiting (executed first)
        if settings.rate_limiting_enabled:
            app.add_middleware(
                RateLimitMiddleware,
                settings=settings,
                redis_url=settings.redis_url
            )
            setup_info["middleware_added"].append("rate_limiting")
            logger.info("Added rate limiting middleware")
        
        logger.info(f"Protection middleware setup complete: {len(setup_info['middleware_added'])} middleware added")
        
    except Exception as e:
        logger.error(f"Failed to setup protection middleware: {e}")
        setup_info["error"] = str(e)
        
        # If we fail to setup protection, we should not fail the entire app
        # unless we're in production mode with fail_closed
        if settings and not settings.fail_open_on_redis_error:
            raise
    
    return setup_info


def create_timeout_handler(settings: Optional[ProtectionSettings] = None) -> TimeoutHandler:
    """
    Create a timeout handler instance with the given settings
    
    Args:
        settings: Protection settings (loads from environment if None)
        
    Returns:
        TimeoutHandler instance
    """
    if settings is None:
        settings = load_protection_settings()
    
    return TimeoutHandler(settings.timeouts)


def get_protection_health_endpoints():
    """
    Get FastAPI endpoints for protection system health monitoring
    
    Returns:
        Dictionary of endpoint functions that can be added to FastAPI routers
    """
    
    async def protection_health():
        """Get overall protection system health"""
        try:
            settings = load_protection_settings()
            validation = validate_protection_settings(settings)
            
            return {
                "protection_enabled": settings.enabled,
                "rate_limiting_enabled": settings.rate_limiting_enabled,
                "deduplication_enabled": settings.deduplication_enabled,
                "timeouts_enabled": settings.timeouts.enabled,
                "redis_url": settings.redis_url,
                "validation": validation,
                "status": "healthy" if validation["valid"] else "unhealthy"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    async def protection_metrics():
        """Get protection system metrics"""
        # This would be populated by the actual middleware instances
        # For now, return a placeholder structure
        return {
            "rate_limiting": {
                "requests_checked": 0,
                "requests_blocked": 0,
                "errors": 0
            },
            "deduplication": {
                "requests_checked": 0,
                "duplicates_found": 0,
                "cache_hits": 0
            },
            "timeouts": {
                "total_operations": 0,
                "timeouts_triggered": 0,
                "active_operations": 0
            }
        }
    
    async def protection_config():
        """Get current protection configuration"""
        try:
            settings = load_protection_settings()
            
            # Return sanitized config (no sensitive data)
            return {
                "general": {
                    "enabled": settings.enabled,
                    "fail_open_on_redis_error": settings.fail_open_on_redis_error,
                    "has_bypass_headers": len(settings.protection_bypass_headers) > 0
                },
                "rate_limiting": {
                    "enabled": settings.rate_limiting_enabled,
                    "limits": {
                        name: {"requests": config.requests, "window": config.window, "enabled": config.enabled}
                        for name, config in settings.rate_limits.items()
                    }
                },
                "deduplication": {
                    "enabled": settings.deduplication_enabled,
                    "configs": {
                        name: {"ttl": config.ttl, "enabled": config.enabled}
                        for name, config in settings.deduplication.items()
                    }
                },
                "timeouts": {
                    "enabled": settings.timeouts.enabled,
                    "agent_total": settings.timeouts.agent_total,
                    "agent_phase": settings.timeouts.agent_phase,
                    "llm_call": settings.timeouts.llm_call,
                    "emergency_shutdown": settings.timeouts.emergency_shutdown
                }
            }
        except Exception as e:
            return {
                "error": str(e),
                "status": "configuration_error"
            }
    
    return {
        "health": protection_health,
        "metrics": protection_metrics,
        "config": protection_config
    }


def create_protection_router():
    """
    Create a FastAPI router with protection monitoring endpoints
    
    Returns:
        APIRouter instance with health and metrics endpoints
    """
    router = APIRouter(prefix="/protection", tags=["protection"])
    endpoints = get_protection_health_endpoints()
    
    router.add_api_route("/health", endpoints["health"], methods=["GET"])
    router.add_api_route("/metrics", endpoints["metrics"], methods=["GET"])
    router.add_api_route("/config", endpoints["config"], methods=["GET"])
    
    return router


# Main setup function for intelligent protection
async def setup_unified_protection_middleware(
    app: FastAPI, 
    session_store: Optional[ISessionStore] = None, 
    environment: str = "development"
) -> Dict[str, Any]:
    """
    Main function to set up unified protection middleware
    
    Args:
        app: FastAPI application instance
        session_store: Session store for intelligent protection features
        environment: Deployment environment
        
    Returns:
        Setup results and configuration
    """
    protection_system = ProtectionSystem(app, session_store)
    
    # Set environment
    protection_system.environment = environment
    
    # Setup protection system
    setup_results = await protection_system.setup_protection_system()
    
    # Add status endpoints
    @app.get("/health/protection")
    async def get_protection_health():
        """Protection system health endpoint"""
        return await protection_system.get_protection_status()
    
    @app.get("/health/protection/metrics")
    async def get_protection_metrics():
        """Protection system metrics endpoint"""
        return await protection_system.get_protection_metrics()
    
    # Store protection system for later access
    app.extra["protection_system"] = protection_system
    
    return setup_results