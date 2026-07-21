"""CLI runner for FaultMaven background jobs.

This module provides a minimal CLI interface to run background jobs
as standalone processes, without requiring a running web server.

Usage:
    python -m faultmaven.jobs.run <job_name>

Examples:
    python -m faultmaven.jobs.run case_cleanup

This enables operational neutrality - jobs can be scheduled by external
orchestrators (cron, Kubernetes CronJobs, Airflow, etc.) instead of
being tied to the web process lifecycle.

## Tenant scope (ADR-010 P3)

The web path binds the tenant context per request from the verified JWT; a job
has no request, so its tenant scope must be declared and enforced here. Every
job module declares ``JOB_TENANT_SCOPE``:

- ``tenant_neutral`` — touches no tenanted tables (e.g. filesystem sweeps).
  Runs identically in both tenancy modes.
- ``org`` — operates on one organization's tenanted rows. Under the
  multi-tenant provider the caller must pass ``--organization-id``; the runner
  binds it to the tenant contextvar (the RLS scope) and logs the binding.
  Under single-tenant the Standalone default already scopes correctly.
- ``cross_tenant`` — needs a view across ALL organizations (e.g. case_cleanup
  diffs the DB case-id set against ChromaDB collections, which are not
  org-partitioned). Under multi this **fails closed**: RLS scopes every DB
  transaction to the single org bound in the tenant context, so any run would
  see a partial id set and delete other tenants' data. Cross-tenant jobs
  cannot be scheduled in cloud until an audited maintenance path exists
  (tracked in #629).

``--organization-id`` is operator input: the runner binds whatever org id the
caller passes (and logs it). The scope model is a mis-scoping guard for
in-cluster maintenance, not an authorization boundary.

A job that declares no scope fails closed under multi — an undeclared job may
read tenanted tables, and the mis-scoped default (the never-seeded Standalone
org) turns "cleanup" into either a silent no-op or a cross-tenant delete.

The runner also runs the same boot gates as the web lifespan: the deployment
coherence gate and, under multi, the RLS role guard — a Kubernetes CronJob with
a misprovisioned (RLS-exempt) DB role must refuse to run, exactly like the API.
"""

import argparse
import asyncio
import importlib
import logging
import sys
from typing import Any, Dict, List, Optional

from faultmaven.config.deployment_coherence import DeploymentCoherenceError
from faultmaven.providers.tenancy.factory import TenancyConfigurationError

# Valid JOB_TENANT_SCOPE declarations (see module docstring).
TENANT_SCOPE_NEUTRAL = "tenant_neutral"
TENANT_SCOPE_ORG = "org"
TENANT_SCOPE_CROSS_TENANT = "cross_tenant"

_VALID_TENANT_SCOPES = frozenset(
    {TENANT_SCOPE_NEUTRAL, TENANT_SCOPE_ORG, TENANT_SCOPE_CROSS_TENANT}
)


class JobTenantScopeError(RuntimeError):
    """A job's tenant-scope requirements cannot be satisfied — refuse to run."""


