"""API routes for document-to-runbook conversion and conversion management.

Case→runbook generation is chat-initiated only (the Copilot RESOLVED
affordance → ``MilestoneEngine._handle_runbook_creation``); there is no
case-conversion HTTP endpoint. The Dashboard is view-only for case runbooks.

Endpoints:
- POST   /knowledge/convert                                    Upload and convert document
- GET    /knowledge/conversions                                List user's conversions
- GET    /knowledge/conversions/{id}                           Get conversion details
- GET    /knowledge/conversions/by-case/{case_id}              Get conversion for a case
- PUT    /knowledge/conversions/{id}/drafts/{draft_id}         Edit draft
- POST   /knowledge/drafts/verify-batch                        Batch activate
- POST   /knowledge/conversions/{id}/drafts/{draft_id}/verify  Activate (verify + ingest)
- DELETE /knowledge/conversions/{id}/drafts/{draft_id}         Delete draft
"""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from faultmaven.exceptions import FaultMavenException
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionErrorCode,
    DraftUpdateRequest,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionRejectedError,
    ConversionService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge-conversion"])

# Allowed MIME types for conversion
CONVERSION_ALLOWED_TYPES = {
    "text/plain",
    "text/markdown",
    "text/html",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Also accept by extension for ambiguous MIME types
CONVERSION_ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".md",
    ".html",
    ".htm",
}


def _get_conversion_service(request: Request) -> ConversionService:
    """Dependency: get ConversionService from app state."""
    service = getattr(request.app.state, "conversion_service", None)
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Document conversion service is not available",
        )
    return service


from faultmaven.api.v1.auth_dependencies import require_authentication as _require_auth
from faultmaven.modules.knowledge.api.platform_tier import (
    require_global_authoring_allowed,
)

# =============================================================================
# POST /knowledge/convert
# =============================================================================


