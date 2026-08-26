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

These pins fix the contradiction ONLY. They do **not** release the held root —
that is the open #1122 product decision — so
``test_known_limit_unattached_duplicate_frames_its_own_root`` and
``test_656_disjunction_root_stays_blocked_against_verbose_siblings``
(``test_restatement_guard_calibration.py``) are untouched and still assert the
hold, and ``ROOT_NOVELTY_MIN_FRACTION`` / ``_FRAME_OWNER_JACCARD`` are unchanged.

The fixture is the live incident's own statements: a terse ROOT, its ATTACHED
hypothesis, and the turn-11 near-duplicate whose ``root_node_ref`` adoption the
#1091 one-cause-one-chain guard refused — which is what leaves it unattached and
frames its own root (novelty 1/9 against a 0.30 bar).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import restatement_held_root_ids
from faultmaven.core.investigation.cause_assurance import _graph_hooks
from faultmaven.core.investigation.milestone_engine import (
    _insufficient_evidence_handoff_pending,
    _insufficient_evidence_handoff_suggestions,
    _restatement_held_pending,
    _restatement_held_suggestions,
    engine_owned_affordances,
)
from faultmaven.core.investigation.verification_status import (
    VerificationStatus,
    assess_verification_status,
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
)

pytestmark = pytest.mark.unit


# --- the live incident's own text (case_a3d354f08765) ------------------------

