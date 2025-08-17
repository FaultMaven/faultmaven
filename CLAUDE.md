# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

FaultMaven is an AI-powered troubleshooting copilot backend featuring:
- **Clean Architecture**: Interface-based design with dependency injection
- **Multi-LLM Support**: 7 providers (Fireworks, OpenAI, Anthropic, Gemini, HuggingFace, OpenRouter, Local)
- **Centralized Registry**: Data-driven provider management with automatic fallback
- **5-Phase SRE Doctrine**: Structured troubleshooting methodology
- **Privacy-First**: Comprehensive PII redaction with Presidio integration
- **RAG Knowledge Base**: ChromaDB with BGE-M3 embeddings
- **Service-Oriented**: Clear separation between API, Service, Core, and Infrastructure layers

## Development Commands

### Environment Setup
```bash
# Initial setup
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-test.txt
python -m spacy download en_core_web_lg

# Configuration
cp .env.example .env
# Edit .env with your API keys

# Hostname resolution for K8s services (via Ingress on port 30080)
echo "192.168.0.111 opik.faultmaven.local" >> /etc/hosts
echo "192.168.0.111 redis.faultmaven.local" >> /etc/hosts
echo "192.168.0.111 chromadb.faultmaven.local" >> /etc/hosts
echo "192.168.0.111 presidio-analyzer.faultmaven.local" >> /etc/hosts
echo "192.168.0.111 presidio-anonymizer.faultmaven.local" >> /etc/hosts
echo "192.168.0.112 opik.faultmaven.local" >> /etc/hosts
echo "192.168.0.112 redis.faultmaven.local" >> /etc/hosts
echo "192.168.0.112 chromadb.faultmaven.local" >> /etc/hosts
echo "192.168.0.112 presidio-analyzer.faultmaven.local" >> /etc/hosts
echo "192.168.0.112 presidio-anonymizer.faultmaven.local" >> /etc/hosts
```

### Running the Application
```bash
# Standard startup (loads .env automatically)
./run_faultmaven.sh

# Manual startup (ensure .env is loaded first)
python -m faultmaven.main

# Docker stack (with Redis, ChromaDB dependencies)
docker-compose up --build -d

# Direct development with uvicorn hot reload
uvicorn faultmaven.main:app --reload --host 0.0.0.0 --port 8000

# With debug logging for development
LOG_LEVEL=DEBUG python -m faultmaven.main

# Skip external service checks for local development
SKIP_SERVICE_CHECKS=true python -m faultmaven.main
```

### Testing
```bash
# Advanced test runner (recommended)
python run_tests.py --all                    # All tests, linting, type checking, security
python run_tests.py --unit --coverage --html # Unit tests with HTML coverage report
python run_tests.py --integration            # Integration tests only
python run_tests.py --security               # Security/redaction tests
python run_tests.py --api                    # API endpoint tests
python run_tests.py --lint --type-check      # Code quality checks only

# Direct pytest usage
pytest --cov=faultmaven tests/               # Full test suite with coverage
pytest -m "unit"                            # Unit tests only
pytest -m "integration"                     # Integration tests (requires Docker)
pytest -m "llm"                            # LLM-related tests
pytest -m "agent"                          # Agent workflow tests
pytest -m "security"                        # Security/redaction tests

# Single test examples
pytest tests/unit/test_container_foundation.py::TestContainerFoundation::test_initialization -v
pytest tests/services/test_agent_service.py::TestAgentService::test_process_query -v  
pytest tests/api/test_agent_endpoints_rebuilt.py::TestAgentAPIEndpointsRebuilt::test_troubleshoot_endpoint -v

# Run with specific test environment variables
SKIP_SERVICE_CHECKS=true pytest tests/unit/ -v --tb=short

# Test with coverage and skip external services
SKIP_SERVICE_CHECKS=true pytest --cov=faultmaven --cov-report=term-missing tests/unit/ tests/services/
```

### Code Quality
```bash
# Pre-commit hooks (install once)
pre-commit install

# Manual linting/formatting
black faultmaven tests                    # Code formatting
flake8 faultmaven tests                   # Linting
mypy faultmaven                          # Type checking
isort faultmaven tests                   # Import sorting
bandit -r faultmaven                     # Security analysis

# Check formatting without applying changes
black --check --diff faultmaven tests
isort --check-only --diff faultmaven tests

# All quality checks at once
python run_tests.py --lint --type-check
```

## Clean Architecture Overview

FaultMaven follows a **Clean Architecture** pattern with interface-based design and comprehensive dependency injection.

