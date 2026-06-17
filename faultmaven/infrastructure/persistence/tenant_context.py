"""Tenant context for PostgreSQL Row-Level Security (ADR-010).

Carries the current organization id in a contextvar and applies it to a DB
session as ``SET LOCAL app.current_org_id``, so the RLS policies (migration 018)
scope reads to that organization. The contextvar **defaults to the Standalone
org**, so single-tenant deployments are always scoped correctly without any
per-request wiring; multi-tenant request handling sets it per request (a later
phase). On SQLite this is a no-op (Standalone has one organization and no RLS).

The session-scoping chokepoint is ``get_db_session`` (``database.py``), which
calls :func:`apply_tenant_context` on every session it yields — covering the web
request path, the sessionless repositories, and the jobs/CLI path uniformly.
"""

from contextvars import ContextVar
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.config.constants import STANDALONE_ORG_ID

# Default to the Standalone org so every single-tenant session is scoped without
# any caller having to set it. Multi-tenant request handling overrides per request.
_current_org_id: ContextVar[str] = ContextVar(
    "current_org_id", default=STANDALONE_ORG_ID
)


def set_current_org_id(organization_id: str) -> None:
    """Set the organization id for the current execution context (request/task)."""
    _current_org_id.set(organization_id)


def get_current_org_id() -> str:
    """Return the current context's organization id (defaults to the Standalone org)."""
    return _current_org_id.get()


async def apply_tenant_context(
    session: AsyncSession, organization_id: Optional[str] = None
) -> None:
    """Scope ``session`` to an organization for RLS via ``SET LOCAL`` (PostgreSQL).

    ``organization_id`` defaults to the current contextvar (the Standalone org in
    single-tenant). ``SET LOCAL`` is per-transaction, so this runs inside the
    session's transaction and is visible to RLS policies for the queries that
    follow. No-op on SQLite. The value is bound as a parameter (no injection).
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    org_id = organization_id if organization_id is not None else get_current_org_id()
    # `set_config(name, value, is_local=true)` is the transaction-local equivalent
    # of `SET LOCAL` that accepts a BOUND parameter — `SET LOCAL x = :p` is a
    # syntax error (SET does not take parameters). is_local=true scopes it to the
    # current transaction, exactly like SET LOCAL.
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": org_id},
    )
