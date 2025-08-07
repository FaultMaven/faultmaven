# FaultMaven Developer Onboarding Guide

**Document Type**: Developer Guide  
**Last Updated**: August 2025  
**Status**: Active - Post-Refactoring Complete

## Overview

Welcome to FaultMaven! This guide helps new developers quickly understand our clean architecture, development workflow, and best practices. FaultMaven has recently completed a major architectural refactoring, resulting in a modern, interface-based system that prioritizes testability, maintainability, and deployment flexibility.

## Quick Start (5 Minutes)

### 1. Environment Setup

```bash
# Clone and setup
git clone <repository-url>
cd FaultMaven
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# or .venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-test.txt

# Download language model for processing
python -m spacy download en_core_web_lg
```

### 2. Basic Configuration

Create `.env` file:

```bash
# Minimal working configuration
CHAT_PROVIDER="local"
LOCAL_LLM_URL="http://localhost:5000"  # Or use real provider
REDIS_URL="redis://localhost:6379"

# Optional: Add real LLM provider
FIREWORKS_API_KEY="fw_your_key"
CHAT_PROVIDER="fireworks"
```

### 3. Start Development

```bash
# Simple startup (loads .env automatically)
./run_faultmaven.sh

# Or manual startup
python -m faultmaven.main

# Run tests to verify setup
python run_tests.py --unit
```

## Architecture Overview (Interface-Based Design)

### Core Philosophy

FaultMaven uses **interface-based dependency injection** throughout. This means:

- **All dependencies are abstractions (interfaces)**
- **Services receive interfaces, not concrete classes**
- **Easy testing with mocked interfaces**
- **Flexible deployment options**

### Layer Structure

```
API Layer (FastAPI routes)
    ↓ [Uses dependency injection]
Service Layer (Business logic)
    ↓ [Uses interface contracts]
Core Domain (Business entities)
    ↓ [Uses interface abstractions]
Infrastructure (External integrations)
```

### Key Interfaces

Located in `faultmaven/models/interfaces.py`:

```python
# Infrastructure Interfaces
ILLMProvider     # AI model interaction
ISanitizer       # Data cleaning/PII removal
ITracer          # Observability/tracing
IVectorStore     # Knowledge base storage
ISessionStore    # Session management

# Processing Interfaces  
IDataClassifier  # Data type detection
ILogProcessor    # Log analysis
IStorageBackend  # General storage

# Tool Interfaces
BaseTool         # Agent capabilities
```

## Understanding Services

### Service Constructor Pattern

All services follow the same pattern - **constructor injection of interfaces**:

```python
class AgentService:
    def __init__(
        self,
        llm_provider: ILLMProvider,    # Interface dependency
        tools: List[BaseTool],         # Interface list  
        tracer: ITracer,              # Interface dependency
        sanitizer: ISanitizer         # Interface dependency
    ):
        self._llm = llm_provider      # Store interface reference
        self._tools = tools           # Store interface list
        self._tracer = tracer         # Store interface reference
        self._sanitizer = sanitizer   # Store interface reference
```

**Key Points**:
- Services depend **only on interfaces**, never concrete classes
- All dependencies provided via constructor
- Services are **completely testable** with interface mocks
- No direct imports of infrastructure components

### Service Examples

**AgentService** - AI troubleshooting workflows:
```python
# Usage in API routes
agent_service = container.get_agent_service()  # All dependencies injected
result = await agent_service.process_query(user_input)
```

**DataService** - Data processing and analysis:
```python
# Usage example
data_service = container.get_data_service()  # All dependencies injected  
analysis = await data_service.analyze_logs(log_data)
```

**KnowledgeService** - Knowledge base operations:
```python
# Usage example
knowledge_service = container.get_knowledge_service()  # All dependencies injected
search_results = await knowledge_service.search(query)
```

## Dependency Injection System

### Global Container Access

The `DIContainer` provides all services with dependencies pre-injected:

```python
from faultmaven.container import container

# Get services (all dependencies automatically injected)
agent_service = container.get_agent_service()
data_service = container.get_data_service()
knowledge_service = container.get_knowledge_service()

# Get infrastructure components
llm_provider = container.get_llm_provider()
sanitizer = container.get_sanitizer()
tracer = container.get_tracer()
```

### Container Features

- **Singleton Pattern**: One instance across application
- **Lazy Initialization**: Components created only when needed
- **Health Monitoring**: Built-in health checks for all dependencies  
- **Graceful Fallbacks**: Mock implementations when dependencies unavailable
- **Environment-Specific**: Different implementations per environment

## Development Workflow

### 1. Adding New Features

