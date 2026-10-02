"""The error-text analysis's REACH, pinned shape by shape (#1634, #1598).

The surface guard (``test_api_surface_error_text_not_echoed.py``) asserts the
analysis's answer on the current tree, and the tree is clean — so that guard
says nothing about what the analysis can see. #1634 is what that costs:
``llm_service_error_http_exception(exc)`` put ``str(exc)[:200]`` into an
``HTTPException`` through a same-module factory, ``/turns`` echoed internal
text, and the guard passed. #1598 is the same gap in the status rule: a 4xx
raised from ``except Exception`` carrying ``e`` passed too.

Each row below plants one shape in its own module and runs it through
``http_exception_leak_sites``, the entry point every error-text guard calls.
The rows are the mechanism probe #1634's plan ran before the change was built
(https://github.com/FaultMaven/faultmaven/issues/1634#issuecomment-5944121540),
copied as literal source, P for must-flag and N for must-not-flag. The
invariant they pin, on the response-producing surface:

* an exception's text reaching an ``HTTPException`` ``detail`` is reported
  whenever the exception came from a broad source, whatever the status, and at
  5xx otherwise;
* whether the ``HTTPException`` is built directly or through a same-module
  factory, and whether the exception arrives by an ``except`` binding or a
  function parameter.

The N rows are as much the point as the P rows: a guard that cries wolf on a
typed 4xx or a static detail is the guard the next person narrows until it is
quiet, which is how both blind spots got there.
"""

import ast
import pathlib
from collections.abc import Callable

import pytest

import faultmaven
from tests.error_text_ast import http_exception_leak_sites
from tests.unit.api import test_api_surface_error_text_not_echoed as surface_guard

#: Every planted module starts with these: ``_llm_http`` (the
#: ``api/exception_handlers.py`` factory's signature), ``_f(msg)`` building a
#: literal 500 with a dict detail, and ``_f_default`` whose status parameter
#: has a default. The header must not flag on its own — a factory is not a
#: leak until a caller hands it an exception — which the exact-equality
#: assertions below check on every row.
_HEADER = (
    "import traceback\n"
    "from typing import Optional\n"
    "from fastapi import HTTPException, status\n"
    "def _llm_http(status_code, error_code, detail, retry_after, correlation_id):\n"
    '    return HTTPException(status_code=status_code, detail=detail, headers={"x": error_code})\n'
    "def _f(msg):\n"
    '    return HTTPException(500, detail={"message": msg})\n'
    "def _f_default(detail, status_code=500):\n"
    "    return HTTPException(status_code=status_code, detail=detail)\n"
)

#: A row is a planter: ``plant(tmp_path) -> (path, leak_line)``. The leak line
#: is what a must-flag row expects reported, and nothing else; a must-not-flag
#: row ignores it.
_Planter = Callable[[pathlib.Path], tuple[pathlib.Path, int]]


def _planted(body: str) -> _Planter:
    """A row planted under ``_HEADER`` in a module of its own.

    Every must-flag row puts its leaking ``raise``/``return`` on its final
    line, so that is the one site expected.
    """

    def plant(tmp_path: pathlib.Path) -> tuple[pathlib.Path, int]:
        path = tmp_path / "planted.py"
        path.write_text(_HEADER + body, encoding="utf-8")
        return path, len((_HEADER + body).splitlines())

    return plant


# --- The real helper: #1634's leak, re-planted ------------------------------

_EXCEPTION_HANDLERS = pathlib.Path(faultmaven.__file__).parent / (
    "api/exception_handlers.py"
)

#: Arm 5 of ``llm_service_error_http_exception``: the unclassifiable fallback
#: #1633 made static. The ``"10"`` retry-after is what tells it apart from the
#: single-line ``SERVICE_ERROR`` call in
#: ``global_service_exception_http_exception``, which passes ``None``.
_P1_ANCHOR = '        "SERVICE_ERROR",\n        SERVICE_ERROR_DETAIL,\n        "10",\n'

#: The text the fallback carried before #1633, verbatim from #1634.
_P1_LEAK = (
    '        "SERVICE_ERROR",\n'
    '        f"Unable to process your message: {str(exc)[:200]}",\n'
    '        "10",\n'
)


def _p1_mutant() -> tuple[str, int]:
    """The real ``exception_handlers.py`` with #1634's leak put back, and the
    line of the ``return`` that carries it.

    The anchor must occur exactly once: if the helper is edited or moved, this
    fails loudly rather than planting nothing and passing on an unmutated file.
    The expected line is derived from the mutant's own tree rather than
    written down, and pinned to ``llm_service_error_http_exception`` — the
    helper is where the text enters the body, so that is where the finding
    belongs, not at any of its callers.
    """
    source = _EXCEPTION_HANDLERS.read_text(encoding="utf-8")
    assert source.count(_P1_ANCHOR) == 1, (
        "the arm-5 fallback of llm_service_error_http_exception moved or "
        "changed; re-point _P1_ANCHOR at it so P1 still plants #1634's leak"
    )
    mutant = source.replace(_P1_ANCHOR, _P1_LEAK)
    anchor_line = source[: source.index(_P1_ANCHOR)].count("\n") + 1

    (helper,) = [
        node
        for node in ast.walk(ast.parse(mutant))
        if isinstance(node, ast.FunctionDef)
        and node.name == "llm_service_error_http_exception"
    ]
    (leak,) = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Return)
        and node.lineno <= anchor_line <= node.end_lineno
    ]
    return mutant, leak.lineno


