"""Every billed LLM call site is metered, or says why it is not (#640).

The usage ledger is only as complete as the metering under it: a call that
never reaches ``record_provider_call`` is spend no row, no counter and no log
line will ever show. Metering happens at ONE chokepoint,
``ProviderRegistry.route_request``, which every call through the container's
``LLMRouter`` reaches. A call that goes to a concrete provider directly
(``dispatch.send_to_model``) bypasses it, and must meter itself.

So this census collects every place the package reaches a billed send, in
every shape the codebase writes one:

* ``send_to_model(...)`` — the model dispatch, the one way to a provider
  (#1998) — called, or read without being called (passed, bound with
  ``partial``, stored);
* ``<x>._generate`` and ``<x>.route_request``, called or read WITHOUT being
  called — a bound method stored, passed (``call_func=...``) or bound
  (``partial(...)``, under any name ``partial`` is imported as). An assignment
  target is not a read;
* ``getattr(<x>, "_generate")`` (and ``"route_request"``,
  ``"send_to_model"``), called on the spot or not, and
  ``methodcaller("_generate")``;
* ``generate_with_truncation_retry(...)`` — the retry helper, whose callable
  argument makes the call.

Each ``(file, function)`` that holds one is declared below, with its count and
one classification:

* ``ROUTER`` — the receiver is the container's ``LLMRouter``, metered at the
  chokepoint. A DECLARATION: the scan cannot prove what a receiver is bound to.
* ``METERS_ITSELF`` — the declared function (including functions nested in it)
  calls ``record_provider_call``. This proves the meter is PRESENT, not that
  the call path reaches it: a meter behind a dead branch still passes here.
  The path proof for the one such site, the dedicated DA provider's tool-loop
  call, is behavioural —
  ``tests/integration/test_llm_usage_ledger_coverage.py::TestAnEngineTurn::test_the_da_direct_call_is_metered_once``
  and
  ``tests/unit/core/investigation/test_milestone_engine_tool_loop.py::TestToolLoopTruncationLadder::test_the_retry_is_metered_too``
  both fail when the meter is put behind ``if False:``.
* ``CHOKEPOINT`` — ``ProviderRegistry.route_request`` itself, and the router
  handing it to ``call_external``.
* ``DISPATCH`` — ``send_to_model``'s own call of a provider's ``_generate``:
  metered by whoever called the dispatch, and every caller is declared here.
* ``UNMETERED`` — with the reason. Only the connection test.

A new site, a moved one or a changed count fails until it is declared. The
directory the rule can be violated in is declared too (``faultmaven/``), and the
scan fails if it read no files there or found none of the declared sites.

What this does NOT see, stated so nobody mistakes it for more:

* a receiver swapped to a concrete provider inside a function declared
  ``ROUTER``, with the count unchanged — ``ROUTER`` is taken on trust;
* a provider's private transport reached from outside the provider
  (``OpenAIProvider._post_chat_completion`` and its siblings) — refused by the
  dispatch's lexical rule (A4, ``test_model_dispatch_lexical_1998.py``) — or
  raw HTTP to a provider's API from anywhere else;
* a billed provider method not named ``_generate`` — every provider bills
  through ``_generate`` today (``BaseLLMProvider``), and a future one (a
  streaming method, say) is NOT classified free in advance: it must be added to
  the collected names when it is written;
* a ``getattr`` or ``methodcaller`` whose name is not a literal, and the retry
  helper imported under another name.

It also does not see ``.route(...)``: that is ``LLMRouter``'s own entry point,
no provider has one, and everything it calls goes through the chokepoint.
"""

from __future__ import annotations

import ast
import warnings
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.architecture]

_ROOT = Path(__file__).resolve().parents[3]
#: Where the rule can be violated: every billed call is made from the package.
_PACKAGE = _ROOT / "faultmaven"

#: The provider methods that bill, and the dispatch every send goes through.
_BILLED = frozenset({"_generate", "route_request"})
_DISPATCH = "send_to_model"
_RETRY = "generate_with_truncation_retry"
_METER = "record_provider_call"

