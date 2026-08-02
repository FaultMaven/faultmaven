"""Caller scoping for the idempotency middleware (fm#958).

Root cause guarded here: ``IdempotencyMiddleware._create_cache_key`` used to
build its Redis key from ``Idempotency-Key + method + path`` only. Nothing in
the key identified the *caller*, so any client that learned (or guessed) a key
was served the original caller's cached response body.

Two properties make that worse than a cache-poisoning nuisance:

* the cache lookup happens *before* ``call_next``, and FaultMaven enforces
  authentication with route-level ``Depends(...)`` rather than middleware — so
  on a cache hit authentication never runs at all, and a request with no
  ``Authorization`` header whatsoever is served an authenticated caller's body;
* a token-mint endpoint reached with a key would cache and replay a token pair.

These tests drive real requests through the real middleware with ``fakeredis``
on a single event loop. ``fastapi.testclient.TestClient`` is deliberately not
used: it creates a fresh event loop per request and async FakeRedis then raises
"bound to a different event loop", which the middleware's own ``except`` would
swallow into a pass.
"""

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import Body, FastAPI, HTTPException, Request, UploadFile

from faultmaven.api.middleware.idempotency import (
    MAX_FINGERPRINTED_BODY_BYTES,
    IdempotencyMiddleware,
)

VICTIM = "Bearer victim-token-aaaaaaaaaaaaaaaa"
ATTACKER = "Bearer attacker-token-bbbbbbbbbbbb"
KEY = "11111111-2222-3333-4444-555555555555"
OTHER_KEY = "99999999-8888-7777-6666-555555555555"


def _build_app(redis_client=None):
    """FastAPI app wired exactly like production: auth is route-level.

    ``/api/v1/cases`` mirrors the real shape — the route, not the middleware,
    rejects an unauthenticated caller. That is what makes a pre-``call_next``
    cache hit an authentication bypass rather than merely a wrong body.
    """
    app = FastAPI()
    route_calls: dict[str, int] = {"boom": 0}

    @app.post("/api/v1/boom")
    async def boom():
        route_calls["boom"] += 1
        raise RuntimeError("route exploded")

    @app.post("/api/v1/cases")
    async def create_case(request: Request, payload: dict = Body(default={})):
        authorization = request.headers.get("Authorization")
        if not authorization:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {"owner": authorization, "title": payload.get("title")}

    @app.post("/api/v1/anon-ok")
    async def anon_ok(request: Request):
        """A 2xx route that does not require auth, to observe cache writes."""
        return {"owner": request.headers.get("Authorization") or "anonymous"}

    @app.post("/api/v1/auth/oauth/token")
    async def mint_token(request: Request):
        return {"access_token": f"token-for-{request.headers.get('Authorization')}"}

    @app.post("/api/v1/echo")
    async def echo(payload: dict = Body(default={})):
        """Echoes the parsed body: proves the route still received it."""
        return {"received": payload}

    @app.post("/api/v1/upload")
    async def upload(file: UploadFile):
        content = await file.read()
        return {"filename": file.filename, "size": len(content), "sha": content.hex()}

    fake = redis_client or fakeredis_aio.FakeRedis(decode_responses=True)
    app.add_middleware(IdempotencyMiddleware, redis_client=fake)
    app.state.route_calls = route_calls
    return app, fake


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _replayed(response) -> bool:
    return response.headers.get("X-Idempotency-Replayed") == "true"


async def _seed_one_cache_entry(client, fake):
    """Positive control for the ``idempotency:*`` glob.

    ``assert await fake.keys("idempotency:*") == []`` proves nothing by itself:
    rename ``key_prefix`` and the glob matches nothing in every case, so the
    assertion holds vacuously and the test can no longer fail. Seed one entry
    from a known-good identified request and prove the glob *does* see it, so a
    later "unchanged" result is real evidence rather than a broken pattern.
    """
    await client.post(
        "/api/v1/anon-ok",
        headers={"Authorization": VICTIM, "Idempotency-Key": OTHER_KEY},
        json={"seed": True},
    )
    seeded = await fake.keys("idempotency:*")
    assert seeded, "positive control: an identified request must write a cache key"
    return sorted(seeded)


# ---------------------------------------------------------------------------
# The legitimate feature must survive: the copilot retries a failed create or
# turn with the SAME key and the SAME credential and expects the cached result.
# ---------------------------------------------------------------------------


async def test_same_caller_same_key_still_replays():
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        first = await client.post("/api/v1/cases", headers=headers, json={"title": "t"})
        second = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "t"}
        )

    assert first.status_code == 200
    assert not _replayed(first)
    assert second.status_code == 200
    assert _replayed(second), "same-caller retry must still be served from cache"
    assert second.json() == first.json()


