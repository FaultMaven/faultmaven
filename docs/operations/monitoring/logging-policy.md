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
- Knowledge retrieval (kb_qa, case_evidence_qa): DEBUG (search latency, chunk count), WARN (empty results on 2nd attempt), ERROR (vector store failure)
- Auth / JWT: DEBUG (token issued), WARN (refresh/revoke), ERROR (verification failure)
- Container/DI: INFO (wiring), WARN (degraded init), ERROR (init failure)

## Sampling
- Decision records: target 5–10% sampling if volume high; otherwise 100% during hardening

## Structure
- JSON logs with fields: timestamp, level, component, session_id, case_id, event, payload
- `request_id` (from the `X-Request-ID` header, or generated) is bound into
  structlog contextvars by `RequestIdMiddleware` for the duration of each
  request — every log line emitted while handling a request carries it.

## Access logs

Uvicorn's plaintext access log is **disabled** (`access_log=False` in
`main.py`). The single access log is the structured request start/completion
pair emitted by `LoggingMiddleware`, plus a `Request failed` ERROR in place of
the completion when the route raises.

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
| `session_id`, `case_id` | all | Extracted from the header / query / JSON body when present |
| `user_id` | all | Who the request acts as — see below |
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
- `user_id` falls back to a lookup from `session_id` where the binding names no
  subject — the single-tenant arm, which deliberately never reads the token.
  That lookup is all this log had before, and a bearer-authenticated request
  carries no session id, which is why API calls used to log `user_id: null`.

## Redaction
- Strip or hash PII/session identifiers; avoid storing raw user content in logs
