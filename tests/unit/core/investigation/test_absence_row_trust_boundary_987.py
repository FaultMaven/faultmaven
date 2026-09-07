"""#987 — a conversationally-confirmed root cause must reach the structured
truth surfaces, and a SUCCESSFUL fix must never be read as a failed one.

The incident (sim ``engine-assess-iam-2026-08-04``, case ``case_e7d4f551d87b``):
the LLM recorded its success confirmation as a ``causal_absence_evidence`` row
("post-fix authentication succeeded") and REFUTES-linked it to the very root it
confirmed. The engine read that as a failed-fix counterfactual disconfirmation,
which fired M6 — which then MINTED a row asserting "the cause was addressed or
confirmed correct, yet the problem persisted", a fact it had never checked and
which was false. The true root went REFUTED at belief 0, the conclusion was
retracted, ``cause_state`` fell to UNKNOWN, and the turn-10 resolution recap
rendered the early-stage placeholder as the case's root cause.

Three independent gates now stand between that emission and the truth layer,
and each is tested here in BOTH directions (it refuses the bad shape AND it
still permits the good one), because a gate that cannot fail proves nothing:

1. the category-gated M2 trust boundary, asserted at BOTH belief axes;
2. M6's established preconditions (destructive transitions establish);
3. the honest "no root cause established" rendering.
"""

from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import (
    demote_disconfirmed_cause_via_evidence,
    derive_node_states,
    ingest_emitted_chain,
    m6_disconfirmation_basis,
    seed_problem_node,
)
from faultmaven.core.investigation.cause_assurance import ENGINE_EVIDENCE_AUTHOR
from faultmaven.modules.case.contracts import (
    CONFIRMED_ESTABLISHED_BY,
    Case,
    CaseSeverity,
    CaseState,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    InvestigationActionType,
    NodeState,
    NodeType,
    ProblemVerification,
    ProposedAction,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — the incident shape, built from real domain types (a stand-in would
# pass a dead gate).
# ---------------------------------------------------------------------------


def _case() -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="IRSA AssumeRoleWithWebIdentity fails",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="IRSA exchange fails",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="IRSA AssumeRoleWithWebIdentity fails in prod-west-2",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 9
    case.progress.symptom_verified = True
    return case


def _evidence(ev_id, category, turn, summary, collected_by="llm") -> Evidence:
    return Evidence(
        evidence_id=ev_id,
        summary=summary,
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by=collected_by,
        collected_at_turn=turn,
        collected_at=datetime.now(timezone.utc),
    )


def _success_absence_row(turn=9) -> Evidence:
    """The row at the heart of the incident: a SUCCESS confirmation."""
    return _evidence(
        "ev_47b2f3337ffc",
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        turn,
        "After replacing the source-account OIDC provider audience with "
        "sts.amazonaws.com, web-identity credentials and chained role "
        "assumption succeeded; S3 processing completed successfully.",
    )


def _hypothesis(hypothesis_id, state, *, root_node_id=None, refutation_reason=None):
    """A REAL Hypothesis — a SimpleNamespace stand-in would sail past the very
    model validators the engine relies on."""
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement="OIDC provider audience mismatch breaks the IRSA exchange",
        category=HypothesisCategory.CONFIG,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        refutation_reason=refutation_reason,
        generated_at_turn=1,
    )


def _link(node_ref, evidence_id, stance, reasoning="r", confidence=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        node_ref=node_ref,
        evidence_id=evidence_id,
        stance=stance,
        reasoning=reasoning,
        stance_confidence=confidence,
    )


def _root_spec(statement):
    from types import SimpleNamespace

    return SimpleNamespace(
        statement=statement, node_type=NodeType.ROOT, produces="D", and_group=None
    )


_TRUE_ROOT = (
    "The IAM OIDC provider for the prod-west-2 issuer is registered with "
    "sts.amazonaws.com.cn instead of the workload token audience "
    "sts.amazonaws.com"
)


# ---------------------------------------------------------------------------
# 1. The category-gated trust boundary — BOTH axes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stance", ["refutes", "supports", "neutral"])
def test_node_axis_refuses_any_llm_stance_on_an_absence_row(stance):
    """Chain axis: no model-authored stance on a causal_absence row lands."""
    case = _case()
    seed_problem_node(case)
    case.evidence.append(_success_absence_row())
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[_link("new_index_0", "ev_47b2f3337ffc", stance)],
        current_turn=case.current_turn,
    )
    assert case.causal_nodes[created[0]].evidence_links == []


