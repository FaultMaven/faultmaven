"""A case route says what it refused (#2806).

- A case the caller cannot read (absent, deleted, outside their read scope)
  answers 404 ``x-error-code: CASE_NOT_FOUND`` on every case route.
- An investigation write by a caller who READS the case but does not drive it
  answers 403 ``x-error-code: CASE_NOT_DRIVER``. The driver-only routes used to
  answer that reader with the absent case's 404, which a client could not tell
  from a deletion.
- A 404 for something INSIDE a case (a file, evidence, a report, a team the
  case is not shared with) carries no ``x-error-code``.
- A repository failure during the case lookup is never the case's 404.

Driven through the real case router, the app's exception handlers and the real
``CaseService`` over the in-memory repository, in the ADR-020 world of
``test_case_driver_1898``: CREATOR opened the case, DRIVER drives it, TEAMMATE
reads it through team ``t1``, OUTSIDER reads nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import (
    get_case_repository,
    get_investigation_service,
)
from faultmaven.exceptions import (
    CASE_NOT_DRIVER,
    CASE_NOT_FOUND,
    CaseNotDriverError,
    NotFoundError,
)
from faultmaven.modules.agent.domain.services.investigation_service.reclassification import (
    EvidenceReclassifier,
)
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router as case_router
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.unit.modules.case.test_case_driver_1898 import (
    CASE_ID,
    CREATOR,
    DRIVER,
    OUTSIDER,
    TEAMMATE,
    _Accounts,
    _case,
    _Shares,
    _Teams,
    _tenant,  # noqa: F401 - autouse fixture: binds the enterprise
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

MISSING = "case_00000000dead"

#: Every route that reads a case: (method, path template, request body).
READ_ROUTES = [
    ("GET", "/cases/{id}", None),
    ("GET", "/cases/{id}/ui", None),
    ("GET", "/cases/{id}/analytics", None),
    ("GET", "/cases/{id}/messages", None),
    ("GET", "/cases/{id}/data", None),
    ("GET", "/cases/{id}/data/d1", None),
    ("GET", "/cases/{id}/report-recommendations", None),
    ("GET", "/cases/{id}/reports", None),
    ("GET", "/cases/{id}/reports/r1/download", None),
    ("GET", "/cases/{id}/uploaded-files", None),
    ("GET", "/cases/{id}/uploaded-files/f1", None),
    ("GET", "/cases/{id}/evidence", None),
    ("GET", "/cases/{id}/evidence/e1", None),
]

#: Every write that is the case's DRIVER's (ADR-020 D2) and is refused at the
#: route. The turn's driver refusal is the service's, pinned below.
DRIVER_ROUTES = [
    ("PUT", "/cases/{id}", {"title": "Renamed"}),
    ("POST", "/cases/{id}/title", None),
    ("POST", "/cases/sessions/s1/resume/{id}", None),
    ("DELETE", "/cases/{id}/data/d1", None),
    ("POST", "/cases/{id}/reports", {"report_types": ["closure_summary"]}),
    ("POST", "/cases/{id}/close", None),
]

#: Writes the case's CREATOR governs: a reader is refused 403, unlabelled.
CREATOR_ROUTES = [
    ("POST", "/cases/{id}/team-shares", {"team_id": "t1"}),
    ("DELETE", "/cases/{id}/team-shares/t1", None),
    ("GET", "/cases/{id}/driver-candidates", None),
    ("PUT", "/cases/{id}/driver", {"driver_id": CREATOR}),
]


class _Repository(InMemoryCaseRepository):
    """The in-memory repository, and one that can be made to fail."""

    fail_reads = False

    async def get(self, case_id):
        if self.fail_reads:
            raise ConnectionError("database unreachable")
        return await super().get(case_id)


@pytest.fixture
async def world():
    repository = _Repository()
    service = CaseService(
        repository,
        team_service=_Teams({"t1": {CREATOR, DRIVER, TEAMMATE}}),
        share_repository=_Shares({CASE_ID: {"t1"}}, []),
        account_reader=_Accounts(),
    )
    await _case(repository, driver_id=DRIVER)
    caller = SimpleNamespace(user_id=DRIVER)

    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.include_router(case_router, prefix="/api/v1")

    async def _session_service():
        async def get_session(session_id, validate=True):
            return SimpleNamespace(user_id=caller.user_id)

        return SimpleNamespace(get_session=get_session)

    async def _user():
        return SimpleNamespace(
            user_id=caller.user_id,
            enterprise_id="ent_driver_0001",
            roles=["member"],
        )

    app.dependency_overrides[_di_get_case_service_dependency] = lambda: service
    app.dependency_overrides[_di_get_session_service_dependency] = _session_service
    app.dependency_overrides[get_case_repository] = lambda: repository
    # The turn route's case lookup runs before the service is reached.
    app.dependency_overrides[get_investigation_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_current_user_optional] = _user
    app.dependency_overrides[require_authentication] = _user

    async def call(method, template, body=None, *, as_user, case_id=CASE_ID):
        caller.user_id = as_user
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                "/api/v1" + template.format(id=case_id),
                json=body,
            )

    return SimpleNamespace(call=call, repository=repository, service=service)


def _label(answer: httpx.Response):
    return answer.headers.get("x-error-code")


# ============================================================
# CASE_NOT_FOUND: one answer for a case the caller cannot read
# ============================================================


@pytest.mark.parametrize(
    "method,template,body",
    READ_ROUTES + DRIVER_ROUTES + CREATOR_ROUTES,
    ids=lambda v: v if isinstance(v, str) else None,
)
@pytest.mark.parametrize("case_id,as_user", [(MISSING, DRIVER), (CASE_ID, OUTSIDER)])
async def test_a_case_the_caller_cannot_read_is_404_case_not_found(
    world, method, template, body, case_id, as_user
):
    """Absent and unreadable are one answer, so it discloses nothing."""
    answer = await world.call(method, template, body, as_user=as_user, case_id=case_id)

    assert answer.status_code == 404, answer.text
    assert _label(answer) == CASE_NOT_FOUND


async def test_the_turn_route_labels_a_case_the_caller_cannot_read(world):
    answer = await world.call("POST", "/cases/{id}/turns", None, as_user=OUTSIDER)

    assert answer.status_code == 404, answer.text
    assert _label(answer) == CASE_NOT_FOUND


# ============================================================
# CASE_NOT_DRIVER: a reader who does not drive
# ============================================================


@pytest.mark.parametrize(
    "method,template,body",
    DRIVER_ROUTES,
    ids=lambda v: v if isinstance(v, str) else None,
)
@pytest.mark.parametrize("as_user", [TEAMMATE, CREATOR])
async def test_a_reader_who_does_not_drive_is_403_case_not_driver(
    world, method, template, body, as_user
):
    """The creator included: while DRIVER drives, the creator only reads."""
    answer = await world.call(method, template, body, as_user=as_user)

    assert answer.status_code == 403, answer.text
    assert _label(answer) == CASE_NOT_DRIVER


async def test_a_readers_put_on_a_terminal_case_is_refused_for_who_asked(world):
    """The driver gate runs before the terminal check: the reader is refused
    for who asked, never told the case's state (409 CASE_TERMINAL)."""
    from faultmaven.modules.case.contracts import CaseState

    await _case(world.repository, driver_id=DRIVER, state=CaseState.CLOSED)

    answer = await world.call(
        "PUT", "/cases/{id}", {"title": "Renamed"}, as_user=TEAMMATE
    )

    assert answer.status_code == 403
    assert _label(answer) == CASE_NOT_DRIVER


