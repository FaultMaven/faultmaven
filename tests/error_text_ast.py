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
import re

# HTTPException(status_code, detail=None, headers=None) — `detail` is
# positional index 1 when not passed by keyword.
_DETAIL_POSITION = 1
_STATUS_POSITION = 0

_MAX_ALIAS_DEPTH = 5

#: A status expression that RECOGNISABLY names a non-5xx class. Used only to
#: decide whether an unreadable ``status_code`` should fail open or closed —
#: see ``_is_server_error``.
_NON_5XX_STATUS_RE = re.compile(r"HTTP_[1234]\d\d|\b[1234]\d\d\b")

#: Rendering the live exception without binding it. A handler using these has
#: no ``as <name>``, so the name-following analysis cannot see it at all —
#: yet ``traceback.format_exc()`` in a ``detail`` is ``py/stack-trace-exposure``
#: in its purest form.
_UNBOUND_EXCEPTION_RENDERERS = frozenset(
    {"format_exc", "format_exception", "format_exception_only", "exc_info"}
)


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


def _written_name(target: ast.AST) -> str | None:
    """The name a binding writes through, as the RETURN side will spell it.

    Subscripts collapse to the container: ``d["k"]`` and ``d["k"]["j"]`` both
    write into ``d``, the object that later gets returned. That
    over-approximation is the point -- the exception reaches one key, but the
    whole object goes onto the wire.

    Attributes do NOT collapse. ``self.last_error = str(e)`` writes
    ``self.last_error``, not ``self``, and collapsing it to ``self`` made every
    ``return`` in the method that merely mentions ``self`` a reported leak --
    ``return {"status": self.status}`` among them. That is not a hypothetical:
    ``self.<field> = str(e)`` is an ordinary pattern, and
    ``infrastructure/health/component_monitor.py`` alone reported twelve
    offenders, nearly all of them this. A guard that cries wolf is one the next
    person to hit it weakens, which this module's own docstring says out loud.

    See ``_object_root`` for the narrower collapse that ships alongside this
    one: the FP above is specifically about ``self``, and every attribute
    target paid for it.
    """
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return _dotted_name(target)
    if isinstance(target, ast.Subscript):
        return _written_name(target.value)
    return None


def _object_root(target: ast.AST) -> str | None:
    """The OBJECT an attribute write lands in, when that object is not ``self``.

    ``_written_name`` is the spelling the return side uses; this is the
    container, and both are recorded because a leak can travel under either::

        response = JSONResponse(status_code=500, content={"detail": "failed"})
        response.headers["X-Error"] = str(e)
        return response

    The write is to ``response.headers``; the return reads ``response``, so
    keying only on the dotted spelling reports the file clean while the text
    is on the wire in a header. #1400 measured that shape MISSED.

    ``self`` and ``cls`` are excluded, which is the whole reason the collapse
    is safe. The twelve false positives ``_written_name`` documents were all
    ``self.<field> = str(e)`` paired with a ``return`` that merely mentions
    ``self``; an instance is not a response object and its fields are not one
    payload. Measured over the response-producing surface this collapse adds
    **zero** findings; over the whole package it adds 17, every one a genuine
    "the object I return carries the text" in a CLI or a monitor that no guard
    scans (``cli/wipe_deployment.py``, ``health/component_monitor.py``).
    """
    while isinstance(target, (ast.Attribute, ast.Subscript)):
        target = target.value
    if isinstance(target, ast.Name) and target.id not in {"self", "cls"}:
        return target.id
    return None


