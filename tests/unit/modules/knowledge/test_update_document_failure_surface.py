"""What PUT /knowledge/documents/{id} TELLS the caller when re-indexing fails.

#952 made the row commit last, so a re-index failure now leaves the document
unchanged rather than half-applied. That guarantee is only worth something if
the response says so — a caller who is told "Document saved" when nothing was
saved will not retry, and the edit is silently lost.

This file exists because that surface had no test at all. An adversarial review
of the #952/#953 branch replaced the whole error-code tuple below with an empty
one — so every failure rendered the wrong claim about whether the document
survived — and all 544 knowledge tests still passed. The route is the layer
that RENDERS the guarantee; pinning the service alone pins the half nobody
reads.

It also tells an operator what search serves while they retry: since #2388 the
new chunks are written under a new generation and nothing is deleted before the
row moves, so whichever indexing step failed, the chunks the row served before
the update are still the ones served. One answer, for every error code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.domain import global_authoring
from faultmaven.providers.tenancy.factory import BUILTIN_SINGLE

pytestmark = [pytest.mark.unit]

#: Every indexing failure the update path raises.
CODES = [
    "KNOWLEDGE_EMBEDDER_UNAVAILABLE",
    "KNOWLEDGE_EMBEDDER_TIMEOUT",
    "KNOWLEDGE_NO_CHUNKS",
    "KNOWLEDGE_INDEXING_FAILED",
]


def _user(*, user_id="u1", roles=("user",)) -> DevUser:
    return DevUser(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        created_at=datetime.now(timezone.utc),
        roles=list(roles),
    )


def _doc():
    return {
        "document_id": "doc1",
        "title": "T",
        "content": "C",
        "document_type": "runbook",
        "tags": [],
        "scope": "personal",
        "owner_id": "u1",
        "source_url": None,
        "created_at": "",
        "updated_at": "",
        "metadata": {},
    }


def _client(service, user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.knowledge.api.routes import get_knowledge_service, router

    app = FastAPI()
    app.include_router(router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    app.dependency_overrides[require_authentication] = lambda: user
    return TestClient(app)


def _service_raising(error):
    service = MagicMock()
    doc = _doc()
    service.get_document = AsyncMock(return_value=doc)
    service.get_document_visible = AsyncMock(return_value=doc)
    service.update_document_metadata = AsyncMock(side_effect=error)
    return service


def _put(monkeypatch, error):
    monkeypatch.setattr(
        global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
    )
    client = _client(_service_raising(error), _user())
    return client.put("/knowledge/documents/doc1", json={"content": "new body"})


# ---------------------------------------------------------------------------
# The response must not claim the edit was saved — it wasn't
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", CODES)
def test_a_reindex_failure_never_reports_the_edit_as_saved(monkeypatch, code):
    """The pre-#952 response opened with "Document saved, but re-indexing for
    search failed". Under commit-last that is false for every one of these."""
    resp = _put(monkeypatch, KnowledgeBaseError("boom", error_code=code))

    assert resp.status_code == 503
    detail = resp.json()["detail"].lower()
    assert "not saved" in detail, f"{code} response does not say the edit was lost"
    assert "document saved" not in detail


# ---------------------------------------------------------------------------
# ...and says search is unchanged too, whatever failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [*CODES, "KNOWLEDGE_NEW"])
def test_every_indexing_failure_says_search_is_unchanged(monkeypatch, code):
    """No indexing step deletes a chunk (#2388), so a failure at any of them,
    a code this route has never seen included, leaves what search serves as
    it was."""
    resp = _put(monkeypatch, KnowledgeBaseError("boom", error_code=code))

    detail = resp.json()["detail"].lower()
    assert "unchanged, and so is what search serves for it" in detail, detail
    assert "may not be searchable" not in detail


# ---------------------------------------------------------------------------
# A non-indexing failure must not be dressed up as one
# ---------------------------------------------------------------------------


def test_a_repository_failure_is_not_reported_as_a_reindex_failure(monkeypatch):
    """Only `KnowledgeBaseError` means "the re-index failed". A commit failure
    arrives as something else and must not borrow the 503's searchability
    story, which would be a guess about a path it never reached."""
    resp = _put(monkeypatch, RuntimeError("database gone"))

    assert resp.status_code == 500
    assert "searchable" not in resp.json().get("detail", "").lower()
