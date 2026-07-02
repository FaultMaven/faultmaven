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

from faultmaven.core.investigation.causal_graph import ingest_emitted_chain
from faultmaven.core.investigation.cause_schemas import (
    CauseMatch,
    CauseMatchResult,
    CauseRecord,
)
from faultmaven.core.investigation.runbook_cause_matcher import (
    _record_differential_runbook,
    apply_runbook_cause_matcher,
    build_case_evidence_fallback_text,
    chain_to_specs,
    differential_runbook_ids,
    instantiate_cause_chain,
    resolve_root,
)
from faultmaven.core.investigation.schemas import CausalNodeToAdd
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
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

    def test_dropped_edge_is_logged_not_silent(self, caplog):
        # M1: a dropped edge (the common case is an unsupported cross-cause
        # `converges:` edge) must not vanish silently — it is logged so a
        # malformed/cross-cause chain stays diagnosable.
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[_node("root", "root", "r")],
            chain_edges=[_edge("root", "other_cause_node")],
        )
        with caplog.at_level(logging.WARNING):
            _, edges = chain_to_specs(cause)
        assert edges == []
        assert any("Dropping chain edge" in r.message for r in caplog.records)

    def test_non_string_statement_node_skipped_not_coerced(self, caplog):
        # L3: a non-string statement (malformed pack nesting a dict) must be
        # skipped, never `str()`-coerced into a node whose text is a Python repr.
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[
                {"ref": "root", "node_type": "root", "statement": {"nested": "x"}},
                _node("s1", "intermediate", "real state"),
            ],
            chain_edges=[],
        )
        with caplog.at_level(logging.WARNING):
            nodes, _ = chain_to_specs(cause)
        assert [n.statement for n in nodes] == ["real state"]
        assert not any("nested" in n.statement for n in nodes)
        assert any(
            "Non-string chain-node statement" in r.message for r in caplog.records
        )

    def test_multiple_roots_warns_but_instantiates(self, caplog):
        # L3: a linear chain has exactly one ROOT; multiple ROOTs signal a
        # malformed pack — log it, but still instantiate as authored (root
        # semantics belong to the engine, not this converter).
        cause = CauseRecord(
            cause_letter="A",
            chain_nodes=[
                _node("r1", "root", "first root"),
                _node("r2", "root", "second root"),
                _node("D", "problem", "the problem"),
            ],
            chain_edges=[],
        )
        with caplog.at_level(logging.WARNING):
            nodes, _ = chain_to_specs(cause)
        assert len(nodes) == 2  # both roots instantiated
        assert any("ROOT nodes" in r.message for r in caplog.records)


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
    """Stand-in for AnswerFromKB.aget_cause_matches + aget_retrieved_runbook_ids.

    ``retrieved_ids`` seeds the retrieval-only ranking Part A reads; when not given it
    defaults to the runbook ids of ``results`` (the realistic case — the T2-evaluated
    set is drawn from the retrieved set). Whether a retrieved id is actually seeded
    still depends on the ``resolve_causes`` v4-filter in the matcher.
    """

    def __init__(self, results, *, retrieved_ids=None):
        self._results = results
        self._retrieved_ids = (
            retrieved_ids
            if retrieved_ids is not None
            else [r.runbook_id for r in results]
        )
        self.calls = []
        self.retrieved_calls = []

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

    async def aget_retrieved_runbook_ids(
        self, question, user_id, *, team_ids=None, top_k=3
    ):
        self.retrieved_calls.append({"question": question, "top_k": top_k})
        return list(self._retrieved_ids[:top_k])


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


