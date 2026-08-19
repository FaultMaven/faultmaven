# Tool-Result Context Budget

Metrics defined in `faultmaven/core/investigation/tool_loop_metrics.py`, emitted
from the investigation tool loop (`MilestoneEngine._tool_augmented_generate`).
They answer one question: **what does the engine relay to the model, and what
does it cut off on the way?**

Gated on `ENABLE_METRICS=true` plus `prometheus_client` (graceful no-op
otherwise — see `faultmaven/infrastructure/shims/metrics.py`). A standalone run
does **not** set `ENABLE_METRICS`, so on that run the `tool_result_truncated`
log line below is the instrument, not the counters.

## What is being measured

Every tool result is truncated to `MilestoneEngine.TOOL_RESULT_MAX_CHARS`
(8000) before it re-enters the model's context. The result is cut head-first
and marked `[truncated]`, so the model can see that something is missing but
has no way to recover it.

Until [#1088](https://github.com/FaultMaven/faultmaven/issues/1088) that cut was
**completely silent** — no log line, no counter, nothing recorded that it had
fired. The clip rate was therefore not merely unmeasured but unmeasurable: the
only available estimate came from arithmetic across two unrelated log lines plus
a hand-measured wrapper size, for one tool, on one run.

The cap is a **single global constant shared by tools that are not alike**,
which is why every metric here is labeled by tool:

- `kb_qa` relays curated prose written to a synthesis prompt that explicitly
  asks for full procedure ("preserve procedural detail … compress only
  background context, never actionable steps"). A head-first cut removes
  exactly what that prompt was written to preserve — the remediation steps at
  the tail.
- `search_file` already shapes itself defensively around the cap: its
  `DEFAULT_CONTEXT_LINES` is kept small explicitly because "large context
  windows cause the `TOOL_RESULT_MAX_CHARS` truncation to cut off matches", and
  its compact result format is justified as fitting "far more results within the
  milestone engine's `TOOL_RESULT_MAX_CHARS` budget". Its clip rate therefore
  reports the health of a workaround, not an unmet need.

Two tools were working around the same unmeasured global limit and neither knew
its own rate.

## Metrics

- `faultmaven_tool_result_relayed_total{tool}` — every tool result handed back
  to the model, counted at the same point the cap is applied: after PII
  redaction and after per-tool formatting, so it counts the string that actually
  enters the context rather than what the tool returned. Error results and the
  `deep_analysis` per-turn-limit notice are relayed strings too and are counted;
  they are short and never clip, so they dilute the clip rate only in the
  direction of **understating** it. This is the denominator.
- `faultmaven_tool_result_truncated_total{tool}` — the subset of the above that
  exceeded the cap and was cut. The numerator.
- `faultmaven_tool_result_chars{tool}` — histogram of relayed-result size,
  observed **pre-cut**. Buckets are dense either side of 8000 because the
  decision this instruments is where to put that boundary: "just under" and
  "just over" have to be distinguishable, and the tail beyond it says whether
  the overflow is a trim or a different order of magnitude.

The `tool` label is bounded to the tool names the call actually offered.
A tool name arrives on a **model-supplied** tool call, so a model that invents
names would otherwise mint a Prometheus label per invention; anything not
offered folds into `unknown`.

## Log line

Emitted at `WARNING` from `faultmaven.core.investigation.milestone_engine`,
event name `tool_result_truncated`, with four structured fields:

```json
{"event": "tool_result_truncated",
 "logger": "faultmaven.core.investigation.milestone_engine",
 "level": "warning", "timestamp": "2026-08-18T11:42:45.462943Z",
 "tool": "kb_qa", "original_chars": 8319, "cap_chars": 8000,
 "dropped_chars": 319}
```

(`original_chars` is the *wrapped* result — for `kb_qa` that is the synthesized
answer plus ~590 characters of relay instruction — because the cap applies to
the wrapped string. The example above is the 7,729-character answer observed in
#1088.)

```bash
# Every clip in a run, as JSON records.
grep tool_result_truncated api.log

# Per-tool clip count and total characters discarded.
#
# Deduplicated, and that filter is load-bearing in two ways.
# A kb_qa answer the formatter trimmed can be re-inflated past the cap by
# redaction and cut again, emitting a SECOND record for one physical clip:
# unfiltered, this over-counts those answers in both columns. (The Prometheus
# counters are protected from that by the marker anchor; this log aggregation
# is not.)
#
# `dropped_chars` means the same thing at both sites -- SOURCE characters
# destroyed by the cut, not the overflow that preceded it and not the
# before/after length difference, which would net the inserted markers off the
# loss. It is comparable across tools and summable.
grep -h tool_result_truncated api.log \
  | jq -r 'select(.after_formatter_trim != true) | [.tool, .dropped_chars] | @tsv' \
  | awk '{n[$1]++; d[$1]+=$2} END {for (t in n) print t, n[t], d[t]}'
```

> The filter keys on `after_formatter_trim`, not on `at`. `at` says *which site
> cut* (`formatter` or `tool_loop`) and both are real cuts worth seeing;
> `after_formatter_trim` says *this result was already counted at the other
> site*, which is the property that makes a record a duplicate. Drop the filter
> to see second cuts deliberately.

`WARNING` rather than `INFO`: the event discards content the model was meant to
reason over. It is also the only surface available on a run without
`ENABLE_METRICS` — which is exactly the run the ceiling decision is most likely
to be made from, so the log is the instrument there and the counters are not.

## The load-bearing query

Read the pair, never the numerator alone — a fire count without its denominator
is a leading indicator at best.

```promql
# Per-tool clip rate. This is the number the ceiling decision turns on.
  sum by (tool) (rate(faultmaven_tool_result_truncated_total[24h]))
/ sum by (tool) (rate(faultmaven_tool_result_relayed_total[24h]))
```

```promql
# Is a tool's size distribution pressed against the cap, or comfortably below
# it? A tool at p50 well under 8000 gains nothing from a higher ceiling.
histogram_quantile(
  0.5, sum by (tool, le) (rate(faultmaven_tool_result_chars_bucket[24h]))
)
```

```promql
# How much is being thrown away, not just how often — the share of relayed
# results in each oversize band. A cluster just past the cap is a trim; a long
# tail is a tool producing a different order of magnitude than the cap assumes.
  sum by (tool) (rate(faultmaven_tool_result_chars_bucket{le="+Inf"}[24h]))
- sum by (tool) (rate(faultmaven_tool_result_chars_bucket{le="9000.0"}[24h]))
```

## What these metrics deliberately do not decide

They are read-only and never change what the engine relays. They also do not
argue for raising `TOOL_RESULT_MAX_CHARS`. What raising it would cost has two
halves, and #1088 states one of them incorrectly while omitting the other.

**The tool message costs one turn.** The issue argues the cap must stay low
because a relayed result "enters the conversation history and is re-sent on
every subsequent turn of that case". It is not. `MessageRole` has only `USER`,
`ASSISTANT` and `SYSTEM` — there is no tool role — and the only `"role": "tool"`
construction site is the local `messages` list inside
`_tool_augmented_generate`, rebuilt per call as `[system, prompt]`. A tool
result cannot reach `case_messages`. Inside the turn it is bounded twice: at
most `MAX_TOOL_ITERATIONS` (4) iterations, and `_bound_tool_loop_messages`
elides the oldest tool-exchange groups past the per-call budget (with a marker,
never a silent drop).

**The kb_qa content costs a bounded, decaying tail across turns.** "One turn" is
true of the tool *message* and false of the *content*, on the one tool this
issue is about. The kb_qa wrapper instructs the model to place the answer into
`agent_response` and preserve its diagnostic steps rather than collapse them.
That `agent_response` **is** persisted as a case message, and
`_build_graduated_history` replays the last `HISTORY_VERBATIM_TURNS` (3) turns
verbatim — smart-truncating any agent response over
`HISTORY_AGENT_TRUNCATE_THRESHOLD` (600 chars) — before collapsing older turns
to one-line summaries.

So KB content does recur across turns, through the **assistant** message rather
than the tool message, over a bounded window that decays as history graduates.
Neither "every subsequent turn" (wrong channel, too strong) nor "one turn"
(right about tool messages, too weak about kb_qa).

> **Measurement gap.** The recurring half of that cost lives in `agent_response`
> length and its share of persisted history, which none of these metrics
> observe. Deciding the ceiling from `tool_result_chars` alone decides it on the
> intra-turn half only. Size the copy-through half separately before moving the
> constant.

**The paired-constant guard.** `DocumentQATool.SYNTHESIS_MAX_TOKENS` is sized
against this cap in both directions and pinned by
`tests/unit/modules/agent/tools/test_kb_synthesis_budget.py` (#1086, merged).
That test **fails by design** if `TOOL_RESULT_MAX_CHARS` is raised on its own —
the guard forcing the paired decision. Change both constants together, or
neither.

The options on the table, and their trade-offs, are recorded in
[#1088](https://github.com/FaultMaven/faultmaven/issues/1088). The intended
sequence is instrument → observe one run → then decide with data.

## The run that was observed, and what it decided

One full standalone simulation, on the image built from the instrumentation
commit. Small, and deliberately reported as such — but unambiguous on the
question the instrumentation was added to answer.

| | relayed | truncated | clip rate | dropped |
|---|---|---|---|---|
| `kb_qa` | 5 | 3 | **60%** | 540, 655, 1249 chars |
| `search_file` | 3 | 0 | 0% | — |

> **These overflows are censored lower bounds, not a demand distribution.**
> The run predates #1094: synthesis was capped at `SYNTHESIS_MAX_TOKENS` (2000)
> with no retry, and three of the five answers sit within a few percent of what
> 2000 tokens can write. What was measured is therefore *how far past the relay
> budget a 2000-token-capped answer reached*, not how long the answer wanted to
> be. The true overflow is **at least** 540–1249 characters and may be
> materially larger — #1094 now retries once at up to 4000 tokens, so a
> post-#1094 run should be expected to show a wider band. `KB_QA_ANSWER_TAIL_SHARE`
> was sized against these numbers and inherits the caveat; the mechanism does not
> depend on the number being right, but anything that *does* must re-measure.

Two things follow.

**The global cap binds one tool.** Every clip in the run was `kb_qa`, and every
one fired at the *formatter* rather than at the loop's cut site. `search_file`
never came close, which is what its defensive `DEFAULT_CONTEXT_LINES` and
compact result format were for. So the "one global constant shared by tools
that are not alike" concern is real, but in the direction of the cap being
sized for the tools that already work around it and binding only the one that
does not.

**The measurement gap above narrowed. It did not close the way a first pass
suggested.** The recurring half — `agent_response` length and its share of
persisted history — has three regimes, not one, and only the third is bounded:

| when | path | copy-through cost |
|---|---|---|
| case has ≤ `HISTORY_VERBATIM_TURNS` (3) turns | `_build_verbatim_history` | **full length, no truncation call at all** |
| response ≤ `HISTORY_AGENT_TRUNCATE_THRESHOLD` (600) | `_smart_truncate_agent_response` returns it unchanged | **identity** |
| response > 600 on the graduated path | first + marker + last, trimmed | **bounded, ~≤900** |

`_build_verbatim_history` (`context_builder.py:2039-2057`) appends
`f"{role}: {content}\n"` with no truncation of any kind, so a KB answer relayed
on turn 1 is replayed at full length in the prompts for turns 2, 3 and 4. KB
lookups concentrate in exactly those early turns — all five in the observed run
fell in turns 1–5 — so for the first three prompts of a case the cost scales
**1:1 with what the model copied through**.

Measured on the graduated path only, over the 8 assistant turns of the observed
case:

| raw `agent_response` | 705 | 1199 | 1367 | 1828 | 2165 | 2485 | 2932 | 4739 |
|---|---|---|---|---|---|---|---|---|
| **`_smart_truncate_agent_response`** | 738 | 762 | 826 | 632 | 406 | 413 | 725 | 182 |

That is the third regime and it is genuinely bounded — the bound, ~≤900, is
what the argument needs. It is *not* evidence of being uncorrelated with input,
which was an extrapolation from a function measured outside the path that calls
it.

So the honest statement is narrower than "the objection does not survive". What
survives: the tool message itself is intra-turn and bounded twice, and the
copy-through is bounded above by what the **model** chooses to write, which in
the observed run compressed 5.2–8.7 KB answers into 1.2–2.9 KB responses. What
does not survive: any claim that the copy-through is invariant to the answer's
size. It is replayed whole for three prompts before any banding starts. Size
that regime before moving the ceiling.

**What was changed, and what was not.** Neither constant moved. The clip is not
principally a ceiling problem: the synthesizer was never told the ceiling
existed. It is instructed to preserve full procedural detail, given 2000 tokens
(up to 4000 since #1094's retry), and the relay then removes the overflow
**head-first** — deleting the remediation steps the prompt exists to protect,
and the `Sources:` line that `KB_QA_RELAY_SUFFIX` instructs the model to cite
"from the content above". So #1088's options 3 and 4 shipped and option 2 did
not:

- the synthesis prompt now states its allowance (`KB_ANSWER_RELAY_CHARS`), so
  the model drops background deliberately rather than having its tail removed;
- when the answer still overflows, the cut removes the **middle**
  (`KB_QA_ANSWER_TAIL_SHARE`), so the procedure's ending and its source line
  survive.

Because no constant moved, the paired-constant guard stays green and the
context budget is unchanged. Re-read the clip rate from this dashboard after a
run on the new prompt before considering the ceiling itself again.
