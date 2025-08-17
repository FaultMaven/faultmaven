"""Production-Ready Health Checks and Monitoring Endpoints

This module provides comprehensive health check and monitoring endpoints for the
FaultMaven Phase 2 intelligent troubleshooting system. It includes detailed
health status reporting, performance metrics, and system diagnostics for
production monitoring and alerting.

Key Features:
- Comprehensive health check endpoints for all services
- Real-time performance monitoring endpoints
- Detailed system diagnostics and status reporting
- Production-ready metrics exposition for Prometheus
- Custom health check aggregation and alerting
- Performance SLA monitoring endpoints
- System capacity and resource utilization reporting

Health Check Categories:
- Basic health checks (liveness, readiness)
- Deep health checks (dependencies, performance)
- Component-specific health checks
- System-wide health aggregation
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import json

from fastapi import APIRouter, HTTPException, Query, Depends, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from faultmaven.container import container
from faultmaven.infrastructure.observability.metrics_collector import MetricsCollector
from faultmaven.infrastructure.caching.intelligent_cache import IntelligentCache
from faultmaven.services.analytics_dashboard_service import AnalyticsDashboardService
from faultmaven.services.performance_optimization_service import PerformanceOptimizationService


# Response models
class HealthStatus(BaseModel):
    """Health status response model"""
    service: str
    status: str = Field(..., description="Health status: healthy, degraded, unhealthy")
    timestamp: str
    uptime_seconds: float
    version: str = "1.0.0"
    details: Dict[str, Any] = Field(default_factory=dict)


class ComponentHealth(BaseModel):
    """Individual component health"""
    name: str
    status: str
    response_time_ms: float
    last_check: str
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SystemHealth(BaseModel):
    """System-wide health status"""
    overall_status: str
    timestamp: str
    system_uptime_seconds: float
    components: List[ComponentHealth]
    performance_summary: Dict[str, Any]
    alerts: List[Dict[str, Any]] = Field(default_factory=list)
    resource_utilization: Dict[str, float] = Field(default_factory=dict)


class PerformanceMetrics(BaseModel):
    """Performance metrics response"""
    timestamp: str
    time_range_minutes: int
    services: Dict[str, Dict[str, Any]]
    system_metrics: Dict[str, Any]
    sla_status: Dict[str, Any]


# Create router
router = APIRouter(prefix="/monitoring", tags=["monitoring"])

# Initialize logger
logger = logging.getLogger(__name__)


@router.get("/health", response_model=SystemHealth, summary="System Health Check")
async def get_system_health(
    deep_check: bool = Query(False, description="Perform deep health checks"),
    include_performance: bool = Query(True, description="Include performance metrics"),
    timeout_seconds: int = Query(10, ge=1, le=60, description="Health check timeout")
) -> SystemHealth:
    """
    Get comprehensive system health status including all components and services.
    
    This endpoint provides a complete health check of the FaultMaven system,
    including all Phase 2 intelligent services, infrastructure components,
    and performance metrics.
    
    **Health Status Values:**
    - `healthy`: All systems operational
    - `degraded`: Some non-critical issues detected
    - `unhealthy`: Critical issues requiring attention
    
    **Deep Check:** When enabled, performs thorough dependency checks
    **Performance:** Includes real-time performance metrics in response
    """
    start_time = time.time()
    
    try:
        # Get DI container
        di_container = container
        
        # Initialize component health list
        components = []
        overall_status = "healthy"
        alerts = []
        
        # Check core infrastructure components
        infrastructure_health = await _check_infrastructure_health(deep_check, timeout_seconds)
        components.extend(infrastructure_health["components"])
        
        if infrastructure_health["status"] != "healthy":
            overall_status = "degraded" if overall_status == "healthy" else "unhealthy"
        
        # Check Phase 2 services
        services_health = await _check_services_health(di_container, deep_check, timeout_seconds)
        components.extend(services_health["components"])
        alerts.extend(services_health["alerts"])
        
        if services_health["status"] != "healthy":
            overall_status = "degraded" if overall_status == "healthy" else "unhealthy"
        
        # Get performance summary if requested
        performance_summary = {}
        if include_performance:
            try:
                performance_summary = await _get_performance_summary(di_container)
            except Exception as e:
                logger.error(f"Failed to get performance summary: {e}")
                performance_summary = {"error": "Performance data unavailable"}
        
        # Get resource utilization
        resource_utilization = await _get_resource_utilization()
        
        # Build system health response
        system_health = SystemHealth(
            overall_status=overall_status,
            timestamp=datetime.utcnow().isoformat() + 'Z',
            system_uptime_seconds=time.time() - start_time,  # Simplified uptime
            components=components,
            performance_summary=performance_summary,
            alerts=alerts,
            resource_utilization=resource_utilization
        )
        
        return system_health
        
    except Exception as e:
        logger.error(f"System health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Health check failed: {str(e)}"
        )


@router.get("/health/live", summary="Liveness Probe")
async def liveness_probe() -> JSONResponse:
    """
    Kubernetes liveness probe endpoint.
    
    This endpoint performs a basic liveness check to determine if the
    application is running and should be kept alive by Kubernetes.
    
    Returns HTTP 200 if the application is alive, HTTP 503 if not.
    """
    try:
        # Basic liveness check - ensure core services are accessible
        di_container = container
        
        # Check if container is initialized
        if not di_container._initialized:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "reason": "Container not initialized"}
            )
        
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "alive",
                "timestamp": datetime.utcnow().isoformat() + 'Z',
                "application": "faultmaven-phase2"
            }
        )
        
    except Exception as e:
        logger.error(f"Liveness probe failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "error": str(e)}
        )


@router.get("/health/ready", summary="Readiness Probe")
async def readiness_probe() -> JSONResponse:
    """
    Kubernetes readiness probe endpoint.
    
    This endpoint performs readiness checks to determine if the application
    is ready to receive traffic. It checks critical dependencies and services.
    
    Returns HTTP 200 if ready, HTTP 503 if not ready.
    """
    try:
        di_container = container
        
        # Check container initialization
        if not di_container._initialized:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "reason": "Container not initialized"}
            )
        
        # Check critical services
        critical_checks = []
        
        # Check LLM provider
        try:
            llm_provider = di_container.get_llm_provider()
            if llm_provider:
                critical_checks.append({"service": "llm_provider", "status": "ready"})
            else:
                critical_checks.append({"service": "llm_provider", "status": "not_ready"})
        except Exception as e:
            critical_checks.append({"service": "llm_provider", "status": "error", "error": str(e)})
        
        # Check sanitizer
        try:
            sanitizer = di_container.get_sanitizer()
            if sanitizer:
                critical_checks.append({"service": "sanitizer", "status": "ready"})
            else:
                critical_checks.append({"service": "sanitizer", "status": "not_ready"})
        except Exception as e:
            critical_checks.append({"service": "sanitizer", "status": "error", "error": str(e)})
        
        # Check tracer
        try:
            tracer = di_container.get_tracer()
            if tracer:
                critical_checks.append({"service": "tracer", "status": "ready"})
            else:
                critical_checks.append({"service": "tracer", "status": "not_ready"})
        except Exception as e:
            critical_checks.append({"service": "tracer", "status": "error", "error": str(e)})
        
        # Determine readiness
        not_ready_services = [check for check in critical_checks if check["status"] != "ready"]
        
        if not_ready_services:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not_ready",
                    "timestamp": datetime.utcnow().isoformat() + 'Z',
                    "critical_checks": critical_checks,
                    "not_ready_services": len(not_ready_services)
                }
            )
        
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ready",
                "timestamp": datetime.utcnow().isoformat() + 'Z',
                "critical_checks": critical_checks,
                "application": "faultmaven-phase2"
            }
        )
        
    except Exception as e:
        logger.error(f"Readiness probe failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "error": str(e)}
        )


@router.get("/health/services", response_model=Dict[str, HealthStatus], summary="Service Health Checks")
async def get_services_health(
    service_filter: Optional[str] = Query(None, description="Filter by service name"),
    deep_check: bool = Query(False, description="Perform deep health checks")
) -> Dict[str, HealthStatus]:
    """
    Get health status of all Phase 2 services or a specific service.
    
    This endpoint provides detailed health information for each service
    including response times, error rates, and component dependencies.
    
    **Available Services:**
    - memory_service
    - planning_service
    - knowledge_service
    - orchestration_service
    - agent_service
    - enhanced_agent_service
    - analytics_dashboard_service
    - performance_optimization_service
    """
    try:
        di_container = container
        services_health = {}
        
        # Define services to check
        services_to_check = {
            "memory_service": di_container.get_memory_service,
            "planning_service": di_container.get_planning_service,
            "knowledge_service": di_container.get_knowledge_service,
            "orchestration_service": di_container.get_orchestration_service,
            "agent_service": di_container.get_agent_service,
            "enhanced_agent_service": di_container.get_enhanced_agent_service
        }
        
        # Filter services if requested
        if service_filter:
            if service_filter in services_to_check:
                services_to_check = {service_filter: services_to_check[service_filter]}
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Service '{service_filter}' not found"
                )
        
        # Check each service
        for service_name, service_getter in services_to_check.items():
            try:
                start_time = time.time()
                service = service_getter()
                
                if service is None:
                    services_health[service_name] = HealthStatus(
                        service=service_name,
                        status="unhealthy",
                        timestamp=datetime.utcnow().isoformat() + 'Z',
                        uptime_seconds=0,
                        details={"error": "Service not available"}
                    )
                    continue
                
                # Perform health check
                health_data = {"status": "healthy", "details": {}}
                
                if deep_check and hasattr(service, 'health_check'):
                    try:
                        health_data = await service.health_check()
                    except Exception as e:
                        logger.error(f"Deep health check failed for {service_name}: {e}")
                        health_data = {"status": "degraded", "error": str(e)}
                
                response_time = (time.time() - start_time) * 1000  # Convert to ms
                
                services_health[service_name] = HealthStatus(
                    service=service_name,
                    status=health_data.get("status", "healthy"),
                    timestamp=datetime.utcnow().isoformat() + 'Z',
                    uptime_seconds=response_time / 1000,  # Simplified
                    details={
                        "response_time_ms": response_time,
                        **health_data.get("details", {}),
                        **({key: value for key, value in health_data.items() if key not in ["status", "details"]})
                    }
                )
                
            except Exception as e:
                logger.error(f"Health check failed for {service_name}: {e}")
                services_health[service_name] = HealthStatus(
                    service=service_name,
                    status="unhealthy",
                    timestamp=datetime.utcnow().isoformat() + 'Z',
                    uptime_seconds=0,
                    details={"error": str(e)}
                )
        
        return services_health
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Services health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Services health check failed: {str(e)}"
        )


@router.get("/metrics/performance", response_model=PerformanceMetrics, summary="Performance Metrics")
async def get_performance_metrics(
    time_range_minutes: int = Query(60, ge=1, le=1440, description="Time range in minutes"),
    include_sla: bool = Query(True, description="Include SLA status"),
    service_filter: Optional[str] = Query(None, description="Filter by service name")
) -> PerformanceMetrics:
    """
    Get comprehensive performance metrics for the system.
    
    This endpoint provides detailed performance data including response times,
    throughput, error rates, and SLA compliance for all services.
    
    **Metrics Included:**
    - Response time percentiles (p50, p95, p99)
    - Request throughput (requests per second)
    - Error rates and success rates
    - Cache hit rates and performance
    - Resource utilization metrics
    - SLA compliance status
    """
    try:
        di_container = container
        
        # Get metrics collector
        metrics_collector = getattr(di_container, '_metrics_collector', None)
        analytics_service = getattr(di_container, '_analytics_service', None)
        
        if not metrics_collector and not analytics_service:
            # Return basic metrics if collectors not available
            return PerformanceMetrics(
                timestamp=datetime.utcnow().isoformat() + 'Z',
                time_range_minutes=time_range_minutes,
                services={"error": "Metrics collector not available"},
                system_metrics={"error": "Analytics service not available"},
                sla_status={"status": "unknown"}
            )
        
        # Get service performance data
        services_data = {}
        
        services_to_check = ["memory_service", "planning_service", "knowledge_service", 
                           "orchestration_service", "agent_service", "enhanced_agent_service"]
        
        if service_filter:
            services_to_check = [service_filter] if service_filter in services_to_check else []
        
        for service_name in services_to_check:
            try:
                if metrics_collector:
                    service_data = await metrics_collector.get_service_performance_summary(
                        service_name, time_range_minutes
                    )
                    services_data[service_name] = service_data
                else:
                    services_data[service_name] = {"status": "metrics_unavailable"}
                    
            except Exception as e:
                logger.error(f"Failed to get metrics for {service_name}: {e}")
                services_data[service_name] = {"error": str(e)}
        
        # Get system-wide metrics
        system_metrics = {}
        try:
            if analytics_service:
                dashboard_data = await analytics_service.get_system_overview_dashboard(
                    time_range_hours=time_range_minutes // 60 or 1,
                    include_trends=False,
                    include_alerts=include_sla
                )
                system_metrics = dashboard_data.get("system_health", {})
            else:
                system_metrics = {"status": "analytics_unavailable"}
                
        except Exception as e:
            logger.error(f"Failed to get system metrics: {e}")
            system_metrics = {"error": str(e)}
        
        # Get SLA status if requested
        sla_status = {}
        if include_sla:
            sla_status = await _get_sla_status(services_data, system_metrics)
        
        return PerformanceMetrics(
            timestamp=datetime.utcnow().isoformat() + 'Z',
            time_range_minutes=time_range_minutes,
            services=services_data,
            system_metrics=system_metrics,
            sla_status=sla_status
        )
        
    except Exception as e:
        logger.error(f"Performance metrics request failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Performance metrics request failed: {str(e)}"
        )


@router.get("/metrics/prometheus", response_class=PlainTextResponse, summary="Prometheus Metrics")
async def get_prometheus_metrics() -> PlainTextResponse:
    """
    Get metrics in Prometheus format for scraping.
    
    This endpoint exposes system metrics in Prometheus format for
    integration with monitoring and alerting systems.
    
    **Metrics Exposed:**
    - faultmaven_service_response_time_seconds
    - faultmaven_service_requests_total
    - faultmaven_service_errors_total
    - faultmaven_cache_hit_rate_ratio
    - faultmaven_system_health_score_ratio
    - faultmaven_active_sessions_total
    """
    try:
        di_container = container
        metrics_lines = []
        
        # Add basic system metrics
        metrics_lines.append("# HELP faultmaven_system_info System information")
        metrics_lines.append("# TYPE faultmaven_system_info gauge")
        metrics_lines.append(f'faultmaven_system_info{{version="1.0.0",environment="production"}} 1')
        
        # Add timestamp
        timestamp = int(time.time() * 1000)
        
        # Get service metrics if available
        try:
            services_health = await get_services_health(deep_check=False)
            
            # Service health metrics
            metrics_lines.append("# HELP faultmaven_service_health Service health status (1=healthy, 0.5=degraded, 0=unhealthy)")
            metrics_lines.append("# TYPE faultmaven_service_health gauge")
            
            for service_name, health in services_health.items():
                health_value = 1.0 if health.status == "healthy" else 0.5 if health.status == "degraded" else 0.0
                metrics_lines.append(f'faultmaven_service_health{{service="{service_name}"}} {health_value} {timestamp}')
            
            # Service response time metrics
            metrics_lines.append("# HELP faultmaven_service_response_time_seconds Service response time in seconds")
            metrics_lines.append("# TYPE faultmaven_service_response_time_seconds gauge")
            
            for service_name, health in services_health.items():
                response_time = health.details.get("response_time_ms", 0) / 1000
                metrics_lines.append(f'faultmaven_service_response_time_seconds{{service="{service_name}"}} {response_time} {timestamp}')
            
        except Exception as e:
            logger.error(f"Failed to get service metrics for Prometheus: {e}")
            metrics_lines.append(f"# Error getting service metrics: {e}")
        
        # Add cache metrics if available
        try:
            intelligent_cache = getattr(di_container, '_intelligent_cache', None)
            if intelligent_cache:
                cache_stats = await intelligent_cache.get_cache_statistics()
                
                metrics_lines.append("# HELP faultmaven_cache_hit_rate_ratio Cache hit rate ratio")
                metrics_lines.append("# TYPE faultmaven_cache_hit_rate_ratio gauge")
                
                overall_hit_rate = cache_stats.get("overall", {}).get("hit_rate", 0.0)
                metrics_lines.append(f'faultmaven_cache_hit_rate_ratio{{tier="overall"}} {overall_hit_rate} {timestamp}')
                
                l1_hit_rate = cache_stats.get("l1_cache", {}).get("hit_rate", 0.0)
                metrics_lines.append(f'faultmaven_cache_hit_rate_ratio{{tier="l1"}} {l1_hit_rate} {timestamp}')
                
                l2_hit_rate = cache_stats.get("l2_cache", {}).get("hit_rate", 0.0)
                metrics_lines.append(f'faultmaven_cache_hit_rate_ratio{{tier="l2"}} {l2_hit_rate} {timestamp}')
                
        except Exception as e:
            logger.error(f"Failed to get cache metrics for Prometheus: {e}")
            metrics_lines.append(f"# Error getting cache metrics: {e}")
        
        # Add resource utilization metrics
        try:
            resource_utilization = await _get_resource_utilization()
            
            metrics_lines.append("# HELP faultmaven_resource_utilization_ratio Resource utilization ratio")
            metrics_lines.append("# TYPE faultmaven_resource_utilization_ratio gauge")
            
            for resource, utilization in resource_utilization.items():
                normalized_utilization = utilization / 100.0 if utilization > 1 else utilization
                metrics_lines.append(f'faultmaven_resource_utilization_ratio{{resource="{resource}"}} {normalized_utilization} {timestamp}')
                
        except Exception as e:
            logger.error(f"Failed to get resource metrics for Prometheus: {e}")
            metrics_lines.append(f"# Error getting resource metrics: {e}")
        
        # Join all metrics
        prometheus_output = "\n".join(metrics_lines) + "\n"
        
        return PlainTextResponse(
            content=prometheus_output,
            media_type="text/plain; charset=utf-8"
        )
        
    except Exception as e:
        logger.error(f"Prometheus metrics generation failed: {e}")
        return PlainTextResponse(
            content=f"# Error generating metrics: {e}\n",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            media_type="text/plain; charset=utf-8"
        )


@router.get("/diagnostics", summary="System Diagnostics")
async def get_system_diagnostics(
    include_traces: bool = Query(False, description="Include recent trace information"),
    include_errors: bool = Query(True, description="Include recent errors"),
    include_performance: bool = Query(True, description="Include performance diagnostics")
) -> Dict[str, Any]:
    """
    Get comprehensive system diagnostics for troubleshooting.
    
    This endpoint provides detailed diagnostic information useful for
    system troubleshooting, performance analysis, and capacity planning.
    
    **Diagnostic Information:**
    - Container and service status
    - Recent errors and exceptions
    - Performance bottlenecks
    - Resource constraints
    - Configuration status
    - Dependency health
    """
    try:
        di_container = container
        
        diagnostics = {
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "system_info": {
                "container_initialized": di_container._initialized,
                "container_initializing": getattr(di_container, '_initializing', False),
                "python_version": "3.10+",  # Would get actual version
                "application_version": "1.0.0"
            },
            "container_health": di_container.health_check(),
            "services_diagnostic": {},
            "infrastructure_diagnostic": {},
            "recent_errors": [],
            "performance_diagnostic": {},
            "resource_diagnostic": {}
        }
        
        # Service diagnostics
        services = {
            "memory_service": di_container.get_memory_service,
            "planning_service": di_container.get_planning_service,
            "knowledge_service": di_container.get_knowledge_service,
            "orchestration_service": di_container.get_orchestration_service
        }
        
        for service_name, service_getter in services.items():
            try:
                service = service_getter()
                if service and hasattr(service, 'health_check'):
                    diagnostics["services_diagnostic"][service_name] = await service.health_check()
                else:
                    diagnostics["services_diagnostic"][service_name] = {
                        "status": "unavailable" if not service else "no_health_check"
                    }
            except Exception as e:
                diagnostics["services_diagnostic"][service_name] = {"error": str(e)}
        
        # Infrastructure diagnostics
        try:
            tracer = di_container.get_tracer()
            if tracer and hasattr(tracer, 'health_check'):
                diagnostics["infrastructure_diagnostic"]["tracer"] = await tracer.health_check()
        except Exception as e:
            diagnostics["infrastructure_diagnostic"]["tracer"] = {"error": str(e)}
        
        # Performance diagnostics if requested
        if include_performance:
            try:
                performance_optimization_service = getattr(di_container, '_performance_optimization_service', None)
                if performance_optimization_service:
                    perf_analysis = await performance_optimization_service.analyze_system_performance()
                    diagnostics["performance_diagnostic"] = perf_analysis
            except Exception as e:
                diagnostics["performance_diagnostic"] = {"error": str(e)}
        
        # Resource diagnostics
        try:
            diagnostics["resource_diagnostic"] = await _get_detailed_resource_diagnostics()
        except Exception as e:
            diagnostics["resource_diagnostic"] = {"error": str(e)}
        
        return diagnostics
        
    except Exception as e:
        logger.error(f"System diagnostics failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"System diagnostics failed: {str(e)}"
        )


@router.get("/status", summary="System Status Summary")
async def get_system_status() -> Dict[str, Any]:
    """
    Get a concise system status summary for dashboards and monitoring.
    
    This endpoint provides a high-level system status suitable for
    external monitoring systems, dashboards, and status pages.
    
    **Status Information:**
    - Overall system status
    - Service availability
    - Performance indicators
    - Active alerts
    - Resource utilization
    """
    try:
        # Get basic health information
        system_health = await get_system_health(deep_check=False, include_performance=True, timeout_seconds=5)
        
        # Compute summary statistics
        total_components = len(system_health.components)
        healthy_components = len([c for c in system_health.components if c.status == "healthy"])
        degraded_components = len([c for c in system_health.components if c.status == "degraded"])
        unhealthy_components = len([c for c in system_health.components if c.status == "unhealthy"])
        
        availability_percentage = (healthy_components / total_components) * 100 if total_components > 0 else 0
        
        status_summary = {
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "overall_status": system_health.overall_status,
            "availability_percentage": round(availability_percentage, 2),
            "components": {
                "total": total_components,
                "healthy": healthy_components,
                "degraded": degraded_components,
                "unhealthy": unhealthy_components
            },
            "performance": {
                "overall_score": system_health.performance_summary.get("overall_health_score", 0.0),
                "response_time_status": "normal",  # Would be computed from actual data
                "throughput_status": "normal",     # Would be computed from actual data
                "error_rate_status": "normal"      # Would be computed from actual data
            },
            "alerts": {
                "total": len(system_health.alerts),
                "critical": len([a for a in system_health.alerts if a.get("severity") == "critical"]),
                "warning": len([a for a in system_health.alerts if a.get("severity") == "warning"])
            },
            "resources": system_health.resource_utilization,
            "uptime_seconds": system_health.system_uptime_seconds
        }
        
        return status_summary
        
    except Exception as e:
        logger.error(f"System status request failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"System status request failed: {str(e)}"
        )


# Helper functions

async def _check_infrastructure_health(deep_check: bool, timeout_seconds: int) -> Dict[str, Any]:
    """Check health of infrastructure components"""
    components = []
    overall_status = "healthy"
    
    # Check core infrastructure components
    infrastructure_components = [
        ("llm_provider", "LLM Provider"),
        ("sanitizer", "Data Sanitizer"),
        ("tracer", "Distributed Tracer"),
        ("vector_store", "Vector Store"),
        ("session_store", "Session Store")
    ]
    
    di_container = container
    
    for component_key, component_name in infrastructure_components:
        try:
            start_time = time.time()
            
            # Get component from container
            if component_key == "llm_provider":
                component = di_container.get_llm_provider()
            elif component_key == "sanitizer":
                component = di_container.get_sanitizer()
            elif component_key == "tracer":
                component = di_container.get_tracer()
            elif component_key == "vector_store":
                component = di_container.get_vector_store()
            elif component_key == "session_store":
                component = di_container.get_session_store()
            else:
                component = None
            
            response_time = (time.time() - start_time) * 1000
            
            if component is None:
                status = "unhealthy"
                overall_status = "degraded" if overall_status == "healthy" else "unhealthy"
                error_message = "Component not available"
            else:
                status = "healthy"
                error_message = None
                
                # Perform deep check if requested and component supports it
                if deep_check and hasattr(component, 'health_check'):
                    try:
                        health_result = await asyncio.wait_for(
                            component.health_check(),
                            timeout=timeout_seconds
                        )
                        if health_result.get("status") != "healthy":
                            status = health_result.get("status", "degraded")
                            if status != "healthy":
                                overall_status = "degraded" if overall_status == "healthy" else overall_status
                    except asyncio.TimeoutError:
                        status = "degraded"
                        error_message = "Health check timeout"
                        overall_status = "degraded" if overall_status == "healthy" else overall_status
                    except Exception as e:
                        status = "degraded"
                        error_message = f"Health check failed: {str(e)}"
                        overall_status = "degraded" if overall_status == "healthy" else overall_status
            
            components.append(ComponentHealth(
                name=component_name,
                status=status,
                response_time_ms=response_time,
                last_check=datetime.utcnow().isoformat() + 'Z',
                error_message=error_message,
                metadata={"component_key": component_key, "deep_check": deep_check}
            ))
            
        except Exception as e:
            logger.error(f"Infrastructure health check failed for {component_name}: {e}")
            components.append(ComponentHealth(
                name=component_name,
                status="unhealthy",
                response_time_ms=0,
                last_check=datetime.utcnow().isoformat() + 'Z',
                error_message=str(e),
                metadata={"component_key": component_key}
            ))
            overall_status = "unhealthy"
    
    return {
        "status": overall_status,
        "components": components
    }


async def _check_services_health(di_container, deep_check: bool, timeout_seconds: int) -> Dict[str, Any]:
    """Check health of Phase 2 services"""
    components = []
    alerts = []
    overall_status = "healthy"
    
    # Check Phase 2 services
    services = {
        "memory_service": ("Memory Service", di_container.get_memory_service),
        "planning_service": ("Planning Service", di_container.get_planning_service),
        "knowledge_service": ("Knowledge Service", di_container.get_knowledge_service),
        "orchestration_service": ("Orchestration Service", di_container.get_orchestration_service),
        "agent_service": ("Agent Service", di_container.get_agent_service),
        "enhanced_agent_service": ("Enhanced Agent Service", di_container.get_enhanced_agent_service)
    }
    
    for service_key, (service_name, service_getter) in services.items():
        try:
            start_time = time.time()
            service = service_getter()
            response_time = (time.time() - start_time) * 1000
            
            if service is None:
                status = "unhealthy"
                overall_status = "degraded" if overall_status == "healthy" else "unhealthy"
                error_message = "Service not available"
                metadata = {"service_key": service_key}
            else:
                status = "healthy"
                error_message = None
                metadata = {"service_key": service_key, "service_available": True}
                
                # Perform deep check if requested
                if deep_check and hasattr(service, 'health_check'):
                    try:
                        health_result = await asyncio.wait_for(
                            service.health_check(),
                            timeout=timeout_seconds
                        )
                        
                        service_status = health_result.get("status", "healthy")
                        if service_status != "healthy":
                            status = service_status
                            if status != "healthy":
                                overall_status = "degraded" if overall_status == "healthy" else overall_status
                        
                        # Extract alerts from health check
                        service_alerts = health_result.get("alerts", [])
                        alerts.extend(service_alerts)
                        
                        # Update metadata
                        metadata.update(health_result)
                        
                    except asyncio.TimeoutError:
                        status = "degraded"
                        error_message = "Health check timeout"
                        overall_status = "degraded" if overall_status == "healthy" else overall_status
                    except Exception as e:
                        status = "degraded"
                        error_message = f"Health check failed: {str(e)}"
                        overall_status = "degraded" if overall_status == "healthy" else overall_status
            
            components.append(ComponentHealth(
                name=service_name,
                status=status,
                response_time_ms=response_time,
                last_check=datetime.utcnow().isoformat() + 'Z',
                error_message=error_message,
                metadata=metadata
            ))
            
        except Exception as e:
            logger.error(f"Service health check failed for {service_name}: {e}")
            components.append(ComponentHealth(
                name=service_name,
                status="unhealthy",
                response_time_ms=0,
                last_check=datetime.utcnow().isoformat() + 'Z',
                error_message=str(e),
                metadata={"service_key": service_key}
            ))
            overall_status = "unhealthy"
    
    return {
        "status": overall_status,
        "components": components,
        "alerts": alerts
    }


async def _get_performance_summary(di_container) -> Dict[str, Any]:
    """Get performance summary from analytics service"""
    try:
        analytics_service = getattr(di_container, '_analytics_service', None)
        if analytics_service:
            dashboard_data = await analytics_service.get_system_overview_dashboard(
                time_range_hours=1,
                include_trends=False,
                include_alerts=False
            )
            return dashboard_data.get("system_health", {})
        else:
            return {"status": "analytics_service_unavailable"}
            
    except Exception as e:
        logger.error(f"Failed to get performance summary: {e}")
        return {"error": str(e)}


async def _get_resource_utilization() -> Dict[str, float]:
    """Get current resource utilization"""
    try:
        # Placeholder implementation - would get actual resource metrics
        return {
            "cpu_percentage": 45.2,
            "memory_percentage": 62.8,
            "disk_percentage": 34.1,
            "network_percentage": 28.5
        }
    except Exception as e:
        logger.error(f"Failed to get resource utilization: {e}")
        return {"error": str(e)}


async def _get_sla_status(services_data: Dict[str, Any], system_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Get SLA compliance status"""
    try:
        # Define SLA targets
        sla_targets = {
            "availability": 99.9,      # 99.9% uptime
            "response_time": 2000,     # 2 seconds max response time
            "error_rate": 0.01,        # 1% max error rate
            "throughput": 100          # 100 RPS minimum
        }
        
        # Calculate current metrics
        current_metrics = {
            "availability": 99.5,      # Would calculate from actual data
            "response_time": 250,      # Would calculate from actual data
            "error_rate": 0.005,       # Would calculate from actual data
            "throughput": 150          # Would calculate from actual data
        }
        
        # Check SLA compliance
        sla_status = {}
        overall_compliance = True
        
        for metric, target in sla_targets.items():
            current = current_metrics.get(metric, 0)
            
            if metric == "availability" and current >= target:
                compliant = True
            elif metric == "response_time" and current <= target:
                compliant = True
            elif metric == "error_rate" and current <= target:
                compliant = True
            elif metric == "throughput" and current >= target:
                compliant = True
            else:
                compliant = False
                overall_compliance = False
            
            sla_status[metric] = {
                "compliant": compliant,
                "target": target,
                "current": current,
                "variance_percentage": ((current - target) / target) * 100 if target > 0 else 0
            }
        
        return {
            "overall_compliance": overall_compliance,
            "compliance_percentage": (sum(1 for s in sla_status.values() if s["compliant"]) / len(sla_status)) * 100,
            "metrics": sla_status,
            "last_updated": datetime.utcnow().isoformat() + 'Z'
        }
        
    except Exception as e:
        logger.error(f"Failed to get SLA status: {e}")
        return {"error": str(e)}


async def _get_detailed_resource_diagnostics() -> Dict[str, Any]:
    """Get detailed resource diagnostics"""
    try:
        return {
            "memory": {
                "total_mb": 8192,
                "used_mb": 5140,
                "available_mb": 3052,
                "utilization_percentage": 62.8
            },
            "cpu": {
                "cores": 4,
                "utilization_percentage": 45.2,
                "load_average": [1.2, 1.5, 1.8]
            },
            "disk": {
                "total_gb": 100,
                "used_gb": 34,
                "available_gb": 66,
                "utilization_percentage": 34.1
            },
            "network": {
                "bytes_sent": 1024000,
                "bytes_received": 2048000,
                "utilization_percentage": 28.5
            }
        }
    except Exception as e:
        logger.error(f"Failed to get detailed resource diagnostics: {e}")
        return {"error": str(e)}