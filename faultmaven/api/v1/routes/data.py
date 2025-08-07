"""Refactored Data Routes - Phase 6.2

Purpose: Thin API layer for data operations with pure delegation pattern

This refactored module follows clean API architecture principles by removing
all business logic from the API layer and delegating to the service layer.

Key Changes from Original:
- Removed all business logic (file processing, classification, analysis)
- Pure delegation to DataServiceRefactored
- Simplified file upload handling 
- Proper dependency injection via DI container
- Clean separation of concerns (API vs Business logic)

Architecture Pattern:
API Route (validation + delegation) → Service Layer (business logic) → Core Domain
"""

import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from faultmaven.models import DataInsightsResponse, UploadedData
from faultmaven.api.v1.dependencies import get_data_service
from faultmaven.services.data_service import DataService
from faultmaven.infrastructure.observability.tracing import trace

router = APIRouter(prefix="/data", tags=["data_processing"])

logger = logging.getLogger(__name__)

# File size limit (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024


@router.post("/", response_model=UploadedData)
@trace("api_upload_data_compat")
async def upload_data_compat(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    description: Optional[str] = Form(None),
    data_service: DataService = Depends(get_data_service)
) -> UploadedData:
    """
    Compatibility endpoint for legacy tests - delegates to main upload function
    
    This endpoint maintains backward compatibility for existing tests that
    expect POST to /data instead of /data/upload.
    """
    return await upload_data(file, session_id, description, data_service)


