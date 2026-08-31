"""#1195 — a restatement-held root must not be disposed INSUFFICIENT_EVIDENCE.

On a case whose ROOT is held by the §7.1 restatement guard alone, the engine
used to emit opposite guidance to its two audiences in the same turn: the
model-facing ``<causal_graph>`` annotation said "MORE SUPPORTING EVIDENCE WILL
NOT VALIDATE IT" (#1140) while the user-facing disposition said
``insufficient_evidence`` and offered "Share data that would distinguish the
causes". The live case (``case_a3d354f08765``) had 100% evidence coverage, 14
evidence rows and 3 independent qualifying causal supports against a bar of 2 —
no amount of the data being requested could move the hold, because the hold is
LEXICAL.

The carve-out is deliberately NARROW, and most of this file pins its edges
rather than its centre. It applies only when all three hold:

1. the stall is a TIME stall — a model-declared data wall is an explicit
   assertion that the data cannot be had, which is the honest
   ``INSUFFICIENT_EVIDENCE`` archetype and must win;
2. the guard holds a root; and
3. it holds EVERY unsettled root — while some other live root is blocked by
   something evidence CAN move, the case-level claim "more data will not help"
   is false.

Each of those, and the sibling-overlap discrimination in the affordance, was
added because the first cut got it wrong in a way that produced the same class
of wrong guidance this issue exists to remove, inverted.

These pins fixed the contradiction only; releasing the held root was left open
as the #1122 product decision. fm#1122 has since taken it: a root whose whole
overlap ONE standing explanation accounts for is released as a DUPLICATE
(``test_unattached_duplicate_no_longer_frames_its_own_root``), while a root the
standing explanations SPAN stays held (the #656 pins in
``test_restatement_guard_calibration.py``, which are untouched). The disposition
and closure behaviour below is unchanged — only which roots reach it. Neither
``ROOT_NOVELTY_MIN_FRACTION`` nor ``_FRAME_OWNER_JACCARD`` moved.

The fixture is the live incident's own statements: a terse ROOT, its ATTACHED
hypothesis, and the turn-11 near-duplicate whose ``root_node_ref`` adoption the
#1091 one-cause-one-chain guard refused — which is what leaves it unattached and
frames its own root (novelty 1/9 against a 0.30 bar).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation import cause_assurance, milestone_engine
from faultmaven.core.investigation.causal_graph import (
    restatement_held_root_ids,
    summarize_restatement_hold,
)
from faultmaven.core.investigation.cause_assurance import _graph_hooks
from faultmaven.core.investigation.exhaustion_thresholds import (
    EXHAUSTION_MIN_TURNS,
    EXHAUSTION_STALL_THRESHOLD,
)
from faultmaven.core.investigation.milestone_engine import (
    _GATE_VERIFICATION_STATUS,
    _insufficient_evidence_handoff_pending,
    _insufficient_evidence_handoff_suggestions,
    _restatement_held_pending,
    _restatement_held_suggestions,
    _terminal_confirmation_response,
    engine_owned_affordances,
)
from faultmaven.core.investigation.terminal_transitions import derive_closure_reason
from faultmaven.core.investigation.verification_status import (
    VerificationStatus,
    assess_verification_status,
    is_progress_stalled,
    is_stalled,
    restatement_hold_governs,
    work_gate_passed,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NeedObtainability,
    NeedPurpose,
    NeedState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


# --- the live incident's own text (case_a3d354f08765) ------------------------

SYMPTOM = (
    "The production payment-processor deployment in the `payments` namespace is "
    "currently unavailable or unstable because its v2.1.4 pods enter "
    "CrashLoopBackOff after 2-3 minutes, causing customer payment failures."
)
# The SIBLING-INVOLVING hold, in the incident's own domain: a DISJUNCTION root
# covered by the union of the case's two standing candidate causes and by
# neither alone (solo residues 4/9 and 3/9 against a 0.30 bar; union residue
# empty). The incident's original shape — one unattached DUPLICATE of the
# root's own hypothesis — is released by the fm#1122 attribution test and can
# no longer stand in for a held root here; it is pinned as released in
# ``test_derive_node_states.py::test_fm1122_incident_shape_now_validates``.
ROOT_STATEMENT = (
    "JVM heap exhaustion or node memory eviction terminating the "
    "payment-processor pods"
)
DISJUNCT_A = (
    "JVM heap exhaustion inside the 400Mi container drives total RSS past the "
    "cgroup limit, so the kernel terminates the payment-processor process with "
    "SIGKILL exit 137"
)
DISJUNCT_B = (
    "Node memory eviction removes the payment-processor pods when the kubelet "
    "reclaims memory under node pressure, terminating them mid-request"
)
ROOT_ID = "cn_597a37af74c7"

# The ANCHOR-ONLY variant: a ROOT that restates the PROBLEM STATEMENT itself.
# ``_node_restates`` unions the anchors with the sibling statements, so this is
# held with no two hypotheses overlapping anywhere.
ANCHOR_SYMPTOM = "Checkout requests intermittently return 502 errors under peak load"
ANCHOR_ROOT_STATEMENT = "Intermittent 502 errors under peak load"
UNRELATED_HYPOTHESIS_A = "A misbehaving sidecar proxy drops connections during rollout"
UNRELATED_HYPOTHESIS_B = "Disk pressure on the node evicts pods before readiness"


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _causal_evidence(label: str, summary: str) -> Evidence:
    """A qualifying §7.1 causal support: ``CAUSAL_EVIDENCE`` category, and (via
    the link's ``stance_confidence``) above ``CAUSAL_STANCE_CONFIDENCE_MIN``."""
    return Evidence(
        evidence_id=_eid(label),
        summary=summary,
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=5,
        collected_at=datetime.now(timezone.utc),
    )


def _restatement_held_case(
    *,
    duplicate_attached: bool = False,
    current_turn: int = 15,
    turns_without_progress: int = 5,
    include_second_hypothesis: bool = True,
    second_hypothesis_category: HypothesisCategory = HypothesisCategory.CONFIG,
    extra_unsettled_root: bool = False,
    declared_wall: bool = False,
    anchor_only_hold: bool = False,
) -> Case:
    """A ROOT that clears the §7.1 grounding bar (two INDEPENDENT causal
    supports, both confident, against a bar of 2) and is held at INCONCLUSIVE by
    the restatement guard alone: a DISJUNCTION of the case's two standing
    candidate causes, which the fm#1122 attribution test leaves held because no
    single sibling accounts for it.

    Each knob turns exactly ONE premise of the carve-out off, so every pin below
    differs from the baseline in one respect only:

    - ``duplicate_attached`` — anchoring the SECOND disjunct to the root makes it
      the root's OWN hypothesis, which drops it out of the frame. The remaining
      sibling leaves the root 0.444 novel, so it clears the NOVELTY bar outright
      and the guard never reaches the fm#1122 attribution test — an otherwise
      identical genuine INSUFFICIENT_EVIDENCE stall. (Measured, not assumed: the
      knob turns off the novelty premise, not the attribution premise.)
    - ``extra_unsettled_root`` — a second live ROOT with no evidence at all. The
      hold is still there; it is no longer the case's SOLE block.
    - ``declared_wall`` — one outstanding ``CAUSAL_VERIFICATION`` need per
      hypothesis, all ``UNOBTAINABLE``, so the stall comes from the model's own
      declaration rather than from the clock.
    - ``anchor_only_hold`` — the ROOT restates the problem statement and the two
      hypotheses are unrelated to it and to each other, so the hold survives with
      no sibling overlap anywhere.
    - ``second_hypothesis_category`` — set equal to the first to fail the work
      gate on CATEGORIES while keeping two hypotheses, which is what keeps the
      unattached duplicate in the frame and the hold alive below the gate.
    """
    symptom = ANCHOR_SYMPTOM if anchor_only_hold else SYMPTOM
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=current_turn,
        inquiry=InquiryData(
            proposed_problem_statement=symptom,
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement=symptom, severity=CaseSeverity.HIGH
        ),
    )
    case.turns_without_progress = turns_without_progress
    case.progress.symptom_verified = True
    case.evidence = [
        _causal_evidence(
            "e1", "container memory cgroup limit is 400Mi on the payments deployment"
        ),
        _causal_evidence(
            "e2", "exit code 137 recorded by kubelet on the terminated process"
        ),
    ]

    problem = CausalNode(
        node_id="cn_0000000000d0",
        statement=symptom,
        node_type=NodeType.PROBLEM,
        generated_at_turn=1,
    )
    root = CausalNode(
        node_id=ROOT_ID,
        statement=ANCHOR_ROOT_STATEMENT if anchor_only_hold else ROOT_STATEMENT,
        node_type=NodeType.ROOT,
        node_state=NodeState.INCONCLUSIVE,
        generated_at_turn=5,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=_eid("e1"),
                stance=EvidenceStance.SUPPORTS,
                reasoning="the cgroup limit is the ceiling being hit",
                stance_confidence=0.99,
            ),
            NodeEvidenceLink(
                evidence_id=_eid("e2"),
                stance=EvidenceStance.SUPPORTS,
                reasoning="exit 137 is the OOM kill",
                stance_confidence=0.95,
            ),
        ],
    )
    case.causal_nodes = {problem.node_id: problem, root.node_id: root}
    case.causal_edges = [
        CausalEdge(cause_node_id=root.node_id, effect_node_id=problem.node_id)
    ]

    if extra_unsettled_root:
        # A live ROOT with ZERO evidence links: blocked by something more data
        # very much CAN move.
        orphan = CausalNode(
            node_id="cn_0000deadbeef",
            statement="a second, wholly ungrounded candidate cause",
            node_type=NodeType.ROOT,
            node_state=NodeState.INCONCLUSIVE,
            generated_at_turn=9,
        )
        case.causal_nodes[orphan.node_id] = orphan

    hypotheses = [
        Hypothesis(
            hypothesis_id="hyp_e44ffbfe6fa4",
            statement=(UNRELATED_HYPOTHESIS_A if anchor_only_hold else DISJUNCT_A),
            category=HypothesisCategory.ENVIRONMENT,
            state=HypothesisState.ACTIVE,
            rationale="a reason",
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            generated_at_turn=5,
            root_node_id=None,
        )
    ]
    if include_second_hypothesis:
        hypotheses.append(
            Hypothesis(
                hypothesis_id="hyp_fd24a60ab341",
                statement=(UNRELATED_HYPOTHESIS_B if anchor_only_hold else DISJUNCT_B),
                category=second_hypothesis_category,
                state=HypothesisState.ACTIVE,
                rationale="a reason",
                generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
                generated_at_turn=11,
                # Unattached: the #1091 one-cause-one-chain guard REFUSED the
                # adoption, which is what puts it in the root's own frame.
                root_node_id=ROOT_ID if duplicate_attached else None,
            )
        )
    case.hypotheses = {h.hypothesis_id: h for h in hypotheses}

    if declared_wall:
        case.evidence_needs = [
            EvidenceNeed(
                need_id=f"eneed_{i:012x}",
                case_id=case.case_id,
                purpose=NeedPurpose.CAUSAL_VERIFICATION,
                request_text="the capture that would separate the candidates",
                rationale="it discriminates between the residual candidates",
                state=NeedState.PENDING,
                obtainability=NeedObtainability.UNOBTAINABLE,
                motivating_hypothesis_ids=[h.hypothesis_id],
                created_at_turn=2,
            )
            for i, h in enumerate(hypotheses)
        ]
    return case


