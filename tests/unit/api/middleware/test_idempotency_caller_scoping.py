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
from fastapi.responses import JSONResponse

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
    route_calls: dict[str, int] = {
        "boom": 0,
        "cases": 0,
        "case_from_session": 0,
        "reject": 0,
        "server_error": 0,
    }

    @app.post("/api/v1/reject")
    async def reject():
        route_calls["reject"] += 1
        raise HTTPException(status_code=400, detail="nope")

    @app.post("/api/v1/server-error")
    async def server_error():
        """Returns 5xx rather than raising, to exercise the status gate.

        Raising would take the ``downstream_invoked`` re-raise path instead,
        which is a different guard.
        """
        route_calls["server_error"] += 1
        return JSONResponse(status_code=500, content={"detail": "boom"})

    @app.post("/api/v1/boom")
    async def boom():
        route_calls["boom"] += 1
        raise RuntimeError("route exploded")

    @app.post("/api/v1/cases/sessions/{session_id}/case")
    async def case_from_session(session_id: str, force_new: bool = False):
        """Mirrors the real route whose behaviour is selected by the query."""
        route_calls["case_from_session"] += 1
        return {"session_id": session_id, "force_new": force_new}

    @app.post("/api/v1/sessions")
    async def mint_session():
        """Mirrors the real session-mint route: the body IS a credential."""
        return {"session_id": "newly-minted-credential"}

    @app.post("/api/v1/sessions/")
    async def mint_session_trailing_slash():
        """Registered so the trailing-slash request reaches a 2xx route.

        Without it FastAPI answers ``/api/v1/sessions/`` with a 307, which is
        not cacheable — and a test asserting "nothing was cached" would then
        pass whether or not the exclusion normalises the trailing slash. In the
        real app ``TrailingSlashMiddleware`` plays this role, and it sits
        *inside* this middleware, so idempotency still sees the raw path.
        """
        return {"session_id": "newly-minted-credential"}

    async def _create_case(request: Request, payload: dict):
        authorization = request.headers.get("Authorization")
        if not authorization:
            raise HTTPException(status_code=401, detail="Not authenticated")
        route_calls["cases"] += 1
        return {"owner": authorization, "title": payload.get("title")}

    @app.post("/api/v1/cases")
    async def create_case(request: Request, payload: dict = Body(default={})):
        return await _create_case(request, payload)

    @app.post("/api/v1/cases/")
    async def create_case_trailing_slash(
        request: Request, payload: dict = Body(default={})
    ):
        """Both spellings reach the same handler and the same call counter.

        Registered for the same reason as the trailing-slash session route:
        without it FastAPI answers 307, which is not cacheable, and a test
        asserting "the retry replayed" could not tell a working normalisation
        from a redirect. In the real app ``TrailingSlashMiddleware`` plays this
        role, and it sits *inside* this middleware.
        """
        return await _create_case(request, payload)

    @app.post("/api/v1/anon-okay")
    async def anon_okay(request: Request):
        """A near-miss path: differs from /anon-ok by more than a slash."""
        return {"owner": request.headers.get("Authorization") or "anonymous"}

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

    assert first.status_code == 200, "must reach a cacheable 2xx, not a redirect/error"
    assert second.status_code == 200
    assert not _replayed(first)
    assert not _replayed(second)
    assert after == seeded, "a session-only caller must not write to the cache"


async def test_two_sessions_under_one_token_share_a_bucket():
    """X-Session-ID must NOT contribute to identity, only Authorization.

    The copilot sends both headers, and on a 401 its retry path calls
    ``refreshSession()``, which persists a *new* session id before re-sending.
    If the session id were part of the identity, that retry would land in a
    different bucket and execute a second time — the exact duplicate this
    middleware exists to prevent, on the path where it matters most. One
    authenticated principal replaying its own response across two sessions is
    correct, not a leak.
    """
    app, _ = _build_app()

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/anon-ok",
            headers={
                "Authorization": VICTIM,
                "X-Session-ID": "session-before-refresh",
                "Idempotency-Key": KEY,
            },
            json={},
        )
        after_refresh = await client.post(
            "/api/v1/anon-ok",
            headers={
                "Authorization": VICTIM,
                "X-Session-ID": "session-after-refresh",
                "Idempotency-Key": KEY,
            },
            json={},
        )

    assert not _replayed(first)
    assert _replayed(after_refresh), "a rotated session id must not fork the bucket"


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
        pytest.param({}, id="no-headers-at-all"),
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

    assert victim.status_code == 200
    assert other.status_code == 200, "must reach the route, not fail some other way"
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

    assert first.status_code == 200, "must reach a cacheable 2xx, not a redirect/error"
    assert second.status_code == 200
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


