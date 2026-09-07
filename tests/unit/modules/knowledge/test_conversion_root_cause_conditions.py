"""#1096 — a runbook harvested from a two-condition cause records both.

The resolution summary and the runbook read the same conclusion. Fixing only the
summary would leave the knowledge flywheel recording half a cause — the durable
half, since a runbook outlives the case it came from.
"""

from datetime import UTC, datetime

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseSeverity,
    CaseState,
    ConfidenceLevel,
    InquiryData,
    ProblemVerification,
    RootCauseConclusion,
)
from faultmaven.modules.knowledge.domain.models.conversion import CaseConversionRequest

pytestmark = pytest.mark.unit

_LIMIT = "the v2.14.0 release halved the checkout-api memory limit to 512Mi"


def _case(conditions: list[str]) -> Case:
    case = Case(
        case_id="case_aa0000000003",
        user_id="u",
        enterprise_id="o",
        title="Checkout OOM kills",
        description="checkout-api crash-loops after v2.14.0.",
        state=CaseState.INVESTIGATING,
        created_at=datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC),
        inquiry=InquiryData(
            proposed_problem_statement="checkout-api crash-loops",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout-api crash-loops after v2.14.0",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="checkout-api v2.14.0 retains an unbounded orderSummaryCache",
        mechanism="heap pressure stalls readiness until the container is OOMKilled",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        contributing_factors=conditions,
    )
    return case


def test_conversion_request_carries_the_co_necessary_conditions():
    request = CaseConversionRequest.from_case(_case([_LIMIT]))
    assert request.root_cause_conditions == [_LIMIT]


def test_a_single_condition_cause_carries_none():
    assert CaseConversionRequest.from_case(_case([])).root_cause_conditions == []
