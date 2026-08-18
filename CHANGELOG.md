# Changelog

All notable changes to FaultMaven will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The tool-result truncation is no longer silent** (fm#1088): the investigation tool loop truncates every tool result to `MilestoneEngine.TOOL_RESULT_MAX_CHARS` (8000) before it re-enters the model's context, cutting it head-first and marking it `[truncated]`. Nothing recorded that this happened — no log line, no counter — so the clip rate was not merely unmeasured but **unmeasurable**: the only available estimate came from arithmetic across two unrelated log lines plus a hand-measured wrapper size, for one tool, on one run. The cut now emits a `tool_result_truncated` **WARNING** carrying the tool, the original length, the cap and the dropped characters, and three Prometheus metrics in the new `core/investigation/tool_loop_metrics.py`: `faultmaven_tool_result_relayed_total{tool}` (the denominator — every relayed result, counted after redaction and per-tool formatting, i.e. the string that actually enters the context), `faultmaven_tool_result_truncated_total{tool}` (the numerator), and `faultmaven_tool_result_chars{tool}` (the pre-cut size distribution, bucketed densely either side of 8000). Per **tool** because the cap is one global constant shared by tools that are not alike: `kb_qa` relays curated prose written to a prompt that asks for full procedure, while `search_file` already shapes `DEFAULT_CONTEXT_LINES` and its result format defensively around the same cap — two tools working around one unmeasured limit, neither knowing its own rate. The `tool` label is bounded to the names the call offered, because a tool name arrives on a model-supplied tool call and an inventive model would otherwise mint a Prometheus label per invention. **Observability only — nothing about what the engine relays changes, and the ceiling itself is deliberately not touched**; that decision is fm#1088's, to be made after a run with this instrumentation in place. The cost model that decision rests on is corrected here in two halves. fm#1088 argues the cap must stay low because a relayed result "enters the conversation history and is re-sent on every subsequent turn of that case" — it does not: `MessageRole` has no tool role, the only `"role": "tool"` site is the list local to `_tool_augmented_generate`, and a tool result cannot reach `case_messages`. That half is **intra-turn**, bounded by `MAX_TOOL_ITERATIONS` (4) and by `_bound_tool_loop_messages`. But "one turn" is true only of the tool *message*: the kb_qa wrapper tells the model to copy the answer into `agent_response`, which **is** persisted and replayed by `_build_graduated_history` for the last 3 turns verbatim before collapsing to summaries — so KB content does recur across turns, via the assistant message, over a bounded decaying window. **Neither reading was right, and the recurring half is not observable from these metrics** (it lives in `agent_response` length), so the ceiling must not be decided from `tool_result_chars` alone. Because fm#1086's kb_qa formatter trim now clips kb_qa *before* the loop's cap, that earlier trim feeds the same counters against the true pre-trim wrapped size — otherwise kb_qa, the tool the issue is about, would report the lowest clip rate in the system while still being clipped. See [tool-result-budget.md](docs/operations/monitoring/tool-result-budget.md) for the clip-rate query.
- **Standalone first-run requires only ONE setting**: an LLM provider's API key. The JWT signing secret is now auto-generated and persisted (`data/.jwt_secret`) in local auth mode, so `JWT_SECRET_KEY` no longer has to be set (and the shared `dev-secret-change-me` placeholder is gone). `.env.example` was rewritten to a minimal REQUIRED block plus every other variable shown as a commented default that mirrors `settings.py`.
- **`.env.example` ↔ `settings.py` sync guard**: `scripts/check_env_example_sync.py` fails (pre-commit + CI) if a documented default in `.env.example` drifts from the `settings.py` default, so the two stay identical and defaults can be re-defined in one place.
- **Local deployment runs pre-built images by default**: `./faultmaven.sh start` now pulls `ghcr.io/faultmaven/faultmaven` and `ghcr.io/faultmaven/faultmaven-dashboard` from GHCR instead of building the API from source (no ~5GB local build, no Docker Hub pull-rate limits). New flags: `--build` (build the API from this repo), `--build-dashboard` (build the Dashboard from `../faultmaven-dashboard`), `--pull` (refresh images). Image tags are pinnable via `FM_IMAGE_TAG` / `FM_DASHBOARD_IMAGE_TAG` in `.env`; a mutable `:latest` is auto-refreshed on each start. Build-from-source is layered via `docker-compose.build.yml` / `docker-compose.dashboard-build.yml`.
- **Version-controlled git hook installer**: `.githooks/pre-commit` (black auto-formats staged Python, pinned to `black==26.3.1`) + `scripts/install-git-hooks.sh` (wires `core.hooksPath`; refuses to override an installed pre-commit framework unless `--force`, so secret-scanning hooks aren't silently disabled) + `scripts/black-pinned-version.sh`.

### Changed

- **KB retrieval embeds the query once per search instead of once per keyword**: `hybrid_search()` embedded the query for vector recall and then re-embedded the *same* text for each of up to three keyword probes. The vector is loop-invariant — only the `where_document` `$contains` filter differs between probes — so each repeat was an identical local BGE-M3 call, measured at 1.2–2.3s apiece on CPU: roughly **4.9s of a ~30s investigation turn**, on every turn that consults the knowledge base. The sweep now embeds once and passes the vector down; `KnowledgeVectorStore.search()` gained an optional trailing `query_embedding` (default `None`) so the recall arm reuses it too, leaving its existing callers unchanged. **Retrieval results are identical** — the same text through a deterministic embedder yields the same vector, which is what the four calls were each recomputing. A side effect worth knowing: the embedder-unavailable failure now raises from a single embed *before* any keyword is probed, structurally outside the per-keyword handler that tolerates individual probe failures, rather than three lines above it.

- **Two new rate limit types, and `limit_type` in logs now takes five values**: `per_session_read` and `per_session_read_hourly` join `global`, `per_session` and `per_session_hourly` across both presets (development, production) and the `ProtectionSettings` default. **An alert matching `limit_type: per_session` exactly no longer sees read refusals** — match the `per_session` prefix, which covers all four session limits, or enumerate them. Both read keys must be configured together: the middleware applies the split only when it finds both, and otherwise meters reads against the write buckets and logs why, because the limiter allows anything it holds no configuration for and a half-configured split would leave reads unmetered rather than merely tighter. `per_session_read_hourly` is also the largest window key in the system and there is one per *session*, so it — not `global` — is now the term that scales Redis memory with concurrency; see [rate-limiting-sliding-window.md](docs/architecture/security/rate-limiting-sliding-window.md#memory-bound).
- **Redefined the default LLM model per provider to one coherent, valid set** (performance-weighted: quality over price, all tool-calling + large-context) across `settings.py`, `registry.py`, and `.env.example` (Anthropic `claude-sonnet-4-6`, OpenAI `gpt-5.4-mini` (#644 restored this after #615 had briefly moved to the non-reasoning `gpt-4.1-mini`; hidden-reasoning output-budget starvation is now capped for OpenAI reasoning families as well as Gemini 3.x — see *Fixed*, below), Gemini `gemini-3.5-flash`, Fireworks `accounts/fireworks/models/deepseek-v3`, Groq `llama-3.3-70b-versatile`, Cohere `command-r-plus`, OpenRouter `anthropic/claude-sonnet-4-6`; HuggingFace `Mistral-Large-Instruct-2411` kept but not recommended — no tool calling). Previously the three sources disagreed and some IDs were invalid/deprecated. Added a `groq_model` field for parity, retired the orphaned `groq_chat_model`/`cohere_chat_model` fields, and fixed the registry + dashboard-override map to resolve Groq/Cohere via the canonical model field.
- **Standalone config is a single shared `.env`**: the containerized API reads `.env` via a read-only bind mount at `/app/.env` (not compose `env_file:`), so it is parsed by the same loader as the process runner (`scripts/faultmaven-dev.sh`); `./faultmaven.sh restart` re-reads edits, and a missing `.env` fails loudly instead of Docker creating a directory.
- **Clearer `faultmaven.sh start` output**: confirms `.env` before naming the LLM provider (reported from `CHAT_PROVIDER`), streams live `docker compose` pull/build progress (previously captured/silent), shows an elapsed/budget health-wait with per-service state, and recognizes an already-running stack instead of reporting a false port conflict.

- **KB Vector Store**: Introduced `KnowledgeVectorStore` for KB retrieval — uses collection names as-is from `KBConfig`. Replaces `CaseVectorStore` (which prepends `case_`) for all Q&A tools. Global KB collection standardized to `global_kb`.

- Removed `IP_ADDRESS` from Presidio default `entities_to_protect` — public IPs are investigation evidence, not PII. Private IPs (RFC1918) remain redacted by the regex layer.

### Removed

- **`GET /api/v1/reports/recommendations/{case_id}`** (fm#1036): removed the reports-module recommendations endpoint. It was mounted with its backing service unconditionally `None`, so no deployment could ever answer it with data — since fm#1032 its only reachable response was a static `503`. The working surface with the same recommendation contract is `GET /api/v1/cases/{case_id}/report-recommendations`.

### Fixed

- **Plain chat calls on default-reasoning OpenAI models ran with uncapped hidden reasoning, silently truncating KB answers**: `OpenAIProvider` set `reasoning_effort` on tool calls (forced `"none"`, since the gpt-5.6 family rejects function tools alongside reasoning) and on structured-JSON calls (capped `"low"`, so reasoning cannot starve the schema). A **plain** chat call — neither `tools` nor `response_format` — matched neither branch and went out with no effort parameter at all, leaving server-side default reasoning to bill against `max_completion_tokens`: the same budget the visible answer is drawn from. Observed against a live standalone run, one KB synthesis call spent **~1950 of its 2000 tokens on hidden reasoning and returned a 215-character answer**, where three sibling calls on the same prompt returned 5261–7729 characters. The failure is silent — a starved answer is still returned as a successful one, and the investigation engine consumes it as a real result. Plain calls on families that reason by default are now capped explicitly, set before the caller-kwargs merge so an explicit `reasoning_effort` still wins. The cap is scoped to `_DEFAULT_REASONING_MODEL_FAMILIES` (currently `gpt-5.6`); families that accept the parameter but stay silent unless asked (`gpt-5`, `gpt-5.4-mini`, `o1`, `o3-mini`) are deliberately unchanged. **The cap applies by call shape, so every plain chat call on such a model is affected, not only KB synthesis** — including callers that reach the provider through `provider.generate()` rather than `LLMRouter.route()`. Verified examples: `intent_resolver` requests `max_tokens=10`, so default reasoning could consume its entire budget and return nothing, silently falling through to its exception fallback — capping repairs it; `conversion_service`'s document-to-runbook conversion and `suggestion_service`'s extraction both lose default reasoning, a real change on quality-sensitive paths. Treat that list as verified rather than exhaustive: the predicate is the call shape, not an enumerated set of call sites.

- **A long KB answer silently deleted the instructions telling the model how to reply**: `MilestoneEngine._format_tool_result()` wraps the kb_qa answer in a 221-character prefix and a **369-character suffix**, and the engine truncates the combined string by keeping the **head**. The suffix is not prose — it carries the source-citation format and *"Then return the structured response by calling the response schema tool. Do not reply with plain text."* So any answer over ~7410 characters lost its tail instructions rather than its tail prose, and the observed 7729-character answer wrapped to 8319 and lost 319 of its 369 suffix characters: that turn ran without the citation guidance or the schema-tool instruction. The wrapper now reserves its own space and trims the **answer** instead, marking it `[answer truncated]` and logging the original length, budget and dropped characters — so the instructions always survive and the trim is observable rather than silent. (No downstream failure was traced to the loss; the exposure was structural.)

- **KB synthesis token budget documented against the ceiling that actually binds**: `DocumentQATool.SYNTHESIS_MAX_TOKENS` (2000, unchanged) was believed to be clipping answers, since observed visible answers ran 1346–1970 tokens against it. Measuring the whole path shows the binding limit is downstream and smaller: the engine truncates every tool result to `MilestoneEngine.TOOL_RESULT_MAX_CHARS` (8000) before it re-enters the model's context, and the kb_qa result carries ~590 characters of relay wrapper, leaving ~7410 characters — about **1845 tokens** at the 3.91–4.11 chars/token measured on real answers. One observed 7729-character answer was therefore already being clipped by the *engine*, not the token budget, and raising the budget alone would have been inert: the surplus is generated, billed, and discarded. The value stays at 2000, now a named constant carrying the derivation, and a new test pins the two constants in agreement in both directions so neither can drift out of step. **Whether 8000 characters is the right allowance for a runbook procedure inside the investigation context is a separate, open question.**

- **`ENVIRONMENT=staging` ran with no rate limiting and no request deduplication** (fm#1023): `setup_protection_middleware` routed `production` and `development` to their presets and sent every other value — including `staging`, the third `Environment` member — to a settings-driven loader gated on `BASIC_PROTECTION_ENABLED`, which defaulted to `false` and which nothing set. A staging deployment therefore booted with an empty protection middleware stack, silently. Routing is now fail-safe in both directions: only `development` selects the lenient preset, **everything else** (`staging`, `production`, any unrecognised value) selects production's, and the function's own default argument moved from `development` to `production`. **This arms rate limiting and deduplication on any box running `ENVIRONMENT=staging`** — the staging and flip-rehearsal overlays — where they were previously absent; the deduplication window is 30s and an exact-match duplicate is answered `409`. **It also puts staging on production's degrade policy, which is fail-*closed*:** a Redis outage on a staging box now *refuses* rate-limited requests rather than passing them — as `429` with a `0/0 requests` body while the per-replica fallback rung is in play, and as `503` once that rung disengages (liveness and readiness probes are exempt from both) — where previously there was no limiter and so nothing to refuse. That the refusal arrives as a `429` before it arrives as a `503` is a known reporting defect in the pinned path, not the intended shape. The unreachable loader (`load_protection_settings`, `_load_from_settings`, `_load_from_environment`), the `BASIC_PROTECTION_ENABLED` / `basic_protection_enabled` setting, and the `RATE_LIMIT_*` / `DEDUP_*` / `TIMEOUT_*` environment variables only that loader read were removed rather than left looking configurable. Two operator-visible consequences follow: **staging now has its own Redis key namespace** (`faultmaven_staging`) rather than sharing production's `faultmaven_prod`, so a staging box pointed at a production Redis no longer spends production's rate-limit quota or answers `409` to a request production already saw — nothing is orphaned, because staging had never written a protection key under any prefix; and **`ENVIRONMENT=staging` no longer auto-applies the `local` config preset**, which `detect_zero_config_preset` used to do on any under-configured box that was not explicitly `production`, quietly setting `DEBUG=true`, `SANITIZE_PII=false` and `PROTECTION_ENABLED=false` — any explicitly named environment other than `development` now declines the auto-preset, while an unset `ENVIRONMENT` keeps the zero-config quick start.
- **Browsing a case was rate limited as if it were AI work** (fm#994): every request carrying an `X-Session-ID` shared a single `per_session` bucket — 5 requests/minute under the production preset, which is what every deployment other than `ENVIRONMENT=development` runs — so a Copilot or Dashboard case view — which issues well over ten GETs for details, files, messages and history — returned `Error loading case: Rate limit exceeded. Please try again in 44 seconds.` A session now holds **two independent pairs** of per-session buckets and each request is charged to exactly one: cheap reads to `per_session_read` / `per_session_read_hourly` (production 120/min, 1200/hour), and writes to `per_session` / `per_session_hourly` (production 10/min, 50/hour), which is the quota that protects LLM compute. A burst of navigation can no longer refuse the next `POST .../turns`. Classification is by cost rather than by HTTP verb: `GET /cases/{id}/report-recommendations` and `GET /knowledge/documents/{id}/snippet` each run a query embedding and a vector similarity search per call, so they are metered as writes despite being GETs.

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
