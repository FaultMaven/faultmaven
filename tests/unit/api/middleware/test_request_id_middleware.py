"""Unit tests for RequestIdMiddleware.

Verifies that:
- An incoming X-Request-ID header is honored, bound into structlog
  contextvars for the duration of the request, and echoed in the response.
- A UUID is generated when the header is missing.
- The request_id is unbound after the request, even when the handler raises.
- It writes **no** rate-limit headers and no rate-limit request state (fm#931).

The last of those is the fm#931 residue. This module used to carry a second
rate-limit header authority: a ``RateLimitHeaderMiddleware`` sibling that stamped
``X-RateLimit-Limit: 1000 / Remaining: 999 / Reset: now+3600 /
Window: 3600s`` on every response and pre-set ``request.state.retry_after = 60``,
plus a branch here that overwrote a 429's ``Retry-After`` with that constant.
Neither knew what any limiter had enforced, and both were registered *outside*
the enforcement, so the fabrication won. It was invisible to tests because
``setup_middleware`` skips the whole block under a test environment — hence
``TestTheHonestWaitSurvivesTheMiddlewareStack`` below, which stacks the
middleware itself rather than relying on the app that skips it.
"""

import time
import uuid

import pytest
import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from faultmaven.api.middleware import request_id as request_id_module
from faultmaven.api.middleware.request_id import RequestIdMiddleware


