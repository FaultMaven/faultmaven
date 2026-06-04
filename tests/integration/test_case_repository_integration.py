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

from faultmaven.infrastructure.persistence.database import (
    close_database,
    get_db_session,
    init_database,
    reset_engine,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import (
    Case,
    CaseAction,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    InvestigationProgress,
    InvestigationStrategy,
    Solution,
    SolutionType,
    UploadedFile,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    CaseRepository,
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from tests.utils import seed_organizations, seed_users

# Hardcoded org IDs referenced by tests below — seeded once per test_engine
# so FK constraints on cases.organization_id (Phase 9) are satisfied.
TEST_ORG_IDS = (
    "integration-test-org",
    "lifecycle-test-org",
    "evidence-test-org",
    "hypothesis-test-org",
    "factory-test-org",
    "factory-db-test-org",
    "concurrent-test-org",
    "concurrent-msg-org",
    "complex-test-org",
)

# Hardcoded user IDs referenced by tests below — seeded once per test_engine
# so FK constraints on cases.user_id are satisfied with PRAGMA foreign_keys=ON.
TEST_USER_IDS = (
    "integration-test-user",
    "lifecycle-test-user",
    "evidence-test-user",
    "hypothesis-test-user",
    "concurrent-test-user",
    "concurrent-msg-user",
    "complex-test-user",
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

    # Seed organizations referenced by tests so cases.organization_id FK is satisfied.
    seed_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with seed_factory() as seed_session:
        await seed_organizations(seed_session, TEST_ORG_IDS)
        await seed_users(seed_session, TEST_USER_IDS)

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
async def db_repository(test_session) -> SQLiteCaseRepository:
    """Create SQLiteCaseRepository with test session."""
    return SQLiteCaseRepository(test_session)


@pytest.fixture(scope="function")
def inmemory_repository() -> InMemoryCaseRepository:
    """Create fresh InMemoryCaseRepository."""
    return InMemoryCaseRepository()


@pytest.fixture
def sample_case_with_evidence() -> Case:
    """Create a case with evidence for testing.

    Post-010: file-backed evidence requires a matching UploadedFile
    row so the source_file_id FK resolves. We synthesize both the
    file row and the evidence row pointing at it.
    """
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="integration-test-user",
        organization_id="integration-test-org",
        title="Case with Evidence",
        description="Testing evidence linking",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )

    # Add the source file the evidence references. uploaded_by must
    # match a seeded user_id (the test_engine fixture seeds the
    # TEST_USER_IDS list); use ``integration-test-user`` to align with
    # the case's user_id.
    file_id = f"file_{uuid4().hex[:12]}"
    case.uploaded_files.append(
        UploadedFile(
            file_id=file_id,
            filename="app.log",
            size_bytes=1024,
            content_type="text/plain",
            uploaded_at_turn=1,
            uploaded_by="integration-test-user",
            upload_source="file_upload",
            summary="Application log with connection timeouts",
            structural_index="2024-01-01 12:00:00 ERROR: Connection timeout after 30s",
            data_type="logs",
        )
    )

    # Add the claim-anchored extract that points at it
    case.evidence.append(
        Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=EvidenceCategory.SYMPTOM_EVIDENCE,
            primary_purpose="symptom_verified",
            summary="Error logs showing connection timeouts",
            extract="2024-01-01 12:00:00 ERROR: Connection timeout after 30s",
            source_type=EvidenceSourceType.LOGS,
            source_file_id=file_id,
            collected_by="integration-test-user",
            collected_at_turn=1,
        )
    )

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
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
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
        state=HypothesisState.ACTIVE,
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
async def test_full_case_lifecycle(db_repository: SQLiteCaseRepository):
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
    assert retrieved.state == CaseState.INQUIRY

    # Step 3: Update case
    case.title = "Updated Lifecycle Test"
    # INVESTIGATING requires confirmed problem statement and decision - SET BEFORE STATUS CHANGE
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.decided_to_investigate = True
    case.inquiry.proposed_problem_statement = "Test problem statement"
    case.state = CaseState.INVESTIGATING
    case.current_turn = 5
    updated = await db_repository.save(case)
    assert updated.title == "Updated Lifecycle Test"

    # Verify update persisted
    retrieved_updated = await db_repository.get(case.case_id)
    assert retrieved_updated.title == "Updated Lifecycle Test"
    assert retrieved_updated.state == CaseState.INVESTIGATING

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
    db_repository: SQLiteCaseRepository, sample_case_with_evidence: Case
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
async def test_add_evidence_to_existing_case(db_repository: SQLiteCaseRepository):
    """Test adding evidence to an existing case.

    Post-010: evidence references its source file via source_file_id;
    the file must exist (FK constraint).
    """
    # Create initial case with a backing file
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="evidence-test-user",
        organization_id="evidence-test-org",
        title="Evidence Addition Test",
    )
    file_id = f"file_{uuid4().hex[:12]}"
    case.uploaded_files.append(
        UploadedFile(
            file_id=file_id,
            filename="metrics.json",
            size_bytes=512,
            content_type="application/json",
            uploaded_at_turn=3,
            uploaded_by="evidence-test-user",
            upload_source="file_upload",
            data_type="metrics",
            structural_index="Memory stats: used 7.6GB / 8GB",
        )
    )
    await db_repository.save(case)

    # Add evidence
    case.evidence.append(
        Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            primary_purpose="root_cause_identified",
            summary="Memory usage at 95%",
            extract="Memory stats: used 7.6GB / 8GB",
            source_type=EvidenceSourceType.METRICS,
            source_file_id=file_id,
            collected_by="evidence-test-user",
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
    db_repository: SQLiteCaseRepository, sample_case_with_hypotheses: Case
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
    assert hypothesis.state == HypothesisState.ACTIVE
    assert "connection pool" in hypothesis.statement.lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hypothesis_validation_flow(db_repository: SQLiteCaseRepository):
    """Test hypothesis lifecycle from proposed to validated."""
    # Create case
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="hypothesis-test-user",
        organization_id="hypothesis-test-org",
        title="Hypothesis Validation Test",
        description="Testing hypothesis validation flow",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
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
        state=HypothesisState.CAPTURED,
        likelihood=0.5,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="Connection count increases over time",
        generated_at_turn=1,
    )
    await db_repository.save(case)

    # Update to testing
    case.hypotheses[hyp_id].state = HypothesisState.ACTIVE
    case.hypotheses[hyp_id].likelihood = 0.7
    await db_repository.save(case)

    # Update to validated
    case.hypotheses[hyp_id].state = HypothesisState.VALIDATED
    case.hypotheses[hyp_id].likelihood = 0.95
    await db_repository.save(case)

    # Verify final state
    retrieved = await db_repository.get(case.case_id)
    assert retrieved.hypotheses[hyp_id].state == HypothesisState.VALIDATED
    assert retrieved.hypotheses[hyp_id].likelihood == 0.95


