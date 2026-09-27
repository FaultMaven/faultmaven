"""Uploaded-file and evidence-detail routes (fm#1707).

Read access to a case's uploaded files and derived evidence: the uploaded
files list and detail, and the evidence list and detail (Phase 2
evidence-to-file linkage). Split out of ``case/api/routes.py`` as its own
sub-router (A6); ``dependencies.py`` holds the DI accessor, guard and
enum-formatting helper every handler here uses.
"""

import logging
from typing import Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_case_service
from faultmaven.exceptions import NotFoundError, PermissionDeniedException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.api_models import (
    CaseEvidenceListResponse,
    DerivedEvidenceSummary,
    EvidenceDetailsResponse,
    RelatedHypothesis,
    SourceFileReference,
    UploadedFileDetailsResponse,
    UploadedFileMetadata,
    UploadedFilesList,
)
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _safe_enum_value,
    check_case_service_available,
)

router = APIRouter(prefix="/cases", tags=["cases"])


logger = logging.getLogger(__name__)


# Case archive endpoints removed in storage redesign 2026-05.
# Postpone path: the columns and feature are gone from the schema until
# archive UX is wired up as a deliberate epic (with retention policy,
# scheduled archival, list-view filter UI, etc.). Reintroduce the routes
# at that point, not before.
# ============================================================
# Uploaded Files / Evidence Endpoints
# ============================================================


