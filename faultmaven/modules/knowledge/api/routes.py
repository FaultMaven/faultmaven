"""kb_management.py

Purpose: Knowledge base endpoints

Requirements:
--------------------------------------------------------------------------------
• Handle document uploads
• Provide job status checks
• Manage knowledge base documents

Key Components:
--------------------------------------------------------------------------------
  router = APIRouter()
  @router.post('/kb/documents')

Technology Stack:
--------------------------------------------------------------------------------
FastAPI, Pydantic

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)

from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_actor_organization,
    require_authentication,
    require_platform_admin,
)
from faultmaven.api.v1.utils.parsing import parse_comma_separated_tags
from faultmaven.exceptions import AuthorizationError, FaultMavenException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models import KnowledgeBaseDocument, SearchRequest
from faultmaven.models.api import DocumentSnippetResponse
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api.platform_tier import (
    require_global_authoring_allowed,
)
from faultmaven.modules.knowledge.domain.document_write import (
    ensure_document_write_allowed,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge_base"])


# Local dependency function to avoid circular imports
# Cannot import from api.v1.dependencies due to circular dependency chain:
# api.v1.dependencies → services → case → persistence → knowledge → api/routes → api.v1.dependencies
async def get_knowledge_service(request: Request) -> KnowledgeService:
    """Get KnowledgeService instance from app.state (Composition Root).

    ``None`` means the container composed without a knowledge service — every
    KB route is unserviceable, and saying so is the whole point. This used to
    be papered over one layer up, where the container substituted an in-memory
    stub that answered reads with invented content (#899); a 503 an operator
    can act on is strictly better than a 200 nobody can trust.
    """
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Knowledge base unavailable",
        )
    return service


async def _resolve_team_ids(request: Request, user_id: Optional[str]) -> List[str]:
    """Resolve the caller's team memberships for read-visibility checks.

    One resolver for every document surface (list, id-addressed read, write,
    bulk) so the team arm of the visibility rule cannot drift between them.
    """
    if not user_id:
        return []
    team_service = getattr(request.app.state, "team_service", None)
    if not team_service:
        return []
    return await team_service.list_all_user_team_ids(user_id)


async def _authorize_document_write(
    document: Dict[str, Any],
    document_id: str,
    request: Request,
    knowledge_service: KnowledgeService,
    current_user: DevUser,
) -> None:
    """Apply the write policy to an already-loaded document, 404-safe (#867).

    Policy-first ordering is deliberate: the single-tenant operator override
    applies to documents the operator cannot list, so a visibility-first load
    would 404 legitimate operator writes. Only on refusal do we re-check read
    visibility — an actor who cannot see the target gets the same answer as
    for an absent id, so a 403 never confirms that the document exists. The
    extra query therefore runs only on the refusal path.

    Raises:
        HTTPException: 404 when the refused target is also invisible.
        AuthorizationError: re-raised otherwise; the global handler maps it
            to HTTP 403.
    """
    try:
        ensure_document_write_allowed(
            scope=document.get("scope", "global"),
            owner_id=document.get("owner_id"),
            actor_user_id=current_user.user_id,
            is_platform_admin=current_user.is_platform_admin(),
        )
    except AuthorizationError:
        team_ids = await _resolve_team_ids(request, current_user.user_id)
        visible = await knowledge_service.get_document_visible(
            document_id, user=current_user, team_ids=team_ids
        )
        if visible is None:
            raise HTTPException(status_code=404, detail="Document not found")
        raise


# Upper bound on a bulk write batch. The bounded surface is per-target DB work
# — a document load per id, plus a visibility query per refusal — reachable by
# any authenticated caller since #866 moved these routes off platform_admin.
# Nothing else bounds it: MAX_UPLOAD_SIZE_MB is multipart-only and the rate
# limiter counts requests, not targets within one.
MAX_BULK_DOCUMENT_IDS = 200


def _normalize_bulk_document_ids(document_ids: Any) -> List[str]:
    """Validate, cap and de-duplicate a bulk batch (#866).

    The cap is applied to the RAW list, before dedupe: capping the deduped list
    would still let a caller submit an unbounded one and make the server pay
    for collapsing it.

    Dedupe preserves first-seen order, so the per-target gate runs — and each
    refusal is reported — once per unique id. It also removes a timing oracle:
    an existing-but-invisible target costs two DB round trips against one for
    an absent id, and repeating a single id would otherwise scale that
    difference into a measurable signal despite the identical response strings.
    """
    if not isinstance(document_ids, list) or not all(
        isinstance(doc_id, str) for doc_id in document_ids
    ):
        raise HTTPException(
            status_code=400, detail="document_ids must be a list of strings"
        )
    if len(document_ids) > MAX_BULK_DOCUMENT_IDS:
        raise HTTPException(
            status_code=400,
            detail=(f"Too many documents: at most {MAX_BULK_DOCUMENT_IDS} per request"),
        )
    return list(dict.fromkeys(document_ids))


async def _partition_bulk_targets(
    document_ids: List[str],
    request: Request,
    knowledge_service: KnowledgeService,
    current_user: DevUser,
) -> tuple[List[str], List[str]]:
    """Split bulk targets into (permitted ids, per-target refusal messages).

    The bulk routes are semantically a loop over the single-document write
    routes, so they run the same per-document policy (#866) before anything
    reaches the service — only permitted ids are passed on.

    Refusals carry no existence oracle: a target the actor cannot even see is
    reported with the identical message as an absent one.
    """
    team_ids = await _resolve_team_ids(request, current_user.user_id)
    permitted: List[str] = []
    errors: List[str] = []

    for doc_id in document_ids:
        document = await knowledge_service.get_document(doc_id)
        if not document:
            errors.append(f"Document {doc_id} not found")
            continue
        try:
            ensure_document_write_allowed(
                scope=document.get("scope", "global"),
                owner_id=document.get("owner_id"),
                actor_user_id=current_user.user_id,
                is_platform_admin=current_user.is_platform_admin(),
            )
        except AuthorizationError:
            visible = await knowledge_service.get_document_visible(
                doc_id, user=current_user, team_ids=team_ids
            )
            errors.append(
                f"Document {doc_id}: not authorized"
                if visible is not None
                else f"Document {doc_id} not found"
            )
            continue
        permitted.append(doc_id)

    return permitted, errors


# Suggested document types (not enforced — free-text with UI suggestions)
SUGGESTED_DOCUMENT_TYPES = {
    "runbook",
    "playbook",
    "troubleshooting_guide",
    "reference",
    "how_to",
}


# (No legacy router paths)
@router.post("/documents", status_code=201)
@trace("api_upload_document")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form(...),
    category: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    response: Response = Response(),
    current_user: DevUser = Depends(require_platform_admin),
) -> dict:
    """
    Upload a document to the knowledge base

    Args:
        file: Document file to upload
        title: Document title
        document_type: Type of document
        tags: Comma-separated tags
        source_url: Source URL if applicable

    Returns:
        Upload job information
    """
    logger = logging.getLogger(__name__)
    logger.info(f"Uploading document: {file.filename}")

    # This route publishes at global scope (KnowledgeService.upload_document
    # default) — the platform tier, never authorable from a tenant session
    # under multi (#770).
    require_global_authoring_allowed()

    try:
        # Validate file type — runbook upload accepts text formats only
        # Binary formats (PDF, DOCX) should go through "Convert to Runbook"
        allowed_types = {
            "text/plain",
            "text/markdown",
        }
        allowed_extensions = {".md", ".txt", ".markdown"}

        filename = file.filename or ""
        file_ext = filename[filename.rfind(".") :].lower() if "." in filename else ""

        if (
            file.content_type not in allowed_types
            and file_ext not in allowed_extensions
        ):
            logger.warning(
                f"Invalid file type for runbook upload: {file.content_type} ({filename})"
            )
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Unsupported file type: {file.content_type}. "
                    "Upload Runbook accepts Markdown (.md) and text (.txt) files. "
                    "For PDF, DOCX, or HTML files, use Convert to Runbook instead."
                ),
            )

        # Read file content
        content = await file.read()

        try:
            content_str = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            # Try latin-1 fallback
            try:
                content_str = content.decode("latin-1")
            except UnicodeDecodeError:
                logger.warning("File contains unreadable encoding")
                raise HTTPException(
                    status_code=422,
                    detail="File encoding not supported. Re-save as UTF-8 and try again.",
                )

        # Validate runbook content against standards
        from faultmaven.modules.knowledge.domain.services.runbook_validator import (
            RunbookValidator,
        )

        validator = RunbookValidator()
        validation = validator.validate_content(content_str)

        if not validation.passed:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Runbook does not meet quality standards",
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                    "help": (
                        "The uploaded file must be a valid runbook with YAML frontmatter "
                        "(id, title, domain, service, symptom_class, severity, scope, "
                        "version, last_updated, verified_by, status) and required sections "
                        "(Symptom Recognition, Applicability, Diagnostic Steps, Causes, "
                        "Prevention, Sources). "
                        "Use Write Runbook to create one from the template, or "
                        "Convert to Runbook to generate from a source document."
                    ),
                },
            )

        # Parse tags
        tag_list = parse_comma_separated_tags(tags)

        # Delegate to service layer
        result = await knowledge_service.upload_document(
            content=content_str,
            title=title,
            document_type=document_type,
            category=category,
            tags=tag_list,
            source_url=source_url,
            description=description,
        )

        # Set Location header for REST compliance
        document_id = result.get("document_id", result.get("id", "unknown"))
        response.headers["Location"] = f"/api/v1/knowledge/documents/{document_id}"

        logger.info(f"Successfully queued document {document_id} for ingestion")

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document upload failed: {e}")
        # No str(e) in the response — the rule for every 500 in this module
        # (#866). A DB driver raises with the connection URI in its message, so
        # echoing exception text hands a caller credentials. Each `except` logs
        # the exception (that is where the diagnostic belongs) and answers with
        # the static prefix only.
        raise HTTPException(status_code=500, detail="Document upload failed")


@router.get("/documents")
async def list_documents(
    request: Request,
    document_type: Optional[str] = None,
    tags: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: Optional[DevUser] = Depends(get_current_user_optional),
) -> dict:
    """
    List knowledge base documents with optional filtering

    Args:
        document_type: Filter by document type
        tags: Filter by tags (comma-separated)
        scope: Filter by scope (global, team, personal)
        limit: Maximum number of documents to return
        offset: Number of documents to skip

    Returns:
        List of documents
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate scope if provided
        if scope and scope not in ("global", "team", "personal"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid scope: {scope}. Allowed: global, team, personal",
            )

        # Parse tags filter
        tag_list = parse_comma_separated_tags(tags) or None

        # Resolve user's team memberships for RBAC
        team_ids = await _resolve_team_ids(
            request, current_user.user_id if current_user else None
        )

        # Delegate to service layer
        return await knowledge_service.list_documents(
            document_type=document_type,
            tags=tag_list,
            scope=scope,
            limit=limit,
            offset=offset,
            user=current_user,
            team_ids=team_ids,
        )

    except Exception as e:
        logger.error(f"Failed to list documents: {e}")
        raise HTTPException(status_code=500, detail="Failed to list documents")


@router.get("/documents/{document_id}")
async def get_document(
    document_id: str,
    request: Request,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
) -> KnowledgeBaseDocument:
    """Get a specific knowledge base document.

    Requires authentication and applies the same read-visibility rule as the
    inventory listing (#867): global ∪ own ∪ shared-to-my-teams. A document
    the caller may not see answers 404, identically to an absent id — the
    refusal must not confirm that someone else's runbook exists.

    Unlike ``GET /documents`` (which stays optionally authenticated so an
    anonymous caller can browse global titles), the id-addressed reads require
    a caller: they return full document content, and no consumer needs them
    anonymously.

    Args:
        document_id: Document identifier

    Returns:
        Document details
    """
    logger = logging.getLogger(__name__)

    try:
        team_ids = await _resolve_team_ids(request, current_user.user_id)
        document = await knowledge_service.get_document_visible(
            document_id, user=current_user, team_ids=team_ids
        )
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return document

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve document {document_id}: {e}")
        # No str(e) in the response: raw exception text is an internal detail
        # (#867). The log line above keeps the diagnostic.
        raise HTTPException(status_code=500, detail="Failed to retrieve document")


@router.get("/documents/{document_id}/snippet")
@trace("api_get_document_snippet")
async def get_document_snippet(
    document_id: str,
    request: Request,
    line_start: int = Query(default=1, ge=1, description="Starting line number"),
    line_end: Optional[int] = Query(
        default=None, ge=1, description="Ending line number"
    ),
    max_lines: int = Query(
        default=5, ge=1, le=50, description="Maximum lines to return"
    ),
    query_string: Optional[str] = Query(
        default=None, description="Query for semantic snippet extraction"
    ),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
) -> DocumentSnippetResponse:
    """
    Get a snippet/preview of a knowledge base document for hover cards.

    Requires authentication and applies the same read-visibility rule as
    ``GET /documents/{document_id}`` (#867): a document the caller may not see
    answers 404, identically to an absent id.

    Supports two modes:
    1. **Line-based extraction**: Extract lines from line_start to line_end (or max_lines)
    2. **Semantic extraction**: If query_string is provided, returns the most relevant
       snippet based on vector similarity (more robust than line numbers after edits)

    Args:
        document_id: Document identifier
        line_start: Starting line number (1-indexed, default: 1)
        line_end: Ending line number (optional, computed from max_lines if not provided)
        max_lines: Maximum lines to return (default: 5, max: 50)
        query_string: Query for semantic snippet extraction (optional)

    Returns:
        Document snippet with verification status for badge display
    """
    logger = logging.getLogger(__name__)

    try:
        # Get the full document first, scoped to the caller's visibility
        team_ids = await _resolve_team_ids(request, current_user.user_id)
        document = await knowledge_service.get_document_visible(
            document_id, user=current_user, team_ids=team_ids
        )
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        content = document["content"]
        lines = content.split("\n")
        total_lines = len(lines)

        snippet = ""
        actual_line_start = line_start
        actual_line_end = line_start + max_lines - 1
        relevance_score = None

        if query_string:
            # Semantic snippet extraction: find most relevant chunk
            try:
                # Use the knowledge service's semantic search to find relevant chunk
                search_result = await knowledge_service.get_semantic_snippet(
                    document_id=document_id,
                    query=query_string,
                    max_lines=max_lines,
                )
                if search_result:
                    snippet = search_result.get("snippet", "")
                    actual_line_start = search_result.get("line_start", 1)
                    actual_line_end = search_result.get("line_end", max_lines)
                    relevance_score = search_result.get("relevance_score")
                else:
                    # Fallback to line-based extraction
                    snippet = "\n".join(
                        lines[line_start - 1 : line_start - 1 + max_lines]
                    )
            except Exception as semantic_error:
                logger.warning(
                    f"Semantic snippet extraction failed, falling back to line-based: {semantic_error}"
                )
                # Fallback to line-based extraction
                snippet = "\n".join(lines[line_start - 1 : line_start - 1 + max_lines])
        else:
            # Line-based extraction
            end_line = line_end if line_end else line_start + max_lines - 1
            end_line = min(end_line, total_lines)
            actual_line_end = end_line

            # Ensure we don't go past the end of the document
            start_idx = max(0, line_start - 1)
            end_idx = min(end_line, total_lines)

            snippet = "\n".join(lines[start_idx:end_idx])

        # Get verification status
        verification_level = document.get("verification_level", 0)
        verification_status = "experimental"
        if verification_level >= 2:
            verification_status = "verified"
        elif verification_level >= 1:
            verification_status = "community"

        return DocumentSnippetResponse(
            document_id=document_id,
            title=document["title"],
            snippet=snippet,
            line_range=(actual_line_start, actual_line_end),
            total_lines=total_lines,
            document_type=document["document_type"],
            verification_status=verification_status,
            verification_level=verification_level,
            relevance_score=relevance_score,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get snippet for document {document_id}: {e}")
        # No str(e) in the response (#867) — see get_document.
        raise HTTPException(status_code=500, detail="Failed to get document snippet")


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    request: Request,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
):
    """
    Delete a knowledge base document

    Ownership-aware (#834): the owner may delete their own personal/team
    document; global-scope deletion is platform-corpus authoring (operator
    only, per the global-tier policy). A refusal over a document the caller
    cannot even see answers 404 rather than 403 (#867).

    Args:
        document_id: Document identifier

    Returns:
        Deletion confirmation
    """
    logger = logging.getLogger(__name__)

    document = await knowledge_service.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    await _authorize_document_write(
        document, document_id, request, knowledge_service, current_user
    )

    try:
        result = await knowledge_service.delete_document(document_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail="Document not found")

        logger.info(f"Successfully deleted document {document_id}")

        # Return no content for 204 status code
        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete document {document_id}: {e}")
        # See update_document — no internal exception text to the widened
        # audience (#834).
        raise HTTPException(status_code=500, detail="Failed to delete document")


@router.post("/search")
@trace("api_search_documents")
async def search_documents(
    request: SearchRequest,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: Optional[DevUser] = Depends(get_current_user_optional),
) -> dict:
    """
    Search knowledge base documents

    Args:
        request: Search request with query and filters

    Returns:
        Search results
    """
    logger = logging.getLogger(__name__)

    try:
        # Additional validation beyond Pydantic (Pydantic handles empty query via min_length=1)
        if len(request.query.strip()) > 1000:
            logger.warning("Search query too long")
            raise HTTPException(
                status_code=422, detail="Query cannot exceed 1000 characters"
            )

        # Parse tags filter
        tag_list = parse_comma_separated_tags(request.tags) or None

        # Extract category from filters or direct field
        category = request.category
        if request.filters and not category:
            category = request.filters.get("category")

        # Extract document_type from filters if not directly specified
        document_type = request.document_type
        if request.filters and not document_type:
            document_type = request.filters.get("document_type")

        # Delegate to service layer
        return await knowledge_service.search_documents(
            query=request.query.strip(),
            document_type=document_type,
            category=category,
            tags=tag_list,
            limit=request.limit,
            similarity_threshold=request.similarity_threshold,
            rank_by=request.rank_by,
            user=current_user,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.post("/documents/search")
@trace("api_fulltext_search_documents")
async def fulltext_search_documents(
    request: SearchRequest,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: Optional[DevUser] = Depends(get_current_user_optional),
) -> dict:
    """
    Full-text search for knowledge base documents (Microservices Parity)

    Implements full-text search complementing the semantic search at /knowledge/search.
    This endpoint provides simple keyword-based text matching across document titles
    and content, useful when semantic understanding is not required.

    **Differences from /knowledge/search:**
    - `/knowledge/search` - Semantic vector search using embeddings (similarity-based)
    - `/documents/search` - Full-text keyword search (exact/partial word matching)

    **Use Cases:**
    - Searching for specific error codes or identifiers
    - Finding documents with exact phrases
    - Faster search when semantic understanding not needed
    - Filtering by document_type, category, tags

    **Request Body:**
    ```json
    {
        "query": "PostgreSQL connection timeout",
        "document_type": "kb_article",
        "category": "database",
        "tags": "postgresql,timeout",
        "limit": 20,
        "similarity_threshold": 0.5
    }
    ```

    **Returns:**
    ```json
    {
        "query": "...",
        "total_results": 5,
        "results": [
            {
                "document_id": "...",
                "content": "...",
                "metadata": {
                    "title": "...",
                    "document_type": "...",
                    "category": "...",
                    "tags": [...],
                    "priority": "..."
                },
                "similarity_score": 0.85
            }
        ]
    }
    ```
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate query length
        if len(request.query.strip()) > 1000:
            logger.warning("Full-text search query too long")
            raise HTTPException(
                status_code=422, detail="Query cannot exceed 1000 characters"
            )

        # Parse tags filter
        tag_list = parse_comma_separated_tags(request.tags) or None

        # Extract category and document_type
        category = request.category
        if request.filters and not category:
            category = request.filters.get("category")

        document_type = request.document_type
        if request.filters and not document_type:
            document_type = request.filters.get("document_type")

        # Use full-text (title substring) search, distinct from semantic /search endpoint
        result = await knowledge_service.fulltext_search_documents(
            query=request.query.strip(),
            document_type=document_type,
            category=category,
            tags=tag_list,
            limit=request.limit,
            similarity_threshold=request.similarity_threshold,
            rank_by=request.rank_by,
            user=current_user,
        )

        logger.info(
            f"Full-text search for '{request.query}' returned {result.get('total_results', 0)} results"
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Full-text search failed: {e}")
        raise HTTPException(status_code=500, detail="Full-text search failed")


@router.put("/documents/{document_id}")
async def update_document(
    document_id: str,
    update_data: dict,
    request: Request,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
) -> dict:
    """Update document metadata and content.

    Ownership-aware (#834): the owner may edit their own personal/team
    document; global-scope editing is platform-corpus authoring (operator
    only, per the global-tier policy). A refusal over a document the caller
    cannot even see answers 404 rather than 403 (#867).
    """
    logger = logging.getLogger(__name__)

    document = await knowledge_service.get_document(document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    await _authorize_document_write(
        document, document_id, request, knowledge_service, current_user
    )

    try:
        # Parse tags if provided using standardized utility
        if "tags" in update_data:
            update_data["tags"] = parse_comma_separated_tags(update_data["tags"])

        result = await knowledge_service.update_document_metadata(
            document_id=document_id, **update_data
        )

        if not result:
            raise HTTPException(status_code=404, detail="Document not found")

        logger.info(f"Successfully updated document {document_id}")
        return result

    except HTTPException:
        raise
    except KnowledgeBaseError as e:
        # Re-indexing failed. Whether the OLD vectors survived depends on where
        # it failed, and the response must not assert more than is known:
        # the pre-delete failures (no embedder, no chunks) leave the previous
        # vectors intact, but KNOWLEDGE_INDEXING_FAILED can fire from
        # add_documents AFTER the delete, leaving the document with none.
        logger.error(f"Failed to re-index document {document_id}: {e}")
        searchable_state = (
            "Search results still reflect the previous content."
            if getattr(e, "error_code", None)
            in ("KNOWLEDGE_EMBEDDER_UNAVAILABLE", "KNOWLEDGE_NO_CHUNKS")
            else "This document may not be searchable until the update succeeds."
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Document saved, but re-indexing for search failed. "
                f"{searchable_state} Retry the update once the knowledge base "
                f"is available."
            ),
        )
    except Exception as e:
        logger.error(f"Failed to update document {document_id}: {e}")
        # No str(e) in the response: this route is now reachable by any
        # authenticated user (#834), and raw exception text is an internal
        # detail. The log line above keeps the diagnostic.
        raise HTTPException(status_code=500, detail="Failed to update document")


@router.post("/documents/bulk-update")
async def bulk_update_documents(
    request: Request,
    payload: Dict[str, Any],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
) -> dict:
    """Bulk update document metadata.

    Ownership-aware per target (#866): this surface is a loop over
    ``PUT /documents/{id}``, so it carries the same gate — any authenticated
    caller may submit a batch, and the per-document write policy decides each
    target. Refused targets are reported in ``errors`` and never counted as
    updated; ``total_requested`` is the de-duplicated batch size (see
    ``_normalize_bulk_document_ids``), so the counts reconcile.
    """
    logger = logging.getLogger(__name__)

    try:
        document_ids = payload.get("document_ids", [])
        updates = payload.get("updates", {})

        if not document_ids:
            raise HTTPException(status_code=400, detail="Document IDs are required")

        document_ids = _normalize_bulk_document_ids(document_ids)

        # Parse tags in updates if provided using standardized utility
        if "tags" in updates:
            updates["tags"] = parse_comma_separated_tags(updates["tags"])

        permitted_ids, gate_errors = await _partition_bulk_targets(
            document_ids, request, knowledge_service, current_user
        )

        result = await knowledge_service.bulk_update_documents(
            document_ids=permitted_ids, updates=updates
        )
        result["errors"] = gate_errors + list(result.get("errors") or [])
        result["total_requested"] = len(document_ids)

        logger.info(f"Bulk updated {result['updated_count']} documents")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk update failed: {e}")
        # No str(e) in the response (#866) — see update_document.
        raise HTTPException(status_code=500, detail="Bulk update failed")


@router.post("/documents/bulk-delete")
async def bulk_delete_documents(
    request: Request,
    payload: Dict[str, List[str]],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
) -> dict:
    """Bulk delete documents.

    Ownership-aware per target (#866): the same gate as
    ``DELETE /documents/{id}``, applied to each target before anything reaches
    the service. Refused targets are reported in ``errors`` and never counted
    as deleted; ``total_requested`` is the de-duplicated batch size (see
    ``_normalize_bulk_document_ids``), so the counts reconcile.
    """
    logger = logging.getLogger(__name__)

    try:
        document_ids = payload.get("document_ids", [])

        if not document_ids:
            raise HTTPException(status_code=400, detail="Document IDs are required")

        document_ids = _normalize_bulk_document_ids(document_ids)

        permitted_ids, gate_errors = await _partition_bulk_targets(
            document_ids, request, knowledge_service, current_user
        )

        result = await knowledge_service.bulk_delete_documents(permitted_ids)
        result["errors"] = gate_errors + list(result.get("errors") or [])
        result["total_requested"] = len(document_ids)

        logger.info(f"Bulk deleted {result['deleted_count']} documents")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk delete failed: {e}")
        # No str(e) in the response (#866) — see update_document.
        raise HTTPException(status_code=500, detail="Bulk delete failed")


@router.get("/stats")
async def get_knowledge_stats(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
) -> dict:
    """Get knowledge base statistics.

    Requires authentication (#867): aggregate counts over the corpus are not
    public, and ``docs/architecture/security/rbac.md`` already documented this
    route as "Any authenticated user".
    """
    logger = logging.getLogger(__name__)

    try:
        stats = await knowledge_service.get_knowledge_stats()
        logger.info("Retrieved knowledge base statistics")
        return stats

    except Exception as e:
        logger.error(f"Failed to get knowledge stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get statistics")


@router.get("/analytics/search")
async def get_search_analytics(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_authentication),
) -> dict:
    """Get search analytics and insights.

    Requires authentication, for the same reason as ``/stats``.
    """
    logger = logging.getLogger(__name__)

    try:
        analytics = await knowledge_service.get_search_analytics()
        logger.info("Retrieved search analytics")
        return analytics

    except Exception as e:
        logger.error(f"Failed to get search analytics: {e}")
        raise HTTPException(status_code=500, detail="Failed to get analytics")


# ============================================================
# Knowledge Suggestions Endpoints (Review Inbox)
# ============================================================


async def get_suggestion_service(request: Request):
    """Get SuggestionService instance from app.state"""
    if hasattr(request.app.state, "suggestion_service"):
        return request.app.state.suggestion_service

    # Create temporary service if not in app state
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )

    return SuggestionService()


@router.get("/suggestions")
@trace("api_list_suggestions")
async def list_suggestions(
    status: Optional[str] = Query(
        default=None, description="Filter by status: pending_review, approved, rejected"
    ),
    limit: int = Query(default=20, ge=1, le=100, description="Maximum items to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    request: Request = None,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_platform_admin),
) -> dict:
    """
    List the caller's organization's knowledge suggestions.

    Returns suggestions extracted from cases that are pending review.
    Includes lineage information for each suggestion (source case, extractor, timestamp).

    Scoped to the caller's tenant, resolved fail-closed: the operator role says
    *what* you may do, never *whose* data you may see.

    Args:
        status: Filter by status (pending_review, approved, rejected)
        limit: Maximum suggestions to return (default: 20)
        offset: Pagination offset (default: 0)

    Returns:
        SuggestionListResponse with paginated suggestions
    """
    logger = logging.getLogger(__name__)

    organization_id = require_actor_organization(current_user)

    try:
        result = await suggestion_service.list_suggestions(
            organization_id=organization_id,
            status=status,
            limit=limit,
            offset=offset,
        )

        # Convert to API response format
        suggestions = [
            suggestion_service.to_api_response(s) for s in result["suggestions"]
        ]

        return {
            "suggestions": suggestions,
            "total_count": result["total_count"],
            "limit": result["limit"],
            "offset": result["offset"],
        }

    except Exception as e:
        logger.error(f"Failed to list suggestions: {e}")
        raise HTTPException(status_code=500, detail="Failed to list suggestions")


@router.get("/suggestions/{suggestion_id}")
@trace("api_get_suggestion")
async def get_suggestion(
    suggestion_id: str,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_platform_admin),
) -> dict:
    """
    Get a specific knowledge suggestion by ID.

    Returns full suggestion details including content, PII scan status,
    and lineage information.

    Resolved through the tenant-scoped lookup: an id belonging to another
    organization answers 404, identically to an absent id, so the response is
    never an existence oracle.

    Args:
        suggestion_id: Suggestion identifier

    Returns:
        KnowledgeSuggestionDetail
    """
    logger = logging.getLogger(__name__)

    organization_id = require_actor_organization(current_user)

    try:
        suggestion = await suggestion_service.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
        if not suggestion:
            raise HTTPException(status_code=404, detail="Suggestion not found")

        return suggestion_service.to_api_response(suggestion, include_content=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get suggestion {suggestion_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get suggestion")


@router.put("/suggestions/{suggestion_id}")
@trace("api_update_suggestion")
async def update_suggestion(
    suggestion_id: str,
    update_data: dict,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_platform_admin),
) -> dict:
    """
    Update a suggestion's content.

    Allows editing the suggested title, content, or type before approval.
    Content changes trigger a new PII scan.

    Tenant-scoped: an id outside the caller's organization answers 404 and
    nothing is written.

    Args:
        suggestion_id: Suggestion to update
        update_data: Fields to update (title, content, suggested_type)

    Returns:
        Updated suggestion details
    """
    logger = logging.getLogger(__name__)

    organization_id = require_actor_organization(current_user)

    try:
        suggestion = await suggestion_service.update_suggestion(
            suggestion_id=suggestion_id,
            title=update_data.get("title"),
            content=update_data.get("content"),
            suggested_type=update_data.get("suggested_type"),
            organization_id=organization_id,
        )

        if not suggestion:
            raise HTTPException(status_code=404, detail="Suggestion not found")

        logger.info(f"Updated suggestion {suggestion_id}")
        return suggestion_service.to_api_response(suggestion, include_content=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update suggestion {suggestion_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update suggestion")


@router.post("/suggestions/{suggestion_id}/approve", status_code=201)
@trace("api_approve_suggestion")
async def approve_suggestion(
    suggestion_id: str,
    request_body: Optional[dict] = None,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_platform_admin),
) -> dict:
    """
    Approve a suggestion and create a knowledge item.

    Validates that PII scan is complete and clean/remediated before approval.
    Creates a new KnowledgeItem with verification_level=2 (admin verified).
    Establishes bidirectional link between suggestion and knowledge item.

    Args:
        suggestion_id: Suggestion to approve
        request_body: Optional review notes

    Returns:
        Approval result with new knowledge_item_id
    """
    logger = logging.getLogger(__name__)

    # Approval publishes at global scope (KnowledgeService.upload_document
    # default) — the platform tier, never authorable from a tenant session
    # under multi (#770).
    require_global_authoring_allowed()

    organization_id = require_actor_organization(current_user)

    try:
        review_notes = None
        if request_body:
            review_notes = request_body.get("review_notes")

        # Resolve existence through the tenant-scoped lookup FIRST, so an
        # absent id and another organization's id get the same 404. Folding
        # both into the "not found or not ready" 400 below would make the
        # status code an existence oracle: 400 would mean "it exists here,
        # just isn't ready" while an out-of-scope id fell somewhere else.
        if not await suggestion_service.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        ):
            raise HTTPException(status_code=404, detail="Suggestion not found")

        result = await suggestion_service.approve_suggestion(
            suggestion_id=suggestion_id,
            reviewed_by=current_user.user_id,
            review_notes=review_notes,
            organization_id=organization_id,
        )

        if not result:
            raise HTTPException(
                status_code=400,
                detail="Cannot approve: PII scan not complete",
            )

        logger.info(f"Approved suggestion {suggestion_id}")
        return result

    except HTTPException:
        raise
    except FaultMavenException:
        # Typed service exceptions (ConflictError for "not ready for
        # review", etc.) propagate to FastAPI's global handlers which
        # map them to 409/422/404. See api/exception_handlers.py.
        # Without this pass-through, the blanket `except Exception`
        # below would swallow them and re-wrap as 500.
        raise
    except Exception as e:
        logger.error(f"Failed to approve suggestion {suggestion_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to approve suggestion")


@router.post("/suggestions/{suggestion_id}/reject")
@trace("api_reject_suggestion")
async def reject_suggestion(
    suggestion_id: str,
    request_body: dict,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_platform_admin),
) -> dict:
    """
    Reject a suggestion.

    Marks the suggestion as rejected with the provided reason.

    Args:
        suggestion_id: Suggestion to reject
        request_body: Must include rejection_reason, optional review_notes

    Returns:
        Rejection confirmation
    """
    logger = logging.getLogger(__name__)

    organization_id = require_actor_organization(current_user)

    try:
        rejection_reason = request_body.get("rejection_reason")
        if not rejection_reason:
            raise HTTPException(status_code=400, detail="rejection_reason is required")

        review_notes = request_body.get("review_notes")

        success = await suggestion_service.reject_suggestion(
            suggestion_id=suggestion_id,
            reviewed_by=current_user.user_id,
            rejection_reason=rejection_reason,
            review_notes=review_notes,
            organization_id=organization_id,
        )

        if not success:
            raise HTTPException(status_code=404, detail="Suggestion not found")

        logger.info(f"Rejected suggestion {suggestion_id}")
        return {
            "suggestion_id": suggestion_id,
            "status": "rejected",
            "rejection_reason": rejection_reason,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reject suggestion {suggestion_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to reject suggestion")


@router.post("/suggestions/{suggestion_id}/remediate-pii")
@trace("api_remediate_pii")
async def remediate_pii(
    suggestion_id: str,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_platform_admin),
) -> dict:
    """
    Mark PII as remediated after manual review.

    Called when an admin has manually reviewed and cleaned up
    PII-flagged content. Allows the suggestion to proceed to approval.

    Args:
        suggestion_id: Suggestion with PII to remediate

    Returns:
        Updated suggestion with remediated status
    """
    logger = logging.getLogger(__name__)

    organization_id = require_actor_organization(current_user)

    try:
        suggestion = await suggestion_service.remediate_pii(
            suggestion_id=suggestion_id,
            remediated_by=current_user.user_id,
            organization_id=organization_id,
        )

        if not suggestion:
            raise HTTPException(status_code=404, detail="Suggestion not found")

        logger.info(f"PII remediated for suggestion {suggestion_id}")
        return suggestion_service.to_api_response(suggestion, include_content=True)

    except HTTPException:
        raise
    except FaultMavenException:
        # See approve_suggestion above for the rationale — ConflictError
        # from Suggestion.mark_pii_remediated propagates to the global
        # 409 handler instead of being collapsed to 400.
        raise
    except Exception as e:
        logger.error(f"Failed to remediate PII for suggestion {suggestion_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to remediate PII")
