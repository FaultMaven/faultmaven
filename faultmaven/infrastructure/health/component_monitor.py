"""
Component Health Monitoring

Provides detailed health monitoring for individual components with
SLA tracking and dependency relationship mapping.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone, timedelta
from enum import Enum
import logging
import asyncio
import time


class HealthStatus(Enum):
    """Component health status levels."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Represents the health status of a single component."""
    component_name: str
    status: HealthStatus
    response_time_ms: float
    last_error: Optional[str] = None
    uptime_seconds: float = 0.0
    sla_current: float = 100.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    last_check: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_count_24h: int = 0
    success_count_24h: int = 0


@dataclass
class DependencyMapping:
    """Maps component dependencies and criticality."""
    component: str
    critical_dependencies: List[str] = field(default_factory=list)
    optional_dependencies: List[str] = field(default_factory=list)
    dependent_components: List[str] = field(default_factory=list)


class ComponentHealthMonitor:
    """Monitors health of individual components with SLA tracking."""
    
    def __init__(self):
        """Initialize component health monitor."""
        self.logger = logging.getLogger(__name__)
        self.component_health: Dict[str, ComponentHealth] = {}
        self.dependency_map: Dict[str, DependencyMapping] = {}
        self.health_history: Dict[str, List[Tuple[datetime, HealthStatus, float]]] = {}
        self.sla_thresholds: Dict[str, Dict[str, float]] = {}
        self._initialize_default_components()
    
    def _initialize_default_components(self) -> None:
        """Initialize monitoring for default FaultMaven components."""
        default_components = {
            "database": {
                "dependencies": [],
                "sla_thresholds": {"response_time_ms": 100, "availability": 99.9},
                "critical": True
            },
            "llm_provider": {
                "dependencies": ["database"],
                "sla_thresholds": {"response_time_ms": 2000, "availability": 99.5},
                "critical": True
            },
            "knowledge_base": {
                "dependencies": ["database", "vector_store"],
                "sla_thresholds": {"response_time_ms": 500, "availability": 99.0},
                "critical": False
            },
            "session_store": {
                "dependencies": ["redis"],
                "sla_thresholds": {"response_time_ms": 50, "availability": 99.9},
                "critical": True
            },
            "vector_store": {
                "dependencies": [],
                "sla_thresholds": {"response_time_ms": 300, "availability": 99.0},
                "critical": False
            },
            "redis": {
                "dependencies": [],
                "sla_thresholds": {"response_time_ms": 10, "availability": 99.9},
                "critical": True
            },
            "sanitizer": {
                "dependencies": [],
                "sla_thresholds": {"response_time_ms": 100, "availability": 99.5},
                "critical": True
            },
            "tracer": {
                "dependencies": [],
                "sla_thresholds": {"response_time_ms": 50, "availability": 95.0},
                "critical": False
            }
        }
        
        for component, config in default_components.items():
            self.register_component(
                component,
                dependencies=config["dependencies"],
                sla_thresholds=config["sla_thresholds"]
            )
    
    def register_component(
        self,
        component_name: str,
        dependencies: Optional[List[str]] = None,
        sla_thresholds: Optional[Dict[str, float]] = None
    ) -> None:
        """Register a component for health monitoring.
        
        Args:
            component_name: Name of the component to monitor
            dependencies: List of components this component depends on
            sla_thresholds: SLA thresholds for this component
        """
        # Initialize component health
        self.component_health[component_name] = ComponentHealth(
            component_name=component_name,
            status=HealthStatus.UNKNOWN,
            response_time_ms=0.0,
            dependencies=dependencies or []
        )
        
        # Set up dependency mapping
        self.dependency_map[component_name] = DependencyMapping(
            component=component_name,
            critical_dependencies=dependencies or []
        )
        
        # Set SLA thresholds
        if sla_thresholds:
            self.sla_thresholds[component_name] = sla_thresholds
        
        # Initialize health history
        self.health_history[component_name] = []
        
        self.logger.info(f"Registered component for health monitoring: {component_name}")
    
    async def check_component_health(self, component_name: str) -> ComponentHealth:
        """Check health of a specific component.
        
        Args:
            component_name: Name of component to check
            
        Returns:
            Current health status of the component
        """
        if component_name not in self.component_health:
            self.logger.warning(f"Component not registered: {component_name}")
            return ComponentHealth(
                component_name=component_name,
                status=HealthStatus.UNKNOWN,
                response_time_ms=0.0
            )
        
        start_time = time.time()
        
        try:
            # Perform component-specific health check
            health_result = await self._perform_health_check(component_name)
            response_time = (time.time() - start_time) * 1000
            
            # Update component health
            component_health = self.component_health[component_name]
            component_health.status = health_result["status"]
            component_health.response_time_ms = response_time
            component_health.last_error = health_result.get("error")
            component_health.last_check = datetime.now(timezone.utc)
            component_health.metadata.update(health_result.get("metadata", {}))
            
            # Update success/error counts
            if health_result["status"] == HealthStatus.HEALTHY:
                component_health.success_count_24h += 1
            else:
                component_health.error_count_24h += 1
                if health_result.get("error"):
                    component_health.last_error = health_result["error"]
            
            # Calculate SLA
            component_health.sla_current = self._calculate_sla(component_name)
            
            # Record in history
            self._record_health_history(component_name, component_health.status, response_time)
            
            return component_health
            
        except Exception as e:
            self.logger.error(f"Health check failed for {component_name}: {e}")
            
            # Update with error status
            component_health = self.component_health[component_name]
            component_health.status = HealthStatus.UNHEALTHY
            component_health.response_time_ms = (time.time() - start_time) * 1000
            component_health.last_error = str(e)
            component_health.last_check = datetime.now(timezone.utc)
            component_health.error_count_24h += 1
            
            return component_health
    
    async def _perform_health_check(self, component_name: str) -> Dict[str, Any]:
        """Perform actual health check for a component.
        
        Args:
            component_name: Name of component to check
            
        Returns:
            Health check result with status and metadata
        """
        # Component-specific health check logic
        if component_name == "database":
            return await self._check_database_health()
        elif component_name == "llm_provider":
            return await self._check_llm_provider_health()
        elif component_name == "knowledge_base":
            return await self._check_knowledge_base_health()
        elif component_name == "session_store":
            return await self._check_session_store_health()
        elif component_name == "vector_store":
            return await self._check_vector_store_health()
        elif component_name == "redis":
            return await self._check_redis_health()
        elif component_name == "sanitizer":
            return await self._check_sanitizer_health()
        elif component_name == "tracer":
            return await self._check_tracer_health()
        else:
            return await self._generic_health_check(component_name)
    
    async def _check_database_health(self) -> Dict[str, Any]:
        """Check database health."""
        try:
            # In a real implementation, this would check actual database connectivity
            # For now, simulate a health check
            await asyncio.sleep(0.01)  # Simulate database query time
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "connection_pool_size": 10,
                    "active_connections": 3,
                    "query_cache_hit_rate": 0.95
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": str(e)
            }
    
    async def _check_llm_provider_health(self) -> Dict[str, Any]:
        """Check LLM provider health."""
        try:
            # Check if LLM providers are responsive
            await asyncio.sleep(0.05)  # Simulate LLM health check
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "active_providers": ["fireworks", "openai"],
                    "failed_providers": [],
                    "average_response_time": 1200,
                    "rate_limit_remaining": 95
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.DEGRADED,
                "error": str(e),
                "metadata": {
                    "active_providers": ["fireworks"],
                    "failed_providers": ["openai"],
                    "fallback_active": True
                }
            }
    
    async def _check_knowledge_base_health(self) -> Dict[str, Any]:
        """Check knowledge base health."""
        try:
            await asyncio.sleep(0.02)  # Simulate KB query
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "document_count": 1250,
                    "index_size_mb": 45.2,
                    "search_cache_hit_rate": 0.88
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": str(e)
            }
    
    async def _check_session_store_health(self) -> Dict[str, Any]:
        """Check session store health."""
        try:
            await asyncio.sleep(0.005)  # Simulate Redis operation
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "active_sessions": 150,
                    "memory_usage_mb": 12.5,
                    "hit_rate": 0.97
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": str(e)
            }
    
    async def _check_vector_store_health(self) -> Dict[str, Any]:
        """Check vector store health."""
        try:
            await asyncio.sleep(0.03)  # Simulate vector search
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "collection_count": 5,
                    "total_vectors": 15000,
                    "index_status": "ready"
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": str(e)
            }
    
    async def _check_redis_health(self) -> Dict[str, Any]:
        """Check Redis health."""
        try:
            await asyncio.sleep(0.001)  # Simulate Redis ping
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "memory_usage_mb": 25.1,
                    "connected_clients": 8,
                    "uptime_seconds": 86400
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": str(e)
            }
    
    async def _check_sanitizer_health(self) -> Dict[str, Any]:
        """Check data sanitizer health."""
        try:
            await asyncio.sleep(0.01)  # Simulate sanitization check
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "models_loaded": True,
                    "pii_detection_accuracy": 0.96
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.UNHEALTHY,
                "error": str(e)
            }
    
    async def _check_tracer_health(self) -> Dict[str, Any]:
        """Check tracer health."""
        try:
            await asyncio.sleep(0.005)  # Simulate trace operation
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "tracing_enabled": True,
                    "traces_sent_24h": 2500,
                    "export_failures": 0
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.DEGRADED,
                "error": str(e),
                "metadata": {
                    "tracing_enabled": False,
                    "fallback_mode": True
                }
            }
    
    async def _generic_health_check(self, component_name: str) -> Dict[str, Any]:
        """Generic health check for unknown components."""
        try:
            # Basic connectivity/availability check
            await asyncio.sleep(0.01)
            
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {
                    "type": "generic",
                    "last_check": datetime.now(timezone.utc).isoformat()
                }
            }
        except Exception as e:
            return {
                "status": HealthStatus.UNKNOWN,
                "error": str(e)
            }
    
    def _calculate_sla(self, component_name: str) -> float:
        """Calculate current SLA for a component based on recent history."""
        if component_name not in self.health_history:
            return 100.0
        
        # Calculate SLA based on last 24 hours
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_history = [
            (timestamp, status, response_time)
            for timestamp, status, response_time in self.health_history[component_name]
            if timestamp >= cutoff_time
        ]
        
        if not recent_history:
            return 100.0
        
        # Calculate uptime percentage
        healthy_checks = len([s for _, s, _ in recent_history if s == HealthStatus.HEALTHY])
        total_checks = len(recent_history)
        
        sla = (healthy_checks / total_checks) * 100 if total_checks > 0 else 100.0
        
        # Apply response time penalties if thresholds are configured
        if component_name in self.sla_thresholds:
            threshold = self.sla_thresholds[component_name].get("response_time_ms")
            if threshold:
                slow_responses = len([
                    rt for _, _, rt in recent_history if rt > threshold
                ])
                if slow_responses > 0:
                    penalty = (slow_responses / total_checks) * 5  # Up to 5% penalty
                    sla = max(0.0, sla - penalty)
        
        return round(sla, 2)
    
    def _record_health_history(self, component_name: str, status: HealthStatus, response_time: float) -> None:
        """Record health check result in history."""
        if component_name not in self.health_history:
            self.health_history[component_name] = []
        
        # Add new record
        self.health_history[component_name].append((datetime.now(timezone.utc), status, response_time))
        
        # Keep only last 24 hours of history
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
        self.health_history[component_name] = [
            record for record in self.health_history[component_name]
            if record[0] >= cutoff_time
        ]
    
    async def check_all_components(self) -> Dict[str, ComponentHealth]:
        """Check health of all registered components.
        
        Returns:
            Dictionary mapping component names to their health status
        """
        health_results = {}
        
        # Run health checks concurrently
        tasks = [
            self.check_component_health(component_name)
            for component_name in self.component_health.keys()
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for i, result in enumerate(results):
            component_name = list(self.component_health.keys())[i]
            if isinstance(result, Exception):
                self.logger.error(f"Health check failed for {component_name}: {result}")
                health_results[component_name] = ComponentHealth(
                    component_name=component_name,
                    status=HealthStatus.UNHEALTHY,
                    response_time_ms=0.0,
                    last_error=str(result)
                )
            else:
                health_results[component_name] = result
        
        return health_results
    
    def get_dependency_map(self) -> Dict[str, List[str]]:
        """Get dependency mapping for all components.
        
        Returns:
            Dictionary mapping components to their dependencies
        """
        return {
            component: mapping.critical_dependencies + mapping.optional_dependencies
            for component, mapping in self.dependency_map.items()
        }
    
    def get_critical_path_dependencies(self) -> Dict[str, List[str]]:
        """Get critical path dependencies for all components.
        
        Returns:
            Dictionary mapping components to their critical dependencies only
        """
        return {
            component: mapping.critical_dependencies
            for component, mapping in self.dependency_map.items()
        }
    
    def get_overall_health_status(self) -> Tuple[HealthStatus, Dict[str, Any]]:
        """Get overall system health status based on all components.
        
        Returns:
            Tuple of overall status and summary information
        """
        if not self.component_health:
            return HealthStatus.UNKNOWN, {"reason": "No components registered"}
        
        # Count components by status
        status_counts = {}
        critical_unhealthy = []
        
        for component_name, health in self.component_health.items():
            status = health.status
            status_counts[status.value] = status_counts.get(status.value, 0) + 1
            
            # Check if critical component is unhealthy
            dependencies = self.dependency_map.get(component_name)
            if dependencies and dependencies.critical_dependencies and status != HealthStatus.HEALTHY:
                critical_unhealthy.append(component_name)
        
        # Determine overall status
        if critical_unhealthy:
            overall_status = HealthStatus.UNHEALTHY
            reason = f"Critical components unhealthy: {', '.join(critical_unhealthy)}"
        elif status_counts.get("unhealthy", 0) > 0:
            overall_status = HealthStatus.DEGRADED
            reason = f"{status_counts['unhealthy']} components unhealthy"
        elif status_counts.get("degraded", 0) > 0:
            overall_status = HealthStatus.DEGRADED
            reason = f"{status_counts['degraded']} components degraded"
        else:
            overall_status = HealthStatus.HEALTHY
            reason = "All components healthy"
        
        # Calculate overall SLA
        sla_values = [
            health.sla_current for health in self.component_health.values()
            if health.sla_current > 0
        ]
        overall_sla = sum(sla_values) / len(sla_values) if sla_values else 100.0
        
        summary = {
            "reason": reason,
            "component_counts": status_counts,
            "overall_sla": round(overall_sla, 2),
            "critical_unhealthy": critical_unhealthy,
            "total_components": len(self.component_health)
        }
        
        return overall_status, summary
    
    def get_component_metrics(self, component_name: str) -> Dict[str, Any]:
        """Get detailed metrics for a specific component.
        
        Args:
            component_name: Name of component to get metrics for
            
        Returns:
            Dictionary with detailed component metrics
        """
        if component_name not in self.component_health:
            return {"error": f"Component {component_name} not found"}
        
        health = self.component_health[component_name]
        history = self.health_history.get(component_name, [])
        
        # Calculate metrics from history
        if history:
            response_times = [rt for _, _, rt in history]
            avg_response_time = sum(response_times) / len(response_times)
            max_response_time = max(response_times)
            min_response_time = min(response_times)
        else:
            avg_response_time = health.response_time_ms
            max_response_time = health.response_time_ms
            min_response_time = health.response_time_ms
        
        return {
            "component_name": component_name,
            "current_status": health.status.value,
            "current_response_time_ms": health.response_time_ms,
            "sla_current": health.sla_current,
            "last_error": health.last_error,
            "last_check": health.last_check.isoformat(),
            "dependencies": health.dependencies,
            "metadata": health.metadata,
            "metrics_24h": {
                "success_count": health.success_count_24h,
                "error_count": health.error_count_24h,
                "avg_response_time_ms": round(avg_response_time, 2),
                "max_response_time_ms": round(max_response_time, 2),
                "min_response_time_ms": round(min_response_time, 2),
                "total_checks": len(history)
            }
        }


# Global component health monitor instance
component_monitor = ComponentHealthMonitor()