# ============================================================
# Concurrent Operations Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_case_creation(test_engine):
    """Test creating multiple cases concurrently.

    Each concurrent call gets its own session, matching production behavior
    where each request creates a session via get_db_session().
    """
    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def create_case(index: int) -> Case:
        async with session_factory() as session:
            repo = SQLiteCaseRepository(session)
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id="concurrent-test-user",
                organization_id="concurrent-test-org",
                title=f"Concurrent Case {index}",
            )
            return await repo.save(case)

    # Create 10 cases concurrently
    cases = await asyncio.gather(*[create_case(i) for i in range(10)])

    # Verify all cases created
    assert len(cases) == 10
    assert len(set(c.case_id for c in cases)) == 10  # All unique IDs

    # Verify all retrievable
    async with session_factory() as session:
        repo = SQLiteCaseRepository(session)
        for case in cases:
            retrieved = await repo.get(case.case_id)
            assert retrieved is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_message_addition(test_engine):
    """Test adding messages concurrently.

    Each concurrent call gets its own session, matching production behavior
    where each request creates a session via get_db_session(). A single
    AsyncSession is not safe for concurrent use.
    """
    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )

    # Create case with its own session
    case_id = f"case_{uuid4().hex[:12]}"
    async with session_factory() as session:
        repo = SQLiteCaseRepository(session)
        case = Case(
            case_id=case_id,
            user_id="concurrent-msg-user",
            organization_id="concurrent-msg-org",
            title="Concurrent Messages Test",
        )
        await repo.save(case)

    async def add_message(index: int) -> bool:
        async with session_factory() as session:
            repo = SQLiteCaseRepository(session)
            return await repo.add_message(
                case_id,
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
    async with session_factory() as session:
        repo = SQLiteCaseRepository(session)
        messages = await repo.get_messages(case_id, limit=20)
    assert len(messages) == 10


# ============================================================
# Error Handling Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_nonexistent_case(db_repository: SQLiteCaseRepository):
    """Test getting a case that doesn't exist."""
    result = await db_repository.get("case_doesnotexist")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_nonexistent_case(db_repository: SQLiteCaseRepository):
    """Test deleting a case that doesn't exist."""
    result = await db_repository.delete("case_doesnotexist")
    assert result is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_add_message_nonexistent_case(db_repository: SQLiteCaseRepository):
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
async def test_complex_case_persistence(db_repository: SQLiteCaseRepository):
    """Test persisting a case with all complex fields."""
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="complex-test-user",
        organization_id="complex-test-org",
        title="Complex Case Test",
        description="Testing all fields",
        state=CaseState.INVESTIGATING,
        investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        inquiry=InquiryData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            inquiry_turns=3,
        ),
    )

    # Add progress
    case.progress.symptom_verified = True

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
    assert retrieved.state == CaseState.INVESTIGATING
    assert retrieved.investigation_strategy == InvestigationStrategy.ACTIVE_INCIDENT
    assert retrieved.inquiry.inquiry_turns == 3
    assert retrieved.progress.symptom_verified is True
    assert retrieved.current_turn == 5
    assert retrieved.turns_without_progress == 1
    assert retrieved.message_count == 10


