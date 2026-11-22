"""The case and report areas' log lines name an exception by its class, never
its text (#2889 S3f).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Rule 4, as ruled on #2886: "A line that logs an unexpected failure
before the request is answered with a 5xx carries that exception's frames
(``exc_info``)." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``). The marker is on no rendered line, frames
included, and the class name is on the site's line; a line before a 5xx
answering an unexpected failure also carries the exception's frames. How each
site is driven is stated on its case, with the answer the caller gets beside
it, unchanged.
"""

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_case_repository, get_case_service
from faultmaven.infrastructure.concurrency.inflight import CaseDeleteUnavailable
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    TenantTurnCapUnavailable,
)
from faultmaven.modules.case.api.routes import conversation as conversation_module
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router as case_router
from faultmaven.modules.case.api.turn_idempotency import _answer_from_receipt
from faultmaven.modules.case.domain.owned_models.turn_receipt import TurnReceipt
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.sqlite_case_repository.loading import (
    _load_evidence_for_case,
)
from faultmaven.modules.report.api import routes as report_routes
from tests.case_builders import inquiry_case
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)
from tests.unit.modules.case.test_turn_database_timeout_2280 import (
    _post_turn,
    _stubbed_service,
)
from tests.unit.modules.case.test_turn_receipt_1888 import CASE_ID, OWNER, _user

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _case():
    return inquiry_case(case_id=CASE_ID, user_id=OWNER, enterprise_id="ent_1")


async def _call_case_route(case_service, method: str, path: str) -> httpx.Response:
    """The mounted case router, the app's exception handlers, the caller
    authenticated as the case's owner and ``case_service`` injected."""
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.include_router(case_router, prefix="/api/v1")
    app.dependency_overrides[require_authentication] = lambda: _user(OWNER)
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(method, f"/api/v1{path}")


# ── modules/case/api/routes/cases.py ──────────────────────────────────────


async def test_a_failed_case_read_is_named_by_class_with_its_frames(lines):
    """``GET /cases/{id}`` (``get_case``) with a service whose lookup raises: the
    catch-all's static 500, and a line naming the class with its frames."""
    service = MagicMock()
    service.require_case = AsyncMock(side_effect=RuntimeError(f"pool {MARKER}"))

    answer = await _call_case_route(service, "GET", f"/cases/{CASE_ID}")

    assert answer.status_code == 500
    assert answer.json()["detail"]["error"]["code"] == "GET_CASE_ERROR"
    failed = _named(lines(), "Unexpected error in get_case", "get_case: RuntimeError")
    assert failed["frames"].startswith("RuntimeError:")


async def test_a_refused_delete_carries_the_frames_of_both_links(lines):
    """``DELETE /cases/{id}`` with a service whose delete raises
    ``CaseDeleteUnavailable`` from a store error carrying the marker: the 503,
    and a line naming the cause's class with the frames of both links."""
    service = MagicMock()
    try:
        try:
            raise ConnectionError(f"lease store said {MARKER}")
        except ConnectionError as cause:
            raise CaseDeleteUnavailable() from cause
    except CaseDeleteUnavailable as unavailable:
        service.hard_delete_case = AsyncMock(side_effect=unavailable)

    answer = await _call_case_route(service, "DELETE", f"/cases/{CASE_ID}")

    assert answer.status_code == 503
    assert answer.headers["x-error-code"] == "CASE_DELETE_UNAVAILABLE"
    refused = _named(lines(), "Refused delete of case", f"{CASE_ID}: ConnectionError")
    assert "CaseDeleteUnavailable:" in refused["frames"]
    assert "ConnectionError:" in refused["frames"]


# ── modules/case/api/routes/conversation.py ───────────────────────────────


async def test_an_unavailable_turn_cap_is_named_by_class_with_its_frames(lines):
    """``POST /cases/{id}/turns`` with the turn's preparation raising
    ``TenantTurnCapUnavailable``: the 503, and the line with its frames."""
    failure = TenantTurnCapUnavailable(f"ledger write failed {MARKER}")
    service = _stubbed_service(AsyncMock(side_effect=failure))

    answer = await _post_turn(service)

    assert answer.status_code == 503
    refused = _named(
        lines(),
        "Turn refused: the tenant turn cap",
        "could not be applied: TenantTurnCapUnavailable",
    )
    assert refused["frames"].startswith("TenantTurnCapUnavailable:")


