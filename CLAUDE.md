# CLAUDE.md - AI Assistant Guide for FaultMaven

This document provides essential context for AI assistants working with the FaultMaven codebase.

## Project Overview

FaultMaven is an **AI-powered troubleshooting copilot** for modern engineering teams. It correlates live telemetry with runbooks, documentation, and past fixes to deliver contextual AI-driven incident investigation.

**Key Value Propositions:**
- Evidence-centric investigation (logs, metrics, configs, past solutions)
- Knowledge flywheel (learns from resolved incidents)
- Multi-LLM support (9 providers: OpenAI, Anthropic, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter, local Ollama/vLLM)
- Zero context-switching (browser extension integrates into existing tools)

**System Components:**
| Component | Purpose |
|-----------|---------|
| FaultMaven API (this repo) | Backend investigation engine, knowledge base, AI orchestration |
| FaultMaven Dashboard | Web UI for KB management and case history |
| FaultMaven Copilot | Browser extension for in-context troubleshooting |

## Architecture

### Module Architecture (Vertical Slice + Domain Services)

FaultMaven uses a hybrid architecture with **Vertical Modules** (own data) and **Domain Services** (business logic only):

```
faultmaven/
├── main.py                 # FastAPI entry point
├── api/                    # Shared API middleware, dependencies, error handling
│   ├── dependencies.py     # DI for legacy code
│   ├── exception_handlers.py
│   ├── middleware/         # 11 middleware modules
│   └── routes/             # Legacy routes (admin, auth, cases, evidence, sessions, users)
├── modules/                # Feature modules
│   │
│   │ # VERTICAL MODULES (own database tables, have contracts.py + infrastructure/)
│   ├── auth/               # Authentication, JWT, OAuth 2.0, RBAC
│   ├── case/               # Investigation cases (owns evidence, reports, agent_executions)
│   ├── knowledge/          # Knowledge base, RAG, vector search
│   │
│   │ # DOMAIN SERVICES (business logic only, NO contracts.py, NO infrastructure/)
│   ├── agent/              # Investigation orchestration & AI tools
│   ├── evidence/           # Evidence processing (uses Case repository)
│   └── report/             # Report generation (uses Case repository)
│
├── core/                   # Core investigation engine
│   ├── investigation/      # OODA framework, milestone engine
│   ├── knowledge/          # Knowledge ingestion and retrieval
│   ├── preprocessing/      # Log analysis, pattern learning
│   └── confidence/         # Confidence scoring
├── infrastructure/         # Shared adapters (20 subdirectories)
│   ├── llm/                # LLM provider routing, caching
│   ├── persistence/        # Database layer (SQLAlchemy, 20 repository files)
│   ├── knowledge/          # Vector databases (ChromaDB)
│   ├── auth/               # JWT, bcrypt, RBAC
│   ├── security/           # PII protection (Presidio)
│   ├── caching/            # Redis sessions
│   ├── storage/            # File storage (local, S3, Azure)
│   ├── logging/            # Structured logging (structlog)
│   ├── observability/      # Opik tracing, Prometheus metrics
│   └── health/             # Health checks
├── config/                 # Pydantic-settings configuration
├── container/              # Dependency injection
├── services/               # Legacy service layer
└── models/                 # Legacy models
```

### Module Types

**Vertical Modules (Auth, Case, Knowledge):**
- Own database tables
- Have `contracts.py` - exposes interfaces (ICaseRepository, etc.) and models
- Have `infrastructure/` - repositories for persistence
- Other modules import from their contracts

**Domain Services (Evidence, Agent, Report):**
- Business logic only, NO data ownership
- NO `contracts.py` (nothing to expose)
- NO `infrastructure/` (use Case repository via contracts)
- Import models from Case contracts

