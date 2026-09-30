import json
from typing import (
    Any,
    NamedTuple,
    Optional,
)

from faultmaven.exceptions import (
    TOKEN_LIMIT,
    LLMErrorCategory,
    declared_llm_category,
)


class _ToolLoopBudget(NamedTuple):
    """The two limits on one tool-loop call (#614), kept apart because they
    bound different things.

    - ``soft`` — the size/cost target on ``messages`` alone:
      ``prompt_target + tool_observation_max_tokens``. The ``tools=`` payload is
      not counted against it. It governs observation elision only, exactly as
      before #614.
    - ``window`` — the receiving model's context window when the resolver knows
      it, else ``None``; ``response_reserve`` is the output room the registry
      holds for that model. Together they give the HARD cap, :meth:`hard`.
    """

    soft: int
    window: Optional[int]
    response_reserve: int = 0

    def hard(self, max_tokens: int) -> Optional[int]:
        """The prompt tokens — ``messages`` plus ``tools=`` — a call may send
        when it asks for ``max_tokens`` of completion, so that prompt plus
        completion stays inside the window: ``window - max(max_tokens,
        response_reserve)``. ``None`` when the window is unknown. Keyed on the
        call's OWN completion cap, because the loop asks for more than the
        registry's reserve (8,000 against 6,000 by default) and a truncation
        retry doubles it (to 16,000)."""
        if self.window is None:
            return None
        return self.window - max(max_tokens, self.response_reserve)


# Stands in for the elided tool-exchange groups (INV-4: never a silent drop).
# One string, so the head-fit check before the loop and the bound inside it
# reserve the same marker.
_TOOL_LOOP_ELISION_MARKER = (
    "[Earlier tool calls and their results were elided to stay within "
    "the context budget. Re-run a search if you need those specifics.]"
)


def _tool_loop_message_tokens(
    m: dict,
    provider_name: Any,
    model: Any,
    token_cache: Optional[dict] = None,
) -> int:
    """The estimator the tool-loop bound applies to one message:
    ``estimate_tokens`` for the loop's provider name and model — tiktoken
    cl100k for openai/openrouter/anthropic/fireworks, ``len // 4`` for gemini,
    local, and the router (whose name, ``LLMRouter``, is not a provider)."""
    from faultmaven.utils.token_estimation import estimate_tokens

    # Memoize by object identity — `messages` is append-only within a turn and
    # every dict is held alive in it, so ids are stable and the large,
    # unchanging head is tokenized once, not once per iteration.
    key = id(m)
    if token_cache is not None and key in token_cache:
        return token_cache[key]
    parts = [str(m.get("content") or "")]
    if m.get("tool_calls"):
        parts.append(str(m.get("tool_calls")))
    # Reasoning artifacts are part of the WIRE payload and must be counted, or
    # this bound is not a bound. For a thinking-carrying assistant turn the
    # provider serializes provider_metadata["assistant_content"] (Anthropic
    # thinking / redacted_thinking blocks) or ["assistant_parts"] (Gemini parts
    # with thoughtSignatures) INSTEAD OF `content` — reasoning text that can
    # run to thousands of tokens. Estimating from `content` alone under-counts
    # those turns by roughly the size of their reasoning, so the bound would
    # report "under budget" while the request it green-lights blows the
    # provider's context limit — precisely the failure it exists to prevent.
    if m.get("provider_metadata"):
        parts.append(str(m.get("provider_metadata")))
    val = estimate_tokens(
        " ".join(parts),
        provider=provider_name if isinstance(provider_name, str) else "local",
        model=model if isinstance(model, str) else None,
    )
    if token_cache is not None:
        token_cache[key] = val
    return val


def _tool_payload_tokens(
    tools: Optional[list],
    provider_name: Any,
    model: Any,
    token_cache: Optional[dict] = None,
) -> int:
    """Estimated tokens of one call's ``tools=`` payload (#614): the same
    ``estimate_tokens`` call as the messages, over the JSON the definitions are
    sent as. Memoized under a tuple key, which cannot collide with the
    messages' int keys in a shared cache."""
    if not tools:
        return 0
    from faultmaven.utils.token_estimation import estimate_tokens

    key = ("tools", id(tools))
    if token_cache is not None and key in token_cache:
        return token_cache[key]
    val = estimate_tokens(
        json.dumps(tools, default=str),
        provider=provider_name if isinstance(provider_name, str) else "local",
        model=model if isinstance(model, str) else None,
    )
    if token_cache is not None:
        token_cache[key] = val
    return val


