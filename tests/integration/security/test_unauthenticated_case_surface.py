"""The case router, driven with no credential against services that all fail.

The twenty-nine gated operations on this router that resolve the case service,
among them the twenty-two #1527 fixed and ``GET /cases/{case_id}/uploaded-files``,
fixed by #2356. Those carried #1494's shape: the gate was declared as a
trailing handler parameter while the case service was declared above it, and
FastAPI solves a dependant's dependencies in DECLARATION ORDER — so an anonymous caller reached
the provider first and got whatever the provider does on a degraded deployment
instead of the 401 the route promises. The same finding as #1447 on the session
router, one module over, and closed the same way: the gate is a ROUTE-LEVEL
dependency, declared on the decorator ahead of everything, with the
``current_user`` parameter kept where the handler needs the principal (FastAPI
caches a dependency per request, so it still runs once).

**Why the providers are forced to raise here, rather than simply left
unwired.** That is the difference between a measurement and a restatement, and
on this router the distinction bites. Measured on a service-less app before
#1527, three of the twenty-two it fixed answered 500 and the other nineteen
already answered 401 — not because the ordering was right, but because
``get_case_service`` is ``getattr(app.state, "case_service", None)`` and
``check_case_service_available`` turned that ``None`` into a 401 of its own (a
503 since #2103). A probe that only dropped the services would therefore have
passed on nineteen rows with the defect fully present, and would go on passing
the day that provider learns to raise. Overriding the providers to raise removes
that accident: the ONLY way to answer 401 is for the gate to have run first.

Two batteries, because the ordering claim and the 401-with-a-credential claim
are different claims: **anonymous** (no header at all) and **rejected
credential** (a header that does not authenticate). Both must be refused before
any collaborator resolves. A third drives the same rows **authenticated, with
no case service**: each but the evidence-reclassification row must answer 503
"Case service unavailable", never the 500 of a handler that dereferenced the
``None`` its provider returned (#2358); that row is driven on its own below.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from faultmaven.api import route_enumeration
from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import (
    get_case_repository,
    get_case_service,
    get_evidence_reclassifier,
    get_investigation_service,
)
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_runbook_kb_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router as case_router
from tests.utils import uncomposed_auth_service

pytestmark = [pytest.mark.integration, pytest.mark.security]

#: Every collaborator a route in the set below declares. All of them are made to
#: raise, so reaching any one of them is a 500 and cannot be mistaken for a
#: refusal.
_SERVICE_PROVIDERS = (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
    _di_get_runbook_kb_dependency,
    get_investigation_service,
    get_evidence_reclassifier,
    get_case_repository,
    get_case_service,
)

#: The auth gate, named the way the guard module names it, so "is this route
#: gated" is asked of the dependency tree rather than of the source text.
_GATE = "faultmaven.api.v1.auth_dependencies.require_authentication"

#: The case-service providers, and the seam this module is about: the DI wrapper
#: every route here declares, and the bare provider it calls. The wrapper calls
#: it outside the dependency tree, so a route declaring the bare provider — the
#: shape #2358 removed — is collected only because it is named here.
_CASE_SERVICES = (
    "faultmaven.modules.case.api.routes.dependencies._di_get_case_service_dependency",
    "faultmaven.api.v1.dependencies.get_case_service",
)

#: One concrete request per operation, because a route cannot be driven through
#: its template. Hand-written, and then checked against the router below — a new
#: case route that takes the case service and is missed here fails
#: :func:`test_every_gated_case_service_route_is_driven` rather than quietly
#: going unmeasured.
#:
#: The three ``/evidence`` and ``/uploaded-files/{file_id}`` rows were never in
#: #1494's allowlist: they already declared ``current_user`` ahead
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
    ("GET", "/api/v1/cases/{case_id}/uploaded-files"): {
        "url": "/api/v1/cases/c1/uploaded-files"
    },
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
        "json": {"report_types": ["resolution_summary"]},
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
    # ADR-020 D4: the driver routes.
    ("GET", "/api/v1/cases/{case_id}/driver-candidates"): {
        "url": "/api/v1/cases/c1/driver-candidates"
    },
    ("PUT", "/api/v1/cases/{case_id}/driver"): {
        "url": "/api/v1/cases/c1/driver",
        "json": {"driver_id": "u1"},
    },
    # Joins the set because the handler now asks the case service whether the
    # caller reads the case (ADR-020 D2).
    ("PATCH", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"): {
        "url": "/api/v1/cases/c1/evidence/e1/classification",
        "json": {"data_type": "logs_and_errors"},
    },
}

#: Driven by the anonymous batteries but not held to 503 with no case service.
#: Under the default flag (``FAULTMAVEN_RECLASSIFY_ENABLED`` off) the handler
#: answers the flag's 404 before it reads the case service. With the flag on it
#: asks the case service only whether the caller reads the case (ADR-020 D2)
#: and proceeds without one by design, which
#: :func:`test_with_no_case_service_reclassification_proceeds_as_a_non_reader`
#: drives.
_CASE_SERVICE_OPTIONAL = {
    ("PATCH", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"),
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
    app.state.auth_service = uncomposed_auth_service()
    for provider in _SERVICE_PROVIDERS:
        app.dependency_overrides[provider] = _explodes(
            f"{provider.__module__}.{provider.__name__}"
        )
    return app


def _drive(client: TestClient, operation: tuple[str, str], headers: dict):
    """Send one row of :data:`DRIVEN`."""
    request = DRIVEN[operation]
    kwargs = {k: v for k, v in request.items() if k != "url"}
    return client.request(operation[0], request["url"], headers=headers, **kwargs)


def _answers(client: TestClient, headers: dict) -> dict:
    """Drive every row of :data:`DRIVEN` once; the response each answered."""
    return {
        f"{method} {template}": _drive(client, (method, template), headers)
        for method, template in sorted(DRIVEN)
    }


def _service_less_surface() -> FastAPI:
    """The case router, authenticated, with no case service composed.

    The case-service providers are left unwired, so they return ``None`` as on
    a deployment that composed none; every other collaborator is an inert
    stand-in, so the case service is the only thing missing. The app's own
    exception handlers map a domain exception to the status production sends.
    """

    async def _inert():
        return object()

    async def _caller():
        return SimpleNamespace(user_id="u1")

    app = _surface()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    for provider in _SERVICE_PROVIDERS:
        app.dependency_overrides[provider] = _inert
    del app.dependency_overrides[_di_get_case_service_dependency]
    del app.dependency_overrides[get_case_service]
    app.dependency_overrides[require_authentication] = _caller
    return app


def _case_service_operations(app: FastAPI) -> set[tuple[str, str]]:
    """Gated operations on this router whose tree holds a case-service provider.

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
        if names.isdisjoint(_CASE_SERVICES) or _GATE not in names:
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

    answered = _answers(client, headers)

    not_refused = {
        k: r.status_code for k, r in answered.items() if r.status_code != 401
    }
    assert not not_refused, (
        f"with {label}, these case operations answered something other than "
        "401 although every service provider on the route was made to raise — "
        "so a collaborator resolved before the auth gate, and an anonymous "
        f"caller gets its failure in place of the refusal (#1494): {not_refused}"
    )


