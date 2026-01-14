"""Job Runner Implementations

Provides different job runner backends for scheduling background tasks.
The job runner abstraction allows FaultMaven to use different schedulers
without changing application code.

Implementations:
- APSchedulerJobRunner: Production scheduler using APScheduler (external dep)
- InMemoryJobRunner: Simple threading-based scheduler for local development

Configuration:
    JOB_RUNNER_TYPE=apscheduler  # Use APScheduler (requires apscheduler package)
    JOB_RUNNER_TYPE=inmemory     # Use in-memory scheduler (no external deps)

Usage:
    from faultmaven.infrastructure.tasks import get_job_runner

    runner = get_job_runner()
    runner.start()

    job_id = runner.schedule_recurring(
        task_func=my_cleanup_task,
        interval_seconds=3600,
        job_id="hourly_cleanup"
    )

    runner.shutdown()
"""

import asyncio
import logging
import threading
import time
import uuid
from abc import ABC
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from faultmaven.models.interfaces import IJobRunner

logger = logging.getLogger(__name__)


# =============================================================================
# APSCHEDULER IMPLEMENTATION
# =============================================================================

# Check if APScheduler is available
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    APSCHEDULER_AVAILABLE = True
except ImportError:
    BackgroundScheduler = None
    IntervalTrigger = None
    DateTrigger = None
    APSCHEDULER_AVAILABLE = False


