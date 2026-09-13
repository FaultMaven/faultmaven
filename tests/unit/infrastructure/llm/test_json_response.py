"""Reading JSON out of an LLM body that arrived wrapped in a markdown fence.

#1380: `response_format={"type": "json_object"}` is an OpenAI-shaped parameter.
A provider that cannot express it drops it — the Anthropic provider never reads
the kwarg — so the model answers the way it answers prose: correct JSON inside a
fence. A bare `json.loads` died on the first backtick, and document conversion
failed on EVERY document under `CHAT_PROVIDER=anthropic`.

The logic existed, written twice inside `milestone_engine` and nowhere else,
which is exactly why the two conversion call sites had none.
"""

from __future__ import annotations

import json

import pytest

from faultmaven.infrastructure.llm.json_response import loads_llm_json, strip_json_fence

pytestmark = [pytest.mark.unit, pytest.mark.llm]

FENCE = "`" * 3
PAYLOAD = '{"is_actionable": true, "failure_modes": []}'


@pytest.mark.parametrize(
    "label,body",
    [
        ("raw json", PAYLOAD),
        ("json fence", f"{FENCE}json\n{PAYLOAD}\n{FENCE}"),
        ("JSON fence, upper", f"{FENCE}JSON\n{PAYLOAD}\n{FENCE}"),
        ("bare fence", f"{FENCE}\n{PAYLOAD}\n{FENCE}"),
        ("prose before", f"Here is the analysis:\n{FENCE}json\n{PAYLOAD}\n{FENCE}"),
        ("prose both sides", f"Sure:\n{FENCE}json\n{PAYLOAD}\n{FENCE}\nLet me know."),
        ("unterminated fence", f"{FENCE}json\n{PAYLOAD}"),
        ("leading whitespace", f"\n\n  {FENCE}json\n{PAYLOAD}\n{FENCE}\n"),
    ],
)
def test_every_shape_a_provider_without_json_mode_emits(label, body):
    assert loads_llm_json(body) == json.loads(PAYLOAD)


def test_raw_json_is_untouched():
    """On a provider that honoured response_format this must be the identity.

    Otherwise the fix for one provider is a regression for the others.
    """
    assert strip_json_fence(PAYLOAD) == PAYLOAD


def test_a_body_that_is_not_json_still_raises():
    """Tolerant of a fence, not a repair kit.

    A caller that cannot parse its response needs to fail rather than receive a
    guess — the conversion path turns this into a typed LLM_PARSE_ERROR.
    """
    with pytest.raises(json.JSONDecodeError):
        loads_llm_json("I'm sorry, I can't help with that.")

    with pytest.raises(json.JSONDecodeError):
        loads_llm_json(f"{FENCE}json\nnot actually json\n{FENCE}")


def test_the_first_fenced_block_wins():
    """Non-greedy: two fences must not be spanned into one unparseable blob."""
    body = (
        f'{FENCE}json\n{PAYLOAD}\n{FENCE}\nand also:\n{FENCE}json\n{{"b": 2}}\n{FENCE}'
    )

    assert loads_llm_json(body) == json.loads(PAYLOAD)


def test_control_characters_inside_strings_are_admitted():
    """`strict=False`, matching the engine's existing parses.

    Models put raw log excerpts and command output in string fields, and those
    carry literal control characters; a strict parse rejects the whole body.
    """
    body = f'{FENCE}json\n{{"reason": "line one\tand a tab"}}\n{FENCE}'

    assert loads_llm_json(body)["reason"] == "line one\tand a tab"


def test_the_engine_no_longer_carries_its_own_copy():
    """One decision, one place — the property this module exists to create.

    Two byte-equivalent fence-strippers lived in `milestone_engine`, and their
    being private to that file is the reason the conversion path had none. A
    third copy appearing anywhere re-creates #1380.
    """
    from pathlib import Path

    engine = (
        Path(__file__).resolve().parents[4]
        / "faultmaven/core/investigation/milestone_engine.py"
    ).read_text()

    assert "(?:json|JSON)?" not in engine, (
        "milestone_engine has a private fence-stripper again; route it through "
        "faultmaven.infrastructure.llm.json_response instead"
    )
    assert "loads_llm_json" in engine
