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
• Cost-Efficiency: Use semantic caching
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

from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.api.v1.role_dependencies import require_admin
from faultmaven.api.v1.utils.parsing import parse_comma_separated_tags
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models import KnowledgeBaseDocument, SearchRequest
from faultmaven.models.api import DocumentSnippetResponse
from faultmaven.models.auth import DevUser
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge_base"])


# Local dependency function to avoid circular imports
# Cannot import from api.v1.dependencies due to circular dependency chain:
# api.v1.dependencies → services → case → persistence → knowledge → api/routes → api.v1.dependencies
async def get_knowledge_service(request: Request) -> KnowledgeService:
    """Get KnowledgeService instance from app.state (Composition Root)"""
    return request.app.state.knowledge_service


# Canonical document types (authoritative)
ALLOWED_DOCUMENT_TYPES = {"playbook", "troubleshooting_guide", "reference", "how_to"}


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
    current_user: DevUser = Depends(require_admin),
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

    try:
        # Validate file type
        allowed_types = {
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/json",
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }

        if file.content_type not in allowed_types:
            logger.warning(f"Invalid file type: {file.content_type}")
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type: {file.content_type}. Allowed types: {', '.join(allowed_types)}",
            )

        # Validate document type
        if document_type not in ALLOWED_DOCUMENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Invalid document_type",
                    "allowed_values": sorted(list(ALLOWED_DOCUMENT_TYPES)),
                },
            )

        # Read file content
        content = await file.read()

        # Additional validation for binary files that might not be text-processable
        if file.content_type in [
            "image/png",
            "image/jpeg",
            "image/gif",
            "application/octet-stream",
        ]:
            logger.warning(f"Binary file type detected: {file.content_type}")
            raise HTTPException(
                status_code=422,
                detail=f"Cannot process binary file type: {file.content_type}",
            )

        try:
            content_str = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            logger.warning(f"File contains non-UTF-8 content")
            raise HTTPException(
                status_code=422, detail="File must contain valid UTF-8 text content"
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
        raise HTTPException(status_code=500, detail=f"Document upload failed: {str(e)}")


@router.get("/documents")
async def list_documents(
    document_type: Optional[str] = None,
    tags: Optional[str] = None,
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
        limit: Maximum number of documents to return
        offset: Number of documents to skip

    Returns:
        List of documents
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate filters
        if document_type is not None and document_type not in ALLOWED_DOCUMENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Invalid document_type filter",
                    "allowed_values": sorted(list(ALLOWED_DOCUMENT_TYPES)),
                },
            )

        # Parse tags filter
        tag_list = parse_comma_separated_tags(tags) or None

        # Delegate to service layer
        return await knowledge_service.list_documents(
            document_type=document_type,
            tags=tag_list,
            limit=limit,
            offset=offset,
            user=current_user,
        )

    except Exception as e:
        logger.error(f"Failed to list documents: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list documents: {str(e)}"
        )


