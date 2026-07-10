"""Resolution summary assurance qualifier (#572 / #656).

The Root Cause section labels a conclusion the engine grades below CONFIRMED
(the M2 top grade), so the report never presents an unconfirmed cause at full
certainty. A counterfactually confirmed cause renders clean — no note.

The note RECOMPUTES the grade from the persisted causal graph (terminal cases
never recompute the persisted progress field, and blobs persisted before the
field existed default to
NO_ROOT), so these fixtures build the graph shape for each grade rather than
setting the persisted field.
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
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
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


def _resolved_case(*, with_root: bool, confirmed: bool) -> Case:
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


@pytest.mark.asyncio
async def test_mechanistic_conclusion_carries_assurance_note():
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _resolved_case(with_root=True, confirmed=False), {"duration": "2h"}
    )
    assert "## Root Cause" in summary
    assert "not counterfactually confirmed" in summary


@pytest.mark.asyncio
async def test_no_root_conclusion_carries_stated_by_assistant_note():
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _resolved_case(with_root=False, confirmed=False), {"duration": "2h"}
    )
    assert "not\nvalidated" in summary or "not validated" in summary


@pytest.mark.asyncio
async def test_confirmed_conclusion_renders_without_note():
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _resolved_case(with_root=True, confirmed=True), {"duration": "2h"}
    )
    assert "## Root Cause" in summary
    assert "Assurance:" not in summary


@pytest.mark.asyncio
async def test_engine_disconfirmation_row_not_cited_as_confirming_evidence():
    """INV-30: an engine-authored absence row is an M6 failed-fix
    DISCONFIRMATION — the opposite polarity; the Confirming Evidence fallback
    (no ``evidence_basis`` on the conclusion) must not render it."""
    case = _resolved_case(with_root=True, confirmed=False)
    case.evidence.append(
        Evidence(
            evidence_id="ev_cccccccccccc",
            summary="Counterfactual disconfirmation (M6): fix failed",
            primary_purpose="failed-treatment disconfirmation",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="engine",
            collected_at_turn=3,
            collected_at=datetime(2026, 7, 4, 11, 30, 0, tzinfo=UTC),
        )
    )
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(case, {"duration": "2h"})
    assert "Counterfactual disconfirmation" not in summary
    assert "## Confirming Evidence" in summary  # the causal row still cites


@pytest.mark.asyncio
async def test_note_recomputes_grade_ignoring_stale_persisted_default():
    """A terminal case whose blob predates the persisted grade (NO_ROOT default) and whose
    graph carries a confirmed root must render clean — the note follows the
    graph, not the stale blob."""
    case = _resolved_case(with_root=True, confirmed=True)
    # Persisted field left at its default (NO_ROOT) — the pre-field blob shape.
    assert case.progress.cause_assurance.value == "no_root"
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(case, {"duration": "2h"})
    assert "Assurance:" not in summary
