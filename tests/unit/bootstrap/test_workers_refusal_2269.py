"""``WORKERS>1`` is refused for the stores a worker would hold alone, and only for those (#2269).

The refusal keyed on ``SESSION_STORAGE_TYPE``, a setting no store read, and told
the operator to set ``CASE_STORAGE_TYPE=database``, another one, so following it
never cleared it. Every uvicorn worker composes its own container, and what makes
that wrong is a store held inside the process: the in-process FakeRedis, or a
local ChromaDB ``PersistentClient``. The refusal now asks the clients the
composition root built, and names the settings that change what it builds.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from faultmaven.bootstrap.lifespan import (
    _start_serving,
    refuse_per_process_stores_across_workers,
)

pytestmark = pytest.mark.unit


class _ChromaServerClient:
    """Stands in for ``chromadb.HttpClient``, which cannot be built without a
    reachable server. Its settings are chromadb's own, carrying the one field
    ``is_server_backed`` reads."""

    def get_settings(self):
        from chromadb.config import Settings

        return Settings(chroma_server_host="chroma.internal")


@pytest.fixture
async def clients(tmp_path):
    """Real clients of each kind: FakeRedis, a never-connected Redis, a local
    ``PersistentClient``. Every Redis client opened here is closed after."""
    import chromadb
    from fakeredis import aioredis as fake_aioredis
    from redis.asyncio import Redis

    opened = []

    def fake_redis():
        client = fake_aioredis.FakeRedis()
        opened.append(client)
        return client

    def server_redis():
        client = Redis(host="127.0.0.1", port=6390)  # never connects
        opened.append(client)
        return client

    def local_chroma(name):
        return chromadb.PersistentClient(path=str(tmp_path / name))

    yield SimpleNamespace(
        fake_redis=fake_redis,
        server_redis=server_redis,
        local_chroma=local_chroma,
        server_chroma=_ChromaServerClient,
    )
    for client in opened:
        await client.aclose()


def _app(*, redis, kb, evidence) -> FastAPI:
    """An app whose state carries what the composition root publishes."""
    app = FastAPI()
    app.state.redis_client = redis
    app.state.kb_chromadb_client = kb
    app.state.evidence_chromadb_client = evidence
    return app


def test_fakeredis_is_refused_and_the_message_names_what_clears_it(clients):
    app = _app(
        redis=clients.fake_redis(),
        kb=clients.server_chroma(),
        evidence=clients.server_chroma(),
    )

    with pytest.raises(ValueError) as refused:
        refuse_per_process_stores_across_workers(app, 2)

    message = str(refused.value)
    assert "FakeRedis" in message
    assert "WORKERS=1" in message and "REDIS_URL" in message
    assert "ChromaDB" not in message, "the Chroma stores are servers here"


def test_a_local_chroma_store_is_refused_and_the_message_names_chromadb_url(
    clients,
):
    app = _app(
        redis=clients.server_redis(),
        kb=clients.local_chroma("kb"),
        evidence=clients.local_chroma("evidence"),
    )

    with pytest.raises(ValueError) as refused:
        refuse_per_process_stores_across_workers(app, 4)

    message = str(refused.value)
    assert "KB and evidence ChromaDB" in message
    assert "CHROMADB_URL" in message and "WORKERS=1" in message
    assert "REDIS" not in message, "Redis is a server here"


def test_one_local_chroma_store_is_enough_to_refuse(clients):
    """The standalone fallback can build one server client and one local one."""
    app = _app(
        redis=clients.server_redis(),
        kb=clients.server_chroma(),
        evidence=clients.local_chroma("evidence"),
    )

    with pytest.raises(ValueError, match="the evidence ChromaDB store"):
        refuse_per_process_stores_across_workers(app, 2)


def test_both_process_local_stores_are_named_in_one_refusal(clients):
    app = _app(
        redis=clients.fake_redis(),
        kb=clients.local_chroma("kb"),
        evidence=clients.local_chroma("evidence"),
    )

    with pytest.raises(ValueError) as refused:
        refuse_per_process_stores_across_workers(app, 2)

    assert "REDIS_URL" in str(refused.value)
    assert "CHROMADB_URL" in str(refused.value)


def test_server_stores_admit_several_workers(clients):
    app = _app(
        redis=clients.server_redis(),
        kb=clients.server_chroma(),
        evidence=clients.server_chroma(),
    )

    refuse_per_process_stores_across_workers(app, 4)  # must not raise


def test_no_chroma_store_at_all_is_not_a_shared_store(clients):
    """``SKIP_SERVICE_CHECKS`` on standalone builds no ChromaDB client."""
    app = _app(redis=clients.server_redis(), kb=None, evidence=None)

    refuse_per_process_stores_across_workers(app, 2)  # must not raise


def test_one_worker_may_hold_every_store_itself(clients):
    """The standalone default: FakeRedis and local Chroma, in one process."""
    app = _app(
        redis=clients.fake_redis(),
        kb=clients.local_chroma("kb"),
        evidence=clients.local_chroma("evidence"),
    )

    refuse_per_process_stores_across_workers(app, 1)  # must not raise


async def test_the_lifespan_refuses_before_it_starts_anything(clients):
    """Wired into the startup that follows composition, as its first step:
    the settings stand-in carries ``workers`` alone, so any later step would
    fail on a missing attribute instead of refusing."""
    app = _app(redis=clients.fake_redis(), kb=None, evidence=None)
    settings = SimpleNamespace(server=SimpleNamespace(workers=2))

    with pytest.raises(ValueError, match="FakeRedis"):
        await _start_serving(app, settings)


# --- the settings the refusal names are the ones that decide ---------------


@pytest.fixture
def settings_from(monkeypatch):
    """Rebuild the settings singleton from an environment the test names."""
    from faultmaven.config.settings import get_settings, reset_settings

    def build(**environ):
        monkeypatch.setenv("CONFIG_PRESET", "none")
        monkeypatch.setenv("SKIP_SERVICE_CHECKS", "false")
        monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
        for name in ("REDIS_URL", "REDIS_HOST", "CHROMADB_URL"):
            monkeypatch.delenv(name, raising=False)
        for name, value in environ.items():
            monkeypatch.setenv(name, value)
        reset_settings()
        return get_settings()

    yield build
    reset_settings()


@pytest.mark.parametrize(
    "environ",
    [
        {"REDIS_URL": "redis://127.0.0.1:6390/0", "REDIS_HOST": ""},
        {"REDIS_HOST": "127.0.0.1"},
    ],
    ids=["REDIS_URL", "REDIS_HOST"],
)
async def test_naming_a_redis_server_is_what_turns_the_redis_arm_off(
    clients, settings_from, environ
):
    """The client factory builds a real client from either variable, and
    FakeRedis from neither; the refusal follows the client it built."""
    from faultmaven.infrastructure.redis_client import RedisClientFactory

    settings_from(REDIS_HOST="")
    unnamed = RedisClientFactory.create_client()
    with pytest.raises(ValueError, match="REDIS_URL"):
        refuse_per_process_stores_across_workers(
            _app(redis=unnamed, kb=None, evidence=None), 2
        )

    settings_from(**environ)
    named = RedisClientFactory.create_client()
    try:
        refuse_per_process_stores_across_workers(
            _app(redis=named, kb=None, evidence=None), 2
        )  # must not raise
    finally:
        await named.aclose()


def test_naming_a_chromadb_server_is_what_turns_the_chroma_arm_off(
    clients, settings_from, monkeypatch, tmp_path
):
    """The container's ChromaDB factory builds the server client from
    ``CHROMADB_URL``, and a local ``PersistentClient`` without it. The server
    client is the stand-in: ``HttpClient`` needs a reachable server."""
    from faultmaven.container.providers.infrastructure import _create_chromadb_client
    from faultmaven.infrastructure import chroma_client

    monkeypatch.setattr(
        chroma_client, "build_http_client", lambda *a, **k: _ChromaServerClient()
    )

    def kb_client(settings):
        return _create_chromadb_client(settings, str(tmp_path / "kb"), "KB")

    local = kb_client(settings_from())
    with pytest.raises(ValueError, match="CHROMADB_URL"):
        refuse_per_process_stores_across_workers(
            _app(redis=clients.server_redis(), kb=local, evidence=None), 2
        )

    server = kb_client(settings_from(CHROMADB_URL="http://chroma.internal:8000"))
    refuse_per_process_stores_across_workers(
        _app(redis=clients.server_redis(), kb=server, evidence=None), 2
    )  # must not raise
