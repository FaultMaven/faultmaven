# Service Layer Patterns and Best Practices

## Overview

This document describes the patterns and best practices used in the FaultMaven service layer. Following these patterns ensures consistency, maintainability, and scalability across the codebase.

## Service Layer Principles

### 1. Single Responsibility Principle
Each service should have one clear purpose:
- **AgentService**: Agent operations and troubleshooting workflows
- **DataService**: Data processing and transformation
- **KnowledgeService**: Knowledge base operations
- **SessionService**: Session lifecycle management

### 2. Dependency Injection
Services receive their dependencies through constructor injection:

```python
class AgentService:
    def __init__(
        self,
        core_agent: FaultMavenAgent,
        data_sanitizer: DataSanitizer,
        logger: Optional[logging.Logger] = None,
    ):
        self.core_agent = core_agent
        self.data_sanitizer = data_sanitizer
        self.logger = logger or logging.getLogger(__name__)
```

### 3. Async-First Design
All service methods that perform I/O should be async:

```python
async def process_query(
    self,
    query: str,
    session_id: str,
    context: Optional[Dict[str, Any]] = None,
) -> TroubleshootingResponse:
    # Async implementation
```

## Common Service Patterns

### 1. Input Validation Pattern

Services validate input at the boundary:

```python
async def process_query(self, query: str, session_id: str) -> Response:
    # Validate input
    if not query or not query.strip():
        raise ValueError("Query cannot be empty")
    
    # Sanitize input
    sanitized_query = self.data_sanitizer.sanitize(query)
    
    # Process...
```

### 2. Error Handling Pattern

Services use structured error handling:

```python
async def operation(self, params: Dict) -> Result:
    try:
        # Main operation
        result = await self._perform_operation(params)
        return result
        
    except ValidationError as e:
        self.logger.warning(f"Validation failed: {e}")
        raise ValueError(f"Invalid input: {str(e)}") from e
        
    except ExternalServiceError as e:
        self.logger.error(f"External service failed: {e}")
        raise RuntimeError(f"Service unavailable: {str(e)}") from e
        
    except Exception as e:
        self.logger.error(f"Unexpected error: {e}", exc_info=True)
        raise RuntimeError("Operation failed") from e
```

### 3. Tracing Pattern

Use decorators for operation tracing:

```python
@trace("service_operation_name")
async def traced_operation(self, params: Dict) -> Result:
    self.logger.info(f"Starting operation with params: {params}")
    # Implementation
```

### 4. Result Formatting Pattern

Services format results consistently:

```python
def _format_response(
    self,
    raw_data: Dict,
    metadata: Dict,
) -> FormattedResponse:
    """Format raw data into standardized response."""
    return FormattedResponse(
        data=self._transform_data(raw_data),
        metadata={
            **metadata,
            "processed_at": datetime.utcnow().isoformat(),
            "service": self.__class__.__name__,
        }
    )
```

## Service Method Categories

### 1. Primary Operations
Main business operations that fulfill the service's purpose:

```python
async def process_query(...)  # AgentService
async def ingest_data(...)    # DataService
async def search(...)         # KnowledgeService
async def create_session(...) # SessionService
```

### 2. Query Methods
Read-only operations for retrieving data:

```python
async def get_session(...)
async def find_similar_documents(...)
async def get_session_analytics(...)
```

### 3. Command Methods
Operations that modify state:

```python
async def update_session(...)
async def delete_document(...)
async def cleanup_inactive_sessions(...)
```

### 4. Utility Methods
Helper methods marked as private:

```python
def _validate_input(...)
async def _fetch_from_cache(...)
def _calculate_metrics(...)
```

## Inter-Service Communication

### 1. Direct Dependency
Services can depend on other services when there's a clear hierarchy:

```python
class SessionService:
    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
```

### 2. Event-Based (Future)
For loose coupling, use events:

```python
async def process_data(self, data: Data):
    result = await self._process(data)
    await self.event_bus.publish("data.processed", result)
```

### 3. Service Locator (via Container)
Access services through the container when needed:

```python
from ..container import container

async def cross_service_operation():
    agent_service = container.agent_service
    result = await agent_service.process_query(...)
```

## Testing Patterns

### 1. Mock Dependencies
Services should be testable with mocked dependencies:

```python
# In tests
mock_agent = Mock(spec=FaultMavenAgent)
mock_sanitizer = Mock(spec=DataSanitizer)
service = AgentService(mock_agent, mock_sanitizer)
```

