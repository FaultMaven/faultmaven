import re
from typing import Iterable

from fastapi.testclient import TestClient

from faultmaven.main import app


client = TestClient(app)


def _split_expose_headers(value: str) -> Iterable[str]:
    return [h.strip() for h in value.split(",") if h.strip()]


def test_agent_endpoint_sends_deprecation_headers():
    # Use a simple agent endpoint that doesn't require auth
    # /api/v1/agent/health is marked deprecated and should include deprecation headers
    resp = client.get("/api/v1/agent/health")
    assert resp.status_code == 200

    # Deprecation headers must be present
    assert resp.headers.get("Deprecation") == "true"
    assert resp.headers.get("Sunset") is not None

    # Link header should point to the successor endpoint
    link = resp.headers.get("Link")
    assert link, "Expected Link header with successor-version relation"
    assert 'rel="successor-version"' in link

    # Basic sanity check that a URL is present in Link
    assert re.search(r"<[^>]+>", link), f"Unexpected Link format: {link}"


def test_cors_expose_headers_present_on_cors_request():
    # Simulate a CORS request by sending an Origin header
    resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200

    # Access-Control-Expose-Headers should include the configured headers
    aceh = resp.headers.get("access-control-expose-headers") or resp.headers.get("Access-Control-Expose-Headers")
    assert aceh, "Expected Access-Control-Expose-Headers in response"

    headers = {h.lower() for h in _split_expose_headers(aceh)}
    expected = {
        "location",
        "x-total-count",
        "link",
        "deprecation",
        "sunset",
        "x-request-id",
        "retry-after",
    }
    missing = expected - headers
    assert not missing, f"Missing expose headers: {sorted(missing)}; got: {headers}"


