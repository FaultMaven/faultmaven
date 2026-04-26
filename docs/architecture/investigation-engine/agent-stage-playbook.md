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

## Stage Model

```
INQUIRY (phase)
  └─ purpose: define the problem, assess urgency, surface KB solutions
  └─ gate → DIAGNOSIS: problem statement confirmed by user

INVESTIGATING (phase)
  ├─ DIAGNOSIS (stage)
  │    ├─ Zone 1: Symptom Verification   [symptom_verified not set]
  │    ├─ Zone 2: Root Cause Analysis    [symptom_verified set, root_cause_identified not set]
  │    └─ Zone 3: Solution Proposal      [root_cause_identified set, solution_proposed not set]
  │
  │    └─ gate → MITIGATION: user executes proposed temp fix (mitigation_accepted)
  │    └─ gate → TREATMENT:  user executes proposed solution  (solution_accepted)
  │
  ├─ MITIGATION (detour — optional)
  │    └─ gate → DIAGNOSIS: mitigation verified (mitigation_verified, flags reset)
  │
  └─ TREATMENT (stage)
       └─ gate → TERMINAL: user confirms fix worked (solution_verified + handshake)

TERMINAL (state — not a phase)
  RESOLVED or CLOSED — investigation is immutable; Q&A only
```

**Evidence types by stage:**

| Stage | Evidence types | Milestone target |
| ----- | -------------- | ---------------- |
| DIAGNOSIS Zone 1 | `symptom_evidence`, `contextual_evidence` | `symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified` |
| DIAGNOSIS Zone 2 | `causal_evidence` (requires hypothesis first), `contextual_evidence` | `root_cause_identified` |
| DIAGNOSIS Zone 3 | None (agent proposes from existing evidence) | `solution_proposed` |
| MITIGATION | `mitigation_evidence` | `mitigation_accepted`, `mitigation_verified` |
| TREATMENT | `solution_evidence` | `solution_accepted`, `solution_verified` |
| TREATMENT (failure path) | `causal_evidence`, `symptom_evidence` | Re-enters Zone 2 logic within TREATMENT |

---

## Design Principles

Every stage instruction must pass three tests before being written:

1. **What the agent does** — stated as a concrete duty, not an aspiration
2. **What triggers it** — tied to milestone state, evidence availability, or user action
3. **What it produces** — evidence classified, milestone advanced, or gate triggered

Aspirational instructions ("be thorough", "investigate carefully") are not stage instructions. If a duty cannot be tied to a milestone or gate, it belongs in behavioral rules or prompt quality guidance.

---

## INQUIRY Phase

### Purpose

Establish a shared understanding of the problem before any investigation begins. The agent is not yet diagnosing — it is clarifying, assessing, and confirming.

### Agent Duties

**When no problem signal is present:**

1. Answer the user's question directly (Rule 1). Do not propose a problem statement or initiate investigation.

**When a problem signal is detected** (error, slowness, outage, anomaly):

1. **Extract the problem signal** — identify: what is failing, scope, temporal state (ongoing vs historical), and business impact.
2. **Assess urgency** — classify as CRITICAL / HIGH / MEDIUM / LOW based on business impact signals:
   - CRITICAL: revenue loss, production down, data loss, customers affected
   - HIGH: checkout failing, payments broken, 30%+ requests failing, SLA violation
   - MEDIUM: intermittent, partial failure, degraded experience
   - LOW: historical, post-mortem, optimization, cosmetic
3. **Search the KB first** (`kb_qa`) — before proposing any investigation, check whether a past case or runbook resolves the issue. If a match exists:
   - Surface it: "This looks similar to [past case]. The solution was [X]. Would you like to try it?"
   - Await user's attempt and confirmation — do not transition to INVESTIGATING unless the KB solution fails.
4. **Propose a problem statement** — one sentence, from the user's perspective, capturing: symptom + scope + temporal state. Set `proposed_problem_statement` in state_updates.
5. **Surface early path hint for CRITICAL/HIGH + ONGOING** — after proposing the problem statement, add: "This is actively affecting [scope]. Should I focus on a quick mitigation first while we investigate the root cause?"
6. **Await confirmation** — present the problem statement with a yes/no choice. Do not transition until the user explicitly confirms. Do not ask for evidence or propose next steps on the same turn as problem statement presentation.

### Evidence in INQUIRY

The agent may classify data uploaded during INQUIRY as evidence (`evidence_to_add`) if the user submits files before investigation begins. Categories apply normally:
- `symptom_evidence`: data showing the problem
- `contextual_evidence`: background data not related to the problem

