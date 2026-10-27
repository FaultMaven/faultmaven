"""#2352: an explicit typed decline of resolution binds (#1895) when it shares
a message with a question, and when it answers an offer that was withdrawn or
made in prose only. The backstop then does not re-propose on the same evidence.

Seen on ``case_f1aa690028ba``: "I can't call it resolved yet." was typed on
three turns, beside a question or after a withdrawal, and resolution was
offered again after each; the refusal list stayed empty.

Real turns with a stubbed model: no live LLM call.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    DECLINED_RESOLVE_CARD_LABEL,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    revision_pending,
)
from faultmaven.core.investigation.milestone_engine.transition_consent import (
    card_reads_as_bare_reply,
    declines_offer_explicitly,
    revision_offer_key,
)
from faultmaven.core.investigation.problem_status import propose_revision
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    TurnPayload,
)
from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    closure_verdict,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.agent.domain.services.out_of_band import (
    TRIAGE_MAX_TOKENS,
    OutOfBandTriage,
)
from faultmaven.modules.case.domain.models.evidence import EvidenceCategory
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _FILE_ID,
    _confirmed_case,
    _declined,
    _engine,
    _offered_then_declined,
    _respond,
    _turn,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import _case
from tests.unit.modules.agent.test_typed_text_mints_no_terminal_step_2222 import (
    _Store,
)

pytestmark = pytest.mark.unit

DECLINE = "I can't call it resolved yet."
QUESTION = (
    "What do you think the root cause is, what points to it, and what rules "
    "out the alternatives?"
)
ASIDE = 'You asked about "x" — I don\'t know that one.'

#: Against a pending RESOLVE, none of these declines: replies that confirm a
#: fix, ask for resolution, quote an error, or open with a decline token and go
#: on (#1813 ruling (a)).
NOT_A_DECLINE = [
    "Why isn't it resolved yet?",
    "No problem, mark it resolved.",
    "Yes, it's resolved.",
    "It looks resolved to me — can we close?",
    "I can't see any errors anymore, so I think it's resolved.",
    "We're out of time on this one — please close the case.",
    "Don't forget to add the runbook link before you resolve it.",
    "Not sure what else to check — the 503s are gone.",
    "The cert renewal didn't fix it on its own; the issuer change resolved it.",
    "I didn't touch the config, it resolved itself after the restart.",
    ASIDE,
    "Your call — which do you recommend?",
    "Yes, go ahead and resolve it. I can't think of anything else.",
    "How do we know it's fixed?",
    "Note the latency didn't resolve until 09:40.",
    "Note the latency didn't get resolved until 09:40.",
    "Please don't close the dashboard tab, resolve it when ready.",
    "The rollback didn't resolve it, the config change did.",
    "The first restart didn't resolve it; the second one did.",
    "It wasn't resolved by the restart but by the cert rotation.",
    "The previous ticket was never resolved, which is why we got here.",
    "That's not what resolved it.",
    "It never resolved on its own, we had to rotate the cert.",
    "no?",
    "No. Go ahead and resolve it.",
    "Nope. Mark it resolved.",
    "No. All clean for the last hour.",
    "Not seeing errors now so resolve it.",
    "Can't reproduce anymore so resolve it",
    "Don't forget to resolve it.",
    "There's no reason not to resolve it.",
    "Don't hesitate to resolve it.",
    "I'm not opposed to marking it resolved.",
    "Errors aren't back since we resolved it",
    "Not sure exactly when it resolved, but the errors are gone now.",
    "why isn't it resolved yet",
    "The rollback did not resolve it, the config change did.",
    "It was not resolved by the restart but by the cert rotation.",
    "The first restart did not resolve it; the second one did.",
    "host api.internal could not be resolved",
    "dependency not resolved",
    "No. we did not do anything yet. did you see anything wrong?",
    "No, keep it open.",
    "no, not yet",
    "No. Keep it open.",
]

#: Against a pending RESOLVE, each declines.
DECLINES = [
    DECLINE,
    f"Your call — which do you recommend?\n\n{DECLINE}",
    f"{ASIDE}\n\n{DECLINE}",
    "Not yet.",
    "Don't resolve it yet.",
    "It's not resolved — the errors are back.",
    "Please don't mark it resolved.",
    "I wouldn't call it resolved.",
    f"Can you explain the evidence? {DECLINE}",
    "no",
    "i cant call it resolved yet",
    "I can’t call it resolved yet.",
    f"OK, but {DECLINE}",
    f"Sure, but {DECLINE}",
    f"Can you explain？ {DECLINE}",
    f"Can you explain :question: {DECLINE}",
    "I can't call it\nresolved yet.",
    "Not resolved yet.",
    f"The AssumeRole errors are gone since the fix, but {DECLINE}",
    "Don't mark this case as resolved.",
]


@pytest.mark.parametrize("message", NOT_A_DECLINE)
def test_what_does_not_decline(message):
    assert declines_offer_explicitly(message, "resolved", bare_token=True) is False


@pytest.mark.parametrize("message", DECLINES)
def test_what_declines(message):
    assert declines_offer_explicitly(message, "resolved", bare_token=True) is True


def test_a_close_is_declined_by_its_own_constructions():
    assert declines_offer_explicitly("Don't close it yet.", "closed", bare_token=True)
    for message in ("I don't need anything else so close it", "Please close the case."):
        assert not declines_offer_explicitly(message, "closed", bare_token=True)
    assert not declines_offer_explicitly(
        "Don't close it yet.", "resolved", bare_token=True
    )


def test_a_bare_token_counts_only_as_the_whole_message_and_only_when_asked():
    """No offer pending (``bare_token=False``) a plain "no" answers whatever
    the model asked; at a gate it declines, but only as the whole reply. With
    no target (the revision gate) only the bare token counts."""
    assert not declines_offer_explicitly("no", "resolved", bare_token=False)
    assert not declines_offer_explicitly("Not yet.", "resolved", bare_token=False)
    assert declines_offer_explicitly(DECLINE, "resolved", bare_token=False)
    assert declines_offer_explicitly("Not yet.", None, bare_token=True)
    assert not declines_offer_explicitly("Not yet. Why?", None, bare_token=True)
    assert not declines_offer_explicitly(DECLINE, None, bare_token=True)


def test_a_long_paste_is_scanned_at_its_ends():
    log = "2026-10-27 12:00:00 ERROR AssumeRole failed. " * 2500
    assert declines_offer_explicitly(f"{log}{DECLINE}", "resolved", bare_token=False)
    assert declines_offer_explicitly(f"{DECLINE} {log}", "resolved", bare_token=False)


@pytest.mark.parametrize(
    "payload", [DECLINE, "Don't resolve it yet.", "Don't close it yet."]
)
def test_a_card_whose_payload_declines_reads_as_a_bare_reply(payload):
    """#1839: clicked later, it would decline the offer standing then."""
    assert card_reads_as_bare_reply(payload)


