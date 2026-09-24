"""Every route-walk call site reads through the flattener — proved on the pin.

On the pinned ``fastapi==0.136.0`` a flat ``app.routes`` walk is EXACT:
``include_router`` copies eagerly, so the flat table and the flattened one hold
the same routes. That makes reverting a call site to a flat walk invisible to
every other test on the pin. Measured: with every site reverted, the pinned
suite stayed green. Past the pin the walk loses most of the surface (0.141.1:
20 of 148 operations), and even there three sites stayed green, because nothing
on the real app exercised what they had lost: 0 mounts, 0 duplicate
registrations, and a documented-routes reader with no reach floor.

So each site is driven here with ``route_enumeration.iter_route_contexts``
replaced by the app's own routes as contexts, PLUS a canary context that is NOT
in ``app.routes``, shaped like what FastAPI 0.141 yields for a route reached
through ``include_router``. Only a walk that goes through the flattener can see
the canary, on either FastAPI; a flat walk misses it on both. Each case first
asserts the canary is invisible without the injection — a positive control
that the site is not seeing it for some other reason.

``test_every_flattener_call_site_is_registered`` keeps the list honest: it
finds every call into ``route_enumeration`` by scanning the source, and fails
on one that has no case here. What it cannot find is a walk that never calls
the flattener at all. fm#1308.
"""

import ast
import asyncio
import os
from pathlib import Path
from typing import Callable, NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Mount, Route, Router, WebSocketRoute

from faultmaven.api import route_enumeration

pytestmark = [pytest.mark.integration, pytest.mark.security]

REPO_ROOT = Path(__file__).resolve().parents[3]

try:  # FastAPI >= 0.139: the real flattener supplies the app's own contexts
    from fastapi.routing import iter_route_contexts as _fastapi_flattener
except ImportError:  # the pin: the eager copy is flat AND complete
    _fastapi_flattener = None

_ABSENT = object()


class _Context:
    """The part of ``fastapi.routing.RouteContext`` ``route_enumeration`` reads.

    Built from a route, with overrides for what an ``include_router`` context
    carries differently: its ``path`` is the effective one (and ``""`` for a
    non-API route, whose served copy is on ``starlette_route``), its
    ``dependant`` is the resolved tree, and its ``include_in_schema`` folds in
    the include's flag.
    """

    def __init__(
        self,
        route,
        path=None,
        include_in_schema=None,
        starlette_route=_ABSENT,
        methods=None,
        dependant=None,
    ):
        self.route = route
        self.path = getattr(route, "path", "") if path is None else path
        self.methods = getattr(route, "methods", None) if methods is None else methods
        self.dependant = (
            getattr(route, "dependant", None) if dependant is None else dependant
        )
        self.include_in_schema = (
            getattr(route, "include_in_schema", True)
            if include_in_schema is None
            else include_in_schema
        )
        if starlette_route is not _ABSENT:
            self.starlette_route = starlette_route


def _own_contexts(routes) -> list:
    if _fastapi_flattener is not None:
        return list(_fastapi_flattener(routes))
    return [_Context(route) for route in routes]


def _inject(monkeypatch, *canaries) -> None:
    monkeypatch.setattr(
        route_enumeration,
        "iter_route_contexts",
        lambda routes: [*_own_contexts(routes), *canaries],
    )


def _handler():  # pragma: no cover - never dispatched
    return {}


def _canary_handler():  # pragma: no cover - never dispatched
    return {}


def _sub_app():
    return Starlette(routes=[Route("/x", lambda request: PlainTextResponse("x"))])


async def _plain(request):  # pragma: no cover - never dispatched
    return PlainTextResponse("plain")


async def _socket(websocket):  # pragma: no cover - never dispatched
    await websocket.close()


def _app(*paths: str) -> FastAPI:
    app = FastAPI()
    for path in paths:
        app.add_api_route(path, _handler)
    return app


def _api_canary(path: str):
    return lambda app: [_Context(APIRoute(path, _handler))]


