"""What each writer of ``case_messages`` puts on the case, pinned (#1452).

There are four writers of a conversation row, and #1452 routes all four
through one constructor. That change is only safe if every writer's output is
the same after it as before it — the same text, the same flags, the same drop —
so this module pins each one through its REAL path, against a real SQLite
schema, and was run unchanged on the pre-#1452 tree before the refactor.

That is why the markers and metadata keys below are string LITERALS rather
than imports: the constants moved module in #1452, and a pin that imported
them from their new home could not have been run against the old tree. It is
also the stronger pin — it fixes the persisted spelling, which is what a
reader of an existing row sees, rather than whatever a constant now holds.

What is pinned is what persists and what the turn returns. The in-memory dict
a writer appends is compared field by field rather than as a whole, because
its KEY SET was not uniform before #1452 (two writers added ``case_id`` and
omitted ``token_count``, which the repository reads from the case and
defaults respectively) and is uniform after it.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    CapPolicyResolver,
    InMemoryTurnLedger,
    TurnCapService,
)
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from tests.utils import seed_enterprises, seed_users

ENTERPRISE = "ent-row-writers"
USER_ID = "user_row_writers"

#: The persisted spellings. Literals on purpose — see the module docstring.
EMPTY_TURN_TEXT = "(no message)"
EMPTY_AGENT_RESPONSE_TEXT = "(this turn produced no answer)"
USER_EMPTY_KEY = "user_message_empty"
SYNTHESIZED_KEY = "agent_response_synthesized"

#: The keys the turn path writes onto a user row's metadata.
USER_ROW_METADATA_KEYS = {
    "has_attachments",
    "attachment_count",
    "intent_type",
    USER_EMPTY_KEY,
    "intent_metadata",
}

#: Every blank spelling the writers have been fixed for. A tab and a newline
#: matter separately: one-argument SQL ``TRIM`` strips only spaces, so they
#: pass the CHECK constraint and would persist as a blank-looking row.
BLANKS = ["", "   ", "\t", "\n"]
BLANK_IDS = ["empty", "spaces", "tab", "newline"]


class _Orgs:
    async def get_organization(self, organization_id):
        return SimpleNamespace(organization_id=organization_id, daily_turn_cap=None)


@pytest.fixture(autouse=True)
def bound_tenant():
    from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
    from faultmaven.config.tenant_context import set_current_enterprise_id

    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


@pytest.fixture
async def sqlite_engine():
    """The application's own schema and FK pragma, so the CHECK constraint
    that makes a blank row fatal is the one production runs."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as seed:
            await seed_enterprises(seed, [ENTERPRISE])
            await seed_users(seed, [USER_ID])
        yield engine
    finally:
        await engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(path + suffix).unlink(missing_ok=True)


@pytest.fixture
async def session(sqlite_engine):
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


