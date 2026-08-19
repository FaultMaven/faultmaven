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

# Prefixed to prose handed onward after a cut we could not recover from.
#
# This is a NOTICE bolted onto real content, not a placeholder that REPLACES
# it — the substitute-a-sentinel-string channel is the anti-pattern #1094
# retired, and reintroducing it here would undo that. The wording is aimed at
# an LLM reader as much as a human one: several consumers feed this text
# straight back into a model that must not treat it as a complete answer.
#
# It goes at the HEAD, and that is load-bearing rather than stylistic. Every
# consumer that annotates feeds a relay which trims to a character cap by
# KEEPING THE HEAD — ``MilestoneEngine._format_tool_result`` gives a kb_qa
# answer 7410 characters and cuts the rest, and ``_truncate_tool_result`` does
# the same for tier-2. A response that filled a 2000-token cap is roughly
# 7800-8200 characters, so a notice appended at the tail lands squarely in the
# dropped region — dropped, that is, in exactly the case where the answer is
# longest and its incompleteness matters most. At the head it always survives.
#
# This is the same conclusion the engine reached for system feedback, which is
# prepended for exactly this reason ("the turn record truncates feedback
# head-first" — see ``_withdraw_stale_solution_proposals``). Any notice that
# must outlive a head-keeping trim belongs at the front.
TRUNCATION_NOTICE = (
    "[TRUNCATED: the answer below hit the model's output limit and stops "
    "mid-answer. Treat it as incomplete — do not read the absence of further "
    "content as an absence of further information.]"
)


def annotate_if_truncated(text: str, response: LLMResponse) -> str:
    """Prefix :data:`TRUNCATION_NOTICE` to *text* when *response* was cut.

    Head-first, so the notice survives the head-keeping character caps every
    consumer of this text sits behind. See the constant for the arithmetic.
    """
    if not response.is_truncated:
        return text
    return f"{TRUNCATION_NOTICE}\n\n{text.lstrip()}"


async def generate_with_truncation_retry(
    call: Callable[[int], Awaitable[LLMResponse]],
    *,
    max_tokens: int,
    ceiling: Optional[int] = None,
    label: str = "llm call",
) -> LLMResponse:
    """Run *call*, and retry once with a bigger cap if the body was cut.

    The retry differs from the first attempt only in the cap, and the router's
    response cache does not key on the cap — so a cached first attempt would be
    handed straight back and no retry would actually happen, silently, reading
    exactly like a genuine second truncation. That is closed at the router
    rather than here: a response the provider reported as cut is never stored,
    so there is nothing under the key for the retry to be answered from, and no
    caller has to remember to pass a bypass flag (#1094).

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
