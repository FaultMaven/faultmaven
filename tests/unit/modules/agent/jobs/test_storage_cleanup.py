"""Unit tests for the sidecar + database-backed storage cleanup job.

Covers per evidence-failure-modes.md:

1. Sidecar writing on `FileStorageService.store_file()`.
2. `mark_linked()` flips the sidecar to linked=True.
3. `cleanup_orphaned_files()` deletes only unlinked + past-TTL files that the
   database does not reference, preserves linked or young files, skips files
   without sidecars.
4. `dry_run=True` logs without deleting.
5. `run()` respects the `orphan_cleanup_enabled` settings gate.
6. The database cross-check (#1232): a file whose sidecar says `linked: false`
   past the TTL is NOT deleted while an `uploaded_files` row still names it,
   and the sweep refuses to run at all when it cannot ask the authority.

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


# A reference set that matches nothing these tests store. Deliberately NOT
# empty: an empty reference set beside live candidates is the RLS/authority
# failure shape, and the sweep refuses the run outright on it (its own test
# below). Using set() here would make every unrelated test assert against a
# refusal instead of the behaviour it is about.
UNRELATED_REFS = {"org_other/case_other/2020/01/01/never-a-candidate.log"}


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
    async def test_survey_sidecars_returns_file_keys(self, storage_service):
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

        candidates, strays = await storage_service.survey_sidecars()

        assert candidates == [result["storage_key"]]
        assert not candidates[0].endswith(SIDECAR_SUFFIX)
        assert strays == []


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
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
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
            storage=storage_service,
            ttl_hours=24,
            dry_run=True,
            referenced_refs=UNRELATED_REFS,
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
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
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
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
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
            storage=service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
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

    @pytest.fixture
    def container(self):
        """A DI container whose case repository answers the reference query.

        Since #1232 the sweep refuses to run without one — these tests are
        about the settings gate, so they supply a working authority and let
        the authority-refusal tests below own that behaviour.
        """
        repo = SimpleNamespace(
            list_all_storage_refs=AsyncMock(return_value=set(UNRELATED_REFS))
        )
        return SimpleNamespace(case_repository=repo)

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
    async def test_skipped_when_disabled_and_not_dry_run(self, container):
        settings = self._settings(enabled=False, dry_run=False)
        result = await run(settings=settings, container=container)
        assert result["status"] == "skipped"
        assert result["reason"] == "orphan_cleanup_disabled"

    @pytest.mark.asyncio
    async def test_runs_when_enabled(self, container):
        settings = self._settings(enabled=True, dry_run=False)
        result = await run(settings=settings, container=container)
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_runs_when_dry_run_even_if_disabled(self, container):
        # dry_run=True is safe, so we run even with enabled=False
        settings = self._settings(enabled=False, dry_run=True)
        result = await run(settings=settings, container=container)
        assert result["status"] == "completed"
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_caller_dry_run_override_wins(self, container):
        # Settings say live mode, but caller explicitly asks for dry-run
        settings = self._settings(enabled=True, dry_run=False)
        result = await run(settings=settings, container=container, dry_run=True)
        assert result["status"] == "completed"
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_caller_ttl_override_wins(self, container):
        settings = self._settings(enabled=True, dry_run=True, ttl_hours=24)
        result = await run(settings=settings, container=container, ttl_hours=72)
        assert result["ttl_hours"] == 72

    @pytest.mark.asyncio
    async def test_explicit_dry_run_false_does_not_enable_cleanup(self, container):
        """An override is a lever, not an enabler (issue #923).

        `dry_run=False` — what `--no-dry-run` delivers — asks for deletion. It
        satisfies neither arm of the gate while cleanup is disabled, so the
        run is refused exactly as a settings-level dry_run=False is.
        """
        settings = self._settings(enabled=False, dry_run=True)
        result = await run(settings=settings, container=container, dry_run=False)
        assert result["status"] == "skipped"
        assert result["reason"] == "orphan_cleanup_disabled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ttl_hours", [0, -1, 721])
    async def test_out_of_range_ttl_override_refused(self, container, ttl_hours):
        """The kwarg must not be a way around the setting's own bound.

        ttl_hours=0 puts every unlinked file past the cutoff, in-flight
        uploads included — refuse it rather than clamp it silently.
        """
        settings = self._settings(enabled=True, dry_run=False)
        with pytest.raises(ValueError, match="ttl_hours must be between"):
            await run(settings=settings, container=container, ttl_hours=ttl_hours)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ttl_hours", [1, 720])
    async def test_ttl_override_at_the_bounds_accepted(self, container, ttl_hours):
        settings = self._settings(enabled=True, dry_run=True)
        result = await run(settings=settings, container=container, ttl_hours=ttl_hours)
        assert result["ttl_hours"] == ttl_hours

    def test_ttl_bounds_track_the_settings_field(self):
        """The bound is read off the field, so the two cannot drift apart."""
        from faultmaven.config.settings import EvidenceStorageSettings

        field = EvidenceStorageSettings.model_fields["orphan_file_ttl_hours"]
        declared = {
            name: getattr(c, name)
            for c in field.metadata
            for name in ("ge", "le")
            if getattr(c, name, None) is not None
        }
        assert storage_cleanup.ttl_hours_bounds() == (declared["ge"], declared["le"])


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
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
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
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
        )

        assert result["errors"] == 1
        assert result["deleted"] == 0
        assert target.exists()


class TestStraySidecarReporting:
    @pytest.mark.asyncio
    async def test_stray_sidecars_are_counted_not_silent(
        self, storage_service, storage_root
    ):
        """A sidecar whose file is gone is never swept — so it must be visible.

        The phantom-base guard deliberately excludes these (that is what stops
        a hostile key getting another object deleted), which means they would
        otherwise accumulate forever with no signal at all.
        """
        stray = Path(storage_root) / "org/case/1/vanished.log.meta.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(
            json.dumps({"linked": False, "uploaded_at": "2000-01-01T00:00:00+00:00"})
        )

        result = await cleanup_orphaned_files(
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
        )

        assert result["stray_sidecars"] == 1
        assert result["deleted"] == 0
        assert stray.exists()  # left in place, but reported


# ============================================================
# The database cross-check — issue #1232
# ============================================================


class TestDatabaseCrossCheck:
    """The sweep asks the authority; the sidecar is only a hint.

    The state under test is the one #1232 describes and that no test could
    reach before it: an `uploaded_files` row exists and the case references
    the file, but `mark_linked` failed at upload so the sidecar still says
    `linked: false`. Past the TTL, the pre-fix sweep deleted it.

    Note that `test_deletes_only_unlinked_and_past_ttl` passes with the
    cross-check removed — nothing in it is referenced — which is exactly why
    it pins nothing here.
    """

    @pytest.mark.asyncio
    async def test_referenced_file_is_never_deleted_despite_unlinked_sidecar(
        self, storage_service, storage_root
    ):
        """THE regression test: row present, sidecar unflipped, past TTL.

        Remove the `is_referenced` gate in cleanup_orphaned_files and this
        reds — the file is deleted and skipped_db_referenced stays 0.
        """
        old = datetime.now(UTC) - timedelta(hours=48)

        at_risk = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/mark_linked_failed.log",
            linked=False,
            uploaded_at=old,
        )
        at_risk_key = str(at_risk.relative_to(storage_root))

        # Genuinely unreferenced, same age and same sidecar state — the
        # positive control. Without it a sweep that simply stopped deleting
        # anything would also pass.
        genuine_orphan = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/2/genuine_orphan.log",
            linked=False,
            uploaded_at=old,
        )

        result = await cleanup_orphaned_files(
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs={at_risk_key},
        )

        # The live file survives, and the count says WHY it survived.
        assert at_risk.exists(), (
            "a file the database still references was deleted — this is the "
            "irreversible data loss #1232 describes"
        )
        assert Path(f"{at_risk}{SIDECAR_SUFFIX}").exists()
        assert result["skipped_db_referenced"] == 1
        assert result["skipped_linked"] == 0, (
            "the rescue must not be folded into skipped_linked — that would "
            "make the count lie about why the file survived"
        )

        # The positive control: the sweep still works.
        assert not genuine_orphan.exists()
        assert result["found"] == 1
        assert result["deleted"] == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_report_a_referenced_file_as_found(
        self, storage_service, storage_root
    ):
        """A dry run is what an operator reads before arming the sweep.

        Reporting a live file as `would delete` is how a wrong deletion gets
        approved by a human, so the cross-check must apply in dry-run too.
        """
        old = datetime.now(UTC) - timedelta(hours=48)
        at_risk = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/at_risk.log",
            linked=False,
            uploaded_at=old,
        )

        result = await cleanup_orphaned_files(
            storage=storage_service,
            ttl_hours=24,
            dry_run=True,
            referenced_refs={str(at_risk.relative_to(storage_root))},
        )

        assert result["found"] == 0
        assert result["skipped_db_referenced"] == 1

    @pytest.mark.asyncio
    async def test_unreferenced_but_linked_file_is_still_kept(
        self, storage_service, storage_root
    ):
        """The mirror-image hazard, pinned so a later 'tidy-up' cannot land it.

        The tidier-sounding rewrite — "the DB is the authority, delete what it
        does not reference" — is a data-loss change wearing a data-safety
        label. On the measured production corpus 160 of 850 candidates had no
        row and EVERY one of them said linked=true; that rewrite destroys all
        160 on its first armed run. Deletion requires BOTH signals to agree.
        """
        old = datetime.now(UTC) - timedelta(hours=48)
        no_row_but_linked = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/row_deleted_later.log",
            linked=True,
            uploaded_at=old,
        )

        result = await cleanup_orphaned_files(
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
        )

        assert no_row_but_linked.exists()
        assert result["deleted"] == 0
        assert result["skipped_linked"] == 1
        assert result["unreferenced_by_db"] == 1

    @pytest.mark.asyncio
    async def test_classification_counters_report_the_referenced_fraction(
        self, storage_service, storage_root
    ):
        """Standing report, not a one-off probe.

        referenced_by_db / unreferenced_by_db classify EVERY candidate against
        the authority, independent of which deletion branch it lands in — so
        the referenced fraction of the corpus is in every night's run summary.
        """
        now = datetime.now(UTC)
        old = now - timedelta(hours=48)
        recent = now - timedelta(hours=1)

        referenced_old_linked = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/a.log",
            linked=True,
            uploaded_at=old,
        )
        referenced_recent = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/2/b.log",
            linked=False,
            uploaded_at=recent,
        )
        _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/3/c.log",
            linked=False,
            uploaded_at=old,
        )

        refs = {
            str(referenced_old_linked.relative_to(storage_root)),
            str(referenced_recent.relative_to(storage_root)),
        }

        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=True, referenced_refs=refs
        )

        assert result["scanned"] == 3
        assert result["referenced_by_db"] == 2
        assert result["unreferenced_by_db"] == 1
        assert result["referenced_by_db"] + result["unreferenced_by_db"] == (
            result["scanned"]
        )
        assert result["referenced_refs"] == 2


class TestFailsClosedWithoutTheAuthority:
    """A zero-row answer reads as "cannot decide", never "nothing is live"."""

    @pytest.fixture(autouse=True)
    def _bind_temp_storage(self, monkeypatch, storage_service):
        monkeypatch.setattr(
            storage_cleanup, "FileStorageService", lambda: storage_service
        )

    def _settings(self, *, enabled=True, dry_run=False, ttl_hours=24):
        return SimpleNamespace(
            evidence_storage=SimpleNamespace(
                orphan_cleanup_enabled=enabled,
                orphan_cleanup_dry_run=dry_run,
                orphan_file_ttl_hours=ttl_hours,
            )
        )

    @pytest.mark.asyncio
    async def test_empty_reference_set_with_candidates_refuses_the_run(
        self, storage_service, storage_root
    ):
        """The RLS shape: uploaded_files is tenanted and fail-closed, so an
        unbound session sees ZERO rows — under which EVERY live file reads as
        an orphan. Refuse, do not reclassify the whole corpus."""
        old = datetime.now(UTC) - timedelta(hours=48)
        live = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/live.log",
            linked=False,
            uploaded_at=old,
        )

        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=False, referenced_refs=set()
        )

        assert result["status"] == "failed"
        assert result["reason"] == storage_cleanup.REASON_REFERENCE_SET_EMPTY
        assert result["deleted"] == 0
        assert live.exists()

    @pytest.mark.asyncio
    async def test_empty_reference_set_with_no_candidates_is_not_an_error(
        self, storage_service
    ):
        """A genuinely empty deployment has nothing to refuse — the guard is
        about candidates existing while the authority reports none."""
        result = await cleanup_orphaned_files(
            storage=storage_service, ttl_hours=24, dry_run=False, referenced_refs=set()
        )

        assert result["status"] == "completed"
        assert result["scanned"] == 0

    @pytest.mark.asyncio
    async def test_run_refuses_without_a_container(self, storage_root):
        """`container` used to be accepted and ignored. It is the authority
        now, so a run without one is refused rather than degraded to the
        pre-#1232 sidecar-only decision."""
        old = datetime.now(UTC) - timedelta(hours=48)
        live = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/live.log",
            linked=False,
            uploaded_at=old,
        )

        result = await run(settings=self._settings(), container=None)

        assert result["status"] == "failed"
        assert result["reason"] == storage_cleanup.REASON_AUTHORITY_UNAVAILABLE
        assert live.exists()

    @pytest.mark.asyncio
    async def test_run_refuses_when_the_repository_query_raises(self, storage_root):
        """A DB error is 'cannot decide', not 'nothing is referenced'."""
        old = datetime.now(UTC) - timedelta(hours=48)
        live = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/live.log",
            linked=False,
            uploaded_at=old,
        )
        container = SimpleNamespace(
            case_repository=SimpleNamespace(
                list_all_storage_refs=AsyncMock(
                    side_effect=RuntimeError("connection refused")
                )
            )
        )

        result = await run(settings=self._settings(), container=container)

        assert result["status"] == "failed"
        assert result["reason"] == storage_cleanup.REASON_AUTHORITY_UNAVAILABLE
        assert live.exists()

    @pytest.mark.asyncio
    async def test_dry_run_is_refused_too_when_the_authority_is_unreachable(
        self, storage_root
    ):
        """A dry run without the authority prints a classification nobody
        should act on — refuse it rather than publish a fiction."""
        result = await run(
            settings=self._settings(enabled=False, dry_run=True), container=None
        )

        assert result["status"] == "failed"
        assert result["reason"] == storage_cleanup.REASON_AUTHORITY_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_run_passes_the_reference_set_through_to_the_sweep(
        self, storage_service, storage_root
    ):
        """run() must feed the DB answer to the sweep, not a fresh empty set.

        Without this, wiring the query in and then dropping it on the floor
        would leave every other test in this class green.
        """
        old = datetime.now(UTC) - timedelta(hours=48)
        at_risk = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/at_risk.log",
            linked=False,
            uploaded_at=old,
        )
        key = str(at_risk.relative_to(storage_root))
        container = SimpleNamespace(
            case_repository=SimpleNamespace(
                list_all_storage_refs=AsyncMock(return_value={key})
            )
        )

        result = await run(settings=self._settings(), container=container)

        assert result["status"] == "completed"
        assert result["skipped_db_referenced"] == 1
        assert result["deleted"] == 0
        assert at_risk.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make_container,expected_reason",
        [
            (lambda: None, storage_cleanup.REASON_AUTHORITY_UNAVAILABLE),
            (
                lambda: SimpleNamespace(
                    case_repository=SimpleNamespace(
                        list_all_storage_refs=AsyncMock(return_value=set())
                    )
                ),
                storage_cleanup.REASON_REFERENCE_SET_EMPTY,
            ),
        ],
    )
    async def test_a_refusal_exits_non_zero_so_the_cronjob_alert_fires(
        self, storage_root, make_container, expected_reason
    ):
        """A fail-closed nobody hears about is half a fail-closed.

        The runner maps status="skipped" to exit 0, which is exactly the "a
        CronJob that refuses at boot looks like a CronJob with nothing to do"
        shape that hid #1232. These two refusals are the job wanting to run and
        being unable to decide, so they must exit non-zero.
        """
        _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/live.log",
            linked=False,
            uploaded_at=datetime.now(UTC) - timedelta(hours=48),
        )

        result = await run(settings=self._settings(), container=make_container())

        assert result["reason"] == expected_reason
        assert result["status"] not in ("completed", "skipped"), (
            "faultmaven.jobs.run.main() maps completed/skipped to exit 0, so "
            "this refusal would be silent forever"
        )

    @pytest.mark.asyncio
    async def test_the_configured_refusal_still_exits_zero(self, container):
        """The positive control, and the line the split is drawn on.

        ORPHAN_CLEANUP_ENABLED=false is a refusal the DEPLOYMENT asked for; it
        happens every night by design and must stay a clean exit. Without this,
        a blanket "refusals are failures" change would page nightly on every
        deployment that has not opted in.
        """
        result = await run(
            settings=self._settings(enabled=False, dry_run=False), container=container
        )

        assert result["status"] == "skipped"
        assert result["reason"] == storage_cleanup.REASON_CLEANUP_DISABLED

    @pytest.fixture
    def container(self):
        repo = SimpleNamespace(
            list_all_storage_refs=AsyncMock(return_value=set(UNRELATED_REFS))
        )
        return SimpleNamespace(case_repository=repo)


