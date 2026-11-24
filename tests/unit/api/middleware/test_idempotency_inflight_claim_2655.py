"""The keyed-request claim (#2655): claim, then look up, then answer.

A duplicate overlapping the first request under one ``Idempotency-Key`` waits
for its answer instead of running again: it replays a 2xx, runs after a
non-2xx, and is answered 409 ``IDEMPOTENCY_REQUEST_IN_PROGRESS`` past the wait
or on a Redis error while waiting. Through the real middleware on one
FakeRedis, the overlaps ordered by events, never by time.
"""

import asyncio
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from faultmaven.api.middleware import idempotency
from faultmaven.api.middleware.idempotency import (
    CLAIM_PREFIX,
    CLAIM_TTL_SECONDS,
    IN_PROGRESS_DETAIL,
    KEY_PREFIX,
    IdempotencyMiddleware,
    case_index_key,
    declare_replay_case,
)
from faultmaven.config.idempotency_key import (
    IDEMPOTENCY_KEY_REUSE,
    IDEMPOTENCY_REQUEST_IN_PROGRESS,
)
from faultmaven.infrastructure.concurrency.owned_claim import ClaimHeld, OwnedClaim

pytestmark = [pytest.mark.unit]

KEY = "key-2655-aaaaaaaa"
CALLER = {"Authorization": "Bearer caller-2655-aaaaaaaa"}
CASE = "case_2655aaaaaaaa"
#: A hang guard only: every wait below ends on an event.
GUARD = 30


@pytest.fixture(autouse=True)
def _no_poll_pause(monkeypatch):
    monkeypatch.setattr(idempotency, "IDEMPOTENCY_POLL_SECONDS", 0)


@pytest.fixture
def rig(monkeypatch):
    """The app, its FakeRedis, and the route's script: ``holds`` pause the
    next runs until set, ``statuses`` answer them (201 when spent)."""
    app, fake = FastAPI(), fakeredis_aio.FakeRedis(decode_responses=True)
    rig = SimpleNamespace(
        app=app,
        fake=fake,
        runs=[],
        holds=[],
        statuses=[],
        entered=asyncio.Event(),
        waiting=asyncio.Event(),
    )

    @app.post("/api/v1/things")
    async def things(request: Request, payload: dict = Body(default={})):
        rig.runs.append(payload)
        run = len(rig.runs)
        if rig.holds:
            hold = rig.holds.pop(0)
            rig.entered.set()
            await hold.wait()
        status = rig.statuses.pop(0) if rig.statuses else 201
        if status == "raise":
            raise RuntimeError("route failed")
        declare_replay_case(request, CASE)
        return JSONResponse(status_code=status, content={"run": run})

    take_claim = idempotency.take_claim

    async def watched_take_claim(*args, **kwargs):
        try:
            return await take_claim(*args, **kwargs)
        except ClaimHeld:
            rig.held += 1
            rig.on_held()
            rig.waiting.set()
            raise

    monkeypatch.setattr(idempotency, "take_claim", watched_take_claim)

    def middleware(inner, *, redis_client):
        built = IdempotencyMiddleware(inner, redis_client=redis_client)
        if rig.no_redis:
            built.redis_client, built._resolved = None, True
        return built

    rig.no_redis, rig.held, rig.on_held = False, 0, lambda: None
    app.add_middleware(middleware, redis_client=fake)
    return rig


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


def _post(client, body=None, headers=CALLER):
    return client.post(
        "/api/v1/things", headers={**headers, "Idempotency-Key": KEY}, json=body or {}
    )


def _replayed(response) -> bool:
    return response.headers.get("X-Idempotency-Replayed") == "true"


async def _claims(fake):
    return await fake.keys(f"{CLAIM_PREFIX}*")


async def _overlap(rig, client, second_body=None):
    """The first request held in its route, the second waiting on its claim;
    then the first goes on. Returns both answers."""
    go = asyncio.Event()
    rig.holds.append(go)
    first = asyncio.ensure_future(_post(client))
    await asyncio.wait_for(rig.entered.wait(), GUARD)
    second = asyncio.ensure_future(_post(client, second_body))
    await asyncio.wait_for(rig.waiting.wait(), GUARD)
    go.set()
    return await asyncio.wait_for(asyncio.gather(first, second), GUARD)


async def test_an_overlapping_duplicate_replays_the_first_answer(rig):
    async with _client(rig.app) as client:
        first, second = await _overlap(rig, client)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() == {"run": 1}
    assert _replayed(second) and not _replayed(first)
    assert len(rig.runs) == 1
    assert await _claims(rig.fake) == []
    # One execution, so the key is indexed under one case, once.
    assert len(await rig.fake.zrange(case_index_key(KEY_PREFIX, CASE), 0, -1)) == 1


