"""API Routes Package (TASK-014)

This package provides FastAPI router modules for:
- cases: Case management endpoints
- sessions: Investigation session endpoints
- evidence: Evidence artifact endpoints
"""

from faultmaven.api.routes.cases import router as cases_router
from faultmaven.api.routes.sessions import router as sessions_router
from faultmaven.api.routes.evidence import router as evidence_router

__all__ = [
    "cases_router",
    "sessions_router",
    "evidence_router",
]
