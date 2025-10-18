"""
Background Task: Case Collection Cleanup

Periodically cleans up orphaned case collections from CaseVectorStore.
Runs as a background task using APScheduler.

An "orphaned" collection is one that doesn't have a corresponding active case.
This is a safety net for collections that weren't properly deleted when cases closed.

Configuration:
- Cleanup interval: Every 6 hours (configurable)
- Cleanup method: Lifecycle-based (checks against active cases)

Usage:
    scheduler = start_case_cleanup_scheduler(case_vector_store, case_store)
    # ... app runs ...
    scheduler.shutdown()
"""

import logging
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import asyncio

from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.models.interfaces_case import ICaseStore


logger = logging.getLogger(__name__)


async def cleanup_orphaned_collections_task(
    case_vector_store: CaseVectorStore,
    case_store: ICaseStore
):
    """
    Background task to clean up orphaned case collections.

    An orphaned collection is one that doesn't have a corresponding active case.
    This is a safety net for collections that weren't properly deleted.

    Args:
        case_vector_store: CaseVectorStore instance
        case_store: CaseStore instance to get active case IDs
    """
    try:
        logger.info("Starting orphaned case collection cleanup task")

        # Get all active case IDs from CaseStore
        try:
            # Get all cases (we'll filter to just IDs)
            # Note: This might need pagination for very large deployments
            active_case_ids = await case_store.get_all_case_ids()
            logger.debug(f"Found {len(active_case_ids)} active cases in case store")
        except AttributeError:
            # If get_all_case_ids doesn't exist, skip cleanup this run
            logger.warning("CaseStore doesn't support get_all_case_ids(), skipping cleanup")
            return
        except Exception as e:
            logger.error(f"Failed to get active case IDs: {e}")
            return

        # Clean up orphaned collections
        deleted_count = await case_vector_store.cleanup_orphaned_collections(active_case_ids)

        if deleted_count > 0:
            logger.info(f"Case cleanup completed: {deleted_count} orphaned collections deleted")
        else:
            logger.debug("Case cleanup completed: no orphaned collections found")

    except Exception as e:
        logger.error(f"Error during case cleanup task: {e}", exc_info=True)


def _sync_cleanup_wrapper(case_vector_store: CaseVectorStore, case_store: ICaseStore):
    """
    Synchronous wrapper for async cleanup task.

    APScheduler requires synchronous functions, so we use asyncio.run().
    """
    try:
        asyncio.run(cleanup_orphaned_collections_task(case_vector_store, case_store))
    except Exception as e:
        logger.error(f"Error in sync cleanup wrapper: {e}", exc_info=True)


def start_case_cleanup_scheduler(
    case_vector_store: CaseVectorStore,
    case_store: ICaseStore,
    interval_hours: int = 6
) -> Optional[BackgroundScheduler]:
    """
    Start background scheduler for case collection cleanup.

    Args:
        case_vector_store: CaseVectorStore instance
        case_store: CaseStore instance for getting active case IDs
        interval_hours: Cleanup interval in hours (default: 6)

    Returns:
        BackgroundScheduler instance (or None if initialization fails)
    """
    try:
        scheduler = BackgroundScheduler()

        # Schedule cleanup task to run every N hours
        scheduler.add_job(
            func=lambda: _sync_cleanup_wrapper(case_vector_store, case_store),
            trigger=IntervalTrigger(hours=interval_hours),
            id='case_collection_cleanup',
            name='Clean up orphaned case collections',
            replace_existing=True
        )

        scheduler.start()
        logger.info(f"Case cleanup scheduler started (interval: {interval_hours} hours, lifecycle-based)")

        return scheduler

    except Exception as e:
        logger.error(f"Failed to start case cleanup scheduler: {e}", exc_info=True)
        return None


def stop_case_cleanup_scheduler(scheduler: Optional[BackgroundScheduler]):
    """
    Stop the case cleanup scheduler.

    Args:
        scheduler: BackgroundScheduler instance (or None)
    """
    if scheduler:
        try:
            scheduler.shutdown()
            logger.info("Case cleanup scheduler stopped")
        except Exception as e:
            logger.warning(f"Error stopping case cleanup scheduler: {e}")
