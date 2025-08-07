# Dependency Injection System Architecture

**Document Type**: Architecture Deep-dive  
**Last Updated**: August 2025  
**Status**: Active Implementation

## Overview

FaultMaven implements a comprehensive dependency injection (DI) system that manages the lifecycle and dependencies of all system components. The DI container provides centralized dependency resolution, interface-based abstraction, and runtime configuration flexibility through feature flags.

## Container Architecture

### Singleton Container Design

The `DIContainer` class follows the singleton pattern to ensure consistent dependency management across the application:

```python
class DIContainer:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
```

**Key Features**:
- **Single Source of Truth**: One container instance manages all dependencies
- **Lazy Initialization**: Components created only when first requested
- **Thread Safety**: Singleton implementation handles concurrent access
- **Reset Capability**: Full container reset for testing scenarios

### Layered Dependency Structure

The container organizes dependencies into logical layers:

#### 1. Infrastructure Layer
**Purpose**: External service integrations and technical concerns

```python
def _create_infrastructure_layer(self):
    # LLM Provider with multi-provider support
    self.llm_provider: ILLMProvider = LLMRouter()
    
    # Data sanitization for PII protection
    self.sanitizer: ISanitizer = DataSanitizer()
    
    # Distributed tracing
    self.tracer: ITracer = OpikTracer()
    
    # Core processing interfaces
    self.data_classifier = DataClassifier()
    self.log_processor = LogProcessor()
```

**Components**:
- **LLM Provider**: Multi-provider routing with failover
- **Sanitizer**: PII redaction with Presidio integration
- **Tracer**: Observability with Opik integration
- **Classifier**: Data type detection and categorization
- **Log Processor**: Log analysis and insight extraction

#### 2. Tools Layer
**Purpose**: Agent capabilities with standardized interfaces

```python
def _create_tools_layer(self):
    # Knowledge base tool with ingester dependency
    knowledge_base_tool = KnowledgeBaseTool(
        knowledge_ingester=KnowledgeIngester()
    )
    
    # Web search capability
    web_search_tool = WebSearchTool()
    
    # Create tools list with error handling
    self.tools: List[BaseTool] = [
        tool for tool in [knowledge_base_tool, web_search_tool] 
        if tool is not None
    ]
```

**Features**:
- **Interface Compliance**: All tools implement `BaseTool` interface
- **Error Resilience**: Failed tool initialization doesn't break container
- **Dynamic Loading**: Tools can be enabled/disabled based on environment
- **Standardized Execution**: Consistent `execute()` and `get_schema()` methods

#### 3. Service Layer
**Purpose**: Business logic orchestration with interface dependencies

```python
def _create_service_layer(self):
    # Agent Service - Core troubleshooting orchestration
    self.agent_service = AgentService(
        llm_provider=self.llm_provider,    # Interface injection
        tools=self.tools,                  # Interface list injection
        tracer=self.tracer,               # Interface injection
        sanitizer=self.sanitizer          # Interface injection
    )
    
    # Data Service - Data processing and analysis
    self.data_service = DataService(
        data_classifier=self.data_classifier,
        log_processor=self.log_processor,
        sanitizer=self.sanitizer,
        tracer=self.tracer,
        storage_backend=SimpleStorageBackend()
    )
```

**Dependency Resolution**:
- **Interface-Based**: All dependencies are interface references
- **Constructor Injection**: Dependencies provided at object creation
- **Transitive Resolution**: Container handles dependency chains
- **Error Handling**: Graceful fallbacks when dependencies unavailable

## Interface Resolution Strategy

### Automatic Implementation Selection

The container automatically selects appropriate implementations based on environment:

```python
def get_llm_provider(self) -> ILLMProvider:
    self.initialize()
    
    # Environment-based selection
    if os.getenv("USE_MOCK_LLM") == "true":
        return MockLLMProvider()
    elif os.getenv("LLM_PROVIDER") == "local":
        return LocalLLMProvider()
    else:
        return self.llm_provider  # Production LLMRouter
```

### Graceful Degradation

When dependencies are unavailable, the container provides fallback implementations:

```python
def _create_minimal_container(self):
    """Create minimal container for testing environments"""
    from unittest.mock import MagicMock
    
    # Infrastructure layer mocks
    self.llm_provider = MagicMock()
    self.sanitizer = MagicMock()  
    self.tracer = MagicMock()
    
    # Service layer mocks
    self.agent_service = MagicMock()
    self.data_service = MagicMock()
    
    logger.info("Created minimal container for testing")
```