# ---------------------------------------------------------------------------
# Fixture premises — without these the pins below could pass vacuously
# ---------------------------------------------------------------------------


def test_fixture_is_actually_restatement_held():
    """The hold is real, it is the RESTATEMENT guard rather than a grounding bar
    (the root carries two INDEPENDENT qualifying causal supports against a §7.1
    bar of 2, so ``restatement_held_root_ids`` — which requires the grounding bar
    already MET — is what reports it), and it is the case's SOLE root block."""
    case = _restatement_held_case()
    assert restatement_held_root_ids(case) == {ROOT_ID}
    hold = summarize_restatement_hold(case)
    assert hold is not None
    assert hold.root_ids == frozenset({ROOT_ID})
    assert hold.is_sole_root_block is True
    assert hold.involves_siblings is True


def test_fixture_crosses_the_work_gate_and_is_time_stalled():
    """The cell under test is the WORK-GATED, TIME-stalled one. Asserted through
    the real predicates and the shared constants rather than hardcoded integers:
    a fixture that quietly stopped being stalled would move every pin below into
    the OPEN cell and they would all still pass."""
    case = _restatement_held_case()
    assert work_gate_passed(case) is True
    assert is_stalled(case) is True
    assert is_progress_stalled(case) is True
    assert case.current_turn >= EXHAUSTION_MIN_TURNS
    assert case.turns_without_progress >= EXHAUSTION_STALL_THRESHOLD