### Architectural Principles

1. **Interface-Based Programming**: All dependencies defined through abstract interfaces (`models/interfaces.py`)
2. **Dependency Injection**: Centralized `DIContainer` manages all service dependencies and lifecycles
3. **Layered Architecture**: Clean separation between API, Service, Core, and Infrastructure layers
4. **Feature Flag Configuration**: Runtime configuration allows multiple deployment modes
5. **Graceful Degradation**: System handles missing dependencies with intelligent fallbacks

### Layer Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        API Layer                             │
│  (FastAPI Routers, Dependencies, Request/Response Models)    │
├─────────────────────────────────────────────────────────────┤
│                      Service Layer                           │
│  (Business Logic, Orchestration, Domain Operations)          │
├─────────────────────────────────────────────────────────────┤
│                    Core Components                           │
│  (Agent, Data Processing, Knowledge Base)                    │
├─────────────────────────────────────────────────────────────┤
│                   Infrastructure Layer                       │
│  (LLM Router, Redis, ChromaDB, Security, Observability)      │
└─────────────────────────────────────────────────────────────┘
```

### Core Design Patterns

**Centralized Provider Registry**: All 7 LLM providers are managed through a single `PROVIDER_SCHEMA` in `infrastructure/llm/providers/registry.py`. This eliminates scattered configuration and provides unified fallback strategies.

**Dependency Injection Container**: Services are managed through `container.py` with:
- **Singleton Pattern**: Single container instance across application
- **Lazy Initialization**: Components created only when needed
- **Interface Resolution**: Automatic mapping from interfaces to implementations
- **Health Monitoring**: Built-in health checking for all dependencies
- **Graceful Fallback**: Mock implementations when dependencies unavailable

**Five-Phase SRE Doctrine**: The AI agent follows a structured troubleshooting methodology defined in `core/agent/doctrine.py`:
1. **Define Blast Radius** - Scope the impact
2. **Establish Timeline** - Understand when issues started
3. **Formulate Hypothesis** - Generate potential causes
4. **Validate Hypothesis** - Test theories with evidence
5. **Propose Solution** - Recommend fixes

### Key Components by Layer

#### 1. API Layer (`api/v1/`)
**Purpose**: Handle HTTP requests, validation, and response formatting

- **Routes**: FastAPI routers for agent, data, knowledge, session operations
- **Dependencies**: FastAPI dependency injection (`dependencies.py`)
- **Middleware**: Authentication, rate limiting, request logging

#### 2. Service Layer (`services/`)
**Purpose**: Business logic orchestration with interface dependencies

- **AgentService**: Troubleshooting workflow orchestration with injected `ILLMProvider`, `ISanitizer`, `ITracer`
- **DataService**: Data processing pipeline management with pluggable processors
- **KnowledgeService**: Knowledge base operations with vector store abstraction  
- **SessionService**: Session lifecycle and analytics

#### 3. Core Domain (`core/`)
**Purpose**: Core business logic and domain models

- **Agent** (`core/agent/`): LangGraph-based reasoning engine following five-phase doctrine
- **Processing** (`core/processing/`): Data classification and log analysis
- **Knowledge** (`core/knowledge/`): RAG document ingestion and retrieval

#### 4. Infrastructure Layer (`infrastructure/`)
**Purpose**: External service integrations and technical concerns

- **LLM Router** (`infrastructure/llm/`): Multi-provider routing with automatic fallback implementing `ILLMProvider`
- **Security** (`infrastructure/security/`): K8s Presidio microservice integration implementing `ISanitizer`
- **Protection** (`infrastructure/protection/`): Comprehensive client protection system with ML-based threat detection
- **Monitoring** (`infrastructure/monitoring/`): Advanced monitoring and alerting for protection systems
- **Observability** (`infrastructure/observability/`): Opik tracing implementing `ITracer`
- **Persistence** (`infrastructure/persistence/`): Redis and ChromaDB integrations

#### 5. Tools (`tools/`)
**Purpose**: Agent capabilities with interface compliance

- **KnowledgeBaseTool**: RAG operations implementing `BaseTool`
- **WebSearchTool**: External search capability implementing `BaseTool`

### Configuration Management

**Environment Variables**: Primary configuration through `.env` file. All providers use a consistent naming pattern:
- `{PROVIDER}_API_KEY` for authentication
- `{PROVIDER}_MODEL` for default model selection  
- `CHAT_PROVIDER` to select primary provider

**Environment Configuration**: Primary configuration through `.env` file with enhanced logging and protection support:
- `LOG_LEVEL=INFO` - Logging verbosity (DEBUG, INFO, WARNING, ERROR)
- `LOG_FORMAT=json` - Structured JSON logging with correlation IDs
- `LOG_DEDUPE=true` - Prevent duplicate log entries (95% deduplication success)
- `LOG_BUFFER_SIZE=100` - Log buffer optimization
- `LOG_FLUSH_INTERVAL=5` - Flush interval in seconds

**Protection System Configuration**: Comprehensive client protection with two-phase approach:
- `PROTECTION_ENABLED=true` - Master protection system toggle
- `PROTECTION_PHASE_1_ENABLED=true` - Immediate protection (rate limiting, deduplication, timeouts)
- `PROTECTION_PHASE_2_ENABLED=true` - Intelligent protection (ML, behavioral analysis, reputation)
- `RATE_LIMIT_GLOBAL_REQUESTS=1000` - Global rate limit threshold
- `ML_ANOMALY_DETECTION_ENABLED=true` - Enable ML-based anomaly detection
- `BEHAVIORAL_ANALYSIS_ENABLED=true` - Enable behavioral pattern analysis
- `REPUTATION_SYSTEM_ENABLED=true` - Enable client reputation management

**Service Connection Defaults** (Hybrid Ingress + NodePort):
- `REDIS_HOST=192.168.0.111` `REDIS_PORT=30379` (NodePort - TCP binary protocol)
- `CHROMADB_URL=http://chromadb.faultmaven.local:30080` (Ingress - HTTP REST API)
- `PRESIDIO_ANALYZER_URL=http://presidio-analyzer.faultmaven.local:30080` (Ingress - HTTP REST API)
- `PRESIDIO_ANONYMIZER_URL=http://presidio-anonymizer.faultmaven.local:30080` (Ingress - HTTP REST API)
- `OPIK_LOCAL_URL=http://opik.faultmaven.local:30080` (Ingress - HTTP REST API)

