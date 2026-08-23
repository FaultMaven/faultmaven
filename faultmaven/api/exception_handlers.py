"""API Exception Handlers

Purpose: FastAPI exception handlers for translating service exceptions to HTTP responses.

This module provides exception handlers for:
- NotFoundError → 404 Not Found
- AuthorizationError → 403 Forbidden
- ValidationException → 422 Unprocessable Entity
- ConflictError → 409 Conflict
- ServiceError → 500 Internal Server Error
- OAuthProtocolError → the RFC 6749 §5.2 body, at the status it carries

``OAuthProtocolError`` is the one handler here that does not answer the
common ``{"error", "detail", "status_code"}`` shape: the OAuth token and
revocation endpoints answer the RFC's own object so a standards-written
client can dispatch on its ``error`` code (#1150).

It also holds the handler for FastAPI's own ``RequestValidationError``
(→ 422), which is a framework error rather than a domain one: it fires
before any module code runs, and ``main.py`` registers it separately from
``get_exception_handlers()``.

NotFoundError and ConflictError surface their structured metadata
(``resource_type``, ``resource_id``, and ``conflict_reason`` on
ConflictError) in the response body when present. Clients should
branch on those fields rather than parsing the ``detail`` string.

Specification: docs/architecture/specifications/exception-contract.md
"""

import json
import logging
from json import JSONDecodeError
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Type

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    TOKEN_LIMIT,
    AuthorizationError,
    ConflictError,
    LLMException,
    NotFoundError,
    ServiceError,
    ValidationException,
    is_billing_error,
)
from faultmaven.models.exceptions import OAuthProtocolError
from faultmaven.utils.serialization import to_json_safe

logger = logging.getLogger(__name__)

# User-facing message for billing/quota exhaustion. Single source of truth so
# every endpoint that surfaces QUOTA_EXHAUSTED says the same thing.
QUOTA_EXHAUSTED_DETAIL = (
    "FaultMaven's AI provider is out of quota or credits. An administrator "
    "needs to add credits or update the provider's billing plan before this "
    "can continue."
)


def is_quota_exhausted_service_error(exc: BaseException) -> bool:
    """True if ``exc`` carries the ``QUOTA_EXHAUSTED`` error_code in its details.

    The signal a ServiceException carries when an LLM-calling service hits
    billing/quota exhaustion. Shared by every route's ``except ServiceException``
    block so billing is detected identically (→ 402) instead of each handler
    re-implementing the lookup.
    """
    return (getattr(exc, "details", None) or {}).get("error_code") == QUOTA_EXHAUSTED


def quota_exhausted_http_exception(
    correlation_id: Optional[str] = None,
) -> HTTPException:
    """Build the canonical HTTP 402 response for billing/quota exhaustion.

    A permanent, operator-actionable condition — the provider is out of credits.
    Maps to **402 Payment Required** with ``x-error-code: QUOTA_EXHAUSTED`` and
    **no** ``Retry-After`` (retrying can't help until an operator adds credits).
    Use from any route's ``except`` block when ``exceptions.is_billing_error``
    (or a ServiceException carrying ``error_code == QUOTA_EXHAUSTED``) matches,
    so every LLM-calling endpoint surfaces billing identically.
    """
    headers = {"x-error-code": QUOTA_EXHAUSTED}
    if correlation_id is not None:
        headers["x-correlation-id"] = correlation_id
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=QUOTA_EXHAUSTED_DETAIL,
        headers=headers,
    )


