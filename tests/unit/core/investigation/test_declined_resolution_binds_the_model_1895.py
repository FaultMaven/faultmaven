"""#1895: a declined resolution binds the model until a NEW confirmation is
recorded, and a user who changes their mind has the "Mark it resolved" chip.

RESOLVED is earned, never requested: ``assess_resolution_readiness`` READY (a
qualifying ``causal_absence_evidence`` row) earns the offer, the engine or the
model makes it, and the user confirms it (INV-03). A decline postpones it until
a solution or a confirmation it never saw is recorded (fm#1122, ruling 3), and
the move a user most often brings after "not yet" is a fresh confirmation that
the fix held. So the decline's premise on the offer record is the solution and
confirmation ids on record, matched by a subset rule: the decline stands until
a row it never saw arrives. While it stands the
model's ``resolved`` (and its ``closed`` that pivots to RESOLVED) is refused
with feedback, and the turn carries the chip. The chip re-presents the engine's
declined offer under a reopen key; only that key admits a RESOLVED
``status_transition`` at the two refusal sites, and it only proposes.

Real ``process_turn`` turns with a stubbed generator: no live LLM.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.cause_assurance import (
    ENGINE_EVIDENCE_AUTHOR,
    cause_elimination_rows,
    resolution_confirmation_rows,
)
from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.transitions import (
    DECLINED_RESOLVE_FEEDBACK,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import TurnCommitPlan
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _compose_turn_reply,
)
from faultmaven.core.investigation.offer_projection import (
    DECLINED_RESOLVE_CARD_LABEL,
    project_offers,
)
from faultmaven.core.investigation.offers import decline_stands
from faultmaven.core.investigation.prompts.templates.assembly import (
    RESOLVE_DECLINED_LINE,
    get_prompt_for_case,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    SuggestedFollowUp,
)
from faultmaven.core.investigation.terminal_transitions import (
    RESOLVE_DECLINED_RULE,
    ClosureReadiness,
    ResolutionReadiness,
    closure_verdict,
    confirm_open_offer,
    derive_disposition_eligibility,
    resolve_reopen_admitted,
    resolve_reopen_key,
)
from faultmaven.exceptions import ValidationException
from faultmaven.models.api_models import QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.intent_gates import (
    _minted_intent_swallows_gate_consent,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import (
    AnswerVia,
    CaseState,
    InvestigationStage,
    MitigationRecord,
    ProblemStatus,
)
from faultmaven.modules.case.domain.models.causal import (
    CausalNode,
    NodeEvidenceLink,
    NodeType,
)
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    USER_SELECTABLE_ACTIONS,
    CaseActionManager,
    SelectableNextState,
)
from tests.case_builders import with_declined_offer, with_open_offer
from tests.model_content import disabled_redaction
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _case as _deferred_case,
)
from tests.unit.core.investigation.test_deferred_disposition_turn import (
    _deferred_response,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
    _make_repo,
)
from tests.unit.modules.agent.conftest import MockCaseRepository

_FILE_ID = "file_0123456789ab"

pytestmark = pytest.mark.unit

_SR = ClosureReadiness.SUGGEST_RESOLVE

#: A confirmation the model records from the user's words: a new verification.
_ROW = {
    "summary": "Overnight batch ran clean after the client-ID fix; no AssumeRole "
    "failures in 12h.",
    "extract": "0 AssumeRole errors since 02:00",
    "category": "causal_absence_evidence",
    "source_type": "logs",
    "source_file_id": _FILE_ID,
}

#: A long non-question reply to the offer: a deflection, recorded as a decline
#: and handed to the model.
_DEFLECTION = (
    "Not yet - it has been clean for an hour but I want to wait for the "
    "overnight batch before calling it"
)

_MODEL_FOLLOW_UPS = [
    SuggestedFollowUp(label="Share more context", action_type="FREE_SPEECH"),
]


def _engine() -> MilestoneEngine:
    return MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())


def _respond(
    engine,
    *,
    row: bool = False,
    propose: str | None = None,
    narration: str = "Noted.",
    follow_ups: list | None = None,
    side_effect=None,
) -> None:
    state_updates: dict = {}
    if row:
        state_updates["evidence_to_add"] = [dict(_ROW)]
    if propose:
        state_updates["proposed_transition"] = {"to_state": propose}
    response = InvestigationResponse_Diagnosis(
        agent_response=narration,
        state_updates=state_updates,
        suggested_follow_ups=follow_ups,
    )
    if side_effect is None:
        engine.generator.generate_structured_output = AsyncMock(return_value=response)
    else:

        async def _generate(*args, **kwargs):
            side_effect()
            return response

        engine.generator.generate_structured_output = AsyncMock(side_effect=_generate)


async def _turn(engine, case, message: str, **kw) -> dict:
    case.current_turn += 1
    process_kw = {
        k: kw.pop(k) for k in ("intent_type", "intent_data", "typed") if k in kw
    }
    _respond(engine, **kw)
    return await engine.process_turn(
        case=case, user_message=message, **process_kw, redaction_ctx=None
    )


def _feedback(result) -> str:
    return result["case_updated"].turn_history[-1].system_feedback or ""


def _labels(result) -> list:
    return [f["label"] for f in result["suggested_follow_ups"]]


def _declined(case) -> list:
    """The declines on the case's offer record, oldest first."""
    return [a for a in case.offer_record.answers if a.answer == "declined"]


