"""One door, and a gate that fails when a second one appears (ADR-016 D5.3).

The per-tenant turn cap is enforced in exactly one place —
``POST /cases/{case_id}/turns``, through
``modules/case/api/turn_cap.enforce_tenant_turn_cap``. That is only a bound on
spend while it stays true, and "it is true today" is not a property a reader can
check. So this module asks the **running application** two questions on every
run:

1. Which operations can accept an investigation turn? Every one of them must be
   classified in :data:`TURN_SURFACE_INVENTORY` — as capped, or as exempt with a
   stated reason. A turn-accepting route added tomorrow fails this module until
   somebody decides which it is.
2. Does every operation classified as capped actually carry the guard, and does
   no other operation carry it? The second half is not symmetry for its own
   sake: the guard *reserves a turn*, so a copy of it landing on a read would
   spend the tenant's allowance on reading, and invariant 1's promise that reads
   and sign-in keep working at the cap would quietly stop being true.

Modelled on ``tests/integration/security/test_two_tenant_surface_probe.py``'s
``test_every_tenant_scoped_route_is_in_the_inventory``, and for the same reason:
it is the only kind of assertion here that cannot rot silently, because it reads
the live surface rather than a list written when the module was.

Two detectors, deliberately
---------------------------
**Shape**, from the OpenAPI document: an operation whose success response is a
``TurnResponse``, or whose request body carries the turn form fields, or whose
path ends in ``/turns``. **Reachability**, from the route objects: an operation
whose handler can reach ``InvestigationService.process_turn``, through a
declared dependency or an import in its body. Either one alone has a blind spot
— a new route could consume a turn while returning a different model, or could
reach the service through a name this file has never heard of — and a route
flagged by either must be classified.

The reachability half walks the routers listed in ``MOUNTS``, which this module
**imports** from the read-cost probe rather than copying: that table is asserted
against ``main.py``'s own ``include_router`` calls by
``test_the_mount_table_matches_main``, and a second copy here would drift alone
while that guard kept passing.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap

import pytest

from faultmaven.modules.case.api.turn_cap import enforce_tenant_turn_cap

# The routers mounted in ``main.py``, imported from the read-cost probe rather
# than copied. That is the point rather than convenience: a second table here
# would drift on its own, and ``test_the_mount_table_matches_main`` — which
# asserts THIS object against ``main.py``'s own ``include_router`` calls — would
# keep passing while this module quietly stopped seeing a whole router. One
# table, one guard.
from tests.unit.api.middleware.test_rate_limit_read_cost_classification import (
    MOUNTS as _MOUNTS,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]


_CAPPED = "capped"
_EXEMPT = "exempt"

#: Every operation that can accept an investigation turn, and what guards it.
#:
#: The inventory is tiny because the surface is: one route consumes a turn.
#: Entries are ``(method, path) -> (verdict, reason)``; a verdict of ``capped``
#: additionally requires the guard to be present in that route's dependency
#: tree, which is asserted separately.
TURN_SURFACE_INVENTORY: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/api/v1/cases/{case_id}/turns"): (
        _CAPPED,
        "the single entry point: every client — Copilot, Dashboard, the Slack "
        "agent, any API caller — submits turns here, and it is the only route "
        "that reaches InvestigationService.process_turn.",
    ),
    ("PATCH", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"): (
        _EXEMPT,
        "flagged by reachability because it holds the investigation service, but "
        "it calls reclassify_evidence, not process_turn: it re-runs the Tier 0/1 "
        "mechanical preprocessor over bytes already stored, consumes no turn "
        "number and reaches no model. It is also off by default "
        "(FAULTMAVEN_RECLASSIFY_ENABLED). Capping it would charge a tenant's "
        "daily allowance for correcting the classifier's mistake.",
    ),
}

#: Request-body fields that only a turn submission carries. ``query`` alone is
#: deliberately NOT here — it is a search parameter on several read routes, and
#: including it would make this detector fire on things that consume nothing.
_TURN_BODY_FIELDS = frozenset({"pasted_content", "intent_type", "intent_data"})

#: The response model a turn produces.
_TURN_RESPONSE_SCHEMA = "TurnResponse"

#: Names in a handler's dependency tree, or modules imported in its body, that
#: put ``InvestigationService.process_turn`` within reach. Substring matches on
#: purpose: a provider named ``get_turn_service`` should trip this without being
#: added here.
_TURN_SERVICE_MARKERS = ("investigation_service", "process_turn", "turn_service")


@pytest.fixture(scope="module")
def spec():
    from faultmaven.main import app

    return app.openapi()


def _schema_names(node, seen=None) -> set[str]:
    """Every ``$ref``-ed schema name reachable from an OpenAPI fragment."""
    seen = seen if seen is not None else set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            seen.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _schema_names(value, seen)
    elif isinstance(node, list):
        for value in node:
            _schema_names(value, seen)
    return seen


def _body_fields(operation, schemas) -> set[str]:
    fields: set[str] = set()
    for content in (operation.get("requestBody") or {}).get("content", {}).values():
        schema = content.get("schema", {})
        ref = schema.get("$ref")
        if ref:
            schema = schemas.get(ref.rsplit("/", 1)[-1], {})
        fields |= set(schema.get("properties", {}) or {})
    return fields


def turn_shaped_operations(spec) -> dict[tuple[str, str], str]:
    """Every operation in the LIVE document that looks like it accepts a turn."""
    schemas = spec.get("components", {}).get("schemas", {})
    found: dict[tuple[str, str], str] = {}

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            reasons = []
            if re.search(r"/turns/?$", path):
                reasons.append("path")
            if _TURN_RESPONSE_SCHEMA in _schema_names(operation.get("responses", {})):
                reasons.append(f"response:{_TURN_RESPONSE_SCHEMA}")
            hit = _body_fields(operation, schemas) & _TURN_BODY_FIELDS
            if hit:
                reasons.append("body:" + ",".join(sorted(hit)))
            if reasons:
                found[(method.upper(), path)] = ";".join(reasons)
    return found


def _dependency_calls(route) -> list:
    """Every callable in a route's dependency tree."""
    calls, stack = [], [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            calls.append(dependant.call)
        stack.extend(getattr(dependant, "dependencies", []))
    return calls


def _handler_imports(endpoint) -> set[str]:
    """Modules imported anywhere in a handler, including inside its body."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    except (OSError, TypeError, SyntaxError):  # pragma: no cover - defensive
        return set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def turn_reaching_routes() -> dict[tuple[str, str], str]:
    """Every mounted route whose handler can reach the turn service."""
    found: dict[tuple[str, str], str] = {}
    for prefix, module_name in _MOUNTS:
        router = importlib.import_module(module_name).router
        for route in router.routes:
            names = {
                (getattr(call, "__name__", "") or "").lower()
                for call in _dependency_calls(route)
            }
            imports = {module.lower() for module in _handler_imports(route.endpoint)}
            hits = [
                marker
                for marker in _TURN_SERVICE_MARKERS
                if any(marker in name for name in names)
                or any(marker in module for module in imports)
            ]
            if not hits:
                continue
            for method in getattr(route, "methods", None) or set():
                # HEAD and OPTIONS are added by the framework for a declared GET
                # and are not separate operations; counting them would put a
                # phantom entry in the inventory for every flagged read.
                if method.upper() in ("HEAD", "OPTIONS"):
                    continue
                found[(method.upper(), prefix + route.path)] = ",".join(sorted(hits))
    return found


def flagged_operations(spec) -> dict[tuple[str, str], str]:
    """The union of both detectors — everything that owes a verdict.

    Combined rather than checked leg by leg so an entry stays live while EITHER
    probe still flags it: a route classified on reachability grounds would
    otherwise read as "stale" to the shape probe and be deleted, taking its
    recorded reason with it.
    """
    merged = dict(turn_shaped_operations(spec))
    for key, why in turn_reaching_routes().items():
        merged[key] = ";".join(sorted({merged.get(key, ""), f"reaches:{why}"} - {""}))
    return merged


def guarded_routes() -> set[tuple[str, str]]:
    """Every mounted route carrying ``enforce_tenant_turn_cap``."""
    guarded: set[tuple[str, str]] = set()
    for prefix, module_name in _MOUNTS:
        router = importlib.import_module(module_name).router
        for route in router.routes:
            if enforce_tenant_turn_cap not in _dependency_calls(route):
                continue
            for method in getattr(route, "methods", None) or set():
                guarded.add((method.upper(), prefix + route.path))
    return guarded


# =============================================================================
# The inventory cannot rot silently
# =============================================================================


def test_every_turn_accepting_operation_is_in_the_inventory(spec):
    """Invariant 4. A second door fails this module until somebody classifies it.

    Both directions, on purpose. An operation missing from the inventory is an
    unguarded way to spend a tenant's compute. An inventory entry naming an
    operation the app no longer exposes is a guard aimed at nothing — which,
    left alone, is a green test asserting nothing about anything.
    """
    live = flagged_operations(spec)

    unclassified = {
        key: why for key, why in live.items() if key not in TURN_SURFACE_INVENTORY
    }
    assert not unclassified, (
        "these operations look like they accept an investigation turn and are "
        "not in TURN_SURFACE_INVENTORY. Guard them with "
        "enforce_tenant_turn_cap and record them as capped, or add an entry "
        "saying why they cost a tenant nothing:\n"
        + "\n".join(
            f"  {m} {p}  ({why})" for (m, p), why in sorted(unclassified.items())
        )
    )

    stale = sorted(set(TURN_SURFACE_INVENTORY) - set(live))
    assert not stale, (
        "these TURN_SURFACE_INVENTORY entries name operations neither probe "
        "flags any more — the route was renamed or removed, and the verdict is "
        "now a guard aimed at nothing:\n" + "\n".join(f"  {m} {p}" for m, p in stale)
    )


def test_every_route_that_can_reach_the_turn_service_is_in_the_inventory():
    """The detector the OpenAPI shape cannot be: reachability.

    A route could consume a turn while returning some other model and taking a
    body this file has never seen — the shape probe would miss it entirely. This
    asks the route objects instead: can the handler get at the investigation
    service at all, through a declared dependency or an import in its body?
    """
    reaching = turn_reaching_routes()
    assert reaching, "the reachability probe resolved no routes at all"

    unclassified = {
        key: why for key, why in reaching.items() if key not in TURN_SURFACE_INVENTORY
    }
    assert not unclassified, (
        "these routes can reach the investigation service and are not "
        "classified in TURN_SURFACE_INVENTORY:\n"
        + "\n".join(
            f"  {m} {p}  ({why})" for (m, p), why in sorted(unclassified.items())
        )
    )


def test_every_capped_operation_actually_carries_the_guard():
    """A verdict of "capped" has to be a fact about the running app.

    Read off the route's dependency tree rather than by grepping the handler:
    the guard is declared as a route dependency precisely so this question has
    a structural answer.
    """
    expected = {
        key
        for key, (verdict, _) in TURN_SURFACE_INVENTORY.items()
        if verdict == _CAPPED
    }
    guarded = guarded_routes()

    missing = sorted(expected - guarded)
    assert not missing, (
        "these operations are recorded as capped but do not carry "
        f"enforce_tenant_turn_cap: {missing}"
    )


def test_the_guard_is_on_no_other_operation(spec):
    """Invariant 1's other half, structurally.

    The guard RESERVES a turn, so it is not a harmless thing to over-apply: a
    copy on a read route would spend the tenant's daily allowance on reading,
    and "reads and sign-in keep working at the cap" would stop being true
    without a single test failing. So the guarded set must be exactly the capped
    set — and, said again in the terms the promise is made in, must contain no
    read and no auth route.
    """
    expected = {
        key
        for key, (verdict, _) in TURN_SURFACE_INVENTORY.items()
        if verdict == _CAPPED
    }
    guarded = guarded_routes()

    unexpected = sorted(guarded - expected)
    assert not unexpected, (
        "these operations carry the turn cap guard but are not recorded as "
        f"capped — each one now spends a tenant's daily allowance: {unexpected}"
    )

    reads = sorted((m, p) for (m, p) in guarded if m in {"GET", "HEAD", "OPTIONS"})
    assert not reads, f"a read operation reserves a turn: {reads}"

    auth = sorted((m, p) for (m, p) in guarded if "/auth" in p)
    assert not auth, f"an authentication operation reserves a turn: {auth}"


def test_each_inventory_entry_states_a_reason_somebody_wrote():
    """A one-word verdict is a rubber stamp; the reason is the review."""
    for key, (verdict, reason) in sorted(TURN_SURFACE_INVENTORY.items()):
        assert verdict in (_CAPPED, _EXEMPT), key
        assert len(reason) > 40, f"{key} carries no real reason: {reason!r}"


def test_exactly_one_operation_is_capped():
    """States the claim the design rests on, so a widening is visible in a diff.

    Not "the inventory has one entry" — it may grow exemptions, and each of
    those is a decision somebody recorded. The claim is narrower and is the one
    the cap's soundness depends on: exactly one operation spends a turn.
    """
    capped = {
        key
        for key, (verdict, _) in TURN_SURFACE_INVENTORY.items()
        if verdict == _CAPPED
    }
    assert capped == {("POST", "/api/v1/cases/{case_id}/turns")}
