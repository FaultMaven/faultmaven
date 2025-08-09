# FaultMaven Logging Strategy - Developer Guide

## Overview

This guide provides comprehensive information for developers on how to use the new FaultMaven logging system effectively. It includes practical code examples, best practices, migration patterns, and troubleshooting guidance for all application layers.

## Table of Contents

1. [Quick Start](#quick-start)
2. [Layer-Specific Patterns](#layer-specific-patterns)
3. [Core Concepts](#core-concepts)
4. [Code Examples by Layer](#code-examples-by-layer)
5. [Migration Guide](#migration-guide)
6. [Best Practices](#best-practices)
7. [Common Patterns](#common-patterns)
8. [Troubleshooting](#troubleshooting)
9. [Testing Guidelines](#testing-guidelines)

## Quick Start

### Basic Setup

All logging components are automatically configured. Simply use the appropriate pattern for your layer:

```python
# Service Layer
from faultmaven.services.base_service import BaseService

class YourService(BaseService):
    def __init__(self):
        super().__init__("your_service")

# Infrastructure Layer  
from faultmaven.infrastructure.base_client import BaseExternalClient

class YourClient(BaseExternalClient):
    def __init__(self):
        super().__init__("your_client", "ExternalService")

# Direct Logging (API/Core layers)
from faultmaven.infrastructure.logging.unified import get_unified_logger

logger = get_unified_logger(__name__, "api")  # or "core"
```

### Essential Imports

```python
# Core logging components
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator
from faultmaven.infrastructure.logging.unified import get_unified_logger

# Base classes (inherit from these)
from faultmaven.services.base_service import BaseService
from faultmaven.infrastructure.base_client import BaseExternalClient

# For manual context management (advanced)
from faultmaven.infrastructure.logging.coordinator import request_context
```

## Layer-Specific Patterns

### API Layer Pattern

Use unified logger directly for request handling and endpoint operations:

```python
from fastapi import APIRouter, Depends
from faultmaven.infrastructure.logging.unified import get_unified_logger

router = APIRouter()
logger = get_unified_logger(__name__, "api")

@router.post("/sessions/{session_id}/query")
async def process_user_query(
    session_id: str,
    query_data: QueryRequest
):
    # Log API boundary - inbound
    logger.log_boundary(
        operation="process_user_query",
        direction="inbound", 
        data={
            "session_id": session_id,
            "query_type": query_data.query_type,
            "data_size": len(str(query_data))
        }
    )
    
    # Use operation context for timing and error handling
    async with logger.operation(
        "process_user_query",
        session_id=session_id,
        query_type=query_data.query_type
    ) as ctx:
        
        # Update context as operation progresses
        ctx["validation_phase"] = "started"
        await validate_query(query_data)
        ctx["validation_phase"] = "completed"
        
        # Call service layer
        ctx["service_call"] = "agent_service"
        result = await agent_service.process_query(session_id, query_data)
        ctx["result_items"] = len(result.get("items", []))
        
        # Log business event
        logger.log_event(
            event_type="business",
            event_name="user_query_processed",
            severity="info",
            data={
                "session_id": session_id,
                "query_type": query_data.query_type,
                "result_count": len(result.get("items", []))
            }
        )
    
    # Log API boundary - outbound
    logger.log_boundary(
        operation="process_user_query",
        direction="outbound",
        data={"success": True, "result_type": type(result).__name__}
    )
    
    return result
```

### Service Layer Pattern

Inherit from `BaseService` and use `execute_operation()`:

```python
from faultmaven.services.base_service import BaseService
from typing import Dict, Any, List

class AgentService(BaseService):
    def __init__(self, llm_provider, knowledge_service):
        super().__init__("agent_service")
        self.llm_provider = llm_provider
        self.knowledge_service = knowledge_service
    
    async def process_query(
        self, 
        session_id: str, 
        query_data: QueryRequest
    ) -> Dict[str, Any]:
        """Process user query through agent reasoning."""
        
        # Validation function
        def validate_query_inputs(session_id: str, query_data: QueryRequest) -> None:
            if not session_id:
                raise ValueError("Session ID is required")
            if not query_data.query_text:
                raise ValueError("Query text cannot be empty")
            if len(query_data.query_text) > 10000:
                raise ValueError("Query text too long")
        
        # Result transformation function
        def transform_agent_response(result: Dict[str, Any]) -> Dict[str, Any]:
            # Add response metadata
            result["response_timestamp"] = datetime.utcnow().isoformat()
            result["agent_version"] = "v2.1"
            return result
        
        # Execute with unified logging, validation, and error handling
        return await self.execute_operation(
            operation_name="process_query",
            operation_func=self._internal_process_query,
            session_id,
            query_data,
            validate_inputs=validate_query_inputs,
            transform_result=transform_agent_response,
            log_result=True
        )
    
    async def _internal_process_query(
        self, 
        session_id: str, 
        query_data: QueryRequest
    ) -> Dict[str, Any]:
        """Internal query processing implementation."""
        
        # Phase 1: Knowledge retrieval
        self.log_business_event(
            "agent_phase_started",
            data={"phase": "knowledge_retrieval", "session_id": session_id}
        )
        
        knowledge_results = await self.knowledge_service.search(
            query_data.query_text,
            session_id=session_id
        )
        
        self.log_metric(
            "knowledge_results_found",
            len(knowledge_results),
            "count",
            tags={"session_id": session_id}
        )
        
        # Phase 2: LLM reasoning
        self.log_business_event(
            "agent_phase_started", 
            data={"phase": "llm_reasoning", "session_id": session_id}
        )
        
        llm_response = await self.llm_provider.generate_response(
            query_data.query_text,
            context=knowledge_results,
            session_id=session_id
        )
        
        # Phase 3: Response synthesis
        response = {
            "answer": llm_response.text,
            "confidence": llm_response.confidence,
            "sources": [r["source"] for r in knowledge_results],
            "session_id": session_id
        }
        
        self.log_business_event(
            "query_processed",
            data={
                "session_id": session_id,
                "confidence": llm_response.confidence,
                "sources_used": len(knowledge_results)
            }
        )
        
        return response
```

### Core Layer Pattern

Use unified logger for domain logic and processing:

```python
from faultmaven.infrastructure.logging.unified import get_unified_logger
from faultmaven.core.processing.data_classifier import DataType

class DataProcessor:
    def __init__(self):
        self.logger = get_unified_logger(__name__, "core")
    
    async def classify_troubleshooting_data(
        self, 
        data_content: str,
        context: Dict[str, Any]
    ) -> DataType:
        """Classify troubleshooting data with comprehensive logging."""
        
        # Log processing boundary
        self.logger.log_boundary(
            operation="classify_data",
            direction="inbound",
            data={
                "content_size": len(data_content),
                "context_keys": list(context.keys())
            }
        )
        
        # Use operation context for detailed processing tracking
        async with self.logger.operation(
            "classify_troubleshooting_data",
            data_size=len(data_content),
            context_type=context.get("type", "unknown")
        ) as ctx:
            
            # Phase 1: Text preprocessing
            ctx["phase"] = "preprocessing"
            preprocessed_text = await self._preprocess_text(data_content)
            ctx["preprocessed_length"] = len(preprocessed_text)
            
            # Phase 2: Pattern analysis
            ctx["phase"] = "pattern_analysis"
            patterns = await self._analyze_patterns(preprocessed_text)
            ctx["patterns_found"] = len(patterns)
            
            # Phase 3: Classification
            ctx["phase"] = "classification"
            classification = await self._perform_classification(patterns, context)
            ctx["classification"] = classification.value
            ctx["confidence"] = classification.confidence
            
            # Log classification results
            self.logger.log_event(
                event_type="technical",
                event_name="data_classified",
                severity="info",
                data={
                    "classification": classification.value,
                    "confidence": classification.confidence,
                    "patterns_count": len(patterns)
                }
            )
            
            # Log performance metrics
            self.logger.log_metric(
                "data_classification_confidence",
                classification.confidence,
                "percentage",
                tags={
                    "data_type": classification.value,
                    "context_type": context.get("type", "unknown")
                }
            )
        
        # Log outbound boundary
        self.logger.log_boundary(
            operation="classify_data",
            direction="outbound",
            data={
                "classification": classification.value,
                "confidence": classification.confidence
            }
        )
        
        return classification

    async def _preprocess_text(self, text: str) -> str:
        """Internal preprocessing with sub-operation logging."""
        async with self.logger.operation("preprocess_text", text_length=len(text)) as ctx:
            # Normalize whitespace
            normalized = " ".join(text.split())
            ctx["normalization"] = "completed"
            
            # Remove sensitive patterns
            cleaned = self._remove_sensitive_patterns(normalized)
            ctx["sensitive_patterns_removed"] = len(normalized) - len(cleaned)
            
            return cleaned
```

### Infrastructure Layer Pattern

Inherit from `BaseExternalClient` and use `call_external()`:

```python
from faultmaven.infrastructure.base_client import BaseExternalClient
from typing import List, Dict, Any
import aioredis

class RedisSessionClient(BaseExternalClient):
    def __init__(self, redis_url: str):
        super().__init__(
            client_name="redis_session_client",
            service_name="Redis",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30
        )
        self.redis_url = redis_url
        self._connection = None
    
    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """Get session data with unified logging and circuit breaker."""
        
        async def redis_get_operation(session_id: str) -> Dict[str, Any]:
            """Internal Redis GET operation."""
            if not self._connection:
                self._connection = await aioredis.from_url(self.redis_url)
            
            session_data = await self._connection.hgetall(f"session:{session_id}")
            return {k.decode(): v.decode() for k, v in session_data.items()}
        
        def validate_session_response(session_data: Dict[str, Any]) -> bool:
            """Validate that session data is present and valid."""
            return bool(session_data) and "created_at" in session_data
        
        def transform_session_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
            """Transform raw Redis data to application format."""
            return {
                "session_id": session_id,
                "created_at": raw_data.get("created_at"),
                "last_activity": raw_data.get("last_activity"),
                "user_id": raw_data.get("user_id"),
                "context": json.loads(raw_data.get("context", "{}"))
            }
        
        # Execute with circuit breaker, retry, validation, and transformation
        return await self.call_external(
            operation_name="get_session",
            call_func=redis_get_operation,
            session_id,
            timeout=5.0,
            retries=2,
            retry_delay=1.0,
            validate_response=validate_session_response,
            transform_response=transform_session_data
        )
    
    async def set_session(
        self, 
        session_id: str, 
        session_data: Dict[str, Any],
        ttl: int = 3600
    ) -> bool:
        """Set session data with expiration."""
        
        async def redis_set_operation(
            session_id: str, 
            data: Dict[str, Any], 
            ttl: int
        ) -> bool:
            """Internal Redis SET operation with TTL."""
            if not self._connection:
                self._connection = await aioredis.from_url(self.redis_url)
            
            # Prepare data for Redis
            redis_data = {
                "created_at": data.get("created_at", datetime.utcnow().isoformat()),
                "last_activity": datetime.utcnow().isoformat(),
                "user_id": data.get("user_id", ""),
                "context": json.dumps(data.get("context", {}))
            }
            
            # Set with expiration
            await self._connection.hset(f"session:{session_id}", mapping=redis_data)
            await self._connection.expire(f"session:{session_id}", ttl)
            
            return True
        
        def validate_set_response(result: bool) -> bool:
            """Validate that set operation succeeded."""
            return result is True
        
        return await self.call_external(
            operation_name="set_session",
            call_func=redis_set_operation,
            session_id,
            session_data,
            ttl,
            timeout=5.0,
            retries=3,
            retry_delay=0.5,
            validate_response=validate_set_response
        )

    async def health_check(self) -> Dict[str, Any]:
        """Override health check with Redis-specific checks."""
        base_health = await super().health_check()
        
        try:
            # Test Redis connectivity
            if not self._connection:
                self._connection = await aioredis.from_url(self.redis_url)
            
            # Perform ping test
            pong = await self._connection.ping()
            redis_healthy = pong == b"PONG"
            
            base_health.update({
                "redis_ping": "success" if redis_healthy else "failed",
                "connection_status": "connected" if self._connection else "disconnected"
            })
            
            if redis_healthy:
                base_health["status"] = "healthy"
            else:
                base_health["status"] = "unhealthy"
                
        except Exception as e:
            base_health.update({
                "status": "unhealthy",
                "redis_error": str(e),
                "connection_status": "error"
            })
        
        return base_health
```

## Core Concepts

### 1. Request Context Management

The logging system automatically manages request-scoped context:

```python
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator

# Typically done by middleware, but can be manual
coordinator = LoggingCoordinator()
context = coordinator.start_request(
    session_id="session_123",
    user_id="user_456",
    investigation_id="inv_789"
)

# All logging within this request will include the context
# Context automatically includes correlation_id, timestamps, etc.

# End request and get summary
summary = coordinator.end_request()
print(f"Request processed in {summary['duration_seconds']}s")
print(f"Operations logged: {summary['operations_logged']}")
```

### 2. Operation Deduplication

Operations are automatically deduplicated within a request:

```python
# Within the same request context:
logger.log_boundary("user_lookup", "inbound", data)  # Logged
logger.log_boundary("user_lookup", "inbound", data)  # Skipped - duplicate

# Different operations are logged separately:
logger.log_boundary("user_lookup", "inbound", data)   # Logged
logger.log_boundary("user_update", "inbound", data)   # Logged - different operation
logger.log_boundary("user_lookup", "outbound", data)  # Logged - different direction
```

### 3. Performance Tracking

Each layer has specific performance thresholds:

```python
# API layer operations should complete within 100ms
async with api_logger.operation("handle_request") as ctx:
    await process_request()  # If > 100ms, logs performance violation

# Service layer operations should complete within 500ms  
async with service_logger.operation("business_logic") as ctx:
    await complex_business_logic()  # If > 500ms, logs performance violation

# Custom thresholds can be set via environment variables:
# LOG_PERF_THRESHOLD_API=0.05  # 50ms threshold for API
```

### 4. Error Cascade Prevention

Errors are intelligently handled to prevent duplicate logging:

```python
# Service layer catches and logs error first
try:
    result = await external_service.call()
except Exception as e:
    service_logger.error("Service call failed", error=e)  # Logged
    raise

# When error bubbles up to API layer:
try:
    result = await service.process()
except Exception as e:
    api_logger.error("API call failed", error=e)  # Skipped - already logged
    raise
```

## Migration Guide

### From Old Logging Patterns

#### 1. Replace Direct Logger Usage

**Before:**
```python
import logging
logger = logging.getLogger(__name__)

def process_data(data):
    logger.info("Processing data")
    try:
        result = do_processing(data)
        logger.info("Processing completed")
        return result
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        raise
```

**After:**
```python
from faultmaven.services.base_service import BaseService

class DataProcessor(BaseService):
    def __init__(self):
        super().__init__("data_processor")
    
    async def process_data(self, data):
        return await self.execute_operation(
            "process_data",
            self._do_processing,
            data
        )
    
    async def _do_processing(self, data):
        # Business logic here - automatic logging handled by execute_operation
        return await do_processing(data)
```

#### 2. Replace Manual Error Handling

**Before:**
```python
async def call_external_api():
    for attempt in range(3):
        try:
            response = await api_client.request()
            logger.info("API call succeeded")
            return response
        except Exception as e:
            logger.warning(f"API call failed, attempt {attempt + 1}: {e}")
            if attempt == 2:
                logger.error("API call failed after 3 attempts")
                raise
            await asyncio.sleep(1 * attempt)
```

**After:**
```python
class ApiClient(BaseExternalClient):
    def __init__(self):
        super().__init__("api_client", "ExternalAPI")
    
    async def make_request(self):
        return await self.call_external(
            "api_request",
            self._api_request_impl,
            timeout=10.0,
            retries=2,
            retry_delay=1.0
        )
    
    async def _api_request_impl(self):
        return await self.http_client.request()
```

#### 3. Replace Manual Timing

**Before:**
```python
import time

async def timed_operation():
    start_time = time.time()
    logger.info("Operation started")
    
    try:
        result = await do_work()
        duration = time.time() - start_time
        logger.info(f"Operation completed in {duration:.3f}s")
        return result
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"Operation failed after {duration:.3f}s: {e}")
        raise
```

**After:**
```python
async with logger.operation("timed_operation") as ctx:
    result = await do_work()
    ctx["work_items"] = len(result)
    # Automatic timing, start/end logging, error handling
    return result
```

### Migration Checklist

For each component you're migrating:

- [ ] Replace direct logger imports with unified patterns
- [ ] Choose appropriate base class (BaseService/BaseExternalClient) or direct UnifiedLogger
- [ ] Replace manual try/except with operation patterns  
- [ ] Replace manual timing with context managers
- [ ] Add service boundary logging for cross-service calls
- [ ] Add business events for significant state changes
- [ ] Add performance metrics for key operations
- [ ] Update tests to verify new logging behavior

## Best Practices

### 1. Choose the Right Layer Pattern

```python
# API Layer - request handling, input validation
api_logger = get_unified_logger(__name__, "api")

# Service Layer - business logic orchestration  
class BusinessService(BaseService): ...

# Core Layer - domain logic, data processing
core_logger = get_unified_logger(__name__, "core")

# Infrastructure Layer - external service calls
class ExternalClient(BaseExternalClient): ...
```

### 2. Use Operation Context Managers

```python
# Good - provides automatic timing and error handling
async with logger.operation("complex_process", item_count=len(items)) as ctx:
    ctx["phase"] = "validation"
    await validate_items(items)
    
    ctx["phase"] = "processing"
    result = await process_items(items)
    
    ctx["result_count"] = len(result)
    return result

# Avoid - manual logging without context
logger.info("Starting complex process")
result = await process_items(items)
logger.info("Complex process completed")
```

### 3. Log Service Boundaries

```python
# Log when crossing service boundaries
logger.log_boundary("user_service_call", "inbound", {
    "operation": "create_user",
    "user_data_size": len(user_data)
})

result = await user_service.create_user(user_data)

logger.log_boundary("user_service_call", "outbound", {
    "success": True,
    "user_id": result["user_id"]
})
```

### 4. Use Business Events for State Changes

```python
# Log significant business events
logger.log_event(
    event_type="business",
    event_name="user_account_created",
    severity="info",
    data={
        "user_id": user.id,
        "account_type": user.account_type,
        "registration_source": "web_app"
    }
)
```

### 5. Include Relevant Context

```python
# Good - rich context for debugging
async with logger.operation("generate_report", 
                           user_id=user_id,
                           report_type=report_type,
                           date_range=f"{start_date} to {end_date}") as ctx:
    ctx["data_sources"] = ["database", "cache", "external_api"]
    report = await generate_report_impl()
    ctx["report_size_mb"] = len(report) / (1024 * 1024)

# Avoid - minimal context
async with logger.operation("generate_report") as ctx:
    report = await generate_report_impl()
```

### 6. Use Appropriate Log Levels

```python
# Debug - detailed internal state (disabled in production)
logger.debug("Cache lookup", key=cache_key, hit=cache_hit)

# Info - normal operational events
logger.info("User session created", session_id=session_id)

# Warning - recoverable problems
logger.warning("Cache miss, falling back to database", key=cache_key)

# Error - error conditions that need attention
logger.error("Failed to save user data", error=exception, user_id=user_id)

# Critical - severe problems requiring immediate attention
logger.critical("Database connection lost", error=exception)
```

## Common Patterns

### 1. Validation with Logging

```python
async def validate_and_process(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate input data and process with comprehensive logging."""
    
    def validate_input(data: Dict[str, Any]) -> None:
        required_fields = ["user_id", "action", "timestamp"]
        missing_fields = [f for f in required_fields if f not in data]
        if missing_fields:
            raise ValueError(f"Missing required fields: {missing_fields}")
        
        if not isinstance(data["timestamp"], str):
            raise ValueError("Timestamp must be a string")
    
    return await self.execute_operation(
        "validate_and_process",
        self._process_validated_data,
        data,
        validate_inputs=validate_input
    )
```

### 2. Multi-Step Operations

```python
async def complex_multi_step_operation(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Multi-step operation with phase tracking."""
    
    async with self.logger.operation("complex_operation", 
                                   input_size=len(input_data)) as ctx:
        
        # Step 1: Data preparation
        ctx["phase"] = "preparation"
        prepared_data = await self._prepare_data(input_data)
        ctx["prepared_items"] = len(prepared_data)
        
        # Step 2: External enrichment
        ctx["phase"] = "enrichment"
        enriched_data = await self._enrich_data(prepared_data)
        ctx["enrichment_sources"] = ["api_1", "api_2", "cache"]
        
        # Step 3: Processing
        ctx["phase"] = "processing"
        processed_data = await self._process_data(enriched_data)
        ctx["processing_rules_applied"] = len(processed_data["rules"])
        
        # Step 4: Output formatting
        ctx["phase"] = "formatting"
        final_result = await self._format_output(processed_data)
        ctx["output_format"] = "json_api_v2"
        
        return final_result
```

### 3. Conditional Operations

```python
async def conditional_processing(data: Dict[str, Any]) -> Dict[str, Any]:
    """Process data based on conditions with appropriate logging."""
    
    async with self.logger.operation("conditional_processing",
                                   data_type=data.get("type")) as ctx:
        
        data_type = data.get("type")
        ctx["condition_check"] = data_type
        
        if data_type == "premium":
            ctx["processing_path"] = "premium_flow"
            result = await self._premium_processing(data)
            
        elif data_type == "standard":
            ctx["processing_path"] = "standard_flow"  
            result = await self._standard_processing(data)
            
        else:
            ctx["processing_path"] = "default_flow"
            self.logger.warning(f"Unknown data type: {data_type}, using default processing")
            result = await self._default_processing(data)
        
        ctx["result_type"] = result.get("type")
        return result
```

### 4. Batch Processing

```python
async def process_batch(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Process batch of items with progress tracking."""
    
    async with self.logger.operation("batch_processing", 
                                   batch_size=len(items)) as ctx:
        
        results = []
        failed_items = []
        
        for i, item in enumerate(items):
            try:
                result = await self._process_single_item(item)
                results.append(result)
                
                # Log progress every 100 items
                if (i + 1) % 100 == 0:
                    ctx[f"progress_{i + 1}"] = {
                        "processed": i + 1,
                        "success": len(results),
                        "failed": len(failed_items)
                    }
                    
            except Exception as e:
                failed_items.append({"item": item, "error": str(e)})
                self.logger.warning(f"Failed to process item {i}: {e}")
        
        # Final results
        ctx["batch_summary"] = {
            "total_items": len(items),
            "successful": len(results),
            "failed": len(failed_items),
            "success_rate": len(results) / len(items) * 100
        }
        
        if failed_items:
            self.logger.log_event(
                event_type="technical",
                event_name="batch_processing_partial_failure",
                severity="warning",
                data={
                    "total_items": len(items),
                    "failed_items": len(failed_items),
                    "failure_rate": len(failed_items) / len(items) * 100
                }
            )
        
        return results
```

## Troubleshooting

### Common Issues and Solutions

#### 1. Missing Request Context

**Problem**: Logs don't include correlation IDs or request context.

**Solution**: Ensure request context is properly initialized:

```python
# Check if context is set
from faultmaven.infrastructure.logging.coordinator import request_context

current_context = request_context.get()
if current_context is None:
    logger.warning("No request context found - middleware may not be configured")
```

**Fix**: Verify middleware is properly configured in FastAPI application.

#### 2. Duplicate Logs Still Appearing

**Problem**: Still seeing duplicate log entries.

**Diagnosis**:
```python
# Check if deduplication is working
ctx = request_context.get()
if ctx:
    logger.info(f"Logged operations: {len(ctx.logged_operations)}")
    logger.info(f"Operations: {list(ctx.logged_operations)}")
```

**Common Causes**:
- Multiple logger instances for the same component
- Manual logging bypassing the unified system
- Missing request context initialization

**Solution**: Use consistent logger patterns and verify context initialization.

#### 3. Performance Violations Not Logged

**Problem**: Slow operations not triggering performance violation logs.

**Diagnosis**:
```python
# Check performance tracker
ctx = request_context.get()
if ctx and ctx.performance_tracker:
    thresholds = ctx.performance_tracker.thresholds
    logger.info(f"Performance thresholds: {thresholds}")
else:
    logger.warning("No performance tracker in context")
```

**Solution**: Ensure operations use context managers and verify threshold configuration.

#### 4. Circuit Breaker Not Working

**Problem**: External calls not protected by circuit breaker.

**Diagnosis**:
```python
# Check circuit breaker status
health = await client.health_check()
logger.info(f"Circuit breaker status: {health['circuit_breaker']}")
```

**Solution**: Verify BaseExternalClient is being used and circuit breaker is enabled.

#### 5. Context Not Propagating to Async Tasks

**Problem**: Async tasks don't inherit request context.

**Solution**: Use `contextvars` copy for async tasks:

```python
import asyncio
from contextvars import copy_context

# Create task with current context
ctx = copy_context()
task = ctx.run(asyncio.create_task, async_function())
```

### Debugging Tools

#### 1. Context Inspection

```python
def debug_request_context():
    """Debug helper to inspect current request context."""
    ctx = request_context.get()
    if ctx:
        return {
            "correlation_id": ctx.correlation_id,
            "session_id": ctx.session_id,
            "logged_operations_count": len(ctx.logged_operations),
            "logged_operations": list(ctx.logged_operations),
            "performance_violations": (
                len([t for t, v in ctx.performance_tracker.layer_timings.items() 
                     if v > ctx.performance_tracker.thresholds.get(t.split('.')[0], 1.0)])
                if ctx.performance_tracker else 0
            )
        }
    return {"error": "No request context found"}
```

#### 2. Logger Instance Validation

```python
def validate_logger_setup(logger_instance):
    """Validate logger configuration."""
    checks = {
        "is_unified_logger": isinstance(logger_instance, UnifiedLogger),
        "has_coordinator": hasattr(logger_instance, 'coordinator'),
        "layer_configured": hasattr(logger_instance, 'layer'),
        "structlog_configured": hasattr(logger_instance.logger, 'bind')
    }
    
    logger.info("Logger validation", **checks)
    return all(checks.values())
```

#### 3. Performance Analysis

```python
async def analyze_operation_performance(operation_name: str):
    """Analyze performance of specific operation."""
    ctx = request_context.get()
    if ctx and ctx.performance_tracker:
        timings = ctx.performance_tracker.layer_timings
        operation_timings = {
            k: v for k, v in timings.items() 
            if operation_name in k
        }
        
        logger.info("Operation performance analysis", 
                   operation=operation_name,
                   timings=operation_timings)
        return operation_timings
    
    return {}
```

## Testing Guidelines

### Testing with Unified Logger

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator

@pytest.fixture
async def mock_request_context():
    """Fixture to provide mock request context for testing."""
    coordinator = LoggingCoordinator()
    context = coordinator.start_request(session_id="test_session")
    yield context
    coordinator.end_request()

@pytest.mark.asyncio
async def test_service_operation_logging(mock_request_context):
    """Test that service operations log correctly."""
    service = YourService()
    
    # Execute operation
    result = await service.process_data({"test": "data"})
    
    # Verify logging behavior
    ctx = mock_request_context
    assert len(ctx.logged_operations) > 0
    assert any("process_data" in op for op in ctx.logged_operations)
    
    # Verify no performance violations for fast operations
    assert ctx.performance_tracker is not None
    # Add specific assertions based on your operation
```

### Mocking External Dependencies

```python
@pytest.fixture
def mock_external_client():
    """Mock external client with circuit breaker."""
    client = YourExternalClient()
    client._connection = AsyncMock()
    client.circuit_breaker = MagicMock()
    client.circuit_breaker.can_execute.return_value = True
    return client

@pytest.mark.asyncio  
async def test_external_call_with_circuit_breaker(mock_external_client):
    """Test external call with circuit breaker protection."""
    # Mock successful response
    mock_external_client._connection.get.return_value = {"data": "test"}
    
    result = await mock_external_client.get_data("key")
    
    # Verify circuit breaker was checked
    mock_external_client.circuit_breaker.can_execute.assert_called_once()
    
    # Verify call was made
    mock_external_client._connection.get.assert_called_once_with("key")
```

This developer guide provides comprehensive guidance for using the FaultMaven logging system effectively. Follow these patterns and best practices to ensure consistent, observable, and maintainable logging across your application components.