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

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends

from ..models import KnowledgeBaseDocument, SearchRequest
from ..knowledge_base.ingestion import KnowledgeIngester
from ..observability.tracing import trace
from ..security.redaction import DataSanitizer

router = APIRouter(prefix="/kb", tags=["knowledge_base"])

# Global instances (in production, these would be dependency injected)
kb_ingestion = KnowledgeIngester()
data_sanitizer = DataSanitizer()


def get_kb_ingestion():
    return kb_ingestion


def get_data_sanitizer():
    return data_sanitizer


@router.post("/documents")
@trace("api_upload_document")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form("troubleshooting_guide"),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    kb_ingestion: KnowledgeIngester = Depends(get_kb_ingestion),
    data_sanitizer: DataSanitizer = Depends(get_data_sanitizer),
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
        # Generate document ID
        document_id = str(uuid.uuid4())
        
        # Read file content
        content = await file.read()
        content_str = content.decode('utf-8', errors='ignore')
        
        # Sanitize content for security
        sanitized_content = data_sanitizer.sanitize(content_str)
        
        # Parse tags
        tag_list = []
        if tags:
            tag_list = [tag.strip() for tag in tags.split(',') if tag.strip()]
        
        # Create document record
        document = KnowledgeBaseDocument(
            document_id=document_id,
            title=title,
            content=sanitized_content,
            document_type=document_type,
            tags=tag_list,
            source_url=source_url,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Process document with knowledge base ingestion
        job_id = await kb_ingestion.ingest_document_object(document)
        
        logger.info(f"Successfully queued document {document_id} for ingestion")
        
        return {
            "document_id": document_id,
            "job_id": job_id,
            "status": "queued",
            "message": "Document uploaded and queued for processing"
        }
        
    except Exception as e:
        logger.error(f"Document upload failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Document upload failed: {str(e)}"
        )


@router.get("/documents")
async def list_documents(
    document_type: Optional[str] = None,
    tags: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    kb_ingestion: KnowledgeIngester = Depends(get_kb_ingestion)
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
            tag_list = [tag.strip() for tag in tags.split(',') if tag.strip()]
        
        # Get documents from knowledge base
        documents = await kb_ingestion.list_documents(
            document_type=document_type,
            tags=tag_list,
            limit=limit,
            offset=offset
        )
        
        return {
            "documents": [
                {
                    "document_id": doc.document_id,
                    "title": doc.title,
                    "document_type": doc.document_type,
                    "tags": doc.tags,
                    "source_url": doc.source_url,
                    "created_at": doc.created_at.isoformat(),
                    "updated_at": doc.updated_at.isoformat()
                }
                for doc in documents
            ],
            "total": len(documents),
            "limit": limit,
            "offset": offset
        }
        
    except Exception as e:
        logger.error(f"Failed to list documents: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list documents: {str(e)}"
        )


@router.get("/documents/{document_id}")
async def get_document(
    document_id: str,
    kb_ingestion: KnowledgeIngester = Depends(get_kb_ingestion)
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
        document = await kb_ingestion.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        
        return document
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve document {document_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve document: {str(e)}"
        )


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    kb_ingestion: KnowledgeIngester = Depends(get_kb_ingestion)
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
        success = await kb_ingestion.delete_document(document_id)
        if not success:
            raise HTTPException(status_code=404, detail="Document not found")
        
        logger.info(f"Successfully deleted document {document_id}")
        
        return {
            "document_id": document_id,
            "status": "deleted",
            "message": "Document deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete document {document_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete document: {str(e)}"
        )


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    kb_ingestion: KnowledgeIngester = Depends(get_kb_ingestion)
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
        job_status = await kb_ingestion.get_job_status(job_id)
        if not job_status:
            raise HTTPException(status_code=404, detail="Job not found")
        
        return job_status
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get job status {job_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get job status: {str(e)}"
        )


@router.post("/search")
@trace("api_search_documents")
async def search_documents(
    request: SearchRequest,
    kb_ingestion: KnowledgeIngester = Depends(get_kb_ingestion)
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
            tag_list = [tag.strip() for tag in request.tags.split(',') if tag.strip()]
        
        # Search documents
        results = await kb_ingestion.search_documents(
            query=request.query,
            document_type=request.document_type,
            tags=tag_list,
            limit=request.limit
        )
        
        return {
            "query": request.query,
            "results": [
                {
                    "document_id": result["document_id"],
                    "title": result["title"],
                    "document_type": result["document_type"],
                    "tags": result["tags"],
                    "score": result["score"],
                    "snippet": result["snippet"]
                }
                for result in results
            ],
            "total": len(results)
        }
        
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Search failed: {str(e)}"
        )

