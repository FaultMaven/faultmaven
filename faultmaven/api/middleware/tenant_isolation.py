"""Tenant isolation helper: set the PostgreSQL RLS context for a session.

RLS policies (migration 018) enforce tenant isolation by filtering on
``current_setting('app.current_org_id')``. That context is normally applied
automatically **per transaction** by the engine ``begin`` listener (see
``infrastructure/persistence/database.py``), which reads the current organization
from the request/task contextvar. This helper sets it **explicitly** for a given
session — e.g. to scope one session to a specific organization.

On SQLite (Standalone) this is a no-op — RLS is PostgreSQL-only and Standalone has
exactly one organization.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_tenant_context(session: AsyncSession, organization_id: str) -> None:
    """Set ``app.current_org_id`` for ``session`` (PostgreSQL RLS).

    ``set_config(..., is_local=true)`` is the transaction-local equivalent of
    ``SET LOCAL`` that accepts a bound parameter (``SET LOCAL x = :p`` is a syntax
    error — ``SET`` takes no parameters). No-op on SQLite. The value is bound to
    prevent injection.

    Args:
        session: Active async SQLAlchemy session.
        organization_id: The tenant identifier.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        # No RLS on SQLite — Standalone has one organization.
        return
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": organization_id},
    )
