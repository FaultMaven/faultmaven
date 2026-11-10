"""Azure: Foundry deployments over the resource's OpenAI v1 route.

What is Azure's own is pinned here: the request goes to the configured
resource's ``/openai/v1`` route (whatever spelling of the resource URL the
operator gave), the model id is the deployment name, and the provider is
configured only when both its key and its endpoint are set. Everything else is
:class:`OpenAIProvider`'s, the bearer-token authentication included.
"""

from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from faultmaven.config.llm_validation import validate_llm_provider_credentials
from faultmaven.config.settings import LLMSettings
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.azure_provider import (
    AzureProvider,
    v1_base_url,
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

RESOURCE = "https://example-resource.services.ai.azure.com"
ENDPOINT = RESOURCE + "/openai/v1"
_OK = {
    "choices": [
        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_AZURE_ENV = ("AZURE_API_KEY", "AZURE_API_BASE", "AZURE_MODEL")


def _provider(model: str = "gpt-5.6-luna") -> AzureProvider:
    return AzureProvider(
        ProviderConfig(
            name="azure",
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
    async def test_the_request_reaches_the_resource_v1_route(
        self, mock_aiohttp_session
    ):
        session = mock_aiohttp_session(_OK)
        with patch("aiohttp.ClientSession", return_value=session):
            response = await send_to_model(
                _provider(), prompt=vetted("hi"), max_tokens=64
            )

        call = session.post.call_args
        assert call.args[0] == f"{ENDPOINT}/chat/completions"
        # The v1 route takes the resource key as a bearer token, and needs
        # no api-version (implicit versioning).
        assert call.kwargs["headers"]["Authorization"] == "Bearer azure-test-key"
        assert "params" not in call.kwargs or not call.kwargs["params"]
        assert call.kwargs["json"]["model"] == "gpt-5.6-luna"
        assert response.content == "ok"
        assert response.provider == "azure"

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

    @pytest.mark.parametrize(
        "endpoint",
        [
            ENDPOINT,
            ENDPOINT + "/",
            RESOURCE,
            RESOURCE + "/",
            RESOURCE + "/openai/",
            # The Model Inference route, which the v1 route replaces.
            RESOURCE + "/models",
            RESOURCE + "/model",
            "  " + RESOURCE + "/  ",
        ],
    )
    def test_every_spelling_of_the_resource_reaches_its_v1_route(self, endpoint):
        assert v1_base_url(endpoint) == ENDPOINT

    def test_the_azure_openai_host_is_handled_the_same_way(self):
        assert v1_base_url("https://example-resource.openai.azure.com/") == (
            "https://example-resource.openai.azure.com/openai/v1"
        )

    def test_an_explicit_other_path_is_kept_and_unset_is_empty(self):
        assert v1_base_url("https://proxy.example/azure/v1/") == (
            "https://proxy.example/azure/v1"
        )
        assert v1_base_url(None) == "" and v1_base_url("  ") == ""

    def test_key_endpoint_and_deployment_build_the_provider(self, monkeypatch):
        registry = self._registry(
            monkeypatch,
            AZURE_API_KEY="k",
            AZURE_API_BASE=ENDPOINT + "/",
            AZURE_MODEL="my-deployment",
        )
        config = registry._create_provider_config("azure", PROVIDER_SCHEMA["azure"])
        assert config is not None
        assert config.api_key == "k"
        assert config.base_url == ENDPOINT, "trailing slash stripped"
        assert config.models[0] == "my-deployment"

    def test_a_key_without_an_endpoint_configures_nothing(self, monkeypatch):
        registry = self._registry(monkeypatch, AZURE_API_KEY="k")
        assert (
            registry._create_provider_config("azure", PROVIDER_SCHEMA["azure"]) is None
        )

    def test_the_registry_names_both_required_variables(self):
        assert set(required_env_vars("azure")) == {
            "AZURE_API_KEY",
            "AZURE_API_BASE",
        }


def _gate_settings(**llm):
    return NS(llm=NS(provider=NS(value="azure"), local_url=None, **llm))


@pytest.mark.unit
class TestStartupGate:
    def test_key_and_endpoint_pass(self):
        validate_llm_provider_credentials(
            _gate_settings(azure_api_key="k", azure_base_url=ENDPOINT)
        )

    def test_missing_key_is_refused(self):
        with pytest.raises(ValueError, match="AZURE_API_KEY"):
            validate_llm_provider_credentials(
                _gate_settings(azure_api_key=None, azure_base_url=ENDPOINT)
            )

    def test_missing_endpoint_is_refused(self):
        with pytest.raises(ValueError, match="AZURE_API_BASE"):
            validate_llm_provider_credentials(
                _gate_settings(azure_api_key="k", azure_base_url="  ")
            )


@pytest.mark.unit
class TestDashboardOverride:
    """The key an admin sets from the dashboard reaches the provider (review F1)."""

    def test_the_route_field_is_one_the_applier_accepts(self):
        from faultmaven.config.llm_config_overrides import (
            _ALLOWED_OVERRIDES,
            _API_KEY_FIELDS,
        )
        from faultmaven.config.llm_validation import api_key_field

        field, env_name = api_key_field("azure")
        assert (field, env_name) == ("azure_api_key", "AZURE_API_KEY")
        assert field in _ALLOWED_OVERRIDES and field in _API_KEY_FIELDS

    async def test_a_stored_key_override_lands_on_the_settings(self, monkeypatch):
        from faultmaven.config import llm_config_overrides
        from faultmaven.infrastructure.persistence import llm_config_repository

        async def stored():
            return {"azure_api_key": "from-dashboard"}

        monkeypatch.setattr(llm_config_repository, "get_all_overrides", stored)
        for var in _AZURE_ENV:
            monkeypatch.delenv(var, raising=False)
        settings = NS(llm=LLMSettings())

        await llm_config_overrides.apply_overrides_to_settings(settings)

        assert settings.llm.azure_api_key.get_secret_value() == ("from-dashboard")


@pytest.mark.unit
class TestEveryReaderNeedsTheEndpoint:
    """A key without an endpoint is not "configured" anywhere (review F5/F9)."""

    def test_missing_required_vars_names_the_endpoint(self, monkeypatch):
        from faultmaven.config.llm_validation import missing_required_vars

        for var in _AZURE_ENV:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AZURE_API_KEY", "k")
        assert missing_required_vars(LLMSettings(), "azure") == ["AZURE_API_BASE"]
        monkeypatch.setenv("AZURE_API_BASE", ENDPOINT)
        assert missing_required_vars(LLMSettings(), "azure") == []
