"""#1888 — a retried turn returns the committed turn: the turn receipt.

The invariant (owner ruling 2026-10-08): the turn's one commit records its
``Idempotency-Key``; a retry with that key returns the committed turn instead
of running a new one.

Driven through the MOUNTED case router (``POST /api/v1/cases/{id}/turns``), the
REAL ``InvestigationService`` and ``MilestoneEngine`` (the generator stubbed, no
provider call), the REAL ``CaseService`` and the production
``SessionlessCaseRepository`` over a SQLite file, with ``get_db_session``
replaced by one of the production context manager's exact shape (commit on
exit, rollback on an exception, close). The in-flight claim runs on FakeRedis,
as standalone's does. Requests go through ``httpx.ASGITransport`` on one event
loop, so two of them can genuinely overlap.

What committed is read back through a SEPARATE session.

The PostgreSQL + RLS half of the write path:
``tests/integration/test_turn_receipts_postgres_1888.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.config.idempotency_key import (
    IDEMPOTENCY_KEY_REUSE,
    IDEMPOTENCY_REPLAYED_HEADER,
)
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
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.core.investigation.terminal_transitions import propose_transition
from faultmaven.exceptions import CASE_TERMINAL
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api import turn_idempotency
from faultmaven.modules.case.api.routes import conversation as conversation_module
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.api.turn_idempotency import (
    IDEMPOTENCY_REPLAY_UNAVAILABLE,
    TURN_IN_PROGRESS,
    KeyedTurn,
    claim_name,
    open_keyed_turn,
    request_fingerprint,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    TurnReceipt,
    TurnReceiptExistsError,
)
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure import (
    sessionless_case_repository as sessionless_module,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    RepositoryException,
    SQLiteCaseRepository,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.unit.core.investigation.test_runbook_completion_and_summary_failure import (
    _make_resolved_case,
    _make_runbook_ready,
)
from tests.unit.modules.agent.conftest import make_preprocessing_result

pytestmark = pytest.mark.unit

ENTERPRISE = "00000000-0000-0000-0000-000000000002"
CASE_ID = "case_1888aaaaaaaa"
OTHER_CASE_ID = "case_1888bbbbbbbb"
OWNER = "user_1888_owner"
TEAMMATE = "user_1888_mate"
KEY = "opt_msg_1759900000000_1"
TURNS = f"/api/v1/cases/{CASE_ID}/turns"
REPLY = "The 503s line up with pool saturation."


def _user(user_id: str) -> UserDTO:
    return UserDTO(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        is_active=True,
    )


def _investigating_case(case_id: str = CASE_ID) -> Case:
    case = Case(
        case_id=case_id,
        title="Checkout 503s",
        state=CaseState.INQUIRY,
        user_id=OWNER,
        enterprise_id=ENTERPRISE,
        description="checkout 503s",
        problem_verification=ProblemVerification(
            symptom_statement="checkout returns 503",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "checkout returns 503"
    case.inquiry.problem_statement_confirmed = True
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 4
    return case


def _runbook_ready_case() -> Case:
    case = _make_resolved_case(CASE_ID)
    case.user_id = OWNER
    case.enterprise_id = ENTERPRISE
    _make_runbook_ready(case)
    return case


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


def _preprocessing():
    """Extraction whose content hash is the real hash of the bytes."""

    seen: list = []

    async def _classify(content, filename=None, source_metadata=None):
        seen.append((filename, content))
        result = make_preprocessing_result()
        result.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return result

    service = MagicMock()
    service.classify_and_extract = AsyncMock(side_effect=_classify)
    service.seen = seen
    return service


class _Storage:
    """Raw-file storage that keeps nothing."""

    async def store_file(
        self, file_data, original_filename, enterprise_id, case_id, mime_type=None
    ):
        return {"storage_key": f"{enterprise_id}/{case_id}/{original_filename}"}

    async def mark_linked(self, storage_key: str) -> bool:
        return True


@dataclass
class _Faults:
    """Failures armed for the NEXT save's session, as production meets them."""

    close_raises_after_save: bool = False
    commit_lands_then_raises: bool = False
    commit_raises_without_landing: bool = False


class _TeamCaseService(CaseService):
    """The real ``CaseService``; its read arm also admits ``TEAMMATE`` (a team
    share, which the standalone wiring has no resolver for)."""

    async def get_case(
        self, case_id, user_id=None, *, driver_only=False, creator_only=False
    ):
        case = await self.repository.get(case_id)
        if case is None:
            return None
        if user_id is None:
            return case
        if creator_only:
            return case if user_id == case.user_id else None
        reads = user_id in (case.user_id, TEAMMATE)
        if driver_only:
            return case if reads and user_id == case.effective_driver_id else None
        return case if reads else None


