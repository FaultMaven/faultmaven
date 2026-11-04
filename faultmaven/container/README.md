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

# Release the hold; the last one closes what the composition opened
await container.shutdown()
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

    llm = create_llm_provider(container.settings, sanitizer)
    container._register_service("llm_provider", llm)
```

### Ownership and shutdown

A provider records each resource it opens with `own()`, on the line that opens
it, so a composition refused part-way strands nothing it built:

```python
kb_client = container.own(
    create_kb_chromadb_client(settings),
    close=close_chroma_client,
    what="the KB ChromaDB client",
)
```

The composition owns the real Redis client, the KB and evidence ChromaDB
clients and the sanitizer's Presidio session. The FakeRedis stand-in belongs
to its event loop and is never recorded. `shutdown()` names no resource.

- **Holders.** Each `initialize()` that leaves the container initialized is a
  holder: the composition, and every later call that finds it initialized and
  shares it (a second lifespan in the process, a job beside one). A call that
  finds it still initializing holds nothing. `shutdown()` releases one and
  returns whether it was the last.
- **The last release** closes everything owned, newest first, through
  `run_teardown`'s rules (every close runs, a failed one is logged, an
  interruption is held until all have run), then resets. A shutdown with no
  holder closes whatever is owned, which is how a refused composition is
  undone. Earlier releases close nothing (#1843).
- **Resets.** `reset()` and `reset_singleton()` raise while anything is owned,
  naming it: shut down first. The record and the count are process state on
  `BaseDIContainer`, beside `_instance`, so a reset does not rebuild them.
- **The database engine** stays process-global, outside the record. Every
  composer (`decompose_application`, `run_with_container`, the jobs runner)
  disposes it only when `shutdown()` returns True. The holder count is a proxy
  for "the process is done serving", not a count of the engine's users:
  `get_engine()` opens it lazily for any caller. `run_command` composes no
  container and disposes it directly.

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
| Tools | `tools.py` | document_qa_tools |
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

Take the `reset_container` fixture (`tests/conftest.py`): it resets the
container before the test and releases every hold the test took after it, so
the last release closes what the test's compositions opened. A test that
composes without it shuts the container down itself; a reset over what it
left owned is refused.
