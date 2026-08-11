# CLAUDE.md - AI Assistant Guide for FaultMaven

This document provides essential context for AI assistants working with the FaultMaven codebase.

## Project Overview

FaultMaven is an **AI-powered troubleshooting copilot**. It correlates the logs, metrics, and configs you share with runbooks, documentation, and past fixes to deliver contextual AI-driven incident investigation. It works a problem the way a seasoned engineer does — goal-driven, methodical, evidence-based, self-learning — and never forgets what it learns.

**Key Value Propositions:**
- Evidence-centric investigation (logs, metrics, configs, past solutions)
- Knowledge flywheel (learns from resolved incidents)
- Multi-LLM support (9 providers: Anthropic, OpenAI, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter, Local Ollama/vLLM)
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
│   ├── middleware/         # 12 middleware modules
│   │   ├── auth.py                    # JWT/OAuth authentication
│   │   ├── client_ip.py               # Trusted-proxy client IP resolution
│   │   ├── contract_probe.py          # API contract validation
│   │   ├── deduplication.py           # Request deduplication
│   │   ├── idempotency.py             # Idempotent request handling
│   │   ├── logging.py                 # Request/response logging
│   │   ├── performance.py             # Performance monitoring
│   │   ├── rate_limiting.py           # Rate limiting
│   │   ├── request_id.py              # Request ID injection
│   │   ├── system_optimization.py     # System optimization
│   │   ├── tenant_scope.py            # Request-scoped tenant binding
│   │   └── trailing_slash.py          # URL normalization
│   ├── v1/                 # API v1 utilities and dependencies
│   └── routes/             # Admin routes (admin.py, admin_config.py, sessions.py)
├── modules/                # Feature modules (primary code organization)
│   │
│   │ # VERTICAL MODULES (own database tables, have contracts.py + infrastructure/)
│   ├── auth/               # Authentication, JWT, OAuth 2.0, RBAC
│   ├── case/               # Investigation cases (owns evidence, reports, agent_executions)
│   ├── knowledge/          # Knowledge base, RAG, vector search
│   │
│   │ # DOMAIN SERVICES (business logic only, NO contracts.py, NO infrastructure/)
│   ├── agent/              # Investigation orchestration & AI tools
│   ├── evidence/           # Evidence processing (uses Case repository)
│   ├── preprocessing/      # Data classification, extraction (11 extractors), chunking
│   └── report/             # Report generation (uses Case repository)
│
├── core/                   # Core investigation engine
│   ├── investigation/      # Milestone-based investigation framework
│   │   ├── milestone_engine.py      # Main investigation orchestrator (process_turn)
│   │   ├── hypothesis_manager.py    # Hypothesis lifecycle & confidence scoring
│   │   ├── schemas.py               # Pydantic schemas for structured LLM output
│   │   ├── working_conclusion_generator.py  # Progress metrics
│   │   └── prompts/                 # Prompt templates and context building
│   │       ├── templates.py         # INQUIRY/INVESTIGATING/TERMINAL templates
│   │       └── context_builder.py   # Token-aware context assembly
│   ├── preprocessing/      # Tier 0/1 mechanical preprocessor
│   └── processing/         # Log analyzer, pattern learner
├── infrastructure/         # Shared adapters (13 subdirectories)
│   ├── llm/                # LLM provider routing, caching
│   │   ├── providers/      # 9 LLM providers (see Supported LLM Providers)
│   │   ├── router.py       # Provider routing with fallback chain
│   │   ├── cache.py        # Response caching
│   │   └── local_llm_manager.py
│   ├── persistence/        # Database layer (SQLAlchemy)
│   ├── knowledge/          # Vector databases (ChromaDB)
│   ├── auth/               # JWT, bcrypt, RBAC, user stores
│   ├── security/           # PII protection (Presidio)
│   ├── protection/         # System protection and rate limiting
│   ├── caching/            # Intelligent cache
│   ├── storage/            # File storage (local, S3, Azure)
│   ├── logging/            # Structured logging (structlog), coordinator
│   ├── observability/      # Opik tracing, Prometheus metrics, APM, alerting, SLA, confidence/dashboard services
│   ├── health/             # Health checks, SLA tracker, component monitor
│   ├── jobs/               # Background job service
│   ├── tasks/              # Async task management
│   ├── shims/              # Compatibility shims (enterprise feature flags)
│   └── concurrency/        # Report lock manager
├── bootstrap/              # Application startup and service factories
├── cli/                    # Operator console entrypoints (fm-*, [project.scripts])
├── config/                 # Pydantic-settings configuration
│   ├── settings.py         # Main settings with validation
│   ├── presets.py          # Configuration presets
│   ├── feature_flags.py    # Feature toggles
│   └── protection.py       # Protection configuration
├── container/              # Dependency injection
│   ├── base.py             # Base container
│   ├── registry.py         # Service registry
│   └── providers/          # Infrastructure, services, tools providers
├── services/               # Shared service utilities (BaseService class + request-scoped DI factory)
└── models/                 # Shared interfaces, API schemas, domain models
```

### Module Types

**Vertical Modules (Auth, Case, Knowledge):**
- Own database tables
- Have `contracts.py` - exposes interfaces (ICaseRepository, etc.) and DTOs
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
├── contracts.py            # Public interfaces (ICaseRepository, DTOs)
├── api/
│   └── routes.py           # FastAPI endpoints
├── domain/
│   ├── models/             # Domain entities
│   ├── owned_models/       # Case-owned shared models (evidence, report, agent_execution)
│   └── services/           # Business logic
├── infrastructure/
│   └── persistence/        # Repositories
│       └── stores/         # Session/token stores (auth module)
└── exceptions.py           # Module-specific exceptions

# Case module infrastructure (multiple repository implementations)
modules/case/infrastructure/
├── case_repository.py              # Abstract base repository
├── sqlite_case_repository.py       # SQLite implementation (default)
├── postgresql_hybrid_case_repository.py  # PostgreSQL implementation
├── database_case_repository.py     # Generic database repository
├── sessionless_case_repository.py  # Sessionless repository variant
├── investigation_session_repository.py  # Session management
└── case_vector_store.py            # Vector storage for cases

# Domain Service structure (Evidence, Agent, Report)
module/
├── api/
│   └── routes.py           # FastAPI endpoints
├── domain/
│   ├── models.py           # Re-exports from Case contracts (backward compat)
│   └── services/           # Business logic (uses Case repository)
├── tools/                  # Agent tools (agent module only)
└── exceptions.py           # Module-specific exceptions

# Agent module tools (investigation capabilities)
modules/agent/tools/
├── base.py                 # Base tool class
├── kb_qa.py                # Unified KB Q&A (answer_from_kb — all scopes via metadata filter)
├── case_evidence_qa.py     # Case evidence queries (answer_from_case_evidence)
├── knowledge_base.py       # Generic KB interaction tool
├── document_qa_tool.py     # Document Q&A
├── kb_tool_adapter.py      # Adapter between unified kb_qa and downstream tools
├── kb_config.py            # Shared KB tool configuration
├── list_evidence_tool.py   # List available evidence
├── read_file_tool.py       # File reading tool
├── search_file_tool.py     # Query strategy: keyword/regex/extractor search on raw files
├── deep_analysis_tool.py   # Query strategy: interpreted search (dedicated LLM call on file sections)
├── vectorize_file_tool.py  # Vectorize a file into the case vector store
├── web_search.py           # Web search integration (Tavily)
└── kb_configs/             # KB tool configurations
    ├── unified_kb_config.py
    └── case_evidence_config.py
```

