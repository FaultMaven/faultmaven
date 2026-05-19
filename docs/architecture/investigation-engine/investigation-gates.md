# Investigation Gates — User-Confirmed Decision Points

## Status

**Design — not yet implemented.** This document supersedes the path-selection
sections of [investigation-lifecycle-logic.md](investigation-lifecycle-logic.md)
(specifically §10 path routing and the USER_CHOICE handling in §1283-1394).

Once implemented, the lifecycle doc will be updated to point here for all
path-related material.

## Motivation

Today the system makes two decisions that materially shape an investigation —
*what problem are we solving* and *how do we attack it* — but only the first is
explicitly user-confirmed. Path selection is computed from a deterministic
Urgency × Temporal matrix and silently committed; the `USER_CHOICE` enum value
exists to mark "ambiguous case, ask the user" but has **no consumer** — no
prompt branch, no suggestion, no UI surface. Cases assigned `USER_CHOICE`
proceed with default DIAGNOSIS instructions, behaving implicitly as
`ROOT_CAUSE`.

The deterministic matrix also can't see what's happening outside the system.
If a user has already mitigated out-of-band (someone restarted the service,
traffic rolled over), the telemetry still reads ONGOING + HIGH and the system
recommends mitigation that's no longer needed. The user has context the data
doesn't.

This design replaces silent commitment with three explicit confirmation gates,
each carrying a system recommendation that the user accepts or overrides.

## Three gates

| Gate | Triggered when | Decision | State change on confirmation |
| --- | --- | --- | --- |
| **1. Problem statement** | Agent has populated `proposed_problem_statement` | Is this the right problem? | `inquiry.problem_statement_confirmed = True` |
| **2. Investigation path** | Gate 1 passed + `problem_verification` populated | Mitigation-first or root-cause-only? | `path_selection.path` set, `path_selection.user_confirmed = True`. Triggers INQUIRY → INVESTIGATING transition. |
| **3. Post-mitigation continuation** | Path is `MITIGATION_FIRST` and `mitigation_verified` just became `True` | Continue with RCA against the now-stable problem, or close as mitigation-sufficient? | Either `path_selection.rca_after_mitigation_confirmed = True` (continue), or transition INVESTIGATING → CLOSED with `closure_reason = mitigation_sufficient` |

Gate 1 already exists. Gate 2 replaces the silent matrix commitment. Gate 3 is
new, and addresses the highest-risk leg of the lifecycle — the loop back from
completed mitigation into RCA, where today the case has no explicit checkpoint.

## Shape — same for every gate

Each gate follows the same propose-then-confirm shape, surfaced inline in chat:

```
Agent message (inline reply)
└─ recommendation + rationale
└─ COOPERATIVE suggestion 1   (recommended option — visually emphasized)
└─ COOPERATIVE suggestion 2   (alternate)
   …
```

Clicking a suggestion fires the carried `intent`. The user can also reply in
free text to challenge the recommendation; the agent re-evaluates and may
re-propose. No gate has a separate UI banner — the chat is the single surface.
This keeps the three gates visually consistent and avoids competing surfaces.

## Schema changes (clean baseline)

This is a pre-production redesign. No backward compatibility — existing case
data is regenerated. See
[feedback_no_backcompat_pre_data.md](../../../.claude/memory/feedback_no_backcompat_pre_data.md).

### `InvestigationPath` collapses to binary

```python
class InvestigationPath(str, Enum):
    MITIGATION_FIRST = "mitigation_first"
    ROOT_CAUSE = "root_cause"
```

`USER_CHOICE` is removed. The "ask the user" semantic is now carried by
`PathSelection.user_confirmed = False` (Gate 2 pending) or
`PathSelection.rca_after_mitigation_confirmed = False` (Gate 3 pending).

**Slice timing for this removal:** the enum value stays in the codebase
through slice 1 even though the router stops returning it. The actual
deletion is part of slice 2, where Gate 2 lands and ambiguity surfaces
through `PathSelection.user_confirmed = False` instead. Removing the value
in slice 1 — before its consumer exists — would leave a window where the
router silently defaults ambiguous cases to `ROOT_CAUSE` with no UX
surface to flag the ambiguity. See *Slice plan* below.