@router.post("/upload", response_model=UploadedData)
@trace("api_upload_data")
async def upload_data(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    description: Optional[str] = Form(None),
    data_service: DataService = Depends(get_data_service)
) -> UploadedData:
    """
    Upload and process data with clean delegation pattern
    
    This endpoint follows the thin controller pattern:
    1. Basic input validation (file size, type)
    2. Pure delegation to service layer for all business logic
    3. Clean error boundary handling
    
    Args:
        file: File to upload
        session_id: Session identifier 
        description: Optional description of the data
        data_service: Injected DataServiceRefactored from DI container
        
    Returns:
        UploadedData with processing results
        
    Raises:
        HTTPException: On service layer errors (400, 404, 413, 500)
    """
    logger.info(f"Received data upload for session {session_id}: {file.filename}")
    
    try:
        # Basic file validation at API boundary
        if file.size and file.size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large: {file.size} bytes (max: {MAX_FILE_SIZE})"
            )
        
        # Read file content
        content = await file.read()
        content_str = content.decode("utf-8", errors="ignore")
        
        # Pure delegation - all business logic is in the service layer
        uploaded_data = await data_service.ingest_data(
            content=content_str,
            session_id=session_id,
            file_name=file.filename,
            file_size=len(content)
        )
        
        logger.info(f"Successfully uploaded data {uploaded_data.data_id}")
        return uploaded_data
        
    except ValueError as e:
        # Business logic validation errors
        logger.warning(f"Data upload validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except PermissionError as e:
        # Authorization/access errors
        logger.warning(f"Data upload authorization failed: {e}")
        raise HTTPException(status_code=403, detail="Access denied")
        
    except FileNotFoundError as e:
        # Resource not found errors (session, etc.)
        logger.warning(f"Resource not found: {e}")
        raise HTTPException(status_code=404, detail="Resource not found")
        
    except Exception as e:
        # Unexpected service layer errors
        logger.error(f"Data upload failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal server error during data upload"
        )


@router.post("/batch-upload", response_model=List[UploadedData])
@trace("api_batch_upload_data")
async def batch_upload_data(
    files: List[UploadFile] = File(...),
    session_id: str = Form(...),
    data_service: DataService = Depends(get_data_service)
) -> List[UploadedData]:
    """
    Batch upload multiple files with clean delegation
    
    Args:
        files: List of files to upload
        session_id: Session identifier
        data_service: Injected DataServiceRefactored
        
    Returns:
        List of UploadedData results
    """
    logger.info(f"Received batch upload of {len(files)} files for session {session_id}")
    
    try:
        # Validate batch size
        if len(files) > 10:
            raise HTTPException(
                status_code=400,
                detail="Too many files in batch (max: 10)"
            )
        
        # Prepare data items for batch processing
        data_items = []
        for file in files:
            if file.size and file.size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File {file.filename} too large: {file.size} bytes"
                )
            
            content = await file.read()
            content_str = content.decode("utf-8", errors="ignore")
            data_items.append((content_str, file.filename))
        
        # Delegate batch processing to service layer
        results = await data_service.batch_process(data_items, session_id)
        
        logger.info(f"Successfully processed {len(results)} files")
        return results
        
    except ValueError as e:
        logger.warning(f"Batch upload validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except Exception as e:
        logger.error(f"Batch upload failed unexpectedly: {e}")
        raise HTTPException(status_code=500, detail="Batch upload failed")


@router.post("/analyze/{data_id}", response_model=DataInsightsResponse)
@trace("api_analyze_data")
async def analyze_data(
    data_id: str,
    session_id: str = Form(...),
    data_service: DataService = Depends(get_data_service)
) -> DataInsightsResponse:
    """
    Analyze uploaded data with clean delegation
    
    Args:
        data_id: Data identifier to analyze
        session_id: Session identifier for validation
        data_service: Injected DataServiceRefactored
        
    Returns:
        DataInsightsResponse with analysis results
    """
    logger.info(f"Analyzing data {data_id} for session {session_id}")
    
    try:
        # Pure delegation to service layer
        insights = await data_service.analyze_data(data_id, session_id)
        
        logger.info(f"Successfully analyzed data {data_id}")
        return insights
        
    except ValueError as e:
        logger.warning(f"Data analysis validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except FileNotFoundError as e:
        logger.warning(f"Data not found: {e}")
        raise HTTPException(status_code=404, detail="Data not found")
        
    except Exception as e:
        logger.error(f"Data analysis failed: {e}")
        raise HTTPException(status_code=500, detail="Analysis failed")


@router.get("/session/{session_id}", response_model=List[UploadedData])
@trace("api_get_session_data")
async def get_session_data(
    session_id: str,
    limit: Optional[int] = 10,
    offset: Optional[int] = 0,
    data_service: DataService = Depends(get_data_service)
) -> List[UploadedData]:
    """
    Get all data for a session with clean delegation
    
    Args:
        session_id: Session identifier
        limit: Maximum number of results
        offset: Pagination offset
        data_service: Injected DataServiceRefactored
        
    Returns:
        List of UploadedData for the session
    """
    logger.info(f"Retrieving data for session {session_id}")
    
    try:
        # Input validation at API boundary
        if limit and (limit <= 0 or limit > 100):
            raise HTTPException(
                status_code=400,
                detail="Limit must be between 1 and 100"
            )
        if offset and offset < 0:
            raise HTTPException(
                status_code=400,
                detail="Offset must be non-negative"
            )
        
        # Delegate to service layer
        session_data = await data_service.get_session_data(session_id)
        
        # Apply pagination at API layer (simple approach)
        start_idx = offset or 0
        end_idx = start_idx + (limit or 10)
        paginated_data = session_data[start_idx:end_idx]
        
        return paginated_data
        
    except ValueError as e:
        logger.warning(f"Session data retrieval validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except FileNotFoundError as e:
        logger.warning(f"Session not found: {e}")
        raise HTTPException(status_code=404, detail="Session not found")
        
    except Exception as e:
        logger.error(f"Session data retrieval failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve session data")


@router.delete("/data/{data_id}")
@trace("api_delete_data")
async def delete_data(
    data_id: str,
    session_id: str,
    data_service: DataService = Depends(get_data_service)
):
    """
    Delete uploaded data with clean delegation
    
    Args:
        data_id: Data identifier to delete
        session_id: Session identifier for validation
        data_service: Injected DataServiceRefactored
        
    Returns:
        Success confirmation
    """
    logger.info(f"Deleting data {data_id} for session {session_id}")
    
    try:
        # Delegate deletion logic to service layer
        success = await data_service.delete_data(data_id, session_id)
        
        if success:
            return {"message": "Data deleted successfully", "data_id": data_id}
        else:
            raise HTTPException(status_code=500, detail="Deletion failed")
            
    except ValueError as e:
        logger.warning(f"Data deletion validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except FileNotFoundError as e:
        logger.warning(f"Data not found: {e}")
        raise HTTPException(status_code=404, detail="Data not found")
        
    except Exception as e:
        logger.error(f"Data deletion failed: {e}")
        raise HTTPException(status_code=500, detail="Deletion failed")


@router.get("/health")
@trace("api_data_health")
async def health_check(
    data_service: DataService = Depends(get_data_service)
):
    """
    Health check endpoint with service delegation
    
    Returns:
        Service health status
    """
    try:
        # Delegate health check to service layer
        health_status = await data_service.health_check()
        
        return {
            "status": "healthy",
            "service": "data_refactored",
            "details": health_status
        }
        
    except Exception as e:
        logger.error(f"Data health check failed: {e}")
        raise HTTPException(
            status_code=503,
            detail="Data service unavailable"
        )


# Compatibility functions for legacy tests
# These are stubs to support existing test infrastructure
def get_session_manager():
    """Compatibility function for legacy tests"""
    from faultmaven.container import container
    return container.session_service

def get_data_classifier():
    """Compatibility function for legacy tests"""
    from faultmaven.container import container
    return container.data_classifier

def get_log_processor():
    """Compatibility function for legacy tests"""
    from faultmaven.container import container
    return container.log_processor

def get_redis_client():
    """Compatibility function for legacy tests"""
    from faultmaven.container import container
    return container.redis_client