class Site(NamedTuple):
    """One call site: what it is asked about, its canary, how it reports it.

    ``covers`` is ``<path>::<function>`` of the function that calls into
    ``route_enumeration``, which is what the census matches against.
    """

    covers: str
    app: Callable[[], object]
    canaries: Callable[[object], list]
    sees_canary: Callable[[object], bool]


# -----------------------------------------------------------------------------
# The sites. Imported lazily so a module that fails to import fails ITS case.
# -----------------------------------------------------------------------------

_PROBE = "tests/integration/security/test_multi_tenant_isolation_probe.py"
_OPENAPI = "tests/integration/api/test_openapi_documents_auth.py"
_AUTHORIZE = "/api/v1/auth/oauth/authorize"


def _binder_probe():
    from tests.integration.security import test_multi_tenant_isolation_probe

    return test_multi_tenant_isolation_probe._routes_the_binder_does_not_cover


def _binder_probe_site(canary, kind, path="", host=None) -> Site:
    def sees(app):
        return any(
            (r.kind, r.path, r.host) == (kind, path, host) for r in _binder_probe()(app)
        )

    return Site(
        f"{_PROBE}::_routes_the_binder_does_not_cover",
        FastAPI,
        lambda app: [canary()],
        sees,
    )


def _openapi_auth():
    from tests.integration.api import test_openapi_documents_auth

    return test_openapi_documents_auth


def _sees_duplicate(app) -> bool:
    module = _openapi_auth()
    operations = module._served_operations(app)
    keys = [
        (method, module._matched_request_paths(served))
        for method, served in operations
        if served.endpoint is _canary_handler
    ]
    others = {
        (method, module._matched_request_paths(served))
        for method, served in operations
        if served.endpoint is not _canary_handler
    }
    return bool(keys) and all(key in others for key in keys)


def _sees_documented(app) -> bool:
    paths = {route.path for route in _openapi_auth()._schema_routes(app)}
    assert "/pre/hidden-by-include" not in paths, (
        "_schema_routes counted a route hidden by include_router(..., "
        "include_in_schema=False) as documented"
    )
    return "/pre/documented" in paths


def _surface_clone(module_name: str, helper_name: str, path: str):
    """A canary carrying the RESOLVED tree of a route the helper reports.

    The surface helpers select by what the tree holds (the gate and a service),
    so the canary must hold them too; it is a real reported route presented
    again at a path no router declares.
    """

    def canaries(app):
        import importlib

        helper = getattr(importlib.import_module(module_name), helper_name)
        reported = helper(app)
        template = next(
            served
            for served in route_enumeration.iter_served_routes(app)
            if any((method, served.path) in reported for method in served.methods)
        )
        return [
            _Context(
                APIRoute(path, _handler),
                methods=template.methods,
                dependant=template.dependant,
            )
        ]

    return canaries


def _surface_app(module_name: str):
    def build():
        import importlib

        return importlib.import_module(module_name)._surface()

    return build


def _surface_sees(module_name: str, helper_name: str, path: str):
    def sees(app):
        import importlib

        helper = getattr(importlib.import_module(module_name), helper_name)
        return any(reported_path == path for _, reported_path in helper(app))

    return sees


def _debug_app():
    """The real app, built with the debug router mounted."""
    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    previous = os.environ.get("ENVIRONMENT")
    os.environ["ENVIRONMENT"] = "development"
    reset_settings()
    try:
        return rebuild_app()
    finally:
        if previous is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = previous
        reset_settings()


def _debug_routes_reports(path: str):
    """Call the ``GET /debug/routes`` handler itself; its gate is on the route."""

    def sees(app):
        handler = next(
            served.endpoint
            for served in route_enumeration.iter_served_endpoints(app)
            if served.path == "/debug/routes"
        )
        rows = asyncio.run(handler())["routes"]
        return any(row["path"] == path for row in rows)

    return sees


def _call(module_name: str, name: str):
    def call(*args):
        import importlib

        return getattr(importlib.import_module(module_name), name)(*args)

    return call


