"""Cause work is accepted only on a verified problem — and one turn can do both.

Driven through the real ``_apply_investigation_updates`` path:

- On an unverified problem, hypotheses, causal chain structure and root-cause
  claims are refused (not queued) and the model is told why. Evidence is kept.
- The gate reads the status the turn ENDS with. A turn that verifies the symptom
  and brings its cause forms ACTIVE hypotheses, emits the chain and reaches
  ``cause_state = IDENTIFIED`` in that same turn: the opportunistic flow
  (owner ruling 2026-10-06). A symptom claim the cited evidence does not
  support is reverted at step 2b, and the cause work of that turn is refused.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph.ingestion import seed_problem_node
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import (
    chain_emission,
    response_application,
)
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.response_application import (
    ResponseApplier,
)
from faultmaven.core.investigation.schemas import (
    CausalEdgeToAdd,
    CausalNodeToAdd,
    DeductiveValidationToAdd,
    EvidenceToAdd,
    EvidenceTrail,
    HypothesisToAdd,
    InvestigationResponse_Diagnosis,
    MilestoneJustifications,
    MilestoneUpdates,
    NodeEvidenceLinkToAdd,
    RootCauseConclusionUpdate,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    CauseState,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisCategory,
    HypothesisState,
    InquiryData,
    NodeState,
    NodeType,
    ProblemStatus,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

_DSU = InvestigationResponse_Diagnosis.DiagnosisStateUpdate

_CAUSE = "checkout-api v2.14.0 retains an unbounded orderSummaryCache"


def _engine() -> MilestoneEngine:
    """Bare engine — the apply path with only the hypothesis manager wired."""
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.deps = EngineDeps()
    # Attributes __init__ always sets and the engine reads directly (#1722).
    eng.deps.llm_provider = None
    eng.deps.team_service = None
    eng.deps.share_repository = None
    eng.deps.conversion_service = None
    eng.deps.hypothesis_manager = HypothesisManager()
    eng.responses = ResponseApplier(deps=eng.deps, kb_prefetcher=_NoKb())
    return eng


class _NoKb:
    """The IDENTIFIED edge warms the KB; nothing here reads what it fetches."""

    async def prefetch_kb_context(self, *args, **kwargs) -> None:
        return None


def _case(status: ProblemStatus) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="checkout orders failing with 500s",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="checkout orders failing with 500s",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout orders failing with 500s",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 5
    case.progress.problem_status = status
    return case


def _meta() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


def _hypothesis(**kw) -> HypothesisToAdd:
    return HypothesisToAdd(
        statement=kw.pop("statement", _CAUSE),
        category=HypothesisCategory.CODE,
        likelihood=0.6,
        rationale="heap grows with every order",
        **kw,
    )


def _row(category: EvidenceCategory, label: str) -> EvidenceToAdd:
    # Label embedded as content tokens so two causal rows read as INDEPENDENT
    # observations under the INV-29 mirror collapse.
    return EvidenceToAdd(
        summary=f"fact-{label} metric-{label} reading-{label}",
        extract=f"observation {label}: value-{label} at host-{label}",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
    )


class _Response:
    """The slice of a response the apply path reads besides state_updates."""

    def __init__(self, justification: str | None = None):
        self.evidence_trail = EvidenceTrail(
            evidence_analyzed=[],
            milestone_justifications=MilestoneJustifications(
                symptom_verified=justification
            ),
        )


async def _apply(eng, case, dsu, response=None) -> dict:
    meta = _meta()
    await eng.responses._apply_investigation_updates(
        case, dsu, meta, response or _Response()
    )
    return meta


def _counter(module):
    # The counter is a no-op unless ENABLE_METRICS — assert the fire via a mock,
    # patched in the namespace the code under test reads.
    return patch.object(module, "cause_work_refused_unverified_total")


# ---------------------------------------------------------------------------
# Refused on an unverified problem
# ---------------------------------------------------------------------------


async def test_hypotheses_on_an_unverified_problem_are_refused_not_queued():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)

    with _counter(response_application) as counter:
        meta = await _apply(eng, case, _DSU(hypotheses_to_add=[_hypothesis()]))

    assert case.hypotheses == {}
    assert meta["hypotheses_generated"] == []
    assert "HYPOTHESES NOT ACCEPTED" in meta["system_feedback"]
    counter.labels.assert_called_once_with(kind="hypothesis")
    counter.labels.return_value.inc.assert_called_once_with(1)


async def test_evidence_on_an_unverified_problem_is_kept():
    """Refusing the cause work never drops the data behind it."""
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)

    await _apply(
        eng,
        case,
        _DSU(
            evidence_to_add=[_row(EvidenceCategory.CAUSAL_EVIDENCE, "c1")],
            hypotheses_to_add=[_hypothesis()],
        ),
    )

    assert case.hypotheses == {}
    assert [e.category for e in case.evidence] == [EvidenceCategory.CAUSAL_EVIDENCE]


async def test_chain_structure_on_an_unverified_problem_is_refused():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)

    with _counter(chain_emission) as counter:
        meta = await _apply(
            eng,
            case,
            _DSU(
                causal_nodes_to_add=[
                    CausalNodeToAdd(statement=_CAUSE, node_type="root", produces="D")
                ]
            ),
        )

    assert [n.node_type for n in case.causal_nodes.values()] == [NodeType.PROBLEM]
    assert case.causal_edges == []
    assert "CAUSAL CHAIN NOT ACCEPTED" in meta["system_feedback"]
    counter.labels.assert_called_once_with(kind="chain")


def _seed_root(case: Case) -> str:
    """A ROOT node and the PROBLEM node already on the graph, so a chain
    emission can name them without adding nodes."""
    seed_problem_node(case)
    root = CausalNode(
        node_id="cn_0000000000aa",
        statement=_CAUSE,
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=1,
    )
    case.causal_nodes[root.node_id] = root
    return root.node_id


async def test_an_edge_between_standing_nodes_on_an_unverified_problem_is_refused():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    root_id = _seed_root(case)

    meta = await _apply(
        eng,
        case,
        _DSU(causal_edges_to_add=[CausalEdgeToAdd(cause=root_id, effect="D")]),
    )

    assert case.causal_edges == []
    assert "CAUSAL CHAIN NOT ACCEPTED" in meta["system_feedback"]


async def test_a_deductive_validation_on_an_unverified_problem_is_refused():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)
    root_id = _seed_root(case)

    meta = await _apply(
        eng,
        case,
        _DSU(
            deductive_validations=[
                DeductiveValidationToAdd(
                    survivor_node_ref=root_id,
                    exhaustive_rationale="every sibling in the differential refuted",
                )
            ]
        ),
    )

    assert "deductive_survivor_ids" not in meta
    assert "CAUSAL CHAIN NOT ACCEPTED" in meta["system_feedback"]


async def test_root_cause_claims_on_an_unverified_problem_are_refused():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)

    with _counter(response_application) as counter:
        meta = await _apply(
            eng,
            case,
            _DSU(
                milestones=MilestoneUpdates(
                    root_cause_likelihood=0.9, root_cause_method="user_provided"
                ),
                root_cause_conclusion=RootCauseConclusionUpdate(
                    root_cause=_CAUSE, mechanism="heap exhaustion", likelihood=0.9
                ),
            ),
        )

    assert case.root_cause_conclusion is None
    assert case.progress.root_cause_method is None
    assert case.progress.root_cause_likelihood == 0.0
    assert "rcc_authored_this_turn" not in meta
    assert "CAUSE WORK NOT ACCEPTED" in meta["system_feedback"]
    counter.labels.assert_called_once_with(kind="conclusion")


async def test_a_verified_problem_accepts_hypotheses_active():
    eng, case = _engine(), _case(ProblemStatus.VERIFIED)

    meta = await _apply(eng, case, _DSU(hypotheses_to_add=[_hypothesis()]))

    (hyp,) = case.hypotheses.values()
    assert hyp.state == HypothesisState.ACTIVE
    assert meta["hypotheses_generated"] == [hyp.hypothesis_id]
    assert "system_feedback" not in meta


# ---------------------------------------------------------------------------
# The gate reads the status the turn ends with
# ---------------------------------------------------------------------------


async def test_a_turn_that_verifies_the_symptom_forms_its_hypotheses():
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)

    meta = await _apply(
        eng,
        case,
        _DSU(
            milestones=MilestoneUpdates(symptom_verified=True),
            evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
            hypotheses_to_add=[_hypothesis()],
        ),
        _Response("500s on POST /checkout in the gateway log"),
    )

    assert case.progress.problem_status == ProblemStatus.VERIFIED
    assert meta["milestones_completed"] == ["symptom_verified"]
    (hyp,) = case.hypotheses.values()
    assert hyp.state == HypothesisState.ACTIVE


async def test_a_symptom_claim_reverted_at_2b_takes_the_turns_cause_work_with_it():
    """No symptom evidence this turn: step 2b reverts the claim, so every gate
    reads UNVERIFIED — the hypothesis and the root-cause claims are refused."""
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)

    meta = await _apply(
        eng,
        case,
        _DSU(
            milestones=MilestoneUpdates(
                symptom_verified=True,
                root_cause_likelihood=0.9,
                root_cause_method="direct_analysis",
            ),
            hypotheses_to_add=[_hypothesis()],
            root_cause_conclusion=RootCauseConclusionUpdate(
                root_cause=_CAUSE, mechanism="heap exhaustion", likelihood=0.9
            ),
        ),
        _Response("the user says checkout is down"),
    )

    assert case.progress.problem_status == ProblemStatus.UNVERIFIED
    assert meta["milestones_completed"] == []
    assert case.hypotheses == {}
    assert case.root_cause_conclusion is None
    assert case.progress.root_cause_method is None
    assert case.progress.root_cause_likelihood == 0.0
    assert "CAUSE WORK NOT ACCEPTED" in meta["system_feedback"]


async def test_one_turn_verifies_forms_grounds_and_identifies():
    """The opportunism pin: verify + hypothesis + chain + two independent causal
    datums in ONE emission reach cause_state IDENTIFIED on that turn."""
    eng, case = _engine(), _case(ProblemStatus.UNVERIFIED)

    meta = await _apply(
        eng,
        case,
        _DSU(
            milestones=MilestoneUpdates(symptom_verified=True),
            evidence_to_add=[
                _row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1"),
                _row(EvidenceCategory.CAUSAL_EVIDENCE, "a1"),
                _row(EvidenceCategory.CAUSAL_EVIDENCE, "a2"),
            ],
            hypotheses_to_add=[_hypothesis(root_node_ref="new_index_0")],
            causal_nodes_to_add=[
                CausalNodeToAdd(statement=_CAUSE, node_type="root", produces="D")
            ],
            node_evidence_links=[
                NodeEvidenceLinkToAdd(
                    node_ref="new_index_0",
                    evidence_id_ref=f"new_index_{i}",
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="the cache grows with every order",
                )
                for i in (1, 2)
            ],
        ),
        _Response("500s on POST /checkout in the gateway log"),
    )

    assert case.progress.problem_status == ProblemStatus.VERIFIED
    (hyp,) = case.hypotheses.values()
    assert hyp.root_node_id is not None
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert "NOT ACCEPTED" not in meta.get("system_feedback", "")
