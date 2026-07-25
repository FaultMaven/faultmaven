"""Storage Cleanup Job — TTL-Based Orphan File Removal

Implements per evidence-failure-modes.md. Deletes
files whose sidecar metadata shows `linked=False` AND whose `uploaded_at`
is older than `orphan_file_ttl_hours`. Files without sidecars are skipped
(unknown state is not a license to delete).

## Sidecar pairing

Every file stored via `FileStorageService.store_file()` gets a companion
`{filename}.meta.json` sidecar with:

    {
        "case_id": "case_abc",
        "organization_id": "org_xyz",
        "uploaded_at": "2026-04-18T10:00:00+00:00",
        "linked": false,
        "schema_version": 1
    }

`FileStorageService.mark_linked()` flips `linked=true` once an Evidence row
is created referencing the file (called from
`InvestigationService._preprocess_attachment`).

## Safety protocol

This job ships with `orphan_cleanup_enabled=False` and `dry_run=True` by
default. Per the M1 canary protocol: run dry-run for ≥48 hours, eyeball
logs, fix any unexpected entries in the `mark_linked` path, then flip
`dry_run=False`.

## Usage

    python -m faultmaven.jobs.run storage_cleanup
    python -m faultmaven.jobs.run storage_cleanup --dry-run
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_ORPHAN_FILES_DELETED_TOTAL,
    EVIDENCE_ORPHAN_FILES_FOUND_TOTAL,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)

logger = logging.getLogger(__name__)

JOB_DESCRIPTION = (
    "Delete stored files whose sidecar metadata shows linked=False and "
    "uploaded_at older than the TTL (PLAN-evidence-failure-modes M1)."
)
# Backend sweep driven by sidecar metadata written at upload time — no tenanted
# DB reads, so it runs identically in both tenancy modes.
JOB_TENANT_SCOPE = "tenant_neutral"


async def cleanup_orphaned_files(
    storage: FileStorageService,
    ttl_hours: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Sweep stored files and delete orphans.

    Enumerates sidecars through the storage backend rather than walking a
    local directory, so the sweep works whichever backend STORAGE_BACKEND
    selects.

    Returns a stats dict with counts for observability / CLI output.
    Emits two Prometheus counters per run:
      - ``faultmaven_evidence_orphan_files_found_total`` (+= found count)
      - ``faultmaven_evidence_orphan_files_deleted_total`` (+= deleted count;
        not incremented when dry_run is True)

    Args:
        storage: FileStorageService owning the sidecar protocol.
        ttl_hours: Delete only files whose sidecar `uploaded_at` is older
            than this many hours. Younger files are always safe.
        dry_run: When True, log `would delete` without deleting.

    Returns:
        Dict with keys: ``status``, ``storage_backend``, ``ttl_hours``,
        ``dry_run``, ``scanned``, ``skipped_no_sidecar``,
        ``skipped_linked``, ``skipped_within_ttl``, ``found``, ``deleted``,
        ``errors``.
    """
    result: dict[str, Any] = {
        "status": "completed",
        "storage_backend": storage.backend.get_storage_type().value,
        "ttl_hours": ttl_hours,
        "dry_run": dry_run,
        "scanned": 0,
        "skipped_no_sidecar": 0,
        "skipped_linked": 0,
        "skipped_within_ttl": 0,
        "found": 0,
        "deleted": 0,
        "errors": 0,
    }

    cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)

    for storage_key in await storage.list_sidecar_keys():
        result["scanned"] += 1

        try:
            payload = await storage.read_sidecar(storage_key)
        except Exception as e:
            # Corrupt payload, or the backend failed to answer. Either way we
            # do not know this file's state, and unknown state is never a
            # licence to delete.
            logger.warning("Unreadable sidecar for %s — skipping: %s", storage_key, e)
            result["errors"] += 1
            continue

        if payload is None:
            # Listed a moment ago, gone now — deleted concurrently.
            result["skipped_no_sidecar"] += 1
            continue

        if payload.get("linked") is True:
            result["skipped_linked"] += 1
            continue

        uploaded_at_str = payload.get("uploaded_at")
        if not uploaded_at_str:
            logger.warning("Sidecar for %s missing uploaded_at — skipping", storage_key)
            result["errors"] += 1
            continue

        try:
            uploaded_at = datetime.fromisoformat(uploaded_at_str)
        except ValueError:
            logger.warning(
                "Sidecar for %s has unparseable uploaded_at=%r — skipping",
                storage_key,
                uploaded_at_str,
            )
            result["errors"] += 1
            continue

        if uploaded_at > cutoff:
            result["skipped_within_ttl"] += 1
            continue

        # Candidate for deletion: unlinked AND past TTL.
        result["found"] += 1
        try:
            EVIDENCE_ORPHAN_FILES_FOUND_TOTAL.inc()
        except Exception:
            pass

        if dry_run:
            logger.info(
                "[DRY RUN] would delete orphan: %s (uploaded=%s, linked=False)",
                storage_key,
                uploaded_at_str,
            )
            continue

        # Actually delete. delete_file removes the sidecar too, and raises if
        # the backend refuses — so a failed delete is counted as an error
        # rather than reported as reclaimed storage. A False return means the
        # file was already gone, which still counts: the sidecar that kept it
        # findable is now removed.
        try:
            await storage.delete_file(storage_key)
            result["deleted"] += 1
            try:
                EVIDENCE_ORPHAN_FILES_DELETED_TOTAL.inc()
            except Exception:
                pass
            logger.info(
                "Deleted orphan: %s (uploaded=%s)",
                storage_key,
                uploaded_at_str,
            )
        except Exception as e:
            logger.error("Failed to delete orphan %s: %s", storage_key, e)
            result["errors"] += 1

    logger.info(
        "Storage cleanup %s — scanned=%d, found=%d, deleted=%d, "
        "skipped_linked=%d, skipped_within_ttl=%d, skipped_no_sidecar=%d, errors=%d",
        "DRY RUN" if dry_run else "live",
        result["scanned"],
        result["found"],
        result["deleted"],
        result["skipped_linked"],
        result["skipped_within_ttl"],
        result["skipped_no_sidecar"],
        result["errors"],
    )
    return result


