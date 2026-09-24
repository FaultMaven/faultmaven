"""``api/route_enumeration``: the flat arm on the pin, the flattened arm past it.

Every CI job installs ``fastapi==0.136.0``, where ``include_router`` copies
eagerly and a flat ``app.routes`` scan is exactly right — so the ``>= 0.139``
arm cannot run there on its own. It is exercised here by INJECTION:
``iter_route_contexts`` is replaced with stub contexts shaped like the ones
FastAPI 0.141 yields, and each such test FAILS against a flat walk on the pinned
version. Beside them, a behavioural test builds a real app, asks the router what
it actually serves, and requires the enumeration to agree — a positive control
on the pin, and the load-bearing check the moment the pin moves. The same
pattern covers ``iter_served_routes`` in
``tests/integration/api/test_no_unauthenticated_operations.py``.

fm#1308: three readers still took the pre-0.139 shape for granted — the
tenant-binder probe's escape scan and the duplicate-registration guard walked
``app.routes`` flatly, and ``iter_documented_routes`` read the route's own
``include_in_schema`` where only the context carries an included router's
``False``. These pin the module half of each; the call-site half — that each
site reads through this module at all — is
``tests/integration/api/test_route_walk_sites_reach_included_routers.py``.
"""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Mount, Route

from faultmaven.api import route_enumeration

pytestmark = pytest.mark.unit

_ABSENT = object()


class _Context:
    """The part of ``fastapi.routing.RouteContext`` the module reads.

    ``starlette_route`` is set only when given, because on a real context it is
    ABSENT for a route added straight to the app (``RouteContext.__getattr__``
    proxies to the route itself, which has no such attribute) and present only
    for one reached through ``include_router``.
    """

    def __init__(
        self,
        route,
        path,
        methods=frozenset(),
        dependant=None,
        include_in_schema=True,
        starlette_route=_ABSENT,
    ):
        self.route = route
        self.path = path
        self.methods = methods
        self.dependant = dependant
        self.include_in_schema = include_in_schema
        if starlette_route is not _ABSENT:
            self.starlette_route = starlette_route


class _IncludedRouterPlaceholder:
    """What ``app.routes`` holds for an included router on >= 0.139.

    Neither a route nor a container: no ``path``, no ``methods``, no
    ``routes``. A flat scan over a table containing it finds nothing inside.
    """


def _handler():  # pragma: no cover - never dispatched
    return {}


def _sub_app():
    return Starlette(routes=[Route("/x", lambda request: PlainTextResponse("sub"))])


async def _plain(request):
    return PlainTextResponse("plain")


async def _socket(websocket):  # pragma: no cover - never dispatched
    await websocket.close()


# =============================================================================
# iter_unresolved_routes
# =============================================================================


def _unresolved(app) -> set:
    return {
        (r.kind, r.path, r.host) for r in route_enumeration.iter_unresolved_routes(app)
    }


_FASTAPI_DOCS = {
    ("Route", "/openapi.json", None),
    ("Route", "/docs", None),
    ("Route", "/docs/oauth2-redirect", None),
    ("Route", "/redoc", None),
}


def test_a_sub_app_inside_an_included_router_is_reported_by_the_flattened_arm(
    monkeypatch,
):
    """The >= 0.139 arm, executed on the pinned 0.136 by injection.

    The table below is the real 0.141.1 shape: the top-level Mount sits in
    ``app.routes`` and the included router is one opaque placeholder. A flat
    scan of it answers ``/top`` and never sees what the router carries — which
    on that version is served, beneath an app whose global dependencies never
    run for it. That is the escape ``test_the_real_app_binds_every_route``
    exists to find.
    """
    top = Mount("/top", app=_sub_app())
    monkeypatch.setattr(
        route_enumeration,
        "iter_route_contexts",
        lambda routes: [
            _Context(APIRoute("/leaf", _handler), "/pre/leaf", {"GET"}, object()),
            # Reached through include_router: the context's own path is empty
            # and the prefixed one is on the copy FastAPI dispatches to.
            _Context(
                Mount("/sub", app=_sub_app()),
                "",
                starlette_route=Mount("/pre/sub", app=_sub_app()),
            ),
            _Context(Route("/r", _plain), "", starlette_route=Route("/pre/r", _plain)),
            _Context(Host("nested.example.com", app=_sub_app()), ""),
            _Context(top, "/top"),
        ],
    )
    app = SimpleNamespace(routes=[top, _IncludedRouterPlaceholder()])

    assert _unresolved(app) == {
        ("Mount", "/pre/sub", None),
        ("Route", "/pre/r", None),
        ("Host", "", "nested.example.com"),
        ("Mount", "/top", None),
    }, (
        "the flattened arm did not run, or it read a context's empty path "
        "instead of the served one — a flat scan of this table sees only /top"
    )


