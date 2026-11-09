"""The replay cache's per-case index and the case delete's eraser (#2624),
through the real middleware on one FakeRedis, on a single event loop. An entry
is indexed by the case its route declares, never by ids its exchange mentions.
"""

import inspect
import time

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import Body, Depends, FastAPI, Request

from faultmaven.api.middleware import idempotency
from faultmaven.api.middleware.idempotency import (
    KEY_PREFIX,
    IdempotencyMiddleware,
    IdempotencyReplayEraser,
    case_index_key,
    declare_replay_case,
    declare_replay_case_from_path,
    declare_replay_case_from_query,
)

pytestmark = [pytest.mark.unit]

CASE_A, CASE_B = "case_0123456789ab", "case_ba9876543210"
KEY_A, KEY_B = "key-2624-aaaaaaaa", "key-2624-bbbbbbbb"
CALLER = "Bearer caller-2624-aaaaaaaa"
ENTERPRISE = "ent_2624"
# What the regex scan missed or wrongly matched: an escape, a Unicode letter
# before the id, an ``_`` after it.
MENTIONS = f"案件{CASE_B}\n{CASE_B}_postmortem.md"


def _app(prefix: str = KEY_PREFIX):
    app, fake, runs = FastAPI(), fakeredis_aio.FakeRedis(decode_responses=True), []

    @app.post("/api/v1/cases")
    async def create(request: Request, payload: dict = Body(default={})):
        runs.append(payload)
        declare_replay_case(request, payload["case_id"])
        return {"case_id": payload["case_id"], "title": MENTIONS, "run": len(runs)}

    path_case = [Depends(declare_replay_case_from_path)]

    @app.post("/api/v1/cases/{case_id}/title", dependencies=path_case)
    async def title(case_id: str):
        return {"title": MENTIONS}

    query_case = [Depends(declare_replay_case_from_query)]

    @app.post("/api/v1/reports/generate", dependencies=query_case)
    async def generate(case_id: str):
        return {"report": MENTIONS}

    @app.post("/api/v1/echo")
    async def echo():
        return {"named": f"{CASE_A} {MENTIONS}"}

    def middleware(inner, *, redis_client):
        built = IdempotencyMiddleware(inner, redis_client=redis_client)
        built.key_prefix = prefix
        return built

    app.add_middleware(middleware, redis_client=fake)
    return app, fake


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _post(client, path, key, body=None):
    headers = {"Authorization": CALLER, "Idempotency-Key": key}
    return client.post(path, headers=headers, json=body or {})


def _replayed(response) -> bool:
    return response.headers.get("X-Idempotency-Replayed") == "true"


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/v1/cases", {"case_id": CASE_A}),
        (f"/api/v1/cases/{CASE_A}/title", None),
        (f"/api/v1/reports/generate?case_id={CASE_A}", None),
    ],
    ids=["create", "path", "query"],
)
async def test_only_the_declared_case_indexes_the_entry(path, body):
    app, fake = _app()
    async with _client(app) as client:
        assert (await _post(client, path, KEY_A, body)).status_code == 200

    index = case_index_key(KEY_PREFIX, CASE_A)
    (entry,) = [key for key in await fake.keys("idempotency:*") if key != index]
    assert await fake.zrange(index, 0, -1) == [entry]
    assert 0 < await fake.ttl(index) <= 3600
    assert not await fake.exists(case_index_key(KEY_PREFIX, CASE_B))


def test_the_declarations_run_on_the_event_loop():
    """A sync dependency runs in a worker thread, here on every case-router
    request; these do no I/O."""
    assert inspect.iscoroutinefunction(declare_replay_case_from_path)
    assert inspect.iscoroutinefunction(declare_replay_case_from_query)


async def test_an_undeclared_answer_naming_a_case_is_indexed_by_none():
    app, fake = _app()
    async with _client(app) as client:
        assert (await _post(client, "/api/v1/echo", KEY_A)).status_code == 200

    assert await fake.keys(f"{KEY_PREFIX}case:*") == []


async def test_the_eraser_clears_the_case_and_leaves_another_cases_replay():
    """Under a namespaced prefix, which the eraser takes as the middleware's."""
    prefix = "ns-2624:idempotency:"
    app, fake = _app(prefix)
    async with _client(app) as client:
        await _post(client, "/api/v1/cases", KEY_A, {"case_id": CASE_A})
        await _post(client, "/api/v1/cases", KEY_B, {"case_id": CASE_B})

        eraser = IdempotencyReplayEraser(fake, key_prefix=prefix)
        assert await eraser.erase_case(CASE_B, ENTERPRISE) == 1
        assert not await fake.exists(case_index_key(prefix, CASE_B))

        again_a = await _post(client, "/api/v1/cases", KEY_A, {"case_id": CASE_A})
        again_b = await _post(client, "/api/v1/cases", KEY_B, {"case_id": CASE_B})

    # A's answer mentions B, and B's delete leaves it replaying.
    assert _replayed(again_a) and again_a.json()["run"] == 1
    assert not _replayed(again_b) and again_b.json()["run"] == 3


async def test_the_next_write_prunes_expired_members():
    app, fake = _app()
    index = case_index_key(KEY_PREFIX, CASE_A)
    await fake.zadd(index, {"idempotency:key-2624-expired:h": time.time() - 1})
    async with _client(app) as client:
        await _post(client, f"/api/v1/cases/{CASE_A}/title", KEY_A)

    (member,) = await fake.zrange(index, 0, -1)
    assert member != "idempotency:key-2624-expired:h"


