"""Local endpoint tool-calling capability: transport + operator declaration.

Regression tests for #1356. ``LocalProvider.supports_tool_calling`` used to
answer from the MODEL NAME ("functionary" or "hermes"), so a self-hosted
OpenAI-compatible endpoint serving gpt-oss / Qwen / Mistral / Llama 3.3 with
full native tool calling reported False, ``validate_investigation_tooling``
refused to boot, and the remedy text told the self-hoster to adopt a cloud
vendor or disable Directed Analysis.

Capability is a property of the ENDPOINT, so the rule now reads the two
endpoint properties that are knowable from configuration:

* the TRANSPORT — Ollama's ``/api/generate`` has no ``tool_calls`` field, so it
  is never capable and no declaration can make it so; ``/v1/chat/completions``
  is assumed capable, as it is for every other OpenAI-compatible provider;
* the OPERATOR'S DECLARATION (``LOCAL_LLM_TOOL_CALLING``) — the only party who
  knows whether the serving stack was built with tool support.

The Ollama half of the old rule was correct and must not move: the tests below
drive it through the real startup gate, not by inspection.
"""

import logging
from types import SimpleNamespace as NS

import pytest

from faultmaven.config.investigation_capability import (
    InvestigationToolingError,
    resolve_investigation_capability,
    validate_investigation_tooling,
)
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

# A vLLM / llama.cpp / TGI style OpenAI-compatible endpoint.
OPENAI_COMPATIBLE_URL = "http://vllm.internal:8000/v1"
# Routed to Ollama's /api/generate by generate()'s dispatch.
OLLAMA_URL = "http://ollama.internal:11434"


def _provider(model: str, base_url: str, tool_calling=None) -> LocalProvider:
    return LocalProvider(
        ProviderConfig(
            name="local",
            api_key=None,
            base_url=base_url,
            models=[model],
            default_model=model,
            timeout=30,
            tool_calling=tool_calling,
        )
    )


# --- the OpenAI-compatible transport is capable by default -------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    "model",
    [
        "gpt-oss-120b",
        "Qwen/Qwen3-32B",
        "mistralai/Mistral-Small-3.2-24B-Instruct",
        "meta-llama/Llama-3.3-70B-Instruct",
        "phi3-mini",
        "llama3.2",
    ],
)
def test_openai_compatible_endpoint_is_capable_whatever_the_model_is_called(model):
    """The defect: every one of these reported False on name alone, and each is
    routinely served over vLLM with native tool calling."""
    assert _provider(model, OPENAI_COMPATIBLE_URL).supports_tool_calling() is True


@pytest.mark.unit
@pytest.mark.llm
def test_capability_is_unchanged_when_the_model_is_passed_explicitly():
    provider = _provider("Qwen/Qwen3-32B", OPENAI_COMPATIBLE_URL)
    assert provider.supports_tool_calling("Qwen/Qwen3-32B") is True


@pytest.mark.unit
@pytest.mark.llm
def test_config_without_the_declaration_field_still_resolves():
    """A duck-typed config (test doubles, older construction sites) means "no
    declaration", not an AttributeError."""
    config = NS(
        base_url=OPENAI_COMPATIBLE_URL,
        models=["qwen3-32b"],
        default_model="qwen3-32b",
    )
    assert LocalProvider(config).supports_tool_calling() is True


# --- the Ollama /api/generate transport is never capable ---------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    "model,base_url",
    [
        ("functionary-7b-v2", OLLAMA_URL),  # capable model, incapable transport
        ("hermes-2-pro-llama-3-8b", OLLAMA_URL),
        ("qwen3-32b", OLLAMA_URL),
        # The dispatch also keys off the model string, so this reaches
        # /api/generate despite an OpenAI-compatible base URL.
        ("hermes-2-pro-ollama", OPENAI_COMPATIBLE_URL),
    ],
)
def test_ollama_generate_transport_is_never_capable(model, base_url):
    assert _provider(model, base_url).supports_tool_calling() is False


