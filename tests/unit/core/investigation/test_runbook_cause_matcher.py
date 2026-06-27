"""Unit tests for the runbook Cause matcher instantiation (increment 4a).

- ``chain_to_specs`` converts a ``CauseRecord`` chain into
  ``ingest_emitted_chain`` spec shapes (drops the engine-seeded D, maps refs).
- ``instantiate_cause_chain`` seeds the chain into a case as CANDIDATE nodes.
- ``apply_runbook_cause_matcher`` runs the matcher and instantiates the winner
  (only a confident single-Cause verdict), never anything else.
"""

import logging
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
                "evaluator": evaluator,
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


class TestHypothesisAttachment:
    @staticmethod
    def _hm():
        from faultmaven.core.investigation.hypothesis_manager import (
            create_hypothesis_manager,
        )

        return create_hypothesis_manager()

    @pytest.mark.asyncio
    async def test_single_match_attaches_hypothesis_rooted_at_chain_root(self):
        from faultmaven.modules.case.contracts import HypothesisState

        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
            hypothesis_manager=self._hm(),
        )
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1
        assert len(case.hypotheses) == 1
        hyp = next(iter(case.hypotheses.values()))
        assert hyp.root_node_id == roots[0].node_id
        assert hyp.state == HypothesisState.ACTIVE
        # path is root → D (the matched chain materialized).
        assert hyp.path[0] == roots[0].node_id
        assert len(hyp.path) >= 2  # root ... D

    @pytest.mark.asyncio
    async def test_attachment_is_idempotent_across_turns(self):
        # The matcher runs every turn; node dedup → same root → no 2nd hypothesis.
        case = _case()
        hm = self._hm()

        async def _run():
            kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
            await apply_runbook_cause_matcher(
                case,
                kb_tool=kb,
                resolve_causes=_noop_resolver,
                evaluator=object(),
                question="q",
                user_id="u1",
                hypothesis_manager=hm,
            )

        await _run()
        await _run()  # second turn
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1  # node deduped
        assert len(case.hypotheses) == 1  # hypothesis deduped (not re-spawned)

    @pytest.mark.asyncio
    async def test_belief_is_capped_below_cause_identified_gate(self):
        # A runbook is a PRIOR, not a conclusion: even a belief of 1.0 must not
        # push the hypothesis likelihood to/over the 0.6 cause-identified gate
        # (else a runbook alone would satisfy working_conclusion → M5/resolution).
        from faultmaven.core.investigation.runbook_cause_matcher import (
            _MATCHER_MAX_PRIOR,
        )

        case = _case()
        result = _result("kb_rb1", "single", record=_linear_cause("A"))
        result.selected_cause.belief = 1.0
        kb = _FakeKB([result])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
            hypothesis_manager=self._hm(),
        )
        hyp = next(iter(case.hypotheses.values()))
        assert hyp.likelihood == _MATCHER_MAX_PRIOR
        assert hyp.likelihood < 0.6  # below the cause-identified gate

    @pytest.mark.asyncio
    async def test_rootless_chain_attaches_no_hypothesis(self):
        # A chain with only intermediates (no ROOT) instantiates nodes but has
        # no root to anchor a hypothesis.
        case = _case()
        cause = CauseRecord(
            cause_letter="A",
            cause_name="n",
            chain_nodes=[
                _node("s1", "intermediate", "an intermediate effect"),
                _node("D", "problem", "the problem"),
            ],
            chain_edges=[_edge("s1", "D")],
        )
        kb = _FakeKB([_result("kb_rb1", "single", record=cause)])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
            hypothesis_manager=self._hm(),
        )
        assert case.hypotheses == {}  # no ROOT → no hypothesis
        assert any(  # but the intermediate node was instantiated
            n.node_type == NodeType.INTERMEDIATE for n in case.causal_nodes.values()
        )

    @pytest.mark.asyncio
    async def test_chain_not_reaching_D_attaches_no_hypothesis(self):
        # A lone root with no edge to D: the root instantiates, but it has no
        # path to D, so no (inconsistent, empty-path) hypothesis is attached.
        case = _case()
        cause = CauseRecord(
            cause_letter="A",
            cause_name="n",
            chain_nodes=[_node("root", "root", "the root cause")],
            chain_edges=[],  # not linked to D
        )
        kb = _FakeKB([_result("kb_rb1", "single", record=cause)])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
            hypothesis_manager=self._hm(),
        )
        assert case.hypotheses == {}  # no root→D path → no hypothesis
        assert any(n.node_type == NodeType.ROOT for n in case.causal_nodes.values())

    @pytest.mark.asyncio
    async def test_no_manager_instantiates_chain_without_hypothesis(self):
        # Backward-compatible: without a manager, instantiate only (4a behavior).
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert any(n.node_type == NodeType.ROOT for n in case.causal_nodes.values())
        assert case.hypotheses == {}