async def test_the_driver_is_not_refused(world):
    answer = await world.call(
        "PUT", "/cases/{id}", {"title": "Renamed"}, as_user=DRIVER
    )

    assert answer.status_code == 200, answer.text
    assert _label(answer) is None


@pytest.mark.parametrize(
    "method,template,body",
    CREATOR_ROUTES,
    ids=lambda v: v if isinstance(v, str) else None,
)
async def test_a_creator_write_refuses_a_reader_without_the_driver_label(
    world, method, template, body
):
    """Governance is the creator's (and for the driver routes, the driver's):
    TEAMMATE reads the case and is neither, so 403, not CASE_NOT_DRIVER."""
    answer = await world.call(method, template, body, as_user=TEAMMATE)

    assert answer.status_code == 403, answer.text
    assert _label(answer) != CASE_NOT_DRIVER


# ============================================================
# A 404 inside a case is not the case's
# ============================================================


@pytest.mark.parametrize(
    "method,template",
    [
        ("GET", "/cases/{id}/uploaded-files/f-missing"),
        ("GET", "/cases/{id}/evidence/e-missing"),
        ("GET", "/cases/{id}/reports/r-missing/download"),
    ],
)
async def test_a_missing_thing_inside_a_readable_case_is_unlabelled(
    world, method, template
):
    answer = await world.call(method, template, as_user=TEAMMATE)

    assert answer.status_code == 404, answer.text
    assert _label(answer) is None