@pytest.mark.parametrize("stance", ["refutes", "supports", "neutral"])
def test_hypothesis_axis_refuses_any_llm_stance_on_an_absence_row(stance):
    """Flat axis — the second entry point (#987's second finding).

    Guarding only the node axis would have left the identical cascade one
    stance choice away: a REFUTES here reaches ``_net_refuted`` →
    ``_hypothesis_disconfirmed`` → M6 exactly as the node-axis link reached
    ``derive_node_states``.
    """
    from types import SimpleNamespace

    from faultmaven.core.investigation.hypothesis_manager import (
        create_hypothesis_manager,
    )
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    case = _case()
    case.evidence.append(_success_absence_row())
    hyp = _hypothesis("hyp_421fd53b5fd7", HypothesisState.ACTIVE)
    case.hypotheses[hyp.hypothesis_id] = hyp

    engine = MilestoneEngine.__new__(MilestoneEngine)
    # A REAL manager, so removing the gate makes this test fail by the link
    # LANDING — not by an AttributeError on the way there. A mutation that
    # trips over missing wiring proves the call site is reached; it does not
    # prove the gate is what refuses the link.
    engine.hypothesis_manager = create_hypothesis_manager()
    MilestoneEngine._apply_hypothesis_evidence_links(
        engine,
        case,
        [
            SimpleNamespace(
                hypothesis_id_ref="hyp_421fd53b5fd7",
                evidence_id_ref="ev_47b2f3337ffc",
                stance=EvidenceStance(stance),
                reasoning="the fix worked",
                stance_confidence=1.0,
            )
        ],
        {},
    )
    assert hyp.evidence_links == []


def test_both_axes_still_accept_ordinary_causal_evidence():
    """The gate must be CATEGORY-scoped, not a blanket refusal — otherwise it
    would silence the grounding path the whole engine runs on."""
    from types import SimpleNamespace

    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    case = _case()
    seed_problem_node(case)
    case.evidence.append(
        _evidence(
            "ev_bf208c13b2b7",
            EvidenceCategory.CAUSAL_EVIDENCE,
            6,
            "Provider record contains only sts.amazonaws.com.cn",
        )
    )
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[_link("new_index_0", "ev_bf208c13b2b7", "supports")],
        current_turn=case.current_turn,
    )
    assert len(case.causal_nodes[created[0]].evidence_links) == 1

    hyp = _hypothesis("hyp_1b14ba619dd5", HypothesisState.ACTIVE)
    case.hypotheses[hyp.hypothesis_id] = hyp
    engine = MilestoneEngine.__new__(MilestoneEngine)
    from faultmaven.core.investigation.hypothesis_manager import (
        create_hypothesis_manager,
    )

    engine.hypothesis_manager = create_hypothesis_manager()
    MilestoneEngine._apply_hypothesis_evidence_links(
        engine,
        case,
        [
            SimpleNamespace(
                hypothesis_id_ref="hyp_1b14ba619dd5",
                evidence_id_ref="ev_bf208c13b2b7",
                stance=EvidenceStance.SUPPORTS,
                reasoning="grounds it",
                stance_confidence=0.9,
            )
        ],
        {},
    )
    assert len(hyp.evidence_links) == 1


def test_refused_link_is_metered_and_not_silent():
    """The strip HIDES the emission, so the violation must be counted — a model
    that routinely mis-links absence rows must not look identical to one that
    follows the contract."""
    case = _case()
    seed_problem_node(case)
    case.evidence.append(_success_absence_row())
    with patch(
        "faultmaven.core.investigation.cause_assurance.absence_row_link_refused_total"
    ) as counter:
        ingest_emitted_chain(
            case,
            nodes_to_add=[_root_spec(_TRUE_ROOT)],
            edges_to_add=[],
            node_evidence=[_link("new_index_0", "ev_47b2f3337ffc", "refutes")],
            current_turn=case.current_turn,
        )
    counter.labels.assert_called_once_with(axis="node", stance="refutes")
    counter.labels.return_value.inc.assert_called_once()


def test_success_confirmation_no_longer_refutes_the_true_root():
    """End-to-end on the incident shape: the root the fix CONFIRMED must not
    end the turn REFUTED at belief 0.

    This is the assertion that would have failed before #987 — the mutation
    test for the whole gate.
    """
    case = _case()
    seed_problem_node(case)
    case.evidence.append(
        _evidence(
            "ev_bf208c13b2b7",
            EvidenceCategory.CAUSAL_EVIDENCE,
            6,
            "Provider record contains only sts.amazonaws.com.cn",
        )
    )
    case.evidence.append(
        _evidence(
            "ev_6159eb53665c",
            EvidenceCategory.CAUSAL_EVIDENCE,
            8,
            "Issuer URL and cluster ID independently match the provider URL",
        )
    )
    case.evidence.append(_success_absence_row())
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[
            _link("new_index_0", "ev_bf208c13b2b7", "supports", confidence=0.98),
            _link("new_index_0", "ev_6159eb53665c", "supports", confidence=0.94),
            # The incident: the success row, REFUTES-linked to its own cause.
            _link("new_index_0", "ev_47b2f3337ffc", "refutes", confidence=1.0),
        ],
        current_turn=case.current_turn,
    )
    root = case.causal_nodes[created[0]]
    derive_node_states(case)
    assert root.node_state is not NodeState.REFUTED
    assert root.belief != 0.0


