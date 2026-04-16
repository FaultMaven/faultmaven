"""API Routes Package.

Legacy routes (admin, admin_config, sessions) remain here.
Module routes live in their respective modules (agent, case, evidence, knowledge, report).
"""

from faultmaven.api.routes.admin import router as admin_router
from faultmaven.api.routes.admin_config import router as admin_config_router
from faultmaven.api.routes.sessions import router as sessions_router

__all__ = [
    "admin_router",
    "admin_config_router",
    "sessions_router",
]
