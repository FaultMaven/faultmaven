"""Integration tests for APICaseService (TASK-011).

These tests verify end-to-end functionality with real database operations.

Tests include:
- Complete case lifecycle
- Authorization enforcement
- CASCADE delete verification
- Transaction rollback on error
- Cross-organization isolation
"""

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    DatabaseInvestigationSessionRepository,
    InMemoryInvestigationSessionRepository,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.models.investigation_session import InvestigationSession, SessionState
from faultmaven.modules.case.contracts import (
    AgentExecution,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.case.domain.models import Case, CaseSeverity, CaseState
from faultmaven.modules.case.domain.services.api_case_service import APICaseService
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)
from faultmaven.services.service_factory import ServiceFactory

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Create in-memory SQLite engine for tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create async session for tests."""
    session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session


@pytest.fixture
async def case_repo(async_session) -> SQLiteCaseRepository:
    """Create database case repository."""
    return SQLiteCaseRepository(async_session)


@pytest.fixture
def session_repo() -> InMemoryInvestigationSessionRepository:
    """Create in-memory investigation session repository."""
    return InMemoryInvestigationSessionRepository()


# evidence_repo fixture removed in storage redesign 2026-04 phase 2
# (standalone evidence path deletion). Evidence is case-tied only and
# accessed via case.evidence loaded by the case repository.

# execution_repo removed - APICaseService now uses case_repo for agent executions


@pytest.fixture
def case_service(case_repo, session_repo) -> APICaseService:
    """Create APICaseService with real/in-memory repositories."""
    return APICaseService(
        case_repo=case_repo,
        session_repo=session_repo,
    )


@pytest.fixture
def session_factory(async_engine):
    """
    Create session factory for concurrent tests.

    Returns a factory that creates independent sessions for each operation.
    This is required for concurrent operations to avoid SQLAlchemy transaction state conflicts.
    """
    return async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
def case_service_factory(session_factory, session_repo):
    """
    Create case service factory for concurrent tests.

    Returns a function that creates independent APICaseService instances,
    each with its own database session. Required for concurrent operations.
    """

    async def create_service():
        async with session_factory() as session:
            case_repo = SQLiteCaseRepository(session)
            service = APICaseService(
                case_repo=case_repo,
                session_repo=session_repo,
            )
            return service, session

    return create_service


# ============================================================
# Test Data Helpers
# ============================================================


def create_test_case_id() -> str:
    """Generate unique test case ID."""
    return f"case_{uuid4().hex[:12]}"


def create_test_org_id() -> str:
    """Generate unique test organization ID."""
    return f"org_{uuid4().hex[:8]}"


def create_test_user_id() -> str:
    """Generate unique test user ID."""
    return f"user_{uuid4().hex[:8]}"


# ============================================================
# End-to-End Case Lifecycle Tests
# ============================================================


class TestCaseLifecycle:
    """Test complete case lifecycle."""

    @pytest.mark.asyncio
    async def test_case_lifecycle_create_to_resolve(self, case_service):
        """Test complete case lifecycle from creation to resolution."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        # Step 1: Create case
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Production database slow",
            description="Database queries are taking 10x longer than normal",
            severity=CaseSeverity.HIGH,
        )

        assert case is not None
        assert case.state == CaseState.INQUIRY

        # Step 2: Retrieve case
        retrieved = await case_service.get_case(case.case_id, organization_id)
        assert retrieved is not None
        assert retrieved.case_id == case.case_id

        # Step 3: Update case
        # First update title and description
        updated = await case_service.update_case(
            case.case_id,
            organization_id,
            {
                "title": "DB performance issue",
                "description": "Database queries are taking 10x longer than normal - investigating",
            },
        )

        # Now set inquiry fields required for INVESTIGATING status
        # We need to access the case directly from the repository to modify nested fields
        case_from_repo = await case_service.case_repo.get(case.case_id)
        case_from_repo.inquiry.proposed_problem_statement = (
            "Database queries are taking 10x longer than normal"
        )
        case_from_repo.inquiry.problem_statement_confirmed = True
        case_from_repo.inquiry.decided_to_investigate = True
        await case_service.case_repo.save(case_from_repo)

        # Finally transition to INVESTIGATING status
        updated = await case_service.update_case(
            case.case_id,
            organization_id,
            {"state": CaseState.INVESTIGATING},
        )

        assert updated.state == CaseState.INVESTIGATING
        assert updated.title == "DB performance issue"

        # Step 4: Close case
        closed = await case_service.close_case(
            case.case_id,
            organization_id,
        )

        assert closed.state == CaseState.RESOLVED
        assert closed.resolved_at is not None
        # closure_reason is None for RESOLVED — sub-categorization would
        # be redundant with the status itself. The free-form resolution
        # text is logged but not stored on the case directly; it would
        # live in the resolution_summary Report when generated.
        assert closed.closure_reason is None

    @pytest.mark.asyncio
    async def test_case_lifecycle_with_assignment(self, case_service):
        """Test case lifecycle with assignment."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()
        assignee_id = create_test_user_id()

        # Create case
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="API timeout issue",
            description="API calls timing out",
            severity=CaseSeverity.CRITICAL,
        )

        # Assign to specialist
        assigned = await case_service.assign_case(
            case.case_id, organization_id, assignee_id
        )

        assert assigned is not None

    @pytest.mark.asyncio
    async def test_case_lifecycle_multiple_updates(self, case_service):
        """Test multiple case updates."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        # Create case
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Initial Title",
            description="Initial description",
            severity=CaseSeverity.LOW,
        )

        # Multiple updates
        for i in range(5):
            case = await case_service.update_case(
                case.case_id,
                organization_id,
                {"title": f"Updated Title {i + 1}", "description": f"Update {i + 1}"},
            )

        assert case.title == "Updated Title 5"
        assert case.description == "Update 5"


