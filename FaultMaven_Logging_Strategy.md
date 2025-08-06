# FaultMaven Logging Strategy

## Executive Summary

This document outlines a comprehensive logging strategy for the FaultMaven backend application. The strategy addresses current gaps in the logging implementation and establishes standards for structured, contextual, and production-grade logging that enables effective monitoring, debugging, and observability.

## Current State Assessment

### Existing Implementation

1. **Partial Infrastructure**: 
   - A sophisticated logging configuration exists (`infrastructure/logging_config.py`) with:
     - Structured JSON logging capability
     - Request correlation IDs
     - Environment-based configuration
     - Log level management
   - Request logging middleware with correlation tracking

2. **Inconsistent Usage**:
   - Mixed approaches: Some modules use the enhanced logging, while others use basic `logging.basicConfig()`
   - Example: `main.py` uses basic configuration instead of the enhanced system
   - Inconsistent logger initialization patterns across modules

3. **Limited Context Capture**:
   - Basic log messages with minimal contextual information
   - Missing critical business context (session_id, agent_phase, user_id)
   - No standardized extra fields for structured data

4. **Incomplete Error Handling**:
   - Exceptions logged without full stack traces in many places
   - Generic `except:` blocks without proper error context
   - Missing error categorization and severity levels

### Key Shortcomings

1. **Underutilized Infrastructure**: The existing enhanced logging system is not being used consistently
2. **Missing Business Context**: Logs lack essential troubleshooting context
3. **No Log Schema**: Absence of standardized log structure across the application
4. **Limited Observability**: Insufficient integration with distributed tracing
5. **Poor Error Details**: Exception handling doesn't capture full debugging information

## Proposed Logging Strategy

### Core Principles

1. **Structured First**: All logs must be machine-readable JSON with consistent schema
2. **Context Rich**: Every log must include relevant business and technical context
3. **Performance Aware**: Logging should not impact application performance
4. **Security Conscious**: Never log sensitive data (PII, credentials, tokens)
5. **Actionable**: Logs should enable quick problem identification and resolution

### Technical Architecture

#### 1. Tooling Stack

- **Primary Logger**: Python's built-in `logging` module (already in use)
- **Formatter**: `python-json-logger` for consistent JSON output
- **Context Management**: `contextvars` for request/session correlation
- **Performance**: `logging` with lazy evaluation and appropriate levels
- **Integration**: OpenTelemetry for trace correlation

#### 2. Centralized Configuration

Enhance the existing `infrastructure/logging_config.py`:

```python
# Enhanced configuration with OpenTelemetry integration
import logging
import json
from pythonjsonlogger import jsonlogger
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from contextvars import ContextVar

# Context variables for request-scoped data
request_context: ContextVar[Dict[str, Any]] = ContextVar('request_context', default={})

class FaultMavenFormatter(jsonlogger.JsonFormatter):
    """Enhanced JSON formatter with FaultMaven-specific fields"""
    
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        
        # Add timestamp in ISO format
        log_record['timestamp'] = datetime.utcnow().isoformat()
        
        # Add service metadata
        log_record['service'] = 'faultmaven-api'
        log_record['environment'] = os.getenv('ENVIRONMENT', 'development')
        log_record['version'] = os.getenv('APP_VERSION', '1.0.0')
        
        # Add context from contextvars
        ctx = request_context.get()
        if ctx:
            log_record.update({
                'correlation_id': ctx.get('correlation_id'),
                'session_id': ctx.get('session_id'),
                'user_id': ctx.get('user_id'),
                'agent_phase': ctx.get('agent_phase'),
                'investigation_id': ctx.get('investigation_id')
            })
        
        # Add OpenTelemetry trace context
        span = trace.get_current_span()
        if span and span.is_recording():
            span_context = span.get_span_context()
            log_record['trace_id'] = format(span_context.trace_id, '032x')
            log_record['span_id'] = format(span_context.span_id, '016x')
```

#### 3. Standard Log Schema

```json
{
  "timestamp": "2024-01-15T10:30:45.123Z",
  "level": "INFO",
  "logger": "faultmaven.services.agent_service",
  "message": "Processing troubleshooting query",
  
  // Service metadata
  "service": "faultmaven-api",
  "environment": "production",
  "version": "1.0.0",
  "hostname": "faultmaven-api-7d9f8b-xyz",
  
  // Request context
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "session_123456",
  "user_id": "user_789",
  
  // Business context
  "agent_phase": "validate_hypothesis",
  "investigation_id": "inv_987654",
  "data_type": "kubernetes_logs",
  "query_complexity": "high",
  
  // Technical context
  "module": "agent_service",
  "function": "process_query",
  "line": 74,
  
  // Performance metrics
  "duration_ms": 1234,
  "tokens_used": 500,
  "cache_hit": false,
  
  // Tracing
  "trace_id": "7d9f8b6c5e4a3b2c1d0e9f8a7b6c5d4e",
  "span_id": "3b2c1d0e9f8a7b6c",
  
  // Error details (when applicable)
  "error": {
    "type": "ValidationError",
    "message": "Invalid session state",
    "stack_trace": "...",
    "error_code": "SESSION_EXPIRED"
  }
}
```

