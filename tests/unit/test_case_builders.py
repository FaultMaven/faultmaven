"""The shared ``Case`` builders (``tests/case_builders.py``, #2171).

Two properties: every call builds fresh objects, and each knob lands where
the hand-written helpers it replaced put it — compared against those helpers'
own spellings, ``model_fields_set`` included.

The builders are reached through the module (``builders.make_case``) on
purpose: #2171's identity recorder wraps every builder name bound in a test
module, and this file's calls are not a helper's.
"""

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from faultmaven.modules.case.contracts import (
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationProgress,
    ProblemStatus,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models.case import Case
from tests import case_builders as builders

UNSET = builders.UNSET
_FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_VOLATILE = {
    "case_id",
    "created_at",
    "updated_at",
    "last_activity_at",
    "problem_statement_confirmed_at",
}


def _shape(value):
    """A model's values and ``model_fields_set``, at every level, with the
    per-call id and now-stamps reduced to their presence."""
    if isinstance(value, BaseModel):
        return {
            "__set__": sorted(value.model_fields_set),
            **{
                name: (
                    getattr(value, name) is not None
                    if name in _VOLATILE
                    else _shape(getattr(value, name))
                )
                for name in type(value).model_fields
            },
        }
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return value


def _mutables(value, out=None):
    """Every model, list, dict and set reachable from ``value``, by id."""
    out = {} if out is None else out
    if isinstance(value, (BaseModel, list, dict, set)) and id(value) not in out:
        out[id(value)] = value
        if isinstance(value, BaseModel):
            children = [getattr(value, n) for n in type(value).model_fields]
        elif isinstance(value, dict):
            children = list(value.values())
        else:
            children = list(value)
        for child in children:
            _mutables(child, out)
    return out


def _evidence() -> Evidence:
    return Evidence(
        summary="s",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_0123456789ab",
        collected_by="u",
        primary_purpose="p",
        collected_at_turn=1,
    )


_CALLS = {
    "make_case": lambda: builders.make_case(),
    "inquiry_case": lambda: builders.inquiry_case(statement="s", current_turn=2),
    "investigating_case": lambda: builders.investigating_case(
        statement="s",
        symptom="y",
        problem_status=ProblemStatus.VERIFIED,
        evidence=[_evidence()],
    ),
    "investigating_case(inquiry=UNSET)": lambda: builders.investigating_case(
        inquiry=UNSET, statement="s", progress=InvestigationProgress()
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("call", sorted(_CALLS))
def test_two_calls_share_no_mutable_sub_object(call):
    first, second = _CALLS[call](), _CALLS[call]()
    shared = _mutables(first).keys() & _mutables(second).keys()
    assert not shared
    assert first.case_id != second.case_id


@pytest.mark.unit
class TestMakeCase:
    def test_the_defaults_are_passed(self):
        case = builders.make_case()
        assert (case.user_id, case.enterprise_id, case.title) == ("u", "o", "t")
        assert case.model_fields_set == {"case_id", "user_id", "enterprise_id", "title"}
        assert case.case_id.startswith("case_") and len(case.case_id) == 17

    def test_unset_leaves_a_field_to_the_model(self):
        case = builders.make_case(case_id=UNSET, user_id=UNSET)
        assert case.user_id is None
        assert case.model_fields_set == {"enterprise_id", "title"}
        assert case.case_id.startswith("case_") and len(case.case_id) == 17

    def test_other_fields_pass_through_as_given(self):
        case = builders.make_case(case_id="case_aabbccddeeff", organization_id=None)
        assert case.case_id == "case_aabbccddeeff"
        assert "organization_id" in case.model_fields_set


@pytest.mark.unit
def test_inquiry_case_proposes_the_statement_on_the_default_inquiry():
    case = builders.inquiry_case(statement="db slow", current_turn=3)
    old = Case(
        case_id="case_000000000001",
        user_id="u",
        enterprise_id="o",
        title="t",
        state=CaseState.INQUIRY,
        current_turn=3,
    )
    old.inquiry.proposed_problem_statement = "db slow"
    assert _shape(case) == _shape(old)
    assert "inquiry" not in case.model_fields_set
    assert case.inquiry.problem_statement_confirmed is False


@pytest.mark.unit
class TestInvestigatingCase:
    def test_the_statement_lands_as_a_constructed_confirmed_inquiry(self):
        case = builders.investigating_case(
            statement="db slow",
            symptom="db slow",
            current_turn=9,
            problem_status=ProblemStatus.VERIFIED,
        )
        old = Case(
            case_id="case_000000000001",
            user_id="u",
            enterprise_id="o",
            title="t",
            description="d",
            state=CaseState.INVESTIGATING,
            inquiry=InquiryData(
                proposed_problem_statement="db slow", problem_statement_confirmed=True
            ),
            problem_verification=ProblemVerification(
                symptom_statement="db slow", severity="HIGH"
            ),
            current_turn=9,
        )
        old.progress.problem_status = ProblemStatus.VERIFIED
        assert _shape(case) == _shape(old)
        assert case.inquiry.problem_statement_confirmed_at.tzinfo is not None

    def test_an_inquiry_confirmed_by_assignment_is_stamped_by_its_validator(self):
        inquiry = InquiryData()
        inquiry.proposed_problem_statement = "s"
        inquiry.problem_statement_confirmed = True
        old = Case(
            case_id="case_000000000001",
            user_id="u",
            enterprise_id="o",
            title="t",
            description="d",
            state=CaseState.INVESTIGATING,
            inquiry=inquiry,
        )
        assert old.inquiry.problem_statement_confirmed_at is not None
        case = builders.investigating_case(statement="s")
        assert _shape(case) == _shape(old)

    def test_confirmed_at_unset_holds_on_the_cases_own_inquiry(self):
        case = builders.investigating_case(
            inquiry=UNSET, statement="s", confirmed_at=UNSET
        )
        old = Case(
            case_id="case_000000000001",
            user_id="u",
            enterprise_id="o",
            title="t",
            description="d",
        )
        old.inquiry.proposed_problem_statement = "s"
        old.inquiry.problem_statement_confirmed = True
        old.state = CaseState.INVESTIGATING
        assert case.inquiry.problem_statement_confirmed_at is None
        assert _shape(case) == _shape(old)

    def test_a_given_confirmed_at_and_inquiry_are_used(self):
        given = InquiryData(inquiry_turns=1)
        case = builders.investigating_case(
            statement="s", confirmed_at=_FIXED, inquiry=given
        )
        assert case.inquiry is given
        assert case.inquiry.problem_statement_confirmed_at == _FIXED
        assert case.inquiry.inquiry_turns == 1

    def test_inquiry_unset_confirms_the_cases_own_inquiry_then_moves_it(self):
        case = builders.investigating_case(
            inquiry=UNSET, statement="s", progress=InvestigationProgress()
        )
        old = Case(
            case_id="case_000000000001",
            user_id="u",
            enterprise_id="o",
            title="t",
            description="d",
        )
        old.inquiry.proposed_problem_statement = "s"
        old.inquiry.problem_statement_confirmed = True
        old.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
        old.state = CaseState.INVESTIGATING
        old.progress = InvestigationProgress()
        assert _shape(case) == _shape(old)
        assert "inquiry" not in case.model_fields_set

    def test_severity_unset_leaves_it_off_the_verification(self):
        case = builders.investigating_case(statement="s", symptom="y", severity=UNSET)
        assert case.problem_verification.severity is None
        assert case.problem_verification.model_fields_set == {"symptom_statement"}

    def test_a_terminal_state_is_built_with_its_stamps(self):
        case = builders.investigating_case(
            statement="s",
            state=CaseState.RESOLVED,
            resolved_at=_FIXED,
            closed_at=_FIXED,
            created_at=_FIXED,
            updated_at=_FIXED,
            last_activity_at=_FIXED,
        )
        assert case.state is CaseState.RESOLVED
        assert case.inquiry.problem_statement_confirmed is True