def test_same_case_without_the_hold_is_insufficient_evidence():
    """The discriminator, and the guard against an over-broad carve-out.

    ATTACHING the near-duplicate makes it the root's OWN hypothesis, so it
    leaves the frame and the hold dissolves — with the causal graph, the
    evidence, the hypothesis count and the stall all unchanged. That case is a
    genuine ungrounded stall and must still dispose INSUFFICIENT_EVIDENCE and
    still fire the original handoff. So #1195 removes the disposition for the
    restatement hold specifically, not for stalls generally."""
    case = _restatement_held_case(duplicate_attached=True)
    assert restatement_held_root_ids(case) == set()
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE
    assert _insufficient_evidence_handoff_pending(case) is True
    assert engine_owned_affordances(case)[0] == "insufficient_evidence"


def test_restatement_hold_hook_is_registered():
    """The join, the affordance and the closure reason all read the hold through
    the ``cause_assurance`` graph-hook seam (a direct import closes the
    causal_graph -> hypothesis_manager -> terminal_transitions ->
    verification_status cycle). They read it with ``.get``, so a missing
    registration would degrade SILENTLY back to the pre-#1195 contradiction."""
    assert _graph_hooks().get("restatement_hold") is summarize_restatement_hold


def test_cleared_hooks_degrade_to_the_pre_fix_reading_without_raising():
    """The honest form of the degradation, pinned rather than assumed away.

    ``_graph_hooks()``'s import fallback does NOT repopulate a dict that was
    emptied after import (``import_module`` returns the cached module), and two
    existing suites patch ``_GRAPH_HOOKS`` to ``{}``. Inside such a block the
    carve-out is simply absent. What must hold is that the engine keeps working
    and falls back to the pre-#1195 answer rather than raising on a live turn —
    and that the affordance drops the claim it can no longer substantiate."""
    case = _restatement_held_case()
    with patch.object(cause_assurance, "_GRAPH_HOOKS", {}):
        assert (
            assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE
        )
        moves = _restatement_held_suggestions(case)
        assert len(moves) == 1
    assert assess_verification_status(case) == VerificationStatus.RESTATEMENT_HELD


# ---------------------------------------------------------------------------
# The join — the disposition itself
# ---------------------------------------------------------------------------


def test_restatement_held_root_is_not_insufficient_evidence():
    """THE pin. A time-stalled, work-gated, ungrounded case whose only block is
    the §7.1 restatement guard reads its own cell, never INSUFFICIENT_EVIDENCE."""
    case = _restatement_held_case()
    assert assess_verification_status(case) == VerificationStatus.RESTATEMENT_HELD


