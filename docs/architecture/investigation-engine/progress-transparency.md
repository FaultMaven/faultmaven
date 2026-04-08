# Progress Transparency

This document specifies the progress transparency feature for the FaultMaven investigation engine. When an investigation stalls, the agent surfaces what is needed to reach the next milestone — not as a generic reminder, but as case-specific guidance grounded in the current evidence and context.

**Related Documents**:

- [Agent Behavioral Rules](./agent-behavioral-rules.md) — The 7 rules governing agent behavior
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) — Stage transitions and milestones
- [Prompt Templates](./prompt-templates.md) — Where progress transparency is injected
- [Error Handling and Recovery](./error-handling-and-recovery.md) — System-level safety nets

---

## Design Philosophy

The agent follows behavioral rules at every turn. By this standard alone, the agent should not be the reason for a stalled investigation. However, investigations can stall for legitimate reasons — insufficient data, access constraints, non-deterministic problems, or simply the nature of the issue.

Progress transparency makes the investigation state visible to the user when progress stalls. It does not detect or judge user behavior. It does not try to force progress. It influences the user by illuminating the situation — what milestone is pending, what evidence would advance it, and (from the agent) what specifically that means for this case.

**Core principles:**

1. **The user controls the conversation.** The agent serves the user's query. Progress transparency informs but never pressures.
2. **The agent always knows what's needed.** Milestone dependencies are deterministic. The question is when to surface them, not whether they're known.
3. **Influence through visibility, not steering.** Like a light: it draws attention and lets you see what's there. It doesn't push you anywhere.
4. **Case-specific over generic.** "We need evidence of network connectivity issues from 14:00-16:00 UTC" is useful. "We need symptom evidence" is not.

---

## Two Modes

### Silent Mode (default)

Progress is tracked internally. The agent works through milestones naturally following the stage-specific templates. Nothing extra is surfaced to the user.

### Transparent Mode (activated on stall)

When N turns pass without a milestone completing in the current stage, transparent mode activates. The agent is informed via prompt injection and provides case-specific guidance on what evidence would advance the pending milestone.

### Transitions

| Transition           | Trigger                                                              |
|----------------------|----------------------------------------------------------------------|
| Silent → Transparent | N data-bearing turns without a milestone completing in current stage |
| Transparent → Silent | Any milestone is completed                                           |

**What counts toward the counter:** Turns where the user submitted data (evidence attached) OR the agent executed diagnostic tools (search_file, deep_analysis, etc.) against existing evidence. These are **investigative turns** — turns where diagnostic work was attempted. Conversational turns (questions, off-topic chat, acknowledgments without data or tool calls) do not increment the counter.

This means the counter tracks "N turns of investigative work without a milestone advancing." Whether the user provided new data or the agent dug deeper into existing data, diagnostic effort was spent without result.

**The exact threshold is not critical.** A higher number means the light turns on later; a lower number means earlier. Neither derails the investigation — transparent mode is informational, not corrective. The default threshold of 5 investigative turns is a reasonable starting point.

**One-turn delay:** Progress transparency flows through `system_feedback`, which is stored on the current turn and delivered to the LLM on the next turn. This means the transparency guidance appears in the agent's response **one turn after the threshold is reached**. If the threshold is met on Turn 9, the user sees the guidance in the Turn 10 response. This is inherent to the `system_feedback` mechanism and applies to all system-injected instructions (stagnation, validation errors, etc.).

Transparent mode is **stage-scoped**. A stage transition resets the counter. A milestone completion in the current stage turns the light off.

**Scope:** Progress transparency applies only during the INVESTIGATING phase. INQUIRY has no milestones to track. TERMINAL is read-only.

---

## What the Agent Sees (Prompt Injection)

When transparent mode activates, the following is injected into the prompt via `system_feedback`:

```text
PROGRESS TRANSPARENCY: The investigation has been in {stage} for {N} turns 
without reaching the next milestone.
Pending milestone: {milestone_name} — {milestone_description}.
In your response, provide case-specific guidance on what evidence would 
advance this milestone. Be concrete: name specific files, services, time 
ranges, or commands based on what you know about this case.
```

