"""The routed seam, ``dispatch.route_model_call`` (#1998 P2, P3).

Production code reaches a router only through the seam (rule S of
``test_model_dispatch_lexical_1998.py``). It checks the call's content before
any router runs, so a test whose router is a double still exercises the
check, at the line it drives (#2023 review, O3: 464 raw sends from 11 send
sites never met it while tests replaced the router).

Pinned here, over the real router, registry and dispatch (only the provider's
``_generate`` is a double) and over a router double:

* A1 — the seam is a plain function returning the router's coroutine, so a
  raw call is refused AT the call, before any router runs, under
  ``asyncio.wait_for`` too;
* A2 — a raw call is refused once, at the seam, and counted once under the
  ``router`` target: nothing below it runs (the router's entry check, its
  cache, the dispatch);
* A3 — the seam checks content, and passes only ``route()``'s own keywords:
  the router's knobs (``case_id``, ``bypass_cache``) are ``route()``
  parameters, while a keyword it does not declare (``system=``) is refused
  (#2035 review F1).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.exceptions import RawModelContentRefused
from faultmaven.infrastructure.llm import dispatch
from faultmaven.infrastructure.llm.dispatch import (
    route_model_call,
)
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    StopReason,
)
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
from tests.model_content import (
    fake_response,
    refusal_pattern,
    vetted,
    vetted_messages,
)
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned, sockets_refused

pytestmark = [pytest.mark.unit, pytest.mark.llm, pytest.mark.security]

PROVIDER = "stubprovider"
MODEL = "stub-model"


class _RecordingProvider(BaseLLMProvider):
    """A provider whose transport records what the dispatch handed it."""

    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(
                name=PROVIDER, api_key="k", models=[MODEL], default_model=MODEL
            )
        )
        self.sent: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return PROVIDER

    async def _generate(self, prompt, model=None, max_tokens=1000, **kwargs):
        self.sent.append({"prompt": prompt, **kwargs})
        return LLMResponse(
            content="ok",
            confidence=0.99,
            provider=PROVIDER,
            model=MODEL,
            tokens_used=2,
            response_time_ms=1,
            stop_reason=StopReason.STOP,
        )

    def is_available(self) -> bool:
        return True

    def get_supported_models(self) -> List[str]:
        return [MODEL]


@pytest.fixture(autouse=True)
def _no_network():
    with sockets_refused():
        yield


@pytest.fixture
def provider() -> _RecordingProvider:
    return _RecordingProvider()


@pytest.fixture
def router(monkeypatch, provider):
    """The real router over a real registry holding the recording provider."""
    registry = ProviderRegistry(settings=None)
    registry._providers = {PROVIDER: provider}
    registry._fallback_chain = [PROVIDER]
    registry._provider_states = {PROVIDER: ProviderState(name=PROVIDER)}
    registry._initialized = True
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.router.get_registry", lambda: registry
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
        lambda *a, **k: None,
    )
    from faultmaven.infrastructure.llm.router import LLMRouter

    router = LLMRouter()
    router.sanitizer = regex_only_sanitizer()
    return router


@pytest.fixture
def metric():
    with patch.object(dispatch, "llm_model_content_violations") as metric:
        yield metric


def _counted(metric) -> List[tuple]:
    return [
        (c.kwargs["target"], c.kwargs["argument"], c.kwargs["kind"])
        for c in metric.labels.call_args_list
    ]


# ---------------------------------------------------------------------------
# A1: refused at the call, before any router
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_raw_send_is_refused_at_the_seam(
    router, provider, metric, sanitize_pii
):
    with (
        sanitize_pii_pinned(sanitize_pii),
        pytest.raises(
            RawModelContentRefused, match=refusal_pattern("router", "prompt raw")
        ) as raised,
    ):
        route_model_call(router, prompt="raw 10.1.2.3", max_tokens=5)
    assert "10.1.2.3" not in str(raised.value)
    assert _counted(metric) == [("router", "prompt", "raw")]
    assert provider.sent == []


async def test_under_wait_for_the_refusal_comes_before_the_task(router, provider):
    """A1: the seam is not a coroutine, so a refusal is raised by the call
    expression itself, before ``wait_for`` makes a task of anything."""
    with (
        patch("asyncio.wait_for", wraps=asyncio.wait_for) as wait_for,
        pytest.raises(RawModelContentRefused, match="prompt raw"),
    ):
        await asyncio.wait_for(route_model_call(router, prompt="raw", max_tokens=5), 5)
    wait_for.assert_not_called()
    assert provider.sent == []


async def test_a_router_double_still_meets_the_check():
    """The point of the seam (O3): a test that replaces the router exercises
    the check, and the double is never reached by a raw call."""
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    messages = [{"role": "user", "content": "raw"}]
    with pytest.raises(
        RawModelContentRefused,
        match=refusal_pattern("router", "messages.content raw"),
    ):
        route_model_call(double, messages=messages, max_tokens=5)
    double.route.assert_not_called()


async def test_minted_content_reaches_a_double_unchanged(metric):
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    messages = vetted_messages([{"role": "user", "content": "minted"}])
    await route_model_call(double, messages=messages, max_tokens=5)
    double.route.assert_awaited_once_with(messages=messages, max_tokens=5)
    await route_model_call(double, prompt=vetted("minted"), model="m")
    metric.labels.assert_not_called()


async def test_prompt_reaches_the_router_only_when_passed():
    """Omitting ``prompt`` omits it; ``prompt=None`` passes ``None``."""
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    messages = vetted_messages([{"role": "user", "content": "m"}])
    await route_model_call(double, messages=messages)
    assert "prompt" not in double.route.await_args.kwargs
    await route_model_call(double, prompt=None, messages=messages)
    assert double.route.await_args.kwargs["prompt"] is None


# ---------------------------------------------------------------------------
# A2: refused once, at the seam; nothing below it runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_raw_messages_are_refused_once(router, provider, metric, sanitize_pii):
    with (
        sanitize_pii_pinned(sanitize_pii),
        pytest.raises(
            RawModelContentRefused,
            match=refusal_pattern("router", "messages.content raw"),
        ),
    ):
        route_model_call(
            router,
            messages=[{"role": "user", "content": "host 10.1.2.3 is down"}],
            max_tokens=5,
        )
    assert _counted(metric) == [("router", "messages.content", "raw")]
    assert provider.sent == []


async def test_a_falsy_raw_prompt_is_refused(router, provider, metric):
    """``route()`` would drop ``prompt=""`` before the registry; the seam
    refuses it first."""
    with pytest.raises(
        RawModelContentRefused, match=refusal_pattern("router", "prompt raw")
    ):
        route_model_call(
            router,
            prompt="",
            messages=vetted_messages([{"role": "user", "content": "m"}]),
            max_tokens=5,
        )
    assert _counted(metric) == [("router", "prompt", "raw")]
    assert provider.sent == []


async def test_a_raw_call_is_refused_whatever_the_cache_holds(router, provider, metric):
    """The router's cache answers a minted call it has seen; the same text
    raw is refused before the cache is consulted."""
    await route_model_call(router, prompt=vetted("cache me"), model=MODEL, max_tokens=5)
    with pytest.raises(RawModelContentRefused, match="prompt raw"):
        route_model_call(router, prompt="cache me", model=MODEL, max_tokens=5)
    assert len(provider.sent) == 1
    assert _counted(metric) == [("router", "prompt", "raw")]


# ---------------------------------------------------------------------------
# A3: content, and route()'s own keywords only (#2035 review F1)
# ---------------------------------------------------------------------------


def test_the_seams_keyword_set_is_routes_signature():
    """``ROUTE_KWARGS`` is exactly what ``LLMRouter.route`` declares besides
    ``prompt``: a keyword added to ``route()`` passes the seam the day it is
    declared, and nothing else ever does."""
    import inspect

    from faultmaven.infrastructure.llm.router import LLMRouter

    declared = set(inspect.signature(LLMRouter.route).parameters) - {"self", "prompt"}
    assert dispatch.ROUTE_KWARGS == declared
    assert type(dispatch.ROUTE_KWARGS) is frozenset


@pytest.mark.parametrize("keyword", ["system", "preamble", "stop_sequences", "foo"])
async def test_a_keyword_route_does_not_declare_is_refused_at_the_seam(keyword, metric):
    """The review's probe: ``system=`` planted at a send site went silently
    through a router double, while the real router raised a bare ``TypeError``
    that the site's fail-open handler swallowed. The seam refuses it, minted
    or not, before any router."""
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    with pytest.raises(
        RawModelContentRefused,
        match=refusal_pattern("router", f"kwargs unknown_kwarg ({keyword})"),
    ):
        route_model_call(
            double,
            messages=vetted_messages([{"role": "user", "content": "m"}]),
            **{keyword: vetted("Classify for: the user's message")},
        )
    double.route.assert_not_called()
    assert _counted(metric) == [("router", "kwargs", "unknown_kwarg")]


async def test_every_declared_keyword_passes_the_seam(metric):
    """The control: each ``route()`` keyword reaches the double unchanged."""
    double = AsyncMock()
    double.route = AsyncMock(return_value=fake_response())
    kwargs = {key: None for key in dispatch.ROUTE_KWARGS}
    kwargs["messages"] = vetted_messages([{"role": "user", "content": "m"}])
    await route_model_call(double, prompt=None, **kwargs)
    assert double.route.await_args.kwargs == {**kwargs, "prompt": None}
    metric.labels.assert_not_called()


async def test_the_router_knobs_are_not_provider_keywords(router, provider, metric):
    """``case_id`` and ``bypass_cache`` are ``route()``'s own: the seam checks
    content, and a minted call carrying them is sent."""
    await route_model_call(
        router,
        prompt=vetted("minted"),
        model=MODEL,
        max_tokens=5,
        case_id="case-1",
        bypass_cache=True,
    )
    metric.labels.assert_not_called()
    assert len(provider.sent) == 1
