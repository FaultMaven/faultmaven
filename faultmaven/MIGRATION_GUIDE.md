# FaultMaven Logging Migration Guide

## Overview

This guide outlines the migration from the old logging system to the new unified logging approach that eliminates duplicate log entries and provides enhanced observability.

## What Changed

### **Before (Problematic)**
- Multiple middleware components logging the same events
- Inconsistent logger initialization patterns
- Duplicate correlation IDs for the same request
- Mixed logging approaches across modules

### **After (Improved)**
- Single unified request coordinator
- Consistent logger initialization via `get_logger()`
- Shared correlation IDs across all middleware
- Standardized business context logging

## Migration Steps

### **1. Update Logger Initialization**

**Before:**
```python
import logging
logger = logging.getLogger(__name__)
```

**After:**
```python
from faultmaven.infrastructure.logging_config import get_logger
logger = get_logger(__name__)
```

### **2. Replace Basic Logging with Context Managers**

**Before:**
```python
logger.info(f"Processing query for session {session_id}")
try:
    result = await process_query(query)
    logger.info(f"Query processed successfully")
except Exception as e:
    logger.error(f"Query processing failed: {e}")
```

**After:**
```python
from faultmaven.infrastructure.logging_config import BusinessLogContext

with BusinessLogContext(logger, "process_query", session_id=session_id) as ctx:
    result = await process_query(query)
    # Automatic start/end logging with duration and context
```

### **3. Use Standardized Error Context**

**Before:**
```python
logger.error(f"Session not found: {session_id}")
```

**After:**
```python
from faultmaven.infrastructure.logging_standards import LoggingStandards, ErrorCode

error_context = LoggingStandards.get_error_context(
    ErrorCode.SESSION_NOT_FOUND,
    f"Session not found: {session_id}",
    {"session_id": session_id}
)
logger.error("Session not found", extra=error_context)
```

### **4. Add Business Context to Logs**

**Before:**
```python
logger.info("Agent processing started")
```

**After:**
```python
business_context = LoggingStandards.get_business_context(
    session_id=session_id,
    user_id=user_id,
    agent_phase="validate_hypothesis"
)
logger.info("Agent processing started", extra=business_context)
```

## File-by-File Migration

### **API Routes**

**Files to update:**
- `faultmaven/api/v1/routes/session.py` ✅ (Already updated)
- `faultmaven/api/v1/routes/agent.py`
- `faultmaven/api/v1/routes/data.py`
- `faultmaven/api/v1/routes/knowledge.py`

**Pattern:**
```python
# Replace direct logging with BusinessLogContext
with BusinessLogContext(logger, "operation_name", session_id=session_id) as ctx:
    # Operation logic here
    # Automatic logging with context
```

### **Service Modules**

**Files to update:**
- `faultmaven/services/agent_service.py`
- `faultmaven/services/data_service.py`
- `faultmaven/services/knowledge_service.py`

**Pattern:**
```python
# Use enhanced logging with business context
from faultmaven.infrastructure.logging_config import get_logger, BusinessLogContext
from faultmaven.infrastructure.logging_standards import LoggingStandards

logger = get_logger(__name__)

class SomeService:
    def __init__(self):
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")
    
    async def process_something(self, session_id: str):
        with BusinessLogContext(self.logger, "process_something", session_id=session_id) as ctx:
            # Business logic here
            return result
```

### **Infrastructure Modules**

**Files to update:**
- `faultmaven/infrastructure/redis_client.py`
- `faultmaven/infrastructure/llm/router.py`
- `faultmaven/infrastructure/security/redaction.py`

**Pattern:**
```python
# Use LogContext for technical operations
from faultmaven.infrastructure.logging_config import get_logger, LogContext

logger = get_logger(__name__)

with LogContext(logger, "redis_operation", operation="get", key=key) as ctx:
    result = await redis_client.get(key)
    return result
```

## Configuration Updates

### **Environment Variables**

