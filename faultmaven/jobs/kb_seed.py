"""KB Seed Job - Standalone entrypoint for platform KB pack seeding.

Runs the same idempotent KB pack bootstrap as the single-tenant web startup
(``faultmaven/bootstrap/kb_init.py``): ensure every pack runbook has
its ``knowledge_items`` row + ChromaDB chunks, prune orphaned built-ins, and
reconcile SQL<->vector drift.

Usage:
    python -m faultmaven.jobs.run kb_seed                            # single-tenant
    python -m faultmaven.jobs.run kb_seed --cross-tenant-maintenance # multi-tenant

The same pass classifies AUTHORED vectors whose ``knowledge_items`` row is gone
(#1982), in every enterprise, when this role's row read is proven unfiltered by
row-level security — the maintenance role's is. It is REPORT-ONLY unless an
operator arms it (``KB_ORPHAN_SWEEP_ENABLED=true`` and a run that is not a dry
run; ``--dry-run`` / ``--no-dry-run`` override ``KB_ORPHAN_SWEEP_DRY_RUN`` for
this run, and govern only the sweep — seeding always runs). An enterprise whose
authored vectors match none of its rows is skipped unless this run names it:
``--allow-disjoint-enterprise <enterprise_id>`` (repeatable; ``unattributed``
names the bucket of chunks no listed enterprise's stamp names). The counts are
the result's ``authored_orphans`` and ride on the ``BootstrapResult(...)`` line.
See ``faultmaven/bootstrap/kb_orphan_sweep.py``.

It also finds rows whose authored ingest never completed, past
``KB_INCOMPLETE_INGEST_MAX_AGE_HOURS`` (#2394), and removes them with their
vectors only once armed the same way (``KB_INCOMPLETE_INGEST_SWEEP_ENABLED``
and a run that is not a dry run; the same ``--dry-run`` / ``--no-dry-run``
overrides ``KB_INCOMPLETE_INGEST_SWEEP_DRY_RUN``). The counts are the result's
``incomplete_ingests``. See ``faultmaven/bootstrap/kb_incomplete_sweep.py``.

The generation sweep (#2388) removes chunks of a generation no row serves,
past the same age, once armed with its own gate (``KB_GENERATION_SWEEP_ENABLED``
and a run that is not a dry run; the same ``--dry-run`` / ``--no-dry-run``
overrides ``KB_GENERATION_SWEEP_DRY_RUN``). Arm it only with one ChromaDB per
database. Its counts are the result's ``generation_sweep``. See
``faultmaven/bootstrap/kb_generation_sweep.py``.

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
from typing import Any, Dict, Optional, Sequence

from faultmaven.config.settings import FaultMavenSettings

logger = logging.getLogger(__name__)


async def run(
    settings: FaultMavenSettings,
    container: Any,
    dry_run: Optional[bool] = None,
    allow_disjoint_enterprises: Sequence[str] = (),
    **kwargs: Any,
) -> Dict[str, Any]:
    """Seed the platform KB pack into knowledge_items + ChromaDB.

    Args:
        settings: FaultMaven settings instance
        container: DI container with initialized services
        dry_run: The dry-run override for this run of all three sweeps
            (``--dry-run`` / ``--no-dry-run``); None defers to each sweep's
            own ``KB_ORPHAN_SWEEP_DRY_RUN``, ``KB_INCOMPLETE_INGEST_SWEEP_DRY_RUN``
            and ``KB_GENERATION_SWEEP_DRY_RUN``. ``--no-dry-run`` asks for
            deletion but cannot grant it: a sweep whose ``*_ENABLED`` is false
            is off. Seeding is unaffected either way.
        allow_disjoint_enterprises: The buckets (enterprise ids, or
            ``unattributed``) the operator asserts genuinely hold only orphans
            this run, one at a time (``--allow-disjoint-enterprise``). Empty
            by default — see ``faultmaven.jobs.reference_set``.
        **kwargs: Additional job parameters (unused)

    Returns:
        Job result dictionary with:
        - status: "completed", "skipped", or "failed"
        - ingested / skipped_unchanged / pruned / orphaned_vectors_cleaned /
          unpublished_vectors_cleaned / repaired_rows / unserved_rows: counts
          from the bootstrap pass (unserved_rows is the residual AFTER the
          pack re-ingest and the cross-store repair: the complete rows this
          deployment serves nothing for)
        - authored_orphans: the authored-orphan sweep (#1982) — its mode
          (off / report_only / armed), whether the row read was proven
          unfiltered, the cap, totals and per-enterprise counts of documents
          would_delete / deleted / unproven / disjoint / capped / misstamped /
          unremoved, the documents an armed run deletes, and the count of
          chunks no listed enterprise's stamp attributes
        - incomplete_ingests: the incomplete-ingest sweep (#2394) — its mode,
          ``skipped`` reason, age bound, and counts would_delete / deleted /
          spared / unremoved
        - generation_sweep: the superseded-chunk sweep (#2388) — its mode,
          ``skipped`` reason, and chunk counts would_delete / deleted /
          unremoved, with the documents they belong to
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
        # A partially composed container returns None here (#899 removed the
        # in-memory stub that used to stand in, which made this check inert and
        # left the job to die mid-pack on an opaque AttributeError). The
        # container now yields either a real, DB-capable KnowledgeService or
        # nothing, so `is None` is the complete test.
        if knowledge_service is None:
            logger.warning(
                "KnowledgeService unavailable (container composed without one), "
                "skipping KB seed"
            )
            result["status"] = "skipped"
            result["reason"] = "knowledge_service_unavailable"
            return result

        from faultmaven.bootstrap.kb_generation_sweep import (
            resolve_generation_sweep_mode,
        )
        from faultmaven.bootstrap.kb_incomplete_sweep import (
            resolve_incomplete_sweep_mode,
        )
        from faultmaven.bootstrap.kb_init import bootstrap_kb
        from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
        from faultmaven.infrastructure.persistence.database import get_db_session

        # organization_id applies only to org-owned (personal/team) pack rows —
        # the shipped pack is all-global, and global rows are stored org-free
        # (ingest_runbook derives organization_id=None for global scope, #770).
        bootstrap_result = await bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=get_db_session,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            sweep_dry_run=dry_run,
            allow_disjoint_enterprises=tuple(allow_disjoint_enterprises),
            incomplete_sweep_mode=resolve_incomplete_sweep_mode(dry_run),
            generation_sweep_mode=resolve_generation_sweep_mode(dry_run),
        )

        result["ingested"] = len(bootstrap_result.ingested)
        result["skipped_unchanged"] = len(bootstrap_result.skipped_unchanged)
        result["pruned"] = len(bootstrap_result.pruned)
        result["orphaned_vectors_cleaned"] = len(
            bootstrap_result.orphaned_vectors_cleaned
        )
        result["unpublished_vectors_cleaned"] = len(
            bootstrap_result.unpublished_vectors_cleaned
        )
        result["authored_orphans"] = bootstrap_result.authored_orphans.summary()
        result["incomplete_ingests"] = bootstrap_result.incomplete_ingests.summary()
        result["generation_sweep"] = bootstrap_result.generation_sweep.summary()
        result["repaired_rows"] = len(bootstrap_result.repaired_rows)
        result["unserved_rows"] = len(bootstrap_result.unserved_rows)
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
