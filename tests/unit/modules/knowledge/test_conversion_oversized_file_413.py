"""An oversized upload to POST /knowledge/convert must be a 413, not a 500.

Root cause guarded here: ``convert_document`` performs its size check inside
the ``try`` and raises its own ``HTTPException(413, "File exceeds maximum
size of 10MB")``. ``HTTPException`` is not a ``FaultMavenException`` and not a
``ConversionRejectedError``, so it fell through to the blanket
``except Exception``, which does not re-raise — it *returns* a 500
``JSONResponse``:

    {"detail": "Document conversion failed. Please try again.",
     "error_code": "LLM_GENERATION_FAILED"}

Two things make that worse than a wrong status. The advice is actively false —
the upload is deterministically too large and retrying can never succeed — and
the failure is misattributed to LLM generation, so the operator-facing error
code points investigation at the wrong subsystem.

Reachability: the route writes the upload to a temp file and stats it before
any service call, so a request whose body exceeds the 10 MB ceiling reaches the
branch. The test drives a real oversized multipart upload rather than patching
the size check, so the guard is exercised the way production hits it.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.modules.knowledge.api.conversion_routes import (
    _get_conversion_service,
    _require_auth,
)
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)

MAX_BYTES = 10 * 1024 * 1024  # the route's ceiling
OVERSIZED = b"x" * (MAX_BYTES + 1)
SMALL = b"# A runbook\n\nSome technical content.\n"


def _build_app():
    app = FastAPI()
    app.include_router(conversion_router, prefix="/api/v1")

    async def _service():
        async def convert_document(**kwargs):
            return SimpleNamespace(model_dump=lambda: {"drafts": [], "status": "ok"})

        return SimpleNamespace(convert_document=convert_document)

    async def _user():
        return SimpleNamespace(
            user_id="user-1",
            organization_id="org-1",
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[_get_conversion_service] = _service
    app.dependency_overrides[_require_auth] = _user
    return app


async def _convert(content):
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/v1/knowledge/convert",
            # "personal" keeps the request clear of the global-authoring admin
            # gate, isolating the size branch.
            data={"scope": "personal"},
            files={"file": ("big.md", content, "text/markdown")},
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_upload_surfaces_as_413_not_500():
    """The handler's own 413 must reach the client verbatim."""
    response = await _convert(OVERSIZED)

    # Exact status, not merely "not 500": a request that never reaches the
    # handler fails here rather than passing vacuously.
    assert response.status_code == 413, response.text

    body = response.json()
    assert "exceeds maximum size" in body["detail"]
    # The blanket handler's 500 body told the caller to retry a request that
    # can never succeed, and blamed LLM generation.
    assert body["detail"] != "Document conversion failed. Please try again."
    assert body.get("error_code") != "LLM_GENERATION_FAILED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_acceptable_upload_still_reaches_the_service():
    """Vacuity control: the same app/path reaches the handler and succeeds.

    Proves the 413 above is produced by the size branch rather than by
    routing, multipart parsing, or dependency resolution.
    """
    response = await _convert(SMALL)

    assert response.status_code == 201, response.text
    assert response.json() == {"drafts": [], "status": "ok"}
