# Infrastructure Layer Architecture Guide

## Overview

This document explains the architectural principles and patterns for the FaultMaven infrastructure layer, with specific focus on the distinction between internal infrastructure operations and external service monitoring.

## Key Architectural Principle

The infrastructure layer follows a critical architectural principle:

> **Internal infrastructure operations should use lightweight clients for performance, while external service monitoring should use comprehensive logging for reliability.**

This separation prevents performance bottlenecks while maintaining observability where it matters most.

## Architecture Patterns

### Pattern 1: External Service Integration

**Use Case**: Third-party APIs, LLM providers, external databases, services outside your control

**Implementation**: Inherit from `BaseExternalClient` for comprehensive monitoring

**Characteristics**:
- Full logging and observability
- Circuit breaker protection
- Retry mechanisms with exponential backoff
- Response validation and transformation
- Connection metrics tracking
- Health monitoring
- Performance thresholds monitoring

```python
from faultmaven.infrastructure.base_client import BaseExternalClient

class OpenAIClient(BaseExternalClient):
    """External LLM provider with comprehensive monitoring."""
    
    def __init__(self, api_key: str):
        super().__init__(
            client_name="openai_client",
            service_name="OpenAI",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30
        )
        self.api_key = api_key
    
    async def generate_completion(self, prompt: str) -> Dict[str, Any]:
        """Generate completion with full monitoring."""
        
        async def llm_api_call(prompt: str) -> Dict[str, Any]:
            # Actual API call implementation
            response = await self._http_client.post(
                "https://api.openai.com/v1/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": "gpt-3.5-turbo", "prompt": prompt}
            )
            return response.json()
        
        # Execute with comprehensive monitoring
        return await self.call_external(
            operation_name="generate_completion",
            call_func=llm_api_call,
            prompt,
            timeout=30.0,
            retries=3,
            retry_delay=2.0
        )
```

### Pattern 2: Internal Infrastructure

**Use Case**: Redis sessions, internal databases, services you control and trust

**Implementation**: Use lightweight client factories and interface implementations

**Characteristics**:
- Minimal logging overhead
- Direct client usage
- High-frequency operation optimization
- Application-level error logging only
- No circuit breakers or retry logic
- Optimized for performance

```python
from faultmaven.infrastructure.redis_client import create_redis_client
from faultmaven.models.interfaces import ISessionStore

class RedisSessionStore(ISessionStore):
    """Lightweight Redis session store for high-frequency operations."""
    
    def __init__(self):
        # Lightweight factory function - no BaseExternalClient overhead
        self.redis_client = create_redis_client()
        self.default_ttl = 1800
        self.prefix = "session:"
    
    async def get(self, key: str) -> Optional[Dict]:
        """Direct Redis operation with minimal logging."""
        full_key = f"{self.prefix}{key}"
        data = await self.redis_client.get(full_key)
        
        if data:
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                # Only log application-level errors
                logger.warning(f"Invalid JSON in session {key}")
                return None
        return None
    
    async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
        """Direct Redis operation - no comprehensive logging."""
        full_key = f"{self.prefix}{key}"
        
        if 'last_activity' not in value:
            value['last_activity'] = datetime.utcnow().isoformat()
        
        serialized = json.dumps(value)
        ttl = ttl if ttl is not None else self.default_ttl
        
        # Direct Redis call - minimal overhead
        await self.redis_client.set(full_key, serialized, ex=ttl)
```

## Redis Logging Architecture Improvement

### Problem Statement

Previously, `RedisSessionStore` used a verbose `RedisClient` that inherited from `BaseExternalClient`, causing excessive logging for high-frequency session operations that occur on every HTTP request.

**Root Cause**: Architectural anti-pattern - session storage operations were being treated as "external service calls" requiring comprehensive logging, when Redis is internal infrastructure.

### Solution Implementation

1. **Changed RedisSessionStore to use lightweight client**:
   ```python
   # Before (excessive logging)
   class RedisSessionStore(BaseExternalClient):
       async def get(self, key: str):
           return await self.call_external("get_session", self._redis_get, key)
   
   # After (optimized)
   class RedisSessionStore(ISessionStore):
       def __init__(self):
           self.redis_client = create_redis_client()  # Lightweight factory
   
       async def get(self, key: str):
           return await self.redis_client.get(f"{self.prefix}{key}")  # Direct call
   ```

2. **Removed unused verbose Redis client**:
   - Deleted `faultmaven/infrastructure/persistence/redis.py` (297 lines)
   - Eliminated `RedisClient(BaseExternalClient)` pattern
   - Updated all test mocks accordingly

3. **Maintained interface compliance**:
   - All functionality preserved through `ISessionStore` interface
   - No breaking changes to dependent services
   - Test coverage maintained

### Benefits Achieved

1. **Performance Improvement**: Eliminated logging overhead for high-frequency session operations
2. **Proper Separation of Concerns**: Internal infrastructure vs external service monitoring
3. **Resource Efficiency**: Reduced CPU and memory usage for session operations
4. **Architectural Clarity**: Clear distinction between internal and external service patterns

