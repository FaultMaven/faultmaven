"""Tests for job runner abstraction.

Tests cover:
- InMemoryJobRunner functionality
- Factory function behavior
- Job scheduling and cancellation
- Async task execution
"""

import pytest
import asyncio
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import threading


class TestInMemoryJobRunner:
    """Test InMemoryJobRunner implementation."""

    def test_start_and_stop(self):
        """Test starting and stopping the job runner."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        assert not runner.is_running()

        runner.start()
        assert runner.is_running()

        runner.shutdown()
        assert not runner.is_running()

    def test_schedule_recurring_job(self):
        """Test scheduling a recurring job."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        call_count = {"count": 0}

        def task():
            call_count["count"] += 1

        job_id = runner.schedule_recurring(
            task_func=task,
            interval_seconds=1,
            job_id="test_recurring",
            job_name="Test Recurring Job",
        )

        assert job_id == "test_recurring"

        # Verify job is listed
        jobs = runner.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "test_recurring"
        assert jobs[0]["name"] == "Test Recurring Job"

        runner.shutdown()

    def test_schedule_once_job(self):
        """Test scheduling a one-time job."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        executed = {"done": False}

        def task():
            executed["done"] = True

        run_at = datetime.now(timezone.utc) + timedelta(seconds=0.1)
        job_id = runner.schedule_once(task_func=task, run_at=run_at, job_id="test_once")

        assert job_id == "test_once"

        # Wait for job to execute
        time.sleep(0.3)

        assert executed["done"] is True

        runner.shutdown()

    def test_cancel_job(self):
        """Test cancelling a scheduled job."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        def task():
            pass

        job_id = runner.schedule_recurring(
            task_func=task, interval_seconds=60, job_id="test_cancel"
        )

        # Job should exist
        assert runner.get_job_info(job_id) is not None
        assert len(runner.list_jobs()) == 1

        # Cancel job
        result = runner.cancel_job(job_id)
        assert result is True

        # Job should be gone
        assert runner.get_job_info(job_id) is None
        assert len(runner.list_jobs()) == 0

        runner.shutdown()

    def test_cancel_nonexistent_job(self):
        """Test cancelling a job that doesn't exist."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        result = runner.cancel_job("nonexistent")
        assert result is False

        runner.shutdown()

    def test_get_job_info(self):
        """Test getting job information."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        def task():
            pass

        runner.schedule_recurring(
            task_func=task,
            interval_seconds=3600,
            job_id="test_info",
            job_name="Test Info Job",
        )

        info = runner.get_job_info("test_info")
        assert info is not None
        assert info["job_id"] == "test_info"
        assert info["name"] == "Test Info Job"
        assert info["type"] == "recurring"
        assert info["interval_seconds"] == 3600
        assert info["status"] == "scheduled"

        runner.shutdown()

    def test_async_task_execution(self):
        """Test that async tasks are executed correctly."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        executed = {"done": False}

        async def async_task():
            await asyncio.sleep(0.05)
            executed["done"] = True

        run_at = datetime.now(timezone.utc) + timedelta(seconds=0.1)
        runner.schedule_once(task_func=async_task, run_at=run_at)

        # Wait for job to execute
        time.sleep(0.5)

        assert executed["done"] is True

        runner.shutdown()

    def test_replace_existing_job(self):
        """Test replacing an existing job."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        def task1():
            pass

        def task2():
            pass

        # Schedule first job
        runner.schedule_recurring(
            task_func=task1, interval_seconds=60, job_id="test_replace"
        )

        # Replace with second job
        runner.schedule_recurring(
            task_func=task2,
            interval_seconds=120,
            job_id="test_replace",
            replace_existing=True,
        )

        # Should still be one job
        assert len(runner.list_jobs()) == 1

        info = runner.get_job_info("test_replace")
        assert info["interval_seconds"] == 120

        runner.shutdown()

    def test_start_without_running_raises_error(self):
        """Test that scheduling without starting raises an error."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()

        with pytest.raises(RuntimeError, match="Job runner not started"):
            runner.schedule_recurring(task_func=lambda: None, interval_seconds=60)

    def test_auto_generated_job_id(self):
        """Test that job IDs are auto-generated if not provided."""
        from faultmaven.infrastructure.tasks import InMemoryJobRunner

        runner = InMemoryJobRunner()
        runner.start()

        job_id = runner.schedule_recurring(task_func=lambda: None, interval_seconds=60)

        assert job_id.startswith("job_")
        assert len(job_id) == 16  # "job_" + 12 hex chars

        runner.shutdown()


class TestJobRunnerFactory:
    """Test get_job_runner factory function."""

    def test_default_returns_inmemory(self):
        """Test that default returns InMemoryJobRunner."""
        from faultmaven.infrastructure.tasks import get_job_runner, InMemoryJobRunner

        with patch.dict("os.environ", {}, clear=True):
            import os

            os.environ.pop("JOB_RUNNER_TYPE", None)
            os.environ.pop("ENVIRONMENT", None)

            runner = get_job_runner()
            assert isinstance(runner, InMemoryJobRunner)

    def test_explicit_inmemory(self):
        """Test explicit inmemory selection."""
        from faultmaven.infrastructure.tasks import get_job_runner, InMemoryJobRunner

        runner = get_job_runner("inmemory")
        assert isinstance(runner, InMemoryJobRunner)

    def test_invalid_type_raises_error(self):
        """Test that invalid type raises ValueError."""
        from faultmaven.infrastructure.tasks import get_job_runner

        with pytest.raises(ValueError, match="Unknown job runner type"):
            get_job_runner("invalid_type")

    def test_apscheduler_fallback_when_unavailable(self):
        """Test fallback to InMemory when APScheduler not available."""
        from faultmaven.infrastructure.tasks import get_job_runner, InMemoryJobRunner

        with patch(
            "faultmaven.infrastructure.tasks.job_runners.APSCHEDULER_AVAILABLE", False
        ):
            runner = get_job_runner("apscheduler")
            assert isinstance(runner, InMemoryJobRunner)

    def test_env_var_selection(self):
        """Test selection via JOB_RUNNER_TYPE env var."""
        from faultmaven.infrastructure.tasks import get_job_runner, InMemoryJobRunner
        import os

        with patch.dict(os.environ, {"JOB_RUNNER_TYPE": "inmemory"}):
            runner = get_job_runner()
            assert isinstance(runner, InMemoryJobRunner)


class TestAPSchedulerJobRunner:
    """Test APSchedulerJobRunner implementation."""

    def test_apscheduler_available_check(self):
        """Test that APScheduler availability is properly detected."""
        from faultmaven.infrastructure.tasks import APSCHEDULER_AVAILABLE

        # This test just verifies the constant exists
        assert isinstance(APSCHEDULER_AVAILABLE, bool)

    def test_apscheduler_import_error_when_unavailable(self):
        """Test ImportError when APScheduler not installed."""
        with patch(
            "faultmaven.infrastructure.tasks.job_runners.APSCHEDULER_AVAILABLE", False
        ):
            # Re-import to get fresh class
            from faultmaven.infrastructure.tasks.job_runners import APSchedulerJobRunner

            with pytest.raises(ImportError, match="APScheduler is not installed"):
                APSchedulerJobRunner()


class TestJobRunnerConfiguration:
    """Test job runner configuration in settings."""

    def test_default_job_runner_type(self):
        """Test default job_runner_type setting."""
        from faultmaven.config.settings import FeatureSettings

        settings = FeatureSettings()
        assert settings.job_runner_type == "inmemory"

    def test_job_runner_type_from_env(self):
        """Test job_runner_type from environment variable."""
        import os
        from faultmaven.config.settings import FeatureSettings

        with patch.dict(os.environ, {"JOB_RUNNER_TYPE": "apscheduler"}):
            settings = FeatureSettings()
            assert settings.job_runner_type == "apscheduler"
