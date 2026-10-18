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
widen to it (2026-09-30). A *typed* source at 4xx stays exempt wherever the
site stands, because the carve-out is about the SOURCE's type: a typed arm
nested inside a broad handler, or a typed parameter raised under one, keeps
it. #1858 first lifted the gate for any site inside a broad handler, and its
review measured that flagging exactly those typed messages, so it was
reversed.

**Where the leak is built.** Not only at a ``raise HTTPException(...)`` written
inside the function that caught the exception. #1634's leak crossed a function
boundary in both directions, and the analysis reported its file clean:

* **Down, into a factory.** ``api/exception_handlers.py``'s ``_llm_http``
  builds the ``HTTPException`` from its ``detail`` parameter, so no caller ever
  spells ``HTTPException`` at all. A same-module function — a ``def``, or a
  ``lambda`` bound to a name — whose own ``HTTPException(...)`` (returned,
  raised or assigned) carries one of its parameters onto the wire is a
  factory, and a call to it is read as the construction it performs — see
  ``_http_exception_factories``. That includes
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
  reported alone. See ``_scope_taints``.

Every call the factory view recognises is a sink, wherever it stands: raised,
returned, assigned and raised later, awaited, or called for its side effect.
Each is reported at the line of the call. So is a ``detail`` or ``headers``
written onto an ``HTTPException`` after it exists (``err.detail = str(e)``),
at the line of the write: one built here or by the function a closure sits
in, or one caught (``except HTTPException as he``) or taken as a parameter.
An ``HTTPException`` received that way, and as nothing else, is no source of
text: its ``detail`` and ``headers`` were written FOR the caller, so writing
them back — ``he.headers = he.headers or {}``, then a correlation id, which
the live ``modules/case/api/routes/cases.py`` does — adds nothing to the
wire.

**How the text travels: one taint analysis per scope.** Until #1858 this was
five passes — inside a handler, after it, under a handler binding nothing,
from an exception parameter, and the returned-body walk — each with its own
scope and its own alias rule, and taint went one hop out of a handler. Every
shape #1858 lists slipped between two of them: ``err = str(e)`` in the
handler and ``message = f"...{err}"`` after it was two hops, so the raise of
``message`` was clean. Now each scope — the module, every ``def``, every
``lambda`` — is analysed once, over its *definitions*, to a fixed point
(``_ScopeTaint``):

* the sources are an ``except ... as e`` name inside its handler, an exception
  parameter (for the ``HTTPException`` sinks only — see the stated limits), a
  rendering of the live exception inside a handler, and, in a closure, a
  tainted name it reads from the function that defines it or a parameter
  default that function evaluates (``def fail(m=msg)``). Not an ``as`` name
  of the MODULE, which Python deletes when its handler ends, and not an
  ``HTTPException`` caught or passed in as nothing else;
* a definition is tainted when its value carries a source, or a name with a
  tainted definition reaching it — through any number of hops. Reaching is by
  position, so a binding earlier on the same line counts
  (``msg = str(e); raise ...``), but an assignment is not seen inside its own
  statement, whose value runs first;
* a binding that rebinds exactly that name on every path to a read stops the
  earlier definitions reaching it, and the definitions of every dotted name
  rooted at it (``state = make()`` ends ``state.error``) unless it is built
  from that object. One that binds only in a place — an ``as`` name in its
  handler, a comprehension target in its comprehension, a ``match`` capture
  in its ``case``, a ``for`` or ``with`` target in the body it heads — stops
  them only there. A write *into* an object never kills. A logging call on a
  logger (``logger.warning(f"{e}")``) is no write at all, nor is a call on an
  imported module (``sys.stderr.write(...)``), and a call through a
  parameter's attribute (``request.state.errors.append(...)``) writes into
  the attribute, not the parameter. Nothing is a definition of ``self`` or
  ``cls``.

**Stated limits.** These shapes are outside this analysis on purpose, written
down so the next widening starts from a measurement rather than re-deriving
one:

* a factory that builds its ``HTTPException`` by calling another factory — the
  factory view is one level deep. Iterating it to a fixed point added zero
  findings (#1634);
* a factory defined in a different module from the call, and a same-module
  function reached through an attribute (``this_module._fail(...)``), which
  reads as a method call. Treating every factory in the package as visible
  from every file added zero findings (#1634);
* a factory the view cannot bind or reach: ``*args``/``**kwargs`` parameters,
  ``**{...}`` at the call and ``HTTPException(**kw)``; a factory reached
  through a local alias or a dispatch dict; ``ClassName._err(self, msg)``.
  Zero live sites in ``faultmaven/`` when #1858 recorded them;
* a conditional status not spelled ``x if c else y`` — ``e.status_code or
  400``, ``_MAP.get(kind, 400)``, ``max(400, e.code)`` — is read by the 4xx it
  names. Zero live sites;
* annotation forms the reader cannot parse: ``Annotated[Exception, ...]``, a
  bound ``TypeVar``, a union alias, ``*errors: Exception``, a third-party
  class not named ``*Error``/``*Exception``, an import-aliased base. Zero live
  sites;
* an unannotated exception parameter named outside
  ``_UNANNOTATED_EXCEPTION_PARAMS`` (``failure``, ...) is not a source. Zero
  live sites on the surface;
* a cause-chain alias under a *typed* handler (``for c in
  walk_cause_chain(e)`` under ``except LLMException as e``) keeps the handler's
  4xx carve-out, although a link of the chain may be any exception — the same
  question #1860 ruled on for a generic wrapper, below. Zero live sites;
* a helper that *returns* a ``JSONResponse`` (or any body other than an
  ``HTTPException``) carrying an exception parameter —
  ``returned_body_leak_sites`` does not start from parameters. No helper
  with a *broad* exception parameter returned one; the typed ones that do are
  the domain handlers in ``api/exception_handlers.py``, which are shape O's
  sinks below, and the ``HTTPException`` handler itself, which renders a
  ``detail`` already built;
* a custom typed exception carrying the text into one of the domain handlers
  that render ``str(exc)`` (#1598's shape O). Closed, not guarded, by the #1598
  ruling: its entire live population was five legitimate sites, and a guard
  over it would be an allowlist and nothing else;
* the reaching rule's own limit: position stands in for reachability, and
  only an exact rebinding kills. There is no control-flow graph. In the
  direction that misses: a value carried round a loop to a line *above* the
  one that binds it (``if last_error: raise ...`` heading a retry loop), and
  likewise a closure's default read on a later turn (``def fail(m=msg)``
  defined in a loop before the handler that binds ``msg``); a root swapped
  through an alias (``self.state, other = other, self.state``, then
  ``other.error``); and, inside a closure, a ``status_code`` the enclosing
  function overwrote, so a write onto that exception is gated by the status
  it was built with. In the direction that reports: a rebinding inside a
  nested block, a conditional one or one in a ``finally``, and a write into
  an object, leave the earlier definition reaching; a dotted name is matched
  by its spelling, so ``self.last_error is not None`` carries it although a
  ``Compare`` cannot; and a closure sees every tainted definition of a name
  it reads, wherever it is called from. Taint flows INTO a nested scope only:
  what a closure returns (``detail=render()``), writes into an enclosing
  object, or binds through ``nonlocal`` is not followed back out. Zero live
  sites for each shape #1858's second review recorded here. Measured when
  this replaced the five passes (#1858, with its review's two revisions):
  zero findings gained or lost on the surface, and over the whole package
  three genuine returns gained and two false positives lost;
* a caught ``HTTPException``'s ``__cause__`` and ``args``: as no source of
  text, ``he.detail = f"{he.__cause__}"`` and ``he.detail = str(he.args)``
  are not reported. ``main`` misses both as well;
* the shapes #1858's review declined (2026-10-18), each with zero live
  sites by its defeat pass's scans of ``faultmaven/``, the
  response-producing surface included. Writes not followed: through an
  alias of the exception (``h2 = err; h2.detail = ...``),
  ``setattr(err, "detail", ...)``, onto an exception built inside an
  ``IfExp``, into a container aliased into a ``detail``, a header or a list
  before it is written, through a call-valued receiver
  (``d.setdefault(k, []).append(...)``, ``d.get(k).append(...)``,
  ``getattr(o, "x").append(...)``), two attributes deep and returned through
  the parent (``self.state.last_error``, then ``return {"state":
  self.state}``), and by a closure from its own parameter (``def fill(msg):
  err.detail = msg``). Reads not followed: ``getattr(self, "last_error")``,
  ``vars(self)``, ``self.model_dump()``. Factories not recognised:
  ``functools.partial(HTTPException, 500)``, a ``staticmethod`` lambda, and a
  lambda bound by a walrus, by tuple unpacking or in a chained assignment.
  Sources not recognised: ``sys.exception()``,
  ``traceback.print_exc(file=buf)``, and a tuple alias of ``Exception``
  (``except BROAD as e``), read as typed; a typed handler rendering
  ``format_exc()`` at 4xx keeps the carve-out, as its ``e`` would. Sinks not
  recognised: an ``HTTPException`` subclass or import alias, a streaming
  body, an ASGI or websocket ``send``, and a ``lambda`` whose body returns a
  response.

And one is ruled out of the guard's reach: "broad" is decided by class NAME
(``Exception`` / ``BaseException``), so a generic wrapper —
``ServiceException``, ``RuntimeError`` — keeps the typed 4xx carve-out although
it carries whatever it wrapped. The #1860 ruling (b), 2026-10-02, keeps it so:
a generic wrapper caught under a typed arm is reviewed at the site, not flagged
by the guard.
"""

from __future__ import annotations

import ast
import builtins
import functools
import pathlib
import re
from collections.abc import Iterator
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


def _bindings(node: ast.AST) -> Iterator[tuple[str, ast.AST, bool]]:
    """``(name, value, exact)`` for every binding ONE node makes.

    One node, not a subtree, so that the taint analysis can place each binding
    at its own statement and a compound statement's body is never bound twice;
    ``_local_assignments`` walks a scope and asks this of every node.

    Covers ``x = ...``, ``x: T = ...``, ``x += ...`` and ``(x := ...)``. The
    last two bind just as effectively as the first, so leaving them out would
    give the alias-following two silent blind spots. So do a ``for`` (or
    comprehension) target, bound to what it iterates, and a ``with ... as``
    target, bound to the context expression: #1634's review measured ``for c in
    walk_cause_chain(exc)`` breaking the taint in the very helper #1634 is
    about. And a ``match`` capture, bound to the subject it is a piece of:
    ``case {"m": text}`` takes ``text`` out of whatever was matched (#1858).

    It also covers writes *through* a name -- ``d["k"] = ...``, ``d.attr = ...``
    -- keyed on the container (``_written_name``) and on the object the write
    lands in (``_object_root``). Restricting this to ``ast.Name`` targets was a
    real blind spot, not a theoretical one::

        except Exception as e:
            cleanup_results["resource_cleanup"] = {"error": str(e)}
        ...
        return cleanup_results

    ``main.py`` had that exact shape, and this analysis reported the file clean
    while CodeQL's ``py/stack-trace-exposure`` flagged it correctly.

    ``exact`` says the binding REPLACES the name, which is what lets it stop an
    earlier definition reaching past it (``_ScopeTaint``). Only a target that
    is that very name is: a ``Name``, or the dotted spelling ``self.a`` for
    ``self.a = ...``. A write into a container, an attribute write keyed on its
    object, a method-call mutation and ``x += ...`` all keep what was there,
    so none of them is exact — #1858's first kill rule let
    ``resp["more"] = "static"`` kill the ``resp`` a handler had filled, and
    lost six true findings package-wide. Nor is a comprehension target, which
    binds in the comprehension's own scope and leaves the outer name as it
    was, or a ``match`` capture, which binds only when its case matches: each
    replaces the name only in that place (``_ScopeTaint._kills_before``).
    """
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            yield from _target_bindings(target, node.value, exact=True, pair=True)
    elif isinstance(node, ast.NamedExpr):
        yield from _target_bindings(node.target, node.value, exact=True)
    elif isinstance(node, ast.AugAssign):
        yield from _target_bindings(node.target, node.value, exact=False)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        # A loop target is an alias of what it iterates. `for c in
        # walk_cause_chain(exc)` is this codebase's own idiom for reading
        # an exception's cause chain -- `api/exception_handlers.py` has
        # two such `for` statements and four comprehensions -- and
        # `str(c)` carries the text of a link of `exc` exactly as
        # `str(exc)` carries its own.
        exact = not isinstance(node, ast.comprehension)
        yield from _target_bindings(node.target, node.iter, exact=exact)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        # `with ctx(e) as msg:` binds `msg` from the context expression.
        for item in node.items:
            if item.optional_vars is not None:
                yield from _target_bindings(
                    item.optional_vars, item.context_expr, exact=True
                )
    elif isinstance(node, ast.Match):
        for case in node.cases:
            for captured in _captures(case.pattern):
                yield captured, node.subject, False
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        # A container MUTATED by method call is bound just as effectively
        # as one assigned into: `out.append({"error": str(e)})` and
        # `res.update(...)` are the natural siblings of the
        # `cleanup_results["k"] = ...` shape this analysis already covers,
        # and both reported clean. Any method is accepted rather than a
        # list of append/update/extend/setdefault -- the receiver is what
        # gets returned either way, and guessing which names mutate is how
        # the next spelling slips through. The one exception is a logging
        # method called on a logger (`_LOGGING_METHODS`, `_is_logger`). Like
        # an attribute write, the mutation also lands in the object the
        # receiver hangs off: `resp.headers.update({"x": str(e)})` puts the
        # text in `resp`.
        if node.func.attr in _LOGGING_METHODS and _is_logger(node.func.value):
            return
        receiver = _written_name(node.func.value)
        root = _object_root(node.func.value)
        for value in [*node.args, *(kw.value for kw in node.keywords)]:
            if receiver:
                yield receiver, value, False
            if root and root != receiver:
                yield root, value, False


#: Methods that hand their arguments to a log record rather than store them in
#: the receiver — when the receiver is a logger (``_is_logger``). Read as
#: writes, ``logger.warning(f"missing {e}")`` in a module-level import guard
#: tainted ``logger``, and every function reading ``logger.name`` or
#: ``logger.level`` after it reported a leak (#1858's review).
_LOGGING_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)

#: A logger's spelling: ``logger``, ``log``, ``self._log``, ``LOGGER``,
#: ``audit_logger``. Matched on the receiver's terminal name.
_LOGGER_NAME = re.compile(r"(^|_)log(ger)?$", re.IGNORECASE)


def _is_logger(receiver: ast.AST) -> bool:
    """Is ``receiver`` a logger, whose logging methods store nothing in it?

    Spelled like one (``_LOGGER_NAME``), or a ``getLogger(...)`` /
    ``get_logger(...)`` call. Matching the method name alone stopped
    following an accumulator: ``report.error(f"failed: {e}")`` and
    ``validation.log("check", str(e))`` put the text in an object that is
    then returned, and #1858's review measured each of them missed.
    """
    if isinstance(receiver, ast.Call):
        return _terminal_name(receiver.func) in {"getLogger", "get_logger"}
    name = _terminal_name(receiver)
    return bool(name and _LOGGER_NAME.search(name))


def _captures(pattern: ast.AST) -> list[str]:
    """Every name a ``match`` pattern captures: ``x``, ``[*x]``, ``{**x}``."""
    names = []
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)):
            captured = node.name
        else:
            captured = getattr(node, "rest", None)  # `{**rest}`
        if captured:
            names.append(captured)
    return names


def _target_bindings(
    target: ast.AST, value: ast.AST | None, exact: bool, pair: bool = False
) -> Iterator[tuple[str, ast.AST, bool]]:
    """The bindings one assignment target makes from ``value`` (``_bindings``).

    ``pair`` is for an assignment, whose literal tuple value lines up with its
    target element by element. A ``for`` or comprehension target unpacks each
    ITEM of what it iterates, not the item at its own position: ``for label,
    msg in (("db", str(e)), ("cache", "ok"))`` binds ``msg`` to ``str(e)`` on
    the first turn, and pairing it with the second item read it as ``"ok"``
    — a miss #1858's review measured, on a shape with five live sites.
    """
    if value is None:
        return
    if isinstance(target, ast.Starred):
        # `first, *rest = str(e).split()`: `rest` holds the text as surely
        # as `first` does. A starred target used to bind nothing.
        yield from _target_bindings(target.value, value, exact, pair)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        # `msg, code = str(e), 500` binds each element. Without this the
        # whole statement was skipped and `msg` looked clean. Paired element
        # to element when assigning a literal tuple of the same length with
        # nothing starred, so `code` is bound to `500` alone; otherwise every
        # element is bound to the whole value.
        paired = (
            pair
            and isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(target.elts)
            and not any(isinstance(n, ast.Starred) for n in [*target.elts, *value.elts])
        )
        for index, element in enumerate(target.elts):
            part = value.elts[index] if paired else value
            yield from _target_bindings(element, part, exact, paired)
        return
    name = _written_name(target)
    if name:
        yield name, value, exact and not isinstance(target, ast.Subscript)
    # ...and, for an attribute write, the object it lands in. Two keys for
    # one write, because the return side may spell it either way. See
    # ``_object_root``.
    root = _object_root(target) if not isinstance(target, ast.Name) else None
    if root and root != name:
        yield root, value, False


def _local_assignments(scope: ast.AST) -> dict[str, list[ast.AST]]:
    """Every binding made anywhere inside ``scope``, keyed by the name written.

    The alias map a factory's ``detail`` is traced back through to the
    parameter it carries (``_http_exception_factories``). It is flat — every
    binding of a name, wherever it stands — which is right for that question:
    a factory either can put the parameter on the wire or it cannot. Where the
    ORDER of bindings matters, the taint analysis reads ``_bindings`` itself.
    """
    assigns: dict[str, list[ast.AST]] = {}
    for node in ast.walk(scope):
        for name, value, _ in _bindings(node):
            assigns.setdefault(name, []).append(value)
    return assigns


def _carries_exception(
    expr: ast.AST,
    exc_name: str,
    assigns: dict[str, list[ast.AST]],
    _depth: int = 0,
) -> bool:
    """Does ``expr`` carry the parameter named ``exc_name``, directly or via a
    local alias?

    The factory view's question (``_factory_of``): CAN this parameter reach
    the ``HTTPException`` the function builds. Flat and order-blind over
    ``_local_assignments``, which is right for "can"; where the order of
    bindings decides what reaches a sink, the taint analysis (``_ScopeTaint``)
    answers instead.
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
    whole rule, and every sink asks it.
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
    A generic wrapper such as ``ServiceException`` does not count as broad:
    the #1860 ruling (b) keeps "broad" to those two names, and an arm catching
    such a wrapper is reviewed at its site, not flagged here.
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
    the function's own body can rebind (``_rebound_names``): a status the body
    may overwrite is neither its default nor the argument a call bound.
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
    """Every name ``fn``'s own body can rebind, nested functions excluded.

    A status parameter in this set is not reliably what it was on entry —
    neither its default nor the value a call bound to it::

        def _err(msg, status_code, internal=False):
            if internal:
                status_code = 500
            return HTTPException(status_code=status_code, detail=msg)

    ``_err(str(e), 404, internal=True)`` reads as a 404 by its argument and
    builds a 500. #1634's review measured both readings — trusting the
    default, and trusting the call's argument — exempting such a factory
    under a typed handler.

    Every statement that binds a name counts, because any of them replaces
    the parameter: ``x = ...``, ``x: T = ...``, ``x += ...``, ``(x := ...)``,
    a ``for`` target, a ``with ... as`` target, an ``except ... as`` name, an
    ``import`` (its ``as`` name, or its first component), ``global`` and
    ``nonlocal``, a ``match`` capture (``case {"status": x}``, ``case [*x]``,
    ``case {**x}``), and ``del``. Through any tuple or starred unpacking. A
    comprehension target is NOT one: it binds in the comprehension's own
    scope, so ``[status_code for status_code in codes]`` leaves the parameter
    as it was.
    """
    names: set[str] = set()

    def add(target: ast.AST | None) -> None:
        if target is not None:
            names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))

    for node in _own_nodes(fn):
        if isinstance(node, (ast.Assign, ast.Delete)):
            for target in node.targets:
                add(target)
        elif isinstance(
            node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)
        ):
            add(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                add(item.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(
                (alias.asname or alias.name).split(".")[0] for alias in node.names
            )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return frozenset(names)


def _http_exception_factories(tree: ast.AST) -> dict[str, list[_Factory]]:
    """Every function in ``tree`` that builds an ``HTTPException`` from a parameter.

    A factory is a function whose own ``HTTPException(...)`` — returned,
    raised or assigned, wherever in the function it is built — carries one of
    its parameters into ``detail`` or ``headers``, directly or through a local
    alias (``_carries_exception``). ``api/exception_handlers.py``'s
    ``_llm_http(status_code, error_code, detail, ...)`` is the shape: its
    callers never spell ``HTTPException``, so without this a ``raise
    _llm_http(500, ..., str(e), ...)`` was invisible to the analysis. #1634's
    leak went through it.

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

    A ``lambda`` bound to a name is a factory on the same terms (#1858):
    ``mk = lambda m: HTTPException(500, detail=m)`` is ``def mk(m)`` spelled
    on one line, and ``raise mk(str(e))`` was invisible while only a ``def``
    could be one. It is keyed by the name it is bound to, and it is a method
    when that binding sits directly in a class body. Its body can rebind a
    parameter only with a walrus (``lambda s: HTTPException((s := 500), ...)``
    is valid Python), which ``_rebound_names`` reads as it does for a ``def``.
    A lambda reached any other way — a default argument, a dispatch-dict value
    — has no name a call could use, which is the dispatch-dict stated limit.
    """
    factories: dict[str, list[_Factory]] = {}
    in_class_body = {
        id(item)
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for item in cls.body
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name, fn = node.name, node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, ast.Lambda
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            name, fn = targets[0].id, node.value
        else:
            continue
        factory = _factory_of(fn, is_method=id(node) in in_class_body)
        if factory is not None:
            factories.setdefault(name, []).append(factory)
    return factories


def _factory_of(fn: ast.AST, is_method: bool) -> _Factory | None:
    """What a call to ``fn`` builds, or ``None`` when it is not a factory.

    ``fn`` is a ``def`` or a ``lambda``; see ``_http_exception_factories``.
    Its assignment map is built only once it is known to construct an
    ``HTTPException``. Most functions never do, and the map walks the whole
    function.
    """
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
    if not carried_params:
        return None
    return _Factory(
        positional,
        params,
        _param_defaults(args),
        tuple(statuses),
        frozenset(carried_params),
        is_method,
        _rebound_names(fn),
    )


def _http_exception_view(
    call: ast.AST | None, factories: dict[str, list[_Factory]]
) -> tuple[tuple[ast.AST | None, ...], ast.AST | None] | None:
    """``(statuses, carried)`` if ``call`` builds an ``HTTPException``, else ``None``.

    The one question the analysis asks of every node it walks, and of the
    value a name was bound to before a ``detail`` or ``headers`` is written
    onto it — so a factory call is seen wherever it stands, not only under a
    ``raise`` or ``return``, and by every sink at once.

    * A direct ``HTTPException(...)`` answers its own ``status_code``, and its
      ``detail`` and ``headers`` (``_carried_expr``).
    * A call to a factory answers the CALLER's arguments: ``carried`` is the
      arguments bound to the parameters the factory carries onto the wire (a
      tuple of them), and ``statuses`` is each of the factory's statuses — the
      argument bound to its status parameter, else the factory's own
      expression when it is not a parameter (``_f(msg)`` building a literal
      500). A status parameter the factory's body can rebind
      (``_rebound_names``) is ``None``, unknown, whatever the call bound to
      it. One the call leaves unbound takes its default only when the call
      spreads nothing (``*args``, ``**kwargs``); otherwise, or with no
      default, it is unknown too, and ``_status_is_5xx`` fails closed on it.
      Several same-named factories contribute all of their statuses and
      arguments.

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
                # A parameter the body can rebind is unknown whatever the
                # call bound to it. Otherwise: the call's argument; else
                # unknown when the call spreads `*`/`**`; else the default.
                # Unknown fails closed as a 5xx.
                if status.id in factory.rebound:
                    status = None
                elif status.id in bound:
                    status = bound[status.id]
                elif spread:
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
    caller that reaches the second. ``broad`` is the source's, never the
    site's: a typed message raised at 400 inside an enclosing broad handler
    keeps the carve-out. Every sink asks this rather than spelling the rule
    again; four copies of it were how one pass could drift from the others.
    """
    return broad or any(_status_is_5xx(status) for status in statuses)


def _parse(path: pathlib.Path) -> ast.AST:
    """One tree per call of a public entry point.

    Load-bearing, not tidiness: the analysis keys its parent map, its
    definitions and its caches on ``id()``, which is only comparable across
    nodes from the SAME parse. Re-parsing between two passes once made every
    node-identity lookup miss, which reported two static-message 500 arms in
    ``modules/case/api/routes/`` as leaks — a false positive produced by the
    guard's plumbing rather than by its rule.
    """
    return ast.parse(path.read_text(encoding="utf-8"))


def _renders_the_live_exception(expr: ast.AST) -> bool:
    """Does ``expr`` render the in-flight exception WITHOUT naming it?

    ``traceback.format_exc()`` and ``sys.exc_info()`` read the exception the
    interpreter is currently handling, so a handler that uses them needs no
    ``as <name>`` — and the name-following analysis, which starts from that
    name, cannot see them at all. #1400 measured ``detail=traceback
    .format_exc()`` under a bare ``except Exception:`` as MISSED; it is the
    single richest leak on the list, since it carries the whole stack. A
    handler that DOES bind a name can call them just as well, so a rendering
    inside ANY handler is a source (``_ScopeTaint``), and so is a local it
    was stashed in first (``tb = traceback.format_exc()``, #1858).

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
    its ``headers``, whether passed when it is built or written onto it
    afterwards (``_wire_writes``). Every such call is a sink wherever it stands
    — raised, returned, assigned, awaited or called as a statement — and is
    reported at the call's line, and a write at the write's; the surface
    guard's ``_offender_keys`` maps that line to the statement holding it.

    The text arrives through any chain of local bindings in one scope, or in a
    closure it defines (``_scope_taints``), from an ``except ... as`` name, a
    rendering of the live exception (``traceback.format_exc()``) under any
    handler, or an exception PARAMETER. The last is the helper twin of the
    first: ``llm_service_error_http_exception(exc)`` put ``str(exc)[:200]``
    into ``_llm_http``'s ``detail``, and #1634's file was reported clean
    while ``/turns`` echoed internal text.

    Sorted, and de-duplicated: one site two sources reach is one finding, and
    reporting it twice reads as a bug in the guard rather than as two.
    """
    tree = _parse(path)
    factories = _http_exception_factories(tree)
    lines: set[int] = set()
    for scope in _scope_taints(tree, parameters=True, factories=factories):
        for node in scope.nodes:
            for statuses, carried in _wire_writes(node, scope, factories):
                broad = scope.carried(carried, at=node)
                if broad is not None and _gated(statuses, broad):
                    lines.add(node.lineno)
    return [f"{path.name}:{line}" for line in sorted(lines)]


def returned_body_leak_sites(path: pathlib.Path) -> list[str]:
    """``file:line`` for every ``return`` carrying a caught exception's text.

    The ``HTTPException`` guard structurally cannot see these: a handler that
    degrades to a 200 body — ``GET /auth/health`` did — leaks just as much.
    A body has no status gate to lift, so every one is reported.

    The same analysis as ``http_exception_leak_sites`` (``_scope_taints``), so
    a return is reported wherever the text reaches it: in the handler, or
    after it through any chain of locals::

          try:
              sla_details = sla_tracker.get_component_sla_details(name)
          except Exception as e:
              sla_details = {"error": str(e)}
          return {"sla": sla_details}

    Scoping the walk to the handler missed every site of that shape —
    ``main.py`` had six, all of which CodeQL's ``py/stack-trace-exposure``
    flagged while this analysis reported the file clean — and following one
    alias out of the handler missed every later hop: ``storage_errors.append(
    f"...{str(e)}")`` reaching the returned response through ``debug_info``
    was found twice package-wide only once the chain was followed, and
    ``result.failed.append((relpath, f"{exc}"))`` reaching ``return result``
    once a mutation through an attribute was keyed on its object too (#1858).
    A handler returning ``traceback.format_exc()`` is covered the same way
    (#1400), whether or not it binds a name: logging ``e`` and returning the
    formatted stack leaks it all the same.

    Not from exception PARAMETERS — a stated limit, and the typed domain
    handlers in ``api/exception_handlers.py`` are why: #1598 ruled them
    closed, not guarded.
    """
    tree = _parse(path)
    lines: set[int] = set()
    for scope in _scope_taints(tree, parameters=False):
        for node in scope.nodes:
            if (
                isinstance(node, ast.Return)
                and node.value is not None
                and scope.carried(node.value) is not None
            ):
                lines.add(node.lineno)
    return [f"{path.name}:{line}" for line in sorted(lines)]


#: The nodes that open a scope of their own (``_ScopeTaint``).
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

#: The expressions a comprehension target is bound in, and seen only inside.
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

#: What an ``HTTPException`` puts on the wire, when it is written onto one
#: after it exists (``_wire_writes``).
_WIRE_ATTRIBUTES = frozenset({"detail", "headers"})


def _wire_writes(
    node: ast.AST, scope: _ScopeTaint, factories: dict[str, list[_Factory]]
) -> Iterator[tuple[tuple[ast.AST | None, ...], ast.AST]]:
    """``(statuses, value)`` for what ``node`` puts into an ``HTTPException``.

    A construction the factory view recognises, with what it carries
    (``_http_exception_view``). And a ``detail`` or ``headers`` written onto
    one that already exists — ``err.detail = str(e)``, ``self.err.headers =
    {...}``, ``err.headers["x"] = ...``, ``err.headers.update(...)`` — when a
    definition of the receiver reaching the write holds an ``HTTPException``
    (``_http_exception_statuses``): built here, built by the function a
    closure sits in, caught, or taken as a parameter. Building the exception
    with a static detail and filling it in later is the same leak as building
    it with the text, and #1634's second defeat pass measured it missed
    (#1858).

    The gate is that exception's statuses — or, when a definition of
    ``<receiver>.status_code`` reaches the write, the statuses written there:
    what was built as a 500 and set to ``422`` is a 422 (#1858's review
    measured an unknown there flagging typed text at 422), and an expression
    nobody can read still fails closed in ``_status_is_5xx``.

    What a caught or passed-in ``HTTPException`` already holds is not a source
    (``_ScopeTaint``), so writing it back — ``he.headers = he.headers or {}``,
    which the live ``modules/case/api/routes/cases.py`` does before setting a
    correlation id — adds nothing to the wire.
    """
    view = _http_exception_view(node, factories)
    if view is not None:
        statuses, carried = view
        if carried is not None:
            yield statuses, carried
        return
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        values = [node.value] if node.value is not None else []
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        targets = [node.func.value]
        values = [*node.args, *(kw.value for kw in node.keywords)]
    else:
        return
    for target in targets:
        while isinstance(target, ast.Subscript):
            target = target.value
        if not (isinstance(target, ast.Attribute) and target.attr in _WIRE_ATTRIBUTES):
            continue
        receiver = _dotted_name(target.value)
        if receiver is None:
            continue
        overwritten = tuple(
            scope.definitions[index].value
            for index in scope.reaching(f"{receiver}.status_code", node)
        )
        for index in scope.reaching(receiver, node):
            statuses = _http_exception_statuses(scope.definitions[index], factories)
            if statuses is None:
                continue
            for value in values:
                yield overwritten or statuses, value


def _http_exception_statuses(
    definition: _Definition, factories: dict[str, list[_Factory]]
) -> tuple[ast.AST | None, ...] | None:
    """The statuses of the ``HTTPException`` ``definition`` holds, else ``None``.

    Built — a construction the factory view recognises — it has the statuses
    the view reads. Received — caught by ``except HTTPException as he``, or
    taken as a parameter annotated ``HTTPException`` — its status is one
    nobody here can read: unknown, which fails closed. Matched on the class's
    terminal name, as ``_is_http_exception`` matches a construction.
    """
    if "HTTPException" in _received_classes(definition.binder):
        return (None,)
    view = _http_exception_view(definition.value, factories)
    return None if view is None else view[0]


def _received_classes(binder: ast.AST) -> set[str]:
    """The classes a caught or passed-in exception can be, by terminal name.

    An ``except`` clause's types, a tuple's members included, or a parameter
    annotation's classes (``_annotation_classes``); empty for any other
    binder.
    """
    if isinstance(binder, ast.ExceptHandler) and binder.type is not None:
        types = (
            binder.type.elts if isinstance(binder.type, ast.Tuple) else [binder.type]
        )
        return {name for name in map(_terminal_name, types) if name}
    if isinstance(binder, ast.arg):
        return set(_annotation_classes(binder.annotation))
    return set()


def _caller_facing(binder: ast.AST) -> bool:
    """Is the exception ``binder`` receives an ``HTTPException`` and nothing else?

    Its ``detail`` and ``headers`` were written FOR the caller — that is what
    an ``HTTPException`` is — so it is not a source of text (``_ScopeTaint``),
    though it stays a receiver ``_wire_writes`` follows. Reading it as a
    source made its own fields a leak the moment they were written back:
    ``detail = he.detail`` and then ``he.detail = {"message": detail, ...}``
    (#1858's review). A tuple catching anything else as well stays a source,
    because the name may hold that other exception.
    """
    return _received_classes(binder) == {"HTTPException"}


class _Definition(NamedTuple):
    """One binding of one name — the unit the taint analysis tracks.

    Definitions rather than names, because a name is not tainted for the rest
    of its function: ``error_response`` built from ``e`` in a typed arm and
    rebuilt from a static message in its broad sibling is two definitions,
    and only the first carries anything. Tracking the NAME made the sibling's
    ``raise HTTPException(500, detail=error_response.model_dump())`` a leak —
    #1858's first prototype reported three such sites in
    ``modules/case/api/routes/cases.py``, none of them real, and a guard that
    cries wolf on the live surface is the guard the next person narrows.

    ``binder`` is the node that binds: the one ``_bindings`` read it from (an
    ``Assign``, a walrus, a ``for``, a ``comprehension``, ...), the handler for
    an ``as`` name, the ``arg`` for a parameter, the closure for a name it
    inherits. ``statement`` is where the binding stands, and ``position`` is
    that statement's ``(line, column)``: the statement holding the binder, the
    comprehension expression for a comprehension target, the handler for an
    ``as`` name — and the scope itself, at ``(0, 0)``, for a definition the
    scope is HANDED, whose taint is ``source_broad`` rather than computed from
    its value. ``value`` is what the name is bound to, ``None`` for a source;
    a handed definition keeps one only when it holds an ``HTTPException`` the
    enclosing scope built, so that a closure can write onto it
    (``_wire_writes``). ``kills`` says the binding replaces the name, so the
    definitions before it can stop reaching past it: ``_bindings``' ``exact``,
    and true for an ``as`` name.
    """

    name: str
    value: ast.AST | None
    binder: ast.AST
    statement: ast.AST
    position: tuple[int, int]
    source_broad: bool | None
    kills: bool


def _position(node: ast.AST) -> tuple[int, int]:
    """Where ``node`` starts, which is the order the taint analysis reads in."""
    return node.lineno, node.col_offset


def _join(a: bool | None, b: bool | None) -> bool | None:
    """Two taints combined: ``None`` carries nothing, ``True`` is broad."""
    if a is None:
        return b
    if b is None:
        return a
    return a or b


class _ScopeTaint:
    """Which definitions in ONE scope carry an exception's text.

    A scope is the module, a ``def`` or a ``lambda``, and it holds the nodes
    ``_own_nodes`` gives it. Its definitions are the ones it is handed (names
    an enclosing scope tainted and the ``HTTPException``s it built, and for
    the ``HTTPException`` sinks the exception parameters), every ``except ...
    as`` name in it, and every binding ``_bindings`` reads from its nodes —
    never one of ``self`` or ``cls``. A method call on the instance is not a
    payload, which is ``_object_root``'s rule: binding ``self`` to ``e`` for
    ``self._skip(e)`` made every later ``self.MAX`` carry the exception, which
    #1858's prototype measured as two false positives package-wide.

    A definition is tainted when its value carries a source, or a name with a
    tainted definition REACHING it (``reaching``). That is iterated until
    nothing changes, one round per hop, so a chain of any length is followed
    — the five passes this replaced followed one hop out of a handler. A
    definition's taint is ``None`` (clean), ``False`` (only typed sources reach
    it) or ``True`` (a broad one does): a definition reached by both is broad.
    """

    def __init__(
        self,
        scope: ast.AST,
        parents: dict[int, ast.AST],
        sources: list[_Definition],
        imported: frozenset[str],
    ) -> None:
        self.scope = scope
        self.nodes = list(_own_nodes(scope))
        self._parents = parents
        self._imported = imported
        self.definitions = [*sources, *self._bound_here()]
        self._by_name: dict[str, list[int]] = {}
        for index, definition in enumerate(self.definitions):
            self._by_name.setdefault(definition.name, []).append(index)
        self._paths: dict[int, frozenset[int]] = {}
        self._reaching: dict[tuple[str, int], tuple[int, ...]] = {}
        self.taint = self._fixed_point()

    def _bound_here(self) -> Iterator[_Definition]:
        """The ``except ... as`` names and the bindings this scope's nodes make.

        An ``as`` name catching only ``HTTPException`` is defined but is no
        source (``_caller_facing``).
        """
        bound = {
            name for node in self.nodes for name, _, exact in _bindings(node) if exact
        }
        for node in self.nodes:
            if isinstance(node, ast.ExceptHandler) and node.name:
                yield _Definition(
                    node.name,
                    None,
                    node,
                    node,
                    _position(node),
                    (
                        None
                        if _caller_facing(node)
                        else _is_broad_handler_type(node.type)
                    ),
                    True,
                )
            for name, value, exact in _bindings(node):
                if name in {"self", "cls"} or self._not_written(node, name, bound):
                    continue
                if isinstance(node, ast.comprehension):
                    # Bound in the comprehension, and seen only inside it.
                    statement = self._parents[id(node)]
                else:
                    statement = self._statement_of(node)
                yield _Definition(
                    name, value, node, statement, _position(statement), None, exact
                )

    def _not_written(self, node: ast.AST, name: str, bound: set[str]) -> bool:
        """Is ``name`` one that the method call ``node`` cannot have written into?

        ``_bindings`` reads a method call as a write into its receiver and into
        the object the receiver hangs off. Two of those are no write a return
        can carry:

        * an imported module, unless this scope binds the name itself —
          ``warnings.warn(f"{e}")``, ``sys.stderr.write(...)``,
          ``os.path.join(base, str(e))``;
        * an object root this scope does not bind exactly — a parameter or a
          global. ``request.state.errors.append(str(e))`` followed by ``return
          await call_next(request)`` returns what the framework made of the
          request, not the text. #1858's review counted seven live sites newly
          binding such a root, one on the surface, none of them returned.

        ``bound`` is every name this scope binds exactly.
        """
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return False
        if name.split(".")[0] in self._imported and name.split(".")[0] not in bound:
            return True
        return name != _written_name(node.func.value) and name not in bound

    def _statement_of(self, node: ast.AST) -> ast.AST:
        """The statement of this scope that a binding in ``node`` belongs to.

        A binding inside a ``lambda`` has none — the statement holding the
        lambda belongs to the enclosing scope — so it stands at the nearest
        node with a line of its own.
        """
        current = node
        while current is not self.scope:
            if isinstance(current, ast.stmt):
                return current
            current = self._parents[id(current)]
        current = node
        while not hasattr(current, "lineno"):
            current = self._parents[id(current)]
        return current

    def _path(self, node: ast.AST) -> frozenset[int]:
        """``node`` and every ancestor of it inside this scope, by ``id()``."""
        path = self._paths.get(id(node))
        if path is None:
            ids: set[int] = set()
            current: ast.AST | None = node
            while current is not None and current is not self.scope:
                ids.add(id(current))
                current = self._parents.get(id(current))
            path = self._paths[id(node)] = frozenset(ids)
        return path

    def _kills_before(self, definition: _Definition, path: frozenset[int]) -> bool:
        """Does ``definition`` replace its name on every way to where ``path`` ends?

        Without a control-flow graph, "every way" is read off the structure.
        It does for a node:

        * inside the place Python binds it, for a name bound only there: an
          ``except ... as`` name inside its handler, a comprehension target
          inside its comprehension, a ``match`` capture inside the body of a
          ``case`` whose pattern captures it. Not read that way, a closure's
          ``[e.name for e in items]`` read an enclosing handler's ``e``
          (#1858's review);
        * inside the body a statement heads, for a binding that has run before
          that body can (``_heads_its_body``);
        * past its statement, for a binding that runs whenever its statement
          does, when the statement sits in a block of which a LATER statement
          contains the node: whatever reaches the node ran the binding first.
          Not a ``for`` target, whose loop may never run; not a ``with``
          target, which a suppressing context manager can skip (``with
          suppress(OSError), open(p) as msg``); nor a walrus that may never be
          evaluated (``_binds_whenever_reached``). #1858's reviews measured
          each of those killing a ``msg`` still on the wire after it.

        A binding inside an ``if`` the node follows, in a ``try`` body the
        node's handler interrupts, or in a statement that contains the node is
        none of these.
        """
        statement = definition.statement
        if isinstance(statement, _COMPREHENSIONS):
            return id(statement) in path
        if isinstance(definition.binder, ast.Match):
            return any(
                id(inner) in path
                for case in definition.binder.cases
                if definition.name in _captures(case.pattern)
                for inner in case.body
            )
        if not definition.kills:
            return False
        if isinstance(statement, ast.ExceptHandler):
            return id(statement) in path
        if not isinstance(statement, ast.stmt):
            return False
        if self._heads_its_body(definition) and any(
            id(inner) in path for inner in statement.body
        ):
            return True
        if isinstance(
            statement, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)
        ) or not self._binds_whenever_reached(definition):
            return False
        parent = self._parents.get(id(statement))
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if not isinstance(block, list):
                continue
            for index, sibling in enumerate(block):
                if sibling is statement:
                    return any(id(later) in path for later in block[index + 1 :])
        return False

    @staticmethod
    def _heads_its_body(definition: _Definition) -> bool:
        """Has ``definition`` bound before the body its statement heads runs?

        A ``for`` or ``with`` target has, and so has a walrus that IS an ``if``
        or ``while`` test, or one operand of an ``and`` test: the body runs
        only once it was evaluated. Read only there, ``for msg in ["a", "b"]:
        raise HTTPException(500, detail=msg)`` kept an earlier tainted ``msg``
        reaching the raise.
        """
        statement, binder = definition.statement, definition.binder
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            return binder is statement
        if isinstance(statement, (ast.If, ast.While)):
            test = statement.test
            return binder is test or (
                isinstance(test, ast.BoolOp)
                and isinstance(test.op, ast.And)
                and any(operand is binder for operand in test.values)
            )
        return False

    def _binds_whenever_reached(self, definition: _Definition) -> bool:
        """Does the binding run whenever its statement runs to completion?

        Only a walrus can fail to: under an ``IfExp`` (``x = a if c else (msg
        := "static")``), past the first operand of ``and``/``or``, inside a
        comprehension, or in a ``case`` guard, it may never be evaluated.
        """
        binder = definition.binder
        if not isinstance(binder, ast.NamedExpr):
            return True
        child, current = binder, self._parents.get(id(binder))
        while current is not None and current is not definition.statement:
            if isinstance(current, (ast.IfExp, ast.match_case, *_COMPREHENSIONS)):
                return False
            if isinstance(current, ast.BoolOp) and current.values[0] is not child:
                return False
            child, current = current, self._parents.get(id(current))
        return True

    def reaching(self, name: str, at: ast.AST) -> tuple[int, ...]:
        """The definitions of ``name`` that can be what it holds at ``at``.

        Position stands in for reachability, a stated limit: every definition
        whose statement starts before ``at`` reaches it, a source handed to the
        scope included — except that an ``except ... as`` name is seen only
        inside its own handler, and a comprehension target only inside its
        comprehension, where Python binds them. Position and not line, so
        ``msg = str(e); raise HTTPException(500, detail=msg)`` and a walrus
        inside the raise itself are seen; by line, #1858's review measured both
        missed. An assignment is not seen inside its own statement, whose value
        runs before it binds: ``msg, err = str(e), HTTPException(500,
        detail=msg)`` puts the OLD ``msg`` on the wire. The latest earlier
        definition that replaces the name on every way to ``at``
        (``_kills_before``) cuts off every definition before it, and so does
        one that replaces the object a dotted name hangs off — ``state =
        make()`` ends ``state.error`` — unless it is built from that object:
        ``self.state = self.state.copy()`` keeps ``self.state.error``. A later
        conditional one still reaches.

        Sibling handlers of one ``try`` are NOT kept apart. Kills keep the live
        idiom — each arm rebuilding its own ``error_response`` — clean, and
        keeping them apart would hide a typed arm's stash raised by its broad
        sibling on the next turn of a loop. That raise is gated by the stash's
        own source, typed, so it is a finding at 5xx and not at 4xx.
        """
        key = (name, id(at))
        found = self._reaching.get(key)
        if found is not None:
            return found
        path = self._path(at)
        here = _position(at)

        def visible(index: int) -> bool:
            definition = self.definitions[index]
            if isinstance(definition.statement, (ast.ExceptHandler, *_COMPREHENSIONS)):
                return id(definition.statement) in path
            if (
                isinstance(
                    definition.binder, (ast.Assign, ast.AnnAssign, ast.AugAssign)
                )
                and id(definition.binder) in path
            ):
                return False
            return definition.position < here

        def rebuilt_from(index: int, root: str) -> bool:
            value = self.definitions[index].value
            return value is not None and any(
                read == root or read.startswith(f"{root}.") for read in _names_in(value)
            )

        candidates = [i for i in self._by_name.get(name, ()) if visible(i)]
        parts = name.split(".")
        objects = [".".join(parts[:end]) for end in range(1, len(parts))]
        killers = candidates + [
            i
            for root in objects
            for i in self._by_name.get(root, ())
            if visible(i) and not rebuilt_from(i, root)
        ]
        cut = max(
            (
                self.definitions[i].position
                for i in killers
                if self._kills_before(self.definitions[i], path)
            ),
            default=None,
        )
        if cut is not None:
            candidates = [i for i in candidates if self.definitions[i].position >= cut]
        found = self._reaching[key] = tuple(candidates)
        return found

    def _enclosing_handlers(self, node: ast.AST) -> list[ast.ExceptHandler]:
        """Every ``except`` handler ``node`` sits inside, through nested scopes.

        Lexically, not only within this scope: a closure defined inside a
        handler renders the live exception exactly as the handler's own body
        would.
        """
        handlers = []
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.ExceptHandler):
                handlers.append(current)
            current = self._parents.get(id(current))
        return handlers

    def _carrier(
        self, expr: ast.AST, at: ast.AST
    ) -> tuple[bool | None, tuple[int, ...]]:
        """What ``expr`` at ``at`` can carry, before any taint is known.

        A rendering of the live exception inside a handler, broad when any
        handler around ``at`` is; and the definitions reaching each name the
        value can carry — by ``_mentions``, which skips a ``Compare`` and an
        ``IfExp``'s condition, or for a dotted name by ``_names_in``, the
        spelling ``self.last_error`` is bound under.

        Each occurrence of such a name is read where it stands, not at ``at``:
        inside a comprehension in the value, its own target is what the name
        holds. Read at the sink, outside the comprehension, a closure's
        ``detail=[err["msg"] for err in errors]`` took ``err`` for the
        enclosing function's caught exception (#1858's review).
        """
        rendered = None
        if _renders_the_live_exception(expr):
            handlers = self._enclosing_handlers(at)
            if handlers:
                rendered = any(_is_broad_handler_type(h.type) for h in handlers)
        carried = {
            name for name in _names_in(expr) if "." in name or _mentions(expr, name)
        }
        reaching: set[int] = set()
        for occurrence in ast.walk(expr):
            if isinstance(occurrence, ast.Name):
                name = occurrence.id
            elif isinstance(occurrence, ast.Attribute):
                name = _dotted_name(occurrence)
            else:
                continue
            if name in carried:
                reaching.update(self.reaching(name, occurrence))
        return rendered, tuple(sorted(reaching))

    @staticmethod
    def _resolve(
        carrier: tuple[bool | None, tuple[int, ...]], taint: list[bool | None]
    ) -> bool | None:
        rendered, reaching = carrier
        for index in reaching:
            rendered = _join(rendered, taint[index])
        return rendered

    def _fixed_point(self) -> list[bool | None]:
        """Each definition's taint, grown one hop per round until it holds.

        A definition the scope was handed keeps the taint it was handed: its
        value, if it has one, belongs to the enclosing scope.
        """
        carriers = [
            (
                None
                if d.value is None or d.statement is self.scope
                else self._carrier(d.value, d.value)
            )
            for d in self.definitions
        ]
        taint = [d.source_broad for d in self.definitions]
        while True:
            grown = [
                (
                    current
                    if carrier is None
                    else _join(current, self._resolve(carrier, taint))
                )
                for current, carrier in zip(taint, carriers)
            ]
            if grown == taint:
                return taint
            taint = grown

    def carried(self, expr: ast.AST, at: ast.AST | None = None) -> bool | None:
        """The taint ``expr`` carries, read at ``at`` (by default, where it stands).

        ``at`` is the sink, which decides the handlers a rendering of the live
        exception sits in: what the factory view hands back is a synthetic
        ``Tuple`` of the call's arguments, with no parent of its own.
        """
        return self._resolve(self._carrier(expr, at or expr), self.taint)

    def closure_sources(
        self, closure: ast.AST, factories: dict[str, list[_Factory]] | None = None
    ) -> list[_Definition]:
        """The definitions a closure defined in this scope is handed.

        * A name the closure reads and does not take as a parameter, tainted
          anywhere in this scope, is a source in the closure from its first
          line — wherever the closure is defined or called. So ``def fail():
          raise HTTPException(500, detail=msg)`` written before the ``try``
          whose handler binds ``msg`` is seen (#1858), and so is one reading
          the handler's own ``e``: the closure reads the enclosing function's
          variable, which holds the exception while the handler calls it.
          #1858's review measured that shape missed while ``as`` names were
          handed only to a closure defined inside the handler. A rebinding
          inside the closure cuts a source off by the ordinary kill rule.
        * Two limits on an ``as`` name. It is never handed from the MODULE:
          Python deletes it when the handler ends, and handing a module-level
          import guard's ``e`` made it a source in every function reading a
          name ``e`` — fourteen routes on the surface under ``main.py``'s
          guards. And a closure defined inside a handler that binds the name
          is handed that handler's definition alone, the innermost, which is
          the one it reads: the join over every same-named handler made a
          typed arm's text broad.
        * A parameter whose default carries taint is a source: the default is
          evaluated HERE, where the closure is defined (``def fail(m=msg)``,
          ``lambda m=str(e): ...``).
        * With ``factories`` — for the ``HTTPException`` sinks — the closure is
          also handed this scope's definitions of the ``HTTPException``s it
          reads, built or received (``_http_exception_statuses``), so that a
          ``detail`` it writes onto one is seen (``_wire_writes``). They carry
          no taint of their own.
        """
        args = closure.args
        params = {
            p.arg: p
            for p in [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *(p for p in (args.vararg, args.kwarg) if p is not None),
            ]
        }
        reads = {node.id for node in ast.walk(closure) if isinstance(node, ast.Name)}
        innermost: dict[str, ast.ExceptHandler] = {}
        for handler in reversed(self._enclosing_handlers(closure)):
            if handler.name:
                innermost[handler.name] = handler
        inherited: dict[str, bool | None] = {}
        exceptions: list[_Definition] = []
        for definition, taint in zip(self.definitions, self.taint):
            root = definition.name.split(".")[0]
            if root not in reads or root in params:
                continue
            if isinstance(definition.binder, ast.ExceptHandler) and (
                isinstance(self.scope, ast.Module)
                or innermost.get(definition.name, definition.binder)
                is not definition.binder
            ):
                continue
            if taint is not None:
                inherited[definition.name] = _join(
                    inherited.get(definition.name), taint
                )
            if (
                factories is not None
                and _http_exception_statuses(definition, factories) is not None
            ):
                exceptions.append(
                    definition._replace(
                        statement=closure,
                        position=(0, 0),
                        source_broad=None,
                        kills=False,
                    )
                )
        sources = [
            _Definition(name, None, closure, closure, (0, 0), broad, False)
            for name, broad in inherited.items()
        ]
        for name, default in _param_defaults(args).items():
            broad = self.carried(default)
            if broad is not None:
                sources.append(
                    _Definition(name, None, params[name], closure, (0, 0), broad, False)
                )
        return sources + exceptions


def _scope_taints(
    tree: ast.AST,
    parameters: bool,
    factories: dict[str, list[_Factory]] | None = None,
) -> Iterator[_ScopeTaint]:
    """Every scope in ``tree`` analysed, each before the scopes nested in it.

    The one taint analysis both public functions run (#1858). It replaced five
    passes — inside a handler, after it, under a handler binding nothing, from
    an exception parameter, and the returned-body walk — each with its own
    scope and its own alias rule, which every shape #1858 lists slipped
    between.

    ``parameters`` makes each function's exception parameters
    (``_exception_params``) sources from its first line — all but one
    annotated ``HTTPException`` and nothing else, which is defined but
    caller-facing (``_caller_facing``). The ``HTTPException`` sinks start from
    them; the returned-body sinks do not, a stated limit. ``factories`` is
    given for the ``HTTPException`` sinks too, so that a closure is handed the
    exceptions it can write onto. A nested scope starts from what its
    enclosing scope hands it (``closure_sources``), which is why the enclosing
    one is analysed first. The names the tree's imports bind are read once
    here, for every scope (``_ScopeTaint._not_written``).
    """
    parents = {
        id(child): node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    imported = frozenset(
        (alias.asname or alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )
    exception_classes = _module_exception_classes(tree) if parameters else frozenset()
    pending: list[tuple[ast.AST, list[_Definition]]] = [(tree, [])]
    while pending:
        scope, sources = pending.pop()
        analysis = _ScopeTaint(scope, parents, sources, imported)
        yield analysis
        for node in analysis.nodes:
            if not isinstance(node, _SCOPES):
                continue
            handed = analysis.closure_sources(node, factories)
            if parameters:
                args = node.args
                given = {
                    p.arg: p for p in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                }
                handed.extend(
                    _Definition(
                        name,
                        None,
                        given[name],
                        node,
                        (0, 0),
                        None if _caller_facing(given[name]) else broad,
                        False,
                    )
                    for name, broad in _exception_params(node, exception_classes)
                )
            pending.append((node, handed))


def _own_nodes(fn: ast.AST):
    """Every node belonging to ``fn`` itself, not to a function nested inside it.

    A nested ``def``/``lambda`` is its own scope: a name tainted in there cannot
    be the name the outer function returns. Attributing both to the outer
    function would make the guard cry wolf, and a guard that cries wolf gets
    weakened by the next person to hit it. Taint flows INTO a nested scope only
    through ``_ScopeTaint.closure_sources``, and never out of one.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


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
