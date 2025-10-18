"""Performance Monitoring and Optimization Startup Initialization

This module handles the startup initialization of all performance monitoring
and optimization services for the FaultMaven Phase 2 system. It ensures
proper service startup order, background task initialization, and graceful
error handling during system startup.

Key Responsibilities:
- Initialize all performance monitoring services
- Start background processing tasks
- Handle service interdependencies
- Provide graceful degradation on startup errors
- Monitor startup health and timing
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from faultmaven.container import container
from faultmaven.infrastructure.observability.metrics_collector import MetricsCollector
from faultmaven.infrastructure.caching.intelligent_cache import IntelligentCache
from faultmaven.services.analytics.dashboard_service import AnalyticsDashboardService
from faultmaven.services.performance_optimization import PerformanceOptimizationService
from faultmaven.infrastructure.monitoring.sla_monitor import SLAMonitor


class PerformanceMonitoringStartup:
    """Handles startup initialization of performance monitoring system"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.startup_metrics = {
            "startup_time": None,
            "services_initialized": 0,
            "services_started": 0,
            "startup_errors": [],
            "background_tasks_started": 0
        }
    
    async def initialize_performance_monitoring(self) -> Dict[str, Any]:
        """Initialize all performance monitoring and optimization services
        
        Returns:
            Initialization status and metrics
        """
        start_time = datetime.now(timezone.utc)
        self.logger.info("Starting performance monitoring system initialization")
        
        try:
            # Get DI container
            di_container = container

            # Ensure container is initialized
            if not di_container._initialized:
                await di_container.initialize()
                await asyncio.sleep(1)  # Give container time to initialize
            
            # Initialize services in dependency order
            initialization_status = {
                "overall_status": "success",
                "started_at": start_time.isoformat(),
                "services": {}
            }
            
            # 1. Start Metrics Collector background processing
            await self._start_metrics_collector(di_container, initialization_status)
            
            # 2. Start Intelligent Cache background processing
            await self._start_intelligent_cache(di_container, initialization_status)
            
            # 3. Start Analytics Dashboard Service background processing
            await self._start_analytics_dashboard_service(di_container, initialization_status)
            
            # 4. Start Performance Optimization Service background processing
            await self._start_performance_optimization_service(di_container, initialization_status)
            
            # 5. Start SLA Monitor
            await self._start_sla_monitor(di_container, initialization_status)
            
            # Calculate final metrics
            end_time = datetime.now(timezone.utc)
            self.startup_metrics["startup_time"] = (end_time - start_time).total_seconds()
            
            # Determine overall status
            if self.startup_metrics["startup_errors"]:
                if self.startup_metrics["services_started"] == 0:
                    initialization_status["overall_status"] = "failed"
                else:
                    initialization_status["overall_status"] = "degraded"
            
            initialization_status.update({
                "completed_at": end_time.isoformat(),
                "startup_duration_seconds": self.startup_metrics["startup_time"],
                "services_initialized": self.startup_metrics["services_initialized"],
                "services_started": self.startup_metrics["services_started"],
                "background_tasks_started": self.startup_metrics["background_tasks_started"],
                "startup_errors": self.startup_metrics["startup_errors"]
            })
            
            self.logger.info(
                f"Performance monitoring initialization completed: {initialization_status['overall_status']} "
                f"({self.startup_metrics['services_started']}/{self.startup_metrics['services_initialized']} services started)"
            )
            
            return initialization_status
            
        except Exception as e:
            self.logger.error(f"Critical error during performance monitoring initialization: {e}")
            return {
                "overall_status": "failed",
                "error": str(e),
                "startup_metrics": self.startup_metrics
            }
    
    async def _start_metrics_collector(self, di_container, status: Dict[str, Any]):
        """Start metrics collector background processing"""
        try:
            self.logger.info("Initializing Metrics Collector...")
            metrics_collector = di_container.get_metrics_collector()
            
            if metrics_collector:
                await metrics_collector.start_background_processing()
                status["services"]["metrics_collector"] = {
                    "status": "started",
                    "background_tasks": True,
                    "startup_time": datetime.now(timezone.utc).isoformat()
                }
                self.startup_metrics["services_initialized"] += 1
                self.startup_metrics["services_started"] += 1
                self.startup_metrics["background_tasks_started"] += 1
                
                self.logger.info("✅ Metrics Collector initialized and background processing started")
            else:
                status["services"]["metrics_collector"] = {
                    "status": "not_available",
                    "error": "Service not created by container"
                }
                self.startup_metrics["startup_errors"].append("Metrics collector not available")
                self.logger.warning("❌ Metrics Collector not available")
            
        except Exception as e:
            self.logger.error(f"Failed to start Metrics Collector: {e}")
            status["services"]["metrics_collector"] = {
                "status": "failed",
                "error": str(e)
            }
            self.startup_metrics["startup_errors"].append(f"Metrics Collector: {str(e)}")
    
    async def _start_intelligent_cache(self, di_container, status: Dict[str, Any]):
        """Start intelligent cache background processing"""
        try:
            self.logger.info("Initializing Intelligent Cache...")
            intelligent_cache = di_container.get_intelligent_cache()
            
            if intelligent_cache:
                await intelligent_cache.start_background_processing()
                status["services"]["intelligent_cache"] = {
                    "status": "started",
                    "background_tasks": True,
                    "startup_time": datetime.now(timezone.utc).isoformat()
                }
                self.startup_metrics["services_initialized"] += 1
                self.startup_metrics["services_started"] += 1
                self.startup_metrics["background_tasks_started"] += 1
                
                self.logger.info("✅ Intelligent Cache initialized and background processing started")
            else:
                status["services"]["intelligent_cache"] = {
                    "status": "not_available",
                    "error": "Service not created by container"
                }
                self.startup_metrics["startup_errors"].append("Intelligent cache not available")
                self.logger.warning("❌ Intelligent Cache not available")
            
        except Exception as e:
            self.logger.error(f"Failed to start Intelligent Cache: {e}")
            status["services"]["intelligent_cache"] = {
                "status": "failed",
                "error": str(e)
            }
            self.startup_metrics["startup_errors"].append(f"Intelligent Cache: {str(e)}")
    
    async def _start_analytics_dashboard_service(self, di_container, status: Dict[str, Any]):
        """Start analytics dashboard service background processing"""
        try:
            self.logger.info("Initializing Analytics Dashboard Service...")
            analytics_service = di_container.get_analytics_dashboard_service()
            
            if analytics_service:
                await analytics_service.start_background_processing()
                status["services"]["analytics_dashboard_service"] = {
                    "status": "started",
                    "background_tasks": True,
                    "startup_time": datetime.now(timezone.utc).isoformat()
                }
                self.startup_metrics["services_initialized"] += 1
                self.startup_metrics["services_started"] += 1
                self.startup_metrics["background_tasks_started"] += 1
                
                self.logger.info("✅ Analytics Dashboard Service initialized and background processing started")
            else:
                status["services"]["analytics_dashboard_service"] = {
                    "status": "not_available",
                    "error": "Service not created by container"
                }
                self.startup_metrics["startup_errors"].append("Analytics dashboard service not available")
                self.logger.warning("❌ Analytics Dashboard Service not available")
            
        except Exception as e:
            self.logger.error(f"Failed to start Analytics Dashboard Service: {e}")
            status["services"]["analytics_dashboard_service"] = {
                "status": "failed",
                "error": str(e)
            }
            self.startup_metrics["startup_errors"].append(f"Analytics Dashboard Service: {str(e)}")
    
    async def _start_performance_optimization_service(self, di_container, status: Dict[str, Any]):
        """Start performance optimization service background processing"""
        try:
            self.logger.info("Initializing Performance Optimization Service...")
            optimization_service = di_container.get_performance_optimization_service()
            
            if optimization_service:
                await optimization_service.start_background_processing()
                status["services"]["performance_optimization_service"] = {
                    "status": "started",
                    "background_tasks": True,
                    "auto_optimization_enabled": optimization_service._enable_auto_optimization,
                    "aggressiveness": optimization_service._optimization_aggressiveness,
                    "startup_time": datetime.now(timezone.utc).isoformat()
                }
                self.startup_metrics["services_initialized"] += 1
                self.startup_metrics["services_started"] += 1
                self.startup_metrics["background_tasks_started"] += 1
                
                self.logger.info("✅ Performance Optimization Service initialized and background processing started")
            else:
                status["services"]["performance_optimization_service"] = {
                    "status": "not_available",
                    "error": "Service not created by container"
                }
                self.startup_metrics["startup_errors"].append("Performance optimization service not available")
                self.logger.warning("❌ Performance Optimization Service not available")
            
        except Exception as e:
            self.logger.error(f"Failed to start Performance Optimization Service: {e}")
            status["services"]["performance_optimization_service"] = {
                "status": "failed",
                "error": str(e)
            }
            self.startup_metrics["startup_errors"].append(f"Performance Optimization Service: {str(e)}")
    
    async def _start_sla_monitor(self, di_container, status: Dict[str, Any]):
        """Start SLA monitor"""
        try:
            self.logger.info("Initializing SLA Monitor...")
            sla_monitor = di_container.get_sla_monitor()
            
            if sla_monitor:
                await sla_monitor.start_monitoring()
                status["services"]["sla_monitor"] = {
                    "status": "started",
                    "background_tasks": True,
                    "total_slas": len(sla_monitor._sla_definitions),
                    "alert_channels": len(sla_monitor._alert_channels),
                    "startup_time": datetime.now(timezone.utc).isoformat()
                }
                self.startup_metrics["services_initialized"] += 1
                self.startup_metrics["services_started"] += 1
                self.startup_metrics["background_tasks_started"] += 1
                
                self.logger.info("✅ SLA Monitor initialized and monitoring started")
            else:
                status["services"]["sla_monitor"] = {
                    "status": "not_available",
                    "error": "Service not created by container"
                }
                self.startup_metrics["startup_errors"].append("SLA monitor not available")
                self.logger.warning("❌ SLA Monitor not available")
            
        except Exception as e:
            self.logger.error(f"Failed to start SLA Monitor: {e}")
            status["services"]["sla_monitor"] = {
                "status": "failed",
                "error": str(e)
            }
            self.startup_metrics["startup_errors"].append(f"SLA Monitor: {str(e)}")
    
    async def shutdown_performance_monitoring(self) -> Dict[str, Any]:
        """Gracefully shutdown all performance monitoring services
        
        Returns:
            Shutdown status and metrics
        """
        start_time = datetime.now(timezone.utc)
        self.logger.info("Starting performance monitoring system shutdown")
        
        shutdown_status = {
            "overall_status": "success",
            "started_at": start_time.isoformat(),
            "services": {}
        }
        
        try:
            di_container = container
            
            # Shutdown services in reverse order
            await self._shutdown_sla_monitor(di_container, shutdown_status)
            await self._shutdown_performance_optimization_service(di_container, shutdown_status)
            await self._shutdown_analytics_dashboard_service(di_container, shutdown_status)
            await self._shutdown_intelligent_cache(di_container, shutdown_status)
            await self._shutdown_metrics_collector(di_container, shutdown_status)
            
            end_time = datetime.now(timezone.utc)
            shutdown_status.update({
                "completed_at": end_time.isoformat(),
                "shutdown_duration_seconds": (end_time - start_time).total_seconds()
            })
            
            self.logger.info("Performance monitoring system shutdown completed")
            return shutdown_status
            
        except Exception as e:
            self.logger.error(f"Error during performance monitoring shutdown: {e}")
            shutdown_status["overall_status"] = "error"
            shutdown_status["error"] = str(e)
            return shutdown_status
    
    async def _shutdown_sla_monitor(self, di_container, status: Dict[str, Any]):
        """Shutdown SLA monitor"""
        try:
            sla_monitor = di_container.get_sla_monitor()
            if sla_monitor:
                await sla_monitor.stop_monitoring()
                status["services"]["sla_monitor"] = {"status": "stopped"}
                self.logger.info("✅ SLA Monitor stopped")
        except Exception as e:
            self.logger.error(f"Error stopping SLA Monitor: {e}")
            status["services"]["sla_monitor"] = {"status": "error", "error": str(e)}
    
    async def _shutdown_performance_optimization_service(self, di_container, status: Dict[str, Any]):
        """Shutdown performance optimization service"""
        try:
            optimization_service = di_container.get_performance_optimization_service()
            if optimization_service:
                await optimization_service.stop_background_processing()
                status["services"]["performance_optimization_service"] = {"status": "stopped"}
                self.logger.info("✅ Performance Optimization Service stopped")
        except Exception as e:
            self.logger.error(f"Error stopping Performance Optimization Service: {e}")
            status["services"]["performance_optimization_service"] = {"status": "error", "error": str(e)}
    
    async def _shutdown_analytics_dashboard_service(self, di_container, status: Dict[str, Any]):
        """Shutdown analytics dashboard service"""
        try:
            analytics_service = di_container.get_analytics_dashboard_service()
            if analytics_service:
                await analytics_service.stop_background_processing()
                status["services"]["analytics_dashboard_service"] = {"status": "stopped"}
                self.logger.info("✅ Analytics Dashboard Service stopped")
        except Exception as e:
            self.logger.error(f"Error stopping Analytics Dashboard Service: {e}")
            status["services"]["analytics_dashboard_service"] = {"status": "error", "error": str(e)}
    
    async def _shutdown_intelligent_cache(self, di_container, status: Dict[str, Any]):
        """Shutdown intelligent cache"""
        try:
            intelligent_cache = di_container.get_intelligent_cache()
            if intelligent_cache:
                await intelligent_cache.stop_background_processing()
                status["services"]["intelligent_cache"] = {"status": "stopped"}
                self.logger.info("✅ Intelligent Cache stopped")
        except Exception as e:
            self.logger.error(f"Error stopping Intelligent Cache: {e}")
            status["services"]["intelligent_cache"] = {"status": "error", "error": str(e)}
    
    async def _shutdown_metrics_collector(self, di_container, status: Dict[str, Any]):
        """Shutdown metrics collector"""
        try:
            metrics_collector = di_container.get_metrics_collector()
            if metrics_collector:
                await metrics_collector.stop_background_processing()
                status["services"]["metrics_collector"] = {"status": "stopped"}
                self.logger.info("✅ Metrics Collector stopped")
        except Exception as e:
            self.logger.error(f"Error stopping Metrics Collector: {e}")
            status["services"]["metrics_collector"] = {"status": "error", "error": str(e)}


