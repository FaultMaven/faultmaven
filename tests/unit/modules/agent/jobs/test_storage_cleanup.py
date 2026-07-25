"""Unit tests for the sidecar-based storage cleanup job.

Covers per evidence-failure-modes.md:

1. Sidecar writing on `FileStorageService.store_file()`.
2. `mark_linked()` flips the sidecar to linked=True.
3. `cleanup_orphaned_files()` deletes only unlinked + past-TTL files,
   preserves linked or young files, skips files without sidecars.
4. `dry_run=True` logs without deleting.
5. `run()` respects the `orphan_cleanup_enabled` settings gate.

The sweep enumerates sidecars through the storage backend rather than walking
a directory, so these tests drive it through a filesystem-backed service and
assert on-disk outcomes.

Run with:
    pytest tests/unit/modules/agent/jobs/test_storage_cleanup.py -v
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend
from faultmaven.modules.agent.jobs import storage_cleanup
from faultmaven.modules.agent.jobs.storage_cleanup import cleanup_orphaned_files, run
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    SIDECAR_SUFFIX,
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
    return FileStorageService(
        backend=FilesystemStorageBackend(storage_root=storage_root)
    )


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
        storage_key = result["storage_key"]
        full_path = Path(storage_root) / storage_key
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
        storage_key = result["storage_key"]

        ok = await storage_service.mark_linked(storage_key)
        assert ok is True

        sidecar = await storage_service.read_sidecar(storage_key)
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
        assert await storage_service.mark_linked(result["storage_key"]) is True
        # Second call should succeed and remain linked
        assert await storage_service.mark_linked(result["storage_key"]) is True

        sidecar = await storage_service.read_sidecar(result["storage_key"])
        assert sidecar["linked"] is True

    @pytest.mark.asyncio
    async def test_mark_linked_missing_sidecar_returns_false(self, storage_service):
        # Key for a file that was never stored — no sidecar exists
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
        storage_key = result["storage_key"]
        full_path = Path(storage_root) / storage_key
        sidecar_path = Path(f"{full_path}{SIDECAR_SUFFIX}")
        assert sidecar_path.exists()

        await storage_service.delete_file(storage_key)

        assert not full_path.exists()
        assert not sidecar_path.exists()

    @pytest.mark.asyncio
    async def test_list_sidecar_keys_returns_file_keys(self, storage_service):
        """Sidecar listing yields the FILE key, not the sidecar key.

        The cleanup job feeds these straight back into read_sidecar /
        delete_file, so the suffix must already be stripped.
        """
        result = await storage_service.store_file(
            file_data=b"x",
            original_filename="listed.txt",
            organization_id="o",
            case_id="c",
            mime_type="text/plain",
        )

        keys = await storage_service.list_sidecar_keys()

        assert keys == [result["storage_key"]]
        assert not keys[0].endswith(SIDECAR_SUFFIX)


# ============================================================
# Cleanup sweep behavior
# ============================================================


class TestCleanupOrphanedFiles:
    @pytest.mark.asyncio
    async def test_deletes_only_unlinked_and_past_ttl(
        self, storage_service, storage_root
    ):
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
            storage=storage_service, ttl_hours=24, dry_run=False
        )

        assert result["scanned"] == 3
        assert result["found"] == 1
        assert result["deleted"] == 1
        assert result["skipped_linked"] == 1
        assert result["skipped_within_ttl"] == 1
        assert not target.exists()
        assert not target_sidecar.exists()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_delete(self, storage_service, storage_root):
        old = datetime.now(UTC) - timedelta(hours=48)
        target = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/doomed.log",
            linked=False,
            uploaded_at=old,
        )
        sidecar = Path(f"{target}{SIDECAR_SUFFIX}")

        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=True
        )

        assert result["found"] == 1
        assert result["deleted"] == 0
        assert target.exists()  # not deleted
        assert sidecar.exists()

    @pytest.mark.asyncio
    async def test_skips_file_without_sidecar(self, storage_service, storage_root):
        # Write a file with NO sidecar — cleanup must not touch it
        orphan_no_sidecar = Path(storage_root) / "lonely.log"
        orphan_no_sidecar.write_bytes(b"no sidecar here")

        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=False
        )

        assert result["scanned"] == 0  # no sidecars to scan
        assert orphan_no_sidecar.exists()

    @pytest.mark.asyncio
    async def test_corrupt_sidecar_counted_as_error(
        self, storage_service, storage_root
    ):
        full_path = Path(storage_root) / "org/case/1/broken.log"
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(b"data")
        Path(f"{full_path}{SIDECAR_SUFFIX}").write_text("not valid json {")

        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=False
        )

        assert result["errors"] == 1
        assert full_path.exists()  # err on the side of NOT deleting

    @pytest.mark.asyncio
    async def test_missing_root_returns_empty_result(self, tmp_path):
        """A storage root that was never created is 'nothing to clean'.

        The backend must report an empty listing rather than raising — a
        cleanup run on a fresh deployment is normal, not an error.
        """
        nonexistent = str(tmp_path / "does_not_exist")
        service = FileStorageService(
            backend=FilesystemStorageBackend(storage_root=nonexistent)
        )
        assert not Path(nonexistent).exists(), (
            "constructing the backend must not touch the filesystem — it "
            "happens lazily on request paths where the root may be a network "
            "mount"
        )

        result = await cleanup_orphaned_files(
            storage=service, ttl_hours=24, dry_run=False
        )
        assert result["status"] == "completed"
        assert result["scanned"] == 0


# ============================================================
# Settings gate on run()
# ============================================================


class TestRunSettingsGate:
    @pytest.fixture(autouse=True)
    def _bind_temp_storage(self, monkeypatch, storage_service):
        """Bind run()'s self-constructed service to the temp backend.

        run() resolves the configured backend itself (that is the point of
        #689 — no construction site carries its own storage root), so tests
        substitute the service rather than passing a root through settings.
        """
        monkeypatch.setattr(
            storage_cleanup, "FileStorageService", lambda: storage_service
        )

    def _settings(
        self,
        *,
        enabled: bool,
        dry_run: bool,
        ttl_hours: int = 24,
    ):
        ev = SimpleNamespace(
            orphan_cleanup_enabled=enabled,
            orphan_cleanup_dry_run=dry_run,
            orphan_file_ttl_hours=ttl_hours,
        )
        return SimpleNamespace(evidence_storage=ev)

    @pytest.mark.asyncio
    async def test_skipped_when_disabled_and_not_dry_run(self):
        settings = self._settings(enabled=False, dry_run=False)
        result = await run(settings=settings)
        assert result["status"] == "skipped"
        assert result["reason"] == "orphan_cleanup_disabled"

    @pytest.mark.asyncio
    async def test_runs_when_enabled(self):
        settings = self._settings(enabled=True, dry_run=False)
        result = await run(settings=settings)
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_runs_when_dry_run_even_if_disabled(self):
        # dry_run=True is safe, so we run even with enabled=False
        settings = self._settings(enabled=False, dry_run=True)
        result = await run(settings=settings)
        assert result["status"] == "completed"
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_caller_dry_run_override_wins(self):
        # Settings say live mode, but caller explicitly asks for dry-run
        settings = self._settings(enabled=True, dry_run=False)
        result = await run(settings=settings, dry_run=True)
        assert result["status"] == "completed"
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_caller_ttl_override_wins(self):
        settings = self._settings(enabled=True, dry_run=True, ttl_hours=24)
        result = await run(settings=settings, ttl_hours=72)
        assert result["ttl_hours"] == 72


class TestCleanupErrorAccounting:
    """A sweep must never report storage it did not actually reclaim."""

    @pytest.mark.asyncio
    async def test_failed_delete_counts_as_error_not_deleted(
        self, storage_service, storage_root
    ):
        """An S3 AccessDenied (etc.) must not be logged as a deletion.

        delete_file used to swallow backend failures and return False, so the
        job counted the file as deleted and incremented the deletion metric
        for a file still sitting in the bucket.
        """
        old = datetime.now(UTC) - timedelta(hours=48)
        target = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/undeletable.log",
            linked=False,
            uploaded_at=old,
        )
        storage_service.backend.delete_file = AsyncMock(
            side_effect=PermissionError("AccessDenied")
        )

        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=False
        )

        assert result["found"] == 1
        assert result["deleted"] == 0
        assert result["errors"] == 1
        assert target.exists()

    @pytest.mark.asyncio
    async def test_unreadable_sidecar_never_deletes_the_file(
        self, storage_service, storage_root
    ):
        """A backend fault must not present a live file as an unlinked orphan."""
        old = datetime.now(UTC) - timedelta(hours=48)
        target = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/live.log",
            linked=True,
            uploaded_at=old,
        )
        storage_service.backend.retrieve_file = AsyncMock(
            side_effect=ConnectionError("transient S3 fault")
        )

        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=False
        )

        assert result["errors"] == 1
        assert result["deleted"] == 0
        assert target.exists()
