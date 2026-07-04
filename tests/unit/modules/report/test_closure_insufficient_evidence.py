"""Closure summary for insufficient-evidence closes (verification-status Phase 3).

The closure summary is the durable capture for a case closed from the
INSUFFICIENT_EVIDENCE cell: it renders the residual candidates and the specific
unmet (unobtainable) discriminating need from persisted case state — the honest
partial that is signal for calibration and the flywheel (§5.4). No separate
snapshot column is stored; the report reads what is already on the case.
"""

from datetime import UTC, datetime

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    EvidenceNeed,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NeedObtainability,
    NeedPurpose,
    NeedState,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)


def _closed_insufficient_case() -> Case:
    case = Case(
        case_id="case_ce0000000001",
        user_id="user_x",
        organization_id="org_x",
        title="Intermittent 500s on checkout",
        description="Sporadic 500s, no reproducible pattern.",
        state=CaseState.CLOSED,
        closure_reason="closed_insufficient_evidence",
        created_at=datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC),
        closed_at=datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC),
    )
    # Two residual candidates (still in play) + one refuted (excluded).
    case.hypotheses = {
        "hyp_000000000001": Hypothesis(
            hypothesis_id="hyp_000000000001",
            statement="Upstream timeout under burst load",
            category=list(HypothesisCategory)[0],
            state=HypothesisState.CAPTURED,
            rationale="bursts correlate with errors",
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            generated_at_turn=1,
            likelihood=0.5,
        ),
        "hyp_000000000002": Hypothesis(
            hypothesis_id="hyp_000000000002",
            statement="Connection pool exhaustion",
            category=list(HypothesisCategory)[1],
            state=HypothesisState.CAPTURED,
            rationale="pool metrics unavailable",
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            generated_at_turn=1,
            likelihood=0.4,
        ),
        "hyp_000000000003": Hypothesis(
            hypothesis_id="hyp_000000000003",
            statement="Bad deploy",
            category=list(HypothesisCategory)[0],
            state=HypothesisState.REFUTED,
            rationale="ruled out by deploy timeline",
            refutation_reason="errors predate the deploy",
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            generated_at_turn=1,
            likelihood=0.1,
        ),
    }
    # The unmet discriminating need, declared unobtainable — the data wall.
    case.evidence_needs = [
        EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            request_text="connection-pool saturation metrics for the incident window",
            rationale="would distinguish pool exhaustion from upstream timeout",
            state=NeedState.PENDING,
            motivating_hypothesis_ids=["hyp_000000000002"],
            created_at_turn=3,
            obtainability=NeedObtainability.UNOBTAINABLE,
        )
    ]
    return case


@pytest.mark.asyncio
async def test_closure_summary_renders_data_boundary_section():
    service = ReportGenerationService()
    summary = await service._generate_closure_summary(
        _closed_insufficient_case(), {"duration": "2h"}
    )

    # The distinct closure label + the durable data-boundary capture.
    assert "insufficient evidence" in summary.lower()
    assert "Data Boundary" in summary
    # Residual candidates surfaced; the refuted one is NOT a residual candidate.
    assert "Upstream timeout under burst load" in summary
    assert "Connection pool exhaustion" in summary
    # The unmet need + the wall flag.
    assert "connection-pool saturation metrics" in summary
    assert "declared unobtainable" in summary


@pytest.mark.asyncio
async def test_time_stall_close_does_not_overstate_a_data_wall():
    """An insufficient-evidence close reached by pure time-stall (no need
    declared unobtainable) must NOT claim the data was unavailable."""
    case = _closed_insufficient_case()
    # Flip the only need back to UNKNOWN obtainability → no declared wall.
    case.evidence_needs[0].obtainability = NeedObtainability.UNKNOWN

    service = ReportGenerationService()
    summary = await service._generate_closure_summary(case, {"duration": "2h"})

    boundary = summary.split("## Data Boundary")[1].split("## ")[0]
    # No wall was declared → wording must not assert the data was unavailable.
    assert "could not be obtained" not in boundary
    assert "declared unobtainable" not in boundary
    assert "stalled before a single cause could be grounded" in boundary
    # The outstanding need is still surfaced, just not flagged as a wall.
    assert "connection-pool saturation metrics" in boundary


@pytest.mark.asyncio
async def test_refuted_candidate_excluded_from_residual_list():
    service = ReportGenerationService()
    summary = await service._generate_closure_summary(
        _closed_insufficient_case(), {"duration": "2h"}
    )
    # "Bad deploy" is REFUTED — it is not a candidate still in play. It may
    # appear in the general Leading Hypotheses list, but the Data Boundary block
    # lists only residual (non-refuted) candidates.
    boundary = summary.split("## Data Boundary")[1].split("##")[0]
    assert "Bad deploy" not in boundary
