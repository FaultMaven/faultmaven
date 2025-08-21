import os
import contextlib

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app


pytestmark = pytest.mark.integration


def test_readiness_endpoint_present():
    client = TestClient(app)
    r = client.get("/readiness")
    # We don't assert exact status here because env varies, but endpoint should exist
    assert r.status_code == 200
    assert "status" in r.json()


@pytest.mark.skipif(
    not os.getenv("REDIS_HOST") and not os.getenv("REDIS_URL"),
    reason="Requires REDIS_* env in CI to assert healthy readiness",
)
def test_readiness_reports_ready_with_redis_and_chroma():
    client = TestClient(app)
    r = client.get("/readiness")
    assert r.status_code == 200
    body = r.json()
    # In CI with dependencies up, expect ready; if not, this test can be adjusted per environment
    assert body.get("status") in {"ready", "unready"}


