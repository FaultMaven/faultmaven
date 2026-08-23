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
  org-partitioned). Under multi this **fails closed by default**: RLS scopes
  every DB transaction to the single org bound in the tenant context, so a
  run under the regular app role would see a partial id set and delete other
  tenants' data. The audited maintenance path (ADR-010 / #629) is the ONLY
  way to run one in cloud: invoke with ``--cross-tenant-maintenance`` and
  connect as the dedicated maintenance role (``faultmaven_maintenance`` —
  BYPASSRLS, non-superuser, non-owner; the runner probe-verifies this and
  refuses anything else). Each run emits an audit log line with the job, DB
  role, and arguments. See docs/operations/evidence-job-scheduling.md.

``--organization-id`` is operator input: the runner binds whatever org id the
caller passes (and logs it). The scope model is a mis-scoping guard for
in-cluster maintenance, not an authorization boundary.

A job that declares no scope fails closed under multi — an undeclared job may
read tenanted tables, and the mis-scoped default (the never-seeded Standalone
org) turns "cleanup" into either a silent no-op or a cross-tenant delete.

The runner also runs the same boot gates as the web lifespan: the deployment
coherence gate and, under multi, the RLS role guard — a Kubernetes CronJob with
a misprovisioned (RLS-exempt) DB role must refuse to run, exactly like the API.

## Runner-global vs job-specific flags

Most flags configure the *runner* and apply to whatever job is named
(``--verbose``, ``--organization-id``, ``--cross-tenant-maintenance``). A few
configure ONE job: ``--dry-run``/``--no-dry-run`` and ``--ttl-hours`` belong to
``storage_cleanup``. A job accepts a job-specific flag only by declaring it as
an explicit parameter of its ``run()``; passing one to a job that merely
absorbs ``**kwargs`` is refused (``JobArgumentError``) rather than delivered as
a stray kwarg nothing reads.

