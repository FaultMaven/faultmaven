# Case telemetry stream

Per-turn, observe-only monitoring data about an investigation, emitted as
structured log lines. Its purpose is to make a **stalled case attributable to a
side** — engine, user, or both — without anyone reading the transcript.

Implementation: `faultmaven/core/investigation/case_telemetry.py`.
Design issue: [#1142](https://github.com/FaultMaven/faultmaven/issues/1142).

---

## Why it exists

`turns_without_progress` cannot answer "did the ENGINE stall this case?".
`MilestoneEngine._check_if_progress_made` is an OR over arms that straddle both
parties — user data (`novel_files_uploaded`) beside engine output (hypotheses,
solutions, milestones, evidence links) — so the counter is the NOR of all of
them. One live arm on either side holds it at 0, and it therefore fires only on a
**joint** stall, collapsing three different situations into one bit:

| situation | `turns_without_progress` | this stream |
|---|---|---|
| user dry, engine advancing | not stalled | `engine_advanced=true`, `user_supplied_new=false` |
| **engine dry, user still supplying** | **not stalled — invisible** | `engine_advanced=false`, `user_supplied_new=true` |
| both dry | stalled | both false, frontier flat |

Four of the predicate's nine arms — `novel_evidence_added`,
`novel_solutions_proposed`, `novel_files_uploaded`, `status_transitioned`,
`hypothesis_evidence_links_applied` — exist only on the engine's in-flight
working dict and were written nowhere. The stored `TurnProgress` keeps the *raw*
artifact lists instead, so `progress_made=true` with every stored list empty is
both the ordinary shape of a legitimate upload turn and the exact shape of a
lying counter. On the local corpus that is 376 of 1018 progress turns (36.9%).
Recording the decision where it is **made** is the point of this stream.

## What it is not

- **Not consumed by the engine.** Nothing reads it mid-case and it is never
  referenced in a prompt. An "idle" flag fed back into a running case recreates
  the nagging failure of #1138; prompt text naming a metric invites the model to
  satisfy the metric, and minting a spurious hypothesis to clear a flag is
  easier than investigating.
- **Not in the turn response.** The engine's arm counts cross the call boundary
  under a reserved key which `InvestigationService.process_turn` **pops** before
  the returned metadata is persisted onto the assistant `case_messages` row.
  That row is readable through the transcript API; this is monitoring data.
- **Not Prometheus.** `case_id` as a label is unbounded cardinality. Fleet-level
  counters live in `core/investigation/lifecycle_metrics.py`.
- **Not a verdict.** See *Known limits*.

## Emission

| property | value |
|---|---|
| logger | `faultmaven.telemetry.case` |
| level | `INFO`, **pinned on the logger itself** |
| rendering | root structlog handler (`ProcessorFormatter` + `ExtraAdder`) — fields render as top-level JSON keys |
| point | `InvestigationService.process_turn`, after the case is saved |
| cardinality | exactly **one row per consumed turn**, every route |

The level is pinned so a deployment running the root logger above INFO cannot
silence the stream. That is not hypothetical: the pre-existing
`grounding_assessment` trace is `logger.debug` behind an `isEnabledFor` guard
and produced **0 hits in 5,576 log lines** of a real run. Propagation is left on
so the one root handler renders it — a second handler could double-emit.

Emission is from the **service**, not the engine, because `case.current_turn` is
advanced there: that assignment is what "a turn was consumed" means. Several
routes answer without reaching `MilestoneEngine.process_turn` at all (`GREETING`,
`FILE_RECLASSIFICATION`) and a terminal case short-circuits inside it. An
engine-side emitter leaves those turns as **gaps**, and a gap is not a harmless
missing row — every streak computed over the stream silently shortens, so a
correct multi-turn confirmation handshake reads as an engine-dry run.

`path` records which route ran:

`llm` · `deterministic` · `terminal` · `greeting` · `reclassification` · `error`

`error` marks a turn whose number was consumed before the request failed. It is
worth a row so a provider outage is not read as an idle engine, and it carries
`user_message_chars` / `attachment_count` for the same reason — a failed turn on
which the user supplied data must not read as the user going quiet. It is gated
on the turn having been **consumed**, not merely on the case having loaded: a
failure before `current_turn` is advanced would otherwise emit a row carrying the
previous turn's number and collide with that turn's real row. Because the case
may not have been saved at that turn number, a consumer still dedups on
`(case_id, turn)` preferring the non-`error` row.

## ⚠️ The turn key repeats until #1264 lands

`turn` is not yet a reliable key, and analysis over this stream must not treat
`(case_id, turn)` as unique until [#1264](https://github.com/FaultMaven/faultmaven/issues/1264)
is fixed.

`turn_history` has two writers, both in the milestone engine, so a route that
never reaches it — a greeting, a file reclassification — appends nothing. The
repositories persist `Case.effective_current_turn`, which reads the last
`turn_history` entry, so on those turns the persisted counter stands still while
the in-flight one advances. `process_turn` reloads the case on every request and
derives `next_turn` from the persisted value, so **the very next turn re-derives
the number that was just used**. No process boundary is required; measured:

```
request                in-flight  persisted  telemetry row
"what is happening?"       1          1      (llm, turn 1)
"hi"          (greeting)   2          1      (greeting, turn 2)
"and now?"                 2          2      (llm, turn 2)   ← duplicate
```

Every row is individually correct — the arms, ledgers and counter all describe
the turn that actually ran. Only the *key* collides. The practical consequences:
a streak computed over a window containing a greeting is short by one, and a
simulator persona that opens with "hi" makes that run's streaks wrong.

Rows emitted before #1264 lands should be treated as an ordered sequence per
case (use arrival order, not `turn`) or excluded from turn-keyed analysis.

## Schema

`schema_version` is `1`. Adding a field is backwards-compatible and does not bump
it; changing a field's meaning or removing one does.

**Identity** — `schema_version`, `case_id`, `turn`, `path`, `case_state`

**The progress decision** — `progress_made`, `outcome`, `turns_without_progress`
(the settled post-turn value), `arms`, `user_supplied_new`, `engine_advanced`,
`gate_name`

`arms` carries two groups. The **predicate arms** are every arm
`_check_if_progress_made` actually scores, including `outcome_progress` — a
derived 0/1 for the one arm with no metadata key of its own, which the predicate
expresses as `outcome in (DATA_REQUESTED, HYPOTHESIS_TESTED)`. An arm missing
from that set is not cosmetic: the turn it fires on emits `progress_made=true`
with every recorded arm 0, which reads as a lying counter and, for an
engine-side arm, as an idle engine — both false accusations aimed at the engine
this stream exists to judge. The set is pinned by a test that parses the
predicate's own source, so an arm added there fails rather than shipping
unrecorded.

The **diagnostic counts** (`evidence_added`, `solutions_proposed`,
`files_uploaded`) are deliberately NOT scored by the predicate. #1136 narrowed
every artifact arm to its `novel_*` form because the LLM restates constantly;
carrying the raw count beside the novel one is what makes that restatement
visible — `evidence_added: 4` with `novel_evidence_added: 0` is the engine
re-emitting what the case already holds, which no other field shows.

**A row can never carry a fired predicate arm beside `progress_made: false`.**
The predicate is a NOR over the arms, so that shape is not a judgement call the
engine could make — it can only mean the score was read before the arm was
written. It was emittable on two paths until #1270: the generation path scored
five lines before `_check_automatic_transitions` wrote `status_transitioned`, and
the three routes that bypass the engine's turn bookkeeping (`GREETING`,
`FILE_RECLASSIFICATION`, the terminal short-circuit) scored in the consumed-turn
backstop without writing the reading back, so their rows reported a hardcoded
`false` beside `novel_files_uploaded: 1` and `turns_without_progress: 0`. Both
now score through the one monotone write (`score_progress`), and the invariant is
guarded arm-generically — the last reading of a turn must have seen every arm its
row reports, whichever arm that is. A consumer may treat the shape as a bug
report about the stream rather than a fact about the case.

Note this is the mirror of the lying-counter rule above, and both are needed:
`progress_made: true` with every arm 0 says the counter claimed more than the
arms support; a fired arm with `progress_made: false` says it claimed less.

Every arm is present on every row, zero rather than absent. Absent and zero read
differently to a rule keyed on "progress was claimed and every arm was 0": an
absent arm makes that rule silently unevaluable instead of false. For the same
reason the content guard rejects a malformed value **per entry**, never per
mapping — dropping a whole `arms` object over one bad member is worse than any
single wrong count.

**Input-disposition ledger** — `inputs_total`, `inputs_disposed`,
`inputs_undisposed`, `oldest_undisposed_input_age`

**Ask ledger** — `needs_total`, `needs_outstanding`, `needs_fulfilled`,
`needs_superseded`, `needs_raised_this_turn`, `oldest_outstanding_need_age`

**Frontier** — `hypothesis_count`, `hypothesis_states`, `causal_node_count`,
`causal_node_states`, `evidence_count`, `solution_proposed`, `solution_accepted`,
`solution_verified`, `solution_state`, `solution_feasible`, `mitigation_present`

**Assessment** — `verification_status`, `grade`, `cause_state`,
`symptom_verified`, `work_gate_passed`, `is_progress_stalled`, `mece_contested`,
`seam_divergence`, `seam_overclaim`

**Conformance / volume** — `validation_repairs`, `repair_pattern`,
`user_message_chars`, `attachment_count`

### Attribution

- `user_supplied_new` is defined as **`novel_files_uploaded > 0` and nothing
  else**. Typed prose has no upstream measurement point and cannot mechanically
  have one: deciding whether a message carries new content *is* the semantic
  judgment this stream is forbidden to make, and no evidence rows are minted
  from it. Pasted content does get an `uploaded_files` row and so does count.
- `engine_advanced` is true when any engine-side arm fired or a new need was
  raised this turn. Uploads are excluded — that exclusion is what lets the two
  sides be told apart.

### Why the frontier fields ship in the same event

`engine_advanced` is **gameable by construction**: its arms are LLM-authored
artifacts, so minting one spurious hypothesis per turn makes the engine
permanently "not idle", and a stream carrying only that bit would *certify* a
spinning engine as healthy — worse than no stream. The frontier counters are the
counterweight, and they ship in the same row so non-convergence (turns of
`engine_advanced` with no shrink in the residual candidate pool, the need pool or
the undisposed-input pool, and no grade rise) is computable by any consumer from
the stream alone — with no engine-side rule, and nothing for the model to
optimise against.

### Worked example — the row that is invisible today

A real open case in the corpus, 20 turns in, with `turns_without_progress = 0` —
which is to say the current signal reports it perfectly healthy. Its last three
turns each record `progress_made = true` with **every stored artifact list
empty**, and the user pasted data on each of them. Seven of its eight inputs
have no evidence row citing them; the case holds four evidence rows in total.

From the stored record it is not possible to say whether the engine advanced on
those turns or the counter lied, because the arm that fired was not written
down. That is the defect this stream closes. The same three turns read, in the
stream:

```
user_supplied_new: true    engine_advanced: false
arms: {novel_files_uploaded: 1, novel_evidence_added: 0, hypotheses_generated: 0, ...}
inputs_undisposed: 7       oldest_undisposed_input_age: 18
```

Twelve of 122 open multi-turn cases in the corpus (9.8%) sit in this
"engine dry, user still supplying" state on their trailing three-turn window.
None of them are visible to `turns_without_progress`.

## Content guard

Every value is a count, an id, an enum or a bounded flag. Two independent checks
run before emission and **both** are load-bearing:

1. a **name allowlist** (`FIELD_ALLOWLIST`) — rejects `user_id`, `filename` and
   anything else nobody named, even when the value looks harmless;
2. a **value shape check** — strings must be ≤ 64 chars and match
   `^[A-Za-z0-9_.:@/-]*$`, so prose is rejected on both length and punctuation.

The guard fails **closed**: a rejected field is dropped and warned about rather
than emitted, because a leak cannot be un-shipped, whereas a field dropped by
mistake is visible as a missing column on the very first row. Rejection inside a
histogram or the `arms` object is **per entry**, so one malformed member cannot
erase the whole mapping.

A failure in the payload builder is isolated — it must never break the turn it
observes — but not silent: the first one per process logs a WARNING with its
traceback, the rest DEBUG. Failure isolation becoming failure invisibility is the
same defect as a level gate; a total absence of rows is indistinguishable from
"no turns happened". It exists because
the natural way to extend this event is to lift a field off `TurnProgress` —
which carries `user_message_summary` and `agent_response_summary`, i.e. raw
transcript text.

## Known limits

**`inputs_undisposed` is a screen, not a verdict.** Disposition is observable
today in exactly one form: an `Evidence` row citing the file's
`source_file_id`. The other three the design calls for — `no_signal` (the engine
looked and found nothing), `duplicate`, `classification_failed` — have no
emission surface on the model, so an input the engine examined and correctly
found barren is indistinguishable from one it never looked at. Measured on the
local corpus:

| | files | share |
|---|---:|---:|
| disposed (evidence cites the file) | 402 | 58.1% |
| undisposed, engine produced evidence elsewhere in the window | 115 | 16.6% |
| undisposed, engine produced nothing in the window | 97 | 14.0% |
| younger than the 3-turn window | 78 | 11.3% |

So a naive "engine owes disposition" rule at M=3 flags 212 inputs across 97 of
the 185 cases that received any input at all, and **54% of those flags are turns
the engine demonstrably worked** —
it simply had nowhere to record what it concluded about that input. Reports must
not render this as "the engine ignored N inputs". Giving the engine a way to
declare a non-extraction disposition is an engine **behaviour** change and
belongs to its own issue; adding it converts that 16.6% from unattributable to
disposed.

**LLM health is only partly present.** A model failure must never render as an
idle engine, and today the stream separates them in two places: `path=error`
labels a turn that failed outright, and `validation_repairs` counts the
structural repairs the state validator applied to what the model emitted. The
finer signals the design asks for — structured-output retries, fallback provider
engaged, truncated response, tool-loop failure — are not on the turn's metadata
dict at all; promoting them from the LLM error handler and the router is its own
change. Until then a turn degraded by a struggling provider is visible as low
`engine_advanced` with no explanatory field, so **do not treat a run of
engine-dry turns as an engine defect without checking provider health for the
same window.**

**Streaks are consumer-side.** `engine_dry_streak` / `user_dry_streak` are
trivial derivations over a per-turn stream. Computing them in-engine would mean a
new persisted counter — a migration — whose only reader is offline, and a second
copy of a number the stream already determines.

**Prose-borne misbehaviour is out of reach.** A fabricated statistic narrated
into the reply (#1138's "67% evidence completeness") is invisible to anything
content-free. That class needs a semantic judge.

**`org_id` is deliberately absent.** `case_id` joins to an organisation
server-side for any consumer entitled to make that join; carrying it inline
raises the sensitivity of the stream for no reach the join does not provide.

## Relationship to `grounding_assessment`

`milestone_engine._log_grounding_assessment` stays as it is. It is a *grounding
and seam* trace — the grade × cause_state divergence, the per-node list — not a
progress ledger, and it is emitted from inside response application, before the
progress decision and the counter update, so its `turns_without_progress` is the
previous turn's. The assessment fields duplicated here are read from the same
persisted progress blob, so the two channels cannot disagree about a turn.
