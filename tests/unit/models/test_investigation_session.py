"""Unit tests for Investigation Session domain models.

Tests the InvestigationSession dataclass and SessionState enum.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.models.investigation_session import InvestigationSession, SessionState


class TestInvestigationSessionValidation:
    """Tests for InvestigationSession validation."""

    def test_empty_session_id_fails(self):
        """Test that empty session_id raises ValueError."""
        with pytest.raises(ValueError, match="session_id is required"):
            InvestigationSession(
                session_id="",
                case_id="case_123",
                user_id="user_456",
                enterprise_id="org_789",
            )

    def test_empty_case_id_fails(self):
        """Test that empty case_id raises ValueError."""
        with pytest.raises(ValueError, match="case_id is required"):
            InvestigationSession(
                session_id="sess_123",
                case_id="",
                user_id="user_456",
                enterprise_id="org_789",
            )

    def test_empty_user_id_fails(self):
        """Test that empty user_id raises ValueError."""
        with pytest.raises(ValueError, match="user_id is required"):
            InvestigationSession(
                session_id="sess_123",
                case_id="case_456",
                user_id="",
                enterprise_id="org_789",
            )

    def test_empty_organization_id_fails(self):
        """Test that empty organization_id raises ValueError."""
        with pytest.raises(ValueError, match="organization_id is required"):
            InvestigationSession(
                session_id="sess_123",
                case_id="case_456",
                user_id="user_789",
                enterprise_id="",
            )

    def test_negative_token_usage_fails(self):
        """Test that negative total_token_usage raises ValueError."""
        with pytest.raises(ValueError, match="total_token_usage cannot be negative"):
            InvestigationSession(
                session_id="sess_123",
                case_id="case_456",
                user_id="user_789",
                enterprise_id="org_012",
                total_token_usage=-100,
            )

    def test_negative_agent_executions_fails(self):
        """Test that negative total_agent_executions raises ValueError."""
        with pytest.raises(
            ValueError, match="total_agent_executions cannot be negative"
        ):
            InvestigationSession(
                session_id="sess_123",
                case_id="case_456",
                user_id="user_789",
                enterprise_id="org_012",
                total_agent_executions=-1,
            )

    def test_negative_budget_limit_fails(self):
        """Test that negative token_budget_limit raises ValueError."""
        with pytest.raises(ValueError, match="token_budget_limit cannot be negative"):
            InvestigationSession(
                session_id="sess_123",
                case_id="case_456",
                user_id="user_789",
                enterprise_id="org_012",
                token_budget_limit=-1000,
            )

    def test_negative_duration_fails(self):
        """Test that negative total_duration_ms raises ValueError."""
        with pytest.raises(ValueError, match="total_duration_ms cannot be negative"):
            InvestigationSession(
                session_id="sess_123",
                case_id="case_456",
                user_id="user_789",
                enterprise_id="org_012",
                total_duration_ms=-1000,
            )

    def test_ended_before_started_fails(self):
        """Test that ended_at before started_at raises ValueError."""
        started = datetime(2025, 12, 29, 10, 0, 0, tzinfo=timezone.utc)
        ended = datetime(2025, 12, 29, 9, 0, 0, tzinfo=timezone.utc)  # Before started

        with pytest.raises(ValueError, match="ended_at must be after started_at"):
            InvestigationSession(
                session_id="sess_123",
                case_id="case_456",
                user_id="user_789",
                enterprise_id="org_012",
                started_at=started,
                ended_at=ended,
            )

    def test_zero_values_allowed(self):
        """Test that zero values are allowed for counters."""
        session = InvestigationSession(
            session_id="sess_123",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            total_token_usage=0,
            total_agent_executions=0,
            token_budget_limit=0,
            total_duration_ms=0,
        )

        assert session.total_token_usage == 0
        assert session.total_agent_executions == 0
        assert session.token_budget_limit == 0
        assert session.total_duration_ms == 0


class TestInvestigationSessionLifecycleMethods:
    """Tests for InvestigationSession lifecycle methods."""

    @pytest.fixture
    def sample_session(self):
        """Create sample session for testing."""
        return InvestigationSession(
            session_id="sess_sample123",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            session_goal="Investigate timeout issue",
        )

    def test_pause_from_active(self, sample_session):
        """Test pause() transitions from ACTIVE to PAUSED."""
        before = datetime.now(timezone.utc)
        sample_session.pause()
        after = datetime.now(timezone.utc)

        assert sample_session.state == SessionState.PAUSED
        assert before <= sample_session.updated_at <= after
        assert sample_session.total_duration_ms is not None

    def test_pause_from_paused_fails(self, sample_session):
        """Test pause() fails if already paused."""
        sample_session.pause()

        with pytest.raises(ValueError, match="Only active sessions can be paused"):
            sample_session.pause()

    def test_pause_from_completed_fails(self, sample_session):
        """Test pause() fails if completed."""
        sample_session.complete("Findings here")

        with pytest.raises(ValueError, match="Only active sessions can be paused"):
            sample_session.pause()

    def test_resume_from_paused(self, sample_session):
        """Test resume() transitions from PAUSED to ACTIVE."""
        sample_session.pause()

        before = datetime.now(timezone.utc)
        sample_session.resume()
        after = datetime.now(timezone.utc)

        assert sample_session.state == SessionState.ACTIVE
        assert before <= sample_session.updated_at <= after
        assert before <= sample_session.last_activity_at <= after

    def test_resume_from_active_fails(self, sample_session):
        """Test resume() fails if already active."""
        with pytest.raises(ValueError, match="Only paused sessions can be resumed"):
            sample_session.resume()

    def test_resume_from_completed_fails(self, sample_session):
        """Test resume() fails if completed."""
        sample_session.complete("Findings here")

        with pytest.raises(ValueError, match="Only paused sessions can be resumed"):
            sample_session.resume()

    def test_complete_from_active(self, sample_session):
        """Test complete() from ACTIVE state."""
        before = datetime.now(timezone.utc)
        sample_session.complete("Root cause identified: DB connection leak")
        after = datetime.now(timezone.utc)

        assert sample_session.state == SessionState.COMPLETED
        assert (
            sample_session.findings_summary
            == "Root cause identified: DB connection leak"
        )
        assert before <= sample_session.ended_at <= after
        assert before <= sample_session.updated_at <= after
        assert sample_session.total_duration_ms is not None

    def test_complete_from_paused(self, sample_session):
        """Test complete() from PAUSED state."""
        sample_session.pause()
        sample_session.complete("Findings from paused session")

        assert sample_session.state == SessionState.COMPLETED
        assert sample_session.findings_summary == "Findings from paused session"

    def test_complete_from_completed_fails(self, sample_session):
        """Test complete() fails if already completed."""
        sample_session.complete("First findings")

        with pytest.raises(ValueError, match="Session is already in a terminal state"):
            sample_session.complete("Second findings")

    def test_complete_from_abandoned_fails(self, sample_session):
        """Test complete() fails if already abandoned."""
        sample_session.abandon()

        with pytest.raises(ValueError, match="Session is already in a terminal state"):
            sample_session.complete("Late findings")

    def test_abandon_from_active(self, sample_session):
        """Test abandon() from ACTIVE state."""
        before = datetime.now(timezone.utc)
        sample_session.abandon()
        after = datetime.now(timezone.utc)

        assert sample_session.state == SessionState.ABANDONED
        assert before <= sample_session.ended_at <= after
        assert before <= sample_session.updated_at <= after
        assert sample_session.total_duration_ms is not None

    def test_abandon_from_paused(self, sample_session):
        """Test abandon() from PAUSED state."""
        sample_session.pause()
        sample_session.abandon()

        assert sample_session.state == SessionState.ABANDONED

    def test_abandon_from_completed_fails(self, sample_session):
        """Test abandon() fails if already completed."""
        sample_session.complete("Findings here")

        with pytest.raises(ValueError, match="Session is already in a terminal state"):
            sample_session.abandon()

    def test_abandon_from_abandoned_fails(self, sample_session):
        """Test abandon() fails if already abandoned."""
        sample_session.abandon()

        with pytest.raises(ValueError, match="Session is already in a terminal state"):
            sample_session.abandon()


class TestInvestigationSessionAgentExecution:
    """Tests for add_agent_execution method."""

    @pytest.fixture
    def sample_session(self):
        """Create sample session for testing."""
        return InvestigationSession(
            session_id="sess_exec",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
        )

    def test_add_agent_execution(self, sample_session):
        """Test add_agent_execution increments counters."""
        before = datetime.now(timezone.utc)
        sample_session.add_agent_execution(token_usage=500)
        after = datetime.now(timezone.utc)

        assert sample_session.total_token_usage == 500
        assert sample_session.total_agent_executions == 1
        assert before <= sample_session.last_activity_at <= after
        assert before <= sample_session.updated_at <= after

    def test_add_multiple_executions(self, sample_session):
        """Test multiple add_agent_execution calls accumulate."""
        sample_session.add_agent_execution(token_usage=500)
        sample_session.add_agent_execution(token_usage=300)
        sample_session.add_agent_execution(token_usage=200)

        assert sample_session.total_token_usage == 1000
        assert sample_session.total_agent_executions == 3

    def test_add_agent_execution_zero_tokens(self, sample_session):
        """Test add_agent_execution with zero tokens."""
        sample_session.add_agent_execution(token_usage=0)

        assert sample_session.total_token_usage == 0
        assert sample_session.total_agent_executions == 1

    def test_add_agent_execution_negative_tokens_fails(self, sample_session):
        """Test add_agent_execution with negative tokens fails."""
        with pytest.raises(ValueError, match="token_usage cannot be negative"):
            sample_session.add_agent_execution(token_usage=-100)

    def test_add_agent_execution_when_paused_fails(self, sample_session):
        """Test add_agent_execution fails when session is paused."""
        sample_session.pause()

        with pytest.raises(ValueError, match="Session must be active"):
            sample_session.add_agent_execution(token_usage=500)

    def test_add_agent_execution_when_completed_fails(self, sample_session):
        """Test add_agent_execution fails when session is completed."""
        sample_session.complete("Done")

        with pytest.raises(ValueError, match="Session must be active"):
            sample_session.add_agent_execution(token_usage=500)


class TestInvestigationSessionStatusChecks:
    """Tests for state check methods."""

    @pytest.fixture
    def sample_session(self):
        """Create sample session for testing."""
        return InvestigationSession(
            session_id="sess_status",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
        )

    def test_is_active(self, sample_session):
        """Test is_active() method."""
        assert sample_session.is_active() is True

        sample_session.pause()
        assert sample_session.is_active() is False

        sample_session.resume()
        assert sample_session.is_active() is True

        sample_session.complete("Done")
        assert sample_session.is_active() is False

    def test_is_paused(self, sample_session):
        """Test is_paused() method."""
        assert sample_session.is_paused() is False

        sample_session.pause()
        assert sample_session.is_paused() is True

        sample_session.resume()
        assert sample_session.is_paused() is False

    def test_is_completed(self, sample_session):
        """Test is_completed() for terminal states."""
        assert sample_session.is_completed() is False

        sample_session.complete("Done")
        assert sample_session.is_completed() is True

    def test_is_completed_when_abandoned(self, sample_session):
        """Test is_completed() returns True for abandoned sessions."""
        sample_session.abandon()
        assert sample_session.is_completed() is True


class TestInvestigationSessionBudget:
    """Tests for budget-related methods."""

    @pytest.fixture
    def session_with_budget(self):
        """Create session with budget limit."""
        return InvestigationSession(
            session_id="sess_budget",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            token_budget_limit=1000,
        )

    @pytest.fixture
    def session_without_budget(self):
        """Create session without budget limit."""
        return InvestigationSession(
            session_id="sess_no_budget",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
        )

    def test_is_over_budget_false_when_under(self, session_with_budget):
        """Test is_over_budget() returns False when under budget."""
        session_with_budget.add_agent_execution(token_usage=500)
        assert session_with_budget.is_over_budget() is False

    def test_is_over_budget_false_when_at_budget(self, session_with_budget):
        """Test is_over_budget() returns False when exactly at budget."""
        session_with_budget.add_agent_execution(token_usage=1000)
        assert session_with_budget.is_over_budget() is False

    def test_is_over_budget_true_when_over(self, session_with_budget):
        """Test is_over_budget() returns True when over budget."""
        session_with_budget.add_agent_execution(token_usage=1001)
        assert session_with_budget.is_over_budget() is True

    def test_is_over_budget_false_when_no_limit(self, session_without_budget):
        """Test is_over_budget() returns False when no budget limit set."""
        session_without_budget.add_agent_execution(token_usage=1000000)
        assert session_without_budget.is_over_budget() is False

    def test_get_remaining_budget_positive(self, session_with_budget):
        """Test get_remaining_budget() when under budget."""
        session_with_budget.add_agent_execution(token_usage=300)
        assert session_with_budget.get_remaining_budget() == 700

    def test_get_remaining_budget_zero(self, session_with_budget):
        """Test get_remaining_budget() when at budget."""
        session_with_budget.add_agent_execution(token_usage=1000)
        assert session_with_budget.get_remaining_budget() == 0

    def test_get_remaining_budget_negative(self, session_with_budget):
        """Test get_remaining_budget() when over budget."""
        session_with_budget.add_agent_execution(token_usage=1500)
        assert session_with_budget.get_remaining_budget() == -500

    def test_get_remaining_budget_none_when_no_limit(self, session_without_budget):
        """Test get_remaining_budget() returns None when no budget limit."""
        assert session_without_budget.get_remaining_budget() is None

    def test_get_budget_usage_percent_50(self, session_with_budget):
        """Test get_budget_usage_percent() at 50%."""
        session_with_budget.add_agent_execution(token_usage=500)
        assert session_with_budget.get_budget_usage_percent() == 50.0

    def test_get_budget_usage_percent_100(self, session_with_budget):
        """Test get_budget_usage_percent() at 100%."""
        session_with_budget.add_agent_execution(token_usage=1000)
        assert session_with_budget.get_budget_usage_percent() == 100.0

    def test_get_budget_usage_percent_over_100(self, session_with_budget):
        """Test get_budget_usage_percent() over 100%."""
        session_with_budget.add_agent_execution(token_usage=1500)
        assert session_with_budget.get_budget_usage_percent() == 150.0

    def test_get_budget_usage_percent_none_when_no_limit(self, session_without_budget):
        """Test get_budget_usage_percent() returns None when no budget limit."""
        assert session_without_budget.get_budget_usage_percent() is None

    def test_get_budget_usage_percent_none_when_zero_limit(self):
        """Test get_budget_usage_percent() returns None when budget is zero."""
        session = InvestigationSession(
            session_id="sess_zero_budget",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            token_budget_limit=0,
        )
        assert session.get_budget_usage_percent() is None


class TestInvestigationSessionDurationDisplay:
    """Tests for get_duration_display method."""

    def test_duration_display_seconds_only(self):
        """Test duration display with only seconds."""
        session = InvestigationSession(
            session_id="sess_dur",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            total_duration_ms=45000,  # 45 seconds
        )
        assert session.get_duration_display() == "45s"

    def test_duration_display_minutes_and_seconds(self):
        """Test duration display with minutes and seconds."""
        session = InvestigationSession(
            session_id="sess_dur",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            total_duration_ms=135000,  # 2 min 15 sec
        )
        assert session.get_duration_display() == "2m 15s"

    def test_duration_display_hours_minutes_seconds(self):
        """Test duration display with hours, minutes, and seconds."""
        session = InvestigationSession(
            session_id="sess_dur",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            total_duration_ms=8145000,  # 2h 15m 45s
        )
        assert session.get_duration_display() == "2h 15m 45s"

    def test_duration_display_exact_hour(self):
        """Test duration display with exact hour."""
        session = InvestigationSession(
            session_id="sess_dur",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            total_duration_ms=3600000,  # 1 hour
        )
        assert session.get_duration_display() == "1h"

    def test_duration_display_zero(self):
        """Test duration display with zero duration."""
        session = InvestigationSession(
            session_id="sess_dur",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            total_duration_ms=0,
        )
        assert session.get_duration_display() == "0s"

    def test_duration_display_active_session(self):
        """Test duration display calculates from started_at for active sessions."""
        started = datetime.now(timezone.utc) - timedelta(seconds=90)
        session = InvestigationSession(
            session_id="sess_dur",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            started_at=started,
            # total_duration_ms is None (active session)
        )
        # Should show approximately 1m 30s (allow for test execution time)
        display = session.get_duration_display()
        assert "1m" in display or "2m" in display


class TestInvestigationSessionTouch:
    """Tests for touch methods."""

    def test_touch_updates_updated_at(self):
        """Test that touch() updates updated_at timestamp."""
        session = InvestigationSession(
            session_id="sess_touch",
            case_id="case_123",
            user_id="user_456",
            enterprise_id="org_789",
        )

        original = session.updated_at
        time.sleep(0.001)

        session.touch()

        assert session.updated_at > original

    def test_update_last_activity_updates_both(self):
        """Test update_last_activity updates both timestamps."""
        session = InvestigationSession(
            session_id="sess_activity",
            case_id="case_123",
            user_id="user_456",
            enterprise_id="org_789",
        )

        original_updated = session.updated_at
        original_activity = session.last_activity_at
        time.sleep(0.001)

        session.update_last_activity()

        assert session.updated_at > original_updated
        assert session.last_activity_at > original_activity


class TestInvestigationSessionRepr:
    """Tests for string representation."""

    def test_repr(self):
        """Test string representation."""
        session = InvestigationSession(
            session_id="sess_repr123",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
            total_agent_executions=5,
        )

        repr_str = repr(session)
        assert "InvestigationSession" in repr_str
        assert "sess_repr123" in repr_str
        assert "case_456" in repr_str
        assert "active" in repr_str
        assert "5" in repr_str


class TestInvestigationSessionStateTransitions:
    """Tests for complete state transition flows."""

    def test_full_lifecycle_active_to_completed(self):
        """Test complete lifecycle: active → paused → active → completed."""
        session = InvestigationSession(
            session_id="sess_lifecycle",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
        )

        # Start active
        assert session.state == SessionState.ACTIVE
        session.add_agent_execution(token_usage=100)

        # Pause
        session.pause()
        assert session.state == SessionState.PAUSED

        # Resume
        session.resume()
        assert session.state == SessionState.ACTIVE
        session.add_agent_execution(token_usage=200)

        # Complete
        session.complete("Investigation complete")
        assert session.state == SessionState.COMPLETED
        assert session.total_token_usage == 300
        assert session.total_agent_executions == 2
        assert session.findings_summary == "Investigation complete"
        assert session.ended_at is not None

    def test_lifecycle_active_to_abandoned(self):
        """Test lifecycle: active → abandoned."""
        session = InvestigationSession(
            session_id="sess_abandoned",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
        )

        session.add_agent_execution(token_usage=100)
        session.abandon()

        assert session.state == SessionState.ABANDONED
        assert session.ended_at is not None
        assert session.findings_summary is None

    def test_lifecycle_pause_and_abandon(self):
        """Test lifecycle: active → paused → abandoned."""
        session = InvestigationSession(
            session_id="sess_pause_abandon",
            case_id="case_456",
            user_id="user_789",
            enterprise_id="org_012",
        )

        session.pause()
        session.abandon()

        assert session.state == SessionState.ABANDONED
