"""Every route that requires authentication must say so in the spec.

The drift gate (``scripts/generate_api_docs.py --check``) keeps the checked-in
reference equal to ``app.openapi()``. That is necessary but not sufficient: if
the *app's own* spec misdescribes a route, regenerating propagates the mistake
into the artifact and the gate goes green over it.

That is not hypothetical — it is what issue #880 turned out to be. Routes
authenticated through ``require_authentication`` resolved their token from a
plain ``Header(...)`` parameter, so no security scheme appeared in the
dependency tree and FastAPI emitted no ``security``. 49 routes that require a
token were documented as open, and every regeneration faithfully reproduced
that.

So this compares the spec against the *dependency graph* rather than against
another document. The graph is only a trustworthy oracle if the classification
of auth dependencies is complete, so that completeness is itself asserted:
anything defined in an auth module must be explicitly classified, and an
unclassified one fails rather than being silently treated as harmless. A new
mandatory dependency that repeats the #880 mistake would otherwise make both
sides agree at "no auth" and pass.

Reading ``app.routes`` is also what makes this file the right home for the two
route-table gates at the bottom, which have nothing to do with authentication.
Every *other* route-level guard in this repository is downstream of
``app.openapi()`` — the surface inventories, the API-reference drift job, the
schema-naming and response-declaration gates — so the whole family is blind by
construction to a route that is *served* but not *documented*. That is not a
hypothetical either: ``POST /api/v1/sessions/cleanup`` was defined twice in
``modules/auth/api/session.py``, Starlette served the first definition and
FastAPI's generator documented the second, and the published contract described
a response the server never returned until #1440 removed it. The drift gate
could not see it, because the document it diffs *is* ``app.openapi()``.

So the last two tests compare the route table against the document, which is
only possible somewhere that holds both.
"""

import inspect
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# The environment the published reference is generated under, imported rather
# than copied so it cannot go stale — see scripts/generation_environment.py.
# ``scripts/generate_api_docs.py`` applies it through
# ``_pin_generation_environment()``, which cannot be called from inside pytest
# because it empties ``os.environ`` and monkeypatches dotenv permanently; the
# ``published_app`` fixture applies the same two steps reversibly.
#
# APPENDED, never inserted at position 0. ``scripts/`` also contains a
# ``tests.py`` CLI runner, so putting it ahead of the project root would shadow
# THIS repository's ``tests`` package for the life of the process: the next
# uncached ``import tests`` resolves to the runner, and the fixtures' own
# ``from tests.integration._app_rebuild import rebuild_app`` fails with
# ``ModuleNotFoundError: No module named 'tests.integration'``. A prepend
# happens to work today only because pytest has already imported the real
# package by the time this module is collected — a property of the invocation,
# not of this file. Appending needs no such luck, and the membership guard
# keeps a repeated import from growing the path.
#
# ``tests/integration/api/test_openapi_generation_is_pinned.py`` carries the
# same two lines and still prepends. Hoisting both into one helper reaches
# beyond this file, so it is left alone here and noted instead; this copy no
# longer creates the hazard.
_SCRIPTS_DIRECTORY = str(PROJECT_ROOT / "scripts")
if _SCRIPTS_DIRECTORY not in sys.path:
    sys.path.append(_SCRIPTS_DIRECTORY)
from generation_environment import (  # noqa: E402, I001
    PINNED_ENVIRONMENT,
    _SYSTEM_ENVIRONMENT_KEYS,
)

# The verbs an OpenAPI path item may key an operation on. Everything else a
# path item can carry (``parameters``, ``summary``, ``x-`` extensions) is not an
# operation and must not be counted as one.
HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

