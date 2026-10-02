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
* It follows local aliases. ``modules/case/api/routes/`` builds its detail one
  statement earlier::

      error_response = ErrorResponse(error=ErrorDetail(message=f"...{e}"))
      raise HTTPException(500, detail=error_response.model_dump())

  so the leaking text never appears in the ``detail`` expression at all. A guard
  that only inspects ``detail`` reports the file clean while the exception is on
  the wire — which is precisely how one such site survived #966.

**Which statuses.** An ``HTTPException`` carrying the exception's text is
reported at 5xx, and at **any** status when the exception came from a *broad*
source: a bare ``except:``, ``except Exception`` or ``except BaseException``
(alone or in a tuple), or a parameter annotated ``Exception`` or
``BaseException``. The 4xx carve-out #866/#966 wrote down is about the
exception's TYPE — ``detail=str(e)`` on a ``ValidationException`` or
``InvalidGrantError`` arm is a domain message written *for* the caller — and a
broad source has no domain type, so the carve-out has nothing to stand on
there. #1598 measured ``except Exception as e: raise HTTPException(400,
detail=str(e))`` passing a 5xx-only rule silently, and the owner ruled the rule
widen to it (2026-09-30). A *typed* source at 4xx stays exempt.

**Where the leak is built.** Not only at a ``raise HTTPException(...)`` written
inside the function that caught the exception. #1634's leak crossed a function
boundary in both directions, and the analysis reported its file clean:

* **Down, into a factory.** ``api/exception_handlers.py``'s ``_llm_http``
  builds the ``HTTPException`` from its ``detail`` parameter, so no caller ever
  spells ``HTTPException`` at all. A same-module function whose own ``return
  HTTPException(...)`` carries one of its parameters into ``detail`` is a
  factory, and a call to it is read as the construction it performs — see
  ``_http_exception_factories``.
* **Up, out of a helper.** ``llm_service_error_http_exception(exc:
  BaseException)`` receives the exception as a parameter, so there is no
  ``except ... as`` binding for the taint to start from. An exception-typed
  parameter is a taint source too, and the finding is the helper's own
  ``return``/``raise`` — that is where the text is put in the body — not its
  callers. See ``_exception_parameter_leak_sites``.