# ---------------------------------------------------------------------------
# 2. M6 preconditions — destructive transitions establish.
# ---------------------------------------------------------------------------


def _fix_applied(case, turn=8, action_type=InvestigationActionType.SOLUTION):
    """A fix the user EXECUTED at ``turn``.

    ``accepted_in_turn`` is the execution turn — ``proposed_in_turn`` is the
    OFFER, one turn earlier in the ordinary flow. Keying the persistence window
    on the proposal let evidence from the offering turn (recorded before the fix
    ever ran) read as a post-fix outcome.
    """
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=action_type,
            description="Correct the OIDC provider ClientIDList",
            proposed_in_turn=turn - 1,
            accepted_in_turn=turn,
            state="accepted",
        )
    )


def test_m6_refuses_when_no_fix_application_is_recorded():
    case = _case()
    assert m6_disconfirmation_basis(case) is None


def test_m6_refuses_when_nothing_observes_the_problem_persisting():
    """A fix was applied, but no symptom evidence at/after it: "nothing said it
    was fixed" is not an observation that it stayed broken."""
    case = _case()
    _fix_applied(case)
    case.evidence.append(
        _evidence(
            "ev_8e84e4f956be", EvidenceCategory.SYMPTOM_EVIDENCE, 5, "pre-fix errors"
        )
    )
    assert m6_disconfirmation_basis(case) is None


def test_m6_refuses_when_a_resolution_confirmation_stands():
    """The #987 shape: a qualifying gone⇒gone row at/after the fix turn is
    direct evidence the problem did NOT persist, so the failed-fix premise is
    false on the case's own record."""
    case = _case()
    _fix_applied(case)
    case.evidence.append(
        _evidence(
            "ev_f6e5d4c3b2a1",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            9,
            "still investigating",
        )
    )
    case.evidence.append(_success_absence_row())
    assert m6_disconfirmation_basis(case) is None


def test_m6_fires_on_a_genuine_failed_fix():
    """The gate must still PASS the case it exists for — otherwise it is a
    silent disabling of M6 rather than a precondition."""
    case = _case()
    _fix_applied(case, turn=8)
    case.evidence.append(
        _evidence(
            "ev_a1b2c3d4e5f6",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            9,
            "AssumeRoleWithWebIdentity still returns AccessDenied after the fix",
        )
    )
    basis = m6_disconfirmation_basis(case)
    assert basis is not None
    fix_turn, provenance = basis
    assert fix_turn == 8
    assert "executed at turn 8" in provenance.lower()


def test_m6_engine_row_records_inference_with_provenance_not_an_observation():
    """The fabrication itself: the minted row must not assert a first-person
    observation the engine never made.

    Exercised on the arm that actually MINTS a fresh row. Post-#987 the
    counterfactual arm can only fire on a node that already carries the engine's
    marker (the ingest gate leaves the engine as the sole producer of a
    node-side counterfactual refute), so its mint is always idempotent-skipped —
    that arm is the LATCH. The counterfactual provenance TEXT is pinned at its
    source in ``test_m6_fires_on_a_genuine_failed_fix``.
    """
    from faultmaven.core.investigation.causal_graph import _attach_engine_refutation

    case = _case()
    seed_problem_node(case)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    _attach_engine_refutation(
        case,
        created[0],
        "failed treatment",
        "a fix recorded as EXECUTED at turn 8 did not hold",
    )
    engine_rows = [e for e in case.evidence if e.collected_by == "engine"]
    assert len(engine_rows) == 1
    summary = engine_rows[0].summary
    assert summary.startswith("Engine inference (M6), not an observation:")
    # The retired fabrication, verbatim — it must never come back.
    assert "yet the problem persisted." not in summary
    assert "EXECUTED at turn 8" in summary
    # Engine authorship is what keeps this out of every observation reader.
    assert engine_rows[0].primary_purpose.startswith("engine inference")


def _counterfactual_case(*, with_persistence: bool):
    """A case whose ONLY disconfirmation is a counterfactual refute on the root
    (an engine-authored absence REFUTES) — the arm that CLAIMS a failed fix.

    The hypothesis is deliberately left ACTIVE and un-refuted so the
    unconditional evidence arm cannot fire and mask the gate under test.
    """
    from faultmaven.modules.case.contracts import NodeEvidenceLink

    case = _case()
    _fix_applied(case, turn=8)
    if with_persistence:
        case.evidence.append(
            _evidence(
                "ev_a1b2c3d4e5f6",
                EvidenceCategory.SYMPTOM_EVIDENCE,
                9,
                "AssumeRoleWithWebIdentity still returns AccessDenied after the fix",
            )
        )
    seed_problem_node(case)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    engine_row = _evidence(
        "ev_0e0e0e0e0e0e",
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        9,
        "prior engine disconfirmation",
        collected_by="engine",
    )
    case.evidence.append(engine_row)
    case.causal_nodes[created[0]].evidence_links.append(
        NodeEvidenceLink(
            evidence_id=engine_row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="counterfactual",
            linked_at_turn=9,
        )
    )
    hyp = _hypothesis(
        "hyp_421fd53b5fd7", HypothesisState.ACTIVE, root_node_id=created[0]
    )
    case.hypotheses[hyp.hypothesis_id] = hyp
    case.progress.cause_state = CauseState.IDENTIFIED
    return case


