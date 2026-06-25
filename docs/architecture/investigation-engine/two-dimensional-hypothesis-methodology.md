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
  is registered only once the root is mechanistically validated (`cause_state ==
  IDENTIFIED`), else downgraded to a diagnostic with a recovery reason (engine
  veto, extends INV-23; mitigation exempt). *Quadrant-level precision (exempting
  `defensive_fix`) is deferred until the solution emission carries an
  `InterventionQuadrant`.*
- **Design-intent, not yet built** — the LLM satisfies these *behaviorally*; no
  engine gate enforces them: chain-level belief propagation (§9.4 — the engine
  still uses the per-evidence `+0.15 / −0.20` counter from
  [framework §6](./evidence-driven-investigation-framework.md#6-hypothesis-model));
  F3 signature-screening (§4); the F4 family-completeness sweep; and
  invalidation-first search prioritization (§5). Each promotes to the
  methodology-invariant registry (§0) as it is implemented.

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
| **M2** | A root cause is marked **confirmed** ("verified") only with **counterfactual** evidence — removing it removed `D` (`causal_absence_evidence`). *Gone ⇒ problem gone.* | Counterfactual unreachable → **CLOSE** on `symptom_absence`, never hang. | Engine-guard (resolution gate) |
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
  with signature-incompatible noise.

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
correlation are not validation.

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
   (S2). A non-exhaustive elimination simply concludes the wrong survivor.
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
