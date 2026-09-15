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

The first test below pins the instance. The rest pin the CLASS, and there are
TWO halves to it, because a session id can be spent two ways:

* **As a credential** — the route admits an anonymous caller and resolves
  identity from the session. Requiring a bearer is what makes the session id an
  argument rather than a proof.
* **As a licence over somebody else's session** — the route requires a bearer
  and still never asks whose session it was handed. That is the #1390/#1393
  defect, and requiring authentication does not touch it.

WHERE THE ROUTER LOOKS FOR A SESSION ID. Originally only ``{session_id}`` in
the path, which was structurally blind to the one that arrives in a request
BODY — which is exactly how ``POST /api/v1/cases`` takes one
(``CaseCreateRequest.session_id``), and why that route sat outside both guards
while carrying the same defect. The rule is now "wherever a request can put
one": path, query, or a declared body model. The one place it still cannot see
is an untyped ``Dict[str, Any]`` body, which has no field names to inspect;
every such route on this router is addressed by ``{case_id}`` and gated on the
case.

SCOPE. This file is about the case router and about AUTH sessions — the ones
``POST /api/v1/sessions`` mints, whose ids spend as identity and which key
``session:{id}:current_case_id``. The ``{session_id}`` in
``/api/v1/cases/{case_id}/sessions/{session_id}/…`` is a different thing (an
INVESTIGATION session), served by a different router
(``faultmaven/api/routes/sessions.py``) which gates on the CASE named in its
path with ``owner_only`` chosen from the HTTP method. Those routes are not
missing from the lists below; they are not in this router.
"""

from __future__ import annotations

import typing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from faultmaven.modules.case.api.routes import router as case_router
from faultmaven.modules.case.domain.models import Case

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

CALLER = "user-1"  # what the conftest's `require_authentication` override mints
SESSION_ID = "sess-belongs-to-somebody-else"
CASE_ID = "case-123"


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


def _declares_a_session_id(annotation: object) -> bool:
    """Whether a body annotation is a model with a ``session_id`` field.

    Unions are unwrapped because ``Optional[Model]`` is how an optional body is
    spelled, and reading the union itself finds no fields at all.
    """
    candidates = [annotation, *typing.get_args(annotation)]
    return any(
        "session_id" in (getattr(candidate, "model_fields", None) or {})
        for candidate in candidates
    )


def _session_id_locations(route: APIRoute) -> set[str]:
    """Every place this route lets a request hand it a session id.

    Path, query and body are three surfaces of one question — "did the caller
    supply a session id?" — and a guard that reads only one of them is not
    weaker in principle, it is blind in practice: the body arm is where
    ``POST /api/v1/cases`` hides.
    """
    found: set[str] = set()
    if "{session_id}" in route.path:
        found.add("path")
    if any(p.name == "session_id" for p in route.dependant.query_params):
        found.add("query")
    for param in route.dependant.body_params:
        annotation = getattr(getattr(param, "field_info", None), "annotation", None)
        if param.name == "session_id" or _declares_a_session_id(annotation):
            found.add("body")
    return found


def _session_addressed_routes() -> list[APIRoute]:
    return [route for route in _case_routes() if _session_id_locations(route)]


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


#: How to drive each session-addressed route with a session that is NOT the
#: caller's: the concrete request, keyed on the route it exercises.
#:
#: A table rather than a generated request, because each route needs its own
#: body and path arguments — and keyed on the route so the completeness check
#: below turns a NEW session-addressed route into a failure here until somebody
#: decides what it does with one. That is the point of it: this guard is for
#: the route nobody has written yet.
FOREIGN_SESSION_PROBES: dict[tuple[str, str], dict] = {
    ("POST", "/api/v1/cases"): {
        "url": "/api/v1/cases",
        "json": {"title": "t", "session_id": SESSION_ID},
    },
    ("POST", "/api/v1/cases/sessions/{session_id}/resume/{case_id}"): {
        "url": f"/api/v1/cases/sessions/{SESSION_ID}/resume/{CASE_ID}",
    },
}


def _permissive_case_service() -> SimpleNamespace:
    """A case service that refuses NOTHING, so the session gate is the only
    thing that can.

    Every member answers yes and records that it was reached. A route with no
    session gate therefore succeeds here — which is what makes the refusals
    below attributable to the gate rather than to some unrelated validation
    failing first.
    """
    reached: list[str] = []

    async def get_case(case_id, user_id=None, *, owner_only=False):
        return SimpleNamespace(case_id=CASE_ID, user_id=CALLER)

    async def create_case(
        title=None,
        description=None,
        owner_id=None,
        session_id=None,
        initial_message=None,
        source="copilot",
    ):
        reached.append("create_case")
        return Case(enterprise_id="ent-1", title="t", user_id=CALLER)

    async def resume_case_in_session(case_id, session_id, user_id):
        reached.append("resume_case_in_session")
        return True

    return SimpleNamespace(
        get_case=get_case,
        create_case=create_case,
        resume_case_in_session=resume_case_in_session,
        reached=reached,
    )


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
    """The class, half one: no case route lets a session id stand in for a
    credential.

    A route that takes a session id and admits an anonymous caller has nothing
    else to identify that caller by, so it will resolve identity from the
    session or from nothing. Requiring a bearer is what makes the session id an
    argument rather than a proof.
    """
    offenders = [
        f"{sorted(route.methods)} {route.path} ({sorted(_session_id_locations(route))})"
        for route in _session_addressed_routes()
        if not (_dependency_names(route) & MANDATORY_AUTH_DEPENDENCIES)
    ]

    assert offenders == [], (
        "case routes that take a session id but do not require "
        f"authentication: {offenders}"
    )


@pytest.mark.unit
@pytest.mark.security
def test_the_foreign_session_probes_cover_every_session_addressed_route():
    """The completeness half of the guard below.

    Without this, adding a session-addressed route and no probe for it would
    leave that route untested while the suite stayed green — the guard would go
    on passing about the two routes it already knew.
    """
    served = {
        (method, route.path)
        for route in _session_addressed_routes()
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }

    assert served == set(FOREIGN_SESSION_PROBES), (
        "session-addressed case routes with no foreign-session probe: "
        f"{sorted(served - set(FOREIGN_SESSION_PROBES))}; probes for routes "
        f"that no longer exist: {sorted(set(FOREIGN_SESSION_PROBES) - served)}"
    )


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", sorted(FOREIGN_SESSION_PROBES))
async def test_every_session_addressed_case_route_refuses_a_foreign_session(
    method, path, build_app, call_api
):
    """The class, half two: requiring a bearer is not the same as asking whose
    session it is.

    Both of these routes write ``session:{id}:current_case_id``, so an
    authenticated caller naming somebody else's session retargets that session
    at a case of the caller's choosing — the owner's next turn then lands in
    the caller's case or abandons their own. The case service here refuses
    nothing, so a route that answers 2xx has no session gate at all.
    """
    probe = FOREIGN_SESSION_PROBES[(method, path)]
    service = _permissive_case_service()
    app = build_app(
        session=SimpleNamespace(session_id=SESSION_ID, user_id="somebody-else"),
        case_service=service,
    )

    response = await call_api(
        app, method, probe["url"], **{k: v for k, v in probe.items() if k != "url"}
    )

    assert response.status_code in {401, 403, 404}, (
        f"{method} {path} accepted a session belonging to another user: "
        f"{response.status_code} {response.text}"
    )
    # The status alone is not enough. The pointer write happens inside the
    # service and ahead of its own failure paths, so a gate that refused after
    # calling the service would leave the retarget done and still answer 404.
    assert service.reached == [], (
        f"{method} {path} reached the service with a foreign session: "
        f"{service.reached}"
    )


@pytest.mark.unit
@pytest.mark.security
def test_the_class_guards_inspect_something():
    """Guard against the guards above passing because nothing matched.

    The session id disappearing from every case route would make both lists
    empty and both assertions vacuous. Asserted per LOCATION rather than in
    total, because the body arm is the one that was missing: a router that
    still carried a path-addressed session id would satisfy a bare non-empty
    check while the blind spot this closes was back.
    """
    located = {
        location
        for route in _session_addressed_routes()
        for location in _session_id_locations(route)
    }

    assert "path" in located, "no path-addressed session id left to inspect"
    assert "body" in located, "no body-carried session id left to inspect"
