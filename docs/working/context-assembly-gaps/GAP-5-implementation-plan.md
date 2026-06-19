# GAP-5 (Phase 1) — Legacy→Absence Evidence-Category Migration: Implementation Plan

> **Status: IMPLEMENTED (simplified, outright-removal). #478 merged; this work
> on branch `docs/gap-5-evidence-promotion-plan`.**
>
> **Simplification applied (no users yet):** the original "readers-before-writers
> with a transition period" framing has been collapsed. There is no production
> data to protect, so there is **no feature flag, no deprecation period, no
> dual-path, no compat shim**. `MITIGATION_EVIDENCE` / `SOLUTION_EVIDENCE` are
> removed **outright in one PR** (enum value + `CATEGORY_MILESTONE_MAP` entries +
> prompts), and the validation is re-aimed from "no regression vs. the legacy
> path" to **"prove the clean end-state is correct."** The §2/§4/§5 sections
> below reflect this resolved shape; struck framing from the original plan is
> noted inline where it helps explain a decision.
>
> This supersedes the original GAP-5 ladder (observe → prompt-nudge → engine-assist)
> for this phase. The nudge/assist work was implemented, reviewed, and **backed
> out of #478** (it was an un-QA'd prompt change shipping by default); it is
> preserved whole on branch `gap-5-evidence-promotion` and is **out of scope here**.
>
> **Origin of this plan:** review discussion concluding GAP-5 is predominantly a
> *design↔implementation gap*, not a redesign. See "Decision summary" below.
>
> **Authoritative code (after implementation):**
> `faultmaven/modules/case/domain/models.py` (`EvidenceCategory`),
> `faultmaven/core/investigation/milestone_engine.py` (`CATEGORY_MILESTONE_MAP`),
> `faultmaven/core/investigation/prompts/templates.py` (the emit sites).
>
> **Related:** `investigation-lifecycle-logic.md` §1.2.1 (evidence lifecycle),
> `evidence-driven-investigation-framework.md` §5 (evidence model),
> `evidence-context-assembly.md` §5 (rejected auto-stub).

---

## 0. Decision summary (why this plan exists, and what it is *not*)

GAP-5 ("evidence-creation timing is fragile") was originally framed as needing
new machinery. Review concluded the opposite: **the machinery already exists and
the design is sound; the failures are the implementation not matching the design.**

What already exists and is correct:

- **The four verification signals** map cleanly to a presence/absence × symptom/
  causal quartet:
  - symptom **presence** → `symptom_verified` (problem verified)
  - symptom **absence** → `mitigation_verified` (symptom gone after a workaround)
  - causal **presence** → `root_cause_identified` (hypothesis validated)
  - causal **absence** → `solution_verified` (cause gone after the fix)
- **Demand/supply matching** that decouples *when data arrives* from *when a claim
  is recorded*: `EvidenceNeed`s (demand) + `uploaded_files` (supply). The need is
  effectively the hypothesis ("what we're searching for"); when a new file
  arrives the LLM searches it against open needs and records evidence on a hit.
  Whichever side arrives first is durably recorded, so nothing is permanently
  lost. Demand is *phase-implied*: entering INVESTIGATING ⟹ a symptom-verification
  need; an active hypothesis ⟹ a causal need; **nothing left to search ⟹ the
  terminal condition (RESOLVED)**, not a stall.

Therefore the residual GAP-5 work is **closing the gap between that design and a
half-finished implementation** — specifically, the `EvidenceCategory` model marks
`mitigation_evidence`/`solution_evidence` as **legacy, "slated for removal once
prompts stop emitting them,"** but the prompts have *not* stopped. The result is a
half-migrated, internally-contradictory prompt surface.

**Explicitly OUT of scope for this plan** (do not re-introduce):

- Phase-2 prompt-nudge (`<unpromoted_files_notice>`) and Phase-3 engine-assist
  (`_maybe_assist_evidence_promotion`) — backed out of #478, preserved on
  `gap-5-evidence-promotion`. Revisit only after this migration + an observability
  baseline, if at all.
