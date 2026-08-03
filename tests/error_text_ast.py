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
    """Does ``node`` read the variable ``name`` anywhere inside it?"""
    return any(
        isinstance(child, ast.Name) and child.id == name for child in ast.walk(node)
    )


def _local_assignments(handler: ast.ExceptHandler) -> dict[str, list[ast.AST]]:
    """Every ``x = <expr>`` bound to a plain name inside this except handler."""
    assigns: dict[str, list[ast.AST]] = {}
    for node in ast.walk(handler):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigns.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assigns.setdefault(node.target.id, []).append(node.value)
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
    """Is this ``HTTPException`` a 5xx?"""
    candidates: list[ast.AST] = [
        kw.value for kw in call.keywords if kw.arg == "status_code"
    ]
    if not candidates and len(call.args) > _STATUS_POSITION:
        candidates.append(call.args[_STATUS_POSITION])
    for node in candidates:
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


def returned_body_leak_sites(path: pathlib.Path) -> list[str]:
    """``file:line`` for every ``return`` in an except handler carrying the exception.

    The ``HTTPException`` guard structurally cannot see these: a handler that
    degrades to a 200 body — ``GET /auth/health`` did — leaks just as much.
    """
    offenders: list[str] = []
    for handler in _except_handlers(path):
        assigns = _local_assignments(handler)
        for node in ast.walk(handler):
            if isinstance(node, ast.Return) and node.value is not None:
                if _carries_exception(node.value, handler.name, assigns):
                    offenders.append(f"{path.name}:{node.lineno}")
    return offenders
