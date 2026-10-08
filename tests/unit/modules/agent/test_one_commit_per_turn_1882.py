"""#1882 — a turn commits once, at the end, or not at all.

The invariant (owner ruling): a 2xx turn response means everything the turn
wrote is committed; a non-2xx means none of it is. The only writes a failed turn
may leave are the ones whose truth does not depend on the turn committing.

Driven through the REAL ``MilestoneEngine`` (its LLM seam stubbed, no provider
call) and the REAL ``InvestigationService`` on the REAL ``SQLiteCaseRepository``
over a file database, read back through a SEPARATE session, which sees only what
committed. A commit is made to fail the way it fails in production: a concurrent
writer wins the case first, so the turn's one save meets a real optimistic-
concurrency conflict and rolls back.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_LOGGER_NAME,
    TurnPath,
)
from faultmaven.core.investigation.milestone_engine import runbook_creation
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    GENERATE_RUNBOOK_PAYLOAD,
    _resolution_confirmation_suggestions,
)
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    TurnPayload,
)
from faultmaven.core.investigation.terminal_transitions import propose_transition
from faultmaven.core.investigation.turn_budget import (
    TURN_COMMIT_RESERVE_SECONDS,
    bind_turn_deadline,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.models.api_models import QueryIntent, TurnResponse
from faultmaven.modules.agent.domain.services.investigation_service import (
    attachments as attachments_module,
)
from faultmaven.modules.agent.domain.services.investigation_service import (
    turn_settlement,
)
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes import conversation as conversation_module
from faultmaven.modules.case.api.routes.conversation import submit_turn
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.owned_models.report import ReportType
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.unit.core.investigation.test_runbook_completion_and_summary_failure import (
    _make_resolved_case,
    _make_runbook_ready,
)

pytestmark = pytest.mark.unit

CASE_ID = "case_1882c0ffee00"
USER_ID = "user_1882"
REGENERATE_CLOSURE = "Regenerate the closure summary report for this case"


# ---------------------------------------------------------------------------
# The world: one case on a file database
# ---------------------------------------------------------------------------


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fm-1882.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class _Repository(SQLiteCaseRepository):
    """The real repository. Armed with ``lose_the_next_race``, the next save
    first lets a concurrent writer win the case, so that save meets a real
    version conflict and rolls back, exactly as a production 409 does."""

    def __init__(self, session, sessions):
        super().__init__(session)
        self._sessions = sessions
        self._lose = False
        self.saves = 0

    def lose_the_next_race(self) -> None:
        self._lose = True

    async def save(self, case, **rows):
        self.saves += 1
        if self._lose:
            self._lose = False
            async with self._sessions() as other:
                winner = await SQLiteCaseRepository(other).get(case.case_id)
                winner.title = "a concurrent writer won"
                await SQLiteCaseRepository(other).save(winner)
        return await super().save(case, **rows)


async def _seed(sessions, case: Case) -> None:
    async with sessions() as session:
        await SQLiteCaseRepository(session).save(case)


async def _committed(sessions) -> dict[str, Any]:
    """What a fresh session reads: the case (its ``case_actions`` records
    included) and its report rows."""
    async with sessions() as other:
        case = await SQLiteCaseRepository(other).get(CASE_ID)
        reports = (
            await other.execute(
                text("SELECT report_type, version FROM reports WHERE case_id = :c"),
                {"c": CASE_ID},
            )
        ).fetchall()
    return {"case": case, "reports": reports}


def _resolved_actions(case: Case) -> list:
    """The ``case_actions`` records of a transition to RESOLVED."""
    return [a for a in case.action_history if a.to_state == CaseState.RESOLVED]


def _engine(
    repository,
    *,
    response: Optional[InvestigationResponse_Diagnosis] = None,
    generate=None,
    conversion_service=None,
) -> MilestoneEngine:
    engine = MilestoneEngine(
        MagicMock(),
        repository,
        investigation_tools=MagicMock(),
        report_service=ReportGenerationService(case_repository=repository),
        conversion_service=conversion_service,
    )
    engine.generator.generate_structured_output = generate or AsyncMock(
        return_value=response
        or InvestigationResponse_Diagnosis(
            agent_response="The 503s line up with pool saturation.", state_updates={}
        )
    )
    return engine


def _service(repository, engine, *, storage=None) -> InvestigationService:
    service = InvestigationService(engine, repository, file_storage_service=storage)
    # No triage call: every text turn here is incident work.
    service.out_of_band_triage.triage = AsyncMock(return_value=None)
    return service


async def _turn(
    sessions,
    query: str,
    *,
    intent: Optional[QueryIntent] = None,
    lose_the_race: bool = False,
    **engine_kwargs,
):
    """One request: a fresh session and repository, as the route has."""
    async with sessions() as session:
        repository = _Repository(session, sessions)
        engine = _engine(repository, **engine_kwargs)
        service = _service(repository, engine)
        if lose_the_race:
            repository.lose_the_next_race()
        try:
            response = await service.process_turn(
                case_id=CASE_ID,
                user_id=USER_ID,
                payload=TurnPayload(query=query, intent=intent),
            )
        finally:
            # A released conversion writes its notice through this request's
            # repository; production's is sessionless, this one is not, so let
            # it finish before the session closes.
            await asyncio.gather(
                *list(runbook_creation._CONVERSION_TASKS), return_exceptions=True
            )
    return response, repository


def _verification() -> ProblemVerification:
    return ProblemVerification(
        symptom_statement="checkout returns 503",
        severity="HIGH",
        temporal_state="ongoing",
        urgency_level="high",
    )


def _inquiry_case() -> Case:
    case = Case(
        case_id=CASE_ID,
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id=USER_ID,
        enterprise_id="00000000-0000-0000-0000-000000000002",
        description="checkout 503s",
        problem_verification=_verification(),
    )
    case.current_turn = 4
    return case


def _investigating_case() -> Case:
    case = _inquiry_case()
    case.inquiry.proposed_problem_statement = "checkout returns 503"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


def _pending_resolve_case() -> Case:
    case = _investigating_case()
    propose_transition(case, to_state="resolved", summary="Resolve it?")
    return case


def _confirm_click(case: Case) -> QueryIntent:
    """The Yes card's intent, as the engine offers it on ``case``."""
    return QueryIntent(**_resolution_confirmation_suggestions(case)[0]["intent"])


