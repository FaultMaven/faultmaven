# AUDIT — Runbook Template Fitness + Corrected-Template Spec + Keep-vs-Regenerate

**Status:** Draft for ratification · **Type:** Template-fitness audit + corrected-template design
**Date:** 2026-07-07 · **Scope:** the v4 runbook template as a diagnostic instrument — does it let a
runbook effectively guide the engine to the correct root cause (retrieve → discriminate → instantiate →
ground)? Phase 1 (template) is the deliverable; Phase 2 (corpus keep-vs-regenerate) is a recommendation
**gated on Phase-1 ratification**.

**Method:** read the engine consumers (`runbook_cause_matcher.py`, `indicator_evaluator.py`,
`differential_intake.py`, `intake_evaluation.py`, `causal_graph.py`), the two matching specs, the content
architecture §3, `cause_schemas.py` (the reference `CauseRecord`), all four authoring mirrors, and a
representative + adversarial sample of the 91-runbook corpus and the shipped `pack.json`. Findings are
grounded in code, not docs. Distinguishes **TEMPLATE** gaps (structural — touch the 4 mirrors) from
**CORPUS** defects (authoring — fix by regenerating) from **MATCHER/engine** changes (e.g. predicate
normalization).

---

## 0. Bottom line (read this first)

**The template is fit for *sound* diagnosis. Its *deterministic-discrimination* layer is dead — and
repairing it is a Phase-0-gated enhancement, not a soundness repair.** The *match surface*
(`cause_statement`, symptom-level, MECE) and the *causal-chain topology* (`Chain` → CANDIDATE prior) are
correctly specified, correctly consumed, and already satisfied by the corpus — a retrieved cause reliably
matches the right case and seeds a sound, capped, evidence-gated scaffold. Without any predicate, the engine
still matches, instantiates, and either grounds through the LLM or hands off on insufficient evidence — i.e.
**guarantee 1 (NO INCORRECT CONCLUSION) does not depend on the predicate layer.** What the predicate layer
buys is **guarantee 2 (NO COLLAPSE UNDER PRESSURE)** — a deterministic check on a confident-but-wrong LLM and
targeted discrimination of MECE siblings — plus the auto knowledge-flywheel (`provenance="runbook"` is the
sole non-deductive source of `GROUNDED`). That layer is **structurally present but practically dead**:
predicates are authored against the runbook's *own diagnostic-command output format* (not the raw telemetry
users paste), matched by a *literal, case- and whitespace-sensitive substring* test, and carry **no
first-class counterfactual/ruling-out signal**. It is also **flag-off and its value is Phase-0-unproven**
(the grounding-RCA gate), so the right framing is *dead enhancement*, not *broken diagnosis*.

