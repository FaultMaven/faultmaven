"""Startup guard: the application's DB role must not be exempt from RLS.

ADR-010 forward-consolidation P2d. PostgreSQL exempts **superusers** and a
table's **owner** from row-level security. So if the application connects as
either, the tenant-isolation policies (migrations 018/023/030) are silently
bypassed and cross-tenant reads leak — the most dangerous possible failure for a
multi-tenant deployment, and one that no test on a superuser CI role would ever
surface.

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

# One round-trip: is the current role a superuser, and does it own any
# RLS-enabled table in a user schema? Either makes it exempt from RLS.
_ROLE_PRIVILEGE_QUERY = text("""
    SELECT
        current_user AS role_name,
        (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
            AS is_superuser,
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
    role_name: str, is_superuser: bool, owns_rls_table: bool
) -> None:
    """Fail closed if the role bypasses RLS. Pure decision logic (no I/O)."""
    if is_superuser or owns_rls_table:
        raise DeploymentCoherenceError(
            "Multi-tenant isolation requires the application to connect with a "
            "non-superuser, non-owner PostgreSQL role, but it is connected as "
            f"'{role_name}' (superuser={is_superuser}, "
            f"owns_rls_table={owns_rls_table}). PostgreSQL exempts superusers and "
            "table owners from row-level security, so the tenant-isolation "
            "policies (migrations 018/023/030) would be silently bypassed and "
            "cross-tenant reads would leak. Provision a dedicated non-owner, "
            "non-superuser role for the application; the schema must be owned and "
            "migrated by a separate role."
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
        DeploymentCoherenceError: If the app's role is a superuser or owns an
            RLS-enabled table (RLS would be bypassed).
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
        row.role_name, bool(row.is_superuser), bool(row.owns_rls_table)
    )
    logger.info(
        "RLS role guard passed: app DB role '%s' is non-superuser and owns no "
        "RLS-enabled table; tenant isolation is enforced.",
        row.role_name,
    )