def test_m6_counterfactual_arm_refuses_when_preconditions_are_unestablished():
    """The failed-fix CLAIM without an observed persistence: refused."""
    case = _counterfactual_case(with_persistence=False)
    before = {e.evidence_id for e in case.evidence}
    assert demote_disconfirmed_cause_via_evidence(case) is False
    assert {e.evidence_id for e in case.evidence} == before  # nothing minted


def test_m6_counterfactual_arm_fires_when_preconditions_hold():
    """...and the same shape WITH the persistence observation fires."""
    case = _counterfactual_case(with_persistence=True)
    assert demote_disconfirmed_cause_via_evidence(case) is True


def test_evidence_based_disconfirmation_demotes_without_any_fix_record():
    """REGRESSION (review finding 1): an ordinary evidence-based
    disconfirmation must NOT require a fix record.

    `_disconfirmed_cause_trigger` fires on `_net_refuted` — the cause outweighed
    by its own contradicting evidence — which asserts nothing about any fix.
    The first cut of the #987 precondition gate covered the whole trigger, so a
    net-refuted cause with no ProposedAction stayed VALIDATED at
    cause_state=IDENTIFIED with its conclusion intact: a disproven cause left
    standing, the NO-INCORRECT-CONCLUSION breach in the opposite direction.
    """
    from faultmaven.modules.case.contracts import (
        ConfidenceLevel,
        HypothesisEvidenceLink,
        RootCauseConclusion,
    )

    case = _case()
    seed_problem_node(case)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    hyp = _hypothesis(
        "hyp_421fd53b5fd7", HypothesisState.ACTIVE, root_node_id=created[0]
    )
    # Net-refuted by its own links — no fix, no ProposedAction anywhere.
    case.evidence.append(
        _evidence("ev_c0ffee000001", EvidenceCategory.CAUSAL_EVIDENCE, 7, "contra")
    )
    hyp.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp.hypothesis_id,
            evidence_id="ev_c0ffee000001",
            stance=EvidenceStance.REFUTES,
            reasoning="the provider record contradicts this cause",
            stance_confidence=0.9,
        )
    )
    case.hypotheses[hyp.hypothesis_id] = hyp
    case.progress.cause_state = CauseState.IDENTIFIED
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_TRUE_ROOT,
        mechanism="m",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        validated_hypothesis_id=hyp.hypothesis_id,
    )
    assert not case.proposed_actions  # nothing establishes a fix

    assert demote_disconfirmed_cause_via_evidence(case) is True
    assert hyp.state == HypothesisState.REFUTED
    assert case.root_cause_conclusion is None
    # ...and the durable record makes NO failed-fix claim it cannot substantiate.
    engine_rows = [e for e in case.evidence if e.collected_by == "engine"]
    assert len(engine_rows) == 1
    assert "net-refute" in engine_rows[0].summary
    assert "fix" not in engine_rows[0].summary


# ---------------------------------------------------------------------------
# 3. Honest rendering — naming the state that had no name.
# ---------------------------------------------------------------------------


def test_resolution_recap_never_renders_the_early_stage_placeholder():
    """Acceptance criterion 4. The placeholder contradicted the ten preceding
    turns at the most trust-sensitive moment of the case."""
    from faultmaven.core.investigation.milestone_engine import (
        NO_ROOT_CAUSE_ESTABLISHED,
        _get_root_cause_summary,
    )
    from faultmaven.core.investigation.working_conclusion_generator import (
        generate_working_conclusion,
    )

    case = _case()
    case.working_conclusion = generate_working_conclusion(case, case.current_turn)
    assert "awaiting hypothesis generation" in case.working_conclusion.statement

    summary = _get_root_cause_summary(case)
    assert summary == NO_ROOT_CAUSE_ESTABLISHED
    assert "awaiting hypothesis generation" not in summary