**Benefits**:
- **Testing Support**: Works in environments without external dependencies
- **Development Flexibility**: Developers can work with mock implementations
- **Deployment Resilience**: Application starts even with missing services
- **Debugging Assistance**: Clear logging of fallback activations

## Service Registration and Retrieval

### Public Getter Methods

The container provides typed getter methods for all services:

```python
def get_agent_service(self) -> AgentService:
    """Get the agent service with all dependencies injected"""
    self.initialize()
    return self.agent_service

def get_data_service(self) -> DataService:
    """Get the data service with all dependencies injected"""
    self.initialize()
    return self.data_service

def get_llm_provider(self) -> ILLMProvider:
    """Get the LLM provider interface implementation"""
    self.initialize()
    return self.llm_provider
```

**Features**:
- **Lazy Initialization**: Services created on first access
- **Type Safety**: Return types are explicitly declared
- **Interface Contracts**: All returned objects implement required interfaces
- **Consistent API**: Uniform naming pattern for all getters

### Global Container Access

A proxy class provides convenient global access to the container:

```python
class GlobalContainer:
    """Proxy class that always returns the current singleton instance"""
    
    def __getattr__(self, name):
        current_instance = DIContainer()
        return getattr(current_instance, name)
    
    def __call__(self, *args, **kwargs):
        return DIContainer(*args, **kwargs)

# Global container instance
container = GlobalContainer()
```

**Usage**:
```python
# Direct access from anywhere in the application
agent_service = container.get_agent_service()
llm_provider = container.get_llm_provider()

# Type hints work correctly
def process_query(service: AgentService = None):
    if service is None:
        service = container.get_agent_service()
```

## Health Monitoring System

### Comprehensive Health Checks

The container provides detailed health monitoring for all dependencies:

```python
def health_check(self) -> dict:
    """Check health of all container dependencies"""
    if not self._initialized:
        return {"status": "not_initialized", "components": {}}
    
    components = {
        "llm_provider": self.llm_provider is not None,
        "sanitizer": self.sanitizer is not None,
        "tracer": self.tracer is not None,
        "tools_count": len(self.tools) if self.tools else 0,
        "agent_service": self.agent_service is not None,
        "data_service": self.data_service is not None,
        "knowledge_service": self.knowledge_service is not None,
    }
    
    all_healthy = all(
        comp if isinstance(comp, bool) else comp > 0
        for comp in components.values()
    )
    
    return {
        "status": "healthy" if all_healthy else "degraded",
        "components": components
    }
```

**Health Status Indicators**:
- **healthy**: All components initialized and available
- **degraded**: Some components missing or failed
- **not_initialized**: Container hasn't been initialized yet

### Monitoring Integration

Health checks integrate with application monitoring:

```python
@router.get("/health/dependencies")
async def check_dependencies():
    container = DIContainer()
    health = container.health_check()
    
    status_code = 200 if health["status"] == "healthy" else 503
    return JSONResponse(content=health, status_code=status_code)
```

## Feature Flag Integration

### Runtime Configuration Control

The container integrates with the feature flag system for dynamic behavior:

```python
from faultmaven.config.feature_flags import (
    USE_REFACTORED_SERVICES,
    USE_DI_CONTAINER,
    ENABLE_MIGRATION_LOGGING
)

def get_service_dependencies(service_type: str):
    """Get appropriate service based on feature flags"""
    container_instance = get_container_type()
    
    if USE_REFACTORED_SERVICES:
        logger.info(f"Using interface-based {service_type} service")
        return getattr(container_instance, f"get_{service_type}_service")()
    else:
        logger.info(f"Using legacy {service_type} service")
        return get_legacy_service(service_type)
```

### Migration Support

Feature flags enable gradual migration from legacy to interface-based services:

```python
def get_container_type():
    """Determine which container to use based on feature flags"""
    if USE_DI_CONTAINER:
        from faultmaven.container import container
        return container
    else:
        # Legacy fallback for backward compatibility
        return get_legacy_container()
```

**Migration Strategies**:
- **Full New**: All services use interface-based implementations
- **Mixed Mode**: Some services refactored, others legacy
- **Legacy Only**: Original implementations for rollback scenarios
- **Testing Mode**: Mock implementations for development

## Error Handling and Resilience

### Initialization Error Recovery

The container handles initialization failures gracefully:

```python
def initialize(self):
    try:
        self._create_infrastructure_layer()
        self._create_tools_layer()
        self._create_service_layer()
        
        self._initialized = True
        logger.info("✅ DI Container initialized successfully")
        
    except Exception as e:
        logger.error(f"❌ DI Container initialization failed: {e}")
        
        if not INTERFACES_AVAILABLE:
            logger.warning("Creating minimal container for testing")
            self._create_minimal_container()
            self._initialized = True
        else:
            logger.error(f"Critical initialization error")
            self._initialized = False
```

### Dependency Failure Handling

Individual dependency failures don't crash the entire container:

```python
def _create_tools_layer(self):
    tools = []
    
    try:
        knowledge_tool = KnowledgeBaseTool(ingester)
        tools.append(knowledge_tool)
    except Exception as e:
        logger.warning(f"KnowledgeBaseTool failed: {e}")
    
    try:
        web_tool = WebSearchTool()
        tools.append(web_tool)
    except Exception as e:
        logger.warning(f"WebSearchTool failed: {e}")
    
    self.tools = tools  # Only include successfully created tools
```

## Testing Integration

### Container Reset for Testing

The container supports full reset for test isolation:

```python
def reset(self):
    """Reset container state (useful for testing)"""
    self._initialized = False
    
    # Clear all cached components
    for attr in ['llm_provider', 'sanitizer', 'tracer', 'tools']:
        if hasattr(self, attr):
            delattr(self, attr)
```

### Test-Specific Configuration

Tests can configure the container for specific scenarios:

```python
def test_with_mock_dependencies():
    container = DIContainer()
    container.reset()
    
    # Set test environment variables
    os.environ["USE_MOCK_LLM"] = "true"
    os.environ["TESTING_MODE"] = "true"
    
    # Initialize with test configuration
    container.initialize()
    
    # Verify mock implementations
    assert isinstance(container.get_llm_provider(), MockLLMProvider)
```

## Performance Considerations

### Lazy Initialization Benefits

- **Faster Startup**: Only create components when needed
- **Memory Efficiency**: Don't load unused dependencies
- **Error Isolation**: Failed components don't prevent application start
- **Development Speed**: Developers can work without all dependencies

### Singleton Efficiency

- **Memory Conservation**: Single instance of each component
- **Consistent State**: All consumers use same component instance
- **Reduced Overhead**: No repeated initialization costs
- **Predictable Behavior**: Consistent behavior across application

### Caching Strategy

The container caches resolved dependencies for performance:

```python
def get_agent_service(self):
    if not hasattr(self, '_cached_agent_service'):
        self.initialize()
        self._cached_agent_service = self.agent_service
    return self._cached_agent_service
```

## Best Practices

### Container Usage Guidelines

1. **Single Responsibility**: Each getter method returns one interface type
2. **Interface Returns**: Always return interface types, not concrete classes
3. **Error Handling**: Gracefully handle missing dependencies
4. **Logging**: Provide clear logging for debugging dependency issues
5. **Health Monitoring**: Include health checks for all dependencies

### Dependency Design Patterns

1. **Constructor Injection**: All dependencies provided at construction time
2. **Interface Segregation**: Dependencies request only needed interface methods
3. **Dependency Inversion**: High-level modules depend on abstractions
4. **Service Locator**: Container acts as central service locator
5. **Factory Pattern**: Container acts as factory for complex object graphs

### Testing Strategies

1. **Mock Injection**: Replace interfaces with mocks for unit tests
2. **Partial Mocking**: Mix real and mock implementations for integration tests
3. **Container Reset**: Reset container state between tests
4. **Environment Isolation**: Use environment variables for test configuration
5. **Health Verification**: Verify container health in integration tests

## Future Enhancements

### Planned Container Features

1. **Lifecycle Management**: Support for component startup/shutdown hooks
2. **Scope Management**: Request-scoped and session-scoped dependencies
3. **Circular Dependency Detection**: Detect and prevent circular dependencies
4. **Performance Monitoring**: Track dependency resolution performance
5. **Dynamic Reconfiguration**: Hot-swap implementations at runtime

### Advanced DI Patterns

1. **Decorator-Based Injection**: Automatic dependency injection via decorators
2. **Configuration-Driven Wiring**: External configuration files for dependency wiring
3. **Plugin Architecture**: Dynamic loading of implementations from plugins
4. **Multi-Tenant Support**: Tenant-specific dependency configurations
5. **Cloud-Native Integration**: Integration with service discovery systems

This dependency injection system provides the foundation for FaultMaven's maintainable, testable, and flexible architecture while supporting smooth migration paths and operational excellence.