Job-specific flags are **three-valued on purpose**: omitting one means "defer
to settings", which is not the same as passing the setting's current value.
``storage_cleanup --verbose`` — what the deployed CronJob runs — reaches
``run()`` with neither kwarg, so ``ORPHAN_CLEANUP_DRY_RUN`` /
``ORPHAN_FILE_TTL_HOURS`` decide it, exactly as they did before these flags
existed. And ``--no-dry-run`` is a lever, not an enabler: it asks for deletion
but cannot grant it, because ``ORPHAN_CLEANUP_ENABLED=false`` still refuses the
run (``status="skipped"``).
"""

import argparse
import asyncio
import importlib
import inspect
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


class JobArgumentError(RuntimeError):
    """A job was invoked with an argument it does not accept — refuse to run."""


# Arguments that configure ONE job rather than the runner, mapped to the CLI
# spelling used in refusals. Every runner-global kwarg is absorbed by every
# job's ``**kwargs``; a job-specific one must not be, or a flag meant for
# another job would be swallowed silently. See the module docstring.
JOB_SPECIFIC_FLAGS: Dict[str, str] = {
    "dry_run": "--dry-run/--no-dry-run",
    "ttl_hours": "--ttl-hours",
}


# Available jobs registry
AVAILABLE_JOBS: Dict[str, str] = {
    "case_cleanup": "faultmaven.jobs.case_cleanup",
    "kb_seed": "faultmaven.jobs.kb_seed",
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
    cross_tenant_maintenance: bool = False,
) -> Optional[str]:
    """Enforce the job's declared tenant scope; return the org id to bind.

    Returns the organization id to set on the tenant contextvar before the job
    runs, or ``None`` when the ambient default is already correct (single-tenant,
    or a tenant-neutral job) or when the maintenance path bypasses RLS entirely.

    ``cross_tenant_maintenance`` is the operator's explicit acknowledgment
    (``--cross-tenant-maintenance``) that this run uses the audited maintenance
    path. It is only meaningful for ``cross_tenant`` jobs under the multi-tenant
    provider; anywhere else it is a configuration error and fails closed (a
    manifest carrying the flag against the wrong job or the wrong deployment
    mode is drift worth catching, not ignoring).

    Raises:
        JobTenantScopeError: If the scope declaration is missing/invalid, if an
            ``org``-scoped job runs under multi without ``--organization-id``,
            if a ``cross_tenant`` job runs under multi without the maintenance
            acknowledgment, or if the acknowledgment is passed where it does
            not apply.
    """
    scope = getattr(module, "JOB_TENANT_SCOPE", None)

    if scope is not None and scope not in _VALID_TENANT_SCOPES:
        raise JobTenantScopeError(
            f"Job module {module_path} declares unknown JOB_TENANT_SCOPE="
            f"{scope!r} (expected one of {sorted(_VALID_TENANT_SCOPES)})."
        )

    if not is_multi_tenant:
        if cross_tenant_maintenance:
            raise JobTenantScopeError(
                "--cross-tenant-maintenance only applies to cross-tenant jobs "
                "under the multi-tenant provider; this deployment is "
                "single-tenant, where every job already sees the one implicit "
                "org. Remove the flag (it usually indicates a manifest copied "
                "from a cloud deployment)."
            )
        # Single-tenant: the contextvar default (Standalone org) scopes every
        # session correctly; --organization-id is not a scoping instruction here.
        return None

    if cross_tenant_maintenance and organization_id:
        raise JobTenantScopeError(
            "--cross-tenant-maintenance and --organization-id are mutually "
            "exclusive: the maintenance path bypasses RLS entirely, so an org "
            "id could not scope anything and passing one indicates a confused "
            "manifest. Drop one of the two."
        )

    if cross_tenant_maintenance and scope != TENANT_SCOPE_CROSS_TENANT:
        raise JobTenantScopeError(
            f"--cross-tenant-maintenance was passed but job '{module_path}' "
            f"declares JOB_TENANT_SCOPE={scope!r}. The maintenance role "
            "bypasses RLS, so running a tenant-scoped or undeclared job under "
            "it would expose every tenant's rows to a job that expects a "
            "scoped view. Run this job with the regular app role instead."
        )

    if scope is None:
        raise JobTenantScopeError(
            f"Job module {module_path} declares no JOB_TENANT_SCOPE, so it "
            "cannot run under the multi-tenant provider: an undeclared job may "
            "read tenanted tables, and the default tenant context (the "
            "Standalone org, never seeded under multi) would scope those reads "
            "to zero rows. Declare JOB_TENANT_SCOPE in the job module."
        )

    if scope == TENANT_SCOPE_CROSS_TENANT:
        if not cross_tenant_maintenance:
            raise JobTenantScopeError(
                f"Job '{module_path}' requires a cross-tenant view, which the "
                "multi-tenant deployment refuses by default: row-level "
                "security scopes every DB transaction to the single "
                "organization bound in the tenant context, so the job would "
                "operate on a partial view of tenanted data (for cleanup "
                "jobs, that means deleting other tenants' resources). To run "
                "it, use the audited maintenance path: pass "
                "--cross-tenant-maintenance and connect as the dedicated "
                "maintenance DB role (BYPASSRLS, non-superuser, non-owner) — "
                "see docs/operations/evidence-job-scheduling.md and issue "
                "#629."
            )
        # Maintenance path: RLS is bypassed by role, so there is no org to
        # bind — the contextvar stays at its default and is irrelevant. The
        # role posture is verified by assert_maintenance_db_role_posture
        # after the container (and its engine) is up.
        return None

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


def _reject_unsupported_job_flags(
    module: Any,
    job_name: str,
    kwargs: Dict[str, Any],
) -> None:
    """Refuse job-specific arguments the named job does not declare.

    Runner-global kwargs are meant to reach every job and every ``run()``
    absorbs them via ``**kwargs``. A job-specific one must not travel that
    way: a job that does not declare ``dry_run`` would swallow ``--dry-run``
    and then run in whatever mode its settings say, reporting success. The
    declaration that counts is an explicit parameter on ``run()`` — absorbing
    ``**kwargs`` is not accepting the flag.

    Raises:
        JobArgumentError: If a job-specific argument was passed to a job whose
            ``run()`` has no such parameter.
    """
    try:
        params = inspect.signature(module.run).parameters
    except (TypeError, ValueError):  # pragma: no cover — non-introspectable run()
        params = {}

    for name, spelling in JOB_SPECIFIC_FLAGS.items():
        if name not in kwargs:
            continue
        param = params.get(name)
        if param is None or param.kind is inspect.Parameter.VAR_KEYWORD:
            accepted = sorted(
                flag
                for flag_name, flag in JOB_SPECIFIC_FLAGS.items()
                if flag_name in params
            )
            raise JobArgumentError(
                f"Job '{job_name}' does not accept {spelling}: its run() "
                f"declares no '{name}' parameter, so the value would be "
                "delivered as a kwarg nothing reads. Job-specific flags "
                f"accepted by this job: {', '.join(accepted) or 'none'}."
            )


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
        JobArgumentError: If a job-specific argument (see JOB_SPECIFIC_FLAGS)
            was passed to a job whose run() does not declare it.
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

    # Refuse a job-specific flag aimed at the wrong job before doing any work.
    _reject_unsupported_job_flags(module, job_name, kwargs)

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
    cross_tenant_maintenance = bool(kwargs.pop("cross_tenant_maintenance", False))

    # Enforce the job's declared tenant scope before any heavy initialization.
    try:
        bind_org_id = _enforce_tenant_scope(
            module,
            module_path,
            is_multi_tenant=is_multi_tenant,
            organization_id=kwargs.get("organization_id"),
            cross_tenant_maintenance=cross_tenant_maintenance,
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

    # DB-role gate. Two mutually exclusive postures under multi:
    # - Regular jobs (org / tenant_neutral): same hard gate as the web
    #   lifespan — refuse if the app's PostgreSQL role is exempt from RLS
    #   (superuser / BYPASSRLS / table owner); a misprovisioned CronJob would
    #   otherwise see every tenant's rows unguarded.
    # - The audited maintenance path (cross_tenant + --cross-tenant-maintenance,
    #   already validated by _enforce_tenant_scope): the INVERSE — the role
    #   must hold BYPASSRLS (an RLS-scoped partial view is the delete-other-
    #   tenants hazard) while still being non-superuser and non-owner.
    # Both are no-ops in single-tenant mode; the app gate is also a no-op on
    # SQLite, while the maintenance gate fails closed off PostgreSQL.
    from faultmaven.infrastructure.persistence.rls_role_guard import (
        assert_app_db_role_enforces_rls,
        assert_maintenance_db_role_posture,
    )

    if is_multi_tenant and cross_tenant_maintenance:
        try:
            maintenance_role = await assert_maintenance_db_role_posture()
        except DeploymentCoherenceError:
            logger.critical(
                "Refusing to run job: DB role does not fit the maintenance "
                "posture (BYPASSRLS + non-superuser + non-owner)"
            )
            raise
        # The audit record for the run: who (DB role), what (job + args), and
        # under which posture. Emitted at WARNING so it survives quiet logging
        # configs — a cross-tenant sweep should never be invisible. Logged
        # BEFORE the job runs so a crashing job still leaves the trail.
        logger.warning(
            "AUDIT cross-tenant maintenance run: job=%s db_role=%s args=%s "
            "(RLS bypassed by dedicated maintenance role; "
            "--cross-tenant-maintenance acknowledged)",
            job_name,
            maintenance_role,
            {k: v for k, v in kwargs.items()},
        )
    else:
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


def _ttl_hours_arg(value: str) -> int:
    """Parse ``--ttl-hours``, refusing anything the setting itself refuses.

    ``ORPHAN_FILE_TTL_HOURS`` is bounded by pydantic (``ge``/``le``); a CLI
    override that skipped that bound would be a path around a field
    constraint — ``--ttl-hours 0`` deletes files of any age, in-flight uploads
    included. The bounds are read off the settings field rather than restated
    here, so the two cannot drift apart.
    """
    from faultmaven.modules.agent.jobs.storage_cleanup import validate_ttl_hours

    try:
        hours = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")

    try:
        return validate_ttl_hours(hours)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (exposed so tests can check what actually parses)."""
    parser = argparse.ArgumentParser(
        prog="python -m faultmaven.jobs.run",
        description="Run FaultMaven background jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m faultmaven.jobs.run case_cleanup
  python -m faultmaven.jobs.run case_cleanup --verbose
  python -m faultmaven.jobs.run --list
  python -m faultmaven.jobs.run storage_cleanup --dry-run
  python -m faultmaven.jobs.run storage_cleanup --ttl-hours 72
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
    parser.add_argument(
        "--cross-tenant-maintenance",
        action="store_true",
        help=(
            "Run a JOB_TENANT_SCOPE='cross_tenant' job on the audited "
            "maintenance path (multi-tenant deployments only). Requires the "
            "process to connect as the dedicated maintenance DB role "
            "(BYPASSRLS, non-superuser, non-owner) — the runner verifies the "
            "role and refuses anything else, and every run is audit-logged. "
            "See docs/operations/evidence-job-scheduling.md."
        ),
    )

    # Job-specific flags (storage_cleanup). Both default to None — "defer to
    # settings" — which is deliberately distinct from passing the setting's
    # current value, so an invocation that omits them behaves exactly as it
    # did before they existed. Passing one to a job that does not declare it
    # is refused by run_job rather than delivered as a stray kwarg.
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "storage_cleanup only. --dry-run logs 'would delete' without "
            "deleting; --no-dry-run asks for real deletion. Omit to defer to "
            "ORPHAN_CLEANUP_DRY_RUN. --no-dry-run is not an enabler: with "
            "ORPHAN_CLEANUP_ENABLED=false the run is still refused "
            "(status='skipped')."
        ),
    )
    parser.add_argument(
        "--ttl-hours",
        type=_ttl_hours_arg,
        default=None,
        metavar="HOURS",
        help=(
            "storage_cleanup only. Age threshold for orphan deletion, for "
            "this run only; omit to defer to ORPHAN_FILE_TTL_HOURS. Bounded "
            "by the same range as that setting."
        ),
    )

    return parser


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

    parser = build_parser()
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
    if parsed_args.cross_tenant_maintenance:
        kwargs["cross_tenant_maintenance"] = True
    # Only a flag that was actually given becomes a kwarg: absent means the
    # job defers to settings, which is not the same as passing their value.
    if parsed_args.dry_run is not None:
        kwargs["dry_run"] = parsed_args.dry_run
    if parsed_args.ttl_hours is not None:
        kwargs["ttl_hours"] = parsed_args.ttl_hours

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