def test_recap_still_surfaces_a_real_working_conclusion():
    """The fallback must stay LIVE — gating it on the placeholder must not
    silence a genuine finding the conclusion record does not hold."""
    from faultmaven.core.investigation.milestone_engine import _get_root_cause_summary
    from faultmaven.modules.case.contracts import WorkingConclusion

    case = _case()
    case.working_conclusion = WorkingConclusion(
        statement="Connection pool exhaustion under peak load",
        likelihood=0.82,
        reasoning="3 supporting items",
        supporting_evidence_ids=["ev_1"],
        caveats=[],
        updated_at=datetime.now(timezone.utc),
    )
    assert _get_root_cause_summary(case) == "Connection pool exhaustion under peak load"


def test_working_conclusion_mirrors_a_standing_conclusion_over_the_placeholder():
    """Run-3 shape: a correct RootCauseConclusion stood while every hypothesis
    had decayed to RETIRED, and the working conclusion still reported
    'awaiting hypothesis generation'. Two truth surfaces, opposite answers."""
    from faultmaven.core.investigation.working_conclusion_generator import (
        generate_working_conclusion,
    )
    from faultmaven.modules.case.contracts import ConfidenceLevel, RootCauseConclusion

    case = _case()
    stale = _hypothesis("hyp_82f6e78c64d0", HypothesisState.RETIRED)
    case.hypotheses[stale.hypothesis_id] = stale
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_TRUE_ROOT,
        mechanism="STS cannot match the token audience to an eligible provider",
        confidence_level=ConfidenceLevel.from_score(0.9),
        likelihood=0.9,
        evidence_basis=["ev_bf208c13b2b7"],
    )
    wc = generate_working_conclusion(case, case.current_turn)
    assert wc.statement == _TRUE_ROOT
    assert wc.likelihood == 0.9


# ---------------------------------------------------------------------------
# Acceptance criterion 1 — the resolved case's truth surfaces AGREE.
# ---------------------------------------------------------------------------


def test_resolved_case_truth_surfaces_agree_with_the_confirmed_cause():
    """The incident replayed end to end through the real recompute and the real
    RESOLVED finalizer.

    Before #987 this case terminated with ``cause_state=unknown``,
    ``cause_assurance=no_root``, ``root_cause_conclusion=None``, and the true
    cause REFUTED at belief 0 — while the transcript read correct throughout.
    All three structured surfaces must now agree with the cause the user
    confirmed, and the record must say HOW it was established.
    """
    from faultmaven.core.investigation.milestone_engine import (
        _recompute_cause_state_from_chain,
    )
    from faultmaven.core.investigation.terminal_transitions import (
        finalize_resolution_truth_surface,
    )
    from faultmaven.modules.case.contracts import CauseAssuranceGrade

    case = _case()
    seed_problem_node(case)
    for eid, turn, summary in (
        ("ev_bf208c13b2b7", 6, "Provider record contains only sts.amazonaws.com.cn"),
        ("ev_6159eb53665c", 8, "Issuer URL and cluster ID match the provider URL"),
    ):
        case.evidence.append(
            _evidence(eid, EvidenceCategory.CAUSAL_EVIDENCE, turn, summary)
        )

    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[
            _link("new_index_0", "ev_bf208c13b2b7", "supports", confidence=0.98),
            _link("new_index_0", "ev_6159eb53665c", "supports", confidence=0.94),
        ],
        current_turn=case.current_turn,
    )
    root_id = created[0]
    hyp = _hypothesis("hyp_421fd53b5fd7", HypothesisState.ACTIVE, root_node_id=root_id)
    hyp.path = [root_id]
    case.hypotheses[hyp.hypothesis_id] = hyp

    # Turn 9: the fix worked. The model records its success confirmation AND
    # (contract violation) REFUTES-links it to the very root it confirms.
    case.evidence.append(_success_absence_row(turn=9))
    ingest_emitted_chain(
        case,
        nodes_to_add=[],
        edges_to_add=[],
        node_evidence=[_link(root_id, "ev_47b2f3337ffc", "refutes", confidence=1.0)],
        current_turn=9,
    )
    _recompute_cause_state_from_chain(case)

    # Turn 11: the user confirms; the RESOLVED finalizer reconciles.
    case.current_turn = 11
    finalize_resolution_truth_surface(case)

    root = case.causal_nodes[root_id]
    assert root.node_state == NodeState.VALIDATED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert case.progress.cause_assurance == CauseAssuranceGrade.CONFIRMED
    assert case.root_cause_conclusion is not None
    assert case.root_cause_conclusion.root_cause == _TRUE_ROOT
    # ...and the record carries HOW it was established, not a bare assertion —
    # on BOTH surfaces, in the form each audience needs (#1097). The conclusion
    # is rendered to a user, so it carries prose; the durable node link is the
    # audit trail, so it keeps the turn and the ids that make the promotion
    # reconstructible.
    established = case.root_cause_conclusion.established_by
    assert established == CONFIRMED_ESTABLISHED_BY
    audit = [
        link.reasoning
        for link in root.evidence_links
        if link.stance == EvidenceStance.SUPPORTS
    ]
    assert any(
        r and "user-confirmed resolution at turn 11" in r and "cn_" in r
        # The CITED row is named there too — that is what makes the promotion
        # reconstructible, and it is why the ids belong on this surface.
        and "ev_47b2f3337ffc" in r
        for r in audit
    )


