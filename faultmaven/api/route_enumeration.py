"""Every API route the app really serves, on every FastAPI this project runs on.

THE ONE PLACE the version gate lives. There were three copies before this
module — ``main.debug_routes``, the ``/admin/config/status`` mount reading, and
the guard in ``tests/integration/api/test_no_unauthenticated_operations.py`` —
and they had already drifted: two skipped empty paths, one demanded a
``dependant`` and one did not. A rule with three implementations is three rules.

**Why a flat ``app.routes`` scan is not enough, and why it is also not wrong.**
FastAPI 0.139 stopped copying an included router's routes into ``app.routes``
and records one ``_IncludedRouter`` placeholder per ``include_router`` instead,
moving BOTH the effective path and the resolved dependency tree onto a route
context. Before that, the eager copy merged the prefix into the path and the
contributed dependencies into the route's own dependant. Measured, on a router
included at ``prefix="/pre"`` with an app-level dependency, a router-level one
and a handler parameter:

    fastapi==0.136.0  (PINNED in requirements/{test,dev,cloud}.txt)
      app.routes types      ['APIRoute', 'Route']
      flat APIRoute paths   ['/direct', '/pre/leaf']                 COMPLETE
      route.dependant       [app_global, router_gate, handler_dep]   COMPLETE

    fastapi==0.141.1
      app.routes types      ['APIRoute', 'Route', '_IncludedRouter']
      flat APIRoute paths   ['/direct']                              20 of 144
      route.dependant       [handler_dep]                            INCOMPLETE
      ctx.path              '/pre/leaf'
      ctx.dependant         [app_global, router_gate, handler_dep]

So on the version that currently ships the flat arm is not a degraded fallback
— it is exactly right, and this module is a correct no-op there. It exists so
that the pin moving is a version bump rather than a silent loss of reach.

``original_router`` is deliberately NOT walked: it reaches the routes but yields
their UNPREFIXED paths and their handler-only dependants, which is wrong on both
counts.
"""

from __future__ import annotations

from typing import NamedTuple

from fastapi.routing import APIRoute

try:  # pragma: no cover - exercised on FastAPI >= 0.139
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - FastAPI < 0.139
    iter_route_contexts = None


class ServedRoute(NamedTuple):
    """One served operation: its EFFECTIVE path, its verbs, its RESOLVED tree.

    The third field is the point. On >= 0.139 ``route.dependant`` is not the
    tree that runs for a route reached through ``include_router`` — it carries
    only what the handler declares. Measured on the composed app under 0.141.1:
    132 of 147 routes had ``ctx.dependant is not route.dependant``, and all 132
    were missing the application-level tenant binder. Reading the wrong one
    means a gate contributed by ``include_router(..., dependencies=[...])`` is
    invisible and its whole router reads as unauthenticated.
    """

    path: str
    methods: frozenset
    dependant: object


def iter_served_routes(app) -> list[ServedRoute]:
    """Flatten ``app`` into its served API operations.

    Raises:
        RuntimeError: if this FastAPI yields a route context with no resolved
            dependant. Falling back to ``route.dependant`` there would silently
            reinstate the defect above, on a future version, with every caller
            still green — so it is refused loudly instead.
    """
    if iter_route_contexts is None:  # FastAPI < 0.139: the eager-copy shape
        return [
            ServedRoute(route.path, frozenset(route.methods or ()), route.dependant)
            for route in app.routes
            if isinstance(route, APIRoute)
        ]

    served: list[ServedRoute] = []
    for context in iter_route_contexts(app.routes):
        route = getattr(context, "route", None)
        if not isinstance(route, APIRoute):
            continue
        # ``getattr(..., None) is None`` rather than ``not hasattr(...)``:
        # ``RouteContext.__getattr__`` proxies to a record where ``dependant``
        # is a declared field DEFAULTING TO None, so a context that carries the
        # attribute set to None passes a ``hasattr`` check and then fails much
        # later inside whatever walks the tree.
        dependant = getattr(context, "dependant", None)
        if dependant is None:
            raise RuntimeError(
                f"{getattr(context, 'path', '?')}: this FastAPI's route context "
                "carries no resolved dependant, so the dependency tree that "
                "actually runs cannot be read. Do NOT fall back to "
                "route.dependant — on >= 0.139 that is the handler's tree only."
            )
        served.append(
            ServedRoute(
                context.path,
                frozenset(getattr(context, "methods", None) or ()),
                dependant,
            )
        )
    return served


def iter_documented_routes(app) -> list[ServedRoute]:
    """The served operations the OpenAPI document makes a claim about.

    A SEPARATE function rather than a fourth field on ``ServedRoute``, and the
    reason is not style. ``ServedRoute`` is unpacked positionally at seven call
    sites across three security suites — ``for path, methods, dependant in
    ...`` — so appending a member is a BREAKING change to this module's
    contract: it was, measured, 12 tests failing with "too many values to
    unpack" for one caller's convenience. Filtering here keeps both the
    version gate and the ``include_in_schema`` reading inside the one module
    that is allowed to touch the route object, and leaves the tuple alone.

    ``include_in_schema=False`` marks a route that is matched, dispatched and
    served like any other and simply not described — so this is the set to
    compare against the document, and ``iter_served_routes`` stays the set to
    compare against what the router will match.
    """
    if iter_route_contexts is None:  # FastAPI < 0.139: the eager-copy shape
        return [
            ServedRoute(route.path, frozenset(route.methods or ()), route.dependant)
            for route in app.routes
            if isinstance(route, APIRoute) and route.include_in_schema
        ]

    documented = []
    for context in iter_route_contexts(app.routes):
        route = getattr(context, "route", None)
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        dependant = getattr(context, "dependant", None)
        if dependant is None:
            raise RuntimeError(
                f"{getattr(context, 'path', '?')}: this FastAPI's route context "
                "carries no resolved dependant, so the dependency tree that "
                "actually runs cannot be read. Do NOT fall back to "
                "route.dependant — on >= 0.139 that is the handler's tree only."
            )
        documented.append(
            ServedRoute(
                context.path,
                frozenset(getattr(context, "methods", None) or ()),
                dependant,
            )
        )
    return documented


def serves_path_prefix(app, prefix: str) -> bool:
    """Does ``app`` serve any API route under ``prefix``?

    Read off the route table rather than off a flag some branch of the
    composition root remembered to set. For a security-audit observable the
    failure directions are not symmetric: being told "no debug surface" about a
    pod that has one is the answer that ends an investigation early, and a flag
    only one code path writes reports exactly that for any app composed another
    way.
    """
    return any(served.path.startswith(prefix) for served in iter_served_routes(app))