def _dotted_name(node: ast.AST) -> str | None:
    """``self.a.b`` -> ``"self.a.b"``; anything else -> ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


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
        if isinstance(target, (ast.Tuple, ast.List)):
            # `msg, code = str(e), 500` binds each element. Without this the
            # whole statement was skipped and `msg` looked clean.
            for element in target.elts:
                bind(element, value)
            return
        name = _written_name(target)
        if name:
            assigns.setdefault(name, []).append(value)
        # ...and, for an attribute write, the object it lands in. Two keys for
        # one write, because the return side may spell it either way. See
        # ``_object_root``.
        root = _object_root(target) if not isinstance(target, ast.Name) else None
        if root and root != name:
            assigns.setdefault(root, []).append(value)

    for node in ast.walk(handler):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            bind(node.target, node.value)
        elif isinstance(node, ast.NamedExpr):
            bind(node.target, node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # A container MUTATED by method call is bound just as effectively
            # as one assigned into: `out.append({"error": str(e)})` and
            # `res.update(...)` are the natural siblings of the
            # `cleanup_results["k"] = ...` shape this analysis already covers,
            # and both reported clean. Any method is accepted rather than a
            # denylist of append/update/extend/setdefault -- the receiver is
            # what gets returned either way, and guessing which names mutate
            # is how the next spelling slips through.
            receiver = _written_name(node.func.value)
            if receiver:
                for value in [*node.args, *(kw.value for kw in node.keywords)]:
                    assigns.setdefault(receiver, []).append(value)
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

    A status the analysis **cannot read** answers True, not False. The range
    test above only reaches a literal and the spelling test only a recognisable
    constant; ``status_code=code`` matched neither and fell out of the bottom as
    "not a 5xx", so::

        except Exception as e:
            code = 500
            raise HTTPException(status_code=code, detail=str(e))

    was reported clean — #1400 measured that shape MISSED. Falling open on an
    unknown is the same defect the docstring above describes for ``503``, one
    step further out. What keeps this from swallowing the 4xx carve-out is that
    the carve-out is *recognisable*: ``status.HTTP_422_...`` and a bare ``404``
    both name their class, so only a genuinely opaque expression is treated as
    possibly-5xx. Measured cost over the response-producing surface: **zero**
    new findings — the one opaque site (``api/exception_handlers.py``, a
    re-raise carrying the original status) does not carry the exception text.
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
        if not _NON_5XX_STATUS_RE.search(text):
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


def _parse(path: pathlib.Path) -> ast.AST:
    """One tree per call of a public entry point.

    Load-bearing, not tidiness: the passes below share node-identity sets to
    avoid attributing one site to two of them, and ``id()`` is only comparable
    across nodes from the SAME parse. Re-parsing per pass made every
    ``in_handler`` lookup miss, which reported two static-message 500 arms in
    ``modules/case/api/routes.py`` as leaks — a false positive produced by the
    guard's plumbing rather than by its rule.
    """
    return ast.parse(path.read_text(encoding="utf-8"))


def _except_handlers(tree: ast.AST):
    """Every ``except ... as <name>`` handler in the tree, with its bound name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name:
            yield node


def _renders_the_live_exception(expr: ast.AST) -> bool:
    """Does ``expr`` render the in-flight exception WITHOUT naming it?

    ``traceback.format_exc()`` and ``sys.exc_info()`` read the exception the
    interpreter is currently handling, so a handler that uses them needs no
    ``as <name>`` — and the name-following analysis, which starts from that
    name, cannot see them at all. #1400 measured ``detail=traceback
    .format_exc()`` under a bare ``except Exception:`` as MISSED; it is the
    single richest leak on the list, since it carries the whole stack.

    Matched on the attribute name rather than on the module, because
    ``from traceback import format_exc`` is the same call.
    """
    return any(
        isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
        in _UNBOUND_EXCEPTION_RENDERERS
        for node in ast.walk(expr)
    )


def http_exception_leak_sites(path: pathlib.Path) -> list[str]:
    """``file:line`` for every 5xx ``HTTPException`` carrying the caught exception.

    Three shapes, all measured against #1400's mutation matrix:

    * the ``raise`` is inside a handler and its ``detail`` carries ``e``,
      directly or through a local alias;
    * the handler stashes the text in a local and the ``raise`` happens
      **after** it, back in the enclosing function — the exact twin of the
      shape ``returned_body_leak_sites`` has covered since #1394, and the
      reason this module's docstring stopped saying it was uncovered;
    * the ``detail`` renders the live exception without binding it at all
      (``traceback.format_exc()``), so there is no ``as <name>`` to follow.
    """
    tree = _parse(path)
    offenders: list[str] = []
    in_handler: set[int] = set()
    for handler in _except_handlers(tree):
        assigns = _local_assignments(handler)
        for node in ast.walk(handler):
            in_handler.add(id(node))
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            if not _is_http_exception(node.exc) or not _is_server_error(node.exc):
                continue
            detail = _detail_expr(node.exc)
            if detail is None:
                continue
            if _carries_exception(detail, handler.name, assigns):
                offenders.append(f"{path.name}:{node.lineno}")

    offenders.extend(_raise_after_handler_leak_sites(tree, path.name, in_handler))
    offenders.extend(_unbound_render_leak_sites(tree, path.name))
    # Sorted, and de-duplicated: the three passes walk overlapping node sets,
    # and reporting one site twice reads as a bug in the guard rather than as
    # two findings.
    return sorted(set(offenders), key=lambda s: int(s.rsplit(":", 1)[1]))


def _raise_after_handler_leak_sites(
    tree: ast.AST, filename: str, in_handler: set[int]
) -> list[str]:
    """5xx ``raise`` sites OUTSIDE a handler that carry a local it tainted.

        except Exception as e:
            message = f"...: {e}"
        raise HTTPException(status_code=500, detail=message)

    ``returned_body_leak_sites`` has covered this for ``return`` since #1394,
    and its docstring recorded the ``raise`` twin as deliberately uncovered
    because no such site existed. #1400 is shipping a widened guard, so "no
    site exists today" stops being a reason: the mutation matrix measured this
    shape MISSED, and closing it costs **zero** findings on the whole
    response-producing surface.

    ``in_handler`` is the set of nodes the in-handler pass already walked, so a
    site is never attributed to both. It arrives with ``tree`` rather than a
    path because ``id()`` only compares within one parse — see ``_parse``.
    """
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = list(_own_nodes(fn))
        handlers = [n for n in own if isinstance(n, ast.ExceptHandler) and n.name]
        if not handlers:
            continue
        tainted = _tainted_names(handlers)
        for node in own:
            if id(node) in in_handler:
                continue
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            if not _is_http_exception(node.exc) or not _is_server_error(node.exc):
                continue
            detail = _detail_expr(node.exc)
            if detail is None:
                continue
            if any(
                name in tainted and node.lineno > tainted[name]
                for name in _names_in(detail)
            ):
                offenders.append(f"{filename}:{node.lineno}")
    return offenders


