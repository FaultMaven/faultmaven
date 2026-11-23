"""A container factory never turns its own construction failure into ``None``
unseen (#3003; principles §7, "Startup: refuse, or degrade knowingly").

#2103, #2342 and #3003 found the same shape factory by factory: an ``except``
around a constructor that logged and returned ``None``, so the container's
must-not-degrade gate never saw the failure. Every broad handler
(``except Exception``, ``except BaseException`` or a bare ``except``) in
``faultmaven/container/providers/*.py`` whose last statement is not a ``raise``
is listed in ``ARMS``, keyed by function, with its class and its reason:

* ``MUST_NOT_DEGRADE``: the arm calls ``unbuilt_where_must_not_degrade``,
  which refuses the start where the deployment must not degrade;
* ``UNDER_MULTI``: a tenancy or sharing factory, whose arm calls
  ``_unbuilt_under_multi`` (#2342);
* ``DECLARED``: a line that degrades by declaration, the reason naming it.

A new arm, an arm that drops its call, or a ``raise`` removed fails it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROVIDERS = Path(__file__).resolve().parents[3] / "faultmaven/container/providers"

MUST_NOT_DEGRADE = "unbuilt_where_must_not_degrade"
UNDER_MULTI = "_unbuilt_under_multi"
DECLARED = None

_REFUSES_WHERE_MUST_NOT_DEGRADE = (
    "a configured service; refused where the deployment must not degrade, "
    "served without it elsewhere (#3003)"
)
_TENANCY = "single-tenant None is configuration's answer; refused under multi (#2342)"

# (file, function) -> (class, number of arms, reason)
ARMS: dict[tuple[str, str], tuple[str | None, int, str]] = {
    ("infrastructure.py", "_create_chromadb_client"): (
        DECLARED,
        1,
        "an unreachable configured server: standalone falls back to its local "
        "client, which every reader then shares, and local_chroma_or_fail "
        "refuses under cloud (#901)",
    ),
    ("infrastructure.py", "register_infrastructure"): (
        MUST_NOT_DEGRADE,
        2,
        "file storage and the vector store: " + _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
    ("services.py", "create_milestone_engine"): (
        MUST_NOT_DEGRADE,
        1,
        _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
    ("services.py", "create_investigation_service"): (
        MUST_NOT_DEGRADE,
        1,
        "the turn cap included: " + _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
    ("services.py", "create_user_service"): (
        MUST_NOT_DEGRADE,
        1,
        _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
    ("services.py", "create_report_generation_service"): (
        MUST_NOT_DEGRADE,
        1,
        _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
    ("services.py", "create_organization_repository"): (UNDER_MULTI, 1, _TENANCY),
    ("services.py", "create_enterprise_repository"): (UNDER_MULTI, 1, _TENANCY),
    ("services.py", "create_team_repository"): (UNDER_MULTI, 1, _TENANCY),
    ("services.py", "create_share_repository"): (UNDER_MULTI, 1, _TENANCY),
    ("services.py", "create_tenant_provider"): (UNDER_MULTI, 1, _TENANCY),
    ("services.py", "register_services"): (
        DECLARED,
        1,
        "the conversion service, optional by §7's declaration",
    ),
    ("tools.py", "create_registry_tools"): (
        MUST_NOT_DEGRADE,
        1,
        "web search, configured: " + _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
    ("tools.py", "create_document_qa_tools"): (
        MUST_NOT_DEGRADE,
        1,
        "kb_qa and case_evidence_search: " + _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
    ("tools.py", "register_tools"): (
        MUST_NOT_DEGRADE,
        4,
        "search_file, vectorize_file, reclassify_evidence and the entity tools: "
        + _REFUSES_WHERE_MUST_NOT_DEGRADE,
    ),
}


def _broad(handler: ast.ExceptHandler) -> bool:
    kinds = handler.type
    if kinds is None:
        return True
    names = kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]
    return any(
        isinstance(n, ast.Name) and n.id in ("Exception", "BaseException")
        for n in names
    )


def _calls(handler: ast.ExceptHandler, name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        for node in ast.walk(handler)
    )


def violations(
    filename: str,
    source: str,
    arms: dict[tuple[str, str], tuple[str | None, int, str]],
) -> list[str]:
    """Each broad, non-raising arm in ``source`` that ``arms`` does not account for."""
    found: dict[str, list[ast.ExceptHandler]] = {}

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else function
            )
            if (
                isinstance(child, ast.ExceptHandler)
                and _broad(child)
                and not isinstance(child.body[-1], ast.Raise)
            ):
                found.setdefault(inner, []).append(child)
            visit(child, inner)

    visit(ast.parse(source), "<module>")
    problems = []
    for function, handlers in found.items():
        entry = arms.get((filename, function))
        lines = ", ".join(str(h.lineno) for h in handlers)
        if entry is None:
            problems.append(f"{filename}::{function} (line {lines}) is not listed")
            continue
        kind, count, _reason = entry
        if len(handlers) != count:
            problems.append(
                f"{filename}::{function} has {len(handlers)} arms (line {lines}), "
                f"{count} listed"
            )
        if kind is not None:
            problems += [
                f"{filename}::{function} line {h.lineno} does not call {kind}"
                for h in handlers
                if not _calls(h, kind)
            ]
    problems += [
        f"{f}::{fn} is listed but has no such arm"
        for (f, fn) in arms
        if f == filename and fn not in found
    ]
    return problems


def test_every_swallowing_arm_is_classified():
    problems = [
        problem
        for path in sorted(PROVIDERS.glob("*.py"))
        for problem in violations(path.name, path.read_text(), ARMS)
    ]
    assert problems == []


def test_every_listed_arm_names_a_reason():
    assert all(reason for _kind, _count, reason in ARMS.values())


_CATCH_ALL = """
def create_thing():
    try:
        return Thing()
    except Exception as e:
        logger.warning("Thing failed")
        return None