def test_insufficient_evidence_handoff_is_not_pending_while_a_root_is_held():
    """The issue's required pin, in its own words: the handoff is False while
    ``restatement_held_root_ids(case)`` is non-empty."""
    case = _restatement_held_case()
    assert restatement_held_root_ids(case)
    assert _insufficient_evidence_handoff_pending(case) is False


def test_another_live_root_blocked_by_evidence_defeats_the_carve_out():
    """Premise 3 — the hold must be the SOLE root block.

    A second unsettled ROOT with no causal evidence at all is blocked by exactly
    what more data fixes. The held root is still held, but the CASE-level claim
    the carve-out licenses ("more supporting evidence will not move this
    forward") is then false, so the honest evidence reading must stand."""
    case = _restatement_held_case(extra_unsettled_root=True)
    assert restatement_held_root_ids(case) == {ROOT_ID}  # still held
    hold = summarize_restatement_hold(case)
    assert hold is not None and hold.is_sole_root_block is False
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE
    assert _insufficient_evidence_handoff_pending(case) is True
    assert engine_owned_affordances(case)[0] == "insufficient_evidence"


def test_a_settled_second_root_does_not_defeat_the_carve_out():
    """The other side of premise 3, so the sole-block rule is not simply "one
    root". A REFUTED root is a settled question — nothing about it is waiting on
    data — so it must not veto the carve-out."""
    case = _restatement_held_case(extra_unsettled_root=True)
    case.causal_nodes["cn_0000deadbeef"].node_state = NodeState.REFUTED
    hold = summarize_restatement_hold(case)
    assert hold is not None and hold.is_sole_root_block is True
    assert assess_verification_status(case) == VerificationStatus.RESTATEMENT_HELD


def test_declared_data_wall_beats_the_carve_out():
    """Premise 1 — the stall's ARM decides.

    Here the model has EXPLICITLY declared every discriminator unobtainable, so
    ``is_progress_stalled`` is True through ``_declared_wall`` and not through
    the clock. That is the canonical insufficient-evidence archetype and the
    whole reason the wall arm exists; answering it with "the block is only
    phrasing" would contradict the model's own assertion — the same failure as
    the one #1195 removes, inverted."""
    case = _restatement_held_case(
        current_turn=3, turns_without_progress=0, declared_wall=True
    )
    assert is_stalled(case) is False  # not the clock
    assert is_progress_stalled(case) is True  # the wall
    assert restatement_held_root_ids(case) == {ROOT_ID}  # still held
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE
    # The AFFORDANCES are the composite pair (next class) — the status is what
    # this pin is about, and it stays with the wall.
    assert engine_owned_affordances(case)[0].startswith("insufficient_evidence")


def test_declared_wall_beats_the_carve_out_even_when_also_time_stalled():
    """And it wins when BOTH arms are true — otherwise the rule would be "the
    wall only counts when the clock has not run out", which is not a claim
    anyone would defend."""
    case = _restatement_held_case(declared_wall=True)
    assert is_stalled(case) is True
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE


def test_a_promoted_cause_defeats_the_carve_out():
    """A VALIDATED root means the case HAS a cause, so the hold on some sibling
    is not what governs it — and any consumer saying "a cause was supported but
    never stated distinctly" would be describing a case with a stated cause.

    The status path was shielded from this by ``_is_grounded``; the closure path
    was not, and closed such a case as ``closed_restatement_held`` (#1195
    review). The guard now lives in the shared predicate, so neither can."""
    case = _restatement_held_case()
    promoted = CausalNode(
        node_id="cn_00000000aaaa",
        statement="the promoted cause: a stale sidecar image tag on the deployment",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        actionable=True,
        generated_at_turn=4,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=_eid("e1"),
                stance=EvidenceStance.SUPPORTS,
                reasoning="observed directly",
                stance_confidence=0.99,
            )
        ],
    )
    case.causal_nodes[promoted.node_id] = promoted
    case.causal_edges.append(
        CausalEdge(cause_node_id=promoted.node_id, effect_node_id="cn_0000000000d0")
    )
    assert restatement_held_root_ids(case) == {ROOT_ID}  # still held
    assert summarize_restatement_hold(case).is_sole_root_block is False
    assert restatement_hold_governs(case) is None
    assert derive_closure_reason(case) != "closed_restatement_held"


def test_the_carve_out_requires_a_verified_symptom():
    """The claim is "the evidence already grounds a cause, so more data will not
    help". Without ``symptom_verified`` the PROBLEM itself was never established
    from data, so that claim is about a symptom nobody confirmed — and
    ``_is_grounded`` demands the same anchor for the same reason. It also keeps
    the closure report's "the reported problem was never established" arm
    reachable for this population."""
    case = _restatement_held_case()
    case.progress.symptom_verified = False
    assert restatement_held_root_ids(case) == {ROOT_ID}  # the hold is real
    assert summarize_restatement_hold(case).is_sole_root_block is True
    assert restatement_hold_governs(case) is None  # but it does not govern
    assert assess_verification_status(case) == VerificationStatus.INSUFFICIENT_EVIDENCE
    assert engine_owned_affordances(case)[0] == "insufficient_evidence"
    assert derive_closure_reason(case) != "closed_restatement_held"


