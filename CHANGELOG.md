# Changelog

All notable changes to FaultMaven will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Standalone first-run requires only ONE setting**: an LLM provider's API key. The JWT signing secret is now auto-generated and persisted (`data/.jwt_secret`) in local auth mode, so `JWT_SECRET_KEY` no longer has to be set (and the shared `dev-secret-change-me` placeholder is gone). `.env.example` was rewritten to a minimal REQUIRED block plus every other variable shown as a commented default that mirrors `settings.py`.
- **`.env.example` ↔ `settings.py` sync guard**: `scripts/check_env_example_sync.py` fails (pre-commit + CI) if a documented default in `.env.example` drifts from the `settings.py` default, so the two stay identical and defaults can be re-defined in one place.
- **Local deployment runs pre-built images by default**: `./faultmaven.sh start` now pulls `ghcr.io/faultmaven/faultmaven` and `ghcr.io/faultmaven/faultmaven-dashboard` from GHCR instead of building the API from source (no ~5GB local build, no Docker Hub pull-rate limits). New flags: `--build` (build the API from this repo), `--build-dashboard` (build the Dashboard from `../faultmaven-dashboard`), `--pull` (refresh images). Image tags are pinnable via `FM_IMAGE_TAG` / `FM_DASHBOARD_IMAGE_TAG` in `.env`; a mutable `:latest` is auto-refreshed on each start. Build-from-source is layered via `docker-compose.build.yml` / `docker-compose.dashboard-build.yml`.
- **Version-controlled git hook installer**: `.githooks/pre-commit` (black auto-formats staged Python, pinned to `black==26.3.1`) + `scripts/install-git-hooks.sh` (wires `core.hooksPath`; refuses to override an installed pre-commit framework unless `--force`, so secret-scanning hooks aren't silently disabled) + `scripts/black-pinned-version.sh`.

### Changed

- **Two new rate limit types, and `limit_type` in logs now takes five values**: `per_session_read` and `per_session_read_hourly` join `global`, `per_session` and `per_session_hourly` across every preset (`_load_from_settings`, `_load_from_environment`, development, production) and the `ProtectionSettings` default. **An alert matching `limit_type: per_session` exactly no longer sees read refusals** — match the `per_session` prefix, which covers all four session limits, or enumerate them. Both read keys must be configured together: the middleware applies the split only when it finds both, and otherwise meters reads against the write buckets and logs why, because the limiter allows anything it holds no configuration for and a half-configured split would leave reads unmetered rather than merely tighter. `per_session_read_hourly` is also the largest window key in the system and there is one per *session*, so it — not `global` — is now the term that scales Redis memory with concurrency; see [rate-limiting-sliding-window.md](docs/architecture/security/rate-limiting-sliding-window.md#memory-bound).
- **Redefined the default LLM model per provider to one coherent, valid set** (performance-weighted: quality over price, all tool-calling + large-context) across `settings.py`, `registry.py`, and `.env.example` (Anthropic `claude-sonnet-4-6`, OpenAI `gpt-4.1-mini` (non-reasoning: no hidden-reasoning surcharge or output-budget starvation, which FaultMaven caps only for Gemini 3.x), Gemini `gemini-3.5-flash`, Fireworks `accounts/fireworks/models/deepseek-v3`, Groq `llama-3.3-70b-versatile`, Cohere `command-r-plus`, OpenRouter `anthropic/claude-sonnet-4-6`; HuggingFace `Mistral-Large-Instruct-2411` kept but not recommended — no tool calling). Previously the three sources disagreed and some IDs were invalid/deprecated. Added a `groq_model` field for parity, retired the orphaned `groq_chat_model`/`cohere_chat_model` fields, and fixed the registry + dashboard-override map to resolve Groq/Cohere via the canonical model field.
- **Standalone config is a single shared `.env`**: the containerized API reads `.env` via a read-only bind mount at `/app/.env` (not compose `env_file:`), so it is parsed by the same loader as the process runner (`scripts/faultmaven-dev.sh`); `./faultmaven.sh restart` re-reads edits, and a missing `.env` fails loudly instead of Docker creating a directory.
- **Clearer `faultmaven.sh start` output**: confirms `.env` before naming the LLM provider (reported from `CHAT_PROVIDER`), streams live `docker compose` pull/build progress (previously captured/silent), shows an elapsed/budget health-wait with per-service state, and recognizes an already-running stack instead of reporting a false port conflict.

- **KB Vector Store**: Introduced `KnowledgeVectorStore` for KB retrieval — uses collection names as-is from `KBConfig`. Replaces `CaseVectorStore` (which prepends `case_`) for all Q&A tools. Global KB collection standardized to `global_kb`.

- Removed `IP_ADDRESS` from Presidio default `entities_to_protect` — public IPs are investigation evidence, not PII. Private IPs (RFC1918) remain redacted by the regex layer.

### Fixed

- **Browsing a case was rate limited as if it were AI work** (fm#994): every request carrying an `X-Session-ID` shared a single `per_session` bucket — 10 requests/minute on the canonical settings path, 5/minute in production — so a Copilot or Dashboard case view — which issues well over ten GETs for details, files, messages and history — returned `Error loading case: Rate limit exceeded. Please try again in 44 seconds.` A session now holds **two independent pairs** of per-session buckets and each request is charged to exactly one: cheap reads to `per_session_read` / `per_session_read_hourly` (production 120/min, 1200/hour), and writes to `per_session` / `per_session_hourly` (production 10/min, 50/hour), which is the quota that protects LLM compute. A burst of navigation can no longer refuse the next `POST .../turns`. Classification is by cost rather than by HTTP verb: `GET /cases/{id}/report-recommendations`, `GET /reports/recommendations/{id}` and `GET /knowledge/documents/{id}/snippet` each run a query embedding and a vector similarity search per call, so they are metered as writes despite being GETs.

- **Docker image could publish from a failing commit**: `publish-docker.yml` triggered on every push to `main` independently of CI, so `:latest` could ship from a commit whose tests or Dockerfile build had failed. It now publishes only after the **CI/CD Pipeline** workflow succeeds on `main` (via `workflow_run` + a `conclusion == 'success'` gate), matching the dashboard's publish flow; version tags and manual dispatch still publish directly.
- **`faultmaven.sh` corrupted on JSON `.env` values**: the script `source`d `.env` as shell, so a valid value like `LLM_PROVIDER_TIMEOUT_OVERRIDES={"fireworks": 180}` aborted startup (`180}: command not found`). It now reads keys with a safe parser (`read_env_var`) instead of sourcing.
- **`faultmaven.sh` reported the wrong LLM provider**: it printed the first credential found in arbitrary order rather than the configured `CHAT_PROVIDER`; it now reports `CHAT_PROVIDER` and warns if that provider's credential is missing.
- **KB Collection Mismatch**: Fixed silent empty results from KB Q&A tools caused by ingestion writing to `faultmaven_kb` while retrieval searched `case_global_kb` (CaseVectorStore prefix). Also fixes case evidence double-prefix bug (`case_case_{id}`).
- **Opik Ghost Spans**: Removed duplicate `@opik.track` decorator from `LLMRouter.generate()` — it created an empty outer span on every LLM call since `generate()` is a thin delegate to `route()` which already has the decorator
- **Router Timeout Enforcement**: Changed `timeout=None` to `timeout=self.request_timeout` in `LLMRouter.route()` — the configured timeout (default 30s) was never enforced at the router level, allowing unbounded latency when the fallback chain tried multiple slow providers
- **Test Mock Setup**: Fixed 3 tests in `test_router.py` where `aiohttp` response mocks were missing `response.text` setup, causing `AsyncMock` objects to leak into error messages recorded by Opik tracing

### Added (Existing)

- Session-level tools documentation in `docs/tools/`
- MCP (Model Context Protocol) integration guide
- Comprehensive tool catalog and developer guide
- Email uniqueness constraint migration (008_email_uniqueness_constraint)
- Database migration scripts for data integrity:
  - `scripts/check_duplicate_emails.py` - Check for duplicate email addresses
  - `scripts/resolve_duplicate_emails.py` - Resolve duplicate emails (auto/interactive modes)
  - `scripts/backfill_closed_at_timestamps.py` - Backfill missing closed_at timestamps

### Changed (Existing)

- Documentation reorganization: cleaner folder structure
- Renamed `guides/` to `how-to/` for clarity
- Moved historical docs to `archive/`
- Consolidated specifications into `architecture/specifications/`

### Fixed (Existing)

- **CRITICAL**: Storage infrastructure gaps causing data loss (Commits 52bfb854, b434152a, ecaafed7)
  - **Message Persistence**: Fixed PostgreSQL repositories missing `_upsert_messages()` call
    - All conversation messages now persist correctly across application restarts
    - Messages stored in normalized `case_messages` table
  - **Missing Case Fields**: Added 9 critical fields to PostgreSQL repository
    - `organization_id` - Multi-tenancy security restored
    - `description` - Problem descriptions now saved
    - `closure_reason`, `investigation_strategy` - Case metadata preserved
    - `current_turn`, `turns_without_progress` - Turn tracking restored
    - `last_activity_at`, `resolved_at`, `closed_at` - Timestamp tracking complete
  - **Missing Repository Methods**: Added 6 methods to infrastructure PostgreSQL repository
    - Report CRUD operations: `add_report()`, `get_report()`, `get_reports()`, `update_report()`, `delete_report()`
    - Rate limiting: `count_user_cases_on_date()`
  - PostgreSQL repositories now have full parity with SQLite repository
  - All tests passing: 29 unit tests, 8 SQLite integration tests, 6 Alembic migration tests
- **TEST**: Alembic migration test expectations (Commit 67d615a5)
  - Updated for migration 013_verification_suggestions (knowledge_suggestions table)
  - Fixed database path handling to use absolute paths
- **CRITICAL**: Email uniqueness not enforced in database (Security Bug)
  - Added explicit UNIQUE constraint on users.email (case-insensitive)
  - InMemoryUserRepository now stores deep copies to prevent mutable reference bugs
  - Prevents authentication bypass via duplicate email accounts
  - Migration: `20250109_1000_008_add_email_uniqueness_constraint.py`
- **CRITICAL**: Case cleanup using wrong timestamp (Data Integrity Bug)
  - DatabaseCaseRepository.cleanup_expired() now uses closed_at from metadata
  - Previously used updated_at, causing premature deletion of recently-updated closed cases
  - Added backfill script for missing closed_at timestamps
  - Prevents database bloat and incorrect case aging

### Removed

- Obsolete `_temp/` folder from architecture docs
- Redundant planning documents

### Security

- Email uniqueness enforcement prevents duplicate account creation (authentication bypass risk)
- Added comprehensive unit tests for email uniqueness and immutability

## [2.0.0] - 2025-10-04

### Added
- Infrastructure improvements (see `docs/archive/release-notes/`)
- Enhanced investigation phases and OODA integration
- Evidence-centric troubleshooting framework

### Changed
- Major architecture refactoring
- Clean dependency injection system
- Interface-based design patterns

## [1.0.0] - Initial Release

For historical release notes, see `docs/archive/release-notes/`.

---

**Note**: This changelog started with v2.0. For earlier releases, see archived documentation.
