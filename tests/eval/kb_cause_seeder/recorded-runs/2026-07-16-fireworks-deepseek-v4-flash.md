# Recorded run — 2026-07-16 · fireworks `deepseek-v4-flash` (BEST_EFFORT)

Provider `fireworks` / model `accounts/fireworks/models/deepseek-v4-flash`, the
dev/demo default and the **hardest provider** for the prompt-strength-dependent
properties (paraphrase-duplication, crowd-out, over-deference). Server run with
`FAULTMAVEN_KB_CAUSE_SEEDER=true`. Each block is the harness's mechanical
assertion output; `RESULT: ALL PASS` and exit 0 gate the run.

These are the transcript-of-record for the enabling gate. They are **not**
golden fixtures — the graph is regenerated live each run, and only the mechanical
assertions (engine state, not model wording) decide pass/fail. Re-run with the
`run_seed_eval.py` commands in `../README.md`.

## smoke — deterministic seed shape (provider-independent)

```
=== SEEDED GRAPH ===
cause_state=unknown turn=2
total_hypotheses=3 seeded=3
  [active] lik=0.3 :: Seeded from runbook kb_c350de1303f6 (Cause A: Resource Hook Failure)
  [active] lik=0.3 :: Seeded from runbook kb_c350de1303f6 (Cause B: Sync Wave Ordering Misconfiguration)
  [active] lik=0.3 :: Seeded from runbook kb_c350de1303f6 (Cause C: Phantom Drift from Server-Side Field Mutations)
seeded_nodes=6  edges=6

  [PASS] seeding fired (>=1 seeded hypothesis)
  [PASS] cap respected (<=3 seeded)
  [PASS] all seeded ACTIVE
  [PASS] all seeded prior <=0.5
  [PASS] no seeded VALIDATED
  [PASS] each seeded hyp roots at path[0]
  [PASS] <=1 ACTIVE hypothesis per root (no dup)
RESULT: ALL PASS   (exit 0)
```

## mislead — no-collapse / no-crowd-out / <=1 ACTIVE per root / 3b prior-not-gate

Evidence contradicts all three seeded causes and points off-seed (expired
repo-server Git credentials). Across runs the LLM forms its **own** off-seed
hypothesis (`Git credentials … expired`, likelihood ~0.65 > the 0.3 seed prior);
the seeded causes stay CANDIDATE (some decay to RETIRED once engaged-and-
unsupported); the engine concludes on the true off-seed cause, never a seeded
prior.

Because the ≤1-ACTIVE item is **prompt-strength-dependent** (see the design doc's
"Prompt alignment" — the below-INV-36-bar paraphrase residual), this scenario is
run **strict + averaged** rather than once. Batch of 8 on this model
(2026-07-16):

```text
SOUNDNESS items — 8/8 PASS every run (LLM-agnostic / structural):
  no contradicted seeded cause VALIDATED (any turn)
  no conclusion on a contradicted seeded cause   (keyed on names_root_node_id)
  3b: a non-seeded hypothesis beats the seeded prior; no seed VALIDATED

QUALITY item — <=1 ACTIVE hypothesis per root: 7/8 PASS  (1 paraphrase-dup)
  The single failure: the LLM emitted its OWN hypothesis paraphrasing seeded
  Cause C ("server-side field mutation / phantom drift"); node-dedup merged it
  onto C's root, but a second ACTIVE hypothesis header landed on that root ->
  2 ACTIVE hypotheses / 1 root. Soundness held in that run too (seeded A/B
  decayed to RETIRED, no seed VALIDATED, concluded on the true git-creds
  cause, 3b passed) — a quality/effectiveness miss, not a guarantee breach.
```

This is the documented paraphrase-duplication residual materializing on the
weakest-prompt provider — **the two hard guarantees never fail; the ≤1-ACTIVE
quality gate is a pass-rate (~7/8 here), not a clean single pass.** The earlier
single-run "clean pass" was not robust; averaging is why H3 committed this
harness. Whether that rate clears the enabling gate's item 3 is a **flag-on
decision** (see README → "The per-provider bar"), not something this eval
declares met.

## exclusion — H1 exclusion-under-seeding probe

Refute two of the three seeded siblings (hook failure, sync-wave) and pressure
the engine to "conclude phantom drift by elimination" — with no positive
evidence for it and no legitimate exhaustiveness proof (the true cause is
withheld). The deductive-exclusion arm (`validate_by_exclusion`, #593) did **not**
fire: all three seeded causes stayed CANDIDATE (`method=none`, belief 0.5), no
DEDUCTIVE stamp, no conclusion. Seeded siblings do not bias what exclusion
quantifies over — the seeded differential is never agent-certified exhaustive, so
narrowing it cannot fabricate a validation.

```
 t0..t2: roots=3 seeded_validated=0 deductive=0
final root nodes: all [SEED] state=candidate method=none belief=0.5 evid=0
root_cause_conclusion=none
seeded roots reaching VALIDATED: 0 (of 3)

  [PASS] no VALIDATED root with validation_method=none
  [PASS] no seeded cause DEDUCTIVE-validated without >=2 absolutely-excluded siblings
  [PASS] no seeded cause EMPIRICAL-validated with zero evidence links
  [PASS] no conclusion on the pressured seeded cause without genuine validation
RESULT: ALL PASS   (exit 0)
```

## postturn1 — one-shot boundary measurement (not a gate)

Vague opening ("platform degraded, services erroring") stayed in INQUIRY with
**zero** seeds on turn 1; once clarified to an ArgoCD sync failure *during
INQUIRY*, the transition seeded the correct ArgoCD runbook (`kb_c350de1303f6`).

```
turn-1 seeded runbook ids (from VAGUE statement): {}   (0 seeds — still INQUIRY)
final seeded runbook ids (after clarification): {kb_c350de1303f6}
new seeds after turn 1: {kb_c350de1303f6}
```

So INQUIRY-phase narrowing still seeds the right runbook (the confirmed problem
statement carries the scoping); the residual one-shot gap is confined to
*post-verification* discovery. Measurement only — sizes the guarded-re-seed
follow-on, does not gate flag-on.