def test_an_entry_whose_path_cannot_be_recovered_is_still_reported(monkeypatch):
    """Fail closed: an unnameable sub-app is still served.

    If a future FastAPI stops exposing the dispatched copy, the entry must not
    vanish — the tenant-binder probe exempts known paths, and a dropped entry
    would pass it.
    """
    monkeypatch.setattr(
        route_enumeration,
        "iter_route_contexts",
        lambda routes: [_Context(Mount("/sub", app=_sub_app()), "")],
    )

    assert _unresolved(SimpleNamespace(routes=[])) == {("Mount", "", None)}


def test_the_pre_0_139_arm_is_the_flat_scan(monkeypatch):
    """Complete there: the eager copy drops a nested Mount or Host.

    Built without ``include_router`` so that this also holds when the flat arm
    is forced on a FastAPI that records placeholders.
    """
    monkeypatch.setattr(route_enumeration, "iter_route_contexts", None)
    app = FastAPI()
    app.add_api_route("/leaf", _handler)
    app.add_api_websocket_route("/ws", _socket)
    app.mount("/top", app=_sub_app())
    app.host("top.example.com", app=_sub_app())
    app.add_route("/plain", _plain)

    assert _unresolved(app) == _FASTAPI_DOCS | {
        ("Mount", "/top", None),
        ("Host", "", "top.example.com"),
        ("Route", "/plain", None),
    }


def test_every_unresolved_entry_the_router_serves_is_enumerated():
    """Measured against the router, not against a belief about the version.

    Each shape is added once to the app and once to a router included at
    ``/pre``. Which of those the router really serves differs by version —
    on 0.136.0 the nested Mount and Host are 404, dropped by the eager copy;
    on 0.141.1 they are 200 — and the enumeration must equal what is served
    either way, so this test needs no version gate to be right on both.
    """
    router = APIRouter()
    router.mount("/sub", app=_sub_app())
    router.host("nested.example.com", app=_sub_app())
    router.add_route("/nested-route", _plain)
    router.add_api_route("/leaf", _handler)
    app = FastAPI()
    app.include_router(router, prefix="/pre")
    app.mount("/top", app=_sub_app())
    app.host("top.example.com", app=_sub_app())
    app.add_route("/top-route", _plain)
    client = TestClient(app)

    probes = {
        ("Mount", "/top", None): client.get("/top/x"),
        ("Mount", "/pre/sub", None): client.get("/pre/sub/x"),
        ("Host", "", "top.example.com"): client.get(
            "/x", headers={"host": "top.example.com"}
        ),
        ("Host", "", "nested.example.com"): client.get(
            "/pre/x", headers={"host": "nested.example.com"}
        ),
        ("Route", "/top-route", None): client.get("/top-route"),
        ("Route", "/pre/nested-route", None): client.get("/pre/nested-route"),
    }
    served = {
        entry for entry, response in probes.items() if response.status_code == 200
    }

    assert {
        ("Mount", "/top", None),
        ("Host", "", "top.example.com"),
        ("Route", "/top-route", None),
        ("Route", "/pre/nested-route", None),
    } <= served, "positive control: every top-level shape, and an included Route, serve"
    assert _unresolved(app) - _FASTAPI_DOCS == served


# =============================================================================
# iter_documented_routes: include_in_schema is the EFFECTIVE flag
# =============================================================================


