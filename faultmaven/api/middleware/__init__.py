"""
Middleware package for FaultMaven API protection

This package contains middleware components for:
- Request logging and monitoring
- Rate limiting (sliding window, multi-level)
- Request deduplication (content-based hashing)
- Security headers and protection
- JWT Authentication (TASK-017)
"""

from .auth import (
    get_auth_service,
    get_current_user,
    get_current_user_optional,
    require_admin,
    require_all_permissions,
    require_any_permission,
    require_any_role,
    require_permission,
    require_role,
    set_auth_service,
)
from .deduplication import DeduplicationMiddleware
from .logging import LoggingMiddleware
from .rate_limiting import RateLimitMiddleware
from .tenant_isolation import set_tenant_context

__all__ = [
    "LoggingMiddleware",
    "RateLimitMiddleware",
    "DeduplicationMiddleware",
    # JWT Authentication
    "get_current_user",
    "get_current_user_optional",
    "require_permission",
    "require_any_permission",
    "require_all_permissions",
    "require_role",
    "require_any_role",
    "require_admin",
    "get_auth_service",
    "set_auth_service",
    # PostgreSQL RLS (Phase 8 storage redesign)
    "set_tenant_context",
]
