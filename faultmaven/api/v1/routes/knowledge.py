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

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from faultmaven.models import KnowledgeBaseDocument, SearchRequest
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.api.v1.dependencies import get_knowledge_service
from faultmaven.services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge_base"])

# Create a secondary router with /kb prefix for backward compatibility
kb_router = APIRouter(prefix="/kb", tags=["knowledge_base"])


@router.post("/documents")
@trace("api_upload_document")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form("troubleshooting_guide"),
    category: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
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
        tag_list = []
        if tags:
            tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

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

        logger.info(f"Successfully queued document {result['document_id']} for ingestion")

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
        # Parse tags filter
        tag_list = None
        if tags:
            tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

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


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
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

        return result

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
        tag_list = None
        if request.tags:
            tag_list = [tag.strip() for tag in request.tags.split(",") if tag.strip()]

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
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Update document metadata and content."""
    logger = logging.getLogger(__name__)
    
    try:
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
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Bulk update document metadata."""
    logger = logging.getLogger(__name__)
    
    try:
        document_ids = request.get("document_ids", [])
        updates = request.get("updates", {})
        
        if not document_ids:
            raise HTTPException(status_code=400, detail="Document IDs are required")
            
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
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
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


# Duplicate all endpoints on kb_router for backward compatibility
@kb_router.post("/documents")
@trace("api_upload_document")
async def upload_document_kb(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form("troubleshooting_guide"),
    category: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> dict:
    """Upload a document to the knowledge base (kb prefix)"""
    return await upload_document(file, title, document_type, category, tags, source_url, description, knowledge_service)


@kb_router.get("/documents")
async def list_documents_kb(
    document_type: Optional[str] = None,
    tags: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> dict:
    """List knowledge base documents (kb prefix)"""
    return await list_documents(document_type, tags, limit, offset, knowledge_service)


@kb_router.get("/documents/{document_id}")
async def get_document_kb(
    document_id: str, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> KnowledgeBaseDocument:
    """Get a specific document (kb prefix)"""
    return await get_document(document_id, knowledge_service)


@kb_router.delete("/documents/{document_id}")
async def delete_document_kb(
    document_id: str, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Delete a document (kb prefix)"""
    return await delete_document(document_id, knowledge_service)


@kb_router.get("/jobs/{job_id}")
async def get_job_status_kb(
    job_id: str, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Get job status (kb prefix)"""
    return await get_job_status(job_id, knowledge_service)


@kb_router.post("/search")
@trace("api_search_documents")
async def search_documents_kb(
    request: SearchRequest, knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Search documents (kb prefix)"""
    return await search_documents(request, knowledge_service)


@kb_router.put("/documents/{document_id}")
async def update_document_kb(
    document_id: str,
    update_data: dict,
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Update document (kb prefix)"""
    return await update_document(document_id, update_data, knowledge_service)


@kb_router.post("/documents/bulk-update")
async def bulk_update_documents_kb(
    request: Dict[str, Any],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Bulk update documents (kb prefix)"""
    return await bulk_update_documents(request, knowledge_service)


@kb_router.post("/documents/bulk-delete")
async def bulk_delete_documents_kb(
    request: Dict[str, List[str]],
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Bulk delete documents (kb prefix)"""
    return await bulk_delete_documents(request, knowledge_service)


@kb_router.get("/stats")
async def get_knowledge_stats_kb(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Get knowledge stats (kb prefix)"""
    return await get_knowledge_stats(knowledge_service)


@kb_router.get("/analytics/search")
async def get_search_analytics_kb(
    knowledge_service: KnowledgeService = Depends(get_knowledge_service)
) -> dict:
    """Get search analytics (kb prefix)"""
    return await get_search_analytics(knowledge_service)
