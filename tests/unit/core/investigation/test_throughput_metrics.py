"""Investigation throughput instrumentation — funnel transition metrics.

Verifies the terminal-transition executors record the case-funnel metrics
(faultmaven_case_transitions_total / faultmaven_case_resolution_turns) and
that the module helpers behave (clamp negative turn counts).

Resolution is measured in TURNS, not wall-clock: wall-clock is dominated by
human idle time and measures user availability, not the copilot's effort.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from faultmaven.core.investigation import terminal_transitions
from faultmaven.core.investigation.terminal_transitions import (
    _execute_closed_transition,
    _execute_resolved_transition,
)
from faultmaven.infrastructure.observability import investigation_metrics
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
)


def _investigating_case(current_turn: int = 7) -> Case:
    created = datetime.now(UTC) - timedelta(minutes=30)
    case = Case(
        case_id="case_1234567890ab",
        title="Test Case",
        state=CaseState.INVESTIGATING,
        user_id="user_123",
        organization_id="org_123",
        description="Test description",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        progress=InvestigationProgress(),
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            thread_id="thread_123",
            proposed_problem_statement="Test symptom",
        ),
    )
    # created_at is validated/managed by the model; set directly for the test.
    object.__setattr__(case, "created_at", created)
    object.__setattr__(case, "current_turn", current_turn)
    return case


@pytest.mark.unit
class TestTerminalTransitionInstrumentation:
    def test_resolved_records_transition_and_turns(self):
        case = _investigating_case(current_turn=7)
        with (
            patch.object(terminal_transitions, "record_transition") as rt,
            patch.object(terminal_transitions, "record_resolution_turns") as rd,
        ):
            _execute_resolved_transition(case, "user_123")

        rt.assert_called_once_with("investigating", "resolved")
        rd.assert_called_once_with("resolved", 7)

    def test_closed_records_transition_and_turns(self):
        case = _investigating_case(current_turn=3)
        with (
            patch.object(terminal_transitions, "record_transition") as rt,
            patch.object(terminal_transitions, "record_resolution_turns") as rd,
        ):
            _execute_closed_transition(case, "user_123", "closed_after_investigation")

        rt.assert_called_once_with("investigating", "closed")
        rd.assert_called_once_with("closed", 3)


@pytest.mark.unit
class TestMetricHelpers:
    def test_resolution_turns_clamps_negative(self):
        # A defensive guard: a negative turn count must never be observed.
        with patch.object(
            investigation_metrics.case_resolution_turns, "labels"
        ) as labels:
            investigation_metrics.record_resolution_turns("resolved", -5)
        labels.assert_called_once_with(to_state="resolved")
        observed = labels.return_value.observe.call_args[0][0]
        assert observed == 0

    def test_record_transition_labels(self):
        with patch.object(
            investigation_metrics.case_transitions_total, "labels"
        ) as labels:
            investigation_metrics.record_transition("inquiry", "investigating")
        labels.assert_called_once_with(from_state="inquiry", to_state="investigating")
        labels.return_value.inc.assert_called_once()
