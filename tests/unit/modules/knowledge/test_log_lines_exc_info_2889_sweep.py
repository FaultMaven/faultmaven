"""The knowledge area's arms that log an unexpected failure before a 5xx carry
its frames (#2889, the ``exc_info`` sweep).

Principles §7, "What a log line may hold", rule 4, as ruled on #2886: "A line
that logs an unexpected failure before the request is answered with a 5xx
carries that exception's frames (``exc_info``)." Stage S3 of #2886.

Each case drives one arm through its mounted route with :data:`MARKER` in the
exception's message, captures every record at DEBUG, and renders each through
the real chain's formatter (``build_log_formatter``). The marker is on no
rendered line, the arm's line names the class and carries its frames, and the
status the caller is answered is the one it was before the frames were added.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure import worker_process
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api import routes as knowledge_routes
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.services import runbook_gate
from tests.runbook_samples import valid_runbook
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _framed(rendered: list[str], event: str, cls: str) -> None:
    """The arm's line names ``cls`` and carries its frames; no line holds the
    marker."""
    line = _named(rendered, event, cls)
    assert line["frames"].startswith(f"{cls}:"), line


def _store_failure() -> RuntimeError:
    return RuntimeError(f"could not connect to postgres://kb:{MARKER}@db")


def _user() -> DevUser:
    return DevUser(
        user_id="u1",
        username="u1",
        email="u1@example.com",
        display_name="u1",
        created_at=datetime.now(timezone.utc),
    )


def _own_document() -> dict:
    """A personal document the caller owns, so the write gate admits it."""
    return {
        "document_id": "doc1",
        "title": "T",
        "content": "C",
        "document_type": "runbook",
        "tags": [],
        "scope": "personal",
        "owner_id": "u1",
        "metadata": {},
    }


async def _call(
    method: str, path: str, *, knowledge=None, conversion=None, extra_router=None, **kw
) -> httpx.Response:
    """The knowledge and conversion routers on a bare app with the app's
    exception handlers, the caller authenticated as ``u1``."""
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(conversion_router, prefix="/api/v1")
    if extra_router is not None:
        app.include_router(extra_router)
    app.state.team_service = None
    app.state.conversion_service = conversion
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge
    app.dependency_overrides[require_authentication] = _user
    app.dependency_overrides[get_current_user_optional] = _user
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kw)


# ── modules/knowledge/api/routes.py ───────────────────────────────────────


async def test_a_failed_upload_carries_its_frames(lines):
    """``POST /knowledge/documents`` whose publish raises: the static 500, and
    the catch-all's line with its frames."""
    service = MagicMock()
    service.upload_document = AsyncMock(side_effect=_store_failure())

    answer = await _call(
        "POST",
        "/api/v1/knowledge/documents",
        knowledge=service,
        data={"title": "T", "document_type": "runbook", "scope": "personal"},
        files={"file": ("rb.md", valid_runbook().encode(), "text/markdown")},
    )

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Document upload failed"
    _framed(lines(), "Document upload failed", "RuntimeError")


@pytest.mark.parametrize(
    ("method", "path", "body", "event", "detail"),
    [
        pytest.param(
            "GET",
            "/api/v1/knowledge/documents",
            None,
            "Failed to list documents",
            "Failed to list documents",
            id="list",
        ),
        pytest.param(
            "GET",
            "/api/v1/knowledge/documents/doc1",
            None,
            "Failed to retrieve document doc1",
            "Failed to retrieve document",
            id="get",
        ),
        pytest.param(
            "GET",
            "/api/v1/knowledge/documents/doc1/snippet",
            None,
            "Failed to get snippet for document doc1",
            "Failed to get document snippet",
            id="snippet",
        ),
        pytest.param(
            "POST",
            "/api/v1/knowledge/search",
            {"query": "disk full"},
            "Search failed",
            "Search failed",
            id="search",
        ),
        pytest.param(
            "POST",
            "/api/v1/knowledge/documents/search",
            {"query": "disk full"},
            "Full-text search failed",
            "Full-text search failed",
            id="fulltext-search",
        ),
    ],
)
async def test_a_failed_read_carries_its_frames(
    lines, method, path, body, event, detail
):
    """Each read route whose team-visibility lookup raises: the catch-all's
    static 500, and its line with the frames."""
    with patch.object(
        knowledge_routes,
        "_resolve_team_ids",
        AsyncMock(side_effect=_store_failure()),
    ):
        answer = await _call(method, path, knowledge=MagicMock(), json=body)

    assert answer.status_code == 500
    assert answer.json()["detail"] == detail
    _framed(lines(), event, "RuntimeError")


async def test_a_failed_delete_carries_its_frames(lines):
    """``DELETE /knowledge/documents/{id}`` on the caller's own document whose
    delete raises: the static 500, and the line with its frames."""
    service = MagicMock()
    service.get_document = AsyncMock(return_value=_own_document())
    service.delete_document = AsyncMock(side_effect=_store_failure())

    answer = await _call(
        "DELETE", "/api/v1/knowledge/documents/doc1", knowledge=service
    )

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Failed to delete document"
    _framed(lines(), "Failed to delete document doc1", "RuntimeError")