def test_governing_predicate_is_one_read_not_a_checklist():
    """Both guards are carried by ONE predicate on purpose: this fix twice
    shipped a consumer that applied some of them and not others. A caller that
    reads ``restatement_hold_governs`` cannot half-apply it."""
    assert restatement_hold_governs(_restatement_held_case()) is not None
    for case in (
        _restatement_held_case(extra_unsettled_root=True),
        _restatement_held_case(duplicate_attached=True),
    ):
        assert restatement_hold_governs(case) is None
    unanchored = _restatement_held_case()
    unanchored.progress.symptom_verified = False
    assert restatement_hold_governs(unanchored) is None


def test_hold_does_not_pre_empt_a_progressing_case():
    """Scoped to the STALLED cell. A case still making progress reads OPEN,
    which asserts nothing false about its evidence and needs no corrective."""
    case = _restatement_held_case(current_turn=3, turns_without_progress=0)
    assert restatement_held_root_ids(case) == {ROOT_ID}
    assert is_progress_stalled(case) is False
    assert assess_verification_status(case) == VerificationStatus.OPEN
    assert engine_owned_affordances(case) is None


def test_hold_does_not_pre_empt_the_work_gate():
    """Scoped BELOW the work gate too — and non-vacuously: the hold is REAL here.

    Two hypotheses (so the unattached duplicate still frames the root) in ONE
    category, which fails the gate on breadth while leaving the hold intact.
    ``NOT_YET_PRODUCTIVE`` is a provider-health reading ("too little work to
    judge"), never a per-case evidence verdict, so it does not contradict the
    hold and is left alone."""
    case = _restatement_held_case(
        second_hypothesis_category=HypothesisCategory.ENVIRONMENT
    )
    assert restatement_held_root_ids(case) == {ROOT_ID}  # the hold is real
    assert is_progress_stalled(case) is True  # and it IS stalled
    assert work_gate_passed(case) is False  # but below the gate
    assert assess_verification_status(case) == VerificationStatus.NOT_YET_PRODUCTIVE


# ---------------------------------------------------------------------------
# The replacement affordance — suppression alone would be the OTHER failure
# ---------------------------------------------------------------------------