def _qualifying(case) -> list:
    return sorted(e.evidence_id for e in resolution_confirmation_rows(case))


def _decline(case, how: str) -> tuple[str, dict]:
    """The message and ``process_turn`` keywords of a decline of the open
    resolve offer, by how it arrives at the gate: a bare typed token, a
    confirmation the intent resolver minted from short typed text (#2223's
    turn 10), the Not-yet click, or a deflection."""
    offer = case.offer_record.open
    assert offer is not None and offer.kind == "resolve", "premise: an offer"
    if how == "token":
        return "no", {}
    if how == "minted":
        return "I can't call it resolved yet.", {
            "intent_type": "confirmation",
            "intent_data": {"value": False, "proposal_id": offer.proposal_id},
            "typed": True,
        }
    if how == "click":
        # The engine's intent shape, as the service hands the card's on.
        card = project_offers(case).pair[1]
        assert card["intent"]["confirmation_value"] is False, "premise: Not yet"
        return card["payload"], {
            "intent_type": "confirmation",
            "intent_data": {
                "value": False,
                "proposal_id": card["intent"]["proposal_id"],
            },
        }
    assert how == "deflection", how
    return _DEFLECTION, {}


#: The gate's record of each way a decline arrives (``_decline``).
_DECLINE_VIA = {
    "token": AnswerVia.TOKEN,
    "minted": AnswerVia.TOKEN,
    "click": AnswerVia.CLICK,
    "deflection": AnswerVia.TYPED,
}


async def _offered_then_declined(reply: str = "no"):
    """READY → the engine's backstop offers on turn 1 → declined on turn 2."""
    case = _confirmed_case()
    engine = _engine()
    await _turn(engine, case, "errors gone")
    assert case.offer_record.open.kind == "resolve"
    result = await _turn(engine, case, reply)
    assert case.offer_record.open is None
    return engine, case, result


def _chip_intent(case) -> dict:
    chip = project_offers(case).reopen_chip
    assert chip is not None, "premise: the resolve decline stands"
    return dict(chip["intent"])


def _engine_disconfirmation(turn: int) -> Evidence:
    """An M6 failed-fix marker: an ENGINE-authored causal_absence row."""
    return Evidence(
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        primary_purpose="record the failed fix",
        summary="The restart did not remove the cause; the failures returned.",
        source_type=EvidenceSourceType.ENGINE_INFERENCE,
        collected_by=ENGINE_EVIDENCE_AUTHOR,
        collected_at_turn=turn,
    )


def _user_confirmation(turn: int) -> Evidence:
    return Evidence(
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        primary_purpose="confirm the cause was eliminated",
        summary="The nightly job ran clean after the fix; zero AssumeRole errors.",
        source_type=EvidenceSourceType.LOGS,
        source_file_id=_FILE_ID,
        collected_by="user",
        collected_at_turn=turn,
    )


def _user_problem_gone(turn: int) -> Evidence:
    """The problem leg of a confirmation (#1906): the reported symptom gone."""
    return Evidence(
        category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        primary_purpose="re-check the reported symptom after the fix",
        summary="The nightly job's AssumeRole calls succeed again.",
        source_type=EvidenceSourceType.LOGS,
        source_file_id=_FILE_ID,
        collected_by="user",
        collected_at_turn=turn,
    )


def _refute(case, evidence_ids: list) -> str:
    """Mark ``evidence_ids`` failed-fix disconfirmations: REFUTES-linked to a
    node the engine's own absence row refutes. Returns the node id."""
    marker = _engine_disconfirmation(turn=0)
    case.evidence.append(marker)
    node = CausalNode(
        statement="The pod restart cleared the stale token cache",
        node_type=NodeType.INTERMEDIATE,
        generated_at_turn=0,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=eid, stance=EvidenceStance.REFUTES, reasoning="refuted"
            )
            for eid in [marker.evidence_id, *evidence_ids]
        ],
    )
    case.causal_nodes[node.node_id] = node
    return node.node_id


# ---------------------------------------------------------------------------
# The premise, and the one predicate that reads it
# ---------------------------------------------------------------------------


class TestThePremiseNamesTheConfirmations:
    async def test_a_decline_snapshots_the_solution_and_confirmation_ids(self):
        _, case, _ = await _offered_then_declined()
        (decline,) = _declined(case)
        assert decline.kind == "resolve" and decline.basis == "backstop"
        assert list(decline.premise.confirmation_ids) == _qualifying(case)
        assert list(decline.premise.solution_ids) == [
            s.solution_id for s in case.solutions
        ]

    async def test_it_stands_on_a_shrink_and_lapses_on_a_row_it_never_saw(self):
        _, case, _ = await _offered_then_declined()
        (decline,) = _declined(case)
        _refute(case, [_qualifying(case)[0]])
        assert decline_stands(decline, case), "a shrink leaves it standing"
        case.evidence.append(_user_confirmation(turn=case.current_turn))
        case.evidence.append(_user_problem_gone(turn=case.current_turn))
        assert not decline_stands(decline, case), "a new row lapses it"


# ---------------------------------------------------------------------------
# The model is bound (D3, A2)
# ---------------------------------------------------------------------------