These records are created but `advances_milestones` is empty — milestone tracking is not active during INQUIRY. At transition, retroactive attribution runs for all evidence based on category.

### User Instruction Patterns

| Situation | Suggestion type | Example |
| --------- | --------------- | ------- |
| Need to understand the problem | FREE_SPEECH | "What behavior are you seeing? When did it start?" |
| KB fast-track found | COOPERATIVE (query_submit) | "Yes, try that solution" |
| Confirming problem statement | COOPERATIVE (query_submit) | "Yes, let's investigate" / "Not yet, I have more context" |

**Do not** request diagnostic evidence (logs, metrics) during INQUIRY before the problem statement is confirmed. The investigation has not started yet.

### Gate Conditions

| To | Condition |
| -- | --------- |
| INVESTIGATING / DIAGNOSIS | `problem_statement_confirmed = True` (user explicitly confirms) |
| RESOLVED (fast-track) | KB match found + user confirms solution worked |
| CLOSED | User declines investigation or closes the inquiry |

### Anti-Patterns

- Proposing a problem statement when the user asks a general how-to question
- Starting investigation before the user confirms the problem statement
- Requesting evidence (logs, metrics) before problem statement is confirmed
- Treating data uploads or continued engagement as confirmation ("yes" must be explicit)
- Re-proposing investigation after the user has declined

---

## DIAGNOSIS Stage

### Purpose

Understand the problem deeply enough to propose a concrete fix. DIAGNOSIS has three internal zones based on progress milestone state, each with distinct duties. The agent may complete multiple zones in one turn if evidence supports it.

### Zone Determination

The active zone is computed from progress milestones — not from a stored field:

| Zone | Condition |
| ---- | --------- |
| Zone 1: Symptom Verification | `symptom_verified = False` |
| Zone 2: Root Cause Analysis | `symptom_verified = True`, `root_cause_identified = False` |
| Zone 3: Solution Proposal | `root_cause_identified = True`, `solution_proposed = False` |

When `solution_proposed = True`, the agent is awaiting user compliance with the proposed action. No new duties apply — the agent holds until the user executes or asks a question.

---

### Zone 1: Symptom Verification

**Purpose:** Confirm the problem is real, establish its scope, timeline, and any correlated changes.

**Target milestones:** `symptom_verified`, `scope_assessed`, `timeline_established`, `changes_identified`

**Agent duties:**

1. **Request symptom evidence** — ask for data that shows the problem exists: error logs, metrics with anomalies, alerts. Frame the request around what the evidence will confirm ("to verify the symptom, we need to see the error rate during the affected window").
2. **Classify uploaded data as `symptom_evidence`** when it confirms the problem (errors, performance degradation, service failures).
3. **Complete scope, timeline, and changes from the same evidence** — these four milestones are co-located and often completable from one evidence set. Advance all that the evidence supports in a single turn.
4. **Do not form hypotheses yet** — hypothesis formation requires symptom confirmation. Creating a hypothesis in Zone 1 violates the milestone ordering.

**User instruction patterns:**

| Situation | Suggestion type | Example |
| --------- | --------------- | ------- |
| Need error logs | EVIDENCE | "Application error logs from the affected timeframe" |
| Need metrics | EVIDENCE | "Error rate / latency metrics for the past 2 hours" |
| Need to establish timeline | FREE_SPEECH | "When did this start? Any changes deployed recently?" |
| Have a command to collect data | COOPERATIVE (command_copy) | `kubectl logs <pod> --since=2h --tail=200` |

**Gate out of Zone 1:** `symptom_verified = True` (set by the agent in structured output when evidence confirms the symptom).

**Anti-patterns:**

- Creating hypotheses before `symptom_verified`
- Classifying evidence as `causal_evidence` before a hypothesis exists
- Asking for RCA-level data (deployment diffs, config changes) before symptoms are confirmed

---

### Zone 2: Root Cause Analysis

**Purpose:** Identify why the problem is happening. Produce one or more hypotheses, link them to evidence, and reach `root_cause_identified`.

**Target milestone:** `root_cause_identified`

**Agent duties:**

1. **Search the KB first** — before inventing diagnostic procedures, call `kb_qa` for the symptom. If a runbook matches, follow its steps as the default approach. If case evidence contradicts the runbook's assumptions, note the conflict and adapt.
2. **Hypothesis-Evidence Ordering (Non-Negotiable)** — the sequence is fixed:
   - CREATE hypothesis (`hypotheses_to_add`)
   - CLASSIFY evidence as `causal_evidence` (`evidence_to_add`)
   - LINK evidence to hypothesis (`hypothesis_evidence_links`)
   - SET `root_cause_identified = True` if confidence ≥ 70%
   - Never classify evidence as `causal_evidence` without a hypothesis in the same turn or already existing.
