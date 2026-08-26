"""Per-role (provider, model) routing must actually reach the provider.

The `{PROVIDER}_{TASK}_MODEL` matrix and the role provider fields
(`DA_PROVIDER`, `CLASSIFIER_PROVIDER`, `SYNTHESIS_PROVIDER`, …) have existed at
the settings layer for a long time, and were silently unhonoured downstream:

  - the registry built every provider with ``models=[<base model>]``, and
  - ``BaseLLMProvider.get_effective_model`` honours a requested model ONLY if
    it is in ``config.models``,

so every per-task model override was quietly replaced by the base model at
call time. Two roles could never run different models on one provider — the
brief's §1 problem — while the configuration surface claimed they could.

Pins:
  1. `configured_task_models` enumerates a provider's per-task values
     (deduplicated, base model excluded, [] where nothing is configured);
  2. `explicit_role_provider` is a name ONLY when the operator explicitly set
     the role provider (None = follow CHAT_PROVIDER, today's routing);
  3. the registry folds per-task models into ``config.models`` with the base
     model still first (``default_model`` derives from models[0], so
     no-override calls are unchanged);
  4. `get_effective_model` honours a configured per-task model, and a model it
     must discard falls back LOUDLY (warning log), never silently.
"""

import logging

import pytest

from faultmaven.config.settings import LLMProvider, LLMSettings
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider

