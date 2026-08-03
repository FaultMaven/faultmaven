"""Per-attachment size cap on the unified /turns endpoint.

Starlette >= 1.1 bounds only non-file multipart fields (via Request.form()'s
max_part_size, overridden in faultmaven.main); file parts reach the route
unbounded, so submit_turn enforces MAX_UPLOAD_SIZE_MB per attachment itself.
These tests pin that behavior at the route surface.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.modules.case.api.routes import (
    _di_get_case_service_dependency,
    get_investigation_service,
    require_authentication,
)
from faultmaven.modules.case.api.routes import router as case_router

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # settings default: MAX_UPLOAD_SIZE_MB=10


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    app.dependency_overrides[require_authentication] = lambda: MagicMock()
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: MagicMock()
    app.dependency_overrides[get_investigation_service] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
class TestTurnUploadSizeCap:
    def test_oversized_attachment_is_rejected_with_413(self):
        client = _client()
        oversized = b"x" * (MAX_UPLOAD_BYTES + 1024)
        response = client.post(
            "/api/v1/cases/case_123/turns",
            files={"files": ("huge.log", oversized, "text/plain")},
        )
        assert response.status_code == 413, response.text
        assert "upload limit" in str(response.json()["detail"])
        assert "huge.log" in str(response.json()["detail"])

    def test_small_attachment_passes_the_size_gate(self):
        client = _client()
        response = client.post(
            "/api/v1/cases/case_123/turns",
            files={"files": ("small.log", b"a few bytes", "text/plain")},
        )
        # Downstream mocks may fail in any number of ways; the property under
        # test is only that the size gate does not fire.
        assert response.status_code != 413, response.text
