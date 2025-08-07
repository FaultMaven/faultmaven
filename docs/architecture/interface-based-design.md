# Interface-Based Design Architecture

**Document Type**: Architecture Deep-dive  
**Last Updated**: August 2025  
**Status**: Active Implementation

## Overview

FaultMaven employs a comprehensive interface-based programming model that provides loose coupling, high testability, and flexible deployment configurations. This architecture separates contracts from implementations, enabling dependency injection and supporting multiple runtime configurations through feature flags.

## Interface System Design

### Core Interface Hierarchy

The system defines **10 primary interfaces** in `faultmaven/models/interfaces.py` that abstract all external dependencies and major system components:

#### Infrastructure Interfaces

**`ILLMProvider`**
- **Purpose**: Abstracts interaction with Large Language Model providers
- **Methods**: `generate(prompt, **kwargs) -> str`
- **Implementations**: LLMRouter with OpenAI, Anthropic, Fireworks AI providers
- **Benefits**: Provider switching without code changes, easy testing with mocks

**`ITracer`** 
- **Purpose**: Distributed tracing and observability
- **Methods**: `trace(operation) -> ContextManager`
- **Implementations**: OpikTracer for production, mock for testing
- **Integration**: Automatic span creation and telemetry collection

**`ISanitizer`**
- **Purpose**: PII redaction and data privacy protection
- **Methods**: `sanitize(data) -> Any`
- **Implementations**: DataSanitizer with Presidio integration, regex fallback
- **Compliance**: GDPR and privacy law adherence

#### Data Processing Interfaces

**`IDataClassifier`**
- **Purpose**: Automatic data type detection and classification
- **Methods**: `classify(content, filename) -> DataType`
- **Implementations**: ML-based classifier with heuristic fallback
- **Use Cases**: Log file type detection, format identification

**`ILogProcessor`**
- **Purpose**: Log analysis and insight extraction
- **Methods**: `process(content, data_type) -> Dict[str, Any]`
- **Implementations**: Pattern-based analyzer with LLM enhancement
- **Features**: Error pattern detection, timeline analysis

#### Storage and Persistence Interfaces

**`IVectorStore`**
- **Purpose**: Vector database operations for knowledge base
- **Methods**: `add_documents(documents)`, `search(query, k) -> List[Dict]`
- **Implementations**: ChromaDB integration with local fallback
- **Features**: Semantic search, document embedding

**`ISessionStore`**
- **Purpose**: Session management and user state
- **Methods**: `get(key) -> Dict`, `set(key, value, ttl)`
- **Implementations**: Redis with in-memory fallback
- **Features**: TTL support, authentication integration

**`IStorageBackend`**
- **Purpose**: Generic storage operations
- **Methods**: `store(key, data)`, `retrieve(key) -> Any`
- **Implementations**: File-based, Redis-based, S3-compatible
- **Usage**: Document storage, cache management

#### Knowledge Management Interfaces

**`IKnowledgeIngester`**
- **Purpose**: Document ingestion and processing pipeline
- **Methods**: `ingest_document()`, `update_document()`, `delete_document()`
- **Implementations**: Multi-format processor with vector embedding
- **Features**: Document lifecycle management, metadata extraction

#### Tool System Interfaces

**`BaseTool`**
- **Purpose**: Agent tool abstraction for consistent integration
- **Methods**: `execute(params) -> ToolResult`, `get_schema() -> Dict`
- **Implementations**: KnowledgeBaseTool, WebSearchTool
- **Features**: Standardized parameter validation, result formatting

**`ToolResult`**
- **Purpose**: Standardized tool execution results
- **Properties**: `success: bool`, `data: Any`, `error: Optional[str]`
- **Benefits**: Consistent error handling, structured responses

## Interface Implementation Strategy

### 1. Implementation Isolation

Each interface has multiple implementations optimized for different environments:

```python
# Production implementation
class OpikTracer(ITracer):
    def trace(self, operation: str) -> ContextManager:
        return opik.track(name=operation)

# Testing implementation  
class MockTracer(ITracer):
    def trace(self, operation: str) -> ContextManager:
        return nullcontext()

# Development implementation
class LoggingTracer(ITracer):
    def trace(self, operation: str) -> ContextManager:
        return self.logger.info(f"Tracing: {operation}")
```

### 2. Graceful Degradation

Interfaces support fallback implementations when dependencies are unavailable:

```python
class DataSanitizer(ISanitizer):
    def __init__(self):
        try:
            self.presidio_client = PresidioClient()
            self.use_presidio = True
        except Exception:
            self.use_presidio = False
            
    def sanitize(self, data: Any) -> Any:
        if self.use_presidio:
            return self.presidio_client.anonymize(data)
        else:
            return self._regex_redaction(data)  # Fallback
```

### 3. Configuration-Driven Selection

Feature flags control which implementations are used:

```python
def create_llm_provider() -> ILLMProvider:
    if os.getenv("USE_LOCAL_LLM") == "true":
        return LocalLLMProvider()
    elif os.getenv("USE_MOCK_LLM") == "true":
        return MockLLMProvider()
    else:
        return LLMRouter()  # Production implementation
```

## Dependency Injection Architecture

### Container-Based Management

The `DIContainer` class provides centralized dependency resolution:

**Features**:
- **Singleton Pattern**: One container instance per application
- **Lazy Initialization**: Components created only when requested
- **Interface Resolution**: Maps interface requests to concrete implementations
- **Health Monitoring**: Built-in dependency health checking
- **Environment Adaptation**: Different implementations based on runtime environment

### Service Constructor Injection

All services receive dependencies through constructor injection:

```python
class AgentServiceRefactored:
    def __init__(
        self,
        llm_provider: ILLMProvider,        # Interface dependency
        tools: List[BaseTool],              # Interface list
        tracer: ITracer,                   # Interface dependency
        sanitizer: ISanitizer              # Interface dependency
    ):
        # All dependencies are interfaces, not concrete classes
        self.llm_provider = llm_provider
        self.tools = tools
        self.tracer = tracer
        self.sanitizer = sanitizer
```

**Benefits**:
- **Testability**: Easy to mock all dependencies in unit tests
- **Flexibility**: Swap implementations without changing service code
- **Explicitness**: All dependencies are clearly declared
- **Type Safety**: Interface contracts enforced at compile time

### Container Resolution Process

1. **Interface Request**: Service requests `ILLMProvider` from container
2. **Implementation Selection**: Container selects appropriate implementation based on environment
3. **Dependency Graph**: Container resolves all transitive dependencies
4. **Singleton Management**: Returns existing instance or creates new one
5. **Health Verification**: Ensures implementation is healthy before returning

## Testing Strategy with Interfaces

### Unit Testing Benefits

Interfaces make unit testing straightforward through dependency mocking:

```python
def test_agent_service_query_processing():
    # Mock all interface dependencies
    mock_llm = Mock(spec=ILLMProvider)
    mock_tracer = Mock(spec=ITracer)
    mock_sanitizer = Mock(spec=ISanitizer)
    mock_tools = [Mock(spec=BaseTool)]
    
    # Inject mocked dependencies
    service = AgentServiceRefactored(
        llm_provider=mock_llm,
        tools=mock_tools,
        tracer=mock_tracer,
        sanitizer=mock_sanitizer
    )
    
    # Test business logic in isolation
    result = service.process_query(test_request)
    
    # Verify interface interactions
    mock_llm.generate.assert_called_once()
    mock_sanitizer.sanitize.assert_called()
```

### Integration Testing

Interfaces allow selective integration testing:

```python
def test_integration_with_real_llm():
    # Use real LLM provider, mock others
    real_llm = LLMRouter()
    mock_tracer = Mock(spec=ITracer) 
    mock_sanitizer = Mock(spec=ISanitizer)
    
    service = AgentServiceRefactored(
        llm_provider=real_llm,  # Real implementation
        tools=[],
        tracer=mock_tracer,     # Mock
        sanitizer=mock_sanitizer # Mock
    )
    
    # Test actual LLM integration
```

## Runtime Configuration Benefits

### Feature Flag Integration

Interfaces work seamlessly with feature flags for runtime behavior control:

```python
def get_agent_service() -> AgentServiceRefactored:
    container = DIContainer()
    
    if USE_REFACTORED_SERVICES:
        # Return interface-based service
        return container.get_agent_service()
    else:
        # Return legacy implementation
        return get_legacy_agent_service()
```

### Environment-Specific Implementations

Different environments use different interface implementations:

- **Development**: Mock implementations with logging
- **Testing**: In-memory implementations with deterministic behavior
- **Staging**: Real implementations with debug features
- **Production**: Optimized implementations with monitoring

## Architecture Benefits

### 1. Maintainability
- **Clear Contracts**: Interfaces define exact expectations
- **Implementation Freedom**: Internal changes don't affect contracts
- **Dependency Clarity**: All dependencies are explicit and typed

### 2. Testability
- **Isolation**: Each component can be tested independently
- **Mocking**: Easy to create test doubles for all dependencies
- **Verification**: Interface interactions can be precisely verified

### 3. Flexibility
- **Provider Switching**: Change LLM providers without code changes
- **Environment Adaptation**: Different implementations per environment
- **Feature Toggling**: Enable/disable features through implementation selection

### 4. Extensibility
- **New Implementations**: Add new providers implementing existing interfaces
- **Backward Compatibility**: New interface methods can have default implementations
- **Plugin Architecture**: External tools can implement BaseTool interface

### 5. Deployment Options
- **Microservice Ready**: Services can be deployed independently
- **Monolithic Flexible**: Can run as monolith with interface benefits
- **Hybrid Deployment**: Some services local, others remote

## Implementation Guidelines

### Interface Design Principles

1. **Single Responsibility**: Each interface has one clear purpose
2. **Minimal Contracts**: Only essential methods in interface
3. **Async Support**: All I/O operations are async by default
4. **Error Handling**: Consistent error contracts across implementations
5. **Documentation**: Clear docstrings for all interface methods

### Adding New Interfaces

1. **Define Interface**: Create abstract base class in `interfaces.py`
2. **Create Implementation**: Build concrete implementation
3. **Register in Container**: Add to DI container resolution
4. **Update Tests**: Add interface compliance tests
5. **Update Documentation**: Document new interface capabilities

### Best Practices

1. **Favor Composition**: Use interface composition over inheritance
2. **Explicit Dependencies**: All dependencies injected through constructor
3. **Interface Compliance**: Ensure implementations fully satisfy contracts
4. **Graceful Fallbacks**: Provide fallback implementations for reliability
5. **Health Monitoring**: Include health checks for all implementations

## Future Enhancements

### Planned Interface Extensions

1. **IEventBus**: Event-driven architecture support
2. **IConfigurationProvider**: Dynamic configuration management
3. **ICacheProvider**: Distributed caching abstraction
4. **ISecurityProvider**: Authentication and authorization abstraction
5. **IMonitoringProvider**: Metrics and monitoring abstraction

### Advanced Patterns

1. **Interface Decorators**: Cross-cutting concerns through interface wrapping
2. **Interface Composition**: Complex interfaces built from simple ones
3. **Interface Versioning**: Backward-compatible interface evolution
4. **Interface Discovery**: Dynamic interface implementation discovery
5. **Interface Validation**: Runtime interface compliance checking

This interface-based design positions FaultMaven for scalable growth while maintaining high code quality and operational flexibility.