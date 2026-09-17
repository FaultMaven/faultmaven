"""Every operation the contract publishes as OPEN is one somebody chose.

The gap this closes, stated as the thing that was missing rather than as the
bug that exposed it (#1447): **there is no global authentication gate.** The
app's only global dependency is ``bind_request_enterprise_context`` — the
tenant binder, not authentication — so every route is individually responsible
for its own auth, and until this module nothing told you when one forgot. Five
session routes had, ``/admin/optimization/trigger-cleanup`` had — declared on
``app`` directly, with no ``/api/v1`` prefix, which is exactly how it escaped
the routers where the rule is enforced — and
all of them were found by a person reading rather than by CI.

HOW MANY IMPLEMENTATIONS OF THE NEIGHBOURING RULE EXIST, since a new guard
beside old ones has to say what it is not duplicating. Two, and the scan that
found them is ``grep -rn 'MANDATORY_AUTH_DEPENDENCIES|_requires_auth'`` over
``faultmaven/ tests/ scripts/``:

* ``tests/integration/api/test_openapi_documents_auth.py`` — the spec must
  agree with the dependency graph.
* ``tests/unit/modules/case/api/test_session_is_not_identity.py`` — the same
  classification, narrowed to session-addressed case routes (#1448), and
  spelled identically on purpose so the two cannot disagree.

Both ask *"does a route that requires auth SAY SO?"*. Neither asks *"is this
operation ALLOWED to be open?"* — and that second question is the whole of this
module. It needs no third copy of ``MANDATORY_AUTH_DEPENDENCIES``, because it
reads the published ``security`` key rather than walking the graph.

WHERE THIS RULE CAN BE VIOLATED, and the proof that this module looked there.
The rule can be broken by any *documented* operation, and the document is the
whole published surface — so :func:`test_the_document_under_test_is_the_whole_
surface` asserts the artifact parses, carries the version the code declares,
covers every router family by name, and that the open set is non-empty. A guard
that silently read an empty or trimmed document would pass by having nothing to
check, which is the failure mode this repository has already been bitten by.

The one place the rule can be broken that the CONTRACT cannot see is a route
that is served but not documented — ``include_in_schema=False``, or the
double-registration case where Starlette serves one definition and FastAPI
documents another. Two of those belong to
``test_served_and_documented_operation_counts_agree`` and
``test_no_operation_is_registered_twice`` in ``test_openapi_documents_auth.py``,
and this module asserts those guards still exist rather than assuming it.

**But that delegation was, as first written, a claim this module did not have.**
Both delegates build their app under the generator's ``PINNED_ENVIRONMENT``,
which pins ``ENVIRONMENT=production`` precisely to exclude the debug router — so
a route that only ever mounts OUTSIDE production is absent from their route
tables too, and their counts agree about it vacuously. Measured: under
``ENVIRONMENT=development`` (the shipped default — ``Environment.DEVELOPMENT``
in ``config/settings.py``, and ``.env.example`` ships the override commented
out) ``/debug/routes``, ``/debug/health``, ``/debug/config`` and
``/debug/llm-providers`` were served with NO auth dependency, and
``GET /debug/config`` answered an anonymous caller 200 with
``settings.get_configuration_summary()``. They were also served that way in
PRODUCTION wherever ``ENABLE_DEBUG_ENDPOINTS=true``, because the gate mounting
them is a disjunction rather than an environment check.

So the reach is extended rather than the claim narrowed:
:func:`test_no_conditionally_mounted_route_is_open_without_a_decision` builds
the app with the debug router mounted and applies the same rule to it, and
:func:`test_the_debug_router_in_production_is_an_explicit_opt_in` asserts the
gate that decides whether they mount at all.

#1474 closed the four with ``require_platform_admin``, so what those guards now
hold is a property rather than a deferral — which is why this module also drives
the routes through the request path
(:func:`test_the_debug_routes_refuse_anonymous_and_non_operator_callers`) and
pins the resolution ORDER the gate depends on
(:func:`test_the_debug_gate_resolves_before_anything_the_handler_declares`). A
dependency graph says a gate is present; only a request says it runs first.

Reading the COMMITTED artifact rather than ``app.openapi()`` is deliberate. It
is the document the clients are written against, it is kept equal to the app by
the ``api-contract-drift`` CI job, and reading it means a reviewer can diff the
allowlist against the same file the change publishes.
"""

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = PROJECT_ROOT / "docs" / "reference" / "api" / "openapi.json"

# The verbs an OpenAPI path item may key an operation on. Everything else a path
# item can carry (``parameters``, ``summary``, ``x-`` extensions) is not an
# operation.
HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: Open because it is meant to be. The reason is a claim about the route, so it
#: can be checked.
_PUBLIC = "public"

