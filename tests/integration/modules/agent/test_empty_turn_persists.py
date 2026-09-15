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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
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

ENTERPRISE = "ent-empty-turn"
ORG = "org-empty-turn"


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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def session_factory(sqlite_engine):
    return lambda: sessionmaker(
        sqlite_engine, class_=AsyncSession, expire_on_commit=False
    )()


@pytest.fixture
def service(session_factory):
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
    # An orientation turn must never reach the LLM; if it does, say so loudly
    # rather than letting a MagicMock answer and the test pass for free.
    router = MagicMock()
    router.route = AsyncMock(
        side_effect=AssertionError("no LLM call expected on an orientation turn")
    )
    engine.llm_provider = router

    return InvestigationService(
        milestone_engine=engine,
        case_repository=SQLiteCaseRepository(session_factory()),
        turn_cap=cap,
    )


def _case(user_id: str) -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        enterprise_id=ENTERPRISE,
        organization_id=ORG,
        title="Empty turn",
        state=CaseState.INQUIRY,
        inquiry=InquiryData(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.integration
class TestEmptyTurnPersists:
    async def test_an_empty_turn_is_answered_and_the_case_survives(self, service):
        """A bare mention: no query, no attachments, no paste."""
        user_id = f"user_{uuid4().hex[:8]}"
        case = _case(user_id)
        await service.repository.save(case)

        response = await service.process_turn(
            case_id=case.case_id, user_id=user_id, payload=TurnPayload()
        )

        assert response.agent_response, "an empty turn must still be answered"

        reloaded = await service.repository.get(case.case_id)
        assert reloaded is not None, "the empty turn must not destroy the case"
        # The turn is recorded, not silently dropped.
        assert reloaded.current_turn >= 1
        for message in reloaded.messages:
            assert str(
                message.get("content") or ""
            ).strip(), f"blank content persisted: {message}"

    async def test_a_greeting_turn_still_works(self, service):
        """Positive control: the sibling orientation kind, which carries text.

        If this failed too, the harness would be wrong rather than the empty
        turn being special.
        """
        user_id = f"user_{uuid4().hex[:8]}"
        case = _case(user_id)
        await service.repository.save(case)

        response = await service.process_turn(
            case_id=case.case_id, user_id=user_id, payload=TurnPayload(query="hi")
        )

        assert response.agent_response
        reloaded = await service.repository.get(case.case_id)
        assert reloaded is not None
        assert any(m.get("content") == "hi" for m in reloaded.messages)
