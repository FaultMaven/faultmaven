# FaultMaven Logging Strategy - Operations Runbook

## Overview

This runbook provides operational guidance for monitoring, troubleshooting, and maintaining the FaultMaven logging system in production environments. It includes monitoring setup, alerting configuration, performance optimization, and troubleshooting procedures.

## Table of Contents

1. [Monitoring Setup](#monitoring-setup)
2. [Key Metrics](#key-metrics)
3. [Alerting Configuration](#alerting-configuration)
4. [Log Analysis Patterns](#log-analysis-patterns)
5. [Troubleshooting Guide](#troubleshooting-guide)
6. [Performance Optimization](#performance-optimization)
7. [Maintenance Procedures](#maintenance-procedures)
8. [Emergency Procedures](#emergency-procedures)

## Monitoring Setup

### Essential Monitoring Components

#### 1. Log Aggregation System

**ELK Stack Configuration:**
```yaml
# elasticsearch.yml
cluster.name: faultmaven-logs
node.name: ${HOSTNAME}
path.data: /var/lib/elasticsearch
path.logs: /var/log/elasticsearch

# logstash.conf
input {
  beats {
    port => 5044
  }
}

filter {
  if [fields][service] == "faultmaven" {
    json {
      source => "message"
    }
    
    # Extract performance violations
    if [performance_violation] == true {
      mutate {
        add_tag => ["performance_issue"]
      }
    }
    
    # Extract error cascades
    if [cascade_prevented] == true {
      mutate {
        add_tag => ["error_cascade_prevented"]
      }
    }
  }
}

output {
  elasticsearch {
    hosts => ["elasticsearch:9200"]
    index => "faultmaven-logs-%{+YYYY.MM.dd}"
  }
}
```

**Filebeat Configuration:**
```yaml
# filebeat.yml
filebeat.inputs:
- type: log
  enabled: true
  paths:
    - /app/logs/*.log
  fields:
    service: faultmaven
  fields_under_root: true
  json.keys_under_root: true
  json.add_error_key: true

output.logstash:
  hosts: ["logstash:5044"]

processors:
  - add_host_metadata:
      when.not.contains.tags: forwarded
```

#### 2. Grafana Dashboards

**Main Monitoring Dashboard:**
```json
{
  "dashboard": {
    "title": "FaultMaven Logging Metrics",
    "panels": [
      {
        "title": "Request Processing Rate",
        "type": "stat",
        "targets": [{
          "expr": "rate(faultmaven_requests_total[5m])",
          "legendFormat": "Requests/sec"
        }]
      },
      {
        "title": "Performance Violations by Layer", 
        "type": "bargauge",
        "targets": [{
          "expr": "increase(faultmaven_performance_violations_total[1h])",
          "legendFormat": "{{layer}}"
        }]
      },
      {
        "title": "Error Cascade Prevention",
        "type": "stat", 
        "targets": [{
          "expr": "increase(faultmaven_error_cascades_prevented_total[1h])",
          "legendFormat": "Cascades Prevented"
        }]
      },
      {
        "title": "Duplicate Logs Prevented",
        "type": "stat",
        "targets": [{
          "expr": "increase(faultmaven_duplicate_logs_prevented_total[1h])",  
          "legendFormat": "Duplicates Prevented"
        }]
      }
    ]
  }
}
```

#### 3. Prometheus Metrics Collection

**Application Metrics Export:**
```python
# metrics.py - Add to application
from prometheus_client import Counter, Histogram, Gauge
from faultmaven.infrastructure.logging.coordinator import request_context

# Define metrics
REQUEST_DURATION = Histogram(
    'faultmaven_request_duration_seconds',
    'Time spent processing requests',
    ['method', 'endpoint', 'status']
)

PERFORMANCE_VIOLATIONS = Counter(
    'faultmaven_performance_violations_total',
    'Number of performance violations',
    ['layer', 'operation']
)

ERROR_CASCADES_PREVENTED = Counter(
    'faultmaven_error_cascades_prevented_total',
    'Number of error cascades prevented'
)

DUPLICATE_LOGS_PREVENTED = Counter(
    'faultmaven_duplicate_logs_prevented_total',
    'Number of duplicate logs prevented'
)

ACTIVE_REQUESTS = Gauge(
    'faultmaven_active_requests',
    'Number of currently active requests'
)

def export_request_metrics(request_summary: dict):
    """Export metrics from request summary."""
    REQUEST_DURATION.observe(request_summary.get('duration_seconds', 0))
    
    performance_violations = request_summary.get('performance_violations', 0)
    if performance_violations > 0:
        PERFORMANCE_VIOLATIONS.inc(performance_violations)
    
    operations_logged = request_summary.get('operations_logged', 0)
    # Estimate duplicates prevented (rough calculation)
    estimated_duplicates = max(0, operations_logged - request_summary.get('unique_operations', operations_logged))
    if estimated_duplicates > 0:
        DUPLICATE_LOGS_PREVENTED.inc(estimated_duplicates)
```

## Key Metrics

### 1. Request Processing Metrics

**Primary Indicators:**
- **Request Rate**: Requests processed per second
- **Request Duration**: p50, p95, p99 latencies by endpoint
- **Active Requests**: Current number of concurrent requests
- **Request Success Rate**: Percentage of successful requests

**Query Examples:**
```promql
# Request rate
rate(faultmaven_requests_total[5m])

# Request duration percentiles
histogram_quantile(0.95, rate(faultmaven_request_duration_seconds_bucket[5m]))

# Success rate
rate(faultmaven_requests_total{status=~"2.."}[5m]) / rate(faultmaven_requests_total[5m])
```

### 2. Performance Violation Metrics

**Key Indicators:**
- **Violations by Layer**: API, Service, Core, Infrastructure performance violations
- **Violation Rate**: Percentage of operations exceeding thresholds
- **Slow Operations**: Top 10 slowest operations by layer

**Log Queries:**
```json
// Elasticsearch query for performance violations
{
  "query": {
    "bool": {
      "must": [
        {"term": {"performance_violation": true}},
        {"range": {"timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "aggs": {
    "by_layer": {"terms": {"field": "layer"}},
    "by_operation": {"terms": {"field": "operation"}}
  }
}
```

### 3. Error and Cascade Metrics

**Key Indicators:**
- **Error Rate**: Errors per minute by layer and operation
- **Cascade Prevention**: Number of error cascades prevented
- **Circuit Breaker Trips**: External service circuit breaker activations
- **Recovery Success**: Successful error recovery attempts

**Analysis Queries:**
```json
// Error cascade analysis
{
  "query": {
    "bool": {
      "must": [
        {"term": {"level": "ERROR"}},
        {"range": {"timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "aggs": {
    "cascade_prevented": {
      "filter": {"term": {"cascade_prevented": true}}
    },
    "by_layer": {
      "terms": {"field": "layer"}
    }
  }
}
```

### 4. Deduplication Effectiveness

**Key Indicators:**
- **Duplicate Logs Prevented**: Number of duplicate log entries prevented
- **Deduplication Rate**: Percentage of operations that would have been duplicated
- **Context Propagation**: Success rate of correlation ID propagation

## Alerting Configuration

### 1. Critical Alerts (Immediate Response Required)

#### High Error Rate Alert
```yaml
groups:
- name: faultmaven-critical
  rules:
  - alert: FaultMavenHighErrorRate
    expr: rate(faultmaven_requests_total{status=~"5.."}[5m]) > 0.05
    for: 2m
    labels:
      severity: critical
    annotations:
      summary: "High error rate detected"
      description: "Error rate is {{ $value }} errors/sec for 2+ minutes"
      runbook_url: "https://docs.faultmaven.com/runbooks/high-error-rate"
```

#### Performance Violation Spike Alert
```yaml
  - alert: FaultMavenPerformanceViolationSpike
    expr: increase(faultmaven_performance_violations_total[5m]) > 50
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Performance violation spike detected"
      description: "{{ $value }} performance violations in last 5 minutes"
```

#### Circuit Breaker Open Alert
```yaml
  - alert: FaultMavenCircuitBreakerOpen
    expr: increase(faultmaven_circuit_breaker_trips_total[5m]) > 5
    for: 30s
    labels:
      severity: critical
    annotations:
      summary: "Multiple circuit breakers opening"
      description: "{{ $value }} circuit breakers opened in last 5 minutes"
```

### 2. Warning Alerts (Monitor Closely)

#### Performance Degradation Alert
```yaml
- name: faultmaven-warnings
  rules:
  - alert: FaultMavenPerformanceDegradation
    expr: histogram_quantile(0.95, rate(faultmaven_request_duration_seconds_bucket[10m])) > 2.0
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Request latency degradation"
      description: "p95 latency is {{ $value }}s for 5+ minutes"
```

#### Log Volume Spike Alert
```yaml
  - alert: FaultMavenLogVolumeSpike
    expr: rate(faultmaven_log_entries_total[5m]) > 1000
    for: 3m
    labels:
      severity: warning
    annotations:
      summary: "High log volume detected"
      description: "Log volume is {{ $value }} entries/sec"
```

### 3. Info Alerts (Notification Only)

#### Deduplication Effectiveness Alert
```yaml
  - alert: FaultMavenDeduplicationEffectiveness
    expr: rate(faultmaven_duplicate_logs_prevented_total[1h]) < 10
    for: 15m
    labels:
      severity: info
    annotations:
      summary: "Low deduplication activity"
      description: "Only {{ $value }} duplicates prevented in last hour"
```

## Log Analysis Patterns

### 1. Request Tracing

**Find all logs for a specific request:**
```json
{
  "query": {
    "term": {"correlation_id": "req_12345"}
  },
  "sort": [{"timestamp": {"order": "asc"}}]
}
```

**Analyze request flow:**
```bash
# Using jq to analyze request flow
cat logs/app.log | jq -r 'select(.correlation_id=="req_12345") | "\(.timestamp) \(.layer) \(.event_type) \(.operation // .message)"' | sort
```

### 2. Performance Analysis

**Find slow operations by layer:**
```json
{
  "query": {
    "bool": {
      "must": [
        {"term": {"event_type": "operation_end"}},
        {"range": {"duration_seconds": {"gte": 1.0}}}
      ]
    }
  },
  "aggs": {
    "by_layer_operation": {
      "composite": {
        "sources": [
          {"layer": {"terms": {"field": "layer"}}},
          {"operation": {"terms": {"field": "operation"}}}
        ]
      },
      "aggs": {
        "avg_duration": {"avg": {"field": "duration_seconds"}},
        "max_duration": {"max": {"field": "duration_seconds"}}
      }
    }
  }
}
```

**Performance violation trending:**
```bash
# Daily performance violation trend
curl -X GET "elasticsearch:9200/faultmaven-logs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": {"term": {"performance_violation": true}},
    "aggs": {
      "violations_per_day": {
        "date_histogram": {
          "field": "timestamp",
          "calendar_interval": "1d"
        }
      }
    }
  }'
```

### 3. Error Pattern Analysis

**Error clustering by operation:**
```json
{
  "query": {
    "bool": {
      "must": [
        {"term": {"level": "ERROR"}},
        {"range": {"timestamp": {"gte": "now-24h"}}}
      ]
    }
  },
  "aggs": {
    "error_patterns": {
      "terms": {"field": "operation"},
      "aggs": {
        "error_types": {"terms": {"field": "error_type"}},
        "cascade_prevention": {
          "filter": {"term": {"cascade_prevented": true}}
        }
      }
    }
  }
}
```

**Error cascade effectiveness:**
```bash
# Count prevented vs actual error cascades
echo "Error Cascade Analysis:"
echo "Errors by layer:"
cat logs/app.log | jq -r 'select(.level=="ERROR") | .layer' | sort | uniq -c

echo "Cascades prevented:"
cat logs/app.log | jq -r 'select(.cascade_prevented==true)' | wc -l
```

### 4. Circuit Breaker Analysis

**Circuit breaker status monitoring:**
```json
{
  "query": {
    "term": {"event_type": "circuit_breaker_open"}
  },
  "aggs": {
    "by_service": {"terms": {"field": "service"}},
    "timeline": {
      "date_histogram": {
        "field": "timestamp",
        "interval": "1h"
      }
    }
  }
}
```

## Troubleshooting Guide

### 1. High Log Volume Issues

**Symptoms:**
- Disk space filling rapidly
- Elasticsearch cluster struggling
- High I/O on log storage

**Investigation Steps:**

1. **Check log volume by component:**
```bash
# Analyze log volume by logger
cat logs/app.log | jq -r '.logger_name' | sort | uniq -c | sort -nr | head -20

# Check for log level distribution
cat logs/app.log | jq -r '.level' | sort | uniq -c
```

2. **Identify verbose operations:**
```json
{
  "size": 0,
  "aggs": {
    "by_operation": {
      "terms": {"field": "operation", "size": 50},
      "aggs": {
        "log_count": {"value_count": {"field": "timestamp"}}
      }
    }
  }
}
```

3. **Check for deduplication failures:**
```bash
# Look for operations that should be deduplicated but aren't
grep -E "operation_start|operation_end" logs/app.log | \
  jq -r '[.correlation_id, .operation, .event_type] | @csv' | \
  sort | uniq -c | sort -nr | head -20
```

**Resolution Steps:**

1. **Adjust log levels:**
```bash
# Temporarily increase log level
export LOG_LEVEL=WARNING
kubectl set env deployment/faultmaven LOG_LEVEL=WARNING
```

2. **Enable log filtering:**
```python
# Add to logging configuration
VERBOSE_OPERATION_FILTER = [
    "heartbeat", 
    "health_check", 
    "metrics_collection"
]

def should_skip_operation(operation_name: str) -> bool:
    return operation_name in VERBOSE_OPERATION_FILTER
```

3. **Implement log sampling:**
```python
# Sample high-frequency operations
import random

def should_sample_log(operation: str, sample_rate: float = 0.1) -> bool:
    if operation in ["frequent_operation"]:
        return random.random() < sample_rate
    return True
```

### 2. Performance Violation Spikes

**Symptoms:**
- Sudden increase in performance violation alerts
- Response time degradation
- User complaints about slow responses

**Investigation Steps:**

1. **Identify affected layers:**
```json
{
  "query": {
    "bool": {
      "must": [
        {"term": {"performance_violation": true}},
        {"range": {"timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "aggs": {
    "by_layer": {"terms": {"field": "layer"}},
    "by_operation": {"terms": {"field": "operation", "size": 20}}
  }
}
```

2. **Analyze operation timing trends:**
```bash
# Extract timing data for slow operations
cat logs/app.log | jq -r 'select(.performance_violation==true) | [.timestamp, .layer, .operation, .duration_seconds, .threshold_seconds] | @csv' > performance_violations.csv
```

3. **Check system resources:**
```bash
# Check system metrics during violation period
kubectl top nodes
kubectl top pods

# Check database performance
psql -c "SELECT query, mean_exec_time, calls FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;"
```

**Resolution Steps:**

1. **Immediate mitigation:**
```bash
# Scale up if resource constrained
kubectl scale deployment/faultmaven --replicas=5

# Check for resource limits
kubectl describe pod faultmaven-xxx | grep -A 5 "Limits"
```

2. **Optimize slow operations:**
```python
# Add operation-specific optimizations
async def optimize_slow_operation(data):
    # Add caching
    cache_key = f"operation:{hash(str(data))}"
    cached_result = await cache.get(cache_key)
    if cached_result:
        return cached_result
    
    result = await perform_operation(data)
    await cache.set(cache_key, result, ttl=300)
    return result
```

3. **Adjust performance thresholds:**
```bash
# Temporarily adjust thresholds if realistic
export LOG_PERF_THRESHOLD_SERVICE=1.0  # Increase from 0.5s
kubectl set env deployment/faultmaven LOG_PERF_THRESHOLD_SERVICE=1.0
```

### 3. Missing Correlation IDs

**Symptoms:**
- Logs missing correlation_id field
- Unable to trace requests across services
- Debugging becomes difficult

**Investigation Steps:**

1. **Check context initialization:**
```bash
# Look for requests without correlation IDs
cat logs/app.log | jq -r 'select(.correlation_id == null) | .message' | head -10

# Check middleware configuration
grep -r "LoggingMiddleware" src/
```

2. **Verify context propagation:**
```python
# Add debugging to middleware
async def debug_context_propagation(request: Request, call_next):
    ctx = request_context.get()
    logger.info(f"Context before request: {ctx.correlation_id if ctx else 'None'}")
    
    response = await call_next(request)
    
    ctx_after = request_context.get()
    logger.info(f"Context after request: {ctx_after.correlation_id if ctx_after else 'None'}")
    
    return response
```

**Resolution Steps:**

1. **Fix middleware ordering:**
```python
# Ensure LoggingMiddleware is first
app.add_middleware(LoggingMiddleware)  # Must be first
app.add_middleware(CORSMiddleware, ...)
app.add_middleware(OtherMiddleware, ...)
```

2. **Handle async context propagation:**
```python
import asyncio
from contextvars import copy_context

# When creating background tasks
ctx = copy_context()
task = ctx.run(asyncio.create_task, background_operation())
```

### 4. Circuit Breaker Issues

**Symptoms:**
- External services marked as unavailable
- Cascade failures across services
- Circuit breaker trips frequently

**Investigation Steps:**

1. **Check circuit breaker status:**
```bash
# Query circuit breaker events
cat logs/app.log | jq -r 'select(.event_type=="circuit_breaker_open") | [.timestamp, .client, .service] | @csv'

# Check health endpoints
curl http://faultmaven:8000/health/dependencies
```

2. **Analyze failure patterns:**
```json
{
  "query": {
    "term": {"event_type": "external_call_failed"}
  },
  "aggs": {
    "by_service": {"terms": {"field": "service"}},
    "failure_timeline": {
      "date_histogram": {
        "field": "timestamp", 
        "interval": "5m"
      }
    }
  }
}
```

**Resolution Steps:**

1. **Adjust circuit breaker thresholds:**
```python
# Increase failure threshold temporarily
client = BaseExternalClient(
    client_name="api_client",
    service_name="ExternalAPI",
    circuit_breaker_threshold=10,  # Increase from 5
    circuit_breaker_timeout=30     # Reduce timeout
)
```

2. **Implement graceful degradation:**
```python
# Add fallback mechanisms
async def call_with_fallback(operation):
    try:
        return await primary_service.call(operation)
    except CircuitBreakerError:
        logger.info("Circuit breaker open, using fallback")
        return await fallback_service.call(operation)
```

## Performance Optimization

### 1. Log Processing Optimization

**Reduce JSON Serialization Overhead:**
```python
# Lazy field evaluation
class LazyLogData:
    def __init__(self, data_func):
        self._data_func = data_func
        self._cached_data = None
    
    def __str__(self):
        if self._cached_data is None:
            self._cached_data = self._data_func()
        return json.dumps(self._cached_data)

# Usage
logger.info("Complex operation", 
           expensive_data=LazyLogData(lambda: compute_expensive_data()))
```

**Optimize Context Variable Access:**
```python
# Cache context lookups within request
_context_cache = None

def get_cached_context():
    global _context_cache
    if _context_cache is None:
        _context_cache = request_context.get()
    return _context_cache

# Reset cache at request end
def clear_context_cache():
    global _context_cache
    _context_cache = None
```

### 2. Memory Usage Optimization

**Limit Context Data Size:**
```python
class RequestContext:
    MAX_LOGGED_OPERATIONS = 1000
    MAX_ATTRIBUTES_SIZE = 10000
    
    def mark_logged(self, operation_key: str):
        if len(self.logged_operations) >= self.MAX_LOGGED_OPERATIONS:
            # Remove oldest entries
            oldest_ops = sorted(self.logged_operations)[:100]
            for op in oldest_ops:
                self.logged_operations.discard(op)
        
        self.logged_operations.add(operation_key)
```

**Optimize Performance Tracker:**
```python
class PerformanceTracker:
    MAX_TIMINGS = 500
    
    def record_timing(self, layer: str, operation: str, duration: float):
        if len(self.layer_timings) >= self.MAX_TIMINGS:
            # Keep only recent timings
            recent_timings = dict(list(self.layer_timings.items())[-400:])
            self.layer_timings = recent_timings
        
        # Continue with normal recording
        key = f"{layer}.{operation}"
        self.layer_timings[key] = duration
```

### 3. I/O Optimization

**Async Log Writing:**
```python
import asyncio
from asyncio import Queue

class AsyncLogWriter:
    def __init__(self, max_queue_size=10000):
        self.log_queue = Queue(maxsize=max_queue_size)
        self.writer_task = None
    
    async def start(self):
        self.writer_task = asyncio.create_task(self._write_logs())
    
    async def write_log(self, log_entry):
        try:
            await self.log_queue.put(log_entry, timeout=0.1)
        except asyncio.TimeoutError:
            # Drop log if queue full (backpressure)
            pass
    
    async def _write_logs(self):
        while True:
            try:
                log_entry = await self.log_queue.get()
                # Write to file/stdout
                await self._actual_write(log_entry)
                self.log_queue.task_done()
            except Exception as e:
                # Handle write errors
                pass
```

## Maintenance Procedures

### 1. Log Rotation and Retention

**Daily Maintenance Script:**
```bash
#!/bin/bash
# daily-log-maintenance.sh

set -e

LOG_DIR="/app/logs"
RETENTION_DAYS=30
ARCHIVE_DAYS=7

echo "Starting daily log maintenance - $(date)"

# Rotate current logs
logrotate /etc/logrotate.d/faultmaven

# Archive logs older than 7 days
find "$LOG_DIR" -name "*.log" -mtime +$ARCHIVE_DAYS -exec gzip {} \;

# Delete logs older than retention period
find "$LOG_DIR" -name "*.log.gz" -mtime +$RETENTION_DAYS -delete

# Clean up Elasticsearch indices
curl -X DELETE "elasticsearch:9200/faultmaven-logs-$(date -d '-30 days' +%Y.%m.%d)"

echo "Log maintenance completed - $(date)"
```

**Logrotate Configuration:**
```
# /etc/logrotate.d/faultmaven
/app/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0644 app app
    postrotate
        # Signal application to reopen log files
        kill -USR1 $(cat /app/faultmaven.pid) 2>/dev/null || true
    endscript
}
```

### 2. Index Optimization

**Weekly Elasticsearch Maintenance:**
```bash
#!/bin/bash
# weekly-elasticsearch-maintenance.sh

# Force merge old indices
for index in $(curl -s "elasticsearch:9200/_cat/indices/faultmaven-logs-*?h=index" | grep -E "[0-9]{4}\.[0-9]{2}\.[0-9]{2}" | head -7); do
    echo "Force merging $index"
    curl -X POST "elasticsearch:9200/$index/_forcemerge?max_num_segments=1"
done

# Update index templates
curl -X PUT "elasticsearch:9200/_template/faultmaven-logs" \
  -H 'Content-Type: application/json' \
  -d '{
    "index_patterns": ["faultmaven-logs-*"],
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "refresh_interval": "30s"
    },
    "mappings": {
      "properties": {
        "timestamp": {"type": "date"},
        "correlation_id": {"type": "keyword"},
        "layer": {"type": "keyword"},
        "operation": {"type": "keyword"},
        "duration_seconds": {"type": "float"},
        "performance_violation": {"type": "boolean"}
      }
    }
  }'
```

### 3. Health Checks

**Application Health Monitoring:**
```python
# health_checks.py
async def check_logging_system_health():
    """Comprehensive logging system health check."""
    health_status = {
        "timestamp": datetime.utcnow().isoformat(),
        "overall_status": "healthy",
        "components": {}
    }
    
    # Check context management
    try:
        coordinator = LoggingCoordinator()
        test_context = coordinator.start_request(session_id="health_check")
        coordinator.end_request()
        health_status["components"]["context_management"] = "healthy"
    except Exception as e:
        health_status["components"]["context_management"] = f"unhealthy: {e}"
        health_status["overall_status"] = "degraded"
    
    # Check logger creation
    try:
        test_logger = get_unified_logger("health_check", "api")
        health_status["components"]["logger_factory"] = "healthy"
    except Exception as e:
        health_status["components"]["logger_factory"] = f"unhealthy: {e}"
        health_status["overall_status"] = "degraded"
    
    # Check performance tracking
    try:
        tracker = PerformanceTracker()
        exceeds, threshold = tracker.record_timing("api", "test", 0.05)
        health_status["components"]["performance_tracking"] = "healthy"
    except Exception as e:
        health_status["components"]["performance_tracking"] = f"unhealthy: {e}"
        health_status["overall_status"] = "degraded"
    
    return health_status
```

## Emergency Procedures

### 1. Log System Failure

**Immediate Actions:**

1. **Disable non-critical logging:**
```bash
# Emergency log level adjustment
export LOG_LEVEL=ERROR
kubectl set env deployment/faultmaven LOG_LEVEL=ERROR

# Disable performance tracking temporarily
export LOG_ENABLE_PERFORMANCE_TRACKING=false
kubectl set env deployment/faultmaven LOG_ENABLE_PERFORMANCE_TRACKING=false
```

2. **Switch to minimal logging:**
```python
# Emergency logging fallback
import logging
import sys

# Simple fallback logger
fallback_logger = logging.getLogger('faultmaven.emergency')
fallback_logger.setLevel(logging.ERROR)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
fallback_logger.addHandler(handler)

# Replace unified logger temporarily
def emergency_log_error(message, **kwargs):
    fallback_logger.error(f"{message} {kwargs}")
```

### 2. Performance Degradation

**Emergency Response:**

1. **Disable expensive logging features:**
```bash
# Disable operation context managers
export LOG_ENABLE_OPERATION_TRACKING=false

# Disable boundary logging
export LOG_ENABLE_BOUNDARY_LOGGING=false

# Disable metric collection
export LOG_ENABLE_METRICS=false
```

2. **Scale application resources:**
```bash
# Increase resource limits
kubectl patch deployment faultmaven -p '{"spec":{"template":{"spec":{"containers":[{"name":"faultmaven","resources":{"limits":{"memory":"2Gi","cpu":"1000m"}}}]}}}}'

# Scale replicas
kubectl scale deployment/faultmaven --replicas=10
```

### 3. Disk Space Emergency

**Immediate Actions:**

1. **Emergency log cleanup:**
```bash
# Delete old logs immediately
find /app/logs -name "*.log" -mtime +1 -delete
find /app/logs -name "*.log.gz" -mtime +7 -delete

# Truncate current log files
truncate -s 100M /app/logs/app.log
```

2. **Redirect logs to external system:**
```bash
# Pipe logs to external syslog
export LOG_HANDLER=syslog
export SYSLOG_SERVER=external-log-server:514
```

### 4. Elasticsearch Outage

**Fallback Procedures:**

1. **Switch to file-based logging:**
```bash
# Disable Elasticsearch output
export LOG_OUTPUT=file
export LOG_FILE_PATH=/app/logs/emergency.log

# Restart Filebeat to use file output
kubectl restart daemonset/filebeat
```

2. **Enable local log aggregation:**
```python
# Emergency local log aggregation
import json
import gzip
from pathlib import Path

class EmergencyLogAggregator:
    def __init__(self, output_dir="/app/emergency-logs"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
    def aggregate_logs(self, timeframe_hours=24):
        """Aggregate logs for emergency analysis."""
        log_files = list(Path("/app/logs").glob("*.log"))
        
        aggregated_data = {
            "errors": [],
            "performance_violations": [],
            "circuit_breaker_events": []
        }
        
        for log_file in log_files:
            with open(log_file) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        
                        if entry.get("level") == "ERROR":
                            aggregated_data["errors"].append(entry)
                        elif entry.get("performance_violation"):
                            aggregated_data["performance_violations"].append(entry)
                        elif "circuit_breaker" in entry.get("event_type", ""):
                            aggregated_data["circuit_breaker_events"].append(entry)
                            
                    except json.JSONDecodeError:
                        continue
        
        # Write aggregated data
        output_file = self.output_dir / f"emergency-aggregate-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json.gz"
        with gzip.open(output_file, 'wt') as f:
            json.dump(aggregated_data, f, indent=2)
        
        return str(output_file)
```

This operations runbook provides comprehensive guidance for monitoring, troubleshooting, and maintaining the FaultMaven logging system in production environments. Regular review and practice of these procedures ensures reliable operation and quick response to issues.