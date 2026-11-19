"""A check the case showed cannot run stays in front of the model (#2101).

The assistant re-asked for ``jcmd`` one turn after reporting it absent: the
fact lived only in its previous reply's middle paragraph, which the graduated
history elided, and no instruction said where it goes (the journal).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.prompts.context_builder import budget
from faultmaven.core.investigation.prompts.context_builder import history as cb
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.tool_text import NO_TOOLS
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    MitigationRecord,
    ProblemStatus,
)
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


def _row(turn: int, role: str, content: str, **metadata) -> dict:
    return {"turn_number": turn, "role": role, "content": content, "metadata": metadata}


def _older(turn: int) -> str:
    return _reply(f"Opening {turn}.", _OLDER_MIDDLE, f"Closing {turn}.")


def _history(rows: list) -> str:
    case = SimpleNamespace(messages=rows, turn_history=[])
    return cb._build_graduated_history(case, PromptFence(mint_token()))


def _rows(newest_reply: str) -> list:
    """Four turns: turn 1 is summarized, turns 2-4 render in RECENT TURNS."""
    rows = []
    for turn in range(1, 5):
        rows.append(_row(turn, "user", f"user line for turn {turn}"))
        rows.append(
            _row(turn, "assistant", newest_reply if turn == 4 else _older(turn))
        )
    return rows


def test_the_newest_reply_keeps_its_middle_paragraph():
    out = _history(_rows(_reply("I read the output.", _LIMIT, "Next, run X.")))

    assert "RECENT TURNS:" in out
    assert "executable file not found in $PATH" in out
    # Older replies in the window are still smart-truncated.
    assert _OLDER_MIDDLE not in out
    assert "Opening 3." in out and "Closing 3." in out


def test_a_placeholder_newest_row_does_not_hide_the_reply_before_it():
    """Turn 4 timed out and the server wrote its placeholder: the reply the
    model reads last is turn 3's, so turn 5's prompt keeps it whole."""
    rows = [
        _row(2, "user", "u2"),
        _row(2, "assistant", _older(2)),
        _row(3, "user", "here is the jcmd output"),
        _row(3, "assistant", _reply("I read the output.", _LIMIT, "Next, run X.")),
        _row(4, "user", "u4"),
        _row(
            4, "assistant", "(no answer)", **{MESSAGE_METADATA_AGENT_SYNTHESIZED: True}
        ),
        _row(5, "user", "retry"),
    ]
    out = _history([_row(1, "user", "u1"), _row(1, "assistant", "a1"), *rows])

    assert "ASSISTANT: (no answer)" not in out
    assert "executable file not found in $PATH" in out


def test_a_newest_reply_past_the_cap_is_cut_to_the_cap():
    cap = budget.LATEST_REPLY_MAX_CHARS
    head = "HEAD-SENTINEL opens the reply."
    tail = "TAIL-SENTINEL closes the reply."
    filler = "Filler sentence here. " * (cap // 30)
    newest = f"{head} {filler}MIDDLE-SENTINEL. {filler}{tail}"
    assert len(newest) > cap

    out = _history(_rows(newest))
    start = out.index("ASSISTANT: " + head)
    rendered = out[start : out.index(tail) + len(tail)]

    assert "MIDDLE-SENTINEL" not in rendered
    assert len(rendered) - len("ASSISTANT: ") <= cap
    # Not the smart truncation: that keeps ~600 chars, the cap keeps ~4,000.
    assert len(rendered) > cap // 2


def _investigating(stage: str = "diagnosis", turn: int = 4):
    case = investigating_case(
        statement="order-processing pods OOMKilled",
        symptom="order-processing pods OOMKilled every 40 minutes",
        current_turn=turn,
        problem_status=ProblemStatus.VERIFIED,
    )
    if stage == "mitigation":
        case.progress.mitigation = MitigationRecord(proposed_at_turn=2, accepted=True)
    elif stage == "treatment":
        case.progress.solution_accepted = True
    return case


def _context(case, max_tokens: int) -> dict:
    return build_investigation_context(
        case,
        "what next?",
        max_tokens=max_tokens,
        provider_name="openai",
        model_name="gpt-4",
    )


def _five_turns():
    """Turn 5 of a case whose newest reply runs to 3,500 chars."""
    case = _investigating(turn=5)
    rows = []
    for turn in range(1, 5):
        rows.append(_row(turn, "user", f"user line for turn {turn} " + "paste " * 40))
        older = " ".join(f"Sentence {k} of turn {turn}." for k in range(1, 40))
        newest = "Opening. " + "Detail here. " * 90 + "\n\n" + _LIMIT + "\n\n"
        newest = (newest + "More analysis. " * 400)[:3500]
        rows.append(_row(turn, "assistant", newest if turn == 4 else older))
    case.messages = [*rows, _row(5, "user", "what next?")]
    return case


def test_a_long_newest_reply_does_not_push_the_history_to_compact():
    """A tight budget: the graduated form with the newest reply whole does
    not fit, and the one with it smart-truncated (``main``'s) does."""
    history = _context(_five_turns(), max_tokens=1200)["conversation_history"]
    assert "RECENT TURNS:" in history


def test_a_whole_newest_reply_never_costs_the_journal_a_blocker():
    """Below the state-summary threshold, at a budget where ``main`` keeps
    every blocker line, the whole newest reply does not take their room."""
    case = _five_turns()
    blockers = [
        JournalEntry(
            turn=t, entry_type="blocker", content=f"B{t} jcmd missing " + "z" * 150
        )
        for t in (2, 9, 11)
    ]
    findings = [
        JournalEntry(
            turn=t,
            entry_type="finding",
            content=f"finding {t}: " + "metaspace grows " * 10,
        )
        for t in range(3, 9)
    ]
    case.investigation_journal = sorted([*blockers, *findings], key=lambda e: e.turn)

    journal = _context(case, max_tokens=1600)["investigation_journal"]

    for entry in blockers:
        assert f"BLOCKER: {entry.content}\n" in journal


def test_a_cut_journal_keeps_an_older_blocker():
    """Past the state-summary threshold, under a budget that cuts the
    journal: the T3 blocker survives eleven newer findings."""
    case = _investigating(turn=17)
    case.messages = [
        _row(t, role, f"{role} line {t}")
        for t in range(1, 17)
        for role in ("user", "assistant")
    ]
    case.investigation_journal = [
        JournalEntry(
            turn=3,
            entry_type="blocker",
            content="jcmd/jmap/jstat not found in the JRE image",
        ),
        *[
            JournalEntry(
                turn=t,
                entry_type="finding",
                content=f"finding {t}: " + "metaspace grows " * 10,
            )
            for t in range(4, 15)
        ],
    ]

    journal = _context(case, max_tokens=900)["investigation_journal"]

    assert "entries omitted" in journal  # the journal was cut
    assert "[T3] BLOCKER: jcmd/jmap/jstat not found in the JRE image" in journal


@pytest.mark.parametrize("given", [DEFAULT_GIVEN, NO_TOOLS], ids=["tools", "toolless"])
@pytest.mark.parametrize("stage", ["diagnosis", "mitigation", "treatment"])
def test_every_stage_that_asks_for_checks_carries_the_rule(stage, given):
    prompt = get_prompt_for_case(_investigating(stage), "what now?", given_tools=given)
    flat = " ".join(prompt.split())

    assert flat.count(_RULE) == 1
    # Named as the schema spells it, so the model can write it.
    assert '`journal_entries` with `entry_type: "blocker"`' in flat
    assert "also when you raise `missing_critical_data`" in flat
    assert (
        "When no source still available can supply a `causal_verification` "
        "need's data, set `obtainability=unobtainable` on that need" in flat
    )


def test_inquiry_does_not_carry_it():
    """A control: INQUIRY renders no stage instructions, here or on main."""
    prompt = get_prompt_for_case(
        inquiry_case(statement="pods restart"), "hi", given_tools=DEFAULT_GIVEN
    )
    assert _RULE not in " ".join(prompt.split())