- **Hard engine-derived demand** — rejected: mandating the demand in the engine
  risks fragile hard-fails with no fallback. The engine should *observe*, not
  *mandate*.
- Mechanical "auto-stub" evidence on upload — rejected (see
  `evidence-context-assembly.md` §5): it manufactures claimless rows that pollute
  the milestone map and scoring.

---

## 1. Impact map (as of `origin/main` + #478; reference by **symbol**, not line — lines shift)

### 1.1 The categories
`EvidenceCategory` (`models.py`): the absence quartet
(`SYMPTOM_EVIDENCE`, `CAUSAL_EVIDENCE`, `SYMPTOM_ABSENCE_EVIDENCE`,
`CAUSAL_ABSENCE_EVIDENCE`) **plus two legacy values to remove**:
`MITIGATION_EVIDENCE = "mitigation_evidence"`, `SOLUTION_EVIDENCE = "solution_evidence"`.

### 1.2 READERS — **already mostly migrated** (this is the key finding)

- **`CATEGORY_MILESTONE_MAP`** (`milestone_engine.py`): on `origin/main` the
  legacy values already routed to nothing (`MITIGATION_EVIDENCE → []`,
  `SOLUTION_EVIDENCE → []` — inert rows), while the absence categories routed to
  the gate milestones (`SYMPTOM_ABSENCE_EVIDENCE → [mitigation_verified,
  solution_verified]`, `CAUSAL_ABSENCE_EVIDENCE → [solution_verified]`).
  **End state (this PR): the absence categories are neutralized to `[]`** — see
  the §2 decision below. The map performs evidence *attribution* (intersection-
  based `_infer_milestones`), and the verification gates are **not**
  evidence-attributed: they are handshake-set by the LLM and the absence rows are
  consumed directly by the readiness checks (`_has_causal_absence`). Mapping
  absence → a gate milestone would have been a *second*, silent firing path. So
  the clean end-state map is: presence categories attribute their milestones
  (`SYMPTOM_EVIDENCE → [symptom_verified]`, `CAUSAL_EVIDENCE →
  [root_cause_identified, solution_proposed]`); absence categories → `[]`.
- **The one real non-milestone legacy reader:**
  `report_generation_service.py` (~`:433`) branches on
  `ev.category.value in ("causal_evidence", "solution_evidence")`. This must be
  migrated to the absence category (or the agreed "fix-worked" signal) **before**
  the enum value is removed.
- **Cosmetic / description-only** mentions (no logic): `models.py` field
  description, `models/case_ui.py`, `models/api_models.py` (these say
  `RESOLUTION_EVIDENCE`, a stale name — clean up while here).

