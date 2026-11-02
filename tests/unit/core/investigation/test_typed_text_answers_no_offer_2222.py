"""#2222: typed text answers no offer. Only a click or a whole-message bare
token does (ruling 2026-10-30).

The intent resolver minted the "Mark it resolved" chip from a typed decline
("I can't call it resolved yet.") and from a typed close request ("We're out
of time on this one — please close the case."), so a declined resolution was
proposed again and the close never happened. And the gate recorded a long
typed reply as a decline (#1808's deflection). Now:

* the resolver sets every offer's chip aside before either tier, so it mints
  none of them and the classifier is not called for them;
* a typed reply that is not one bare token never records a decline: a short
  one is re-asked (the re-ask names "Not yet"), a long one or a question is
  withdrawn and handed to the model, and the offer returns while still earned;
* every reply that carries "Mark it resolved" carries "Close without
  resolving", so a client with no status menu (Slack) can close.

The ruled copy is written out literally here, so a change to it fails a test.
Tests marked "kept-behaviour pin" pass on ``origin/main`` too; every other
#2222 assertion fails there. Real seams, a stubbed generator and router
doubles: no live LLM.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _investigation_confirmation_suggestions,
)
from faultmaven.core.investigation.milestone_engine.transition_turns import (
    _pending_transition_reask,
)
from faultmaven.core.investigation.milestone_engine.transitions import (
    DECLINED_RESOLVE_FEEDBACK,
)
from faultmaven.core.investigation.offer_projection import project_offers
from faultmaven.core.investigation.offers import (
    confirmation_pair,
    terminal_offer_withdrawn_this_turn,
)
from faultmaven.core.investigation.offers import (
    standing_resolve_decline as _standing_resolve_decline,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    RESOLVE_DECLINED_LINE,
    get_prompt_for_case,
)
from faultmaven.core.investigation.schemas import SuggestedFollowUp, TurnPayload
from faultmaven.modules.agent.domain.services.investigation_service.clarification import (
    _reclassification_intent,
)
from faultmaven.modules.agent.domain.services.out_of_band import OutOfBandTriage
from faultmaven.modules.case.contracts import AnswerVia, CaseState, ProblemStatus
from tests.case_builders import inquiry_case, with_declined_offer, with_open_offer
from tests.model_content import disabled_redaction
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _MODEL_FOLLOW_UPS,
    _declined,
    _engine,
    _feedback,
    _labels,
    _offered_then_declined,
    _respond,
    _service,
    _turn,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _case as _revision_case,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _engine as _revision_engine,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _propose,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)

pytestmark = pytest.mark.unit

#: The issue's two turns, typed under the "Mark it resolved" chip.
DECLINE_TEXT = "I can't call it resolved yet."
CLOSE_TEXT = "We're out of time on this one — please close the case."

#: The ruled copy (2026-11-02).
REASK = "Please select one of the options above to continue."
REASK_HOW = (
    "To confirm, click **Yes** or reply with the single word yes. "
    "To decline, click **Not yet** or reply with the single word no."
)
CLOSE_LABEL = "Close without resolving"
CLOSE_PAYLOAD = "Close this case without marking it resolved."
CLOSE_BODY = (
    "Close the case with the confirmed fix on record, not marked resolved. "
    "You confirm on the next step; closing is irreversible."
)
CLOSE_INTENT = {"type": "status_transition", "to_state": "closed"}
REOPEN_LABEL = "Mark it resolved"
RESOLVE_PAIR = ["Yes, mark as resolved", "Not yet, continue investigating"]


def _router(content: str = "1") -> MagicMock:
    """A classifier router double that would pick choice ``content``."""
    router = MagicMock()
    router.route = AsyncMock(
        return_value=SimpleNamespace(content=content, is_truncated=False)
    )
    return router


async def _resolve(router, text: str, choices: list):
    return await IntentResolver(router).resolve(
        text, choices, redaction_ctx=disabled_redaction()
    )


async def _offered():
    """A READY case; the engine's backstop offers the resolution on turn 1."""
    case = _confirmed_case()
    engine = _engine()
    await _turn(engine, case, "errors gone")
    assert case.offer_record.open.kind == "resolve", "premise: an open offer"
    return engine, case


