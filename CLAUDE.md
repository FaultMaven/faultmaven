# CLAUDE.md - AI Assistant Guide for FaultMaven

This document provides essential context for AI assistants working with the FaultMaven codebase.

## Project Overview

FaultMaven is an **AI-powered troubleshooting copilot** for modern engineering teams. It correlates live telemetry with runbooks, documentation, and past fixes to deliver contextual AI-driven incident investigation.

**Key Value Propositions:**
- Evidence-centric investigation (logs, metrics, configs, past solutions)
- Knowledge flywheel (learns from resolved incidents)
- Multi-LLM support (OpenAI, Anthropic, Fireworks, local Ollama)
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
│   ├── processing/         # Log analysis, pattern learning
│   └── confidence/         # Confidence scoring
├── infrastructure/         # Shared adapters
│   ├── llm/                # LLM provider routing, caching
│   ├── persistence/        # Database layer (SQLAlchemy)
│   ├── knowledge/          # Vector databases (ChromaDB)
│   ├── auth/               # JWT, bcrypt, RBAC
│   ├── security/           # PII protection
│   └── caching/            # Redis sessions
├── config/                 # Pydantic-settings configuration
├── container/              # Dependency injection
└── services/               # Service layer
```

### Module Types

**Vertical Modules (Auth, Case, Knowledge):**
- Own database tables
- Have `contracts.py` - exposes ICaseRepository, models
- Have `infrastructure/` - repositories for persistence
- Other modules import from their contracts

**Domain Services (Evidence, Agent, Report):**
- Business logic only, NO data ownership
- NO `contracts.py` (nothing to expose)
- NO `infrastructure/` (use Case repository)
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
# ✅ CORRECT: Domain Service uses Vertical Module's contract
from faultmaven.modules.case.contracts import ICaseRepository, EvidenceArtifact

# ❌ WRONG: Domain Service bypasses contracts
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository

# ❌ WRONG: Vertical Module imports Domain Service internals
from faultmaven.modules.evidence.domain.validators import validate_evidence
```

### Architecture Enforcement

Architecture is enforced via **import-linter** with 13 contracts (`.importlinter`):
- Service layer independence
- Services cannot import API layer
- Models cannot import services
- Module layer boundaries (Knowledge, Case, Auth, Evidence, Report)
- Cross-module boundaries (other modules use auth contracts only)
- **Domain Services cannot bypass Case contracts** (new)
- **Other modules use Case contracts for shared models** (new)

**Run architecture checks:**
```bash
lint-imports
```

## Tech Stack

| Layer | Technologies |
|-------|--------------|
| Framework | Python 3.11+, FastAPI 0.115+, Uvicorn, AsyncIO |
| LLM/AI | LangGraph, LangChain, OpenAI, Anthropic, Fireworks |
| Database | SQLAlchemy 2.0+, SQLite (local), PostgreSQL (prod), Alembic |
| Vector DB | ChromaDB, sentence-transformers |
| Cache | Redis (optional), in-memory fallback |
| Auth | JWT (PyJWT), bcrypt, RBAC |
| Testing | pytest 8.0+, pytest-asyncio, pytest-cov, factory-boy |
| Code Quality | ruff, black, isort, mypy, import-linter |

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

### CLI Commands

**Docker-based (faultmaven.sh):**
```bash
./faultmaven.sh start              # Start services
./faultmaven.sh start --demo       # Start with demo data
./faultmaven.sh stop               # Stop services
./faultmaven.sh status             # Check health
./faultmaven.sh logs [service]     # View logs
./faultmaven.sh restart            # Restart all
./faultmaven.sh create-user        # Create user account
```

**Local development (scripts/faultmaven-dev.sh):**
```bash
./scripts/faultmaven-dev.sh start  # Start API as local process
./scripts/faultmaven-dev.sh health # Run health checks
./scripts/faultmaven-dev.sh test   # Run tests
```

## Testing

### Test Structure

```
tests/
├── unit/           # Fast, isolated (~100 files)
├── integration/    # Cross-layer workflows (~25 files)
├── infrastructure/ # External service tests
├── benchmarks/     # Performance baselines
├── performance/    # Overhead validation
├── health/         # Docker smoke tests
└── load/           # Locust stress tests
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
| `asyncio` | Async tests |

### Writing Tests

```python
import pytest

# Async test
@pytest.mark.asyncio
async def test_async_operation():
    result = await some_async_function()
    assert result is not None

# Use fixtures from conftest.py
def test_with_container(reset_container):
    service = reset_container.get_agent_service()
    assert service is not None
```

## Code Quality

### Linting and Formatting

```bash
# Lint with ruff
ruff check .

# Format
black .
isort .

# Type check
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
| LLM | `CHAT_PROVIDER`, `OPENAI_API_KEY` | Primary LLM provider |
| Database | `DATABASE_URL` | SQLite (default) or PostgreSQL |
| Sessions | `SESSION_STORAGE_TYPE` | `inmemory` or `redis` |
| Vectors | `VECTOR_STORAGE_TYPE` | `inmemory` or `chromadb` |
| Security | `JWT_SECRET_KEY`, `CORS_ALLOW_ORIGINS` | Auth settings |

### Storage Backends

**Local (default):**
- SQLite database
- In-memory sessions/vectors
- Local filesystem storage

**Production:**
- PostgreSQL database
- Redis sessions
- ChromaDB vectors
- S3/Azure blob storage

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

- `sessions` - User session management
- `cases` - Investigation cases
- `case_messages` - Conversation history
- `investigation_sessions` - Agent interaction records
- `evidence_artifacts` - Uploaded files
- `knowledge_documents` - KB items
- `users` - User accounts with RBAC

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

| Endpoint | Description |
|----------|-------------|
| `/cases` | Case management |
| `/cases/{id}/sessions/{sid}/execute` | Start AI investigation |
| `/knowledge/documents` | Knowledge base CRUD |
| `/knowledge/search` | Semantic search |
| `/auth/register`, `/auth/login` | Authentication |
| `/evidence/upload` | File upload |

**Documentation:** http://localhost:8090/docs

## Important Files

| File | Purpose |
|------|---------|
| `faultmaven/main.py` | FastAPI application entry point |
| `faultmaven/config/settings.py` | Pydantic settings |
| `faultmaven/container/` | Dependency injection setup |
| `.env.example` | Configuration template |
| `pyproject.toml` | Dependencies and tool config |
| `.importlinter` | Architecture contracts |
| `pytest.ini` | Test configuration |
| `alembic/` | Database migrations |

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

### Modifying Database Schema

1. Update SQLAlchemy models in `infrastructure/persistence/models/`
2. Create migration: `alembic revision --autogenerate -m "description"`
3. Review generated migration in `alembic/versions/`
4. Apply: `alembic upgrade head`

## Security Considerations

- **PII Redaction** - Automatic scrubbing via Presidio
- **JWT Auth** - Stateless session management
- **RBAC** - Role-based access control
- **Secret Detection** - Pre-commit hooks prevent credential commits
- **CORS** - Configurable origins for browser extension

## Documentation

```
docs/
├── architecture/     # System design, ADRs
├── guides/           # How-to guides
├── development/      # Dev standards
├── operations/       # Runbooks, monitoring
└── reference/        # API docs, config reference
```

## Troubleshooting

### Import Errors
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### Async Test Issues
Ensure `@pytest.mark.asyncio` decorator is used.

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

## Version Info

- **Current Version:** 1.0.0
- **Python Support:** 3.11, 3.12, 3.13
- **License:** Apache 2.0
