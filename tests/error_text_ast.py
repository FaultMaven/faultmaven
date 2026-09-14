"""Shared AST analysis for the "raw exception text on the wire" guards.

Both the auth and case modules pin the same class: an exception caught by a
broad ``except`` must not have its text handed to the caller. The guards live
in their own modules, but the *analysis* lives here so that hardening it hardens
every guard at once — the alternative, a copy per module, is how one guard ends
up strictly weaker than another.

Two things this deliberately does **not** do the obvious way:

* It matches ``ast.Name`` nodes rather than substrings of ``ast.unparse``
  output. A substring check for ``str(e)``/``{e}`` sees ``f"...: {str(e)}"`` but
  misses ``repr(e)``, ``f"{e!s}"``, ``"%s" % e`` and ``e.args[0]`` — all of
  which leak exactly as much.
* It follows local aliases. ``modules/case/api/routes.py`` builds its detail one
  statement earlier::

      error_response = ErrorResponse(error=ErrorDetail(message=f"...{e}"))
      raise HTTPException(500, detail=error_response.model_dump())

  so the leaking text never appears in the ``detail`` expression at all. A guard
  that only inspects ``detail`` reports the file clean while the exception is on
  the wire — which is precisely how one such site survived #966.

Scope is 5xx only, by design and consistently with #866/#966: ``detail=str(e)``
on a ``ValidationException`` or ``InvalidGrantError`` arm is a domain message
written *for* the caller, not internal text escaping a broad except.
"""

from __future__ import annotations

import ast
import pathlib

# HTTPException(status_code, detail=None, headers=None) — `detail` is
# positional index 1 when not passed by keyword.
_DETAIL_POSITION = 1
_STATUS_POSITION = 0

_MAX_ALIAS_DEPTH = 5


def _mentions(node: ast.AST, name: str) -> bool:
    """Can the *value* of ``node`` carry text from the exception bound to ``name``?

    Not a plain ``ast.walk`` for ``Name``: reading the exception is not the same
    as putting it on the wire, and two positions read it without carrying it.

    * ``ast.Compare`` evaluates to a bool. ``getattr(e, "error_code", None) in
      (...)`` inspects the exception; no text can travel through a boolean.
    * ``ast.IfExp`` carries only its branches. ``"A" if e.code == X else "B"``
      selects between two literals — the exception steers the choice without
      appearing in the result.

    Both shapes are live: ``modules/knowledge/api/routes.py`` picks one of two
    static sentences from ``e.error_code``, and a walk-for-``Name`` guard reports
    it as a leak. A guard that cries wolf on a non-leak gets weakened by the
    next person to hit it, so the precision is part of the guard working.

    Everything else over-approximates deliberately: an unrecognised call that
    receives the exception is assumed to carry it.
    """
    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, ast.Compare):
        return False
    if isinstance(node, ast.IfExp):
        return _mentions(node.body, name) or _mentions(node.orelse, name)
    return any(_mentions(child, name) for child in ast.iter_child_nodes(node))


def _root_name(target: ast.AST) -> str | None:
    """The name a binding ultimately writes through.

    ``d["k"]``, ``d.attr`` and ``d["k"]["j"]`` all write into ``d``. Treating
    them as bindings of ``d`` is an over-approximation -- the exception reaches
    one key, not the whole object -- but the object is what gets returned, so
    the whole object is what carries the text onto the wire.
    """
    while isinstance(target, (ast.Subscript, ast.Attribute)):
        target = target.value
    return target.id if isinstance(target, ast.Name) else None


def _local_assignments(handler: ast.ExceptHandler) -> dict[str, list[ast.AST]]:
    """Every binding made inside this except handler, keyed by the name written.

    Covers ``x = ...``, ``x: T = ...``, ``x += ...`` and ``(x := ...)``. The
    last two bind just as effectively as the first, so leaving them out would
    give the alias-following two silent blind spots.

    It also covers writes *through* a name -- ``d["k"] = ...``, ``d.attr = ...``
    -- keyed on the root name via ``_root_name``. Restricting this to
    ``ast.Name`` targets was a real blind spot, not a theoretical one::

        except Exception as e:
            cleanup_results["resource_cleanup"] = {"error": str(e)}
        ...
        return cleanup_results

    ``main.py`` had that exact shape, and this analysis reported the file clean
    while CodeQL's ``py/stack-trace-exposure`` flagged it correctly.
    """
    assigns: dict[str, list[ast.AST]] = {}

    def bind(target: ast.AST, value: ast.AST | None) -> None:
        if value is None:
            return
        name = target.id if isinstance(target, ast.Name) else _root_name(target)
        if name:
            assigns.setdefault(name, []).append(value)

    for node in ast.walk(handler):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            bind(node.target, node.value)
        elif isinstance(node, ast.NamedExpr):
            bind(node.target, node.value)
    return assigns


def _carries_exception(
    expr: ast.AST,
    exc_name: str,
    assigns: dict[str, list[ast.AST]],
    _depth: int = 0,
) -> bool:
    """Does ``expr`` carry the caught exception, directly or via a local alias?"""
    if _mentions(expr, exc_name):
        return True
    if _depth >= _MAX_ALIAS_DEPTH:
        return False
    for child in ast.walk(expr):
        if not isinstance(child, ast.Name) or child.id == exc_name:
            continue
        for value in assigns.get(child.id, ()):
            if _carries_exception(value, exc_name, assigns, _depth + 1):
                return True
    return False


