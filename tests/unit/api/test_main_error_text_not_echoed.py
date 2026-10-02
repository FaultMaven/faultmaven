"""Raw exception text never reaches a response body from ``main.py``.

``main.py`` carries the health, readiness, SLA and metrics endpoints, and 19 of
its handlers were shaped like::

    except Exception as e:
        logger.error(f"...: {e}")
        return {"error": f"...: {e}", "timestamp": ...}

The text was already going to the log — the return added nothing diagnostic and
handed the caller the internals instead. CodeQL's ``py/stack-trace-exposure``
flagged 16 of them; the other three were the same defect in sites it did not
reach.

This is the ``main.py`` analogue of the knowledge module's #866 fix and the case
module's #966, and follows their guards: assert on the **class**, not on one
site, so a handler added later cannot reintroduce the pattern quietly.

Six of the 19 were invisible to the shared analysis before this change. They
stash the text in a local *inside* the handler and return it *after*::

    except Exception as e:
        sla_details = {"error": str(e)}
    return {"sla": sla_details}

and one wrote through a subscript (``cleanup_results["resource_cleanup"] = ...``),
which the alias-following skipped entirely. Both gaps are fixed in
``tests/error_text_ast`` rather than here, so every module's guard gains them.

Scope is ``main.py``. The nine files that used to be listed here as "same
class, no guard, queued separately" were swept by #1400, and the guard that
covers them is scoped to the whole response-producing surface rather than to a
file list — ``tests/unit/api/test_api_surface_error_text_not_echoed.py``. That
one now also covers ``main.py``, deliberately: this file keeps the ``main.py``
handler-count floor and the two ``main.py``-specific facts below, which a
surface-wide floor cannot express.

fm#1707 wave 2 moved the composition root, lifespan and middleware setup out
of ``main.py`` into ``faultmaven/bootstrap/{composition,lifespan,middleware}.py``
— 21 of the original 49 bound handlers went with them. None of those three
files hands a caught exception to an HTTP caller (they hold no route handler:
no ``@app.get``/``@app.post``, nothing that returns a response body), so the
returned-body and ``HTTPException`` checks below stay scoped to ``main.py``,
where the response-producing endpoints still live. The "swallowed, not even
logged" check is not about response bodies, though — it is about whether the
exception is read *anywhere*, and it applies exactly as much to a fail-fast
gate's ``except Exception as e: logger.warning(...)`` as to a route handler's.
That check runs over ``main.py`` and the three bootstrap files, so the moved
handlers keep the coverage they had before the move (R8: a test migrates to
the new seam, it is not weakened by the code moving out from under it).
"""

import ast
import pathlib

import pytest

import faultmaven.bootstrap.composition as composition_module
import faultmaven.bootstrap.lifespan as lifespan_module
import faultmaven.bootstrap.middleware as middleware_module
import faultmaven.main as main_module
from tests.error_text_ast import (
    _http_exception_factories,
    _http_exception_view,
    http_exception_leak_sites,
    returned_body_leak_sites,
)

# main.py had 49 bound handlers when this guard was written; 21 moved to the
# three bootstrap files below in fm#1707 wave 2. Each floor only has to be
# high enough that a failed parse or a gutted file cannot pass vacuously.
_MIN_BOUND_HANDLERS = 20

#: The three modules that received main.py's composition-root/lifespan/
#: middleware code. Read by the swallowed-exception check only — see the
#: module docstring for why the response-body checks stay main.py-only.
_MOVED_MODULES = (composition_module, lifespan_module, middleware_module)


def _count_bound_handlers(source: str) -> int:
    return sum(
        isinstance(node, ast.ExceptHandler) and bool(node.name)
        for node in ast.walk(ast.parse(source))
    )


def _main_source() -> pathlib.Path:
    """The guarded file, with a floor so an empty parse cannot pass vacuously."""
    path = pathlib.Path(main_module.__file__)
    handlers = _count_bound_handlers(path.read_text())
    assert handlers >= _MIN_BOUND_HANDLERS, (
        f"main.py unexpectedly has only {handlers} bound except handlers — "
        "the guards below would pass without inspecting anything"
    )
    return path