@dataclass
class World:
    client: httpx.AsyncClient
    sessions: Any
    repository: SessionlessCaseRepository
    generate: AsyncMock
    reserve: AsyncMock
    title: AsyncMock
    conversion: MagicMock
    redis: Any
    faults: _Faults
    app: Any = None
    preprocessing: Any = None
    user: dict = field(default_factory=lambda: {"id": OWNER})

    async def seed(self, case: Case) -> None:
        async with self.sessions() as session:
            await SQLiteCaseRepository(session).save(case)

    async def committed(self, case_id: str = CASE_ID) -> Case:
        async with self.sessions() as session:
            return await SQLiteCaseRepository(session).get(case_id)

    async def receipts(self, case_id: str = CASE_ID) -> list:
        async with self.sessions() as session:
            return (
                await session.execute(
                    text(
                        "SELECT author_id, idempotency_key, turn_number "
                        "FROM turn_receipts WHERE case_id = :c"
                    ),
                    {"c": case_id},
                )
            ).fetchall()

    async def post(
        self, data: dict, *, key: Optional[str] = KEY, path: str = TURNS, files=None
    ) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key else {}
        return await self.client.post(path, data=data, headers=headers, files=files)


@pytest.fixture
async def world(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fm-1888.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    faults = _Faults()

    @asynccontextmanager
    async def _get_db_session():
        # The production context manager's shape (database.get_db_session).
        session = sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
            if session.info.pop("saved", False) and faults.close_raises_after_save:
                faults.close_raises_after_save = False
                raise ConnectionResetError("connection reset while closing")

    real_factory = sessionless_module.get_repository_for_session

    def _factory(session):
        repo = real_factory(session)
        real_save = repo.save

        async def save(*args, **kwargs):
            real_commit = repo.db.commit
            if faults.commit_lands_then_raises:
                faults.commit_lands_then_raises = False

                async def commit():
                    await real_commit()
                    raise ConnectionError("connection lost mid-COMMIT")

                repo.db.commit = commit
            elif faults.commit_raises_without_landing:
                faults.commit_raises_without_landing = False

                async def commit():
                    raise ConnectionError("connection lost before COMMIT")

                repo.db.commit = commit
            result = await real_save(*args, **kwargs)
            session.info["saved"] = True
            return result

        repo.save = save
        return repo

    monkeypatch.setattr(sessionless_module, "get_db_session", _get_db_session)
    monkeypatch.setattr(sessionless_module, "get_repository_for_session", _factory)

    repository = SessionlessCaseRepository()
    conversion = MagicMock()
    conversion.convert_from_case = AsyncMock(return_value=MagicMock(drafts=[]))
    conversion.get_conversion_by_case = AsyncMock(return_value=None)
    conversion.list_drafts_for_case = AsyncMock(return_value=[])
    milestone_engine = MilestoneEngine(
        MagicMock(),
        repository,
        investigation_tools=MagicMock(),
        report_service=ReportGenerationService(case_repository=repository),
        conversion_service=conversion,
    )
    generate = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response=REPLY, state_updates={}
        )
    )
    milestone_engine.generator.generate_structured_output = generate
    cap = MagicMock()
    cap.reserve = AsyncMock()
    preprocessing = _preprocessing()
    service = InvestigationService(
        milestone_engine,
        repository,
        preprocessing_service=preprocessing,
        file_storage_service=_Storage(),
        turn_cap=cap,
    )
    service.out_of_band_triage.triage = AsyncMock(return_value=None)
    case_service = _TeamCaseService(repository)

    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.llm_provider = None
    app.state.redis_client = redis
    user = {"id": OWNER}
    app.dependency_overrides[require_authentication] = lambda: _user(user["id"])
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: service

    title = AsyncMock()
    tasks_before = set(runbook_creation._CONVERSION_TASKS)
    with patch.object(conversation_module, "_auto_title_case_if_default", new=title):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            w = World(
                client=client,
                sessions=sessions,
                repository=repository,
                generate=generate,
                reserve=cap.reserve,
                title=title,
                conversion=conversion,
                redis=redis,
                faults=faults,
                app=app,
                preprocessing=preprocessing,
            )
            w.user = user
            yield w
    await asyncio.gather(
        *(set(runbook_creation._CONVERSION_TASKS) - tasks_before),
        return_exceptions=True,
    )
    await engine.dispose()


async def _conversions_settled() -> None:
    """Let a released runbook conversion run (it is spawned, not awaited)."""
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.gather(*runbook_creation._CONVERSION_TASKS, return_exceptions=True)


def _confirm_click(case: Case) -> dict:
    """The standing offer's Yes card, as a client posts it."""
    card = _resolution_confirmation_suggestions(case)[0]
    intent = dict(card["intent"])
    return {
        "query": card["payload"],
        "intent_type": intent.pop("type"),
        "intent_data": json.dumps(intent),
    }


