"""A refutation stands only on data on record (#1911).

Owner rulings (2026-10-10): the engine refuses REFUTED without data behind it,
and evidence already on record counts. A ``refutation_reason`` is an argument;
"rejections are isolated to app-02, so it is not a signing-key problem" reads
exactly like a sound exclusion and excludes nothing. So ``state=REFUTED`` is
applied only when an evidence row (submitted data, #1930) is linked REFUTES,
unhedged, to the hypothesis or its chain, this turn or earlier, or when a
failed fix of it is recorded (the one disproof with no row of its own).

Driven through the real ``_apply_investigation_updates`` path where the order
matters: the refutation is judged after both link passes, so a turn can add a
competitor, link the row that excludes it and refute it at once (plan item 3),
and the competitor's own REFUTES link must not retire it first.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    refuting_evidence_on_record,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_hypothesis_updates,
    _apply_pending_refutations,
)
from faultmaven.core.investigation.milestone_engine.response_application import (
    ResponseApplier,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    EvidenceToAdd,
    EvidenceTrail,
    HypothesisEvidenceLinkToAdd,
    HypothesisToAdd,
    HypothesisUpdate,
    InvestigationResponse_Diagnosis,
    MilestoneJustifications,
    MilestoneUpdates,
    NodeEvidenceLinkToAdd,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemStatus,
    ProblemVerification,
    UploadedFile,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _failed_fix,
    _identified_case,
)

pytestmark = pytest.mark.unit

_FILE_ID = "file_0123456789ab"
_DSU = InvestigationResponse_Diagnosis.DiagnosisStateUpdate
_UPDATES = "faultmaven.core.investigation.milestone_engine.hypothesis_updates"

_CAUSE = "chronyd is disabled on app-02 and its clock runs 247 s fast"
_COMPETITOR = "app-02 signs tokens with a stale or different signing key"


class _NoKb:
    async def prefetch_kb_context(self, *args, **kwargs) -> None:
        return None


def _engine() -> MilestoneEngine:
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.deps = EngineDeps()
    eng.deps.llm_provider = None
    eng.deps.team_service = None
    eng.deps.share_repository = None
    eng.deps.conversion_service = None
    eng.deps.hypothesis_manager = HypothesisManager()
    eng.responses = ResponseApplier(deps=eng.deps, kb_prefetcher=_NoKb())
    return eng


def _case(status: ProblemStatus = ProblemStatus.VERIFIED) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="a third of logins fail with 401",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="a third of logins fail with 401",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="a third of logins fail with 401",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 5
    case.uploaded_files.append(
        UploadedFile(
            file_id=_FILE_ID, filename="gateway.log", size_bytes=512, uploaded_at_turn=1
        )
    )
    case.progress.problem_status = status
    return case


def _meta() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "evidence_slots": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


class _Response:
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


def _row(category: EvidenceCategory, label: str) -> EvidenceToAdd:
    return EvidenceToAdd(
        summary=f"fact-{label} metric-{label} reading-{label}",
        extract=f"observation {label}: value-{label} at host-{label}",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=_FILE_ID,
    )


def _on_record(case: Case, summary: str) -> str:
    """A row recorded on an earlier turn, from the submitted gateway log."""
    ev_id = f"ev_{uuid4().hex[:12]}"
    case.evidence.append(
        Evidence(
            evidence_id=ev_id,
            summary=summary,
            primary_purpose="diagnosis",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            source_file_id=_FILE_ID,
            collected_by="llm",
            collected_at_turn=1,
        )
    )
    return ev_id


def _standing(case: Case, statement: str = _COMPETITOR, likelihood=0.4) -> str:
    h = HypothesisManager().create_hypothesis(
        statement=statement,
        category=HypothesisCategory.SECURITY,
        initial_likelihood=likelihood,
        current_turn=3,
    )
    case.hypotheses[h.hypothesis_id] = h
    return h.hypothesis_id


def _refute(h_id: str, reason: str = "the rejection reason is a time check"):
    return HypothesisUpdate(
        hypothesis_id=h_id, state=HypothesisState.REFUTED, refutation_reason=reason
    )


def _link(h_ref: str, e_ref: str, confidence: float = 1.0):
    return HypothesisEvidenceLinkToAdd(
        hypothesis_id_ref=h_ref,
        evidence_id_ref=e_ref,
        stance=EvidenceStance.REFUTES,
        reasoning="'token is not valid yet' runs after the signature verified",
        stance_confidence=confidence,
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


async def test_an_argument_alone_does_not_refute():
    """The charter-1 shape: "isolated to app-02, so not a key problem", with
    nothing linked. The competitor stays open and the model is told why."""
    eng, case = _engine(), _case()
    h_id = _standing(case)

    with patch(f"{_UPDATES}.hypothesis_refutation_refused_total") as counter:
        meta = await _apply(
            eng,
            case,
            _DSU(
                hypotheses_to_update=[
                    _refute(h_id, "rejections are isolated to tokens from app-02")
                ]
            ),
        )

    h = case.hypotheses[h_id]
    assert h.state == HypothesisState.ACTIVE
    assert h.refutation_reason is None
    assert f"Hypothesis {h_id} was NOT refuted" in meta["system_feedback"]
    assert "It stays open." in meta["system_feedback"]
    counter.labels.assert_called_once_with(source="model")


async def test_evidence_already_on_record_backs_a_refutation():
    """Ruling 2: the rejection reason the user pasted on turn 1 is on record.
    Linking it now is all the refutation needs; no fresh request."""
    eng, case = _engine(), _case()
    h_id = _standing(case)
    reason_row = _on_record(
        case, 'gateway rejects with "token is not valid yet", kid 2026-09'
    )

    meta = await _apply(
        eng,
        case,
        _DSU(
            hypothesis_evidence_links=[_link(h_id, reason_row)],
            hypotheses_to_update=[_refute(h_id)],
        ),
    )

    h = case.hypotheses[h_id]
    assert h.state == HypothesisState.REFUTED
    assert [link.evidence_id for link in h.evidence_links] == [reason_row]
    assert "NOT refuted" not in meta.get("system_feedback", "")


async def test_a_refutes_link_from_an_earlier_turn_backs_a_refutation():
    eng, case = _engine(), _case()
    h_id = _standing(case)
    reason_row = _on_record(case, "gateway rejects with token is not valid yet")
    case.hypotheses[h_id].evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h_id,
            evidence_id=reason_row,
            stance=EvidenceStance.REFUTES,
            reasoning="a time check, after the signature verified",
            stance_confidence=0.9,
        )
    )

    await _apply(eng, case, _DSU(hypotheses_to_update=[_refute(h_id)]))

    assert case.hypotheses[h_id].state == HypothesisState.REFUTED


async def test_a_low_prior_competitor_is_refuted_not_retired_by_its_own_link():
    """A competitor at 0.2 drops to 0.0 on its REFUTES link, under the 0.3
    soft-retirement bar. Retired first, it would be terminal when the
    refutation is judged and the disproof would be lost (held, then refuted)."""
    eng, case = _engine(), _case()
    h_id = _standing(case, likelihood=0.2)
    reason_row = _on_record(case, "gateway rejects with token is not valid yet")

    await _apply(
        eng,
        case,
        _DSU(
            hypothesis_evidence_links=[_link(h_id, reason_row)],
            hypotheses_to_update=[_refute(h_id)],
        ),
    )

    assert case.hypotheses[h_id].state == HypothesisState.REFUTED


async def test_a_hedged_refutes_link_does_not_refute_and_the_held_retire_lands():
    """A self-hedged REFUTES link is not a disproof. The refutation is refused,
    and the soft transition the link pass held then applies: at 0.0 the
    competitor is set aside (RETIRED), and the feedback says so."""
    eng, case = _engine(), _case()
    h_id = _standing(case, likelihood=0.2)
    reason_row = _on_record(case, "gateway rejects with token is not valid yet")

    meta = await _apply(
        eng,
        case,
        _DSU(
            hypothesis_evidence_links=[_link(h_id, reason_row, confidence=0.4)],
            hypotheses_to_update=[_refute(h_id)],
        ),
    )

    h = case.hypotheses[h_id]
    assert h.state == HypothesisState.RETIRED
    assert h.refutation_reason is None
    assert "self-hedged" in meta["system_feedback"]
    assert "set it aside (RETIRED)" in meta["system_feedback"]


async def test_a_held_transition_without_a_refutation_is_not_held():
    """Only a hypothesis refuted in the same emission is held: a plain REFUTES
    link still retires a low-prior hypothesis in the link pass."""
    eng, case = _engine(), _case()
    h_id = _standing(case, likelihood=0.2)
    reason_row = _on_record(case, "gateway rejects with token is not valid yet")

    await _apply(eng, case, _DSU(hypothesis_evidence_links=[_link(h_id, reason_row)]))

    assert case.hypotheses[h_id].state == HypothesisState.RETIRED


# ---------------------------------------------------------------------------
# Opportunism (binding): one turn verifies, identifies, and rules a
# competitor out
# ---------------------------------------------------------------------------


async def test_one_turn_verifies_identifies_and_refutes_a_competitor():
    """Plan item 3 + the opportunism pin: the symptom verified, the cause
    hypothesized, chained and grounded on two independent causal rows, and a
    competitor added and refuted on a third row, all in one emission, through
    ``new_index_N`` refs. The competitor is added below the soft-retirement
    bar, so the held transition is exercised too."""
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
                _row(EvidenceCategory.SYMPTOM_EVIDENCE, "reason"),
            ],
            hypotheses_to_add=[
                HypothesisToAdd(
                    statement=_CAUSE,
                    category=HypothesisCategory.CONFIG,
                    likelihood=0.6,
                    rationale="iat/nbf run 247 s ahead on app-02 tokens",
                    root_node_ref="new_index_0",
                ),
                HypothesisToAdd(
                    statement=_COMPETITOR,
                    category=HypothesisCategory.SECURITY,
                    likelihood=0.15,
                    rationale="rejections are isolated to app-02 tokens",
                ),
            ],
            causal_nodes_to_add=[
                CausalNodeToAdd(statement=_CAUSE, node_type="root", produces="D")
            ],
            node_evidence_links=[
                NodeEvidenceLinkToAdd(
                    node_ref="new_index_0",
                    evidence_id_ref=f"new_index_{i}",
                    stance=EvidenceStance.SUPPORTS,
                    reasoning="the clock offset matches the iat skew",
                )
                for i in (1, 2)
            ],
            hypothesis_evidence_links=[_link("new_index_1", "new_index_3")],
            hypotheses_to_update=[_refute("new_index_1")],
        ),
        _Response("401s on POST /login in the gateway log"),
    )

    assert case.progress.problem_status == ProblemStatus.VERIFIED
    cause, competitor = (case.hypotheses[h_id] for h_id in meta["hypotheses_generated"])
    assert competitor.state == HypothesisState.REFUTED
    assert [link.evidence_id for link in competitor.evidence_links] == [
        meta["evidence_added"][3]
    ]
    assert cause.root_node_id is not None
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert "NOT" not in meta.get("system_feedback", "")


# ---------------------------------------------------------------------------
# The failed-fix exception
# ---------------------------------------------------------------------------


def _judge(case, h_id, reason="the fix had no effect on the 503s") -> dict:
    meta = _meta()
    manager = HypothesisManager()
    _apply_hypothesis_updates(
        manager, case, [_refute(h_id, reason)], meta, case.current_turn
    )
    _apply_pending_refutations(manager, case, meta, case.current_turn)
    return meta


def test_a_recorded_failed_fix_of_the_cause_backs_its_refutation():
    """The failed fix has no row of its own to link (the treatment prompt
    records none): the case's fix window is the disproof (#1927)."""
    case, _root, hyp = _identified_case()
    _failed_fix(case)
    case.current_turn = 9

    _judge(case, hyp.hypothesis_id)

    assert hyp.state == HypothesisState.REFUTED
    assert hyp.refutation_reason == "the fix had no effect on the 503s"


