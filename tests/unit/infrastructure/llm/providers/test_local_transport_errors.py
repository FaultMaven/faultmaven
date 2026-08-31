"""fm#1287 — the local provider types transport failures on ALL THREE transports.

``LocalProvider`` is the one provider with more than one wire path, chosen at
call time from the base URL and model name:

* ``_call_ollama_api``            — POST {base}/api/generate
* ``_call_openai_compatible_api`` — POST {base}/chat/completions (vLLM, llama.cpp+OAI)
* ``_call_llamacpp_api``          — POST {base}/completion (fallback when that 404s)

Only the middle one had ``except asyncio.TimeoutError``. On the other two a hung
or restarting local server surfaced as a **bare** ``asyncio.TimeoutError``, whose
``str()`` is the EMPTY STRING, or as raw aiohttp wording — neither classifiable
by any phrase list, so the engine treated a server that was back seconds later as
a permanent failure.

Every test here asserts the POSTED URL as well as the raised exception. The first
draft of this file did not, parametrised "ollama" with
``base_url="http://localhost:11434"``, and silently exercised the
OpenAI-compatible path for both rows: deleting the Ollama handler outright left
all of them green. A transport test that does not prove WHICH transport ran is
not a transport test.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider


def _config(base_url: str, model: str) -> ProviderConfig:
    return ProviderConfig(
        name="local",
        api_key="",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
    )


# (label, base_url, model, expected_endpoint_suffix)
#
# ``generate`` dispatches to Ollama when "ollama" appears in the base URL OR the
# model name, which is why the Ollama row uses a host that contains it — the
# port number alone selects nothing.
_TRANSPORTS = [
    ("ollama", "http://ollama:11434", "llama3.2", "/api/generate"),
    (
        "openai_compatible",
        "http://localhost:8000/v1",
        "vllm-model",
        "/chat/completions",
    ),
]


def _recording_session(post_impl):
    """aiohttp.ClientSession mock that records the URL each post() targets."""
    posted: list = []

    def _post(url, *args, **kwargs):
        posted.append(url)
        return post_impl()

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, posted


def _raising_ctx(error: Exception):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=error)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _status_ctx(status: int, text: str = "boom"):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value={})
    response.text = AsyncMock(return_value=text)
    response.headers = {}
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("label,base_url,model,endpoint", _TRANSPORTS)
async def test_timeout_is_typed_and_retryable(label, base_url, model, endpoint):
    """A hung local server must raise a retryable 504, not a bare timeout."""
    session, posted = _recording_session(lambda: _raising_ctx(asyncio.TimeoutError()))
    provider = LocalProvider(_config(base_url, model))
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")

    assert posted and posted[0].endswith(endpoint), (label, posted)
    assert exc_info.value.status_code == 504, label
    assert exc_info.value.retryable is True, label


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("label,base_url,model,endpoint", _TRANSPORTS)
async def test_disconnect_is_typed_and_retryable(label, base_url, model, endpoint):
    """A local server restarting mid-request is the canonical transient here."""
    session, posted = _recording_session(
        lambda: _raising_ctx(aiohttp.ServerDisconnectedError())
    )
    provider = LocalProvider(_config(base_url, model))
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")

    assert posted and posted[0].endswith(endpoint), (label, posted)
    assert exc_info.value.retryable is True, label
    assert "connection error" in str(exc_info.value).lower(), label


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("label,base_url,model,endpoint", _TRANSPORTS)
async def test_http_error_is_still_untouched(label, base_url, model, endpoint):
    """POSITIVE CONTROL: the transport handlers must not swallow HTTP status.

    Without this, a handler placed so that it also caught the provider's own
    ``LLMException`` would turn every 400 into a retryable transport error and
    every one of the assertions above would still pass.
    """
    session, posted = _recording_session(lambda: _status_ctx(400))
    provider = LocalProvider(_config(base_url, model))
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")

    assert posted and posted[0].endswith(endpoint), (label, posted)
    assert exc_info.value.status_code == 400, label
    assert exc_info.value.retryable is False, label


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llamacpp_fallback_transport_is_typed_and_retryable():
    """The third transport, reached only through the 404 fallback.

    ``generate`` tries the OpenAI-compatible path first and falls back to raw
    llama.cpp when that 404s. A transport failure on the fallback used to be
    folded into a composite ``LLMException`` that declared nothing and so
    defaulted to NON-retryable — turning a transient failure permanent one layer
    above the one #1287 was filed for.
    """
    posted: list = []

    def _session_factory(*args, **kwargs):
        def _post(url, *a, **kw):
            posted.append(url)
            # First transport 404s (which is what selects the fallback); the
            # fallback then loses its connection.
            if len(posted) == 1:
                return _status_ctx(404, "not found")
            return _raising_ctx(aiohttp.ServerDisconnectedError())

        session = MagicMock()
        session.post = MagicMock(side_effect=_post)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    provider = LocalProvider(_config("http://localhost:8080/v1", "llama-model"))
    with patch("aiohttp.ClientSession", side_effect=_session_factory):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate("hello")

    # BOTH transports were actually exercised, in order. Without this the
    # assertion below could be satisfied by the first transport alone.
    assert len(posted) == 2, posted
    assert posted[0].endswith("/chat/completions"), posted
    assert posted[1].endswith("/completion"), posted
    assert exc_info.value.retryable is True
    assert "llama.cpp" in str(exc_info.value)
