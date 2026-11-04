"""A composition's last shutdown closes everything it opened (#2490, #1843).

The observable is the process's own, so it catches a resource left unrecorded
whatever its type: compose, shut down, collect, and the descriptor and thread
counts are back where the first cycle left them (some of what that starts stays
the process's). Both path shapes: a new store path each cycle, as tests compose,
and the same one, as a production process does. On ``main`` the new-path shape
grew by 12 descriptors and 102 threads a cycle. Removing a registration makes
it grow. The sanitizer's session opens no socket until it is used, and the suite
has no real Redis, so their rows check that a stand-in is closed instead.
"""

from __future__ import annotations

import gc
import inspect
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from sqlalchemy import text

from faultmaven.config.settings import reset_settings
from faultmaven.container.base import BaseDIContainer
from faultmaven.container.providers import infrastructure
from faultmaven.infrastructure.llm import usage_ledger
from tests.chroma_clients import systems_open_under

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self"),
]


def _held() -> tuple[int, int]:
    """The process's open descriptors and its threads, native ones included."""
    return len(os.listdir("/proc/self/fd")), len(os.listdir("/proc/self/task"))


def _descriptors_under(root) -> list[str]:
    prefix = str(root) + os.sep
    held = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(prefix):
            held.append(target)
    return held


@pytest.fixture
def stores_at(monkeypatch, tmp_path):
    """Compose with service checks on, the two local ChromaDB stores at
    ``tmp_path / name``. Every CI job sets ``SKIP_SERVICE_CHECKS=true``, under
    which neither store is built; Presidio is not the subject either."""
    monkeypatch.setenv("SKIP_SERVICE_CHECKS", "false")
    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    monkeypatch.setenv("PROTECTION_ENABLED", "false")
    monkeypatch.setenv("SANITIZE_PII", "false")

    def _at(name: str) -> None:
        monkeypatch.setenv("CHROMADB_KB_PERSIST_DIR", str(tmp_path / name / "kb"))
        monkeypatch.setenv(
            "CHROMADB_EVIDENCE_PERSIST_DIR", str(tmp_path / name / "evidence")
        )
        reset_settings()

    yield _at
    reset_settings()


@pytest.fixture
async def drop_registration(monkeypatch):
    """Remove one registration: ``own`` skips the resource ``what`` names. What
    it skipped is closed after the test, so the leak it shows ends there."""
    skipped = []
    own = BaseDIContainer.own

    def _drop(dropped: str) -> None:
        def _own(self, resource, *, close, what):
            if what != dropped:
                return own(self, resource, close=close, what=what)
            skipped.append((resource, close))
            return resource

        monkeypatch.setattr(BaseDIContainer, "own", _own)

    yield _drop
    for resource, close in skipped:
        if resource is not None and inspect.isawaitable(outcome := close(resource)):
            await outcome


async def _cycle(container, stores_at, name: str) -> tuple[int, int]:
    stores_at(name)
    await container.initialize()
    assert await container.shutdown() is True
    gc.collect()
    return _held()


@pytest.mark.parametrize("same_path", [False, True], ids=["new path", "same path"])
async def test_each_cycle_returns_to_what_the_first_left(
    same_path, stores_at, fresh_container
):
    baseline = await _cycle(fresh_container, stores_at, "store-0")

    after = [
        await _cycle(fresh_container, stores_at, "store-0" if same_path else name)
        for name in ("store-1", "store-2", "store-3")
    ]

    assert after == [baseline] * 3, f"(fds, threads) after the first: {baseline}"


@pytest.mark.parametrize(
    "dropped", ["the KB ChromaDB client", "the evidence ChromaDB client"]
)
async def test_without_its_registration_a_store_is_left_open(
    dropped, stores_at, fresh_container, drop_registration
):
    drop_registration(dropped)
    baseline = await _cycle(fresh_container, stores_at, "store-0")

    grown = await _cycle(fresh_container, stores_at, "store-1")

    assert grown[0] > baseline[0], (grown, baseline)


