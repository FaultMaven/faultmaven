"""A check the case showed cannot run stays in front of the model (#2101).

The assistant re-asked for ``jcmd`` one turn after reporting it absent: the
fact lived only in its previous reply's middle paragraph, which the graduated
history elided, and no instruction said where it goes (the journal).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.prompts.context_builder import budget
from faultmaven.core.investigation.prompts.context_builder import history as cb
from faultmaven.core.investigation.prompts.context_builder.budget import (
    STATE_SUMMARY_TURN_THRESHOLD,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.tool_text import NO_TOOLS
from faultmaven.modules.case.contracts import ProblemStatus
from faultmaven.modules.case.domain.models.documentation import JournalEntry
from tests.case_builders import inquiry_case, investigating_case
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

_LIMIT = (
    "JDK Diagnostic Tools Are Blocked: `jcmd`, `jmap` and `jstat` failed with "
    "`executable file not found in $PATH`: the image is a minimal JRE."
)
_OLDER_MIDDLE = "The older middle paragraph names the GC configuration we read."
_RULE = "Never request a check a journal blocker names, nor offer it as a fallback"


def _reply(first: str, middle: str, last: str) -> str:
    """Three paragraphs, together past the smart-truncation threshold."""
    pad = " Detail." * 40
    return f"{first}{pad}\n\n{middle}{pad}\n\n{last}{pad}"


def _row(turn: int, role: str, content: str) -> dict:
    return {"turn_number": turn, "role": role, "content": content, "metadata": {}}


def _case(newest_reply: str) -> MagicMock:
    """Four turns: turn 1 is summarized, turns 2-4 render in RECENT TURNS."""
    rows = []
    for turn in range(1, 5):
        rows.append(_row(turn, "user", f"user line for turn {turn}"))
        reply = (
            newest_reply
            if turn == 4
            else _reply(f"Opening {turn}.", _OLDER_MIDDLE, f"Closing {turn}.")
        )
        rows.append(_row(turn, "assistant", reply))
    case = MagicMock()
    case.messages = rows
    case.turn_history = []
    return case


def _history(case) -> str:
    return cb._build_graduated_history(case, PromptFence(mint_token()))


def test_the_newest_reply_keeps_its_middle_paragraph():
    out = _history(_case(_reply("I read the output.", _LIMIT, "Next, run X.")))

    assert "RECENT TURNS:" in out
    assert "executable file not found in $PATH" in out
    # Older replies in the window are still smart-truncated.
    assert _OLDER_MIDDLE not in out
    assert "Opening 3." in out and "Closing 3." in out


def test_a_newest_reply_past_the_cap_is_cut_to_the_cap():
    cap = budget.LATEST_REPLY_MAX_CHARS
    head = "HEAD-SENTINEL opens the reply."
    tail = "TAIL-SENTINEL closes the reply."
    filler = "Filler sentence here. " * (cap // 30)
    newest = f"{head} {filler}MIDDLE-SENTINEL. {filler}{tail}"
    assert len(newest) > cap

    out = _history(_case(newest))
    line = next(x for x in out.splitlines() if x.startswith("ASSISTANT: HEAD-"))
    rendered = out[out.index(line) : out.index(tail) + len(tail)]

    assert rendered.startswith("ASSISTANT: " + head)
    assert "MIDDLE-SENTINEL" not in rendered
    assert len(rendered) - len("ASSISTANT: ") <= cap
    # Not the smart truncation: that keeps ~600 chars, the cap keeps ~4,000.
    assert len(rendered) > cap // 2


def _investigating(stage: str):
    case = investigating_case(
        statement="order-processing pods OOMKilled",
        symptom="order-processing pods OOMKilled every 40 minutes",
        current_turn=4,
        problem_status=ProblemStatus.VERIFIED,
    )
    if stage == "treatment":
        case.progress.solution_accepted = True
    return case


@pytest.mark.parametrize("given", [DEFAULT_GIVEN, NO_TOOLS], ids=["tools", "toolless"])
@pytest.mark.parametrize("stage", ["diagnosis", "treatment"])
def test_diagnosis_and_treatment_carry_the_blocker_rule(stage, given):
    prompt = get_prompt_for_case(_investigating(stage), "what now?", given_tools=given)
    flat = " ".join(prompt.split())

    assert flat.count(_RULE) == 1
    # Named as the schema spells it, so the model can write it.
    assert 'journal_entries with entry_type "blocker"' in flat


def test_inquiry_does_not_carry_it():
    prompt = get_prompt_for_case(
        inquiry_case(statement="pods restart"), "hi", given_tools=DEFAULT_GIVEN
    )
    assert _RULE not in " ".join(prompt.split())


def test_a_blocker_entry_reaches_a_compact_prompt():
    turn = STATE_SUMMARY_TURN_THRESHOLD + 2
    case = _investigating("diagnosis")
    case.current_turn = turn
    case.messages = [
        _row(t, role, f"{role} line for turn {t}")
        for t in range(1, turn)
        for role in ("user", "assistant")
    ]
    case.investigation_journal = [
        JournalEntry(
            turn=3,
            entry_type="blocker",
            content="jcmd/jmap/jstat not found in the JRE image; debug container denied",
        )
    ]

    prompt = get_prompt_for_case(case, "what next?", given_tools=DEFAULT_GIVEN)

    assert "RECENT TURNS:" not in prompt  # the compact history, not graduated
    assert "[T3] BLOCKER: jcmd/jmap/jstat not found in the JRE image" in prompt
