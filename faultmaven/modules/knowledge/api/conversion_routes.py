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
from pydantic import BaseModel, Field, ValidationError

from faultmaven.config.idempotency_key import IDEMPOTENCY_REPLAY_UNAVAILABLE
from faultmaven.config.tenant_context import writable_enterprise_id
from faultmaven.exceptions import FaultMavenException, RawModelContentRefused
from faultmaven.infrastructure.concurrency.request_fingerprint import (
    digest_file,
    request_fingerprint,
)
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api.request_idempotency import (
    IdempotencyKey,
    KeyedRequest,
    keyed_responses,
    open_keyed_request,
    redis_of,
    replay_committed_request,
    replayed,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    ConversionErrorCode,
    ConversionResponse,
    DraftUpdateRequest,
)
from faultmaven.modules.knowledge.domain.request_receipts import (
    ROUTE_CONVERT,
    KbReceiptKey,
    KbRequestReceipt,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.errors import (
    ConversionRejectedError,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.exceptions import KbReceiptExistsError
from faultmaven.modules.knowledge.taxonomy import (
    KnowledgeScope,
    RunbookDifficulty,
    RunbookDomain,
    RunbookSeverity,
    SymptomClass,
    render_vocabulary,
    vocabulary,
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

# Every route below declares the gate TWICE: once on the decorator's
# ``dependencies=[...]``, where FastAPI solves it ahead of every handler
# parameter, and once as ``current_user`` where the body reads the principal.
# The decorator copy is what makes an anonymous caller's answer a 401 rather
# than ``_get_conversion_service``'s 503 (#1494); the parameter copy costs
# nothing, because FastAPI caches a dependency per request. Removing either is
# a behaviour change.

# =============================================================================
# POST /knowledge/convert
# =============================================================================


def _replayed_conversion(receipt: KbRequestReceipt) -> JSONResponse:
    """The stored answer if it validates as a ``ConversionResponse``, else 409
    ``IDEMPOTENCY_REPLAY_UNAVAILABLE``, as the turn's replay answers (#2433)."""
    try:
        ConversionResponse.model_validate(receipt.response)
    except ValidationError:
        logger.error("A conversion receipt no longer validates as its response")
        raise HTTPException(
            status_code=409,
            detail="This conversion committed; reload your conversions.",
            headers={"x-error-code": IDEMPOTENCY_REPLAY_UNAVAILABLE},
        ) from None
    return replayed(receipt)


@router.post(
    "/convert",
    status_code=201,
    dependencies=[Depends(_require_auth)],
    responses=keyed_responses(
        created="The conversion job and its drafts.",
        location=False,
        conflicts=(
            f" `{IDEMPOTENCY_REPLAY_UNAVAILABLE}` (the conversion committed, and "
            "its stored response no longer validates: reload your conversions)."
            " Without `x-error-code`, `conflict_reason: duplicate_runbook_id`: "
            "a live draft already holds a runbook id this conversion minted."
        ),
        codes=[IDEMPOTENCY_REPLAY_UNAVAILABLE],
    ),
)
async def convert_document(
    file: UploadFile = File(...),
    scope: str = Form(...),
    team_id: Optional[str] = Form(None),
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
    request: Request = None,  # noqa: B008 - app.state carries the claim store
    idempotency_key: IdempotencyKey = None,
):
    """Upload a document and convert it to one or more runbook drafts.

    A draft's title is redacted before the draft is stored (#1981). If that
    check cannot run (the sanitizer or its pseudonym key is unavailable) the
    request answers **503**, a retryable server-side condition, with a generic
    detail, and nothing is saved.

    **Retries (`Idempotency-Key`).** A keyed conversion commits a receipt with
    its job, in the same transaction. A request with a key this caller already
    used for a conversion: the same file and fields, once the job committed →
    **201** with that job's body and `X-Idempotency-Replayed: true`, and no new
    job or drafts; a different file or fields → **409** `IDEMPOTENCY_KEY_REUSE`;
    the first still running → **409** `KB_REQUEST_IN_PROGRESS` with
    `Retry-After`. Without a key, every request converts again.
    """
    # Validate scope. A 400, not the 422 a typed Form field would answer:
    # this form field predates the taxonomy enums and its status is part of
    # the published contract. The allowed set is the enum's.
    if scope not in vocabulary(KnowledgeScope):
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of: {render_vocabulary(KnowledgeScope)}",
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
    # The keyed conversion's claim, released in ``finally`` (#2433).
    keyed: Optional[KeyedRequest] = None
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

        # The idempotency step (#2433), as the upload route's. The file is
        # fingerprinted from the temp file, off the event loop, so it is still
        # never resident in memory.
        if idempotency_key:
            digest = await asyncio.to_thread(digest_file, filename, tmp_path)
            keyed = await open_keyed_request(
                redis=redis_of(request),
                receipt_key=KbReceiptKey(
                    enterprise_id=writable_enterprise_id(None),
                    author_id=current_user.user_id,
                    route_name=ROUTE_CONVERT,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint(
                        {
                            "scope": scope,
                            "team_id": team_id,
                            "content_type": content_type,
                        },
                        [digest],
                    ),
                ),
                lookup=service.get_request_receipt,
            )
            if keyed.replay is not None:
                return _replayed_conversion(keyed.replay)

        result = await service.convert_document(
            file_path=tmp_path,
            content_type=content_type,
            original_filename=filename,
            scope=scope,
            user_id=current_user.user_id,
            enterprise_id=getattr(current_user, "enterprise_id", None),
            team_id=team_id,
            receipt_key=keyed.receipt_key if keyed is not None else None,
        )

        # JSON mode: the body a keyed conversion's receipt stores, so a first
        # answer and its replay are the same bytes.
        return result.model_dump(mode="json")

    except HTTPException:
        # The idempotency step's 409s; and defensive: the blanket handler
        # below returns a 500 for anything it catches, so an HTTPException
        # raised here would be silently rewritten without this clause.
        raise

    except KbReceiptExistsError:
        # Another conversion under this key published while this one ran
        # without a claim; nothing of this one did. Answered as its retry.
        return _replayed_conversion(
            await replay_committed_request(
                keyed=keyed, lookup=service.get_request_receipt
            )
        )

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
        # Safe in place, and allowlisted as such in
        # ``tests/unit/api/test_api_surface_error_text_not_echoed.py``:
        # ``ConversionRejectedError`` is a TYPED domain exception whose every
        # construction is a hand-written caller-facing sentence ("No LLM
        # provider is configured. Set CHAT_PROVIDER…"), which is the whole
        # point of the error-code map above. That held for ten of the eleven
        # construction sites; the eleventh interpolated a parse exception and
        # was made static under #1400, so the promise this arm relies on is
        # now true for all of them. The broad ``except Exception`` directly
        # below is separately sanitised.
        return JSONResponse(
            status_code=status,
            content={"detail": str(e), "error_code": error_code},
        )

    except RawModelContentRefused:
        # A refusal is answered by its own handler: 500 SERVICE_ERROR (#1998).
        raise
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
        # Only now is the conversion fully answered, its receipt committed.
        if keyed is not None:
            await keyed.release()
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


@router.get("/conversions", dependencies=[Depends(_require_auth)])
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


@router.get("/drafts", dependencies=[Depends(_require_auth)])
async def list_all_drafts(
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """List all non-deleted drafts across all conversion jobs."""
    return await service.list_all_drafts(user_id=current_user.user_id)


# =============================================================================
# POST /knowledge/scan — import the standalone runbook directory as drafts
# =============================================================================


@router.post("/scan", dependencies=[Depends(_require_auth)])
async def scan_for_runbooks(
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Import the .md files under data/knowledge/ as drafts (standalone only).

    Each file that is not already a live draft or a published runbook becomes a
    draft holding the file's text, so it appears in the Drafts tab for review.
    The import never changes an existing draft (#1950).

    Under ``TENANT_PROVIDER=multi`` the API has no runbook directory — its
    replicas share no disk — so the service refuses with a
    ``ValidationException``, which the global handler answers as 422.

    Global-scope files (the platform corpus) are minted into drafts only for a
    platform operator (#770). Non-global discovery is unaffected.

    A file under ``personal_<id>/`` or ``user_<id>/`` is imported as a personal
    draft. A file under ``team_<id>/`` is imported as a **personal** draft too
    (#1986): the scan runs only where there are no teams, so no team exists to
    share it with, and a team-scope runbook with no team would be visible to
    its owner alone. The response's ``notes`` list names each such file
    ("team_x/foo.md imported as personal: ..."); ``errors`` names the files
    that were not imported.

    Each draft's title is redacted before its row is written (#1981). If that
    check cannot run (the sanitizer or its pseudonym key is unavailable) the
    scan answers **503**, a retryable server-side condition, with a generic
    detail. It can do so MID-WALK: the files imported before the failing one
    stay imported (each is its own draft), and a second scan skips them and
    imports the rest.
    """
    return await service.scan_for_runbooks(
        user_id=current_user.user_id,
        enterprise_id=getattr(current_user, "enterprise_id", None),
        is_platform_admin=current_user.is_platform_admin(),
    )


# =============================================================================
# GET /knowledge/conversions/{conversion_id}
# =============================================================================


@router.get("/conversions/{conversion_id}", dependencies=[Depends(_require_auth)])
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


@router.put(
    "/conversions/{conversion_id}/drafts/{draft_id}",
    dependencies=[Depends(_require_auth)],
)
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


@router.post("/drafts/verify-batch", dependencies=[Depends(_require_auth)])
async def verify_batch(
    body: BatchVerifyRequest,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Activate multiple drafts sequentially (verify + ingest into KB)."""
    result = await service.verify_batch(
        draft_refs=[(ref.conversion_id, ref.draft_id) for ref in body.draft_ids],
        user_id=current_user.user_id,
        is_platform_admin=current_user.is_platform_admin(),
    )
    return result


# =============================================================================
# POST /knowledge/conversions/{id}/drafts/{draft_id}/verify
# =============================================================================


@router.post(
    "/conversions/{conversion_id}/drafts/{draft_id}/verify",
    dependencies=[Depends(_require_auth)],
)
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

    The published title is redacted before anything is written (#1981). If that
    check cannot run (the sanitizer or its pseudonym key is unavailable) the
    request answers **503**, a retryable server-side condition, with a generic
    detail, and the draft stays a draft.
    """
    result = await service.verify_draft(
        conversion_id=conversion_id,
        draft_id=draft_id,
        user_id=current_user.user_id,
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


@router.delete(
    "/conversions/{conversion_id}/drafts/{draft_id}",
    status_code=204,
    dependencies=[Depends(_require_auth)],
)
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
    """A runbook authored field by field (the dashboard's Create form).

    The closed vocabularies are typed with the taxonomy enums, so the request
    schema publishes the allowed values and an off-vocabulary value is a 422
    here rather than a draft that fails validation (#1886).
    """

    title: str = Field(min_length=10, max_length=100)
    domain: RunbookDomain
    service: str
    symptom_class: list[SymptomClass] = Field(min_length=1)
    severity: RunbookSeverity
    scope: KnowledgeScope
    tags: list[str] = Field(default_factory=list)
    difficulty: RunbookDifficulty = RunbookDifficulty.INTERMEDIATE
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


@router.post("/runbooks/create", status_code=201, dependencies=[Depends(_require_auth)])
async def create_runbook_manually(
    body: RunbookCreateRequest,
    service: ConversionService = Depends(_get_conversion_service),
    current_user: DevUser = Depends(_require_auth),
):
    """Create a runbook manually from template fields. Returns a draft for review.

    The title is redacted before the draft, its id and its text are made
    (#1981). If that check cannot run (the sanitizer or its pseudonym key is
    unavailable) the request answers **503**, a retryable server-side condition,
    with a generic detail, and nothing is saved.
    """
    # Access control: global scope is the platform tier — never authorable
    # from a tenant session under multi (#770), admin-only in single-tenant.
    if body.scope == KnowledgeScope.GLOBAL:
        require_global_authoring_allowed()
        if not current_user.is_platform_admin():
            raise HTTPException(
                status_code=403,
                detail="Global KB runbook creation requires platform admin role",
            )
    if body.scope == KnowledgeScope.TEAM and not body.team_id:
        raise HTTPException(
            status_code=400, detail="team_id is required for team scope"
        )

    try:
        result = await service.create_runbook_from_template(
            title=body.title,
            domain=body.domain.value,
            service_name=body.service,
            symptom_class=[item.value for item in body.symptom_class],
            severity=body.severity.value,
            scope=body.scope.value,
            tags=body.tags,
            difficulty=body.difficulty.value,
            symptom_recognition=body.symptom_recognition,
            applicability=body.applicability,
            diagnostic_steps=body.diagnostic_steps,
            causes=body.causes,
            prevention=body.prevention,
            user_id=current_user.user_id,
            enterprise_id=getattr(current_user, "enterprise_id", None),
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


@router.get("/conversions/by-case/{case_id}", dependencies=[Depends(_require_auth)])
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