_CASE_SURFACE = "tests.integration.security.test_unauthenticated_case_surface"
_KNOWLEDGE_SURFACE = "tests.integration.security.test_unauthenticated_knowledge_surface"

SITES = {
    # --- the global tenant binder's escape probe, one case per shape ---------
    "binder probe: Mount on the app": _binder_probe_site(
        lambda: _Context(Mount("/canary", app=_sub_app())), "Mount", "/canary"
    ),
    "binder probe: Mount in an included router": _binder_probe_site(
        lambda: _Context(
            Mount("/sub", app=_sub_app()),
            path="",
            starlette_route=Mount("/pre/sub", app=_sub_app()),
        ),
        "Mount",
        "/pre/sub",
    ),
    "binder probe: Host on the app": _binder_probe_site(
        lambda: _Context(Host("canary.example.com", app=_sub_app())),
        "Host",
        host="canary.example.com",
    ),
    "binder probe: Host in an included router": _binder_probe_site(
        lambda: _Context(
            Host("nested.example.com", app=_sub_app()),
            path="",
            starlette_route=Host(
                "nested.example.com",
                app=Router(routes=[Mount("/pre", app=_sub_app())]),
            ),
        ),
        "Host",
        host="nested.example.com",
    ),
    "binder probe: plain Route on the app": _binder_probe_site(
        lambda: _Context(Route("/canary-route", _plain)), "Route", "/canary-route"
    ),
    "binder probe: plain Route in an included router": _binder_probe_site(
        lambda: _Context(
            Route("/r", _plain), path="", starlette_route=Route("/pre/r", _plain)
        ),
        "Route",
        "/pre/r",
    ),
    "binder probe: WebSocketRoute": _binder_probe_site(
        lambda: _Context(WebSocketRoute("/canary-ws", _socket)),
        "WebSocketRoute",
        "/canary-ws",
    ),
    # --- test_openapi_documents_auth ------------------------------------------
    "duplicate-registration guard (_served_operations)": Site(
        f"{_OPENAPI}::_served_operations",
        lambda: _app("/pre/dup"),
        lambda app: [_Context(APIRoute("/pre/dup", _canary_handler))],
        _sees_duplicate,
    ),
    "auth-documentation gate (_schema_routes)": Site(
        f"{_OPENAPI}::_schema_routes",
        lambda: _app("/direct"),
        lambda app: [
            _Context(APIRoute("/pre/documented", _handler)),
            _Context(
                APIRoute("/pre/hidden-by-include", _handler),
                include_in_schema=False,
            ),
        ],
        _sees_documented,
    ),
    "OAuth-router premise (_served_paths)": Site(
        f"{_OPENAPI}::_served_paths",
        lambda: _app("/direct"),
        _api_canary("/pre/canary"),
        lambda app: "/pre/canary" in _openapi_auth()._served_paths(app),
    ),
    "served/documented count (_served_operation_counts)": Site(
        f"{_OPENAPI}::_served_operation_counts",
        lambda: _app("/direct"),
        _api_canary("/pre/canary"),
        lambda app: _openapi_auth()._served_operation_counts(app)[
            ("GET", "/pre/canary")
        ]
        == 1,
    ),
    # --- the other security suites --------------------------------------------
    "operator user confinement (_operator_user_operations)": Site(
        "tests/integration/api/test_operator_user_routes_are_confined.py"
        "::_operator_user_operations",
        lambda: _app("/direct"),
        _api_canary("/api/v1/admin/users/canary"),
        lambda app: ("GET", "/api/v1/admin/users/canary")
        in _call(
            "tests.integration.api.test_operator_user_routes_are_confined",
            "_operator_user_operations",
        )(app),
    ),
    "ungated session routes (_ungated_operations)": Site(
        "tests/integration/security/test_unauthenticated_session_surface.py"
        "::_ungated_operations",
        lambda: _app("/direct"),
        _api_canary("/api/v1/sessions/canary"),
        lambda app: "GET /api/v1/sessions/canary"
        in _call(
            "tests.integration.security.test_unauthenticated_session_surface",
            "_ungated_operations",
        )(app),
    ),
    "case-service surface (_case_service_operations)": Site(
        "tests/integration/security/test_unauthenticated_case_surface.py"
        "::_case_service_operations",
        _surface_app(_CASE_SURFACE),
        _surface_clone(_CASE_SURFACE, "_case_service_operations", "/pre/canary"),
        _surface_sees(_CASE_SURFACE, "_case_service_operations", "/pre/canary"),
    ),
    "conversion-service surface (_conversion_service_operations)": Site(
        "tests/integration/security/test_unauthenticated_knowledge_surface.py"
        "::_conversion_service_operations",
        _surface_app(_KNOWLEDGE_SURFACE),
        _surface_clone(
            _KNOWLEDGE_SURFACE, "_conversion_service_operations", "/pre/canary"
        ),
        _surface_sees(
            _KNOWLEDGE_SURFACE, "_conversion_service_operations", "/pre/canary"
        ),
    ),
    "no-unauthenticated-operations guard (_served_api_routes)": Site(
        "tests/integration/api/test_no_unauthenticated_operations.py"
        "::_served_api_routes",
        lambda: _app("/direct"),
        _api_canary("/pre/canary"),
        lambda app: any(
            path == "/pre/canary"
            for path, _methods, _dependant in _call(
                "tests.integration.api.test_no_unauthenticated_operations",
                "_served_api_routes",
            )(app)
        ),
    ),
    "authorize-leg fixture premise (_serves_the_authorize_leg)": Site(
        "tests/unit/api/test_admin_config_endpoints.py::_serves_the_authorize_leg",
        lambda: _app("/direct"),
        _api_canary(_AUTHORIZE),
        _call(
            "tests.unit.api.test_admin_config_endpoints", "_serves_the_authorize_leg"
        ),
    ),
    "session-is-not-identity sweep (_case_routes)": Site(
        "tests/unit/modules/case/api/test_session_is_not_identity.py::_case_routes",
        lambda: None,  # the site builds its own app around the case router
        _api_canary("/api/v1/cases/canary"),
        lambda app: "/api/v1/cases/canary"
        in {
            route.path
            for route in _call(
                "tests.unit.modules.case.api.test_session_is_not_identity",
                "_case_routes",
            )()
        },
    ),
    # --- production -----------------------------------------------------------
    "admin_config._oauth_flow_is_mounted": Site(
        "faultmaven/api/routes/admin_config.py::_oauth_flow_is_mounted",
        lambda: _app("/direct"),
        _api_canary(_AUTHORIZE),
        _call("faultmaven.api.routes.admin_config", "_oauth_flow_is_mounted"),
    ),
    "admin_config._debug_endpoints_are_mounted": Site(
        "faultmaven/api/routes/admin_config.py::_debug_endpoints_are_mounted",
        lambda: _app("/direct"),
        _api_canary("/debug/canary"),
        _call("faultmaven.api.routes.admin_config", "_debug_endpoints_are_mounted"),
    ),
    "GET /debug/routes (main.debug_routes)": Site(
        "faultmaven/main.py::debug_routes",
        _debug_app,
        _api_canary("/pre/canary-debug"),
        _debug_routes_reports("/pre/canary-debug"),
    ),
}


