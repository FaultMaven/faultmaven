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

Scope is ``main.py``. ``api/routes/admin.py``, ``api/routes/admin_config.py``,
``api/protection.py``, ``api/v1/auth_dependencies.py``, the three middlewares and
``modules/report/api/routes.py`` have sites of the same class and no guard; they
are queued separately and are knowingly not covered here.
"""

import ast
import pathlib

import pytest

import faultmaven.main as main_module
from tests.error_text_ast import (
    http_exception_leak_sites,
    returned_body_leak_sites,
)

# main.py had 49 bound handlers when this guard was written. The floor only has
# to be high enough that a failed parse or a gutted file cannot pass vacuously.
_MIN_BOUND_HANDLERS = 40


def _main_source() -> pathlib.Path:
    """The guarded file, with a floor so an empty parse cannot pass vacuously."""
    path = pathlib.Path(main_module.__file__)
    handlers = sum(
        isinstance(node, ast.ExceptHandler) and bool(node.name)
        for node in ast.walk(ast.parse(path.read_text()))
    )
    assert handlers >= _MIN_BOUND_HANDLERS, (
        f"main.py unexpectedly has only {handlers} bound except handlers — "
        "the guards below would pass without inspecting anything"
    )
    return path


@pytest.mark.unit
def test_main_raises_no_http_exception_so_only_the_returned_body_guard_applies():
    """States the fact the guard below depends on, instead of implying it.

    ``main.py`` contains **zero** ``raise HTTPException`` sites -- its health,
    readiness and metrics endpoints degrade by RETURNING a body rather than
    raising. So running ``http_exception_leak_sites`` over it passes
    unconditionally, and an earlier version of this file did exactly that while
    sharing a handler-count floor with the returned-body test, which made the
    vacuous half look guarded.

    Asserting the count instead makes the vacuity explicit and gives it a job:
    the first ``raise HTTPException`` added to ``main.py`` trips this test, and
    whoever adds it has to decide whether the 5xx guard now needs to run here.
    """
    source = _main_source().read_text(encoding="utf-8")
    raise_sites = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and (getattr(node.exc.func, "id", None) or getattr(node.exc.func, "attr", None))
        == "HTTPException"
    ]

    assert raise_sites == [], (
        "main.py now raises HTTPException at these lines; the 5xx leak guard "
        f"(http_exception_leak_sites) should be enabled for this file: {raise_sites}"
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
    """
    tree = ast.parse(_main_source().read_text(encoding="utf-8"))
    silent = [
        handler.lineno
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        and handler.name
        and not any(
            isinstance(node, ast.Name) and node.id == handler.name
            for node in ast.walk(handler)
        )
    ]

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
