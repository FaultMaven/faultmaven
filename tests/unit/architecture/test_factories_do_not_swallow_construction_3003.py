"""A composition never turns its own construction failure into ``None``
unseen (#3003; principles §7, "Startup: refuse, or degrade knowingly").

#2103, #2342 and #3003 found the same shape factory by factory: an ``except``
around a constructor that logged and returned ``None``, so the container's
must-not-degrade gate never saw the failure. Every ``except`` arm, of any
exception class, in the composition's files (``SCOPE``) that can complete
without raising is listed in ``ARMS``: keyed by function, one entry per arm in
source order, each with its class and its reason:

* ``MUST_NOT_DEGRADE``: the arm calls ``unbuilt_where_must_not_degrade``,
  which refuses the start where the deployment must not degrade;
* ``UNDER_MULTI``: a tenancy or sharing factory, whose arm calls
  ``_unbuilt_under_multi`` (#2342);
* ``DECLARED``: an arm that degrades by declaration, or builds nothing.

A refusing arm makes its call as a statement of its own body, before any
``return``, ``continue`` or ``break``. A new arm, an arm that drops, hides or
postpones its call, or a ``raise`` an arm no longer reaches on every path fails
it, and each reason names its issue, its configuration, §7 or, in backticks,
the code that grounds it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SCOPE = (
    *sorted((ROOT / "faultmaven/container/providers").glob("*.py")),
    ROOT / "faultmaven/_container_impl.py",
    ROOT / "faultmaven/bootstrap/composition.py",
)

MUST_NOT_DEGRADE = "unbuilt_where_must_not_degrade"
UNDER_MULTI = "_unbuilt_under_multi"
DECLARED = None

_CONFIGURED = (
    MUST_NOT_DEGRADE,
    "a configured service: refused where the deployment must not degrade, "
    "served without it elsewhere (#3003)",
)
_TENANCY = (
    UNDER_MULTI,
    "single-tenant None is configuration's answer; refused under "
    "TENANT_PROVIDER=multi (#2342)",
)

# (file, function) -> one (class, reason) per arm, in source order
ARMS: dict[tuple[str, str], tuple[tuple[str | None, str], ...]] = {
    ("infrastructure.py", "_create_chromadb_client"): (
        (
            DECLARED,
            "a configured CHROMADB_URL unreachable: standalone falls back to its "
            "local client, which every reader shares; local_chroma_or_fail "
            "refuses under cloud, a capability gate on is_cloud (#901)",
        ),
    ),
    ("infrastructure.py", "register_infrastructure"): (_CONFIGURED, _CONFIGURED),
    ("services.py", "create_milestone_engine"): (_CONFIGURED,),
    ("services.py", "create_investigation_service"): (_CONFIGURED,),
    ("services.py", "create_user_service"): (_CONFIGURED,),
    ("services.py", "create_report_generation_service"): (_CONFIGURED,),
    ("services.py", "create_organization_repository"): (_TENANCY,),
    ("services.py", "create_enterprise_repository"): (_TENANCY,),
    ("services.py", "create_team_repository"): (_TENANCY,),
    ("services.py", "create_share_repository"): (_TENANCY,),
    ("services.py", "create_tenant_provider"): (_TENANCY,),
    ("services.py", "register_services"): (
        _CONFIGURED,  # the signing generator's SigningKeyUnavailableError
        _CONFIGURED,  # the OAuth generator's SigningKeyUnavailableError
        (DECLARED, "the conversion service, optional by §7's declaration"),
    ),
    ("tools.py", "create_registry_tools"): (_CONFIGURED,),
    ("tools.py", "create_document_qa_tools"): (_CONFIGURED,),
    ("tools.py", "register_tools"): (_CONFIGURED,) * 4,
    ("_container_impl.py", "<module>"): (
        (
            DECLARED,
            "interface imports: their absence sets INTERFACES_AVAILABLE false, "
            "which selects the minimal container on a failed composition",
        ),
        (
            DECLARED,
            "agentic interface imports, read only as annotations: each name "
            "binds `Any` when absent, and nothing is constructed",
        ),
    ),
    ("_container_impl.py", "initialize"): (
        (
            DECLARED,
            "the container's own gate (§7): refuses where must_not_degrade, and "
            "re-raises the refusal classes first",
        ),
        (
            DECLARED,
            "the tenancy module unimportable is not a tenancy refusal (#885); "
            "the gate it sits in decides",
        ),
    ),
    ("_container_impl.py", "get_job_service"): (_CONFIGURED,),
    ("composition.py", "_is_test_environment"): (
        (
            DECLARED,
            "settings unreadable: reads SKIP_SERVICE_CHECKS and "
            "PYTEST_CURRENT_TEST from the environment; builds nothing",
        ),
    ),
    ("composition.py", "_check_llm_configuration"): (
        (
            DECLARED,
            "settings unreadable: skips the no-LLM-provider warning, the "
            "function's only output (`_check_llm_configuration`)",
        ),
    ),
    ("composition.py", "_wire_composition_root"): (
        (
            MUST_NOT_DEGRADE,
            "the LLM config overrides, which outrank the environment "
            "(DEPLOYMENT_MODE=cloud): refused where the deployment must not "
            "degrade (#3003)",
        ),
        (DECLARED, "the conversion service's tables, optional by §7's declaration"),
        (DECLARED, "the query classification engine, optional by §7's declaration"),
    ),
    ("composition.py", "compose_application"): (
        (
            DECLARED,
            "the composition root's gate (§7): refuses where must_not_degrade",
        ),
    ),
}

_REFERENCE = re.compile(r"#\d+|§7|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|`\w+`")


def _always_raises(body: list[ast.stmt]) -> bool:
    """Whether every path through ``body`` ends in a ``raise``."""
    if any(
        isinstance(node, (ast.Return, ast.Continue, ast.Break))
        for statement in body
        for node in ast.walk(statement)
    ):
        return False
    last = body[-1]
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.If):
        return bool(last.orelse) and (
            _always_raises(last.body) and _always_raises(last.orelse)
        )
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _always_raises(last.body)
    return False


def _leaves(statement: ast.stmt) -> bool:
    """Whether ``statement`` can leave the handler, nested scopes aside."""
    pending: list[ast.AST] = [statement]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Return, ast.Continue, ast.Break)):
            return True
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            pending.extend(ast.iter_child_nodes(node))
    return False


def _arm_problem(handler: ast.ExceptHandler, kind: str) -> str | None:
    """``None`` when the handler calls ``kind`` as a statement of its own body
    before anything that can leave it."""
    calls = [
        index
        for index, statement in enumerate(handler.body)
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == kind
    ]
    if not calls:
        return f"does not call {kind} as a statement of its body"
    if any(_leaves(statement) for statement in handler.body[: calls[0]]):
        return f"leaves before it calls {kind}"
    return None


def violations(
    filename: str,
    source: str,
    arms: dict[tuple[str, str], tuple[tuple[str | None, str], ...]],
) -> list[str]:
    """Each arm in ``source`` that can complete without raising and that
    ``arms`` does not account for."""
    found: dict[str, list[ast.ExceptHandler]] = {}

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else function
            )
            if isinstance(child, ast.ExceptHandler) and not _always_raises(child.body):
                found.setdefault(inner, []).append(child)
            visit(child, inner)

    visit(ast.parse(source), "<module>")
    problems = []
    for function, handlers in found.items():
        handlers.sort(key=lambda h: h.lineno)
        lines = ", ".join(str(h.lineno) for h in handlers)
        entries = arms.get((filename, function))
        if entries is None:
            problems.append(f"{filename}::{function} (line {lines}) is not listed")
            continue
        if len(handlers) != len(entries):
            problems.append(
                f"{filename}::{function} has {len(handlers)} arms (line {lines}), "
                f"{len(entries)} listed"
            )
            continue
        for handler, (kind, _reason) in zip(handlers, entries):
            problem = kind and _arm_problem(handler, kind)
            if problem:
                problems.append(
                    f"{filename}::{function} line {handler.lineno} {problem}"
                )
    problems += [
        f"{f}::{fn} is listed but has no such arm"
        for (f, fn) in arms
        if f == filename and fn not in found
    ]
    return problems


def unreferenced(
    arms: dict[tuple[str, str], tuple[tuple[str | None, str], ...]],
) -> list[str]:
    """Each listed reason that names no issue, configuration or §7."""
    return [
        f"{f}::{fn}: {reason!r}"
        for (f, fn), entries in arms.items()
        for _kind, reason in entries
        if not _REFERENCE.search(reason)
    ]


def test_every_arm_that_can_complete_is_classified():
    problems = [
        problem
        for path in SCOPE
        for problem in violations(path.name, path.read_text(), ARMS)
    ]
    assert problems == []


def test_every_listed_reason_names_its_ground():
    assert unreferenced(ARMS) == []


def test_a_bare_reason_fails_it():
    assert unreferenced({("x.py", "f"): ((DECLARED, "optional"),)}) == [
        "x.py::f: 'optional'"
    ]


_CATCH_ALL = """
def create_thing():
    try:
        return Thing()
    except Exception as e:
        logger.warning("Thing failed")
        return None
