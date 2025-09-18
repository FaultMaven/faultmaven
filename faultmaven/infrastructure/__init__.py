"""Infrastructure Package

This package contains infrastructure layer components that handle external
integrations, observability, security, persistence, and background jobs.

The base_client module provides the BaseExternalClient class that all
infrastructure clients should inherit from for consistent external service
interaction patterns with unified logging, circuit breaker protection,
and comprehensive error handling.
"""

from .base_client import BaseExternalClient, CircuitBreakerError
from .jobs import JobService

__all__ = [
    "BaseExternalClient",
    "CircuitBreakerError",
    "JobService",
]