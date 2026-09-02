"""Shared AST analysis for the "a swallowed ImportError hides a defect" guards.

Two guards ask the same question about the same construct:

* ``tests/unit/infrastructure/test_optional_dependency_detection.py`` — is an
  availability flag assigned ``True`` inside a try that swallows ImportError?
* ``tests/unit/architecture/test_swallowed_first_party_imports.py`` — does a
  first-party import inside such a construct name a module that exists?

The guards live in their own modules, but the *analysis* lives here so that
hardening it hardens every guard at once — the alternative, a copy per module,
is how one guard ends up strictly weaker than another. That is not
hypothetical: the two copies this module replaces had already diverged, one
recognising ``except builtins.ImportError`` and applying Python's
first-matching-handler rule while the other did neither.

Everything here is decided from the AST plus ``builtins``. Nothing is imported
from the project, so the answers cannot change with which optional
dependencies a job happens to install.

**The governing invariant is that a wrong answer must be a FALSE NEGATIVE.**
A guard that reddens on correct code is worse than no guard: it gets routed
around, and then it is not protecting anything. So every construct this cannot
decide is reported as "does not swallow", and the places that happens are named
in the docstrings below rather than left for a reader to discover.
"""

from __future__ import annotations

import ast
import builtins
from typing import Iterator, Literal

Verdict = Literal["catches", "misses", "unknown"]

#: The exception CPython raises when an import names a module that is not
#: there. Both guards ask about an import that cannot resolve, so this — not the
#: broader ``ImportError`` — is the class whose capture decides.
#:
#: The direction matters and is easy to invert. ``ModuleNotFoundError`` is a
#: SUBCLASS of ``ImportError``, so the handler question is
#: ``issubclass(ModuleNotFoundError, caught)``. Asking
#: ``issubclass(ImportError, caught)`` reads ``except ModuleNotFoundError:`` as
#: a handler that does not catch — which is exactly backwards, and was caught
#: here by a probe rather than in review.
ABSENT_MODULE_ERROR = ModuleNotFoundError


def _exception_named(node: ast.AST) -> type | None:
    """The builtin exception class this handler expression names, if any.

    Resolved against ``builtins`` rather than matched against a hardcoded name
    set, so the subclass question is answered by Python itself. The last dotted
    segment is used, which is what makes ``except builtins.ImportError``
    equivalent to ``except ImportError``.

    Returns None for anything not a builtin exception — a project exception, a
    local alias (``IE = ImportError``), a dynamic expression. Callers must treat
    None as "cannot decide", never as "not an exception".
    """
    name = ast.unparse(node).split(".")[-1]
    obj = getattr(builtins, name, None)
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return obj
    return None


def _handler_verdict(handler: ast.ExceptHandler) -> Verdict:
    """Whether this one handler would catch the failure of an absent import.

    A bare ``except:`` catches everything. Otherwise every name in the (possibly
    tuple) type is resolved: if any names a class ``ABSENT_MODULE_ERROR`` is a
    subclass of, the handler catches; if all name classes it is not, the handler
    misses; a name that cannot be resolved makes the whole handler undecidable.
    """
    if handler.type is None:
        return "catches"
    entries = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    saw_unknown = False
    for entry in entries:
        caught = _exception_named(entry)
        if caught is None:
            saw_unknown = True
        elif issubclass(ABSENT_MODULE_ERROR, caught):
            return "catches"
    return "unknown" if saw_unknown else "misses"


def _may_reraise(handler: ast.ExceptHandler) -> bool:
    """Whether this handler might put the exception back.

    Over-approximates on purpose: any ``raise`` anywhere in the handler counts,
    including a conditional one (``if not OPTIONAL: raise``) and one nested in a
    function that may never be called. Over-approximating means "not a swallow",
    which is the false-negative direction the module invariant demands — the
    alternative reddens the gate on a handler that does re-raise.
    """
    return any(isinstance(node, ast.Raise) for node in ast.walk(handler))


def swallows_import_error(node: ast.Try) -> bool:
    """Whether an ``ImportError`` raised in this try's body is silently absorbed.

    Applies Python's **first-matching-handler** rule: handlers are tested in
    order and exactly one runs, so a later ``except Exception:`` is unreachable
    for an ImportError already claimed by an earlier ``except ImportError:``.
    Ignoring that read this as a swallow::

        try:
            from pkg import thing
        except ImportError:
            raise
        except Exception:
            thing = None

    which propagates. Scanning to the first *decidable* catching handler and
    stopping there is what makes the answer match the interpreter.

    Returns False — "cannot prove a swallow" — as soon as an undecidable
    handler is reached, because a project exception earlier in the chain may be
    an ``ImportError`` subclass and claim it first.
    """
    for handler in node.handlers:
        verdict = _handler_verdict(handler)
        if verdict == "unknown":
            return False
        if verdict == "misses":
            continue
        return not _may_reraise(handler)
    return False


def suppresses_import_error(node: ast.With | ast.AsyncWith) -> bool:
    """Whether this ``with`` is a ``suppress(...)`` that absorbs an ImportError.

    The callee is matched on its last segment so both ``suppress(ImportError)``
    and ``contextlib.suppress(...)`` are recognised, and so is an aliased module
    (``import contextlib as ctx``). The *arguments* are what decide, resolved
    the same way as a handler type, so a same-named helper that suppresses
    something else is not mistaken for this.
    """
    for item in node.items:
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        if ast.unparse(call.func).split(".")[-1] != "suppress":
            continue
        for arg in call.args:
            caught = _exception_named(arg)
            if caught is not None and issubclass(ABSENT_MODULE_ERROR, caught):
                return True
    return False


def _is_type_checking_test(node: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` — False at runtime."""
    return ast.unparse(node).split(".")[-1] == "TYPE_CHECKING"


def executed_when_block_runs(body) -> Iterator[ast.AST]:
    """Every statement that runs when this block runs, at the time it runs.

    Three distinctions, each of which a plain ``ast.walk`` gets wrong:

    * A **class body executes at definition time**, so an import inside a
      ``class`` in a guarded try *is* guarded. Treating ``ClassDef`` like
      ``FunctionDef`` hid exactly the #947 shape from the gate written to catch
      it.
    * A **function body does not**. Its statements run at call time, outside the
      guard, where a failed import raises loudly. Descending into one reports a
      construct that cannot have the defect.
    * ``if TYPE_CHECKING:`` **never runs at all**. The name is False at runtime
      by construction, so a body under it is a type-checker artefact, not code
      the guard protects.

    Everything else — ``if``/``for``/``while``/``with``/``try``/``match`` — is
    ordinary control flow and is descended into, because "may not run on this
    input" is not "does not run".
    """

    def emit(node: ast.AST) -> Iterator[ast.AST]:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            yield from _walk_all(node.orelse)
            return
        for child in ast.iter_child_nodes(node):
            yield from emit(child)

    def _walk_all(nodes) -> Iterator[ast.AST]:
        for node in nodes:
            yield from emit(node)

    yield from _walk_all(body)