def test_with_no_case_service_every_case_operation_answers_503():
    """An authenticated caller on an app with no case service gets 503, never 500.

    A handler that reads the service without ``check_case_service_available``
    dereferences the ``None`` its provider returned, and its catch-all answers
    500 (#2358). The detail is asserted too: a 503 raised for some other
    missing collaborator ahead of the check would otherwise pass for it.
    """
    client = TestClient(_service_less_surface(), raise_server_exceptions=False)

    answered = _answers(client, {})

    assert _CASE_SERVICE_OPTIONAL <= set(DRIVEN)
    exempt = {f"{method} {template}" for method, template in _CASE_SERVICE_OPTIONAL}
    unavailable = (503, {"detail": "Case service unavailable"})
    wrong = {
        k: (r.status_code, r.text)
        for k, r in answered.items()
        if k not in exempt and (r.status_code, r.json()) != unavailable
    }
    assert not wrong, (
        "with no case service composed, these case operations did not answer "
        "503 'Case service unavailable' to an authenticated caller — a 500 is "
        "a handler that used the service without check_case_service_available: "
        f"{wrong}"
    )


def test_with_no_case_service_reclassification_proceeds_as_a_non_reader(
    monkeypatch,
):
    """The exempt row, driven past its flag with no case service.

    The handler cannot learn whether the caller reads the case, so it must
    tell the reclassifier they do not (ADR-020 D2): never assume they do, and
    never dereference the missing service.
    """
    from faultmaven.config.settings import get_settings

    monkeypatch.setattr(get_settings().preprocessing, "reclassify_enabled", True)
    reclassifier = SimpleNamespace(
        reclassify_out_of_band=AsyncMock(
            return_value=SimpleNamespace(
                evidence_id="e1", source_type="logs", summary="", metadata={}
            )
        )
    )

    async def _reclassifier():
        return reclassifier

    app = _service_less_surface()
    app.dependency_overrides[get_evidence_reclassifier] = _reclassifier
    (operation,) = _CASE_SERVICE_OPTIONAL

    response = _drive(TestClient(app, raise_server_exceptions=False), operation, {})

    assert response.status_code == 200, response.text
    reclassifier.reclassify_out_of_band.assert_awaited_once()
    assert (
        reclassifier.reclassify_out_of_band.await_args.kwargs["caller_reads_case"]
        is False
    )


def test_the_probe_bites_when_the_gate_is_declared_after_the_service():
    """The positive control: the same shape, built wrong, answers 500.

    Without this the battery above passes on an app where nothing resolves at
    all, and "every row is 401" would say nothing about ordering. The gate here
    is the real ``require_authentication`` and the collaborator is the real
    overridden provider; only the DECLARATION ORDER differs.
    """
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
    app.state.auth_service = uncomposed_auth_service()
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/gate-after-the-service").status_code == 500, (
        "the mis-ordered shape did not answer 500, so the overrides above are "
        "not making the provider fail and the battery proves nothing"
    )
    assert client.get("/gate-on-the-decorator").status_code == 401, (
        "the fixed shape did not answer 401 — the route-level gate is not "
        "being solved ahead of the handler's own parameters on this FastAPI"
    )