# Dependencies that refuse an anonymous caller. A route whose tree contains one
# of these must carry `security` in the spec.
MANDATORY_AUTH_DEPENDENCIES = frozenset(
    {
        "faultmaven.api.v1.auth_dependencies.require_authentication",
        "faultmaven.api.v1.auth_dependencies.require_platform_admin",
        "faultmaven.api.middleware.auth.get_current_user",
        "faultmaven.api.middleware.auth.require_platform_admin",
        # The team-consent routers' shared context (ADR-017 D4). It depends on
        # ``require_authentication`` and resolves the caller's enterprise, so it
        # refuses an anonymous caller twice over. One name for both routers,
        # because one factory builds both — they differ only in the reason slug
        # a single-tenant deployment is refused with.
        "faultmaven.modules.auth.api.teams.resolve_team_context",
    }
)

# Dependencies that live in an auth module but do not, by themselves, make a
# route refuse anonymous callers. Listed explicitly so that the set above can
# be trusted as complete rather than merely plausible.
NON_MANDATORY_AUTH_DEPENDENCIES = frozenset(
    {
        # Optional authentication: returns None instead of raising.
        "faultmaven.api.v1.auth_dependencies.extract_bearer_token",
        "faultmaven.api.v1.auth_dependencies.get_current_user_optional",
        # Service providers — they resolve collaborators, not identity.
        "faultmaven.api.middleware.auth.get_auth_service",
        "faultmaven.api.v1.auth_dependencies.get_user_store",
        "faultmaven.modules.auth.api.oauth.get_oauth_service",
        "faultmaven.modules.auth.api.sso.get_sso_login_service",
        # Deployment-mode gate: 404s outside local auth mode. /login and
        # /register are deliberately reachable without credentials.
        "faultmaven.modules.auth.api.auth.require_local_mode",
        # Rate limiters: 429, and they apply equally to anonymous callers.
        "faultmaven.modules.auth.api.rate_limiting.require_oauth_rate_limit_authorize",
        "faultmaven.modules.auth.api.rate_limiting.require_oauth_rate_limit_revoke",
        "faultmaven.modules.auth.api.rate_limiting.require_oauth_rate_limit_token",
        "faultmaven.modules.auth.api.rate_limiting.require_sso_rate_limit_callback",
        "faultmaven.modules.auth.api.rate_limiting.require_sso_rate_limit_exchange",
        "faultmaven.modules.auth.api.rate_limiting.require_sso_rate_limit_login",
    }
)


@pytest.fixture(scope="module")
def app():
    """An app carrying every authenticated router, built here rather than found.

    Reading the published singleton used to work only by accident: the OAuth
    integration modules rebuilt ``faultmaven.main`` under ``OAUTH_ENABLED`` at
    import time and left the rebuilt app published, so whether this gate saw the
    OAuth operations depended on whether those modules had been collected.
    Running this file on its own covered none of them, and nothing said so — the
    assertions below simply had fewer routes to walk.

    fm#990 stopped that rebuild from leaking, which would have made the omission
    permanent instead of intermittent. So the routers are asked for explicitly:
    a documentation gate that silently covers a different surface depending on
    collection order is the same failure it exists to catch.
    """
    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    previous = os.environ.get("OAUTH_ENABLED")
    # Inside the try, so the finally below undoes it however this exits — see
    # ``published_app``, which had the same shape and far more to lose.
    try:
        os.environ["OAUTH_ENABLED"] = "true"
        reset_settings()
        return rebuild_app()
    finally:
        if previous is None:
            os.environ.pop("OAUTH_ENABLED", None)
        else:
            os.environ["OAUTH_ENABLED"] = previous
        reset_settings()


def _qualified_name(call) -> str:
    module = getattr(inspect.getmodule(call), "__name__", "")
    name = getattr(call, "__name__", type(call).__name__)
    return f"{module}.{name}" if module else name


def _dependency_names(dependant, seen=None):
    """Every callable in a route's dependency tree, transitively, qualified."""
    seen = seen if seen is not None else set()
    for dependency in dependant.dependencies:
        seen.add(_qualified_name(dependency.call))
        _dependency_names(dependency, seen)
    return seen


def _schema_routes(app):
    """Routes the spec makes a claim about.

    Routes with ``include_in_schema=False`` are deliberately absent from the
    document, so there is nothing to agree or disagree with.
    """
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema
    ]


