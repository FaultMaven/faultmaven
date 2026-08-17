"""Causal Map section in terminal summaries.

The section is engine-derived (serialized from the persisted causal graph,
never LLM-authored) and gated: it appears only when the cause is established
over a non-trivial graph, in both the resolution and closure summaries, and a
rendering failure omits the section rather than failing the report.

Fixtures build the real graph shape for each grade rather than patching the
assurance gate (the test_resolution_assurance_note pattern).
"""

from datetime import UTC, datetime

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    CausalEdge,
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


def _terminal_case(*, resolved: bool, with_graph: bool = True) -> Case:
    case = Case(
        case_id="case_aa0000000001",
        user_id="user_x",
        organization_id="org_x",
        title="Checkout timeouts",
        description="p99 spikes on checkout.",
        state=CaseState.INVESTIGATING,
        created_at=datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC),
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
    if with_graph:
        case.evidence = [
            Evidence(
                evidence_id="ev_aaaaaaaaaaaa",
                summary="pool metrics show saturation",
                primary_purpose="diagnosis",
                category=EvidenceCategory.CAUSAL_EVIDENCE,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_by="u",
                collected_at_turn=1,
                collected_at=datetime(2026, 8, 17, 11, 0, 0, tzinfo=UTC),
            )
        ]
        problem = CausalNode(
            node_id="cn_00000000000d",
            statement="API requests time out",
            node_type=NodeType.PROBLEM,
            generated_at_turn=0,
        )
        root = CausalNode(
            node_id="cn_00000000000a",
            statement="connection pool exhausted",
            node_type=NodeType.ROOT,
            node_state=NodeState.VALIDATED,
            validation_method=ValidationMethod.EMPIRICAL,
            actionable=True,
            generated_at_turn=1,
            evidence_links=[
                NodeEvidenceLink(
                    evidence_id="ev_aaaaaaaaaaaa",
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="pool metrics",
                    linked_at_turn=1,
                )
            ],
        )
        intermediate = CausalNode(
            node_id="cn_00000000000b",
            statement="requests queue behind saturated pool",
            node_type=NodeType.INTERMEDIATE,
            generated_at_turn=2,
        )
        case.causal_nodes = {n.node_id: n for n in (problem, root, intermediate)}
        case.causal_edges = [
            CausalEdge(
                edge_id="ce_000000000001",
                cause_node_id=root.node_id,
                effect_node_id=intermediate.node_id,
                created_at_turn=1,
            ),
            CausalEdge(
                edge_id="ce_000000000002",
                cause_node_id=intermediate.node_id,
                effect_node_id=problem.node_id,
                created_at_turn=2,
            ),
        ]
    terminal_at = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
    if resolved:
        object.__setattr__(case, "state", CaseState.RESOLVED)
        object.__setattr__(case, "resolved_at", terminal_at)
        object.__setattr__(case, "closed_at", terminal_at)
    else:
        object.__setattr__(case, "state", CaseState.CLOSED)
        object.__setattr__(case, "closed_at", terminal_at)
        object.__setattr__(case, "closure_reason", "solution_deferred")
    return case


@pytest.mark.asyncio
async def test_resolution_summary_embeds_causal_map():
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _terminal_case(resolved=True), {"duration": "2h"}
    )
    assert "## Causal Map" in summary
    assert "```mermaid" in summary
    # Section lands between the cause and the fix narrative.
    assert summary.index("## Root Cause") < summary.index("## Causal Map")


@pytest.mark.asyncio
async def test_closure_summary_embeds_causal_map_when_cause_established():
    service = ReportGenerationService()
    summary = await service._generate_closure_summary(
        _terminal_case(resolved=False), {"duration": "2h"}
    )
    assert "## Causal Map" in summary
    assert "```mermaid" in summary


@pytest.mark.asyncio
async def test_no_section_when_cause_not_established():
    # No graph → the stated conclusion grades NO_ROOT → renderer gates out.
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _terminal_case(resolved=True, with_graph=False), {"duration": "2h"}
    )
    assert "## Causal Map" not in summary
    assert "```mermaid" not in summary


@pytest.mark.asyncio
async def test_rendering_failure_omits_section_not_report(monkeypatch):
    def _boom(case):
        raise RuntimeError("serializer bug")

    monkeypatch.setattr(
        "faultmaven.core.investigation.causal_map.render_causal_map", _boom
    )
    service = ReportGenerationService()
    summary = await service._generate_resolution_summary(
        _terminal_case(resolved=True), {"duration": "2h"}
    )
    # The report still generates in full; only the map is missing.
    assert "## Causal Map" not in summary
    assert "## Root Cause" in summary
    assert "## Timeline" in summary
