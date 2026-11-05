"""A request whose answer changed nothing does not block its own retry (#2508).

``DeduplicationMiddleware`` claims a request's key when it arrives, so an
identical request arriving while it runs is refused. The claim used to stand
for its whole TTL whatever the first request was answered. So a ``DELETE``
answered 401, resent after a credential refresh, and a delete the case-delete
gate refused (409 ``CASE_WORK_IN_PROGRESS``, 503 ``CASE_DELETE_UNAVAILABLE``,
both inviting a retry with ``Retry-After``) were answered 409
``DUPLICATE_REQUEST`` on the retry, which never reached the route.

Now the answer decides: a 2xx keeps the claim for its TTL, anything else or a
route exception releases it. The real app's 401 resend, the delete gate's
refusals, the route exception and the failing release fail on the old
middleware; a success, two requests in flight and a late release pin what must
not change.

The scratch harness is the one ``test_route_policy_templated_tier_1888.py``
uses: the real middleware and a FakeRedis on one event loop
(``httpx.ASGITransport``). It runs fail-closed, so a store the middleware could
not use answers 503 rather than letting a retry through. Each test that shows
a retry reaching the route also shows, in the same test, a refusal.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, Response

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.protection import live_protection_middleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.utils.serialization import to_json_compatible

pytestmark = [pytest.mark.unit, pytest.mark.security]

PATH = "/api/v1/cases/case-2508"
SESSION = "sess-2508"


def _answer(status: int, code: str = "") -> Response:
    headers = {"x-error-code": code, "Retry-After": "10"} if code else {}
    return Response(status_code=status, headers=headers)


class _Dedup:
    """A scripted ``DELETE`` behind the real middleware, over a FakeRedis.

    Each call to the route takes the next scripted answer: a ``Response`` is
    returned, an exception raised. While ``gate`` is set to an event, the route
    holds every request inside it until the event fires.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.entries = 0
        self.entered = asyncio.Event()
        self.gate = None
        self.redis = fakeredis_aio.FakeRedis(decode_responses=True)

        settings = get_development_protection_settings()
        settings.deduplication_enabled = True
        settings.fail_open_on_redis_error = False
        self.app = FastAPI()
        self.app.state.redis_client = self.redis
        self.app.add_middleware(DeduplicationMiddleware, settings=settings)

        @self.app.delete("/api/v1/cases/{case_id}")
        async def _delete(case_id: str):
            self.entries += 1
            self.entered.set()
            if self.gate is not None:
                await self.gate.wait()
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    @property
    def middleware(self) -> DeduplicationMiddleware:
        node = self.app.middleware_stack
        while not isinstance(node, DeduplicationMiddleware):
            node = node.app
        return node

    async def delete(self, session: str = SESSION) -> httpx.Response:
        return await self.client.delete(PATH, headers={"X-Session-ID": session})

    async def assert_refused_as_duplicate(self) -> None:
        refused = await self.delete()
        assert refused.status_code == 409, refused.text
        assert refused.headers["x-error-code"] == "DUPLICATE_REQUEST"

    async def held_in_route(self) -> asyncio.Task:
        """Send a request and return once the route holds it, with no clock."""
        self.gate = asyncio.Event()
        sent = asyncio.create_task(self.delete())
        entered = asyncio.create_task(self.entered.wait())
        done, _ = await asyncio.wait(
            {sent, entered}, return_when=asyncio.FIRST_COMPLETED
        )
        assert entered in done, "the request never reached the route"
        return sent


def _is_duplicate_refusal(response) -> bool:
    return (
        response.status_code == 409
        and response.headers.get("x-error-code") == "DUPLICATE_REQUEST"
    )


def test_the_real_app_lets_a_resend_after_a_401_reach_the_route(booted_app_client):
    """Done-when 1, on the app a deployment serves, under its hardened preset.

    The held client is reset first, as ``shutdown`` does: ``_initialize`` keeps
    a client it already holds before it reads ``app.state``, so it would
    re-adopt one bound to an earlier loop and, failing open, pass both
    requests whatever the rule. ``metrics["errors"]`` shows it did not.
    """
    from faultmaven.main import app

    client = booted_app_client
    [middleware] = [
        node
        for node in live_protection_middleware(app)
        if isinstance(node, DeduplicationMiddleware)
    ]
    app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
    middleware._redis = middleware._injected_redis
    middleware._initialized = False
    try:
        errors_before = middleware.metrics["errors"]
        session = {"X-Session-ID": f"sess-2508-{uuid.uuid4().hex}"}

        login = client.post("/api/v1/auth/dev-login", json={"username": "admin"})
        assert login.status_code == 200, login.text
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post("/api/v1/cases", json={"title": "2508"}, headers=auth)
        assert created.status_code == 201, created.text
        case_path = f"/api/v1/cases/{created.json()['case_id']}"

        # Positive control: dedup is live here. A 2xx keeps the claim.
        edit = {"title": f"renamed {uuid.uuid4().hex}"}
        edited = client.put(case_path, json=edit, headers={**auth, **session})
        assert edited.status_code == 200, edited.text
        repeat = client.put(case_path, json=edit, headers={**auth, **session})
        assert _is_duplicate_refusal(repeat), repeat.text

        # A resend after a credential refresh differs only in Authorization,
        # which the key does not hash: to this middleware it is identical.
        refused = client.delete(case_path, headers=session)
        assert refused.status_code == 401, refused.text
        resent = client.delete(case_path, headers=session)
        assert resent.status_code == 401, resent.text

        assert middleware.metrics["errors"] == errors_before
    finally:
        middleware._redis = middleware._injected_redis
        middleware._initialized = False


