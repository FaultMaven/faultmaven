"""The OAuth limiter advertises the limit it enforces.

The tightest quota an OAuth/SSO caller is under is this one — 5 to 10 per
minute, against the general limits' hundreds — and it was the only one invisible.
A 2xx carried ``X-RateLimit-*`` from the roomy global/session results, so a
client pacing itself off the headers paced against a limit it was nowhere near
while the one it was five requests from went unmentioned; and the 429 carried
``Retry-After`` alone, which says when to come back but not what was hit.

Both outcomes now publish this limiter's own state, in the same convention the
Redis-backed enforcer uses — so ``limit - current_count`` is the remaining quota
whichever limiter produced the result.
"""

from contextlib import asynccontextmanager

import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import State

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.models.protection import LimitType, RateLimitConfig
from faultmaven.modules.auth.api.rate_limiting import (
    OAuthRateLimiter,
    require_oauth_rate_limit_token,
    reset_rate_limiter,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

TOKEN_LIMIT = 5  # /token, requests per minute
WINDOW = 60


class _Peer:
    def __init__(self, host):
        self.host = host


class _StubRequest:
    """A request carrying a real ``State``, as the middleware would hand over."""

    def __init__(self, peer="203.0.113.9", rate_limit_results=None):
        from starlette.datastructures import Headers

        self.client = _Peer(peer)
        self.headers = Headers(raw=[])
        self.cookies: dict[str, str] = {}
        self.state = State()
        if rate_limit_results is not None:
            self.state.rate_limit_results = rate_limit_results


async def _refusal(limiter, request, endpoint="/token"):
    from fastapi import HTTPException

    for _ in range(TOKEN_LIMIT):
        await limiter.check_rate_limit(_StubRequest(), endpoint)
    with pytest.raises(HTTPException) as excinfo:
        await limiter.check_rate_limit(request, endpoint)
    assert excinfo.value.status_code == 429
    return excinfo.value


class TestTheRefusalNamesTheLimitItEnforced:
    async def test_it_carries_the_whole_quartet(self):
        limiter = OAuthRateLimiter(trusted_proxies=[])

        exc = await _refusal(limiter, _StubRequest())

        assert exc.headers["X-RateLimit-Limit"] == str(TOKEN_LIMIT)
        assert exc.headers["X-RateLimit-Remaining"] == "0"
        assert int(exc.headers["Retry-After"]) >= 1
        assert int(exc.headers["X-RateLimit-Reset"]) > 0

    async def test_reset_and_retry_after_name_one_instant(self):
        """Two renderings of a single ``frees_at``, so they must reconcile."""
        import time

        limiter = OAuthRateLimiter(trusted_proxies=[])

        exc = await _refusal(limiter, _StubRequest())

        retry_after = int(exc.headers["Retry-After"])
        reset = int(exc.headers["X-RateLimit-Reset"])
        assert abs(reset - (int(time.time()) + retry_after)) <= 1, (reset, retry_after)

    async def test_the_advertised_wait_is_bounded_by_the_window(self):
        """No sliding window of 60s can honestly ask for more than 60s."""
        limiter = OAuthRateLimiter(trusted_proxies=[])

        exc = await _refusal(limiter, _StubRequest())

        assert 1 <= int(exc.headers["Retry-After"]) <= WINDOW

    async def test_a_refusal_deep_in_the_window_advertises_the_short_wait(self):
        """A caller seconds from quota must not be told to wait a full minute."""
        import time

        limiter = OAuthRateLimiter(trusted_proxies=[])
        for _ in range(TOKEN_LIMIT):
            await limiter.check_rate_limit(_StubRequest(), "/token")

        # Age every recorded timestamp so the oldest is 55s into the 60s window.
        now = time.time()
        for key, stamps in limiter._requests.items():
            limiter._requests[key] = [now - 55.0] * len(stamps)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            await limiter.check_rate_limit(_StubRequest(), "/token")

        assert 1 <= int(excinfo.value.headers["Retry-After"]) <= 6


class TestAnAllowedRequestOffersItsStateToTheHeaderPath:
    async def test_it_appends_a_result_the_middleware_can_read(self):
        limiter = OAuthRateLimiter(trusted_proxies=[])
        results = []

        await limiter.check_rate_limit(
            _StubRequest(rate_limit_results=results), "/token"
        )

        assert len(results) == 1
        result = results[0]
        assert result.allowed is True
        assert result.limit_type is LimitType.OAUTH
        assert result.limit == TOKEN_LIMIT
        assert result.reset_time is not None

    async def test_the_count_convention_matches_the_redis_enforcer(self):
        """``current_count`` includes the request that just went in.

        The Lua script returns ``count + 1`` on its allowed path, so the header
        path computes ``limit - current_count`` without knowing which limiter
        produced the result. A convention that differed by one here would
        advertise one more (or one fewer) than the caller actually has.
        """
        limiter = OAuthRateLimiter(trusted_proxies=[])
        results = []
        request = _StubRequest(rate_limit_results=results)

        for _ in range(3):
            await limiter.check_rate_limit(request, "/token")

        assert [r.current_count for r in results] == [1, 2, 3]
        assert [r.limit - r.current_count for r in results] == [4, 3, 2]

    async def test_the_reset_instant_does_not_march_with_the_clock(self):
        """It names when quota frees, so it is stable across the window."""
        limiter = OAuthRateLimiter(trusted_proxies=[])
        results = []
        request = _StubRequest(rate_limit_results=results)

        for _ in range(3):
            await limiter.check_rate_limit(request, "/token")

        instants = {r.reset_time for r in results}
        assert len(instants) == 1, instants

    async def test_an_absent_list_is_never_created(self):
        """Its absence means the middleware is not in the stack.

        Manufacturing the list there would build a collection nothing reads and
        hide the fact that no header path exists on that app.
        """
        limiter = OAuthRateLimiter(trusted_proxies=[])
        request = _StubRequest()

        await limiter.check_rate_limit(request, "/token")

        assert not hasattr(request.state, "rate_limit_results")


class TestThroughTheFullMiddlewareStack:
    """The surface a client reads, with the middleware that writes it installed.

    Every test here runs the client as a context manager so the app's lifespan
    executes, and the FakeRedis is built *inside* that lifespan. Building it at
    test scope binds its internal queues to whichever loop imported it, and the
    middleware's checks then raise "bound to a different event loop", fail open,
    and report ``limit == 0`` — which the header path skips. The whole class
    would pass while asserting nothing about a healthy limiter, which is exactly
    how the overwrite defect below survived a green local run and was caught
    only by CI. ``_assert_middleware_limiter_is_healthy`` re-establishes the
    precondition per test rather than trusting the fixture to have held.
    """

    @staticmethod
    def _settings(global_requests):
        settings = get_development_protection_settings()
        settings.rate_limits = {
            "global": RateLimitConfig(
                enabled=True, requests=global_requests, window=WINDOW
            )
        }
        return settings

    def _app(self, global_requests):
        @asynccontextmanager
        async def lifespan(app):
            # Created in the loop that will serve the requests, so the
            # middleware's checks actually reach it.
            app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
            yield

        app = FastAPI(lifespan=lifespan)

        @app.post(
            "/api/v1/auth/oauth/token",
            dependencies=[Depends(require_oauth_rate_limit_token)],
        )
        async def token():
            return {"access_token": "t"}

        @app.get("/api/v1/cases")
        async def cases():
            return {"cases": []}

        app.add_middleware(
            RateLimitMiddleware, settings=self._settings(global_requests)
        )
        return app

    @staticmethod
    def _assert_middleware_limiter_is_healthy(client, global_requests):
        """Prove the middleware holds a real result before asserting on it.

        A route the OAuth limiter does not guard advertises the general limit
        and nothing else. If the middleware's limiter had failed open the result
        would carry ``limit == 0``, the header path would skip it, and no
        ``X-RateLimit-Limit`` would appear at all — so this distinguishes
        "healthy" from "silently vacuous" in one assertion.
        """
        probe = client.get("/api/v1/cases")
        assert probe.status_code == 200
        assert probe.headers.get("X-RateLimit-Limit") == str(global_requests), (
            "the middleware's limiter is not healthy — its results carry no "
            f"quota, so this test asserts nothing: {dict(probe.headers)}"
        )

    @pytest.fixture(autouse=True)
    def _clean(self):
        from faultmaven.infrastructure.redis_client import reset_fakeredis_client

        reset_fakeredis_client()
        reset_rate_limiter()
        yield
        reset_rate_limiter()
        reset_fakeredis_client()

    def test_the_served_response_advertises_the_oauth_limit_when_it_is_tightest(self):
        """The defect: the roomy global limit was the only one a client saw."""
        with TestClient(self._app(global_requests=1000)) as client:
            self._assert_middleware_limiter_is_healthy(client, 1000)

            response = client.post("/api/v1/auth/oauth/token")

            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == str(TOKEN_LIMIT)
            assert response.headers["X-RateLimit-Remaining"] == str(TOKEN_LIMIT - 1)

    def test_a_tighter_general_limit_still_wins(self):
        """ "Tightest" is a comparison, not a preference for the new entrant."""
        with TestClient(self._app(global_requests=3)) as client:
            self._assert_middleware_limiter_is_healthy(client, 3)

            response = client.post("/api/v1/auth/oauth/token")

            # The probe above spent one of the 3 and this request spends a
            # second, leaving the general limit 1 from exhaustion against the
            # OAuth limit's 4.
            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == "3"
            assert response.headers["X-RateLimit-Remaining"] == "1"

    def test_an_unlimited_route_is_unaffected(self):
        """Only the endpoints the OAuth limiter guards contribute its result."""
        with TestClient(self._app(global_requests=1000)) as client:
            response = client.get("/api/v1/cases")

            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == "1000"

    def test_the_oauth_refusal_survives_the_middleware_on_the_way_out(self):
        """The overwrite defect, on the stack production actually serves.

        The dependency's 429 comes back up through ``call_next``, so
        ``_add_rate_limit_headers`` runs on it holding the middleware's own
        healthy global result. Stamping that over the OAuth quartet advertised
        ``Remaining: 994`` beside a refusal — telling the caller the limit it
        just hit is nowhere near exhausted. The middleware must yield: an inner
        enforcer's headers stand, and no refusal is ever given a remaining quota.
        """
        with TestClient(self._app(global_requests=1000)) as client:
            self._assert_middleware_limiter_is_healthy(client, 1000)

            for _ in range(TOKEN_LIMIT):
                assert client.post("/api/v1/auth/oauth/token").status_code == 200

            refused = client.post("/api/v1/auth/oauth/token")

            assert refused.status_code == 429
            assert refused.headers["X-RateLimit-Limit"] == str(TOKEN_LIMIT), (
                "the middleware overwrote the enforcer's limit with its own: "
                f"{dict(refused.headers)}"
            )
            assert refused.headers["X-RateLimit-Remaining"] == "0", (
                "a refusal was given remaining quota: " f"{dict(refused.headers)}"
            )
            assert int(refused.headers["Retry-After"]) >= 1
            assert int(refused.headers["X-RateLimit-Reset"]) > 0

    def _scratch_app(self, route):
        """The middleware over one route that writes its own response."""

        @asynccontextmanager
        async def lifespan(app):
            app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
            yield

        app = FastAPI(lifespan=lifespan)
        app.get("/api/v1/probe")(route)
        app.get("/api/v1/cases")(lambda: {"cases": []})
        app.add_middleware(RateLimitMiddleware, settings=self._settings(1000))
        return app

    def test_a_refusal_carrying_no_limit_headers_is_still_left_alone(self):
        """The 429 condition, isolated from the already-written one.

        A refusal with no ``X-RateLimit-*`` on it — anything that 429s for a
        reason other than one of these windows — must not be handed the
        middleware's remaining quota. Remaining beside a refusal is a
        contradiction whatever produced the refusal.
        """
        from fastapi.responses import JSONResponse

        async def _bare_429():
            return JSONResponse(status_code=429, content={"error": "elsewhere"})

        with TestClient(self._scratch_app(_bare_429)) as client:
            self._assert_middleware_limiter_is_healthy(client, 1000)

            refused = client.get("/api/v1/probe")

            assert refused.status_code == 429
            assert "X-RateLimit-Remaining" not in refused.headers, dict(refused.headers)
            assert "X-RateLimit-Limit" not in refused.headers, dict(refused.headers)

    def test_an_inner_writers_headers_on_a_200_are_left_alone(self):
        """The already-written condition, isolated from the 429 one.

        An enforcer that advertises its own quota on a *served* response is
        authoritative for that response: it measured the limit it wrote, and the
        middleware holds results only for the general limits. No enforcer in the
        tree currently writes on a 200 — the OAuth limiter contributes a result
        instead — so this pins the rule rather than a present caller, and it is
        the case the 429 condition alone would let through.
        """

        async def _self_advertising():
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=200,
                content={"ok": True},
                headers={"X-RateLimit-Limit": "7", "X-RateLimit-Remaining": "2"},
            )

        with TestClient(self._scratch_app(_self_advertising)) as client:
            self._assert_middleware_limiter_is_healthy(client, 1000)

            response = client.get("/api/v1/probe")

            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == "7", dict(response.headers)
            assert response.headers["X-RateLimit-Remaining"] == "2"