# Global startup instance
_startup_instance: Optional[PerformanceMonitoringStartup] = None


async def initialize_performance_monitoring_system() -> Dict[str, Any]:
    """Initialize the performance monitoring system
    
    Returns:
        Initialization status and metrics
    """
    global _startup_instance
    
    if _startup_instance is None:
        _startup_instance = PerformanceMonitoringStartup()
    
    return await _startup_instance.initialize_performance_monitoring()


async def shutdown_performance_monitoring_system() -> Dict[str, Any]:
    """Shutdown the performance monitoring system
    
    Returns:
        Shutdown status and metrics
    """
    global _startup_instance
    
    if _startup_instance is not None:
        return await _startup_instance.shutdown_performance_monitoring()
    
    return {"overall_status": "no_services_running"}


def get_startup_metrics() -> Dict[str, Any]:
    """Get current startup metrics
    
    Returns:
        Current startup metrics
    """
    global _startup_instance
    
    if _startup_instance is not None:
        return _startup_instance.startup_metrics.copy()
    
    return {"status": "not_initialized"}


# Integration with existing FastAPI application startup
async def on_startup():
    """FastAPI startup event handler for performance monitoring"""
    logger = logging.getLogger(__name__)
    logger.info("Starting FaultMaven Performance Monitoring System...")
    
    try:
        initialization_status = await initialize_performance_monitoring_system()
        
        if initialization_status["overall_status"] == "success":
            logger.info("🚀 Performance Monitoring System started successfully")
        elif initialization_status["overall_status"] == "degraded":
            logger.warning("⚠️ Performance Monitoring System started with some issues")
        else:
            logger.error("❌ Performance Monitoring System failed to start")
        
        return initialization_status
        
    except Exception as e:
        logger.error(f"Critical error starting Performance Monitoring System: {e}")
        raise


async def on_shutdown():
    """FastAPI shutdown event handler for performance monitoring"""
    logger = logging.getLogger(__name__)
    logger.info("Shutting down FaultMaven Performance Monitoring System...")
    
    try:
        shutdown_status = await shutdown_performance_monitoring_system()
        
        if shutdown_status["overall_status"] == "success":
            logger.info("✅ Performance Monitoring System shut down gracefully")
        else:
            logger.warning("⚠️ Performance Monitoring System shutdown completed with issues")
        
        return shutdown_status
        
    except Exception as e:
        logger.error(f"Error during Performance Monitoring System shutdown: {e}")
        # Don't raise exception during shutdown to avoid blocking other cleanup