def _telemetry_paths(caplog) -> list:
    return [r.path for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]


QUERY = {"query": "what does the pool log show?"}


# ---------------------------------------------------------------------------
# 1. The receipt commits in the turn's transaction
# ---------------------------------------------------------------------------


class TestTheReceiptCommitsWithTheTurn:
    async def test_a_keyed_turn_commits_its_receipt_and_its_rows(self, world):
        await world.seed(_investigating_case())

        response = await world.post(QUERY)

        assert response.status_code == 200, response.text
        assert IDEMPOTENCY_REPLAYED_HEADER not in response.headers
        assert await world.receipts() == [(OWNER, KEY, 5)]
        committed = await world.committed()
        assert committed.current_turn == 5
        assert [m["role"] for m in committed.messages] == ["user", "assistant"]

    async def test_a_stale_save_writes_no_receipt(self, world):
        """Same transaction: the case UPDATE losing OCC leaves no receipt."""
        await world.seed(_investigating_case())
        case = await world.committed()
        case.title = "this turn's write"
        async with world.sessions() as other:
            winner = await SQLiteCaseRepository(other).get(CASE_ID)
            winner.title = "a concurrent writer won"
            await SQLiteCaseRepository(other).save(winner)

        receipt = TurnReceipt(
            case_id=CASE_ID,
            author_id=OWNER,
            idempotency_key=KEY,
            request_fingerprint="f" * 64,
            turn_number=case.current_turn,
            response={"agent_response": "x"},
        )
        with pytest.raises(StaleCaseException):
            await world.repository.save(case, receipt=receipt)

        assert await world.receipts() == []
        assert (await world.committed()).title == "a concurrent writer won"

    async def test_a_receipt_refused_by_another_constraint_is_not_a_duplicate(
        self, world
    ):
        """The unique-key translation is decided by re-reading the key, not by
        the error: a receipt refused by its CHECK, under a key with no receipt,
        stays the save's own failure."""
        await world.seed(_investigating_case())
        case = await world.committed()
        broken = TurnReceipt.model_construct(
            case_id=CASE_ID,
            author_id=OWNER,
            idempotency_key=KEY,
            request_fingerprint="f" * 64,
            turn_number=-1,
            response={"agent_response": "x"},
            created_at=datetime.now(timezone.utc),
        )

        with pytest.raises(RepositoryException) as failed:
            await world.repository.save(case, receipt=broken)

        assert not isinstance(failed.value, TurnReceiptExistsError)
        assert await world.receipts() == []

    async def test_an_unkeyed_turn_commits_no_receipt(self, world):
        await world.seed(_investigating_case())

        response = await world.post(QUERY, key=None)

        assert response.status_code == 200, response.text
        assert await world.receipts() == []
        assert (await world.committed()).current_turn == 5


# ---------------------------------------------------------------------------
# 2. A retry after the commit replays it
# ---------------------------------------------------------------------------


