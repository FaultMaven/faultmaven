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
from coming back is pinned here, on the side that owns it:

* a 409 emitter — any ``HTTPException`` or ``*Response`` constructed with
  status 409, by keyword or by position, anywhere under ``faultmaven/`` —
  either carries ``x-error-code`` or is listed in ``UNLABELLED_BY_DESIGN``
  with the reason it may go out bare;
* an emitter whose status the scan cannot read (a variable, an expression) is
  listed in ``DYNAMIC_STATUS`` with the reason it never answers an unlabelled
  409, so a 409 cannot hide behind a spelling the scan does not resolve;
* inside an ``if … is_terminal …:`` branch, every 409 emitter and every
  ``ConflictError`` names the ``CASE_TERMINAL`` constant — not a look-alike
  label, and not an expression that only adds up to it;
* a ``ConflictError`` about a case carries an ``error_code``, or its
  ``conflict_reason`` is listed in ``CASE_CONFLICTS_WITHOUT_CODE``: the handler
  sends a code-less conflict bare, so a terminal refusal raised under a new
  reason would otherwise go out exactly as the old inference expects.
"""

import ast
import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPO_ROOT / "faultmaven"

#: The names a status-bearing literal can be read from.
STATUS_NAMESPACES = {"status", "HTTPStatus"}

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

#: Emitters whose status is not a literal the scan can read, keyed by
#: ``(path, enclosing function)``, each with why it never answers a bare 409.
#: Every entry must still match a site (``test_every_dynamic_status_is_classified``).
DYNAMIC_STATUS = {
    (
        "faultmaven/api/exception_handlers.py",
        "_llm_http",
    ): (
        "The LLM-failure statuses of `llm_service_error_http_exception`'s "
        "table (402/429/5xx); never 409, and always labelled."
    ),
    (
        "faultmaven/api/exception_handlers.py",
        "team_operation_refused_handler",
    ): (
        "`TeamOperationRefused`'s own status (403/409/410), with a `reason` "
        "slug in the body; the team routes are not case routes."
    ),
    (
        "faultmaven/api/exception_handlers.py",
        "http_exception_handler",
    ): (
        "Re-renders an `HTTPException` raised elsewhere, forwarding its "
        "headers; that raise is scanned at its own site."
    ),
    (
        "faultmaven/api/exception_handlers.py",
        "oauth_protocol_error_handler",
    ): "The RFC 6749 error status of the OAuth token/revoke routes (400/401).",
    (
        "faultmaven/api/middleware/idempotency.py",
        "_create_response_from_cache",
    ): (
        "Replays a cached response with its recorded status and headers; the "
        "original was scanned where it was built."
    ),
    (
        "faultmaven/modules/knowledge/api/conversion_routes.py",
        "convert_document",
    ): "The conversion error map's statuses (413/415/422/503); never 409.",
}

#: The ``conflict_reason``s a ``ConflictError`` about a case may carry with no
#: ``error_code``, each with why it is not terminal.
CASE_CONFLICTS_WITHOUT_CODE = {
    "concurrent_update": (
        "`close_case` lost the version race on every retry: the case changed "
        "while closing, and a reload decides. Not a terminal refusal."
    ),
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
    #: ``"409"``, ``"other"`` (a readable non-409 status) or ``"dynamic"``.
    status: str
    #: The ``x-error-code`` value expression, or ``None`` when unlabelled.
    label: Optional[ast.expr]

    @property
    def key(self) -> tuple:
        return (self.path, self.function)

    @property
    def where(self) -> str:
        return f"{self.path}:{self.lineno} ({self.function})"


def _emitter_name(call: ast.Call) -> Optional[str]:
    """``HTTPException`` or any ``*Response``: the shapes a status leaves in."""
    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
    if name and (name == "HTTPException" or name.endswith("Response")):
        return name
    return None


def _status_expr(call: ast.Call, name: str) -> Optional[ast.expr]:
    for kw in call.keywords:
        if kw.arg == "status_code":
            return kw.value
    # `HTTPException(409, ...)`; `JSONResponse(content, 409)`.
    index = 0 if name == "HTTPException" else 1
    return call.args[index] if len(call.args) > index else None


def _status_kind(status: ast.expr) -> str:
    if isinstance(status, ast.Constant):
        return "409" if status.value == 409 else "other"
    # `status.HTTP_409_CONFLICT`, `fastapi.status.HTTP_409_CONFLICT`,
    # `HTTPStatus.CONFLICT` — read only off a status namespace, so an
    # attribute of anything else (`exc.status_code`) is not mistaken for one.
    if (
        isinstance(status, ast.Attribute)
        and ast.unparse(status.value).split(".")[-1] in STATUS_NAMESPACES
    ):
        conflict = "409" in status.attr or status.attr == "CONFLICT"
        return "409" if conflict else "other"
    return "dynamic"


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


def _names_case_terminal(expr: Optional[ast.expr]) -> bool:
    """The constant itself — ``CASE_TERMINAL`` or ``exceptions.CASE_TERMINAL``."""
    return (isinstance(expr, ast.Name) and expr.id == "CASE_TERMINAL") or (
        isinstance(expr, ast.Attribute) and expr.attr == "CASE_TERMINAL"
    )


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
        name = _emitter_name(node)
        if name:
            status = _status_expr(node, name)
            if status is not None:
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                self.found.append(
                    Emitter(
                        path=self.path,
                        function=self.functions[-1] if self.functions else None,
                        lineno=node.lineno,
                        constructor=name,
                        status=_status_kind(status),
                        label=_label(kwargs.get("headers")),
                    )
                )
        self.generic_visit(node)


@functools.lru_cache(maxsize=1)
def _parsed() -> Tuple[Tuple[str, ast.AST], ...]:
    """Every module under faultmaven/, parsed once for all the scans here."""
    trees = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        trees.append((str(path.relative_to(REPO_ROOT)), tree))
    return tuple(trees)


def _trees() -> Iterator[Tuple[str, ast.AST]]:
    return iter(_parsed())


def _iter_emitters() -> Iterator[Emitter]:
    """Every status-bearing ``HTTPException`` / ``*Response`` under faultmaven/.

    Parsed rather than executed: the point is to catch an emitter that no test
    exercises, which is exactly how the dead deduplication path survived.
    """
    for path, tree in _trees():
        scanner = _Scanner(path)
        scanner.visit(tree)
        yield from scanner.found


def _iter_409_emitters() -> Iterator[Emitter]:
    return (e for e in _iter_emitters() if e.status == "409")


def _conflict_error_kwargs(call: ast.Call) -> dict:
    """``ConflictError``'s arguments by name, positional ones included."""
    names = ["message", "resource_type", "resource_id", "conflict_reason", "error_code"]
    kwargs = dict(zip(names, call.args))
    kwargs.update({kw.arg: kw.value for kw in call.keywords if kw.arg})
    return kwargs


