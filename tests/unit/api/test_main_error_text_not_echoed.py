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
def test_no_500_site_interpolates_the_exception():
    """No 5xx ``HTTPException`` in ``main.py`` carries ``e`` into the response."""
    offenders = http_exception_leak_sites(_main_source())

    assert offenders == [], (
        "5xx HTTPException sites carrying the caught exception into the "
        "response (leaks internal text verbatim; use a static message and log "
        f"server-side): {offenders}"
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
