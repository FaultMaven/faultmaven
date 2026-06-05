# Investigation Flow Redesign — Unified Opportunistic Flow with Stabilization-as-Insert

**Status: DRAFT / PROPOSAL** (2026-06-05) — not yet ratified. Supersedes the
`mitigation_first` vs `root_cause` path fork and the path-conditional RCA
emission ban (INV-17, INV-21, `_SYMPTOM_VALIDATION_BLOCK`). Pairs with
[investigation-lifecycle-logic.md](./investigation-lifecycle-logic.md) (whose
§1.2 path model this replaces) and [investigation-data-models.md](./investigation-data-models.md).

---

## 1. Why

### 1.1 The motivating failure

`case_961722db0284` (Scenario 1, k8s-pvc-pending) ended **UNRESOLVED at 15
turns** even though FaultMaven identified the root cause at turn 5 with
`root_cause_likelihood=1.0`. Mechanically:

1. Persona picked **mitigation-first** at the Gate-2 fork → case entered
   `pre_mitigation_mitigation_first`, which renders `_SYMPTOM_VALIDATION_BLOCK`
   and **forbids all RCA-side emissions**.
2. The cause was self-evident from the error
   (`storageclass.storage.k8s.io "fast-ssd" not found`), so the LLM reflexively
   kept setting `root_cause_identified` — **forbidden on this path**.
3. That made `validate_reasoning_first` fail, and the graceful-degradation
   branch ([`milestone_engine.py:5316`](../../../faultmaven/core/investigation/milestone_engine.py))
   **wipes _all_ milestones to None** — destroying the *valid*
   `mitigation_accepted` / `mitigation_verified` that the LLM emitted alongside
   the forbidden one (the surgical path-guard that would have removed only
   `root_cause_identified` runs later, too late).
4. `mitigation_completed_at_turn` stayed `null` forever → permanent
   `pre_mitigation_mitigation_first` → RCA forbidden, gate unreachable.

The collateral wipe is a real bug, but fixing only the wipe (surgical
invalidation) just lets the engine **suppress a true signal more cleanly**. The
deeper issue is the design assumption underneath the whole fork.

### 1.2 The broken assumption

The path fork and the RCA ban both assume: **at symptom-verification time the
cause is still unknown** — diagnosis is a future activity to defer
(mitigation-first) or start now (root-cause).

This is false for a large, definable class of incidents: **self-naming errors**
(log states the cause — missing resource, wrong config value, `OOMKilled`,
`permission denied`), **config typos / recent-change breaks** (the diff *is* the
cause), and **single-step resource faults** (disk full, quota exceeded). For
these, *symptom verification and root-cause identification are the same
observation.* Forbidding the LLM from recording a cause it legitimately knows is
fighting reality, and it is what trapped the case above.

### 1.3 The conflation to remove

`root_cause_identified` is currently treated as something **earned by performing
RCA**. But it actually encodes **a state of knowledge** — "the cause is known" —
which can become true at any turn, sometimes turn 1. It is distinct from "a
root-cause investigation was performed." The engine must record the *fact* of
cause-knowledge whenever the LLM has it (evidence-grounded), independent of how
that knowledge arrived.

---

## 2. Core model: two orthogonal axes

The single fork conflated two independent questions. Separate them:

- **Axis A — Certainty.** Do we know the cause? the solution? Drives whether
  diagnostic *labor* (hypothesis formulation, causal evidence-needs) is needed.
- **Axis B — Stabilization gap.** Is something hurting *now* that we cannot
  fully resolve *this session*? Drives whether a **stabilization** is inserted.

