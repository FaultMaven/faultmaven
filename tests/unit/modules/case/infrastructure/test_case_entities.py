"""Phase 4a — CaseRepository entity-registry methods.

Covers ``upsert_case_entities`` / ``find_entity`` / ``list_top_entities``
on both ``InMemoryCaseRepository`` and ``SQLiteCaseRepository``. The
PostgreSQL repo uses the same SQL shape (see
``postgresql_hybrid_case_repository.py``) — its semantics are mirrored
by the SQLite suite and verified in staging.

Why two backends: the in-memory impl scans Python dicts, the SQLite
impl issues DELETE/INSERT/SELECT statements against a real schema.
They must agree on ordering, replace-per-evidence semantics, and
NULL-tolerance for ``first_seen_ts``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import (
    Case,
    CaseEntity,
    CaseStatus,
    EntityType,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
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
def sqlite_repo(async_session) -> SQLiteCaseRepository:
    return SQLiteCaseRepository(async_session)


@pytest.fixture
def inmemory_repo() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


def _make_case(
    *,
    case_id: str | None = None,
    user_id: str = "user_alpha",
    organization_id: str = "org_alpha",
) -> Case:
    return Case(
        case_id=case_id or f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id=organization_id,
        title="Test case",
        status=CaseStatus.INQUIRY,
    )


def _make_evidence(
    *,
    evidence_id: str | None = None,
    summary: str = "Evidence",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id or f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary=summary,
        extract="content",
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_aabb12345678",
        collected_by="user_alpha",
        collected_at_turn=1,
    )


def _entity(
    *,
    case_id: str,
    evidence_id: str,
    entity_type: EntityType = EntityType.IP,
    entity_value: str = "10.0.0.5",
    mention_count: int = 1,
    in_error_context: bool = False,
    first_seen_ts: datetime | None = None,
) -> CaseEntity:
    return CaseEntity(
        case_id=case_id,
        entity_type=entity_type,
        entity_value=entity_value,
        evidence_id=evidence_id,
        mention_count=mention_count,
        in_error_context=in_error_context,
        first_seen_ts=first_seen_ts,
    )


# ============================================================
# Scenario helpers (backend-agnostic — take any CaseRepository)
# ============================================================


async def _scenario_upsert_then_find_exact_match(repo):
    case = _make_case()
    ev = _make_evidence()
    case.evidence.append(ev)
    await repo.save(case)

    entity = _entity(
        case_id=case.case_id,
        evidence_id=ev.evidence_id,
        entity_value="10.0.0.5",
        mention_count=4,
    )
    await repo.upsert_case_entities(case.case_id, ev.evidence_id, [entity])

    hits = await repo.find_entity(case.case_id, "10.0.0.5", EntityType.IP)
    assert len(hits) == 1
    assert hits[0].entity_value == "10.0.0.5"
    assert hits[0].mention_count == 4


async def _scenario_find_without_type_returns_all_matching(repo):
    case = _make_case()
    ev = _make_evidence()
    case.evidence.append(ev)
    await repo.save(case)

    await repo.upsert_case_entities(
        case.case_id,
        ev.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev.evidence_id,
                entity_type=EntityType.IP,
                entity_value="alpha",
                mention_count=2,
            ),
            _entity(
                case_id=case.case_id,
                evidence_id=ev.evidence_id,
                entity_type=EntityType.HOSTNAME,
                entity_value="alpha",
                mention_count=7,
            ),
        ],
    )

    all_hits = await repo.find_entity(case.case_id, "alpha")
    types = {h.entity_type for h in all_hits}
    assert types == {EntityType.IP, EntityType.HOSTNAME}
    assert all_hits[0].entity_type == EntityType.HOSTNAME
    assert all_hits[0].mention_count == 7


async def _scenario_find_empty_when_value_missing(repo):
    case = _make_case()
    ev = _make_evidence()
    case.evidence.append(ev)
    await repo.save(case)

    result = await repo.find_entity(case.case_id, "10.9.9.9")
    assert result == []


async def _scenario_find_empty_when_case_missing(repo):
    result = await repo.find_entity("case_doesnotexist", "10.0.0.5")
    assert result == []


async def _scenario_upsert_replaces_previous_rows(repo):
    case = _make_case()
    ev = _make_evidence()
    case.evidence.append(ev)
    await repo.save(case)

    first_batch = [
        _entity(
            case_id=case.case_id,
            evidence_id=ev.evidence_id,
            entity_value="ip-original",
            mention_count=3,
        ),
        _entity(
            case_id=case.case_id,
            evidence_id=ev.evidence_id,
            entity_value="ip-second",
            mention_count=1,
        ),
    ]
    await repo.upsert_case_entities(case.case_id, ev.evidence_id, first_batch)

    second_batch = [
        _entity(
            case_id=case.case_id,
            evidence_id=ev.evidence_id,
            entity_value="ip-replacement",
            mention_count=5,
        )
    ]
    await repo.upsert_case_entities(case.case_id, ev.evidence_id, second_batch)

    assert await repo.find_entity(case.case_id, "ip-original") == []
    assert await repo.find_entity(case.case_id, "ip-second") == []
    replaced = await repo.find_entity(case.case_id, "ip-replacement")
    assert len(replaced) == 1
    assert replaced[0].mention_count == 5


async def _scenario_upsert_empty_clears_rows(repo):
    case = _make_case()
    ev = _make_evidence()
    case.evidence.append(ev)
    await repo.save(case)

    await repo.upsert_case_entities(
        case.case_id,
        ev.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev.evidence_id,
                entity_value="to-be-cleared",
            )
        ],
    )
    await repo.upsert_case_entities(case.case_id, ev.evidence_id, [])
    assert await repo.find_entity(case.case_id, "to-be-cleared") == []


async def _scenario_upsert_scopes_to_single_evidence(repo):
    case = _make_case()
    ev_a = _make_evidence(summary="A")
    ev_b = _make_evidence(summary="B")
    case.evidence.append(ev_a)
    case.evidence.append(ev_b)
    await repo.save(case)

    await repo.upsert_case_entities(
        case.case_id,
        ev_a.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev_a.evidence_id,
                entity_value="from-A",
            )
        ],
    )
    await repo.upsert_case_entities(
        case.case_id,
        ev_b.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev_b.evidence_id,
                entity_value="from-B",
            )
        ],
    )
    await repo.upsert_case_entities(
        case.case_id,
        ev_a.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev_a.evidence_id,
                entity_value="A-replacement",
            )
        ],
    )

    assert await repo.find_entity(case.case_id, "from-A") == []
    a_hits = await repo.find_entity(case.case_id, "A-replacement")
    b_hits = await repo.find_entity(case.case_id, "from-B")
    assert len(a_hits) == 1
    assert len(b_hits) == 1


async def _scenario_find_scopes_to_case(repo):
    case_a = _make_case()
    ev_a = _make_evidence()
    case_a.evidence.append(ev_a)
    await repo.save(case_a)

    case_b = _make_case()
    ev_b = _make_evidence()
    case_b.evidence.append(ev_b)
    await repo.save(case_b)

    await repo.upsert_case_entities(
        case_a.case_id,
        ev_a.evidence_id,
        [
            _entity(
                case_id=case_a.case_id,
                evidence_id=ev_a.evidence_id,
                entity_value="shared",
                mention_count=10,
            )
        ],
    )
    await repo.upsert_case_entities(
        case_b.case_id,
        ev_b.evidence_id,
        [
            _entity(
                case_id=case_b.case_id,
                evidence_id=ev_b.evidence_id,
                entity_value="shared",
                mention_count=2,
            )
        ],
    )

    a_hits = await repo.find_entity(case_a.case_id, "shared")
    b_hits = await repo.find_entity(case_b.case_id, "shared")
    assert len(a_hits) == 1
    assert a_hits[0].mention_count == 10
    assert len(b_hits) == 1
    assert b_hits[0].mention_count == 2


async def _scenario_list_top_aggregates_across_evidence(repo):
    case = _make_case()
    ev_a = _make_evidence(summary="A")
    ev_b = _make_evidence(summary="B")
    case.evidence.append(ev_a)
    case.evidence.append(ev_b)
    await repo.save(case)

    await repo.upsert_case_entities(
        case.case_id,
        ev_a.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev_a.evidence_id,
                entity_value="10.0.0.5",
                mention_count=2,
            ),
            _entity(
                case_id=case.case_id,
                evidence_id=ev_a.evidence_id,
                entity_value="10.0.0.6",
                mention_count=1,
            ),
        ],
    )
    await repo.upsert_case_entities(
        case.case_id,
        ev_b.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev_b.evidence_id,
                entity_value="10.0.0.5",
                mention_count=3,
            ),
            _entity(
                case_id=case.case_id,
                evidence_id=ev_b.evidence_id,
                entity_value="10.0.0.7",
                mention_count=8,
            ),
        ],
    )

    top = await repo.list_top_entities(case.case_id, EntityType.IP, limit=5)
    by_value = {row.entity_value: row.mention_count for row in top}
    assert by_value["10.0.0.5"] == 5
    assert by_value["10.0.0.6"] == 1
    assert by_value["10.0.0.7"] == 8
    counts = [row.mention_count for row in top]
    assert counts == sorted(counts, reverse=True)


async def _scenario_list_top_respects_limit(repo):
    case = _make_case()
    ev = _make_evidence()
    case.evidence.append(ev)
    await repo.save(case)

    rows = [
        _entity(
            case_id=case.case_id,
            evidence_id=ev.evidence_id,
            entity_value=f"10.0.0.{i}",
            mention_count=i + 1,
        )
        for i in range(5)
    ]
    await repo.upsert_case_entities(case.case_id, ev.evidence_id, rows)

    top = await repo.list_top_entities(case.case_id, EntityType.IP, limit=2)
    assert len(top) == 2
    assert top[0].entity_value == "10.0.0.4"
    assert top[1].entity_value == "10.0.0.3"


async def _scenario_list_top_filters_by_type(repo):
    case = _make_case()
    ev = _make_evidence()
    case.evidence.append(ev)
    await repo.save(case)

    await repo.upsert_case_entities(
        case.case_id,
        ev.evidence_id,
        [
            _entity(
                case_id=case.case_id,
                evidence_id=ev.evidence_id,
                entity_type=EntityType.IP,
                entity_value="10.0.0.5",
                mention_count=5,
            ),
            _entity(
                case_id=case.case_id,
                evidence_id=ev.evidence_id,
                entity_type=EntityType.HOSTNAME,
                entity_value="host-a",
                mention_count=10,
            ),
        ],
    )

    ips = await repo.list_top_entities(case.case_id, EntityType.IP)
    hosts = await repo.list_top_entities(case.case_id, EntityType.HOSTNAME)
    assert [r.entity_value for r in ips] == ["10.0.0.5"]
    assert [r.entity_value for r in hosts] == ["host-a"]


async def _scenario_list_top_empty_for_missing_case(repo):
    top = await repo.list_top_entities("case_missing", EntityType.IP)
    assert top == []


# ============================================================
# InMemoryCaseRepository
# ============================================================


class TestInMemoryEntityRegistry:
    @pytest.mark.asyncio
    async def test_upsert_then_find_exact_match(self, inmemory_repo):
        await _scenario_upsert_then_find_exact_match(inmemory_repo)

    @pytest.mark.asyncio
    async def test_find_without_type_filter_returns_all_matching(self, inmemory_repo):
        await _scenario_find_without_type_returns_all_matching(inmemory_repo)

    @pytest.mark.asyncio
    async def test_find_returns_empty_when_value_missing(self, inmemory_repo):
        await _scenario_find_empty_when_value_missing(inmemory_repo)

    @pytest.mark.asyncio
    async def test_find_returns_empty_when_case_missing(self, inmemory_repo):
        await _scenario_find_empty_when_case_missing(inmemory_repo)

    @pytest.mark.asyncio
    async def test_upsert_replaces_previous_rows_for_same_evidence(self, inmemory_repo):
        await _scenario_upsert_replaces_previous_rows(inmemory_repo)

    @pytest.mark.asyncio
    async def test_upsert_empty_list_clears_evidence_rows(self, inmemory_repo):
        await _scenario_upsert_empty_clears_rows(inmemory_repo)

    @pytest.mark.asyncio
    async def test_upsert_scopes_to_single_evidence(self, inmemory_repo):
        await _scenario_upsert_scopes_to_single_evidence(inmemory_repo)

    @pytest.mark.asyncio
    async def test_find_scopes_to_case(self, inmemory_repo):
        await _scenario_find_scopes_to_case(inmemory_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_aggregates_across_evidence(self, inmemory_repo):
        await _scenario_list_top_aggregates_across_evidence(inmemory_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_respects_limit(self, inmemory_repo):
        await _scenario_list_top_respects_limit(inmemory_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_filters_by_type(self, inmemory_repo):
        await _scenario_list_top_filters_by_type(inmemory_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_empty_for_missing_case(self, inmemory_repo):
        await _scenario_list_top_empty_for_missing_case(inmemory_repo)


# ============================================================
# SQLiteCaseRepository
# ============================================================


class TestSQLiteEntityRegistry:
    @pytest.mark.asyncio
    async def test_upsert_then_find_exact_match(self, sqlite_repo):
        await _scenario_upsert_then_find_exact_match(sqlite_repo)

    @pytest.mark.asyncio
    async def test_find_without_type_filter_returns_all_matching(self, sqlite_repo):
        await _scenario_find_without_type_returns_all_matching(sqlite_repo)

    @pytest.mark.asyncio
    async def test_find_returns_empty_when_value_missing(self, sqlite_repo):
        await _scenario_find_empty_when_value_missing(sqlite_repo)

    @pytest.mark.asyncio
    async def test_find_returns_empty_when_case_missing(self, sqlite_repo):
        await _scenario_find_empty_when_case_missing(sqlite_repo)

    @pytest.mark.asyncio
    async def test_upsert_replaces_previous_rows_for_same_evidence(self, sqlite_repo):
        await _scenario_upsert_replaces_previous_rows(sqlite_repo)

    @pytest.mark.asyncio
    async def test_upsert_empty_list_clears_evidence_rows(self, sqlite_repo):
        await _scenario_upsert_empty_clears_rows(sqlite_repo)

    @pytest.mark.asyncio
    async def test_upsert_scopes_to_single_evidence(self, sqlite_repo):
        await _scenario_upsert_scopes_to_single_evidence(sqlite_repo)

    @pytest.mark.asyncio
    async def test_find_scopes_to_case(self, sqlite_repo):
        await _scenario_find_scopes_to_case(sqlite_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_aggregates_across_evidence(self, sqlite_repo):
        await _scenario_list_top_aggregates_across_evidence(sqlite_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_respects_limit(self, sqlite_repo):
        await _scenario_list_top_respects_limit(sqlite_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_filters_by_type(self, sqlite_repo):
        await _scenario_list_top_filters_by_type(sqlite_repo)

    @pytest.mark.asyncio
    async def test_list_top_entities_empty_for_missing_case(self, sqlite_repo):
        await _scenario_list_top_empty_for_missing_case(sqlite_repo)