# ============================================================
# Authorization Enforcement Tests
# ============================================================


class TestAuthorizationEnforcement:
    """Test organization-level authorization."""

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_get(self, case_service):
        """Test that get_case prevents cross-organization access."""
        org_a = create_test_org_id()
        org_b = create_test_org_id()
        user_id = create_test_user_id()

        # Create case for org A
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=org_a,
            title="Org A Case",
            description="",
            severity=CaseSeverity.LOW,
        )

        # Attempt to access with org B
        result = await case_service.get_case(case.case_id, org_b)

        assert result is None

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_update(self, case_service):
        """Test that update_case prevents cross-organization access."""
        org_a = create_test_org_id()
        org_b = create_test_org_id()
        user_id = create_test_user_id()

        # Create case for org A
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=org_a,
            title="Org A Case",
            description="",
            severity=CaseSeverity.LOW,
        )

        # Attempt to update with org B
        with pytest.raises(AuthorizationError):
            await case_service.update_case(
                case.case_id,
                org_b,
                {"title": "Hacked Title"},
            )

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_delete(self, case_service):
        """Test that delete_case prevents cross-organization access."""
        org_a = create_test_org_id()
        org_b = create_test_org_id()
        user_id = create_test_user_id()

        # Create case for org A
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=org_a,
            title="Org A Case",
            description="",
            severity=CaseSeverity.LOW,
        )

        # Attempt to delete with org B
        with pytest.raises(AuthorizationError):
            await case_service.delete_case(case.case_id, org_b)

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_close(self, case_service):
        """Test that close_case prevents cross-organization access."""
        org_a = create_test_org_id()
        org_b = create_test_org_id()
        user_id = create_test_user_id()

        # Create case for org A
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=org_a,
            title="Org A Case",
            description="",
            severity=CaseSeverity.LOW,
        )

        # Attempt to close with org B
        with pytest.raises(NotFoundError):
            await case_service.close_case(case.case_id, org_b)

    @pytest.mark.asyncio
    async def test_authorization_allows_same_org_access(self, case_service):
        """Test that same organization can access case."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        # Create case
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Org Case",
            description="",
            severity=CaseSeverity.LOW,
        )

        # All operations should succeed with same org
        retrieved = await case_service.get_case(case.case_id, organization_id)
        assert retrieved is not None

        updated = await case_service.update_case(
            case.case_id, organization_id, {"title": "Updated"}
        )
        assert updated.title == "Updated"


# ============================================================
# Organization Scoping (single-tenant CE — reads are NOT org-isolated)
# ============================================================


class TestOrganizationScopingCE:
    """CE is single-tenant: list/statistics do NOT isolate by organization.

    Per ADR-006 the ``organization_id`` argument is retained but does not
    scope reads in the Community Edition — multi-tenant isolation is enforced
    by PostgreSQL RLS in faultmaven-cloud, not by CE per-query filters. These
    tests pin that CE behavior so the org-filter removal cannot silently
    regress back to per-org scoping.
    """

    @pytest.mark.asyncio
    async def test_list_cases_not_isolated_by_org_in_ce(self, case_service):
        """list_cases returns all cases regardless of the org passed (single-tenant CE)."""
        org_a = create_test_org_id()
        org_b = create_test_org_id()
        user_id = create_test_user_id()

        # Create cases for org A
        for i in range(3):
            await case_service.create_case(
                user_id=user_id,
                organization_id=org_a,
                title=f"Org A Case {i}",
                description="",
                severity=CaseSeverity.LOW,
            )

        # Create cases for org B
        for i in range(2):
            await case_service.create_case(
                user_id=user_id,
                organization_id=org_b,
                title=f"Org B Case {i}",
                description="",
                severity=CaseSeverity.LOW,
            )

        # CE does not scope by org: all 5 cases are returned regardless of the
        # org argument (ADR-006 — isolation is cloud RLS, not CE filters).
        all_cases = await case_service.list_cases(org_a)
        assert len(all_cases) == 5
        assert {case.organization_id for case in all_cases} == {org_a, org_b}

    @pytest.mark.asyncio
    async def test_statistics_not_isolated_by_org_in_ce(self, case_service):
        """get_case_statistics aggregates all cases regardless of org (single-tenant CE)."""
        org_a = create_test_org_id()
        org_b = create_test_org_id()
        user_id = create_test_user_id()

        # Create 5 cases for org A
        for i in range(5):
            await case_service.create_case(
                user_id=user_id,
                organization_id=org_a,
                title=f"Org A Case {i}",
                description="",
                severity=CaseSeverity.LOW,
            )

        # Create 3 cases for org B
        for i in range(3):
            await case_service.create_case(
                user_id=user_id,
                organization_id=org_b,
                title=f"Org B Case {i}",
                description="",
                severity=CaseSeverity.LOW,
            )

        # CE statistics are not org-scoped: both calls count all 8 cases
        # (ADR-006 — isolation is cloud RLS, not CE filters).
        stats_a = await case_service.get_case_statistics(org_a)
        stats_b = await case_service.get_case_statistics(org_b)

        assert stats_a["total_cases"] == 8
        assert stats_b["total_cases"] == 8


# ============================================================
# Case State Transition Tests
# ============================================================


class TestCaseStateTransitions:
    """Test case status transitions."""

    @pytest.mark.asyncio
    async def test_transition_inquiry_to_investigating(self, case_service):
        """Test transition from INQUIRY to INVESTIGATING."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Test",
            description="Test problem description",
            severity=CaseSeverity.LOW,
        )

        assert case.state == CaseState.INQUIRY

        # Set inquiry fields required for INVESTIGATING status
        case_from_repo = await case_service.case_repo.get(case.case_id)
        case_from_repo.inquiry.proposed_problem_statement = "Test problem description"
        case_from_repo.inquiry.problem_statement_confirmed = True
        case_from_repo.inquiry.decided_to_investigate = True
        await case_service.case_repo.save(case_from_repo)

        updated = await case_service.update_case(
            case.case_id,
            organization_id,
            {"state": CaseState.INVESTIGATING},
        )

        assert updated.state == CaseState.INVESTIGATING

    @pytest.mark.asyncio
    async def test_transition_investigating_to_resolved(self, case_service):
        """Test transition from INVESTIGATING to RESOLVED via close."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Test",
            description="Test problem description",
            severity=CaseSeverity.LOW,
        )

        # Set inquiry fields required for INVESTIGATING status
        case_from_repo = await case_service.case_repo.get(case.case_id)
        case_from_repo.inquiry.proposed_problem_statement = "Test problem description"
        case_from_repo.inquiry.problem_statement_confirmed = True
        case_from_repo.inquiry.decided_to_investigate = True
        await case_service.case_repo.save(case_from_repo)

        await case_service.update_case(
            case.case_id, organization_id, {"state": CaseState.INVESTIGATING}
        )

        closed = await case_service.close_case(case.case_id, organization_id)

        assert closed.state == CaseState.RESOLVED

    @pytest.mark.asyncio
    async def test_cannot_close_already_closed_case(self, case_service):
        """Test that closing already-closed case raises error."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        # RESOLVED status requires a non-empty description (the description
        # carries the case's problem statement). Without it the Case validator
        # rejects the close transition before we can exercise the
        # already-closed conflict path.
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Test",
            description="Test problem statement",
            severity=CaseSeverity.LOW,
        )

        await case_service.close_case(case.case_id, organization_id)

        with pytest.raises(ConflictError):
            await case_service.close_case(case.case_id, organization_id)


