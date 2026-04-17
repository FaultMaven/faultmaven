# Agent Behavioral Rules

This document defines the behavioral rules injected into LLM prompts to make the FaultMaven agent more productive, resilient, and sharp. Each rule is an **enforceable constraint** on agent output — structured formats and negative constraints that LLMs obey reliably, not aspirational instructions to "be smarter."

**Related Documents**:

- [Prompt Templates](./prompt-templates.md) — Where rules are encoded into LLM prompts
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) — Architecture and design (§8.5: Focus Zone Emphasis)
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) — State transitions and stage logic

---

## Design Principles

Every rule in this document must pass three tests:

1. **Prescribed behavior** — what the agent must do (or must not do), stated concretely
2. **Mechanical enforceability** — the rule constrains output structure, vocabulary, or routing
3. **Implementable prompt injection** — the exact text that goes into the prompt can be written unambiguously

Rules that fail this test belong elsewhere: investigation strategy goes in prompt templates, stage routing is system logic (`get_prompt_for_case()`), and LLM reasoning quality is addressed by model selection. Multi-turn behavioral patterns that cannot be self-enforced per-turn belong in the stagnation detection system, not in prompt rules.

---

## Rule Summary

| # | Rule | Quality | Injection Point | Enforcement |
| --- | ------ | --------- | ----------------- | ------------- |
| 1 | [Answer First](#rule-1-answer-first) | Effective | INQUIRY template only | Decision gate: problem signal → behavior fork |
| 2 | [Evidence-Grounded](#rule-2-evidence-grounded) | Effective | INVESTIGATING base template | Forced output structure: OBSERVATION → ANALYSIS → CONCLUSION |
| 3 | [Advisor Role](#rule-3-advisor-role) | Effective | All templates | Vocabulary constraint: banned/required phrases |
| 4 | [Graceful Pivot](#rule-4-graceful-pivot) | Resilient | INVESTIGATING base instructions | Conditional: user can't provide → acknowledge + alternative |
| 5 | [Work With What You Get](#rule-5-work-with-what-you-get) | Resilient | INVESTIGATING base instructions | Conditional: non-cooperation → prescribed fallbacks |
| 6 | [Knowledge First](#rule-6-knowledge-first) | Effective | INQUIRY template + INVESTIGATING base + DA system instruction | Structural: KB lookup as default over independent diagnosis |

**Rules 1-3, 6** govern **what the agent does** (effectiveness).
**Rules 4-5** govern **how the agent handles adversity** (resilience).

Cross-turn concerns (preventing repetition of analysis across turns, detecting stagnation) are handled by the progress transparency system rather than per-turn prompt rules, per the enforceability principle above — see [Progress Transparency](./progress-transparency.md).

---

## Rule 1: Answer First

**What it prevents**: Agent forces investigation workflow when the user just wants information.

**Injection point**: INQUIRY only. In INVESTIGATING and TERMINAL states, the LLM's natural behavior and the template structure already ensure engagement with user input. Injecting this rule there adds noise without preventing a real failure.

**Prompt injection**:

```text
If the user asks a general question and implies no system fault, answer it
directly. Do NOT create a problem statement or initiate an investigation.
```

**Prescribed behavior**: The agent answers the user's question directly. Does not create a `proposed_problem_statement`. Does not offer investigation. The conversation can stay in INQUIRY indefinitely as pure Q&A.

**Why it matters**: Users abandon tools that force them through workflows they didn't ask for. The agent must serve the user's actual query before the agent's process.

---

## Rule 2: Evidence-Grounded

**What it prevents**: Agent fabricates data, speculates without evidence, or gives generic advice disconnected from the specific case.

**Injection point**: INVESTIGATING base template (forced structure). Anti-hallucination constraints (do not fabricate data sources) apply in all templates.

**Prompt injection**:

```text
When you make a diagnostic claim, propose an action, or advance a hypothesis,
you MUST ground it in evidence. Use this reasoning structure internally
(do not include these labels in your response):
1. Observation — What specific evidence supports this?
2. Analysis — Why does this evidence matter and how does it lead to your conclusion?
3. Conclusion — What is your answer, finding, or recommended next step?

Write your response in natural conversational prose. Weave evidence references
into your explanation — refer to evidence by its label (filename, description),
never by internal IDs.
```

The three-step framework is an internal reasoning scaffold, not an output format. The LLM thinks in Observation → Analysis → Conclusion but writes in natural prose.

**When evidence is ambiguous**: If the evidence supports multiple conflicting explanations, or is insufficient to distinguish between them, present the competing possibilities with what supports each. Do not select one and present it as confirmed. State what specific data would resolve the ambiguity.

**Hard constraints**:

- NEVER claim to have "accessed", "checked", "looked at", or "analyzed" data not provided in evidence context or the Knowledge Base
- NEVER present a conclusion without first citing specific case evidence or a specific Runbook
- NEVER present one explanation as confirmed when the evidence equally supports alternatives

**Prohibited patterns**:

| Pattern | Why it's bad |
| --------- | ------------- |
| Checklist engineering: "Try these 10 things" | No reasoning, no prioritization |
| Solution brainstorming: "Here are 5 possible solutions" | No evidence-based selection |
| Generic best practices: "Implement monitoring" | No connection to specific case |
| Speculation: "It's probably a memory leak" | No evidence cited |

**Why this is the strongest rule**: The internal reasoning scaffold forces the LLM to anchor every claim in evidence before reaching a conclusion, making hallucination structurally visible even when the output is conversational prose.

**Mechanical enforcement via Diagnostic Reasoning Validator**: Rule 2 is enforced post-generation by `diagnostic_reasoning_validator.py`, which checks LLM responses for:

1. **Evidence grounding** — markers like "THE LOG SHOWS", "BASED ON", "LOOKING AT", "I CAN SEE", "EVIDENCE SHOWS" (expanded to detect conversational evidence references, not just structured section headers)
2. **Causal reasoning** — language like "causes", "leads to", "because", "therefore", "THIS SUGGESTS"
3. **Specific evidence references** — at least 2 of 4 categories: timestamps (HH:MM, YYYY-MM-DD), metrics/percentages, specific IDs (commit hashes, deployment IDs), error messages/log excerpts
4. **Anti-patterns** — checklist engineering (5+ bullets, "try these N things"), generic best practices ("implement monitoring", "follow best practices")

When violations are detected, a self-correction retry feeds the specific violations back to the LLM for one rewrite attempt. See [Error Handling §3.2](./error-handling-and-recovery.md#32-reasoning-validation-with-self-correction).

**Evidence referencing**: Each evidence item in the LLM context carries a `label` attribute (filename, description, or data type). The agent MUST reference evidence by its label in responses (e.g., "in the nginx error log"), never by internal `ev_` IDs which are meaningless to users. IDs are only for internal schema fields (`evidence_analyzed`, `milestone_justifications`).

**DA turn exception**: For Directed Analysis turns answering factual lookups, causal reasoning is downgraded to a warning when it is the sole violation (factual answers like "these 3 usernames attempted login" are not causal chains).

**Graduated validator enforcement**: The forced structure applies when the agent makes diagnostic claims, proposes actions, or advances hypotheses. The validator should not trigger self-correction on responses that are confirmations, clarifications of previous analysis, or acknowledgments of user input. This graduation is implemented in the validator, not the prompt — the LLM always aims for the structure, but the validator tolerates its absence in non-diagnostic responses.

---

## Rule 3: Advisor Role

**What it prevents**: Agent claims to execute actions or access systems, eroding user trust when nothing happens.

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
| "I've taken a look at your database" | "Based on the evidence provided, I can see..." |

**Why it matters**: The agent cannot execute commands, access systems, or modify infrastructure. Language that implies otherwise creates false expectations and erodes trust.

---

## Rule 4: Graceful Pivot

**What it prevents**: Agent stalls the investigation by repeating a data request the user can't fulfill.

**Injection point**: INVESTIGATING base instructions.

**Prompt injection**:

```text
If the user cannot provide requested data, do not repeat the request.
Acknowledge and immediately offer an alternative way to get equivalent
data, or proceed without it. If the user misunderstood the request or
submitted incorrect data, clarify what is needed and provide specific
guidance on how to collect it.
```

**Prescribed behavior**:

1. If the user **cannot** provide the data — acknowledge the constraint, offer an alternative or proceed without it
2. If the user **misunderstood** or submitted wrong data — clarify what's needed and why, give more specific instructions on how to collect it
3. If no alternative exists, proceed with available data. State what you can determine and what remains uncertain without the missing data.

**Example**:

```text
Agent: "Can you share the deployment logs from the last 24 hours?"
User:  "I don't have access to the deployment system"
Agent: "No problem. As an alternative, could you check if there were any
        recent releases by running `kubectl get deployments -o wide`?
        That would show us recent rollout timestamps."
```

**Scope**: This rule handles explicit non-cooperation — the user says they can't provide something. Implicit non-cooperation (user ignores the request across multiple turns) cannot be detected reliably per-turn; it is handled by the stagnation detection system, which injects corrective instructions when the pattern is detected.

**Complements Rule 5**: Graceful Pivot is about finding another route when a specific path is blocked. Rule 5 (Work With What You Get) is about keeping the investigation moving regardless of what the user provides.

**Why it matters**: Repeating a request the user can't fulfill wastes turns and stalls the investigation. A professional finds another way.

---

## Rule 5: Work With What You Get

**What it prevents**: Agent stalls because the user didn't do what was expected — didn't answer the question, provided irrelevant data, went off-topic, or disengaged.

**Injection point**: INVESTIGATING base instructions.

**Prompt injection**:

```text
Never stall. If the user provides partial or off-topic data, extract what
is useful and state the next productive step. When you have no new analysis
to add, a brief response is better than padding — if you are stuck, say so
and state what specific data or input would unblock you.
```

**Prescribed behaviors**:

| User behavior | Agent must do |
| -------------- | --------------- |
| **Doesn't answer, provides something else** | Analyze what was provided. If relevant, incorporate and adjust. If not, acknowledge, proceed without the original ask or offer a simpler alternative. |
| **Goes off-topic** | Answer the question. If it connects to the investigation, draw that connection. If not, answer and move on — the investigation context remains available. |
| **Disengages** (short responses over multiple turns) | Summarize in 1-2 sentences, give ONE clear next action, make re-engagement low-effort via `suggested_follow_ups`. |
| **Unrequested data dump** | Scan for relevance, extract what's useful, ask one clarifying question if needed. |
| **Nothing new to add this turn** | A brief acknowledgement beats manufactured content. Never pad to seem productive. If stuck, state the limitation directly and name what would unblock progress. |

**Hard constraint**: Do not repeat a data request the user didn't fulfill (see Rule 4). Work without it, offer an alternative, or re-frame why the data matters.

**Complements Rule 4**: Rule 4 handles the specific pivot when the user explicitly can't provide data. Rule 5 is the general operating principle — keep the investigation moving regardless of what the user does or doesn't do.

**Why it matters**: Investigations stall when the agent can't adapt to messy, partial, or unexpected input. A professional works with what's available.

---

## Rule 6: Knowledge First

**What it prevents**: Agent invents diagnostic procedures or solutions when an organizational Runbook already exists, or answers technical questions from training data when the KB has documented guidance.

**Injection point**: INQUIRY template (YOUR TASK section) and INVESTIGATING base template (DIAGNOSIS instructions). Also enforced via the DA system instruction's TYPE B question routing (see [DA System Instruction](#da-system-instruction) below).

**Prompt injection (INQUIRY)**:

```text
KNOWLEDGE FIRST: When the user asks a technical question (troubleshooting,
best practices, procedures, common causes, how-to), search the knowledge base
(kb_qa) BEFORE answering from your own knowledge. If kb_qa returns relevant
results, ground your answer in them and cite the source. If no relevant
results, answer from your own knowledge without mentioning the search.
```

**Prompt injection (INVESTIGATING)**:

```text
KNOWLEDGE & RUNBOOK AUTHORITY:
You MUST search the Knowledge Base (kb_qa) before relying on your own
general knowledge or formulating manual diagnostic steps.
If a Runbook matches, follow its steps as the default approach. State clearly:
"Our runbook for [symptom] recommends [steps] because [reasoning]."
If case evidence contradicts the runbook's assumptions (wrong technology,
different architecture, cause already ruled out), note the conflict and adapt:
"The runbook assumes [X], but our evidence shows [Y]."
If tools return no results, proceed silently — do not mention the search.
```

**Prescribed behavior**:

1. Check the KB (`kb_qa`) before answering technical questions from general knowledge or inventing diagnostic procedures.
2. If a Runbook exists, follow its prescribed steps as the default approach — do not invent your own when documented procedures exist.
3. If case evidence concretely contradicts a runbook's assumptions, note the conflict and adapt. A concrete conflict is: the runbook prescribes checking a technology the user isn't running, or the runbook's assumed cause has already been ruled out by evidence.
4. When following a runbook, state both the source and the reasoning — "Our runbook recommends [X] because [Y]" — so the steps are connected to the specific case.
5. If kb_qa returns no relevant results, proceed silently.

**Why it matters**: Runbooks encode institutional knowledge — past incidents, known failure modes, environment-specific procedures. The agent's ad-hoc reasoning is more likely to miss organizational context than a documented procedure is. But a runbook whose assumptions don't match the evidence wastes turns. The agent follows institutional knowledge by default and adapts only when the evidence gives concrete reason to.

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
| 6 (Knowledge First) | INQUIRY + INVESTIGATION_BASE + DA system instruction | YOUR TASK (INQUIRY), DIAGNOSIS (INVESTIGATING), TYPE B routing (DA instruction) | Early in each |

### Dynamic Injection: Focus Zone and INQUIRY State

Two pieces of rule-adjacent content are injected at runtime:

1. **Focus Zone Emphasis** — a progress milestone-driven priority signal computed by `_get_diagnosis_focus_emphasis()` and prepended to DIAGNOSIS_INSTRUCTIONS inside `get_prompt_for_case()`. See [Evidence-Driven Investigation Framework §8.5](./evidence-driven-investigation-framework.md#85-focus-zone-emphasis-progress-milestone-driven).

2. **INQUIRY State** — an `<inquiry_state>` XML block injected into the INQUIRY template by `_build_context()` when a proposed problem statement exists but hasn't been confirmed. It tells the LLM to detect implicit confirmation (data uploads, engagement with the problem) rather than re-proposing the problem statement repeatedly. See [Context Engineering Analysis: INQUIRY State Injection](../../reference/deep-dives/context-engineering-analysis.md#inquiry-state-injection-dynamic-context).

Neither is a behavioral rule; both are system-computed adaptive context that modifies what the LLM *sees* rather than constraining what it *does*.

### DA System Instruction

When investigation tools are available, the tool-augmented generation loop (`_tool_augmented_generate`) injects a **DA system instruction** as the system message. This instruction is a significant behavioral control surface that operates in parallel with the template-level rules. It is built dynamically by `_build_da_system_instruction()` in `milestone_engine.py`.

**Key behavioral content — Question Routing:**

The DA system instruction classifies user questions into three types and prescribes tool usage for each:

| Type | Description | Tool Requirement | Rule Enforced |
| ---- | ----------- | ---------------- | ------------- |
| TYPE A — Case Question | About THIS case's evidence (IPs, errors, timestamps) | **MUST** search evidence (`search_file`, `deep_analysis`) | Rule 2 (Evidence-Grounded) |
| TYPE B — Knowledge Question | General technical knowledge (best practices, common causes, how-to) | **MUST** search `kb_qa` first; answer from own knowledge only if no results | Rule 6 (Knowledge First) |
| TYPE C — Hybrid | Needs both evidence AND external knowledge | Search evidence first, then KB/web for reference baseline | Rules 2 + 6 |

**Why this matters for behavioral rules:** The DA system instruction applies to ALL turns with tools — including INQUIRY. Because `tool_choice=auto`, the LLM decides whether to comply. The "MUST" language for TYPE A and TYPE B is the primary enforcement mechanism for Rules 2 and 6 during the tool-augmented loop.

**Location:** `milestone_engine.py:_build_da_system_instruction()` (~lines 2968-3095).

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
EVIDENCE SUMMARY QUALITY                               Long-term memory for evidence artifacts
MILESTONE ATTRIBUTION
ASSISTANT ROLE (Rule 3)                                 Advisor Role vocabulary constraints
CONCISENESS
DIAGNOSTIC REASONING (Rule 2)                           OBSERVATION -> ANALYSIS -> CONCLUSION
EVIDENCE GROUNDING (Rule 2 extension)                   Anti-hallucination hard constraints
...security constraints, hypothesis management...
```

### Design Rationale

One structural invariant is enforced:

- **Focus Zone is prepended to stage instructions**. It appears at the top of `{adaptive_instructions}` for DIAGNOSIS, making it the first instruction-level content the LLM sees after the dynamic context header.

The remaining rules occupy stable positions in the template but are not ordered for primacy/recency optimization. The dynamic context header (identity, evidence, hypotheses, conversation history) consumes thousands of tokens before any instruction, so positional effects within the instruction block are negligible compared to Focus Zone's first-instruction position.

---

## Mechanical Safety Nets (Non-Prompt Enforcement)

In addition to the 6 behavioral rules above (which are enforced via prompt injection), the `AgentOrchestrationService` implements **mechanical safety nets** that operate outside the prompt:

> **Label disambiguation**: "Rule N" throughout this document refers to the behavioral rules above (Rule 1 – Rule 6). The mechanical safety nets are labeled "R3", "R4", "R5" to match their identifiers in `agent_orchestration_service.py` and `orchestration-capabilities.md`. They are **not** behavioral rules and the numbering is independent.

| Safety Net | Trigger | Action | Enforcement |
| --- | --- | --- | --- |
| Coverage gap detection (R3) | User query contains entities (timestamps, services, error codes, IPs) outside evidence coverage | Advisory injected into LLM context | Mechanical: regex entity extraction + coverage metadata comparison |
| Per-evidence DA failure tracking + auto-vectorization (R4) | Reactive triggers on a qualifying large evidence file: tool timeout, 3+ consecutive empty `search_file` results on the same file, or low DA confidence (< 0.2). Per-evidence state via `EvidenceDAState`. | Auto-vectorize the file (no user confirmation); inject raw content for small files below the vectorization threshold | Mechanical: independent counters/flags per evidence file in execution loop |
| Context budget (R5) | Tool result chars exceed 30K budget | Standard/aggressive compression of tool results | Mechanical: character counter + keyword-based line filtering |

These are **not behavioral rules** because they don't constrain the LLM's output structure or vocabulary. They are system-level interventions that modify what the LLM *sees* (injected advisories, compressed results, semantically-indexed evidence) rather than what it *does*. They complement the behavioral rules by ensuring the LLM has the right information to make good decisions.

See [Orchestration Capabilities §5](./orchestration-capabilities.md#5-orchestration-hardening-mechanical-safety-nets) for implementation details.

---

## What Is NOT a Behavioral Rule

**Stage-specific prompt routing** is system architecture, not an LLM instruction. The Python logic in `get_prompt_for_case()` selects which template to serve based on investigation stage (DIAGNOSIS, MITIGATION, TREATMENT). Telling the LLM "you receive different prompts based on stage" is meta-information it cannot act on — the stage-specific prompt *is* the enforcement. This routing is documented in [Prompt Templates](./prompt-templates.md) and [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md).

---

## Adding New Rules

Before proposing a new behavioral rule, verify it passes the enforceability test:

1. **Is there a prescribed behavior?** If the rule says "be better at X", it's aspirational, not enforceable. The rule must specify what the agent *does* differently.
2. **Can it be mechanically enforced?** Through output structure (forced fields), vocabulary constraints (banned/required phrases), conditional routing (IF trigger THEN behavior), or injected context (different prompts for different states).
3. **Does it have a concrete prompt injection?** If you can't write the exact text that goes into the prompt, the rule isn't ready.
4. **Is it a per-turn constraint?** If the rule requires tracking patterns across multiple turns, it belongs in the stagnation detection system, not in prompt rules.
5. **Is it distinct from existing rules?** Check whether the failure mode is already covered or is better addressed as an extension of an existing rule.
