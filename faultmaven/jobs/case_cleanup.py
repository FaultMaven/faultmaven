"""Case Cleanup Job - Standalone entrypoint for orphaned case collection cleanup.

This job can be run independently via CLI without requiring a running web server.
It cleans up orphaned case collections from CaseVectorStore.

Usage:
    python -m faultmaven.jobs.run case_cleanup

An "orphaned" collection is one that doesn't have a corresponding active case.
This is a safety net for collections that weren't properly deleted when cases closed.

Tenant scope: **cross_tenant**. ChromaDB collections are not org-partitioned, so
"orphaned" is only decidable against the case-id set of ALL organizations; a
partial (single-org) view would classify every other tenant's collections as
orphaned and delete them. Under the multi-tenant provider the runner refuses to
run this job except on the audited maintenance path (--cross-tenant-maintenance
+ the dedicated BYPASSRLS maintenance role, probe-verified); see
faultmaven.jobs.run and docs/operations/evidence-job-scheduling.md (ADR-010 /
issue #629).
"""

import logging
from typing import Any, Dict, Optional

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.jobs.reference_set import assess_reference_set

logger = logging.getLogger(__name__)


async def run(
    settings: FaultMavenSettings,
    container: Any,
    allow_disjoint_reference_set: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run case cleanup job.

    This is the standalone job entrypoint. It can be invoked via CLI
    without requiring a running web server or scheduler.

    Args:
        settings: FaultMaven settings instance
        container: DI container with initialized services
        allow_disjoint_reference_set: The operator's explicit assertion that a
            case-id set sharing NOTHING with the existing collections is
            expected here. Off by default — see
            ``faultmaven.jobs.reference_set``.
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
        "collections": 0,
        "reference_overlap": 0,
    }

    try:
        # Get required services from container
        case_vector_store = getattr(container, "case_vector_store", None)
        case_repository = getattr(container, "case_repository", None)

        # "failed", not "skipped" (#1232). The runner maps completed/skipped to
        # exit 0, so a job that can NEVER reach its dependencies would exit
        # cleanly every night and look like a CronJob with nothing to do —
        # the same invisibility that let the storage sweep's hazard stand for
        # months. Neither of these is a configured refusal; both are the job
        # being unable to do its work.
        if not case_vector_store:
            logger.error(
                "Case cleanup REFUSED: CaseVectorStore unavailable, so the "
                "candidate set cannot be read. Nothing was deleted."
            )
            result["status"] = "failed"
            result["reason"] = "case_vector_store_unavailable"
            return result

        if not case_repository:
            logger.error(
                "Case cleanup REFUSED: case repository unavailable, so the "
                "authority cannot be asked which collections are still "
                "referenced. Nothing was deleted."
            )
            result["status"] = "failed"
            result["reason"] = "case_repository_unavailable"
            return result

        # The reference set: EVERY case row's id (any state). A collection is
        # orphaned only when no case row exists for it at all, so terminal/
        # archived cases keep their collections. Under multi the jobs runner
        # only lets this job run on the maintenance path (BYPASSRLS role), so
        # the set is complete — never a partial single-org view.
        active_case_ids = await case_repository.list_all_case_ids()
        result["active_cases"] = len(active_case_ids)
        logger.debug(f"Found {len(active_case_ids)} case rows in the database")

        # Same fail-closed guard the storage sweep uses, from the same module
        # (#1232). Without it, `list_all_case_ids()` returning [] makes
        # `expected_collections` empty inside cleanup_orphaned_collections and
        # EVERY case collection is classified orphaned — and the per-candidate
        # `case_exists` re-check reads the same scoped repository, so it
        # answers "no" too and rescues nothing. An RLS-scoped session on
        # `cases` produces exactly that.
        collection_case_ids = await case_vector_store.list_case_collection_ids()
        result["collections"] = len(collection_case_ids)
        verdict = assess_reference_set(
            candidates=collection_case_ids,
            referenced=active_case_ids,
            # This job has no dry-run mode, so every run is a live one.
            dry_run=False,
            acknowledged=allow_disjoint_reference_set,
            authority="the cases table",
            candidate_noun="case collection",
        )
        result["reference_overlap"] = verdict.overlap_count
        if verdict.disjoint:
            logger.error("Case cleanup: %s", verdict.message)
        if verdict.refuse:
            result["status"] = "failed"
            result["reason"] = verdict.reason
            return result

        # Clean up orphaned collections. The per-candidate re-check closes the
        # snapshot race (a case created after list_all_case_ids() must not
        # lose its collection).
        async def _case_exists(case_id: str) -> bool:
            return await case_repository.get(case_id) is not None

        deleted_count = await case_vector_store.cleanup_orphaned_collections(
            active_case_ids, case_exists=_case_exists
        )
        result["deleted_count"] = deleted_count

        if deleted_count > 0:
            logger.info(
                f"Case cleanup completed: {deleted_count} orphaned collections deleted"
            )
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
# Needs the case-id set of ALL orgs (collections are not org-partitioned);
# the runner refuses to run this under the multi-tenant provider.
JOB_TENANT_SCOPE = "cross_tenant"
