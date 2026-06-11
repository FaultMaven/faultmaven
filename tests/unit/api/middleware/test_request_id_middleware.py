"""Unit tests for RequestIdMiddleware structlog contextvars binding.

Verifies that:
- An incoming X-Request-ID header is honored, bound into structlog
  contextvars for the duration of the request, and echoed in the response.
- A UUID is generated when the header is missing.
- The request_id is unbound after the request, even when the handler raises.
"""

import uuid

import pytest
import structlog
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

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
        return {"ok": True}

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