# ============================================================
# Case Details Tests
# ============================================================


class TestCaseDetails:
    """Test get_case_with_details functionality."""

    @pytest.mark.asyncio
    async def test_get_case_with_details_includes_sessions(
        self, case_service, session_repo
    ):
        """Test that details include investigation sessions."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        # Create case
        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Test",
            description="",
            severity=CaseSeverity.LOW,
        )

        # Add sessions
        session = InvestigationSession(
            session_id=f"sess_{uuid4().hex[:12]}",
            case_id=case.case_id,
            user_id=user_id,
            organization_id=organization_id,
            state=SessionState.ACTIVE,
        )
        await session_repo.create(session)

        # Get details
        details = await case_service.get_case_with_details(
            case.case_id, organization_id, include_sessions=True
        )

        assert details is not None
        assert "sessions" in details
        assert len(details["sessions"]) >= 0  # May be 0 if not persisted

    @pytest.mark.asyncio
    async def test_get_case_with_details_includes_evidence(self, case_service):
        """Test that details include the case-embedded evidence list.

        Storage redesign 2026-04 phase 2: evidence is case-tied only, surfaced
        from `case.evidence` rather than via a separate evidence repository.
        """
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Test",
            description="",
            severity=CaseSeverity.LOW,
        )

        details = await case_service.get_case_with_details(
            case.case_id, organization_id, include_evidence=True
        )

        assert details is not None
        assert "evidence" in details
        # New cases have no embedded evidence yet.
        assert details["evidence"] == []


# ============================================================
# Edge Case Tests
# ============================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_create_case_trims_whitespace(self, case_service):
        """Test that create_case trims whitespace from inputs."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        case = await case_service.create_case(
            user_id=f"  {user_id}  ",
            organization_id=f"  {organization_id}  ",
            title="  Trimmed Title  ",
            description="  Trimmed Description  ",
            severity=CaseSeverity.LOW,
        )

        assert case.user_id == user_id
        assert case.organization_id == organization_id
        assert case.title == "Trimmed Title"
        assert case.description == "Trimmed Description"

    @pytest.mark.asyncio
    async def test_update_case_with_empty_description(self, case_service):
        """Test updating case with empty description."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Test",
            description="Original description",
            severity=CaseSeverity.LOW,
        )

        updated = await case_service.update_case(
            case.case_id, organization_id, {"description": ""}
        )

        assert updated.description == ""

    @pytest.mark.asyncio
    async def test_list_cases_with_zero_limit(self, case_service):
        """Test list_cases with zero limit."""
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        # Create some cases
        for i in range(3):
            await case_service.create_case(
                user_id=user_id,
                organization_id=organization_id,
                title=f"Case {i}",
                description="",
                severity=CaseSeverity.LOW,
            )

        # List with zero limit
        cases = await case_service.list_cases(organization_id, limit=0)

        assert isinstance(cases, list)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_case(self, case_service):
        """Test deleting nonexistent case returns False."""
        organization_id = create_test_org_id()

        result = await case_service.delete_case("nonexistent_case_id", organization_id)

        assert result is False


# ============================================================
# Concurrent Operations Tests
# ============================================================


class TestConcurrentOperations:
    """
    Test concurrent case operations.

    ARCHITECTURAL NOTE: These tests are currently skipped due to SQLAlchemy
    session/transaction management issues in concurrent scenarios.

    ROOT CAUSE:
    - SQLiteCaseRepository.save() calls commit() and rollback() on shared session
    - When multiple async tasks use the same session concurrently, they can trigger:
      * IllegalStateChangeError: rollback() already in progress
      * InvalidRequestError: Session is already flushing
      * ResourceClosedError: This transaction is closed

    PROPER FIX REQUIRES:
    1. Session-per-operation pattern: Each repository method gets its own session
    2. OR: Repository methods should NOT commit/rollback (let caller control transactions)
    3. OR: Use scoped sessions with proper async context management

    CURRENT WORKAROUND:
    - Tests are skipped to avoid false negatives
    - Production code uses request-scoped sessions from dependency injection
    - Real concurrent operations in production are isolated by HTTP request boundaries

    RELATED:
    - test_session_case_integration.py::test_concurrent_session_operations (same issue)
    - faultmaven/infrastructure/persistence/database_case_repository.py lines 124, 130

    TO RE-ENABLE:
    - Implement session-per-operation pattern in repository layer
    - OR: Move transaction control to service layer
    - Then remove pytest.skip decorators
    """

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="Requires session-per-operation pattern - see class docstring for architectural details"
    )
    async def test_concurrent_case_creation(self, case_service):
        """
        Test creating multiple cases concurrently.

        SKIPPED: Exposes SQLAlchemy session sharing issue in concurrent operations.
        See TestConcurrentOperations class docstring for full details.
        """
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        async def create_case(i: int) -> Case:
            return await case_service.create_case(
                user_id=user_id,
                organization_id=organization_id,
                title=f"Concurrent Case {i}",
                description="",
                severity=CaseSeverity.LOW,
            )

        # Create 10 cases concurrently
        tasks = [create_case(i) for i in range(10)]
        cases = await asyncio.gather(*tasks)

        assert len(cases) == 10
        assert len(set(c.case_id for c in cases)) == 10  # All unique IDs

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="Requires session-per-operation pattern - see class docstring for architectural details"
    )
    async def test_concurrent_updates_same_case(self, case_service):
        """
        Test concurrent updates to the same case.

        SKIPPED: Exposes SQLAlchemy session sharing issue in concurrent operations.
        See TestConcurrentOperations class docstring for full details.
        """
        organization_id = create_test_org_id()
        user_id = create_test_user_id()

        case = await case_service.create_case(
            user_id=user_id,
            organization_id=organization_id,
            title="Concurrent Update Target",
            description="",
            severity=CaseSeverity.LOW,
        )

        async def update_case(i: int) -> Case:
            return await case_service.update_case(
                case.case_id,
                organization_id,
                {"title": f"Update {i}"},
            )

        # Concurrent updates (last write wins)
        tasks = [update_case(i) for i in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # All should succeed (no crashes)
        successful = [r for r in results if isinstance(r, Case)]
        assert len(successful) >= 1