#: Open although it should not be, pending a fix that is not this PR's to make.
#: Must name the issue that tracks it — a deferral with no ticket is an
#: exemption with a nicer name.
_DEFERRED = "deferred"

#: Every operation the published contract declares with no ``security``, and why.
#:
#: Adding a line here is the review. An operation that reaches this file without
#: one fails :func:`test_no_operation_is_open_without_a_decision`; an entry whose
#: operation has since gained auth fails
#: :func:`test_no_entry_describes_an_operation_that_is_no_longer_open`, because a
#: stale entry is a green test asserting nothing.
PUBLIC_OPERATIONS: dict[tuple[str, str], tuple[str, str]] = {
    # --- service and deployment identity ----------------------------------
    ("GET", "/"): (
        _PUBLIC,
        "the service root: name, product version and status. A client has to "
        "be able to ask what it is talking to before it can authenticate to it.",
    ),
    ("GET", "/api/v1/meta/capabilities"): (
        _PUBLIC,
        "backend capability discovery for the extension and the dashboard. It "
        "is read before sign-in, to decide which sign-in to offer.",
    ),
    ("GET", "/v1/meta/capabilities"): (
        _PUBLIC,
        "the deprecated alias of the above, kept for installed extensions; it "
        "is the same handler and therefore the same decision.",
    ),
    # --- becoming authenticated -------------------------------------------
    #
    # Every operation in this group either exchanges a credential or tells a
    # caller how to obtain one. Requiring a token to reach them is the
    # circularity that has no bottom.
    ("GET", "/api/v1/auth/config"): (
        _PUBLIC,
        "which auth mode this deployment runs (local vs oauth) and where its "
        "endpoints are. A client cannot authenticate without it.",
    ),
    ("GET", "/api/v1/auth/health"): (
        _PUBLIC,
        "liveness of the auth subsystem, reported without reference to any "
        "user; it is the probe you reach for when nobody can sign in.",
    ),
    ("POST", "/api/v1/auth/login"): (
        _PUBLIC,
        "credential exchange: the password IS the credential presented. "
        "``require_local_mode`` 404s it outside local auth mode.",
    ),
    ("POST", "/api/v1/auth/register"): (
        _PUBLIC,
        "account creation in local auth mode; there is no prior account to "
        "authenticate as. ``require_local_mode`` 404s it elsewhere.",
    ),
    ("POST", "/api/v1/auth/dev-login"): (
        _PUBLIC,
        "the local-mode developer sign-in; same handler and same "
        "``require_local_mode`` 404 as ``/auth/login``.",
    ),
    ("POST", "/api/v1/auth/dev-register"): (
        _PUBLIC,
        "the local-mode developer registration; same handler and same "
        "``require_local_mode`` 404 as ``/auth/register``.",
    ),
    ("POST", "/api/v1/auth/refresh"): (
        _PUBLIC,
        "the refresh token in the body is the credential; an access token is "
        "exactly what the caller does not have when it calls this.",
    ),
    ("POST", "/api/v1/auth/oauth/token"): (
        _PUBLIC,
        "RFC 6749 §3.2 token endpoint: the grant (code + PKCE verifier, or a "
        "refresh token) is the credential, and it is in the body by the RFC.",
    ),
    ("POST", "/api/v1/auth/oauth/revoke"): (
        _PUBLIC,
        "RFC 7009 revocation: the token being revoked is the credential "
        "presented, and the RFC requires the endpoint to accept it that way.",
    ),
    ("GET", "/api/v1/auth/sso/login"): (
        _PUBLIC,
        "the SSO entry point — it redirects an anonymous browser to the "
        "identity provider, which is where a credential is obtained.",
    ),
    ("GET", "/api/v1/auth/sso/callback"): (
        _PUBLIC,
        "where the identity provider returns the browser; the provider's code "
        "is the credential, and no FaultMaven token exists yet.",
    ),
    ("POST", "/api/v1/auth/sso/exchange"): (
        _PUBLIC,
        "trades the SSO one-time code for FaultMaven tokens; the code is the "
        "credential, which is the whole point of the exchange.",
    ),
    # --- probes and operator diagnostics ----------------------------------
    ("GET", "/health"): (
        _PUBLIC,
        "the deployment liveness probe. Kubernetes presents no credential, "
        "and a probe that can fail on auth reports the wrong thing.",
    ),
    ("GET", "/readiness"): (
        _PUBLIC,
        "the Kubernetes readiness probe, for the same reason as ``/health``.",
    ),
    ("GET", "/health/dependencies"): (
        _PUBLIC,
        "per-dependency health for an operator. It is documented as a bare "
        "curl in the user guide and the operations runbook, which is the "
        "decision being recorded here rather than one taken now.",
    ),
    ("GET", "/health/components/{component_name}"): (
        _PUBLIC,
        "one component's health; same operator surface as "
        "``/health/dependencies`` and the same decision.",
    ),
    ("GET", "/health/logging"): (
        _PUBLIC,
        "health of the logging subsystem; same operator surface and decision.",
    ),
    ("GET", "/health/patterns"): (
        _PUBLIC,
        "error-pattern detection health; same operator surface and decision.",
    ),
    ("GET", "/health/sla"): (
        _PUBLIC,
        "SLA tracking health; same operator surface and decision.",
    ),
    ("GET", "/metrics"): (
        _PUBLIC,
        "the Prometheus scrape endpoint, mounted only under "
        "``METRICS_EXPORTER=prometheus_http``. Prometheus presents no bearer, "
        "and confining the scrape is the deployment's network job.",
    ),
    ("GET", "/metrics/performance"): (
        _PUBLIC,
        "aggregate performance counters, no per-user or per-case content; the "
        "same operator-metrics surface as the Prometheus scrape above.",
    ),
    ("GET", "/metrics/realtime"): (
        _PUBLIC,
        "live aggregate counters, same operator-metrics surface and decision.",
    ),
    ("GET", "/metrics/alerts"): (
        _PUBLIC,
        "current alert status, same operator-metrics surface and decision.",
    ),
    ("GET", "/metrics/optimization"): (
        _PUBLIC,
        "system optimization counters, same operator-metrics surface and "
        "decision. Its sibling ``/admin/optimization/trigger-cleanup`` is NOT "
        "here: that one has a write effect and took auth in #1447.",
    ),
    ("GET", "/api/v1/cases/health"): (
        _PUBLIC,
        "liveness of the case subsystem, reported without reading any case; "
        "the case module's counterpart to ``/api/v1/auth/health``.",
    ),
    # --- tombstones -------------------------------------------------------
    #
    # Both raise 410 unconditionally and reach no service. They authenticate
    # nobody because they do nothing; adding a dependency would only make the
    # refusal slower and give an anonymous caller a 401 where the honest answer
    # is "this endpoint is gone".
    ("POST", "/api/v1/cases/{case_id}/data"): (
        _PUBLIC,
        "a 410 Gone tombstone (``upload_case_data_gone``) pointing callers at "
        "``POST /cases/{case_id}/turns``; it touches no case and no service.",
    ),
    ("POST", "/api/v1/cases/{case_id}/queries"): (
        _PUBLIC,
        "a 410 Gone tombstone (``submit_case_query_gone``) pointing callers at "
        "``POST /cases/{case_id}/turns``; it touches no case and no service.",
    ),
    # --- knowledge: optional auth, and the visibility that makes it safe ---
    #
    # These three take ``get_current_user_optional``, so they are open by
    # design rather than by omission. What makes that safe is the repository's
    # visibility predicate, ``_inventory_visible``: global ∪ owned
    # (``owner_id == user_id``) ∪ shared-to-my-teams. An anonymous caller
    # resolves to ``user_id = None``, so only the global arm can admit a row —
    # the shipped runbook tier, which carries no tenant.
    ("GET", "/api/v1/knowledge/documents"): (
        _PUBLIC,
        "optional auth by design; an anonymous caller resolves to no user id, "
        "so the repository's visibility predicate admits the global runbook "
        "tier only — never a personal or team-shared item.",
    ),
    ("POST", "/api/v1/knowledge/search"): (
        _PUBLIC,
        "optional auth by design; same visibility predicate, so an anonymous "
        "search reaches the global runbook tier only.",
    ),
    ("POST", "/api/v1/knowledge/documents/search"): (
        _PUBLIC,
        "optional auth by design; same visibility predicate, so an anonymous "
        "full-text search reaches the global runbook tier only.",
    ),
    # --- open although it should not be -----------------------------------
    ("POST", "/api/v1/sessions"): (
        _DEFERRED,
        "the mint. Requiring auth here is a coordinated two-repository release "
        "(#1460), not a server fix: faultmaven-copilot mints header-less when "
        "a token refresh stumbles and treats a credential-less 401 as "
        "recoverable, so flipping the server alone is a mint -> 401 -> re-mint "
        "loop in the field. Client tolerant first, then the server.",
    ),
    ("POST", "/api/v1/sessions/{session_id}/heartbeat"): (
        _DEFERRED,
        "coupled to the mint above (#1460). While the mint is "
        "anonymous-capable, anonymously-minted sessions exist and "
        "``require_authentication`` would refuse their owner; worse, a "
        "signed-in copilot can hold one (the panel mints at mount, sign-in "
        "does not re-mint), so an ownership check would 403 forever and the "
        "only caller swallows the error. It closes with the mint.",
    ),
}

