"""One door for an investigation turn, and a gate that fails when a second appears.

The per-tenant turn cap is charged inside
``InvestigationService.process_turn`` (ADR-016 D5.3), so the invariant "every
turn is bounded" holds for every caller of that service **by construction** —
no route can forget a dependency, because there is none to forget.

That leaves one thing worth asserting at the HTTP surface, and it is the reason
this module still exists: *how many ways in are there?* If a second operation
ever reaches the investigation service, the cap still bounds it — but the
product has grown a surface nobody costed, and the person adding it should have
to say so. So this asks the **running application**, through two independent
detectors, and requires every operation either flags to carry a recorded
verdict.

Two detectors, deliberately
---------------------------
**Shape**, from the live OpenAPI document: an operation whose success response
is a ``TurnResponse``, whose request body carries the turn form fields, or whose
path ends in ``/turns``. **Reachability**, from the live route objects: a
handler that can reach the investigation service through a declared dependency
or an import in its body. Either alone has a blind spot — a new route could
consume a turn while returning a different model, or reach the service through a
name this file has never heard of.

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

from tests.unit.api.middleware.test_rate_limit_read_cost_classification import (
    MOUNTS as _MOUNTS,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]


_CAPPED = "capped"
_EXEMPT = "exempt"

#: Every operation that can accept an investigation turn, and what guards it.
#:
#: Entries are ``(method, path) -> (verdict, reason)``. A verdict of ``capped``
#: means the operation reaches ``process_turn`` and is therefore bounded by the
#: reservation inside it; ``exempt`` means the probe flagged it but it consumes
#: no turn, and the reason has to say why.
TURN_SURFACE_INVENTORY: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/api/v1/cases/{case_id}/turns"): (
        _CAPPED,
        "the single entry point: every client — Copilot, Dashboard, the Slack "
        "agent, any API caller — submits turns here, and it is the only route "
        "that reaches InvestigationService.process_turn, where the reservation "
        "is taken.",
    ),
    ("PATCH", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"): (
        _EXEMPT,
        "flagged by reachability because it holds the investigation service, "
        "but it calls reclassify_evidence, not process_turn: it re-runs the "
        "Tier 0/1 mechanical preprocessor over bytes already stored, consumes "
        "no turn number and reaches no model. It is also off by default "
        "(FAULTMAVEN_RECLASSIFY_ENABLED). Charging it would bill a tenant for "
        "correcting the classifier's mistake.",
    ),
}

#: Request-body fields only a turn submission carries. ``query`` alone is
#: deliberately NOT here — it is a search parameter on several read routes.
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
                # and are not separate operations.
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


def test_the_turn_surface_is_exactly_what_is_recorded(spec):
    """One equality, both directions.

    An operation either probe flags and the inventory does not name is an
    uncosted way into the investigation engine. An inventory entry neither probe
    flags any more is a verdict about a route that no longer exists — which,
    left alone, is a green test asserting nothing about anything.

    It is one assertion rather than the three it used to be because the cap no
    longer lives on the routes: with the reservation inside ``process_turn``
    there is no per-route guard to check for presence or absence, and asking
    "which routes carry the dependency" would now be asking about a thing that
    does not exist.
    """
    live = flagged_operations(spec)

    assert set(live) == set(TURN_SURFACE_INVENTORY), (
        "the turn-accepting surface has changed.\n"
        "  flagged but not recorded: "
        + str(sorted(set(live) - set(TURN_SURFACE_INVENTORY)))
        + "\n  recorded but no longer flagged: "
        + str(sorted(set(TURN_SURFACE_INVENTORY) - set(live)))
        + "\nRecord a new operation as capped (it reaches process_turn and is "
        "bounded by the reservation there) or as exempt, with the reason."
    )


def test_each_inventory_entry_states_a_reason_somebody_wrote():
    """A one-word verdict is a rubber stamp; the reason is the review."""
    for key, (verdict, reason) in sorted(TURN_SURFACE_INVENTORY.items()):
        assert verdict in (_CAPPED, _EXEMPT), key
        assert len(reason) > 40, f"{key} carries no real reason: {reason!r}"


def test_exactly_one_operation_consumes_a_turn():
    """States the claim the design rests on, so a widening is visible in a diff."""
    capped = {
        key
        for key, (verdict, _) in TURN_SURFACE_INVENTORY.items()
        if verdict == _CAPPED
    }
    assert capped == {("POST", "/api/v1/cases/{case_id}/turns")}
