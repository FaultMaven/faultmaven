"""#1877 — a runbook harvested from a case carries the severity the case
recorded, and none when it recorded none (no invented "medium")."""

import pytest

from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import (
    InquiryData,
    ProblemVerification,
)
from faultmaven.modules.knowledge.domain.models.conversion import CaseConversionRequest

pytestmark = pytest.mark.unit


def _case(**pv_fields) -> Case:
    return Case(
        case_id="case_aa0000000004",
        user_id="u",
        enterprise_id="o",
        title="Checkout OOM kills",
        description="checkout-api crash-loops after v2.14.0.",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="checkout-api crash-loops",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout-api crash-loops", **pv_fields
        ),
    )


def test_a_case_with_no_severity_converts_with_none():
    assert CaseConversionRequest.from_case(_case()).severity is None


def test_an_assessed_severity_is_carried_lowercase():
    assert CaseConversionRequest.from_case(_case(severity="HIGH")).severity == "high"


def test_a_case_with_no_record_converts_with_none():
    case = _case()
    case.problem_verification = None
    assert CaseConversionRequest.from_case(case).severity is None
