"""§7.1.2 MECE arbitration (#656): >1 simultaneously-validated DISTINCT
standing roots is a coherence violation (S2 — roots are mutually-exclusive
origins, at most one can be the cause), so case-level identification is HELD
at CANDIDATES pending discrimination — the forward mirror of the §7.1.1
exclusion collapse. Node states are untouched (each root's evidence rules it).

Specimen shapes pinned here come from the live gate runs that motivated the
phase: a prior gate's disp-confirmed-cause-demote run produced THREE
simultaneously-VALIDATED roots left unarbitrated, and a later gate's run of
the same scenario produced DUPLICATE root nodes for one cause — duplicates
must collapse to ONE cause (holding on a statement vs its own restatement
would deadlock: no evidence can ever discriminate them).
"""

import hashlib
import logging
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph import (
    distinct_cause_clusters,
    mece_contested_root_ids,
    retract_stale_engine_rcc,
)
from faultmaven.core.investigation.milestone_engine import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.prompts.context_builder import (
    _build_causal_graph_block,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    RootCauseConclusion,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(
    label, category=EvidenceCategory.CAUSAL_EVIDENCE, summary=None
) -> Evidence:
    # Default summary embeds the label as content tokens so two fixture rows
    # read as INDEPENDENT observations under the §7.1 mirror collapse (INV-29).
    return Evidence(
        evidence_id=_eid(label),
        summary=summary or f"fact-{label} metric-{label} reading-{label}",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _root(node_id, statement, *, support_labels=()) -> CausalNode:
    links = [
        NodeEvidenceLink(
            evidence_id=_eid(label),
            stance=EvidenceStance.SUPPORTS,
            reasoning="bears on the root",
            linked_at_turn=2,
        )
        for label in support_labels
    ]
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=links,
        generated_at_turn=1,
    )


def _hyp(root_node_id, statement, *, hypothesis_id) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        generated_at_turn=1,
    )


def _case(nodes, edges=None, evidence=None, hyps=None) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="checkout orders failing with 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout orders failing with 500s",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.causal_nodes = {n.node_id: n for n in nodes}
    case.causal_edges = edges or []
    case.evidence = evidence or []
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    case.progress.symptom_verified = True
    return case


def _problem(case_id="cn_00000000000d") -> CausalNode:
    return CausalNode(
        node_id=case_id,
        statement="checkout orders failing with 500s",
        node_type=NodeType.PROBLEM,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        belief=1.0,
        generated_at_turn=1,
    )


# The banked gate-run specimen shape: three distinct competing explanations
# for one pool-exhaustion incident, each independently grounded.
_SPECIMEN = [
    (
        "cn_00000000000a",
        "hyp_0000000000aa",
        "the deploy removed the connection release call so pool "
        "connections leak until exhaustion",
        ["ev_a1", "ev_a2"],
    ),
    (
        "cn_00000000000b",
        "hyp_0000000000bb",
        "connection pool max size undersized for current checkout load",
        ["ev_b1", "ev_b2"],
    ),
    (
        "cn_00000000000c",
        "hyp_0000000000cc",
        "transient traffic spike exhausted available database connections",
        ["ev_c1", "ev_c2"],
    ),
]


def _specimen_case(members=_SPECIMEN):
    d = _problem()
    nodes, hyps, edges, evidence = [d], [], [], []
    for node_id, hyp_id, statement, labels in members:
        nodes.append(_root(node_id, statement, support_labels=labels))
        hyps.append(_hyp(node_id, statement, hypothesis_id=hyp_id))
        edges.append(CausalEdge(cause_node_id=node_id, effect_node_id=d.node_id))
        evidence.extend(_evidence(label) for label in labels)
    return _case(nodes, edges=edges, evidence=evidence, hyps=hyps)


# ---------------------------------------------------------------------------
# The hold: the banked gate-run specimen (3 simultaneously-validated distinct roots)
# ---------------------------------------------------------------------------


def test_three_distinct_validated_roots_hold_candidates():
    """The banked specimen: all three roots VALIDATE from their own evidence
    (node truth untouched) but case-level identification is HELD — CANDIDATES,
    no engine conclusion, contested flag persisted."""
    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    for node_id, _, _, _ in _SPECIMEN:
        assert case.causal_nodes[node_id].node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.root_cause_conclusion is None
    assert case.progress.cause_identification_contested is True
    assert mece_contested_root_ids(case) == {m[0] for m in _SPECIMEN}


