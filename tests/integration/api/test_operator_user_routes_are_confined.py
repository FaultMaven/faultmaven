"""No operator user route escapes the tenant predicate by being new (#1318).

The confinement itself is asserted in
``tests/unit/api/test_operator_user_admin_tenant_confinement.py``, which sweeps a
hand-written table of operations. A hand-written table is wrong the day after it
is written, and the failure mode is silent: a route added to
``/api/v1/admin/users*`` or ``/api/v1/auth/users*`` tomorrow would simply not be
swept, and every case there would still pass.

So the set of operations is **computed from the live application's OpenAPI
document** and compared with that table. A new operator user route fails this
module until someone decides whether it is confined and adds it — the same
discipline ``test_two_tenant_surface_probe.py`` applies to its route inventory,
narrowed to the surface #1318 is about.

Two independent things are asserted, because either alone is satisfiable
without the other:

* the operation is **swept** by the unit module (its table names it), and
* the operation **declares** ``get_operator_user_scope`` in its dependency tree,
  which is the structural half — a route that forgot the parameter cannot
  resolve a scope to ask, whatever its handler body says.

Neither proves the handler calls ``scope.admits``; that is what the unit sweep
and the two-tenant surface probe are for. What this stops is a route being added
without anyone deciding.
"""

import inspect

import pytest
from fastapi.routing import APIRoute

from tests.unit.api.test_operator_user_admin_tenant_confinement import (
    CONFINED_OPERATIONS,
)

#: The two path prefixes that make an operation part of this surface. Prefixes
#: rather than an enumeration: the point is to catch what nobody enumerated.
OPERATOR_USER_PREFIXES = ("/api/v1/admin/users", "/api/v1/auth/users")

#: The dependency every operation on this surface must declare.
SCOPE_DEPENDENCY = "faultmaven.api.operator_user_scope.get_operator_user_scope"

#: The operator-role gate, in its two forms — the routers use different user
#: representations and each has its own copy of the same policy.
OPERATOR_ROLE_DEPENDENCIES = frozenset(
    {
        "faultmaven.api.middleware.auth.require_platform_admin",
        "faultmaven.api.v1.auth_dependencies.require_platform_admin",
    }
)

#: Operations under those prefixes that are deliberately NOT confined, each with
#: the reason. Empty today — and it must stay a decision rather than an
#: oversight, which is why an exemption has to be written here to pass.
UNCONFINED_EXEMPTIONS: dict[tuple[str, str], str] = {}


@pytest.fixture(scope="module")
def app():
    """The full application, built here rather than found.

    Same reasoning as ``test_openapi_documents_auth.py``: reading the published
    singleton makes the surface under test depend on what else was imported
    first, and a gate that silently covers fewer routes is the failure it exists
    to catch.
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
    seen = seen if seen is not None else set()
    for dependency in dependant.dependencies:
        seen.add(_qualified_name(dependency.call))
        _dependency_names(dependency, seen)
    return seen


def _operator_user_operations(app) -> dict[tuple[str, str], APIRoute]:
    """Every operation the LIVE app exposes on the operator user surface."""
    found: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith(OPERATOR_USER_PREFIXES):
            continue
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            found[(method, route.path)] = route
    return found


@pytest.mark.integration
@pytest.mark.security
def test_the_surface_under_test_is_not_empty(app):
    """The gate must not pass by having nothing to check.

    Every assertion below quantifies over the discovered operations, so a build
    that mounts neither router passes trivially — silently.
    """
    discovered = _operator_user_operations(app)
    assert len(discovered) >= len(CONFINED_OPERATIONS), sorted(discovered)


@pytest.mark.integration
@pytest.mark.security
def test_every_operator_user_operation_is_swept_or_exempted(app):
    """A route added to this surface has to be decided about."""
    discovered = set(_operator_user_operations(app))
    unaccounted = discovered - CONFINED_OPERATIONS - set(UNCONFINED_EXEMPTIONS)

    assert not unaccounted, (
        "operator user-administration operations that no one has decided about: "
        f"{sorted(unaccounted)}. Confine them (api/operator_user_scope) and add "
        "them to ID_ADDRESSED_OPERATIONS / LISTING_OPERATIONS in "
        "tests/unit/api/test_operator_user_admin_tenant_confinement.py, or "
        "record an exemption with its reason in UNCONFINED_EXEMPTIONS here."
    )


@pytest.mark.integration
@pytest.mark.security
def test_the_sweep_does_not_name_operations_the_app_never_exposes(app):
    """The stale half. A table naming a removed route asserts nothing.

    Without this the gate above stays green while the sweep quietly covers a
    surface that no longer exists.
    """
    discovered = set(_operator_user_operations(app))
    stale = CONFINED_OPERATIONS - discovered

    assert not stale, (
        f"the confinement sweep names operations the app does not expose: "
        f"{sorted(stale)}"
    )


@pytest.mark.integration
@pytest.mark.security
def test_every_confined_operation_declares_the_scope_dependency(app):
    """The structural half: the route can actually resolve a scope to ask.

    A handler that forgot the parameter cannot consult the predicate however it
    is written, and that omission is invisible to a test that only reads status
    codes on the routes it remembered to sweep.
    """
    discovered = _operator_user_operations(app)

    missing = [
        operation
        for operation, route in discovered.items()
        if operation not in UNCONFINED_EXEMPTIONS
        and SCOPE_DEPENDENCY not in _dependency_names(route.dependant)
    ]

    assert not missing, (
        f"operator user operations with no tenant scope in their dependency "
        f"tree: {sorted(missing)}"
    )


@pytest.mark.integration
@pytest.mark.security
def test_every_operator_user_operation_still_requires_the_operator_role(app):
    """Unchanged, and asserted so it stays that way.

    The tenant predicate narrows WHOSE accounts an operator reaches; it must not
    be mistaken for the gate that decides WHO is an operator. An organization
    admin is refused this whole surface — the two-tenant surface probe asserts
    that behaviourally, with real tokens — and a route that traded the role gate
    for the new predicate would still look confined to every case above.
    """
    discovered = _operator_user_operations(app)

    unguarded = [
        operation
        for operation, route in discovered.items()
        if not (OPERATOR_ROLE_DEPENDENCIES & _dependency_names(route.dependant))
    ]

    assert not unguarded, (
        f"operator user operations reachable without the platform-admin role: "
        f"{sorted(unguarded)}"
    )
