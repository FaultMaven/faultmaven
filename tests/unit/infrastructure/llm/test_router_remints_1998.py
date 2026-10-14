"""A routed call's minted content reaches the provider still minted (#1998, A2).

``LLMRouter.route`` runs its own ``SANITIZE_PII`` pass over the prompt and the
messages. ``DataSanitizer`` returns plain ``str``/``dict`` values, so before
``sanitizer_pass`` re-minted them, every routed call would have reached the
provider boundary as raw content under ``SANITIZE_PII=true``, minted by its
caller or not. These tests drive the real router over a real registry and a
real provider boundary — only the provider's ``_generate`` is a double — under
both settings of the flag, and read the boundary's own verdict: no
``RawModelContentWarning``, no metric, and the values the boundary was handed.

They also pin the other half of the pass: raw content is still sanitized (no
regression in what reaches a provider), and is still reported raw.
"""

from __future__ import annotations

import sys
import warnings
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from faultmaven.infrastructure.llm.providers import base
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    RawModelContentWarning,
    StopReason,
)
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.security.model_content import provenance
from tests.model_content import vetted, vetted_messages
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned

pytestmark = [pytest.mark.unit, pytest.mark.llm, pytest.mark.security]

PROVIDER = "stubprovider"
MODEL = "stub-model"
IP = "10.1.2.3"


class _RecordingProvider(BaseLLMProvider):
    """A provider whose transport records what the boundary handed it."""

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
def boundary(monkeypatch):
    """What the boundary was handed, and what it reported."""
    seen: List[Dict[str, Any]] = []
    check = BaseLLMProvider._model_content_violations

    def spy(self, prompt, kwargs):
        seen.append({"prompt": prompt, **kwargs})
        return check(self, prompt, kwargs)

    monkeypatch.setattr(BaseLLMProvider, "_model_content_violations", spy)
    with patch.object(base, "llm_model_content_violations") as metric:
        yield seen, metric


def _minted_messages() -> List[Dict[str, Any]]:
    return vetted_messages(
        [
            {"role": "system", "content": "fixed instructions"},
            {"role": "user", "content": f"host {IP} is down"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": f'{{"q": "{IP}"}}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "search_file",
                "content": f"{IP} refused",
            },
        ]
    )


def _raw_warnings(recorded) -> list:
    return [w for w in recorded if issubclass(w.category, RawModelContentWarning)]


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_a_minted_prompt_reaches_the_provider_minted(
    router, provider, boundary, sanitize_pii
):
    seen, metric = boundary
    with (
        sanitize_pii_pinned(sanitize_pii),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await router.route(prompt=vetted(f"host {IP} is down"), max_tokens=50)

    assert not _raw_warnings(recorded)
    metric.labels.assert_not_called()
    [handed] = seen
    assert provenance(handed["prompt"]) is not None
    sent = provider.sent[0]["prompt"]
    assert type(sent) is str
    assert (IP in sent) is (not sanitize_pii)


@pytest.mark.parametrize("sanitize_pii", [True, False], ids=["pii_on", "pii_off"])
async def test_minted_messages_reach_the_provider_minted(
    router, provider, boundary, sanitize_pii
):
    seen, metric = boundary
    with (
        sanitize_pii_pinned(sanitize_pii),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await router.route(messages=_minted_messages(), max_tokens=50)

    assert not _raw_warnings(recorded)
    metric.labels.assert_not_called()
    [handed] = seen
    messages = handed["messages"]
    assert all(provenance(m["content"]) is not None for m in messages)
    arguments = messages[2]["tool_calls"][0]["function"]["arguments"]
    assert provenance(arguments)[0] == "model_output"
    if sanitize_pii:
        assert all(provenance(m["content"])[1] is True for m in messages)
    # The transport sees plain values, redacted or not per the flag.
    for message in provider.sent[0]["messages"]:
        assert type(message["content"]) is str
        assert (IP in message["content"]) is (
            not sanitize_pii and message["role"] in {"user", "tool"}
        )


async def test_router_generate_forwards_minted_content(router, boundary):
    """``LLMRouter.generate`` — what the engine's tool loop calls — too."""
    seen, metric = boundary
    with sanitize_pii_pinned(True), warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        await router.generate(prompt=None, messages=_minted_messages(), max_tokens=50)
    assert not _raw_warnings(recorded)
    metric.labels.assert_not_called()


async def test_raw_content_is_still_sanitized_and_still_reported(
    router, provider, boundary
):
    seen, metric = boundary
    with sanitize_pii_pinned(True), pytest.warns(RawModelContentWarning):
        await router.route(
            messages=[{"role": "user", "content": f"host {IP} is down"}],
            max_tokens=50,
        )
    assert IP not in provider.sent[0]["messages"][0]["content"]
    [call] = metric.labels.call_args_list
    assert call.kwargs["argument"] == "messages.content"
    assert call.kwargs["kind"] == "raw"


async def test_what_the_router_keeps_is_plain_text(router, provider):
    """The cache, the telemetry and the response's fields never hold a ModelText."""
    with sanitize_pii_pinned(False):
        response = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        assert type(response.sanitized_prompt) is str
        again = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        assert len(provider.sent) == 1, "the second call is answered from the cache"
        assert type(again.sanitized_prompt) is str
        response = await router.route(messages=_minted_messages(), max_tokens=50)
    assert all(type(m["content"]) is str for m in response.sanitized_messages)


async def test_raw_telemetry_fields_are_plain_text(router, monkeypatch):
    """With raw-prompt logging on, ``raw_prompt``/``raw_messages`` are plain ``str``."""
    monkeypatch.setattr(router.settings.observability, "opik_log_raw_prompts", True)
    with sanitize_pii_pinned(False):
        first = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        cached = await router.route(
            prompt=vetted("cache me"), model=MODEL, max_tokens=50
        )
        routed = await router.route(messages=_minted_messages(), max_tokens=50)
    assert type(first.raw_prompt) is str
    assert type(cached.raw_prompt) is str
    assert all(type(m["content"]) is str for m in routed.raw_messages)
    arguments = routed.raw_messages[2]["tool_calls"][0]["function"]["arguments"]
    assert type(arguments) is str


async def test_a_routed_call_names_the_line_that_called_the_router(router):
    """The registry reaches the boundary in a task of its own; the warning
    still names the caller of ``route``, from what the router recorded."""
    import inspect

    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning) as recorded:
        line = inspect.currentframe().f_lineno + 1
        await router.route(prompt="raw text", max_tokens=50)
    [warning] = _raw_warnings(recorded)
    assert (warning.filename, warning.lineno) == (__file__, line)


async def test_a_send_its_caller_put_in_a_task_falls_back_to_the_dispatch(router):
    """The stated limit: ``asyncio.wait_for(router.route(...))`` on 3.11 runs
    ``route`` as a task's root, so no frame reaches the caller; the warning
    names the registry's dispatch, ``generate``'s direct caller."""
    import asyncio

    with sanitize_pii_pinned(False), pytest.warns(RawModelContentWarning) as recorded:
        await asyncio.wait_for(router.route(prompt="raw text", max_tokens=50), 5)
    [warning] = _raw_warnings(recorded)
    if sys.version_info >= (3, 12):  # wait_for creates no task from 3.12
        assert warning.filename == __file__
    else:
        assert warning.filename.endswith(
            "faultmaven/infrastructure/llm/providers/registry.py"
        )
