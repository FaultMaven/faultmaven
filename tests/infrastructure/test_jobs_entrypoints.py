"""Tests for job entrypoints and CLI runner.

Verifies:
1. Job run() functions work with in-memory providers
2. Jobs complete without requiring a running server
3. CLI runner correctly dispatches to jobs
4. App boot passes without scheduler threads (RUN_SCHEDULER=false)
"""

import contextlib
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def clean_env():
    """Ensure clean environment for each test."""
    env_vars = ["RUN_SCHEDULER", "SKIP_SERVICE_CHECKS"]
    original = {k: os.environ.get(k) for k in env_vars}

    yield

    # Restore original values
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def mock_container():
    """Create mock DI container with in-memory providers."""
    container = MagicMock()

    # Mock case_vector_store
    mock_vector_store = AsyncMock()
    mock_vector_store.cleanup_orphaned_collections = AsyncMock(return_value=5)
    container.case_vector_store = mock_vector_store

    # Mock case_repository (the reference case-id set for the sweep)
    mock_case_repository = AsyncMock()
    mock_case_repository.list_all_case_ids = AsyncMock(
        return_value=["case_1", "case_2", "case_3"]
    )
    container.case_repository = mock_case_repository

    # Mock initialize
    container.initialize = AsyncMock()

    return container


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.server.run_scheduler = False
    return settings


# =============================================================================
# Case Cleanup Job Tests
# =============================================================================


class TestCaseCleanupJob:
    """Tests for case_cleanup job entrypoint."""

    @pytest.mark.asyncio
    async def test_case_cleanup_run_completes(self, mock_container, mock_settings):
        """Test case cleanup job completes successfully with in-memory providers."""
        from faultmaven.jobs.case_cleanup import run

        result = await run(settings=mock_settings, container=mock_container)

        assert result["status"] == "completed"
        assert result["deleted_count"] == 5
        assert result["active_cases"] == 3

    @pytest.mark.asyncio
    async def test_case_cleanup_skips_when_vector_store_unavailable(
        self, mock_settings
    ):
        """Test job skips gracefully when case_vector_store is not available."""
        from faultmaven.jobs.case_cleanup import run

        container = MagicMock()
        container.case_vector_store = None
        container.case_repository = MagicMock()

        result = await run(settings=mock_settings, container=container)

        assert result["status"] == "skipped"
        assert result["reason"] == "case_vector_store_unavailable"

    @pytest.mark.asyncio
    async def test_case_cleanup_skips_when_case_repository_unavailable(
        self, mock_settings
    ):
        """Test job skips gracefully when case_repository is not available."""
        from faultmaven.jobs.case_cleanup import run

        container = MagicMock()
        container.case_vector_store = MagicMock()
        container.case_repository = None

        result = await run(settings=mock_settings, container=container)

        assert result["status"] == "skipped"
        assert result["reason"] == "case_repository_unavailable"

    @pytest.mark.asyncio
    async def test_case_cleanup_handles_errors(self, mock_settings):
        """Test job handles errors gracefully."""
        from faultmaven.jobs.case_cleanup import run

        container = MagicMock()
        mock_vector_store = AsyncMock()
        mock_vector_store.cleanup_orphaned_collections = AsyncMock(
            side_effect=Exception("Database error")
        )
        container.case_vector_store = mock_vector_store

        mock_case_repository = AsyncMock()
        mock_case_repository.list_all_case_ids = AsyncMock(return_value=["case_1"])
        container.case_repository = mock_case_repository

        result = await run(settings=mock_settings, container=container)

        assert result["status"] == "failed"
        assert "Database error" in result["error"]

    def test_job_metadata_exists(self):
        """Test job module has required metadata."""
        from faultmaven.jobs import case_cleanup

        assert hasattr(case_cleanup, "JOB_NAME")
        assert hasattr(case_cleanup, "JOB_DESCRIPTION")
        assert case_cleanup.JOB_NAME == "case_cleanup"


# =============================================================================
# KB Seed Job Tests (#770)
# =============================================================================