class TestADeclinedResolutionBindsTheModel:
    async def test_the_models_resolve_on_unchanged_evidence_is_refused(self):
        """B3 measured ``pending=resolved``: nothing refused it."""
        engine, case, _ = await _offered_then_declined()
        declined_before = list(_declined(case))
        result = await _turn(
            engine,
            case,
            "anything else?",
            propose="resolved",
            follow_ups=_MODEL_FOLLOW_UPS,
        )
        assert case.offer_record.open is None
        assert case.state == CaseState.INVESTIGATING
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        # Appended after the model's own follow-ups, never substituted.
        assert _labels(result) == ["Share more context", DECLINED_RESOLVE_CARD_LABEL]
        assert result["suggested_follow_ups"][-1]["intent"] == {
            "type": "status_transition",
            "to_state": "resolved",
            "proposal_id": resolve_reopen_key(declined_before[-1]),
        }
        assert _declined(case) == declined_before

    async def test_the_models_close_that_pivots_to_resolve_is_refused(self):
        """INV-37's pivot asks the same question the user just answered."""
        engine, case, _ = await _offered_then_declined()
        result = await _turn(engine, case, "anything else?", propose="closed")
        assert case.offer_record.open is None
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        assert _labels(result)[-1] == DECLINED_RESOLVE_CARD_LABEL

    async def test_the_refusal_is_logged_as_resolve(self, caplog):
        engine, case, _ = await _offered_then_declined()
        with caplog.at_level("INFO"):
            await _turn(engine, case, "anything else?", propose="resolved")
        lines = [r for r in caplog.records if r.getMessage() == "transition_compliance"]
        assert lines and lines[-1].transition_refused_as_declined == "resolve"

    async def test_a_later_ordinary_turn_still_shows_the_chip(self):
        """The model never proposes on a request and points the user at the
        chip, so the chip is on screen on EVERY turn the decline stands, not
        only on a refusal: a user who changes their mind always has it."""
        engine, case, _ = await _offered_then_declined()
        for message in ("what else is in that log?", "ok, I changed my mind"):
            result = await _turn(engine, case, message, follow_ups=_MODEL_FOLLOW_UPS)
            assert _labels(result) == [
                "Share more context",
                DECLINED_RESOLVE_CARD_LABEL,
            ]
            assert result["suggested_follow_ups"][-1]["intent"] == _chip_intent(case)
            assert DECLINED_RESOLVE_FEEDBACK not in _feedback(result)
            assert case.offer_record.open is None

    async def test_exactly_one_chip_on_a_refusal_turn(self):
        """Refusal and standing decline both call for the chip; a model
        suggestion carrying its label is dropped rather than shown twice."""
        engine, case, _ = await _offered_then_declined()
        result = await _turn(
            engine,
            case,
            "please mark it resolved",
            propose="resolved",
            follow_ups=[
                *_MODEL_FOLLOW_UPS,
                SuggestedFollowUp(label="Mark it resolved", action_type="FREE_SPEECH"),
            ],
        )
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        assert _labels(result) == ["Share more context", DECLINED_RESOLVE_CARD_LABEL]
        assert result["suggested_follow_ups"][-1]["intent"] == _chip_intent(case)

    async def test_no_chip_once_a_new_confirmation_brings_the_offer_back(self):
        engine, case, _ = await _offered_then_declined()
        result = await _turn(
            engine, case, "the overnight batch ran clean, zero errors", row=True
        )
        assert case.offer_record.open.kind == "resolve"
        assert _labels(result) == [
            "Yes, mark as resolved",
            "Not yet, continue investigating",
        ]

    async def test_no_chip_while_an_offer_is_pending(self):
        """At the append site: with the decline standing, the chip is attached
        until an offer is pending, then left off (the offer's own pair answers
        it). Composed directly, since no real turn ends with an offer pending
        while the decline stands: every opener is refused or silent then."""
        _, case, _ = await _offered_then_declined()

        async def _compose():
            return await _compose_turn_reply(
                None,
                None,
                _make_repo(),
                case_updated=case,
                follow_ups=[],
                metadata={},
                plan=TurnCommitPlan(),
                redaction_ctx=None,
                response_obj=InvestigationResponse_Diagnosis(
                    agent_response="Noted.", state_updates={}
                ),
                stagnation_str="",
                summary_failed=False,
                summary_payload=None,
                validation_repairs=[],
            )

        assert _labels(await _compose()) == [DECLINED_RESOLVE_CARD_LABEL], "control"
        # The chip's own resolve offer: the user's, so the decline does not
        # bind it.
        with_open_offer(case, kind="resolve", basis="reopen")
        assert DECLINED_RESOLVE_CARD_LABEL not in _labels(await _compose())

    async def test_a_refused_model_narrating_resolved_gets_the_still_open_notice(self):
        """INV-40: the refusal leaves no gate prose, so the over-claim is
        contradicted below the reply."""
        engine, case, _ = await _offered_then_declined()
        result = await _turn(
            engine,
            case,
            "so we're done?",
            propose="resolved",
            narration="Case resolved. The audience mismatch is corrected.",
        )
        assert case.state == CaseState.INVESTIGATING
        assert "this case has not been resolved or closed" in result["agent_response"]

    async def test_the_users_own_close_proposes_the_close(self):
        """U1, as ruled in #2360: the user's close is never refused, and after
        a decline it is not pivoted back to the resolution they declined. It
        proposes CLOSED with the close pair, and the chip stays off the turn
        (an offer stands)."""
        engine, case, _ = await _offered_then_declined()
        result = await _turn(
            engine,
            case,
            "",
            intent_type="status_transition",
            intent_data={"to_state": "closed", "from_state": "investigating"},
        )
        assert case.offer_record.open.kind == "close"
        assert "TRANSITION NOT PROPOSED" not in result["agent_response"]
        assert _labels(result) == [
            "Yes, close this case",
            "Not yet, continue investigating",
        ]
        assert DECLINED_RESOLVE_CARD_LABEL not in _labels(result)