def _v4_resolver(*v4_ids):
    """A resolve_causes stub: returns a non-empty cause list (v4-matchable) for the
    named ids, None otherwise — so the matcher's v4-filter seeds only the named ids."""

    async def _resolve(item_id):
        return [{"cause_letter": "A"}] if item_id in v4_ids else None

    return _resolve


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
        # The matched runbook is recorded (structured) for differential re-resolution.
        assert differential_runbook_ids(case) == ["kb_rb1"]
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
    async def test_none_verdict_seeds_differential_from_retrieval(self):
        # Part A / RC-1 fix: on a 'none' verdict the T2 path instantiates nothing, but
        # the differential is now seeded from RETRIEVAL (all v4-matchable top-K ids), so
        # the deterministic intake loop still has candidates to validate against.
        case = _case()
        kb = _FakeKB(
            [_result("kb_rb1", "none")], retrieved_ids=["kb_rb1", "kb_rb2", "kb_rb3"]
        )
        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_v4_resolver("kb_rb1", "kb_rb2", "kb_rb3"),
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert chosen is None  # graph prior unchanged: nothing instantiated
        assert case.causal_nodes == {}
        # ...but the differential is seeded from retrieval, and the marker is set.
        assert differential_runbook_ids(case) == ["kb_rb1", "kb_rb2", "kb_rb3"]
        assert case.runbook_retrieved is True

    @pytest.mark.asyncio
    async def test_retrieval_seed_v4_filters_non_v4_runbooks(self):
        # A retrieved runbook with no resolvable v4 causes (upload-path / pre-v4) is
        # dropped from the seed — only groundable candidates enter the differential.
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "multiple")], retrieved_ids=["kb_rb1", "kb_v3"])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_v4_resolver("kb_rb1"),  # kb_v3 resolves to None → dropped
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert differential_runbook_ids(case) == ["kb_rb1"]
        assert case.runbook_retrieved is True

    @pytest.mark.asyncio
    async def test_marker_not_set_when_no_v4_runbook_seeded(self):
        # Retrieval returned ids but none are v4-matchable → nothing seeded, and the
        # denominator marker stays False (an all-non-v4 case can never ground).
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "none")], retrieved_ids=["kb_v3a", "kb_v3b"])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_v4_resolver(),  # nothing is v4
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert differential_runbook_ids(case) == []
        assert case.runbook_retrieved is False

    @pytest.mark.asyncio
    async def test_single_winner_included_in_retrieval_seed(self):
        # The 'single' winner is seeded via retrieval (and the defensive tail re-seed is
        # a dedup no-op): the differential holds the whole v4 retrieved set, winner first.
        case = _case()
        record = _linear_cause("A")
        kb = _FakeKB(
            [_result("kb_rb1", "single", record=record)],
            retrieved_ids=["kb_rb1", "kb_rb2"],
        )
        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_v4_resolver("kb_rb1", "kb_rb2"),
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert chosen is not None and chosen.runbook_id == "kb_rb1"
        # winner's chain instantiated (graph prior), differential = full v4 retrieved set
        roots = [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]
        assert len(roots) == 1
        assert differential_runbook_ids(case) == ["kb_rb1", "kb_rb2"]  # no duplicate

    @pytest.mark.asyncio
    async def test_seed_passes_differential_top_k(self):
        # The seeding path requests exactly differential_top_k runbooks from retrieval.
        case = _case()
        kb = _FakeKB([_result("kb_rb1", "none")], retrieved_ids=["kb_rb1"])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_v4_resolver("kb_rb1"),
            evaluator=object(),
            question="q",
            user_id="u1",
            differential_top_k=5,
        )
        assert kb.retrieved_calls[0]["top_k"] == 5

    @pytest.mark.asyncio
    async def test_seed_is_once_per_case_not_per_call(self):
        # Seed-once: a second matcher pass on the same case (none verdict, so nothing
        # instantiated and the engine skip-guard wouldn't block re-entry) does NOT
        # re-seed — even with drifted retrieval — and short-circuits before retrieval.
        case = _case()
        kb1 = _FakeKB([_result("kb_a", "none")], retrieved_ids=["kb_a"])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb1,
            resolve_causes=_v4_resolver("kb_a"),
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert differential_runbook_ids(case) == ["kb_a"]

        kb2 = _FakeKB([_result("kb_b", "none")], retrieved_ids=["kb_b", "kb_c"])
        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb2,
            resolve_causes=_v4_resolver("kb_b", "kb_c"),
            evaluator=object(),
            question="q-drifted",
            user_id="u1",
        )
        assert differential_runbook_ids(case) == ["kb_a"]  # unchanged — seeded once
        assert kb2.retrieved_calls == []  # retrieval not even attempted on re-entry

    @pytest.mark.asyncio
    async def test_retrieval_seed_drops_unbuildable_causes(self):
        # v4-filter uses build_cause_records, not merely "resolve_causes non-empty":
        # a runbook whose raw causes build NO CauseRecord (non-dict / malformed) is
        # dropped from the seed, matching aget_cause_matches's own gate.
        case = _case()
        kb = _FakeKB([_result("kb_ok", "none")], retrieved_ids=["kb_ok", "kb_bad"])

        async def _resolve(item_id):
            if item_id == "kb_ok":
                return [{"cause_letter": "A"}]
            return ["not-a-dict-entry"]  # non-empty but unbuildable → dropped

        await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_resolve,
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert differential_runbook_ids(case) == ["kb_ok"]

    @pytest.mark.asyncio
    async def test_tail_seed_marks_denominator_when_seed_block_empty(self):
        # Regression: if the retrieval-seed yields nothing this turn (e.g. a transient
        # retrieval miss → aget_retrieved_runbook_ids returns []) but the T2 path finds a
        # 'single' winner, the differential is populated only by the tail. The
        # runbook_retrieved denominator marker must still be set — otherwise the seed-once
        # guard blocks it from ever running again and the grounded case is silently
        # excluded from the R3 denominator for life.
        case = _case()
        record = _linear_cause("A")
        kb = _FakeKB([_result("kb_win", "single", record=record)], retrieved_ids=[])
        chosen = await apply_runbook_cause_matcher(
            case,
            kb_tool=kb,
            resolve_causes=_v4_resolver("kb_win"),
            evaluator=object(),
            question="q",
            user_id="u1",
        )
        assert chosen is not None and chosen.runbook_id == "kb_win"
        assert differential_runbook_ids(case) == ["kb_win"]  # populated via the tail
        assert case.runbook_retrieved is True  # marker set after the tail, not skipped

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


