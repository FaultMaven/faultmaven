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
|---|---|---|---|
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

The orphan duplicate `determine_investigation_path` in
[`models.py:2703`](../../../faultmaven/modules/case/domain/models.py) is also
removed — only the live resolver in
[`investigation_router.py`](../../../faultmaven/modules/case/domain/services/investigation_router.py)
remains.

### `PathSelection` gains confirmation fields

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
```

### Case-level marker for the mitigation boundary

```python
class Case(BaseModel):
    # ... existing fields ...
    mitigation_completed_at_turn: Optional[int] = None
```

Set when `mitigation_verified` first becomes `True`. Used by the context
builder to weight/filter evidence on post-mitigation RCA runs (the evidence
collected before this turn is the RCA-relevant window).

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
|---|---|---|---|---|
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
  │       └─ case.mitigation_completed_at_turn = current_turn
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
|---|---|---|
| **User never returns post-mitigation** | Case sits in INVESTIGATING indefinitely with no clear next step | Explicit prompt surfaces the open decision; case state where `mitigation_verified=True && !rca_after_mitigation_confirmed && status==INVESTIGATING` is detectable and can drive header chips, stale-case sweeps, etc. |
| **System silently restarts RCA when user is done** | Agent keeps probing root cause; user is frustrated ("I told you it's fixed") | RCA continuation requires explicit `rca_after_mitigation_confirmed=True`. Agent doesn't progress until commitment. |
| **Mitigation masked symptoms; new evidence shows healthy state** | RCA struggles because telemetry no longer captures the issue | On `rca_after_mitigation_confirmed=True`, agent prompt is cued: *"Mitigation has stabilized the system at turn N. Focus on evidence collected before that turn for RCA. New evidence should be evaluated against that prior window."* Context builder filters/weights evidence by `collected_at_turn < case.mitigation_completed_at_turn`. |
| **User wants partial — "mitigate now, RCA Tuesday"** | No fit in current model | Case can sit at Gate 3 indefinitely; user re-engages later and the gate is still open. No timeout-driven auto-progression. |
| **User closes prematurely** | No runbook produced from a case that had real RCA potential | Gate 3 prompt explicitly mentions the runbook implication so the trade-off is visible at the click moment. |
| **Path was wrong from start** | User clicked mitigation-first but actually wanted RCA | Path remains mutable. Agent or user can revise via chat (resets `user_confirmed=False`, re-enters Gate 2). This was already true today; the new gate doesn't make it worse. |
| **Mitigation_verified set incorrectly** | Gate 3 fires when mitigation isn't actually done | Upstream concern — Gate 3 trusts the stage-gate milestones. Existing safeguards apply (`mitigation_verified` requires user-submitted action results, per agent-stage-playbook). |

## Pre-mitigation evidence window

The "focus on pre-mitigation evidence" cue needs the engine to know which
evidence is RCA-relevant after mitigation. Evidence rows already carry
`collected_at_turn`, and `Case.mitigation_completed_at_turn` is set when
`mitigation_verified` first becomes `True`. The context builder filters
or up-weights evidence rows where `collected_at_turn < mitigation_completed_at_turn`
on post-mitigation runs.

No new column on the `evidence` table. One scalar on the case. One filter
clause in `context_builder.py`.

## Re-evaluation on problem statement revision

If the user revises the confirmed problem statement after Gate 2 has passed
(via chat — "actually, this is historical, not ongoing"), the path
recommendation may be invalidated:

- `problem_verification.{temporal_state, urgency_level}` re-evaluated
- `path_selection` cleared, set `user_confirmed = False`
- Gate 2 re-fires next turn with the updated recommendation
- If the case had already entered INVESTIGATING, no automatic regression to
  INQUIRY — the path simply re-confirms in place, and the agent acknowledges
  the change in approach

This is consistent with the principle that gates can be revisited as long as
the underlying state changes warrant it.

## Open with sliced delivery

This is a multi-PR design. Each slice is independently shippable and testable.

### Slice 1 — Schema + intent vocabulary

Purely additive / structural. No behavior change visible to users yet.

- Add `user_confirmed`, `rca_after_mitigation_confirmed` (and `_at_turn` audit
  fields) to `PathSelection`
- Add `mitigation_completed_at_turn: Optional[int]` to `Case`
- Remove `USER_CHOICE` from `InvestigationPath`
- Remove the orphan `determine_investigation_path` in `models.py:2703`
- Add `PATH_SELECTION` and `POST_MITIGATION_CHOICE` to `IntentType`
- Add `investigation_path` and `continue_to_rca` fields to `QueryIntent`
- Update `investigation_router.py` to only return `MITIGATION_FIRST` or
  `ROOT_CAUSE`. Ambiguous cases default to `ROOT_CAUSE` with `auto_selected=False`
  and an honest rationale.
- Unit tests for schema, intent parsing, router output

### Slice 2 — Gates 1 and 2 wiring

- Move `determine_investigation_path` call to fire when `problem_verification`
  is populated during inquiry (instead of at transition)
- Add Gate 2 prompt block to INQUIRY template — only when
  `problem_statement_confirmed && path_selection && !path_selection.user_confirmed`
- Emit two COOPERATIVE suggestions per Gate 2 with `PATH_SELECTION` intent
- Add intent handler: sets `path` and `user_confirmed=True`
- Update `_check_automatic_transitions` to require
  `path_selection.user_confirmed=True`
- Clear `path_selection` if the confirmed problem statement is later revised
- Tests: full Gate 1 + Gate 2 round-trip including override and revision

### Slice 3 — Gate 3 (mitigation → RCA loop)

- Detect `mitigation_verified=True` first becoming true; set
  `case.mitigation_completed_at_turn = current_turn`
- Emit Gate 3 prompt and two suggestions (POST_MITIGATION_CHOICE for continue,
  STATUS_TRANSITION for close)
- Add prompt cue to DIAGNOSIS template for post-mitigation runs:
  focus on pre-mitigation evidence window
- Context builder filters/weights evidence by `collected_at_turn`
  vs `mitigation_completed_at_turn` when assembling RCA context
- Tests: full mitigation_first end-to-end including both Gate 3 outcomes

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

| Removed | Replacement | Reason |
|---|---|---|
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