async def test_session_id_alone_is_not_sufficient_identity():
    """X-Session-ID is client-chosen, so it cannot stand in for a credential.

    This deliberately replaces an earlier test that asserted the opposite.
    A caller who supplies only a session id could otherwise pre-seed a bucket
    that any later caller presenting the same guessable session id is served
    from. Session-only callers now do not participate at all: no replay, and
    nothing written.
    """
    app, fake = _build_app()
    headers = {"X-Session-ID": "session-abc", "Idempotency-Key": KEY}

    async with _client(app) as client:
        seeded = await _seed_one_cache_entry(client, fake)
        first = await client.post("/api/v1/anon-ok", headers=headers, json={})
        second = await client.post("/api/v1/anon-ok", headers=headers, json={})
        after = sorted(await fake.keys("idempotency:*"))

    assert not _replayed(first)
    assert not _replayed(second)
    assert after == seeded, "a session-only caller must not write to the cache"


async def test_two_sessions_under_one_token_get_separate_buckets():
    """X-Session-ID still narrows the bucket; it just cannot open one alone."""
    app, _ = _build_app()

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/anon-ok",
            headers={
                "Authorization": VICTIM,
                "X-Session-ID": "session-one",
                "Idempotency-Key": KEY,
            },
            json={},
        )
        other_session = await client.post(
            "/api/v1/anon-ok",
            headers={
                "Authorization": VICTIM,
                "X-Session-ID": "session-two",
                "Idempotency-Key": KEY,
            },
            json={},
        )
        same_session_retry = await client.post(
            "/api/v1/anon-ok",
            headers={
                "Authorization": VICTIM,
                "X-Session-ID": "session-one",
                "Idempotency-Key": KEY,
            },
            json={},
        )

    assert not _replayed(first)
    assert not _replayed(other_session), "a second session must not reuse the first"
    assert _replayed(same_session_retry), "the same session must still retry-replay"


# ---------------------------------------------------------------------------
# Guard 1: caller scoping.
# ---------------------------------------------------------------------------


async def test_other_authenticated_caller_cannot_replay_victims_response():
    """Identical request, different credential — only the caller distinguishes.

    The bodies are deliberately byte-identical so this isolates caller scoping:
    the body fingerprint cannot be what separates these two requests.
    """
    app, _ = _build_app()
    body = {"title": "victim case"}

    async with _client(app) as client:
        victim = await client.post(
            "/api/v1/cases",
            headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
            json=body,
        )
        attacker = await client.post(
            "/api/v1/cases",
            headers={"Authorization": ATTACKER, "Idempotency-Key": KEY},
            json=body,
        )

    assert victim.json()["owner"] == VICTIM
    assert not _replayed(attacker)
    assert attacker.json()["owner"] == ATTACKER


@pytest.mark.parametrize(
    "attacker_headers",
    [
        pytest.param({"Authorization": ATTACKER}, id="different-authorization"),
        pytest.param({"Authorization": VICTIM + "x"}, id="near-miss-authorization"),
        pytest.param({"X-Session-ID": "someone-else"}, id="session-id-instead"),
        pytest.param(
            {"Authorization": VICTIM, "X-Session-ID": "different-session"},
            id="same-authorization-different-session",
        ),
    ],
)
async def test_caller_identity_space_is_scoped(attacker_headers):
    """Sweep the identity space: no variation may land in the victim's bucket."""
    app, _ = _build_app()

    async with _client(app) as client:
        victim = await client.post(
            "/api/v1/anon-ok",
            headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
            json={},
        )
        other = await client.post(
            "/api/v1/anon-ok",
            headers={**attacker_headers, "Idempotency-Key": KEY},
            json={},
        )

    assert victim.json()["owner"] == VICTIM
    assert not _replayed(other)


# ---------------------------------------------------------------------------
# Guard 2: fail closed with no caller identity.
# ---------------------------------------------------------------------------


async def test_unauthenticated_caller_is_not_served_a_cached_response():
    """The headline defect: no Authorization header at all, victim's body back.

    On a cache hit the middleware returns before ``call_next``, so the route's
    ``Depends``-based auth never runs. Post-fix the anonymous request does not
    participate in idempotency and the route rejects it with 401.
    """
    app, _ = _build_app()

    async with _client(app) as client:
        victim = await client.post(
            "/api/v1/cases",
            headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
            json={"title": "victim case"},
        )
        anonymous = await client.post(
            "/api/v1/cases",
            headers={"Idempotency-Key": KEY},
            json={"title": "victim case"},
        )

    assert victim.status_code == 200
    assert anonymous.status_code == 401, "cache hit must not bypass route-level auth"
    assert not _replayed(anonymous)
    assert "owner" not in anonymous.text


