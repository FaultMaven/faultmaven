"""Case Cleanup Job - Standalone entrypoint for orphaned case collection cleanup.

This job can be run independently via CLI without requiring a running web server.
It cleans up orphaned case collections from CaseVectorStore.

Usage:
    python -m faultmaven.jobs.run case_cleanup

An "orphaned" collection is one that doesn't have a corresponding active case.
This is a safety net for collections that weren't properly deleted when cases closed.
"""

import logging
from typing import Any, Dict, Optional

from faultmaven.config.settings import FaultMavenSettings


logger = logging.getLogger(__name__)


async def run(
    settings: FaultMavenSettings,
    container: Any,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run case cleanup job.

    This is the standalone job entrypoint. It can be invoked via CLI
    without requiring a running web server or scheduler.

    Args:
        settings: FaultMaven settings instance
        container: DI container with initialized services
        **kwargs: Additional job parameters (unused)

    Returns:
        Job result dictionary with:
        - status: "completed" or "failed"
        - deleted_count: Number of orphaned collections deleted
        - active_cases: Number of active cases found
        - error: Error message if failed
    """
    logger.info("Starting case cleanup job")

    result: Dict[str, Any] = {
        "job": "case_cleanup",
        "status": "completed",
        "deleted_count": 0,
        "active_cases": 0,
    }

    try:
        # Get required services from container
        case_vector_store = getattr(container, 'case_vector_store', None)
        case_store = getattr(container, 'case_store', None)

        if not case_vector_store:
            logger.warning("CaseVectorStore not available, skipping cleanup")
            result["status"] = "skipped"
            result["reason"] = "case_vector_store_unavailable"
            return result

        if not case_store:
            logger.warning("CaseStore not available, skipping cleanup")
            result["status"] = "skipped"
            result["reason"] = "case_store_unavailable"
            return result

        # Get all active case IDs from CaseStore
        try:
            active_case_ids = await case_store.get_all_case_ids()
            result["active_cases"] = len(active_case_ids)
            logger.debug(f"Found {len(active_case_ids)} active cases in case store")
        except AttributeError:
            logger.warning("CaseStore doesn't support get_all_case_ids(), skipping cleanup")
            result["status"] = "skipped"
            result["reason"] = "get_all_case_ids_not_supported"
            return result

        # Clean up orphaned collections
        deleted_count = await case_vector_store.cleanup_orphaned_collections(active_case_ids)
        result["deleted_count"] = deleted_count

        if deleted_count > 0:
            logger.info(f"Case cleanup completed: {deleted_count} orphaned collections deleted")
        else:
            logger.debug("Case cleanup completed: no orphaned collections found")

    except Exception as e:
        logger.error(f"Case cleanup job failed: {e}", exc_info=True)
        result["status"] = "failed"
        result["error"] = str(e)

    return result


# Job metadata for CLI discovery
JOB_NAME = "case_cleanup"
JOB_DESCRIPTION = "Clean up orphaned case collections from vector store"
