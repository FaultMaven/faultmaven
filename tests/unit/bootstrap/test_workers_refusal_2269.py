"""Several server processes are refused over a store a process would hold alone (#2269).

The refusal keyed on ``SESSION_STORAGE_TYPE``, a setting no store read, and told
the operator to set ``CASE_STORAGE_TYPE=database``, another one, so following it
never cleared it. Every uvicorn worker composes its own container, and what makes
that wrong is a store held inside the process: the in-process FakeRedis, or a
local ChromaDB ``PersistentClient``.

It is refused at the earliest point that can know it
(``faultmaven/config/worker_stores.py``): before composition when the
configuration decides it, right after the container builds its clients (before
the data layer migrates or seeds anything) for a runtime fallback, and when a
composition failure would leave a partial API.
"""

import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from faultmaven.bootstrap.composition import (
    _refuse_built_per_process_stores,
    _wire_composition_root,
    compose_application,
)
from faultmaven.config.worker_stores import (
    PerProcessStoresError,
    configured_per_process_stores,
    refuse_configured_per_process_stores,
)

pytestmark = pytest.mark.unit


class _ChromaServerClient:
    """Stands in for ``chromadb.HttpClient``, which cannot be built without a
    reachable server. Its settings are chromadb's own, carrying the one field
    ``is_server_backed`` reads."""

    def get_settings(self):
        from chromadb.config import Settings

        return Settings(chroma_server_host="chroma.internal")


def _closed_loopback_port() -> int:
    """A port this process bound and closed: nothing answers on it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def settings_from(monkeypatch):
    """Rebuild the settings singleton from an environment the test names.

    Every store is named by default (a Redis server, a ChromaDB server); a test
    unnames what it is about.
    """
    from faultmaven.config.settings import get_settings, reset_settings

    def build(**environ):
        base = {
            "CONFIG_PRESET": "none",
            "DEPLOYMENT_MODE": "standalone",
            "SKIP_SERVICE_CHECKS": "false",
            "RELOAD": "false",
            "WORKERS": "2",
            "REDIS_URL": "redis://127.0.0.1:6390/0",
            "REDIS_HOST": "",
            "CHROMADB_URL": "http://chroma.internal:8000",
            "VECTOR_STORAGE_TYPE": "chromadb",
            # Several workers need PostgreSQL (#2281); never connected to here.
            "DATABASE_URL": "postgresql+asyncpg://fm@127.0.0.1:1/fm",
        }
        for name, value in {**base, **environ}.items():
            monkeypatch.setenv(name, value)
        reset_settings()
        return get_settings()

    yield build
    reset_settings()


@pytest.fixture
def container(monkeypatch):
    """A container singleton of this test's own; the process one is put back."""
    from faultmaven.container import DIContainer
    from faultmaven.infrastructure.llm import usage_ledger

    monkeypatch.setattr(DIContainer, "_instance", None)
    instance = DIContainer()
    monkeypatch.setattr(
        usage_ledger, "_installed_ledger", usage_ledger.get_installed_usage_ledger()
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.close_database",
        AsyncMock(),
    )
    return instance


def _built(*, redis=None, kb=None, evidence=None, initialized=True):
    """What ``container.initialize()`` leaves for the refusal to read."""
    return SimpleNamespace(
        redis_client=redis,
        kb_chromadb_client=kb,
        evidence_chromadb_client=evidence,
        is_initialized=initialized,
    )


# --- before composition: what the configuration decides ---------------------


def test_skip_service_checks_is_refused_and_named_with_what_clears_it(settings_from):
    settings = settings_from(SKIP_SERVICE_CHECKS="true")

    with pytest.raises(PerProcessStoresError) as refused:
        refuse_configured_per_process_stores(settings)

    message = str(refused.value)
    assert "FakeRedis" in message and "SKIP_SERVICE_CHECKS=true" in message
    assert "SKIP_SERVICE_CHECKS=false with REDIS_URL" in message
    assert "WORKERS=1" in message
    assert "ChromaDB" not in message, "no ChromaDB client is built when skipping"


