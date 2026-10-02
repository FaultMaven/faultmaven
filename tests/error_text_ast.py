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

**What is on the wire.** An ``HTTPException``'s ``detail`` **and its
``headers``** — a response header reaches the caller exactly as a body does,
and the returned-body half has treated ``response.headers[...] = str(e)`` as a
leak since #1400. A factory parameter that becomes a header counts the same
way: ``_llm_http(503, code_of(exc), ...)`` puts ``code_of(exc)`` into
``x-error-code``.

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
  spells ``HTTPException`` at all. A same-module function whose own
  ``HTTPException(...)`` — returned, raised or assigned — carries one of its
  parameters onto the wire is a factory, and a call to it is read as the
  construction it performs — see ``_http_exception_factories``. That includes
  a *raising* helper called as a statement (``_fail(str(e))``), the house
  idiom for a 404 helper.
* **Up, out of a helper.** ``llm_service_error_http_exception(exc:
  BaseException)`` receives the exception as a parameter, so there is no
  ``except ... as`` binding for the taint to start from. An exception-typed
  parameter is a taint source too. The finding is the helper's own
  construction — that is where the text is put on the wire. When the helper
  spells ``HTTPException(...)`` itself it is also a factory, so every
  same-module caller that hands it an exception is reported as well, and
  fixing the helper clears all of them. A helper that builds through another
  factory, as ``llm_service_error_http_exception`` builds through
  ``_llm_http``, is not itself one (the view is one level deep) and is
  reported alone. See ``_exception_parameter_leak_sites``.

Every call the factory view recognises is a sink, wherever it stands: raised,
returned, assigned and raised later, awaited, or called for its side effect.
Each is reported at the line of the call.