@router.get(
    "/{case_id}/uploaded-files",
    response_model=UploadedFilesList,
    operation_id="list_uploaded_files",
)
@trace("api_list_uploaded_files")
async def list_uploaded_files(
    case_id: str,
    response: Response,
    limit: int = Query(
        50, ge=1, le=100, description="Maximum number of files to return"
    ),
    offset: int = Query(
        0, ge=0, description="Number of files to skip (for pagination)"
    ),
    sort_by: str = Query(
        "uploaded_at_turn", description="Sort field: uploaded_at_turn | filename | size"
    ),
    sort_order: str = Query("desc", description="Sort direction: asc | desc"),
    case_service=Depends(get_case_service),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    List uploaded files for a case with pagination.

    Returns:
        Paginated list of file metadata with AI analysis status
    """
    try:
        # Get case with access control
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Get uploaded files list (not evidence - files exist in ALL phases)
        uploaded_files_list = case.uploaded_files

        # Sort uploaded files
        reverse = sort_order == "desc"
        if sort_by == "uploaded_at_turn":
            uploaded_files_list = sorted(
                uploaded_files_list, key=lambda f: f.uploaded_at_turn, reverse=reverse
            )
        elif sort_by == "filename":
            uploaded_files_list = sorted(
                uploaded_files_list, key=lambda f: f.filename, reverse=reverse
            )
        elif sort_by == "size":
            uploaded_files_list = sorted(
                uploaded_files_list, key=lambda f: f.size_bytes, reverse=reverse
            )

        # Paginate
        total_count = len(uploaded_files_list)
        paginated_files = uploaded_files_list[offset : offset + limit]

        # Convert to response models
        # One `out_of_band_turns` for the whole page rather than per row: the
        # formula lives on the Case and bisects this list, and rebuilding it per
        # file is the only cost that scales with the page size.
        asides = case.out_of_band_turns
        files = [
            UploadedFileMetadata.from_uploaded_file(
                f,
                investigation_turn=case.investigation_turn_at(
                    f.uploaded_at_turn, asides=asides
                ),
            )
            for f in paginated_files
        ]

        # Set pagination header (required by API contract)
        response.headers["X-Total-Count"] = str(total_count)

        return UploadedFilesList(
            files=files, total_count=total_count, limit=limit, offset=offset
        )

    except HTTPException:
        raise
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to list uploaded files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list uploaded files")


@router.get(
    "/{case_id}/uploaded-files/{file_id}",
    response_model=UploadedFileDetailsResponse,
    summary="Get uploaded file details with derived evidence",
    description="Retrieve detailed information about an uploaded file including all evidence derived from it and hypothesis linkage.",
    operation_id="get_uploaded_file_details",
)
async def get_uploaded_file_details(
    case_id: str = Path(..., description="Case ID"),
    file_id: str = Path(..., description="File ID"),
    current_user: UserDTO = Depends(require_authentication),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
):
    """
    GET /api/v1/cases/{case_id}/uploaded-files/{file_id}

    Returns comprehensive file details including:
    - File metadata (name, size, upload time)
    - List of evidence derived from this file
    - Hypothesis linkage for each evidence piece
    """
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        # ICaseService.get_case applies ownership-based access control and
        # returns None for both "not found" and "not owned" — both surface as 404.
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        # Find the uploaded file
        uploaded_file = next(
            (f for f in case.uploaded_files if f.file_id == file_id), None
        )
        if not uploaded_file:
            raise HTTPException(
                status_code=404, detail=f"File {file_id} not found in case {case_id}"
            )

        # Find all evidence derived from this file via the canonical FK
        # (Evidence.source_file_id → UploadedFile.file_id). The pre-redesign
        # `content_ref == content_ref` matching was a polymorphism workaround
        # eliminated by the schema redesign.
        derived_evidence = []
        first_summary: Optional[str] = None
        # Built once for the loop below, for the same reason as the file list.
        asides = case.out_of_band_turns
        for evidence in case.evidence:
            if evidence.source_file_id != uploaded_file.file_id:
                continue
            # Find hypotheses related to this evidence (junction list)
            related_hypothesis_ids = [
                hyp.hypothesis_id
                for hyp in case.hypotheses.values()
                if any(
                    link.evidence_id == evidence.evidence_id
                    for link in hyp.evidence_links
                )
            ]
            derived_evidence.append(
                DerivedEvidenceSummary(
                    evidence_id=evidence.evidence_id,
                    summary=evidence.summary,
                    category=_safe_enum_value(evidence.category),
                    collected_at_turn=evidence.collected_at_turn,
                    investigation_turn=case.investigation_turn_at(
                        evidence.collected_at_turn, asides=asides
                    ),
                    source_type=_safe_enum_value(evidence.source_type),
                    primary_purpose=evidence.primary_purpose,
                    related_hypothesis_ids=related_hypothesis_ids,
                )
            )
            if first_summary is None:
                first_summary = evidence.summary

        # Format file size for display
        size_bytes = uploaded_file.size_bytes
        if size_bytes < 1024:
            size_display = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_display = f"{size_bytes / 1024:.1f} KB"
        else:
            size_display = f"{size_bytes / (1024 * 1024):.1f} MB"

        return UploadedFileDetailsResponse(
            file_id=uploaded_file.file_id,
            filename=uploaded_file.filename,
            size_bytes=uploaded_file.size_bytes,
            size_display=size_display,
            content_type=uploaded_file.content_type,
            content_hash=uploaded_file.content_hash,
            uploaded_at_turn=uploaded_file.uploaded_at_turn,
            investigation_turn=case.investigation_turn_at(
                uploaded_file.uploaded_at_turn, asides=asides
            ),
            uploaded_at=uploaded_file.uploaded_at,
            upload_source=uploaded_file.upload_source,
            summary=first_summary,
            derived_evidence=derived_evidence,
            evidence_count=len(derived_evidence),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get file details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get file details")


def _build_evidence_response(
    case, evidence, case_id: str, asides: Optional[Sequence[int]] = None
) -> EvidenceDetailsResponse:
    """Build an EvidenceDetailsResponse from a domain Evidence + its parent Case.

    Resolves the source-file reference via the canonical FK and walks the
    hypothesis-evidence junction once to collect every related hypothesis.
    Shared by ``list_case_evidence`` and ``get_evidence_details`` so both
    endpoints produce identical row shapes.
    """
    matched_file = case.find_uploaded_file(evidence.source_file_id)
    source_file = (
        SourceFileReference(
            file_id=matched_file.file_id,
            filename=matched_file.filename,
            uploaded_at_turn=matched_file.uploaded_at_turn,
            investigation_turn=case.investigation_turn_at(
                matched_file.uploaded_at_turn, asides=asides
            ),
        )
        if matched_file
        else None
    )

    related_hypotheses = []
    for hypothesis in case.hypotheses.values():
        for link in hypothesis.evidence_links:
            if link.evidence_id != evidence.evidence_id:
                continue
            related_hypotheses.append(
                RelatedHypothesis(
                    hypothesis_id=hypothesis.hypothesis_id,
                    statement=hypothesis.statement,
                    stance=(
                        link.stance.value
                        if hasattr(link.stance, "value")
                        else str(link.stance)
                    ),
                )
            )

    return EvidenceDetailsResponse(
        evidence_id=evidence.evidence_id,
        case_id=case_id,
        summary=evidence.summary,
        category=_safe_enum_value(evidence.category),
        primary_purpose=evidence.primary_purpose,
        collected_at_turn=evidence.collected_at_turn,
        investigation_turn=case.investigation_turn_at(
            evidence.collected_at_turn, asides=asides
        ),
        collected_at=evidence.collected_at,
        collected_by=evidence.collected_by,
        source_file=source_file,
        related_hypotheses=related_hypotheses,
        extract=evidence.extract,
        analysis=evidence.analysis,
    )


@router.get(
    "/{case_id}/evidence",
    response_model=CaseEvidenceListResponse,
    summary="List all evidence for a case",
    description="Retrieve all evidence records for a case, each with source-file reference and hypothesis linkage.",
    operation_id="list_case_evidence",
)
async def list_case_evidence(
    case_id: str = Path(..., description="Case ID"),
    current_user: UserDTO = Depends(require_authentication),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
):
    """
    GET /api/v1/cases/{case_id}/evidence

    Returns the full evidence list for a case. Each item carries the
    same shape as the single-evidence endpoint so the UI can render a
    list view and a detail panel from one payload.
    """
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        # Built ONCE for the whole list. This endpoint does not paginate, so a
        # case with many evidence rows would otherwise walk and sort the turn
        # history twice per row — the cost the two paginated endpoints already
        # hoist out, skipped on the one where it actually scales.
        asides = case.out_of_band_turns
        evidence_items = [
            _build_evidence_response(case, evidence, case_id, asides=asides)
            for evidence in case.evidence
        ]

        return CaseEvidenceListResponse(
            case_id=case_id,
            total_count=len(evidence_items),
            evidence=evidence_items,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list evidence: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list evidence")


@router.get(
    "/{case_id}/evidence/{evidence_id}",
    response_model=EvidenceDetailsResponse,
    summary="Get evidence details with source file",
    description="Retrieve detailed evidence information including source file reference and hypothesis linkage.",
)
async def get_evidence_details(
    case_id: str = Path(..., description="Case ID"),
    evidence_id: str = Path(..., description="Evidence ID"),
    current_user: UserDTO = Depends(require_authentication),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
):
    """
    GET /api/v1/cases/{case_id}/evidence/{evidence_id}

    Returns comprehensive evidence details including:
    - Evidence metadata and content
    - Source file reference (if derived from upload)
    - Related hypotheses with stance (SUPPORTS/REFUTES/NEUTRAL)
    """
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        evidence = next(
            (e for e in case.evidence if e.evidence_id == evidence_id), None
        )
        if not evidence:
            raise HTTPException(
                status_code=404,
                detail=f"Evidence {evidence_id} not found in case {case_id}",
            )

        return _build_evidence_response(case, evidence, case_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get evidence details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get evidence details")


# ============================================================
# REMOVED ENDPOINTS: Download and Delete
# ============================================================
# Rationale: Each file upload is a conversational turn. Downloading files users
# already have is an anti-pattern, and deleting would break conversation history
# integrity (similar to deleting individual chat messages).
# Only "View Analysis" feature remains for transparency and troubleshooting.
#
# Removed endpoints (cleaned up 2025-01-XX):
# - GET /{case_id}/uploaded-files/{file_id}/download
# - DELETE /{case_id}/uploaded-files/{file_id}
# ============================================================