def test_no_redis_server_named_is_refused(settings_from):
    settings = settings_from(REDIS_URL="", REDIS_HOST="")

    with pytest.raises(PerProcessStoresError, match="neither REDIS_URL nor REDIS_HOST"):
        refuse_configured_per_process_stores(settings)


@pytest.mark.parametrize(
    "environ",
    [{"CHROMADB_URL": ""}, {"VECTOR_STORAGE_TYPE": "inmemory"}],
    ids=["no_chromadb_url", "vector_storage_type_not_chromadb"],
)
def test_no_chromadb_server_selected_is_refused(settings_from, environ):
    settings = settings_from(**environ)

    with pytest.raises(PerProcessStoresError) as refused:
        refuse_configured_per_process_stores(settings)

    assert "PersistentClient" in str(refused.value)
    assert "CHROMADB_URL naming a ChromaDB server" in str(refused.value)
    assert "FakeRedis" not in str(refused.value)


@pytest.mark.parametrize(
    "database_url",
    ["sqlite+aiosqlite:////tmp/fm-2281.db", "sqlite:///relative.db"],
)
def test_several_workers_over_sqlite_are_refused(settings_from, database_url):
    """SQLite is the one-box install and serves one process (#2281, owner
    ruling); several workers need PostgreSQL."""
    settings = settings_from(DATABASE_URL=database_url)

    with pytest.raises(PerProcessStoresError, match="SQLite") as refused:
        refuse_configured_per_process_stores(settings)
    assert "DATABASE_URL" in str(refused.value)


@pytest.mark.parametrize("environ", [{"WORKERS": "1"}, {"RELOAD": "true"}])
def test_one_process_over_sqlite_passes(settings_from, environ):
    refuse_configured_per_process_stores(
        settings_from(DATABASE_URL="sqlite+aiosqlite:////tmp/fm-2281.db", **environ)
    )


def test_named_servers_pass_the_configuration_check(settings_from):
    refuse_configured_per_process_stores(settings_from())  # must not raise


@pytest.mark.parametrize(
    "environ",
    [{"WORKERS": "1"}, {"WORKERS": "4", "RELOAD": "true"}],
    ids=["one_worker", "reload_serves_one_process"],
)
def test_one_serving_process_may_hold_every_store_itself(settings_from, environ):
    """Under ``RELOAD`` uvicorn serves one process and ignores ``WORKERS``."""
    settings = settings_from(SKIP_SERVICE_CHECKS="true", CHROMADB_URL="", **environ)

    refuse_configured_per_process_stores(settings)  # must not raise


async def test_the_configuration_check_never_refuses_what_would_be_built_shared(
    settings_from, tmp_path
):
    """Every store the configuration check calls process-local is one the
    container's own factories build process-local, without asking a server."""
    from faultmaven.container.providers.infrastructure import (
        _create_chromadb_client,
        create_redis_client,
    )
    from faultmaven.infrastructure.chroma_client import chroma_client_kind
    from faultmaven.infrastructure.redis_client import is_fakeredis

    for environ in (
        {"SKIP_SERVICE_CHECKS": "true"},
        {"REDIS_URL": "", "REDIS_HOST": ""},
        {"CHROMADB_URL": ""},
        {"VECTOR_STORAGE_TYPE": "inmemory"},
    ):
        settings = settings_from(**environ)
        verdict = {store.what for store in configured_per_process_stores(settings)}
        assert verdict, environ
        if any("FakeRedis" in what for what in verdict):
            assert is_fakeredis(await create_redis_client(settings)), environ
        if any("PersistentClient" in what for what in verdict):
            client = _create_chromadb_client(settings, str(tmp_path / "kb"), "KB")
            assert chroma_client_kind(client) == "persistent", environ


async def test_the_lifespan_refuses_before_it_composes_anything(
    settings_from, monkeypatch
):
    """The configuration's verdict is refused before composition: no container,
    no migrations, no admin seed, no data directory, no ChromaDB tree."""
    import faultmaven.bootstrap.lifespan as lifespan_module

    settings_from(SKIP_SERVICE_CHECKS="true")
    compose = AsyncMock()
    resolve_key = MagicMock()
    monkeypatch.setattr(lifespan_module, "compose_application", compose)
    monkeypatch.setattr(
        "faultmaven.infrastructure.security.pseudonym_key.resolve_pseudonym_key",
        resolve_key,
    )

    with pytest.raises(PerProcessStoresError, match="FakeRedis"):
        async with lifespan_module.lifespan(FastAPI()):
            pytest.fail("the lifespan must not yield after a refusal")

    compose.assert_not_called()
    resolve_key.assert_not_called()


