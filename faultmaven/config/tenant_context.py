"""Tenant context for PostgreSQL Row-Level Security (ADR-010, ADR-017).

Carries the current **enterprise** id in a contextvar. The engine ``begin``
listener (``infrastructure/persistence/database.py``) reads it and applies it to
**every transaction** as ``app.current_enterprise_id``, so the RLS policies scope
reads to that enterprise. The contextvar **defaults to the Standalone
enterprise**, so single-tenant deployments are always scoped correctly without
any per-request wiring; multi-tenant request handling sets it per request
(ADR-010 P2).

ADR-017 moved the isolation key one tier up: the enterprise isolates, the
organization bills, the team shares. The **billing** organization is carried here
too, in a second contextvar, but it is a different kind of thing and the module
keeps them apart on purpose: the enterprise is a *binding* (the RLS GUC, refused
when absent), the organization is *attribution* (stamped on a row, ``None`` is
an ordinary answer, and no read predicate may ever use it).

This lives in ``config`` — a neutral leaf importable from every layer — rather
than under ``infrastructure``, so the api-layer request middleware that sets it
per request can import it without violating the api→infrastructure boundary
(``tests/.../test_architecture_boundaries.py::test_api_layer_boundaries``).
"""

from contextvars import ContextVar
from typing import Optional

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID

# Default to the Standalone enterprise so every single-tenant session is scoped
# without any caller having to set it. Multi-tenant request handling overrides
# per request.
_current_enterprise_id: ContextVar[str] = ContextVar(
    "current_enterprise_id", default=STANDALONE_ENTERPRISE_ID
)

#: The organization that pays for the account this request acts as (ADR-017 D2),
#: or ``None`` — which is the ordinary answer, because an account may be in no
#: organization at all. Bound at the request front door from the verified
#: ``organization_id`` claim, beside the enterprise, so that both tenancy facts
#: enter a request in exactly one place.
#:
#: **Attribution, never a predicate.** Every writer of a nullable
#: ``organization_id`` column reads this to stamp who pays; nothing reads it to
#: decide what may be seen. That is the whole of ADR-017 D2, and the reason it is
#: a separate contextvar rather than a second field on the tenant binding: a
#: single "current tenant" object is what let the two concerns be confused in the
#: first place.
_current_billing_organization_id: ContextVar[Optional[str]] = ContextVar(
    "current_billing_organization_id", default=None
)


def set_current_billing_organization_id(organization_id: Optional[str]) -> None:
    """Set the billing organization for the current execution context."""
    _current_billing_organization_id.set(organization_id or None)


def get_current_billing_organization_id() -> Optional[str]:
    """Return the organization this request's rows are billed to, or ``None``.

    ``None`` means "nobody is paying for this account", which is a legitimate
    steady state (a standalone deployment, or a cloud account that has not been
    added to an organization). Writers stamp it as-is; the column is nullable
    precisely so they can.
    """
    return _current_billing_organization_id.get()


#: What a caller is told when :func:`usable_tenant_id` answers ``None`` on a path
#: that refuses rather than degrades. It lives beside the predicate so the rule
#: and its announcement stay together: both the request front door
#: (``api/middleware/tenant_scope``) and the route-level
#: ``api/v1/auth_dependencies.require_actor_enterprise`` raise 403 with this
#: exact text, and neither owns a private copy of it.
UNSCOPED_REQUEST_MSG = "Request is not scoped to an enterprise."


def set_current_enterprise_id(enterprise_id: str) -> None:
    """Set the enterprise id for the current execution context (request/task)."""
    _current_enterprise_id.set(enterprise_id)


def get_current_enterprise_id() -> str:
    """Return the current context's enterprise id (defaults to Standalone).

    **Total by construction** — the contextvar carries the Standalone sentinel as
    its default, so this never returns a falsy value and ``if not
    get_current_enterprise_id()`` is unreachable code, not a guard. Use it where
    an enterprise is simply *needed* (the RLS ``begin`` binding, the case write
    stamp). Where the value becomes a **query predicate**, use
    :func:`get_current_tenant_id`, which answers ``None`` when the context holds
    no usable tenant.
    """
    return _current_enterprise_id.get()


