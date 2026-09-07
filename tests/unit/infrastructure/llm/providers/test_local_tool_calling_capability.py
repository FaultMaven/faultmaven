"""Local endpoint tool-calling capability: transport, path, and declaration.

Regression tests for #1356 and its adversarial review.

``LocalProvider.supports_tool_calling`` originally answered from the MODEL NAME
("functionary" or "hermes"), so a self-hosted OpenAI-compatible endpoint serving
gpt-oss / Qwen / Mistral / Llama 3.3 with full native tool calling reported
False, ``validate_investigation_tooling`` refused to boot, and the remedy text
told the self-hoster to adopt a cloud vendor.

The first fix keyed the answer on the transport but decided the transport from
the HOSTNAME (the substring "ollama" in the base URL) — the same "capability
from a name" error one layer down, and it refused Ollama's own
OpenAI-compatible endpoint, whose service is called ``ollama`` in Ollama's
compose examples and Helm chart.

Capability is now decided by ``resolve_local_transport``, the SINGLE predicate
``generate()`` also dispatches on, reading the URL **path**:

* ``/api`` or ``/api/generate`` → Ollama's native protocol, which has no
  ``tool_calls`` field: never capable, and no declaration can override it;
* a bare host, ``/v1`` or ``/v1/chat/completions`` → the OpenAI-compatible
  protocol: capable unless the operator declares otherwise. The suffix is
  stripped before the request URL is built, so no form doubles the path.

The toolless transport must still fail the boot gate — driven here through the
real gate, not by inspection.
"""

import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.investigation_capability import (
    InvestigationToolingError,
    resolve_investigation_capability,
    validate_investigation_tooling,
)
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.local_provider import (
    TRANSPORT_OLLAMA_NATIVE,
    TRANSPORT_OPENAI_COMPATIBLE,
    LocalProvider,
    resolve_local_transport,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

# A vLLM / llama.cpp / TGI style OpenAI-compatible endpoint.
OPENAI_COMPATIBLE_URL = "http://vllm.internal:8000/v1"
# Ollama's NATIVE API, named explicitly by its path.
OLLAMA_NATIVE_URL = "http://ollama.internal:11434/api"


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


# --- the shared predicate ----------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    "base_url,expected_transport,expected_root",
    [
        # Bare host: the documented default, and what every stack but Ollama
        # serves. Ollama serves it too, on the same port.
        ("http://llm:8000", TRANSPORT_OPENAI_COMPATIBLE, "http://llm:8000"),
        ("http://ollama:11434", TRANSPORT_OPENAI_COMPATIBLE, "http://ollama:11434"),
        # Explicit OpenAI-compatible paths, both forms, suffix stripped.
        ("http://ollama:11434/v1", TRANSPORT_OPENAI_COMPATIBLE, "http://ollama:11434"),
        (
            "http://ollama:11434/v1/chat/completions",
            TRANSPORT_OPENAI_COMPATIBLE,
            "http://ollama:11434",
        ),
        ("http://vllm:8000/v1/", TRANSPORT_OPENAI_COMPATIBLE, "http://vllm:8000"),
        # Explicit Ollama-native paths, both forms, suffix stripped.
        ("http://ollama:11434/api", TRANSPORT_OLLAMA_NATIVE, "http://ollama:11434"),
        (
            "http://ollama:11434/api/generate",
            TRANSPORT_OLLAMA_NATIVE,
            "http://ollama:11434",
        ),
        ("http://llm:11434/api", TRANSPORT_OLLAMA_NATIVE, "http://llm:11434"),
    ],
)
def test_transport_is_resolved_from_the_path(
    base_url, expected_transport, expected_root
):
    assert resolve_local_transport(base_url) == (expected_transport, expected_root)


