"""An empty turn must survive the case save (#1420).

``POST /cases/{id}/turns`` with no query, no file and no paste — a bare
``@FaultMaven`` in Slack — is a documented accepted input, answered with a
state-aware orientation. The user row it writes carries ``content = query or
""``, and ``case_messages_content_not_empty`` rejects a blank one, so the
IntegrityError takes the WHOLE aggregate save with it: the case row, its
evidence and its hypotheses.

Every existing empty-turn test runs against a repository DOUBLE that enforces
no constraints, which is why this was invisible. This one drives the real
service against a real SQLite schema.
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
from faultmaven.modules.agent.domain.services.orientation import EMPTY_TURN_TEXT
from faultmaven.modules.case.domain.models import Case, CaseState, InquiryData
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from tests.utils import seed_enterprises, seed_users

ENTERPRISE = "ent-empty-turn"
ORG = "org-empty-turn"
#: Seeded, because ``cases.user_id`` is an FK and this harness enforces them.
USER_ID = "user_empty_turn"


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
    """A schema as strong as the one production runs.

    ``PRAGMA foreign_keys=ON`` matters: the application sets it on every
    SQLite connect, so an engine without it enforces LESS than production and
    would pass an FK-shaped regression on this very save path. Enforcing only
    the CHECK constraints would make this harness the same kind of weaker
    stand-in whose gap let #1420 through in the first place.
    """
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
        # The tenant rows the case FKs point at. The repository writes raw
        # SQL, so nothing autoseeds these.
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as seed:
            await seed_enterprises(seed, [ENTERPRISE])
            await seed_users(seed, [USER_ID])
        yield engine
    finally:
        await engine.dispose()
        # In a finally: a dispose failure must not leave the .db (plus -wal
        # and -shm) behind in the system temp directory on every run.
        for suffix in ("", "-wal", "-shm"):
            Path(path + suffix).unlink(missing_ok=True)


@pytest.fixture
async def session(sqlite_engine):
    """One session, closed on the way out — not a factory.

    The repository takes a session INSTANCE; handing it a fresh
    ``sessionmaker`` per call leaked a connection per test and emitted
    NullPool "non-checked-in connection" errors into the suite output.
    """
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
def service(session):
    """The real InvestigationService on a REAL repository.

    The engine is a double only because an orientation turn never reaches it —
    an empty turn dispatches to ``_handle_greeting``. The repository is what
    this test is about, so it is the real one.
    """
    cap = TurnCapService(
        CapPolicyResolver(_Orgs(), default_limit=lambda: 30, multi_tenant=lambda: True),
        InMemoryTurnLedger(),
    )
    engine = create_autospec(MilestoneEngine, instance=True)
    # Recorded, not raised: an AssertionError thrown here would be swallowed by
    # ``process_turn``'s broad ``except Exception`` and re-raised as a generic
    # ServiceException, burying the diagnostic. The tests assert on the mock
    # afterwards instead.
    engine.llm_provider = MagicMock()
    engine.llm_provider.route = AsyncMock(return_value=None)

    return InvestigationService(
        milestone_engine=engine,
        case_repository=SQLiteCaseRepository(session),
        turn_cap=cap,
    )


def _case(user_id: str = None) -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id or USER_ID,
        enterprise_id=ENTERPRISE,
        # No organization: an account that no organization bills is a
        # first-class state (ADR-017 D5), and the turn cap then keys on
        # the account. Seeding one purely to satisfy an FK would add a
        # tenant shape this test does not need.
        organization_id=None,
        title="Empty turn",
        state=CaseState.INQUIRY,
        inquiry=InquiryData(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.integration
class TestEmptyTurnPersists:
    """A turn with no user message must be answered AND persist.

    Every pre-existing empty-turn test runs against ``RecordingCaseRepository``,
    a double that enforces no constraints; every test that used a real schema
    supplied non-empty content. The two halves never met, which is how #1420
    survived — a documented-accepted input that could not be saved.
    """

    async def _turn(self, service, query=None):
        case = _case()
        await service.repository.save(case)
        response = await service.process_turn(
            case_id=case.case_id, user_id=USER_ID, payload=TurnPayload(query=query)
        )
        reloaded = await service.repository.get(case.case_id)
        return response, reloaded

    @pytest.mark.parametrize(
        "query",
        [None, "", "   ", "\t", "\xa0"],
        ids=["none", "empty", "spaces", "tab", "nbsp"],
    )
    async def test_a_turn_with_no_message_is_answered_and_the_case_survives(
        self, service, query
    ):
        """Every spelling of "no message", not just ``None``.

        ``detect_orientation`` calls all of these ``EMPTY``, so they are one
        turn shape. They fail in two different ways, which is why both are
        pinned:

        - ``"   "`` is TRUTHY in Python but blank to SQL: ``TRIM`` reduces it
          to length 0 and the CHECK rejects it, aborting the whole save.
        - ``"\t"`` is the mirror — one-argument SQL ``TRIM`` strips only
          SPACES, so a tab PASSES the constraint and persists as a
          blank-looking bubble and a blank ``User:`` line in the LLM history.
          The constraint cannot catch that one; only the write site can.
        """
        response, reloaded = await self._turn(service, query)

        assert response.agent_response, "a turn with no message must still be answered"
        assert reloaded is not None, "the turn must not destroy the case"
        assert reloaded.current_turn >= 1

        user_rows = [m for m in reloaded.messages if m.get("role") == "user"]
        assert user_rows, "the turn must be recorded, not silently dropped"
        for row in user_rows:
            assert str(row.get("content") or "").strip(), f"blank content: {row}"
            assert row["content"] == EMPTY_TURN_TEXT
            assert row["metadata"]["user_message_empty"] is True

    async def test_a_greeting_turn_still_works(self, service):
        """Positive control: the sibling orientation kind, which carries text.

        If this failed too, the harness would be wrong rather than the empty
        turn being special.
        """
        response, reloaded = await self._turn(service, query="hi")

        assert response.agent_response
        assert reloaded is not None
        assert any(m.get("content") == "hi" for m in reloaded.messages)
        user_rows = [m for m in reloaded.messages if m.get("role") == "user"]
        assert all(
            not m["metadata"].get("user_message_empty") for m in user_rows
        ), "real content must not be flagged as the marker"

    async def test_no_llm_call_on_an_orientation_turn(self, service):
        """Orientation answers are deterministic; reaching the LLM is a bug.

        Asserted on the mock rather than raised from inside it: an exception
        thrown in the router would be caught by ``process_turn``'s broad
        handler and re-raised as a generic ServiceException.
        """
        await self._turn(service, query=None)
        service.engine.llm_provider.route.assert_not_awaited()