**Stated limits.** These shapes are outside this analysis on purpose, written
down so the next widening starts from a measurement rather than re-deriving
one. When they were recorded (#1634) none of the first four was a live leak on
the response-producing surface:

* a factory that builds its ``HTTPException`` by calling another factory — the
  factory view is one level deep. Iterating it to a fixed point added zero
  findings;
* a factory defined in a different module from the call, and a same-module
  function reached through an attribute (``this_module._fail(...)``), which
  reads as a method call. Treating every factory in the package as visible
  from every file added zero findings;
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

Four more are tracked in #1858, the issue for a single function-scope taint
pass that would subsume them:

* a two-hop stash after the handler — ``err = str(e)`` in the handler, then
  ``message = f"...{err}"`` and the raise after it. The raise-after pass
  follows one alias out of the handler, not a chain;
* a closure defined OUTSIDE the handler that raises with the handler's
  exception, and a ``lambda`` factory (``mk = lambda m: HTTPException(500,
  m)``). A closure defined inside the handler is walked with it and is caught;
  one defined before the ``try`` is its own scope. A factory is a ``def``;
* an unannotated exception parameter named outside
  ``_UNANNOTATED_EXCEPTION_PARAMS``;
* a local stashed by a *typed* handler and raised inside a sibling *broad*
  handler of the same ``try``.

#1858 also records the zero-live shapes #1634's second defeat pass found: a
rendering stashed in a local before it is used; a ``detail`` or header written
onto the exception after it is built; ``*args``/``**kwargs`` factories and
factory aliases; a conditional status not spelled as ``x if c else y``;
annotation forms the reader cannot parse; a cause chain walked under a typed
handler; and ``match``.

And one is pending a ruling: "broad" is decided by class NAME, so a generic
wrapper — ``ServiceException``, ``RuntimeError`` — keeps the typed 4xx
carve-out although it carries whatever it wrapped. Whether to widen "broad" is
#1860; the two probable live sites are #1859.
"""

from __future__ import annotations

import ast
import builtins
import functools
import pathlib
import re
from typing import NamedTuple

# HTTPException(status_code, detail=None, headers=None) — the positional
# index of each argument when it is not passed by keyword.
_STATUS_POSITION = 0
_DETAIL_POSITION = 1
_HEADERS_POSITION = 2

_MAX_ALIAS_DEPTH = 5

#: A status expression that RECOGNISABLY names a non-5xx class. Used only to
#: decide whether an unreadable ``status_code`` should fail open or closed —
#: see ``_status_is_5xx``.
_NON_5XX_STATUS_RE = re.compile(r"HTTP_[1234]\d\d|\b[1234]\d\d\b")

#: The exception classes that name no domain at all. A handler or parameter
#: typed as one of these is a *broad* source, and its text is checked at every
#: status — see the module docstring and ``_is_broad_handler_type``.
_BROAD_EXCEPTION_TYPES = frozenset({"Exception", "BaseException"})

#: Parameter names that carry an exception when nothing annotates them, or
#: when the annotation says nothing (``_UNTYPED_ANNOTATIONS``). Such a
#: parameter is never *broad* — nothing says it is not a domain exception, or
#: not a message string at all — so it keeps the 5xx gate:
#: ``def bad(error): raise HTTPException(400, detail=error)`` reads as a
#: caller-facing message helper as plausibly as a leak, and the name alone
#: cannot tell the two apart.
_UNANNOTATED_EXCEPTION_PARAMS = frozenset(
    {"e", "ex", "exc", "err", "error", "exception"}
)

#: Annotations that admit anything, an exception included. ``exc: Any`` is as
#: uninformative as no annotation, so the name decides, as it does there.
_UNTYPED_ANNOTATIONS = frozenset({"Any", "object"})

#: Every exception class the interpreter defines — the seed the package's own
#: exception classes are derived from (``_package_exception_classes``).
_BUILTIN_EXCEPTIONS = frozenset(
    name
    for name in dir(builtins)
    if isinstance(getattr(builtins, name), type)
    and issubclass(getattr(builtins, name), BaseException)
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
    give the alias-following two silent blind spots. So do a ``for`` (or
    comprehension) target, bound to what it iterates, and a ``with ... as``
    target, bound to the context expression: #1634's review measured ``for c in
    walk_cause_chain(exc)`` breaking the taint in the very helper #1634 is
    about.

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
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            # A loop target is an alias of what it iterates. `for c in
            # walk_cause_chain(exc)` is this codebase's own idiom for reading
            # an exception's cause chain -- `api/exception_handlers.py` has
            # two such `for` statements and four comprehensions -- and
            # `str(c)` carries the text of a link of `exc` exactly as
            # `str(exc)` carries its own.
            bind(node.target, node.iter)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            # `with ctx(e) as msg:` binds `msg` from the context expression.
            for item in node.items:
                if item.optional_vars is not None:
                    bind(item.optional_vars, item.context_expr)
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
    caller's source. ``None`` is a status nobody can read — a factory status
    parameter the call leaves unbound and that has no default — and is
    *unknown*, which fails closed below. A conditional status (``400 if a else
    503``) is a 5xx if either branch can be.

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
    broad source is checked whatever this answers (#1598). ``_gated`` is the
    whole rule, and every pass asks it.
    """
    if node is None:
        return True
    if isinstance(node, ast.IfExp):
        return _status_is_5xx(node.body) or _status_is_5xx(node.orelse)
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


def _headers_expr(call: ast.Call) -> ast.AST | None:
    """The ``headers`` argument, whether passed by keyword or positionally."""
    for kw in call.keywords:
        if kw.arg == "headers":
            return kw.value
    if len(call.args) > _HEADERS_POSITION:
        return call.args[_HEADERS_POSITION]
    return None


def _carried_expr(call: ast.Call) -> ast.AST | None:
    """Everything a direct ``HTTPException(...)`` puts on the wire.

    ``detail`` **and** ``headers``. Reading ``detail`` alone was a blind spot
    #1634's review measured against 57 live ``headers=`` constructions: the
    ``x-error-code`` header ``_llm_http`` builds from its ``error_code``
    parameter reaches the caller exactly as the body does. Two arguments come
    back as one tuple, which every reader below walks like any other
    expression.
    """
    parts = [e for e in (_detail_expr(call), _headers_expr(call)) if e is not None]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else ast.Tuple(elts=parts, ctx=ast.Load())


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


def _is_exception_class_name(name: str, known: frozenset[str] | set[str]) -> bool:
    """Is ``name`` an exception class, by name or by the derived class set?"""
    return (
        name in _BROAD_EXCEPTION_TYPES
        or name in known
        or name.endswith(("Exception", "Error"))
    )


def _classes_deriving(trees: list[ast.AST], seed: frozenset[str]) -> frozenset[str]:
    """``seed`` plus every class in ``trees`` that derives from an exception.

    By name and transitively: a class whose base is in the set, or is named
    like an exception (``*Exception``/``*Error``), joins it, and so do the
    classes deriving from that one. Iterated to a fixed point, because a
    subclass may be read before its base.
    """
    bases: dict[str, set[str]] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases.setdefault(node.name, set()).update(
                    _terminal_name(base) or "" for base in node.bases
                )
    known = set(seed)
    changed = True
    while changed:
        changed = False
        for name, parents in bases.items():
            if name not in known and any(
                _is_exception_class_name(parent, known) for parent in parents
            ):
                known.add(name)
                changed = True
    return frozenset(known)


@functools.lru_cache(maxsize=1)
def _package_exception_classes() -> frozenset[str]:
    """Every exception class ``faultmaven`` defines, plus the builtin ones.

    The suffix test alone missed 23 of the package's exception classes —
    ``TeamOperationRefused``, ``TenantTurnCapExceeded``, ``PathEscape`` — so a
    parameter typed as one was not a taint source at all. Derived from the
    source rather than listed, so a new exception class is covered the day it
    is written. Cached: the package cannot change inside one test session, and
    this parses all of it.
    """
    import faultmaven

    trees: list[ast.AST] = []
    for path in pathlib.Path(faultmaven.__file__).parent.rglob("*.py"):
        try:
            trees.append(ast.parse(path.read_text(encoding="utf-8")))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
    return _classes_deriving(trees, _BUILTIN_EXCEPTIONS)


def _module_exception_classes(tree: ast.AST) -> frozenset[str]:
    """The package's exception classes plus those ``tree`` defines itself."""
    return _classes_deriving([tree], _package_exception_classes())


def _exception_params(
    fn: ast.AST, exception_classes: frozenset[str]
) -> list[tuple[str, bool]]:
    """``(name, broad)`` for every parameter of ``fn`` that can carry an exception.

    A parameter counts when its annotation admits an exception class:
    ``Exception``, ``BaseException``, a name in ``exception_classes`` (the
    package's exception classes and the module's own, derived by inheritance —
    see ``_package_exception_classes``), or a name ending in ``Exception`` or
    ``Error``. With no annotation, one that admits anything (``Any``,
    ``object``), or one that names no class the analysis can read, the name
    decides: ``exc`` and its usual spellings
    (``_UNANNOTATED_EXCEPTION_PARAMS``).

    ``broad`` is True only for an annotation naming ``Exception`` or
    ``BaseException``. A parameter typed as a domain exception is the helper
    twin of a typed ``except`` arm, and keeps the 4xx carve-out the same way;
    an unannotated one keeps it too, because nothing says it is not one.
    Whether a generic wrapper such as ``ServiceException`` should count as
    broad is #1860.
    """
    found: list[tuple[str, bool]] = []
    args = fn.args
    for param in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        classes = _annotation_classes(param.annotation)
        if classes and not set(classes) <= _UNTYPED_ANNOTATIONS:
            exception_types = [
                c for c in classes if _is_exception_class_name(c, exception_classes)
            ]
            if exception_types:
                found.append(
                    (
                        param.arg,
                        any(c in _BROAD_EXCEPTION_TYPES for c in exception_types),
                    )
                )
        elif param.arg in _UNANNOTATED_EXCEPTION_PARAMS:
            found.append((param.arg, False))
    return found


class _Factory(NamedTuple):
    """What a call to one same-module ``HTTPException`` factory builds.

    ``positional`` is the parameter order a positional argument binds by, and
    ``params`` every name a keyword argument can bind. ``defaults`` is each
    parameter's default expression. ``statuses`` holds the ``status_code``
    expression of EVERY ``HTTPException(...)`` the factory builds from a
    parameter, in the factory's terms; ``carried_params`` are the parameters
    that reach any of their ``detail`` or ``headers``. ``is_method`` says the
    function is defined directly in a class body, which decides the calls
    that can reach it (``_http_exception_view``). ``rebound`` is every name
    the function's own body assigns to: a default that the body may overwrite
    is not the status the factory builds with.
    """

    positional: list[str]
    params: list[str]
    defaults: dict[str, ast.AST]
    statuses: tuple[ast.AST | None, ...]
    carried_params: frozenset[str]
    is_method: bool
    rebound: frozenset[str]


def _param_defaults(args: ast.arguments) -> dict[str, ast.AST]:
    """Each parameter's default expression, by name; no entry when there is none."""
    positional = [*args.posonlyargs, *args.args]
    defaults: dict[str, ast.AST] = {}
    for param, default in zip(
        positional[len(positional) - len(args.defaults) :], args.defaults
    ):
        defaults[param.arg] = default
    for param, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            defaults[param.arg] = default
    return defaults


def _rebound_names(fn: ast.AST) -> frozenset[str]:
    """Every name ``fn``'s own body assigns to, nested functions excluded.

    ``x = ...``, ``x: T = ...``, ``x += ...``, ``(x := ...)`` and a ``for``
    target, through any tuple or starred unpacking. A status parameter in this
    set is not reliably its default::

        def _err(msg, status_code=400):
            if "timeout" in msg:
                status_code = 504
            return HTTPException(status_code=status_code, detail=msg)

    reads as a 400 by its default and builds a 504. #1634's review measured
    the first reading of defaults exempting exactly that under a typed
    handler, where the earlier, fail-closed reading had caught it.
    """
    names: set[str] = set()
    for node in _own_nodes(fn):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(
            node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)
        ):
            targets = [node.target]
        else:
            continue
        for target in targets:
            names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return frozenset(names)


