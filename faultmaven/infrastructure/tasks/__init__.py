"""Background tasks for FaultMaven infrastructure.

This package provides:
- Job runner abstraction for scheduling background tasks
- Concrete implementations: APScheduler, InMemory
- Case cleanup scheduled task

Configuration:
    JOB_RUNNER_TYPE=apscheduler  # Production (requires apscheduler package)
    JOB_RUNNER_TYPE=inmemory     # Development (no external deps)

Usage:
    from faultmaven.infrastructure.tasks import get_job_runner

    runner = get_job_runner()
    runner.start()
    runner.schedule_recurring(my_task, interval_seconds=3600)
"""

from faultmaven.infrastructure.tasks.case_cleanup import (
    cleanup_orphaned_collections_task,
    start_case_cleanup_scheduler,
    stop_case_cleanup_scheduler,
)
from faultmaven.infrastructure.tasks.job_runners import (
    APSCHEDULER_AVAILABLE,
    APSchedulerJobRunner,
    InMemoryJobRunner,
    get_job_runner,
)

__all__ = [
    "start_case_cleanup_scheduler",
    "stop_case_cleanup_scheduler",
    "cleanup_orphaned_collections_task",
    # Job runner abstraction
    "APSchedulerJobRunner",
    "InMemoryJobRunner",
    "get_job_runner",
    "APSCHEDULER_AVAILABLE",
]
