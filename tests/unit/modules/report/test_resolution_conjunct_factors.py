"""#1096 — the Root Cause section publishes the whole conjunction.

Boundary twin of ``tests/unit/core/investigation/test_cause_conjuncts.py``: that
one pins what the engine DERIVES, this one pins that the report SAYS it. The
report is the surface the defect was reported against — an operator reading a
resolution summary later, or a runbook harvested from it, must not be told a
two-condition cause was one condition.
"""

from datetime import UTC, datetime

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    ConfidenceLevel,
    InquiryData,
    RootCauseConclusion,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

pytestmark = pytest.mark.unit

_LIMIT = "the v2.14.0 release halved the checkout-api memory limit to 512Mi"


def _resolved_case(conjuncts: list[str]) -> Case:
    # Built in INVESTIGATING, then promoted via object.__setattr__ to bypass the
    # cross-field terminal validators (the established fixture pattern).
    case = Case(
        case_id="case_aa0000000002",
        user_id="user_x",
        enterprise_id="org_x",
        title="Checkout OOM kills",
        description="checkout-api crash-loops after v2.14.0.",
        state=CaseState.INVESTIGATING,
        created_at=datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC),
        inquiry=InquiryData(
            proposed_problem_statement="checkout-api crash-loops after v2.14.0",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="checkout-api v2.14.0 retains an unbounded orderSummaryCache",
        mechanism="heap pressure stalls readiness until the container is OOMKilled",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        contributing_factors=conjuncts,
    )
    terminal_at = datetime(2026, 8, 19, 0, 10, 0, tzinfo=UTC)
    object.__setattr__(case, "state", CaseState.RESOLVED)
    object.__setattr__(case, "resolved_at", terminal_at)
    object.__setattr__(case, "closed_at", terminal_at)
    return case


@pytest.mark.asyncio
async def test_co_necessary_conditions_are_rendered_under_the_root_cause():
    summary = await ReportGenerationService()._generate_resolution_summary(
        _resolved_case([_LIMIT]), {"duration": "32 minutes"}
    )
    assert "## Root Cause" in summary
    assert "**Producing the problem also required:**" in summary
    assert f"- {_LIMIT}" in summary
    # Stated as co-necessity, under the cause and before the next section.
    factors_at = summary.index("Producing the problem also required")
    assert summary.index("## Root Cause") < factors_at


@pytest.mark.asyncio
async def test_a_single_factor_cause_grows_no_empty_section():
    summary = await ReportGenerationService()._generate_resolution_summary(
        _resolved_case([]), {"duration": "32 minutes"}
    )
    assert "## Root Cause" in summary
    assert "Producing the problem also required" not in summary