**Multi-Provider Support**: 7 LLM providers supported out-of-the-box:
1. **Fireworks AI** - High-performance inference (`accounts/fireworks/models/llama-v3p1-8b-instruct`)
2. **OpenAI** - GPT-4o and other models (`gpt-4o`)
3. **Anthropic** - Claude 3.5 Sonnet (`claude-3-5-sonnet-20241022`)
4. **Google Gemini** - Gemini 1.5 Pro (`gemini-1.5-pro`)
5. **Hugging Face** - Community models (`tiiuae/falcon-7b-instruct`)
6. **OpenRouter** - Multi-provider access (`anthropic/claude-3-sonnet`)
7. **Local** - Self-hosted models (`Phi-3-mini-128k-instruct-onnx`)

**Provider Addition**: To add new providers, update `PROVIDER_SCHEMA` in `infrastructure/llm/providers/registry.py`. See [How to Add Providers](docs/how-to-add-providers.md) for details.

**Opik Observability**: Default setup connects to team server at `opik.faultmaven.local`. For custom setups, use config templates in `scripts/config/`.

### Interface-Based Data Flow

1. **Browser Extension** → **FastAPI Router** → **DI Container Resolution**
2. **Service Layer** receives all dependencies as interfaces:
   - `AgentService` uses `ILLMProvider`, `ISanitizer`, `ITracer`, `List[BaseTool]`
   - `DataService` uses `IDataClassifier`, `ILogProcessor`, `IStorageBackend`
   - `KnowledgeService` uses `IVectorStore`, `IKnowledgeIngester`
3. **Core Domain** executes business logic through interface contracts
4. **Infrastructure Layer** implements interfaces for external services:
   - `LLMRouter` implements `ILLMProvider` with multi-provider routing
   - `DataSanitizer` implements `ISanitizer` with Presidio integration
   - `OpikTracer` implements `ITracer` for distributed tracing
   - Tools implement `BaseTool` for standardized agent interaction

### Testing Architecture

**Test Structure**: Organized by architectural layer:
- `tests/api/` - API layer tests (endpoints, request/response, middleware)
- `tests/services/` - Service layer tests (orchestration, business logic)
- `tests/core/` - Core domain tests (agent, processing, knowledge)
- `tests/infrastructure/` - Infrastructure tests (LLM, security, observability, persistence)
- `tests/unit/` - Architecture component tests (DI container, interfaces, feature flags)
- `tests/integration/` - End-to-end workflows with mocks (includes logging integration tests)
- `tests/performance/` - Logging and context overhead performance tests
- `tests/architecture/` - Architecture validation and compliance tests