**Stated limits.** Four shapes are outside this analysis on purpose, written
down so the next widening starts from a measurement rather than re-deriving
one. When they were recorded (#1634) none of them was a live leak on the
response-producing surface:

* a factory that builds its ``HTTPException`` by calling another factory — the
  factory view is one level deep. Iterating it to a fixed point added zero
  findings;
* a factory defined in a different module from the call. Treating every
  factory in the package as visible from every file added zero findings;
* a helper that *returns* a ``JSONResponse`` (or any body other than an
  ``HTTPException``) carrying an exception parameter —
  ``returned_body_leak_sites`` starts from ``except`` bindings only. No helper
  with a *broad* exception parameter returned one; the typed ones that do are
  the domain handlers in ``api/exception_handlers.py``, which are shape O's
  sinks below, and the ``HTTPException`` handler itself, which renders a
  ``detail`` already built;
* a custom typed exception carrying the text into one of the domain handlers
  that render ``str(exc)`` (#1598's shape O). Closed, not guarded, by the #1598
  ruling: its entire live population was five legitimate sites, and a guard
  over it would be an allowlist and nothing else.
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import NamedTuple

# HTTPException(status_code, detail=None, headers=None) — `detail` is
# positional index 1 when not passed by keyword.
_DETAIL_POSITION = 1
_STATUS_POSITION = 0

_MAX_ALIAS_DEPTH = 5

#: A status expression that RECOGNISABLY names a non-5xx class. Used only to
#: decide whether an unreadable ``status_code`` should fail open or closed —
#: see ``_status_is_5xx``.
_NON_5XX_STATUS_RE = re.compile(r"HTTP_[1234]\d\d|\b[1234]\d\d\b")

#: The exception classes that name no domain at all. A handler or parameter
#: typed as one of these is a *broad* source, and its text is checked at every
#: status — see the module docstring and ``_is_broad_handler_type``.
_BROAD_EXCEPTION_TYPES = frozenset({"Exception", "BaseException"})

#: Parameter names that carry an exception when nothing annotates them. An
#: unannotated parameter is never *broad* — nothing says it is not a domain
#: exception, or not a message string at all — so it keeps the 5xx gate:
#: ``def bad(error): raise HTTPException(400, detail=error)`` reads as a
#: caller-facing message helper as plausibly as a leak, and the name alone
#: cannot tell the two apart.
_UNANNOTATED_EXCEPTION_PARAMS = frozenset(
    {"e", "ex", "exc", "err", "error", "exception"}
)

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


def _local_assignments(scope: ast.AST) -> dict[str, list[ast.AST]]:
    """Every binding made inside ``scope``, keyed by the name written.

    ``scope`` is an except handler when the taint starts from its ``as`` name,
    and a whole function when it starts from an exception parameter or when a
    factory's ``detail`` is traced back to the parameter it carries.

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

    for node in ast.walk(scope):
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
    """Does ``expr`` carry the exception named ``exc_name``, directly or via a
    local alias?

    ``exc_name`` is a handler's ``as`` name or an exception parameter; the
    question is the same either way.
    """
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


def _status_expr(call: ast.Call) -> ast.AST | None:
    """The ``status_code`` argument, whether passed by keyword or positionally."""
    for kw in call.keywords:
        if kw.arg == "status_code":
            return kw.value
    if len(call.args) > _STATUS_POSITION:
        return call.args[_STATUS_POSITION]
    return None


def _status_is_5xx(node: ast.AST | None) -> bool:
    """Is this status expression a 5xx — or one the analysis cannot read?

    Per *expression*, not per call, because a factory's status is whatever
    argument its caller bound to the factory's status parameter, and that
    argument is not the ``status_code`` of any ``HTTPException(...)`` in the
    caller's source. ``None`` is a status nobody bound — a factory parameter
    left at its default — and is *unknown*, which fails closed below.

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

    This gate is only half of the status rule: a site whose taint came from a
    broad source is checked whatever this answers (#1598). Each pass applies
    that as ``broad or _status_is_5xx(status)``.
    """
    if node is None:
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return 500 <= node.value <= 599
    text = ast.unparse(node)
    if "500" in text or "INTERNAL_SERVER_ERROR" in text or "HTTP_5" in text:
        return True
    return not _NON_5XX_STATUS_RE.search(text)


def _detail_expr(call: ast.Call) -> ast.AST | None:
    """The ``detail`` argument, whether passed by keyword or positionally."""
    for kw in call.keywords:
        if kw.arg == "detail":
            return kw.value
    if len(call.args) > _DETAIL_POSITION:
        return call.args[_DETAIL_POSITION]
    return None


def _terminal_name(node: ast.AST) -> str | None:
    """``Name`` -> its id, ``a.b.C`` -> ``"C"``; anything else -> ``None``."""
    return getattr(node, "id", None) or getattr(node, "attr", None)


def _is_http_exception(call: ast.Call) -> bool:
    return _terminal_name(call.func) == "HTTPException"


def _is_broad_handler_type(node: ast.AST | None) -> bool:
    """Does an ``except`` clause of this type catch with no domain in it?

    ``None`` is a bare ``except:``. A tuple is broad when any member is: in
    ``except (ValueError, Exception) as e`` the ``Exception`` arm catches
    everything the ``ValueError`` does not, and ``e`` may be any of it.
    Matched on the terminal name, so ``builtins.Exception`` is the same class.
    """
    if node is None:
        return True
    if isinstance(node, ast.Tuple):
        return any(_is_broad_handler_type(element) for element in node.elts)
    return _terminal_name(node) in _BROAD_EXCEPTION_TYPES


def _annotation_classes(annotation: ast.AST | None) -> list[str]:
    """The class names an annotation admits.

    Reads through the spellings an optional exception parameter actually
    takes — ``X``, ``Optional[X]``, ``Union[X, Y]``, ``X | None`` — and a
    string annotation, which ``from __future__ import annotations`` code and
    forward references both produce. Any other subscript answers its outer
    name only: ``type[Exception]`` admits a class, not an instance.
    """
    if annotation is None:
        return []
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _annotation_classes(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return []
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_classes(annotation.left) + _annotation_classes(
            annotation.right
        )
    if isinstance(annotation, ast.Subscript):
        outer = _terminal_name(annotation.value)
        if outer in {"Optional", "Union"}:
            inner = annotation.slice
            elements = inner.elts if isinstance(inner, ast.Tuple) else [inner]
            return [name for e in elements for name in _annotation_classes(e)]
        return [outer] if outer else []
    name = _terminal_name(annotation)
    return [name] if name else []


def _exception_params(fn: ast.AST) -> list[tuple[str, bool]]:
    """``(name, broad)`` for every parameter of ``fn`` that can carry an exception.

    A parameter counts when its annotation admits a class named ``Exception``
    or ``BaseException`` or ending in ``Exception`` or ``Error``. With no
    annotation, the name decides: ``exc`` and its usual spellings
    (``_UNANNOTATED_EXCEPTION_PARAMS``).

    The suffix test is a name test, so a domain exception named otherwise —
    ``TeamOperationRefused``, ``TenantTurnCapExceeded`` — is not recognised.
    Such a parameter would be typed, so it could only matter at 5xx. When this
    was written one surface parameter was typed that way, in
    ``team_operation_refused_handler``, which returns a ``JSONResponse`` and
    so builds no ``HTTPException`` for this pass to read.

    ``broad`` is True only for an annotation naming ``Exception`` or
    ``BaseException``. A parameter typed as a domain exception is the helper
    twin of a typed ``except`` arm, and keeps the 4xx carve-out the same way;
    an unannotated one keeps it too, because nothing says it is not one.
    """
    found: list[tuple[str, bool]] = []
    args = fn.args
    for param in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        classes = _annotation_classes(param.annotation)
        if classes:
            exception_classes = [
                c
                for c in classes
                if c in _BROAD_EXCEPTION_TYPES
                or c.endswith("Exception")
                or c.endswith("Error")
            ]
            if exception_classes:
                found.append(
                    (
                        param.arg,
                        any(c in _BROAD_EXCEPTION_TYPES for c in exception_classes),
                    )
                )
        elif param.annotation is None and param.arg in _UNANNOTATED_EXCEPTION_PARAMS:
            found.append((param.arg, False))
    return found


class _Factory(NamedTuple):
    """What a call to a same-module ``HTTPException`` factory builds.

    ``positional`` is the parameter order a positional argument binds by, and
    ``params`` every name a keyword argument can bind. ``status`` is the
    ``status_code`` expression of the factory's own ``HTTPException(...)``,
    in the factory's terms; ``detail_params`` are the parameters that reach
    its ``detail``.
    """

    positional: list[str]
    params: list[str]
    status: ast.AST | None
    detail_params: frozenset[str]


def _http_exception_factories(tree: ast.AST) -> dict[str, _Factory]:
    """Every function in ``tree`` that builds an ``HTTPException`` from a parameter.

    A factory is a function whose own ``return HTTPException(...)`` carries one
    of its parameters into ``detail`` — directly or through a local alias,
    by the same ``_carries_exception`` that follows a handler's ``as`` name.
    ``api/exception_handlers.py``'s ``_llm_http(status_code, error_code,
    detail, ...)`` is the shape: its callers never spell ``HTTPException``, so
    without this a ``raise _llm_http(500, ..., str(e), ...)`` was invisible to
    every pass. #1634's leak went through it.

    Keyed by name, because a call names its target only by name (``f(...)``)
    or by terminal attribute (``self.f(...)``). Same module only, and one
    level: a factory whose ``HTTPException`` comes from calling another
    factory is not one here — both are stated limits in the module docstring.
    When this was written the response-producing surface held two:
    ``exception_handlers.py::_llm_http`` and
    ``operator_user_scope.py::user_not_found``.
    """
    factories: dict[str, _Factory] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = fn.args
        positional = [p.arg for p in [*args.posonlyargs, *args.args]]
        params = positional + [p.arg for p in args.kwonlyargs]
        assigns = _local_assignments(fn)
        for node in _own_nodes(fn):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not _is_http_exception(call):
                continue
            detail = _detail_expr(call)
            if detail is None:
                continue
            detail_params = frozenset(
                p for p in params if _carries_exception(detail, p, assigns)
            )
            if detail_params:
                factories[fn.name] = _Factory(
                    positional, params, _status_expr(call), detail_params
                )
    return factories


def _http_exception_view(
    call: ast.AST | None, factories: dict[str, _Factory]
) -> tuple[ast.AST | None, ast.AST | None] | None:
    """``(status, detail)`` if ``call`` builds an ``HTTPException``, else ``None``.

    The one question every ``HTTPException`` pass asks, so that a factory call
    is seen by all of them at once rather than by whichever pass remembered it.

    * A direct ``HTTPException(...)`` answers its own ``status_code`` and
      ``detail`` arguments.
    * A call to a factory answers the CALLER's arguments: ``detail`` is the
      arguments bound to the factory's detail parameters (a tuple of them, which
      every reader below walks like any other expression), and ``status`` is the
      argument bound to the factory's status parameter — or the factory's own
      expression when that is not a parameter (``_f(msg)`` building a literal
      500). A status parameter the call leaves at its default is ``None``,
      unknown, and ``_status_is_5xx`` fails closed on it.

    A method factory reached through ``self.``/``cls.`` binds its receiver
    implicitly, so positional binding starts after it.
    """
    if not isinstance(call, ast.Call):
        return None
    if _is_http_exception(call):
        return _status_expr(call), _detail_expr(call)
    factory = factories.get(_terminal_name(call.func))
    if factory is None:
        return None
    positional = factory.positional
    if (
        isinstance(call.func, ast.Attribute)
        and positional
        and positional[0] in {"self", "cls"}
    ):
        positional = positional[1:]
    bound: dict[str, ast.AST] = {}
    for index, arg in enumerate(call.args):
        if index < len(positional):
            bound[positional[index]] = arg
    for kw in call.keywords:
        if kw.arg in factory.params:
            bound[kw.arg] = kw.value
    detail_args = [bound[p] for p in factory.detail_params if p in bound]
    detail = ast.Tuple(elts=detail_args, ctx=ast.Load()) if detail_args else None
    status = factory.status
    if isinstance(status, ast.Name) and status.id in factory.params:
        status = bound.get(status.id)  # unbound -> None -> unknown -> 5xx
    return status, detail


def _parse(path: pathlib.Path) -> ast.AST:
    """One tree per call of a public entry point.

    Load-bearing, not tidiness: the passes below share node-identity sets to
    avoid attributing one site to two of them, and ``id()`` is only comparable
    across nodes from the SAME parse. Re-parsing per pass made every
    ``in_handler`` lookup miss, which reported two static-message 500 arms in
    ``modules/case/api/routes/`` as leaks — a false positive produced by the
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
    """``file:line`` for every ``HTTPException`` carrying an exception's text.

    Reported at 5xx, or at any status when the exception came from a broad
    source — see the module docstring for the rule and #1598 for the ruling.
    "``HTTPException``" includes a call to a same-module factory that builds
    one (``_http_exception_view``), in every pass below.

    Four shapes, the first three measured against #1400's mutation matrix and
    the fourth against #1634's:

    * the ``raise`` is inside a handler and its ``detail`` carries ``e``,
      directly or through a local alias;
    * the handler stashes the text in a local and the ``raise`` happens
      **after** it, back in the enclosing function — the exact twin of the
      shape ``returned_body_leak_sites`` has covered since #1394, and the
      reason this module's docstring stopped saying it was uncovered;
    * the ``detail`` renders the live exception without binding it at all
      (``traceback.format_exc()``), so there is no ``as <name>`` to follow;
    * the exception arrives as a PARAMETER, and the function raises or
      returns an ``HTTPException`` carrying it — the helper twin of the first
      shape, reported at the helper's own line.
    """
    tree = _parse(path)
    factories = _http_exception_factories(tree)
    offenders: list[str] = []
    in_handler: set[int] = set()
    for handler in _except_handlers(tree):
        # A broad handler lifts the status gate (#1598): with no domain type
        # in the clause, a 4xx carrying `e` is not a message written for the
        # caller — it is whatever the code under `try` happened to raise.
        broad = _is_broad_handler_type(handler.type)
        assigns = _local_assignments(handler)
        for node in ast.walk(handler):
            in_handler.add(id(node))
            if not isinstance(node, ast.Raise):
                continue
            view = _http_exception_view(node.exc, factories)
            if view is None:
                continue
            status, detail = view
            if detail is None or not (broad or _status_is_5xx(status)):
                continue
            if _carries_exception(detail, handler.name, assigns):
                offenders.append(f"{path.name}:{node.lineno}")

    offenders.extend(
        _raise_after_handler_leak_sites(tree, path.name, in_handler, factories)
    )
    offenders.extend(_unbound_render_leak_sites(tree, path.name, factories))
    offenders.extend(_exception_parameter_leak_sites(tree, path.name, factories))
    # Sorted, and de-duplicated: the four passes walk overlapping node sets,
    # and reporting one site twice reads as a bug in the guard rather than as
    # two findings.
    return sorted(set(offenders), key=lambda s: int(s.rsplit(":", 1)[1]))


def _raise_after_handler_leak_sites(
    tree: ast.AST,
    filename: str,
    in_handler: set[int],
    factories: dict[str, _Factory],
) -> list[str]:
    """``raise`` sites OUTSIDE a handler that carry a local it tainted.

        except Exception as e:
            message = f"...: {e}"
        raise HTTPException(status_code=500, detail=message)

    Gated like the in-handler pass: 5xx, or any status when a broad handler
    tainted the local (``_tainted_names`` records which).

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
            if id(node) in in_handler or not isinstance(node, ast.Raise):
                continue
            view = _http_exception_view(node.exc, factories)
            if view is None or view[1] is None:
                continue
            status, detail = view
            is_5xx = _status_is_5xx(status)
            if any(
                name in tainted
                and node.lineno > tainted[name].line
                and (tainted[name].broad or is_5xx)
                for name in _names_in(detail)
            ):
                offenders.append(f"{filename}:{node.lineno}")
    return offenders


def _unbound_render_leak_sites(
    tree: ast.AST, filename: str, factories: dict[str, _Factory]
) -> list[str]:
    """``detail`` sites rendering the live exception with no ``as <name>``.

    Scoped to handlers that bind NOTHING, because that is the only case the
    name-following passes cannot reach; a handler that binds ``e`` and also
    calls ``traceback.format_exc()`` is already reported by the walk above if
    it puts either on the wire, and reporting it twice would read as two
    findings.

    Same status gate as the other passes: 5xx, or any status under a broad
    clause — and the clause this pass exists for, a bare ``except:`` or
    ``except Exception:``, is the broad one. ``traceback.format_exc()`` in a
    400 is the whole stack in a 400.
    """
    offenders: list[str] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or handler.name:
            continue
        broad = _is_broad_handler_type(handler.type)
        for node in ast.walk(handler):
            if not isinstance(node, ast.Raise):
                continue
            view = _http_exception_view(node.exc, factories)
            if view is None or view[1] is None:
                continue
            status, detail = view
            if (broad or _status_is_5xx(status)) and _renders_the_live_exception(
                detail
            ):
                offenders.append(f"{filename}:{node.lineno}")
    return offenders


def _exception_parameter_leak_sites(
    tree: ast.AST, filename: str, factories: dict[str, _Factory]
) -> list[str]:
    """``raise``/``return`` sites whose ``HTTPException`` carries an exception
    PARAMETER.

    The other three passes start from an ``except ... as`` binding, so a
    helper that receives the exception — ``def h(exc: BaseException) ->
    HTTPException`` — has nothing for them to start from. That is the upward
    half of #1634: ``llm_service_error_http_exception(exc)`` put
    ``str(exc)[:200]`` into ``_llm_http``'s ``detail``, and the file was
    reported clean while ``/turns`` echoed internal text.

    Reported at the helper's own ``return``/``raise`` line, not at its callers:
    the helper is where the text is put in the body, a caller only passes an
    exception along, and one finding per helper is the one a fix closes.
    ``_offender_keys`` in the surface guard maps a ``Return`` exactly as it
    maps a ``Raise``.

    Which parameters count, and which are broad, is ``_exception_params``. The
    status gate is the handler passes' gate with ``broad`` from the
    annotation. The whole function is the alias scope (``text = str(exc)``
    and then ``detail=text``), as the whole handler is for an ``as`` name.
    """
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = _exception_params(fn)
        if not params:
            continue
        assigns = _local_assignments(fn)
        for node in _own_nodes(fn):
            if isinstance(node, ast.Raise):
                built = node.exc
            elif isinstance(node, ast.Return):
                built = node.value
            else:
                continue
            view = _http_exception_view(built, factories)
            if view is None or view[1] is None:
                continue
            status, detail = view
            is_5xx = _status_is_5xx(status)
            if any(
                (broad or is_5xx) and _carries_exception(detail, name, assigns)
                for name, broad in params
            ):
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


class _Taint(NamedTuple):
    """Where a local was first tainted, and whether any broad handler did it."""

    line: int
    broad: bool


def _tainted_names(handlers: list[ast.ExceptHandler]) -> dict[str, _Taint]:
    """Names bound in these handlers to the exception, and where that happens.

    ``broad`` is True when ANY handler tainting the name is a broad one, which
    lifts the status gate for a ``raise`` after it (#1598) exactly as it does
    for one inside it. ``returned_body_leak_sites`` reads only ``line``: a
    returned body has no status gate to lift.

    ``line`` is the line of the EARLIEST handler that taints the name. A
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
    tainted: dict[str, _Taint] = {}
    for handler in handlers:
        broad = _is_broad_handler_type(handler.type)
        assigns = _local_assignments(handler)
        for name, values in assigns.items():
            if any(_carries_exception(v, handler.name, assigns) for v in values):
                seen = tainted.get(name, _Taint(handler.lineno, False))
                tainted[name] = _Taint(
                    min(seen.line, handler.lineno), seen.broad or broad
                )
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
                name in tainted and node.lineno > tainted[name].line
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