class TestSoundnessInvariant:
    def test_matcher_cap_strictly_below_cause_identified_gate(self):
        # L1: the matcher's instantiated prior MUST sit strictly below the
        # cause-identified likelihood gate, so a runbook match ALONE can never
        # trip resolution ("no incorrect conclusion"). Both constants now share a
        # single source of truth (terminal_transitions.CAUSE_IDENTIFIED_LIKELIHOOD)
        # — this pins their relationship so a drift fails the build.
        from faultmaven.core.investigation.runbook_cause_matcher import (
            _MATCHER_MAX_PRIOR,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            CAUSE_IDENTIFIED_LIKELIHOOD,
        )

        assert _MATCHER_MAX_PRIOR < CAUSE_IDENTIFIED_LIKELIHOOD


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

    async def answer_yes_no(self, question, scope_id=None, k=5, fallback_context=None):
        self.calls.append(
            {
                "question": question,
                "scope_id": scope_id,
                "fallback_context": fallback_context,
            }
        )
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

            async def aget_retrieved_runbook_ids(self, *a, **k):
                return []  # benign: the boom under test is aget_cause_matches

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
    async def test_t2_closure_passes_raw_evidence_fallback(self):
        # #543: the closure must hand the tool a fallback built from case.evidence
        # so T2 can judge even when the vector collection is empty.
        case = _case()
        case.evidence.append(
            _ev(
                "PreSync job fails on SSL",
                extract="FATAL: SSL connection is required",
                category=EvidenceCategory.CAUSAL_EVIDENCE,
            )
        )
        kb = _FakeKB([_result("kb_rb1", "none")])
        ce = _FakeCaseEvidence(answer=True)
        eng = _engine(registry=_registry(kb, ce_wrapped=ce), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})
        evaluator = kb.calls[0]["evaluator"]
        await evaluator._case_evidence_qa("is SSL required?")
        fb = ce.calls[0]["fallback_context"]
        assert fb is not None
        assert "PreSync job fails on SSL" in fb
        assert "FATAL: SSL connection is required" in fb

    @pytest.mark.asyncio
    async def test_t2_closure_fallback_none_when_no_evidence(self):
        # No evidence → fallback is None (tool keeps its abstain-on-empty behavior).
        case = _case()  # no evidence
        kb = _FakeKB([_result("kb_rb1", "none")])
        ce = _FakeCaseEvidence(answer=False)
        eng = _engine(registry=_registry(kb, ce_wrapped=ce), knowledge_service=_ks())
        await eng._apply_runbook_cause_matcher(case, None, {})
        await kb.calls[0]["evaluator"]._case_evidence_qa("q")
        assert ce.calls[0]["fallback_context"] is None

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


