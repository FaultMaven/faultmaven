# Insufficient-Evidence Handling and Verification Status

When the engine cannot reach a grounded root cause, it must do two things at once: **not assert an ungrounded conclusion** (soundness), and **not stall** (keep engaging — surface what is missing, present options, hand off). This document describes how the engine handles that situation today, the two layers the handling currently spans, its enforcement tier, and the known limitations that motivate consolidating it into a single verification status.

**Related Documents**:

- [Progress Transparency](./progress-transparency.md) — the hypothesis-layer stall detector and repair patterns this document builds on
- [Grounded-Cause Counting](./grounded-cause-counting.md) — the causal-graph-layer assurance grade (`grade_cause_assurance` → `GROUNDED`)
- [Two-Dimensional Hypothesis Methodology](./two-dimensional-hypothesis-methodology.md) — the hypothesis/causal-graph distinction and the two grades of root-cause validation
- [Investigation Invariant Enforcement Matrix](./investigation-invariants.md) — the enforcement-tier framework (Structural > Code-guarded > Schema > Prompt-only) and *composition seam* / *dynamic drift* concepts this document uses
- [Evidence Needs Design](./evidence-needs-design.md) — the demand-side pool of outstanding evidence asks

---

## 1. Why this is a first-class concern

Verifiability is not a property of the reasoner (AI or human) — it is a property of the **case and the available data**. Some root causes leave no direct footprint (a race, a transient blip); some discriminating data was never collected, has rotated away, or is too costly to obtain. A correct engine therefore cannot promise "always produce the cause." Its contract is narrower and honest:

- **Soundness** — never present an ungrounded cause as the answer. A cause reaches a validated state only through a recognized grounding method (empirical evidence, runbook predicate, or deductive exclusion), **never by assertion** (see [Grounded-Cause Counting](./grounded-cause-counting.md) and the `CausalNode` state contract).
- **No collapse** — when the data does not (yet) support a conclusion, keep engaging: name the discriminating evidence, present options, escalate, or hand off. *A partial investigation that narrows the problem and states what is needed next is a valid outcome.*

"Insufficient evidence" is the case where those two guarantees are both live at once. It is common enough — and the failure modes (fabricating a cause, or spinning silently) are severe enough — that it warrants an explicit, consistent handling path rather than emergent behavior.

---

## 2. Terminology

Standardized terms used throughout this document. Where the codebase already has a term, this document adopts it rather than coining a synonym.

| Term | Meaning |
|---|---|
| **Hypothesis layer** | `case.hypotheses` — candidate root-cause *claims* and their `HypothesisState` (CAPTURED / VALIDATED / REFUTED / INCONCLUSIVE / RETIRED). The claim layer. |
| **Causal-graph layer** | `case.causal_nodes` / `causal_edges` — the *materialized* graph of PROBLEM / INTERMEDIATE / ROOT nodes whose states are set only by grounding. The node layer. |
| **Candidate / residual candidate** | A *candidate* (root-cause claim) is a hypothesis (the hypothesis layer above). Its *discriminators* are the `causal_verification` `EvidenceNeed`s motivated by it — the data that would confirm or eliminate it. *Residual* candidates are those still in play: hypothesis state **not** `REFUTED` or `RETIRED`. The declared-data-wall stall (§5.3) and the close record (§5.4) both range over the residual candidates. |
| **Assessment variable** | A signal *recomputed every turn* from case state (e.g. `cause_state`). It reflects a reading, not a committed decision. Contrast with a **disposition**. |
| **Disposition** | A **terminal outcome** — `RESOLVED` or `CLOSED` — reached only through the propose→confirm handshake (never auto-fired). This document does **not** overload "disposition" for the assessment concept below. |
| **Verification status** | Proposed unifying *assessment variable*: the engine's reading of whether a grounded cause is reachable and, if not, why. It is a **join of two orthogonal axes** — grounding (is a cause grounded?) × progress (has progress stalled?) — not a merged scalar (§5.1). Today this reading is split across the two layers (§3); it is not yet a single computed field (§4, §5). |
| **Insufficient-evidence** | The verification-status cell where diagnostic work has actually happened (the *work gate*, §5.2) but no cause can be grounded from currently available data. Distinct **both** from "not investigated yet" **and** from "the reasoner produced nothing" — the latter is a model/provider failure, not a property of the case (§5.2). |
| **Advisor posture** | The engine's baseline stance: it *suggests, asks, and recommends* — it is "an advisor, never an actor" (`templates.py`). It never claims to take actions itself. |
| **Structured handoff** | The engine's response shape when progress is blocked: state what is established, state what remains uncertain and why, and present the user with concrete options (data that would decide it, alternative angles, escalation, or pause). |
| **Enforcement tier** | From the invariant matrix: *Structural > Code-guarded > Schema > Prompt-only*, in decreasing strength. A Prompt-only behavior depends on LLM compliance and is subject to drift. |

