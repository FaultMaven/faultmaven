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

## Progress Metrics

The investigation state at any point is fully described by 8 variables across 3 types. These are the common denominator across all stages — the map the agent navigates and the basis for both what the agent asks the user and how it processes submitted data.

| # | Variable | Type | Advanced by |
| - | -------- | ---- | ----------- |
| 1 | `symptom_verified` | Diagnostic | Agent — from evidence search and evaluation |
| 2 | `root_cause_identified` | Diagnostic | Agent — from hypothesis validation |
| 3 | `solution_proposed` | Action | Agent — set when `ProposedAction(SOLUTION)` is created |
| 4 | `mitigation_accepted` | Action | User — compliance detected in user message |
| 5 | `mitigation_verified` | Action | User — stabilization confirmed in user message |
| 6 | `solution_accepted` | Action | User — compliance detected in user message |
| 7 | `solution_verified` | Action | User — explicit handshake confirmation only |
| 8 | Hypothesis state | Analytical | Agent — constructed from evidence; bridges 1 → 2 |

Variables not in this table (`scope_assessed`, `timeline_established`, `changes_identified`) are secondary attributes. They are extracted from evidence when available and inform analysis, but their absence never blocks progress and they do not define state.

---

### Diagnostic Variables

A diagnostic variable tells the agent what to search for in submitted data. The pattern for both is identical:

1. **Search** — use `search_file` to look for specific signatures in the submitted data. Do not judge from the file_extract summary alone.
2. **Evaluate** — determine whether what was found meets the conclusive threshold.
3. **Advance or ask** — if conclusive, set the variable and cite the evidence explicitly. If not, ask the user with full specificity.

#### `symptom_verified`

**Evidence type:** `symptom_evidence`

**Search for:**

- Error messages: "error", "exception", "failed", "timeout", "refused", HTTP 5xx codes
- Performance anomalies: latency spikes, error rate increase, throughput drop, queue depth
- Alert signals: pager events, health check failures, circuit breaker open
- Service failure: pod restarts, process crashes, connection pool exhaustion

**Conclusive when:** specific errors with count and timestamp range are found in the data, or a metric directly shows the reported anomaly, and the evidence is from the affected system — not unrelated background noise.

**When not conclusive — ask specifically:**

- Something found but unclear: "I see [X] in the log — is this the error users are hitting, or unrelated noise?"
- Nothing found: "I can't find evidence of [symptom] in this file. The [log type] from [source] for the [timeframe] window would confirm it — can you provide that?"

**Citation requirement:** "Setting symptom verified — found [N] [error type] errors in [source] between [start] and [end]."

---

#### `root_cause_identified`

**Evidence type:** `causal_evidence`

**Prerequisite:** a hypothesis must exist before causal evidence can be classified. The sequence is fixed: form hypothesis → search → evaluate → validate. Never classify causal evidence without a corresponding hypothesis.

**Search for (per hypothesis category):**

- Deployment / change: timestamps correlating with symptom onset, config diffs, rollout events
- Resource exhaustion: memory / CPU / disk / connection counts at or near limits
- Dependency failure: downstream service timeouts, external API errors, database failures
- Code / query defect: slow query logs, exception stack traces, query plan changes

**Conclusive when:** an active hypothesis has confidence ≥ 70%, evidence directly links the proposed cause to the symptom (timing, error chain, or mechanism match), and no alternative hypothesis is equally supported.

**When not conclusive — ask specifically:**

- Partial support: "The timing matches — [deployment at 14:28, errors at 14:31] — but I need [specific log] from [source] for [timeframe] to confirm the mechanism."
- Two hypotheses tied: "This could be [A] or [B]. [Specific file or metric] would distinguish them — can you provide that?"

**Citation requirement:** "Root cause identified — [hypothesis] at [N]% confidence, supported by [specific evidence]."

---

### Action Variables

Action variables are not driven by evidence search. They are compliance signals detected in the user's message, or set directly by the agent's own output.

**`solution_proposed`** is set by the agent when it creates a `ProposedAction` with `action_type=SOLUTION`. It is the agent's own act — no detection required.