def _bootstrap_result(failed=None):
    from faultmaven.bootstrap.kb_init import BootstrapResult

    result = BootstrapResult()
    result.ingested = ["a.md", "b.md"]
    result.skipped_unchanged = ["c.md"]
    result.failed = failed or []
    return result


class TestKbSeedJob:
    """Tests for the kb_seed job entrypoint (platform KB pack seeding)."""

    @pytest.mark.asyncio
    async def test_kb_seed_completes(self, mock_container, mock_settings):
        from faultmaven.jobs.kb_seed import run

        mock_container.get_knowledge_service = MagicMock(return_value=MagicMock())
        with patch(
            "faultmaven.bootstrap.kb_init.bootstrap_kb",
            new=AsyncMock(return_value=_bootstrap_result()),
        ) as bootstrap:
            result = await run(settings=mock_settings, container=mock_container)

        assert result["status"] == "completed"
        assert result["ingested"] == 2
        assert result["skipped_unchanged"] == 1
        assert result["failures"] == []
        bootstrap.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kb_seed_skips_without_knowledge_service(
        self, mock_container, mock_settings
    ):
        from faultmaven.jobs.kb_seed import run

        mock_container.get_knowledge_service = MagicMock(return_value=None)
        result = await run(settings=mock_settings, container=mock_container)

        assert result["status"] == "skipped"
        assert result["reason"] == "knowledge_service_unavailable"

    # The companion case — "a partially composed container hands back a stub
    # with no ``ingest_runbook``" — is gone with the stub itself (#899). The
    # container now returns either a real KnowledgeService or None, which makes
    # the ``is None`` test above the complete guard. That the container does not
    # substitute anything is pinned at its own layer, in
    # tests/unit/container/test_knowledge_service_db_wiring.py.

    @pytest.mark.asyncio
    async def test_kb_seed_partial_failure_is_failed_status(
        self, mock_container, mock_settings
    ):
        """A runbook that fails to seed is absent for every tenant — the job
        must exit non-zero so the operator notices."""
        from faultmaven.jobs.kb_seed import run

        mock_container.get_knowledge_service = MagicMock(return_value=MagicMock())
        with patch(
            "faultmaven.bootstrap.kb_init.bootstrap_kb",
            new=AsyncMock(return_value=_bootstrap_result(failed=[("bad.md", "boom")])),
        ):
            result = await run(settings=mock_settings, container=mock_container)

        assert result["status"] == "failed"
        assert result["failures"] == [("bad.md", "boom")]

    @pytest.mark.asyncio
    async def test_kb_seed_handles_errors(self, mock_container, mock_settings):
        from faultmaven.jobs.kb_seed import run

        mock_container.get_knowledge_service = MagicMock(return_value=MagicMock())
        with patch(
            "faultmaven.bootstrap.kb_init.bootstrap_kb",
            new=AsyncMock(side_effect=RuntimeError("pack unreadable")),
        ):
            result = await run(settings=mock_settings, container=mock_container)

        assert result["status"] == "failed"
        assert "pack unreadable" in result["error"]

    def test_kb_seed_is_cross_tenant(self):
        """kb_seed writes the org-free global platform tier served to ALL
        tenants — under multi it must only run on the audited maintenance
        path, so the declaration must stay cross_tenant."""
        from faultmaven.jobs import kb_seed

        assert kb_seed.JOB_TENANT_SCOPE == "cross_tenant"
        assert kb_seed.JOB_NAME == "kb_seed"

    def test_kb_seed_registered(self):
        from faultmaven.jobs.run import AVAILABLE_JOBS

        assert AVAILABLE_JOBS["kb_seed"] == "faultmaven.jobs.kb_seed"


# =============================================================================
# CLI Runner Tests
# =============================================================================