class TestARetryReplaysTheCommittedTurn:
    async def test_the_retry_is_the_first_response_byte_for_byte(self, world, caplog):
        await world.seed(_investigating_case())
        first = await world.post(QUERY)
        assert first.status_code == 200, first.text
        messages_after_first = len((await world.committed()).messages)
        assert world.generate.await_count == 1
        assert world.reserve.await_count == 1
        assert world.title.await_count == 1

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            retry = await world.post(QUERY)

        assert retry.status_code == 200, retry.text
        assert retry.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert retry.content == first.content
        committed = await world.committed()
        assert committed.current_turn == 5
        assert len(committed.messages) == messages_after_first
        assert world.generate.await_count == 1, "the LLM ran again"
        assert world.reserve.await_count == 1, "the retry was charged"
        assert world.title.await_count == 1, "the retry was auto-titled"
        assert _telemetry_paths(caplog) == [], "the replay emitted a turn row"
        assert await world.receipts() == [(OWNER, KEY, 5)]

    async def test_a_retried_status_transition_replays_after_the_case_closed(
        self, world
    ):
        """A2: a status pick commits (it proposes the close), the user's next
        click closes the case, and only then does the pick's retry arrive. It
        must replay, not meet "Cannot change status of a closed case"."""
        await world.seed(_investigating_case())
        pick = {
            "query": "Close this case",
            "intent_type": "status_transition",
            "intent_data": json.dumps({"to_state": "closed"}),
        }
        first = await world.post(pick, key="opt_msg_1_pick")
        assert first.status_code == 200, first.text
        confirm = _confirm_click(await world.committed())
        assert (await world.post(confirm, key="opt_msg_2_yes")).status_code == 200
        assert (await world.committed()).state == CaseState.CLOSED, "control"
        unkeyed = await world.post(pick, key=None)
        assert unkeyed.status_code == 409, "control: the gate the retry must skip"
        assert unkeyed.headers["x-error-code"] == CASE_TERMINAL

        retry = await world.post(pick, key="opt_msg_1_pick")

        assert retry.status_code == 200, retry.text
        assert retry.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert retry.content == first.content

    async def test_a_retried_paste_and_close_replays_on_the_case_it_closed(self, world):
        """The other gate: new data on a closed case is a 409. The turn that
        closed the case carried a paste; its retry must replay."""
        case = _investigating_case()
        propose_transition(case, to_state="closed", summary="Close it?")
        await world.seed(case)
        close = {
            **_confirm_click(case),
            "pasted_content": "2026-10-08T10:00:00Z pool exhausted",
        }
        first = await world.post(close)
        assert first.status_code == 200, first.text
        assert (await world.committed()).state == CaseState.CLOSED, "control"
        unkeyed = await world.post(close, key=None)
        assert unkeyed.status_code == 409, "control: the gate the retry must skip"
        assert unkeyed.headers["x-error-code"] == CASE_TERMINAL

        retry = await world.post(close)

        assert retry.status_code == 200, retry.text
        assert retry.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert retry.content == first.content

    async def test_a_retried_file_turn_replays_and_a_different_file_does_not(
        self, world
    ):
        """The fingerprint reads each file's CONTENT, once, before the gates."""
        await world.seed(_investigating_case())
        upload = {"files": ("app.log", b"ERROR pool exhausted\n", "text/plain")}
        first = await world.post(QUERY, files=upload)
        assert first.status_code == 200, first.text
        # The bytes were read once, for the fingerprint, and the SAME bytes
        # reached the engine: a second ``UploadFile.read()`` returns b"".
        assert world.preprocessing.seen == [("app.log", "ERROR pool exhausted\n")]

        retry = await world.post(QUERY, files=upload)
        assert retry.status_code == 200, retry.text
        assert retry.content == first.content

        same_name_and_size = {
            "files": ("app.log", b"ERROR pool drained!!\n", "text/plain")
        }
        reused = await world.post(QUERY, files=same_name_and_size)
        assert reused.status_code == 409, reused.text
        assert reused.headers["x-error-code"] == IDEMPOTENCY_KEY_REUSE


# ---------------------------------------------------------------------------
# 3. The same key for a different turn
# ---------------------------------------------------------------------------


PASTE = {
    "query": "what does the pool log show?",
    "pasted_content": "2026-10-08T10:00:00Z pool exhausted",
    "input_type": "text_paste",
    "observed_at": "2026-10-08T10:00:00Z",
}


class TestAKeyReusedForADifferentTurn:
    @pytest.mark.parametrize(
        "changed",
        [
            {"query": "and the replica lag?"},
            {"pasted_content": "2026-10-08T10:05:00Z pool drained"},
            {"input_type": "page_capture"},
            {"source_url": "https://status.example.com/incident/7"},
            {"observed_at": "2026-10-08T09:00:00Z"},
            {"intent_type": "conversation"},
            {"intent_data": json.dumps({"note": "x"})},
        ],
        ids=lambda changed: next(iter(changed)),
    )
    async def test_is_refused_and_charges_nothing(self, world, changed):
        """Every form field is in the fingerprint: the same key with any ONE
        of them changed is a different turn."""
        await world.seed(_investigating_case())
        assert (await world.post(PASTE)).status_code == 200
        assert world.generate.await_count == 1, "control: the first turn ran"

        reused = await world.post({**PASTE, **changed})

        assert reused.status_code == 409, reused.text
        assert reused.headers["x-error-code"] == IDEMPOTENCY_KEY_REUSE
        assert world.generate.await_count == 1
        assert world.reserve.await_count == 1
        assert (await world.committed()).current_turn == 5


# ---------------------------------------------------------------------------
# 4. A duplicate while the first is in flight
# ---------------------------------------------------------------------------