class TestRestatementHeldHandoff:
    """``_insufficient_evidence_handoff_pending`` exists so the engine "must not
    spin silently or fabricate a cause on a walled case". Gating it without
    supplying anything in its place converts a contradictory case into a silent
    one — the exact failure it was written to prevent. These pin the
    replacement."""

    def test_handoff_fires_with_its_own_gate_label(self):
        case = _restatement_held_case()
        result = engine_owned_affordances(case)
        assert result is not None
        gate, affordances = result
        assert gate == "restatement_held"
        assert len(affordances) == 2
        assert _restatement_held_pending(case) is True

    def test_the_moves_are_a_restatement_ask_not_a_data_ask(self):
        """The whole point: the hold is about the root's PHRASING, so the moves
        concern restating the cause distinctly."""
        moves = _restatement_held_suggestions(_restatement_held_case())
        assert moves != _insufficient_evidence_handoff_suggestions()
        blob = " ".join(m["label"] + " " + m["body"] for m in moves).lower()
        assert "mechanism" in blob
        assert "same cause" in blob
        # The insufficient-evidence ask must not survive into this pair.
        assert "distinguish the causes" not in blob
        assert "diagnostic angle" not in blob

    def test_anchor_only_hold_does_not_claim_a_sibling_overlap(self):
        """``_node_restates`` unions the problem anchors with the sibling
        statements, so a root that restates the PROBLEM alone is held with no
        two hypotheses overlapping. Offering "two of the causes on the table may
        be one cause worded twice" there asserts an overlap that does not exist —
        wrong guidance of the same class this issue removes. The mechanism move,
        which is true of every held shape, is offered alone."""
        case = _restatement_held_case(anchor_only_hold=True)
        hold = summarize_restatement_hold(case)
        assert hold is not None
        assert hold.root_ids == frozenset({ROOT_ID})  # held...
        assert hold.involves_siblings is False  # ...by the anchors alone
        assert assess_verification_status(case) == VerificationStatus.RESTATEMENT_HELD

        gate, moves = engine_owned_affordances(case)
        assert gate == "restatement_held"
        assert len(moves) == 1
        blob = (moves[0]["label"] + " " + moves[0]["body"]).lower()
        assert "mechanism" in blob
        assert "same cause" not in blob
        assert "worded twice" not in blob

    def test_the_moves_never_steer_toward_close(self):
        """D4 no-soft-collapse, as for all peers: a hold the engine can describe
        is not a reason to nudge the user toward abandoning the case.
        Non-clickable FREE_SPEECH — the user supplies the content."""
        for case in (
            _restatement_held_case(),
            _restatement_held_case(anchor_only_hold=True),
        ):
            moves = _restatement_held_suggestions(case)
            assert {m["action_type"] for m in moves} == {"FREE_SPEECH"}
            assert not any(m.get("payload") or m.get("intent") for m in moves)
            blob = " ".join(m["label"] + " " + m["body"] for m in moves).lower()
            for word in ("close", "pause", "abandon", "give up", "stop here"):
                assert word not in blob, f"soft-collapse language: {word!r}"

    def test_scoped_to_investigating(self):
        """A stall outside INVESTIGATING is a different concern; the reading is
        only meaningful mid-investigation."""
        case = _restatement_held_case()
        case.state = CaseState.INQUIRY
        assert _restatement_held_pending(case) is False

    def test_a_gateless_non_investigating_turn_computes_no_join(self):
        """``engine_owned_affordances`` hoists the INVESTIGATING guard out of the
        four predicates so it can compute the join once. That must not make a
        gate-less INQUIRY or terminal turn newly pay a join it previously
        short-circuited past — all four predicates rejected it on state before
        touching the graph."""
        case = _restatement_held_case()
        case.state = CaseState.INQUIRY
        calls = {"n": 0}
        real = cause_assurance._GRAPH_HOOKS["restatement_hold"]

        def counting(c):
            calls["n"] += 1
            return real(c)

        with patch.dict(cause_assurance._GRAPH_HOOKS, {"restatement_hold": counting}):
            assert engine_owned_affordances(case) is None
        assert calls["n"] == 0

    def _count_joins(self, case):
        """Count ``assess_verification_status`` calls made by one dispatch.

        Counting graph SWEEPS would be vacuous here: a progressing case never
        reaches the hold read inside the join, so the sweep count is 0 whether
        or not the guard exists. What the guard actually saves is the join
        itself — ``grade_cause_assurance`` plus a ``work_gate_passed`` rebuild
        of every evidence datum key — so that is what this counts."""
        calls = {"n": 0}
        real = milestone_engine.assess_verification_status

        def counting(c, **kw):
            calls["n"] += 1
            return real(c, **kw)

        with patch.object(milestone_engine, "assess_verification_status", counting):
            result = engine_owned_affordances(case)
        return result, calls["n"]

    def test_a_progressing_turn_computes_no_join(self):
        """The ordinary turn. All four readings require a stall, so the cheap
        stall guard is hoisted alongside the INVESTIGATING one — without it a
        PROGRESSING turn pays the whole join, twice a turn, where each predicate
        previously returned early (#1195 review)."""
        case = _restatement_held_case(current_turn=3, turns_without_progress=0)
        assert is_progress_stalled(case) is False
        result, n = self._count_joins(case)
        assert result is None
        assert n == 0, f"progressing turn computed the join {n}x"

    def test_a_firing_turn_computes_the_join_once(self):
        """And the turn that does fire computes it ONCE for all four readings,
        rather than each predicate recomputing it."""
        _result, n = self._count_joins(_restatement_held_case())
        assert n == 1, f"firing turn computed the join {n}x"

    def _count_sweeps(self, case):
        calls = {"n": 0}
        real = cause_assurance._GRAPH_HOOKS["restatement_hold"]

        def counting(c):
            calls["n"] += 1
            return real(c)

        with patch.dict(cause_assurance._GRAPH_HOOKS, {"restatement_hold": counting}):
            result = engine_owned_affordances(case)
        return result, calls["n"]

    def test_a_firing_turn_sweeps_the_graph_a_bounded_number_of_times(self):
        """The exact cost of a turn that fires, pinned rather than asserted.

        The dispatch reads the hold ONCE and hands it to both suggestion
        builders; each used to derive its own, which re-swept the graph and
        opened a window where two reads of one fact could disagree (#1195
        review). What remains is the join's own internal read on its way to
        RESTATEMENT_HELD — it returns a single value, so that read cannot be
        handed back out. Hence 2 on the held turn and 1 on the composite (whose
        join returns at the wall before ever consulting the hold). A regression
        to 3+ means a builder started deriving its own again."""
        (gate, _moves), n = self._count_sweeps(_restatement_held_case())
        assert gate == "restatement_held"
        assert n == 2, f"held turn swept {n}x"

        (gate, _moves), n = self._count_sweeps(
            _restatement_held_case(declared_wall=True)
        )
        assert gate == "insufficient_evidence_restatement_held"
        assert n == 1, f"composite turn swept {n}x"

    def test_state_machine_gates_take_precedence(self):
        """Ordered with its peers, BELOW any pending state-machine handshake."""
        case = _restatement_held_case()
        override = [{"label": "Yes, resolve it", "action_type": "DECIDE"}]
        gate, affordances = engine_owned_affordances(
            case, {"override_suggestions": override}
        )
        assert gate == "disposition"
        assert affordances == override

    def test_peers_are_mutually_exclusive_on_this_case(self):
        """All four mid-investigation branches read the same join, and a case
        has exactly one verification status — so exactly one can fire."""
        from faultmaven.core.investigation.milestone_engine import (
            _hypothesis_vacuum_pending,
            _treatment_blocked_pending,
        )

        case = _restatement_held_case()
        pending = [
            _insufficient_evidence_handoff_pending(case),
            _restatement_held_pending(case),
            _hypothesis_vacuum_pending(case),
            _treatment_blocked_pending(case),
        ]
        assert sum(bool(p) for p in pending) == 1

    def test_precomputed_status_matches_a_fresh_one(self):
        """``engine_owned_affordances`` computes the join ONCE and passes it
        down. A predicate that ignored the argument, or read a different field
        from it, would silently diverge from the status the case is reported
        under — so pin that the two forms agree in both directions."""
        held = _restatement_held_case()
        plain = _restatement_held_case(duplicate_attached=True)
        assert _restatement_held_pending(
            held, status=assess_verification_status(held)
        ) is _restatement_held_pending(held)
        assert _restatement_held_pending(
            plain, status=assess_verification_status(plain)
        ) is _restatement_held_pending(plain)
        assert (
            _restatement_held_pending(
                held, status=VerificationStatus.INSUFFICIENT_EVIDENCE
            )
            is False
        )