**Test Markers**: Tests are categorized by:
- **Domain**: `agent`, `security`, `data_processing`, `llm`, `api`
- **Type**: `unit`, `integration`
- **Performance**: `logging_overhead`, `context_performance`
- **Layer**: Implicitly organized by directory structure

**Advanced Test Runner**: `run_tests.py` provides comprehensive testing with linting, type checking, security analysis, and parallel execution options.

**Coverage**: Current coverage at 71% with 341+ passing tests. Mock infrastructure used for external dependencies. Performance tests ensure <0.5% logging overhead.

**Performance Testing**: 26 performance tests ensuring < 0.5% logging overhead.

**Interface Testing**: Comprehensive interface compliance tests ensure implementations meet contracts. DI container provides mock implementations for isolation.

**Advanced Test Runner**: Use `python run_tests.py --all` for comprehensive testing including linting, type checking, security analysis, and performance validation.

**Test Debugging**: Use `SKIP_SERVICE_CHECKS=true` environment variable to bypass external service dependencies during testing. Test files are organized by architectural layer for easy navigation.

## Critical Implementation Notes

### Logging Architecture (100% Production Ready)
- **LoggingCoordinator**: Centralized logging with RequestContext, ErrorContext, PerformanceTracker
- **Deduplication System**: UUID-based uniqueness prevents duplicate entries (100% duplicate prevention)
- **Context Propagation**: contextvars implementation working across async boundaries  
- **Performance Optimized**: < 0.5% overhead with 26 passing performance tests
- **Request Traceability**: Correlation IDs maintained across all architectural layers
- **Configuration**: Enhanced with 5 environment variables (`LOG_LEVEL`, `LOG_FORMAT`, `LOG_DEDUPE`, `LOG_BUFFER_SIZE`, `LOG_FLUSH_INTERVAL`)
- **Health Monitoring**: Container provides detailed logging system status via `/health/logging` endpoint
- **Structured JSON**: Complete request lifecycle tracking with session/user context

### Dependency Injection System
- **Container Access**: Use `from faultmaven.container import container` for global singleton access
- **Service Resolution**: `container.get_agent_service()`, `container.get_llm_provider()`, etc.
- **Interface Injection**: All services receive dependencies as interfaces (e.g., `ILLMProvider`, `ISanitizer`)
- **Initialization**: Container auto-initializes on first access, call `container.initialize()` for explicit setup
- **Testing Reset**: Use `container.reset()` to clear container state between tests
- **Health Monitoring**: `container.health_check()` provides detailed dependency status
- **Graceful Degradation**: Missing dependencies automatically use fallback implementations

### Interface Compliance Requirements
- **Infrastructure Interfaces**: All external integrations must implement defined interfaces
- **Service Dependencies**: Services only depend on interface contracts, not concrete implementations
- **Tool Implementation**: Agent tools must implement `BaseTool` interface with `execute()` and `get_schema()` methods
- **Testing Strategy**: Use interface mocks for unit testing, real implementations for integration tests

### LLM Provider Registry (7 Providers)
- **Centralized Schema**: All provider configuration in `PROVIDER_SCHEMA` (`infrastructure/llm/providers/registry.py`)
- **Automatic Registration**: Providers auto-initialize based on environment variables (API keys)
- **Fallback Chain**: Primary provider → Fireworks → OpenAI → Local (based on available configurations)
- **Provider Status**: Use `registry.get_provider_status()` to check availability and health
- **Testing Reset**: Use `reset_registry()` in tests to ensure clean state

### Agent Architecture
- **LangGraph Integration**: State machine with explicit state transitions
- **Five-Phase Doctrine**: Structured troubleshooting workflow in `core/agent/doctrine.py`
- **Tool Integration**: Agent uses injected `List[BaseTool]` for capabilities
- **Case/Session Management**: Redis-backed sessions with case-level conversation tracking
- **Conversation Continuity**: Full context injection across case queries
- **Tracing**: Use `@trace` decorator on agent methods for observability

### Security Requirements (Privacy-First)
- **Mandatory PII Redaction**: All user input through `ISanitizer.sanitize()` before LLM processing
- **Presidio Integration**: K8s microservice for advanced PII detection
- **Interface Compliance**: Use `ISanitizer` interface for all sanitization operations
- **Security Testing**: Use `@pytest.mark.security` for privacy-related functionality
- **Fallback Strategy**: Local regex patterns when Presidio unavailable

