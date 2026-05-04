# Agent Stage Playbook

This playbook is the authoritative source for per-stage agent behavior in FaultMaven investigations. The duties, evidence requirements, user instruction patterns, and gate conditions defined here are the direct basis for what is injected into each stage's prompt template. When writing or updating a prompt block, this playbook is the spec: a duty absent from the prompt is a gap to close; a prompt instruction not grounded here should not exist.

Where [Agent Behavioral Rules](./agent-behavioral-rules.md) define **how the agent must behave at all times** (cross-cutting constraints), this playbook defines **what the agent must do at each stage** — the plays it runs, the evidence it collects, and the conditions that advance the investigation.

**The distinction:**

- Behavioral rules = good citizenship (do not speculate, do not claim execution, read inputs well)
- Stage playbook = execution (advance milestones, collect evidence, instruct the user precisely)

Both operate together. A rule says "never claim to execute code." The playbook says "in Zone 2, ask for the deployment log that distinguishes between your two active hypotheses." Neither is the other's substitute.

**Related Documents**:

- [Agent Behavioral Rules](./agent-behavioral-rules.md) — Cross-cutting behavioral constraints (Rules 1–8)
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) — State transitions, gate milestones, path selection
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) — Evidence classification design
- [Prompt Templates](./prompt-templates.md) — Where this playbook's content is encoded

---

## §1 Investigation Phase Model

This section defines the phase structure, phase markers, and transition mechanisms that the rest of the playbook is organized around.

### Phase Definitions

A **phase** is a distinct operational mode of the investigation. Each phase has a specific prompt assembly and a distinct set of agent duties. The active phase is identified by two signals: the `case.status` field (the authoritative top-level discriminator) and, within INVESTIGATING, a **phase marker**.

| `case.status` | Phase Marker | Phase Name | Prompt Assembly |
| ------------- | ------------ | ---------- | --------------- |
| `INQUIRY` | *(none — starting phase)* | INQUIRY | `INQUIRY_TEMPLATE` |
| `INVESTIGATING` | `problem_statement_confirmed = True` | DIAGNOSIS | `INVESTIGATION_BASE` + `DIAGNOSIS_INSTRUCTIONS` |
| `INVESTIGATING` | `mitigation_accepted = True` | MITIGATION | `INVESTIGATION_BASE` + `MITIGATION_INSTRUCTIONS` |
| `INVESTIGATING` | `solution_accepted = True` | TREATMENT | `INVESTIGATION_BASE` + `TREATMENT_INSTRUCTIONS` |
| `RESOLVED` | *(none — `case.status` is authoritative)* | TERMINAL | `TERMINAL_TEMPLATE` |
| `CLOSED` | *(none — `case.status` is authoritative)* | TERMINAL | `TERMINAL_TEMPLATE` |

TERMINAL is not a phase — it is a final state. RESOLVED and CLOSED differ in their generated summary type and runbook eligibility, but share the same prompt template.

### Phase Transitions

There are two distinct transition mechanisms:

**User-Agent Handshake** — A formal two-step process. The agent sets `proposed_transition` and presents exactly two COOPERATIVE suggestions (Yes/No). The transition completes only when the user selects "Yes." Used when explicit user consent is required.

**Inference-based** — The LLM detects user behavior (compliance, confirmation) in its structured output and sets the milestone directly. No confirmation dialog is presented.

| From | To | Mechanism | Condition |
| ---- | -- | --------- | --------- |
| INQUIRY | DIAGNOSIS | User-Agent Handshake | `problem_statement_confirmed` — user confirms the problem statement |
| INQUIRY | TERMINAL (RESOLVED) | User-Agent Handshake | Fast-track: KB match found and user confirms it resolved the issue |
| INQUIRY | TERMINAL (CLOSED) | User-Agent Handshake | User declines investigation |
| DIAGNOSIS | MITIGATION | Inference-based | `mitigation_accepted` — user accepts the proposed temporary fix |
| DIAGNOSIS | TREATMENT | Inference-based | `solution_accepted` — user acknowledges executing the proposed solution |
| MITIGATION | DIAGNOSIS | Inference-based | `mitigation_verified` — user confirms mitigation worked; `mitigation_accepted` and `mitigation_verified` reset to False |
| TREATMENT | TERMINAL (RESOLVED) | User-Agent Handshake | `solution_verified` — user confirms the solution resolved the issue |
| Any INVESTIGATING stage | TERMINAL (CLOSED) | User-Agent Handshake | User escalates or abandons |

### Phase Diagram