async def test_a_duplicate_runs_once_after_a_non_2xx_holder(rig):
    rig.statuses.extend([422, 201])
    async with _client(rig.app) as client:
        first, second = await _overlap(rig, client)

    assert first.status_code == 422
    assert second.status_code == 201 and not _replayed(second)
    assert second.json() == {"run": 2}


async def test_a_different_body_waiting_on_the_first_is_key_reuse(rig):
    async with _client(rig.app) as client:
        first, second = await _overlap(rig, client, {"other": "body"})

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.headers["x-error-code"] == IDEMPOTENCY_KEY_REUSE
    assert len(rig.runs) == 1


async def _refused_while_waiting(rig, client, monkeypatch, fail=None):
    """The second request, refused while it waits on the first."""
    go = asyncio.Event()
    rig.holds.append(go)
    first = asyncio.ensure_future(_post(client))
    await asyncio.wait_for(rig.entered.wait(), GUARD)
    with monkeypatch.context() as patch:
        if fail == "read":
            # The holder reads nothing more; the waiter's every poll fails.
            patch.setattr(rig.fake, "mget", _raise)
        if fail == "claim":
            # The holder answers 4xx (no entry) and releases; the waiter then
            # tries the claim, and the store fails.
            rig.statuses.append(422)
            rig.on_held = lambda: patch.setattr(rig.fake, "set", _raise)
        second = asyncio.ensure_future(_post(client))
        if fail == "claim":
            await asyncio.wait_for(rig.waiting.wait(), GUARD)
            go.set()
        refused = await asyncio.wait_for(second, GUARD)
    go.set()
    await asyncio.wait_for(first, GUARD)
    return refused


async def _raise(*args, **kwargs):
    raise ConnectionError("redis unreachable")


@pytest.mark.parametrize(
    "fail", [None, "read", "claim"], ids=["past-the-bound", "read-fails", "claim-fails"]
)
async def test_the_waiter_is_answered_in_progress_and_never_runs(
    rig, monkeypatch, fail
):
    if fail is None:
        monkeypatch.setattr(idempotency, "IDEMPOTENCY_WAIT_SECONDS", 0)
    async with _client(rig.app) as client:
        refused = await _refused_while_waiting(rig, client, monkeypatch, fail)

    assert refused.status_code == 409
    assert refused.headers["x-error-code"] == IDEMPOTENCY_REQUEST_IN_PROGRESS
    # A moment, a fifth of the wait: 2 s, or 1 s at a wait of 0.
    assert refused.headers["Retry-After"] == ("1" if fail is None else "2")
    assert (
        refused.json()["detail"]
        == IN_PROGRESS_DETAIL
        == (
            "This request is still being processed. Wait a moment, then reload: it "
            "may already have gone through."
        )
    )
    assert len(rig.runs) == 1
    assert rig.held == 1


async def test_a_miss_then_winning_the_released_claim_replays(rig, monkeypatch):
    """The critique's probe: the second request misses the entry, then the
    first runs, caches and releases before the second claims. The second's
    lookup under the claim finds the entry."""
    resume, looked_up, get = asyncio.Event(), asyncio.Event(), rig.fake.get

    async def first_lookup_pauses(name):
        found = await get(name)
        if not looked_up.is_set():
            looked_up.set()
            await resume.wait()
        return found

    monkeypatch.setattr(rig.fake, "get", first_lookup_pauses)
    async with _client(rig.app) as client:
        late = asyncio.ensure_future(_post(client))
        await asyncio.wait_for(looked_up.wait(), GUARD)
        first = await asyncio.wait_for(_post(client), GUARD)
        resume.set()
        late = await asyncio.wait_for(late, GUARD)

    assert first.status_code == late.status_code == 201
    assert _replayed(late) and late.json() == {"run": 1}
    assert len(rig.runs) == 1


