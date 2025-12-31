"""Hypothesis & Solution Management API Routes (TASK-026)

Purpose: REST API endpoints for hypothesis-driven debugging workflow.

This module provides 9 endpoints for Investigation Orchestrator:

Hypothesis Endpoints:
1. POST   /cases/{case_id}/hypotheses       - Create hypothesis
2. GET    /cases/{case_id}/hypotheses       - List hypotheses for case
3. GET    /hypotheses/{hypothesis_id}       - Get hypothesis details
4. PUT    /hypotheses/{hypothesis_id}       - Update hypothesis
5. DELETE /hypotheses/{hypothesis_id}       - Delete hypothesis

Solution Endpoints:
6. POST   /cases/{case_id}/solutions        - Create solution
7. GET    /cases/{case_id}/solutions        - List solutions for case
8. GET    /solutions/{solution_id}          - Get solution details
9. DELETE /solutions/{solution_id}          - Delete solution

Authentication:
- JWT Bearer token: Authorization: Bearer <token>

Design Reference: docs/working/TASK-026.md
"""

from datetime import datetime, timezone
from typing import List, Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status, Path
from pydantic import BaseModel, Field

from faultmaven.models.api_hypothesis import (
    HypothesisCreateRequest,
    HypothesisUpdateRequest,
    HypothesisResponse,
    SolutionCreateRequest,
    SolutionResponse,
)
from faultmaven.models.auth import DevUser
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import (
    get_investigation_orchestrator,
    get_tenant_provider,
)
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.services.domain.investigation_orchestrator import InvestigationOrchestrator
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import (
    ValidationException,
    NotFoundError,
    ConflictError,
)


# Create router
router = APIRouter(prefix="", tags=["hypotheses", "solutions"])

# Set up logging
logger = logging.getLogger(__name__)


# ============================================================
# API Response Models
# ============================================================


class HypothesisListResponse(BaseModel):
    """API response for list of hypotheses."""
    hypotheses: List[HypothesisResponse]
    total: int
    case_id: str


class SolutionListResponse(BaseModel):
    """API response for list of solutions."""
    solutions: List[SolutionResponse]
    total: int
    case_id: str


# ============================================================
# Helper Functions
# ============================================================


def check_orchestrator_available(orchestrator: Optional[InvestigationOrchestrator]) -> InvestigationOrchestrator:
    """Check if investigation orchestrator is available and raise appropriate error if not."""
    if orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail="Investigation service unavailable"
        )
    return orchestrator


def check_tenant_provider_available(tenant_provider: Optional[TenantProvider]) -> TenantProvider:
    """Check if tenant provider is available."""
    if tenant_provider is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant provider unavailable"
        )
    return tenant_provider


async def validate_organization_access(
    tenant_provider: TenantProvider,
    current_user: DevUser,
) -> str:
    """Validate user has access to the organization context.

    Args:
        tenant_provider: TenantProvider for organization resolution
        current_user: Authenticated user

    Returns:
        organization_id: Resolved organization ID

    Raises:
        HTTPException: 403 if user lacks organization access
    """
    try:
        # Get current organization context
        organization = await tenant_provider.get_current_organization(current_user)
        return organization.organization_id
    except Exception as e:
        logger.error(f"Organization validation failed: {e}")
        raise HTTPException(
            status_code=403,
            detail="Unable to validate organization access"
        )


# ============================================================
# Hypothesis Endpoints
# ============================================================


