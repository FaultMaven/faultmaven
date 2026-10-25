"""Tests for configuration presets module.

Tests cover:
- Preset definitions and structure
- Preset loading and application
- Zero-config detection
- Provider-aware required overrides
- Preset validation
"""

import os
from unittest.mock import patch

import pytest


class TestPresetDefinitions:
    """Test preset definitions are correct."""

    def test_all_presets_defined(self):
        """Test that all expected presets are defined."""
        from faultmaven.config.presets import PRESETS, PresetName

        assert PresetName.LOCAL in PRESETS
        assert PresetName.STANDARD in PRESETS
        assert PresetName.ENTERPRISE in PRESETS
        # Minimal is an alias for local
        assert PresetName.MINIMAL in PRESETS

    def test_preset_structure(self):
        """Test that presets have required fields."""
        from faultmaven.config.presets import PRESETS, PresetName

        for preset_name, preset in PRESETS.items():
            assert preset.name, f"Preset {preset_name} missing name"
            assert preset.description, f"Preset {preset_name} missing description"
            assert isinstance(
                preset.defaults, dict
            ), f"Preset {preset_name} defaults not a dict"

    def test_local_preset_has_zero_config_defaults(self):
        """Test that local preset has zero-config defaults."""
        from faultmaven.config.presets import PRESETS, PresetName

        local_preset = PRESETS[PresetName.LOCAL]
        defaults = local_preset.defaults

        # Vectors use the local ChromaDB PersistentClient (no external server
        # needed). Sessions use FakeRedis when no Redis server is reachable, and
        # cases the DATABASE_URL SQLite file: no setting selects either.
        assert defaults.get("VECTOR_STORAGE_TYPE") == "chromadb"

        # Should use local LLM
        assert defaults.get("CHAT_PROVIDER") == "local"
        assert "localhost" in defaults.get("LOCAL_LLM_URL", "")

        # Must NOT set SKIP_SERVICE_CHECKS: that flag makes the DI container skip
        # creating the ChromaDB vector stores, which disables the knowledge base
        # the preset otherwise relies on (embedded PersistentClient). Redis still
        # degrades to FakeRedis and ChromaDB defaults to a local PersistentClient,
        # so nothing external is required without skipping store creation.
        assert "SKIP_SERVICE_CHECKS" not in defaults

    def test_local_preset_no_required_keys_for_local_provider(self):
        """Test that local preset has no required keys for local provider."""
        from faultmaven.config.presets import PRESETS, PresetName, preset_required_keys

        local_preset = PRESETS[PresetName.LOCAL]
        required = preset_required_keys(local_preset, "local")

        # Local provider should have no required keys for true zero-config
        assert len(required) == 0

    def test_standard_preset_requires_external_services(self):
        """Test that standard preset uses external services."""
        from faultmaven.config.presets import PRESETS, PresetName

        standard_preset = PRESETS[PresetName.STANDARD]
        defaults = standard_preset.defaults

        # Sessions: Redis via REDIS_HOST in the preset
        assert defaults.get("REDIS_HOST") == "localhost"
        # Should use ChromaDB for vectors
        assert defaults.get("VECTOR_STORAGE_TYPE") == "chromadb"

    def test_enterprise_preset_full_production(self):
        """Test that enterprise preset is configured for full production."""
        from faultmaven.config.presets import PRESETS, PresetName

        enterprise_preset = PRESETS[PresetName.ENTERPRISE]
        defaults = enterprise_preset.defaults

        # Should be multi-tenant
        assert defaults.get("DEPLOYMENT_MODE") == "multi-tenant"

        # Should have PII redaction enabled
        assert defaults.get("PROTECTION_ENABLED") == "true"

    def test_no_preset_sets_a_rate_limit_key(self):
        """No preset may hand out a rate-limiting env key.

        Rate limiting is not configurable by environment at all — the protection
        presets in ``config/protection.py`` own it. A preset writing
        ``RATE_LIMIT_*`` into ``os.environ`` is how the removed
        ``settings.security`` knobs came to report *disabled* on a deployment
        that was rate limiting (fm#985 item 16): ``CONFIG_PRESET=local`` set
        ``RATE_LIMIT_ENABLED=false`` and nothing enforcing ever read it.

        Asserted over every preset rather than the three known keys, so a new
        preset cannot reintroduce the class under a new name.
        """
        from faultmaven.config.presets import PRESETS

        offenders = {
            preset.name: sorted(
                k for k in preset.defaults if k.startswith("RATE_LIMIT")
            )
            for preset in PRESETS.values()
            if any(k.startswith("RATE_LIMIT") for k in preset.defaults)
        }
        assert offenders == {}


