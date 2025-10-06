# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

FaultMaven is an AI-powered troubleshooting copilot backend featuring:
- **Agentic Framework**: ✅ **PRODUCTION READY** - 7-component autonomous AI system with true Plan→Execute→Observe→Re-plan cycles
- **Interface-Based Architecture**: Clean dependency injection design with comprehensive container management
- **Multi-LLM Support**: 7 providers (Fireworks, OpenAI, Anthropic, Gemini, HuggingFace, OpenRouter, Local) with intelligent routing
- **Hierarchical Memory System**: Context consolidation and strategic planning with persistent state management
- **Centralized Provider Registry**: Unified LLM provider management with automatic health monitoring
- **Autonomous Decision-Making**: Sophisticated reasoning with comprehensive error handling and recovery
- **Privacy-First**: Advanced PII redaction with Presidio integration and multi-layer guardrails
- **RAG Knowledge Base**: ChromaDB with BGE-M3 embeddings for intelligent document retrieval
- **Service-Oriented**: Clean layer separation between API, Service, Agentic Framework, Core, and Infrastructure

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

# Local LLM Service Management (Container-Based)
./scripts/local_llm_service.sh start <model_name>  # Start local LLM Docker container
./scripts/local_llm_service.sh stop                # Stop local LLM service
./scripts/local_llm_service.sh status              # Show service status and model consistency
./scripts/local_llm_service.sh check               # Check model consistency and auto-fix
./scripts/local_llm_service.sh restart <model>     # Restart with new model
```

### Testing - Post-Architecture Overhaul (1425+ Tests)
```bash
# Advanced test runner with architecture validation
python run_tests.py --all --coverage --html  # All tests, linting, type checking, security (recommended)
python run_tests.py --unit --coverage        # Unit tests with container patterns
python run_tests.py --integration            # Integration tests with architecture workflows
python run_tests.py --security               # Security/redaction tests
python run_tests.py --api                    # API endpoint tests with container injection
python run_tests.py --lint --type-check      # Code quality checks

# New Comprehensive Architecture Tests (130+ new tests)
SKIP_SERVICE_CHECKS=true python -m pytest tests/unit/test_settings_system_comprehensive.py -v      # Settings system (37+ tests)
python -m pytest tests/infrastructure/test_llm_registry_comprehensive.py -v                        # LLM registry (37+ tests)
python -m pytest tests/unit/test_container_integration_comprehensive.py -v                         # Container integration (38+ tests)
python -m pytest tests/integration/test_new_architecture_workflows.py -v                           # Architecture workflows (18+ tests)

# Container-Based Test Execution with Clean State
SKIP_SERVICE_CHECKS=true pytest --cov=faultmaven tests/ # Full test suite (1425+ tests) with external service bypass
SKIP_SERVICE_CHECKS=true pytest tests/unit/ -v         # Unit tests with container reset patterns
pytest tests/services/ -v                              # Service layer tests with interface injection
pytest tests/integration/ -v                           # Cross-layer integration with container patterns

# Architecture Layer Testing
pytest -m "unit" -v                           # Container, interfaces, settings tests
pytest -m "integration" -v                    # Cross-layer workflow tests
pytest -m "security" -v                       # PII redaction and sanitization
pytest -m "api" -v                           # FastAPI endpoints with container injection

# Performance and Resource Testing
RUN_PERFORMANCE_TESTS=true pytest tests/performance/ -v # Container overhead and logging performance

# Container and Interface Testing Examples
pytest tests/unit/test_container_foundation.py::TestContainerFoundation::test_singleton_behavior -v
pytest tests/unit/test_interface_compliance_new.py::TestInterfaceCompliance::test_llm_provider_interface -v
pytest tests/integration/test_new_architecture_workflows.py::TestEndToEndWorkflows::test_complete_troubleshooting_workflow -v
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
│               Agentic Framework                              │
│  (7-Component Autonomous System: Planning, Memory, Safety)   │
├─────────────────────────────────────────────────────────────┤
│                    Core Components                           │
│  (Agent Workflows, Data Processing, Knowledge Base)          │
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

**Sub-Agent Architecture (Anthropic Context Engineering)**: The system uses specialized phase agents with focused context instead of a monolithic approach:
- **DiagnosticOrchestrator** routes queries to appropriate phase agent based on `current_phase`
- **6 Specialized Agents** (Intake, BlastRadius, Timeline, Hypothesis, Validation, Solution)
- **49% Token Reduction** - Each agent uses 300-700 tokens vs 1300 monolithic
- **Goal-Oriented Advancement** - Phases advance when objectives met, not turn-based
- **Comprehensive Test Coverage** - 78 unit tests across all agents

