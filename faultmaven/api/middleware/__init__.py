"""
Middleware package for FaultMaven API.
"""

from .logging import LoggingMiddleware

__all__ = ['LoggingMiddleware']