class TestPresetLoading:
    """Test preset loading functions."""

    def test_get_preset_returns_correct_preset(self):
        """Test that get_preset returns the correct preset."""
        from faultmaven.config.presets import PresetName, get_preset

        preset = get_preset("local")
        assert preset is not None
        assert preset.name == "local"

        preset = get_preset("standard")
        assert preset is not None
        assert preset.name == "standard"

    def test_get_preset_case_insensitive(self):
        """Test that get_preset is case insensitive."""
        from faultmaven.config.presets import get_preset

        assert get_preset("LOCAL") is not None
        assert get_preset("Local") is not None
        assert get_preset("local") is not None

    def test_get_preset_returns_none_for_unknown(self):
        """Test that get_preset returns None for unknown presets."""
        from faultmaven.config.presets import get_preset

        assert get_preset("unknown") is None
        assert get_preset("nonexistent") is None

    def test_get_current_preset_name_from_env(self):
        """Test that get_current_preset_name reads from environment."""
        from faultmaven.config.presets import get_current_preset_name

        with patch.dict(os.environ, {"CONFIG_PRESET": "standard"}):
            assert get_current_preset_name() == "standard"

        with patch.dict(os.environ, {"CONFIG_PRESET": "enterprise"}):
            assert get_current_preset_name() == "enterprise"

    def test_get_current_preset_name_returns_none_when_not_set(self):
        """Test that get_current_preset_name returns None when not set."""
        from faultmaven.config.presets import get_current_preset_name

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CONFIG_PRESET", None)
            assert get_current_preset_name() is None


class TestPresetApplication:
    """Test preset application to environment."""

    def test_apply_preset_defaults_sets_env_vars(self):
        """Test that apply_preset_defaults sets environment variables."""
        from faultmaven.config.presets import apply_preset_defaults

        with patch.dict(os.environ, {"CONFIG_PRESET": "local"}, clear=True):
            os.environ.pop("VECTOR_STORAGE_TYPE", None)

            apply_preset_defaults()

            # Check that preset values were applied
            assert os.environ.get("VECTOR_STORAGE_TYPE") == "chromadb"

    def test_apply_preset_defaults_does_not_override_existing(self):
        """Test that apply_preset_defaults does not override existing env vars."""
        from faultmaven.config.presets import apply_preset_defaults

        with patch.dict(
            os.environ,
            {
                "CONFIG_PRESET": "local",
                "VECTOR_STORAGE_TYPE": "inmemory",  # User override (non-default)
            },
            clear=True,
        ):
            apply_preset_defaults()

            # User value should be preserved (not overwritten by preset default)
            assert os.environ.get("VECTOR_STORAGE_TYPE") == "inmemory"

    def test_apply_preset_defaults_no_op_without_preset(self):
        """Test that apply_preset_defaults is a no-op without preset."""
        from faultmaven.config.presets import apply_preset_defaults

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CONFIG_PRESET", None)
            os.environ.pop("VECTOR_STORAGE_TYPE", None)

            apply_preset_defaults()

            # No preset means no defaults applied
            assert os.environ.get("VECTOR_STORAGE_TYPE") is None


