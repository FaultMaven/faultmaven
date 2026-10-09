"""Every terminal-case 409 is labelled ``x-error-code: CASE_TERMINAL`` (#1907).

A resolved or closed case refuses new data, a status change and a file
reclassification on the turn route, and refuses ``PUT /cases/{case_id}``.
Until #1907 each of those 409s went out unlabelled, and clients read "closed"
from the header being absent — an inference any other unlabelled 409 turns into
a false claim about a live case. These tests drive the real routes, with the
app's real exception handlers, and read the header a client receives. The
static half (every 409 emitter is labelled or classified) is
``tests/unit/api/middleware/test_conflict_labelling.py``.

``POST /cases/{case_id}/close`` refuses an already-terminal case through
``ConflictError(error_code=CASE_TERMINAL)``; that leg is pinned in
``test_case_service.py`` (the raiser names the code) and
``test_exception_handlers.py`` (the handler sends it).
"""

import json
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
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.core.investigation.terminal_transitions import execute_user_closure
from faultmaven.exceptions import CASE_TERMINAL
from faultmaven.models.api_models import TurnResponse
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router as case_router
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.case.domain.models.case import Case

pytestmark = pytest.mark.unit

USER_ID = "test-user-123"
CASE_ID = "case_abc123def456"
TURNS_URL = f"/api/v1/cases/{CASE_ID}/turns"
CASE_URL = f"/api/v1/cases/{CASE_ID}"


def _closed_case() -> Case:
    # A real (non-placeholder) title, so the inline auto-titling pass returns
    # immediately instead of reaching for an LLM provider.
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        title="Checkout latency spike",
        description="p99 latency tripled after the 14:02 deploy",
        user_id=USER_ID,
        enterprise_id="org_test123",
        state=CaseState.INQUIRY,
        current_turn=3,
    )
    execute_user_closure(case, USER_ID)
    assert case.is_terminal, "control: the case is closed"
    return case


def _client() -> tuple[TestClient, AsyncMock, MagicMock]:
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )

    user = MagicMock()
    user.user_id = USER_ID

    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=_closed_case())
    case_service.update_case = AsyncMock(return_value=True)

    prepare_turn = AsyncMock(
        return_value=TurnResponse(
            agent_response="It closed on the pool-size fix.",
            turn_number=4,
            milestones_completed=[],
            case_state=CaseState.CLOSED,
            progress_made=False,
            attachments_processed=[],
        )
    )
    investigation_service = MagicMock()
    investigation_service.prepare_turn = prepare_turn
    investigation_service.commit_turn = AsyncMock(
        side_effect=lambda prepared, **_: prepared
    )

    app.dependency_overrides[require_authentication] = lambda: user
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: investigation_service

    return TestClient(app, raise_server_exceptions=False), prepare_turn, case_service


class TestTheTurnRouteLabelsItsTerminalRefusals:
    @pytest.mark.parametrize(
        "data,files",
        [
            pytest.param(
                {"query": "see this", "pasted_content": "ERROR pool exhausted"},
                None,
                id="pasted-content",
            ),
            pytest.param(
                {"query": "see this"},
                {"files": ("app.log", b"ERROR pool exhausted", "text/plain")},
                id="file",
            ),
            pytest.param(
                {
                    "query": "Reopen",
                    "intent_type": "status_transition",
                    "intent_data": json.dumps({"to_state": "investigating"}),
                },
                None,
                id="status-transition",
            ),
            pytest.param(
                {
                    "query": "That was a config file",
                    "intent_type": "file_reclassification",
                    "intent_data": json.dumps(
                        {"file_id": "f1", "data_type": "structured_config"}
                    ),
                },
                None,
                id="file-reclassification",
            ),
        ],
    )
    def test_refusal_is_labelled_case_terminal(self, data, files):
        client, prepare_turn, _ = _client()

        response = client.post(TURNS_URL, data=data, files=files)

        assert response.status_code == 409, response.text
        assert response.headers["x-error-code"] == CASE_TERMINAL
        assert prepare_turn.await_count == 0, "a refused turn ran"

    def test_a_question_on_a_closed_case_is_still_answered(self):
        """Control: the gates refuse mutation, not conversation."""
        client, prepare_turn, _ = _client()

        response = client.post(TURNS_URL, data={"query": "what fixed it?"})

        assert response.status_code == 200, response.text
        assert prepare_turn.await_count == 1


class TestUpdatingATerminalCase:
    def test_put_is_refused_with_case_terminal(self):
        client, _, case_service = _client()

        response = client.put(CASE_URL, json={"title": "New title"})

        assert response.status_code == 409, response.text
        assert response.headers["x-error-code"] == CASE_TERMINAL
        case_service.update_case.assert_not_awaited()


class TestTheContractDocumentsTheLabel:
    """Each route that refuses a terminal case publishes ``CASE_TERMINAL`` in
    its 409's ``x-error-code`` enum, so a generated client can name it."""

    @pytest.mark.parametrize(
        "path,method",
        [
            ("/api/v1/cases/{case_id}/turns", "post"),
            ("/api/v1/cases/{case_id}", "put"),
            ("/api/v1/cases/{case_id}/close", "post"),
        ],
    )
    def test_the_409_enum_names_case_terminal(self, path, method):
        app = FastAPI()
        app.include_router(case_router, prefix="/api/v1")

        operation = app.openapi()["paths"][path][method]
        header = operation["responses"]["409"]["headers"]["x-error-code"]

        assert CASE_TERMINAL in header["schema"]["enum"]
