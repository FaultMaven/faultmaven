"""The shared ``Case`` builders (``tests/case_builders.py``, #2171).

Two properties: every call builds fresh objects, and each knob lands where
the hand-written helpers it replaced put it, compared against those helpers'
own spelling.
"""

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from faultmaven.modules.case.contracts import (
    CaseState,
    InquiryData,
    ProblemStatus,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)
from faultmaven.modules.case.domain.models.case import Case
from tests.case_builders import inquiry_case, investigating_case, make_case

pytestmark = pytest.mark.unit

_FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_PER_CALL = {
    "case_id",
    "created_at",
    "updated_at",
    "last_activity_at",
    "problem_statement_confirmed_at",
}


def _values(value):
    """``model_dump()`` with the per-call id and stamps reduced to presence."""
    if isinstance(value, dict):
        return {
            k: (v is not None) if k in _PER_CALL else _values(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_values(v) for v in value]
    return value


def _same(new: Case, old: Case) -> bool:
    return _values(new.model_dump()) == _values(old.model_dump())


def _confirmed(statement: str) -> InquiryData:
    return InquiryData(
        proposed_problem_statement=statement, problem_statement_confirmed=True
    )


def test_two_calls_share_no_mutable_sub_object():
    def reachable(value, out):
        if isinstance(value, (list, dict, BaseModel)):
            if id(value) in out:
                return out
            out[id(value)] = value
            if isinstance(value, BaseModel):
                children = [getattr(value, n) for n in type(value).model_fields]
            else:
                children = value.values() if isinstance(value, dict) else value
            for child in children:
                reachable(child, out)
        return out

    first, second = (investigating_case(statement="s", symptom="y") for _ in "12")
    assert not reachable(first, {}).keys() & reachable(second, {}).keys()


def test_make_case_defaults_and_a_fresh_case_id_per_call():
    first, second = make_case(), make_case()
    assert (first.user_id, first.enterprise_id, first.title) == ("u", "o", "t")
    assert first.case_id != second.case_id


def test_inquiry_case_proposes_the_statement_without_confirming_it():
    old = Case(user_id="u", enterprise_id="o", title="t", state=CaseState.INQUIRY)
    old.inquiry.proposed_problem_statement = "db slow"
    assert _same(inquiry_case(statement="db slow"), old)


def test_investigating_case_lands_as_the_helpers_spelled_it():
    case = investigating_case(
        statement="db slow", symptom="db slow", problem_status=ProblemStatus.VERIFIED
    )
    old = Case(
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=_confirmed("db slow"),
        problem_verification=ProblemVerification(
            symptom_statement="db slow", severity="HIGH"
        ),
    )
    old.progress.problem_status = ProblemStatus.VERIFIED
    assert _same(case, old)
    assert case.inquiry.problem_statement_confirmed_at is not None


def test_the_verification_knobs_land_on_the_verification():
    pv = investigating_case(
        statement="s", symptom="y", temporal_state="ongoing", urgency="high"
    ).problem_verification
    assert (pv.temporal_state, pv.urgency_level) == (
        TemporalState.ONGOING,
        UrgencyLevel.HIGH,
    )


def test_a_given_confirmation_time_is_used():
    case = investigating_case(statement="s", confirmed_at=_FIXED)
    assert case.inquiry.problem_statement_confirmed_at == _FIXED


def test_the_confirmation_precedes_the_case_unless_confirmed_after_build():
    before = investigating_case(statement="s")
    after = investigating_case(statement="s", confirm_after_build=True)
    assert before.inquiry.problem_statement_confirmed_at <= before.created_at
    assert after.inquiry.problem_statement_confirmed_at >= after.created_at
    assert after.state is CaseState.INVESTIGATING
    assert after.inquiry.proposed_problem_statement == "s"
    with pytest.raises(ValueError, match="confirm_after_build"):
        investigating_case(confirm_after_build=True, state=CaseState.INQUIRY)


def test_a_given_inquiry_is_copied_and_keeps_its_values():
    given = InquiryData(inquiry_turns=1)
    given.problem_statement_confirmed_at = _FIXED
    for after_build in (False, True):
        case = investigating_case(
            statement="s", inquiry=given, confirm_after_build=after_build
        )
        assert case.inquiry is not given
        assert case.inquiry.inquiry_turns == 1
        assert case.inquiry.problem_statement_confirmed_at == _FIXED
    assert given.proposed_problem_statement is None


def test_a_terminal_state_is_built_with_its_stamps():
    case = investigating_case(
        statement="s",
        state=CaseState.RESOLVED,
        resolved_at=_FIXED,
        closed_at=_FIXED,
        created_at=_FIXED,
        updated_at=_FIXED,
        last_activity_at=_FIXED,
    )
    assert case.state is CaseState.RESOLVED
