"""#2418: a REST close of a case that qualifies for RESOLVED records the
declined resolution (rulings (b), 2026-11-03, and 2026-11-05).

"Qualifies" is the chat path's predicate: a close would pivot to the resolve
offer (INV-37, ``close_pivots_to_resolve``), a confirmed case still holding a
check asked after the fix included. Over REST the close is the user's answer
to that offer: the resolve offer is declined, via the menu, by the driver, and
the case closes as ``resolution_declined``. A case that does not qualify keeps
the reason it gets today.

Driven through ``CaseService.close_case`` (the route's one call) over the
in-memory repository, the case driven by someone other than its creator.
"""

from __future__ import annotations

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.core.investigation.terminal_transitions import (
    close_pivots_to_resolve,
    closure_verdict,
)
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.case_builders import inquiry_case, with_declined_offer, with_open_offer
from tests.unit.core.investigation import test_closure_readiness as readiness
from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
    _held_case,
)
from tests.unit.core.investigation.test_window_zero_relapse_2501 import (
    _record_ready,
    _relapsed,
)
from tests.unit.modules.case.test_case_driver_1898 import (
    CASE_ID,
    CREATOR,
    DRIVER,
    ENTERPRISE,
    _Accounts,
    _Shares,
    _Teams,
)

pytestmark = pytest.mark.unit

_DECLINED = "resolution_declined"
_RESOLVE_DECLINED = ("resolve", "declined", "menu", DRIVER)


@pytest.fixture(autouse=True)
def _tenant():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


@pytest.fixture
def world():
    """CREATOR shared the case with ``t1``, where DRIVER, who drives it,
    reads it."""
    repository = InMemoryCaseRepository()
    service = CaseService(
        repository,
        team_service=_Teams({"t1": {CREATOR, DRIVER}}),
        share_repository=_Shares({CASE_ID: {"t1"}}, []),
        account_reader=_Accounts(),
    )
    return service, repository


async def _close(world, case: Case) -> Case:
    """Save ``case`` as CREATOR's, driven by DRIVER; close it as DRIVER."""
    service, repository = world
    ids = dict(case_id=CASE_ID, user_id=CREATOR, driver_id=DRIVER)
    await repository.save(case.model_copy(update={**ids, "enterprise_id": ENTERPRISE}))
    closed = await service.close_case(CASE_ID, DRIVER)
    assert closed.state == CaseState.CLOSED
    assert (await repository.get(CASE_ID)).closure_reason == closed.closure_reason
    return closed


def _qualifies(case: Case) -> bool:
    return case.state == CaseState.INVESTIGATING and close_pivots_to_resolve(
        case, closure_verdict(case)
    )


def _fixed(*, solution: bool = True, confirmed: bool = True) -> Case:
    """``test_closure_readiness``'s case: evidence and a root cause, with the
    Solution and the confirmation (both absence rows) as asked."""
    case = readiness._make_investigating_case()
    readiness._attach_evidence(case)
    readiness._attach_root_cause(case)
    if solution:
        readiness._attach_solution(case)
    if confirmed:
        readiness._attach_confirmation(case)
    return case


def _answers(case: Case) -> list[tuple[str, str, str, str | None]]:
    return [
        (a.kind.value, a.answer.value, a.via.value, a.answered_by)
        for a in case.offer_record.answers
    ]


class TestAQualifyingCaseRecordsTheDecline:
    @pytest.mark.parametrize(
        "build, today",
        [
            (_fixed, "solution_deferred"),
            (lambda: _fixed(solution=False), "closed_insufficient_evidence"),
            (_held_case, "solution_deferred"),
        ],
        ids=["confirmed-fix", "out-of-band-fix", "pending-check"],
    )
    async def test_it_closes_as_resolution_declined(self, world, build, today):
        """``today`` is the reason the close recorded before #2418."""
        case = build()
        assert _qualifies(case), "premise: a close would pivot to the resolve"
        assert case.offer_record.open is None, "premise: no offer open"

        closed = await _close(world, case)

        assert closed.closure_reason == _DECLINED != today
        assert _answers(closed) == [_RESOLVE_DECLINED]
        assert closed.offer_record.open is None
        assert closed.action_history[-1].triggered_by == DRIVER

    async def test_an_out_of_band_fix_ready_in_window_zero(self, world):
        """#2501's positive control for the relapse below: no fix dated, the
        cause and the problem both shown gone."""
        _, case = await _record_ready()
        assert _qualifies(case), "premise"

        closed = await _close(world, case)

        assert closed.closure_reason == _DECLINED
        assert _answers(closed)[-1] == _RESOLVE_DECLINED


