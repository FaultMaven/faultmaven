# CLAUDE.md - AI Assistant Guide for FaultMaven

Context for coding agents working in this repository. This file holds what
most sessions need; detail lives where it loads on demand — `.claude/rules/*.md`
(loaded when a matching file is touched) and `docs/`.

## Project Overview

FaultMaven is an **AI-powered troubleshooting copilot**. It correlates the logs, metrics, and configs you share with runbooks, documentation, and past fixes to deliver contextual AI-driven incident investigation. It works a problem the way a seasoned engineer does — goal-driven, methodical, evidence-based, self-learning — and never forgets what it learns.

**Deployment positioning:** run it yourself, or let us run it for you — same engine, both first-class. Local-model and air-gapped claims are gated on a verified end-to-end run. Canonical wording and rules: `.claude/skills/brand-messaging/SKILL.md` §1 "Deployment positioning".

**Key Value Propositions:** evidence-centric investigation (logs, metrics, configs, past solutions); knowledge flywheel (learns from resolved incidents); multi-LLM support (9 providers: Anthropic, OpenAI, Gemini, Fireworks, Groq, HuggingFace, Cohere, OpenRouter, local Ollama/vLLM); zero context-switching (browser extension integrates into existing tools).

| Component | Purpose |
|-----------|---------|
| FaultMaven API (this repo) | Backend investigation engine, knowledge base, AI orchestration |
| FaultMaven Dashboard | Web UI — the full product in a browser tab; works end to end on its own |
| FaultMaven Copilot | Browser extension for in-context troubleshooting |
| FaultMaven Slack Agent | Answers in the incident thread; while the beta runs, workspaces are connected by hand (fm#1457) |

Python ≥ 3.11 (`requires-python`; classifiers cover 3.11–3.13). License: FSL-1.1-ALv2 (`LICENSE`). Versions are not written here: the package version is `pyproject.toml` `version`, the contract version clients pin is `API_CONTRACT_VERSION` in `faultmaven/api/contract_version.py`.

## Tech Stack

| Layer | Technologies |
|-------|--------------|
| Framework | Python 3.11+, FastAPI, Uvicorn, AsyncIO, Pydantic v2 + pydantic-settings |
| LLM/AI | 9 providers behind one router (`faultmaven/infrastructure/llm/`) |
| Database | SQLAlchemy 2.0 async (aiosqlite, asyncpg), SQLite (standalone), PostgreSQL (cloud), Alembic |
| Vector DB | ChromaDB (PersistentClient in-process by default), BGE-M3 via sentence-transformers |
| Cache / sessions | Redis (cloud), FakeRedis in-process (standalone — full API parity, no server) |
| Auth | PyJWT, bcrypt, RBAC, OAuth 2.0 with PKCE |
| Observability | Opik (tracing), Prometheus (metrics), structlog (logging) |
| Security | Presidio (PII redaction), cryptography |
| Testing | pytest, pytest-asyncio, pytest-cov, factory-boy, locust |
| Code Quality | ruff (lint + import sorting via its `I` rules), black, mypy, import-linter |

Version floors: `pyproject.toml`. The pins CI installs: `requirements/dev.txt` (ruff, black and FastAPI versions matter for the gates below).

## Repository Layout

```text
faultmaven/
├── main.py                 # FastAPI entry point; composition root in the lifespan
├── api/                    # Shared middleware (middleware/), dependencies, exception handlers, admin routes (routes/)
├── modules/                # Feature modules — the primary code organisation
│   ├── auth/ case/ knowledge/                  # VERTICAL MODULES: own tables, contracts.py, infrastructure/
│   └── agent/ evidence/ preprocessing/ report/  # DOMAIN SERVICES: business logic only
├── core/
│   ├── investigation/      # milestone_engine.py (process_turn), hypothesis_manager.py, progress_monitor.py,
│   │                       # schemas.py, intent_resolver.py, turn_budget.py, prompts/{templates,context_builder}.py
│   ├── preprocessing/      # Tier 0/1 mechanical preprocessor
│   └── processing/         # Log analyzer, pattern learner
├── infrastructure/         # Shared adapters: llm/ (providers/, router.py, cache.py, truncation.py, pricing.py),
│                           # persistence/ (models.py = every ORM table), knowledge/ (ChromaDB), auth/, security/ (Presidio),
│                           # protection/, storage/ (local, S3, Azure), logging/, observability/, health/, jobs/, tasks/, caching/, shims/, concurrency/
├── bootstrap/              # Startup: startup.py, data_init.py (bootstrap admin), kb_init.py + kb_pack.py (KB pack ingestion)
├── cli/                    # Operator console entrypoints (fm-*, `[project.scripts]`)
├── config/                 # settings.py, presets.py, feature_flags.py, protection.py, investigation_capability.py, llm_config_overrides.py
├── container/              # DI: base.py, registry.py, providers/ — implementation in faultmaven/_container_impl.py
├── services/               # BaseService + request-scoped DI factory
├── models/                 # Shared interfaces, API schemas, domain models
└── utils/                  # schema_converter.py, token_estimation.py, …
alembic/                    # Migrations (see Database)
resources/knowledge/pack/   # Vendored KB pack: shipped runbooks + pre-built vectors (owned by faultmaven-kb-toolkit)
scripts/                    # Dev and maintenance scripts — NOT in the wheel, NOT in the image
tests/                      # unit/ (modules, api, infrastructure, services, cli, core) · integration/ (api, modules) · infrastructure/ · benchmarks/ · performance/ · health/ · load/ · installation/
docs/                       # docs/README.md maps the tree and says where a new document goes
.claude/                    # rules/ (path-scoped guidance) · skills/ · commands/ · manifest.json (module → type map)
```

Key files: `.env.example` (config template), `pyproject.toml`, `.importlinter`, `pytest.ini`, `docs/architecture/data-and-storage/er-diagram.md`.

## Architecture

Modular monolith: **Vertical Modules** own data, **Domain Services** hold business logic only. `.claude/manifest.json` is the module → type map.

| Type | Modules | Has | Rule |
|------|---------|-----|------|
| Vertical Module | `auth`, `case`, `knowledge` | own tables; `contracts.py` (interfaces such as `ICaseRepository` + DTOs); `infrastructure/` (repositories) | other modules import only from its `contracts.py` |
| Domain Service | `agent`, `evidence`, `preprocessing`, `report` | `api/` + `domain/` (+ `tools/` in agent) — NO `contracts.py`, NO `infrastructure/` | reaches data through Case/Auth contracts, injected via DI |

```python
from faultmaven.modules.case.contracts import ICaseRepository, EvidenceArtifact   # CORRECT
from faultmaven.modules.auth.contracts import UserDTO, AuthTokenDTO               # CORRECT
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository  # WRONG: bypasses the contract
from faultmaven.modules.evidence.domain.validators import validate_evidence       # WRONG: another module's internals
```

- Boundaries are enforced by import-linter: `.importlinter` holds the contracts (15 today) and `lint-imports` prints the authoritative list.
- The `case` module owns evidence, reports and investigation sessions (`domain/owned_models/`); `evidence` and `report` re-export from Case contracts.
- Agent tools (`modules/agent/tools/`) include one `kb_qa` (every knowledge scope via metadata filter — there is no per-scope variant), `case_evidence_qa`, `document_qa_tool`, `search_file` / `deep_analysis` (query strategies over raw files), `read_file`, the `list_evidence*` and entity tools, `vectorize_file` and `web_search` (Tavily).
- Design docs: `docs/architecture/core-architecture/` (start at `module-organization-design.md`). The `architecture` skill applies when adding endpoints, services or modules.

## Commands

```bash
cp .env.example .env                 # set CHAT_PROVIDER + its API key; both run modes read this one file
./faultmaven.sh start                # Docker: API :8090 + Dashboard :3333 from pre-built GHCR images
./faultmaven.sh start --build        # build the API from source instead (--build-dashboard for ../faultmaven-dashboard)
./faultmaven.sh restart | stop | logs [service] | health
pip install -e ".[dev]" && ./scripts/faultmaven-dev.sh start    # local process, no Docker (stop | health | logs | test)

python scripts/tests.py --unit       # also --integration, --ci, --ci-full, --ci-nightly, --coverage
pytest tests/unit/ ; pytest -m "security" ; pytest -k "test_case"

ruff check faultmaven/ tests/        # the lint gate, byte for byte
black faultmaven/ tests/             # CI runs `black --check` on the same paths
lint-imports                         # architecture boundaries
python scripts/generate_api_docs.py --check   # API-reference drift (CI gate)
```

- Ports: API 8090 (`/docs` = Swagger UI), Dashboard 3333. Redis (FakeRedis) and ChromaDB run **in-process** inside the API container. Images are `ghcr.io/faultmaven/faultmaven{,-dashboard}`, pinnable via `FM_IMAGE_TAG` / `FM_DASHBOARD_IMAGE_TAG`; `start --pull` refreshes them.
- Compose mounts `.env` read-only at `/app/.env` (not `env_file:`), so both run modes parse it identically; `./faultmaven.sh restart` recreates containers (`up -d --force-recreate`) so `.env` edits and refreshed images apply. `docker-compose.yml` `environment:` entries override the file (pydantic env-var > `.env`).
- First startup creates `data/`, runs migrations, creates the bootstrap admin (`admin@local.faultmaven`, holds the operator roles) and ingests the KB pack (`resources/knowledge/pack` or `KB_PACK_DIR`) with **no embedding model** — pre-chunked, pre-embedded, content-hash idempotent. Under `TENANT_PROVIDER=multi` the pack is seeded by the audited `kb_seed` job instead. Design: `docs/architecture/knowledge-and-ai/kb-ingestion-architecture.md`. Local login: `POST /api/v1/auth/dev-login {"username": "admin"}`.
- `scripts/` never ships: anything an operator runs in a pod is an `fm-*` entrypoint in `faultmaven/cli/` — `docs/operations/operator-cli.md`. Dev scripts: `docs/development/script-usage-guide.md`. Local problems: `docs/development/local-troubleshooting.md`.

## Testing

- `asyncio_mode = auto`: plain `async def` tests run. `--strict-markers` is on — mark categories with `@pytest.mark.unit` / `integration` / `security` / `llm` / … from the list in `pytest.ini`.
- Fixtures come from `tests/conftest.py` (`reset_container` gives a fresh DI container). Mock interfaces, not concrete providers.
- Some tests read documentation, and `.github/scripts/classify_docs_only.py` treats a document a test names as executable: this file (`test_claude_md_pins_no_migration_head.py`: no alembic revision id here; `test_no_unauthenticated_operations.py`: its `ENVIRONMENT=` / `ENABLE_DEBUG_ENDPOINTS` lines name only settable values) and `.claude/rules/llm-providers.md` (`test_claude_md_pins_reasoning_intent_call_sites.py`, `test_groq_model_defaults.py`). Move pinned text and its test together.
- Standards, patterns and the architecture-testing guide: `docs/development/testing/`.

## Code Quality

- `ruff check faultmaven/ tests/` and `black --check faultmaven/ tests/` are the CI `code-quality` gate. **Never add `--select`** to ruff — it REPLACES `[tool.ruff.lint].select` instead of narrowing it. Import sorting is ruff's `I` rules (ruff replaced isort in #179); do not run `isort`, which is not a gate and rewrites files across the repo. `ruff check .` / `black .` also cover `scripts/`, `alembic/` and `docs/`, which CI deliberately does not lint.
- mypy is **not** a gate: nothing in CI runs it, and `ignore_errors = true` in `pyproject.toml` means it reports nothing unless paths are passed explicitly.
- `docs/reference/api/openapi.json` and `docs/reference/api/README.md` are **generated** by `scripts/generate_api_docs.py`. Never hand-edit them: a change to any route, schema or docstring ships with the regenerated artifact in the same PR, produced with the lockfile installed (`pip install -r requirements/dev.txt`) — the `api-contract-drift` job regenerates and diffs. Details and contract versioning: `.claude/rules/api-contract.md`, `docs/development/api-contract-changes.md`.
- Pre-commit: `pre-commit install` (detect-secrets, check-api-keys, check-hardcoded-rsa-keys, JSON/YAML checks), or the black-only hook `./scripts/install-git-hooks.sh` — `docs/CONTRIBUTING.md` §"Pre-commit hooks".

## Configuration

Pydantic settings (`faultmaven/config/settings.py`) read `.env`; environment variables beat the file. Reference: `docs/development/environment-variables.md`.

| Category | Variables |
|----------|-----------|
| LLM | `CHAT_PROVIDER` (the anchor; unset roles follow it) + `*_API_KEY`; role overrides `CODE_PROVIDER`, `MULTIMODAL_PROVIDER`, `SYNTHESIS_PROVIDER`, `CLASSIFIER_PROVIDER`, `KNOWLEDGE_PROVIDER`, `DA_PROVIDER` — `.claude/rules/llm-providers.md` |
| Knowledge | `KB_PREFETCH_ENABLED` (default `true`; governs the KB **push** — the deterministic pre-fetch into the prompt — only; the `kb_qa` **pull** stays registered either way; gated at both ends, and `GET /admin/config/status` reports `kb_prefetch`), `KB_PACK_DIR`, `ENABLE_WEB_SEARCH` + `TAVILY_API_KEY` |
| Storage | `DATABASE_URL` / `DB_BACKEND` (SQLite default, PostgreSQL), `REDIS_URL` / `REDIS_HOST` (FakeRedis default), `VECTOR_STORAGE_TYPE` / `CHROMADB_URL` (PersistentClient default; external server via the URL) |
| Auth | `AUTH_MODE` (`local` = HS256 + `JWT_SECRET_KEY`; `oauth` = RS256 key pair), `JWT_ACCESS_TOKEN_EXPIRY_MINUTES` / `JWT_REFRESH_TOKEN_EXPIRY_DAYS` (both modes; out of range fails startup), `OAUTH_REDIRECT_URI_PATTERNS`, `OAUTH_FIRST_PARTY_CLIENTS` + `OAUTH_FIRST_PARTY_REDIRECT_PATTERNS` (nothing skips consent until both are set) — `.claude/rules/data-model.md`, `docs/architecture/security/iam-design.md` |
| Tenancy | `TENANT_PROVIDER` (`multi` = PostgreSQL RLS), `TENANT_DAILY_TURN_CAP` (accounts in no organization only), `SSO_JIT_PERSONAL_TENANT_ENABLED`, `TEAM_INVITATION_TTL_DAYS` — `.claude/rules/data-model.md` |
| Protection | `PROTECTION_PROFILE` (`hardened` default / `development`) selects the rate-limit preset in `faultmaven/config/protection.py`. There is **no** rate-limit env knob and no variable switches limiting off (`SKIP_SERVICE_CHECKS` no longer does, fm#990); `ENVIRONMENT` can only *veto* a development profile, never select one, so no deployment that configures nothing arms the `X-Dev-Bypass` / `X-Test-Bypass` headers (fm#985). `GET /admin/config/status` reports `features.request_protection_hardened`. `docs/operations/security/client-protection.md` |
| Limits / CORS | `MAX_UPLOAD_SIZE_MB`, `CORS_ALLOW_ORIGINS`, `CORS_ALLOW_CREDENTIALS` |

Standalone (default): SQLite, FakeRedis, local filesystem, `.env` as the sole source of settings. Cloud: PostgreSQL, Redis, S3/Azure blob storage, Presidio, Opik, and `config_overrides` (dashboard-managed settings hot-reloaded via `faultmaven/config/llm_config_overrides.py`).

## Database

```bash
alembic revision --autogenerate -m "description"   # after editing faultmaven/infrastructure/persistence/models.py
alembic upgrade head ; alembic downgrade -1
alembic heads                                      # the only way to learn the current head
```

- **Never write an alembic revision id into this file.** A lane that parents a migration onto a revision read from prose parents onto a non-head (#1246). `tests/integration/test_alembic_migrations.py` pins `HEAD_REVISION` and the expected table set; a new migration moves both.
- While the chain is a single baseline (`001_enterprise_baseline`, ADR-017 — pre-user, no backward compatibility, existing deployments are wiped with `fm-wipe-deployment --wipe` and re-provisioned) there is no history to step through: `downgrade()` drops everything, and the migration's docstring carries the per-table reasoning.
- Every migration runs on **SQLite and PostgreSQL**: PostgreSQL-only DDL (RLS, triggers, partial indexes, `ON CONFLICT`) is dialect-guarded and column types stay SQLite-compatible. Conventions: `docs/guides/database-migrations.md`; `/migrate` command.
- ORM models: `faultmaven/infrastructure/persistence/models.py` (every table). ER diagram: `docs/architecture/data-and-storage/er-diagram.md` (`python scripts/generate_er_diagram.py --update`); tables by domain: `docs/reference/database/README.md`; schema specs: `docs/architecture/data-and-storage/schemas/`.
- Tenancy (ADR-017): the **enterprise isolates** (RLS on `enterprise_id`, NOT NULL on every tenant-scoped row), the **organization bills** (nullable `organization_id`, never a visibility predicate), the **team shares** (formed by consent — accepting an invitation is the only call that writes `team_members`). Invariants, sharing and the turn-usage ledger: `.claude/rules/data-model.md`.

## Investigation Engine

- Case lifecycle: `INQUIRY → INVESTIGATING → RESOLVED | CLOSED` (`CaseState` in `faultmaven/modules/case/domain/models.py` — the authoritative enum source). Within INVESTIGATING the stages (`InvestigationStage`: DIAGNOSIS default, MITIGATION optional insert, TREATMENT) are **derived labels** re-computed from the gate milestones; they never drive prompt dispatch.
- Milestones are opportunistic — several can complete in one turn. Gate milestones (`mitigation_accepted`, `mitigation_verified`, `solution_accepted`, `solution_verified`) fire on user compliance; progress indicators (`symptom_verified` LLM-set, `solution_proposed` programmatic, `cause_state` ∈ `UNKNOWN | CANDIDATES | IDENTIFIED` engine-derived and never path-stripped) inform focus only.
- Hypotheses: `CAPTURED → ACTIVE → VALIDATED | REFUTED | INCONCLUSIVE | RETIRED` (`HypothesisState`); stagnant likelihood decays by `0.85^iterations_without_progress`; anchoring detection prevents fixation on weak theories.
- Design docs are canonical and start at `docs/architecture/investigation-engine/README.md`; the `investigation-framework` skill applies to `modules/agent/` and `core/investigation/`. LLM-facing rules (structured output, stop reasons, turn budget, reasoning intent): `.claude/rules/llm-providers.md`.
- DI: service-locator container (`faultmaven/container/`, implementation `faultmaven/_container_impl.py`), composed once in the `main.py` lifespan, resolved by interface. Async throughout: FastAPI endpoints, async drivers, concurrent LLM calls via `asyncio.gather()`.

## Security Rules

- PII redaction via Presidio (cloud); JWT auth (HS256 local / RS256 oauth) with JTI-based revocation; OAuth 2.0 + PKCE for the extension; RBAC; CORS; rate limiting per IP and per user (presets — see Configuration). Design: `docs/architecture/security/`.
- Secrets never enter the repo: pre-commit `detect-secrets`, `check-api-keys`, `check-hardcoded-rsa-keys`.
- Debug router (`/debug/routes|health|config|llm-providers`): mounted when `ENVIRONMENT=development` or, in any environment, when `ENABLE_DEBUG_ENDPOINTS=true`; the `Environment` enum admits only development/staging/production. All four routes require the **platform administrator** role (#1474): anonymous → 401, authenticated non-operator → 403. Full surface semantics: `.claude/rules/api-contract.md`.
- Which operations require auth is derived from the dependency graph: a route gains `security` in the OpenAPI spec because `require_authentication` declares the `HTTPBearer` scheme. `tests/integration/api/test_openapi_documents_auth.py` fails on an auth dependency that reads the header directly.
- Operator reads of case content go through the audited `/admin/cases` surface (break-glass under cloud, ADR-012 D9): `docs/architecture/security/break-glass-content-access.md`.

## Common Tasks

- **New endpoint**: route in the module's `api/routes.py`, logic in `domain/services/`, tests in `tests/unit/modules/` and `tests/integration/`, regenerate the API reference, run `lint-imports`. `/new-endpoint` command; `architecture` skill.
- **New module**: vertical = `contracts.py` + `api/` + `domain/` + `infrastructure/` (repository under `infrastructure/persistence/`), routes registered in `main.py`; domain service = `api/` + `domain/` only, models from Case contracts, repository via DI. Either way add a layer-boundary contract to `.importlinter` and the entry to `.claude/manifest.json`.
- **New LLM provider**: the checklist in `.claude/rules/llm-providers.md` §"Adding a New LLM Provider"; long-form guide `docs/guides/adding-llm-providers.md`.
- **Modifying Database Schema**: 1. edit `faultmaven/infrastructure/persistence/models.py`; 2. `alembic revision --autogenerate -m "description"`; 3. review the file in `alembic/versions/` (both directions populated, PostgreSQL-only DDL dialect-guarded); 4. `alembic upgrade head`; 5. move `HEAD_REVISION` and the table set in `tests/integration/test_alembic_migrations.py`.

## Documentation

`docs/README.md` maps the tree (Diátaxis: getting-started, guides, architecture, reference, development, operations) and says where a new document goes. Never create files in the repository root. The canonical architecture documents are listed in `docs/architecture/architecture-overview.md`.

| Detail | Location | Loads |
|--------|----------|-------|
| LLM providers, structured output, stop reasons, turn budget, reasoning intent, provider checklist | `.claude/rules/llm-providers.md` | on touching `infrastructure/llm/`, `core/investigation/`, `modules/agent/`, LLM config and tests |
| Auth modes, tenancy, teams, sharing, turn-usage ledger, auth/case module maps | `.claude/rules/data-model.md` | on touching `modules/auth/`, `modules/case/`, persistence, CLI, alembic |
| API surface semantics, generated reference, debug endpoints | `.claude/rules/api-contract.md` | on touching `api/`, `modules/*/api/`, `docs/reference/api/` |
| Operator entrypoints (`fm-*`) | `docs/operations/operator-cli.md` | |
| Local troubleshooting | `docs/development/local-troubleshooting.md` | |
| Investigation framework, RAG, ingestion, architecture, brand copy | `.claude/skills/*/SKILL.md` | by skill trigger |