class _Gate:
    """A point the first request parks at until the test lets it go."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    async def park(self) -> None:
        self.reached.set()
        await self.go.wait()


class TestADuplicateInFlight:
    async def test_is_refused_with_retry_after_and_the_turn_runs_once(self, world):
        await world.seed(_investigating_case())
        gate = _Gate()

        async def _slow(*_args, **_kwargs):
            await gate.park()
            return InvestigationResponse_Diagnosis(
                agent_response=REPLY, state_updates={}
            )

        world.generate.side_effect = _slow
        first = asyncio.create_task(world.post(QUERY))
        await asyncio.wait_for(gate.reached.wait(), 10)

        duplicate = await asyncio.wait_for(world.post(QUERY), 30)

        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.headers["x-error-code"] == TURN_IN_PROGRESS
        assert int(duplicate.headers["Retry-After"]) >= 1
        gate.go.set()
        assert (await asyncio.wait_for(first, 30)).status_code == 200
        assert world.generate.await_count == 1
        assert world.reserve.await_count == 1

        after = await world.post(QUERY)
        assert after.status_code == 200
        assert after.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert world.generate.await_count == 1

    async def test_the_claim_is_held_through_the_commit(self, world):
        """Released only after the settlement: a duplicate arriving while the
        first is INSIDE its commit is refused, never let in to miss the receipt
        that commit is about to write and run the turn a second time."""
        await world.seed(_investigating_case())
        gate = _Gate()
        real_save = world.repository.save

        async def _parked_save(case, **rows):
            # The first turn's commit only: a duplicate let in must reach its
            # own commit and answer, not park behind the first.
            if rows.get("receipt") is not None and not gate.reached.is_set():
                await gate.park()
            return await real_save(case, **rows)

        world.repository.save = _parked_save
        first = asyncio.create_task(world.post(QUERY))
        await asyncio.wait_for(gate.reached.wait(), 10)

        duplicate = await asyncio.wait_for(world.post(QUERY), 30)

        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.headers["x-error-code"] == TURN_IN_PROGRESS
        gate.go.set()
        assert (await asyncio.wait_for(first, 30)).status_code == 200
        assert world.generate.await_count == 1

    async def test_the_claim_is_held_through_the_auto_title(self, world):
        """...and after the commit, until the auto-title has landed too."""
        await world.seed(_investigating_case())
        gate = _Gate()

        async def _parked_title(**_kwargs):
            await gate.park()

        world.title.side_effect = _parked_title
        first = asyncio.create_task(world.post(QUERY))
        await asyncio.wait_for(gate.reached.wait(), 10)

        duplicate = await asyncio.wait_for(world.post(QUERY), 30)

        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.headers["x-error-code"] == TURN_IN_PROGRESS
        gate.go.set()
        assert (await asyncio.wait_for(first, 30)).status_code == 200

    async def test_the_claim_is_taken_before_the_receipt_is_looked_up(self, world):
        """The A4 race, scheduled deterministically. The duplicate's first
        idempotency step runs while the first turn is in flight and
        uncommitted; its second runs only once the first is fully answered.
        Claim-then-look-up refuses it at the first step. Looked up first, it
        would read "no receipt", then win the claim the first released, and
        run the turn a second time."""
        await world.seed(_investigating_case())
        gate = _Gate()
        first_done = asyncio.Event()

        async def _slow(*_args, **_kwargs):
            await gate.park()
            return InvestigationResponse_Diagnosis(
                agent_response=REPLY, state_updates={}
            )

        world.generate.side_effect = _slow
        real_lookup = world.repository.get_turn_receipt
        lookups = {"n": 0}

        async def _lookup(**kwargs):
            lookups["n"] += 1
            found = await real_lookup(**kwargs)
            if lookups["n"] == 2:  # the duplicate's
                gate.go.set()
                await first_done.wait()
            return found

        world.repository.get_turn_receipt = _lookup

        async def _first():
            try:
                return await world.post(QUERY)
            finally:
                first_done.set()

        first = asyncio.create_task(_first())
        await asyncio.wait_for(gate.reached.wait(), 10)
        duplicate = asyncio.create_task(world.post(QUERY))
        # The first finishes either when the duplicate's lookup lets it go, or
        # (claim-first) when the test does after the duplicate was refused.
        done, _ = await asyncio.wait({duplicate}, timeout=5)
        if not done:
            gate.go.set()
        gate.go.set()
        assert (await asyncio.wait_for(first, 30)).status_code == 200
        dup = await asyncio.wait_for(duplicate, 30)

        assert world.generate.await_count == 1, "the duplicate ran the turn again"
        assert dup.status_code == 409, dup.text
        assert dup.headers["x-error-code"] == TURN_IN_PROGRESS


class TestTheClaimIsOwnerTokened:
    async def test_a_release_after_expiry_leaves_the_next_holders_claim(self):
        """A's claim expires mid-turn and B claims the key. A's late release
        must not delete B's claim, or a third duplicate gets in."""
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        case = _investigating_case()
        case_service = MagicMock()
        case_service.get_turn_receipt = AsyncMock(return_value=None)

        async def _open() -> KeyedTurn:
            return await open_keyed_turn(
                redis=redis,
                case=case,
                author_id=OWNER,
                idempotency_key=KEY,
                fingerprint="f" * 64,
                case_service=case_service,
                response_bound_seconds=138.5,
                correlation_id="c",
            )

        a = await _open()
        await redis.delete(claim_name(case, OWNER, KEY))  # A's TTL ran out
        b = await _open()

        await a.release()

        assert await redis.exists(claim_name(case, OWNER, KEY)) == 1
        with pytest.raises(Exception) as third:
            await _open()
        assert third.value.headers["x-error-code"] == TURN_IN_PROGRESS
        await b.release()
        assert await redis.exists(claim_name(case, OWNER, KEY)) == 0

    def test_the_ttl_is_the_turns_response_bound_plus_the_margin(self):
        """The TTL adds only the claim's margin to the response bound the route
        hands it; the bound itself is ``resolve_turn_ceiling``'s (#1905)."""
        assert turn_idempotency.claim_ttl_seconds(138.5) == math.ceil(
            138.5 + turn_idempotency.CLAIM_MARGIN_SECONDS
        )


