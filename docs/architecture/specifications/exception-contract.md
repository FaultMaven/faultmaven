# Service Exception Contract

## Status

**Current** — Reflects the shared exception hierarchy in
[`faultmaven/exceptions.py`](../../../faultmaven/exceptions.py) and the
global handlers in
[`faultmaven/api/exception_handlers.py`](../../../faultmaven/api/exception_handlers.py).
The auth module conforms; per-module migration of other modules
(case, knowledge, agent) is in progress under Item 3 of the
2026-05-20 investigation-pipeline-followups handoff series.

## Purpose

Defines the formal contract between FaultMaven's service layer and
API layer for error propagation:

- Which exception types services may raise.
- How each type maps to an HTTP status code and response body.
- How routes should structure their `try/except` blocks so that
  typed exceptions reach the global handlers instead of being
  re-wrapped as 500.
- The legacy anti-pattern this contract replaces.

This document is the implementation reference. The principle that
backs it lives in
[architectural-design-principles.md §6](../core-architecture/architectural-design-principles.md#6-errors-as-domain-concepts).

## Exception Types and HTTP Mapping

| Service-layer exception | HTTP status | When to raise |
|-------------------------|-------------|---------------|
| `ValidationException` | 422 Unprocessable Entity | Client input is malformed (bad format, missing required field, fails business validation). |
| `ConflictError` | 409 Conflict | Resource state conflict (duplicate username, double-close, attempting an operation incompatible with current state). |
| `NotFoundError` | 404 Not Found | Resource lookup miss (case/session/user does not exist). |
| `PermissionDeniedException` | 403 Forbidden | Caller is authenticated but lacks permission for the operation. |
| `ServiceException` | 500 Internal Server Error | Genuine server failure that the client cannot resolve (database error, wrapped infrastructure failure). |

All five inherit from `FaultMavenException` (base class). The
`ServiceError` subclass groups `NotFoundError` / `ConflictError` /
`AuthenticationError` / `AuthorizationError` as a related family.

## Structured Metadata

`NotFoundError` and `ConflictError` accept structured fields so the
JSON response body carries actionable detail without forcing the
client to parse human-readable messages:

```python
raise NotFoundError(
    resource_type="user",
    resource_id=user_id,
    message=f"User {user_id} not found",  # optional override
)

raise ConflictError(
    f"User with username '{username}' already exists",
    resource_type="user",
    resource_id=username,
    conflict_reason="duplicate_username",
)
```

The handlers surface these fields in the response (`resource_type`,
`resource_id`, `conflict_reason`) so a frontend can distinguish
`duplicate_username` from `duplicate_email` without regex on a
free-text message.

## Service-Layer Usage Example

```python
# faultmaven/infrastructure/auth/database_user_store.py
from faultmaven.exceptions import ConflictError, NotFoundError, ValidationException

async def create_user(self, username: str, email: str | None = None) -> DevUser:
    if not self._validate_username(username):
        raise ValidationException(f"Invalid username format: {username}")
    if await self.get_user_by_username(username):
        raise ConflictError(
            f"User with username '{username}' already exists",
            resource_type="user",
            resource_id=username,
            conflict_reason="duplicate_username",
        )
    # ... rest of create flow

async def update_user(self, user: DevUser) -> DevUser:
    existing = await self.user_repository.get(user.user_id)
    if not existing:
        raise NotFoundError(
            resource_type="user",
            resource_id=user.user_id,
            message=f"User {user.user_id} not found",
        )
    # ... rest of update flow
```

## API Layer Translation

Global handlers in
[`api/exception_handlers.py`](../../../faultmaven/api/exception_handlers.py)
are registered once at app startup. Routes do not need to catch
these exceptions individually.

```python
@app.exception_handler(ValidationException)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={"error": "Validation Error", "detail": str(exc)},
    )

@app.exception_handler(ConflictError)
async def conflict_exception_handler(request, exc):
    return JSONResponse(
        status_code=409,
        content={
            "error": "Conflict",
            "detail": str(exc),
            "resource_type": exc.resource_type,
            "resource_id": exc.resource_id,
            "conflict_reason": exc.conflict_reason,
        },
    )

@app.exception_handler(NotFoundError)
async def not_found_handler(request, exc):
    return JSONResponse(
        status_code=404,
        content={
            "error": "Not Found",
            "detail": str(exc),
            "resource_type": exc.resource_type,
            "resource_id": exc.resource_id,
        },
    )
```

## Route Pattern

The recommended route structure when an outer `try/except` is
needed (typically for logging or to map otherwise-unforeseen errors
to 500):

```python
@router.post("/login")
async def local_login(request_body: ...):
    try:
        # ... call into services that may raise typed exceptions
        return await user_store.get_user_by_username(...)
    except HTTPException:
        raise  # FastAPI HTTPExceptions pass through unchanged
    except FaultMavenException:
        # Typed service exceptions (ValidationException, ConflictError,
        # NotFoundError, PermissionDeniedException, ServiceException)
        # propagate to the global handlers, which map them to
        # 422/409/404/403/500 respectively.
        raise
    except Exception as e:
        # Genuine unforeseen failure → 500.
        logger.error(f"Login failed: {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Login failed due to an internal error.",
        )
```

The `except FaultMavenException: raise` block is **load-bearing**
when a blanket `except Exception` exists below it. Without it, the
blanket would swallow typed exceptions before FastAPI's global
dispatcher saw them and the response would be 500 regardless of the
intended status.

If a route does not need a blanket `except Exception`, omit both —
the typed exceptions propagate to the global handlers without any
route-side `try/except` at all.

## Legacy Anti-Pattern (Pattern B)

The codebase is migrating away from this shape:

```python
# DON'T DO THIS
# Service layer raises ValueError for everything
async def create_user(...) -> User:
    if invalid_format:
        raise ValueError("Invalid username format")     # actually a 422
    if duplicate:
        raise ValueError("Username already exists")     # actually a 409

# Route catches ValueError and returns 400 indiscriminately
@router.post("/register")
async def register(...):
    try:
        return await user_store.create_user(...)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

This pattern collapses three semantically distinct errors
(validation / conflict / not-found) into HTTP 400, and risks a 500
leak whenever a route forgets the `except ValueError` block. It is
being replaced module-by-module under Item 3 of the 2026-05-20
investigation-pipeline-followups work; the auth module landed first
(PR #331) as the migration template.

## Cross-References

- Principle: [architectural-design-principles.md §6 Errors as Domain Concepts](../core-architecture/architectural-design-principles.md#6-errors-as-domain-concepts)
- Implementation:
  [`faultmaven/exceptions.py`](../../../faultmaven/exceptions.py),
  [`faultmaven/api/exception_handlers.py`](../../../faultmaven/api/exception_handlers.py)
- Handler tests: [`tests/unit/api/test_exception_handlers.py`](../../../tests/unit/api/test_exception_handlers.py)
- Endpoint-specific application: [Security IAM design — Error Responses](../security/iam-design.md#error-responses-login--register)