@pytest.mark.parametrize(
    ("error", "event", "cls", "status"),
    [
        pytest.param(
            KnowledgeBaseError(
                f"embedder {MARKER}", error_code="KNOWLEDGE_INDEXING_FAILED"
            ),
            "Failed to re-index document doc1",
            "KnowledgeBaseError",
            503,
            id="reindex-failed",
        ),
        pytest.param(
            _store_failure(),
            "Failed to update document doc1",
            "RuntimeError",
            500,
            id="catch-all",
        ),
    ],
)
async def test_a_failed_update_carries_its_frames(lines, error, event, cls, status):
    """``PUT /knowledge/documents/{id}`` on the caller's own document whose
    update raises: a re-index failure's 503 or the catch-all's 500, and the
    arm's line with its frames."""
    service = MagicMock()
    service.get_document = AsyncMock(return_value=_own_document())
    service.update_document_metadata = AsyncMock(side_effect=error)

    answer = await _call(
        "PUT",
        "/api/v1/knowledge/documents/doc1",
        knowledge=service,
        json={"content": "new body"},
    )

    assert answer.status_code == status
    _framed(lines(), event, cls)


@pytest.mark.parametrize(
    ("path", "body", "event", "detail"),
    [
        pytest.param(
            "/api/v1/knowledge/documents/bulk-update",
            {"document_ids": ["doc1"], "updates": {"category": "db"}},
            "Bulk update failed",
            "Bulk update failed",
            id="bulk-update",
        ),
        pytest.param(
            "/api/v1/knowledge/documents/bulk-delete",
            {"document_ids": ["doc1"]},
            "Bulk delete failed",
            "Bulk delete failed",
            id="bulk-delete",
        ),
    ],
)
async def test_a_failed_bulk_write_carries_its_frames(lines, path, body, event, detail):
    """Each bulk route whose per-target gate raises: the catch-all's static
    500, and its line with the frames."""
    with patch.object(
        knowledge_routes,
        "_partition_bulk_targets",
        AsyncMock(side_effect=_store_failure()),
    ):
        answer = await _call("POST", path, knowledge=MagicMock(), json=body)

    assert answer.status_code == 500
    assert answer.json()["detail"] == detail
    _framed(lines(), event, "RuntimeError")


@pytest.mark.parametrize(
    ("path", "service_method", "event", "detail"),
    [
        pytest.param(
            "/api/v1/knowledge/stats",
            "get_knowledge_stats",
            "Failed to get knowledge stats",
            "Failed to get statistics",
            id="stats",
        ),
        pytest.param(
            "/api/v1/knowledge/analytics/search",
            "get_search_analytics",
            "Failed to get search analytics",
            "Failed to get analytics",
            id="analytics",
        ),
    ],
)
async def test_a_failed_aggregate_read_carries_its_frames(
    lines, path, service_method, event, detail
):
    """Each aggregate route whose service read raises: the catch-all's static
    500, and its line with the frames."""
    service = MagicMock()
    setattr(service, service_method, AsyncMock(side_effect=_store_failure()))

    answer = await _call("GET", path, knowledge=service)

    assert answer.status_code == 500
    assert answer.json()["detail"] == detail
    _framed(lines(), event, "RuntimeError")


# ── modules/knowledge/api/conversion_routes.py ────────────────────────────


async def test_a_failed_draft_delete_carries_its_frames(lines):
    """``DELETE /knowledge/conversions/{id}/drafts/{draft_id}`` whose delete
    raises ``ServiceException`` (the trace backend unreachable): the static
    500, and the line with its frames."""
    conversion = MagicMock()
    conversion.delete_draft = AsyncMock(
        side_effect=ServiceException(f"opik at https://x:{MARKER}@traces")
    )

    answer = await _call(
        "DELETE",
        "/api/v1/knowledge/conversions/conv1/drafts/draft1",
        conversion=conversion,
    )

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Failed to delete draft"
    _framed(lines(), "Failed to delete draft draft1", "ServiceException")


# ── modules/knowledge/domain/services/runbook_gate.py ─────────────────────


async def test_checks_that_do_not_answer_carry_their_frames(lines, monkeypatch):
    """The runbook checks whose worker fails, behind a route that runs them:
    ``GateUnavailable``'s 503 from the app's handler (which logs no frames),
    and the gate's line with the worker failure's frames."""
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)
    monkeypatch.setattr(
        worker_process,
        "run",
        AsyncMock(side_effect=worker_process.WorkerFailed(f"frame {MARKER}")),
    )
    checks = APIRouter()

    @checks.post("/checks")
    async def _run_checks():
        await runbook_gate.avalidate_and_score("body", principal="u1")

    answer = await _call("POST", "/checks", extra_router=checks)

    assert answer.status_code == 503
    assert answer.headers["x-error-code"] == "RUNBOOK_VALIDATION_UNAVAILABLE"
    _framed(lines(), "did not answer", "WorkerFailed")
