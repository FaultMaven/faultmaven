# FaultMaven Improved Logging Strategy - Production-Ready Implementation

## Executive Summary

This document presents a robust, production-ready logging strategy that eliminates duplicate log entries through architectural patterns, clear responsibility boundaries, and unified request coordination. The strategy provides excellent observability while maintaining performance and security.

### Key Improvements (v2)
- **Simplified UnifiedLogger**: Single logger class handles all concerns, reducing complexity from 3 patterns to 1
- **Error Cascade Prevention**: ErrorContext tracks errors across layers, preventing duplicate error logging
- **Performance Tracking**: Automatic performance monitoring with configurable thresholds per layer
- **Cleaner API**: Simple context manager pattern for all logging needs

## Problem Analysis

### Root Causes of Duplicate Logging

1. **Multiple Middleware Layers**: FastAPI middleware, custom middleware, and service layers all logging the same events
2. **No Clear Boundaries**: Undefined responsibilities about which layer should log what information
3. **Context Propagation Issues**: Same context being logged at multiple levels without coordination
4. **Cascading Log Calls**: Parent and child functions both logging the same operations
5. **Missing Request Orchestration**: No single point of truth for request-level logging

## Architectural Solution

### Core Principles

1. **Single Responsibility Logging**: Each layer has exclusive logging responsibilities
2. **Request Coordinator Pattern**: One coordinator per request manages all logging context
3. **Boundary-Driven Logging**: Clear rules about what each layer logs
4. **Context Inheritance**: Child operations inherit but don't duplicate parent context
5. **Performance First**: Minimal overhead through lazy evaluation and smart buffering

### Unified Request Coordination

```python
# faultmaven/infrastructure/logging/coordinator.py

from contextvars import ContextVar
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
import uuid

@dataclass
class RequestContext:
    """Single source of truth for request-scoped data"""
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    investigation_id: Optional[str] = None
    agent_phase: Optional[str] = None
    start_time: datetime = field(default_factory=datetime.utcnow)
    attributes: Dict[str, Any] = field(default_factory=dict)
    logged_operations: set = field(default_factory=set)  # Prevents duplicates
    error_context: Optional['ErrorContext'] = None  # Track errors across layers
    performance_tracker: Optional['PerformanceTracker'] = None  # Track performance
    
    def has_logged(self, operation_key: str) -> bool:
        """Check if an operation has already been logged"""
        return operation_key in self.logged_operations
    
    def mark_logged(self, operation_key: str):
        """Mark an operation as logged"""
        self.logged_operations.add(operation_key)

@dataclass
class ErrorContext:
    """Track error context across layers to prevent cascade logging"""
    original_error: Optional[Exception] = None
    layer_errors: Dict[str, Dict] = field(default_factory=dict)
    recovery_attempts: int = 0
    
    def add_layer_error(self, layer: str, error: Exception):
        """Add error from specific layer"""
        self.layer_errors[layer] = {
            'error': str(error),
            'type': type(error).__name__,
            'timestamp': datetime.utcnow().isoformat()
        }
        if not self.original_error:
            self.original_error = error
    
    def should_log_error(self, layer: str) -> bool:
        """Determine if layer should log error (prevent cascade)"""
        # Only log at the first layer that catches it or during recovery
        return layer not in self.layer_errors or self.recovery_attempts > 0

class PerformanceTracker:
    """Track performance metrics across layers"""
    
    def __init__(self):
        self.layer_timings = {}
        self.thresholds = {
            'api': 0.1,      # 100ms
            'service': 0.5,   # 500ms
            'core': 0.3,      # 300ms
            'infrastructure': 1.0  # 1s
        }
    
    def record_timing(self, layer: str, operation: str, duration: float):
        """Record timing and return if it exceeds threshold"""
        key = f"{layer}.{operation}"
        self.layer_timings[key] = duration
        
        threshold = self.thresholds.get(layer, 1.0)
        return duration > threshold, threshold

# Thread-safe context variable
request_context: ContextVar[Optional[RequestContext]] = ContextVar('request_context', default=None)

class LoggingCoordinator:
    """Coordinates all logging for a request lifecycle"""
    
    def __init__(self):
        self.context = None
        
    def start_request(self, **initial_context) -> RequestContext:
        """Initialize request context - called ONCE per request"""
        self.context = RequestContext(**initial_context)
        self.context.error_context = ErrorContext()
        self.context.performance_tracker = PerformanceTracker()
        request_context.set(self.context)
        return self.context
    
    def end_request(self) -> Dict[str, Any]:
        """Finalize request - returns metrics for single summary log"""
        if not self.context:
            return {}
            
        duration = (datetime.utcnow() - self.context.start_time).total_seconds()
        summary = {
            'correlation_id': self.context.correlation_id,
            'duration_seconds': duration,
            'operations_logged': len(self.context.logged_operations),
            'errors_encountered': len(self.context.error_context.layer_errors) if self.context.error_context else 0,
            'performance_violations': sum(1 for k, v in self.context.performance_tracker.layer_timings.items() 
                                        if v > self.context.performance_tracker.thresholds.get(k.split('.')[0], 1.0))
                                     if self.context.performance_tracker else 0,
            **self.context.attributes
        }
        
        # Clear context
        request_context.set(None)
        self.context = None
        
        return summary
    
    @staticmethod
    def get_context() -> Optional[RequestContext]:
        """Get current request context"""
        return request_context.get()
    
    @staticmethod
    def log_once(operation_key: str, logger, level: str, message: str, **extra):
        """Log an operation only if it hasn't been logged yet"""
        ctx = request_context.get()
        if ctx and not ctx.has_logged(operation_key):
            getattr(logger, level)(message, extra=extra)
            ctx.mark_logged(operation_key)
```