def _unbound_render_leak_sites(tree: ast.AST, filename: str) -> list[str]:
    """5xx ``detail`` sites rendering the live exception with no ``as <name>``.

    Scoped to handlers that bind NOTHING, because that is the only case the
    name-following passes cannot reach; a handler that binds ``e`` and also
    calls ``traceback.format_exc()`` is already reported by the walk above if
    it puts either on the wire, and reporting it twice would read as two
    findings.
    """
    offenders: list[str] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or handler.name:
            continue
        for node in ast.walk(handler):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            if not _is_http_exception(node.exc) or not _is_server_error(node.exc):
                continue
            detail = _detail_expr(node.exc)
            if detail is not None and _renders_the_live_exception(detail):
                offenders.append(f"{filename}:{node.lineno}")
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


def _tainted_names(handlers: list[ast.ExceptHandler]) -> dict[str, int]:
    """Names bound in these handlers to the exception, and where that happens.

    The value is the line of the EARLIEST handler that taints the name. A
    ``return`` above that line cannot be carrying the text -- it has already
    executed by the time the handler can run -- and reporting it is the
    cry-wolf mode this module is careful about::

        def f(flag):
            data = build()
            if flag:
                return {"data": data}      # <- cannot be a leak
            try:
                do()
            except Exception as e:
                data = {"error": str(e)}
            return data                    # <- is one

    Line order is a coarse stand-in for reachability, not a real flow analysis.
    It is sound for the shape that matters (a handler stashing text that a
    later return carries) and it removes the one false positive that shape's
    guard produced.
    """
    tainted: dict[str, int] = {}
    for handler in handlers:
        assigns = _local_assignments(handler)
        for name, values in assigns.items():
            if any(_carries_exception(v, handler.name, assigns) for v in values):
                tainted[name] = min(tainted.get(name, handler.lineno), handler.lineno)
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

    A third shape is covered here since #1400: a handler that binds NOTHING
    and returns ``traceback.format_exc()``. There is no ``as <name>``, so
    neither of the two shapes above can see it, and it carries the whole
    stack rather than one message.

    (The ``raise``-outside-the-handler twin of the second shape used to be
    listed here as deliberately uncovered, on the grounds that no such site
    existed. #1400 shipped a surface-wide guard and measured that shape MISSED,
    so it is covered now — in ``http_exception_leak_sites``, where a ``raise``
    belongs — at a measured cost of zero findings.)

    Results are sorted. ``_own_nodes`` pops LIFO, so an unsorted list reports
    line numbers out of order, which reads as a bug in the guard.
    """
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = list(_own_nodes(fn))
        handlers = [n for n in own if isinstance(n, ast.ExceptHandler) and n.name]
        if not handlers:
            continue
        tainted = _tainted_names(handlers)
        # One pass: which handler owns each node, and each handler's assignment
        # map. The fallback below used to rebuild both per return per handler.
        owner: dict[int, ast.ExceptHandler] = {
            id(n): h for h in handlers for n in ast.walk(h)
        }
        assigns_by_handler = {id(h): _local_assignments(h) for h in handlers}

        for node in own:
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            leaks = any(
                name in tainted and node.lineno > tainted[name]
                for name in _names_in(node.value)
            )
            if not leaks:
                handler = owner.get(id(node))
                if handler is not None:
                    leaks = _carries_exception(
                        node.value, handler.name, assigns_by_handler[id(handler)]
                    )
            if leaks:
                offenders.append(f"{path.name}:{node.lineno}")

    # Third shape: an UNBOUND handler returning the live exception's rendering.
    # Its own walk, because the loop above starts from handlers that bind a
    # name and this one is defined by binding none.
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or handler.name:
            continue
        for node in ast.walk(handler):
            if (
                isinstance(node, ast.Return)
                and node.value is not None
                and _renders_the_live_exception(node.value)
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    return sorted(set(offenders), key=lambda s: int(s.rsplit(":", 1)[1]))


def _names_in(expr: ast.AST) -> set[str]:
    """Plain and dotted names an expression reads.

    Dotted too, because ``self.last_error`` is bound and read under that
    spelling -- collapsing it to ``self`` is what made every return mentioning
    ``self`` look like a leak.
    """
    found: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            dotted = _dotted_name(node)
            if dotted:
                found.add(dotted)
    return found
