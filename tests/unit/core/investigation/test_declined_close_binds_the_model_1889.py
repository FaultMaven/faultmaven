"""#1889: a declined close binds whoever proposes it, until its premise moves.

The user's "no" to closing on a false-alarm finding is a fact about the
finding, so it is recorded ON the finding (``close_declined_at_turn``), whoever
opened the close; a declined engine deferred close is recorded against the
signature that justified it. While either stands, a transition the model
proposes that re-asks it is refused, the model is told the close may be
proposed only when the user directs it, and the turn's own follow-ups gain one
``status_transition`` card so the user who did ask still has the close one step
away on every client. The user's own close is never blocked.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    declined_close_card,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    terminal_offer_key,
)
from faultmaven.core.investigation.milestone_engine.transition_turns import (
    FALSE_ALARM_HOLD_REPLY,
)
from faultmaven.core.investigation.problem_status import (
    FALSE_ALARM_CLOSURE_REASON,
    edit_statement,
    invalidate_problem,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    _problem_hold_emphasis,
    get_prompt_for_case,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
    ProblemVerificationUpdate,
    ProposedTransition,
    SuggestedFollowUp,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    EvidenceCategory,
    InvestigationStage,
    MitigationRecord,
    ProblemStatus,
)
from faultmaven.modules.case.domain.models.solution import Solution, SolutionType
from tests.unit.core.investigation.test_every_card_names_its_offer_1812 import (
    _investigating,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _DSU,
    _case,
    _engine,
    _evidence,
    _finding_turn,
    _revision_update,
    _row,
    _with,
    _withdraw_update,
)

pytestmark = pytest.mark.unit

_CLOSE_INTENT = {"type": "status_transition", "to_state": "closed"}
_SIGNED = "justifying_signature"

#: What the model suggests on the refused turn. The close card is appended to
#: these; a refusal that replaced them would be the re-ask in disguise.
_MODEL_FOLLOW_UPS = [
    SuggestedFollowUp(label="Share more context", action_type="FREE_SPEECH"),
    SuggestedFollowUp(label="Ask what this means", action_type="FREE_SPEECH"),
]


def _respond(
    engine: MilestoneEngine, dsu: _DSU, *, follow_ups: list | None = None
) -> None:
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.",
            state_updates=dsu,
            suggested_follow_ups=follow_ups,
        )
    )


def _proposes(to_state: str) -> _DSU:
    return _DSU(proposed_transition=ProposedTransition(to_state=to_state))


async def _turn(engine, case, message: str, dsu: _DSU | None = None, **kw) -> dict:
    case.current_turn += 1
    _respond(engine, dsu or _DSU(), follow_ups=kw.pop("follow_ups", None))
    return await engine.process_turn(case=case, user_message=message, **kw)


def _feedback(result) -> str:
    return result["case_updated"].turn_history[-1].system_feedback or ""


def _intents(result) -> list:
    return [f.get("intent") for f in result["suggested_follow_ups"]]


async def _declined_finding() -> tuple:
    """The finding is made on turn 4 and the engine's close is declined with a
    bare "no" on turn 5."""
    engine, case, _ = await _finding_turn(model_proposes=None)
    result = await _turn(engine, case, "no")
    assert case.pending_transition is None
    assert case.problem_verification.invalidation.close_declined_at_turn == 5
    return engine, case, result


# ---------------------------------------------------------------------------
# The false-alarm side
# ---------------------------------------------------------------------------


class TestADeclinedFalseAlarmCloseBindsTheModel:
    async def test_the_models_close_is_refused_and_the_card_appended(self):
        engine, case, _ = await _declined_finding()
        result = await _turn(
            engine,
            case,
            "ok, close it",
            _proposes("closed"),
            follow_ups=_MODEL_FOLLOW_UPS,
        )
        assert case.pending_transition is None
        assert case.state == CaseState.INVESTIGATING
        feedback = _feedback(result)
        assert "TRANSITION NOT PROPOSED" in feedback
        assert "declined closing on this finding at turn 5" in feedback
        assert "only if the user directs it" in feedback
        # Appended, never an override: the model's follow-ups stand, and the
        # close card comes after them, naming the state rather than an offer.
        labels = [f["label"] for f in result["suggested_follow_ups"]]
        assert labels == [
            "Share more context",
            "Ask what this means",
            "Close as false alarm",
        ]
        assert _intents(result)[-1] == _CLOSE_INTENT
        assert "proposal_id" not in _intents(result)[-1]

    async def test_the_models_resolve_is_refused_too(self):
        """Gated on the case, not the target: on an invalidated case a
        ``resolved`` pivots to the same false-alarm close."""
        engine, case, _ = await _declined_finding()
        result = await _turn(engine, case, "so is it done then?", _proposes("resolved"))
        assert case.pending_transition is None
        assert "TRANSITION NOT PROPOSED" in _feedback(result)
        assert _intents(result) == [_CLOSE_INTENT]

    async def test_a_turn_with_no_proposal_carries_no_card(self):
        """The card answers a refused re-proposal; it is not a standing nag."""
        engine, case, _ = await _declined_finding()
        result = await _turn(
            engine, case, "what else is in that log?", follow_ups=_MODEL_FOLLOW_UPS
        )
        assert _CLOSE_INTENT not in _intents(result)
        assert "TRANSITION NOT PROPOSED" not in _feedback(result)

    async def test_a_model_opened_close_declined_is_recorded_on_the_finding(self):
        """Driven the real way: the user's question withdraws the engine's
        offer unrecorded, the model then opens the close itself, and the
        user's "no" to THAT is recorded on the finding (it used to record
        nothing, and the model re-proposed on the next turn)."""
        engine, case, _ = await _finding_turn(model_proposes=None)
        await _turn(engine, case, "what does that mean for the alert?")
        assert case.pending_transition is None
        assert case.problem_verification.invalidation.close_declined_at_turn is None

        await _turn(engine, case, "makes sense", _proposes("closed"))
        pending = case.pending_transition
        assert pending["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
        assert _SIGNED not in pending

        await _turn(engine, case, "no")
        assert case.pending_transition is None
        assert case.problem_verification.invalidation.close_declined_at_turn == 7
        assert case.progress.deferred_disposition_declined_signatures == []

        result = await _turn(engine, case, "hmm, fine", _proposes("closed"))
        assert case.pending_transition is None
        assert "at turn 7" in _feedback(result)

    async def test_a_user_opened_close_declined_is_recorded_on_the_finding(self):
        """The status-menu close is the third proposer: its "Not yet" click is
        a decline like any other."""
        engine, case, _ = await _finding_turn(model_proposes=None)
        await _turn(engine, case, "what does that mean for the alert?")
        case.current_turn += 1
        result = await engine.process_turn(
            case=case,
            user_message="Close this case as unresolved.",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )
        pending = case.pending_transition
        assert pending["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
        assert _SIGNED not in pending
        not_yet = result["suggested_follow_ups"][1]

        await _turn(
            engine,
            case,
            not_yet["payload"],
            intent_type="confirmation",
            intent_data={"value": False, **not_yet["intent"]},
        )
        assert case.pending_transition is None
        assert case.problem_verification.invalidation.close_declined_at_turn == 7
        assert case.progress.deferred_disposition_declined_signatures == []

    @pytest.mark.parametrize("model_opened", [False, True])
    async def test_no_false_alarm_decline_enters_the_signature_list(self, model_opened):
        engine, case, _ = await _finding_turn(model_proposes=None)
        if model_opened:
            await _turn(engine, case, "what does that mean for the alert?")
            await _turn(engine, case, "makes sense", _proposes("closed"))
        await _turn(engine, case, "no")
        assert case.problem_verification.invalidation.close_declined_at_turn
        assert not any(
            s.startswith(f"{FALSE_ALARM_CLOSURE_REASON}|")
            for s in case.progress.deferred_disposition_declined_signatures
        )

    async def test_a_bare_no_is_told_the_hold_and_given_the_card(self):
        """ "The case remains open for further investigation" is untrue on a
        hold, and the bare reply carried no follow-up at all."""
        _, _, result = await _declined_finding()
        assert result["agent_response"] == FALSE_ALARM_HOLD_REPLY
        assert "new evidence of a different problem" in result["agent_response"]
        assert "further investigation" not in result["agent_response"]
        assert _intents(result) == [_CLOSE_INTENT]

    async def test_a_bare_no_to_another_close_keeps_the_ordinary_reply(self):
        engine, case = _engine(), _case(ProblemStatus.VERIFIED)
        _respond(engine, _proposes("closed"))
        await engine.process_turn(case=case, user_message="let's stop here")
        result = await _turn(engine, case, "no")
        assert "further investigation" in result["agent_response"]
        assert result["suggested_follow_ups"] == []

    async def test_the_card_closes_the_case_as_a_false_alarm(self):
        """End to end: the card's intent is the status menu's, so it opens the
        user's own close, whose confirm pair then executes it."""
        engine, case, _ = await _declined_finding()
        refused = await _turn(engine, case, "ok, close it", _proposes("closed"))
        card = refused["suggested_follow_ups"][-1]

        case.current_turn += 1
        offered = await engine.process_turn(
            case=case,
            user_message=card["payload"],
            intent_type=card["intent"]["type"],
            intent_data={"to_state": card["intent"]["to_state"]},
        )
        assert case.pending_transition["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
        key = terminal_offer_key(case.pending_transition)
        assert [
            f["intent"].get("proposal_id") for f in offered["suggested_follow_ups"]
        ] == [
            key,
            key,
        ]

        closing = await _turn(engine, case, "yes", _proposes("closed"))
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == FALSE_ALARM_CLOSURE_REASON
        # The closed case still holds its declined finding, so the read that
        # attaches the card would answer; the closing reply carries none.
        assert _CLOSE_INTENT not in _intents(closing)


# ---------------------------------------------------------------------------
# Negative controls: the hold lasts exactly as long as its premise
# ---------------------------------------------------------------------------


class TestTheHoldEndsWithItsPremise:
    async def test_a_withdrawal_then_a_new_finding_is_offered_again(self):
        engine, case, _ = await _declined_finding()
        await _turn(engine, case, "we have customer tickets", _withdraw_update())
        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        assert case.problem_verification.invalidation is None

        await _turn(
            engine,
            case,
            "here is the load balancer log for the same window",
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "b2")],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="no 5xx at the load balancer either",
                ),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.INVALIDATED
        assert case.problem_verification.invalidation.close_declined_at_turn is None
        assert case.pending_transition[_SIGNED] == f"{FALSE_ALARM_CLOSURE_REASON}|7"

    async def test_a_confirmed_revision_lifts_the_hold(self):
        engine, case, _ = await _declined_finding()
        await _turn(
            engine,
            case,
            "The /orders API times out after 30s; the database is fine",
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
        result = await _turn(engine, case, "yes", _proposes("closed"))
        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert case.problem_verification.invalidation is None
        assert "TRANSITION NOT PROPOSED" not in _feedback(result)
        assert case.pending_transition["to_state"] == "closed"
        assert case.pending_transition["closure_reason"] != FALSE_ALARM_CLOSURE_REASON

    async def test_a_declined_revision_keeps_the_hold(self):
        """The finding survives a revision the user declined, and so does the
        answer the user gave about closing on it."""
        engine, case, _ = await _declined_finding()
        await _turn(
            engine,
            case,
            "The /orders API times out after 30s; the database is fine",
            _DSU(
                evidence_to_add=[_row(EvidenceCategory.SYMPTOM_EVIDENCE, "s1")],
                verification_updates=_revision_update("new_index_0"),
            ),
        )
        await _turn(engine, case, "no")
        assert case.progress.problem_status == ProblemStatus.INVALIDATED
        result = await _turn(engine, case, "right, ok", _proposes("closed"))
        assert case.pending_transition is None
        assert "at turn 5" in _feedback(result)

    async def test_an_edit_clears_the_hold(self):
        engine, case, _ = await _declined_finding()
        edit_statement(case, "The checkout page returns 502 at peak")
        assert case.problem_verification.invalidation is None
        result = await _turn(engine, case, "let's stop here", _proposes("closed"))
        assert "TRANSITION NOT PROPOSED" not in _feedback(result)
        assert case.pending_transition["to_state"] == "closed"

    async def test_a_close_repick_while_the_offer_stands_is_not_a_decline(self):
        """A status-menu CLOSE pick while the engine's false-alarm close stands
        re-shows that offer (#1838) and records nothing."""
        engine, case, _ = await _finding_turn(model_proposes=None)
        key = terminal_offer_key(case.pending_transition)
        case.current_turn += 1
        result = await engine.process_turn(
            case=case,
            user_message="Close this case as unresolved.",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )
        assert terminal_offer_key(case.pending_transition) == key
        assert case.problem_verification.invalidation.close_declined_at_turn is None
        assert case.progress.deferred_disposition_declined_signatures == []
        assert "false alarm" in result["agent_response"]

    async def test_the_users_own_close_is_never_blocked(self):
        engine, case, _ = await _declined_finding()
        case.current_turn += 1
        await engine.process_turn(
            case=case,
            user_message="Close this case as unresolved.",
            intent_type="status_transition",
            intent_data={"to_state": "closed"},
        )
        assert case.pending_transition["closure_reason"] == FALSE_ALARM_CLOSURE_REASON
        await _turn(engine, case, "yes")
        assert case.state == CaseState.CLOSED


# ---------------------------------------------------------------------------
# The deferred side
# ---------------------------------------------------------------------------


class TestADeclinedDeferredCloseBindsTheModel:
    async def _declined(self) -> tuple:
        """The engine's deferred close on a documented cause and fix, declined
        with a bare "no"."""
        engine, case = _engine(), _investigating(cause=True)
        _respond(
            engine, _DSU(milestones=MilestoneUpdates(solution_feasible="deferred"))
        )
        await engine.process_turn(case=case, user_message="the platform team ships it")
        assert case.pending_transition["closure_reason"] == "solution_deferred"
        signature = case.pending_transition[_SIGNED]
        await _turn(engine, case, "no")
        assert case.progress.deferred_disposition_declined_signatures == [signature]
        return engine, case

    async def test_the_models_close_at_the_declined_state_is_refused(self):
        engine, case = await self._declined()
        result = await _turn(
            engine,
            case,
            "nothing more to do here",
            _proposes("closed"),
            follow_ups=_MODEL_FOLLOW_UPS,
        )
        assert case.pending_transition is None
        feedback = _feedback(result)
        assert "TRANSITION NOT PROPOSED" in feedback
        assert "deferred-implementation close" in feedback
        assert "only if the user directs it" in feedback
        assert [f["label"] for f in result["suggested_follow_ups"]] == [
            "Share more context",
            "Ask what this means",
            "Close with the solution documented",
        ]
        assert _intents(result)[-1] == _CLOSE_INTENT

    async def test_the_card_closes_the_case_with_the_solution_documented(self):
        """End to end on the deferred side: the refused re-proposal's card opens
        the user's own close, whose confirm pair executes it as deferred."""
        engine, case = await self._declined()
        refused = await _turn(
            engine, case, "nothing more to do here", _proposes("closed")
        )
        card = refused["suggested_follow_ups"][-1]
        assert card["intent"] == _CLOSE_INTENT

        case.current_turn += 1
        offered = await engine.process_turn(
            case=case,
            user_message=card["payload"],
            intent_type=card["intent"]["type"],
            intent_data={"to_state": card["intent"]["to_state"]},
        )
        assert case.pending_transition["closure_reason"] == "solution_deferred"
        key = terminal_offer_key(case.pending_transition)
        assert [
            f["intent"].get("proposal_id") for f in offered["suggested_follow_ups"]
        ] == [key, key]

        closing = await _turn(engine, case, "yes", _proposes("closed"))
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == "solution_deferred"
        assert _CLOSE_INTENT not in _intents(closing)

    async def test_a_moved_premise_lets_the_models_close_through(self):
        """A second solution changes the justifying signature: the decline was
        about the state it was given in. The engine's own deferred close comes
        back with the new signature, and the model's proposal is superseded by
        it (#1885), not refused as a re-ask."""
        engine, case = await self._declined()
        declined = list(case.progress.deferred_disposition_declined_signatures)
        case.solutions.append(
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Pin the pool size in the base chart",
                longterm_fix="Set max connections in the chart, not per release.",
            )
        )
        result = await _turn(engine, case, "let's stop here", _proposes("closed"))
        assert "declined" not in _feedback(result)
        assert _CLOSE_INTENT not in _intents(result)
        assert case.pending_transition["to_state"] == "closed"
        assert case.pending_transition[_SIGNED] not in declined


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


