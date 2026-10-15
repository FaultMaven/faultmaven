"""``text_budget.fixed_text_like`` mints a closed set only (#1998 P2a, #2033 F1).

The truncation helpers cut a tool result before redaction (plain text) and
after it (minted case text), so the markers and fences they join are minted
beside minted text, as text with no case content. That claim is checked, not
trusted: ``fixed_text_like`` takes the end markers, the code fences, and the
elision notice with nothing but a count in it. Any other text — a runbook
title, a user's note — raises, minted context or not, and needs a real mint at
its own call site.
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.milestone_engine.text_budget import (
    FENCE_CLOSE,
    FENCE_OPEN,
    FIXED_TEXTS,
    KB_QA_ANSWER_ELIDED_TEMPLATE,
    KB_QA_ANSWER_TRUNCATED_MARKER,
    TOOL_RESULT_TRUNCATED_MARKER,
    fixed_text_like,
)
from faultmaven.infrastructure.security.model_content import (
    SOURCE_NON_CASE,
    provenance,
)
from tests.model_content import vetted

pytestmark = [pytest.mark.unit, pytest.mark.security]

#: Every text the six call sites pass (``_elide_answer_middle`` ×4,
#: ``_balance_code_fences``, ``StructuredOutputGenerator._truncate_tool_result``).
CLOSED = {
    "no end marker (already marked)": "",
    "kb_qa end marker": KB_QA_ANSWER_TRUNCATED_MARKER,
    "tool-result cut marker": TOOL_RESULT_TRUNCATED_MARKER,
    "opening fence": FENCE_OPEN,
    "closing fence": FENCE_CLOSE,
    "elision notice, 0": KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=0),
    "elision notice, 999": KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=999),
    "elision notice, 1,234": KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=1234),
    "elision notice, 12,345,678": KB_QA_ANSWER_ELIDED_TEMPLATE.format(
        dropped=12_345_678
    ),
}

#: Texts outside the set, the elision notice's near misses among them.
_HEAD, _TAIL = KB_QA_ANSWER_ELIDED_TEMPLATE.split("{dropped:,}")
OUTSIDE = {
    "a user's words": "raw user alice@example.com",
    "a runbook title as a marker": "\n[answer truncated: Rotate the db-01 password]",
    "the notice with text for a count": _HEAD + "alice@example.com" + _TAIL,
    "the notice with a negative count": _HEAD + "-5" + _TAIL,
    "the notice with ungrouped digits": _HEAD + "1234" + _TAIL,
    "the notice with text after it": KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=1)
    + "host 10.0.0.1",
    "a marker with text after it": TOOL_RESULT_TRUNCATED_MARKER + " 10.0.0.1",
    "the template itself": KB_QA_ANSWER_ELIDED_TEMPLATE,
}


def test_the_set_is_what_the_callers_pass():
    assert FIXED_TEXTS == {
        "",
        KB_QA_ANSWER_TRUNCATED_MARKER,
        TOOL_RESULT_TRUNCATED_MARKER,
        FENCE_OPEN,
        FENCE_CLOSE,
    }


@pytest.mark.parametrize("name", sorted(CLOSED))
def test_each_member_is_minted_beside_minted_text(name):
    fixed = CLOSED[name]
    minted = fixed_text_like(vetted("a tool result"), fixed)
    assert minted == fixed
    assert provenance(minted) == (SOURCE_NON_CASE, False)
    joined = vetted("a tool result") + minted
    assert provenance(joined) is not None


@pytest.mark.parametrize("name", sorted(CLOSED))
def test_each_member_stays_plain_beside_plain_text(name):
    fixed = CLOSED[name]
    out = fixed_text_like("a formatter's text", fixed)
    assert out == fixed and type(out) is str


@pytest.mark.parametrize("name", sorted(OUTSIDE))
@pytest.mark.parametrize("beside", ["minted", "plain"])
def test_any_other_text_raises(name, beside):
    text = vetted("a tool result") if beside == "minted" else "a formatter's text"
    with pytest.raises(ValueError, match="needs a real mint") as raised:
        fixed_text_like(text, OUTSIDE[name])
    # The refusal names the rule, never the text it refused.
    assert "alice" not in str(raised.value) and "10.0.0.1" not in str(raised.value)


def test_case_text_cannot_be_joined_to_a_minted_result_through_it():
    minted = vetted("a tool result")
    with pytest.raises(ValueError):
        minted + fixed_text_like(minted, "raw user alice@example.com")


def test_a_non_str_is_refused():
    with pytest.raises(ValueError):
        fixed_text_like(vetted("x"), None)