# ---------------------------------------------------------------------------
# 1. The resolver answers no offer
# ---------------------------------------------------------------------------


class TestTheResolverAnswersNoOffer:
    @pytest.mark.parametrize("text", [DECLINE_TEXT, CLOSE_TEXT, REOPEN_LABEL])
    async def test_typed_text_never_mints_the_reopen_chip(self, text):
        _, case, _ = await _offered_then_declined()
        router = _router("1")
        assert await _resolve(router, text, [project_offers(case).reopen_chip]) is None
        router.route.assert_not_called()

    async def test_typed_text_never_mints_an_open_offers_pair(self):
        _, case = await _offered()
        pair = confirmation_pair(case.offer_record.open)
        router = _router("1")
        assert await _resolve(router, "Yes, mark as resolved", pair) is None
        assert await _resolve(router, DECLINE_TEXT, pair) is None
        router.route.assert_not_called()

    async def test_gate1_pair_is_still_minted(self):
        """Kept-behaviour pin: the bounded classifier stays for Gate 1 and
        clarification choices."""
        pair = _investigation_confirmation_suggestions(
            inquiry_case(statement="API returns 503 on /v1/users since 14:02")
        )
        router = _router("1")
        minted = await _resolve(router, "sounds right to me", pair)
        assert minted == pair[0]["intent"]
        router.route.assert_awaited_once()

    async def test_the_classifier_sees_only_the_choices_text_may_answer(self):
        _, case, _ = await _offered_then_declined()
        reclassify = {
            "label": "Treat it as logs",
            "action_type": "DECIDE",
            "payload": "Treat the file as logs.",
            "intent": _reclassification_intent("file_0123456789ab", "logs"),
        }
        router = _router("1")
        minted = await _resolve(
            router, "use the logs one", [project_offers(case).reopen_chip, reclassify]
        )
        assert minted == reclassify["intent"]
        prompt = str(router.route.await_args.kwargs["messages"][0]["content"])
        assert "1. Treat it as logs" in prompt
        assert REOPEN_LABEL not in prompt
        assert "2." not in prompt


# ---------------------------------------------------------------------------
# 2. Adoption: typed text under the chip reaches the model
# ---------------------------------------------------------------------------


class TestAdoptionUnderTheChip:
    @pytest.mark.parametrize("text", [DECLINE_TEXT, CLOSE_TEXT])
    async def test_typed_text_under_the_chip_reaches_the_model(self, text):
        """#2222 turns 11 and 13: the chip's intent is not minted, so no
        RESOLVED proposal opens and the decline still stands. The out-of-band
        triage double answers ``1`` (incident): the turn is triaged like any
        text turn (ruling 2026-11-02, no exemption)."""
        engine, case, _ = await _offered_then_declined()
        chip = project_offers(case).reopen_chip
        case.last_suggestions = [{**chip, "offered_turn": case.current_turn}]
        service = _service(engine)
        await service.repository.save(case)
        classifier = _router("1")
        service.intent_resolver = IntentResolver(classifier)
        service.out_of_band_triage = OutOfBandTriage(_router("1"))
        spy = AsyncMock(wraps=engine.process_turn)
        engine.process_turn = spy
        _respond(engine, follow_ups=_MODEL_FOLLOW_UPS)

        await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=TurnPayload(query=text),
        )

        classifier.route.assert_not_called()
        assert spy.await_args.kwargs["intent_type"] == "conversation"
        engine.generator.generate_structured_output.assert_awaited_once()
        assert case.offer_record.open is None
        assert case.state == CaseState.INVESTIGATING
        assert _standing_resolve_decline(case) is not None


# ---------------------------------------------------------------------------
# 3. The gate: only a click or a bare token answers
# ---------------------------------------------------------------------------


