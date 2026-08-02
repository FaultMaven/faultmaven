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

from faultmaven.api.middleware.idempotency import IdempotencyMiddleware

VICTIM = "Bearer victim-token-aaaaaaaaaaaaaaaa"
ATTACKER = "Bearer attacker-token-bbbbbbbbbbbb"
KEY = "11111111-2222-3333-4444-555555555555"


def _build_app():
    """FastAPI app wired exactly like production: auth is route-level.

    ``/api/v1/cases`` mirrors the real shape — the route, not the middleware,
    rejects an unauthenticated caller. That is what makes a pre-``call_next``
    cache hit an authentication bypass rather than merely a wrong body.
    """
    app = FastAPI()

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

    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    app.add_middleware(IdempotencyMiddleware, redis_client=fake)
    return app, fake


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _replayed(response) -> bool:
    return response.headers.get("X-Idempotency-Replayed") == "true"


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


async def test_same_caller_identified_by_session_id_replays():
    """Identity may come from X-Session-ID alone (mirrors deduplication)."""
    app, _ = _build_app()
    headers = {"X-Session-ID": "session-abc", "Idempotency-Key": KEY}

    async with _client(app) as client:
        first = await client.post("/api/v1/anon-ok", headers=headers, json={})
        second = await client.post("/api/v1/anon-ok", headers=headers, json={})

    assert not _replayed(first)
    assert _replayed(second)


# ---------------------------------------------------------------------------
# Guard 1: caller scoping.
# ---------------------------------------------------------------------------


async def test_other_authenticated_caller_cannot_replay_victims_response():
    app, _ = _build_app()

    async with _client(app) as client:
        victim = await client.post(
            "/api/v1/cases",
            headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
            json={"title": "victim case"},
        )
        attacker = await client.post(
            "/api/v1/cases",
            headers={"Authorization": ATTACKER, "Idempotency-Key": KEY},
            json={"title": "attacker case"},
        )

    assert victim.json()["owner"] == VICTIM
    assert not _replayed(attacker)
    assert attacker.json()["owner"] == ATTACKER
    assert attacker.json()["title"] == "attacker case"


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
    assert other.json()["owner"] != VICTIM or "X-Session-ID" in attacker_headers


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
        response = await client.post(
            "/api/v1/anon-ok", headers={"Idempotency-Key": KEY}, json={}
        )

    assert response.status_code == 200
    assert await fake.keys("idempotency:*") == []


async def test_two_anonymous_callers_never_share_a_bucket():
    app, _ = _build_app()

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/anon-ok", headers={"Idempotency-Key": KEY}, json={"who": "first"}
        )
        second = await client.post(
            "/api/v1/anon-ok", headers={"Idempotency-Key": KEY}, json={"who": "second"}
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
        first = await client.post("/api/v1/auth/oauth/token", headers=headers, json={})
        second = await client.post("/api/v1/auth/oauth/token", headers=headers, json={})

    assert first.status_code == 200
    assert not _replayed(first)
    assert not _replayed(second)
    assert await fake.keys("idempotency:*") == [], "token mints must never be cached"


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
