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

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

# FastAPI's own route flattener. Imported at MODULE level, not inside the
# helper, for two reasons: it is how ``api/middleware/route_policy.py`` does it
# (fm#1305), and a function-local import cannot be monkeypatched — which would
# leave the >= 0.139 arm unexecuted on the pinned ``fastapi==0.136.0`` that CI
# and the image install. See ``TestTheFlattenedArmOnThePinnedVersion``.
try:  # pragma: no cover - exercised on FastAPI >= 0.139
    from fastapi.routing import iter_route_contexts
except ImportError:  # pragma: no cover - FastAPI < 0.139
    iter_route_contexts = None

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


#: One built app per distinct configuration, for the whole module.
#:
#: ``rebuild_app()`` drops ``faultmaven.main`` from ``sys.modules`` and imports
#: it again, re-running every router registration and all the DI wiring. Nine
#: calls across this module resolve to four distinct configurations
#: (development; production; production + the flag; staging), so five of the
#: nine were rebuilding an app byte-identical to one already in hand.
#:
#: Safe to share because every consumer of ``_app_under`` READS — route tables
#: and dependency trees — and none of them starts the app or mutates it. The one
#: test that does both uses ``_served_under`` instead, which builds its own app
#: each time precisely because it installs ``dependency_overrides`` and enters a
#: lifespan; sharing one of those would leak an override between tests. The
#: split between the two helpers is what makes this cache safe, so it is not an
#: incidental difference.
_APP_CACHE: dict[tuple, object] = {}


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

    key = tuple(sorted(overrides.items()))
    if key in _APP_CACHE:
        return _APP_CACHE[key]

    saved_environ = dict(os.environ)
    saved_dotenv = (dotenv.load_dotenv, dotenv.dotenv_values)
    try:
        dotenv.load_dotenv = lambda *args, **kwargs: None
        dotenv.dotenv_values = lambda *args, **kwargs: {}

        preserved = {
            key_: value
            for key_, value in os.environ.items()
            if key_ in _SYSTEM_ENVIRONMENT_KEYS
        }
        os.environ.clear()
        os.environ.update(preserved)
        os.environ.update(PINNED_ENVIRONMENT)
        os.environ.update(overrides)
        reset_settings()
        built = rebuild_app()
        _APP_CACHE[key] = built
        return built
    finally:
        dotenv.load_dotenv, dotenv.dotenv_values = saved_dotenv
        os.environ.clear()
        os.environ.update(saved_environ)
        reset_settings()


