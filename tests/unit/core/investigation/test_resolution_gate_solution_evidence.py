"""Regression: the resolution gate must be satisfiable from solution_evidence.

project-resolution-gate-stuck-loop (Run 36, case_95d86b7daf8c): an
instant/opportunistic resolution recorded a strong root cause + multiple
``solution_evidence`` rows but never emitted a formal ``Solution`` record
(in a pre-path state the engine backstop forbids ``solutions_to_add``
outright). ``assess_resolution_readiness`` hard-required ``case.solutions``,
so it returned NEEDS_INFO ("- Solution: What action resolved the issue?")
every turn the user confirmed resolution — an unescapable loop to max_turns.

The gate now treats the solution requirement as met when a ``solution_evidence``
row exists, since that row is direct proof a fix was applied and verified.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InvestigationProgress,
    ProblemVerification,
    RootCauseConclusion,
    Solution,
    SolutionType,
)


def _investigating_case_with_cause() -> Case:
    # Build as INQUIRY then promote — the Case validator forbids constructing
    # INVESTIGATING without a confirmed problem statement.
    case = Case(
        case_id="case_95d86b7daf8c",
        title="Resolution gate regression",
        status=CaseStatus.INQUIRY,
        user_id="user_test",
        organization_id="org_test",
        description="ES fielddata latency",
        problem_verification=ProblemVerification(
            symptom_statement="4-8s p99 latency on events-*",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "ES fielddata latency"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(timezone.utc)
    case.status = CaseStatus.INVESTIGATING
    case.progress = InvestigationProgress()
    case.progress.symptom_verified = True
    # has_cause is satisfied via a recorded conclusion (the gate reads
    # conclusions, not progress.root_cause_likelihood).
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="fielddata=true on text fields exhausts heap",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        mechanism="fielddata cache growth → GC pauses → latency",
    )
    return case


def _evidence(category: EvidenceCategory, n: int = 1) -> list[Evidence]:
    return [
        Evidence(
            summary=f"{category.value} row {i}",
            category=category,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_at=datetime.now(timezone.utc),
            collected_by="user_test",
            primary_purpose="regression",
            preprocessed_content="sample",
            content_size_bytes=10,
            preprocessing_method="manual",
            source_file_id=None,
            collected_at_turn=1,
        )
        for i in range(n)
    ]


def test_solution_evidence_satisfies_gate_without_solution_record():
    """The exact Run 36 contradiction: strong cause + solution_evidence rows,
    ZERO Solution records → READY (was an infinite NEEDS_INFO loop)."""
    case = _investigating_case_with_cause()
    case.evidence = _evidence(EvidenceCategory.SYMPTOM_EVIDENCE, 1) + _evidence(
        EvidenceCategory.SOLUTION_EVIDENCE, 8
    )
    assert not case.solutions  # no formal Solution record — the trap

    readiness = assess_resolution_readiness(case)

    assert readiness.verdict == ResolutionReadiness.READY
    assert "solution" not in readiness.missing


def test_formal_solution_record_still_satisfies_gate():
    """Positive control: the original path (a real Solution record) is unchanged."""
    case = _investigating_case_with_cause()
    case.evidence = _evidence(EvidenceCategory.SYMPTOM_EVIDENCE, 1)
    case.solutions.append(
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Reindex text fields to keyword",
            longterm_fix="Set fielddata=false; reindex",
        )
    )
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_no_solution_and_no_solution_evidence_still_needs_info():
    """Negative control: cause + symptom evidence only (no solution of any
    kind) → the solution requirement still bites (NEEDS_INFO), so the gate
    isn't blanket-weakened."""
    case = _investigating_case_with_cause()
    case.evidence = _evidence(EvidenceCategory.SYMPTOM_EVIDENCE, 2)
    assert not case.solutions

    readiness = assess_resolution_readiness(case)

    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert "solution" in readiness.missing
