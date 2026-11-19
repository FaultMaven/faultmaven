"""The held reason has one derivation (#2811 S2b-1c-ii, #2615 stage S2).

Why a held check holds (``pending_check_reason``) is derived once, in
``cause_assurance``, and both the record (``resolution.pending_check_reason``)
and the PENDING_CHECK decision (``pending_check_params``) read it. The notice
renders byte for byte as on ``main`` (``pending_check_oracle_2811.json``,
captured at 40498f968 through ``pending_check_params`` on the golden held
cases, then ``render_notice``), and a decision whose held reason the record
does not state is counted, once per decision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultmaven.core.investigation.cause_assurance import (
    pending_check_reason,
    pending_symptom_check,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import _conflicts
from faultmaven.core.investigation.narration.codes import NoticeCode, OfferCode
from faultmaven.core.investigation.narration.notices import (
    PENDING_CHECK_STATUS,
    pending_check_feedback,
    render_notice,
)
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.core.investigation.terminal_transitions import pending_check_params
from faultmaven.modules.case.contracts import (
    NeedState,
    PendingCheckReason,
    ResolutionReason,
)
from tests.unit.core.investigation import golden_cases_2636 as golden

pytestmark = pytest.mark.unit

ORACLE = json.loads(
    (Path(__file__).parent / "pending_check_oracle_2811.json").read_text()
)

_HELD = ("unanswered", "fulfilled")


def _case(held: str, declined: bool):
    case = golden._held(fulfilled=held == "fulfilled")
    if declined:
        golden._declined(case)
    return case


def _params(case, *, proposed: bool = False) -> dict:
    return pending_check_params(case, pending_symptom_check(case), proposed=proposed)


def _key(held: str, proposed: bool, declined: bool) -> str:
    return f"{held}|proposed={proposed}|declined_stands={declined}"


def test_the_oracle_covers_every_combination():
    assert set(ORACLE) == {
        _key(h, p, d) for h in _HELD for p in (False, True) for d in (False, True)
    }


@pytest.mark.parametrize("declined", [False, True])
@pytest.mark.parametrize("proposed", [False, True])
@pytest.mark.parametrize("held", _HELD)
def test_the_notice_renders_as_main_did(held, proposed, declined):
    case = _case(held, declined)
    params = _params(case, proposed=proposed)
    assert params["declined_stands"] is declined, "premise"
    rendered = render_notice(NoticeCode.PENDING_CHECK, params, None, [])
    assert rendered == ORACLE[_key(held, proposed, declined)]


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (NeedState.PENDING, PendingCheckReason.UNANSWERED),
        (NeedState.PARTIALLY_MET, PendingCheckReason.UNANSWERED),
        (NeedState.FULFILLED, PendingCheckReason.FULFILLED_BEFORE_ASK),
    ],
)
def test_the_held_reason_is_the_checks_state(state, reason):
    need = golden._held().evidence_needs[-1]
    need.state = state
    assert pending_check_reason(need) == reason


@pytest.mark.parametrize("declined", [False, True])
@pytest.mark.parametrize("held", _HELD)
def test_the_decision_and_the_record_state_one_held_reason(held, declined):
    case = _case(held, declined)
    resolution = narration_outcome(case).resolution
    assert resolution.reason == ResolutionReason.PENDING_CHECK, "premise: held"
    assert _params(case)["pending_check_reason"] == resolution.pending_check_reason


def _record(outcome, **resolution):
    return outcome.model_copy(
        update={"resolution": outcome.resolution.model_copy(update=resolution)}
    )


@pytest.fixture
def held():
    """A held case's PENDING_CHECK decision, and the record it agrees with."""
    case = golden._held()
    outcome = narration_outcome(case)
    log = [{"code": NoticeCode.PENDING_CHECK, "params": _params(case)}]
    assert log[0]["params"]["pending_check_reason"] == PendingCheckReason.UNANSWERED
    return log, outcome


def test_a_held_reason_the_record_does_not_state_is_counted(held):
    log, outcome = held
    record = _record(
        outcome, pending_check_reason=PendingCheckReason.FULFILLED_BEFORE_ASK
    )
    assert _conflicts(log, record) == [f"narration.{NoticeCode.PENDING_CHECK}"]


def test_a_reason_the_record_does_not_state_is_counted_once(held):
    """The held reason refines the reason: with the reason moved off
    PENDING_CHECK, the record's held reason differs too, and the decision
    still counts one conflict."""
    log, outcome = held
    record = _record(
        outcome, reason=ResolutionReason.CONFIRMED, pending_check_reason=None
    )
    assert _conflicts(log, record) == [f"narration.{NoticeCode.PENDING_CHECK}"]


def test_a_decision_the_record_states_counts_nothing(held):
    log, outcome = held
    assert _conflicts(log, outcome) == []


def test_other_codes_count_each_field_as_on_main(held):
    """A BACKSTOP_RESOLVE decision with both of its fields differing counts
    two conflicts, as on ``main``: the refinement rule is the held reason's."""
    _, outcome = held
    assert outcome.cause.identification_leg is not None, "premise"
    log = [
        {
            "code": OfferCode.BACKSTOP_RESOLVE,
            "params": {
                "reason": ResolutionReason.CONFIRMED,
                "identification_leg": None,
            },
        }
    ]
    assert _conflicts(log, outcome) == [f"narration.{OfferCode.BACKSTOP_RESOLVE}"] * 2


@pytest.mark.parametrize("reason", list(PendingCheckReason))
def test_every_held_reason_renders_a_status(reason):
    """Pin 6: each ``PendingCheckReason`` member has its status."""
    status = PENDING_CHECK_STATUS[reason].format(named="[need] the ask")
    text = pending_check_feedback(
        "need",
        "the ask",
        pending_check_reason=reason,
        proposed=False,
        declined_stands=False,
    )
    assert text.startswith(f"RESOLUTION HELD: {status}. ")
    assert len(set(PENDING_CHECK_STATUS.values())) == len(PendingCheckReason)
