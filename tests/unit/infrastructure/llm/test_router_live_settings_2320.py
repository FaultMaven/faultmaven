"""The router reads live settings, so a config reload moves its timeout (#2320).

``LLMRouter`` used to snapshot ``get_settings()`` and ``LLM_REQUEST_TIMEOUT``
in its constructor. ``reload_llm_config`` (the dashboard's hot reload) replaces
the settings singleton and the registry but never the router, so after a reload
that switched the primary provider the router kept calling with the OLD
provider's per-provider timeout, while ``GET /admin/config/status`` reported the
new one from ``resolve_request_timeout(get_settings())``.

The invariant: the router's per-call ceiling is always
``resolve_request_timeout(get_settings())``, clamped to the turn.

``_ALLOWED_OVERRIDES`` has no timeout key, so a reload moves the ceiling only
by switching the provider whose per-provider override applies; the base
``request_timeout`` moves with the live settings object itself.
"""

import json
from unittest.mock import AsyncMock

import pytest

from faultmaven.config import llm_config_overrides
from faultmaven.config.settings import get_settings, reset_settings
from faultmaven.infrastructure.llm.providers.registry import reset_registry
from faultmaven.infrastructure.llm.router import LLMRouter, resolve_request_timeout
from faultmaven.infrastructure.persistence import llm_config_repository

pytestmark = pytest.mark.unit

BASE_SECONDS = 30
OVERRIDES = {"openai": 45, "fireworks": 300}


@pytest.fixture
def live_settings(monkeypatch):
    """Settings built from a known env: openai primary, both overrides set."""
    monkeypatch.setenv("CHAT_PROVIDER", "openai")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", str(BASE_SECONDS))
    monkeypatch.setenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", json.dumps(OVERRIDES))
    reset_settings()
    reset_registry()
    yield
    monkeypatch.undo()
    reset_settings()
    reset_registry()


async def _reload_with(monkeypatch, overrides: dict) -> None:
    """``reload_llm_config`` through its real path, the override store stubbed."""
    monkeypatch.setattr(
        llm_config_repository, "get_all_overrides", AsyncMock(return_value=overrides)
    )
    await llm_config_overrides.reload_llm_config()


async def test_a_reload_that_switches_the_provider_moves_the_routers_timeout(
    live_settings, monkeypatch
):
    """The router built before the reload answers with the NEW provider's
    override, the figure the status report computes from the live settings."""
    router = LLMRouter()
    assert router._resolve_timeout() == pytest.approx(OVERRIDES["openai"])

    await _reload_with(monkeypatch, {"primary_provider": "fireworks"})

    assert get_settings().llm.provider.value == "fireworks"  # the reload took
    assert router._resolve_timeout() == pytest.approx(OVERRIDES["fireworks"])
    assert router._resolve_timeout() == pytest.approx(
        resolve_request_timeout(get_settings())
    )
    assert router.settings is get_settings()


async def test_a_provider_with_no_override_falls_back_to_the_live_base(
    live_settings, monkeypatch
):
    """A switch to a provider with no override lands on the base, not on the
    previous provider's override."""
    router = LLMRouter()
    await _reload_with(monkeypatch, {"primary_provider": "anthropic"})

    assert router._resolve_timeout() == pytest.approx(BASE_SECONDS)


def test_request_timeout_reads_the_live_settings_when_the_env_is_unset(
    live_settings, monkeypatch
):
    """Env first, then the live settings: with ``LLM_REQUEST_TIMEOUT`` unset, a
    change to the settings object reaches a router built before it."""
    router = LLMRouter()
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT")
    object.__setattr__(get_settings().llm, "request_timeout", 75)

    assert router.request_timeout == pytest.approx(75)
    assert router._resolve_timeout() == pytest.approx(
        resolve_request_timeout(get_settings())
    )


def test_settings_cannot_be_assigned(live_settings):
    """Read-only, so no caller can re-introduce a snapshot."""
    router = LLMRouter()
    with pytest.raises(AttributeError):
        router.settings = get_settings()
    with pytest.raises(AttributeError):
        router.request_timeout = 1.0