# ---------------------------------------------------------------------------
# T2 raw-evidence fallback (#543) — build_case_evidence_fallback_text
# ---------------------------------------------------------------------------


def _ev(summary, *, extract=None, category=EvidenceCategory.CAUSAL_EVIDENCE):
    return Evidence(
        summary=summary,
        extract=extract,
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
    )


class TestBuildCaseEvidenceFallbackText:
    def test_none_when_no_evidence(self):
        case = _case()  # no evidence
        assert build_case_evidence_fallback_text(case) is None

    def test_renders_category_summary_and_extract(self):
        case = _case()
        case.evidence.append(
            _ev(
                "migration job fails on PreSync",
                extract="FATAL: SSL connection is required",
                category=EvidenceCategory.CAUSAL_EVIDENCE,
            )
        )
        case.evidence.append(
            _ev("sync stuck 47m", category=EvidenceCategory.SYMPTOM_EVIDENCE)
        )
        text = build_case_evidence_fallback_text(case)
        assert text is not None
        # category tag + summary present
        assert "[causal_evidence] migration job fails on PreSync" in text
        assert "[symptom_evidence] sync stuck 47m" in text
        # verbatim extract carried through
        assert "FATAL: SSL connection is required" in text

    def test_truncates_on_whole_row_boundary(self):
        case = _case()
        for i in range(50):
            case.evidence.append(_ev(f"finding number {i} " + "x" * 200))
        text = build_case_evidence_fallback_text(case, max_chars=1000)
        assert text is not None
        assert len(text) <= 1000  # hard cap honored
        # It stopped early — not all 50 rows are present.
        assert text.count("finding number") < 50

    def test_oversized_first_row_is_truncated_not_dropped(self):
        # extract has no max_length; a single huge first row must NOT blow the
        # budget (and must not be dropped, leaving an empty result).
        case = _case()
        case.evidence.append(_ev("big finding", extract="y" * 50000))
        text = build_case_evidence_fallback_text(case, max_chars=2000)
        assert text is not None
        assert len(text) == 2000  # capped exactly, first row truncated in place
        assert text.startswith("[causal_evidence] big finding")

    def test_skips_blank_summaries_returns_none(self):
        # Evidence guarantees non-blank summary, but guard defensively: an object
        # whose summary is whitespace contributes nothing.
        from types import SimpleNamespace

        case = _case()
        case.evidence.append(
            SimpleNamespace(summary="   ", extract=None, category=None)
        )
        assert build_case_evidence_fallback_text(case) is None


# ---------------------------------------------------------------------------
# resolve_root — the lazy-promotion seam the intake loop binds to
# ---------------------------------------------------------------------------


def _roots(case):
    return [n for n in case.causal_nodes.values() if n.node_type == NodeType.ROOT]


