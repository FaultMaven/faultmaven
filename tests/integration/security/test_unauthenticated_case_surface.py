"""The case router, driven with no credential against services that all fail.

#1494's shape, for the twenty-two operations behind
``_di_get_case_service_dependency``: the gate was declared as a trailing handler
parameter while the case service was declared above it, and FastAPI solves a
dependant's dependencies in DECLARATION ORDER — so an anonymous caller reached
the provider first and got whatever the provider does on a degraded deployment
instead of the 401 the route promises. The same finding as #1447 on the session
router, one module over, and closed the same way: the gate is a ROUTE-LEVEL
dependency, declared on the decorator ahead of everything, with the
``current_user`` parameter kept where the handler needs the principal (FastAPI
caches a dependency per request, so it still runs once).

**Why the providers are forced to raise here, rather than simply left
unwired.** That is the difference between a measurement and a restatement, and
on this router the distinction bites. Measured on a service-less app before the
fix, three of the twenty-two answered 500 and the other nineteen already
answered 401 — not because the ordering was right, but because
``get_case_service`` is ``getattr(app.state, "case_service", None)`` and
``check_case_service_available`` turns that ``None`` into a 401 of its own. A
probe that only dropped the services would therefore have passed on nineteen
rows with the defect fully present, and would go on passing the day that
provider learns to raise. Overriding the providers to raise removes that
accident: the ONLY way to answer 401 is for the gate to have run first.

Two batteries, because the ordering claim and the 401-with-a-credential claim
are different claims: **anonymous** (no header at all) and **rejected
credential** (a header that does not authenticate). Both must be refused before
any collaborator resolves.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from faultmaven.api import route_enumeration
from faultmaven.api.v1.dependencies import (
    get_case_repository,
    get_investigation_service,
)
from faultmaven.modules.case.api.routes import (
    _di_get_case_service_dependency,
    _di_get_runbook_kb_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.api.routes import router as case_router

pytestmark = [pytest.mark.integration, pytest.mark.security]

#: Every collaborator a route in the set below declares. All of them are made to
#: raise, so reaching any one of them is a 500 and cannot be mistaken for a
#: refusal.
_SERVICE_PROVIDERS = (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
    _di_get_runbook_kb_dependency,
    get_investigation_service,
    get_case_repository,
)

#: The auth gate, named the way the guard module names it, so "is this route
#: gated" is asked of the dependency tree rather than of the source text.
_GATE = "faultmaven.api.v1.auth_dependencies.require_authentication"

#: The provider whose twenty-two routes #1494 was split on, and the seam this
#: module is about.
_CASE_SERVICE = "faultmaven.modules.case.api.routes._di_get_case_service_dependency"

#: One concrete request per operation, because a route cannot be driven through
#: its template. Hand-written, and then checked against the router below — a new
#: case route that takes the case service and is missed here fails
#: :func:`test_every_gated_case_service_route_is_driven` rather than quietly
#: going unmeasured.
#:
#: The three ``/evidence`` and ``/uploaded-files/{file_id}`` rows were never in
#: ``MISORDERED_GATE_OPERATIONS``: they already declared ``current_user`` ahead
#: of the service. They are driven anyway, because the property is about the
#: route rather than about which routes happened to be wrong, and a parameter
#: edit could put them back.
DRIVEN: dict[tuple[str, str], dict] = {
    ("DELETE", "/api/v1/cases/{case_id}"): {"url": "/api/v1/cases/c1"},
    ("DELETE", "/api/v1/cases/{case_id}/data/{data_id}"): {
        "url": "/api/v1/cases/c1/data/d1"
    },
    ("DELETE", "/api/v1/cases/{case_id}/team-shares/{team_id}"): {
        "url": "/api/v1/cases/c1/team-shares/t1"
    },
    ("GET", "/api/v1/cases"): {"url": "/api/v1/cases"},
    ("GET", "/api/v1/cases/{case_id}"): {"url": "/api/v1/cases/c1"},
    ("GET", "/api/v1/cases/{case_id}/analytics"): {"url": "/api/v1/cases/c1/analytics"},
    ("GET", "/api/v1/cases/{case_id}/data"): {"url": "/api/v1/cases/c1/data"},
    ("GET", "/api/v1/cases/{case_id}/data/{data_id}"): {
        "url": "/api/v1/cases/c1/data/d1"
    },
    ("GET", "/api/v1/cases/{case_id}/evidence"): {"url": "/api/v1/cases/c1/evidence"},
    ("GET", "/api/v1/cases/{case_id}/evidence/{evidence_id}"): {
        "url": "/api/v1/cases/c1/evidence/e1"
    },
    ("GET", "/api/v1/cases/{case_id}/messages"): {"url": "/api/v1/cases/c1/messages"},
    ("GET", "/api/v1/cases/{case_id}/report-recommendations"): {
        "url": "/api/v1/cases/c1/report-recommendations"
    },
    ("GET", "/api/v1/cases/{case_id}/reports"): {"url": "/api/v1/cases/c1/reports"},
    ("GET", "/api/v1/cases/{case_id}/reports/{report_id}/download"): {
        "url": "/api/v1/cases/c1/reports/r1/download"
    },
    ("GET", "/api/v1/cases/{case_id}/ui"): {"url": "/api/v1/cases/c1/ui"},
    ("GET", "/api/v1/cases/{case_id}/uploaded-files/{file_id}"): {
        "url": "/api/v1/cases/c1/uploaded-files/f1"
    },
    ("POST", "/api/v1/cases"): {
        "url": "/api/v1/cases",
        "json": {"description": "anything"},
    },
    ("POST", "/api/v1/cases/search"): {
        "url": "/api/v1/cases/search",
        "json": {"query": "anything"},
    },
    ("POST", "/api/v1/cases/sessions/{session_id}/resume/{case_id}"): {
        "url": "/api/v1/cases/sessions/s1/resume/c1"
    },
    ("POST", "/api/v1/cases/{case_id}/close"): {
        "url": "/api/v1/cases/c1/close",
        "json": {},
    },
    ("POST", "/api/v1/cases/{case_id}/reports"): {
        "url": "/api/v1/cases/c1/reports",
        "json": {},
    },
    ("POST", "/api/v1/cases/{case_id}/team-shares"): {
        "url": "/api/v1/cases/c1/team-shares",
        "json": {"team_id": "t1"},
    },
    ("POST", "/api/v1/cases/{case_id}/title"): {
        "url": "/api/v1/cases/c1/title",
        "json": {},
    },
    ("POST", "/api/v1/cases/{case_id}/turns"): {
        "url": "/api/v1/cases/c1/turns",
        "data": {"query": "anything"},
    },
    ("PUT", "/api/v1/cases/{case_id}"): {
        "url": "/api/v1/cases/c1",
        "json": {"title": "anything"},
    },
}


def _explodes(name):
    async def _boom():
        raise RuntimeError(
            f"{name} was resolved for a caller who is not authenticated — "
            "the auth gate is being solved after a service provider (#1494)"
        )

    return _boom


def _dependency_names(dependant, seen=None) -> set[str]:
    """Every callable in a route's tree, qualified. The guard module's walk."""
    from tests.integration.api.test_openapi_documents_auth import (
        _dependency_names as _walk,
    )

    return _walk(dependant, seen)


def _surface() -> FastAPI:
    """The case router, mounted as ``main`` mounts it, over failing services."""
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    for provider in _SERVICE_PROVIDERS:
        app.dependency_overrides[provider] = _explodes(
            f"{provider.__module__}.{provider.__name__}"
        )
    return app


def _case_service_operations(app: FastAPI) -> set[tuple[str, str]]:
    """Gated operations on this router whose tree holds the case service.

    Through ``route_enumeration`` rather than a flat ``app.routes`` scan. On
    the FastAPI this repository develops against (0.141.1) ``include_router``
    records a ``_IncludedRouter`` placeholder instead of copying the routes, so
    a flat scan of this two-line app finds NOTHING — measured: the set came
    back empty and the assertion below fired. CI's pinned 0.136.0 still copies,
    which is exactly the drift that module exists to absorb.
    """
    found = set()
    for path, methods, dependant in route_enumeration.iter_served_routes(app):
        names = _dependency_names(dependant)
        if _CASE_SERVICE not in names or _GATE not in names:
            continue
        for method in methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, path))
    return found


def test_every_gated_case_service_route_is_driven():
    """The hand-written map is checked against what the router actually serves.

    Without this the battery below is a rule about the rows somebody
    remembered: a case route added tomorrow that takes the case service would
    simply not appear, and every assertion would still pass.
    """
    served = _case_service_operations(_surface())

    assert served, (
        "no gated route on the case router resolves the case service — the "
        "walk is not seeing the router's dependency trees, and the battery "
        "below would be quantified over nothing"
    )
    assert served == set(DRIVEN), (
        "the driven set and the served set disagree.\n"
        f"served but not driven: {sorted(served - set(DRIVEN))}\n"
        f"driven but not served: {sorted(set(DRIVEN) - served)}"
    )


@pytest.mark.parametrize(
    "headers,label",
    [
        ({}, "no Authorization header at all"),
        ({"Authorization": "Bearer not-a-real-token"}, "a credential that fails"),
    ],
)
def test_no_case_service_route_resolves_a_service_for_an_unauthenticated_caller(
    headers, label
):
    """Every one of them answers 401, on an app where every provider raises.

    If the gate were solved after the case service — the shape #1494 carried —
    each row would be a 500 raised out of :func:`_explodes`, because the
    provider is reached before anything checks the caller.
    """
    client = TestClient(_surface(), raise_server_exceptions=False)

    answered = {}
    for (method, template), request in sorted(DRIVEN.items()):
        kwargs = {k: v for k, v in request.items() if k != "url"}
        response = client.request(method, request["url"], headers=headers, **kwargs)
        answered[f"{method} {template}"] = response.status_code

    not_refused = {k: v for k, v in answered.items() if v != 401}
    assert not not_refused, (
        f"with {label}, these case operations answered something other than "
        "401 although every service provider on the route was made to raise — "
        "so a collaborator resolved before the auth gate, and an anonymous "
        f"caller gets its failure in place of the refusal (#1494): {not_refused}"
    )


def test_the_probe_bites_when_the_gate_is_declared_after_the_service():
    """The positive control: the same shape, built wrong, answers 500.

    Without this the battery above passes on an app where nothing resolves at
    all, and "every row is 401" would say nothing about ordering. The gate here
    is the real ``require_authentication`` and the collaborator is the real
    overridden provider; only the DECLARATION ORDER differs.
    """
    from faultmaven.api.v1.auth_dependencies import require_authentication

    app = FastAPI()

    @app.get("/gate-after-the-service")
    async def _wrong(  # pragma: no cover - the body is never reached
        case_service=Depends(_di_get_case_service_dependency),
        current_user=Depends(require_authentication),
    ):
        return {}

    @app.get(
        "/gate-on-the-decorator",
        dependencies=[Depends(require_authentication)],
    )
    async def _right(  # pragma: no cover - the body is never reached
        case_service=Depends(_di_get_case_service_dependency),
        current_user=Depends(require_authentication),
    ):
        return {}

    app.dependency_overrides[_di_get_case_service_dependency] = _explodes(
        "the case service"
    )
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/gate-after-the-service").status_code == 500, (
        "the mis-ordered shape did not answer 500, so the overrides above are "
        "not making the provider fail and the battery proves nothing"
    )
    assert client.get("/gate-on-the-decorator").status_code == 401, (
        "the fixed shape did not answer 401 — the route-level gate is not "
        "being solved ahead of the handler's own parameters on this FastAPI"
    )
