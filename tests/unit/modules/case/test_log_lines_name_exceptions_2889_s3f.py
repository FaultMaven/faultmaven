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
included, and the class name is on the site's line. Each of the 17 arms S3f
gave frames (``exc_info``), because each logs an unexpected failure before a
5xx, has its own case, which also asserts the line carries those frames. How each site is driven is
stated on its case, with the answer the caller gets beside it, unchanged.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
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
from faultmaven.exceptions import (
    CircuitBreakerError,
    CommitOutcomeUnknown,
    ServiceException,
)
from faultmaven.infrastructure.concurrency.inflight import CaseDeleteUnavailable
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    TenantTurnCapUnavailable,
)
from faultmaven.modules.case.api import title_generation
from faultmaven.modules.case.api.routes import cases as cases_module
from faultmaven.modules.case.api.routes import conversation as conversation_module
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_creator_service_channel,
    _di_get_session_service_dependency,
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


def _raised(error: BaseException, cause: BaseException) -> BaseException:
    """``error``, raised from ``cause``, with both tracebacks set."""
    try:
        try:
            raise cause
        except BaseException as inner:
            raise error from inner
    except BaseException as outer:
        return outer


def _breaker() -> CircuitBreakerError:
    """A first-party store failure carrying an ``error_code``, which the line
    names as ``CircuitBreakerError[CIRCUIT_OPEN]``."""
    return CircuitBreakerError(f"redis breaker open {MARKER}", "CIRCUIT_OPEN")


def _session_store_down():
    """A session service whose lookup cannot answer."""
    failure = ServiceException(f"session store at redis://:{MARKER}@cache")
    return SimpleNamespace(get_session=AsyncMock(side_effect=failure))


async def _call_case_route(
    case_service, method: str, path: str, *, session_service=None, **kwargs
) -> httpx.Response:
    """The mounted case router, the app's exception handlers, the caller
    authenticated as the case's owner, and ``case_service`` (and
    ``session_service``) injected."""
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.include_router(case_router, prefix="/api/v1")
    app.dependency_overrides[require_authentication] = lambda: _user(OWNER)
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[_di_get_session_service_dependency] = lambda: (
        session_service
    )
    app.dependency_overrides[_di_get_creator_service_channel] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(method, f"/api/v1{path}", **kwargs)


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
    service.hard_delete_case = AsyncMock(
        side_effect=_raised(CaseDeleteUnavailable(), _breaker())
    )

    answer = await _call_case_route(service, "DELETE", f"/cases/{CASE_ID}")

    assert answer.status_code == 503
    assert answer.headers["x-error-code"] == "CASE_DELETE_UNAVAILABLE"
    refused = _named(
        lines(),
        "Refused delete of case",
        f"{CASE_ID}: CircuitBreakerError[CIRCUIT_OPEN]",
    )
    assert "CaseDeleteUnavailable:" in refused["frames"]
    assert "CircuitBreakerError:" in refused["frames"]


async def test_a_failed_delete_is_named_by_class_and_code_with_its_frames(lines):
    """``DELETE /cases/{id}`` with a service whose delete raises a first-party
    store failure: the catch-all's static 500, and a line naming the class
    with its ``error_code`` and carrying its frames."""
    service = MagicMock()
    service.hard_delete_case = AsyncMock(side_effect=_breaker())

    answer = await _call_case_route(service, "DELETE", f"/cases/{CASE_ID}")

    assert answer.status_code == 500
    assert answer.json()["detail"]["error"]["code"] == "DELETE_CASE_ERROR"
    failed = _named(
        lines(),
        "Unexpected error in delete_case",
        f"{CASE_ID}: CircuitBreakerError[CIRCUIT_OPEN]",
    )
    assert failed["frames"].startswith("CircuitBreakerError:")


async def test_an_unevaluable_session_gate_on_create_carries_its_frames(lines):
    """``POST /cases`` naming a session, with a session service that cannot
    answer: the 503, and the line with its frames."""
    answer = await _call_case_route(
        MagicMock(),
        "POST",
        "/cases",
        session_service=_session_store_down(),
        json={"title": "Checkout API 502s", "session_id": "s-1"},
    )

    assert answer.status_code == 503
    refused = _named(
        lines(), "Cannot evaluate session ownership for s-1", ": ServiceException"
    )
    assert refused["frames"].startswith("ServiceException:")