# ---------------------------------------------------------------------------
# 5. Key scoping
# ---------------------------------------------------------------------------


def _opener(redis, case_service=None, *, response_bound_seconds: float = 138.5):
    case = _investigating_case()
    if case_service is None:
        case_service = MagicMock()
        case_service.get_turn_receipt = AsyncMock(return_value=None)

    async def _open() -> KeyedTurn:
        return await open_keyed_turn(
            redis=redis,
            case=case,
            author_id=OWNER,
            idempotency_key=KEY,
            fingerprint="f" * 64,
            case_service=case_service,
            response_bound_seconds=response_bound_seconds,
            correlation_id="c",
        )

    return case, _open


class TestTheClaimsLifetime:
    async def test_the_claim_expires_at_the_turns_whole_bound(self):
        """A claim with no TTL would outlive a request that died without its
        ``finally`` and refuse its key forever."""
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        case, _open = _opener(redis)

        keyed = await _open()

        remaining_ms = await redis.pttl(claim_name(case, OWNER, KEY))
        expected_ms = turn_idempotency.claim_ttl_seconds(138.5) * 1000
        assert expected_ms - 1000 <= remaining_ms <= expected_ms
        await keyed.release()

    async def test_retry_after_is_the_claims_remaining_ttl(self):
        redis = fakeredis_aio.FakeRedis(decode_responses=True)
        case, _open = _opener(redis)
        holder = await _open()
        await redis.pexpire(claim_name(case, OWNER, KEY), 47_000)
        remaining_s = math.ceil(await redis.pttl(claim_name(case, OWNER, KEY)) / 1000)

        with pytest.raises(HTTPException) as refused:
            await _open()

        assert refused.value.headers["x-error-code"] == TURN_IN_PROGRESS
        retry_after = int(refused.value.headers["Retry-After"])
        assert remaining_s - 5 <= retry_after <= remaining_s
        await holder.release()

    async def test_a_failed_lookup_releases_the_claim(self, world):
        """The receipt lookup raises after the claim was taken: the request
        fails, and leaves no claim behind to refuse its own retry."""
        await world.seed(_investigating_case())
        world.repository.get_turn_receipt = AsyncMock(
            side_effect=RuntimeError("receipt store unavailable")
        )

        response = await world.post(QUERY)

        assert response.status_code == 500, response.text
        assert world.repository.get_turn_receipt.await_count == 1, "control"
        case = await world.committed()
        assert await world.redis.exists(claim_name(case, OWNER, KEY)) == 0
        assert world.generate.await_count == 0


class TestWithoutAClaim:
    async def test_a_late_duplicate_is_answered_with_the_committed_turn(self, world):
        """No claim store (no Redis; likewise a claim that failed or expired).
        A duplicate whose lookup missed BEFORE the first turn committed, and
        which loaded the case AFTER it, passes OCC and meets the receipt's
        unique key. Nothing of it commits; it answers with the committed turn.

        The second LLM run is this degraded mode's residual, pinned."""
        await world.seed(_investigating_case())
        world.app.state.redis_client = None
        gate = _Gate()
        first_done = asyncio.Event()

        async def _slow(*_args, **_kwargs):
            await gate.park()
            return InvestigationResponse_Diagnosis(
                agent_response=REPLY, state_updates={}
            )

        world.generate.side_effect = _slow
        real_lookup = world.repository.get_turn_receipt
        lookups = {"n": 0}

        async def _lookup(**kwargs):
            lookups["n"] += 1
            found = await real_lookup(**kwargs)
            if lookups["n"] == 2:  # the duplicate's: let the first commit first
                gate.go.set()
                await first_done.wait()
            return found

        world.repository.get_turn_receipt = _lookup

        async def _first():
            try:
                return await world.post(QUERY)
            finally:
                first_done.set()

        first = asyncio.create_task(_first())
        await asyncio.wait_for(gate.reached.wait(), 10)
        duplicate = asyncio.create_task(world.post(QUERY))
        first_response = await asyncio.wait_for(first, 30)
        dup = await asyncio.wait_for(duplicate, 30)

        assert first_response.status_code == 200, first_response.text
        assert lookups["n"] == 3, "the duplicate read the receipt back"
        assert dup.status_code == 200, dup.text
        assert dup.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert dup.content == first_response.content
        assert await world.receipts() == [(OWNER, KEY, 5)]
        committed = await world.committed()
        assert committed.current_turn == 5
        assert len(committed.messages) == 2
        assert world.generate.await_count == 2, "the residual: a second LLM run"

    async def test_a_refused_receipt_that_cannot_be_read_back_is_in_progress(
        self, world
    ):
        """The refusal's receipt vanished before the read-back (its case was
        deleted in between): no receipt to replay, no claim to time. In flight,
        with a short Retry-After; the next retry decides."""
        await world.seed(_investigating_case())
        real_save = world.repository.save

        async def _refused(case, **rows):
            raise TurnReceiptExistsError(case.case_id, KEY)

        world.repository.save = _refused

        response = await world.post(QUERY)

        assert response.status_code == 409, response.text
        assert response.headers["x-error-code"] == TURN_IN_PROGRESS
        assert response.headers["Retry-After"] == str(
            turn_idempotency.REFUSED_REPLAY_RETRY_AFTER_SECONDS
        )
        world.repository.save = real_save


