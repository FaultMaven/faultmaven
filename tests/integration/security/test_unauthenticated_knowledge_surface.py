"""The knowledge conversion surface, driven with no credential.

#1494's shape, for the eleven operations behind ``_get_conversion_service``:
the gate was declared as a trailing handler parameter while the conversion
service was declared above it, and FastAPI solves a dependant's dependencies in
DECLARATION ORDER — so an anonymous caller reached the provider first and got
whatever it does on a degraded deployment instead of the 401 the route
promises. The same finding as #1447 on the session router and #1527 on the case
router, and closed the same way: the gate is a ROUTE-LEVEL dependency declared
on the decorator ahead of everything, with the ``current_user`` parameter kept
where the handler needs the principal (FastAPI caches a dependency per request,
so it still runs once).

**This module is the knowledge-side twin of
``test_unauthenticated_case_surface.py``**, deliberately the same shape rather
than a second invention. The remaining knowledge seams — ``get_knowledge_service``
(9 operations) and ``get_suggestion_service`` (6) — belong here too when their
slices land; ``_CONVERSION_SERVICE`` and :data:`DRIVEN` are what would widen.

**Why the provider is forced to raise here, rather than simply left unwired.**
On the case router that distinction was load-bearing: ``get_case_service``
returns ``None`` and ``check_case_service_available`` turns that into a 401 of
its own, so nineteen of those twenty-two answered 401 with the defect fully
present. The knowledge module has NO such masking — measured, all three of its
providers (``_get_conversion_service``, ``get_knowledge_service``,
``get_suggestion_service``) raise a 503 of their own — so on a service-less app
all eleven of these answered **503** before the fix, and the defect was
visible. The provider is overridden to raise anyway, for the same reason the
case module needed it: the battery must not depend on WHICH failure the
provider happens to choose today. Overridden, a 401 is reachable only if the
gate ran first.

Three batteries, because they are three different claims: **anonymous** (no
header at all) and **rejected credential** (a header that does not
authenticate) must both be refused before any collaborator resolves — and an
**authenticated** caller must still reach the handler with a populated
principal, because a route that refuses correctly and then 500s for a
legitimate user is worse than the defect.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from faultmaven.api import route_enumeration
from faultmaven.modules.knowledge.api.conversion_routes import _get_conversion_service
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)
from faultmaven.modules.knowledge.api.routes import (
    get_knowledge_service,
    get_suggestion_service,
)
from faultmaven.modules.knowledge.api.routes import router as knowledge_router

pytestmark = [pytest.mark.integration, pytest.mark.security]

#: Every service provider reachable on the mounted surface. All of them are
#: made to raise, so reaching any one is a 500 and cannot be mistaken for a
#: refusal — including the two that belong to the sibling router, which are
#: overridden so a route that migrates between the two files cannot start
#: passing for the wrong reason.
_SERVICE_PROVIDERS = (
    _get_conversion_service,
    get_knowledge_service,
    get_suggestion_service,
)

#: The auth gate, named the way the guard module names it, so "is this route
#: gated" is asked of the dependency tree rather than of the source text. The
#: module under test imports it as ``_require_auth``; an alias does not change
#: the qualified name.
_GATE = "faultmaven.api.v1.auth_dependencies.require_authentication"

#: The provider whose eleven routes this slice of #1494 was split on.
_CONVERSION_SERVICE = (
    "faultmaven.modules.knowledge.api.conversion_routes._get_conversion_service"
)

#: The principal an authenticated caller presents in the third battery. A real
#: ``DevUser``, so ``is_platform_admin()`` and the dataclass defaults are the
#: production ones rather than a mock's opinion.
_USER_ID = "user-under-test"

#: One concrete request per operation, because a route cannot be driven through
#: its template. Hand-written, and then checked against the router below — a
#: new conversion route missed here fails
#: :func:`test_every_gated_conversion_service_route_is_driven` rather than
#: quietly going unmeasured.
#:
#: Every body is valid: a 422 from request validation would be a refusal this
#: battery did not mean to measure, and on the authenticated battery it would
#: stop the handler ever running.
DRIVEN: dict[tuple[str, str], dict] = {
    ("DELETE", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): {
        "url": "/api/v1/knowledge/conversions/c1/drafts/d1"
    },
    ("GET", "/api/v1/knowledge/conversions"): {"url": "/api/v1/knowledge/conversions"},
    ("GET", "/api/v1/knowledge/conversions/by-case/{case_id}"): {
        "url": "/api/v1/knowledge/conversions/by-case/case1"
    },
    ("GET", "/api/v1/knowledge/conversions/{conversion_id}"): {
        "url": "/api/v1/knowledge/conversions/c1"
    },
    ("GET", "/api/v1/knowledge/drafts"): {"url": "/api/v1/knowledge/drafts"},
    (
        "POST",
        "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}/verify",
    ): {"url": "/api/v1/knowledge/conversions/c1/drafts/d1/verify"},
    ("POST", "/api/v1/knowledge/convert"): {
        "url": "/api/v1/knowledge/convert",
        "data": {"scope": "personal"},
        "files": {"file": ("note.txt", b"anything", "text/plain")},
    },
    ("POST", "/api/v1/knowledge/drafts/verify-batch"): {
        "url": "/api/v1/knowledge/drafts/verify-batch",
        "json": {"draft_ids": [{"conversion_id": "c1", "draft_id": "d1"}]},
    },
    ("POST", "/api/v1/knowledge/runbooks/create"): {
        "url": "/api/v1/knowledge/runbooks/create",
        "json": {
            "title": "A runbook title long enough",
            "domain": "networking",
            "service": "gateway",
            "symptom_class": ["timeout"],
            "severity": "high",
            "scope": "personal",
            "symptom_recognition": "aaaaaaaaaaaa",
            "applicability": "aaaaaaaaaaaa",
            "diagnostic_steps": "aaaaaaaaaaaa",
            "causes": "aaaaaaaaaaaa",
            "prevention": "aaaaaaaaaaaa",
        },
    },
    ("POST", "/api/v1/knowledge/scan"): {"url": "/api/v1/knowledge/scan"},
    ("PUT", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): {
        "url": "/api/v1/knowledge/conversions/c1/drafts/d1",
        # ``DraftUpdateRequest.content`` is ``min_length=100``: a short body
        # would answer 422 for the authenticated caller and never reach the
        # handler, which is the one thing the third battery is about.
        "json": {"content": "# Runbook\n\n" + "a" * 120},
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
    """The knowledge surface, mounted as ``main`` mounts it, over dead services.

    Both routers, in ``main.py``'s order (``knowledge_router`` then
    ``conversion_router``, both at ``/api/v1``), so any path shadowing between
    the two files is reproduced rather than assumed away.
    """
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(conversion_router, prefix="/api/v1")
    for provider in _SERVICE_PROVIDERS:
        app.dependency_overrides[provider] = _explodes(
            f"{provider.__module__}.{provider.__name__}"
        )
    return app


def _conversion_service_operations(app: FastAPI) -> set[tuple[str, str]]:
    """Gated operations on this surface whose tree holds the conversion service.

    Through ``route_enumeration`` rather than a flat ``app.routes`` scan: on
    FastAPI >= 0.139 ``include_router`` records a ``_IncludedRouter``
    placeholder instead of copying the routes, so a flat scan of this app finds
    nothing. CI's pinned 0.136.0 still copies — the drift that module absorbs.
    """
    found = set()
    for path, methods, dependant in route_enumeration.iter_served_routes(app):
        names = _dependency_names(dependant)
        if _CONVERSION_SERVICE not in names or _GATE not in names:
            continue
        for method in methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, path))
    return found


def test_every_gated_conversion_service_route_is_driven():
    """The hand-written map is checked against what the routers actually serve.

    Without this the batteries below are a rule about the rows somebody
    remembered: a conversion route added tomorrow would simply not appear, and
    every assertion would still pass.
    """
    served = _conversion_service_operations(_surface())

    assert served, (
        "no gated route on the knowledge surface resolves the conversion "
        "service — the walk is not seeing the routers' dependency trees, and "
        "the batteries below would be quantified over nothing"
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
def test_no_conversion_route_resolves_a_service_for_an_unauthenticated_caller(
    headers, label
):
    """Every one of them answers 401, on an app where every provider raises.

    If the gate were solved after the conversion service — the shape #1494
    carried — each row would be a 500 raised out of :func:`_explodes`, because
    the provider is reached before anything checks the caller. Unoverridden the
    same rows answered 503, which is the same defect wearing the provider's own
    refusal.
    """
    client = TestClient(_surface(), raise_server_exceptions=False)

    answered = {}
    for (method, template), request in sorted(DRIVEN.items()):
        kwargs = {k: v for k, v in request.items() if k != "url"}
        response = client.request(method, request["url"], headers=headers, **kwargs)
        answered[f"{method} {template}"] = response.status_code

    not_refused = {k: v for k, v in answered.items() if v != 401}
    assert not not_refused, (
        f"with {label}, these conversion operations answered something other "
        "than 401 although every service provider on the route was made to "
        "raise — so a collaborator resolved before the auth gate, and an "
        "anonymous caller gets its failure in place of the refusal (#1494): "
        f"{not_refused}"
    )


class _Reply(dict):
    """Whatever the handler does with a service result, this survives it.

    Truthy even when empty (the ``if not result`` branches would otherwise
    become 404/500), ``model_dump()``-able, and indexable for the one handler
    that reads ``result["conversion_id"]`` / ``result["draft"]``.
    """

    def __bool__(self) -> bool:
        return True

    def model_dump(self) -> dict:
        return {}

    def __getitem__(self, key):
        return _Reply() if key == "draft" else "recorded"


class _RecordingConversionService:
    """Accepts any call the handlers make and records the kwargs it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        async def _call(**kwargs):
            self.calls.append((name, kwargs))
            return _Reply()

        return _call


