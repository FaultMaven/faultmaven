"""The resolution summary's fix section reports what was DONE, not what was said.

The engine mints one ``Solution`` per LLM fix proposal and never stamps its
lifecycle fields, so ``case.solutions`` accumulates every re-proposal of the
same remediation. Rendering all of them under "Solution Applied" claimed three
fixes were applied in a case where one was executed, one was superseded by its
own re-proposal, and one was still pending at resolution (fm#1091).

Which is which is recorded on the ``ProposedAction`` each solution is
co-created with — the same signal the runbook boundary already derives with
``classify_solution_outcome``.
"""

from datetime import UTC, datetime

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    ConfidenceLevel,
    InquiryData,
    InvestigationActionType,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    SolutionType,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

pytestmark = pytest.mark.unit


def _resolved_case() -> Case:
    case = Case(
        case_id="case_aa0000000002",
        user_id="user_x",
        organization_id="org_x",
        title="Checkout OOMKills",
        description="checkout-api is OOM-killed since v2.14.0.",
        state=CaseState.INVESTIGATING,
        created_at=datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC),
        inquiry=InquiryData(
            proposed_problem_statement="checkout-api is OOM-killed",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="An unbounded cache met a lowered memory limit",
        mechanism="the cache grows past the 512Mi cgroup limit",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
    )
    terminal_at = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    object.__setattr__(case, "state", CaseState.RESOLVED)
    object.__setattr__(case, "resolved_at", terminal_at)
    object.__setattr__(case, "closed_at", terminal_at)
    return case


def _solution(description: str) -> Solution:
    return Solution(
        solution_type=SolutionType.CONFIG_CHANGE,
        title="Solution: SolutionType.CONFIG_CHANGE",
        immediate_action=description,
    )


def _action(case_id: str, description: str, state: str, turn: int) -> ProposedAction:
    return ProposedAction(
        case_id=case_id,
        action_type=InvestigationActionType.SOLUTION,
        description=description,
        proposed_in_turn=turn,
        state=state,
    )


@pytest.mark.asyncio
async def test_only_the_executed_fix_is_reported_as_applied():
    """The live shape from fm#1091: one superseded proposal, one accepted, one
    still pending. Exactly the accepted one is the fix."""
    case = _resolved_case()
    superseded, applied, pending = (
        "Restore the limit to 1Gi and bound the cache",
        "Bound orderSummaryCache and restore the limit to 1Gi, deploy rev 11",
        "Bound the cache at 50,000 entries and restore the limit to 1Gi",
    )
    case.solutions = [_solution(s) for s in (superseded, applied, pending)]
    case.proposed_actions = [
        _action(case.case_id, superseded, "superseded", 4),
        _action(case.case_id, applied, "accepted", 5),
        _action(case.case_id, pending, "pending", 7),
    ]

    summary = await ReportGenerationService()._generate_resolution_summary(
        case, {"duration": "38 minutes"}
    )

    assert "## Solution Applied" in summary
    assert applied in summary
    # A superseded proposal was never run — reporting it as applied is the
    # over-claim the runbook boundary already refuses.
    assert superseded not in summary
    # A proposal still pending at resolution is not the applied fix either.
    assert pending not in summary
    # One numbered entry, not three.
    assert "**1. Config Change**" in summary
    assert "**2. Config Change**" not in summary


@pytest.mark.asyncio
async def test_standing_proposal_is_labeled_a_proposal_when_nothing_was_executed():
    # No action was ever correlated/accepted: the summary still shows the fix
    # under discussion, but must not claim it was applied.
    case = _resolved_case()
    case.solutions = [_solution("Bound the cache and restore the limit")]
    case.proposed_actions = []

    summary = await ReportGenerationService()._generate_resolution_summary(
        case, {"duration": "38 minutes"}
    )

    assert "## Proposed Solution" in summary
    assert "## Solution Applied" not in summary
    assert "No fix was recorded as executed" in summary
    assert "Bound the cache and restore the limit" in summary


@pytest.mark.asyncio
async def test_only_never_executed_proposals_yields_no_fix_section():
    # Every proposal was superseded or rejected: there is nothing honest to put
    # under either heading, so the section is omitted (the report still renders).
    case = _resolved_case()
    rejected = "Restart the pod and hope"
    case.solutions = [_solution(rejected)]
    case.proposed_actions = [_action(case.case_id, rejected, "rejected", 3)]

    summary = await ReportGenerationService()._generate_resolution_summary(
        case, {"duration": "38 minutes"}
    )

    assert "## Solution Applied" not in summary
    assert "## Proposed Solution" not in summary
    assert rejected not in summary
    assert "## Root Cause" in summary
    assert "## Timeline" in summary