**Add to `.env`:**
```bash
# Enhanced logging configuration
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_ENABLE_TRACING=true
LOG_ENABLE_METRICS=true
LOG_ENABLE_AUDIT=true
```

### **Dependencies**

**Add to `requirements.txt`:**
```
python-json-logger>=2.0.0
```

## Testing the Migration

### **1. Verify No Duplicate Logs**

Start the application and check for duplicate log entries:
```bash
# Start the application
python -m faultmaven.main

# In another terminal, make a request
curl -X POST http://localhost:8000/api/v1/sessions/abc/heartbeat

# Check logs - should see only one "Request started" and one "Request completed"
```

### **2. Verify Enhanced Context**

Check that logs include business context:
```json
{
  "timestamp": "2024-01-15T10:30:45.123Z",
  "level": "INFO",
  "message": "Business operation started: session_heartbeat",
  "session_id": "abc",
  "correlation_id": "a1b2c3d4",
  "service": "faultmaven-api",
  "environment": "development"
}
```

### **3. Test Error Logging**

Verify standardized error codes:
```json
{
  "timestamp": "2024-01-15T10:30:45.123Z",
  "level": "ERROR",
  "message": "Session not found",
  "error_code": "SESSION_001",
  "error_type": "SESSION_NOT_FOUND",
  "session_id": "invalid_session"
}
```

## Rollback Plan

If issues arise during migration:

### **1. Revert Middleware Changes**
```python
# In main.py, temporarily revert to old middleware
from .infrastructure.request_logging import RequestLoggingMiddleware
app.add_middleware(RequestLoggingMiddleware)
```

### **2. Revert Logger Initialization**
```python
# Temporarily use old logger pattern
import logging
logger = logging.getLogger(__name__)
```

### **3. Disable Enhanced Features**
```bash
# Set environment variables to disable enhanced features
LOG_ENABLE_TRACING=false
LOG_ENABLE_METRICS=false
```

## Performance Impact

### **Expected Changes**
- **Memory**: +2-5% due to context management
- **CPU**: +1-3% due to enhanced formatting
- **Log Volume**: -10-20% due to deduplication
- **Query Performance**: No impact on database operations

### **Monitoring**
Monitor these metrics during migration:
- Request duration
- Memory usage
- Log file size growth
- Error rates

## Success Criteria

### **Immediate (Week 1)**
- [ ] No duplicate log entries
- [ ] All routes use `get_logger()`
- [ ] Consistent correlation IDs
- [ ] Enhanced JSON logging working

### **Short-term (Week 2)**
- [ ] All service modules migrated
- [ ] Standardized error codes implemented
- [ ] Business context added to key operations
- [ ] Performance monitoring in place

### **Long-term (Week 3-4)**
- [ ] Log aggregation system integration
- [ ] Alerting based on log patterns
- [ ] Dashboard for log analytics
- [ ] Automated log analysis

## Troubleshooting

### **Common Issues**

**1. Duplicate logs still appearing**
- Check middleware order in `main.py`
- Verify `UnifiedRequestMiddleware` is being used
- Check for multiple logger instances

**2. Missing correlation IDs**
- Ensure `set_request_id()` is called in middleware
- Check that `CorrelationFilter` is applied to handlers

**3. Performance degradation**
- Monitor log level settings
- Check for excessive debug logging
- Verify lazy evaluation is used

**4. Context not propagating**
- Check `contextvars` usage
- Verify request state is being set correctly
- Ensure async context is handled properly

### **Debug Commands**

```bash
# Check current log level
echo $LOG_LEVEL

# Verify middleware stack
curl -v http://localhost:8000/health

# Test correlation ID propagation
curl -H "X-Correlation-ID: test123" http://localhost:8000/health

# Check log format
tail -f /var/log/faultmaven.log | jq .
```

## Support

For issues during migration:
1. Check this guide first
2. Review the logging standards in `logging_standards.py`
3. Examine the unified coordinator in `request_coordinator.py`
4. Test with the provided examples
5. Contact the development team if issues persist 