"""Resolution summary assurance qualifier (#572 / #656 P1.2).

The Root Cause section labels a conclusion the engine grades below CONFIRMED
(the M2 top grade), so the report never presents an unconfirmed cause at full
certainty. A counterfactually confirmed cause renders clean — no note.
"""

from datetime import UTC, datetime

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    CauseAssuranceGrade,
    ConfidenceLevel,
    InquiryData,
    RootCauseConclusion,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

pytestmark = pytest.mark.unit


def _resolved_case(grade: CauseAssuranceGrade) -> Case:
    # Built in INVESTIGATING, then promoted via object.__setattr__ to bypass
    # the cross-field terminal validators (the established fixture pattern).
    case = Case(
        case_id="case_aa0000000001",
        user_id="user_x",
        organization_id="org_x",
        title="Checkout timeouts",
        description="p99 spikes on checkout.",
        state=CaseState.INVESTIGATING,
        created_at=datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC),
        inquiry=InquiryData(
            proposed_problem_statement="p99 spikes on checkout",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Connection pool exhausted",
        mechanism="pool saturation queues requests past the timeout",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
    )
    case.progress.cause_assurance = grade
    terminal_at = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
    object.__setattr__(case, "state", CaseState.RESOLVED)
    object.__setattr__(case, "resolved_at", terminal_at)
    object.__setattr__(case, "closed_at", terminal_at)
    object.__setattr__(case, "closure_reason", "closed_after_investigation")
    return case


@pytest.mark.asyncio
async def test_mechanistic_conclusion_carries_assurance_note():
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _resolved_case(CauseAssuranceGrade.MECHANISTIC), {"duration": "2h"}
    )
    assert "## Root Cause" in summary
    assert "not counterfactually confirmed" in summary


@pytest.mark.asyncio
async def test_no_root_conclusion_carries_stated_by_assistant_note():
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _resolved_case(CauseAssuranceGrade.NO_ROOT), {"duration": "2h"}
    )
    assert "not\nvalidated" in summary or "not validated" in summary


@pytest.mark.asyncio
async def test_confirmed_conclusion_renders_without_note():
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _resolved_case(CauseAssuranceGrade.CONFIRMED), {"duration": "2h"}
    )
    assert "## Root Cause" in summary
    assert "Assurance:" not in summary
