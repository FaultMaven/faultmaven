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
- Uvicorn's plaintext access log is **disabled** (`access_log=False` in
  `main.py`). The single access log is the structured request
  start/completion pair emitted by `LoggingMiddleware` (method, path,
  status_code, duration_seconds, correlation_id, request_id).

## Redaction
- Strip or hash PII/session identifiers; avoid storing raw user content in logs
