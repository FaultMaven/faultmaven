"""Tests for job entrypoints and CLI runner.

Verifies:
1. Job run() functions work with in-memory providers
2. Jobs complete without requiring a running server
3. CLI runner correctly dispatches to jobs
4. App boot passes without scheduler threads (RUN_SCHEDULER=false)
"""

import os
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

    # Mock case_store
    mock_case_store = AsyncMock()
    mock_case_store.get_all_case_ids = AsyncMock(
        return_value=["case_1", "case_2", "case_3"]
    )
    container.case_store = mock_case_store

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
        container.case_store = MagicMock()

        result = await run(settings=mock_settings, container=container)

        assert result["status"] == "skipped"
        assert result["reason"] == "case_vector_store_unavailable"

    @pytest.mark.asyncio
    async def test_case_cleanup_skips_when_case_store_unavailable(self, mock_settings):
        """Test job skips gracefully when case_store is not available."""
        from faultmaven.jobs.case_cleanup import run

        container = MagicMock()
        container.case_vector_store = MagicMock()
        container.case_store = None

        result = await run(settings=mock_settings, container=container)

        assert result["status"] == "skipped"
        assert result["reason"] == "case_store_unavailable"

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

        mock_case_store = AsyncMock()
        mock_case_store.get_all_case_ids = AsyncMock(return_value=["case_1"])
        container.case_store = mock_case_store

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