def _is_conflict_error(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        getattr(node.func, "id", None) == "ConflictError"
        or getattr(node.func, "attr", None) == "ConflictError"
    )


def _terminal_branches(tree: ast.AST) -> Iterator[List[ast.stmt]]:
    """The statements that run when an ``if … is_terminal …:`` holds.

    ``if not x.is_terminal:`` runs its ``else`` on a terminal case, so that is
    the branch read for a negated test.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and "is_terminal" in ast.unparse(node.test)):
            continue
        negated = isinstance(node.test, ast.UnaryOp) and isinstance(
            node.test.op, ast.Not
        )
        yield node.orelse if negated else node.body


def _terminal_branch_findings() -> Tuple[list, list]:
    """``(checked, violations)`` for every 409 emitter and ``ConflictError``
    lexically inside a terminal branch."""
    checked, violations = [], []
    for path, tree in _trees():
        for branch in _terminal_branches(tree):
            for node in (n for stmt in branch for n in ast.walk(stmt)):
                if not isinstance(node, ast.Call):
                    continue
                where = f"{path}:{node.lineno}"
                name = _emitter_name(node)
                if name:
                    status = _status_expr(node, name)
                    if status is None or _status_kind(status) == "other":
                        continue
                    kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                    label = _label(kwargs.get("headers"))
                    checked.append(where)
                    if not _names_case_terminal(label):
                        violations.append(
                            (where, name, ast.unparse(label) if label else None)
                        )
                elif _is_conflict_error(node):
                    code = _conflict_error_kwargs(node).get("error_code")
                    checked.append(where)
                    if not _names_case_terminal(code):
                        violations.append(
                            (
                                where,
                                "ConflictError",
                                ast.unparse(code) if code else None,
                            )
                        )
    return checked, violations


def test_the_scan_finds_the_409_emitters():
    """Guards the guard: an AST scan that matches nothing passes vacuously."""
    found = list(_iter_409_emitters())
    assert found, "the 409 scan resolved nothing — it is no longer a guard"
    for constructor in ("HTTPException", "JSONResponse"):
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


def test_every_dynamic_status_is_classified():
    """A status the scan cannot read is a 409 it cannot see. Each one is
    classified with why it never answers a bare 409, and every
    classification still names a site."""
    dynamic: dict = {}
    for e in _iter_emitters():
        if e.status == "dynamic":
            dynamic.setdefault(e.key, []).append(e.where)

    unclassified = {k: v for k, v in dynamic.items() if k not in DYNAMIC_STATUS}
    assert not unclassified, (
        "These construct a response whose status the 409 scan cannot read. "
        "Use a literal or a `status.*` constant, or add the site to "
        f"DYNAMIC_STATUS with a reason: {unclassified}"
    )
    stale = [key for key in DYNAMIC_STATUS if key not in dynamic]
    assert not stale, f"DYNAMIC_STATUS entries that match no site: {stale}"


def test_the_terminal_case_409s_are_labelled_case_terminal():
    """The terminal refusals carry the label, by the one constant."""
    by_key: dict = {}
    for e in _iter_409_emitters():
        if e.key in TERMINAL_EMITTERS:
            by_key.setdefault(e.key, []).append(e)

    for key, expected in TERMINAL_EMITTERS.items():
        emitters = by_key.get(key, [])
        terminal = [e for e in emitters if _names_case_terminal(e.label)]
        assert len(terminal) == expected, (
            f"{key}: expected {expected} 409(s) labelled with the "
            f"CASE_TERMINAL constant, found "
            f"{[(e.where, ast.unparse(e.label) if e.label else None) for e in emitters]}"
        )


def test_every_refusal_in_a_terminal_branch_names_case_terminal():
    """Inside ``if … is_terminal …:``, a 409 or a ``ConflictError`` is a
    terminal refusal, and it names the ``CASE_TERMINAL`` constant: a
    look-alike label (``CASE_CLOSED``) or an expression that adds up to the
    string would pass a count of the known sites and still miss the client."""
    checked, violations = _terminal_branch_findings()

    # The turn route's three, the PUT guard, and the container stand-in's
    # close: a scan that checks fewer has stopped seeing the branches.
    assert len(checked) >= 5, checked
    assert not violations, (
        "These refuse a terminal case without the CASE_TERMINAL constant as "
        f"their x-error-code / error_code: {violations}"
    )


def test_a_case_conflict_error_names_a_code_or_a_known_reason():
    """``conflict_exception_handler`` sends a code-less ``ConflictError``
    bare. For a conflict about a case that is the shape the terminal refusal
    used to have, so each one names an ``error_code`` or carries a
    ``conflict_reason`` classified as not terminal."""
    raises, offenders = [], []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if not _is_conflict_error(node):
                continue
            kwargs = _conflict_error_kwargs(node)
            resource = kwargs.get("resource_type")
            if not (
                isinstance(resource, ast.Constant)
                and isinstance(resource.value, str)
                and resource.value.lower() == "case"
            ):
                continue
            where = f"{path}:{node.lineno}"
            raises.append(where)
            reason = kwargs.get("conflict_reason")
            known = (
                isinstance(reason, ast.Constant)
                and reason.value in CASE_CONFLICTS_WITHOUT_CODE
            )
            if "error_code" not in kwargs and not known:
                offenders.append(
                    (where, ast.unparse(reason) if reason is not None else None)
                )

    assert raises, "no ConflictError about a case found — the scan is no longer a guard"
    assert not offenders, (
        "These raise a ConflictError about a case with no error_code and a "
        "conflict_reason not in CASE_CONFLICTS_WITHOUT_CODE. Name the code "
        "(CASE_TERMINAL for a terminal refusal) or classify the reason: "
        f"{offenders}"
    )


def test_a_terminal_conflict_error_is_labelled_where_it_is_raised():
    """The close route's terminal refusal travels as a ``ConflictError`` and
    reaches the client through ``conflict_exception_handler``, which labels it
    only if the raiser named the code. ``already_closed`` is the terminal
    ``conflict_reason``; every raise of it names ``CASE_TERMINAL``."""
    raises = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if not _is_conflict_error(node):
                continue
            kwargs = _conflict_error_kwargs(node)
            reason = kwargs.get("conflict_reason")
            if isinstance(reason, ast.Constant) and reason.value == "already_closed":
                raises.append(
                    (
                        f"{path}:{node.lineno}",
                        _names_case_terminal(kwargs.get("error_code")),
                    )
                )

    assert raises, "no terminal ConflictError found — the scan is no longer a guard"
    assert all(labelled for _, labelled in raises), raises


def test_case_terminal_is_one_constant():
    """One name for the code: every use reads ``faultmaven.exceptions``'s
    constant, so a second spelling cannot drift from it."""
    literals = [
        f"{path}:{node.lineno}"
        for path, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "CASE_TERMINAL"
    ]

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