class TestOnlyAClickOrABareTokenAnswers:
    @pytest.mark.parametrize(
        "text",
        [CLOSE_TEXT, "We'll do it in Friday's change window, keep it open until then."],
    )
    async def test_a_long_typed_reply_is_withdrawn_and_returns(self, text):
        engine, case = await _offered()
        await _turn(engine, case, text)

        assert engine.generator.generate_structured_output.await_count == 1
        last = case.offer_record.answers[-1]
        assert (last.answer, last.via) == ("withdrawn", AnswerVia.TYPED)
        assert _declined(case) == []
        assert _standing_resolve_decline(case) is None
        assert case.offer_record.open is None

        await _turn(engine, case, "ok what next")
        assert case.offer_record.open.kind == "resolve", "it returns while earned"

    async def test_a_short_typed_reply_is_re_asked_naming_not_yet(self):
        engine, case = await _offered()
        offer, answers = case.offer_record.open, case.offer_record.answers
        result = await _turn(engine, case, DECLINE_TEXT)

        engine.generator.generate_structured_output.assert_not_awaited()
        paragraphs = result["agent_response"].split("\n\n")
        assert paragraphs[-2:] == [REASK, REASK_HOW]
        assert _labels(result) == RESOLVE_PAIR
        assert {f["intent"]["proposal_id"] for f in result["suggested_follow_ups"]} == {
            offer.proposal_id
        }
        assert case.offer_record.open == offer
        assert case.offer_record.answers == answers

    async def test_a_minted_decline_on_long_text_is_no_decline(self):
        engine, case = await _offered()
        await _turn(
            engine,
            case,
            "We'll do it in Friday's change window, keep it open until then.",
            intent_type="confirmation",
            intent_data={"value": False},
            typed=True,
        )
        assert _declined(case) == []
        assert case.offer_record.answers[-1].answer == "withdrawn"

    async def test_a_minted_confirmation_on_text_that_is_not_bare_is_re_asked(self):
        """Kept-behaviour pin."""
        engine, case = await _offered()
        offer = case.offer_record.open
        result = await _turn(
            engine,
            case,
            "that works",
            intent_type="confirmation",
            intent_data={"value": True},
            typed=True,
        )
        assert case.state == CaseState.INVESTIGATING
        assert case.offer_record.open == offer
        assert result["agent_response"].split("\n\n")[-2] == REASK

    async def test_a_bare_yes_confirms_whatever_was_minted(self):
        """Kept-behaviour pin: the bare token decides."""
        engine, case = await _offered()
        await _turn(
            engine,
            case,
            "yes",
            intent_type="confirmation",
            intent_data={"value": True},
            typed=True,
        )
        assert case.state == CaseState.RESOLVED
        assert case.turn_history[-1].terminal_confirmed_via == "explicit_token"


# ---------------------------------------------------------------------------
# 4. The revision gate (0b'): kept-behaviour pins
# ---------------------------------------------------------------------------


class TestTheRevisionGate:
    async def test_a_minted_confirmation_on_text_that_is_not_bare_commits_nothing(
        self,
    ):
        """Kept-behaviour pin."""
        engine, case = _revision_engine(), _revision_case()
        _propose(case)
        await engine.process_turn(
            case=case,
            user_message="Yes, use the revised statement",
            intent_type="confirmation",
            intent_data={"value": True},
            typed=True,
            redaction_ctx=None,
        )
        assert case.progress.problem_status == ProblemStatus.REVISION_PENDING

    async def test_a_bare_yes_commits(self):
        """Kept-behaviour pin."""
        engine, case = _revision_engine(), _revision_case()
        _propose(case)
        await engine.process_turn(case=case, user_message="yes", redaction_ctx=None)
        assert case.progress.problem_status == ProblemStatus.VERIFIED


# ---------------------------------------------------------------------------
# 5. The Close chip
# ---------------------------------------------------------------------------


