# Environment Variables Guide

> **For deployment mode / preset philosophy**, see [Principle 1: Deployment Agnostic Architecture](../architecture/core-architecture/architectural-design-principles.md#1-deployment-agnostic-architecture) in the canonical principles doc.
> **This document** is the per-variable reference.

## Where are environment variables set?

Environment variables are defined in **`.env` file** in the project root directory.

### File Structure

```
FaultMaven/
├── .env                    # Active configuration (gitignored - never commit!)
├── .env.example            # Template with all available variables and defaults
├── .env.backup             # Backup (if exists)
└── faultmaven/
    └── config/
        └── settings.py     # Loads and validates environment variables
```

### Files Explained

1. **`.env`** - Your actual configuration
   - Contains real API keys, passwords, settings
   - **GITIGNORED** - Never committed to version control
   - Created by copying `.env.example` and filling in values

2. **`.env.example`** - Template and documentation
   - Shows all available environment variables
   - Includes comments explaining each variable
   - Safe to commit (contains no secrets)
   - Used by new developers to create their `.env`

## When are they loaded?

Environment variables are loaded **at application startup** in this sequence:

### 1. Startup Sequence

```
Application Start
    ↓
get_settings() called (first time)
    ↓
load_settings_dotenv()  ← Loads ./.env into the environment (never over a set variable, in any case)
    ↓
Preset defaults applied (CONFIG_PRESET, or the zero-config `local` preset; none under CONFIG_PRESET=none)
    ↓
FaultMavenSettings.__init__() ← Pydantic reads env vars
    ↓
Validation & Type Checking
    ↓
Settings instance created (singleton)
    ↓
Application continues with loaded settings
```

### 2. Code Location

**File**: `faultmaven/config/settings.py`

```python
def get_settings() -> FaultMavenSettings:
    """Get global settings instance (singleton pattern)"""
    global _settings_instance
    if _settings_instance is None:
        # Load ./.env BEFORE creating settings, without overriding
        load_settings_dotenv()  # ← HERE: .env loaded

        # Create settings instance (reads env vars)
        _settings_instance = FaultMavenSettings()  # ← HERE: Env vars read

    return _settings_instance
```

`.env` is read from the **working directory** only: no parent directory is
searched, so start the process in the directory that holds it, as every
launcher in this repository does. `faultmaven/main.py`, `faultmaven/jobs/run.py`
and `alembic/env.py` call the same loader. Under `FAULTMAVEN_SKIP_DOTENV` no
`.env` is read at all.

### 3. Singleton Pattern

Settings are loaded **once** when first accessed, then reused:

```python
# First call - loads .env and creates settings
settings = get_settings()  # Loads .env, reads env vars

# Subsequent calls - returns same instance
settings = get_settings()  # No reload, returns cached instance
```

**Important**: Changes to `.env` require application restart to take effect.

## How Pydantic Loads Environment Variables

### Automatic Loading

Pydantic's `BaseSettings` automatically:
1. Looks for environment variables matching field names
2. Uses `env="VARIABLE_NAME"` to map non-standard names
3. Applies type validation (int, float, bool, str)
4. Uses defaults if env var not set

### Example

```python
class ConversationThresholds(BaseSettings):
    max_clarifications: int = Field(default=3, env="MAX_CLARIFICATIONS")
    #                                ↑          ↑
    #                             default    env var name
```

**Loading sequence**:
1. Check for `MAX_CLARIFICATIONS` env var
2. If found: validate it's an int, use it
3. If not found: use default value (3)
4. If invalid type: raise validation error

## Configuration Priority

Settings are loaded in this priority order (highest to lowest):

1. **System environment** (OS-level env vars set before the process starts)
2. **`.env` file** (never replaces 1, whatever the letter case of either name)
3. **Preset defaults** (`CONFIG_PRESET`, or the zero-config `local` preset; none under `CONFIG_PRESET=none`)
4. **Default values** (in `settings.py`)

### `CONFIG_PRESET`

A preset fills in variables that neither the environment nor the `.env` sets. It never overrides a value either of them sets.

| Value | Effect |
|-------|--------|
| `local` (alias `minimal`) | Development defaults: `DEBUG`, a local LLM, a local Chroma, PII redaction off |
| `standard` | Single-server production: Redis sessions, ChromaDB |
| `enterprise` | Production server settings with `WORKERS=4`, vectors in ChromaDB, OpenAI for chat, PII sanitized, and `DEPLOYMENT_MODE=cloud`: a cloud deployment, which startup refuses until the cloud settings are in place, each refusal naming what is missing (see the [deployment coherence gate](../architecture/specifications/llm-configuration-design.md#deployment-coherence-gate-fail-fast-at-boot)). For a self-hosted install, set `DEPLOYMENT_MODE=standalone`, which overrides only that value. The preset names no service: set `DATABASE_URL`, `REDIS_HOST` and `CHROMADB_URL`. It sets `ENVIRONMENT=production`, where the shipped wildcard extension origins in `CORS_ALLOW_ORIGINS` are refused, so set your extension's own origins too. With more than one worker, startup refuses the in-process Redis stand-in and a local Chroma, which each worker would hold alone, and a SQLite `DATABASE_URL` (several workers need PostgreSQL) |
| `none` (any letter case) | No preset. None is applied, and the zero-config `local` preset is not detected either. Every setting is the environment's, the `.env`'s or the default in `settings.py` |
| unset | The zero-config `local` preset is applied when nothing configures an LLM provider, a Redis server (`REDIS_URL` or `REDIS_HOST`), a ChromaDB server (`CHROMADB_URL`) or an `ENVIRONMENT` other than `development`. A blank value configures nothing. Otherwise no preset is applied |

`none` is the operator's opt-out from zero-config detection: a deployment that wants exactly what it configured, and nothing a preset would add. Test Cloud's test steps set it (#2243).

No preset chooses where cases and users are kept: under every one they are in the database `DATABASE_URL` names (the SQLite file by default). Sessions are in Redis, the server `REDIS_URL` or `REDIS_HOST` names, or the in-process stand-in when none is reachable on standalone. `GET /admin/config/status` reports which stores the running process built.

## ConversationThresholds Variables

All these are **optional** - application works with defaults if not set.

### In `.env` file:

```bash
# Conversation Limits
MAX_CLARIFICATIONS=3                    # Default: 3
MAX_CONVERSATION_TURNS=20               # Default: 20
MAX_CONVERSATION_TOKENS=4000            # Default: 4000

# Token Budgets
CONTEXT_TOKEN_BUDGET=4000               # Default: 4000
SYSTEM_PROMPT_MAX_TOKENS=500            # Default: 500
PATTERN_TEMPLATE_MAX_TOKENS=300         # Default: 300

# Classification Thresholds
PATTERN_CONFIDENCE_THRESHOLD=0.7        # Default: 0.7
CONFIDENCE_OVERRIDE_THRESHOLD=0.4       # Default: 0.4
```

### In code:

```python
from faultmaven.config.settings import get_settings

settings = get_settings()

# Access thresholds
max_clarifications = settings.thresholds.max_clarifications
# ↑ reads MAX_CLARIFICATIONS from .env or uses default (3)
```

## Setting Up Your Environment

### For Development (First Time)

```bash
# 1. Copy example to create your .env
cp .env.example .env

# 2. Edit .env with your values
nano .env  # or vim, code, etc.

# 3. Add your API keys
OPENAI_API_KEY=sk-...
FIREWORKS_API_KEY=...

# 4. Adjust thresholds (optional)
MAX_CLARIFICATIONS=5
PATTERN_CONFIDENCE_THRESHOLD=0.8

# 5. Start application
./run_faultmaven.sh
```

### For Production

```bash
# Use environment-specific files
.env.development
.env.staging
.env.production

# Load appropriate file
export ENV_FILE=.env.production
python -m faultmaven.server
```

Or use **environment variables directly** (Docker/K8s):

```yaml
# docker-compose.yml or k8s deployment
environment:
  - MAX_CLARIFICATIONS=5
  - PATTERN_CONFIDENCE_THRESHOLD=0.8
  - OPENAI_API_KEY=${OPENAI_API_KEY}  # From secrets
```

## Validation and Type Safety

### Type Validation

Pydantic validates types automatically:

```bash
# In .env
MAX_CLARIFICATIONS=abc  # Invalid!
```

```python
# Startup error:
ValidationError: 1 validation error for ConversationThresholds
max_clarifications
  value is not a valid integer (type=type_error.integer)
```

### Range Validation

Some fields have validators:

```python
class ConversationThresholds(BaseSettings):
    pattern_confidence_threshold: float = Field(
        default=0.7,
        env="PATTERN_CONFIDENCE_THRESHOLD",
        ge=0.0,  # ≥ 0.0
        le=1.0   # ≤ 1.0
    )
```

**If you set**: `PATTERN_CONFIDENCE_THRESHOLD=1.5`

**Error**: `value must be <= 1.0`

## Runtime Changes

### Can I change env vars at runtime?

**No** - Settings are loaded once at startup (singleton pattern).

**To apply changes**:
1. Edit `.env` file
2. Restart application

```bash
# Edit .env
vim .env

# Restart
./run_faultmaven.sh  # Stops and restarts
```

### For development hot-reload

Set `RELOAD=true` (the `local` preset and a zero-config start already do):

```bash
RELOAD=true python -m faultmaven.server
# Changes to .env require manual restart even with reload
```

## Troubleshooting

### Problem: Changes to .env not taking effect

**Cause**: Application not restarted

**Fix**:
```bash
# Stop application (Ctrl+C)
# Start again
./run_faultmaven.sh
```

### Problem: "Field required" error on startup

**Cause**: Required field missing from `.env`

**Fix**: Add the required variable to `.env`

```bash
# Error says: Field required for 'openai_api_key'
# Add to .env:
OPENAI_API_KEY=sk-your-key-here
```

### Problem: Using wrong defaults

**Cause**: Variable in `.env` but typo in name

**Example**:
```bash
# In .env
MAX_CLARIFICATONS=5  # Typo! Should be MAX_CLARIFICATIONS
```

**Result**: Uses default (3) instead of 5

**Fix**: Check spelling exactly matches `env="..."` in settings.py

### Problem: Boolean values not working

**Cause**: Pydantic is strict about boolean parsing

**Correct**:
```bash
ENABLE_INTELLIGENT_PROMPTS=true   # lowercase
ENABLE_INTELLIGENT_PROMPTS=false
ENABLE_INTELLIGENT_PROMPTS=1      # also works
ENABLE_INTELLIGENT_PROMPTS=0
```

**Incorrect**:
```bash
ENABLE_INTELLIGENT_PROMPTS=True   # Capital T
ENABLE_INTELLIGENT_PROMPTS=yes    # Not recognized
```

## Environment Variable Naming Convention

FaultMaven follows these conventions:

1. **ALL_CAPS** - Environment variable names
2. **SNAKE_CASE** - Multiple words separated by underscores
3. **Prefixes** - Group related variables
   - `OPENAI_*` - OpenAI settings
   - `REDIS_*` - Redis settings
   - `ENABLE_*` - Feature flags
   - `MAX_*` - Limit thresholds

## Security Best Practices

### ❌ Never commit .env

```bash
# .gitignore should have:
.env
.env.local
.env.*.local
```

### ✅ Use .env.example

```bash
# Document all variables (without secrets)
# Commit .env.example to repo
git add .env.example
git commit -m "Update env template"
```

### ✅ Rotate secrets regularly

```bash
# Change API keys periodically
# Update .env with new keys
# Restart application
```

### ✅ Use different secrets per environment

```bash
# Development
OPENAI_API_KEY=sk-dev-key-...

# Production
OPENAI_API_KEY=sk-prod-key-...  # Different key!
```

## Complete List of ConversationThresholds Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAX_CLARIFICATIONS` | int | 3 | Max clarification requests before escalation |
| `MAX_CONVERSATION_TURNS` | int | 20 | Max conversation turns to track |
| `MAX_CONVERSATION_TOKENS` | int | 4000 | Max tokens for conversation history |
| `CONTEXT_TOKEN_BUDGET` | int | 4000 | Total token budget (system+user+history) |
| `SYSTEM_PROMPT_MAX_TOKENS` | int | 500 | Max tokens for system prompt (warning) |
| `PATTERN_TEMPLATE_MAX_TOKENS` | int | 300 | Max tokens for pattern templates |
| `PATTERN_CONFIDENCE_THRESHOLD` | float | 0.7 | Trigger LLM when pattern confidence < this |
| `CONFIDENCE_OVERRIDE_THRESHOLD` | float | 0.4 | Force clarification when confidence < this |

## LLM Usage Ledger Retention

How long the LLM usage ledger keeps its rows (#640). Both are
`ObservabilitySettings` fields (`settings.observability`), and both are pruned
by the `llm_usage_retention` job; see
[LLM cost observability](../operations/monitoring/llm-cost-observability.md#the-usage-ledger)
for when that job runs.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `LLM_USAGE_DAILY_RETENTION_DAYS` | int (1–36500) | 400 | Days of `llm_usage_daily` rows kept; a row goes when its UTC `usage_date` is older than today minus this |
| `LLM_USAGE_TURN_RETENTION_DAYS` | int (1–36500) | 90 | Days of `llm_turn_spend` rows kept; a row goes when its `occurred_at` is older than now minus this |

## Database Pool and Timeouts

`DatabaseSettings` fields (`settings.database`). The pool and the PostgreSQL
bounds apply when `DATABASE_URL` names PostgreSQL (`postgresql+asyncpg://`);
SQLite opens a connection per checkout and none of them applies there. The
revocation-read deadline applies to the database revocation store on either
backend. The bounds in force are reported under `database_timeouts` on
`GET /api/v1/admin/config/status`.

Why the bounds exist (#2280): a PostgreSQL that stops answering once a
connection is open used to hold every request that read it. Past the command
timeout the client aborts the connection instead of sending a CancelRequest,
which a silent server never answers, and the pool discards it. Design and the
private asyncpg hook it relies on: `faultmaven/infrastructure/persistence/pg_connection.py`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DATABASE_POOL_SIZE` | int | 5 | Connections the pool keeps open |
| `DATABASE_MAX_OVERFLOW` | int | 10 | Connections opened beyond the pool size under load, closed when returned |
| `DATABASE_POOL_TIMEOUT` | int | 30 | Seconds a caller waits for a free connection once all `POOL_SIZE + MAX_OVERFLOW` are checked out |
| `DATABASE_POOL_RECYCLE` | int | 1800 | Seconds after which a pooled connection is replaced on its next checkout |
| `DATABASE_COMMAND_TIMEOUT_SECONDS` | float (>= 0) | 30.0 | Seconds one PostgreSQL command may take before the client aborts its connection; `0` disables it, for an operator command with a legitimately long statement |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | float (>= 0) | 10.0 | Seconds a new PostgreSQL connection may take to open; `0` disables it |
| `DATABASE_STATEMENT_TIMEOUT_MARGIN_SECONDS` | float (>= 0) | 2.0 | The server's `statement_timeout` is the command timeout less this, set per transaction, so a live but slow server cancels its own statement and the connection survives. Must be less than the command timeout (startup refuses otherwise); with the command timeout at `0` no `statement_timeout` is set. It overrides a `statement_timeout` configured on the role or database for that transaction |
| `DATABASE_CLIENT_CONNECTION_CHECK_INTERVAL_SECONDS` | float (>= 0) | 0.0 | PostgreSQL 14+ `client_connection_check_interval`, set per transaction: how often the server checks, during a long query, that the client is still there. `0` sets nothing; a server older than 14 refuses any other value on every transaction |
| `REVOCATION_READ_TIMEOUT_SECONDS` | float (>= 0) | 5.0 | Deadline on the token-revocation read every authenticated request makes. Past it the request fails closed with 503 `REVOCATION_STATE_UNKNOWN` (#1478); `0` disables it |

Bounds that follow, at the defaults, measured against a paused `postgres:16`:
an authenticated request against a silent PostgreSQL answers 503 in 5.0 s. A
read with no deadline of its own fails in 30 s when it draws a stale pooled
connection (its pre-ping meets the command timeout) and in 10 s when the
connection has to be reopened (the connect timeout); allow 40 s for both. The
16th concurrent caller can wait the 30 s pool timeout first. A turn whose commit meets a database that stops
answering mid-commit fails with the outcome unknown: a retry under the same
`Idempotency-Key` is answered from the turn receipt, an unkeyed retry may save
the turn twice. Alembic is outside these bounds: it runs on the sync driver in
a subprocess bounded at 60 s.

On PostgreSQL connections over TCP the engine also sets keepalive (30 s idle,
3 probes 10 s apart) and `TCP_USER_TIMEOUT` (30 s), so a connection whose
packets are lost while it sits idle in the pool is found dead. These are fixed,
not settings. They do nothing for a frozen server or behind a pooler such as
PgBouncer; the command timeout covers those.

## KB Incomplete-Ingest Sweep

`DatabaseSettings` fields (`settings.database`), read by the KB bootstrap
(`faultmaven/bootstrap/kb_incomplete_sweep.py`, #2394). An authored runbook's
`knowledge_items` row is incomplete from its INSERT until its vectors are
written; one left incomplete by a failed ingest is never listed or re-embedded,
and the sweep removes it with its vectors once it is older than the bound. The
web-startup bootstrap always deletes; `kb_seed` applies the two gate settings,
like the authored-orphan sweep, and `--dry-run` / `--no-dry-run` override the
dry run for one run. The counts are on the `BootstrapResult(...)` line
(`incomplete_ingests_*`) and in the `kb_seed` result's `incomplete_ingests`.

The max age is also the youngest chunk any KB collector may delete, the
generation sweep's included (below).

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `KB_INCOMPLETE_INGEST_MAX_AGE_HOURS` | int (>= 1) | 24 | Hours after its `created_at` before an incomplete row is removed. A younger one may be an ingest still running; a slower ingest whose row is removed fails, and its vectors stay unadmitted. Also the age of the youngest chunk any KB collector may delete (the reconcile's no-row and unpublished built-in chunks, the generation sweep): a younger one may be a pack ingest's or an update's, written before its row |
| `KB_INCOMPLETE_INGEST_SWEEP_ENABLED` | bool | false | `kb_seed` deletes only when this is true and the run is not a dry run |
| `KB_INCOMPLETE_INGEST_SWEEP_DRY_RUN` | bool | true | `kb_seed` reports the rows an armed run would delete and deletes nothing |

## KB Generation Sweep

`DatabaseSettings` fields (`settings.database`), read by `kb_seed`
(`faultmaven/bootstrap/kb_generation_sweep.py`, #2388). Every KB chunk carries
its row's `vector_generation`, and a door serves only the row's generation. The
writer that switches a row's generation (a content update, a pack replacement)
deletes the generation it replaced once its commit has landed, best effort.
This sweep removes the rest once older than `KB_INCOMPLETE_INGEST_MAX_AGE_HOURS`:
a writer's delete that failed, a failed write's chunks, the generation a racing
update overwrote, and every chunk written before migration 022. The standalone
web startup deletes only when the store is a local `PersistentClient`, and only
reports under an external `CHROMADB_URL`; these settings do not change that.
`kb_seed` deletes only when armed here, whatever the incomplete-ingest sweep's
settings say, and `--dry-run` / `--no-dry-run` override the dry run for one run.
Arm it only with one ChromaDB per database. The counts are
`generation_sweep_*` on the `BootstrapResult(...)` line and the `kb_seed`
result's `generation_sweep`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `KB_GENERATION_SWEEP_ENABLED` | bool | false | `kb_seed` deletes superseded chunks only when this is true and the run is not a dry run |
| `KB_GENERATION_SWEEP_DRY_RUN` | bool | true | `kb_seed` reports the superseded chunks an armed run would delete and deletes nothing |

## API Server Keep-Alive

`ServerSettings.timeout_keep_alive` (`settings.server`), passed to `uvicorn.run`
(which does not read `UVICORN_TIMEOUT_KEEP_ALIVE`).

How the server is launched, and what `run_server` fixes: [Logging policy, launching the server](../operations/monitoring/logging-policy.md#launching-the-server).

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `TIMEOUT_KEEP_ALIVE` | int (>= 1) | 75 | Seconds uvicorn holds an idle keep-alive connection. Keep it above the fronting proxy's upstream keep-alive (ingress-nginx `upstream-keepalive-timeout`, 60 s) and every client's idle expiry (slack-agent httpx `keepalive_expiry`, 5 s), so a POST is never sent on a connection the API just closed |

## Sessions

`SessionSettings` (`settings.session`). A session lives in Redis (FakeRedis on
standalone) under one clock: the session store is built with
`SESSION_TIMEOUT_MINUTES` and every session write (creation, resumption, a
heartbeat, an update) sets it as the key's TTL, so a session lapses that long
after its last write. Its `expires_at` is that instant, and
`POST /api/v1/sessions` answers it with `timeout_minutes`. There is no separate
inactivity timeout, no absolute lifetime and no server-side heartbeat interval:
clients pace their own heartbeats (the Copilot pings every 10 minutes,
`VITE_HEARTBEAT_INTERVAL_MS`), so keep the timeout at least twice their
interval. `GET /admin/config/status` reports the TTL the running store applies
as `session_ttl_seconds`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `SESSION_TIMEOUT_MINUTES` | int (1–1440) | 30 | Minutes a session lives past its last write. A preset fills it in when it is unset: `local` sets 60, `standard` and `enterprise` set 30. A preset applies when `CONFIG_PRESET` names it, and `local` also applies by itself to a deployment that configures nothing (no LLM provider, Redis or ChromaDB server) |
| `SESSION_MAX_SESSIONS_PER_USER` | int (>= 1) | 10 | Live sessions one user may hold; a new session evicts the least recently active beyond it |

## Knowledge Base Indexing

`KnowledgeSettings.index_request_window_seconds` (`settings.knowledge`). KB
upload, update (`PUT /knowledge/documents/{id}` with `content`) and draft
verify index the document inside the request (#2055), so behind a proxy whose
read timeout is shorter than the indexing a document needs, the client is cut
off after all the work. With a window set, such a document is refused before
anything is written: **413** `KNOWLEDGE_DOCUMENT_TOO_LARGE` when it alone
cannot fit, **503** `KNOWLEDGE_INDEXING_BUSY` + `Retry-After` when the indexing
already queued on the server leaves no room for it, and `verify-batch` returns
the drafts the rest of its window cannot fit as `deferred`. The estimate is the
document's BGE-M3 token count at a measured 25 core-ms a token, divided by the
CPU quota (at most 4 cores). KB indexing encodes run one at a time per
container. A document that fits only once a cold model has loaded starts the
load and is answered 503 until it has (after a failed load, with no load in
progress, it is answered as for an unavailable embedder and no load starts);
while an encode is past its time bound every document is answered 503. The
window, the queue, the running encode's age and the host lock's state are
reported under `knowledge_indexing` on `GET /api/v1/admin/config/status`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `INDEX_REQUEST_WINDOW_SECONDS` | int (0, or 60–3600) | 0 | Seconds a KB write may spend indexing its document. `0` refuses nothing up front; the embedding guard (180 s for a model load plus twice the estimate) still bounds the embed. Set it below the fronting proxy's read timeout, less the rest of the request: for example 600 under an ingress `proxy-read-timeout` of 660 s |

## LLM Trace Content

`ObservabilitySettings.opik_capture_content` (`settings.observability`). Spans
carry model, provider, usage, timing, their owner tags and a failed call's
error class, and no prompt, response or exception text (#2616 K0).

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `OPIK_CAPTURE_CONTENT` | bool | false | Spans carry prompts and responses. `true` stops the API, every `fm-*` command and every job at startup until a retention bound for the trace store is verified (#2616 K3). `GET /admin/config/status` reports it as `trace_backend.capture_content` |

## LLM Trace SDK (Opik)

The Opik SDK reads these straight from the environment, so they are not
`settings.py` fields. `faultmaven/infrastructure/observability/opik_sdk.py`, the
one module that imports the SDK, loads `.env` into the environment before it
does, so a value in either reaches the SDK; the environment wins over `.env`.
Each is read once, when the process starts.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `OPIK_SENTRY_ENABLE` | bool | false | The SDK's crash reporting to Comet's Sentry. The SDK's own default is on; FaultMaven sets `false` unless you set it (#2585). `GET /admin/config/status` reports the effect as `trace_backend.sentry_reporting` |
| `OPIK_TRACK_DISABLE` | bool | false | Suppresses every span while a backend stays configured (`docs/operations/monitoring/configuration.md`) |

## TLS Root Set

Every TLS client verifies the peer against one set: the OS CA bundle, plus this
setting's certificates ([Principle 13.4](../architecture/core-architecture/architectural-design-principles.md#13-one-owner-per-fact)).
It is read from the environment or `.env`, in any letter case, once, at the
start of the API, each `fm-*` command, the jobs runner, the migration process
and the demo seeder, so a change takes effect at the next start.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `TLS_EXTRA_CA_FILE` | path | unset | A PEM file of CA certificates added to the OS bundle, for a private CA (a Postgres or S3 endpoint, an LLM gateway). Only its `CERTIFICATE` blocks are used; a file with none, or with an `X509` or `TRUSTED CERTIFICATE` block, refuses the start. A relative path is read from the working directory; `data/extra-ca.pem` works in both run modes |

FaultMaven sets `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE`, `AWS_CA_BUNDLE`, `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` and
`PGSSLROOTCERT` itself. Unset, the file variables name the OS bundle file; when
no OS bundle file loads and the bundle is built from OpenSSL's certificate
directory (or, on native Windows, from the certificate store), they name the
written file below even with the setting unset. Set, they name `<tmp>/faultmaven-tls-<uid>/<sha256>.pem` (under `TMPDIR`, in a
directory only that user can write): the OS bundle, then the setting's
certificates; one file per content, never deleted. On native Windows the OS
bundle is the certificate store's server-auth roots, so the file is always
written. A variable already set is accepted, in either case, when it names the
file FaultMaven would name, or holds the bytes it would write, or holds only
`CERTIFICATE` blocks with exactly the set's certificates; except the OS bundle
file itself, OpenSSL must then load exactly the set from it (as many
certificates, each CA among the set's). An already-set `SSL_CERT_DIR`, read as a
`:`-separated list (`;` on Windows), is accepted when its hashed entries hold no
certificate, or only `CERTIFICATE` blocks with exactly the set's, each entry
loading to exactly its own. Anything else refuses the start: remove it, or put
its CA in `TLS_EXTRA_CA_FILE`. A refused file in a `faultmaven-tls-<uid>`
directory is a set file from an earlier FaultMaven start that is missing,
altered or not this start's set: restart that process, or unset the
variable.

A host that ages `/tmp` (systemd-tmpfiles' 10-day rule, for example) can remove
the written file under a long-idle process. The next start, command or child
that applies the set re-writes it. To avoid the gap, set `TMPDIR` to a directory
that is not aged, or restart the process. With no usable temporary directory,
the start is refused naming `TMPDIR`.

With `PGSSLROOTCERT` set, Postgres `sslmode=require` (SQLAlchemy's
`?ssl=require`) verifies the server's chain. Under libpq's default
`sslmode=prefer` (the migration process), libpq verifies against the set and,
when that fails, connects in plaintext: put the server's CA in
`TLS_EXTRA_CA_FILE`, or set `sslmode=require` or `verify-full` in
`DATABASE_URL` to fail closed. The API's asyncpg path under `prefer` is
unaffected.

## Related Documentation

- [Context Management Guide](./CONTEXT_MANAGEMENT.md)
- [Token Estimation Guide](./TOKEN_ESTIMATION.md)
- [Configuration Reference](../architecture/SYSTEM_ARCHITECTURE.md)

## Quick Reference: ConversationThresholds Variables

All environment variables for the ConversationThresholds system:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAX_CLARIFICATIONS` | int | 3 | Max clarification requests before escalation |
| `MAX_CONVERSATION_TURNS` | int | 20 | Max conversation turns to track |
| `MAX_CONVERSATION_TOKENS` | int | 4000 | Max tokens for conversation history |
| `CONTEXT_TOKEN_BUDGET` | int | 4000 | Total token budget (system+user+history) |
| `SYSTEM_PROMPT_MAX_TOKENS` | int | 500 | Max tokens for system prompt (warning) |
| `PATTERN_TEMPLATE_MAX_TOKENS` | int | 300 | Max tokens for pattern templates |
| `PATTERN_CONFIDENCE_THRESHOLD` | float | 0.7 | Trigger LLM when pattern confidence < this |
| `CONFIDENCE_OVERRIDE_THRESHOLD` | float | 0.4 | Force clarification when confidence < this |

### Status

- ✅ All variables added to `.env` with defaults
- ✅ All variables added to `.env.example` with documentation
- ✅ All variables defined in `ConversationThresholds` class (settings.py)
- ✅ Duplicates removed from `FeatureSettings`
- ✅ Production ready - no migration needed (defaults work)

### Configuration in Code

```python
from faultmaven.config.settings import get_settings

settings = get_settings()

# Access thresholds
max_clarifications = settings.thresholds.max_clarifications  # 3
pattern_confidence = settings.thresholds.pattern_confidence_threshold  # 0.7
```

### Files Modified

1. **`faultmaven/config/settings.py`** - Added ConversationThresholds class
2. **`.env`** - Added all threshold variables with defaults
3. **`.env.example`** - Added all threshold variables with documentation

See [Infrastructure Improvements](../../releases/INFRASTRUCTURE_IMPROVEMENTS_2025-10-04.md) for full details.
