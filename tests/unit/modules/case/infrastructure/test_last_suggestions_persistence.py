"""Case.last_suggestions must survive a repository round-trip (#914).

``last_suggestions`` is the bridge between one turn's intent-bearing DECIDE
suggestions and the next turn's typed reply: ``InvestigationService`` stores
them after each turn and the ``IntentResolver`` adoption site matches typed
text against them (``… and case.last_suggestions``). The fm#688 contract
tests exercised that flow on an in-memory Case only — neither repository
serialized the field, so on every real request it reloaded as ``None`` and
the typed-choice tier (and the INV-26 guard behind it) was unreachable.

These tests pin the property at the persistence seam: save → reload via the
repository preserves the suggestions in the exact shape the service stores
(``label`` / ``action_type`` / ``payload`` / ``body`` / ``intent``), and the
reloaded value is sufficient for the resolver's exact-match tier.

The PostgreSQL repository's real path is behind ``@pytest.mark.cloud`` (no
Postgres in CI), so — following ``test_pg_evidence_need_obtainability.py`` —
its serialization is checked through the pure ``_case_record_params`` and the
rehydration read is asserted against the module source, where a param drop
is observable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import Case
from faultmaven.modules.case.infrastructure import (
    postgresql_hybrid_case_repository as _pg_repo_module,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Async engine with fresh in-memory SQLite database per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def repository(async_session) -> SQLiteCaseRepository:
    return SQLiteCaseRepository(async_session)


def _make_case(**overrides) -> Case:
    defaults = dict(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_alpha",
        organization_id="org_alpha",
        title="Clarification round-trip",
    )
    defaults.update(overrides)
    return Case(**defaults)


def _clarification_suggestions() -> list[dict]:
    """The exact shape InvestigationService persists after a turn.

    Mirrors the ``updated_case.last_suggestions`` assembly in
    ``investigation_service.py`` for a classification clarification: the
    stored dicts carry ``action_type`` (not ``type``) plus the engine-owned
    ``file_reclassification`` intent.
    """
    return [
        {
            "label": "Application logs",
            "action_type": "DECIDE",
            "payload": "Treat the text you pasted as application logs.",
            "body": "Treat as application logs.",
            "intent": {
                "type": "file_reclassification",
                "file_id": "file_0d4bea692dd1",
                "data_type": "logs_and_errors",
            },
        },
        {
            "label": "Something else",
            "action_type": "DECIDE",
            "payload": "Treat the text you pasted as unstructured text.",
            "body": "Treat as unstructured text.",
            "intent": {
                "type": "file_reclassification",
                "file_id": "file_0d4bea692dd1",
                "data_type": "unstructured_text",
            },
        },
    ]


def _pg_repo() -> PostgreSQLHybridCaseRepository:
    session = MagicMock()
    dialect = MagicMock()
    dialect.name = "postgresql"
    session.bind = MagicMock()
    session.bind.dialect = dialect
    return PostgreSQLHybridCaseRepository(session)


# ============================================================
# SQLite — live round-trip
# ============================================================


@pytest.mark.unit
class TestSQLiteLastSuggestionsRoundTrip:
    @pytest.mark.asyncio
    async def test_round_trips_clarification_suggestions(self, repository):
        case = _make_case()
        case.last_suggestions = _clarification_suggestions()

        await repository.save(case)
        reloaded = await repository.get(case.case_id)

        assert reloaded is not None
        assert reloaded.last_suggestions == _clarification_suggestions()

    @pytest.mark.asyncio
    async def test_reloaded_suggestions_feed_the_resolver_exact_match(self, repository):
        """The consumer that was dead: typed 'Application logs' must resolve
        to the stored file_reclassification intent from the RELOADED case."""
        case = _make_case()
        case.last_suggestions = _clarification_suggestions()
        await repository.save(case)
        reloaded = await repository.get(case.case_id)

        resolver = IntentResolver(llm_router=MagicMock())
        matched = resolver._exact_match(
            "Application logs",
            [s for s in reloaded.last_suggestions if s.get("intent")],
        )

        assert matched == {
            "type": "file_reclassification",
            "file_id": "file_0d4bea692dd1",
            "data_type": "logs_and_errors",
        }

    @pytest.mark.asyncio
    async def test_none_round_trips_as_none(self, repository):
        case = _make_case()
        assert case.last_suggestions is None

        await repository.save(case)
        reloaded = await repository.get(case.case_id)

        assert reloaded.last_suggestions is None


# ============================================================
# PostgreSQL hybrid — pure param serialization + source-observable read
# ============================================================


@pytest.mark.unit
class TestPostgresLastSuggestionsPersistence:
    def test_record_params_serialize_last_suggestions(self):
        repo = _pg_repo()
        case = _make_case()
        case.last_suggestions = _clarification_suggestions()

        params = repo._case_record_params(case, datetime.now(timezone.utc))
        metadata = json.loads(params["metadata"])

        assert metadata.get("last_suggestions") == _clarification_suggestions()

    def test_record_params_omit_empty_last_suggestions(self):
        """The PG metadata bag drops falsy entries — None must not appear."""
        repo = _pg_repo()
        case = _make_case()

        params = repo._case_record_params(case, datetime.now(timezone.utc))
        metadata = json.loads(params["metadata"])

        assert "last_suggestions" not in metadata

    def test_row_to_case_reads_last_suggestions(self):
        """A rehydration drop is observable in the module source (the
        real read path needs live Postgres — same rationale as
        test_pg_evidence_need_obtainability.py)."""
        source = Path(_pg_repo_module.__file__).read_text()
        assert '"last_suggestions": metadata.get("last_suggestions") or None' in source