async def test_an_unshare_from_a_team_the_case_is_not_shared_with_is_unlabelled(
    world,
):
    answer = await world.call(
        "DELETE", "/cases/{id}/team-shares/t-other", as_user=CREATOR
    )

    assert answer.status_code == 404, answer.text
    assert _label(answer) is None


# ============================================================
# A repository failure is not an absence
# ============================================================


@pytest.mark.parametrize(
    "method,template,body",
    [
        ("GET", "/cases/{id}", None),
        ("PUT", "/cases/{id}", {"title": "Renamed"}),
        # Its "never fail a list" fallback answered the failure 200 [].
        ("GET", "/cases/{id}/data", None),
    ],
)
async def test_a_failed_lookup_is_never_the_cases_404(world, method, template, body):
    """``get_case`` answered None for a repository failure (until #2996); a
    gate on it said the case was gone, and a client closes a case on
    CASE_NOT_FOUND."""
    world.repository.fail_reads = True

    answer = await world.call(method, template, body, as_user=DRIVER)

    assert answer.status_code >= 500, answer.text
    assert _label(answer) != CASE_NOT_FOUND


class _Unreachable:
    """A team or share lookup whose store is down."""

    def __init__(self, *names):
        for name in names:
            setattr(self, name, self._fail)

    async def _fail(self, *args, **kwargs):
        raise ConnectionError("share store unreachable")


@pytest.mark.parametrize("store", ["share_repository", "team_service"])
@pytest.mark.parametrize(
    "method,template,body,as_user",
    [
        # TEAMMATE and DRIVER both read the case only through the team share.
        ("GET", "/cases/{id}", None, TEAMMATE),
        ("PUT", "/cases/{id}", {"title": "Renamed"}, DRIVER),
        ("GET", "/cases/{id}/driver-candidates", None, DRIVER),
    ],
    ids=["read", "driver-write", "handoff"],
)
async def test_a_failed_share_lookup_is_never_a_refusal(
    world, store, method, template, body, as_user
):
    """The listing collapses a failed share arm to owner-only, which only
    shows less. On a gate that empty arm told a teammate the case was gone
    and the driver they do not drive it, both labels a client acts on."""
    unreachable = _Unreachable("list_resource_ids", "list_all_user_team_ids")
    setattr(world.service, store, unreachable)

    answer = await world.call(method, template, body, as_user=as_user)

    assert answer.status_code >= 500, answer.text
    assert _label(answer) not in (CASE_NOT_FOUND, CASE_NOT_DRIVER)


