"""Unit tests for the runbook Cause matcher instantiation (increment 4a).

- ``chain_to_specs`` converts a ``CauseRecord`` chain into
  ``ingest_emitted_chain`` spec shapes (drops the engine-seeded D, maps refs).
- ``instantiate_cause_chain`` seeds the chain into a case as CANDIDATE nodes.
- ``apply_runbook_cause_matcher`` runs the matcher and instantiates the winner
  (only a confident single-Cause verdict), never anything else.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.cause_schemas import (
    CauseMatch,
    CauseMatchResult,
    CauseRecord,
)
from faultmaven.core.investigation.runbook_cause_matcher import (
    apply_runbook_cause_matcher,
    chain_to_specs,
    instantiate_cause_chain,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemVerification,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _case() -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
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
    case.current_turn = 4
    return case


def _node(ref, node_type, statement):
    return {"ref": ref, "node_type": node_type, "statement": statement}


def _edge(cause_ref, effect_ref):
    return {"cause_ref": cause_ref, "effect_ref": effect_ref}


def _linear_cause(letter: str = "A") -> CauseRecord:
    # root -> s1 -> D
    return CauseRecord(
        cause_letter=letter,
        cause_name="name",
        cause_statement="stmt",
        chain_nodes=[
            _node("root", "root", "the root cause"),
            _node("s1", "intermediate", "an intermediate effect"),
            _node("D", "problem", "the problem"),
        ],
        chain_edges=[_edge("root", "s1"), _edge("s1", "D")],
    )


# ---------------------------------------------------------------------------
# chain_to_specs
# ---------------------------------------------------------------------------


class TestChainToSpecs:
    def test_linear_chain_drops_problem_and_maps_refs(self):
        nodes, edges = chain_to_specs(_linear_cause())
        assert [n.node_type for n in nodes] == [NodeType.ROOT, NodeType.INTERMEDIATE]
        assert [n.statement for n in nodes] == [
            "the root cause",
            "an intermediate effect",
        ]
        # root=new_index_0, s1=new_index_1, D='D'
        assert (edges[0].cause, edges[0].effect) == ("new_index_0", "new_index_1")
        assert (edges[1].cause, edges[1].effect) == ("new_index_1", "D")

    def test_unknown_node_type_defaults_to_intermediate(self):
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[_node("x", "weird", "some state")],
            chain_edges=[],
        )
        nodes, _ = chain_to_specs(cause)
        assert nodes[0].node_type == NodeType.INTERMEDIATE

    def test_empty_statement_node_skipped(self):
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[
                _node("root", "root", "   "),
                _node("s1", "intermediate", "ok"),
            ],
            chain_edges=[],
        )
        nodes, _ = chain_to_specs(cause)
        assert [n.statement for n in nodes] == ["ok"]

    def test_degenerate_cause_only_problem_yields_no_nodes(self):
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[_node("D", "problem", "the problem")],
            chain_edges=[],
        )
        nodes, edges = chain_to_specs(cause)
        assert nodes == [] and edges == []

    def test_edge_with_unresolvable_endpoint_is_dropped(self):
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[_node("root", "root", "r")],
            chain_edges=[_edge("root", "ghost")],  # 'ghost' never defined
        )
        _, edges = chain_to_specs(cause)
        assert edges == []


# ---------------------------------------------------------------------------
# instantiate_cause_chain
# ---------------------------------------------------------------------------


class TestInstantiate:
    def test_seeds_chain_as_candidate_nodes(self):
        case = _case()
        created = instantiate_cause_chain(case, _linear_cause(), case.current_turn)

        assert len([c for c in created if c]) == 2  # root + s1 (D is seeded)
        statements = {n.statement for n in case.causal_nodes.values()}
        assert "the root cause" in statements
        assert "an intermediate effect" in statements
        # A ROOT and a PROBLEM (D) both present; matcher nodes are CANDIDATE.
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1
        assert roots[0].node_state == NodeState.CANDIDATE
        problems = [
            n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
        ]
        assert len(problems) == 1  # exactly one D
        assert len(case.causal_edges) == 2

    def test_degenerate_cause_instantiates_nothing(self):
        case = _case()
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[_node("D", "problem", "the problem")],
            chain_edges=[],
        )
        created = instantiate_cause_chain(case, cause, case.current_turn)
        assert created == []
        assert case.causal_nodes == {}  # D not even seeded


# ---------------------------------------------------------------------------
# apply_runbook_cause_matcher
# ---------------------------------------------------------------------------


class _FakeKB:
    """Stand-in for AnswerFromKB.aget_cause_matches."""

    def __init__(self, results):
        self._results = results
        self.calls = []

    async def aget_cause_matches(
        self,
        question,
        user_id,
        *,
        resolve_causes,
        evaluator,
        team_ids=None,
        max_runbooks=3,
    ):
        self.calls.append(
            {
                "question": question,
                "user_id": user_id,
                "team_ids": team_ids,
                "max_runbooks": max_runbooks,
            }
        )
        return self._results


def _result(runbook_id, verdict, *, record=None, letter="A"):
    selected = (
        CauseMatch(cause_letter=letter, cause_name="n", belief=1.0, is_fallback=False)
        if verdict == "single"
        else None
    )
    return CauseMatchResult(
        runbook_id=runbook_id,
        verdict=verdict,
        selected_cause=selected,
        selected_record=record,
    )


async def _noop_resolver(item_id):
    return None


class TestApply:
    @pytest.mark.asyncio
    async def test_single_verdict_instantiates_and_returns_choice(self):
        case = _case()
        record = _linear_cause("A")
        kb = _FakeKB([_result("kb_rb1", "single", record=record)])

        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),  # fake ignores it
            question="why does deploy fail?",
            user_id="u1",
            team_ids=["t1"],
        )

        assert chosen is not None and chosen.runbook_id == "kb_rb1"
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1  # the matched chain was instantiated
        # Inputs threaded through to the matcher.
        assert kb.calls[0]["user_id"] == "u1"
        assert kb.calls[0]["team_ids"] == ["t1"]

    @pytest.mark.asyncio
    async def test_none_verdict_instantiates_nothing(self):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "none")])
        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert chosen is None
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_multiple_verdict_instantiates_nothing(self):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "multiple")])
        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert chosen is None
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_single_without_record_is_skipped(self):
        # Defensive: a 'single' verdict that somehow lacks selected_record must
        # not be chosen (nothing to instantiate).
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=None)])
        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert chosen is None
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_picks_first_single_among_results(self):
        case = _case()
        kb = _FakeKB(
            [
                _result("kb_none", "none"),
                _result("kb_win", "single", record=_linear_cause("B"), letter="B"),
                _result("kb_other", "single", record=_linear_cause("C"), letter="C"),
            ]
        )
        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert chosen.runbook_id == "kb_win"
        # Only the first winner's chain instantiated (one root).
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1


class TestDuplicateRef:
    def test_duplicate_ref_skips_node_and_keeps_first_edge_target(self):
        # Two nodes share ref 'x'; the duplicate must be skipped (not overwrite
        # 'x' → new_index), so an edge to 'x' still targets the first node.
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[
                _node("x", "root", "first x"),
                _node("x", "intermediate", "second x (dup)"),
                _node("D", "problem", "the problem"),
            ],
            chain_edges=[_edge("x", "D")],
        )
        nodes, edges = chain_to_specs(cause)
        assert [n.statement for n in nodes] == ["first x"]  # dup dropped
        assert (edges[0].cause, edges[0].effect) == ("new_index_0", "D")


# ---------------------------------------------------------------------------
# Engine integration: MilestoneEngine._apply_runbook_cause_matcher
# (constructed via __new__ to bypass the heavy engine __init__ — the method
# only touches self.investigation_tools and self.knowledge_service).
# ---------------------------------------------------------------------------


def _engine(*, registry, knowledge_service):
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.investigation_tools = registry
    eng.knowledge_service = knowledge_service
    return eng


def _registry(wrapped):
    """A fake AgentToolRegistry: .get('kb_qa') → adapter exposing .wrapped."""
    adapter = SimpleNamespace(wrapped=wrapped)
    return SimpleNamespace(get=lambda name: adapter if name == "kb_qa" else None)


def _ks():
    async def _resolver(item_id):
        return None

    return SimpleNamespace(get_runbook_causes=_resolver)


class TestEngineIntegration:
    @pytest.mark.asyncio
    async def test_single_match_instantiates_via_engine(self):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        eng = _engine(registry=_registry(kb), knowledge_service=_ks())

        await eng._apply_runbook_cause_matcher(case, None, {})

        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1
        # Question sourced from the verified symptom statement.
        assert kb.calls[0]["question"] == "Deploy to on-prem job fails"
        assert kb.calls[0]["user_id"] == "u"

    @pytest.mark.asyncio
    async def test_question_falls_back_to_description(self):
        case = _case()
        object.__setattr__(case, "problem_verification", None)
        object.__setattr__(case, "description", "fallback question")
        kb = _FakeKB([_result("kb_rb1", "none")])
        eng = _engine(registry=_registry(kb), knowledge_service=_ks())

        await eng._apply_runbook_cause_matcher(case, None, {})
        assert kb.calls[0]["question"] == "fallback question"

    @pytest.mark.asyncio
    async def test_empty_question_skips_matcher(self):
        case = _case()
        object.__setattr__(case, "problem_verification", None)
        object.__setattr__(case, "description", "")
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        eng = _engine(registry=_registry(kb), knowledge_service=_ks())

        await eng._apply_runbook_cause_matcher(case, None, {})
        assert kb.calls == []  # never reached the tool
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_no_investigation_tools_is_noop(self):
        case = _case()
        eng = _engine(registry=None, knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})  # must not raise
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_tool_without_matcher_method_is_noop(self):
        case = _case()
        eng = _engine(registry=_registry(object()), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})  # must not raise
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_no_knowledge_service_is_noop(self):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        eng = _engine(registry=_registry(kb), knowledge_service=None)
        await eng._apply_runbook_cause_matcher(case, None, {})
        assert kb.calls == []  # guarded out before the tool call
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_matcher_exception_is_swallowed(self):
        case = _case()

        class _BoomKB:
            async def aget_cause_matches(self, *a, **k):
                raise RuntimeError("kb boom")

        eng = _engine(registry=_registry(_BoomKB()), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})  # must not raise
        assert case.causal_nodes == {}