Three changes fix it: (1) re-orient the predicate contract from step-output tokens to symptom-telemetry
tokens; (2) add an authored exclusion (counterfactual) predicate — `stance:"refutes"` — so "what rules this
cause out" is expressible and MECE has teeth at the validation layer. **Ratified scope (2026-07-07): T2 is
sibling-elimination-only** — an authored `refutes` drives node-state REFUTED, *not* belief→0 — which is sound
(a mis-authored `refutes` degrades to a safe hand-off, never a wrong conclusion). Wiring it into
proof-by-exclusion (the M-D engine change) is **deferred**: it would spend the in-scope no-incorrect-result
guarantee to buy out-of-scope grounding-rate (§3, T2 scope note). (3) normalize predicate matching in the
engine (case + whitespace collapse), paired with the dead-target lint so it doesn't amplify over-broad short
tokens. **Split the work by gate:** the mirror/doc-drift fixes (§2.6) are broken guardrails — do them now,
ungated; the predicate program (T1 / T2 / M-A / M-B + regeneration) is a bigger investment behind the Phase-0
adversarial gate, and belongs **inside the harvest-grounding campaign (Slice 6 / #584)**, not a parallel
template track.

---

## 1. How the engine actually consumes a runbook (verified contract)

A retrieved `Cause` (`CauseRecord`, `cause_schemas.py:43`) does four jobs on **three different surfaces**:

| Job | Surface | Engine mechanism (verified) | Status |
|---|---|---|---|
| **Match** ("does this cause explain the case?") | `cause_statement` (symptom-level) + non-problem chain prose; `cause_name` is subject-only | Holistic per-cause T2 `case_evidence_qa`; condition built by `indicator_evaluator._build_cause_condition` (`indicator_evaluator.py:272`) from `cause_statement` (or chain prose) — **not** from `rung_indicators`. Overlapping siblings → both answer YES → verdict `multiple` → matcher **abstains** (`indicator_evaluator.py:124-131`). | **Live, load-bearing** |
| **Instantiate** (once matched) | `chain_nodes` / `chain_edges` topology | `chain_to_specs` → `ingest_emitted_chain` seeds CANDIDATE nodes, capped prior ≤0.5, never VALIDATED without evidence (`runbook_cause_matcher.py:174-279`, `_MATCHER_MAX_PRIOR=0.5`). | **Live, sound by construction** |
| **Validate** (does a submitted datum support/refute?) | `match_predicates` (content-addressed) | `differential_intake.evaluate_datum_against_differential` (`differential_intake.py:189`) runs each candidate's predicates against the datum's trusted digest under **subset-trust** (`evaluate_predicate_against_text(..., complete=False)`), emitting `SUPPORTS`/`REFUTES` with `provenance="runbook"`. Wired per-turn at `milestone_engine.py:7285`. | **Live but flag-gated OFF + weak (see §2.3)** |
| **Remediate** | `interventions` (quadrant-tagged) | Stashed on the ROOT node, surfaced as `<documented_fixes>` only once `cause_state==IDENTIFIED`, proposed by the LLM through the M5 gate (`runbook_cause_matcher.py:285-303`). | **Live, sound** |

**Two corrections to stale framing** (important, because they change how you read the code):

- The `CauseRecord` docstring calls `match_predicates` **"optional annotations, inert for matching"**
  (`cause_schemas.py:9,55-59`). This is *true for matching and misleading overall*: predicates carry no
  matching weight **by design** (matching is holistic over the Statement — correct), but they are the
  **load-bearing validation surface** in the differential-intake loop. The content-architecture doc already
  supersedes the "inert fast-path" framing (`runbook-content-architecture.md:194-225`); the `cause_schemas.py`
  docstring has not caught up. → **Doc drift to fix (see §3, mirror changes).**
- The predicate-coverage baseline concluded "consumption (wiring) is 0%" (`ANALYSIS-predicate-coverage-baseline.md:31-34`).
  That was true when written; **RC-1 is now fixed in the working tree** — `apply_runbook_cause_matcher` seeds
  the differential from the top-K *retrieved* runbooks regardless of verdict (`runbook_cause_matcher.py:578-615`),
  so the loop runs on `none`/`multiple` too. The bottleneck has moved from *unwired* to *wired-but-flag-off +
  weak-authoring + no-normalization*.

---

## 2. Template-fitness verdict, per dimension

### 2.1 Match surface — `cause_statement` — **FIT**

- **Right key.** The Statement (symptom-level) is exactly what the holistic matcher judges on, and
  `cause_name` alone is correctly insufficient (`_build_cause_condition` returns `None` on a bare title,
  `indicator_evaluator.py:291-294`). Copying it into `RootCauseConclusion.root_cause` with no LLM extraction
  is the right minimal seam.
- **MECE required by the mechanism, not just by hygiene.** Two overlapping sibling Statements produce verdict
  `multiple` → the matcher instantiates nothing. So "MECE with teeth" is load-bearing, and the template says
  so (`runbook-content-architecture.md:238-243`).
- **Corpus satisfies it.** 640/640 causes carry a Statement; sample Statements are symptom/mechanism-level
  prose, and siblings are MECE at the Statement layer (pg-slow-queries A–F, redis-oom A–H, nginx-high-latency
  A–H are cleanly separable).
- **Enforcement (RE-VERIFIED on `origin/main` — CORRECTS an earlier stale-checkout claim).** The Statement
  invariants that give MECE teeth — block `[Step N]` in a Statement, block empty non-fallback Statement, block
  exact-duplicate siblings, warn on Jaccard≥0.6 near-dupes — live in `check_cause_statement_invariants`, and the
  module comment says it is "mirrored BYTE-IDENTICAL" into `kb_toolkit/core/validator.py`. **An earlier draft of
  this audit reported that claim as stale** (that the toolkit lacked the block) — **that was wrong**: it came
  from an exploration agent reading a kb-toolkit checkout 14 commits behind `origin/main`. Re-verified on
  `origin/main`: `kb_toolkit/core/validator.py:107-192` carries the block, `diff` against the backend's copy is
  **empty (byte-identical)**, and the toolkit calls it (`_validate_causes`, ~line 531). So the *generation path*
  **does** enforce MECE, and the mirror claim is accurate. (Independently, the ≤300 Statement limit and the
  per-cause required-subfield **ERROR** are enforced in the toolkit but NOT the backend — a real backend gap,
  see §2.6.) → **Verdict: the match surface is well-specified and MECE-enforced on both mirrors; the residual
  drift is backend-only (≤300 + per-cause ERROR parity).**

### 2.2 Causal chain — `chain_nodes` / `chain_edges` — **FIT**

- Root→…→D, one ROOT per cause (M1), OR-siblings as mutually-exclusive alternatives, `Chain` optional/tolerant
  (degenerate `root→D`). Instantiates cleanly through the engine's own node-identity machinery (dedup, `cn_`
  render-back, edges), inheriting the never-VALIDATED-without-evidence + failed-fix-demotes guarantees with no
  matcher-specific bypass.