def _is_context_length_error(exc: Exception) -> bool:
    """True if *exc* is a provider context-length / prompt-too-long rejection.

    Provider-agnostic: the gateway may enforce a smaller window than our registry
    estimate (proxy/aggregator/reduced-context serving). We classify ONLY on
    length-specific phrases — deliberately NOT on a bare ``400 + "token"`` or the
    generic Pydantic phrase ``"string too long"``, which fire on ordinary
    request-validation errors and would trigger needless fallback retries.

    Two shapes reach here, and each has its own authoritative signal.

    The **retry-loop path** (``with_retry`` → ``handle_error`` classifies the
    overflow as ``TOKEN_LIMIT`` → ``_generate_structured_output_inner``
    re-raises a ``MilestoneEngineError``) stamps the shared ``TOKEN_LIMIT``
    error_code on the raised exception. Recognizing that deterministic engine
    signal — and walking the ``__cause__`` chain in case it is wrapped — is
    what makes the degrade-recovery in ``_generate_structured_output`` actually
    reachable for an overflow that surfaced through the retry loop. Without it
    a *recoverable* overflow fails the turn instead of degrading to the minimal
    fallback prompt (the NO-COLLAPSE guarantee; #662).

    A **raw provider exception** (proxy/aggregator path) carries the typed
    ``LLMErrorCategory`` the provider boundary stamped on it (#509). This used
    to re-match ``CONTEXT_OVERFLOW_PHRASES`` — the same tuple the error handler
    matched, imported from it so the two could not drift. Asking the exception
    what it IS removes the drift question rather than managing it: there is one
    classification, made once, and both readers get the same answer because
    there is only one answer.
    """
    # Deterministic engine signal from the retry-loop path (see docstring).
    # ``seen`` bounds the walk: ``__cause__`` is assignable, so a hand-built cycle
    # would otherwise spin here — and hanging this classifier would stall the very
    # turn the degrade path exists to rescue. (We cannot reuse
    # ``api.exception_handlers._walk_cause_chain``; core importing the API layer
    # breaks import-linter contract 2.)
    cursor: Optional[BaseException] = exc
    seen: set[int] = set()
    while cursor is not None and id(cursor) not in seen:
        if getattr(cursor, "error_code", None) == TOKEN_LIMIT:
            return True
        seen.add(id(cursor))
        cursor = cursor.__cause__

    return declared_llm_category(exc) is LLMErrorCategory.CONTEXT_OVERFLOW


KB_QA_RELAY_PREFIX = (
    "KNOWLEDGE BASE RESULT — Place the content below into the "
    "`agent_response` field of your structured response. Preserve "
    "key details, diagnostic steps, and resolution procedures — do "
    "NOT collapse it into a single sentence.\n\n"
)
# Appended to a kb_qa answer that ``_format_tool_result`` trimmed to fit the
# relay wrapper (#1086). Named rather than inlined because the tool loop reads
# it back: a result carrying this marker has ALREADY been measured into the
# tool-result budget metrics at the formatter, against its true pre-trim size,
# and must not be measured a second time at the cut site (#1088).
KB_QA_ANSWER_TRUNCATED_MARKER = "\n[answer truncated]"

KB_QA_RELAY_SUFFIX = (
    "\n\n"
    "SOURCE CITATION: At the end of `agent_response`, append a "
    "compact source line in italic markdown using this exact format:\n"
    "*Sources: [title1], [title2]*\n"
    "Use only the primary source title(s) from the content above. "
    "One short line — no verbose attribution paragraph.\n\n"
    "Then return the structured response by calling the response "
    "schema tool. Do not reply with plain text."
)


# Fraction of the answer allowance reserved for the answer's TAIL when a kb_qa
# answer overflows it.
#
# The head-first cut this replaces was wrong for this one payload, for two
# reasons that are properties of the payload rather than of the cap:
#
# 1. The synthesis prompt is written to load the tail. It asks the model to
#    "preserve procedural detail -- include full diagnostic steps, commands,
#    and resolution procedures" and to "compress only background context,
#    never actionable steps". The tail of a procedure is its remediation, so a
#    head-keeping cut deletes exactly what the prompt was written to protect
#    and keeps the background it was told to compress.
# 2. ``UnifiedKBConfig.format_response`` appends the source list to the very
#    end of the answer, and ``KB_QA_RELAY_SUFFIX`` then instructs the model to
#    cite "the primary source title(s) from the content above". A head-keeping
#    cut removes that line before the model reads the instruction depending on
#    it, so on a trimmed answer the citation requirement is unsatisfiable from
#    the content it names.
#
# 0.35 rather than a smaller share because the tail has to hold a whole
# remediation section plus the source line, not just the last paragraph.
# Measured against the run that produced #1088's numbers: answers overflowed
# the allowance by 540-1249 characters (7-17% of the answer), so a 35% tail
# reservation puts every observed elision strictly inside the middle -- the
# preserved tail is real answer text in every case seen, not padding.
#
# Those overflows are CENSORED LOWER BOUNDS, not a demand distribution. The run
# predates #1094: synthesis was capped at 2000 tokens with no retry, and three
# of the five answers sit within a few percent of what 2000 tokens can write.
# So what was measured is how far past the budget a capped answer reached, not
# how long the answer wanted to be, and a post-#1094 run should be expected to
# show a wider band. The mechanism does not depend on the number -- the elide
# fires whatever the overflow, and the budget is hard-bounded either way -- but
# do not treat 0.35 as tuned without re-measuring. See
# docs/operations/monitoring/tool-result-budget.md.
KB_QA_ANSWER_TAIL_SHARE = 0.35

