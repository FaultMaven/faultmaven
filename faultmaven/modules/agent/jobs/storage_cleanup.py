"""Storage Cleanup Job for Orphaned Files

This module implements garbage collection for orphaned files in storage that
were uploaded but never linked to evidence records due to processing failures.

Design Reference:
- docs/architecture/data-processing/EVIDENCE-CREATION-FAILURE-MODES.md
- Storage Cleanup Strategy (Approach 1: TTL-Based Cleanup)

Key Features:
- TTL-based cleanup (files >24h old with no evidence)
- Batch processing to avoid overwhelming storage API
- Metrics tracking for orphaned file rate
- Safe deletion with existence checks
- Scheduled daily execution (2 AM)

Scheduling:
- Run daily at 2 AM via cron or job scheduler
- Usage: python -m faultmaven.jobs.run storage_cleanup
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from faultmaven.container import container
from faultmaven.infrastructure.storage.base import IFileStorageBackend, StoredFile

logger = logging.getLogger(__name__)

# Job metadata for CLI runner
JOB_DESCRIPTION = "Clean up orphaned files in storage (>24h old with no evidence)"

# Configuration
DEFAULT_TTL_HOURS = 24
DEFAULT_BATCH_SIZE = 100
EVIDENCE_PREFIX = "evidence/"


async def cleanup_orphaned_files(
    ttl_hours: int = DEFAULT_TTL_HOURS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Delete orphaned files uploaded >TTL hours ago with no evidence record.

    This job scans the evidence storage bucket for files that:
    1. Were uploaded more than ttl_hours ago (based on metadata)
    2. Have no corresponding evidence record in the database

    These files are "orphaned" - they were uploaded successfully but the
    subsequent LLM analysis or database insert failed, and all retries
    were exhausted or timed out.

    Args:
        ttl_hours: Delete files older than this many hours (default: 24)
        batch_size: Maximum files to process per run (default: 100)
        dry_run: If True, log actions without deleting (default: False)

    Returns:
        Result dictionary with cleanup statistics

    Metrics:
        - evidence.orphaned_files_found (gauge)
        - evidence.orphaned_files_cleaned (gauge)
        - evidence.orphaned_files_failed (counter)
    """
    logger.info(
        f"Starting storage cleanup job (TTL: {ttl_hours}h, "
        f"batch: {batch_size}, dry_run: {dry_run})"
    )

    start_time = datetime.now(UTC)

    try:
        # Get storage backend and case repository
        storage_backend = container.get_storage_backend()
        case_repository = container.get_case_repository()
        metrics_collector = container.get_metrics_collector()

        # Calculate cutoff time
        cutoff_time = datetime.now(UTC) - timedelta(hours=ttl_hours)
        logger.info(f"Cutoff time: {cutoff_time.isoformat()}")

        # List files in evidence storage
        files = await _list_evidence_files(
            storage_backend=storage_backend,
            prefix=EVIDENCE_PREFIX,
        )

        logger.info(f"Found {len(files)} total files in evidence storage")

        # Filter files older than cutoff
        old_files = []
        for file_info in files:
            if file_info.created_at < cutoff_time:
                old_files.append(file_info)

        logger.info(f"Found {len(old_files)} files older than {ttl_hours}h cutoff")

        # Record total old files metric
        try:
            await metrics_collector.gauge(
                "evidence.orphaned_files_found", len(old_files)
            )
        except Exception as e:
            logger.warning(f"Failed to record metric: {e}")

        # Check each file for evidence record
        orphaned_files = []
        checked_files = 0

        for file_info in old_files[:batch_size]:  # Process in batches
            checked_files += 1

            # Check if evidence record exists for this file
            evidence_exists = await _evidence_exists_for_file(
                case_repository=case_repository, content_ref=file_info.key
            )

            if not evidence_exists:
                orphaned_files.append(file_info)

            # Log progress every 10 files
            if checked_files % 10 == 0:
                logger.info(f"Checked {checked_files}/{len(old_files)} files...")

        logger.info(
            f"Found {len(orphaned_files)} orphaned files "
            f"(files without evidence records)"
        )

        # Delete orphaned files
        deleted_count = 0
        failed_deletes = []

        for file_info in orphaned_files:
            try:
                if dry_run:
                    logger.info(
                        f"[DRY RUN] Would delete orphaned file: {file_info.key}"
                    )
                    deleted_count += 1
                else:
                    # Delete the file
                    success = await storage_backend.delete_file(file_info.key)

                    if success:
                        logger.info(f"Deleted orphaned file: {file_info.key}")
                        deleted_count += 1
                    else:
                        logger.warning(f"Failed to delete (not found): {file_info.key}")
                        failed_deletes.append(
                            {
                                "key": file_info.key,
                                "reason": "File not found during delete",
                            }
                        )

            except Exception as e:
                logger.error(f"Error deleting file {file_info.key}: {e}")
                failed_deletes.append({"key": file_info.key, "error": str(e)})

                # Record failure metric
                try:
                    await metrics_collector.increment("evidence.orphaned_files_failed")
                except Exception:
                    pass

        # Record cleanup metrics
        try:
            await metrics_collector.gauge(
                "evidence.orphaned_files_cleaned", deleted_count
            )
        except Exception as e:
            logger.warning(f"Failed to record metric: {e}")

        # Calculate duration
        duration_seconds = (datetime.now(UTC) - start_time).total_seconds()

        result = {
            "status": "completed",
            "ttl_hours": ttl_hours,
            "batch_size": batch_size,
            "dry_run": dry_run,
            "total_files": len(files),
            "old_files": len(old_files),
            "checked_files": checked_files,
            "orphaned_files": len(orphaned_files),
            "deleted_count": deleted_count,
            "failed_deletes": len(failed_deletes),
            "failed_details": failed_deletes if failed_deletes else None,
            "duration_seconds": duration_seconds,
            "completed_at": datetime.now(UTC).isoformat(),
        }

        logger.info(
            f"Storage cleanup completed: "
            f"deleted {deleted_count} orphaned files "
            f"in {duration_seconds:.2f}s"
        )

        return result

    except Exception as e:
        logger.exception(f"Storage cleanup job failed: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "duration_seconds": (datetime.now(UTC) - start_time).total_seconds(),
        }


