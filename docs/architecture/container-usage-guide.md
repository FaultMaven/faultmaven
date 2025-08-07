# Dependency Injection Container Usage Guide

**Document Type**: Developer Guide  
**Last Updated**: January 2025  
**Status**: Production Ready

## Overview

This guide provides practical instructions for using FaultMaven's dependency injection (DI) container system. The `DIContainer` class manages all service dependencies through interface-based design, providing centralized dependency resolution, health monitoring, and graceful degradation.

## Quick Start

### Basic Container Usage

```python
from faultmaven.container import container

# Get services through interface resolution
agent_service = container.get_agent_service()
llm_provider = container.get_llm_provider()  # Returns ILLMProvider implementation
data_service = container.get_data_service()

# Check container health
health = container.health_check()
print(f"Container status: {health['status']}")  # healthy | degraded | not_initialized
```

### FastAPI Integration

```python
# api/v1/dependencies.py
from faultmaven.container import container
from faultmaven.services.agent_service import AgentService

def get_agent_service() -> AgentService:
    """FastAPI dependency for agent service"""
    return container.get_agent_service()

# api/v1/routes/agent.py
from fastapi import APIRouter, Depends

router = APIRouter()

@router.post("/troubleshoot")
async def troubleshoot_issue(
    query: TroubleshootRequest,
    agent_service: AgentService = Depends(get_agent_service)
):
    # Service is automatically injected with all dependencies
    result = await agent_service.process_query(query.message, query.session_id)
    return result
```

## Container Architecture

### Singleton Pattern

The container follows a singleton pattern ensuring consistent dependency management:

```python
from faultmaven.container import DIContainer

# All instances return the same container
container1 = DIContainer()
container2 = DIContainer()
assert container1 is container2  # True

# Global proxy for convenience
from faultmaven.container import container
assert container() is container1  # True
```

### Layered Initialization

The container initializes dependencies in layers with proper dependency resolution:

```python
class DIContainer:
    def initialize(self):
        """Initialize all dependencies in dependency order"""
        try:
            # Layer 1: Infrastructure (no dependencies)
            self._create_infrastructure_layer()
            
            # Layer 2: Tools (depend on infrastructure)
            self._create_tools_layer()
            
            # Layer 3: Services (depend on infrastructure + tools)
            self._create_service_layer()
            
            self._initialized = True
        except Exception as e:
            # Graceful degradation with mock implementations
            self._create_minimal_container()
```

## Service Resolution

### Available Services

The container provides the following service getters:

```python
# Core Services
agent_service = container.get_agent_service()        # AgentService
data_service = container.get_data_service()          # DataService  
knowledge_service = container.get_knowledge_service() # KnowledgeService
session_service = container.get_session_service()    # SessionService

# Infrastructure Components (Interface Implementations)
llm_provider = container.get_llm_provider()          # ILLMProvider
sanitizer = container.get_sanitizer()               # ISanitizer
tracer = container.get_tracer()                     # ITracer

# Processing Components
classifier = container.get_data_classifier()         # IDataClassifier
log_processor = container.get_log_processor()       # ILogProcessor

# Tools Collection
tools = container.get_tools()                       # List[BaseTool]
```

### Service Dependencies

Services are automatically injected with all required dependencies:

```python
# AgentService receives these dependencies automatically:
class AgentService:
    def __init__(
        self,
        llm_provider: ILLMProvider,      # Multi-provider routing
        tools: List[BaseTool],           # Knowledge base + web search
        tracer: ITracer,                 # Opik tracing
        sanitizer: ISanitizer            # PII redaction
    ):
        # All dependencies are interface implementations
        self.llm_provider = llm_provider
        self.tools = tools
        self.tracer = tracer
        self.sanitizer = sanitizer
```

### Lazy Initialization

Services are created only when first requested:

```python
# Container starts empty
container = DIContainer()
print(container._initialized)  # False

# First access triggers initialization
agent_service = container.get_agent_service()
print(container._initialized)  # True

# Subsequent access returns cached instance
agent_service2 = container.get_agent_service()
assert agent_service is agent_service2  # Same instance
```

## Interface-Based Dependencies

### Infrastructure Interfaces