The orphan duplicate `determine_investigation_path` in
[`models.py:2703`](../../../faultmaven/modules/case/domain/models.py) is
removed in slice 2 alongside the enum value — only the live resolver in
[`investigation_router.py`](../../../faultmaven/modules/case/domain/services/investigation_router.py)
remains.

### `PathSelection` gains confirmation fields and the mitigation boundary marker

```python
class PathSelection(BaseModel):
    # existing
    path: InvestigationPath
    auto_selected: bool             # True if the matrix picked this; False if ambiguous and defaulted
    rationale: str                  # human-readable explanation, surfaced in Gate 2 prompt
    alternate_path: Optional[InvestigationPath] = None

    # new — Gate 2
    user_confirmed: bool = False
    user_confirmed_at_turn: Optional[int] = None

    # new — Gate 3 (meaningful only when path == MITIGATION_FIRST)
    rca_after_mitigation_confirmed: bool = False
    rca_after_mitigation_confirmed_at_turn: Optional[int] = None
    mitigation_completed_at_turn: Optional[int] = None
```

`mitigation_completed_at_turn` is set when `mitigation_verified` first becomes
`True`. Used by the context builder to weight/filter evidence on
post-mitigation RCA runs (evidence collected before this turn is the
RCA-relevant window). Lives inside `PathSelection` rather than directly on
`Case` because it's only meaningful on the mitigation-first path; co-locating
it with the other path state keeps related fields together and avoids
polluting the `Case` row schema for a path-specific concept.

### Storage — no DB migration required

`PathSelection` and `inquiry` are stored as `JsonBlob` columns on the `cases`
table ([persistence/models.py:633-657](../../../faultmaven/infrastructure/persistence/models.py)).
Adding fields to the Pydantic models that serialize into them does **not**
require an Alembic migration — the field set is owned by the Pydantic schema,
not the database schema. Slice 1 is therefore migration-free.

### `IntentType` gains two values

```python
class IntentType(str, Enum):
    # ... existing types ...
    PATH_SELECTION = "path_selection"
    POST_MITIGATION_CHOICE = "post_mitigation_choice"
```

### `QueryIntent` gains two fields

```python
class QueryIntent(BaseModel):
    # ... existing fields ...

    # for PATH_SELECTION
    investigation_path: Optional[InvestigationPath] = None

    # for POST_MITIGATION_CHOICE
    continue_to_rca: Optional[bool] = None
```

Gate 3's "close as mitigation sufficient" branch does **not** use
`POST_MITIGATION_CHOICE`. It uses the existing `STATUS_TRANSITION` intent with
`to_status = CLOSED` and `closure_reason = mitigation_sufficient`. Reusing the
existing transition machinery preserves the closure-summary generation path
already documented in
[closure_summary_redesign.md](../../../.claude/memory/closure_summary_redesign.md).

## Router behavior (revised)

The Urgency × Temporal matrix collapses to two outcomes. Ambiguous cases
default to `ROOT_CAUSE` (safer for non-emergency) and surface the ambiguity
honestly in the `rationale` text — the user sees the system isn't certain and
can override via Gate 2.

| Temporal | Urgency | `path` | `auto_selected` | Rationale |
| --- | --- | --- | --- | --- |
| ONGOING | CRITICAL/HIGH | MITIGATION_FIRST | True | "Ongoing high-urgency impact — recommend mitigating first, RCA after stabilization" |
| ONGOING | MEDIUM/LOW | ROOT_CAUSE | True | "Ongoing but lower-urgency — recommend root-cause analysis for a permanent fix" |
| HISTORICAL | any | ROOT_CAUSE | True | "Historical issue — recommend root-cause analysis since immediate impact has subsided" |
| missing/UNKNOWN | any | ROOT_CAUSE | False | "Unable to determine urgency from data — defaulting to root-cause analysis; switch to mitigation-first if you have active impact" |

