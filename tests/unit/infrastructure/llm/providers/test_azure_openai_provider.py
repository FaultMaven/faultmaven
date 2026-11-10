"""Azure OpenAI: the OpenAI wire protocol on the operator's own resource.

What differs from :class:`OpenAIProvider` is pinned here: the credential
travels in an ``api-key`` header (never as a bearer token), the request goes to
the configured resource endpoint, the model id is the deployment name, and the
provider is configured only when both its key and its endpoint are set.
"""

from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from faultmaven.config.llm_validation import validate_llm_provider_credentials
from faultmaven.config.settings import LLMSettings
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.azure_openai_provider import (
    AzureOpenAIProvider,
)
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.registry import (
    PROVIDER_SCHEMA,
    ProviderRegistry,
    required_env_vars,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)
from tests.model_content import vetted

ENDPOINT = "https://example-resource.openai.azure.com/openai/v1"
_OK = {
    "choices": [
        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_AZURE_ENV = ("AZURE_OPENAI_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_MODEL")


def _provider(model: str = "gpt-5.6-luna") -> AzureOpenAIProvider:
    return AzureOpenAIProvider(
        ProviderConfig(
            name="azure_openai",
            api_key="azure-test-key",
            base_url=ENDPOINT,
            models=[model],
            default_model=model,
            timeout=30,
            confidence_score=0.85,
        )
    )


@pytest.mark.unit
@pytest.mark.llm
class TestTheRequestOnTheWire:
    async def test_key_travels_as_api_key_header_to_the_resource(
        self, mock_aiohttp_session
    ):
        session = mock_aiohttp_session(_OK)
        with patch("aiohttp.ClientSession", return_value=session):
            response = await send_to_model(
                _provider(), prompt=vetted("hi"), max_tokens=64
            )

        call = session.post.call_args
        assert call.args[0] == f"{ENDPOINT}/chat/completions"
        headers = call.kwargs["headers"]
        assert headers["api-key"] == "azure-test-key"
        assert "Authorization" not in headers, "Azure keys are not bearer tokens"
        assert call.kwargs["json"]["model"] == "gpt-5.6-luna"
        assert response.content == "ok"
        assert response.provider == "azure_openai"

    def test_openai_still_sends_a_bearer_token(self):
        """The header hook did not change the parent's authentication."""
        from faultmaven.infrastructure.llm.providers.openai_provider import (
            OpenAIProvider,
        )

        provider = OpenAIProvider(_provider().config)
        headers = provider._request_headers()
        assert headers["Authorization"] == "Bearer azure-test-key"
        assert "api-key" not in headers

    def test_a_deployment_named_after_its_model_inherits_its_capability(self):
        assert (
            _provider("gpt-5.6-luna").get_structured_output_capability()
            is StructuredOutputCapability.STRICT
        )


@pytest.mark.unit
@pytest.mark.llm
class TestConfiguration:
    def _registry(self, monkeypatch, **env):
        for var in _AZURE_ENV:
            monkeypatch.delenv(var, raising=False)
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        return ProviderRegistry(settings=NS(llm=LLMSettings()))

    def test_key_endpoint_and_deployment_build_the_provider(self, monkeypatch):
        registry = self._registry(
            monkeypatch,
            AZURE_OPENAI_KEY="k",
            AZURE_OPENAI_ENDPOINT=ENDPOINT + "/",
            AZURE_OPENAI_MODEL="my-deployment",
        )
        config = registry._create_provider_config(
            "azure_openai", PROVIDER_SCHEMA["azure_openai"]
        )
        assert config is not None
        assert config.api_key == "k"
        assert config.base_url == ENDPOINT, "trailing slash stripped"
        assert config.models[0] == "my-deployment"

    def test_a_key_without_an_endpoint_configures_nothing(self, monkeypatch):
        registry = self._registry(monkeypatch, AZURE_OPENAI_KEY="k")
        assert (
            registry._create_provider_config(
                "azure_openai", PROVIDER_SCHEMA["azure_openai"]
            )
            is None
        )

    def test_the_registry_names_both_required_variables(self):
        assert set(required_env_vars("azure_openai")) == {
            "AZURE_OPENAI_KEY",
            "AZURE_OPENAI_ENDPOINT",
        }


def _gate_settings(**llm):
    return NS(llm=NS(provider=NS(value="azure_openai"), local_url=None, **llm))


@pytest.mark.unit
class TestStartupGate:
    def test_key_and_endpoint_pass(self):
        validate_llm_provider_credentials(
            _gate_settings(azure_openai_key="k", azure_openai_endpoint=ENDPOINT)
        )

    def test_missing_key_is_refused(self):
        with pytest.raises(ValueError, match="AZURE_OPENAI_KEY"):
            validate_llm_provider_credentials(
                _gate_settings(azure_openai_key=None, azure_openai_endpoint=ENDPOINT)
            )

    def test_missing_endpoint_is_refused(self):
        with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
            validate_llm_provider_credentials(
                _gate_settings(azure_openai_key="k", azure_openai_endpoint="  ")
            )
