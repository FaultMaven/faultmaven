"""Unit tests for the sidecar-based storage cleanup job.

Covers per PLAN-evidence-failure-modes-implementation.md §M1:

1. Sidecar writing on `FileStorageService.store_file()`.
2. `mark_linked()` flips the sidecar to linked=True.
3. `cleanup_orphaned_files()` deletes only unlinked + past-TTL files,
   preserves linked or young files, skips files without sidecars.
4. `dry_run=True` logs without deleting.
5. `run()` respects the `orphan_cleanup_enabled` settings gate.

Run with:
    pytest tests/unit/modules/agent/jobs/test_storage_cleanup.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultmaven.modules.agent.jobs.storage_cleanup import (
    SIDECAR_SUFFIX,
    cleanup_orphaned_files,
    run,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def storage_root():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def storage_service(storage_root):
    return FileStorageService(storage_root=storage_root)


def _write_file_with_sidecar(
    storage_root: str,
    *,
    relative_path: str,
    linked: bool,
    uploaded_at: datetime,
) -> Path:
    """Create a file + sidecar at a controlled uploaded_at timestamp.

    Used by tests that need to simulate files older/newer than the TTL
    without waiting real wall-clock time.
    """
    full_path = Path(storage_root) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(b"file content")

    sidecar_path = Path(f"{full_path}{SIDECAR_SUFFIX}")
    sidecar_path.write_text(
        json.dumps(
            {
                "case_id": "case_test",
                "organization_id": "org_test",
                "uploaded_at": uploaded_at.isoformat(),
                "linked": linked,
                "schema_version": 1,
            }
        )
    )
    return full_path


# ============================================================
# Sidecar writing + mark_linked round-trip
# ============================================================


class TestFileStorageServiceSidecar:
    @pytest.mark.asyncio
    async def test_store_file_writes_sidecar(self, storage_service, storage_root):
        result = await storage_service.store_file(
            file_data=b"hello world",
            original_filename="app.log",
            organization_id="org_alpha",
            case_id="case_alpha",
            mime_type="text/plain",
        )
        file_path = result["file_path"]
        full_path = Path(storage_root) / file_path
        sidecar_path = Path(f"{full_path}{SIDECAR_SUFFIX}")

        assert full_path.exists()
        assert sidecar_path.exists()

        payload = json.loads(sidecar_path.read_text())
        assert payload["case_id"] == "case_alpha"
        assert payload["organization_id"] == "org_alpha"
        assert payload["linked"] is False
        assert payload["schema_version"] == 1
        assert "uploaded_at" in payload

    @pytest.mark.asyncio
    async def test_mark_linked_flips_flag(self, storage_service, storage_root):
        result = await storage_service.store_file(
            file_data=b"hello",
            original_filename="log.txt",
            organization_id="org",
            case_id="case",
            mime_type="text/plain",
        )
        file_path = result["file_path"]

        ok = await storage_service.mark_linked(file_path)
        assert ok is True

        sidecar = await storage_service.read_sidecar(file_path)
        assert sidecar is not None
        assert sidecar["linked"] is True

    @pytest.mark.asyncio
    async def test_mark_linked_idempotent(self, storage_service):
        result = await storage_service.store_file(
            file_data=b"data",
            original_filename="a.txt",
            organization_id="o",
            case_id="c",
            mime_type="text/plain",
        )
        assert await storage_service.mark_linked(result["file_path"]) is True
        # Second call should succeed and remain linked
        assert await storage_service.mark_linked(result["file_path"]) is True

        sidecar = await storage_service.read_sidecar(result["file_path"])
        assert sidecar["linked"] is True

    @pytest.mark.asyncio
    async def test_mark_linked_missing_sidecar_returns_false(self, storage_service):
        # File_path for a file that was never stored — no sidecar exists
        ok = await storage_service.mark_linked("org/case/date/nonexistent.txt")
        assert ok is False

    @pytest.mark.asyncio
    async def test_delete_file_removes_sidecar(self, storage_service, storage_root):
        result = await storage_service.store_file(
            file_data=b"bye",
            original_filename="gone.txt",
            organization_id="o",
            case_id="c",
            mime_type="text/plain",
        )
        file_path = result["file_path"]
        full_path = Path(storage_root) / file_path
        sidecar_path = Path(f"{full_path}{SIDECAR_SUFFIX}")
        assert sidecar_path.exists()

        await storage_service.delete_file(file_path)

        assert not full_path.exists()
        assert not sidecar_path.exists()


# ============================================================
# Cleanup sweep behavior
# ============================================================


class TestCleanupOrphanedFiles:
    @pytest.mark.asyncio
    async def test_deletes_only_unlinked_and_past_ttl(self, storage_root):
        now = datetime.now(UTC)
        old = now - timedelta(hours=48)
        recent = now - timedelta(hours=1)

        # 1) linked + old → keep
        _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/linked_old.log",
            linked=True,
            uploaded_at=old,
        )
        # 2) unlinked + recent → keep (within TTL grace)
        _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/2/unlinked_recent.log",
            linked=False,
            uploaded_at=recent,
        )
        # 3) unlinked + old → DELETE
        target = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/3/unlinked_old.log",
            linked=False,
            uploaded_at=old,
        )
        target_sidecar = Path(f"{target}{SIDECAR_SUFFIX}")

        result = await cleanup_orphaned_files(
            storage_root=storage_root, ttl_hours=24, dry_run=False
        )

        assert result["scanned"] == 3
        assert result["found"] == 1
        assert result["deleted"] == 1
        assert result["skipped_linked"] == 1
        assert result["skipped_within_ttl"] == 1
        assert not target.exists()
        assert not target_sidecar.exists()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_delete(self, storage_root):
        old = datetime.now(UTC) - timedelta(hours=48)
        target = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/doomed.log",
            linked=False,
            uploaded_at=old,
        )
        sidecar = Path(f"{target}{SIDECAR_SUFFIX}")

        result = await cleanup_orphaned_files(
            storage_root=storage_root, ttl_hours=24, dry_run=True
        )

        assert result["found"] == 1
        assert result["deleted"] == 0
        assert target.exists()  # not deleted
        assert sidecar.exists()

    @pytest.mark.asyncio
    async def test_skips_file_without_sidecar(self, storage_root):
        # Write a file with NO sidecar — cleanup must not touch it
        orphan_no_sidecar = Path(storage_root) / "lonely.log"
        orphan_no_sidecar.write_bytes(b"no sidecar here")

        result = await cleanup_orphaned_files(
            storage_root=storage_root, ttl_hours=24, dry_run=False
        )

        assert result["scanned"] == 0  # no sidecars to scan
        assert orphan_no_sidecar.exists()

    @pytest.mark.asyncio
    async def test_corrupt_sidecar_counted_as_error(self, storage_root):
        full_path = Path(storage_root) / "org/case/1/broken.log"
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(b"data")
        Path(f"{full_path}{SIDECAR_SUFFIX}").write_text("not valid json {")

        result = await cleanup_orphaned_files(
            storage_root=storage_root, ttl_hours=24, dry_run=False
        )

        assert result["errors"] == 1
        assert full_path.exists()  # err on the side of NOT deleting

    @pytest.mark.asyncio
    async def test_missing_root_returns_empty_result(self, tmp_path):
        nonexistent = str(tmp_path / "does_not_exist")
        result = await cleanup_orphaned_files(
            storage_root=nonexistent, ttl_hours=24, dry_run=False
        )
        assert result["status"] == "completed"
        assert result["scanned"] == 0


# ============================================================
# Settings gate on run()
# ============================================================


class TestRunSettingsGate:
    def _settings(
        self,
        *,
        enabled: bool,
        dry_run: bool,
        storage_root: str,
        ttl_hours: int = 24,
    ):
        ev = SimpleNamespace(
            orphan_cleanup_enabled=enabled,
            orphan_cleanup_dry_run=dry_run,
            orphan_file_ttl_hours=ttl_hours,
            evidence_storage_root=storage_root,
        )
        return SimpleNamespace(evidence_storage=ev)

    @pytest.mark.asyncio
    async def test_skipped_when_disabled_and_not_dry_run(self, storage_root):
        settings = self._settings(
            enabled=False, dry_run=False, storage_root=storage_root
        )
        result = await run(settings=settings)
        assert result["status"] == "skipped"
        assert result["reason"] == "orphan_cleanup_disabled"

    @pytest.mark.asyncio
    async def test_runs_when_enabled(self, storage_root):
        settings = self._settings(
            enabled=True, dry_run=False, storage_root=storage_root
        )
        result = await run(settings=settings)
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_runs_when_dry_run_even_if_disabled(self, storage_root):
        # dry_run=True is safe, so we run even with enabled=False
        settings = self._settings(
            enabled=False, dry_run=True, storage_root=storage_root
        )
        result = await run(settings=settings)
        assert result["status"] == "completed"
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_caller_dry_run_override_wins(self, storage_root):
        # Settings say live mode, but caller explicitly asks for dry-run
        settings = self._settings(
            enabled=True, dry_run=False, storage_root=storage_root
        )
        result = await run(settings=settings, dry_run=True)
        assert result["status"] == "completed"
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_caller_ttl_override_wins(self, storage_root):
        settings = self._settings(
            enabled=True,
            dry_run=True,
            storage_root=storage_root,
            ttl_hours=24,
        )
        result = await run(settings=settings, ttl_hours=72)
        assert result["ttl_hours"] == 72