def _requires_auth(route: APIRoute) -> bool:
    return bool(MANDATORY_AUTH_DEPENDENCIES & _dependency_names(route.dependant))


def _documented_as_secured(spec, route: APIRoute, method: str) -> bool:
    operation = spec.get("paths", {}).get(route.path, {}).get(method.lower(), {})
    # An empty or absent `security` means "no credentials required".
    return bool(operation.get("security"))


@pytest.mark.integration
def test_the_surface_under_test_includes_the_oauth_router(app):
    """The gate must not pass by having nothing to check.

    Every assertion in this module quantifies over the app's routes, so a build
    that mounts fewer routers passes more easily — silently. The OAuth router is
    the one that comes and goes with configuration here, so its presence is
    asserted rather than assumed.

    The SSO router is deliberately not asserted: it mounts only under
    ``sso_configured`` (``auth_mode=oauth`` plus real WorkOS credentials), so it
    is absent from this surface and was absent before fm#990 too. This gate has
    never covered it, and saying so is better than an assertion that would have
    to fake a credential to hold.
    """
    mounted = {route.path for route in app.routes if isinstance(route, APIRoute)}

    assert {
        path for path in mounted if "/auth/oauth/" in path
    }, f"no OAuth routes on the app under test: {sorted(mounted)[:20]}"


@pytest.mark.integration
def test_auth_dependencies_are_classified(app):
    """No auth dependency may go unclassified.

    This is what stops the oracle below from degrading into an allowlist that
    quietly stops covering things. If someone adds a dependency that refuses
    anonymous callers and does not classify it here, the route would be read as
    unauthenticated, the spec would agree, and the comparison would pass while
    documenting a protected route as open — the exact shape of #880.
    """
    found = set()
    for route in _schema_routes(app):
        for name in _dependency_names(route.dependant):
            module = name.rsplit(".", 1)[0]
            if module.startswith("faultmaven") and "auth" in module.lower():
                found.add(name)

    classified = MANDATORY_AUTH_DEPENDENCIES | NON_MANDATORY_AUTH_DEPENDENCIES
    unclassified = found - classified

    assert not unclassified, (
        "These dependencies are defined in an auth module but are not "
        "classified in this file:\n"
        + "\n".join(f"  {name}" for name in sorted(unclassified))
        + "\n\nAdd each to MANDATORY_AUTH_DEPENDENCIES if it refuses an "
        "anonymous caller, or to NON_MANDATORY_AUTH_DEPENDENCIES if it does "
        "not. Leaving it out would make the check below read the routes that "
        "use it as unauthenticated."
    )

    # Deliberately not asserting the converse (that every classified name is
    # still reachable): the OAuth, SSO and metrics routers are mounted
    # conditionally, so which dependencies exist depends on the configuration
    # the suite happens to run under. A classified-but-absent name is harmless;
    # an unclassified present one is not.


@pytest.mark.integration
def test_every_authenticated_route_documents_its_auth(app):
    """Auth in code and auth in the spec must be the same set.

    Checked in both directions. Under-reporting is the dangerous one — it
    publishes an authenticated route as open — but over-reporting is also a
    defect: it tells a client to send credentials where none are required, and
    it is how a well-meaning fix to the under-reporting sweeps in the
    optional-auth routes.
    """
    spec = app.openapi()

    undocumented = []
    overdocumented = []

    for route in _schema_routes(app):
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            required = _requires_auth(route)
            documented = _documented_as_secured(spec, route, method)
            if required and not documented:
                undocumented.append(f"{method} {route.path}")
            elif documented and not required:
                overdocumented.append(f"{method} {route.path}")

    assert not undocumented, (
        "These routes refuse anonymous callers but the spec documents them as "
        "open. Clients generated from it will not send credentials, and the "
        "published reference understates the auth surface:\n"
        + "\n".join(f"  {route}" for route in sorted(undocumented))
        + "\n\nThe usual cause is an auth dependency that reads the "
        "Authorization header directly instead of depending on a security "
        "scheme — FastAPI only emits `security` for schemes it can see."
    )

    assert not overdocumented, (
        "The spec requires credentials on these routes, but nothing in their "
        "dependency tree refuses an anonymous caller:\n"
        + "\n".join(f"  {route}" for route in sorted(overdocumented))
    )


