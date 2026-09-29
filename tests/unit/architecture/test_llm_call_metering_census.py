"""Every billed LLM call site is metered, or says why it is not (#640).

The usage ledger is only as complete as the metering under it: a call that
never reaches ``record_provider_call`` is spend no row, no counter and no log
line will ever show. Metering happens at ONE chokepoint,
``ProviderRegistry.route_request``, which every call through the container's
``LLMRouter`` reaches. A call that goes to a concrete provider directly
bypasses it, and must meter itself.

So this census collects every place the package makes a provider call, in
every shape the codebase writes one:

* ``<x>.generate(...)`` and ``<x>.route_request(...)``;
* ``generate_with_truncation_retry(...)`` — the retry helper, whose callable
  argument makes the call;
* ``getattr(<x>, "generate")(...)`` (and ``"route_request"``);
* ``functools.partial(<x>.generate, ...)`` / ``partial(<x>.generate, ...)``.

Each ``(file, function)`` that holds one is declared below, with its count and
one classification:

* ``ROUTER`` — the receiver is the container's ``LLMRouter``, metered at the
  chokepoint. A DECLARATION: the scan cannot prove what a receiver is bound to.
* ``METERS_ITSELF`` — VERIFIED: the declared function (including functions
  nested in it) calls ``record_provider_call``.
* ``CHOKEPOINT`` — ``ProviderRegistry.route_request`` itself.
* ``UNMETERED`` — with the reason. Only the connection test.

A new site, a moved one or a changed count fails until it is declared. The
directory the rule can be violated in is declared too (``faultmaven/``), and the
scan fails if it read no files there or found none of the declared sites.

What this does NOT see, stated so nobody mistakes it for more:

* a receiver swapped to a concrete provider inside a function declared
  ``ROUTER``, with the call count unchanged — ``ROUTER`` is taken on trust;
* a call through a variable holding the bound method (``gen = p.generate;
  await gen()``), a ``getattr`` whose name is not a literal, and ``**kwargs``
  indirection that hands a provider's ``generate`` to something else to call;
* the retry helper imported under another name;
* a billed call made by a provider method other than ``generate`` — every
  provider bills through ``generate`` today (``BaseLLMProvider``).

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

#: The provider methods that bill.
_BILLED = frozenset({"generate", "route_request"})
_RETRY = "generate_with_truncation_retry"
_METER = "record_provider_call"

ROUTER = "ROUTER"
METERS_ITSELF = "METERS_ITSELF"
CHOKEPOINT = "CHOKEPOINT"
UNMETERED = "UNMETERED"

_GENERATION = "faultmaven/core/investigation/milestone_engine/generation.py"
_SUGGESTION = "faultmaven/modules/knowledge/domain/services/suggestion_service.py"
_TIER2 = "faultmaven/core/preprocessing/tier2/local_service.py"

#: (file, function) -> (sites, classification, reason). The reason is required
#: for UNMETERED and says, for the others, what the receiver is.
SITES: dict[tuple[str, str], tuple[int, str, str]] = {
    # The chokepoint: every router call's provider.generate().
    (
        "faultmaven/infrastructure/llm/providers/registry.py",
        "ProviderRegistry.route_request",
    ): (1, CHOKEPOINT, "the registry's own provider.generate()"),
    # The engine's tool loop: a dedicated DA provider is concrete, so the
    # closure meters it; with none set, the provider is the router.
    (_GENERATION, "StructuredOutputGenerator._tool_augmented_generate"): (
        1,
        METERS_ITSELF,
        "retry helper over _tool_loop_call, which meters",
    ),
    (
        _GENERATION,
        "StructuredOutputGenerator._tool_augmented_generate._tool_loop_call",
    ): (1, METERS_ITSELF, "meters a dedicated DA provider's call"),
    (
        _GENERATION,
        "StructuredOutputGenerator._generate_structured_output_inner.llm_operation",
    ): (1, ROUTER, "self.deps.llm_provider"),
    # Outside the engine.
    ("faultmaven/modules/case/api/title_generation.py", "_generate_title_with_llm"): (
        1,
        ROUTER,
        "app.state.llm_provider",
    ),
    (_SUGGESTION, "SuggestionService._generate_once"): (
        1,
        ROUTER,
        "retry helper over _call",
    ),
    (_SUGGESTION, "SuggestionService._generate_once._call"): (
        1,
        ROUTER,
        "self._llm_provider",
    ),
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
    (_TIER2, "LocalTier2Service.analyze"): (1, ROUTER, "retry helper over _analyze"),
    (_TIER2, "LocalTier2Service.analyze._analyze"): (1, ROUTER, "self.llm_client"),
    # The one billed call nothing meters.
    ("faultmaven/api/routes/admin_config.py", "check_llm_connection"): (
        1,
        UNMETERED,
        "one 50-token 'Say hello' an operator triggers; metering it is filed "
        "separately per #640's spec §7",
    ),
}


def _is_site(node: ast.AST) -> bool:
    """Whether ``node`` is a provider call in one of the collected shapes."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _BILLED:
        return True
    if (isinstance(func, ast.Name) and func.id == _RETRY) or (
        isinstance(func, ast.Attribute) and func.attr == _RETRY
    ):
        return True
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "getattr"
        and len(func.args) >= 2
        and isinstance(func.args[1], ast.Constant)
        and func.args[1].value in _BILLED
    ):
        return True
    is_partial = (isinstance(func, ast.Name) and func.id == "partial") or (
        isinstance(func, ast.Attribute) and func.attr == "partial"
    )
    return (
        is_partial
        and bool(node.args)
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr in _BILLED
    )


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

    return Counter(scope_of(node) for node in ast.walk(tree) if _is_site(node))


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
            "async def f(provider):\n    return await provider.generate(prompt='x')",
            "async def f(self, params):\n"
            "    return await self.llm_provider.generate(**params)",
            "async def f(self):\n    return await self.deps.llm_provider.generate()",
            "async def f(registry):\n    return await registry.route_request('x')",
            "async def f(call):\n"
            "    return await generate_with_truncation_retry(call, max_tokens=5)",
            "async def f(truncation, call):\n"
            "    return await truncation.generate_with_truncation_retry(call)",
            "async def f(p):\n    return await getattr(p, 'generate')(prompt='x')",
            "def f(p):\n    return functools.partial(p.generate, prompt='x')",
            "def f(p):\n    return partial(p.generate, prompt='x')",
            "async def f(p):\n"
            "    return await asyncio.wait_for(p.generate(prompt='x'), 5)",
        ],
    )
    def test_every_call_shape_is_collected(self, snippet):
        assert sum(call_sites(snippet).values()) == 1, snippet

    @pytest.mark.parametrize(
        "snippet",
        [
            # Defining generate is not calling it.
            "class P:\n    async def generate(self, prompt):\n        return 1",
            # A string that mentions the shape.
            "def f():\n    return 'await provider.generate(prompt=x)'",
            '"""Example:\n    >>> await llm_provider.generate(prompt="x")\n"""',
            # The router's own entry point, and other methods that bill nothing.
            "async def f(router):\n    return await router.route(prompt='x')",
            "async def f(p):\n    return await p.generate_stream('x')",
            "def f(fn):\n    return partial(fn, prompt='x')",
            "async def f(p):\n    return await getattr(p, 'is_available')()",
        ],
    )
    def test_non_calls_are_not_collected(self, snippet):
        assert not call_sites(snippet), snippet

    def test_a_site_is_attributed_to_its_innermost_function(self):
        src = (
            "class C:\n    async def m(self, p):\n"
            "        async def inner():\n            return await p.generate()\n"
            "        return await generate_with_truncation_retry(inner)"
        )
        assert call_sites(src) == Counter({"C.m.inner": 1, "C.m": 1})


