"""API Routes Package (TASK-014, TASK-016)

This package provides FastAPI router modules for:
- cases: Case management endpoints
- sessions: Investigation session endpoints
- evidence: Evidence artifact endpoints
- agent: AI agent execution endpoints (TASK-016)
"""

from faultmaven.api.routes.cases import router as cases_router
from faultmaven.api.routes.sessions import router as sessions_router
from faultmaven.api.routes.evidence import router as evidence_router
from faultmaven.api.routes.agent import router as agent_router

__all__ = [
    "cases_router",
    "sessions_router",
    "evidence_router",
    "agent_router",
]