All infrastructure components are accessed through interfaces:

```python
from faultmaven.models.interfaces import ILLMProvider, ISanitizer, ITracer

# Container returns interface implementations
llm_provider: ILLMProvider = container.get_llm_provider()
sanitizer: ISanitizer = container.get_sanitizer()
tracer: ITracer = container.get_tracer()

# Can be any implementation (production, mock, etc.)
assert isinstance(llm_provider, ILLMProvider)  # True
```

### Implementation Selection

The container automatically selects appropriate implementations:

```python
def _create_infrastructure_layer(self):
    # Environment-based implementation selection
    if os.getenv("USE_MOCK_LLM") == "true":
        self.llm_provider = MockLLMProvider()
    else:
        # Production: Multi-provider routing with 7 providers
        self.llm_provider = LLMRouter()
    
    # Presidio integration with local fallback
    if self._is_presidio_available():
        self.sanitizer = DataSanitizer()  # K8s microservice
    else:
        self.sanitizer = LocalSanitizer()  # Regex fallback
    
    # Opik tracing with graceful degradation
    try:
        self.tracer = OpikTracer()
    except Exception:
        self.tracer = NoOpTracer()  # Disabled tracing
```

## Health Monitoring

### Container Health Check

```python
# Comprehensive health status
health = container.health_check()

print(f"Status: {health['status']}")  # healthy | degraded | not_initialized
print("Components:")
for component, status in health['components'].items():
    icon = "✅" if status else "❌"
    print(f"  {icon} {component}: {status}")

# Example output:
# Status: healthy
# Components:
#   ✅ llm_provider: True
#   ✅ sanitizer: True
#   ✅ tracer: True
#   ✅ tools_count: 2
#   ✅ agent_service: True
#   ✅ data_service: True
#   ✅ knowledge_service: True
```

### HTTP Health Endpoint

```python
# Health endpoint integration (main.py)
@app.get("/health/dependencies")
async def check_dependencies():
    health = container.health_check()
    status_code = 200 if health["status"] == "healthy" else 503
    return JSONResponse(content=health, status_code=status_code)
```

```bash
# Check container health via API
curl http://localhost:8000/health/dependencies

# Response
{
  "status": "healthy",
  "components": {
    "llm_provider": true,
    "sanitizer": true,
    "tracer": true,
    "tools_count": 2,
    "agent_service": true,
    "data_service": true,
    "knowledge_service": true
  }
}
```

### Component Status Details

```python
# Detailed component inspection
if health["status"] == "degraded":
    # Identify failed components
    failed_components = [
        name for name, status in health["components"].items() 
        if not status
    ]
    print(f"Failed components: {failed_components}")
    
    # Check specific providers
    llm_provider = container.get_llm_provider()
    if hasattr(llm_provider, 'get_provider_status'):
        provider_status = llm_provider.get_provider_status()
        for name, info in provider_status.items():
            if not info['available']:
                print(f"❌ LLM Provider '{name}' unavailable")
```

## Testing Integration

### Container Reset

```python
import pytest
from faultmaven.container import DIContainer

@pytest.fixture(autouse=True)
def reset_container():
    """Reset container state between tests"""
    container = DIContainer()
    container.reset()
    yield
    container.reset()  # Cleanup after test
```

### Mock Implementations

```python
# Test with mock dependencies
def test_agent_service_with_mocks():
    # Reset container
    container = DIContainer()
    container.reset()
    
    # Set test environment
    os.environ["USE_MOCK_LLM"] = "true"
    os.environ["TESTING_MODE"] = "true"
    
    # Get service with mock dependencies
    agent_service = container.get_agent_service()
    
    # Verify mock implementations
    assert isinstance(agent_service.llm_provider, MockLLMProvider)
    assert isinstance(agent_service.sanitizer, MockSanitizer)
```

### Custom Test Configuration

```python
# Override specific dependencies for testing
class TestContainer(DIContainer):
    def _create_infrastructure_layer(self):
        # Custom test implementations
        self.llm_provider = MockLLMProvider()
        self.sanitizer = MockSanitizer() 
        self.tracer = MockTracer()
        
        # Real implementations for other components
        super()._create_infrastructure_layer()

# Use in tests
def test_with_custom_container():
    original_container = DIContainer._instance
    DIContainer._instance = TestContainer()
    
    try:
        agent_service = container.get_agent_service()
        # Test with custom implementations
        ...
    finally:
        DIContainer._instance = original_container
```