async def test_a_stale_member_whose_key_now_serves_another_case_is_skipped():
    """A create's key names no case: reused after its entry expired, it caches
    case Y's answer while case X's index still lists it, stale."""
    app, fake = _app()
    x_index = case_index_key(KEY_PREFIX, CASE_A)
    async with _client(app) as client:
        await _post(client, "/api/v1/cases", KEY_A, {"case_id": CASE_A})
        (entry,) = await fake.zrange(x_index, 0, -1)
        await fake.delete(entry)  # its hour passes
        await fake.zadd(x_index, {entry: time.time() - 1})
        await _post(client, "/api/v1/cases", KEY_A, {"case_id": CASE_B})

        assert await IdempotencyReplayEraser(fake).erase_case(CASE_A, ENTERPRISE) == 0
        retry = await _post(client, "/api/v1/cases", KEY_A, {"case_id": CASE_B})

    assert _replayed(retry)
    assert (retry.json()["case_id"], retry.json()["run"]) == (CASE_B, 2)


def _watch_unlinks(monkeypatch, fake, refuse=None):
    """Record each ``UNLINK`` batch, a client call or one queued in a
    transaction, and refuse batch number ``refuse`` with a Redis error."""
    batches, unlink, pipeline = [], fake.unlink, fake.pipeline

    def record(keys):
        batches.append(len(keys))
        if len(batches) == refuse:
            raise ConnectionError("redis unreachable")

    async def client_unlink(*keys):
        record(keys)
        return await unlink(*keys)

    def watched_pipeline(*args, **kwargs):
        transaction = pipeline(*args, **kwargs)
        queue = transaction.unlink

        def queued_unlink(*keys):
            record(keys)
            return queue(*keys)

        transaction.unlink = queued_unlink
        return transaction

    monkeypatch.setattr(fake, "unlink", client_unlink)
    monkeypatch.setattr(fake, "pipeline", watched_pipeline)
    return batches


async def test_a_retried_delete_after_a_failed_batch_removes_the_rest(monkeypatch):
    """A failed batch fails the delete and keeps the case row (#1016), so the
    retry must still find the entries the failure left."""
    monkeypatch.setattr(idempotency, "ERASE_BATCH", 1)
    app, fake = _app()
    title = f"/api/v1/cases/{CASE_A}/title"
    async with _client(app) as client:
        for key in (KEY_A, KEY_B):
            await _post(client, title, key)
        _watch_unlinks(monkeypatch, fake, refuse=2)
        eraser = IdempotencyReplayEraser(fake)

        with pytest.raises(ConnectionError):
            await eraser.erase_case(CASE_A, ENTERPRISE)
        assert await eraser.erase_case(CASE_A, ENTERPRISE) == 1
        retries = [await _post(client, title, key) for key in (KEY_A, KEY_B)]

    assert not any(_replayed(retry) for retry in retries)


async def test_an_entry_cached_after_the_read_stays_indexed_for_a_later_delete(
    monkeypatch,
):
    app, fake = _app()
    index, title = case_index_key(KEY_PREFIX, CASE_A), f"/api/v1/cases/{CASE_A}/title"
    zrange = fake.zrange
    async with _client(app) as client:
        await _post(client, title, KEY_A)

        async def read_then_cache_one(*args, **kwargs):
            read = await zrange(*args, **kwargs)
            await _post(client, title, KEY_B)
            return read

        monkeypatch.setattr(fake, "zrange", read_then_cache_one)
        eraser = IdempotencyReplayEraser(fake)
        assert await eraser.erase_case(CASE_A, ENTERPRISE) == 1
        monkeypatch.undo()

        (late,) = await fake.zrange(index, 0, -1)
        assert await fake.exists(late)
        assert await eraser.erase_case(CASE_A, ENTERPRISE) == 1
        assert not await fake.exists(index, late)


async def test_the_erase_unlinks_in_batches_and_counts_what_it_removed(monkeypatch):
    """A member can outlive its entry, so the count is ``UNLINK``'s, not the
    index's; a case with no index unlinks nothing."""
    monkeypatch.setattr(idempotency, "ERASE_BATCH", 2)
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    live = [f"idempotency:key-2624-{n}aaaaaaa:h" for n in range(3)]
    for key in live:
        await fake.set(key, "{}")
    index = case_index_key(KEY_PREFIX, CASE_A)
    expiry = time.time() + 3600
    await fake.zadd(index, {k: expiry for k in [*live, "idempotency:key-2624-gone:h"]})
    batches = _watch_unlinks(monkeypatch, fake)
    eraser = IdempotencyReplayEraser(fake)

    assert await eraser.erase_case(CASE_B, ENTERPRISE) == 0
    assert batches == []
    assert await eraser.erase_case(CASE_A, ENTERPRISE) == 3
    assert batches == [2, 2]
    assert await fake.keys("*") == []


class _Unreachable:
    async def zrange(self, *args, **kwargs):
        raise ConnectionError("redis unreachable")


async def test_a_redis_error_raises_so_the_delete_keeps_the_case():
    with pytest.raises(ConnectionError):
        await IdempotencyReplayEraser(_Unreachable()).erase_case(CASE_A, ENTERPRISE)