`auto_selected=False` indicates the system fell back to a default rather than
matching a matrix row — useful for telemetry and for the Gate 2 rationale text.
It does **not** affect Gate 2 behavior; every Gate 2 still requires user
confirmation regardless of `auto_selected`.

## Engine flow per path

### Root-cause-only path

```
[INQUIRY]
  ├─ proposed_problem_statement populated by agent
  │
  ├─ Gate 1 — Cooperative: "Yes, that's the problem"
  │   click → problem_statement_confirmed = True
  │
  ├─ problem_verification populated → compute path_selection (auto: ROOT_CAUSE)
  │
  ├─ Gate 2 — Cooperative x2: "Root-cause analysis (recommended)" / "Mitigation-first"
  │   click → path_selection.path, path_selection.user_confirmed = True
  │   → transition INQUIRY → INVESTIGATING
  │
[INVESTIGATING, path=root_cause]
  └─ standard DIAGNOSIS milestones → TREATMENT → RESOLVED
     (Gate 3 never fires)
```

### Mitigation-first path

```
[INQUIRY]
  └─ (Gate 1, Gate 2 as above; user confirms mitigation-first)
  → transition INQUIRY → INVESTIGATING

[INVESTIGATING, path=mitigation_first]
  ├─ DIAGNOSIS — agent discovers / proposes mitigation
  │
  ├─ MITIGATION — user executes mitigation, reports back
  │   ├─ mitigation_accepted = True
  │   └─ mitigation_verified = True
  │       └─ path_selection.mitigation_completed_at_turn = current_turn
  │
  ├─ Gate 3 — Cooperative x2:
  │   "Continue with root-cause analysis (recommended)"
  │      → POST_MITIGATION_CHOICE intent, continue_to_rca = True
  │      → rca_after_mitigation_confirmed = True
  │      → DIAGNOSIS continues, agent refocuses on RCA
  │        using pre-mitigation evidence window
  │
  │   "Mitigation is sufficient, close case"
  │      → STATUS_TRANSITION intent, to_status=CLOSED,
  │        closure_reason=mitigation_sufficient
  │      → CLOSED (closure summary generated)
  │
  │   Prompt text includes: "If you skip RCA, no root-cause runbook
  │   will be generated — only a mitigation note."
  │
  └─ (if RCA continued) TREATMENT → RESOLVED
```

## The mitigation → RCA leg — failure modes and how Gate 3 handles each

This was identified as the highest-risk leg of the lifecycle: the existing
design has no explicit checkpoint when mitigation completes, so the case
either drifts (no clear next action) or auto-continues into RCA when the user
considered the matter closed.

| Failure mode | What goes wrong without Gate 3 | How Gate 3 handles it |
| --- | --- | --- |
| **User never returns post-mitigation** | Case sits in INVESTIGATING indefinitely with no clear next step | Explicit prompt surfaces the open decision; case state where `mitigation_verified=True && !rca_after_mitigation_confirmed && status==INVESTIGATING` is detectable and can drive header chips, stale-case sweeps, etc. |
| **System silently restarts RCA when user is done** | Agent keeps probing root cause; user is frustrated ("I told you it's fixed") | RCA continuation requires explicit `rca_after_mitigation_confirmed=True`. Agent doesn't progress until commitment. |
| **Mitigation masked symptoms; new evidence shows healthy state** | RCA struggles because telemetry no longer captures the issue | On `rca_after_mitigation_confirmed=True`, agent prompt is cued: *"Mitigation has stabilized the system at turn N. Focus on evidence collected before that turn for RCA. New evidence should be evaluated against that prior window."* Context builder filters/weights evidence by `collected_at_turn < path_selection.mitigation_completed_at_turn`. |
| **User wants partial — "mitigate now, RCA Tuesday"** | No fit in current model | Case can sit at Gate 3 indefinitely; user re-engages later and the gate is still open. No timeout-driven auto-progression. |
| **User closes prematurely** | No runbook produced from a case that had real RCA potential | Gate 3 prompt explicitly mentions the runbook implication so the trade-off is visible at the click moment. |
| **Path was wrong from start** | User clicked mitigation-first but actually wanted RCA | Path remains mutable. Agent or user can revise via chat (resets `user_confirmed=False`, re-enters Gate 2). This was already true today; the new gate doesn't make it worse. |
| **Mitigation_verified set incorrectly** | Gate 3 fires when mitigation isn't actually done | Upstream concern — Gate 3 trusts the stage-gate milestones. Existing safeguards apply (`mitigation_verified` requires user-submitted action results, per agent-stage-playbook). |

