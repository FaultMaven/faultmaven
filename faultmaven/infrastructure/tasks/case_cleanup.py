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
    scheduler = start_case_cleanup_scheduler(case_vector_store, case_repository)
    # ... app runs ...
    scheduler.shutdown()
"""

import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.modules.case.contracts import ICaseRepository

logger = logging.getLogger(__name__)


async def cleanup_orphaned_collections_task(
    case_vector_store: CaseVectorStore, case_repository: ICaseRepository
):
    """
    Background task to clean up orphaned case collections.

    An orphaned collection is one with NO corresponding case row at all (any
    state — terminal/archived cases keep their collections). This is a safety
    net for collections that weren't properly deleted.

    Args:
        case_vector_store: CaseVectorStore instance
        case_repository: Case repository for the reference case-id set
    """
    try:
        logger.info("Starting orphaned case collection cleanup task")

        # The reference set: every case row's id, any state.
        # Note: This might need pagination for very large deployments.
        try:
            active_case_ids = await case_repository.list_all_case_ids()
            logger.debug(f"Found {len(active_case_ids)} case rows in the database")
        except Exception as e:
            logger.error(f"Failed to get case IDs: {e}")
            return

        # Clean up orphaned collections. The per-candidate re-check closes the
        # snapshot race (a case created after list_all_case_ids() must not
        # lose its collection).
        async def _case_exists(case_id: str) -> bool:
            return await case_repository.get(case_id) is not None

        deleted_count = await case_vector_store.cleanup_orphaned_collections(
            active_case_ids, case_exists=_case_exists
        )

        if deleted_count > 0:
            logger.info(
                f"Case cleanup completed: {deleted_count} orphaned collections deleted"
            )
        else:
            logger.debug("Case cleanup completed: no orphaned collections found")

    except Exception as e:
        logger.error(f"Error during case cleanup task: {e}", exc_info=True)


def _sync_cleanup_wrapper(
    case_vector_store: CaseVectorStore, case_repository: ICaseRepository
):
    """
    Synchronous wrapper for async cleanup task.

    APScheduler requires synchronous functions, so we use asyncio.run().
    """
    try:
        asyncio.run(
            cleanup_orphaned_collections_task(case_vector_store, case_repository)
        )
    except Exception as e:
        logger.error(f"Error in sync cleanup wrapper: {e}", exc_info=True)


def start_case_cleanup_scheduler(
    case_vector_store: CaseVectorStore,
    case_repository: ICaseRepository,
    interval_hours: int = 6,
    is_multi_tenant: bool = False,
) -> Optional[BackgroundScheduler]:
    """
    Start background scheduler for case collection cleanup.

    Args:
        case_vector_store: CaseVectorStore instance
        case_repository: Case repository for the reference case-id set
        interval_hours: Cleanup interval in hours (default: 6)
        is_multi_tenant: Whether the deployment runs the multi-tenant provider.
            The cleanup task is cross-tenant scoped (it diffs the DB case-id
            set against ChromaDB collections, which are not org-partitioned),
            but RLS scopes a background task's DB reads to whatever org the
            tenant contextvar holds — under multi, its default (the never-
            seeded Standalone org) — a partial view that would classify other
            tenants' collections as orphaned and delete them. Refused under
            multi (ADR-010 P3 / issue #629).

    Returns:
        BackgroundScheduler instance (or None if refused or initialization fails)
    """
    if is_multi_tenant:
        logger.warning(
            "In-process case-cleanup scheduler refused under the multi-tenant "
            "provider: the cleanup task needs a cross-tenant case-id view, but "
            "RLS scopes background DB reads to the single org bound in the "
            "tenant context — a partial view would delete other tenants' "
            "collections (issue #629)."
        )
        return None

    try:
        scheduler = BackgroundScheduler()

        # Schedule cleanup task to run every N hours
        scheduler.add_job(
            func=lambda: _sync_cleanup_wrapper(case_vector_store, case_repository),
            trigger=IntervalTrigger(hours=interval_hours),
            id="case_collection_cleanup",
            name="Clean up orphaned case collections",
            replace_existing=True,
        )

        scheduler.start()
        logger.info(
            f"Case cleanup scheduler started (interval: {interval_hours} hours, lifecycle-based)"
        )

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