async def test_a_failed_share_lookup_still_lets_the_creator_in(world):
    """The creator reads through the owner arm, which never asks the store."""
    world.service.share_repository = _Unreachable("list_resource_ids")

    assert (await world.service.require_case(CASE_ID, CREATOR)).case_id == CASE_ID


@pytest.mark.parametrize(
    "verdict",
    [CaseNotDriverError(), NotFoundError("Case", CASE_ID)],
    ids=["handed-off", "deleted"],
)
async def test_a_title_write_that_loses_the_race_keeps_its_verdict(verdict):
    """``update_case`` raises when the case is handed off or deleted while the
    title is generated. The persist step wrapped that in a 500 after the model
    spend; it reaches the global handlers, which label it."""
    from tests.unit.api.test_title_generation_gate import (
        _conversation,
        _generate,
        _make_case,
        _service_for,
        mock_request,
        mock_response,
        mock_user,
    )

    case = _make_case()
    case.inquiry.proposed_problem_statement = (
        "Checkout API returns 502 for 30% of requests since the v2.1.3 deploy"
    )
    service = _service_for(case, _conversation(user_turns=1))
    service.update_case.side_effect = verdict

    with pytest.raises(type(verdict)):
        await _generate(
            service,
            mock_request.__wrapped__(),
            mock_response.__wrapped__(),
            mock_user.__wrapped__(),
            case.case_id,
        )
    service.update_case.assert_awaited_once()


# ============================================================
# The services' own verdicts
# ============================================================


async def test_require_case_tells_unreadable_from_not_driven(world):
    require = world.service.require_case

    assert (await require(CASE_ID, DRIVER, driver_only=True)).case_id == CASE_ID
    assert (await require(CASE_ID, TEAMMATE)).case_id == CASE_ID
    with pytest.raises(CaseNotDriverError):
        await require(CASE_ID, TEAMMATE, driver_only=True)
    for case_id, user in [(MISSING, DRIVER), (CASE_ID, OUTSIDER)]:
        with pytest.raises(NotFoundError) as absent:
            await require(case_id, user, driver_only=True)
        assert absent.value.resource_type == "Case"


async def test_a_turn_by_a_reader_who_does_not_drive_is_case_not_driver(world):
    """The turn's driver gate is the service's, after the receipt replay."""
    from faultmaven.modules.agent.domain.services.investigation_service.service import (
        InvestigationService,
    )

    case = await world.repository.get(CASE_ID)
    gate = InvestigationService._verify_access_and_reserve

    with pytest.raises(CaseNotDriverError):
        await gate(
            SimpleNamespace(),
            case=case,
            case_id=CASE_ID,
            payload=None,
            user_id=TEAMMATE,
        )


def test_reclassifying_needs_the_read_then_the_wheel():
    """A non-reader was told 403, which confirmed a case hidden from them."""
    case = SimpleNamespace(case_id=CASE_ID, effective_driver_id=DRIVER, evidence=[])
    gate = EvidenceReclassifier._require_reclassifiable

    with pytest.raises(NotFoundError) as absent:
        gate(case, "e1", DRIVER, reads=False)
    assert absent.value.resource_type == "Case"
    with pytest.raises(CaseNotDriverError):
        gate(case, "e1", TEAMMATE, reads=True)
    with pytest.raises(NotFoundError) as evidence:
        gate(case, "e1", DRIVER, reads=True)
    assert evidence.value.resource_type == "Evidence"


async def test_a_driver_change_while_an_update_retries_is_case_not_driver(world):
    """The race backstop answers what happened, not an absence."""
    original = world.repository.get
    loads = []

    async def handed_off_after_the_gate(case_id):
        # The gate's load sees DRIVER drive; the retry's fresh load sees the
        # case handed to TEAMMATE in between.
        loads.append(case_id)
        case = await original(case_id)
        if len(loads) == 1 or case is None:
            return case
        return case.model_copy(update={"driver_id": TEAMMATE})

    world.repository.get = handed_off_after_the_gate

    with pytest.raises(CaseNotDriverError):
        await world.service.close_case(CASE_ID, DRIVER)
    assert len(loads) == 2, "refused under the retry, not at the gate"