**Five-Phase SRE Diagnostic Flow**:
1. **Phase 0: Intake** - Problem detection & urgency assessment
2. **Phase 1: Blast Radius** - Scope the impact and affected services
3. **Phase 2: Timeline** - Establish when issues started and correlate changes
4. **Phase 3: Hypothesis** - Generate potential root causes
5. **Phase 4: Validation** - Test theories with evidence
6. **Phase 5: Solution** - Propose resolution and remediation

### Directory Structure

The FaultMaven codebase follows Clean Architecture principles with a well-organized directory structure:

```
faultmaven/
├── api/v1/                     # API Layer - HTTP endpoints and routing
│   ├── routes/                 # FastAPI routers (agent, case, data, knowledge, session)
│   └── dependencies.py         # FastAPI dependency injection
├── services/                   # Service Layer - Business logic orchestration
│   ├── domain/                 # Core domain services
│   │   ├── case_service.py     # Case lifecycle management
│   │   ├── data_service.py     # Data processing coordination
│   │   ├── knowledge_service.py # Knowledge base operations
│   │   ├── session_service.py  # Session lifecycle management
│   │   └── planning_service.py # Strategic planning coordination
│   ├── analytics/              # Analytics and reporting services
│   │   ├── dashboard_service.py # Analytics dashboard (formerly analytics_dashboard.py)
│   │   └── confidence_service.py # Confidence scoring (formerly confidence.py)
│   ├── agentic/               # Agentic Framework - 7-component autonomous system
│   │   ├── orchestration/     # Main orchestration layer
│   │   │   └── agent_service.py # Primary agentic orchestrator
│   │   ├── engines/           # Processing engines
│   │   │   ├── workflow_engine.py # Business logic workflows
│   │   │   ├── classification_engine.py # Query classification
│   │   │   └── response_synthesizer.py # Response assembly
│   │   ├── management/        # State and resource management
│   │   │   ├── state_manager.py # Hierarchical memory management
│   │   │   └── tool_broker.py # Dynamic tool orchestration
│   │   └── safety/            # Safety and error handling
│   │       ├── guardrails_layer.py # Security validation
│   │       └── error_manager.py # Intelligent error recovery
│   ├── converters/            # Data transformation services
│   │   └── case_converter.py  # Case data mapping
│   └── base.py               # Base service classes
├── core/                      # Core Domain - Business logic and domain models
│   ├── agent/                # Agent reasoning engine
│   │   ├── agent.py          # LangGraph-based agent
│   │   └── doctrine.py       # Five-phase SRE troubleshooting methodology
│   ├── processing/           # Data classification and analysis
│   │   ├── classifier.py     # Data type classification
│   │   └── log_processor.py  # Log analysis capabilities
│   └── knowledge/            # Knowledge base operations
│       └── ingestion.py      # RAG document ingestion
├── infrastructure/           # Infrastructure Layer - External integrations
│   ├── jobs/                 # Background job processing (formerly services/job.py)
│   │   └── job_service.py    # Job management and execution
│   ├── llm/                  # LLM provider integrations
│   │   ├── router.py         # Multi-provider routing with failover
│   │   └── providers/        # Individual provider implementations
│   ├── security/             # Security and PII protection
│   │   └── redaction.py      # Presidio-based data sanitization
│   ├── observability/        # Monitoring and tracing
│   │   └── tracing.py        # Opik integration for LLM observability
│   ├── persistence/          # Data storage integrations
│   │   ├── redis_*.py        # Redis-based stores
│   │   └── chromadb_*.py     # ChromaDB vector store
│   ├── logging/              # Centralized logging system
│   │   ├── coordinator.py    # Logging coordination
│   │   └── unified.py        # Unified logging interface
│   ├── protection/           # Client protection systems
│   │   ├── protection_coordinator.py # ML-based threat detection
│   │   └── *.py             # Rate limiting, circuit breakers, etc.
│   ├── monitoring/           # Advanced monitoring
│   │   └── *.py             # Metrics, alerting, SLA tracking
│   ├── health/               # Component health monitoring
│   │   └── *.py             # Health checks and monitoring
│   └── caching/              # Intelligent caching
│       └── *.py             # Cache strategies and management
├── tools/                    # Agent Tools - Capabilities for AI agents
│   ├── knowledge_base.py     # RAG operations
│   └── web_search.py         # External search capability
├── models/                   # Data Models - Pydantic schemas and interfaces
│   ├── interfaces.py         # Interface contracts
│   ├── case.py              # Case-related models
│   ├── session.py           # Session models
│   └── *.py                 # Domain models
├── container.py              # Dependency Injection - Service management
└── main.py                   # FastAPI Application - Entry point
```

