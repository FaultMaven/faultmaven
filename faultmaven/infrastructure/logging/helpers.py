"""
Logging Helper Utilities

Standardized error logging functions that ensure consistent error handling
with full context and stack traces across the FaultMaven codebase.
"""

import logging
from typing import Any


def log_error_with_context(
    logger: logging.Logger, message: str, error: Exception, **context: Any
) -> None:
    """
    Standard error logging with full context and stack trace.

    This helper ensures consistent error logging with exc_info=True and
    structured context across all FaultMaven components.

    Args:
        logger: Logger instance to use
        message: Error message describing what failed
        error: The exception that occurred
        **context: Additional context as keyword arguments (e.g., case_id, user_id)

    Example:
        >>> try:
        ...     result = await process_case(case_id)
        ... except Exception as e:
        ...     log_error_with_context(
        ...         logger,
        ...         "Failed to process case",
        ...         e,
        ...         case_id=case_id,
        ...         operation="process_case"
        ...     )
    """
    # Add exception type to context for searchability
    context["exception_type"] = type(error).__name__

    # Log with full stack trace
    logger.error(
        f"{message}: {type(error).__name__}: {error}", exc_info=True, extra=context
    )


def log_warning_with_context(
    logger: logging.Logger, message: str, **context: Any
) -> None:
    """
    Standard warning logging with structured context.

    Args:
        logger: Logger instance to use
        message: Warning message
        **context: Additional context as keyword arguments

    Example:
        >>> log_warning_with_context(
        ...     logger,
        ...     "Fallback provider used",
        ...     provider="openai",
        ...     reason="rate_limit"
        ... )
    """
    logger.warning(message, extra=context)


def log_operation_error(
    logger: logging.Logger,
    operation: str,
    error: Exception,
    entity_id: str | None = None,
    **context: Any,
) -> None:
    """
    Log an operation failure with standardized format.

    Convenience wrapper for common pattern of logging operation failures.

    Args:
        logger: Logger instance to use
        operation: Name of the operation that failed (e.g., "database_query", "llm_call")
        error: The exception that occurred
        entity_id: Optional ID of the entity being operated on
        **context: Additional context

    Example:
        >>> try:
        ...     await repo.update(case)
        ... except Exception as e:
        ...     log_operation_error(
        ...         logger,
        ...         "update_case",
        ...         e,
        ...         entity_id=case.case_id,
        ...         table="cases"
        ...     )
    """
    context["operation"] = operation
    if entity_id:
        context["entity_id"] = entity_id

    message = f"Operation '{operation}' failed"
    if entity_id:
        message += f" for {entity_id}"

    log_error_with_context(logger, message, error, **context)
