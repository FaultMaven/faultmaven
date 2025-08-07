# FaultMaven Architecture - Interface-Based System

This document describes the current architecture of FaultMaven, featuring a fully activated interface-based programming model with comprehensive dependency injection and clean service-oriented design.

**Last Updated**: January 2025  
**Status**: Production-ready with complete interface-based architecture and 7 LLM providers

## Overview

FaultMaven features a modular, interface-based service-oriented architecture designed for maintainability, testability, and flexible deployment. The system uses dependency injection, clean separation of concerns, and feature flags to support multiple configuration modes.

## Architecture Principles

1. **Interface-Based Programming**: All dependencies defined through abstract interfaces for maximum flexibility
2. **Dependency Injection**: Centralized container manages all service dependencies and lifecycles  
3. **Layered Architecture**: Clean separation between API, Service, Core, and Infrastructure layers
4. **Feature Flag Configuration**: Runtime configuration allows multiple deployment modes
5. **Graceful Degradation**: System handles missing dependencies with intelligent fallbacks

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
- `AgentService` - Troubleshooting workflow orchestration with interface dependencies
- `DataService` - Data processing pipeline management with pluggable processors
- `KnowledgeService` - Knowledge base operations with vector store abstraction
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
- `llm/` - LLM provider management with interface implementations
  - `router.py` - Provider selection and fallback implementing `ILLMProvider`
  - `providers/` - Specific LLM implementations with common interface
- `security/` - Security infrastructure
  - `redaction.py` - K8s Presidio microservice integration implementing `ISanitizer`
- `observability/` - Monitoring
  - `tracing.py` - Distributed tracing implementing `ITracer`
- `interfaces.py` - Infrastructure interface definitions

**Responsibilities**:
- External API integration
- Interface implementations for infrastructure services
- Cross-cutting technical concerns
- K8s microservice integration
- Security implementation
- Monitoring and metrics

### 4.1. K8s Microservices Integration

**Architecture**: FaultMaven now integrates with dedicated K8s microservices for:

**Redis Session Storage**:
- **Service**: `192.168.0.111:30379` (NodePort - TCP protocol)
- **Purpose**: Distributed session management with authentication
- **Implementation**: Enhanced Redis client with health checks and fallback
- **Configuration**: Environment variables with K8s defaults

**ChromaDB Vector Storage**:
- **Service**: `chromadb.faultmaven.local:30080`
- **Purpose**: Vector database for knowledge base and embeddings
- **Authentication**: Token-based with `faultmaven-dev-chromadb-2025`
- **Implementation**: HTTP client with graceful local fallback

**Presidio PII Protection**:
- **Services**: 
  - Analyzer: `presidio-analyzer.faultmaven.local:30080`
  - Anonymizer: `presidio-anonymizer.faultmaven.local:30080`
- **Purpose**: Advanced PII detection and redaction
- **Implementation**: HTTP API integration with regex fallback
- **Benefits**: Removes heavy spaCy model loading from main application

**Design Principles**:
- **Graceful Degradation**: All services work with fallback when K8s unavailable
- **Health Monitoring**: Proactive service availability checking
- **Environment Flexibility**: Works in development, testing, and production
- **Consistent Configuration**: Unified approach across all K8s services

## Interface-Based Architecture

### Core Interface System

FaultMaven employs a comprehensive interface system defined in `models/interfaces.py` that provides abstract contracts for all major components:

**Infrastructure Interfaces**:
- `ILLMProvider` - Abstract LLM interaction (generate/completion operations)  
- `ISanitizer` - Data sanitization and PII redaction contracts
- `ITracer` - Distributed tracing and observability contracts
- `IVectorStore` - Vector database operations for knowledge base
- `ISessionStore` - Session storage and retrieval operations

**Processing Interfaces**:
- `IDataClassifier` - Data type detection and classification
- `ILogProcessor` - Log analysis and insight extraction
- `IKnowledgeIngester` - Document ingestion and processing
- `IStorageBackend` - Generic storage operations

**Tool Interfaces**:
- `BaseTool` - Common contract for all agent tools
- `ToolResult` - Standardized tool execution results

### Benefits of Interface-Based Design

1. **Testability**: Easy mocking and unit testing with interface stubs
2. **Flexibility**: Swap implementations without changing business logic
3. **Modularity**: Clear contracts between components
4. **Extensibility**: Add new implementations through interfaces
5. **Deployment Options**: Different implementations for different environments

## Dependency Injection System

### Container Architecture

The `DIContainer` class provides centralized dependency management with:

**Features**:
- **Singleton Pattern**: Single container instance across application
- **Lazy Initialization**: Components created only when needed
- **Interface Resolution**: Automatic mapping from interfaces to implementations
- **Health Monitoring**: Built-in health checking for all dependencies
- **Graceful Fallback**: Mock implementations when dependencies unavailable