"""
_LISTED = {("x.py", "create_thing"): ((MUST_NOT_DEGRADE, "#1"),)}


def test_a_catch_all_factory_fails_it():
    """Positive control: the #3003 shape, unlisted, then listed without its call."""
    assert violations("x.py", _CATCH_ALL, {}) == [
        "x.py::create_thing (line 5) is not listed"
    ]
    assert violations("x.py", _CATCH_ALL, _LISTED) == [
        f"x.py::create_thing line 5 does not call {MUST_NOT_DEGRADE} as a "
        "statement of its body"
    ]


@pytest.mark.parametrize(
    "handler",
    ["except ImportError:", "except (ImportError, OSError):", "except:"],
    ids=["narrow", "tuple", "bare"],
)
def test_a_narrow_or_bare_swallowing_arm_fails_it(handler):
    source = _CATCH_ALL.replace("except Exception as e:", handler)
    assert violations("x.py", source, {}) == [
        "x.py::create_thing (line 5) is not listed"
    ]


def test_an_arm_that_returns_before_its_raise_fails_it():
    source = _CATCH_ALL.replace(
        '        logger.warning("Thing failed")\n        return None\n',
        "        if quiet:\n            return None\n        raise\n",
    )
    assert violations("x.py", source, {}) == [
        "x.py::create_thing (line 5) is not listed"
    ]