"""


def test_a_catch_all_factory_fails_it():
    """Positive control: the #3003 shape, unlisted, then listed without its call."""
    assert violations("x.py", _CATCH_ALL, {}) == [
        "x.py::create_thing (line 5) is not listed"
    ]
    listed = {("x.py", "create_thing"): (MUST_NOT_DEGRADE, 1, "r")}
    assert violations("x.py", _CATCH_ALL, listed) == [
        f"x.py::create_thing line 5 does not call {MUST_NOT_DEGRADE}"
    ]


def test_a_new_arm_fails_it():
    listed = {("x.py", "create_thing"): (DECLARED, 1, "r")}
    assert violations("x.py", _CATCH_ALL, listed) == []
    second = _CATCH_ALL.replace(
        "    try:\n",
        "    try:\n        Part()\n    except Exception:\n        pass\n    try:\n",
        1,
    )
    assert violations("x.py", second, listed) == [
        "x.py::create_thing has 2 arms (line 5, 9), 1 listed"
    ]


@pytest.mark.parametrize(
    "arm,listed",
    [
        ('raise RequiredServiceConstructionError("Thing") from e', {}),
        (
            '_unbuilt_under_multi("Thing", e)\n        return None',
            {("x.py", "create_thing"): (UNDER_MULTI, 1, "r")},
        ),
        (
            'unbuilt_where_must_not_degrade("Thing", e)\n        return None',
            {("x.py", "create_thing"): (MUST_NOT_DEGRADE, 1, "r")},
        ),
    ],
    ids=["raises", "under-multi", "must-not-degrade"],
)
def test_a_refusing_arm_passes(arm, listed):
    """Negative controls: an arm that raises needs no entry; a listed arm that
    makes its call passes."""
    source = _CATCH_ALL.replace("return None\n", "").replace(
        'logger.warning("Thing failed")', arm
    )
    assert violations("x.py", source, listed) == []