async def test_a_failed_case_list_is_named_by_class_with_its_frames(lines):
    """``GET /cases`` with a service whose list raises: the catch-all's 500,
    and the line with its frames."""
    service = MagicMock()
    service.list_user_cases = AsyncMock(side_effect=RuntimeError(f"db {MARKER}"))

    answer = await _call_case_route(service, "GET", "/cases")

    assert answer.status_code == 500
    failed = _named(
        lines(), "Unexpected error in list_cases", "list_cases: RuntimeError"
    )
    assert failed["frames"].startswith("RuntimeError:")


async def test_a_failed_case_ui_read_is_named_by_class_with_its_frames(lines):
    """``GET /cases/{id}/ui`` with a service whose lookup raises: the 500, and
    the line with its frames."""
    service = MagicMock()
    service.require_case = AsyncMock(side_effect=RuntimeError(f"pool {MARKER}"))

    answer = await _call_case_route(service, "GET", f"/cases/{CASE_ID}/ui")

    assert answer.status_code == 500
    failed = _named(
        lines(), "Unexpected error in get_case_ui", "get_case_ui: RuntimeError"
    )
    assert failed["frames"].startswith("RuntimeError:")


async def test_a_failed_title_request_is_named_by_class_with_its_frames(lines):
    """``POST /cases/{id}/title`` with the shared generate-and-persist step
    (stubbed) raising: the 500, and the line with its frames."""
    service = MagicMock()
    service.require_case = AsyncMock(return_value=_case())

    with patch.object(
        cases_module,
        "_generate_and_persist_title",
        AsyncMock(side_effect=RuntimeError(f"title store {MARKER}")),
    ):
        answer = await _call_case_route(service, "POST", f"/cases/{CASE_ID}/title")

    assert answer.status_code == 500
    failed = _named(
        lines(),
        "Unexpected error in generate_case_title",
        "generate_case_title: RuntimeError",
    )
    assert failed["frames"].startswith("RuntimeError:")


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


async def test_an_unevaluable_session_gate_on_resume_carries_its_frames(lines):
    """``POST /cases/sessions/{sid}/resume/{id}`` with the case readable and a
    session service that cannot answer: the 503, and the line with frames."""
    service = MagicMock()
    service.require_case = AsyncMock(return_value=_case())
    service.get_case = AsyncMock(return_value=_case())

    answer = await _call_case_route(
        service,
        "POST",
        f"/cases/sessions/s-1/resume/{CASE_ID}",
        session_service=_session_store_down(),
    )

    assert answer.status_code == 503
    refused = _named(
        lines(), "Cannot evaluate session ownership for s-1", ": ServiceException"
    )
    assert refused["frames"].startswith("ServiceException:")


def _resuming_service(**repository_calls) -> CaseService:
    """The real ``CaseService`` over a repository holding the owner's case (with
    ``repository_calls`` replacing its methods) and a session store, for
    ``resume_case_in_session`` and ``link_session_to_case`` to run as written."""
    repository = MagicMock()
    repository.get = AsyncMock(return_value=_case())
    repository.update_activity_timestamp = AsyncMock()
    for name, call in repository_calls.items():
        setattr(repository, name, call)
    return CaseService(case_repository=repository, session_store=AsyncMock())


async def _resume(service: CaseService) -> httpx.Response:
    """``POST /cases/sessions/s-1/resume/{id}`` as the case's owner, whose
    session it is."""
    sessions = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(user_id=OWNER))
    )
    return await _call_case_route(
        service,
        "POST",
        f"/cases/sessions/s-1/resume/{CASE_ID}",
        session_service=sessions,
    )


async def test_a_session_store_refusing_the_link_carries_its_frames(lines):
    """The resume route over the real service, whose session store refuses the
    pointer write: the route's 500, and the store failure's line with frames."""
    service = _resuming_service()
    service.session_store.set = AsyncMock(
        side_effect=ConnectionError(f"redis said {MARKER}")
    )

    answer = await _resume(service)

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Failed to resume case"
    failed = _named(lines(), "Failed to update session store", "store: ConnectionError")
    assert failed["frames"].startswith("ConnectionError:")


async def test_a_failed_link_carries_its_frames(lines):
    """The resume route over the real service, whose repository's activity
    write raises: the route's 500, and the link's line with frames."""
    service = _resuming_service(
        update_activity_timestamp=AsyncMock(side_effect=RuntimeError(f"db {MARKER}"))
    )

    answer = await _resume(service)

    assert answer.status_code == 500
    failed = _named(lines(), "Failed to link session to case", "case: RuntimeError")
    assert failed["frames"].startswith("RuntimeError:")