ROUTER = "ROUTER"
METERS_ITSELF = "METERS_ITSELF"
CHOKEPOINT = "CHOKEPOINT"
DISPATCH = "DISPATCH"
UNMETERED = "UNMETERED"

_GENERATION = "faultmaven/core/investigation/milestone_engine/generation.py"
_TIER2 = "faultmaven/core/preprocessing/tier2/local_service.py"

#: (file, function) -> (sites, classification, reason). The reason is required
#: for UNMETERED and says, for the others, what the receiver is.
SITES: dict[tuple[str, str], tuple[int, str, str]] = {
    # The chokepoint: every router call's send.
    (
        "faultmaven/infrastructure/llm/providers/registry.py",
        "ProviderRegistry.route_request",
    ): (1, CHOKEPOINT, "the registry's own send_to_model(provider)"),
    # ...and the router handing it on, as a bound method, to call_external.
    ("faultmaven/infrastructure/llm/router.py", "LLMRouter.route"): (
        1,
        CHOKEPOINT,
        "the chokepoint handed to call_external",
    ),
    # The dispatch: the one call of a provider's _generate.
    ("faultmaven/infrastructure/llm/dispatch.py", "send_to_model"): (
        1,
        DISPATCH,
        "provider._generate after the check; metered by its declared callers",
    ),
    # The engine's tool loop: a dedicated DA provider is concrete, so the
    # closure sends through the dispatch and meters it; with none set, the
    # call is router.route (not collected: it reaches the chokepoint).
    (_GENERATION, "StructuredOutputGenerator._tool_augmented_generate"): (
        1,
        METERS_ITSELF,
        "retry helper over _tool_loop_call, which meters",
    ),
    (
        _GENERATION,
        "StructuredOutputGenerator._tool_augmented_generate._tool_loop_call",
    ): (1, METERS_ITSELF, "meters a dedicated DA provider's call"),
    # Outside the engine.
    (
        "faultmaven/modules/knowledge/domain/services/conversion_service/pipeline.py",
        "_analyze_document",
    ): (1, ROUTER, "retry helper over llm_router.route"),
    (
        "faultmaven/modules/knowledge/domain/services/conversion_service/service.py",
        "ConversionService._convert_single_failure_mode",
    ): (1, ROUTER, "retry helper over self._llm_router.route"),
    (
        "faultmaven/modules/agent/tools/document_qa_tool.py",
        "DocumentQATool.answer_question",
    ): (1, ROUTER, "retry helper over self._llm_router.route"),
    (_TIER2, "LocalTier2Service.analyze"): (
        1,
        ROUTER,
        "retry helper over self.llm_client.route",
    ),
    # The one billed call nothing meters: the admin connection check's send.
    (
        "faultmaven/infrastructure/llm/providers/registry.py",
        "ProviderRegistry.send_connection_check",
    ): (
        1,
        UNMETERED,
        "one 50-token 'Say hello' an operator triggers; metering it is filed "
        "separately per #640's spec §7",
    ),
}


def _names(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_billed(node: ast.Call, index: int) -> bool:
    return (
        len(node.args) > index
        and isinstance(node.args[index], ast.Constant)
        and node.args[index].value in _BILLED | {_DISPATCH}
    )


def _is_site(node: ast.AST, parents: dict) -> bool:
    """Whether ``node`` reaches a billed send in a collected shape."""
    if isinstance(node, ast.Attribute):
        # ``x._generate`` / ``dispatch.send_to_model`` read, whether called on
        # the spot or not; a store or a del is not a read. The call form is
        # counted HERE, once — its Call node is not counted again below.
        return node.attr in _BILLED | {_DISPATCH} and isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Name):
        # ``send_to_model`` imported by name: read, called or not.
        return node.id == _DISPATCH and isinstance(node.ctx, ast.Load)
    if not isinstance(node, ast.Call):
        return False
    name = _names(node.func)
    if name == _RETRY:
        return True
    if name == "getattr":
        return _literal_billed(node, 1)
    if name == "methodcaller":
        return _literal_billed(node, 0)
    return False