def test_hold_counter_and_warning_are_edge_triggered(caplog):
    """One block event per transition INTO the hold — never per turn while the
    contest stands (the M2 over-claim seam pattern)."""
    case = _specimen_case()
    with (
        patch(
            "faultmaven.core.investigation.milestone_engine."
            "cause_identification_held_mece_total"
        ) as counter,
        caplog.at_level(
            logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
        ),
    ):
        _recompute_cause_state_from_chain(case)
        _recompute_cause_state_from_chain(case)  # standing hold: no re-fire
    assert counter.inc.call_count == 1
    warns = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "cause_identification_mece_hold"
    ]
    assert len(warns) == 1
    assert case.progress.cause_identification_contested is True


def test_flag_records_contest_existence_independent_of_symptom_anchor():
    """The persisted flag keys on the SAME predicate the behavioral consumers
    act on (mirror retraction, discrimination ask) — contest existence, not
    the symptom-anchored sub-case. A contest with an unverified symptom is
    already behaviorally enforced, so it must be observable too; verifying
    the symptom later is NOT a second contest event."""
    case = _specimen_case()
    case.progress.symptom_verified = False
    with patch(
        "faultmaven.core.investigation.milestone_engine."
        "cause_identification_held_mece_total"
    ) as counter:
        _recompute_cause_state_from_chain(case)
        assert case.progress.cause_identification_contested is True
        assert counter.inc.call_count == 1
        case.progress.symptom_verified = True
        _recompute_cause_state_from_chain(case)
    assert case.progress.cause_identification_contested is True
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert counter.inc.call_count == 1  # no re-fire on the symptom flip


def test_single_validated_root_is_never_contested():
    case = _specimen_case(members=_SPECIMEN[:1])
    _recompute_cause_state_from_chain(case)
    assert mece_contested_root_ids(case) == set()
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.progress.cause_identification_contested is False
    assert case.root_cause_conclusion is not None


# ---------------------------------------------------------------------------
# Duplicates and deepened chains are ONE cause (the duplicate-emission specimen)
# ---------------------------------------------------------------------------


def _duplicate_case():
    """The banked duplicate-emission shape: ONE cause recorded as two
    near-identical ROOT nodes (statement Jaccard 0.875, pinned ≥ the 0.6
    distinct-cause bar), each carried by a differently-worded hypothesis —
    the realistic emission shape (identically-worded attached hypotheses
    would already hold BOTH nodes at the §7.1 restatement entry bar, so the
    duplicates would never reach validation in the first place)."""
    dup = [
        _SPECIMEN[1],
        (
            "cn_00000000000e",
            "hyp_0000000000ee",
            "pool max size undersized for current checkout load",
            ["ev_e1", "ev_e2"],
        ),
    ]
    case = _specimen_case(members=dup)
    case.hypotheses["hyp_0000000000bb"].statement = (
        "the pool is simply too small for demand"
    )
    case.hypotheses["hyp_0000000000ee"].statement = (
        "capacity theory: connections insufficient at peak"
    )
    return case


def test_duplicate_roots_are_one_cause_and_identify():
    """The banked duplicate-emission specimen: one cause recorded as two
    near-identical nodes is NOT a differential — identification proceeds
    (holding would deadlock: nothing can discriminate a statement from its
    own restatement)."""
    case = _duplicate_case()
    _recompute_cause_state_from_chain(case)
    assert mece_contested_root_ids(case) == set()
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.progress.cause_identification_contested is False
    assert case.root_cause_conclusion is not None


def test_deepened_chain_roots_are_one_line_and_identify():
    """Two ROOT-typed nodes on one causal path (R1 → R2 → D) are one line of
    explanation at two depths (S2 competition is between ORIGINS), not a
    contest."""
    case = _specimen_case(members=_SPECIMEN[:2])
    # b lies downstream of a: a deepened chain, not a differential.
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id="cn_00000000000b")
    )
    _recompute_cause_state_from_chain(case)
    assert mece_contested_root_ids(case) == set()
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_untokenizable_statement_stays_a_distinct_cause():
    """Conservative direction: an unjudgeable root statement merges with
    nothing — under NO-INCORRECT-CONCLUSION the safe failure is holding, never
    concluding on an arbitrary pick."""
    weird = [
        _SPECIMEN[0],
        ("cn_00000000000f", "hyp_0000000000ff", "???", ["ev_f1", "ev_f2"]),
    ]
    case = _specimen_case(members=weird)
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.progress.cause_identification_contested is True


