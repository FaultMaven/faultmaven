"""Unit tests for CaseRepository.find_by_content_hash.

Covers both InMemoryCaseRepository and SQLiteCaseRepository — the two live
implementations per the dedup plan's Step 2a audit. PostgreSQL uses the same
SQL shape as SQLite (see postgresql_hybrid_case_repository.py); its behavior
is covered by the SQLite test class plus manual verification in staging.

Run with:
    pytest tests/unit/modules/case/infrastructure/test_find_by_content_hash.py -v
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    UploadedFile,
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
    """Fresh in-memory SQLite engine per test."""
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


def _attach_evidence_with_hash(
    case: Case,
    *,
    content_hash: str | None,
    collected_at_turn: int = 1,
    summary: str = "Evidence",
) -> Evidence:
    """Append matched (UploadedFile, Evidence) rows to ``case``.

    Post-redesign: ``content_hash`` lives on ``UploadedFile`` (not
    ``Evidence``). Dedup joins through ``Evidence.source_file_id``. The
    factory creates both rows and wires them up so the dedup query has
    something to match.

    Pass ``content_hash=None`` to simulate legacy / mid-stream evidence
    that has no hash on its file (or no file at all). The Evidence's
    ``source_file_id`` is still set so the JOIN sees a row, but the
    file's ``content_hash`` is None and nothing matches.
    """
    uploaded_file = UploadedFile(
        filename="evidence.txt",
        size_bytes=100,
        content_type="text/plain",
        content_hash=content_hash,
        uploaded_at_turn=collected_at_turn,
        upload_source="file_upload",
    )
    case.uploaded_files.append(uploaded_file)

    evidence = Evidence(
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary=summary,
        extract="content",
        source_type=EvidenceSourceType.LOGS,
        form=EvidenceForm.DOCUMENT,
        source_file_id=uploaded_file.file_id,
        collected_by="user_alpha",
        collected_at_turn=collected_at_turn,
    )
    case.evidence.append(evidence)
    return evidence


# ============================================================
# InMemoryCaseRepository
# ============================================================


class TestFindByContentHashInMemory:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self, inmemory_repo):
        case = _make_case()
        _attach_evidence_with_hash(case, content_hash="abc123")
        await inmemory_repo.save(case)

        result = await inmemory_repo.find_by_content_hash(case.case_id, "zzz999")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_existing_evidence_on_match(self, inmemory_repo):
        case = _make_case()
        evidence = _attach_evidence_with_hash(case, content_hash="hash_abc")
        await inmemory_repo.save(case)

        result = await inmemory_repo.find_by_content_hash(case.case_id, "hash_abc")

        assert result is not None
        assert result.evidence_id == evidence.evidence_id

    @pytest.mark.asyncio
    async def test_scoped_to_case(self, inmemory_repo):
        # Same hash in different cases — querying case A should not return case B's evidence
        case_a = _make_case()
        _attach_evidence_with_hash(case_a, content_hash="shared_hash", summary="A")
        await inmemory_repo.save(case_a)

        case_b = _make_case()
        _attach_evidence_with_hash(case_b, content_hash="shared_hash", summary="B")
        await inmemory_repo.save(case_b)

        result_a = await inmemory_repo.find_by_content_hash(
            case_a.case_id, "shared_hash"
        )
        result_b = await inmemory_repo.find_by_content_hash(
            case_b.case_id, "shared_hash"
        )

        assert result_a is not None
        assert result_b is not None
        assert result_a.summary == "A"
        assert result_b.summary == "B"

    @pytest.mark.asyncio
    async def test_returns_oldest_when_multiple_match(self, inmemory_repo):
        case = _make_case()
        # Insert newer first; insertion order shouldn't matter — lookup
        # picks the oldest by collected_at_turn.
        _attach_evidence_with_hash(
            case, content_hash="dup_hash", collected_at_turn=5, summary="newer"
        )
        _attach_evidence_with_hash(
            case, content_hash="dup_hash", collected_at_turn=2, summary="older"
        )
        await inmemory_repo.save(case)

        result = await inmemory_repo.find_by_content_hash(case.case_id, "dup_hash")

        assert result is not None
        assert result.summary == "older"
        assert result.collected_at_turn == 2

    @pytest.mark.asyncio
    async def test_null_content_hash_ignored(self, inmemory_repo):
        # Evidence whose UploadedFile has content_hash=None must never match,
        # including against an empty-string lookup.
        case = _make_case()
        _attach_evidence_with_hash(case, content_hash=None)
        await inmemory_repo.save(case)

        result_empty = await inmemory_repo.find_by_content_hash(case.case_id, "")
        result_none_match = await inmemory_repo.find_by_content_hash(
            case.case_id, "anything"
        )

        assert result_empty is None
        assert result_none_match is None

    @pytest.mark.asyncio
    async def test_returns_none_when_case_missing(self, inmemory_repo):
        result = await inmemory_repo.find_by_content_hash("case_doesnotexist", "hash")
        assert result is None


# ============================================================
# SQLiteCaseRepository
# ============================================================


class TestFindByContentHashSQLite:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self, sqlite_repo):
        case = _make_case()
        _attach_evidence_with_hash(case, content_hash="abc123")
        await sqlite_repo.save(case)

        result = await sqlite_repo.find_by_content_hash(case.case_id, "zzz999")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_existing_evidence_on_match(self, sqlite_repo):
        case = _make_case()
        evidence = _attach_evidence_with_hash(case, content_hash="hash_abc")
        await sqlite_repo.save(case)

        result = await sqlite_repo.find_by_content_hash(case.case_id, "hash_abc")

        assert result is not None
        assert result.evidence_id == evidence.evidence_id

    @pytest.mark.asyncio
    async def test_scoped_to_case(self, sqlite_repo):
        case_a = _make_case()
        _attach_evidence_with_hash(case_a, content_hash="shared_hash", summary="A-ev")
        await sqlite_repo.save(case_a)

        case_b = _make_case()
        _attach_evidence_with_hash(case_b, content_hash="shared_hash", summary="B-ev")
        await sqlite_repo.save(case_b)

        result_a = await sqlite_repo.find_by_content_hash(case_a.case_id, "shared_hash")
        result_b = await sqlite_repo.find_by_content_hash(case_b.case_id, "shared_hash")

        assert result_a is not None
        assert result_b is not None
        # Summaries are persisted — confirm each case returned its own evidence
        assert result_a.summary == "A-ev"
        assert result_b.summary == "B-ev"

    @pytest.mark.asyncio
    async def test_null_content_hash_ignored(self, sqlite_repo):
        case = _make_case()
        _attach_evidence_with_hash(case, content_hash=None)
        await sqlite_repo.save(case)

        result_empty = await sqlite_repo.find_by_content_hash(case.case_id, "")
        assert result_empty is None

    @pytest.mark.asyncio
    async def test_returns_none_when_case_missing(self, sqlite_repo):
        result = await sqlite_repo.find_by_content_hash("case_doesnotexist", "hash")
        assert result is None
