"""A session id alone is not identity anywhere on the case router.

``POST /api/v1/cases/sessions/{session_id}/case`` took
``get_current_user_optional`` and resolved::

    user_id = current_user.user_id if current_user else session.user_id

so with no ``Authorization`` header the owner of the created case came from the
session id in the path — a value the caller supplies. Paired with the session
mint, which until contract 6.0.0 let a caller *name* the user_id it bound, that
was a two-step to writing cases under a chosen identity with no credential at
all.

The route is REMOVED rather than repaired, because once it requires
authentication it does nothing ``POST /api/v1/cases`` does not already do:
that route validates the same ``session_id``, requires a bearer, and creates
the case with ``owner_id=current_user.user_id``. No client called it — the only
occurrences of its path in faultmaven-dashboard and faultmaven-copilot are
generated type declarations, and faultmaven-slack-agent and
faultmaven-website do not mention it at all.

The first test below pins the instance. The second pins the CLASS, which is the
one that has to keep holding: a route addressed by a session id and reachable
without a credential is, by construction, a route where the caller's own
session id is the only identity on offer.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from faultmaven.modules.case.api.routes import router as case_router

#: Dependencies that refuse an anonymous caller, spelled as
#: ``tests/integration/api/test_openapi_documents_auth.py`` spells them so the
#: two guards cannot disagree about what "authenticated" means.
MANDATORY_AUTH_DEPENDENCIES = frozenset(
    {
        "faultmaven.api.v1.auth_dependencies.require_authentication",
        "faultmaven.api.v1.auth_dependencies.require_platform_admin",
        "faultmaven.api.middleware.auth.get_current_user",
        "faultmaven.api.middleware.auth.require_platform_admin",
    }
)


def _case_routes() -> list[APIRoute]:
    # ``prefix="/api/v1"``, exactly as ``main.py`` mounts it — the router
    # carries its own ``/cases``. Mounting it at ``/api/v1/cases`` instead
    # doubles the segment and every path assertion below quietly stops
    # describing a served route.
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    # A floor, so a router that failed to mount cannot satisfy either test
    # below by having nothing to inspect.
    assert len(routes) > 30, f"only {len(routes)} case routes mounted"
    return routes


def _dependency_names(route: APIRoute) -> set[str]:
    """Every dependency in the route's tree, as dotted names.

    Walks the tree rather than reading the top level: ``require_authentication``
    can be reached through a composed dependency, and a guard that only looked
    one level down would report such a route as open.
    """
    names: set[str] = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        call = dependant.call
        if call is not None:
            # A security scheme is an INSTANCE (``HTTPBearer()``), not a
            # function, so it carries no ``__name__``. Fall back to its class
            # rather than skipping it, which would silently shrink the set the
            # membership test below is run against.
            name = getattr(call, "__name__", None) or type(call).__name__
            names.add(f"{call.__module__}.{name}")
        pending.extend(dependant.dependencies)
    return names


@pytest.mark.unit
@pytest.mark.security
def test_the_session_addressed_case_creation_route_is_gone():
    """The instance: nothing serves the removed path, under any method."""
    paths = {route.path for route in _case_routes()}

    assert "/api/v1/cases/sessions/{session_id}/case" not in paths
    # The sibling that survives, as a vacuity control: this asserts the router
    # really is mounted at the prefix the assertion above spells.
    assert "/api/v1/cases/sessions/{session_id}/resume/{case_id}" in paths


@pytest.mark.unit
@pytest.mark.security
def test_every_session_addressed_case_route_requires_authentication():
    """The class: no case route lets a session id stand in for a credential.

    A route that names ``{session_id}`` in its path and admits an anonymous
    caller has nothing else to identify that caller by, so it will resolve
    identity from the session or from nothing. Requiring a bearer is what makes
    the session id an argument rather than a proof.
    """
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in _case_routes()
        if "{session_id}" in route.path
        and not (_dependency_names(route) & MANDATORY_AUTH_DEPENDENCIES)
    ]

    assert offenders == [], (
        "case routes addressed by a session id that do not require "
        f"authentication: {offenders}"
    )


@pytest.mark.unit
@pytest.mark.security
def test_the_class_guard_inspects_something():
    """Guard against the guard above passing because nothing matched.

    ``{session_id}`` disappearing from every case path would make the list
    comprehension empty and the assertion vacuous.
    """
    session_addressed = [r.path for r in _case_routes() if "{session_id}" in r.path]

    assert session_addressed, "no session-addressed case routes left to inspect"
