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
from faultmaven.modules.case.domain.models import (
    CaseState,
    TurnOutcome,
    TurnProgress,
)

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
    through ``MilestoneEngine._process_terminal_turn``, which returns before the
    engine's turn bookkeeping. Every one of those turns consumed a number and
    recorded none, so a resolved case's counter froze indefinitely. Corpus
    evidence: one resolved case has THREE user turns all stamped turn 9 — a
    regen, a second regen, and a runbook request.

    Driven through the REAL engine method, not a hand-written stub of its shape.
    Stubbing ``engine.process_turn`` would leave ``_process_terminal_turn``
    unentered and the terminal branch unexercised, so a regression that made the
    real short-circuit record a turn (or stop consuming one) would keep the test
    green — which is the whole failure this class exists to catch.
    """

    async def test_a_real_terminal_case_advances_its_counter(
        self, recording_case_repository, sample_case
    ):
        from unittest.mock import AsyncMock, MagicMock

        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        case = sample_case
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
        case.inquiry.decided_to_investigate = True
        case.inquiry.decision_made_at = datetime.now(timezone.utc)
        case.state = CaseState.INVESTIGATING
        # A recorded history, so ``effective_current_turn`` reads the tail rather
        # than falling back to ``current_turn`` — without it the projection is a
        # no-op and this test cannot express the defect at all.
        case.turn_history = [
            TurnProgress(
                turn_number=1, progress_made=True, outcome=TurnOutcome.CONVERSATION
            )
        ]
        case.current_turn = 1
        # Both at once: the model refuses ``resolved_at`` outside RESOLVED and
        # refuses RESOLVED without it, so neither assignment can come first.
        case = case.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": datetime.now(timezone.utc),
                "closed_at": datetime.now(timezone.utc),
            }
        )
        assert case.is_terminal, "the fixture must actually be terminal"

        # A real engine, with only the LLM boundary doubled: the terminal Q&A
        # path calls the provider and nothing else that matters here.
        import asyncio
        from collections import defaultdict

        engine = MilestoneEngine.__new__(MilestoneEngine)
        engine.llm_provider = MagicMock()
        engine.repository = recording_case_repository
        engine._case_locks = defaultdict(asyncio.Lock)

        # Returns the case it was HANDED, not a closure over the outer one:
        # the service increments the reloaded object, and returning the stale
        # local would hide the very counter movement under test.
        async def answer(case_arg, *_args, **_kwargs):
            return {
                "case_updated": case_arg,
                "agent_response": "That case is resolved.",
                "metadata": {"milestones_completed": [], "progress_made": False},
            }

        answered = AsyncMock(side_effect=answer)
        engine._process_terminal_qa = answered

        service = InvestigationService(
            milestone_engine=engine, case_repository=recording_case_repository
        )
        await recording_case_repository.save(case)

        for q in ["what was the root cause?", "and the fix?", "anything else?"]:
            await _turn(service, case, q)

        assert answered.await_count == 3, "the real terminal Q&A path did not run"
        saved = await recording_case_repository.get(case.case_id)
        assert saved.current_turn == 4, (
            "three terminal turns consumed three numbers; the counter must "
            "advance or they all stamp the same turn (corpus: all stamped 9)"
        )
        numbers = [m["turn_number"] for m in saved.messages if m.get("role") == "user"]
        assert numbers == sorted(set(numbers)), f"turn numbers repeat: {numbers}"


class TestTheRecordDoesNotDestroyWhatTurnHistoryFeeds:
    """``turn_history`` is not only a counter.

    ``prompts/context_builder`` renders it as the prompt's EARLIER TURNS block
    and reads ``[-1].system_feedback``; ``working_conclusion_generator`` and
    ``progress_monitor`` window it. Adding a MINIMAL entry is therefore not a
    harmless placeholder — it displaces the tail and replaces real text. Each
    test below fails against the first version of this fix, which recorded an
    empty record.
    """

    def test_it_forwards_unconsumed_system_feedback(self, sample_case):
        """Feedback is read off ``turn_history[-1]`` and is meant for the NEXT
        prompt. These routes build no prompt, so they have not consumed it —
        dropping it silently swallows a reasoning-validation error whenever a
        greeting lands between two engine turns."""
        from faultmaven.modules.case.domain.models import TurnOutcome, TurnProgress

        case = sample_case
        case.turn_history = [
            TurnProgress(
                turn_number=1,
                progress_made=True,
                outcome=TurnOutcome.CONVERSATION,
                system_feedback="REASONING VALIDATION: provide milestone_justifications.",
            )
        ]
        case.current_turn = 2

        _backfill_consumed_turn(
            case, user_message="hi", agent_response="hello", metadata={}
        )

        assert case.turn_history[-1].system_feedback == (
            "REASONING VALIDATION: provide milestone_justifications."
        ), "the greeting swallowed feedback the next engine turn still needs"

    def test_it_records_the_real_text(self, sample_case):
        """``_build_graduated_history`` renders from the record when one exists
        and falls back to the message text only when it is MISSING. An empty
        record does not leave the text alone — it replaces it."""
        case = sample_case
        case.turn_history = []
        case.current_turn = 1

        _backfill_consumed_turn(
            case,
            user_message="treat the mystery file as application logs",
            agent_response="Got it — reclassified.",
            metadata={},
        )

        recorded = case.turn_history[-1]
        assert "mystery file" in recorded.user_message_summary
        assert "reclassified" in recorded.agent_response_summary

    def test_a_novel_upload_on_a_bypass_route_counts_as_progress(self, sample_case):
        """The turns route accepts an intent alongside files, so a clarification
        click can carry a genuinely novel upload. ``_finish_deterministic_turn``
        is explicit that such an upload counts; hardcoding ``False`` would report
        an inert turn on a turn the user supplied new data — and would leave the
        stall counter climbing through it."""
        case = sample_case
        case.turn_history = []
        case.current_turn = 1
        case.turns_without_progress = 4

        _backfill_consumed_turn(
            case,
            user_message="treat it as logs",
            agent_response="done",
            metadata={"novel_files_uploaded": ["file_0123456789ab"]},
        )

        assert case.turn_history[-1].progress_made is True
        assert case.turns_without_progress == 0

    def test_the_predicate_is_the_engine_s_own(self):
        """Scored with the same function the engine's deterministic branches
        use, so a progress arm added there lands here too rather than needing a
        second edit nobody remembers to make."""
        import inspect

        from faultmaven.core.investigation.milestone_engine import (
            check_if_progress_made,
        )
        from faultmaven.modules.agent.domain.services import investigation_service

        assert "check_if_progress_made" in inspect.getsource(
            investigation_service._backfill_consumed_turn
        )
        assert check_if_progress_made({"novel_files_uploaded": ["f"]}) is True


class TestTheBackfilledTurnIsHonest:
    def test_it_claims_no_progress(self, sample_case):
        """``progress_made`` feeds ``turns_without_progress``. A greeting that
        reset the stall counter would hide exactly the stalls that counter
        exists to surface — so the backfilled turn must claim nothing."""
        case = sample_case
        case.current_turn = 4
        case.turn_history = []

        _backfill_consumed_turn(
            case, user_message="hi", agent_response="hello", metadata={}
        )

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

        _backfill_consumed_turn(
            case, user_message="hi", agent_response="hello", metadata={}
        )

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
