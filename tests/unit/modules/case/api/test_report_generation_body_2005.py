"""``POST /cases/{case_id}/reports`` declares its body, so a malformed one is a 422 (#2005).

The route took ``Dict[str, Any] = Body(...)`` and built ``ReportGenerationRequest``
inside its ``try``, so a missing ``report_types``, an unknown type, a bare string
or an empty list was a 500 with an ERROR traceback, and the contract carried no
body shape.

The body is now the route's declared model, so FastAPI validates it. That moves
the 422 AHEAD of the route's case gate (``get_case(..., driver_only=True)``),
which is in the body. Measured on the pinned FastAPI: body validation errors are
collected while dependencies are solved, and a dependency that raises
(``require_authentication``) stops the solve first. So:

* an anonymous caller with a malformed body still gets 401, never 422;
* an authenticated caller gets the 422 for a malformed body whether or not the
  case exists and whether or not they can read it, so the 422 tells a party who
  cannot see the case nothing about it.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router as case_router
from tests.utils import uncomposed_auth_service

pytestmark = [pytest.mark.unit, pytest.mark.security]

EXISTING = "case_aaaabbbbcccc"
MISSING = "case_000000000000"
DRIVER = "user_driver"
STRANGER = "user_stranger"

VALID = {"report_types": ["resolution_summary"]}
MALFORMED = {
    "missing report_types": {},
    "unknown type": {"report_types": ["not_a_report"]},
    "empty list": {"report_types": []},
    "more than three": {
        "report_types": [
            "resolution_summary",
            "closure_summary",
            "runbook",
            "resolution_summary",
        ]
    },
    "bare string": {"report_types": "resolution_summary"},
    "wrong json type": [],
}


def _case_service() -> MagicMock:
    """Only ``DRIVER`` reads ``EXISTING``; everyone else gets ``None``."""
    service = MagicMock()

    async def get_case(case_id, user_id=None, *, driver_only=False, creator_only=False):
        if case_id != EXISTING or user_id != DRIVER:
            return None
        return SimpleNamespace(case_id=EXISTING, is_terminal=True)

    service.get_case = AsyncMock(side_effect=get_case)
    return service


def _app(user_id: str | None, service: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: service
    app.state.auth_service = uncomposed_auth_service()
    if user_id is not None:
        app.dependency_overrides[require_authentication] = lambda: SimpleNamespace(
            user_id=user_id, email="u@example.com", enterprise_id="e"
        )
    generation = MagicMock()
    generation.generate_reports = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda: {"reports": []})
    )
    app.state.report_generation_service = generation
    return app


def _post(client: TestClient, case_id: str, body):
    return client.post(f"/api/v1/cases/{case_id}/reports", json=body)


@pytest.mark.parametrize("label", list(MALFORMED))
def test_a_malformed_body_is_a_422_naming_report_types(label):
    service = _case_service()
    client = TestClient(_app(DRIVER, service), raise_server_exceptions=False)

    response = _post(client, EXISTING, MALFORMED[label])

    assert response.status_code == 422, label
    # A body that is not an object has no field to name: it names the body.
    named = '"body"' if label == "wrong json type" else "report_types"
    assert named in response.text, label
    service.get_case.assert_not_awaited()


def test_a_client_error_is_not_logged_at_error(caplog):
    client = TestClient(_app(DRIVER, _case_service()), raise_server_exceptions=False)

    with caplog.at_level(logging.WARNING):
        response = _post(client, EXISTING, {"report_types": "resolution_summary"})

    assert response.status_code == 422
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize("label", ["valid", *MALFORMED])
def test_a_non_reader_cannot_tell_an_existing_case_from_a_missing_one(label):
    """Both case columns answer identically, for every body."""
    body = VALID if label == "valid" else MALFORMED[label]
    client = TestClient(_app(STRANGER, _case_service()), raise_server_exceptions=False)

    existing = _post(client, EXISTING, body)
    missing = _post(client, MISSING, body)

    assert (existing.status_code, existing.json()) == (
        missing.status_code,
        missing.json(),
    ), label
    assert existing.status_code == (404 if label == "valid" else 422)


@pytest.mark.parametrize("label", list(MALFORMED))
def test_an_anonymous_caller_with_a_malformed_body_is_refused_401_not_422(label):
    service = _case_service()
    client = TestClient(_app(None, service), raise_server_exceptions=False)

    response = _post(client, EXISTING, MALFORMED[label])

    assert response.status_code == 401, label
    service.get_case.assert_not_awaited()


def test_a_valid_body_on_a_terminal_case_still_generates():
    """The control: refusing everything would satisfy every case above."""
    app = _app(DRIVER, _case_service())
    client = TestClient(app, raise_server_exceptions=False)

    response = _post(client, EXISTING, VALID)

    assert response.status_code == 200
    app.state.report_generation_service.generate_reports.assert_awaited_once()
    assert response.json() == {"reports": []}