class TestResolveRoot:
    def test_may_instantiate_seeds_chain_and_returns_root(self):
        case = _case()
        root_id = resolve_root(case, _linear_cause(), may_instantiate=True)

        assert root_id is not None
        node = case.causal_nodes[root_id]
        assert node.node_type == NodeType.ROOT
        assert node.statement == "the root cause"
        assert len(_roots(case)) == 1

    def test_second_supports_is_idempotent_no_duplicate_root(self):
        # A standing cause re-supported on a later turn must resolve to the SAME
        # root, never a second one — the single-identity guarantee on the mint path.
        case = _case()
        cause = _linear_cause()
        first = resolve_root(case, cause, may_instantiate=True)
        node_count = len(case.causal_nodes)

        second = resolve_root(case, cause, may_instantiate=True)

        assert second == first
        assert len(case.causal_nodes) == node_count  # nothing new minted
        assert len(_roots(case)) == 1

    def test_lookup_only_when_not_may_instantiate_has_no_side_effect(self):
        # A REFUTES against an unseen cause must not seed a node to refute.
        case = _case()
        result = resolve_root(case, _linear_cause(), may_instantiate=False)

        assert result is None
        assert case.causal_nodes == {}

    def test_lookup_returns_existing_root_after_instantiation(self):
        case = _case()
        cause = _linear_cause()
        seeded = resolve_root(case, cause, may_instantiate=True)

        # Now a non-promoting check (REFUTES) finds the same standing root.
        found = resolve_root(case, cause, may_instantiate=False)

        assert found == seeded

    def test_degenerate_cause_returns_none_even_when_may_instantiate(self):
        # A cause carrying only the engine-seeded problem node has no instantiable
        # root; the caller must skip the verdict regardless of stance.
        case = _case()
        degenerate = CauseRecord(
            cause_letter="A",
            chain_nodes=[_node("D", "problem", "the problem")],
        )
        assert resolve_root(case, degenerate, may_instantiate=True) is None
        assert case.causal_nodes == {}  # D not even seeded

    def test_cross_author_same_statement_converges_on_one_root(self):
        # A matcher-seeded root and a later verbatim LLM emission of the same root
        # statement reconcile to ONE node — lookup and mint share one identity.
        case = _case()
        seeded = resolve_root(case, _linear_cause(), may_instantiate=True)

        # The in-loop LLM independently emits the same root statement.
        ingest_emitted_chain(
            case,
            [CausalNodeToAdd(statement="the root cause", node_type=NodeType.ROOT)],
            [],
            [],
            case.current_turn,
        )

        assert len(_roots(case)) == 1
        assert _roots(case)[0].node_id == seeded

    def test_cross_author_divergent_wording_fragments_into_two_roots(self):
        # The deliberate exact-match boundary: paraphrases are NOT merged (a fuzzy
        # threshold can't separate a true duplicate from a distinct OR-sibling).
        case = _case()
        resolve_root(case, _linear_cause(), may_instantiate=True)

        ingest_emitted_chain(
            case,
            [
                CausalNodeToAdd(
                    statement="a different root cause", node_type=NodeType.ROOT
                )
            ],
            [],
            [],
            case.current_turn,
        )

        assert len(_roots(case)) == 2


# ---------------------------------------------------------------------------
# differential_runbook_ids — the structured accessor the intake hook binds to
# ---------------------------------------------------------------------------


class TestDifferentialRunbookIds:
    def test_empty_when_nothing_matched(self):
        assert differential_runbook_ids(_case()) == []

    def test_record_appends_and_dedupes_in_order(self):
        case = _case()
        _record_differential_runbook(case, "kb_rb1")
        _record_differential_runbook(case, "kb_rb2")
        _record_differential_runbook(case, "kb_rb1")  # duplicate → ignored
        _record_differential_runbook(case, "")  # empty → ignored
        assert differential_runbook_ids(case) == ["kb_rb1", "kb_rb2"]

    def test_survives_node_pruning(self):
        # The durability fix: the id lives on the case, not a causal node — so
        # clearing the graph (re-root / prune_abandoned_nodes) cannot lose it.
        case = _case()
        instantiate_cause_chain(case, _linear_cause("A"), case.current_turn)
        _record_differential_runbook(case, "kb_rb1")
        case.causal_nodes.clear()  # simulate prune of every node
        assert differential_runbook_ids(case) == ["kb_rb1"]

    def test_accessor_returns_defensive_copy(self):
        case = _case()
        _record_differential_runbook(case, "kb_rb1")
        out = differential_runbook_ids(case)
        out.append("mutation")
        assert differential_runbook_ids(case) == ["kb_rb1"]  # field unchanged