def _http_exception_factories(tree: ast.AST) -> dict[str, list[_Factory]]:
    """Every function in ``tree`` that builds an ``HTTPException`` from a parameter.

    A factory is a function whose own ``HTTPException(...)`` — returned,
    raised or assigned, wherever in the function it is built — carries one of
    its parameters into ``detail`` or ``headers``, directly or through a local
    alias, by the same ``_carries_exception`` that follows a handler's ``as``
    name. ``api/exception_handlers.py``'s ``_llm_http(status_code, error_code,
    detail, ...)`` is the shape: its callers never spell ``HTTPException``, so
    without this a ``raise _llm_http(500, ..., str(e), ...)`` was invisible to
    every pass. #1634's leak went through it.

    So is a *raising* helper — ``def _fail(msg): raise HTTPException(404,
    detail=msg)``, called as a statement. #1634's review counted six live on
    the surface, and an analysis that looked only at a factory's ``return``
    saw none of them.

    All of a factory's constructions are merged: the union of the parameters
    they carry, and every status. Reading one arbitrary ``return`` took the
    status of whichever the walk met last, which made ``client=True ->
    400 else 500`` a 400. Keyed by name, because a call names its target only
    by name (``f(...)``) or by terminal attribute (``self.f(...)``), and
    same-named functions — two classes' ``_err`` methods — keep one record
    each, all applied at a call: the analysis cannot tell which one a call
    reaches. Each record says whether it is a method, so that a bare-name call
    and an attribute call reach only the kind they can (``_http_exception_view``).

    Same module only, and one level: a factory whose ``HTTPException`` comes
    from calling another factory is not one here — both are stated limits in
    the module docstring. When this was first written the response-producing
    surface held two returning factories: ``exception_handlers.py::_llm_http``
    and ``operator_user_scope.py::user_not_found``. Counting every
    construction, 35 functions there qualify: most are route handlers whose
    own 404 names a path parameter. FastAPI calls those, and the module calls
    them by bare name nowhere; a same-named ``service.update_report(...)`` is
    an attribute call and does not reach a module-level function.

    A function's assignment map is built only once it is known to construct
    an ``HTTPException``. Most functions never do, and the map walks the
    whole function.
    """
    factories: dict[str, list[_Factory]] = {}
    methods = {
        id(item)
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for item in cls.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = fn.args
        positional = [p.arg for p in [*args.posonlyargs, *args.args]]
        params = positional + [p.arg for p in args.kwonlyargs]
        assigns: dict[str, list[ast.AST]] | None = None
        statuses: list[ast.AST | None] = []
        carried_params: set[str] = set()
        for node in _own_nodes(fn):
            if not isinstance(node, ast.Call) or not _is_http_exception(node):
                continue
            carried = _carried_expr(node)
            if carried is None:
                continue
            if assigns is None:
                assigns = _local_assignments(fn)
            reached = {p for p in params if _carries_exception(carried, p, assigns)}
            if reached:
                carried_params |= reached
                statuses.append(_status_expr(node))
        if carried_params:
            factories.setdefault(fn.name, []).append(
                _Factory(
                    positional,
                    params,
                    _param_defaults(args),
                    tuple(statuses),
                    frozenset(carried_params),
                    id(fn) in methods,
                    _rebound_names(fn),
                )
            )
    return factories


def _http_exception_view(
    call: ast.AST | None, factories: dict[str, list[_Factory]]
) -> tuple[tuple[ast.AST | None, ...], ast.AST | None] | None:
    """``(statuses, carried)`` if ``call`` builds an ``HTTPException``, else ``None``.

    The one question every ``HTTPException`` pass asks, of every node it
    walks, so that a factory call is seen by all of them at once rather than
    by whichever pass remembered it — and seen wherever it stands, not only
    under a ``raise`` or ``return``.

    * A direct ``HTTPException(...)`` answers its own ``status_code``, and its
      ``detail`` and ``headers`` (``_carried_expr``).
    * A call to a factory answers the CALLER's arguments: ``carried`` is the
      arguments bound to the parameters the factory carries onto the wire (a
      tuple of them), and ``statuses`` is each of the factory's statuses — the
      argument bound to its status parameter, else the factory's own
      expression when it is not a parameter (``_f(msg)`` building a literal
      500). A status parameter the call leaves unbound takes its default only
      when the factory's body never rebinds it and the call spreads nothing
      (``*args``, ``**kwargs``); otherwise, or with no default, it is ``None``,
      unknown, and ``_status_is_5xx`` fails closed on it. Several same-named
      factories contribute all of their statuses and arguments.

    A bare-name call (``_fail(...)``) resolves only to a module-level or nested
    function, and an attribute call (``self._err(...)``, ``A()._err(...)``)
    only to a method defined in a class body. Matching on the name alone made
    every route handler whose own 404 names a path parameter a factory for any
    same-named service or repository call — #1634's review counted 28–50 such
    call sites on the surface. A same-module FUNCTION reached through an
    attribute (``this_module._fail(...)``) is therefore not seen, under the
    same stated limit as a factory in another module. A method reached
    through ``self.``/``cls.`` binds its receiver implicitly, so positional
    binding starts after it.
    """
    if not isinstance(call, ast.Call):
        return None
    if _is_http_exception(call):
        return (_status_expr(call),), _carried_expr(call)
    # A bare name reaches a function and an attribute reaches a method. Keyed
    # by name alone, `case_repository.update_report(failed)` read as a call to
    # the module's route handler `update_report`, whose own 404 names a path
    # parameter.
    by_attribute = isinstance(call.func, ast.Attribute)
    records = [
        record
        for record in factories.get(_terminal_name(call.func), ())
        if record.is_method == by_attribute
    ]
    if not records:
        return None
    # An argument spread with `*` or `**` can bind any parameter, the status
    # included, so no default is trusted for this call.
    spread = any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        kw.arg is None for kw in call.keywords
    )
    statuses: list[ast.AST | None] = []
    carried: list[ast.AST] = []
    for factory in records:
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
        carried.extend(bound[p] for p in factory.carried_params if p in bound)
        for status in factory.statuses:
            if isinstance(status, ast.Name) and status.id in factory.params:
                # Bound by the call; else the default, when nothing can have
                # replaced it; else unknown, which fails closed as a 5xx.
                if status.id in bound:
                    status = bound[status.id]
                elif spread or status.id in factory.rebound:
                    status = None
                else:
                    status = factory.defaults.get(status.id)
            statuses.append(status)
    return tuple(statuses), (
        ast.Tuple(elts=carried, ctx=ast.Load()) if carried else None
    )


