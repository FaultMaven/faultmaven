"""Stagnation decay must not erode belief that causal evidence earned.

Observed (#1678): a hypothesis with a confident SUPPORTS link to causal evidence
sat at 0.95 while the user applied its fix and waited for the result. No turn
touched it — there was nothing left to test — so the age-based sweep aged it,
and the decay compounded: 0.95 -> 0.81 -> 0.58 -> 0.36 in three turns, below
the cause-identification bar.

Two rules close that, both asserted here on engine state (no LLM output):

- The age sweep (``advance_stagnation_if_ignored``) does not age a hypothesis
  whose causal support stands: confident support from causal evidence, with no
  confident refutation and a head the graph has not refuted. Support from
  symptom evidence — which every sibling can claim — does not exempt one, and a
  contradicted hypothesis still has open work, so it ages when ignored.
- Decay (``apply_likelihood_decay``) is one step (x0.85) per stagnant turn — a
  turn that touched the hypothesis without progress — and none on a turn that
  did not touch it. It never raises belief.

#713's protection against an ignored prior lingering is intact, and completed:
beside a leader that is no longer aged, anti-anchoring (which acts on fixation)
never fires for a lone stalled prior, so the age-out retires it once it has
gone the full stagnation horizon below the retirement threshold.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation.causal_graph import (
    project_hypothesis_states_from_roots,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NeedPriority,
    NeedPurpose,
    NeedState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

CAUSAL = EvidenceCategory.CAUSAL_EVIDENCE
SYMPTOM = EvidenceCategory.SYMPTOM_EVIDENCE
CAUSAL_ABSENCE = EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
SUPPORTS, REFUTES = EvidenceStance.SUPPORTS, EvidenceStance.REFUTES


def _engine() -> MilestoneEngine:
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


def _ev(category: EvidenceCategory = CAUSAL) -> Evidence:
    return Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        summary="migration log: server requires SSL, client not configured",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=5,
        collected_at=datetime.now(timezone.utc),
    )


def _case(hypotheses: list[Hypothesis], evidence: list[Evidence] = ()) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="sync stuck",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="sync stuck", severity=CaseSeverity.HIGH
        ),
    )
    case.hypotheses = {h.hypothesis_id: h for h in hypotheses}
    case.evidence = list(evidence)
    return case


def _hyp(
    *,
    likelihood: float,
    progress_turn: int,
    links: tuple[tuple[Evidence, EvidenceStance, float], ...] = (),
    root_node_id: str | None = None,
    path: list[str] | None = None,
) -> Hypothesis:
    """A hypothesis last touched, with progress, at ``progress_turn``."""
    hid = f"hyp_{uuid4().hex[:12]}"
    return Hypothesis(
        hypothesis_id=hid,
        statement="migration job lacks the SSL connection parameters",
        category=HypothesisCategory.CONFIG,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        likelihood=likelihood,
        initial_likelihood=0.5,
        generated_at_turn=progress_turn,
        last_updated_turn=progress_turn,
        last_progress_at_turn=progress_turn,
        iterations_without_progress=0,
        evidence_links=[
            HypothesisEvidenceLink(
                hypothesis_id=hid,
                evidence_id=ev.evidence_id,
                stance=stance,
                reasoning="bears on it",
                stance_confidence=confidence,
            )
            for ev, stance, confidence in links
        ],
        root_node_id=root_node_id,
        path=path or [],
    )


def _supported_leader(progress_turn: int = 5) -> tuple[Hypothesis, list[Evidence]]:
    """The observed shape: SUPPORTS at 1.0 to causal evidence and at 0.9 to
    symptom evidence, at 0.95."""
    causal, symptom = _ev(CAUSAL), _ev(SYMPTOM)
    leader = _hyp(
        likelihood=0.95,
        progress_turn=progress_turn,
        links=(
            (causal, EvidenceStance.SUPPORTS, 1.0),
            (symptom, EvidenceStance.SUPPORTS, 0.9),
        ),
    )
    return leader, [causal, symptom]


def _housekeep(eng: MilestoneEngine, case: Case, turn: int) -> None:
    case.current_turn = turn
    eng._perform_hypothesis_housekeeping(case, {})


# ---------------------------------------------------------------------------
# The age sweep does not age a causally supported hypothesis
# ---------------------------------------------------------------------------


def test_supported_leader_keeps_its_belief_while_the_user_verifies_its_fix():
    """0.95 at turn 5, then turns that only wait on the user. Belief and the
    stagnation counter hold."""
    eng = _engine()
    leader, evidence = _supported_leader()
    case = _case([leader], evidence)

    for turn in range(6, 21):
        _housekeep(eng, case, turn)
        assert leader.likelihood == 0.95, f"eroded on turn {turn}"
        assert leader.iterations_without_progress == 0
        assert leader.state == HypothesisState.ACTIVE


@pytest.mark.parametrize("confidence", [0.6, 1.0])
def test_a_confident_link_to_causal_evidence_exempts_from_aging(confidence):
    eng = _engine()
    ev = _ev(CAUSAL)
    h = _hyp(
        likelihood=0.65,
        progress_turn=0,
        links=((ev, EvidenceStance.SUPPORTS, confidence),),
    )
    case = _case([h], [ev])

    for turn in range(1, 10):
        _housekeep(eng, case, turn)

    assert h.iterations_without_progress == 0
    assert h.likelihood == 0.65


@pytest.mark.parametrize(
    "category, stance, confidence, on_case",
    [
        (CAUSAL, EvidenceStance.SUPPORTS, 0.59, True),  # hedged under the bar
        (SYMPTOM, EvidenceStance.SUPPORTS, 1.0, True),  # every sibling can claim it
        (CAUSAL, EvidenceStance.SUPPORTS, 1.0, False),  # evidence row not on the case
        (CAUSAL, EvidenceStance.REFUTES, 1.0, True),
        (CAUSAL, EvidenceStance.NEUTRAL, 1.0, True),
        (None, None, None, True),  # no links
    ],
    ids=[
        "hedged-causal",
        "symptom-supports",
        "unknown-evidence",
        "refutes",
        "neutral",
        "no-links",
    ],
)
def test_a_hypothesis_without_causal_support_ages(
    category, stance, confidence, on_case
):
    hm = HypothesisManager()
    ev = _ev(category) if category else None
    links = ((ev, stance, confidence),) if ev else ()
    h = _hyp(likelihood=0.4, progress_turn=0, links=links)
    case = _case([h], [ev] if ev and on_case else [])

    hm.advance_stagnation_if_ignored(h, 2, case)
    assert h.iterations_without_progress == 0  # inside the grace window
    hm.advance_stagnation_if_ignored(h, 3, case)
    assert h.iterations_without_progress == 1


def test_a_hypothesis_grounded_only_on_its_chain_root_is_not_aged():
    """Chain links never mirror into ``hypothesis.evidence_links``: support on
    the chain ROOT exempts the hypothesis too."""
    ev = _ev(CAUSAL)
    root = CausalNode(
        node_id=f"cn_{uuid4().hex[:12]}",
        statement="connection string lacks sslmode",
        node_type=NodeType.ROOT,
        node_state=NodeState.INCONCLUSIVE,  # held by the independent-support count
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=ev.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the root",
                stance_confidence=1.0,
                linked_at_turn=5,
            )
        ],
        generated_at_turn=5,
    )
    h = _hyp(likelihood=0.65, progress_turn=5, root_node_id=root.node_id)
    case = _case([h], [ev])
    case.causal_nodes = {root.node_id: root}
    eng = _engine()

    for turn in range(6, 15):
        _housekeep(eng, case, turn)

    assert h.iterations_without_progress == 0
    assert h.likelihood == 0.65


def test_an_unsupported_sibling_still_ages_beside_a_supported_leader():
    """#713 stays intact in the same case: the ignored prior ages and decays
    while the supported leader holds."""
    eng = _engine()
    leader, evidence = _supported_leader()
    sibling = _hyp(likelihood=0.35, progress_turn=5)
    case = _case([leader, sibling], evidence)

    for turn in range(6, 11):
        _housekeep(eng, case, turn)

    assert leader.likelihood == 0.95
    assert sibling.iterations_without_progress == 3  # aged on turns 8, 9, 10
    assert sibling.likelihood == pytest.approx(0.35 * 0.85**3)


# ---------------------------------------------------------------------------
# Decay is one step per stagnant turn
# ---------------------------------------------------------------------------


def test_one_stagnant_turn_costs_one_decay_step():
    """A restatement that moves belief < 0.05 is a stagnant turn for the
    hypothesis: one step. The untouched turns after it are not, even though the
    counter stays positive."""
    eng = _engine()
    leader, evidence = _supported_leader()
    case = _case([leader], evidence)

    case.current_turn = 8
    eng.hypothesis_manager.update_hypothesis_likelihood(
        leader, 0.95, 8, "restated unchanged", case
    )
    assert leader.iterations_without_progress == 1
    _housekeep(eng, case, 8)
    assert leader.likelihood == pytest.approx(0.8075)

    for turn in range(9, 16):
        _housekeep(eng, case, turn)
        assert leader.likelihood == pytest.approx(0.8075), f"decayed on turn {turn}"


def test_an_ignored_prior_decays_by_one_factor_per_turn():
    """Each aged turn multiplies belief by 0.85 — not 0.85 raised to the
    counter, which compounded to 0.85^(n(n+1)/2)."""
    eng = _engine()
    prior = _hyp(likelihood=0.4, progress_turn=0)
    case = _case([prior])

    observed = []
    for turn in range(1, 5):
        _housekeep(eng, case, turn)
        observed.append(prior.likelihood)

    assert observed == pytest.approx([0.4, 0.4, 0.34, 0.289])


def test_a_turn_that_restarts_the_stagnation_clock_does_not_decay():
    """Activation (CAPTURED -> ACTIVE) and reversion (VALIDATED -> ACTIVE) stamp
    both ``last_updated_turn`` and ``last_progress_at_turn`` to the current
    turn without clearing the counter. That turn is a fresh start, not a
    stagnant turn."""
    eng = _engine()
    h = _hyp(likelihood=0.7, progress_turn=9)
    h.iterations_without_progress = 2
    case = _case([h])

    _housekeep(eng, case, 9)

    assert h.likelihood == 0.7


def _head(
    *links: tuple[Evidence, EvidenceStance, float],
    state: NodeState = NodeState.INCONCLUSIVE,
) -> CausalNode:
    return CausalNode(
        node_id=f"cn_{uuid4().hex[:12]}",
        statement="connection string lacks sslmode",
        node_type=NodeType.ROOT,
        node_state=state,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=ev.evidence_id,
                stance=stance,
                reasoning="bears on the root",
                stance_confidence=confidence,
                linked_at_turn=5,
            )
            for ev, stance, confidence in links
        ],
        generated_at_turn=5,
        refutation_reason=(
            "the symptom persisted after the fix"
            if state == NodeState.REFUTED
            else None
        ),
    )


def _ages_at_threshold(h: Hypothesis, case: Case) -> bool:
    """Whether the sweep advances ``h`` once it is past the grace window."""
    HypothesisManager().advance_stagnation_if_ignored(h, 3, case)
    return h.iterations_without_progress == 1


# ---------------------------------------------------------------------------
# Contradiction ends the exemption
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", [CAUSAL_ABSENCE, SYMPTOM, CAUSAL])
def test_a_confident_refutation_on_the_hypothesis_ends_the_exemption(category):
    """The fix was applied and the symptom persists: the refutation is open
    work, so a contradicted hypothesis nobody is working on ages."""
    support, refute = _ev(CAUSAL), _ev(category)
    h = _hyp(
        likelihood=0.45,
        progress_turn=0,
        links=((support, SUPPORTS, 1.0), (refute, REFUTES, 0.9)),
    )
    assert _ages_at_threshold(h, _case([h], [support, refute]))


def test_a_confident_refutation_on_the_chain_head_ends_the_exemption():
    support, refute = _ev(CAUSAL), _ev(CAUSAL_ABSENCE)
    head = _head((support, SUPPORTS, 1.0), (refute, REFUTES, 1.0))
    h = _hyp(likelihood=0.5, progress_turn=0, root_node_id=head.node_id)
    case = _case([h], [support, refute])
    case.causal_nodes = {head.node_id: head}
    assert _ages_at_threshold(h, case)


def test_a_head_the_graph_refuted_ends_the_exemption():
    """A REFUTED head keeps its old SUPPORTS link; the derived state wins."""
    support = _ev(CAUSAL)
    head = _head((support, SUPPORTS, 1.0), state=NodeState.REFUTED)
    h = _hyp(likelihood=0.5, progress_turn=0, root_node_id=head.node_id)
    case = _case([h], [support])
    case.causal_nodes = {head.node_id: head}
    assert _ages_at_threshold(h, case)


def test_a_hedged_refutation_leaves_the_exemption_in_place():
    support, refute = _ev(CAUSAL), _ev(CAUSAL_ABSENCE)
    h = _hyp(
        likelihood=0.65,
        progress_turn=0,
        links=((support, SUPPORTS, 1.0), (refute, REFUTES, 0.59)),
    )
    assert not _ages_at_threshold(h, _case([h], [support, refute]))


def test_a_chain_head_named_only_by_the_path_is_recognised():
    """A chain mid-expansion carries ``path`` before ``root_node_id``; support
    on that head exempts the hypothesis like support on an assigned root."""
    support = _ev(CAUSAL)
    head = _head((support, SUPPORTS, 1.0))
    h = _hyp(likelihood=0.65, progress_turn=0, path=[head.node_id])
    case = _case([h], [support])
    case.causal_nodes = {head.node_id: head}
    assert not _ages_at_threshold(h, case)


# ---------------------------------------------------------------------------
# Decay never raises belief
# ---------------------------------------------------------------------------


def test_decay_never_lifts_a_hypothesis_below_the_floor():
    eng = _engine()
    low = _hyp(likelihood=0.05, progress_turn=0)
    case = _case([low])

    for turn in range(1, 8):
        _housekeep(eng, case, turn)
        assert low.likelihood <= 0.05, f"raised on turn {turn}"


# ---------------------------------------------------------------------------
# The age-out: an ignored prior soft-retires even beside a healthy leader
# ---------------------------------------------------------------------------


def test_an_ignored_prior_ages_out_beside_a_supported_leader():
    """Anti-anchoring never fires here (the top hypothesis is not stalled and
    only one is), so without the age-out the prior sat ACTIVE at the floor."""
    eng = _engine()
    leader, evidence = _supported_leader()
    sibling = _hyp(likelihood=0.35, progress_turn=5)
    case = _case([leader, sibling], evidence)

    for turn in range(6, 10):
        _housekeep(eng, case, turn)
    # Aged on turns 8 and 9: two stagnant iterations, still inside the horizon.
    assert sibling.state == HypothesisState.ACTIVE

    _housekeep(eng, case, 10)  # third stagnant iteration, 0.35 x 0.85^3 < 0.30
    assert sibling.state == HypothesisState.RETIRED
    assert sibling.retirement_reason.startswith("Aged out")
    assert sibling.refutation_reason is None
    assert leader.state == HypothesisState.ACTIVE
    assert leader.likelihood == 0.95


def test_the_age_out_stands_down_while_requested_evidence_is_outstanding():
    eng = _engine()
    leader, evidence = _supported_leader()
    sibling = _hyp(likelihood=0.35, progress_turn=5)
    case = _case([leader, sibling], evidence)
    case.evidence_needs = [
        EvidenceNeed(
            case_id=case.case_id,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="please attach the new migration pod's log",
            rationale="shows whether the fix took",
            priority=NeedPriority.MEDIUM,
            state=NeedState.PENDING,
            created_at_turn=10,
        )
    ]

    for turn in range(6, 12):
        _housekeep(eng, case, turn)
    assert sibling.state == HypothesisState.ACTIVE  # asked on turn 10: waiting

    _housekeep(eng, case, 12)  # the ask is no longer recent
    assert sibling.state == HypothesisState.RETIRED


# ---------------------------------------------------------------------------
# A restarted clock restarts the counter
# ---------------------------------------------------------------------------


def test_activation_restarts_the_stagnation_counter():
    h = _hyp(likelihood=0.4, progress_turn=2)
    h.state = HypothesisState.CAPTURED
    h.iterations_without_progress = 3
    case = _case([h])
    case.current_turn = 9

    HypothesisManager.activate_queued_hypotheses(case)

    assert h.state == HypothesisState.ACTIVE
    assert h.iterations_without_progress == 0


def test_a_reverted_hypothesis_is_not_retired_as_stalled_on_the_turn_it_reverts():
    """VALIDATED -> ACTIVE gives the fresh grace of a new candidate: a counter
    built up before validating no longer lets anti-anchoring retire it on the
    same turn."""
    eng = _engine()
    reverted = []
    for _ in range(2):
        head = _head(state=NodeState.CANDIDATE)  # root no longer validated
        h = _hyp(likelihood=0.5, progress_turn=2, root_node_id=head.node_id)
        h.state = HypothesisState.VALIDATED
        h.iterations_without_progress = 3
        reverted.append((h, head))
    case = _case([h for h, _ in reverted])
    case.causal_nodes = {head.node_id: head for _, head in reverted}
    case.current_turn = 9

    project_hypothesis_states_from_roots(case)
    _housekeep(eng, case, 9)

    for h, _ in reverted:
        assert h.state == HypothesisState.ACTIVE
        assert h.iterations_without_progress == 0
