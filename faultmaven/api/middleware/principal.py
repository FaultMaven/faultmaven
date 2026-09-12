"""The principal a request was bound to, published for the access log.

``bind_request_enterprise_context`` (``api/middleware/tenant_scope.py``) is the
one place per request where the token is verified and the tenancy decision is
made. It already holds the answer to "who is this, and which enterprise is the
request scoped to" — and until this module it kept it to itself, so the access
log had to guess. It guessed from a **session id**, which a bearer-authenticated
request does not carry: every case read, list and search on the live deployment
logged ``user_id: null`` and named no enterprise at all, and attributing "who
read case X" meant joining correlation ids against sign-in lines.

The binder cannot hand the answer over in a contextvar. ``LoggingMiddleware`` is
a ``BaseHTTPMiddleware``, and Starlette runs its downstream app in a separate
task — the reason ``tenant_scope`` is a dependency rather than a middleware in
the first place. ``request.state`` is the channel that does cross that boundary:
it is backed by the ASGI ``scope``, which is one dict shared by both tasks.

**Nothing secret goes on state.** The binder has the bearer token in hand at the
moment it publishes, and the whole point of this record is to be logged, so the
shape is a frozen dataclass with four declared identifier fields rather than a
free dict something could later drop a token into. ``tests/unit/api/middleware/
test_access_log_omits_credentials.py`` asserts on the field set.
"""

from dataclasses import dataclass
from typing import Optional

from starlette.requests import Request

#: The ``request.state`` attribute the binder writes and the access log reads.
#: Named once here so the writer and the reader cannot drift apart.
REQUEST_PRINCIPAL_ATTR = "principal"


@dataclass(frozen=True)
class RequestPrincipal:
    """Who a request acts as, and what it was bound to — identifiers only.

    Attributes:
        user_id: The verified ``sub`` claim, or ``None`` when nothing was
            verified. ``None`` is not "unknown": it means this request carries
            no verified subject, which is the honest answer for an
            unauthenticated request and for the single-tenant arm, which
            deliberately never reads the token.
        enterprise_id: The enterprise actually bound — the Standalone
            enterprise under single-tenant, the verified claim under
            multi-tenant, and the empty non-tenant sentinel when there was
            nothing to verify or the claim was unusable. Empty is a value, not
            an absence; an *absent* principal is what "no binder ran" looks
            like.
        organization_id: The validated billing organization, or ``None`` for an
            account in none. Billing attribution only — never a visibility
            predicate (ADR-017 D5).
        account_kind: ``users.account_kind`` if the token carries it. No mint
            path emits this claim today, so it is ``None`` in every deployment;
            it is read through rather than derived so that the day a token does
            carry it, the log says so without another change here.
    """

    user_id: Optional[str]
    enterprise_id: str
    organization_id: Optional[str] = None
    account_kind: Optional[str] = None


def publish_request_principal(request: Request, principal: RequestPrincipal) -> None:
    """Publish the binding decision where the access log can read it."""
    setattr(request.state, REQUEST_PRINCIPAL_ATTR, principal)


def read_request_principal(request: Request) -> Optional[RequestPrincipal]:
    """The published principal, or ``None`` when no binder ran for this request.

    ``None`` is reachable: the binder is a route dependency, so a request that
    matches no route (a 404, a scanner) and a response a middleware short-
    circuits above the router never reach it. Those lines keep the session
    lookup they always had.
    """
    return getattr(request.state, REQUEST_PRINCIPAL_ATTR, None)
