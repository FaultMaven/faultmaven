"""A failed override read never installs env-only settings over the operator's
(#2987).

``apply_overrides_to_settings`` and ``get_config_source_map`` used to read a
raising override store as an empty one: a reload then installed settings built
from the environment alone, a cloud startup served them, and
``GET /admin/llm/config`` reported every key ``env-default``. The read now
raises to its caller: a reload keeps the settings it would replace, a cloud
startup refuses, the provenance route answers its 500.

Two consequences are pinned with it. A save whose reload fails still bumps the
shared version, so the other replicas apply the stored value, and leaves this
replica's mark unassigned, so its own watcher retries. A reload that fails
blocks no other: a reload installs only when it is newer than the installed
one, so an earlier in-flight read still installs (the two- and three-way
interleavings below), and an older read returning after a newer install is
still skipped (``test_router_live_settings_2320.py``).
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import FastAPI

from faultmaven.bootstrap.composition import _wire_composition_root
from faultmaven.config import llm_config_overrides as cfg
from faultmaven.config.settings import (
    FaultMavenSettings,
    get_settings,
    reset_settings,
)
from faultmaven.container.errors import RequiredServiceConstructionError
from faultmaven.infrastructure.llm.providers import registry
from faultmaven.infrastructure.persistence import llm_config_repository

pytestmark = pytest.mark.unit


class StoreDown(Exception):
    """The override store's read failing (a database error)."""


@pytest.fixture
def live_settings(monkeypatch):
    """Settings built from a known env: openai primary."""
    monkeypatch.setenv("CHAT_PROVIDER", "openai")
    reset_settings()
    registry.reset_registry()
    yield
    monkeypatch.undo()
    reset_settings()
    registry.reset_registry()


def _store(monkeypatch, read) -> None:
    monkeypatch.setattr(llm_config_repository, "get_all_overrides", read)


def _provider() -> str:
    return get_settings().llm.provider.value


def _blocked_read(value: dict):
    """A read that waits for ``release``; ``reading`` is set once it began."""
    reading, release = asyncio.Event(), asyncio.Event()

    async def read():
        reading.set()
        await release.wait()
        return value

    return read, reading, release


async def _in_flight(monkeypatch, value: dict):
    """A reload whose override read is under way, and the event releasing it."""
    read, reading, release = _blocked_read(value)
    _store(monkeypatch, read)
    task = asyncio.create_task(cfg.reload_llm_config())
    await reading.wait()
    return task, release


async def _failed_reload(monkeypatch) -> None:
    _store(monkeypatch, AsyncMock(side_effect=StoreDown("db down")))
    with pytest.raises(StoreDown):
        await cfg.reload_llm_config()


# ── reload ────────────────────────────────────────────────────────────────


async def test_a_reload_whose_read_fails_keeps_the_installed_overrides(
    live_settings, monkeypatch
):
    _store(monkeypatch, AsyncMock(return_value={"primary_provider": "fireworks"}))
    await cfg.reload_llm_config()
    installed = get_settings()
    reset = MagicMock()
    monkeypatch.setattr(registry, "reset_registry", reset)

    await _failed_reload(monkeypatch)

    assert get_settings() is installed
    assert _provider() == "fireworks"
    reset.assert_not_called()


async def test_a_failed_reload_does_not_cancel_an_earlier_in_flight_one(
    live_settings, monkeypatch
):
    """X's read is under way when Y starts and fails: X still installs."""
    x, release_x = await _in_flight(monkeypatch, {"primary_provider": "fireworks"})
    await _failed_reload(monkeypatch)  # Y
    release_x.set()
    await x

    assert _provider() == "fireworks"


async def test_two_failed_reloads_do_not_cancel_an_earlier_in_flight_one(
    live_settings, monkeypatch
):
    """X in flight, then Y and Z fail: rolling a counter back would undo Z
    only, and X would still be skipped."""
    x, release_x = await _in_flight(monkeypatch, {"primary_provider": "fireworks"})
    await _failed_reload(monkeypatch)  # Y
    await _failed_reload(monkeypatch)  # Z
    release_x.set()
    await x

    assert _provider() == "fireworks"