@pytest.mark.parametrize("name", sorted(SITES))
def test_the_site_reports_a_route_only_the_flattener_can_see(name, monkeypatch):
    site = SITES[name]
    app = site.app()

    assert not site.sees_canary(app), (
        "positive control: the site already reports the canary without the "
        "injected context, so this case cannot tell a flat walk from a "
        "flattened one"
    )

    _inject(monkeypatch, *site.canaries(app))

    assert site.sees_canary(app), (
        f"{name} did not report a route that is served but absent from "
        "app.routes — the shape FastAPI >= 0.139 gives every route reached "
        "through include_router. It is walking app.routes flatly: route it "
        "through faultmaven.api.route_enumeration."
    )


def test_the_binder_probe_passes_what_the_binder_covers(monkeypatch):
    """The other direction: API routes and the named exemptions stay quiet.

    Without this the probe could satisfy every case above by reporting
    everything. An ``APIWebSocketRoute`` is covered — measured, an app-level
    dependency runs for it — and FastAPI's four generated documentation routes
    are exempt by name.
    """
    app = FastAPI()
    assert {route.path for route in app.routes if isinstance(route, Route)} >= {
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }

    _inject(
        monkeypatch,
        _Context(APIRoute("/pre/api", _handler)),
        _Context(APIWebSocketRoute("/pre/api-ws", _socket)),
    )

    assert _binder_probe()(app) == []