def test_a_failed_fix_does_not_back_refuting_a_hypothesis_it_did_not_target():
    """The window is a fix of the identified cause. A competitor on its own
    chain, which the fix's rows are not about, is not disproved by it."""
    case, _root, _hyp = _identified_case()
    other_root = CausalNode(
        node_id="cn_0000000000bb",
        statement="pgbouncer pool_mode transaction drops prepared statements",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        generated_at_turn=2,
    )
    case.causal_nodes[other_root.node_id] = other_root
    other_id = _standing(case, "pgbouncer drops prepared statements under load")
    case.hypotheses[other_id].root_node_id = other_root.node_id
    _failed_fix(case)
    case.current_turn = 9

    _judge(case, other_id)

    assert case.hypotheses[other_id].state == HypothesisState.ACTIVE


def test_no_failed_fix_without_the_cause_seen_removed():
    """ "I ran it, still failing" with no re-check of the cause may be a fix
    not applied as intended, which disproves nothing (#1927's predicate)."""
    case, _root, hyp = _identified_case()
    _failed_fix(case)
    case.evidence = [e for e in case.evidence if "max_connections now" not in e.summary]
    case.current_turn = 9

    _judge(case, hyp.hypothesis_id)

    assert hyp.state != HypothesisState.REFUTED