## Pre-mitigation evidence window

The "focus on pre-mitigation evidence" cue needs the engine to know which
evidence is RCA-relevant after mitigation. Evidence rows already carry
`collected_at_turn`, and `path_selection.mitigation_completed_at_turn` is
set when `mitigation_verified` first becomes `True`. The context builder
filters or up-weights evidence rows where
`collected_at_turn < path_selection.mitigation_completed_at_turn`
on post-mitigation runs.

No new column on the `evidence` table. One scalar inside the `path_selection`
JSON blob. One filter clause in `context_builder.py`.

## Why every Gate 2 always asks (no fast path)

The design's stance: Gate 2 surfaces both options on every case, regardless
of how unambiguous the matrix output is. No auto-confirm shortcut for
ONGOING × CRITICAL or any other "obviously mitigate" combination. The
recommendation may be visually emphasized, but the click is always required.

This intentionally rejects a friction-reduction shortcut. The rationale:

**The deterministic matrix only sees what telemetry submits.** It cannot see
out-of-band context that often inverts the right path:

- Mitigation has already been applied elsewhere (someone restarted the
  service, traffic rolled to a backup region, a deploy reverted) — telemetry
  still reads ONGOING because the new measurements haven't caught up, or the
  pre-rollback evidence is what the user uploaded. Matrix says
  MITIGATION_FIRST; user wants RCA on the now-stable problem.
- The user is doing post-incident retrospective work — the case is "ongoing"
  in the data because the data captures the incident window, but the
  incident itself is over. Matrix says MITIGATION_FIRST; user wants RCA.
- The user has decided mitigation is out of scope (e.g., capacity constraint
  that requires a separate change-management process) and wants to use
  FaultMaven only for root-cause analysis to inform that process. Matrix
  says MITIGATION_FIRST; user wants RCA.

In each case the data is correct but the recommendation drawn from the data
is wrong. The user has knowledge the matrix cannot capture. Auto-confirming
on "unambiguous" matrix output would silently commit to the wrong path in
every one of these scenarios.

This posture matches **INV-02** in the existing Invariant Enforcement Matrix:
*"INQUIRY → INVESTIGATING never auto-fires on CRITICAL/HIGH urgency alone —
confirmation is still required regardless of severity."* Gate 2 extends the
same principle from "should we investigate" to "how should we investigate."

The friction cost is one COOPERATIVE click per case. The downside of skipping
it is silent commitment to a wrong path that may not be recoverable without
user-noticed conversational steering, which is precisely the failure mode
this design exists to eliminate.

## Re-evaluation triggers — deterministic, not LLM-driven

Gates can become invalid after they've passed when underlying state changes.
The design specifies *deterministic recompute hooks* rather than relying on
the LLM to notice and re-propose.

### Gate 1 invalidation

Gate 1 is invalidated when the user explicitly rejects the confirmed
statement and the agent writes a revised `proposed_problem_statement`. This
case is already handled by the existing inquiry confirmation flow.

### Gate 2 invalidation

`path_selection.user_confirmed` is cleared (set to `False`) whenever
`problem_verification` mutates after Gate 2 first passed. The mutation hook
lives in `_apply_inquiry_updates` / the equivalent INVESTIGATING update path:

```python
def _on_problem_verification_change(case: Case, old: ProblemVerification, new: ProblemVerification) -> None:
    if old.temporal_state != new.temporal_state or old.urgency_level != new.urgency_level:
        case.path_selection = None  # forces recompute + Gate 2 re-fire next turn
```

The watcher is a deterministic comparison on the two fields that drive path
selection (`temporal_state`, `urgency_level`). Mutations to other fields on
`problem_verification` (e.g., `impact`, `severity` descriptive text) do not
invalidate Gate 2. The LLM does not decide whether to re-propose — the engine
detects the mutation and clears the gate.

