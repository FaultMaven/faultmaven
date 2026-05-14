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

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_ORPHAN_FILES_DELETED_TOTAL,
    EVIDENCE_ORPHAN_FILES_FOUND_TOTAL,
)

logger = logging.getLogger(__name__)

JOB_DESCRIPTION = (
    "Delete stored files whose sidecar metadata shows linked=False and "
    "uploaded_at older than the TTL (PLAN-evidence-failure-modes M1)."
)

SIDECAR_SUFFIX = ".meta.json"


async def cleanup_orphaned_files(
    storage_root: str,
    ttl_hours: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Sweep the storage root and delete orphaned files.

    Returns a stats dict with counts for observability / CLI output.
    Emits two Prometheus counters per run:
      - ``faultmaven_evidence_orphan_files_found_total`` (+= found count)
      - ``faultmaven_evidence_orphan_files_deleted_total`` (+= deleted count;
        not incremented when dry_run is True)

    Args:
        storage_root: Absolute path to the FileStorageService storage root.
        ttl_hours: Delete only files whose sidecar `uploaded_at` is older
            than this many hours. Younger files are always safe.
        dry_run: When True, log `would delete` without deleting.

    Returns:
        Dict with keys: ``status``, ``storage_root``, ``ttl_hours``,
        ``dry_run``, ``scanned``, ``skipped_no_sidecar``,
        ``skipped_linked``, ``skipped_within_ttl``, ``found``, ``deleted``,
        ``errors``.
    """
    root = Path(storage_root)
    result: dict[str, Any] = {
        "status": "completed",
        "storage_root": str(root),
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

    if not root.exists():
        logger.info("Storage root %s does not exist — nothing to clean", root)
        return result

    cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)

    for sidecar_path in root.rglob(f"*{SIDECAR_SUFFIX}"):
        result["scanned"] += 1
        file_path = Path(str(sidecar_path)[: -len(SIDECAR_SUFFIX)])

        try:
            payload = json.loads(sidecar_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Corrupt sidecar %s — skipping: %s", sidecar_path, e)
            result["errors"] += 1
            continue

        if payload.get("linked") is True:
            result["skipped_linked"] += 1
            continue

        uploaded_at_str = payload.get("uploaded_at")
        if not uploaded_at_str:
            logger.warning("Sidecar %s missing uploaded_at — skipping", sidecar_path)
            result["errors"] += 1
            continue

        try:
            uploaded_at = datetime.fromisoformat(uploaded_at_str)
        except ValueError:
            logger.warning(
                "Sidecar %s has unparseable uploaded_at=%r — skipping",
                sidecar_path,
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
                file_path,
                uploaded_at_str,
            )
            continue

        # Actually delete.
        try:
            if file_path.exists():
                file_path.unlink()
            sidecar_path.unlink()
            result["deleted"] += 1
            try:
                EVIDENCE_ORPHAN_FILES_DELETED_TOTAL.inc()
            except Exception:
                pass
            logger.info(
                "Deleted orphan: %s (uploaded=%s)",
                file_path,
                uploaded_at_str,
            )
        except OSError as e:
            logger.error("Failed to delete orphan %s: %s", file_path, e)
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

    storage_root = os.path.abspath(ev_settings.evidence_storage_root)
    logger.info(
        "Storage cleanup starting (root=%s, ttl_hours=%d, dry_run=%s, " "enabled=%s)",
        storage_root,
        effective_ttl_hours,
        effective_dry_run,
        ev_settings.orphan_cleanup_enabled,
    )

    return await cleanup_orphaned_files(
        storage_root=storage_root,
        ttl_hours=effective_ttl_hours,
        dry_run=effective_dry_run,
    )