async def test_anonymous_request_does_not_populate_the_cache():
    """Fail closed in both directions: no read *and* no write."""
    app, fake = _build_app()

    async with _client(app) as client:
        seeded = await _seed_one_cache_entry(client, fake)
        response = await client.post(
            "/api/v1/anon-ok", headers={"Idempotency-Key": KEY}, json={}
        )
        after = sorted(await fake.keys("idempotency:*"))

    assert response.status_code == 200
    assert after == seeded, "an anonymous request must not add a cache entry"


async def test_two_anonymous_callers_never_share_a_bucket():
    """Byte-identical anonymous requests must still not replay one another.

    Anonymous callers are indistinguishable, so "same bucket" would mean *every*
    anonymous caller sharing one. The bodies match so only the fail-closed guard
    can keep these apart.
    """
    app, _ = _build_app()
    body = {"who": "anyone"}

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/anon-ok", headers={"Idempotency-Key": KEY}, json=body
        )
        second = await client.post(
            "/api/v1/anon-ok", headers={"Idempotency-Key": KEY}, json=body
        )

    assert not _replayed(first)
    assert not _replayed(second)


# ---------------------------------------------------------------------------
# Guard 3: auth paths excluded outright.
# ---------------------------------------------------------------------------


async def test_auth_path_never_caches_even_for_the_same_caller():
    app, fake = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        seeded = await _seed_one_cache_entry(client, fake)
        first = await client.post("/api/v1/auth/oauth/token", headers=headers, json={})
        second = await client.post("/api/v1/auth/oauth/token", headers=headers, json={})
        after = sorted(await fake.keys("idempotency:*"))

    assert first.status_code == 200
    assert not _replayed(first)
    assert not _replayed(second)
    assert after == seeded, "token mints must never be cached"


async def test_auth_path_is_excluded_before_key_validation():
    """Exclusion is structural: an auth path is skipped, not merely re-keyed."""
    app, _ = _build_app()

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/auth/oauth/token",
            headers={"Authorization": VICTIM, "Idempotency-Key": "bad key!!"},
            json={},
        )

    assert response.status_code == 200, "auth paths skip idempotency entirely"


# ---------------------------------------------------------------------------
# Guard 4: body fingerprint (and the safety proof that buffering the body does
# not starve the downstream route).
# ---------------------------------------------------------------------------


async def test_same_key_different_body_does_not_replay():
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "first"}
        )
        second = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "second"}
        )

    assert first.json()["title"] == "first"
    assert not _replayed(second)
    assert second.json()["title"] == "second"


@pytest.mark.parametrize("with_key", [True, False], ids=["with-key", "without-key"])
async def test_downstream_route_still_reads_the_full_json_body(with_key):
    """Safety proof for body buffering under BaseHTTPMiddleware.

    ``call_next`` builds a new request from the same receive channel. Starlette
    replays a body that middleware has awaited, but a regression there would
    hand every POST route an empty or hanging body — so assert the route sees
    the payload byte for byte, both on the buffered path and the bypass path.
    """
    app, _ = _build_app()
    payload = {"title": "x" * 5000, "nested": {"items": list(range(200))}}
    headers = {"Authorization": VICTIM}
    if with_key:
        headers["Idempotency-Key"] = KEY

    async with _client(app) as client:
        response = await client.post("/api/v1/echo", headers=headers, json=payload)

    assert response.status_code == 200
    assert response.json()["received"] == payload


async def test_multipart_upload_body_reaches_the_route_intact():
    """Uploads are never buffered; prove the stream survives the middleware."""
    app, _ = _build_app()
    content = bytes(range(256)) * 64  # 16 KiB of non-JSON bytes

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/upload",
            headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
            files={"file": ("evidence.bin", content, "application/octet-stream")},
        )

    assert response.status_code == 200
    assert response.json()["size"] == len(content)
    assert response.json()["sha"] == content.hex()
    assert response.json()["filename"] == "evidence.bin"


# ---------------------------------------------------------------------------
# Correctness riders in the same middleware (pre-existing, fixed here).
# ---------------------------------------------------------------------------