The two axes are independent. The old fork forced an Axis-B answer
("mitigate first") that wrongly *implied* an Axis-A answer ("cause unknown, RCA
deferred"). Decoupling them yields the rest of this design.

**Rule (diagnostic machinery):** run hypothesis formulation + evidence-needs
**iff the cause is uncertain** (`cause_state ∈ {UNKNOWN, CANDIDATES}`) — *not*
because a stabilization was or wasn't inserted. When the cause is `IDENTIFIED`,
skip straight to solution work. This single rule replaces the entire
path-conditional RCA ban.

---

## 3. Unified flow: one investigation, stabilization as a re-evaluable insert

There is **one opportunistic INVESTIGATING flow**. There is **no fork and no
merge**. A **stabilization** (formerly "mitigation") is an *optional inserted
sub-activity* that buys time when an Axis-B gap exists. A case is described
retrospectively as:

- **direct** — resolved with no stabilization, or
- **stabilized** — a stabilization was inserted.

These are *descriptions of what happened*, not paths chosen upfront.

```
INQUIRY ──confirm problem──▶ INVESTIGATING ───────────────────────────▶ RESOLVED / CLOSED
                                  │
                                  │  opportunistically record what we learn:
                                  │   symptom_verified, cause_state, solution_state, feasibility
                                  │
                                  ├─(Axis-B gap detected, any turn)─▶ [STABILIZATION insert]
                                  │        propose → accept → verify → return to flow
                                  │
                                  └─(CLOSE available at ANY point: abandon, or data/impl. limit)
```

### 3.1 Stabilization triggers and their forwarding paths

A stabilization is proposed when an Axis-B gap exists. The three triggering
circumstances each leave a *different* thing unresolved — which determines the
forwarding path **after** the stabilization verifies. This is the answer to
"what happens after mitigation?" — it is **not** uniformly "continue to RCA"
(today's Gate-3 assumption is only row 1):

| Trigger for inserting a stabilization | cause_state | solution_state | Forwarding path after stabilization |
|---|---|---|---|
| **(1)** cause unknown / multiple candidates needing different fixes | UNKNOWN / CANDIDATES | UNKNOWN | **RCA** — hypothesis formulation + evidence-needs |
| **(2)** cause known, solution unclear / multiple complex options | IDENTIFIED | CANDIDATES | **Solution deliberation** (see §6 — under-built today) |
| **(3)** cause + solution known, implementation takes time | IDENTIFIED | SELECTED, deferred | **Handoff / schedule** — disposition, no diagnostic work |

If **no** Axis-B gap exists (cause known, solution known, implementable now),
the flow is **direct**: verify → propose solution → accept → verify → RESOLVED.
No stabilization, no hypothesis machinery. (This is the case that the old model
trapped.)

### 3.2 When does the engine decide to insert a stabilization? (timing)

Because data arrives turn-by-turn, this is an agent judgment, not a one-shot
fork. The **first and most common assessment point is immediately after
`symptom_verified`** (the same point the old Gate 2 fired) — the agent asks "is
there an impact-now gap that can't close this session?" and, if so, *proposes* a
stabilization (the user accepts → insert; declines → continue). The assessment
is **re-evaluable**: a stabilization can also be proposed later (RCA stalls,
situation deteriorates). It is never an irreversible commitment.

### 3.2.1 Single insert, but never a dead-end (§8 Q3)

For now the engine models **one** stabilization per investigation (forward-only,
simplest). But "single" must not become a new trap: if the first stabilization
**doesn't stabilize** the situation, the agent must not run into a dead-end. The
flow stays open to **user-led action** — the user can apply a different fix,
escalate, provide new data, or CLOSE. Concretely: a stabilization that fails to
stabilize leaves `stabilization.verified=false`; the agent acknowledges it didn't
work, may propose an alternative _in prose / as a fresh proposed action_, and the
case continues opportunistically (or closes). The "single record" constraint is a
data-model simplification, **not** a cap on how many remediation attempts the
conversation can explore. Multiple structured stabilization records are a
possible future extension (§8) but are not required to avoid the dead-end.

### 3.3 Close-anytime

Per the no-fork model, **CLOSE is always available**: the user may abandon, or
progress may be blocked by data limits (can't obtain the evidence) or
implementation limits (fix can't be applied here). This is the existing
INVESTIGATING → CLOSED disposition handshake, now reachable from any point in the
flow rather than gated behind a path.

---

## 4. State-variable model

The redesign splits state by its true nature: **assessment** (truths the
engine records and *believes*) vs **action-compliance gates** (did the user do a
proposed action). The path enum disappears.

### 4.1 Assessment — recordable any turn, path-independent, never stripped

These encode *what we know*. The engine accepts them whenever they are
evidence-grounded; they are never rejected because of "which path/stage" the
case is in.

| Field | Type | Meaning | Replaces |
|---|---|---|---|
| `symptom_verified` | bool | Symptom confirmed against case evidence | (unchanged) |
| `cause_state` | enum `UNKNOWN \| CANDIDATES \| IDENTIFIED` | Knowledge of the root cause. `CANDIDATES` = multiple plausible causes implying different fixes (circumstance 1) | the overloaded boolean `root_cause_identified` |
| `root_cause_likelihood` | float | Confidence scalar for the leading cause | (retained) |
| `solution_state` | enum `UNKNOWN \| CANDIDATES \| SELECTED` | Knowledge of the fix. `CANDIDATES` = multiple/complex options needing deliberation (circumstance 2) | (new — was implicit in `solution_proposed`) |
| `solution_feasible` | enum `NOW \| DEFERRED` | Can the SELECTED solution be applied this session? `DEFERRED` drives circumstance 3 / handoff disposition | (new) |

`root_cause_identified` is **cut cleanly** (per [feedback_no_backcompat_pre_data]
— pre-production, no shim): read sites migrate to `cause_state == IDENTIFIED`.
The canonical signal is the enum. **Critically: `cause_state` is never
path-stripped** — this is the linchpin that dissolves the trap.

`cause_state = CANDIDATES` is **derived from `hypothesis_manager`** (≥2 ACTIVE
hypotheses), not a second stored field — we are removing a conflation, not adding
a source of truth (decided, §8 Q4). **Coupling caveat (from QA):** across real
runs the LLM routinely identifies the cause _in prose_ with **0 formal hypothesis
records**, so a purely-derived `CANDIDATES` will silently under-fire unless the
prompt reliably forces hypothesis emission when the cause is uncertain. **The
derivation and the prompt change ship together** — a derived signal over an
unreliable producer is worse than the boolean it replaces.

### 4.2 Action-compliance gates — track user compliance, gate nothing about RCA

These track whether the user accepted/verified a *proposed action*. They drive
the UI stage label and the resolution handshake, but **do not gate diagnostic
work** (that is gated by `cause_state` per §2).

| Field | Type | Meaning |
|---|---|---|
| `stabilization` | optional record `{proposed_at_turn, accepted, verified, completed_at_turn}` | A stabilization insert. Its *existence* marks the case "stabilized". `completed_at_turn` (set when `verified`) is the boundary for up-weighting pre-stabilization evidence in later RCA. Replaces `mitigation_accepted` / `mitigation_verified` / `path_selection.mitigation_completed_at_turn`. |
| `solution_accepted` | bool | User accepted the permanent solution → "Resolving" |
| `solution_verified` | bool | User confirmed the permanent solution worked → RESOLVED |

`solution_proposed` becomes derived: `solution_state == SELECTED` AND a Solution
record exists.

### 4.3 Removed / re-derived

- **`InvestigationPath` (MITIGATION_FIRST / ROOT_CAUSE)** — removed as a
  prospective fork. A retrospective descriptor `investigation_shape: DIRECT |
  STABILIZED` is derived from `stabilization is not None`.
- **`PathSelection` row + Gate 2 commit** — removed. The post-`symptom_verified`
  assessment (§3.2) no longer materializes a path; it may surface a
  stabilization _proposal_.
- **`_SYMPTOM_VALIDATION_BLOCK`, `_GATE3_PENDING_BLOCK`, `_POST_MITIGATION_RCA_PREFIX`,
  `pre_mitigation_mitigation_first` state, the path-conditional emission
  backstop** — all removed. The diagnostic-machinery gate (§2 rule) and the
  forwarding table (§3.1) replace them.
- **`current_stage` (DIAGNOSIS/MITIGATION/TREATMENT)** — re-derived as a pure UI
  view:
  - `stabilization.accepted && !stabilization.verified` → "Stabilizing"
  - `solution_accepted && !solution_verified` → "Resolving"
  - else → "Investigating" (sub-phase from `symptom_verified` / `cause_state`)

### 4.4 Stage diagram (derived, not driving)

```
                 cause_state          solution_state         gates
Investigating ── UNKNOWN/CANDIDATES ──────────────────────  (diagnostic machinery ON)
      │              │ IDENTIFIED
      │              ▼
      │          UNKNOWN/CANDIDATES ── solution deliberation (§6)
      │              │ SELECTED
      │              ▼
      │          feasible NOW ──── propose solution ── solution_accepted ─▶ "Resolving"
      │              │ DEFERRED                                  │ solution_verified
      │              ▼                                           ▼
      │          handoff disposition                         RESOLVED
      │
   [stabilization insert] ── accepted ─▶ "Stabilizing" ── verified ─▶ return to flow
                                                                (forwarding per §3.1)
```

---

## 5. What the engine believes vs guards

Old model: the engine **distrusted** LLM cause-knowledge structurally
(path-stripped it). New model: the engine **believes** evidence-grounded
knowledge and guards only the things that actually need guarding:

- **Believe:** `cause_state`, `solution_state`, likelihoods — recorded whenever
  the LLM emits them with evidence backing. The `validate_reasoning_first`
  justification requirement stays (a milestone needs a cited basis), but it is
  **per-milestone surgical** — a failure on one milestone never wipes the others
  (this also fixes §1.1 directly). **The justification bar must accept a
  self-naming error** (from QA): when the cited evidence row's extract literally
  states the cause (`storageclass ... "fast-ssd" not found`), that _is_ a valid
  basis for `cause_state=IDENTIFIED` — otherwise self-naming causes get stripped
  surgically instead of wholesale, which is tidier but still wrong.
- **Guard (structural, unchanged):** disposition transitions stay a 2-turn
  user handshake (`propose_transition` + `confirm_pending_transition`, INV-03).
  Gates still require a pending `ProposedAction` (no hallucinated compliance).
- **Guard (ordering):** `solution_verified` requires `solution_accepted`;
  `stabilization.verified` requires `.accepted`. These survive — they are real
  state-machine orderings, not path bans.

---

## 6. The genuinely under-built surface: solution-space deliberation

Rows (1) and (3) of §3.1 mostly reuse existing pieces — (1) is today's RCA loop,
(3) is a disposition question. **Row (2) — cause known, multiple/complex
candidate solutions — has no real machinery today.** The current SOLUTION stage
assumes a *single obvious fix* to propose-and-verify; it has no notion of
deliberating across a solution space (trade-offs, workaround-vs-permanent,
design choices). `solution_state = CANDIDATES` is the new hook for this.

**Decided (§8 Q1): reuse the hypothesis/evidence-needs machinery** for solution
deliberation — candidate solutions are enumerated, compared, and selected through
the same propose/evidence/converge loop that drives causal hypotheses, rather
than a parallel structure. A dedicated structure is introduced only if a clear
advantage emerges. Rationale: the deliberation shape (multiple competing
options, narrowed by evidence/criteria until one is selected) is structurally the
same as hypothesis convergence; a second mechanism would re-introduce the kind of
duplication this redesign removes. The detailed deliberation-loop design (what
"evidence" means for a solution choice — trade-off criteria, blast radius,
reversibility — and how `solution_state` advances UNKNOWN→CANDIDATES→SELECTED)
is the **highest-value follow-on work**.

**Decided (§8 Q2): deferred-implementation disposition is
CLOSE-with-documented-solution** — consistent with the 2-mode terminal lifecycle
([Terminal State & Tool Routing]) and the no-backcompat-collapse principle. No
third terminal state unless analytics genuinely need to separate
"resolved-pending-impl" from "abandoned." The documented solution is preserved on
the closed case (root-cause analysis + selected fix), so the knowledge is not
lost.

---

## 7. Migration / blast radius (high level)

Pre-production, no back-compat required ([feedback_no_backcompat_pre_data]):
collapse to the clean model rather than stacking shims.

- **Schema:** add `cause_state`, `solution_state`, `solution_feasible`,
  `stabilization`; drop `path_selection`, `InvestigationPath`,
  `mitigation_*`/`pre_mitigation` machinery. **Cut `root_cause_identified`
  cleanly** — no compat shim (per [feedback_no_backcompat_pre_data]); update read
  sites to `cause_state == IDENTIFIED` in the same change.
- **Engine (`milestone_engine.py`):** make the `validate_reasoning_first` strip
  per-milestone surgical; delete the path-conditional emission backstop; gate
  the diagnostic machinery on `cause_state`; replace Gate-2/Gate-3 handlers with
  the stabilization assessment + forwarding table. (Parse-time validation
  hardening is **out of scope** — see §9.)
- **Prompts (`templates.py`):** delete the four path blocks; one
  `INVESTIGATION` block whose stage guidance is selected by `cause_state` /
  `solution_state` / `stabilization`, not by path.
- **Recommender (`investigation_router.py`):** the urgency-only path recommender
  is removed; the stabilization assessment is cause/impact-aware by
  construction.
- **Invariants:** retire INV-17, INV-21; revise INV-04/05/06 references to the
  fork; INV-03 (disposition handshake) and the close-anytime rule are unchanged.
- **Docs:** update `investigation-lifecycle-logic.md` §1.2, the stage docstrings
  in `models.py`, `agent-stage-playbook.md`.

---

## 8. Decisions (resolved 2026-06-05)

1. **Solution deliberation loop (§6).** ✅ **Reuse hypothesis/evidence-needs
   machinery**; introduce a dedicated structure only if a clear advantage emerges.
2. **Disposition for deferred-implementation (circumstance 3).** ✅
   **CLOSE-with-documented-solution** — no third terminal state unless analytics
   require separating "resolved-pending-impl" from "abandoned."
3. **Multiple stabilizations.** ✅ **Single record for now**, but the flow must
   stay open to user-led action so a non-stabilizing insert is never a dead-end
   (§3.2.1). Multiple structured records are a possible future extension.
4. **`cause_state = CANDIDATES` derivation.** ✅ **Derived from
   `hypothesis_manager`** (≥2 ACTIVE hypotheses), not a stored field. **Coupled to
   a prompt change** that forces hypothesis emission under uncertainty (§4.1) —
   they ship together.
5. **Resolution gate interaction.** ✅ The existing implementation has a
   reconciliation path; `solution_verified` ↔ the absence-evidence end-state
   ([project-resolution-gate-stuck-loop] step 2/3) is **revisited after** this
   redesign lands, not folded in.

---

## 9. Out of scope: parse-time structured-output robustness (track separately)

This redesign addresses **post-parse** failures — milestone application, the
collateral strip, and path-conditional bans (the S1 trap). It does **not** touch
a distinct, earlier failure layer surfaced by the QA campaign (**S4**):

> The LLM emits a structured sub-record that violates a cross-field Pydantic
> validator — e.g. `evidence_need_updates{state: FULFILLED}` with no
> `fulfilling_evidence_id`, or (last session) `evidence{source_type: TEXT}` with
> no `source_file_id`. The **entire `InvestigationResponse_*` fails to parse** →
> unhandled `ValidationError` → **500 + traceback**.

These fail **before any milestone logic runs**, so "per-milestone surgical strip"
(§5) never gets a chance. The cross-field invariants themselves are **correct and
should stay** (they gate on real facts). What's missing is a **systemic parse-time
recovery policy** for LLM structured output: on `ValidationError`, either (a)
retry once with the validation error fed back to the LLM, or (b) drop/quarantine
the offending sub-record and continue the turn — so one bad sub-field never 500s
the whole turn. This is the same class as the prior-session `source_file_id` 500
and is systemic across **any** schema with cross-field validators; removing the
path fork does not touch it.

**Action: tracked as a separate robustness item**, not folded into this redesign.
See [project-llm-structured-output-strategy] (provider-native constrained
generation is the upstream mitigation; graceful parse-time recovery is the
backstop).
