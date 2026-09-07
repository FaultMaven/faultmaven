"""One file per turn on the unified ``POST /cases/{case_id}/turns`` endpoint.

``files`` is declared as a list because that is how multipart repeats a field,
but the supported contract is a single file per turn: the Copilot sends exactly
one, and multi-file turns are undefined and untested (per-file attachment
results, request-size budget). fm#694 also cited the clarification emitter,
which then clarified only the first ``classification_failed`` attachment —
that is no longer true (it clarifies every failure, #1222) and is no longer
part of why this cap exists.
fm#694 makes the rule server-side instead of a client-side convention, and
does it with ``File(max_length=1)`` rather than a hand-rolled ``len(files)``
check: the framework both refuses the request AND publishes ``maxItems: 1``
into the OpenAPI schema, so the narrowing is visible to clients and to
``scripts/check_contract_version.py``. ``test_openapi_publishes_the_one_file_cap``
pins the schema half; a revert of ``max_length`` trips it as well as the 422s.

The cap is on ``files`` alone: ``pasted_content`` legitimately rides alongside
a file as a *second* attachment, and that combination is a shipped path —
``test_single_file_plus_pasted_content_is_accepted`` pins it.

These tests drive the real route through ``TestClient`` (multipart form data
needs the sync client) with the app's real ``request_validation_exception_handler``
registered, so the asserted 422 body is the one a client actually receives.
That handler is registered EXPLICITLY in ``faultmaven.main`` and is deliberately
not part of ``get_exception_handlers()``, which maps domain exceptions — so a
test app that only installs the latter renders FastAPI's default 422 instead of
the app's normalized envelope.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
    request_validation_exception_handler,
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
        enterprise_id="org_test123",
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


def _app() -> tuple[FastAPI, AsyncMock]:
    """An app wired so a turn actually succeeds, with the real error handlers.

    Returns the app and the ``process_turn`` mock, so a test can assert on the
    payload the route built as well as on the status code.
    """
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")

    # The app's real handlers, registered the way main.py registers them: the
    # domain map, then HTTPException, then RequestValidationError separately.
    # The last one is what renders the 422 this file is about.
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )

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

    return app, process_turn


def _client() -> tuple[TestClient, AsyncMock]:
    app, process_turn = _app()
    return TestClient(app, raise_server_exceptions=False), process_turn


@pytest.mark.unit
class TestTurnOneFileLimit:
    """The `files` field accepts at most one item."""

    def test_query_only_turn_is_accepted(self):
        """Zero files: the cap must not fire on a text-only turn."""
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
        against the cap — this is the combination the cap is most likely to
        break, and it is a shipped path.
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
        # The turn is refused, not half-processed. (It IS fully parsed and
        # spooled first — the cap is correctness, not cost.)
        assert process_turn.await_count == 0

    def test_rejection_uses_the_apps_validation_envelope(self):
        """The normalized envelope from `request_validation_exception_handler`,
        which is what a client sees for every framework validation failure —
        `detail` is the fixed string, the specifics live in `errors`."""
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
        assert body["detail"] == "Validation error"
        errors = body["errors"]
        assert isinstance(errors, list) and errors
        too_long = [e for e in errors if e["type"] == "too_long"]
        assert len(too_long) == 1, errors
        assert too_long[0]["loc"] == ["body", "files"]

    def test_openapi_publishes_the_one_file_cap(self):
        """The narrowing has to be in the machine-readable contract, not only
        in the running code: that is what lets a client see it and what lets
        `scripts/check_contract_version.py` demand a version bump for it."""
        app, _ = _app()
        spec = app.openapi()

        body = spec["paths"]["/api/v1/cases/{case_id}/turns"]["post"]["requestBody"]
        schema = body["content"]["multipart/form-data"]["schema"]
        name = schema["$ref"].rsplit("/", 1)[-1]
        files_schema = spec["components"]["schemas"][name]["properties"]["files"]

        assert files_schema["maxItems"] == 1, files_schema
        assert files_schema["type"] == "array"
        # The human guidance lives in the spec, not in an error string.
        assert "pasted_content" in files_schema["description"]
