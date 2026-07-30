"""Startup guard: the application's DB role must not be exempt from RLS.

ADR-010 forward-consolidation P2d. PostgreSQL exempts three kinds of role from
row-level security: **superusers**, roles with the **BYPASSRLS** attribute, and a
table's **owner**. So if the application connects as any of them, the
tenant-isolation policies (migrations 018/023/030) are silently bypassed and
cross-tenant reads leak — the most dangerous possible failure for a multi-tenant
deployment, and one that no test on a superuser CI role would ever surface.

This guard runs once at startup and **fails closed** (raising
:class:`DeploymentCoherenceError`, the same boot-refusal signal the deployment
coherence gate uses) when multi-tenant is enabled on PostgreSQL and the app's
role is RLS-exempt. It is a no-op in single-tenant mode (no RLS to bypass) and on
SQLite (Standalone has no RLS).

The production posture it enforces: the schema is owned and migrated by a
separate role, and the app connects as a dedicated non-owner, non-superuser role
(provisioned by faultmaven-enterprise-infra).
"""

import logging

from sqlalchemy import text

from faultmaven.config.deployment_coherence import DeploymentCoherenceError

logger = logging.getLogger(__name__)

# One round-trip: is the current role a superuser, does it hold the BYPASSRLS
# attribute, and does it own any RLS-enabled table in a user schema? Any of the
# three makes it exempt from RLS.
_ROLE_PRIVILEGE_QUERY = text("""
    SELECT
        current_user AS role_name,
        (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
            AS is_superuser,
        (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)
            AS has_bypassrls,
        EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND c.relrowsecurity
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND pg_catalog.pg_get_userbyid(c.relowner) = current_user
        ) AS owns_rls_table
    """)


def _raise_if_rls_exempt(
    role_name: str,
    is_superuser: bool,
    has_bypassrls: bool,
    owns_rls_table: bool,
) -> None:
    """Fail closed if the role bypasses RLS. Pure decision logic (no I/O)."""
    if is_superuser or has_bypassrls or owns_rls_table:
        raise DeploymentCoherenceError(
            "Multi-tenant isolation requires the application to connect with a "
            "non-superuser, non-BYPASSRLS, non-owner PostgreSQL role, but it is "
            f"connected as '{role_name}' (superuser={is_superuser}, "
            f"bypassrls={has_bypassrls}, owns_rls_table={owns_rls_table}). "
            "PostgreSQL exempts superusers, roles with the BYPASSRLS attribute, "
            "and table owners from row-level security, so the tenant-isolation "
            "policies (migrations 018/023/030) would be silently bypassed and "
            "cross-tenant reads would leak. Provision a dedicated non-owner "
            "role without SUPERUSER or BYPASSRLS for the application; the schema "
            "must be owned and migrated by a separate role."
        )


async def assert_app_db_role_enforces_rls(
    *, is_multi_tenant: bool, engine=None
) -> None:
    """Refuse to boot if a multi-tenant PG deployment's role is RLS-exempt.

    Args:
        is_multi_tenant: Whether the deployment runs the multi-tenant provider
            (resolved by the caller from ``requested_tenant_provider()`` so this
            guard and the coherence gate share one predicate). No-op when False.
        engine: Async engine to probe; defaults to the shared engine. Injected in
            tests.

    Raises:
        DeploymentCoherenceError: If the app's role is a superuser, holds the
            BYPASSRLS attribute, or owns an RLS-enabled table (RLS would be
            bypassed).
    """
    if not is_multi_tenant:
        return  # single-tenant: no RLS to bypass

    if engine is None:
        from faultmaven.infrastructure.persistence.database import get_engine

        engine = get_engine()

    if engine.dialect.name != "postgresql":
        return  # SQLite (Standalone) has no RLS

    async with engine.connect() as conn:
        row = (await conn.execute(_ROLE_PRIVILEGE_QUERY)).first()

    _raise_if_rls_exempt(
        row.role_name,
        bool(row.is_superuser),
        bool(row.has_bypassrls),
        bool(row.owns_rls_table),
    )
    logger.info(
        "RLS role guard passed: app DB role '%s' is non-superuser, non-BYPASSRLS "
        "and owns no RLS-enabled table; tenant isolation is enforced.",
        row.role_name,
    )


