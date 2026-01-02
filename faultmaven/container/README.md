# FaultMaven Container Package

Modular Dependency Injection container for FaultMaven.

## Package Structure

```
faultmaven/container/
├── __init__.py          # Package exports
├── base.py              # BaseDIContainer with registry integration
├── errors.py            # Container-specific exceptions
├── registry.py          # DependencyRegistry for service lifecycle
├── utils.py             # LazyService, decorators
└── providers/           # Service factory modules
    ├── __init__.py
    ├── infrastructure.py  # LLM, storage, security factories
    ├── services.py        # Business logic service factories
    └── tools.py           # Tool registry factories
```

## Quick Start

```python
from faultmaven.container import container

# Initialize container (async)
await container.initialize()

# Get services
llm = container.get_service("llm_provider", required=True)
sanitizer = container.get_sanitizer()

# Check health
health = container.health_check()
```

## Architecture

### BaseDIContainer

Core container with registry integration:

```python
from faultmaven.container import BaseDIContainer

class DIContainer(BaseDIContainer):
    async def initialize(self):
        self.settings = get_settings()
        self._register_service("settings", self.settings)

        await register_infrastructure(self)
        register_tools(self)
        register_services(self)
```

### DependencyRegistry

Tracks service lifecycle and dependencies:

```python
from faultmaven.container import DependencyRegistry, ServiceStatus

registry = DependencyRegistry()

# Register with dependencies
registry.register("child", dependencies=["parent"])
registry.set_instance("parent", parent_instance)
registry.set_instance("child", child_instance)

# Query status
info = registry.get_info("child")
assert info.status == ServiceStatus.READY

# Detect circular dependencies
order = registry.get_initialization_order()  # Raises CircularDependencyError
```

### Service Providers

Factory modules for service creation:

```python
# providers/infrastructure.py
async def register_infrastructure(container):
    sanitizer = create_sanitizer(container.settings)
    container._register_service("sanitizer", sanitizer)

    llm = create_llm_provider()
    container._register_service("llm_provider", llm)
```

### Exceptions

```python
from faultmaven.container import (
    ContainerError,           # Base exception
    ServiceUnavailableError,  # Service not available
    InitializationError,      # Startup failure
    CircularDependencyError,  # Dependency cycle detected
    ConfigurationError,       # Invalid config
)
```

### Utilities

```python
from faultmaven.container import LazyService, service_getter

# Lazy initialization
lazy_model = LazyService(lambda: load_expensive_model())
model = lazy_model.get()  # Only loads on first access

# Standardized getter decorator
class Container:
    @service_getter("_llm", required=True)
    def get_llm(self):
        pass
```

## Service Registration

Services are registered during initialization:

| Layer | Provider | Key Services |
|-------|----------|--------------|
| Infrastructure | `infrastructure.py` | sanitizer, tracer, llm_provider, vector_store |
| Tools | `tools.py` | knowledge_ingester, document_qa_tools |
| Services | `services.py` | case_service, session_service, knowledge_service |

## Health Check

```python
health = container.health_check()
# Returns:
# {
#     "status": "healthy" | "degraded" | "not_initialized",
#     "initialized": true,
#     "components": {"llm_provider": true, "sanitizer": true, ...},
#     "registry": {...}
# }
```

## Testing

Reset container between tests:

```python
@pytest.fixture(autouse=True)
def reset_container():
    DIContainer.reset_singleton()
    yield
    DIContainer.reset_singleton()
```