class TestZeroConfigDetection:
    """Test zero-config detection logic."""

    def test_detect_zero_config_returns_local_when_clean(self):
        """Test that detect_zero_config_preset returns 'local' in clean environment."""
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {}, clear=True):
            # Remove any LLM keys or service configs
            for key in [
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "REDIS_HOST",
                "CHROMADB_URL",
            ]:
                os.environ.pop(key, None)

            result = detect_zero_config_preset()
            assert result is not None
            assert result.value == "local"

    def test_detect_zero_config_returns_none_with_api_key(self):
        """Test that detect_zero_config_preset returns None when API key is set."""
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            result = detect_zero_config_preset()
            assert result is None

    def test_detect_zero_config_returns_none_with_redis(self):
        """Test that detect_zero_config_preset returns None when Redis is configured."""
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {"REDIS_HOST": "localhost"}, clear=True):
            result = detect_zero_config_preset()
            assert result is None

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("REDIS_URL", "redis://redis.internal:6379/0"),
            ("REDIS_HOST", "redis.internal"),
            ("CHROMADB_URL", "http://chroma.internal:8000"),
            ("CHROMADB_HOST", "chroma.internal"),
        ],
    )
    def test_any_variable_naming_a_redis_or_chromadb_server_declines_it(
        self, name, value
    ):
        """A box pointed at a server is not zero-config, whichever variable names it.

        ``REDIS_URL`` is the Redis client factory's other way to name the
        server, and it wins over ``REDIS_HOST``. Detection asked ``REDIS_HOST``
        alone, so a Redis named by URL got the laptop preset: ``DEBUG`` on,
        protection and PII redaction off (#2270). ``CHROMADB_HOST`` other than
        ``localhost`` sends the KB ingester to a server
        (``is_host_only_chroma_configured``).
        """
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {name: value}, clear=True):
            assert detect_zero_config_preset() is None

    def test_redis_url_alone_keeps_the_local_preset_off(self):
        """The acceptance of #2270, end to end: nothing of the preset is applied."""
        from faultmaven.config.presets import ensure_preset_applied

        environ = {"REDIS_URL": "redis://redis.internal:6379/0"}
        with patch.dict(os.environ, environ, clear=True):
            ensure_preset_applied()
            applied = dict(os.environ)

        assert applied == environ

    @pytest.mark.parametrize(
        "environ",
        [
            {"REDIS_URL": ""},
            {"REDIS_URL": "   "},
            {"REDIS_HOST": ""},
            {"CHROMADB_URL": " "},
            {"CHROMADB_HOST": "localhost"},
            {"CHROMADB_HOST": ""},
        ],
        ids=[
            "blank_redis_url",
            "whitespace_redis_url",
            "blank_redis_host",
            "whitespace_chromadb_url",
            "embedded_chromadb_host",
            "blank_chromadb_host",
        ],
    )
    def test_a_blank_value_or_the_embedded_chromadb_host_names_no_server(self, environ):
        """Set but empty names nothing, and ``CHROMADB_HOST=localhost`` is its
        default, the embedded store: the box is still zero-config."""
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, environ, clear=True):
            result = detect_zero_config_preset()

        assert result is not None
        assert result.value == "local"

    def test_detect_zero_config_returns_none_in_production(self):
        """Test that detect_zero_config_preset returns None in production."""
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True):
            result = detect_zero_config_preset()
            assert result is None

    @pytest.mark.parametrize(
        "environment",
        ["staging", "STAGING", "weird-env"],
        ids=["staging", "staging_uppercase", "unrecognised"],
    )
    def test_detect_zero_config_declines_any_named_non_development_environment(
        self, environment
    ):
        """An under-configured deployment is not a request for local defaults.

        The gate used to name ``production`` alone, so ``ENVIRONMENT=staging`` on
        a box with no LLM key and no Redis fell through and got the ``local``
        preset — which carries ``DEBUG=true``, ``SANITIZE_PII=false`` and
        ``PROTECTION_ENABLED=false``. Naming an environment is an act of
        classification; "merely under-configured" must not silently reclassify a
        deployed box as a development one. The unrecognised value is the
        near-miss, and it has to fail the same way.
        """
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {"ENVIRONMENT": environment}, clear=True):
            assert detect_zero_config_preset() is None

    @pytest.mark.parametrize(
        "environment", ["", "   "], ids=["empty", "whitespace_only"]
    )
    def test_detect_zero_config_still_applies_the_local_preset_when_unnamed(
        self, environment
    ):
        """Unset — and a ConfigMap key that reads as blank — stay zero-config.

        Tightening the gate must not cost the path its purpose: nobody has
        classified this box, so the quick-start default still applies. Explicit
        ``development`` is covered by
        ``test_detect_zero_config_returns_local_preset``' clean environment.
        """
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {"ENVIRONMENT": environment}, clear=True):
            result = detect_zero_config_preset()

        assert result is not None
        assert result.value == "local"

    def test_detect_zero_config_still_applies_it_for_explicit_development(self):
        """``development`` named outright is the one value that keeps the preset."""
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            result = detect_zero_config_preset()

        assert result is not None
        assert result.value == "local"