3. **Single-shot vs multi-hypothesis** — use judgment:
   - Single hypothesis: root cause is obvious from evidence (clear error, strong timing correlation, mechanism understood). Create one hypothesis, link evidence, set `VALIDATED`, propose solution — all in one turn.
   - Multi-hypothesis: root cause unclear. Create 2–4 hypotheses across different categories, request targeted evidence to distinguish between them.
4. **Request evidence targeted to hypothesis testing** — each evidence request must be tied to a specific hypothesis: "to confirm whether [hypothesis A] or [hypothesis B] is the cause, we need [specific log/metric]."
5. **Refute vs Retire** — when a hypothesis is eliminated:
   - `REFUTED` = evidence directly disproves it. Requires `refutation_reason` citing the specific evidence.
   - `RETIRED` = abandoning without disproof (superseded, lower priority, blocked on data). No reason required.
   - Do not use REFUTED when there is no evidence of disproof — that is RETIRED.
6. **Handle hypothesis deadlock** — if all active hypotheses are refuted and no evidence distinguishes between new theories, generate 2–3 new hypotheses from a different category than those already tested. After two complete cycles with no convergence, produce a structured handoff (see STALL below).

**User instruction patterns:**

| Situation | Suggestion type | Example |
| --------- | --------------- | ------- |
| Evidence needed to test hypothesis | EVIDENCE | "Deployment logs from 13:50–14:10 to check for config changes" |
| Diagnostic command to run | COOPERATIVE (command_copy) | `kubectl describe pod <pod-name>` |
| Hypothesis engagement | COOPERATIVE (query_submit) | "Check if there were recent database migrations" |

**Gate out of Zone 2:** `root_cause_identified = True` (agent sets in structured output when a hypothesis is validated at ≥ 70% confidence).

**Anti-patterns:**

- Classifying evidence as `causal_evidence` without a corresponding hypothesis
- Using `REFUTED` without `refutation_reason`
- Using `REFUTED` when there is no evidence of disproof (use `RETIRED`)
- Inventing diagnostic procedures without checking the KB first
- Requesting evidence without tying it to a specific hypothesis being tested

---

### Zone 3: Solution Proposal

**Purpose:** Propose a concrete, executable fix based on the identified root cause.

**Target milestone:** `solution_proposed` (set automatically when a `ProposedAction` with `action_type=SOLUTION` is created).

**Agent duties:**

1. **State the root cause** — one clear sentence. The user must understand what is broken before being asked to execute a fix.
2. **Propose a direct action** — frame the fix as an instruction, not a question: "The fix is [X]. Run: [command]". Do not ask "would you like to try X?"
3. **Classify the action's impact** — state whether it is reversible, and the blast radius (single pod, cluster, shared service, database). This is required for state-modifying actions (restart, delete, rollback, scale, reconfigure).
4. **Await compliance** — do not ask for more evidence after proposing a solution. Hold until the user executes or raises an objection.
5. **If MITIGATION_FIRST path** — when `path_selection.path = MITIGATION_FIRST` and no mitigation has been applied yet, propose a temp fix instead of a permanent solution. Frame it as temporary: "To stabilize the system now, run [temp fix]. We'll find the root cause once things are stable."

**User instruction patterns:**

| Situation | Suggestion type | Example |
| --------- | --------------- | ------- |
| Proposing the fix command | COOPERATIVE (command_copy) | `kubectl rollout undo deployment/api` |
| Clarifying path | COOPERATIVE (query_submit) | "Yes, apply the temp fix first" |

**Gate out of Zone 3:**
- `solution_accepted` (user executes proposed solution → TREATMENT)
- `mitigation_accepted` (user executes proposed temp fix → MITIGATION)

**Anti-patterns:**

- Proposing a fix as a question ("Would you like to try X?")
- Requesting more evidence after `root_cause_identified` is set
- Proposing multiple competing fixes simultaneously ("here are three options")
- Failing to state impact for state-modifying actions

---

### When DIAGNOSIS Stalls

Not every diagnosis reaches root cause. If all hypothesis categories have been tested with no convergence after two cycles, produce a structured handoff:

1. **Consolidate** — state what is established: verified symptom, scope, timeline, evidence analyzed, hypotheses tested and their outcomes.
2. **State the boundary** — "I cannot determine the cause without [specific data/access]. The most likely causes based on available evidence are [X and Y]."
3. **Present options** — specific additional data that would resolve the ambiguity; alternative diagnostic angles not yet tried; escalation to a specialist; or pausing the investigation with state preserved.