@contextlib.contextmanager
def _served_under(**overrides):
    """``_app_under``, but the pin stays in force while the app is USED.

    ``_app_under`` restores ``os.environ`` and calls ``reset_settings()`` in its
    ``finally``, which is right for a caller that only wants the route table:
    the app is built under the pin and nothing else in the session is disturbed.
    It is wrong for a caller that starts the app and issues requests, because
    everything that reads settings at REQUEST time — the protection preset, the
    auth mode, the tenant provider, the storage backends — then reads whatever
    the session's ambient environment happens to hold. That makes the assertions
    a function of the machine, which is the hazard ``_app_under``'s own
    docstring says it exists to stop ("An earlier version overrode two variables
    and inherited the rest; that is the bug this docstring exists to stop coming
    back").

    It also contains a leak that is only reachable by starting the app: the
    lifespan applies a configuration preset and pins BLAS/OMP thread counts, so
    it WRITES to ``os.environ``. Measured on this box, entering the lifespan and
    leaving it added ``KMP_DUPLICATE_LIB_OK``, ``KMP_INIT_AT_FORK``,
    ``MKL_NUM_THREADS``, ``OMP_NUM_THREADS``, ``OPENBLAS_NUM_THREADS`` and
    ``TORCHINDUCTOR_CACHE_DIR`` to the pytest process, where they outlive the
    test. Restoring the snapshot on the way out is what keeps a module that
    starts an app from changing the environment every later module runs under.

    An override of ``None`` REMOVES the key. ``PINNED_ENVIRONMENT`` exists to
    build a *document* and therefore pins ``AUTH_MODE=oauth`` plus WorkOS
    placeholders to mount the SSO router — which is fine for describing routes
    and fatal for starting the app, because the DI container then imports
    ``workos``, a Cloud-only dependency absent from the Standalone install
    (measured: ``RuntimeError: DI Container initialization failed: No module
    named 'workos'``). A caller that starts the app says which deployment shape
    it is exercising instead of inheriting one.
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
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_settings()
        yield rebuild_app()
    finally:
        dotenv.load_dotenv, dotenv.dotenv_values = saved_dotenv
        os.environ.clear()
        os.environ.update(saved_environ)
        reset_settings()


class _Served(NamedTuple):
    """One served API operation's path, verbs, and the dependant FastAPI RESOLVES.

    The third field is the point. Every consumer here asks a question about the
    dependency tree, and on FastAPI >= 0.139 ``route.dependant`` is NOT the tree
    that runs for a route reached through ``include_router`` — it carries only
    what the handler declares. Measured on the composed app under 0.141.1:

        APIRoute contexts                                 147
        ctx.dependant IS route.dependant                   15
        ctx.dependant IS NOT route.dependant              132
        route.dependant LACKING the app-level tenant binder 132
        ctx.dependant   LACKING the app-level tenant binder   0

    So reading ``route.dependant`` there would mean: the binder is absent from
    132 trees (falsifying what ``_PERMITTED_BEFORE_A_GATE`` says about it), a
    gate contributed by ``include_router(..., dependencies=[...])`` is invisible
    so its whole router reads as OPEN, and a COLLABORATOR contributed the same
    way resolves ahead of the route's own gate at runtime while ``_gate_failure``
    answers ``None`` — the #1467 shape passing silently.
    """

    path: str
    methods: frozenset
    dependant: object


def _served_api_routes(app) -> list[_Served]:
    """Every API operation the app serves, with its EFFECTIVE path and dependant.

    NOT ``[r for r in app.routes if isinstance(r, APIRoute)]``, and the reason
    is entirely version-dependent — which is why both arms exist and both are
    tested. Measured, on a router included at ``prefix="/pre"`` with an
    app-level dependency, a router-level dependency and a handler parameter:

        fastapi==0.136.0  (PINNED: requirements/{test,dev,cloud}.txt, the image)
          app.routes types            ['APIRoute', 'Route']
          flat APIRoute paths         ['/direct', '/pre/leaf']   <- COMPLETE
          route.dependant             [app_global, router_gate, handler_dep]
                                                                 <- COMPLETE
        fastapi==0.141.1
          app.routes types            ['APIRoute', 'Route', '_IncludedRouter']
          flat APIRoute paths         ['/direct']                <- 20 of 144
          route.dependant             [handler_dep]              <- INCOMPLETE
          ctx.path                    '/pre/leaf'
          ctx.dependant               [app_global, router_gate, handler_dep]

    0.139 stopped copying an included router's routes into ``app.routes`` and
    records one ``_IncludedRouter`` placeholder per ``include_router`` instead,
    moving BOTH the path and the resolved dependant onto the context. Before
    that, the eager copy already merged the prefix and the contributed
    dependencies into the route itself, so the flat scan is not a degraded
    fallback there — it is exactly right, and this helper is a correct no-op on
    the version that currently ships.

    The obvious hand-rolled alternative is wrong on both counts: walking
    ``original_router`` reaches the routes but yields their UNPREFIXED paths and
    their handler-only dependants.

        flat app.routes        -> ['/direct']
        original_router walk   -> ['/direct', '/leaf']      <- wrong path
        iter_route_contexts    -> ['/direct', '/pre/leaf']  <- effective path
        app.openapi()["paths"] -> ['/direct', '/pre/leaf']
    """
    from fastapi.routing import APIRoute

    if iter_route_contexts is None:  # FastAPI < 0.139: the eager-copy shape
        return [
            _Served(route.path, frozenset(route.methods or ()), route.dependant)
            for route in app.routes
            if isinstance(route, APIRoute)
        ]

    found = []
    for context in iter_route_contexts(app.routes):
        route = getattr(context, "route", None)
        if not isinstance(route, APIRoute):
            continue
        # Demanded, never defaulted. Falling back to ``route.dependant`` when a
        # context does not carry one would silently reinstate the exact defect
        # this helper exists to avoid, on a future FastAPI, with every test
        # still green. Measured on 0.141.1: 0 of 147 APIRoute contexts lack it.
        if not hasattr(context, "dependant"):
            raise AssertionError(
                f"{context.path}: this FastAPI's route context carries no "
                "`dependant`, so the tree that actually resolves cannot be "
                "read. Do NOT fall back to route.dependant — on >= 0.139 that "
                "is the handler's tree only, and every guard in this module "
                "would quietly start asking about the wrong one."
            )
        found.append(
            _Served(
                context.path,
                frozenset(getattr(context, "methods", None) or ()),
                context.dependant,
            )
        )
    return found


def _debug_routes(app) -> list[str]:
    return sorted(
        path
        for path, _methods, _dependant in _served_api_routes(app)
        if path.startswith("/debug")
    )


def _qualified(call) -> str:
    """The sibling's ``_qualified_name``, delegated rather than copied.

    Beside ``MANDATORY_AUTH_DEPENDENCIES``, which this module already takes from
    that module so the two "cannot drift about what 'authenticated' means".
    Defining a second qualifier here would be the same drift one layer down, and
    the cost is on the record: two copies of the walk below already disagreed
    about depth once — one recursed, one read the top level only — so a route
    gated through a composite was "authenticated" to one and "no auth at all" to
    the other, on the same app in the same run. Delegating rather than importing
    at module scope because every other cross-module reference here is a
    function-local import too.
    """
    from tests.integration.api.test_openapi_documents_auth import _qualified_name

    return _qualified_name(call)


def _dependency_names(dependant, seen=None) -> set[str]:
    """The sibling's transitive dependency walk. See :func:`_qualified`."""
    from tests.integration.api.test_openapi_documents_auth import (
        _dependency_names as _sibling_walk,
    )

    return _sibling_walk(dependant, seen)


