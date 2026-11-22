"""The router reads live settings, so a config reload moves its timeout (#2320).

``LLMRouter`` used to snapshot ``get_settings()`` and ``LLM_REQUEST_TIMEOUT``
in its constructor. ``reload_llm_config`` (the dashboard's hot reload) replaces
the settings singleton and the registry but never the router, so after a reload
that switched the primary provider the router kept calling with the OLD
provider's per-provider timeout, while ``GET /admin/config/status`` reported the
new one from ``resolve_request_timeout(get_settings())``.

The invariant: the router's per-call ceiling is always
``resolve_request_timeout(get_settings())``, clamped to the turn, and its
``request_timeout`` (the routing health report's figure) is that ceiling
unclamped.

``_ALLOWED_OVERRIDES`` has no timeout key, so a reload moves the ceiling only
by switching the provider whose per-provider override applies; the base
(``LLMSettings.request_timeout``) moves with the live settings object itself.

Read live, the router also sees whatever the reload installs while it runs, so
the reload builds the next settings off the singleton and installs them in one
step: a call during its override read keeps the previous ceiling, and a build
that raises keeps the previous settings.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from faultmaven.config import llm_config_overrides
from faultmaven.config.settings import get_settings, reset_settings
from faultmaven.infrastructure.llm.providers.registry import reset_registry
from faultmaven.infrastructure.llm.router import LLMRouter, resolve_request_timeout
from faultmaven.infrastructure.persistence import llm_config_repository
from faultmaven.models.exceptions import ConfigurationError

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
    assert router.request_timeout == pytest.approx(OVERRIDES["fireworks"])
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


async def test_a_call_during_the_override_read_keeps_the_previous_ceiling(
    live_settings, monkeypatch
):
    """The reload awaits the override store. A call in that await resolves the
    ceiling the installed settings give (fireworks' 300 s), never the env-only
    settings being built (openai's 45 s); the reloaded one (anthropic, the
    30 s base) applies once the reload returns."""
    router = LLMRouter()
    await _reload_with(monkeypatch, {"primary_provider": "fireworks"})
    reading, release = asyncio.Event(), asyncio.Event()

    async def store_that_yields_mid_read():
        reading.set()
        await release.wait()
        return {"primary_provider": "anthropic"}

    monkeypatch.setattr(
        llm_config_repository, "get_all_overrides", store_that_yields_mid_read
    )
    reload = asyncio.create_task(llm_config_overrides.reload_llm_config())
    await reading.wait()

    async def a_call_during_the_read():
        return router._resolve_timeout()

    during = await asyncio.create_task(a_call_during_the_read())
    release.set()
    await reload

    assert during == pytest.approx(OVERRIDES["fireworks"])
    assert router._resolve_timeout() == pytest.approx(BASE_SECONDS)


async def test_a_rebuild_that_raises_keeps_the_previous_settings(
    live_settings, monkeypatch
):
    """A reload whose settings no longer validate raises, and the settings it
    would have replaced stay installed, so routed calls keep their ceiling."""
    router = LLMRouter()
    await _reload_with(monkeypatch, {"primary_provider": "fireworks"})
    previous = get_settings()
    monkeypatch.setenv("LLM_PROVIDER_TIMEOUT_OVERRIDES", "not json")

    with pytest.raises(ConfigurationError):
        await _reload_with(monkeypatch, {"primary_provider": "anthropic"})

    assert get_settings() is previous
    assert router._resolve_timeout() == pytest.approx(OVERRIDES["fireworks"])