@pytest.mark.parametrize(
    "code, status", [("CASE_WORK_IN_PROGRESS", 409), ("CASE_DELETE_UNAVAILABLE", 503)]
)
async def test_a_retry_the_delete_gate_invites_reaches_the_route(code, status):
    """Done-when 2: the gate's refusals cleared nothing, and say so."""
    dedup = _Dedup(_answer(status, code), _answer(204))
    async with dedup.client:
        refused = await dedup.delete()
        assert refused.status_code == status
        assert refused.headers["x-error-code"] == code

        assert (await dedup.delete()).status_code == 204
        assert dedup.entries == 2
        # The 204 stands, so dedup was live all along.
        await dedup.assert_refused_as_duplicate()
    assert dedup.middleware.metrics["errors"] == 0


async def test_a_route_exception_releases_the_claim():
    """The exception still reaches the app's error handling, unchanged."""
    dedup = _Dedup(RuntimeError("the route broke"), _answer(204))
    async with dedup.client:
        with pytest.raises(RuntimeError, match="the route broke"):
            await dedup.delete()

        assert (await dedup.delete()).status_code == 204
        assert dedup.entries == 2
        await dedup.assert_refused_as_duplicate()
    assert dedup.middleware.metrics["errors"] == 0


async def test_a_success_keeps_the_claim():
    dedup = _Dedup(_answer(204), _answer(204))
    async with dedup.client:
        assert (await dedup.delete()).status_code == 204
        await dedup.assert_refused_as_duplicate()
    assert dedup.entries == 1


async def test_two_identical_requests_in_flight_run_once():
    dedup = _Dedup(_answer(204), _answer(204))
    async with dedup.client:
        first = await dedup.held_in_route()
        await dedup.assert_refused_as_duplicate()
        dedup.gate.set()
        assert (await first).status_code == 204
    assert dedup.entries == 1


async def test_a_late_release_never_deletes_a_newer_claim():
    """A outlives its claim, another request claims the key, then A answers 401."""
    dedup = _Dedup(_answer(401))
    async with dedup.client:
        first = await dedup.held_in_route()

        [key] = await dedup.redis.keys("*")
        newer = f"{to_json_compatible(datetime.now(timezone.utc))}|another-request"
        await dedup.redis.set(key, newer, ex=30)
        dedup.gate.set()
        assert (await first).status_code == 401

        assert await dedup.redis.get(key) == newer
        await dedup.assert_refused_as_duplicate()
    assert dedup.entries == 1


async def test_a_failing_release_changes_no_answer(monkeypatch):
    """Best-effort: counted, swallowed, never the answer and never the error."""

    async def _store_down(self, key, value):
        raise ConnectionError("the store is down")

    monkeypatch.setattr(DeduplicationMiddleware, "_release_claim", _store_down)
    dedup = _Dedup(_answer(401), RuntimeError("the route broke"))
    async with dedup.client:
        assert (await dedup.delete()).status_code == 401
        assert dedup.middleware.metrics["errors"] == 1
        # Unreleased, the claim lapses at its TTL, as every claim used to.
        await dedup.assert_refused_as_duplicate()

        with pytest.raises(RuntimeError, match="the route broke"):
            await dedup.delete(session="another-session")
    assert dedup.middleware.metrics["errors"] == 2


async def test_the_retry_after_reads_the_timestamp_the_claim_stores():
    """The parser, on the exact value a claim stores.

    An unreadable value falls back to "now", which a ``Retry-After`` taken a
    moment later cannot tell from the real timestamp, so the value is read here.
    """
    from faultmaven.api.middleware.deduplication import _claimed_at

    dedup = _Dedup(_answer(204))
    async with dedup.client:
        before = datetime.now(timezone.utc)
        assert (await dedup.delete()).status_code == 204
        after = datetime.now(timezone.utc)
        [key] = await dedup.redis.keys("*")
        stored = await dedup.redis.get(key)

    claimed = _claimed_at(stored)
    assert claimed is not None, stored
    assert before <= claimed <= after