class TestWallAndHoldComposite:
    """A declared data wall AND a governing hold, in one case (#1195 review).

    NEITHER cell is true alone: the wall's "no cause can be grounded from
    currently available data" is false about a root three independent supports
    already ground, and the hold's "the block is lexical, not evidential" is
    false about discriminators the model declared unobtainable. What IS true of
    both is that more data will not help — so the status keeps the wall (a real,
    user-declared boundary the close must record) while the affordances drop the
    data ask, which is the channel the contradiction actually reached."""

    def _case(self):
        return _restatement_held_case(declared_wall=True)

    def test_both_premises_hold(self):
        case = self._case()
        assert is_progress_stalled(case) is True
        assert restatement_hold_governs(case) is not None
        assert assess_verification_status(case) == (
            VerificationStatus.INSUFFICIENT_EVIDENCE
        )

    def test_the_data_ask_is_gone(self):
        """The defect: the user was told to share data the model had just told
        the engine would not validate the root — and which the user had already
        declared unobtainable."""
        gate, moves = engine_owned_affordances(self._case())
        blob = " ".join(m["label"] + " " + m["body"] for m in moves).lower()
        assert "share data that would distinguish" not in blob
        assert "new discriminating data" not in blob
        assert "mechanism" in blob

    def test_it_keeps_a_pair_and_its_own_gate_label(self):
        """Two moves like every peer, and its own telemetry label: a turn that
        silently swaps its advice is a turn nobody can measure."""
        gate, moves = engine_owned_affordances(self._case())
        assert gate == "insufficient_evidence_restatement_held"
        assert len(moves) == 2
        assert moves[1]["label"] == "Suggest a diagnostic angle not yet tried"

    def test_a_plain_wall_keeps_the_data_ask(self):
        """The narrowing is to the composite. A walled case with no governing
        hold is the canonical insufficient-evidence archetype and is untouched."""
        case = _restatement_held_case(declared_wall=True, duplicate_attached=True)
        assert restatement_hold_governs(case) is None
        gate, moves = engine_owned_affordances(case)
        assert gate == "insufficient_evidence"
        assert moves[0]["label"] == "Share data that would distinguish the causes"

    def test_the_gate_label_reports_the_wall_status(self):
        """The label distinguishes the affordances, not the disposition: the
        turn metadata must agree with the persisted status."""
        assert _GATE_VERIFICATION_STATUS["insufficient_evidence_restatement_held"] == (
            VerificationStatus.INSUFFICIENT_EVIDENCE
        )

    @pytest.mark.asyncio
    async def test_the_close_records_the_wall_as_well_as_the_hold(self):
        """The composite closes on the hold — the more specific and more
        actionable finding — so the wall would be dropped from the durable
        record unless the Cause Boundary block names it."""
        from faultmaven.modules.report.domain.services.report_generation_service import (
            ReportGenerationService,
        )

        case = self._case()
        assert derive_closure_reason(case) == "closed_restatement_held"
        case = case.model_copy(
            update={
                "state": CaseState.CLOSED,
                "closed_at": datetime.now(timezone.utc),
                "closure_reason": "closed_restatement_held",
            }
        )
        svc = object.__new__(ReportGenerationService)
        summary = await svc._generate_closure_summary(case, {})
        assert "Cause Boundary" in summary
        assert "not more data" in summary
        assert "declared unobtainable" in summary.lower()
        assert "the capture that would separate the candidates" in summary

    @pytest.mark.asyncio
    async def test_a_plain_hold_close_claims_no_data_wall(self):
        """And the wall sentence must not appear when there is no wall — that
        would re-import the evidence framing through the back door."""
        from faultmaven.modules.report.domain.services.report_generation_service import (
            ReportGenerationService,
        )

        case = _restatement_held_case()
        case = case.model_copy(
            update={
                "state": CaseState.CLOSED,
                "closed_at": datetime.now(timezone.utc),
                "closure_reason": "closed_restatement_held",
            }
        )
        svc = object.__new__(ReportGenerationService)
        summary = await svc._generate_closure_summary(case, {})
        assert "unobtainable" not in summary.lower()


# ---------------------------------------------------------------------------
# The durable artifact — the contradiction must not survive the close
# ---------------------------------------------------------------------------


