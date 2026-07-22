"""KB Seed Job - Standalone entrypoint for platform KB pack seeding.

Runs the same atomic, idempotent KB pack bootstrap as the single-tenant web
startup (``faultmaven/bootstrap/kb_init.py``): ensure every pack runbook has
its ``knowledge_items`` row + ChromaDB chunks, prune orphaned built-ins, and
reconcile SQL<->vector drift.

Usage:
    python -m faultmaven.jobs.run kb_seed                            # single-tenant
    python -m faultmaven.jobs.run kb_seed --cross-tenant-maintenance # multi-tenant

Tenant scope: **cross_tenant**. Global-scope pack rows are the org-free
platform tier (#770): they are readable by EVERY tenant (RLS read exemption +
org-free vector path), and the RLS write policies bar the tenant-bound app
role from writing them under the multi-tenant provider. Seeding under multi
therefore runs exclusively on the audited maintenance path
(--cross-tenant-maintenance + the dedicated BYPASSRLS maintenance role,
probe-verified, INSERT/DELETE grants on knowledge_items only); see
faultmaven.jobs.run and docs/operations/evidence-job-scheduling.md. In
single-tenant deployments this job is an operator alternative to the web
startup bootstrap (same code path, same idempotency).
"""

import logging
from typing import Any, Dict

from faultmaven.config.settings import FaultMavenSettings

logger = logging.getLogger(__name__)


async def run(
    settings: FaultMavenSettings,
    container: Any,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Seed the platform KB pack into knowledge_items + ChromaDB.

    Args:
        settings: FaultMaven settings instance
        container: DI container with initialized services
        **kwargs: Additional job parameters (unused)

    Returns:
        Job result dictionary with:
        - status: "completed", "skipped", or "failed"
        - ingested / skipped_unchanged / pruned / orphaned_vectors_cleaned /
          repaired_rows / orphaned_rows: counts from the bootstrap pass
          (orphaned_rows is the residual AFTER the cross-store repair)
        - failures: list of (runbook relpath, reason) for failed runbooks
        - error: Error message if the whole pass failed
    """
    logger.info("Starting KB seed job")

    result: Dict[str, Any] = {
        "job": "kb_seed",
        "status": "completed",
    }

    try:
        knowledge_service = container.get_knowledge_service()
        if knowledge_service is None:
            logger.warning("KnowledgeService not available, skipping KB seed")
            result["status"] = "skipped"
            result["reason"] = "knowledge_service_unavailable"
            return result

        from faultmaven.bootstrap.kb_init import bootstrap_kb
        from faultmaven.infrastructure.persistence.database import get_db_session
        from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

        # organization_id applies only to org-owned (personal/team) pack rows —
        # the shipped pack is all-global, and global rows are stored org-free
        # (ingest_runbook derives organization_id=None for global scope, #770).
        bootstrap_result = await bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=get_db_session,
            organization_id=SingleTenantProvider.DEFAULT_ORG_ID,
        )

        result["ingested"] = len(bootstrap_result.ingested)
        result["skipped_unchanged"] = len(bootstrap_result.skipped_unchanged)
        result["pruned"] = len(bootstrap_result.pruned)
        result["orphaned_vectors_cleaned"] = len(
            bootstrap_result.orphaned_vectors_cleaned
        )
        result["repaired_rows"] = len(bootstrap_result.repaired_rows)
        result["orphaned_rows"] = len(bootstrap_result.orphaned_rows)
        result["failures"] = list(bootstrap_result.failed)

        if bootstrap_result.failed:
            # Partial failure must be visible to the operator (non-zero exit):
            # a runbook that failed to seed is simply absent for every tenant.
            result["status"] = "failed"
            result["error"] = (
                f"{len(bootstrap_result.failed)} runbook(s) failed to seed"
            )
            for relpath, reason in bootstrap_result.failed:
                logger.error(f"KB seed failed for {relpath}: {reason}")
        else:
            logger.info(f"KB seed completed: {bootstrap_result!r}")

    except Exception as e:
        logger.error(f"KB seed job failed: {e}", exc_info=True)
        result["status"] = "failed"
        result["error"] = str(e)

    return result


# Job metadata for CLI discovery
JOB_NAME = "kb_seed"
JOB_DESCRIPTION = "Seed the platform KB pack (knowledge_items + vector chunks)"
# Writes the org-free global platform tier served to ALL tenants; under the
# multi-tenant provider the runner only allows this on the audited
# maintenance path (--cross-tenant-maintenance + BYPASSRLS role).
JOB_TENANT_SCOPE = "cross_tenant"