def _gated(statuses: tuple[ast.AST | None, ...], broad: bool) -> bool:
    """The status rule, in one place: does a site with these statuses count?

    Any status, when the taint came from a broad source (#1598); otherwise any
    reachable 5xx — a factory with a 400 arm and a 500 arm is a 500 for the
    caller that reaches the second. Every pass asks this rather than spelling
    the rule again; four copies of it were how one pass could drift from the
    others.
    """
    return broad or any(_status_is_5xx(status) for status in statuses)


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
    single richest leak on the list, since it carries the whole stack. A
    handler that DOES bind a name can call them just as well, so the
    in-handler ``HTTPException`` pass and the returned-body renderer walk ask
    this of bound handlers too.

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
    one (``_http_exception_view``), and "carrying" covers its ``detail`` and
    its ``headers``, in every pass below. Every such call is a sink wherever
    it stands — raised, returned, assigned, awaited or called as a statement —
    and is reported at the call's line; the surface guard's ``_offender_keys``
    maps that line to the statement holding it.

    Four shapes, the first three measured against #1400's mutation matrix and
    the fourth against #1634's:

    * the construction is inside a handler and carries ``e``, directly or
      through a local alias — or renders the live exception
      (``traceback.format_exc()``), which a handler binding ``e`` can do as
      easily as one binding nothing;
    * the handler stashes the text in a local and the construction happens
      **after** it, back in the enclosing function — the exact twin of the
      shape ``returned_body_leak_sites`` has covered since #1394, and the
      reason this module's docstring stopped saying it was uncovered;
    * the construction renders the live exception under a handler that binds
      nothing, so there is no ``as <name>`` to follow;
    * the exception arrives as a PARAMETER, and the function builds an
      ``HTTPException`` carrying it — the helper twin of the first shape.
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
            view = _http_exception_view(node, factories)
            if view is None:
                continue
            statuses, carried = view
            if carried is None or not _gated(statuses, broad):
                continue
            # `format_exc()` is checked here too, not only under a handler
            # that binds nothing: binding `e` does not stop the stack being
            # rendered, and #1634's review measured `log(e)` followed by
            # `detail=traceback.format_exc()` passing.
            if _carries_exception(
                carried, handler.name, assigns
            ) or _renders_the_live_exception(carried):
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
    factories: dict[str, list[_Factory]],
) -> list[str]:
    """Constructions OUTSIDE a handler that carry a local it tainted.

        except Exception as e:
            message = f"...: {e}"
        raise HTTPException(status_code=500, detail=message)

    Gated like the in-handler pass: 5xx, or any status when a broad handler
    tainted the local (``_tainted_names`` records which). One alias out of the
    handler, not a chain — a two-hop stash is a stated limit (#1858).

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
            view = _http_exception_view(node, factories)
            if view is None or view[1] is None:
                continue
            statuses, carried = view
            if any(
                name in tainted
                and node.lineno > tainted[name].line
                and _gated(statuses, tainted[name].broad)
                for name in _names_in(carried)
            ):
                offenders.append(f"{filename}:{node.lineno}")
    return offenders


def _unbound_render_leak_sites(
    tree: ast.AST, filename: str, factories: dict[str, list[_Factory]]
) -> list[str]:
    """Constructions rendering the live exception under a handler with no
    ``as <name>``.

    Scoped to handlers that bind NOTHING, because the in-handler pass walks
    only the handlers that bind a name; it checks the renderer there too, so a
    handler that binds ``e`` and puts ``traceback.format_exc()`` on the wire is
    reported once, by that pass.

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
            view = _http_exception_view(node, factories)
            if view is None or view[1] is None:
                continue
            statuses, carried = view
            if _gated(statuses, broad) and _renders_the_live_exception(carried):
                offenders.append(f"{filename}:{node.lineno}")
    return offenders


