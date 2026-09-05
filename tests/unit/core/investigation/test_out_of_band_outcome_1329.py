"""#1329 — OUT_OF_BAND is a recorded turn that is not investigation work."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.context_builder import _build_turn_summary
from faultmaven.models.api_models import TurnResponse
from faultmaven.modules.case.domain.models import (
    NON_INVESTIGATIVE_OUTCOMES,
    Case,
    CaseState,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.modules.case.domain.services import case_ui_adapter

pytestmark = pytest.mark.unit


def _turn(n, outcome, evidence=(), milestones=()):
    return TurnProgress(
        turn_number=n,
        timestamp=datetime.now(timezone.utc),
        milestones_completed=list(milestones),
        evidence_added=list(evidence),
        progress_made=False,
        outcome=outcome,
        user_message_summary=f"u{n}",
        agent_response_summary=f"a{n}",
    )


def _case(*turns):
    case = Case(
        case_id="case_aabb11223344",
        title="OOM",
        description="d",
        user_id="u",
        organization_id="o",
        # INQUIRY: neither property under test reads the state, and INVESTIGATING
        # would need a confirmed problem statement the test does not care about.
        state=CaseState.INQUIRY,
        current_turn=len(turns),
    )
    case.turn_history = list(turns)
    return case


class TestOutcome:
    def test_member_and_shared_exclusion_set(self):
        assert TurnOutcome.OUT_OF_BAND.value == "out_of_band"
        assert "out_of_band" in NON_INVESTIGATIVE_OUTCOMES
        assert {"conversation", "other"} <= NON_INVESTIGATIVE_OUTCOMES

    def test_investigation_turn_count_excludes_asides_and_skips(self):
        case = _case(
            _turn(1, TurnOutcome.DATA_PROVIDED),
            _turn(2, TurnOutcome.OUT_OF_BAND),
            _turn(3, TurnOutcome.CONVERSATION),
            _turn(4, TurnOutcome.SKIPPED),
            _turn(5, TurnOutcome.OUT_OF_BAND),
        )
        assert case.current_turn == 5
        assert case.investigation_turn_count == 2

    def test_progress_monitor_does_not_count_an_aside_as_investigative(self):
        case = _case(
            _turn(1, TurnOutcome.MILESTONE_COMPLETED, milestones=["symptom_verified"]),
            _turn(2, TurnOutcome.HYPOTHESIS_TESTED),
            _turn(3, TurnOutcome.OUT_OF_BAND),
            _turn(4, TurnOutcome.OUT_OF_BAND),
        )
        assert ProgressMonitor()._count_investigative_turns_since_milestone(case) == 1

    def test_ui_adapter_uses_the_same_rule(self):
        src = open(case_ui_adapter.__file__).read()
        assert "NON_INVESTIGATIVE_OUTCOMES" in src
        assert '("conversation", "other")' not in src

    def test_history_summary_renders_the_aside_as_an_aside(self):
        line = _build_turn_summary(_turn(7, TurnOutcome.OUT_OF_BAND))
        assert line == "TURN 7: (off-topic exchange — not part of the investigation)"
        assert "a7" not in line

    def test_turn_response_reports_the_investigation_turn(self):
        resp = TurnResponse(
            agent_response="x",
            turn_number=8,
            investigation_turn=7,
            milestones_completed=[],
            case_state=CaseState.INVESTIGATING,
            progress_made=False,
        )
        assert resp.investigation_turn == 7
        assert TurnResponse.model_fields["investigation_turn"].default is None