# Marks where content was removed. Carries the count because the model is asked
# to relay this answer onward and "some of the middle is missing" is a different
# instruction from "the answer ends here" -- which is what the end-anchored
# KB_QA_ANSWER_TRUNCATED_MARKER alone used to imply.
KB_QA_ANSWER_ELIDED_TEMPLATE = (
    "\n\n[... {dropped:,} characters elided from the middle of this answer to "
    "fit the relay budget. What follows is the TAIL of the answer as it was "
    "received — its closing steps and source line, where it reached them. "
    "...]\n\n"
)
# "as it was received", not "the end of the answer", and that hedge is
# load-bearing rather than cautious phrasing. An answer can arrive already
# incomplete: when a #1094 retry still comes back ``finish_reason=length``,
# ``truncation.TRUNCATION_NOTICE`` is prepended saying the text "stops
# mid-answer". A marker asserting the tail below IS the end would then
# contradict it, in the same string, with nothing to tell the model which to
# believe. Both are now true at once -- the notice says the answer was cut
# short, this says what follows is the end of what arrived.


def _elide_answer_middle(content: str, budget: int) -> tuple[str, int]:
    """Fit *content* into *budget* characters by removing its MIDDLE.

    Returns the fitted text and the number of *content* characters destroyed.
    That count is returned rather than derived by the caller because the two
    are not the same number: the result carries inserted markers, so a
    before/after length difference nets those off and under-reports what was
    actually lost by roughly their combined length. ``dropped_chars`` is the
    field the ceiling gets sized from (#1090), so it has to mean one thing.

    Keeps the opening (framing and the first diagnostic steps) and the closing
    (remediation and the ``Sources:`` line), which is the opposite of the
    head-first cut every other tool result gets -- see
    ``KB_QA_ANSWER_TAIL_SHARE`` for why kb_qa is the exception.

    Both markers are inside the returned budget. On every path reachable from
    the two production callers the result ENDS on
    ``KB_QA_ANSWER_TRUNCATED_MARKER``, which is load-bearing rather than
    decorative: the tool loop reads that anchor back to know this cut already
    fed the truncation metrics, so one relayed result yields exactly one
    observation (#1090). It reads as an overall "this answer was trimmed" flag;
    the inline marker says where. Content arriving already marked keeps the
    marker it has rather than gaining a second one -- the anchor holds either
    way, since slicing the tail carries the existing marker along with it.

    "Reachable" is the honest qualifier, not a hedge. The degenerate-budget
    branch below slices to ``budget - len(end_marker)``, and on
    already-marked content that slice can land inside the marker it was meant
    to preserve. Both callers pass a budget three orders of magnitude larger,
    so the corner is unreachable today; it is called out rather than asserted
    away because a wrapper edit is exactly what would open it.
    """
    # Nothing to do. Both production callers already gate on the overflow, so
    # this is defensive rather than load-bearing -- but without it the head and
    # tail slices OVERLAP when the budget exceeds the content, duplicating text
    # into the result and returning a negative dropped count. A helper that
    # reports a nonsense number on an easy input is a trap for the next caller.
    if len(content) <= budget:
        return content, 0

    # Already marked means this is the SECOND cut on one answer: the formatter
    # trimmed it, then redaction expanded it back past the cap. One marker
    # still says the true thing; two in a row just read as noise to the model
    # that has to relay this.
    already_marked = content.endswith(KB_QA_ANSWER_TRUNCATED_MARKER)
    end_marker = "" if already_marked else KB_QA_ANSWER_TRUNCATED_MARKER

    # Sized on a worst-case count so the marker cannot itself push the result
    # past the budget once the real number is substituted in.
    elided_len = len(KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=len(content)))
    # Reserved only when repair could actually fire. An answer with no fence in
    # it cannot come back with an odd fence count, so holding the room back
    # unconditionally spent up to 8 characters of answer on a repair that was
    # never possible -- on the majority of KB answers, which carry no fenced
    # block at all. The test is exact rather than heuristic: no ``` in, no ```
    # out, because both slices are substrings of the content.
    fence_reserve = FENCE_REPAIR_RESERVE if "```" in content else 0
    available = budget - len(end_marker) - elided_len - fence_reserve

    # Degenerate budget (a wrapper edit that leaves almost no room): fall back
    # to the plain head-first cut rather than emit markers with no answer
    # between them.
    if available < 2:
        kept = max(0, budget - len(end_marker))
        return content[:kept] + end_marker, len(content) - min(kept, len(content))

    tail_chars = int(available * KB_QA_ANSWER_TAIL_SHARE)
    head_chars = available - tail_chars

    head = _trim_head_to_paragraph(content[:head_chars])
    # Sliced from the end, so an existing marker rides along on the tail and
    # the result still ends on the anchor the tool loop looks for.
    tail = _trim_tail_to_paragraph(content[len(content) - tail_chars :])

    # Counted BEFORE fence repair. Repair inserts characters that were never in
    # the answer, so measuring the kept slices afterwards would credit them as
    # retained content and under-report the loss -- the same netting error the
    # returned count exists to avoid.
    dropped = len(content) - len(head) - len(tail)

    # Balanced independently, and only after the budget has been reserved for
    # it (FENCE_REPAIR_RESERVE): an unbalanced fence in either piece makes
    # everything after it render, and read, as code.
    head = _balance_code_fences(head)
    if tail.count("```") % 2:
        tail = "```\n" + tail

    elided = KB_QA_ANSWER_ELIDED_TEMPLATE.format(dropped=dropped)
    return head + elided + tail + end_marker, dropped


