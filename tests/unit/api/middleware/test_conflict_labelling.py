"""No 409 may go out unlabelled unless it is classified here.

Several unrelated conflicts share HTTP 409 and a client cannot tell them apart
from the body. Until #1907 the terminal-case refusal was the one 409 the turn
route raised with no ``x-error-code``, and the Slack agent read "this case is
closed" from the header being ABSENT:

    if not error_code and not polled:
        raise CaseTerminalError(...)      # faultmaven-slack-agent/faultmaven/client.py

That inference reads absence as evidence, and the conclusion it draws is a
claim about the user's investigation: a wrongly-unlabelled 409 makes the agent
tell someone with a live, open case that it is closed. It had already been
broken once — the deduplication middleware's dead
``_create_duplicate_error_response`` emitted exactly that shape.

Every terminal-case refusal now carries ``x-error-code: CASE_TERMINAL``, so a
client names the condition instead of inferring it. What keeps the inference
from coming back is pinned here, on the side that owns it: a 409 emitter —
``JSONResponse(status_code=409)`` or ``HTTPException(status_code=409)``,
anywhere under ``faultmaven/`` — either carries ``x-error-code`` or is listed in
``UNLABELLED_BY_DESIGN`` with the reason it may go out bare.
"""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPO_ROOT / "faultmaven"

#: The constructors a 409 leaves the API through. Raised exceptions that carry
#: their status as data (``TeamOperationRefused``) render through a handler
#: whose ``status_code`` is ``exc.status_code`` and answer a ``reason`` slug in
#: the body; they are outside this scan by construction.
EMITTERS = {"JSONResponse", "HTTPException"}

#: The 409s that go out without ``x-error-code`` on purpose, keyed by
#: ``(path, enclosing function)``, each with its reason. A key covers exactly
#: ONE unlabelled emitter (``test_each_classification_names_one_emitter``), so a
#: second bare 409 added to the same function is not absorbed by the first's
#: reason.
UNLABELLED_BY_DESIGN = {
    (
        "faultmaven/api/exception_handlers.py",
        "conflict_exception_handler",
    ): (
        "A domain `ConflictError` whose raiser named no `error_code`. The "
        "handler's labelled branch carries the code when one is named (a "
        "terminal case's close: CASE_TERMINAL); the rest are told apart by "
        "`conflict_reason` in the body. None is raised on the turn route, "
        "which answers an exception it does not map with a 500."
    ),
    (
        "faultmaven/modules/auth/api/auth.py",
        "local_register",
    ): "Registration's only 409 (the username is taken); not a case route.",
    (
        "faultmaven/api/routes/admin.py",
        "assign_role",
    ): "Role assignment's only 409 (the role is already held); not a case route.",
    (
        "faultmaven/modules/knowledge/api/conversion_routes.py",
        "scan_for_runbooks",
    ): "The runbook scan's only 409 (the scan aborted); not a case route.",
}

#: The terminal-case refusals, by ``(path, enclosing function)``, and how many
#: each raises. Each must be labelled with the ``CASE_TERMINAL`` constant.
TERMINAL_EMITTERS = {
    # New data, a status change, a file reclassification.
    ("faultmaven/modules/case/api/routes/conversation.py", "submit_turn"): 3,
    # `PUT /cases/{case_id}`.
    (
        "faultmaven/modules/case/api/routes/dependencies.py",
        "require_case_not_terminal",
    ): 1,
}


@dataclass(frozen=True)
class Emitter:
    path: str
    function: Optional[str]
    lineno: int
    constructor: str
    #: The ``x-error-code`` value expression, or ``None`` when unlabelled.
    label: Optional[ast.expr]

    @property
    def key(self) -> tuple:
        return (self.path, self.function)

    @property
    def where(self) -> str:
        return f"{self.path}:{self.lineno} ({self.function})"


def _is_409(status: Optional[ast.expr]) -> bool:
    if isinstance(status, ast.Constant):
        return status.value == 409
    # `status.HTTP_409_CONFLICT`, `HTTPStatus.CONFLICT`
    return isinstance(status, ast.Attribute) and (
        "409" in status.attr or status.attr == "CONFLICT"
    )


def _label(headers: Optional[ast.expr]) -> Optional[ast.expr]:
    if not isinstance(headers, ast.Dict):
        return None
    for key, value in zip(headers.keys, headers.values):
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value.lower() == "x-error-code"
        ):
            return value
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.functions: list = []
        self.found: list = []

    def _visit_function(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call):
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name in EMITTERS:
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            status = kwargs.get("status_code")
            if status is None and name == "HTTPException" and node.args:
                # `HTTPException(409, ...)`: the status is its first positional.
                status = node.args[0]
            if _is_409(status):
                self.found.append(
                    Emitter(
                        path=self.path,
                        function=self.functions[-1] if self.functions else None,
                        lineno=node.lineno,
                        constructor=name,
                        label=_label(kwargs.get("headers")),
                    )
                )
        self.generic_visit(node)


