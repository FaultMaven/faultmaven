"""API Routes Package.

Legacy routes (admin, admin_config) remain here.
Module routes live in their respective modules (agent, case, evidence, knowledge, report).
"""

from faultmaven.api.routes.admin import router as admin_router
from faultmaven.api.routes.admin_config import router as admin_config_router

__all__ = [
    "admin_router",
    "admin_config_router",
]
