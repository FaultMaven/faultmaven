# Design Review — the RCC dual-authoring seam (#668, #673) against original design intent

> **Outcome (2026-07-13): every recommendation in §5 is executed.** INV-40
> narration-truth guard + #675 cleanup merged (PR #684 → `6c8b218b`); INV-41
> backstop-reliance metric merged (PR #686 → `e80b09f7`, emission pre-stamp at
> `finalize_resolution_truth_surface`); §3.5 grade-juxtaposition shipped as a
> 4-repo workstream (backend #687, copilot #94, dashboard #38, slack #28 — all
> LGTM'd). #656 remains closed; #673 remains open and gated on the INV-41
> backstop-reliance rate clearing at the INV-39 provider floor (gate condition +
> retirement sequencing recorded on the ticket, comment 4952456465). The durable
> design content now lives in methodology §7.7/§7.9 and the INV-40/INV-41
> registry rows; this file is the review record.

**Author:** solutions architect, investigation engine. **Date:** 2026-07-12.
**Scope:** review + design only; no code changed. Grounded in the design docs
(`two-dimensional-hypothesis-methodology.md` §7.6/§7.7, `investigation-invariants.md`,
`insufficient-evidence-handling.md`, `agent-behavioral-rules.md`), the code as it stands on main,
and issues #656 (body + systemic-review + acceptance comments), #668, #673, #675.
**Lens:** the two soundness guarantees (NO INCORRECT CONCLUSION, NO COLLAPSE UNDER PRESSURE)
and the LLM-agnostic testing invariant (mechanical engine-state assertions decide correctness).

---

## 0. Executive verdicts

| Item | Verdict | One-line reason |
|---|---|---|
| Dual-authoring itself | **Acceptable-by-design trade-off** (documented) | §7.6 names LLM free-text RCC authorship a deliberate trust boundary; §7.7 records chain-derivation as the *gated eventual convergence* |
| **#673** (retire dual-authoring) | **Not a gap and not a deviation — it is the design's own recorded endpoint**, correctly gated. Residual gap: the gate is not yet a measurable metric | §7.7 "rejected-for-now alternative … tracked as #673"; the gate ("reliable chain-grounding") has no instrumented definition |
| **#668** (false "Case resolved." narration) | **Real design gap** — in guarantee *scope*, not in the reconciliation layer | The user-visible narration channel sits outside every truth surface and every invariant; the reconciliation layer structurally cannot reach it; the guarantees as documented stop at engine-surfaced truth |
| **#675** (stale `root_cause_identified` readers) | **Implementation deviation** (housekeeping) | Dead readers of a decommissioned signal, one test pinning the buggy behavior — cleanup, no design question |
| Terminal report / closure-summary prose | **Acceptable-by-design** | Generated only after an engine-executed terminal transition (disposition true by construction); highest-stakes sub-sections engine-gated (INV-28 recomputed qualifier, INV-30 absence rendering) |
| LLM RCC confidence over-claim on read surfaces | **Acceptable-by-design trade-off with a bounded completion item** | Deliberate read-time-labeling choice (#572/INV-28); WARNING seam exists; completion item = verify the grade is actually juxtaposed on every RCC read surface (uncertainty flagged, §3.5) |
| `symptom_verified` (LLM-set) | **Acceptable-by-design**, evidence-gated hardening candidate | Documented LLM-owned indicator; guarded by evidence-revert + anchoring requirement; no observed failure to justify an engine gate yet |
| Closing #656 | **Correct — keep closed** | Its guarantees are engine-state properties and they held in #668's own incident; acceptance replay passed; #668 is a different failure class on a different surface |

The single most load-bearing architectural finding: **#668 and #673 are on different surfaces.
Completing #673 (single-authority RCC) does not fix #668** — the narration channel
(`agent_response`) remains LLM free text under any RCC authorship model. #668 is therefore not
evidence for accelerating #673, and #673 must not be sold as the fix for #668.

---

## 1. What the design actually says (intent reconstruction)

### 1.1 Dual-authoring is intentional — with a documented expiry condition

The division of labor is stated verbatim in the methodology, not inferred:

- §7.6: an LLM-written `RootCauseConclusion` "is the LLM's own stance, a **trust boundary**: the
  engine never authors or overwrites it."
- §7.7: "the LLM decides *what* the cause is … the engine only confirms whether that decision is
  grounded enough to drive the irreversible actions gated on identification. … Deciding the cause
  is the LLM's; certifying the grounding bar is the engine's."
- §0 (M-axioms): the engine operates a **derive/veto lane** — it derives assessment signals and
  strips/withholds invalid emissions, but never advances or authors LLM-owned content.

Historically the namespace split is accretion — #673 is right that "the RCC predates the chain
model." But the *retention* of LLM free-text authorship after the chain model landed was a
ratified decision, made for a soundness reason: the free-text conclusion is the terminal-soundness
backstop while models under-build chains (§9.2: `_cause_identified` reads `cause_state` OR the
RCC OR the working conclusion "so terminal soundness never rests on the chain alone while models
under-build it"). Every #656 gate run measured this under-building (the recurring CAPTURED-hyp
under-claim). So: origin = accretion; current state = deliberate design with a recorded
retirement path.

### 1.2 The reconciliation layer is a bridge, and the docs say so

§7.6's link/retract/read-suppress machinery is documented as the standing cost of the namespace
split, and §7.7 carries the explicit line: deriving the conclusion text from the chain — at which
point "dual-authoring (with the §7.6 link/retract and INV-25 over-claim reconciliation) **is
retired**" — "is the eventual convergence. It is **gated** on reliable chain-grounding …
Tracked as #673." INV-35 restates it. The design already contains #673 as its own endpoint.

### 1.3 The guarantees are defined over engine-surfaced truth — narration is out of scope as written

- The methodology binds the guarantees to "the conclusion the engine surfaces to every terminal
  consumer (the disposition/M5 gate, the report, the copilot UI, KB runbook harvesting)" (§7.6)
  and to the validation layer / anchoring machinery (§6 status note).
- The only prose↔state coherence rule is **INV-26(b)** and it is scoped to gate-override turns:
  "the visible transcript may not contradict the applied `state_updates`."
- **INV-15** is prompt-tier with a "deliberately narrow" runtime scan (`_completion_phrases`),
  and its narrowness was separately ratified (PR #299: keep the scan narrow; add a
  separately-tagged telemetry signal if broader drift detection becomes valuable).
- `agent-behavioral-rules.md` explicitly rejects post-hoc prose validation *as behavioral-rule
  enforcement*, while naming two legitimate roles it could play: observability, and a
  "last-resort safety net for genuinely uncapable models." Neither ships today.

So there is no invariant, guard, or guarantee clause covering ordinary-turn narration coherence
with engine state. That absence is the precise hole #668 fell through.

---

## 2. What the code actually does (deviation check)

The implementation is **faithful to the documented design** at every point this review checked:

- The engine mirror defers entirely to the LLM RCC (`synthesize_rcc_from_validated_root` returns
  early on a non-engine-authored RCC; the confirm-stamp upgraders never touch LLM RCCs; "the
  LLM's own conclusion always wins" is stated in code comments) — exactly §7.6's trust boundary.
- The reconciliation layer (`link_llm_rcc_to_cause`, `retract_disconfirmed_rcc`, MECE
  read-suppress in `_cause_identified`) operates on the structured RCC object and graph/evidence
  rows only — **never on prose**. It cannot see, let alone correct, a narration claim.
- Narration (`agent_response`) flows to the user verbatim. Gate branches *append* engine text
  (`_prose_with_gate_notice` is pure composition, INV-26); no branch inspects prose content.
- The engine already computes the #668 signal and discards it: `_completion_phrases` in
  `milestone_engine.py` contains "case resolved", "case is resolved", "marked as resolved",
  "case closed"; the scan result is emitted **only** into the `transition_compliance` log record
  ("compliance instrumentation … quarterly drift review"). No consumer compares it to
  `case.state`, the pending transition, or the assurance grade.

Conclusion: #668 is **not** an implementation deviation. The implementation implements the
documented prompt-only stance exactly. The gap is in the design's scope definition.

---

## 3. Per-gap analysis

### 3.1 #673 — retire dual-authoring (derive the conclusion from the validated chain)

**Verdict: the design's own recorded endpoint, correctly gated. Keep open as gated tech-debt; do
not schedule; make the gate measurable.**

Issue #673 does not challenge the design — it *is* the design's §7.7 trajectory, filed so the
retirement is deliberate rather than forgotten. Its own body argues the counter-case correctly:
retiring the free-text backstop before models ground chains reliably strands the cases that today
resolve via the RCC leg of `_cause_identified` — a NO-COLLAPSE regression. INV-35 already banked
the monotone step (graph as authoritative *reference* via `names_root_node_id`) without adding
machinery the retirement would tear out.

The one real gap inside #673 is that its gate — "models ground causal chains reliably enough that
`cause_state` reaches IDENTIFIED via the chain without needing the RCC backstop" — is prose, not
a metric. The data to make it a metric already exists: P1.2 persists `cause_assurance` per turn,
and `_cause_identified` knows which leg (chain / RCC / working-conclusion) satisfied it.
**Recommendation:** instrument a per-resolution counter of "terminal identification satisfied by
the RCC or working-conclusion leg while no chain root stood validated" (the backstop-reliance
rate), segmented by provider, and write the numeric threshold into #673. This composes with
INV-39's provider-floor metric: the gate must hold at the provider floor, not just on the best
model — otherwise retirement is a NO-COLLAPSE regression precisely for the weakest supported
provider.

*Rejected alternative: scheduling the retirement now on the strength of the #656 campaign's
trust-relocation momentum — the campaign's own gate runs repeatedly measured chain under-building,
so the gate condition is demonstrably not met.*

### 3.2 #668 — LLM narrates "Case resolved." while every engine surface says INVESTIGATING

**Verdict: real design gap — in guarantee scope. Structurally unreachable by the reconciliation
layer; fixable within the current design at the render boundary. Does not require, and is not
fixed by, #673.**

Three findings compose:

1. **Structurally unfixable by the reconciliation layer.** That layer's inputs are the structured
   RCC and the graph. Prose is authored outside every truth surface it reads. No amount of
   link/retract/read-suppress hardening touches `agent_response`. (Question 3 of the brief:
   answered — it is *both* structurally out of the layer's reach *and* fixable by a bounded
   render-time mechanism; those are not in tension because the fix lives in a different lane.)
2. **The guarantee scope excludes the surface the user actually reads.** As documented, both
   guarantees held in the #668 incident — and a user was still told, three times out of three
   attempts, that the case was resolved when it was not. The user acts on the chat message, not
   on the DB row. A false resolution claim delivered to the user is an incorrect conclusion in
   every sense that matters to the product's promise; the letter of the guarantee excludes it
   only because the guarantee was written about engine state. This is the definition of a design
   gap: the spirit and the letter have come apart on the highest-stakes claim.
3. **The failure has two halves, and #668's two candidate fixes are complements, not
   alternatives.** The elicitation half (haiku skipping `proposed_transition` +
   `causal_absence_evidence` on long context, 3/3, despite a prompt contract mandating both) is a
   prompt/elicitation defect that determines *frequency*. The truth-split half (nothing reconciles
   narration against state) is the design gap that determines *blast radius*. Fix the template to
   reduce occurrences; fix the render boundary so an occurrence can never lie to the user.

**Compatibility with prior decisions (this matters — two documented rejections are adjacent):**

- PR #299 ("keep the `_completion_phrases` scan narrow") is about scan *breadth*. The fix below
  adds no phrases and no new detector; it changes the *consumer* of the existing narrow scan from
  log-only to state-reconciled. #299's substance (don't build a broad advisor-role drift detector)
  stands untouched.
- `agent-behavioral-rules.md`'s rejection of post-hoc prose validators is about *behavioral-rule
  enforcement* (style, methodology compliance). A narration-truth coherence check on terminal
  claims is neither: it is a soundness guard in the engine-derive/veto lane, mechanical
  (regex + engine state, no model-graded judge — LLM-agnostic testing invariant satisfied), and
  matches the doc's own allowance for a "last-resort safety net."

The design-level specification is in §5.

### 3.3 #675 — stale readers of decommissioned `root_cause_identified`

**Verdict: implementation deviation (housekeeping).** Two pre-existing consumers read a signal
INV-35 removed: `progress_monitor._find_pending_milestone` (getattr on a nonexistent attribute →
milestone perpetually "pending", post-symptom guidance never advances) and
`CATEGORY_MILESTONE_MAP` (causal evidence attributes to nothing). One test pins the buggy
behavior. This is dead-signal cleanup fully covered by the no-dead-code norm: re-express both
against `cause_state` or delete, recalibrate the pinned test. No design question; keep it a
standalone tech-debt item.

### 3.4 Terminal report / closure-summary prose

**Verdict: acceptable-by-design.** The summary/report is LLM-authored prose, but it is generated
only after the engine has actually executed the terminal transition — the disposition it narrates
is true by construction, which removes the #668 failure shape from this surface. The
highest-stakes sub-claims inside it are engine-gated: the Root Cause section carries an assurance
qualifier **recomputed from the graph at render time** (INV-28), and a causal-absence row renders
under Confirming Evidence only when it passes gate-side qualification (INV-30). Residual prose
drift (florid over-narration around true facts) is a quality concern, not a soundness one.

### 3.5 LLM RCC confidence over-claim on read surfaces

**Verdict: acceptable-by-design trade-off, with one bounded completion item.** An LLM RCC
claiming VERIFIED/0.95 while the engine grade is MECHANISTIC stands as text by design (#572 chose
read-time labeling over apply-site clamping; INV-28 emits the `cause_confidence_overclaim`
WARNING; retraction is link-based, and an unlinked RCC is a documented residual). The design is
coherent *provided the read-time label is actually present wherever the RCC text is read*. The
resolution report and progress-transparency surfaces carry the grade; whether the
copilot/dashboard RCC displays juxtapose it is **not verified by this review** (uncertainty —
`case_ui_adapter` surfaces the raw RCC as informational). Completion item: audit the frontend RCC
read surfaces for grade juxtaposition; if absent, that is a small implementation deviation from
the #572 decision, not new design.

### 3.6 `symptom_verified` and the working-conclusion proxy

**`symptom_verified` — acceptable-by-design.** It is the documented LLM-owned progress indicator
("set by the LLM", framework §Progress Indicators), guarded by the evidence-revert rule and by
being load-bearing only in *conjunction* with engine-derived legs (the grounding join also
requires it, but it cannot alone identify a cause or open a terminal gate). It is the last
LLM self-claim in the DF-2 inventory without an engine derivation. Per the repo's own
evidence-gated discipline (F3/invalidation-first precedent: no mechanism until a sim demonstrates
the failure), leave it prompt-governed until a gate run or production case shows a false
`symptom_verified` driving a wrong decision. Note it explicitly as a watched seam.

**Working-conclusion proxy — guarded.** Engine-generated, MECE-suppressed (INV-31), its solution
license clears on recompute (INV-32 documents the one-turn timing leg), and new-hypothesis priors
are capped below the identification threshold. No action.

---

## 4. Systemic assessment of the dual-authoring seam

**Is the intended design single-authority?** Not today, by explicit decision — and eventually
yes, by explicit decision. The design holds both, coherently: dual authorship with an engine
derive/veto lane is the *current* architecture (a NO-COLLAPSE backstop while chain-grounding is
unreliable), and single-authority chain-derivation is the *recorded convergence* (§7.7), gated
and tracked (#673). The #656 campaign's LLM→engine trust relocation (INV-23/29/30/34/35) is
consistent with this: every step relocated *guardrail operation* — categories, support counts,
absence force, retraction, the identification signal — while deliberately leaving *cause-text
authorship* with the LLM. The trajectory's endpoint for the text is #673, and the design says so.

**Where has implementation diverged?** Almost nowhere on this seam — that is the striking result.
The mirror-deference rules, the link/retract mechanics, the read-suppress, the WARNING-only
over-claim seam, the prompt-only narration stance: all match the documented decisions, including
the documented residuals (unlinked-RCC retraction, over-claim-as-label). The two real divergences
found are small and mechanical: the #675 dead readers, and (unverified) possible missing grade
juxtaposition on frontend RCC surfaces (§3.5).

**Where has the *design* diverged from its own guarantees?** In exactly one place: the narration
channel. The design painstakingly makes every decision-driving surface engine-derived or
engine-gated, then delivers all of it to the user wrapped in an ungated free-text channel that
can assert the opposite. INV-26(b) already states the correct principle — "the visible transcript
may not contradict the applied `state_updates`" — but scopes it to gate-override turns. #668
demonstrates the principle is load-bearing on *every* turn for the terminal-claim class. The
design gap is that this principle was never generalized, and the guarantees were never stated to
cover the one surface users read.

---

## 5. Recommendation

### 5.1 Dispositions

1. **Keep #656 closed.** Its scope was the three-stage derail; the acceptance replay passes; both
   guarantees, as documented, held even in #668's incident (the engine refused every unsound
   state). Reopening would conflate a completed engine-truth campaign with a new surface class.
2. **Keep #673 open, gated — and add the gate metric** (backstop-reliance rate per §3.1,
   evaluated at the provider floor). Do not schedule until the metric shows sustained
   low reliance. #673 is Phase-2 work relative to the item below and is *not* its dependency.
3. **Elevate #668 from "prose concern" to a bounded design initiative** — narration-truth
   coherence (candidate registry row: INV-40). It is small (one detector already exists, one
   composition mechanism already exists), but it is a *design* addition: it extends guarantee
   scope to the user-visible transcript for terminal claims. Specification below.
4. **Execute #675 as ordinary cleanup.** No design content.
5. **Doc amendments (with the #668 work, same branch, design-first):** (a) generalize the
   INV-26(b) principle in the invariants registry: the visible transcript may not contradict
   engine truth *on terminal-disposition claims, on any turn*; (b) add one clause where the
   guarantees are defined (§7.6 or the methodology §6 note) naming the user-visible transcript a
   guarantee surface for disposition claims; (c) fix the brief's stale pointer — there is no
   `investigation-flow-redesign.md`; the flow-redesign record lives in the framework +
   lifecycle docs and `investigation-invariants.md`'s retirement notes.

### 5.2 Design specification — narration-truth coherence guard (INV-40 candidate; design level only)

**Invariant statement.** On a non-terminal turn with no engine-confirmed terminal transition, the
turn's user-visible message never asserts, unqualified, that the case is resolved or closed. If
the LLM's prose makes such an assertion, the engine composes a corrective notice; it never
silently passes the claim through.

**Lane and mechanism (all existing pieces, one new join):**

- *Detect:* reuse the existing `_completion_phrases` scan verbatim — no new phrases (preserves
  PR #299's narrowness decision).
- *Reconcile (the new join):* when the scan fires AND engine truth disagrees (case state not
  terminal AND no confirmed `proposed_transition` executing this turn), classify the turn as a
  narration over-claim.
- *Respond by composition, never substitution:* append an engine-owned notice below the LLM
  prose via the existing `_prose_with_gate_notice` composition pattern (INV-26 lane), stating
  that the case remains under investigation and what resolution actually requires (the readiness
  gate's own missing-items verdict is already available in `transition_compliance`). The LLM's
  prose is preserved intact — the DF-4 lesson (never destroy work product) is binding.
- *Observe:* a counter for narration over-claims (the DF-6 lesson: the over-claim polarity must
  be watched; today the completion-phrase boolean dies in a log field), segmented by provider —
  this also feeds the INV-39 provider-floor picture.
- *Elicitation half (companion fix, same initiative):* harden the TREATMENT/verify-turn template
  so a user-confirmed fix elicits the absence row + `proposed_transition` reliably on long
  context (#668's second candidate direction). Template change follows the prompt-design norm:
  minimal, one rule + one example, validated by behavior.

**Graceful denial (invariant-grade test).** False positive — the regex fires on conditional or
quoted prose ("once you confirm, the case is resolved") — degrades to appending a notice that is
*still true* ("this case is currently open; to resolve it …"): mildly redundant, never wrong,
never blocks the turn, never mutates state. NO-COLLAPSE is unthreatened because the guard is
append-only and pre-LLM paths are untouched. NO INCORRECT CONCLUSION is strengthened: the false
claim can still be *authored* but can no longer stand *uncontradicted* on the user surface.

**LLM-agnostic testing.** Pass/fail is mechanical: given a canned LLM response containing a
completion phrase and an engine state of INVESTIGATING, assert the composed message contains the
engine notice and the counter incremented. No judge, no model tuning; model variation changes
frequency, never the boundary.

*Rejected alternative: rewriting or suppressing the LLM prose (repeats the DF-4 override
failure), and an LLM-graded prose-consistency judge (violates the LLM-agnostic testing
invariant and reopens the removed post-generation-validator pattern).*

**Sequencing.** (1) #675 cleanup — independent, immediate. (2) #668 initiative (guard, template,
and doc amendments, one branch, design-first). (3) #673 gate metric instrumentation — can ride with
(2)'s counter work since both read the same truth surfaces. (4) #673 retirement itself — untouched
until the metric clears its threshold at the provider floor.

**Guardrail against re-breaching the guarantees.** Every element above is additive and
composition-only: no state mutation, no new blocking gate, no LLM-owned signal promoted to
engine authority, no removal of the RCC backstop. The only behavior change a user can observe is
an appended truthful notice on turns where the model over-claimed. The #656 acceptance replay and
the INV-26/27/28/29/30/31 pinning tests remain the regression floor; the new invariant adds its
own mechanical pins without touching theirs.

---

## 6. Uncertainties flagged

- Whether copilot/dashboard RCC displays juxtapose the assurance grade (§3.5) — not verified;
  needs a frontend check before calling it a deviation.
- The exact wording-collision risk of `_completion_phrases` on conditional prose is asserted from
  the phrase list, not measured; the guard's graceful-denial design makes the cost of a false
  positive a redundant-but-true notice, so precision tuning is not load-bearing.
- #668 was observed on one provider (claude-haiku-4-5, long-context) in a gate tier; production
  frequency is unknown. This affects the priority of the elicitation half, not the existence of
  the design gap.
- This review takes the design docs' §7.6/§7.7 as the authoritative record of intent (they are
  recent and internally consistent with the INV matrix); if there is older unwritten intent that
  free-text authorship was meant to be temporary from day one, the "acceptable-by-design" framing
  of §3.1 would soften further toward "deviation," but no document supports that reading.