### K8s Infrastructure Integration
- **Service Discovery**: Default hostnames for team infrastructure
- **Graceful Degradation**: All services work with local fallbacks
- **Health Monitoring**: Proactive service availability checking
- **Hybrid Access**: NodePort for Redis (TCP), Ingress for HTTP services

### Development Workflow Best Practices
- **Container First**: Always use DI container for service access via `from faultmaven.container import container`
- **Interface Contracts**: New components must implement appropriate interfaces from `models/interfaces.py`
- **Environment Variables**: Load configuration with `python-dotenv` from `.env` file
- **Health Checks**: Use `/health/dependencies` endpoint to validate all services
- **Testing Isolation**: Use `SKIP_SERVICE_CHECKS=true` for unit tests without external dependencies

### Observability & Logging Setup (100% Complete)
- **Production-Ready Logging**: Complete improved logging strategy implementation with zero duplicates
- **Structured Logging**: JSON format with correlation IDs, session context, and user tracking
- **Zero Duplicate Logs**: UUID-based deduplication system with 100% effectiveness
- **Performance Optimized**: < 0.5% overhead confirmed through comprehensive performance testing
- **Context Propagation**: Full request context maintained across all architectural layers
- **Opik Integration**: Team server integration at `opik.faultmaven.local:30080` with targeted tracing
- **Health Monitoring**: `/health/logging` dedicated endpoint for logging system health
- **Legacy Cleanup**: All obsolete logging components removed (312 lines of technical debt eliminated)
- **Environment Configuration**: Complete environment variable support for all logging features

## Quick Reference for Common Tasks

### Adding a New LLM Provider
1. Update `PROVIDER_SCHEMA` in `infrastructure/llm/providers/registry.py`
2. Create provider implementation in `infrastructure/llm/providers/`
3. Add environment variables to `.env.example`
4. Update documentation in `docs/how-to-add-providers.md`

### Adding a New Tool
1. Create tool class implementing `BaseTool` interface in `tools/`
2. Register in `tools/registry.py`
3. Add to container initialization in `container.py`
4. Write unit tests following existing patterns

### Debugging Service Issues
1. Check health endpoint: `curl http://localhost:8000/health/dependencies`
2. Review logs for dependency injection issues
3. Verify environment variables are loaded
4. Use `SKIP_SERVICE_CHECKS=true` for isolated testing

### Running Specific Test Categories
```bash
# Architecture and DI container tests
pytest tests/unit/test_container_foundation.py tests/unit/test_interface_compliance_new.py -v

# Service layer tests
pytest tests/services/ -v

# API endpoint tests
pytest tests/api/ -v

# Security and PII redaction tests
pytest -m security -v

# Performance and observability tests
pytest tests/performance/ tests/test_observability_core.py -v

# Integration and architecture tests
pytest tests/test_architecture.py tests/test_main_application_comprehensive.py -v
```

## Documentation

### API Documentation
Comprehensive API documentation is auto-generated with examples:
- **OpenAPI UI**: Available at `/docs` when server is running
- **ReDoc**: Available at `/redoc` when server is running  
- **Generated Files**:
  - `docs/api/openapi.json` - Machine-readable OpenAPI specification
  - `docs/api/openapi.yaml` - YAML format OpenAPI specification
  - `docs/api/README.md` - Human-readable API reference with examples

**Generate Documentation**:
```bash
# Regenerate API documentation
source .venv/bin/activate
python scripts/generate_api_docs.py
```

### Architecture Documentation
Comprehensive system documentation with visual diagrams:
- **System Architecture**: `docs/architecture/SYSTEM_ARCHITECTURE.md` - Complete system overview with Mermaid diagrams
- **Component Interactions**: `docs/architecture/COMPONENT_INTERACTIONS.md` - Detailed interaction patterns and data flows
- **Case/Session Concepts**: `docs/specifications/CASE_SESSION_CONCEPTS.md` - Fundamental concepts for case vs session management
- **Deployment Guide**: `docs/architecture/DEPLOYMENT_GUIDE.md` - Complete deployment instructions for all environments
- **Interface Documentation**: `docs/specifications/` - Detailed interface specifications and compliance

### Key Documentation Features
- **Visual Architecture Diagrams**: Mermaid diagrams showing system structure and data flow
- **Real Usage Examples**: Actual FaultMaven usage patterns in API documentation
- **Deployment Instructions**: From local development to production Kubernetes
- **Component Health Monitoring**: Comprehensive monitoring and SLA documentation
- **Error Handling Patterns**: Complete error recovery and context propagation flows