```text
INQUIRY
  ├── → DIAGNOSIS         (handshake: problem_statement_confirmed)
  ├── → TERMINAL/RESOLVED (fast-track: KB match confirmed)
  └── → TERMINAL/CLOSED   (handshake: user declines)

INVESTIGATING
  ├─ DIAGNOSIS [marker: problem_statement_confirmed]
  │    ├─ Zone 1: Symptom Verification  [symptom_verified = False]
  │    ├─ Zone 2: Root Cause Analysis   [symptom_verified = True, root_cause_identified = False]
  │    └─ Zone 3: Solution Proposal     [root_cause_identified = True, solution_proposed = False]
  │    ├── → MITIGATION  (inferred: mitigation_accepted)
  │    └── → TREATMENT   (inferred: solution_accepted)
  │
  ├─ MITIGATION [marker: mitigation_accepted]
  │    └── → DIAGNOSIS   (inferred: mitigation_verified; markers reset to False)
  │
  └─ TREATMENT [marker: solution_accepted]
       ├── → TERMINAL/RESOLVED  (handshake: solution_verified)
       └── → TERMINAL/CLOSED    (handshake: user abandons)

TERMINAL — immutable; Q&A only
  RESOLVED — resolution_summary always generated; runbook eligible (requires root cause)
  CLOSED   — closure_summary generated (subject to skip-if-trivial guardrail); no runbook generation
```

### Key Definitions

**Phase marker** — A boolean variable that both gates the transition INTO a phase and identifies the phase while active. The same variable serves both purposes: setting it to True triggers the transition; its True value identifies the active phase. A phase marker is introduced (as False) in the preceding phase and set to True at the transition boundary. When MITIGATION exits back to DIAGNOSIS, `mitigation_accepted` and `mitigation_verified` are both reset to False.

**User-Agent Handshake** — The formal two-step confirmation mechanism. The agent proposes a transition by setting `proposed_transition` and presenting exactly two COOPERATIVE suggestions: "Yes" (proceed) and "No" (stay). The transition executes only when the user selects "Yes." This is the only mechanism for INQUIRY→INVESTIGATING, TREATMENT→RESOLVED, and Any→CLOSED.

**Inference-based transition** — The LLM detects a specific user behavior (acknowledging execution, confirming stabilization) directly in its structured output and sets the milestone. No confirmation dialog is presented. Used for `mitigation_accepted`, `solution_accepted`, and `mitigation_verified`.

---

## §2 Progress Metrics

The GPS map. At any turn the agent reads this table to know where the investigation stands, what is blocking it, and what to ask the user next. Each row answers: which stage owns this variable, which prompt template is active, what must happen for the variable to advance, and what suggestion type communicates that to the user.