```
# Vertical Module structure (Case, Auth, Knowledge)
module/
├── contracts.py            # Public interfaces (ICaseRepository, models)
├── api/
│   └── routes.py           # FastAPI endpoints
├── domain/
│   ├── models/             # Domain entities
│   ├── owned_models/       # Case-owned shared models (evidence, report, agent_execution)
│   └── services/           # Business logic
└── infrastructure/
    └── persistence/        # Repositories

# Domain Service structure (Evidence, Agent, Report)
module/
├── api/
│   └── routes.py           # FastAPI endpoints
└── domain/
    ├── models.py           # Re-exports from Case contracts (backward compat)
    └── services/           # Business logic (uses Case repository)
```

### Cross-Module Import Rules

```python
# CORRECT: Domain Service uses Vertical Module's contract
from faultmaven.modules.case.contracts import ICaseRepository, EvidenceArtifact

# WRONG: Domain Service bypasses contracts
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository

# WRONG: Vertical Module imports Domain Service internals
from faultmaven.modules.evidence.domain.validators import validate_evidence
```

### Architecture Enforcement

Architecture is enforced via **import-linter** with 13 contracts (`.importlinter`):

| Contract | Description | Status |
|----------|-------------|--------|
| 1 | Service layer independence | Active |
| 2 | Services cannot import API layer | Active |
| 3 | Models cannot import services | Active |
| 4 | Knowledge module layer boundaries | Active |
| 5 | Case module layer boundaries | Active |
| 6 | Auth module layer boundaries | Disabled (TODO) |
| 7 | Agent module layer boundaries | Disabled (TODO) |
| 8 | Evidence module layer boundaries | Active |
| 9 | Report module layer boundaries | Active |
| 10 | Other modules use auth contracts only | Active |
| 11 | No direct database access to auth tables | Active |
| 12 | Domain Services use Case contracts, not infrastructure | Active |
| 13 | Other modules use Case contracts for shared models | Disabled |

**Run architecture checks:**
```bash
lint-imports
```

## Tech Stack

| Layer | Technologies |
|-------|--------------|
| Framework | Python 3.11+, FastAPI 0.115.8+, Uvicorn, AsyncIO |
| LLM/AI | LangGraph 0.1.2+, LangChain 0.1.15+, OpenAI, Anthropic, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter |
| Database | SQLAlchemy 2.0+, SQLite (local), PostgreSQL (prod), Alembic 1.13+ |
| Vector DB | ChromaDB 0.5.3+, sentence-transformers 3.0.1+ |
| Cache | Redis 5.0+ (optional), in-memory fallback |
| Auth | JWT (PyJWT 2.8+), bcrypt, RBAC |
| Observability | Opik 0.2.1+ (tracing), Prometheus (metrics), structlog (logging) |
| Security | Presidio 2.2+ (PII redaction), cryptography 41+ |
| Testing | pytest 8.0+, pytest-asyncio, pytest-cov, factory-boy, locust |
| Code Quality | ruff 0.2+, black 24.10, isort 5.12+, mypy 1.8+, import-linter 2.0+ |

### Supported LLM Providers

| Provider | Environment Variable | Notes |
|----------|---------------------|-------|
| Anthropic | `ANTHROPIC_API_KEY` | Recommended for logic |
| OpenAI | `OPENAI_API_KEY` | Recommended for consistency |
| Google Gemini | `GEMINI_API_KEY` | Fast multimodal |
| Fireworks AI | `FIREWORKS_API_KEY` | Fast & cheap |
| Groq | `GROQ_API_KEY` | Ultra-fast inference |
| HuggingFace | `HUGGINGFACE_API_KEY` | Open models |
| Cohere | `COHERE_API_KEY` | Enterprise RAG |
| OpenRouter | `OPENROUTER_API_KEY` | Multi-model gateway |
| Local (Ollama/vLLM) | `LOCAL_LLM_URL` | Private & offline |

## Development Workflow

### Quick Start

```bash
# Clone and setup
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven
cp .env.example .env
# Edit .env: Set CHAT_PROVIDER and API key

# Start with Docker
./faultmaven.sh start

# Or run locally (development)
./scripts/faultmaven-dev.sh start
```