def _raise_unless_maintenance_posture(
    role_name: str,
    is_superuser: bool,
    has_bypassrls: bool,
    owns_rls_table: bool,
) -> None:
    """Fail closed unless the role fits the maintenance posture (no I/O).

    The maintenance posture is the deliberate INVERSE of the app posture on
    exactly one axis: the role MUST hold BYPASSRLS (a cross-tenant job run
    with an RLS-scoped role sees a partial, single-org view of tenanted data —
    for cleanup jobs that means classifying other tenants' resources as
    orphaned and deleting them). Superuser and table ownership stay forbidden:
    they grant DDL/catalog power far beyond what a maintenance job needs, and
    ownership would let a compromised job rewrite the schema it sweeps.
    """
    problems = []
    if not has_bypassrls:
        problems.append(
            "it lacks the BYPASSRLS attribute (RLS would scope the job to a "
            "single org — a partial view that turns cross-tenant cleanup into "
            "deleting other tenants' resources)"
        )
    if is_superuser:
        problems.append("it is a superuser (far more privilege than a sweep needs)")
    if owns_rls_table:
        problems.append(
            "it owns RLS-enabled tables (the maintenance role must not be able "
            "to alter the schema it sweeps)"
        )
    if problems:
        raise DeploymentCoherenceError(
            "Cross-tenant maintenance requires a dedicated PostgreSQL role with "
            "BYPASSRLS but neither SUPERUSER nor table ownership "
            "(faultmaven_maintenance, provisioned by faultmaven-enterprise-infra), "
            f"but the job is connected as '{role_name}': " + "; ".join(problems) + "."
        )


async def assert_maintenance_db_role_posture(*, engine=None) -> str:
    """Refuse to run a cross-tenant maintenance job on the wrong DB role.

    Counterpart of :func:`assert_app_db_role_enforces_rls` for the audited
    maintenance path (ADR-010 / issue #629): a job whose tenant scope is
    ``cross_tenant`` may only run under the multi-tenant provider when the
    connected role deliberately bypasses RLS (BYPASSRLS, non-superuser,
    non-owner). Callers gate on the multi-tenant predicate before calling —
    single-tenant deployments have no RLS and no maintenance role.

    Args:
        engine: Async engine to probe; defaults to the shared engine. Injected
            in tests.

    Returns:
        The verified DB role name — the caller interpolates it into the
        audit log line so every maintenance run records WHO ran it.

    Raises:
        DeploymentCoherenceError: If the connected role lacks BYPASSRLS, is a
            superuser, or owns an RLS-enabled table — or if the deployment is
            not on PostgreSQL at all (a multi-tenant deployment on SQLite is
            already refused by the coherence gate; failing closed here keeps
            this guard self-contained).
    """
    if engine is None:
        from faultmaven.infrastructure.persistence.database import get_engine

        engine = get_engine()

    if engine.dialect.name != "postgresql":
        raise DeploymentCoherenceError(
            "Cross-tenant maintenance is only defined for multi-tenant "
            "PostgreSQL deployments (RLS + a dedicated BYPASSRLS role); the "
            f"connected database dialect is '{engine.dialect.name}'."
        )

    async with engine.connect() as conn:
        row = (await conn.execute(_ROLE_PRIVILEGE_QUERY)).first()

    _raise_unless_maintenance_posture(
        row.role_name,
        bool(row.is_superuser),
        bool(row.has_bypassrls),
        bool(row.owns_rls_table),
    )
    logger.info(
        "Maintenance role guard passed: DB role '%s' holds BYPASSRLS and is "
        "neither superuser nor owner; cross-tenant maintenance may proceed.",
        row.role_name,
    )
    return row.role_name


