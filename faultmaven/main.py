"""main.py

Purpose: FastAPI entry point and central application setup

Requirements:
--------------------------------------------------------------------------------
• Initialize the core FastAPI application instance
• Configure CORS middleware for browser extension
• Include API routers from data_ingestion, query_processing, and kb_management
• Set up startup/shutdown event handlers
• Integrate Comet Opik tracing middleware

Key Components:
--------------------------------------------------------------------------------
  app = FastAPI(title='FaultMaven API')
  app.include_router(data_ingestion.router, prefix='/api/v1')
  @app.on_event('startup')

Technology Stack:
--------------------------------------------------------------------------------
FastAPI, Uvicorn, Comet Opik

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# Load environment variables from .env file
load_dotenv()
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# Configure enhanced logging system first
from .infrastructure.logging.config import get_logger
logger = get_logger(__name__)

# Import API routes
from .api.v1.routes import agent, data, knowledge, session

from .infrastructure.observability.tracing import init_opik_tracing
from .api.middleware.logging import LoggingMiddleware
from .session_management import SessionManager

# Optional opik middleware import
try:
    import opik
    # Try different middleware import patterns for different Opik versions
    try:
        from opik.integrations.fastapi import OpikMiddleware
        OPIK_MIDDLEWARE_AVAILABLE = True
    except ImportError:
        try:
            from opik import OpikMiddleware
            OPIK_MIDDLEWARE_AVAILABLE = True
        except ImportError:
            OPIK_MIDDLEWARE_AVAILABLE = False
            logger.info("Opik middleware class not available, tracing will work without middleware")
    
    OPIK_AVAILABLE = True
    logger.info("Opik SDK loaded successfully")
except ImportError:
    logger.warning("Opik not available, running without tracing")
    OPIK_AVAILABLE = False
    OPIK_MIDDLEWARE_AVAILABLE = False

# Note: For local Opik, we'll rely on environment variable configuration
# The Opik SDK should pick up the custom URL and headers automatically

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    logger.info("Starting FaultMaven API server...")

    # Initialize and validate configuration first
    logger.info("Validating configuration...")
    try:
        from .config.configuration_manager import get_config
        config = get_config()
        if not config.validate():
            logger.error("Configuration validation failed")
            raise RuntimeError("Invalid configuration")
        logger.info("Configuration validated successfully")
        
        # Make configuration available to app
        app.extra["config"] = config
    except Exception as e:
        logger.error(f"Configuration initialization failed: {e}")
        raise

    # Initialize core services with K8s support
    # Store SessionManager in app.extra for centralized access
    # SessionManager now uses the DI container pattern with ISessionStore interface
    app.extra["session_manager"] = SessionManager()

    # Pre-load expensive ML models during startup (not per-request)
    logger.info("Pre-loading ML models...")
    try:
        from .infrastructure.model_cache import model_cache
        bge_model = model_cache.get_bge_m3_model()
        if bge_model:
            logger.info("✅ BGE-M3 model pre-loaded successfully")
        else:
            logger.warning("⚠️ BGE-M3 model not available")
    except Exception as e:
        logger.warning(f"Failed to pre-load ML models: {e}")

    # Initialize DI container
    from .container import container
    
    # Initialize container and validate completion
    logger.info("🚀 Starting DI container initialization...")
    container.initialize()
    app.extra["di_container"] = container
    
    # Validate initialization succeeded
    if not getattr(container, '_initialized', False):
        logger.error("❌ DI container initialization failed - _initialized flag is False")
        raise RuntimeError("DI container initialization failed")
    
    # Health check the container
    health = container.health_check()
    logger.info(f"📊 DI container health: {health['status']}")
    if health['status'] == 'healthy':
        logger.info("✅ DI container ready - all components initialized successfully during startup") 
    else:
        logger.warning(f"⚠️ DI container degraded: {health['components']}")
        
    # Test critical services to ensure they're available
    try:
        agent_service = container.get_agent_service()
        session_service = container.get_session_service()
        logger.info("✅ Critical services validated - container ready for requests (no lazy initialization needed)")
    except Exception as e:
        logger.error(f"❌ Critical services validation failed: {e}")
        raise RuntimeError(f"Critical services not available: {e}")
    
    logger.info("🎯 Container initialization COMPLETE during startup - requests will be fast!")

    # Setup tracing
    init_opik_tracing()

    # Initialize Phase 2 monitoring components
    try:
        from .infrastructure.monitoring.apm_integration import apm_integration
        from .infrastructure.monitoring.alerting import alert_manager, setup_default_alert_rules
        
        # Start APM integration background export
        apm_integration.start_background_export()
        logger.info("✅ APM integration started")
        
        # Set up default alert rules
        setup_default_alert_rules()
        logger.info("✅ Default alert rules configured")
        
        logger.info("✅ Phase 2 monitoring components initialized")
        
    except Exception as e:
        logger.warning(f"Phase 2 monitoring initialization failed (non-critical): {e}")

    logger.info("🚀 FaultMaven API server startup COMPLETE - ready to serve fast requests!")

    yield

    # Shutdown
    logger.info("Shutting down FaultMaven API server...")

    # Cleanup resources
    if "session_manager" in app.extra:
        # Cleanup any active sessions
        session_manager = app.extra["session_manager"]
        try:
            cleaned_count = await session_manager.cleanup_inactive_sessions()
            logger.info(f"Cleaned up {cleaned_count} expired sessions during shutdown")
            
            # Close session manager (stops scheduler and connections)
            await session_manager.close()
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")

    # Cleanup Phase 2 monitoring components
    try:
        from .infrastructure.monitoring.apm_integration import apm_integration
        
        # Stop APM background export
        apm_integration.stop_background_export()
        
        # Flush any remaining metrics
        await apm_integration.flush_metrics()
        
        logger.info("✅ Phase 2 monitoring components cleaned up")
        
    except Exception as e:
        logger.warning(f"Phase 2 monitoring cleanup failed (non-critical): {e}")

    logger.info("FaultMaven API server shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="FaultMaven API",
    description="AI-powered troubleshooting assistant for Engineers, "
    "SREs, and DevOps professionals",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add middleware in optimized order to prevent duplicates
def setup_middleware():
    """Setup middleware - only log when not in test mode"""
    import sys
    
    # Skip verbose logging during test collection
    if os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        logging_enabled = False
    else:
        logging_enabled = True
    
    if logging_enabled:
        logger.info("Starting middleware registration...")
        logger.info(f"Initial middleware stack: {[type(m).__name__ for m in app.user_middleware]}")

    # 1. CORS middleware (first - handles preflight requests)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "chrome-extension://*",  # Browser extension
            "http://localhost:3000",  # Local development
            "http://localhost:8000",  # Local API
            "https://faultmaven.ai",  # Production domain
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if logging_enabled:
        logger.info(f"After CORS middleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 2. GZip middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if logging_enabled:
        logger.info(f"After GZip middleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 3. New unified logging middleware (integrates with Phase 1 & 2 infrastructure)
    if logging_enabled:
        logger.info("Adding LoggingMiddleware to FastAPI app")
    app.add_middleware(LoggingMiddleware)
    if logging_enabled:
        logger.info(f"After LoggingMiddleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 3.5. Performance tracking middleware (Phase 2 enhancement)
    from .api.middleware.performance import PerformanceTrackingMiddleware
    if logging_enabled:
        logger.info("Adding PerformanceTrackingMiddleware to FastAPI app")
    app.add_middleware(PerformanceTrackingMiddleware, service_name="faultmaven_api")
    if logging_enabled:
        logger.info(f"After PerformanceTrackingMiddleware: {[type(m).__name__ for m in app.user_middleware]}")

    # 4. Opik tracing middleware (if available) - now coordinated with unified logging
    if OPIK_AVAILABLE and OPIK_MIDDLEWARE_AVAILABLE:
        if logging_enabled:
            if os.getenv("OPIK_USE_LOCAL", "true").lower() == "true":
                logger.info("Adding OpikMiddleware for local Opik instance")
            else:
                logger.info("Adding OpikMiddleware for cloud instance")
        app.add_middleware(OpikMiddleware)
        if logging_enabled:
            logger.info(f"After Opik middleware: {[type(m).__name__ for m in app.user_middleware]}")
    elif OPIK_AVAILABLE and logging_enabled:
        logger.info("Opik SDK available but middleware not found - tracing will work at function level")

    if logging_enabled:
        logger.info(f"Final middleware stack: {[type(m).__name__ for m in app.user_middleware]}")

# Setup middleware
setup_middleware()

# Include API routers
app.include_router(data.router, prefix="/api/v1", tags=["data_ingestion"])

app.include_router(agent.router, prefix="/api/v1", tags=["query_processing"])

app.include_router(knowledge.router, prefix="/api/v1", tags=["knowledge_base"])

# Include kb router for backward compatibility with /kb/ prefix
app.include_router(knowledge.kb_router, prefix="/api/v1", tags=["knowledge_base"])

app.include_router(session.router, prefix="/api/v1", tags=["session_management"])





# Custom exception handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Custom 404 handler to ensure consistent error responses"""
    return JSONResponse(
        status_code=404,
        content={"detail": "Not Found"}
    )


