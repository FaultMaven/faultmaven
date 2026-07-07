# EXECUTION CHECKLIST — Runbook template update → predicate program

**Owner:** harvest-grounding campaign (Slice 6 / #584). **Design source:** `AUDIT-runbook-template.md`
(§3 corrected-template spec). **Ratified decisions:** T2 = **sibling-elimination-only**; **M-D deferred**
(precondition below). **Lens:** NO INCORRECT RESULT / NO COLLAPSE — grounding-*rate* is out of scope.

**Sequence:** update template (1a + 1b + M-B) → ⛔ **Phase-0 go/no-go** → regenerate validation layer →
rebuild KB → flip flag. Do **not** regenerate the corpus before the Phase-0 gate passes.

---

## Step 1 — Update the template

### 1a. Broken guardrails — do these REGARDLESS of the Phase-0 outcome (pure correctness)
- [ ] Replace the **v3 human fill-in template** (`docs/operations/runbooks/template.md`) with v4 grammar, or
  delete it and re-point `kb_toolkit/docs/TEMPLATE.md:8-9` at `runbook-content-architecture.md §3`.
  *(Highest severity: an author following it today produces a v4-invalid runbook.)*
- [ ] Backend `runbook_validator.py`: required sub-fields document-level **WARNING → per-cause ERROR**;
  add `Statement ≤300` parity.
- [ ] **Port the MECE Statement invariants** (`check_cause_statement_invariants`, `_norm_statement`,
  Jaccard≥0.6) into `kb_toolkit/core/validator.py` so the **generation path** enforces MECE — the
  "byte-identical mirror" claim is **stale** (verified: the toolkit has only a max-chars check). Fix/remove
  the stale claim in `runbook_validator.py`.
- [ ] `conversion_service.py`: decide + document whether the conversion path emits predicates or is
  Statement/Chain/Interventions-only (today it silently produces predicate-dead runbooks); enforce the
  per-cause grammar in `create_runbook_from_template`.

### 1b. Predicate-contract standards (the new template) — land WITH M-B
- [ ] **T1** — `target` = verbatim token in **RAW uploaded telemetry** (not diagnostic-command output);
  `step` optional / provenance-only. Validator lint: warn on ≥2-space runs and ≤3-char / stop-word targets.
- [ ] **T2 (sibling-elimination-only)** — add optional `stance: "supports"|"refutes"` (default `supports`).
  A firing `refutes` → `node_state=REFUTED` (sibling eliminated). **NO `belief→0`** (that is M-D — deferred).
  Discipline: each non-fallback cause SHOULD carry ≥1 discriminating predicate; **`refutes`-authoring stricter
  than `supports`** (a wrong `supports` adds noise; a wrong `refutes` eliminates a real cause → at worst an
  under-claim/hand-off, which is safe *only* because M-D is not wired).
- [ ] **T3 (doc)** — clarify `**Indicators:**` = authoring vehicle for `<!-- match -->` predicates, not the
  match surface; `cause_schemas.py` docstring: drop "inert for matching," state predicates are the
  load-bearing **validation** surface (no matching weight).
- [ ] **M-B (paired engine change)** — normalize predicate matching (**case-fold + whitespace-collapse**) in
  `evaluate_predicate_against_text` for `contains`/`absent`; leave `exit_code`/`threshold` numeric untouched.
  Bounded to case + whitespace only (determinism / NO-INCORRECT preserved). **Pair with the ≤3-char/stop-word
  lint** so normalization doesn't amplify over-broad short tokens. *Highest-leverage change — revives existing
  targets without re-authoring and makes new symptom-surface targets robust; must precede any regeneration.*
- [ ] Update `runbook-content-architecture.md §3` (canonical) + the four authoring mirrors per audit §3.

**DO NOT wire M-D** (runbook-provenance `REFUTES` → `belief→0` → proof-by-exclusion). Deferred by decision.
Verified why: `belief→0` fires only on `counterfactual_refutes` = `CAUSAL_ABSENCE_EVIDENCE` REFUTES
(`causal_graph.py:246-250,349`); the SUPPORTS side has a runbook-provenance exception (line 242), the REFUTES
side does not. Wiring it would let a **wrong** `refutes` predicate manufacture a **wrong VALIDATED root** via
exclusion — the exact `incorrect` the completion measurement forbids. **Revisit precondition:** Phase-0 proves
the deductive value AND an **exclusion-specific adversarial eval** shows a wrong `refutes` cannot produce a
wrong VALIDATED root. (`refutes` correctness is *semantic* — can't be validated by a lint — so the gate is an
adversarial eval, backstopped by #593 guard #4: DEDUCTIVE is mechanistic-grade, counterfactual-before-RESOLVED.)

---

## ⛔ STOP — Phase-0 gate (go/no-go BEFORE any corpus regeneration)

The predicate/validation layer is **flag-off and its value is unproven** (0c: collapse-protection value is real
but *narrow* — careful models don't collapse on easy cases). Before spending the ~756-predicate regeneration:
- [ ] **Hand-author ONE runbook** with corrected predicates (T1 symptom-surface + a T2 sibling-eliminating
  `refutes`).
- [ ] Run the **adversarial acceptance case** (a confident-but-wrong-LLM scenario the predicate would catch —
  `ANALYSIS-runbook-grounding-pipeline.md §7` / the 0c masquerade).
- [ ] **GO** iff the predicate deterministically catches the wrong cause AND produces **no wrong VALIDATED
  root**. **NO-GO** → keep the 1a guardrails + M-B; **remove** the dead predicate layer rather than regenerate.
- [ ] **Human sign-off** on go/no-go.

---

## Step 2 — Regenerate the VALIDATION LAYER only (gated on GO)

Keep the sound, satisfied layers — **Statements, Chains, Interventions** (audit "keep" verdict). Regenerate
**only** predicates + counterfactuals.
- [ ] **Preferred:** predicate-only regeneration pass in kb-toolkit — author symptom-surface `supports` +
  sibling-eliminating `refutes` per existing cause, keeping Statement/Chain/Interventions.
- [ ] Re-run the **MECE-teeth collision audit** (~18 runbooks with identical sibling predicates) + backfill the
  ~11 uncovered discriminating causes.
- [ ] **Fallback** (only if the pipeline can't isolate predicates from Statements): full-regen all 91 from
  source — Statements/Chains come out materially the same; the delta is the validation layer.

## Step 3 — Rebuild KB + flip flag
- [ ] `kb-build-pack` → rebuild the vendored pack (`faultmaven/resources/knowledge/pack`).
- [ ] Flip `enable_runbook_cause_matcher` on (behind the campaign's normal validation).
- [ ] Re-run the grounded-link metric (0b) — confirm `runbook_arm` off 0 on matching-runbook cases.

---

## Execution log

**2026-07-07 — Step 1a COMPLETE** (worktrees off `origin/main`: `fm-tmpl-wt` + `kbtk-tmpl-wt`, branch
`feat/runbook-template-guardrails`). Docs recovered + force-committed first (`docs/working` is gitignored;
untracked copies had been wiped by a concurrent workstream).

- [x] **1a-1 v3 fill-in template** — deleted `docs/operations/runbooks/template.md`; re-pointed kb-toolkit
  `docs/TEMPLATE.md` at canonical §3 (which carries a copy-pasteable v4 example). Commits: fm `c2f2eab8`,
  kbtk `75e606d`. Chose delete+re-point over replace (fewer drift-prone mirrors).
- [x] **1a-2 backend validator parity** — required sub-fields now per-Cause **ERROR** (was document-level
  WARNING) + Statement≤300 ERROR; shared cause-block parser extracted. Verified **0 false failures across all
  91 shipped runbooks**; +8 tests, 305 knowledge unit tests green. Commit fm `3609f8c1`.
- [x] **1a-3 port MECE to kb-toolkit — DROPPED (already done).** CORRECTION: the audit's "generation path
  unguarded for MECE / stale byte-identical claim" was itself a **stale-checkout artifact** (mirror agent read
  the kb-toolkit checkout 14 behind `origin/main`). On `origin/main`, `kb_toolkit/core/validator.py:107-192`
  has `check_cause_statement_invariants` **byte-identical** to the backend (`diff` empty) and calls it — the
  mirror is real. Audit + memory corrected. Commit fm `afdbae5d`.
- [x] **1a-4 conversion predicate-less decision** — documented as intentional (comment at the prompt def, not
  in the prompt string); the "enforce per-Cause grammar in `create_runbook_from_template`" half is satisfied by
  1a-2 (that path calls `validate_content`, now strict). Commit fm `4a6a8e3f`.

**Caveat carried forward:** all kb-toolkit-side audit findings + the corpus sample were read from the stale
kb-toolkit checkout — **re-confirm on `origin/main`** before Steps 1b/2 (esp. the predicate/collision counts).

**NOT STARTED (correctly gated):** Step 1b (predicate contract T1/T2/M-A/M-B), the Phase-0 gate, Step 2/3.
1a shipped as guardrail/correctness only; the predicate program stays behind the Phase-0 adversarial gate and
belongs in the harvest-grounding campaign (Slice 6 / #584).