class TestJobsRunner:
    """Tests for the CLI job runner."""

    def test_list_available_jobs(self):
        """Test listing available jobs."""
        from faultmaven.jobs.run import list_available_jobs

        jobs = list_available_jobs()

        assert len(jobs) >= 1
        job_names = [j["name"] for j in jobs]
        assert "case_cleanup" in job_names

    def test_main_list_flag(self, capsys):
        """Test CLI --list flag."""
        from faultmaven.jobs.run import main

        exit_code = main(["--list"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "case_cleanup" in captured.out

    def test_main_no_args_shows_help(self, capsys):
        """Test CLI with no args shows help."""
        from faultmaven.jobs.run import main

        exit_code = main([])

        assert exit_code == 1

    def test_main_unknown_job(self):
        """Test CLI with unknown job name."""
        from faultmaven.jobs.run import main

        exit_code = main(["nonexistent_job"])

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_run_job_with_mock_container(self, mock_container, mock_settings):
        """Test run_job function with mocked dependencies."""
        from faultmaven.jobs.run import run_job

        with (
            patch(
                "faultmaven.config.settings.get_settings", return_value=mock_settings
            ),
            patch("faultmaven.container.container", mock_container),
            patch(
                "faultmaven.config.deployment_coherence.validate_deployment_coherence"
            ),
            patch(
                "faultmaven.providers.tenancy.factory.requested_tenant_provider",
                return_value="single",
            ),
        ):
            result = await run_job("case_cleanup", verbose=False)

        assert result["status"] == "completed"


# =============================================================================
# App Boot Tests (without scheduler)
# =============================================================================


class TestAppBootWithoutScheduler:
    """Tests that app boots correctly without scheduler threads."""

    def test_run_scheduler_default_is_false(self, clean_env):
        """Test that RUN_SCHEDULER defaults to False."""
        # Remove any existing RUN_SCHEDULER
        os.environ.pop("RUN_SCHEDULER", None)

        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        settings = get_settings()
        assert settings.server.run_scheduler is False

    def test_run_scheduler_can_be_enabled(self, clean_env):
        """Test that RUN_SCHEDULER can be set to True."""
        os.environ["RUN_SCHEDULER"] = "true"

        from faultmaven.config.settings import get_settings, reset_settings

        reset_settings()

        settings = get_settings()
        assert settings.server.run_scheduler is True

    def test_settings_documents_run_scheduler(self):
        """Test that RUN_SCHEDULER setting is documented."""
        from faultmaven.config.settings import ServerSettings

        # Check the field exists and has description
        field_info = ServerSettings.model_fields.get("run_scheduler")
        assert field_info is not None
        assert "operational neutrality" in field_info.description.lower()


# =============================================================================
# Integration Tests (job works without server)
# =============================================================================


class TestJobsWorkWithoutServer:
    """Tests that jobs work independently of a running server."""

    @pytest.mark.asyncio
    async def test_case_cleanup_no_server_dependency(
        self, mock_container, mock_settings
    ):
        """Test case cleanup job has no dependency on running server."""
        from faultmaven.jobs.case_cleanup import run

        # Should complete without any FastAPI/uvicorn imports or running server
        result = await run(settings=mock_settings, container=mock_container)

        assert result["status"] == "completed"
        # Verify the cleanup was actually called
        mock_container.case_vector_store.cleanup_orphaned_collections.assert_called_once()

    def test_job_module_imports_cleanly(self):
        """Test job modules can be imported without side effects."""
        # This should not start any servers or schedulers
        import faultmaven.jobs.case_cleanup
        import faultmaven.jobs.run

        # Just importing should not cause errors
        assert hasattr(faultmaven.jobs.case_cleanup, "run")
        assert hasattr(faultmaven.jobs.run, "main")


# =============================================================================
# Tenant-scope gates on the jobs path (ADR-010 P3, issue #629)
# =============================================================================

FAKE_JOB_MODULE = "tests_fake_job_module_p3"


@contextlib.contextmanager
def _fake_job(tenant_scope, mock_container, mock_settings, provider="multi"):
    """Register a fake job module and patch the runner's boot gates.

    Yields (job_run_mock, rls_guard_mock, set_org_mock, maintenance_guard_mock).
    """
    module = types.ModuleType(FAKE_JOB_MODULE)
    module.run = AsyncMock(return_value={"status": "completed"})
    if tenant_scope is not None:
        module.JOB_TENANT_SCOPE = tenant_scope
    sys.modules[FAKE_JOB_MODULE] = module

    rls_guard = AsyncMock()
    maintenance_guard = AsyncMock()
    set_org = MagicMock()
    try:
        with (
            patch(
                "faultmaven.config.settings.get_settings", return_value=mock_settings
            ),
            patch("faultmaven.container.container", mock_container),
            patch(
                "faultmaven.config.deployment_coherence.validate_deployment_coherence"
            ),
            patch(
                "faultmaven.providers.tenancy.factory.requested_tenant_provider",
                return_value=provider,
            ),
            patch(
                "faultmaven.infrastructure.persistence.rls_role_guard"
                ".assert_app_db_role_enforces_rls",
                rls_guard,
            ),
            patch(
                "faultmaven.infrastructure.persistence.rls_role_guard"
                ".assert_maintenance_db_role_posture",
                maintenance_guard,
            ),
            patch("faultmaven.config.tenant_context.set_current_org_id", set_org),
            patch.dict(
                "faultmaven.jobs.run.AVAILABLE_JOBS",
                {"fake_job": FAKE_JOB_MODULE},
            ),
        ):
            yield module.run, rls_guard, set_org, maintenance_guard
    finally:
        sys.modules.pop(FAKE_JOB_MODULE, None)


class TestJobTenantScopeGates:
    """The runner enforces each job's declared tenant scope (ADR-010 P3)."""

    @pytest.mark.asyncio
    async def test_cross_tenant_job_refused_under_multi(
        self, mock_container, mock_settings
    ):
        """A cross_tenant job must fail closed under multi: the RLS-scoped role
        sees a partial case-id view, which would delete other tenants' data."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job("cross_tenant", mock_container, mock_settings) as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError):
                await run_job("fake_job")

        job_run.assert_not_called()
        # Refusal happens before any heavy initialization.
        mock_container.initialize.assert_not_called()

    @pytest.mark.asyncio
    async def test_org_job_requires_explicit_org_under_multi(
        self, mock_container, mock_settings
    ):
        """An org-scoped job without --organization-id fails closed under multi
        (the contextvar default is the never-seeded Standalone org)."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job("org", mock_container, mock_settings) as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError):
                await run_job("fake_job")

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_org_job_binds_tenant_context_under_multi(
        self, mock_container, mock_settings
    ):
        """An org-scoped job with an explicit org binds the tenant contextvar
        before running, so every DB transaction is RLS-scoped to that org."""
        from faultmaven.jobs.run import run_job

        with _fake_job("org", mock_container, mock_settings) as (
            job_run,
            rls_guard,
            set_org,
            _maint,
        ):
            # The binding must happen BEFORE the job executes — a run with the
            # contextvar still at its default is exactly the P3 hole.
            def _assert_bound_then_complete(**kwargs):
                assert (
                    set_org.called
                ), "tenant context must be bound before the job runs"
                return {"status": "completed"}

            job_run.side_effect = _assert_bound_then_complete

            result = await run_job("fake_job", organization_id="org_alpha")

        assert result["status"] == "completed"
        set_org.assert_called_once_with("org_alpha")
        rls_guard.assert_awaited_once_with(is_multi_tenant=True)
        job_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tenant_neutral_job_runs_under_multi_without_org(
        self, mock_container, mock_settings
    ):
        """A tenant-neutral job (no tenanted DB access) runs under multi with
        no org binding."""
        from faultmaven.jobs.run import run_job

        with _fake_job("tenant_neutral", mock_container, mock_settings) as (
            job_run,
            _,
            set_org,
            _maint,
        ):
            result = await run_job("fake_job")

        assert result["status"] == "completed"
        set_org.assert_not_called()
        job_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_undeclared_scope_fails_closed_under_multi(
        self, mock_container, mock_settings
    ):
        """A job with no JOB_TENANT_SCOPE declaration cannot run under multi."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job(None, mock_container, mock_settings) as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError):
                await run_job("fake_job")

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_undeclared_scope_still_runs_single_tenant(
        self, mock_container, mock_settings
    ):
        """Single-tenant behavior is unchanged: the Standalone contextvar
        default scopes correctly, no declaration required."""
        from faultmaven.jobs.run import run_job

        with _fake_job(None, mock_container, mock_settings, provider="single") as (
            job_run,
            _,
            set_org,
            _maint,
        ):
            result = await run_job("fake_job")

        assert result["status"] == "completed"
        set_org.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_scope_declaration_rejected_in_any_mode(
        self, mock_container, mock_settings
    ):
        """A typo'd JOB_TENANT_SCOPE fails loudly even in single-tenant, so a
        bad declaration is caught long before a multi deployment runs it."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job("per-org", mock_container, mock_settings, provider="single") as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError):
                await run_job("fake_job")

        job_run.assert_not_called()

    def test_real_job_modules_declare_valid_scopes(self):
        """Every registered job declares a scope the runner recognizes."""
        import importlib

        from faultmaven.jobs.run import _VALID_TENANT_SCOPES, AVAILABLE_JOBS

        for job_name, module_path in AVAILABLE_JOBS.items():
            module = importlib.import_module(module_path)
            scope = getattr(module, "JOB_TENANT_SCOPE", None)
            assert scope in _VALID_TENANT_SCOPES, (
                f"Job '{job_name}' ({module_path}) must declare a valid "
                f"JOB_TENANT_SCOPE, got {scope!r}"
            )

    def test_case_cleanup_is_cross_tenant(self):
        """case_cleanup diffs the DB case-id set against non-partitioned
        ChromaDB collections — it must stay cross_tenant scoped."""
        from faultmaven.jobs import case_cleanup

        assert case_cleanup.JOB_TENANT_SCOPE == "cross_tenant"

    def test_storage_cleanup_is_tenant_neutral(self):
        """storage_cleanup is a sidecar-driven filesystem sweep with no
        tenanted DB reads."""
        from faultmaven.modules.agent.jobs import storage_cleanup

        assert storage_cleanup.JOB_TENANT_SCOPE == "tenant_neutral"


class TestJobsPathBootGates:
    """The jobs path runs the same boot gates as the web lifespan."""

    @pytest.mark.asyncio
    async def test_coherence_gate_failure_is_terminal(
        self, mock_container, mock_settings
    ):
        """An incoherent deployment config refuses the job (not a warning)."""
        from faultmaven.config.deployment_coherence import DeploymentCoherenceError
        from faultmaven.jobs.run import run_job

        with (
            patch(
                "faultmaven.config.settings.get_settings", return_value=mock_settings
            ),
            patch("faultmaven.container.container", mock_container),
            patch(
                "faultmaven.config.deployment_coherence.validate_deployment_coherence",
                side_effect=DeploymentCoherenceError("incoherent"),
            ),
        ):
            with pytest.raises(DeploymentCoherenceError):
                await run_job("case_cleanup")

        mock_container.initialize.assert_not_called()

    @pytest.mark.asyncio
    async def test_rls_role_guard_failure_is_terminal(
        self, mock_container, mock_settings
    ):
        """A multi-tenant job with an RLS-exempt DB role refuses to run."""
        from faultmaven.config.deployment_coherence import DeploymentCoherenceError
        from faultmaven.jobs.run import run_job

        with _fake_job("tenant_neutral", mock_container, mock_settings) as (
            job_run,
            rls_guard,
            _set_org,
            _maint,
        ):
            rls_guard.side_effect = DeploymentCoherenceError("role is RLS-exempt")
            with pytest.raises(DeploymentCoherenceError):
                await run_job("fake_job")

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_container_init_runtime_error_is_terminal(
        self, mock_container, mock_settings
    ):
        """The container's production fail-fast (RuntimeError) refuses the job
        instead of degrading to a half-initialized 'skipped' run (exit 0)."""
        from faultmaven.jobs.run import run_job

        mock_container.initialize.side_effect = RuntimeError(
            "Container initialization failed in production"
        )
        with _fake_job(
            "tenant_neutral", mock_container, mock_settings, provider="single"
        ) as (job_run, _, _set_org, _maint):
            with pytest.raises(RuntimeError):
                await run_job("fake_job")

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_rls_guard_noop_single_tenant(self, mock_container, mock_settings):
        """Single-tenant jobs pass the guard without touching the DB (the
        guard short-circuits on is_multi_tenant=False)."""
        from faultmaven.jobs.run import run_job

        with _fake_job("cross_tenant", mock_container, mock_settings, "single") as (
            job_run,
            rls_guard,
            _set_org,
            _maint,
        ):
            result = await run_job("fake_job")

        assert result["status"] == "completed"
        rls_guard.assert_awaited_once_with(is_multi_tenant=False)


class TestSchedulerMultiTenantGate:
    """The in-process cleanup scheduler is refused under multi (same hazard)."""

    def test_scheduler_refused_under_multi(self):
        from faultmaven.infrastructure.tasks.case_cleanup import (
            start_case_cleanup_scheduler,
        )

        with patch(
            "faultmaven.infrastructure.tasks.case_cleanup.BackgroundScheduler"
        ) as scheduler_cls:
            result = start_case_cleanup_scheduler(
                case_vector_store=MagicMock(),
                case_repository=MagicMock(),
                is_multi_tenant=True,
            )

        assert result is None
        scheduler_cls.assert_not_called()

    def test_scheduler_starts_single_tenant(self):
        from faultmaven.infrastructure.tasks.case_cleanup import (
            start_case_cleanup_scheduler,
        )

        with patch(
            "faultmaven.infrastructure.tasks.case_cleanup.BackgroundScheduler"
        ) as scheduler_cls:
            result = start_case_cleanup_scheduler(
                case_vector_store=MagicMock(),
                case_repository=MagicMock(),
                is_multi_tenant=False,
            )

        assert result is scheduler_cls.return_value
        scheduler_cls.return_value.start.assert_called_once()


# =============================================================================
# Audited cross-tenant maintenance path (ADR-010 / issue #629)
# =============================================================================


class TestCrossTenantMaintenancePath:
    """--cross-tenant-maintenance is the ONLY way to run a cross_tenant job
    under multi, and it swaps the app-role guard for the maintenance guard."""

    @pytest.mark.asyncio
    async def test_maintenance_path_runs_cross_tenant_job(
        self, mock_container, mock_settings, caplog
    ):
        """Flag + maintenance role → the job runs; the maintenance guard (not
        the app guard) is enforced; the run is audit-logged; no org is bound
        (BYPASSRLS makes the contextvar irrelevant)."""
        from faultmaven.jobs.run import run_job

        with _fake_job("cross_tenant", mock_container, mock_settings) as (
            job_run,
            rls_guard,
            set_org,
            maintenance_guard,
        ):
            maintenance_guard.return_value = "faultmaven_maintenance"
            result = await run_job("fake_job", cross_tenant_maintenance=True)

        assert result["status"] == "completed"
        job_run.assert_awaited_once()
        # The acknowledgment flag is runner input, never job input.
        assert "cross_tenant_maintenance" not in job_run.await_args.kwargs
        maintenance_guard.assert_awaited_once()
        rls_guard.assert_not_called()
        set_org.assert_not_called()
        audit_lines = [r for r in caplog.records if "AUDIT" in r.getMessage()]
        assert audit_lines, "cross-tenant maintenance run must be audit-logged"
        assert audit_lines[0].levelname == "WARNING"
        assert "fake_job" in audit_lines[0].getMessage()
        # WHO ran it: the probe-verified DB role is part of the audit record.
        assert "faultmaven_maintenance" in audit_lines[0].getMessage()

    @pytest.mark.asyncio
    async def test_audit_line_survives_crashing_job(
        self, mock_container, mock_settings, caplog
    ):
        """The audit record is emitted BEFORE the job runs, so a crashed
        sweep still leaves the trail."""
        from faultmaven.jobs.run import run_job

        with _fake_job("cross_tenant", mock_container, mock_settings) as (
            job_run,
            _rls_guard,
            _set_org,
            maintenance_guard,
        ):
            maintenance_guard.return_value = "faultmaven_maintenance"
            job_run.side_effect = RuntimeError("sweep exploded")
            result = await run_job("fake_job", cross_tenant_maintenance=True)

        assert result["status"] == "failed"
        audit_lines = [r for r in caplog.records if "AUDIT" in r.getMessage()]
        assert audit_lines, "audit record must precede the job invocation"

    @pytest.mark.asyncio
    async def test_flag_with_organization_id_refused(
        self, mock_container, mock_settings
    ):
        """The maintenance path bypasses RLS entirely — an org id could not
        scope anything, so passing both is contradictory manifest input."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job("cross_tenant", mock_container, mock_settings) as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError, match="mutually"):
                await run_job(
                    "fake_job",
                    cross_tenant_maintenance=True,
                    organization_id="org-1",
                )

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_maintenance_path_refused_on_wrong_role(
        self, mock_container, mock_settings
    ):
        """Flag + a role that fails the maintenance posture probe → refused
        before the job runs."""
        from faultmaven.config.deployment_coherence import DeploymentCoherenceError
        from faultmaven.jobs.run import run_job

        with _fake_job("cross_tenant", mock_container, mock_settings) as (
            job_run,
            _rls_guard,
            _set_org,
            maintenance_guard,
        ):
            maintenance_guard.side_effect = DeploymentCoherenceError(
                "role lacks BYPASSRLS"
            )
            with pytest.raises(DeploymentCoherenceError):
                await run_job("fake_job", cross_tenant_maintenance=True)

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_refused_for_org_scoped_job(self, mock_container, mock_settings):
        """The maintenance role must never run tenant-scoped work: an org job
        with the flag fails closed (it would see every tenant's rows)."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job("org", mock_container, mock_settings) as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError):
                await run_job(
                    "fake_job",
                    cross_tenant_maintenance=True,
                    organization_id="org-1",
                )

        job_run.assert_not_called()
        mock_container.initialize.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_refused_for_tenant_neutral_job(
        self, mock_container, mock_settings
    ):
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job("tenant_neutral", mock_container, mock_settings) as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError):
                await run_job("fake_job", cross_tenant_maintenance=True)

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_refused_under_single_tenant(
        self, mock_container, mock_settings
    ):
        """Single-tenant has no maintenance role; the flag is config drift
        (e.g. a manifest copied from cloud) and fails closed."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job(
            "cross_tenant", mock_container, mock_settings, provider="single"
        ) as (job_run, _, _set_org, _maint):
            with pytest.raises(JobTenantScopeError):
                await run_job("fake_job", cross_tenant_maintenance=True)

        job_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_tenant_without_flag_still_refused(
        self, mock_container, mock_settings
    ):
        """The pre-existing fail-closed default survives: no flag → refusal,
        and the message points at the maintenance path."""
        from faultmaven.jobs.run import JobTenantScopeError, run_job

        with _fake_job("cross_tenant", mock_container, mock_settings) as (
            job_run,
            _,
            _set_org,
            _maint,
        ):
            with pytest.raises(JobTenantScopeError, match="cross-tenant-maintenance"):
                await run_job("fake_job")

        job_run.assert_not_called()

    def test_cli_flag_reaches_run_job(self):
        """--cross-tenant-maintenance parses and lands in run_job kwargs."""
        from faultmaven.jobs import run as run_module

        captured = {}

        async def fake_run_job(job_name, verbose=False, **kwargs):
            captured.update(kwargs, job_name=job_name)
            return {"status": "completed"}

        with patch.object(run_module, "run_job", side_effect=fake_run_job):
            exit_code = run_module.main(["case_cleanup", "--cross-tenant-maintenance"])

        assert exit_code == 0
        assert captured["cross_tenant_maintenance"] is True
        assert captured["job_name"] == "case_cleanup"
