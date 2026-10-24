# Logging Policy (Modular Monolith)

## Default levels
- Production: INFO
- Non-prod: INFO with optional DEBUG via FAULTMAVEN_DEBUG=1

## Error Logging Standards

**REQUIRED**: All error logging must include stack traces for debugging.

### Mandatory Practices

- **Use `exc_info=True`** for all `logger.error()`, `logger.critical()` calls within exception handlers
- **Use `logger.exception()`** inside `except` blocks (automatically adds `exc_info=True`)
- **Include exception type and message** in the log message for searchability

### Examples

**✅ GOOD - Using logger.exception():**

```python
try:
    result = await dangerous_operation()
except Exception as e:
    logger.exception(f"Operation failed for {entity_id}: {e}")
    raise
```

**✅ GOOD - Using exc_info=True:**

```python
try:
    result = await dangerous_operation()
except SpecificError as e:
    logger.error(f"Specific error occurred: {type(e).__name__}: {e}", exc_info=True)
    # Handle gracefully
```

**❌ BAD - Missing stack trace:**

```python
except Exception as e:
    logger.error(f"Operation failed: {e}")  # Missing exc_info=True!
```

**Exception**: `exc_info=True` may be omitted for expected/handled errors where stack traces add no debugging value (e.g., validation errors, user input errors).

## Component guidance
- API middleware: DEBUG (request/response trace), WARN (rate limit / dedup / circuit), ERROR (middleware failure)
- Investigation (MilestoneEngine): DEBUG (milestone evaluation, turn context), INFO (stage transitions), WARN (stagnation/repair), ERROR (execution failure)
- LLM Router: DEBUG (provider selection), WARN (fallback triggered, retry), ERROR (all providers exhausted)
- LLM Providers: DEBUG (request/response sizes), WARN (rate limit, timeout), ERROR (API error)
- Agent tools: DEBUG (tool start/finish, inputs), WARN (partial results, coverage gap), ERROR (tool failure)
- Repositories: DEBUG (query), WARN (slow query), ERROR (persistence failure)
- Knowledge retrieval (kb_qa, case_evidence_search): DEBUG (search latency, chunk count), WARN (empty results on 2nd attempt), ERROR (vector store failure)
- Auth / JWT: DEBUG (token issued), WARN (refresh/revoke), ERROR (verification failure)
- Container/DI: INFO (wiring), WARN (degraded init), ERROR (init failure)

## Sampling
- Decision records: target 5–10% sampling if volume high; otherwise 100% during hardening

## Structure
- JSON logs with fields: timestamp, level, component, claimed_session_id, case_id, event, payload
- `request_id` (from the `X-Request-ID` header, or generated) is bound into
  structlog contextvars by `RequestIdMiddleware` for the duration of each
  request — every log line emitted while handling a request carries it.

## Access logs

Uvicorn's plaintext access log is **disabled** (`access_log=False` in
`server.py`). The single access log is the structured request start/completion
pair emitted by `LoggingMiddleware`, plus a `Request failed` ERROR in place of
the completion when the route raises.

### Launching the server

`faultmaven.server.run_server(app)` is the one place the API's uvicorn options
live: host, port, reload, workers (only above 1) and `timeout_keep_alive` from
`settings.server`, `log_level="info"`, `access_log=False` and `log_config=None`.
`log_config=None` is what keeps `uvicorn.error` inside the redaction boundary:
uvicorn's default config gives the `uvicorn` logger its own handler with
`propagate=False`, so its records, whose tracebacks embed client exception
messages and request URLs, would stop there and never reach the root logger's
redacting renderer. The uvicorn CLI can express `--no-access-log` but not this:
`--log-config` takes a file, and giving it none leaves uvicorn's default config.

