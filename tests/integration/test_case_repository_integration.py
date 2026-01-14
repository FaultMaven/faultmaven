"""Integration tests for Case Repository.

Tests the complete case repository flow including:
- Full case lifecycle (create -> update -> retrieve -> delete)
- Database session management
- Repository factory functionality
- Evidence, hypotheses, and messages integration

Run with:
    pytest tests/integration/test_case_repository_integration.py -v

Requirements:
    - SQLite for local testing (automatic)
    - PostgreSQL for production testing (optional, requires DATABASE_URL)
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.case_repository import (
    CaseRepository,
    InMemoryCaseRepository,
)
from faultmaven.infrastructure.persistence.database import (
    close_database,
    get_db_session,
    init_database,
    reset_engine,
)
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.repository_factory import (
    STORAGE_TYPE_DATABASE,
    STORAGE_TYPE_INMEMORY,
    get_case_repository,
    get_case_repository_async,
    reset_inmemory_repository,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    CaseStatusTransition,
    ConsultingData,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
    InvestigationProgress,
    InvestigationStrategy,
    Solution,
    SolutionType,
)

# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def test_engine():
    """Create test engine with in-memory SQLite with foreign key constraints enabled."""
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    reset_engine()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    # Enable foreign key constraints for SQLite
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()
    reset_engine()


@pytest.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test session."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="function")
async def db_repository(test_session) -> DatabaseCaseRepository:
    """Create DatabaseCaseRepository with test session."""
    return DatabaseCaseRepository(test_session)


@pytest.fixture(scope="function")
def inmemory_repository() -> InMemoryCaseRepository:
    """Create fresh InMemoryCaseRepository."""
    reset_inmemory_repository()
    return InMemoryCaseRepository()


@pytest.fixture
def sample_case_with_evidence() -> Case:
    """Create a case with evidence for testing."""
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="integration-test-user",
        organization_id="integration-test-org",
        title="Case with Evidence",
        description="Testing evidence linking",
        status=CaseStatus.INVESTIGATING,
        consulting=ConsultingData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )

    # Add evidence
    evidence = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary="Error logs showing connection timeouts",
        preprocessed_content="2024-01-01 12:00:00 ERROR: Connection timeout after 30s",
        source_type=EvidenceSourceType.LOG_FILE,
        form=EvidenceForm.DOCUMENT,
        content_size_bytes=1024,
        preprocessing_method="crime_scene_extraction",
        collected_by="test-user",
        collected_at_turn=1,
    )
    case.evidence.append(evidence)

    return case


@pytest.fixture
def sample_case_with_hypotheses() -> Case:
    """Create a case with hypotheses for testing."""
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="integration-test-user",
        organization_id="integration-test-org",
        title="Case with Hypotheses",
        description="Testing hypothesis tracking",
        status=CaseStatus.INVESTIGATING,
        consulting=ConsultingData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )

    # Add hypothesis
    hypothesis = Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement="Connection pool is exhausted",
        category=HypothesisCategory.ENVIRONMENT,
        status=HypothesisStatus.ACTIVE,
        likelihood=0.7,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="High connection count observed during error spike",
        generated_at_turn=2,
    )
    case.hypotheses[hypothesis.hypothesis_id] = hypothesis

    return case


# ============================================================
# Full Case Lifecycle Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_case_lifecycle(db_repository: DatabaseCaseRepository):
    """Test create -> update -> retrieve -> delete flow."""
    # Create
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="lifecycle-test-user",
        organization_id="lifecycle-test-org",
        title="Full Lifecycle Test",
        description="Testing complete case lifecycle",
    )

    # Step 1: Create case
    created = await db_repository.save(case)
    assert created.case_id == case.case_id

    # Step 2: Retrieve case
    retrieved = await db_repository.get(case.case_id)
    assert retrieved is not None
    assert retrieved.title == "Full Lifecycle Test"
    assert retrieved.status == CaseStatus.CONSULTING

    # Step 3: Update case
    case.title = "Updated Lifecycle Test"
    # INVESTIGATING requires confirmed problem statement and decision - SET BEFORE STATUS CHANGE
    case.consulting.problem_statement_confirmed = True
    case.consulting.decided_to_investigate = True
    case.consulting.proposed_problem_statement = "Test problem statement"
    case.status = CaseStatus.INVESTIGATING
    case.current_turn = 5
    case.consulting.quick_suggestions = ["Check database", "Review logs"]

    updated = await db_repository.save(case)
    assert updated.title == "Updated Lifecycle Test"

    # Verify update persisted
    retrieved_updated = await db_repository.get(case.case_id)
    assert retrieved_updated.title == "Updated Lifecycle Test"
    assert retrieved_updated.status == CaseStatus.INVESTIGATING

    # Step 4: Delete case
    deleted = await db_repository.delete(case.case_id)
    assert deleted is True

    # Verify deletion
    final = await db_repository.get(case.case_id)
    assert final is None


# ============================================================
# Case with Evidence Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_case_with_evidence(
    db_repository: DatabaseCaseRepository, sample_case_with_evidence: Case
):
    """Test case with linked evidence."""
    # Save case with evidence
    await db_repository.save(sample_case_with_evidence)

    # Retrieve and verify evidence
    retrieved = await db_repository.get(sample_case_with_evidence.case_id)
    assert retrieved is not None
    assert len(retrieved.evidence) == 1
    assert retrieved.evidence[0].category == EvidenceCategory.SYMPTOM_EVIDENCE
    assert "connection timeouts" in retrieved.evidence[0].summary.lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_add_evidence_to_existing_case(db_repository: DatabaseCaseRepository):
    """Test adding evidence to an existing case."""
    # Create initial case
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="evidence-test-user",
        organization_id="evidence-test-org",
        title="Evidence Addition Test",
    )
    await db_repository.save(case)

    # Add evidence
    case.evidence.append(
        Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            primary_purpose="root_cause_identified",
            summary="Memory usage at 95%",
            preprocessed_content="Memory stats: used 7.6GB / 8GB",
            source_type=EvidenceSourceType.METRICS_DATA,
            form=EvidenceForm.DOCUMENT,
            content_size_bytes=512,
            preprocessing_method="anomaly_detection",
            collected_by="test-user",
            collected_at_turn=3,
        )
    )
    await db_repository.save(case)

    # Verify evidence added
    retrieved = await db_repository.get(case.case_id)
    assert len(retrieved.evidence) == 1


# ============================================================
# Case with Hypotheses Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_case_with_hypotheses(
    db_repository: DatabaseCaseRepository, sample_case_with_hypotheses: Case
):
    """Test case with linked hypotheses."""
    # Save case with hypotheses
    await db_repository.save(sample_case_with_hypotheses)

    # Retrieve and verify hypotheses
    retrieved = await db_repository.get(sample_case_with_hypotheses.case_id)
    assert retrieved is not None
    assert len(retrieved.hypotheses) == 1

    hypothesis_id = list(retrieved.hypotheses.keys())[0]
    hypothesis = retrieved.hypotheses[hypothesis_id]
    assert hypothesis.status == HypothesisStatus.ACTIVE
    assert "connection pool" in hypothesis.statement.lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hypothesis_validation_flow(db_repository: DatabaseCaseRepository):
    """Test hypothesis lifecycle from proposed to validated."""
    # Create case
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="hypothesis-test-user",
        organization_id="hypothesis-test-org",
        title="Hypothesis Validation Test",
        description="Testing hypothesis validation flow",
        status=CaseStatus.INVESTIGATING,
        consulting=ConsultingData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )

    # Add hypothesis as proposed
    hyp_id = f"hyp_{uuid4().hex[:12]}"
    case.hypotheses[hyp_id] = Hypothesis(
        hypothesis_id=hyp_id,
        statement="Database connection leak",
        category=HypothesisCategory.DATA,
        status=HypothesisStatus.CAPTURED,
        likelihood=0.5,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="Connection count increases over time",
        generated_at_turn=1,
    )
    await db_repository.save(case)

    # Update to testing
    case.hypotheses[hyp_id].status = HypothesisStatus.ACTIVE
    case.hypotheses[hyp_id].likelihood = 0.7
    await db_repository.save(case)

    # Update to validated
    case.hypotheses[hyp_id].status = HypothesisStatus.VALIDATED
    case.hypotheses[hyp_id].likelihood = 0.95
    await db_repository.save(case)

    # Verify final state
    retrieved = await db_repository.get(case.case_id)
    assert retrieved.hypotheses[hyp_id].status == HypothesisStatus.VALIDATED
    assert retrieved.hypotheses[hyp_id].likelihood == 0.95


# ============================================================
# Repository Factory Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_inmemory():
    """Test repository factory returns InMemoryCaseRepository."""
    # Set storage type
    os.environ["CASE_STORAGE_TYPE"] = STORAGE_TYPE_INMEMORY
    reset_inmemory_repository()

    # Get repository
    async with get_case_repository_async() as repo:
        assert isinstance(repo, InMemoryCaseRepository)

        # Test basic operations
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="factory-test-user",
            organization_id="factory-test-org",
            title="Factory Test Case",
        )
        await repo.save(case)
        retrieved = await repo.get(case.case_id)
        assert retrieved is not None

    # Cleanup
    reset_inmemory_repository()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_database(test_session: AsyncSession):
    """Test repository factory with explicit session returns DatabaseCaseRepository."""
    # Pass storage_type explicitly to avoid Settings caching
    repo = get_case_repository(storage_type=STORAGE_TYPE_DATABASE, session=test_session)
    assert isinstance(repo, DatabaseCaseRepository)

    # Test basic operations
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="factory-db-test-user",
        organization_id="factory-db-test-org",
        title="Factory DB Test Case",
    )
    await repo.save(case)
    retrieved = await repo.get(case.case_id)
    assert retrieved is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_invalid_type():
    """Test repository factory with invalid storage type."""
    # Pass storage_type explicitly to avoid Settings caching
    with pytest.raises(ValueError) as exc_info:
        async with get_case_repository_async(storage_type="invalid_type") as repo:
            pass

    assert "Unknown storage type" in str(exc_info.value)


# ============================================================
# Concurrent Operations Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.xfail(
    reason="SQLAlchemy async session not thread-safe for concurrent operations - needs separate sessions per task",
    strict=False,
)
async def test_concurrent_case_creation(db_repository: DatabaseCaseRepository):
    """Test creating multiple cases concurrently."""

    async def create_case(index: int) -> Case:
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="concurrent-test-user",
            organization_id="concurrent-test-org",
            title=f"Concurrent Case {index}",
        )
        return await db_repository.save(case)

    # Create 10 cases concurrently
    cases = await asyncio.gather(*[create_case(i) for i in range(10)])

    # Verify all cases created
    assert len(cases) == 10
    assert len(set(c.case_id for c in cases)) == 10  # All unique IDs

    # Verify all retrievable
    for case in cases:
        retrieved = await db_repository.get(case.case_id)
        assert retrieved is not None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.xfail(
    reason="SQLAlchemy async session not thread-safe for concurrent operations - needs separate sessions per task",
    strict=False,
)
async def test_concurrent_message_addition(db_repository: DatabaseCaseRepository):
    """Test adding messages concurrently."""
    # Create case
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="concurrent-msg-user",
        organization_id="concurrent-msg-org",
        title="Concurrent Messages Test",
    )
    await db_repository.save(case)

    async def add_message(index: int) -> bool:
        return await db_repository.add_message(
            case.case_id,
            {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"Message {index}",
                "timestamp": datetime.now(timezone.utc),
            },
        )

    # Add 10 messages concurrently
    results = await asyncio.gather(*[add_message(i) for i in range(10)])

    # Verify all succeeded
    assert all(results)

    # Verify all messages stored
    messages = await db_repository.get_messages(case.case_id, limit=20)
    assert len(messages) == 10


# ============================================================
# Error Handling Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_nonexistent_case(db_repository: DatabaseCaseRepository):
    """Test getting a case that doesn't exist."""
    result = await db_repository.get("case_doesnotexist")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_nonexistent_case(db_repository: DatabaseCaseRepository):
    """Test deleting a case that doesn't exist."""
    result = await db_repository.delete("case_doesnotexist")
    assert result is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_add_message_nonexistent_case(db_repository: DatabaseCaseRepository):
    """Test adding message to nonexistent case."""
    result = await db_repository.add_message(
        "case_doesnotexist", {"role": "user", "content": "Test message"}
    )
    assert result is False