@pytest.fixture(autouse=True)
def clean_contextvars():
    """Ensure no structlog contextvars leak between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def unbind_spy(monkeypatch):
    """Spy on structlog.contextvars.unbind_contextvars, preserving behavior."""
    calls = []
    real_unbind = structlog.contextvars.unbind_contextvars

    def spy(*keys):
        calls.append(keys)
        return real_unbind(*keys)

    monkeypatch.setattr(structlog.contextvars, "unbind_contextvars", spy)
    return calls


@pytest.fixture
def captured():
    """Holder for values captured inside route handlers during the request."""
    return {}


@pytest.fixture
def app(captured):
    """Minimal FastAPI app with RequestIdMiddleware and capture routes."""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ctx")
    async def ctx(request: Request):
        captured["contextvars"] = dict(structlog.contextvars.get_contextvars())
        captured["state_request_id"] = request.state.request_id
        captured["state_keys"] = set(request.state._state)
        return {"ok": True}

    @app.get("/refused")
    async def refused():
        # A downstream limiter's own refusal, with its own measured wait.
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded.",
            headers={"Retry-After": "3"},
        )

    @app.get("/refused-silently")
    async def refused_silently():
        # A 429 from something that has nothing to advertise.
        return JSONResponse({"detail": "no"}, status_code=429)

    @app.get("/boom")
    async def boom():
        captured["contextvars"] = dict(structlog.contextvars.get_contextvars())
        raise RuntimeError("handler exploded")

    return app


@pytest.mark.unit
class TestIncomingRequestIdHonored:
    """An incoming X-Request-ID is reused, bound, and echoed."""

    def test_header_bound_during_handling_and_echoed(self, app, captured, unbind_spy):
        client = TestClient(app)

        response = client.get("/ctx", headers={"X-Request-ID": "my-id-123"})

        assert response.status_code == 200
        # Echoed in response header
        assert response.headers["X-Request-ID"] == "my-id-123"
        # Bound into structlog contextvars while the handler ran
        assert captured["contextvars"].get("request_id") == "my-id-123"
        # Also exposed on request.state for other components
        assert captured["state_request_id"] == "my-id-123"
        # Unbound after the request completed
        assert ("request_id",) in unbind_spy

    def test_processing_time_header_added(self, app):
        client = TestClient(app)

        response = client.get("/ctx", headers={"X-Request-ID": "abc"})

        assert "X-Processing-Time" in response.headers


@pytest.mark.unit
class TestGeneratedRequestId:
    """A UUID is generated when no X-Request-ID header is supplied."""

    def test_uuid_generated_bound_and_echoed(self, app, captured, unbind_spy):
        client = TestClient(app)

        response = client.get("/ctx")

        assert response.status_code == 200
        request_id = response.headers["X-Request-ID"]
        # Valid UUID
        uuid.UUID(request_id)
        # The same generated ID was bound during handling
        assert captured["contextvars"].get("request_id") == request_id
        assert captured["state_request_id"] == request_id
        # Unbound after the request completed
        assert ("request_id",) in unbind_spy


@pytest.mark.unit
class TestUnbindOnException:
    """Unbinding happens even when the handler raises."""

    def test_unbound_when_handler_raises(self, app, captured, unbind_spy):
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/boom", headers={"X-Request-ID": "boom-id"})

        assert response.status_code == 500
        # request_id was bound while the failing handler ran
        assert captured["contextvars"].get("request_id") == "boom-id"
        # The finally-block still unbound it
        assert ("request_id",) in unbind_spy
        # And the contextvar truly isn't lingering in a fresh context
        assert "request_id" not in structlog.contextvars.get_contextvars()


# The rate-limit family, spelled once. A correlation layer must not produce any
# member of it — it has no idea what, if anything, was enforced.
RATE_LIMIT_HEADERS = (
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "X-RateLimit-Window",
)


@pytest.mark.unit
@pytest.mark.security
class TestNoRateLimitSignalIsManufactured:
    """This layer emits correlation headers and nothing else (fm#931)."""

    def test_a_served_response_carries_no_rate_limit_headers(self, app):
        client = TestClient(app)

        response = client.get("/ctx")

        assert response.status_code == 200
        for header in RATE_LIMIT_HEADERS:
            assert header not in response.headers, header

    def test_no_rate_limit_state_is_pre_set_on_the_request(self, app, captured):
        """The state the sibling middleware seeded is what the 429 branch read.

        Asserted on ``request.state`` rather than only on headers because state
        is the wider blast radius: anything downstream could have believed it.
        """
        client = TestClient(app)

        client.get("/ctx")

        assert captured["state_keys"] == {"request_id"}, captured["state_keys"]

    def test_a_429_keeps_the_wait_its_refuser_measured(self, app):
        """The overwrite itself: a downstream 3s must not become a constant."""
        client = TestClient(app)

        response = client.get("/refused")

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "3", response.headers

    def test_a_429_with_nothing_to_advertise_gets_no_default(self, app):
        """No ``Retry-After`` is honest; a fabricated 60 is not.

        A client that reads no header backs off on its own policy. One that
        reads a made-up minute backs off on ours, and we did not measure it.
        """
        client = TestClient(app)

        response = client.get("/refused-silently")

        assert response.status_code == 429
        for header in RATE_LIMIT_HEADERS:
            assert header not in response.headers, header

    def test_correlation_still_works_on_a_refusal(self, app):
        """Removing the fabrication must not cost the header that is real."""
        client = TestClient(app)

        response = client.get("/refused", headers={"X-Request-ID": "refused-id"})

        assert response.headers["X-Request-ID"] == "refused-id"
        assert "X-Processing-Time" in response.headers

    def test_the_module_exports_no_second_header_authority(self):
        """The deleted sibling must not come back under any name.

        ``RateLimitHeaderMiddleware`` was reachable only through
        ``setup_middleware``, so no test referenced it and nothing noticed it
        existed. This pins the module's surface instead of one call site.
        """
        from starlette.middleware.base import BaseHTTPMiddleware

        middlewares = {
            name
            for name, obj in vars(request_id_module).items()
            if isinstance(obj, type)
            and issubclass(obj, BaseHTTPMiddleware)
            and obj is not BaseHTTPMiddleware
        }

        assert middlewares == {"RequestIdMiddleware"}, middlewares


@pytest.mark.unit
@pytest.mark.security
class TestTheHonestWaitSurvivesTheMiddlewareStack:
    """The production shape fm#931's original tests could not see.

    ``setup_middleware`` skips this middleware entirely under a test
    environment, so every assertion made against ``faultmaven.main.app`` was
    blind to it: the OAuth limiter's honest ``Retry-After`` was verified at
    ``check_rate_limit``'s ``HTTPException`` and never at the surface a client
    reads. Measured end-to-end, a ~5-second wait rendered as 60.

    So this stacks the middleware over the real OAuth-limited dependency
    directly, in production's relative order (middleware outer, route inner),
    and asserts on ``response.headers``.
    """

    OLDEST_AGE_SECONDS = 55.0  # deep in the 60s window: ~5s left to wait

    @pytest.fixture
    def limited_app(self):
        from faultmaven.modules.auth.api.rate_limiting import (
            require_oauth_rate_limit_token,
            reset_rate_limiter,
        )

        reset_rate_limiter()
        app = FastAPI()

        @app.post(
            "/api/v1/auth/oauth/token",
            dependencies=[Depends(require_oauth_rate_limit_token)],
        )
        async def token():
            return {"access_token": "t"}

        # Registered exactly as ``setup_middleware`` registers it: outer to the
        # route, so it sees the 429 the dependency raised.
        app.add_middleware(RequestIdMiddleware)
        yield app
        reset_rate_limiter()

    @staticmethod
    def _age_the_recorded_window(seconds):
        """Rewind every recorded timestamp so the oldest is ``seconds`` old.

        The budget is spent through the real client first, so the limiter's key
        is whatever the resolver actually produced — guessing it here is how a
        regression test ends up exercising an empty bucket.
        """
        from faultmaven.modules.auth.api import rate_limiting

        recorded = rate_limiting._oauth_rate_limiter._requests
        assert recorded, "the limiter recorded nothing; the test is vacuous"
        now = time.time()
        for key, timestamps in recorded.items():
            recorded[key] = [now - seconds] * len(timestamps)

    def _refusal(self, limited_app):
        client = TestClient(limited_app)

        for _ in range(5):  # /token's per-minute budget
            assert client.post("/api/v1/auth/oauth/token").status_code == 200

        self._age_the_recorded_window(self.OLDEST_AGE_SECONDS)
        return client.post("/api/v1/auth/oauth/token")

    def test_the_client_reads_the_wait_the_limiter_measured(self, limited_app):
        response = self._refusal(limited_app)

        assert response.status_code == 429, "the limit did not trip; test is vacuous"
        advertised = int(response.headers["Retry-After"])
        assert advertised != 60, (
            "the honest wait was overwritten with the flat-minute fabrication: "
            f"{dict(response.headers)}"
        )
        assert 1 <= advertised <= 6, advertised

    def test_no_fabricated_quota_accompanies_the_refusal(self, limited_app):
        """The OAuth limiter publishes only ``Retry-After``.

        Anything else in the family on this response was invented by a layer
        that enforced nothing — ``X-RateLimit-Remaining: 999`` beside a 429 is
        a contradiction the client has to resolve, and it resolves it wrongly.
        """
        response = self._refusal(limited_app)

        assert response.status_code == 429
        for header in RATE_LIMIT_HEADERS:
            if header == "Retry-After":
                continue
            assert header not in response.headers, (header, dict(response.headers))

    def test_a_served_request_through_the_same_stack_is_equally_quiet(
        self, limited_app
    ):
        client = TestClient(limited_app)

        response = client.post("/api/v1/auth/oauth/token")

        assert response.status_code == 200
        for header in RATE_LIMIT_HEADERS:
            assert header not in response.headers, header