def _real_helper_with_its_leak_put_back(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, int]:
    """P1, #1634's pin: the two-hop leak, through the real file.

    ``str(exc)`` arrives as a PARAMETER of ``llm_service_error_http_exception``
    and reaches ``detail`` only through ``_llm_http``'s parameters, so it
    needs both the factory view and the parameter pass. The analysis before
    #1634 reported this mutant clean.
    """
    mutant, leak_line = _p1_mutant()
    path = tmp_path / "exception_handlers.py"
    path.write_text(mutant, encoding="utf-8")
    return path, leak_line


def _real_helper_as_shipped(tmp_path: pathlib.Path) -> tuple[pathlib.Path, int]:
    """N10: the same file unmodified.

    It holds a factory (``_llm_http``) and a broad exception parameter
    (``exc: BaseException``), so both halves #1634 added read it, and neither
    may report it.
    """
    path = tmp_path / "exception_handlers.py"
    path.write_text(_EXCEPTION_HANDLERS.read_text(encoding="utf-8"), encoding="utf-8")
    return path, 0


_MUST_FLAG = [
    pytest.param(
        _real_helper_with_its_leak_put_back,
        id="P1",  # #1634's pin: the real helper with its leak put back
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        raise HTTPException(status_code=400, detail=str(e))\n"
        ),
        id="P2",  # shape K: broad handler, 4xx — #1598's positive control
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        msg = str(e)\n    raise HTTPException(422, detail=msg)\n"
        ),
        id="P3",  # broad handler taints a local, 422 raised after the handler
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except (ValueError, Exception) as e:\n        raise HTTPException(404, str(e))\n"
        ),
        id="P4",  # a tuple holding a broad type, 404
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except:\n        raise HTTPException(400, detail=traceback.format_exc())\n"
        ),
        id="P5",  # bare except, format_exc(), 400
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except BaseException as e:\n        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f'{e}')\n"
        ),
        id="P6",  # BaseException, a named 400 constant
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        raise _llm_http(400, 'X', str(e), None, None)\n"
        ),
        id="P7",  # factory call from a broad handler, 4xx
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except LLMException as e:\n        raise _llm_http(500, 'X', str(e), None, None)\n"
        ),
        id="P8",  # factory call from a typed handler, 5xx
    ),
    pytest.param(
        _planted(
            "def h(exc: Exception) -> HTTPException:\n    return HTTPException(status_code=400, detail=str(exc))\n"
        ),
        id="P9",  # broad exception parameter, 4xx, returned
    ),
    pytest.param(
        _planted(
            "def h(exc: SomeError):\n    raise HTTPException(500, detail=repr(exc))\n"
        ),
        id="P10",  # typed exception parameter, 5xx, raised
    ),
    pytest.param(
        _planted(
            "def h(exc: Optional[BaseException] = None):\n    return HTTPException(400, detail=str(exc))\n"
        ),
        id="P11",  # Optional[BaseException] parameter, 4xx
    ),
    pytest.param(
        _planted(
            "async def h(exc: BaseException):\n    return HTTPException(503, str(exc))\n"
        ),
        id="P12",  # async helper, 503
    ),
    pytest.param(
        _planted(
            "def h(exc: 'BaseException'):\n    return HTTPException(400, detail=str(exc))\n"
        ),
        id="P13",  # string annotation, 4xx
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except KeyError as e:\n        raise _llm_http(status_code=500, error_code='X', detail=str(e), retry_after=None, correlation_id=None)\n"
        ),
        id="P14",  # factory called by keyword
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except KeyError as e:\n        raise _f(str(e))\n"
        ),
        id="P15",  # factory with a dict detail and its own literal 500
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        raise HTTPException(400, detail=str(e)) from e\n"
        ),
        id="P16",  # raise ... from e
    ),
    pytest.param(
        _planted(
            "def h(exc: Exception | None):\n    return HTTPException(400, detail=str(exc))\n"
        ),
        id="P17",  # PEP 604 `Exception | None` parameter, 4xx
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        d = {'m': str(e)}\n        raise HTTPException(409, detail=d)\n"
        ),
        id="P18",  # broad handler, alias dict, 409
    ),
    pytest.param(
        _planted(
            "def h(exc: BaseException):\n    text = str(exc)\n    return _llm_http(400, 'X', text, None, None)\n"
        ),
        id="P19",  # exception parameter, alias, through a factory
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except KeyError as e:\n        raise _f_default(str(e))\n"
        ),
        id="P20",  # status parameter left at its default: unknown, fails closed
    ),
    pytest.param(
        _planted("def h(exc):\n    return HTTPException(500, str(exc))\n"),
        id="P21",  # unannotated `exc` parameter, 5xx
    ),
    pytest.param(
        _planted(
            "class C:\n    def _err(self, msg):\n        return HTTPException(500, msg)\n    def r(self):\n        try: x()\n        except KeyError as e:\n            raise self._err(str(e))\n"
        ),
        id="P22",  # method factory called through self.
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except KeyError as e:\n        raise HTTPException(500, detail=str(e))\n"
        ),
        id="P23",  # typed handler, direct 5xx — the control the old analysis caught
    ),
]