@pytest.mark.unit
@pytest.mark.llm
def test_hostname_never_decides_the_transport():
    """The review's control: rename the host, keep the endpoint — same verdict."""
    named_ollama = resolve_local_transport("http://ollama:11434/v1")
    named_llm = resolve_local_transport("http://llm:11434/v1")
    assert named_ollama[0] == named_llm[0] == TRANSPORT_OPENAI_COMPATIBLE


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
    """The original defect: every one of these reported False on name alone, and
    each is routinely served over vLLM with native tool calling."""
    assert _provider(model, OPENAI_COMPATIBLE_URL).supports_tool_calling() is True


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    "base_url",
    [
        "http://ollama:11434",
        "http://ollama:11434/v1",
        "http://ollama.ollama.svc.cluster.local:11434/v1",
        "http://my-ollama-host:11434/v1/chat/completions",
    ],
)
def test_ollama_hosted_openai_compatible_endpoint_is_capable(base_url):
    """Review F1: a host merely NAMED ollama is not a toolless transport. Ollama
    serves an OpenAI-compatible API that does tool calling."""
    assert _provider("llama3.2", base_url).supports_tool_calling() is True


@pytest.mark.unit
@pytest.mark.llm
def test_model_name_no_longer_selects_a_transport():
    """A model called ``…-ollama`` served on an OpenAI-compatible path is
    capable. The old rule read the model name as a transport signal, which is
    the category error #1356 exists to remove."""
    assert (
        _provider("hermes-2-pro-ollama", OPENAI_COMPATIBLE_URL).supports_tool_calling()
        is True
    )


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
    provider = LocalProvider.__new__(LocalProvider)
    provider.config = config
    provider.logger = logging.getLogger("test")
    provider._declared_tool_calling_warned = False
    assert provider.supports_tool_calling() is True


# --- the Ollama native transport is never capable ----------------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    "model,base_url",
    [
        ("functionary-7b-v2", OLLAMA_NATIVE_URL),  # capable model, incapable protocol
        ("hermes-2-pro-llama-3-8b", OLLAMA_NATIVE_URL),
        ("qwen3-32b", "http://ollama:11434/api/generate"),
        ("llama3.2", "http://llm:11434/api"),  # not named ollama, still native
    ],
)
def test_ollama_native_transport_is_never_capable(model, base_url):
    assert _provider(model, base_url).supports_tool_calling() is False


@pytest.mark.unit
@pytest.mark.llm
def test_operator_cannot_declare_tool_calling_onto_the_native_transport(caplog):
    """``/api/generate`` has nowhere to put a tool call. An operator declaring
    otherwise is refused (and told why) rather than believed."""
    provider = _provider("qwen3-32b", OLLAMA_NATIVE_URL, tool_calling=True)
    with caplog.at_level(logging.WARNING):
        assert provider.supports_tool_calling() is False
    assert "not honoured" in caplog.text
    assert "/api/generate" in caplog.text


@pytest.mark.unit
@pytest.mark.llm
def test_the_refusal_warning_is_latched(caplog):
    """Review F4: /health resolves capability on every probe, so an unlatched
    warning is emitted forever on a documented configuration."""
    provider = _provider("qwen3-32b", OLLAMA_NATIVE_URL, tool_calling=True)
    with caplog.at_level(logging.WARNING):
        for _ in range(100):
            provider.supports_tool_calling()
    assert caplog.text.count("not honoured") == 1


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
@pytest.mark.parametrize(
    "base_url,declared",
    [
        (OLLAMA_NATIVE_URL, None),  # transport says no
        (OPENAI_COMPATIBLE_URL, False),  # operator says no
    ],
)
def test_an_endpoint_that_cannot_carry_tool_calls_demotes_a_function_calling_model(
    base_url, declared
):
    """The two axes cannot contradict each other: a model named ``hermes`` on an
    endpoint that cannot carry tool calls is BEST_EFFORT."""
    provider = _provider("hermes-2-pro-llama-3-8b", base_url, tool_calling=declared)
    assert (
        provider.get_structured_output_capability()
        == StructuredOutputCapability.BEST_EFFORT
    )


# --- the request URL is built from the normalised root (review F2) -----------