class TestTheCloseChip:
    async def test_it_follows_mark_it_resolved_on_every_reply(self):
        engine, case, declined = await _offered_then_declined()
        assert _labels(declined)[-2:] == [REOPEN_LABEL, CLOSE_LABEL]
        assert declined["suggested_follow_ups"][-1] == {
            "label": CLOSE_LABEL,
            "action_type": "DECIDE",
            "payload": CLOSE_PAYLOAD,
            "body": CLOSE_BODY,
            "intent": CLOSE_INTENT,
        }

        later = await _turn(engine, case, "what else?", follow_ups=_MODEL_FOLLOW_UPS)
        assert _labels(later) == ["Share more context", REOPEN_LABEL, CLOSE_LABEL]

    async def test_not_yet_then_close_then_yes_closes_with_resolution_declined(self):
        """The Slack ruling: "Not yet" -> "Close without resolving" -> "Yes,
        close this case"."""
        engine, case, declined = await _offered_then_declined()
        chip = declined["suggested_follow_ups"][-1]
        assert chip["label"] == CLOSE_LABEL

        offered = await _turn(
            engine,
            case,
            chip["payload"],
            intent_type="status_transition",
            intent_data=dict(chip["intent"]),
        )
        assert case.offer_record.open.kind == "close"
        assert case.offer_record.open.closure_reason == "resolution_declined"
        assert _labels(offered)[0] == "Yes, close this case"

        await _turn(engine, case, "yes")
        assert case.state == CaseState.CLOSED
        assert case.closure_reason == "resolution_declined"

    async def test_absent_with_no_decline_and_while_an_offer_is_open(self):
        engine, case = await _offered()
        assert project_offers(case).close_chip is None
        await _turn(engine, case, "no")
        reopen = project_offers(case).reopen_chip
        assert project_offers(case).close_chip is not None, "control"
        reopened = await _turn(
            engine,
            case,
            reopen["payload"],
            intent_type="status_transition",
            intent_data=dict(reopen["intent"]),
        )
        assert case.offer_record.open.kind == "resolve"
        assert _labels(reopened) == RESOLVE_PAIR
        assert project_offers(case).close_chip is None

    @pytest.mark.parametrize(
        "status", [ProblemStatus.INVALIDATED, ProblemStatus.REVISION_PENDING]
    )
    def test_absent_while_the_problem_is_on_hold(self, status):
        case = _confirmed_case()
        with_declined_offer(case, kind="resolve", basis="backstop")
        assert project_offers(case).close_chip is not None, "control"
        case.progress.problem_status = status
        assert project_offers(case).close_chip is None

    async def test_a_model_card_with_its_label_leaves_exactly_one(self):
        engine, case, _ = await _offered_then_declined()
        result = await _turn(
            engine,
            case,
            "anything else?",
            follow_ups=[
                *_MODEL_FOLLOW_UPS,
                SuggestedFollowUp(label=CLOSE_LABEL, action_type="FREE_SPEECH"),
            ],
        )
        closes = [
            f for f in result["suggested_follow_ups"] if f["label"] == CLOSE_LABEL
        ]
        assert closes == [result["suggested_follow_ups"][-1]]
        assert closes[0]["intent"] == CLOSE_INTENT


# ---------------------------------------------------------------------------
# 6. The re-ask copy
# ---------------------------------------------------------------------------


class TestTheReaskCopy:
    @pytest.mark.parametrize("kind", ["resolve", "close"])
    def test_it_names_not_yet(self, kind):
        case = _confirmed_case()
        with_open_offer(case, kind=kind, basis="model", summary="Confirm?")
        text, pair = _pending_transition_reask(case)
        assert text.endswith(f"\n\n{REASK}\n\n{REASK_HOW}")
        assert len(pair) == 2


# ---------------------------------------------------------------------------
# 7. A withdrawn offer stays quiet for its turn (review F1)
# ---------------------------------------------------------------------------

#: The stand-down's feedback, as the model reads it.
STOOD_DOWN = "TRANSITION NOT PROPOSED: the user set the open offer aside"


class TestAWithdrawnOfferStaysQuietForItsTurn:
    @pytest.mark.parametrize(
        "text",
        [
            "No, keep it open - we will verify after the Friday maintenance "
            "window first.",
            "How do we know the fix held through the overnight batch?",
        ],
    )
    async def test_the_models_proposal_stands_down_then_the_offer_returns(self, text):
        engine, case = await _offered()
        first = case.offer_record.open.proposal_id
        result = await _turn(engine, case, text, propose="resolved")

        last = case.offer_record.answers[-1]
        assert (last.answer, last.via) == ("withdrawn", AnswerVia.TYPED)
        assert terminal_offer_withdrawn_this_turn(case)
        assert case.offer_record.open is None
        assert "Yes, mark as resolved" not in _labels(result)
        assert STOOD_DOWN in _feedback(result)

        await _turn(engine, case, "ok what next")
        assert case.offer_record.open.kind == "resolve", "still READY: it returns"
        assert case.offer_record.open.proposal_id != first

    async def test_a_bare_decline_is_still_refused_by_the_decline(self):
        engine, case = await _offered()
        result = await _turn(engine, case, "no", propose="resolved")

        assert not terminal_offer_withdrawn_this_turn(case)
        assert _declined(case)[-1].via == AnswerVia.TOKEN
        assert case.offer_record.open is None
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        assert STOOD_DOWN not in _feedback(result)