def _iter_409_emitters() -> Iterator[Emitter]:
    """Every 409 ``JSONResponse`` / ``HTTPException`` construction under faultmaven/.

    Parsed rather than executed: the point is to catch an emitter that no test
    exercises, which is exactly how the dead deduplication path survived.
    """
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        scanner = _Scanner(str(path.relative_to(REPO_ROOT)))
        scanner.visit(tree)
        yield from scanner.found


def test_the_scan_finds_the_409_emitters():
    """Guards the guard: an AST scan that matches nothing passes vacuously."""
    found = list(_iter_409_emitters())
    assert found, "the 409 scan resolved nothing — it is no longer a guard"
    for constructor in sorted(EMITTERS):
        mine = [e for e in found if e.constructor == constructor]
        assert mine, f"no 409 {constructor} found"
        assert any(e.label is not None for e in mine), f"no labelled {constructor}"
    assert any(e.label is None for e in found), "no unlabelled 409 found"


def test_every_409_is_labelled_or_classified():
    offenders = [
        e.where
        for e in _iter_409_emitters()
        if e.label is None and e.key not in UNLABELLED_BY_DESIGN
    ]

    assert not offenders, (
        "These emit HTTP 409 without an `x-error-code` header. Several "
        "unrelated conflicts share the status and a client cannot tell them "
        "apart from the body; a client that reads meaning into the header's "
        "absence turns a bare 409 into a false claim (the Slack agent still "
        "maps an unlabelled 409 on the turn POST to 'this case is closed'). "
        "Add a header (an unrecognized label degrades safely to the client's "
        "generic 4xx handling), or add the site to UNLABELLED_BY_DESIGN with a "
        f"reason: {offenders}"
    )


def test_each_classification_names_one_emitter():
    """An exemption is for one site: a stale one hides nothing, a shared one
    would quietly cover a second bare 409 added beside the first."""
    unlabelled: dict = {}
    for e in _iter_409_emitters():
        if e.label is None:
            unlabelled.setdefault(e.key, []).append(e.where)

    wrong = {
        key: unlabelled.get(key, [])
        for key in UNLABELLED_BY_DESIGN
        if len(unlabelled.get(key, [])) != 1
    }
    assert not wrong, (
        "Each UNLABELLED_BY_DESIGN entry must match exactly one unlabelled "
        f"409 emitter: {wrong}"
    )


def test_the_terminal_case_409s_are_labelled_case_terminal():
    """The terminal refusals carry the label, by the one constant."""
    by_key: dict = {}
    for e in _iter_409_emitters():
        if e.key in TERMINAL_EMITTERS:
            by_key.setdefault(e.key, []).append(e)

    for key, expected in TERMINAL_EMITTERS.items():
        emitters = by_key.get(key, [])
        terminal = [
            e
            for e in emitters
            if isinstance(e.label, ast.Name) and e.label.id == "CASE_TERMINAL"
        ]
        assert len(terminal) == expected, (
            f"{key}: expected {expected} 409(s) labelled with the "
            f"CASE_TERMINAL constant, found "
            f"{[(e.where, ast.dump(e.label) if e.label else None) for e in emitters]}"
        )


def test_a_terminal_conflict_error_is_labelled_where_it_is_raised():
    """The close route's terminal refusal travels as a ``ConflictError`` and
    reaches the client through ``conflict_exception_handler``, which labels it
    only if the raiser named the code. ``already_closed`` is the terminal
    ``conflict_reason``; every raise of it names ``CASE_TERMINAL``."""
    raises = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "ConflictError"
            ):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            reason = kwargs.get("conflict_reason")
            if isinstance(reason, ast.Constant) and reason.value == "already_closed":
                code = kwargs.get("error_code")
                raises.append(
                    (
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}",
                        isinstance(code, ast.Name) and code.id == "CASE_TERMINAL",
                    )
                )

    assert raises, "no terminal ConflictError found — the scan is no longer a guard"
    assert all(labelled for _, labelled in raises), raises


def test_case_terminal_is_one_constant():
    """One name for the code: every use reads ``faultmaven.exceptions``'s
    constant, so a second spelling cannot drift from it."""
    literals = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "CASE_TERMINAL":
                literals.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert len(literals) == 1, literals
    assert literals[0].startswith("faultmaven/exceptions.py:"), literals


def test_deduplication_no_longer_has_an_unlabelled_409_path():
    """The path that had already broken the premise, kept from coming back.

    `DuplicateRequestError` is constructed but never raised, so its handler was
    dead — but it emitted a 409 with no header, and dedup runs on the turn POST.
    """
    path = SOURCE_ROOT / "api/middleware/deduplication.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Checked on the AST, not as substrings: the removal is explained in a
    # comment in that file, and a substring scan would match the explanation.
    defs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_create_duplicate_error_response" not in defs

    handled = {
        handler.type.id
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler) and isinstance(handler.type, ast.Name)
    }
    assert "DuplicateRequestError" not in handled

    # The live path keeps both signals.
    source = path.read_text(encoding="utf-8")
    assert '"x-error-code": error_response.error_code' in source
    assert '"Retry-After": str(ttl_remaining)' in source