class TestDriftIsSelfHealing:
    """The operator runbook promises this; a promise nobody tests rots.

    `docs/operations/evidence-job-scheduling.md` tells an operator NOT to go
    hunting for leaked objects after sidecar drift, because the object is
    protected only while its `uploaded_files` row lives and is reclaimed
    normally once the row goes. Both halves of that are pinned here: the
    sweep's own behaviour, and the two facts the claim rests on.
    """

    @pytest.mark.asyncio
    async def test_the_same_file_is_protected_then_reclaimed(
        self, storage_service, storage_root
    ):
        """Protected while the row exists; an ordinary orphan once it does not.

        Two sweeps over the SAME file, differing only in whether the database
        still names it. Asserting protection alone (as the other tests do)
        would leave "and then it leaks forever" indistinguishable from "and
        then it is reclaimed on schedule" — which is precisely the sentence
        the runbook now puts in front of an operator.
        """
        old = datetime.now(UTC) - timedelta(hours=48)
        drifted = _write_file_with_sidecar(
            storage_root,
            relative_path="org/case/1/mark_linked_failed.log",
            linked=False,
            uploaded_at=old,
        )
        key = str(drifted.relative_to(storage_root))

        # Row alive: the sidecar says orphan, the database disagrees, the
        # database wins.
        first = await cleanup_orphaned_files(
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs={key},
        )
        assert drifted.exists()
        assert first["skipped_db_referenced"] == 1
        assert first["deleted"] == 0

        # Case deleted -> the row cascades away -> nothing references the
        # object. The sidecar is STILL stale at linked=False; that is the
        # point. It is now an ordinary orphan and is reclaimed.
        second = await cleanup_orphaned_files(
            storage=storage_service,
            ttl_hours=24,
            dry_run=False,
            referenced_refs=UNRELATED_REFS,
        )
        assert not drifted.exists(), (
            "a drifted object must not be protected forever — the runbook "
            "tells operators it is reclaimed once its row is gone"
        )
        assert second["deleted"] == 1
        assert second["skipped_db_referenced"] == 0

    def test_the_row_lifetime_the_claim_rests_on(self):
        """`uploaded_files.case_id` is ON DELETE CASCADE.

        Structural, not behavioural, and deliberately so: the unit fixture in
        the repository tests builds its own engine without the production
        PRAGMA, so a "delete the case, watch the row vanish" test there would
        pass or fail for reasons unrelated to the schema. What the runbook
        claim actually needs is this constraint plus the pragma below.
        """
        from faultmaven.infrastructure.persistence.models import UploadedFileModel

        case_fk = [
            fk
            for fk in UploadedFileModel.__table__.foreign_keys
            if fk.column.table.name == "cases"
        ]
        assert case_fk, "uploaded_files no longer references cases"
        assert all(fk.ondelete == "CASCADE" for fk in case_fk), (
            "uploaded_files.case_id lost ON DELETE CASCADE — a drifted object "
            "would now be protected forever, and the operator runbook says "
            "the opposite"
        )

    def test_sqlite_actually_enforces_that_cascade(self):
        """SQLite ignores FKs unless told otherwise, so the pragma is load-bearing.

        Asserted against the engine-setup source rather than a live engine:
        the listener fires on connect, and the claim is about what production
        wiring does, not about whatever engine a test happens to build.
        """
        import inspect

        from faultmaven.infrastructure.persistence import database

        source = inspect.getsource(database)
        assert "PRAGMA foreign_keys=ON" in source, (
            "the SQLite engine no longer enables foreign_keys, so ON DELETE "
            "CASCADE is silently a no-op there and drifted objects WOULD leak"
        )
