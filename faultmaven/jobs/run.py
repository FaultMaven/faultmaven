"""CLI runner for FaultMaven background jobs.

This module provides a minimal CLI interface to run background jobs
as standalone processes, without requiring a running web server.

Usage:
    python -m faultmaven.jobs.run <job_name>

Examples:
    python -m faultmaven.jobs.run case_cleanup
    python -m faultmaven.jobs.run knowledge_indexing --organization-id org_123

This enables operational neutrality - jobs can be scheduled by external
orchestrators (cron, Kubernetes CronJobs, Airflow, etc.) instead of
being tied to the web process lifecycle.
"""

import argparse
import asyncio
import importlib
import logging
import sys
from typing import Any, Dict, List, Optional

# Available jobs registry
AVAILABLE_JOBS: Dict[str, str] = {
    "case_cleanup": "faultmaven.jobs.case_cleanup",
    "knowledge_indexing": "faultmaven.jobs.knowledge_indexing_job",
}


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI execution."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def list_available_jobs() -> List[Dict[str, str]]:
    """Get list of available jobs with descriptions."""
    jobs = []
    for job_name, module_path in AVAILABLE_JOBS.items():
        try:
            module = importlib.import_module(module_path)
            description = getattr(module, "JOB_DESCRIPTION", "No description available")
            jobs.append({"name": job_name, "description": description})
        except ImportError:
            jobs.append({"name": job_name, "description": "(module not found)"})
    return jobs


async def run_job(
    job_name: str,
    verbose: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run a job by name.

    Args:
        job_name: Name of the job to run
        verbose: Enable verbose logging
        **kwargs: Additional arguments to pass to the job

    Returns:
        Job result dictionary

    Raises:
        ValueError: If job_name is not found
        ImportError: If job module cannot be imported
    """
    setup_logging(verbose)
    logger = logging.getLogger(__name__)

    if job_name not in AVAILABLE_JOBS:
        available = ", ".join(AVAILABLE_JOBS.keys())
        raise ValueError(f"Unknown job: {job_name}. Available jobs: {available}")

    module_path = AVAILABLE_JOBS[job_name]
    logger.info(f"Loading job module: {module_path}")

    # Import job module
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        logger.error(f"Failed to import job module {module_path}: {e}")
        raise

    # Get run function
    if not hasattr(module, "run"):
        raise AttributeError(f"Job module {module_path} has no 'run' function")

    run_func = module.run

    # Initialize settings and container
    logger.info("Initializing settings and DI container...")

    from faultmaven.config.settings import get_settings
    from faultmaven.container import container

    settings = get_settings()

    # Initialize container (may already be initialized)
    try:
        await container.initialize()
        logger.info("DI container initialized")
    except Exception as e:
        logger.warning(f"Container initialization warning: {e}")

    # Run the job
    logger.info(f"Running job: {job_name}")
    try:
        result = await run_func(settings=settings, container=container, **kwargs)
        logger.info(f"Job completed: {result}")
        return result
    except Exception as e:
        logger.error(f"Job failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}


def main(args: Optional[List[str]] = None) -> int:
    """CLI entry point.

    Args:
        args: Command line arguments (defaults to sys.argv)

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    # Load environment variables at function level, not module level
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Run FaultMaven background jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m faultmaven.jobs.run case_cleanup
  python -m faultmaven.jobs.run case_cleanup --verbose
  python -m faultmaven.jobs.run --list
        """,
    )

    parser.add_argument(
        "job_name",
        nargs="?",
        help="Name of the job to run",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available jobs",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--organization-id",
        type=str,
        help="Organization ID for organization-scoped jobs",
    )

    parsed_args = parser.parse_args(args)

    # Handle --list
    if parsed_args.list:
        print("Available jobs:")
        for job in list_available_jobs():
            print(f"  {job['name']}: {job['description']}")
        return 0

    # Require job_name if not listing
    if not parsed_args.job_name:
        parser.print_help()
        return 1

    # Build kwargs from parsed args
    kwargs: Dict[str, Any] = {}
    if parsed_args.organization_id:
        kwargs["organization_id"] = parsed_args.organization_id

    # Run the job
    try:
        result = asyncio.run(
            run_job(
                job_name=parsed_args.job_name,
                verbose=parsed_args.verbose,
                **kwargs,
            )
        )

        # Determine exit code based on result
        status = result.get("status", "unknown")
        if status in ("completed", "skipped"):
            return 0
        else:
            return 1

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