def test_clusters_are_order_invariant():
    """DB/dict ordering never changes the clustering (the INV-29 lesson:
    greedy order-dependent grouping flips verdicts across reloads)."""
    case = _specimen_case()
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id="cn_00000000000b")
    )
    ids = [m[0] for m in _SPECIMEN]
    forward = distinct_cause_clusters(case, ids)
    reversed_ids = list(reversed(ids))
    case.causal_nodes = dict(reversed(list(case.causal_nodes.items())))
    backward = distinct_cause_clusters(case, reversed_ids)
    assert (
        forward
        == backward
        == [
            {"cn_00000000000a", "cn_00000000000b"},
            {"cn_00000000000c"},
        ]
    )


# ---------------------------------------------------------------------------
# Contest resolution: discrimination and confirmation release the hold
# ---------------------------------------------------------------------------


def _counterfactual_refute(case, node_id, label):
    row = _evidence(label, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(row)
    case.causal_nodes[node_id].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="cause removed, problem persists",
            linked_at_turn=case.current_turn,
        )
    )


def test_discriminating_refutes_release_the_hold():
    """The intended escape: counterfactual refutes on the alternatives leave
    one standing validated root — IDENTIFIED, mirror minted, flag cleared."""
    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_identification_contested is True
    _counterfactual_refute(case, "cn_00000000000b", "ev_refute_b")
    _counterfactual_refute(case, "cn_00000000000c", "ev_refute_c")
    _recompute_cause_state_from_chain(case)
    assert case.causal_nodes["cn_00000000000b"].node_state == NodeState.REFUTED
    assert case.causal_nodes["cn_00000000000c"].node_state == NodeState.REFUTED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.progress.cause_identification_contested is False
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.validated_hypothesis_id == "hyp_0000000000aa"


def test_counterfactual_confirmation_settles_the_contest():
    """A confirmed root IS the discrimination (M2 dominance): validated
    siblings never hold a proven cause hostage, and the mirror names it."""
    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_identification_contested is True
    confirm_row = _evidence("ev_confirm_a", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(confirm_row)
    case.causal_nodes["cn_00000000000a"].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=confirm_row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="removing the cause removed the problem",
            linked_at_turn=case.current_turn,
        )
    )
    _recompute_cause_state_from_chain(case)
    assert mece_contested_root_ids(case) == set()
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.progress.cause_identification_contested is False
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.validated_hypothesis_id == "hyp_0000000000aa"
    assert rcc.confidence_level == ConfidenceLevel.VERIFIED


# ---------------------------------------------------------------------------
# Engine-mirror discipline under contest
# ---------------------------------------------------------------------------


def test_engine_mirror_retracts_when_contest_arises():
    """Turn N: sole validated root → engine mirror minted. Turn N+1: a second
    distinct root validates → the mirror is an arbitrary pick (DF-3) and is
    withheld pending discrimination."""
    case = _specimen_case(members=_SPECIMEN[:1])
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion is not None
    # A competing distinct cause validates on a later turn.
    node_id, hyp_id, statement, labels = _SPECIMEN[1]
    case.causal_nodes[node_id] = _root(node_id, statement, support_labels=labels)
    case.causal_edges.append(
        CausalEdge(cause_node_id=node_id, effect_node_id="cn_00000000000d")
    )
    case.evidence.extend(_evidence(label) for label in labels)
    case.hypotheses[hyp_id] = _hyp(node_id, statement, hypothesis_id=hyp_id)
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion is None
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.progress.cause_identification_contested is True


def test_llm_authored_conclusion_survives_contest():
    """The retraction discipline is engine-mirror-only: the LLM's own recorded
    conclusion is never touched here (its lifecycle is #656)."""
    case = _specimen_case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the LLM's own worded conclusion",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
    )
    _recompute_cause_state_from_chain(case)
    assert case.root_cause_conclusion is not None
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert retract_stale_engine_rcc(case) is False