### Layer-Specific Logging Boundaries

#### 1. API Layer (Middleware & Routes)
**Responsibility**: Request lifecycle, HTTP details, authentication

```python
# ONLY logs these events:
- Request received (method, path, headers)
- Authentication/authorization results
- Response sent (status, duration)
- Request validation errors
- HTTP-specific errors (404, 405, etc.)

# NEVER logs:
- Business logic operations
- Service layer details
- Database operations
- External API calls
```

#### 2. Service Layer
**Responsibility**: Business operations, orchestration, high-level flow

```python
# ONLY logs these events:
- Business operation started (with context)
- Major workflow transitions
- Business rule violations
- Operation results (success/failure)
- Performance metrics (if > threshold)

# NEVER logs:
- HTTP details
- Infrastructure details
- Individual tool calls
- Database queries
```

#### 3. Core Domain Layer
**Responsibility**: Domain events, state changes, agent phases

```python
# ONLY logs these events:
- Agent phase transitions
- Domain model state changes
- Business rule evaluations
- Hypothesis formulation/validation
- Critical decision points

# NEVER logs:
- HTTP concerns
- Infrastructure details
- Service orchestration
- External API details
```

#### 4. Infrastructure Layer
**Responsibility**: External integrations, technical operations

```python
# ONLY logs these events:
- External API calls (start/end)
- Database operations (if slow)
- Cache hits/misses
- Connection pool events
- Technical errors with full stack traces

# NEVER logs:
- Business logic
- Domain events
- HTTP details
- Service orchestration
```

### Enhanced Logging Configuration

```python
# faultmaven/infrastructure/logging/config.py

import logging
import json
from typing import Dict, Any, Optional
from pythonjsonlogger import jsonlogger
from opentelemetry import trace
import structlog

class FaultMavenLogger:
    """Enhanced logger with deduplication and structure"""
    
    def __init__(self):
        self.configure_structlog()
        
    def configure_structlog(self):
        """Configure structlog with processors"""
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                self.add_request_context,
                self.deduplicate_fields,
                self.add_trace_context,
                structlog.processors.JSONRenderer()
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    
    @staticmethod
    def add_request_context(logger, method_name, event_dict):
        """Add request context without duplication"""
        from faultmaven.infrastructure.logging.coordinator import request_context
        
        ctx = request_context.get()
        if ctx:
            # Only add if not already present
            if 'correlation_id' not in event_dict:
                event_dict['correlation_id'] = ctx.correlation_id
            if 'session_id' not in event_dict and ctx.session_id:
                event_dict['session_id'] = ctx.session_id
            if 'investigation_id' not in event_dict and ctx.investigation_id:
                event_dict['investigation_id'] = ctx.investigation_id
                
        return event_dict
    
    @staticmethod
    def deduplicate_fields(logger, method_name, event_dict):
        """Remove duplicate fields"""
        seen = set()
        deduped = {}
        
        for key, value in event_dict.items():
            if key not in seen:
                deduped[key] = value
                seen.add(key)
                
        return deduped
    
    @staticmethod
    def add_trace_context(logger, method_name, event_dict):
        """Add OpenTelemetry trace context"""
        span = trace.get_current_span()
        if span and span.is_recording():
            span_context = span.get_span_context()
            event_dict['trace_id'] = format(span_context.trace_id, '032x')
            event_dict['span_id'] = format(span_context.span_id, '016x')
            
        return event_dict

# Singleton configuration
_logger_config = None

def get_logger(name: str) -> structlog.BoundLogger:
    """Get a configured logger instance"""
    global _logger_config
    if _logger_config is None:
        _logger_config = FaultMavenLogger()
    
    return structlog.get_logger(name)
```