def _parse(source: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", (DeprecationWarning, SyntaxWarning))
        return ast.parse(source)


def call_sites(source: str) -> Counter:
    """``{function qualname: provider calls}`` for one module's source."""
    tree = _parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }

    def scope_of(node: ast.AST) -> str:
        names = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
        return ".".join(reversed(names)) or "<module>"

    return Counter(scope_of(node) for node in ast.walk(tree) if _is_site(node, parents))


@lru_cache(maxsize=1)
def _scan() -> tuple[Counter, int]:
    sites: Counter = Counter()
    files = 0
    for path in sorted(_PACKAGE.rglob("*.py")):
        files += 1
        rel = path.relative_to(_ROOT).as_posix()
        for scope, count in call_sites(path.read_text(encoding="utf-8")).items():
            sites[(rel, scope)] = count
    return sites, files


def _function(path: str, qualname: str) -> ast.AST:
    """The def or class at ``qualname`` — the same naming ``call_sites`` uses,
    so a closure nested inside an ``if`` or a loop is found by its scope."""
    tree = _parse((_ROOT / path).read_text(encoding="utf-8"))
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        names, cursor = [node.name], node
        while cursor in parents:
            cursor = parents[cursor]
            if isinstance(
                cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                names.append(cursor.name)
        if ".".join(reversed(names)) == qualname:
            return node
    raise AssertionError(f"{path}::{qualname} not found")


def _meters(fn: ast.AST) -> bool:
    """Whether ``fn`` — or a function nested in it — calls the meter."""
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == _METER)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == _METER)
        )
        for node in ast.walk(fn)
    )


class TestTheDetector:
    """Reach first: a shape the scan misses is a site it reports clean."""

    @pytest.mark.parametrize(
        "snippet",
        [
            "async def f(provider):\n"
            "    return await send_to_model(provider, prompt='x')",
            "async def f(self, params):\n"
            "    return await dispatch.send_to_model(self.provider, **params)",
            "async def f(provider):\n    return await provider._generate(prompt='x')",
            "def f(p):\n    return partial(send_to_model, p)",
            "async def f(p):\n    return await getattr(dispatch, 'send_to_model')(p)",
            "async def f(registry):\n    return await registry.route_request('x')",
            "async def f(call):\n"
            "    return await generate_with_truncation_retry(call, max_tokens=5)",
            "async def f(truncation, call):\n"
            "    return await truncation.generate_with_truncation_retry(call)",
            "async def f(p):\n    return await getattr(p, '_generate')(prompt='x')",
            "def f(p):\n    return functools.partial(p._generate, prompt='x')",
            "def f(p):\n    return partial(p._generate, prompt='x')",
            "async def f(p):\n"
            "    return await asyncio.wait_for(send_to_model(p, prompt='x'), 5)",
            # A billed method read without being called: stored, passed, bound.
            "async def f(p):\n    gen = p._generate\n    return await gen(prompt='x')",
            "async def f(self):\n"
            "    return await self.call_external(call_func=self.registry.route_request)",
            "from functools import partial as bind\n"
            "def f(p):\n    return bind(p._generate, prompt='x')",
            "def f(p):\n    m = getattr(p, '_generate')\n    return m",
            "def f():\n    return operator.methodcaller('_generate', prompt='x')",
            "def f():\n    return methodcaller('route_request', 'x')",
        ],
    )
    def test_every_call_shape_is_collected(self, snippet):
        assert sum(call_sites(snippet).values()) == 1, snippet

    @pytest.mark.parametrize(
        "snippet",
        [
            # Defining the send is not calling it.
            "class P:\n    async def _generate(self, prompt):\n        return 1",
            "async def send_to_model(provider, **kw):\n    return 1",
            # A string that mentions the shape.
            "def f():\n    return 'await send_to_model(provider, prompt=x)'",
            '"""Example:\n    >>> await send_to_model(provider, prompt="x")\n"""',
            # A public generate is no longer a send: nothing calls it.
            "async def f(p):\n    return await p.generate(prompt='x')",
            # The router's own entry point, and other methods that bill nothing.
            "async def f(router):\n    return await router.route(prompt='x')",
            "def f(fn):\n    return partial(fn, prompt='x')",
            "async def f(p):\n    return await getattr(p, 'is_available')()",
            # Writing the attribute is not reading the method.
            "def f(p, mock):\n    p._generate = mock",
            "def f(p):\n    del p._generate",
        ],
    )
    def test_non_calls_are_not_collected(self, snippet):
        assert not call_sites(snippet), snippet

    def test_a_site_is_attributed_to_its_innermost_function(self):
        src = (
            "class C:\n    async def m(self, p):\n"
            "        async def inner():\n            return await send_to_model(p)\n"
            "        return await generate_with_truncation_retry(inner)"
        )
        assert call_sites(src) == Counter({"C.m.inner": 1, "C.m": 1})