def _recording_session():
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(
        return_value={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"total_tokens": 3},
        }
    )
    response.text = AsyncMock(return_value="")
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:9999",
        "http://127.0.0.1:9999/v1",
        "http://127.0.0.1:9999/v1/",
        "http://127.0.0.1:9999/v1/chat/completions",
    ],
)
async def test_every_openai_compatible_url_form_posts_to_the_same_endpoint(base_url):
    """Review F2: a base already ending in /v1 used to POST to /v1/v1/… and 404,
    while the gate reported capable — boot passed and every request failed."""
    provider = _provider("qwen3-32b", base_url)
    session = _recording_session()
    with patch("aiohttp.ClientSession", return_value=session):
        await provider.generate("hello")
    assert session.post.call_args[0][0] == "http://127.0.0.1:9999/v1/chat/completions"


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url", ["http://127.0.0.1:9999/api", "http://127.0.0.1:9999/api/generate"]
)
async def test_every_ollama_native_url_form_posts_to_the_same_endpoint(base_url):
    provider = _provider("llama3.2", base_url)
    session = _recording_session()
    session.post.return_value.json = AsyncMock(
        return_value={"response": "ok", "eval_count": 3, "done_reason": "stop"}
    )
    with patch("aiohttp.ClientSession", return_value=session):
        await provider.generate("hello")
    assert session.post.call_args[0][0] == "http://127.0.0.1:9999/api/generate"


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
async def test_dispatch_and_capability_agree_on_every_form():
    """The invariant behind the shared predicate: whatever `generate()` sends to,
    `supports_tool_calling` answered about."""
    for base_url in (
        "http://ollama:11434",
        "http://ollama:11434/v1",
        "http://ollama:11434/api",
        "http://vllm:8000/v1/chat/completions",
    ):
        provider = _provider("llama3.2", base_url)
        transport, _root = resolve_local_transport(base_url)
        assert provider.supports_tool_calling() is (
            transport == TRANSPORT_OPENAI_COMPATIBLE
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
@pytest.mark.parametrize(
    "base_url",
    [
        "http://vllm.internal:8000/v1",
        "http://ollama:11434",
        "http://ollama:11434/v1",
        "http://ollama.ollama.svc.cluster.local:11434/v1",
    ],
)
def test_gate_boots_on_a_self_hosted_openai_compatible_endpoint(base_url):
    """Done-when #1, including the review's four refused-by-hostname URLs."""
    provider = _provider("gpt-oss-120b", base_url)
    settings = _settings("gpt-oss-120b", allow_toolless=False)
    registry = _Registry(provider)

    assert resolve_investigation_capability(settings, registry).tool_capable is True
    validate_investigation_tooling(settings, registry)  # must not raise


@pytest.mark.unit
@pytest.mark.llm
def test_gate_still_refuses_the_ollama_native_transport():
    """Done-when #2: a genuinely toolless transport still fails the gate."""
    provider = _provider("llama3.2", OLLAMA_NATIVE_URL)
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
    provider = _provider("llama3.2", OLLAMA_NATIVE_URL)
    settings = _settings("llama3.2", allow_toolless=True)
    validate_investigation_tooling(settings, _Registry(provider))  # must not raise


@pytest.mark.unit
@pytest.mark.llm
def test_local_remedy_names_the_two_ways_to_reach_the_gate():
    """The other half of the reported defect: the remedy text. It must name a
    change the operator can actually make, and must not send them to a URL that
    404s (review F2)."""
    provider = _provider("llama3.2", OLLAMA_NATIVE_URL)
    with pytest.raises(InvestigationToolingError) as exc:
        validate_investigation_tooling(_settings("llama3.2"), _Registry(provider))
    message = str(exc.value)

    assert "/api" in message  # the suffix to drop
    assert "LOCAL_LLM_TOOL_CALLING" in message  # the other way in
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


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.parametrize(
    "raw,expected", [("", None), ("   ", None), ("false", False), ("true", True)]
)
def test_blank_local_llm_tool_calling_is_unset_not_a_type_error(
    monkeypatch, raw, expected
):
    """Review F3: this is the only Optional[bool] in the settings, and pydantic
    rejects "" for bool — so the habitual way of neutralising a key (delete the
    value, keep the line) crashed startup here and nowhere else. It is also what
    lets .env.example document the real default as a blank."""
    from faultmaven.config.settings import LLMSettings

    monkeypatch.setenv("LOCAL_LLM_TOOL_CALLING", raw)
    assert LLMSettings().local_tool_calling is expected
