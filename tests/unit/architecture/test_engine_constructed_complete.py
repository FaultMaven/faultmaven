"""No production code assigns a MilestoneEngine dependency after construction (#1722).

The composition root used to build the engine early and assign five of its
dependencies afterwards: ``report_service``, ``knowledge_service``,
``team_service`` and ``share_repository`` in ``register_services``, and
``conversion_service`` in the lifespan. The engine then read three of them with
``getattr(self, ..., None)``. Late wiring is invisible to anything that
snapshots a dependency in ``__init__``: a collaborator built there keeps the
``None`` it was constructed with, and nothing fails. In production that means
no report, no runbook draft and no team KB.

The rule this pins: every constructor dependency arrives through
``MilestoneEngine.__init__``. The parameter names come from the live
signature, so a new dependency is covered the day it is added. A receiver is
anything whose last dotted segment names an engine (``engine``,
``milestone_engine``, ``_milestone_engine``, ``self.engine``), which is how
the composition root and the services refer to it. The engine's own module is
exempt: ``__init__`` is where these are meant to be set.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3] / "faultmaven"
_ENGINE_PACKAGE = _ROOT / "core" / "investigation" / "milestone_engine"


def _dependency_names() -> set[str]:
    params = inspect.signature(MilestoneEngine.__init__).parameters
    return {name for name in params if name != "self"}


def _late_assignments(tree: ast.AST, deps: set[str]) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if not (isinstance(target, ast.Attribute) and target.attr in deps):
                continue
            receiver = ast.unparse(target.value).split(".")[-1].lower()
            if receiver.endswith("engine"):
                hits.append(f"line {target.lineno}: {ast.unparse(target)} = ...")
    return hits


def test_the_signature_names_the_formerly_late_dependencies():
    # Positive control on the rule's input: if these ever leave the
    # constructor, the scan below would silently stop looking for them.
    assert {
        "report_service",
        "knowledge_service",
        "team_service",
        "share_repository",
        "conversion_service",
    } <= _dependency_names()


def test_no_engine_dependency_is_assigned_after_construction():
    deps = _dependency_names()
    offenders = {}
    for path in sorted(_ROOT.rglob("*.py")):
        if _ENGINE_PACKAGE in path.parents:
            continue
        hits = _late_assignments(ast.parse(path.read_text()), deps)
        if hits:
            offenders[str(path.relative_to(_ROOT.parent))] = hits
    assert not offenders, (
        "MilestoneEngine dependencies assigned after construction; pass them to "
        f"the constructor instead (#1722): {offenders}"
    )


def test_the_scan_catches_the_shapes_it_replaced():
    # The two late-binding blocks this guard replaced, verbatim in shape.
    deps = _dependency_names()
    source = (
        "if report_generation_service and milestone_engine:\n"
        "    milestone_engine.report_service = report_generation_service\n"
        "_milestone_engine = getattr(container, 'milestone_engine', None)\n"
        "_milestone_engine.conversion_service = _conversion_svc\n"
    )
    assert len(_late_assignments(ast.parse(source), deps)) == 2
