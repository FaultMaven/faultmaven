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
- JSON logs with fields: timestamp, level, component, claimed_session_id, case_id, event, payload
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