_MUST_NOT_FLAG = [
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except ValidationException as e:\n        raise HTTPException(400, detail=str(e))\n"
        ),
        id="N1",  # typed handler, 4xx: the #866/#966 carve-out
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        raise HTTPException(400, detail='static')\n"
        ),
        id="N2",  # broad handler, static detail
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except (ValueError, KeyError) as e:\n        raise HTTPException(400, str(e))\n"
        ),
        id="N3",  # a tuple of typed members only, 4xx
    ),
    pytest.param(
        _planted(
            "def h(exc: NotFoundError):\n    return HTTPException(404, detail=str(exc))\n"
        ),
        id="N4",  # typed exception parameter, 4xx
    ),
    pytest.param(
        _planted(
            "def h(exc: BaseException):\n    return _llm_http(500, 'X', 'static', None, None)\n"
        ),
        id="N5",  # factory with a static detail
    ),
    pytest.param(
        _planted(
            "def h(exc: BaseException):\n    return _llm_http(503, code_of(exc), 'static', None, None)\n"
        ),
        id="N6",  # the exception reaches a header argument, not detail
    ),
    pytest.param(
        _planted(
            "def h(exc: BaseException, message: str):\n    return HTTPException(500, detail=message)\n"
        ),
        id="N7",  # detail from a str parameter beside the exception
    ),
    pytest.param(
        _planted(
            "def g(exc: BaseException):\n    return llm_service_error_http_exception(exc)\n"
        ),
        id="N8",  # passes the exception to a function that is not a factory here
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        raise HTTPException(400, detail='A' if e.code == 1 else 'B')\n"
        ),
        id="N9",  # the exception steers a choice between literals
    ),
    pytest.param(
        _real_helper_as_shipped,
        id="N10",  # the real exception_handlers.py, unmodified
    ),
    pytest.param(
        _planted(
            "def bad_request(message: str):\n    raise HTTPException(400, detail=message)\n"
        ),
        id="N11",  # a message helper with a str parameter, 4xx
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        logger.error(e)\n        raise HTTPException(400, detail='bad input')\n"
        ),
        id="N12",  # logged server-side, static detail
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception:\n        raise HTTPException(400, detail='x')\n"
        ),
        id="N13",  # unbound broad handler, static detail
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except ValidationException as e:\n        raise _llm_http(422, 'V', str(e), None, None)\n"
        ),
        id="N14",  # typed handler, 4xx through a factory
    ),
    pytest.param(
        _planted("def bad(error):\n    raise HTTPException(400, detail=error)\n"),
        id="N15",  # unannotated `error` parameter is not broad, 4xx
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        log(e)\n    raise HTTPException(400, detail='static')\n"
        ),
        id="N16",  # broad handler taints nothing; static 4xx raised after it
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize("plant", _MUST_FLAG)
def test_must_flag(tmp_path, plant):
    """The planted leak is reported, at its own line and nowhere else.

    Exact equality, not "non-empty": a header factory reported as a leak, or
    the site reported at a caller instead of where the text enters the body,
    would be a different finding from the one the row plants.
    """
    path, leak_line = plant(tmp_path)

    assert http_exception_leak_sites(path) == [f"{path.name}:{leak_line}"]


@pytest.mark.unit
@pytest.mark.parametrize("plant", _MUST_NOT_FLAG)
def test_must_not_flag(tmp_path, plant):
    path, _ = plant(tmp_path)

    assert http_exception_leak_sites(path) == []


@pytest.mark.unit
def test_p1_reaches_the_surface_guard_as_a_finding_in_the_helper(tmp_path, monkeypatch):
    """P1 again, through the guard that runs the analysis in CI.

    A direct call proves the analysis and nothing about the guard's plumbing:
    the surface guard re-keys every reported line onto a ``Return``/``Raise``
    node, its enclosing function and its expression (``_offender_keys``), and
    then filters through ``_ALLOWED``. A finding the analysis reports but the
    plumbing drops is a guard that passes. So the surface guard's own test is
    driven here, scoped to the mutant, and must fail naming the helper.
    """
    mutant, leak_line = _p1_mutant()
    path = tmp_path / "faultmaven" / "api" / "exception_handlers.py"
    path.parent.mkdir(parents=True)
    path.write_text(mutant, encoding="utf-8")
    monkeypatch.setattr(surface_guard, "_REPO", tmp_path)
    monkeypatch.setattr(surface_guard, "_surface", lambda: (path,))

    with pytest.raises(AssertionError) as failure:
        surface_guard.test_no_api_surface_site_puts_the_caught_exception_on_the_wire()

    assert (
        f"faultmaven/api/exception_handlers.py:{leak_line} in "
        "llm_service_error_http_exception()"
    ) in str(failure.value)