class _ServerRedis(FakeRedis):
    """A client the container must close: ``is_fakeredis`` reads it as real."""

    closes = 0

    async def aclose(self, *args, **kwargs):
        self.closes += 1
        await super().aclose(*args, **kwargs)


def _stand_in_session(monkeypatch):
    session = Mock()
    build = infrastructure.create_sanitizer

    def _create(settings):
        sanitizer = build(settings)
        sanitizer.session.close()
        sanitizer.session = session
        return sanitizer

    monkeypatch.setattr(infrastructure, "create_sanitizer", _create)
    return lambda: session.close.call_count


def _stand_in_redis(monkeypatch):
    client = _ServerRedis(server=FakeServer(), decode_responses=True)
    monkeypatch.setattr(
        infrastructure, "create_redis_client", AsyncMock(return_value=client)
    )
    return lambda: client.closes


_STAND_INS = {
    "the sanitizer's HTTP session": _stand_in_session,
    "the Redis client": _stand_in_redis,
}


@pytest.mark.parametrize("registered", [True, False], ids=["owned", "dropped"])
@pytest.mark.parametrize("what", sorted(_STAND_INS))
async def test_the_shutdown_closes_what_holds_no_descriptor_yet(
    what, registered, reset_container, drop_registration, monkeypatch
):
    closes = _STAND_INS[what](monkeypatch)
    if not registered:
        drop_registration(what)

    await reset_container.initialize()
    await reset_container.shutdown()

    assert closes() == (1 if registered else 0)


async def test_the_loops_fakeredis_is_not_the_compositions_to_close(
    reset_container,
):
    await reset_container.initialize()
    client = reset_container.redis_client
    assert all(owned is not client for _what, owned, _close in BaseDIContainer._owned)

    await reset_container.shutdown()

    assert await client.ping()


async def test_a_composition_refused_part_way_strands_nothing(
    stores_at, fresh_container, monkeypatch, tmp_path
):
    """The KB client is built before the evidence client fails to be, and is
    already recorded: the shutdown that follows the refusal closes it."""

    def _no_evidence_store(settings):
        raise OSError("the evidence store cannot be opened")

    monkeypatch.setattr(
        infrastructure, "create_evidence_chromadb_client", _no_evidence_store
    )
    stores_at("store")

    with pytest.raises(RuntimeError, match="the evidence store cannot be opened"):
        await fresh_container.initialize()
    assert "the KB ChromaDB client" in [w for w, _r, _c in BaseDIContainer._owned]
    assert _descriptors_under(tmp_path / "store" / "kb")  # positive control

    await fresh_container.shutdown()
    gc.collect()

    assert _descriptors_under(tmp_path / "store" / "kb") == []


async def test_two_live_lifespans_close_nothing_until_the_last_ends(
    stores_at, fresh_container, monkeypatch, tmp_path
):
    """#1843: the second lifespan's ``initialize()`` returns early and shares
    the first one's clients. Its shutdown disposed the engine the first still
    served from, and would have closed the first one's ChromaDB clients."""
    from faultmaven.bootstrap import lifespan as lifespan_module
    from faultmaven.infrastructure.persistence import database

    monkeypatch.setattr(lifespan_module, "_bootstrap_kb_step", AsyncMock())
    monkeypatch.setattr(
        usage_ledger, "_installed_ledger", usage_ledger.get_installed_usage_ledger()
    )
    stores_at("store")

    async with lifespan_module.lifespan(FastAPI()):
        kb, engine = fresh_container.kb_chromadb_client, database.get_engine()
        async with lifespan_module.lifespan(FastAPI()):
            assert fresh_container.kb_chromadb_client is kb

        kb.heartbeat()  # a closed client raises AttributeError
        assert database._engine is engine, "the second lifespan disposed it"
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    assert systems_open_under(tmp_path) == []
    assert database._engine is None
