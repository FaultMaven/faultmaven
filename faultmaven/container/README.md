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

- **Holds belong to the caller's context.** An `initialize()` that leaves the
  container initialized takes one hold for its caller's context: the
  composition, and a later call that finds it initialized and shares it. One
  that raises, returns degraded or finds a composition in progress takes none.
  The same context's `shutdown()` releases the hold, so a composer that holds
  nothing releases nobody else's. A lifespan, a job run and an `fm-*` command
  each compose and shut down in one context. Two lifespans nested in one
  coroutine share a context and hold twice.
- **A stranded hold:** a successful composition whose context never shuts down
  keeps every later release from being the last, until a reset. One case is an
  `initialize()` run in a task of its own (`create_task`, or `wait_for` on
  Python 3.11): it holds in that task's context, which no other context's
  shutdown releases.
- **The last release.** `shutdown()` returns `False`, closing nothing, while a
  hold remains anywhere (#1843). With none left it resets first, so an
  `initialize()` during the closes composes afresh and keeps its hold. Then it
  closes everything owned, newest first, through `run_teardown`'s rules (every
  close runs, a failed one is logged, an interruption is held until all have
  run), and returns `True`. With no hold anywhere it closes whatever is owned,
  which is how a refused composition is undone.
- **Resets.** `reset()` raises while anything is owned, naming it: shut down
  first. The record and the holds are the container instance's, rebuilt by a
  reset, so a hold taken before a reset is void after it. Tests end every hold
  with `release_every_hold()` (the `reset_container` fixture).
- **What is the process's** goes only on the last release. Every composer
  releases through `ContainerRelease` (`concurrency/teardown.py`), and only
  when `shutdown()` does not answer `False` does it dispose the
  process-global database engine. The lifespan's decomposition also uninstalls
  the usage ledger, drains its writes, and stops the APM export then. The hold
  count is a proxy for "the process is done serving", not a count of the
  engine's users: `get_engine()` opens it lazily for any caller. `run_command`
  composes no container and disposes it directly.
- **Residual: the event loop.** With holders on two loops, the last release
  closes the Redis client on its own loop, which need not be the one the
  client was opened on, and each lifespan's ledger drain cancels the other
  loop's pending writes. A production process runs one loop, and no test
  overlaps holders over a real Redis client.

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