@pytest.mark.integration
def test_declared_security_schemes_are_resolvable(app):
    """Every scheme an operation names must be defined in components.

    The previous generator injected `ApiKeyAuth`/`BearerAuth` placeholders
    while operations referenced `HTTPBearer`, leaving the committed spec with
    a security scheme that resolved to nothing. The CI check that was supposed
    to catch dangling references only walked `#/components/schemas/`.
    """
    spec = app.openapi()
    declared = set(spec.get("components", {}).get("securitySchemes", {}))

    referenced = {
        scheme
        for path_item in spec.get("paths", {}).values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for requirement in operation.get("security", [])
        for scheme in requirement
    }

    assert referenced <= declared, (
        f"operations reference security schemes that components.securitySchemes "
        f"does not define: {sorted(referenced - declared)}"
    )


# ---------------------------------------------------------------------------
# The route table itself: served-vs-documented parity.
#
# Everything above compares one property of a route against how the spec
# describes it. The two tests below ask instead which routes there ARE — one
# by looking for a second registration the router will never reach, the other
# by counting the table against the document. Neither question can be asked
# downstream of ``app.openapi()``, because a route the document cannot
# describe separately is invisible in the document it would have to read.
#
# The two do not overlap, and each sees a shape the other cannot: only the
# uniqueness check sees a HIDDEN route, or two spellings of one parameter;
# only the count sees a documented operation with nothing serving it.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def published_app():
    """The app this project publishes, built the way the generator builds it.

    ``scripts/generate_api_docs.py`` empties the environment down to
    ``_SYSTEM_ENVIRONMENT_KEYS`` and applies ``PINNED_ENVIRONMENT`` before
    importing the app, because several routers mount conditionally — OAuth, SSO,
    ``/metrics``, debug. The published document is therefore a function of that
    environment as well as of the code, and a gate that compares served routes
    against documented ones has to evaluate the *same* app or it is comparing
    two different surfaces.

    That is why this is not the ``app`` fixture above: that one sets only
    ``OAUTH_ENABLED``, so it mounts neither the SSO router nor ``/metrics``, and
    a route defined twice in either would be invisible to it.

    The generator's own ``_pin_generation_environment()`` cannot be called from
    inside pytest — it clears ``os.environ`` and replaces dotenv's loaders for
    the life of the process — so the same two steps are applied here and undone
    before the fixture hands the app back.
    """
    import dotenv

    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    saved_environ = dict(os.environ)
    saved_dotenv = (dotenv.load_dotenv, dotenv.dotenv_values)

    # EVERY mutation is inside the try. Both captures above are reads, so the
    # first thing that changes anything is already protected by the finally.
    #
    # This block used to run before the try, and what it does is not survivable
    # if it does not finish: the process environment is emptied to
    # ``_SYSTEM_ENVIRONMENT_KEYS`` and dotenv's two loaders are replaced with
    # stubs that return nothing. A raise from ``reset_settings()`` — or a
    # KeyboardInterrupt between any two of these lines — would leave the REST
    # OF THE SESSION with no ``DATABASE_URL``, no ``REDIS_URL``, no
    # ``OAUTH_ENABLED`` and a ``.env`` that silently reads as empty. The
    # symptom is a cascade of failures in unrelated tests, none of which names
    # this fixture. A module-scoped fixture that mutates global process state
    # gets exactly one chance to register its undo.
    try:
        # A local .env is exactly the ambient state that makes a document
        # irreproducible; the generator neutralises it for the same reason.
        dotenv.load_dotenv = lambda *args, **kwargs: None
        dotenv.dotenv_values = lambda *args, **kwargs: {}

        preserved = {
            key: value
            for key, value in os.environ.items()
            if key in _SYSTEM_ENVIRONMENT_KEYS
        }
        os.environ.clear()
        os.environ.update(preserved)
        os.environ.update(PINNED_ENVIRONMENT)
        reset_settings()
        return rebuild_app()
    finally:
        # Restore before re-reading: ``reset_settings()`` can itself raise, and
        # if it does the environment must already be back.
        dotenv.load_dotenv, dotenv.dotenv_values = saved_dotenv
        os.environ.clear()
        os.environ.update(saved_environ)
        reset_settings()


