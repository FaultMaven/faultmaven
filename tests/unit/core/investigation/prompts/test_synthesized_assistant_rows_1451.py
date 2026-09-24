"""A placeholder the server wrote is never replayed as the assistant's words (#1451).

The assistant mirror of #1434. When the model gives no usable answer the engine
(#1442) or the service backstop writes a placeholder — "[Response withheld by
safety filter]", "(this turn produced no answer)" — and flags the row
``agent_response_synthesized``. Quoted back next turn as ``ASSISTANT: <text>``
it reads as something the model said.

Ruled 2026-09-24: such a turn renders as ONE neutral marker line, the way an
aside renders ``ASIDE_LINE`` — a bare line, never ``ASSISTANT: <marker>`` — and
is not skipped, because a turn the model failed to answer is information.

Four prompt surfaces, over two data sources (the fifth, the auto-titler, is
tested beside its own module):

- ``_build_verbatim_history`` and ``_build_graduated_history``'s RECENT window
  read ``case_messages`` rows;
- ``_build_turn_summary`` (EARLIER TURNS) and ``_build_compact_history``'s
  ``<previous_turn>`` read ``TurnProgress``, which carries its own flag because
  the row's never reaches it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    RESPONSE_EMPTY_TEXT,
    RESPONSE_NO_SIGNAL_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    RESPONSE_WITHHELD_TEXT,
)
from faultmaven.core.investigation.prompts import context_builder as cb
from faultmaven.core.investigation.prompts.fence import mint_token
from faultmaven.modules.agent.domain.services.orientation import (
    EMPTY_AGENT_RESPONSE_TEXT,
)
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MESSAGE_METADATA_USER_EMPTY,
    is_server_written_assistant_row,
)
from faultmaven.modules.case.domain.models import TurnOutcome, TurnProgress

#: Every text the server writes into an assistant row: the engine's four
#: stop-reason placeholders and the service backstop's.
PLACEHOLDERS = [
    RESPONSE_WITHHELD_TEXT,
    RESPONSE_TRUNCATED_TEXT,
    RESPONSE_EMPTY_TEXT,
    RESPONSE_NO_SIGNAL_TEXT,
    EMPTY_AGENT_RESPONSE_TEXT,
]


def _said(turn: int, role: str, content: str) -> dict:
    return {"turn_number": turn, "role": role, "content": content, "metadata": {}}


def _synthesized(turn: int, content: str) -> dict:
    return {
        "turn_number": turn,
        "role": "assistant",
        "content": content,
        "metadata": {MESSAGE_METADATA_AGENT_SYNTHESIZED: True},
    }


def _fence():
    return cb.PromptFence(mint_token())


def _record(turn: int, summary: str, *, synthesized: bool) -> TurnProgress:
    # A REAL record: every attribute of a Mock is truthy, which would make the
    # record read as an aside and never exercise the branch under test.
    return TurnProgress(
        turn_number=turn,
        progress_made=False,
        outcome=TurnOutcome.CONVERSATION,
        user_message_summary=f"user line for turn {turn}",
        agent_response_summary=summary,
        agent_response_synthesized=synthesized,
    )


@pytest.mark.unit
class TestThePredicate:
    def test_it_is_role_checked(self):
        """A stray key on a USER row must not turn what the user typed into a
        line saying the assistant did not answer."""
        user_row = {
            "role": "user",
            "content": "the pool is exhausted",
            "metadata": {MESSAGE_METADATA_AGENT_SYNTHESIZED: True},
        }
        assert is_server_written_assistant_row(user_row) is False
        out = cb._build_verbatim_history([user_row], _fence())
        assert "USER: the pool is exhausted" in out
        assert cb.NO_ANSWER_LINE not in out

    def test_it_reads_the_flag_not_the_text(self):
        """A model that genuinely wrote a bracketed sentence said it."""
        row = _said(1, "assistant", RESPONSE_EMPTY_TEXT)
        assert is_server_written_assistant_row(row) is False
        assert is_server_written_assistant_row(_synthesized(1, "x")) is True
        assert is_server_written_assistant_row({"role": "assistant"}) is False

    def test_the_user_rows_key_does_not_trip_it(self):
        """The two predicates read different keys; neither answers for the
        other's row."""
        row = {
            "role": "assistant",
            "content": "a real answer",
            "metadata": {MESSAGE_METADATA_USER_EMPTY: True},
        }
        assert is_server_written_assistant_row(row) is False