### Cross-Module Import Rules

```python
# CORRECT: Domain Service uses Vertical Module's contract
from faultmaven.modules.case.contracts import ICaseRepository, EvidenceArtifact

# CORRECT: Use auth contracts for DTOs
from faultmaven.modules.auth.contracts import UserDTO, AuthTokenDTO

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

## Authentication System

FaultMaven uses a unified JWT-based authentication system supporting two modes:

### Auth Modes

| Mode | Algorithm | Use Case | Configuration |
|------|-----------|----------|---------------|
| `local` | HS256 (symmetric) | Self-hosted, single-user | `AUTH_MODE=local`, `JWT_SECRET_KEY` |
| `oauth` | RS256 (asymmetric) | Cloud, multi-user, browser extension | `AUTH_MODE=oauth`, RSA key pair |

### Key Auth Components

```
modules/auth/
├── contracts.py                    # Public DTOs (UserDTO, AuthTokenDTO, SessionDTO)
├── api/
│   ├── auth.py                     # Login, register, token refresh
│   ├── oauth.py                    # OAuth 2.0 flow with PKCE
│   ├── session.py                  # Session management
│   ├── teams.py                    # GET /teams — list the caller's teams (read-only)
│   └── rate_limiting.py            # Auth-specific rate limiting
├── domain/
│   ├── models/                     # User, Session, RBAC, Organization models
│   └── services/
│       ├── auth_service.py         # Core authentication logic
│       ├── auth_session_service.py # Session management service
│       ├── oauth_service.py        # OAuth 2.0 implementation
│       ├── jwt_token_generator.py  # RS256/HS256 token generation
│       ├── user_service.py         # User CRUD operations
│       ├── organization_service.py # Organization management
│       └── team_service.py         # Team management
└── infrastructure/
    ├── repositories/               # User, session, OAuth code, team, org repositories
    ├── stores/                     # Redis session stores (FakeRedis for local), token revocation
    └── metrics/                    # OAuth metrics tracking
