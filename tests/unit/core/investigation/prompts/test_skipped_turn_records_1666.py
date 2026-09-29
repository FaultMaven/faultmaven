"""A turn record the server backfilled is never rendered as what a party said (#1666).

``Case.reconcile_turn_sequence`` repairs a gap in ``turn_history`` by inserting
a ``SKIPPED`` placeholder (``TurnProgress.is_skipped``) whose two summaries are
server text: ``"(turn not recorded)"`` and ``"(turn not recorded — recovered
after an interrupted turn)"``. The EARLIER TURNS summary used to render it as
``TURN 2: (turn not recorded) → … | Agent: (turn not recorded — …)`` — the
server's words presented as the user's (#1434's rule) and as the agent's
(#1451's rule), for a turn whose real message rows may still exist.

Three readers of a turn record's summaries, and each is pinned here:

- ``_build_graduated_history``'s EARLIER TURNS treats a placeholder as no
  record and names the turn from its real user row, the path it already took
  when the record was absent;
- ``_build_turn_summary`` renders one as the bare ``NOT_RECORDED_LINE``;
- ``_build_compact_history``'s ``<previous_turn>`` does the same.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.context_builder import history as cb
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress

pytestmark = pytest.mark.unit

#: What the writer puts in a placeholder's summaries. Asserted against the
#: writer's own record below, so a change to the writer's text shows up here
#: rather than leaving these checks looking for a string nobody writes.
WRITER_USER_TEXT = "(turn not recorded)"
WRITER_AGENT_TEXT = "(turn not recorded — recovered after an interrupted turn)"

#: The real question the user asked on the turn the record was lost for.
TURN_2_QUESTION = "why is /var full on db-2"


def _record(n: int) -> TurnProgress:
    return TurnProgress(
        turn_number=n,
        outcome=TurnOutcome.CONVERSATION,
        progress_made=False,
        user_message_summary=f"record summary for turn {n}",
        agent_response_summary=f"record reply for turn {n}",
    )


def _row(turn: int, role: str, content: str) -> dict:
    return {"turn_number": turn, "role": role, "content": content, "metadata": {}}


def _reconciled_case(*, turn_2_user_row: bool = True) -> Case:
    """A real case whose turn 2 record was lost, repaired by the real writer.

    Records for turns 1 and 3-7 and message rows for all seven: seven turns
    take the GRADUATED path, and turn 2 falls in EARLIER TURNS, outside the
    verbatim window, so its rows are not replayed anywhere else.
    """
    case = Case(enterprise_id="org1", title="t")
    case.turn_history = [_record(n) for n in (1, 3, 4, 5, 6, 7)]
    case.current_turn = 7
    rows = []
    for turn in range(1, 8):
        if turn == 2:
            if turn_2_user_row:
                rows.append(_row(2, "user", TURN_2_QUESTION))
        else:
            rows.append(_row(turn, "user", f"user line for turn {turn}"))
        rows.append(_row(turn, "assistant", f"assistant line for turn {turn}"))
    case.messages = rows

    case.reconcile_turn_sequence()

    # Positive control: the writer made the placeholder this is about, with
    # the texts the assertions below look for.
    placeholder = {t.turn_number: t for t in case.turn_history}[2]
    assert placeholder.is_skipped
    assert placeholder.user_message_summary == WRITER_USER_TEXT
    assert placeholder.agent_response_summary == WRITER_AGENT_TEXT
    return case


def _line(out: str, turn: int) -> str:
    prefix = f"TURN {turn}:"
    return next(ln for ln in out.splitlines() if ln.startswith(prefix))


class TestEarlierTurns:
    def test_a_placeholder_is_named_by_the_turns_real_user_row(self):
        out = cb._build_graduated_history(_reconciled_case(), PromptFence(mint_token()))

        assert "EARLIER TURNS:" in out
        assert _line(out, 2) == f"TURN 2: {TURN_2_QUESTION}"
        assert WRITER_USER_TEXT not in out
        assert "Agent: (turn not recorded" not in out
        # A real record beside it still renders from the record: this is a
        # screen on the placeholder, not a switch away from turn records.
        assert _line(out, 1).startswith("TURN 1: record summary for turn 1")

    def test_a_placeholder_with_no_user_row_takes_the_existing_fallback(self):
        out = cb._build_graduated_history(
            _reconciled_case(turn_2_user_row=False), PromptFence(mint_token())
        )

        assert "EARLIER TURNS:" in out
        assert _line(out, 2) == "TURN 2: ..."
        assert WRITER_USER_TEXT not in out
        assert WRITER_AGENT_TEXT not in out


class TestTurnSummary:
    def test_a_placeholder_renders_as_the_marker_line(self):
        """The function's own guard, so no caller can render one: called
        directly on the record the writer made."""
        case = _reconciled_case()
        placeholder = {t.turn_number: t for t in case.turn_history}[2]

        assert cb._build_turn_summary(placeholder) == f"TURN 2: {cb.NOT_RECORDED_LINE}"


class TestCompactHistory:
    def test_a_placeholder_as_the_previous_turn_renders_the_marker_line(self):
        """Hand-built: the writer inserts a placeholder only BETWEEN two
        records, so it never produces one as the last record. The rule is per
        record, not per position, and this pins it where the writer cannot."""
        case = Case(enterprise_id="org1", title="t")
        case.turn_history = [
            _record(1),
            TurnProgress(
                turn_number=2,
                outcome=TurnOutcome.SKIPPED,
                progress_made=False,
                user_message_summary=WRITER_USER_TEXT,
                agent_response_summary=WRITER_AGENT_TEXT,
            ),
        ]
        case.current_turn = 2

        out = cb._build_compact_history(case, "and now?", PromptFence(mint_token()))

        body = out.split("<previous_turn>\n", 1)[1].split("</previous_turn>", 1)[0]
        assert body == f"{cb.NOT_RECORDED_LINE}\n"
        assert "Agent:" not in out
        assert WRITER_USER_TEXT not in out