#: What configures each provider the router supports, written out literally (not
#: through the registry helper the code under test uses) so a wrong or dropped
#: ``api_key_var`` / ``endpoint_var`` in the registry fails here too. The
#: registry-derived tests below cover a provider this table has not heard of.
CONFIGURING_ENV_VAR = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACE_API_KEY",
    "cohere": "COHERE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "local": "LOCAL_LLM_URL",
}


class TestAnyConfiguredProviderDeclinesZeroConfig:
    """A deployment that configures any provider the router supports is not zero-config.

    ``detect_zero_config_preset`` used to ask a hand-kept list of five API keys,
    so a ``.env`` configuring only Gemini, OpenRouter, HuggingFace or a local
    server (``.env.example`` itself suggests ``CHAT_PROVIDER=gemini``) got the
    ``local`` preset: ``DEBUG=true``, ``LOG_LEVEL=DEBUG``,
    ``PROTECTION_ENABLED=false``, ``127.0.0.1:8000`` with reload (#1984). The
    question is now answered from the provider registry. Every environment here
    is built with ``clear=True``: the box's own shell must not decide the
    outcome (``tests/conftest.py`` already keeps its ``.env`` out of an
    in-process run).
    """

    @pytest.mark.parametrize(
        ("provider", "env_var"),
        sorted(CONFIGURING_ENV_VAR.items()),
        ids=sorted(CONFIGURING_ENV_VAR),
    )
    def test_each_provider_alone_declines_the_preset(self, provider, env_var):
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {env_var: "configured"}, clear=True):
            assert detect_zero_config_preset() is None, (
                f"{provider!r} configured through {env_var} alone was still "
                "treated as zero-config"
            )

    def test_the_table_agrees_with_the_registry(self):
        """Every provider in the table is configured by the variable it names."""
        from faultmaven.infrastructure.llm.providers.registry import (
            configuration_env_var,
        )

        for provider, env_var in CONFIGURING_ENV_VAR.items():
            assert configuration_env_var(provider) == env_var

    def test_every_registered_provider_alone_declines_the_preset(self):
        """Derived from the registry: a provider added tomorrow is covered today.

        Nothing here names a provider. A new ``PROVIDER_SCHEMA`` entry that
        declares neither ``api_key_var`` nor ``endpoint_var`` makes
        ``configuration_env_var`` raise, which fails this test — the provider
        would be one a deployment can configure without the preset noticing.
        """
        from faultmaven.config.presets import detect_zero_config_preset
        from faultmaven.infrastructure.llm.providers.registry import (
            PROVIDER_SCHEMA,
            configuration_env_var,
        )

        assert len(PROVIDER_SCHEMA) >= len(CONFIGURING_ENV_VAR)
        for provider in PROVIDER_SCHEMA:
            env_var = configuration_env_var(provider)
            with patch.dict(os.environ, {env_var: "configured"}, clear=True):
                assert (
                    detect_zero_config_preset() is None
                ), f"{provider!r} ({env_var}) did not decline the zero-config preset"

    def test_a_provider_added_to_the_registry_is_recognised_without_a_code_change(
        self, monkeypatch
    ):
        """The check reads the registry; it does not hold a list of its own."""
        from faultmaven.config.presets import detect_zero_config_preset
        from faultmaven.infrastructure.llm.providers import registry

        monkeypatch.setitem(
            registry.PROVIDER_SCHEMA,
            "acme",
            {"api_key_var": "ACME_API_KEY", "default_model": "acme-1"},
        )

        with patch.dict(os.environ, {}, clear=True):
            assert detect_zero_config_preset() is not None
        with patch.dict(os.environ, {"ACME_API_KEY": "configured"}, clear=True):
            assert detect_zero_config_preset() is None

    def test_a_registry_entry_that_cannot_be_configured_is_refused_not_ignored(
        self, monkeypatch
    ):
        from faultmaven.config.presets import detect_zero_config_preset
        from faultmaven.infrastructure.llm.providers import registry

        monkeypatch.setitem(
            registry.PROVIDER_SCHEMA, "acme", {"default_model": "acme-1"}
        )

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="acme"):
                detect_zero_config_preset()

    def test_no_provider_configured_still_applies_the_local_preset(self):
        """The no-config case is unchanged, and a blank value is not a config."""
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, {}, clear=True):
            assert detect_zero_config_preset() == "local"

        # A ConfigMap key that reads as blank (or ``KEY=`` in a ``.env``) is unset.
        blank = {env_var: "" for env_var in CONFIGURING_ENV_VAR.values()}
        with patch.dict(os.environ, blank, clear=True):
            assert detect_zero_config_preset() == "local"
        padded = {env_var: "   " for env_var in CONFIGURING_ENV_VAR.values()}
        with patch.dict(os.environ, padded, clear=True):
            assert detect_zero_config_preset() == "local"

    def test_a_gemini_only_environment_gets_no_preset_defaults(self):
        """The reported probe: ``GEMINI_API_KEY`` alone must not turn DEBUG on.

        Through ``ensure_preset_applied``, which is what ``get_settings`` calls:
        no ``CONFIG_PRESET`` is set and none of the preset's server or
        protection defaults reach the environment.
        """
        from faultmaven.config.presets import ensure_preset_applied

        with patch.dict(
            os.environ,
            {"CHAT_PROVIDER": "gemini", "GEMINI_API_KEY": "configured"},
            clear=True,
        ):
            ensure_preset_applied()
            for leaked in (
                "CONFIG_PRESET",
                "HOST",
                "PORT",
                "RELOAD",
                "DEBUG",
                "LOG_LEVEL",
                "PROTECTION_ENABLED",
                "SANITIZE_PII",
            ):
                assert leaked not in os.environ, f"{leaked} came from the preset"