### Service Ports

| Service | Port | Description |
|---------|------|-------------|
| API | 8090 | REST API |
| Dashboard | 3333 | Web UI |
| API Docs | 8090/docs | Swagger UI |
| ChromaDB | 8001 | Vector DB (dev mode) |
| Redis | 6379 | Sessions (dev mode) |

### CLI Commands

**Docker-based (faultmaven.sh v2.0.0):**
```bash
./faultmaven.sh start              # Start services
./faultmaven.sh start --demo       # Start with demo data
./faultmaven.sh stop               # Stop services
./faultmaven.sh status             # Check health
./faultmaven.sh logs [service]     # View logs
./faultmaven.sh restart            # Restart all
./faultmaven.sh create-user        # Create user account
./faultmaven.sh test               # Run tests
./faultmaven.sh health             # Health checks
```

**Local development (scripts/faultmaven-dev.sh):**
```bash
./scripts/faultmaven-dev.sh start  # Start API as local process
./scripts/faultmaven-dev.sh stop   # Stop local process
./scripts/faultmaven-dev.sh status # Check process status
./scripts/faultmaven-dev.sh health # Run health checks
./scripts/faultmaven-dev.sh test   # Run tests
./scripts/faultmaven-dev.sh logs   # View logs
```

**Utility Scripts (scripts/):**
```bash
python scripts/create_builtin_accounts.py  # Create default users
python scripts/generate_oauth_keys.py      # Generate OAuth RSA keys
./scripts/db_migrate.sh                    # Database migrations
python scripts/verify_vector_storage.py    # Verify ChromaDB
```

## Testing

### Test Structure

```
tests/
├── unit/              # Fast, isolated tests (~100+ files)
│   ├── modules/       # Module-specific (agent, auth, evidence)
│   ├── api/           # API endpoints, middleware
│   ├── infrastructure/ # Persistence, logging
│   ├── services/      # Service layer
│   └── core/          # Investigation engine
├── integration/       # Cross-layer workflows
│   ├── api/
│   └── modules/
├── infrastructure/    # External service tests
├── benchmarks/        # Performance baselines
├── performance/       # Overhead validation
├── health/            # Docker smoke tests
├── load/              # Locust stress tests
└── installation/      # Setup verification
```

### Running Tests

```bash
# All tests
./faultmaven.sh test

# By category
./faultmaven.sh test --unit
./faultmaven.sh test --integration
./faultmaven.sh test --coverage

# CI modes
./faultmaven.sh test --ci           # Fast (unit, parallel)
./faultmaven.sh test --ci-full      # Unit + integration
./faultmaven.sh test --ci-nightly   # All including benchmarks

# Direct pytest
pytest tests/unit/
pytest -k "test_case"
pytest -m "security"
pytest -m "llm"
```

### Test Markers

| Marker | Description |
|--------|-------------|
| `unit` | Unit tests (fast, isolated) |
| `integration` | Cross-layer workflows |
| `slow` | Long-running (excluded from fast CI) |
| `enterprise` | Requires Redis, PostgreSQL |
| `security` | Security-focused tests |
| `benchmark` | Performance benchmarks |
| `performance` | Overhead validation tests |
| `asyncio` | Async tests |
| `api` | API endpoint tests |
| `llm` | LLM-related tests |
| `agent` | Agent-related tests |
| `session` | Session management tests |
| `knowledge_base` | Knowledge base tests |
| `architecture` | Architecture validation tests |

### Writing Tests

```python
import pytest

# Async test (asyncio_mode = auto in pyproject.toml)
@pytest.mark.asyncio
async def test_async_operation():
    result = await some_async_function()
    assert result is not None

# Use fixtures from conftest.py
def test_with_container(reset_container):
    service = reset_container.get_agent_service()
    assert service is not None

# Mark with category
@pytest.mark.unit
@pytest.mark.llm
async def test_llm_provider():
    pass
```

## Code Quality

### Linting and Formatting