# ---------------------------------------------------------------------------
# Cluster-aware confirm-stamp (the INV-29 stamp-veto lesson): duplicate and
# deepened-line node shapes never veto the user's gone⇒gone handshake
# ---------------------------------------------------------------------------


def _resolution_absence_row(case, label="ev_resolution_confirm"):
    row = _evidence(label, EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    object.__setattr__(row, "collected_at_turn", 8)
    case.evidence.append(row)
    return row


def test_stamp_confirms_a_duplicate_cluster():
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
        grade_cause_assurance,
    )
    from faultmaven.modules.case.contracts import CauseAssuranceGrade

    case = _duplicate_case()
    _recompute_cause_state_from_chain(case)
    _resolution_absence_row(case)
    assert confirm_root_from_resolution_absence(case) is True
    assert grade_cause_assurance(case) == CauseAssuranceGrade.CONFIRMED


def test_stamp_confirms_the_deepened_line_at_its_origin():
    """On a single causal line recorded as two ROOT nodes, the confirmation is
    asserted of the ORIGIN (ancestor-most member), not its consequence."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
    )

    case = _specimen_case(members=_SPECIMEN[:2])
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id="cn_00000000000b")
    )
    _recompute_cause_state_from_chain(case)
    row = _resolution_absence_row(case)
    assert confirm_root_from_resolution_absence(case) is True
    origin_links = case.causal_nodes["cn_00000000000a"].evidence_links
    assert any(
        link.evidence_id == row.evidence_id and link.stance == EvidenceStance.SUPPORTS
        for link in origin_links
    )
    assert not any(
        link.evidence_id == row.evidence_id
        for link in case.causal_nodes["cn_00000000000b"].evidence_links
    )


def test_stamp_still_refuses_a_genuine_contest():
    """Distinct competing causes stay refused — the engine never guesses which
    cause the fix removed (the case terminates MECHANISTIC, honestly)."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
        grade_cause_assurance,
    )
    from faultmaven.modules.case.contracts import CauseAssuranceGrade

    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    _resolution_absence_row(case)
    assert confirm_root_from_resolution_absence(case) is False
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


# ---------------------------------------------------------------------------
# Elicitation: the discrimination ask is rendered on contested roots
# ---------------------------------------------------------------------------


def test_context_annotation_renders_on_contested_roots():
    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    block = _build_causal_graph_block(case)
    assert block.count("MUTUALLY-EXCLUSIVE roots") == len(_SPECIMEN)
    assert "discriminating evidence" in block


def test_context_annotation_absent_when_uncontested():
    case = _specimen_case(members=_SPECIMEN[:1])
    _recompute_cause_state_from_chain(case)
    block = _build_causal_graph_block(case)
    assert "MUTUALLY-EXCLUSIVE roots" not in block


# ---------------------------------------------------------------------------
# Live-path clustering: a REFUTED rung does not connect two causes; lexical
# limits of the mirror bar are pinned as accepted
# ---------------------------------------------------------------------------


def test_path_through_refuted_rung_does_not_merge_roots():
    """A → X → B with X counterfactually REFUTED: the link is disproven, so A
    and B are genuine competitors — the raw-edge walk would have merged them
    and masked the contest."""
    case = _specimen_case(members=_SPECIMEN[:2])
    rung = CausalNode(
        node_id="cn_0000000000f1",
        statement="pool wait queue saturates under sustained connection debt",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        generated_at_turn=1,
    )
    case.causal_nodes[rung.node_id] = rung
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id=rung.node_id)
    )
    case.causal_edges.append(
        CausalEdge(cause_node_id=rung.node_id, effect_node_id="cn_00000000000b")
    )
    refute_row = _evidence("ev_rung_refute", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(refute_row)
    rung.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=refute_row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="queue saturation ruled out",
            linked_at_turn=case.current_turn,
        )
    )
    _recompute_cause_state_from_chain(case)
    assert case.causal_nodes["cn_0000000000f1"].node_state == NodeState.REFUTED
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert case.progress.cause_identification_contested is True