class APSchedulerJobRunner(IJobRunner):
    """Job runner implementation using APScheduler.

    This is the recommended implementation for production deployments.
    Requires the `apscheduler` package to be installed.

    Features:
    - Persistent job scheduling with configurable job stores
    - Thread-safe job execution
    - Missed job execution handling
    - Multiple trigger types (interval, cron, date)
    """

    def __init__(self):
        if not APSCHEDULER_AVAILABLE:
            raise ImportError(
                "APScheduler is not installed. Install it with: pip install apscheduler"
            )
        self._scheduler: Optional[BackgroundScheduler] = None
        self._started = False

    def start(self) -> None:
        """Start the APScheduler background scheduler."""
        if self._started:
            logger.warning("APScheduler job runner already started")
            return

        self._scheduler = BackgroundScheduler()
        self._scheduler.start()
        self._started = True
        logger.info("APScheduler job runner started")

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the APScheduler scheduler."""
        if not self._started or not self._scheduler:
            return

        try:
            self._scheduler.shutdown(wait=wait)
            self._started = False
            logger.info("APScheduler job runner stopped")
        except Exception as e:
            logger.warning(f"Error stopping APScheduler: {e}")

    def _wrap_async_task(
        self, task_func: Callable, args: tuple, kwargs: dict
    ) -> Callable:
        """Wrap async task function for synchronous execution by APScheduler."""

        def sync_wrapper():
            if asyncio.iscoroutinefunction(task_func):
                try:
                    asyncio.run(task_func(*args, **kwargs))
                except Exception as e:
                    logger.error(f"Error in async task: {e}", exc_info=True)
            else:
                try:
                    task_func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Error in sync task: {e}", exc_info=True)

        return sync_wrapper

    def schedule_recurring(
        self,
        task_func: Callable,
        interval_seconds: int,
        job_id: Optional[str] = None,
        job_name: Optional[str] = None,
        args: tuple = (),
        kwargs: dict = None,
        replace_existing: bool = True,
    ) -> str:
        """Schedule a recurring job using APScheduler."""
        if not self._started or not self._scheduler:
            raise RuntimeError("Job runner not started. Call start() first.")

        kwargs = kwargs or {}
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        job_name = job_name or f"Recurring job {job_id}"

        wrapped_func = self._wrap_async_task(task_func, args, kwargs)

        self._scheduler.add_job(
            func=wrapped_func,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            name=job_name,
            replace_existing=replace_existing,
        )

        logger.info(
            f"Scheduled recurring job '{job_name}' ({job_id}) every {interval_seconds}s"
        )
        return job_id

    def schedule_once(
        self,
        task_func: Callable,
        run_at: datetime,
        job_id: Optional[str] = None,
        job_name: Optional[str] = None,
        args: tuple = (),
        kwargs: dict = None,
    ) -> str:
        """Schedule a one-time job using APScheduler."""
        if not self._started or not self._scheduler:
            raise RuntimeError("Job runner not started. Call start() first.")

        kwargs = kwargs or {}
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        job_name = job_name or f"One-time job {job_id}"

        wrapped_func = self._wrap_async_task(task_func, args, kwargs)

        self._scheduler.add_job(
            func=wrapped_func,
            trigger=DateTrigger(run_date=run_at),
            id=job_id,
            name=job_name,
        )

        logger.info(f"Scheduled one-time job '{job_name}' ({job_id}) at {run_at}")
        return job_id

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a scheduled job."""
        if not self._scheduler:
            return False

        try:
            self._scheduler.remove_job(job_id)
            logger.info(f"Cancelled job {job_id}")
            return True
        except Exception:
            return False

    def get_job_info(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a scheduled job."""
        if not self._scheduler:
            return None

        job = self._scheduler.get_job(job_id)
        if not job:
            return None

        return {
            "job_id": job.id,
            "name": job.name,
            "next_run_time": (
                job.next_run_time.isoformat() if job.next_run_time else None
            ),
            "trigger": str(job.trigger),
            "status": "scheduled" if job.next_run_time else "paused",
        }

    def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs."""
        if not self._scheduler:
            return []

        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append(
                {
                    "job_id": job.id,
                    "name": job.name,
                    "next_run_time": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                    "trigger": str(job.trigger),
                    "status": "scheduled" if job.next_run_time else "paused",
                }
            )
        return jobs

    def is_running(self) -> bool:
        """Check if the job runner is running."""
        return self._started and self._scheduler is not None


# =============================================================================
# IN-MEMORY IMPLEMENTATION
# =============================================================================


class InMemoryJobRunner(IJobRunner):
    """Simple in-memory job runner for local development.

    Uses Python threading for job scheduling. No external dependencies required.
    Jobs are not persisted across restarts.

    Features:
    - Zero external dependencies
    - Simple threading-based execution
    - Good for local development and testing
    - No persistence (jobs lost on restart)

    Limitations:
    - Single process only (not distributed)
    - Jobs not persisted
    - Less precise timing than APScheduler
    """

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._timers: Dict[str, threading.Timer] = {}
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the in-memory job runner."""
        if self._started:
            logger.warning("InMemory job runner already started")
            return

        self._started = True
        logger.info("InMemory job runner started")

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the in-memory job runner."""
        if not self._started:
            return

        with self._lock:
            # Cancel all timers
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._jobs.clear()
            self._started = False

        logger.info("InMemory job runner stopped")

    def _execute_task(
        self, job_id: str, task_func: Callable, args: tuple, kwargs: dict
    ):
        """Execute a task, handling both sync and async functions."""
        try:
            if asyncio.iscoroutinefunction(task_func):
                asyncio.run(task_func(*args, **kwargs))
            else:
                task_func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error executing job {job_id}: {e}", exc_info=True)

    def _schedule_next_run(self, job_id: str):
        """Schedule the next run for a recurring job."""
        with self._lock:
            if job_id not in self._jobs:
                return

            job = self._jobs[job_id]
            if job["type"] != "recurring":
                return

            # Create wrapper that executes and reschedules
            def run_and_reschedule():
                self._execute_task(job_id, job["task_func"], job["args"], job["kwargs"])
                # Schedule next run
                self._schedule_next_run(job_id)

            timer = threading.Timer(job["interval_seconds"], run_and_reschedule)
            timer.daemon = True
            timer.start()
            self._timers[job_id] = timer

            # Update next run time
            job["next_run_time"] = (
                datetime.now(timezone.utc).timestamp() + job["interval_seconds"]
            )

    def schedule_recurring(
        self,
        task_func: Callable,
        interval_seconds: int,
        job_id: Optional[str] = None,
        job_name: Optional[str] = None,
        args: tuple = (),
        kwargs: dict = None,
        replace_existing: bool = True,
    ) -> str:
        """Schedule a recurring job."""
        if not self._started:
            raise RuntimeError("Job runner not started. Call start() first.")

        kwargs = kwargs or {}
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        job_name = job_name or f"Recurring job {job_id}"

        with self._lock:
            # Cancel existing job if replace_existing
            if replace_existing and job_id in self._jobs:
                if job_id in self._timers:
                    self._timers[job_id].cancel()
                    del self._timers[job_id]

            self._jobs[job_id] = {
                "job_id": job_id,
                "name": job_name,
                "type": "recurring",
                "interval_seconds": interval_seconds,
                "task_func": task_func,
                "args": args,
                "kwargs": kwargs,
                "next_run_time": datetime.now(timezone.utc).timestamp()
                + interval_seconds,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

        # Schedule first run
        self._schedule_next_run(job_id)

        logger.info(
            f"Scheduled recurring job '{job_name}' ({job_id}) every {interval_seconds}s"
        )
        return job_id

    def schedule_once(
        self,
        task_func: Callable,
        run_at: datetime,
        job_id: Optional[str] = None,
        job_name: Optional[str] = None,
        args: tuple = (),
        kwargs: dict = None,
    ) -> str:
        """Schedule a one-time job."""
        if not self._started:
            raise RuntimeError("Job runner not started. Call start() first.")

        kwargs = kwargs or {}
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        job_name = job_name or f"One-time job {job_id}"

        # Calculate delay
        now = datetime.now(timezone.utc)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        delay_seconds = max(0, (run_at - now).total_seconds())

        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "name": job_name,
                "type": "once",
                "run_at": run_at.isoformat(),
                "task_func": task_func,
                "args": args,
                "kwargs": kwargs,
                "next_run_time": run_at.timestamp(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Create wrapper that executes and cleans up
            def run_and_cleanup():
                self._execute_task(job_id, task_func, args, kwargs)
                with self._lock:
                    self._jobs.pop(job_id, None)
                    self._timers.pop(job_id, None)

            timer = threading.Timer(delay_seconds, run_and_cleanup)
            timer.daemon = True
            timer.start()
            self._timers[job_id] = timer

        logger.info(f"Scheduled one-time job '{job_name}' ({job_id}) at {run_at}")
        return job_id

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a scheduled job."""
        with self._lock:
            if job_id not in self._jobs:
                return False

            if job_id in self._timers:
                self._timers[job_id].cancel()
                del self._timers[job_id]

            del self._jobs[job_id]
            logger.info(f"Cancelled job {job_id}")
            return True

    def get_job_info(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a scheduled job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None

            return {
                "job_id": job["job_id"],
                "name": job["name"],
                "type": job["type"],
                "next_run_time": (
                    datetime.fromtimestamp(
                        job["next_run_time"], tz=timezone.utc
                    ).isoformat()
                    if job.get("next_run_time")
                    else None
                ),
                "interval_seconds": job.get("interval_seconds"),
                "status": "scheduled",
            }

    def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs."""
        with self._lock:
            return [
                {
                    "job_id": job["job_id"],
                    "name": job["name"],
                    "type": job["type"],
                    "next_run_time": (
                        datetime.fromtimestamp(
                            job["next_run_time"], tz=timezone.utc
                        ).isoformat()
                        if job.get("next_run_time")
                        else None
                    ),
                    "status": "scheduled",
                }
                for job in self._jobs.values()
            ]

    def is_running(self) -> bool:
        """Check if the job runner is running."""
        return self._started


# =============================================================================
# FACTORY FUNCTION
# =============================================================================


def get_job_runner(runner_type: Optional[str] = None) -> IJobRunner:
    """Factory function to get the appropriate job runner.

    Args:
        runner_type: Type of job runner ("apscheduler" or "inmemory").
                     If None, reads from JOB_RUNNER_TYPE env var.
                     Defaults to "inmemory" for development, "apscheduler" for production.

    Returns:
        IJobRunner implementation instance

    Raises:
        ValueError: If unknown runner type specified
        ImportError: If APScheduler requested but not installed
    """
    import os

    if runner_type is None:
        runner_type = os.getenv("JOB_RUNNER_TYPE", "").lower()

    # Auto-detect based on environment if not specified
    if not runner_type:
        environment = os.getenv("ENVIRONMENT", "development").lower()
        if environment == "production":
            runner_type = "apscheduler" if APSCHEDULER_AVAILABLE else "inmemory"
        else:
            runner_type = "inmemory"

    if runner_type == "apscheduler":
        if not APSCHEDULER_AVAILABLE:
            logger.warning(
                "APScheduler not available, falling back to InMemoryJobRunner. "
                "Install with: pip install apscheduler"
            )
            return InMemoryJobRunner()
        return APSchedulerJobRunner()

    elif runner_type == "inmemory":
        return InMemoryJobRunner()

    else:
        raise ValueError(
            f"Unknown job runner type: {runner_type}. "
            f"Valid types: 'apscheduler', 'inmemory'"
        )


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "IJobRunner",
    "APSchedulerJobRunner",
    "InMemoryJobRunner",
    "get_job_runner",
    "APSCHEDULER_AVAILABLE",
]