# Most a paragraph realignment may spend to land on a clean boundary.
#
# Bounded in absolute characters rather than as a share of the slice, because
# the cost being traded is answer text and the benefit is cosmetic. A share --
# "up to a third" -- scales the cosmetic allowance with the budget, so on the
# standard 7,410 it could discard ~2,400 characters to tidy two seams, on
# answers whose measured overflow was 540-1,249. A paragraph that does not
# begin within this many characters is left cut mid-sentence, which the
# markers on either side already explain.
PARAGRAPH_REALIGN_MAX_CHARS = 400


def _trim_head_to_paragraph(head: str) -> str:
    """Back the head up to a line boundary, if one is cheaply reachable.

    Paragraph first, then any line break. A runbook answer's most valuable
    region is a fenced block or a numbered command list, and neither contains a
    blank line -- so a paragraph-only search walks straight past the whole
    block and leaves the seam mid-command (``kubectl get pod pod-01``, verb
    intact, target truncated). A single newline is a real boundary there.
    """
    return _rewind_to_boundary(head, ("\n\n", "\n")).rstrip()


def _trim_tail_to_paragraph(tail: str) -> str:
    """Advance the tail to a line boundary, if one is cheaply reachable."""
    for sep in ("\n\n", "\n"):
        cut = tail.find(sep)
        if 0 <= cut <= PARAGRAPH_REALIGN_MAX_CHARS:
            return tail[cut:].lstrip()
    return tail.lstrip()


def _rewind_to_boundary(text: str, separators: tuple) -> str:
    """Back *text* up to the nearest of *separators* within the realign bound."""
    for sep in separators:
        cut = text.rfind(sep)
        if cut >= 0 and len(text) - cut <= PARAGRAPH_REALIGN_MAX_CHARS:
            return text[:cut]
    return text


# Room held back so fence repair cannot push the result past the budget.
# One opening fence for the tail and one closing fence for the head, each with
# its newline -- repair adds at most one of each.
#
# Applied only when the content actually contains a fence (see the call site).
# The reservation is otherwise pure loss: it is subtracted from the answer's
# room whether or not repair fires, and for a KB answer with no fenced block it
# never can.
FENCE_REPAIR_RESERVE = len("\n```") + len("```\n")


def _balance_code_fences(text: str) -> str:
    """Close a fenced block the elide cut open.

    The drop zone can contain the closing ``\u0060\u0060\u0060`` of a block whose opening
    survived in the head, or the opening of one whose close survived in the
    tail. Either way the relayed answer carries an unbalanced fence, and every
    downstream reader -- the model asked to relay it, and the Dashboard
    rendering the transcript as markdown -- then treats the rest of the answer
    as code. Cheaper to close it than to reason about which side is short.
    """
    if text.count("```") % 2 == 0:
        return text
    return text + "\n```"
