"""The replay cache's per-case index and the case delete's eraser (#2624),
through the real middleware on one FakeRedis, on a single event loop."""

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import Body, FastAPI

from faultmaven.api.middleware import idempotency
from faultmaven.api.middleware.idempotency import (
    KEY_PREFIX,
    IdempotencyMiddleware,
    IdempotencyReplayEraser,
    case_index_key,
)
from faultmaven.modules.case.contracts import CASE_ID_PATTERN, Case

pytestmark = [pytest.mark.unit]

CASE_A, CASE_B = "case_0123456789ab", "case_ba9876543210"
KEY_A, KEY_B = "key-2624-aaaaaaaa", "key-2624-bbbbbbbb"
CALLER = "Bearer caller-2624-aaaaaaaa"
ENTERPRISE = "ent_2624"


def _app(prefix: str = KEY_PREFIX):
    app, fake, runs = FastAPI(), fakeredis_aio.FakeRedis(decode_responses=True), []

    @app.post("/api/v1/cases")
    async def create(payload: dict = Body(default={})):
        runs.append(payload)
        return {"case_id": payload["case_id"], "run": len(runs)}

    @app.post("/api/v1/cases/{case_id}/title")
    async def title(case_id: str):
        return {"title": "Disk full"}

    @app.post("/api/v1/reports/generate")
    async def generate(case_id: str = "", q: str = ""):
        return {"report": "ok"}

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


def test_a_built_case_id_matches_the_grammar():
    case_id = Case(user_id="u_2624", enterprise_id=ENTERPRISE, title="t").case_id
    assert CASE_ID_PATTERN.fullmatch(case_id)
    assert not CASE_ID_PATTERN.search(f"case_{case_id}")  # Chroma's collection name


@pytest.mark.parametrize(
    "path, body",
    [
        (f"/api/v1/cases/{CASE_A}/title", None),
        (f"/api/v1/reports/generate?case_id={CASE_A}", None),
        # Decoded first: in the raw query ``%3D`` hides the id from ``\b``.
        (f"/api/v1/reports/generate?q=case_id%3D{CASE_A}", None),
        ("/api/v1/cases", {"case_id": CASE_A}),
    ],
    ids=["path", "query", "encoded-query", "answer"],
)
async def test_the_index_records_the_case_an_exchange_names(path, body):
    app, fake = _app()
    async with _client(app) as client:
        assert (await _post(client, path, KEY_A, body)).status_code == 200

    index = case_index_key(KEY_PREFIX, CASE_A)
    (entry,) = [key for key in await fake.keys("idempotency:*") if key != index]
    assert await fake.smembers(index) == {entry}
    assert 0 < await fake.ttl(index) <= 3600


async def test_the_eraser_clears_the_case_and_leaves_another_cases_replay():
    """Under a namespaced prefix, which the eraser takes as the middleware's."""
    prefix = "ns-2624:idempotency:"
    app, fake = _app(prefix)
    async with _client(app) as client:
        await _post(client, "/api/v1/cases", KEY_A, {"case_id": CASE_A})
        await _post(client, "/api/v1/cases", KEY_B, {"case_id": CASE_B})

        eraser = IdempotencyReplayEraser(fake, key_prefix=prefix)
        assert await eraser.erase_case(CASE_A, ENTERPRISE) == 1
        assert not await fake.exists(case_index_key(prefix, CASE_A))

        again_a = await _post(client, "/api/v1/cases", KEY_A, {"case_id": CASE_A})
        again_b = await _post(client, "/api/v1/cases", KEY_B, {"case_id": CASE_B})

    assert not _replayed(again_a) and again_a.json()["run"] == 3
    assert _replayed(again_b) and again_b.json()["run"] == 2


async def test_the_erase_deletes_in_batches_and_counts_what_it_deleted(monkeypatch):
    """A member can outlive its entry, so the count is ``DEL``'s, not the
    set's; a case with no index sends no ``DEL``, which Redis refuses."""
    monkeypatch.setattr(idempotency, "ERASE_BATCH", 2)
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    live = [f"idempotency:key-2624-{n}aaaaaaa:h" for n in range(3)]
    for key in live:
        await fake.set(key, "{}")
    index = case_index_key(KEY_PREFIX, CASE_A)
    await fake.sadd(index, *live, "idempotency:key-2624-expired:h")
    calls, delete = [], fake.delete

    async def counted(*keys):
        calls.append(len(keys))
        return await delete(*keys)

    monkeypatch.setattr(fake, "delete", counted)
    eraser = IdempotencyReplayEraser(fake)

    assert await eraser.erase_case(CASE_B, ENTERPRISE) == 0
    assert calls == []
    assert await eraser.erase_case(CASE_A, ENTERPRISE) == 3
    assert calls == [2, 2, 1]  # the four members, then the index
    assert await fake.keys("*") == []


class _Unreachable:
    async def smembers(self, key):
        raise ConnectionError("redis unreachable")


async def test_a_redis_error_raises_so_the_delete_keeps_the_case():
    with pytest.raises(ConnectionError):
        await IdempotencyReplayEraser(_Unreachable()).erase_case(CASE_A, ENTERPRISE)