# Scenarios for the dotenv channel. ``tests/conftest.py`` makes every in-process
# run blind to a ``.env`` (it replaces ``DotEnvSettingsSource._read_env_files``),
# so a test of what the settings read from the working directory's file has to
# run where that is not true: a child interpreter. One child runs every scenario
# (each in its own empty directory and cleared environment) and the tests below
# read its answers, so the cost is one interpreter start, not one per case.
_DOTENV_SCENARIOS = """
import json, os, sys, tempfile
from pathlib import Path

import faultmaven

assert faultmaven.__file__.startswith(sys.argv[1]), faultmaven.__file__
from faultmaven.config.presets import ensure_preset_applied

scenarios = json.loads(sys.argv[2])
answers = {}
for name, (environ, dotenv) in scenarios.items():
    os.chdir(tempfile.mkdtemp())
    if dotenv is not None:
        Path(".env").write_text(dotenv)
    os.environ.clear()
    os.environ.update(environ)
    ensure_preset_applied()
    answers[name] = {
        "preset": os.environ.get("CONFIG_PRESET"),
        "chat_provider": os.environ.get("CHAT_PROVIDER"),
    }
print(json.dumps(answers))
"""

_NOTHING = ({}, None)

DOTENV_SCENARIOS = {
    # (a) and (b): a provider named only in the working directory's file.
    "gemini_in_dotenv": ({}, "GEMINI_API_KEY=x\nCHAT_PROVIDER=gemini\n"),
    "openai_in_dotenv": ({}, "OPENAI_API_KEY=sk-test\n"),
    "local_url_in_dotenv": ({}, "LOCAL_LLM_URL=http://ollama:11434/v1\n"),
    # The other reads go through the same view, in any letter case.
    "redis_in_dotenv": ({}, "REDIS_HOST=redis\n"),
    "redis_url_in_dotenv": ({}, "REDIS_URL=redis://redis:6379/0\n"),
    "chromadb_lowercase_in_dotenv": ({}, "chromadb_url=http://chroma:8000\n"),
    "production_in_dotenv": ({}, "ENVIRONMENT=production\n"),
    "staging_lowercase_in_dotenv": ({}, "environment=staging\n"),
    # The checkout shape: load_dotenv put the file in the environment already.
    "dotenv_agrees_with_environment": (
        {"GEMINI_API_KEY": "x"},
        "GEMINI_API_KEY=x\n",
    ),
    # Still zero-config.
    "nothing_anywhere": _NOTHING,
    "dotenv_without_a_provider": ({}, "# nothing configured\nLOG_LEVEL=INFO\n"),
    # The environment outranks the file, so the settings see a blank key.
    "blank_environment_beats_the_dotenv": (
        {"GEMINI_API_KEY": ""},
        "GEMINI_API_KEY=x\n",
    ),
    # FAULTMAVEN_SKIP_DOTENV: the settings are built without the file.
    "skip_dotenv_ignores_the_file": (
        {"FAULTMAVEN_SKIP_DOTENV": "1"},
        "GEMINI_API_KEY=x\n",
    ),
    "skip_dotenv_still_reads_the_environment": (
        {"FAULTMAVEN_SKIP_DOTENV": "1", "gemini_api_key": "x"},
        "OPENAI_API_KEY=x\n",
    ),
}


