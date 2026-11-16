"""The HuggingFace provider on default settings reaches a host that serves it (#2111).

The default base used to be ``https://api-inference.huggingface.co/models``,
which no longer resolves, and the adapter sent the retired text-generation shape
(``inputs``/``parameters`` -> ``generated_text``) for a default model no live
provider serves. Hugging Face's router serves the OpenAI-compatible
chat-completions API, so on defaults the provider must send that request to
``https://router.huggingface.co/v1/chat/completions`` for a served model.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.config.settings import LLMSettings
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.huggingface import HuggingFaceProvider
from faultmaven.infrastructure.llm.providers.registry import (
    PROVIDER_SCHEMA,
    ProviderRegistry,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)
from tests.model_content import vetted

pytestmark = [pytest.mark.unit, pytest.mark.llm]

_ROUTER_CHAT_URL = "https://router.huggingface.co/v1/chat/completions"
_DEFAULT_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
_OPENAI_RESP = {
    "choices": [
        {"message": {"content": "ok", "role": "assistant"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


@pytest.fixture
def hf_from_defaults(monkeypatch):
    """The provider the registry builds when only HUGGINGFACE_API_KEY is set."""
    for var in list(os.environ):
        if var.startswith("HUGGINGFACE_"):
            monkeypatch.delenv(var)
    monkeypatch.setenv("HUGGINGFACE_API_KEY", "hf_test_key")
    registry = ProviderRegistry(settings=SimpleNamespace(llm=LLMSettings()))
    config = registry._create_provider_config(
        "huggingface", PROVIDER_SCHEMA["huggingface"]
    )
    registry._initialize_provider("huggingface", config)
    return registry._providers["huggingface"]


async def test_default_call_posts_an_openai_chat_request_to_the_router(
    hf_from_defaults, mock_aiohttp_session
):
    session = mock_aiohttp_session(_OPENAI_RESP)
    with patch("aiohttp.ClientSession", return_value=session):
        response = await send_to_model(
            hf_from_defaults, prompt=vetted("Why is the pod restarting?")
        )

    (url,), kwargs = session.post.call_args
    body = kwargs["json"]
    assert url == _ROUTER_CHAT_URL
    assert body["model"] == _DEFAULT_MODEL
    assert body["messages"] == [
        {"role": "user", "content": "Why is the pod restarting?"}
    ]
    assert "inputs" not in body and "parameters" not in body
    assert kwargs["headers"]["Authorization"] == "Bearer hf_test_key"
    assert response.content == "ok"
    assert response.provider == "huggingface"


def test_the_default_declarations_agree():
    schema = PROVIDER_SCHEMA["huggingface"]
    fields = LLMSettings.model_fields
    assert fields["huggingface_base_url"].default == schema["default_base_url"]
    assert fields["huggingface_model"].default == schema["default_model"]
    assert schema["default_model"] in schema["available_models"]


def test_capability_claims_are_unchanged(hf_from_defaults):
    """The router's tool and strict-schema support vary per routed provider and
    are unverified, so the provider claims neither."""
    assert hf_from_defaults.provider_name == HuggingFaceProvider.LABEL
    assert HuggingFaceProvider.LABEL == "huggingface"
    assert hf_from_defaults.supports_tool_calling() is False
    assert (
        hf_from_defaults.get_structured_output_capability()
        == StructuredOutputCapability.BEST_EFFORT
    )
