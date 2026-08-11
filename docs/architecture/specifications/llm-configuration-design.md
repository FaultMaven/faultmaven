# LLM Configuration System Design

## Status

**Draft** (original 2026-03-20). **Amended 2026-06-14** per ADR-004 (`faultmaven-doc-internal`): deployment mode is now **canonical (`DEPLOYMENT_MODE`)** and **decoupled from `AUTH_MODE`**; a fail-fast **deployment coherence gate** and the **complete config port** are added; terminology aligned to `standalone`/`cloud`. The rest of this design stands except where the amended sections (Deployment Modes, Configuration Completeness) supersede it.

## Problem Statement

The current LLM configuration system has several design flaws:

1. **Two sources of truth**: `.env` file and `llm_config_overrides` DB table compete silently. DB overrides `.env`, but the admin has no visibility into which source is active.
2. **No deployment-mode awareness**: Local self-hosted users and cloud platform admins use the same config path, despite fundamentally different needs.
3. **Strict mode limits manageability**: `strict_provider_mode=true` prevents initializing non-primary providers, which blocks testing and configuration of alternative providers via the dashboard.
4. **No separation between credential management and routing**: Saving an API key and switching the active provider are conflated in the UX.
5. **No path to per-user preferences**: LLM config is entirely system-wide with no model for individual user choice.

## Design Principles

1. **One source of truth per deployment mode.** Local: `.env` file. Cloud: config DB.
2. **Configuration determines capability.** Missing key = feature unavailable, not a runtime error. (The Tavily pattern.)
3. **Separate credential management from routing.** Adding an API key ≠ activating a provider.
4. **Design for per-user preferences from day one**, even if the initial implementation is system-wide only.

## Deployment Modes

> **Terminology (per ADR-004):** "Local mode" → **`standalone`**, "Cloud mode" → **`cloud`**. `local` is now reserved for `AUTH_MODE=local` only. Older "Local mode" wording elsewhere in this doc means `standalone`.

The deployment mode is **canonical**, set explicitly by `DEPLOYMENT_MODE`:

- `DEPLOYMENT_MODE=standalone` → single-process, single-user (the default)
- `DEPLOYMENT_MODE=cloud` → orchestrated (k8s), multi-tenant

**It is NOT derived from `AUTH_MODE`.** The previous design computed `deployment = "cloud" if auth_mode == "oauth" else "local"` — which silently mislabels a cloud deployment as standalone whenever its `AUTH_MODE` is wrong. That exact coupling caused a production incident: a cloud k8s deployment whose Secret carried `AUTH_MODE=local` was treated as standalone (DB overrides skipped, dashboard read-only, auth bypassed) while running on full cloud infrastructure (Postgres/Redis/ChromaDB). See ADR-004 §D and its 2026-06-14 incident note.

`is_cloud` / `is_standalone` derive from `DEPLOYMENT_MODE` **alone**:

```python
# faultmaven/config/settings.py
settings.is_cloud       # deployment_mode == "cloud"
settings.is_standalone  # not is_cloud
```

`auth_mode`, the storage backends, and tenancy are **consequences** that must be *coherent* with `DEPLOYMENT_MODE` (enforced by the gate below) — not inputs that decide the mode.

### Deployment Coherence Gate (fail-fast, at boot)

`validate_deployment_coherence(settings)` runs during startup and **refuses to boot** on any mismatch, raising one error that lists every incoherence. No mixed state is representable.