**Step 1**: Define interface (if needed)
```python
# faultmaven/models/interfaces.py
class INewFeature(ABC):
    @abstractmethod
    async def do_something(self, input_data: str) -> str:
        """Process input and return result"""
        pass
```

**Step 2**: Implement interface
```python
# faultmaven/infrastructure/new_feature.py
class NewFeatureImpl(INewFeature):
    async def do_something(self, input_data: str) -> str:
        # Implementation logic
        return processed_data
```

**Step 3**: Add to container
```python
# faultmaven/container.py
def _create_infrastructure_layer(self):
    # ... existing components ...
    self.new_feature = NewFeatureImpl()
```

**Step 4**: Use in service
```python
# faultmaven/services/some_service.py
class SomeService:
    def __init__(self, new_feature: INewFeature):
        self._new_feature = new_feature
    
    async def use_feature(self, data: str):
        return await self._new_feature.do_something(data)
```

### 2. Testing Strategy

**Unit Tests** - Test services in isolation:
```python
import pytest
from unittest.mock import AsyncMock

@pytest.fixture
def mock_llm_provider():
    mock = AsyncMock()
    mock.generate.return_value = "Mock response"
    return mock

@pytest.fixture  
def agent_service(mock_llm_provider):
    return AgentService(
        llm_provider=mock_llm_provider,
        tools=[],
        tracer=AsyncMock(),
        sanitizer=AsyncMock()
    )

async def test_agent_service_query(agent_service):
    result = await agent_service.process_query("test query")
    assert result is not None
```

**Integration Tests** - Test with real dependencies:
```python
@pytest.fixture
def container():
    # Use real container with test configuration
    container = DIContainer()
    container.initialize()
    return container

async def test_full_workflow(container):
    agent_service = container.get_agent_service()
    result = await agent_service.process_query("integration test")
    assert result.success
```

### 3. Running Tests

```bash
# All tests with coverage
python run_tests.py --all

# Unit tests only
python run_tests.py --unit

# Integration tests
python run_tests.py --integration

# Specific test category
pytest -m "unit"       # Unit tests
pytest -m "api"        # API tests  
pytest -m "security"   # Security tests
```

## LLM Provider System

### Quick Provider Setup

FaultMaven supports 7 LLM providers out of the box. Just add API keys to use them:

```bash
# High performance, recommended
FIREWORKS_API_KEY="fw_your_key"
CHAT_PROVIDER="fireworks"

# Highest quality
OPENAI_API_KEY="sk_your_key"  
CHAT_PROVIDER="openai"

# Best reasoning
ANTHROPIC_API_KEY="sk-ant_your_key"
CHAT_PROVIDER="anthropic"

# Free/Local development
CHAT_PROVIDER="local"  # No API key needed
```

### Automatic Fallbacks

The system automatically creates fallback chains:
- If primary provider fails → Try Fireworks → Try OpenAI → Try Local
- Completely transparent to your application code

### Provider Status

Check which providers are available:
```python
from faultmaven.infrastructure.llm.providers.registry import get_registry

registry = get_registry()
print("Available:", registry.get_available_providers())
print("Fallback chain:", registry.get_fallback_chain())
```

## API Development

### Route Structure

API routes are **thin controllers** that delegate to services:

```python
# faultmaven/api/v1/routes/agent.py
from fastapi import APIRouter, Depends
from faultmaven.api.v1.dependencies import get_agent_service

router = APIRouter(prefix="/agent")

@router.post("/query")
async def process_query(
    request: QueryRequest,
    agent_service = Depends(get_agent_service)  # Dependency injection
):
    # Thin controller - just delegate to service
    result = await agent_service.process_query(request.query)
    return QueryResponse(result=result)
```

### Dependency Injection in FastAPI

```python  
# faultmaven/api/v1/dependencies.py
from faultmaven.container import container

def get_agent_service():
    """FastAPI dependency to get agent service with all dependencies injected"""
    return container.get_agent_service()

def get_data_service():
    """FastAPI dependency to get data service with all dependencies injected"""
    return container.get_data_service()
```

## Common Development Tasks

### 1. Adding New API Endpoint

```python
# 1. Add to route file
@router.post("/new-endpoint")
async def new_endpoint(
    request: NewRequest,
    service = Depends(get_appropriate_service)
):
    return await service.handle_new_request(request)

# 2. Add method to service
class AppropriateService:
    async def handle_new_request(self, request: NewRequest):
        # Business logic using injected interfaces
        return result

# 3. Add tests
async def test_new_endpoint(client, mock_service):
    response = client.post("/api/v1/new-endpoint", json={...})
    assert response.status_code == 200
```