A well-documented partial investigation that narrows the problem is a valid outcome. Do not frame it as failure.

---

## MITIGATION Stage (Detour)

### Purpose

Apply a temporary fix to reduce immediate impact while root cause analysis continues. This stage is a controlled detour — its goal is stabilization, not resolution.

### Entry

`mitigation_accepted` gate milestone — set by the agent when the user's message indicates execution of the proposed temp fix (submitted results, used past tense, provided post-action evidence).

### Agent Duties

1. **Guide implementation** — provide numbered steps for the user to execute. Frame as instructions ("run this", "apply that"), not as actions the agent performs.
2. **State rollback** — for every temp fix, state what to do if it makes things worse. State that the fix is temporary and must be reverted after the permanent fix.
3. **Request mitigation evidence** — ask for post-fix metrics, logs, or user observation that confirm the situation is stabilizing.
4. **Accept subjective confirmation** — "it stabilized" or "errors dropped" is sufficient to set `mitigation_verified`. Do not require specific metric values.
5. **Iterate if ineffective** — if the first mitigation attempt does not stabilize the situation, propose a modified or alternative temp fix. Stay in MITIGATION until the user verifies stabilization. Do not give up after one attempt.
6. **Do not pursue root cause** — MITIGATION is about stopping the bleeding. RCA resumes in DIAGNOSIS after `mitigation_verified`.

### Evidence

- Request: `mitigation_evidence` — post-mitigation metrics, error rates, user confirmation of improvement.
- Do not classify evidence as `causal_evidence` or create new hypotheses during MITIGATION.

### User Instruction Patterns

| Situation | Suggestion type | Example |
| --------- | --------------- | ------- |
| Temp fix command | COOPERATIVE (command_copy) | `kubectl scale deployment/api --replicas=10` |
| Post-fix evidence | EVIDENCE | "Metrics after applying the restart — error rate, response times" |

### Gate Conditions

| To | Condition |
| -- | --------- |
| DIAGNOSIS (Zone 2 or 3) | `mitigation_verified = True` — agent sets when user confirms stabilization |
| CLOSED (mitigation_sufficient) | `rca_infeasible = True` + user confirms via handshake |

After `mitigation_verified`, both `mitigation_accepted` and `mitigation_verified` are reset, allowing a future MITIGATION detour if needed. History is preserved in `action_attempts`.

### Anti-Patterns

- Pursuing root cause analysis (forming new hypotheses, requesting causal evidence) during MITIGATION
- Giving up and proposing CLOSED after one failed mitigation attempt
- Not communicating that the fix is temporary
- Not providing a rollback plan

---

## TREATMENT Stage

### Purpose

Verify that the applied fix resolves the problem. If it does, close the investigation. If it does not, diagnose the failure and revise the approach — without returning to DIAGNOSIS.

### Entry

`solution_accepted` gate milestone — set by the agent when the user's message indicates execution of the proposed solution.

### Agent Duties

**Primary path (fix verification):**

1. **Verify the submitted evidence** — analyze the results the user provides. Does the evidence confirm the fix worked?
2. **Accept subjective confirmation** — "it's working now" or "looks good" is sufficient for verification. Do not require specific metrics unless the initial evidence is ambiguous.
3. **Connect the dots** — when the fix works, explicitly state: "The [evidence] confirms the [root cause hypothesis] was correct and the fix resolved it."

**Failure path (fix did not work):**

4. **Failure analysis first** — determine whether the failure is an implementation error (wrong command, missing step) or evidence that the root cause theory was wrong:
   - Implementation error → correct the steps and re-propose without requiring new evidence.
   - Theory wrong → the original evidence produced a wrong hypothesis. New evidence is required before a new solution can be proposed.
5. **Request NEW evidence before revising** — do not re-propose a different fix using only the original evidence. State: "The fix didn't work, which tells us [what's eliminated]. To determine whether the cause is [A] or [B], we need [specific data]."
6. **Update hypotheses** — refute the disproven hypothesis with `refutation_reason` citing the failed fix as evidence. Form a new hypothesis if a different cause is suspected.
7. **Propose revised solution** — after new evidence, follow the same hypothesis-evidence ordering from Zone 2 and propose a concrete fix.

**Completion (two-step handshake):**

8. **Detect solution success** — when evidence confirms the fix worked, propose the transition: set `proposed_transition` to RESOLVED in the response.
9. **Offer exactly two COOPERATIVE suggestions:**
   - Positive: "Yes, mark as resolved"
   - Mild negative: "Not yet, I want to investigate further"
   - Do NOT suggest evidence collection as an alternative — if the user declines, they want to continue, not collect data.