@pytest.fixture(scope="module")
def dotenv_answers(tmp_path_factory):
    """What the preset check did, per scenario, in a child with a real ``.env`` read."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    import faultmaven

    root = str(Path(faultmaven.__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", _DOTENV_SCENARIOS, root, json.dumps(DOTENV_SCENARIOS)],
        cwd=tmp_path_factory.mktemp("cwd"),
        # Nothing of the parent's: no PYTHONPATH either, so the child imports
        # the checkout this test file sits in (the editable install may point
        # elsewhere), and the assertion in the child says so if it did not.
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path_factory.mktemp("home")),
            "PYTHONPATH": root,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestDetectionReadsWhatTheSettingsRead:
    """One view of the environment: the one ``FaultMavenSettings`` will read.

    ``detect_zero_config_preset`` read exact-case ``os.environ`` alone, while the
    settings read names case-insensitively AND a ``.env`` resolved from the
    working directory (``env_file=".env"``). ``get_settings``' own
    ``load_dotenv()`` walked up from ``settings.py`` instead, so an installed
    package run next to its ``.env`` had a configuration the settings honoured
    and the preset check never saw: the laptop preset was applied over it, and
    its ``CHAT_PROVIDER=local`` beat the user's own selection (#1984 review).
    """

    # --- the environment channel (in process) ------------------------------

    @staticmethod
    def _detect(environ):
        from faultmaven.config.presets import detect_zero_config_preset

        with patch.dict(os.environ, environ, clear=True):
            return detect_zero_config_preset()

    def test_a_lowercase_environment_variable_declines_the_preset(self):
        """Probe (c): pydantic-settings binds ``gemini_api_key`` to the field."""
        assert self._detect({"gemini_api_key": "x"}) is None

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("REDIS_HOST", "x"),
            ("redis_host", "x"),
            ("Redis_Url", "x"),
            ("ChromaDB_Url", "x"),
            ("environment", "production"),
            ("Environment", "staging"),
        ],
    )
    def test_every_other_read_is_case_insensitive_in_the_environment(self, name, value):
        assert self._detect({name: value}) is None

    def test_the_last_spelling_of_a_variable_wins_as_it_does_for_the_settings(self):
        """Two spellings of one variable: the settings take the last, so must the view."""
        assert self._detect({"GEMINI_API_KEY": "x", "gemini_api_key": ""}) == "local"
        assert self._detect({"gemini_api_key": "", "GEMINI_API_KEY": "x"}) is None

    # --- the dotenv channel (a child process that really reads the file) ----

    def test_a_gemini_key_in_the_working_directory_dotenv_declines_the_preset(
        self, dotenv_answers
    ):
        """Probe (a): the preset must not overwrite the user's own selection."""
        answer = dotenv_answers["gemini_in_dotenv"]
        assert answer == {"preset": None, "chat_provider": None}

    def test_an_openai_key_in_the_working_directory_dotenv_declines_the_preset(
        self, dotenv_answers
    ):
        """Probe (b): the five-key list missed this one too (a cwd file)."""
        assert dotenv_answers["openai_in_dotenv"]["preset"] is None

    @pytest.mark.parametrize(
        "scenario",
        [
            "local_url_in_dotenv",
            "redis_in_dotenv",
            "redis_url_in_dotenv",
            "chromadb_lowercase_in_dotenv",
            "production_in_dotenv",
            "staging_lowercase_in_dotenv",
            "dotenv_agrees_with_environment",
            "skip_dotenv_still_reads_the_environment",
        ],
    )
    def test_every_read_goes_through_the_same_view(self, dotenv_answers, scenario):
        assert dotenv_answers[scenario]["preset"] is None

    @pytest.mark.parametrize(
        "scenario",
        [
            "nothing_anywhere",
            "dotenv_without_a_provider",
            "blank_environment_beats_the_dotenv",
            "skip_dotenv_ignores_the_file",
        ],
    )
    def test_without_a_configured_provider_the_local_preset_still_applies(
        self, dotenv_answers, scenario
    ):
        assert dotenv_answers[scenario]["preset"] == "local"
        assert dotenv_answers[scenario]["chat_provider"] == "local"

    def test_the_scenarios_are_all_answered(self, dotenv_answers):
        """A scenario added to the table must reach a test above."""
        assert set(dotenv_answers) == set(DOTENV_SCENARIOS)