### 1.3 WRITERS — prompts still emit legacy (~15 sites, all in `templates.py`)
The evidence-classification block (category enumerations) and the MITIGATION /
TREATMENT instruction blocks still instruct the LLM to create
`mitigation_evidence` / `solution_evidence` rows — e.g. "first create a
`mitigation_evidence` record … then set `mitigation_verified=True`", and
"create a `solution_evidence` record". These **coexist** with already-correct
absence guidance in the same file ("you MUST record a `causal_absence_evidence`
row", "case RESOLVED only when a `causal_absence_evidence` row is on record"). The
contradiction is the bug. Find all sites with:
`grep -n "mitigation_evidence\|solution_evidence" faultmaven/core/investigation/prompts/templates.py`

### 1.4 TESTS pinning legacy (update in lockstep)
`test_mitigation_evidence_gate.py` (squarely about the legacy gate — likely
becomes a `symptom_absence` gate test), `test_evidence_source_invariant.py`,
`test_surgical_strip_and_cause_state.py`. Re-grep before starting:
`grep -rln "mitigation_evidence\|solution_evidence\|MITIGATION_EVIDENCE\|SOLUTION_EVIDENCE" tests/`

---

## 2. The behavioral DECISION POINT — **RESOLVED: (b) compliance-driven**

The open question was whether each verification gate should be **(a)**
evidence-driven (fired by absence evidence via the map), **(b)** compliance-driven
(the LLM handshake bool), or **(c)** both.

**Decision: (b).** The gates (`mitigation_verified` / `solution_verified`) stay
LLM-set via the User-Agent handshake / compliance signal — exactly as on
`origin/main`. The migration changes **attribution, not firing logic**:

- The legacy `mitigation_evidence` / `solution_evidence` rows the prompts emitted
  alongside the handshake were decorative (mapped to `[]`) and *contradicted* the
  already-correct absence guidance in the same prompts. That contradiction is the
  bug. Migrating the writers to the absence quartet collapses the dual prompt path
  to one, so the recorded evidence finally describes what actually happened
  (symptom relieved / cause eliminated) instead of a stage-named placeholder.
- The absence rows are **not** wired to fire the gates. They are consumed
  *directly* by the readiness checks (`assess_resolution_readiness` /
  `assess_closure_readiness` via `_has_causal_absence`), which is why
  `CATEGORY_MILESTONE_MAP` neutralizes the absence categories to `[]` (§1.2).
  Mapping them to the gate milestones would re-introduce a second, silent firing
  path — the opposite of the "one path" goal.

Rejected **(c) both**: the original plan floated "(c) during a transition,
narrow later." With no users and outright removal there is no transition to hedge
for; (c) is exactly the dual-path this migration exists to delete. **(a)** was
rejected for the same reason — it would move gate-firing into the map and away
from the handshake the engine already trusts.

Pinned by `test_gap5_absence_migration.py` (absence categories map to `[]`;
presence categories still attribute; readiness checks resolve/close off the
absence rows).

---

## 3. Implementation steps (readers-before-writers; each independently verifiable)

**Step 0 — clean base.** Branch off `main` **only after #478 has merged.** This
work edits `templates.py` (and possibly `milestone_engine.py`/`models.py`), which
#478 also edits — starting earlier recreates a multi-agent merge tangle.

**Step 1 — finish the readers (additive, no prompt change yet).**
Migrate `report_generation_service.py` off `solution_evidence` to the absence
category (or the agreed fix-worked signal). Re-run the reader grep to confirm no
*other* code branches on the legacy categories. Tests stay green; behavior
unchanged (legacy still emitted, still inert).

**Step 2 — resolve the §2 decision** (gate-firing signal). **Done: (b)**, written
up in §2.

**Step 3 — migrate the writers (the behavioral core).**
Rewrite every `templates.py` legacy-emit site to the absence categories,
consistent with the existing absence guidance. Remove the contradictory dual
guidance so the prompt drives **one** path. This is the change that alters LLM
behavior. (Notable: the TREATMENT *failure* path had no absence equivalent — a
failed fix means the cause persists — so its `solution_evidence` row is removed
outright, not remapped; failure is captured by the REFUTED hypothesis.)

**Step 4 — correctness validation (re-aimed; two tiers).**
The original "shadow both paths, assert identical dispositions pre/post" gate is
**dropped** — with outright removal there is no legacy path to diff against, and
the goal is no longer "no regression" but "the clean end-state is correct."
Replaced by:

- **Tier-1 — deterministic golden tests (THE MERGE GATE).** Feed canned Cases
  with absence evidence straight through the readiness checks and assert the
  correct terminal disposition fires: `causal_absence` → RESOLVED; `symptom_absence`
  only → NEEDS_INFO / CLOSE (never auto-resolve); close-on-resolvable pivots to
  RESOLVE; nothing investigated → CLOSE. Plus the source-grep / enum / map pins.
  Lives in `tests/unit/core/investigation/test_gap5_absence_migration.py`. No
  LLM, deterministic, runs in CI.
- **Tier-2 — persona disposition matrix (dev correctness check, NOT a gate).**
  The five `disp-*` scenarios in `fm-sre-simulator` exercise the same matrix
  end-to-end against a live stack (applied-not-verified → INVESTIGATING,
  out-of-band / KB-sourced / close-on-resolvable → RESOLVED, unverified-resolve →
  CLOSE). Push-button: `fm-sre-simulator/scripts/run-disposition-matrix.sh`
  (manifest `config/disposition-matrix.yaml`). Stochastic; run on whatever dev
  stack is handy as a confidence check, not a CI blocker.

**Step 5 — remove the now-inert legacy (same PR — no deprecation period).**
Delete `MITIGATION_EVIDENCE`/`SOLUTION_EVIDENCE` from `EvidenceCategory`, their
`CATEGORY_MILESTONE_MAP` entries, and the description strings. Check the
`evidence_source_invariant` CHECK constraint and Pydantic validators don't
reference them. Migrate the one real legacy reader
(`report_generation_service.py`, "Confirming Evidence") to the absence category
**before** the enum value is deleted.

**Step 6 — update tests in lockstep.**
Rework `test_mitigation_evidence_gate.py` to a surviving category; fix the other
two pinned tests (`test_evidence_source_invariant.py`,
`test_surgical_strip_and_cause_state.py`). Add the regression pins for the §2
decision and for "no prompt emits a legacy category" (source-grep test).

---

## 4. Acceptance criteria

- No prompt emits `mitigation_evidence`/`solution_evidence`; the source-grep
  regression test passes.
- `EvidenceCategory` no longer contains the legacy values; nothing references them
  (`grep -rn "MITIGATION_EVIDENCE\|SOLUTION_EVIDENCE\|mitigation_evidence\|solution_evidence" faultmaven/` is clean except intended history).
- Reports render correctly off the absence signal (Confirming Evidence reader
  migrated to `causal_evidence` / `causal_absence_evidence`).
- **Tier-1 golden tests pass** — the absence-driven flow reaches the correct
  dispositions (the merge gate). `causal_absence` → RESOLVED; `symptom_absence`
  only → CLOSE, never auto-resolve.
- The §2 gate-firing decision (b) is documented and pinned by a test (absence →
  `[]` in the map; presence categories still attribute).
- The graceful terminal ("nothing left to search → CLOSE/RESOLVED") still fires —
  no stuck loop (covered by `disp-unverified-resolve`; prior stuck-loop history).
- Full investigation + modules suite green (no collateral regressions from the
  enum removal).

---

## 5. Sequencing & safety rules

1. **Start from a clean post-#478 tree.** One workstream; do not edit
   `templates.py`/`milestone_engine.py` concurrently with another agent.
2. **Migrate the one real reader before deleting the enum value** —
   `report_generation_service.py` first, then the enum (§3 Step 5).
3. **Tier-1 golden tests are the merge gate.** They prove the clean end-state is
   correct (not "no regression vs. legacy" — there is no legacy path left to diff).
   Tier-2 persona runs are a dev confidence check, not a blocker.
4. **One PR, outright removal.** No feature flag, no deprecation period, no
   dual-path, no compat shim — there are no users to protect and the dual path is
   exactly what this migration deletes.
5. Reviewer checks the PR against §4 + the §2 decision, with particular attention
   to the map-neutralization (absence → `[]`) and the readiness-check reads.

---

## 6. Recommended companion (separate, additive PR — still open)

The Phase-1 **observability** that was removed from #478 (promotion metrics +
per-turn snapshot) is genuinely useful and low-risk. Land it as its **own
metrics-only PR**, kept out of this behavioral PR so the two are reviewed
separately. With the migration now done, its value shifts from "baseline pre/post"
to **ongoing visibility** into how often absence evidence is actually recorded
when the gates fire — i.e. instrumenting the residual "LLM set the handshake but
never recorded the absence row" case. Pair it with the graceful-terminal
fail-safe (covered by `disp-unverified-resolve`): observe + fail-safe is how we
handle the irreducible "LLM just won't comply" remainder **without** mandating
behavior in the engine.