**`mitigation_accepted` and `solution_accepted`** are set when the user's message indicates execution of a proposed action.

*Is compliance:*

- User provides new output from after the action (logs, metrics, command output with post-action timestamps)
- User uses past tense: "I ran…", "I applied…", "I restarted…"
- User asks a follow-up about the result: "It reduced errors by 80% — what next?"

*Is NOT compliance:*

- "Thanks, I'll try it" — intent, not execution
- User asks clarifying questions about the command
- User goes silent

**`mitigation_verified`** is set when the user confirms stabilization. Subjective confirmation is sufficient: "it's better", "errors dropped", "seems stable". Specific metric values are not required.

**`solution_verified`** requires the explicit User-Agent Handshake. The agent proposes "Yes, mark as resolved" as a COOPERATIVE suggestion and sets `solution_verified` only when the user selects it. Cannot be set from ambiguous confirmation.

---

### Hypothesis State

Hypothesis state is the analytical bridge between `symptom_verified` and `root_cause_identified`. It is not a boolean — it is a lifecycle with confidence scoring.

**Formation:** after `symptom_verified = True`. Each hypothesis must state: suspected cause, proposed mechanism, what evidence would confirm it, and what would refute it.

**Lifecycle:**

| State | Meaning | Requirement |
| ----- | ------- | ----------- |
| `ACTIVE` | Under investigation | — |
| `VALIDATED` | Confidence ≥ 70% with causal evidence | Enables `root_cause_identified` |
| `REFUTED` | Evidence directly disproves it | Requires `refutation_reason` citing specific evidence |
| `RETIRED` | Abandoned without disproof | No reason required |

Use `REFUTED` only when disproof exists. When there is no evidence of disproof, use `RETIRED`.

---

## Forward-Looking Message Structure

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

## Stage Model

```text
INQUIRY (phase)
  └─ purpose: define the problem, assess urgency, surface KB solutions
  └─ gate → DIAGNOSIS: problem statement confirmed by user

INVESTIGATING (phase)
  ├─ DIAGNOSIS (stage)
  │    ├─ Zone 1: Symptom Verification  [symptom_verified = False]
  │    ├─ Zone 2: Root Cause Analysis   [symptom_verified = True, root_cause_identified = False]
  │    └─ Zone 3: Solution Proposal     [root_cause_identified = True, solution_proposed = False]
  │    └─ gate → MITIGATION: mitigation_accepted
  │    └─ gate → TREATMENT:  solution_accepted
  │
  ├─ MITIGATION (detour — optional)
  │    └─ gate → DIAGNOSIS: mitigation_verified (flags reset, RCA resumes)
  │
  └─ TREATMENT (stage)
       └─ gate → TERMINAL: solution_verified

TERMINAL (state — not a phase)
  RESOLVED or CLOSED — investigation is immutable; Q&A only
```

**Progress metrics by stage:**

| Stage | Diagnostic variable | Action variable |
| ----- | ------------------- | --------------- |
| DIAGNOSIS Zone 1 | `symptom_verified` | — |
| DIAGNOSIS Zone 2 | `root_cause_identified` (via hypothesis) | — |
| DIAGNOSIS Zone 3 | — | `solution_proposed` |
| MITIGATION | — | `mitigation_accepted` → `mitigation_verified` |
| TREATMENT | — | `solution_accepted` → `solution_verified` |

---

## INQUIRY Phase

### Purpose

Establish a shared understanding of the problem before investigation begins. The metrics framework is not yet active — no milestones are tracked during INQUIRY.

### Agent Duties

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

### Gate Conditions

| To | Condition |
| -- | --------- |
| INVESTIGATING / DIAGNOSIS | `problem_statement_confirmed = True` (user explicitly confirms) |
| RESOLVED (fast-track) | KB match found + user confirms it worked |
| CLOSED | User declines investigation |

### Anti-Patterns

- Requesting diagnostic evidence before the problem statement is confirmed
- Treating data uploads or continued engagement as confirmation ("yes" must be explicit)
- Proposing a problem statement when the user asks a general knowledge question