# ---------------------------------------------------------------------------
# 8. A bare answer to the open offer is the gate's (review F2)
# ---------------------------------------------------------------------------


def _live_reclassification(case) -> dict:
    """A carried clarification for the case's upload, live this turn."""
    upload = case.uploaded_files[0]
    return {
        "label": "Logs",
        "action_type": "DECIDE",
        "payload": f"Treat {upload.filename} as logs.",
        "body": "Treat the file as logs.",
        "intent": _reclassification_intent(upload.file_id, "logs"),
        "offered_turn": case.current_turn,
        "offered_data_type": upload.data_type,
    }


class TestABareAnswerIsTheGates:
    async def test_a_bare_yes_never_reaches_the_resolver(self):
        """With the pair filtered out, the resolver would classify "yes"
        against whatever else is live; the gate reads it instead."""
        engine, case = await _offered()
        case.last_suggestions = [
            *[
                {**c, "offered_turn": case.current_turn}
                for c in project_offers(case).pair
            ],
            _live_reclassification(case),
        ]
        router = _router("1")
        engine_turns = AsyncMock(wraps=engine.process_turn)
        engine.process_turn = engine_turns
        service = _service(engine)
        service.intent_resolver = IntentResolver(router)
        await service.repository.save(case)

        await service.process_turn(
            case_id=case.case_id, user_id=case.user_id, payload=TurnPayload(query="yes")
        )

        router.route.assert_not_called()
        assert engine_turns.await_args.kwargs["intent_type"] == "conversation"
        assert case.state == CaseState.RESOLVED

    async def test_other_text_still_reaches_the_resolver(self):
        """The negative control: a reply that is not a bare token is matched
        against the live clarification as before."""
        engine, case = await _offered()
        case.last_suggestions = [_live_reclassification(case)]
        router = _router("none")
        service = _service(engine)
        service.intent_resolver = IntentResolver(router)
        await service.repository.save(case)
        _respond(engine)

        await service.process_turn(
            case_id=case.case_id,
            user_id=case.user_id,
            payload=TurnPayload(query="Treat it as logs"),
        )

        router.route.assert_awaited_once()

    @pytest.mark.parametrize(
        "text, triaged", [("yes :+1: :skin-tone-2:", False), ("Treat it as logs", True)]
    )
    async def test_a_bare_consent_at_a_revision_skips_the_triage(self, text, triaged):
        """With no mint to keep it off the out-of-band lane, a decorated bare
        consent at a pending revision would meet the triage, and an aside
        verdict ("2") would skip 0b'."""
        engine, case = _revision_engine(), _revision_case()
        _propose(case)
        service = _service(engine)
        triage = AsyncMock(wraps=OutOfBandTriage(_router("2")).triage)
        service.out_of_band_triage.triage = triage
        await service.repository.save(case)

        await service.process_turn(
            case_id=case.case_id, user_id=case.user_id, payload=TurnPayload(query=text)
        )

        assert triage.await_count == (1 if triaged else 0)
        if not triaged:
            assert case.progress.problem_status == ProblemStatus.VERIFIED


# ---------------------------------------------------------------------------
# 9. The model is told about the Close chip (review F3)
# ---------------------------------------------------------------------------


def test_the_decline_prompt_and_feedback_name_the_close_chip():
    case = _confirmed_case()
    with_declined_offer(case, kind="resolve", basis="backstop")
    prompt = get_prompt_for_case(case, "what now?")
    assert RESOLVE_DECLINED_LINE in prompt
    assert "'Close without resolving'" in prompt
    assert "'Close without resolving'" in DECLINED_RESOLVE_FEEDBACK
