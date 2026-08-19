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
flat-sentence structure and its transitional flag/bridge scaffolding were removed
in #487–#507; the flat VALIDATED/REFUTED state transition — a validation remnant
that silently coexisted with the graph until it surfaced on a solved case — was
removed in #695 (Defect A), making the causal graph the sole producer of a
VALIDATED hypothesis, enforced by `test_projection_is_the_sole_source_writer_of_validated`),
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
| **M6** | **Counterfactual disconfirmation** (fix applied, `D` persists) **demotes** the chain root `validated → candidate`, attaches refuting evidence, and recomputes `cause_state` — **deterministically, in the engine**, not awaiting an LLM signal. No "verified" conclusion survives its own disproof. Its FAILED-FIX arm is **destructive and extra-graphical**, so that arm fires only on preconditions the case record ESTABLISHES — an executed SOLUTION, an observed persistence after it, and no standing resolution confirmation (INV-42). The EVIDENCE arm (a hypothesis refuted/net-refuted by its own links) is graph-grounded and demotes unconditionally. Either way the durable record states an engine *inference with provenance*, never an observation it did not make. | New evidence required before re-validating (§7.3); exhaustion → re-expand (R6) / escalate. | Engine (deterministic, derive lane) |
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

One exception, at the flat-hypothesis layer: an ACTIVE hypothesis that *no* turn
ever touches gets no investigation-turn increment (nothing engages it), so it
would otherwise sit at its prior forever — never decaying, never tripping
anchoring. The housekeeping loop closes that gap with an origin-blind, age-based
stagnation sweep (`advance_stagnation_if_ignored`): once such a hypothesis has
gone `IGNORED_STAGNATION_TURN_THRESHOLD` turns since its last progress, its
counter advances one per turn so decay and anchoring act on it (#713). This is
conservative and reversible — decay only lowers belief, and the moment evidence
touches the hypothesis its likelihood recomputes from `initial_likelihood` (the
age-decay is erased) — so an ignored candidate stalls/soft-retires rather than
lingering, and never reaches a conclusion on age alone.

---

## 7. Validation, Treatment, and the Intervention Quadrants

### 7.1 Empirical validation only

A hypothesis — intermediate *or* root — is validated only by **direct,
observable facts** matching its predicted state (an exact log/stack trace, a
return code, a reproducer's output). Assumption, inference, and secondary
correlation are not validation. Engine-side, a node reaches VALIDATED only on
**causally-grounding SUPPORTS** link(s) (`derive_node_states` →
`_node_evidence_tally`, `causal_graph.py`), net of refutations and behind the
M7 AND-gate.

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
`summary + extract`, counted as a **maximum independent set** of the
pairwise mirror graph — order-invariant AND monotone under added evidence (a
later "bridge" row paraphrasing two independent observations must never
reduce the count and retract a validated conclusion); a pure mirror pair
still collapses to one observation; rows too short to tokenize are
unjudgeable and count ZERO). The
rationale is the trust boundary: with the runbook-provenance arm
decommissioned (#658), *every* causal link is an LLM self-labeled claim, so a
single self-certified datum must not conclude a case (#573 / #656) —
corroboration is the only engine-computable difficulty knob left. Two
exceptions, both principled:

- A **counterfactually CONFIRMED** root (engine-stamped `causal_absence`
  SUPPORTS, *gone ⇒ gone*) satisfies the bar outright — M2's top grade
  dominates empirical counting, and the stamp is engine-only (ingest strips
  LLM attempts). The same principle runs the other way at RESOLVED execution:
  when NO root stands validated, the confirm-stamp may target the **sole
  count-held root** (`support_count_held_root_ids` — really causally
  supported and blocked *only* by this bar) because the user's explicit
  gone⇒gone handshake IS the decisive second observation; the count bar must
  not veto the strongest evidence class, or a confirmed 1-support case would
  terminate `NO_ROOT` with harvest permanently blocked. A root held for any
  other reason (restating, net-refuted, AND-gate, no qualifying support)
  never qualifies.
- The **deductive lane** (§7.1.1) is untouched: exclusion carries its own
  strict guards and validates a survivor with no supports of its own.

The count-held state is first-class for the engine's own behavior, not just
the stamp: the anti-anchoring retirer exempts a count-held root's hypothesis
(pre-bar it would have been VALIDATED and protected — the raised bar must not
feed the true cause to forced retirement while it waits for its second
observation), and the context builder annotates a count-held root with its
recovery action ("needs a SECOND INDEPENDENT causal observation") so the
model is steered to corroborate instead of re-recording the first datum.

The bar is recompute-honest (NOT grandfathered, unlike the restatement
guard): support counts are monotone in evidence — links only accumulate
(chain-emission ingest upserts a re-emitted link per `(node, evidence)`, so a
raised `stance_confidence` after corroboration lands; links on
`causal_absence` rows are never overwritten — engine-verdict territory), and
the only decreases (a stance flip or a lowered `stance_confidence` on
re-emission) are genuine re-assessments that *should* demote. A held root
sits at INCONCLUSIVE — a live candidate (`cause_state=CANDIDATES`), never a
refuted one. Block events increment
`root_validation_blocked_support_count_total` (fires when the generic bar
passed and real causal-category support exists; a root failing both this bar
and the restatement guard is attributed here).

The same prior-vs-evidence discipline holds on the flat axis: a direct LLM
likelihood update on a hypothesis with **no confident supporting evidence
links** (a SUPPORTS link at `stance_confidence ≥ CAUSAL_STANCE_CONFIDENCE_MIN`
— the same bar as the chain tally, one shared constant) is capped at
`NEW_HYPOTHESIS_MAX_PRIOR` (`update_hypothesis_likelihood`, #573 B1) — the
creation-time cap was otherwise a fiat lever away from the
`CAUSE_IDENTIFIED_LIKELIHOOD` gate it exists to protect. Likelihood updates
apply AFTER the same turn's evidence links (the prompt mandates
record → link → set in one turn; capping before the link lands would punish
compliance). The model is told to record and link the observation instead of
re-asserting a larger number.

*Known limits (by design):* independence is lexical — Jaccard over content
tokens. A paraphrase re-record of ONE datum with disjoint vocabulary reads as
independent (the bar is one layer, not a semantic dedup), and two terse,
scaffold-dominated summaries about the same component can falsely collapse
(held at INCONCLUSIVE — the conservative direction; the annotation steers the
model to a genuinely different observation, which will not mirror).
Calibration pairs live in one executable home:
`test_evidence_independence_calibration.py`.

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
guard is one layer of the #656 defense, beside the independent-support bar
(above), the assurance-grade caps (§9.5), and MECE arbitration (§7.1.2).

*Accepted recall limit — terse-only fragmentation (#695 B3, closed
won't-fix #699).* Sibling-frame dilution only DELAYS validation while an investigation is
live, but a case that reaches RESOLVED with the cause emitted **only** as terse
duplicate roots — none carrying enough independent grounding to clear the ROOT
bar, each restating its siblings — terminates `NO_ROOT`: the genuinely-solved
case is **under-certified, not mis-certified**. This is the deliberate recall
cost of the soundness bar, and it **fails safe** — NO INCORRECT CONCLUSION holds
(the engine certifies nothing rather than guess which fragment is the cause), the
case still resolves (the RESOLVED gate keys on the `causal_absence` confirmation,
not the grade), and nothing is harvested to the KB (harvest requires `CONFIRMED`).
The two obvious closers were evaluated and rejected as soundness-touching:
*merging* the duplicate roots (the over-merge trap, §7.1.2 — a paraphrase may be
a distinct OR-sibling) and *pooling* their support via a lexical same-cause
relation **pre-validation** (a 0.6-mutual false positive manufactures a wrong
validation; the confirm-stamp's use of that same relation is safe only because it
runs *post-validation* and is handshake-gated). A recovery would require a
*proven pooling-safe* same-cause relation — #699 holds the binding constraints
and explicitly permits a negative result. **Frequency, not the bar, falls with
inputs:** a more capable model fragments less and grounds a single root, and
better KB coverage scaffolds one coherent chain — but per the LLM-agnostic
invariant the engine never lowers the certification bar for the model, so this
limit shrinks toward zero as models/coverage improve yet never closes to zero.

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
(absence-based) refutation declared at `stance_confidence >=
CAUSAL_STANCE_CONFIDENCE_MIN` (a self-HEDGED counterfactual is ordinary refuting
evidence, not the decisive grade — the refute-side twin of the §7.1 support
filter; INV-30), so a merely-correlational net-refute — or a hedged
counterfactual — stays above `DEDUCTIVE_EXCLUSION_MAX_BELIEF` and blocks the
deduction. A mis-asserted
exhaustiveness therefore cannot fabricate a validation on its own — the differential
must have genuinely collapsed — and guard #4 (counterfactual before RESOLVED, and
harvest is RESOLVED-only) is the downstream backstop against a missed family.

### 7.1.2 MECE arbitration — simultaneous validation is a coherence violation

Roots are mutually-exclusive origins (S2): at most one can be the real cause.
So **more than one simultaneously-validated DISTINCT root** does not mean "we
proved several causes" — it means the evidence has **not discriminated** yet
(each root cleared its own §7.1 bar, but nothing separated them). This is the
forward mirror of the §7.1.1 exclusion collapse: just as proof-by-exclusion
concludes only when the differential has collapsed to one survivor, forward
validation concludes at case level only when one distinct cause stands.

While contested, the engine **withholds every case-level assertion of the
cause** — never the node states:

- `cause_state` reads **CANDIDATES** (the honest state: several candidates),
  never IDENTIFIED; the persisted `cause_identification_contested` flag and
  the `cause_identification_held_mece_total` block-event counter make the
  standing contest queryable per turn. The flag records **contest
  existence** — the same predicate every behavioral consumer acts on —
  independent of the symptom anchor (a contest whose symptom is still
  unverified already withholds the mirror and renders the discrimination
  ask, so the observability surfaces must see it too).
- The **engine conclusion mirror is withheld**: `retract_stale_engine_rcc`
  clears a standing one naming a contested root, and
  `synthesize_rcc_from_validated_root` refuses to mint one (defense in
  depth — asserting ONE of several equally-validated exclusive causes is an
  arbitrary pick, not a reflection). The engine-generated **working
  conclusion stops counting as a known cause** in `_cause_identified` (it is
  the max-likelihood pick over the very hypotheses under contest — the same
  arbitrary assertion through a side door; that fallback exists to rescue
  under-reporting, and a contest is a deliberate hold, not under-reporting).
  An **LLM-authored** conclusion is **not retracted** (the LLM's stance may yet
  be borne out — erasing it would be a NO-COLLAPSE breach) but is **read-
  suppressed**: while contested it likewise stops counting as an identified
  cause in `_cause_identified`, and re-counts the moment the contest resolves
  (§7.6 — the LLM-conclusion lifecycle).
- Each contested root keeps its evidence-derived VALIDATED standing (the §7.1
  entry-bar lesson: re-adjudicating settled nodes is what causes flap), and
  the context builder renders the discrimination ask inline on the contested
  roots.

**What is NOT a contest** (`distinct_cause_clusters` collapses these before
counting):

- **Duplicate emission** — near-identical root statements (mutual mirror at
  `_ROOT_DISTINCT_JACCARD`) are ONE cause recorded twice. Holding on them
  would deadlock: no evidence can ever discriminate a statement from its own
  restatement (NO-COLLAPSE). An unjudgeably-short statement merges with
  nothing — the safe failure is holding, never concluding on an arbitrary
  pick.
- **A deepened chain** — two ROOT-typed nodes on one LIVE causal path
  (either direction) are one line of explanation at two depths, not a
  differential. A path through a **REFUTED** rung does NOT connect
  (`_live_descendant_ids`): the link is disproven, so roots joined only by a
  broken chain are genuine competitors. (This is deliberately stricter than
  the INV-30 bearing-frame walk, which renders a chain's *recorded*
  mechanism refuted rungs and all.)
- **A CONJUNCTION** — two roots co-necessary for the same effect (one M7
  AND-set, sharing an `(effect, and_group)`) are ONE cause carrying two
  conditions, not a differential. S2's "at most one root can be the cause"
  ranges over OR-alternatives; an AND-set is the explicit counterexample, so
  both conjuncts standing VALIDATED is the correct end state rather than a
  failure to discriminate. The exclusion lane already draws this line
  (`_survivor_or_sets` builds its differential from `and_group is None` edges
  only). Only the conjuncts merge: an AND-set beside an independent
  alternative is still a real contest. Without this the engine punished the
  very shape §5 asks for on a two-condition cause — holding identification
  (so no M5 solution license), asserting no conclusion, and refusing the
  resolution confirm-stamp (#1096).

  **This is a trust grant, and it is instrumented as one.** The AND-set is
  model-authored, so a grouping token emitted over two already-validated
  rivals dissolves a standing hold: identification is granted, the conjunction
  is published, and the confirm-stamp unblocks. Honoring it is deliberate —
  the model authors causal structure everywhere else, and requiring M7 proof
  before the grouping counts would restore the deadlock — but the merge is
  MONOTONE, so the grant is permanent and never re-examined. The engine
  therefore does not adjudicate the grouping; it makes the *sequence*
  observable. `causal_and_set_late_grouping_total` (plus a
  `causal_and_set_late_grouping` log naming the group, the effect and the
  joined members) fires only when a grouping arrives AFTER its members
  validated — a conjunction modeled up front never increments it, so the
  counter reads as the audit population rather than as conjunction volume. A
  genuine late recognition is indistinguishable from a hallucinated one at
  that point, which is precisely why the engine records rather than guesses.
  The symmetric refusal — a re-emission trying to move or clear a standing
  group — is dropped and counted (`causal_and_group_regroup_refused_total`),
  because the model then reasons over a grouping the graph does not have and
  the ingest path has no `system_feedback` channel to say so.
- **A counterfactually confirmed root** (§7.2 top grade, engine-only
  producer) settles the contest outright: the gone⇒gone confirmation IS the
  discrimination, so validated siblings never hold a proven cause hostage.
  On a reopened case this deliberately still settles even when the old
  confirmation has gone stale — recurrence is discharged by the failed-fix
  machinery (M6 demotes the confirmed root on disconfirmation), not by
  re-litigating the confirmation here.

The hold is escapable exactly the way the methodology says a differential
resolves: **discriminating evidence** (a decisive refute on the alternatives —
M6 or counterfactual), a **counterfactual confirmation** of one root, or the
duplicates merging. Resolution itself is never blocked — the RESOLVED gate
keys on the confirmation row, and the confirm-stamp applies the same
clustering: a duplicate/deepened-line node shape never vetoes the user's
handshake — the cited node is the cluster's ORIGIN (`sole_cluster_origin`:
most live in-cluster descendants, by the same reachability the cluster count
used, in one relations pass), idempotence and the per-root
refutation window are taken CLUSTER-wide (a confirmation or a failed-fix
refute anywhere in the cluster belongs to the CAUSE) — while a genuine
multi-cause contest stays refused and the case terminates MECHANISTIC,
honestly, with the candidates on record instead of an arbitrary conclusion.

*Known limits (by design):* root-statement identity is LEXICAL, like every
mirror bar in this family — negation is stopworded, so opposite-polarity
statements ("disk full" / "disk NOT full") read as one cause and would not
contest each other; synonym paraphrases of one cause read as DISTINCT and
hold identification until one is refuted or the user confirms. Both are
token-layer limits shared with the §7.1 guards, pinned in the tests.

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

**A `causal_absence` row carries no model-authored stance at all (INV-42,
#987).** The row is a stand-alone audit record, so every LLM-emitted link on
one is refused — on BOTH belief axes, whatever the stance. The invariant is a
property of the evidence *category*, not of the link target: enforced on the
chain axis alone, it is a rule a single stance choice routes around via the
flat hypothesis axis. Counterfactual links on absence rows are therefore
**engine-minted only** (the §9.5 confirm-stamp's SUPPORTS, M6's REFUTES).

This replaced an earlier boundary that stripped only SUPPORTS, keeping REFUTES
"to feed M6" — self-refuting under this document's own contract, since an
absence row records a **CONFIRMED** fix and a failed fix emits no absence row
at all (§7.3: the failed outcome is recorded by refuting the hypothesis). An
absence-REFUTES was thus never a sanctioned emission, and reading one as a
failed-fix disconfirmation *inverts the row's own meaning* — the #987 incident,
where a model's success confirmation ("post-fix authentication succeeded"),
REFUTES-linked to the root it confirmed, refuted that root at belief 0, fired
M6, retracted a correct conclusion, and terminated a RESOLVED case asserting no
cause was ever known. Refusals are metered (`absence_row_link_refused_total`) —
a prompt-adherence signal, not a truth problem; refusing silently would make a
model that mis-links absence rows indistinguishable from one that does not.

The **decisive-force** bar below therefore now scopes the ENGINE's own links
plus persisted history: a counterfactual REFUTES carries decisive force only at
`stance_confidence >= CAUSAL_STANCE_CONFIDENCE_MIN` — the refute-side twin of
the §7.1 support filter (INV-30). A self-hedged "I think the fix didn't work"
must not single-handedly refute a node, zero a sibling's belief for
proof-by-exclusion (§7.1.1 guard #3), or demote the identified cause (M6); it
still counts as *ordinary* refuting evidence (feeding `refutes > supports` and
`_net_refuted`). The engine's own M6 links carry no declared confidence and are
decisive by construction.

**M6 establishes its preconditions; it does not infer them (INV-42, #987).**
The demotion trigger carries two materially different claims and the engine
records only what it can substantiate:

- **Evidence-based** — the hypothesis is REFUTED or net-refuted by its OWN
  links. This asserts nothing about any fix, is grounded in the graph, and
  demotes **unconditionally**. Its durable record names the tally it derived
  from. (Gating *this* arm on fix evidence was the over-broad first cut of the
  #987 fix, and it left a net-refuted cause standing as identified with its
  conclusion intact — the NO-INCORRECT-CONCLUSION breach in the opposite
  direction.)
- **Counterfactual** — "a fix was applied and the problem persisted". This is a
  claim about events *outside* the graph, so it fires only on preconditions the
  case record establishes: an executed **SOLUTION** (a MITIGATION is by
  definition not a fix of the cause, so a failed workaround must never
  establish that the cause was addressed), dated by its **execution** turn and
  not the turn it was offered; a **positive** persistence observation at/after
  it (a `symptom_evidence` row — "nothing said it was fixed" is not an
  observation that it stayed broken); and **no** qualifying gone⇒gone
  confirmation at/after that turn, which would be direct evidence the problem
  did not persist. Refusals are labeled (`m6_demotion_refused_total{reason}`);
  a nonzero `resolution_confirmed` rate is a *defect* signal, not elicitation
  drift.

Refusing the counterfactual arm never leaves a disproven cause standing — the
hypothesis's own state still governs, so a refuted cause stops grounding
`cause_state` regardless. What is withheld is only the durable engine
refutation.

Whatever the arm, **the engine never mints an observation row for something it
inferred.** M6's durable record previously asserted "the cause was addressed or
confirmed correct, yet the problem persisted" — a first-person observation
nothing had checked, and false in the #987 incident. It now states an engine
*inference with provenance*. The general rule, which explains both halves of
this design:

> **Constructive transitions may be derived from confirmation plus evidence
> with recorded provenance; destructive transitions require established
> preconditions.**

The constructive half is §9.5's confirm-stamp: it promotes a cause the user
explicitly confirmed and records *how* (`RootCauseConclusion.established_by`
plus the node link's reasoning), which the resolution report renders beside the
assurance grade.

Those two records have **different audiences, and therefore different text**
(#1097). The node link is the durable audit trail — it names the evidence row
and the node so the promotion can be reconstructed, and those ids are the point
of it. `established_by` is rendered *verbatim to a user*, so it carries the
prose form: the two legs of the promotion, with no ids and no grade shorthand.
Writing one string onto both surfaces is how `ev_…`, `cn_…` and "M2 gone⇒gone"
reached a resolution summary as debug output. The same rule governs
`mechanism`, which the report prints and any harvested runbook inherits: it is
the chain's rungs, never the engine's synthetic PROBLEM terminal. Because
terminal cases never recompute, both fields are also normalized at the READ
(`established_by_for_display` / `mechanism_for_display`, beside the model) so
cases resolved before the split do not render the internal form forever. Resolution is **reconciled, never refused** — a
refuse-on-divergence gate would convert an engine defect into a deadlock no
user action can clear (NO COLLAPSE). And "no root cause established" is a
nameable state: before it had a rendering, the resolution recap reached for the
early-stage working-conclusion placeholder and told the user the investigation
had not begun.

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
  (`mitigation_sufficient`) on *symptom* absence, not *causal* absence.

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
| Defensive fix (perm @ intermediate) | accepted permanent workaround (often `rca_infeasible`) | **CLOSED** (`closed_rca_infeasible` / `mitigation_sufficient`) | symptom_absence |
| Mitigation (temp @ intermediate) | mitigation insert | buy time → forward → RESOLVED / CLOSED (revert reminder) | symptom_absence (interim) |
| Loop-break (R9) | mitigate the cycle | **CLOSED** / stabilized | symptom_absence |

### 7.6 The LLM-authored conclusion's lifecycle — retraction ≠ authorship (INV-34)

A `RootCauseConclusion` the LLM writes (`determined_by` = the agent) is the
LLM's own stance, a **trust boundary**: the engine never re-words it — the M2
grade labels its confidence at read-time rather than rewriting it (§9.5), and
what the engine may do instead is *replace* it wholesale with the chain-derived
mirror when a validated root stands (the §7.7 precedence), or leave it exactly as
written when none does. This section governs that fallback lane: everything below
binds the LLM conclusion whenever it is the conclusion the case carries. But
*never re-wording* it does not mean *never retracting* it. The two guarantees still
bind the conclusion the engine surfaces to every terminal consumer (the
disposition/M5 gate, the report, the copilot UI, KB runbook harvesting); the
user-visible narration is a further guarantee surface for *disposition claims*,
covered separately in §7.9:

- **Disconfirmation → retract at source (NO-INCORRECT-CONCLUSION).** When the
  engine has decisive structural evidence the named cause is *wrong* — a
  counterfactual refute or net-refutation (M6, §7.3) — the conclusion must be
  `None` for every reader, exactly as an engine mirror is. Retraction is not
  authorship: it asserts nothing, it clears a proven-wrong assertion.
- **Contest → read-suppress, don't erase (NO-COLLAPSE).** A MECE contest
  (§7.1.2) is *not* disconfirmation — the LLM's cause may yet be borne out — so
  the conclusion is **preserved** but stops counting as identified in
  `_cause_identified` while contested, and counts again the moment the contest
  resolves. Erasing a reversible not-yet-arbitrated stance would be a collapse.

The mechanism that makes the retraction lane reach an LLM conclusion is a
**cause link**. An LLM conclusion arrives with no `validated_hypothesis_id`, so
the link-based retraction (`retract_disconfirmed_rcc`) and the M6
representative-cause pick cannot attribute it until it is linked. The link is
established two ways, authoritative first: **(1)** when the LLM names the cause's
root node on its conclusion (`names_root_node_id`, §7.7), the engine attributes
it exactly to the hypothesis whose `root_node_id` matches — no guessing;
**(2)** as a fallback for a conclusion that arrives without a named node (older
turns, a non-compliant model), each recompute `link_llm_rcc_to_cause` attributes
it to the standing hypothesis it names — but only on a **single unambiguous
STRONG lexical match** (the orphan-chain T1 discipline: exactly one hypothesis
at/above `RESTATEMENT_STRONG`, substantive shared-token overlap). A wrong link
would retract a valid conclusion on an unrelated refutation — itself a collapse —
so *when unsure, don't link*, and an unattributable free-text conclusion stays
the documented residual it has always been (no regression). Giving the conclusion
a cause link is not authorship: the engine writes only *which hypothesis the
LLM's cause corresponds to*, never the cause text.

Two consequences fall out of the link, not extra machinery:

- **Same-turn re-grounding survives (refresh).** When a fix fails and the LLM
  re-grounds its conclusion onto a *different* still-standing cause, the link
  points the M6 trigger at that new cause, so the demotion of the old cause
  never fires on the conclusion — the max-`initial_likelihood` proxy that used
  to wipe it is gone.
- **The retraction is single-truth.** Because the conclusion is retracted at
  its source, every downstream reader sees one truth; no per-reader
  disconfirmation guard is needed.

*Known limit (by design):* the link and the contest are LEXICAL, like every
mirror bar in this family — a synonym-paraphrased conclusion may not link (and
stays the residual); the conservative bar is calibrated in
`test_llm_rcc_lifecycle.py`.

### 7.7 Cause identification is engine-derived, not LLM-declared (INV-35)

Identification (`cause_state=IDENTIFIED`, §9.2) is an engine derivation from a
validated, uncontested chain root — it is **never** an LLM self-claim. This is a
guardrail, not a seizure of the LLM's judgement: the LLM decides *what* the cause
is (it authors the hypotheses, builds and grounds the chain, states its
confidence in `root_cause_likelihood`, and names the cause in its conclusion);
the engine only confirms whether that decision is grounded enough to drive the
irreversible actions gated on identification (solution work, terminal transition,
KB harvest). Deciding the cause is the LLM's; certifying the grounding bar is the
engine's. This is the same trust boundary §7.6 draws for the conclusion, applied
to the identification signal.

Concretely, the LLM does **not** operate the identification gate:

- **No self-certification signal.** There is no LLM-settable "root cause
  identified" boolean. The engine recognizes identification when the hypothesis's
  chain ROOT validates on **two independent causal observations** (§7.1 / INV-29),
  the symptom is verified, and no rival cause is equally validated (§7.1.2) —
  never from a flat assertion or a bare confidence number. `root_cause_likelihood`
  is the LLM's stated confidence; it informs focus and reporting, and does not by
  itself advance identification.
- **The conclusion names its cause.** When the LLM authors its
  `RootCauseConclusion`, it sets `names_root_node_id` to the `cn_` root node it
  already emits during chain construction (§5/S3). The engine attributes the
  conclusion to the STANDING hypothesis rooted there — the authoritative tier of
  §7.6's link, replacing the lexical scan on the compliant path. It stays
  *guarded* by the same trust discipline as the lexical tier (a standing
  hypothesis, never a refuted or retired one, with substantive text overlap), so a
  stale or mis-copied id cannot attribute the conclusion to the wrong cause and let
  the retraction lane wipe a valid one. This closes the DF-2 residual that the
  conclusion was authored in a namespace disconnected from the causal graph, so the
  engine had to reverse-engineer which hypothesis the prose named.

This section completes the campaign's relocation of guardrail *operation* off LLM
self-claims (DF-1/DF-2): the same move made for causal-link category (INV-23),
support count (INV-29), absence trust (INV-30), and conclusion retraction
(INV-34) is here made for the identification signal itself, at the prompt/schema
layer. The prompt teaches the engine's actual model — build and ground the chain,
the engine confirms — rather than a self-certified boolean the engine had already
stopped reading.

*The chain outranks the conclusion (precedence).* The `root_cause_conclusion` the
engine surfaces **is** the chain-derived mirror whenever a standing validated,
uncontested chain root exists: the per-turn recompute mints or refreshes the
mirror even over an LLM-authored conclusion, so every terminal consumer reads text
rendered from the root the chain actually proves. The LLM-authored conclusion is
the explicit **fallback** — it stands byte-identical, and only, when no such root
stands (the terminal-soundness backstop, now named rather than implied). Every
older refusal sits *ahead* of the precedence: while identification is
MECE-contested (§7.1.2) the engine asserts nothing at all, and a disconfirmed
conclusion is retracted at source (§7.6) whoever wrote it. A mirror that replaced
a conclusion and whose root later demotes is retracted like any other mirror
(`retract_stale_engine_rcc`) and the case then carries **no** conclusion. The
replaced text is not restored, and the engine keeps no copy of it: retaining one
would be a second conclusion namespace — the very thing single authority
retires — and re-surfacing it would assert a cause no validated root backs,
whether or not it named the cause the demoted root did.
`rcc_precedence_inversion_total{provider}` counts each replacement — the second
read INV-41 asks for, showing how often the fallback is the only thing standing.
The precedence is a kill switch (`FAULTMAVEN_CHAIN_AUTHORED_CONCLUSION`, default
on); off restores conclusion-wins precedence exactly.

*Accepted costs of chain-rendered text.* The mirror renders mechanism by joining
the chain's rung statements (" → "), so it reads flatter than the LLM's prose —
the fix is better rung-statement elicitation, not a second conclusion namespace.
An LLM-authored `contributing_factors` is likewise dropped on replacement rather
than blended in: single authority means the engine does not carry LLM prose into
text it renders from the graph.

*The conclusion carries the whole conjunction.* `contributing_factors` is not
empty, though — the mirror populates it from the graph, with the statements of
the VALIDATED nodes that share an M7 AND-set with the chain it renders and are
not themselves on it (`causal_graph.validated_and_conjuncts`). A conclusion
mirrors ONE chain — root as the cause, rungs as the mechanism — so a
co-necessary cause sits off that chain, and without this a cause the
investigation established as a conjunction reached the report, and any runbook
harvested from it, as its first conjunct alone (#1096). Only VALIDATED conjuncts
are named: an AND-member still a candidate is not established, and the
conclusion is the one place that must never assert more than the graph proves.
The conjunct set joins the root and the M2 grade in the mirror's faithfulness
check, because a conjunct typically validates AFTER the root — omitted from that
check, the mirror would freeze at the single-factor text it was first minted
with. Because that makes the mirror re-mint for a reason unrelated to the cause,
root selection now prefers the prior mirror's own root within the confirmed set:
a refresh must not silently swap the published cause. The list is SORTED, not
edge-ordered — neither repository loads `causal_edges` with an `ORDER BY`, so
row order would make both the text and that equality check vary with fetch order
on PostgreSQL. The same conjuncts travel to the runbook boundary
(`CaseConversionRequest.root_cause_conditions`), because a runbook recording
half a cause outlives the case it came from. One chain-builder
(`conjuncts_for_chain`) serves both mint sites, the per-turn mirror and the
terminal confirm-stamp — the stamp can only cross that module boundary by hook,
and a second copy of the rule would let the two conclusions one case passes
through name different conjuncts.

The report heading attributes the requirement to **producing the problem**, not
to the root cause: an AND-set can sit on any rung of the mirrored chain, and a
conjunct co-necessary with an intermediate is a condition of that mechanism step
rather than of the cause. The LLM gets no schema field for any of this by
design: the way to say "the problem needed both" is the AND-set, and the engine
reads it — a blank `and_group` names no group and normalizes to `None` at the
READ site (`incoming_and_groups`), not only at ingest, because rows persisted
before that guard cannot be reached by it and are exactly the rows at risk.
Grade labeling needs nothing new and already covers the fallback — the assurance
grade and the over-claim flag are recomputed from the graph at every surface (turn
response, resolved payload, report note), so a fallback conclusion standing at
`NO_ROOT` carries its "not validated in the causal analysis" label, and a fallback
claiming VERIFIED there still trips the INV-25 over-claim seam. The resolution
confirm-stamp keeps its own narrow mint rule (it fills an *absent* conclusion for a
count-held root and never overwrites one): re-adjudicating precedence on a case the
user has already confirmed resolved would change the conclusion text under the
confirmation.

*Still gated — retiring the authorship itself.* The LLM continues to author its
conclusion (schema and prompt untouched); what remains rejected-for-now is
removing that authorship and, with it, the reconciliation layer (§7.6's
link/retract and the INV-25 over-claim seam). That step is **gated** on reliable
chain-grounding: retiring the fallback before models ground reliably would strand
cases that today resolve through it (a NO-COLLAPSE regression). Tracked as #673 so
the retirement is deliberate, not forgotten.

*The gate is a metric, not prose (INV-41).* "Reliable chain-grounding" is now
measurable: at each RESOLVED transition the engine records which leg of
`_cause_identified` licensed the resolution — the chain (`cause_state=IDENTIFIED`),
or the RCC / working-conclusion **backstop** (with no chain-validated root) —
via `resolution_cause_leg_total{provider, leg}`. It is captured **pre-stamp** at
the shared resolution finalizer (`finalize_resolution_truth_surface`, which every
resolve surface calls), *before* the confirm-stamp retroactively validates the
root — a post-stamp read would relabel a backstop-licensed resolution as chain
and hide the very reliance this gate measures. The **backstop-reliance rate**
(`(rcc + working_conclusion) / all`, per provider) is the retirement gate: while
it is materially non-zero at the **INV-39 provider floor**, retiring the free-text
backstop would strand exactly those resolutions. The gate must clear at the
weakest supported provider, not merely on the best model — otherwise retirement
is a NO-COLLAPSE regression precisely for the provider floor. Metric-only: it
never changes engine behavior; it is the observability half of the #673 decision,
the way INV-39 is for the §5.2 provider floor.

### 7.8 One cause, one hypothesis: dedup on `hypotheses_to_add` (INV-36)

The work gate (§5.2) — ≥2 hypotheses across ≥2 categories with ≥2 evidence items
— is the axis that separates a productive investigation (`INSUFFICIENT_EVIDENCE`,
real diagnostic work happened) from a stalled one (`NOT_YET_PRODUCTIVE`, the model
is spinning). It counts `len(case.hypotheses)`. A model that emits the **same
cause twice** therefore buys itself a spurious gate crossing — two records, or two
records under two categories, for one idea (observed live: an identical DNS
hypothesis minted on turns 10 and 11 of the #656 incident).

At the `hypotheses_to_add` apply layer the engine refuses to mint a hypothesis
whose statement **duplicates** one already standing or an earlier sibling in the
same emission batch. The bar (`hypothesis_statements_duplicate`) is deliberately
**stricter than §7.1.2's fold and fails open**, because deduping *drops* an LLM
emission for the turn rather than holding it reversibly:

- **A near-verbatim bar, not the fold's 0.6.** Duplicate = mutual mirror at
  `_HYPOTHESIS_DUPLICATE_JACCARD` (0.8), well above the reversible MECE
  `_ROOT_DISTINCT_JACCARD` (0.6). A genuinely-distinct short statement that
  differs by one substantive token ("memory leak in *connection* pool" vs "…
  *cache* pool", Jaccard 0.6) **survives**; the incident's actual duplicate was
  verbatim-identical (~1.0). It is the *symmetric* mirror, not `restatement_score`
  containment, so a more-**specific elaboration** of a standing hypothesis (a real
  refinement) also survives.
- **A polarity guard.** "not" is a content stopword, so a hypothesis and its
  negation tokenize identically and would mirror at 1.0. An asymmetric negation
  cue refuses the dedup — a **disputing** hypothesis is never a duplicate of the
  claim it contradicts (that would erase the very competing-cause signal §7.1.2 /
  INV-33 exist to preserve).
- **A numeric-discriminator guard.** The similarity tokenizer drops single-digit
  tokens and stopwords like "version"/"node", so two hypotheses distinguished
  ONLY by a number ("server 1 down" vs "server 2 down", "version 5" vs
  "version 6") tokenize identically. Differing digit runs refuse the dedup.
- **Standing causes only.** `REFUTED`/`RETIRED` hypotheses are *not* dedup
  targets: those states are terminal-immutable and the update path instructs the
  LLM to "open a NEW hypothesis" to revive a theory — deduping against them would
  deadlock the revival (re-mint refused here, update refused there). The
  gate-inflation vector is duplicate *active* records; a revival minting a fresh
  hypothesis is legitimate work.

Together these keep the drop conservative: it collapses only genuine
restatements, never diagnostic work (NO-COLLAPSE). On a skip the engine surfaces
the matched id via `system_feedback` ("this duplicates `hyp_…`; update it rather
than restate it"), so a genuine re-examination flows to `hypotheses_to_update`
against the standing record. The skip is **not** counted as generation:
`hypotheses_generated` (turn-record + turn-outcome progress) stays truly-new, so a
dedup cannot masquerade as progress and mask the exhaustion detector. Positional
integrity is preserved by a separate `hyp_emit_order` list that records the
**canonical** existing id at the skipped item's slot, so a same-turn `new_index_N`
reference (evidence link, hypothesis update, need motivator) resolves to the kept
hypothesis rather than shifting onto the wrong sibling. A chain the LLM emitted
for the duplicate is left to the existing orphan-chain post-pass
(`resolve_orphan_chains`), which re-attaches it to a **flat** standing hypothesis
under its own anti-clobber guard (`_hypothesis_lacks_real_chain`); the dedup does
**not** re-root the canonical itself — doing so would bypass that guard and could
GC a validated hypothesis's existing chain. Telemetry:
`faultmaven_hypothesis_dedup_skipped_total`.

*Rejected alternative — reuse the §7.1.2 fold bar (0.6) / dedup by containment.*
The fold is reversible (both statements stay represented in the graph); this drops
an emission, so it warrants the stricter, fail-open 0.8 mutual bar. Containment
would additionally fold a specific elaboration into its more-general parent and
lose the refinement. *Complementary prevention (routed):* the apply-layer dedup is
enforcement; the deeper fix for repeated re-emission is showing the LLM its
standing hypotheses so it never emits a duplicate — a context/prompt change
tracked separately, not in scope here.

### 7.8.1 One cause, one CHAIN: a root belongs to exactly one hypothesis (fm#1091)

§7.8 stops two records for one cause. The dual failure is one record adopting
another's cause: an emitted `root_node_ref` may name **any** existing `cn_` root,
and the engine used to accept it as long as it resolved to a ROOT node. So a
hypothesis could anchor itself on the chain root of a *different* hypothesis, and
the two axes then described one node with two statements.

Nothing downstream can survive that. A root node **is** its hypothesis's cause
statement, and the engine reads the node, not the hypothesis, everywhere it
matters: B1 mirrors the adopter's grounding onto the owner's root, `derive_node_states`
validates that root on the mirrored support, the §9.2 projection reads the
validation back onto the adopter, and the report's causal map draws the *owner's*
statement as the validated cause of `D`. Observed live (fm#1091): a cache-exhaustion
hypothesis adopted the root of a REFUTED runner-out-of-memory hypothesis, and the
resolution summary drew "a step's working set exceeds the runner's available RAM"
with solid arrows into the problem — while its own Hypotheses section listed that
statement as Refuted at 0%, and the real cause appeared in the map nowhere.

**Rule (attach-time, both emission paths).** A `root_node_ref` resolving to a root
that is already some *other* hypothesis's `root_node_id` is REFUSED. The hypothesis
keeps whatever anchoring it had (usually none), its own chain is not GC'd, and the
LLM is told the owner's id and statement and instructed to emit its own root — or
to update the owner instead, if the two really are one cause (which routes back to
§7.8). Counter: `faultmaven_hypothesis_root_adoption_refused_total`.

**Contested refs are settled in a second pass.** A turn's refs are applied in
emission order (adds before re-roots), so a root that is owned when it is first
read may be FREED later in the same batch — the hand-off shape, where the owner
deepens onto a new root and the old one becomes the new hypothesis's cause.
Judging on first read would refuse a hand-off the model expressed correctly and
then GC the very chain it handed over, so contested refs are re-checked against
final ownership and the abandoned-chain GC runs once, after every move has
settled (`prune_abandoned_nodes` then drops only what no hypothesis still
references).

The bar is **ownership, not similarity.** A "does this hypothesis's statement match
this node's?" test would have to fire on the normal path too, where a hypothesis and
its root are legitimately worded differently ("expired cert breaks TLS" rooted at
"the API's TLS cert expired at 02:00"), and refusing there would strand chains the
model built correctly. Adoption of an *owned* root is anomalous by construction —
the normal emission points at a `new_index_N` the same turn — so the collision test
is exact, cheap, and carries no false-refusal risk of that kind.

Refusing leaves the hypothesis flat, which holds identification (`cause_state`
cannot reach IDENTIFIED off a chain that does not exist) until the model emits a
real root. That is the intended direction under NO-INCORRECT-CONCLUSION: an
unanchored hypothesis delays a conclusion, a mis-anchored one publishes the wrong
one. The orphan-chain post-pass (T1/T2a) is unaffected — it only ever re-attaches
roots that no hypothesis references.

**Backstop at the report boundary.** The map additionally refuses to draw a node
as ✓ validated when a REFUTED hypothesis is rooted there — whatever the store
contains, the same document must not assert as the established cause a statement it
lists as refuted. The whole map is withheld (the section is simply absent) rather
than redrawn, because the engine cannot tell at render time which axis is right.
Counter: `faultmaven_causal_map_suppressed_contradiction_total`, expected to stay
at zero now that the attach-time rule holds the relation.

### 7.9 Narration-truth coherence — the transcript is a guarantee surface for disposition claims (INV-40)

§7.6 binds the two guarantees to the conclusion the engine *surfaces to every
terminal consumer* — the disposition/M5 gate, the report, the copilot UI, KB
harvest. Each of those is engine-derived or engine-gated. But the user does not
read `cause_state` or the disposition row; the user reads the agent's chat
message (`agent_response`), and that message is LLM free text on every ordinary
turn. Nothing in the derive/veto lane inspects it — the §7.6 reconciliation
machinery reads the structured RootCauseConclusion and the causal graph, never
prose — so an LLM that narrates *"Case resolved."* while the case stands at
INVESTIGATING delivers a false disposition claim the user acts on, even though
every engine surface correctly refused the transition. Both guarantees as §7.6
scopes them hold in that incident; the user is still told the opposite of the
truth. That gap is one of *guarantee scope*, not of the reconciliation layer,
which structurally cannot reach the prose.

The narration channel is therefore a guarantee surface for the one claim class
where a false statement is a wrong *conclusion* in the sense the product
promises: **disposition claims** (the case is resolved / closed). On a
non-terminal turn with no engine-confirmed terminal transition executing, the
user-visible message never asserts, unqualified, that the case is resolved or
closed. The engine does not rewrite the LLM's prose — authorship stays the
LLM's (§7.6), and destroying the analysis it just wrote repeats the DF-4
override failure. Instead the engine **appends** a truthful corrective notice
below the prose, through the same INV-26 composition lane
(`_prose_with_gate_notice`): the case is not resolved or closed — it is still
open — and what resolution actually requires. (The wording is phase-neutral:
the guard also fires on an INQUIRY over-claim, where "under investigation" would
itself be false, so the notice asserts only "still open".) The false claim can
still be *authored*; it can no longer stand *uncontradicted* on the surface the
user reads.

This generalizes INV-26(b) — "the visible transcript may not contradict the
applied `state_updates`" — from gate-override turns to disposition claims on
*any* turn. It reuses the existing narrow `_COMPLETION_PHRASES` scan
(§1.3.1 / INV-15) unchanged: the same detector, a new consumer
(state-reconciled, not log-only), so the PR #299 decision to keep that scan
narrow is untouched. It is a soundness guard in the engine derive/veto lane —
mechanical (regex + engine state, no model-graded judge), append-only, mutating
no state and opening no blocking gate — not a behavioral-rule prose validator
(`agent-behavioral-rules.md` rejects those *as behavioral enforcement*; a
disposition-truth coherence check is neither style nor methodology compliance,
and matches that doc's own allowance for a last-resort safety net on genuinely
capable-model over-claims).

**Graceful denial.** A false positive — the scan fires on conditional or quoted
prose (*"once you confirm, the case is resolved"*) — degrades to appending a
notice that is *still true* (the case is currently open): mildly redundant,
never wrong, never blocking, never state-mutating. NO-COLLAPSE is unthreatened
(append-only, pre-LLM paths untouched); NO INCORRECT CONCLUSION is strengthened
(the false claim cannot stand uncontradicted). Precision tuning is therefore not
load-bearing.

*Rejected alternative — rewrite or suppress the over-claiming prose (repeats the
DF-4 destroy-work-product failure), or an LLM-graded prose-consistency judge
(reopens the removed post-generation-validator pattern and violates the
LLM-agnostic testing invariant).*

*Elicitation companion (frequency, not blast radius).* The guard bounds the
blast radius of an over-claim; the TREATMENT/verify-turn prompt reduces how
often one occurs — a user-confirmed fix must elicit the `causal_absence` row +
`proposed_transition` reliably, even on long context (the #668 incident skipped
both 3/3 on a long-context haiku turn). Prompt guidance is the frequency lever;
the engine append is the guarantee.

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
  *Realized today at the OFFER level (INV-32):* the solution emission carries no
  `node_id`, so per-chain retirement is not yet computable — instead the engine
  re-checks the M5 established-cause license on every recompute and withdraws
  PENDING SOLUTION offers when NO established cause stands (demotion, retraction,
  MECE hold). `Solution` rows stay as historical records; per-node linkage remains
  the rejected-for-now finer grain (waits until the emission carries `node_id`).

### 9.2 Assessment variables — `cause_state`

`cause_state` derivation aligns to chain / node states:

- **IDENTIFIED** — some chain's **root node is validated**, mechanistically (§7.1)
  or deductively (§7.1.1), **and the validated root is uncontested** (§7.1.2 —
  several simultaneously-validated distinct roots hold at CANDIDATES pending
  discrimination). This is mechanistic grade; it unlocks solution work.
- **CANDIDATES** — ≥2 ACTIVE chains (preserves the existing ≥2-active derivation,
  [§2.5 decision 4](./investigation-lifecycle-logic.md#25-design-decisions-and-open-follow-ons),
  now counting chains), a live INCONCLUSIVE root, a validated root awaiting
  symptom verification, or the §7.1.2 MECE-contested hold.
- **UNKNOWN** — otherwise.
- **AND gate:** a chain's root cannot be validated until *every* AND-member on its
  path is validated (S1 symmetric proof), so `IDENTIFIED` is never reached on a
  half-proven conjunctive chain.

The LLM never self-declares this state: it builds and grounds the chain and the
engine derives `cause_state` — there is no LLM-settable "root cause identified"
signal (§7.7 / INV-35). `cause_state` is a SOFT signal, so under-reporting (a
correct conclusion whose rung evidence the LLM did not attach) costs only
prompt-focus accuracy — `terminal_transitions._cause_identified` reads
`cause_state` OR the `RootCauseConclusion` OR the working conclusion, so terminal
soundness never rests on the chain alone while models under-build it.

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
now" row emitted mid-rollout (observed live) must not upgrade the grade.
Several candidate NODES are first collapsed to DISTINCT causes (§7.1.2
`distinct_cause_clusters` — a duplicate emission or a deepened chain is one
cause, cited at its ancestor-most origin, and never vetoes the user's
handshake); with several **distinct** causes remaining the engine never
guesses which one the fix removed — the case stays `MECHANISTIC` pending
arbitration; a REFUTES-linked absence row (a failed fix) never flips to
confirmation.

Which rows may stand as a confirmation at all is ONE shared definition
(`resolution_confirmation_rows`, INV-30): non-engine-authored (the engine only
mints absence rows as M6 failed-fix **dis**confirmations), not itself a
failed-fix disconfirmation (`_disconfirmation_row_ids` — REFUTES-linked, on
either belief axis, to the cause the **engine marked** disconfirmed; a
REFUTES link to a *sibling* is proof-by-exclusion, so the natural dual-use
emission "the fix worked, so it wasn't X" stays confirmable — a blanket
REFUTES-linked exclusion regressed READY to NEEDS_INFO right after the user
confirmed), and at-or-after the latest **engine-known** failed-fix
disconfirmation (`latest_disconfirmation_turn` = the newest engine-authored
M6 row — a premature "stable" row from a fix window the engine saw fail
confirms nothing). The window is authorship-keyed and `>=` on purpose: node
pruning can orphan the engine row's REFUTES link (the window must survive
that); an LLM exclusion note on a sibling recorded *after* a legitimate
confirmation must not retroactively mask it; and the mixed single-turn shape
("the restart didn't fix it, but correcting the config did") stamps the
failed fix and the genuine confirmation at the SAME turn — masking that
confirmation would strand the resolve behind an ask the user just answered.
The engine marker is reliable because M6 mints its row even when the model
already recorded the failure with its own decisive refute
(`_attach_engine_refutation` idempotence is scoped to the ENGINE's own row —
suppressing the mint left the window unset and re-qualified stale premature
rows). A failed fix the engine never saw (the cause was never grounded, so
M6 never fired) sets no window and marks no rows — accepted self-claim
trust; the RESOLVED handshake and the stamp's per-root refutation window +
bearing check still guard the truth surface. The resolution-readiness gate
and the closure→resolve pivot read the same predicate — before this, the
gate read READY off ANY absence row, including the engine's own
disconfirmations, so a *failed* fix satisfied "confirmation the problem is
now resolved".

The stamp adds two root-scoped bars on top of the metadata bar. The
**per-root refutation window** (`_root_disconfirmation_turn`, strict `>`):
the cited row must be NEWER than any refutation recorded against the root
being confirmed — any author, any confidence. A hedged self-claimed failed
fix does not demote the root (§7.2), but the top grade is never minted from
a row recorded at-or-before it; only the user's strictly-newer post-refute
gone⇒gone completes `CONFIRMED`. And **content-level bearing**
(`_select_bearing_row`): the citation is the NEWEST candidate not
affirmatively **about a different chain** (≥2 shared content tokens with
another chain's statements while <2 with the frame — the root statement, its
mechanism rungs, its attached hypotheses, the problem anchors — is refused,
`absence_confirmation_bearing_rejected_total`). Recency beats specificity:
an older frame-echoing row (premature rows echo frame tokens by
construction) never outranks the user's actual latest confirmation, however
terse — a generic "user confirms it's working" row is fine, because the
handshake is the trust bar and a terse row must not strand a count-held root
at `NO_ROOT` (the INV-29 rescue). This is a mis-citation guard, not a trust
bar. Refusal never blocks the resolution itself; the grade stays honest
(`MECHANISTIC` when a validated root stands, `NO_ROOT` on the count-held
shape, where the held root stays INCONCLUSIVE and no conclusion is minted).

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
over-claim — and, under the §7.7 precedence, specifically a *fallback*
conclusion over-claiming with no validated root behind it, which is exactly the
population the seam exists to label.

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