```bash
# Lint with ruff
ruff check .

# Format
black .
isort .

# Type check (limited files configured)
mypy faultmaven/

# Architecture validation
lint-imports
```

### Pre-commit Hooks

Pre-commit hooks (`.pre-commit-config.yaml`) include:
- **detect-secrets** - API key detection
- **check-api-keys** - Custom API key patterns
- **check-hardcoded-rsa-keys** - RSA key detection
- Standard hooks (JSON/YAML validation, trailing whitespace)

**Install hooks:**
```bash
pip install pre-commit
pre-commit install
```

## Configuration

### Environment Variables

Key configuration in `.env`:

| Category | Variables | Description |
|----------|-----------|-------------|
| LLM | `CHAT_PROVIDER`, `*_API_KEY` | Primary LLM provider |
| Capability Overrides | `CODE_PROVIDER`, `MULTIMODAL_PROVIDER`, `SYNTHESIS_PROVIDER`, `CLASSIFIER_PROVIDER` | Override specific agents |
| External Tools | `ENABLE_WEB_SEARCH`, `TAVILY_API_KEY` | Web search capability |
| Database | `DATABASE_URL`, `DB_BACKEND` | SQLite (default) or PostgreSQL |
| Sessions | `CACHE_BACKEND`, `REDIS_URL` | `inmemory` or `redis` |
| Vectors | `VECTOR_BACKEND`, `CHROMADB_URL` | `inmemory` or `chromadb` |
| OAuth | `OAUTH_ENABLED`, `DASHBOARD_URL` | OAuth 2.0 settings |
| Security | `CORS_ALLOW_ORIGINS`, `CORS_ALLOW_CREDENTIALS` | CORS settings |
| Limits | `MAX_UPLOAD_SIZE_MB`, `RATE_LIMIT_REQUESTS_PER_MINUTE` | Rate limiting |

### Storage Backends

**Local (default - Community Edition):**
- SQLite database
- In-memory sessions/vectors
- Local filesystem storage

**Production (Enterprise Edition):**
- PostgreSQL database
- Redis sessions
- ChromaDB vectors
- S3/Azure blob storage
- Presidio PII redaction
- Opik tracing

## Database

### Migrations

```bash
# Create migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Downgrade
alembic downgrade -1
```

### Key Tables

| Table | Description |
|-------|-------------|
| `users` | User accounts with RBAC |
| `sessions` | User session management |
| `cases` | Investigation cases |
| `case_messages` | Conversation history |
| `investigation_sessions` | Agent interaction records |
| `evidence_artifacts` | Uploaded files |
| `knowledge_documents` | Knowledge base items |

### Migration History (10 versions)

1. Baseline schema
2. Session management
3. Evidence artifacts
4. Agent executions
5. Investigation sessions
6. Knowledge items
7. Users table
8. Hypothesis/solution multitenancy
9. Standalone evidence
10. Email uniqueness constraint

## Key Patterns

### Investigation Framework (OODA Loop)

The investigation engine uses OODA (Observe, Orient, Decide, Act):
- **Observe** - Data collection and evidence analysis
- **Orient** - Context building with knowledge base
- **Decide** - Hypothesis generation using AI
- **Act** - Tool execution and refinement

Implemented in `core/investigation/ooda_engine.py`.

### Dependency Injection

- DI Container in `faultmaven/container/`
- Service locator pattern
- Provider pattern for pluggable implementations

### Async Throughout

- FastAPI async endpoints
- Async database drivers (aiosqlite, asyncpg)
- Concurrent LLM calls via `asyncio.gather()`

## API Endpoints

**Base URL:** `http://localhost:8090/api/v1`

| Module | Endpoint | Description |
|--------|----------|-------------|
| Cases | `/cases` | Case management CRUD |
| Agent | `/cases/{id}/sessions/{sid}/execute` | Start AI investigation |
| Knowledge | `/knowledge/documents` | Knowledge base CRUD |
| Knowledge | `/knowledge/search` | Semantic search |
| Auth | `/auth/register`, `/auth/login` | Authentication |
| Auth | `/auth/oauth/*` | OAuth 2.0 flow |
| Evidence | `/evidence/upload` | File upload |
| Reports | `/reports` | Report generation |