class TestTheMeterCheck:
    def test_a_call_to_the_meter_counts(self):
        fn = _parse(
            "async def f(p):\n    r = await p.generate()\n"
            "    record_provider_call('x', 'm', r, 1.0)\n    return r"
        ).body[0]
        assert _meters(fn)

    def test_a_nested_closure_that_meters_counts(self):
        fn = _parse(
            "async def f(p):\n    async def call():\n"
            "        r = await p.generate()\n"
            "        metering.record_provider_call('x', 'm', r, 1.0)\n"
            "        return r\n"
            "    return await generate_with_truncation_retry(call)"
        ).body[0]
        assert _meters(fn)

    def test_a_mention_is_not_a_call(self):
        fn = _parse(
            "async def f(p):\n    meter = record_provider_call\n"
            "    return await p.generate()"
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


def test_there_is_one_chokepoint_and_every_classification_is_known():
    kinds = Counter(v[1] for v in SITES.values())
    assert kinds[CHOKEPOINT] == 1
    assert set(kinds) <= {ROUTER, METERS_ITSELF, CHOKEPOINT, UNMETERED}
    for key, (_count, kind, reason) in SITES.items():
        assert reason.strip(), f"{key} has no reason"
        if kind == UNMETERED:
            assert "#640" in reason, f"{key}: an unmetered site names its ruling"