@router.post("/convert", status_code=201)
async def convert_document(
    file: UploadFile = File(...),
    scope: str = Form(...),
    team_id: Optional[str] = Form(None),
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Upload a document and convert it to one or more runbook drafts."""
    # Validate scope
    if scope not in ("global", "team", "personal"):
        raise HTTPException(
            status_code=400, detail="scope must be 'global', 'team', or 'personal'"
        )

    if scope == "team" and not team_id:
        raise HTTPException(
            status_code=400, detail="team_id is required when scope is 'team'"
        )

    # Access control: global scope is the platform tier — never authorable
    # from a tenant session under multi (#770), admin-only in single-tenant.
    if scope == "global":
        require_global_authoring_allowed()
        if not current_user.is_platform_admin():
            raise HTTPException(
                status_code=403,
                detail="Global KB conversion requires platform admin role",
            )

    # Validate file type
    content_type = file.content_type or ""
    filename = file.filename or "document"
    ext = Path(filename).suffix.lower()

    if (
        content_type not in CONVERSION_ALLOWED_TYPES
        and ext not in CONVERSION_ALLOWED_EXTENSIONS
    ):
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type: {content_type or ext}. "
                f"Allowed: PDF, DOCX, TXT, Markdown, HTML"
            ),
        )

    # Per-file size cap, from settings rather than a local literal. Starlette
    # bounds only non-file form fields (see main.py); file parts arrive
    # unbounded, so the ceiling has to be enforced here.
    from faultmaven.config.settings import get_settings as _get_settings

    max_upload_mb = _get_settings().upload.max_upload_size_mb
    max_upload_bytes = max_upload_mb * 1024 * 1024

    def _too_large() -> ConversionRejectedError:
        """The canonical refusal: the except-branch maps it to 413."""
        return ConversionRejectedError(
            f"File exceeds maximum size of {max_upload_mb}MB",
            error_code=ConversionErrorCode.FILE_TOO_LARGE,
        )

    tmp_path: Optional[Path] = None
    try:
        # Refuse before reading anything. Starlette populates file.size for
        # spooled uploads, so the common case never touches memory or disk.
        if file.size is not None and file.size > max_upload_bytes:
            raise _too_large()

        # Save upload to temp file. Streamed off the event loop rather than
        # read() into a bytes object, so the whole upload is never resident.
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            await asyncio.to_thread(shutil.copyfileobj, file.file, tmp)
            tmp_path = Path(tmp.name)

        # Fallback for uploads that arrive without a declared size.
        if file.size is None and tmp_path.stat().st_size > max_upload_bytes:
            raise _too_large()

        result = await service.convert_document(
            file_path=tmp_path,
            content_type=content_type,
            original_filename=filename,
            scope=scope,
            user_id=current_user.user_id,
            organization_id=getattr(current_user, "organization_id", None),
            team_id=team_id,
        )

        return result.model_dump()

    except HTTPException:
        # Defensive: no in-try raiser remains today (the size refusal is now a
        # typed ConversionRejectedError), but the blanket handler below returns
        # a 500 for anything it catches, so a future HTTPException raised here
        # would be silently rewritten without this clause.
        raise

    except FaultMavenException:
        # Typed service exceptions (AuthorizationError for a team the caller
        # doesn't belong to, ValidationException for team publishing being
        # unavailable) propagate to the global handlers for canonical 403/422
        # translation instead of being swallowed into a 500 below.
        raise

    except ConversionRejectedError as e:
        error_code = getattr(e, "error_code", "UNKNOWN")
        status_map = {
            ConversionErrorCode.FILE_TOO_LARGE: 413,
            ConversionErrorCode.DOCUMENT_TOO_LONG: 413,
            ConversionErrorCode.UNSUPPORTED_FORMAT: 415,
            ConversionErrorCode.LLM_UNAVAILABLE: 503,
            ConversionErrorCode.FILE_EMPTY: 422,
            ConversionErrorCode.FILE_CORRUPT: 422,
            ConversionErrorCode.ENCODING_ERROR: 422,
            ConversionErrorCode.DOCUMENT_TOO_SHORT: 422,
            ConversionErrorCode.NOT_ACTIONABLE: 422,
            ConversionErrorCode.NO_FAILURE_MODES: 422,
            ConversionErrorCode.NO_TECHNICAL_CONTENT: 422,
            ConversionErrorCode.ALREADY_A_RUNBOOK: 422,
            ConversionErrorCode.LLM_PARSE_ERROR: 422,
        }
        status = status_map.get(error_code, 422)
        return JSONResponse(
            status_code=status,
            content={"detail": str(e), "error_code": error_code},
        )

    except Exception as e:
        logger.error(f"Conversion failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Document conversion failed. Please try again.",
                "error_code": ConversionErrorCode.LLM_GENERATION_FAILED,
            },
        )

    finally:
        # Clean up temp file. Unset when the pre-read size refusal fired, in
        # which case there is nothing on disk to remove.
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


# =============================================================================
# GET /knowledge/conversions
# =============================================================================


@router.get("/conversions")
async def list_conversions(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """List the current user's conversion jobs."""
    return await service.list_conversions(
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
    )


# =============================================================================
# GET /knowledge/drafts — flat list of all user's drafts
# =============================================================================


@router.get("/drafts")
async def list_all_drafts(
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """List all non-deleted drafts across all conversion jobs."""
    return await service.list_all_drafts(user_id=current_user.user_id)


# =============================================================================
# POST /knowledge/scan — discover runbooks on disk not tracked in DB
# =============================================================================


@router.post("/scan")
async def scan_for_runbooks(
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Scan data/knowledge/ for .md files not tracked in the database.

    Discovers runbooks created by the KB Toolkit or placed on disk manually.
    Creates draft records so they appear in the Drafts tab for review.

    Global-scope files (the platform corpus) are minted into drafts only for a
    platform operator — an admin in single-tenant, and never a tenant session
    under multi (#770). Non-global (personal/team) discovery is unaffected.
    """
    try:
        return await service.scan_for_runbooks(
            user_id=current_user.user_id,
            organization_id=getattr(current_user, "organization_id", None),
            is_platform_admin=current_user.is_platform_admin(),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# =============================================================================
# GET /knowledge/conversions/{conversion_id}
# =============================================================================


@router.get("/conversions/{conversion_id}")
async def get_conversion(
    conversion_id: str,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Get conversion job details with all drafts."""
    result = await service.get_conversion(
        conversion_id=conversion_id,
        user_id=current_user.user_id,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Conversion not found")
    return result.model_dump()


# =============================================================================
# PUT /knowledge/conversions/{id}/drafts/{draft_id}
# =============================================================================


@router.put("/conversions/{conversion_id}/drafts/{draft_id}")
async def update_draft(
    conversion_id: str,
    draft_id: str,
    body: DraftUpdateRequest,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Update draft content. Re-runs validation and quality scoring.

    Editing a global-scope draft is platform-corpus authoring, so the service
    applies the global-authoring gate once the job's scope is loaded (#785);
    the resulting AuthorizationError maps to 403 via the global handlers.
    """
    result = await service.update_draft(
        conversion_id=conversion_id,
        draft_id=draft_id,
        user_id=current_user.user_id,
        content=body.content,
        is_platform_admin=current_user.is_platform_admin(),
    )
    if not result:
        raise HTTPException(status_code=404, detail="Draft not found")
    return result.model_dump()


# =============================================================================
# POST /knowledge/drafts/verify-batch
# =============================================================================


class BatchDraftRef(BaseModel):
    conversion_id: str
    draft_id: str


class BatchVerifyRequest(BaseModel):
    draft_ids: list[BatchDraftRef] = Field(min_length=1, max_length=100)


@router.post("/drafts/verify-batch")
async def verify_batch(
    body: BatchVerifyRequest,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Activate multiple drafts sequentially (verify + ingest into KB)."""
    result = await service.verify_batch(
        draft_refs=[(ref.conversion_id, ref.draft_id) for ref in body.draft_ids],
        user_id=current_user.user_id,
        username=current_user.username,
        is_platform_admin=current_user.is_platform_admin(),
    )
    return result


# =============================================================================
# POST /knowledge/conversions/{id}/drafts/{draft_id}/verify
# =============================================================================


@router.post("/conversions/{conversion_id}/drafts/{draft_id}/verify")
async def verify_draft(
    conversion_id: str,
    draft_id: str,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Promote draft to verified status and trigger ingestion into ChromaDB.

    Verifying a draft publishes it into the KB at the job's scope; a global
    draft is the platform corpus, so the service refuses (AuthorizationError →
    403) unless the caller may author global scope (admin single-tenant; never
    a tenant session under multi, #770).

    Service-layer typed exceptions (AuthorizationError, NotFoundError,
    ConflictError, ValidationException) propagate to the global handlers in
    api/exception_handlers.py for canonical translation to 403 / 404 / 409 /
    422 respectively. The route no longer catches ValueError; see
    docs/architecture/specifications/exception-contract.md.
    """
    result = await service.verify_draft(
        conversion_id=conversion_id,
        draft_id=draft_id,
        user_id=current_user.user_id,
        username=current_user.username,
        is_platform_admin=current_user.is_platform_admin(),
    )
    if not result:
        raise HTTPException(
            status_code=500,
            detail="Verification completed but no response returned",
        )
    return result.model_dump()


# =============================================================================
# DELETE /knowledge/conversions/{id}/drafts/{draft_id}
# =============================================================================


@router.delete("/conversions/{conversion_id}/drafts/{draft_id}", status_code=204)
async def delete_draft(
    conversion_id: str,
    draft_id: str,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Delete a conversion draft.

    Deleting a global-scope draft is platform-corpus authoring — the service
    applies the global-authoring gate once the job's scope is loaded, same as
    edit (#785) and verify.
    """
    success = await service.delete_draft(
        conversion_id=conversion_id,
        draft_id=draft_id,
        user_id=current_user.user_id,
        is_platform_admin=current_user.is_platform_admin(),
    )
    if not success:
        raise HTTPException(status_code=404, detail="Draft not found")


# =============================================================================
# POST /knowledge/runbooks/create — Manual runbook creation
# =============================================================================


class RunbookCreateRequest(BaseModel):
    title: str = Field(min_length=10, max_length=100)
    domain: str
    service: str
    symptom_class: list[str] = Field(min_length=1)
    severity: str
    scope: str
    tags: list[str] = Field(default_factory=list)
    difficulty: str = "intermediate"
    symptom_recognition: str = Field(min_length=10)
    applicability: str = Field(min_length=10)
    diagnostic_steps: str = Field(min_length=10)
    causes: str = Field(
        min_length=10,
        description=(
            "Pre-formatted markdown with ### Cause N subsections (one ROOT each). "
            "Each cause needs Statement, an optional Chain (root->D rungs), "
            "Indicators (per-rung, [Step N]-anchored), and quadrant-tagged "
            "Interventions (remediation/defensive_fix/mitigation/loop_break). "
            "Include ### Cause Z: Unidentified with a [Default] indicator as fallback."
        ),
    )
    prevention: str = Field(min_length=10)
    team_id: Optional[str] = None


@router.post("/runbooks/create", status_code=201)
async def create_runbook_manually(
    body: RunbookCreateRequest,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Create a runbook manually from template fields. Returns a draft for review."""
    # Access control: global scope is the platform tier — never authorable
    # from a tenant session under multi (#770), admin-only in single-tenant.
    if body.scope == "global":
        require_global_authoring_allowed()
        if not current_user.is_platform_admin():
            raise HTTPException(
                status_code=403,
                detail="Global KB runbook creation requires platform admin role",
            )
    if body.scope == "team" and not body.team_id:
        raise HTTPException(
            status_code=400, detail="team_id is required for team scope"
        )

    try:
        result = await service.create_runbook_from_template(
            title=body.title,
            domain=body.domain,
            service_name=body.service,
            symptom_class=body.symptom_class,
            severity=body.severity,
            scope=body.scope,
            tags=body.tags,
            difficulty=body.difficulty,
            symptom_recognition=body.symptom_recognition,
            applicability=body.applicability,
            diagnostic_steps=body.diagnostic_steps,
            causes=body.causes,
            prevention=body.prevention,
            user_id=current_user.user_id,
            organization_id=getattr(current_user, "organization_id", None),
            team_id=body.team_id,
        )
        return {
            "conversion_id": result["conversion_id"],
            "draft": result["draft"].model_dump(),
        }
    except FaultMavenException:
        # See convert_document — typed refusals (team membership,
        # team-publishing unavailable) map to 403/422, not 500.
        raise
    except Exception as e:
        logger.error(f"Manual runbook creation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Runbook creation failed")


# =============================================================================
# GET /knowledge/conversions/by-case/{case_id} — Get conversion for a case
# =============================================================================


@router.get("/conversions/by-case/{case_id}")
async def get_conversion_by_case(
    case_id: str,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Get the conversion job and drafts for a specific case."""
    result = await service.get_conversion_by_case(
        case_id=case_id,
        user_id=current_user.user_id,
    )
    if not result:
        raise HTTPException(status_code=404, detail="No conversion found for this case")
    return result.model_dump()
