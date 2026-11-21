"""The investigation model the startup checks judge is the model the turn sends (#2142).

`create_da_provider` returns `(None, None)` unless `DA_PROVIDER` is set, and
the tool loop's router path then sends no `model`, so the chat provider's base
`{PROVIDER}_MODEL` answers. `get_da_model()` resolved `{PROVIDER}_DA_MODEL`
regardless, so the tool-calling gate and the enforcement resolver judged a
model no turn ever sent: a deployment whose base model cannot call tools
passed the fail-fast gate on a `FIREWORKS_DA_MODEL` that never ran.
"""

import os
from types import SimpleNamespace as NS

import pytest

from faultmaven.config import settings as settings_module
from faultmaven.config.investigation_capability import (
    resolve_enforcement_classes,
    resolve_investigation_capability,
)
from faultmaven.config.settings import LLMSettings
from faultmaven.container.providers.infrastructure import create_da_provider
from faultmaven.infrastructure.llm.providers.fireworks_provider import (
    FireworksProvider,
)

pytestmark = [pytest.mark.unit, pytest.mark.llm]

FW_BASE = "accounts/fireworks/models/minimax-m2p7"  # on the tool-calling denylist
FW_DA = "accounts/fireworks/models/deepseek-v4-flash"
AN_BASE = "claude-sonnet-4-6"
AN_DA = "claude-opus-5-5"


class _Provider:
    """Answers tool calling by model id, as the Fireworks denylist does."""

    def supports_tool_calling(self, model=None):
        return model not in FireworksProvider._TOOL_CALLING_DENYLIST

    def get_structured_output_capability(self, model=None):
        return NS(value="best_effort")


class _Registry:
    def get_provider(self, name):
        return _Provider()


@pytest.fixture
def env(monkeypatch):
    """No provider or model variable leaks in; `LLMSettings` reads only the
    process environment (it has no `env_file`)."""
    for var in list(os.environ):
        if var.endswith(("_PROVIDER", "_MODEL")):
            monkeypatch.delenv(var)
    monkeypatch.setenv("CHAT_PROVIDER", "fireworks")
    monkeypatch.setenv("FIREWORKS_MODEL", FW_BASE)
    monkeypatch.setenv("ANTHROPIC_MODEL", AN_BASE)
    return monkeypatch


def _investigation_row(llm):
    resolved = resolve_enforcement_classes(NS(llm=llm), _Registry())
    return next(r for r in resolved.roles if r.role.startswith("investigation"))


def test_issue_reproduction_judges_the_base_model_the_turn_sends(env):
    env.setenv("FIREWORKS_DA_MODEL", FW_DA)
    llm = LLMSettings()

    assert llm.get_da_model() == FW_BASE

    capability = resolve_investigation_capability(NS(llm=llm), _Registry())
    assert capability.model == FW_BASE
    assert capability.tool_capable is False

    assert _investigation_row(llm).model == FW_BASE

    # The tool loop's decider: no DA provider, so it sends no `model` and the
    # router's provider answers with the base model it was built with.
    env.setattr(settings_module, "get_settings", lambda: NS(llm=llm))
    assert create_da_provider() == (None, None)


@pytest.mark.parametrize(
    ("da_provider", "da_model_set", "expected"),
    [
        (None, False, FW_BASE),
        (None, True, FW_BASE),  # #2142: the DA model never runs here
        ("anthropic", False, AN_BASE),
        ("anthropic", True, AN_DA),
    ],
)
def test_every_da_combination_judges_the_sent_model(
    env, da_provider, da_model_set, expected
):
    if da_provider:
        env.setenv("DA_PROVIDER", da_provider)
    if da_model_set:
        env.setenv("FIREWORKS_DA_MODEL", FW_DA)
        env.setenv("ANTHROPIC_DA_MODEL", AN_DA)
    llm = LLMSettings()

    assert llm.get_da_model() == expected
    assert resolve_investigation_capability(NS(llm=llm), _Registry()).model == expected
    assert _investigation_row(llm).model == expected


def test_local_without_da_provider_judges_local_model(env):
    env.setenv("CHAT_PROVIDER", "local")
    env.setenv("LOCAL_LLM_MODEL", "qwen3:32b")
    assert LLMSettings().get_da_model() == "qwen3:32b"
