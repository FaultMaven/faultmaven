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

from faultmaven.modules.agent.domain.services.orientation import EMPTY_TURN_TEXT
from faultmaven.modules.case.contracts import MESSAGE_METADATA_USER_EMPTY


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