def test_live_path_still_merges_roots():
    """Control for the liveness filter: the same A → X → B shape with a LIVE
    rung is one deepened line — not a contest."""
    case = _specimen_case(members=_SPECIMEN[:2])
    rung = CausalNode(
        node_id="cn_0000000000f1",
        statement="pool wait queue saturates under sustained connection debt",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        generated_at_turn=1,
    )
    case.causal_nodes[rung.node_id] = rung
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id=rung.node_id)
    )
    case.causal_edges.append(
        CausalEdge(cause_node_id=rung.node_id, effect_node_id="cn_00000000000b")
    )
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_identification_contested is False
    assert case.progress.cause_state == CauseState.IDENTIFIED


def test_negation_blind_merge_is_the_accepted_lexical_limit():
    """Pinned accepted limit: negation is stopworded, so opposite-polarity
    statements read as ONE cause (no contest). Shared token-layer limit with
    the §7.1 guards; a semantic layer would be a new lever, not a fix here."""
    pair = [
        (
            "cn_0000000000d1",
            "hyp_0000000000d1",
            "the failover config flag was applied to the primary",
            ["ev_n1", "ev_n2"],
        ),
        (
            "cn_0000000000d2",
            "hyp_0000000000d2",
            "the failover config flag was NOT applied to the primary",
            ["ev_n3", "ev_n4"],
        ),
    ]
    case = _specimen_case(members=pair)
    case.hypotheses["hyp_0000000000d1"].statement = "flag application theory"
    case.hypotheses["hyp_0000000000d2"].statement = "missing flag theory"
    clusters = distinct_cause_clusters(case, {p[0] for p in pair})
    assert len(clusters) == 1  # the documented lexical limit


# ---------------------------------------------------------------------------
# Engine-assertion discipline beyond the mirror: synthesize refusal and the
# working-conclusion proxy gate
# ---------------------------------------------------------------------------


def test_synthesize_refuses_to_mint_while_contested():
    """Defense in depth: a direct (non-recompute) caller of the mirror
    synthesis never gets an arbitrary pick minted on a contested case."""
    from faultmaven.core.investigation.causal_graph import (
        derive_node_states,
        synthesize_rcc_from_validated_root,
    )

    case = _specimen_case()
    derive_node_states(case)
    assert synthesize_rcc_from_validated_root(case) is False
    assert case.root_cause_conclusion is None


def test_neither_proxy_counts_while_contested():
    """§7.1.2 / INV-34: while identification is MECE-contested, NEITHER fallback
    proxy counts as a known cause — not the ENGINE working conclusion (the
    arbitrary max-likelihood pick the hold withholds) NOR an LLM-authored
    conclusion (the LLM asserting ONE contested cause is exactly the over-claim
    the hold suppresses). Read-time suppression: both are preserved and count
    again once the contest resolves."""
    from faultmaven.core.investigation.terminal_transitions import _cause_identified
    from faultmaven.modules.case.contracts import WorkingConclusion

    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    case.working_conclusion = WorkingConclusion(
        statement="the deploy removed the connection release call",
        likelihood=0.8,
        reasoning="engine-generated per-turn pick",
    )
    assert _cause_identified(case) is False
    # An LLM-authored conclusion is ALSO suppressed while contested (P2.3 closed
    # this trust-boundary leak) — but preserved, not erased.
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the LLM's own worded conclusion",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
    )
    assert _cause_identified(case) is False
    assert case.root_cause_conclusion is not None  # preserved, not retracted
    # Once the contest resolves, the preserved LLM conclusion counts again.
    case.progress.cause_identification_contested = False
    assert _cause_identified(case) is True


def test_contested_flag_survives_progress_blob_round_trip():
    """The edge-trigger and the terminal _cause_identified gate read the
    PERSISTED flag — pin the progress-blob round-trip (model_dump → JSON →
    reconstruct), the same shape the repositories use."""
    import json

    from faultmaven.modules.case.contracts import InvestigationProgress

    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_identification_contested is True
    blob = json.dumps(case.progress.model_dump(mode="json"))
    reloaded = InvestigationProgress(**json.loads(blob))
    assert reloaded.cause_identification_contested is True
    # Old blobs (pre-field) default False rather than failing validation.
    legacy = json.loads(blob)
    legacy.pop("cause_identification_contested")
    assert InvestigationProgress(**legacy).cause_identification_contested is False


