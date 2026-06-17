"""Tenant isolation middleware: set PostgreSQL RLS context per request.

After authentication resolves the user's organization_id, this middleware
sets the per-connection PostgreSQL setting ``app.current_org_id`` so that
RLS policies (created in Phase 8 of the storage redesign) can enforce
tenant isolation at the database level.

On SQLite (local deployment), this is a no-op — RLS is PostgreSQL-only,
and Local Deployment has exactly one organization, so the practical risk
is zero.

Wire-in (deferred):
    The schema-side RLS policies are the primary deliverable of Phase 8.
    Wiring this helper into the request lifecycle (e.g., calling it from
    ``get_async_db_session`` after authentication resolves the org_id)
    is tracked as a follow-up. The helper is dialect-aware and safe to
    call from any context.

Example wire-in pattern (for reference, not yet active)::

    async def get_async_db_session(
        user: UserDTO = Depends(require_authenticated_user),
    ) -> AsyncGenerator[AsyncSession, None]:
        async with get_db_session() as session:
            await set_tenant_context(session, user.organization_id)
            yield session

Per ``deployment-schema-strategy.md`` v2.2 §10.
"""

from sqlalchemy.ext.asyncio import AsyncSession


async def set_tenant_context(session: AsyncSession, organization_id: str) -> None:
    """Set the PostgreSQL ``app.current_org_id`` for this session.

    ``SET LOCAL`` is per-transaction. Call this after authentication and
    before any tenanted query in the request — the value is then visible
    to RLS policies via ``current_setting('app.current_org_id', true)``.

    On SQLite, this is a no-op. The dialect check happens before the
    SQL is sent, so SQLite never sees the unsupported ``SET LOCAL``
    syntax.

    Args:
        session: Active async SQLAlchemy session.
        organization_id: The tenant identifier. Bound as a parameter to
            prevent SQL injection.
    """
    # Single implementation lives in the persistence layer (the get_db_session
    # chokepoint uses it too); delegate so the SET LOCAL logic cannot diverge.
    from faultmaven.infrastructure.persistence.tenant_context import (
        apply_tenant_context,
    )

    await apply_tenant_context(session, organization_id)