def _rows(caplog) -> list:
    return [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]


# ---------------------------------------------------------------------------
# The engine commits nothing; the service commits the turn once
# ---------------------------------------------------------------------------


class TestTheTurnCommitsOnce:
    async def test_a_generation_turn_is_one_save(self, sessions):
        await _seed(sessions, _investigating_case())

        _, repository = await _turn(sessions, "what does the pool log show?")

        assert repository.saves == 1
        committed = (await _committed(sessions))["case"]
        assert committed.current_turn == 5
        assert [m["role"] for m in committed.messages] == ["user", "assistant"]

    async def test_a_terminal_confirm_is_one_save_with_its_report_and_record(
        self, sessions
    ):
        """Control for the failure test below: the CLOSED/RESOLVED state, its
        summary report and its ``case_actions`` record all land, in the one
        save."""
        case = _pending_resolve_case()
        await _seed(sessions, case)

        response, repository = await _turn(
            sessions, "Yes, resolve it.", intent=_confirm_click(case)
        )

        assert repository.saves == 1
        got = await _committed(sessions)
        assert got["case"].state == CaseState.RESOLVED
        assert [r[0] for r in got["reports"]] == [ReportType.RESOLUTION_SUMMARY.value]
        assert len(_resolved_actions(got["case"])) == 1
        assert response.case_state == CaseState.RESOLVED


# ---------------------------------------------------------------------------
# A turn whose commit fails leaves nothing of itself
# ---------------------------------------------------------------------------


