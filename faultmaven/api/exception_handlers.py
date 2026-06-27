"""API Exception Handlers

Purpose: FastAPI exception handlers for translating service exceptions to HTTP responses.

This module provides exception handlers for:
- NotFoundError → 404 Not Found
- AuthorizationError → 403 Forbidden
- ValidationException → 422 Unprocessable Entity
- ConflictError → 409 Conflict
- ServiceError → 500 Internal Server Error

NotFoundError and ConflictError surface their structured metadata
(``resource_type``, ``resource_id``, and ``conflict_reason`` on
ConflictError) in the response body when present. Clients should
branch on those fields rather than parsing the ``detail`` string.

Specification: docs/architecture/specifications/exception-contract.md
"""

import logging
from typing import Callable, Optional, Type

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationException,
)

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
    }
