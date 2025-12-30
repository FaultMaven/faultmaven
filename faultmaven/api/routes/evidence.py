"""Evidence Artifact API Routes (TASK-014)

Purpose: FastAPI routes for evidence artifact management operations.

Endpoints:
- POST   /api/v1/cases/{case_id}/evidence           - Upload evidence
- GET    /api/v1/cases/{case_id}/evidence           - List evidence for case
- GET    /api/v1/cases/{case_id}/evidence/{evidence_id}           - Get evidence metadata
- GET    /api/v1/cases/{case_id}/evidence/{evidence_id}/download  - Download evidence file
- PATCH  /api/v1/cases/{case_id}/evidence/{evidence_id}           - Update evidence metadata
- DELETE /api/v1/cases/{case_id}/evidence/{evidence_id}           - Delete evidence
- POST   /api/v1/cases/{case_id}/evidence/{evidence_id}/set-primary - Set primary evidence

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile, status
from fastapi.responses import StreamingResponse
import io

from faultmaven.api.dependencies import get_evidence_artifact_service
from faultmaven.api.models import (
    EvidenceListResponse,
    EvidenceResponse,
    EvidenceUpdateRequest,
)
from faultmaven.exceptions import NotFoundError
from faultmaven.models.evidence_artifact import EvidenceArtifactType
from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService


router = APIRouter(prefix="/api/v1/cases/{case_id}/evidence", tags=["Evidence"])


@router.post("", response_model=EvidenceResponse, status_code=status.HTTP_201_CREATED)
async def upload_evidence(
    case_id: str,
    file: UploadFile = File(...),
    evidence_type: EvidenceArtifactType = Form(...),
    description: Optional[str] = Form(None),
    is_primary: bool = Form(False),
    organization_id: str = Header(..., alias="X-Organization-ID"),
    user_id: str = Header(..., alias="X-User-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Upload evidence artifact for case.

    Uploads a file as evidence for a case. Supports any file type.
    The file is stored and associated with the specified case.

    This is a multipart form upload:
    - file: The file to upload (required)
    - evidence_type: Type of evidence (required)
    - description: Optional description
    - is_primary: Whether this is the primary evidence (default: false)

    Headers:
        X-Organization-ID: Organization identifier (required)
        X-User-ID: User identifier (required)

    Args:
        case_id: Case to attach evidence to
        file: Uploaded file
        evidence_type: Type of evidence artifact
        description: Optional description
        is_primary: Whether this is primary evidence
        organization_id: Organization owning the case
        user_id: User uploading the evidence
        evidence_service: Injected evidence service

    Returns:
        Created evidence artifact metadata

    Raises:
        404: Case not found
        403: Not authorized
        422: Validation error
        500: File storage error

    Example:
        curl -X POST "http://localhost:8000/api/v1/cases/CASE-123/evidence" \\
          -H "X-Organization-ID: org-456" \\
          -H "X-User-ID: user-789" \\
          -F "file=@screenshot.png" \\
          -F "evidence_type=screenshot" \\
          -F "description=Login error screenshot"
    """
    # Read file content
    file_data = await file.read()

    # Get mime type from file or default
    mime_type = file.content_type or "application/octet-stream"

    # Get filename
    original_filename = file.filename or "unnamed_file"

    evidence = await evidence_service.upload_evidence(
        case_id=case_id,
        organization_id=organization_id,
        user_id=user_id,
        file_data=file_data,
        original_filename=original_filename,
        mime_type=mime_type,
        evidence_type=evidence_type,
        description=description,
        is_primary=is_primary,
        metadata=None,
    )

    return EvidenceResponse.from_domain(evidence)