So every launch goes through `run_server`: the image runs
`python -m faultmaven.server`, and `scripts/faultmaven-dev.sh` runs the same
command with `HOST`, `PORT`, `RELOAD`, `WORKERS` and `TIMEOUT_KEEP_ALIVE` in the
environment. `run_server` takes host, port and reload from settings, so a launch
sets `HOST`, `PORT` and `RELOAD` itself: a deployment that configures no LLM
provider at all gets the zero-config `local` preset, `127.0.0.1:8000` with
reload. "Configures a provider" is answered from the provider registry (any
registered provider's API key, or `LOCAL_LLM_URL` for the local one), so a
Gemini-only `.env` is not zero-config (#1984).

`app` is a `module:attr` import string (default `faultmaven.main:app`), never an
object: uvicorn refuses an object when `workers > 1` or `reload` is set, and
`run_server` raises `ValueError` for anything that is not an import string.

Every launch executes `faultmaven/main.py` exactly once (#1985). The launcher is
its own module, `faultmaven/server.py`, and imports neither `faultmaven.main` nor
anything that does: uvicorn imports the `"faultmaven.main:app"` string, once, in
the process that serves requests. `python -m faultmaven.main` is not a launch
path: it ran `main.py` as `__main__` and then again for uvicorn's import, so every
import-time side effect ran twice (duplicate import-time logs,
`starlette.requests.Request.form` wrapped twice, Prometheus scrape hooks
registered twice) and the served app was not the `__main__` one. It now refuses:
`main.py` opens with a `__main__` guard, above the `.env` load and every import,
that raises `SystemExit` naming `faultmaven.server`, so the command exits 1 with
one line and no import-time work (a stale `command:` fails loudly instead of
exiting 0 without serving).
`tests/unit/test_run_server_launch_paths.py` runs the launcher in a child process
with `uvicorn.run` replaced by a stub that imports the app string, as uvicorn
does, and asserts `main.py` executed once.

The Cloud image is to launch through `run_server("faultmaven_cloud.main:app")`
once faultmaven-cloud#25 bumps its core pin; it does not yet. It serves on 8000,
so it must set `ENV PORT=8000 HOST=0.0.0.0 RELOAD=false`. `python -m
faultmaven.server` always serves the core app, so the Cloud image calls the
function instead:
`python -c "from faultmaven.server import run_server; run_server('faultmaven_cloud.main:app')"`.
That command imports `faultmaven.server`, not `faultmaven_cloud.main`, so the
Cloud module executes once, when uvicorn imports the string. It must not use
`python -m faultmaven_cloud.main`: that module mounts `CLOUD_ROUTERS` at import,
and a `__main__` run followed by uvicorn importing the string mounts them twice.

### Fields

| Field | On | Meaning |
|---|---|---|
| `method`, `path` | all | The request line. `path` is the raw path; the *route template* is what the Prometheus `endpoint` label carries |
| `status_code` | completed | |
| `duration_seconds` | completed, failed | |
| `error`, `error_type` | failed | |
| `correlation_id` | all | Ties the pair together, and to every line logged while handling the request |
| `client_ip` | started | The **resolved** client — the same address the rate limiters enforce on, not the socket peer |
| `query_param_names` | started | Parameter **names** only. Values never reach a record: the SSO callback carries the IdP authorization code as a query parameter |
| `claimed_session_id` | all | The session id the **caller** sent, off the header / query / JSON body. Recorded for correlation, never resolved to an account, and named so no reader takes it as established fact (fm#1461) |
| `case_id` | all | Extracted from the header / query / JSON body when present. Caller-asserted too, and deliberately NOT renamed: it says what the request was about, not who made it |
| `user_id` | all | Who the request acts as — the **verified** subject, or nothing. See below |
| `enterprise_id` | completed, failed | The enterprise the request was bound to: the isolation boundary (ADR-017) |
| `organization_id` | completed, failed | The billing organization, or `null` for an account in none. Attribution only, never a visibility predicate |

### How a line is attributed

`bind_request_enterprise_context` is the one place per request that verifies the
token, and it publishes what it bound on `request.state` as a `RequestPrincipal`
(`api/middleware/principal.py`). The completion and failure lines read it.
Identifiers only — the binder holds the bearer token at that moment and never
puts it, or anything else secret, on the record.

`LoggingMiddleware` is a `BaseHTTPMiddleware`, so Starlette runs the route in a
separate task and the tenancy contextvars do not reach it; `request.state` is
backed by the ASGI scope, which both tasks share. This is why the **started**
line carries no `enterprise_id`: it is emitted before the route, and therefore
before the binding exists.

Two fallbacks are worth knowing when reading a line:

- **No `enterprise_id` at all** means no binder ran — the request matched no
  route, or a middleware answered above the router.
- An **empty** `enterprise_id` is the non-tenant sentinel: an unauthenticated or
  invalid-token request under `TENANT_PROVIDER=multi`, which matches no
  enterprise's rows.
- `user_id` has exactly one source: the published principal. It is **never**
  derived from a session id. It used to fall back to the owner of
  `claimed_session_id` where the binding named no subject — so a request
  carrying no credential and somebody else's session id was recorded against
  that somebody, and the account it named was the one the caller chose
  (fm#1461). There are three states to read:
  - a **name** — a verified subject;
  - `user_id: null` with `[user: anonymous]` in the message — a binder ran and
    verified nobody. The unauthenticated arm, and the single-tenant arm, which
    deliberately never reads the token, so standalone lines say `anonymous`;
  - `user_id: null` with **no** `[user: …]` in the message — no binder ran, so
    nobody has looked. Not the same fact as `anonymous`.

  `user_id` also no longer appears on records emitted *during* the request by
  other layers: it is knowable only after the binder runs, which is after the
  request context is built. Join those to the completion line by
  `correlation_id`, which every record carries.

## Redaction
- Strip or hash PII/session identifiers; avoid storing raw user content in logs