@pytest.mark.parametrize("ending", ["read-fails", "cancelled"])
async def test_a_lookup_under_a_won_claim_that_fails_never_runs(
    rig, monkeypatch, ending
):
    """The second request misses, the first answers and releases, the second
    wins the claim and its lookup under it fails or is cancelled: nothing
    runs again, and the claim goes."""
    resume, looked_up, get = asyncio.Event(), asyncio.Event(), rig.fake.get
    lookups = []

    async def second_lookups(name):
        lookups.append(name)
        if len(lookups) == 1:  # the fast path's miss, then the first runs
            found = await get(name)
            looked_up.set()
            await resume.wait()
            return found
        if ending == "cancelled":
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(rig.fake, "get", second_lookups)
    async with _client(rig.app) as client:
        late = asyncio.ensure_future(_post(client))
        await asyncio.wait_for(looked_up.wait(), GUARD)
        # The first request answers 201 without reading (its own fast path
        # reads through the original client).
        monkeypatch.setattr(rig.fake, "get", get)
        first = await asyncio.wait_for(_post(client), GUARD)
        monkeypatch.setattr(rig.fake, "get", second_lookups)
        resume.set()
        if ending == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await late
        else:
            late = await asyncio.wait_for(late, GUARD)
            assert late.status_code == 409
            assert late.headers["x-error-code"] == IDEMPOTENCY_REQUEST_IN_PROGRESS

    assert first.status_code == 201 and len(rig.runs) == 1
    assert len(lookups) == 2
    assert await _claims(rig.fake) == []


@pytest.mark.parametrize("status", [201, 422, 500, "raise"])
async def test_the_claim_is_released_on_every_answer(rig, monkeypatch, status):
    """And a 2xx's entry and case index are written before the release."""
    rig.statuses.append(status)
    at_release = []
    release = OwnedClaim.release

    async def watched_release(claim):
        at_release.append(sorted(await rig.fake.keys(f"{KEY_PREFIX}*")))
        await release(claim)

    monkeypatch.setattr(OwnedClaim, "release", watched_release)
    async with _client(rig.app) as client:
        answer = await _post(client)

    assert answer.status_code == (500 if status == "raise" else status)
    assert await _claims(rig.fake) == []
    written = at_release[0]
    if status == 201:
        assert case_index_key(KEY_PREFIX, CASE) in written and len(written) == 2
    else:
        assert written == []


async def test_a_cancelled_holder_keeps_its_claim_until_it_lapses(rig, monkeypatch):
    """The residual "a holder dying between commit and cache": its duplicate
    is refused until the claim lapses, then runs again."""
    monkeypatch.setattr(idempotency, "IDEMPOTENCY_WAIT_SECONDS", 0)
    rig.holds.append(asyncio.Event())
    async with _client(rig.app) as client:
        holder = asyncio.ensure_future(_post(client))
        await asyncio.wait_for(rig.entered.wait(), GUARD)
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder

        (claim,) = await _claims(rig.fake)
        assert 0 < await rig.fake.ttl(claim) <= CLAIM_TTL_SECONDS
        refused = await _post(client)
        await rig.fake.delete(claim)  # its TTL runs out
        again = await _post(client)

    assert refused.headers["x-error-code"] == IDEMPOTENCY_REQUEST_IN_PROGRESS
    assert again.status_code == 201 and again.json() == {"run": 2}


@pytest.mark.parametrize("residual", ["cache-write-fails", "commits-then-5xx"])
async def test_the_documented_residuals_run_again(rig, monkeypatch, residual):
    """Once per 2xx answer, not once per key: with no entry written, the next
    request under the key runs again."""
    if residual == "cache-write-fails":
        monkeypatch.setattr(rig.fake, "pipeline", _refuse_pipeline)
    else:
        rig.statuses.append(500)
    async with _client(rig.app) as client:
        first, retry = await _post(client), await _post(client)

    assert not _replayed(retry) and len(rig.runs) == 2
    assert first.status_code == (201 if residual == "cache-write-fails" else 500)


def _refuse_pipeline(*args, **kwargs):
    raise ConnectionError("redis unreachable")


@pytest.mark.parametrize(
    "store", ["claim-store-fails", "redis-down", "no-redis-client"]
)
async def test_without_a_claim_store_the_route_runs_unclaimed(rig, monkeypatch, store):
    """Redis down entirely included: no read, claim or cache write works, and
    the request still runs, as before the claim."""
    if store == "no-redis-client":
        rig.no_redis = True
    else:
        monkeypatch.setattr(rig.fake, "set", _raise)
    if store == "redis-down":
        for command in ("get", "mget"):
            monkeypatch.setattr(rig.fake, command, _raise)
        monkeypatch.setattr(rig.fake, "pipeline", _refuse_pipeline)
    async with _client(rig.app) as client:
        answer = await _post(client)

    assert answer.status_code == 201 and len(rig.runs) == 1
    assert await _claims(rig.fake) == []


async def test_a_request_without_an_authorization_header_takes_no_claim(rig):
    async with _client(rig.app) as client:
        first, second = await _post(client, headers={}), await _post(client, headers={})

    assert first.status_code == second.status_code == 201
    assert len(rig.runs) == 2 and await _claims(rig.fake) == []
