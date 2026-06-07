# Investigation Flow Redesign — Unified Opportunistic Flow with Mitigation-as-Insert

**Status: SHIPPED / AS-BUILT** (branch `refactor/investigation-flow-redesign`,
2026-06-05). This is the design rationale for the unified opportunistic flow that
replaced the `mitigation_first` vs `root_cause` path fork and the path-conditional
RCA emission ban (retired INV-17, INV-21; removed `_SYMPTOM_VALIDATION_BLOCK` and
the three path blocks). The locked implementation refinements **R1–R6** are folded
into the body below, with **as-built deviation** call-outs where the shipped code
differs from the original proposal. Pairs with
[investigation-lifecycle-logic.md](./investigation-lifecycle-logic.md) (whose §2 is
now the unified-flow spec) and [investigation-data-models.md](./investigation-data-models.md).

**As-built reconciliation (read first):**

- **No engine GATE on hypothesis emission.** The proposal framed the diagnostic-machinery rule (§2) as something the engine could enforce. As built, the engine **removed the path-conditional emission ban entirely** — the `cause_state` rule is **prompt-guided**, not a hard engine reject. `cause_state` is still recomputed and never path-stripped (the truth-signal linchpin holds), but "run hypothesis work iff cause uncertain" is guidance, not a backstop. This is the deliberate R6 tier shift.
- **Emission names are `mitigation_accepted` / `mitigation_verified` (R2).** The LLM emits `mitigation_*` on `MilestoneUpdates`, the `EvidenceCategory` value is `mitigation_evidence`, and the stage/action enums are `MITIGATION`. The concept uses the standard incident-response term "mitigation" throughout (consistent with runbooks). (The runbook "Mitigation" authoring section — a separate KB concept — is unrelated.)
- **`MitigationRecord` lives on `progress`.** It is `InvestigationProgress.mitigation`, persisted inside the `progress` JSON (no new DB column). Migration 016 only **drops** `cases.path_selection`.
- **Prompt dispatcher kept its old name.** `_select_diagnosis_block(case)` survives as a thin wrapper returning `focus_emphasis + _RCA_DIAGNOSIS_BLOCK`; it is no longer a path selector. `investigation_router.py` was deleted.
- **`SolutionState.CANDIDATES` is reserved, not produced.** Only `UNKNOWN | SELECTED` ship this round (R3).

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
- **Axis B — Mitigation gap.** Is something hurting *now* that we cannot
  fully resolve *this session*? Drives whether a **mitigation** is inserted.