class TestADeclineBesideAQuestionBinds:
    @pytest.mark.parametrize("minted", [None, False])
    async def test_recorded_answered_and_not_re_offered(self, minted):
        """With a resolve offer pending, as the classifier minted it on the
        incident (``False``) or with no mint at all."""
        case = _confirmed_case()
        engine = _engine()
        await _turn(engine, case, "errors gone")
        assert case.pending_transition["to_state"] == "resolved"

        intent = (
            {}
            if minted is None
            else {
                "intent_type": "confirmation",
                "intent_data": {"value": minted},
                "typed": True,
            }
        )
        result = await _turn(
            engine, case, f"Can you explain the evidence? {DECLINE}", **intent
        )

        assert case.pending_transition is None
        assert _declined(case), "the decline was not recorded"
        engine.generator.generate_structured_output.assert_awaited_once()
        assert [f["label"] for f in result["suggested_follow_ups"]][-1] == (
            DECLINED_RESOLVE_CARD_LABEL
        )

        await _turn(engine, case, "ok, what next?")
        assert case.pending_transition is None

    async def test_a_pure_question_still_records_nothing(self):
        """#1840 holds: a question with no decline construction withdraws the
        offer for the turn and is not a refusal."""
        case = _confirmed_case()
        engine = _engine()
        await _turn(engine, case, "errors gone")
        await _turn(engine, case, QUESTION)
        assert case.pending_transition is None
        assert _declined(case) == []


class TestARepeatDeclineCoversWhatItsTurnRecords:
    async def test_a_row_recorded_from_the_decline_does_not_re_earn_the_offer(self):
        """The decline already stands, and the model records a confirmation row
        from the repeated "not yet" and proposes. The turn is marked even though
        nothing new is appended, so the row is covered and the proposal refused."""
        engine, case, _ = await _offered_then_declined()
        await _turn(engine, case, DECLINE, row=True, propose="resolved")
        assert case.pending_transition is None
        await _turn(engine, case, "ok, what next?")
        assert case.pending_transition is None


#: The row that makes ``_not_yet_ready()`` resolution-ready: the problem leg.
_PROBLEM_GONE = {
    "summary": "AssumeRole calls succeed again for the batch pods.",
    "extract": "0 failures since 02:00",
    "category": "symptom_absence_evidence",
    "source_type": "logs",
    "source_file_id": _FILE_ID,
}


def _not_yet_ready():
    """The cause observed gone, the problem leg still missing."""
    case = _case(absence=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    assert closure_verdict(case) != ClosureReadiness.SUGGEST_RESOLVE
    return case


def _model_records_the_problem_gone(engine, **state_updates) -> None:
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.",
            state_updates={"evidence_to_add": [dict(_PROBLEM_GONE)], **state_updates},
        )
    )


