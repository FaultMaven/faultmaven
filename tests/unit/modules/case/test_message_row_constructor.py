"""``append_message_row``: one blank-content answer per row kind (#1452).

The writers' own output is pinned end to end, through their real paths, in
``tests/integration/modules/case/test_message_row_writers_pinned.py``. This
module pins the constructor's contract directly — each kind's answer to blank
content, the fields it fills, and the invariant that makes it worth having: no
kind can hand the aggregate save a row the repository will refuse.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from faultmaven.modules.case.contracts import (
    EMPTY_AGENT_RESPONSE_TEXT,
    EMPTY_TURN_TEXT,
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MESSAGE_METADATA_USER_EMPTY,
    Case,
    MessageRowKind,
    append_message_row,
    is_server_written_assistant_row,
    is_server_written_user_row,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository

pytestmark = pytest.mark.unit

#: Every blank spelling. SQL ``TRIM`` strips only spaces, so the tab and the
#: newline would pass the CHECK constraint and persist blank-looking; the NBSP
#: is blank to ``str.strip()``, which is the rule the writers always used.
BLANKS = [None, "", "   ", "\t", "\n", "\xa0"]
BLANK_IDS = ["none", "empty", "spaces", "tab", "newline", "nbsp"]

#: The eight ``case_messages`` columns a repository hydrates — the shape of a
#: reloaded row, which an appended row must match.
HYDRATED_KEYS = {
    "message_id",
    "turn_number",
    "role",
    "content",
    "created_at",
    "author_id",
    "token_count",
    "metadata",
}


def _case() -> Case:
    return Case(title="t", user_id="u1", enterprise_id="e1")


class TestUserTurn:
    @pytest.mark.parametrize("blank", BLANKS, ids=BLANK_IDS)
    def test_blank_is_recorded_as_the_marker_and_flagged(self, blank):
        case = _case()
        row = append_message_row(
            case, MessageRowKind.USER_TURN, blank, turn_number=4, author_id="u1"
        )

        assert case.messages == [row]
        assert row["content"] == EMPTY_TURN_TEXT
        assert row["role"] == "user"
        assert row["metadata"][MESSAGE_METADATA_USER_EMPTY] is True
        assert is_server_written_user_row(row)

    def test_text_is_kept_verbatim_and_the_flag_is_present_and_false(self):
        case = _case()
        row = append_message_row(
            case, MessageRowKind.USER_TURN, "  why?  ", turn_number=4, author_id="u1"
        )

        # Not stripped: a turn row stores what was sent.
        assert row["content"] == "  why?  "
        assert row["metadata"] == {MESSAGE_METADATA_USER_EMPTY: False}
        assert not is_server_written_user_row(row)

    def test_the_flag_is_written_into_the_callers_dict(self):
        metadata = {"intent_type": "conversation"}
        row = append_message_row(
            _case(), MessageRowKind.USER_TURN, "", turn_number=1, metadata=metadata
        )

        assert row["metadata"] is metadata
        assert metadata == {
            "intent_type": "conversation",
            MESSAGE_METADATA_USER_EMPTY: True,
        }


class TestAgentAnswer:
    @pytest.mark.parametrize("blank", BLANKS, ids=BLANK_IDS)
    def test_blank_is_recorded_as_the_backstop_marker_and_flagged(self, blank, caplog):
        case = _case()
        turn_meta = {"progress_made": False}
        with caplog.at_level(logging.WARNING):
            row = append_message_row(
                case,
                MessageRowKind.AGENT_ANSWER,
                blank,
                turn_number=4,
                metadata=turn_meta,
            )

        assert case.messages == [row]
        assert row["content"] == EMPTY_AGENT_RESPONSE_TEXT
        assert row["role"] == "assistant"
        assert row["author_id"] is None
        # In place: ``turn_meta`` is shared with the turn's other readers.
        assert row["metadata"] is turn_meta
        assert turn_meta[MESSAGE_METADATA_AGENT_SYNTHESIZED] is True
        assert is_server_written_assistant_row(row)
        assert "Empty agent_response" in caplog.text

    def test_an_answer_is_verbatim_and_the_flag_stays_absent(self):
        turn_meta: dict = {}
        row = append_message_row(
            _case(),
            MessageRowKind.AGENT_ANSWER,
            "  the pool was exhausted  ",
            turn_number=4,
            metadata=turn_meta,
        )

        assert row["content"] == "  the pool was exhausted  "
        assert MESSAGE_METADATA_AGENT_SYNTHESIZED not in turn_meta

    def test_an_engine_flag_on_real_text_is_never_cleared(self):
        """The engine flags its own placeholders; the backstop only ever SETS."""
        turn_meta = {MESSAGE_METADATA_AGENT_SYNTHESIZED: True}
        row = append_message_row(
            _case(),
            MessageRowKind.AGENT_ANSWER,
            "[Response withheld by safety filter]",
            turn_number=4,
            metadata=turn_meta,
        )

        assert row["content"] == "[Response withheld by safety filter]"
        assert turn_meta[MESSAGE_METADATA_AGENT_SYNTHESIZED] is True


class TestInitialMessage:
    @pytest.mark.parametrize("blank", BLANKS, ids=BLANK_IDS)
    def test_blank_writes_no_row(self, blank):
        case = _case()

        assert (
            append_message_row(
                case, MessageRowKind.INITIAL_MESSAGE, blank, turn_number=1
            )
            is None
        )
        assert case.messages == []

    def test_text_is_stripped(self):
        row = append_message_row(
            _case(),
            MessageRowKind.INITIAL_MESSAGE,
            "  /var is at 100%  ",
            turn_number=1,
            author_id="u1",
        )

        assert row["content"] == "/var is at 100%"
        assert row["role"] == "user"
        # Content the user wrote, so it is not a server-written row.
        assert not is_server_written_user_row(row)
        assert row["metadata"] == {}


class TestSystemNotice:
    @pytest.mark.parametrize("blank", BLANKS, ids=BLANK_IDS)
    def test_blank_writes_no_row_and_says_so(self, blank, caplog):
        case = _case()
        with caplog.at_level(logging.WARNING):
            row = append_message_row(
                case,
                MessageRowKind.SYSTEM_NOTICE,
                blank,
                turn_number=2,
                metadata={"source": "runbook_conversion_complete"},
            )

        assert row is None
        assert case.messages == []
        assert "Blank system notice" in caplog.text

    def test_text_is_a_system_row_with_no_author(self):
        row = append_message_row(
            _case(),
            MessageRowKind.SYSTEM_NOTICE,
            "Your runbook draft is ready.",
            turn_number=2,
            metadata={"source": "runbook_conversion_complete"},
        )

        assert row["role"] == "system"
        assert row["author_id"] is None
        assert row["content"] == "Your runbook draft is ready."


class TestTheRowItself:
    @pytest.mark.parametrize("kind", list(MessageRowKind), ids=lambda k: k.value)
    def test_the_row_is_the_shape_a_reload_returns(self, kind):
        row = append_message_row(_case(), kind, "text", turn_number=3)

        assert set(row) == HYDRATED_KEYS
        assert row["turn_number"] == 3
        assert row["token_count"] is None
        assert row["message_id"].startswith("msg_")
        assert len(row["message_id"]) == len("msg_") + CaseRepository._MESSAGE_ID_HEX
        assert (
            datetime.fromisoformat(row["created_at"]).utcoffset().total_seconds() == 0
        )

    @pytest.mark.parametrize("kind", list(MessageRowKind), ids=lambda k: k.value)
    def test_the_save_has_nothing_to_complete(self, kind):
        """The constructor fills what the repository would otherwise have to
        mint or refuse, in the spelling it canonicalises to — so the aggregate
        save's normalisation is a no-op on it."""
        row = append_message_row(_case(), kind, "text", turn_number=3)

        assert CaseRepository.normalise_message_row(copy.deepcopy(row)) == row

    @pytest.mark.parametrize("kind", list(MessageRowKind), ids=lambda k: k.value)
    @pytest.mark.parametrize("blank", BLANKS, ids=BLANK_IDS)
    def test_no_kind_hands_the_save_a_row_it_refuses(self, kind, blank):
        """The invariant #1452 exists for: whatever the kind and whatever the
        blank spelling, the result is either no row or a row the repository
        accepts. A blank row aborts the WHOLE aggregate save."""
        case = _case()
        row = append_message_row(case, kind, blank, turn_number=1)

        if row is None:
            assert case.messages == []
        else:
            CaseRepository.normalise_message_row(copy.deepcopy(row))

    def test_the_same_dict_is_appended_and_returned(self):
        """Callers tag the row after appending it (orientation, out-of-band)."""
        case = _case()
        row = append_message_row(case, MessageRowKind.USER_TURN, "", turn_number=1)
        row["metadata"]["orientation"] = "empty"

        assert case.messages[-1]["metadata"]["orientation"] == "empty"

    @pytest.mark.parametrize("kind", list(MessageRowKind), ids=lambda k: k.value)
    def test_the_row_goes_LAST_behind_every_existing_row(self, kind):
        """In memory, before any save, order is what the turn reads: the
        greeting walks ``reversed(case.messages)`` for the last question asked,
        and the prompt's RECENT window is ``messages[-20:]``. A reload re-sorts
        by ``created_at``, so nothing downstream of a save can see a row that
        went in at the wrong end — only this can. Several rows, because with
        one ``[-1]`` cannot tell an append from an insert at the front."""
        case = _case()
        earlier = [
            append_message_row(case, MessageRowKind.USER_TURN, f"q{i}", turn_number=i)
            for i in range(1, 4)
        ]
        before = list(case.messages)

        row = append_message_row(case, kind, "the newest", turn_number=4)

        assert case.messages[-1] is row
        assert case.messages[:-1] == before
        assert [m["content"] for m in case.messages] == [
            *(m["content"] for m in earlier),
            "the newest",
        ]

    def test_no_turn_number_is_refused_before_anything_is_appended(self):
        case = _case()

        with pytest.raises(ValueError, match="turn_number"):
            append_message_row(case, MessageRowKind.USER_TURN, "why?", turn_number=None)
        assert case.messages == []

    def test_every_kind_has_a_role(self):
        """A kind added to the enum without a role would KeyError at the first
        append in production rather than here."""
        roles = {
            kind: append_message_row(_case(), kind, "x", turn_number=1)["role"]
            for kind in MessageRowKind
        }
        assert set(roles.values()) <= {"user", "assistant", "system"}

    def test_the_kind_may_be_passed_by_value(self):
        row = append_message_row(
            SimpleNamespace(case_id="c", messages=[]), "agent_answer", "", turn_number=1
        )
        assert row["content"] == EMPTY_AGENT_RESPONSE_TEXT