class TestInterventionStash:
    @staticmethod
    def _hm():
        from faultmaven.core.investigation.hypothesis_manager import (
            create_hypothesis_manager,
        )

        return create_hypothesis_manager()

    @pytest.mark.asyncio
    async def test_interventions_stashed_on_root_node(self):
        from faultmaven.core.investigation.runbook_cause_matcher import (
            RUNBOOK_INTERVENTIONS_META_KEY,
        )

        case = _case()
        rec = _linear_cause("A")
        rec.interventions = [
            {"quadrant": "remediation", "ref": "root", "text": "Fix it."}
        ]
        kb = _FakeKB([_result("kb_rb1", "single", record=rec)])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
            hypothesis_manager=self._hm(),
        )
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert roots[0].metadata[RUNBOOK_INTERVENTIONS_META_KEY] == rec.interventions

    @pytest.mark.asyncio
    async def test_no_interventions_leaves_no_metadata_key(self):
        from faultmaven.core.investigation.runbook_cause_matcher import (
            RUNBOOK_INTERVENTIONS_META_KEY,
        )

        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_noop_resolver,
            evaluator=object(),
            question="q",
            user_id="u1",
            hypothesis_manager=self._hm(),
        )
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert RUNBOOK_INTERVENTIONS_META_KEY not in roots[0].metadata


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
    from faultmaven.core.investigation.hypothesis_manager import (
        create_hypothesis_manager,
    )
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.investigation_tools = registry
    eng.knowledge_service = knowledge_service
    eng.hypothesis_manager = create_hypothesis_manager()
    return eng


def _registry(wrapped, *, ce_wrapped=None):
    """A fake AgentToolRegistry: .get('kb_qa') → adapter exposing .wrapped, and
    optionally .get('case_evidence_search') → adapter exposing the T2 tool."""
    kb_adapter = SimpleNamespace(wrapped=wrapped)
    ce_adapter = SimpleNamespace(wrapped=ce_wrapped) if ce_wrapped else None

    def _get(name):
        if name == "kb_qa":
            return kb_adapter
        if name == "case_evidence_search":
            return ce_adapter
        return None

    return SimpleNamespace(get=_get)


class _FakeCaseEvidence:
    """Stand-in for AnswerFromCaseEvidence with the answer_yes_no primitive."""

    def __init__(self, answer=True):
        self._answer = answer
        self.calls = []

    async def answer_yes_no(self, question, scope_id=None, k=5):
        self.calls.append({"question": question, "scope_id": scope_id})
        return self._answer


def _ks():
    async def _resolver(item_id):
        return None

    return SimpleNamespace(get_runbook_causes=_resolver)


# The engine method returns SILENTLY on a clean guard (no tools / no matcher
# method / no knowledge_service / empty question) but logs this warning when it
# *swallows* an exception. So "warning absent" distinguishes a real guard-return
# from a vacuous pass where some unintended error was swallowed.
_SKIP_WARNING = "Runbook cause matcher skipped"


class TestEngineIntegration:
    @pytest.mark.asyncio
    async def test_single_match_instantiates_via_engine(self):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        eng = _engine(registry=_registry(kb), knowledge_service=_ks())

        await eng._apply_runbook_cause_matcher(case, None, {})

        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1
        # The chain is load-bearing: a hypothesis is attached to its root.
        assert len(case.hypotheses) == 1
        hyp = next(iter(case.hypotheses.values()))
        assert hyp.root_node_id == roots[0].node_id
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
    async def test_no_investigation_tools_is_noop(self, caplog):
        case = _case()
        eng = _engine(registry=None, knowledge_service=_ks())
        with caplog.at_level(logging.WARNING):
            await eng._apply_runbook_cause_matcher(case, None, {})  # must not raise
        assert _SKIP_WARNING not in caplog.text  # clean guard, not a swallowed error
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_tool_without_matcher_method_is_noop(self, caplog):
        case = _case()
        eng = _engine(registry=_registry(object()), knowledge_service=_ks())
        with caplog.at_level(logging.WARNING):
            await eng._apply_runbook_cause_matcher(case, None, {})  # must not raise
        assert _SKIP_WARNING not in caplog.text
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_no_knowledge_service_is_noop(self, caplog):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        eng = _engine(registry=_registry(kb), knowledge_service=None)
        with caplog.at_level(logging.WARNING):
            await eng._apply_runbook_cause_matcher(case, None, {})
        assert _SKIP_WARNING not in caplog.text  # guarded out, not swallowed
        assert kb.calls == []  # never reached the tool
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_matcher_exception_is_swallowed(self, caplog):
        case = _case()

        class _BoomKB:
            def __init__(self):
                self.called = False

            async def aget_cause_matches(self, *a, **k):
                self.called = True
                raise RuntimeError("kb boom")

        boom = _BoomKB()
        eng = _engine(registry=_registry(boom), knowledge_service=_ks())
        with caplog.at_level(logging.WARNING):
            await eng._apply_runbook_cause_matcher(case, None, {})  # must not raise
        # The swallow path: reached the tool, raised, and logged the skip warning
        # (distinguishes a real swallow from an unrelated early no-op).
        assert boom.called is True
        assert _SKIP_WARNING in caplog.text
        assert case.causal_nodes == {}