async def test_a_failure_answering_a_committed_turn_is_named_by_class(lines):
    """``POST /cases/{id}/turns`` whose commit succeeds and whose auto-title
    (stubbed) then raises: the 500 ``TURN_OUTCOME_UNKNOWN``, and the line names
    the class where it held the failure's repr, with its frames."""
    service = _stubbed_service(AsyncMock(return_value=MagicMock()))
    service.commit_turn = AsyncMock(return_value=MagicMock())

    with patch.object(
        conversation_module,
        "_auto_title_case_if_default",
        AsyncMock(side_effect=RuntimeError(f"title store {MARKER}")),
    ):
        answer = await _post_turn(service)

    assert answer.status_code == 500
    assert answer.headers["x-error-code"] == "TURN_OUTCOME_UNKNOWN"
    failed = _named(lines(), "committed; answering it failed", "failed: RuntimeError")
    assert failed["frames"].startswith("RuntimeError:")


# ── modules/case/api/turn_idempotency.py ──────────────────────────────────


def test_a_receipt_that_no_longer_validates_is_named_by_class(lines):
    """``_answer_from_receipt`` called with a stored receipt whose response
    fails ``TurnResponse`` validation on a field holding the marker. A
    ``ValidationError`` quotes the input it refused; the line names the class,
    and the 409 is unchanged."""
    receipt = TurnReceipt(
        case_id=CASE_ID,
        author_id=OWNER,
        idempotency_key="k" * 32,
        request_fingerprint="f" * 64,
        turn_number=1,
        response={"agent_response": "ok", "turn_number": MARKER},
    )

    with pytest.raises(HTTPException) as refused:
        _answer_from_receipt(receipt, receipt.request_fingerprint, _case())

    assert refused.value.status_code == 409
    _named(lines(), "no longer validates", "TurnResponse: ValidationError")


# ── modules/case/domain/services/case_service.py ──────────────────────────


async def test_a_message_that_fails_to_convert_is_named_by_class(lines):
    """``CaseService.get_case_messages_enhanced`` with the API ``Message``
    model (patched) raising on the one stored row: the row is skipped and
    counted, and the line names the class once."""
    case = _case()
    case.messages = [
        {
            "message_id": "m-1",
            "turn_number": 1,
            "role": "user",
            "content": "hi",
            "created_at": datetime.now(timezone.utc),
        }
    ]
    service = CaseService(
        case_repository=AsyncMock(get=AsyncMock(return_value=case)),
        session_store=AsyncMock(),
        max_cases_per_user=50,
    )

    with patch(
        "faultmaven.models.api.Message",
        side_effect=ValueError(f"bad row {MARKER}"),
    ):
        page = await service.get_case_messages_enhanced(CASE_ID, include_debug=True)

    assert page.debug_info.message_parsing_errors == 1
    failed = _named(
        lines(), "Failed to convert message", f"m-1 of case {CASE_ID}: ValueError"
    )
    assert failed["event"].endswith(f"{CASE_ID}: ValueError")


# ── modules/case/infrastructure/sqlite_case_repository/loading.py ─────────


async def test_an_evidence_load_that_raises_is_named_by_class(lines):
    """``_load_evidence_for_case`` called with a database stub whose query
    raises: the case keeps the evidence it had, and the line names the class."""
    case = _case()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError(f"no such table {MARKER}"))

    await _load_evidence_for_case(db, case)

    assert case.evidence == []
    _named(
        lines(),
        "Failed to load evidence for case",
        f"{CASE_ID}: RuntimeError",
    )


# ── modules/report/api/routes.py ──────────────────────────────────────────


def test_a_failed_report_read_is_named_by_class_with_its_frames(lines):
    """``GET /reports/{id}`` through ``TestClient`` with a repository whose
    report read raises: the static 500, and the line with its frames."""
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.include_router(report_routes.router)
    repository = MagicMock()
    repository.get_report = AsyncMock(side_effect=RuntimeError(f"db {MARKER}"))
    app.dependency_overrides[require_authentication] = lambda: _user(OWNER)
    app.dependency_overrides[get_case_repository] = lambda: repository
    app.dependency_overrides[get_case_service] = lambda: MagicMock()

    answer = TestClient(app, raise_server_exceptions=False).get("/reports/r-1")

    assert answer.status_code == 500
    assert answer.json() == {"detail": "Failed to retrieve report"}
    failed = _named(lines(), "Error getting report", "r-1: RuntimeError")
    assert failed["frames"].startswith("RuntimeError:")


def test_the_lines_render_at_debug(lines):
    """Positive control for the fixture: a DEBUG record is captured."""
    logging.getLogger("faultmaven.modules.case.tests").debug("control line")
    assert any("control line" in line for line in lines())