class TestAFailedCommitLeavesNothing:
    async def test_the_former_half_turn_a_conflict_after_the_engine_returned(
        self, sessions
    ):
        """The shape #1882 was filed for. The engine used to save the user
        message, the clock and the state at its Step 7, so a conflict at the
        service's save left half a turn and the retry duplicated it. Now the
        service's save is the only one: the conflict leaves nothing, and the
        retry is the same turn at the same number."""
        await _seed(sessions, _investigating_case())

        with pytest.raises(StaleCaseException):
            await _turn(sessions, "what does the pool log show?", lose_the_race=True)

        committed = (await _committed(sessions))["case"]
        assert committed.title == "a concurrent writer won", "positive control"
        assert committed.current_turn == 4
        assert committed.messages == []

        await _turn(sessions, "what does the pool log show?")

        committed = (await _committed(sessions))["case"]
        assert committed.current_turn == 5
        assert [m["role"] for m in committed.messages] == ["user", "assistant"]
        assert [m["turn_number"] for m in committed.messages] == [5, 5]

    async def test_a_terminal_confirm_whose_commit_fails_leaves_no_closed_report_or_record(
        self, sessions
    ):
        """The inverse window, closed by construction: the summary row and the
        transition's ``case_actions`` record ride the same transaction as
        RESOLVED, so a failed commit leaves none of the three."""
        case = _pending_resolve_case()
        await _seed(sessions, case)

        with pytest.raises(StaleCaseException):
            await _turn(
                sessions,
                "Yes, resolve it.",
                intent=_confirm_click(case),
                lose_the_race=True,
            )

        got = await _committed(sessions)
        assert got["case"].title == "a concurrent writer won", "positive control"
        assert got["case"].state == CaseState.INVESTIGATING
        assert got["case"].pending_transition
        assert got["reports"] == []
        assert _resolved_actions(got["case"]) == []

    async def test_a_regenerate_turn_whose_commit_fails_consumes_no_slot(
        self, sessions
    ):
        """``MAX_REGENERATIONS`` is 2 versions. The failed regeneration commits
        no row, so the retry still gets the second version."""
        case = _pending_resolve_case()
        await _seed(sessions, case)
        await _turn(sessions, "Yes, resolve it.", intent=_confirm_click(case))
        regenerate = "Regenerate the resolution summary report for this case"

        with pytest.raises(StaleCaseException):
            await _turn(sessions, regenerate, lose_the_race=True)

        got = await _committed(sessions)
        assert [r[1] for r in got["reports"]] == [1]

        response, _ = await _turn(sessions, regenerate)

        got = await _committed(sessions)
        assert sorted(r[1] for r in got["reports"]) == [1, 2]
        assert not response.agent_response.startswith("Failed to regenerate")

    async def test_a_runbook_turn_whose_commit_fails_starts_no_conversion(
        self, sessions
    ):
        conversion_service = MagicMock()
        conversion_service.convert_from_case = AsyncMock(
            return_value=MagicMock(drafts=[])
        )
        conversion_service.get_conversion_by_case = AsyncMock(return_value=None)
        conversion_service.list_drafts_for_case = AsyncMock(return_value=[])
        case = _make_resolved_case(CASE_ID)
        case.user_id = USER_ID
        _make_runbook_ready(case)
        await _seed(sessions, case)

        with pytest.raises(StaleCaseException):
            await _turn(
                sessions,
                GENERATE_RUNBOOK_PAYLOAD,
                lose_the_race=True,
                conversion_service=conversion_service,
            )
        conversion_service.convert_from_case.assert_not_awaited()
        assert not runbook_creation._CONVERSION_TASKS

        # Control: the same click on a turn that commits starts it.
        await _turn(
            sessions, GENERATE_RUNBOOK_PAYLOAD, conversion_service=conversion_service
        )
        conversion_service.convert_from_case.assert_awaited_once()


# ---------------------------------------------------------------------------
# The regenerate card counts the row this turn holds uncommitted
# ---------------------------------------------------------------------------

REGENERATE_RESOLUTION = "Regenerate the resolution summary report for this case"
REGEN_CARD = "Regenerate resolution summary"


def _labels(response: TurnResponse) -> list[str]:
    return [action.label for action in response.suggested_actions or []]


