"""``Case.kb_context`` must survive a repository round-trip on BOTH backends.

The KB push (fm#1360) writes matched runbooks to ``case.kb_context`` from
``MilestoneEngine._prefetch_kb_context``. Both of its triggers fire during
RESPONSE APPLICATION — after the turn's prompt is already built — so the only
prompt the pre-fetched runbooks can ever reach belongs to a LATER turn, which
loads the case back from a repository. A field dropped at save is a field the
model never sees, on any turn.

Why this file exists beside the SQLite integration test: that test proves the
durability of the SQLite writer through a real committed round-trip, and
covers PostgreSQL not at all. Deleting the PG writer's ``kb_context`` line left
4096 tests passing — a regression dropping the field on the CLOUD backend,
where the push matters most, would have shipped green.

The PG repository's live SELECT needs a real Postgres (``jsonb_build_object`` /
``json_agg``), which CI runs only in the integration ``test-postgres`` job — so
here, following ``test_last_suggestions_persistence.py`` (the sibling field
written two lines away in the same metadata bag), the write side is checked
through the pure ``_case_record_params`` and the read side by executing
``_row_to_case`` directly against a stub row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import Case
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

#: One admitted hit in exactly the shape ``_prefetch_kb_context`` writes.
KB_CONTEXT = [
    {
        "title": "ENOSPC triage on a full root volume",
        "summary": "Look for deleted-but-open descriptors with lsof +L1",
        "score": 0.91,
        "type": "runbook",
        "parent_document_id": "rb_enospc_triage",
        "trigger": "symptom",
    }
]


@pytest.fixture(scope="function")
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session


@pytest.fixture
def repository(async_session) -> SQLiteCaseRepository:
    return SQLiteCaseRepository(async_session)


def _make_case(**overrides) -> Case:
    defaults = dict(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_alpha",
        enterprise_id="ent_alpha",
        organization_id=None,
        title="KB push round trip",
    )
    defaults.update(overrides)
    return Case(**defaults)


def _pg_repo() -> PostgreSQLHybridCaseRepository:
    session = MagicMock()
    dialect = MagicMock()
    dialect.name = "postgresql"
    session.bind = MagicMock()
    session.bind.dialect = dialect
    return PostgreSQLHybridCaseRepository(session)


def _pg_row(metadata: dict) -> SimpleNamespace:
    """Duck-typed SELECT row for ``_row_to_case`` — see the sibling module."""
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_alpha",
        enterprise_id="ent_alpha",
        organization_id=None,
        source="copilot",
        title="KB push round trip",
        description="",
        state="inquiry",
        closure_reason=None,
        disposition_eligibility=None,
        investigation_strategy=None,
        current_turn=3,
        turns_without_progress=0,
        inquiry=None,
        problem_verification=None,
        working_conclusion=None,
        root_cause_conclusion=None,
        escalation_state=None,
        documentation=None,
        progress=None,
        hypotheses_data=None,
        solutions_data=None,
        uploaded_files_data=None,
        messages_data=None,
        metadata=json.dumps(metadata),
        created_at=now,
        updated_at=now,
        version=1,
        last_activity_at=None,
        resolved_at=None,
        closed_at=None,
    )


@pytest.mark.unit
class TestSQLiteKBContextRoundTrip:
    @pytest.mark.asyncio
    async def test_round_trips_the_pre_fetched_runbooks(self, repository):
        case = _make_case()
        case.kb_context = list(KB_CONTEXT)

        await repository.save(case)
        reloaded = await repository.get(case.case_id)

        assert reloaded is not None
        assert reloaded.kb_context == KB_CONTEXT

    @pytest.mark.asyncio
    async def test_none_round_trips_as_none(self, repository):
        case = _make_case()
        assert case.kb_context is None

        await repository.save(case)
        reloaded = await repository.get(case.case_id)

        assert not reloaded.kb_context


@pytest.mark.unit
class TestPostgresKBContextPersistence:
    """The half that had no coverage at all."""

    def test_record_params_serialize_kb_context(self):
        repo = _pg_repo()
        case = _make_case()
        case.kb_context = list(KB_CONTEXT)

        params = repo._case_record_params(case, datetime.now(timezone.utc))
        metadata = json.loads(params["metadata"])

        assert metadata.get("kb_context") == KB_CONTEXT

    def test_record_params_omit_empty_kb_context(self):
        """The PG metadata bag drops falsy entries — None must not appear.

        Worth its own assertion rather than being assumed: PG writes the bag
        with ``metadata = CAST(:metadata …)``, a FULL replace, so the falsy
        filter is also what CLEARS a previously-written value. If the filter
        kept ``None`` the column would carry an explicit null instead, which
        reads back the same — but if the write were additive instead of a
        replace, a cleared push would keep citing stale runbooks.
        """
        repo = _pg_repo()
        params = repo._case_record_params(_make_case(), datetime.now(timezone.utc))
        metadata = json.loads(params["metadata"])

        assert "kb_context" not in metadata

    @pytest.mark.asyncio
    async def test_row_to_case_reads_kb_context(self):
        repo = _pg_repo()
        repo._load_case_actions = AsyncMock(return_value=[])

        case = await repo._row_to_case(_pg_row({"kb_context": KB_CONTEXT}))

        assert case.kb_context == KB_CONTEXT

    @pytest.mark.asyncio
    async def test_row_to_case_without_the_key_reads_none(self):
        """A row written before fm#1360 loads as no context, not as a crash."""
        repo = _pg_repo()
        repo._load_case_actions = AsyncMock(return_value=[])

        case = await repo._row_to_case(_pg_row({}))

        assert not case.kb_context


@pytest.mark.unit
class TestBothBackendsAgree:
    """The parity assertion itself.

    A field that survives SQLite and is dropped in PostgreSQL is a silent
    deployment-dependent bug of exactly the class #1245 pinned for the
    suggestion turn-stamp: every local test reports "persistence works" while
    the cloud backend loses the push on every turn.
    """

    def test_both_writers_emit_the_same_kb_context(self, repository):
        case = _make_case()
        case.kb_context = list(KB_CONTEXT)
        stamp = datetime.now(timezone.utc)

        sqlite_meta = json.loads(
            repository._case_record_params(case, stamp)["metadata"]
        )
        pg_meta = json.loads(_pg_repo()._case_record_params(case, stamp)["metadata"])

        assert sqlite_meta.get("kb_context") == KB_CONTEXT
        assert pg_meta.get("kb_context") == KB_CONTEXT
        assert sqlite_meta["kb_context"] == pg_meta["kb_context"]
