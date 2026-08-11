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
"""

import inspect

import pytest
from fastapi.routing import APIRoute

# Dependencies that refuse an anonymous caller. A route whose tree contains one
# of these must carry `security` in the spec.
MANDATORY_AUTH_DEPENDENCIES = frozenset(
    {
        "faultmaven.api.v1.auth_dependencies.require_authentication",
        "faultmaven.api.v1.auth_dependencies.require_platform_admin",
        "faultmaven.api.middleware.auth.get_current_user",
        "faultmaven.api.middleware.auth.require_platform_admin",
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