### Key Components by Layer

#### 1. API Layer (`api/v1/`)
**Purpose**: Handle HTTP requests, validation, and response formatting

- **Routes**: FastAPI routers for agent, data, knowledge, session operations
- **Dependencies**: FastAPI dependency injection (`dependencies.py`)
- **Middleware**: Authentication, rate limiting, request logging

#### 2. Service Layer (`services/`)
**Purpose**: Business logic orchestration with interface dependencies

**Domain Services** (`services/domain/`):
- **CaseService**: Case lifecycle and management operations
- **SessionService**: Session lifecycle and analytics
- **DataService**: Data processing pipeline management with pluggable processors
- **KnowledgeService**: Knowledge base operations with vector store abstraction
- **PlanningService**: Strategic planning and decision-making coordination

**Analytics Services** (`services/analytics/`):
- **DashboardService**: Analytics dashboard and reporting (formerly analytics_dashboard.py)
- **ConfidenceService**: Confidence scoring and evaluation (formerly confidence.py)

**Converter Services** (`services/converters/`):
- **CaseConverter**: Case data transformation and mapping

#### 2.1. Agentic Framework (`services/agentic/`) ✅ PRODUCTION READY
**Purpose**: Autonomous AI system with true Plan→Execute→Observe→Re-plan cycles

**Orchestration** (`services/agentic/orchestration/`):
- **AgentService**: Main orchestrator implementing autonomous agentic loops

**Processing Engines** (`services/agentic/engines/`):
- **BusinessLogicWorkflowEngine**: Primary workflow orchestrator
- **QueryClassificationEngine**: Multi-dimensional query analysis (intent, complexity, domain, urgency)
- **ResponseSynthesizer**: Multi-source response assembly with quality validation

**Management** (`services/agentic/management/`):
- **AgentStateManager**: Hierarchical memory backbone with persistent Redis storage
- **ToolSkillBroker**: Dynamic capability discovery and intelligent orchestration

**Safety** (`services/agentic/safety/`):
- **GuardrailsPolicyLayer**: Multi-layer security validation and advanced PII protection
- **ErrorFallbackManager**: Intelligent error recovery with circuit breakers and learning

**7 Core Components** (7,770 lines of code, 55 classes, Full Implementation)

#### 3. Core Domain (`core/`)
**Purpose**: Core business logic and domain models

- **Agent** (`core/agent/`): LangGraph-based reasoning engine following five-phase doctrine
- **Processing** (`core/processing/`): Data classification and log analysis
- **Knowledge** (`core/knowledge/`): RAG document ingestion and retrieval

#### 4. Infrastructure Layer (`infrastructure/`)
**Purpose**: External service integrations and technical concerns

- **Jobs** (`infrastructure/jobs/`): Background job processing and management (formerly services/job.py)
- **LLM Router** (`infrastructure/llm/`): Multi-provider routing with automatic fallback implementing `ILLMProvider`
- **Security** (`infrastructure/security/`): K8s Presidio microservice integration implementing `ISanitizer`
- **Protection** (`infrastructure/protection/`): Comprehensive client protection system with ML-based threat detection
- **Monitoring** (`infrastructure/monitoring/`): Advanced monitoring and alerting for protection systems
- **Observability** (`infrastructure/observability/`): Opik tracing implementing `ITracer`
- **Persistence** (`infrastructure/persistence/`): Redis and ChromaDB integrations
- **Logging** (`infrastructure/logging/`): Centralized logging coordination and management
- **Health** (`infrastructure/health/`): Component monitoring and SLA tracking
- **Caching** (`infrastructure/caching/`): Intelligent caching strategies

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

**LLM Timeout Configuration**: Three-layer timeout architecture for robust response handling:
- `LLM_REQUEST_TIMEOUT=30` - Base timeout for LLM requests (applies to both local and cloud providers)
  - Infrastructure Layer: 30 seconds
  - Service Layer: 32 seconds (infrastructure + 2s buffer)
  - API Layer: 35 seconds (service + 3s buffer)

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