def _raise_if_rls_scoped(
    role_name: str,
    is_superuser: bool,
    has_bypassrls: bool,
    owns_rls_table: bool,
) -> None:
    """Fail closed if the role is SUBJECT to RLS (pure decision logic, no I/O).

    The exact inverse of :func:`_raise_if_rls_exempt`, and deliberately so.
    Tenant provisioning writes the *first* rows of a tenant that does not exist
    yet — an organization row whose own policy is what would have to admit it,
    and an enterprise row in a table the application role has no business
    writing at all. A role that RLS scopes cannot do that: the writes are
    filtered or refused, and under FORCE ROW LEVEL SECURITY the failure can be
    a silent zero-row result rather than an error.

    The role that *can* is the schema owner (or any other RLS-exempt role).
    Being exempt is the qualification here, which is why the app-role posture —
    non-superuser, no BYPASSRLS, owns no RLS-enabled table — is precisely the
    refusal condition.
    """
    if not (is_superuser or has_bypassrls or owns_rls_table):
        raise DeploymentCoherenceError(
            "Tenant provisioning must run with the RLS-owning database role "
            "(the role that owns and migrates the schema), but this connection "
            f"is using '{role_name}', which row-level security applies to "
            "(superuser=False, bypassrls=False, owns_rls_table=False) — the "
            "posture of the limited *application* role. Provisioning writes the "
            "first rows of a tenant that does not exist yet, so RLS filters or "
            "refuses them, and under FORCE ROW LEVEL SECURITY the failure can "
            "be a silent zero-row result rather than an error. Nothing was "
            "written. Note that `kubectl exec` into the API pod inherits the "
            "pod's DATABASE_URL, which is the application role by design "
            "(main.py asserts it) — pass the owner DSN explicitly:\n"
            "    kubectl exec -it deploy/faultmaven-api -- \\\n"
            "      env DATABASE_URL='postgresql+asyncpg://faultmaven:...@host/faultmaven' \\\n"
            "      fm-provision-sso-org --name ... --slug ... --workos-org-id org_..."
        )


async def assert_provisioning_db_role_bypasses_rls(*, engine=None) -> str | None:
    """Refuse to provision a tenant on a role that row-level security scopes.

    Third posture alongside :func:`assert_app_db_role_enforces_rls` (the app
    must be scoped) and :func:`assert_maintenance_db_role_posture` (a sweep must
    hold BYPASSRLS): the tenant-provisioning path must be RLS-*exempt*, because
    it writes rows for a tenant that has no policy admitting it yet.

    Args:
        engine: Async engine to probe; defaults to the shared engine. Injected
            in tests.

    Returns:
        The verified DB role name, so the caller can show the operator which
        role the writes will be attributed to — or ``None`` on a non-PostgreSQL
        dialect, where there is no RLS to be scoped by and nothing to verify.

    Raises:
        DeploymentCoherenceError: If the connected role is subject to RLS.
    """
    if engine is None:
        from faultmaven.infrastructure.persistence.database import get_engine

        engine = get_engine()

    if engine.dialect.name != "postgresql":
        # SQLite (Standalone) has no row-level security, so no role can be
        # scoped by it. Nothing to verify rather than nothing to enforce.
        return None

    async with engine.connect() as conn:
        row = (await conn.execute(_ROLE_PRIVILEGE_QUERY)).first()

    _raise_if_rls_scoped(
        row.role_name,
        bool(row.is_superuser),
        bool(row.has_bypassrls),
        bool(row.owns_rls_table),
    )
    logger.info(
        "Provisioning role guard passed: DB role '%s' is not scoped by RLS; "
        "tenant rows can be written outside any existing tenant policy.",
        row.role_name,
    )
    return row.role_name
