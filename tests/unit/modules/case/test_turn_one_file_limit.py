"""One file per turn on the unified ``POST /cases/{case_id}/turns`` endpoint.

``files`` is declared as a list because that is how multipart repeats a field,
but the supported contract is a single file per turn — the clarification
emitter only ever clarifies the first ``classification_failed`` attachment, so
a second file's failed classification would be unrecoverable. fm#694 makes the
contract a server-side rule instead of a client-side convention.

The limit is on ``files`` alone: ``pasted_content`` legitimately rides
alongside a file as a *second* attachment, and that combination is a shipped
path — ``test_single_file_plus_pasted_content_is_accepted`` pins it.

These tests drive the real route through ``TestClient`` (multipart form data
needs the sync client) with the app's real exception handlers registered, so
the asserted 422 body is the one a client actually receives.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
)
from faultmaven.models.api_models import TurnResponse
from faultmaven.modules.case.api.routes import (
    _di_get_case_service_dependency,
    get_investigation_service,
    require_authentication,
)
from faultmaven.modules.case.api.routes import router as case_router
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models import Case

TURNS_URL = "/api/v1/cases/case_abc123def456/turns"


def _make_case() -> Case:
    # A real (non-placeholder) title, so the inline auto-titling pass returns
    # immediately instead of reaching for an LLM provider.
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        title="Checkout latency spike",
        description="p99 latency tripled after the 14:02 deploy",
        user_id="test-user-123",
        organization_id="org_test123",
        state=CaseState.INQUIRY,
        current_turn=0,
    )


def _make_turn_response() -> TurnResponse:
    return TurnResponse(
        agent_response="Looking at that now.",
        turn_number=1,
        milestones_completed=[],
        case_state=CaseState.INQUIRY,
        progress_made=False,
        attachments_processed=[],
    )


def _client() -> tuple[TestClient, AsyncMock]:
    """A client wired to a turn that actually succeeds.

    Returns the client and the ``process_turn`` mock, so a test can assert on
    the payload the route built as well as on the status code.
    """
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")

    # The app's real handlers — an HTTPException raised in the route renders
    # through `http_exception_handler` in production, and the shape of the 422
    # body is part of what these tests pin.
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)

    user = MagicMock()
    user.user_id = "test-user-123"

    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=_make_case())

    process_turn = AsyncMock(return_value=_make_turn_response())
    investigation_service = MagicMock()
    investigation_service.process_turn = process_turn

    app.dependency_overrides[require_authentication] = lambda: user
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: investigation_service

    return TestClient(app, raise_server_exceptions=False), process_turn


@pytest.mark.unit
class TestTurnOneFileLimit:
    """The `files` field accepts at most one item."""

    def test_query_only_turn_is_accepted(self):
        """Zero files: the guard must not fire on a text-only turn."""
        client, process_turn = _client()
        response = client.post(TURNS_URL, data={"query": "why is checkout slow?"})

        assert response.status_code == 200, response.text
        assert process_turn.await_count == 1

    def test_single_file_is_accepted(self):
        """One file: the supported shape, and the success pin for this file.

        If the fixture ever stops reaching the route body, this fails loudly
        rather than letting the error assertions below pass vacuously.
        """
        client, process_turn = _client()
        response = client.post(
            TURNS_URL,
            files={"files": ("app.log", b"ERROR connection refused", "text/plain")},
        )

        assert response.status_code == 200, response.text
        payload = process_turn.await_args.kwargs["payload"]
        assert [a.filename for a in payload.attachments] == ["app.log"]

    def test_single_file_plus_pasted_content_is_accepted(self):
        """One file AND a paste: two attachments, one `files` item.

        `pasted_content` is a separate form field, so it must not count
        against the limit — this is the combination the guard is most likely
        to break, and it is a shipped path.
        """
        client, process_turn = _client()
        response = client.post(
            TURNS_URL,
            files={"files": ("app.log", b"ERROR connection refused", "text/plain")},
            data={"pasted_content": "2026-08-28 12:00:01 WARN pool exhausted"},
        )

        assert response.status_code == 200, response.text
        payload = process_turn.await_args.kwargs["payload"]
        assert len(payload.attachments) == 2
        assert payload.attachments[0].filename == "app.log"
        # The paste keeps its own minted name and its paste provenance.
        assert payload.attachments[1].filename.startswith("pasted-content-")
        assert payload.attachments[1].source_metadata["source_type"] == "text_paste"

    @pytest.mark.parametrize("count", [2, 5])
    def test_more_than_one_file_is_rejected_with_422(self, count):
        client, process_turn = _client()
        response = client.post(
            TURNS_URL,
            files=[
                ("files", (f"file{i}.log", b"log line", "text/plain"))
                for i in range(count)
            ],
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "Only one file may be attached per turn" in detail
        assert f"received {count}" in detail
        # The turn is refused outright, not half-processed.
        assert process_turn.await_count == 0

    def test_rejection_names_the_paste_carve_out(self):
        """The message has to say what the limit is NOT, or the obvious client
        workaround is to stop sending `pasted_content` too."""
        client, _ = _client()
        response = client.post(
            TURNS_URL,
            files=[
                ("files", ("a.log", b"x", "text/plain")),
                ("files", ("b.log", b"y", "text/plain")),
            ],
        )

        assert response.status_code == 422, response.text
        assert "pasted_content" in response.json()["detail"]

    def test_rejection_uses_the_route_error_envelope(self):
        """A string `detail`, like this route's other 422s (bad `intent_type`)
        — not FastAPI's `{"detail": [...], "errors": [...]}` field-error shape,
        which clients render differently."""
        client, _ = _client()
        response = client.post(
            TURNS_URL,
            files=[
                ("files", ("a.log", b"x", "text/plain")),
                ("files", ("b.log", b"y", "text/plain")),
            ],
        )

        assert response.status_code == 422, response.text
        body = response.json()
        assert set(body) == {"detail"}
        assert isinstance(body["detail"], str)
        assert response.headers.get("x-correlation-id")