async def _list_evidence_files(
    storage_backend: IFileStorageBackend,
    prefix: str,
) -> List[StoredFile]:
    """
    List all files in evidence storage with prefix.

    Note: This is a simplified implementation. For production with S3,
    implement pagination using list_objects_v2 with continuation tokens
    to handle buckets with >1000 files.
    """
    files: List[StoredFile] = []

    try:
        # For filesystem backend, list directory
        if storage_backend.get_storage_type().value == "filesystem":
            files = await _list_filesystem_files(storage_backend, prefix)

        # For S3 backend, use list_objects
        elif storage_backend.get_storage_type().value == "s3":
            files = await _list_s3_files(storage_backend, prefix)

        else:
            logger.warning(
                f"Unsupported storage backend: {storage_backend.get_storage_type()}"
            )

    except Exception as e:
        logger.error(f"Failed to list files with prefix {prefix}: {e}")

    return files


async def _list_filesystem_files(
    storage_backend: IFileStorageBackend, prefix: str
) -> List[StoredFile]:
    """List files from filesystem storage backend."""
    import os
    from pathlib import Path

    files: List[StoredFile] = []

    try:
        # Get storage root from backend
        # Assuming filesystem backend has a root_path attribute
        if hasattr(storage_backend, "root_path"):
            root_path = Path(storage_backend.root_path)
            evidence_path = root_path / prefix.rstrip("/")

            if evidence_path.exists() and evidence_path.is_dir():
                for file_path in evidence_path.rglob("*"):
                    if file_path.is_file():
                        # Get file metadata
                        stat = file_path.stat()
                        relative_key = str(file_path.relative_to(root_path))

                        file_info = StoredFile(
                            key=relative_key,
                            size_bytes=stat.st_size,
                            content_type="application/octet-stream",
                            created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                            metadata=None,
                        )
                        files.append(file_info)

    except Exception as e:
        logger.error(f"Failed to list filesystem files: {e}")

    return files


async def _list_s3_files(
    storage_backend: IFileStorageBackend, prefix: str
) -> List[StoredFile]:
    """
    List files from S3 storage backend.

    Note: For production, implement pagination to handle large buckets.
    """
    files: List[StoredFile] = []

    try:
        # S3 backend should have a method to list objects
        # This is a placeholder - actual implementation depends on S3 backend
        if hasattr(storage_backend, "list_objects"):
            objects = await storage_backend.list_objects(prefix=prefix)

            for obj in objects:
                file_info = StoredFile(
                    key=obj.get("Key", ""),
                    size_bytes=obj.get("Size", 0),
                    content_type=obj.get("ContentType", "application/octet-stream"),
                    created_at=obj.get("LastModified", datetime.now(UTC)),
                    metadata=obj.get("Metadata"),
                )
                files.append(file_info)

    except Exception as e:
        logger.error(f"Failed to list S3 files: {e}")

    return files


async def _evidence_exists_for_file(case_repository: Any, content_ref: str) -> bool:
    """
    Check if evidence record exists for the given content_ref.

    This performs a database query to find any evidence with matching content_ref.

    Returns:
        True if evidence exists, False otherwise
    """
    try:
        # Query all cases for evidence with matching content_ref
        # This is expensive for large databases - consider adding an index on content_ref

        # For now, check if repository has a method to query by content_ref
        if hasattr(case_repository, "find_evidence_by_content_ref"):
            evidence = await case_repository.find_evidence_by_content_ref(content_ref)
            return evidence is not None

        # Fallback: Query all cases (not recommended for production)
        logger.warning(
            "No efficient content_ref query method - falling back to full scan"
        )

        # Get all cases (limit to recent cases for performance)
        # This is a placeholder - actual implementation depends on repository
        all_cases = await case_repository.list_cases(limit=1000)

        for case in all_cases:
            if not hasattr(case, "evidence"):
                continue

            for evidence in case.evidence:
                if evidence.content_ref == content_ref:
                    return True

        return False

    except Exception as e:
        logger.error(f"Failed to check evidence existence for {content_ref}: {e}")
        # Err on the side of caution - assume evidence exists if check fails
        return True


# =============================================================================
# Job Runner Integration
# =============================================================================


async def run(
    settings: Any = None,
    container: Any = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Job runner entry point for CLI execution.

    This function is called by faultmaven.jobs.run CLI runner.

    Args:
        settings: Application settings (injected by runner)
        container: DI container (injected by runner)
        ttl_hours: File age threshold for deletion
        batch_size: Maximum files to process
        dry_run: Preview mode without actual deletion
        **kwargs: Additional arguments

    Returns:
        Job result dictionary

    Usage:
        python -m faultmaven.jobs.run storage_cleanup
        python -m faultmaven.jobs.run storage_cleanup --dry-run
    """
    logger.info("Storage cleanup job started via CLI runner")

    result = await cleanup_orphaned_files(
        ttl_hours=ttl_hours, batch_size=batch_size, dry_run=dry_run
    )

    logger.info(f"Storage cleanup job result: {result['status']}")

    return result
