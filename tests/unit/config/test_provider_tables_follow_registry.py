"""One source for "which providers exist and what configures each" (#2006).

``PROVIDER_SCHEMA`` is that source. Tables elsewhere are derived from it where
the import allows (``llm_validation``, ``admin_config``'s key check, the
presets' required keys). The ones that cannot be derived are pinned here, each
to exactly the registry's providers and variables:

* ``LLMProvider`` (an Enum typing needs, in ``config/settings.py``, which the
  registry imports);
* ``faultmaven.sh``'s ``provider_specs`` (a bash table);
* ``tests/conftest.py``'s ``ENV_VARS_TO_CLEAR`` (test isolation);
* ``PROVIDER_DISPLAY_NAMES`` in the admin routes (display copy: the registry
  holds no display names, and these are not a restatement of anything in it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import faultmaven
from faultmaven.config.presets import PRESETS, PresetName, preset_required_keys
from faultmaven.config.settings import LLMProvider
from faultmaven.infrastructure.llm.providers.registry import (
    PROVIDER_SCHEMA,
    configuration_env_var,
    required_env_vars,
)

pytestmark = pytest.mark.unit

ROOT = Path(faultmaven.__file__).resolve().parents[1]
PROVIDERS = sorted(PROVIDER_SCHEMA)


def test_the_llm_provider_enum_is_the_registry_plus_the_unset_sentinel():
    assert {p.value for p in LLMProvider} - {"NOT_SET"} == set(PROVIDER_SCHEMA)


def test_faultmaven_sh_provider_specs_are_the_registry():
    text = (ROOT / "faultmaven.sh").read_text()
    block = re.search(r"local provider_specs=\((.*?)\n    \)", text, re.S).group(1)
    specs = re.findall(r'"([^"|]+)\|[^"|]*\|([^"|]+)"', block)

    assert dict(specs) == {
        name: configuration_env_var(name) for name in PROVIDER_SCHEMA
    }
    assert len(specs) == len(PROVIDER_SCHEMA)


def test_the_test_isolation_list_clears_every_provider_variable():
    from tests.conftest import ENV_VARS_TO_CLEAR

    needed = set()
    for schema in PROVIDER_SCHEMA.values():
        for key in ("api_key_var", "endpoint_var", "model_var"):
            if schema.get(key):
                needed.add(schema[key])

    assert needed - set(ENV_VARS_TO_CLEAR) == set()


def test_admin_display_names_cover_exactly_the_registry():
    from faultmaven.api.routes.admin_config import PROVIDER_DISPLAY_NAMES

    assert set(PROVIDER_DISPLAY_NAMES) == set(PROVIDER_SCHEMA)


def test_every_keyed_provider_requires_its_api_key_and_local_requires_url_and_model():
    for name in PROVIDERS:
        if name == "local":
            assert set(required_env_vars(name)) == {"LOCAL_LLM_URL", "LOCAL_LLM_MODEL"}
        else:
            assert required_env_vars(name) == (PROVIDER_SCHEMA[name]["api_key_var"],)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("preset_name", ["standard", "enterprise"])
def test_a_production_preset_names_exactly_the_providers_required_variables(
    monkeypatch, provider, preset_name
):
    from faultmaven.config import presets

    for var in {k for p in PROVIDER_SCHEMA for k in required_env_vars(p)}:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CONFIG_PRESET", preset_name)
    monkeypatch.setenv("CHAT_PROVIDER", provider)
    monkeypatch.setattr(
        "faultmaven.config.settings.environment_settings_read",
        lambda: {"CONFIG_PRESET": preset_name, "CHAT_PROVIDER": provider},
    )

    errors = presets.validate_preset_requirements()

    # Expected from the schema's own key fields, not through the accessor
    # under test.
    expected = (
        ["LOCAL_LLM_MODEL", "LOCAL_LLM_URL"]
        if provider == "local"
        else [PROVIDER_SCHEMA[provider]["api_key_var"]]
    )
    assert sorted(re.findall(r"'([A-Z_]+)' for provider", " ".join(errors))) == expected


def test_the_local_preset_supplies_what_local_needs():
    assert preset_required_keys(PRESETS[PresetName.LOCAL], "local") == set()


def test_get_preset_info_reports_every_registry_provider():
    from faultmaven.config.presets import get_preset_info

    info = get_preset_info("standard")["required_for_providers"]

    assert set(info) == set(PROVIDER_SCHEMA)
    assert info["gemini"] == ["GEMINI_API_KEY"]
    assert info["local"] == ["LOCAL_LLM_MODEL", "LOCAL_LLM_URL"]
