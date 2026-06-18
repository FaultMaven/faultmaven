"""Multi-replica propagation of cloud LLM config changes.

A UI config write hot-reloads only the replica that served it. The shared Redis
config version + per-replica watcher (``faultmaven.config.llm_config_overrides``)
is what makes the change reach the OTHER replicas. These tests pin that contract:

* a write bumps the shared version,
* a watcher reloads when the version advances beyond what it has applied,
* a watcher does NOT reload on an unchanged version,
* a FakeRedis fallback (Redis down/unset) is REJECTED — not silently accepted —
  so the bump warns and the watcher exits instead of operating on fake state
  that cannot propagate across replicas.
"""

import asyncio
import logging

import pytest

import faultmaven.config.llm_config_overrides as cfg


@pytest.fixture
def fake_redis():
    """Fresh in-process async Redis with decode_responses (matches prod client)."""
    import fakeredis.aioredis as fakeredis_aio

    return fakeredis_aio.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def _reset_version():
    """Each test starts from a clean per-process high-water mark."""
    cfg._last_applied_version = None
    yield
    cfg._last_applied_version = None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_version_absent_is_zero(fake_redis):
    assert await cfg._read_config_version(fake_redis) == 0
    await fake_redis.set(cfg._CONFIG_VERSION_KEY, 7)
    assert await cfg._read_config_version(fake_redis) == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_and_reload_bumps_shared_version(fake_redis, monkeypatch):
    """A config write persists, reloads locally, and advances the shared version."""
    reloaded = []
    monkeypatch.setattr(cfg, "_get_redis", lambda: _coro(fake_redis))
    monkeypatch.setattr(
        cfg,
        "reload_llm_config",
        lambda: _coro(reloaded.append(True)),
    )

    async def _fake_set_overrides(overrides, user_id=None):
        return None

    # set_overrides is imported inside save_and_reload; patch at the source module.
    import faultmaven.infrastructure.persistence.llm_config_repository as repo

    monkeypatch.setattr(repo, "set_overrides", _fake_set_overrides)

    await cfg.save_and_reload({"openai_model": "gpt-4o"}, user_id="u1")

    assert reloaded == [True]  # local hot-reload happened
    assert await cfg._read_config_version(fake_redis) == 1  # shared version bumped
    assert cfg._last_applied_version == 1  # this replica recorded its own bump


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watcher_reloads_when_version_advances(fake_redis, monkeypatch):
    reloads = []
    monkeypatch.setattr(cfg, "_get_redis", lambda: _coro(fake_redis))
    monkeypatch.setattr(cfg, "reload_llm_config", lambda: _coro(reloads.append(True)))

    task = asyncio.create_task(cfg.watch_config_version(poll_interval=0.01))
    await asyncio.sleep(0.03)  # let it seed from version 0
    assert reloads == []  # nothing to do yet

    await fake_redis.incr(cfg._CONFIG_VERSION_KEY)  # another replica wrote config
    await asyncio.sleep(0.05)  # let a poll observe the bump

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert reloads == [True]  # reloaded exactly once for the single bump
    assert cfg._last_applied_version == 1


def _patch_underlying_redis(monkeypatch, client):
    """Make get_async_redis_client (what _get_redis calls) return `client`.

    Patches the REAL infrastructure factory rather than _get_redis itself, so the
    FakeRedis-rejection guard inside _get_redis is actually exercised. This mirrors
    production: on a Redis outage get_async_redis_client swallows the error and
    returns a FakeRedis — it never raises.
    """
    import faultmaven.infrastructure.redis_client as rc

    async def _fake_get(redis_url=None, host=None, port=None):
        return client

    monkeypatch.setattr(rc, "get_async_redis_client", _fake_get)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_redis_rejects_fakeredis_fallback(fake_redis, monkeypatch):
    """A FakeRedis cannot propagate across replicas, so _get_redis must reject it."""
    _patch_underlying_redis(monkeypatch, fake_redis)
    with pytest.raises(RuntimeError, match="shared Redis"):
        await cfg._get_redis()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watcher_exits_when_redis_falls_back_to_fakeredis(
    fake_redis, monkeypatch
):
    """The real outage path: get_async_redis_client returns FakeRedis (never raises).

    _get_redis rejects it, so the watcher exits at startup instead of binding to
    fake state and polling it forever (the bug the guard prevents).
    """
    _patch_underlying_redis(monkeypatch, fake_redis)
    reloads = []
    monkeypatch.setattr(cfg, "reload_llm_config", lambda: _coro(reloads.append(True)))

    # Returns promptly (exits) without raising and without reloading.
    await asyncio.wait_for(cfg.watch_config_version(poll_interval=0.01), timeout=1.0)
    assert reloads == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_and_reload_warns_and_skips_bump_when_redis_unavailable(
    fake_redis, monkeypatch, caplog
):
    """Redis down: local apply still happens, but the bump is skipped WITH a warning
    (not a silent success against fake state that never reaches other replicas)."""
    _patch_underlying_redis(monkeypatch, fake_redis)
    reloaded = []
    monkeypatch.setattr(cfg, "reload_llm_config", lambda: _coro(reloaded.append(True)))

    import faultmaven.infrastructure.persistence.llm_config_repository as repo

    async def _fake_set_overrides(overrides, user_id=None):
        return None

    monkeypatch.setattr(repo, "set_overrides", _fake_set_overrides)

    with caplog.at_level(logging.WARNING):
        await cfg.save_and_reload({"openai_model": "gpt-4o"}, user_id="u1")

    assert reloaded == [True]  # local apply was NOT blocked
    assert cfg._last_applied_version is None  # bump skipped — no fake counter recorded
    assert await cfg._read_config_version(fake_redis) == 0  # fake counter untouched
    assert any(
        "could not bump" in r.message.lower() for r in caplog.records
    )  # the operator-visible warning fired


def _coro(value):
    """Wrap a value in an awaitable so a lambda can stand in for an async fn."""

    async def _inner():
        return value

    return _inner()