def _served_operations(app):
    """Every operation the router will match, as ``(method, route)`` pairs.

    A list, in registration order, and never a set: a second registration of a
    ``(method, path)`` that already exists is the whole subject here, and it is
    only visible as a repeat.

    ``include_in_schema`` is deliberately NOT applied — this is ``app.routes``,
    not ``_schema_routes``. That flag is a property of the *document*: a route
    carrying ``include_in_schema=False`` is matched, dispatched and served like
    any other, it is simply undescribed. Filtering the served side by it means
    asking the document which routes exist, which is precisely the assumption
    that makes every gate downstream of ``app.openapi()`` blind — and it hides
    the canonical #1440 shape rather than catching it. Register a hidden
    ``POST /x`` ahead of a documented ``POST /x`` and the router serves the
    hidden one while the document describes the other; through the filter that
    is a single, unremarkable route, both tests below pass, and the contract
    describes a handler nobody can reach. Measured: the two tests passed on the
    mutated app before this filter came off, and name the offender after.

    The *documented* side is of course filtered by it. That is what the flag is
    for, and it is the generator that applies it.
    """
    return [
        (method, route)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
    ]


# Starlette compiles ``/cases/{case_id}`` to ``^/cases/(?P<case_id>[^/]+)$``:
# the parameter NAME is part of the pattern. So neither ``path_format`` nor
# ``path_regex.pattern`` distinguishes "matches the same requests" from "is
# spelled the same" — ``/cases/{case_id}`` and ``/cases/{id}`` differ under
# both, while ``/cases/abc`` matches both and the first registration is the
# only one the router will ever reach. Stripping the group names leaves a key
# that is equal exactly when two routes accept the same set of request paths.
_PATH_PARAMETER_GROUP = re.compile(r"\(\?P<[^>]+>")


def _matched_request_paths(route: APIRoute) -> str:
    """The set of request paths a route matches, as a comparable key.

    Equality here means total overlap. Partial overlap — a typed convertor
    against an untyped parameter, ``/x/{id:int}`` versus ``/x/{name}``, where
    the first shadows the second for integers only — is not detected, and
    deciding it in general is not a thing a regex comparison can do. This app
    registers no convertors at all, so the exact case is the whole case here.
    """
    return _PATH_PARAMETER_GROUP.sub("(?:", route.path_regex.pattern)


def _definition_site(route: APIRoute) -> str:
    """Where a route's handler is written, as ``path/to/file.py:line``."""
    endpoint = inspect.unwrap(route.endpoint)
    try:
        source_file = inspect.getsourcefile(endpoint)
        line = inspect.getsourcelines(endpoint)[1]
    except (OSError, TypeError):  # pragma: no cover - C-implemented endpoint
        return "<source unavailable>"
    if source_file is None:  # pragma: no cover - C-implemented endpoint
        return "<source unavailable>"
    path = Path(source_file)
    try:
        path = path.relative_to(PROJECT_ROOT)
    except ValueError:  # pragma: no cover - installed rather than in-tree
        pass
    return f"{path}:{line}"