# ---------------------------------------------------------------------------
# What counts as refuting evidence on record
# ---------------------------------------------------------------------------


def _bare(case: Case) -> tuple:
    h_id = _standing(case)
    return case.hypotheses[h_id], _on_record(case, "gateway: token is not valid yet")


def test_a_link_to_a_row_not_on_record_does_not_count():
    case = _case()
    h, _row_id = _bare(case)
    h.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=h.hypothesis_id,
            evidence_id="ev_gone00000000",
            stance=EvidenceStance.REFUTES,
            reasoning="r",
            stance_confidence=1.0,
        )
    )
    assert refuting_evidence_on_record(case, h) == []


def test_a_refutes_link_on_a_rung_of_the_chain_counts():
    """Refuting any rung breaks the chain (methodology S1), so a REFUTES link
    on an intermediate node of the hypothesis's path backs its refutation."""
    case = _case()
    h, row_id = _bare(case)
    for node_id, node_type in (
        ("cn_0000000000a1", NodeType.ROOT),
        ("cn_0000000000a2", NodeType.INTERMEDIATE),
    ):
        case.causal_nodes[node_id] = CausalNode(
            node_id=node_id,
            statement=f"{node_type.value} of the key chain",
            node_type=node_type,
            generated_at_turn=3,
        )
    h.root_node_id = "cn_0000000000a1"
    h.path = ["cn_0000000000a1", "cn_0000000000a2"]
    case.causal_nodes["cn_0000000000a2"].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=row_id,
            stance=EvidenceStance.REFUTES,
            reasoning="the signature verified before the time check",
            linked_at_turn=4,
        )
    )
    assert refuting_evidence_on_record(case, h) == [row_id]