### Standardized Logging Patterns

#### Simplified UnifiedLogger Pattern
```python
# faultmaven/infrastructure/logging/unified.py

from contextlib import contextmanager
from time import time
from typing import Optional, Dict, Any
from faultmaven.infrastructure.logging.config import get_logger
from faultmaven.infrastructure.logging.coordinator import request_context, LoggingCoordinator

class UnifiedLogger:
    """Single unified logger that handles all concerns with simplicity"""
    
    def __init__(self, name: str, layer: str):
        """
        Initialize unified logger
        Args:
            name: Logger name (e.g., 'agent_service')
            layer: Layer type ('api', 'service', 'core', 'infrastructure')
        """
        self.logger = get_logger(name)
        self.layer = layer
        self.name = name
        
    def log_boundary(self, operation: str, direction: str = "entry", **context):
        """Log service boundary with automatic deduplication"""
        ctx = request_context.get()
        if not ctx:
            return
            
        key = f"{self.layer}_{operation}_{direction}"
        if not ctx.has_logged(key):
            level = "debug" if direction == "entry" else "info"
            message = f"{self.layer.title()} {direction}: {operation}"
            self.logger.log(level, message, operation=operation, layer=self.layer, **context)
            ctx.mark_logged(key)
    
    @contextmanager
    def operation(self, operation: str, **context):
        """Unified operation logging with timing and error handling"""
        start_time = time()
        ctx = request_context.get()
        
        # Log entry at debug level
        self.log_boundary(operation, "entry", **context)
        
        try:
            yield
            
            # Calculate duration
            duration = time() - start_time
            
            # Check performance threshold
            if ctx and ctx.performance_tracker:
                exceeds, threshold = ctx.performance_tracker.record_timing(
                    self.layer, operation, duration
                )
                
                # Only log completion if slow
                if exceeds:
                    self.logger.warning(
                        f"Slow {self.layer} operation: {operation}",
                        operation=operation,
                        duration_seconds=duration,
                        threshold=threshold,
                        layer=self.layer,
                        **context
                    )
            
            # Log exit
            self.log_boundary(operation, "exit", duration_ms=duration*1000)
            
        except Exception as e:
            duration = time() - start_time
            
            # Handle error with cascade prevention
            if ctx and ctx.error_context:
                if ctx.error_context.should_log_error(self.layer):
                    ctx.error_context.add_layer_error(self.layer, e)
                    self.logger.error(
                        f"{self.layer.title()} operation failed: {operation}",
                        operation=operation,
                        error=str(e),
                        error_type=type(e).__name__,
                        duration_seconds=duration,
                        layer=self.layer,
                        **context,
                        exc_info=True  # Include stack trace
                    )
            else:
                # Fallback if no context
                self.logger.error(
                    f"{self.layer.title()} operation failed: {operation}",
                    operation=operation,
                    error=str(e),
                    duration_seconds=duration,
                    layer=self.layer,
                    **context
                )
            raise
    
    def log_metric(self, metric: str, value: float, unit: str = "count"):
        """Log a metric with context"""
        ctx = request_context.get()
        if ctx:
            key = f"metric_{self.layer}_{metric}"
            if not ctx.has_logged(key):
                self.logger.info(
                    f"Metric: {metric}",
                    metric=metric,
                    value=value,
                    unit=unit,
                    layer=self.layer
                )
                ctx.mark_logged(key)
    
    def log_event(self, event: str, level: str = "info", **context):
        """Log a business or technical event"""
        ctx = request_context.get()
        if ctx:
            key = f"event_{self.layer}_{event}"
            LoggingCoordinator.log_once(
                key,
                self.logger,
                level,
                f"{self.layer.title()} event: {event}",
                event=event,
                layer=self.layer,
                **context
            )

# Factory function for easy creation
def get_unified_logger(name: str, layer: str) -> UnifiedLogger:
    """Get a unified logger instance"""
    return UnifiedLogger(name, layer)
```