The two axes are independent. The old fork forced an Axis-B answer
("mitigate first") that wrongly *implied* an Axis-A answer ("cause unknown, RCA
deferred"). Decoupling them yields the rest of this design.

**Rule (diagnostic machinery):** run hypothesis formulation + evidence-needs
**iff the cause is uncertain** (`cause_state ∈ {UNKNOWN, CANDIDATES}`) — *not*
because a mitigation was or wasn't inserted. When the cause is `IDENTIFIED`,
skip straight to solution work. This single rule replaces the entire
path-conditional RCA ban.

---

## 3. Unified flow: one investigation, mitigation as a re-evaluable insert

There is **one opportunistic INVESTIGATING flow**. There is **no fork and no
merge**. A **mitigation** is an *optional inserted
sub-activity* that buys time when an Axis-B gap exists. A case is described
retrospectively as:

- **direct** — resolved with no mitigation, or
- **stabilized** — a mitigation was inserted.

These are *descriptions of what happened*, not paths chosen upfront.

```
INQUIRY ──confirm problem──▶ INVESTIGATING ───────────────────────────▶ RESOLVED / CLOSED
                                  │
                                  │  opportunistically record what we learn:
                                  │   symptom_verified, cause_state, solution_state, feasibility
                                  │
                                  ├─(Axis-B gap detected, any turn)─▶ [MITIGATION insert]
                                  │        propose → accept → verify → return to flow
                                  │
                                  └─(CLOSE available at ANY point: abandon, or data/impl. limit)
```

### 3.1 Mitigation triggers and their forwarding paths

A mitigation is proposed when an Axis-B gap exists. The three triggering
circumstances each leave a *different* thing unresolved — which determines the
forwarding path **after** the mitigation verifies. This is the answer to
"what happens after mitigation?" — it is **not** uniformly "continue to RCA"
(today's Gate-3 assumption is only row 1):

| Trigger for inserting a mitigation | cause_state | solution_state | Forwarding path after mitigation |
|---|---|---|---|
| **(1)** cause unknown / multiple candidates needing different fixes | UNKNOWN / CANDIDATES | UNKNOWN | **RCA** — hypothesis formulation + evidence-needs |
| **(2)** cause known, solution unclear / multiple complex options | IDENTIFIED | CANDIDATES | **Solution deliberation** (see §6 — under-built today) |
| **(3)** cause + solution known, implementation takes time | IDENTIFIED | SELECTED, deferred | **Handoff / schedule** — disposition, no diagnostic work |

If **no** Axis-B gap exists (cause known, solution known, implementable now),
the flow is **direct**: verify → propose solution → accept → verify → RESOLVED.
No mitigation, no hypothesis machinery. (This is the case that the old model
trapped.)

### 3.2 When does the engine decide to insert a mitigation? (timing)

Because data arrives turn-by-turn, this is an agent judgment, not a one-shot
fork. The **first and most common assessment point is immediately after
`symptom_verified`** (the same point the old Gate 2 fired) — the agent asks "is
there an impact-now gap that can't close this session?" and, if so, *proposes* a
mitigation (the user accepts → insert; declines → continue). The assessment
is **re-evaluable**: a mitigation can also be proposed later (RCA stalls,
situation deteriorates). It is never an irreversible commitment.

### 3.2.1 Single insert, but never a dead-end (§8 Q3)

For now the engine models **one** mitigation per investigation (forward-only,
simplest). But "single" must not become a new trap: if the first mitigation
**doesn't stabilize** the situation, the agent must not run into a dead-end. The
flow stays open to **user-led action** — the user can apply a different fix,
escalate, provide new data, or CLOSE. Concretely: a mitigation that fails to
stabilize leaves `mitigation.verified=false`; the agent acknowledges it didn't
work, may propose an alternative _in prose / as a fresh proposed action_, and the
case continues opportunistically (or closes). The "single record" constraint is a
data-model simplification, **not** a cap on how many remediation attempts the
conversation can explore. Multiple structured mitigation records are a
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

**As-built (R1) — `cause_state` is engine-derived, recomputed every turn.** The
LLM is not a raw setter of the enum; it keeps emitting a *grounded* "cause
identified" signal (the old `root_cause_identified` emission). The engine computes
the stored enum each turn in `_recompute_assessment_state`: **`IDENTIFIED`** if the
grounded signal is set and passes the self-naming-aware justification, **else
`CANDIDATES`** if `count_active_hypotheses(case) >= 2`, **else `UNKNOWN`**.
`IDENTIFIED` is forward-only (sticky once set). This reconciles "derive CANDIDATES
from hypothesis_manager" (Q4) with "the LLM still tells us when it knows the cause."

### 4.2 Action-compliance gates — track user compliance, gate nothing about RCA

These track whether the user accepted/verified a *proposed action*. They drive
the UI stage label and the resolution handshake, but **do not gate diagnostic
work** (that is gated by `cause_state` per §2).

| Field | Type | Meaning |
|---|---|---|
| `mitigation` | optional record `{proposed_at_turn, accepted, verified, completed_at_turn}` on `progress` (R2) | A mitigation insert. Its *existence* marks the case "stabilized". `completed_at_turn` (set when `verified`) is the boundary for up-weighting pre-mitigation evidence in later RCA. Replaces the legacy path-coupled mitigation gates. Its own validator enforces `verified ⇒ accepted` (forward-only). |
| `solution_accepted` | bool | User accepted the permanent solution → "Resolving" |
| `solution_verified` | bool | User confirmed the permanent solution worked → RESOLVED |

**As-built (R2):** the LLM **emits** `mitigation_accepted` /
`mitigation_verified` in `MilestoneUpdates`; the engine materializes the
`MitigationRecord` from those emission symbols plus the
`solution_type=workaround` ProposedAction. `solution_proposed` is derived:
`solution_state == SELECTED` AND a Solution record exists.

### 4.3 Removed / re-derived

- **`InvestigationPath` (MITIGATION_FIRST / ROOT_CAUSE)** — removed as a
  prospective fork. A retrospective descriptor `investigation_shape: DIRECT |
  STABILIZED` is derived from `mitigation is not None`.
- **`PathSelection` row + Gate 2 commit** — removed. The post-`symptom_verified`
  assessment (§3.2) no longer materializes a path; it may surface a
  mitigation _proposal_.
- **`_SYMPTOM_VALIDATION_BLOCK`, `_GATE3_PENDING_BLOCK`, `_POST_MITIGATION_RCA_PREFIX`,
  `_PRE_PATH_DIAGNOSIS_BLOCK`, `pre_*` restricted states, the path-conditional emission
  backstop (`_path_conditional_emission_restriction` / `_RESTRICTED_STATE_BLOCK_NAMES`)** —
  all removed. The diagnostic-machinery rule (§2) and the forwarding table (§3.1)
  replace them. **As-built (R6):** the §2 rule is **prompt-guided**, not a hard engine
  reject — the engine no longer bans hypothesis / causal-evidence emission by state.
- **`investigation_router.py` (urgency path recommender) + `test_investigation_router.py`** —
  deleted outright (R5). Mitigation is proposed by the LLM in-prompt, not via a
  user fork; there is no recommendation to compute.
- **Intents `PATH_SELECTION` / `POST_MITIGATION_CHOICE`** — removed from `IntentType`,
  along with the Gate 2/Gate 3 affordances, predicates, and metrics (R5).
- **`current_stage` (DIAGNOSIS/MITIGATION/TREATMENT)** — re-derived as a pure UI
  view:
  - `mitigation.accepted && !mitigation.verified` → "Mitigating"
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
   [mitigation insert] ── accepted ─▶ "Mitigating" ── verified ─▶ return to flow
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
  `mitigation.verified` requires `.accepted`. These survive — they are real
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

## 7. What shipped (blast radius)

Pre-production, no back-compat ([feedback_no_backcompat_pre_data]): the change
collapsed to the clean model rather than stacking shims. As built:

- **Schema:** added `CauseState` / `SolutionState` / `SolutionFeasible` enums and
  `MitigationRecord`; added `cause_state` / `solution_state` /
  `solution_feasible` / `mitigation` to `InvestigationProgress` (all inside the
  `progress` JSON). Cut `root_cause_identified` boolean cleanly — read sites use
  `cause_state == IDENTIFIED`. `InvestigationPath` / `PathSelection` / the
  `mitigation_*` booleans / `pre_*` machinery removed. **Migration 016
  (`0a1b2c3d4e5f`) drops the `cases.path_selection` column** — the only DDL change.
- **Engine (`milestone_engine.py`):** `validate_reasoning_first` returns the *set*
  of offending milestones and the caller strips only those (per-milestone surgical
  strip). The path-conditional emission backstop is **deleted** (not replaced with
  a `cause_state` reject — the rule is prompt-guided). `_recompute_assessment_state`
  derives `cause_state` / `solution_state` each turn (see §10 for the IDENTIFIED
  derivation). The mitigation side-effects materialize `MitigationRecord` from
  the `mitigation_*` emissions; the §3.1 forwarding row 3 (deferred implementation)
  is engine-proposed (§10-A). Parse-time validation hardening **shipped** as a
  general never-500 backstop (§10-C / §9).
- **Prompts (`templates.py`):** the four path blocks are deleted; DIAGNOSIS assembles
  one block (`focus_emphasis + _RCA_DIAGNOSIS_BLOCK`). `_select_diagnosis_block` kept
  its name as a thin wrapper. The hypothesis-emission-under-uncertainty mandate lives
  in `_HYPOTHESIS_EVIDENCE_ORDERING_BLOCK` inside the single block.
- **Recommender:** `investigation_router.py` **deleted** (R5).
- **Closure:** reasons collapsed to `{inquiry_only, closed_after_investigation}` —
  `mitigation_sufficient` dropped and folded into `closed_after_investigation`
  (`derive_closure_reason` in `terminal_transitions.py`).
- **Invariants:** retired INV-17, INV-19, INV-20, INV-21; added INV-22
  (`cause_state` never path-stripped), INV-23 (surgical strip), INV-24
  (single forward-only mitigation). Revised INV-05 to the mitigation record.
  INV-03 (disposition handshake) and the close-anytime rule are unchanged.
- **Docs:** synced `investigation-lifecycle-logic.md` (§1.x, §2, §4), the stage
  docstrings in `models.py`, `investigation-data-models.md`, `agent-stage-playbook.md`,
  `agent-behavioral-rules.md`, `evidence-needs-design.md`, `case-schema.md`.

---

## 8. Decisions (resolved 2026-06-05)

1. **Solution deliberation loop (§6).** ✅ **Reuse hypothesis/evidence-needs
   machinery**; introduce a dedicated structure only if a clear advantage emerges.
2. **Disposition for deferred-implementation (circumstance 3).** ✅
   **CLOSE-with-documented-solution** — no third terminal state unless analytics
   require separating "resolved-pending-impl" from "abandoned."
3. **Multiple mitigations.** ✅ **Single record for now**, but the flow must
   stay open to user-led action so a non-mitigating insert is never a dead-end
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

## 9. Parse-time structured-output robustness (S4) — backstop SHIPPED (§10-C)

> **Update 2026-06-06:** the never-500 backstop described below **shipped** as
> part of validation hardening — see §10-C. This section is retained for the
> problem statement and the upstream direction. The core redesign still only
> addresses **post-parse** failures; the parse-time backstop is the separate
> layer, now implemented.

This redesign's core addresses **post-parse** failures — milestone application, the
collateral strip, and path-conditional bans (the S1 trap). The distinct, earlier
failure layer surfaced by the QA campaign (**S4**):

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

**Backstop shipped (§10-C).** The graceful parse-time recovery is now implemented.
Provider-native constrained generation remains the **upstream** mitigation
([project-llm-structured-output-strategy]); the backstop is the safety net, not a
per-variant patch.

---

## 10. Post-ship enhancements (validation-driven, 2026-06-05/06)

These landed during end-to-end validation of the shipped redesign (the
k8s-pvc-pending trap scenario re-run across four runs). They refine the model;
none reopens the path fork. All are unit-tested in
`tests/unit/core/investigation/test_surgical_strip_and_cause_state.py`.

**Regression caught by validation (fixed):** the milestone-claim **revert** path
(`validate_milestone_claims` → `setattr(progress, milestone, False)`) still wrote
the removed `root_cause_identified` bool → `AttributeError` → 500 on every turn
that identified a cause with <2 causal rows. A _dynamic_ `setattr` the static
sweep couldn't see; only a live run exposed it. Revert now maps to
`cause_state = UNKNOWN` with a `hasattr` guard.

**A — Deferred-implementation disposition (forwarding-table row 3 / §6).** When the
cause + fix are known but the fix can't be applied/verified this session (an
out-of-band change request, maintenance window, or another team), the LLM emits
`solution_feasible="deferred"` (new `MilestoneUpdates` field), and the engine
**deterministically proposes CLOSE-with-documented-solution**
(`_maybe_propose_deferred_close`, mirroring the `rca_infeasible` pattern) — the LLM
does not reliably drive to close on its own. Guarded against clobbering an in-flight
handshake; the documented root cause + solution are preserved on close
(`closure_reason = closed_after_investigation`). Prompt guidance added in
`_RCA_DIAGNOSIS_BLOCK`.

**B — `cause_state = IDENTIFIED` is engine-derived, not LLM-milestone-dependent.**
Validation showed the LLM routinely sets `root_cause_likelihood` (high) but skips
the `root_cause_identified` milestone, leaving `cause_state` stuck at UNKNOWN and
the prompt stuck in RCA-mode. `_recompute_assessment_state` now also derives
IDENTIFIED from observable grounding: **`root_cause_likelihood ≥ 0.7` AND ≥2 causal
evidence rows**, OR a recorded `RootCauseConclusion`. The evidence bar (≥2 causal)
**matches the milestone-claim validator** (`MILESTONE_EVIDENCE_EXPECTATIONS`), so a
claim reverted for insufficient evidence is not re-granted by derivation. IDENTIFIED
remains forward-only sticky. This makes `cause_state` a robust engine-owned truth
signal (R1) rather than a fragile dependency on a milestone the LLM may not set.

**C — Never-500 backstop for parse-time validation errors (S4 / §9).** A single
malformed sub-record (e.g. `evidence_to_add` with `source_type=text` and no
`source_file_id`; `evidence_need_updates{FULFILLED}` with no `fulfilling_evidence_id`)
made the _whole_ `InvestigationResponse_*` fail `model_validate_json` → 500, before
any milestone logic. `_validate_with_degradation` (general, not per-invariant):
(1) validate as-is; (2) on failure, **prune the exact list entries the
`ValidationError` loc points at** (general across `evidence_to_add` /
`evidence_need_updates` / `hypotheses_to_add` / any list field) and re-validate —
bad sub-records quarantined + logged (`structured_output_degraded`), the rest
survives; (3) else drop `state_updates` entirely and keep the conversational
`agent_response`; (4) else re-raise. Wired into both the schema-tool-call and
text-fallback parse paths. The cross-field invariants themselves stay (they gate on
real facts); upstream constrained generation remains the real fix.

**Harness note (not a FaultMaven defect):** the validation runs reported
"UNRESOLVED" even on a clean resolve because the fm-sre-simulator reads a stale
`case_status` key while the API returns `case_state` (post the #405 status→state
rename). The case genuinely transitions to RESOLVED in the DB; the harness can't
see it. Fix belongs in the simulator.
