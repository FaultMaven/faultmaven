"""#2138 — Gemini answers an invalid API key with HTTP 400, not 401/403.

Its only machine signal is the ``google.rpc.ErrorInfo`` detail's ``reason``,
``API_KEY_INVALID``. The engine reads AUTH_FAILED off a declaration only, so
the adapter declares ``PROVIDER_AUTH_FAILED`` from that reason, read off the
parsed body, never the prose. Driven through the adapter's real non-200 raise.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler
from faultmaven.exceptions import PROVIDER_AUTH_FAILED, LLMException
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider
from tests.model_content import vetted

pytestmark = [pytest.mark.unit, pytest.mark.llm]


def _body(reason=None, message="API key not valid. Please pass a valid API key."):
    error = {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}
    if reason is not None:
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": reason,
                "domain": "googleapis.com",
            }
        ]
    return json.dumps({"error": error})


def _error_session(status: int, body: str):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value={})
    response.text = AsyncMock(return_value=body)
    response.headers = {}
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


async def _raised(body: str) -> LLMException:
    provider = GeminiProvider(
        ProviderConfig(
            name="gemini",
            api_key="test-key",
            base_url="https://generativelanguage.test.invalid/v1beta",
            models=["gemini-3.7-flash"],
            default_model="gemini-3.7-flash",
            timeout=30,
        )
    )
    with patch("aiohttp.ClientSession", return_value=_error_session(400, body)):
        with pytest.raises(LLMException) as exc_info:
            await send_to_model(provider, prompt=vetted("hello"))
    return exc_info.value


async def test_an_invalid_key_400_is_auth_failed():
    error = await _raised(_body(reason="API_KEY_INVALID"))
    assert error.status_code == 400
    assert error.error_code == PROVIDER_AUTH_FAILED
    result = await LLMErrorHandler().handle_error(error, 0)
    assert result.error_code == "AUTH_FAILED"


@pytest.mark.parametrize(
    "body",
    [
        # INVALID_ARGUMENT for a reason that is not the key.
        _body(reason="FIELD_INVALID", message="Invalid value at 'contents'"),
        # No ErrorInfo at all.
        _body(message="Invalid value at 'contents'"),
        # The reason's words in the PROSE only: not a declaration.
        _body(message="API_KEY_INVALID"),
    ],
    ids=["other-reason", "no-details", "reason-in-prose-only"],
)
async def test_another_400_is_not_auth_failed(body):
    error = await _raised(body)
    assert error.error_code != PROVIDER_AUTH_FAILED
    result = await LLMErrorHandler().handle_error(error, 0)
    assert result.error_code != "AUTH_FAILED"


@pytest.mark.parametrize(
    "reason",
    [["API_KEY_INVALID"], {"code": "API_KEY_INVALID"}],
    ids=["list-reason", "dict-reason"],
)
async def test_an_unhashable_reason_is_still_a_400(reason):
    """A detail whose reason is not a string names no rejected key, and the
    400 is still raised as itself, not as a TypeError."""
    error = await _raised(_body(reason=reason))
    assert error.status_code == 400
    assert error.error_code != PROVIDER_AUTH_FAILED
    result = await LLMErrorHandler().handle_error(error, 0)
    assert result.error_code != "AUTH_FAILED"
