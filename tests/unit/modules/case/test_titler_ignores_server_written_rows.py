"""The auto-titler must not name a case after a marker the server wrote (#1434).

``get_case_conversation_context`` formats every user row as
``"N. [ts] User: <content>"``, and ``_extract_user_signals_from_context`` keeps
every ``"] User:"`` line as title signal. After #1420 a turn carrying no user
message stores ``EMPTY_TURN_TEXT`` rather than a blank string, so the marker
became eligible signal — and on a case still holding its ``Case-YYMMDD-N``
placeholder, a couple of bare mentions would contribute it to the name the user
ends up seeing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.case.contracts import (
    EMPTY_TURN_TEXT,
    MESSAGE_METADATA_USER_EMPTY,
)


def _row(role: str, content: str, *, server_written: bool = False) -> dict:
    return {
        "role": role,
        "content": content,
        "created_at": "2026-06-13T10:15:30+00:00",
        "metadata": {MESSAGE_METADATA_USER_EMPTY: True} if server_written else {},
    }


async def _context(rows):
    from faultmaven.modules.case.domain.services.case_service import CaseService

    service = CaseService.__new__(CaseService)
    repo = MagicMock()
    # The reader takes its rows from ``get_messages``, not from ``case.messages``.
    repo.get_messages = AsyncMock(return_value=rows)
    service.repository = repo
    return await service.get_case_conversation_context("case_abc123abc123")


@pytest.mark.unit
@pytest.mark.asyncio
class TestTitlerIgnoresServerWrittenRows:
    async def test_the_marker_is_not_offered_as_user_signal(self):
        rows = [
            _row("user", "postgres OOMs every night at 02:00"),
            _row("assistant", "since when?"),
            _row("user", EMPTY_TURN_TEXT, server_written=True),
            # Excluded by the reader as "the current query", so the context
            # needs a row after the marker for the marker to be considered.
            _row("assistant", "still waiting"),
        ]
        context = await _context(rows)

        assert EMPTY_TURN_TEXT not in context
        assert "postgres OOMs every night at 02:00" in context

    async def test_a_user_who_typed_that_text_still_counts(self):
        """Positive control: keyed on the flag, not on the string.

        Without it, a reader that dropped any line containing the marker text
        — or every user line — would pass the test above.
        """
        rows = [
            _row("user", EMPTY_TURN_TEXT),
            _row("assistant", "ok"),
            _row("assistant", "trailing"),
        ]
        context = await _context(rows)

        assert EMPTY_TURN_TEXT in context


@pytest.mark.unit
@pytest.mark.asyncio
class TestTitlerIgnoresSynthesizedAssistantRows:
    """The assistant mirror (#1451). A placeholder the engine or the service
    backstop wrote in place of an answer — "[Response withheld by safety
    filter]" — is not what the case is about, and must not become title
    signal. Skipped rather than marked: this context is title input, not a
    transcript the model continues from."""

    WITHHELD = "[Response withheld by safety filter]"

    def _assistant(self, content: str, *, synthesized: bool) -> dict:
        from faultmaven.modules.case.contracts import (
            MESSAGE_METADATA_AGENT_SYNTHESIZED,
        )

        return {
            "role": "assistant",
            "content": content,
            "created_at": "2026-06-13T10:15:30+00:00",
            "metadata": (
                {MESSAGE_METADATA_AGENT_SYNTHESIZED: True} if synthesized else {}
            ),
        }

    async def test_the_placeholder_is_not_offered_as_signal(self):
        rows = [
            _row("user", "postgres OOMs every night at 02:00"),
            self._assistant(self.WITHHELD, synthesized=True),
            _row("user", "hello?"),
            _row("assistant", "trailing"),  # excluded as the current query
        ]
        context = await _context(rows)

        assert self.WITHHELD not in context
        assert "postgres OOMs every night at 02:00" in context

    async def test_an_assistant_that_wrote_that_text_still_counts(self):
        """Positive control: keyed on the flag, not on the string."""
        rows = [
            _row("user", "why?"),
            self._assistant(self.WITHHELD, synthesized=False),
            _row("assistant", "trailing"),
        ]
        context = await _context(rows)

        assert self.WITHHELD in context

    async def test_a_user_row_carrying_the_key_is_not_dropped(self):
        """Role-checked: the assistant rule never removes a user's words."""
        from faultmaven.modules.case.contracts import (
            MESSAGE_METADATA_AGENT_SYNTHESIZED,
        )

        user = _row("user", "the disk is full")
        user["metadata"] = {MESSAGE_METADATA_AGENT_SYNTHESIZED: True}
        context = await _context([user, _row("assistant", "trailing")])

        assert "the disk is full" in context
