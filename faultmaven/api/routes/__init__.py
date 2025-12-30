"""API Routes Package (TASK-014, TASK-016, TASK-017, TASK-019)

This package provides FastAPI router modules for:
- auth: JWT authentication endpoints (TASK-017)
- admin: Admin user management endpoints (TASK-019)
- users: Organization user management endpoints (TASK-019)
- cases: Case management endpoints
- sessions: Investigation session endpoints
- evidence: Evidence artifact endpoints
- agent: AI agent execution endpoints (TASK-016)
"""

from faultmaven.api.routes.auth import router as auth_router
from faultmaven.api.routes.admin import router as admin_router
from faultmaven.api.routes.users import router as users_router
from faultmaven.api.routes.cases import router as cases_router
from faultmaven.api.routes.sessions import router as sessions_router
from faultmaven.api.routes.evidence import router as evidence_router
from faultmaven.api.routes.agent import router as agent_router

__all__ = [
    "auth_router",
    "admin_router",
    "users_router",
    "cases_router",
    "sessions_router",
    "evidence_router",
    "agent_router",
]