SYMPTOM = (
    "The production payment-processor deployment in the `payments` namespace is "
    "currently unavailable or unstable because its v2.1.4 pods enter "
    "CrashLoopBackOff after 2-3 minutes, causing customer payment failures."
)
ROOT_STATEMENT = (
    "JVM heap and native/non-heap memory exceed the 400Mi container cgroup limit"
)
HYPOTHESIS_STATEMENT = (
    "The v2.1.4 JVM configuration sets a 512MB maximum heap inside a 400Mi "
    "container, leaving insufficient headroom for JVM native/non-heap memory; "
    "total RSS reaches the cgroup limit, the kernel kills the process with "
    "SIGKILL/exit 137, and Kubernetes restarts it into CrashLoopBackOff."
)
ROOT_ID = "cn_597a37af74c7"


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
) -> Case:
    """The incident shape: a ROOT that clears the §7.1 grounding bar (two
    INDEPENDENT causal supports, both confident, against a bar of 2) and is held
    at INCONCLUSIVE by the restatement guard alone.

    ``duplicate_attached`` is the fixture-side mutation used by the
    discrimination pins: attaching the near-duplicate to the root makes it the
    root's OWN hypothesis, which drops it out of the frame and dissolves the
    hold — leaving an otherwise identical case that is a genuine
    ``INSUFFICIENT_EVIDENCE`` stall. Everything else is held constant, so the
    two dispositions differ by the hold and nothing else.
    """
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=current_turn,
        inquiry=InquiryData(
            proposed_problem_statement=SYMPTOM,
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement=SYMPTOM, severity=CaseSeverity.HIGH
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
        statement=SYMPTOM,
        node_type=NodeType.PROBLEM,
        generated_at_turn=1,
    )
    root = CausalNode(
        node_id=ROOT_ID,
        statement=ROOT_STATEMENT,
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

    hypotheses = [
        Hypothesis(
            hypothesis_id="hyp_e44ffbfe6fa4",
            statement=HYPOTHESIS_STATEMENT,
            category=HypothesisCategory.ENVIRONMENT,
            state=HypothesisState.ACTIVE,
            rationale="a reason",
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            generated_at_turn=5,
            root_node_id=ROOT_ID,
        )
    ]
    if include_second_hypothesis:
        hypotheses.append(
            Hypothesis(
                hypothesis_id="hyp_fd24a60ab341",
                statement=HYPOTHESIS_STATEMENT,
                category=HypothesisCategory.CONFIG,
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
    return case


# ---------------------------------------------------------------------------
# Fixture premises — without these the pins below could pass vacuously
# ---------------------------------------------------------------------------


def test_fixture_is_actually_restatement_held():
    """The hold is real, and it is the RESTATEMENT guard rather than a grounding
    bar: the root carries two INDEPENDENT qualifying causal supports (the §7.1
    bar is 2), so ``restatement_held_root_ids`` — which requires the grounding
    bar already MET — is what reports it."""
    case = _restatement_held_case()
    assert restatement_held_root_ids(case) == {ROOT_ID}


def test_fixture_crosses_the_work_gate_and_is_stalled():
    """The cell under test is the WORK-GATED stall. If the fixture failed the
    work gate it would read NOT_YET_PRODUCTIVE and every pin below would be
    testing a different cell."""
    case = _restatement_held_case()
    assert work_gate_passed(case) is True
    assert case.current_turn >= 8 and case.turns_without_progress >= 3


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
    """The join reads the hold through the ``cause_assurance`` graph-hook seam
    (a direct import closes the causal_graph -> hypothesis_manager ->
    terminal_transitions -> verification_status cycle). The consumer reads it
    with ``.get`` like every other hook consumer, so a missing registration
    would degrade SILENTLY back to the pre-#1195 contradiction — pin it."""
    assert _graph_hooks().get("restatement_held") is restatement_held_root_ids


# ---------------------------------------------------------------------------
# The join — the disposition itself
# ---------------------------------------------------------------------------


def test_restatement_held_root_is_not_insufficient_evidence():
    """THE pin. A stalled, work-gated, ungrounded case whose only block is the
    §7.1 restatement guard reads its own cell, never INSUFFICIENT_EVIDENCE."""
    case = _restatement_held_case()
    assert assess_verification_status(case) == VerificationStatus.RESTATEMENT_HELD


def test_insufficient_evidence_handoff_is_not_pending_while_a_root_is_held():
    """The issue's required pin, in its own words: the handoff is False while
    ``restatement_held_root_ids(case)`` is non-empty."""
    case = _restatement_held_case()
    assert restatement_held_root_ids(case)
    assert _insufficient_evidence_handoff_pending(case) is False


def test_hold_does_not_pre_empt_a_progressing_case():
    """Scoped to the STALLED cell. A case still making progress reads OPEN,
    which asserts nothing false about its evidence and needs no corrective —
    the contradiction is specific to the stalled cell, so the carve-out is
    too."""
    case = _restatement_held_case(current_turn=3, turns_without_progress=0)
    assert restatement_held_root_ids(case) == {ROOT_ID}
    assert assess_verification_status(case) == VerificationStatus.OPEN
    assert engine_owned_affordances(case) is None


def test_hold_does_not_pre_empt_the_work_gate():
    """Scoped BELOW the work gate too. ``NOT_YET_PRODUCTIVE`` is a
    provider-health reading ("too little work to judge"), never a per-case
    evidence verdict, so it does not contradict the hold and is left alone."""
    case = _restatement_held_case(include_second_hypothesis=False)
    assert work_gate_passed(case) is False
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
        concern restating the cause distinctly. They are not the
        insufficient-evidence pair, and they name both recoveries the
        model-facing note names (state the mechanism / settle the overlapping
        alternative) — the engine cannot tell the two held shapes apart, so
        offering only one would be false for the other half."""
        moves = _restatement_held_suggestions()
        assert moves != _insufficient_evidence_handoff_suggestions()
        blob = " ".join(m["label"] + " " + m["body"] for m in moves).lower()
        assert "mechanism" in blob
        assert "same cause" in blob
        # The insufficient-evidence ask must not survive into this pair.
        assert "distinguish the causes" not in blob
        assert "diagnostic angle" not in blob

    def test_the_moves_never_steer_toward_close(self):
        """D4 no-soft-collapse, as for all three peers: a hold the engine can
        describe is not a reason to nudge the user toward abandoning the case.
        Non-clickable FREE_SPEECH — the user supplies the content."""
        moves = _restatement_held_suggestions()
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
