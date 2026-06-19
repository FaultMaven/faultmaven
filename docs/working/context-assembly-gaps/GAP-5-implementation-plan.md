# GAP-5 (Phase 1) — Legacy→Absence Evidence-Category Migration: Implementation Plan

> **Status: PLAN — ready to implement, sequenced AFTER PR #478 merges.**
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
- **`CATEGORY_MILESTONE_MAP`** (`milestone_engine.py`): already routed off the
  absence categories. `MITIGATION_EVIDENCE → []` and `SOLUTION_EVIDENCE → []`
  (they fire **no** milestones today — emitting them produces inert rows);
  `SYMPTOM_ABSENCE_EVIDENCE → [mitigation_verified, solution_verified]`;
  `CAUSAL_ABSENCE_EVIDENCE → [solution_verified]`. So "migrate readers before
  writers" is *largely done* on the milestone path.
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

## 2. The behavioral DECISION POINT (resolve explicitly — do **not** assume)

This migration is **not cosmetic**, because it shifts the signal that fires the
verification gates:

- **Today:** `mitigation_verified` / `solution_verified` are set by the LLM
  (compliance / User-Agent handshake bool), and the legacy `mitigation_evidence` /
  `solution_evidence` row created alongside is **decorative** (maps to `[]`). The
  absence categories *also* fire those gates via the map — a parallel path that is
  already wired but not consistently driven by the prompts.
- **After migration:** the gate is fired by the LLM emitting
  `symptom_absence_evidence` / `causal_absence_evidence`, which the map turns into
  the milestone.

So the open question the implementer must resolve **with the team, backed by
persona evidence** — not silently:

> Should each verification gate be **(a)** evidence-driven (fired by absence
> evidence), **(b)** compliance-driven (the handshake bool), or **(c)** both?

This changes *when and how* cases verify, mitigate, and resolve. Pick deliberately,
document the choice, and pin it with a regression test. The safest default is
likely **(c) both** during transition (absence evidence OR the handshake bool
fires the gate), narrowing later — but validate, don't assume.

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

**Step 2 — resolve the §2 decision** (gate-firing signal) and write it down.

**Step 3 — migrate the writers (the behavioral core).**
Rewrite every `templates.py` legacy-emit site to the absence categories,
consistent with the existing absence guidance. Remove the contradictory dual
guidance so the prompt drives **one** path. This is the change that alters LLM
behavior.

**Step 4 — behavioral validation (mandatory gate; do not skip to Step 5).**
Shadow + persona/scenario testing across the disposition matrix
(INQUIRY→INVESTIGATING, symptom→mitigation→solution, RESOLVED/CLOSED, mitigation-
first, KB-resolution). **Assert the same cases reach the same dispositions** via
the absence path as they did via the legacy path. Diff milestone progression and
terminal transitions before/after. This is the safety gate — the whole point of
treating GAP-5 as sensitive.

**Step 5 — remove the now-inert legacy.**
Delete `MITIGATION_EVIDENCE`/`SOLUTION_EVIDENCE` from `EvidenceCategory`, their
`CATEGORY_MILESTONE_MAP` entries, and the description strings. Check the
`evidence_source_invariant` CHECK constraint and Pydantic validators don't
reference them.

**Step 6 — update tests in lockstep.**
Rework `test_mitigation_evidence_gate.py` to the absence gate; fix the other two
pinned tests. Add a regression pin for the §2 gate-signal decision and for "no
prompt emits a legacy category" (a source grep test).

---

## 4. Acceptance criteria

- No prompt emits `mitigation_evidence`/`solution_evidence`; the source-grep
  regression test passes.
- `EvidenceCategory` no longer contains the legacy values; nothing references them
  (`grep -rn "MITIGATION_EVIDENCE\|SOLUTION_EVIDENCE\|mitigation_evidence\|solution_evidence" faultmaven/` is clean except intended history).
- Reports render correctly off the absence/agreed signal.
- **Persona/shadow: identical dispositions pre/post** across the matrix (the
  behavioral guarantee).
- The §2 gate-firing decision is documented and pinned by a test.
- The graceful terminal ("nothing left to search → RESOLVED") still fires — no
  stuck loop (regression-pin it; there is prior stuck-loop history).

---

## 5. Sequencing & safety rules (this is sensitive engine behavior)

1. **Start from a clean post-#478 tree.** One workstream; do not edit
   `templates.py`/`milestone_engine.py` concurrently with another agent.
2. **Readers before writers** (mostly done — verify Step 1 before Step 3).
3. **Behavioral validation (Step 4) is mandatory before enum removal (Step 5).**
   No unit-test-only sign-off; this changes investigation outcomes.
4. **Resolve the §2 decision deliberately** — it's the one place a silent
   behavior change could hide.
5. Reviewer checks the PR against §4 + the persona/shadow deltas, with particular
   attention to the gate-signal decision and the "same dispositions" diff.

---

## 6. Recommended companion (separate, additive PR — not this one)

The Phase-1 **observability** that was removed from #478 (promotion metrics +
per-turn snapshot) is genuinely useful and low-risk. Land it as its **own
metrics-only PR** *before* Step 3, to baseline promotion behavior pre/post the
migration. Keep it out of this behavioral PR so the two are reviewed separately.
Pair it with confirming the graceful-terminal fail-safe — these two (observe +
fail-safe) are how we handle the irreducible "LLM just won't comply" remainder
**without** mandating behavior in the engine.