class TestKeyScoping:
    async def test_a_teammate_with_the_owners_key_is_refused_as_today(self, world):
        """A5: only the DRIVER may submit a turn (ADR-020 D2). The teammate can SEE the case,
        so the idempotency step runs for them, and must find nothing: the
        receipt is keyed on the caller. A lookup that ignored the author would
        replay the owner's turn to them instead of the 403."""
        await world.seed(_investigating_case())
        assert (await world.post(QUERY)).status_code == 200

        world.user["id"] = TEAMMATE
        theirs = await world.post(QUERY)

        assert theirs.status_code == 403, theirs.text
        assert IDEMPOTENCY_REPLAYED_HEADER not in theirs.headers
        assert world.generate.await_count == 1

    async def test_a_former_driver_gets_their_turn_replayed_and_a_new_one_refused(
        self, world
    ):
        """The driver gate sits AFTER the replay (ADR-020 D2): the driver commits
        a keyed turn, the case is handed back, and the same-key retry is still
        answered with the committed turn — while a fresh key, a NEW write by a
        former driver, is refused 403. A driver gate at the route, ahead of the
        replay, refuses the retry; no gate refuses nothing."""
        case = _investigating_case()
        case.driver_id = TEAMMATE
        await world.seed(case)
        world.user["id"] = TEAMMATE
        first = await world.post(QUERY)
        assert first.status_code == 200, first.text

        # The creator takes the wheel back: what reassign_driver writes.
        async with world.sessions() as session:
            await session.execute(
                text("UPDATE cases SET driver_id = NULL, version = version + 1")
            )
            await session.commit()

        retry = await world.post(QUERY)
        assert retry.status_code == 200, retry.text
        assert retry.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert retry.content == first.content

        fresh = await world.post(QUERY, key="another-key-0002")
        assert fresh.status_code == 403, fresh.text
        assert world.generate.await_count == 1

    async def test_the_same_key_on_another_case_is_another_turn(self, world):
        await world.seed(_investigating_case())
        await world.seed(_investigating_case(OTHER_CASE_ID))
        assert (await world.post(QUERY)).status_code == 200

        other = await world.post(QUERY, path=f"/api/v1/cases/{OTHER_CASE_ID}/turns")

        assert other.status_code == 200, other.text
        assert IDEMPOTENCY_REPLAYED_HEADER not in other.headers
        assert world.generate.await_count == 2
        assert await world.receipts(OTHER_CASE_ID) == [(OWNER, KEY, 5)]


# ---------------------------------------------------------------------------
# 6. Lost acknowledgements
# ---------------------------------------------------------------------------