This is **new information** the LLM doesn't have in the base prompt — the turn count, the specific pending milestone, and the instruction to be concrete. It is not a restated behavioral rule.

---

## What the User Sees

The agent's response, which includes case-specific guidance informed by the injection. There is no separate system message — the agent's response IS the transparency mechanism.

The case header in the UI already displays milestone status. Transparent mode does not duplicate this. It adds contextual, case-aware guidance in the conversation flow where the user is reading.

---

## Milestone Dependency Map

Each milestone has known evidence dependencies. This map is static — it defines what category of evidence advances each milestone. The LLM provides case-specific detail on top of this.

| Pending Milestone | Stage | What Would Advance It |
|---|---|---|
| symptom_verified | DIAGNOSIS | Evidence showing error patterns, symptoms, or anomalies (logs, error reports, metrics) |
| scope_assessed | DIAGNOSIS | Evidence showing which systems or services are affected |
| timeline_established | DIAGNOSIS | Evidence with timestamps showing when the issue started or changed |
| changes_identified | DIAGNOSIS | Evidence of recent changes (deployment logs, config diffs, code changes) |
| root_cause_identified | DIAGNOSIS | Validated hypothesis with supporting causal evidence |
| solution_proposed | DIAGNOSIS | Sufficient confidence in root cause to propose a concrete fix |
| mitigation_verified | MITIGATION | User confirmation that temporary fix stabilized the situation |
| solution_verified | TREATMENT | User confirmation that permanent fix resolved the issue |

---

## Agent State Repair (Exception Handling)

While in transparent mode, the system also checks for specific agent-internal failure patterns that require structural intervention beyond prompt injection. These are conditions where the agent's internal state is broken, not just where progress has stalled.

### Patterns

| Pattern | Stages | Detection | Structural Action |
|---|---|---|---|
| **HYPOTHESIS_DEADLOCK** | DIAGNOSIS, TREATMENT | 3+ hypotheses, all INCONCLUSIVE | Retire all inconclusive hypotheses — gives LLM a clean slate |
| **HYPOTHESIS_ANCHORING** | DIAGNOSIS, TREATMENT | 4+ failed hypotheses in same category | Ban the anchored category — adds a constraint LLM doesn't have |
| **EXHAUSTED** | DIAGNOSIS | 2+ categories explored, 2+ hypotheses refuted, no validated hypothesis, 8+ turns | Agent produces structured handoff — summary of findings, what remains uncertain, options for user |
| **FIX_FAILURE_CYCLE** | MITIGATION, TREATMENT | 2+ accepted proposed actions, verification milestone not set | Agent summarizes what was tried and presents options |
| **ACTION_LOOP** | All stages | Identical structural output across 5+ consecutive turns | Prompt injection (current); tool blocking (future) |

These patterns are checked **only while transparent mode is active** — if progress is being made (silent mode), there is no reason to check for failure patterns.

### Relationship to Progress Transparency

Progress transparency is the container. The repair patterns are specific diagnoses within it:

```
Silent Mode (progress being made)
  └── No checks needed

Transparent Mode (progress stalled)
  ├── Always: LLM injection with pending milestone + case-specific guidance
  └── Additionally, check for:
      ├── HYPOTHESIS_DEADLOCK → retire hypotheses
      ├── HYPOTHESIS_ANCHORING → ban category
      ├── EXHAUSTED → structured handoff
      ├── FIX_FAILURE_CYCLE → structured summary + options
      └── ACTION_LOOP → break the loop
```

---

## Threshold Configuration

| Parameter | Default | Description |
|---|---|---|
| `transparency_threshold` | 5 | Turns without milestone progress before transparent mode activates |
| `category_anchoring_threshold` | 4 | Failed hypotheses in same category before ANCHORING fires |
| `action_loop_threshold` | 5 | Turns with identical structural output before ACTION_LOOP fires |
| `exhaustion_min_turns` | 8 | Minimum total turns before EXHAUSTED pattern can fire |
| `fix_failure_threshold` | 2 | Accepted-but-unverified fix attempts before FIX_FAILURE_CYCLE fires |

---

## Implementation

### Components

