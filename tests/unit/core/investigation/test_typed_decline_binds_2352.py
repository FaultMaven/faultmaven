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
    declines_offer_explicitly,
    revision_offer_key,
)
from faultmaven.core.investigation.problem_status import propose_revision
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    closure_verdict,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _confirmed_case,
    _declined,
    _engine,
    _offered_then_declined,
    _respond,
    _turn,
)
from tests.unit.modules.agent.test_typed_text_mints_no_terminal_step_2222 import (
    _Store,
)

pytestmark = pytest.mark.unit

DECLINE = "I can't call it resolved yet."
QUESTION = (
    "What do you think the root cause is, what points to it, and what rules "
    "out the alternatives?"
)
ASIDE = "You asked about what remains uncertain — I don't know that one."

#: (message, target): none of these declines.
NOT_A_DECLINE = [
    ("Why isn't it resolved yet?", "resolved"),
    ("No problem, mark it resolved.", "resolved"),
    ("Yes, it's resolved.", "resolved"),
    ("It looks resolved to me — can we close?", "resolved"),
    ("I can't see any errors anymore, so I think it's resolved.", "resolved"),
    ("We're out of time on this one — please close the case.", "resolved"),
    ("We're out of time on this one — please close the case.", "closed"),
    ("Don't forget to add the runbook link before you resolve it.", "resolved"),
    ("Not sure what else to check — the 503s are gone.", "resolved"),
    (QUESTION, "resolved"),
    (
        "The cert renewal didn't fix it on its own; the issuer change resolved it.",
        "resolved",
    ),
    ("I didn't touch the config, it resolved itself after the restart.", "resolved"),
    (ASIDE, "resolved"),
    ("Your call — which do you recommend?", "resolved"),
    ("Yes, go ahead and resolve it. I can't think of anything else.", "resolved"),
    ("How do we know it's fixed?", "resolved"),
    ("Note the latency didn't resolve until 09:40.", "resolved"),
    ("Note the latency didn't get resolved until 09:40.", "resolved"),
    ("Please don't close the dashboard tab, resolve it when ready.", "resolved"),
    ("Don't close it yet.", "resolved"),
    ("The rollback didn't resolve it, the config change did.", "resolved"),
    ("The first restart didn't resolve it; the second one did.", "resolved"),
    ("It wasn't resolved by the restart but by the cert rotation.", "resolved"),
    ("The previous ticket was never resolved, which is why we got here.", "resolved"),
    ("That's not what resolved it.", "resolved"),
    ("It never resolved on its own, we had to rotate the cert.", "resolved"),
    ("no?", "resolved"),
]

#: (message, target): each declines.
DECLINES = [
    (DECLINE, "resolved"),
    (f"Your call — which do you recommend?\n\n{DECLINE}", "resolved"),
    (f"{ASIDE}\n\n{DECLINE}", "resolved"),
    ("Not yet.", "resolved"),
    ("Don't resolve it yet.", "resolved"),
    ("It's not resolved — the errors are back.", "resolved"),
    ("Please don't mark it resolved.", "resolved"),
    ("I wouldn't call it resolved.", "resolved"),
    (f"Can you explain the evidence? {DECLINE}", "resolved"),
    ("Don't close it yet.", "closed"),
    ("no", "resolved"),
    ("i cant call it resolved yet", "resolved"),
    ("No. Keep it open.", "resolved"),
    ("I can’t call it resolved yet.", "resolved"),
]


@pytest.mark.parametrize("message, target", NOT_A_DECLINE)
def test_what_does_not_decline(message, target):
    assert declines_offer_explicitly(message, target) is False


@pytest.mark.parametrize("message, target", DECLINES)
def test_what_declines(message, target):
    assert declines_offer_explicitly(message, target) is True


def test_with_no_target_only_a_bare_decline_sentence_counts():
    """The revision gate reads with no target."""
    assert declines_offer_explicitly("Not yet. Why?", None) is True
    assert declines_offer_explicitly(DECLINE, None) is False


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
        """#1840 holds: a question with no declining sentence withdraws the
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


class TestADeclineOfARevisionIsNotADeclineOfResolution:
    async def test_not_yet_to_a_revision_records_no_resolve_decline(self):
        """The no-offer arm reads only a turn that began with no offer: a
        revised statement awaiting re-confirmation is one, and "Not yet."
        declines it alone, on a case that reads resolvable again after."""
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


class TestADeclineAfterAWithdrawalBinds:
    async def test_recorded_and_the_backstop_holds_on_that_turn(self):
        """Turn 1 the backstop offers; turn 2 a pure question withdraws the
        offer, and the model's prose asks again; turn 3 the user types the
        decline with no offer pending. Through ``InvestigationService.
        process_turn``, so the decline meets the resolver and the out-of-band
        triage as it did on the incident; the triage is stubbed to "not an
        aside", which is what the incident's log shows for that turn."""
        store = _Store(_confirmed_case())
        engine = MilestoneEngine(MagicMock(), store, investigation_tools=MagicMock())
        _respond(engine)
        svc = InvestigationService(engine, store)
        classifier = MagicMock()
        # "2": the "Not yet" card, as the classifier answered on the incident.
        classifier.route = AsyncMock(return_value=SimpleNamespace(content="2"))
        svc.intent_resolver = IntentResolver(classifier)
        svc.out_of_band_triage.triage = AsyncMock(return_value=None)

        async def turn(query):
            return await svc.process_turn(
                case_id=store.case_id,
                user_id=store.row().user_id,
                payload=TurnPayload(query=query),
            )

        await turn("errors gone")
        assert store.row().pending_transition["to_state"] == "resolved"
        _respond(engine, narration="Would you like me to resolve the case?")
        await turn(QUESTION)
        assert store.row().pending_transition is None
        assert _declined(store.row()) == [], "#1840: a question is not a refusal"

        _respond(engine)
        response = await turn(DECLINE)

        row = store.row()
        assert row.pending_transition is None
        assert _declined(row), "the decline was not recorded"
        assert [a.label for a in response.suggested_actions] == [
            DECLINED_RESOLVE_CARD_LABEL
        ]
