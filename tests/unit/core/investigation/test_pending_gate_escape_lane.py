"""The pending-transition gate must never swallow substantive input.

Regression for #656 (case_5db5417fe445, turns 12-13): with a pending CLOSE
proposal armed, typed messages that matched neither the confirm nor the
decline pattern list were answered with a canned re-present of the proposal
— no LLM call, no state change — indefinitely. The user's "I refuse to do
that. you must continue to investigate" and "what is the root cause?" were
swallowed every turn; the case was unrecoverable from the keyboard.

Contract pinned here (the escape lane):

- A message that is not a gate answer (longer than the substantive-length
  bound, or ANY second non-answer) withdraws the pending proposal and the
  turn proceeds to normal processing — the LLM seam is reached.
- A short ambiguous reply ("why?") re-presents the confirmation at most
  ONCE, then the next non-answer withdraws.
- A bare decline still gets the cheap canned acknowledgment; a decline
  carrying substance ("No. we did not do anything yet. …did you see
  anything wrong?") is processed normally after the cancel so its content
  is not lost.

The LLM seam is patched to raise a sentinel: reaching it proves the gate
fell through instead of bricking; not reaching it proves the deterministic
paths still short-circuit.
"""

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    MilestoneEngineError,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationProgress,
    ProblemVerification,
)

# The two messages that were swallowed in the live incident.
INCIDENT_REFUSAL = (
    "what are you talking about? you suggested me to prematurely resolve "
    "or close this case. I refuse to do that. you must continue to "
    "investigate into the issue."
)
INCIDENT_QUESTION = (
    "what is the root cause? you have not helped identified the root cause yet"
)


class _SeamReached(Exception):
    """Raised by the patched LLM seam — proves the gate fell through."""


def _make_repo():
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    return repo


def _engine():
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine._generate_structured_output = AsyncMock(side_effect=_SeamReached())
    return engine