- **Progress Monitor** (`core/investigation/progress_monitor.py`): Tracks investigative turns, determines silent/transparent mode, checks repair patterns, builds prompt injection.
- **Milestone Engine** (`core/investigation/milestone_engine.py`): Calls `ProgressMonitor.check_progress()` after each turn, stores injection in `system_feedback`.
- **API Models** (`models/api_models.py`): `ProgressTransparencyInfo` model on `TurnResponse` and `CaseUIResponse_Investigating`.
- **Case UI Adapter** (`services/adapters/case_ui_adapter.py`): Computes progress transparency from turn history for case page loads.
- **Investigation Service** (`modules/agent/domain/services/investigation_service.py`): Populates `ProgressTransparencyInfo` from turn metadata.

### Design Decisions

- **Stateless**: Progress transparency is fully computed from `case.turn_history` each turn. No persisted state, no schema changes.
- **No separate system message**: The agent's response carries the case-specific guidance. The `ProgressTransparencyInfo` in the API response enables frontend UI indicators.
- **Prompt injection via `system_feedback`**: Delivered to the LLM on the next turn (one-turn delay).
- **Context builder complement**: The context builder implements **stage-specific hypothesis condensing** that complements progress transparency by freeing token budget during MITIGATION/TREATMENT. During long DIAGNOSIS investigations (state summary mode), hypotheses are condensed to top 3 to avoid duplicating the state summary. See [Context Engineering Analysis: Stage-Specific Hypothesis Condensing](../../reference/deep-dives/context-engineering-analysis.md#stage-specific-hypothesis-condensing).

---

## Example Scenario

**Turn 1-4 (Silent mode):** User submits logs, agent analyzes them, verifies symptoms, assesses scope. Milestones advancing normally. Investigative turn counter: 4.

**Turn 5-7 (Silent mode):** Agent forms hypotheses, requests additional evidence. User provides some data. Agent tests hypotheses but can't validate any. Investigative turn counter: 7 (3 more investigative turns, still below threshold of 5 since last milestone at turn 4).

**Turn 8 (Silent mode, user chats):** User asks "what does connection pooling mean?" Agent answers. This is a conversational turn — counter stays at 7, not incremented.

**Turn 9 (Threshold reached):** User provides config file. Agent analyzes it, still can't validate. Counter reaches 5 investigative turns since last milestone. System detects threshold breach and stores the prompt injection in `system_feedback`.

**Note: one-turn delay.** The transparency guidance is stored on Turn 9 but delivered to the LLM on Turn 10. The user does not see it in the Turn 9 response.

**Turn 10 (Transparent mode — user sees guidance):** User sends a message. The LLM receives the `system_feedback` injection:

```
PROGRESS TRANSPARENCY: The investigation has been in DIAGNOSIS for 5 
investigative turns without reaching the next milestone.
Pending milestone: root_cause_identified — Validated hypothesis with 
supporting causal evidence.
In your response, provide case-specific guidance on what evidence would 
advance this milestone. Be concrete: name specific files, services, time 
ranges, or commands based on what you know about this case.
```

Agent response includes: "We've narrowed the issue to either a connection pool exhaustion or a DNS resolution timeout, but the current evidence doesn't distinguish between them. A packet capture from the application server during the next error window (likely between 14:00-16:00 UTC based on the pattern) would tell us which one it is. Alternatively, the connection pool metrics from the monitoring dashboard would show whether connections are being exhausted."

**Turn 11:** User provides packet capture. Agent analyzes it, validates hypothesis, `root_cause_identified` milestone completes. Transparent mode → Silent mode. Counter resets.

---

## What This Feature Does NOT Do

- **Does not detect or judge user behavior.** If the user chats for 20 turns without providing data, the counter only increments on turns where the agent attempted diagnostic work. Conversational turns don't trigger transparency.
- **Does not force the agent to do anything.** The injection provides context and asks for case-specific guidance. It does not override the behavioral rules or the stage-specific template.
- **Does not replace the behavioral rules.** Rules 1-7 govern per-turn agent behavior. Progress transparency governs multi-turn trajectory awareness.
- **Does not guarantee resolution.** Some investigations won't resolve regardless. This feature makes the dependencies visible so the user can make informed decisions.
