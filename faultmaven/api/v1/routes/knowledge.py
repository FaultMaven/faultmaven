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
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from faultmaven.models import KnowledgeBaseDocument, SearchRequest
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.api.v1.dependencies import get_knowledge_service
from faultmaven.services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/kb", tags=["knowledge_base"])


@router.post("/documents")
@trace("api_upload_document")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form("troubleshooting_guide"),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
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
        # Read file content
        content = await file.read()
        content_str = content.decode("utf-8", errors="ignore")

        # Parse tags
        tag_list = []
        if tags:
            tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

        # Delegate to service layer
        result = await knowledge_service.upload_document(
            content=content_str,
            title=title,
            document_type=document_type,
            tags=tag_list,
            source_url=source_url
        )

        logger.info(f"Successfully queued document {result['document_id']} for ingestion")

        return result

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

        return {
            "document_id": document_id,
            "status": "deleted",
            "message": "Document deleted successfully",
        }

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
        # Parse tags filter
        tag_list = None
        if request.tags:
            tag_list = [tag.strip() for tag in request.tags.split(",") if tag.strip()]

        # Delegate to service layer
        return await knowledge_service.search_documents(
            query=request.query,
            document_type=request.document_type,
            tags=tag_list,
            limit=request.limit,
        )

    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")