class TestPresetValidation:
    """Test preset validation."""

    def test_validate_preset_requirements_empty_for_local_provider(self):
        """Test that validation passes for local provider without keys."""
        from faultmaven.config.presets import validate_preset_requirements

        with patch.dict(
            os.environ,
            {
                "CONFIG_PRESET": "local",
                "CHAT_PROVIDER": "local",
            },
            clear=True,
        ):
            errors = validate_preset_requirements()
            assert len(errors) == 0

    def test_validate_preset_requirements_error_for_missing_api_key(self):
        """Test that validation fails when required API key is missing."""
        from faultmaven.config.presets import validate_preset_requirements

        with patch.dict(
            os.environ,
            {
                "CONFIG_PRESET": "local",
                "CHAT_PROVIDER": "openai",
            },
            clear=True,
        ):
            os.environ.pop("OPENAI_API_KEY", None)

            errors = validate_preset_requirements()
            assert len(errors) > 0
            assert "OPENAI_API_KEY" in errors[0]

    def test_validate_preset_requirements_passes_with_api_key(self):
        """Test that validation passes when API key is provided."""
        from faultmaven.config.presets import validate_preset_requirements

        with patch.dict(
            os.environ,
            {
                "CONFIG_PRESET": "local",
                "CHAT_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-test",
            },
            clear=True,
        ):
            errors = validate_preset_requirements()
            assert len(errors) == 0