```

### Token Structure (Both Modes)

```json
{
  "sub": "user_id",
  "username": "john",
  "email": "john@example.com",
  "roles": ["user", "admin"],
  "scopes": ["openid", "profile", "email", "cases:read", "cases:write"],
  "exp": 1234567890,
  "iat": 1234567890,
  "iss": "faultmaven",
  "aud": "faultmaven-api",
  "jti": "unique-token-id",
  "type": "access",
  "auth_mode": "local"
}
```

## Tech Stack

| Layer | Technologies |
|-------|--------------|
| Framework | Python 3.11+, FastAPI 0.115.8+, Uvicorn, AsyncIO |
| LLM/AI | OpenAI, Anthropic, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter |
| Database | SQLAlchemy 2.0+, SQLite (local), PostgreSQL (prod), Alembic 1.13+ |
| Vector DB | ChromaDB 0.5.3+, sentence-transformers 3.0.1+ |
| Cache | Redis 5.0+ (cloud), FakeRedis (local — full API parity, no external server) |
| Auth | JWT (PyJWT 2.8+), bcrypt, RBAC, OAuth 2.0 with PKCE |
| Observability | Opik 0.2.1+ (tracing), Prometheus (metrics), structlog (logging) |
| Security | Presidio 2.2+ (PII redaction), cryptography 41+ |
| Testing | pytest 8.0+, pytest-asyncio, pytest-cov, factory-boy, locust |
| Code Quality | ruff 0.2+, black 24.10, isort 5.12+, mypy 1.8+, import-linter 2.0+ |

### Supported LLM Providers

| Provider | Environment Variable | Models | Structured output | Notes |
|----------|---------------------|--------|-------------------|-------|
| Anthropic | `ANTHROPIC_API_KEY` | claude-sonnet-4-6 | **FUNCTION_CALLING** | Schema enforced via forced tool use; recommended for logic |
| OpenAI | `OPENAI_API_KEY` | gpt-5.4-mini | **STRICT** (gpt-4o+) | Reasoning model; `reasoning_effort` capped to `low` on structured calls (starvation guard); recommended default |
| Google Gemini | `GEMINI_API_KEY` | gemini-3.5-flash | **STRICT** (1.5+) | Fast multimodal; baseline |
| Fireworks AI | `FIREWORKS_API_KEY` | accounts/fireworks/models/deepseek-v4-flash | BEST_EFFORT | Strong open weights, but schema not enforced — see note |
| Groq | `GROQ_API_KEY` | llama-3.3-70b-versatile | BEST_EFFORT (STRICT on gpt-oss) | Ultra-fast inference |
| HuggingFace | `HUGGINGFACE_API_KEY` | Mistral-Large-Instruct-2411 | BEST_EFFORT | Open models — NOT recommended (no tool calling) |
| Cohere | `COHERE_API_KEY` | command-r-plus | BEST_EFFORT | Enterprise RAG (json_object only; not schema-enforced) |
| OpenRouter | `OPENROUTER_API_KEY` | anthropic/claude-sonnet-4-6 | depends on routed model | Multi-model gateway (STRICT for `openai/*`, else FUNCTION_CALLING) |
| Local (Ollama/vLLM) | `LOCAL_LLM_URL` | llama3.2, etc. | FUNCTION_CALLING (functionary/hermes on OpenAI-compatible transport only), else BEST_EFFORT | Private & offline; Ollama `/api/generate` transport can't return tool_calls |

**Structured-output enforcement matters.** The investigation engine drives state
from large schema-constrained LLM responses. **STRICT** providers enforce the
schema natively (tool calling / `json_schema` response_format) — the engine gets
valid state. **BEST_EFFORT** providers only request the schema in-prompt: the
model can omit required fields, the engine drops the `state_updates`, and you get
empty/degraded investigations. **Use a STRICT provider as `CHAT_PROVIDER`**
(OpenAI, Anthropic, Gemini 1.5+). BEST_EFFORT providers (Fireworks incl.
`deepseek-v3`/minimax, Groq, HuggingFace, Local) are fine for cheap
`CLASSIFIER_PROVIDER`/`SYNTHESIS_PROVIDER` overrides but degrade primary CHAT.
Capability is reported per-provider via `get_structured_output_capability()`.
STRICT enforcement is necessary but not sufficient: a STRICT **thinking** model
bills hidden reasoning against `maxOutputTokens`, which can starve the JSON
output on deep-context turns (truncation to `MAX_TOKENS` → 500). The Gemini
provider caps thinking on structured calls for **Gemini 3.x only** via
`thinkingConfig.thinkingLevel: "low"` (3.x dropped the 2.5-era integer
`thinkingBudget`). This is scoped to 3.x because that's where the starvation was
observed (gemini-3.5-flash, the default); Gemini 2.5 is left at native dynamic
thinking — it ran clean, and capping it would change a working reasoning path
without evidence.

**Tool calling is required for the investigation role.** Directed Analysis
(`search_file`, `deep_analysis`) needs function/tool calling; a tool-incapable
model can't gather evidence yet would still conclude — the premature-conclusion
failure FaultMaven guards against. A **startup fail-fast gate**
(`config/investigation_capability.py`, `validate_investigation_tooling`, called
from the lifespan beside the deployment-coherence and credential gates) **refuses
to boot** when the resolved investigation model (`DA_PROVIDER` → `CHAT_PROVIDER`)
can't do tool calling — unless `ALLOW_TOOLLESS_INVESTIGATION=true` (knowing
opt-in to degraded/offline mode; `/health` then reports `degraded`). The per-turn
runtime fallback in `milestone_engine` still covers transient tool failures on an
otherwise-capable model. Capability is per-provider/model via
`supports_tool_calling()` (HuggingFace: always False; Fireworks: a denylist for
models that accept tools but time out on forced `tool_choice=required`).

### Capability Overrides

Different LLM providers can be assigned to specific tasks:

```bash
CHAT_PROVIDER=anthropic      # Default for all tasks
CODE_PROVIDER=openai         # Code generation tasks
MULTIMODAL_PROVIDER=gemini   # Image analysis
SYNTHESIS_PROVIDER=fireworks # Fast JSON generation
CLASSIFIER_PROVIDER=groq     # Query routing
```

## Development Workflow

### Quick Start

```bash
# Clone and setup
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven
cp .env.example .env
# Edit .env: Set CHAT_PROVIDER and API key

# Start with Docker (pulls pre-built images from GHCR by default)
./faultmaven.sh start

# Build from source instead of pulling (contributors)
./faultmaven.sh start --build              # build the API from this repo
./faultmaven.sh start --build-dashboard    # also build the Dashboard from ../faultmaven-dashboard

# Or run locally (development, no Docker)
pip install -e ".[dev]"           # Install dependencies
./scripts/faultmaven-dev.sh start # Start the server
```

**Image source:** `./faultmaven.sh start` runs pre-built images from GHCR (`ghcr.io/faultmaven/faultmaven`, `…/faultmaven-dashboard`), pinnable via `FM_IMAGE_TAG` / `FM_DASHBOARD_IMAGE_TAG` in `.env`. The build path layers `docker-compose.build.yml` (API) / `docker-compose.dashboard-build.yml` (Dashboard) on top of `docker-compose.yml`.

**Config (`.env`) is shared by both run modes.** The Docker stack and the process runner (`faultmaven-dev.sh`) read the *same* `.env` with the *same* parser: compose mounts `.env` read-only at `/app/.env` (not `env_file:`), so values are interpreted identically and `./faultmaven.sh restart` re-reads edits. Container-only overrides live in `docker-compose.yml`'s `environment:` (e.g. `HOST=0.0.0.0`) and take precedence over the file (pydantic env-var > `.env`).

**Auto-Initialization:** On first startup, FaultMaven automatically:
- Creates `data/` directories (database, ChromaDB, evidence, knowledge)
- Runs database migrations
- Creates a default admin account (`admin@local.faultmaven`)
- Bootstraps the KB from the **KB pack**: a self-contained bundle of shipped runbooks + build-time BGE-M3 vectors (`resources/knowledge/pack`, or `KB_PACK_DIR`). Ingestion is atomic + idempotent (content-hash skip) and writes the pack's pre-chunked, pre-embedded chunks straight into `knowledge_items` + ChromaDB — **no embedding model at startup**, so it runs in seconds. Implementation: `faultmaven/bootstrap/kb_init.py` + `kb_pack.py`. The pack is built/owned by `faultmaven-kb-toolkit` (`kb-build-pack`) and vendored here; override `KB_PACK_DIR` to update the KB offline without rebuilding the image. Single-tenant only: under `TENANT_PROVIDER=multi` the web-startup bootstrap is skipped and the pack is seeded via the audited `kb_seed` maintenance job (#770, `docs/operations/evidence-job-scheduling.md`). See [`docs/architecture/knowledge-and-ai/kb-ingestion-architecture.md`](docs/architecture/knowledge-and-ai/kb-ingestion-architecture.md).

Login via dev-login: `POST /api/v1/auth/dev-login` with `{"username": "admin"}`

### Service Ports

| Service | Port | Description |
|---------|------|-------------|
| API | 8090 | REST API |
| Dashboard | 3333 | Web UI |
| API Docs | 8090/docs | Swagger UI |
| ChromaDB | 8000 | Vector DB (external mode) |
| Redis | 6379 | Sessions (external mode) |

### CLI Commands

**Docker-based (faultmaven.sh v2.0.0):**
```bash
./faultmaven.sh start              # Start services (pre-built GHCR images)
./faultmaven.sh start --pull       # Refresh images from registry, then start
./faultmaven.sh start --build      # Build the API from source, then start
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
# User & Account Management
python scripts/create_builtin_accounts.py  # Create default users
python scripts/resolve_duplicate_emails.py # Fix duplicate email issues
python scripts/check_duplicate_emails.py   # Check for duplicate emails

# OAuth & Security
python scripts/generate_oauth_keys.py      # Generate OAuth RSA keys
python scripts/test_rbac.py                # Test RBAC configuration

# Database & Storage
./scripts/db_migrate.sh                    # Database migrations
python scripts/verify_vector_storage.py    # Verify ChromaDB
python scripts/cleanup_corrupt_cases.py    # Database maintenance
python scripts/backfill_closed_at_timestamps.py  # Backfill case timestamps

# Architecture & Validation
python scripts/check_import_violations.py  # Check architecture
python scripts/check_config_compliance.py  # Validate configuration
python scripts/generate_api_docs.py --check  # Detect API reference drift (CI gate)

# Development & Testing
python scripts/setup_env.py                # Environment setup
python scripts/generate_api_docs.py        # Regenerate the API reference (commit the result)
python scripts/frontend_verification_smoke_test.py  # Frontend smoke test
./scripts/run_load_tests.sh                # Run Locust load tests
./scripts/test_integration_logging.sh      # Test integration logging

# User Management — dev-only, run from a checkout (scripts/auth/)
python scripts/auth/create_user.py         # Create a new user
python scripts/auth/list_users.py          # List all users
python scripts/auth/list_users_fast.py     # Fast user listing

# Security (scripts/security/)
./scripts/security/cleanup_exposed_keys_from_history.sh  # Clean secrets from git history

# Local LLM
./scripts/local_llm_service.sh             # Manage local LLM service (Ollama/vLLM)
```

**Operator console entrypoints (`faultmaven/cli/`, `[project.scripts]`):**

Deployment procedures ship *with the installed package*, not as files under
`scripts/` — the wheel excludes `scripts/` and the image never COPYs it, so a
path-based in-pod invocation cannot work (#887). These land on `PATH` wherever
FaultMaven is installed (API pod; locally after `pip install -e .`):

```bash
fm-promote-platform-admin <username>       # Promote user to platform admin (deployment operator)
fm-demote-platform-admin <username>        # Remove platform admin privileges
fm-provision-service-account -u slack-agent  # Mint a service-account OAuth refresh credential (AUTH_MODE=oauth)
fm-provision-sso-org --name ... --slug ... --workos-org-id org_...  # Provision a Cloud tenant + WorkOS org mapping (TENANT_PROVIDER=multi)
fm-reset-kb --dry-run                      # Wipe/re-bootstrap the KB (refuses under TENANT_PROVIDER=multi)

# In a pod:
kubectl exec -it deploy/faultmaven-api -- fm-provision-sso-org --name ...
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
│   ├── cli/           # Operator console entrypoints (fm-*)
│   └── core/          # Investigation engine
├── integration/       # Cross-layer workflows
│   ├── api/           # API integration tests
│   └── modules/       # Module integration (auth, OAuth)
├── infrastructure/    # External service tests
├── benchmarks/        # Performance baselines (excluded from CI)
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

# API reference drift (same check CI runs)
python scripts/generate_api_docs.py --check
```

### API Reference

`docs/reference/api/openapi.json` and `docs/reference/api/README.md` are
**generated** from the running app by `scripts/generate_api_docs.py`. Never edit
them by hand — the `api-contract-drift` CI job regenerates and diffs, so a
change to any route, schema or docstring must ship with its regenerated
artifact in the same PR:

```bash
python scripts/generate_api_docs.py
git add docs/reference/api/openapi.json docs/reference/api/README.md
```

The generator empties the environment and applies its own pinned settings, so
the artifact is a function of the code rather than of your `.env`. It documents
the **maximal deployed surface** (OAuth, SSO and `/metrics` mounted; debug
endpoints, which are development-only, excluded).

⚠️ **Regenerate with the lockfile installed** (`pip install -r
requirements/dev.txt`). FastAPI and Pydantic decide how schemas are emitted, so
the document depends on their versions as well as on the code — a stale local
FastAPI produces a valid-looking artifact that CI rejects, with the diff showing
up in schema shape (`ctx`/`input` on ValidationError, `const` vs a single-value
`enum`, `contentMediaType` vs `format: binary`) rather than in routes.

Which operations require authentication is derived from the dependency graph:
a route gains `security` in the spec because `require_authentication` declares
the `HTTPBearer` scheme. An auth dependency that reads the `Authorization`
header directly emits no `security` and would publish a protected route as
open — `tests/integration/api/test_openapi_documents_auth.py` fails on that,
and on any new auth dependency it has not been told how to classify.

### Pre-commit Hooks

Pre-commit hooks (`.pre-commit-config.yaml`) include:
- **detect-secrets** - API key detection
- **check-api-keys** - Custom API key patterns
- **check-hardcoded-rsa-keys** - RSA key detection
- Standard hooks (JSON/YAML validation, trailing whitespace)

**Install hooks (full suite — recommended, matches CI):**
```bash
pip install pre-commit
pre-commit install
```

**Lightweight alternative (black auto-format only):**
```bash
./scripts/install-git-hooks.sh   # points core.hooksPath at tracked .githooks/
```

Use this when you only want black-on-commit without the full framework. The
hook (`.githooks/pre-commit`) formats only *staged* `.py` files, prefers the
`.venv` black, and warns if its version drifts from the pinned `black==26.3.1`
(CI runs `black --check`, so local formatting must match). To switch back to the
framework: `git config --unset core.hooksPath && pre-commit install`.

## Configuration

### Environment Variables

Key configuration in `.env`:

| Category | Variables | Description |
|----------|-----------|-------------|
| LLM | `CHAT_PROVIDER`, `*_API_KEY` | Primary LLM provider |
| Capability Overrides | `CODE_PROVIDER`, `MULTIMODAL_PROVIDER`, `SYNTHESIS_PROVIDER`, `CLASSIFIER_PROVIDER`, `KNOWLEDGE_PROVIDER` | Override specific agents |
| External Tools | `ENABLE_WEB_SEARCH`, `TAVILY_API_KEY` | Web search capability |
| Database | `DATABASE_URL`, `DB_BACKEND` | SQLite (default) or PostgreSQL |
| Sessions | `REDIS_HOST`, `REDIS_URL` | FakeRedis (default) or real Redis |
| Vectors | `VECTOR_STORAGE_TYPE`, `CHROMADB_URL` | `chromadb` (local PersistentClient by default; external server via `CHROMADB_URL`) |
| Auth | `AUTH_MODE`, `JWT_SECRET_KEY` | `local` or `oauth` |
| OAuth | `OAUTH_ENABLED`, `JWT_PRIVATE_KEY_PATH`, `JWT_PUBLIC_KEY_PATH` | OAuth 2.0 settings |
| JWT | `JWT_ACCESS_TOKEN_EXPIRY_MINUTES`, `JWT_REFRESH_TOKEN_EXPIRY_DAYS` | Access token lifetime in minutes (default 15, max 1440); refresh token lifetime in DAYS (default 7, max 90). Single source, effective in **both** auth modes (local/HS256 and cloud/RS256). Out-of-range values fail at startup; the retired `JWT_*_EXPIRE_*` spelling is rejected at startup |
| Security | `CORS_ALLOW_ORIGINS`, `CORS_ALLOW_CREDENTIALS` | CORS settings |
| Limits | `MAX_UPLOAD_SIZE_MB` | Evidence upload bounds. There is **no rate-limit knob** — limits, windows and the on/off decision live in the presets in `faultmaven/config/protection.py`, chosen by `ENVIRONMENT`. (`SKIP_SERVICE_CHECKS=true` does switch it off, by skipping protection setup entirely — test runs only) |

### Storage Backends

**Standalone (default, self-hosted):**
- SQLite database
- FakeRedis sessions/cache (in-process, no external server)
- Local filesystem storage

**Cloud (FaultMaven-hosted):**
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

### Key Tables (31 total, 4 domains)

**User domain:** `users`, `organizations`, `organization_members`, `roles`, `permissions`, `role_permissions`, `teams`, `team_members`, `user_audit_log`, `oauth_authorization_codes`

**Case domain:** `cases`, `case_messages`, `case_actions`, `case_tags`, `case_checkpoints`, `case_entities`, `evidence`, `hypotheses`, `hypothesis_evidence`, `solutions`, `uploaded_files`, `investigation_sessions`, `agent_executions`, `agent_tool_calls`, `reports`, `conversion_jobs`, `conversion_drafts`

> `agent_executions` / `agent_tool_calls` currently have **no writer**. They were populated by `AgentOrchestrationService` behind the `POST /cases/{id}/sessions/{sid}/execute` endpoint; both were removed once the milestone engine took over turn execution. The tables, their ORM models, and the `ICaseRepository` read/write methods remain, so `get_case_with_details(include_executions=True)` returns an empty list rather than failing. Investigation activity is recorded in `case_messages` and `case_actions`. Dropping them needs a migration and is a separate decision — do not read their emptiness as a bug.

**Knowledge domain (case-adjacent):** `knowledge_items`, `knowledge_suggestions`

**Tenancy:** `enterprises` (top-tier container), with `users.enterprise_id` and `organizations.enterprise_id` NOT NULL FKs.

**Sharing:** `resource_shares` — polymorphic `(resource_type, resource_id, scope_type, scope_id)` association (ADR-013 §D4). Single source of truth for team visibility of runbooks/cases/drafts; replaced the nullable `team_id` columns on `cases`/`knowledge_items`/`conversion_jobs`. v1 `scope_type=team`; `organization` reserved (D4a). Retrieval resolves it to a visible-id allowlist in SQL; ChromaDB metadata never carries team state.

**Config domain:** `config_overrides` (dashboard-managed settings, hot-reloaded at runtime — cloud mode only; local mode uses .env as sole source of truth)

All tables have SQLAlchemy ORM models in `faultmaven/infrastructure/persistence/models.py`. ER diagram: `docs/architecture/data-and-storage/er-diagram.md` (regenerate with `python scripts/generate_er_diagram.py --update`).

### Migration

Baseline `001_clean_baseline` (revision `c4689af8aa3f`) creates 32 tables + RBAC seed data. Subsequent migrations 002–010 cover the post-baseline cleanups: evidence `summary`/`extract` two-field shape (002), enterprise-tier transitional nullability (003), uploaded_files cleanup (004), description CHECK relaxation (005), enterprise-tier NOT NULL tightening (006), drop `users_password_or_sso` CHECK to permit dev-login (007), `case_actions.triggered_by` audit column + read-path wiring (008), Evidence/Solution audit fields (009), and the strict evidence-model redesign (010 — preprocessing artifacts move to `uploaded_files`; `evidence.form` dropped; `evidence_source_invariant` CHECK added so every Evidence row has a known source). Migrations continue through 011–031 (evidence-needs, `status`→`state`, RLS tenant isolation, causal-graph chain model, PostgreSQL type-divergence fixes, causal-table RLS, provenance-column drop, `account_kind`+source, plan-tier rename, drop orphaned `organization` KB scope, the polymorphic `resource_shares` table replacing the nullable `team_id` columns (028), RBAC role/permission seed (029), `team_members` RLS (030), dropping the never-written `oauth_revoked_tokens` table — token revocation is Redis-only via the single deployment-wide store (031, #767), `user_audit_log` `success`/`session_id` columns for the SSO JIT audit trail (032, ADR-015 PR 7), and the global-KB platform tier (033, #770 — `knowledge_items.organization_id` nullable with `(scope='global') ⟺ (organization_id IS NULL)` CHECK; the single FOR ALL RLS policy replaced by four per-command policies granting every tenant read access to global rows while confining global writes to single-tenant sentinel sessions or the audited maintenance path)). Migrations continue through 035 (durable append-only `operator_access_audit` for platform-operator access to tenant data) and 036 (`operator_access_grants` — break-glass grants over Cloud tenant case content; the justification columns are pinned by triggers and DELETE is rejected, plus a `BEFORE TRUNCATE` statement trigger and the `grant_id` index on 035's table), 037 (`case_messages.author_id` — per-turn authorship capture; nullable, deliberately not an FK so attribution outlives the account, ADR-013 D4/ADR-011 D5), and 038 (`sso_org_mappings` — IdP organization → FaultMaven organization, so a multi-tenant SSO login lands in its own tenant; deliberately **not** RLS-tenanted because the callback that reads it is unauthenticated and no tenant is bound yet, #869), and 039 (`oauth_authorization_codes.organization_id` — the OAuth-PKCE authorize leg captures the request's tenant so the *unauthenticated* token exchange has something to mint from; nullable, no FK, not RLS-tenanted for 038's reason, #872). Current head: `d2e3f4a5b6c7`. See `docs/architecture/data-and-storage/schemas/case-schema.md` for the full migration table.

## Key Patterns

### Investigation Framework (Milestone-Based)

The investigation engine uses a **data-driven, milestone-based** approach:

**State Lifecycle:** INQUIRY → INVESTIGATING → RESOLVED/CLOSED

**Investigation Stages (within INVESTIGATING):**
1. **SYMPTOM_VERIFICATION** - Verify symptoms, assess scope, establish timeline
2. **HYPOTHESIS_FORMULATION** - Generate likely theories based on evidence
3. **HYPOTHESIS_VALIDATION** - Test hypotheses with evidence linking
4. **SOLUTION** - Propose and verify fixes

**Key Characteristics:**
- **Opportunistic completion** - Multiple milestones can complete in a single turn
- **Data-driven transitions** - State changes when evidence thresholds are met
- **Hypothesis lifecycle** - CAPTURED → ACTIVE → VALIDATED/REFUTED/RETIRED
- **Confidence decay** - Stagnant hypotheses decay via `0.85^iterations` formula
- **Anchoring detection** - Prevents fixation on weak theories

**Progress Indicators (3)** — non-stage-driving; inform focus and analytics within DIAGNOSIS:
`symptom_verified`, `cause_state`, `solution_proposed` — `symptom_verified` is LLM-set and `solution_proposed` programmatic; `cause_state` is an engine-derived enum (`UNKNOWN | CANDIDATES | IDENTIFIED`) that replaced the old `root_cause_identified` boolean, recomputed each turn from the LLM's grounded cause signal (never path-stripped).

**Gate Milestones (4)** — Drive stage transitions when LLM detects user compliance:
`mitigation_accepted`, `mitigation_verified`, `solution_accepted`, `solution_verified`

Implemented in `core/investigation/milestone_engine.py` with hypothesis management in `hypothesis_manager.py` and progress monitoring in `progress_monitor.py`.

### Dependency Injection

- DI Container in `faultmaven/container/` with centralized implementation in `_container_impl.py`
- Service locator pattern with providers (infrastructure, services, tools)
- Composition Root pattern in `main.py` lifespan
- Singleton container with lazy initialization and interface-based dependency resolution

### Async Throughout

- FastAPI async endpoints
- Async database drivers (aiosqlite, asyncpg)
- Concurrent LLM calls via `asyncio.gather()`

## API Endpoints

**Base URL:** `http://localhost:8090/api/v1`

| Module | Endpoint | Description |
|--------|----------|-------------|
| Cases | `GET/POST /cases` | Case management CRUD |
| Cases | `GET /cases/{id}` | Get case details |
| Knowledge | `GET/POST /knowledge/documents` | Knowledge base CRUD |
| Knowledge | `POST /knowledge/search` | Semantic search |
| Knowledge | `POST /knowledge/convert` | Convert document to runbook drafts |
| Knowledge | `POST /knowledge/runbooks/create` | Create runbook manually from template |
| Knowledge | `GET /knowledge/drafts` | List all draft runbooks |
| Knowledge | `POST /knowledge/scan` | Manual draft reconciliation (auto-scan removed; ingestion is now owned by startup bootstrap) |
| Knowledge | `GET /knowledge/conversions` | List conversion jobs |
| Knowledge | `GET /knowledge/conversions/by-case/{case_id}` | Get conversion for a case |
| Knowledge | `PUT/POST/DELETE .../drafts/{id}` | Draft management (edit, verify, delete) |
| Auth | `POST /auth/register` | User registration |
| Auth | `POST /auth/login` | User login |
| Auth | `POST /auth/refresh` | Token refresh |
| OAuth | `GET /auth/oauth/authorize` | OAuth authorization |
| OAuth | `POST /auth/oauth/token` | OAuth token exchange |
| Evidence | `POST /evidence/upload` | File upload |
| Reports | `GET/POST /reports` | Terminal summaries (auto-generated) |
| Teams | `GET /teams` | List the caller's teams (read-only; names for share badges + share-to-team picker). Team *management* is the Cloud-composed admin module. |
| Sessions | `GET /sessions` | Session management |
| Admin | `GET /admin/users` | List users (admin only) |
| Admin | `GET /admin/users/{id}` | User details (admin only) |
| Admin | `POST /admin/users/{id}/roles` | Assign role (admin only) |
| Admin | `GET /admin/llm/config` | LLM provider status and fallback chain |
| Admin | `POST /admin/llm/config/test` | Test provider connection |
| Admin | `GET /admin/config/status` | Environment configuration status |
| Admin | `GET /admin/cases` | Cross-tenant case list (all users/orgs) — platform-admin only, audited. Deployment-split (ADR-012 D9): standalone returns full summaries (`view: "full"`), cloud returns ambient metadata with no title/description (`view: "metadata"`); titles need break-glass. 403 under `TENANT_PROVIDER=multi` (RLS would make the list silently partial) |
| Admin | `GET /admin/cases/{id}` | Operator **content** read — title, description, state (ADR-012 D9). Standalone: standing access, audited not gated. Cloud: requires a live break-glass grant naming that case. Response envelope names how it was authorised (`access: "standing" \| "break_glass"`). Separate from `GET /cases/{id}`, which has no operator bypass |
| Admin | `GET /admin/cases/{id}/messages` | Operator **transcript** read — same gate, same audit action, same envelope |
| Admin | `POST /admin/grants` | Mint a break-glass grant over ONE case: reason (min 20 chars) + TTL (default 60 min, max 240). No extend path — needing longer means a new grant |
| Admin | `GET /admin/grants` | List grants (not scoped to the caller — who holds access is the governance question) |
| Admin | `POST /admin/grants/{id}/revoke` | End a grant early; idempotent, and any operator may revoke any grant |
| Admin | `GET /admin/audit/operator-access` | The durable operator access trail (append-only) |

**Health & Metrics Endpoints:**

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Overall health status |
| `GET /health/dependencies` | Dependency health check |
| `GET /health/sla` | SLA metrics |
| `GET /health/logging` | Logging system health |
| `GET /health/components/{name}` | Component-specific health |
| `GET /health/patterns` | Error pattern detection |
| `GET /readiness` | Kubernetes readiness probe |
| `GET /metrics/performance` | Performance metrics |
| `GET /metrics/realtime` | Real-time metrics |
| `GET /metrics/alerts` | Alert status |
| `GET /metrics/optimization` | System optimization metrics |
| `GET /v1/meta/capabilities` | Backend capabilities for extension |

**Debug Endpoints (development only):**

| Endpoint | Description |
|----------|-------------|
| `GET /debug/routes` | List all registered routes |
| `GET /debug/health` | Minimal debug health |
| `GET /debug/config` | Configuration summary |
| `GET /debug/llm-providers` | LLM provider status |

**Documentation:** http://localhost:8090/docs

## Important Files

| File | Purpose |
|------|---------|
| `faultmaven/main.py` | FastAPI application entry point |
| `faultmaven/config/settings.py` | Pydantic settings (unified config) |
| `faultmaven/container/` | Dependency injection setup |
| `faultmaven/bootstrap/startup.py` | Application bootstrap |
| `faultmaven/modules/auth/contracts.py` | Auth DTOs and interfaces |
| `faultmaven/modules/case/contracts.py` | Case DTOs and interfaces |
| `faultmaven/modules/knowledge/contracts.py` | Knowledge DTOs and interfaces |
| `faultmaven/modules/knowledge/domain/services/conversion_service.py` | Document-to-runbook conversion pipeline |
| `faultmaven/modules/knowledge/api/conversion_routes.py` | Conversion API endpoints (feature-flagged) |
| `.env.example` | Configuration template |
| `pyproject.toml` | Dependencies, tool config, and `[project.scripts]` (the `fm-*` operator entrypoints) |
| `faultmaven/cli/` | Operator console entrypoint modules targeted by `[project.scripts]` |
| `faultmaven/infrastructure/persistence/models.py` | SQLAlchemy ORM models (all 31 tables) |
| `faultmaven/config/llm_config_overrides.py` | Config override application + hot-reload (cloud mode only) |
| `faultmaven/api/routes/admin_config.py` | Admin endpoints: LLM config, env status, features, connection test |
| `.importlinter` | Architecture contracts (13 rules) |
| `pytest.ini` | Test configuration |
| `alembic/` | Database migration (single clean baseline) |
| `faultmaven/_container_impl.py` | Centralized DI container implementation |
| `scripts/generate_er_diagram.py` | Generate ER diagram from SQLAlchemy models |

## Common Tasks

### Adding a New API Endpoint

1. Add route in appropriate module's `api/routes.py`
2. Add business logic in module's `domain/services/`
3. Add tests in `tests/unit/modules/` and `tests/integration/`
4. Run `lint-imports` to verify architecture

### Adding a New LLM Provider

1. Implement provider in `infrastructure/llm/providers/`
2. Inherit from `BaseLLMProvider` in `base.py`
3. Register in `infrastructure/llm/providers/registry.py`
4. Add config in `config/settings.py`
5. Document in `.env.example`
6. Add tests in `tests/unit/infrastructure/`

### Modifying Database Schema

1. Update SQLAlchemy models in `infrastructure/persistence/models/`
2. Create migration: `alembic revision --autogenerate -m "description"`
3. Review generated migration in `alembic/versions/`
4. Apply: `alembic upgrade head`

### Adding a New Module

**Vertical Module (owns data):**
1. Create `modules/newmodule/` with `contracts.py`, `api/`, `domain/`, `infrastructure/`
2. Define interfaces and DTOs in `contracts.py`
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
- **JWT Auth** - Stateless session management (HS256 local, RS256 OAuth)
- **OAuth 2.0 with PKCE** - Browser extension integration
- **RBAC** - Role-based access control
- **Token Revocation** - JTI-based revocation tracking
- **Secret Detection** - Pre-commit hooks prevent credential commits
- **CORS** - Configurable origins for browser extension
- **Rate Limiting** - Per-IP and per-user limits

## Documentation

```
docs/
├── architecture/           # System design, ADRs
│   ├── api-and-integration/    # API mapping, integration specs
│   ├── case-and-session/       # Case lifecycle, session management
│   ├── core-architecture/      # Module design, DI, service patterns
│   ├── data-and-storage/       # Database schemas, vector storage
│   ├── data-processing/        # Data classification, preprocessing
│   ├── investigation-engine/   # Milestone framework, hypothesis management, prompts
│   ├── knowledge-and-ai/       # RAG, vector operations
│   ├── security/               # IAM design, PII handling
│   ├── specifications/         # Formal specs (sessions, config, errors)
│   ├── decisions/              # Architecture Decision Records (ADRs)
│   └── diagrams/               # System diagrams (Mermaid)
├── getting-started/        # Installation, quickstart, user guide
├── guides/                 # How-to guides (config, migrations, KB)
├── development/            # Dev standards, testing, datetime handling
├── operations/             # Runbooks, monitoring, logging policies
└── archive/                # Historical implementation notes
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

# Check debug endpoint
curl http://localhost:8090/debug/llm-providers

# Check logs for API errors
./faultmaven.sh logs api
```

### JWT/Auth Issues
```bash
# Verify auth mode
echo $AUTH_MODE

# For OAuth mode, ensure RSA keys exist
python scripts/generate_oauth_keys.py

# Check token validation
curl -H "Authorization: Bearer $TOKEN" http://localhost:8090/api/v1/auth/me
```

## Version Info

- **Current Version:** 1.0.0
- **Python Support:** 3.11, 3.12, 3.13
- **License:** FSL-1.1-ALv2 (source-available; converts to Apache-2.0 two years after each release)
- **Min Python:** 3.11
