"""Reading JSON out of an LLM response body.

A caller asks for JSON with ``response_format={"type": "json_object"}``, which
is an **OpenAI-shaped** parameter. Providers that cannot express it drop it — the
Anthropic provider never reads the kwarg at all — so the request goes out with
no JSON constraint and the model answers the way it answers prose: correct JSON
inside a markdown fence. A bare ``json.loads`` then dies on the first backtick,
and the caller reports a parse error for a response that parsed fine one line
later (#1380).

That is not an Anthropic quirk. It is what every provider without a JSON mode
does, so the stripping belongs in one place rather than at each call site. It
had been written twice inside ``milestone_engine`` and nowhere else, which is
why the two conversion call sites — the ones an operator actually hits first —
had no protection at all.

Deliberately tolerant, and deliberately not a repair kit: it removes a FENCE and
surrounding prose, nothing more. A body that is genuinely not JSON still raises,
because a caller that cannot parse its response needs to fail rather than
receive a guess.
"""

import json
import re
from typing import Any

# ```json\n{...}\n``` — anywhere in the body, so "Here is the response:\n```json
# …```\nLet me know if…" is handled as well as a bare fenced block. Non-greedy,
# so the FIRST fenced block wins rather than a span across two of them.
_FENCED_BLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n```", re.DOTALL)


def strip_json_fence(content: str) -> str:
    """Return ``content`` with a surrounding markdown fence removed.

    Returns the input unchanged when there is no fence, so this is safe to apply
    to a provider that honoured ``response_format`` — on a raw JSON body it is
    the identity.
    """
    cleaned = content.strip()
    if "```" not in cleaned:
        return cleaned

    match = _FENCED_BLOCK_RE.search(cleaned)
    if match:
        return match.group(1).strip()

    # An unterminated fence, or one with no newline after the info string. The
    # regex needs both delimiters; this peels what is there.
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    return cleaned


def loads_llm_json(content: str, *, strict: bool = False) -> Any:
    """``json.loads`` over an LLM body, tolerating a markdown fence.

    ``strict=False`` matches the engine's existing parses: it admits literal
    control characters inside strings, which models emit in log excerpts and
    command output. Raises ``json.JSONDecodeError`` exactly as ``json.loads``
    does when the body is not JSON once the fence is off.
    """
    return json.loads(strip_json_fence(content), strict=strict)
