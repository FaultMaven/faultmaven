# DESIGN — Step 1b predicate contract (implementation-ready)

**Status:** Design-first for ratification · **Date:** 2026-07-07 · **Source:** `AUDIT-runbook-template.md` §3
(T1/T2/T3 + companion M-A/M-B) and `PLAN-runbook-template-execution.md` 1b. This pins the exact rules the audit
spec left at diff level so implementation is unambiguous. **Scope:** land the predicate *contract* + the two
engine changes (M-A/M-B). **Not in scope:** M-D (deferred), and the Step-2 corpus regeneration (Phase-0-gated).
**Ratified (2026-07-07): T2 is sibling-elimination-only; M-D deferred.**

Branch `feat/runbook-predicate-contract-1b`, stacked on the 1a branch (has 1a's validator helpers). The
Phase-0 adversarial gate sits **after** this contract lands, **before** regeneration.

---

## Exact rules

### T3 — documentation (no behavior)
- `cause_schemas.py` `CauseRecord` docstring: drop "optional annotations, **inert for matching**" as the
  headline framing of `match_predicates`. State: *predicates carry **no matching weight** (matching is holistic
  over the symptom-level `cause_statement`), but they are the **load-bearing validation surface** consumed by
  the content-addressed differential-intake loop.* Add the optional `stance` key to the documented predicate
  shape.
- `runbook-content-architecture.md` §3: one clarification that `**Indicators:**` is the **authoring vehicle**
  for `<!-- match -->` predicates (the validation surface), **not** the match surface, and its prose is not
  consumed for matching.

### M-B — predicate normalization (engine; `indicator_evaluator.evaluate_predicate_against_text`)
- For `contains` and `absent` ONLY: before the `target in text` test, normalize **both** sides with
  `_normalize_predicate_text(s) = re.sub(r"[ \t]+", " ", s).strip().casefold()`. This case-folds and collapses
  **horizontal** whitespace runs (the double-space / column-alignment artifacts) to a single space.
  **Newlines are preserved as boundaries** (`[ \t]+`, not `\s+`): collapsing line breaks would let a multi-word
  target match two tokens on adjacent, unrelated lines → a false SUPPORTS on a phrase that never occurred.
- `exit_code` / `threshold` are numeric-parse — **unchanged** (normalization does not apply).
- Applies on BOTH tiers (`complete=True` step-addressed and `complete=False` content-addressed) since it is the
  same function — the `complete` subset-trust logic is untouched (a normalized miss under `complete=False` is
  still `untested`, never a false refute).
- **Soundness:** bounded to case + whitespace. No fuzzy/semantic matching → determinism preserved. Making the
  match more permissive cannot manufacture an *incorrect conclusion*: a runbook `SUPPORTS` link is a capped
  prior (≤0.5), gated by M5 + never-VALIDATED-without-evidence; a `REFUTES` still fires only on a decisive
  present-token contradiction (now whitespace/case-insensitive, which is *more* correct, not less).

### T1 — target contract + `step` optional + dead-target lint (kb-toolkit validator `_validate_match_hints`)
- Make `step` **optional** in the predicate grammar (already ignored by the content-addressed evaluator; keep
  as provenance). A predicate with no `step` is valid.
- **Dead-target lint (WARNING, not blocking)** on a `contains`/`absent` `target`:
  - contains a run of **≥2 consecutive spaces** (a diagnostic-command column/format artifact), OR
  - is **≤3 characters** after strip, OR
  - is a **stop-word** from a small controlled set: `{"no", "yes", "0", "1", "true", "false", "path", "name",
    "type", "error", "none"}`.
  - Message names the cause + target + reason ("looks command-output-shaped / over-broad; author a
    symptom-telemetry token that appears verbatim in raw uploads").
- **Discrimination discipline (WARNING):** a non-fallback cause with **zero** `match_predicates` → warn
  ("no discriminating predicate; validation falls back to the LLM tier"). *(Do NOT hard-block — a Statement-only
  cause still matches; predicates are the sound *validation* tier, not required for correctness.)*

### T2 — `stance` counterfactual (grammar + M-A engine)
- **Grammar/schema:** predicate JSON gains an OPTIONAL `stance: "supports" | "refutes"`, default `"supports"`.
  Validator (`_validate_match_hints`): if `stance` present and not in the set → **ERROR** (parity with the
  strict-JSON predicate-name check). `pack_builder` + chunker carry `stance` through verbatim (they already
  pass predicate bodies through — add a passthrough test).
- **M-A (engine; `differential_intake._aggregate_predicates`):** replace the fixed
  `_VERDICT_STANCE = {"matched": SUPPORTS, "refuted": REFUTES}` with a **stance-aware** resolver:

  | predicate verdict | `stance="supports"` (default) | `stance="refutes"` |
  |---|---|---|
  | `matched` (condition holds) | **SUPPORTS** | **REFUTES** |
  | `refuted` (condition contradicted) | **REFUTES** | **— (silent)** |
  | `untested` | — (silent) | — (silent) |

  A `refutes` predicate is a **disqualifier**: it eliminates the cause only when it FIRES (`matched`) and is
  silent otherwise — it **never yields SUPPORTS** (that would be a double-negative footgun, and on the content
  tier `absent`+`refutes` can only ever produce a `refuted` verdict → it would wrongly SUPPORT on presence). An
  absent `stance` key defaults to `"supports"` and reproduces today's mapping exactly (no existing predicate
  changes meaning). **An unknown/invalid `stance` value (typo `"refute"`, `null`, …) → silent (no verdict)** —
  never guess the belief-raising direction.
- **Sibling-elimination ONLY (the ratified scope):** the produced `REFUTES` is an **ordinary** typed evidence
  link (`provenance="runbook"`). It drives `node_state=REFUTED` via `refutes > supports` in `derive_node_states`
  — a competitor is eliminated. It is **NOT** routed to `counterfactual_refutes` / `belief→0` (that is **M-D**,
  deferred). Do not touch `_node_evidence_tally`.
- **Authoring discipline:** `refutes` predicates must be **stricter** than `supports` — restrict to unambiguous
  mutually-exclusive signatures (a token that categorically belongs to a *sibling*). A wrong `refutes` only
  mis-eliminates a competitor (safe under-claim) because M-D is not wired — but keep the bar high.

### M-C — doc de-stale (no code)
- `runbook-cause-matching.md:192-198` "Current limitation (2026-07-01)" + the impl-doc: RC-1 is fixed in tree
  (differential seeded from retrieval); note it. Add the `stance` predicate + normalization to §3 vocab.

---

## Ordered implementation (each its own reviewable commit; ★ = matcher-owned engine)

1. **T3 docs** — `cause_schemas.py` docstring + §3 Indicators-role. (fm; no behavior.)
2. **★ M-B normalization** — `indicator_evaluator.py` + tests (dead-target revival). (fm engine.)
3. **T1 lint + `step`-optional** — kb-toolkit `validator.py` `_validate_match_hints` + config grammar + tests.
4. **T2 grammar** — kb-toolkit predicate vocab (`stance` key) + `pack_builder`/chunker passthrough + tests.
5. **★ M-A honor-stance** — `differential_intake._aggregate_predicates` stance-aware resolver + tests. (fm engine.)
6. **§3 canonical + mirror + M-C doc de-stale.**

**Phase-0 gate (after 1–6, before Step 2):** hand-author ONE runbook with corrected predicates (symptom-surface
`supports` + one `refutes`), run the adversarial acceptance case (confident-but-wrong LLM the predicate catches),
GO iff it deterministically catches AND produces no wrong VALIDATED root. Only then regenerate the 756-predicate
validation layer.

**Test surfaces:** `test_indicator_evaluator.py` (M-B), `differential_intake` intake tests (M-A stance),
kb-toolkit validator tests (T1 lint, T2 grammar), pack/chunker passthrough tests (stance survives to pack.json).