class TestTheRegenerateCardCountsTheTurnsOwnRow:
    """A turn's report row commits with the turn, so when the reply is
    composed the row is not in the table yet. "Regenerations left" must count
    it (``pending``), or the reply offers a regeneration the cap has already
    spent. ``MAX_REGENERATIONS`` is 2 versions."""

    async def test_the_turn_that_renders_the_last_version_offers_no_regen_card(
        self, sessions
    ):
        case = _pending_resolve_case()
        await _seed(sessions, case)
        await _turn(sessions, "Yes, resolve it.", intent=_confirm_click(case))

        response, _ = await _turn(sessions, REGENERATE_RESOLUTION)

        got = await _committed(sessions)
        assert sorted(r[1] for r in got["reports"]) == [1, 2], "v2 was rendered"
        assert REGEN_CARD not in _labels(response)

    async def test_with_a_version_left_the_regen_card_is_offered(
        self, sessions, monkeypatch
    ):
        """Control: the card is live, and hidden above only because the cap
        is spent."""
        monkeypatch.setattr(ReportGenerationService, "MAX_REGENERATIONS", 3)
        case = _pending_resolve_case()
        await _seed(sessions, case)
        await _turn(sessions, "Yes, resolve it.", intent=_confirm_click(case))

        response, _ = await _turn(sessions, REGENERATE_RESOLUTION)

        assert REGEN_CARD in _labels(response)

    async def test_the_confirm_ack_turn_offers_no_regen_card(self, sessions):
        """The ack turn renders v1 into its plan. Its reply carries no regen
        card: the summary is rendered inline above it (INV-13's success path),
        so the count cannot make it offer one either way."""
        case = _pending_resolve_case()
        await _seed(sessions, case)

        response, _ = await _turn(
            sessions, "Yes, resolve it.", intent=_confirm_click(case)
        )

        assert [r[1] for r in (await _committed(sessions))["reports"]] == [1]
        assert REGEN_CARD not in _labels(response)


# ---------------------------------------------------------------------------
# The deadline: the route bounds the preparation, never the commit
# ---------------------------------------------------------------------------


def _user() -> UserDTO:
    return UserDTO(
        user_id=USER_ID,
        username="u1882",
        email="u1882@example.com",
        display_name="User 1882",
        is_active=True,
    )


async def _submit(sessions, monkeypatch, *, agent_timeout: float, generate=None):
    """``POST /cases/{id}/turns`` through the route function, on the real
    service, with the turn ceiling forced to ``agent_timeout``."""
    monkeypatch.setattr(
        conversation_module,
        "_resolve_agent_timeout",
        lambda _settings: (agent_timeout, "test"),
    )
    async with sessions() as session:
        repository = _Repository(session, sessions)
        engine = _engine(repository, generate=generate)
        service = _service(repository, engine)

        async def _get_case(case_id, _user_id):
            async with sessions() as other:
                return await SQLiteCaseRepository(other).get(case_id)

        case_service = MagicMock()
        case_service.get_case = AsyncMock(side_effect=_get_case)
        request = MagicMock()
        request.app.state.llm_provider = None
        return await submit_turn(
            case_id=CASE_ID,
            request=request,
            query="what does the pool log show?",
            files=[],
            pasted_content=None,
            intent_type=None,
            intent_data=None,
            input_type=None,
            source_url=None,
            case_service=case_service,
            investigation_service=service,
            current_user=_user(),
        )