#: Operations that are served, unauthenticated, and NOT in the published
#: contract, because the router carrying them mounts only outside production.
#: Same rule as ``PUBLIC_OPERATIONS``, applied to the surface the contract
#: cannot describe.
#:
#: **Empty since #1474, and kept rather than deleted.** It held the debug
#: router's four ungated routes — ``/debug/routes``, ``/debug/health``,
#: ``/debug/llm-providers``, ``/debug/config`` — as ``_DEFERRED``, which is the
#: disposition for "open although it should not be". They are now gated with
#: ``require_platform_admin``, matching ``/debug/cases/{case_id}/causal-graph``,
#: which already required auth; the entries came out because a ``_DEFERRED``
#: entry outliving its finding is a green test asserting nothing, and
#: :func:`test_no_conditionally_mounted_route_is_open_without_a_decision`
#: fails on a stale one in exactly the way it fails on a new open route.
#:
#: An empty allowlist is not a dead one. The conditionally-mounted surface is
#: where the contract-derived half of this module is blind, so the *next*
#: ungated debug route has to arrive here with a reason and an issue number
#: rather than merely arriving. What is asserted in its absence is the positive
#: control below: the router still MOUNTS under development, so "no open
#: operation" means the routes are closed and not that nothing was looked at.
DEBUG_OPERATIONS: dict[tuple[str, str], tuple[str, str]] = {}

