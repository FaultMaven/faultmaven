"""RESOLVED UI payload carries the assurance grade beside the root cause (#572 /
INV-28, §3.5 frontend read-time labeling).

``RootCauseSummary`` exposes ``cause_assurance`` and ``cause_overclaim`` so a
frontend can label a lower-assurance conclusion instead of presenting every
resolved cause at equal certainty. Like the report's ``_assurance_note``, the
adapter RECOMPUTES the grade from the causal graph — terminal cases never
recompute the persisted progress field, and a blob persisted before the field
existed defaults to NO_ROOT — so these fixtures build the graph shape per grade.

Mechanical / LLM-agnostic: the grade is decided by graph state, asserted directly.
"""

from datetime import UTC, datetime

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    CausalNode,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    RootCauseConclusion,
    ValidationMethod,
)
from faultmaven.modules.case.domain.services.case_ui_adapter import (
    transform_case_for_ui,
)

pytestmark = pytest.mark.unit


def _evidence(evidence_id: str, category: EvidenceCategory) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        summary="an observed fact",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="u",
        collected_at_turn=1,
        collected_at=datetime(2026, 7, 4, 11, 0, 0, tzinfo=UTC),
    )


def _resolved_case(
    *,
    with_root: bool,
    confirmed: bool,
    confidence_level: ConfidenceLevel = ConfidenceLevel.CONFIDENT,
) -> Case:
    # Built in INVESTIGATING, then promoted via object.__setattr__ to bypass the
    # cross-field terminal validators (the established fixture pattern).
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
    # likelihood must satisfy the RCC's confidence_consistency validator
    # (VERIFIED requires >= 0.9); otherwise the CONFIDENT band (< 0.9) applies.
    likelihood = 0.95 if confidence_level == ConfidenceLevel.VERIFIED else 0.8
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Connection pool exhausted",
        mechanism="pool saturation queues requests past the timeout",
        confidence_level=confidence_level,
        likelihood=likelihood,
    )
    if with_root:
        case.evidence = [_evidence("ev_aaaaaaaaaaaa", EvidenceCategory.CAUSAL_EVIDENCE)]
        links = [
            NodeEvidenceLink(
                evidence_id="ev_aaaaaaaaaaaa",
                stance=EvidenceStance.SUPPORTS,
                reasoning="pool metrics",
                linked_at_turn=1,
            )
        ]
        if confirmed:
            case.evidence.append(
                _evidence("ev_bbbbbbbbbbbb", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
            )
            links.append(
                NodeEvidenceLink(
                    evidence_id="ev_bbbbbbbbbbbb",
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="removing the cause removed the problem",
                    linked_at_turn=2,
                )
            )
        case.causal_nodes = {
            "cn_aaaaaaaaaaaa": CausalNode(
                node_id="cn_aaaaaaaaaaaa",
                statement="connection pool exhausted",
                node_type=NodeType.ROOT,
                node_state=NodeState.VALIDATED,
                validation_method=ValidationMethod.EMPIRICAL,
                actionable=True,
                belief=0.8,
                generated_at_turn=1,
                evidence_links=links,
            )
        }
    terminal_at = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
    object.__setattr__(case, "state", CaseState.RESOLVED)
    object.__setattr__(case, "resolved_at", terminal_at)
    object.__setattr__(case, "closed_at", terminal_at)
    object.__setattr__(case, "closure_reason", "closed_after_investigation")
    return case


def test_mechanistic_root_grades_mechanistic_no_overclaim():
    response = transform_case_for_ui(_resolved_case(with_root=True, confirmed=False))
    assert response.root_cause.cause_assurance == "mechanistic"
    assert response.root_cause.cause_overclaim is False


def test_no_validated_root_grades_no_root():
    response = transform_case_for_ui(_resolved_case(with_root=False, confirmed=False))
    assert response.root_cause.cause_assurance == "no_root"
    assert response.root_cause.cause_overclaim is False


def test_counterfactually_confirmed_root_grades_confirmed():
    response = transform_case_for_ui(_resolved_case(with_root=True, confirmed=True))
    assert response.root_cause.cause_assurance == "confirmed"
    assert response.root_cause.cause_overclaim is False


def test_verified_claim_over_unconfirmed_root_flags_overclaim():
    # RCC self-claims VERIFIED while the graph grade is only mechanistic — the
    # conclusion_overclaims seam.
    response = transform_case_for_ui(
        _resolved_case(
            with_root=True,
            confirmed=False,
            confidence_level=ConfidenceLevel.VERIFIED,
        )
    )
    assert response.root_cause.cause_assurance == "mechanistic"
    assert response.root_cause.cause_overclaim is True


def test_grade_recomputed_ignoring_stale_persisted_no_root_default():
    # Blob predates the persisted grade (NO_ROOT default) but the graph carries a
    # confirmed root: the payload must follow the graph, not the stale field.
    case = _resolved_case(with_root=True, confirmed=True)
    assert case.progress.cause_assurance.value == "no_root"
    response = transform_case_for_ui(case)
    assert response.root_cause.cause_assurance == "confirmed"
