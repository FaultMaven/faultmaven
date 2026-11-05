# ADR-021: Earned-Autonomy Direction — the seams Phase 1 leaves for Phases 2 and 3

**Date**: 2026-11-05
**Status**: Proposed
**Decision Makers**: Engineering Leadership, Product Team
**Affects**: Investigation engine, agent module, jobs infrastructure, public roadmap

---

## Context

The public roadmap ([faultmaven.ai/roadmap](https://www.faultmaven.ai/roadmap)) commits to three
phases under one thesis, *trust first, autonomy earned*:

| Phase | Name | Actuator | Data access | Terminal |
|-------|------|----------|-------------|----------|
| 1 (shipped) | Copilot | the **user** runs what the agent recommends | what the user pastes, uploads or captures | RESOLVED / CLOSED by user handshake |
| 2 (next) | Investigator | **nobody** — the engine works a bundle end to end | an offline bundle (`.zip` / `.tar.gz`) | a graded diagnosis and remediation plan |
| 3 (roadmap) | Integrated Agent | the **agent** itself, under configured approval | live connectors (observability, cloud APIs, Kubernetes, paging) reached through tool calls | the incident resolved, or escalated |

The roadmap aligns the phases on *access*: each one reaches further into the customer's
stack. It does not say how they align on *mechanism*, and today they do not. The Phase 1
engine's control flow is driven by the user at every gate (`investigation-lifecycle-logic.md`,
`orchestration-capabilities.md` §3):

- Gate 1 needs the user to confirm the problem statement.
- Stage transitions fire on **user compliance** — the user runs the proposed check and
  pastes the output (`mitigation_accepted`, `solution_accepted`).
- Verification evidence is whatever artifact the user brings back (`mitigation_verified`,
  `solution_verified`).
- RESOLVED / CLOSED need a two-turn user handshake (`OfferRecord`).
- A turn is one HTTP request bounded at the server timeout; the agent's tools are read-only;
  mid-turn checkpoints and resume were retired on purpose (#1882).

Run unattended, this engine stalls in DIAGNOSIS: nothing confirms Gate 1, no gate milestone
can fire, and RESOLVED is unreachable because nothing was fixed. So Phase 2 is **not Phase 1
without the user**, and Phase 3 is not Phase 2 with write tools. Each phase changes who
acts, and the engine currently hard-codes the answer.

What does carry across unchanged is the **method**, which the roadmap promises will never
change: claims are not evidence (INV-08), every evidence row cites a stored artifact, the
causal graph derives what is validated or refuted, the engine vetoes what the derivation does
not support, and a conclusion carries an assurance grade (`cause_assurance.py`: no root /
mechanistic / confirmed).

This record fixes the seams Phase 1 must leave so that Phases 2 and 3 are *extensions* of
the engine rather than a second and third loop. It is a direction, not a feature design: it
decides what Phase 1 must not foreclose, and it deliberately leaves the Phase 2 and Phase 3
feature sets open (§"Non-decisions").

Related: the engine design-flaw tracking issue
[FaultMaven/faultmaven#2516](https://github.com/FaultMaven/faultmaven/issues/2516), whose
items are the concrete Phase 1 work this record depends on.

---

## Decision

Seven seams. D1–D4 are the ones Phase 1 must open before Phase 2 starts; D5–D7 shape how
Phase 2 is built so Phase 3 can plug into it.

### D1. The actuator is an abstraction, not the user

The engine's gate milestones ask *was the recommended action performed, and was its result
captured as an artifact*. Today both halves mean "the user pasted output". They become a
single **actuator** interface with three implementations across the phases:

| Phase | Actuator | "performed" means | "result captured" means |
|-------|----------|-------------------|-------------------------|
| 1 | `UserActuator` | the user says they ran it, or the uploaded output shows it | the user's upload / paste |
| 2 | `BundleActuator` | the check is answerable from the bundle, or it is not | a slice of the bundle, recorded as an `UploadedFile` extract |
| 3 | `ToolActuator` | the agent ran it through a connector, under approval policy | the connector's output, stored as an artifact |

Gate semantics, the source invariant and the assurance grades do not change. Only the
object that performs actions and returns artifacts does. The affordance projection
(`offer_projection.py`) keeps asking the actuator what it can do, so a UI chip in Phase 1
and a tool call in Phase 3 are the same question answered differently.

**Phase 1 consequence:** name the seam now. Wherever the engine reads "the user did X"
(compliance inference, Gate 1 confirmation, handshake consent, the `in_memory_case`
contract in `ToolContext`), route it through one interface with `UserActuator` as its only
implementation. No behaviour changes.

### D2. An investigation is a resumable job; a turn is one step of it

Phase 2 cannot run inside a 120-second request, and Phase 3 must pause for approval and
survive a multi-hour incident. Both need a durable unit of work above the turn. The decision:

- A **turn** keeps its current contract — prepared in memory, committed once, or not at all
  (`TurnCommitPlan`). This is the atomicity guarantee the design leans on, and it stays.
- An **investigation job** is a sequence of turns driven by a policy: in Phase 1 the policy
  is "wait for the user", in Phase 2 "the engine poses its own next step until the bundle
  is exhausted or a stop condition fires", in Phase 3 "the engine acts, pausing where the
  approval policy says so".
- The job's durable state is the committed case plus a job row; resuming is loading the
  case and running the next turn. **No mid-turn checkpoint is reintroduced.** This honours
  the #1882 ruling: per-turn commits are the checkpoints, and they already exist.

`infrastructure/jobs/job_service.py` and `modules/agent/jobs/` are the home. Phase 1 gains
nothing user-visible from this, but Phase 2's self-driving loop and Phase 3's approval pause
are both "a job policy", not new engines.

### D3. A graded diagnosis is a first-class terminal

Phase 1 ends a case RESOLVED or CLOSED by handshake. Phase 2 has no one to shake hands with
and nothing was fixed, so its honest terminal is **a diagnosis with an assurance grade**:

- Bundle-only work can reach *mechanistic* (the causal chain is supported by evidence in the
  bundle) but never *confirmed* (counterfactual proof needs a fix observed to work).
- The Phase 2 report states its grade and what evidence would raise it. That grade is the
  product's differentiator against tools that return an ungraded guess, and it falls out of
  Phase 1's assurance ladder unchanged.
- `CaseState` gains no new value for this; the diagnosis terminal is a closure reason on
  the existing lifecycle, so the lifecycle invariants (`investigation-invariants.md`) hold.

**Phase 1 consequence:** the report generator and the closure-reason schema must be able to
emit a graded diagnosis without a handshake. Today both assume a user ending.

### D4. The turn is traced, and outcomes are scored

Items 2 and 4 of #2516. Phase 2's promise is "works on its own"; that is unverifiable
without a per-step trace of what the engine did and an outcome score for whether it was
right. Phase 3 adds "and acted", which must be auditable. The decision:

- Every model call, tool dispatch and repair step inside the tool loop
  (`_tool_augmented_generate`) emits an event. The same events stream to the client
  (progress for a long Phase 2 job) and are kept as the turn's trace.
- An outcome harness replays traces over the replay corpora in `tests/eval/` and scores:
  reached the right cause, in how many turns, how many ungrounded claims vetoed, final
  assurance grade.
- Phase 2 does not ship until its outcome score on bundle corpora is published alongside
  the Phase 1 baseline.

### D5. Bundle ingestion extends the upload path; it does not fork it

Phase 2's input is an archive. Today an upload is one file through the Tier 0/1
preprocessor to an `UploadedFile` with a structural index. The decision: an archive is
**many uploads with a shared provenance**, not a new evidence kind. Extraction produces
`UploadedFile` rows per member, the existing extractors and vectorization apply, and the
source invariant holds member by member. Cross-file correlation is a tool over those rows,
in the same registry Phase 3's connector tools will join.

### D6. Evidence gathering is a tool call, whatever the source

In Phase 2 the agent asks the bundle; in Phase 3 it asks a live system. Both go through the
`AgentTool` registry with the same `ToolContext`. The decision: Phase 2's bundle-query tools
are written against the registry interface, not against the bundle, so Phase 3 swaps the
backend (a connector, reached by MCP or a native client) without the loop noticing. The
registry already refuses a tool the call did not offer; in Phase 3 that gate is where the
approval policy attaches.

### D7. The remediation plan is a typed action list

Phase 2 returns "a remediation plan". Phase 3 executes one. The decision: the plan is a
typed list of actions (target, operation, preconditions, verification check, rollback) from
the first Phase 2 release, not prose. Phase 3's `ToolActuator` consumes that type; the
approval policy is a predicate over it; the knowledge base harvests it.

### The flywheel ties the phases together

The roadmap does not mention it, but the knowledge base is why the phases compound rather
than stack:

- Phase 1 harvests *confirmed* fixes into runbooks (INV-25).
- Phase 2 feeds graded diagnoses in, marked by grade, and reads runbooks out as hypotheses.
- Phase 3 runs pre-approved runbooks as its first action class, and its confirmed outcomes
  close the loop.

Each phase adds one writer and one reader. Phase 1 should make sure a harvested runbook
carries the typed action list (D7) so Phase 3 can run it.

---

## Non-decisions

Deliberately left open, because deciding them now would be guessing:

- The Phase 2 stop policy (when does a self-driving job give up) and its cost ceiling.
- Which connectors Phase 3 ships first, and whether MCP or native clients carry them.
- The approval policy language (per action class, per blast radius, per environment).
- Rollback and idempotency semantics for Phase 3 actions.
- Whether Phase 3's job needs sub-turn durability (an action that takes an hour). If it
  does, that is the point to revisit #1882, with the retention policy
  `orchestration-capabilities.md` §2.2 asks for designed first.
- Any Phase 2 / Phase 3 feature not needed to keep the seams above open.

A feature design for Phase 2 is written when Phase 1 meets the exit criteria below, against
the engine as it then stands.

---

## Phase 1 exit criteria

Phase 2 work starts when all of these hold. They are the Phase 1 items in #2516 plus the
seams above.

1. The actuator interface exists with `UserActuator` as its sole implementation (D1).
2. An investigation job row and a "wait for the user" policy exist and Phase 1 runs on it
   with no behaviour change (D2).
3. A graded diagnosis can be emitted as a closure reason without a handshake (D3).
4. The tool loop emits step events; a trace is kept per turn; the outcome harness scores
   the existing corpora and the score is published as the baseline (D4; #2516 items 2, 4).
5. Answer and state emission are separated, measured against that baseline (#2516 item 1).
6. Harvested runbooks carry a typed action list (D7).
7. The generic layer (router, budgets, repair, tool loop) depends on an abstract state
   protocol, so a `BundleActuator` and a bundle-query tool can be added without touching
   the case module (#2516 item 3).

Items 5 and 6 of #2516 (prompt-only invariants, side effects outside the commit) are
required for Phase 3's auditability but do not block Phase 2.

---

## Consequences

**Easier**

- Phase 2 becomes a job policy, an actuator, an ingestion extension and a terminal, each
  on an existing seam. Phase 3 becomes a second actuator, connector-backed tools and an
  approval predicate.
- "The method never changes" is true in code: the evidence layer, causal graph and
  assurance grades are untouched across all three phases.
- The roadmap's ordering is justified by dependency, not preference: Phase 3 needs Phase 2's
  job runtime and typed plan.

**Harder**

- Phase 1 carries abstraction it does not use yet (an actuator with one implementation, a
  job with one policy). The cost is bounded by requiring no behaviour change and full
  pinning by the existing tests.
- Two of the seven seams (D2, D4) touch the engine's most coupled files. They are sequenced
  after the trace work so the split follows real step boundaries.
- Public copy must change: Phase 2's "no turn-by-turn prompting" should be stated as
  "you can still step in", which is what D2's policy model delivers first; and the roadmap's
  closing line that the order is still open is no longer accurate.

---

## References

- [Roadmap page source](https://github.com/FaultMaven/faultmaven-website/blob/main/src/app/roadmap/page.tsx)
- [Investigation engine design](../investigation-engine/README.md) — lifecycle logic,
  orchestration capabilities (§1–2 retired checkpointing, §3 human in the loop)
- [Investigation invariants](../investigation-engine/investigation-invariants.md)
- [Engine design-flaw tracking issue #2516](https://github.com/FaultMaven/faultmaven/issues/2516)
- [ADR-001](./adr-001-monolith-evolution-strategy.md) — the evolution strategy this record
  continues
