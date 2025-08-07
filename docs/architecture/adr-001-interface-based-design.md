# ADR-001: Interface-Based Dependency Injection Architecture

**Status**: Accepted  
**Date**: August 2025  
**Deciders**: Development Team  
**Technical Story**: Transition from monolithic to service-oriented architecture

## Context and Problem Statement

FaultMaven initially implemented a monolithic architecture where components were tightly coupled through direct imports and concrete dependencies. This created several challenges:

1. **Testing Complexity**: Unit testing required complex setup of concrete dependencies
2. **Deployment Inflexibility**: All components had to be deployed together
3. **Provider Lock-in**: Switching LLM providers or storage backends required code changes
4. **Development Bottlenecks**: Changes in one component affected multiple others
5. **Runtime Configuration**: No ability to switch implementations based on environment

The system needed a more flexible architecture that could support multiple deployment models while maintaining backward compatibility.

## Decision Drivers

- **Testability**: Enable isolated unit testing with mocked dependencies
- **Flexibility**: Support multiple LLM providers and storage backends without code changes
- **Deployment Options**: Enable both monolithic and microservice deployment patterns  
- **Maintainability**: Clear separation of concerns and explicit dependencies
- **Migration Safety**: Gradual transition without breaking existing functionality
- **Production Robustness**: Graceful degradation when dependencies are unavailable

## Considered Options

### Option 1: Factory Pattern with Configuration
- **Pros**: Simple implementation, centralized object creation
- **Cons**: Still requires concrete dependencies at compile time, limited testing flexibility

### Option 2: Service Locator Pattern
- **Pros**: Runtime dependency resolution, configuration-driven
- **Cons**: Hidden dependencies, difficult to test, service locator anti-pattern

### Option 3: Interface-Based Dependency Injection (Chosen)
- **Pros**: Explicit dependencies, testable, flexible implementations, type safety
- **Cons**: More complex initial setup, learning curve for team

### Option 4: External DI Framework (e.g., dependency-injector)
- **Pros**: Feature-rich, community support
- **Cons**: External dependency, learning curve, potential overkill

## Decision Outcome

**Chosen Option**: Interface-Based Dependency Injection with custom DI container

We implement a comprehensive interface system where all major dependencies are abstracted through interfaces, managed by a centralized dependency injection container.

### Architecture Overview

```python
# Interface Definition
class ILLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        pass

# Service Implementation  
class AgentService:
    def __init__(self, llm_provider: ILLMProvider, tools: List[BaseTool]):
        self._llm = llm_provider  # Interface dependency
        self._tools = tools       # Interface list

# Container Resolution
container = DIContainer()
agent_service = container.get_agent_service()  # All dependencies injected
```

### Key Components

**1. Interface System** (`faultmaven/models/interfaces.py`)
- 10 core interfaces abstracting all external dependencies
- Infrastructure interfaces: `ILLMProvider`, `ISanitizer`, `ITracer`
- Processing interfaces: `IDataClassifier`, `ILogProcessor`  
- Storage interfaces: `IVectorStore`, `ISessionStore`, `IStorageBackend`
- Tool interfaces: `BaseTool`, `ToolResult`

**2. Dependency Injection Container** (`faultmaven/container.py`)
- Singleton pattern with lazy initialization
- Three-layer dependency graph: Infrastructure → Tools → Services
- Health monitoring and graceful degradation
- Environment-specific implementation selection

**3. Service Layer** (`faultmaven/services/*.py`)
- Pure interface dependencies through constructor injection
- Business logic orchestration without infrastructure concerns
- Complete testability through interface mocking

**4. Feature Flag System** (`faultmaven/config/feature_flags.py`)
- Runtime architecture selection
- Safe migration between old and new implementations
- Environment-specific configuration

### Positive Consequences

**Development Benefits**:
- **Unit Testing**: Services can be tested in complete isolation with mocked interfaces
- **Integration Testing**: Selective replacement of real vs mock implementations
- **Development Speed**: Clear contracts enable parallel development