Effect on flow:
- If the case is still in INQUIRY, Gate 2 simply re-fires next turn before
  the INQUIRY → INVESTIGATING transition can re-pass.
- If the case has already entered INVESTIGATING, the path re-confirms in
  place. The agent acknowledges the change in approach in its next reply.
  No regression to INQUIRY status.

### Gate 3 invalidation (unfire)

`rca_after_mitigation_confirmed` is cleared whenever `mitigation_verified`
flips back to `False`. The flip-back is rare (e.g., user retracts a
mitigation outcome) but possible per existing milestone semantics. Same
shape as Gate 2's watcher — deterministic, engine-side.

```python
def _on_mitigation_verified_change(case: Case, old: bool, new: bool) -> None:
    if old is True and new is False:
        if case.path_selection is not None:
            case.path_selection.rca_after_mitigation_confirmed = False
            case.path_selection.rca_after_mitigation_confirmed_at_turn = None
            case.path_selection.mitigation_completed_at_turn = None
```

Gate 3 re-fires once `mitigation_verified` flips back to `True`.

## Invariant Enforcement Matrix — new rows

Three new INV-* rows for the gates, slotted into the existing matrix in
`investigation-lifecycle-logic.md`. Each follows the existing format:
*Invariant | Source | Enforcement | Test*.

### INV-19 — Gate 2 precedes INQUIRY → INVESTIGATING

- **Invariant**: INQUIRY → INVESTIGATING requires `path_selection.user_confirmed = True`. The transition gate must verify *both* `inquiry.problem_statement_confirmed` (INV-01) and `path_selection.user_confirmed` before status flips.
- **Source**: this document, *Three gates* section
- **Enforcement**: **Code-guarded** — `_check_automatic_transitions` checks `case.path_selection is not None and case.path_selection.user_confirmed is True` as part of the transition condition. Same chokepoint as INV-01's `user_confirmed_investigation` check.
- **Test**: `test_inv19_gate2_required_for_investigating_transition` — case with `problem_statement_confirmed=True` but `path_selection.user_confirmed=False` does not auto-transition; case with both True transitions normally; case with `path_selection=None` (no recommendation yet) does not transition.

### INV-20 — Gate 2 invalidates when problem_verification mutates

- **Invariant**: Mutating `problem_verification.temporal_state` or `problem_verification.urgency_level` after Gate 2 has passed clears `path_selection.user_confirmed`. The mutation watcher cannot be bypassed; LLM emissions of new verification values trigger the recompute deterministically.
- **Source**: this document, *Re-evaluation triggers* section
- **Enforcement**: **Code-guarded** — mutation hook in the verification-update path compares old/new `(temporal_state, urgency_level)` tuples and clears `path_selection` on mismatch.
- **Test**: `test_inv20_problem_verification_change_clears_gate2` — case at Gate 2 confirmed; LLM emits a verification update with changed urgency; engine clears `path_selection`; next turn Gate 2 re-fires.

### INV-21 — Gate 3 precedes post-mitigation RCA progression (mitigation-first path only)

- **Invariant**: When `path_selection.path == MITIGATION_FIRST` and `mitigation_verified == True`, no new RCA-side milestones (`root_cause_identified`, `solution_proposed`) can be set unless `path_selection.rca_after_mitigation_confirmed == True` OR the case is transitioning to CLOSED with `closure_reason = mitigation_sufficient`. This prevents the engine from silently restarting RCA when the user hasn't explicitly committed.
- **Source**: this document, *Three gates* section + *Gate 3 failure modes*
- **Enforcement**: **Code-guarded** — milestone-application logic rejects RCA-side milestone updates on mitigation-first cases where Gate 3 hasn't passed. The closure exit (STATUS_TRANSITION) is not gated by Gate 3 — closing out is always allowed.
- **Test**: `test_inv21_gate3_required_for_post_mitigation_rca` — mitigation-first case with `mitigation_verified=True && !rca_after_mitigation_confirmed`; LLM emits `root_cause_identified=True`; engine rejects the milestone update and surfaces Gate 3 again; same case with `rca_after_mitigation_confirmed=True` accepts the milestone.

