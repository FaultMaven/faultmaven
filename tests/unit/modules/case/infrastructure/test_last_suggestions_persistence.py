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
reloaded value satisfies the resolver's public ``resolve()`` entry point.

The PostgreSQL repository's live SELECT needs a real Postgres
(``jsonb_build_object`` / ``json_agg``), which CI runs only in the
integration ``test-postgres`` job — so here, following
``test_pg_evidence_need_obtainability.py``, the write side is checked
through the pure ``_case_record_params`` and the read side by executing
``_row_to_case`` directly against a stub row (its only I/O is
``_load_case_actions``, patched out).

The same PG metadata bag also read ``message_count`` without ever writing
it — the identical asymmetry class, fixed and pinned here alongside.
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

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.suggestion_liveness import live_suggestions
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import Case, UploadedFile
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

    They also carry the liveness bookkeeping added for #1245 —
    ``offered_turn`` and ``offered_data_type``. Those keys are what bound the
    carried set and what tells a live offer from one a non-turn writer left
    behind, so a repository that drops them does not merely lose detail: the
    reader treats an unstamped entry as expired, and typed choice-matching
    goes dead on that backend only. Included in the shared fixture rather
    than in one dedicated test so EVERY assertion in this module is an
    assertion about the full stored shape.
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
            "offered_turn": 3,
            "offered_data_type": "text",
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
            "offered_turn": 3,
            "offered_data_type": "text",
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
    async def test_reloaded_suggestions_feed_the_resolver(self, repository):
        """The consumer that was dead: typed 'Application logs' must resolve
        to the stored file_reclassification intent from the RELOADED case,
        through the same public entry point the service calls. The exact-match
        tier answers before the classifier tier, so the mocked router is
        never touched."""
        case = _make_case()
        case.last_suggestions = _clarification_suggestions()
        await repository.save(case)
        reloaded = await repository.get(case.case_id)

        resolver = IntentResolver(llm_router=MagicMock())
        matched = await resolver.resolve(
            user_message="Application logs",
            last_suggestions=reloaded.last_suggestions,
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
# PostgreSQL hybrid — pure param serialization + direct _row_to_case read
# ============================================================


def _pg_row(metadata: dict) -> SimpleNamespace:
    """Duck-typed SELECT row for ``_row_to_case``.

    Every JSON/optional attribute the method touches is None-tolerant;
    only identity/scalar columns need real values.
    """
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_alpha",
        organization_id="org_alpha",
        source="copilot",
        title="Clarification round-trip",
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

    @pytest.mark.asyncio
    async def test_row_to_case_reads_last_suggestions(self):
        """Execute the real PG rehydration (the live SELECT needs Postgres,
        but _row_to_case itself is pure once _load_case_actions is patched)."""
        repo = _pg_repo()
        repo._load_case_actions = AsyncMock(return_value=[])

        case = await repo._row_to_case(
            _pg_row({"last_suggestions": _clarification_suggestions()})
        )

        assert case.last_suggestions == _clarification_suggestions()

    @pytest.mark.asyncio
    async def test_row_to_case_without_key_reads_none(self):
        """Rows written before #914 (or with no suggestions) load as None."""
        repo = _pg_repo()
        repo._load_case_actions = AsyncMock(return_value=[])

        case = await repo._row_to_case(_pg_row({}))

        assert case.last_suggestions is None


@pytest.mark.unit
class TestTurnStampSurvivesBothBackends:
    """#1245's bound is only as durable as its stamp.

    A stamp that survives SQLite and is dropped in PostgreSQL is a silent
    tenancy-dependent bug: every local test would report "expiry works" while
    the cloud backend reloads unstamped entries, which the liveness rule
    reads as expired — so typed choice-matching dies on that backend and
    nowhere else. Both sides are checked here, and both are checked by
    running the real reader over the reloaded value rather than by
    eyeballing the dict.
    """

    @staticmethod
    def _case_with_the_file(case: Case) -> Case:
        """The clarification's target file, unchanged since the offer.

        ``live_suggestions`` compares the file's current ``data_type``
        against the stamped one, so the case has to actually hold the row.
        """
        case.uploaded_files = [
            UploadedFile(
                file_id="file_0d4bea692dd1",
                filename="pasted-content-20260713T083214.txt",
                size_bytes=100,
                storage_ref="evidence/case_x/paste.txt",
                uploaded_at_turn=3,
                data_type="text",
            )
        ]
        return case

    @pytest.mark.asyncio
    async def test_sqlite_reloads_a_live_offer(self, repository):
        case = self._case_with_the_file(_make_case(current_turn=3))
        case.last_suggestions = _clarification_suggestions()
        await repository.save(case)

        reloaded = await repository.get(case.case_id)
        assert [s["offered_turn"] for s in reloaded.last_suggestions] == [3, 3]
        assert (
            live_suggestions(reloaded.last_suggestions, reloaded, as_of_turn=4)
            == reloaded.last_suggestions
        )
        # Positive control: the same reloaded value IS out of window later.
        assert live_suggestions(reloaded.last_suggestions, reloaded, as_of_turn=7) == []

    @pytest.mark.asyncio
    async def test_postgres_reloads_a_live_offer(self):
        repo = _pg_repo()
        repo._load_case_actions = AsyncMock(return_value=[])
        case = self._case_with_the_file(_make_case(current_turn=3))
        case.last_suggestions = _clarification_suggestions()

        params = repo._case_record_params(case, datetime.now(timezone.utc))
        reloaded = await repo._row_to_case(_pg_row(json.loads(params["metadata"])))
        # ``uploaded_files`` ride a separate column the stub row nulls out;
        # the point under test is the metadata bag, so restore the row the
        # referent check reads.
        reloaded = self._case_with_the_file(reloaded)

        assert [s["offered_turn"] for s in reloaded.last_suggestions] == [3, 3]
        assert (
            live_suggestions(reloaded.last_suggestions, reloaded, as_of_turn=4)
            == reloaded.last_suggestions
        )
        assert live_suggestions(reloaded.last_suggestions, reloaded, as_of_turn=7) == []


@pytest.mark.unit
class TestPostgresMessageCountPersistence:
    """PG read the counter from the metadata bag but never wrote it —
    the same write/read asymmetry class as #914, fixed alongside."""

    def test_record_params_serialize_message_count(self):
        repo = _pg_repo()
        case = _make_case()
        case.message_count = 12

        params = repo._case_record_params(case, datetime.now(timezone.utc))
        metadata = json.loads(params["metadata"])

        assert metadata.get("message_count") == 12

    @pytest.mark.asyncio
    async def test_row_to_case_round_trips_message_count(self):
        repo = _pg_repo()
        repo._load_case_actions = AsyncMock(return_value=[])
        case = _make_case()
        case.message_count = 12

        params = repo._case_record_params(case, datetime.now(timezone.utc))
        reloaded = await repo._row_to_case(_pg_row(json.loads(params["metadata"])))

        assert reloaded.message_count == 12