class TestPresetInfo:
    """Test preset info functions."""

    def test_get_preset_info_returns_active_preset(self):
        """Test that get_preset_info returns info about active preset."""
        from faultmaven.config.presets import get_preset_info

        with patch.dict(os.environ, {"CONFIG_PRESET": "local"}):
            info = get_preset_info()

            assert info["active"] is True
            assert info["name"] == "local"
            assert "description" in info
            assert "defaults_count" in info

    def test_get_preset_info_returns_inactive_when_not_set(self):
        """Test that get_preset_info returns inactive when no preset."""
        from faultmaven.config.presets import get_preset_info

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CONFIG_PRESET", None)

            info = get_preset_info()
            assert info["active"] is False

    def test_list_available_presets(self):
        """Test that list_available_presets returns all presets."""
        from faultmaven.config.presets import list_available_presets

        presets = list_available_presets()

        assert len(presets) >= 3  # local, standard, enterprise
        names = [p["name"] for p in presets]
        assert "local" in names
        assert "standard" in names
        assert "enterprise" in names


class TestConfigPresetNone:
    """``CONFIG_PRESET=none``: no preset is applied, and none is detected (#2243).

    Every case configures no provider, Redis or ChromaDB: the zero-config shape,
    where an unset ``CONFIG_PRESET`` auto-applies ``local``.
    """

    @staticmethod
    def _session_timeout_default():
        from faultmaven.config.settings import SessionSettings

        return SessionSettings.model_fields["timeout_minutes"].default

    @pytest.mark.parametrize("spelling", ["none", "NONE", " None "])
    def test_none_applies_no_preset_and_the_settings_see_none(self, spelling, caplog):
        from faultmaven.config.presets import (
            ensure_preset_applied,
            get_current_preset_name,
            get_preset_info,
        )
        from faultmaven.config.settings import FaultMavenSettings

        with patch.dict(os.environ, {"CONFIG_PRESET": spelling}, clear=True):
            with caplog.at_level("DEBUG", logger="faultmaven.config.presets"):
                ensure_preset_applied()
            untouched = dict(os.environ)
            settings = FaultMavenSettings(_env_file=None)
            current, active = get_current_preset_name(), settings.get_active_preset()
            info = get_preset_info()

        assert untouched == {"CONFIG_PRESET": spelling}, "a preset wrote defaults"
        assert current is None and active is None
        assert info["active"] is False and "error" not in info
        assert settings.session.timeout_minutes == self._session_timeout_default()
        assert "Unknown preset" not in caplog.text

    def test_unset_still_applies_the_local_preset(self):
        """The positive control: the same environment without the opt-out."""
        from faultmaven.config.presets import PRESETS, PresetName, ensure_preset_applied
        from faultmaven.config.settings import FaultMavenSettings

        local = PRESETS[PresetName.LOCAL].defaults
        with patch.dict(os.environ, {}, clear=True):
            ensure_preset_applied()
            applied = dict(os.environ)
            settings = FaultMavenSettings(_env_file=None)
            active = settings.get_active_preset()

        assert applied == {
            "CONFIG_PRESET": "local",
            **{k: str(v) for k, v in local.items()},
        }
        assert active == "local"
        timeout = int(local["SESSION_TIMEOUT_MINUTES"])
        assert settings.session.timeout_minutes == timeout
        assert timeout != self._session_timeout_default()

    def test_an_unknown_preset_is_still_reported(self, caplog):
        """The capture's own control: it does see the warning ``none`` must not log."""
        from faultmaven.config.presets import ensure_preset_applied

        with patch.dict(os.environ, {"CONFIG_PRESET": "bogus"}, clear=True):
            with caplog.at_level("DEBUG", logger="faultmaven.config.presets"):
                ensure_preset_applied()

        assert "Unknown preset: bogus" in caplog.text