These three rows are added to the matrix in `investigation-lifecycle-logic.md`
as part of slice 2 (INV-19, INV-20) and slice 3 (INV-21).

## Success metrics

Three metrics validate whether the gates are pulling their weight. Each is
emitted as a Prometheus counter or gauge alongside the existing lifecycle
metrics ([`docs/operations/monitoring/lifecycle-metrics.md`](../../operations/monitoring/lifecycle-metrics.md)).

### `faultmaven_gate2_override_rate`

**Definition**: ratio of Gate 2 confirmations where the user picked
`alternate_path` over the system's recommendation, divided by total Gate 2
confirmations.

**Why it matters**: high override rates indicate the matrix is
mis-classifying cases — the user has out-of-band context the system can't
see. Sustained override rate above some threshold (suggest 30%) means the
matrix logic needs revisiting, or the rationale text needs to set
expectations differently.

**Telemetry**: counter pair `faultmaven_gate2_confirmed_total{outcome="recommended"|"override"}`.

### `faultmaven_gate3_stall_rate`

**Definition**: count of cases where `mitigation_verified=True` happened but
neither Gate 3 outcome (`rca_after_mitigation_confirmed=True` or transition
to CLOSED) fired within N days. Recommend N=7 initially.

**Why it matters**: this is the prime failure mode of the mitigation → RCA
leg — cases stranded at Gate 3 with no clear next action. A sustained stall
rate above some threshold (suggest 5%) means the Gate 3 prompt isn't
prompting enough, or a stale-case nudge mechanism is needed.

**Telemetry**: gauge `faultmaven_gate3_stalled_cases` plus counter
`faultmaven_gate3_resolved_total{outcome="rca_continued"|"closed_mitigation_sufficient"}`.

### `faultmaven_gate3_close_as_sufficient_rate`

**Definition**: ratio of Gate 3 outcomes where the user picked
"mitigation is sufficient" over total Gate 3 outcomes.

**Why it matters**: indicates how often the path-selection recommendation was
*ultimately correct* — mitigation-first was indeed enough, no RCA needed.
High rate suggests the matrix is recommending mitigation-first when RCA-only
would have served the user; low rate suggests mitigation is just a stepping
stone and the design assumption (mitigation → RCA as the default
post-mitigation continuation) is sound.

**Telemetry**: derived from the `faultmaven_gate3_resolved_total` counter
labels above.

## Open questions

Items genuinely unresolved at design time, to be settled before or during
implementation:

1. **Stale Gate 3 auto-nudge threshold.** The failure-mode table notes that
   Gate 3 stalls should drive header chips and stale-case sweeps. What
   duration triggers a nudge in chat — 24 hours, 3 days, 7 days? Should the
   nudge re-emit the Gate 3 suggestions, or be a lighter "remember to close
   this" reminder? Initial recommendation: 7 days, re-emit suggestions; tune
   from `faultmaven_gate3_stall_rate` telemetry.

2. **Re-opening a CLOSED(mitigation_sufficient) case for retroactive RCA.**
   The user closed via Gate 3, then later decides RCA is warranted (e.g.,
   issue recurred). Current terminal-state model is immutable (INV-09 holds:
   no new evidence, no transitions). Does this redesign want a "reopen for
   RCA" affordance, or is the user expected to file a new case linked to the
   prior one? Initial recommendation: file a new case, reference the prior
   via a `derived_from_case_id` linkage. Reopening terminal cases is a
   separate, larger change and is out of scope for these gates.

3. **Gate 2 surfacing of alternate-path rationale.** The Gate 2 prompt
   includes the recommended path's rationale (e.g., "ongoing high-urgency
   impact"). Should it also include rationale for the alternate path
   (e.g., "or root-cause if the impact is already contained")? Adding
   alternate rationale lengthens the prompt; omitting it leaves the
   alternate as an unjustified bare option. Initial recommendation: include
   a one-line alternate framing only when the matrix output is borderline
   (e.g., HISTORICAL × HIGH, where the user is most likely to override).