class TestAnOpenOffer:
    async def test_an_open_resolve_offer_is_the_one_declined(self, world):
        case = _fixed()
        offered = with_open_offer(case, kind="resolve", basis="menu").proposal_id

        closed = await _close(world, case)

        assert closed.closure_reason == _DECLINED
        assert _answers(closed) == [_RESOLVE_DECLINED]
        assert closed.offer_record.answers[0].proposal_id == offered

    async def test_an_open_close_offer_lapses_and_a_resolve_is_declined(self, world):
        case = _fixed()
        offered = with_open_offer(case, kind="close", basis="model").proposal_id

        closed = await _close(world, case)

        assert closed.closure_reason == _DECLINED
        lapsed = ("close", "lapsed", "menu", None)
        assert _answers(closed) == [lapsed, _RESOLVE_DECLINED]
        assert closed.offer_record.answers[0].proposal_id == offered


class TestACaseThatDoesNotQualify:
    @pytest.mark.parametrize(
        "build, reason",
        [
            (lambda: _fixed(confirmed=False), "solution_deferred"),
            (
                lambda: _fixed(solution=False, confirmed=False),
                "closed_insufficient_evidence",
            ),
            (lambda: inquiry_case(statement="checkout fails"), "inquiry_only"),
        ],
        ids=["fix-not-confirmed", "root-cause-only", "inquiry"],
    )
    async def test_it_keeps_the_reason(self, world, build, reason):
        case = build()
        assert not _qualifies(case), "premise"

        closed = await _close(world, case)

        assert closed.closure_reason == reason
        assert _answers(closed) == []

    async def test_a_relapse_after_an_out_of_band_fix(self, world):
        """#2501: window 0 failed, so nothing is resolvable to decline."""
        _, case = await _relapsed()
        assert not _qualifies(case), "premise"
        before = _answers(case)

        closed = await _close(world, case)

        assert closed.closure_reason == "closed_insufficient_evidence"
        assert _answers(closed) == before

    async def test_a_standing_decline_lapses_the_reopen_offer(self, world):
        """The close is not a new answer to a resolution already declined: the
        reopen offer lapses (nobody answered it), and the reason is the
        decline on record."""
        case = _fixed()
        with_declined_offer(case, kind="resolve", basis="backstop")
        with_open_offer(case, kind="resolve", basis="reopen")
        assert not _qualifies(case), "premise: the decline stands"
        before = _answers(case)
        assert [a[:2] for a in before] == [("resolve", "declined")], "premise"

        closed = await _close(world, case)

        assert closed.closure_reason == _DECLINED
        assert _answers(closed) == [*before, ("resolve", "lapsed", "menu", None)]


@pytest.mark.parametrize("solution", [True, False], ids=["solution", "out-of-band"])
async def test_the_closure_report_renders_the_fix_and_what_confirmed_it(
    world, solution
):
    closed = await _close(world, _fixed(solution=solution))
    assert closed.closure_reason == _DECLINED, "premise"

    svc = object.__new__(ReportGenerationService)
    summary = await svc._generate_closure_summary(closed, {})

    assert "## Confirming Evidence\n" in summary
    assert "Post-fix logs confirm pool exhaustion no longer occurs" in summary
    assert ("## Solution Applied\n" in summary) is solution
    assert ("Increase connection pool to 100" in summary) is solution