---

## DIAGNOSIS Stage

### Zone 1: Symptom Verification

**Target variable:** `symptom_verified`

**Agent duties:**

1. Apply the three-step diagnostic pattern: search for symptom signatures using `search_file` → evaluate against conclusive criteria → advance with citation or ask specifically.
2. When asking for data, apply the specificity standard: what log or metric, from what source, for what timeframe.
3. Do not form hypotheses until `symptom_verified = True`.
4. Scope, timeline, and recent changes are secondary — extract them from symptom evidence when present, but do not delay Zone 1 progress waiting for them.

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

### Zone 2: Root Cause Analysis

**Target variable:** `root_cause_identified` (via hypothesis state)

**Agent duties:**

1. **Search KB first** — call `kb_qa` for the confirmed symptom before generating hypotheses. If a runbook matches, follow its diagnostic steps as the default approach.
2. Apply the hypothesis-evidence ordering: form hypothesis → apply three-step pattern for `root_cause_identified` → validate or refute.
3. **Single-shot vs multi-hypothesis:** if the root cause is obvious from existing evidence (clear error chain, strong timing correlation), form one hypothesis and validate in the same turn. If ambiguous, form 2–4 hypotheses across different categories and request targeted evidence per hypothesis.
4. Each evidence request must be tied to a specific hypothesis and follow the specificity standard.
5. Refute with reason; retire without. Never use `REFUTED` without evidence of disproof.

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

---

### Zone 3: Solution Proposal

**Target variable:** `solution_proposed`

**Agent duties:**

1. State the root cause in one sentence before proposing a fix.
2. Propose a direct, executable action — instruction form, not a question ("Run: [command]", not "Would you like to try X?").
3. State impact: reversible or not, blast radius (single pod, cluster, database, shared service).
4. Do not request further evidence after `root_cause_identified` is set. Hold until the user executes or raises an objection.

**Gate out of Zone 3:**

- `solution_accepted` → TREATMENT
- `mitigation_accepted` → MITIGATION

**Anti-patterns:**

- Proposing a fix as a question
- Proposing multiple competing fixes simultaneously
- Requesting more evidence after `root_cause_identified`

---

### When DIAGNOSIS Stalls

If two full hypothesis cycles complete with no convergence:

1. State what is established: verified symptom, evidence analyzed, hypotheses tested and outcomes.
2. State the boundary: "I cannot determine the cause without [specific data or access]."
3. Present options: data that would resolve the ambiguity, alternative diagnostic angles, escalation, or pausing with state preserved.

A well-documented partial investigation that narrows the problem is a valid outcome.

---

## MITIGATION Stage

Apply a temporary fix to reduce immediate impact while root cause analysis is blocked or pending. Goal is stabilization, not resolution.

**Agent Duties:**

1. Provide numbered steps framed as user instructions, not agent actions.
2. State rollback: what to do if the fix makes things worse. State the fix is temporary.
3. Request `mitigation_evidence` — post-fix metrics, error rates, user observation.
4. Accept subjective confirmation for `mitigation_verified`.
5. Iterate if ineffective — propose a modified approach, stay in MITIGATION.
6. Do not form hypotheses or classify `causal_evidence` here.

**Gate Conditions:**

| To | Condition |
| -- | --------- |
| DIAGNOSIS (Zone 2 or 3) | `mitigation_verified = True` — flags reset, RCA resumes |
| CLOSED | `rca_infeasible = True` + user handshake |

**Anti-Patterns:**

- Pursuing root cause analysis during MITIGATION
- Giving up after one failed attempt
- Not stating the fix is temporary or not providing rollback

---

## TREATMENT Stage

Verify that the applied fix resolves the problem. If it does not, diagnose the failure and revise — without returning to DIAGNOSIS.

**Agent Duties — Primary path (fix verification):**

1. Search post-fix data for resolution signals — error rate drop, latency return to baseline, service health restored.
2. Accept subjective confirmation for initial verification.
3. Connect the dots explicitly: "The [evidence] confirms [hypothesis] was correct and the fix resolved it."