@pytest.mark.unit
@pytest.mark.llm
def test_operator_cannot_declare_tool_calling_onto_the_ollama_transport(caplog):
    """``/api/generate`` has nowhere to put a tool call. An operator declaring
    otherwise is refused (and told why) rather than believed."""
    provider = _provider("qwen3-32b", OLLAMA_URL, tool_calling=True)
    with caplog.at_level(logging.WARNING):
        assert provider.supports_tool_calling() is False
    assert "not honoured" in caplog.text
    assert "/api/generate" in caplog.text


# --- the operator declaration narrows the OpenAI-compatible default ----------


@pytest.mark.unit
@pytest.mark.llm
def test_declared_false_is_honoured_on_the_openai_compatible_transport():
    """The local equivalent of the Fireworks denylist: a stack built without
    tool support, keyed on the deployment rather than on a model name."""
    provider = _provider("qwen3-32b", OPENAI_COMPATIBLE_URL, tool_calling=False)
    assert provider.supports_tool_calling() is False


@pytest.mark.unit
@pytest.mark.llm
def test_declared_true_is_honoured_on_the_openai_compatible_transport():
    provider = _provider("some-unknown-model", OPENAI_COMPATIBLE_URL, tool_calling=True)
    assert provider.supports_tool_calling() is True


# --- structured output stays on its own axis, but cannot contradict ----------


@pytest.mark.unit
@pytest.mark.llm
def test_unrecognised_model_keeps_best_effort_structured_output():
    """Widening tool calling must NOT promote every local model to
    FUNCTION_CALLING: that would start forcing a schema tool call on every
    existing local deployment where prompt-requested JSON serves today."""
    provider = _provider("qwen3-32b", OPENAI_COMPATIBLE_URL)
    assert provider.supports_tool_calling() is True
    assert (
        provider.get_structured_output_capability()
        == StructuredOutputCapability.BEST_EFFORT
    )


@pytest.mark.unit
@pytest.mark.llm
def test_functionary_on_openai_compatible_transport_still_function_calling():
    provider = _provider("functionary-7b-v2", OPENAI_COMPATIBLE_URL)
    assert (
        provider.get_structured_output_capability()
        == StructuredOutputCapability.FUNCTION_CALLING
    )


@pytest.mark.unit
@pytest.mark.llm
def test_declared_toolless_endpoint_demotes_a_function_calling_model():
    """The two axes cannot contradict each other: an endpoint declared toolless
    is BEST_EFFORT even when its model is named ``hermes``."""
    provider = _provider(
        "hermes-2-pro-llama-3-8b", OPENAI_COMPATIBLE_URL, tool_calling=False
    )
    assert (
        provider.get_structured_output_capability()
        == StructuredOutputCapability.BEST_EFFORT
    )


# --- the startup gate, driven with a real LocalProvider ----------------------


class _Registry:
    def __init__(self, provider):
        self._provider = provider

    def get_provider(self, name):
        return self._provider


def _settings(model: str, *, allow_toolless: bool = False):
    prov = NS(value="local")
    return NS(
        llm=NS(
            da_provider=None,
            allow_toolless_investigation=allow_toolless,
            get_da_provider=lambda: prov,
            get_da_model=lambda: model,
        )
    )


@pytest.mark.unit
@pytest.mark.llm
def test_gate_boots_on_a_self_hosted_openai_compatible_endpoint():
    """Done-when #1: a self-hosted OpenAI-compatible endpoint serving a
    tool-calling model boots WITHOUT ALLOW_TOOLLESS_INVESTIGATION."""
    provider = _provider("gpt-oss-120b", OPENAI_COMPATIBLE_URL)
    settings = _settings("gpt-oss-120b", allow_toolless=False)
    registry = _Registry(provider)

    assert resolve_investigation_capability(settings, registry).tool_capable is True
    validate_investigation_tooling(settings, registry)  # must not raise