# Every env var these tests touch — cleared up front so a developer's .env
# (LLMSettings reads one) cannot leak into assertions.
_ENV_VARS = (
    "CHAT_PROVIDER",
    "DA_PROVIDER",
    "CLASSIFIER_PROVIDER",
    "SYNTHESIS_PROVIDER",
    "OPENAI_MODEL",
    "OPENAI_DA_MODEL",
    "OPENAI_CLASSIFIER_MODEL",
    "OPENAI_SYNTHESIS_MODEL",
    "GEMINI_MODEL",
    "GEMINI_CLASSIFIER_MODEL",
    "OPENAI_API_KEY",
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.mark.unit
@pytest.mark.llm
class TestConfiguredTaskModels:
    def test_returns_per_task_values_for_provider(self, clean_env):
        clean_env.setenv("OPENAI_DA_MODEL", "gpt-5.6-luna")
        clean_env.setenv("OPENAI_CLASSIFIER_MODEL", "gpt-5.4-mini")
        settings = LLMSettings()
        assert settings.configured_task_models(LLMProvider.OPENAI) == [
            "gpt-5.4-mini",  # classifier precedes da in LLM_MODEL_TASKS order
            "gpt-5.6-luna",
        ]

    def test_deduplicates_repeated_values(self, clean_env):
        clean_env.setenv("OPENAI_DA_MODEL", "gpt-5.6-luna")
        clean_env.setenv("OPENAI_SYNTHESIS_MODEL", "gpt-5.6-luna")
        settings = LLMSettings()
        assert settings.configured_task_models("openai") == ["gpt-5.6-luna"]

    def test_empty_when_nothing_configured(self, clean_env):
        assert LLMSettings().configured_task_models("openai") == []

    def test_local_has_no_per_task_fields(self, clean_env):
        assert LLMSettings().configured_task_models(LLMProvider.LOCAL) == []

    def test_accepts_enum_and_string(self, clean_env):
        clean_env.setenv("GEMINI_CLASSIFIER_MODEL", "gemini-3.5-flash-lite")
        settings = LLMSettings()
        assert settings.configured_task_models(LLMProvider.GEMINI) == [
            "gemini-3.5-flash-lite"
        ]
        assert settings.configured_task_models("gemini") == ["gemini-3.5-flash-lite"]


@pytest.mark.unit
@pytest.mark.llm
class TestExplicitRoleProvider:
    def test_none_when_role_provider_unset(self, clean_env):
        settings = LLMSettings()
        assert settings.explicit_role_provider("classifier") is None
        assert settings.explicit_role_provider("synthesis") is None
        assert settings.explicit_role_provider("da") is None

    def test_name_when_role_provider_set(self, clean_env):
        clean_env.setenv("CLASSIFIER_PROVIDER", "groq")
        settings = LLMSettings()
        assert settings.explicit_role_provider("classifier") == "groq"

    def test_chat_maps_to_primary_provider_field(self, clean_env):
        clean_env.setenv("CHAT_PROVIDER", "gemini")
        settings = LLMSettings()
        assert settings.explicit_role_provider("chat") == "gemini"

    def test_unknown_role_is_none(self, clean_env):
        # getattr fallback — an unknown role must not raise in a call site.
        assert LLMSettings().explicit_role_provider("nonexistent") is None


@pytest.mark.unit
@pytest.mark.llm
class TestRegistryFoldsTaskModelsIntoProviderConfig:
    """The registry must configure a provider for EVERY model a role can
    resolve to — that list membership is what `get_effective_model` honours."""

    def _create_config(self, monkeypatch):
        from types import SimpleNamespace

        from faultmaven.infrastructure.llm.providers.registry import (
            PROVIDER_SCHEMA,
            ProviderRegistry,
        )

        # `_create_provider_config` reads only `self.settings.llm`; a
        # namespace around a fresh LLMSettings keeps the global settings
        # singleton out of the test.
        registry = ProviderRegistry(settings=SimpleNamespace(llm=LLMSettings()))
        return registry._create_provider_config("openai", PROVIDER_SCHEMA["openai"])

    def test_models_list_carries_base_plus_task_models(self, clean_env):
        clean_env.setenv("CHAT_PROVIDER", "openai")
        clean_env.setenv("OPENAI_API_KEY", "sk-test")
        clean_env.setenv("OPENAI_MODEL", "gpt-5.6-luna")
        clean_env.setenv("OPENAI_CLASSIFIER_MODEL", "gpt-5.4-mini")
        config = self._create_config(clean_env)
        assert config is not None
        assert config.models == ["gpt-5.6-luna", "gpt-5.4-mini"]
        # Base model stays models[0] → default_model → no-override calls
        # are byte-identical to before.
        assert config.default_model == "gpt-5.6-luna"

    def test_task_model_equal_to_base_not_duplicated(self, clean_env):
        clean_env.setenv("CHAT_PROVIDER", "openai")
        clean_env.setenv("OPENAI_API_KEY", "sk-test")
        clean_env.setenv("OPENAI_MODEL", "gpt-5.6-luna")
        clean_env.setenv("OPENAI_DA_MODEL", "gpt-5.6-luna")
        config = self._create_config(clean_env)
        assert config.models == ["gpt-5.6-luna"]


@pytest.mark.unit
@pytest.mark.llm
class TestGetEffectiveModelHonoursConfiguredTaskModel:
    def _provider(self):
        return OpenAIProvider(
            ProviderConfig(
                name="openai",
                api_key="sk-test",
                models=["gpt-5.6-luna", "gpt-5.4-mini"],
            )
        )

    def test_per_task_model_is_honoured(self):
        provider = self._provider()
        assert provider.get_effective_model("gpt-5.4-mini") == "gpt-5.4-mini"

    def test_no_request_uses_default(self):
        provider = self._provider()
        assert provider.get_effective_model(None) == "gpt-5.6-luna"

    def test_foreign_model_falls_back_loudly(self, caplog):
        """A model this provider is not configured for still falls back (the
        safe behavior — it is typically another provider's model arriving via
        the fallback chain) but must WARN: the silent version of this exact
        fallback is what made the per-task matrix a no-op."""
        provider = self._provider()
        with caplog.at_level(logging.WARNING):
            effective = provider.get_effective_model("llama-3.3-70b-versatile")
        assert effective == "gpt-5.6-luna"
        assert any(
            "llama-3.3-70b-versatile" in r.message and "falling back" in r.message
            for r in caplog.records
        ), "discarding a requested model must not be silent"