| `DEPLOYMENT_MODE=cloud` requires | Why |
|---|---|
| `auth_mode == oauth` | Cloud is multi-user; `local` bypasses auth |
| RS256 key material present (`JWT_PRIVATE_KEY`/`_PATH` + public) | OAuth tokens are RS256 |
| `DATABASE_URL` is PostgreSQL | SQLite is single-writer / standalone |
| real Redis (session storage `redis` + host/url) | FakeRedis is ephemeral / standalone |
| WorkOS AuthKit configured (`WORKOS_API_KEY` + `WORKOS_CLIENT_ID` + `WORKOS_REDIRECT_URI`) | Cloud sign-in is AuthKit-only (ADR-015); without an IdP the deployment sits dark while looking healthy |
| `STORAGE_BACKEND=s3` **with** `S3_BUCKET_NAME` | Filesystem storage is single-node (RWX-volume SPOF, infra#127); a bucket-less s3 config would only fail at first upload |
| external ChromaDB (`CHROMADB_URL` + `VECTOR_STORAGE_TYPE=chromadb`) | A local PersistentClient writes vectors into one container's filesystem — per-replica search on web pods, silent vector loss in seeding jobs (#901). The client factories enforce the same refusal at build time (`ChromaUnavailableError`) when the configured server is unreachable |

**Tenancy is an independent axis.** `cloud` validates cloud-native infra + real auth, *not* tenancy — a cloud deployment may be single-tenant (one organization, many users) or multi-tenant (SaaS). `TENANT_PROVIDER` is chosen separately, and (per the multi-tenancy boundary review) real multi-tenancy is provided by `faultmaven-cloud`, not CE.

`DEPLOYMENT_MODE=standalone` keeps the simple defaults (local auth, SQLite, FakeRedis, single tenant); the gate flags only egregious mixes (e.g. standalone declaring `auth_mode=oauth`). The dangerous, asymmetric failure is **cloud silently running as standalone**, which the gate makes impossible. This replaces the three independent, unsynchronized "is cloud?" checks (`auth_mode`, `dashboard_url`, `tenant_provider`) with one canonical switch + one gate.

### Investigation Tooling Gate (fail-fast, at boot)

`validate_investigation_tooling(settings, registry)` (`config/investigation_capability.py`) runs during startup, alongside the coherence and credential gates, and **refuses to boot** when the **resolved investigation model** — `DA_PROVIDER`, falling back to `CHAT_PROVIDER` — does not support tool calling.

**Why a hard gate, not a warning.** Directed Analysis (`search_file`, `deep_analysis`) is how the engine *gathers evidence*, and it requires function/tool calling. A tool-incapable investigation model can't reach the evidence yet will still emit conclusions from whatever is already in context — exactly the *premature / unfounded conclusion* the product guarantees against. A log line nobody reads doesn't protect that guarantee, and the condition is deterministically detectable at boot.

**Explicit opt-out for degraded/offline mode.** `ALLOW_TOOLLESS_INVESTIGATION=true` is a knowing choice (e.g. a local model with no tools). With it set, the gate logs a loud warning and boots; `/health` then reports `status: degraded` with an `investigation` block (`tools_available: false`, provider/model/reason) so the state stays visible, not log-only. Degraded-by-choice is allowed; degraded-by-accident is not.

**Scope.** Tool calling is required only for the **investigation role** (CHAT/DA); `CLASSIFIER_PROVIDER` / `SYNTHESIS_PROVIDER` overrides never call tools, so a tool-incapable model there is fine and untouched by the gate. The gate keys *solely* on the server's `DA_PROVIDER` → `CHAT_PROVIDER` resolution, so LLMs configured **outside** the FaultMaven server — e.g. a simulation/eval **persona** that only generates user text — are a separate config it never sees. A denylisted model such as `minimax-m2p7` is therefore perfectly valid as a sim persona; only its use as the *investigation* model is gated. The per-turn runtime fallback in `milestone_engine` (catch `ToolCallingUnsupportedError`, fall through to the non-tool path) remains the safety net for *transient* tool failures on an otherwise-capable model — defense in depth, not a substitute for the gate. The gate is skipped in test environments (`SKIP_SERVICE_CHECKS` / pytest), matching the other startup gates.

### Standalone Mode (`DEPLOYMENT_MODE=standalone`)

| Aspect | Behavior |
|--------|----------|
| Config source | `.env` file only |
| Config DB | Not used (skip `apply_overrides_to_settings()`) |
| Dashboard | Read-only status view |
| Config changes | User edits `.env`, restarts server |
| Hot-reload | Not available |

**Rationale**: The local user has full control of the server. They can edit files and restart. Adding a DB layer creates confusion (two sources, precedence rules) with no benefit.

### Cloud Mode

| Aspect | Behavior |
|--------|----------|
| Config source | Config DB (authoritative) |
| `.env` role | Seed values for first boot + fallback for keys not in DB |
| Dashboard | Full admin control |
| Config changes | Dashboard → DB → hot-reload |
| Hot-reload | Yes, via `save_and_reload()` |

**First boot flow:**
1. Server reads `.env` via pydantic-settings
2. DB is empty → `.env` values are seeded into DB for all DB-managed settings
3. `apply_overrides_to_settings()` applies DB values (same as `.env` on first boot)

**Subsequent boot flow:**
1. Server reads `.env` via pydantic-settings (provides defaults)
2. `apply_overrides_to_settings()` applies DB values, overriding `.env`
3. DB values are authoritative for all DB-managed settings

**Hot-reload flow (dashboard save):**
1. Dashboard writes to DB via PUT endpoint
2. `save_and_reload()`: reset settings → re-read `.env` → apply DB overrides → reset registry
3. New registry initializes with updated config

**Key change from current behavior**: In cloud mode, if an admin wants to change a value, they use the dashboard. Editing `.env` on the server and restarting will NOT take effect for any key that exists in the DB. The dashboard should show the config source (`env-default` vs `admin-override`) for transparency.

## Configuration Scope

### What the DB stores (cloud mode)

Settings that are safe and meaningful to change at runtime without infrastructure changes.

| Category | Settings | Count |
|----------|----------|-------|
| **LLM Providers** | API keys (9 providers), models (9 providers), primary provider, strict mode | ~20 |
| **Capability Overrides** | `CODE_PROVIDER`, `MULTIMODAL_PROVIDER`, `SYNTHESIS_PROVIDER`, `CLASSIFIER_PROVIDER`, `DA_PROVIDER` | 5 |
| **Feature Toggles** | `ENABLE_WEB_SEARCH`, `TAVILY_API_KEY` | 2 |
| **Operational Tuning** | `LOG_LEVEL`, `MAX_UPLOAD_SIZE_MB` | 2 |
| **Observability** | `OPIK_ENABLED`, `OPIK_TRACK_DISABLE` (SDK-owned), `OPIK_PROJECT_NAME`, `OPIK_USE_LOCAL`, `OPIK_LOCAL_URL`, `OPIK_API_KEY`, `OPIK_LOG_RAW_PROMPTS` | 7 |
| **LLM Behavior** | `LLM_REQUEST_TIMEOUT`, `LLM_MAX_RETRIES`, `LLM_MAX_TOKENS` | 3 |

**Total: ~42 settings**

### What stays in `.env` only (never in DB)

Settings that require infrastructure changes or have security implications if changed at runtime.

| Category | Settings | Why |
|----------|----------|-----|
| **IAM** | `AUTH_MODE`, `JWT_SECRET_KEY`, `JWT_PRIVATE_KEY_PATH`, `JWT_PUBLIC_KEY_PATH`, OAuth settings | Determines deployment mode itself. Changing at runtime is dangerous. |
| **Storage Backends** | `DATABASE_URL`, `DB_BACKEND`, `REDIS_URL`, `CACHE_BACKEND`, `CHROMADB_URL`, `VECTOR_BACKEND`, `STORAGE_BACKEND` | Requires actual infrastructure (PostgreSQL, Redis, S3) to be running. |
| **Network/Security** | `CORS_ALLOW_ORIGINS`, `CORS_ALLOW_CREDENTIALS` | Security-critical. Wrong CORS config could expose the API. |
| **PII** | `SANITIZE_PII` | Enabling/disabling mid-session could leak previously-protected data. Requires deliberate restart. |
| **Provider Base URLs** | `OPENAI_API_BASE`, `ANTHROPIC_API_BASE`, etc. | Rarely changed. Incorrect values break providers silently. |

### Configuration Completeness & the cloud-required set (the port)

The two tables above are *illustrative*, not the full surface. The canonical surface is **`settings.py`** (~150 settings across ~25 classes). Every setting is tagged on three axes so neither deployment surface drifts:

- **scope**: `standalone` | `cloud` | `both`
- **home**: `bootstrap-env` (ConfigMap) | `secret` | `operational-override` (DB, hot-reload) | `derived`
- **cloud-required**: must-be-explicit (fails the coherence gate if unset/defaulted) vs safe-to-default

`cloud-required` closes the incident's root cause: the cloud LLM layer (active `*_MODEL`, capability providers, `STRICT_PROVIDER_MODE`) and parts of the data-tier wiring were absent from cloud config and silently fell to stale code defaults (e.g. `gemini-2.0-flash`, now 404). Tagging them `cloud-required` makes a cloud boot fail loudly instead of serving a stale default. The standalone `.env.example` is **generated** from the `standalone`/`both` scope; cloud config is **validated** against the `cloud-required` set (the boot gate + a CI parity check against the k8s ConfigMap/Secret).

### DB Schema

Rename `llm_config_overrides` → `config_overrides` since the scope is broader than LLM.

```sql
CREATE TABLE config_overrides (
    key         VARCHAR(100) PRIMARY KEY,
    value       TEXT NOT NULL,
    category    VARCHAR(50) NOT NULL,   -- 'llm', 'feature', 'operational', 'observability'
    source      VARCHAR(20) NOT NULL DEFAULT 'admin',  -- 'env-seed', 'admin'
    updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_by  VARCHAR(255)            -- user_id of admin who changed it
);
```

The `source` column tracks provenance:
- `env-seed`: Value was seeded from `.env` on first boot. Admin hasn't touched it.
- `admin`: Value was explicitly set by an admin via the dashboard.

The `category` column enables the dashboard to group settings logically.

## Provider Lifecycle

### Provider States

A provider has three possible states, derived from configuration:

```
NOT_CONFIGURED  →  No API key. Not initialized. Not available.
CONFIGURED      →  API key present. Initialized. Can be tested. Not routing traffic.
ACTIVE          →  Configured + selected as primary (or in fallback chain). Routing traffic.
```

### Strict Mode Redefined

**Current behavior** (problematic): `strict_provider_mode=true` means "only _initialize_ the primary provider." This prevents testing or configuring other providers.

**New behavior**: Strict mode is a **routing** constraint, not an **initialization** constraint.

| | Provider Initialization | Traffic Routing |
|--|------------------------|-----------------|
| **Strict mode** (single) | All providers with valid API keys | Primary provider only |
| **Multi mode** (fallback) | All providers with valid API keys | Primary → fallback chain |

This means:
- Admin can configure and test multiple providers regardless of mode
- Strict mode = "route all traffic to one provider" (the user's mental model)
- Switching the active provider is a single action: change `primary_provider`

### Initialization Change

In `registry._initialize_from_settings()`, remove the strict mode gate on initialization:

```python
# BEFORE (problematic):
if strict_mode:
    providers_to_init = {primary_provider: PROVIDER_SCHEMA[primary_provider]}
else:
    providers_to_init = PROVIDER_SCHEMA

# AFTER:
# Always initialize all providers that have valid API keys.
# Strict mode only affects the fallback chain (routing), not initialization.
providers_to_init = PROVIDER_SCHEMA
```

The `_setup_fallback_chain()` method already handles strict mode correctly for routing — it only adds the primary provider to the chain when strict mode is on.

## Dashboard Views

### Standalone Mode — Read-Only Status

The dashboard shows a summary of the server's current configuration as a reference for the user. No edit controls.

**What's shown:**

| Section | Content |
|---------|---------|
| Active LLM | Provider name, model, health status |
| Configured Providers | List of providers with keys set (no key values) |
| Features | Web search (enabled/disabled), Deep analysis status |
| Infrastructure | Auth mode, DB backend, session storage, vector storage |

**Purpose**: Remind the user what they configured in `.env`. Help diagnose "why isn't feature X working?" without SSH.

### Cloud Mode — Admin View

Full control over all DB-managed settings. Requires `admin` role.

**Layout:**

| Section | Content | Editable |
|---------|---------|----------|
| **Active Provider** | Primary provider selector, model selector | Yes |
| **Provider Cards** | Per-provider: API key (set/test), model selector, health status, "Set as Active" button | Yes |
| **Routing Mode** | Strict (single provider) vs Multi (fallback chain) toggle | Yes |
| **Features** | Web search toggle, Tavily key | Yes |
| **Operational** | Log level, rate limits, upload limits | Yes |
| **Observability** | Opik enabled, tracing toggles, project name | Yes |
| **Infrastructure** | Auth mode, DB backend, storage (read-only, from `.env`) | No |

**Key UX elements:**

1. **Config source indicator**: Each setting shows `env-default` or `admin-override` badge. Makes precedence visible.
2. **Active provider badge**: One provider card has a green "Active" badge. Others show "Configured" or "Not configured."
3. **Test result context**: After testing, show "Key valid — this is your active provider" or "Key valid — not currently active. Set as active?"
4. **Capability overrides section** (advanced): Which providers handle code/multimodal/synthesis/classifier tasks. Defaults to primary for all.

### Cloud Mode — Regular User View

Read-only status of what's available. No API keys visible. No edit controls.

**What's shown:**

| Section | Content |
|---------|---------|
| Active LLM | Provider name, model (e.g., "Gemini — gemini-2.0-flash") |
| Available Features | Web search (on/off), Deep analysis (on/off) |
| System Status | Health indicators for active provider |
| My Preferences | _(Future: model picker from admin-allowed list)_ |

**Purpose**: User knows what they're working with. In the future, this is where they'd pick their preferred model from the admin's allowed list.

## Per-User Preferences (Future Design)

### Three-Layer Model

```
┌─────────────────────────────────────────────┐
│  Platform Layer (admin)                      │
│  config_overrides table                      │
│  "Which providers have API keys?"            │
│  "What's the system default?"                │
├─────────────────────────────────────────────┤
│  User Layer (each user)                      │
│  user_preferences table                      │
│  "What's my preferred provider/model?"       │
│  Constrained to: platform-configured options │
├─────────────────────────────────────────────┤
│  Request Layer (runtime)                     │
│  Router receives provider_hint per request   │
│  Falls back to system default if unavailable │
└─────────────────────────────────────────────┘
```

### User Preferences Table (future)

```sql
CREATE TABLE user_preferences (
    user_id             VARCHAR(255) PRIMARY KEY,
    preferred_provider  VARCHAR(50),        -- NULL = use system default
    preferred_model     VARCHAR(200),       -- NULL = use provider's default model
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
```

### Router Integration (future)

```python
async def route(self, ..., provider_hint: Optional[str] = None):
    if provider_hint and self.registry.get_provider(provider_hint):
        # User preference is available → use it
        routing_order = [provider_hint]
    else:
        # Fall back to system default chain
        routing_order = self._get_routing_order()
```

### Admin Controls for User Choice (future — Option 1 → Option 3)

**Option 1** (next step): Admin marks provider/model pairs as "user-selectable" in the config DB. Users pick from that list.

```sql
-- Additional column on config_overrides or separate table
CREATE TABLE allowed_user_models (
    provider    VARCHAR(50) NOT NULL,
    model       VARCHAR(200) NOT NULL,
    tier        VARCHAR(20) DEFAULT 'standard',  -- 'standard', 'premium'
    PRIMARY KEY (provider, model)
);
```

**Option 3** (ultimate goal): Tier-based access. Admin defines model tiers. User roles determine which tiers are accessible.

| Tier | Models | Access |
|------|--------|--------|
| Standard | gemini-2.0-flash, gpt-4o-mini, llama-3.3-70b | All users |
| Premium | claude-opus, gpt-4o, gemini-2.5-pro | Admin / premium role |

This maps naturally to the existing RBAC system (`Role.ADMIN`, `Role.MEMBER`, `Role.VIEWER`).

## Implementation Phases

### Phase 1: Foundation (Current Sprint)

**Goal**: Clean separation of local vs cloud mode. No new features, just correct behavior.

1. **Local mode**: Skip `apply_overrides_to_settings()` when `AUTH_MODE=local`. Dashboard LLM page becomes read-only.
2. **Strict mode fix**: Initialize all keyed providers regardless of strict mode. Strict mode only affects routing.
3. **Router fix**: `registry` as property (already done).
4. **Test endpoint fix**: `create_provider_for_test()` fallback (already done).

### Phase 2: Cloud Admin Dashboard

**Goal**: Full admin config management via dashboard with hot-reload.

1. **Rename table**: `llm_config_overrides` → `config_overrides` with `category` and `source` columns.
2. **Expand allowed overrides**: Add feature toggles, operational tuning, observability settings.
3. **First-boot seeding**: On first startup in cloud mode, seed DB from `.env` values.
4. **Admin dashboard UI**: Provider cards with "Active" badge, config source indicators, capability overrides section.
5. **API changes**: GET endpoint returns `source` per setting. PUT endpoint accepts broader setting categories.

### Phase 3: User View + Preferences Foundation

**Goal**: Regular users see system status. Schema ready for preferences.

1. **User dashboard view**: Read-only status of active provider, features, health.
2. **`user_preferences` table**: Created but unused (schema ready for Phase 4).
3. **API**: New endpoint `GET /api/v1/user/preferences` returning available options and current preference.

### Phase 4: Per-User Model Selection

**Goal**: Users can pick from admin-allowed providers/models.

1. **Admin UI**: Mark models as user-selectable.
2. **User UI**: Model picker in dashboard profile or copilot settings.
3. **Router**: Accept `provider_hint` from request context (user's JWT → preference lookup → router).
4. **Constraint enforcement**: User preference validated against `allowed_user_models`.

### Phase 5: Tier-Based Access (Option 3)

**Goal**: Cost-aware model access based on user roles.

1. **Tier definitions**: Admin defines standard/premium model tiers.
2. **RBAC integration**: Map tiers to roles.
3. **Cost tracking**: Per-user LLM usage metrics (optional).

## Data Processing Model

### Overview

Evidence processing has two phases: **preprocessing** (at upload time) and
**query strategies** (during investigation). These are not sequential tiers —
preprocessing always runs, then the agent picks a query strategy per turn.

### Preprocessing (upload time, always runs)

| Step | Operation | LLM? | Output |
|------|-----------|------|--------|
| **Classification** | Determine data type (logs, metrics, config, code, text, image) | No | `data_type`, `detailed_data_type` |
| **Structural Index** | Domain-specific extraction: error counts, time ranges, anomalies, patterns, sample entries | No | Compressed summary for LLM context (~8KB) |

Structural index extraction has a **2-second timeout** with fallback to truncated preview.

### Query Strategies (investigation time, agent chooses per turn)

| Strategy | Tool | How it works | LLM call? | Best for |
|----------|------|-------------|-----------|----------|
| **Keyword/regex search** | `search_file` | Grep on raw file content, return matching lines with context | No | "Show me lines with X", exact matching |
| **Interpreted search** | `search_file` with `interpret: true` | Grep + dedicated LLM call to reason over results in isolation | Yes (focused) | "What caused X?", pattern reasoning |
| **Semantic search** | `vectorize_file` → `case_evidence_qa` | Chunk + embed into ChromaDB, similarity search | No (embeddings) | Large files, vague queries |

### Scenario Mapping

| Scenario | Preprocessing | Query strategy |
|----------|--------------|----------------|
| Data uploaded, no question | Classification + Structural index | None — index is the deliverable |
| Data + general question ("summarize") | Classification + Structural index | None — LLM reads the index directly |
| Data + specific question | Classification + Structural index | Agent picks keyword, interpreted, or semantic |
| Follow-up question on prior data | Already done | Agent picks keyword, interpreted, or semantic |

### Interpreted Search (formerly "deep_analysis")

The key difference between keyword search and interpreted search is **context isolation**:

- **Keyword search**: raw lines return to the main conversation context. The agent interprets them alongside everything else (prior messages, other evidence, hypotheses).
- **Interpreted search**: a **dedicated, focused LLM call** receives only the relevant file sections + investigation context + the specific question. Returns a pre-interpreted answer.

This is valuable when:
- The main conversation context is long and noisy
- The question requires reasoning across multiple lines (e.g., "is this a brute force attack or a misconfigured service?")
- The raw lines alone don't tell the story without interpretation

Interpreted search is a core investigation capability, not a feature toggle. It uses the already-configured LLM provider with no additional setup. It is enabled by default and should not appear on the dashboard as a configurable feature.

### Implementation Notes

- **Current state**: `deep_analysis` is a separate tool with `DEEP_ANALYSIS_BACKEND` config (default: `disabled`). The `basic` backend duplicates `search_file` functionality.
- **Target state**: Merge into `search_file` as `interpret: true` option. Remove `basic` backend. Default to enabled (uses configured LLM). `external` backend preserved for enterprise use as a separate configuration.
- **Terminology**: Replace "Tier 0/1/2/3" with "Preprocessing" and "Query strategies" throughout codebase and docs.

## Dashboard Feature Visibility

Features shown on the dashboard are capabilities that depend on **user-provided configuration** (API keys, service URLs). Core capabilities that work automatically with the existing LLM should not appear as toggles.

| Feature | Dashboard | Why |
|---------|-----------|-----|
| **Web search** | Shown — requires `TAVILY_API_KEY` | User must provide a separate API key |
| **LLM tracing** | Shown — requires Opik setup | User must configure Opik (cloud key or local instance) |
| **Interpreted search** | NOT shown | Uses existing LLM, no additional config needed |
| **Semantic search** | NOT shown | Uses existing ChromaDB/embeddings infrastructure |

## API Changes

### GET /api/v1/admin/llm/config

Add to response:
```json
{
  "deployment": "local" | "cloud",
  "config_readonly": true | false,
  "providers": {
    "fireworks": {
      "state": "not_configured" | "configured" | "active",
      "config_source": "env-default" | "admin-override",
      ...existing fields...
    }
  }
}
```

### GET /api/v1/user/config (new — Phase 3)

Non-admin view of system capabilities:
```json
{
  "active_provider": "gemini",
  "active_model": "gemini-2.0-flash",
  "features": {
    "web_search": true
  },
  "health": "healthy",
  "user_preference": null,
  "available_models": []
}
```

### PUT /api/v1/admin/config (expanded — Phase 2)

Accept settings beyond LLM:
```json
{
  "category": "llm" | "feature" | "operational" | "observability",
  "settings": {
    "key": "value"
  }
}
```

## Migration from Current System

1. **Rename table**: `llm_config_overrides` → `config_overrides` (Alembic migration).
2. **Add columns**: `category`, `source` to existing rows.
3. **Backfill**: Existing rows get `category='llm'`, `source='admin'`.
4. **Update `_ALLOWED_OVERRIDES`**: Expand whitelist to include new categories.
5. **Conditional DB usage**: `apply_overrides_to_settings()` checks deployment mode before reading DB.
6. **First-boot seeding**: New function `seed_config_from_env()` runs once when DB table is empty in cloud mode.

## Case Archival Lifecycle

### Current Implementation (Phase 1)

Cases have an `is_archived: bool` field (default `false`) and `archived_at` timestamp.
Only terminal cases (RESOLVED or CLOSED) can be archived. Archiving is a user action
from the dashboard, independent of case state.

| Component | Status |
|-----------|--------|
| `is_archived` flag on domain model | Done |
| `is_archived` column in DB schema (indexed) | Done |
| `POST /cases/{id}/archive` and `/unarchive` endpoints | Done |
| Dashboard archive UI (list + detail pages) | Done |
| Service-layer filtering (`include_archived` query param) | Done |

### Future: Full Data Lifecycle (Phase 2+)

**PostgreSQL partitioning (cloud deployment):**
- List-partition `cases` table by `is_archived`: `cases_active` (FALSE) and `cases_archive` (TRUE)
- Queries with `WHERE is_archived = FALSE` automatically hit only the active partition
- SQLite (local deployment) uses the indexed boolean — partitioning not needed

**Auto-archive sweeper job:**
- Background task that archives terminal cases after a grace period (e.g., 30 days)
- Condition: `status IN ('closed', 'resolved') AND closed_at < NOW() - INTERVAL '30 days'`
- Sets `is_archived = TRUE` and `archived_at` timestamp
- Runs as nightly cron or scheduled task

**Knowledge base exclusion:**
- Archived cases excluded from "similar past cases" suggestions
- Archived cases excluded from RAG/vector search results
- Explicit opt-in to search archived knowledge

**Cold storage offload (future):**
- Export archived case data (messages, evidence, reports) to object storage (S3/GCS)
- Delete from PostgreSQL archive partition
- Retention policy configuration (e.g., 1 year in archive, then cold storage)

### Archive vs Close — Design Distinction

| Concept | What it means | Who does it | Reversible? |
|---------|--------------|-------------|-------------|
| **CLOSED** (status) | Investigation ended without solution | Agent or user via copilot | No (terminal) |
| **RESOLVED** (status) | Investigation ended with solution | Agent or user via copilot | No (terminal) |
| **Archived** (flag) | Case hidden from active views, excluded from search | User via dashboard | Yes (unarchive) |

Archiving is about **data lifecycle management** — reducing clutter, improving DB performance,
and eventually moving data to cheaper storage. It is not a case state.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Stale DB overrides after `.env` changes in cloud mode | Dashboard shows `source` column. Admin can "reset to env default" per setting. |
| First-boot seeding misses new settings added later | Seeding runs per-key: only seeds keys not already in DB. New settings get `.env` default until admin changes them. |
| Per-user preferences increase LLM costs | Admin controls the allowed model list. Tier system (Phase 5) adds cost boundaries. |
| Migration breaks existing overrides | Alembic migration preserves existing data, only adds columns with defaults. |

## Changes Log

Summary of changes implemented during the design session (2026-03-20):

### Backend (faultmaven/)
- **LLM Router**: `registry` changed from instance attribute to `@property` for hot-reload support
- **Provider Registry**: All keyed providers initialize regardless of strict mode; strict mode only affects routing
- **Registry**: Added `create_provider_for_test()` for testing providers not in active fallback chain
- **Admin Config API**: GET returns `deployment`, `config_readonly`, provider `state`; PUT rejects in local mode (403)
- **API Models**: Added `FeatureStatus`, provider `state` field, `deployment`/`config_readonly` to config response
- **Case Model**: Added `is_archived` and `archived_at` fields
- **Case Service**: Filters by `is_archived` instead of `status != CLOSED`
- **Case Routes**: New `POST /cases/{id}/archive` and `/unarchive` endpoints
- **Startup**: `apply_overrides_to_settings()` skipped in local mode
- **Deep Analysis**: Default changed from `disabled` to `local`
- **HTTP 422**: Fixed `HTTP_422_UNPROCESSABLE_CONTENT` → `HTTP_422_UNPROCESSABLE_ENTITY` in 13 files
- **Test Connection**: Increased `max_tokens` from 10 to 50 to fix Gemini truncation error

### Frontend (faultmaven-dashboard/)
- **LLMConfigPage**: Deployment-mode-aware (read-only for local, full controls for cloud)
- **ProviderCard**: Shows provider state badges (Active/Configured/Not configured); readonly support
- **FeatureStatusPanel**: New component showing web search and tracing status
- **CaseListPage**: Archive button uses `is_terminal && !is_archived`; "Include archived" checkbox
- **CaseDetailPage**: Archive button for terminal non-archived cases only
- **Types**: Added `ProviderState`, `FeatureStatus`, `is_archived` to case types
