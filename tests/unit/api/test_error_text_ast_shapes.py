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
copied as literal source, P for must-flag and N for must-not-flag. The D and
NN rows are the shapes #1634's review re-introduced to defeat the first
version (https://github.com/FaultMaven/faultmaven/pull/1856#issuecomment-5944512232),
probed the same way before they were built. The invariant they pin, on the
response-producing surface:

* an exception's text reaching an ``HTTPException``'s ``detail`` or
  ``headers`` is reported whenever the exception came from a broad source,
  whatever the status, and at 5xx otherwise;
* whether the ``HTTPException`` is built directly or through a same-module
  factory or raising helper, wherever that call stands, and whether the
  exception arrives by an ``except`` binding or a function parameter.

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

#: The header the review's D and NN rows were probed under: ``_llm_http``
#: again, and ``Any`` for the annotation rows.
_DEFEAT_HEADER = 'import traceback\nfrom typing import Any, Optional\nfrom fastapi import HTTPException, status\ndef _llm_http(status_code, error_code, detail, retry_after, correlation_id):\n    return HTTPException(status_code=status_code, detail=detail, headers={"x": error_code})\n'

#: A row is a planter: ``plant(tmp_path) -> (path, leak_line)``. The leak line
#: is what a must-flag row expects reported, and nothing else; a must-not-flag
#: row ignores it.
_Planter = Callable[[pathlib.Path], tuple[pathlib.Path, int]]


def _planted(
    body: str, header: str = _HEADER, leak_line: int | None = None
) -> _Planter:
    """A row planted under ``header`` in a module of its own.

    A must-flag row's leak is on its final line unless ``leak_line`` (counted
    within ``body``, from 1) says otherwise: D12a builds the ``HTTPException``
    one line before it raises it, and the call is what is reported; D1b's
    leaking ``return`` is followed by a static one.
    """

    def plant(tmp_path: pathlib.Path) -> tuple[pathlib.Path, int]:
        path = tmp_path / "planted.py"
        path.write_text(header + body, encoding="utf-8")
        line = leak_line if leak_line is not None else len(body.splitlines())
        return path, len(header.splitlines()) + line

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

#: The comment that opens arm 5. #1634's review planted its two real-file
#: mutants as a new arm just above it, inside the same helper.
_ARM5 = "    # 5. Genuinely unclassifiable"

#: D1: the file's own idiom for reading a cause chain — it has two such loops —
#: rendering a link of it. ``str(c)`` is ``exc``'s text one ``__cause__`` down.
_D1_ARM = (
    "    for c in walk_cause_chain(exc):\n"
    "        if isinstance(c, LLMException):\n"
    "            return _llm_http(502, 'LLM_PROVIDER_ERROR', str(c), None, correlation_id)\n"
)

#: D3b: the exception's text in ``_llm_http``'s ``error_code``, which the
#: factory puts in the ``x-error-code`` response header rather than the body.
_D3B_ARM = "    return _llm_http(500, f'SERVICE_ERROR:{exc}', SERVICE_ERROR_DETAIL, '10', correlation_id)\n"


def _helper_mutant(anchor: str, replacement: str, leak: str) -> tuple[str, int]:
    """The real ``exception_handlers.py`` with a leak planted in
    ``llm_service_error_http_exception``, and the line of the ``return`` that
    carries it.

    The anchor must occur exactly once: if the helper is edited or moved, this
    fails loudly rather than planting nothing and passing on an unmutated file.
    The expected line is derived from the mutant's own tree rather than
    written down — the ``return`` holding the planted ``leak`` text — and
    pinned to ``llm_service_error_http_exception``: the helper is where the
    text enters the response.
    """
    source = _EXCEPTION_HANDLERS.read_text(encoding="utf-8")
    assert source.count(anchor) == 1, (
        "llm_service_error_http_exception moved or changed under this anchor; "
        f"re-point it so the row still plants its leak: {anchor!r}"
    )
    mutant = source.replace(anchor, replacement)
    assert mutant.count(leak) == 1, leak
    leak_line = mutant[: mutant.index(leak)].count("\n") + 1

    (helper,) = [
        node
        for node in ast.walk(ast.parse(mutant))
        if isinstance(node, ast.FunctionDef)
        and node.name == "llm_service_error_http_exception"
    ]
    (carrier,) = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Return) and node.lineno <= leak_line <= node.end_lineno
    ]
    return mutant, carrier.lineno


