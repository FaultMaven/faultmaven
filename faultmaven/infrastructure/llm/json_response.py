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

# ```json … ``` anywhere in the body, so "Here is the response:\n```json
# …```\nLet me know if…" is handled as well as a bare fenced block. Non-greedy,
# so the FIRST fenced block wins rather than a span across two of them.
#
# ``\s*`` rather than ``\n`` around the payload: a model that emits short JSON
# puts the whole thing on ONE line (```json {"a": 1} ```), and a newline-anchored
# pattern misses it — which then fell through to the peel below and returned the
# EMPTY STRING, producing the exact "Expecting value: line 1 column 1 (char 0)"
# this module exists to eliminate.
_FENCED_BLOCK_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)

# Just the opening marker plus its info string, for an unterminated fence.
_OPENING_FENCE_RE = re.compile(r"^```(?:json|JSON)?[ \t]*\n?")


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

    # An UNTERMINATED fence — the regex needs both delimiters, so peel the
    # opening marker (and its info string) wherever it sits. Peeling the whole
    # first LINE is wrong when the body is a single line: it drops the payload
    # too and returns "", which is worse than not stripping at all.
    if cleaned.startswith("```"):
        peeled = _OPENING_FENCE_RE.sub("", cleaned, count=1)
        if peeled.endswith("```"):
            peeled = peeled[: -len("```")]
        peeled = peeled.strip()
        if peeled:
            return peeled

    return cleaned


def json_payload_text(content: str, *, strict: bool = False) -> str:
    """Return the text that a JSON parse of ``content`` should be attempted on.

    Identity when ``content`` already parses — so a body from a provider that
    honoured ``response_format``, including one whose string values contain a
    triple backtick, is never rewritten. De-fenced when it does not.

    Resolved BEFORE parsing, and separate from :func:`extract_json_payload`,
    because the caller that needs it needs it on the FAILURE path. The engine
    measures a ``JSONDecodeError.pos`` against ``len(content)`` to decide
    whether the body was cut and the ``max_tokens`` ladder should re-run
    (#513) — and on a truncated fenced body the parse raises, so any function
    that hands back the text only on success leaves the caller holding the
    fenced original and comparing an offset from one string against the length
    of a longer one. The guard then answers False and the ladder silently stops
    engaging.
    """
    try:
        json.loads(content, strict=strict)
    except json.JSONDecodeError:
        return strip_json_fence(content)
    return content


def extract_json_payload(content: str, *, strict: bool = False) -> tuple[Any, str]:
    """Return ``(parsed, the exact text that parsed)``.

    **Parses FIRST and strips only on failure.** Stripping unconditionally is
    not the identity on a valid body: JSON whose string values contain a triple
    backtick — a runbook analysis quoting a fenced command, which this codebase
    produces routinely — gets mangled into its own snippet and then fails to
    parse. Trying the body as-is means a provider that honoured
    ``response_format`` is never touched, and only a body that genuinely did not
    parse is reinterpreted.

    Returning the text alongside the object is not a convenience. A caller that
    inspects a *later* failure against the body — the engine measures a
    ``JSONDecodeError.pos`` against ``len(content)`` to decide whether a
    response was truncated and the ``max_tokens`` ladder should re-run — has to
    hold the string that was actually parsed. Comparing an offset from the
    stripped body against the length of the fenced one silently disables that
    ladder.

    ``strict=False`` matches the engine's existing parses: it admits literal
    control characters inside strings, which models emit in log excerpts and
    command output.
    """
    try:
        return json.loads(content, strict=strict), content
    except json.JSONDecodeError:
        stripped = strip_json_fence(content)
        return json.loads(stripped, strict=strict), stripped


def loads_llm_json(content: str, *, strict: bool = False) -> Any:
    """``json.loads`` over an LLM body, tolerating a markdown fence.

    For callers that need only the object. Use :func:`extract_json_payload`
    where the text that parsed matters too.
    """
    return extract_json_payload(content, strict=strict)[0]