### 2. Adding New LLM Provider

```python
# 1. Add to provider schema (if using existing pattern)
# Edit: faultmaven/infrastructure/llm/providers/registry.py
"new_provider": {
    "api_key_var": "NEW_PROVIDER_API_KEY",
    "model_var": "NEW_PROVIDER_MODEL", 
    "default_model": "some-model-name",
    "provider_class": CompatibleProviderClass,
    # ... other config
}

# 2. Set environment variables
NEW_PROVIDER_API_KEY="your_key"
CHAT_PROVIDER="new_provider"

# That's it! Provider is automatically available
```

### 3. Adding Observability

All services support tracing via the `ITracer` interface:

```python
class YourService:
    def __init__(self, tracer: ITracer):
        self._tracer = tracer
    
    async def your_method(self):
        with self._tracer.trace("your_operation"):
            # Your logic here
            result = await self._do_work()
            self._tracer.log_event("operation_completed", {"result": result})
            return result
```

## Debugging and Troubleshooting

### 1. Container Health Check

```python
from faultmaven.container import container

# Check container health
health = container.health_check()
print("Container status:", health["status"])
print("Components:", health["components"])
```

### 2. Provider Status

```python
from faultmaven.infrastructure.llm.providers.registry import get_registry

registry = get_registry()
status = registry.get_provider_status()

for name, info in status.items():
    print(f"{name}: {'✅' if info['available'] else '❌'}")
```

### 3. Common Issues

**Issue**: Service not found
**Solution**: Check container initialization and interface implementations

**Issue**: Provider not available  
**Solution**: Verify API key environment variables and provider configuration

**Issue**: Tests failing with missing dependencies
**Solution**: Use proper mocking with interface contracts

### 4. Logging

FaultMaven uses structured logging. Enable debug mode:

```bash
# Enable detailed logging
export LOG_LEVEL=DEBUG
export ENABLE_MIGRATION_LOGGING=true

./run_faultmaven.sh
```

## Best Practices

### 1. Interface Design

- Keep interfaces focused and minimal
- Use async by default for I/O operations
- Include comprehensive docstrings
- Follow consistent naming patterns

### 2. Service Implementation

- Accept only interfaces in constructors
- Never import infrastructure directly
- Use dependency injection consistently  
- Include proper error handling

### 3. Testing

- Mock all interfaces for unit tests
- Use real implementations for integration tests
- Test both success and failure scenarios
- Verify interface contracts are followed

### 4. Development

- Always use the container for service access
- Never create service instances manually
- Follow the established patterns
- Write tests first (TDD)

## Deployment Considerations

### Feature Flags

Control architecture behavior via environment variables:

```bash
# Enable new architecture (default)
USE_REFACTORED_SERVICES=true
USE_DI_CONTAINER=true

# Legacy fallback (if needed)  
USE_REFACTORED_SERVICES=false
USE_DI_CONTAINER=false
```

### Environment-Specific Configuration

**Development**:
```bash
CHAT_PROVIDER="local"           # Use local LLM
REDIS_URL="redis://localhost:6379"
```

**Testing**:
```bash
TESTING_MODE=true               # Enables mocking
USE_MOCK_LLM=true              # Mock LLM responses
```

**Production**:
```bash
CHAT_PROVIDER="fireworks"       # Use production LLM
REDIS_URL="redis://prod-redis:6379"
OPIK_ENABLED=true              # Enable observability
```

## Getting Help

### Documentation

- **Architecture**: [Current Architecture](current-architecture.md)
- **Dependency Injection**: [DI System](dependency-injection-system.md)
- **Container Usage**: [DI Container Usage Guide](container-usage-guide.md)
- **LLM Providers**: [Provider Guide](../how-to-add-providers.md)
- **ADR**: [Interface-Based Design Decision](adr-001-interface-based-design.md)

### Code Examples

Look at existing tests for usage patterns:
- `tests/services/` - Service testing patterns
- `tests/unit/` - Interface and container testing
- `tests/integration/` - Full workflow testing

### Common Commands

```bash
# Development
./run_faultmaven.sh                    # Start server
python run_tests.py --all              # Run all tests
python run_tests.py --unit --coverage  # Unit tests with coverage

# Code Quality
black faultmaven tests                 # Format code
flake8 faultmaven tests               # Lint code
mypy faultmaven                       # Type checking

# Container Management
python -c "from faultmaven.container import container; print(container.health_check())"
```

This guide should get you productive with FaultMaven's clean architecture quickly. The interface-based design makes the codebase predictable and testable - once you understand the patterns, development becomes straightforward and enjoyable!