| Stage | Prompt | # | Variable | Type | Blocked until | Suggestion | § |
| ----- | ------ | - | -------- | ---- | ------------- | ---------- | - |
| INQUIRY | `INQUIRY_TEMPLATE` | — | — | — | Starting phase — no gate to enter | — | [INQUIRY](#inquiry-phase) |
| | | G | `problem_statement_confirmed` | Gate | User confirms problem statement | COOPERATIVE → INVESTIGATING | [INQUIRY](#inquiry-phase) |
| DIAGNOSIS | `DIAGNOSIS_INSTRUCTIONS` | 1 | `symptom_verified` | Diagnostic | User submits symptom evidence | EVIDENCE | [Zone 1](#zone-1-symptom-verification) |
| | | 8 | Hypothesis state | Analytical | Row 1 true — agent reasons from context + KB | — | [Zone 2](#zone-2-root-cause-analysis) |
| | | 2 | `root_cause_identified` | Diagnostic | User submits causal evidence | EVIDENCE | [Zone 2](#zone-2-root-cause-analysis) |
| | | 3 | `solution_proposed` | Action | Row 2 true — agent reasons to fix | — | [Zone 3](#zone-3-solution-proposal) |
| | | 4 | `mitigation_accepted` | Trigger | User acknowledges executing temp fix | COOPERATIVE → MITIGATION | [Zone 3](#zone-3-solution-proposal) |
| | | 6 | `solution_accepted` | Trigger | User acknowledges executing fix | COOPERATIVE → TREATMENT | [Zone 3](#zone-3-solution-proposal) |
| MITIGATION | `MITIGATION_INSTRUCTIONS` | 5 | `mitigation_verified` | Gate | User confirms mitigation worked | EVIDENCE → DIAGNOSIS | [MITIGATION](#mitigation-stage) |
| TREATMENT | `TREATMENT_INSTRUCTIONS` | 7 | `solution_verified` | Gate | User confirms solution worked | COOPERATIVE → TERMINAL | [TREATMENT](#treatment-stage) |
| TERMINAL | `TERMINAL_TEMPLATE` | — | — | — | No milestone tracking | — | [TERMINAL](#terminal-state) |

**Gate** variables are mandatory checkpoints: the investigation cannot leave the current stage until the condition is met (`problem_statement_confirmed`, `mitigation_verified`, `solution_verified`). **Trigger** variables open an optional branch: when the user acknowledges executing the proposed action, the stage redirects — but nothing in the current stage was blocked waiting for it (`mitigation_accepted`, `solution_accepted`).

Rows 8 and 3 are agent-internal — no user input required. The agent resolves them from available context and immediately advances to the next row. The Suggestion column shows the primary type when that variable is the frontier; FREE_SPEECH may supplement any EVIDENCE row when user judgment is also needed.

Multiple variables can advance in the same turn when evidence supports it. The agent processes all submitted evidence first, sets every variable the evidence justifies, then composes a response from the resulting new state — not from the state at turn start.

Scope, timeline, and change correlation are not tracked as separate variables. They are facts extracted from symptom evidence during diagnosis — scope and timeline emerge from the same evidence that confirms `symptom_verified`, and change events classify as `contextual_evidence` that feeds hypothesis formation. Their absence never blocks progress.

---

### Forward-Looking Message Structure

Every agent message has two parts, in order:

1. **Backward-looking** — answering the user's question. Governed by Agent Behavioral Rules (Rule 1: answer first).
2. **Forward-looking** — stating where the investigation stands and what is needed next. Governed by this playbook.

The forward-looking part has a fixed shape:

> **[Progress transparency]** What has been established and what remains open — in plain language, without milestone names.
> **[Specific ask]** What data, from what source, for what timeframe — or what user judgment is needed.

**Progress transparency:** translate milestone state into plain language. Not "symptom_verified is False" but "I haven't confirmed that the reported errors are actually occurring — the data doesn't show it yet." The user must understand where the investigation stands without knowing the internal model.

**Non-pressuring:** state what would help and why. Do not imply the user is blocking progress. The user decides what to provide and when.

**Specificity standard:** every EVIDENCE suggestion must specify three things:

- **What** — log type, metric name, config file
- **Where** — service name, host, pod, system
- **When** — timeframe, relevant window, "since the incident started"

A suggestion missing any of these is incomplete. When the source or timeframe is unknown, say so explicitly and ask the user to fill it in.

---

## §3 Phase Playbooks

---

### INQUIRY Phase

**Phase marker:** None — every case starts here  
**Prompt assembly:** `INQUIRY_TEMPLATE`  
**Variables managed:** `proposed_problem_statement`, `problem_statement_confirmed`

#### Purpose

Establish a shared understanding of the problem before investigation begins. The metrics framework is not yet active — no milestones are tracked during INQUIRY.

#### Agent Duties

**When no problem signal is present:**

1. Answer the user's question directly (Rule 1). Do not propose a problem statement or initiate investigation.

**When a problem signal is detected** (error, slowness, outage, anomaly):

1. **Extract the problem signal** — identify: what is failing, scope, temporal state (ongoing vs historical), business impact.
2. **Assess urgency** — classify as CRITICAL / HIGH / MEDIUM / LOW:
   - CRITICAL: revenue loss, production down, data loss, customers affected
   - HIGH: checkout failing, payments broken, 30%+ requests failing, SLA violation
   - MEDIUM: intermittent, partial failure, degraded experience
   - LOW: historical, post-mortem, optimization, cosmetic
3. **Search KB first** (`kb_qa`) — before proposing investigation, check whether a past case or runbook resolves the issue. If a match exists, surface it and await the user's attempt.
4. **Propose a problem statement** — one sentence: symptom + scope + temporal state. Set `proposed_problem_statement`.
5. **Surface mitigation path hint for CRITICAL/HIGH + ONGOING** — "This is actively affecting [scope]. Should I focus on a quick mitigation first while we find the root cause?"
6. **Await confirmation** — do not transition, request evidence, or propose next steps on the same turn as the problem statement.

#### Gate Conditions

| To | Mechanism | Condition |
| -- | --------- | --------- |
| DIAGNOSIS | User-Agent Handshake | `problem_statement_confirmed = True` — user explicitly confirms the problem statement |
| TERMINAL (RESOLVED) | User-Agent Handshake | Fast-track: KB match found and user confirms it resolved the issue |
| TERMINAL (CLOSED) | User-Agent Handshake | User declines to investigate |

#### Anti-Patterns

- Requesting diagnostic evidence before the problem statement is confirmed
- Treating data uploads or continued engagement as confirmation ("yes" must be explicit)
- Proposing a problem statement when the user asks a general knowledge question

---

### DIAGNOSIS Stage

**Phase marker:** `problem_statement_confirmed = True`  
**Prompt assembly:** `INVESTIGATION_BASE` + `_get_diagnosis_focus_emphasis()` + `DIAGNOSIS_INSTRUCTIONS`

DIAGNOSIS has three internal zones. Zone membership is determined by the diagnostic variables; the agent reads the current state and applies the corresponding zone duties.

---

#### Zone 1: Symptom Verification

**Target variable:** `symptom_verified`  
**Evidence type:** `symptom_evidence`

**Agent duties:**

1. Apply the three-step diagnostic pattern: search for symptom signatures using `search_file` → evaluate against conclusive criteria → advance with citation or ask specifically.
2. When asking for data, apply the specificity standard: what log or metric, from what source, for what timeframe.
3. Do not form hypotheses until `symptom_verified = True`.
4. **Extract scope and timeline from the symptom evidence.** When evidence confirms the symptom, actively note and state:
   - **Scope** — how many systems, services, pods, or users are affected. Wide scope (multiple regions, many pods) signals a systemic cause; narrow scope (single pod, single user) signals an isolated cause. This directly shapes which hypothesis categories Zone 2 prioritises first.
   - **Timeline** — the first occurrence timestamp. This becomes the anchor for all Zone 2 searches — every evidence request in Zone 2 should reference this window. Without a timeline, change searches are unbounded and noisy.
   - These are extracted facts, not tracked variables. State them explicitly in the response when found. Do not delay `symptom_verified` waiting for them, but actively look for them in the same evidence.

**Search for:**

- Error messages: "error", "exception", "failed", "timeout", "refused", HTTP 5xx codes
- Performance anomalies: latency spikes, error rate increase, throughput drop, queue depth
- Alert signals: pager events, health check failures, circuit breaker open
- Service failure: pod restarts, process crashes, connection pool exhaustion

**Conclusive when:** specific errors with count and timestamp range are found in the data, or a metric directly shows the reported anomaly, and the evidence is from the affected system — not unrelated background noise.

**When not conclusive — ask specifically:**

- Something found but unclear: "I see [X] in the log — is this the error users are hitting, or unrelated noise?"
- Nothing found: "I can't find evidence of [symptom] in this file. The [log type] from [source] for the [timeframe] window would confirm it — can you provide that?"

**Evidence and variable sequence:** when confirmed, the agent must (1) create a `symptom_evidence` record in `evidence_to_add` with summary "[N] [error type] in [source] between [start] and [end]", then (2) set `symptom_verified=True` in state updates. In the response, cite the finding explicitly. Setting the variable without a corresponding evidence record violates the evidence-driven progress rule.

**User instruction patterns:**

| Situation | Type | Specificity requirement |
| --------- | ---- | ----------------------- |
| Need error logs | EVIDENCE | Log type + service/host + timeframe |
| Need metrics | EVIDENCE | Metric name + system + window |
| Clarify if evidence matches the symptom | FREE_SPEECH | Name the specific pattern found; ask if it represents the reported issue |
| Diagnostic command | COOPERATIVE (command_copy) | Exact command with parameters |

**Anti-patterns:**

- Forming hypotheses before `symptom_verified`
- Classifying evidence as `causal_evidence` before a hypothesis exists
- Setting `symptom_verified` without citing specific evidence found

---

#### Zone 2: Root Cause Analysis

**Target variable:** `root_cause_identified` (via hypothesis state)  
**Evidence type:** `causal_evidence`

**Hypothesis state** is the analytical bridge between `symptom_verified` and `root_cause_identified`. It is not a boolean — it is a lifecycle with confidence scoring.

**Hypothesis formation:** after `symptom_verified = True`. Each hypothesis must state: suspected cause, proposed mechanism, what evidence would confirm it, and what would refute it.

**Hypothesis precision requirement:** a hypothesis must state a mechanism, not just a trigger. "The deployment at 14:28 caused the issue" is a trigger observation — it is not a hypothesis. "The deployment changed `max_connections` from 100 to 10, causing connection pool exhaustion which produced timeouts at 14:31" is a hypothesis — it names the specific change and the mechanism that produces the symptom.

**Hypothesis lifecycle:**

| State | Meaning | Requirement |
| ----- | ------- | ----------- |
| `CAPTURED` | Just recorded, not yet under investigation | — |
| `ACTIVE` | Under active investigation | — |
| `VALIDATED` | likelihood ≥ 0.70 + 2+ supporting evidence | Enables `root_cause_identified` |
| `REFUTED` | Evidence directly disproves it | Requires `refutation_reason` citing specific evidence |
| `INCONCLUSIVE` | likelihood 0.3–0.5, stagnant 3+ turns without new evidence | Set by progress monitor |
| `RETIRED` | Abandoned without disproof | No reason required |

Use `REFUTED` only when disproof exists. When there is no evidence of disproof, use `RETIRED`.

**Prerequisite:** a hypothesis must exist before causal evidence can be classified. The sequence is fixed: form hypothesis → search → evaluate → validate. Never classify causal evidence without a corresponding hypothesis.

**Agent duties:**

1. **Search KB first (once, at Zone 2 entry)** — call `kb_qa` for the confirmed symptom before generating hypotheses. If a runbook matches, follow its diagnostic steps as the default approach. Do not call `kb_qa` in Zone 1 — KB contains procedures, not incident facts.
2. **Use scope to prioritise hypothesis categories.** Wide scope (multiple services, regions, pods) → systemic hypotheses first: shared dependency failure, network issue, config push affecting all instances. Narrow scope (single pod, user, endpoint) → isolated hypotheses first: pod-specific config, user-specific data, targeted code path.
3. **Use timeline as the search anchor.** Every evidence request in Zone 2 must reference the timeline window established in Zone 1. Before generating hypotheses, run a targeted search for change events just before the timeline: deployments, updates, config pushes, scaling events. A change event near the timeline raises confidence in a deployment/change hypothesis — classify it as `contextual_evidence`. Then drill into the specific changes made to find the candidate root cause.
4. Apply the hypothesis-evidence ordering: form hypothesis → apply three-step pattern for `root_cause_identified` → validate or refute.
5. **Single-shot vs multi-hypothesis:** if the root cause is obvious from existing evidence (clear error chain, strong timing correlation, specific change found), form one hypothesis and validate in the same turn. If ambiguous, form 2–4 hypotheses across different categories and request targeted evidence per hypothesis.
6. Each evidence request must be tied to a specific hypothesis and follow the specificity standard.
7. Refute with reason; retire without. Never use `REFUTED` without evidence of disproof.

**Search for (per hypothesis category):**

- Deployment / change — **two steps, distinct evidence types:**
  - Step 1: Find the **change event** (deployment timestamp, update applied, config push, scaling event near the timeline window) → `contextual_evidence`. This is a trigger signal — it narrows the search space and raises hypothesis confidence, but is not itself a root cause.
  - Step 2: Drill into the **specific changes made** in that event (code diff, config values before/after, dependency version change, schema alteration) → `causal_evidence` once a hypothesis links a specific change to the symptom mechanism. A deployment is a trigger; the changed `max_connections` value is a candidate root cause.
- Resource exhaustion: memory / CPU / disk / connection counts at or near limits
- Dependency failure: downstream service timeouts, external API errors, database failures
- Code / query defect: slow query logs, exception stack traces, query plan changes

**Conclusive when:** an active hypothesis has confidence ≥ 70%, evidence directly links the proposed cause to the symptom (timing, error chain, or mechanism match), and no alternative hypothesis is equally supported.

**When not conclusive — ask specifically:**

- Change event search empty: "Were there any deployments, config changes, or infrastructure updates around [timeline window]? If so, what changed?"
- Causal mechanism search empty: "Which component or config controls [mechanism from hypothesis]? Can you share its current and previous values?"
- Partial support: "The timing matches — [deployment at 14:28, errors at 14:31] — but I need [specific log] from [source] for [timeframe] to confirm the mechanism."
- Two hypotheses tied: "This could be [A] or [B]. [Specific file or metric] would distinguish them — can you provide that?"

**Evidence and variable sequence:** when root cause is confirmed, the agent must follow the hypothesis-evidence ordering: (1) create hypothesis in `hypotheses_to_add`, (2) classify evidence as `causal_evidence` in `evidence_to_add` and link to the hypothesis, (3) set `root_cause_identified=True` if confidence ≥ 0.7. The variable cannot be set without a corresponding hypothesis and causal evidence record.

**User instruction patterns:**

| Situation | Type | Specificity requirement |
| --------- | ---- | ----------------------- |
| Evidence to test a hypothesis | EVIDENCE | What to find + which log or system + timeframe |
| Diagnostic command | COOPERATIVE (command_copy) | Exact command targeting the hypothesis |
| Clarify ambiguous evidence | FREE_SPEECH | Name what is ambiguous; ask the specific question |

**Anti-patterns:**

- Classifying `causal_evidence` without a corresponding hypothesis
- Using `REFUTED` without `refutation_reason`
- Using `REFUTED` when there is no evidence of disproof (use `RETIRED`)
- Requesting evidence not tied to a specific hypothesis
- Treating a change event (deployment, update) as a root cause — it is a trigger; the root cause is the specific change within it
- Setting `root_cause_identified` with only a trigger observation and no mechanism

---

#### Zone 3: Solution Proposal

**Target variable:** `solution_proposed`

`solution_proposed` is set by the agent when it creates a `ProposedAction` with `action_type=SOLUTION`. It is the agent's own act — no detection required.

**Agent duties:**

1. State the root cause in one sentence before proposing a fix.
2. Propose a direct, executable action — instruction form, not a question ("Run: [command]", not "Would you like to try X?").
3. State impact: reversible or not, blast radius (single pod, cluster, database, shared service).
4. Do not request further evidence after `root_cause_identified` is set. Hold until the user executes or raises an objection.
5. `solution_proposed` does not require a new `evidence_to_add` record — it is set when the proposal is issued, derived from causal evidence already linked to the hypothesis.
6. While awaiting compliance (`solution_proposed=True`), offer exactly two COOPERATIVE suggestions: (1) the user reports the outcome ("I ran the command — here's the result"), (2) the user asks for clarification about the fix. Do not offer EVIDENCE or FREE_SPEECH suggestions in this state.

**Trigger variables in Zone 3:**

**`mitigation_accepted`** — Set when the user accepts/agrees to apply the proposed temporary fix. Acceptance alone is sufficient — no evidence required. Execution happens inside MITIGATION. Do not wait for "I've already done it" — "yes", "let's do it", "apply the fix now" are the signals.

**`solution_accepted`** — Set when the user acknowledges executing the proposed solution. Acknowledgement alone is sufficient — no evidence required.

*Is compliance (any one suffices):*

- User uses past tense: "I ran it", "I applied the patch", "Restarted the service"
- User reports a result: "It reduced errors by 80% — what next?" (implies execution)
- User submits post-action output — also qualifies; additionally advances `mitigation_verified` / `solution_verified` if outcome is visible

*Is NOT compliance:*

- "Thanks, I'll try it" — intent, not execution
- User asks clarifying questions about the command
- User goes silent

**Transitions out of Zone 3 (inference-based):**

| Trigger | To | Condition |
| ------- | -- | --------- |
| `mitigation_accepted` | MITIGATION | User acknowledges executing the proposed temp fix |
| `solution_accepted` | TREATMENT | User acknowledges executing the proposed solution |

**Anti-patterns:**

- Proposing a fix as a question
- Proposing multiple competing fixes simultaneously
- Requesting more evidence after `root_cause_identified`

---

#### When DIAGNOSIS Stalls

If two full hypothesis cycles complete with no convergence:

1. State what is established: verified symptom, evidence analyzed, hypotheses tested and outcomes.
2. State the boundary: "I cannot determine the cause without [specific data or access]."
3. Present options: data that would resolve the ambiguity, alternative diagnostic angles, escalation, or pausing with state preserved.

A well-documented partial investigation that narrows the problem is a valid outcome.

---

### MITIGATION Stage

**Phase marker:** `mitigation_accepted = True`  
**Prompt assembly:** `INVESTIGATION_BASE` + `MITIGATION_INSTRUCTIONS`

Apply a temporary fix to reduce immediate impact while root cause analysis is blocked or pending. Goal is stabilization, not resolution.

**Agent Duties:**

1. **Search KB first** — call `kb_qa` for the symptom to find known mitigation procedures or workarounds before suggesting steps. If a match is found, follow those steps. If not, proceed with general knowledge for the technology stack.
2. Provide numbered steps framed as user instructions, not agent actions.
3. State rollback: what to do if the fix makes things worse. State the fix is temporary.
4. Request `mitigation_evidence` — post-fix metrics, error rates, user observation.
5. Accept subjective confirmation for `mitigation_verified`. When confirmed: (1) create a `mitigation_evidence` record in `evidence_to_add`, (2) set `mitigation_verified=True`.
6. Iterate if ineffective — propose a modified approach, stay in MITIGATION.
7. Do not form hypotheses or classify `causal_evidence` here.

**`mitigation_verified`** — Set when the user confirms stabilization. Subjective confirmation is sufficient: "it's better", "errors dropped", "seems stable". Specific metric values are not required. On exit, `mitigation_accepted` and `mitigation_verified` are both reset to False before DIAGNOSIS resumes.

**When mitigation stalls:** If multiple attempts have failed and safe options are exhausted, do not continue proposing variants. Offer exactly two COOPERATIVE suggestions: (1) "Accept current state and proceed to root cause" — creates a `mitigation_evidence` record (source_type: text) and sets `mitigation_verified=True` to return to DIAGNOSIS even with partial stabilization; (2) "Escalate to a human expert" — acknowledges the investigation has reached its limit.

**Gate Conditions:**

| To | Mechanism | Condition |
| -- | --------- | --------- |
| DIAGNOSIS (Zone 2 or 3) | Inference-based | `mitigation_verified = True` — phase markers reset to False; RCA resumes |
| TERMINAL (CLOSED) | User-Agent Handshake | User selects escalation from stall-breaker or abandons |

**Anti-Patterns:**

- Pursuing root cause analysis during MITIGATION
- Giving up after one failed attempt
- Not stating the fix is temporary or not providing rollback

---

### TREATMENT Stage

**Phase marker:** `solution_accepted = True`  
**Prompt assembly:** `INVESTIGATION_BASE` + `TREATMENT_INSTRUCTIONS`

Verify that the applied fix resolves the problem. If it does not, diagnose the failure and revise — without returning to DIAGNOSIS.

#### Agent Duties — Primary path (fix verification)

1. Search post-fix data for resolution signals — error rate drop, latency return to baseline, service health restored.
2. Accept subjective confirmation for initial verification.
3. Connect the dots explicitly: "The [evidence] confirms [hypothesis] was correct and the fix resolved it."

#### Agent Duties — Failure path (fix did not work)

1. Create a `solution_evidence` record in `evidence_to_add` recording the failed outcome: summary "Fix [description] failed — [what was observed]", category `solution_evidence`. This records the failed attempt in the evidence trail regardless of failure type.
2. Determine failure type: implementation error (wrong command, missing step) → correct and re-propose without new evidence. Theory wrong → the original hypothesis was incorrect; new evidence is required before a new proposal.
3. State what the failure eliminates and what is now unclear. Request NEW evidence with full specificity before revising a theory.
4. Refute the disproven hypothesis with `refutation_reason` citing the failed fix. Any new diagnostic data gathered here must be classified as `causal_evidence` (linkable to hypotheses), never `solution_evidence`.

#### Agent Duties — Primary path (fix verified successfully)

1. Analyze post-fix data from the structural index; call `search_file` if specific patterns are needed.
2. When outcome is confirmed: create a `solution_evidence` record in `evidence_to_add` (summary "Fix verified: [what resolved and how]", source_type: logs | metrics | text), then proceed to completion.
3. Accept subjective confirmation: "it's working", "looks good" is sufficient.

#### Agent Duties — Completion (two-step handshake)

**`solution_verified`** requires the explicit User-Agent Handshake. Cannot be set from ambiguous confirmation.

1. When evidence confirms resolution, set `proposed_transition` to RESOLVED.
2. Offer exactly two COOPERATIVE suggestions: "Yes, mark as resolved" / "Not yet, I want to investigate further."
3. After user confirms: the backend sets `solution_verified=True` via `confirm_pending_transition`. The agent's job ends at setting `proposed_transition`. Remind the user to revert any mitigation workaround still in place.

**Gate Conditions:**

| To | Mechanism | Condition |
| -- | --------- | --------- |
| TERMINAL (RESOLVED) | User-Agent Handshake | `solution_verified = True` — user selects "Yes, mark as resolved" |
| TERMINAL (CLOSED) | User-Agent Handshake | User escalates or abandons |

**Anti-Patterns:**

- Proposing a different solution after failure without collecting new evidence
- Returning to DIAGNOSIS after a failed fix (stay in TREATMENT)
- Setting `solution_verified` without the handshake

---

### TERMINAL State

**Phase marker:** None — `case.status = RESOLVED` or `case.status = CLOSED` is authoritative  
**Prompt assembly:** `TERMINAL_TEMPLATE`

| Terminal sub-state | Trigger | Summary type | Runbook eligible |
| ------------------ | ------- | ------------ | ---------------- |
| RESOLVED | `solution_verified = True` via handshake | `resolution_summary` | Yes |
| CLOSED | User handshake (any INVESTIGATING stage or INQUIRY) | `closure_summary` | No |

No investigation. Answer questions using existing case data (evidence, hypotheses, solutions, action history, summary report). Do not accept new evidence, advance milestones, or propose transitions. If the user describes ongoing issues, direct them to open a new case.

---

## §4 Cross-Phase Constraints

### Suggestion Quality Standard

| Type | Required specificity |
| ---- | -------------------- |
| EVIDENCE | What to collect + where/from + timeframe |
| FREE_SPEECH | What specific knowledge is needed + why it matters for the current variable |
| COOPERATIVE (command_copy) | Exact command with all parameters filled in |
| COOPERATIVE (query_submit) | Ready-to-submit request, pre-composed |

One primary ask per turn. Stack only when items are genuinely parallel (e.g., two log files that always come together).

### Suggestion Count by Stage

| Stage | Expected suggestions |
| ----- | -------------------- |
| INQUIRY | 2 COOPERATIVE (yes/no confirmation) |
| DIAGNOSIS Zone 1 | 1–2 EVIDENCE + 1 FREE_SPEECH |
| DIAGNOSIS Zone 2 | 1 EVIDENCE (targeted to hypothesis) + optionally 1 COOPERATIVE (command) |
| DIAGNOSIS Zone 3 (proposing fix) | 1 COOPERATIVE command_copy (the fix) |
| DIAGNOSIS Zone 3 (pending — fix already proposed) | 2 COOPERATIVE query_submit (report outcome / ask clarification); no EVIDENCE or FREE_SPEECH |
| MITIGATION (guiding) | 1 COOPERATIVE command_copy + 1 EVIDENCE (post-fix verification) |
| MITIGATION (stalled) | 2 COOPERATIVE query_submit (accept partial state / escalate) |
| TREATMENT (verifying) | 1 EVIDENCE (post-fix) or 0 if user already confirmed |
| TREATMENT (completion) | 2 COOPERATIVE (yes resolved / not yet) |
| TERMINAL | 1–2 COOPERATIVE (report, runbook) |

---

## §5 Prompt Injection Map

Each section of this playbook maps to a concrete injection point in `templates.py`. The agent duties, anti-patterns, and gate conditions in each stage section above are the spec; the blocks below are where they live in code.

### Assembly Map

There is no single complete prompt. Each turn assembles a prompt from a fixed outer shell plus injected components. The table below shows the full assembly for every stage and mode.

| Stage / Mode | Outer shell | Behavioral blocks embedded | Stage instruction block | Evidence grounding |
| --- | --- | --- | --- | --- |
| INQUIRY | `INQUIRY_TEMPLATE` | `_READING_DISCIPLINE` `_DATA_CITATION` `_ADVISOR_ROLE` `_ACTION_IMPACT` | (built into shell) | ✗ |
| DIAGNOSIS | `INVESTIGATION_BASE` | `_READING_DISCIPLINE` `_DATA_CITATION` `_ADVISOR_ROLE` `_ACTION_IMPACT` | `focus_emphasis()` + `DIAGNOSIS_INSTRUCTIONS` | `_EVIDENCE_GROUNDING` |
| DIAGNOSIS (mitigation-first path) | `INVESTIGATION_BASE` | same | `MITIGATION_FIRST prefix` + `focus_emphasis()` + `DIAGNOSIS_INSTRUCTIONS` | `_EVIDENCE_GROUNDING` |
| MITIGATION | `INVESTIGATION_BASE` | same | `MITIGATION_INSTRUCTIONS` | `_EVIDENCE_GROUNDING` |
| TREATMENT | `INVESTIGATION_BASE` | same | `TREATMENT_INSTRUCTIONS` | `_EVIDENCE_GROUNDING` |
| Knowledge query (mode bypass) | `INVESTIGATION_BASE` | same | `KNOWLEDGE_QUERY_INSTRUCTIONS` | ✗ (suppressed) |
| TERMINAL | `TERMINAL_TEMPLATE` | `_ADVISOR_ROLE` only | (built into shell) | ✗ |
| Fallback INQUIRY | `FALLBACK_INQUIRY_TEMPLATE` | none | none | ✗ |
| Fallback INVESTIGATING | `FALLBACK_INVESTIGATION_TEMPLATE` | none | none | ✗ |
| Fallback TERMINAL | `FALLBACK_TERMINAL_TEMPLATE` | none | none | ✗ |

`INVESTIGATION_BASE` is the only shell that accepts injected components at runtime (`{adaptive_instructions}` and `{evidence_grounding}`). All other shells are self-contained. Fallback templates are minimal and used only when the primary assembly fails (token limit, provider error).

`SCHEMA_INSTRUCTIONS` is appended to the final prompt by `milestone_engine.py` at call time — not part of any template. It is conditional: only injected when the LLM provider requires the JSON schema embedded in the prompt (providers using `json_object` or `prompt_only` structured output mode). Providers with native structured output support skip it.

`_READING_DISCIPLINE`, `_DATA_CITATION`, `_ADVISOR_ROLE`, and `_ACTION_IMPACT` are present in both INQUIRY and INVESTIGATION_BASE but absent from TERMINAL — terminal turns do not do diagnostic reasoning, do not propose actions, and do not need evidence grounding.

### Stage Instruction Blocks

Stage instructions are injected as `{adaptive_instructions}` in `INVESTIGATION_BASE`:

| Stage | Instruction block | Note |
| ----- | ----------------- | ---- |
| INQUIRY | `INQUIRY_TEMPLATE` (YOUR TASK section) | Self-contained shell |
| DIAGNOSIS | `_get_diagnosis_focus_emphasis(progress)` + `DIAGNOSIS_INSTRUCTIONS` | Emphasis prepended at assembly time |
| MITIGATION | `MITIGATION_INSTRUCTIONS` | Injected as `{adaptive_instructions}` |
| TREATMENT | `TREATMENT_INSTRUCTIONS` | Injected as `{adaptive_instructions}` |
| TERMINAL | `TERMINAL_TEMPLATE` | Self-contained shell |
| Knowledge query | `KNOWLEDGE_QUERY_INSTRUCTIONS` | Replaces stage dispatch entirely |

`_get_diagnosis_focus_emphasis()` maps the three DIAGNOSIS zones plus the pending state to a contextual status signal prepended to `DIAGNOSIS_INSTRUCTIONS`:

| Zone | Condition | Emphasis |
| ---- | --------- | -------- |
| Zone 1 | `symptom_verified = False` | "Symptom verification pending — search for evidence the problem exists" |
| Zone 2 | `symptom_verified = True`, `root_cause_identified = False` | "Root cause analysis — form hypotheses, search for causal evidence" |
| Zone 3 | `root_cause_identified = True`, `solution_proposed = False` | "Solution needed — propose a concrete, executable fix" |
| Pending | `solution_proposed = True` | "Solution proposal issued — awaiting execution. Do not request further evidence or introduce alternative proposals." |

**`knowledge_query` mode bypass:** When `processing_mode == "knowledge_query"`, stage dispatch is skipped entirely. `KNOWLEDGE_QUERY_INSTRUCTIONS` replaces `adaptive_instructions` and `_EVIDENCE_GROUNDING_BLOCK` is set to `""`. This handles pure KB questions without investigation context.