# ============================================================
# Data Persistence Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_complex_case_persistence(db_repository: DatabaseCaseRepository):
    """Test persisting a case with all complex fields."""
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="complex-test-user",
        organization_id="complex-test-org",
        title="Complex Case Test",
        description="Testing all fields",
        status=CaseStatus.INVESTIGATING,
        investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        consulting=ConsultingData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            quick_suggestions=["Check logs", "Restart service"],
            consultation_turns=3,
        ),
    )

    # Add progress
    case.progress.symptom_verified = True
    case.progress.scope_assessed = True
    case.progress.timeline_established = True

    # Add turn tracking
    case.current_turn = 5
    case.turns_without_progress = 1

    # Add message count
    case.message_count = 10

    # Save and retrieve
    await db_repository.save(case)
    retrieved = await db_repository.get(case.case_id)

    # Verify all fields preserved
    assert retrieved.title == "Complex Case Test"
    assert retrieved.status == CaseStatus.INVESTIGATING
    assert retrieved.investigation_strategy == InvestigationStrategy.ACTIVE_INCIDENT
    assert retrieved.consulting.quick_suggestions == ["Check logs", "Restart service"]
    assert retrieved.consulting.consultation_turns == 3
    assert retrieved.progress.symptom_verified is True
    assert retrieved.progress.scope_assessed is True
    assert retrieved.progress.timeline_established is True
    assert retrieved.current_turn == 5
    assert retrieved.turns_without_progress == 1
    assert retrieved.message_count == 10
