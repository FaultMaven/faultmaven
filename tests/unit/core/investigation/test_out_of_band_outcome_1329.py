"""#1329 — OUT_OF_BAND is a recorded turn that is not investigation work."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.context_builder import (
    ASIDE_LINE,
    _build_compact_history,
    _build_graduated_history,
    _build_turn_summary,
    _build_verbatim_history,
)
from faultmaven.core.investigation.prompts.fence import PromptFence
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
        enterprise_id="o",
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

    def test_investigation_turn_count_is_the_clock_minus_asides(self):
        case = _case(
            _turn(1, TurnOutcome.DATA_PROVIDED),
            _turn(2, TurnOutcome.OUT_OF_BAND),
            _turn(3, TurnOutcome.CONVERSATION),
            _turn(
                4, TurnOutcome.SKIPPED
            ),  # a lost-but-consumed engine turn still counts
            _turn(5, TurnOutcome.OUT_OF_BAND),
        )
        assert case.current_turn == 5
        assert case.investigation_turn_count == 3

    def test_unrecorded_turns_are_not_undercounted(self):
        """PR #1337 review: 8 of 283 dev cases carry consumed turns with no
        turn_history record; counting only recorded entries would drop them."""
        case = _case()
        case.current_turn = 20
        assert case.investigation_turn_count == 20

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
        assert line == f"TURN 7: {ASIDE_LINE}"
        assert "a7" not in line

    def _messages(self, n_turns, aside_turn):
        msgs = []
        for t in range(1, n_turns + 1):
            meta = {"out_of_band": "off_topic"} if t == aside_turn else {}
            msgs.append(
                {
                    "turn_number": t,
                    "role": "user",
                    "content": f"user text {t}",
                    "metadata": meta,
                }
            )
            msgs.append(
                {
                    "turn_number": t,
                    "role": "assistant",
                    "content": (
                        "Soft paws, warm sunbeam... Canberra."
                        if t == aside_turn
                        else f"agent text {t}"
                    ),
                    "metadata": meta,
                }
            )
        return msgs

    @pytest.mark.parametrize("n_turns", [2, 5, 16])
    def test_every_history_fidelity_hides_the_aside_text(self, n_turns):
        """PR #1337 review: the EARLIER TURNS summary was the only path taught
        about the aside; the verbatim window and the compact <previous_turn>
        rendered the haiku on the very next turn."""
        aside = n_turns  # the most recent turn is the aside
        case = _case(
            *[
                _turn(
                    t,
                    TurnOutcome.OUT_OF_BAND if t == aside else TurnOutcome.CONVERSATION,
                )
                for t in range(1, n_turns + 1)
            ]
        )
        case.messages = self._messages(n_turns, aside)
        fence = PromptFence(token="f1329test")
        outputs = [
            _build_graduated_history(case, fence),
            _build_verbatim_history(case.messages, fence),
            _build_compact_history(case, "next message", fence),
        ]
        for out in outputs:
            assert "Soft paws" not in out
            assert ASIDE_LINE in out
        # And a genuine turn is still rendered.
        assert (
            f"agent text {n_turns - 1}" in outputs[0]
            or f"TURN {n_turns - 1}" in outputs[0]
        )

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