class TestRestatementHeldClosure:
    """``derive_closure_reason`` sent every INVESTIGATING close to
    ``closed_insufficient_evidence``, so a restatement-held case closed with a
    report headed "Data Boundary — Why This Remains Unresolved" over prose about
    stalling "before a single cause could be grounded from the data gathered so
    far" — the same false evidence framing, in the one artifact the user keeps,
    and now beside a terminal blob reading ``verification_status`` =
    ``restatement_held``. Removing the contradiction from the turn channel and
    leaving it in the permanent one is not a fix."""

    def test_closure_reason_is_its_own(self):
        case = _restatement_held_case()
        assert derive_closure_reason(case) == "closed_restatement_held"

    def test_a_genuine_evidence_stall_still_closes_insufficient_evidence(self):
        case = _restatement_held_case(duplicate_attached=True)
        assert derive_closure_reason(case) == "closed_insufficient_evidence"

    def test_another_live_root_closes_insufficient_evidence(self):
        """The closure reason reads the SAME sole-block summary the join reads,
        so the record and the disposition cannot describe a case differently."""
        case = _restatement_held_case(extra_unsettled_root=True)
        assert derive_closure_reason(case) == "closed_insufficient_evidence"

    def test_it_does_not_outrank_the_more_informative_reasons(self):
        """Ranked immediately above the generic bucket and nowhere higher: a
        verified mitigation says more about how the case ended than the hold
        does, and must keep winning."""
        from faultmaven.modules.case.contracts import MitigationRecord

        case = _restatement_held_case()
        case.progress.mitigation = MitigationRecord(
            description="restarted the pods to stop the bleeding",
            applied_at_turn=6,
            accepted=True,
            accepted_at_turn=6,
            verified=True,
            verified_at_turn=7,
        )
        assert derive_closure_reason(case) == "mitigation_sufficient"

    def test_it_is_not_gated_on_the_stall(self):
        """Keyed on the structural hold, not on the ``RESTATEMENT_HELD`` status
        cell. A user may close before the stall thresholds are met; keying on the
        cell would drop that case into a bucket that misdescribes it — the exact
        mistake ``derive_closure_reason``'s docstring already records for the old
        ``closed_insufficient_evidence`` gating."""
        case = _restatement_held_case(current_turn=3, turns_without_progress=0)
        assert assess_verification_status(case) == VerificationStatus.OPEN
        assert derive_closure_reason(case) == "closed_restatement_held"

    def test_every_gate_that_reports_a_status_is_in_the_map(self):
        """The gate labels are produced in ``engine_owned_affordances`` and
        consumed at the return boundary. A gate added in one place and forgotten
        in the other used to fall through in SILENCE — the turn carried no
        status at all, which is how ``treatment_blocked`` and
        ``restatement_held`` both shipped unmapped. Pin the correspondence."""
        import inspect

        from faultmaven.core.investigation import milestone_engine

        src = inspect.getsource(milestone_engine.engine_owned_affordances)
        emitted = set(re.findall(r'return \(\s*"([a-z0-9_]+)"', src))
        emitted |= set(re.findall(r'gate = \(\s*"([a-z0-9_]+)"', src))
        emitted |= set(re.findall(r'\s+else "([a-z0-9_]+)"', src))
        # The state-machine handshakes report no status by design.
        emitted -= {"disposition", "gate1"}
        assert emitted, "the scrape found no gate labels — it has rotted"
        missing = emitted - set(_GATE_VERIFICATION_STATUS)
        assert not missing, f"gates with no status mapping: {sorted(missing)}"

    def test_the_confirm_turn_names_the_distinction(self):
        """Every other closure reason gets bespoke prose on the confirm turn;
        without a branch this one fell through to a bare "Case closed." and the
        distinction was dropped on the closure turn itself."""
        case = _restatement_held_case()
        case = case.model_copy(
            update={
                "state": CaseState.CLOSED,
                "closed_at": datetime.now(timezone.utc),
                "closure_reason": "closed_restatement_held",
            }
        )
        line = _terminal_confirmation_response(case)
        assert line != "Case closed."
        assert "never stated distinctly" in line

    def test_the_reason_is_in_the_valid_vocabulary(self):
        from faultmaven.modules.case.domain.models import VALID_CLOSURE_REASONS

        assert "closed_restatement_held" in VALID_CLOSURE_REASONS

    @pytest.mark.asyncio
    async def test_the_closure_summary_does_not_claim_an_evidence_boundary(self):
        """Render the real summary and read it: the evidence framing must be
        gone and a cause-framing capture must be there in its place — silence
        would be the failure this whole fix exists to avoid, one artifact over."""
        from faultmaven.modules.report.domain.services.report_generation_service import (
            ReportGenerationService,
        )

        case = _restatement_held_case()
        reason = derive_closure_reason(case)
        # state / closed_at / closure_reason are validated as a SET (each field
        # is rejected without the others), so close the case in one update the
        # way the executor's write does.
        case = case.model_copy(
            update={
                "state": CaseState.CLOSED,
                "closed_at": datetime.now(timezone.utc),
                "closure_reason": reason,
            }
        )
        svc = object.__new__(ReportGenerationService)
        summary = await svc._generate_closure_summary(case, {})

        assert "Data Boundary" not in summary
        assert "before a single cause could be grounded" not in summary
        assert "insufficient evidence to establish" not in summary
        assert "Cause Boundary" in summary
        assert "never added anything beyond the problem" in summary
        assert "not more data" in summary
        # The Recommendation must not send a follow-up back for more data.
        assert "naming the mechanism" in summary