@router.get("/documents/{document_id}")
async def get_document(
    document_id: str,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> KnowledgeBaseDocument:
    """
    Get a specific knowledge base document

    Args:
        document_id: Document identifier

    Returns:
        Document details
    """
    logger = logging.getLogger(__name__)

    try:
        document = await knowledge_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return document

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve document {document_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve document: {str(e)}"
        )


@router.get("/documents/{document_id}/snippet")
@trace("api_get_document_snippet")
async def get_document_snippet(
    document_id: str,
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
) -> DocumentSnippetResponse:
    """
    Get a snippet/preview of a knowledge base document for hover cards.

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
        # Get the full document first
        document = await knowledge_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        content = document.content
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
        verification_level = getattr(document, "verification_level", 0)
        verification_status = "experimental"
        if verification_level >= 2:
            verification_status = "verified"
        elif verification_level >= 1:
            verification_status = "community"

        return DocumentSnippetResponse(
            document_id=document_id,
            title=document.title,
            snippet=snippet,
            line_range=(actual_line_start, actual_line_end),
            total_lines=total_lines,
            document_type=document.document_type,
            verification_status=verification_status,
            verification_level=verification_level,
            relevance_score=relevance_score,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get snippet for document {document_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get document snippet: {str(e)}"
        )


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin),
):
    """
    Delete a knowledge base document

    Args:
        document_id: Document identifier

    Returns:
        Deletion confirmation
    """
    logger = logging.getLogger(__name__)

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
        raise HTTPException(
            status_code=500, detail=f"Failed to delete document: {str(e)}"
        )


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """
    Get the status of a knowledge base ingestion job

    Args:
        job_id: Job identifier

    Returns:
        Job status information
    """
    logger = logging.getLogger(__name__)

    try:
        job_status = await knowledge_service.get_job_status(job_id)
        if not job_status:
            raise HTTPException(status_code=404, detail="Job not found")

        return job_status

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get job status {job_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get job status: {str(e)}"
        )


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
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


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

        # Use the search_documents method which implements full-text search
        result = await knowledge_service.search_documents(
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
        raise HTTPException(
            status_code=500, detail=f"Full-text search failed: {str(e)}"
        )


@router.put("/documents/{document_id}")
async def update_document(
    document_id: str,
    update_data: dict,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin),
) -> dict:
    """Update document metadata and content."""
    logger = logging.getLogger(__name__)

    try:
        # Validate document_type if provided
        if "document_type" in update_data and update_data["document_type"] is not None:
            if update_data["document_type"] not in ALLOWED_DOCUMENT_TYPES:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Invalid document_type",
                        "allowed_values": sorted(list(ALLOWED_DOCUMENT_TYPES)),
                    },
                )

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
    except Exception as e:
        logger.error(f"Failed to update document {document_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update document: {str(e)}"
        )


@router.post("/documents/bulk-update")
async def bulk_update_documents(
    request: Dict[str, Any],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin),
) -> dict:
    """Bulk update document metadata."""
    logger = logging.getLogger(__name__)

    try:
        document_ids = request.get("document_ids", [])
        updates = request.get("updates", {})

        if not document_ids:
            raise HTTPException(status_code=400, detail="Document IDs are required")

        # Parse tags in updates if provided using standardized utility
        if "tags" in updates:
            updates["tags"] = parse_comma_separated_tags(updates["tags"])

        result = await knowledge_service.bulk_update_documents(
            document_ids=document_ids, updates=updates
        )

        logger.info(f"Bulk updated {result['updated_count']} documents")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk update failed: {e}")
        raise HTTPException(status_code=500, detail=f"Bulk update failed: {str(e)}")


@router.post("/documents/bulk-delete")
async def bulk_delete_documents(
    request: Dict[str, List[str]],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin),
) -> dict:
    """Bulk delete documents."""
    logger = logging.getLogger(__name__)

    try:
        document_ids = request.get("document_ids", [])

        if not document_ids:
            raise HTTPException(status_code=400, detail="Document IDs are required")

        result = await knowledge_service.bulk_delete_documents(document_ids)

        logger.info(f"Bulk deleted {result['deleted_count']} documents")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk delete failed: {e}")
        raise HTTPException(status_code=500, detail=f"Bulk delete failed: {str(e)}")


@router.get("/stats")
async def get_knowledge_stats(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> dict:
    """Get knowledge base statistics."""
    logger = logging.getLogger(__name__)

    try:
        stats = await knowledge_service.get_knowledge_stats()
        logger.info("Retrieved knowledge base statistics")
        return stats

    except Exception as e:
        logger.error(f"Failed to get knowledge stats: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get statistics: {str(e)}"
        )


@router.get("/analytics/search")
async def get_search_analytics(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> dict:
    """Get search analytics and insights."""
    logger = logging.getLogger(__name__)

    try:
        analytics = await knowledge_service.get_search_analytics()
        logger.info("Retrieved search analytics")
        return analytics

    except Exception as e:
        logger.error(f"Failed to get search analytics: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get analytics: {str(e)}"
        )


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
    current_user: DevUser = Depends(require_admin),
) -> dict:
    """
    List knowledge suggestions with optional filtering.

    Returns suggestions extracted from cases that are pending review.
    Includes lineage information for each suggestion (source case, extractor, timestamp).

    Args:
        status: Filter by status (pending_review, approved, rejected)
        limit: Maximum suggestions to return (default: 20)
        offset: Pagination offset (default: 0)

    Returns:
        SuggestionListResponse with paginated suggestions
    """
    logger = logging.getLogger(__name__)

    try:
        # Get organization_id from user context if available
        organization_id = getattr(current_user, "organization_id", None)

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
        raise HTTPException(
            status_code=500, detail=f"Failed to list suggestions: {str(e)}"
        )


@router.get("/suggestions/{suggestion_id}")
@trace("api_get_suggestion")
async def get_suggestion(
    suggestion_id: str,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_admin),
) -> dict:
    """
    Get a specific knowledge suggestion by ID.

    Returns full suggestion details including content, PII scan status,
    and lineage information.

    Args:
        suggestion_id: Suggestion identifier

    Returns:
        KnowledgeSuggestionDetail
    """
    logger = logging.getLogger(__name__)

    try:
        suggestion = await suggestion_service.get_suggestion(suggestion_id)
        if not suggestion:
            raise HTTPException(status_code=404, detail="Suggestion not found")

        return suggestion_service.to_api_response(suggestion, include_content=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get suggestion {suggestion_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get suggestion: {str(e)}"
        )


@router.put("/suggestions/{suggestion_id}")
@trace("api_update_suggestion")
async def update_suggestion(
    suggestion_id: str,
    update_data: dict,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_admin),
) -> dict:
    """
    Update a suggestion's content.

    Allows editing the suggested title, content, or type before approval.
    Content changes trigger a new PII scan.

    Args:
        suggestion_id: Suggestion to update
        update_data: Fields to update (title, content, suggested_type)

    Returns:
        Updated suggestion details
    """
    logger = logging.getLogger(__name__)

    try:
        suggestion = await suggestion_service.update_suggestion(
            suggestion_id=suggestion_id,
            title=update_data.get("title"),
            content=update_data.get("content"),
            suggested_type=update_data.get("suggested_type"),
        )

        if not suggestion:
            raise HTTPException(status_code=404, detail="Suggestion not found")

        logger.info(f"Updated suggestion {suggestion_id}")
        return suggestion_service.to_api_response(suggestion, include_content=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update suggestion {suggestion_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update suggestion: {str(e)}"
        )


@router.post("/suggestions/{suggestion_id}/approve", status_code=201)
@trace("api_approve_suggestion")
async def approve_suggestion(
    suggestion_id: str,
    request_body: Optional[dict] = None,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_admin),
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

    try:
        review_notes = None
        if request_body:
            review_notes = request_body.get("review_notes")

        result = await suggestion_service.approve_suggestion(
            suggestion_id=suggestion_id,
            reviewed_by=current_user.user_id,
            review_notes=review_notes,
        )

        if not result:
            raise HTTPException(
                status_code=400,
                detail="Cannot approve: suggestion not found or PII scan not complete",
            )

        logger.info(f"Approved suggestion {suggestion_id}")
        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to approve suggestion {suggestion_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to approve suggestion: {str(e)}"
        )


@router.post("/suggestions/{suggestion_id}/reject")
@trace("api_reject_suggestion")
async def reject_suggestion(
    suggestion_id: str,
    request_body: dict,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_admin),
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
        raise HTTPException(
            status_code=500, detail=f"Failed to reject suggestion: {str(e)}"
        )


@router.post("/suggestions/{suggestion_id}/remediate-pii")
@trace("api_remediate_pii")
async def remediate_pii(
    suggestion_id: str,
    suggestion_service=Depends(get_suggestion_service),
    current_user: DevUser = Depends(require_admin),
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

    try:
        suggestion = await suggestion_service.remediate_pii(
            suggestion_id=suggestion_id,
            remediated_by=current_user.user_id,
        )

        if not suggestion:
            raise HTTPException(status_code=404, detail="Suggestion not found")

        logger.info(f"PII remediated for suggestion {suggestion_id}")
        return suggestion_service.to_api_response(suggestion, include_content=True)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to remediate PII for suggestion {suggestion_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to remediate PII: {str(e)}"
        )