def test_supports_and_neutral_links_do_not_count():
    case = _case()
    h, row_id = _bare(case)
    for stance in (EvidenceStance.SUPPORTS, EvidenceStance.NEUTRAL):
        h.evidence_links = [
            HypothesisEvidenceLink(
                hypothesis_id=h.hypothesis_id,
                evidence_id=row_id,
                stance=stance,
                reasoning="r",
                stance_confidence=1.0,
            )
        ]
        assert refuting_evidence_on_record(case, h) == []


# ---------------------------------------------------------------------------
# Review round: the chain axis, and the node every chain shares
# ---------------------------------------------------------------------------


def _two_chains_dsu(**extra) -> "_DSU":
    """Two hypotheses, each with its own root producing D, and a row."""
    return _DSU(
        evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "isolated")],
        hypotheses_to_add=[
            HypothesisToAdd(
                statement=_CAUSE,
                category=HypothesisCategory.CONFIG,
                likelihood=0.5,
                rationale="iat/nbf skew",
                root_node_ref="new_index_0",
            ),
            HypothesisToAdd(
                statement=_COMPETITOR,
                category=HypothesisCategory.SECURITY,
                likelihood=0.5,
                rationale="isolated to app-02",
                root_node_ref="new_index_1",
            ),
        ],
        causal_nodes_to_add=[
            CausalNodeToAdd(statement=_CAUSE, node_type="root", produces="D"),
            CausalNodeToAdd(statement=_COMPETITOR, node_type="root", produces="D"),
        ],
        **extra,
    )


async def test_a_refutes_link_on_the_problem_node_backs_no_refutation():
    """D ends every hypothesis's path. A REFUTES link there must not count as
    refuting any chain, or one link would refute every candidate at once."""
    eng, case = _engine(), _case()
    meta = await _apply(eng, case, _two_chains_dsu())
    (problem_id,) = [
        nid for nid, n in case.causal_nodes.items() if n.node_type == NodeType.PROBLEM
    ]
    cause_id, competitor_id = meta["hypotheses_generated"]
    assert case.hypotheses[competitor_id].path[-1] == problem_id
    row_id = meta["evidence_added"][0]

    meta = await _apply(
        eng,
        case,
        _DSU(
            node_evidence_links=[
                NodeEvidenceLinkToAdd(
                    node_ref=problem_id,
                    evidence_id_ref=row_id,
                    stance=EvidenceStance.REFUTES,
                    reasoning="isolated to app-02",
                )
            ],
            hypotheses_to_update=[_refute(cause_id), _refute(competitor_id)],
        ),
    )

    assert case.hypotheses[cause_id].state == HypothesisState.ACTIVE
    assert case.hypotheses[competitor_id].state == HypothesisState.ACTIVE
    assert refuting_evidence_on_record(case, case.hypotheses[competitor_id]) == []


async def test_a_same_turn_link_on_the_chain_root_alone_backs_a_refutation():
    """The refutation is judged after chain emission too, not only after the
    flat link pass: a competitor's root linked REFUTES through
    ``node_evidence_links`` in the same emission is the ground."""
    eng, case = _engine(), _case()
    meta = await _apply(
        eng,
        case,
        _two_chains_dsu(
            node_evidence_links=[
                NodeEvidenceLinkToAdd(
                    node_ref="new_index_1",
                    evidence_id_ref="new_index_0",
                    stance=EvidenceStance.REFUTES,
                    reasoning="a time check runs after the signature verified",
                )
            ],
            hypotheses_to_update=[_refute("new_index_1")],
        ),
    )

    competitor = case.hypotheses[meta["hypotheses_generated"][1]]
    assert competitor.state == HypothesisState.REFUTED
    assert "NOT refuted" not in meta.get("system_feedback", "")
