"""A marker the server wrote is never presented as the user's words (#1434).

#1420 made a turn carrying no user message persist by storing
``EMPTY_TURN_TEXT`` instead of a blank string — ``case_messages`` requires
non-blank content, and the turn is real (charged, and it advances the message
clock). That fixed the save and created this: the row now has content, so
every renderer that used to drop it on ``if not content`` started quoting it.

Two consumers, and the prompt has THREE channels:

- ``_build_graduated_history`` — verbatim RECENT TURNS on long conversations
- ``_build_verbatim_history`` — the whole history for short ones
- the EARLIER TURNS summary, which names a turn by its first user message

The aside elision cannot cover this: it keys on ``out_of_band``, and the
pending-transition path routes a blank reply to the ENGINE rather than to
orientation, so that row is never tagged.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts import context_builder as cb
from faultmaven.core.investigation.prompts.fence import mint_token
from faultmaven.modules.agent.domain.services.orientation import EMPTY_TURN_TEXT
from faultmaven.modules.case.contracts import MESSAGE_METADATA_USER_EMPTY


def _marker_row(turn: int) -> dict:
    """A row exactly as the pending-transition path writes it: the marker,
    flagged, and WITHOUT ``out_of_band`` — which is what makes it invisible to
    the aside elision."""
    return {
        "turn_number": turn,
        "role": "user",
        "content": EMPTY_TURN_TEXT,
        "metadata": {MESSAGE_METADATA_USER_EMPTY: True},
    }


def _said(turn: int, role: str, content: str) -> dict:
    return {"turn_number": turn, "role": role, "content": content, "metadata": {}}


@pytest.mark.unit
class TestServerWrittenRowsAreNotQuoted:
    def test_the_aside_elision_does_not_cover_it(self):
        """Why this needed its own predicate rather than reusing the tag.

        If this ever starts returning the marker's turn, the elision has grown
        to cover it and the skip may be redundant — but until then, asserting
        it keeps the reason for a second mechanism visible.
        """
        rows = [_marker_row(2), _said(2, "assistant", "still waiting")]
        assert cb._aside_turns(rows) == set()

    def test_an_assistant_row_carrying_the_key_is_not_dropped(self):
        """The role check, which the two former copies of this rule disagreed on.

        An assistant row's ``metadata`` IS the engine's own per-turn metadata
        dict, which many handlers write into. A predicate that ignored ``role``
        would silently delete the ASSISTANT's answer from the prompt the day
        anything stamped this key there — with no log and no other test
        failing.
        """
        poisoned = {
            "turn_number": 1,
            "role": "assistant",
            "content": "the pool was exhausted",
            "metadata": {MESSAGE_METADATA_USER_EMPTY: True},
        }
        assert cb.is_server_written_user_row(poisoned) is False

        out = cb._build_verbatim_history(
            [_said(1, "user", "why?"), poisoned], cb.PromptFence(mint_token())
        )
        assert "the pool was exhausted" in out

    def test_the_predicate_reads_the_flag_not_the_text(self):
        """Keyed on the metadata, not on matching the marker string.

        A user who literally types "(no message)" said it, and must be quoted.
        """
        assert cb.is_server_written_user_row(_marker_row(1)) is True
        assert cb.is_server_written_user_row(_said(1, "user", EMPTY_TURN_TEXT)) is False
        assert cb.is_server_written_user_row({"role": "user"}) is False

    def test_the_verbatim_history_does_not_quote_it(self):
        """Short-conversation fidelity."""
        rows = [
            _said(1, "user", "disk is full on /var"),
            _said(1, "assistant", "which volume?"),
            _marker_row(2),
            _said(2, "assistant", "still waiting on the volume name"),
        ]
        out = cb._build_verbatim_history(rows, cb.PromptFence(mint_token()))

        assert EMPTY_TURN_TEXT not in out
        # The real content is still there — this is a skip, not a blanket drop.
        assert "disk is full on /var" in out
        assert "still waiting on the volume name" in out

    def test_a_user_who_actually_typed_the_marker_text_is_quoted(self):
        """Positive control for the whole class.

        Without it, a renderer that dropped every row containing the marker
        string — or every user row — would pass every test above.
        """
        rows = [_said(1, "user", EMPTY_TURN_TEXT), _said(1, "assistant", "ok")]
        out = cb._build_verbatim_history(rows, cb.PromptFence(mint_token()))
        assert EMPTY_TURN_TEXT in out

    def _long_case(self, marker_turn: int):
        """A case with enough turns to take the GRADUATED path.

        ``_build_graduated_history`` splits at ``HISTORY_VERBATIM_TURNS``: the
        last three turns render verbatim, everything earlier is summarised by
        its first user message. The marker has to be pinned on BOTH sides —
        they are different code, and a fix applied to one leaves the other
        quoting it.
        """
        from unittest.mock import MagicMock

        rows = []
        for turn in range(1, 8):
            if turn == marker_turn:
                rows.append(_marker_row(turn))
            else:
                rows.append(_said(turn, "user", f"user line for turn {turn}"))
            rows.append(_said(turn, "assistant", f"assistant line for turn {turn}"))

        case = MagicMock()
        case.messages = rows
        case.turn_history = []
        return case

    def test_the_graduated_recent_window_does_not_quote_it(self):
        """The marker in one of the last three turns, rendered verbatim there."""
        case = self._long_case(marker_turn=7)
        out = cb._build_graduated_history(case, cb.PromptFence(mint_token()))

        assert EMPTY_TURN_TEXT not in out
        assert "assistant line for turn 7" in out

    def test_the_graduated_earlier_summary_does_not_name_a_turn_by_it(self):
        """The marker in an EARLIER turn, which is summarised by its first
        user message — a different code path from the verbatim window."""
        case = self._long_case(marker_turn=1)
        out = cb._build_graduated_history(case, cb.PromptFence(mint_token()))

        assert EMPTY_TURN_TEXT not in out
        # The later turns still render, so this is a skip and not a collapse.
        assert "user line for turn 7" in out

    def test_the_earlier_summary_with_turn_records_does_not_quote_it(self):
        """The SECOND earlier-turns fallback, which a populated
        ``turn_history`` selects.

        There are two copies of "name the turn by its first user message": one
        reached when ``turn_history`` is absent, and one reached when it is
        present but has no record for that particular turn — the #1264 routes
        that consume a turn without recording one. Guarding only the first left
        the marker rendering here, and the sibling test could not see it
        because it sets ``turn_history = []``, which forces the other branch.
        """
        from unittest.mock import MagicMock

        rows = []
        for turn in range(1, 8):
            if turn == 1:
                rows.append(_marker_row(turn))
            else:
                rows.append(_said(turn, "user", f"user line for turn {turn}"))
            rows.append(_said(turn, "assistant", f"assistant line for turn {turn}"))

        # Populated, but with NO record for turn 1 — so the summary falls back
        # to the messages for exactly the turn carrying the marker.
        record = MagicMock()
        record.turn_number = 4
        case = MagicMock()
        case.messages = rows
        case.turn_history = [record]

        out = cb._build_graduated_history(case, cb.PromptFence(mint_token()))
        assert (
            EMPTY_TURN_TEXT not in out
        ), "the turn_records fallback still names a turn by the server's marker"
