"""Shared recovery for responses the provider cut at the output cap.

``LLMResponse.stop_reason`` makes truncation *visible*; this module is what
most callers should *do* about it. The pattern is always the same — call, and
if the provider says it ran out of room, give it more room once and call again
— and writing it inline at every consumer is how it ends up written at none of
them.

Why this is not just reused from the engine
-------------------------------------------
``milestone_engine`` already has a truncation ladder (#513), and it is good,
but it is not extractable as-is: its ``_on_truncation`` is a closure over the
structured-output constants, a ``bypass_cache`` flag, and a ``with_retry``
loop whose ``OutputTruncationError(cap_reached=True)`` hands off to a
case-scoped minimal-prompt degrade needing a ``case`` and a ``user_message``.
None of that means anything to a KB synthesis call or a runbook conversion.

What generalises is only the first rung: raise the cap, try once more, and
tell the caller what state it ended in. The escalate-to-degrade tail stays
engine-only, where the case context that drives it lives.

Deciding what to do when the retry is ALSO cut is the caller's job, and the
answer differs per consumer — a read path returns the partial with a notice
attached, a write path refuses to persist at all. This helper therefore never
raises on truncation; it returns the response and lets the caller read
``is_truncated``.
"""

import logging
from typing import Awaitable, Callable, Optional

from .providers import LLMResponse

logger = logging.getLogger(__name__)

# Appended to prose handed onward after a cut we could not recover from.
#
# This is a NOTICE bolted onto real content, not a placeholder that REPLACES
# it — the substitute-a-sentinel-string channel is the anti-pattern #1094
# retired, and reintroducing it here would undo that. The wording is aimed at
# an LLM reader as much as a human one: several consumers feed this text
# straight back into a model that must not treat it as a complete answer.
TRUNCATION_NOTICE = (
    "\n\n[TRUNCATED: this response hit the model's output limit and stops "
    "mid-answer. Treat it as incomplete — do not read the absence of further "
    "content as an absence of further information.]"
)


def annotate_if_truncated(text: str, response: LLMResponse) -> str:
    """Append :data:`TRUNCATION_NOTICE` to *text* when *response* was cut."""
    if not response.is_truncated:
        return text
    return f"{text.rstrip()}{TRUNCATION_NOTICE}"


async def generate_with_truncation_retry(
    call: Callable[[int], Awaitable[LLMResponse]],
    *,
    max_tokens: int,
    ceiling: Optional[int] = None,
    label: str = "llm call",
) -> LLMResponse:
    """Run *call*, and retry once with a bigger cap if the body was cut.

    Args:
        call: Coroutine function taking the generation cap to use and returning
            the response. Callers close over everything else (prompt, model,
            temperature) so this helper stays agnostic to how the call is made
            — router ``route``, provider ``generate``, both work.
        max_tokens: The cap for the first attempt.
        ceiling: Highest cap the retry may use. Defaults to ``2 * max_tokens``,
            i.e. exactly one doubling. Bounds what a single logical call may
            spend chasing an answer that keeps overrunning.
        label: Short description used in the log lines.

    Returns:
        The response from the last attempt — which MAY still be truncated. Read
        ``is_truncated`` on it; do not assume success.

    ``UNKNOWN`` never triggers a retry. A provider that reports no stop reason
    is not evidence of a cut, and retrying every such call at double the cap
    would double the bill for the majority of traffic on providers that simply
    do not tell us.
    """
    response = await call(max_tokens)
    if not response.is_truncated:
        return response

    limit = ceiling if ceiling is not None else max_tokens * 2
    retry_cap = min(max_tokens * 2, limit)
    if retry_cap <= max_tokens:
        logger.warning(
            "%s truncated at max_tokens=%s, already at the ceiling (%s) — "
            "returning the partial response",
            label,
            max_tokens,
            limit,
        )
        return response

    logger.warning(
        "%s truncated at max_tokens=%s; retrying once at %s",
        label,
        max_tokens,
        retry_cap,
    )
    retried = await call(retry_cap)
    if retried.is_truncated:
        logger.warning(
            "%s truncated again at max_tokens=%s — returning the partial "
            "response for the caller to handle",
            label,
            retry_cap,
        )
    return retried