# ---------------------------------------------------------------------------
# What re-earns it: a NEW confirmation, on the turn it is recorded
# ---------------------------------------------------------------------------


class TestANewConfirmationReEarnsTheOffer:
    async def test_the_backstop_offers_on_the_turn_the_row_lands(self):
        """C measured silence here: the case re-earned the offer and nobody
        offered."""
        engine, case, _ = await _offered_then_declined()
        await _turn(
            engine, case, "the overnight batch ran clean, zero errors", row=True
        )
        assert case.offer_record.open.kind == "resolve"
        assert case.offer_record.open.basis == "backstop"

    async def test_the_models_offer_lands_on_the_turn_it_records_the_row(self):
        engine, case, _ = await _offered_then_declined()
        result = await _turn(
            engine,
            case,
            "the overnight batch ran clean, zero errors",
            row=True,
            propose="resolved",
        )
        assert case.offer_record.open.kind == "resolve"
        assert DECLINED_RESOLVE_FEEDBACK not in _feedback(result)
        assert DECLINED_RESOLVE_CARD_LABEL not in _labels(result)

    async def test_a_row_disqualified_by_a_later_failed_fix_brings_no_offer(self):
        """The set shrinks: an older row stops qualifying behind a failed-fix
        window. The decline still covers what qualifies, so nothing re-offers."""
        case = _confirmed_case()
        case.evidence.extend([_user_confirmation(turn=3), _user_problem_gone(turn=3)])
        engine = _engine()
        case.current_turn = 3
        await _turn(engine, case, "errors gone")
        assert case.offer_record.open.kind == "resolve"
        await _turn(engine, case, "no")
        before = _qualifying(case)
        assert len(before) == 4, "premise: both legs, at turn 1 and at turn 3"

        case.evidence.append(_engine_disconfirmation(turn=2))
        assert (
            len(_qualifying(case)) == 2
        ), "premise: the turn-1 confirmation (both legs) disqualified"
        assert closure_verdict(case) == _SR
        await _turn(engine, case, "what else should we watch?")
        assert case.offer_record.open is None

    async def test_a_pruned_refutation_re_entering_an_old_row_brings_no_offer(self):
        """A shrink by REFUTES, then the refuting node pruned: the row comes
        back, but the decline saw it. Equality would re-offer on both turns."""
        case = _confirmed_case()
        case.evidence.append(_user_confirmation(turn=1))
        engine = _engine()
        await _turn(engine, case, "errors gone")
        await _turn(engine, case, "no")
        rows = _qualifying(case)
        cause_rows = sorted(e.evidence_id for e in cause_elimination_rows(case))
        assert len(rows) == 3 and len(cause_rows) == 2

        node_id = _refute(case, [cause_rows[1]])
        assert _qualifying(case) == [
            r for r in rows if r != cause_rows[1]
        ], "premise: one cause row disqualified, the confirmation still stands"
        await _turn(engine, case, "ok, what next?")
        assert case.offer_record.open is None

        del case.causal_nodes[node_id]
        assert _qualifying(case) == rows, "premise: the row re-entered"
        await _turn(engine, case, "anything else?")
        assert case.offer_record.open is None


# ---------------------------------------------------------------------------
# The decline turn covers what it records (D2 / A3) and carries the chip (B5)
# ---------------------------------------------------------------------------


