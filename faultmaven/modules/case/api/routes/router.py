"""Aggregate case-API router (fm#1707).

Includes every sub-router in this package, in the same order the routes were
registered on the single ``case/api/routes.py`` router before the split, so
the served route table and ``openapi.json`` are unchanged (A6). This module
carries no prefix of its own: each sub-router already declares
``APIRouter(prefix="/cases", tags=["cases"])`` (FastAPI 0.136 raises "Prefix
and path cannot be both empty" if the prefix sat only here, because
``create_case``/``list_cases`` register on path ``""``).
"""

from fastapi import APIRouter

from faultmaven.modules.case.api.routes.cases import router as cases_router
from faultmaven.modules.case.api.routes.conversation import (
    router as conversation_router,
)
from faultmaven.modules.case.api.routes.data import router as data_router
from faultmaven.modules.case.api.routes.evidence import router as evidence_router
from faultmaven.modules.case.api.routes.reports import router as reports_router
from faultmaven.modules.case.api.routes.sharing import router as sharing_router

router = APIRouter()
router.include_router(cases_router)
router.include_router(conversation_router)
router.include_router(data_router)
router.include_router(reports_router)
router.include_router(evidence_router)
router.include_router(sharing_router)
