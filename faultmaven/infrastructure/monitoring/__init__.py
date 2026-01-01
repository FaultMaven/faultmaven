"""Performance monitoring framework for FaultMaven applications."""

from .apm_integration import APMIntegration, APMMetrics
from .metrics_collector import MetricsCollector, PerformanceMetrics
from .alerting import AlertManager, AlertRule
from .prometheus_metrics import (
    PROMETHEUS_AVAILABLE,
    is_metrics_enabled,
    get_metrics_status,
    record_http_request,
    record_llm_request,
    record_ml_model_load,
    HTTP_REQUESTS,
    HTTP_LATENCY,
    LLM_REQUESTS,
    LLM_LATENCY,
    ML_MODEL_LOAD_TIME,
    ML_MODEL_LOADED,
    BUILD_INFO,
)

__all__ = [
    # APM
    "APMIntegration",
    "APMMetrics",
    # Metrics collector
    "MetricsCollector",
    "PerformanceMetrics",
    # Alerting
    "AlertManager",
    "AlertRule",
    # Prometheus metrics
    "PROMETHEUS_AVAILABLE",
    "is_metrics_enabled",
    "get_metrics_status",
    "record_http_request",
    "record_llm_request",
    "record_ml_model_load",
    "HTTP_REQUESTS",
    "HTTP_LATENCY",
    "LLM_REQUESTS",
    "LLM_LATENCY",
    "ML_MODEL_LOAD_TIME",
    "ML_MODEL_LOADED",
    "BUILD_INFO",
]