async def test_failing_route_is_not_executed_twice():
    """The recovery path must never re-run a route that already ran.

    The blanket ``except`` around dispatch called ``call_next`` a second time,
    so a route that raised — or any downstream middleware that raised after the
    handler had side effects — executed twice for one client request. For a
    middleware whose entire purpose is at-most-once semantics that is the
    sharpest possible failure.
    """
    app, _ = _build_app()

    async with _client(app) as client:
        with pytest.raises(RuntimeError, match="route exploded"):
            await client.post(
                "/api/v1/boom",
                headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
                json={},
            )

    assert app.state.route_calls["boom"] == 1, "route must run at most once"


class _SetexFailsRedis:
    """A Redis stand-in that accepts reads and fails every write.

    Models a real Cloud condition: Redis reachable-then-not, or a write refused
    under memory pressure, in the window between draining the response body and
    restoring it.
    """

    def __init__(self, inner):
        self._inner = inner

    async def get(self, key):
        return await self._inner.get(key)

    async def keys(self, pattern):
        return await self._inner.keys(pattern)

    async def setex(self, *args, **kwargs):
        raise ConnectionError("redis unreachable")


async def test_client_still_receives_full_body_when_caching_fails():
    """A Redis write failure must not truncate the response.

    Caching drains the single-use body iterator. The restore used to sit after
    the ``setex`` call inside the same ``try``, so any Redis error skipped it
    and the client got HTTP 200 with the original Content-Length and zero
    bytes. Caching is best-effort; delivering the response is not.
    """
    inner = fakeredis_aio.FakeRedis(decode_responses=True)
    app, _ = _build_app(redis_client=_SetexFailsRedis(inner))
    payload = {"title": "y" * 4000}

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/cases",
            headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
            json=payload,
        )

    assert response.status_code == 200
    assert response.content, "response body must not be empty"
    assert response.json() == {"owner": VICTIM, "title": payload["title"]}
    assert len(response.content) == int(response.headers["content-length"])


async def test_non_json_body_is_not_buffered_but_still_replays_for_same_caller():
    """Skipping the fingerprint must not disable idempotency for that request."""
    app, _ = _build_app()
    headers = {
        "Authorization": VICTIM,
        "Idempotency-Key": KEY,
        "Content-Type": "text/plain",
    }

    async with _client(app) as client:
        first = await client.post("/api/v1/anon-ok", headers=headers, content=b"hello")
        second = await client.post("/api/v1/anon-ok", headers=headers, content=b"hello")

    assert not _replayed(first)
    assert _replayed(second)


# ---------------------------------------------------------------------------
# The content-type gate, and the sentinel behaviour it produces, are a DELIBERATE
# CONTRACT — not an accident. Requests that are not bounded JSON are never read,
# so they share one unfingerprinted bucket per caller+key+path and a different
# body under the same key DOES replay.
#
# This is the price of never touching an upload body in middleware, and the
# copilot's multipart turn-retry path depends on unfingerprinted requests still
# being cacheable. The tests below fail if the gate is ever widened to buffer
# these bodies, which is what makes them defend the gate rather than merely
# coexist with it.
# ---------------------------------------------------------------------------


async def test_different_text_plain_bodies_replay_by_design():
    """Removing the content-type gate would make this RED."""
    app, _ = _build_app()
    headers = {
        "Authorization": VICTIM,
        "Idempotency-Key": KEY,
        "Content-Type": "text/plain",
    }

    async with _client(app) as client:
        first = await client.post("/api/v1/anon-ok", headers=headers, content=b"alpha")
        second = await client.post("/api/v1/anon-ok", headers=headers, content=b"beta")

    assert not _replayed(first)
    assert _replayed(second), "non-JSON bodies are deliberately unfingerprinted"


async def test_different_multipart_uploads_replay_by_design():
    """Upload bodies are never read, so they cannot be told apart."""
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/upload",
            headers=headers,
            files={"file": ("a.bin", b"aaaaaaaa", "application/octet-stream")},
        )
        second = await client.post(
            "/api/v1/upload",
            headers=headers,
            files={"file": ("b.bin", b"bbbbbbbb", "application/octet-stream")},
        )

    assert first.json()["filename"] == "a.bin"
    assert _replayed(second)
    assert second.json()["filename"] == "a.bin", "replays the first upload by design"


async def test_oversized_json_bodies_replay_by_design():
    """Above MAX_FINGERPRINTED_BODY_BYTES the body is not buffered either."""
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}
    oversized = "x" * (MAX_FINGERPRINTED_BODY_BYTES + 1000)

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/cases", headers=headers, json={"title": oversized}
        )
        second = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "y" * len(oversized)}
        )

    assert first.json()["title"] == oversized
    assert _replayed(second)
    assert second.json()["title"] == oversized, "oversized bodies share one bucket"