class TestTheMeterCheck:
    def test_a_call_to_the_meter_counts(self):
        fn = _parse(
            "async def f(p):\n    r = await send_to_model(p)\n"
            "    record_provider_call('x', 'm', r, 1.0)\n    return r"
        ).body[0]
        assert _meters(fn)

    def test_a_nested_closure_that_meters_counts(self):
        fn = _parse(
            "async def f(p):\n    async def call():\n"
            "        r = await send_to_model(p)\n"
            "        metering.record_provider_call('x', 'm', r, 1.0)\n"
            "        return r\n"
            "    return await generate_with_truncation_retry(call)"
        ).body[0]
        assert _meters(fn)

    def test_a_mention_is_not_a_call(self):
        fn = _parse(
            "async def f(p):\n    meter = record_provider_call\n"
            "    return await send_to_model(p)"
        ).body[0]
        assert not _meters(fn)


def test_the_scan_covers_the_package():
    """A positive control: a walk that matched nothing would declare nothing."""
    sites, files = _scan()
    assert files > 400, f"scanned {files} files under {_PACKAGE}"
    assert set(SITES) <= set(sites), "a declared site was not found"


def test_every_site_is_declared():
    sites, _ = _scan()
    declared = {key: value[0] for key, value in SITES.items()}

    undeclared = {k: n for k, n in sites.items() if k not in declared}
    stale = sorted(k for k in declared if k not in sites)
    recounted = {
        k: (declared[k], n)
        for k, n in sites.items()
        if k in declared and declared[k] != n
    }

    assert not undeclared, (
        "new LLM provider calls:\n  "
        + "\n  ".join(f"{f}::{s} ({n})" for (f, s), n in sorted(undeclared.items()))
        + "\nA call through the container's LLMRouter is metered at "
        "registry.route_request: declare it ROUTER. A call to a concrete provider "
        "bypasses that and must call record_provider_call itself: declare it "
        "METERS_ITSELF. Anything else is spend the usage ledger never sees."
    )
    assert not stale, f"declared but no longer found (moved or renamed?): {stale}"
    assert not recounted, (
        "the number of provider calls changed (declared, found) — look at the "
        f"new one before updating the count: {recounted}"
    )


@pytest.mark.parametrize(
    "site",
    sorted(k for k, v in SITES.items() if v[1] == METERS_ITSELF),
    ids=lambda s: s[1],
)
def test_every_self_metering_site_meters(site):
    path, qualname = site
    assert _meters(
        _function(path, qualname)
    ), f"{path}::{qualname} is declared METERS_ITSELF but never calls {_METER}"


def test_the_chokepoint_is_one_method_and_every_classification_is_known():
    """``CHOKEPOINT`` names the registry's method and the one hand-off of it."""
    kinds = Counter(v[1] for v in SITES.values())
    assert {k for k, v in SITES.items() if v[1] == CHOKEPOINT} == {
        (
            "faultmaven/infrastructure/llm/providers/registry.py",
            "ProviderRegistry.route_request",
        ),
        ("faultmaven/infrastructure/llm/router.py", "LLMRouter.route"),
    }
    assert {k for k, v in SITES.items() if v[1] == DISPATCH} == {
        ("faultmaven/infrastructure/llm/dispatch.py", "send_to_model"),
    }
    assert set(kinds) <= {ROUTER, METERS_ITSELF, CHOKEPOINT, DISPATCH, UNMETERED}
    for key, (_count, kind, reason) in SITES.items():
        assert reason.strip(), f"{key} has no reason"
        if kind == UNMETERED:
            assert "#640" in reason, f"{key}: an unmetered site names its ruling"