# -----------------------------------------------------------------------------
# The census: every call into route_enumeration has a case above.
# -----------------------------------------------------------------------------

#: The public enumeration functions a call site reaches the flattener through.
_FLATTENER_ENTRY_POINTS = frozenset(
    {
        "iter_served_routes",
        "iter_documented_routes",
        "iter_served_endpoints",
        "iter_unresolved_routes",
        "serves_path_prefix",
    }
)

#: Files that call the flattener without being a call site, each with why.
_NOT_CALL_SITES = {
    "faultmaven/api/route_enumeration.py": "the module itself",
    "tests/unit/api/test_route_enumeration.py": (
        "the module's own tests, whose subject is the function they call"
    ),
    "tests/integration/api/test_route_walk_sites_reach_included_routers.py": (
        "this file"
    ),
}

#: A scan that walked nothing finds nothing and passes. Both floors are well
#: under today's numbers (over 1300 files; 15 call sites).
_MIN_FILES_SCANNED = 1000
_MIN_CALL_SITES = 15


class _CallSites(ast.NodeVisitor):
    """Functions that call an entry point, by name, attribute or import alias.

    Aliases are resolved because ``from ... import iter_served_routes as walk``
    would otherwise hide a call site from the census entirely. What it still
    cannot see is the function passed around as a value, or reached through
    ``getattr`` with a string — none of which any call site does today.
    """

    def __init__(self, relative: str):
        self.relative = relative
        self.functions: list[str] = []
        self.found: set[str] = set()
        self.names = set(_FLATTENER_ENTRY_POINTS)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name in _FLATTENER_ENTRY_POINTS and alias.asname:
                self.names.add(alias.asname)

    def _enter(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter

    def visit_Call(self, node):
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name in self.names:
            owner = self.functions[-1] if self.functions else "<module>"
            self.found.add(f"{self.relative}::{owner}")
        self.generic_visit(node)


def _flattener_call_sites() -> tuple[set[str], int]:
    found: set[str] = set()
    scanned = 0
    for top in ("faultmaven", "tests"):
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            scanned += 1
            if relative in _NOT_CALL_SITES:
                continue
            visitor = _CallSites(relative)
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            found |= visitor.found
    return found, scanned


def test_every_flattener_call_site_is_registered():
    """Every function that calls into ``route_enumeration`` has a case above.

    A new call site is exactly as blind to a flat revert as the fifteen this
    file was written for, so it fails here until it is registered with a
    canary. A registered site that no longer calls the flattener fails too: it
    was reverted, or it moved and its case is now testing nothing.
    """
    found, scanned = _flattener_call_sites()
    registered = {site.covers for site in SITES.values()}

    assert scanned >= _MIN_FILES_SCANNED, f"scanned only {scanned} files"
    assert len(found) >= _MIN_CALL_SITES, f"found only {sorted(found)}"
    assert found - registered == set(), (
        "these functions call into faultmaven.api.route_enumeration but have "
        "no case in SITES, so reverting them to a flat app.routes walk would "
        f"pass on the pinned FastAPI: {sorted(found - registered)}"
    )
    assert registered - found == set(), (
        "these registered sites no longer call into route_enumeration — "
        "reverted to a flat walk, or moved without their case: "
        f"{sorted(registered - found)}"
    )
