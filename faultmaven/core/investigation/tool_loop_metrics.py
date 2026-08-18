"""Context-budget telemetry for the investigation tool loop.

Separate from ``lifecycle_metrics`` on purpose. That module measures
**lifecycle invariants** and every counter in it maps back to an INV-XX row by
name; these measure the tool loop's *context budget*, which is not an invariant
and has no row to map to. Same shim, same graceful no-op, different question.

The question is: **what does the engine relay to the model, and what does it
cut off on the way?** ``MilestoneEngine.TOOL_RESULT_MAX_CHARS`` truncates every
tool result before it re-enters the model's context. Until #1088 that clip was
completely silent — no log line, no counter, nothing anywhere recorded that it
had fired. So "how often does this cost us something" was not a gap in the
sample, it was a structural property of the implementation: the only way to
estimate a clip rate was arithmetic across two unrelated log lines plus a
hand-measured wrapper size, for one tool, on one run.

The metrics here are **read-only** — they never change what the engine relays.
They exist so the ceiling can be decided from data rather than from argument.

What that decision actually costs has to be stated in two halves, because both
#1088 and the first draft of this module got it wrong in opposite directions.

**The tool message: one turn.** #1088 argues the cap must stay low because a
relayed result "enters the conversation history and is re-sent on every
subsequent turn of that case". It does not. ``MessageRole`` has only ``USER``,
``ASSISTANT`` and ``SYSTEM`` — there is no tool role — and the only
``"role": "tool"`` construction site is the local ``messages`` list inside
``_tool_augmented_generate``, built fresh per call as ``[system, prompt]``.
A tool result cannot reach ``case_messages``. Within the turn it is bounded
twice: at most ``MAX_TOOL_ITERATIONS`` (4) iterations, and
``_bound_tool_loop_messages`` elides the oldest tool-exchange groups once the
accumulation exceeds the per-call budget.

**The kb_qa content: a bounded, decaying tail across turns.** "One turn" is
true of the tool *message* and false of the *content*, on the one tool this
issue is about. The kb_qa wrapper instructs the model to place the answer into
``agent_response`` and "preserve key details, diagnostic steps, and resolution
procedures — do NOT collapse it into a single sentence". That ``agent_response``
IS persisted as a case message, and ``_build_graduated_history`` replays the
last ``HISTORY_VERBATIM_TURNS`` (3) turns verbatim before collapsing older ones
to one-line summaries — itself smart-truncating any agent response over
``HISTORY_AGENT_TRUNCATE_THRESHOLD`` (600 chars).

So KB content does recur across turns, through the assistant message rather
than the tool message, over a bounded window that decays as history graduates.
Neither "every subsequent turn" (wrong channel, too strong) nor "one turn"
(right about tool messages, too weak about kb_qa).

**A measurement gap this module cannot close.** The recurring half of that cost
lives in ``agent_response`` length and its share of persisted history, which
nothing here observes. Deciding the ceiling from ``tool_result_chars`` alone
decides it on the intra-turn half only. Read these metrics as the intra-turn
half, and size the copy-through half separately before moving the constant.

Read the pair, never the numerator alone (the ``lifecycle_metrics`` house rule
applies here too):

- ``tool_result_truncated_total`` over ``tool_result_relayed_total``, by tool,
  is the **clip rate** — the number the ceiling decision actually turns on.
- ``tool_result_chars`` is the size distribution the clip rate sits in. A tool
  whose distribution is pressed up against the cap is a tool being shaped by
  it; one comfortably below is not, and raising the cap buys it nothing.

Both matter per tool, because the cap is a single global constant and the tools
under it are not alike. ``kb_qa`` relays curated prose written to a prompt that
asks for full procedure; ``search_file`` already shapes itself defensively
around the cap (see its ``DEFAULT_CONTEXT_LINES`` note) and so reports a clip
rate that reflects a workaround, not a need. Neither knew its own rate.

Interpretation and PromQL:
``docs/operations/monitoring/tool-result-budget.md``.
"""

from faultmaven.infrastructure.shims.metrics import Counter, Histogram

# Denominator. Every tool result the loop hands back to the model, counted at
# the same point the cap is applied -- after redaction, after per-tool
# formatting -- so it counts the string that actually enters the context rather
# than what the tool returned. Error results and the deep_analysis-limit notice
# are relayed strings too and are counted; they are short and never clip, so
# they dilute the clip rate only in the direction of understating it.
tool_result_relayed_total = Counter(
    "faultmaven_tool_result_relayed_total",
    "Tool results relayed into the model's context by the investigation tool "
    "loop, labeled by ``tool``. The denominator for the truncation clip rate.",
    ["tool"],
)

# Numerator. One increment per relayed result that exceeded
# ``TOOL_RESULT_MAX_CHARS`` and was cut. Pairs with the ``tool_result_truncated``
# log line, which carries the same fields plus the overflow size.
tool_result_truncated_total = Counter(
    "faultmaven_tool_result_truncated_total",
    "Tool results that exceeded MilestoneEngine.TOOL_RESULT_MAX_CHARS and were "
    "truncated before entering the model's context, labeled by ``tool``. "
    "Divide by faultmaven_tool_result_relayed_total for the per-tool clip rate.",
    ["tool"],
)

# The distribution the clip rate sits in, observed PRE-truncation so the tail
# past the cap is visible rather than piled onto the cap value. Buckets are
# deliberately dense either side of 8000: the decision this instruments is
# where to put that boundary, so "just under" and "just over" have to be
# distinguishable, and the long tail beyond it says whether the overflow is a
# trim or a different order of magnitude.
tool_result_chars = Histogram(
    "faultmaven_tool_result_chars",
    "Size in characters of each relayed tool result, measured BEFORE the "
    "TOOL_RESULT_MAX_CHARS cut, labeled by ``tool``.",
    ["tool"],
    buckets=[
        500,
        1000,
        2000,
        4000,
        6000,
        7000,
        7500,
        8000,
        9000,
        10000,
        12000,
        16000,
        32000,
        64000,
    ],
)
