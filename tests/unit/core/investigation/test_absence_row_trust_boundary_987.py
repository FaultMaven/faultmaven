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
from faultmaven.modules.case.contracts import (
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
        organization_id="o",
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


def _fix_applied(case, turn=8):
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="Correct the OIDC provider ClientIDList",
            proposed_in_turn=turn,
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
    assert "turn 8" in provenance


def test_m6_engine_row_records_inference_with_provenance_not_an_observation():
    """The fabrication itself: the minted row must not assert a first-person
    observation the engine never made."""
    case = _case()
    _fix_applied(case, turn=8)
    case.evidence.append(
        _evidence(
            "ev_a1b2c3d4e5f6", EvidenceCategory.SYMPTOM_EVIDENCE, 9, "still failing"
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
    hyp = _hypothesis(
        "hyp_421fd53b5fd7",
        HypothesisState.REFUTED,
        root_node_id=created[0],
        refutation_reason="fix had no effect",
    )
    case.hypotheses[hyp.hypothesis_id] = hyp
    case.progress.cause_state = CauseState.IDENTIFIED

    assert demote_disconfirmed_cause_via_evidence(case) is True
    engine_rows = [e for e in case.evidence if e.collected_by == "engine"]
    assert len(engine_rows) == 1
    summary = engine_rows[0].summary
    assert "Engine inference" in summary
    # The retired fabrication, verbatim — it must never come back.
    assert "yet the problem persisted." not in summary
    assert "turn 8" in summary


def test_m6_does_not_fire_when_preconditions_are_unestablished():
    """Same setup, minus the persistence observation: no engine refutation is
    minted and the root is not driven to belief 0."""
    case = _case()
    _fix_applied(case, turn=8)
    seed_problem_node(case)
    created = ingest_emitted_chain(
        case,
        nodes_to_add=[_root_spec(_TRUE_ROOT)],
        edges_to_add=[],
        node_evidence=[],
        current_turn=case.current_turn,
    )
    hyp = _hypothesis(
        "hyp_421fd53b5fd7",
        HypothesisState.REFUTED,
        root_node_id=created[0],
        refutation_reason="fix had no effect",
    )
    case.hypotheses[hyp.hypothesis_id] = hyp
    case.progress.cause_state = CauseState.IDENTIFIED

    assert demote_disconfirmed_cause_via_evidence(case) is False
    assert [e for e in case.evidence if e.collected_by == "engine"] == []


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
    # ...and the record carries HOW it was established, not a bare assertion.
    established = case.root_cause_conclusion.established_by
    assert established and "user-confirmed resolution at turn 11" in established
    assert "ev_47b2f3337ffc" in established