class TestALostAcknowledgement:
    @pytest.mark.parametrize("key", [KEY, None], ids=["keyed", "unkeyed"])
    async def test_a_raise_closing_the_session_after_the_commit_is_a_200(
        self, world, caplog, key
    ):
        """6a, window 2: the repository committed, then leaving the session
        raised. The turn committed, so it answers 200, its gate (the runbook
        conversion) is RELEASED, and its telemetry row is the committed one."""
        await world.seed(_runbook_ready_case())
        world.faults.close_raises_after_save = True

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            response = await world.post({"query": GENERATE_RUNBOOK_PAYLOAD}, key=key)
            await _conversions_settled()

        assert response.status_code == 200, response.text
        assert not world.faults.close_raises_after_save, "positive control: it fired"
        world.conversion.convert_from_case.assert_awaited_once()
        assert TurnPath.ERROR.value not in _telemetry_paths(caplog)

    async def test_a_raise_inside_a_commit_that_landed_is_a_200_for_a_keyed_turn(
        self, world, caplog
    ):
        """6b: the commit reached the database and then raised. The receipt
        reads back under this key and turn number: committed."""
        await world.seed(_runbook_ready_case())
        world.faults.commit_lands_then_raises = True

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            response = await world.post({"query": GENERATE_RUNBOOK_PAYLOAD})
            await _conversions_settled()

        assert response.status_code == 200, response.text
        assert not world.faults.commit_lands_then_raises, "positive control"
        world.conversion.convert_from_case.assert_awaited_once()
        assert TurnPath.ERROR.value not in _telemetry_paths(caplog)
        retry = await world.post({"query": GENERATE_RUNBOOK_PAYLOAD})
        assert retry.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert retry.content == response.content

    async def test_unkeyed_the_same_raise_stays_an_error(self, world):
        """The residual, pinned: with no receipt to read, a commit that landed
        and then raised is indistinguishable from one that did not land."""
        await world.seed(_runbook_ready_case())
        world.faults.commit_lands_then_raises = True

        response = await world.post({"query": GENERATE_RUNBOOK_PAYLOAD}, key=None)
        await _conversions_settled()

        assert response.status_code == 500, response.text
        world.conversion.convert_from_case.assert_not_awaited()

    async def test_a_keyed_commit_that_did_not_land_stays_an_error(self, world):
        await world.seed(_runbook_ready_case())
        world.faults.commit_raises_without_landing = True

        response = await world.post({"query": GENERATE_RUNBOOK_PAYLOAD})
        await _conversions_settled()

        assert response.status_code == 500, response.text
        world.conversion.convert_from_case.assert_not_awaited()
        assert await world.receipts() == []

    async def test_an_older_receipt_under_the_key_does_not_vouch_for_this_turn(
        self, world
    ):
        """6c: the probe matches the key AND the turn number. A receipt the key
        left at an earlier turn says nothing about this commit.

        Through ``save`` that receipt refuses this turn's INSERT first
        (``TurnReceiptExistsError``, never probed), so the probe is asked
        directly, with its own control."""
        await world.seed(_investigating_case())
        case = await world.committed()
        older = TurnReceipt(
            case_id=CASE_ID,
            author_id=OWNER,
            idempotency_key=KEY,
            request_fingerprint="f" * 64,
            turn_number=case.current_turn,
            response={"agent_response": "an earlier turn"},
        )
        case.current_turn += 1
        await world.repository.save(case, receipt=older)
        this_turn = older.model_copy(update={"turn_number": older.turn_number + 1})

        assert await world.repository._receipt_committed(case, older), "control"
        assert not await world.repository._receipt_committed(case, this_turn)

        case = await world.committed()
        with pytest.raises(TurnReceiptExistsError):
            await world.repository.save(case, receipt=this_turn)
        assert await world.receipts() == [(OWNER, KEY, older.turn_number)]


# ---------------------------------------------------------------------------
# 11. A receipt whose response no longer validates
# ---------------------------------------------------------------------------


class TestAnUnreplayableReceipt:
    async def test_is_a_409_never_a_500_and_never_a_new_turn(self, world):
        await world.seed(_investigating_case())
        case = await world.committed()
        case.current_turn += 1
        await world.repository.save(
            case,
            receipt=TurnReceipt(
                case_id=CASE_ID,
                author_id=OWNER,
                idempotency_key=KEY,
                request_fingerprint=request_fingerprint(
                    query=QUERY["query"],
                    pasted_content=None,
                    intent_type=None,
                    intent_data=None,
                    input_type=None,
                    source_url=None,
                    observed_at=None,
                    files=[],
                ),
                turn_number=case.current_turn,
                # A deploy since changed the schema: a required field is gone.
                response={"agent_response": REPLY},
            ),
        )

        response = await world.post(QUERY)

        assert response.status_code == 409, response.text
        assert response.headers["x-error-code"] == IDEMPOTENCY_REPLAY_UNAVAILABLE
        assert response.json()["detail"] == "This turn committed; reload the case."
        assert world.generate.await_count == 0
        assert world.reserve.await_count == 0


# ---------------------------------------------------------------------------
# The published key grammar
# ---------------------------------------------------------------------------


class TestTheKeyGrammar:
    @pytest.mark.parametrize("bad", ["short", "has space!", "x" * 256])
    async def test_a_key_outside_the_grammar_is_a_422(self, world, bad):
        await world.seed(_investigating_case())

        response = await world.post(QUERY, key=bad)

        assert response.status_code == 422, response.text
        assert world.generate.await_count == 0
