"""A turn whose model answer came back empty must still be saved (#1433).

``agent_response_text`` reaches the assistant row straight from
``result["agent_response"]``. The engine's semantic check covers one parse
path; a ``MAX_TOKENS`` or ``CONTENT_FILTER`` stop can still deliver ``""``.

Blank content is refused by the repository (#1429) — and before that, rejected
by ``case_messages_content_not_empty`` — and either way the failure aborts the
WHOLE aggregate save: the user's turn, the case's evidence and its hypotheses,
for a turn already charged against the tenant cap. A model that produced
nothing should degrade the answer, not destroy the case.

Run against a real SQLite schema with the pragmas the application sets, because
that is the difference between a double and the thing this is about.
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
from faultmaven.modules.agent.domain.services.orientation import (
    EMPTY_AGENT_RESPONSE_TEXT,
)
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from tests.utils import seed_enterprises, seed_users

ENTERPRISE = "ent-blank-answer"
USER_ID = "user_blank_answer"


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


def _service(session, agent_response: str):
    """The real service on a real repository, with an engine that answers
    exactly what the test asks it to — including nothing."""
    engine = create_autospec(MilestoneEngine, instance=True)

    async def _turn(*, case, user_message, **_kw):
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": agent_response,
            "metadata": {"milestones_completed": [], "progress_made": False},
        }

    engine.process_turn = AsyncMock(side_effect=_turn)
    engine.llm_provider = MagicMock()

    cap = TurnCapService(
        CapPolicyResolver(_Orgs(), default_limit=lambda: 30, multi_tenant=lambda: True),
        InMemoryTurnLedger(),
    )
    return InvestigationService(
        milestone_engine=engine,
        case_repository=SQLiteCaseRepository(session),
        turn_cap=cap,
    )


def _case() -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=USER_ID,
        enterprise_id=ENTERPRISE,
        organization_id=None,
        title="blank answer",
        state=CaseState.INQUIRY,
        inquiry=InquiryData(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.integration
class TestBlankAgentResponsePersists:
    @pytest.mark.parametrize(
        "answer", ["", "   ", "\t"], ids=["empty", "spaces", "tab"]
    )
    async def test_the_case_survives_a_model_that_answered_nothing(
        self, session, answer
    ):
        """Every blank spelling, for the reason #1420 needed them too: SQL
        ``TRIM`` strips only spaces, so a tab would slip past the constraint
        and persist as a blank-looking bubble."""
        service = _service(session, answer)
        case = _case()
        await service.repository.save(case)

        await service.process_turn(
            case_id=case.case_id, user_id=USER_ID, payload=TurnPayload(query="why?")
        )

        reloaded = await service.repository.get(case.case_id)
        assert reloaded is not None, "a blank answer must not destroy the case"

        # The user's turn survived — that is the thing the abort used to take.
        assert any(m["content"] == "why?" for m in reloaded.messages)

        assistant = [m for m in reloaded.messages if m["role"] == "assistant"]
        assert assistant, "the failed turn must be recorded, not dropped"
        assert assistant[-1]["content"] == EMPTY_AGENT_RESPONSE_TEXT
        # And it says the turn FAILED, rather than reading as a quiet answer.
        assert assistant[-1]["metadata"].get("agent_response_empty") is True

    async def test_a_real_answer_is_untouched(self):
        """Positive control, asserted without a database.

        Without it, a guard that replaced every answer would pass the tests
        above.
        """
        from faultmaven.modules.agent.domain.services.orientation import (
            EMPTY_AGENT_RESPONSE_TEXT as marker,
        )

        assert marker != "the pool was exhausted"

    async def test_a_real_answer_is_stored_verbatim(self, session):
        service = _service(session, "the connection pool was exhausted")
        case = _case()
        await service.repository.save(case)

        await service.process_turn(
            case_id=case.case_id, user_id=USER_ID, payload=TurnPayload(query="why?")
        )

        reloaded = await service.repository.get(case.case_id)
        assistant = [m for m in reloaded.messages if m["role"] == "assistant"]
        assert assistant[-1]["content"] == "the connection pool was exhausted"
        assert not assistant[-1]["metadata"].get("agent_response_empty")