def test_an_authenticated_caller_still_reaches_the_handler_with_its_principal():
    """The other half of the fix: the gate refuses, and injection is intact.

    The gate is declared TWICE on each of these routes — on the decorator and
    as ``current_user`` — because the handler bodies read the principal.
    FastAPI caches a dependency per request, so that is one resolution and one
    user; this asserts it rather than trusting it. A route that answers 401
    correctly and then 500s for a legitimate caller would be a worse defect
    than the one being fixed, and only a driven request can tell the two apart.
    """
    from datetime import datetime, timezone

    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.domain.models.auth import DevUser

    service = _RecordingConversionService()
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(conversion_router, prefix="/api/v1")
    app.dependency_overrides[_get_conversion_service] = lambda: service
    app.dependency_overrides[require_authentication] = lambda: DevUser(
        user_id=_USER_ID,
        username="under-test",
        email="under-test@example.invalid",
        display_name="Under Test",
        created_at=datetime.now(timezone.utc),
    )
    client = TestClient(app, raise_server_exceptions=False)

    answered = {}
    principals = {}
    for (method, template), request in sorted(DRIVEN.items()):
        kwargs = {k: v for k, v in request.items() if k != "url"}
        before = len(service.calls)
        response = client.request(method, request["url"], **kwargs)
        label = f"{method} {template}"
        answered[label] = response.status_code
        made = service.calls[before:]
        principals[label] = [
            call_kwargs.get("user_id") for _name, call_kwargs in made
        ] or None

    failed = {k: v for k, v in answered.items() if v >= 400}
    assert not failed, (
        "these conversion operations refused an AUTHENTICATED caller. The "
        "gate moved onto the decorator, so if the handler no longer receives "
        "its ``current_user`` this is where that shows up: " + repr(failed)
    )
    wrong = {k: v for k, v in principals.items() if v != [_USER_ID]}
    assert not wrong, (
        "these operations did not pass the authenticated principal through to "
        "the conversion service exactly once — the ``current_user`` parameter "
        f"is no longer being injected: {wrong}"
    )