async def test_same_key_different_body_is_refused_with_409():
    """A reused key with a different body must be refused, not forked.

    Folding the body into the cache key would make this a cache *miss*: the
    request would silently execute a second time and create a second resource.
    Replaying the first response instead would answer with the wrong data.
    Neither is right — the only correct answer is to refuse.
    """
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "first"}
        )
        conflict = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "second"}
        )

    assert first.json()["title"] == "first"
    assert conflict.status_code == 409
    assert conflict.json()["error_type"] == "IdempotencyKeyReuse"
    assert not _replayed(conflict)
    assert app.state.route_calls["cases"] == 1, "the conflicting request must not run"


async def test_conflicting_request_does_not_overwrite_the_stored_response():
    """A refused request must leave the original entry intact and replayable."""
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        await client.post("/api/v1/cases", headers=headers, json={"title": "first"})
        conflict = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "second"}
        )
        honest_retry = await client.post(
            "/api/v1/cases", headers=headers, json={"title": "first"}
        )

    assert conflict.status_code == 409
    assert _replayed(honest_retry)
    assert honest_retry.json()["title"] == "first"
    assert app.state.route_calls["cases"] == 1


async def test_mixed_fingerprint_is_refused():
    """Exactly one side fingerprinted: equality cannot be shown, so refuse."""
    app, _ = _build_app()

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/cases",
            headers={"Authorization": VICTIM, "Idempotency-Key": KEY},
            json={"title": "json body"},
        )
        # Same caller, key, method and path — but an unfingerprintable body.
        conflict = await client.post(
            "/api/v1/cases",
            headers={
                "Authorization": VICTIM,
                "Idempotency-Key": KEY,
                "Content-Type": "text/plain",
            },
            content=b"not json",
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert app.state.route_calls["cases"] == 1


async def test_byte_different_but_equivalent_json_is_still_refused():
    """Fingerprinting is byte-level, and the refusal is what makes that safe.

    Two semantically identical payloads that differ only in key order are
    *different* to the fingerprint. Refusing is the conservative answer; the
    alternative under a body-in-key scheme was to execute twice.
    """
    app, _ = _build_app()
    headers = {
        "Authorization": VICTIM,
        "Idempotency-Key": KEY,
        "Content-Type": "application/json",
    }

    async with _client(app) as client:
        await client.post("/api/v1/cases", headers=headers, content=b'{"a": 1, "b": 2}')
        conflict = await client.post(
            "/api/v1/cases", headers=headers, content=b'{"b": 2, "a": 1}'
        )

    assert conflict.status_code == 409
    assert app.state.route_calls["cases"] == 1


# ---------------------------------------------------------------------------
# The query string selects behaviour, so it must scope the cache key.
# ---------------------------------------------------------------------------


async def test_query_string_splits_the_bucket():
    """?force_new=true and ?force_new=false are different operations."""
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        forced = await client.post(
            "/api/v1/cases/sessions/s-1/case?force_new=true", headers=headers, json={}
        )
        not_forced = await client.post(
            "/api/v1/cases/sessions/s-1/case?force_new=false", headers=headers, json={}
        )

    assert forced.json()["force_new"] is True
    assert not _replayed(not_forced), "a different query must not replay"
    assert not_forced.json()["force_new"] is False
    assert app.state.route_calls["case_from_session"] == 2


@pytest.mark.parametrize(
    ("path", "counter", "status"),
    [
        pytest.param("/api/v1/reject", "reject", 400, id="4xx"),
        pytest.param("/api/v1/server-error", "server_error", 500, id="5xx"),
    ],
)
async def test_non_2xx_responses_are_not_cached(path, counter, status):
    """Only successful responses are idempotent.

    Nothing pinned this: widening ``if 200 <= response.status_code < 300`` to
    ``if True`` left the whole suite green. Caching a failure would pin a
    transient error to the key for the full hour, so the caller's honest retry
    would be served the failure it was retrying.
    """
    app, fake = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        seeded = await _seed_one_cache_entry(client, fake)
        first = await client.post(path, headers=headers, json={})
        second = await client.post(path, headers=headers, json={})
        after = sorted(await fake.keys("idempotency:*"))

    assert first.status_code == status
    assert second.status_code == status
    assert not _replayed(second)
    assert after == seeded, "a non-2xx response must not be cached"
    assert app.state.route_calls[counter] == 2, "the retry must re-execute"


async def test_path_splits_the_bucket():
    """The other half of "scoped to route" — the path itself.

    ``_create_cache_key`` claims scoping to caller *and route*, but only the
    query half of "route" was pinned: dropping ``request.url.path`` from the
    hashed material left the whole suite green. Two different paths under one
    caller and key must not share a bucket.
    """
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        cases = await client.post("/api/v1/cases", headers=headers, json={})
        anon_ok = await client.post("/api/v1/anon-ok", headers=headers, json={})

    assert cases.status_code == 200
    assert anon_ok.status_code == 200
    assert not _replayed(anon_ok), "a different path must not replay"
    assert "title" not in anon_ok.json(), "must be the anon-ok route's own response"


async def test_trailing_slash_variants_share_a_bucket():
    """A retry that varies only by the trailing slash must not execute twice.

    Both spellings reach the same route — this middleware sits outside
    ``TrailingSlashMiddleware``, so it sees the raw path while the route sees
    the normalised one. Keying them apart would let the retry create a second
    resource, which is the whole failure this middleware prevents.
    """
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}
    body = {"title": "one case"}

    async with _client(app) as client:
        first = await client.post("/api/v1/cases", headers=headers, json=body)
        with_slash = await client.post("/api/v1/cases/", headers=headers, json=body)

    assert first.status_code == 200
    # Defence in depth, not the load-bearing assertion. Unlike the exclusion
    # test — which asserts an *absence* ("nothing was written") that a 307
    # satisfies — this asserts a *presence*: a 307 cannot carry
    # X-Idempotency-Replayed, so `_replayed` already fails if the request never
    # reaches a cacheable route. Verified by probe: with the fixture route
    # removed and this line deleted, a broken guard is still RED.
    assert with_slash.status_code == 200, "must reach a cacheable 2xx, not a 307"
    assert _replayed(with_slash), "the slash variant must replay, not re-execute"
    assert with_slash.json() == first.json()
    assert app.state.route_calls["cases"] == 1, "the route must run exactly once"