_CALL = f'{MUST_NOT_DEGRADE}("Thing", e, settings)'
_LOOPED = """
def create_things():
    for thing in things:
        try:
            Thing()
        except Exception as e:
            ARM
"""


@pytest.mark.parametrize(
    "source,listed,problem",
    [
        (
            _CATCH_ALL.replace(
                '        logger.warning("Thing failed")\n',
                f"        if quiet:\n            return None\n        {_CALL}\n",
            ),
            _LISTED,
            "line 5 leaves before it calls",
        ),
        (
            _LOOPED.replace(
                "ARM", f"if quiet:\n                continue\n            {_CALL}"
            ),
            {("x.py", "create_things"): ((MUST_NOT_DEGRADE, "#1"),)},
            "line 6 leaves before it calls",
        ),
        (
            _LOOPED.replace("ARM", f"break\n            {_CALL}"),
            {("x.py", "create_things"): ((MUST_NOT_DEGRADE, "#1"),)},
            "line 6 leaves before it calls",
        ),
        (
            _CATCH_ALL.replace(
                'logger.warning("Thing failed")', f"if False:\n            {_CALL}"
            ),
            _LISTED,
            "line 5 does not call",
        ),
        (
            _CATCH_ALL.replace(
                'logger.warning("Thing failed")',
                f"def later():\n            {_CALL}",
            ),
            _LISTED,
            "line 5 does not call",
        ),
    ],
    ids=["return", "continue", "break", "if-false", "nested-def"],
)
def test_an_arm_whose_call_may_not_run_fails_it(source, listed, problem):
    [found] = violations("x.py", source, listed)
    assert problem in found


def test_a_new_arm_fails_it():
    listed = {("x.py", "create_thing"): ((DECLARED, "§7"),)}
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
    "name", ["The signing token generator", "The OAuth JWT generator"]
)
def test_the_signing_arms_are_held_to_their_call(name):
    """The two ``SigningKeyUnavailableError`` arms: dropping either call fails."""
    path = ROOT / "faultmaven/container/providers/services.py"
    call = f'{MUST_NOT_DEGRADE}("{name}", e, settings)'
    source = path.read_text()
    assert source.count(call) == 1
    line = source[: source.index(call)].count("\n") + 1
    arm = max(
        (
            h
            for h in ast.walk(ast.parse(source))
            if isinstance(h, ast.ExceptHandler) and h.lineno <= line <= h.end_lineno
        ),
        key=lambda h: h.lineno,
    )
    assert violations(path.name, source.replace(call, "pass"), ARMS) == [
        f"services.py::register_services line {arm.lineno} does not call "
        f"{MUST_NOT_DEGRADE} as a statement of its body"
    ]


@pytest.mark.parametrize(
    "arm,listed",
    [
        ('raise RequiredServiceConstructionError("Thing") from e', {}),
        (
            "if loud:\n            raise\n        else:\n            raise Other()",
            {},
        ),
        (
            '_unbuilt_under_multi("Thing", e)\n        return None',
            {("x.py", "create_thing"): ((UNDER_MULTI, "#2342"),)},
        ),
        (
            f'{MUST_NOT_DEGRADE}("Thing", e, settings)\n        return None',
            _LISTED,
        ),
    ],
    ids=["raises", "raises-on-every-branch", "under-multi", "must-not-degrade"],
)
def test_a_refusing_arm_passes(arm, listed):
    """Negative controls: an arm that raises on every path needs no entry; a
    listed arm that makes its call first passes."""
    source = _CATCH_ALL.replace("        return None\n", "").replace(
        'logger.warning("Thing failed")', arm
    )
    assert violations("x.py", source, listed) == []