**Container Structure**:
```python
# Infrastructure Layer
container.get_llm_provider() -> ILLMProvider implementation
container.get_sanitizer() -> ISanitizer implementation  
container.get_tracer() -> ITracer implementation

# Service Layer
container.get_agent_service() -> AgentService
container.get_data_service() -> DataService
container.get_knowledge_service() -> KnowledgeService

# Processing Layer
container.get_data_classifier() -> IDataClassifier implementation
container.get_log_processor() -> ILogProcessor implementation
```

### Service Dependencies

Services receive all dependencies through constructor injection:

```python
class AgentService:
    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool], 
        tracer: ITracer,
        sanitizer: ISanitizer
    ):
        # All dependencies injected as interfaces
```

This ensures services are decoupled from concrete implementations and fully testable.

### 5. Models (`models/`)

**Purpose**: Data transfer objects and domain entities

**Organization**:
- `agent.py` - Agent-related models
- `api.py` - Request/response DTOs
- `domain.py` - Core business entities
- `session.py` - Session management models

### 5. Tools (`tools/`)

**Purpose**: Agent capabilities with interface compliance

**Components**:
- `knowledge_base.py` - RAG tool implementing `BaseTool` interface
- `web_search.py` - External search capability implementing `BaseTool` interface

Both tools provide standardized `execute()` and `get_schema()` methods for consistent agent integration.

### 6. Configuration (`config/`)

**Feature Flag System**: 

`feature_flags.py` provides runtime configuration control:

**Core Flags**:
- `USE_REFACTORED_SERVICES` - Enable interface-based services
- `USE_REFACTORED_API` - Enable thin controller API routes  
- `USE_DI_CONTAINER` - Enable centralized dependency injection
- `ENABLE_MIGRATION_LOGGING` - Additional logging during transitions

**Migration Strategies**:
- `full_new_architecture` - Complete interface-based system
- `full_legacy_architecture` - Original implementation
- `backend_refactored_api_legacy` - Services updated, API unchanged
- `partial_migration` - Mixed configuration mode

**Usage Example**:
```bash
# Enable new architecture
export USE_REFACTORED_SERVICES=true
export USE_REFACTORED_API=true  
export USE_DI_CONTAINER=true

# Start application
./run_faultmaven.sh
```

**Benefits**:
- Zero-downtime architecture transitions
- A/B testing capabilities  
- Rollback safety mechanisms
- Environment-specific configurations

## Data Flow

### Interface-Based Request Processing

1. **API Layer**: Thin controllers delegate to injected services
2. **DI Container**: Resolves all interface dependencies 
3. **Service Layer**: Orchestrates operations using interface contracts
4. **Core Domain**: Executes business logic through interface methods
5. **Infrastructure**: Implements interfaces for external resources

**Example Flow**:
```
POST /api/v1/query/troubleshoot
  ↓
API Route (agent.py)
  ↓ [Depends(get_agent_service)]
DI Container Resolution
  ↓ [Returns AgentService with injected interfaces]
AgentService.process_query()
  ↓ [Uses ILLMProvider, ISanitizer, ITracer, BaseTool[]]
Core Agent + Tools Execution
  ↓ [Interface-based implementations]
Infrastructure Services (LLM, Sanitization, Tracing)
  ↓
Response via Service Layer
```
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

## Architecture Benefits

### Development Benefits
1. **Maintainability**: Clear interface contracts and layer separation
2. **Testability**: Comprehensive interface mocking with 341 passing tests
3. **Flexibility**: Hot-swappable implementations without code changes
4. **Developer Experience**: Type safety and clear dependency graphs
5. **Debugging**: Health monitoring and detailed error reporting

### Operational Benefits
1. **Reliability**: Multi-provider fallback chains prevent single points of failure
2. **Observability**: Distributed tracing across all LLM calls and operations
3. **Privacy**: Comprehensive PII redaction with Presidio integration
4. **Performance**: Lazy initialization and intelligent caching strategies
5. **Scalability**: Interface-based design enables horizontal scaling

### Business Benefits
1. **Cost Optimization**: Intelligent provider routing based on cost/performance
2. **Vendor Independence**: Easy switching between 7 LLM providers
3. **Compliance**: Built-in PII protection and data sanitization
4. **Time to Market**: Rapid feature development through clean interfaces
5. **Risk Mitigation**: Graceful degradation and comprehensive fallback strategies

## Future Enhancements

1. **Event Sourcing**: Capture all state changes
2. **CQRS**: Separate read/write models
3. **API Gateway**: Advanced routing and policies
4. **Service Mesh**: Inter-service communication
5. **Distributed Tracing**: Full observability