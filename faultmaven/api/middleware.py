"""
DEPRECATED: Legacy middleware file.

This file has been replaced by the new middleware package structure.
Use: from faultmaven.api.middleware.logging import LoggingMiddleware

This file will be removed in a future version.
"""

# Import for backward compatibility
from .middleware.logging import LoggingMiddleware

__all__ = ['LoggingMiddleware']