@router.get("/{evidence_id}", response_model=EvidenceResponse)
async def get_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Get evidence metadata by ID.

    Retrieves metadata for a specific evidence artifact.
    Use the /download endpoint to get the actual file content.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Case the evidence belongs to
        evidence_id: Unique evidence identifier
        organization_id: Organization owning the case
        evidence_service: Injected evidence service

    Returns:
        Evidence artifact metadata

    Raises:
        404: Evidence not found
        403: Not authorized
    """
    evidence = await evidence_service.get_evidence(evidence_id, organization_id)

    if not evidence:
        raise NotFoundError("Evidence", evidence_id)

    # Verify evidence belongs to the specified case
    if evidence.case_id != case_id:
        raise NotFoundError("Evidence", evidence_id)

    return EvidenceResponse.from_domain(evidence)


@router.get("/{evidence_id}/download")
async def download_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> StreamingResponse:
    """Download evidence file.

    Downloads the actual file content for an evidence artifact.
    Returns a streaming response with appropriate content-type
    and content-disposition headers.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Case the evidence belongs to
        evidence_id: Unique evidence identifier
        organization_id: Organization owning the case
        evidence_service: Injected evidence service

    Returns:
        Streaming response with file content

    Raises:
        404: Evidence not found
        403: Not authorized
        500: File read error

    Example Response Headers:
        content-type: image/png
        content-disposition: attachment; filename="screenshot.png"
    """
    # Verify evidence belongs to the case first
    evidence = await evidence_service.get_evidence(evidence_id, organization_id)
    if not evidence or evidence.case_id != case_id:
        raise NotFoundError("Evidence", evidence_id)

    # Download the file
    file_data, original_filename, mime_type = await evidence_service.download_evidence(
        evidence_id=evidence_id,
        organization_id=organization_id,
    )

    # Create streaming response
    return StreamingResponse(
        io.BytesIO(file_data),
        media_type=mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{original_filename}"',
            "Content-Length": str(len(file_data)),
        },
    )


@router.get("", response_model=List[EvidenceResponse])
async def list_evidence(
    case_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_type: Optional[EvidenceArtifactType] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> List[EvidenceResponse]:
    """List evidence for case.

    Retrieves all evidence artifacts for a case with optional filtering.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Query Parameters:
        evidence_type: Filter by evidence type (screenshot, log_file, etc.)
        limit: Maximum number of results (1-100, default 50)
        offset: Pagination offset (default 0)

    Args:
        case_id: Case to list evidence for
        organization_id: Organization owning the case
        evidence_type: Optional type filter
        limit: Page size
        offset: Pagination offset
        evidence_service: Injected evidence service

    Returns:
        List of evidence artifacts for the case

    Raises:
        404: Case not found
        403: Not authorized
    """
    evidence_list = await evidence_service.list_evidence_by_case(
        case_id=case_id,
        organization_id=organization_id,
        evidence_type=evidence_type,
        limit=limit,
        offset=offset,
    )

    return [EvidenceResponse.from_domain(evidence) for evidence in evidence_list]


@router.patch("/{evidence_id}", response_model=EvidenceResponse)
async def update_evidence(
    case_id: str,
    evidence_id: str,
    request: EvidenceUpdateRequest,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Update evidence metadata.

    Updates metadata for an evidence artifact. Only description,
    is_primary, and metadata fields can be updated. The file
    content cannot be modified (must re-upload).

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Case the evidence belongs to
        evidence_id: Unique evidence identifier
        request: Fields to update
        organization_id: Organization owning the case
        evidence_service: Injected evidence service

    Returns:
        Updated evidence artifact metadata

    Raises:
        404: Evidence not found
        403: Not authorized
        422: Validation error
    """
    # Verify evidence belongs to the case first
    evidence = await evidence_service.get_evidence(evidence_id, organization_id)
    if not evidence or evidence.case_id != case_id:
        raise NotFoundError("Evidence", evidence_id)

    # Build updates dict from non-None fields
    updates = {}
    if request.description is not None:
        updates["description"] = request.description
    if request.is_primary is not None:
        updates["is_primary"] = request.is_primary
    if request.metadata is not None:
        updates["metadata"] = request.metadata

    if not updates:
        # If no updates provided, just return current evidence
        return EvidenceResponse.from_domain(evidence)

    updated_evidence = await evidence_service.update_evidence(
        evidence_id=evidence_id,
        organization_id=organization_id,
        updates=updates,
    )

    return EvidenceResponse.from_domain(updated_evidence)


@router.delete("/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> None:
    """Delete evidence artifact and file.

    Permanently deletes an evidence artifact and its associated file.
    This operation cannot be undone.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Case the evidence belongs to
        evidence_id: Unique evidence identifier
        organization_id: Organization owning the case
        evidence_service: Injected evidence service

    Raises:
        403: Not authorized
        404: Evidence not found
    """
    # Verify evidence belongs to the case first
    evidence = await evidence_service.get_evidence(evidence_id, organization_id)
    if not evidence or evidence.case_id != case_id:
        raise NotFoundError("Evidence", evidence_id)

    deleted = await evidence_service.delete_evidence(evidence_id, organization_id)

    if not deleted:
        raise NotFoundError("Evidence", evidence_id)


@router.post("/{evidence_id}/set-primary", response_model=EvidenceResponse)
async def set_primary_evidence(
    case_id: str,
    evidence_id: str,
    organization_id: str = Header(..., alias="X-Organization-ID"),
    evidence_service: APIEvidenceArtifactService = Depends(get_evidence_artifact_service),
) -> EvidenceResponse:
    """Set artifact as primary evidence for case.

    Marks this evidence as the primary/featured evidence for the case.
    Any existing primary evidence will be automatically unset.
    Only one evidence artifact can be primary at a time.

    Headers:
        X-Organization-ID: Organization identifier (required)

    Args:
        case_id: Case the evidence belongs to
        evidence_id: Unique evidence identifier
        organization_id: Organization owning the case
        evidence_service: Injected evidence service

    Returns:
        Updated evidence artifact with is_primary=true

    Raises:
        404: Evidence not found
        403: Not authorized
    """
    # Verify evidence belongs to the case first
    evidence = await evidence_service.get_evidence(evidence_id, organization_id)
    if not evidence or evidence.case_id != case_id:
        raise NotFoundError("Evidence", evidence_id)

    updated_evidence = await evidence_service.set_primary_evidence(
        evidence_id=evidence_id,
        organization_id=organization_id,
    )

    return EvidenceResponse.from_domain(updated_evidence)
