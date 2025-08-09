# FaultMaven Logging Strategy - Configuration Reference

## Overview

This document provides a comprehensive reference for configuring the FaultMaven logging system across different environments. It covers all environment variables, configuration options, integration settings, and deployment-specific configurations.

## Table of Contents

1. [Core Configuration](#core-configuration)
2. [Performance Configuration](#performance-configuration)
3. [Output Configuration](#output-configuration)
4. [Integration Configuration](#integration-configuration)
5. [Security Configuration](#security-configuration)
6. [Environment-Specific Configurations](#environment-specific-configurations)
7. [Runtime Configuration](#runtime-configuration)
8. [Troubleshooting Configuration](#troubleshooting-configuration)

## Core Configuration

### Basic Logging Settings

| Environment Variable | Default Value | Description | Valid Values |
|---------------------|---------------|-------------|--------------|
| `LOG_LEVEL` | `INFO` | Global log level | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `LOG_FORMAT` | `json` | Log output format | `json`, `text`, `structured` |
| `LOG_ENABLE_TRACING` | `true` | Enable OpenTelemetry tracing integration | `true`, `false` |
| `LOG_ENABLE_METRICS` | `true` | Enable metrics collection and export | `true`, `false` |
| `LOG_ENABLE_REQUEST_CONTEXT` | `true` | Enable request context management | `true`, `false` |
| `LOG_ENABLE_DEDUPLICATION` | `true` | Enable operation deduplication | `true`, `false` |

### Request Context Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_CORRELATION_ID_HEADER` | `X-Correlation-ID` | Header name for correlation ID |
| `LOG_SESSION_ID_HEADER` | `X-Session-ID` | Header name for session ID |
| `LOG_USER_ID_HEADER` | `X-User-ID` | Header name for user ID |
| `LOG_REQUEST_TIMEOUT` | `300` | Request context timeout (seconds) |
| `LOG_MAX_LOGGED_OPERATIONS` | `1000` | Maximum operations tracked per request |
| `LOG_MAX_CONTEXT_ATTRIBUTES` | `100` | Maximum context attributes per request |

**Example Configuration:**
```bash
# Basic logging setup
export LOG_LEVEL=INFO
export LOG_FORMAT=json
export LOG_ENABLE_TRACING=true

# Request context
export LOG_CORRELATION_ID_HEADER=X-Correlation-ID  
export LOG_REQUEST_TIMEOUT=300
export LOG_MAX_LOGGED_OPERATIONS=1000
```

## Performance Configuration

### Layer-Specific Performance Thresholds

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_PERF_THRESHOLD_API` | `0.1` | API layer performance threshold (seconds) |
| `LOG_PERF_THRESHOLD_SERVICE` | `0.5` | Service layer performance threshold (seconds) |
| `LOG_PERF_THRESHOLD_CORE` | `0.3` | Core layer performance threshold (seconds) |
| `LOG_PERF_THRESHOLD_INFRASTRUCTURE` | `1.0` | Infrastructure layer performance threshold (seconds) |

### Performance Tracking Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_ENABLE_PERFORMANCE_TRACKING` | `true` | Enable performance violation detection |
| `LOG_PERFORMANCE_SAMPLE_RATE` | `1.0` | Sample rate for performance tracking (0.0-1.0) |
| `LOG_MAX_PERFORMANCE_HISTORY` | `100` | Maximum performance records per request |
| `LOG_PERFORMANCE_VIOLATION_LOG_LEVEL` | `WARNING` | Log level for performance violations |

### Memory and Resource Limits

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_MAX_CONTEXT_MEMORY_MB` | `10` | Maximum memory per request context (MB) |
| `LOG_MAX_ATTRIBUTE_SIZE_BYTES` | `10000` | Maximum size of individual attribute |
| `LOG_BUFFER_SIZE` | `1000` | Log buffer size for async processing |
| `LOG_FLUSH_INTERVAL` | `1.0` | Buffer flush interval (seconds) |

**Example Performance Configuration:**
```bash
# Performance thresholds
export LOG_PERF_THRESHOLD_API=0.05      # Strict 50ms API threshold
export LOG_PERF_THRESHOLD_SERVICE=0.3   # Tighter 300ms service threshold  
export LOG_PERF_THRESHOLD_CORE=0.2      # 200ms core threshold
export LOG_PERF_THRESHOLD_INFRASTRUCTURE=2.0  # Relaxed 2s infrastructure threshold

# Performance tracking
export LOG_ENABLE_PERFORMANCE_TRACKING=true
export LOG_PERFORMANCE_SAMPLE_RATE=0.1  # Sample 10% of operations
```

## Output Configuration

### Standard Output Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_OUTPUT_STDOUT` | `true` | Enable stdout output |
| `LOG_OUTPUT_STDERR` | `false` | Enable stderr output for errors only |
| `LOG_STDOUT_FORMAT` | `json` | Stdout output format |
| `LOG_ENABLE_COLORS` | `false` | Enable color output (text format only) |

### File Output Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_OUTPUT_FILE` | `false` | Enable file output |
| `LOG_FILE_PATH` | `/app/logs/app.log` | Log file path |
| `LOG_FILE_MAX_SIZE_MB` | `100` | Maximum log file size (MB) |
| `LOG_FILE_MAX_FILES` | `10` | Maximum number of rotated files |
| `LOG_FILE_ROTATION` | `daily` | File rotation schedule |

### Structured Log Output

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_JSON_INDENT` | `null` | JSON indentation (null for compact) |
| `LOG_JSON_ENSURE_ASCII` | `false` | Ensure ASCII encoding in JSON |
| `LOG_TIMESTAMP_FORMAT` | `iso` | Timestamp format |
| `LOG_INCLUDE_CALLER_INFO` | `false` | Include file/line information |

**Example Output Configuration:**
```bash
# Production: Compact JSON to stdout only
export LOG_OUTPUT_STDOUT=true
export LOG_OUTPUT_FILE=false  
export LOG_JSON_INDENT=null
export LOG_TIMESTAMP_FORMAT=iso

# Development: Pretty JSON with file output
export LOG_OUTPUT_STDOUT=true
export LOG_OUTPUT_FILE=true
export LOG_FILE_PATH=/app/logs/faultmaven-dev.log
export LOG_JSON_INDENT=2
export LOG_INCLUDE_CALLER_INFO=true
```

## Integration Configuration

### OpenTelemetry Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OpenTelemetry endpoint |
| `OTEL_EXPORTER_OTLP_HEADERS` | `""` | Headers for OTLP exporter |
| `OTEL_SERVICE_NAME` | `faultmaven` | Service name for tracing |
| `OTEL_SERVICE_VERSION` | `""` | Service version |
| `OTEL_ENVIRONMENT` | `""` | Deployment environment |
| `LOG_OTEL_TRACE_CORRELATION` | `true` | Enable trace-log correlation |
| `LOG_OTEL_SPAN_EVENTS` | `true` | Add log events to spans |

### Metrics Export Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_METRICS_ENABLED` | `true` | Enable metrics collection |
| `LOG_METRICS_ENDPOINT` | `http://localhost:9090/metrics` | Prometheus metrics endpoint |
| `LOG_METRICS_INTERVAL` | `15` | Metrics collection interval (seconds) |
| `LOG_METRICS_PREFIX` | `faultmaven_logging` | Metrics name prefix |

### External Log Aggregation

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_ELASTICSEARCH_ENABLED` | `false` | Enable Elasticsearch output |
| `LOG_ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch URL |
| `LOG_ELASTICSEARCH_INDEX` | `faultmaven-logs` | Index name pattern |
| `LOG_ELASTICSEARCH_USERNAME` | `""` | Authentication username |
| `LOG_ELASTICSEARCH_PASSWORD` | `""` | Authentication password |
| `LOG_SYSLOG_ENABLED` | `false` | Enable syslog output |
| `LOG_SYSLOG_HOST` | `localhost` | Syslog server host |
| `LOG_SYSLOG_PORT` | `514` | Syslog server port |
| `LOG_SYSLOG_PROTOCOL` | `udp` | Syslog protocol |

**Example Integration Configuration:**
```bash
# OpenTelemetry setup for production
export OTEL_EXPORTER_OTLP_ENDPOINT=http://opik.faultmaven.local:30080
export OTEL_SERVICE_NAME=faultmaven
export OTEL_SERVICE_VERSION=2.1.0
export OTEL_ENVIRONMENT=production
export LOG_OTEL_TRACE_CORRELATION=true

# Metrics export
export LOG_METRICS_ENABLED=true
export LOG_METRICS_ENDPOINT=http://prometheus:9090/metrics
export LOG_METRICS_INTERVAL=15

# Elasticsearch integration  
export LOG_ELASTICSEARCH_ENABLED=true
export LOG_ELASTICSEARCH_URL=http://elasticsearch:9200
export LOG_ELASTICSEARCH_INDEX=faultmaven-logs-%{+YYYY.MM.dd}
```

## Security Configuration

### Data Privacy Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_ENABLE_DATA_SANITIZATION` | `true` | Enable automatic data sanitization |
| `LOG_SANITIZATION_MODE` | `redact` | Sanitization mode |
| `LOG_SANITIZE_HEADERS` | `true` | Sanitize HTTP headers |
| `LOG_SANITIZE_QUERY_PARAMS` | `true` | Sanitize query parameters |
| `LOG_SANITIZE_REQUEST_BODY` | `true` | Sanitize request bodies |
| `LOG_REDACTION_PLACEHOLDER` | `[REDACTED]` | Placeholder for redacted data |

### Sensitive Field Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_SENSITIVE_FIELDS` | `password,secret,token,key` | Comma-separated sensitive field names |
| `LOG_SENSITIVE_HEADERS` | `Authorization,Cookie,X-API-Key` | Sensitive HTTP headers |
| `LOG_SENSITIVE_PATTERNS` | `""` | Custom regex patterns for sensitive data |

### Access Control Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_ENABLE_ACCESS_LOGGING` | `true` | Log access attempts |
| `LOG_ACCESS_LOG_LEVEL` | `INFO` | Access log level |
| `LOG_FAILED_ACCESS_LOG_LEVEL` | `WARNING` | Failed access log level |
| `LOG_RATE_LIMIT_LOGGING` | `true` | Enable rate limit logging |

**Example Security Configuration:**
```bash
# Data privacy
export LOG_ENABLE_DATA_SANITIZATION=true
export LOG_SANITIZATION_MODE=redact
export LOG_REDACTION_PLACEHOLDER='***'

# Sensitive data patterns
export LOG_SENSITIVE_FIELDS='password,secret,token,key,ssn,credit_card'
export LOG_SENSITIVE_HEADERS='Authorization,Cookie,X-API-Key,X-Auth-Token'
export LOG_SENSITIVE_PATTERNS='\\b\\d{4}[-\\s]?\\d{4}[-\\s]?\\d{4}[-\\s]?\\d{4}\\b'
```

## Environment-Specific Configurations

### Development Environment

```bash
# .env.development
# Verbose logging for development
LOG_LEVEL=DEBUG
LOG_FORMAT=json
LOG_JSON_INDENT=2
LOG_INCLUDE_CALLER_INFO=true

# Relaxed performance thresholds
LOG_PERF_THRESHOLD_API=1.0
LOG_PERF_THRESHOLD_SERVICE=2.0
LOG_PERF_THRESHOLD_CORE=1.5
LOG_PERF_THRESHOLD_INFRASTRUCTURE=5.0

# File output for debugging
LOG_OUTPUT_FILE=true
LOG_FILE_PATH=/app/logs/faultmaven-dev.log
LOG_FILE_MAX_SIZE_MB=50

# Disabled external integrations
LOG_ELASTICSEARCH_ENABLED=false
LOG_METRICS_ENABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=""

# Enhanced debugging
LOG_ENABLE_PERFORMANCE_TRACKING=true
LOG_PERFORMANCE_SAMPLE_RATE=1.0
LOG_MAX_CONTEXT_ATTRIBUTES=200
```

### Staging Environment

```bash
# .env.staging  
# Production-like settings with enhanced monitoring
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_JSON_INDENT=null

# Production performance thresholds
LOG_PERF_THRESHOLD_API=0.1
LOG_PERF_THRESHOLD_SERVICE=0.5
LOG_PERF_THRESHOLD_CORE=0.3
LOG_PERF_THRESHOLD_INFRASTRUCTURE=1.0

# File backup with rotation
LOG_OUTPUT_FILE=true
LOG_FILE_PATH=/app/logs/faultmaven-staging.log
LOG_FILE_MAX_SIZE_MB=100
LOG_FILE_MAX_FILES=7

# External integrations enabled
LOG_ELASTICSEARCH_ENABLED=true
LOG_ELASTICSEARCH_URL=http://elasticsearch-staging:9200
LOG_ELASTICSEARCH_INDEX=faultmaven-staging-logs-%{+YYYY.MM.dd}

LOG_METRICS_ENABLED=true
LOG_METRICS_ENDPOINT=http://prometheus-staging:9090/metrics

OTEL_EXPORTER_OTLP_ENDPOINT=http://opik-staging.faultmaven.local:30080
OTEL_SERVICE_NAME=faultmaven-staging
OTEL_ENVIRONMENT=staging

# Sampling for performance
LOG_PERFORMANCE_SAMPLE_RATE=0.5
```

### Production Environment

```bash
# .env.production
# Optimized for performance and reliability
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_JSON_INDENT=null

# Strict performance thresholds
LOG_PERF_THRESHOLD_API=0.1
LOG_PERF_THRESHOLD_SERVICE=0.5  
LOG_PERF_THRESHOLD_CORE=0.3
LOG_PERF_THRESHOLD_INFRASTRUCTURE=1.0

# Stdout only for container logging
LOG_OUTPUT_STDOUT=true
LOG_OUTPUT_FILE=false

# External log aggregation
LOG_ELASTICSEARCH_ENABLED=true
LOG_ELASTICSEARCH_URL=http://elasticsearch-cluster:9200
LOG_ELASTICSEARCH_INDEX=faultmaven-logs-%{+YYYY.MM.dd}
LOG_ELASTICSEARCH_USERNAME=logstash_writer
LOG_ELASTICSEARCH_PASSWORD=${ELASTICSEARCH_PASSWORD}

# Metrics and tracing
LOG_METRICS_ENABLED=true
LOG_METRICS_ENDPOINT=http://prometheus:9090/metrics
LOG_METRICS_INTERVAL=30

OTEL_EXPORTER_OTLP_ENDPOINT=http://opik.faultmaven.local:30080
OTEL_SERVICE_NAME=faultmaven
OTEL_SERVICE_VERSION=${APP_VERSION}
OTEL_ENVIRONMENT=production

# Optimized for scale
LOG_PERFORMANCE_SAMPLE_RATE=0.1
LOG_MAX_LOGGED_OPERATIONS=500
LOG_MAX_CONTEXT_ATTRIBUTES=50
LOG_BUFFER_SIZE=5000
LOG_FLUSH_INTERVAL=0.5

# Security settings
LOG_ENABLE_DATA_SANITIZATION=true
LOG_SANITIZATION_MODE=redact
LOG_SENSITIVE_FIELDS=password,secret,token,key,ssn,credit_card,auth_token
```

## Runtime Configuration

### Dynamic Configuration Updates

The logging system supports runtime configuration updates for certain settings:

```python
# Runtime configuration API
from faultmaven.infrastructure.logging.config import RuntimeConfig

# Update log level at runtime
RuntimeConfig.update_log_level("DEBUG")

# Update performance thresholds
RuntimeConfig.update_performance_thresholds({
    "api": 0.05,
    "service": 0.3,
    "core": 0.2,
    "infrastructure": 2.0
})

# Update sampling rates
RuntimeConfig.update_sampling_rate(0.5)
```

### Configuration Validation

```python
# Configuration validation
from faultmaven.infrastructure.logging.config import ConfigValidator

validator = ConfigValidator()

# Validate current configuration
validation_result = validator.validate_config()
if not validation_result.is_valid:
    print(f"Configuration errors: {validation_result.errors}")

# Check specific settings
performance_valid = validator.validate_performance_thresholds()
security_valid = validator.validate_security_settings()
```

### Health Check Configuration

| Environment Variable | Default Value | Description |
|---------------------|---------------|-------------|
| `LOG_HEALTH_CHECK_ENABLED` | `true` | Enable logging health checks |
| `LOG_HEALTH_CHECK_INTERVAL` | `60` | Health check interval (seconds) |
| `LOG_HEALTH_CHECK_ENDPOINT` | `/health/logging` | Health check endpoint |
| `LOG_HEALTH_TIMEOUT` | `30` | Health check timeout (seconds) |

## Troubleshooting Configuration

### Debug Configuration

```bash
# Maximum debugging configuration
export LOG_LEVEL=DEBUG
export LOG_INCLUDE_CALLER_INFO=true
export LOG_JSON_INDENT=2
export LOG_ENABLE_PERFORMANCE_TRACKING=true
export LOG_PERFORMANCE_SAMPLE_RATE=1.0

# Disable optimizations for debugging
export LOG_ENABLE_DEDUPLICATION=false
export LOG_BUFFER_SIZE=1
export LOG_FLUSH_INTERVAL=0.1

# Extended context limits
export LOG_MAX_LOGGED_OPERATIONS=5000
export LOG_MAX_CONTEXT_ATTRIBUTES=500
```

### Memory Debugging

```bash
# Memory usage tracking
export LOG_ENABLE_MEMORY_TRACKING=true
export LOG_MEMORY_CHECK_INTERVAL=10
export LOG_MAX_CONTEXT_MEMORY_MB=1  # Strict limit for debugging

# Memory leak detection  
export LOG_ENABLE_LEAK_DETECTION=true
export LOG_LEAK_DETECTION_THRESHOLD=100
```

### Performance Debugging

```bash
# Detailed performance tracking
export LOG_PERFORMANCE_SAMPLE_RATE=1.0
export LOG_ENABLE_OPERATION_PROFILING=true
export LOG_PROFILE_SLOW_OPERATIONS=true
export LOG_SLOW_OPERATION_THRESHOLD=0.01  # 10ms threshold

# Performance violation details
export LOG_PERFORMANCE_VIOLATION_LOG_LEVEL=INFO
export LOG_INCLUDE_STACK_TRACE=true
```

## Configuration Templates

### Docker Compose Configuration

```yaml
# docker-compose.yml
version: '3.8'
services:
  faultmaven:
    environment:
      # Core logging
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - LOG_FORMAT=json
      - LOG_ENABLE_TRACING=true
      
      # Performance
      - LOG_PERF_THRESHOLD_API=0.1
      - LOG_PERF_THRESHOLD_SERVICE=0.5
      - LOG_PERF_THRESHOLD_CORE=0.3
      - LOG_PERF_THRESHOLD_INFRASTRUCTURE=1.0
      
      # Integrations
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://opik:4317
      - LOG_ELASTICSEARCH_ENABLED=true
      - LOG_ELASTICSEARCH_URL=http://elasticsearch:9200
      
      # Security
      - LOG_ENABLE_DATA_SANITIZATION=true
      - LOG_SANITIZATION_MODE=redact
    volumes:
      - ./logs:/app/logs
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### Kubernetes Configuration

```yaml
# kubernetes-config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: faultmaven-logging-config
data:
  LOG_LEVEL: "INFO"
  LOG_FORMAT: "json" 
  LOG_ENABLE_TRACING: "true"
  LOG_PERF_THRESHOLD_API: "0.1"
  LOG_PERF_THRESHOLD_SERVICE: "0.5"
  LOG_PERF_THRESHOLD_CORE: "0.3"
  LOG_PERF_THRESHOLD_INFRASTRUCTURE: "1.0"
  OTEL_EXPORTER_OTLP_ENDPOINT: "http://opik.faultmaven.local:30080"
  OTEL_SERVICE_NAME: "faultmaven"
  OTEL_ENVIRONMENT: "production"
  LOG_ELASTICSEARCH_ENABLED: "true"
  LOG_ELASTICSEARCH_URL: "http://elasticsearch:9200"
  LOG_METRICS_ENABLED: "true"
  LOG_ENABLE_DATA_SANITIZATION: "true"
---
apiVersion: v1
kind: Secret
metadata:
  name: faultmaven-logging-secrets
type: Opaque
stringData:
  LOG_ELASTICSEARCH_USERNAME: "logstash_writer"
  LOG_ELASTICSEARCH_PASSWORD: "secure_password"
```

### Terraform Configuration

```hcl
# terraform/logging.tf
variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "log_level" {
  description = "Log level"
  type        = string
  default     = "INFO"
}

locals {
  logging_config = {
    development = {
      log_level                    = "DEBUG"
      performance_api_threshold    = "1.0"
      performance_service_threshold = "2.0" 
      enable_file_output          = true
      elasticsearch_enabled       = false
    }
    staging = {
      log_level                    = "INFO"
      performance_api_threshold    = "0.2"
      performance_service_threshold = "0.8"
      enable_file_output          = true
      elasticsearch_enabled       = true
    }
    production = {
      log_level                    = "INFO"  
      performance_api_threshold    = "0.1"
      performance_service_threshold = "0.5"
      enable_file_output          = false
      elasticsearch_enabled       = true
    }
  }
}

resource "kubernetes_config_map" "logging_config" {
  metadata {
    name      = "faultmaven-logging-config"
    namespace = var.namespace
  }

  data = {
    LOG_LEVEL                        = local.logging_config[var.environment].log_level
    LOG_PERF_THRESHOLD_API          = local.logging_config[var.environment].performance_api_threshold
    LOG_PERF_THRESHOLD_SERVICE      = local.logging_config[var.environment].performance_service_threshold
    LOG_OUTPUT_FILE                 = tostring(local.logging_config[var.environment].enable_file_output)
    LOG_ELASTICSEARCH_ENABLED       = tostring(local.logging_config[var.environment].elasticsearch_enabled)
    OTEL_SERVICE_NAME               = "faultmaven"
    OTEL_ENVIRONMENT                = var.environment
  }
}
```

This configuration reference provides comprehensive guidance for setting up the FaultMaven logging system across all deployment scenarios. Use the appropriate configuration template for your environment and customize the settings based on your specific requirements.