@pytest.mark.unit
@pytest.mark.llm
def test_gate_still_refuses_the_ollama_generate_transport():
    """Done-when #2: a genuinely toolless transport still fails the gate."""
    provider = _provider("llama3.2", OLLAMA_URL)
    settings = _settings("llama3.2", allow_toolless=False)
    registry = _Registry(provider)

    assert resolve_investigation_capability(settings, registry).tool_capable is False
    with pytest.raises(InvestigationToolingError) as exc:
        validate_investigation_tooling(settings, registry)
    assert "does not support tool calling" in str(exc.value)


@pytest.mark.unit
@pytest.mark.llm
def test_gate_refuses_an_endpoint_the_operator_declared_toolless():
    provider = _provider("qwen3-32b", OPENAI_COMPATIBLE_URL, tool_calling=False)
    settings = _settings("qwen3-32b", allow_toolless=False)
    with pytest.raises(InvestigationToolingError):
        validate_investigation_tooling(settings, _Registry(provider))


@pytest.mark.unit
@pytest.mark.llm
def test_toolless_local_endpoint_can_still_opt_in_to_degraded_mode():
    provider = _provider("llama3.2", OLLAMA_URL)
    settings = _settings("llama3.2", allow_toolless=True)
    validate_investigation_tooling(settings, _Registry(provider))  # must not raise


@pytest.mark.unit
@pytest.mark.llm
def test_local_remedy_does_not_tell_a_self_hoster_to_adopt_a_cloud_vendor():
    """The other half of the reported defect: the remedy text. A self-hosted
    endpoint is the operator's own infrastructure, so the fix must be one they
    can apply to it."""
    provider = _provider("llama3.2", OLLAMA_URL)
    with pytest.raises(InvestigationToolingError) as exc:
        validate_investigation_tooling(_settings("llama3.2"), _Registry(provider))
    message = str(exc.value)

    assert "LOCAL_LLM_URL" in message
    assert "LOCAL_LLM_TOOL_CALLING" in message
    assert "/api/generate" in message
    # The old remedy's only concrete instruction was to switch vendors.
    assert "tool-capable CHAT_PROVIDER (anthropic, openai, gemini)" not in message


@pytest.mark.unit
@pytest.mark.llm
def test_non_local_remedy_is_unchanged():
    """The vendor list is still the right advice for a hosted provider."""

    class _Toolless:
        def supports_tool_calling(self, model=None):
            return False

    settings = NS(
        llm=NS(
            da_provider=None,
            allow_toolless_investigation=False,
            get_da_provider=lambda: NS(value="huggingface"),
            get_da_model=lambda: "mistralai/Mistral-Large-Instruct-2411",
        )
    )
    with pytest.raises(InvestigationToolingError) as exc:
        validate_investigation_tooling(settings, _Registry(_Toolless()))
    assert "tool-capable CHAT_PROVIDER (anthropic, openai, gemini)" in str(exc.value)


# --- the declaration actually reaches the provider ---------------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize("declared", [None, True, False])
def test_registry_wires_local_llm_tool_calling_onto_provider_config(declared):
    """A settings double, not LLMSettings(...): env beats init kwargs, so a
    real settings object would read this box's environment."""
    from faultmaven.infrastructure.llm.providers.registry import (
        PROVIDER_SCHEMA,
        ProviderRegistry,
    )

    llm_settings = NS(
        local_model="qwen3-32b",
        local_url=OPENAI_COMPATIBLE_URL,
        local_tool_calling=declared,
        max_retries=1,
    )
    registry = ProviderRegistry(settings=NS(llm=llm_settings))
    config = registry._create_provider_config("local", PROVIDER_SCHEMA["local"])

    assert config is not None
    assert config.tool_calling is declared
    assert LocalProvider(config).supports_tool_calling() is (declared is not False)
