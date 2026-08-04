"""An oversized upload to POST /knowledge/convert must be refused as 413.

Root cause guarded here: ``convert_document`` raised a raw
``HTTPException(413)`` from inside its ``try``. ``HTTPException`` is neither a
``FaultMavenException`` nor a ``ConversionRejectedError``, so it fell through
to the blanket ``except Exception``, which does not re-raise — it *returns* a
500 ``JSONResponse``::

    {"detail": "Document conversion failed. Please try again.",
     "error_code": "LLM_GENERATION_FAILED"}

Both halves were wrong: the advice is false (the upload is deterministically
too large, so retrying can never succeed) and the failure was misattributed to
LLM generation, pointing operators at the wrong subsystem.

The route now refuses with the module's own typed
``ConversionRejectedError(FILE_TOO_LARGE)``, which the existing status map
already translates to 413 with the canonical ``{detail, error_code}`` body, and
it does so **before** reading the upload — the ceiling comes from
``settings.upload.max_upload_size_mb`` rather than a local literal.

Uses Starlette's ``TestClient`` rather than ``httpx.ASGITransport``: per
TESTING_STANDARDS.md that is the documented-safe combination for multipart form
data with ``dependency_overrides``. The per-request event loop caveat that
rules TestClient out elsewhere in this suite does not apply here — this route
touches no fakeredis-backed infrastructure.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import faultmaven.modules.knowledge.api.conversion_routes as conversion_routes
from faultmaven.config.settings import get_settings
from faultmaven.modules.knowledge.api.conversion_routes import (
    _get_conversion_service,
    _require_auth,
)
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)

# Forced well below any real ceiling so the oversized payload stays small: the
# test exercises the limit, not the machine's memory.
CEILING_MB = 1
OVERSIZED = b"x" * (CEILING_MB * 1024 * 1024 + 1)
SMALL = b"# A runbook\n\nSome technical content.\n"


@pytest.fixture
def convert_service():
    """Service double that records whether the route reached it at all."""
    return SimpleNamespace(
        convert_document=AsyncMock(
            return_value=SimpleNamespace(
                model_dump=lambda: {"drafts": [], "status": "ok"}
            )
        )
    )


@pytest.fixture
def copy_spy(monkeypatch):
    """Counts writes of the upload to disk.

    This is what pins the *ordering*: a refusal that happens before the upload
    is read never reaches ``copyfileobj``. Asserting only that the conversion
    service went uncalled is too weak — a size check placed anywhere ahead of
    the service call satisfies that while still having paid the full read.
    """
    calls = []
    real = conversion_routes.shutil.copyfileobj

    def _spy(src, dst, *args, **kwargs):
        calls.append(1)
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(conversion_routes.shutil, "copyfileobj", _spy)
    return calls


@pytest.fixture
def client(convert_service, copy_spy, monkeypatch):
    """Route mounted with the upload ceiling forced down to CEILING_MB."""
    monkeypatch.setattr(get_settings().upload, "max_upload_size_mb", CEILING_MB)

    app = FastAPI()
    app.include_router(conversion_router, prefix="/api/v1")

    async def _service():
        return convert_service

    async def _user():
        return SimpleNamespace(
            user_id="user-1",
            organization_id="org-1",
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[_get_conversion_service] = _service
    app.dependency_overrides[_require_auth] = _user
    return TestClient(app)


def _post(client, content):
    return client.post(
        "/api/v1/knowledge/convert",
        # "personal" keeps the request clear of the global-authoring admin
        # gate, isolating the size branch.
        data={"scope": "personal"},
        files={"file": ("big.md", content, "text/markdown")},
    )


@pytest.mark.unit
def test_oversized_upload_is_refused_as_413_before_reading(
    client, convert_service, copy_spy
):
    """413 with the canonical error code, refused before the upload is read."""
    response = _post(client, OVERSIZED)

    assert response.status_code == 413, response.text

    body = response.json()
    assert body["error_code"] == "FILE_TOO_LARGE"
    assert "exceeds maximum size" in body["detail"]

    # Ordering oracle: nothing was streamed to disk, so the refusal preceded
    # the read rather than merely preceding the service call.
    assert copy_spy == []
    convert_service.convert_document.assert_not_awaited()


@pytest.mark.unit
def test_acceptable_upload_still_reaches_the_service(client, convert_service):
    """Vacuity control: a payload under the ceiling converts normally.

    Proves the 413 above comes from the size branch rather than from routing,
    multipart parsing, or dependency resolution.
    """
    response = _post(client, SMALL)

    assert response.status_code == 201, response.text
    assert response.json() == {"drafts": [], "status": "ok"}
    convert_service.convert_document.assert_awaited_once()