def _case(**overrides) -> Case:
    fields = dict(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=USER_ID,
        enterprise_id=ENTERPRISE,
        organization_id=None,
        title="row writers",
        state=CaseState.INQUIRY,
        inquiry=InquiryData(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return Case(**fields)


def _turn_service(session, answer: str, extra_metadata: dict | None = None):
    """The real service on a real repository, with an engine that answers
    exactly ``answer`` — including nothing — and hands back the metadata dict
    it returned, so the test can check the row carries that same object."""
    engine = create_autospec(MilestoneEngine, instance=True)
    returned: dict = {}

    async def _turn(*, case, user_message, **_kw):
        case.updated_at = datetime.now(timezone.utc)
        metadata = {
            "milestones_completed": [],
            "progress_made": False,
            **(extra_metadata or {}),
        }
        returned["metadata"] = metadata
        return {"case_updated": case, "agent_response": answer, "metadata": metadata}

    engine.process_turn = AsyncMock(side_effect=_turn)
    engine.llm_provider = MagicMock()
    engine.llm_provider.route = AsyncMock(return_value=None)

    cap = TurnCapService(
        CapPolicyResolver(_Orgs(), default_limit=lambda: 30, multi_tenant=lambda: True),
        InMemoryTurnLedger(),
    )
    service = InvestigationService(
        milestone_engine=engine,
        case_repository=SQLiteCaseRepository(session),
        turn_cap=cap,
    )
    return service, returned


class _SaveSpy:
    """Records the case object each ``save`` was handed, so the in-memory row
    a writer appended can be read exactly as the writer left it."""

    def __init__(self, repository):
        self.saved: list = []
        inner = repository.save

        async def _save(case):
            self.saved.append(case)
            return await inner(case)

        repository.save = _save


def _assert_canonical_created_at(value) -> None:
    """Persisted ``created_at`` is a ``T``-separated UTC ISO-8601 string."""
    assert isinstance(value, str) and "T" in value, value
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _assert_minted_id(value) -> None:
    assert isinstance(value, str) and value.startswith("msg_"), value
    assert len(value) == len("msg_") + 12, value


# ---------------------------------------------------------------------------
# Writer 1 — the turn path's USER row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
class TestUserTurnRow:
    async def _run(self, session, query):
        service, _ = _turn_service(session, "an answer")
        spy = _SaveSpy(service.repository)
        case = _case()
        await service.repository.save(case)
        await service.process_turn(
            case_id=case.case_id, user_id=USER_ID, payload=TurnPayload(query=query)
        )
        live = [m for m in spy.saved[-1].messages if m["role"] == "user"]
        reloaded = await service.repository.get(case.case_id)
        stored = [m for m in reloaded.messages if m["role"] == "user"]
        return live, stored, reloaded

    async def test_real_text_is_stored_verbatim_and_unflagged(self, session):
        # Leading/trailing space is KEPT on a turn row — unlike the initial
        # message, which is stripped. Two writers, two rules; both pinned.
        live, stored, _ = await self._run(session, "  why is it down?  ")

        for row in (live[-1], stored[-1]):
            assert row["content"] == "  why is it down?  "
            assert row["role"] == "user"
            assert row["turn_number"] == 1
            assert row["author_id"] == USER_ID
            assert row["token_count"] is None
            assert set(row["metadata"]) == USER_ROW_METADATA_KEYS
            # Always PRESENT on a user row, False when the user wrote text.
            assert row["metadata"][USER_EMPTY_KEY] is False
            assert row["metadata"]["has_attachments"] is False
            assert row["metadata"]["attachment_count"] == 0
        _assert_minted_id(stored[-1]["message_id"])
        assert live[-1]["message_id"] == stored[-1]["message_id"]
        _assert_canonical_created_at(stored[-1]["created_at"])

    @pytest.mark.parametrize("query", [None, *BLANKS], ids=["none", *BLANK_IDS])
    async def test_a_blank_turn_is_recorded_with_the_marker_and_the_flag(
        self, session, query
    ):
        live, stored, reloaded = await self._run(session, query)

        assert reloaded is not None, "a blank user turn must not abort the save"
        for row in (live[-1], stored[-1]):
            assert row["content"] == EMPTY_TURN_TEXT
            assert row["role"] == "user"
            assert row["turn_number"] == 1
            assert row["author_id"] == USER_ID
            # A blank turn is answered by orientation, which tags the row the
            # constructor returned AFTER it was appended — so the returned row
            # must be the appended one, not a copy.
            assert set(row["metadata"]) == USER_ROW_METADATA_KEYS | {
                "orientation",
                "out_of_band",
            }
            assert row["metadata"]["orientation"] == "empty"
            assert row["metadata"][USER_EMPTY_KEY] is True
        _assert_canonical_created_at(stored[-1]["created_at"])

    async def test_one_row_per_turn_and_the_counters_move_together(self, session):
        _, _, reloaded = await self._run(session, "why?")

        assert [m["role"] for m in reloaded.messages] == ["user", "assistant"]
        assert reloaded.message_count == 2
        assert reloaded.current_turn == 1


# ---------------------------------------------------------------------------
# Writer 2 — the turn path's ASSISTANT row (the persistence backstop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
class TestAgentAnswerRow:
    async def _run(self, session, answer, extra_metadata=None):
        service, returned = _turn_service(session, answer, extra_metadata)
        spy = _SaveSpy(service.repository)
        case = _case()
        await service.repository.save(case)
        response = await service.process_turn(
            case_id=case.case_id, user_id=USER_ID, payload=TurnPayload(query="why?")
        )
        live = [m for m in spy.saved[-1].messages if m["role"] == "assistant"][-1]
        reloaded = await service.repository.get(case.case_id)
        stored = [m for m in reloaded.messages if m["role"] == "assistant"][-1]
        return response, live, stored, returned["metadata"]

    async def test_a_real_answer_is_stored_verbatim_and_unflagged(self, session):
        response, live, stored, engine_meta = await self._run(
            session, "  the pool was exhausted  "
        )

        assert response.agent_response == "  the pool was exhausted  "
        for row in (live, stored):
            assert row["content"] == "  the pool was exhausted  "
            assert row["role"] == "assistant"
            assert row["turn_number"] == 1
            assert row["author_id"] is None
            assert row["token_count"] is None
            # ABSENT, not False, on an answered turn.
            assert SYNTHESIZED_KEY not in row["metadata"]
        # The row's metadata IS the engine's per-turn dict (#1270): one
        # binding, so every reader of the turn's metadata sees the same thing.
        assert live["metadata"] is engine_meta
        _assert_minted_id(stored["message_id"])
        _assert_canonical_created_at(stored["created_at"])

    @pytest.mark.parametrize("answer", BLANKS, ids=BLANK_IDS)
    async def test_a_blank_answer_is_recorded_with_the_backstop_marker_and_flag(
        self, session, answer
    ):
        response, live, stored, engine_meta = await self._run(session, answer)

        # The live answer and the stored row agree.
        assert response.agent_response == EMPTY_AGENT_RESPONSE_TEXT
        for row in (live, stored):
            assert row["content"] == EMPTY_AGENT_RESPONSE_TEXT
            assert row["author_id"] is None
            assert row["metadata"][SYNTHESIZED_KEY] is True
        # Written IN PLACE on the engine's dict, not onto a copy.
        assert live["metadata"] is engine_meta
        assert engine_meta[SYNTHESIZED_KEY] is True

    async def test_an_engine_placeholder_keeps_its_own_wording(self, session):
        placeholder = "[Response withheld by safety filter]"
        response, live, stored, _ = await self._run(
            session, placeholder, {SYNTHESIZED_KEY: True}
        )

        assert response.agent_response == placeholder
        for row in (live, stored):
            assert row["content"] == placeholder
            assert row["metadata"][SYNTHESIZED_KEY] is True


# ---------------------------------------------------------------------------
# Writer 3 — ``CaseService.create_case``'s initial message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
class TestInitialMessageRow:
    def _service(self, session):
        from faultmaven.modules.case.domain.services.case_service import CaseService

        return CaseService(case_repository=SQLiteCaseRepository(session))

    async def test_real_text_is_stripped_and_written_as_turn_one(self, session):
        service = self._service(session)

        case = await service.create_case(
            title="disk full",
            owner_id=f"  {USER_ID}  ",
            initial_message="  /var is at 100%  ",
        )
        live = case.messages
        reloaded = await service.repository.get(case.case_id)

        assert case.message_count == 1
        assert reloaded.message_count == 1
        for rows in (live, reloaded.messages):
            assert len(rows) == 1
            row = rows[0]
            assert row["content"] == "/var is at 100%"
            assert row["role"] == "user"
            assert row["turn_number"] == 1
            # The OWNER, stripped — the same stripping the case's user_id gets.
            assert row["author_id"] == USER_ID
            assert row.get("token_count") is None
            assert row["metadata"] == {}
        _assert_minted_id(reloaded.messages[0]["message_id"])
        assert live[0]["message_id"] == reloaded.messages[0]["message_id"]
        _assert_canonical_created_at(reloaded.messages[0]["created_at"])

    @pytest.mark.parametrize("blank", [None, *BLANKS], ids=["none", *BLANK_IDS])
    async def test_a_blank_initial_message_writes_no_row(self, session, blank):
        service = self._service(session)

        case = await service.create_case(
            title="disk full", owner_id=USER_ID, initial_message=blank
        )
        reloaded = await service.repository.get(case.case_id)

        assert reloaded is not None, f"case creation failed on {blank!r}"
        assert case.messages == [] and reloaded.messages == []
        assert case.message_count == 0 and reloaded.message_count == 0


# ---------------------------------------------------------------------------
# Writer 4 — the engine's runbook-conversion completion notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
class TestSystemNoticeRow:
    async def _run(self, session, conversion_service):
        from faultmaven.modules.knowledge.domain.models.conversion import (
            CaseConversionRequest,
        )

        repository = SQLiteCaseRepository(session)
        case = _case(current_turn=3)
        await repository.save(case)
        spy = _SaveSpy(repository)

        llm = MagicMock()
        llm.generate = AsyncMock(return_value=MagicMock())
        engine = MilestoneEngine(llm, repository, investigation_tools=MagicMock())
        request = CaseConversionRequest(
            case_id=case.case_id,
            title=case.title,
            description=case.description,
            scope="global",
        )
        await engine._run_runbook_conversion(
            conversion_service, request, USER_ID, ENTERPRISE
        )
        live = spy.saved[-1]
        reloaded = await repository.get(case.case_id)
        return live, reloaded

    async def test_the_notice_is_a_system_row_with_no_author(self, session):
        draft = MagicMock()
        draft.runbook_id = "rb_001"
        draft.title = "Pool Timeout Runbook"
        draft.quality_score = 85
        conversion = MagicMock()
        conversion.convert_from_case = AsyncMock(return_value=MagicMock(drafts=[draft]))

        live, reloaded = await self._run(session, conversion)

        expected = (
            "Your runbook draft **Pool Timeout Runbook** is ready. "
            "View it in the Dashboard under **Knowledge Base > Drafts**."
        )
        assert live.message_count == 1
        assert reloaded.message_count == 1
        for rows in (live.messages, reloaded.messages):
            assert len(rows) == 1
            row = rows[0]
            assert row["content"] == expected
            assert row["role"] == "system"
            assert row["author_id"] is None
            # The case's clock at the moment the notice was written.
            assert row["turn_number"] == 3
            assert row.get("token_count") is None
            assert row["metadata"] == {"source": "runbook_conversion_complete"}
        _assert_minted_id(reloaded.messages[0]["message_id"])
        _assert_canonical_created_at(reloaded.messages[0]["created_at"])

    async def test_a_failed_conversion_still_writes_its_notice(self, session):
        conversion = MagicMock()
        conversion.convert_from_case = AsyncMock(side_effect=RuntimeError("boom"))

        _, reloaded = await self._run(session, conversion)

        assert [m["role"] for m in reloaded.messages] == ["system"]
        assert reloaded.messages[0]["content"].startswith("Runbook generation failed")