async def test_an_older_read_returning_first_installs_then_the_newer_replaces_it(
    live_settings, monkeypatch
):
    a, release_a = await _in_flight(monkeypatch, {"primary_provider": "fireworks"})
    b, release_b = await _in_flight(monkeypatch, {"primary_provider": "anthropic"})
    release_a.set()
    await a
    assert _provider() == "fireworks"
    release_b.set()
    await b

    assert _provider() == "anthropic"


async def test_an_empty_store_installs_the_environments_settings(
    live_settings, monkeypatch
):
    _store(monkeypatch, AsyncMock(return_value={}))
    await cfg.reload_llm_config()

    assert _provider() == "openai"


# ── save ──────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_redis(monkeypatch):
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    monkeypatch.setattr(cfg, "_get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(cfg, "_last_applied_version", None)
    monkeypatch.setattr(llm_config_repository, "set_overrides", AsyncMock())
    return redis


async def test_a_save_whose_reload_fails_still_bumps_the_shared_version(
    shared_redis, monkeypatch
):
    """The write landed: the other replicas apply it, and this replica's mark
    stays unassigned so its own watcher retries the reload."""
    monkeypatch.setattr(
        cfg, "reload_llm_config", AsyncMock(side_effect=StoreDown("db down"))
    )

    with pytest.raises(StoreDown):
        await cfg.save_and_reload({"openai_model": "gpt-4o"}, user_id="u1")

    assert await cfg._read_config_version(shared_redis) == 1
    assert cfg._last_applied_version is None


async def test_a_save_whose_reload_succeeds_records_its_own_bump(
    shared_redis, monkeypatch
):
    monkeypatch.setattr(cfg, "reload_llm_config", AsyncMock())

    await cfg.save_and_reload({"openai_model": "gpt-4o"}, user_id="u1")

    assert await cfg._read_config_version(shared_redis) == 1
    assert cfg._last_applied_version == 1


# ── provenance ────────────────────────────────────────────────────────────


async def test_a_cloud_provenance_read_that_fails_raises(monkeypatch):
    _store(monkeypatch, AsyncMock(side_effect=StoreDown("db down")))

    with pytest.raises(StoreDown):
        await cfg.get_config_source_map(True)


async def test_standalone_provenance_reads_nothing(monkeypatch):
    read = AsyncMock(side_effect=StoreDown("db down"))
    _store(monkeypatch, read)

    sources = await cfg.get_config_source_map(False)

    assert set(sources.values()) == {"env-default"}
    read.assert_not_awaited()


# ── startup ───────────────────────────────────────────────────────────────


async def compose_cloud_with_a_failing_read(monkeypatch, error: Exception):
    """``_wire_composition_root`` under cloud, every step before the override
    arm stubbed, the override read raising ``error``. Returns the raise."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.delenv("WORKERS", raising=False)
    settings = FaultMavenSettings(_env_file=None)
    container = MagicMock()
    container.initialize = AsyncMock()
    _store(monkeypatch, AsyncMock(side_effect=error))
    with (
        patch("faultmaven.container.container", container),
        patch("faultmaven.bootstrap.startup.bootstrap_application", new=AsyncMock()),
        pytest.raises(RuntimeError) as raised,
    ):
        await _wire_composition_root(FastAPI(), settings)
    return raised.value


async def test_a_cloud_startup_whose_read_fails_refuses(monkeypatch, caplog):
    error = StoreDown("db down")
    caplog.set_level(logging.WARNING, logger="faultmaven.bootstrap.composition")

    raised = await compose_cloud_with_a_failing_read(monkeypatch, error)

    assert "Critical bootstrap failure" in str(raised)
    assert isinstance(raised.__cause__, RequiredServiceConstructionError)
    assert raised.__cause__.__cause__ is error
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "LLM config overrides" in r.getMessage()
    ]
    assert warnings == ["Could not apply LLM config overrides: StoreDown"]