## Configuration and Environment

### Environment-Based Configuration

The container adapts to different environments:

```python
# Development environment
ENVIRONMENT=development
USE_MOCK_LLM=false
OPIK_ENABLED=true

# Testing environment  
ENVIRONMENT=testing
USE_MOCK_LLM=true
TESTING_MODE=true

# Production environment
ENVIRONMENT=production
USE_MOCK_LLM=false
OPIK_ENABLED=true
```

### Feature Flag Integration

```python
from faultmaven.config.feature_flags import USE_REFACTORED_SERVICES, USE_DI_CONTAINER

def get_service_implementation():
    """Get service based on feature flags"""
    if USE_DI_CONTAINER and USE_REFACTORED_SERVICES:
        # Use interface-based services from container
        return container.get_agent_service()
    else:
        # Legacy service instantiation
        return LegacyAgentService()
```

### Custom Configuration

```python
# Custom container configuration
class CustomContainer(DIContainer):
    def _create_infrastructure_layer(self):
        # Custom LLM provider configuration
        if os.getenv("CUSTOM_LLM_PROVIDER"):
            self.llm_provider = CustomLLMProvider()
        else:
            super()._create_infrastructure_layer()
    
    def _create_service_layer(self):
        # Custom service configuration
        self.agent_service = EnhancedAgentService(
            llm_provider=self.llm_provider,
            tools=self.tools,
            tracer=self.tracer,
            sanitizer=self.sanitizer,
            custom_feature=True  # Additional feature
        )
```

## Error Handling and Resilience

### Graceful Degradation

The container handles initialization failures gracefully:

```python
def initialize(self):
    try:
        # Attempt full initialization
        self._create_infrastructure_layer()
        self._create_tools_layer()
        self._create_service_layer()
        self._initialized = True
        logger.info("✅ DI Container initialized successfully")
        
    except Exception as e:
        logger.error(f"❌ DI Container initialization failed: {e}")
        
        # Fallback to minimal container for testing/development
        try:
            self._create_minimal_container()
            self._initialized = True
            logger.warning("⚠️ Using minimal container with mock implementations")
        except Exception:
            self._initialized = False
            raise
```

### Minimal Container for Testing

```python
def _create_minimal_container(self):
    """Create minimal container with mock implementations"""
    from unittest.mock import MagicMock
    
    # Mock infrastructure layer
    self.llm_provider = MagicMock(spec=ILLMProvider)
    self.sanitizer = MagicMock(spec=ISanitizer)
    self.tracer = MagicMock(spec=ITracer)
    
    # Empty tools list
    self.tools = []
    
    # Mock service layer
    self.agent_service = MagicMock(spec=AgentService)
    self.data_service = MagicMock(spec=DataService)
    self.knowledge_service = MagicMock(spec=KnowledgeService)
    
    logger.info("Created minimal container for testing environment")
```

### Individual Component Failure Handling

```python
def _create_tools_layer(self):
    """Create tools with error isolation"""
    tools = []
    
    # Knowledge Base Tool (may fail if ChromaDB unavailable)
    try:
        knowledge_tool = KnowledgeBaseTool(
            knowledge_ingester=self.get_knowledge_ingester()
        )
        tools.append(knowledge_tool)
        logger.info("✅ KnowledgeBaseTool initialized")
    except Exception as e:
        logger.warning(f"⚠️ KnowledgeBaseTool failed: {e}")
    
    # Web Search Tool (may fail if API keys missing)
    try:
        web_search_tool = WebSearchTool()
        tools.append(web_search_tool)
        logger.info("✅ WebSearchTool initialized")
    except Exception as e:
        logger.warning(f"⚠️ WebSearchTool failed: {e}")
    
    self.tools = tools
    logger.info(f"Initialized {len(tools)} tools")
```

## Performance Considerations

### Singleton Benefits