async def test_paths_differing_by_more_than_a_slash_still_split():
    """Normalising the slash must not merge genuinely different paths."""
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        anon_ok = await client.post("/api/v1/anon-ok", headers=headers, json={})
        anon_okay = await client.post("/api/v1/anon-okay", headers=headers, json={})

    assert anon_ok.status_code == 200
    assert anon_okay.status_code == 200
    assert not _replayed(anon_okay), "a near-miss path must still split the bucket"


async def test_same_query_still_replays():
    """Splitting on the query must not break retries of the same request."""
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}
    url = "/api/v1/cases/sessions/s-1/case?force_new=true"

    async with _client(app) as client:
        first = await client.post(url, headers=headers, json={})
        retry = await client.post(url, headers=headers, json={})

    assert not _replayed(first)
    assert _replayed(retry)
    assert app.state.route_calls["case_from_session"] == 1


# ---------------------------------------------------------------------------
# The session-mint route mints a credential and is excluded by exact path.
# ---------------------------------------------------------------------------


async def test_session_mint_route_is_excluded():
    """POST /api/v1/sessions returns a session id, which IS a credential.

    ``api/v1/dependencies.py`` resolves a user from ``X-Session-Id`` alone, so
    a replayed mint response hands out a working credential — the same class the
    ``/auth/`` exclusion prevents structurally.
    """
    app, fake = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        seeded = await _seed_one_cache_entry(client, fake)
        first = await client.post("/api/v1/sessions", headers=headers, json={})
        second = await client.post("/api/v1/sessions", headers=headers, json={})
        after = sorted(await fake.keys("idempotency:*"))

    assert first.status_code == 200
    assert not _replayed(second)
    assert after == seeded, "a session mint must never be cached"