def _walk_cause_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` and each ``__cause__`` ancestor (cycle-guarded).

    Service code wraps provider failures as ``ServiceException(...) from e``, so
    the authoritative typed metadata lives on the cause, not the wrapper. Walk
    ``__cause__`` only (the explicit ``raise ... from`` link) — the same chain
    ``exceptions.is_billing_error`` and ``LLMErrorHandler.is_retryable_error``
    already trust — never implicit ``__context__``, which can pull in unrelated
    in-flight exceptions.
    """
    cursor: Optional[BaseException] = exc
    seen: set = set()
    while cursor is not None and id(cursor) not in seen:
        yield cursor
        seen.add(id(cursor))
        cursor = cursor.__cause__


def _first_engine_error_code(exc: BaseException) -> Optional[str]:
    """The semantic ``error_code`` the engine attached, if any.

    The investigation turn runs LLM calls through ``LLMErrorHandler.with_retry``,
    which converts the provider exception into an ``ErrorResult`` and re-raises a
    ``MilestoneEngineError(error_code=...)`` — a plain exception with no provider
    ``status_code`` and (for the retry-exhaustion path) no ``__cause__`` link to
    the original. Its ``error_code`` (e.g. ``RETRY_EXHAUSTED``, ``UNKNOWN_ERROR``)
    is the DESIGNED cross-layer signal (see ``MilestoneEngineError``'s docstring).
    The turn service threads it onto the wrapper's ``details["error_code"]``;
    prefer that, then fall back to any ``error_code`` on the ``__cause__`` chain.
    """
    threaded = (getattr(exc, "details", None) or {}).get("error_code")
    if threaded:
        return threaded
    for c in _walk_cause_chain(exc):
        code = getattr(c, "error_code", None)
        if code:
            return code
    return None


# Semantic engine error codes (``LLMErrorHandler`` → ``MilestoneEngineError``)
# that describe a transient LLM-call failure worth retrying: retries were
# exhausted, the context/output hit a token limit, or the failure could not be
# classified but still originated from an LLM call (not arbitrary server logic).
_RETRYABLE_ENGINE_CODES = frozenset({"RETRY_EXHAUSTED", TOKEN_LIMIT, "UNKNOWN_ERROR"})
# Semantic engine codes describing a permanent provider/config rejection — the
# model is misnamed or the credentials are bad; retrying cannot help.
_TERMINAL_ENGINE_CODES = frozenset({"MODEL_NOT_FOUND", "AUTH_FAILED"})


def _llm_http(
    status_code: int,
    error_code: str,
    detail: str,
    retry_after: Optional[str],
    correlation_id: Optional[str],
) -> HTTPException:
    headers = {"x-error-code": error_code}
    if correlation_id is not None:
        headers["x-correlation-id"] = correlation_id
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def llm_service_error_http_exception(
    exc: BaseException,
    correlation_id: Optional[str] = None,
) -> HTTPException:
    """Map an LLM-calling ``ServiceException`` to a precise HTTP response.

    Classifies off the TYPED metadata already attached to the failure — the
    provider's ``LLMException.status_code`` / ``retryable`` on the ``__cause__``
    chain, and the engine's semantic ``error_code`` (from
    ``MilestoneEngineError``) — instead of substring-matching the message.
    Message matching silently mis-routed real failures to a bare 500: a provider
    that raised "…timed out…" (∌ the ``"timeout"`` substring), a Gemini ``400``,
    and a schema-parse ``ValidationError`` all fell through, so a
    transient/upstream provider condition read to the user as a FaultMaven bug.
    Shared by every ``except ServiceException`` block that wraps an LLM turn so
    the mapping is defined once.

    Two signal sources reach the route, because a turn can fail on two paths:
    a **direct** propagation carries the raw ``LLMException`` on ``__cause__``
    (status/retryable read below); the **retry-loop** path (``with_retry`` →
    ``MilestoneEngineError``) carries only a semantic ``error_code`` — no
    provider status, no ``__cause__`` link. Both are read.

    Mapping (first match wins)::

        billing / quota exhausted              → 402  QUOTA_EXHAUSTED           (no retry)
        provider status 429                    → 429  RATE_LIMIT_EXCEEDED       (retry 60)
        provider status 504                    → 504  LLM_TIMEOUT               (retry 30)
        provider status 503                    → 503  LLM_OVER_CAPACITY         (retry 60)
        provider status 5xx (other)            → 503  LLM_PROVIDER_UNAVAILABLE  (retry 60)
        provider status 4xx (other)            → 502  LLM_PROVIDER_ERROR        (no retry)
        LLMException, no status, retryable     → 503  LLM_PROVIDER_UNAVAILABLE  (retry 30)
        LLMException, no status, terminal      → 502  LLM_PROVIDER_ERROR        (no retry)
        engine RETRY_EXHAUSTED/TOKEN_LIMIT/    → 503  LLM_PROVIDER_UNAVAILABLE  (retry 30)
          UNKNOWN_ERROR
        engine MODEL_NOT_FOUND/AUTH_FAILED     → 502  LLM_PROVIDER_ERROR        (no retry)
        schema-parse failure on the chain      → 503  LLM_INVALID_RESPONSE      (retry 30)
        anything else                          → 500  SERVICE_ERROR             (retry 10)

    A 4xx (other than 429) means the provider rejected *this request* — the same
    request retried yields the same rejection, so no ``Retry-After``. A parse
    failure (direct, or engine ``UNKNOWN_ERROR`` from the retry loop) is retried
    because a BEST_EFFORT model may emit valid JSON on the next attempt.
    """
    # 1. Permanent billing / quota exhaustion — 402, no Retry-After. Checked
    #    first: it must never be mistaken for a transient 429/5xx.
    if is_quota_exhausted_service_error(exc) or is_billing_error(exc):
        return quota_exhausted_http_exception(correlation_id)

    # 2. Provider status on the raw LLMException (direct-propagation path).
    llm = next((c for c in _walk_cause_chain(exc) if isinstance(c, LLMException)), None)
    sc = llm.status_code if llm is not None else None
    if sc == 429:
        return _llm_http(
            429,
            "RATE_LIMIT_EXCEEDED",
            "Rate limit exceeded. Please wait before sending another message.",
            "60",
            correlation_id,
        )
    if sc == 504:
        return _llm_http(
            504,
            "LLM_TIMEOUT",
            "The AI provider timed out. Please try again.",
            "30",
            correlation_id,
        )
    if sc == 503:
        return _llm_http(
            503,
            "LLM_OVER_CAPACITY",
            "AI service temporarily unavailable due to high demand. "
            "Please try again.",
            "60",
            correlation_id,
        )
    if sc is not None and sc >= 500:
        return _llm_http(
            503,
            "LLM_PROVIDER_UNAVAILABLE",
            "The AI provider is temporarily unavailable. Please try again.",
            "60",
            correlation_id,
        )
    if sc is not None:
        # Other 4xx — the provider rejected the request itself; retrying the
        # identical request will not help, so no Retry-After.
        return _llm_http(
            502,
            "LLM_PROVIDER_ERROR",
            "The AI provider could not process this request.",
            None,
            correlation_id,
        )
    if llm is not None:
        # Typed LLMException without a status code — trust its retryable flag.
        if llm.retryable:
            return _llm_http(
                503,
                "LLM_PROVIDER_UNAVAILABLE",
                "The AI provider is temporarily unavailable. Please try again.",
                "30",
                correlation_id,
            )
        return _llm_http(
            502,
            "LLM_PROVIDER_ERROR",
            "The AI provider could not process this request.",
            None,
            correlation_id,
        )

    # 3. Engine's semantic failure code (retry-loop path — no LLMException on the
    #    __cause__ chain, only MilestoneEngineError.error_code).
    code = _first_engine_error_code(exc)
    if code in _RETRYABLE_ENGINE_CODES:
        return _llm_http(
            503,
            "LLM_PROVIDER_UNAVAILABLE",
            "The AI provider is temporarily unavailable. Please try again.",
            "30",
            correlation_id,
        )
    if code in _TERMINAL_ENGINE_CODES:
        return _llm_http(
            502,
            "LLM_PROVIDER_ERROR",
            "The AI provider could not process this request.",
            None,
            correlation_id,
        )

    # 4. Direct schema-parse failure (ValidationError / JSONDecodeError that
    #    propagated raw, not via the retry loop's UNKNOWN_ERROR).
    if any(
        isinstance(c, (ValidationError, JSONDecodeError))
        for c in _walk_cause_chain(exc)
    ):
        return _llm_http(
            503,
            "LLM_INVALID_RESPONSE",
            "The AI provider returned an incomplete response. Please try again.",
            "30",
            correlation_id,
        )

    # 5. Genuinely unclassifiable — bounded message, never internals.
    return _llm_http(
        500,
        "SERVICE_ERROR",
        f"Unable to process your message: {str(exc)[:200]}",
        "10",
        correlation_id,
    )


async def not_found_exception_handler(
    request: Request,
    exc: NotFoundError,
) -> JSONResponse:
    """Handle NotFoundError.

    Translates NotFoundError to HTTP 404 Not Found response. The
    structured ``resource_type`` and ``resource_id`` fields carried on
    the exception are surfaced in the response body so clients can
    branch on the missing-resource kind without parsing the
    human-readable ``detail`` string.

    Args:
        request: FastAPI request object
        exc: NotFoundError exception

    Returns:
        JSONResponse with 404 status, detail, and structured metadata.
        ``resource_type`` / ``resource_id`` are omitted (not null) when
        the exception was raised without them.
    """
    logger.warning(
        "Resource not found: %s %s - %s",
        request.method,
        request.url.path,
        str(exc),
    )

    body = {
        "error": "Not Found",
        "detail": str(exc),
        "status_code": 404,
    }
    # Omit-when-absent: keeps the response shape minimal for callers
    # raising NotFoundError(message=...) without the structured fields.
    if exc.resource_type is not None:
        body["resource_type"] = exc.resource_type
    if exc.resource_id is not None:
        body["resource_id"] = exc.resource_id

    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=body)


async def authorization_exception_handler(
    request: Request,
    exc: AuthorizationError,
) -> JSONResponse:
    """Handle AuthorizationError.

    Translates AuthorizationError to HTTP 403 Forbidden response.

    Args:
        request: FastAPI request object
        exc: AuthorizationError exception

    Returns:
        JSONResponse with 403 status and error details
    """
    logger.warning(
        "Authorization denied: %s %s - %s",
        request.method,
        request.url.path,
        str(exc),
    )

    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "error": "Forbidden",
            "detail": str(exc),
            "status_code": 403,
        },
    )


async def validation_exception_handler(
    request: Request,
    exc: ValidationException,
) -> JSONResponse:
    """Handle ValidationException.

    Translates ValidationException to HTTP 422 Unprocessable Entity response.

    Args:
        request: FastAPI request object
        exc: ValidationException exception

    Returns:
        JSONResponse with 422 status and error details
    """
    logger.warning(
        "Validation error: %s %s - %s",
        request.method,
        request.url.path,
        str(exc),
    )

    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation Error",
            "detail": str(exc),
            "status_code": 422,
        },
    )


async def conflict_exception_handler(
    request: Request,
    exc: ConflictError,
) -> JSONResponse:
    """Handle ConflictError.

    Translates ConflictError to HTTP 409 Conflict response. The
    structured ``resource_type`` / ``resource_id`` / ``conflict_reason``
    fields carried on the exception are surfaced in the response body so
    clients can distinguish conflict shapes (e.g. ``duplicate_username``
    vs ``duplicate_email`` vs ``already_verified``) programmatically
    without parsing the ``detail`` string.

    Args:
        request: FastAPI request object
        exc: ConflictError exception

    Returns:
        JSONResponse with 409 status, detail, and structured metadata.
        Fields are omitted (not null) when the exception was raised
        without them.
    """
    logger.warning(
        "Conflict error: %s %s - %s",
        request.method,
        request.url.path,
        str(exc),
    )

    body = {
        "error": "Conflict",
        "detail": str(exc),
        "status_code": 409,
    }
    if exc.resource_type is not None:
        body["resource_type"] = exc.resource_type
    if exc.resource_id is not None:
        body["resource_id"] = exc.resource_id
    if exc.conflict_reason is not None:
        body["conflict_reason"] = exc.conflict_reason

    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=body)


async def service_error_handler(
    request: Request,
    exc: ServiceError,
) -> JSONResponse:
    """Handle ServiceError.

    Translates ServiceError to HTTP 500 Internal Server Error response.
    Hides internal error details from clients for security.

    Args:
        request: FastAPI request object
        exc: ServiceError exception

    Returns:
        JSONResponse with 500 status and generic error message
    """
    logger.error(
        "Service error: %s %s - %s",
        request.method,
        request.url.path,
        str(exc),
        exc_info=True,
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal Server Error",
            "detail": "An unexpected error occurred",
            "status_code": 500,
        },
    )


# =============================================================================
# Framework HTTP errors
# =============================================================================


async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTPException handler to ensure consistent error response format"""
    detail = exc.detail

    # Two dict-detail shapes reach here, and both must yield the human message
    # because clients render `detail` verbatim as user-facing text:
    #
    #   nested — `ErrorResponse.model_dump()`, built at runtime by the case
    #            router: {"schema_version": ..., "error": {"code", "message"}}
    #   flat   — written literally: {"error": "<code>", "message": "<text>"}
    #
    # The `error` value is a str in the flat shape, so the nested branch must
    # test the *type* before subscripting: `"message" in detail["error"]` alone
    # is a substring test over the error code, and a code such as
    # "message_send_failed" would take the branch and raise TypeError here,
    # inside the exception handler.
    if isinstance(detail, dict):
        nested = detail.get("error")
        if isinstance(nested, dict) and "message" in nested:
            message = nested["message"]
        else:
            message = detail.get("message") or detail.get("detail") or str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            # Coerced for the same reason #1048 made the validation handler's
            # serialization total: whatever is pulled out of a dict `detail`
            # goes straight into JSONResponse, and a value the encoder cannot
            # render raises *inside the handler* — turning a deliberate 4xx
            # into a 500 with none of the message the client was meant to see.
            # Neither branch above can promise a str: `nested["message"]` is
            # whatever the raising code put there, and `detail.get("message")`
            # is unconstrained too. Verified: a non-serializable message made
            # a 400 answer 500.
            content={"detail": to_json_safe(message)},
            headers=getattr(exc, "headers", None),
        )
    # If detail is a string, return it as expected by tests
    else:
        return JSONResponse(
            status_code=exc.status_code,
            # Coerced for the same reason as the dict branch above. `str` is
            # not a safe type here: a lone surrogate reaches a detail from a
            # *valid* JSON body — `json.loads('"\ud800"')` succeeds — and
            # user-supplied strings are interpolated straight into details
            # (auth.py's `username`, admin_config.py's `provider_name`). The
            # encoder then raises inside this handler and the deliberate 4xx
            # becomes a 500 carrying none of the message.
            content={"detail": to_json_safe(detail)},
            headers=getattr(exc, "headers", None),
        )


async def oauth_protocol_error_handler(
    request: Request, exc: OAuthProtocolError
) -> JSONResponse:
    """Render an RFC 6749 §5.2 error body for the OAuth token endpoints.

    The OAuth token and revocation endpoints answer errors as
    ``{"error": ..., "error_description": ...}`` rather than the
    ``{"detail": ...}`` every other route uses, because a standards-written
    client dispatches on the `error` code (#1150). `main.py`'s HTTPException
    handler flattens any raised HTTPException to ``detail``, so this shape
    cannot be produced that way.

    Only ``OAuthProtocolError`` is rendered here, and only the two endpoints
    that speak the OAuth wire format raise it — no other route's error shape
    changes.

    RFC 6749 §5.1: a refusal names an expired or revoked credential, so it is
    no more cacheable than the token response it replaces.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "error_description": exc.error_description},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def get_exception_handlers() -> dict[Type[Exception], Callable]:
    """Get all exception handlers as a dictionary.

    Returns:
        Dictionary mapping exception types to handler functions
    """
    return {
        NotFoundError: not_found_exception_handler,
        AuthorizationError: authorization_exception_handler,
        ValidationException: validation_exception_handler,
        ConflictError: conflict_exception_handler,
        ServiceError: service_error_handler,
        OAuthProtocolError: oauth_protocol_error_handler,
    }


# =============================================================================
# Framework validation errors (fm#1048)
# =============================================================================

# Per-error ceiling on the echoed `input`, measured in UTF-8 bytes because that
# is what the response is encoded as: counting characters lets a CJK payload
# through at ~3x the stated ceiling. Pre-#1048 the reflection was already 1:1
# for field-level errors — a 200 KB bad field produced a 200 KB 422.
MAX_VALIDATION_INPUT_BYTES = 2048

# Ceiling on how many errors one 422 reports. Pydantic emits one per offending
# item, so a request whose body is a long list of wrong-typed values produces a
# response many times its own size: a measured 128,900-byte body yielded 20,000
# errors and a 2,057,820-byte response (x16), and the same line goes to the log.
# The per-error budget above cannot bound that — only a cap on the count can.
MAX_VALIDATION_ERRORS = 50

# `loc == ("body",)` means the whole body failed to bind, so `input` IS the whole
# body. Echoing it hands a caller's own credentials back in the response: the
# form-encoded POST that motivated #1048 is exactly this shape, and on
# /auth/oauth/token or /auth/login the body is a refresh token or a password.
# Before #1048 that echo could not happen — the handler crashed instead — so
# restoring the 422 without this would have introduced it. Field-level errors
# keep their `input`; it is what makes a 422 actionable, and it names one field
# rather than the whole payload.
_WHOLE_BODY_LOC = ("body",)


def sanitize_validation_error(error: Mapping[str, Any]) -> Dict[str, Any]:
    """Make one pydantic error dict renderable, and bound its echoed input."""
    safe = to_json_safe(error)
    if not isinstance(safe, dict):  # pragma: no cover - errors() yields dicts
        return {"msg": str(safe)}

    if "input" not in safe:
        return safe

    if tuple(error.get("loc") or ()) == _WHOLE_BODY_LOC:
        raw = error.get("input")
        size = f": {len(raw)} bytes" if isinstance(raw, (bytes, bytearray)) else ""
        safe["input"] = f"<request body not echoed{size}>"
        return safe

    # to_json_safe is total, so this dumps cannot raise. Measured after encoding
    # for the reason given on MAX_VALIDATION_INPUT_BYTES.
    rendered = len(json.dumps(safe["input"], ensure_ascii=False).encode("utf-8"))
    if rendered > MAX_VALIDATION_INPUT_BYTES:
        safe["input"] = (
            f"<input omitted: {rendered} bytes exceeds the "
            f"{MAX_VALIDATION_INPUT_BYTES}-byte echo budget>"
        )
    return safe


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Handle FastAPI's RequestValidationError → 422.

    Distinct from :func:`validation_exception_handler` above, which handles the
    domain-layer ``ValidationException``: this one fires *before* any module
    code runs, on the request FastAPI could not bind to the endpoint's
    signature. It is registered explicitly in ``main.py`` rather than through
    :func:`get_exception_handlers`, which maps domain exceptions only.

    The invariant that matters here is that this handler **cannot raise**.
    A pydantic error's ``input`` is whatever object the framework fed to
    validation, and ``ctx`` values are likewise arbitrary; serializing them
    directly is how a well-formed 422 turned into an opaque 500 (fm#1048).
    Five request shapes did it, and only the first was diagnosed from the
    symptom:

    * a form-encoded (or any non-JSON) body on a JSON endpoint → ``input`` is
      raw ``bytes`` → TypeError;
    * a file part supplied for a scalar ``Form`` field → ``input`` is an
      ``UploadFile`` → TypeError;
    * ``NaN``/``Infinity`` in a JSON body (``json.loads`` accepts both) →
      ValueError, because Starlette renders with ``allow_nan=False``;
    * a lone surrogate in a JSON string (``"\\ud800"``) → UnicodeEncodeError at
      ``.encode("utf-8")``, from a plain ``str``;
    * anything else pydantic hands back that json cannot render.

    So the fix is not a bytes special-case — the previous handler already had a
    special-case of exactly that shape, for ``ValueError`` in ``ctx``, and this
    is the same bug one type later. :func:`to_json_safe` is total instead.

    Three ceilings bound what a 422 costs, because restoring the response is
    what makes them reachable: ``MAX_VALIDATION_INPUT_BYTES`` per echoed value,
    ``MAX_VALIDATION_ERRORS`` on the count, and no echo at all for a whole-body
    error. Note ``exc.body`` still reaches the log in full-ish (bounded to 512
    characters by :func:`to_json_safe`) — that predates #1048 and is unchanged
    here, but it does mean an auth endpoint's credentials land in an ERROR log
    line.
    """
    raw_errors = exc.errors()
    errors = [
        sanitize_validation_error(error) for error in raw_errors[:MAX_VALIDATION_ERRORS]
    ]
    if len(raw_errors) > MAX_VALIDATION_ERRORS:
        errors.append(
            {
                "type": "too_many_errors",
                "loc": ["body"],
                "msg": (
                    f"{len(raw_errors) - MAX_VALIDATION_ERRORS} further "
                    f"validation errors were not reported"
                ),
            }
        )

    logger.error(
        "Validation error on %s %s: %s",
        request.method,
        request.url,
        errors,
        extra={
            "validation_errors": errors,
            "body": to_json_safe(getattr(exc, "body", None)),
        },
    )

    return JSONResponse(
        status_code=422,
        content={"detail": "Validation error", "errors": errors},
    )
