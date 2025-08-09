# Phase 2 FaultMaven Logging Usage Guide

This document explains how to use the Phase 2 FaultMaven logging components that provide unified logging patterns across all application layers.

## Overview

Phase 2 builds on the Phase 1 logging infrastructure to provide:

- **UnifiedLogger**: Consistent logging interface with operation context managers and boundary logging
- **BaseService**: Base class for service layer components with unified logging
- **BaseExternalClient**: Base class for infrastructure clients with circuit breaker patterns

## Quick Start

### 1. Service Layer Components

For service layer classes, inherit from `BaseService`:

```python
from faultmaven.services.base_service import BaseService
from typing import Dict, Any

class UserService(BaseService):
    def __init__(self):
        super().__init__("user_service")  # Service name for logging
    
    async def create_user(self, user_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new user with unified logging."""
        
        def validate_user_data(data: Dict[str, Any]) -> None:
            if not data.get("email"):
                raise ValueError("Email is required")
        
        def transform_response(user: Dict[str, Any]) -> Dict[str, Any]:
            # Remove sensitive fields from response
            return {k: v for k, v in user.items() if k != "password_hash"}
        
        return await self.execute_operation(
            "create_user",              # operation name
            self._internal_create_user, # operation function
            user_data,                  # positional args
            validate_inputs=validate_user_data,
            transform_result=transform_response
        )
    
    async def _internal_create_user(self, user_data: Dict[str, Any]) -> Dict[str, Any]:
        # Actual user creation logic
        user_id = generate_user_id()
        
        # Log metrics and business events
        self.log_metric("users_created", 1, "count", {"source": "api"})
        self.log_business_event("user_created", data={"user_id": user_id})
        
        return {"user_id": user_id, **user_data}
```

### 2. Infrastructure Layer Components

For external service clients, inherit from `BaseExternalClient`:

```python
from faultmaven.infrastructure.base_client import BaseExternalClient
from typing import Dict, Any

class DatabaseClient(BaseExternalClient):
    def __init__(self):
        super().__init__(
            client_name="database_client",
            service_name="PostgreSQL",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30
        )
    
    async def query_user(self, user_id: str) -> Dict[str, Any]:
        """Query user with unified logging and circuit breaker protection."""
        
        async def db_query(user_id: str) -> Dict[str, Any]:
            # Actual database query logic
            async with self.connection_pool.acquire() as conn:
                return await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        
        def validate_response(result: Dict[str, Any]) -> bool:
            return result is not None
        
        return await self.call_external(
            "query_user",           # operation name
            db_query,               # external call function
            user_id,                # positional args
            timeout=10.0,           # 10 second timeout
            retries=2,              # retry twice on failure
            validate_response=validate_response
        )
```

### 3. Direct UnifiedLogger Usage

For other components that don't fit the base classes:

```python
from faultmaven.infrastructure.logging.unified import get_unified_logger

class CustomComponent:
    def __init__(self):
        # Get logger for appropriate layer
        self.logger = get_unified_logger("faultmaven.custom", "core")
    
    async def process_data(self, data):
        # Log service boundaries
        self.logger.log_boundary("process_data", "inbound", {"items": len(data)})
        
        # Use operation context manager
        async with self.logger.operation("data_processing", items_count=len(data)) as ctx:
            ctx["phase"] = "validation"
            # ... validation logic ...
            
            ctx["phase"] = "processing" 
            result = await self._process_items(data)
            
            ctx["result_count"] = len(result)
            ctx["phase"] = "completed"
        
        # Log outbound boundary
        self.logger.log_boundary("process_data", "outbound", {"success": True})
        
        return result
```

## Key Features

### 1. Automatic Deduplication

Operations are automatically deduplicated within a request context:

```python
# Within the same request, duplicate operations are logged only once
logger.log_boundary("user_lookup", "inbound", data)  # Logged
logger.log_boundary("user_lookup", "inbound", data)  # Skipped (duplicate)
```

### 2. Performance Tracking

Layer-specific performance thresholds with automatic violation detection:

- **API Layer**: 100ms threshold
- **Service Layer**: 500ms threshold  
- **Core Layer**: 300ms threshold
- **Infrastructure Layer**: 1000ms threshold

```python
# Automatic performance violation logging if operation exceeds threshold
async with service_logger.operation("slow_operation") as ctx:
    await asyncio.sleep(0.6)  # Will log performance violation warning
```

### 3. Error Cascade Prevention

Errors are only logged at the first layer that catches them:

```python
# Service layer logs the error first
service_logger.error("Service operation failed", error=exception)

# Infrastructure layer won't duplicate the same error
infrastructure_logger.error("Infrastructure error", error=exception)  # Skipped
```

### 4. Request Context Integration

Works seamlessly with Phase 1 request context:

```python
from faultmaven.infrastructure.logging import LoggingCoordinator

coordinator = LoggingCoordinator()
context = coordinator.start_request(session_id="session_123")

# All logging within this context will include correlation IDs
async with logger.operation("business_operation") as ctx:
    # Logs automatically include correlation_id, session_id, etc.
    pass

summary = coordinator.end_request()  # Get request summary
```

## Migration Guide

### From Existing Services

1. **Replace direct logging** with unified patterns:
   ```python
   # Before
   logger.info("Processing user data")
   
   # After  
   async with self.logger.operation("process_user_data") as ctx:
       ctx["user_count"] = len(users)
   ```

2. **Use service boundaries** for cross-service calls:
   ```python
   # Before
   result = await external_service.call()
   
   # After
   self.logger.log_boundary("external_call", "inbound")
   result = await external_service.call() 
   self.logger.log_boundary("external_call", "outbound", {"success": True})
   ```

3. **Replace manual error handling** with base class patterns:
   ```python
   # Before
   try:
       result = await process_data(data)
   except Exception as e:
       logger.error(f"Processing failed: {e}")
       raise
   
   # After
   result = await self.execute_operation("process_data", process_data, data)
   ```

## Best Practices

1. **Use appropriate base classes** for consistent patterns
2. **Leverage operation context managers** for timing and error handling
3. **Log service boundaries** for distributed tracing
4. **Use business events** for important state changes
5. **Include relevant context** in operation managers
6. **Let error cascade prevention** handle duplicate error logging

## Integration with Existing Code

Phase 2 components are fully backward compatible and integrate seamlessly with existing FaultMaven code. You can gradually migrate components to use the unified logging patterns without breaking existing functionality.