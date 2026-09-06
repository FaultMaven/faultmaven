# Task spec — #673 Stage A: RCC precedence inversion (canary)

Owner session dispatches this to the implementer. Issue: FaultMaven/faultmaven#673
(assigned). Read the ISSUE BODY and the architect comment on it first — they are
the authority; this spec pins the decisions already made.

## Scope decision (made — do not re-litigate)

**Stage B (retirement) is GATED and out of scope.** The INV-41 backstop-reliance
metric (`resolution_cause_leg_total`, PR #686) merged 2026-07-12 — the required
rolling-30-day window cannot have elapsed. This task is **Stage A only**, the
architect's "canary, reversible" step:

> invert precedence only — the engine mirror becomes the surfaced conclusion
> whenever a validated chain root exists; the LLM RCC demotes to a labeled
> fallback used *only* when no root stands (today's backstop shape, now
> explicit). Reconciliation layer stays.

## What changes

1. **Precedence inversion** at `synthesize_rcc_from_validated_root`
   (`faultmaven/core/investigation/causal_graph.py:1882`). Today an LLM-authored
   RCC (`determined_by != _ENGINE_RCC_AUTHOR`) is never overwritten ("the LLM's
   own conclusion always wins"). Invert: when a standing validated, uncontested
   chain root exists, the engine mirror is minted/refreshed **even over an
   LLM-authored RCC**. All existing refusals stay AHEAD of the inversion:
   MECE-contested → no mirror (read-suppress unchanged); no standing validated
   root → no-op, the LLM RCC stands untouched (the explicit fallback).
   - Check every call path of `synthesize_rcc_from_validated_root` and
     `retract_stale_engine_rcc` (the per-turn recompute gating) — the inversion
     must hold on ALL paths that can mint the mirror, not just one call site.
   - `retract_stale_engine_rcc` semantics unchanged (engine mirrors only).
     Note the interaction: an LLM RCC replaced by a mirror whose root later
     demotes → mirror retracted → conclusion is None (NOT the old LLM text —
     that text asserted the same now-unsupported cause world; resurrecting it
     would be the exact over-claim the chain no longer backs). Document this.

2. **Kill-switch flag** (reversibility is the point of the canary):
   `FeatureSettings` (`faultmaven/config/settings.py`, `class FeatureSettings`)
   gets `chain_authored_conclusion: bool = True` (env-settable; pydantic-settings
   v2 — use `validation_alias=`, NEVER `env=` in `Field()`). Default **ON**
   (pre-production, we want the canary data; revert = flip off). Consulted at the
   single precedence point in `synthesize_rcc_from_validated_root`; flag OFF
   restores today's behavior exactly.

3. **Metric** (the architect's "second read" for INV-41):
   `rcc_precedence_inversion_total{provider}` in
   `core/investigation/lifecycle_metrics.py` — incremented exactly when the
   inversion actually replaces an LLM-authored RCC (not on every mirror mint).
   Resolution-time fallback surfacing is already measured by INV-41's `rcc` leg —
   do NOT duplicate it. Provider label: same resolution rule as INV-41
   (`_resolve_resolution_provider` / `_resolve_chat_provider_name` precedent —
   pick whichever the call site can honestly resolve; document label-equivalence
   if you use the settings fallback).

4. **Fallback labeling — verify, don't rebuild.** The §3.5 grade-juxtaposition
   work (backend #687: `cause_assurance` + `cause_overclaim` on
   `RootCauseSummary`/`TurnResponse`, recomputed from graph) should already label
   a no-root fallback RCC ("Unvalidated" chip / `_assurance_note`). VERIFY that a
   fallback-surfaced LLM RCC at terminal carries the grade label on the resolved
   payload + turn response; record the verification in the design doc. Only add
   labeling if a terminal surface is actually bare. No frontend changes.

5. **Known expressiveness deltas — handle explicitly in the design doc:**
   - The mirror renders mechanism from rung statements (" → " join). Flat prose
     is an ACCEPTED Stage A cost (architect: fix via rung-statement elicitation,
     not a parallel namespace). Non-goal here.
   - `case_ui_adapter` reads `rcc.contributing_factors[:5]` for key_insights;
     the engine mirror sets none. When the mirror replaces an LLM RCC those
     insights disappear. Decide and document: accepted loss (single authority —
     recommended) — do NOT blend LLM fields into the engine mirror.
   - `milestone_engine.py:~7933` (LLM RCC write path) and the
     `knowledge_resolution` co-author note nearby: LLM authorship is UNCHANGED
     in Stage A (schema + prompt untouched). The LLM keeps writing its RCC; the
     per-turn recompute then overwrites it when a validated root stands. Check
     ordering so the inversion runs after the LLM write within a turn.

6. **Docs, design-first, same PR** (`docs/architecture/investigation-engine/
   two-dimensional-hypothesis-methodology.md`): §7.7 gains a short Stage-A
   present-tense paragraph (precedence now inverted behind the flag; Stage B
   still gated on INV-41); §7.6 reconciliation prose gets a one-line pointer.
   INV registry: amend the INV-34/35 rows' notes if their wording asserts
   "LLM conclusion wins" — do not mint a new INV number for a precedence flip.
   Design docs are PRESENT TENSE, one "rejected alternative" line max.
   No campaign labels ("Stage A") in code/tests — code comments describe the
   behavior, the stage name lives only in docs/PR.

## What must NOT change (non-goals)

- LLM RCC authorship (schema `RootCauseConclusionUpdate`, prompts) — Stage B.
- The reconciliation layer: `link_llm_rcc_to_cause`, `retract_disconfirmed_rcc`,
  `conclusion_overclaims` seam, `cause_identification_leg` legs — all stay,
  they now govern the fallback path.
- INV-40 narration guard (#668) — orthogonal, untouched.
- `working_conclusion` leg, terminal gates, confirm-stamp.
- Frontends.

## Verification bar (task is not done without ALL of these)

- **Property tests, not instances** (sweep, LLM-agnostic, mechanical):
  - For ANY case with a standing validated uncontested root and ANY LLM-authored
    RCC (sweep: confidence levels incl. VERIFIED-overclaiming, linked/unlinked,
    `names_root_node_id` set/absent), after the per-turn recompute the surfaced
    `root_cause_conclusion` is the ENGINE mirror (determined_by == engine,
    text == root statement render). Same sweep with flag OFF → LLM RCC survives
    (today's pins).
  - No validated root (sweep: no root / root INCONCLUSIVE / root REFUTED /
    MECE-contested) → LLM RCC stands byte-identical; contested → engine asserts
    nothing (existing pins keep passing).
  - Overclaim seam still fires on a fallback LLM RCC claiming VERIFIED at
    grade NO_ROOT.
  - Metric pin: counter increments exactly on LLM→mirror replacement, not on
    engine-mirror refresh, not on no-op turns.
- **Mutation check** (verify the gate can fail): flip the flag default in a
  scratch run and confirm the property tests actually fail; confirm the
  old "LLM always wins" pin was UPDATED deliberately, not deleted.
- Existing mirror-deference pins that encode "LLM wins": update each one
  deliberately with a comment-free behavioral rewrite; list them in the PR body.
- Full investigation unit suite green (`pytest tests/unit/core/investigation/ -q`
  and whatever superset the repo's CI runs), `lint-imports` 13/13, `black`/ruff
  clean. Run CI's own scopes locally before reporting green.
- #656 acceptance replay untouched and green if it exists in-tree.

## Process

- Branch off latest origin/main: `feat/673-stage-a-chain-authored-conclusion`.
  Repo gotcha: package is nested at `faultmaven/faultmaven/`; other agents may
  switch HEAD under you — verify `git branch` before every commit.
- Conventional Commits; design doc + code in the SAME PR, design-first commit
  order welcome but not required.
- PR body: link #673, state "Stage A of the architect sequencing; Stage B stays
  gated on INV-41", enumerate the updated pins, note the accepted
  expressiveness deltas. Do NOT close #673 (Stage B remains).
- Open the PR non-draft, do not merge.