def _moved_sources() -> list[pathlib.Path]:
    """The three files fm#1707 wave 2 split out of main.py, same floor logic."""
    paths = [pathlib.Path(m.__file__) for m in _MOVED_MODULES]
    for path in paths:
        handlers = _count_bound_handlers(path.read_text(encoding="utf-8"))
        assert handlers >= 1, (
            f"{path} unexpectedly has no bound except handlers — the swallowed-"
            "exception check below would pass without inspecting anything"
        )
    return paths


@pytest.mark.unit
def test_main_builds_no_http_exception_so_only_the_returned_body_guard_applies():
    """States the fact the guard below depends on, instead of implying it.

    ``main.py`` builds **zero** ``HTTPException`` objects -- its health,
    readiness and metrics endpoints degrade by RETURNING a body rather than
    raising. So running ``http_exception_leak_sites`` over it passes
    unconditionally, and an earlier version of this file did exactly that
    while sharing a handler-count floor with the returned-body test, which
    made the vacuous half look guarded.

    Asserting the count instead makes the vacuity explicit and gives it a job:
    the first ``HTTPException`` built in ``main.py`` trips this test, and
    whoever adds it has to decide whether the ``HTTPException`` guard (5xx, or
    any status from a broad ``except``) now needs to run here.

    "Built" is whatever the analysis itself reads as a construction —
    ``_http_exception_view`` over every node, the way every pass of it walks
    a file. Counting only a literal ``raise HTTPException(...)`` left a
    tripwire narrower than the guard it gates: an ``HTTPException`` returned,
    assigned and raised later, or built by a same-module factory or raising
    helper, went past it, and #1634's review found that gap.
    """
    tree = ast.parse(_main_source().read_text(encoding="utf-8"))
    factories = _http_exception_factories(tree)
    construction_sites = sorted(
        node.lineno
        for node in ast.walk(tree)
        if _http_exception_view(node, factories) is not None
    )

    assert construction_sites == [], (
        "main.py now builds an HTTPException (directly or through a same-module "
        "factory) at these lines; the HTTPException leak guard "
        "(http_exception_leak_sites: 5xx, or any status from a broad except) "
        f"should be enabled for this file: {construction_sites}"
    )


@pytest.mark.unit
def test_no_handler_swallows_the_exception_without_logging_it():
    """Redacting the body must move the diagnostic, not delete it.

    Every site swept here already logged the exception, so replacing the
    returned text with a static message cost nothing. One did not -- the
    service probe in ``/health/dependencies`` bound ``as e`` and, after the
    sweep, read it nowhere: the text had been going only to the caller, and
    redacting it discarded the only record of seven services' failures.

    ``ruff``'s F841 does not catch this: ``pyproject.toml`` exempts
    ``faultmaven/main.py``.

    Also runs over the three bootstrap files fm#1707 wave 2 split out of
    ``main.py`` (see the module docstring): this property has nothing to do
    with response bodies, so it travels with the handlers rather than staying
    behind with the file they used to be bound in.
    """
    silent = []
    for path in (_main_source(), *_moved_sources()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        silent.extend(
            f"{path.name}:{handler.lineno}"
            for handler in ast.walk(tree)
            if isinstance(handler, ast.ExceptHandler)
            and handler.name
            and not any(
                isinstance(node, ast.Name) and node.id == handler.name
                for node in ast.walk(handler)
            )
        )

    assert silent == [], (
        "except handlers that bind the exception and never read it -- the text "
        f"is discarded, not redacted; log it before returning: {silent}"
    )


@pytest.mark.unit
def test_no_handler_returns_the_exception():
    """No ``return`` carries a caught exception's text into a body.

    Covers both shapes: a return inside the handler, and a return *after* one
    that carries a local the handler wrote the text into. Every leak in
    ``main.py`` was the returned-body kind — the endpoints here degrade to a
    200 or a diagnostic dict rather than raising, so the ``HTTPException``
    guard above never saw any of them.
    """
    offenders = returned_body_leak_sites(_main_source())

    assert offenders == [], (
        "return statements carrying the caught exception into the response "
        f"body (use a static message and log server-side): {offenders}"
    )