class TestTheDeclineTurn:
    async def test_a_bare_no_records_one_decline_and_answers_with_the_chip(self):
        """#2223: the bare "no" is answered by the model, not a canned line,
        and the reply still carries the chip."""
        case = _confirmed_case()
        engine = _engine()
        await _turn(engine, case, "errors gone")
        result = await _turn(
            engine,
            case,
            "no",
            narration="What would you need to see?",
            follow_ups=_MODEL_FOLLOW_UPS,
        )
        assert engine.generator.generate_structured_output.await_count == 1
        (decline,) = _declined(case)
        assert decline.via == AnswerVia.TOKEN
        assert list(decline.premise.confirmation_ids) == _qualifying(case)
        assert result["agent_response"].startswith("What would you need to see?")
        assert _labels(result) == ["Share more context", DECLINED_RESOLVE_CARD_LABEL]

    @pytest.mark.parametrize("how", ["deflection", "token", "minted"])
    async def test_a_decline_recording_a_row_and_proposing_is_refused(self, how):
        """D measured ``pending=resolved`` on the turn the user said not yet;
        the re-stamp makes step 2 read the row as covered. Every decline the
        gate records reaches the model (#2223), the bare and the minted one
        too, and on each the decline binds the model's same-turn proposal."""
        case = _confirmed_case()
        engine = _engine()
        await _turn(engine, case, "errors gone")
        message, process_kw = _decline(case, how)
        result = await _turn(
            engine, case, message, row=True, propose="resolved", **process_kw
        )
        assert engine.generator.generate_structured_output.await_count == 1
        assert _declined(case)[-1].via == _DECLINE_VIA[how]
        assert case.offer_record.open is None
        assert case.state == CaseState.INVESTIGATING
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        # The decline was widened to cover the row the model recorded on it.
        assert set(_qualifying(case)) <= set(
            _declined(case)[-1].premise.confirmation_ids
        )

        # And the row is not "new" on the next turn either.
        await _turn(engine, case, "ok what next")
        assert case.offer_record.open is None

    async def test_the_deflection_turn_carries_the_chip(self):
        """B5: the deflection falls through to the model; its reply still
        carries the chip, keyed on the decline the turn recorded."""
        case = _confirmed_case()
        engine = _engine()
        await _turn(engine, case, "errors gone")
        result = await _turn(
            engine, case, _DEFLECTION, row=True, follow_ups=_MODEL_FOLLOW_UPS
        )
        assert _labels(result) == ["Share more context", DECLINED_RESOLVE_CARD_LABEL]
        assert result["suggested_follow_ups"][-1]["intent"]["proposal_id"] == (
            resolve_reopen_key(_declined(case)[-1])
        )
        await _turn(engine, case, "ok what next")
        assert case.offer_record.open is None

    async def test_the_same_turn_widening_never_narrows_the_premise(self):
        """A same-turn disconfirmation shrinks what qualifies: the decline's
        premise still names every row it was given against, so a row that
        re-enters later moves nothing."""
        case = _confirmed_case()
        engine = _engine()
        await _turn(engine, case, "errors gone")
        assert _declined(case) == []
        declined_rows = _qualifying(case)

        def _failed_fix_lands():
            case.evidence.append(_engine_disconfirmation(turn=case.current_turn))

        await _turn(engine, case, _DEFLECTION, side_effect=_failed_fix_lands)
        assert closure_verdict(case) != _SR, "premise: the verdict flipped"
        (decline,) = _declined(case)
        assert set(declined_rows) <= set(decline.premise.confirmation_ids)


# ---------------------------------------------------------------------------
# The chip: re-presents the declined offer, proposes, never confirms
# ---------------------------------------------------------------------------


class TestTheChip:
    async def test_a_click_proposes_with_the_confirmation_pair_then_yes_resolves(self):
        engine, case, _ = await _offered_then_declined()
        intent = _chip_intent(case)
        result = await _turn(
            engine,
            case,
            "Mark this case resolved.",
            intent_type="status_transition",
            intent_data=intent,
        )
        assert case.state == CaseState.INVESTIGATING
        assert case.offer_record.open.kind == "resolve"
        key = case.offer_record.open.proposal_id
        assert _labels(result) == [
            "Yes, mark as resolved",
            "Not yet, continue investigating",
        ]
        assert [f["intent"]["proposal_id"] for f in result["suggested_follow_ups"]] == [
            key,
            key,
        ]

        await _turn(
            engine,
            case,
            "Yes, the issue is resolved. Please mark this case as resolved.",
            intent_type="confirmation",
            intent_data={"value": True, "proposal_id": key},
        )
        assert case.state == CaseState.RESOLVED

    async def test_a_typed_exact_match_mints_the_chip_and_only_proposes(self):
        engine, case, _ = await _offered_then_declined()
        chip = project_offers(case).reopen_chip
        minted = await IntentResolver(None).resolve(
            "Mark this case resolved.", [chip], redaction_ctx=disabled_redaction()
        )
        assert minted == chip["intent"]
        # INV-26: the mint proposes, it commits no gate, so it is adopted.
        assert not _minted_intent_swallows_gate_consent(
            case, QueryIntent(**minted), "Mark this case resolved."
        )
        await _turn(
            engine,
            case,
            "Mark this case resolved.",
            intent_type="status_transition",
            intent_data=dict(minted),
            typed=True,
        )
        assert case.state == CaseState.INVESTIGATING
        assert case.offer_record.open.kind == "resolve"

    async def test_a_double_click_re_asks_and_records_nothing(self):
        engine, case, _ = await _offered_then_declined()
        intent = _chip_intent(case)
        await _turn(
            engine, case, "", intent_type="status_transition", intent_data=intent
        )
        offer = case.offer_record.open
        declined = list(_declined(case))
        result = await _turn(
            engine, case, "", intent_type="status_transition", intent_data=intent
        )
        assert case.state == CaseState.INVESTIGATING
        assert case.offer_record.open == offer
        assert _declined(case) == declined
        assert _labels(result) == [
            "Yes, mark as resolved",
            "Not yet, continue investigating",
        ]

    async def test_a_covered_shrink_keeps_the_chip_working(self):
        """B1: the key names the declined offer, so a chip rendered before a
        row stopped qualifying still names the decline that stands."""
        case = _confirmed_case()
        case.evidence.append(_user_confirmation(turn=1))
        engine = _engine()
        await _turn(engine, case, "errors gone")
        await _turn(engine, case, "no")
        intent = _chip_intent(case)
        _refute(case, [_qualifying(case)[1]])
        assert project_offers(case).reopen_chip["intent"] == intent
        await _turn(
            engine, case, "", intent_type="status_transition", intent_data=intent
        )
        assert case.offer_record.open.kind == "resolve"

    async def test_a_chip_is_refused_unless_resolution_readiness_is_ready(self):
        """B2. Today SUGGEST_RESOLVE and READY share one predicate, so this
        state is constructed by reading readiness as NEEDS_INFO: the handler
        reads resolution readiness, not the closure verdict."""
        engine, case, _ = await _offered_then_declined()
        intent = _chip_intent(case)
        not_ready = ResolutionReadiness(
            verdict=ResolutionReadiness.NEEDS_INFO,
            message="What confirmed the fix held?",
            missing=["confirmation the problem is now resolved"],
        )
        with patch(
            "faultmaven.core.investigation.terminal_transitions."
            "assess_resolution_readiness",
            return_value=not_ready,
        ):
            result = await _turn(
                engine, case, "", intent_type="status_transition", intent_data=intent
            )
        assert case.offer_record.open is None
        assert "can't be marked resolved" in result["agent_response"]
        assert result["suggested_follow_ups"] == []

    def test_no_chip_while_an_offer_stands(self):
        case = _confirmed_case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        assert project_offers(case).reopen_chip is not None
        with_open_offer(case, kind="resolve", basis="reopen")
        assert project_offers(case).reopen_chip is None


