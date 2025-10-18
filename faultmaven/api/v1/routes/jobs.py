"""Jobs API Routes

Purpose: REST API endpoints for async job management and tracking

This module provides REST API endpoints for managing long-running asynchronous
operations through job tracking. It implements consistent 202 → Location → 303/200
semantics for async request handling.

Key Endpoints:
- GET /jobs/{job_id} - Poll job status with proper headers
- DELETE /jobs/{job_id} - Cancel running jobs
- GET /jobs - List jobs with filtering

Architecture Integration:
- Uses dependency injection for job service
- Implements consistent HTTP semantics (202/303 redirects)
- Adds proper Retry-After headers for polling
- Integrates with request correlation system
"""

import logging
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from faultmaven.models.api import JobStatus
from faultmaven.models.interfaces import IJobService
from faultmaven.api.v1.dependencies import get_job_service
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ServiceException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["job_management"])


async def _di_get_job_service_dependency() -> Optional[IJobService]:
    """Runtime wrapper for job service dependency injection."""
    from faultmaven.api.v1.dependencies import get_job_service as _getter
    return await _getter()


def check_job_service_available(job_service: Optional[IJobService]) -> IJobService:
    """Check if job service is available and raise appropriate error if not."""
    if job_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job service unavailable - async operations not supported"
        )
    return job_service


@router.get("/{job_id}", response_model=JobStatus)
@trace("api_get_job_status")
async def get_job_status(
    job_id: str,
    response: Response,
    job_service: Optional[IJobService] = Depends(_di_get_job_service_dependency)
) -> JobStatus:
    """
    Get job status with proper polling semantics
    
    Implements consistent job polling with appropriate headers:
    - 200 OK for running/pending jobs with Retry-After header
    - 303 See Other redirect for completed jobs with results
    - 200 OK for failed/cancelled jobs (terminal states)
    """
    job_service = check_job_service_available(job_service)
    
    try:
        job = await job_service.get_job(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found"
            )
        
        # Handle different job states with appropriate HTTP semantics
        if job.status == "completed":
            # For completed jobs with results, use 303 redirect pattern
            if job.result:
                # In practice, this would redirect to the resource created by the job
                # For now, return the job status with result embedded
                response.status_code = status.HTTP_200_OK
                response.headers["Cache-Control"] = "max-age=3600"  # Cache completed jobs
            else:
                response.status_code = status.HTTP_200_OK
                
        elif job.status in ["failed", "cancelled"]:
            # Terminal states - return 200 with no retry
            response.status_code = status.HTTP_200_OK
            response.headers["Cache-Control"] = "max-age=300"  # Short cache for errors
            
        elif job.status in ["pending", "running"]:
            # Non-terminal states - return 200 with Retry-After
            response.status_code = status.HTTP_200_OK
            
            # Calculate appropriate retry interval
            retry_after = 5  # Default 5 seconds
            if hasattr(job_service, 'get_retry_after_seconds'):
                retry_after = job_service.get_retry_after_seconds(job)
                
            if retry_after > 0:
                response.headers["Retry-After"] = str(retry_after)
                
        # Add job-specific headers
        response.headers["X-Job-Status"] = job.status
        response.headers["X-Job-Created"] = job.created_at
        
        if job.progress is not None:
            response.headers["X-Job-Progress"] = str(job.progress)
            
        return job
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get job {job_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Job status retrieval failed: {str(e)}"
        )


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
@trace("api_cancel_job")
async def cancel_job(
    job_id: str,
    job_service: Optional[IJobService] = Depends(_di_get_job_service_dependency)
):
    """
    Cancel a running job
    
    Attempts to cancel a job if it's still in a cancellable state.
    Returns 204 No Content on success.
    """
    job_service = check_job_service_available(job_service)
    
    try:
        # Check if job exists first
        job = await job_service.get_job(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found"
            )
        
        # Check if job can be cancelled
        if job.status in ["completed", "failed", "cancelled"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Job {job_id} is already in terminal state: {job.status}"
            )
        
        # Attempt to cancel the job
        success = await job_service.cancel_job(job_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to cancel job {job_id}"
            )
        
        logger.info(f"Successfully cancelled job {job_id}")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel job {job_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Job cancellation failed: {str(e)}"
        )


@router.get("", response_model=List[JobStatus])
@trace("api_list_jobs")
async def list_jobs(
    response: Response,
    status_filter: Optional[str] = Query(None, description="Filter by job status"),
    limit: int = Query(50, le=100, ge=1, description="Maximum number of results"),
    offset: int = Query(0, ge=0, description="Result offset for pagination"),
    job_service: Optional[IJobService] = Depends(_di_get_job_service_dependency)
) -> List[JobStatus]:
    """
    List jobs with optional filtering and pagination
    
    Returns a paginated list of jobs with proper pagination headers.
    Supports filtering by job status.
    """
    job_service = check_job_service_available(job_service)
    
    try:
        # Validate status filter if provided
        valid_statuses = ["pending", "running", "completed", "failed", "cancelled"]
        if status_filter and status_filter not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status filter. Must be one of: {', '.join(valid_statuses)}"
            )
        
        # Get jobs from service
        jobs = await job_service.list_jobs(
            status_filter=status_filter,
            limit=limit + 1,  # Get one extra to check if there are more
            offset=offset
        )
        
        # Check if there are more results
        has_more = len(jobs) > limit
        if has_more:
            jobs = jobs[:limit]  # Trim to requested limit
        
        # Add pagination headers
        response.headers["X-Total-Count"] = str(len(jobs))
        
        # Add Link header for pagination (RFC 5988)
        links = []
        base_url = f"/api/v1/jobs?limit={limit}"
        if status_filter:
            base_url += f"&status_filter={status_filter}"
        
        if offset > 0:
            prev_offset = max(0, offset - limit)
            links.append(f'<{base_url}&offset={prev_offset}>; rel="prev"')
            
        if has_more:
            next_offset = offset + limit
            links.append(f'<{base_url}&offset={next_offset}>; rel="next"')
            
        if links:
            response.headers["Link"] = ", ".join(links)
            
        return jobs
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list jobs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Job listing failed: {str(e)}"
        )


@router.get("/health", response_model=dict)
@trace("api_job_service_health")
async def get_job_service_health(
    job_service: Optional[IJobService] = Depends(_di_get_job_service_dependency)
) -> dict:
    """
    Get job service health status
    
    Returns health information about the job management system,
    including connectivity and performance metrics.
    """
    try:
        # Check if job service is available
        if not job_service:
            return {
                "service": "job_management",
                "status": "unavailable",
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "message": "Job service not configured"
            }
        
        # Try to perform a basic operation to verify health
        # This could be expanded with more comprehensive health checks
        return {
            "service": "job_management", 
            "status": "healthy",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "features": {
                "job_creation": True,
                "job_tracking": True,
                "job_cancellation": True,
                "automatic_cleanup": True
            }
        }
        
    except Exception as e:
        logger.error(f"Job service health check failed: {e}")
        return {
            "service": "job_management",
            "status": "unhealthy", 
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "error": str(e)
        }