#### Usage Examples with UnifiedLogger
```python
# Service layer example
class AgentService:
    def __init__(self):
        self.logger = get_unified_logger("agent_service", "service")
    
    async def process_query(self, query: str, session_id: str):
        # Simple, clean logging with automatic deduplication
        with self.logger.operation("process_query", session_id=session_id):
            # Business logic here
            self.logger.log_event("hypothesis_generated", level="info", 
                                hypothesis_count=3)
            
            result = await self._run_agent(query)
            
            self.logger.log_metric("tokens_used", result.token_count)
            return result

# Infrastructure layer example
class LLMRouter:
    def __init__(self):
        self.logger = get_unified_logger("llm_router", "infrastructure")
    
    async def route_request(self, prompt: str):
        with self.logger.operation("route_request", prompt_length=len(prompt)):
            # Only infrastructure concerns logged
            provider = self._select_provider()
            self.logger.log_event("provider_selected", provider=provider)
            
            response = await provider.complete(prompt)
            self.logger.log_metric("api_latency", response.latency, "seconds")
            
            return response
```

### Updated Application Architecture

#### 1. Middleware Configuration
```python
# faultmaven/api/middleware/logging.py

from fastapi import Request, Response
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator
from faultmaven.infrastructure.logging.config import get_logger

logger = get_logger("api.middleware")

class LoggingMiddleware:
    """Single middleware for request logging"""
    
    def __init__(self, app):
        self.app = app
        self.coordinator = LoggingCoordinator()
    
    async def __call__(self, request: Request, call_next):
        # Start request coordination
        context = self.coordinator.start_request(
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host
        )
        
        # Log request (ONLY HERE, not in routes)
        logger.info(
            "Request received",
            method=request.method,
            path=request.url.path,
            correlation_id=context.correlation_id
        )
        
        try:
            response = await call_next(request)
            
            # Log response (ONLY HERE)
            summary = self.coordinator.end_request()
            logger.info(
                "Request completed",
                status_code=response.status_code,
                **summary
            )
            
            return response
            
        except Exception as e:
            # Log error (ONLY HERE for HTTP errors)
            summary = self.coordinator.end_request()
            logger.error(
                "Request failed",
                error=str(e),
                **summary
            )
            raise
```

#### 2. Service Layer Configuration (Simplified)
```python
# faultmaven/services/base_service.py

from faultmaven.infrastructure.logging.unified import get_unified_logger

class BaseService:
    """Base class for all services with simplified unified logging"""
    
    def __init__(self, service_name: str):
        self.service_name = service_name
        self.logger = get_unified_logger(service_name, "service")
    
    async def execute_operation(self, operation_name: str, func, **kwargs):
        """Execute service operation with unified logging"""
        with self.logger.operation(operation_name, **kwargs):
            result = await func(**kwargs)
            return result
```

#### 3. Infrastructure Layer Configuration (Simplified)
```python
# faultmaven/infrastructure/base_client.py

from faultmaven.infrastructure.logging.unified import get_unified_logger

class BaseExternalClient:
    """Base class for external service clients with simplified logging"""
    
    def __init__(self, client_name: str):
        self.client_name = client_name
        self.logger = get_unified_logger(client_name, "infrastructure")
    
    async def call_external(self, operation: str, func, **kwargs):
        """Call external service with unified infrastructure logging"""
        with self.logger.operation(operation, client=self.client_name):
            result = await func(**kwargs)
            return result
```

## Implementation Action Plan

### Phase 1: Core Infrastructure (Day 1-2)

#### Task 1.1: Create Logging Coordinator
```bash
# Create directory structure
mkdir -p faultmaven/infrastructure/logging

# Create coordinator module
touch faultmaven/infrastructure/logging/__init__.py
touch faultmaven/infrastructure/logging/coordinator.py
```

**Implementation Steps:**
1. Copy the `RequestContext` dataclass from this document
2. Implement the `LoggingCoordinator` class with deduplication logic
3. Add context variable management
4. Create unit tests for coordinator

#### Task 1.2: Install Dependencies
```bash
# Add to requirements.txt
echo "structlog==24.1.0" >> requirements.txt
echo "python-json-logger==2.0.7" >> requirements.txt
pip install structlog python-json-logger
```