class TestEngineFiringAndGuards:
    """4b-2: T2 case_evidence_qa wiring, the per-case skip-guard, and top-1."""

    @pytest.mark.asyncio
    async def test_case_evidence_qa_is_wired_and_calls_tool_correctly(self):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "none")])  # verdict doesn't matter here
        ce = _FakeCaseEvidence(answer=True)
        eng = _engine(registry=_registry(kb, ce_wrapped=ce), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})
        # The evaluator handed to the matcher carries a live T2 resolver, and the
        # firing path is bounded to one runbook.
        evaluator = kb.calls[0]["evaluator"]
        assert evaluator._case_evidence_qa is not None
        assert kb.calls[0]["max_runbooks"] == 1
        # Exercise the closure end-to-end: it must call the case-evidence tool
        # scoped to THIS case (not user_id) with the indicator question.
        result = await evaluator._case_evidence_qa("does the evidence show X?")
        assert result is True
        assert ce.calls[0]["scope_id"] == case.case_id
        assert ce.calls[0]["question"] == "does the evidence show X?"

    @pytest.mark.asyncio
    async def test_no_case_evidence_tool_leaves_resolver_none(self):
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "none")])
        eng = _engine(registry=_registry(kb), knowledge_service=_ks())  # no ce tool
        await eng._apply_runbook_cause_matcher(case, None, {})
        assert kb.calls[0]["evaluator"]._case_evidence_qa is None

    @pytest.mark.asyncio
    async def test_skips_when_cause_state_identified(self):
        from faultmaven.modules.case.contracts import CauseState

        case = _case()
        case.progress.cause_state = CauseState.IDENTIFIED
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        ce = _FakeCaseEvidence()
        eng = _engine(registry=_registry(kb, ce_wrapped=ce), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})
        assert kb.calls == []  # expensive match never ran
        assert case.causal_nodes == {}

    @pytest.mark.asyncio
    async def test_skips_when_runbook_match_hypothesis_exists(self):
        # The per-case skip-guard: once a runbook-match hypothesis exists, don't
        # re-run the LLM-heavy match.
        case = _case()
        kb1 = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        ce = _FakeCaseEvidence()
        eng = _engine(registry=_registry(kb1, ce_wrapped=ce), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})  # first turn: matches
        from faultmaven.core.investigation.runbook_cause_matcher import (
            is_runbook_match_hypothesis,
        )

        assert any(is_runbook_match_hypothesis(h) for h in case.hypotheses.values())

        kb2 = _FakeKB([_result("kb_rb2", "single", record=_linear_cause("B"))])
        eng2 = _engine(registry=_registry(kb2, ce_wrapped=ce), knowledge_service=_ks())
        await eng2._apply_runbook_cause_matcher(case, None, {})  # second turn
        assert kb2.calls == []  # guarded out — match did not re-run

    @pytest.mark.asyncio
    async def test_skip_guard_signal_persists_via_rationale(self):
        # The guard keys on rationale (a persisted hypotheses column), so it
        # survives a case reload between turns — unlike a fresh model field.
        from faultmaven.core.investigation.runbook_cause_matcher import (
            RUNBOOK_MATCH_RATIONALE_PREFIX,
            is_runbook_match_hypothesis,
        )

        case = _case()
        kb = _FakeKB([_result("kb_rb1", "single", record=_linear_cause("A"))])
        ce = _FakeCaseEvidence()
        eng = _engine(registry=_registry(kb, ce_wrapped=ce), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})
        hyp = next(iter(case.hypotheses.values()))
        # The marker is the *leading* rationale text (what persists + is matched).
        assert hyp.rationale.startswith(RUNBOOK_MATCH_RATIONALE_PREFIX)
        assert is_runbook_match_hypothesis(hyp) is True

    def test_llm_authored_hypothesis_is_not_a_match(self):
        from faultmaven.core.investigation.hypothesis_manager import (
            create_hypothesis_manager,
        )
        from faultmaven.core.investigation.runbook_cause_matcher import (
            is_runbook_match_hypothesis,
        )

        hyp = create_hypothesis_manager().create_hypothesis(
            statement="some LLM cause",
            category="code",
            initial_likelihood=0.5,
            current_turn=1,
            rationale="The deploy preceded the errors.",
        )
        assert is_runbook_match_hypothesis(hyp) is False
