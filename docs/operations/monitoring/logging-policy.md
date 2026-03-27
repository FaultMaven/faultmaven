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
- Decision records: INFO (structured, sampled if needed; no PII)
- Gateway: DEBUG (clarity), WARN (absurd), ERROR (exceptions)
- Router: DEBUG (selection and scores), WARN (circuit/backoff), ERROR (selection failure)
- Skills: DEBUG (start/finish, budget skips), WARN (partial results), ERROR (exceptions)
- Confidence: DEBUG (final score/band), avoid full feature vectors in prod
- LoopGuard: DEBUG (signals), WARN (recovery), INFO (escalation)
- Retrieval: DEBUG (latency, count), WARN (adapter timeout), ERROR (adapter failure)
- Policy: INFO (confirmation required), WARN (deny), ERROR (engine error)
- Container/DI: INFO (wiring), WARN (degraded), ERROR (init failures)

## Sampling
- Decision records: target 5–10% sampling if volume high; otherwise 100% during hardening

## Structure
- JSON logs with fields: timestamp, level, component, session_id, case_id, event, payload

## Redaction
- Strip or hash PII/session identifiers; avoid storing raw user content in logs