### Testing Architecture - After Major Overhaul (1425+ Tests)

**Test Structure**: Organized by Clean Architecture layers with container-based patterns:
- `tests/api/` - API layer tests (FastAPI endpoints with container injection)
- `tests/services/` - Service layer tests (business logic orchestration with interface dependencies)
- `tests/core/` - Core domain tests (agent, processing, knowledge base)
- `tests/infrastructure/` - Infrastructure tests (LLM providers, security, observability, persistence)
- `tests/unit/` - Architecture component tests (DI container, interfaces, settings system)
- `tests/integration/` - Cross-layer integration with architecture workflows
- `tests/performance/` - Container overhead and logging performance validation
- `tests/architecture/` - Architecture compliance and validation tests

**New Comprehensive Test Files (130+ tests)**:
- `tests/unit/test_settings_system_comprehensive.py` - Complete settings system testing (37+ tests)
- `tests/infrastructure/test_llm_registry_comprehensive.py` - LLM registry management (37+ tests)
- `tests/unit/test_container_integration_comprehensive.py` - DI container integration (38+ tests)
- `tests/integration/test_new_architecture_workflows.py` - Architecture workflow validation (18+ tests)

**Container-Based Testing Patterns**:
- **Dependency Injection**: All tests use `container.get_*_service()` for service resolution
- **Clean State Management**: `container.reset()` ensures proper test isolation
- **Interface Mocking**: Mock dependencies through interfaces (`ILLMProvider`, `ISanitizer`, etc.)
- **Environment Isolation**: Clean environment fixtures for proper configuration testing

**Test Categories and Execution**:
- **Unit Tests**: Container, interfaces, settings with `SKIP_SERVICE_CHECKS=true`
- **Service Tests**: Business logic with interface-based dependency injection
- **Integration Tests**: Cross-layer workflows with container patterns
- **Performance Tests**: Container overhead validation with conditional execution
- **Architecture Tests**: Clean Architecture compliance and layer validation

**Advanced Testing Features**:
- **Test Count**: 1425+ tests across all architectural layers
- **Coverage**: Comprehensive coverage with container-based testing patterns
- **Performance**: <0.5% container overhead with 26+ performance tests
- **Interface Compliance**: All mocks implement proper interface contracts
- **Architecture Validation**: Complete Clean Architecture pattern testing

**Test Environment Configuration**:
```bash
# Container-based testing with external service bypass
export SKIP_SERVICE_CHECKS=true

# Performance testing (conditional execution)
export RUN_PERFORMANCE_TESTS=true

# Debug logging for test troubleshooting
export LOG_LEVEL=DEBUG
```

**Test Documentation**: See `/tests/README.md`, `/tests/ARCHITECTURE_TESTING_GUIDE.md`, and `/tests/NEW_TEST_PATTERNS.md` for comprehensive testing patterns and guidelines.

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
- **Logging Improvements**: Previous logging implementation refined and optimized (312 lines of technical debt eliminated)
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

### Running Specific Test Categories - Architecture Overhaul
```bash
# New Comprehensive Architecture Tests (130+ tests)
pytest tests/unit/test_settings_system_comprehensive.py -v                    # Settings system (37+ tests)
pytest tests/infrastructure/test_llm_registry_comprehensive.py -v             # LLM registry (37+ tests)
pytest tests/unit/test_container_integration_comprehensive.py -v               # Container integration (38+ tests)
pytest tests/integration/test_new_architecture_workflows.py -v                 # Architecture workflows (18+ tests)

# Container and Interface Testing
pytest tests/unit/test_container_foundation.py tests/unit/test_interface_compliance_new.py -v

# Service Layer with Interface Injection
pytest tests/services/ -v

# API Layer with Container Integration
pytest tests/api/ -v

# Cross-Layer Architecture Integration
pytest tests/integration/ -v

# Security and PII Protection
pytest -m security -v

# Performance with Container Overhead Validation
RUN_PERFORMANCE_TESTS=true pytest tests/performance/ -v

# Architecture Compliance and Validation
pytest tests/test_architecture.py tests/test_main.py -v

# Clean Architecture Layer Testing
SKIP_SERVICE_CHECKS=true pytest tests/unit/ -v       # Unit layer (container, interfaces, settings)
pytest tests/services/ -v                            # Service layer (business logic orchestration)
pytest tests/core/ -v                                # Core domain layer (agent, processing)
pytest tests/infrastructure/ -v                      # Infrastructure layer (external integrations)
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