- **Memory Efficiency**: Single instance of each component
- **Consistent State**: All consumers use same component instances
- **Reduced Overhead**: No repeated initialization costs
- **Predictable Behavior**: Consistent behavior across application

### Lazy Initialization Benefits

- **Faster Startup**: Only create components when needed
- **Memory Conservation**: Don't load unused dependencies  
- **Error Isolation**: Failed components don't prevent application start
- **Development Speed**: Developers can work without all dependencies

### Caching Strategy

```python
def get_agent_service(self):
    """Get agent service with caching"""
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
4. **Health Monitoring**: Include health checks for all dependencies
5. **Testing Support**: Provide mock implementations for testing

### Development Workflow

```python
# 1. Define interfaces first
class INewService(ABC):
    @abstractmethod
    def process(self, data: str) -> str: ...

# 2. Implement concrete class
class NewService(INewService):
    def __init__(self, dependency: ISomeDependency):
        self.dependency = dependency
    
    def process(self, data: str) -> str:
        return self.dependency.transform(data)

# 3. Add to container
def get_new_service(self) -> INewService:
    self.initialize()
    return self.new_service

def _create_service_layer(self):
    # ... existing services ...
    self.new_service = NewService(
        dependency=self.some_dependency
    )
```

### Testing Strategies

```python
# 1. Interface-based mocking
@patch.object(DIContainer, 'get_llm_provider')
def test_with_mock_llm(mock_get_llm):
    mock_llm = MagicMock(spec=ILLMProvider)
    mock_get_llm.return_value = mock_llm
    
    service = container.get_agent_service()
    # Test with mock implementation

# 2. Container state isolation
def test_with_clean_container():
    container.reset()
    os.environ["TEST_MODE"] = "true"
    
    service = container.get_agent_service()
    # Test with fresh container state

# 3. Health validation
def test_container_health():
    health = container.health_check()
    assert health["status"] in ["healthy", "degraded"]
    assert "llm_provider" in health["components"]
```

## Troubleshooting

### Common Issues

**Container Not Initialized**:
```python
# Problem: Accessing services before initialization
agent_service = container.get_agent_service()  # May fail

# Solution: Check initialization status
if not container._initialized:
    container.initialize()
agent_service = container.get_agent_service()
```

**Circular Dependencies**:
```python
# Problem: Service A depends on Service B, which depends on Service A
# Solution: Use property injection or event-based communication

class ServiceA:
    def __init__(self, service_b: IServiceB):
        self._service_b = service_b
        
    @property
    def service_b(self) -> IServiceB:
        return self._service_b
```

**Mock Not Working in Tests**:
```python
# Problem: Container returns cached real instance
# Solution: Reset container before each test

@pytest.fixture(autouse=True)
def reset_container():
    DIContainer._instance = None
    yield
    DIContainer._instance = None
```

### Debug Information

```python
# Container debug information
def debug_container():
    health = container.health_check()
    print(f"Container Status: {health['status']}")
    print(f"Initialized: {container._initialized}")
    
    if hasattr(container, 'llm_provider'):
        llm_provider = container.get_llm_provider()
        print(f"LLM Provider: {type(llm_provider).__name__}")
        
        if hasattr(llm_provider, 'get_provider_status'):
            status = llm_provider.get_provider_status()
            print("LLM Provider Status:")
            for name, info in status.items():
                print(f"  {name}: {'✅' if info['available'] else '❌'}")
```

## Migration Guide

### From Direct Instantiation

```python
# Before (direct instantiation)
llm_router = LLMRouter()
sanitizer = DataSanitizer()
tracer = OpikTracer()
tools = [KnowledgeBaseTool(), WebSearchTool()]

agent_service = AgentService(
    llm_provider=llm_router,
    sanitizer=sanitizer,
    tracer=tracer,
    tools=tools
)

# After (container-based)
agent_service = container.get_agent_service()
# All dependencies automatically injected
```

### Gradual Adoption

```python
# Mixed approach during migration
def get_agent_service():
    if USE_DI_CONTAINER:
        return container.get_agent_service()
    else:
        # Legacy instantiation
        return create_legacy_agent_service()
```

This dependency injection container provides the foundation for FaultMaven's maintainable, testable, and flexible architecture while supporting smooth migration paths and operational excellence.