# FaultMaven v2.0 Architecture

This document describes the refactored architecture of FaultMaven, implementing a service-oriented design with dependency injection and clear separation of concerns.

**Last Updated**: December 2024  
**Status**: Implemented and tested

## Overview

The FaultMaven backend has been refactored from a well-structured monolith into a modular, service-oriented architecture. This refactoring improves maintainability, testability, and prepares the system for future microservice decomposition.

## Architecture Principles

1. **Separation of Concerns**: Clear boundaries between API, Service, Core, and Infrastructure layers
2. **Dependency Injection**: Centralized service management for better testability
3. **Domain-Driven Design**: Business logic isolated in the core domain
4. **Interface Segregation**: Small, focused interfaces for each component
5. **Open/Closed Principle**: Extensible design without modifying existing code

## Layer Architecture

### 1. API Layer (`api/v1/`)

**Purpose**: Handle HTTP requests, validation, and response formatting

**Components**:
- `routes/` - FastAPI routers for each domain
  - `agent.py` - Troubleshooting endpoints
  - `data.py` - Data ingestion endpoints
  - `knowledge.py` - Knowledge base endpoints
  - `session.py` - Session management endpoints
- `dependencies.py` - FastAPI dependency injection
- `middleware.py` - Cross-cutting concerns (auth, logging)

**Responsibilities**:
- Request/response validation
- HTTP status codes
- Authentication/authorization
- Rate limiting
- OpenAPI documentation

### 2. Service Layer (`services/`)

**Purpose**: Orchestrate business operations and enforce business rules

**Components**:
- `AgentService` - Troubleshooting workflow orchestration
- `DataService` - Data processing pipeline management
- `KnowledgeService` - Knowledge base operations
- `SessionService` - Session lifecycle and analytics

**Responsibilities**:
- Business logic orchestration
- Transaction management
- Cross-domain operations
- Business rule enforcement
- Service-level caching

### 3. Core Domain (`core/`)

**Purpose**: Core business logic and domain models

**Structure**:
```
core/
├── agent/           # AI reasoning engine
│   ├── agent.py    # LangGraph implementation
│   ├── doctrine.py # 5-phase troubleshooting
│   └── state.py    # Agent state management
├── knowledge/       # Knowledge management
│   ├── ingestion.py # Document processing
│   └── retrieval.py # RAG operations
└── processing/      # Data analysis
    ├── classifier.py # Data type detection
    └── log_analyzer.py # Log processing
```

**Responsibilities**:
- Domain-specific logic
- Business entity behavior
- Domain events
- Invariant enforcement

### 4. Infrastructure Layer (`infrastructure/`)

**Purpose**: External service integrations and technical concerns

**Components**:
- `llm/` - LLM provider management
  - `router.py` - Provider selection and fallback
  - `providers/` - Specific LLM implementations
- `persistence/` - Data storage
  - `redis.py` - Session storage
  - `chromadb.py` - Vector storage
- `security/` - Security infrastructure
  - `redaction.py` - PII sanitization
- `observability/` - Monitoring
  - `tracing.py` - Distributed tracing

**Responsibilities**:
- External API integration
- Data persistence
- Security implementation
- Monitoring and metrics

### 5. Models (`models/`)

**Purpose**: Data transfer objects and domain entities

**Organization**:
- `agent.py` - Agent-related models
- `api.py` - Request/response DTOs
- `domain.py` - Core business entities
- `session.py` - Session management models

### 6. Tools (`tools/`)

**Purpose**: Agent capabilities and integrations

**Components**:
- `knowledge_base.py` - RAG tool for agent
- `web_search.py` - External search capability

## Dependency Injection

### Container (`container.py`)

Central service registry providing:
- Singleton service instances
- Lazy initialization
- Configuration injection
- Service lifecycle management

### Usage Example

```python
# In API endpoint
@router.post("/query")
async def process_query(
    request: QueryRequest,
    agent_service: AgentService = Depends(get_agent_service),
    session: SessionContext = Depends(get_current_session)
):
    return await agent_service.process_query(session, request)
```

## Data Flow

### Troubleshooting Request Flow

1. **API Layer**: Receives request, validates input
2. **Dependencies**: Injects services and session
3. **Service Layer**: Orchestrates operation
4. **Core Domain**: Executes business logic
5. **Infrastructure**: Accesses external resources
6. **Response**: Formatted and returned to client

### Sequence Example

```
Client -> API Router -> AgentService -> FaultMavenAgent -> KnowledgeBaseTool
                                     |                   |
                                     v                   v
                                 LLMRouter          ChromaDB
                                     |
                                     v
                              External LLMs
```

## Key Design Patterns

### 1. Service Layer Pattern
- Encapsulates business logic
- Provides transaction boundaries
- Orchestrates domain operations

### 2. Repository Pattern
- Abstracts data access
- Enables testing with mocks
- Supports multiple backends

### 3. Factory Pattern
- Creates complex objects
- Manages dependencies
- Supports configuration

### 4. Observer Pattern
- Event-driven updates
- Decoupled components
- Async notifications

## Configuration

### Environment Variables
```bash
# Core Configuration
REDIS_URL=redis://localhost:6379
SESSION_TIMEOUT_HOURS=24
MAX_SESSIONS_PER_USER=10

# LLM Providers
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
FIREWORKS_API_KEY=fw_...

# Observability
OPIK_PROJECT_NAME=faultmaven
OPIK_ENABLED=true
```

### Service Configuration
Services are configured through the container with environment-based settings.

## Testing Strategy

### Unit Tests
- Test individual components in isolation
- Mock dependencies through DI
- Focus on business logic

### Integration Tests
- Test service interactions
- Use test containers for dependencies
- Verify data flow

### End-to-End Tests
- Test complete user workflows
- Include all layers
- Verify system behavior

## Migration Path

### From Monolith to Services
1. Current: Service-oriented monolith
2. Next: Separate deployable services
3. Future: Full microservice architecture

### Gradual Migration
- Models maintain backward compatibility
- Services can be extracted individually
- API versioning supports evolution

## Benefits

1. **Maintainability**: Clear structure and responsibilities
2. **Testability**: Easy mocking through DI
3. **Scalability**: Service isolation enables scaling
4. **Flexibility**: Easy to modify and extend
5. **Quality**: Type safety and validation throughout

## Future Enhancements

1. **Event Sourcing**: Capture all state changes
2. **CQRS**: Separate read/write models
3. **API Gateway**: Advanced routing and policies
4. **Service Mesh**: Inter-service communication
5. **Distributed Tracing**: Full observability