@pytest.mark.unit
class TestMessageRowSurfaces:
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_the_verbatim_history_renders_the_marker_not_the_placeholder(
        self, placeholder
    ):
        rows = [
            _said(1, "user", "disk is full on /var"),
            _synthesized(1, placeholder),
            _said(2, "user", "you didn't answer"),
            _said(2, "assistant", "sorry — which volume?"),
        ]
        out = cb._build_verbatim_history(rows, _fence())

        assert placeholder not in out
        # A bare line, the way ASIDE_LINE renders — never quoted as speech.
        assert f"\n{cb.NO_ANSWER_LINE}\n" in out
        assert f"ASSISTANT: {cb.NO_ANSWER_LINE}" not in out
        # Not a skip: the turn is still there, between the two user turns.
        assert out.index("disk is full on /var") < out.index(cb.NO_ANSWER_LINE)
        assert out.index(cb.NO_ANSWER_LINE) < out.index("you didn't answer")
        # Positive control: a real answer is still quoted.
        assert "ASSISTANT: sorry — which volume?" in out

    def _long_case(self, synthesized_turn: int, placeholder: str):
        rows = []
        for turn in range(1, 8):
            rows.append(_said(turn, "user", f"user line for turn {turn}"))
            if turn == synthesized_turn:
                rows.append(_synthesized(turn, placeholder))
            else:
                rows.append(_said(turn, "assistant", f"assistant line {turn}"))
        case = MagicMock()
        case.messages = rows
        case.turn_history = []
        return case

    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_the_graduated_recent_window_renders_the_marker(self, placeholder):
        case = self._long_case(synthesized_turn=7, placeholder=placeholder)
        out = cb._build_graduated_history(case, _fence())

        # Positive control: turn 7 really is in the verbatim window.
        assert "RECENT TURNS:" in out
        assert "USER: user line for turn 7" in out
        assert placeholder not in out
        assert f"\n{cb.NO_ANSWER_LINE}\n" in out
        assert f"ASSISTANT: {cb.NO_ANSWER_LINE}" not in out
        assert "ASSISTANT: assistant line 6" in out


@pytest.mark.unit
class TestTurnRecordSurfaces:
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_the_earlier_turns_summary_renders_the_marker(self, placeholder):
        line = cb._build_turn_summary(_record(3, placeholder, synthesized=True))

        assert placeholder not in line
        assert line == f"TURN 3: user line for turn 3 | {cb.NO_ANSWER_LINE}"
        assert "Agent:" not in line

    def test_the_summary_does_not_fall_back_to_the_outcome_name(self):
        """Why the flag is not "store None as the summary": with no summary
        and no structural parts, the renderer names the turn by its outcome —
        "conversation" — which says the turn went normally."""
        line = cb._build_turn_summary(_record(3, "", synthesized=True))
        assert "conversation" not in line
        assert cb.NO_ANSWER_LINE in line

    def test_an_answered_turn_is_still_quoted(self):
        line = cb._build_turn_summary(_record(3, "pool exhausted", synthesized=False))
        assert line.endswith("| Agent: pool exhausted")

    def _compact(self, record: TurnProgress) -> str:
        case = MagicMock()
        case.turn_history = [record]
        case.messages = []
        # The state summary is not under test and reads a great deal of case
        # state; stub it so the <previous_turn> block is what renders.
        from unittest.mock import patch

        with patch.object(cb, "_build_state_summary", return_value="STATE"):
            return cb._build_compact_history(case, "and now?", _fence())

    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_the_compact_previous_turn_renders_the_marker(self, placeholder):
        out = self._compact(_record(4, placeholder, synthesized=True))

        assert "<previous_turn>" in out
        assert placeholder not in out
        assert f"\n{cb.NO_ANSWER_LINE}\n" in out
        assert "Agent:" not in out

    def test_the_compact_previous_turn_still_quotes_an_answer(self):
        out = self._compact(_record(4, "pool exhausted", synthesized=False))
        assert "Agent: pool exhausted" in out
        assert cb.NO_ANSWER_LINE not in out
