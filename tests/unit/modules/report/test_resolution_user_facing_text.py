"""#1097 — the rendered resolution summary carries no engine notation.

The producers are fixed at the mint sites, but the summary is generated from
the PERSISTED conclusion and terminal cases never recompute — so the surface
test is what a reader of an already-resolved case sees.
"""

import re
from datetime import datetime, timezone

import pytest

from faultmaven.modules.case.contracts import (
    CONFIRMED_ESTABLISHED_BY,
    Case,
    CaseSeverity,
    CaseState,
    ConfidenceLevel,
    InquiryData,
    ProblemVerification,
    RootCauseConclusion,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

pytestmark = pytest.mark.unit

_ENGINE_ID = re.compile(r"\b(?:ev|cn)_[0-9a-f]{12}\b")

_LEGACY_PROVENANCE = (
    "engine: user-confirmed resolution at turn 8 — causal-absence "
    "ev_a9f662e1c86f bears on root cn_984e2337cbda (M2 gone⇒gone)"
)
_LEGACY_MECHANISM = (
    "JVM heap pressure causes prolonged GC pauses and readiness failure "
    "before container OOM termination → the problem"
)


def _resolved_case_with(conclusion: RootCauseConclusion) -> Case:
    case = Case(
        case_id="case_000000001097",
        user_id="u",
        enterprise_id="o",
        title="checkout-api became unavailable after release",
        description="checkout-api crash-looping after v2.14.0",
        state=CaseState.INVESTIGATING,
        current_turn=8,
        inquiry=InquiryData(
            proposed_problem_statement="checkout-api crash-looping",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout-api crash-looping",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.root_cause_conclusion = conclusion
    # Assignment validators pair state with resolved_at; set them together.
    case.__dict__["resolved_at"] = datetime.now(timezone.utc)
    case.__dict__["state"] = CaseState.RESOLVED
    return case


async def _summary(case: Case) -> str:
    return await ReportGenerationService()._generate_resolution_summary(
        case, {"duration": "32 minutes"}
    )


@pytest.mark.asyncio
async def test_a_case_resolved_before_the_fix_renders_no_internal_ids():
    """The exact two lines the issue quotes, from the case it was filed on."""
    case = _resolved_case_with(
        RootCauseConclusion(
            root_cause="checkout-api v2.14.0 retains an unbounded orderSummaryCache",
            mechanism=_LEGACY_MECHANISM,
            confidence_level=ConfidenceLevel.VERIFIED,
            likelihood=0.9,
            established_by=_LEGACY_PROVENANCE,
            determined_by="engine",
        )
    )

    summary = await _summary(case)

    assert not _ENGINE_ID.search(summary)
    assert "gone⇒gone" not in summary
    assert f"_Established by: {CONFIRMED_ESTABLISHED_BY}._" in summary
    assert "→ the problem" not in summary
    # The provenance is restated, not dropped — it is real and worth saying.
    assert "Established by:" in summary


@pytest.mark.asyncio
async def test_the_mechanism_keeps_its_real_content():
    """Normalizing the tail must not eat the mechanism itself."""
    case = _resolved_case_with(
        RootCauseConclusion(
            root_cause="an unbounded cache",
            mechanism=_LEGACY_MECHANISM,
            confidence_level=ConfidenceLevel.VERIFIED,
            likelihood=0.9,
            determined_by="engine",
        )
    )

    summary = await _summary(case)

    assert (
        "**How it produced the symptom:** JVM heap pressure causes prolonged "
        "GC pauses and readiness failure before container OOM termination"
    ) in summary


@pytest.mark.asyncio
async def test_an_llm_authored_provenance_is_left_alone():
    """Only the engine's id-bearing audit form is rewritten. Any other value is
    someone's prose and the report is not entitled to restate it."""
    case = _resolved_case_with(
        RootCauseConclusion(
            root_cause="an unbounded cache",
            mechanism="heap grows until the container is killed",
            confidence_level=ConfidenceLevel.CONFIDENT,
            likelihood=0.7,
            established_by="the on-call engineer reproduced it in staging",
            determined_by="agent",
        )
    )

    summary = await _summary(case)

    assert "_Established by: the on-call engineer reproduced it in staging._" in summary