### 2. Isolated Unit Tests
Test service methods in isolation:

```python
async def test_process_query_validates_input():
    service = AgentService(mock_agent, mock_sanitizer)
    
    with pytest.raises(ValueError):
        await service.process_query("", "session-123")
```

### 3. Integration Tests
Test service integration with real dependencies:

```python
async def test_full_troubleshooting_flow():
    # Use real container
    response = await container.agent_service.process_query(
        "Why is my service failing?",
        "session-123"
    )
    assert response.findings
```

## Performance Patterns

### 1. Lazy Loading
Initialize expensive resources only when needed:

```python
@property
def expensive_resource(self):
    if self._resource is None:
        self._resource = self._initialize_resource()
    return self._resource
```

### 2. Caching
Cache frequently accessed data:

```python
@lru_cache(maxsize=100)
async def get_cached_result(self, key: str):
    return await self._fetch_result(key)
```

### 3. Batch Operations
Provide batch methods for efficiency:

```python
async def batch_process(
    self,
    items: List[Item],
) -> List[Result]:
    # Process items in parallel
    tasks = [self.process_item(item) for item in items]
    return await asyncio.gather(*tasks)
```

## Logging Best Practices

### 1. Structured Logging
Use structured log messages:

```python
self.logger.info(
    "Processing query",
    extra={
        "session_id": session_id,
        "query_length": len(query),
        "priority": priority,
    }
)
```

### 2. Log Levels
- **DEBUG**: Detailed execution flow
- **INFO**: Normal operations
- **WARNING**: Recoverable issues
- **ERROR**: Failures requiring attention

### 3. Sensitive Data
Never log sensitive information:

```python
# Bad
self.logger.info(f"Processing query: {query}")

# Good
self.logger.info(f"Processing query of length {len(query)}")
```

## Configuration Management

### 1. Service Configuration
Services receive configuration through the container:

```python
class DataService:
    def __init__(self, config: Dict[str, Any]):
        self.max_file_size = config.get("max_file_size_mb", 10)
        self.processing_timeout = config.get("processing_timeout_seconds", 300)
```

### 2. Environment Variables
Configuration values come from environment:

```python
# In container.py
config = {
    "max_file_size_mb": int(os.getenv("MAX_FILE_SIZE_MB", "10")),
    "processing_timeout_seconds": int(os.getenv("PROCESSING_TIMEOUT", "300")),
}
```

## Service Lifecycle

### 1. Initialization
Services are initialized once by the container:

```python
# Container creates singleton instances
@property
def agent_service(self) -> AgentService:
    if self._agent_service is None:
        self._agent_service = AgentService(...)
    return self._agent_service
```

### 2. Startup
Async initialization when needed:

```python
async def initialize(self):
    """Initialize service resources."""
    await self._connect_to_external_service()
    await self._load_cache()
```

### 3. Shutdown
Cleanup resources properly:

```python
async def shutdown(self):
    """Cleanup service resources."""
    await self._close_connections()
    await self._flush_cache()
```

## Anti-Patterns to Avoid

### 1. Direct Database Access
Services should use repositories or managers:

```python
# Bad
async def get_data(self):
    return await self.redis_client.get("key")

# Good
async def get_data(self):
    return await self.data_repository.get("key")
```

### 2. Business Logic in API Layer
Keep business logic in services:

```python
# Bad (in API router)
if len(query) > 1000:
    query = query[:1000]

# Good (in service)
def _validate_query(self, query: str) -> str:
    if len(query) > self.max_query_length:
        raise ValueError(f"Query too long: {len(query)} chars")
```

### 3. Circular Dependencies
Avoid services depending on each other circularly:

```python
# Bad
class ServiceA:
    def __init__(self, service_b: ServiceB):
        self.service_b = service_b

class ServiceB:
    def __init__(self, service_a: ServiceA):
        self.service_a = service_a
```

## Future Patterns

### 1. Domain Events
Implement domain event system:

```python
class DataProcessedEvent:
    data_id: str
    session_id: str
    timestamp: datetime
```

### 2. Saga Pattern
For distributed transactions:

```python
class TroubleshootingSaga:
    async def execute(self):
        # Coordinate multiple services
        pass
```

### 3. Circuit Breaker
For external service calls:

```python
@circuit_breaker(failure_threshold=5, recovery_timeout=60)
async def call_external_service(self):
    # Protected external call
    pass
```