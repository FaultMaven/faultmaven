"""The grammar of a Claude model id, shared by the Anthropic adapter and the price table.

``claude-<family>-<major>[<sep><minor>][<date>]``: the minor is 1-2 digits
after ``-`` (Anthropic's spelling) or ``.`` (OpenRouter's), and the date is
8 digits after ``-`` or ``@`` (Vertex). So ``claude-opus-4-20250514`` is
(opus, 4, None), ``claude-haiku-4-5-20251001`` is (haiku, 4, 5) and
``claude-opus-5.5`` is (opus, 5, 5).
"""

from __future__ import annotations

import re

_CLAUDE_MODEL_ID = re.compile(
    r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:[-.](\d{1,2}))?(?:[-@]\d{8})?"
)


def parse_claude_model_id(model: str | None) -> tuple[str, int, int | None] | None:
    """(family, major, minor) of a whole Claude model id, or None.

    *minor* is None when the id names none (``claude-opus-4``). The whole id
    must match: a longer version (``claude-opus-5-6``) or a suffix
    (``:batch``) is not read as the version it starts with.
    """
    m = _CLAUDE_MODEL_ID.fullmatch((model or "").strip().lower())
    if m is None:
        return None
    minor = m.group(3)
    return m.group(1), int(m.group(2)), None if minor is None else int(minor)