async def test_session_mint_exclusion_survives_a_trailing_slash():
    """This middleware sits outside TrailingSlashMiddleware, so it sees '/'."""
    app, fake = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        seeded = await _seed_one_cache_entry(client, fake)
        minted = await client.post("/api/v1/sessions/", headers=headers, json={})
        after = sorted(await fake.keys("idempotency:*"))

    # Pin the precondition this test depends on. Without a route registered at
    # the trailing-slash path FastAPI answers 307, which is not cacheable — and
    # then "nothing was written" holds whether or not the exclusion normalises
    # the slash. Deleting the fixture route would otherwise silently restore
    # that vacuity with no signal.
    assert minted.status_code == 200, "must reach a cacheable 2xx, not a 307"
    assert after == seeded, "a trailing slash must not slip past the exclusion"


async def test_case_session_routes_are_not_excluded():
    """The exclusion must be exact: a '/sessions' substring over-excludes.

    ``/api/v1/cases/sessions/{sid}/case`` contains '/sessions' but is an
    ordinary idempotent route. A substring marker would silently disable
    idempotency on it — including on the query-scoping behaviour above.
    """
    app, _ = _build_app()
    headers = {"Authorization": VICTIM, "Idempotency-Key": KEY}

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/cases/sessions/s-1/case", headers=headers, json={}
        )
        retry = await client.post(
            "/api/v1/cases/sessions/s-1/case", headers=headers, json={}
        )

    assert not _replayed(first)
    assert _replayed(retry), "case-session routes must keep idempotency"
    assert app.state.route_calls["case_from_session"] == 1


def test_exclusion_predicate_normalises_the_trailing_slash():
    """Pin the normalisation on the predicate itself.

    The end-to-end version of this needs a route registered at the trailing-slash
    path, or FastAPI answers 307 and "nothing was cached" holds vacuously. This
    asserts the property directly, with the negative case alongside so the
    normalisation cannot be widened into a prefix match.
    """
    middleware = IdempotencyMiddleware(app=lambda scope, receive, send: None)

    assert middleware._is_excluded_path("/api/v1/sessions")
    assert middleware._is_excluded_path("/api/v1/sessions/")
    assert not middleware._is_excluded_path("/api/v1/sessions/search")
    assert not middleware._is_excluded_path("/api/v1/cases/sessions/s-1/case")


def test_exclusions_do_not_over_catch_any_real_post_route():
    """Enumerate the real app and prove the exclusion set is exactly intended.

    An exclusion is a silent disabling of idempotency, so it must be shown
    against the real route table rather than against hand-written paths. The
    tempting ``/sessions`` substring marker catches fifteen POST routes here.

    Enumerated from the published schema rather than by walking ``app.routes``
    and filtering on ``isinstance(route, Route)``. Every route in this app is
    mounted with ``include_router``, and fastapi 0.139 records that as a lazy
    object rather than as the routes themselves — so the walk returned **zero**
    and this test failed on its own vacuity guard (fm#1305). ``openapi()``
    reports the same 50 POST paths on 0.136 and on 0.139.2, because resolving
    them is FastAPI's own job rather than this test's.
    """
    from faultmaven.main import app as real_app

    middleware = IdempotencyMiddleware(app=lambda scope, receive, send: None)
    post_paths = sorted(
        path
        for path, operations in real_app.openapi()["paths"].items()
        if "post" in operations
    )

    # Guard against a vacuous pass if route collection ever breaks.
    assert len(post_paths) > 40, f"only {len(post_paths)} POST routes enumerated"

    excluded = {path for path in post_paths if middleware._is_excluded_path(path)}

    assert "/api/v1/sessions" in excluded, "the session mint must be excluded"
    for path in excluded:
        assert (
            path == "/api/v1/sessions" or "/auth/" in path
        ), f"{path} is excluded but is neither the session mint nor an auth path"

    # Every other route whose path merely *contains* 'sessions' must survive.
    session_shaped = {p for p in post_paths if "/sessions" in p}
    assert len(session_shaped) > 10, "expected many session-shaped POST routes"
    assert session_shaped - excluded == session_shaped - {"/api/v1/sessions"}
    assert "/api/v1/cases/sessions/{session_id}/case" not in excluded


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
