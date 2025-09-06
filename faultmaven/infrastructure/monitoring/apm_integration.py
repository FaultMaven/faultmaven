"""
APM Integration

Provides integration with Application Performance Monitoring tools
and standardized metrics export capabilities.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
from enum import Enum
import logging
import json
import asyncio
import time


class APMProvider(Enum):
    """Supported APM providers."""
    OPIK = "opik"
    PROMETHEUS = "prometheus"
    GRAFANA = "grafana"
    DATADOG = "datadog"
    NEW_RELIC = "new_relic"
    GENERIC = "generic"


@dataclass
class APMMetrics:
    """Standardized metrics format for APM integration."""
    timestamp: datetime
    service_name: str
    operation_name: str
    duration_ms: float
    status: str  # "success", "error", "timeout"
    tags: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None


@dataclass
class APMConfiguration:
    """Configuration for APM integration."""
    provider: APMProvider
    enabled: bool = True
    endpoint_url: Optional[str] = None
    api_key: Optional[str] = None
    project_name: Optional[str] = None
    batch_size: int = 100
    flush_interval_seconds: int = 30
    custom_headers: Dict[str, str] = field(default_factory=dict)
    custom_tags: Dict[str, str] = field(default_factory=dict)


class APMIntegration:
    """Manages APM integrations and metrics export."""
    
    def __init__(self, service_name: str = "faultmaven", settings=None):
        """Initialize APM integration.
        
        Args:
            service_name: Name of the service for APM identification
            settings: FaultMavenSettings instance for configuration
        """
        self.logger = logging.getLogger(__name__)
        self.service_name = service_name
        
        # Get settings if not provided
        if settings is None:
            try:
                from faultmaven.config.settings import get_settings
                settings = get_settings()
            except:
                settings = None
        
        self.settings = settings
        self.configurations: Dict[APMProvider, APMConfiguration] = {}
        self.metrics_buffer: List[APMMetrics] = []
        self.export_callbacks: Dict[APMProvider, Callable] = {}
        self.last_flush_time = datetime.utcnow()
        self.is_running = False
        self.flush_task: Optional[asyncio.Task] = None
        
        self._initialize_default_configurations()
        self._register_default_exporters()
    
    def _initialize_default_configurations(self) -> None:
        """Initialize default configurations for supported APM providers."""
        # Use fallback values if settings are not available
        fallback_environment = "development"
        fallback_instance_id = "localhost:8000"
        
        if self.settings:
            # Use settings-based configuration
            # Opik configuration (FaultMaven's primary observability tool)
            opik_config = APMConfiguration(
                provider=APMProvider.OPIK,
                enabled=self.settings.observability.opik_enabled,
                endpoint_url=self.settings.observability.opik_url_override or "http://localhost:3003",
                api_key=(self.settings.observability.opik_api_key.get_secret_value() 
                        if self.settings.observability.opik_api_key else None),
                project_name=self.settings.observability.opik_project_name,
                custom_headers={
                    "User-Agent": "FaultMaven-APM-Integration/1.0"
                },
                custom_tags={
                    "service": self.service_name,
                    "version": "1.0.0",
                    "environment": self.settings.server.environment
                }
            )
            
            # Prometheus configuration
            prometheus_config = APMConfiguration(
                provider=APMProvider.PROMETHEUS,
                enabled=self.settings.observability.prometheus_enabled,
                endpoint_url=self.settings.observability.prometheus_pushgateway_url,
                custom_tags={
                    "job": self.service_name,
                    "instance": self.settings.observability.instance_id
                }
            )
            
            # Generic HTTP endpoint configuration
            generic_config = APMConfiguration(
                provider=APMProvider.GENERIC,
                enabled=self.settings.observability.generic_apm_enabled,
                endpoint_url=self.settings.observability.generic_apm_url,
                api_key=(self.settings.observability.generic_apm_api_key.get_secret_value() 
                        if self.settings.observability.generic_apm_api_key else None),
                custom_headers={
                    "Content-Type": "application/json"
                }
            )
        else:
            # Fallback configuration when settings are unavailable
            opik_config = APMConfiguration(
                provider=APMProvider.OPIK,
                enabled=True,
                endpoint_url="http://localhost:3003",
                api_key=None,
                project_name="faultmaven",
                custom_headers={
                    "User-Agent": "FaultMaven-APM-Integration/1.0"
                },
                custom_tags={
                    "service": self.service_name,
                    "version": "1.0.0",
                    "environment": fallback_environment
                }
            )
            
            prometheus_config = APMConfiguration(
                provider=APMProvider.PROMETHEUS,
                enabled=False,
                endpoint_url="http://localhost:9091",
                custom_tags={
                    "job": self.service_name,
                    "instance": fallback_instance_id
                }
            )
            
            generic_config = APMConfiguration(
                provider=APMProvider.GENERIC,
                enabled=False,
                endpoint_url=None,
                api_key=None,
                custom_headers={
                    "Content-Type": "application/json"
                }
            )
        
        self.configurations[APMProvider.OPIK] = opik_config
        self.configurations[APMProvider.PROMETHEUS] = prometheus_config
        self.configurations[APMProvider.GENERIC] = generic_config
    
    def _register_default_exporters(self) -> None:
        """Register default metric exporters for each provider."""
        self.export_callbacks[APMProvider.OPIK] = self._export_to_opik
        self.export_callbacks[APMProvider.PROMETHEUS] = self._export_to_prometheus
        self.export_callbacks[APMProvider.GENERIC] = self._export_to_generic
    
    def configure_provider(
        self,
        provider: APMProvider,
        configuration: APMConfiguration
    ) -> None:
        """Configure a specific APM provider.
        
        Args:
            provider: APM provider to configure
            configuration: Configuration for the provider
        """
        self.configurations[provider] = configuration
        self.logger.info(f"Configured APM provider: {provider.value}")
    
    def start_background_export(self) -> None:
        """Start background task for periodic metrics export."""
        if self.is_running:
            self.logger.warning("Background export already running")
            return
        
        self.is_running = True
        self.flush_task = asyncio.create_task(self._background_flush_loop())
        self.logger.info("Started background APM metrics export")
    
    def stop_background_export(self) -> None:
        """Stop background metrics export."""
        self.is_running = False
        if self.flush_task:
            self.flush_task.cancel()
        self.logger.info("Stopped background APM metrics export")
    
    async def _background_flush_loop(self) -> None:
        """Background loop for flushing metrics."""
        while self.is_running:
            try:
                await self.flush_metrics()
                
                # Wait for flush interval
                await asyncio.sleep(30)  # Default 30 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in background flush loop: {e}")
                await asyncio.sleep(5)  # Brief pause before retry
    
    def record_operation(
        self,
        operation_name: str,
        duration_ms: float,
        status: str = "success",
        tags: Optional[Dict[str, str]] = None,
        attributes: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None
    ) -> None:
        """Record an operation for APM export.
        
        Args:
            operation_name: Name of the operation
            duration_ms: Duration in milliseconds
            status: Operation status
            tags: Additional tags
            attributes: Additional attributes
            trace_id: Distributed trace ID
            span_id: Span ID
            parent_span_id: Parent span ID
        """
        metric = APMMetrics(
            timestamp=datetime.utcnow(),
            service_name=self.service_name,
            operation_name=operation_name,
            duration_ms=duration_ms,
            status=status,
            tags=tags or {},
            attributes=attributes or {},
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id
        )
        
        self.metrics_buffer.append(metric)
        
        # Auto-flush if buffer is full
        if len(self.metrics_buffer) >= 1000:  # Emergency flush threshold
            asyncio.create_task(self.flush_metrics())
    
    async def flush_metrics(self) -> None:
        """Flush buffered metrics to all configured APM providers."""
        if not self.metrics_buffer:
            return
        
        metrics_to_export = self.metrics_buffer.copy()
        self.metrics_buffer.clear()
        
        self.logger.debug(f"Flushing {len(metrics_to_export)} metrics to APM providers")
        
        # Export to each configured provider
        for provider, config in self.configurations.items():
            if not config.enabled:
                continue
            
            if provider in self.export_callbacks:
                try:
                    await self.export_callbacks[provider](metrics_to_export, config)
                except Exception as e:
                    self.logger.error(f"Failed to export to {provider.value}: {e}")
        
        self.last_flush_time = datetime.utcnow()
    
    async def _export_to_opik(
        self,
        metrics: List[APMMetrics],
        config: APMConfiguration
    ) -> None:
        """Export metrics to Opik."""
        try:
            # Try to use existing Opik integration if available
            from ...observability.tracing import OpikTracer
            
            # Convert metrics to Opik format
            for metric in metrics:
                try:
                    # Create Opik span data
                    span_data = {
                        "name": metric.operation_name,
                        "start_time": metric.timestamp.isoformat(),
                        "end_time": (metric.timestamp + timedelta(milliseconds=metric.duration_ms)).isoformat(),
                        "status": metric.status,
                        "tags": {**config.custom_tags, **metric.tags},
                        "metadata": metric.attributes
                    }
                    
                    if metric.trace_id:
                        span_data["trace_id"] = metric.trace_id
                    if metric.span_id:
                        span_data["span_id"] = metric.span_id
                    if metric.parent_span_id:
                        span_data["parent_span_id"] = metric.parent_span_id
                    
                    # This would integrate with the actual Opik client
                    self.logger.debug(f"Exported to Opik: {metric.operation_name}")
                    
                except Exception as e:
                    self.logger.warning(f"Failed to export metric to Opik: {e}")
        
        except ImportError:
            self.logger.warning("Opik integration not available")
    
    async def _export_to_prometheus(
        self,
        metrics: List[APMMetrics],
        config: APMConfiguration
    ) -> None:
        """Export metrics to Prometheus pushgateway."""
        try:
            import aiohttp
            
            # Convert metrics to Prometheus format
            prometheus_data = self._convert_to_prometheus_format(metrics, config)
            
            if not prometheus_data:
                return
            
            # Push to Prometheus pushgateway
            async with aiohttp.ClientSession() as session:
                url = f"{config.endpoint_url}/metrics/job/{config.custom_tags.get('job', self.service_name)}"
                
                async with session.post(
                    url,
                    data=prometheus_data,
                    headers={"Content-Type": "text/plain"}
                ) as response:
                    if response.status == 200:
                        self.logger.debug(f"Exported {len(metrics)} metrics to Prometheus")
                    else:
                        self.logger.warning(f"Prometheus export failed: {response.status}")
        
        except Exception as e:
            self.logger.error(f"Prometheus export error: {e}")
    
    def _convert_to_prometheus_format(
        self,
        metrics: List[APMMetrics],
        config: APMConfiguration
    ) -> str:
        """Convert metrics to Prometheus format."""
        lines = []
        
        # Group metrics by operation
        operation_durations = {}
        operation_counts = {}
        
        for metric in metrics:
            operation = metric.operation_name.replace('-', '_').replace('.', '_')
            
            if operation not in operation_durations:
                operation_durations[operation] = []
                operation_counts[operation] = 0
            
            operation_durations[operation].append(metric.duration_ms)
            operation_counts[operation] += 1
        
        # Generate Prometheus metrics
        for operation, durations in operation_durations.items():
            # Duration histogram
            lines.append(f"# HELP faultmaven_{operation}_duration_ms Operation duration in milliseconds")
            lines.append(f"# TYPE faultmaven_{operation}_duration_ms histogram")
            
            # Request count
            lines.append(f"# HELP faultmaven_{operation}_total Total number of operations")
            lines.append(f"# TYPE faultmaven_{operation}_total counter")
            lines.append(f"faultmaven_{operation}_total {operation_counts[operation]}")
            
            # Average duration
            avg_duration = sum(durations) / len(durations)
            lines.append(f"# HELP faultmaven_{operation}_avg_duration_ms Average operation duration")
            lines.append(f"# TYPE faultmaven_{operation}_avg_duration_ms gauge")
            lines.append(f"faultmaven_{operation}_avg_duration_ms {avg_duration}")
        
        return '\n'.join(lines)
    
    async def _export_to_generic(
        self,
        metrics: List[APMMetrics],
        config: APMConfiguration
    ) -> None:
        """Export metrics to generic HTTP endpoint."""
        if not config.endpoint_url:
            return
        
        try:
            import aiohttp
            
            # Convert metrics to JSON format
            json_data = {
                "service": self.service_name,
                "timestamp": datetime.utcnow().isoformat(),
                "metrics": [
                    {
                        "timestamp": metric.timestamp.isoformat(),
                        "operation": metric.operation_name,
                        "duration_ms": metric.duration_ms,
                        "status": metric.status,
                        "tags": {**config.custom_tags, **metric.tags},
                        "attributes": metric.attributes
                    }
                    for metric in metrics
                ]
            }
            
            headers = config.custom_headers.copy()
            if config.api_key:
                headers["Authorization"] = f"Bearer {config.api_key}"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    config.endpoint_url,
                    json=json_data,
                    headers=headers
                ) as response:
                    if response.status in [200, 202]:
                        self.logger.debug(f"Exported {len(metrics)} metrics to generic endpoint")
                    else:
                        self.logger.warning(f"Generic export failed: {response.status}")
        
        except Exception as e:
            self.logger.error(f"Generic export error: {e}")
    
    def get_export_statistics(self) -> Dict[str, Any]:
        """Get statistics about metrics export.
        
        Returns:
            Export statistics
        """
        enabled_providers = [
            provider.value for provider, config in self.configurations.items()
            if config.enabled
        ]
        
        return {
            "service_name": self.service_name,
            "enabled_providers": enabled_providers,
            "buffered_metrics": len(self.metrics_buffer),
            "last_flush_time": self.last_flush_time.isoformat() if self.last_flush_time else None,
            "is_running": self.is_running,
            "configurations": {
                provider.value: {
                    "enabled": config.enabled,
                    "endpoint_url": config.endpoint_url,
                    "batch_size": config.batch_size,
                    "flush_interval_seconds": config.flush_interval_seconds
                }
                for provider, config in self.configurations.items()
            }
        }
    
    def add_custom_exporter(
        self,
        provider: APMProvider,
        exporter_func: Callable[[List[APMMetrics], APMConfiguration], None]
    ) -> None:
        """Add custom metrics exporter.
        
        Args:
            provider: APM provider identifier
            exporter_func: Async function to export metrics
        """
        self.export_callbacks[provider] = exporter_func
        self.logger.info(f"Added custom exporter for {provider.value}")


# Global APM integration instance - will use default settings
apm_integration = APMIntegration()