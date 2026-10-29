"""No tool result carries a caught exception's text (#2332).

A tool's result goes into the model's context, and the model can repeat it into
the stored answer every reader of the case is served. The class of leak has come
back after each fix: HTTP 500s (#866), ``debug_info`` (#2072), ``web_search``,
then the agent tools' catch-alls, ten of them in the tools that exist today. So
this guards the class with the shared analysis the HTTP guards use
(``tests/error_text_ast``): a ``ToolResult`` whose ``error`` carries the text,
anywhere in the package, and anything a function under ``modules/agent/tools/``
returns carrying it.

``test_reverting_any_fixed_site_is_reported`` puts the text back at each fixed
site in turn and asserts the analysis reports it, so the guard is shown to bite
on the sites it exists for.
"""

import ast
import pathlib

import pytest

import faultmaven
from tests.error_text_ast import tool_result_leak_sites
from tests.source_tree import parse_file

_PKG = pathlib.Path(faultmaven.__file__).parent
_REPO = _PKG.parent
_TOOLS = _PKG / "modules" / "agent" / "tools"

_TYPED_ARM = (
    "Typed arm whose every raiser on the in-turn path writes its message for "
    "the caller (investigation_service/reclassification.py); a storage or "
    "driver failure arrives as ServiceError, which no typed arm catches."
)
_MODEL_ARGUMENT = (
    "The text describes the model's own argument, not our system: a parse "
    "error in the value the model passed."
)

_RECLASSIFY = "faultmaven/modules/agent/tools/reclassify_evidence_tool.py"
_SEARCH_FILE = "faultmaven/modules/agent/tools/search_file_tool.py"

#: Reported sites that are not leaks, keyed on (path, function, the handler's
#: exception type, what the site shows the model), each also justified by a
#: comment at the site. The last part is in the key so that new text under an
#: allowed arm is a new site, not a covered one.
_ALLOWED: dict[tuple[str, str, str, str], str] = {
    (
        _SEARCH_FILE,
        "_count_search",
        "re.error",
        "[{'line': 0, 'content': f'Invalid regex: {e}'}]",
    ): _MODEL_ARGUMENT,
    (
        _SEARCH_FILE,
        "_regex_search",
        "re.error",
        "[{'error': f'Invalid regex pattern: {e}'}]",
    ): _MODEL_ARGUMENT,
    **{
        (_RECLASSIFY, "execute_with_context", arm, shown): _TYPED_ARM
        for arm, shown in (
            ("AuthorizationError", "data=None, error=str(e)"),
            ("ConflictError", "data=None, error=str(e)"),
            (
                "ValidationException",
                "data=None, error=f'{e}. Inform the user and do not retry.'",
            ),
            ("ServiceException", "data=None, error=str(e)"),
        )
    },
}


def _constructs_a_tool_result(path: pathlib.Path) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
        == "ToolResult"
        for node in ast.walk(parse_file(path))
    )


def _scanned() -> list[tuple[pathlib.Path, bool]]:
    """Every tool module, returns included; every other module that builds a
    ``ToolResult``, for its ``error`` only."""
    tools = sorted(_TOOLS.rglob("*.py"))
    others = sorted(
        p
        for p in _PKG.rglob("*.py")
        if not p.is_relative_to(_TOOLS) and _constructs_a_tool_result(p)
    )
    return [(p, True) for p in tools] + [(p, False) for p in others]


def _shown(tree: ast.AST, line: int) -> str:
    """What the site reported at ``line`` shows the model, rendered: a
    ``ToolResult``'s ``data`` and ``error``, or a returned value."""
    nodes = [n for n in ast.walk(tree) if getattr(n, "lineno", None) == line]
    for node in nodes:
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ToolResult":
            fields = [
                f"{kw.arg}={ast.unparse(kw.value)}"
                for kw in node.keywords
                if kw.arg in {"data", "error"}
            ]
            fields += [
                f"{name}={ast.unparse(arg)}"
                for name, arg in zip(("data", "error"), node.args[1:3])
            ]
            return ", ".join(fields)
    returned = [n.value for n in nodes if isinstance(n, ast.Return) and n.value]
    return ast.unparse(returned[0]) if returned else "<unmapped>"


def _keys(
    path: pathlib.Path, returns: bool, rel: str | None = None
) -> list[tuple[str, str, str, str, int]]:
    """(path, function, handler type, what it shows, line) per reported site.

    ``rel`` names a copy of a module by the module's own path.
    """
    tree = parse_file(path)
    rel = rel or path.relative_to(_REPO).as_posix()
    keys = []
    for site in tool_result_leak_sites(path, returns=returns):
        line = int(site.rsplit(":", 1)[1])
        fn = min(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno <= line <= n.end_lineno
            ),
            key=lambda n: n.end_lineno - n.lineno,
        )
        handlers = [
            h
            for h in ast.walk(fn)
            if isinstance(h, ast.ExceptHandler) and h.lineno <= line <= h.end_lineno
        ]
        arm = ast.unparse(handlers[-1].type) if handlers else "<after a handler>"
        keys.append((rel, fn.name, arm, _shown(tree, line), line))
    return keys