# --- after the container builds its clients: what only they can tell -------


@pytest.fixture
async def clients(tmp_path):
    """Real clients of each kind; every Redis client opened here is closed."""
    from fakeredis import aioredis as fake_aioredis
    from redis.asyncio import Redis

    from faultmaven.infrastructure.persistence.chromadb_store import (
        create_persistent_client,
    )

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
        return create_persistent_client(str(tmp_path / name))

    yield SimpleNamespace(
        fake_redis=fake_redis,
        server_redis=server_redis,
        local_chroma=local_chroma,
        server_chroma=_ChromaServerClient,
    )
    for client in opened:
        await client.aclose()


def test_a_fakeredis_fallback_is_refused(settings_from, clients):
    built = _built(
        redis=clients.fake_redis(),
        kb=clients.server_chroma(),
        evidence=clients.server_chroma(),
    )

    with pytest.raises(PerProcessStoresError) as refused:
        _refuse_built_per_process_stores(built, settings_from())

    assert "did not answer" in str(refused.value)
    assert "REDIS_URL (or REDIS_HOST)" in str(refused.value)
    assert "ChromaDB" not in str(refused.value)


def test_no_redis_client_at_all_is_refused(settings_from, clients):
    """With none built, each process's middleware would build its own FakeRedis."""
    built = _built(kb=clients.server_chroma(), evidence=clients.server_chroma())

    with pytest.raises(PerProcessStoresError, match="no Redis client was built"):
        _refuse_built_per_process_stores(built, settings_from())


def test_a_container_that_did_not_initialize_is_refused_as_a_partial_composition(
    settings_from, clients
):
    """Outside pytest a failed ``initialize()`` is tolerated, and may have
    failed before building the Redis client a named REDIS_URL describes:
    naming REDIS_URL as the remedy would send the operator to a setting
    already made."""
    built = _built(kb=clients.server_chroma(), initialized=False)

    with pytest.raises(PerProcessStoresError, match="composition failed") as refused:
        _refuse_built_per_process_stores(built, settings_from())
    assert "REDIS_URL" not in str(refused.value)


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (("kb", "evidence"), "KB and evidence clients fell back to local ones"),
        (("evidence",), "evidence client fell back to a local one"),
    ],
    ids=["both", "one"],
)
def test_a_local_chroma_fallback_is_refused(settings_from, clients, local, expected):
    stores = {
        name: clients.local_chroma(name) if name in local else clients.server_chroma()
        for name in ("kb", "evidence")
    }
    built = _built(redis=clients.server_redis(), **stores)

    with pytest.raises(PerProcessStoresError) as refused:
        _refuse_built_per_process_stores(built, settings_from())

    assert expected in str(refused.value)
    assert "CHROMADB_URL naming a ChromaDB server" in str(refused.value)
    assert "REDIS_URL" not in str(refused.value)


def test_a_local_chroma_client_is_local_whatever_chroma_server_host_says(
    settings_from, clients, monkeypatch
):
    """chromadb's ``Settings`` read ``CHROMA_SERVER_HOST`` from the environment
    (and the working directory's ``.env``), so a local client left to them
    claimed a server and was admitted as one. The one door for local clients
    pins the host empty."""
    from faultmaven.api.dependencies import vector_storage_backend_name

    monkeypatch.setenv("CHROMA_SERVER_HOST", "chroma.internal")
    local = clients.local_chroma("kb")
    built = _built(redis=clients.server_redis(), kb=local)

    with pytest.raises(PerProcessStoresError, match="KB client fell back"):
        _refuse_built_per_process_stores(built, settings_from())
    assert vector_storage_backend_name(local, None) == (
        "chromadb (kb: persistent, evidence: disabled)"
    )