@pytest.mark.integration
def test_no_operation_is_registered_twice(published_app):
    """One handler per method and set of matched requests. The rest are dead.

    Python's last-definition-wins makes a duplicated route silent in the source:
    two ``@router.post("/cleanup")`` decorators in one module both register, and
    the module exports only the second function. Nothing in the file looks
    wrong, and the two halves of the framework then disagree about which one
    exists — see the failure message.

    ``F811`` catches this only when the *function name* is duplicated too, and
    FastAPI's own ``Duplicate Operation ID`` warning only when the derived
    operation id collides. Both were true of the ``/sessions/cleanup`` pair, and
    both were silenced by renaming rather than by deleting one (commit
    d53e5d3a1 gave them ``operation_id="..._v1"`` / ``"..._v2"`` and added an
    ``ignore`` filter for the warning). This asserts the property instead of the
    symptom: it holds however the handlers are named.

    Which means the key must not be a name either, and two of them are:

    - ``include_in_schema`` is not applied to the served side (see
      ``_served_operations``), so the canonical shape — a HIDDEN route ahead of
      a documented one — is visible here. It is the only test that can see it:
      the document has nothing to say about a route it does not contain, so a
      count cannot find it and no gate downstream of ``app.openapi()`` can.
    - the key is ``_matched_request_paths``, not ``path_format`` and not
      ``path_regex.pattern``. Both of those carry the parameter's SPELLING, so
      ``GET /cases/{case_id}`` and ``GET /cases/{id}`` look like two different
      operations under either. They are not: both match ``/cases/abc``,
      Starlette serves the first, and the document advertises both. Measured —
      ``path_regex.pattern`` is ``^/api/v1/cases/(?P<case_id>[^/]+)$`` against
      ``^/api/v1/cases/(?P<id>[^/]+)$``; the named group is why keying on it
      does not help.
    """
    by_operation = defaultdict(list)
    for method, route in _served_operations(published_app):
        by_operation[(method, _matched_request_paths(route))].append(route)

    duplicates = {
        operation: routes
        for operation, routes in by_operation.items()
        if len(routes) > 1
    }

    # Which registration the document ends up describing, decided the way
    # ``get_openapi()`` decides it: one path item per ``path_format``, merged
    # with ``dict.update()``, so the LAST in-schema route with a given
    # ``(method, path_format)`` wins — and a route that is out of schema, or
    # whose spelling nothing else shares, is in a different cell entirely.
    documented_by = {}
    for method, route in _served_operations(published_app):
        if route.include_in_schema:
            documented_by[(method, route.path_format)] = route

    report = []
    for (method, _), routes in sorted(
        duplicates.items(),
        key=lambda item: (item[0][0], item[1][0].path_format),
    ):
        report.append(f"  {method} {routes[0].path_format}")
        for position, route in enumerate(routes):
            served = "SERVED" if position == 0 else "not served"
            documented = (
                "DOCUMENTED"
                if documented_by.get((method, route.path_format)) is route
                else "not documented"
            )
            name = getattr(route.endpoint, "__name__", "<unnamed endpoint>")
            report.append(
                f"      [{served}, {documented}]  {name}  "
                f"{route.path_format}  ({_definition_site(route)})"
            )

    assert not duplicates, (
        "These operations are registered more than once — same method, same "
        "requests matched:\n"
        + "\n".join(report)
        + "\n\nThe two halves of FastAPI resolve that disagreement differently, "
        "and neither one errors:\n"
        "  - Starlette walks app.routes in order and serves the FIRST match. "
        "It neither knows nor cares what the parameter is called, or whether "
        "the route is in the schema.\n"
        "  - get_openapi() builds one path item per `path_format` and merges "
        "with dict.update(). Where two registrations share a spelling it "
        "describes the LAST; where they do not, it describes BOTH, including "
        "the one nobody can reach; and a route registered with "
        "`include_in_schema=False` it describes not at all, while the router "
        "goes on serving it.\n"
        "\nSo the published contract describes a handler nobody can reach, and "
        "`api-contract-drift` cannot see it: the document that job diffs IS "
        "app.openapi(), which already contains the wrong half.\n"
        "\nDelete one of the definitions. Do NOT rename the function, re-spell "
        "the path parameter, hide one with `include_in_schema=False`, or set "
        "`operation_id=` to quiet the linter or FastAPI's Duplicate Operation "
        "ID warning — each of those silences a detector and leaves the "
        "unreachable route in place, which is exactly how this shipped twice."
    )


