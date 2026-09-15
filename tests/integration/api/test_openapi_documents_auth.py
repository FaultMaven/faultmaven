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
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
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
    import os

    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    previous = os.environ.get("OAUTH_ENABLED")
    os.environ["OAUTH_ENABLED"] = "true"
    reset_settings()
    try:
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
# describes it. The two tests below compare how many routes there ARE against
# how many the spec describes — a question no gate downstream of
# ``app.openapi()`` can ask, because a duplicate route is invisible in the
# document it would have to read.
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
    try:
        return rebuild_app()
    finally:
        dotenv.load_dotenv, dotenv.dotenv_values = saved_dotenv
        os.environ.clear()
        os.environ.update(saved_environ)
        reset_settings()


def _served_operations(app):
    """Every operation the router will match, as ``(method, route)`` pairs.

    A list, in registration order, and never a set: a second registration of a
    ``(method, path)`` that already exists is the whole subject here, and it is
    only visible as a repeat.

    Both callers below key these on ``route.path_format`` rather than
    ``route.path``, because ``path_format`` is what the document keys its path
    items on — which makes the comparison apples-to-apples, and is also the more
    exact statement of the defect: two routes sharing a ``path_format`` are
    precisely the pair the document has no way to describe separately.
    """
    return [
        (method, route)
        for route in _schema_routes(app)
        for method in sorted(route.methods)
    ]


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
    """One handler per ``(method, path)``. The second one is unreachable.

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
    """
    by_operation = defaultdict(list)
    for method, route in _served_operations(published_app):
        by_operation[(method, route.path_format)].append(route)

    duplicates = {
        operation: routes
        for operation, routes in by_operation.items()
        if len(routes) > 1
    }

    report = []
    for (method, path), routes in sorted(duplicates.items()):
        report.append(f"  {method} {path}")
        for position, route in enumerate(routes, start=1):
            name = getattr(route.endpoint, "__name__", "<unnamed endpoint>")
            if position == 1:
                verdict = "SERVED — the router matches this one"
            elif position == len(routes):
                verdict = "DOCUMENTED — the spec describes this one"
            else:
                verdict = "SHADOWED — neither served nor documented"
            report.append(f"      {name}  ({_definition_site(route)})  <- {verdict}")

    assert not duplicates, (
        "These operations are registered more than once:\n"
        + "\n".join(report)
        + "\n\nThe two halves of FastAPI resolve that disagreement differently, "
        "and neither one errors:\n"
        "  - Starlette walks app.routes in order and serves the FIRST match.\n"
        "  - get_openapi() merges path items with dict.update(), so the "
        "document describes the LAST one.\n"
        "\nSo the published contract describes a handler nobody can reach, and "
        "`api-contract-drift` cannot see it: the document that job diffs IS "
        "app.openapi(), which already contains the wrong half.\n"
        "\nDelete one of the definitions. Do NOT rename the function or set "
        "`operation_id=` to quiet the linter or FastAPI's Duplicate Operation "
        "ID warning — that silences the detector and leaves the shadowed route "
        "in place, which is exactly how this shipped twice."
    )


@pytest.mark.integration
def test_served_and_documented_operation_counts_agree(published_app):
    """As many operations in the document as the router will match.

    This is the same defect seen from the other side, and it is the assertion
    that survives a future variant the uniqueness check above does not
    anticipate — a router mounted twice under one prefix, say, or a path
    rewritten to collide with one registered elsewhere.

    It has to be a COUNT. The *sets* agree in the duplicate case: both contain
    ``('POST', '/api/v1/sessions/cleanup')`` exactly because the extra
    registration collapsed into the same path item. Measured on the commit
    before #1440: 154 served, 153 documented, sets identical. A set comparison —
    and every gate downstream of ``app.openapi()`` — reads that as clean.
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
        f"The app serves {served_total} in-schema operations but the document "
        f"describes {documented_total}.\n" + "\n".join(detail) + "\n\n"
        "A path item is a dict keyed by method, so a second handler on a "
        "(method, path) that is already registered overwrites the first in the "
        "document while Starlette goes on serving the first. The counts are the "
        "only place that shows: the sets of (method, path) still agree, which "
        "is why the API-reference drift gate — and every other guard built on "
        "app.openapi() — reads it as clean.\n"
        "\nIf the numbers differ for some other reason, the route table and the "
        "generator have stopped agreeing about what an operation is, which is "
        "worth understanding before either side is changed."
    )
