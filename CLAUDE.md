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
# Standard startup
./run_faultmaven.sh

# Development mode (full transparency, all logs)
./run_faultmaven_dev.sh

# Docker stack (with Redis, ChromaDB dependencies)
docker-compose up --build -d

# K8s cluster connectivity (default configuration)
./run_faultmaven_dev.sh

# Alternative: Use local Redis (requires local Redis server)
REDIS_URL="redis://localhost:6379" ./run_faultmaven_dev.sh
```

### Testing
```bash
# Full test suite with coverage
pytest --cov=faultmaven tests/

# Specific test categories
pytest -m "unit"           # Unit tests only
pytest -m "integration"    # Integration tests (requires Docker)
pytest -m "llm"           # LLM-related tests
pytest -m "agent"         # Agent workflow tests
pytest -m "security"      # Security/redaction tests

# Single test
pytest tests/llm/test_router.py::TestLLMRouter::test_route_success_first_provider -v

# Advanced test runner with linting and type checking
python run_tests.py --all
python run_tests.py --unit --coverage --html
```

### Code Quality
```bash
# Pre-commit hooks (install once)
pre-commit install

# Manual linting/formatting
black faultmaven tests
flake8 faultmaven tests
mypy faultmaven
isort faultmaven tests
bandit -r faultmaven  # Security analysis
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
- `tests/api/` - API layer tests (endpoints, request/response)
- `tests/services/` - Service layer tests (orchestration, business logic)
- `tests/core/` - Core domain tests (agent, processing, knowledge)
- `tests/infrastructure/` - Infrastructure tests (LLM, security, observability)
- `tests/unit/` - Architecture component tests (DI container, interfaces)
- `tests/integration/` - End-to-end workflows with mocks

**Test Markers**: Tests are categorized by:
- **Domain**: `agent`, `security`, `data_processing`, `llm`, `api`
- **Type**: `unit`, `integration`
- **Layer**: Implicitly organized by directory structure

**Coverage**: 71% current coverage with 341 passing tests. Mock infrastructure for external dependencies.

**Interface Testing**: Comprehensive interface compliance tests ensure implementations meet contracts. DI container provides mock implementations for isolation.

## Critical Implementation Notes

### Dependency Injection System
- **Container Access**: Use `from faultmaven.container import container` for global access
- **Service Resolution**: `container.get_agent_service()`, `container.get_llm_provider()`, etc.
- **Interface Injection**: All services receive dependencies as interfaces (e.g., `ILLMProvider`, `ISanitizer`)
- **Testing Reset**: Use `container.reset()` to clear container state between tests
- **Health Monitoring**: `container.health_check()` provides detailed dependency status

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
- **Session Management**: Redis-backed sessions with configurable timeout
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

### Observability Setup
- **Opik Integration**: Default team server at `opik.faultmaven.local:30080`
- **Hostname Resolution**: Must resolve via `/etc/hosts` (no hardcoded IPs)
- **Custom Configurations**: Use config scripts in `scripts/config/` for different environments
- **Automatic Tracing**: All LLM calls and agent workflows traced via `ITracer` interface
- **Health Endpoints**: `/health/dependencies` shows container health