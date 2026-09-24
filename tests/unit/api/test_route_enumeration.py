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
tenant-binder probe's Mount scan and the duplicate-registration guard walked
``app.routes`` flatly, and ``iter_documented_routes`` read the route's own
``include_in_schema`` where only the context carries an included router's
``False``. These pin the module half of each.
"""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

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


def _serves(app, path: str) -> bool:
    return TestClient(app).get(path).status_code == 200


# =============================================================================
# iter_mount_paths
# =============================================================================


def test_a_mount_inside_an_included_router_is_reported_by_the_flattened_arm(
    monkeypatch,
):
    """The >= 0.139 arm, executed on the pinned 0.136 by injection.

    The table below is the real 0.141.1 shape: the top-level Mount sits in
    ``app.routes`` and the included router is one opaque placeholder. A flat
    scan of it answers ``['/top']`` and never sees ``/pre/sub`` — which on that
    version is served, beneath an app whose global dependencies never run for
    it. That is the escape ``test_the_real_app_binds_every_route`` exists to
    find.
    """
    top = Mount("/top", app=_sub_app())
    as_written = Mount("/sub", app=_sub_app())
    as_served = Mount("/pre/sub", app=_sub_app())
    monkeypatch.setattr(
        route_enumeration,
        "iter_route_contexts",
        lambda routes: [
            _Context(APIRoute("/leaf", _handler), "/pre/leaf", {"GET"}, object()),
            # A Mount reached through include_router: the context's own path is
            # empty and the prefixed one is on the copy FastAPI dispatches to.
            _Context(as_written, "", starlette_route=as_served),
            _Context(top, "/top"),
        ],
    )
    app = SimpleNamespace(routes=[top, _IncludedRouterPlaceholder()])

    assert route_enumeration.iter_mount_paths(app) == ["/pre/sub", "/top"], (
        "the flattened arm did not run, or it read the context's empty path "
        "instead of the served one — a flat scan of this table sees only /top"
    )


def test_a_mount_whose_path_cannot_be_recovered_is_still_reported(monkeypatch):
    """Fail closed: an unnameable mount is still a mount.

    If a future FastAPI stops exposing the dispatched copy, the entry must not
    vanish — the tenant-binder probe exempts known paths, and a dropped entry
    would pass it.
    """
    monkeypatch.setattr(
        route_enumeration,
        "iter_route_contexts",
        lambda routes: [_Context(Mount("/sub", app=_sub_app()), "")],
    )

    assert route_enumeration.iter_mount_paths(SimpleNamespace(routes=[])) == [""]


def test_the_pre_0_139_mount_arm_is_the_flat_scan(monkeypatch):
    """And it is complete there, because the eager copy drops a nested Mount."""
    monkeypatch.setattr(route_enumeration, "iter_route_contexts", None)
    router = APIRouter()
    router.add_api_route("/leaf", _handler)
    app = FastAPI()
    app.include_router(router, prefix="/pre")
    app.mount("/top", app=_sub_app())

    assert route_enumeration.iter_mount_paths(app) == ["/top"]


def test_every_mount_the_router_serves_is_enumerated():
    """Measured against the router, not against a belief about the version.

    On 0.136.0 ``GET /pre/sub/x`` is 404 — the eager copy dropped the Mount —
    so only ``/top`` is owed. On 0.141.1 it is 200, so both are, and a flat
    scan reports one of the two. Either way the enumeration must equal what is
    really served, so this test needs no version gate to be right on both.
    """
    router = APIRouter()
    router.mount("/sub", app=_sub_app())
    app = FastAPI()
    app.include_router(router, prefix="/pre")
    app.mount("/top", app=_sub_app())

    served = {prefix for prefix in ("/top", "/pre/sub") if _serves(app, prefix + "/x")}

    assert "/top" in served, "positive control: the top-level mount must serve"
    assert set(route_enumeration.iter_mount_paths(app)) == served


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
