"""User Knowledge Base API Routes

Purpose: REST API endpoints for user's personal knowledge base management

This module provides REST API endpoints for managing user-specific runbooks,
procedures, and documentation. Each user has their own persistent knowledge base
that can be queried by the AI agent during troubleshooting.

Key Endpoints:
- Upload runbook/procedure documents
- List user's documents
- Delete specific documents
- Get document count

Architecture:
- Documents stored in user-scoped ChromaDB collections (user_kb_{user_id})
- Documents processed through same preprocessing pipeline as case evidence
- Permanent storage (unlike case evidence which is ephemeral)
- Used by answer_from_user_kb agent tool
"""

from datetime import datetime, timezone
from typing import Optional, List
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, Form
from fastapi.responses import JSONResponse

from faultmaven.models.auth import DevUser
from faultmaven.models.api import ErrorResponse, ErrorDetail
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.utils.serialization import to_json_compatible


# Create router
router = APIRouter(prefix="/users", tags=["user_kb"])
logger = logging.getLogger(__name__)


# Dependency injection helpers
async def get_user_kb_vector_store():
    """Get UserKBVectorStore from container"""
    from faultmaven.container import container
    store = getattr(container, 'user_kb_vector_store', None)
    if not store:
        raise HTTPException(
            status_code=503,
            detail="User KB vector store not available. Check ChromaDB configuration."
        )
    return store


async def get_preprocessing_service():
    """Get PreprocessingService from container"""
    from faultmaven.container import container
    service = getattr(container, 'preprocessing_service', None)
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Preprocessing service not available"
        )
    return service