@pytest.mark.integration
def test_served_and_documented_operation_counts_agree(published_app):
    """As many operations in the document as the router will match.

    The same defect seen from the other side, and the assertion that does not
    depend on recognising a duplicate *as* a duplicate: it compares two totals,
    so it also answers for an operation that appears on one side only — a path
    item with no route behind it, a router mounted twice under one prefix, a
    route the generator declined to emit.

    It has to be a COUNT. The *sets* agree in the duplicate case: both contain
    ``('POST', '/api/v1/sessions/cleanup')`` exactly because the extra
    registration collapsed into the same path item. Measured on the commit
    before #1440: 154 served, 153 documented, sets identical. A set comparison —
    and every gate downstream of ``app.openapi()`` — reads that as clean.

    The served side counts routes hidden with ``include_in_schema=False`` as
    served, because they are (see ``_served_operations``). The app registers
    none today, so the totals are exactly equal rather than equal-by-allowance,
    and a hidden route therefore fails here as surplus — with the *reason* it
    is unreachable, if it shadows something, named by the test above.

    What this does NOT catch, despite an earlier version of this docstring
    claiming it: a path rewritten to collide with one registered elsewhere.
    ``GET /cases/{case_id}`` and ``GET /cases/{id}`` match the same requests,
    but they are two ``path_format`` keys and so two path items — served and
    documented both rise together and the totals stay level. Measured, on the
    real app: adding ``GET /api/v1/cases/{id}`` beside ``{case_id}`` moved both
    counts from 148 to 149 and this test passed.
    ``test_no_operation_is_registered_twice`` is what sees it, because it keys
    on what the router MATCHES rather than on what the path is called.
    """
    spec = published_app.openapi()

    served = Counter(
        (method, route.path_format)
        for method, route in _served_operations(published_app)
    )
    documented = Counter(
        (method.upper(), path)
        for path, path_item in spec.get("paths", {}).items()
        for method, operation in path_item.items()
        if method.lower() in HTTP_METHODS and isinstance(operation, dict)
    )

    served_total = sum(served.values())
    documented_total = sum(documented.values())

    # Counter subtraction keeps only the positive side, which is what names the
    # offender: an operation served twice and documented once appears here as
    # one surplus registration.
    surplus = served - documented
    missing = documented - served

    detail = []
    if surplus:
        detail.append("  Served more often than documented:")
        detail += [
            f"      {method} {path}  — served {served[(method, path)]}x, "
            f"documented {documented[(method, path)]}x"
            for method, path in sorted(surplus)
        ]
    if missing:
        detail.append("  Documented more often than served:")
        detail += [
            f"      {method} {path}  — documented {documented[(method, path)]}x, "
            f"served {served[(method, path)]}x"
            for method, path in sorted(missing)
        ]

    assert served_total == documented_total, (
        f"The app serves {served_total} operations but the document "
        f"describes {documented_total}.\n" + "\n".join(detail) + "\n\n"
        "A path item is a dict keyed by method, so a second handler on a "
        "(method, path) that is already registered overwrites the first in the "
        "document while Starlette goes on serving the first. The counts are the "
        "only place that shows: the sets of (method, path) still agree, which "
        "is why the API-reference drift gate — and every other guard built on "
        "app.openapi() — reads it as clean.\n"
        "\nA surplus with no duplicate behind it is a route the document omits "
        "outright — normally `include_in_schema=False`, which stops the "
        "generator describing the route without stopping the router serving "
        "it. That is a served operation the published contract denies exists. "
        "If some route genuinely must be hidden, exempt THAT operation here, "
        "by name and with the reason; do not restore an `include_in_schema` "
        "filter on the served side, which would take both of these tests back "
        "to reading the document to find out which routes exist.\n"
        "\nIf the numbers differ for some other reason, the route table and the "
        "generator have stopped agreeing about what an operation is, which is "
        "worth understanding before either side is changed."
    )
