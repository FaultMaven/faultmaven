"""Background jobs for FaultMaven.

This package contains background job implementations for various
asynchronous processing tasks.

Jobs can be run standalone via CLI:
    python -m faultmaven.jobs.run --list      # List available jobs
    python -m faultmaven.jobs.run case_cleanup  # Run case cleanup job

This enables operational neutrality - jobs can be scheduled by external
orchestrators (cron, Kubernetes CronJobs, Airflow, etc.) instead of
being tied to the web process lifecycle.
"""

# Job modules are imported dynamically by the runner to avoid circular imports
# See faultmaven.jobs.run for the CLI interface

__all__: list[str] = []
