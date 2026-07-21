"""Tenant context for PostgreSQL Row-Level Security (ADR-010).

Carries the current organization id in a contextvar. The engine ``begin``
listener (``infrastructure/persistence/database.py``) reads it and applies it to
**every transaction** as ``app.current_org_id``, so the RLS policies
(migration 018) scope reads to that organization. The contextvar **defaults to
the Standalone org**, so single-tenant deployments are always scoped correctly
without any per-request wiring; multi-tenant request handling sets it per request
(ADR-010 P2).

This lives in ``config`` — a neutral leaf importable from every layer — rather
than under ``infrastructure``, so the api-layer request middleware that sets it
per request can import it without violating the api→infrastructure boundary
(``tests/.../test_architecture_boundaries.py::test_api_layer_boundaries``).
"""

from contextvars import ContextVar

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