---

## 3. Current handling (as built)

Insufficient-evidence handling exists today, but it is realized across **two independent layers** plus a set of prompt instructions. The pieces are individually correct; the concern is that they are not unified.

### 3.1 Hypothesis-layer signal — progress transparency and repair patterns

The [Progress Monitor](./progress-transparency.md) tracks investigative turns without milestone progress and, past a threshold, activates *transparent mode* — the engine surfaces the pending milestone and what would advance it. Within transparent mode it also detects specific stall patterns (`progress_monitor.py`), the relevant one being:

- **`EXHAUSTED`** — fires when `current_turn ≥ 8` **and** `turns_without_progress ≥ 5`, with ≥2 categories explored, ≥2 hypotheses refuted/inconclusive, ≥2 evidence items, and **no validated hypothesis**. Its handler is framed explicitly as *"not a failure — the agent has done good work but evidence is insufficient"* and its action is a **structured handoff**.
- **`HYPOTHESIS_DEADLOCK`** (all hypotheses inconclusive) → retire them and regenerate. This is the one repair that performs a **structural** mutation; the rest are prompt injections.
- **`FIX_FAILURE_CYCLE`** (repeated fixes that don't verify) → structured summary + escalation. This is the practical backstop for a defeasible conclusion: if treating the presumed cause does not resolve the problem, the differential was incomplete.

All of these read the **hypothesis layer** (`case.hypotheses`). None read the causal-graph layer.

### 3.2 Causal-graph-layer signal — assurance grade and `cause_state`

Independently, the causal-graph layer carries its own reading of how well the cause is established:

- **`cause_state`** (assessment variable: `UNKNOWN` / `CANDIDATES` / `IDENTIFIED`) — recomputed each turn from the grounded state of chain roots. Insufficient evidence simply keeps it at `UNKNOWN` or `CANDIDATES`. A once-grounded root that loses validation to an evidence *tie* holds at `CANDIDATES` rather than dropping to `UNKNOWN` — a deliberate no-collapse floor.
- **`grade_cause_assurance` → `GROUNDED` / `FALLBACK_ONLY` / `NO_ROOT`** — the harvest-gate grade (see [Grounded-Cause Counting](./grounded-cause-counting.md)). `NO_ROOT` means "no validated root," but does not distinguish "still working" from "cannot be grounded from available data."

Neither of these represents "insufficient evidence" as such; the situation is inferred from a low grade persisting.

### 3.3 The response shape — advisor posture and structured handoff

The engine's baseline is the **advisor posture** (`templates.py`): it suggests, asks, and recommends, and never claims to act. On a stall, the prompt templates instruct a **structured handoff**, including the explicit boundary statement and options:

> State the boundary — *"Given the available evidence, the cause is likely X or Y but I cannot determine which without \<specific data/access/test\>."* … Present options … *A well-documented partial investigation that narrows the problem and identifies what is needed next is a valuable outcome.*

The [Evidence Needs](./evidence-needs-design.md) pool feeds this: as `turns_without_progress` climbs, the surfaced discriminators rotate one page per non-progress turn, so a stalled case cycles through the whole outstanding-need pool rather than repeating the same three asks.

### 3.4 Enforcement tier of the current handling

| Piece | Tier |
|---|---|
| Transparent-mode activation, `EXHAUSTED`/`FIX_FAILURE_CYCLE` detection | Code-guarded (detection) |
| `HYPOTHESIS_DEADLOCK` hypothesis retirement | Structural (the one state mutation) |
| **Boundary statement, "present options", partial-is-valid framing** | **Prompt-only** |
| `cause_state` / assurance-grade derivation | Code-guarded |
| No auto-close on insufficient evidence (terminal stays user-confirmed) | Structural (propose→confirm handshake) |

The recognition of the *situation* is code-guarded, but the **behavior that matters to the user — recognizing the boundary, saying what is needed, handing off rather than concluding — is Prompt-only.** The engine asks the model to do it; it does not compute the situation and drive the response.

### 3.5 The unified read and the (flag-gated) code-guarded handoff

Two pieces of the §5 direction are now built and live behind a flag:

- **One computed read.** `assess_verification_status(case)` (`verification_status.py`) computes the verification-status join of §5.1 — grounding (`grade_cause_assurance == GROUNDED`) × progress (stalled?) — as a single value (`HEALTHY` / `TREATMENT_BLOCKED` / `OPEN` / `NOT_YET_PRODUCTIVE` / `INSUFFICIENT_EVIDENCE`), reading the two existing layers rather than adding a third signal. The work gate (§5.2) and the stall thresholds are the **decomposed** `EXHAUSTED` conditions; both readers source them from **one** neutral, dependency-free module (`exhaustion_thresholds.py`) so the dimensions they measure *identically* (the stall thresholds; category/evidence breadth) cannot drift — the first concrete narrowing of the §4.1 composition seam. The hypothesis dimension is deliberately *not* shared: the work gate counts generation depth (hypotheses that exist) while `EXHAUSTED` counts elimination depth (hypotheses refuted), so those keep separate constants.
- **The engine drives the handoff (behind `enable_insufficient_evidence_handoff`, default off).** When the flag is on and `assess_verification_status` reads `INSUFFICIENT_EVIDENCE`, `engine_owned_affordances` emits a deterministic structured-handoff affordance — code-guarded, so the handoff fires regardless of LLM compliance — and records the status on the turn. The handoff sits **last** among the engine-owned affordances: any pending state-machine handshake (disposition, Gate 1) takes precedence, and it is scoped to `INVESTIGATING`. Its affordances are keep-engaging by construction (invite discriminating data or a fresh angle) and **never steer toward close** (§5.4). The boundary-statement *content* — *what specifically* is needed — stays model-authored in the prose; the engine guarantees only that the handoff *occurs*.

The flag is off until the calibration eval (`test_verification_status.py`) validates firing precision on a weak model. With it off, the effective handling is still §3.1–§3.4 (the boundary statement remains Prompt-only). The model-declared obtainability refinement (§5.3), persistence, and the terminal capture-on-close (§5.4) are not yet built.

---

## 4. Known limitations

These are current-state facts, not hypotheticals.

1. **Two unreconciled layers (a composition seam).** Stall/`EXHAUSTED` detection reads the hypothesis layer; grounding and `cause_state` read the causal-graph layer. A case can look "exhausted" to the hypothesis layer while the causal graph was never materialized, and vice versa. Per the invariant matrix's *dynamic drift* warning, two cooperating readings with no single source of truth is exactly the shape that fails while each part looks locally correct.

2. **The load-bearing behavior is Prompt-only.** Because the boundary-statement and handoff are instructions rather than engine-computed outputs, they depend on model compliance. A weaker or non-reasoning model can fabricate a single cause or continue spinning without ever stating the boundary — the two failure modes this handling exists to prevent — and nothing structural catches it.

3. **No first-class insufficient-evidence status.** The situation is a per-turn signal (in turn metadata and prompt injection), not a persisted, queryable field. Terminal states are only `RESOLVED` / `CLOSED`; there is no recorded "insufficient-evidence" outcome and no such closure reason. Consequences: the honest partial outcome is not durable, not surfaced consistently, and **not evaluable** — there is no state against which to measure whether the engine correctly recognized a data wall.

---

## 5. Direction

The consolidation target is to make insufficient-evidence handling a single, engine-owned, evaluable path rather than two layers plus a prompt. Not yet implemented; recorded here to guide the work. The whole design turns on *correctly identifying which fact the case is in and disposing each* — so the refinements below all guard against mis-identifying the confusable situations, which is where the failures actually live.

### 5.1 Verification status is a join of two axes, not a merged scalar

Grounding and progress are **orthogonal facts** — "is a cause grounded?" and "has progress stalled?" answer different questions and dispose differently. Verification status is their **join**; collapsing them to a single "reachable / not" scalar mis-disposes the corners.

| | Progressing | Stalled |
|---|---|---|
| **Grounded** | healthy — moving toward a fix | **treatment-blocked** — have a cause but can't reach a *verified fix* (failed fix, no access, change window, waiting on another team) → escalate / handoff. `FIX_FAILURE_CYCLE` is *one* pattern that lands here, not the cell itself. |
| **Not grounded** | keep working — nothing special surfaced | **insufficient-evidence** — the real case |

Insufficient-evidence is specifically the **(not-grounded × stalled)** cell. Two orderings hold inside it:

- **Grounding-first.** When the differential is exhausted, the cell first disposes to *attempt deductive grounding* (proof-by-exclusion); it falls to insufficient-evidence only if that fails. The status must never silently pre-empt the deductive arm.
- **Work-gated.** The cell is reachable only after real diagnostic work has happened — see §5.2.

### 5.2 Separate "insufficient evidence" from "the reasoner produced nothing"

An empty graph has two causes that must never be conflated: the **data** is insufficient, or the **model** produced nothing. The shipping non-reasoning default can emit an empty graph regardless of how good the data is; disposing that as insufficient-evidence blames the case for the reasoner's failure.

- **Work gate (hard precondition).** Insufficient-evidence requires that diagnostic work actually occurred — reuse **only the ≥2-preconditions of** `EXHAUSTED` (≥2 categories explored, ≥2 hypotheses, ≥2 evidence items). Below the gate the status is **not-yet-productive**, never insufficient-evidence.
- **Decompose `EXHAUSTED`; do not reuse it whole.** `EXHAUSTED` today bundles two independent things: its ≥2/≥2/≥2 *work* preconditions become the **work gate** (this §5.2), while its `current_turn ≥ 8` / `turns_without_progress ≥ 5` *stall* thresholds are the **progress axis** (§5.1). Wiring the turn thresholds into the work gate would re-break the fast-exhaustion case — a differential that genuinely exhausts in a few turns must be able to reach blocked without waiting for an arbitrary turn floor.
- **Provider floor (a health fact, not a per-case verdict).** The handling is **model-agnostic**: every gate reads case state, never a model id, so the design disposes whatever any model produces. A model that *never* crosses the work gate across cases is mis-provisioned — that surfaces as a **configuration/health signal** (alongside the existing tool-calling capability gate and the `/health` degraded state), not as a per-case "your data is insufficient" verdict. Because models populate the hypothesis layer to differing degrees, whether a given *configured* model crosses the gate is **observed per-provider** (see the implementation plan's Phase 0b), never assumed of a particular model.

### 5.3 The engine computes the floor; the model can only refine toward "blocked"

Promote the tier from Prompt-only to Code-guarded: the engine computes the objective half (work-gated, not-grounded, stalled) and *drives* the structured handoff. The one judgment it cannot compute — *is the discriminating data obtainable at all?* — is an opt-in, logged model-declared input (the same pattern as the deductive exhaustiveness assertion).

Because a model output is as often absent as a hypothesis with no evidence, the input is **fail-safe by construction and monotonic**:

- The engine floor is always **keep-engaging / still-reachable**.
- The model's obtainability judgment can only move the reading *toward* "insufficient-with-a-stated-need" — never back toward safety, and never past the work gate (§5.2).
- A **missing** judgment defaults to keep-engaging (no collapse). The engine never depends on the model's presence to stay safe.
- **A declared data wall *is* a stall.** The progress axis (§5.1) is stalled by the time-thresholds **or** by every residual candidate being declared unresolvable (all its discriminators unobtainable). So obtainability can *establish* the stall — a fully-declared wall reaches the handoff immediately rather than waiting out the turn thresholds — rather than only refining a time-stall it could not itself cause. All of §5.1, §5.3, and the counting rule share this one definition of *stalled*.

### 5.4 A first-class status that survives the terminal boundary

The status is an assessment variable with **two distinct durability regimes**:

- **Intra-investigation** — recomputed each turn, so it **dissolves the moment data arrives** (fail-forward to grounded). This is correct and needs no persistence.
- **At CLOSE** — when a user closes an insufficient-evidence case, the honest partial must be **captured, not lost**: a `closed_insufficient_evidence` closure reason carrying the residual candidates and the specific unmet need. Without this, "durable" holds only until the case ends — exactly when the record matters most, for calibration and for the flywheel (what we could *not* resolve, and why, is signal).

It remains **not** a terminal disposition: it never auto-closes (the user still owns RESOLVED/CLOSED via propose→confirm).

### 5.5 Calibrate on the confusable pairs, not the archetypes

The clean archetypes (observable → grounded, unobservable → insufficient-with-a-stated-need, incomplete → regenerate) are the easy cases — a system that *confuses* the near-misses passes an archetype-only eval. Identification quality is the stated point, and only the confusable pairs measure it:

- **data-wall vs model-produced-nothing** (§5.2)
- **still-working vs walled** (§5.3)
- **grounded-but-stalled vs not-grounded-stalled** (§5.1)

The calibration set must include these, run as an **offline signal, never a runtime gate** (consistent with the eval-as-signal discipline).

**Rejected alternatives:**

- *Auto-close on exhaustion* — rejected: disposition is user-owned; the engine must not conclude a case because it gave up (that is collapse, not resolution).
- *Prompt-only strengthening (more directive stall text)* — rejected: it leaves the behavior in the weakest enforcement tier and model-compliance-dependent; the problem is the tier, not the wording.
- *A single merged "reachable/not" scalar* — rejected: it averages the orthogonal grounding × progress axes and mis-disposes the grounded-but-stalled and work-not-yet-done corners (§5.1, §5.2).
- *A deductive-arm-specific fix* — rejected: insufficient-evidence is general; a path scoped to one grounding method would fork handling and worsen the composition seam rather than close it.

---

## 6. Summary

The engine already recognizes evidence-starved stagnation and shifts to an advisor/structured-handoff posture instead of concluding or exiting — the behavior your soundness and no-collapse guarantees require. The gap is not that it is missing; it is that the recognition is split across the hypothesis and causal-graph layers and the decisive behavior is Prompt-only. Consolidating it into a single, engine-computed, persisted **verification status** — with the model supplying only the judgment it alone can make — is what turns "handle it when the model complies" into "handle it consistently, regardless of the model in the seat."