async def test_a_report_write_refuses_a_reader_case_not_driver(world):
    """The report module's writes share the driver gate (ADR-020 D2)."""
    from fastapi import HTTPException

    from faultmaven.modules.report.api.routes import authorize_case_access

    def caller(user_id):
        return SimpleNamespace(user_id=user_id, enterprise_id="ent_driver_0001")

    with pytest.raises(HTTPException) as refused:
        await authorize_case_access(
            CASE_ID,
            caller(TEAMMATE),
            world.service,
            "Report not found",
            driver_only=True,
        )
    assert refused.value.status_code == 403
    assert refused.value.headers == {"x-error-code": CASE_NOT_DRIVER}

    with pytest.raises(HTTPException) as hidden:
        await authorize_case_access(
            CASE_ID,
            caller(OUTSIDER),
            world.service,
            "Report not found",
            driver_only=True,
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail == "Report not found"

    case = await authorize_case_access(
        CASE_ID, caller(TEAMMATE), world.service, "Report not found"
    )
    assert case.case_id == CASE_ID


# ============================================================
# The contract declares both labels
# ============================================================


def _spec():
    app = FastAPI()
    app.include_router(case_router, prefix="/api/v1")
    return app.openapi()["paths"]


#: Under ``/cases/{case_id}`` and declaring no case 404 on purpose.
_NO_CASE_LOOKUP = {
    ("delete", "/api/v1/cases/{case_id}"),  # idempotent: an absent case is 204
    ("post", "/api/v1/cases/{case_id}/queries"),  # 410, removed
    ("post", "/api/v1/cases/{case_id}/data"),  # 410, removed
}


def test_every_case_route_declares_case_not_found():
    missing = []
    for path, operations in _spec().items():
        if "{case_id}" not in path:
            continue
        for method, operation in operations.items():
            if (method, path) in _NO_CASE_LOOKUP:
                continue
            declared = (
                operation.get("responses", {})
                .get("404", {})
                .get("headers", {})
                .get("x-error-code", {})
                .get("schema", {})
                .get("enum", [])
            )
            if CASE_NOT_FOUND not in declared:
                missing.append(f"{method.upper()} {path}")
    assert missing == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("put", "/api/v1/cases/{case_id}"),
        ("post", "/api/v1/cases/{case_id}/title"),
        ("post", "/api/v1/cases/sessions/{session_id}/resume/{case_id}"),
        ("delete", "/api/v1/cases/{case_id}/data/{data_id}"),
        ("post", "/api/v1/cases/{case_id}/reports"),
        ("post", "/api/v1/cases/{case_id}/close"),
        ("post", "/api/v1/cases/{case_id}/turns"),
        ("patch", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"),
    ],
)
def test_every_driver_only_case_route_declares_case_not_driver(method, path):
    declared = _spec()[path][method]["responses"]["403"]["headers"]["x-error-code"]
    assert declared["schema"]["enum"] == [CASE_NOT_DRIVER]


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/v1/reports/generate"),
        ("put", "/api/v1/reports/{report_id}"),
        ("delete", "/api/v1/reports/{report_id}"),
        ("post", "/api/v1/reports/{report_id}/link-case"),
    ],
)
def test_every_report_write_declares_case_not_driver(method, path):
    from faultmaven.modules.report.api.routes import router as report_router

    app = FastAPI()
    app.include_router(report_router, prefix="/api/v1")
    responses = app.openapi()["paths"][path][method]["responses"]
    declared = responses["403"]["headers"]["x-error-code"]["schema"]["enum"]
    assert declared == [CASE_NOT_DRIVER]
