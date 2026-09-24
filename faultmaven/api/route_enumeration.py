"""Every API route the app really serves, on every FastAPI this project runs on.

THE ONE PLACE the version gate lives for this app's own route table (the one
other reader is named at the end, with its reason). There were three copies
before this module — ``main.debug_routes``, the ``/admin/config/status`` mount
reading, and the guard in
``tests/integration/api/test_no_unauthenticated_operations.py`` — and they had
already drifted: two skipped empty paths, one demanded a ``dependant`` and one
did not. A rule with three implementations is three rules.

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

One other module imports the flattener, on purpose:
``api/middleware/route_policy._post_route_paths``. It answers a different
question — every POST path a request can reach, INCLUDING those inside a
``Mount`` or ``Host`` sub-application, because the repeat-suppressing
middlewares see those requests too — and it reports whether its enumeration is
complete. The functions here stop at this app's own ``APIRoute`` table by
design, so routing that walk through them would drop composed sub-app routes
and bring back fm#1305's refusal of a path the app really serves.
"""

from __future__ import annotations

from typing import NamedTuple

from fastapi.routing import APIRoute, APIWebSocketRoute

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

    The flag is read off the CONTEXT on >= 0.139, for the same reason as the
    tree. ``include_router(r, include_in_schema=False)`` hides every route of
    ``r``, and before 0.139 the eager copy folded that into each copied route's
    own flag. On 0.141.1 the route keeps its handler-level ``True`` and only
    the context carries the ``False`` — so reading ``route.include_in_schema``
    there counts a whole hidden router as documented. Measured: the document
    has no paths, and that reading reported ``['/pre/hidden-by-include']``.
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
        if not isinstance(route, APIRoute) or not context.include_in_schema:
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


class ServedEndpoint(NamedTuple):
    """One served operation, named by the handler that answers it.

    For a caller that has to say WHICH handler a path reaches — a duplicate
    registration is only reportable by its definition site — without being
    handed the route object, whose ``path``, ``path_regex`` and ``dependant``
    are the handler's own on >= 0.139 rather than the served ones. ``path`` is
    the effective, still-templated path; derive a matcher from it with
    ``starlette.routing.compile_path``, which is what ``APIRoute`` itself does.

    A separate tuple rather than new members on ``ServedRoute``, whose arity is
    part of its contract (see ``iter_documented_routes``).
    """

    path: str
    methods: frozenset
    endpoint: object
    include_in_schema: bool


def iter_served_endpoints(app) -> list[ServedEndpoint]:
    """Every served operation with its handler, in the order the router tries them.

    Order is load-bearing for the duplicate-registration guard: Starlette
    serves the FIRST route that matches, and the flattener expands each
    ``_IncludedRouter`` in place, so this order is the match order on both
    arms. ``include_in_schema`` is the effective flag — see
    ``iter_documented_routes`` for why that is the context's on >= 0.139.
    """
    if iter_route_contexts is None:  # FastAPI < 0.139: the eager-copy shape
        return [
            ServedEndpoint(
                route.path,
                frozenset(route.methods or ()),
                route.endpoint,
                bool(route.include_in_schema),
            )
            for route in app.routes
            if isinstance(route, APIRoute)
        ]

    served = []
    for context in iter_route_contexts(app.routes):
        route = getattr(context, "route", None)
        if not isinstance(route, APIRoute):
            continue
        served.append(
            ServedEndpoint(
                context.path,
                frozenset(getattr(context, "methods", None) or ()),
                route.endpoint,
                bool(context.include_in_schema),
            )
        )
    return served


class UnresolvedRoute(NamedTuple):
    """A served entry for which FastAPI resolves NO dependency tree.

    ``kind`` is the class name (``Mount``, ``Host``, ``Route``,
    ``WebSocketRoute``, or anything else a composer appends). ``path`` is the
    effective path, ``""`` for a ``Host`` (it matches on the host, and a
    prefix it was included under sits inside the app it wraps) or where
    unrecoverable. ``host`` is a ``Host``'s pattern. ``name`` is the route's own
    name, which is what FastAPI's generated documentation routes are known by.
    """

    kind: str
    path: str
    host: str | None
    name: str | None


#: The two route classes FastAPI builds a dependant for, and so the only two an
#: app-level ``dependencies=[...]`` reaches. Measured on 0.136.0 and 0.141.1
#: alike, with a global dependency that records each call: it ran for an
#: ``APIRoute`` and an ``APIWebSocketRoute``, top-level or included, and for
#: nothing else.
_DEPENDENCY_RESOLVING = (APIRoute, APIWebSocketRoute)


def iter_unresolved_routes(app) -> list[UnresolvedRoute]:
    """Everything this app serves that its own dependency tree never runs for.

    An app-level dependency — the global tenant binder is the one that matters
    — runs only where FastAPI resolves a dependant. A ``Mount`` or ``Host``
    hands the request to another application, and a plain Starlette ``Route``
    or ``WebSocketRoute`` calls its endpoint directly, so for each of those the
    binder never runs. That makes this list a security question, and its
    answer has the same version gate as the routes:

        added with                     0.136.0 (PINNED)      0.141.1
        app.mount / app.host /         served, in            served, in
          app.add_route                app.routes            app.routes
        router.add_route, included     served, copied in     served, NOT in
                                                             app.routes
        router.mount / router.host,    NOT served: the       SERVED, NOT in
          included                     eager copy drops it   app.routes

    So the flat scan is complete on the pin and blind past it, to exactly the
    shapes that become reachable. Measured under 0.141.1: ``GET /pre/sub/x``
    through an included router's Mount, and ``GET /pre/x`` through its Host,
    answer 200 with the app-level dependency never run. (On 0.136.0 a Host in a
    router included with NO prefix does not even get dropped quietly:
    ``include_router`` reads ``route.path`` off it and raises.)

    On >= 0.139 the context of a route reached through ``include_router``
    carries an empty ``path``; the served, prefixed one is on the copy FastAPI
    dispatches to, ``starlette_route``. When neither yields a path the entry is
    reported with ``""`` rather than dropped: an unnameable entry is still
    served, and a caller that exempts known paths must see it fail closed.
    """
    if iter_route_contexts is None:  # FastAPI < 0.139: the eager-copy shape
        return [
            _unresolved(route, route)
            for route in app.routes
            if not isinstance(route, _DEPENDENCY_RESOLVING)
        ]

    found = []
    for context in iter_route_contexts(app.routes):
        route = getattr(context, "route", None)
        if isinstance(route, _DEPENDENCY_RESOLVING):
            continue
        found.append(_unresolved(route, context))
    return found


def _unresolved(route, context) -> UnresolvedRoute:
    dispatched = getattr(context, "starlette_route", None)
    host = getattr(route, "host", None)
    path = (
        ""
        if host is not None
        else (getattr(dispatched, "path", None) or getattr(context, "path", None) or "")
    )
    return UnresolvedRoute(
        type(route).__name__, path, host, getattr(route, "name", None)
    )


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
