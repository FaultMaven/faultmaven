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
import re

import pytest

from faultmaven.infrastructure.llm.json_response import (
    json_payload_text,
    loads_llm_json,
    strip_json_fence,
)

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
        # Single-line shapes — a model emitting short JSON puts it all on one
        # line. The newline-anchored pattern missed these and the peel returned
        # "", producing the exact opaque error this module exists to remove.
        ("single line, spaced", f"{FENCE}json {PAYLOAD} {FENCE}"),
        ("single line, tight", f"{FENCE}json{PAYLOAD}{FENCE}"),
        ("no newline before close", f"{FENCE}json\n{PAYLOAD}{FENCE}"),
        # Unterminated AND single-line: the only shape that reaches the peel
        # with nothing left on the other side. Without the non-empty guard the
        # peel returns "" and the body is destroyed.
        ("unterminated, single line", f"{FENCE}json {PAYLOAD}"),
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

    Reads the IMPORTED module rather than a path under the checkout: a
    `Path(__file__).parents[N]` walk raises FileNotFoundError wherever the
    package is installed away from its source tree (the container image, a
    wheel), turning the guard into an error instead of a check. And it asserts
    on behaviour — that the engine delegates — rather than only on the absence
    of one regex spelling, which a capturing group or a reordered alternation
    would evade.
    """
    import inspect

    from faultmaven.core.investigation import milestone_engine

    source = inspect.getsource(milestone_engine)

    assert "json_payload_text" in source, "the engine no longer delegates"
    # No local compilation of a fence pattern, however it is spelled.
    fence_patterns = re.findall(r"re\.(?:compile|search|match)\([^\n]*```", source)
    assert not fence_patterns, (
        f"milestone_engine has a private fence-stripper again: {fence_patterns}; "
        "route it through faultmaven.infrastructure.llm.json_response instead"
    )


def test_the_payload_text_is_what_a_failing_parse_reports_against():
    """The #513 truncation ladder depends on this, so it is asserted directly.

    `is_truncated_json_error` compares a JSONDecodeError.pos against
    len(content). If the parse ran on a de-fenced copy while the caller kept the
    fenced original, the offset and the length come from different strings and
    the guard answers False — the ladder stops re-running and a recoverable
    truncated turn dies instead.
    """
    truncated_fenced = f'{FENCE}json\n{{"agent_response": "x", "hyps": [1,2'

    text = json_payload_text(truncated_fenced)
    with pytest.raises(json.JSONDecodeError) as excinfo:
        json.loads(text, strict=False)

    assert excinfo.value.pos == len(
        text
    ), "the reported offset must be measurable against the text the caller holds"


def test_payload_text_is_the_identity_on_a_body_that_parses():
    """Including one whose strings contain a fence.

    Stripping unconditionally mangles `{"cmd": "```\nls\n```"}` into `ls` and
    then fails — and runbook analysis output carries fenced command snippets in
    string fields routinely, so this is a real body, not a contrived one.
    """
    for body in (PAYLOAD, f'{{"cmd": "{FENCE}\nls\n{FENCE}"}}'):
        assert json_payload_text(body) == body
        assert loads_llm_json(body) == json.loads(body, strict=False)