# ---------------------------------------------------------------------------
# Cluster-aware stamp hardening: cluster-wide idempotence, cluster-wide
# refutation window, mixed-cluster origin, cluster-member mirror re-mint
# ---------------------------------------------------------------------------


def test_stamp_is_idempotent_across_cluster_members():
    """A cause confirmed once (on its consequence node) is never stamped a
    second time under a different node id when the chain later deepens — the
    idempotence check is cluster-wide, not representative-only."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
    )

    case = _specimen_case(members=_SPECIMEN[:2])
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id="cn_00000000000b")
    )
    _recompute_cause_state_from_chain(case)
    # The DESCENDANT member (b) already carries the confirmation from an
    # earlier resolve.
    confirmed_row = _evidence(
        "ev_prior_confirm", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
    )
    case.evidence.append(confirmed_row)
    case.causal_nodes["cn_00000000000b"].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=confirmed_row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="engine: user-confirmed resolution",
            linked_at_turn=4,
        )
    )
    _resolution_absence_row(case, label="ev_second_confirm")
    assert confirm_root_from_resolution_absence(case) is False


def test_stamp_window_covers_refutes_on_duplicate_members():
    """A hedged failed-fix refute on a NON-representative duplicate is a
    refutation of the SAME cause: a confirmation row at-or-before it never
    mints the top grade, exactly as if the refute sat on the cited origin."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
    )

    case = _duplicate_case()
    _recompute_cause_state_from_chain(case)
    # Hedged refute on the LATER-sorted duplicate (cn_...e, never the cited
    # representative under lexical tie-break) at turn 9.
    refute_row = _evidence("ev_dup_refute", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    object.__setattr__(refute_row, "collected_at_turn", 9)
    case.evidence.append(refute_row)
    case.causal_nodes["cn_00000000000e"].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=refute_row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="raising the pool did not clear the errors",
            stance_confidence=0.4,  # hedged: no demotion, but windows the mint
            linked_at_turn=9,
        )
    )
    stale = _resolution_absence_row(case, label="ev_stale_confirm")
    object.__setattr__(stale, "collected_at_turn", 8)  # at-or-before the refute
    assert confirm_root_from_resolution_absence(case) is False
    fresh = _resolution_absence_row(case, label="ev_fresh_confirm")
    object.__setattr__(fresh, "collected_at_turn", 10)  # newer than the refute
    assert confirm_root_from_resolution_absence(case) is True


def test_mixed_cluster_origin_prefers_the_line_head():
    """Cluster {origin, consequence, edge-less duplicate-of-consequence}: the
    citation goes to the member that HEADS the line (has live in-cluster
    descendants), never to the edge-less duplicate that merely wins a lexical
    tie among no-ancestor members."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
    )

    members = [
        # ids chosen so the duplicate sorts LEXICALLY FIRST among candidates.
        (
            "cn_0000000000e9",
            "hyp_0000000000e9",
            "the deploy removed the connection release call so pool "
            "connections leak until exhaustion",
            ["ev_q1", "ev_q2"],
        ),
        (
            "cn_0000000000e5",
            "hyp_0000000000e5",
            "connection pool max size undersized for current checkout load",
            ["ev_r1", "ev_r2"],
        ),
        (
            "cn_0000000000e1",
            "hyp_0000000000e1",
            "pool max size undersized for current checkout load",
            ["ev_p1", "ev_p2"],
        ),
    ]
    case = _specimen_case(members=members)
    # e9 (origin) → e5 (consequence); e1 is an edge-less mirror duplicate of e5.
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_0000000000e9", effect_node_id="cn_0000000000e5")
    )
    case.hypotheses["hyp_0000000000e5"].statement = "pool sizing theory"
    case.hypotheses["hyp_0000000000e1"].statement = "capacity ceiling theory"
    _recompute_cause_state_from_chain(case)
    row = _resolution_absence_row(case)
    assert confirm_root_from_resolution_absence(case) is True
    assert any(
        link.evidence_id == row.evidence_id
        for link in case.causal_nodes["cn_0000000000e9"].evidence_links
    )
    assert not any(
        link.evidence_id == row.evidence_id
        for link in case.causal_nodes["cn_0000000000e1"].evidence_links
    )


def test_stamp_remints_engine_mirror_naming_a_cluster_member():
    """Deepened-late shape: the engine mirror names the CONSEQUENCE's
    hypothesis; the stamp confirms the ORIGIN. Same cause, wrong depth — the
    mirror re-mints to the confirmed origin at VERIFIED instead of freezing
    at CONFIDENT beside a CONFIRMED grade."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
    )

    case = _specimen_case(members=_SPECIMEN[:2])
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id="cn_00000000000b")
    )
    _recompute_cause_state_from_chain(case)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=case.causal_nodes["cn_00000000000b"].statement,
        mechanism="Directly produces the observed problem.",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        validated_hypothesis_id="hyp_0000000000bb",
        determined_by="engine:chain_validation",
    )
    _resolution_absence_row(case)
    assert confirm_root_from_resolution_absence(case) is True
    rcc = case.root_cause_conclusion
    assert rcc is not None
    assert rcc.validated_hypothesis_id == "hyp_0000000000aa"
    assert rcc.confidence_level == ConfidenceLevel.VERIFIED