#### Task 1.3: Create Enhanced Configuration
```bash
# Create configuration module
touch faultmaven/infrastructure/logging/config.py
```

**Implementation Steps:**
1. Implement `FaultMavenLogger` class with structlog
2. Add deduplication processor
3. Add request context processor
4. Create logger factory function

### Phase 2: Unified Logger Implementation (Day 3-4)

#### Task 2.1: Implement UnifiedLogger
```bash
# Create unified logger module
touch faultmaven/infrastructure/logging/unified.py
```

**Implementation Steps:**
1. Implement `UnifiedLogger` class with all logging methods
2. Add `ErrorContext` for cascade prevention
3. Add `PerformanceTracker` for automatic monitoring
4. Create comprehensive unit tests

#### Task 2.2: Create Base Classes
```bash
# Update service base class
touch faultmaven/services/base_service.py

# Update infrastructure base class
touch faultmaven/infrastructure/base_client.py
```

**Implementation Steps:**
1. Create `BaseService` with boundary and operation logging
2. Create `BaseExternalClient` with metric logging
3. Add proper error handling and deduplication

### Phase 3: Middleware Update (Day 5)

#### Task 3.1: Replace Existing Middleware
```bash
# Update middleware
vi faultmaven/api/middleware/logging.py
```

**Implementation Steps:**
1. Remove ALL existing logging middleware
2. Implement single `LoggingMiddleware` class
3. Integrate with `LoggingCoordinator`
4. Update `main.py` to use new middleware

#### Task 3.2: Update FastAPI Configuration
```python
# main.py updates
from faultmaven.api.middleware.logging import LoggingMiddleware

app = FastAPI()

# Remove all other logging middleware
app.add_middleware(LoggingMiddleware)
```

### Phase 4: Service Migration (Day 6-8)

#### Task 4.1: Migrate Agent Service
```bash
# Update agent service
vi faultmaven/services/agent_service.py
```

**Implementation Steps:**
1. Inherit from `BaseService`
2. Remove duplicate logging calls
3. Use `execute_operation` for main methods
4. Update tests

#### Task 4.2: Migrate Other Services
**Services to update:**
- `SessionService`
- `DataService`
- `KnowledgeService`

**For each service:**
1. Inherit from `BaseService`
2. Remove all request-level logging
3. Keep only business operation logging
4. Use operation logger for workflows

### Phase 5: Infrastructure Migration (Day 9-10)

#### Task 5.1: Update LLM Router
```bash
# Update LLM router
vi faultmaven/infrastructure/llm/router.py
```

**Implementation Steps:**
1. Inherit from `BaseExternalClient`
2. Remove business logic logging
3. Keep only external API call logging
4. Add metric collection

#### Task 5.2: Update Other Infrastructure
**Components to update:**
- Redis client
- ChromaDB client
- Presidio client
- OpenTelemetry integration

### Phase 6: Testing and Validation (Day 11-12)

#### Task 6.1: Create Integration Tests
```python
# tests/infrastructure/test_logging_deduplication.py

import pytest
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator

@pytest.mark.asyncio
async def test_no_duplicate_logs():
    """Verify no duplicate log entries"""
    coordinator = LoggingCoordinator()
    context = coordinator.start_request()
    
    # Simulate multiple log attempts
    from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator
    
    # This should log
    LoggingCoordinator.log_once("test_op", logger, "info", "First log")
    
    # This should NOT log (duplicate)
    LoggingCoordinator.log_once("test_op", logger, "info", "Duplicate log")
    
    # Verify only one operation logged
    assert len(context.logged_operations) == 1
```

#### Task 6.2: Performance Testing
```python
# tests/infrastructure/test_logging_performance.py

import time
import pytest

@pytest.mark.benchmark
def test_logging_overhead():
    """Ensure logging adds < 1% overhead"""
    # Measure operation without logging
    # Measure operation with logging
    # Assert overhead < 1%
```

### Phase 7: Configuration and Documentation (Day 13)

#### Task 7.1: Update Environment Configuration
```bash
# Update .env.example
cat >> .env.example << EOF

# Logging Configuration
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_DEDUPE=true
LOG_BUFFER_SIZE=100
LOG_FLUSH_INTERVAL=5
EOF
```

