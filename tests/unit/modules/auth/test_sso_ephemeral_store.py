"""Unit tests for the SSO single-use ephemeral store (ADR-015, PR 2a).

The store must guarantee: a stored value round-trips once, a second consume
returns None (single-use via GETDEL), state and completion namespaces don't
collide, and a too-small TTL is rejected. Backed by in-process FakeRedis, which
mirrors the async Redis client used in cloud.
"""

from __future__ import annotations

import fakeredis.aioredis as fakeredis
import pytest

from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
    SSOEphemeralStore,
)


@pytest.fixture
def store():
    return SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=True))


async def test_state_round_trips_once(store):
    await store.put_state("st-1", {"return_to": "/cases"}, ttl_seconds=600)
    assert await store.consume_state("st-1") == {"return_to": "/cases"}


async def test_state_is_single_use(store):
    await store.put_state("st-1", {"return_to": "/cases"}, ttl_seconds=600)
    assert await store.consume_state("st-1") is not None
    # A replayed callback (same state) finds nothing.
    assert await store.consume_state("st-1") is None


async def test_consume_missing_state_returns_none(store):
    assert await store.consume_state("never-stored") is None


async def test_login_code_round_trips_once(store):
    payload = {"access_token": "a", "refresh_token": "r", "session_id": "s"}
    await store.put_login("code-1", payload, ttl_seconds=60)
    assert await store.consume_login("code-1") == payload


async def test_login_code_is_single_use(store):
    await store.put_login("code-1", {"access_token": "a"}, ttl_seconds=60)
    assert await store.consume_login("code-1") is not None
    assert await store.consume_login("code-1") is None


async def test_state_and_login_namespaces_are_isolated(store):
    # Same raw key value must not collide across the two namespaces.
    await store.put_state("dup", {"kind": "state"}, ttl_seconds=60)
    await store.put_login("dup", {"kind": "login"}, ttl_seconds=60)
    assert await store.consume_state("dup") == {"kind": "state"}
    assert await store.consume_login("dup") == {"kind": "login"}


async def test_ttl_must_be_positive(store):
    with pytest.raises(ValueError):
        await store.put_state("st", {"x": 1}, ttl_seconds=0)


async def test_consume_decodes_bytes_payload():
    # A client without decode_responses returns bytes; the store must decode.
    store = SSOEphemeralStore(fakeredis.FakeRedis(decode_responses=False))
    await store.put_login("code-b", {"access_token": "a"}, ttl_seconds=60)
    assert await store.consume_login("code-b") == {"access_token": "a"}