def _exception_parameter_leak_sites(
    tree: ast.AST, filename: str, factories: dict[str, list[_Factory]]
) -> list[str]:
    """Constructions whose ``HTTPException`` carries an exception PARAMETER.

    The other three passes start from an ``except ... as`` binding, so a
    helper that receives the exception — ``def h(exc: BaseException) ->
    HTTPException`` — has nothing for them to start from. That is the upward
    half of #1634: ``llm_service_error_http_exception(exc)`` put
    ``str(exc)[:200]`` into ``_llm_http``'s ``detail``, and the file was
    reported clean while ``/turns`` echoed internal text.

    Reported at the helper's own construction, which is where the text is put
    on the wire. When the helper spells ``HTTPException(...)`` itself, that is
    not the only finding: a helper whose ``HTTPException`` carries its
    exception parameter is, by the same token, a factory, so every same-module
    caller that hands it an exception (by bare name, or through ``self.`` for
    a method) is reported by the handler passes as well, and fixing the helper
    clears all of them. A helper that builds through ANOTHER factory —
    ``llm_service_error_http_exception`` returns ``_llm_http(...)`` — is not a
    factory itself, because the view is one level deep, so it is reported
    alone and its callers are not.

    Which parameters count, and which are broad, is ``_exception_params``, over
    the package's exception classes and this module's own. The status gate is
    ``_gated`` with ``broad`` from the annotation. The whole function is the
    alias scope (``text = str(exc)`` and then ``detail=text``, or ``for c in
    walk_cause_chain(exc)`` and then ``str(c)``), as the whole handler is for
    an ``as`` name.
    """
    offenders: list[str] = []
    exception_classes = _module_exception_classes(tree)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = _exception_params(fn, exception_classes)
        if not params:
            continue
        assigns = _local_assignments(fn)
        for node in _own_nodes(fn):
            view = _http_exception_view(node, factories)
            if view is None or view[1] is None:
                continue
            statuses, carried = view
            if any(
                _gated(statuses, broad) and _carries_exception(carried, name, assigns)
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

    A third shape is covered here since #1400: a handler that returns
    ``traceback.format_exc()``. It carries the whole stack rather than one
    message, and the name-following of the two shapes above cannot see it.
    Any handler, not only one that binds nothing: binding ``e`` and returning
    the formatted stack leaks it all the same, and #1634's review measured the
    narrower walk missing exactly that.

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

    # Third shape: a handler -- bound or not -- returning the live exception's
    # rendering. Its own walk, because the loop above starts from handlers
    # that bind a name and follows that name, which `format_exc()` never uses.
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
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