@pytest.mark.unit
def test_the_scan_reaches_every_tool():
    """Not vacuous: tools that hold the ten fixed sites are scanned."""
    scanned = {p.name for p, returns in _scanned() if returns}
    assert {
        "base.py",
        "kb_tool_adapter.py",
        "search_file_tool.py",
        "vectorize_file_tool.py",
        "web_search.py",
    } <= scanned


@pytest.mark.unit
def test_no_tool_result_carries_a_caught_exceptions_text():
    offenders = [
        f"{rel}:{line} in {fn}() under except {arm}: {shown}"
        for path, returns in _scanned()
        for rel, fn, arm, shown, line in _keys(path, returns)
        if (rel, fn, arm, shown) not in _ALLOWED
    ]
    assert offenders == [], (
        "tool results carrying a caught exception's text, which the model can "
        "repeat to every reader of the case. Log the text and answer with "
        "tool_failure(reason, e, guidance=...); if the site is safe, justify it "
        "at the site and in _ALLOWED:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.unit
def test_every_allowlist_entry_still_matches_a_real_site():
    live = {key[:4] for path, returns in _scanned() for key in _keys(path, returns)}
    assert sorted(set(_ALLOWED) - live) == []


@pytest.mark.unit
def test_new_text_under_an_allowed_arm_is_reported(tmp_path):
    """The allowlist covers what an arm shows, not the arm: the
    ``ServiceException`` arm handing over its cause's text is a new site."""
    path = _REPO / _RECLASSIFY
    arm = (
        "        except ServiceException as e:\n"
        "            return ToolResult(success=False, data=None, error=str(e))\n"
    )
    source = path.read_text(encoding="utf-8")
    assert arm in source
    mutated = tmp_path / path.name
    mutated.write_text(
        source.replace(arm, arm.replace("str(e)", "str(e.__cause__)")),
        encoding="utf-8",
    )

    new = [
        key[:4]
        for key in _keys(mutated, True, rel=_RECLASSIFY)
        if key[:4] not in _ALLOWED
    ]
    assert new == [
        (
            _RECLASSIFY,
            "execute_with_context",
            "ServiceException",
            "data=None, error=str(e.__cause__)",
        )
    ]


def _text(exc: ast.expr) -> ast.Call:
    """``str(<exc>)``: the exception's text."""
    return ast.Call(ast.Name("str", ast.Load()), [exc], [])


class _Revert(ast.NodeTransformer):
    """Put the exception's text back at the ``target``-th fixed site.

    A fixed site is a ``tool_failure(reason, e)`` (reverted to
    ``ToolResult(error=str(e))``), an ``exception_class_name(e)`` (to
    ``str(e)``), or a handler's ``ToolResult`` with a fixed ``error`` (to
    ``str(<the handler's name>)``). ``target=-1`` only counts them.
    """

    def __init__(self, target: int):
        self.target, self.count, self.handler, self.logging = target, 0, None, 0

    def _hit(self) -> bool:
        self.count += 1
        return self.count - 1 == self.target

    def visit_ExceptHandler(self, node):
        outer, self.handler = self.handler, node.name
        self.generic_visit(node)
        self.handler = outer
        return node

    def visit_Call(self, node):
        # A log line is not a site: the text belongs there.
        logs = getattr(getattr(node.func, "value", None), "id", None) == "logger"
        self.logging += logs
        self.generic_visit(node)
        self.logging -= logs
        if self.logging:
            return node
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "exception_class_name" and self._hit():
            return _text(node.args[0])
        if name == "tool_failure" and self._hit():
            return ast.Call(
                ast.Name("ToolResult", ast.Load()),
                [ast.Constant(False), ast.Constant(None), _text(node.args[1])],
                [],
            )
        error = next((kw for kw in node.keywords if kw.arg == "error"), None)
        if (
            name == "ToolResult"
            and self.handler
            and error is not None
            and not any(
                getattr(n, "id", None) == self.handler for n in ast.walk(error.value)
            )
            and self._hit()
        ):
            error.value = _text(ast.Name(self.handler, ast.Load()))
        return node


@pytest.mark.unit
def test_reverting_any_fixed_site_is_reported(tmp_path):
    reverted = 0
    for path in sorted(_TOOLS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        counter = _Revert(-1)
        counter.visit(ast.parse(source))
        baseline = tmp_path / f"{path.stem}.py"
        baseline.write_text(ast.unparse(ast.parse(source)), encoding="utf-8")
        before = len(tool_result_leak_sites(baseline, returns=True))
        for target in range(counter.count):
            tree = ast.fix_missing_locations(_Revert(target).visit(ast.parse(source)))
            mutated = tmp_path / f"{path.stem}_{target}.py"
            mutated.write_text(ast.unparse(tree), encoding="utf-8")
            after = len(tool_result_leak_sites(mutated, returns=True))
            assert after > before, f"{path.name}: reverting site {target} passed"
            reverted += 1

    # The 10 sites, tool_failure's own rendering, and the fixed-error handlers
    # (reclassify's NotFoundError arm, web_search's catch-all).
    assert reverted == 13
