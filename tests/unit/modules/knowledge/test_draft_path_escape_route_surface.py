"""A draft row pointing outside the knowledge tree, seen from the API.

Two properties, and they pull against each other, which is why they are pinned
together on the real routes rather than inferred from the service:

* **Typed, not a 500.** ``RunbookPathEscape`` subclasses ``ValueError``, and
  ``get_exception_handlers()`` maps no ``ValueError`` — so before this it fell
  through to Starlette's default and ``PUT /drafts/{id}`` and the verify route
  answered a bare 500. ``verify_draft``'s own docstring records that every
  failure shape on that method is a typed exception precisely so the route can
  translate it.
* **No server path in any response body.** The refusal message an operator
  needs names absolute resolved paths, and ``str()`` of the exception reaches a
  body two ways: the 409 handler's ``detail``, and ``verify_batch``'s blanket
  ``except Exception`` which puts ``str(e)`` into a **200** per-item ``error``.
  Echoing a filesystem layout there is the disclosure #866 closed for this
  module (``api/routes.py:977``).

Measured against the head that lacked this: 500, 500, and a 200 carrying
``/tmp/.../data/escaped/pwned.md`` in the batch body.

The escaping row is seeded directly — the mint points are sanitised, so this is
the pre-#1215 row that can no longer be created through the service, and that
row is the whole reason the guard exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.api]

ESCAPING = "data/knowledge/../escaped/pwned.md"
GOOD = "data/knowledge/global/ok.md"


def _session_factory(job, draft):
    calls = {"n": 0}

    async def _execute(_stmt):
        calls["n"] += 1
        result = MagicMock()
        if calls["n"] == 1:
            result.scalar_one_or_none.return_value = job
        else:
            result.scalar_one_or_none.return_value = draft
            result.scalars.return_value.all.return_value = [draft]
        return result

    session = AsyncMock()
    session.execute = _execute
    session.get = AsyncMock(return_value=None)

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return None

    return _Factory()


def _job():
    j = MagicMock()
    j.id = "conv_x"
    j.user_id = "user_x"
    j.scope = "personal"
    j.status = "completed"
    j.case_id = None
    j.organization_id = "org_x"
    j.analysis_result = None
    j.source_file_id = None
    j.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return j


def _draft(file_path):
    d = MagicMock()
    d.id = "draft_deadbeef"
    d.runbook_id = "rb"
    d.title = "T"
    d.status = "draft"
    d.file_path = file_path
    d.validation_passed = True
    d.validation_errors = []
    d.validation_warnings = []
    d.source_type = "document"
    d.quality_details = {
        "overall": 80,
        "grade": "B",
        "completeness": 80,
        "clarity": 80,
        "actionability": 80,
        "comprehensiveness": 80,
    }
    return d


def _client(file_path):
    """The real conversion router, with the real exception handlers."""
    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.modules.knowledge.api import conversion_routes as cr
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )

    service = ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_session_factory(_job(), _draft(file_path)),
        knowledge_service=None,
    )

    user = MagicMock()
    user.user_id = "user_x"
    user.username = "alice"
    user.is_platform_admin = lambda: False

    app = FastAPI()
    app.include_router(cr.router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[cr._get_conversion_service] = lambda: service
    app.dependency_overrides[cr._require_auth] = lambda: user
    # ``raise_server_exceptions=False`` so an unmapped exception is observable
    # as a 500 rather than re-raised into the test.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def escaping_tree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "knowledge").mkdir(parents=True)
    target = tmp_path / "data" / "escaped" / "pwned.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nstatus: draft\n---\n\n# Not ours\n", encoding="utf-8")
    return tmp_path


def _assert_no_path_leak(body: str, tmp_path) -> None:
    assert str(tmp_path) not in body
    assert "data/escaped" not in body
    assert "knowledge tree:" not in body, "the raw helper message reached the body"


class TestTheRouteAnswersTypedNotFiveHundred:
    def test_update_draft_is_a_conflict(self, escaping_tree):
        response = _client(ESCAPING).put(
            "/knowledge/conversions/conv_x/drafts/draft_deadbeef",
            json={"content": "# x\n" + "y" * 200},
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["conflict_reason"] == "path_outside_knowledge_tree"
        assert body["resource_id"] == "draft_deadbeef"
        _assert_no_path_leak(response.text, escaping_tree)

    def test_verify_draft_is_a_conflict(self, escaping_tree):
        response = _client(ESCAPING).post(
            "/knowledge/conversions/conv_x/drafts/draft_deadbeef/verify"
        )
        assert response.status_code == 409, response.text
        assert response.json()["conflict_reason"] == "path_outside_knowledge_tree"
        _assert_no_path_leak(response.text, escaping_tree)

    def test_verify_batch_reports_failure_without_the_path(self, escaping_tree):
        """The 200 arm. ``verify_batch`` catches everything and puts ``str(e)``
        into the per-item ``error`` — so the exception's message is the whole
        control, and it must carry no filesystem layout."""
        response = _client(ESCAPING).post(
            "/knowledge/drafts/verify-batch",
            json={
                "draft_ids": [{"conversion_id": "conv_x", "draft_id": "draft_deadbeef"}]
            },
        )
        assert response.status_code == 200, response.text
        item = response.json()["results"][0]
        assert item["status"] == "failed"
        assert "draft_deadbeef" in item["error"]
        _assert_no_path_leak(response.text, escaping_tree)


class TestAContainedDraftStillWorks:
    """Without this the class above passes on any fixture that simply breaks
    every route."""

    def test_update_draft_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        good = tmp_path / "data" / "knowledge" / "global" / "ok.md"
        good.parent.mkdir(parents=True)
        good.write_text("# before\n", encoding="utf-8")

        response = _client(GOOD).put(
            "/knowledge/conversions/conv_x/drafts/draft_deadbeef",
            json={"content": "# after\n" + "y" * 200},
        )
        assert response.status_code == 200, response.text
        assert good.read_text(encoding="utf-8").startswith("# after")