#### Task 7.2: Create Runbook
```markdown
# Logging Runbook

## Viewing Logs
- Development: `tail -f logs/faultmaven.log | jq`
- Production: Use log aggregation system

## Common Patterns
- Filter by correlation_id: `jq 'select(.correlation_id=="xxx")'`
- View errors only: `jq 'select(.level=="ERROR")'`
- Track request flow: `jq 'select(.correlation_id=="xxx")' | jq '.message'`

## Troubleshooting
- Missing logs: Check LOG_LEVEL environment variable
- Duplicate logs: Verify single middleware registration
- Performance issues: Check LOG_BUFFER_SIZE and flush settings
```

### Phase 8: Rollout Strategy (Day 14)

#### Step 1: Development Environment
1. Deploy to development
2. Monitor for 24 hours
3. Verify no duplicate logs
4. Check performance metrics

#### Step 2: Staging Environment
1. Deploy to staging
2. Run load tests
3. Verify log aggregation works
4. Check alerting integration

#### Step 3: Production Rollout
1. Deploy during low-traffic window
2. Monitor error rates
3. Verify observability dashboards
4. Document any issues

## Success Metrics

### Quantitative Metrics
- **Zero duplicate log entries** (measured by correlation_id + message hash)
- **< 1% performance overhead** (measured by request latency)
- **100% request traceability** (every request has correlation_id)
- **< 10MB/hour log volume** (efficient logging)

### Qualitative Metrics
- **Clear troubleshooting path** (can trace request through all layers)
- **Actionable error messages** (includes context and remediation)
- **Consistent log structure** (all logs follow schema)
- **No sensitive data leakage** (PII redaction working)

## Monitoring Dashboard

### Key Metrics to Track
```yaml
panels:
  - name: "Request Rate"
    query: "count(level='INFO' AND message='Request received')"
    
  - name: "Error Rate"
    query: "count(level='ERROR') / count(level='INFO')"
    
  - name: "Average Latency"
    query: "avg(duration_seconds WHERE message='Request completed')"
    
  - name: "Duplicate Logs"
    query: "count(GROUP BY correlation_id, message HAVING count > 1)"
    
  - name: "Log Volume"
    query: "sum(bytes) GROUP BY hour"
```

## Configuration Templates

### Development Configuration
```python
LOGGING_CONFIG = {
    "level": "DEBUG",
    "format": "json",
    "dedupe": True,
    "buffer_size": 10,
    "flush_interval": 1,
    "include_trace": True,
    "include_source": True
}
```

### Production Configuration
```python
LOGGING_CONFIG = {
    "level": "INFO",
    "format": "json",
    "dedupe": True,
    "buffer_size": 1000,
    "flush_interval": 10,
    "include_trace": True,
    "include_source": False,
    "sampling": {
        "debug": 0.01,  # Sample 1% of debug logs
        "info": 1.0,     # Log all info
        "error": 1.0     # Log all errors
    }
}
```

## Troubleshooting Guide

### Issue: Still Seeing Duplicate Logs

**Diagnosis Steps:**
1. Check middleware registration: `grep -r "add_middleware" main.py`
2. Verify single coordinator: `grep -r "LoggingCoordinator()" .`
3. Check for multiple logger calls in same function
4. Verify deduplication is enabled: `LOG_DEDUPE=true`

**Resolution:**
- Remove extra middleware
- Use `log_once` for repeated operations
- Enable deduplication in config

### Issue: Missing Correlation IDs

**Diagnosis Steps:**
1. Verify middleware is first in chain
2. Check context propagation: `request_context.get()`
3. Verify async context handling

**Resolution:**
- Move LoggingMiddleware to first position
- Use contextvars properly in async code
- Check for context clearing issues

### Issue: High Log Volume

**Diagnosis Steps:**
1. Check log level: `echo $LOG_LEVEL`
2. Review buffering settings
3. Check for log loops

**Resolution:**
- Increase buffer size
- Enable sampling for high-volume logs
- Fix any recursive logging

## Conclusion

This improved logging strategy eliminates duplicate entries through:

1. **Unified Request Coordination**: Single coordinator manages all request logging
2. **Clear Layer Boundaries**: Each layer has exclusive logging responsibilities
3. **Deduplication Mechanisms**: Prevents same operation from being logged multiple times
4. **Standardized Patterns**: Consistent logging patterns across the application
5. **Performance Optimization**: Buffering and lazy evaluation minimize overhead

The implementation plan provides a step-by-step approach that any developer can follow to achieve production-ready logging with excellent observability and zero duplication.