class TestTheDeadline:
    async def test_a_timeout_during_the_preparation_is_a_504_and_commits_nothing(
        self, sessions, monkeypatch, caplog
    ):
        """The LLM hangs past the ceiling: the route's ``wait_for`` cancels the
        preparation, which commits nothing and re-raises the cancellation (a
        swallowed one would hand ``wait_for`` a partial turn as its result,
        R7), and emits the turn's error row on its way out."""
        await _seed(sessions, _investigating_case())

        async def _hangs(*_args, **_kwargs):
            await asyncio.Event().wait()

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            with pytest.raises(HTTPException) as refused:
                await _submit(
                    sessions,
                    monkeypatch,
                    agent_timeout=0.5,
                    generate=AsyncMock(side_effect=_hangs),
                )

        assert refused.value.status_code == 504
        assert refused.value.headers["Retry-After"] == "30"
        committed = (await _committed(sessions))["case"]
        assert committed.version == 1
        assert committed.current_turn == 4
        assert committed.messages == []
        assert [r.path for r in _rows(caplog)] == [TurnPath.ERROR.value]

    async def test_too_little_budget_left_for_the_commit_is_a_504_and_commits_nothing(
        self, sessions, monkeypatch, caplog
    ):
        """The preparation finishes inside the ceiling but with less than the
        commit's reserve left: ``commit_turn`` refuses to START the commit, and
        the client is told exactly what a timeout tells it."""
        await _seed(sessions, _investigating_case())

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            with pytest.raises(HTTPException) as refused:
                await _submit(
                    sessions,
                    monkeypatch,
                    agent_timeout=TURN_COMMIT_RESERVE_SECONDS - 1.0,
                )

        assert refused.value.status_code == 504
        assert refused.value.headers["x-error-code"] == "REQUEST_TIMEOUT"
        committed = (await _committed(sessions))["case"]
        assert committed.version == 1
        assert committed.messages == []
        assert [r.path for r in _rows(caplog)] == [TurnPath.ERROR.value]

    async def test_enough_budget_commits_through_the_route(self, sessions, monkeypatch):
        """Control for the two above: the same route, a ceiling with room."""
        await _seed(sessions, _investigating_case())

        response = await _submit(
            sessions, monkeypatch, agent_timeout=TURN_COMMIT_RESERVE_SECONDS + 30
        )

        assert isinstance(response, TurnResponse)
        committed = (await _committed(sessions))["case"]
        assert committed.current_turn == 5
        assert [m["role"] for m in committed.messages] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Once the commit has started, it runs to its end
# ---------------------------------------------------------------------------


class _SlowCommit(_Repository):
    """A repository whose turn save waits at the start of its transaction
    until released, so a test can cancel the caller mid-commit."""

    def __init__(self, session, sessions):
        super().__init__(session, sessions)
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()

    async def save(self, case, **rows):
        self.entered.set()
        await self.proceed.wait()
        return await super().save(case, **rows)


class TestACancelledCommitStillCommits:
    async def test_cancelling_the_caller_mid_commit_does_not_stop_the_commit(
        self, sessions, caplog
    ):
        """Under this stack nothing cancels the handler once the route stops
        wrapping the commit in ``wait_for`` (a client disconnect does not, #1882
        R2), so the shield is belt and braces — and this is the probe that it
        holds: the caller sees its ``CancelledError`` untouched, while the
        settlement commits, releases the turn's gates, and records the turn's
        success row rather than an error row (R1)."""
        await _seed(sessions, _investigating_case())
        async with sessions() as session:
            repository = _SlowCommit(session, sessions)
            service = _service(repository, _engine(repository))
            prepared = await service.prepare_turn(
                case_id=CASE_ID,
                user_id=USER_ID,
                payload=TurnPayload(query="what does the pool log show?"),
            )
            gate = prepared.plan.gate()

            with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
                caller = asyncio.ensure_future(service.commit_turn(prepared))
                await repository.entered.wait()
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                assert not gate.done(), "settled by the caller, not the settlement"

                repository.proceed.set()
                await asyncio.wait_for(gate, timeout=5)
                for _ in range(5):
                    await asyncio.sleep(0)

        assert gate.done() and not gate.cancelled(), "the gate was released"
        committed = (await _committed(sessions))["case"]
        assert committed.current_turn == 5
        assert [m["role"] for m in committed.messages] == ["user", "assistant"]
        assert [r.path for r in _rows(caplog)] == [TurnPath.LLM.value]

    async def test_a_cancelled_preparation_re_raises_and_commits_nothing(
        self, sessions
    ):
        """R7: a preparation that swallowed its cancellation would return a
        partial turn to ``wait_for`` as a success."""
        await _seed(sessions, _investigating_case())

        async def _hangs(*_args, **_kwargs):
            await asyncio.Event().wait()

        async with sessions() as session:
            repository = _Repository(session, sessions)
            service = _service(
                repository, _engine(repository, generate=AsyncMock(side_effect=_hangs))
            )
            preparing = asyncio.ensure_future(
                service.prepare_turn(
                    case_id=CASE_ID,
                    user_id=USER_ID,
                    payload=TurnPayload(query="what does the pool log show?"),
                )
            )
            for _ in range(20):
                await asyncio.sleep(0)
            preparing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await preparing

        assert repository.saves == 0
        assert (await _committed(sessions))["case"].messages == []