def test_the_probe_bites_when_the_gate_is_declared_after_the_service():
    """The positive control: the same shape, built wrong, answers 500.

    Without this the batteries above pass on an app where nothing resolves at
    all, and "every row is 401" would say nothing about ordering. The gate here
    is the real ``require_authentication`` and the collaborator is the real
    overridden provider; only the DECLARATION ORDER differs.
    """
    from faultmaven.api.v1.auth_dependencies import require_authentication

    app = FastAPI()

    @app.get("/gate-after-the-service")
    async def _wrong(  # pragma: no cover - the body is never reached
        service=Depends(_get_conversion_service),
        current_user=Depends(require_authentication),
    ):
        return {}

    @app.get(
        "/gate-on-the-decorator",
        dependencies=[Depends(require_authentication)],
    )
    async def _right(  # pragma: no cover - the body is never reached
        service=Depends(_get_conversion_service),
        current_user=Depends(require_authentication),
    ):
        return {}

    app.dependency_overrides[_get_conversion_service] = _explodes(
        "the conversion service"
    )
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/gate-after-the-service").status_code == 500, (
        "the mis-ordered shape did not answer 500, so the overrides above are "
        "not making the provider fail and the batteries prove nothing"
    )
    assert client.get("/gate-on-the-decorator").status_code == 401, (
        "the fixed shape did not answer 401 — the route-level gate is not "
        "being solved ahead of the handler's own parameters on this FastAPI"
    )
