"""End-to-end (in-process) test of the enabled runbook Cause matcher (increment 5).

Drives the WHOLE wired path with only the LLM/vector boundaries mocked — the real
AnswerFromKB.aget_cause_matches, the real IndicatorEvaluator (T2 semantic tier via
case_evidence_qa), real chain instantiation, the capped hypothesis, and the
documented-fixes context block. No API server, no DB.

What this proves that the per-increment unit tests do not: the pieces compose —
retrieve → resolve → T2 match → instantiate chain → attach capped hypothesis →
stash interventions → (cause validates) → documented-fixes reach the prompt — and
the soundness invariants hold across the whole flow.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    build_investigation_context,
)
from faultmaven.core.investigation.runbook_cause_matcher import (
    RUNBOOK_INTERVENTIONS_META_KEY,
    is_runbook_match_hypothesis,
)
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CauseState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models import ValidationMethod

pytestmark = pytest.mark.unit


def _case() -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u1",
        organization_id="o",
        title="t",
        description="Deploy fails",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Deploy fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="Deploy to on-prem job fails",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 3
    return case


def _runbook_causes():
    """As returned by knowledge_service.get_runbook_causes — a real Cause whose
    only indicator has NO deterministic predicate, so it resolves on the T2
    (case_evidence_qa) tier, plus the mandatory fallback Cause."""
    return [
        {
            "cause_letter": "A",
            "cause_name": "Wave-order misconfig",
            "cause_statement": "A prerequisite resource syncs after its dependent.",
            "chain_nodes": [
                {"ref": "root", "node_type": "root", "statement": "wave order wrong"},
                {"ref": "D", "node_type": "problem", "statement": "deploy fails"},
            ],
            "chain_edges": [{"cause_ref": "root", "effect_ref": "D"}],
            "rung_indicators": {"root": ["[Symptom] sync hangs on a missing CRD"]},
            "match_predicates": [],  # no predicate → T2 semantic tier
            "interventions": [
                {
                    "quadrant": "remediation",
                    "ref": "root",
                    "text": "Reorder the sync waves so the CRD applies first.",
                }
            ],
            "is_fallback_cause": False,
        },
        {
            "cause_letter": "Z",
            "cause_name": "Unidentified",
            "chain_nodes": [{"ref": "D", "node_type": "problem", "statement": "D"}],
            "rung_indicators": {"D": ["[Default]"]},
            "is_fallback_cause": True,
        },
    ]


def _chunk(item_id="kb_rb1"):
    return {
        "id": f"{item_id}_chunk_0",
        "content": "runbook body about wave order",
        "metadata": {"title": "Wave order", "parent_document_id": item_id},
        "score": 0.92,
    }


def _engine(*, kb_tool, ce_answer=True):
    """MilestoneEngine via __new__ (bypass heavy __init__) wired with the real KB
    tool, a knowledge service that resolves the runbook's causes, and a case
    evidence tool whose yes/no judgment is `ce_answer`."""
    from faultmaven.core.investigation.hypothesis_manager import (
        create_hypothesis_manager,
    )
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    ce_tool = SimpleNamespace(answer_yes_no=AsyncMock(return_value=ce_answer))
    registry = SimpleNamespace(
        get=lambda name: {
            "kb_qa": SimpleNamespace(wrapped=kb_tool),
            "case_evidence_search": SimpleNamespace(wrapped=ce_tool),
        }.get(name)
    )

    async def _get_runbook_causes(item_id):
        return _runbook_causes()

    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.investigation_tools = registry
    eng.knowledge_service = SimpleNamespace(get_runbook_causes=_get_runbook_causes)
    eng.hypothesis_manager = create_hypothesis_manager()
    return eng, ce_tool


def _real_kb_tool():
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=[_chunk()])
    vector_store.search = AsyncMock(return_value=[_chunk()])
    return AnswerFromKB(vector_store=vector_store, llm_router=MagicMock())


class TestEnabledMatcherEndToEnd:
    @pytest.mark.asyncio
    async def test_full_path_match_to_documented_fixes(self):
        case = _case()
        eng, ce_tool = _engine(kb_tool=_real_kb_tool(), ce_answer=True)

        # --- The enabled per-turn matcher path (what the flag gates) ---
        await eng._apply_runbook_cause_matcher(case, None, {})

        # 1. The matched chain was instantiated, with a real ROOT (CANDIDATE — the
        #    matcher never validates) and the engine-seeded problem node D.
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1
        root = roots[0]
        assert root.node_state == NodeState.CANDIDATE  # soundness: not validated
        assert any(n.node_type == NodeType.PROBLEM for n in case.causal_nodes.values())

        # 2. T2 actually drove the match (the case-evidence tool was consulted).
        assert ce_tool.answer_yes_no.await_count >= 1

        # 3. A load-bearing hypothesis is attached, capped below the cause gate.
        hyps = list(case.hypotheses.values())
        assert len(hyps) == 1
        assert is_runbook_match_hypothesis(hyps[0])
        assert hyps[0].root_node_id == root.node_id
        assert hyps[0].likelihood <= 0.5  # _MATCHER_MAX_PRIOR — can't conclude alone

        # 4. The documented fixes are stashed on the root for later surfacing.
        assert RUNBOOK_INTERVENTIONS_META_KEY in root.metadata

        # 5. Before the cause is established, the fixes do NOT surface.
        ctx = build_investigation_context(case, user_message="next?")
        assert "<documented_fixes>" not in ctx["kb_results"]

        # --- Now real evidence validates the root (the LLM/engine would do this
        #     over turns); cause_state becomes IDENTIFIED. ---
        object.__setattr__(root, "node_state", NodeState.VALIDATED)
        object.__setattr__(root, "validation_method", ValidationMethod.EMPIRICAL)
        case.progress.cause_state = CauseState.IDENTIFIED

        # 6. The documented fixes now reach the assembled prompt — the LLM proposes
        #    them and the M5 gate (cause established) governs the Solution.
        ctx = build_investigation_context(case, user_message="next?")
        assert "<documented_fixes>" in ctx["kb_results"]
        assert "Reorder the sync waves" in ctx["kb_results"]

    @pytest.mark.asyncio
    async def test_no_match_when_evidence_does_not_support(self):
        # T2 says "no" for every indicator → belief 0 → verdict none → the matcher
        # instantiates nothing. A runbook is a prior, not forced onto the case.
        case = _case()
        eng, _ = _engine(kb_tool=_real_kb_tool(), ce_answer=False)
        await eng._apply_runbook_cause_matcher(case, None, {})
        assert case.causal_nodes == {}
        assert case.hypotheses == {}


class TestFlagDefault:
    def test_flag_defaults_off(self):
        # Enabling is a deliberate, sim-validated decision — never on by default.
        from faultmaven.config.settings import get_settings

        assert get_settings().features.enable_runbook_cause_matcher is False