@router.post(
    "/{user_id}/kb/documents",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Document uploaded successfully"},
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        503: {"model": ErrorResponse}
    }
)
@trace("api_upload_user_kb_document")
async def upload_user_kb_document(
    user_id: str,
    file: UploadFile = File(..., description="Runbook or procedure document"),
    title: Optional[str] = Form(None, description="Document title (defaults to filename)"),
    category: Optional[str] = Form(None, description="Document category (e.g., 'database', 'networking')"),
    tags: Optional[str] = Form(None, description="Comma-separated tags"),
    description: Optional[str] = Form(None, description="Document description"),
    user_kb_store = Depends(get_user_kb_vector_store),
    preprocessing_service = Depends(get_preprocessing_service),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Upload a document to user's personal knowledge base.

    Accepts runbooks, procedures, troubleshooting guides, and documentation.
    Documents are preprocessed to extract key information and stored permanently
    in the user's knowledge base for future agent queries.

    **Access Control**: Users can only upload to their own knowledge base.

    **File Types Supported**:
    - Text files (.txt, .md)
    - Logs (.log)
    - Configuration files (.yaml, .json, .xml, .conf)
    - Code files (.py, .js, .java, etc.)

    **Processing**:
    1. File uploaded and validated
    2. Content preprocessed (classification, extraction, sanitization)
    3. Document stored in user-scoped ChromaDB collection
    4. Available immediately for agent queries via answer_from_user_kb tool

    **Example**:
    ```bash
    curl -X POST "http://localhost:8000/api/v1/users/alice/kb/documents" \\
      -H "Authorization: Bearer dev_token" \\
      -F "file=@database_runbook.md" \\
      -F "title=Database Timeout Troubleshooting" \\
      -F "category=database" \\
      -F "tags=postgresql,timeout,performance"
    ```
    """
    # Access control: Users can only upload to their own KB
    if current_user.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. Users can only manage their own knowledge base."
        )

    try:
        # Step 1: Read uploaded file
        file_content = await file.read()
        file_size = len(file_content)

        logger.info(
            f"Processing user KB upload: {file.filename} ({file_size} bytes) for user {user_id}"
        )

        # Step 2: Decode file content
        try:
            content_str = file_content.decode('utf-8')
        except UnicodeDecodeError:
            try:
                content_str = file_content.decode('latin-1')
            except:
                raise HTTPException(
                    status_code=400,
                    detail="Unable to decode file content. Please upload text files only."
                )

        # Step 3: Preprocess document
        preprocessed = await preprocessing_service.preprocess(
            filename=file.filename,
            content=content_str
        )

        # Step 4: Generate document ID and prepare metadata
        doc_id = str(uuid.uuid4())
        doc_title = title or file.filename
        doc_category = category or "general"
        doc_tags = [tag.strip() for tag in tags.split(",")] if tags else []

        metadata = {
            'filename': file.filename,
            'title': doc_title,
            'category': doc_category,
            'tags': ",".join(doc_tags),
            'description': description or "",
            'data_type': preprocessed.metadata.data_type.value,
            'file_size': file_size,
            'uploaded_at': to_json_compatible(datetime.now(timezone.utc)),
            'user_id': user_id
        }

        # Step 5: Store in user's KB collection
        await user_kb_store.add_documents(
            user_id=user_id,
            documents=[{
                'id': doc_id,
                'content': preprocessed.content,
                'metadata': metadata
            }]
        )

        logger.info(
            f"User KB document stored: {doc_id} for user {user_id} "
            f"({preprocessed.metadata.data_type.value}, {preprocessed.processed_size} chars)"
        )

        # Step 6: Return success response
        return JSONResponse(
            status_code=201,
            content={
                "status": "success",
                "message": "Document uploaded to knowledge base",
                "document_id": doc_id,
                "title": doc_title,
                "category": doc_category,
                "data_type": preprocessed.metadata.data_type.value,
                "original_size": preprocessed.original_size,
                "processed_size": preprocessed.processed_size,
                "compression_ratio": round(
                    preprocessed.original_size / preprocessed.processed_size
                    if preprocessed.processed_size > 0 else 1.0,
                    1
                )
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload user KB document: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload document: {str(e)}"
        )


@router.get(
    "/{user_id}/kb/documents",
    responses={
        200: {"description": "List of user's KB documents"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        503: {"model": ErrorResponse}
    }
)
@trace("api_list_user_kb_documents")
async def list_user_kb_documents(
    user_id: str,
    limit: Optional[int] = Query(None, description="Maximum documents to return"),
    offset: int = Query(0, description="Number of documents to skip"),
    category: Optional[str] = Query(None, description="Filter by category"),
    user_kb_store = Depends(get_user_kb_vector_store),
    current_user: DevUser = Depends(require_authentication)
):
    """
    List all documents in user's knowledge base.

    Returns metadata for all documents in the user's KB, with optional filtering
    by category and pagination support.

    **Access Control**: Users can only list their own documents.

    **Query Parameters**:
    - `limit`: Maximum number of documents to return (default: all)
    - `offset`: Number of documents to skip for pagination (default: 0)
    - `category`: Filter by document category (optional)

    **Example**:
    ```bash
    # List all documents
    curl "http://localhost:8000/api/v1/users/alice/kb/documents" \\
      -H "Authorization: Bearer dev_token"

    # List database runbooks only
    curl "http://localhost:8000/api/v1/users/alice/kb/documents?category=database" \\
      -H "Authorization: Bearer dev_token"

    # Paginate (10 documents at a time)
    curl "http://localhost:8000/api/v1/users/alice/kb/documents?limit=10&offset=0" \\
      -H "Authorization: Bearer dev_token"
    ```
    """
    # Access control: Users can only list their own KB
    if current_user.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Users can only access their own knowledge base."
        )

    try:
        # Get documents from user's KB
        documents = await user_kb_store.list_documents(
            user_id=user_id,
            limit=limit,
            offset=offset
        )

        # Apply category filter if specified
        if category:
            documents = [
                doc for doc in documents
                if doc.get('metadata', {}).get('category') == category
            ]

        # Get total count
        total_count = await user_kb_store.get_document_count(user_id)

        logger.info(
            f"Listed {len(documents)} documents from user {user_id} KB "
            f"(total: {total_count}, category: {category or 'all'})"
        )

        return {
            "status": "success",
            "user_id": user_id,
            "total_count": total_count,
            "returned_count": len(documents),
            "offset": offset,
            "documents": documents
        }

    except Exception as e:
        logger.error(f"Failed to list user KB documents: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list documents: {str(e)}"
        )


@router.delete(
    "/{user_id}/kb/documents/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Document deleted successfully"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse}
    }
)
@trace("api_delete_user_kb_document")
async def delete_user_kb_document(
    user_id: str,
    doc_id: str,
    user_kb_store = Depends(get_user_kb_vector_store),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Delete a specific document from user's knowledge base.

    Permanently removes the document from the user's KB. This action cannot be undone.

    **Access Control**: Users can only delete their own documents.

    **Example**:
    ```bash
    curl -X DELETE "http://localhost:8000/api/v1/users/alice/kb/documents/abc-123" \\
      -H "Authorization: Bearer dev_token"
    ```
    """
    # Access control: Users can only delete from their own KB
    if current_user.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Users can only manage their own knowledge base."
        )

    try:
        # Delete document
        await user_kb_store.delete_document(user_id=user_id, doc_id=doc_id)

        logger.info(f"Deleted document {doc_id} from user {user_id} KB")

        # Return 204 No Content (successful deletion)
        return None

    except Exception as e:
        logger.error(f"Failed to delete user KB document: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete document: {str(e)}"
        )


@router.get(
    "/{user_id}/kb/stats",
    responses={
        200: {"description": "User KB statistics"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        503: {"model": ErrorResponse}
    }
)
@trace("api_get_user_kb_stats")
async def get_user_kb_stats(
    user_id: str,
    user_kb_store = Depends(get_user_kb_vector_store),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Get statistics about user's knowledge base.

    Returns summary information about the user's KB including document count
    and category breakdown.

    **Access Control**: Users can only view their own KB stats.

    **Example**:
    ```bash
    curl "http://localhost:8000/api/v1/users/alice/kb/stats" \\
      -H "Authorization: Bearer dev_token"
    ```
    """
    # Access control: Users can only view their own KB stats
    if current_user.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Users can only access their own knowledge base."
        )

    try:
        # Get total count
        total_count = await user_kb_store.get_document_count(user_id)

        # Get all documents to calculate category breakdown
        all_docs = await user_kb_store.list_documents(user_id=user_id)

        # Calculate category breakdown
        category_counts = {}
        for doc in all_docs:
            category = doc.get('metadata', {}).get('category', 'general')
            category_counts[category] = category_counts.get(category, 0) + 1

        logger.info(f"Retrieved KB stats for user {user_id}: {total_count} documents")

        return {
            "status": "success",
            "user_id": user_id,
            "total_documents": total_count,
            "categories": category_counts
        }

    except Exception as e:
        logger.error(f"Failed to get user KB stats: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get KB stats: {str(e)}"
        )
