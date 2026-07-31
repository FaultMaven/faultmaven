"""#866: raw exception text never reaches a knowledge-module response body.

The knowledge module had two ways to hand a caller the text of an internal
exception:

* the degraded-but-successful returns in ``KnowledgeService``
  (``list_documents``, ``search_documents``, ``fulltext_search_documents``),
  which put ``str(e)`` into the ``error`` key of a 200 body; and
* ``HTTPException(status_code=500, detail=f"...: {str(e)}")`` throughout
  ``knowledge/api/routes.py``.

A DB driver raises with the connection URI in the message, so both paths could
echo credentials — and ``GET /knowledge/documents`` takes optional auth, so the
200 arm was reachable with no credentials at all.

These tests assert on the *class*, not on three sites: nothing resembling a
driver string reaches the body, while the degraded 200 responses keep their
shape (callers key on ``error`` being present).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.modules.auth.contracts import DevUser

# A driver-style connection error. Split so the literal credential pair never
# appears as a single token in the source tree.
_DSN = "postgres://kbuser:" + "s3cr3t" + "@db.internal:5432/faultmaven"
_DRIVER_ERROR = f"could not connect to server: {_DSN}"


def _assert_no_leak(body_text: str) -> None:
    """Nothing recognizably from the driver exception survives into the body."""
    assert _DSN not in body_text
    assert "s3cr3t" not in body_text
    assert "db.internal" not in body_text
    assert "could not connect to server" not in body_text


def _user() -> DevUser:
    return DevUser(
        user_id="user-1",
        username="user-1",
        email="user-1@example.com",
        display_name="user-1",
        created_at=datetime.now(timezone.utc),
        organization_id="org-1",
    )


def _client(knowledge_service, user=None):
    """Knowledge router on a bare app, mirroring test_document_read_visibility."""
    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import (
        get_current_user_optional,
        require_authentication,
    )
    from faultmaven.modules.knowledge.api.routes import get_knowledge_service, router

    app = FastAPI()
    app.include_router(router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service
    if user is None:
        app.dependency_overrides[get_current_user_optional] = lambda: None
    else:
        app.dependency_overrides[get_current_user_optional] = lambda: user
        app.dependency_overrides[require_authentication] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _service() -> "object":
    """A real KnowledgeService with only the collaborators these paths touch."""
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    return KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        # Required since #899. The paths under test either replace it with a
        # raising stub or never reach it.
        db_session_factory=MagicMock(),
    )


# ===========================================================================
# Degraded 200 bodies — anonymous-reachable
# ===========================================================================


def test_anonymous_document_listing_degrades_without_echoing_the_exception():
    """``GET /knowledge/documents`` is optional-auth; its degraded 200 leaked."""

    def _boom():
        raise RuntimeError(_DRIVER_ERROR)

    service = _service()
    service._db_session_factory = _boom

    response = _client(service).get("/knowledge/documents")

    assert response.status_code == 200
    _assert_no_leak(response.text)

    body = response.json()
    # Shape preserved: this is a degraded success, and callers key on `error`.
    assert body["documents"] == []
    assert body["total_count"] == 0
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["error"] == "Failed to list documents"


@pytest.mark.asyncio
async def test_semantic_search_degrades_without_echoing_the_exception():
    service = _service()
    service._vector_store = MagicMock()
    service._vector_store.search = AsyncMock(side_effect=RuntimeError(_DRIVER_ERROR))

    result = await service.search_documents(query="disk full")

    _assert_no_leak(repr(result))
    assert result["query"] == "disk full"
    assert result["total_results"] == 0
    assert result["results"] == []
    assert result["error"] == "Search failed"


@pytest.mark.asyncio
async def test_fulltext_search_degrades_without_echoing_the_exception():
    service = _service()
    service.list_documents = AsyncMock(side_effect=RuntimeError(_DRIVER_ERROR))

    result = await service.fulltext_search_documents(query="disk full")

    _assert_no_leak(repr(result))
    assert result["query"] == "disk full"
    assert result["total_results"] == 0
    assert result["results"] == []
    assert result["error"] == "Search failed"


# ===========================================================================
# 500 bodies
# ===========================================================================


def test_anonymous_listing_500_does_not_echo_the_exception():
    """The listing route's own 500 arm is anonymous-reachable too.

    ``_resolve_team_ids`` runs before the service call, so a failure there
    takes the route's ``except Exception`` arm rather than the service's
    degraded return.
    """
    service = MagicMock()
    service.list_documents = AsyncMock(return_value={"documents": []})

    with patch(
        "faultmaven.modules.knowledge.api.routes._resolve_team_ids",
        AsyncMock(side_effect=RuntimeError(_DRIVER_ERROR)),
    ):
        response = _client(service).get("/knowledge/documents")

    assert response.status_code == 500
    _assert_no_leak(response.text)
    assert response.json()["detail"] == "Failed to list documents"


def test_anonymous_search_500_does_not_echo_the_exception():
    service = MagicMock()
    service.search_documents = AsyncMock(side_effect=RuntimeError(_DRIVER_ERROR))

    response = _client(service).post("/knowledge/search", json={"query": "disk full"})

    assert response.status_code == 500
    _assert_no_leak(response.text)
    assert response.json()["detail"] == "Search failed"


def test_stats_500_does_not_echo_the_exception():
    service = MagicMock()
    service.get_knowledge_stats = AsyncMock(side_effect=RuntimeError(_DRIVER_ERROR))

    response = _client(service, user=_user()).get("/knowledge/stats")

    assert response.status_code == 500
    _assert_no_leak(response.text)
    assert response.json()["detail"] == "Failed to get statistics"