## Decision Matrix: Which Pattern to Use

| Criteria | External Service Pattern | Internal Infrastructure Pattern |
|----------|-------------------------|--------------------------------|
| **Service Location** | Third-party, external | Internal, controlled |
| **Operation Frequency** | Low to medium (< 10/sec) | High frequency (> 100/sec) |
| **Monitoring Needs** | Comprehensive | Minimal |
| **Failure Impact** | Business critical | Infrastructure level |
| **Examples** | OpenAI, Anthropic, Webhooks | Redis sessions, Internal DBs |
| **Base Class** | `BaseExternalClient` | Interface implementation |
| **Client Creation** | Constructor injection | Factory functions |

## Implementation Guidelines

### For External Services

1. **Always inherit from BaseExternalClient**
2. **Implement comprehensive error handling**
3. **Use circuit breakers and retries**
4. **Add service-specific health checks**
5. **Monitor response times and success rates**

```python
class ExternalAPIClient(BaseExternalClient):
    def __init__(self, base_url: str, api_key: str):
        super().__init__(
            client_name="external_api",
            service_name="ExternalAPI",
            enable_circuit_breaker=True
        )
        self.base_url = base_url
        self.api_key = api_key
    
    async def call_api(self, endpoint: str, data: dict) -> dict:
        return await self.call_external(
            f"api_{endpoint}",
            self._make_request,
            endpoint,
            data,
            timeout=10.0,
            retries=3
        )
```

### For Internal Infrastructure

1. **Use lightweight factory functions**
2. **Implement interface contracts**
3. **Log only application-level errors**
4. **Optimize for performance**
5. **Keep operations simple and direct**

```python
from faultmaven.infrastructure.redis_client import create_redis_client

class InternalCacheStore:
    def __init__(self):
        self.redis = create_redis_client()
        self.prefix = "cache:"
    
    async def get(self, key: str) -> Optional[str]:
        try:
            return await self.redis.get(f"{self.prefix}{key}")
        except Exception as e:
            # Log only significant errors
            logger.error(f"Cache get failed for {key}: {e}")
            return None
```

## Migration Guide

### Identifying Anti-patterns

Look for these signs that internal infrastructure is using external service patterns:

```python
# ❌ Anti-pattern: Internal service using BaseExternalClient
class RedisCache(BaseExternalClient):
    async def get(self, key):
        return await self.call_external("cache_get", self._get_operation, key)

# ❌ Anti-pattern: High-frequency operations with comprehensive logging
class SessionStore(BaseExternalClient):
    async def get_session(self, session_id):  # Called on every HTTP request
        return await self.call_external("get_session", self._redis_get, session_id)
```

### Migration Steps

1. **Identify the service type**: Internal infrastructure or external service?

2. **For internal infrastructure**:
   ```python
   # Replace BaseExternalClient inheritance
   class MyStore(BaseExternalClient):  # Remove this
   
   # With interface implementation
   class MyStore(IMyStoreInterface):   # Add this
   
   # Replace call_external() calls
   return await self.call_external("op", func, args)  # Remove this
   
   # With direct calls
   return await self.client.operation(args)           # Add this
   ```

3. **Update client creation**:
   ```python
   # Replace complex client creation
   self.client = MyClient("name", "service", circuit_breaker=True)
   
   # With factory function
   self.client = create_my_client()
   ```

4. **Update tests**:
   ```python
   # Update mock targets
   @patch('module.MyClient')           # Old mock target
   @patch('module.create_my_client')   # New mock target
   ```

## Testing Considerations

### External Service Tests

```python
@pytest.fixture
def external_client():
    return ExternalAPIClient("https://api.example.com", "test-key")

async def test_external_api_with_monitoring(external_client):
    # Test should verify monitoring behavior
    with patch.object(external_client, 'call_external') as mock_call:
        await external_client.call_api("test", {"data": "value"})
        mock_call.assert_called_once()
```

### Internal Infrastructure Tests

```python
@pytest.fixture
def redis_store():
    return RedisSessionStore()

@patch('faultmaven.infrastructure.redis_client.create_redis_client')
async def test_session_store_direct_operations(mock_create_client, redis_store):
    # Test should verify direct operations, not monitoring
    mock_client = AsyncMock()
    mock_create_client.return_value = mock_client
    
    await redis_store.get("test-session")
    mock_client.get.assert_called_once_with("session:test-session")
```

## Monitoring and Observability

### External Services

- **Comprehensive metrics**: Response times, error rates, circuit breaker states
- **Detailed logging**: Every call logged with context
- **Health monitoring**: Regular health checks and SLA tracking
- **Alerting**: Failures trigger alerts

### Internal Infrastructure

- **Essential metrics only**: Basic health and performance
- **Error-only logging**: Log failures, not every operation
- **Simple health checks**: Basic connectivity verification
- **Performance monitoring**: Track overall system impact

This architecture guide ensures that FaultMaven maintains optimal performance for high-frequency internal operations while providing comprehensive monitoring for external service integrations.