# ---------------------------------------------------------------------------
# The two refusal sites admit only the current key
# ---------------------------------------------------------------------------


def _service(engine) -> InvestigationService:
    return InvestigationService(
        milestone_engine=engine,
        case_repository=MockCaseRepository(),
        preprocessing_service=AsyncMock(),
        file_storage_service=AsyncMock(),
    )


async def _at_the_boundary(service, case, proposal_id):
    return await service._handle_status_transition(
        case=case,
        user_message="Mark this case resolved.",
        from_state=case.state.value,
        to_state="resolved",
        user_confirmed=False,
        proposal_id=proposal_id,
        redaction_ctx=None,
    )


class TestTheRefusalSitesAdmitOnlyTheCurrentKey:
    async def test_the_boundary_admits_the_current_key_and_forwards_it(self):
        engine, case, _ = await _offered_then_declined()
        key = _chip_intent(case)["proposal_id"]
        stub = MagicMock()
        stub.process_turn = AsyncMock(return_value={"agent_response": "ok"})
        await _at_the_boundary(_service(stub), case, key)
        stub.process_turn.assert_awaited_once()
        assert stub.process_turn.call_args.kwargs["intent_data"]["proposal_id"] == key

        # End to end through the real engine: proposed, not confirmed.
        case.current_turn += 1
        await _at_the_boundary(_service(engine), case, key)
        assert case.offer_record.open.kind == "resolve"
        assert case.state == CaseState.INVESTIGATING

    async def test_a_stale_key_is_a_422_and_the_backstop_offers_instead(self):
        engine, case, _ = await _offered_then_declined()
        stale = _chip_intent(case)["proposal_id"]
        await _turn(
            engine, case, "the overnight batch ran clean, zero errors", row=True
        )
        offer = case.offer_record.open
        assert offer.kind == "resolve", "the engine offers it itself"

        stub = MagicMock()
        stub.process_turn = AsyncMock()
        with pytest.raises(ValidationException, match="not a user-selectable"):
            await _at_the_boundary(_service(stub), case, stale)
        stub.process_turn.assert_not_called()

        case.current_turn += 1
        with pytest.raises(ValueError, match="not a user-selectable"):
            await engine.process_turn(
                case=case,
                user_message="",
                intent_type="status_transition",
                intent_data={"to_state": "resolved", "proposal_id": stale},
                redaction_ctx=None,
            )
        assert case.offer_record.open == offer

    async def test_a_key_with_no_decline_standing_is_a_422(self):
        case = _confirmed_case()
        forged = "reopen:0123456789abcdef"
        stub = MagicMock()
        stub.process_turn = AsyncMock()
        with pytest.raises(ValidationException):
            await _at_the_boundary(_service(stub), case, forged)
        stub.process_turn.assert_not_called()

        case.current_turn += 1
        with pytest.raises(ValueError, match="not a user-selectable"):
            await _engine().process_turn(
                case=case,
                user_message="",
                intent_type="status_transition",
                intent_data={"to_state": "resolved", "proposal_id": forged},
                redaction_ctx=None,
            )
        assert case.offer_record.open is None

    async def test_another_key_while_the_decline_stands_is_a_422(self):
        """The key names the decline: a key for any other one (an older
        decline the user has since moved past, or a guess) is refused at both
        sites even while a decline stands."""
        engine, case, _ = await _offered_then_declined()
        wrong = "reopen:" + "0" * 16
        assert wrong != _chip_intent(case)["proposal_id"]
        stub = MagicMock()
        stub.process_turn = AsyncMock()
        with pytest.raises(ValidationException, match="not a user-selectable"):
            await _at_the_boundary(_service(stub), case, wrong)
        stub.process_turn.assert_not_called()

        case.current_turn += 1
        with pytest.raises(ValueError, match="not a user-selectable"):
            await engine.process_turn(
                case=case,
                user_message="",
                intent_type="status_transition",
                intent_data={"to_state": "resolved", "proposal_id": wrong},
                redaction_ctx=None,
            )
        assert case.offer_record.open is None

    async def test_the_engine_guard_admits_the_current_key(self):
        """The engine's own copy of the admission, reached without the
        service (its backstop)."""
        engine, case, _ = await _offered_then_declined()
        await _turn(
            engine,
            case,
            "",
            intent_type="status_transition",
            intent_data=_chip_intent(case),
        )
        assert case.offer_record.open.kind == "resolve"