# Available jobs registry
AVAILABLE_JOBS: Dict[str, str] = {
    "case_cleanup": "faultmaven.jobs.case_cleanup",
    "storage_cleanup": "faultmaven.modules.agent.jobs.storage_cleanup",
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


def _enforce_tenant_scope(
    module: Any,
    module_path: str,
    is_multi_tenant: bool,
    organization_id: Optional[str],
) -> Optional[str]:
    """Enforce the job's declared tenant scope; return the org id to bind.

    Returns the organization id to set on the tenant contextvar before the job
    runs, or ``None`` when the ambient default is already correct (single-tenant,
    or a tenant-neutral job).

    Raises:
        JobTenantScopeError: If the scope declaration is missing/invalid, if an
            ``org``-scoped job runs under multi without ``--organization-id``,
            or if a ``cross_tenant`` job runs under multi at all.
    """
    scope = getattr(module, "JOB_TENANT_SCOPE", None)

    if scope is not None and scope not in _VALID_TENANT_SCOPES:
        raise JobTenantScopeError(
            f"Job module {module_path} declares unknown JOB_TENANT_SCOPE="
            f"{scope!r} (expected one of {sorted(_VALID_TENANT_SCOPES)})."
        )

    if not is_multi_tenant:
        # Single-tenant: the contextvar default (Standalone org) scopes every
        # session correctly; --organization-id is not a scoping instruction here.
        return None

    if scope is None:
        raise JobTenantScopeError(
            f"Job module {module_path} declares no JOB_TENANT_SCOPE, so it "
            "cannot run under the multi-tenant provider: an undeclared job may "
            "read tenanted tables, and the default tenant context (the "
            "Standalone org, never seeded under multi) would scope those reads "
            "to zero rows. Declare JOB_TENANT_SCOPE in the job module."
        )

    if scope == TENANT_SCOPE_CROSS_TENANT:
        raise JobTenantScopeError(
            f"Job '{module_path}' requires a cross-tenant view, which the "
            "multi-tenant deployment cannot provide: row-level security "
            "scopes every DB transaction to the single organization bound in "
            "the tenant context, so the job would operate on a partial view "
            "of tenanted data (for cleanup jobs, that means deleting other "
            "tenants' resources). Do not schedule this job in cloud; an "
            "audited maintenance path is tracked in issue #629."
        )

    if scope == TENANT_SCOPE_ORG:
        if not organization_id:
            raise JobTenantScopeError(
                f"Job '{module_path}' is organization-scoped: under the "
                "multi-tenant provider it must be invoked with an explicit "
                "--organization-id (there is no ambient tenant on the CLI "
                "path, and the contextvar default is the never-seeded "
                "Standalone org)."
            )
        return organization_id

    return None  # tenant_neutral: no tenanted DB access, nothing to bind


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
        DeploymentCoherenceError: If the running config contradicts
            DEPLOYMENT_MODE, or (under multi) the DB role is RLS-exempt.
        JobTenantScopeError: If the job's tenant-scope requirements cannot be
            satisfied under the configured tenancy mode.
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

    # Same boot gate as the web lifespan (main.py): fail fast if the running
    # config contradicts DEPLOYMENT_MODE. A CronJob must not run against
    # tenanted data under a configuration the API itself would refuse to boot.
    from faultmaven.config.deployment_coherence import validate_deployment_coherence
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )

    try:
        validate_deployment_coherence(settings)
    except DeploymentCoherenceError:
        logger.critical("Refusing to run job: deployment configuration is incoherent")
        raise

    is_multi_tenant = requested_tenant_provider() == BUILTIN_MULTI

    # Enforce the job's declared tenant scope before any heavy initialization.
    try:
        bind_org_id = _enforce_tenant_scope(
            module,
            module_path,
            is_multi_tenant=is_multi_tenant,
            organization_id=kwargs.get("organization_id"),
        )
    except JobTenantScopeError:
        logger.critical("Refusing to run job: tenant-scope requirements not satisfied")
        raise

    # Initialize container (may already be initialized)
    try:
        await container.initialize()
        logger.info("DI container initialized")
    except TenancyConfigurationError:
        # Deliberate fail-closed refusal (e.g. TENANT_PROVIDER=multi outside
        # cloud) — never run a job against tenanted data with an invalid
        # tenancy configuration.
        logger.critical("Refusing to run job: tenancy configuration is invalid")
        raise
    except RuntimeError:
        # The container's production fail-fast (ENVIRONMENT=production raises
        # instead of degrading to a half-initialized container). The web
        # lifespan treats this as terminal; so does the jobs path — otherwise
        # a CronJob with broken infrastructure would report "skipped" (exit 0)
        # forever instead of failing loudly.
        logger.critical("Refusing to run job: container initialization failed")
        raise
    except Exception as e:
        logger.warning(f"Container initialization warning: {e}")

    # Same multi-tenant hard gate as the web lifespan: refuse to run if the
    # app's PostgreSQL role is exempt from RLS (superuser / BYPASSRLS / table
    # owner) — a misprovisioned CronJob would otherwise see every tenant's
    # rows unguarded. No-op in single-tenant mode and on SQLite.
    from faultmaven.infrastructure.persistence.rls_role_guard import (
        assert_app_db_role_enforces_rls,
    )

    try:
        await assert_app_db_role_enforces_rls(is_multi_tenant=is_multi_tenant)
    except DeploymentCoherenceError:
        logger.critical("Refusing to run job: app DB role is exempt from RLS")
        raise

    # Bind the job's tenant scope (multi + org-scoped jobs only). The engine's
    # per-transaction listener reads this contextvar, so every DB transaction
    # the job opens is RLS-scoped to the requested organization.
    if bind_org_id is not None:
        from faultmaven.config.tenant_context import set_current_org_id

        set_current_org_id(bind_org_id)
        logger.info(
            "Tenant scope bound: job '%s' runs scoped to organization_id=%s",
            job_name,
            bind_org_id,
        )

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
        help=(
            "Organization ID for organization-scoped jobs. Required under the "
            "multi-tenant provider for jobs with JOB_TENANT_SCOPE='org'; the "
            "runner binds it to the tenant context so all DB access is "
            "RLS-scoped to that organization."
        ),
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