@app.exception_handler(500) 
async def internal_server_error_handler(request: Request, exc):
    """Custom 500 handler for internal server errors"""
    logger.error(f"Internal server error on {request.method} {request.url}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "message": "FaultMaven API",
        "version": "1.0.0",
        "description": "AI-powered troubleshooting assistant",
        "docs": "/docs",
        "health": "/health"
    }


@app.get("/health")
async def health_check():
    """Enhanced health check endpoint with component-specific metrics and SLA monitoring."""
    from .infrastructure.health.component_monitor import component_monitor
    from .infrastructure.health.sla_tracker import sla_tracker
    
    # Get component health status
    try:
        component_health_results = await component_monitor.check_all_components()
        overall_status, overall_summary = component_monitor.get_overall_health_status()
        sla_summary = sla_tracker.get_sla_summary()
        
        # Enhanced health status with component details
        health_status = {
            "status": overall_status.value,
            "timestamp": datetime.utcnow().isoformat(),
            "overall_sla": sla_summary["overall_sla"],
            "components": {},
            "services": {"session_manager": "active", "api": "running"},
            "summary": overall_summary,
            "sla_status": {
                "active_breaches": sla_summary["active_breaches"],
                "total_breaches_24h": sla_summary["total_breaches_24h"],
                "worst_performing": sla_summary["worst_performing_component"],
                "best_performing": sla_summary["best_performing_component"]
            }
        }
        
        # Add detailed component information
        for component_name, component_health in component_health_results.items():
            health_status["components"][component_name] = {
                "status": component_health.status.value,
                "response_time_ms": component_health.response_time_ms,
                "last_error": component_health.last_error,
                "uptime_seconds": component_health.uptime_seconds,
                "sla_current": component_health.sla_current,
                "error_count_24h": component_health.error_count_24h,
                "success_count_24h": component_health.success_count_24h,
                "dependencies": component_health.dependencies,
                "metadata": component_health.metadata
            }
        
    except Exception as e:
        logger.error(f"Enhanced health check failed: {e}")
        # Fallback to basic health status
        health_status = {
            "status": "degraded",
            "timestamp": datetime.utcnow().isoformat(),
            "error": "Enhanced health monitoring unavailable",
            "services": {"session_manager": "unknown", "api": "running"}
        }
    
    # Add session manager health and metrics
    try:
        if "session_manager" in app.extra:
            session_manager = app.extra["session_manager"]
            session_metrics = session_manager.get_session_metrics()
            
            # Determine session manager health status
            session_status = "healthy"
            if session_metrics["active_sessions"] > 1000:
                session_status = "degraded"
            elif session_metrics["memory_usage_mb"] > session_manager.max_memory_mb:
                session_status = "degraded"
            
            health_status["services"]["session_manager"] = {
                "status": session_status,
                "metrics": session_metrics
            }
    except Exception as e:
        logger.warning(f"Failed to get session manager health: {e}")
        health_status["services"]["session_manager"] = "unknown"
    
    # Add DI container health if available
    try:
        if "di_container" in app.extra:
            container_instance = app.extra["di_container"]
            if hasattr(container_instance, 'health_check'):
                container_health = container_instance.health_check()
                health_status["services"]["di_container"] = container_health["status"]
                health_status["container_components"] = container_health.get("components", {})
                
                # Add container initialization status for debugging
                health_status["container_initialized"] = getattr(container_instance, '_initialized', False)
                health_status["container_initializing"] = getattr(container_instance, '_initializing', False)
    except Exception as e:
        logger.warning(f"Failed to get DI container health: {e}")
        health_status["services"]["di_container"] = "unknown"
    
    return health_status