def _invalidated_case():
    case = _case()
    absent = _with(case, _evidence(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a"))
    invalidate_problem(case, evidence_ids=absent, basis="the alert misfired")
    return case


class TestTheHoldBlock:
    def test_it_no_longer_steers_toward_the_close(self):
        block = _problem_hold_emphasis(_invalidated_case())
        assert "Reported problem not present" in block
        assert "right outcome is closing" not in block
        assert "only when the user directs it" in block
        assert "declined" not in block

    def test_the_declined_line_is_present_once_declined_and_conditional(self):
        case = _invalidated_case()
        case.problem_verification.invalidation.close_declined_at_turn = 6
        block = _problem_hold_emphasis(case)
        assert "The user declined closing on this finding at turn 6" in block
        # Conditional, never a flat ban: a model that obeyed a ban would never
        # propose, and a user who later asks would get no close card.
        assert "Propose a close only if the user directs it" in block
        assert "Do not propose it unprompted" in block

    @pytest.mark.parametrize(
        "stage",
        [
            InvestigationStage.DIAGNOSIS,
            InvestigationStage.TREATMENT,
            InvestigationStage.MITIGATION,
        ],
    )
    def test_it_renders_on_every_stage(self, stage):
        case = _invalidated_case()
        if stage == InvestigationStage.TREATMENT:
            case.progress.solution_accepted = True
        elif stage == InvestigationStage.MITIGATION:
            case.progress.mitigation = MitigationRecord(
                proposed_at_turn=1, accepted=True
            )
        assert case.current_stage == stage
        case.problem_verification.invalidation.close_declined_at_turn = 6
        prompt = get_prompt_for_case(case, "what now?")
        assert "Reported problem not present (false alarm)" in prompt
        assert "declined closing on this finding at turn 6" in prompt
        # The hold IS the focus emphasis: on DIAGNOSIS the zone block must not
        # take its place (an INVALIDATED problem is not verified, so the zone
        # reading would be Zone 1's verification ask).
        assert "Symptom verification pending" not in prompt

    def test_the_card_names_a_state_not_an_offer(self):
        for side in ("false_alarm", "deferred"):
            card = declined_close_card(side)
            assert card["intent"] == _CLOSE_INTENT
            assert card["action_type"] == "DECIDE"