#### 4. Logger Initialization Pattern

```python
# Standard pattern for all modules
from faultmaven.infrastructure.logging_config import get_logger, LogContext

logger = get_logger(__name__)

class SomeService:
    def __init__(self):
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")
    
    async def process_something(self, session_id: str):
        with LogContext(self.logger, "process_something", 
                       session_id=session_id) as ctx:
            # Automatic start/end logging with duration
            result = await self._do_processing()
            ctx.logger.info("Processing milestone reached", 
                          extra={"milestone": "data_validated"})
            return result
```

### Implementation Plan

#### Phase 1: Infrastructure Enhancement (Week 1)

1. **Update logging_config.py**:
   - Add `python-json-logger` dependency
   - Implement FaultMavenFormatter with full schema
   - Add OpenTelemetry integration
   - Create context management utilities

2. **Create logging standards module**:
   - Error code definitions
   - Log level guidelines
   - Context field standards

#### Phase 2: Core Module Migration (Week 2)

1. **Update main.py**:
   - Replace `basicConfig` with enhanced setup
   - Add startup configuration logging
   - Implement graceful shutdown logging

2. **Update all service modules**:
   - Agent Service
   - Session Service
   - Knowledge Service
   - Data Service

3. **Update infrastructure modules**:
   - LLM Router
   - Redis Client
   - Security/Redaction

#### Phase 3: API Layer Enhancement (Week 3)

1. **Enhance request middleware**:
   - Add business context extraction
   - Implement performance logging
   - Add error categorization

2. **Update all API routes**:
   - Standardize error responses
   - Add operation-specific context
   - Implement audit logging

#### Phase 4: Testing and Validation (Week 4)

1. **Add logging tests**:
   - Verify log schema compliance
   - Test context propagation
   - Validate error logging

2. **Integration testing**:
   - Test with log aggregation systems
   - Verify performance impact
   - Validate security compliance

### Configuration Guidelines

#### Environment Variables

```bash
# Logging configuration
LOG_LEVEL=INFO                    # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FORMAT=json                    # json or text
LOG_FILE=/var/log/faultmaven.log  # Optional file output
LOG_SAMPLE_RATE=1.0               # Sampling rate for high-volume logs

# Feature flags
LOG_ENABLE_TRACING=true           # OpenTelemetry integration
LOG_ENABLE_METRICS=true           # Performance metrics in logs
LOG_ENABLE_AUDIT=true             # Audit trail logging
```

#### Log Levels Usage

- **DEBUG**: Detailed diagnostic information, disabled in production
- **INFO**: General operational messages, key business events
- **WARNING**: Recoverable issues, degraded functionality
- **ERROR**: Unrecoverable errors requiring attention
- **CRITICAL**: System failures requiring immediate action

### Security Considerations

1. **Never log**:
   - API keys, tokens, or credentials
   - Full request/response bodies with potential PII
   - Internal system paths or configurations
   - Raw user input without sanitization

2. **Always sanitize**:
   - User queries through DataSanitizer
   - File paths to relative format
   - URLs to remove credentials
   - Error messages to remove sensitive details

### Performance Guidelines

1. **Use lazy evaluation**:
   ```python
   # Good
   logger.debug("Processing %d items", len(items))
   
   # Bad
   logger.debug(f"Processing {len(items)} items")
   ```

2. **Implement sampling for high-volume logs**:
   ```python
   if should_sample(rate=0.1):  # Log 10% of high-volume events
       logger.info("High frequency event", extra={...})
   ```

3. **Batch context updates**:
   ```python
   # Update context once per request, not per operation
   set_request_context({
       'session_id': session_id,
       'user_id': user_id,
       'correlation_id': correlation_id
   })
   ```

### Monitoring and Alerting

1. **Key Metrics from Logs**:
   - Error rates by error_code
   - Response times by operation
   - Token usage by user/session
   - Cache hit rates
   - Agent phase transitions

2. **Alert Conditions**:
   - ERROR level logs spike
   - Specific error_codes frequency
   - Performance degradation patterns
   - Security-related warnings

### Migration Checklist

- [ ] Install `python-json-logger` dependency
- [ ] Update `logging_config.py` with new formatter
- [ ] Create context management utilities
- [ ] Update `main.py` to use enhanced logging
- [ ] Migrate all service modules
- [ ] Update API routes with context
- [ ] Add logging tests
- [ ] Update documentation
- [ ] Configure log aggregation
- [ ] Set up monitoring dashboards

### Success Criteria

1. **100% structured JSON logs** in production
2. **Full context availability** for troubleshooting
3. **< 1% performance impact** from logging
4. **Zero sensitive data** in logs
5. **Complete error traceability** with stack traces
6. **Correlation across services** via trace IDs

## Conclusion

This logging strategy transforms FaultMaven's logging from basic text output to a comprehensive observability foundation. By leveraging the existing infrastructure and enhancing it with consistent patterns, rich context, and structured data, the system will provide the visibility needed for effective monitoring, debugging, and operational excellence.

The phased implementation approach ensures minimal disruption while systematically improving logging across the entire application. The result will be a production-grade logging system that enables rapid troubleshooting and maintains the high standards expected of an AI-powered troubleshooting platform.