class TestRESOLVEDStaysUnselectable:
    def test_the_menu_lists_only_close_on_a_ready_declined_case(self):
        """B3: the chip is not a menu entry. The status menu still lists only
        dispositions, and once the resolution is declined it shows Close
        (#2360)."""
        assert dict(USER_SELECTABLE_ACTIONS) == {
            CaseState.INQUIRY: (CaseState.CLOSED,),
            CaseState.INVESTIGATING: (CaseState.CLOSED,),
            CaseState.RESOLVED: (),
            CaseState.CLOSED: (),
        }
        case = _confirmed_case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        assert project_offers(case).reopen_chip is not None
        assert CaseActionManager.selectable_next_states(case.state) == [
            SelectableNextState.CLOSED
        ]
        assert derive_disposition_eligibility(case) == {
            "resolved": "ready",
            "closed": "ready",
        }


# ---------------------------------------------------------------------------
# The other declines and the INV-37 pivot
# ---------------------------------------------------------------------------


class TestTheRestOfTheSpace:
    async def test_a_declined_deferred_close_still_binds(self):
        """The deferred close is declined below SUGGEST_RESOLVE, with no
        confirmation on record: it binds the engine's deferred close and the
        model's close."""
        case = _deferred_case(confirmed=False)
        engine = _engine()
        engine.generator.generate_structured_output = AsyncMock(
            return_value=_deferred_response()
        )
        await engine.process_turn(
            case=case, user_message="the platform team ships it", redaction_ctx=None
        )
        assert case.offer_record.open.kind == "close"
        case.current_turn += 1
        await engine.process_turn(case=case, user_message="no", redaction_ctx=None)
        ((decline),) = _declined(case)
        assert (decline.kind, decline.basis) == ("close", "deferred")
        assert decline.premise.confirmation_ids == ()

        case.current_turn += 1
        await engine.process_turn(
            case=case, user_message="what should we change?", redaction_ctx=None
        )
        assert case.offer_record.open is None, "the declined offer came back"
        result = await _turn(engine, case, "ok, close it", propose="closed")
        assert case.offer_record.open is None
        assert "deferred-implementation close" in _feedback(result)

    def test_the_inv37_pivot_carries_the_close_basis(self):
        """An engine close accepted on a case that became resolvable is
        answered ``pivoted``, and the resolve offer opened in its place carries
        the close's basis, so its decline binds as the engine's."""
        case = _confirmed_case()
        close = with_open_offer(case, kind="close", basis="deferred")
        with patch(
            "faultmaven.core.investigation.terminal_transitions."
            "close_pivoted_to_resolve_total"
        ):
            assert confirm_open_offer(case, "user_test", via=AnswerVia.CLICK) is False
        resolve = case.offer_record.open
        assert (resolve.kind, resolve.basis) == ("resolve", "deferred")
        assert resolve.pivoted_from_close
        assert resolve.proposal_id != close.proposal_id
        last = case.offer_record.answers[-1]
        assert (last.proposal_id, last.answer) == (close.proposal_id, "pivoted")


# ---------------------------------------------------------------------------
# The prompt (A6): conditional, on every stage, only while the decline stands
# ---------------------------------------------------------------------------


class TestThePromptLine:
    def test_absent_before_a_decline(self):
        case = _confirmed_case()
        assert RESOLVE_DECLINED_LINE not in get_prompt_for_case(case, "what now?")

    @pytest.mark.parametrize(
        "stage",
        [
            InvestigationStage.DIAGNOSIS,
            InvestigationStage.TREATMENT,
            InvestigationStage.MITIGATION,
        ],
    )
    def test_present_on_every_stage_while_the_decline_stands(self, stage):
        case = _confirmed_case()
        if stage == InvestigationStage.TREATMENT:
            case.progress.solution_accepted = True
        elif stage == InvestigationStage.MITIGATION:
            case.progress.mitigation = MitigationRecord(
                proposed_at_turn=1, accepted=True
            )
        assert case.current_stage == stage
        with_declined_offer(case, kind="resolve", basis="backstop")
        prompt = get_prompt_for_case(case, "what now?")
        assert RESOLVE_DECLINED_LINE in prompt

        # Gone once a new confirmation is recorded: the offer is due back.
        case.evidence.append(_user_confirmation(turn=4))
        assert RESOLVE_DECLINED_LINE not in get_prompt_for_case(case, "what now?")

    def test_the_line_and_the_refusal_state_one_rule_and_name_the_chip(self):
        """A NEW verification is evidence: record it, then propose. A request
        is not: the model points the user at the chip and never proposes on it
        (A1/A6), so the prompt and the step-2 feedback cannot disagree."""
        assert RESOLVE_DECLINED_RULE in RESOLVE_DECLINED_LINE
        assert RESOLVE_DECLINED_RULE in DECLINED_RESOLVE_FEEDBACK
        assert "'Mark it resolved'" in RESOLVE_DECLINED_RULE
        assert "point them to it" in RESOLVE_DECLINED_RULE
        assert "never record a confirmation row from a request" in RESOLVE_DECLINED_RULE
        assert "propose resolved in the same turn" in RESOLVE_DECLINED_RULE
        for text in (RESOLVE_DECLINED_LINE, DECLINED_RESOLVE_FEEDBACK):
            assert "asks to mark it resolved" not in text
            assert "if the user asks" not in text.lower()