class TestADeclineSentWithTheConfirmationBinds:
    """Review item 5: the message that brings the confirmation also declines,
    so the case reads resolution-ready only after the model records it."""

    async def test_the_backstop_does_not_offer_on_that_turn(self):
        case = _not_yet_ready()
        engine = _engine()
        _model_records_the_problem_gone(engine)
        case.current_turn += 1
        await engine.process_turn(
            case=case,
            user_message=f"The batch ran clean overnight, but {DECLINE}",
            redaction_ctx=None,
        )
        assert closure_verdict(case) == ClosureReadiness.SUGGEST_RESOLVE
        assert case.pending_transition is None
        assert _declined(case)

    async def test_a_needs_info_offer_is_not_promoted_on_that_turn(self):
        """Review item 6: 0b skips a ``needs_info`` offer, so 0b'' reads it."""
        case = _not_yet_ready()
        engine = _engine()
        await _turn(engine, case, "the fix is in", propose="resolved")
        assert case.pending_transition["needs_info"] is True

        _model_records_the_problem_gone(engine)
        case.current_turn += 1
        await engine.process_turn(
            case=case,
            user_message=f"The batch ran clean overnight, but {DECLINE}",
            redaction_ctx=None,
        )
        assert closure_verdict(case) == ClosureReadiness.SUGGEST_RESOLVE
        assert case.pending_transition is None, "the offer was promoted"
        assert _declined(case)


class TestADeclineOfARevisionIsNotADeclineOfResolution:
    async def test_not_yet_to_a_revision_records_no_resolve_decline(self):
        """0b'' reads a revision turn by the decline constructions only, so
        "Not yet." declines the revision alone, on a case that reads
        resolvable again after."""
        case = _confirmed_case()
        propose_revision(
            case,
            text="The audience mismatch only affects the batch pods.",
            evidence_ids=[],
            basis="the web pods were never affected",
            offer_key=revision_offer_key("x"),
        )
        engine = _engine()
        await _turn(engine, case, "Not yet.")
        assert not revision_pending(case), "premise: the revision was declined"
        assert closure_verdict(case) == ClosureReadiness.SUGGEST_RESOLVE
        assert _declined(case) == []


def _service(store, *, triage_router=None):
    engine = MilestoneEngine(MagicMock(), store, investigation_tools=MagicMock())
    _respond(engine)
    svc = InvestigationService(engine, store)
    classifier = MagicMock()
    # "2": the "Not yet" card, as the classifier answered on the incident.
    classifier.route = AsyncMock(return_value=SimpleNamespace(content="2"))
    svc.intent_resolver = IntentResolver(classifier)
    if triage_router is None:
        svc.out_of_band_triage.triage = AsyncMock(return_value=None)
    else:
        engine.deps.llm_provider = triage_router
        svc.out_of_band_triage = OutOfBandTriage(triage_router)
    return svc, engine


async def _service_turn(svc, store, query):
    return await svc.process_turn(
        case_id=store.case_id,
        user_id=store.row().user_id,
        payload=TurnPayload(query=query),
    )


class TestADeclineAfterAWithdrawalBinds:
    async def test_recorded_and_the_backstop_holds_on_that_turn(self):
        """Turn 1 the backstop offers; turn 2 a pure question withdraws the
        offer, and the model's prose asks again; turn 3 the user types the
        decline with no offer pending. Through ``InvestigationService.
        process_turn``, so the decline meets the resolver as on the incident;
        the triage is stubbed to "not an aside" (the next test drives it)."""
        store = _Store(_confirmed_case())
        svc, engine = _service(store)

        await _service_turn(svc, store, "errors gone")
        assert store.row().pending_transition["to_state"] == "resolved"
        _respond(engine, narration="Would you like me to resolve the case?")
        await _service_turn(svc, store, QUESTION)
        assert store.row().pending_transition is None
        assert _declined(store.row()) == [], "#1840: a question is not a refusal"

        _respond(engine)
        response = await _service_turn(svc, store, DECLINE)

        row = store.row()
        assert row.pending_transition is None
        assert _declined(row), "the decline was not recorded"
        assert [a.label for a in response.suggested_actions] == [
            DECLINED_RESOLVE_CARD_LABEL
        ]

    async def test_a_triage_that_would_call_it_an_aside_is_not_asked(self):
        """Review item 8: with no offer pending the out-of-band triage runs,
        and an aside verdict answers off the case and records nothing. The
        decline is exempt, as #1841 exempts Gate 1's bare consent; this triage
        answers "aside" to anything it is asked."""
        triage = MagicMock()

        async def route(**kwargs):
            aside = kwargs.get("max_tokens") == TRIAGE_MAX_TOKENS
            return SimpleNamespace(content="2" if aside else "An aside answer.")

        triage.route = AsyncMock(side_effect=route)
        store = _Store(_confirmed_case())
        svc, engine = _service(store, triage_router=triage)

        response = await _service_turn(svc, store, DECLINE)

        engine.generator.generate_structured_output.assert_awaited_once()
        assert _declined(store.row()), "the decline was not recorded"
        assert [a.label for a in response.suggested_actions] == [
            DECLINED_RESOLVE_CARD_LABEL
        ]
        assert not any(
            c.kwargs.get("max_tokens") == TRIAGE_MAX_TOKENS
            for c in triage.route.call_args_list
        )