def _is_server_error(call: ast.Call) -> bool:
    """Is this ``HTTPException`` a 5xx?

    A bare integer literal is checked by *range*, not by spelling. Matching only
    ``500``/``INTERNAL_SERVER_ERROR``/``HTTP_5`` let ``raise HTTPException(
    status_code=503, detail=f"...: {e}")`` through — a live leak sitting inside
    a file the guard reported clean, which is worse than no guard at all.

    The 4xx side must keep falling through: ``detail=str(e)`` on a
    ``ValidationException`` arm is a domain message written for the caller, and
    a range test is what keeps that exclusion principled rather than accidental.
    """
    candidates: list[ast.AST] = [
        kw.value for kw in call.keywords if kw.arg == "status_code"
    ]
    if not candidates and len(call.args) > _STATUS_POSITION:
        candidates.append(call.args[_STATUS_POSITION])
    for node in candidates:
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if 500 <= node.value <= 599:
                return True
            continue
        text = ast.unparse(node)
        if "500" in text or "INTERNAL_SERVER_ERROR" in text or "HTTP_5" in text:
            return True
    return False


def _detail_expr(call: ast.Call) -> ast.AST | None:
    """The ``detail`` argument, whether passed by keyword or positionally."""
    for kw in call.keywords:
        if kw.arg == "detail":
            return kw.value
    if len(call.args) > _DETAIL_POSITION:
        return call.args[_DETAIL_POSITION]
    return None


def _is_http_exception(call: ast.Call) -> bool:
    func = call.func
    return (getattr(func, "id", None) or getattr(func, "attr", None)) == "HTTPException"


def _except_handlers(path: pathlib.Path):
    """Every ``except ... as <name>`` handler in the file, with its bound name."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name:
            yield node


def http_exception_leak_sites(path: pathlib.Path) -> list[str]:
    """``file:line`` for every 5xx ``HTTPException`` carrying the caught exception."""
    offenders: list[str] = []
    for handler in _except_handlers(path):
        assigns = _local_assignments(handler)
        for node in ast.walk(handler):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            if not _is_http_exception(node.exc) or not _is_server_error(node.exc):
                continue
            detail = _detail_expr(node.exc)
            if detail is None:
                continue
            if _carries_exception(detail, handler.name, assigns):
                offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def _own_nodes(fn: ast.AST):
    """Every node belonging to ``fn`` itself, not to a function nested inside it.

    A nested ``def``/``lambda`` is its own scope: a name tainted in there cannot
    be the name the outer function returns. Attributing both to the outer
    function would make the guard cry wolf, and a guard that cries wolf gets
    weakened by the next person to hit it.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _tainted_names(handlers: list[ast.ExceptHandler]) -> set[str]:
    """Names bound inside these handlers to something carrying the exception."""
    tainted: set[str] = set()
    for handler in handlers:
        assigns = _local_assignments(handler)
        for name, values in assigns.items():
            if any(_carries_exception(v, handler.name, assigns) for v in values):
                tainted.add(name)
    return tainted


def returned_body_leak_sites(path: pathlib.Path) -> list[str]:
    """``file:line`` for every ``return`` carrying a caught exception's text.

    The ``HTTPException`` guard structurally cannot see these: a handler that
    degrades to a 200 body — ``GET /auth/health`` did — leaks just as much.

    Two shapes, and only the first is inside the handler:

    * the return is *in* the handler and carries ``e`` (directly or by alias);
    * the handler stashes the text in a local and the return happens **after**
      it, back in the enclosing function::

          try:
              sla_details = sla_tracker.get_component_sla_details(name)
          except Exception as e:
              sla_details = {"error": str(e)}
          return {"sla": sla_details}

      Scoping the walk to the handler missed every site of this shape.
      ``main.py`` had six, all of which CodeQL's ``py/stack-trace-exposure``
      flagged while this analysis reported the file clean.

    A ``raise`` outside the handler carrying a tainted local is the same class
    and is deliberately **not** covered: no such site exists in the tree today,
    and widening a guard past its evidence is how false positives arrive.
    """
    offenders: list[str] = []
    tree = ast.parse(path.read_text())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = list(_own_nodes(fn))
        handlers = [n for n in own if isinstance(n, ast.ExceptHandler) and n.name]
        if not handlers:
            continue
        tainted = _tainted_names(handlers)
        in_handler = {id(n) for h in handlers for n in ast.walk(h)}
        for node in own:
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            leaks = any(
                n.id in tainted for n in ast.walk(node.value) if isinstance(n, ast.Name)
            )
            if not leaks and id(node) in in_handler:
                for handler in handlers:
                    if id(node) in {id(n) for n in ast.walk(handler)}:
                        leaks = _carries_exception(
                            node.value, handler.name, _local_assignments(handler)
                        )
                        if leaks:
                            break
            if leaks:
                offenders.append(f"{path.name}:{node.lineno}")
    return offenders