def test_a_router_hidden_by_include_is_not_documented_on_the_flattened_arm(
    monkeypatch,
):
    """``include_router(r, include_in_schema=False)`` on >= 0.139, injected.

    The route keeps its handler-level ``include_in_schema=True`` and only the
    context carries the ``False``. Reading the route's flag counts the whole
    hidden router as documented — which the security gate then reports as a
    protected operation "published as open", about a path the document does
    not contain at all.
    """
    hidden = APIRoute("/hidden", _handler)
    shown = APIRoute("/shown", _handler)
    assert hidden.include_in_schema, "the fixture must carry the route-level True"
    monkeypatch.setattr(
        route_enumeration,
        "iter_route_contexts",
        lambda routes: [
            _Context(hidden, "/pre/hidden", {"GET"}, object(), False),
            _Context(shown, "/shown", {"GET"}, object(), True),
        ],
    )

    documented = route_enumeration.iter_documented_routes(SimpleNamespace(routes=[]))

    assert [served.path for served in documented] == ["/shown"]


def test_the_documented_set_is_the_document():
    """What ``iter_documented_routes`` claims is exactly what ``openapi()`` emits.

    Behavioural, so it holds on either arm: on the pin the eager copy folds the
    include-level flag into the route; past it, only the context has it.
    """
    hidden = APIRouter()
    hidden.add_api_route("/hidden-by-include", _handler)
    app = FastAPI()
    app.include_router(hidden, prefix="/pre", include_in_schema=False)
    app.add_api_route("/documented", _handler)

    documented = {s.path for s in route_enumeration.iter_documented_routes(app)}

    assert documented == set(app.openapi()["paths"]) == {"/documented"}


# =============================================================================
# iter_served_endpoints
# =============================================================================


def test_served_endpoints_take_path_and_flag_from_the_context(monkeypatch):
    """The effective path, the handler as written, the effective flag.

    Injected with a context whose path differs from its route's, so a reading
    of ``route.path`` (``/leaf``, the unprefixed handler-level spelling on
    >= 0.139) cannot pass for the served one.
    """
    route = APIRoute("/leaf", _handler, methods=["POST"])
    monkeypatch.setattr(
        route_enumeration,
        "iter_route_contexts",
        lambda routes: [
            _Context(Mount("/m", app=_sub_app()), "/m"),  # not an APIRoute
            _Context(route, "/pre/leaf", {"POST"}, None, False),
        ],
    )

    served = route_enumeration.iter_served_endpoints(SimpleNamespace(routes=[]))

    assert served == [
        route_enumeration.ServedEndpoint(
            "/pre/leaf", frozenset({"POST"}), _handler, False
        )
    ]


def test_the_pre_0_139_endpoint_arm_is_the_flat_scan(monkeypatch):
    monkeypatch.setattr(route_enumeration, "iter_route_contexts", None)
    app = FastAPI()
    app.add_api_route("/direct", _handler, include_in_schema=False)

    served = [
        entry
        for entry in route_enumeration.iter_served_endpoints(app)
        if entry.path == "/direct"
    ]

    assert served == [
        route_enumeration.ServedEndpoint("/direct", frozenset({"GET"}), _handler, False)
    ]


def test_served_endpoints_reach_what_the_router_serves_in_its_order():
    """Same reach and order as ``iter_served_routes``; documented flag agrees.

    Order matters to the duplicate-registration guard, which reports the FIRST
    match as the one served.
    """
    first = APIRouter()
    first.add_api_route("/a", _handler)
    first.add_api_route("/b", _handler, methods=["POST"])
    hidden = APIRouter()
    hidden.add_api_route("/c", _handler)
    app = FastAPI()
    app.add_api_route("/direct", _handler)
    app.include_router(first, prefix="/one")
    app.include_router(hidden, prefix="/two", include_in_schema=False)

    endpoints = route_enumeration.iter_served_endpoints(app)
    routes = route_enumeration.iter_served_routes(app)

    assert [(e.path, e.methods) for e in endpoints] == [
        (r.path, r.methods) for r in routes
    ]
    assert {e.path for e in endpoints} == {"/direct", "/one/a", "/one/b", "/two/c"}
    assert all(e.endpoint is _handler for e in endpoints)
    assert {e.path for e in endpoints if e.include_in_schema} == set(
        app.openapi()["paths"]
    )