@app.get("/health/dependencies")  
async def health_check_dependencies():
    """Enhanced detailed health check for all dependencies with SLA metrics"""
    try:
        from .container import container
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker
        
        health = container.health_check()
        
        # Add detailed timing information
        import time
        start_time = time.time()
        
        # Test each service getter for performance
        service_tests = {}
        services = ['agent', 'data', 'knowledge', 'session', 'llm_provider', 'sanitizer', 'tracer']
        
        for service_name in services:
            service_start = time.time()
            try:
                service_method = getattr(container, f'get_{service_name}_service' if service_name in ['agent', 'data', 'knowledge', 'session'] else f'get_{service_name}')
                service_instance = service_method()
                service_tests[service_name] = {
                    "available": service_instance is not None,
                    "response_time_ms": round((time.time() - service_start) * 1000, 2)
                }
            except Exception as e:
                service_tests[service_name] = {
                    "available": False,
                    "error": str(e),
                    "response_time_ms": round((time.time() - service_start) * 1000, 2)
                }
        
        total_time_ms = round((time.time() - start_time) * 1000, 2)
        
        # Get enhanced component health data
        component_health_results = await component_monitor.check_all_components()
        dependency_map = component_monitor.get_dependency_map()
        critical_dependencies = component_monitor.get_critical_path_dependencies()
        
        # Get SLA details for each component
        sla_details = {}
        for component_name in component_health_results.keys():
            try:
                sla_details[component_name] = sla_tracker.get_component_sla_details(component_name)
            except Exception as e:
                logger.warning(f"Failed to get SLA details for {component_name}: {e}")
                sla_details[component_name] = {"error": str(e)}
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "container_health": health,
            "service_tests": service_tests,
            "component_health": {
                component_name: {
                    "status": health.status.value,
                    "response_time_ms": health.response_time_ms,
                    "sla_current": health.sla_current,
                    "last_error": health.last_error,
                    "dependencies": health.dependencies,
                    "metadata": health.metadata
                }
                for component_name, health in component_health_results.items()
            },
            "dependency_mapping": {
                "all_dependencies": dependency_map,
                "critical_dependencies": critical_dependencies
            },
            "sla_metrics": sla_details,
            "performance": {
                "total_response_time_ms": total_time_ms,
                "container_initialized": getattr(container, '_initialized', False),
                "container_initializing": getattr(container, '_initializing', False),
                "health_check_overhead_ms": round((time.time() - start_time) * 1000, 2)
            }
        }
    except Exception as e:
        logger.error(f"Enhanced dependency health check failed: {e}")
        return {
            "error": f"Enhanced dependency health check failed: {e}",
            "container_available": False,
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/health/logging")
async def logging_health_check():
    """Get logging system health status."""
    try:
        from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator
        
        coordinator = LoggingCoordinator()
        health_status = coordinator.get_health_status()
        
        # Add timestamp and additional metadata
        health_status["timestamp"] = datetime.utcnow().isoformat()
        health_status["service"] = "logging"
        
        return health_status
    except Exception as e:
        logger.error(f"Logging health check failed: {e}")
        return {
            "status": "error",
            "error": f"Logging health check failed: {e}",
            "timestamp": datetime.utcnow().isoformat(),
            "service": "logging"
        }


@app.get("/health/sla")
async def health_check_sla():
    """Get SLA status and metrics for all components."""
    try:
        from .infrastructure.health.sla_tracker import sla_tracker
        
        sla_summary = sla_tracker.get_sla_summary()
        
        # Get detailed SLA information for each component
        detailed_sla = {}
        for component_name in sla_tracker.component_thresholds.keys():
            try:
                detailed_sla[component_name] = sla_tracker.get_component_sla_details(component_name)
            except Exception as e:
                logger.warning(f"Failed to get SLA details for {component_name}: {e}")
                detailed_sla[component_name] = {"error": str(e)}
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "summary": sla_summary,
            "components": detailed_sla
        }
        
    except Exception as e:
        logger.error(f"SLA health check failed: {e}")
        return {
            "error": f"SLA health check failed: {e}",
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/health/components/{component_name}")
async def health_check_component(component_name: str):
    """Get detailed health information for a specific component."""
    try:
        from .infrastructure.health.component_monitor import component_monitor
        from .infrastructure.health.sla_tracker import sla_tracker
        
        # Get component health
        component_health = await component_monitor.check_component_health(component_name)
        
        # Get component metrics
        component_metrics = component_monitor.get_component_metrics(component_name)
        
        # Get SLA details
        try:
            sla_details = sla_tracker.get_component_sla_details(component_name)
        except Exception as e:
            logger.warning(f"Failed to get SLA details for {component_name}: {e}")
            sla_details = {"error": str(e)}
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "component_name": component_name,
            "health": {
                "status": component_health.status.value,
                "response_time_ms": component_health.response_time_ms,
                "last_error": component_health.last_error,
                "uptime_seconds": component_health.uptime_seconds,
                "sla_current": component_health.sla_current,
                "dependencies": component_health.dependencies,
                "metadata": component_health.metadata
            },
            "metrics": component_metrics,
            "sla": sla_details
        }
        
    except Exception as e:
        logger.error(f"Component health check failed for {component_name}: {e}")
        return {
            "error": f"Component health check failed: {e}",
            "component_name": component_name,
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/health/patterns")
async def health_check_error_patterns():
    """Get error patterns and recovery information from enhanced error context."""
    try:
        from .infrastructure.logging.coordinator import LoggingCoordinator
        
        coordinator = LoggingCoordinator()
        context = coordinator.get_context()
        
        if context and context.error_context:
            error_context = context.error_context
            
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "escalation_level": error_context.escalation_level.value,
                "detected_patterns": error_context.get_pattern_summary(),
                "recovery_summary": error_context.get_recovery_summary(),
                "layer_errors": {
                    layer: {
                        "error_count": info.get("error_count", 0),
                        "severity_score": info.get("severity_score", 0.0),
                        "last_error_time": info.get("last_error_time")
                    }
                    for layer, info in error_context.layer_errors.items()
                }
            }
        else:
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "message": "No active error context",
                "patterns": [],
                "recovery_attempts": []
            }
            
    except Exception as e:
        logger.error(f"Error patterns health check failed: {e}")
        return {
            "error": f"Error patterns health check failed: {e}",
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/metrics/performance")
async def get_performance_metrics():
    """Get comprehensive performance metrics."""
    try:
        from .api.middleware.performance import PerformanceMetricsEndpoint
        from .infrastructure.monitoring.metrics_collector import metrics_collector
        from .infrastructure.monitoring.apm_integration import apm_integration
        from .infrastructure.monitoring.alerting import alert_manager
        
        # Find the performance middleware instance
        performance_middleware = None
        for middleware in app.user_middleware:
            if hasattr(middleware, 'cls') and middleware.cls.__name__ == 'PerformanceTrackingMiddleware':
                performance_middleware = middleware
                break
        
        if performance_middleware:
            metrics_endpoint = PerformanceMetricsEndpoint(performance_middleware)
            return await metrics_endpoint.get_performance_metrics()
        else:
            # Return basic metrics if middleware not found
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "error": "Performance middleware not found",
                "metrics_collector": metrics_collector.get_metrics_summary(),
                "apm_integration": apm_integration.get_export_statistics(),
                "alerting": alert_manager.get_alert_statistics()
            }
            
    except Exception as e:
        logger.error(f"Performance metrics endpoint failed: {e}")
        return {
            "error": f"Performance metrics failed: {e}",
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/metrics/realtime")
async def get_realtime_metrics(time_window_minutes: int = 5):
    """Get real-time performance metrics."""
    try:
        from .infrastructure.monitoring.metrics_collector import metrics_collector
        from .infrastructure.monitoring.alerting import alert_manager
        
        # Validate time window
        if time_window_minutes < 1 or time_window_minutes > 60:
            time_window_minutes = 5
        
        dashboard_data = metrics_collector.get_dashboard_data(time_window_minutes)
        active_alerts = alert_manager.get_active_alerts()
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "time_window_minutes": time_window_minutes,
            "dashboard": dashboard_data,
            "active_alerts": [
                {
                    "rule_name": alert.rule_name,
                    "severity": alert.severity.value,
                    "metric_name": alert.metric_name,
                    "metric_value": alert.metric_value,
                    "threshold_value": alert.threshold_value,
                    "triggered_at": alert.triggered_at.isoformat(),
                    "message": alert.message
                }
                for alert in active_alerts[:10]  # Last 10 alerts
            ]
        }
        
    except Exception as e:
        logger.error(f"Real-time metrics endpoint failed: {e}")
        return {
            "error": f"Real-time metrics failed: {e}",
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/metrics/alerts")
async def get_alert_status():
    """Get current alert status and statistics."""
    try:
        from .infrastructure.monitoring.alerting import alert_manager
        
        active_alerts = alert_manager.get_active_alerts()
        alert_stats = alert_manager.get_alert_statistics()
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "statistics": alert_stats,
            "active_alerts": [
                {
                    "alert_id": alert.alert_id,
                    "rule_name": alert.rule_name,
                    "severity": alert.severity.value,
                    "status": alert.status.value,
                    "metric_name": alert.metric_name,
                    "metric_value": alert.metric_value,
                    "threshold_value": alert.threshold_value,
                    "triggered_at": alert.triggered_at.isoformat(),
                    "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
                    "message": alert.message,
                    "notification_count": alert.notification_count
                }
                for alert in active_alerts
            ]
        }
        
    except Exception as e:
        logger.error(f"Alert status endpoint failed: {e}")
        return {
            "error": f"Alert status failed: {e}",
            "timestamp": datetime.utcnow().isoformat()
        }


if __name__ == "__main__":
    import uvicorn
    
    # Configuration
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "false").lower() == "true"
    
    # Start server
    uvicorn.run(
        "faultmaven.main:app", host=host, port=port, reload=reload, log_level="info"
    )