# ---------------------------------------------------------------------------
# Post-commit steps cannot turn a committed turn into an error
# ---------------------------------------------------------------------------


class _HangingStorage:
    """Stores the upload; its ``mark_linked`` never returns."""

    def __init__(self):
        self.stored: list[str] = []

    async def store_file(
        self, file_data, original_filename, enterprise_id, case_id, mime_type=None
    ):
        key = f"{enterprise_id}/{case_id}/{original_filename}"
        self.stored.append(key)
        return {"storage_key": key}

    async def mark_linked(self, storage_key: str) -> bool:
        await asyncio.Event().wait()
        return True


class TestAHungPostCommitStep:
    """``mark_linked`` is best effort (the orphan sweep keeps every blob a row
    references, #1232), so it runs after the commit in a background task the
    response never waits for, each call bounded by its own timeout."""

    @staticmethod
    async def _upload_turn(sessions, storage):
        from faultmaven.core.investigation.schemas import Attachment

        from .conftest import make_preprocessing_result

        preprocessing = MagicMock()
        preprocessing.classify_and_extract = AsyncMock(
            return_value=make_preprocessing_result()
        )
        async with sessions() as session:
            repository = _Repository(session, sessions)
            service = InvestigationService(
                _engine(repository),
                repository,
                preprocessing_service=preprocessing,
                file_storage_service=storage,
            )
            # Bounded far below the link timeout below: a response that waited
            # on the hung link would never arrive inside it.
            return await asyncio.wait_for(
                service.process_turn(
                    case_id=CASE_ID,
                    user_id=USER_ID,
                    payload=TurnPayload(
                        query="here are the logs",
                        attachments=[
                            Attachment(
                                content=b"07:40 ERROR 503 upstream reset",
                                filename="app.log",
                                content_type="text/plain",
                            )
                        ],
                    ),
                ),
                timeout=10,
            )

    async def test_a_hung_mark_linked_does_not_hold_the_response(
        self, sessions, monkeypatch
    ):
        monkeypatch.setattr(attachments_module, "MARK_LINKED_TIMEOUT_SECONDS", 3600)
        await _seed(sessions, _investigating_case())
        storage = _HangingStorage()

        response = await self._upload_turn(sessions, storage)

        assert isinstance(response, TurnResponse)
        assert storage.stored, "positive control: a blob was stored"
        [row] = (await _committed(sessions))["case"].uploaded_files
        assert row.uploaded_at_turn == 5
        pending = list(turn_settlement._POST_COMMIT_TASKS)
        assert pending, "positive control: the link is still waiting on storage"
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def test_the_background_link_is_bounded_and_counted(
        self, sessions, monkeypatch
    ):
        from unittest.mock import patch

        from .conftest import drain_post_commit

        monkeypatch.setattr(attachments_module, "MARK_LINKED_TIMEOUT_SECONDS", 0.05)
        await _seed(sessions, _investigating_case())

        with patch(
            "faultmaven.modules.agent.domain.services.investigation_service"
            ".turn_bookkeeping.EVIDENCE_MARK_LINKED_FAILURES_TOTAL"
        ) as counter:
            await self._upload_turn(sessions, _HangingStorage())
            await drain_post_commit()

        counter.labels.assert_called_once_with(outcome="timed_out")