async def run(
    settings: Any = None,
    container: Any = None,
    ttl_hours: int | None = None,
    dry_run: bool | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """CLI entry point invoked by ``faultmaven.jobs.run``.

    Args:
        settings: ``FaultMavenSettings`` injected by the runner.
        container: DI container injected by the runner.
        ttl_hours: Override for ``evidence_storage.orphan_file_ttl_hours``.
        dry_run: Override for ``evidence_storage.orphan_cleanup_dry_run``.
            Caller-supplied overrides win over settings — useful for manual
            testing / one-shot invocations.

    The gate is:
      1. ``evidence_storage.orphan_cleanup_enabled`` must be True, OR
      2. caller must pass ``dry_run=True`` explicitly (safe to run anyway).

    Otherwise the job exits with status="skipped" and no side effects.
    """
    if settings is None:
        from faultmaven.config.settings import get_settings

        settings = get_settings()

    ev_settings = settings.evidence_storage
    effective_dry_run = (
        ev_settings.orphan_cleanup_dry_run if dry_run is None else dry_run
    )
    effective_ttl_hours = (
        ev_settings.orphan_file_ttl_hours if ttl_hours is None else ttl_hours
    )

    if not ev_settings.orphan_cleanup_enabled and not effective_dry_run:
        logger.info(
            "Storage cleanup skipped: orphan_cleanup_enabled=False and "
            "dry_run=False. Set ORPHAN_CLEANUP_ENABLED=true or run with "
            "--dry-run."
        )
        return {
            "status": "skipped",
            "reason": "orphan_cleanup_disabled",
        }

    storage = FileStorageService()
    logger.info(
        "Storage cleanup starting (backend=%s, ttl_hours=%d, dry_run=%s, "
        "enabled=%s)",
        storage.backend.get_storage_type().value,
        effective_ttl_hours,
        effective_dry_run,
        ev_settings.orphan_cleanup_enabled,
    )

    return await cleanup_orphaned_files(
        storage=storage,
        ttl_hours=effective_ttl_hours,
        dry_run=effective_dry_run,
    )