async def test_a_failed_resume_carries_its_frames(lines):
    """The resume route over the real service, with its link step (stubbed)
    raising past its own handler: the route's 500, and the resume's line with
    frames."""
    service = _resuming_service()
    service.link_session_to_case = AsyncMock(side_effect=RuntimeError(f"link {MARKER}"))

    answer = await _resume(service)

    assert answer.status_code == 500
    failed = _named(
        lines(), f"Failed to resume case {CASE_ID} in session s-1", ": RuntimeError"
    )
    assert failed["frames"].startswith("RuntimeError:")


async def test_an_unclassified_turn_failure_carries_its_frames(lines):
    """``POST /cases/{id}/turns`` with the preparation raising the
    ``ServiceException`` the service wraps an unclassified failure in: the 500
    ``SERVICE_ERROR``, and the line naming the chain with its frames."""
    failure = _raised(
        ServiceException(f"turn failed: {MARKER}"), KeyError(f"slot {MARKER}")
    )
    service = _stubbed_service(AsyncMock(side_effect=failure))

    answer = await _post_turn(service)

    assert answer.status_code == 500
    assert answer.headers["x-error-code"] == "SERVICE_ERROR"
    failed = _named(
        lines(), "Turn processing failed", "failed: ServiceException <- KeyError"
    )
    assert failed["frames"].startswith("ServiceException:")
    assert "KeyError:" in failed["frames"]


async def test_an_unanswered_commit_carries_its_frames(lines):
    """``POST /cases/{id}/turns`` whose commit raises ``CommitOutcomeUnknown``:
    the 500 ``TURN_OUTCOME_UNKNOWN``, and the line with its frames."""
    service = _stubbed_service(AsyncMock(return_value=MagicMock()))
    service.commit_turn = AsyncMock(
        side_effect=_raised(
            CommitOutcomeUnknown(f"COMMIT unanswered {MARKER}"),
            ConnectionResetError(f"peer reset {MARKER}"),
        )
    )

    answer = await _post_turn(service)

    assert answer.status_code == 500
    assert answer.headers["x-error-code"] == "TURN_OUTCOME_UNKNOWN"
    failed = _named(
        lines(),
        "outcome unknown",
        "unknown: CommitOutcomeUnknown <- ConnectionResetError",
    )
    assert failed["frames"].startswith("CommitOutcomeUnknown:")


# ── modules/case/api/title_generation.py ──────────────────────────────────


async def _persist_title(monkeypatch, failure: BaseException):
    """``_generate_and_persist_title`` called with its generation stubbed (a
    titleable case: a statement and a non-empty substance, #3135; an LLM title
    that passes the disclosure gate) and a case service whose title write
    raises ``failure``."""

    async def _title(*_args, **_kwargs):
        return "Checkout API returns 502s", "llm"

    async def _passes(*_args, **_kwargs):
        return True

    async def _redaction(*_args, **_kwargs):
        return SimpleNamespace(save=AsyncMock())

    monkeypatch.setattr(title_generation, "_has_problem_statement", lambda *a: True)
    monkeypatch.setattr(
        title_generation, "_titleable_substance", lambda *a: "Checkout API 502s"
    )
    monkeypatch.setattr(title_generation, "_generate_title_with_llm", _title)
    monkeypatch.setattr(title_generation, "_passes_disclosure_gate", _passes)
    monkeypatch.setattr(title_generation, "load_case_redaction", _redaction)
    service = MagicMock()
    service.get_case_conversation_context = AsyncMock(return_value="")
    service.update_case = AsyncMock(side_effect=failure)
    with pytest.raises(HTTPException) as refused:
        await title_generation._generate_and_persist_title(
            case=_case(),
            case_id=CASE_ID,
            user_id=OWNER,
            case_service=service,
            llm_provider=None,
            max_words=8,
            hint=None,
            sanitizer=None,
            redis_client=None,
        )
    assert refused.value.status_code == 500


async def test_a_service_failure_persisting_a_title_carries_its_frames(
    lines, monkeypatch
):
    await _persist_title(monkeypatch, ServiceException(f"db {MARKER}"))
    failed = _named(
        lines(), "Service error persisting generated title", ": ServiceException"
    )
    assert failed["frames"].startswith("ServiceException:")


async def test_an_unexpected_failure_persisting_a_title_carries_its_frames(
    lines, monkeypatch
):
    await _persist_title(monkeypatch, RuntimeError(f"db {MARKER}"))
    failed = _named(
        lines(), "Unexpected error persisting generated title", ": RuntimeError"
    )
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