# ---------------------------------------------------------------------------
# Review round (#1915): the decline turn's markers reach the apply step, and
# nothing re-offers while a decline stands
# ---------------------------------------------------------------------------


async def _deferred_offer(*, resolvable: bool):
    """The deferred proposer's offer on turn 1: a resolve on a confirmed case,
    the documented close otherwise."""
    case = _deferred_case(confirmed=resolvable)
    engine = _engine()
    engine.generator.generate_structured_output = AsyncMock(
        return_value=_deferred_response()
    )
    await engine.process_turn(
        case=case, user_message="the platform team ships it", redaction_ctx=None
    )
    assert case.offer_record.open.kind == ("resolve" if resolvable else "close")
    assert case.offer_record.open.basis == "deferred"
    return engine, case


class TestTheDeclineTurnReachesTheApplyStep:
    @pytest.mark.parametrize("model_proposes", [None, "resolved"])
    async def test_a_deflection_on_a_deferred_resolve_offer_is_not_re_offered(
        self, model_proposes
    ):
        """F1: the deferred proposer runs inside the apply step, before the
        step-3b lapse. It reads the decline 0b answered on the record, so the
        row the model records from the user's "not yet" does not let it
        re-offer the resolution on the very turn it was declined."""
        engine, case = await _deferred_offer(resolvable=True)
        result = await _turn(
            engine,
            case,
            _DEFLECTION,
            row=True,
            propose=model_proposes,
            follow_ups=_MODEL_FOLLOW_UPS,
        )
        assert case.offer_record.open is None
        assert set(_qualifying(case)) <= set(
            _declined(case)[-1].premise.confirmation_ids
        )
        assert _labels(result)[-1] == DECLINED_RESOLVE_CARD_LABEL

        case.current_turn += 1
        engine.generator.generate_structured_output = AsyncMock(
            return_value=_deferred_response()
        )
        await engine.process_turn(
            case=case, user_message="anything else?", redaction_ctx=None
        )
        assert case.offer_record.open is None

    @pytest.mark.parametrize("resolvable", [False, True])
    async def test_a_question_withdraws_a_deferred_offer_for_the_turn(self, resolvable):
        """A question is a user deciding, not declining: the offer is withdrawn
        unrecorded and must not be re-offered on the same turn (it used to be,
        by the deferred proposer, which never saw the withdrawal). It is back
        on the next turn."""
        engine, case = await _deferred_offer(resolvable=resolvable)
        flavour = "resolve" if resolvable else "close"
        case.current_turn += 1
        await engine.process_turn(
            case=case,
            user_message=f"what happens to the runbook if I {flavour} this?",
            redaction_ctx=None,
        )
        assert case.offer_record.open is None
        assert _declined(case) == []

        case.current_turn += 1
        await engine.process_turn(
            case=case, user_message="ok, makes sense", redaction_ctx=None
        )
        assert case.offer_record.open is not None, "the offer returns next turn"


class TestNothingReOffersWhileTheDeclineStands:
    async def test_step_zero_does_not_promote_a_needs_info_resolve_on_covered_rows(
        self,
    ):
        """F2: decline at {a}; a failed fix disqualifies a, so the model's
        resolve is a request (no offer, #2408); the refuting node is pruned and
        a qualifies again. Step 0's re-check of the request reads READY on the
        very rows the user declined, and opens nothing."""
        engine, case, _ = await _offered_then_declined()
        node_id = _refute(case, _qualifying(case))
        assert closure_verdict(case) != _SR
        await _turn(engine, case, "can we resolve it", propose="resolved")
        assert case.offer_record.open is None
        assert case.offer_record.resolve_request is not None

        del case.causal_nodes[node_id]
        result = await _turn(engine, case, "here is more context about the fix")
        assert case.offer_record.open is None
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        assert _labels(result)[-1] == DECLINED_RESOLVE_CARD_LABEL


class TestOnHold:
    """A resolve decline stands under a hold (a revision awaiting the user):
    nothing about the problem's status lapses it (ruling 3). The chip is off
    while the case is on hold, and a chip already on screen proposes
    nothing."""

    def test_no_chip_while_the_problem_is_on_hold(self):
        case = _confirmed_case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        assert project_offers(case).reopen_chip is not None, "control"
        case.progress.problem_status = ProblemStatus.REVISION_PENDING
        assert project_offers(case).reopen_chip is None

    async def test_the_chip_handler_proposes_nothing_while_on_hold(self):
        engine, case, _ = await _offered_then_declined()
        intent = _chip_intent(case)
        case.progress.problem_status = ProblemStatus.REVISION_PENDING
        result = await _turn(
            engine, case, "", intent_type="status_transition", intent_data=intent
        )
        assert case.offer_record.open is None
        assert "while its problem statement is in question" in result["agent_response"]

    def test_a_reopen_key_admits_only_a_transition_to_resolved(self):
        """Reachable in principle: the reopen path admits nothing but RESOLVED,
        whatever key a request to another state carries."""
        case = _confirmed_case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        key = project_offers(case).reopen_chip["intent"]["proposal_id"]
        assert resolve_reopen_admitted(case, "resolved", key)
        assert resolve_reopen_admitted(case, CaseState.RESOLVED, key)
        for other in ("closed", CaseState.CLOSED, "investigating"):
            assert not resolve_reopen_admitted(case, other, key)
