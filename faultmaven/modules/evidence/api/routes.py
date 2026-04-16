"""Evidence API Routes

Implements 7 evidence endpoints from microservices architecture:
1. POST /api/v1/evidence - Upload evidence
2. GET /api/v1/evidence/{evidence_id} - Get evidence details
3. GET /api/v1/evidence/{evidence_id}/download - Download evidence file
4. DELETE /api/v1/evidence/{evidence_id} - Delete evidence
5. GET /api/v1/evidence - List all evidence
6. GET /api/v1/evidence/case/{case_id} - Get evidence for case
7. POST /api/v1/evidence/{evidence_id}/link - Link evidence to case
"""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import RedirectResponse

from faultmaven.api.v1.dependencies import get_current_user
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceLinkRequest,
    EvidenceListFilter,
)
from faultmaven.modules.evidence.domain.services import EvidenceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evidence", tags=["evidence"])


async def get_evidence_service(request: Request) -> Optional[EvidenceService]:
    """Get evidence service from app.state (Composition Root).

    Returns:
        EvidenceService if available, None if service not attached
    """
    return getattr(request.app.state, "evidence_service", None)


async def _require_case_not_terminal(service: EvidenceService, case_id: UUID) -> None:
    """Reject evidence operations targeting a terminal case."""
    repo = getattr(service, "case_repository", None)
    if not repo:
        return
    case = await repo.get(str(case_id))
    if case and case.is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot submit evidence to a closed case. Only questions about the case are allowed.",
        )


@router.post("", response_model=EvidenceArtifact, status_code=status.HTTP_201_CREATED)
async def upload_evidence(
    file: UploadFile = File(...),
    description: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),  # Comma-separated
    case_id: Optional[UUID] = Form(None),
    current_user: DevUser = Depends(get_current_user),
    service: EvidenceService = Depends(get_evidence_service),
) -> EvidenceArtifact:
    """Upload evidence file.

    Args:
        file: Evidence file to upload
        description: Optional description of the evidence
        tags: Optional comma-separated tags
        case_id: Optional case UUID to auto-link
        current_user: Authenticated user
        service: Evidence service

    Returns:
        Created evidence record
    """
    if case_id:
        await _require_case_not_terminal(service, case_id)

    tag_list = []
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    return await service.upload_evidence(
        file=file,
        uploaded_by=current_user.id,
        description=description,
        tags=tag_list,
        case_id=case_id,
    )


# NOTE: /case/{case_id} must be defined BEFORE /{evidence_id} routes
# to avoid FastAPI matching "case" as an evidence_id
@router.get("/case/{case_id}", response_model=List[EvidenceArtifact])
async def get_evidence_for_case(
    case_id: UUID,
    current_user: DevUser = Depends(get_current_user),
    service: EvidenceService = Depends(get_evidence_service),
) -> List[EvidenceArtifact]:
    """Get all evidence linked to a specific case.

    Args:
        case_id: Case UUID
        current_user: Authenticated user
        service: Evidence service

    Returns:
        List of evidence records for the case
    """
    filters = EvidenceListFilter(case_id=str(case_id), limit=200)
    evidence_list, _ = await service.list_evidence(filters)
    return evidence_list


@router.get("/{evidence_id}", response_model=EvidenceArtifact)
async def get_evidence(
    evidence_id: UUID,
    current_user: DevUser = Depends(get_current_user),
    service: Optional[EvidenceService] = Depends(get_evidence_service),
) -> EvidenceArtifact:
    """Get evidence details by ID.

    Args:
        evidence_id: Evidence UUID
        current_user: Authenticated user
        service: Evidence service

    Returns:
        Evidence record

    Raises:
        HTTPException: 404 if evidence not found, 503 if service unavailable
    """
    if not service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence service unavailable",
        )
    evidence = await service.get_evidence(evidence_id)
    if not evidence:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evidence {evidence_id} not found",
        )
    return evidence


@router.get("/{evidence_id}/download")
async def download_evidence(
    evidence_id: UUID,
    current_user: DevUser = Depends(get_current_user),
    service: Optional[EvidenceService] = Depends(get_evidence_service),
):
    """Download evidence file.

    Args:
        evidence_id: Evidence UUID
        current_user: Authenticated user
        service: Evidence service

    Returns:
        Redirect to download URL

    Raises:
        HTTPException: 404 if evidence not found, 503 if service unavailable
    """
    if not service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence service unavailable",
        )
    download_url = await service.get_file_url(evidence_id)
    if not download_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evidence {evidence_id} not found",
        )

    # Redirect to storage provider's download URL
    return RedirectResponse(url=download_url)


@router.delete("/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_evidence(
    evidence_id: UUID,
    current_user: DevUser = Depends(get_current_user),
    service: Optional[EvidenceService] = Depends(get_evidence_service),
):
    """Delete evidence file and record.

    Args:
        evidence_id: Evidence UUID
        current_user: Authenticated user
        service: Evidence service

    Raises:
        HTTPException: 404 if evidence not found, 503 if service unavailable
    """
    if not service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence service unavailable",
        )
    deleted = await service.delete_evidence(evidence_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evidence {evidence_id} not found",
        )


@router.get("", response_model=List[EvidenceArtifact])
async def list_evidence(
    case_id: Optional[UUID] = None,
    uploaded_by: Optional[UUID] = None,
    tags: Optional[str] = None,  # Comma-separated
    filename_contains: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: DevUser = Depends(get_current_user),
    service: EvidenceService = Depends(get_evidence_service),
) -> List[EvidenceArtifact]:
    """List evidence with optional filters.

    Args:
        case_id: Filter by case UUID
        uploaded_by: Filter by uploader user ID
        tags: Filter by tags (comma-separated)
        filename_contains: Filter by filename substring
        limit: Max results (default 50, max 200)
        offset: Pagination offset
        current_user: Authenticated user
        service: Evidence service

    Returns:
        List of evidence records
    """
    tag_list = None
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    filters = EvidenceListFilter(
        case_id=case_id,
        uploaded_by=uploaded_by,
        tags=tag_list,
        filename_contains=filename_contains,
        limit=min(limit, 200),
        offset=offset,
    )

    evidence_list, _ = await service.list_evidence(filters)
    return evidence_list


@router.post("/{evidence_id}/link", response_model=EvidenceArtifact)
async def link_evidence_to_case(
    evidence_id: UUID,
    link_request: EvidenceLinkRequest,
    current_user: DevUser = Depends(get_current_user),
    service: EvidenceService = Depends(get_evidence_service),
) -> EvidenceArtifact:
    """Link evidence to a case.

    Args:
        evidence_id: Evidence UUID
        link_request: Case ID to link to
        current_user: Authenticated user
        service: Evidence service

    Returns:
        Updated evidence record

    Raises:
        HTTPException: 404 if evidence not found
    """
    try:
        await _require_case_not_terminal(service, link_request.case_id)
        return await service.link_to_case(evidence_id, link_request.case_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