@router.post(
    "/cases/{case_id}/hypotheses",
    response_model=HypothesisResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create hypothesis",
    description="Create a new investigation hypothesis for a case"
)
@trace("api_create_hypothesis")
async def create_hypothesis(
    case_id: str = Path(..., description="Case ID to create hypothesis for"),
    request: HypothesisCreateRequest = ...,
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Create a new investigation hypothesis.

    Hypotheses represent potential root causes during investigation.
    They start in 'testing' status and can be validated/refuted based on evidence.

    Args:
        case_id: Case ID to create hypothesis for
        request: Hypothesis creation request
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Returns:
        HypothesisResponse: Created hypothesis

    Raises:
        403: Organization access denied
        422: Validation error
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # Create hypothesis via orchestrator
        hypothesis_dict = await orchestrator.create_hypothesis(
            case_id=case_id,
            organization_id=organization_id,
            description=request.description,
            created_by=current_user.user_id,
            confidence=float(request.confidence_score) if request.confidence_score is not None else 0.5,
            supporting_evidence_ids=request.supporting_evidence_ids or [],
            metadata=request.metadata or {},
        )

        logger.info(
            f"Created hypothesis {hypothesis_dict['hypothesis_id']} for case {case_id}",
            extra={
                "hypothesis_id": hypothesis_dict["hypothesis_id"],
                "case_id": case_id,
                "user_id": current_user.user_id,
                "organization_id": organization_id,
            }
        )

        return HypothesisResponse.from_dict(hypothesis_dict)

    except ValidationException as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create hypothesis: {e}")
        raise HTTPException(status_code=500, detail="Failed to create hypothesis")


@router.get(
    "/cases/{case_id}/hypotheses",
    response_model=HypothesisListResponse,
    summary="List hypotheses for case",
    description="Get all hypotheses for a specific case"
)
@trace("api_list_hypotheses")
async def list_hypotheses(
    case_id: str = Path(..., description="Case ID to list hypotheses for"),
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """List all hypotheses for a case.

    Returns hypotheses ordered by creation date (newest first).

    Args:
        case_id: Case ID to list hypotheses for
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Returns:
        HypothesisListResponse: List of hypotheses

    Raises:
        403: Organization access denied
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # List hypotheses via orchestrator
        hypotheses = await orchestrator.list_hypotheses_for_case(
            case_id=case_id,
            organization_id=organization_id,
        )

        return HypothesisListResponse(
            hypotheses=[HypothesisResponse.from_domain(h) for h in hypotheses],
            total=len(hypotheses),
            case_id=case_id,
        )

    except Exception as e:
        logger.error(f"Failed to list hypotheses: {e}")
        raise HTTPException(status_code=500, detail="Failed to list hypotheses")


@router.get(
    "/hypotheses/{hypothesis_id}",
    response_model=HypothesisResponse,
    summary="Get hypothesis details",
    description="Get details of a specific hypothesis"
)
@trace("api_get_hypothesis")
async def get_hypothesis(
    hypothesis_id: str = Path(..., description="Hypothesis ID"),
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Get hypothesis details.

    Args:
        hypothesis_id: Hypothesis ID
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Returns:
        HypothesisResponse: Hypothesis details

    Raises:
        403: Organization access denied
        404: Hypothesis not found
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # Get hypothesis via orchestrator
        hypothesis = await orchestrator.get_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        return HypothesisResponse.from_domain(hypothesis)

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to get hypothesis: {e}")
        raise HTTPException(status_code=500, detail="Failed to get hypothesis")


@router.put(
    "/hypotheses/{hypothesis_id}",
    response_model=HypothesisResponse,
    summary="Update hypothesis",
    description="Update hypothesis details or status"
)
@trace("api_update_hypothesis")
async def update_hypothesis(
    hypothesis_id: str = Path(..., description="Hypothesis ID"),
    request: HypothesisUpdateRequest = ...,
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Update hypothesis details or status.

    Can update title, description, confidence, status, or evidence references.
    Status transitions are validated by confidence thresholds:
    - Validated: requires confidence >= 0.7
    - Refuted: requires confidence <= 0.3

    Args:
        hypothesis_id: Hypothesis ID
        request: Update request
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Returns:
        HypothesisResponse: Updated hypothesis

    Raises:
        403: Organization access denied
        404: Hypothesis not found
        422: Validation error (invalid status transition)
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # Update hypothesis via orchestrator
        hypothesis = await orchestrator.update_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
            updated_by=current_user.user_id,
            title=request.title,
            description=request.description,
            confidence=request.confidence,
            status=request.status,
            evidence_supporting=request.evidence_supporting,
            evidence_against=request.evidence_against,
        )

        logger.info(
            f"Updated hypothesis {hypothesis_id}",
            extra={
                "hypothesis_id": hypothesis_id,
                "user_id": current_user.user_id,
                "organization_id": organization_id,
            }
        )

        return HypothesisResponse.from_domain(hypothesis)

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationException as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update hypothesis: {e}")
        raise HTTPException(status_code=500, detail="Failed to update hypothesis")


@router.delete(
    "/hypotheses/{hypothesis_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete hypothesis",
    description="Delete a hypothesis"
)
@trace("api_delete_hypothesis")
async def delete_hypothesis(
    hypothesis_id: str = Path(..., description="Hypothesis ID"),
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Delete a hypothesis.

    Args:
        hypothesis_id: Hypothesis ID
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Raises:
        403: Organization access denied
        404: Hypothesis not found
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # Delete hypothesis via orchestrator
        await orchestrator.delete_hypothesis(
            hypothesis_id=hypothesis_id,
            organization_id=organization_id,
        )

        logger.info(
            f"Deleted hypothesis {hypothesis_id}",
            extra={
                "hypothesis_id": hypothesis_id,
                "user_id": current_user.user_id,
                "organization_id": organization_id,
            }
        )

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to delete hypothesis: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete hypothesis")


# ============================================================
# Solution Endpoints
# ============================================================


@router.post(
    "/cases/{case_id}/solutions",
    response_model=SolutionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create solution",
    description="Create a new solution for a case"
)
@trace("api_create_solution")
async def create_solution(
    case_id: str = Path(..., description="Case ID to create solution for"),
    request: SolutionCreateRequest = ...,
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Create a new solution.

    Solutions represent fixes or workarounds for case issues.
    Can optionally be linked to validated hypotheses.

    Args:
        case_id: Case ID to create solution for
        request: Solution creation request
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Returns:
        SolutionResponse: Created solution

    Raises:
        403: Organization access denied
        422: Validation error
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # Create solution via orchestrator
        solution = await orchestrator.create_solution(
            case_id=case_id,
            organization_id=organization_id,
            title=request.title,
            description=request.description,
            created_by=current_user.user_id,
            hypothesis_id=request.hypothesis_id,
            implementation_steps=request.implementation_steps or [],
        )

        logger.info(
            f"Created solution {solution.solution_id} for case {case_id}",
            extra={
                "solution_id": solution.solution_id,
                "case_id": case_id,
                "user_id": current_user.user_id,
                "organization_id": organization_id,
                "hypothesis_id": request.hypothesis_id,
            }
        )

        return SolutionResponse.from_domain(solution)

    except ValidationException as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create solution: {e}")
        raise HTTPException(status_code=500, detail="Failed to create solution")


@router.get(
    "/cases/{case_id}/solutions",
    response_model=SolutionListResponse,
    summary="List solutions for case",
    description="Get all solutions for a specific case"
)
@trace("api_list_solutions")
async def list_solutions(
    case_id: str = Path(..., description="Case ID to list solutions for"),
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """List all solutions for a case.

    Returns solutions ordered by creation date (newest first).

    Args:
        case_id: Case ID to list solutions for
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Returns:
        SolutionListResponse: List of solutions

    Raises:
        403: Organization access denied
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # List solutions via orchestrator
        solutions = await orchestrator.list_solutions_for_case(
            case_id=case_id,
            organization_id=organization_id,
        )

        return SolutionListResponse(
            solutions=[SolutionResponse.from_domain(s) for s in solutions],
            total=len(solutions),
            case_id=case_id,
        )

    except Exception as e:
        logger.error(f"Failed to list solutions: {e}")
        raise HTTPException(status_code=500, detail="Failed to list solutions")


@router.get(
    "/solutions/{solution_id}",
    response_model=SolutionResponse,
    summary="Get solution details",
    description="Get details of a specific solution"
)
@trace("api_get_solution")
async def get_solution(
    solution_id: str = Path(..., description="Solution ID"),
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Get solution details.

    Args:
        solution_id: Solution ID
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Returns:
        SolutionResponse: Solution details

    Raises:
        403: Organization access denied
        404: Solution not found
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # Get solution via orchestrator
        solution = await orchestrator.get_solution(
            solution_id=solution_id,
            organization_id=organization_id,
        )

        return SolutionResponse.from_domain(solution)

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to get solution: {e}")
        raise HTTPException(status_code=500, detail="Failed to get solution")


@router.delete(
    "/solutions/{solution_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete solution",
    description="Delete a solution"
)
@trace("api_delete_solution")
async def delete_solution(
    solution_id: str = Path(..., description="Solution ID"),
    current_user: DevUser = Depends(require_authentication),
    orchestrator: InvestigationOrchestrator = Depends(get_investigation_orchestrator),
    tenant_provider: TenantProvider = Depends(get_tenant_provider),
):
    """Delete a solution.

    Args:
        solution_id: Solution ID
        current_user: Authenticated user (from JWT)
        orchestrator: Investigation orchestrator service
        tenant_provider: Tenant provider for multi-tenant isolation

    Raises:
        403: Organization access denied
        404: Solution not found
        503: Service unavailable
    """
    check_orchestrator_available(orchestrator)
    check_tenant_provider_available(tenant_provider)

    # Validate organization access
    organization_id = await validate_organization_access(tenant_provider, current_user)

    try:
        # Delete solution via orchestrator
        await orchestrator.delete_solution(
            solution_id=solution_id,
            organization_id=organization_id,
        )

        logger.info(
            f"Deleted solution {solution_id}",
            extra={
                "solution_id": solution_id,
                "user_id": current_user.user_id,
                "organization_id": organization_id,
            }
        )

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to delete solution: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete solution")