**Documentation:** http://localhost:8090/docs

## Important Files

| File | Purpose |
|------|---------|
| `faultmaven/main.py` | FastAPI application entry point |
| `faultmaven/config/settings.py` | Pydantic settings |
| `faultmaven/container/` | Dependency injection setup |
| `.env.example` | Configuration template |
| `pyproject.toml` | Dependencies and tool config |
| `.importlinter` | Architecture contracts (13 rules) |
| `pytest.ini` | Test configuration |
| `alembic/` | Database migrations (10 versions) |

## Common Tasks

### Adding a New API Endpoint

1. Add route in appropriate module's `api/routes.py`
2. Add business logic in module's `domain/services/`
3. Add tests in `tests/unit/modules/` and `tests/integration/`
4. Run `lint-imports` to verify architecture

### Adding a New LLM Provider

1. Implement provider in `infrastructure/llm/providers/`
2. Register in `infrastructure/llm/llm_router.py`
3. Add config in `config/settings.py`
4. Document in `.env.example`
5. Add tests in `tests/unit/infrastructure/`

### Modifying Database Schema

1. Update SQLAlchemy models in `infrastructure/persistence/models/`
2. Create migration: `alembic revision --autogenerate -m "description"`
3. Review generated migration in `alembic/versions/`
4. Apply: `alembic upgrade head`

### Adding a New Module

**Vertical Module (owns data):**
1. Create `modules/newmodule/` with `contracts.py`, `api/`, `domain/`, `infrastructure/`
2. Define interfaces in `contracts.py`
3. Add repository in `infrastructure/persistence/`
4. Add layer boundary contract in `.importlinter`
5. Register routes in `main.py`

**Domain Service (no data ownership):**
1. Create `modules/newservice/` with `api/`, `domain/` only
2. Import shared models from Case contracts
3. Use Case repository via DI
4. Add layer boundary contract in `.importlinter`

## Security Considerations

- **PII Redaction** - Automatic scrubbing via Presidio (enterprise)
- **JWT Auth** - Stateless session management with RS256
- **OAuth 2.0** - Browser extension integration
- **RBAC** - Role-based access control
- **Secret Detection** - Pre-commit hooks prevent credential commits
- **CORS** - Configurable origins for browser extension

## Documentation

```
docs/
├── architecture/           # System design, ADRs
│   ├── api-and-integration/
│   ├── case-and-session/
│   ├── core-architecture/
│   ├── data-and-storage/
│   ├── investigation-engine/
│   ├── knowledge-and-ai/
│   └── security/
├── guides/                 # How-to guides
├── development/            # Dev standards
├── operations/             # Runbooks, monitoring
└── reference/              # API docs, config reference
```

## Troubleshooting

### Import Errors
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### Async Test Issues
Ensure `@pytest.mark.asyncio` decorator is used. asyncio_mode is set to "auto" in pyproject.toml.

### Port Conflicts
```bash
./faultmaven.sh stop
# Or check what's using ports:
lsof -i :8090
lsof -i :3333
```

### Database Issues
```bash
# Reset database
rm -rf data/faultmaven.db
alembic upgrade head
```

### Architecture Violations
```bash
# Check violations
lint-imports

# The output shows which imports violate contracts
# Fix by using contracts.py interfaces instead of direct imports
```

### LLM Provider Issues
```bash
# Verify provider configuration
echo $CHAT_PROVIDER
echo $OPENAI_API_KEY  # (or relevant provider key)

# Check logs for API errors
./faultmaven.sh logs api
```

## Version Info

- **Current Version:** 1.0.0
- **Python Support:** 3.11, 3.12, 3.13
- **License:** Apache 2.0
- **Min Python:** 3.11
