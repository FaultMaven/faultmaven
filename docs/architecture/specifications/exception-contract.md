# Service Exception Contract

## Status

**Current** — Reflects the shared exception hierarchy in
[`faultmaven/exceptions.py`](../../../faultmaven/exceptions.py) and the
global handlers in
[`faultmaven/api/exception_handlers.py`](../../../faultmaven/api/exception_handlers.py).
Migration status under Item 3 of the 2026-05-20
investigation-pipeline-followups handoff series:

- **Conforming**: auth (PR #331), case/routes
  (PR #335), agent/routes, knowledge/verify_draft (PR #334),
  knowledge/routes (`approve_suggestion` and `remediate_pii`,
  PR #337).
- **Out of scope**: knowledge/conversion_routes keeps its own
  `ConversionRejectedError`/`ConversionErrorCode` contract mapping to
  413/415/422/503; the only Pattern-B remnant in that file (an LLM
  JSON-parse `except ValueError`) was plugged at the service layer in
  PR #336 so the conversion contract is now fully typed end-to-end.

Item 3 is complete at the route-layer scope. A separate, broader
sweep would still be needed to convert programmer-error
`raise ValueError` sites in service-layer code paths that aren't
caught by routes (they currently leak as 500 via blanket
`except Exception` blocks) — that's a follow-up initiative, not part
of Item 3.

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
| `AuthorizationError` | 403 Forbidden | Caller is authenticated but lacks permission for the operation. |
| `ServiceException` | 500 Internal Server Error | Genuine server failure that the client cannot resolve (database error, wrapped infrastructure failure). |

All five inherit from `FaultMavenException` (base class). The
`ServiceError` subclass groups `NotFoundError` / `ConflictError` /
`AuthenticationError` / `AuthorizationError` as a related family —
this is the family the global handlers dispatch by type.

> **Note.** A separate `PermissionDeniedException` exists in
> `exceptions.py` but inherits from `FaultMavenException` directly,
> *not* from `ServiceError`. It has **no global handler** and will
> fall through to FastAPI's default 500 if raised. New code should
> raise `AuthorizationError` for "caller lacks permission" instead.

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

## LLM Provider Failures (turn endpoints)

An LLM turn can fail deep in a provider call, and reaches the route as a
`ServiceException` on one of two paths. On the **direct** path the raw
`LLMException` is chained (`raise ServiceException(...) from e`), so its
typed `status_code` / `retryable` lives on the `__cause__` chain. On the
**retry-loop** path (`LLMErrorHandler.with_retry` → `MilestoneEngineError`)
the provider exception is converted to a semantic `error_code` and
re-raised without a `__cause__` link — so no provider status is reachable,
only that code (threaded onto the wrapper's `details["error_code"]`; this
is `MilestoneEngineError`'s documented cross-layer signal). Route handlers
must classify off both typed signals via
`llm_service_error_http_exception(exc, correlation_id)`
(`api/exception_handlers.py`), never by substring-matching the message.
Message matching silently mis-routed real failures to a bare 500: a
provider that raised `"…timed out…"` (which does not contain the
`"timeout"` substring the old handler looked for), a Gemini `400`, and a
schema-parse `ValidationError` all fell through — a transient/upstream
provider condition presented to the user as a FaultMaven bug.

| Signal | HTTP | `x-error-code` | Retry-After |
|--------|------|----------------|-------------|
| billing / quota exhausted (`QUOTA_EXHAUSTED`) | 402 | `QUOTA_EXHAUSTED` | — |
| provider status 429 | 429 | `RATE_LIMIT_EXCEEDED` | 60 |
| provider status 504 (incl. all provider timeouts) | 504 | `LLM_TIMEOUT` | 30 |
| provider status 503 | 503 | `LLM_OVER_CAPACITY` | 60 |
| provider status 5xx (other) | 503 | `LLM_PROVIDER_UNAVAILABLE` | 60 |
| provider status 4xx (other, e.g. Gemini 400) | 502 | `LLM_PROVIDER_ERROR` | — |
| `LLMException`, no status, `retryable` | 503 | `LLM_PROVIDER_UNAVAILABLE` | 30 |
| `LLMException`, no status, terminal | 502 | `LLM_PROVIDER_ERROR` | — |
| engine `RETRY_EXHAUSTED` / `PROVIDER_CIRCUIT_OPEN` / `TOKEN_LIMIT` / `UNKNOWN_ERROR` | 503 | `LLM_PROVIDER_UNAVAILABLE` | 30 |
| engine `MODEL_NOT_FOUND` / `AUTH_FAILED` | 502 | `LLM_PROVIDER_ERROR` | — |
| direct schema-parse failure (`ValidationError` / `JSONDecodeError`) | 503 | `LLM_INVALID_RESPONSE` | 30 |
| anything else | 500 | `SERVICE_ERROR` | 10 |

The raw provider status (direct path) is more specific than a threaded
engine code and takes precedence when both are present. A 4xx other than
429 means the provider rejected *this request* — the identical request
retried fails identically, so no `Retry-After`. A schema-parse failure
(direct, or the engine's `UNKNOWN_ERROR` from the retry loop) is retried
because a BEST_EFFORT model may emit valid JSON on the next attempt. Every
provider stamps `status_code=504` on a client/read timeout
(`asyncio.TimeoutError`) so the contract classifies timeouts by typed
status, not the wording of the message.

The same rule holds one layer up. `BaseExternalClient.call_external` bounds
each attempt with its own deadline and has no provider status to stamp, so it
raises `ExternalCallTimeout`, which declares `retryable = True` on the
exception. It previously raised a bare `TimeoutError("… timed out after
30.0s")` and the engine's retry ladder decided by substring — against a list
containing `"timeout"`, which is not a substring of `"timed out"` — so a hung
provider got zero retries while a provider's own 504 got three (#1287). The
ladder now reads a declared `retryable` flag anywhere on the `__cause__`
chain, then `TimeoutError` by type (a bare `asyncio.TimeoutError` stringifies
to the EMPTY STRING, so no phrase list can ever classify one), and only then
falls back to phrases. Adding a phrase is almost always the wrong fix.

`PROVIDER_CIRCUIT_OPEN` is the engine code for a request the open LLM breaker
stopped before it reached any provider. It is transient like `RETRY_EXHAUSTED`
and maps to the same 503, but it names a condition an operator can act on
instead of reporting the one failure the system understands completely as
`UNKNOWN_ERROR`. A breaker that latched `QUOTA_EXHAUSTED` or
`PROVIDER_AUTH_FAILED` keeps that classification instead.

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

Every response body contains at minimum:

```json
{"error": "<class label>", "detail": "<message>", "status_code": <int>}
```

**One exception answers a different body on purpose.**
`OAuthProtocolError` (`models/exceptions.py`) renders as RFC 6749 §5.2 —
`{"error": "<rfc code>", "error_description": "<text>"}`, with no `detail`
and no `status_code` field — because a standards-written OAuth client
dispatches on `error`, and the RFC fixes the field names. It is raised only
by `POST /auth/oauth/token` and `POST /auth/oauth/revoke` (#1150); every
other route, `GET /auth/oauth/authorize` included, keeps the shape above.
Raising it from anywhere else would hand that caller a body its client does
not read.

`NotFoundError` and `ConflictError` additionally surface their
structured metadata **when present** on the exception instance.
Fields are **omitted** (not `null`) when absent, so the response
stays minimal for callers that raise with only a message:

```python
@app.exception_handler(NotFoundError)
async def not_found_handler(request, exc):
    body = {"error": "Not Found", "detail": str(exc), "status_code": 404}
    if exc.resource_type is not None:
        body["resource_type"] = exc.resource_type
    if exc.resource_id is not None:
        body["resource_id"] = exc.resource_id
    return JSONResponse(status_code=404, content=body)

@app.exception_handler(ConflictError)
async def conflict_exception_handler(request, exc):
    body = {"error": "Conflict", "detail": str(exc), "status_code": 409}
    if exc.resource_type is not None:
        body["resource_type"] = exc.resource_type
    if exc.resource_id is not None:
        body["resource_id"] = exc.resource_id
    if exc.conflict_reason is not None:
        body["conflict_reason"] = exc.conflict_reason
    return JSONResponse(status_code=409, content=body)

@app.exception_handler(ValidationException)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={"error": "Validation Error", "detail": str(exc), "status_code": 422},
    )
```

**Example `ConflictError` response body:**

```json
{
  "error": "Conflict",
  "detail": "User with username 'alice' already exists",
  "status_code": 409,
  "resource_type": "user",
  "resource_id": "alice",
  "conflict_reason": "duplicate_username"
}
```

Clients should branch on `conflict_reason` / `resource_type` rather
than regex-matching `detail` — those are the stable machine-readable
keys. The `detail` string is human-facing and may be edited freely
without bumping the contract.

### Framework validation errors (`RequestValidationError`)

FastAPI raises `RequestValidationError` *before* any module code runs,
when it cannot bind the request to the endpoint signature. Its handler
lives beside the domain handlers in `api/exception_handlers.py` but is
registered explicitly in `main.py`, because
`get_exception_handlers()` maps domain exceptions only. It answers with
a different, older shape than the table above:

```json
{
  "detail": "Validation error",
  "errors": [
    {
      "type": "string_too_short",
      "loc": ["body", "username"],
      "msg": "String should have at least 3 characters",
      "input": "a",
      "ctx": {"min_length": 3}
    }
  ]
}
```

Three invariants govern it — the first two learned from fm#1048, the
third from fm#1156:

1. **Serialization must be total.** A pydantic error's `input` is
   whatever object the framework fed to validation — raw `bytes` for a
   non-JSON body, an `UploadFile` for a misbound multipart part — and
   `NaN` or a lone surrogate reaches it from a *valid* JSON body.
   Serializing any of those directly raises inside the handler, and an
   exception in the handler is an opaque 500 plus an `api.error_rate`
   hit for a request that was only malformed. `utils.serialization.
   to_json_safe` is the total converter; `to_json_compatible` is not
   (it passes unknown types through) and must not be used here.
2. **The echoed `input` is bounded** (`MAX_VALIDATION_INPUT_BYTES`).
   A body-level error's `input` is the entire request body, so an
   unbounded echo would mirror up to `MAX_UPLOAD_SIZE_MB` back at an
   unauthenticated caller.
3. **`input` is echoed only where echoing it discloses nothing beyond
   the one field the endpoint declared and the error is about** — in
   the response *and* in the ERROR log, which carry the same sanitized
   errors. Otherwise `input` is an object of the caller's other fields,
   or a field the API never declared, and on `/auth/refresh`,
   `/auth/login` or `PUT /admin/llm/config` those are a refresh token, a
   password or a provider API key. `sanitize_validation_error` withholds
   it in five cases, in this order (the order matters only so that the
   placeholder a caller sees is the accurate one):

   1. `loc == ("body",)` — the error is about the body, so `input` is
      the body;
   2. `missing`, `missing_argument`, `missing_keyword_only_argument` or
      `missing_positional_only_argument` **with an aggregate `input`** —
      no value exists at `loc`, so pydantic substitutes the object the
      field is missing *from*, and `loc` reads field-level while `input`
      is the enclosing object. This is what fm#1156 was. The four are
      named rather than prefix-matched: pydantic's fifth `missing*`
      type, `missing_sentinel_error`, reports the supplied value like
      any other and keeps its `input`. The aggregate condition matters
      too — FastAPI hard-codes `input=None` for a missing query, header,
      cookie, path or form field, where nothing was ever enclosed, and
      those keep their `null`;
   3. `value_error` or `assertion_error` **with a Mapping `input`** — a
      `@model_validator` failing on a sub-object reports that whole
      sub-object, so one cross-field check discloses every field in it;
   4. `extra_forbidden`, `unexpected_keyword_argument` or
      `unexpected_positional_argument`, unconditionally — here `input`
      really is one field's value, but the field is one the endpoint
      never declared, so the API has no schema for it and cannot know it
      is not a credential. An undeclared field is most often a mis-keyed
      declared one, so this is fm#1156's own headline case arriving
      under a different type: `{"refreshToken": ...}` against a
      forbidding model yields a guarded `missing` *and* an
      `extra_forbidden` carrying the token. This case is why the
      invariant is phrased as "declared" rather than "one field's
      value";
   5. `input is exc.body` at any `loc` — defence in depth for an error
      type reporting the whole body somewhere the rules above do not
      name. Guarded by `body is not None`, since `exc.body` is None for
      a GET and for a JSON `null` body and an unguarded identity test
      would rewrite every honest `input: null`.

   This is deliberately **not** a complete classification and cannot be
   one here: a pydantic error carries no schema, so a mapping that is a
   model object and a mapping that is one field's own dict value are
   indistinguishable at runtime, and invariant 2 exists precisely to
   bound the latter rather than withhold it. A model validator raising
   `PydanticCustomError` with its own type string is the known residual.

   The log adds only `describe_request_body`'s content-free shape
   (`<dict: 2 items>`, `<bytes: 57 bytes>`) and logs `request.url.path`
   rather than `request.url`; it must never carry `exc.body` itself,
   because there is no redaction processor in the structlog chain to
   catch it downstream. Field-level errors of every other type keep
   their `input` — it is what makes a 422 actionable.

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
        # NotFoundError, AuthorizationError, ServiceException)
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

### When to use the full `try/except` vs. no wrapper at all

Both shapes are valid depending on route needs:

- **Use the full `try/except` with the `FaultMavenException`
  pass-through when** the route needs a route-specific 500 envelope
  (e.g., a domain-specific `error_code`) or a route-specific log
  message on unhandled errors. Pattern from auth `local_login` (PR
  #331).
- **Omit `try/except` entirely when** the route doesn't need either.
  Typed exceptions propagate to global handlers; unhandled
  exceptions go to FastAPI's default 500 (which still logs the
  traceback). Pattern from knowledge `verify_draft` (PR #334).

The choice is per-route. Use the simpler "no wrapper" form unless
there's a specific reason to add the envelope/logging layer.

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