# ---------------------------------------------------------------------------
# Review-round hardening (#987 round 2).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stance", ["refutes", "supports"])
def test_symptom_absence_rows_are_refused_too(stance):
    """Both absence categories, because the prompt states the rule for both.

    ``symptom_absence`` carries no counterfactual force, but it moves likelihood
    and node tallies — and a rule the engine tells the model must be a rule the
    engine enforces, or the prompt is lying about the boundary.
    """
    case = _case()
    seed_problem_node(case)
    case.evidence.append(
        _evidence(
            "ev_5a5a5a5a5a5a",
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            9,
            "errors stopped after the failover",
        )
    )
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[_link("new_index_0", "ev_5a5a5a5a5a5a", stance)],
        current_turn=case.current_turn,
    )
    assert case.causal_nodes[created[0]].evidence_links == []


def test_mitigation_never_establishes_that_the_cause_was_addressed():
    """A workaround is by definition NOT a fix of the cause, so a failed
    mitigation must never establish "the cause was addressed yet the problem
    persisted" and refute the root at belief 0."""
    case = _case()
    _fix_applied(case, turn=8, action_type=InvestigationActionType.MITIGATION)
    case.evidence.append(
        _evidence(
            "ev_a1b2c3d4e5f6", EvidenceCategory.SYMPTOM_EVIDENCE, 9, "still failing"
        )
    )
    assert m6_disconfirmation_basis(case) is None

    # ...and the identical shape with a SOLUTION does establish it.
    case2 = _case()
    _fix_applied(case2, turn=8, action_type=InvestigationActionType.SOLUTION)
    case2.evidence.append(
        _evidence(
            "ev_a1b2c3d4e5f6", EvidenceCategory.SYMPTOM_EVIDENCE, 9, "still failing"
        )
    )
    assert m6_disconfirmation_basis(case2) is not None


def test_persistence_window_keys_on_execution_not_the_offer_turn():
    """`proposed_in_turn` is when the fix was OFFERED; the user executes it a
    turn later. Keying the window on the offer let evidence recorded in the
    offering turn — before the fix ever ran — read as a post-fix outcome."""
    case = _case()
    _fix_applied(case, turn=8)  # proposed turn 7, executed turn 8
    # Symptom evidence from the OFFERING turn: pre-execution, so it observes
    # nothing about whether the fix held.
    case.evidence.append(
        _evidence(
            "ev_a1b2c3d4e5f6",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            7,
            "errors at the time the fix was proposed",
        )
    )
    assert m6_disconfirmation_basis(case) is None


def test_rcc_mirror_does_not_satisfy_the_backstop_leg():
    """A retracted conclusion must not keep reading as identified for one more
    turn through its own stale working-conclusion mirror.

    `cause_identification_leg` reads the PREVIOUS turn's working conclusion.
    Before the mirror existed that path always yielded the 0.0 placeholder,
    which could never clear the backstop threshold; the mirror carries the RCC's
    likelihood, which can.
    """
    from faultmaven.core.investigation.terminal_transitions import (
        cause_identification_leg,
    )
    from faultmaven.core.investigation.working_conclusion_generator import (
        generate_working_conclusion,
    )
    from faultmaven.modules.case.contracts import ConfidenceLevel, RootCauseConclusion

    case = _case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=_TRUE_ROOT,
        mechanism="m",
        confidence_level=ConfidenceLevel.from_score(0.9),
        likelihood=0.9,
    )
    case.working_conclusion = generate_working_conclusion(case, case.current_turn)
    assert case.working_conclusion.mirrors_root_cause_conclusion is True
    assert cause_identification_leg(case) == "rcc"

    # The conclusion is retracted this turn; the mirror is still last turn's.
    case.root_cause_conclusion = None
    assert cause_identification_leg(case) is None


def test_undatable_legacy_acceptance_is_labeled_apart_from_no_fix_applied():
    """A pre-#987 acceptance carries no `accepted_in_turn`, so it still refuses
    — but it must not be counted as "nothing was ever tried".

    While that series is nonzero it marks a REAL, bounded suppression of
    legitimate failed-fix demotions on in-flight cases. Folding it into the
    benign baseline would teach operators to read the suppression window as
    normal.
    """
    legacy = _case()
    legacy.proposed_actions.append(
        ProposedAction(
            case_id=legacy.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="Correct the OIDC provider ClientIDList",
            proposed_in_turn=7,
            state="accepted",  # accepted_in_turn left None, as pre-#987 rows are
        )
    )
    with patch(
        "faultmaven.core.investigation.causal_graph.m6_demotion_refused_total"
    ) as counter:
        assert m6_disconfirmation_basis(legacy) is None
    counter.labels.assert_called_once_with(reason="undatable_acceptance")

    # ...and a case where nothing was ever tried keeps the benign label.
    untouched = _case()
    with patch(
        "faultmaven.core.investigation.causal_graph.m6_demotion_refused_total"
    ) as counter:
        assert m6_disconfirmation_basis(untouched) is None
    counter.labels.assert_called_once_with(reason="no_fix_applied")