- Corpus: all 549 non-fallback causes carry a chain (typical 3–4 nodes). No structural issues.
- → **Verdict: sound and satisfied. Preserve as-is.**

### 2.3 Discriminators — `rung_indicators` / `match_predicates` — **PARTIAL FIT (the central gap)**

The code's treatment ("optional / inert for matching") is **correct for matching** — predicates *should not*
carry matching weight; that keeps matching a holistic semantic judgment and avoids brittle token-gating. But
as a **validation** instrument the predicate layer is unfit in three compounding ways:

1. **Authored against the wrong surface (TEMPLATE + CORPUS).** The template co-locates predicates under
   `**Indicators:**` bullets anchored to `[Step N]`, which frames the author to write the token *as their
   diagnostic command emits it*. The corpus did exactly that, systematically: `"reason=Error"` /
   `"exitCode=0  reason=Completed"` (the runbook's own jsonpath format, incl. a **double space**), `"MemoryPressure       True"` /
   `"Sealed          true"` (column-aligned `kubectl`/`vault status`), `'"response_flags":"UF"'` (JSON-quoted
   when default Envoy logs emit `UF` bare), `"maxmemory_human:0B"` (raw `redis-cli INFO` serialization). Real
   uploaded telemetry (`kubectl describe` → `Reason: Error`, colon+space, capitalized) does **not** contain
   these tokens.
2. **Matched by a literal substring (MATCHER).** `contains`/`absent` evaluate `target in text` —
   case-sensitive, whitespace-sensitive, no normalization (`indicator_evaluator.py:370,378`). This is the RC-2
   "dead-predicate" class. Combined with (1), a large share of the 580 `contains` predicates cannot fire even
   when the user pastes the right artifact, purely on formatting.
3. **No teeth at the validation layer (TEMPLATE + CORPUS).** 18/91 runbooks have ≥1 *identical*
   `(predicate, step, target)` shared across sibling causes (e.g. java-jvm-oom A&B both fire on
   `contains "Java heap space"`; route53 A/C/D all on `SERVFAIL`; k8s-rbac A&B on the two-letter token `"no"`),
   so a firing predicate cannot discriminate the siblings it is meant to split. ~11 genuine discriminating
   causes carry no predicate at all.

The *shape* is right (content-addressed, deterministic, subset-trust-sound, provenance-labeled). The *contract
and the matcher* are what make it dead. → **Verdict: keep the mechanism; fix the authoring contract (template),
the matcher (normalization), and regenerate the targets (corpus).**

### 2.4 Absence / counterfactual signal — **MISSING (genuine template gap)**

The task's hypothesis is confirmed. **No mirror carries a per-cause "what evidence rules this cause OUT"
field** (confirmed across all six schema sites). The only exclusion mechanism is the `absent` predicate op (56
of 756, mostly *config*-absence like `absent "keepalive"`, not *symptom*-exclusion), and under subset-trust an
`absent` predicate can only yield `refuted`/`untested`, never `SUPPORTS` — so 38 causes are `absent`-only and
cannot ground positively. Counterfactual reasoning appears as incidental prose in exactly two files, never as
a machine-readable field.

Why this matters concretely — **and one correction to be precise about how far an authored refute reaches**
(verified in `derive_node_states`, `causal_graph.py:231-350`). The engine has two distinct REFUTES effects:

- **Node-state REFUTED** fires on `counterfactual_refutes >= 1` **OR** `refutes > supports` (line 324). A
  runbook `refutes` predicate produces an ordinary REFUTES link, so it *can* net-refute a sibling to
  node_state REFUTED — **this is real MECE-teeth value at the node-state layer: it eliminates a competitor and
  keeps the differential honest.**
- **Absolute exclusion (belief→0)** — the thing proof-by-exclusion (`deductively_validated`,
  `causal_graph.py:161`) actually counts — fires **only** on `counterfactual_refutes >= 1` (line 349), and
  `counterfactual_refutes` counts **only** `CAUSAL_ABSENCE_EVIDENCE`-backed REFUTES (lines 246-250). There is
  **no runbook-provenance exception on the REFUTES side**, unlike the SUPPORTS side, which *does* count
  `runbook`-provenance as causal grounding (line 242, #590 A2).

**So an authored `refutes` predicate does NOT, by itself, un-starve the deductive arm** — it drives node-state
REFUTED but leaves belief above the exclusion threshold, so proof-by-exclusion never counts it. Delivering the
exclusion value requires a *paired engine change* (M-D, §3): a symmetric runbook-provenance exception on the
REFUTES/counterfactual side, mirroring line 242. That change is worth considering (models **under-emit** the
absence grading, starving this path), **but it opens a genuine new-unsoundness surface** — see T2's scope note
in §3. → **Verdict: add a first-class, author-able counterfactual (T2), scoped to
sibling-elimination (node-state REFUTED). This is the ratified baseline — it is sound (mis-authoring degrades
to a safe hand-off). The deductive-exclusion wiring (M-D) is deferred: it would trade the in-scope
no-incorrect-result guarantee for out-of-scope grounding-rate, and is gated on an exclusion-specific
adversarial eval (§3, M-D).**

### 2.5 Dead vs missing elements

**Dead (inert in evidence-only FaultMaven):**
- **T1 step-addressed predicate tier** — `IndicatorEvaluator._evaluate_predicate` resolves a `[Step N]`
  predicate against that step's output, but `step_output_resolver` is always `None`
  (`milestone_engine.py`), because FM never executes runbook steps. The entire `[Step N]`↔`step`-key
  apparatus is provenance-only for matching/validation (the content-addressed tier ignores `step`).
- **`rung_indicators` (the parsed dict on `CauseRecord`)** — consumed only by the inert T1 tier; the live
  validation tier reads `match_predicates` directly (`differential_intake.py:223`) and the live match tier
  reads the Statement. The `**Indicators:**` *markdown field* is still useful as the *authoring vehicle* for
  the `<!-- match -->` hints (the chunker lifts them), but the indicator prose itself is not consumed.
- **Cross-cause `converges:`** — documented but dropped at instantiation (`runbook_cause_matcher.py:253-265`).

**Missing (diagnosis needs it, template can't express it):**
- A per-cause counterfactual/exclusion signal (§2.4).
- A symptom-vs-step surface distinction for predicate targets (§2.3).
- `data_type` predicate scoping — planned (#583/#584), 0/756 today; out of scope here.

### 2.6 Mirror consistency — **DRIFTED (structural)**

| Drift | Reference / correct | Where it's wrong | Class |
|---|---|---|---|
| Human fill-in template is **still v3** | v4 grammar (`Statement`/`Chain`/`Indicators`/`Interventions`) | `docs/operations/runbooks/template.md` uses v3 `Statement`/`Mechanism`/`Indicator`/`Mitigation`/`Resolution`/`Verification`; kb-toolkit `TEMPLATE.md:8-9` **points authors at it** | Dead doc → authors produce v4-invalid runbooks |
| Backend has **no predicate concept**; conversion prompt never emits `<!-- match -->` | predicates are the validation surface | `conversion_service.py` `CONVERSION_SYSTEM_PROMPT` — LLM-converted runbooks carry **zero** predicates | Whole validation layer unreachable via conversion path |
| Statement ≤300 enforced only in toolkit | both should enforce | backend `runbook_validator.py` has no `cause_statement_max_chars` | Divergent gate |
| ~~MECE Statement invariants only in backend~~ | *(RETRACTED)* | *(RETRACTED — a stale-checkout artifact; `origin/main` `kb_toolkit/core/validator.py:107-192` has the byte-identical block and calls it. The mirror is real.)* | *No drift* |
| Required sub-fields: **ERROR (per-cause)** vs **WARNING (document-level)** | per-cause ERROR | backend `runbook_validator.py:453-459` only checks a field appears *somewhere* in the file | A cause missing `Interventions` passes the backend |
| Chunker field names (`cause_chain`/`cause_indicators`, raw) vs pack (`chain_nodes`/`chain_edges`/`rung_indicators`, parsed) | one vocabulary | chunker metadata layer vs pack_builder | Naming/shape divergence (arguably by-layer, but undocumented) |
| `create_runbook_from_template` enforces almost nothing | full grammar | `conversion_service.py:1616-1651` is a 6-H2 skeleton only | Weak manual path |

Aligned where it counts: `pack_builder._extract_causes` emits **exactly** the 9 `CauseRecord` fields; the 6
required H2 sections and the quadrant/predicate/indicator vocabularies agree wherever each mirror expresses
them.

---

## 3. Corrected-template spec (diffs vs current v4 + per-mirror change list)

**Design stance:** additive and minimal. The match surface and chain are correct — do not touch them. Every
change below either (a) re-points the predicate contract at the surface the engine actually validates against,
(b) adds the missing counterfactual, or (c) closes a mirror drift. All predicate changes are **backward-shaped**
(default = current behavior) so they do not, by themselves, invalidate the corpus structure.

### Change T1 — Predicate `target` contract: symptom-telemetry surface, `step` demoted to optional provenance

**Diff (authoring contract + validator, not the `CauseRecord` field set):**

```diff
  <!-- match -->  predicate JSON:
- {"step": N, "predicate": "contains", "target": "<token as the diagnostic command prints it>"}
+ {"predicate": "contains", "target": "<verbatim token as it appears in RAW uploaded telemetry>",
+  "step": N   // OPTIONAL, provenance only — which step would surface this; never an eval key
+ }
```

- **Authoring rule (new, enforced as a warning):** `target` must be a token that appears **verbatim in raw
  telemetry a user pastes** (a log line, a config directive, a metric `field:value`), **not** as a diagnostic
  command formats it. Explicitly forbid: internal column alignment / multiple consecutive spaces, tool-specific
  JSON re-quoting of text-format logs, and `field=value` shapes that only the runbook's own `jsonpath`/`awk`
  produces.
- **Validator lint (cheap, deterministic):** warn when a `contains`/`absent` `target` contains a run of ≥2
  spaces, or is ≤3 chars / a stop-word (`"no"`, `"0"`, `"path"`) — these are the observed dead/over-broad
  shapes. This does not prove symptom-level-ness (semantic), but catches the mechanical RC-2 artifacts.
- `step` becomes **optional** in the grammar (it is already ignored by the content-addressed evaluator). Keep
  it only as provenance/agenda.

### Change T2 — First-class counterfactual: `stance` on a predicate (the ruling-out signal)

**Diff (predicate JSON schema — additive):**

```diff
  {"predicate": "contains"|"absent"|"exit_code"|"threshold", "target": ..., "op"?: ..., "value"?: ...,
+  "stance": "supports" | "refutes"   // OPTIONAL, default "supports"
  }
```

- Semantics: a `stance:"refutes"` predicate that **fires** yields `REFUTES` (rules the cause out); a
  `stance:"supports"` (default) that fires yields `SUPPORTS` — i.e. the author can now write a *positive-token*
  predicate whose presence **eliminates** the cause (e.g. on the JVM-heap-OOM cause:
  `{"predicate":"contains","target":"medium: Memory","stance":"refutes"}` — a tmpfs signature rules out the JVM
  cause). Today the only way to express exclusion is an `absent` predicate, which under subset-trust never
  produces a positive refute from presence and conflates "config missing" with "cause excluded."
- **Authoring discipline (new, warning-enforced):** each *discriminating* (non-fallback) cause SHOULD carry
  ≥1 predicate that discriminates it from its MECE siblings — either a `supports` predicate on a token unique
  to this cause, or a `refutes` predicate on a sibling's signature token. This is what gives §2.1's MECE "teeth"
  at the *validation* layer (the Statement gives it teeth at the *match* layer).

- **RATIFIED SCOPE (2026-07-07): T2 is sibling-elimination-only. M-D is deferred.** An authored `refutes`
  predicate drives the sibling to `node_state=REFUTED` (via `refutes > supports`) and **stops there** — it does
  **not** drive belief→0 and therefore does **not** feed proof-by-exclusion. This is the baseline spec to write.
- **Why this is the sound scope (the asymmetry is decisive).** Subset-trust (`complete=False`) guarantees the
  *token is really present*, but not *authoring-correctness* (that the token's presence truly rules the cause
  out — a semantic judgment, like the match surface, and so **not** mechanically validatable). The two variants
  fail in opposite directions:
  - **T2 alone (REFUTED only):** a mis-authored `refutes` wrongly eliminates the *right* cause → the case can't
    ground it → **hands off** (or grounds a different cause only via that cause's own real evidence — a surface
    T2 never touches). Worst case = an **under-claim / missed grounding = safe** (no incorrect result, no
    collapse). Not doing M-D creates **no soundness gap**: the case a runbook can't deductively ground just
    hands off, which is the correct outcome; the deductive arm firing less often is precisely the grounding-rate
    variation that is **out of scope** for the completion criterion.
  - **T2 + M-D:** the same mis-authored `refutes` drives belief→0 → proof-by-exclusion → the survivor validates
    `DEDUCTIVE` because its siblings were excluded → **a wrong VALIDATED root**, the one failure the system
    exists to never produce. Guard #4 keeps it out of RESOLVED/harvest, but the wrong-grounded cause still
    surfaces mid-investigation (GROUNDED/IDENTIFIED) and misdirects treatment until the counterfactual fails and
    R6 re-expands. So M-D spends **in-scope guarantee-1** (no incorrect result) to buy **out-of-scope
    grounding-rate**. Wrong direction. **Deferred** (see M-D in the companion list for its gating precondition).

*(With this scope, T2 is effectively a **one-part change** — the template `stance` key **plus M-A** (the
evaluator honoring `stance`; default `supports` preserves current behavior). No M-D. The `refutes` authoring
discipline is still tighter than `supports` — a wrong `refutes` mis-eliminates a competitor — but a wrong
`refutes` can no longer mis-ground by exclusion, because the exclusion path is not wired.)*

### Change T3 — Documentation-only clarity: state `Indicators`' real role

No field rename (that would churn the corpus and mirrors for no engine gain). Instead, correct the template
prose: `**Indicators:**` is the **authoring vehicle for `<!-- match -->` predicates** (the validation surface)
plus optional human notes — it is **not** the match surface and its prose is not consumed for matching. Update
the `cause_schemas.py` docstring likewise: drop "inert for matching" as the headline; state predicates carry
no matching weight but are the load-bearing **validation** surface. This kills the recurring misconception
that indicator prose drives matching.

### Per-mirror change list (spec only — do **not** implement in this pass)

1. **`cause_schemas.py` (reference):** update the `match_predicates` docstring (`:9,55-59,93-97`) — remove
   "inert for matching" as the headline; state "no matching weight; the load-bearing **validation** surface via
   differential-intake." Add optional `stance` to the field's documented predicate shape. No field-set change
   unless `stance` is modeled explicitly (it rides inside the predicate dict, so no new top-level field needed).
2. **`kb_toolkit/core/runbook_grammar.py` + `config.py` + `validator.py` (mirror 1):** (a) make `step`
   optional in the predicate grammar; (b) add the T1 target lint (≥2-space run, ≤3-char/stop-word) and the T2
   `stance` key to the predicate vocab; (c) add the T2 "≥1 discriminating predicate per non-fallback cause"
   warning; (d) ~~port the MECE Statement invariants~~ **— NOT NEEDED (RETRACTED):** re-verified on `origin/main`,
   `kb_toolkit/core/validator.py:107-192` already carries the byte-identical `check_cause_statement_invariants`
   and calls it, so the generation path already enforces MECE; (e) the Statement ≤300 check is already present
   here — the parity gap is backend-only (mirror 3).
3. **`kb_toolkit/core/pack_builder.py` (mirror 2):** carry `stance` through `_extract_causes` into the emitted
   `match_predicates` (it already passes predicate bodies through verbatim — verify `stance` survives). No
   field-name change.
4. **`modules/knowledge/domain/services/runbook_validator.py` (mirror 3):** (a) add Statement ≤300 enforcement
   (parity with toolkit); (b) upgrade required-sub-field checks from document-level WARNING to **per-cause
   ERROR** (parity); (c) add awareness of `<!-- match -->` predicates + the `stance` key + the T1 target lint so
   the API conversion/validation path does not silently accept predicate-dead or dead-target runbooks.
5. **`modules/knowledge/domain/services/conversion_service.py` (mirror 4):** (a) `CONVERSION_SYSTEM_PROMPT` —
   add instructions to emit `<!-- match -->` hints with symptom-telemetry targets and (where applicable)
   `stance:"refutes"` exclusion predicates, **or** an explicit decision that conversion produces
   Statement+Chain+Interventions only and predicates are a toolkit-only enrichment pass (pick one; today it
   silently produces predicate-dead runbooks); (b) `create_runbook_from_template` — enforce the per-cause grammar
   it currently only docstrings.
6. **`docs/operations/runbooks/template.md` (human fill-in — the 5th, undocumented mirror):** **replace the v3
   body with the v4 grammar**, or delete it and re-point `kb_toolkit/docs/TEMPLATE.md:8-9` at the canonical
   `runbook-content-architecture.md §3`. This is the highest-severity mirror fix: an author following the
   pointed-to template today produces a v4-invalid runbook.
7. **`runbook-content-architecture.md §3` (canonical spec):** document the symptom-vs-step target contract (T1),
   the `stance` counterfactual (T2), and the Indicators-role clarification (T3); add them to the Template
   Compliance Rules and Gate-2 lists.

### Companion engine changes (NOT template; sequenced with the corpus regen)

- **M-A — honor `stance`** in `_aggregate_predicates` / `_VERDICT_STANCE` (`differential_intake.py:91-94,240-270`):
  a *firing* (`matched`) predicate with `stance=="refutes"` maps to `REFUTES` (not SUPPORTS); default
  `supports` = current mapping. Tiny, additive — but **REQUIRED for T2** (without it a `stance:"refutes"`
  predicate would wrongly SUPPORT the cause it is meant to rule out). Scope is sibling-elimination only: the
  REFUTES link is an ordinary typed link; do **not** route it to `counterfactual_refutes`/`belief→0` (that is
  M-D).
- **M-B — predicate normalization (fixes RC-2 mechanically)** in `evaluate_predicate_against_text`
  (`indicator_evaluator.py:336-407`): before `target in text`, normalize both sides — case-fold + collapse runs
  of whitespace to a single space (kills the double-space/column-alignment artifacts) for `contains`/`absent`;
  leave `exit_code`/`threshold` numeric parsing untouched. **Bounded to case + whitespace only** — no fuzzy/
  semantic matching, so determinism and NO-INCORRECT-CONCLUSION are preserved. This is the single highest-leverage
  change: it revives a large share of the existing corpus targets without re-authoring, and makes newly-authored
  symptom-surface targets robust to formatting. **Pair it with the T1 dead-target lint** (≤3-char/stop-word
  warning) so normalization — which makes matching *more* permissive — does not amplify over-broad short tokens
  (`"no"`, `"0"`) into spurious fires.
- **M-D — runbook-provenance exception on the counterfactual side — DEFERRED (documented, gated future
  option; NOT in the baseline).** Would make an authored `refutes` feed proof-by-exclusion by counting a
  `runbook`-provenance REFUTES toward `counterfactual_refutes` in `_node_evidence_tally`
  (`causal_graph.py:231-251`), mirroring the SUPPORTS-side rule at line 242. **Ratified out of the baseline
  (2026-07-07):** it spends in-scope guarantee-1 to buy out-of-scope grounding-rate and opens the
  wrong-exclusion→wrong-`DEDUCTIVE` surface (§3, T2 scope note). **Hard precondition to revisit:** Phase-0 must
  first prove the deductive/collapse-protection value is real, **and** there must be an *exclusion-specific
  adversarial eval* demonstrating that a wrong `refutes` predicate cannot produce a wrong VALIDATED root (or is
  caught) — because `refutes`-authoring correctness is a **semantic** judgment and cannot be mechanically
  lint-validated, the gate must be an adversarial eval, not a validator rule. Confirm the #593 guard-#4 backstop
  under that eval. Until both exist, M-D stays deferred. #593 / deductive-arm turf — never land from the
  template track.
- **M-C (already done in tree):** RC-1 differential seeding from retrieval — note as complete
  (`runbook_cause_matcher.py:578-615`); the doc `runbook-cause-matching.md:192-198` "Current limitation" and the
  impl-doc should be updated to reflect it.

---

## 4. Phase 2 — Corpus keep-vs-regenerate (recommendation; gated on §3 ratification)

**Binary answer: the corpus is STRUCTURALLY consistent but SEMANTICALLY inconsistent with the corrected
template — regenerate the validation layer.**

- **Consistent (keep) — the match + topology + remediation layers.** Statement present and symptom-level on
  640/640 causes; MECE at the Statement layer in the sampled clean runbooks; `Chain` on all 549 non-fallback
  causes; exactly one `[Default]` fallback per runbook; quadrant-tagged interventions; no `data_type` keys. None
  of these change under §3. This is genuinely good work and should be preserved.
- **Inconsistent (regenerate) — the predicate/validation layer.** The corrected contract (T1 symptom-surface
  targets, T2 `stance` counterfactuals, the "≥1 discriminating predicate per cause" discipline) is **not**
  satisfied by any of the 756 existing predicates: they are step-output-shaped (T1 miss), 18 runbooks carry
  non-discriminating identical sibling predicates, ~11 discriminating causes have none, and 0 carry an authored
  exclusion. These defects are **pervasive across all 7 domains**, not isolated — a per-file hand-patch of 756
  predicates is precisely the hand-patching the pre-production "clean baseline" discipline rejects.

**Recommendation:** **regenerate against the corrected template**, with the *minimal regeneration unit = the
predicate/validation layer + the counterfactuals*, keyed off the existing (good) Statements and Chains:

1. **Preferred (scoped) path — predicate-only regeneration pass.** Re-run the kb-toolkit generation for the
   `<!-- match -->` layer only: for each existing cause, author symptom-surface `supports` + `refutes` predicates
   against the same authoritative sources, keeping the ratified Statement/Chain/Interventions. Then re-run the
   MECE-teeth check (the ~18-runbook collision audit) and backfill the ~11 uncovered causes. Rebuild the pack
   (`kb-build-pack`). This preserves the load-bearing match/topology work and touches only the dead layer.
2. **Fallback (full) path.** If the pipeline cannot cleanly regenerate predicates in isolation from Statements,
   full-regenerate all 91 runbooks from source via kb-init / kb-researcher against the corrected template. The
   Statements/Chains will come out materially similar (they are already right); the delta is the validation layer.

**Effort estimate.** The pipeline is v4 and automated, so this is bounded:
- Predicate-only pass: one automated authoring run over 91 runbooks (LLM-per-cause predicate authoring) +
  validator/MECE-collision audit on ~18 flagged runbooks + ~11 targeted backfills + pack rebuild. Order **~1–2
  days** of mostly-automated pipeline time + review.
- Full regeneration: larger authoring surface (all sections, 91 runbooks) but the same automated pipeline —
  order **a few days** + review. Prefer the scoped path unless tooling forces the full one.

**Sequencing — split the work by gate (this is not one bundle).**

- **Do now, ungated (broken guardrails, pure correctness — no predicate-strategy decision):** the §2.6 mirror/
  doc-drift fixes — above all **replace the v3 human fill-in template** that `TEMPLATE.md` still points authors
  at (following it today yields a v4-invalid runbook), the **per-cause-ERROR-vs-document-WARNING** gap, and the
  **conversion path emitting zero predicates**. These repair guardrails regardless of what happens to the
  predicate program.
- **Phase-0-gated (bigger investment, unproven value):** T1 + T2 (sibling-elimination) / M-A + M-B + the
  validation-layer regeneration. Regeneration is only worth spending after: (a) §3 is ratified (the predicate
  contract must be final before authoring against it), and (b) **M-B (normalization) lands** — else even
  correctly-authored symptom-surface targets stay formatting-fragile. All of it sits behind the grounding-RCA
  **Phase-0 gate** (`ANALYSIS-runbook-grounding-pipeline.md §7`): the layer is flag-off and its
  collapse-protection value is Phase-0-unproven (recall Phase-0's own finding — a careful model does **not**
  collapse on easy cases; the value is real but **narrow**, on adversarial/confident-wrong cases), so prove it
  on one adversarial acceptance case **before** authoring 756 predicates. Correct order: **ship the ungated
  mirror fixes → ratify §3 → land M-A/M-B → Phase-0 adversarial catch → regenerate the validation layer → flip
  the flag.**
- **Separately gated, NOT in the above bundle:** **M-D** (wire `refutes` into proof-by-exclusion). Revisit only
  if Phase-0 proves the deductive/collapse-protection value *and* an exclusion-specific adversarial eval exists
  showing a wrong `refutes` cannot produce a wrong VALIDATED root (§3, M-D). Its precondition is a superset of
  the baseline's, and it is #593 turf — do not fold it into the template-program schedule.

**Coordinate — this is not a standalone template exercise.** The predicate-authoring work *is* the
harvest-grounding campaign's **Slice 6 / #584** (predicate authoring) plus its Phase-0 gate; the runbook
matcher is one of the two grounding arms feeding the §7 `GROUNDED` gate, and fixing predicates completes that
arm's authoring side. **Run the predicate program (T1/T2/M-A/M-B + regeneration) inside that campaign, with its
owner — not as a parallel template track — or T1/T2/M-B land twice or diverge.** M-D specifically is #593 /
deductive-arm turf.

**Confirmation of tracked defects (not re-discovery).** The RC-2 "literal-substring / no-normalization
dead-predicate" class is confirmed and split cleanly by bucket: the **literal `target in text`** half is a
MATCHER defect (M-B); the **step-output-shaped targets** half is a CORPUS/TEMPLATE defect (T1 + regeneration).
RC-1 is confirmed **fixed** in-tree (M-C). The `absent`-only-cannot-ground finding (38 causes) is subsumed by
T2 (authored `refutes` predicates give exclusion a positive-firing path). New (not previously tracked) findings:
the **v3 human fill-in template** and the **backend predicate-blindness of the conversion path**.

**RETRACTED finding (stale-checkout artifact).** An earlier draft listed a third new finding — a "stale
byte-identical MECE mirror claim" (that the generation path did not enforce MECE). **That was wrong**: the
exploration agent read a kb-toolkit checkout 14 commits behind `origin/main`. Re-verified on `origin/main`, the
MECE block is present and byte-identical in `kb_toolkit/core/validator.py:107-192`, so the mirror is real and
the generation path DOES enforce MECE. **Lesson:** the kb-toolkit shared checkout is routinely stale; all
kb-toolkit-side findings in this audit (chunker field-naming, pack_builder field set, etc.) were read from that
same stale checkout and should be re-confirmed on `origin/main` before anyone acts on them. The corpus sample
(§4) was likewise read from the stale checkout — re-confirm the exact predicate/collision counts on
`origin/main` before Phase 2.

---

## 5. Bottom line (does the template let a runbook effectively guide diagnosis?)

**Yes for sound diagnosis; not yet for deterministic discrimination.** As it stands the template reliably
guides *retrieval → match → chain instantiation → remediation* — the match surface (`cause_statement`,
symptom-level, MECE) and the chain topology are correctly specified, correctly consumed, and already satisfied
by the corpus, and they carry the engine's **NO-INCORRECT-CONCLUSION** guarantee by construction (with or
without any predicate). What the template does **not** yet let a runbook do is *deterministically discriminate
siblings or ground/exclude a cause against telemetry* — the **NO-COLLAPSE-UNDER-PRESSURE** guarantee and the
auto knowledge-flywheel: the predicate/validation surface is present in shape but dead in practice (authored
against diagnostic-command output instead of raw telemetry, matched by a literal case/whitespace-sensitive
substring), and it has **no way to author the ruling-out signal** the exclusion path is starving for. This is a
*dead enhancement*, not broken diagnosis — and its value is Phase-0-unproven, so it should be earned before it
is bought. The changes that complete it — **(1)** re-point the predicate `target` contract at the
symptom-telemetry surface and demote `step`; **(2)** add a first-class `stance:"refutes"` counterfactual plus a
"≥1 discriminating predicate per cause" discipline — **scoped to sibling-elimination** (node-state REFUTED),
which is sound because a mis-authored `refutes` degrades to a safe hand-off; the deductive-exclusion wiring
(M-D) is **deferred** as it would trade the in-scope no-incorrect-result guarantee for out-of-scope
grounding-rate; **(3)** normalize predicate matching (case + whitespace) with the dead-target lint —
are all small, additive, and mirror-consistent. **Do the mirror/doc-drift fixes now (ungated guardrail
repairs); run the predicate program inside the harvest-grounding campaign (Slice 6 / #584) behind its Phase-0
gate.** With that gate passed, the corpus's validation layer should be regenerated (not hand-patched) against
the corrected contract; its match/topology layer can be kept.