def _open_served_operations(app, prefix: str) -> set[tuple[str, str]]:
    """Served operations under ``prefix`` whose tree contains no mandatory auth.

    Over ``_served_api_routes``, not a flat ``app.routes`` scan — see there for
    why a flat scan sees 20 of 144 on this FastAPI, and why that is the gap this
    module's own recommended follow-up would open.
    """
    from tests.integration.api.test_openapi_documents_auth import (
        MANDATORY_AUTH_DEPENDENCIES,
    )

    return {
        (method, path)
        for path, methods, dependant in _served_api_routes(app)
        if path.startswith(prefix)
        for method in methods
        if method not in {"HEAD", "OPTIONS"}
        and not (MANDATORY_AUTH_DEPENDENCIES & _dependency_names(dependant))
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

    # Both structural guarantees, on the flag path, because the flag path is
    # the one that cannot have an HTTP round trip: an app built under
    # ``ENVIRONMENT=production`` selects the production protection preset, which
    # refuses every rate-limited request when it has no Redis client
    # (``fail_open_on_redis_error=False``, and no bypass headers), so the
    # request-path test below runs under development only and says why. "A gate
    # is present" and "the gate resolves first" are separate claims, and this is
    # the only place the second one is made about the routes AS MOUNTED BY THE
    # FLAG rather than as mounted by the environment.
    mis_ordered = {
        path: failure
        for path, _methods, dependant in _served_api_routes(opted_in_app)
        if path in _ALL_DEBUG_PATHS
        and (failure := _gate_failure(dependant)) is not None
    }
    assert not mis_ordered, (
        "the operator flag mounted these debug routes in PRODUCTION with "
        "something resolving ahead of their auth gate:\n"
        + "\n".join(f"{path}: {reason}" for path, reason in mis_ordered.items())
    )


#: Operations that ARE gated but resolve a collaborator before the gate — the
#: #1467 shape, carried rather than fixed. **#1494.**
#:
#: The predicate below is quantified over the WHOLE application, not over the
#: five debug routes it was written for, because a rule applied to five routes
#: out of 147 is a rule about five routes. Run app-wide it reports 53
#: operations: an anonymous caller to ``GET /api/v1/cases/{case_id}`` reaches
#: ``_di_get_case_service_dependency`` before the gate, and whatever that
#: provider does on a degraded deployment is what the caller gets in place of
#: the 401 the route promises. Not a disclosure — the gate still runs if the
#: provider succeeds — a wrong refusal, and the class #1447 was.
#:
#: They are NOT fixed here: 53 operations across ``case``, ``knowledge``,
#: ``auth`` and the shared ``api/v1`` dependencies is a wide mechanical change
#: that wants its own review. They are carried the way ``PUBLIC_OPERATIONS``
#: carries its deferrals — with the issue that closes them — and the allowlist
#: fails in BOTH directions, so the class cannot grow quietly and closing #1494
#: forces the entries out.
#: The disposition for an operation that IS gated but resolves something else
#: first. Its own word rather than ``_DEFERRED``, because the two say different
#: things: ``_DEFERRED`` means "open although it should not be", and none of
#: these is open — the gate runs, it just runs second.
_MISORDERED = "misordered"

MISORDERED_GATE_OPERATIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("DELETE", "/api/v1/cases/{case_id}"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("DELETE", "/api/v1/cases/{case_id}/data/{data_id}"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("DELETE", "/api/v1/cases/{case_id}/team-shares/{team_id}"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("DELETE", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("DELETE", "/api/v1/knowledge/documents/{document_id}"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/auth/oauth/authorize"): (
        _MISORDERED,
        "<decorator>=require_oauth_rate_limit_authorize resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/analytics"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/data"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/data/{data_id}"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/messages"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/report-recommendations"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/reports"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency, case_repository=get_case_repository resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/reports/{report_id}/download"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency, case_repository=get_case_repository resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/ui"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("GET", "/api/v1/cases/{case_id}/uploaded-files"): (
        _MISORDERED,
        "case_service=get_case_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/analytics/search"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/conversions"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/conversions/by-case/{case_id}"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/conversions/{conversion_id}"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/documents/{document_id}"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/documents/{document_id}/snippet"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/drafts"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/stats"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/suggestions"): (
        _MISORDERED,
        "suggestion_service=get_suggestion_service resolves first. #1494.",
    ),
    ("GET", "/api/v1/knowledge/suggestions/{suggestion_id}"): (
        _MISORDERED,
        "suggestion_service=get_suggestion_service resolves first. #1494.",
    ),
    ("PATCH", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"): (
        _MISORDERED,
        "investigation_service=get_investigation_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/auth/oauth/authorize"): (
        _MISORDERED,
        "<decorator>=require_oauth_rate_limit_authorize resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency, session_service=_di_get_session_service_dependency resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/search"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/sessions/{session_id}/resume/{case_id}"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency, session_service=_di_get_session_service_dependency resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/{case_id}/close"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency, case_repository=get_case_repository resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/{case_id}/extract-knowledge"): (
        _MISORDERED,
        "case_service=get_case_service, suggestion_service=get_suggestion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/{case_id}/reports"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/{case_id}/team-shares"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/{case_id}/title"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("POST", "/api/v1/cases/{case_id}/turns"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency, investigation_service=get_investigation_service resolves first. #1494.",
    ),
    (
        "POST",
        "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}/verify",
    ): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/convert"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/documents"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/documents/bulk-delete"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/documents/bulk-update"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/drafts/verify-batch"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/runbooks/create"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/scan"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/approve"): (
        _MISORDERED,
        "suggestion_service=get_suggestion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/reject"): (
        _MISORDERED,
        "suggestion_service=get_suggestion_service resolves first. #1494.",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/remediate-pii"): (
        _MISORDERED,
        "suggestion_service=get_suggestion_service resolves first. #1494.",
    ),
    ("PUT", "/api/v1/cases/{case_id}"): (
        _MISORDERED,
        "case_service=_di_get_case_service_dependency resolves first. #1494.",
    ),
    ("PUT", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): (
        _MISORDERED,
        "service=_get_conversion_service resolves first. #1494.",
    ),
    ("PUT", "/api/v1/knowledge/documents/{document_id}"): (
        _MISORDERED,
        "knowledge_service=get_knowledge_service resolves first. #1494.",
    ),
    ("PUT", "/api/v1/knowledge/suggestions/{suggestion_id}"): (
        _MISORDERED,
        "suggestion_service=get_suggestion_service resolves first. #1494.",
    ),
}

#: The four routes #1474 gated: the ones a bare anonymous GET can drive, which
#: is what :func:`test_the_debug_routes_refuse_anonymous_and_non_operator_callers`
#: needs. ``/debug/cases/{case_id}/causal-graph`` is absent from THIS tuple for
#: that reason alone — it takes a path parameter and a case service.
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

#: Every route on the debug router, which is what the ORDERING guard quantifies
#: over. It reads ``route.dependant`` and issues no request, so "cannot be
#: driven by a bare GET" is no reason to exclude anything — and excluding the
#: causal-graph route would have left the one route on this router that DOES
#: use the condemned trailing-parameter shape as the only one never inspected.
_ALL_DEBUG_PATHS = _DEBUG_PATHS + ("/debug/cases/{case_id}/causal-graph",)


def _resolution_order(dependant) -> list:
    """Every dependency in the tree, in the order FastAPI actually resolves it.

    POST-order, measured rather than assumed. ``solve_dependencies`` walks
    ``dependant.dependencies`` in order and solves each one's sub-dependencies
    before the dependency itself, so a collaborator nested inside a composite
    runs before the composite — and therefore before anything that follows it at
    the top level. Instrumented on fastapi 0.141.1, with tracer callables that
    record when they run:

        @app.get("/o", dependencies=[Depends(outer), Depends(top2)])
        async def h(): ...
        where outer(a=Depends(inner_a), b=Depends(inner_b))

        actual call order -> ['inner_a', 'inner_b', 'outer', 'top2']
        pre-order         -> ['outer', 'inner_a', 'inner_b', 'top2']   <- wrong
        post-order        -> ['inner_a', 'inner_b', 'outer', 'top2']   <- matches

    Reading only the TOP level, which is what this predicate did at first, makes
    the whole composite one opaque entry: a collaborator declared ahead of the
    gate INSIDE it is invisible, the route is reported correctly gated, and an
    anonymous caller still gets the collaborator's 500.
    """
    order = []
    for dependency in dependant.dependencies:
        order.extend(_resolution_order(dependency))
        order.append(dependency)
    return order


#: The only dependency permitted to resolve ahead of an auth gate.
#:
#: ``bind_request_enterprise_context`` is declared as a GLOBAL on ``app`` — the
#: module docstring's "the app's only global dependency is the tenant binder,
#: not authentication" — and FastAPI puts app-level dependencies at the front of
#: every route's dependant. So it is first on every route in the application and
#: no route can be written that does not have it first.
#:
#: Naming it rather than discriminating structurally is deliberate. The obvious
#: structural rule — "ignore anything FastAPI built without a parameter name" —
#: measured WRONG: a collaborator listed in the same ``dependencies=[...]`` as
#: the gate also has ``name is None``, so
#: ``dependencies=[Depends(service), Depends(require_platform_admin)]`` passed a
#: rule written to forbid exactly that, while an anonymous GET answered 500.
#: An allowlist of one cannot make that mistake: everything not named here is
#: reported, wherever and however deep it was declared.
_PERMITTED_BEFORE_A_GATE = frozenset(
    {
        "faultmaven.api.middleware.tenant_scope.bind_request_enterprise_context",
    }
)


def _gate_failure(dependant) -> str | None:
    """``None`` if an auth gate resolves before every other dependency.

    "First" is not literally "index 0": the tenant binder above is, on every
    route. So the property asserted is that nothing EXCEPT the binder resolves
    ahead of the gate, over the whole tree in
    :func:`_resolution_order` — which covers all three shapes measured so far: a
    gate declared as a trailing handler parameter behind a service (#1467), a
    collaborator ahead of the gate in the same ``dependencies=[...]``, and a
    collaborator ahead of the gate inside a composite dependency.
    """
    from tests.integration.api.test_openapi_documents_auth import (
        MANDATORY_AUTH_DEPENDENCIES,
    )

    order = _resolution_order(dependant)
    gates = [
        index
        for index, dependency in enumerate(order)
        if _qualified(dependency.call) in MANDATORY_AUTH_DEPENDENCIES
    ]
    if not gates:
        return (
            "no mandatory auth dependency at all; resolved: "
            f"{[_qualified(d.call) for d in order]}"
        )

    # Two things legitimately resolve before the gate, and both are excluded by
    # the SUBTREE rather than by the name — because post-order puts a
    # dependency's children ahead of the dependency itself, so permitting only
    # the parent's name permits nothing that actually runs first. Measured on
    # ``/debug/config``: positions 0 and 1 are ``get_auth_service`` and
    # ``get_organization_repository``, the tenant binder's own children, and the
    # binder is not reached until position 2.
    #
    # 1. The gate's own machinery. ``require_platform_admin`` pulls
    #    ``require_authentication``, which pulls ``get_current_user_optional``,
    #    ``extract_bearer_token``, ``get_auth_service`` and the ``HTTPBearer``
    #    scheme. They are how the gate does its job; counting them as "ahead of
    #    the gate" reports every correctly gated route in the application.
    # 2. Whatever ``_PERMITTED_BEFORE_A_GATE`` names, with its subtree — the
    #    app-level tenant binder, which is first on every route by construction.
    #
    # Excluded by ``id`` rather than by name, so a collaborator that happens to
    # share a name with something in either tree is still reported.
    first = min(gates)
    excused = {id(d) for d in _resolution_order(order[first])}
    for dependency in order:
        if _qualified(dependency.call) in _PERMITTED_BEFORE_A_GATE:
            excused.add(id(dependency))
            excused.update(id(d) for d in _resolution_order(dependency))

    early = [
        f"{dependency.name or '<decorator>'}={_qualified(dependency.call)}"
        for index, dependency in enumerate(order)
        if index < first and id(dependency) not in excused
    ]
    if early:
        return (
            f"these dependencies resolve BEFORE the auth gate: {early}. Declare "
            "the gate first — on the decorator "
            "(``dependencies=[Depends(require_platform_admin)]``), ahead of "
            "anything else in that list, and not behind a collaborator inside a "
            "composite — or an anonymous caller reaches them and gets their "
            "failure instead of the refusal"
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

    Three shapes, because the second rule this predicate had to learn came from
    a review of the first: a collaborator listed in the SAME
    ``dependencies=[...]`` as the gate, ahead of it, is also parameterless, so a
    rule that discriminated on "was this declared as a handler parameter"
    reported it clean while an anonymous GET answered 500. Measured, which is
    why it is a row here rather than a sentence.

    The gate is the REAL ``require_platform_admin`` in all three, not a
    stand-in: only its position differs, so what the routes disagree about is
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

    @probe.get(
        "/gate-after-a-decorator-collaborator",
        dependencies=[_Depends(service), _Depends(require_platform_admin)],
    )
    async def _mis_ordered():  # pragma: no cover
        return {}

    # A gate reached through TWO levels of wrapper. This is the only thing in
    # the module that observes the recursion in ``_dependency_names``, and it
    # is here because a mutation proved the recursion inert without it: with
    # ``_dependency_names`` flattened to one level the whole module still
    # passed, which means the shared walk — a choke point both predicates now
    # sit on — was carrying an untested claim. One level is not enough to see
    # it (the wrapper's direct children are still enumerated); two is.
    #
    # It fails LOUDLY in both predicates when the walk stops short — the route
    # is reported "no mandatory auth dependency at all" rather than quietly
    # accepted — but loud-when-broken is a property to assert, not to assume.
    async def _inner_gate(caller=_Depends(require_platform_admin)):
        return caller

    async def _outer_gate(caller=_Depends(_inner_gate)):
        return caller

    @probe.get("/gate-through-two-wrappers", dependencies=[_Depends(_outer_gate)])
    async def _nested():  # pragma: no cover
        return {}

    routes = {
        route.path: route for route in probe.routes if isinstance(route, APIRoute)
    }
    assert _gate_failure(routes["/gate-on-the-decorator"].dependant) is None, (
        "the predicate rejected a correctly gated route — it is not a rule, it "
        "is an outage"
    )
    assert _gate_failure(routes["/gate-through-two-wrappers"].dependant) is None, (
        "the predicate did not find an auth gate two wrappers deep, so "
        "_dependency_names is not reaching the whole subtree. Both predicates "
        "read that one walk: _open_served_operations would report this route "
        "OPEN, and this one reports it ungated"
    )
    assert "faultmaven.api.v1.auth_dependencies.require_platform_admin" in (
        _dependency_names(routes["/gate-through-two-wrappers"].dependant)
    ), "the shared walk does not reach a dependency two levels down"
    for path in ("/gate-after-the-parameter", "/gate-after-a-decorator-collaborator"):
        assert _gate_failure(routes[path].dependant) is not None, (
            f"the predicate accepted {path}, where a collaborator resolves "
            "ahead of the gate — it is not discriminating, and every route it "
            "passes is unchecked"
        )

    with TestClient(probe, raise_server_exceptions=False) as client:
        assert client.get("/gate-on-the-decorator").status_code == 401
        assert client.get("/gate-through-two-wrappers").status_code == 401, (
            "a gate two wrappers deep no longer refuses an anonymous caller — "
            "the shape the walk above is asserted to see is not the shape "
            "FastAPI runs"
        )
        for path in (
            "/gate-after-the-parameter",
            "/gate-after-a-decorator-collaborator",
        ):
            assert client.get(path).status_code == 500, (
                f"{path}: a collaborator ahead of the gate no longer reaches "
                "the caller first — FastAPI's resolution order changed, and "
                "the rule below needs re-deriving"
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

    Quantified over the WHOLE router (``_ALL_DEBUG_PATHS``), not the four this
    issue gated. ``/debug/cases/{case_id}/causal-graph`` declares its
    ``require_authentication`` as a trailing handler parameter — the shape this
    rule exists to forbid, currently harmless because it declares nothing else
    — so it is the one route on the router that most needs watching, and the
    one an "only the routes #1474 touched" scope would have skipped.
    """
    served = _app_under(ENVIRONMENT="development")
    routes = {
        path: dependant
        for path, _methods, dependant in _served_api_routes(served)
        if path in _ALL_DEBUG_PATHS
    }

    missing = set(_ALL_DEBUG_PATHS) - set(routes)
    assert not missing, f"debug routes absent from the served app: {sorted(missing)}"

    failures = {
        path: failure
        for path, dependant in sorted(routes.items())
        if (failure := _gate_failure(dependant)) is not None
    }
    assert not failures, "\n".join(
        f"{path}: {reason}" for path, reason in failures.items()
    )


@pytest.mark.integration
@pytest.mark.security
def test_the_debug_routes_refuse_anonymous_and_non_operator_callers():
    """Driven through the served app: the gate as a caller experiences it.

    The structural guards above read ``route.dependant``. This one issues the
    request, because a dependency graph says a gate is PRESENT and only a
    request says what a caller gets.

    Three callers, because "requires auth" and "requires the operator role" are
    different claims and only the third proves the routes still WORK. An
    authenticated non-operator getting 200 would mean the gate had been
    weakened to ``require_authentication``; an operator getting 401 or 403
    would mean the developer surface had been removed rather than gated, which
    is not what #1474 decided. The operator assertion checks the PAYLOAD as
    well as the status, because ``debug_config`` and ``debug_llm_providers``
    both catch every exception and answer 200 with an ``error`` key — a
    regression that broke either of them outright would leave a status-only
    assertion green.

    **Built under development, and only under development.** The obvious
    second arm — the same requests against the app built with
    ``ENVIRONMENT=production`` + ``ENABLE_DEBUG_ENDPOINTS=true`` — was written,
    shipped, and failed in CI's Cloud matrix with

        GET /debug/config -> 503
        {"error": "service_unavailable",
         "message": "Rate limiting service temporarily unavailable"}

    and it was RIGHT to. ``_app_under`` empties the environment before building,
    the production protection preset sets ``fail_open_on_redis_error=False`` and
    ``protection_bypass_headers=[]`` ("No bypasses in production",
    ``config/protection.py``), and the limiter resolves its client from
    ``app.state``. So a production-built app with no reachable Redis refuses
    every rate-limited request at the middleware, ahead of any route. Nothing
    about the gate can be observed through it, and a test that tolerated the 503
    as "refused" would pass a deployment whose auth had been removed.

    It cost nothing real to drop, which is the part worth recording:
    ``_app_under`` restores ``os.environ`` and calls ``reset_settings()`` in its
    ``finally``, so by the time any request is issued ``get_settings()`` reports
    ``environment=development, auth_mode=LOCAL`` whichever way the app was
    built. Measured. The two arms therefore differed only in which routes were
    MOUNTED — never in what a request met — and the mounted-under-the-flag route
    table is asserted, on that same app object, by
    :func:`test_the_debug_router_in_production_is_an_explicit_opt_in`: every
    operation it mounts carries a mandatory auth dependency, and each one's gate
    resolves first. The flag path keeps both structural guarantees; what it
    cannot have, in an environment with no Redis, is an HTTP round trip.
    """
    import os
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.auth.domain.models.auth import DevUser

    #: A key every gated handler puts in its 200 body, so the operator
    #: assertion fails on a handler that answered 200 out of its own
    #: ``except Exception`` instead of doing its job.
    expected_key = {
        "/debug/config": "configuration",
        "/debug/routes": "routes",
        "/debug/health": "status",
        "/debug/llm-providers": "fallback_chain",
    }
    assert set(expected_key) == set(_DEBUG_PATHS)

    def _user(roles):
        return DevUser(
            user_id="00000000-0000-0000-0000-0000000000ff",
            username="probe",
            email="probe@local.faultmaven",
            display_name="probe",
            created_at=datetime.now(UTC),
            roles=roles,
        )

    # Snapshotted around the whole thing because the RESTORE is a claim this
    # test makes and nothing else checks. The lifespan applies a configuration
    # preset and pins BLAS/OMP thread counts, so starting the app WRITES to
    # ``os.environ``; without ``_served_under``'s restore those variables
    # outlive the test and every later module in the session runs under them.
    # Measured before the restore existed: six added here, more on a box with a
    # fuller ``.env``. A mutation that removed the restore passed the whole
    # module until this assertion was added.
    environment_before = dict(os.environ)

    # ``_served_under``, not ``_app_under``: this is the one test here that
    # STARTS the app and issues requests, so the pin has to outlive the build.
    # Under ``_app_under`` the lifespan and every request ran on whatever the
    # session's ambient environment held, which made the protection preset, the
    # auth mode, the tenant provider and the storage backends a function of the
    # machine — and let the lifespan's preset application and BLAS thread
    # pinning leak six variables into the pytest process.
    with _served_under(
        ENVIRONMENT="development",
        # A self-hosted deployment, stated rather than inherited: this is the
        # shape whose 8090 is published on 0.0.0.0 with no proxy, which is the
        # audience #1474 is about. The WorkOS placeholders are removed because
        # they are a document-building fixture, not a deployment.
        AUTH_MODE="local",
        OAUTH_ENABLED="false",
        WORKOS_API_KEY=None,
        WORKOS_CLIENT_ID=None,
        WORKOS_REDIRECT_URI=None,
    ) as served:
        assert _debug_routes(served), "the debug router did not mount"

        # ``raise_server_exceptions=False`` so a handler that raises is reported
        # by the assertion that names the caller identity, rather than as a bare
        # traceback out of ``client.get`` with no indication of which of the
        # three was in play.
        client_cm = TestClient(served, raise_server_exceptions=False)
        with client_cm as client:
            for path in _DEBUG_PATHS:
                anonymous = client.get(path)
                assert anonymous.status_code == 401, (
                    f"GET {path} answered {anonymous.status_code} to a caller with "
                    f"no credential: {anonymous.text[:200]}"
                )

            served.dependency_overrides[require_authentication] = lambda: _user(
                ["user"]
            )
            try:
                for path in _DEBUG_PATHS:
                    signed_in = client.get(path)
                    assert signed_in.status_code == 403, (
                        f"GET {path} answered {signed_in.status_code} to an "
                        "authenticated NON-operator; the gate is "
                        "require_platform_admin, not require_authentication: "
                        f"{signed_in.text[:200]}"
                    )

                served.dependency_overrides[require_authentication] = lambda: _user(
                    ["user", "admin", "platform_admin"]
                )
                for path in _DEBUG_PATHS:
                    operator = client.get(path)
                    assert operator.status_code == 200, (
                        f"GET {path} answered {operator.status_code} to a platform "
                        "administrator — #1474 gated these routes, it did not "
                        f"remove them: {operator.text[:200]}"
                    )
                    assert expected_key[path] in operator.json(), (
                        f"GET {path} answered 200 to a platform administrator but "
                        f"without {expected_key[path]!r}: the handler caught its "
                        "own failure and reported success. "
                        f"{operator.text[:200]}"
                    )

                # ``/debug/routes`` claims in its own docstring to be "a
                # strictly larger surface than the published contract". That
                # was FALSE until this change: the handler walked ``app.routes``
                # flat, which on FastAPI >= 0.139 sees one ``_IncludedRouter``
                # placeholder per ``include_router`` — 24 of 147 routes, a
                # strict SUBSET of the contract, with an operator told "not
                # registered" about a hundred routes that are. Asserted rather
                # than described, because a flattener that quietly stops
                # flattening looks exactly like one that works.
                reported = {
                    row["path"] for row in client.get("/debug/routes").json()["routes"]
                }
                published = set(served.openapi()["paths"])
                assert published <= reported, (
                    "GET /debug/routes does not report every route the "
                    "contract publishes, so it is not the larger surface it "
                    "says it is. Missing: "
                    f"{sorted(published - reported)}"
                )
            finally:
                served.dependency_overrides.clear()

    assert dict(os.environ) == environment_before, (
        "starting the app changed the process environment and it was not put "
        "back. Added: "
        f"{sorted(set(os.environ) - set(environment_before))}; removed: "
        f"{sorted(set(environment_before) - set(os.environ))}; changed: "
        f"{sorted(k for k in set(os.environ) & set(environment_before) if os.environ[k] != environment_before[k])}"
    )


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.parametrize(
    "label,overrides,expected",
    [
        ("the shipped default", {"ENVIRONMENT": "development"}, True),
        (
            "the operator flag in production",
            {"ENVIRONMENT": "production", "ENABLE_DEBUG_ENDPOINTS": "true"},
            True,
        ),
        ("production, flag unset", {"ENVIRONMENT": "production"}, False),
        ("staging, flag unset", {"ENVIRONMENT": "staging"}, False),
    ],
)
def test_whether_the_debug_router_mounted_is_reported_not_only_logged(
    label, overrides, expected
):
    """#1493: a startup log is not an observable.

    Whether ``ENABLE_DEBUG_ENDPOINTS`` lifted the router into a non-development
    environment is a security-relevant deployment fact whose only account was
    one startup line — which has rolled out of ``kubectl logs`` on any pod that
    has been up a while, so a runbook saying "grep for it" returns empty on a
    healthy deployment and teaches the wrong conclusion. It is now reported by
    ``GET /admin/config/status`` beside ``kb_prefetch`` and
    ``first_party_consent_skip``, which are there for the same reason.

    Driven through the built app rather than by calling
    ``_is_debug_enabled()``, because the flag is written by the branch that
    does the mounting and what is under test is that the report and the route
    table agree. ``staging`` is a case of its own: it is neither development nor
    production, the router does NOT mount there by default, and a log line that
    called it production is one of the four claims #1493 lists.
    """
    served = _app_under(**overrides)
    mounted = bool(_debug_routes(served))

    assert mounted is expected, (
        f"{label}: the debug router {'did not mount' if expected else 'mounted'}"
        f" — expected {expected}. Routes: {_debug_routes(served)}"
    )
    assert getattr(served.state, "debug_endpoints_mounted", None) is expected, (
        f"{label}: app.state.debug_endpoints_mounted is "
        f"{getattr(served.state, 'debug_endpoints_mounted', None)!r}, but the "
        f"route table says mounted={mounted}. The report and the mount are "
        "taken in the same if/else and cannot be allowed to disagree"
    )

    from faultmaven.api.routes.admin_config import _debug_endpoints_are_mounted

    assert _debug_endpoints_are_mounted(served) is expected, (
        f"{label}: /admin/config/status would report "
        f"{_debug_endpoints_are_mounted(served)} for a process whose route "
        f"table says mounted={mounted}"
    )


@pytest.mark.integration
@pytest.mark.security
def test_no_gated_operation_resolves_a_collaborator_before_its_gate():
    """The ordering rule, applied to the WHOLE application.

    #1467 found the rule on one route; #1474 wrote a reusable predicate for it
    and then quantified it over a five-element tuple, which is a rule about five
    routes. Run app-wide it reports 53 operations of the same shape — carried in
    ``MISORDERED_GATE_OPERATIONS`` with the issue that closes them (#1494), not
    fixed here, because they span four modules and want their own review.

    What this guard is FOR is that the class stops growing. It fails in both
    directions, like ``PUBLIC_OPERATIONS``: a newly mis-ordered operation is not
    in the allowlist and fails, and an entry that has since been fixed is stale
    and fails too.

    Scoped to operations that ARE gated. An operation with no auth dependency at
    all is a different finding and belongs to the two guards above, which is why
    ``_gate_failure``'s "no mandatory auth dependency at all" answer is filtered
    out here rather than double-reported.
    """
    from tests.integration.api.test_openapi_documents_auth import (
        MANDATORY_AUTH_DEPENDENCIES,
    )

    served = _app_under(ENVIRONMENT="production")
    routes = _served_api_routes(served)

    assert len(routes) > 100, (
        f"only {len(routes)} API routes were found — the enumeration is not "
        "seeing the composed application, and this guard measured almost "
        "nothing. On FastAPI >= 0.139 a flat app.routes scan finds 20 of 144; "
        "on the pinned 0.136 it finds all of them, so a shortfall here means "
        "something else"
    )

    # The two categories ``_gate_failure`` conflates, separated on purpose.
    # It fires for a route with NO auth dependency at all — correct, and
    # expected for ``/auth/login``, ``/auth/register`` and the rest of the
    # public surface, which the two guards above already decide about. Counting
    # those here would produce a number that is mostly the public surface and an
    # allowlist that is wrong. Only the second category is this guard's:
    # operations that HAVE a gate with something resolving ahead of it.
    misordered = set()
    ungated = set()
    for path, methods, dependant in routes:
        if _gate_failure(dependant) is None:
            continue
        gated = bool(MANDATORY_AUTH_DEPENDENCIES & _dependency_names(dependant))
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            (misordered if gated else ungated).add((method, path))

    assert ungated, (
        "no ungated operation was found, so the split above is not splitting "
        "anything — either every route is now gated (report it, it would be "
        "news) or the predicate stopped firing on the public surface"
    )

    for (method, path), (disposition, reason) in MISORDERED_GATE_OPERATIONS.items():
        assert (
            disposition == _MISORDERED
        ), f"{method} {path}: unknown disposition {disposition!r}"
        assert (
            len(reason.strip()) > 20
        ), f"{method} {path}: a reason has to name what resolves ahead of the gate"
        assert (
            "#" in reason
        ), f"{method} {path}: a carried defect must name the issue that tracks it"

    new_offenders = misordered - set(MISORDERED_GATE_OPERATIONS)
    assert not new_offenders, (
        "these operations carry an auth gate but resolve something else "
        "FIRST, so an anonymous caller reaches that collaborator and gets its "
        "failure instead of the 401 (the #1467 shape). Declare the gate on the "
        "decorator, ahead of every collaborator:\n" + _format(new_offenders)
    )

    fixed = set(MISORDERED_GATE_OPERATIONS) - misordered
    assert not fixed, (
        "these MISORDERED_GATE_OPERATIONS entries no longer name a "
        "mis-ordered operation — #1494 is closing, remove them:\n" + _format(fixed)
    )


# ---------------------------------------------------------------------------
# The FastAPI >= 0.139 arm of ``_served_api_routes``, made reachable on the
# version that actually ships.
#
# ``requirements/{test,dev,cloud}.txt`` all pin ``fastapi==0.136.0`` — that is
# what CI installs and what the Dockerfile builds — and ``iter_route_contexts``
# does not exist there, so the whole flattening arm takes the ``is None``
# branch. A test that only drives a real app would therefore leave it
# unexecuted in CI and any regression would ship green. Measured:
#
#     fastapi==0.136.0: `from fastapi.routing import iter_route_contexts`
#                       -> ImportError
#
# Same shape, and the same reasoning, as the fm#1305 arm in
# ``tests/unit/api/middleware/test_composed_route_policy.py``: drive it with
# stand-ins for what the flattener really yields, and keep a live arm that
# becomes real the moment the pin moves.
# ---------------------------------------------------------------------------


class _StubContext:
    """A ``RouteContext``-shaped record, as ``iter_route_contexts`` yields one.

    Carries ``dependant`` because the real one does — on >= 0.139 that is the
    resolved tree, and ``route.dependant`` is the handler's only. A stub without
    it would make the helper's "demanded, never defaulted" check unreachable,
    which is most of what these tests are for.
    """

    def __init__(self, path, methods=frozenset(), route=None, dependant=None):
        self.path = path
        self.methods = methods
        self.route = route
        if dependant is not None:
            self.dependant = dependant


class _StubContextWithoutDependant:
    """The same record from a FastAPI that stopped carrying ``dependant``."""

    def __init__(self, path, methods=frozenset(), route=None):
        self.path = path
        self.methods = methods
        self.route = route


def _route_and_trees():
    """An APIRoute whose own dependant differs from the one that resolves.

    Built by hand rather than by ``include_router`` because on the pinned
    version ``include_router`` copies eagerly and the two trees are the SAME
    object — there would be nothing to tell apart, which is exactly why the
    difference is invisible to CI without this.
    """
    from fastapi import Depends, FastAPI
    from fastapi.routing import APIRoute

    from faultmaven.api.v1.auth_dependencies import require_platform_admin

    probe = FastAPI()

    @probe.get("/leaf")
    async def _leaf(caller=Depends(require_platform_admin)):  # pragma: no cover
        return {}

    route = [r for r in probe.routes if isinstance(r, APIRoute)][0]

    gated = FastAPI(dependencies=[Depends(require_platform_admin)])

    @gated.get("/leaf")
    async def _gated():  # pragma: no cover
        return {}

    resolved = [r for r in gated.routes if isinstance(r, APIRoute)][0].dependant
    return route, resolved


@pytest.mark.integration
@pytest.mark.security
def test_the_flattener_arm_reads_the_contexts_dependant_not_the_routes(monkeypatch):
    """The >= 0.139 arm, executed on the pinned 0.136.

    The bug this forbids is subtle and was shipped once: flattening with
    ``iter_route_contexts`` for the PATH while still reading ``route.dependant``
    for the TREE. Measured on the composed app under 0.141.1, that is wrong for
    **132 of 147 routes**, and all 132 lose the app-level tenant binder — so a
    gate contributed by ``include_router(..., dependencies=[...])`` is invisible
    and its whole router reads as OPEN, while a collaborator contributed the
    same way resolves ahead of the route's own gate with ``_gate_failure``
    answering ``None``.
    """
    import tests.integration.api.test_no_unauthenticated_operations as module

    route, resolved = _route_and_trees()
    assert route.dependant is not resolved, "the fixture does not distinguish them"

    monkeypatch.setattr(
        module,
        "iter_route_contexts",
        lambda routes: [
            _StubContext("/pre/leaf", frozenset({"GET"}), route, resolved),
            _StubContext("", frozenset(), None, None),  # a Mount: no APIRoute
        ],
    )

    served = module._served_api_routes(SimpleNamespace(routes=[]))

    assert [s.path for s in served] == ["/pre/leaf"], (
        "the arm did not run, or it kept a context whose route is not an " "APIRoute"
    )
    assert served[0].methods == frozenset({"GET"})
    assert served[0].dependant is resolved, (
        "the flattened arm returned the ROUTE's dependant. On >= 0.139 that is "
        "the handler's tree only — the path comes from the context and the tree "
        "must come from the same place"
    )


@pytest.mark.integration
@pytest.mark.security
def test_a_context_without_a_dependant_is_refused_rather_than_defaulted(monkeypatch):
    """Falling back to ``route.dependant`` would be silent and wrong.

    If a future FastAPI stops carrying ``dependant`` on the context, the helper
    must say so. Defaulting would reinstate the 132-route defect above with
    every test still green, which is the failure mode this module exists to
    prevent rather than demonstrate.
    """
    import tests.integration.api.test_no_unauthenticated_operations as module

    route, _resolved = _route_and_trees()
    monkeypatch.setattr(
        module,
        "iter_route_contexts",
        lambda routes: [_StubContextWithoutDependant("/pre/leaf", {"GET"}, route)],
    )

    with pytest.raises(AssertionError, match="carries no `dependant`"):
        module._served_api_routes(SimpleNamespace(routes=[]))


@pytest.mark.integration
@pytest.mark.security
def test_the_pre_0_139_arm_is_the_flat_scan(monkeypatch):
    """And it is CORRECT there, not a degraded fallback.

    On the pinned ``fastapi==0.136.0`` ``include_router`` copies eagerly, with
    the prefix merged into the path and the contributed dependencies merged into
    the route's own dependant. Measured, on a router included at
    ``prefix="/pre"`` with an app-level dependency, a router-level one and a
    handler parameter:

        0.136.0  flat APIRoute paths -> ['/direct', '/pre/leaf']
                 route.dependant     -> [app_global, router_gate, handler_dep]
        0.141.1  flat APIRoute paths -> ['/direct']
                 route.dependant     -> [handler_dep]

    So this arm is not second best on the shipped version — it is exactly right,
    and the flattening arm above is forward protection for the pin moving.
    """
    from fastapi import Depends, FastAPI

    import tests.integration.api.test_no_unauthenticated_operations as module
    from faultmaven.api.v1.auth_dependencies import require_platform_admin

    monkeypatch.setattr(module, "iter_route_contexts", None)

    app = FastAPI()

    @app.get("/plain")
    async def _plain(caller=Depends(require_platform_admin)):  # pragma: no cover
        return {}

    served = module._served_api_routes(app)
    paths = {s.path for s in served}

    assert "/plain" in paths, "the flat arm did not run"
    entry = next(s for s in served if s.path == "/plain")
    assert entry.methods == frozenset({"GET"})
    assert (
        _gate_failure(entry.dependant) is None
    ), "the flat arm handed back a tree the predicate cannot read"


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.skipif(
    iter_route_contexts is None,
    reason="fastapi < 0.139 copies included routes in eagerly; nothing to flatten",
)
def test_a_really_included_router_is_flattened_with_its_resolved_tree():
    """The live arm. Vacuous on the pin, real the moment it moves.

    Kept beside the injected ones rather than instead of them: the stubs prove
    the code does the right thing with the shape, and this proves the shape is
    the one FastAPI really produces.
    """
    from fastapi import APIRouter, Depends, FastAPI

    import tests.integration.api.test_no_unauthenticated_operations as module
    from faultmaven.api.v1.auth_dependencies import require_platform_admin

    sub = APIRouter()

    @sub.get("/leaf")
    async def _leaf():  # pragma: no cover
        return {}

    app = FastAPI(dependencies=[Depends(require_platform_admin)])
    app.include_router(sub, prefix="/pre")

    served = {s.path: s for s in module._served_api_routes(app)}

    assert "/pre/leaf" in served, (
        "the effective path was not recovered — an unprefixed '/leaf' here "
        "means the walk reached original_router instead of the flattener"
    )
    assert _gate_failure(served["/pre/leaf"].dependant) is None, (
        "the app-level gate is absent from the tree this helper returned, so "
        "it read route.dependant rather than the context's"
    )


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