def _p1_mutant() -> tuple[str, int]:
    return _helper_mutant(_P1_ANCHOR, _P1_LEAK, "Unable to process your message")


def _real_helper_mutant(anchor: str, replacement: str, leak: str) -> _Planter:
    def plant(tmp_path: pathlib.Path) -> tuple[pathlib.Path, int]:
        mutant, leak_line = _helper_mutant(anchor, replacement, leak)
        path = tmp_path / "exception_handlers.py"
        path.write_text(mutant, encoding="utf-8")
        return path, leak_line

    return plant


#: P1, #1634's pin: the two-hop leak, through the real file. ``str(exc)``
#: arrives as a PARAMETER of ``llm_service_error_http_exception`` and reaches
#: ``detail`` only through ``_llm_http``'s parameters, so it needs both the
#: factory view and the parameter pass. The analysis before #1634 reported
#: this mutant clean.
_real_helper_with_its_leak_put_back = _real_helper_mutant(
    _P1_ANCHOR, _P1_LEAK, "Unable to process your message"
)


def _real_helper_as_shipped(tmp_path: pathlib.Path) -> tuple[pathlib.Path, int]:
    """N10: the same file unmodified.

    It holds a factory (``_llm_http``), a broad exception parameter
    (``exc: BaseException``), ``headers`` and two loops over the cause chain,
    so every half of the analysis reads it, and none may report it.
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
    pytest.param(
        _planted(
            "def h(exc: BaseException):\n    return _llm_http(503, code_of(exc), 'static', None, None)\n"
        ),
        # N6 in the plan's probe, pinned there as must-NOT-flag because the
        # exception reaches a header argument rather than `detail`. That was
        # wrong: `_llm_http` puts `error_code` in the `x-error-code` response
        # header, a header is on the wire exactly as a body is, and the
        # returned-body half already treats `response.headers[...] = str(e)`
        # as a leak. #1634's review moved it here; its id is kept so the
        # history reads.
        id="N6",
    ),
    # --- #1634's review: the shapes that defeated the first version ---
    pytest.param(
        _planted(
            "def _fail(msg):\n    raise HTTPException(400, detail=msg)\ndef r():\n    try: x()\n    except Exception as e:\n        _fail(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D2",  # raising helper broad 400
    ),
    pytest.param(
        _planted(
            "async def _fail(msg):\n    raise HTTPException(404, detail=msg)\nasync def r():\n    try: x()\n    except Exception as e:\n        await _fail(f'Failed: {e}')\n",
            header=_DEFEAT_HEADER,
        ),
        id="D2b",  # async raising helper await
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        raise HTTPException(status_code=500, detail='x', headers={'X-Error': str(e)})\n",
            header=_DEFEAT_HEADER,
        ),
        id="D3a",  # headers direct
    ),
    pytest.param(
        _planted(
            "def h(exc: BaseException):\n    return _llm_http(503, code_of(exc), 'static', None, None)\n",
            header=_DEFEAT_HEADER,
        ),
        id="D3c",  # N6 exc into header arg
    ),
    pytest.param(
        _planted(
            "def _err(msg, client=False):\n    if client:\n        return HTTPException(400, detail=msg)\n    return HTTPException(500, detail=msg)\ndef r():\n    try: x()\n    except KeyError as e:\n        raise _err(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D4",  # factory two returns
    ),
    pytest.param(
        _planted(
            "class A:\n    def _err(self, msg):\n        return HTTPException(500, msg)\nclass B:\n    def _err(self, msg):\n        return HTTPException(400, msg)\ndef r():\n    try: x()\n    except KeyError as e:\n        raise A()._err(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D4b",  # name collision
    ),
    pytest.param(
        _planted(
            "def server_error(detail, status_code=500):\n    return HTTPException(status_code=status_code, detail=detail)\ndef r():\n    try: x()\n    except KeyError as e:\n        raise server_error(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D7b",  # default 500 typed
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        log(e)\n        raise HTTPException(400, detail=traceback.format_exc())\n",
            header=_DEFEAT_HEADER,
        ),
        id="D9",  # bound handler format_exc 400
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        err = HTTPException(400, detail=str(e))\n        raise err\n",
            header=_DEFEAT_HEADER,
            leak_line=4,
        ),
        id="D12a",  # assigned then raised
    ),
    pytest.param(
        _planted(
            "async def mk(m):\n    return HTTPException(500, m)\nasync def r():\n    try: x()\n    except KeyError as e:\n        raise await mk(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D12b",  # raise await async factory
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        with ctx(e) as msg:\n            raise HTTPException(400, detail=msg)\n",
            header=_DEFEAT_HEADER,
        ),
        id="D12c",  # with target
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        parts = []\n        for a in e.args:\n            parts.append(str(a))\n        raise HTTPException(400, detail=' '.join(parts))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D12d",  # for e.args
    ),
    pytest.param(
        _planted(
            "def r(a):\n    try: x()\n    except KeyError as e:\n        raise HTTPException(400 if a else 503, detail=str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D12e",  # IfExp status typed
    ),
    pytest.param(
        _planted(
            "def h(exc: Any):\n    return HTTPException(500, str(exc))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D12f",  # exc: Any
    ),
    pytest.param(
        _planted(
            "def h(exc: TeamOperationRefused):\n    raise HTTPException(500, detail=str(exc))\n",
            header=_DEFEAT_HEADER,
        ),
        id="D-class",  # TeamOperationRefused 5xx
    ),
    pytest.param(
        _planted(
            "def h(exc: BaseException):\n    for c in walk_cause_chain(exc):\n        if isinstance(c, LLMException):\n            return _llm_http(502, 'X', str(c), None, None)\n    return _llm_http(500, 'S', 'static', None, None)\n",
            header=_DEFEAT_HEADER,
            leak_line=4,
        ),
        id="D1b",  # for over chain in broad helper
    ),
    pytest.param(
        _real_helper_mutant(_ARM5, _D1_ARM + _ARM5, "str(c)"),
        id="D1",  # the real helper: a `for c in walk_cause_chain(exc)` arm returning str(c)
    ),
    pytest.param(
        _real_helper_mutant(_ARM5, _D3B_ARM + _ARM5, "SERVICE_ERROR:{exc}"),
        id="D3b",  # the real helper: str(exc) into _llm_http's error_code -> header
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
    # --- #1634's review ---
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        raise HTTPException(400, detail='static', headers={'Retry-After': '60'})\n",
            header=_DEFEAT_HEADER,
        ),
        id="NN2",  # broad static + static header
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except RateLimitError as e:\n        raise HTTPException(429, detail='slow down', headers={'Retry-After': str(e.retry_after)})\n",
            header=_DEFEAT_HEADER,
        ),
        id="NN3",  # typed 429 header from e
    ),
    pytest.param(
        _planted(
            "def _fail(msg):\n    log(msg)\n    raise HTTPException(400, detail='static')\ndef r():\n    try: x()\n    except Exception as e:\n        _fail(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="NN4",  # raising helper static detail
    ),
    pytest.param(
        _planted(
            "def h(exc: BaseException):\n    for c in walk_cause_chain(exc):\n        if isinstance(c, LLMException):\n            return _llm_http(502, 'X', 'static', None, None)\n    return _llm_http(500, 'S', 'static', None, None)\n",
            header=_DEFEAT_HEADER,
        ),
        id="NN5",  # for over chain, static detail
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except Exception as e:\n        logger.exception('x')\n        raise HTTPException(500, detail='Failed')\n",
            header=_DEFEAT_HEADER,
        ),
        id="NN6",  # broad, logged, static 500
    ),
    pytest.param(
        _planted(
            "def bad_request(detail, status_code=400):\n    return HTTPException(status_code=status_code, detail=detail)\ndef r():\n    try: x()\n    except ValidationException as e:\n        raise bad_request(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        # A factory's status parameter left at its default takes the default:
        # a 400 under a typed handler is the carve-out. Read as unknown it
        # failed closed, a false positive #1634's review found.
        id="D7",  # default 400 typed
    ),
    pytest.param(
        _planted(
            "def r():\n    try: x()\n    except ServiceException as e:\n        raise HTTPException(status_code=403, detail=str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        # Pending a ruling, not settled: "broad" is decided by class name, so
        # the generic wrapper `ServiceException` keeps the typed 4xx
        # carve-out although it carries whatever it wrapped. #1860 asks
        # whether to widen "broad"; if it is ruled so, this row moves.
        id="D6",  # ServiceException 403 (pending policy)
    ),
    pytest.param(
        _planted(
            "def _conflict(msg):\n    raise HTTPException(409, detail=msg)\ndef r():\n    try: x()\n    except ConflictError as e:\n        _conflict(str(e))\n",
            header=_DEFEAT_HEADER,
        ),
        id="NN7",  # typed raising helper 409
    ),
    pytest.param(
        _planted(
            "def h(payload: Any):\n    return HTTPException(500, str(payload))\n",
            header=_DEFEAT_HEADER,
        ),
        id="NN8",  # Any-annotated non-exception name
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
    the surface guard re-keys every reported line onto the statement holding
    it, its enclosing function and its expression (``_offender_keys``), and
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


@pytest.mark.unit
@pytest.mark.parametrize(
    "body, body_line, statement",
    [
        pytest.param(
            "def _fail(msg):\n    raise HTTPException(400, detail=msg)\ndef r():\n    try: x()\n    except Exception as e:\n        _fail(str(e))\n",
            6,
            "_fail(str(e))",
            id="D2",  # a raising helper called as a statement: an `Expr`
        ),
        pytest.param(
            "def r():\n    try: x()\n    except Exception as e:\n        err = HTTPException(400, detail=str(e))\n        raise err\n",
            4,
            "HTTPException(400, detail=str(e))",
            id="D12a",  # built and assigned, raised a line later: an `Assign`
        ),
    ],
)
def test_a_sink_that_is_not_a_raise_or_return_reaches_the_surface_guard(
    tmp_path, monkeypatch, body, body_line, statement
):
    """The two sinks #1634's review added that are neither ``raise`` nor
    ``return``, through the surface guard.

    The analysis reports the line of the CALL. ``_offender_keys`` used to
    build a key only for a ``Return``/``Raise`` starting on a reported line,
    so a raising helper called as a statement, or an ``HTTPException`` built
    into a local, was reported by the analysis and then dropped without a
    word — the guard passed with the finding in hand. Each must come out of
    the guard naming the statement that holds the call.
    """
    path = tmp_path / "faultmaven" / "api" / "planted.py"
    path.parent.mkdir(parents=True)
    path.write_text(_DEFEAT_HEADER + body, encoding="utf-8")
    monkeypatch.setattr(surface_guard, "_REPO", tmp_path)
    monkeypatch.setattr(surface_guard, "_surface", lambda: (path,))
    line = len(_DEFEAT_HEADER.splitlines()) + body_line

    with pytest.raises(AssertionError) as failure:
        surface_guard.test_no_api_surface_site_puts_the_caught_exception_on_the_wire()

    assert f"faultmaven/api/planted.py:{line} in r(): {statement}" in str(failure.value)
