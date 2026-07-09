# Two-Dimensional Hypothesis Methodology

This document defines the diagnostic reasoning methodology the investigation
agent follows during DIAGNOSIS and extended-diagnosis TREATMENT: how candidate
root causes are **formed**, **structured** into a search space, **searched**,
and **validated** before any remediation is proposed.

It is the *methodology layer* beneath the hypothesis lifecycle. The lifecycle
states and confidence mechanics are specified in
[Evidence-Driven Investigation Framework §6](./evidence-driven-investigation-framework.md#6-hypothesis-model)
and [Investigation Data Models §3](./investigation-data-models.md#3-hypothesis-workflow);
this document defines what the agent is *reasoning about* when it drives them.

## Status

**Core implemented; reasoning-rigor layer partially landed.** The central
reframe — *a hypothesis is a causal chain, not a single sentence* — is now the
engine's **actual** model: chain emission is the sole investigation path (the
flat-sentence model and its transitional flag/bridge scaffolding were removed),
and `cause_state` is derived from a validated chain root rather than asserted.
The data-model and engine surface in [§9](#9-engine-alignment) landed across
PRs #487–#507. What is built versus still design-intent:

- **Built (merged):** the causal-graph schema (§9.1 — `CausalNode`/`CausalEdge`/
  `NodeEvidenceLink`); chain emission as the only path; **node-identity
  preservation** — the engine renders existing `cn_…` ids back into context so
  the LLM *extends* a chain rather than re-emitting a cause as a duplicate node;
  chain-derived `cause_state` (§9.2), never asserted (M4); engine-deterministic
  failed-fix demotion (M6, §9.3); root-actionability (M1); AND-proof (M7); the
  deductive-exclusion primitive (§7.1.1); and **M5 solution-gating** — a SOLUTION
  is registered only once the cause is *established*, using the **same predicate
  as the resolution gate** (`_cause_identified`: `cause_state == IDENTIFIED`, a
  set `RootCauseConclusion`, or a working conclusion ≥ 0.6) so M5 is never
  stricter than the gate that lets a case resolve; else downgraded to a
  diagnostic with a recovery reason (engine veto, extends INV-23; mitigation
  exempt). *Quadrant-level precision (exempting `defensive_fix`) is deferred
  until the solution emission carries an `InterventionQuadrant`.* Also
  **F3 signature-screening** (§4) — a *prompt-level* formation rule (the LLM
  rejects a cause whose mechanism cannot produce D's observed signature; this is
  a semantic judgment, deliberately not an engine token-match), sim-validated for
  no over-screening. Also the **M2 confidence grades** (§9.5): the assurance
  ladder (`NO_ROOT`/`MECHANISTIC`/`CONFIRMED`) is computed and persisted per
  turn, "verified"/≥0.9 on the engine-synthesized conclusion requires a
  counterfactual confirmation on the validated root (empirical *and* deductive
  validation cap at `CONFIDENT`/0.8), `CONFIRMED` is the sole KB-harvest
  authority, and a conclusion that over-claims "verified" below the grade is
  surfaced (WARNING seam log + report assurance qualifier).
- **Design-intent, not yet built** — the LLM satisfies this *behaviorally*; no
  engine gate enforces it: chain-level belief propagation (§9.4 — the engine
  still uses the per-evidence `+0.15 / −0.20` counter from
  [framework §6](./evidence-driven-investigation-framework.md#6-hypothesis-model)).
  It promotes to the methodology-invariant registry (§0) when implemented.
- **Invalidation-first search (§6) — conceptual principle, deliberately not
  enforced.** The LLM already reasons this way; a prompt-enforcement attempt was
  tried post-F3 and removed (regressed node emission, no demonstrated benefit). It
  is a search-*efficiency* lever, not a soundness mechanism, so it is not a
  pending obligation — see the §6 "Status" / "Rejected alternative" notes and the
  bar for any reintroduction.
- **F4 family-completeness (§4) — measured, not warranted as an engine/prompt
  rule.** The premise is that the agent *tunnels at formation* on the family a
  symptom screams (concluding cause B because family A was never enumerated). A
  measure-first study (two cross-family misdirection scenarios — a multi-downstream
  resource-exhaustion case and a single-downstream expired-mTLS-cert case — in
  `fm-sre-simulator`) found the recommended STRICT model **does not tunnel**: it
  swept to the correct cross-family cause every run, even when the symptom points
  squarely at the obvious wrong family. Authentic symptom signatures encode their
  family (which is what F3 already leverages), so the "points squarely at the wrong
  family with no tell" premise is hard to construct without implausibility. F4 thus
  stays a formation *guideline*, not an enforced rule. The scenarios are retained
  as permanent tunnel-vision regression evals. Reintroduce only if a real case
  exhibits formation tunnel vision on a STRICT model.

The lifecycle/confidence *mechanics* (states, the `+0.15/−0.20` counter, decay)
remain specified in framework §6; this document defines the reasoning the agent
applies on top of them.

**Related documents**:

- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) — hypothesis lifecycle (§6), DIAGNOSIS/TREATMENT stages (§3)
- [Investigation Data Models](./investigation-data-models.md) — Hypothesis schema (§3), RootCauseConclusion (§1.11), Evidence model (§2)
- [Agent Stage Playbook](./agent-stage-playbook.md) — per-stage agent duties (§3 DIAGNOSIS / TREATMENT)
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) — mitigation insert and the resolution gate (§2)

---

## 0. Load-bearing Invariants (axioms)

These seven properties are the **axioms** the rest of this document derives from
— the F / S / R rules in §4–§7 are *how* the agent satisfies them. They are
**methodology invariants**, deliberately *not* added to the lifecycle
[Investigation Invariant Enforcement Matrix](./investigation-invariants.md)
(which indexes state-machine rules sourced from the lifecycle doc). They are a
distinct concern and live here; they are promoted to a methodology-invariant
registry — with a real enforcement tier + pinning test — as each is implemented
(§9). Terms are defined in §2.

Two meta-rules govern the set:

- **Necessity (Gate 1):** if the property does not hold, the diagnostic result is
  ruined. Each is non-negotiable in that sense.
- **Graceful denial (Gate 2):** enforcing an invariant must never stall or crash
  the flow. Every invariant has a defined exit when it cannot be satisfied (keep
  searching / escalate / CLOSE). *An invariant without a graceful denial is not
  invariant-grade.*

A structural constraint governs *where* enforcement may live, to stay consistent
with the lifecycle registry's **INV-16** (the LLM is the sole authority for
milestone *advancement*): these invariants act in the **engine-derive / veto
lane** — the engine *derives* assessment signals (`cause_state`, chain / node
validation) and *withholds or strips* invalid LLM emissions (precedent: INV-22
`cause_state` derivation, INV-23 surgical strip). The engine never *advances* an
LLM-owned gate milestone on the LLM's behalf.

| ID | Invariant (must always hold) | Graceful denial (Gate-2 exit) | Planned enforcement |
|----|------------------------------|-------------------------------|---------------------|
| **M1** | A node is a **candidate root cause** only if it is the *terminal* node of its chain (no controllable upstream node in scope, R7) **and** *actionable* (a performable, independent remediation can be named). | Unmet → node stays an intermediate / candidate state; search continues. | Schema + engine-guard |
| **M2** | A root cause is marked **confirmed** ("verified") only with **counterfactual** evidence — removing it removed `D` (`causal_absence_evidence`). *Gone ⇒ problem gone.* | Counterfactual unreachable → **CLOSE** on `symptom_absence`, never hang. | Engine-guard, **built**: resolution gate + the §9.5 assurance grades (`CONFIRMED` requires a root-linked counterfactual; the engine mirror caps at `CONFIDENT`/0.8 without one) |
| **M3** | Every **hypothesis is a causal chain** terminating in a (possibly-candidate) **root-cause node** proposing a mechanism — distinct from `D` and from its intermediate states. A bare intermediate state / symptom-restatement is not a hypothesis. *(Successor to retired INV-17.)* | Enforced at the **validation / solution-attach checkpoint**, not at creation — a partial chain may exist with no root yet (lazy expansion, §8.2). | Schema (checkpoint) |
| **M4** | A node transitions to **validated** only via empirical evidence (§7.1) **or** deduction over a *certified-exhaustive* set (§7.1.1) — never by assertion, inference, or correlation. | Unobservable + non-exhaustive → node stays candidate; keep searching or escalate. | Engine-guard (extends INV-23) |
| **M5** | A **remediation `Solution`** may not exist before its root is at least *mechanistically validated*; a diagnostic action is never a Solution. *(Mitigation / defensive state-interceptions are exempt — they precede a known root by design.)* | Pre-validation actions are recorded as **tests / mitigations**, not solutions; flow continues. | Engine-guard (veto, extends INV-23) |
| **M6** | **Counterfactual disconfirmation** (fix applied, `D` persists / cause already absent) **demotes** the chain root `validated → candidate`, attaches refuting evidence, and recomputes `cause_state` — **deterministically, in the engine**, not awaiting an LLM signal. No "verified" conclusion survives its own disproof. | New evidence required before re-validating (§7.3); exhaustion → re-expand (R6) / escalate. | Engine (deterministic, derive lane) |
| **M7** | An **AND-node** is `validated` ⇔ *all* co-necessary members are validated; refuting *any one* member refutes the chain. | Withholds validation until members prove out (normal flow); any refutation prunes. | Schema + engine |

Each F / S / R rule in §4–§7 exists to make one or more of these hold; §9
specifies the engine surfaces that enforce them.

---

## 1. Core Purpose

The sole purpose of forming a hypothesis is to identify the **confirmed root
cause** of an active problem so that a durable remediation can be executed.

A direct corollary governs the whole methodology: **until a root cause is
empirically confirmed, there are only _tests_, never _solutions_.** A diagnostic
action ("check the NetworkPolicy", "capture the pod logs") produces *evidence
about a hypothesis*; it is not a solution and must never be recorded as one.
Conflating the two is what lets milestones (`solution_proposed`,
`solution_accepted`) fire over an investigation that has found nothing.

---

## 2. Vocabulary (the building blocks)

These definitions are deliberately strict. Each one closes a specific way the
agent has been observed to drift.

| Term | Definition | What it is **not** |
|------|------------|--------------------|
| **Active problem (`D`)** | The observed top-level failure under investigation. Identical across every hypothesis chain. | — |
| **Intermediate state** | An observed (or observable) effect on the causal path between a cause and `D`. Each is itself an effect of the node below it. | Never a root cause. A restated symptom is an intermediate state, not a hypothesis. |
| **Root cause** | A node satisfying **both**: (a) a specific, independent, performable action permanently eliminates `D`; **and** (b) it is not itself the consequence of a controllable upstream node within scope (the *why-stopping rule*, [§7.5](#75-the-why-stopping-rule-and-its-identity-with-the-mitigation-insert)). | A node that merely *correlates* with `D`; a node where a fix exists but a controllable cause sits above it (that's an intermediate state — see the defensive-fix quadrant in [§7.4](#74-intervention-quadrants)). |
| **Hypothesis** | A **causal chain**: an ordered path `root → [intermediate states…] → D`, optionally with AND-sets of co-necessary conditions. | Not a single sentence. Not a symptom restatement. |
| **Test (diagnostic step)** | An action whose output is *evidence that discriminates between hypotheses*. | Never a fix. |
| **Solution** | An action that removes a **confirmed** root cause. Exists only after confirmation. | — |

**A hypothesis is a chain, not a claim.** This is the central shift. The four
"hypotheses" the agent produced in the worked example ([§10](#10-worked-example)) were
uncomparable precisely because they were flat sentences at inconsistent causal
layers. A chain is comparable, prunable, and testable rung by rung.

---

## 3. The 2D Construct (the diagnostic roadmap)

The troubleshooting search space is a two-dimensional map:

```text
                  ◄─── HORIZONTAL: parallel candidate root causes (OR) ───►
                  [Root Cause A]        [Root Cause B]        [Root Cause C]
                        │                     │                     │
  ▲   (Layer 1)   [State A-1]           [State B-1]           [State C-1]
  │                     │                     │                     │
VERTICAL (Layer 2)   [State A-2]           [State B-2]           [State C-2]
  │                     │                     │                     │
  ▼   (Problem)   [   D   ]             [   D   ]             [   D   ]
```

- **Horizontal dimension** — the set of *independent, alternative* candidate
  root causes. These are OR-ed: any one of them could produce `D`.
- **Vertical dimension (causal ladders)** — the chain of intermediate states
  linking a specific root cause down to `D`.

### 3.1 The grid is a view; the engine stores a DAG

The grid is the right *communication* model, but real failures are not a stack
of disjoint ladders. Two realities force a graph underneath:

- **Convergence.** Distinct roots commonly share a downstream intermediate
  state (the closer to `D`, the more chains overlap). The grid's separate
  columns are a projection; the store is a DAG rooted at `D`.
- **Conjunction (AND).** Many failures fire only when two conditions hold
  *together*. A pure single-parent ladder cannot represent this. A rung may
  therefore be an **AND-set** of co-necessary conditions.

So the underlying structure is a fault-tree/DAG rooted at `D`, with **OR** at
the horizontal (alternative roots) and **AND** at co-necessary rungs.

---

## 4. Formation Rules

How candidate roots are *born*. This is where free-association is prevented.

- **F1 — Single anchor.** Every chain terminates at the *same*, precisely-stated
  active problem `D`.
- **F2 — Mechanism, not restatement.** Each candidate must state a *mechanism*
  ("because X, therefore the node above") — an answer to "why", not a rephrasing
  of the effect. A node that merely re-describes `D` is an intermediate state,
  not a hypothesis.
- **F3 — Signature screening (free falsification).** Before a candidate enters
  the map, confirm its mechanism would actually produce the **observed
  signature** of `D`. A *timeout* signature is not a *connection-refused* is not
  an *authentication-failed* is not a *post-connect warning*; each implies a
  different mechanism family. Candidates whose mechanism cannot produce the
  observed signature are rejected at zero test cost — no user action required.
- **F4 — Family completeness.** Generate candidates against a checklist of
  failure families — *config • network/reachability • resource/capacity •
  dependency-readiness • data/state • code/logic • permission/identity •
  environment/version* — then instantiate **only** the families consistent with
  the signature (F3). This guards against blind spots without flooding the map
  with signature-incompatible noise. **F4 is a formation *guideline* the LLM
  follows, not an engine-enforced rule** — measurement on the recommended STRICT
  model found it does not tunnel at formation, so F4 is not wired as a gate (see
  the F4 entry under [Status](#status)). Where rules below treat F4's
  exhaustiveness certification as a precondition (§7.1.1, §8.2), read it as an
  LLM-judgment precondition, not an engine guarantee.

---

## 5. Structural Rules

- **S1 — Single causal arrow (vertical integrity).** Each link is a direct
  cause→effect step: `root → state₁ → state₂ → D`, each step the direct
  consequence of the one below it — **except** explicit AND-sets (§3.1), where a
  rung lists co-necessary conditions.
  - **AND-gate semantics are two duals, not one rule:**
    - *Disproof (asymmetric, cheap):* refuting **any one** co-necessary member
      refutes the child node and the whole chain.
    - *Proof (symmetric, strict):* the child node validates **only when every**
      co-necessary member is *empirically* validated (§7.1). While any member is
      still a candidate, the child stays a candidate — it cannot transition to
      validated.
  - *Search implication (invalidation-first):* within an AND-set, test the member
    **most likely to be false first** — it is the cheapest route to refuting the
    entire chain (a corollary of R1).
- **S2 — Roots are MECE; chains may converge.** The *root nodes* are distinct,
  mutually-exclusive origins. Their chains are free to share intermediate states
  as they approach `D` (this is what makes intersection pruning, R3, possible).
  Do not promote an intermediate state from one chain into another chain as if
  it were a separate root.
- **S3 — Build backward from `D`.** The map is *discovered*, not descended.
  The primitive operation is: take the lowest open node and ask "what state, if
  true, would *directly* produce this node?" The candidates become its parents;
  screen them (F3), test the most informative (R-rules), recurse on the
  survivor. The grid emerges from repeated backward expansion.

---

## 6. Search & Verification Rules (invalidation-first)

The dominant strategy is **elimination**. A confirmed state only *narrows* the
space; a falsified node *prunes an entire sub-forest* in one cheap, definitive
move. The agent therefore prefers the test most likely to *eliminate the most
chains per unit cost*.

> **Status — conceptual principle, not enforced.** The rules below describe how a
> competent diagnostician (the LLM) *reasons*; the engine and prompt do **not**
> enforce them. This is deliberate. Invalidation-first is a *search-efficiency*
> lever (it shortens the path on a genuine multi-cause differential); it does not
> hold either soundness guarantee — NO INCORRECT CONCLUSION is held by the
> empirical-only validation layer (§7.1, M4/M5/M6) and NO COLLAPSE by the
> anchoring/stagnation-decay machinery, both *independent* of search order. So the
> cost of under-eliminating is extra turns, not a wrong answer.
>
> **Rejected alternative — prompt enforcement (tried post-F3, removed).** A
> prompt rule telling the LLM to pick the most-eliminating test was added to the
> chain-emission block and removed: an A/B sim showed it *regressed node
> emission* (the "where chains diverge" framing pushed the model to restate
> distinguishing causes as duplicate nodes, re-triggering the node-identity loop
> §9.2 had closed — 3/3 runs vs 0/2 baseline) with **no demonstrated benefit**
> (every scenario was single-cause-determined, so it could never exercise the
> differential the rule targets). Lesson: a *search* rule does not belong in the
> *emission* instructions, and prompt-first fits *formation* rules (F3), not
> test-selection.
>
> **Bar for reintroduction.** Bring invalidation-first back as an engine/prompt
> mechanism only after a sim or eval *first demonstrates the failure it targets* —
> an agent that, on a genuinely ambiguous multi-cause case, anchors on a
> confirmatory path and wastes turns (or hits a budget) instead of running the
> discriminating test. With that evidence in hand, prefer a shape that keeps the
> *fact* in the engine and the *judgment* in the LLM (e.g. the engine renders the
> divergence point — a node on one chain but not another — into context and the
> LLM picks the feasible test), or an offline eval signal; not a prompt rule in
> the emission path.

- **R1 — Expected information per cost.** Choose each test to maximize
  `(chains eliminated if the test fails × P(it fails)) ÷ test cost`. This single
  rule subsumes two corollaries:
  - **R2 — Cost-weighted rung skipping.** A cheaper, higher rung that would
    prune the ladder is tested before a more expensive lower rung.
  - **R3 — Intersection pruning.** A state shared by multiple chains scores high
    on "chains eliminated" — disproving it kills every chain that routes through
    it. **Qualifier:** intersection pruning only pays when the shared node's
    truth is *genuinely uncertain*. The proximate effects right above `D` are
    almost always *present* (that is why `D` is happening), so testing them to
    disprove them teaches nothing. The high-value shared targets are *mid-ladder
    and uncertain*.
- **R4 — Test where chains diverge, not where they converge.** Converged nodes
  near `D` are non-discriminating (consistent with many roots). Information lives
  where the candidate set splits.
- **R5 — Untestable rung.** If a rung is neither provable nor disprovable (data
  gone, user cannot run the command, signal unobservable): seek a **proxy
  observation** for the same prediction; if none exists, mark the rung
  `inconclusive`, lower the chain's priority, and pursue a cheaper chain. **Never
  fabricate a verdict on an unobservable rung.**
- **R6 — Map exhaustion → re-expand.** If *every* chain is falsified, the
  horizontal set was incomplete (a family was missed). Re-run formation (F4)
  rather than re-testing dead chains.

### 6.1 The likelihood layer

The 2D map is the logical *skeleton*; a probabilistic layer rides on top. Each
chain carries a current belief; discriminating evidence updates it. "Validated"
means a chain crossed a high threshold **via empirical observation** (R-rules +
[§7](#7-remediation-mitigation-and-defensive-fixes)), never via assertion or
per-mention increment. This is what the existing confidence machinery
([data-models §3](./investigation-data-models.md#3-hypothesis-workflow)) should
compute over — *chain-level belief*, not a count of supporting mentions.

**Belief propagates by gate type.** An **AND** rung is conjunctive: the child's
belief is bounded by its *weakest* co-necessary parent (min / product) — an
AND-node is never more certain than its least-certain member, which is the
arithmetic form of the symmetric proof rule in S1. The **OR** horizontal is
disjunctive: `D`'s explained-belief combines its alternative roots (noisy-OR /
max). With these duals in place, the symmetric AND rule (S1) and deductive
validation (§7.1.1) fall out of the arithmetic rather than needing to be bolted
on as special cases.

**Decay counts investigation turns, not wall-clock turns.** Stagnation decay
(`belief × 0.85^iterations_without_progress`) and anchoring detection key on
`iterations_without_progress`, which must advance **only on investigation turns**
— a turn where a node was *eligible* to progress and didn't (new evidence
analyzed, a proposed test's result returned, or a node-state transition
attempted). Turns spent waiting on the user, answering clarifying questions, or
coaching how to run a command (`TurnOutcome.CONVERSATION`) are **not** progress
opportunities and must not increment the counter. Otherwise a correct hypothesis
decays on user latency alone — a three-turn network-capture detour would penalize
the very chain it is testing. The counter is per-node and resets at
`last_progress_at_turn`.

---

## 7. Validation, Treatment, and the Intervention Quadrants

### 7.1 Empirical validation only

A hypothesis — intermediate *or* root — is validated only by **direct,
observable facts** matching its predicted state (an exact log/stack trace, a
return code, a reproducer's output). Assumption, inference, and secondary
correlation are not validation. Engine-side, a node reaches VALIDATED only on a
**causally-grounding SUPPORTS** link (`derive_node_states` → `_node_evidence_tally`,
`causal_graph.py`), net of refutations and behind the M7 AND-gate.

A SUPPORTS link is causally grounding **only** when its backing datum is
categorized `CAUSAL_EVIDENCE` — a direct observable fact matching the node's
predicted state — **and** the link's own declared `stance_confidence` is at or
above `CAUSAL_STANCE_CONFIDENCE_MIN`: a link the model itself marks as
doubtful is correlational color, not grounding (it still counts in the
generic supports/refutes arithmetic). Links backed by weaker categories
(e.g. `SYMPTOM_EVIDENCE`) inform the narrative but never validate a node.

**Independent-support bar (ROOT-only).** A non-ROOT rung validates on ≥1
causally-grounding link. A ROOT — the node that mints a conclusion — requires
`ROOT_INDEPENDENT_CAUSAL_SUPPORT_MIN` (= 2) **independent** causally-grounding
supports: distinct evidence rows whose contents are not mutual restatements of
each other (`_EVIDENCE_MIRROR_JACCARD` over content tokens of
`summary + extract`; re-recording one datum twice is one observation, and rows
too short to judge collapse into a single bucket). The rationale is the trust
boundary: with the runbook-provenance arm decommissioned (#658), *every*
causal link is an LLM self-labeled claim, so a single self-certified datum
must not conclude a case (#573 / #656 DF-1) — corroboration is the only
engine-computable difficulty knob left. Two exceptions, both principled:

- A **counterfactually CONFIRMED** root (engine-stamped `causal_absence`
  SUPPORTS, *gone ⇒ gone*) satisfies the bar outright — M2's top grade
  dominates empirical counting, the stamp is engine-only (ingest strips LLM
  attempts), and a confirmed root recomputed post-RESOLVED must not demote
  for having validated on fewer supports under an earlier bar.
- The **deductive lane** (§7.1.1) is untouched: exclusion carries its own
  strict guards and validates a survivor with no supports of its own.

The bar is recompute-honest (NOT grandfathered, unlike the restatement
guard): support counts are monotone in evidence — links only accumulate, and
the only decreases (a stance flip or a lowered `stance_confidence` on
re-link) are genuine re-assessments that *should* demote. A held root sits at
INCONCLUSIVE — a live candidate (`cause_state=CANDIDATES`), never a refuted
one. Block events increment `root_validation_blocked_support_count_total`
(fires when the generic bar passed and real causal-category support exists;
a root failing both this bar and the restatement guard is attributed here).

The same prior-vs-evidence discipline holds on the flat axis: a direct LLM
likelihood update on a hypothesis with **no supporting evidence links** is
capped at `NEW_HYPOTHESIS_MAX_PRIOR` (`update_hypothesis_likelihood`, #573
B1) — the creation-time cap was otherwise a fiat lever away from the
`CAUSE_IDENTIFIED_LIKELIHOOD` gate it exists to protect. The model is told to
record and link the observation instead of re-asserting a larger number.

**Restatement guard (ROOT-only, an ENTRY bar on validation).** A ROOT whose
statement carries less than `ROOT_NOVELTY_MIN_FRACTION` novel content tokens
beyond the case frame — the problem anchors (PROBLEM node statement, verified
symptom) plus the OTHER standing hypotheses' statements — is never ADMITTED to
VALIDATED. The symptom dressed up as a cause carries no explanatory depth, and
the LLM labels its own evidence, so a restating root + one self-labeled
`CAUSAL_EVIDENCE` link is exactly the shape of a false conclusion (#656 turn
6: a disjunction of the case's two untested hypotheses restating the symptom —
every token already in the case frame — validated off one link and minted a
0.9 "verified" conclusion).

The guard has one predicate (`root_restates_case_frame`) and two enforcement
points, both **entry bars**:

- **Empirical lane** (`derive_node_states`): a restating ROOT that would
  otherwise validate holds at INCONCLUSIVE. Each *block event* (the state
  transition, never re-checks) increments
  `root_validation_blocked_restatement_total`. A root that has ALREADY
  validated is ruled by its evidence alone — the guard never demotes it, so a
  later sibling emission with overlapping wording cannot retract a correct
  conclusion (monotonicity), and pre-guard persisted conclusions are
  deliberately grandfathered (closed cases never recompute; their confidence
  is governed by the assurance-grade work tracked on #656).
- **Deductive lane** (`validate_by_exclusion`): a restating survivor is not
  stamped (graceful denial) — excluding the alternatives without stating a
  mechanism would conclude "the problem causes itself".

The conclusion mirror (`synthesize_rcc_from_validated_root`) carries NO
restatement check by design: no restating root can freshly validate, and a
root that stands VALIDATED must be mirrorable or `cause_state=IDENTIFIED`
would split from a permanently-absent conclusion. `retract_stale_engine_rcc`
clears an engine-authored conclusion whose root demotes for *evidence*
reasons.

**Frame ownership.** A hypothesis is excluded from a root's frame when it is
attached to that root, or when it is unattached but *mutually mirrors* it
(Jaccard ≥ `_FRAME_OWNER_JACCARD`) — the normal chain-emission shape during
attachment lag must not let a root's own not-yet-linked hypothesis block it.
One-way containment deliberately stays in the frame: the #656 disjunction root
fully contains each sibling hypothesis but mutually mirrors none, which is
what catches the incident.

The guard is ROOT-scoped (rungs adjacent to `D` legitimately paraphrase) and
its bar is its own knob, decoupled from the orphan-reattach threshold. The
calibration lives in one executable home — `test_restatement_guard_calibration.py`
(corpus false-positive pin, incident/verbatim true-positive pins, the
cause-contaminated-anchor boundary, the filler-padding known escape, and the
sibling-frame dilution bound).

*Known limits (by design):* the check is lexical — synonym paraphrases and
filler-padded restatements read as novel and pass; a disjunction root in a
case with no standing hypotheses passes; dense same-domain sibling frames can
DELAY (never permanently block) a terse mechanism root's validation. The
guard is one layer of the #656 defense; the independent-support bar (above)
and the assurance-grade caps (§9.5) are the layers that have landed, MECE
arbitration is tracked on #656.

### 7.1.1 Deductive validation (proof by exclusion)

Some root causes are *unobservable in principle* — microsecond race conditions,
silent memory corruption, transient network blips — and leave no direct
footprint to match against §7.1. To keep the agent from stalling forever on an
unobservable cause, it may validate by **exclusion**:

> If a node has `N` mutually-exclusive parent paths (an OR-set) and `N−1` are
> **empirically refuted** (§7.1), the remaining path is validated by deduction —
> even if its root cannot be directly observed.

Four guards keep this from becoming a fallacy:

1. **Exhaustiveness is mandatory.** Proof-by-exclusion is only as sound as the
   differential is complete. It is permitted **only when the OR-set is certified
   collectively exhaustive** — the family-completeness sweep (F4) has run and
   found no further signature-consistent family, and the roots are genuinely MECE
   (S2). A non-exhaustive elimination simply concludes the wrong survivor. **F4
   is an LLM-judgment guideline, not an engine-enforced sweep** (see
   [Status](#status)), so this exhaustiveness is asserted by the agent, not
   guaranteed by the engine; the deductive-exclusion cap (#3) and the mandatory
   counterfactual before resolution (§7.1) are the engine-side backstops that
   keep a missed family from silently resolving the wrong cause.
2. **The eliminations must be empirical.** Deduce only from refutations that
   themselves meet §7.1 — never from assumed or inferred eliminations.
3. **Strict exclusion — refutation must be absolute, not partial.** Proof by
   exclusion is acutely noise-sensitive: deducing the survivor is "100% valid"
   while a sibling is only *75% refuted* is mathematically unsafe (the survivor's
   deduced belief is at most the product of the siblings' exclusion strengths). A
   sibling counts as *excluded* **only** when its refutation is absolute —
   `node_state=REFUTED` **and** `belief ≤ DEDUCTIVE_EXCLUSION_MAX_BELIEF` (a small
   constant, e.g. `0.05`). If **any** of the `N−1` is merely `INCONCLUSIVE` or
   weakly refuted (`belief` above the bar), deductive validation **does not fire**
   — the survivor stays `CANDIDATE` (graceful denial: keep searching). This makes
   the deduction binary, matching its use as an invariant (M4) rather than a
   probabilistic estimate.
4. **Deductive validation is *mechanistic* grade only** (§7.2). It unlocks
   TREATMENT but never RESOLVED on its own; **counterfactual confirmation** (the
   fix works) is still required to resolve. And if treatment then *fails* (§7.3)
   on a deductively-validated cause, that is strong evidence the OR-set was **not**
   exhaustive — the correct response is to **re-expand the differential (R6)**,
   not merely to demote the surviving chain.

**How it is wired (the division of labour).** The engine cannot compute guard #1
— F4 exhaustiveness is LLM judgment (see [Status](#status)), so a pure derivation
function has no sound source for it. So the guards split by who can supply each:
the **agent** certifies exhaustiveness by naming the survivor in a
`deductive_validations` assertion (the one un-computable guard, opt-in, rare, on
the unobservable-cause path only); the **engine** owns every guard it can check.
`causal_graph.validate_by_exclusion` runs each turn right after `derive_node_states`
(so the siblings' states are settled), and for each asserted survivor calls
`deductively_validated(..., exhaustive=True)` — which re-checks ≥2 members and that
every non-survivor is *absolutely* excluded — before stamping
`validation_method=DEDUCTIVE`. Guard #3's "absolute" bar is itself engine-computed:
`derive_node_states` drives a sibling's `belief` to `0` **only** on a counterfactual
(absence-based) refutation, so a merely-correlational net-refute stays above
`DEDUCTIVE_EXCLUSION_MAX_BELIEF` and blocks the deduction. A mis-asserted
exhaustiveness therefore cannot fabricate a validation on its own — the differential
must have genuinely collapsed — and guard #4 (counterfactual before RESOLVED, and
harvest is RESOLVED-only) is the downstream backstop against a missed family.

### 7.2 Two grades of root-cause confidence

| Grade | Established by | Unlocks |
|-------|----------------|---------|
| **Mechanistically validated** | The cause and its mechanism are *observed* (pre-intervention). | Entry to **TREATMENT** — a solution may now be proposed. |
| **Counterfactually confirmed** | *Removing* the cause makes `D` disappear (post-intervention). Recorded as `causal_absence_evidence`. | **RESOLVED** — the resolution gate. |

These grades resolve the latent contradiction in "transition to treatment the
moment the root cause is validated, but treatment may fail." Treatment begins at
*mechanistic* validation; the case resolves only at *counterfactual*
confirmation. The window between them is exactly where a fix can fail.

### 7.3 A failed treatment is a falsification event

When a fix is applied and `D` persists — or when the user reports the candidate
cause was *already absent/correct* and `D` still occurred — that is
**counterfactual disconfirmation**. The engine must:

1. **Demote** the root from "validated" back to "candidate".
2. Attach the failed fix as **refuting evidence** on that chain.
3. **Check for state contamination — before resuming.** A state-mutating action
   — a failed fix, a *partially*-applied one, even a mitigation or a
   state-changing diagnostic command — can alter the system (left-over locks, a
   half-applied migration, dirty tables, a stuck process). When it does, `D` is
   no longer the same `D` that was diagnosed, and **evidence collected before the
   action may be stale.** The engine must:
   - Detect whether the action mutated state in a way that could invalidate prior
     evidence; if so, **mark pre-action evidence stale** (re-verify before reuse)
     and treat what follows as a new *state epoch*.
   - If contamination is suspected, **prioritize a cleanup/rollback to restore the
     baseline** before continuing diagnosis.
   - If the mutation is **irreversible** (e.g., a one-way migration), do not
     assume the original baseline — **re-baseline**: re-characterize `D` against
     the new state, and watch for a *new* problem `D′` the action introduced
     (cf. framework §7.4, "new symptoms in TREATMENT").
4. **Resume horizontal search** over the remaining / re-expanded candidates.

It must **not** freeze the old conclusion as "verified" nor free-associate an
unrelated new theory. (This is the precise failure in [§10](#10-worked-example):
the user disproved the NetworkPolicy chain at turn 28 and the agent neither
demoted it nor refuted it — it pivoted to an unrelated log warning.)

Distinguish two failure modes before resuming:

- **Implementation error** (right theory, wrong command/step) → correct the
  action and re-attempt; the chain is *not* refuted.
- **Theory wrong** (the cause was absent or its removal didn't help) → refute the
  chain; **new evidence is required** before forming a replacement (the original
  evidence already produced a failed fix and cannot be reprocessed for a
  different result).

### 7.4 Intervention quadrants

Two *independent* axes — **where** you intervene and **how durable** the
intervention is — give four outcomes. The earlier two-way "remediation vs
mitigation" split collapsed them and had no name for the common middle case.

| | **Permanent** | **Temporary** |
|---|---|---|
| **At root** | **Remediation** — the ideal: permanently eliminates `D` at its origin. | (rare) |
| **At intermediate** | **Defensive fix** — durable, ships to prod, but does not address the upstream cause (e.g. add a retry / widen a timeout so a slow dependency no longer breaks the job). | **Mitigation** — temporarily intercepts an intermediate state to suppress `D` under current constraints (lack of permission, vendor outage). See [Lifecycle Logic §2](./investigation-lifecycle-logic.md#2-mitigation-as-an-insert). |

- **R7 — Root-cause test (remediation).** A root cause is reached iff a specific,
  independent, performable action can permanently eliminate `D` **and** no
  controllable upstream node remains (the why-stopping rule).
- **R8 — State interception (mitigation/defensive).** When the root cannot be
  addressed under current constraints, intercept an intermediate state. A
  *temporary* interception is mitigation (later reverted); a *permanent* one is
  a defensive fix (kept). Record which.
- **R9 — Loop breaking.** If the causal chain forms a cycle (`X→Y→Z→X`), no top
  of the ladder exists. Abandon the search for a static root, treat the loop
  itself as the problem, and apply R8 to break the cycle and stabilize.

### 7.5 The why-stopping rule, and its identity with the mitigation insert

Where to stop descending the ladder (declaring a node the root cause) and whether
to intercept an intermediate rung are the *same two orthogonal questions* the
unified flow already separates as **Axis A** and **Axis B**
([Lifecycle Logic §2.1](./investigation-lifecycle-logic.md#21-two-orthogonal-axes-why-the-fork-was-wrong)).
A **mitigation insert** *is* a **temporary state interception** (R8); this section
states that identity in the methodology's terms.

**Axis A (certainty + feasibility) governs the why-stopping rule** — how deep we descend:

- **RCA feasible (`rca_infeasible=False`, default):** descend to the **terminal
  controllable cause** — the deepest node passing the root-cause test (R7) with no
  controllable upstream node remaining in scope. RESOLVED requires reaching it
  *and* counterfactual confirmation (`causal_absence_evidence`, §7.2).
- **RCA infeasible (`rca_infeasible=True` — black box, EOL, intractable condition,
  or user declines):** the ladder is **capped** at the deepest *reachable,
  controllable* rung. The accepted terminal action is a **state interception** (R8)
  at an intermediate node; the root is never eliminated, so the case CLOSES
  (`closed_after_investigation`) on *symptom* absence, not *causal* absence.

**Axis B (impact-now gap) governs the temporary interception** — whether, *en
route*, we intercept an intermediate rung to suppress `D` now. This is the
mitigation insert. It is independent of where the root is: it buys time, then the
flow forwards (continue descending / deliberate the solution / hand off, per
[§2.3](./investigation-lifecycle-logic.md#23-mitigation-triggers-and-forwarding)).

This subsumes the earlier "strategy-dependent" proposal: the driver is **Axis-A
feasibility, not strategy**. `post_mortem` cases simply tend to have no Axis-B gap
and feasible RCA (→ pursue the terminal cause); live incidents tend to have an
Axis-B gap (→ mitigation insert). Strategy *correlates* with the axes; it does not
override them.

**Quadrant → disposition** (closes the loop with the absence-evidence resolution gate):

| Intervention quadrant (§7.4) | Unified-flow term | Disposition | Absence evidence |
|---|---|---|---|
| Remediation (perm @ root) | permanent fix + verified root cause | **RESOLVED** | causal_absence |
| Defensive fix (perm @ intermediate) | accepted permanent workaround (often `rca_infeasible`) | **CLOSED** (`closed_after_investigation`) | symptom_absence |
| Mitigation (temp @ intermediate) | mitigation insert | buy time → forward → RESOLVED / CLOSED (revert reminder) | symptom_absence (interim) |
| Loop-break (R9) | mitigate the cycle | **CLOSED** / stabilized | symptom_absence |

---

## 8. Resolved Design Decisions

Settled 2026-06-20.

1. **The why-stopping rule (R7b) — governed by Axis A, identical to the mitigation
   insert.** "Root cause" defaults to the **terminal controllable cause** when RCA
   is feasible; `rca_infeasible` caps the ladder at the deepest reachable
   controllable rung, where a state interception (defensive fix or mitigation)
   becomes the accepted terminal action. This is the same Axis-A / Axis-B structure
   as the unified flow — see [§7.5](#75-the-why-stopping-rule-and-its-identity-with-the-mitigation-insert).
   *(Supersedes the earlier strategy-dependent proposal: strategy correlates with
   the axes, it does not drive the rule.)*
2. **Lazy map construction.** The agent expands **backward one rung at a time**
   (S3) with a **periodic family-completeness sweep** (F4). Lazy is more practical
   and token-efficient; the completeness sweep is what keeps deductive validation
   (§7.1.1) legal under lazy expansion.
3. **AND-joins from the start.** Conjunctive causes are modeled now, not deferred.
   This is the largest schema change, and it forces the dependent components —
   schema, assessment variables, milestones, confidence — to move together from
   one model. That alignment is specified in [§9](#9-engine-alignment).

---

## 9. Engine Alignment

The methodology reshapes the data model, and (per decision 3) the dependent
components must move together from one model. This is the implementation surface;
it is built incrementally but specified coherently here so schema, assessment
variables, milestones, and confidence stay consistent.

### 9.1 Schema — causal graph with AND-joins

- The investigation owns **one causal DAG rooted at `D`**. Nodes are **causal
  nodes**; edges are cause→effect ("produces").
- A **causal node** carries `statement`, `node_type` ∈ {root, intermediate},
  `state` ∈ {candidate, validated, refuted, inconclusive}, and its direct-cause
  edges. The per-node `state` is new (today only the whole hypothesis has state).
- **AND-set:** a node whose occurrence requires *all* of a listed group of
  co-necessary direct causes (an `and_group` on the incoming edges).
  **OR / alternatives:** distinct chains that independently produce the same effect
  node (horizontal convergence, S2).
- A **`Hypothesis` becomes a chain** — a root→`D` path through the DAG. The
  existing `HypothesisState` (CAPTURED / ACTIVE / VALIDATED / REFUTED /
  INCONCLUSIVE / RETIRED, [data-models §3](./investigation-data-models.md#3-hypothesis-workflow))
  now describes the *chain*; node `state` is the finer-grained rung signal.
- **Evidence links target nodes, not the whole chain.** The `hypothesis_evidence`
  junction (`HypothesisEvidenceLink`) gains a `node_id`, so a SUPPORTS / REFUTES
  stance bears on the specific rung it tests. This is what makes step-by-step
  descent (S3) and symmetric AND-validation (S1) computable.
- **`solutions.hypothesis_id`** (currently unused) is wired, plus a `node_id` +
  quadrant tag (§7.4), so a chain's demotion (§7.3) retires its dependent solutions
  and a defensive-fix / mitigation records which rung it intercepts.

### 9.2 Assessment variables — `cause_state`

`cause_state` derivation aligns to chain / node states:

- **IDENTIFIED** — some chain's **root node is validated**, mechanistically (§7.1)
  or deductively (§7.1.1). This is mechanistic grade; it unlocks solution work.
- **CANDIDATES** — ≥2 ACTIVE chains (preserves the existing ≥2-active derivation,
  [§2.5 decision 4](./investigation-lifecycle-logic.md#25-design-decisions-and-open-follow-ons),
  now counting chains).
- **UNKNOWN** — otherwise.
- **AND gate:** a chain's root cannot be validated until *every* AND-member on its
  path is validated (S1 symmetric proof), so `IDENTIFIED` is never reached on a
  half-proven conjunctive chain.

### 9.3 Milestones & dispositions

- The two validation grades (§7.2) map onto existing signals: **mechanistic
  validation → `cause_state=IDENTIFIED`** (enter solution / TREATMENT);
  **counterfactual confirmation → `causal_absence_evidence` → `solution_verified`
  → RESOLVED**.
- **Intervention quadrants ↔ dispositions** per the §7.5 table; the mitigation
  insert reuses the existing `progress.mitigation` record and gate milestones
  (`mitigation_accepted` / `mitigation_verified`).
- **Treatment-failure demotion (§7.3) is engine-deterministic.** On counterfactual
  disconfirmation the engine flips the chain root back to candidate
  (`HypothesisState → ACTIVE`), attaches REFUTES evidence to the failed node, runs
  the contamination check, and **recomputes `cause_state`** — it does not wait for
  the LLM to volunteer a refutation. This is the direct fix for the runtime-inert
  lifecycle the assessment found (the LLM never emitted the REFUTES signal, so
  nothing ever transitioned).

### 9.4 Confidence

*Design-intent — not yet built. The engine currently uses the framework-§6
`+0.15 / −0.20` per-evidence counter; the chain-level scheme below is the target.*

Chain-level belief replaces per-mention counting: **AND rung = min / product of
members; OR = noisy-OR / max** (§6.1). `HypothesisManager.update_likelihood_from_evidence`
would propagate node beliefs through the gates instead of summing
`+0.15 / −0.20` on a flat hypothesis.

### 9.5 Harvest assurance grade (which causes may seed the KB)

Resolving a case is one thing; turning it into a **reusable runbook** is a
stronger claim — a wrong cause that becomes a runbook misleads every future case
that retrieves it. So KB harvest carries NO INCORRECT CONCLUSION one step further:
only an **authority-grounded** cause may auto-seed knowledge.

`grade_cause_assurance(case)` (`cause_assurance.py`) classifies the identified
cause into three mutually-exclusive grades — the **M2 confirmation ladder** —
in one pass over its validated roots:

| Grade | Condition | May seed KB? |
|-------|-----------|--------------|
| `CONFIRMED` | ≥1 VALIDATED root borne out by a **counterfactual confirmation** — a SUPPORTS evidence link backed by a `causal_absence_evidence` row on that root (the cause was removed and `D` went with it; M2 *gone ⇒ gone*). | **Yes** |
| `MECHANISTIC` | ≥1 VALIDATED root (empirical rung evidence, §7.1, **or** a deductive derivation, [§7.1.1](#711-deductive-validation-proof-by-exclusion)), but none counterfactually confirmed. | No — ask the user to *confirm* the cause. |
| `NO_ROOT` | No VALIDATED root at all (a bare, LLM-authored `RootCauseConclusion` with no causal graph). | No — ask the user to *identify* a cause. |

Validation **method** never raises the grade: empirical and deductive
validation are both *mechanistic* (M2/M4). A deductive derivation is itself
assembled from LLM-mediated refutations plus an asserted-exhaustive
differential, so on its own it stays `MECHANISTIC`; only the counterfactual
outcome of actually removing the cause clears the top bar. (This settles the
harvest-authority question left open by the runbook-matcher retirement:
counterfactual confirmation succeeded the deductive-only `GROUNDED` grade,
issue #656.) The confirmation must be **linked to the root it confirms** — a
case-level `causal_absence` row with no bearing on the root does not confirm
it (the same bearing discipline as the counterfactual-refute arm).

Because the prompt's verify-turn contract records the resolution-confirming
`causal_absence` row as a stand-alone audit row (never linked), the **engine
attaches the confirmation link itself** — but only at **RESOLVED transition
execution**, on the user's explicit confirmation:
`confirm_root_from_resolution_absence` (called from the resolved-transition
executor) links an unlinked absence row to the **sole** standing validated
root (the confirm-side twin of the M6 failed-fix refute stamp) and
re-persists the grade. The row's mere appearance during investigation never
confirms anything — it is an LLM self-claim, and a premature "it's stable
now" row emitted mid-rollout (observed live) must not upgrade the grade. With
several simultaneously-validated roots the engine never guesses which cause
the fix removed — the case stays `MECHANISTIC` pending arbitration; a
REFUTES-linked absence row (a failed fix) never flips to confirmation.

The grade is **persisted** per turn on `InvestigationProgress.cause_assurance`
(the `verification_status` pattern — rides the progress blob, no migration).
That makes the grade × conclusion-confidence seam queryable, drives the
resolution report's assurance qualifier and the `cause_assurance` field on the
progress-transparency surface, and feeds the **over-claim seam warning**: a
recorded conclusion claiming *verified* while the grade is below `CONFIRMED`
is logged at WARNING on the transition into that state (edge-triggered via the
persisted `cause_overclaim` flag, so a standing over-claim warns once rather
than once per turn; the per-turn state stays visible in the DEBUG grounding
trace and the flag itself). This is the incident shape of issue #656; the
engine's own mirror can no longer produce it, so a hit is an LLM-authored
over-claim — LLM-conclusion retraction is a separate correction tracked on
that issue.

The grade also rules the **engine-synthesized conclusion's confidence** (§9.3
mirror): a `MECHANISTIC` root mints `CONFIDENT` at a fixed 0.8 — a *cap*, so
the LLM's own higher `root_cause_likelihood` cannot leak a mechanistic cause
into "verified" — and only a `CONFIRMED` root mints `VERIFIED` (floored at
0.9). A standing engine mirror whose confidence disagrees with the current
grade is re-minted (upgrade the turn confirmation arrives; correction of
pre-cap persisted over-claims).

`CONFIRMED` is the harvest bar, and both entry points refuse to seed an
unconfirmed cause — the `POST /knowledge/convert-from-case` API rejects with
422 before conversion, and the chat-side runbook **action** returns
`NOT_READY` (no draft) when the cause is not confirmed (the suggestion is
offered, but acting on it is gated). The full gate flow is in
[document-to-runbook-conversion.md §1.1](../knowledge-and-ai/document-to-runbook-conversion.md#11-soundness-gate-only-an-authority-grounded-cause-may-seed-the-kb-7).
The three-way grade is deliberate: a single positive bar (`CONFIRMED`) makes the
two held shapes (`MECHANISTIC`, `NO_ROOT`) distinguishable for the user-facing
ask, and unrepresentable as a "harvestable" state.

---

## 10. Worked Example (case_e970a5c24fe1)

**Active problem `D`:** *"The 'Deploy to on-prem' GitHub Actions job is failing,
preventing new code from being deployed."*

**The known causal chain (all observed effects, no root reached):**

```text
deploy job fails
└─ migration job "did not complete" (timed out)
   └─ migration pod cannot open a TCP connection to Postgres (psycopg2 timeout)
      └─ ??????   ← the open question; never answered (case ran 37 turns)
```

**What the agent produced — a flat list, not a roadmap:**

| Agent "hypothesis" | Defect under this methodology |
|---|---|
| 1. "cannot establish a connection to PostgreSQL" | **Symptom restatement** (F2). It is the bottom *known* node — the question, not an answer. |
| 3. "network issue / missing service / restrictive NetworkPolicy" | A real mechanism, but a **child of #1**, not a peer (S2 violated); also a bundle of two mechanisms (S1). |
| 4. "misconfiguration — wrong secret key or hostname" | Child of #1 too; **bundled** — "wrong host" is timeout-consistent, "wrong secret key" would throw an *auth error*, not a timeout (F3). |
| 2. "PostgreSQL collation version mismatch" | **Signature-screened out (F3):** a collation warning is emitted *after* a successful connect and cannot produce a connection *timeout*. Should never have entered the map. |

**The correctly-formed map** anchors on the open question — *"why does the connect
time out?"* — and produces MECE roots, screened by the timeout signature:

```text
why does the connect TIME OUT?  (refused / auth-error / post-connect signatures all screened out by F3)
 ├─ [network path blocked]  ──► (eliminated: user confirmed the NetworkPolicy was already correct, turn 28)
 ├─ [service / DNS misresolves]  ──► reaches wrong/no endpoint ──► timeout
 ├─ [DB not ready at connect time]  ─AND─ [job timeout too aggressive / no retry]  ──► deadline exceeded ──► timeout
 └─ [wrong host/port in job config]  ──► no listener at target ──► timeout
```

Two lessons the methodology encodes: (a) the *timeout signature alone* kills the
collation and wrong-password candidates **for free** (F3), and (b) the real
failure is plausibly an **AND-join** (slow/late DB *and* an unforgiving timeout),
which a single-arrow ladder cannot express (S1/§3.1). The agent reached neither
because it never built a chain — it produced sentences and called the act of
checking each one a "solution".

The collation-refresh churn (turns 30–37) also illustrates §7.3 **state
contamination**: the partial `REINDEX` / `REFRESH COLLATION VERSION` succeeded on
the `postgres` database while failing on `faultmaven` — a state-mutating action,
applied for a signature-impossible hypothesis, that left the system in a
different state than the one originally diagnosed.

---

## 11. Superseded Approaches

- **Flat single-sentence hypotheses** — superseded by hypothesis-as-chain (§2).
  They mix causal layers and cannot be pruned or compared.
- **Per-mention confidence counting** — superseded by chain-level belief from
  discriminating evidence (§6.1).
- **Two-way remediation/mitigation split** — superseded by the four intervention
  quadrants (§7.4), which name the permanent-at-intermediate (defensive-fix) case.
