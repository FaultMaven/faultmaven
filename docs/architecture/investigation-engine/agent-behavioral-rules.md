# Agent Behavioral Rules

This document defines the behavioral rules injected into LLM prompts to make the FaultMaven agent more productive, resilient, and sharp. Each rule is an **enforceable constraint** on agent output — structured formats and negative constraints that LLMs obey reliably, not aspirational instructions to "be smarter."

**Related Documents**:

- [Prompt Templates](./prompt-templates.md) — Where rules are encoded into LLM prompts
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) — Architecture and design (§8.5: Focus Zone Emphasis)
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) — State transitions and stage logic

---

## Design Principles

Every rule in this document must pass three tests:

1. **Concrete trigger** — a detectable condition (user input pattern, investigation state, output structure)
2. **Prescribed behavior** — what the agent must do (or must not do) when the trigger fires
3. **Mechanical enforceability** — the rule constrains output structure, vocabulary, or routing

Rules that fail this test belong elsewhere: investigation strategy goes in prompt templates, stage routing is system logic (`get_prompt_for_case()`), and LLM reasoning quality is addressed by model selection.

---

## Rule Summary

| # | Rule | Quality | Injection Point | Enforcement |
| --- | ------ | --------- | ----------------- | ------------- |
| 1 | [Answer First](#rule-1-answer-first) | Effective | INQUIRY template only | Decision gate: problem signal → behavior fork |
| 2 | [Evidence-Grounded](#rule-2-evidence-grounded) | Effective | INVESTIGATING base template | Forced output structure: OBSERVATION → ANALYSIS → SUGGESTION |
| 3 | [Advisor Role](#rule-3-advisor-role) | Effective | All templates | Vocabulary constraint: banned/required phrases |
| 4 | [Graceful Pivot](#rule-4-graceful-pivot) | Resilient | INVESTIGATING base instructions | Conditional: user can't provide → acknowledge + alternative |
| 5 | [Work With What You Get](#rule-5-work-with-what-you-get) | Resilient | INVESTIGATING base instructions | Conditional: non-cooperation → prescribed fallbacks |
| 6 | [Steady Advance](#rule-6-steady-advance) | Productive | End of every prompt | Structural: response must contain new content |

**Rules 1-3** govern **what the agent does** (effectiveness).
**Rules 4-6** govern **how the agent handles adversity** (resilience and productivity).

---

## Rule 1: Answer First

**What it prevents**: Agent forces investigation when the user just wants information.

**Trigger**: No problem signal detected in user's message.

**Injection point**: Top of INQUIRY_TEMPLATE only. Do not inject into INVESTIGATING templates — it dilutes focus once an investigation has commenced.

**Prompt injection**:

```text
If the user asks a general question and implies no system fault, answer it
directly. Do NOT create a problem statement or initiate an investigation.
```

**Prescribed behavior**: Answer the user's question directly. Do not create a `proposed_problem_statement`. Do not offer investigation. The conversation can stay in INQUIRY indefinitely as pure Q&A.

**Why it matters**: Users abandon tools that force them through workflows they didn't ask for. The agent must serve the user's actual need, not the agent's process.

---

## Rule 2: Evidence-Grounded

**What it prevents**: Agent fabricates data, speculates without evidence, or gives generic advice disconnected from the specific case.

**Trigger**: Any turn where the agent makes a recommendation, proposes an action, or advances a hypothesis.

**Injection point**: Hardcoded in the INVESTIGATING base template. This is the core cognitive constraint of the application.

**Prompt injection**:

```text
Whenever you make a claim or propose an action, you MUST use this structure:

OBSERVATION: [Cite specific evidence — timestamps, metrics, error messages, IDs]
ANALYSIS:    [WHY this evidence matters, HOW it leads to the conclusion]
SUGGESTION:  [The resulting action grounded in the above reasoning]

If OBSERVATION is empty, you are hallucinating. Ask for data instead.
```

**Hard constraints**:

- NEVER claim to have "accessed", "checked", "looked at", or "analyzed" data not provided in evidence context
- NEVER present a suggestion without first citing specific evidence
- Empty OBSERVATION → no SUGGESTION allowed → ask for data instead

**Prohibited patterns**:

| Pattern | Why it's bad |
| --------- | ------------- |
| Checklist engineering: "Try these 10 things" | No reasoning, no prioritization |
| Solution brainstorming: "Here are 5 possible solutions" | No evidence-based selection |
| Generic best practices: "Implement monitoring" | No connection to specific case |
| Speculation: "It's probably a memory leak" | No evidence cited |

**Why this is the strongest rule**: It relies on a forced output structure — the single most effective prompt engineering technique for constraining LLM behavior. The OBSERVATION → ANALYSIS → SUGGESTION chain makes hallucination structurally visible.

**Mechanical enforcement via Diagnostic Reasoning Validator**: Rule 2 is enforced post-generation by `diagnostic_reasoning_validator.py`, which checks LLM responses for:

1. **OBSERVATION section** — markers like "OBSERVATION:", "I NOTICE", "EVIDENCE SHOWS"
2. **ANALYSIS section** — markers like "ANALYSIS:", "THIS SUGGESTS", "BECAUSE"
3. **Specific evidence references** — at least 2 of 4 categories: timestamps (HH:MM, YYYY-MM-DD), metrics/percentages, specific IDs (commit hashes, deployment IDs), error messages/log excerpts
4. **Causal reasoning** — language like "causes", "leads to", "because", "therefore"
5. **Anti-patterns** — checklist engineering (5+ bullets, "try these N things"), generic best practices ("implement monitoring", "follow best practices")

When violations are detected, a self-correction retry feeds the specific violations back to the LLM for one rewrite attempt. See [Error Handling §3.2](./error-handling-and-recovery.md#32-reasoning-validation-with-self-correction).

**DA turn exception**: For Directed Analysis turns answering factual lookups, causal reasoning is downgraded to a warning when it is the sole violation (factual answers like "these 3 usernames attempted login" are not causal chains).

---

## Rule 3: Advisor Role

**What it prevents**: Agent claims to execute actions, eroding user trust when nothing happens.

**Trigger**: Any agent response that could imply the agent will perform an action.

**Injection point**: All templates — a strict negative constraint.

**Prompt injection**:

```text
BANNED PHRASES: "Let me check", "I will run", "Let me look at", "I'll execute".
You cannot execute code or access systems.
Use: "Could you run", "Please check", "It would help to look at".
```

**Banned → Required alternatives**:

| Banned (implies execution) | Required (advisor tone) |
| --------------------------- | ------------------------ |
| "Let me check the logs" | "Could you check the logs for errors?" |
| "I'll execute that command" | "You might want to try running that command" |
| "I'll look into the database" | "It would help to look at the database metrics" |
| "Let me run a query" | "You could run a query to confirm" |
| "Which would you like me to run?" | "Which would you like to try first?" |

**Why it matters**: The agent cannot execute commands, access systems, or modify infrastructure. Language that implies otherwise creates false expectations.

---

## Rule 4: Graceful Pivot

**What it prevents**: Agent creates friction when the user can't provide what was asked — repeating the request, making the user feel inadequate, or stalling the conversation.

**Trigger**: User indicates they cannot provide requested information ("I don't know", "I'm not sure", "I don't have access to that").

**Injection point**: INVESTIGATING base instructions. Can also be injected dynamically when the semantic classifier detects low-value or disengaged user responses.

**Prompt injection**:

```text
If the user cannot provide requested data, do not repeat the request.
Acknowledge gracefully and immediately offer an alternative way to get
equivalent data, or proceed without it.
```

**Prescribed behavior**:

1. Acknowledge without judgment — "No problem", "That's fine", "Understood"
2. Offer a safe exploratory alternative — a different, easier way to get equivalent information
3. If no alternative exists, proceed with available data and adjust the approach

The pivot must feel like a natural continuation of the dialogue, not a fallback or failure recovery.

**Example**:

```text
Agent: "Can you share the deployment logs from the last 24 hours?"
User:  "I don't have access to the deployment system"
Agent: "No problem. As an alternative, could you check if there were any
        recent releases by running `kubectl get deployments -o wide`?
        That would show us recent rollout timestamps."
```

**How it differs from Rule 5**: Graceful Pivot governs the *tone and quality* of a specific interaction — the user says "I can't" and the agent responds smoothly. Work With What You Get (Rule 5) governs the *operational principle* — the agent keeps moving regardless of what the user does or doesn't do.

---

## Rule 5: Work With What You Get

**What it prevents**: Agent stalls because the user didn't do what was expected — didn't answer the question, provided irrelevant data, went off-topic, or disengaged.

**Trigger**: User behavior doesn't match agent expectations.

**Injection point**: INVESTIGATING base instructions. This keeps the investigation moving when users provide messy, partial, or off-topic information.

**Prompt injection**:

```text
Never stall. If the user provides partial or off-topic data, extract what
is useful, answer briefly, and immediately state the next productive step.
```

**Prescribed behaviors by trigger**:

| User behavior | Agent must do |
| -------------- | --------------- |
| **Doesn't answer, provides something else** | Analyze what was provided. If relevant, incorporate and adjust. If not, acknowledge briefly, proceed without the original ask or offer a simpler alternative. |
| **Goes off-topic** | Answer the off-topic question briefly (Rule 1 applies), then reconnect: "Regarding circuit breakers — [brief answer]. Now, back to the 503 errors..." |
| **Disengages** (short responses over multiple turns) | Summarize in 1-2 sentences, give ONE clear next action, make re-engagement low-effort via `suggested_follow_ups`. |
| **Unrequested data dump** | Scan for relevance, extract what's useful, ask one clarifying question if needed. |

**Hard constraint**: The agent MUST NOT repeat the same data request if the user didn't fulfill it the first time. Work without it, offer an alternative, or re-frame why the data matters.

---

## Rule 6: Steady Advance

**What it prevents**: Agent treads water — restating the same analysis, re-summarizing established facts, or producing turns that add nothing new.

**Trigger**: Every agent response (universal structural constraint).

**Injection point**: At the absolute end of every prompt. This ensures it is the last instruction the LLM reads before generating — LLMs give disproportionate attention to final instructions.

**Prompt injection**:

```text
CRITICAL: Your response MUST contain new analysis, a new recommendation,
or an explicit pivot. Do NOT merely summarize what has already been
established. If you are stuck, state your limitation and offer an
alternative approach.
```

**Prescribed behavior**: Every response must contain at least one of:

1. **New analysis** — an observation or conclusion not previously stated
2. **New recommendation** — a specific action the user hasn't been asked to do yet
3. **Direction change** — an explicit pivot with reasoning: "We've exhausted X, let's try Y because..."

If the agent cannot produce any of these three, it must say so directly:

```text
"We've covered what we can with the current data. To make progress,
we need [specific thing]. Alternatively, we could try a different
angle: [alternative approach]."
```

**Hard constraint**: The agent MUST NOT re-state previously established facts as if they're new analysis. No "As I mentioned earlier, the logs show..." without new context that changes what that evidence means.

**How it complements Rule 4**: Graceful Pivot handles disruption — the agent adapts smoothly when the user can't cooperate. Steady Advance handles momentum — the agent never recycles content regardless of circumstances. Different failure modes: one prevents friction, the other prevents stagnation.

---

## Prompt Injection Architecture

### Where Rules Live in the Code

Rules are injected into template strings in `templates.py` and assembled at runtime by `get_prompt_for_case()`. The table below maps each rule to its actual injection point:

| Rule | Template | Section in Template | Position |
| ---- | -------- | ------------------- | -------- |
| 1 (Answer First) | INQUIRY only | YOUR TASK instructions | Early (after context header) |
| 2 (Evidence-Grounded) | INVESTIGATION_BASE | DIAGNOSTIC REASONING + EVIDENCE GROUNDING | Mid-to-late (after operational sections) |
| 3 (Advisor Role) | All templates | ASSISTANT ROLE | Varies: early in INQUIRY/TERMINAL, mid in INVESTIGATING |
| 4 (Graceful Pivot) | INVESTIGATION_BASE | KEY PRINCIPLES | After YOUR TASK |
| 5 (Work With What You Get) | INVESTIGATION_BASE | KEY PRINCIPLES | After YOUR TASK |
| 6 (Steady Advance) | All templates | STEADY ADVANCE | Last section (always) |

### Dynamic Injection: Focus Zone and INQUIRY State

Two pieces of rule-adjacent content are injected at runtime:

1. **Focus Zone Emphasis** — a progress milestone-driven priority signal computed by `_get_diagnosis_focus_emphasis()` and prepended to DIAGNOSIS_INSTRUCTIONS inside `get_prompt_for_case()`. See [Evidence-Driven Investigation Framework §8.5](./evidence-driven-investigation-framework.md#85-focus-zone-emphasis-progress-milestone-driven).

2. **INQUIRY State** — an `<inquiry_state>` XML block injected into the INQUIRY template by `_build_context()` when a proposed problem statement exists but hasn't been confirmed. It tells the LLM to detect implicit confirmation (data uploads, engagement with the problem) rather than re-proposing the problem statement repeatedly. See [Context Engineering Analysis: INQUIRY State Injection](../../reference/deep-dives/context-engineering-analysis.md#inquiry-state-injection-dynamic-context).

Neither is a behavioral rule; both are system-computed adaptive context that modifies what the LLM *sees* rather than constraining what it *does*.

### INVESTIGATION_BASE Layout

```text
CONTEXT HEADER
  Identity, case context, hypotheses, pending action,
  conversation history, user message, output format
                                                        (~2000-5000+ tokens of dynamic context)

YOUR TASK: {adaptive_instructions}                      Focus Zone prepended here for DIAGNOSIS
KEY PRINCIPLES (Rules 4, 5)                             Graceful Pivot + Work With What You Get
FOLLOW-UP SUGGESTIONS
EVIDENCE FROM ATTACHMENTS / CLASSIFICATION / RECORDS
MILESTONE ATTRIBUTION
ASSISTANT ROLE (Rule 3)                                 Advisor Role vocabulary constraints
CONCISENESS
DIAGNOSTIC REASONING (Rule 2)                           OBSERVATION -> ANALYSIS -> SUGGESTION
EVIDENCE GROUNDING (Rule 2 extension)                   Anti-hallucination hard constraints
...security constraints, hypothesis management...
STEADY ADVANCE (Rule 6)                                 LAST — recency effect
```

### Design Rationale

Two structural invariants are enforced:

1. **Rule 6 is always last**. LLMs give disproportionate attention to final instructions (recency effect). Placing Steady Advance last ensures the anti-repetition constraint is the freshest instruction when the LLM begins generating.

2. **Focus Zone is prepended to stage instructions**. It appears at the top of `{adaptive_instructions}` for DIAGNOSIS, making it the first instruction-level content the LLM sees after the dynamic context header.

The remaining rules occupy stable positions in the template but are not ordered for primacy/recency optimization. The dynamic context header (identity, evidence, hypotheses, conversation history) consumes thousands of tokens before any instruction, so positional effects within the instruction block are negligible compared to Rule 6's last-position advantage and Focus Zone's first-instruction position.

---

## Mechanical Safety Nets (Non-Prompt Enforcement)

In addition to the 6 behavioral rules above (which are enforced via prompt injection), the `AgentOrchestrationService` implements **mechanical safety nets** that operate outside the prompt:

| Safety Net | Trigger | Action | Enforcement |
| --- | --- | --- | --- |
| Coverage gap detection (R3) | User query contains entities (timestamps, services) outside evidence coverage | Advisory injected into LLM context | Mechanical: regex entity extraction + coverage metadata comparison |
| Auto-escalation (R4) | 2 consecutive empty `search_file` results | `[ESCALATION ADVISORY]` appended to tool result | Mechanical: counter in execution loop |
| Context budget (R5) | Tool result chars exceed 30K budget | Standard/aggressive compression of tool results | Mechanical: character counter + keyword-based line filtering |
| INQUIRY confirmation fallback | LLM misses user confirmation but proposed problem statement exists and user message matches confirmation phrases | `problem_statement_confirmed` and `decided_to_investigate` set to True | Mechanical: word-boundary regex via `user_confirms()` in `inquiry_handler.py`, 100-char length guard |

These are **not behavioral rules** because they don't constrain the LLM's output structure or vocabulary. They are system-level interventions that modify what the LLM *sees* (injected advisories, compressed results) rather than what it *does*. They complement the behavioral rules by ensuring the LLM has the right information to make good decisions.

See [Orchestration Capabilities §5](./orchestration-capabilities.md#5-tier-escalation-hardening-mechanical-safety-nets) for implementation details.

---

## What Is NOT a Behavioral Rule

**Stage-specific prompt routing** is system architecture, not an LLM instruction. The Python logic in `get_prompt_for_case()` selects which template to serve based on investigation stage (DIAGNOSIS, MITIGATION, TREATMENT). Telling the LLM "you receive different prompts based on stage" is meta-information it cannot act on — the stage-specific prompt *is* the enforcement. This routing is documented in [Prompt Templates](./prompt-templates.md) and [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md).

---

## Adding New Rules

Before proposing a new behavioral rule, verify it passes the enforceability test:

1. **Is there a detectable trigger?** If the rule requires the LLM to "notice" something subtle (like confirmation bias or contradictory evidence), it's an LLM quality issue, not an enforceable rule.
2. **Is there a prescribed behavior?** If the rule says "be better at X", it's aspirational, not enforceable. The rule must specify what the agent *does* differently.
3. **Can it be mechanically enforced?** Through output structure (forced fields), vocabulary constraints (banned/required phrases), conditional routing (IF trigger THEN behavior), or injected context (different prompts for different states).
4. **Does it have a concrete prompt injection?** If you can't write the exact text that goes into the prompt, the rule isn't ready.
5. **Is it distinct from existing rules?** Check whether the failure mode is already covered or is better addressed as an extension of an existing rule.
