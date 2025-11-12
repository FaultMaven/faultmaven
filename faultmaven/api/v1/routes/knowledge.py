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
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Response

from faultmaven.models import KnowledgeBaseDocument, SearchRequest
from faultmaven.models.auth import DevUser
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.api.v1.dependencies import get_knowledge_service
from faultmaven.api.v1.utils.parsing import parse_comma_separated_tags
from faultmaven.api.v1.role_dependencies import require_admin
from faultmaven.services.domain.knowledge_service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge_base"])

# Canonical document types (authoritative)
ALLOWED_DOCUMENT_TYPES = {"playbook", "troubleshooting_guide", "reference", "how_to"}

# Removed kb_router - no backward compatibility, use /knowledge/ only


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
    current_user: DevUser = Depends(require_admin)
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
            "text/plain", "text/markdown", "text/csv", "application/json",
            "application/pdf", "application/msword", 
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        }
        
        if file.content_type not in allowed_types:
            logger.warning(f"Invalid file type: {file.content_type}")
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type: {file.content_type}. Allowed types: {', '.join(allowed_types)}"
            )

        # Validate document type
        if document_type not in ALLOWED_DOCUMENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Invalid document_type",
                    "allowed_values": sorted(list(ALLOWED_DOCUMENT_TYPES))
                }
            )

        # Read file content
        content = await file.read()
        
        # Additional validation for binary files that might not be text-processable
        if file.content_type in ["image/png", "image/jpeg", "image/gif", "application/octet-stream"]:
            logger.warning(f"Binary file type detected: {file.content_type}")
            raise HTTPException(
                status_code=422,
                detail=f"Cannot process binary file type: {file.content_type}"
            )
        
        try:
            content_str = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            logger.warning(f"File contains non-UTF-8 content")
            raise HTTPException(
                status_code=422,
                detail="File must contain valid UTF-8 text content"
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
            description=description
        )

        # Set Location header for REST compliance
        document_id = result.get('document_id', result.get('id', 'unknown'))
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
                    "allowed_values": sorted(list(ALLOWED_DOCUMENT_TYPES))
                }
            )

        # Parse tags filter
        tag_list = parse_comma_separated_tags(tags) or None

        # Delegate to service layer
        return await knowledge_service.list_documents(
            document_type=document_type,
            tags=tag_list,
            limit=limit,
            offset=offset
        )

    except Exception as e:
        logger.error(f"Failed to list documents: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list documents: {str(e)}"
        )


@router.get("/documents/{document_id}")
async def get_document(
    document_id: str, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
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


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin)
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
    request: SearchRequest, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
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
            raise HTTPException(status_code=422, detail="Query cannot exceed 1000 characters")

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
            rank_by=request.rank_by
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@router.put("/documents/{document_id}")
async def update_document(
    document_id: str,
    update_data: dict,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin)
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
                        "allowed_values": sorted(list(ALLOWED_DOCUMENT_TYPES))
                    }
                )

        # Parse tags if provided using standardized utility
        if "tags" in update_data:
            update_data["tags"] = parse_comma_separated_tags(update_data["tags"])

        result = await knowledge_service.update_document_metadata(
            document_id=document_id,
            **update_data
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
            status_code=500, 
            detail=f"Failed to update document: {str(e)}"
        )


@router.post("/documents/bulk-update")
async def bulk_update_documents(
    request: Dict[str, Any],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin)
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
            document_ids=document_ids,
            updates=updates
        )
        
        logger.info(f"Bulk updated {result['updated_count']} documents")
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk update failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Bulk update failed: {str(e)}"
        )


@router.post("/documents/bulk-delete")
async def bulk_delete_documents(
    request: Dict[str, List[str]],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    current_user: DevUser = Depends(require_admin)
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
        raise HTTPException(
            status_code=500,
            detail=f"Bulk delete failed: {str(e)}"
        )


@router.get("/stats")
async def get_knowledge_stats(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
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
            status_code=500,
            detail=f"Failed to get statistics: {str(e)}"
        )


@router.get("/analytics/search")
async def get_search_analytics(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
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
            status_code=500,
            detail=f"Failed to get analytics: {str(e)}"
        )
