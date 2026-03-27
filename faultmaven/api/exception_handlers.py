"""API Exception Handlers (TASK-014)

Purpose: FastAPI exception handlers for translating service exceptions to HTTP responses.

This module provides exception handlers for:
- NotFoundError → 404 Not Found
- AuthorizationError → 403 Forbidden
- ValidationException → 400 Bad Request
- ConflictError → 409 Conflict
- ServiceError → 500 Internal Server Error

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import logging
from collections.abc import Callable

from fastapi import Request, status
from fastapi.responses import JSONResponse

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationException,
)

logger = logging.getLogger(__name__)


async def not_found_exception_handler(
    request: Request,
    exc: NotFoundError,
) -> JSONResponse:
    """Handle NotFoundError.

    Translates NotFoundError to HTTP 404 Not Found response.

    Args:
        request: FastAPI request object
        exc: NotFoundError exception

    Returns:
        JSONResponse with 404 status and error details
    """
    logger.warning(
        "Resource not found: %s %s - %s",
        request.method,
        request.url.path,
        str(exc),
    )

    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "Not Found",
            "detail": str(exc),
            "status_code": 404,
        },
    )


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

    Translates ConflictError to HTTP 409 Conflict response.

    Args:
        request: FastAPI request object
        exc: ConflictError exception

    Returns:
        JSONResponse with 409 status and error details
    """
    logger.warning(
        "Conflict error: %s %s - %s",
        request.method,
        request.url.path,
        str(exc),
    )

    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "Conflict",
            "detail": str(exc),
            "status_code": 409,
        },
    )


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


def get_exception_handlers() -> dict[type[Exception], Callable]:
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
