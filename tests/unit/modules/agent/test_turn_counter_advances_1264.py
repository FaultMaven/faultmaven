"""#1264 — every consumed turn records one, so the persisted counter advances.

Both repositories persist ``Case.effective_current_turn`` (the last
``turn_history`` number), not the in-flight ``current_turn``. That is #500's
prevention half and it is still right: it stops the stored counter running ahead
of the history, which is what let one interrupted turn permanently wedge a case.

The consequence, before this fix: a route that consumed a turn number WITHOUT
recording one froze the persisted counter. ``process_turn`` reloads the case on
every request and derives ``next_turn`` from that column, so the very next turn
re-derived the number just used — no process boundary needed. Measured on the
corpus: 7 cases carried a ``(case_id, turn_number)`` pair with two user messages,
and one resolved case had three user turns all stamped turn 9.

**These tests use the recording doubles on purpose.** ``MockCaseRepository``
stores the ``Case`` as handed to it and never applies the
``effective_current_turn`` projection, and ``MockMilestoneEngine`` records no
turn — so on the plain doubles the two counters cannot diverge and every
assertion below passes whether or not the fix is present. That blindness is why
this defect survived.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
    _backfill_consumed_turn,
)
from faultmaven.modules.case.domain.models import CaseState

pytestmark = pytest.mark.unit


@pytest.fixture
def wired(recording_case_repository, recording_milestone_engine, sample_case):
    case = sample_case
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(timezone.utc)
    case.state = CaseState.INVESTIGATING
    service = InvestigationService(
        milestone_engine=recording_milestone_engine,
        case_repository=recording_case_repository,
    )
    return service, recording_case_repository, case


async def _turn(service, case, query, intent=None):
    await service.process_turn(
        case_id=case.case_id,
        user_id=case.user_id,
        payload=TurnPayload(query=query, intent=intent),
    )


class TestTheCounterAdvancesOnEveryRoute:
    async def test_a_greeting_does_not_freeze_the_persisted_counter(self, wired):
        """``GREETING`` is answered from a static string without ever calling
        the engine, so before this fix it recorded no turn and the persisted
        counter stood still — and the NEXT turn reused its number."""
        service, repo, case = wired
        await repo.save(case)

        await _turn(service, case, "what is happening?")
        assert (await repo.get(case.case_id)).current_turn == 1

        await _turn(service, case, "hi")
        assert (await repo.get(case.case_id)).current_turn == 2, (
            "a greeting consumed a turn number; the persisted counter must "
            "advance with it or the next turn reuses the number"
        )

        await _turn(service, case, "and now?")
        assert (await repo.get(case.case_id)).current_turn == 3

    async def test_turn_numbers_never_repeat_across_a_greeting(self, wired):
        """The user-visible consequence: a repeated number makes per-turn
        grouping of a transcript unfaithful."""
        service, repo, case = wired
        await repo.save(case)

        for q in ["what is happening?", "hi", "and now?", "hello", "so?"]:
            await _turn(service, case, q)

        saved = await repo.get(case.case_id)
        numbers = [m["turn_number"] for m in saved.messages if m.get("role") == "user"]
        assert numbers == sorted(set(numbers)), f"turn numbers repeat: {numbers}"
        assert saved.current_turn == 5

    async def test_the_history_stays_consecutive(self, wired):
        """The property #500 added ``effective_current_turn`` to protect: the
        stored counter never runs ahead of ``turn_history``. A backfilled turn
        must keep the sequence consecutive, not punch a hole in it."""
        service, repo, case = wired
        await repo.save(case)

        for q in ["what is happening?", "hi", "and now?"]:
            await _turn(service, case, q)

        saved = await repo.get(case.case_id)
        numbers = [t.turn_number for t in saved.turn_history]
        assert numbers == list(range(1, len(numbers) + 1)), numbers
        assert saved.effective_current_turn == saved.current_turn


class TestTheTerminalShortCircuit:
    """The route the issue does not mention, and the worst of the three.

    A terminal case answers Q&A, report regeneration and runbook creation
    through ``_process_terminal_turn``, which returns before the engine's turn
    bookkeeping. Every one of those turns consumed a number and recorded none,
    so a resolved case's counter froze indefinitely. Corpus evidence: one
    resolved case has THREE user turns all stamped turn 9 — a regen, a second
    regen, and a runbook request.
    """

    async def test_a_result_that_records_no_turn_still_advances_the_counter(
        self, recording_case_repository, recording_milestone_engine, sample_case
    ):
        from unittest.mock import AsyncMock

        case = sample_case
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
        case.inquiry.decided_to_investigate = True
        case.inquiry.decision_made_at = datetime.now(timezone.utc)
        case.state = CaseState.INVESTIGATING

        # The terminal short-circuit's shape: answers, records nothing.
        async def terminal_shaped(*, case, **_kwargs):
            case.updated_at = datetime.now(timezone.utc)
            return {
                "case_updated": case,
                "agent_response": "that case is closed",
                "metadata": {"milestones_completed": [], "progress_made": False},
            }

        service = InvestigationService(
            milestone_engine=recording_milestone_engine,
            case_repository=recording_case_repository,
        )
        await recording_case_repository.save(case)

        # Two real investigative turns FIRST. Without them ``turn_history`` is
        # empty, ``effective_current_turn`` falls back to ``current_turn``, the
        # projection is a no-op, and this test passes with or without the fix —
        # verified. The corpus shape is 8 recorded turns and THEN terminal turns
        # that record none, so the history must be non-empty for the defect to
        # exist at all.
        await _turn(service, case, "what is happening?")
        await _turn(service, case, "and now?")
        assert (await recording_case_repository.get(case.case_id)).current_turn == 2

        recording_milestone_engine.process_turn = AsyncMock(side_effect=terminal_shaped)

        for q in [
            "please regenerate the report",
            "regenerate it again",
            "generate a runbook from this case",
        ]:
            await _turn(service, case, q)

        saved = await recording_case_repository.get(case.case_id)
        assert saved.current_turn == 5, (
            "three terminal turns consumed three numbers; the counter must "
            "advance or all three stamp the same turn (corpus: all stamped 9)"
        )
        numbers = [m["turn_number"] for m in saved.messages if m.get("role") == "user"]
        assert numbers == sorted(set(numbers)), f"turn numbers repeat: {numbers}"


class TestAClarificationClickCostsAWindowTurn:
    """A deliberate, reversible semantics change — pinned so it is visible.

    ``CLARIFICATION_CARRY_TURNS = 3`` bounds how long an unanswered
    classification question stays answerable. Before #1264 a clarification click
    consumed a turn number but recorded none, so it did not advance the clock
    the window is measured on: the effective reach was "3 engine turns, plus
    unlimited clarification clicks". Now it is 3 turns of any kind.

    That is the constant meaning what it says, and it is the direction #1264
    argues for ("a counter that does not move does not decay"). But it IS a
    behaviour change to #1263's recovery path, and the product question — should
    answering question A spend a turn of question B's window? — is the owner's,
    not this lane's. If the old reach is wanted, the lever is
    ``CLARIFICATION_CARRY_TURNS``, not the turn clock.

    Pinned here rather than left implicit in #1263's scenario arithmetic so that
    reversing it is a one-line decision against a named test.
    """

    async def test_a_non_engine_turn_advances_the_window_clock(self, wired):
        from faultmaven.core.investigation.suggestion_liveness import (
            CLARIFICATION_CARRY_TURNS,
        )

        assert CLARIFICATION_CARRY_TURNS == 3
        service, repo, case = wired
        await repo.save(case)

        await _turn(service, case, "what is happening?")
        before = (await repo.get(case.case_id)).current_turn

        await _turn(service, case, "hi")

        after = (await repo.get(case.case_id)).current_turn
        assert after == before + 1, (
            "a greeting must advance the clock every turn-aged window is "
            "measured on — clarification carry, hypothesis decay, evidence-need "
            "ages. Before #1264 it did not, so those windows silently reached "
            "further than their constants say."
        )


class TestTheBackfilledTurnIsHonest:
    def test_it_claims_no_progress(self, sample_case):
        """``progress_made`` feeds ``turns_without_progress``. A greeting that
        reset the stall counter would hide exactly the stalls that counter
        exists to surface — so the backfilled turn must claim nothing."""
        case = sample_case
        case.current_turn = 4
        case.turn_history = []

        _backfill_consumed_turn(case)

        recorded = case.turn_history[-1]
        assert recorded.turn_number == 4
        assert recorded.progress_made is False
        assert recorded.milestones_completed == []
        assert recorded.evidence_added == []
        assert recorded.hypotheses_generated == []
        assert recorded.solutions_proposed == []

    def test_it_is_a_no_op_when_the_turn_was_already_recorded(self, sample_case):
        """Every route that reaches the engine's bookkeeping already records.
        Appending a second entry for the same turn would break the consecutive
        invariant the repositories depend on."""
        from faultmaven.modules.case.domain.models import TurnOutcome, TurnProgress

        case = sample_case
        case.current_turn = 2
        case.turn_history = [
            TurnProgress(
                turn_number=1, progress_made=True, outcome=TurnOutcome.CONVERSATION
            ),
            TurnProgress(
                turn_number=2, progress_made=True, outcome=TurnOutcome.CONVERSATION
            ),
        ]

        _backfill_consumed_turn(case)

        assert [t.turn_number for t in case.turn_history] == [1, 2]
        assert case.turn_history[-1].progress_made is True, "overwrote a real turn"


class TestTheDoublesCanActuallyExpressTheDefect:
    """A guard on the guard.

    The plain doubles cannot fail on this defect — ``MockCaseRepository`` skips
    the projection and ``MockMilestoneEngine`` records nothing — so a future
    edit that quietly swaps the recording doubles back would leave every test
    above green and meaningless.
    """

    async def test_the_recording_repository_applies_the_projection(
        self, recording_case_repository, sample_case
    ):
        from faultmaven.modules.case.domain.models import TurnOutcome, TurnProgress

        case = sample_case
        case.current_turn = 7
        case.turn_history = [
            TurnProgress(
                turn_number=1, progress_made=True, outcome=TurnOutcome.CONVERSATION
            )
        ]

        await recording_case_repository.save(case)

        stored = await recording_case_repository.get(case.case_id)
        assert stored.current_turn == 1, (
            "the double must store effective_current_turn, as both real "
            "repositories do — otherwise it cannot express #1264 at all"
        )

    async def test_the_plain_repository_cannot(self, mock_case_repository, sample_case):
        """Stated as a fact about the plain double, so the reason these tests
        use the recording one is written down rather than folklore."""
        from faultmaven.modules.case.domain.models import TurnOutcome, TurnProgress

        case = sample_case
        case.current_turn = 7
        case.turn_history = [
            TurnProgress(
                turn_number=1, progress_made=True, outcome=TurnOutcome.CONVERSATION
            )
        ]

        await mock_case_repository.save(case)

        stored = await mock_case_repository.get(case.case_id)
        assert stored.current_turn == 7