@pytest.mark.asyncio
@pytest.mark.integration
async def test_action_history_round_trip(db_repository: SQLiteCaseRepository):
    """CaseAction round-trips through case_actions, including triggered_by.

    Pre-migration-008, the table was write-only: ``action_history`` was
    hardcoded to ``[]`` in ``_to_domain``, and ``triggered_by`` had no
    column at all. This test pins both halves of the fix:
    - INSERT writes ``triggered_by`` (NOT NULL column from migration 008).
    - SELECT path hydrates ``action_history`` from rows, in chronological
      order, with the original ``triggered_by`` value preserved.
    """
    from datetime import datetime, timedelta, timezone

    from faultmaven.modules.case.domain.models import CaseAction

    case_id = f"case_{uuid4().hex[:12]}"
    case = Case(
        case_id=case_id,
        user_id="lifecycle-test-user",
        organization_id="lifecycle-test-org",
        title="Action Trail Test",
        description="Round-trip case_actions including triggered_by",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Pinning the audit trail",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )

    base_ts = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    case.action_history = [
        CaseAction(
            from_state=CaseState.INQUIRY,
            to_state=CaseState.INVESTIGATING,
            triggered_at=base_ts,
            triggered_by="lifecycle-test-user",
            reason="user confirmed problem",
        ),
        CaseAction(
            from_state=CaseState.INVESTIGATING,
            to_state=CaseState.CLOSED,
            triggered_at=base_ts + timedelta(seconds=30),
            triggered_by="system",
            reason="auto-closed: stale investigation",
        ),
    ]

    await db_repository.save(case)
    retrieved = await db_repository.get(case_id)

    assert retrieved is not None
    assert len(retrieved.action_history) == 2

    first, second = retrieved.action_history
    assert first.from_state == CaseState.INQUIRY
    assert first.to_state == CaseState.INVESTIGATING
    assert first.triggered_by == "lifecycle-test-user"
    assert first.reason == "user confirmed problem"

    assert second.from_state == CaseState.INVESTIGATING
    assert second.to_state == CaseState.CLOSED
    assert second.triggered_by == "system"
    assert second.reason == "auto-closed: stale investigation"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_solution_full_audit_round_trip(
    db_repository: SQLiteCaseRepository,
):
    """Solution round-trips with full audit trail across lifecycle stages.

    Pre-009 the repo persisted a hollow shell: ``proposed_by``,
    ``applied_at/by``, ``verified_at``, ``verification_method``,
    ``verification_evidence_id`` and ``effectiveness`` were all dropped
    or hardcoded ``None`` on insert; ``state`` was hardcoded
    ``"proposed"`` and the ON CONFLICT UPDATE clause omitted it. This
    test pins the post-009 fix by walking a Solution through three
    lifecycle stages — proposed -> implemented -> verified — and
    asserting every audit field survives the round-trip on each save.
    """
    from datetime import datetime, timedelta, timezone

    case_id = f"case_{uuid4().hex[:12]}"
    case = Case(
        case_id=case_id,
        user_id="complex-test-user",
        organization_id="complex-test-org",
        title="Solution Audit Round-trip",
        description="Pin the full audit trail across lifecycle stages",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="testing solution persistence",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )

    base_ts = datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
    solution = Solution(
        solution_id=f"sol_{uuid4().hex[:12]}",
        solution_type=SolutionType.RESTART,
        title="Restart the connection pool",
        immediate_action="Bounce the pool to clear leaked handles",
        longterm_fix="Add idle-connection reaper to drop stale handles",
        implementation_steps=["1. Drain traffic", "2. Restart pool", "3. Resume"],
        commands=["systemctl restart pgbouncer"],
        risks=["Brief connection blip during restart"],
        proposed_at=base_ts,
        proposed_by="agent",
    )
    case.solutions = [solution]

    # Stage 1: proposed --------------------------------------------------
    await db_repository.save(case)
    retrieved = await db_repository.get(case_id)
    assert retrieved is not None
    assert len(retrieved.solutions) == 1
    loaded = retrieved.solutions[0]

    assert loaded.solution_id == solution.solution_id
    assert loaded.title == "Restart the connection pool"
    assert loaded.proposed_by == "agent"
    assert loaded.proposed_at == base_ts
    assert loaded.applied_at is None
    assert loaded.applied_by is None
    assert loaded.verified_at is None
    assert loaded.verification_method is None
    assert loaded.verification_evidence_id is None
    assert loaded.effectiveness is None
    assert list(loaded.implementation_steps) == [
        "1. Drain traffic",
        "2. Restart pool",
        "3. Resume",
    ]
    assert list(loaded.commands) == ["systemctl restart pgbouncer"]
    assert list(loaded.risks) == ["Brief connection blip during restart"]

    # Stage 2: implemented ----------------------------------------------
    applied_ts = base_ts + timedelta(minutes=5)
    loaded.applied_at = applied_ts
    loaded.applied_by = "complex-test-user"
    retrieved.solutions = [loaded]

    await db_repository.save(retrieved)
    re_retrieved = await db_repository.get(case_id)
    assert re_retrieved is not None
    re_loaded = re_retrieved.solutions[0]

    assert re_loaded.applied_at == applied_ts
    assert re_loaded.applied_by == "complex-test-user"
    # proposed_by must survive update — pre-009 the lifecycle column was
    # silently overwritten on every upsert.
    assert re_loaded.proposed_by == "agent"
    assert re_loaded.verified_at is None
    assert re_loaded.effectiveness is None

    # Stage 3: verified --------------------------------------------------
    verified_ts = applied_ts + timedelta(minutes=10)
    re_loaded.verified_at = verified_ts
    re_loaded.verification_method = "Confirmed pool size returned to baseline"
    re_loaded.effectiveness = 0.95
    re_retrieved.solutions = [re_loaded]

    await db_repository.save(re_retrieved)
    final = await db_repository.get(case_id)
    assert final is not None
    final_solution = final.solutions[0]

    assert final_solution.verified_at == verified_ts
    assert (
        final_solution.verification_method == "Confirmed pool size returned to baseline"
    )
    assert final_solution.effectiveness == 0.95
    # All earlier-stage fields must still be present.
    assert final_solution.proposed_by == "agent"
    assert final_solution.applied_at == applied_ts
    assert final_solution.applied_by == "complex-test-user"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_evidence_full_audit_round_trip(
    db_repository: SQLiteCaseRepository,
):
    """Evidence round-trips with the five fields added by migration 009.

    Pre-009 the repo dropped ``primary_purpose``, ``analysis``,
    ``processing_mode``, ``advances_milestones`` and ``collected_by``
    on persist; the read path hardcoded ``primary_purpose="loaded_evidence"``
    and ``collected_by="user"`` placeholders, masking the loss. This
    test pins the post-009 fix: every field round-trips faithfully on
    both insert and update paths.
    """
    case_id = f"case_{uuid4().hex[:12]}"
    case = Case(
        case_id=case_id,
        user_id="evidence-test-user",
        organization_id="evidence-test-org",
        title="Evidence Audit Round-trip",
        description="Pin the 5 evidence fields added by migration 009",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="testing evidence persistence",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )

    # Post-010: evidence FKs require a matching UploadedFile row
    file_id = f"file_{uuid4().hex[:12]}"
    case.uploaded_files.append(
        UploadedFile(
            file_id=file_id,
            filename="pool.log",
            size_bytes=2048,
            content_type="text/plain",
            uploaded_at_turn=3,
            uploaded_by="evidence-test-user",
            upload_source="file_upload",
            data_type="logs",
        )
    )

    evidence = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        primary_purpose="hypothesis-test",
        summary="Pool exhaustion correlated with deploy",
        extract="2026-05-10T08:59:31Z pool=100/100 (full)",
        analysis=(
            "Pool fully saturated 30s before first error wave; "
            "directly supports the 'pool exhaustion' hypothesis."
        ),
        processing_mode="directed_analysis",
        source_type=EvidenceSourceType.LOGS,
        source_file_id=file_id,
        is_primary=True,
        tags=["pool", "saturation", "deploy-correlation"],
        advances_milestones=["root_cause_identified"],
        collected_by="evidence-test-user",
        collected_at_turn=3,
    )
    case.evidence = [evidence]

    # Insert path -------------------------------------------------------
    await db_repository.save(case)
    retrieved = await db_repository.get(case_id)
    assert retrieved is not None
    assert len(retrieved.evidence) == 1
    loaded = retrieved.evidence[0]

    assert loaded.evidence_id == evidence.evidence_id
    # The five migration-009 columns the audit was created to surface:
    assert loaded.primary_purpose == "hypothesis-test"
    assert loaded.analysis == evidence.analysis
    assert loaded.processing_mode == "directed_analysis"
    assert sorted(loaded.advances_milestones) == ["root_cause_identified"]
    assert loaded.collected_by == "evidence-test-user"
    # Existing audit data must keep round-tripping.
    assert loaded.summary == "Pool exhaustion correlated with deploy"
    assert sorted(loaded.tags) == sorted(evidence.tags)
    assert loaded.is_primary is True
    assert loaded.collected_at_turn == 3

    # Update path: mutate the new fields and re-save -------------------
    loaded.analysis = "After re-review with broader context, this is the smoking gun."
    loaded.processing_mode = "semantic_search"
    loaded.advances_milestones = [
        "root_cause_identified",
        "solution_proposed",
    ]
    retrieved.evidence = [loaded]

    await db_repository.save(retrieved)
    re_retrieved = await db_repository.get(case_id)
    assert re_retrieved is not None
    re_loaded = re_retrieved.evidence[0]

    assert re_loaded.analysis == loaded.analysis
    assert re_loaded.processing_mode == "semantic_search"
    assert sorted(re_loaded.advances_milestones) == [
        "root_cause_identified",
        "solution_proposed",
    ]
    # Untouched fields must survive the update.
    assert re_loaded.primary_purpose == "hypothesis-test"
    assert re_loaded.collected_by == "evidence-test-user"