4. **Should `mitigation_completed_at_turn` filter or up-weight evidence?**
   The pre-mitigation evidence window section says "filters or up-weights."
   These are different behaviors: filter excludes post-mitigation evidence
   from RCA context; up-weight includes it but de-prioritizes. Filter is
   simpler; up-weight is more forgiving when the user wants to bring in
   confirming post-mitigation evidence (e.g., "the mitigation hasn't
   recurred — proves the trigger was X"). Initial recommendation: up-weight
   (5× boost for pre-mitigation, 1× for post-mitigation in retrieval
   scoring) so the context builder doesn't lose potentially-relevant
   evidence outright.

## Open with sliced delivery

This is a multi-PR design. Each slice is independently shippable and testable.

### Slice 1 — Schema + intent vocabulary (truly additive)

Purely additive. No enum value removed. No behavior change visible to users
yet. No Alembic migration (all storage lives inside existing JSON columns).

- Add `user_confirmed`, `rca_after_mitigation_confirmed`, `mitigation_completed_at_turn` (and `_at_turn` audit fields) to `PathSelection`
- Add `PATH_SELECTION` and `POST_MITIGATION_CHOICE` to `IntentType`
- Add `investigation_path` and `continue_to_rca` fields to `QueryIntent`
- Update `investigation_router.py` so ambiguous cases default to `ROOT_CAUSE` with `auto_selected=False` and an honest rationale. The router stops *returning* `USER_CHOICE`, but the enum value remains in the codebase (consumers that previously checked for it become dead branches but don't break)
- Unit tests for schema, intent parsing, router output (including the "ambiguous → defaults to ROOT_CAUSE" matrix entries)

**Why USER_CHOICE removal is deferred to slice 2:** removing the enum value
before Gate 2 exists would leave a window where the router silently defaults
ambiguous cases to `ROOT_CAUSE` with no UX surface to flag the ambiguity.
Keeping the value in slice 1 means consumers that branch on `USER_CHOICE`
still type-check, even if no live router returns it. Deletion happens in
slice 2 alongside Gate 2 — by then the "ambiguity needs surfacing" semantic
has moved to `path_selection.user_confirmed=False`.

### Slice 2 — Gates 1 and 2 wiring + USER_CHOICE cleanup

- Move `determine_investigation_path` call to fire when `problem_verification` is populated during inquiry (instead of at transition)
- Add Gate 2 prompt block to INQUIRY template — only when `problem_statement_confirmed && path_selection && !path_selection.user_confirmed`
- Emit two COOPERATIVE suggestions per Gate 2 with `PATH_SELECTION` intent
- Add intent handler: sets `path` and `user_confirmed=True`
- Update `_check_automatic_transitions` to require `path_selection.user_confirmed=True` — **enforces INV-19**
- Add the `problem_verification` mutation watcher that clears `path_selection.user_confirmed` on `temporal_state` or `urgency_level` changes — **enforces INV-20**
- **USER_CHOICE removal** (deferred from slice 1): remove the enum value, remove the orphan duplicate `determine_investigation_path` in `models.py:2703`, drop the descriptive `approach` string mapping in `_get_investigation_strategy_data`, drop the client-side `getApproachHint` regex
- Add **INV-19** and **INV-20** to the Invariant Enforcement Matrix in `investigation-lifecycle-logic.md`
- Tests: full Gate 1 + Gate 2 round-trip including override and revision; `test_inv19_*`, `test_inv20_*`

### Slice 3 — Gate 3 (mitigation → RCA loop)

- Detect `mitigation_verified=True` first becoming true; set `path_selection.mitigation_completed_at_turn = current_turn`
- Emit Gate 3 prompt and two suggestions (`POST_MITIGATION_CHOICE` for continue, `STATUS_TRANSITION` for close). Verified: the close branch reuses the existing closure-summary path — `mitigation_sufficient` is a known closure reason with explicit handling (substance-only gate, warning-tinted UI surface), per [closure_summary_redesign.md](../../../.claude/memory/closure_summary_redesign.md). No new summary logic.
- Add prompt cue to DIAGNOSIS template for post-mitigation runs: focus on pre-mitigation evidence window
- Context builder filters/up-weights evidence by `collected_at_turn` vs `path_selection.mitigation_completed_at_turn` when assembling RCA context (see open question #4 for filter-vs-upweight policy)
- Add the `mitigation_verified` flip-back watcher that clears Gate 3 state — **enforces INV-21**
- Add post-mitigation milestone-application guard rejecting RCA-side milestones until Gate 3 passes — **enforces INV-21**
- Add **INV-21** to the Invariant Enforcement Matrix
- Add the three success-metric counters (`faultmaven_gate2_confirmed_total`, `faultmaven_gate3_resolved_total`, `faultmaven_gate3_stalled_cases`)
- Tests: full mitigation_first end-to-end including both Gate 3 outcomes; `test_inv21_*`; metric emission tests

### Slice 4 — Frontend header

- Path chip in collapsed status row when `path_selection.user_confirmed`:
  "Mitigation-first" for the non-default path; no chip for `ROOT_CAUSE`
- Pending-gate chip when a gate is open:
  - "Awaiting path" when Gate 2 pending
  - "RCA or close?" when Gate 3 pending
- Progress dots:
  - 6 standard indicators on root-cause path
  - 8 dots with diamond outline ◇ for `mitigation_accepted`/`mitigation_verified`
    when on mitigation-first path (inserted after Changes, before Root Cause)
- Drop the `getApproachHint` regex
- Drop the Reports/Solution/Closure detail rows per the case-header hardening
  matrix (separate doc)
- Tooltip for pending dot showing `milestone_description`

## What this design explicitly removes

All four removals happen in **slice 2** (not slice 1 — see *Slice plan*
above for the slicing rationale). Slice 1 only stops the router from
returning `USER_CHOICE`; the enum value and its dead-code consumers
remain in place until Gate 2 lands and the ambiguity-surface semantic
moves to `path_selection.user_confirmed=False`.

| Removed (in slice 2) | Replacement | Reason |
| --- | --- | --- |
| `InvestigationPath.USER_CHOICE` enum value | `PathSelection.user_confirmed = False` (Gate 2 not yet passed) | Was set by router but had no consumer — effectively dead code |
| Orphan `determine_investigation_path` in `models.py:2703` | Single live resolver in `investigation_router.py` | Two parallel matrices with subtly different rules; only one was imported |
| Client-side `getApproachHint` regex in `CaseDetails.tsx` | Structured `path_selection.path` on `CaseUIResponse_Investigating` | Server already knows the enum; client shouldn't recover it via regex over descriptive text |
| Descriptive `approach` string in `InvestigationStrategyData` | Direct `path_selection` exposure on `CaseUIResponse_Investigating` | Free-text was the regex bait — drop the bait once the regex is gone |

## What this design explicitly preserves

- The Urgency × Temporal routing matrix as the source of the *recommendation*
  (no LLM-driven path inference)
- The stage-gate milestone set (`mitigation_accepted`, `mitigation_verified`,
  `solution_accepted`, `solution_verified`)
- The closure-reason taxonomy including `mitigation_sufficient`
- The closure-summary inline-in-chat policy
  ([closure_summary_redesign.md](../../../.claude/memory/closure_summary_redesign.md))
- Path mutability during INVESTIGATING (already supported)
- The `ResolutionActionsCard` post-terminal banner as the surface for
  resolved/closed cases

## References

- [investigation-lifecycle-logic.md](investigation-lifecycle-logic.md) —
  predecessor doc; sections on path routing are superseded by this document
- [intent-resolution.md](intent-resolution.md) —
  bounded-choice intent matching, which Gate suggestions use
- [agent-stage-playbook.md](agent-stage-playbook.md) —
  stage-gate milestone semantics (mitigation_accepted, mitigation_verified, etc.)
- [progress-transparency.md](progress-transparency.md) —
  unrelated, but the header-design slice 4 interacts with the progress visualization
