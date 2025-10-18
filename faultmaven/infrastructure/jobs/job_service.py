"""Job Service

Purpose: Async job management with Redis persistence for long-running operations

This service provides:
- Job creation with unique IDs and Redis persistence
- Job status tracking (pending, running, completed, failed, cancelled)
- TTL-based job cleanup and garbage collection
- Consistent 202 → Location → 303/200 workflow
- Retry-After headers for job polling

Architecture Integration:
- Uses Redis for job state persistence
- Integrates with container.py dependency injection
- Follows service layer patterns
- Supports async operation tracking
"""

import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from faultmaven.utils.serialization import to_json_compatible
from faultmaven.models import parse_utc_timestamp
from typing import Optional, Dict, Any, List
from uuid import uuid4
from enum import Enum

from faultmaven.models.interfaces import IJobService
from faultmaven.models.api import JobStatus
from faultmaven.exceptions import ServiceException, ValidationException

logger = logging.getLogger(__name__)


class JobStatusEnum(str, Enum):
    """Job status enumeration."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobService(IJobService):
    """Redis-backed job management service."""
    
    def __init__(self, redis_client=None):
        self.redis_client = redis_client
        self.job_prefix = "job:"
        self.default_ttl = 86400  # 24 hours TTL
        self.retry_after_seconds = 5  # Default polling interval
        
    async def create_job(
        self, 
        job_type: str, 
        payload: Dict[str, Any] = None,
        ttl_seconds: Optional[int] = None
    ) -> str:
        """Create a new job with initial status."""
        job_id = f"job_{uuid4().hex[:12]}"
        
        job_data = {
            "job_id": job_id,
            "job_type": job_type,
            "status": JobStatusEnum.PENDING,
            "payload": payload or {},
            "progress": 0,
            "result": None,
            "error": None,
            "created_at": to_json_compatible(datetime.now(timezone.utc)),
            "updated_at": to_json_compatible(datetime.now(timezone.utc)),
            "ttl_seconds": ttl_seconds or self.default_ttl
        }
        
        try:
            if self.redis_client:
                await self.redis_client.setex(
                    f"{self.job_prefix}{job_id}",
                    ttl_seconds or self.default_ttl,
                    json.dumps(job_data)
                )
                logger.info(f"Created job {job_id} of type {job_type}")
            else:
                logger.warning(f"Redis not available - job {job_id} created without persistence")
                
        except Exception as e:
            logger.error(f"Failed to create job {job_id}: {e}")
            raise ServiceException(f"Job creation failed: {e}")
            
        return job_id
    
    async def get_job(self, job_id: str) -> Optional[JobStatus]:
        """Retrieve job status by ID."""
        try:
            if not self.redis_client:
                logger.warning("Redis not available for job retrieval")
                return None
                
            job_data_str = await self.redis_client.get(f"{self.job_prefix}{job_id}")
            if not job_data_str:
                return None
                
            job_data = json.loads(job_data_str)
            
            return JobStatus(
                job_id=job_data["job_id"],
                status=job_data["status"],
                progress=job_data.get("progress"),
                result=job_data.get("result"),
                error=job_data.get("error"),
                created_at=job_data["created_at"],
                updated_at=job_data["updated_at"]
            )
            
        except Exception as e:
            logger.error(f"Failed to retrieve job {job_id}: {e}")
            raise ServiceException(f"Job retrieval failed: {e}")
    
    async def update_job_status(
        self, 
        job_id: str, 
        status: JobStatusEnum,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> bool:
        """Update job status and metadata."""
        try:
            if not self.redis_client:
                logger.warning(f"Redis not available - cannot update job {job_id}")
                return False
                
            # Get existing job data
            job_data_str = await self.redis_client.get(f"{self.job_prefix}{job_id}")
            if not job_data_str:
                logger.warning(f"Job {job_id} not found for update")
                return False
                
            job_data = json.loads(job_data_str)
            
            # Update fields
            job_data["status"] = status.value
            job_data["updated_at"] = to_json_compatible(datetime.now(timezone.utc))
            
            if progress is not None:
                job_data["progress"] = progress
            if result is not None:
                job_data["result"] = result
            if error is not None:
                job_data["error"] = error
                
            # Save updated data with remaining TTL
            ttl = await self.redis_client.ttl(f"{self.job_prefix}{job_id}")
            ttl = max(ttl, 300)  # Ensure at least 5 minutes remaining
            
            await self.redis_client.setex(
                f"{self.job_prefix}{job_id}",
                ttl,
                json.dumps(job_data)
            )
            
            logger.info(f"Updated job {job_id} status to {status.value}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update job {job_id}: {e}")
            raise ServiceException(f"Job update failed: {e}")
    
    async def start_job(self, job_id: str) -> bool:
        """Mark job as running."""
        return await self.update_job_status(job_id, JobStatusEnum.RUNNING, progress=0)
    
    async def complete_job(
        self, 
        job_id: str, 
        result: Dict[str, Any],
        progress: int = 100
    ) -> bool:
        """Mark job as completed with results."""
        return await self.update_job_status(
            job_id, 
            JobStatusEnum.COMPLETED, 
            progress=progress,
            result=result
        )
    
    async def fail_job(self, job_id: str, error: str) -> bool:
        """Mark job as failed with error message."""
        return await self.update_job_status(
            job_id,
            JobStatusEnum.FAILED,
            error=error
        )
    
    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a job."""
        return await self.update_job_status(job_id, JobStatusEnum.CANCELLED)
    
    async def cleanup_expired_jobs(self, batch_size: int = 100) -> int:
        """Garbage collect expired jobs (Redis handles TTL automatically)."""
        try:
            if not self.redis_client:
                return 0
                
            # Find all job keys
            job_keys = await self.redis_client.keys(f"{self.job_prefix}*")
            
            # Check for jobs that should be cleaned up manually (completed > 1 hour ago)
            cleaned_count = 0
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=1)
            
            for key in job_keys[:batch_size]:  # Process in batches
                try:
                    job_data_str = await self.redis_client.get(key)
                    if job_data_str:
                        job_data = json.loads(job_data_str)
                        status = job_data.get("status")
                        updated_at = parse_utc_timestamp( job_data.get("updated_at", "").replace("Z", "")
                        )
                        
                        # Clean up completed/failed jobs older than 1 hour
                        if (status in [JobStatusEnum.COMPLETED, JobStatusEnum.FAILED] and 
                            updated_at < cutoff_time):
                            await self.redis_client.delete(key)
                            cleaned_count += 1
                            
                except Exception as e:
                    logger.warning(f"Error processing job key {key} for cleanup: {e}")
                    
            logger.info(f"Cleaned up {cleaned_count} expired jobs")
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Job cleanup failed: {e}")
            return 0
    
    async def list_jobs(
        self, 
        status_filter: Optional[JobStatusEnum] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[JobStatus]:
        """List jobs with optional filtering."""
        try:
            if not self.redis_client:
                return []
                
            job_keys = await self.redis_client.keys(f"{self.job_prefix}*")
            jobs = []
            
            for key in job_keys:
                try:
                    job_data_str = await self.redis_client.get(key)
                    if job_data_str:
                        job_data = json.loads(job_data_str)
                        
                        # Apply status filter
                        if status_filter and job_data.get("status") != status_filter.value:
                            continue
                            
                        jobs.append(JobStatus(
                            job_id=job_data["job_id"],
                            status=job_data["status"],
                            progress=job_data.get("progress"),
                            result=job_data.get("result"),
                            error=job_data.get("error"),
                            created_at=job_data["created_at"],
                            updated_at=job_data["updated_at"]
                        ))
                        
                except Exception as e:
                    logger.warning(f"Error processing job key {key}: {e}")
            
            # Sort by created_at (newest first)
            jobs.sort(key=lambda x: x.created_at, reverse=True)
            
            # Apply pagination
            return jobs[offset:offset + limit]
            
        except Exception as e:
            logger.error(f"Failed to list jobs: {e}")
            return []
    
    def get_retry_after_seconds(self, job_status: JobStatus) -> int:
        """Get recommended retry-after interval based on job status."""
        if job_status.status == JobStatusEnum.PENDING:
            return 2  # Check more frequently for pending jobs
        elif job_status.status == JobStatusEnum.RUNNING:
            return 5  # Standard polling interval
        else:
            # Terminal states - no need to poll
            return 0