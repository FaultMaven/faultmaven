"""Tests for the startup LLM-provider credential fail-fast gate.

Covers `validate_llm_provider_credentials` — the gate wired into the app
lifespan that refuses to boot unless the resolved provider has a usable
credential (API key for cloud providers, LOCAL_LLM_URL for 'local'). This is
LLM-critical startup code (CLAUDE.md: NO CODE MERGES WITHOUT TESTS).
"""

from types import SimpleNamespace as NS

import pytest

from faultmaven.config.llm_validation import (
    _secret_present,
    validate_llm_provider_credentials,
)


def _settings(provider: str, *, local_url=None, **api_keys):
    """Minimal settings stub with the fields the gate reads."""
    llm = NS(provider=NS(value=provider), local_url=local_url)
    # Default every known key field to None, then apply overrides.
    for field in (
        "openai_api_key",
        "anthropic_api_key",
        "gemini_api_key",
        "fireworks_api_key",
        "groq_api_key",
        "huggingface_api_key",
        "cohere_api_key",
        "openrouter_api_key",
    ):
        setattr(llm, field, api_keys.get(field))
    return NS(llm=llm)


# --- cloud providers: API key required ---
def test_cloud_provider_with_key_passes():
    validate_llm_provider_credentials(_settings("openai", openai_api_key="sk-x"))


def test_cloud_provider_missing_key_raises():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_llm_provider_credentials(_settings("openai"))


def test_cloud_provider_empty_key_raises():
    with pytest.raises(ValueError):
        validate_llm_provider_credentials(
            _settings("anthropic", anthropic_api_key="  ")
        )


@pytest.mark.parametrize(
    "provider,field",
    [
        ("anthropic", "anthropic_api_key"),
        ("gemini", "gemini_api_key"),
        ("fireworks", "fireworks_api_key"),
        ("groq", "groq_api_key"),
        ("cohere", "cohere_api_key"),
        ("openrouter", "openrouter_api_key"),
    ],
)
def test_each_cloud_provider_checks_its_own_key(provider, field):
    validate_llm_provider_credentials(_settings(provider, **{field: "key"}))
    with pytest.raises(ValueError):
        validate_llm_provider_credentials(_settings(provider))


# --- local: URL required, not a key ---
def test_local_with_url_passes():
    validate_llm_provider_credentials(
        _settings("local", local_url="http://host.docker.internal:11434")
    )


def test_local_without_url_raises():
    with pytest.raises(ValueError, match="LOCAL_LLM_URL"):
        validate_llm_provider_credentials(_settings("local"))


# --- unsupported / unconfigured ---
def test_unsupported_provider_raises():
    with pytest.raises(ValueError, match="not a supported provider"):
        validate_llm_provider_credentials(_settings("whatever"))


def test_shipped_default_provider_without_key_raises():
    """A provider ships as the default; a CREDENTIAL never does. An operator
    who configures nothing resolves to the shipped default (gemini) and must
    be refused for the missing key — the message naming the provider actually
    in effect and its env var, so the fix is obvious."""
    with pytest.raises(ValueError) as exc:
        validate_llm_provider_credentials(_settings("gemini"))
    message = str(exc.value)
    assert "gemini" in message
    assert "GEMINI_API_KEY" in message

    # Same protection for any other provider a deployment names.
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_llm_provider_credentials(_settings("openai"))


# --- SecretStr handling ---
def test_secret_present_handles_secretstr_and_plain():
    from pydantic import SecretStr

    assert _secret_present(SecretStr("x")) is True
    assert _secret_present(SecretStr("")) is False
    assert _secret_present("plain") is True
    assert _secret_present("") is False
    assert _secret_present(None) is False