def writable_enterprise_id(enterprise_id: Optional[str]) -> str:
    """Return the enterprise a row should be stamped with on a write.

    Answers the question every ``enterprise_id NOT NULL`` writer faces when its
    caller passed nothing: *whose row is this?* The answer is the enterprise the
    database session is already bound to — :func:`get_current_enterprise_id`,
    which is exactly what the engine's ``begin`` listener writes into
    ``app.current_enterprise_id`` for the RLS policies to check. Stamping
    anything else writes a row the same transaction is not allowed to write.

    A **hardcoded single-tenant sentinel** is the wrong fallback and is why this
    function exists (#1143): under ``TENANT_PROVIDER=multi`` it is not the
    caller's tenant but the sentinel enterprise, so PostgreSQL rejects the INSERT
    with ``new row violates row-level security policy`` — a failure that cannot
    reproduce on SQLite, which has no RLS. Under ``single`` the contextvar's own
    default *is* that sentinel, so this returns the same value it always did.

    An explicit ``enterprise_id`` wins: a caller that knows whose row this is
    (the requesting user's enterprise, the source case's enterprise) is more
    specific than the ambient context. It is not re-validated against the context
    here — a mismatch is a caller bug that RLS surfaces loudly rather than one
    this function should paper over.

    **Raises on an unscoped context.** :func:`get_current_enterprise_id` is
    total, but "total" is not "usable": ``api/middleware/tenant_scope`` binds the
    empty non-tenant sentinel for unauthenticated and invalid-token requests, so
    an unauthenticated write path reaches here with ``""``. That value would pass
    ``NOT NULL``, and — since ``current_setting('app.current_enterprise_id')`` is
    also ``""`` — it would pass the RLS ``WITH CHECK`` too, only to die on the
    ``enterprises`` foreign key as an opaque ``IntegrityError`` some frames
    later. Refusing here turns "this request owns no tenant" into the statement
    it actually is, at the point it becomes knowable.

    Deliberately NOT the rule for every enterprise-stamping writer. An **audit**
    writer (``infrastructure/persistence/audit_repository``) records failed
    logins and other pre-auth events, where an unscoped context is normal and
    losing the record is worse than storing a sentinel-stamped one; it keeps its
    own inline fallback on purpose. This function is for writers that must
    refuse.
    """
    if enterprise_id:
        return enterprise_id
    ambient = get_current_enterprise_id()
    if not ambient:
        raise ValueError(
            "Cannot stamp an enterprise on this write: the request is not "
            "scoped to an enterprise. " + UNSCOPED_REQUEST_MSG
        )
    return ambient


def usable_tenant_id(enterprise_id: Optional[str]) -> Optional[str]:
    """Return ``enterprise_id`` iff it can serve as a tenant predicate, else ``None``.

    The single place the "is this a real tenant?" question is decided — every
    site that needs the answer calls this, none carries its own copy of the
    test, so they cannot drift. Two styles read it:

    * **refuse** — ``api/middleware/tenant_scope.bind_request_enterprise_context``
      at the request front door and
      ``api/v1/auth_dependencies.require_actor_enterprise`` at the route both
      turn a ``None`` here into a 403 (:data:`UNSCOPED_REQUEST_MSG`);
    * **degrade** — the service-layer read paths (the case read allowlist, the
      KB and agent team arms) turn it into an empty allowlist, which narrows
      rather than breaks;
    * **refuse, at the service layer** — the runbook-similarity consumers.
      Degrading is only safe where an empty result *means* "you may see less".
      On a dedup check it means "nothing similar exists", which the caller
      turns into "generate a new runbook" — a substantive claim derived from a
      search that was refused (#944). ``RunbookKnowledgeBase.search_by_text``
      therefore raises ``KnowledgeBaseError`` rather than returning ``[]``.
      ``terminal_transitions._find_similar_runbooks_for_case`` still checks
      here first, so its common path stays a quiet logged skip that reports
      "not checked" rather than an exception.

    Under ``TENANT_PROVIDER=multi`` the Standalone sentinel is **not** a tenant —
    it identifies the single-tenant deployment. Refusing it here is what makes
    the guard real (the fm#850 invariant, transferred to the enterprise by
    ADR-017 D8): the contextvar's default *is* the sentinel, so an execution
    context that never bound one (a background task that did not inherit the
    request context) otherwise resolves to the sentinel and hands it to a query
    as an ``enterprise_id`` predicate — the sentinel used as a tenant, the exact
    inverse of failing closed. Under ``single`` the sentinel is the deployment's
    one legitimate tenant and passes unchanged.
    """
    # Imported inside the function: ``providers`` reaches settings and the model
    # layer, and ``config`` is the neutral leaf everything else imports.
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )

    if not enterprise_id:
        return None
    if (
        enterprise_id == STANDALONE_ENTERPRISE_ID
        and requested_tenant_provider() == BUILTIN_MULTI
    ):
        return None
    return enterprise_id


def get_current_tenant_id() -> Optional[str]:
    """Return the current context's enterprise iff it is usable as a predicate.

    ``None`` means "this context carries no tenant" — the caller must refuse or
    collapse, never query. This is the read to use anywhere the result becomes a
    SQL or vector predicate; see :func:`usable_tenant_id` for why
    :func:`get_current_enterprise_id` cannot serve that role.
    """
    return usable_tenant_id(_current_enterprise_id.get())