def test_undatable_label_ignores_mitigations_and_pending_offers():
    """The label tracks accepted SOLUTIONs specifically — a pending offer or an
    accepted MITIGATION is not a fix of the cause that we merely cannot date."""
    case = _case()
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.MITIGATION,
            description="fail over",
            proposed_in_turn=7,
            state="accepted",
        )
    )
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="not run yet",
            proposed_in_turn=7,
            state="pending",
        )
    )
    with patch(
        "faultmaven.core.investigation.causal_graph.m6_demotion_refused_total"
    ) as counter:
        assert m6_disconfirmation_basis(case) is None
    counter.labels.assert_called_once_with(reason="no_fix_applied")


# =============================================================================
# The incident replayed through the REAL turn pipeline (#987)
#
# Every test above enters at an internal seam — ingest_emitted_chain,
# _apply_hypothesis_evidence_links, demote_disconfirmed_cause_via_evidence.
# Those pin the PREDICATE. They cannot pin the WIRING: that an LLM emission
# arriving at process_turn actually reaches the guard. #987 was exactly a
# wiring defect — the boundary existed on the node axis while a second entry
# point routed around it — so a seam-only suite would have passed throughout
# the incident.
#
# This drives the incident emission verbatim (a causal_absence SUCCESS row,
# REFUTES-linked to the very root it confirms, on BOTH axes) through
# MilestoneEngine.process_turn with only the LLM boundary stubbed, and asserts
# the cascade does not start. It is the deterministic replacement for waiting
# on a ~1-in-3 simulator run to re-emit the shape.
# =============================================================================


def _incident_emission(root_node_id: str, hypothesis_id: str):
    """The turn-9 emission from case_e7d4f551d87b, verbatim in shape."""
    return {
        "agent_response": (
            "The ClientIDList fix worked — post-fix authentication succeeded."
        ),
        "state_updates": {
            "evidence_to_add": [
                {
                    "summary": (
                        "After replacing the source-account OIDC provider "
                        "audience with sts.amazonaws.com, web-identity "
                        "credentials and chained role assumption succeeded."
                    ),
                    "extract": "AssumeRoleWithWebIdentity 200; S3 processing OK",
                    "category": "causal_absence_evidence",
                    # Verbal post-fix confirmation, as in the incident (the
                    # schema requires a file id for file-backed source types).
                    "source_type": "user_description",
                },
                # POSITIVE CONTROL, same emission, ORDINARY category: its link
                # MUST land. Without it every assertion below is negative, so
                # a wiring defect that drops the emission before the guard —
                # the very defect class this file exists for — would pass. It
                # also proves the refusal is category-scoped rather than the
                # pipeline silently discarding all links.
                {
                    "summary": (
                        "CloudTrail shows AssumeRoleWithWebIdentity calls "
                        "with recipientAccountId 444455556666."
                    ),
                    "extract": "recipientAccountId=444455556666",
                    "category": "causal_evidence",
                    "source_type": "user_description",
                },
            ],
            # The inversion: the model REFUTES-links its own success
            # confirmation to the root that success proves, at full
            # confidence — on both belief axes.
            "node_evidence_links": [
                {
                    "node_ref": root_node_id,
                    "evidence_id_ref": "new_index_0",
                    "stance": "refutes",
                    "reasoning": (
                        "The corrected ClientIDList removed the identified "
                        "wrong audience registration, and post-fix "
                        "authentication succeeded."
                    ),
                    "stance_confidence": 1.0,
                },
                # The positive control's link — ordinary category, must land.
                {
                    "node_ref": root_node_id,
                    "evidence_id_ref": "new_index_1",
                    "stance": "supports",
                    "reasoning": "CloudTrail confirms the caller account.",
                    "stance_confidence": 0.9,
                },
            ],
            "hypothesis_evidence_links": [
                {
                    "hypothesis_id_ref": hypothesis_id,
                    "evidence_id_ref": "new_index_0",
                    "stance": "refutes",
                    "reasoning": "Post-fix authentication succeeded.",
                    "stance_confidence": 1.0,
                }
            ],
        },
    }