10. **After resolution** — provide a brief summary: what happened, what fixed it, and any preventive recommendations. If a mitigation workaround was applied earlier, remind the user to revert it.

### Evidence

| Evidence type | When to classify |
| ------------- | ---------------- |
| `solution_evidence` | Post-fix data showing the fix worked or failed |
| `symptom_evidence` | New symptoms that emerge after a failed fix |
| `causal_evidence` | Data revealing the actual root cause after a theory is disproven — requires hypothesis first |

### User Instruction Patterns

| Situation | Suggestion type | Example |
| --------- | --------------- | ------- |
| Post-fix evidence | EVIDENCE | "Error rate / response times after the deployment" |
| Confirming resolution | COOPERATIVE (query_submit) | "Yes, mark as resolved" |
| New diagnostic command on failure | COOPERATIVE (command_copy) | `kubectl describe pod <pod>` |

### Gate Conditions

| To | Condition |
| -- | --------- |
| TERMINAL (RESOLVED) | `solution_verified = True` — set via User-Agent Handshake (user confirms) |
| TERMINAL (CLOSED) | User escalates or abandons — User-Agent Handshake with closure reason |

### Anti-Patterns

- Proposing a different solution after failure without collecting new evidence
- Suggesting evidence collection (logs, monitoring) as alternatives to the resolution confirmation COOPERATIVE suggestions
- Returning to DIAGNOSIS after a failed fix (stay in TREATMENT)
- Allowing `solution_verified` to be set without user confirmation (it requires the handshake)

---

## TERMINAL State

### Purpose

Allow the user to ask questions about a completed investigation. No new investigation occurs.

### Agent Duties

1. Answer questions using existing case data (evidence, hypotheses, solutions, action history, summary report).
2. Explain what happened, clarify evidence, interpret the timeline, extract lessons learned.
3. Do NOT accept new evidence, update milestones, or propose transitions.
4. If the user describes ongoing issues, direct them to open a new case.

TERMINAL Q&A uses `TERMINAL_TEMPLATE` with `TerminalResponse` schema. The milestone engine short-circuits before intent detection when `case.is_terminal`.

---

## Cross-Stage Constraints

### Compliance Detection

Applies in DIAGNOSIS (Zone 3) and TREATMENT. The agent detects that the user has executed a proposed action from the user's message:

**Is compliance:**
- User provides NEW output from AFTER the action (logs, metrics, command output with post-action timestamps)
- User uses past tense: "I ran…", "I applied…", "I deployed…"
- User asks a follow-up specific to the result: "It reduced errors by 80% — what next?"

**Is NOT compliance:**
- "Thanks, I'll try it" (intent, not execution)
- User goes silent (absence ≠ execution)
- User asks clarifying questions about the command itself

### Suggestion Types by Zone

The right suggestion type depends on what the agent wants the user to do:

| Agent intent | Type | When to use |
| ------------ | ---- | ----------- |
| User engages with analysis, steers investigation | COOPERATIVE (query_submit) | Confirmation, path selection, KB action |
| User runs a command externally | COOPERATIVE (command_copy) | Shell commands, kubectl, SQL queries |
| Agent needs specific data from user's environment | EVIDENCE | Logs, metrics, config files, screenshots |
| Agent needs user's knowledge or judgment | FREE_SPEECH | Timeline, symptoms, recent changes, scope |

One primary ask per turn. When multiple pieces of data would help, pick the single most decisive one and explain why. Stack only when items are genuinely parallel (e.g., two complementary log files that always come together).

### Follow-Up Suggestion Count by Stage

| Stage | Expected suggestions |
| ----- | ------------------- |
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

| Zone | Condition | Emphasis text |
| ---- | --------- | ------------- |
| Zone 1 | `symptom_verified = False` | "Symptom verification pending — look for evidence the problem exists" |
| Zone 2 | `symptom_verified = True`, `root_cause_identified = False` | "Root cause analysis — focus on hypotheses, causal evidence" |
| Zone 3 | `root_cause_identified = True`, `solution_proposed = False` | "Solution needed — propose a concrete, executable fix" |
| Pending | `solution_proposed = True` | (pending action context in template handles framing) |

**`knowledge_query` mode bypass:** When `processing_mode == "knowledge_query"`, stage dispatch is skipped entirely. `KNOWLEDGE_QUERY_INSTRUCTIONS` is used as `adaptive_instructions` and `_EVIDENCE_GROUNDING_BLOCK` is set to `""`. This mode handles pure KB questions without investigation context.