**Agent Duties — Failure path (fix did not work):**

1. Determine failure type: implementation error (wrong command, missing step) → correct and re-propose without new evidence. Theory wrong → the original hypothesis was incorrect; new evidence is required before a new proposal.
2. State what the failure eliminates and what is now unclear. Request NEW evidence with full specificity before revising a theory.
3. Refute the disproven hypothesis with `refutation_reason` citing the failed fix.

**Agent Duties — Completion (two-step handshake):**

1. When evidence confirms resolution, set `proposed_transition` to RESOLVED.
2. Offer exactly two COOPERATIVE suggestions: "Yes, mark as resolved" / "Not yet, I want to investigate further."
3. After resolution: brief summary — what happened, what fixed it, preventive recommendations. Remind the user to revert any mitigation workaround still in place.

**Gate Conditions:**

| To | Condition |
| -- | --------- |
| TERMINAL (RESOLVED) | `solution_verified = True` via handshake |
| TERMINAL (CLOSED) | User escalates or abandons via handshake |

**Anti-Patterns:**

- Proposing a different solution after failure without collecting new evidence
- Returning to DIAGNOSIS after a failed fix (stay in TREATMENT)
- Setting `solution_verified` without the handshake

---

## TERMINAL State

No investigation. Answer questions using existing case data (evidence, hypotheses, solutions, action history, summary report). Do not accept new evidence, advance milestones, or propose transitions. If the user describes ongoing issues, direct them to open a new case.

---

## Cross-Stage Constraints

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
| DIAGNOSIS Zone 3 | 1 COOPERATIVE command_copy (the fix) |
| MITIGATION | 1 COOPERATIVE command_copy + 1 EVIDENCE (post-fix) |
| TREATMENT (primary) | 1 EVIDENCE (post-fix) |
| TREATMENT (completion) | 2 COOPERATIVE (yes resolved / not yet) |
| TERMINAL | 1–2 COOPERATIVE (report, runbook) |

---

## Prompt Injection Map

Each section of this playbook maps to a concrete injection point in `templates.py`. The agent duties, anti-patterns, and gate conditions in each stage section above are the spec; the blocks below are where they live in code.

Stage instructions are injected as `{adaptive_instructions}` in `INVESTIGATION_BASE`:

| Stage | Instruction block | Location in `templates.py` |
| ----- | ----------------- | --------------------------- |
| INQUIRY | `INQUIRY_TEMPLATE` (YOUR TASK section) | Lines 225–362 |
| DIAGNOSIS | `DIAGNOSIS_INSTRUCTIONS` + `_get_diagnosis_focus_emphasis(progress)` prepended | Lines 742–924 (instructions), 1283–1316 (emphasis function) |
| MITIGATION | `MITIGATION_INSTRUCTIONS` | Lines 926–978 |
| TREATMENT | `TREATMENT_INSTRUCTIONS` | Lines 980–1108 |

`_get_diagnosis_focus_emphasis()` maps the three DIAGNOSIS zones to a contextual status signal prepended to `DIAGNOSIS_INSTRUCTIONS`:

| Zone | Condition | Emphasis |
| ---- | --------- | -------- |
| Zone 1 | `symptom_verified = False` | "Symptom verification pending — search for evidence the problem exists" |
| Zone 2 | `symptom_verified = True`, `root_cause_identified = False` | "Root cause analysis — form hypotheses, search for causal evidence" |
| Zone 3 | `root_cause_identified = True`, `solution_proposed = False` | "Solution needed — propose a concrete, executable fix" |
| Pending | `solution_proposed = True` | (pending action context in template handles framing) |

**`knowledge_query` mode bypass:** When `processing_mode == "knowledge_query"`, stage dispatch is skipped entirely. `KNOWLEDGE_QUERY_INSTRUCTIONS` replaces `adaptive_instructions` and `_EVIDENCE_GROUNDING_BLOCK` is set to `""`. This handles pure KB questions without investigation context.