def test_stamp_never_remints_an_llm_conclusion():
    """The cluster-member re-mint is engine-mirror-only: an LLM-authored
    conclusion naming the consequence stays untouched."""
    from faultmaven.core.investigation.cause_assurance import (
        confirm_root_from_resolution_absence,
    )

    case = _specimen_case(members=_SPECIMEN[:2])
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_00000000000a", effect_node_id="cn_00000000000b")
    )
    _recompute_cause_state_from_chain(case)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the LLM's own conclusion about the consequence",
        mechanism="as the LLM described it",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        validated_hypothesis_id="hyp_0000000000bb",
    )
    _resolution_absence_row(case)
    assert confirm_root_from_resolution_absence(case) is True
    assert (
        case.root_cause_conclusion.root_cause
        == "the LLM's own conclusion about the consequence"
    )


# ---------------------------------------------------------------------------
# Review follow-ups: safe hook degradation + working-conclusion coherence
# ---------------------------------------------------------------------------


def test_stamp_refuses_safely_when_arbitration_hook_is_missing():
    """The multi-target stamp path degrades to the safe refusal when the graph
    hook registry is unavailable — never a KeyError that 500s the unguarded
    RESOLVED-execution path (the importlib cold-start fallback no-ops when
    causal_graph is already imported, so the registry stays empty)."""
    from faultmaven.core.investigation import cause_assurance

    case = _specimen_case(members=_SPECIMEN[:2])
    _recompute_cause_state_from_chain(case)
    _resolution_absence_row(case)
    with patch.dict(cause_assurance._GRAPH_HOOKS, {}, clear=True):
        assert cause_assurance.confirm_root_from_resolution_absence(case) is False


def test_working_conclusion_block_carries_contested_note():
    """§7.1.2 prompt coherence: the working conclusion is the max-likelihood
    pick over the contested hypotheses — rendered on a contested case it must
    carry the discrimination note, or the model anchors on the arbitrary pick
    beside a graph block saying identification is held."""
    from faultmaven.core.investigation.prompts.context_builder import (
        build_investigation_context,
    )
    from faultmaven.modules.case.contracts import WorkingConclusion

    case = _specimen_case()
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_identification_contested is True
    case.working_conclusion = WorkingConclusion(
        statement="the deploy removed the connection release call",
        likelihood=0.8,
        reasoning="engine per-turn pick",
    )
    ctx = build_investigation_context(case, "user message", max_tokens=8000)
    assert "mutually-exclusive candidate causes" in ctx["working_conclusion"]
    assert "DISCRIMINATING" in ctx["working_conclusion"]


def test_working_conclusion_block_clean_when_uncontested():
    from faultmaven.core.investigation.prompts.context_builder import (
        build_investigation_context,
    )
    from faultmaven.modules.case.contracts import WorkingConclusion

    case = _specimen_case(members=_SPECIMEN[:1])
    _recompute_cause_state_from_chain(case)
    case.working_conclusion = WorkingConclusion(
        statement="the deploy removed the connection release call",
        likelihood=0.8,
        reasoning="engine per-turn pick",
    )
    ctx = build_investigation_context(case, "user message", max_tokens=8000)
    assert "mutually-exclusive candidate causes" not in ctx["working_conclusion"]
    assert "the deploy removed the connection release call" in ctx["working_conclusion"]