def _investigating_case_with_pending_close(re_presented: bool = False) -> Case:
    case = Case(
        case_id="case_5db5417fe445",
        title="Escape-lane regression",
        state=CaseState.INQUIRY,
        user_id="user_test",
        enterprise_id="org_test",
        description="etcdInsufficientMembers alerts",
        problem_verification=ProblemVerification(
            symptom_statement="recurring etcdInsufficientMembers alerts",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "etcd connectivity"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.pending_transition = {
        "to_state": "closed",
        "summary": "You can **close** the case instead.",
        "evidence_ids": [],
        "proposed_at": datetime.now(UTC).isoformat(),
    }
    if re_presented:
        case.pending_transition["re_presented"] = True
    return case


async def _run_expecting_fall_through(engine, case, message):
    """The turn must reach the (sentinel-raising) LLM seam, with the
    pending proposal withdrawn before it. The engine wraps turn errors in
    MilestoneEngineError; the sentinel inside proves the seam was hit."""
    with pytest.raises(MilestoneEngineError):
        await engine.process_turn(case=case, user_message=message)
    assert case.pending_transition is None, (
        "the non-answer must withdraw the pending proposal before normal " "processing"
    )
    assert engine._generate_structured_output.called


@pytest.mark.asyncio
async def test_incident_refusal_withdraws_pending_and_reaches_llm():
    engine = _engine()
    case = _investigating_case_with_pending_close()
    await _run_expecting_fall_through(engine, case, INCIDENT_REFUSAL)


@pytest.mark.asyncio
async def test_incident_question_withdraws_pending_and_reaches_llm():
    engine = _engine()
    case = _investigating_case_with_pending_close()
    await _run_expecting_fall_through(engine, case, INCIDENT_QUESTION)


@pytest.mark.asyncio
async def test_short_ambiguous_reply_re_presents_once_without_llm():
    engine = _engine()
    case = _investigating_case_with_pending_close()

    result = await engine.process_turn(case=case, user_message="hmm maybe")

    assert "Please select one of the options above" in result["agent_response"]
    assert case.pending_transition is not None
    assert case.pending_transition.get("re_presented") is True
    assert not engine._generate_structured_output.called


@pytest.mark.asyncio
async def test_short_question_is_substantive_and_reaches_llm_first_time():
    """A question is never a gate answer, regardless of length — 'what is
    the rca?' must reach the LLM immediately, not get a canned bounce."""
    engine = _engine()
    case = _investigating_case_with_pending_close()
    await _run_expecting_fall_through(engine, case, "what is the rca?")


@pytest.mark.asyncio
async def test_confirm_prefixed_question_does_not_execute_transition():
    """Review finding: 'ok but what is the root cause?' starts with a
    confirm token but is substantive input, not consent — it must NOT
    execute the terminal transition; it reaches the LLM instead."""
    engine = _engine()
    case = _investigating_case_with_pending_close()

    await _run_expecting_fall_through(engine, case, "ok but what is the root cause?")
    assert case.state.value != "closed"


@pytest.mark.asyncio
async def test_confirm_word_prefix_does_not_confirm():
    """'yesterday...' must not read as 'yes' (word-boundary matching):
    it is a non-answer, so it gets the one re-present, not a close."""
    engine = _engine()
    case = _investigating_case_with_pending_close()

    result = await engine.process_turn(
        case=case, user_message="yesterday the pod restarted"
    )

    assert case.state.value != "closed"
    assert case.pending_transition is not None
    assert "Please select one of the options above" in result["agent_response"]


@pytest.mark.asyncio
async def test_decline_word_prefix_is_not_swallowed_as_bare_decline():
    """Review finding: 'note db latency spiked to 5s' must not read as a
    bare 'no' decline (canned ack, evidence dropped). It is a non-answer:
    re-presented once, then processed normally."""
    engine = _engine()
    case = _investigating_case_with_pending_close()

    result = await engine.process_turn(
        case=case, user_message="note db latency spiked to 5s"
    )

    assert "remains open" not in result["agent_response"]
    assert case.pending_transition is not None  # not cancelled by a fake decline
    assert case.pending_transition.get("re_presented") is True


@pytest.mark.asyncio
async def test_whitespace_only_message_never_reaches_llm():
    """Blank input (whitespace-only slips past the route's empty-payload
    guard) re-presents deterministically even after the re-present
    allowance is spent — it is never worth an LLM turn."""
    engine = _engine()
    case = _investigating_case_with_pending_close(re_presented=True)

    result = await engine.process_turn(case=case, user_message="   ")

    assert not engine._generate_structured_output.called
    assert case.pending_transition is not None
    assert "Please select one of the options above" in result["agent_response"]


class TestWithdrawalRecordsTheEngineOffer:
    """Every withdrawal path must record the refusal, not only the explicit
    "no".

    fm#1122 follow-up: a decline records the offer's ``justifying_signature``
    so the deferred-implementation proposer stops re-firing from unchanged
    state. The two OTHER paths that withdraw a standing offer — a deflection
    that is not a gate answer, and a contradicting status pick — cancelled it
    unrecorded. The deflection case is the sharper one: the turn falls through
    to normal processing, which reaches ``_maybe_propose_deferred_close``
    again in the SAME turn and re-takes the affordances the user just pushed
    away.
    """

    @staticmethod
    def _engine_proposed_case(signature: str = "SUGGEST_CLOSE|1|chain") -> Case:
        case = _investigating_case_with_pending_close()
        # Only the engine proposer writes this key; it is what a refusal is
        # recorded against.
        case.pending_transition["justifying_signature"] = signature
        return case

    @pytest.mark.asyncio
    async def test_long_non_question_deflection_records_the_refusal(self):
        """A deflection is not a "no", but it is not an acceptance either."""
        engine = _engine()
        case = self._engine_proposed_case()

        await _run_expecting_fall_through(
            engine,
            case,
            "We'll apply it in Friday's maintenance window and the on-call "
            "team will pick it up from there.",
        )

        assert case.progress.deferred_disposition_declined_signatures == [
            "SUGGEST_CLOSE|1|chain"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "question",
        [
            "What happens to the runbook if I close this?",
            "Which deployment revision would that close against?",
            "Sorry — what does closing actually do here?",
        ],
    )
    async def test_question_about_the_offer_is_not_a_refusal(self, question):
        """A question is a user DECIDING, not declining.

        ``message_is_substantive`` is true for any message containing "?" —
        the gate treats a question as "never a gate answer" regardless of
        length — so recording these as refusals would make the affordance
        vanish, unexplained, until a premise moved. That is the same
        engine-acts-without-saying-why defect this PR family exists to kill.
        The offer is withdrawn for this turn (the question gets answered) and
        is live again on the next one.
        """
        engine = _engine()
        case = self._engine_proposed_case()

        await _run_expecting_fall_through(engine, case, question)

        assert case.progress.deferred_disposition_declined_signatures == []

    @pytest.mark.asyncio
    async def test_second_non_answer_records_the_refusal(self):
        """The withdrawal on the second non-answer (the spent re-present
        allowance) goes through the same branch and must record too."""
        engine = _engine()
        case = self._engine_proposed_case()
        case.pending_transition["re_presented"] = True

        await _run_expecting_fall_through(engine, case, "hmm maybe")

        assert case.progress.deferred_disposition_declined_signatures == [
            "SUGGEST_CLOSE|1|chain"
        ]

    @pytest.mark.asyncio
    async def test_contradicting_status_pick_records_the_refusal(self):
        """Picking "Investigating" from the status dropdown while a close
        offer stands is a refusal of that offer."""
        engine = _engine()
        case = self._engine_proposed_case()

        with pytest.raises(MilestoneEngineError):
            await engine.process_turn(
                case=case,
                user_message="",
                intent_type="status_transition",
                intent_data={"to_state": "investigating"},
            )

        assert case.pending_transition is None
        assert case.progress.deferred_disposition_declined_signatures == [
            "SUGGEST_CLOSE|1|chain"
        ]

    @pytest.mark.asyncio
    async def test_withdrawal_of_another_proposers_offer_records_nothing(self):
        """An LLM- or user-initiated offer carries no signature, so declining
        it says nothing about the engine-initiated disposition."""
        engine = _engine()
        case = _investigating_case_with_pending_close()
        assert "justifying_signature" not in case.pending_transition

        await _run_expecting_fall_through(engine, case, INCIDENT_QUESTION)

        assert case.progress.deferred_disposition_declined_signatures == []


class TestGateAnswerMatchers:
    """Word-boundary + bare-confirmation contracts on the typed matchers."""

    def test_confirm_matcher_accepts_bare_confirmations(self):
        engine = _engine()
        for msg in ("yes", "ok", "yes, it's resolved, the error is gone"):
            assert engine._user_confirms_transition(msg), msg

    def test_confirm_matcher_rejects_substantive_or_prefix_matches(self):
        engine = _engine()
        for msg in (
            "ok but what is the root cause?",
            "yes, but first can you check the etcd disk latency?",
            "yesterday the pod restarted",
            "yes?",
        ):
            assert not engine._user_confirms_transition(msg), msg

    def test_decline_matcher_accepts_bare_declines(self):
        engine = _engine()
        for msg in ("no", "no.", "no way", "not yet", "nope!"):
            assert engine._user_declines_transition(msg), msg

    def test_decline_matcher_rejects_prefix_sharing_words(self):
        engine = _engine()
        for msg in (
            "note db latency spiked to 5s",
            "nothing in the logs",
            "stopped the pod",
        ):
            assert not engine._user_declines_transition(msg), msg


@pytest.mark.asyncio
async def test_second_non_answer_withdraws_even_when_short():
    engine = _engine()
    case = _investigating_case_with_pending_close(re_presented=True)
    await _run_expecting_fall_through(engine, case, "hm?")


@pytest.mark.asyncio
async def test_bare_decline_keeps_cheap_canned_acknowledgment():
    engine = _engine()
    case = _investigating_case_with_pending_close()

    result = await engine.process_turn(case=case, user_message="no")

    assert case.pending_transition is None
    assert "remains open" in result["agent_response"]
    assert not engine._generate_structured_output.called


@pytest.mark.asyncio
async def test_decline_with_substance_is_processed_normally():
    """The #656 turn-11-shaped message: starts with a decline token but
    carries the actual question — the content must reach the LLM."""
    engine = _engine()
    case = _investigating_case_with_pending_close()
    await _run_expecting_fall_through(
        engine,
        case,
        "No. we did not do anything yet. I showed the configmap without "
        "modification. did you see anything wrong?",
    )