#: Router families the published surface must contain for this module to be
#: looking at the whole of it. One representative operation each — a router that
#: fails to mount takes its paths with it, and a guard that quantifies over a
#: smaller surface passes more easily and says nothing about it.
REPRESENTATIVE_OPERATIONS = (
    ("GET", "/health"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/oauth/token"),
    ("GET", "/api/v1/auth/sso/login"),
    ("GET", "/api/v1/sessions"),
    ("GET", "/api/v1/cases"),
    ("POST", "/api/v1/knowledge/search"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/invitations"),
    ("GET", "/api/v1/admin/cases"),
    ("GET", "/api/v1/reports/{report_id}"),
)


def _contract() -> dict:
    return json.loads(CONTRACT.read_text())


def _operations(spec: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in spec.get("paths", {}).items()
        for method in item
        if method in HTTP_METHODS
    }


def _open_operations(spec: dict) -> set[tuple[str, str]]:
    """Operations declaring no ``security``.

    An absent ``security`` and an empty one mean the same thing in OpenAPI —
    no credential required — so both are collected.
    """
    return {
        (method.upper(), path)
        for path, item in spec.get("paths", {}).items()
        for method, operation in item.items()
        if method in HTTP_METHODS and not operation.get("security")
    }


def _format(operations) -> str:
    return "\n".join(f"  {method} {path}" for method, path in sorted(operations))


@pytest.mark.integration
@pytest.mark.security
def test_the_document_under_test_is_the_whole_surface():
    """The guard must not pass by having nothing to check.

    Everything below quantifies over the published operations, so a document
    that failed to parse, went empty, or lost a router would make every other
    assertion in this module vacuously true. This is the assertion that says
    the module looked where its rule can be violated.
    """
    spec = _contract()
    operations = _operations(spec)

    from faultmaven.api.contract_version import API_CONTRACT_VERSION

    assert spec["info"]["version"] == API_CONTRACT_VERSION, (
        "the committed contract and the version the code declares disagree — "
        f"{spec['info']['version']!r} vs {API_CONTRACT_VERSION!r}. Regenerate "
        "with `python scripts/generate_api_docs.py`."
    )

    missing = [key for key in REPRESENTATIVE_OPERATIONS if key not in operations]
    assert not missing, (
        "the published contract is missing operations from router families "
        "this guard is supposed to cover, so it is reading a smaller surface "
        f"than it claims:\n{_format(missing)}"
    )

    assert _open_operations(spec), (
        "no operation in the contract declares an absent `security` — that is "
        "not a plausible state for this application, so the document is more "
        "likely malformed than the surface fully closed."
    )


@pytest.mark.integration
@pytest.mark.security
def test_no_operation_is_open_without_a_decision():
    """An operation published as open and not listed here is an unreviewed one.

    This is the half that would have caught #1447 on the commit that
    introduced it: four session routes and one ``/admin`` route reached the
    published contract with no ``security`` and nobody decided that.
    """
    undeclared = _open_operations(_contract()) - set(PUBLIC_OPERATIONS)

    assert not undeclared, (
        "these operations are published with NO authentication and are not in "
        "PUBLIC_OPERATIONS. Either give them an auth dependency, or add an "
        "entry saying why an anonymous caller may reach them:\n" + _format(undeclared)
    )


@pytest.mark.integration
@pytest.mark.security
def test_no_entry_describes_an_operation_that_is_no_longer_open():
    """A stale entry is a green test asserting nothing.

    The other direction, and the reason the two ``_DEFERRED`` entries are safe
    to carry: the moment #1460 closes them, this fails and the entries have to
    come out. An allowlist that only ever grows stops being a review.
    """
    spec = _contract()
    stale = set(PUBLIC_OPERATIONS) - _open_operations(spec)
    gone = {key for key in stale if key not in _operations(spec)}
    secured = stale - gone

    assert not secured, (
        "these PUBLIC_OPERATIONS entries name operations that now require "
        "authentication — remove the entries:\n" + _format(secured)
    )
    assert not gone, (
        "these PUBLIC_OPERATIONS entries name operations the contract no "
        "longer publishes at all — remove the entries:\n" + _format(gone)
    )


@pytest.mark.integration
@pytest.mark.security
def test_every_entry_states_a_reason_and_every_deferral_names_its_issue():
    """A reason is a claim about the route, so it can be checked by a reviewer.

    ``_DEFERRED`` additionally has to name the issue that will close it, for
    the same reason the surface probe's ``_FINDING`` does: a known-open route
    with no ticket is an exemption wearing a better word.
    """
    for (method, path), (disposition, reason) in PUBLIC_OPERATIONS.items():
        assert disposition in (
            _PUBLIC,
            _DEFERRED,
        ), f"{method} {path}: unknown disposition {disposition!r}"
        assert (
            len(reason.strip()) > 40
        ), f"{method} {path}: a reason has to say what makes the route public"
        if disposition == _DEFERRED:
            assert (
                "#" in reason
            ), f"{method} {path}: a deferral must name the issue that tracks it"


def _app_under(**overrides):
    """The served app, built under a PINNED environment plus ``overrides``.

    Pinned the way ``published_app`` pins — empty the environment down to
    ``_SYSTEM_ENVIRONMENT_KEYS``, neutralise dotenv's two loaders, then apply
    ``PINNED_ENVIRONMENT`` — rather than setting one or two variables on top of
    whatever the session happens to hold. Both callers below decide what is
    served by reading ``ENVIRONMENT`` and ``ENABLE_DEBUG_ENDPOINTS``, and an
    ambient value for either (a developer's ``.env``, a sibling module that
    leaked one) would make them functions of the machine rather than of the
    code. An earlier version overrode two variables and inherited the rest;
    that is the bug this docstring exists to stop coming back.

    Everything that mutates global state is inside the try, because the undo is
    not survivable if it does not run: the process environment is emptied and
    dotenv is stubbed for the life of the interpreter.
    """
    import os

    import dotenv

    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app
    from tests.integration.api.test_openapi_documents_auth import (
        _SYSTEM_ENVIRONMENT_KEYS,
        PINNED_ENVIRONMENT,
    )

    saved_environ = dict(os.environ)
    saved_dotenv = (dotenv.load_dotenv, dotenv.dotenv_values)
    try:
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
        os.environ.update(overrides)
        reset_settings()
        return rebuild_app()
    finally:
        dotenv.load_dotenv, dotenv.dotenv_values = saved_dotenv
        os.environ.clear()
        os.environ.update(saved_environ)
        reset_settings()


def _debug_routes(app) -> list[str]:
    from fastapi.routing import APIRoute

    return sorted(
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/debug")
    )


def _open_served_operations(app, prefix: str) -> set[tuple[str, str]]:
    """Served operations under ``prefix`` whose tree contains no mandatory auth."""
    import inspect

    from fastapi.routing import APIRoute

    def qualified(call) -> str:
        module = getattr(inspect.getmodule(call), "__name__", "")
        name = getattr(call, "__name__", type(call).__name__)
        return f"{module}.{name}" if module else name

    def dependencies(dependant, seen=None):
        seen = seen if seen is not None else set()
        for dependency in dependant.dependencies:
            seen.add(qualified(dependency.call))
            dependencies(dependency, seen)
        return seen

    # Spelled as ``test_openapi_documents_auth.py`` spells it, and IMPORTED
    # from it rather than copied, so the two cannot drift about what
    # "authenticated" means.
    from tests.integration.api.test_openapi_documents_auth import (
        MANDATORY_AUTH_DEPENDENCIES,
    )

    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(prefix)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
        and not (MANDATORY_AUTH_DEPENDENCIES & dependencies(route.dependant))
    }


@pytest.mark.integration
@pytest.mark.security
def test_no_conditionally_mounted_route_is_open_without_a_decision():
    """The debug router, which no contract-derived guard can see.

    It mounts only where ``_is_debug_enabled()`` holds, so it is absent from
    the published document AND from both delegate guards, which build under
    ``ENVIRONMENT=production``. Same rule, applied to the app instead of the
    artifact: open, or listed with a reason.

    The positive control is that the router MOUNTED, not that something open
    was found on it. It used to be the latter — ``assert open_debug`` — which
    was a fair control while the four routes were ungated and is false the
    moment they are not (#1474). Asserting on the mount keeps the control
    honest under both states: an empty ``open_debug`` now means "every debug
    route is closed", and the assertion above it is what rules out the other
    reading, "the router was never built".
    """
    served = _app_under(ENVIRONMENT="development")

    assert _debug_routes(served), (
        "the debug router did not mount under ENVIRONMENT=development, which "
        "is the shipped default — this guard measured nothing"
    )

    open_debug = _open_served_operations(served, "/debug")
    undeclared = open_debug - set(DEBUG_OPERATIONS)
    assert not undeclared, (
        "these debug operations are served with NO authentication and are not "
        "in DEBUG_OPERATIONS:\n" + _format(undeclared)
    )

    stale = set(DEBUG_OPERATIONS) - open_debug
    assert not stale, (
        "these DEBUG_OPERATIONS entries no longer name an open served "
        "operation — remove them:\n" + _format(stale)
    )

    for (method, path), (disposition, reason) in DEBUG_OPERATIONS.items():
        assert disposition in (_PUBLIC, _DEFERRED), f"{method} {path}: {disposition!r}"
        assert len(reason.strip()) > 40, f"{method} {path}: no reason given"
        if disposition == _DEFERRED:
            assert "#" in reason, f"{method} {path}: a deferral must name its issue"


@pytest.mark.integration
@pytest.mark.security
def test_the_debug_router_in_production_is_an_explicit_opt_in():
    """The gate ``DEBUG_OPERATIONS`` leans on, asserted as it actually behaves.

    ``_is_debug_enabled()`` is a disjunction, not an environment check:

        env in ("development", "testing", "test") or enable_debug_endpoints

    so ``ENABLE_DEBUG_ENDPOINTS=true`` mounts the router IN PRODUCTION. An
    earlier version of this test varied only ``ENVIRONMENT`` and therefore
    asserted "absent in production", which is false on the flag that matters —
    measured: with the flag set, the app logs
    ``🔧 Debug endpoints enabled (ENVIRONMENT=production ...)``.

    Both halves are asserted, because only the pair is the real property: the
    DEFAULT is closed, and the way past it is one named operator switch rather
    than an accident of configuration.

    The second half was the ``_FINDING`` idiom until #1474 — asserted as it
    behaved, so that a fix would turn it red. #1474 landed as auth on the
    routes rather than as a conjunction in the gate, which is a fix at the
    other layer and leaves the mount exactly as it was, so this half did not go
    red and is not deleted. What changes is what it is FOR. It is no longer a
    recorded finding; it is the operator capability the flag exists to provide,
    now paired with the assertion that gives it teeth — that nothing the flag
    mounts in production is reachable without a platform administrator. That
    pairing is the whole of #1474's decision: the flag governs MOUNTING, and
    authentication governs EXPOSURE.

    Making the gate a conjunction as well remains available as defence in
    depth. If it is ever taken, the first assertion below absorbs it and the
    second is what has to be rewritten.
    """
    assert _debug_routes(_app_under(ENVIRONMENT="production")) == [], (
        "the debug router mounted in production with ENABLE_DEBUG_ENDPOINTS "
        "unset — production is no longer closed by DEFAULT, which is the half "
        "of this gate that no route-level auth can substitute for"
    )

    opted_in_app = _app_under(ENVIRONMENT="production", ENABLE_DEBUG_ENDPOINTS="true")
    assert _debug_routes(opted_in_app), (
        "ENABLE_DEBUG_ENDPOINTS=true no longer mounts the debug router in "
        "production. That may be deliberate — the conjunction #1474 left "
        "available — but it removes an operator's ability to debug a "
        "production deployment, so it is a decision, not a refactor"
    )

    open_in_production = _open_served_operations(opted_in_app, "/debug")
    assert not open_in_production, (
        "the operator flag mounted these debug operations in PRODUCTION with "
        "no authentication — this is #1474, on the path where it is worst:\n"
        + _format(open_in_production)
    )


#: The four routes #1474 gated. ``/debug/cases/{case_id}/causal-graph`` is the
#: fifth on that router and is deliberately absent: it already required auth,
#: and it takes a path parameter and a case service, so it cannot be driven by
#: a bare GET the way these four can.
#:
#: Named rather than derived from the served app, because a route silently
#: dropped from the router is a thing this module should notice: a derived list
#: would simply come back shorter and every assertion over it would still pass.
_DEBUG_PATHS = (
    "/debug/config",
    "/debug/routes",
    "/debug/health",
    "/debug/llm-providers",
)


def _gate_failure(route) -> str | None:
    """``None`` if an auth gate resolves before anything the HANDLER declares.

    "First" is not the same as "index 0". The application carries one global
    dependency — ``bind_request_enterprise_context``, the tenant binder — and
    FastAPI inserts app-level and decorator-level dependencies at the front of
    every route's dependant, so the binder is index 0 on all of them. The
    property worth asserting is therefore the one #1467's finding is actually
    about: no dependency the handler DECLARES resolves before the gate.

    The discriminator is ``Dependant.name``, which FastAPI sets to the
    parameter's name for a handler-declared dependency and leaves ``None`` for
    a parameterless one built from ``dependencies=[...]``. Measured:

        @app.get("/decorator", dependencies=[Depends(gate)])
        async def a(svc=Depends(service)): ...
            -> [(None, 'gate'), ('svc', 'service')]

        @app.get("/parameter")
        async def b(svc=Depends(service), user=Depends(gate)): ...
            -> [('svc', 'service'), ('user', 'gate')]

    ``test_a_gate_declared_after_a_service_parameter_is_not_a_gate`` drives both
    shapes through the request path, so this predicate is not the only thing
    claiming the difference matters.
    """
    import inspect

    from tests.integration.api.test_openapi_documents_auth import (
        MANDATORY_AUTH_DEPENDENCIES,
    )

    def qualified(call) -> str:
        module = getattr(inspect.getmodule(call), "__name__", "")
        name = getattr(call, "__name__", type(call).__name__)
        return f"{module}.{name}" if module else name

    dependencies = route.dependant.dependencies
    gates = [
        index
        for index, dependency in enumerate(dependencies)
        if qualified(dependency.call) in MANDATORY_AUTH_DEPENDENCIES
    ]
    if not gates:
        return (
            "no mandatory auth dependency at all; resolved: "
            f"{[qualified(d.call) for d in dependencies]}"
        )

    declared = [
        (index, dependency)
        for index, dependency in enumerate(dependencies)
        if dependency.name is not None
    ]
    early = [
        f"{dependency.name}={qualified(dependency.call)}"
        for index, dependency in declared
        if index < min(gates)
    ]
    if early:
        return (
            "these handler-declared dependencies resolve BEFORE the auth "
            f"gate: {early}. Declare the gate on the decorator "
            "(dependencies=[Depends(require_platform_admin)]) rather than as "
            "a handler parameter"
        )
    return None


@pytest.mark.integration
@pytest.mark.security
def test_a_gate_declared_after_a_service_parameter_is_not_a_gate():
    """#1467's finding, pinned as behaviour rather than repeated as advice.

    This is the positive control for the rule the four debug routes are
    asserted against. It builds the two shapes on a throwaway app and drives
    both, so the rule is justified by what FastAPI does rather than by the
    comment saying so — and a FastAPI upgrade that reordered dependency
    resolution would fail here, where the reason is legible, instead of
    silently turning four 401s into 500s.

    The gate is the REAL ``require_platform_admin`` in both, not a stand-in:
    only its declaration site differs, so what the two routes disagree about is
    exactly the one thing under test.
    """
    from fastapi import Depends as _Depends
    from fastapi import FastAPI
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    from faultmaven.api.v1.auth_dependencies import require_platform_admin

    def service():
        raise RuntimeError("a collaborator that is unavailable to this caller")

    probe = FastAPI()

    @probe.get(
        "/gate-on-the-decorator", dependencies=[_Depends(require_platform_admin)]
    )
    async def _decorated(collaborator=_Depends(service)):  # pragma: no cover
        return {}

    @probe.get("/gate-after-the-parameter")
    async def _parameterised(  # pragma: no cover
        collaborator=_Depends(service),
        caller=_Depends(require_platform_admin),
    ):
        return {}

    routes = {
        route.path: route for route in probe.routes if isinstance(route, APIRoute)
    }
    assert _gate_failure(routes["/gate-on-the-decorator"]) is None
    assert _gate_failure(routes["/gate-after-the-parameter"]) is not None, (
        "the predicate accepted a gate declared after a service parameter — "
        "it is not discriminating, and every route it passes is unchecked"
    )

    with TestClient(probe, raise_server_exceptions=False) as client:
        assert client.get("/gate-on-the-decorator").status_code == 401
        assert client.get("/gate-after-the-parameter").status_code == 500, (
            "a gate declared after a service parameter no longer lets the "
            "service's failure reach the caller first — FastAPI's resolution "
            "order changed, and the rule below needs re-deriving"
        )


@pytest.mark.integration
@pytest.mark.security
def test_the_debug_gate_resolves_before_anything_the_handler_declares():
    """Declared on the decorator, so it runs first — asserted, not assumed.

    #1467 found this the expensive way on ``/admin/optimization/trigger-
    cleanup``: a gate written as a handler PARAMETER resolves in declaration
    order beside the handler's other parameters, so a service dependency ahead
    of it resolves first and an anonymous caller gets that service's 500 where
    the gate promised a 401.

    That is FastAPI's behaviour rather than FaultMaven's, which is precisely
    why it is pinned here: the four debug handlers declare no parameters today,
    so the ordering is currently unobservable from a status code, and the next
    person to add one would be relying on a property nothing in this repository
    checks.
    """
    from fastapi.routing import APIRoute

    served = _app_under(ENVIRONMENT="development")
    routes = {
        route.path: route
        for route in served.routes
        if isinstance(route, APIRoute) and route.path in _DEBUG_PATHS
    }

    missing = set(_DEBUG_PATHS) - set(routes)
    assert not missing, f"debug routes absent from the served app: {sorted(missing)}"

    failures = {
        path: failure
        for path, route in sorted(routes.items())
        if (failure := _gate_failure(route)) is not None
    }
    assert not failures, "\n".join(
        f"{path}: {reason}" for path, reason in failures.items()
    )


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.parametrize(
    "label,overrides",
    [
        ("the shipped default", {"ENVIRONMENT": "development"}),
        (
            "the operator flag in production",
            {"ENVIRONMENT": "production", "ENABLE_DEBUG_ENDPOINTS": "true"},
        ),
    ],
)
def test_the_debug_routes_refuse_anonymous_and_non_operator_callers(label, overrides):
    """Driven through the served app, on both paths that mount the router.

    A guard has to be exercised through the path that runs it. The dependency-
    graph assertions above read the route object; this one issues the request,
    on both mount paths rather than on the convenient one — the flag in
    production is where the exposure mattered (self-hosted publishes 8090 on
    0.0.0.0 with no proxy), and development is the shipped default, so a fix
    that closed only one of them would close neither in practice.

    Three callers, because "requires auth" and "requires the operator role" are
    different claims and only the third proves the routes still WORK. An
    authenticated non-operator getting 200 would mean the gate had been
    weakened to ``require_authentication``; an operator getting 401 or 403
    would mean the developer surface had been removed rather than gated, which
    is not what #1474 decided.
    """
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.domain.models.auth import DevUser

    def _user(roles):
        return DevUser(
            user_id="00000000-0000-0000-0000-0000000000ff",
            username="probe",
            email="probe@local.faultmaven",
            display_name="probe",
            created_at=datetime.now(UTC),
            roles=roles,
        )

    served = _app_under(**overrides)
    assert _debug_routes(served), f"{label}: the debug router did not mount"

    with TestClient(served) as client:
        for path in _DEBUG_PATHS:
            anonymous = client.get(path)
            assert anonymous.status_code == 401, (
                f"{label}: GET {path} answered {anonymous.status_code} to a "
                f"caller with no credential: {anonymous.text[:200]}"
            )

        served.dependency_overrides[require_authentication] = lambda: _user(["user"])
        try:
            for path in _DEBUG_PATHS:
                signed_in = client.get(path)
                assert signed_in.status_code == 403, (
                    f"{label}: GET {path} answered {signed_in.status_code} to "
                    "an authenticated NON-operator; the gate is "
                    f"require_platform_admin, not require_authentication: "
                    f"{signed_in.text[:200]}"
                )

            served.dependency_overrides[require_authentication] = lambda: _user(
                ["user", "admin", "platform_admin"]
            )
            for path in _DEBUG_PATHS:
                operator = client.get(path)
                assert operator.status_code == 200, (
                    f"{label}: GET {path} answered {operator.status_code} to a "
                    "platform administrator — #1474 gated these routes, it did "
                    f"not remove them: {operator.text[:200]}"
                )
        finally:
            served.dependency_overrides.clear()


@pytest.mark.integration
@pytest.mark.security
def test_the_blind_spot_this_guard_declares_is_covered_elsewhere():
    """A route served but not documented is invisible here — on purpose.

    This module reads the published contract, so it can say nothing about a
    route with ``include_in_schema=False``, or about the case where Starlette
    serves one definition of a path and FastAPI documents another (``POST
    /api/v1/sessions/cleanup`` really did that). Those belong to the two gates
    named below, and naming them is worth nothing unless the names are checked
    — a dangling pointer is exactly how a declared blind spot becomes an
    undeclared one.
    """
    from tests.integration.api import test_openapi_documents_auth as sibling

    for guard in (
        "test_served_and_documented_operation_counts_agree",
        "test_no_operation_is_registered_twice",
    ):
        assert hasattr(sibling, guard), (
            f"this module declares its blind spot covered by {guard}, and that "
            "guard no longer exists in test_openapi_documents_auth.py"
        )
