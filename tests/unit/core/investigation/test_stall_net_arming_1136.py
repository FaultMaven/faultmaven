"""Regression set for #1136 — the stall-detection net could never arm.

``turns_without_progress`` is the sole input to every stall net (``is_stalled`` →
``is_progress_stalled`` → ``INSUFFICIENT_EVIDENCE`` / ``TREATMENT_BLOCKED``, the
exhaustion detector, the LOW/BLOCKED momentum bands). It was written by a
predicate that measured *activity* rather than *advancement*, so the LLM's normal
behaviour while waiting on a user — re-quoting the same lines, re-proposing the
standing fix, re-asking for the same data — reset it almost every turn. On the
reference corpus it reached the thresholds on 8 of 103 cases past the turn floor.

Each test below pins one arm of that leak, plus the disposition half: the grounded
row of the §5.1 grid was unreachable in flight, so the cases this newly arms had
nowhere correct to land.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _restates_standing_evidence,
    _restates_standing_solution,
    _treatment_blocked_pending,
    engine_owned_affordances,
)
from faultmaven.core.investigation.schemas import EvidenceToAdd, SolutionToAdd
from faultmaven.core.investigation.turn_outcome import determine_turn_outcome
from faultmaven.core.investigation.verification_status import (
    VerificationStatus,
    assess_verification_status,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    NeedPriority,
    NeedPurpose,
    NeedState,
    Solution,
    SolutionType,
    TurnOutcome,
)
from faultmaven.modules.case.domain.models import TurnProgress

pytestmark = pytest.mark.unit


@pytest.fixture
def engine():
    return MilestoneEngine.__new__(MilestoneEngine)


def _case(**overrides):
    """A work-gate-passing INVESTIGATING case (reuses the calibration fixtures)."""
    from test_verification_status import _work_done

    return _work_done(**overrides)


def _prior_turn(case, agent_reply: str) -> None:
    case.turn_history.append(
        TurnProgress(
            turn_number=case.current_turn - 1,
            timestamp=datetime.now(timezone.utc),
            milestones_completed=[],
            evidence_added=[],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
            user_message_summary="still waiting on the change window",
            agent_response_summary=agent_reply,
        )
    )


def _outcome(case) -> TurnOutcome:
    """The outcome for a turn on which no artifact was produced."""
    return determine_turn_outcome(
        case,
        progress_made=False,
        milestones_completed=[],
        evidence_added=[],
        hypotheses_generated=0,
        solutions_proposed=0,
    )


def _need(
    turn: int, state: NeedState = NeedState.PENDING, fulfilled_by=None
) -> EvidenceNeed:
    return EvidenceNeed(
        fulfilling_evidence_ids=fulfilled_by or [],
        case_id="case_000000000001",
        purpose=NeedPurpose.CAUSAL_VERIFICATION,
        request_text="post-restart credential test output",
        rationale="decides the audience-mismatch theory",
        priority=NeedPriority.MEDIUM,
        state=state,
        created_at_turn=turn,
    )


# --- Arm 1: DATA_REQUESTED was inferred from the WRONG TURN's prose ----------


def test_previous_turns_question_does_not_score_this_turn_as_a_data_request():
    """The off-by-one. ``turn_history[-1]`` is the PREVIOUS turn at call time —
    the current record is appended much later in ``process_turn`` — so an ask made
    on turn N-1 scored turn N, on which nothing happened, as a data request."""
    case = _case(current_turn=9)
    _prior_turn(case, "Please provide the post-fix pod capture.")
    assert _outcome(case) is TurnOutcome.CONVERSATION


def test_bare_question_mark_does_not_score_a_data_request():
    """The keyword list contained a bare ``"?"``, so any question-shaped reply
    matched — measurement by token collision."""
    case = _case(current_turn=9)
    _prior_turn(case, "Is that consistent with what you are seeing?")
    assert _outcome(case) is TurnOutcome.CONVERSATION


def test_a_new_outstanding_need_is_a_data_request():
    """The replacement signal is structural: an ask the engine tracks is a need."""
    case = _case(current_turn=9)
    _prior_turn(case, "Anything else you can share?")
    case.evidence_needs.append(_need(turn=9))
    assert _outcome(case) is TurnOutcome.DATA_REQUESTED


def test_re_asking_a_standing_need_is_not_a_data_request():
    """The distinction that keeps this from over-correcting (#1136 trap 1): asking
    for something NEW is progress, repeating a standing ask is not. A re-ask
    matches an outstanding need rather than minting one, so it no longer resets
    the counter on a case that is genuinely parked."""
    case = _case(current_turn=9)
    case.evidence_needs.append(_need(turn=4))
    assert _outcome(case) is TurnOutcome.CONVERSATION


def test_a_need_fulfilled_the_same_turn_is_not_an_outstanding_ask():
    """The arm reads ``is_outstanding``, not merely "minted this turn". A need
    that arrived and was satisfied in one turn is not the engine still waiting on
    something — the evidence that satisfied it is what carries that turn's
    progress, via ``novel_evidence_added``."""
    case = _case(current_turn=9)
    case.evidence_needs.append(
        _need(turn=9, state=NeedState.FULFILLED, fulfilled_by=["ev_000000000001"])
    )
    assert _outcome(case) is TurnOutcome.CONVERSATION


# --- Arm 2: the standing fix, re-proposed every turn -------------------------


def _standing_solution(text: str, kind=SolutionType.CONFIG_CHANGE) -> Solution:
    return Solution(
        solution_id="sol_000000000001",
        solution_type=kind,
        title=f"Solution: {kind}",
        immediate_action=text,
        proposed_at=datetime.now(timezone.utc),
    )


def _emitted(text: str, kind=SolutionType.CONFIG_CHANGE) -> SolutionToAdd:
    return SolutionToAdd(
        description=text,
        solution_type=kind,
        estimated_impact="restores the failing path",
        risks="reversible; scoped to one deployment",
    )


def test_re_proposing_the_standing_fix_is_not_progress():
    """The dominant arm on the reference corpus: eleven consecutive turns whose
    only artifact was the same fix re-offered, every one scored as progress."""
    case = _case()
    case.solutions = [
        _standing_solution("Set the OIDC provider ClientIDList to sts.amazonaws.com")
    ]
    restated = _emitted("Set the OIDC provider ClientIDList to sts.amazonaws.com")
    assert _restates_standing_solution(restated, case) is True


def test_a_revised_fix_is_not_a_restatement():
    """The numeric-discriminator guard. A revision is new work and must keep
    resetting the counter — otherwise the fix over-corrects into declaring a
    working investigation stalled."""
    case = _case()
    case.solutions = [_standing_solution("Set JAVA_OPTS to -Xmx512m")]
    revised = _emitted("Set JAVA_OPTS to -Xmx256m")
    assert _restates_standing_solution(revised, case) is False


def test_same_words_as_a_different_solution_type_is_a_distinct_offer():
    case = _case()
    case.solutions = [
        _standing_solution("Restart the deployment", kind=SolutionType.WORKAROUND)
    ]
    as_permanent = _emitted("Restart the deployment")
    assert _restates_standing_solution(as_permanent, case) is False


# --- Arm 3: the same observation, recorded again -----------------------------


def _standing_evidence(extract, source=None) -> Evidence:
    return Evidence(
        evidence_id="ev_000000000001",
        summary="pods are crashlooping",
        primary_purpose="diagnosis",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
        extract=extract,
        source_file_id=source,
    )


def test_re_extracting_the_same_span_from_the_same_source_is_not_progress():
    case = _case()
    case.evidence = [_standing_evidence("OOMKilled  exit code 137", source="file_a")]
    again = EvidenceToAdd(
        summary="pods are crashlooping",
        extract="oomkilled   Exit Code 137",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_a",
    )
    assert _restates_standing_evidence(again, case) is True


def test_the_same_text_from_a_different_source_is_an_independent_observation():
    """Corroboration, not duplication — the signal the grading layer counts."""
    case = _case()
    case.evidence = [_standing_evidence("OOMKilled  exit code 137", source="file_a")]
    elsewhere = EvidenceToAdd(
        summary="pods are crashlooping",
        extract="OOMKilled  exit code 137",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_b",
    )
    assert _restates_standing_evidence(elsewhere, case) is False


def test_an_unquoted_row_is_never_deduped_away():
    """Fail open: absence of a quote is not evidence of sameness."""
    case = _case()
    case.evidence = [_standing_evidence(None, source="file_a")]
    unquoted = EvidenceToAdd(
        summary="something happened",
        extract=None,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
    )
    assert _restates_standing_evidence(unquoted, case) is False


# --- The counter itself ------------------------------------------------------


def test_a_turn_of_pure_restatement_is_not_progress(engine):
    """All three restatement arms at once — the shape of the stuck turns in the
    reference runs. Every id is still minted and recorded; only the progress
    reading narrows."""
    assert (
        engine._check_if_progress_made(
            {
                "evidence_added": ["ev_000000000001"],
                "solutions_proposed": ["sol_000000000001"],
                "files_uploaded": ["file_000000000001"],
                "outcome": TurnOutcome.DATA_PROVIDED,
            }
        )
        is False
    )


def test_any_genuinely_new_artifact_is_still_progress(engine):
    for novel_key in (
        "novel_evidence_added",
        "novel_solutions_proposed",
        "novel_files_uploaded",
    ):
        assert (
            engine._check_if_progress_made(
                {novel_key: ["x"], "outcome": TurnOutcome.CONVERSATION}
            )
            is True
        ), novel_key


def test_barren_turns_now_reach_the_stall_threshold(engine):
    """The end-to-end point of the fix: consecutive restatement turns accumulate
    instead of resetting, so ``is_stalled`` can finally become true."""
    from faultmaven.core.investigation.exhaustion_thresholds import (
        EXHAUSTION_STALL_THRESHOLD,
    )
    from faultmaven.core.investigation.verification_status import is_stalled

    case = _case(current_turn=8, turns_without_progress=0)
    restatement = {
        "evidence_added": ["ev_000000000001"],
        "solutions_proposed": ["sol_000000000001"],
        "outcome": TurnOutcome.DATA_PROVIDED,
    }
    for _ in range(EXHAUSTION_STALL_THRESHOLD):
        case.current_turn += 1
        if engine._check_if_progress_made(restatement):
            case.turns_without_progress = 0
        else:
            case.turns_without_progress += 1

    assert case.turns_without_progress == EXHAUSTION_STALL_THRESHOLD
    assert is_stalled(case) is True


# --- The disposition half: the grounded row drives something -----------------


def _grounded_stalled(**overrides):
    from test_verification_status import _mechanistic_case

    base = dict(current_turn=15, turns_without_progress=7)
    base.update(overrides)
    case = _mechanistic_case(**base)
    case.state = CaseState.INVESTIGATING
    return case


def test_a_grounded_stalled_case_is_served_the_treatment_blocked_handoff():
    case = _grounded_stalled()
    assert assess_verification_status(case) == VerificationStatus.TREATMENT_BLOCKED
    assert _treatment_blocked_pending(case) is True
    gate, affordances = engine_owned_affordances(case)
    assert gate == "treatment_blocked"
    assert len(affordances) == 2


def test_the_treatment_blocked_handoff_never_offers_a_disposition():
    """Offering to close here would resurrect, through the affordance channel,
    exactly the deferred-close nag #1138 removed. Disposition belongs to the
    disposition gate, which is checked first."""
    _, affordances = engine_owned_affordances(_grounded_stalled())
    assert all(a["action_type"] == "FREE_SPEECH" for a in affordances)
    rendered = " ".join(a["label"] + " " + a["body"] for a in affordances).lower()
    for word in ("close", "resolve", "give up", "abandon", "pause"):
        assert word not in rendered, word


def test_the_disposition_gate_still_wins_over_the_new_branch():
    """Ordering, not luck: a pending disposition handshake owns the turn."""
    case = _grounded_stalled()
    gate, _ = engine_owned_affordances(
        case, {"override_suggestions": [{"label": "Yes, close this case"}]}
    )
    assert gate == "disposition"


def test_a_progressing_grounded_case_gets_no_handoff():
    case = _grounded_stalled(turns_without_progress=1)
    assert assess_verification_status(case) == VerificationStatus.HEALTHY
    assert engine_owned_affordances(case) is None


def test_the_three_status_branches_are_mutually_exclusive():
    """All three read the same join, and a case has exactly one status."""
    from faultmaven.core.investigation.milestone_engine import (
        _hypothesis_vacuum_pending,
        _insufficient_evidence_handoff_pending,
    )

    grounded = _grounded_stalled()
    not_grounded = _case(current_turn=15, turns_without_progress=7)
    not_grounded.state = CaseState.INVESTIGATING
    not_grounded.progress.symptom_verified = True
    vacuum = _case(
        n_hypotheses=0, n_categories=0, current_turn=15, turns_without_progress=7
    )
    vacuum.state = CaseState.INVESTIGATING

    for case in (grounded, not_grounded, vacuum):
        fired = [
            _insufficient_evidence_handoff_pending(case),
            _hypothesis_vacuum_pending(case),
            _treatment_blocked_pending(case),
        ]
        assert sum(fired) == 1, fired


# --- The plumbing (the expensive failure mode) -------------------------------


def test_novel_keys_reach_the_progress_predicate_through_the_real_apply_path(engine):
    """Drives the actual ``_apply_investigation_updates`` mint loop rather than
    hand-built metadata.

    The narrowing only works if ``novel_evidence_added`` survives from the mint
    loop, through the dict ``_process_response_structured`` returns, to
    ``_check_if_progress_made``. If that plumbing ever breaks the key is simply
    absent — and absent reads as "no progress" on EVERY turn, which would declare
    every case stalled. That fails silently and in the dangerous direction, so it
    is pinned against the real call rather than a fixture.

    Also pins the deliberate split: the duplicate row IS still minted and recorded
    in ``evidence_added`` (positional ``new_index_N`` refs, milestone attribution
    and coverage all resolve against it) — only the progress reading narrows.
    """
    import asyncio
    from types import SimpleNamespace

    def _emit(extract: str) -> EvidenceToAdd:
        return EvidenceToAdd(
            summary="pods crashlooping",
            extract=extract,
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
        )

    def _drive(case, item):
        updates = SimpleNamespace(
            evidence_to_add=[item],
            milestones=None,
            hypotheses_to_add=None,
            hypotheses_to_update=None,
            solutions_to_add=None,
            evidence_need_updates=None,
            journal_entries=None,
            hypothesis_evidence_links=None,
            outcome=None,
        )
        metadata = {
            "milestones_completed": [],
            "evidence_added": [],
            "hypotheses_generated": [],
            "hypotheses_validated": [],
            "solutions_proposed": [],
            "progress_made": False,
            "status_transitioned": False,
        }
        asyncio.run(
            engine._apply_investigation_updates(
                case,
                updates,
                metadata,
                SimpleNamespace(internal_reasoning=None, agent_response=""),
                "any message",
            )
        )
        return metadata

    case = _case(current_turn=5)
    case.evidence = []

    first = _drive(case, _emit("OOMKilled exit 137"))
    assert first["evidence_added"], "the row must still be minted"
    assert first["novel_evidence_added"] == first["evidence_added"]
    assert engine._check_if_progress_made(first) is True

    # The SAME observation again, differing only in whitespace and case.
    case.current_turn = 6
    again = _drive(case, _emit("oomkilled   Exit 137"))
    assert again["evidence_added"], "the duplicate row is still minted and recorded"
    assert not again.get("novel_evidence_added")
    assert engine._check_if_progress_made(again) is False


# --- Arm 4: the same evidence link, re-emitted every turn --------------------


def _link(hyp, stance, turn, confidence=0.9, reasoning="because the log says so"):
    from faultmaven.core.investigation.hypothesis_manager import HypothesisManager

    manager = HypothesisManager.__new__(HypothesisManager)
    return manager.link_evidence(
        hyp,
        "ev_000000000001",
        stance,
        turn,
        reasoning=reasoning,
        stance_confidence=confidence,
    )


def _hypothesis():
    return next(iter(_case(current_turn=1).hypotheses.values()))


def test_re_emitting_the_same_link_is_not_progress(engine):
    """Storage upserts by ``evidence_id``, so re-emitting a standing link leaves
    the link set unchanged — but the counter used to increment per CALL, so a
    parked case whose model re-links the same evidence each turn never stalled.
    The novel_* hole, one arm over."""
    from faultmaven.modules.case.contracts import EvidenceStance

    hyp = _hypothesis()
    assert _link(hyp, EvidenceStance.SUPPORTS, 1) is True
    for turn in range(2, 6):
        assert _link(hyp, EvidenceStance.SUPPORTS, turn) is False, turn
    assert len(hyp.evidence_links) == 1, "upsert — one distinct link throughout"


def test_the_caller_counts_only_material_links(engine):
    """The other half of the arm: ``_apply_hypothesis_evidence_links`` must GATE
    its counter on what ``link_evidence`` reports. Pinned separately because a
    correct return value that the caller ignores restores the whole bug — and
    reads as fixed from ``link_evidence``'s side.
    """
    from types import SimpleNamespace

    from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
    from faultmaven.modules.case.contracts import EvidenceStance

    case = _case(current_turn=1)
    case.evidence = [_standing_evidence("OOMKilled exit 137", source="file_a")]
    hyp = next(iter(case.hypotheses.values()))
    engine.hypothesis_manager = HypothesisManager.__new__(HypothesisManager)

    def emit(stance):
        return SimpleNamespace(
            hypothesis_id_ref=hyp.hypothesis_id,
            evidence_id_ref="ev_000000000001",
            stance=stance,
            reasoning="because the log says so",
            stance_confidence=0.9,
        )

    first = {}
    engine._apply_hypothesis_evidence_links(
        case, [emit(EvidenceStance.SUPPORTS)], first
    )
    assert first.get("hypothesis_evidence_links_applied") == 1
    assert engine._check_if_progress_made(
        {**first, "outcome": TurnOutcome.CONVERSATION}
    )

    # Four more turns re-emitting the SAME link: none of them count.
    for turn in range(2, 6):
        case.current_turn = turn
        repeat = {}
        engine._apply_hypothesis_evidence_links(
            case, [emit(EvidenceStance.SUPPORTS)], repeat
        )
        assert not repeat.get("hypothesis_evidence_links_applied"), turn
        assert (
            engine._check_if_progress_made(
                {**repeat, "outcome": TurnOutcome.CONVERSATION}
            )
            is False
        ), turn

    # A revised stance counts again.
    case.current_turn = 6
    revised = {}
    engine._apply_hypothesis_evidence_links(
        case, [emit(EvidenceStance.REFUTES)], revised
    )
    assert revised.get("hypothesis_evidence_links_applied") == 1


def test_rewording_the_same_link_is_not_progress():
    """Restating with fresh prose is the exact LLM behaviour this change exists
    to stop counting."""
    from faultmaven.modules.case.contracts import EvidenceStance

    hyp = _hypothesis()
    _link(hyp, EvidenceStance.SUPPORTS, 1, reasoning="the log shows the OOM kill")
    assert (
        _link(hyp, EvidenceStance.SUPPORTS, 2, reasoning="per the OOM kill in the log")
        is False
    )


def test_a_revised_stance_is_progress():
    """Not "new links only": the model changing its read of what the evidence
    means is diagnostic work — that is what hypothesis testing looks like."""
    from faultmaven.modules.case.contracts import EvidenceStance

    hyp = _hypothesis()
    _link(hyp, EvidenceStance.SUPPORTS, 1)
    assert _link(hyp, EvidenceStance.REFUTES, 2) is True


def test_crossing_the_hedge_bar_is_progress_but_jitter_beside_it_is_not():
    """At an unchanged stance, crossing ``CAUSAL_STANCE_CONFIDENCE_MIN`` changes
    what the case knows: below it the link is a self-hedge that grounds nothing,
    above it it counts for chain grounding and lifts the evidence-free cap.
    Movement that stays on one side of the bar changes nothing."""
    from faultmaven.core.investigation.cause_assurance import (
        CAUSAL_STANCE_CONFIDENCE_MIN,
    )
    from faultmaven.modules.case.contracts import EvidenceStance

    above, below = (
        CAUSAL_STANCE_CONFIDENCE_MIN + 0.3,
        CAUSAL_STANCE_CONFIDENCE_MIN - 0.2,
    )
    hyp = _hypothesis()
    _link(hyp, EvidenceStance.SUPPORTS, 1, confidence=above)
    assert _link(hyp, EvidenceStance.SUPPORTS, 2, confidence=above + 0.05) is False
    assert _link(hyp, EvidenceStance.SUPPORTS, 3, confidence=below) is True
    assert _link(hyp, EvidenceStance.SUPPORTS, 4, confidence=below - 0.1) is False
    assert _link(hyp, EvidenceStance.SUPPORTS, 5, confidence=above) is True