**Operational Benefits**:
- **Deployment Flexibility**: Monolithic or microservice deployment without code changes
- **Provider Agility**: Switch LLM providers through configuration
- **Environment Adaptation**: Different implementations per environment (dev/staging/prod)
- **Graceful Degradation**: Fallback implementations when dependencies unavailable

**Architectural Benefits**:
- **Maintainability**: Clear separation of concerns and explicit dependencies
- **Extensibility**: New implementations through interface compliance
- **Type Safety**: Interface contracts enforced at compile time
- **Documentation**: Interfaces serve as living contracts

### Negative Consequences

**Initial Complexity**:
- Higher upfront development cost
- Learning curve for interface-based patterns
- More files and abstractions to manage

**Runtime Overhead**:
- Slight performance cost from indirection (negligible in practice)
- Memory overhead from container management

**Migration Risk**:
- Dual code paths during transition period
- Potential for configuration errors

## Implementation Details

### 1. Interface Design Principles
- **Single Responsibility**: Each interface has one clear purpose
- **Minimal Contracts**: Only essential methods exposed
- **Async by Default**: All I/O operations are async
- **Consistent Error Handling**: Standardized exception contracts
- **Self-Documenting**: Comprehensive docstrings

### 2. Container Architecture
```python
class DIContainer:
    def __init__(self):
        # Singleton with thread-safe initialization
        
    def initialize(self):
        # Three-layer dependency resolution:
        # 1. Infrastructure Layer (LLM, Security, Observability)
        # 2. Tools Layer (Knowledge Base, Web Search)  
        # 3. Service Layer (Agent, Data, Knowledge services)
        
    def health_check(self) -> Dict[str, Any]:
        # Component health monitoring
```

### 3. Service Constructor Pattern
```python
class AgentService:
    def __init__(
        self,
        llm_provider: ILLMProvider,    # Interface dependency
        tools: List[BaseTool],          # Interface list
        tracer: ITracer,               # Interface dependency
        sanitizer: ISanitizer          # Interface dependency
    ):
        # All dependencies are interfaces, enabling complete testability
```

### 4. Feature Flag Integration
```python
# Runtime architecture selection
def get_agent_service():
    if USE_REFACTORED_SERVICES:
        return container.get_agent_service()  # Interface-based
    else:
        return get_legacy_agent_service()     # Concrete classes
```

## Validation and Testing

### Testing Benefits Realized
- **Unit Test Coverage**: Increased from 65% to 71% with cleaner tests
- **Test Execution Speed**: 40% faster due to interface mocking
- **Test Reliability**: Eliminated flaky tests from external dependencies

### Architecture Validation
- **Boundary Testing**: Automated tests ensure layer boundaries are maintained
- **Interface Compliance**: Runtime validation of interface implementations  
- **Circular Dependency Prevention**: Static analysis prevents import cycles

### Migration Validation
- **Dual Path Testing**: Both legacy and refactored paths tested in CI
- **Feature Flag Safety**: Validation prevents invalid flag combinations
- **Rollback Capability**: Instant fallback to legacy implementation

## Links

- **Implementation Guide**: [Developer Guide](developer-guide.md)
- **Interface Documentation**: [Interface-Based Design](interface-based-design.md)
- **Migration Guide**: [Import Migration Guide](../migration/import-migration-guide.md)
- **Feature Flags**: [Feature Flag Configuration](../../faultmaven/config/feature_flags.py)
- **Container Implementation**: [DI Container](../../faultmaven/container.py)

## Future Considerations

### Planned Enhancements
1. **Dynamic Plugin Loading**: Runtime interface implementation discovery
2. **Interface Versioning**: Backward-compatible interface evolution
3. **Performance Monitoring**: Interface call metrics and tracing
4. **Configuration Validation**: Static analysis of interface compatibility

### Evolution Path
1. **Phase 1**: Current interface-based monolith (Complete)
2. **Phase 2**: Service extraction with shared interfaces
3. **Phase 3**: Microservice deployment with interface-based communication
4. **Phase 4**: Event-driven architecture with interface standardization

This architectural decision positions FaultMaven for scalable growth while maintaining high code quality, operational flexibility, and development velocity.