def test_server_clients_pass_and_the_log_says_only_what_was_checked(
    settings_from, clients, caplog
):
    built = _built(
        redis=clients.server_redis(),
        kb=clients.server_chroma(),
        evidence=clients.server_chroma(),
    )

    with caplog.at_level(logging.INFO, logger="faultmaven.bootstrap.composition"):
        _refuse_built_per_process_stores(built, settings_from())  # must not raise

    assert (
        "this process's Redis client is a server connection, and no ChromaDB "
        "client it built is a local PersistentClient"
    ) in caplog.text


def test_one_serving_process_skips_the_built_check(settings_from, clients):
    built = _built(redis=clients.fake_redis(), kb=clients.local_chroma("kb"))

    _refuse_built_per_process_stores(built, settings_from(RELOAD="true"))
    _refuse_built_per_process_stores(built, settings_from(WORKERS="1"))


async def test_a_named_but_unreachable_redis_is_refused_end_to_end(settings_from):
    """The motivating path: the container's Redis factory pings the named server,
    finds nothing, falls back to FakeRedis on standalone, and is refused."""
    from faultmaven.container.providers.infrastructure import create_redis_client

    settings = settings_from(REDIS_URL=f"redis://127.0.0.1:{_closed_loopback_port()}/0")
    refuse_configured_per_process_stores(settings)  # it names a server

    built = _built(redis=await create_redis_client(settings), kb=_ChromaServerClient())

    with pytest.raises(PerProcessStoresError, match="did not answer"):
        _refuse_built_per_process_stores(built, settings)


def test_an_unreachable_chroma_server_is_refused_end_to_end(
    settings_from, clients, monkeypatch, tmp_path
):
    """The container's ChromaDB factory falls back to a local client when the
    server cannot be reached (simulated where ``HttpClient`` is built: it needs
    a server), and that client is refused."""
    from faultmaven.container.providers.infrastructure import _create_chromadb_client
    from faultmaven.infrastructure import chroma_client

    def unreachable(*args, **kwargs):
        raise ValueError("Could not connect to a Chroma server")

    monkeypatch.setattr(chroma_client, "build_http_client", unreachable)
    settings = settings_from()
    refuse_configured_per_process_stores(settings)  # it selects a server

    kb = _create_chromadb_client(settings, str(tmp_path / "kb"), "KB")
    built = _built(redis=clients.server_redis(), kb=kb)

    with pytest.raises(PerProcessStoresError, match="KB client fell back"):
        _refuse_built_per_process_stores(built, settings)


async def test_composition_refuses_before_the_data_layer(
    settings_from, container, clients, monkeypatch
):
    """Placed right after ``container.initialize()``: a refused process never
    reaches ``bootstrap_application`` (migrations, the admin seed)."""
    settings = settings_from()
    fake = clients.fake_redis()

    async def initialize():
        container._initialized = True
        container.redis_client = fake

    monkeypatch.setattr(container, "initialize", initialize)
    bootstrap = AsyncMock()
    monkeypatch.setattr("faultmaven.bootstrap.startup.bootstrap_application", bootstrap)

    with pytest.raises(PerProcessStoresError, match="FakeRedis"):
        await _wire_composition_root(FastAPI(), settings)

    bootstrap.assert_not_called()


# --- a composition failure in a mode that permits a partial API ------------


@pytest.mark.parametrize("workers", [1, 2])
async def test_a_partial_api_is_refused_only_across_several_processes(
    container, monkeypatch, workers
):
    failure = ValueError("planted composition failure")
    monkeypatch.setattr(
        "faultmaven.bootstrap.composition._wire_composition_root",
        AsyncMock(side_effect=failure),
    )
    settings = SimpleNamespace(
        must_not_degrade=False,
        is_cloud=False,
        server=SimpleNamespace(workers=workers, reload=False),
    )

    if workers == 1:
        await compose_application(FastAPI(), settings)  # tolerated: partial API
        return
    with pytest.raises(PerProcessStoresError) as refused:
        await compose_application(FastAPI(), settings)
    assert "planted composition failure" in str(refused.value)
    assert refused.value.__cause__ is failure