def _engine_for_incident(emission: dict):
    from unittest.mock import AsyncMock, MagicMock

    from faultmaven.core.investigation.milestone_engine import MilestoneEngine
    from faultmaven.core.investigation.schemas import (
        InvestigationResponse_Diagnosis,
    )

    llm = MagicMock()
    # Real strings on the two attributes the turn path reads off the provider
    # (a bare MagicMock leaks a non-str metric label and makes the
    # model-budget .startswith() truthy for every registry key).
    llm.provider_name = "test-provider"
    llm.config.default_model = "test-model"
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(llm, repo, investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis.model_validate(emission)
    )
    return engine


@pytest.mark.asyncio
async def test_incident_emission_through_process_turn_does_not_start_the_cascade():
    """THE gate: the #987 turn, driven through the real pipeline.

    Pre-#987 this exact emission drove the true root to REFUTED at belief 0,
    fired M6, minted a false persistence row, and cleared the conclusion.
    """
    case = _case()
    seed_problem_node(case)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=3,
    )
    root = case.causal_nodes[created[0]]
    hypothesis = _hypothesis(
        "hyp_421fd53b5fd7", HypothesisState.ACTIVE, root_node_id=root.node_id
    )
    case.hypotheses[hypothesis.hypothesis_id] = hypothesis

    engine = _engine_for_incident(
        _incident_emission(root.node_id, hypothesis.hypothesis_id)
    )
    result = await engine.process_turn(
        case=case, user_message="Fixed the ClientIDList — auth works now."
    )

    updated = result["case_updated"]
    root_after = updated.causal_nodes[root.node_id]

    # 1. The root the fix CONFIRMED is not refuted, and keeps belief.
    assert root_after.node_state is not NodeState.REFUTED
    assert root_after.belief > 0

    # 2. The hypothesis is not refuted off the back of its own confirmation.
    assert (
        updated.hypotheses[hypothesis.hypothesis_id].state
        is not HypothesisState.REFUTED
    )

    # 3. M6 never fired: NO engine-authored row exists at all. (Asserting the
    #    retired fabrication sentence is absent would be vacuous — the current
    #    mint writes "Engine inference (M6), not an observation: …", so that
    #    string cannot appear whether or not the cascade regresses; its absence
    #    is separately pinned where M6 legitimately fires.)
    assert not [e for e in updated.evidence if e.collected_by == ENGINE_EVIDENCE_AUTHOR]

    # 4. No refuting stance survives on EITHER axis. Scanning the node axis
    #    alone left the hypothesis axis unguarded: removing that guard kept
    #    this test green while a confidence-1.0 REFUTES landed on the
    #    hypothesis and eroded its likelihood.
    assert not [
        link
        for link in root_after.evidence_links
        if link.stance is EvidenceStance.REFUTES
    ]
    hypothesis_after = updated.hypotheses[hypothesis.hypothesis_id]
    assert not [
        link
        for link in hypothesis_after.evidence_links
        if link.stance is EvidenceStance.REFUTES
    ]
    assert (
        hypothesis_after.likelihood >= hypothesis.likelihood
    ), "the hypothesis must not be eroded by its own success confirmation"

    # 5. POSITIVE CONTROL — the emission genuinely reached the link-application
    #    path, and the refusal is CATEGORY-scoped rather than the pipeline
    #    discarding links wholesale. Without this, assertions 1-4 are all
    #    negative, so a wiring defect that drops the emission before the guard
    #    — the very defect class this file exists for — passes silently.
    ordinary = [
        e for e in updated.evidence if e.category is EvidenceCategory.CAUSAL_EVIDENCE
    ]
    assert ordinary, "the ordinary evidence row from the same emission must land"
    assert [
        link
        for link in root_after.evidence_links
        if link.evidence_id == ordinary[-1].evidence_id
        and link.stance is EvidenceStance.SUPPORTS
    ], "the ordinary row's link must land — only ABSENCE links are refused"


@pytest.mark.asyncio
async def test_incident_emission_is_metered_on_both_axes_through_the_pipeline():
    """The refusal is observable end-to-end, not just at the seam — the
    counter is how a model that routinely mis-links absence rows stays
    distinguishable from one that follows the contract."""
    case = _case()
    seed_problem_node(case)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=3,
    )
    root = case.causal_nodes[created[0]]
    hypothesis = _hypothesis(
        "hyp_421fd53b5fd7", HypothesisState.ACTIVE, root_node_id=root.node_id
    )
    case.hypotheses[hypothesis.hypothesis_id] = hypothesis

    engine = _engine_for_incident(
        _incident_emission(root.node_id, hypothesis.hypothesis_id)
    )
    with patch(
        "faultmaven.core.investigation.cause_assurance."
        "absence_row_link_refused_total"
    ) as counter:
        await engine.process_turn(
            case=case, user_message="Fixed the ClientIDList — auth works now."
        )

    axes = {c.kwargs.get("axis") for c in counter.labels.call_args_list}
    assert axes == {
        "node",
        "hypothesis",
    }, f"both belief axes must refuse and meter the incident emission; got {axes}"
