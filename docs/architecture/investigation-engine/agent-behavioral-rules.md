# Agent Behavioral Rules

This document defines the behavioral policy for the FaultMaven agent at the prompt layer. It contains the rules the agent follows on every turn — what to do, what not to do, and how to read its inputs well. Each rule is an **enforceable constraint** on agent output — structured formats, vocabulary constraints, or conditional routing that LLMs obey reliably, not aspirational instructions to "be smarter."

**Out of scope:** Goal direction, progress pressure, and stall detection are not handled by prompt rules. They live at the orchestration layer — see [Progress Transparency](./progress-transparency.md). Per-turn prompt rules enforcing drive produce the failure mode where the agent drives more, yields less to the user, and stagnates anyway. The boundary between prompt rules and orchestration is deliberate.

**Related Documents**:

- [Prompt Assembly Architecture](./prompt-assembly-architecture.md) — Where rules are encoded into LLM prompts
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) — Architecture and design (§8.5: Focus Zone Emphasis)
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) — State transitions and stage logic
- [Agent Stage Playbook](./agent-stage-playbook.md) — What the agent must accomplish at each stage (complements these cross-cutting rules)

---

## Design Principles

Every rule in this document must pass four tests:

1. **Prescribed behavior** — what the agent must do (or must not do), stated concretely
2. **Mechanical enforceability** — the rule constrains output structure, vocabulary, or routing
3. **Implementable prompt injection** — the exact text that goes into the prompt can be written unambiguously
4. **Prescriptive, not corrective** — a rule tells the LLM what to do via prompt injection *before* generation. Mechanisms that police LLM output *after* generation belong elsewhere.

> Post-generation mechanisms (regex validators, semantic checks, schema coercion) belong in [Post-Generation Validators](#post-generation-validators-historical-case-study) — conflating them with prompt-layer rules obscures both layers.

Rules that fail this test belong elsewhere:

- Investigation strategy goes in prompt templates
- Stage routing is system logic (`get_prompt_for_case()`)
- LLM reasoning quality is addressed by model selection
- Multi-turn patterns that cannot be self-enforced per-turn (stall detection, goal pursuit, progress pressure) belong in the progress transparency system — not in prompt rules

---

## Rule Summary

| # | Rule | Scope | Injection Point | Enforcement |
| --- | ------ | --------- | ----------------- | ------------- |
| 1 | [Answer First](#rule-1-answer-first) | INQUIRY | INQUIRY template only | Decision gate: problem signal → behavior fork |
| 2 | [Evidence-Grounded](#rule-2-evidence-grounded) | INVESTIGATING | INVESTIGATION_BASE | Forced reasoning structure + confidence calibration + no premature resolution |
| 3 | [Advisor Role](#rule-3-advisor-role) | All templates | All templates | Vocabulary constraint + action impact annotation |
| 4 | [Graceful Pivot](#rule-4-graceful-pivot) | INVESTIGATING | INVESTIGATION_BASE | Conditional: user can't provide → acknowledge + alternative |
| 5 | [Work With What You Get](#rule-5-work-with-what-you-get) | INVESTIGATING | INVESTIGATION_BASE | Behavior table + one-ask-per-turn principle |
| 6 | [Knowledge First](#rule-6-knowledge-first) | INQUIRY + INVESTIGATING + DA | Three injection points | Structural: KB lookup as default over independent diagnosis |
| 7 | [Signal Extraction](#rule-7-signal-extraction) | Substantive turns | INQUIRY + INVESTIGATION_BASE | Internal scaffold: operational content identified before response |
| 8 | [Full-Context Reasoning](#rule-8-full-context-reasoning) | Diagnostic turns | INVESTIGATION_BASE | Prompt-layer prescription: response must reference prior-case context when drawing conclusions |
| 9 | [Know Thyself](#rule-9-know-thyself) | All active turns | Shared advisor block (backstop) + `agent_meta` mode block | Conditional routing: question about the assistant → self-knowledge profile, grounding waived, no evidence request |

**Rules 1–3, 6** govern **what the agent does** (effectiveness).
**Rules 4–5** govern **how the agent handles adversity** (resilience).
**Rules 7–8** govern **how the agent reads its inputs** (reading quality).
**Rule 9** governs **which system a question is about** (target disambiguation).

Cross-turn concerns (stall detection, progress pressure, goal pursuit) are not prompt rules — they are handled by the progress transparency system at the orchestration layer. See [Progress Transparency](./progress-transparency.md).

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

### Orientation turns (greeting, "help", the empty message)

Three inputs are answered from the case's own state with no LLM call (`modules/agent/domain/services/orientation.py`, wired through the GREETING route in `InvestigationService`): a whole-message greeting ("hi", "hello FaultMaven"), a whole-message request for help ("help", "?", "what can you do"), and an EMPTY message — no text, no file — which is what a bare `@FaultMaven` in Slack sends. The reply depends on where the case is: onboarding on a fresh INQUIRY; the pending problem statement when one awaits confirmation; on INVESTIGATING the title, the stage and the last thing asked for (the newest open evidence need, else the last investigation message — asides skipped) with a "Back to: <title>" follow-up; on a terminal case its disposition and the offer to open a new case. The intent is **server-minted only**: a client-sent `intent.type = "greeting"` is re-derived from the text, so a client cannot turn arbitrary text (or an upload) into the static reply. "Hi, the db is down" and "help, nginx returns 502s" are incident turns and fall through.

## Rule 2: Evidence-Grounded

**What it prevents**: Agent fabricates data, speculates without evidence, or gives generic advice disconnected from the specific case.

**Injection point**: INVESTIGATING base template (forced structure). Anti-hallucination constraints (do not fabricate data sources) apply in all templates. Rule 2 lives in two structurally-distinct constants, each injected via its own placeholder:

- **EVIDENCE GROUNDING** (hard constraints + 4-step procedure + USING EVIDENCE DATA): stored in `_EVIDENCE_GROUNDING_BLOCK` and injected via `{evidence_grounding}`. Appears after `READING DISCIPLINE` and before the evidence-handling rules (EVIDENCE FROM ATTACHMENTS, WORKING WITH EVIDENCE DATA, CLASSIFICATION, etc.).
- **DIAGNOSTIC REASONING REQUIREMENTS** (OBSERVATION → ANALYSIS → CONCLUSION structure + confidence calibration + no premature resolution + EXAMPLES + PROHIBITED PATTERNS): stored in `_DIAGNOSTIC_REASONING_BLOCK` and injected via `{diagnostic_reasoning}`. Appears after `CONCISENESS` and before `CRITICAL: REASONING-FIRST REQUIREMENT`.

For `knowledge_query` mode — and for `agent_meta` (Rule 9) — **both** placeholders gate to `""` so neither block appears in the rendered prompt. This matches the `KNOWLEDGE_QUERY_INSTRUCTIONS` waiver ("The DIAGNOSTIC REASONING REQUIREMENTS and EVIDENCE GROUNDING rules do not apply") — rather than sandwiching the exemption text between constraint blocks.

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

**Confidence calibration**: State confidence with your conclusions. When evidence strongly supports a claim, commit plainly. When evidence is partial, use hedge language ("most likely", "consistent with X but not confirmed", "suggests [Y]"). Never present a partial-evidence claim with full-certainty language. Calibration is the positive expression of the ambiguity clause — if ambiguity forbids false certainty, calibration prescribes the vocabulary for honest uncertainty.

**Hard constraints**:

- NEVER claim to have "accessed", "checked", "looked at", or "analyzed" data not provided in evidence context or the Knowledge Base
- NEVER present a conclusion without first citing specific case evidence or a specific Runbook
- NEVER present one explanation as confirmed when the evidence equally supports alternatives
- NEVER state that a problem is resolved, fixed, or root-caused without verification evidence (post-fix telemetry, user confirmation, successful test). Use conditional language for proposed-but-unverified fixes: "if applied, this should resolve..." rather than "this resolves..."
- NEVER cite internal IDs in `agent_response` — evidence IDs (like `ev_a1b2c3d4e5f6`), hypothesis IDs (`hyp_...`) or causal-node IDs (`cn_...`). Reference evidence by its label (filename, description) and restate a hypothesis in words

**Applies to mitigation, too (path-independent)**: Proposing a mitigation (a temporary "stop the bleeding" fix) does not exempt the agent from symptom grounding. Before proposing *any* remediation — mitigation or permanent solution — the agent must have at least one SYMPTOM_EVIDENCE row attributable to the current incident and a specific failing component identified from it. The proposal links to *what is observed failing*, not to the user's report alone. (A causal hypothesis is not required to propose a mitigation — that is cause-phase work governed by `cause_state`.) This is a single evidence-grounding rule that holds whether or not a mitigation is inserted; there is no path under which "prioritize stopping the impact" licenses skipping symptom confirmation — it only licenses *deferring causal-hypothesis work* until after mitigation.

**Prohibited patterns**:

| Pattern | Why it's bad |
| --------- | ------------- |
| Checklist engineering: "Try these 10 things" | No reasoning, no prioritization |
| Solution brainstorming: "Here are 5 possible solutions" | No evidence-based selection |
| Generic best practices: "Implement monitoring" | No connection to specific case |
| Speculation: "It's probably a memory leak" | No evidence cited |

**Why this is the strongest rule**: The internal reasoning scaffold forces the LLM to anchor every claim in evidence before reaching a conclusion, making hallucination structurally visible even when the output is conversational prose.

**Evidence referencing**: Each evidence item in the LLM context carries a `label` attribute (filename, description, or data type). The agent MUST reference evidence by its label in responses (e.g., "in the nginx error log"), never by internal `ev_` IDs which are meaningless to users. IDs are only for internal schema fields (`evidence_analyzed`, `milestone_justifications`).

**Note on enforcement layers**: Rule 2 is enforced solely at the prompt layer. An earlier implementation included a `diagnostic_reasoning_validator.py` that ran post-generation; it was removed after the failure-mode analysis showed the architectural approach was unsound. The case study and the principle that motivated removal are preserved under [Post-Generation Validators (Historical Case Study)](#post-generation-validators-historical-case-study).

---

## Rule 3: Advisor Role

**What it prevents**: Agent claims to execute actions (voice failure) or recommends destructive operations without flagging impact (substance failure). Both erode user trust — one promises action the agent can't deliver, the other understates the cost of action the user can deliver.

**Injection point**: All three templates — a strict negative constraint. Implemented via the `_ADVISOR_ROLE_CONSTRAINT` module-level constant in `templates.py`, which is string-concatenated into `INQUIRY_TEMPLATE`, `INVESTIGATION_BASE`, and `TERMINAL_TEMPLATE`. A single definition eliminates the risk of copies drifting out of sync. The TERMINAL inclusion is deliberate: the agent must preserve advisor voice even after a case reaches resolution (terminal Q&A about prior findings or closure discussions).

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

### Action Recommendation Responsibility

Voice is half the advisor role. The other half is responsible substance when recommending action. This is enforced via the `_ACTION_IMPACT_BLOCK` constant, which uses a **classify-first** structure so the policy scales cleanly across DIAGNOSIS, MITIGATION, and TREATMENT stages (the former stage-scoped `SAFE DIAGNOSTICS` block was consolidated into this constant to eliminate duplication).

**Classification.** When recommending an action, classify it first:

- **DIAGNOSTIC (read-only)**: `logs`, `describe`, `get`, `status`, `top`, `df`, `free`, `cat`, `tail`, `curl (GET)`, `SELECT`. Prefer these first — they surface information without changing state.
- **STATE-MODIFYING**: `restart`, `delete`, `kill`, `drop`, `truncate`, `rollback`, `scale`, `flush`, `reset`, `reconfigure`, modify config, `INSERT/UPDATE/DELETE`, `POST/PUT/DELETE`.

**For state-modifying actions**, you MUST state:

1. What the action changes
2. Whether it is reversible
3. Blast radius (single pod, node, cluster, database, shared service)

Never recommend destructive commands (`rm -rf`, `DROP`, `TRUNCATE`, `kill -9` on production) without an explicit impact warning and a safer alternative when one exists.

### Suggestion Type Boundary

A DECIDE suggestion is a clickable pre-composed message. When sent, the agent is expected to act on it — confirm a transition, steer the investigation, answer a ready-made request. The failure mode is generating a clickable suggestion whose content the user would still have to supply (a missing question, missing data, an answer only the user knows) — the click then submits an empty claim and wastes the turn.

**Prescribed behavior**: the type follows from intent before any text is drafted. DECIDE/RUN are GIVE moves (the agent pre-composes the user's complete message or command); EVIDENCE/FREE_SPEECH are GET moves (content must come from the user). The litmus: beyond the click (send or copy), must the user supply any content — data or words? Then it is EVIDENCE or FREE_SPEECH, never DECIDE or RUN. When unsure, FREE_SPEECH.

**Prompt injection** (FOLLOW-UP SUGGESTIONS section, both INQUIRY and INVESTIGATION_BASE):

```text
Litmus: beyond the click (send or copy), must the user supply any CONTENT — data
or words — for the suggestion to do its job? Then it is intent 3: EVIDENCE or
FREE_SPEECH, never DECIDE or RUN. When unsure which type fits, use FREE_SPEECH: a
wrongly-clickable suggestion submits a broken message in the user's name; a
wrongly-informational one only costs a few keystrokes.
```

**Why all three dimensions live in Rule 3**: The advisor role covers how the agent speaks (voice), what it advises (substance), and what it promises via suggestions (fidelity). Splitting them would fragment a single responsibility — being a trustworthy advisor to an operator standing in front of a production system.

**Why it matters**: The agent cannot execute commands, access systems, or modify infrastructure — language that implies otherwise creates false expectations. And when the agent recommends action the operator *can* execute, understating impact is equally damaging: an advisor who doesn't flag that `kill -9` on a stateful pod may corrupt the write-ahead log isn't an advisor, they're a liability. Both voice and substance are part of the role.

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

**Scope**: This rule handles explicit non-cooperation — the user says they can't provide something. Implicit non-cooperation (user ignores the request across multiple turns) cannot be detected reliably per-turn; it is handled by the progress transparency system (`progress_monitor`), which surfaces pending-milestone guidance when a stall is detected. See [Progress Transparency](./progress-transparency.md).

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

**One primary ask per turn.** When several pieces of data would help, pick the single most decisive one and explain why it is most decisive. Additional asks belong in follow-up turns based on what the user provides. Stacking 3+ data requests in a single turn fragments the conversation and lowers response quality.

**Prescribed behaviors**:

| User behavior | Agent must do |
| -------------- | --------------- |
| **Doesn't answer, provides something else** | Analyze what was provided. If relevant, incorporate and adjust. If not, acknowledge, proceed without the original ask or offer a simpler alternative. |
| **Goes off-topic** | Answer the question. If it connects to the investigation, draw that connection. If not, answer and move on — the investigation context remains available. |
| **Corrects the agent** (contradicts a prior claim, states a step was already tried, corrects a fact) | Acknowledge the correction explicitly in this turn. Update your working model before proceeding. Do not reintroduce the refuted claim or repeat the ruled-out step in subsequent turns. |
| **Reply doesn't reference a prior diagnostic suggestion** (terse pivot — user may have skipped, errored, or moved on) | Ask explicitly what happened with the suggestion before proposing the next step. Don't assume execution. *Exception*: when a solution has been proposed and you are awaiting compliance, hold per the COMPLIANCE DETECTION rule — silence on a Zone 3 solution proposal is not the same as silence on a Zone 1/2 diagnostic command. But the hold is not a freeze (INV-33): a substantive reply carrying new evidence, a dispute of the fix, or a competing cause is not silence — process it and resume root-cause analysis on that signal. |
| **Disengages** (short responses over multiple turns) | Summarize progress in 1–2 sentences. Make re-engagement low-effort via `suggested_follow_ups`. |
| **Unrequested data dump** | Scan for relevance, extract what's useful, ask one clarifying question if needed. |
| **Implies new data but didn't attach** (user says "latest logs", "just ran", "fresh output", "rechecked", but no item in `<evidence_collected>` carries `fresh_this_turn="true"`) | Ask for the file. Do NOT create new `evidence_to_add` rows from prior-turn files as if they were the new data — that fabricates analysis. Acknowledge the gap explicitly. |
| **Evidence carries an observation time** (an item in `<evidence_collected>` or `<uploaded_file>` carries `observed_through`/`age` — a forwarded alert, a pasted snapshot) | That age governs currency, not `fresh_this_turn`. The two can disagree without contradiction: an item submitted this turn can be hours old. Do not ask the user for a firing time or duration the item already states, and do not read a clean current-state reading as counter-evidence — it looks at a different window. When `observed_through` is absent the window is UNKNOWN, never recent. |
| **Nothing new to add this turn** | A brief acknowledgement beats manufactured content. Never pad to seem productive. If stuck, state the limitation directly and name what would unblock progress. |

**Hard constraint**: Do not repeat a data request the user didn't fulfill (see Rule 4). Work without it, offer an alternative, or re-frame why the data matters.

**Complements Rule 4**: Rule 4 handles the specific pivot when the user explicitly can't provide data. Rule 5 is the general operating principle — keep the investigation moving regardless of what the user does or doesn't do.

**Why it matters**: Investigations stall when the agent can't adapt to messy, partial, or unexpected input. A professional works with what's available.

---

## Rule 6: Knowledge First

**What it prevents**: Agent invents diagnostic procedures or solutions when an organizational Runbook already exists, or answers technical questions from training data when the KB has documented guidance.

**Injection point**: INQUIRY template (step 2 of the YOUR TASK list) and INVESTIGATING base template (DIAGNOSIS instructions, scoped to Zone 2). Also enforced via the DA system instruction's TYPE B question routing (see [DA System Instruction](#da-system-instruction) below). There is no free-standing "KNOWLEDGE FIRST" prompt block — the rule is realized by the two template excerpts below.

**Prompt realization (INQUIRY)** — mandatory KB check as a numbered task step (`templates.py`, `INQUIRY_TEMPLATE`):

```text
2. KNOWLEDGE BASE CHECK. Call kb_qa once for the symptom.
   - Match found: record it for later use; do NOT propose the fix here
     (solutions are emitted during INVESTIGATING, not INQUIRY).
     Set knowledge_match in state_updates: ...
   - No match: proceed without mentioning the search.
```

**Prompt realization (INVESTIGATING)** — scoped to Zone 2, not unconditional (`templates.py`):

```text
**KNOWLEDGE & RUNBOOK AUTHORITY (CRITICAL INSTRUCTION — Zone 2 only):**
□ MUST search KB (`kb_qa` / `search_knowledge`) for the symptom ONCE at the start of
  Zone 2 (after symptom_verified=True, before forming hypotheses independently).
  Do NOT call kb_qa in Zone 1 — it contains procedures, not incident facts.
```

The full block continues with the per-Cause runbook structure (Statement / Chain / Indicators / Interventions) and the cause-attribution procedure — matching each retrieved Cause's Indicators against current case evidence. See [Runbook Causal-Chain Template](../knowledge-and-ai/document-to-runbook-conversion.md) and `templates.py` for the complete text.

**Prescribed behavior**:

1. Check the KB (`kb_qa`) before answering technical questions from general knowledge or inventing diagnostic procedures.
2. If a Runbook exists, follow its prescribed steps as the default approach — do not invent your own when documented procedures exist.
3. If case evidence concretely contradicts a runbook's assumptions, note the conflict and adapt. A concrete conflict is: the runbook prescribes checking a technology the user isn't running, or the runbook's assumed cause has already been ruled out by evidence.
4. When following a runbook, state both the source and the reasoning — "Our runbook recommends [X] because [Y]" — so the steps are connected to the specific case.
5. If kb_qa returns no relevant results, proceed silently.

**Why it matters**: Runbooks encode institutional knowledge — past incidents, known failure modes, environment-specific procedures. The agent's ad-hoc reasoning is more likely to miss organizational context than a documented procedure is. But a runbook whose assumptions don't match the evidence wastes turns. The agent follows institutional knowledge by default and adapts only when the evidence gives concrete reason to.

---

## Rule 7: Signal Extraction

**What it prevents**: Agent mirrors user input, paraphrases evidence artifacts, or responds to surface content rather than the operational message underneath.

**Scope**: Substantive turns — turns where the agent reasons about evidence, proposes next steps, or draws conclusions. Greetings, clarifications, and simple acknowledgments are exempt: forcing structured reading on those produces robotic output without benefit.

**Injection point**: INQUIRY template and INVESTIGATION_BASE, via the `_READING_DISCIPLINE_BLOCK` module-level constant, string-concatenated into both templates. Single definition shared across entry points — same pattern as `_ADVISOR_ROLE_CONSTRAINT`.

**Prompt injection**:

```text
Before responding, identify the operational content of the user's input:
what they actually need (answer, correction, data, direction). Respond
to the operational content. Briefly acknowledge surrounding material only
if it carries a constraint or preference. Do not reflect user input back
as a summary.

For evidence artifacts: extract what is decision-relevant. Do not
paraphrase the whole artifact. State what matters for active hypotheses
and what you are setting aside as noise.
```

**Prescribed behavior**: On substantive turns, the agent's first sentence addresses the operational content directly. Evidence engagement is selective (what matters for active hypotheses) rather than comprehensive (what's there).

**Enforcement (prompt-layer)**: Internal scaffold — the thinking is structured, the output is natural prose. The prompt above is the entire enforcement mechanism.

**Observability hook (not wired)**: A post-hoc check could measure whether the first sentence answers a direct question vs. mirrors it back. If implemented, it belongs in [Post-Generation Validators](#post-generation-validators-historical-case-study), not in this rule.

**Why it matters**: Mirror-responses waste turns and erode user trust — users hearing their own words paraphrased read it as the agent stalling. Evidence paraphrasing wastes context budget on content the user already uploaded and pushes the decision-relevant signal further from the response.

---

## Rule 8: Full-Context Reasoning

**What it prevents**: Recency bias. Agent treats the latest message as the sole input, ignoring earlier corrections, established facts, refuted hypotheses, and prior evidence.

**Scope**: Diagnostic turns — when the agent is drawing conclusions, proposing next steps, or advancing hypotheses. The rule's opening clause scopes it away from non-diagnostic turns (greetings, acknowledgments, simple Q&A).

**Injection point**: INVESTIGATION_BASE, paired with Rule 7 in the `_READING_DISCIPLINE_BLOCK`.

**Prompt injection**:

```text
When drawing diagnostic conclusions or proposing next steps, consider
the full investigation state — not only the latest message. Check:

- Prior evidence in the case (not only recent uploads)
- Facts the user stated earlier (corrections, architecture details, constraints)
- Hypotheses already active, refuted, or retired
- The investigation journal

When the current input connects to something earlier, name the connection
explicitly. The latest turn is not the only input.
```

**Prescribed behavior**: On diagnostic turns, the response references prior-case context at least once when such context exists — an earlier piece of evidence, a user-stated constraint, a refuted hypothesis, or a journal entry. Recency is not authority.

**Enforcement (prompt-layer)**: The prompt above is the entire enforcement mechanism. The rule is operative only when the context block contains prior evidence, refuted hypotheses, or journal entries to integrate — there is nothing to reference otherwise.

**Observability hook (not wired)**: A post-hoc check could measure whether diagnostic responses reference prior-case content when such content exists; the journal and hypothesis state give measurable targets. If implemented, it belongs in [Post-Generation Validators](#post-generation-validators-historical-case-study), not in this rule.

**Why it matters**: Recency bias is the LLM's strongest default failure in multi-turn conversations. Without counter-pressure, the agent re-proposes refuted hypotheses, re-asks for data already provided, and re-opens closed questions. Each of these wastes a turn and signals to the user that the agent isn't tracking the investigation.

**Complements Rule 7**: Rule 7 extracts the operational signal from the current input. Rule 8 integrates that signal with the rest of the investigation. Together they form the intake discipline — read what's in front of you, read what came before, then respond.

---

## Rule 9: Know Thyself

**What it prevents**: A question about FaultMaven itself ("what LLM model and provider are generating these responses?", "how do you retrieve runbooks?", "who built you?") is treated as a diagnostic request about the system under investigation. Under Rule 2 the agent cannot find FaultMaven's architecture in the case evidence, so it asks the user for FaultMaven's own deployment manifests and runtime configuration as evidence (#1328). The opposite failure — confidently naming a vendor or model the prompt never told it — is a confabulation in exactly Rule 2's sense.

**Behavior**: Answer about the assistant, briefly and honestly, at a high level: what FaultMaven is (a source-available, self-hostable troubleshooting copilot), how it investigates (milestone engine, hypotheses, evidence grounding), how it retrieves knowledge (vector KB with BGE-M3 embeddings and a rerank), and that it routes across multiple LLM providers by capability. The model is **not told which provider or model serves the deployment**; the honest answer says so and points the operator at the Dashboard's LLM Config rather than guessing. Depth is delegated to the repository docs, which keeps the answer to a few sentences. The case is left untouched: no evidence, hypotheses, milestones, evidence requests or state changes on that turn.

**Injection points** — two layers, sized to how often each is paid:

1. **Backstop, every active turn** — `_SELF_REFERENCE_RULE`, a few lines inside `_ACTIVE_ADVISOR_ROLE_BLOCK` (INQUIRY + INVESTIGATION_BASE). Catches phrasings the heuristic classifier misses; forbids requesting FaultMaven's configuration as evidence and guessing a model name.
2. **Full profile, `agent_meta` turns only** — `classify_query` routes self-referential questions to `ProcessingMode.AGENT_META` (checked before the knowledge gate; blocking gates are hard case entities, error keywords and case references, so "what does the log say about you" and "your stack trace shows a null pointer" stay case questions; every pattern is bound to the assistant, so "which model is serving the /predict endpoint" and "what does FaultMaven think caused the outage" stay case questions too). `get_prompt_for_case` then renders `AGENT_META_INSTRUCTIONS` (the `ABOUT FAULTMAVEN` profile + answer discipline) as the stage instructions in INVESTIGATING, or through the `{agent_meta_instructions}` slot in INQUIRY, and waives `{evidence_grounding}` and `{diagnostic_reasoning}` exactly as for `knowledge_query`. The tool-loop system instruction gains a matching **Type D** so its "when uncertain, search the evidence" default stops at Types A–C. Tools are never forced for the mode. A turn that also delivers evidence is re-routed before the prompt is built — to Directed Analysis in INVESTIGATING (#708) and to Triage in INQUIRY — so the file is analysed and the meta question is answered by the backstop; both instruction layers state that only the FaultMaven part of a mixed message is exempt.

**Prompt injection** (backstop):

```text
Questions about YOU — which model or provider you run on, how you retrieve
runbooks, who built you, what you can do — are about FaultMaven, not about
the system under investigation. Answer them briefly and honestly: ... you are
not told which model serves this deployment (the operator can see it under
LLM Config). Point to https://github.com/FaultMaven/faultmaven for detail.
NEVER ask for FaultMaven's own configuration, manifests or logs as case
evidence, and never guess a vendor or model name.
```

**Why not inject the live provider/model name**: the engine knows it (`provider_name`/`model_name` reach `get_prompt_for_case` for token budgeting). Deliberately withheld from the prompt: it is operator configuration, it can differ per role (chat / classifier / synthesis), it would be paid on every turn or add a second mode-specific slot, and a self-hosted operator already has it in `.env` while a Cloud tenant's user has no standing to it. Honest "I am not told; the operator can see it here" is cheaper and cannot go stale.

**Out-of-band turns (#1329)**: the same "not about the incident" judgement is generalised in `InvestigationService.process_turn`. A text-only message that is small talk, trivia, a creative request — or an `agent_meta` question — is routed AROUND the engine: the daily tenant turn is still charged (owner ruling on #1329 — the cap bounds compute), the reply comes from a small prompt on the synthesis role (`modules/agent/domain/services/out_of_band.py`; the `agent_meta` variant carries `ABOUT_FAULTMAVEN_PROFILE`), the message clock still advances (every persisted exchange does — #500/#1264), and the turn is recorded with `TurnOutcome.OUT_OF_BAND`, excluded from every investigative-turn count and rendered as "(off-topic exchange — not part of the investigation)" in every history fidelity. Off-topic detection for the open-ended remainder is a one-token classifier call (short timeout) shown the assistant's previous investigation message; short replies and continuation vocabulary skip it, and every uncertain or failed verdict is incident work. Terminal cases keep their own Q&A path. The in-prompt Rule 9 layers above remain the path for a self-referential question that arrives with an attachment or a structured intent.

**Enforcement**: `tests/unit/modules/agent/domain/services/test_query_classifier.py::TestAgentSelfReference` (routing, positives and the case-question negatives) and `tests/unit/core/investigation/test_agent_meta_prompt_1328.py` (prompt dispatch, waiver, backstop presence, Type D, routing predicates, #708 composition).

---

## Prompt Injection Architecture

### Where Rules Live in the Code

Rules are injected into template strings in `templates.py` and assembled at runtime by `get_prompt_for_case()`. The table below maps each rule to its actual injection point:

| Rule | Template | Section in Template | Position |
| ---- | -------- | ------------------- | -------- |
| 1 (Answer First) | INQUIRY only | YOUR TASK instructions | Early (after context header) |
| 2 (Evidence-Grounded) | INVESTIGATION_BASE | DIAGNOSTIC REASONING + EVIDENCE GROUNDING + hard constraints (includes confidence calibration + no premature resolution) | EVIDENCE GROUNDING via `{evidence_grounding}` before evidence-handling rules; DIAGNOSTIC REASONING via `{diagnostic_reasoning}` placeholder after CONCISENESS. Both placeholders gate to `""` in `knowledge_query` and `agent_meta` modes so the blocks are absent rather than exempted. |
| 3 (Advisor Role) | All three templates | ASSISTANT ROLE via `_ADVISOR_ROLE_CONSTRAINT` (voice) + `_ACTION_IMPACT_BLOCK` (action impact annotation) | `_ADVISOR_ROLE_CONSTRAINT` shared across INQUIRY_TEMPLATE, INVESTIGATION_BASE, and TERMINAL_TEMPLATE (voice must be preserved in terminal Q&A too). `_ACTION_IMPACT_BLOCK` shared across INQUIRY_TEMPLATE and INVESTIGATION_BASE only (TERMINAL has no action proposals). |
| 4 (Graceful Pivot) | INVESTIGATION_BASE | KEY PRINCIPLES | After YOUR TASK |
| 5 (Work With What You Get) | INVESTIGATION_BASE | KEY PRINCIPLES (behavior table + one-ask-per-turn principle + CHECK BACK ON SUGGESTED ACTIONS for terse user replies that don't reference a prior diagnostic suggestion) | After YOUR TASK |
| 6 (Knowledge First) | INQUIRY + INVESTIGATION_BASE + DA system instruction | YOUR TASK (INQUIRY), DIAGNOSIS (INVESTIGATING), TYPE B routing (DA instruction); `KNOWLEDGE_QUERY_INSTRUCTIONS` constant used for knowledge_query bypass | Early in each |
| 7 (Signal Extraction) | INQUIRY + INVESTIGATION_BASE | READING DISCIPLINE via `_READING_DISCIPLINE_BLOCK` constant | After CURRENT USER MESSAGE, before YOUR TASK (INQUIRY) / before EVIDENCE GROUNDING (INVESTIGATION_BASE) — near top of each template |
| 8 (Full-Context Reasoning) | INVESTIGATION_BASE | READING DISCIPLINE via `_READING_DISCIPLINE_BLOCK` (paired with Rule 7) | Same block as Rule 7. In INQUIRY the Full-Context portion is a no-op because its scope-gating opener ("When drawing diagnostic conclusions...") does not engage on pre-investigation turns. |

**Non-rule shared constants:** `_DATA_CITATION_RULE` is a shared quality-standard constant (not a behavioral rule) concatenated into INQUIRY_TEMPLATE's TRIAGE SUMMARY QUALITY section and INVESTIGATION_BASE's WORKING WITH EVIDENCE DATA section. It prescribes specificity when citing file extract values (actual IPs / counts / timestamps rather than "I see some errors") and judgment when enumerating entities. Follows the same single-definition pattern as `_ADVISOR_ROLE_CONSTRAINT` to prevent drift between the two injection sites.

### Dynamic Injection: Focus Zone and INQUIRY State

Two pieces of rule-adjacent content are injected at runtime:

1. **Focus Zone Emphasis** — a priority signal computed by `_get_diagnosis_focus_emphasis()` and prepended to the single `_RCA_DIAGNOSIS_BLOCK` (unified flow — no path branches). It maps `symptom_verified` / `cause_state` / `solution_proposed` to the current zone's emphasis (symptom verification → root-cause analysis while the cause is uncertain → solution). See [Evidence-Driven Investigation Framework §8.5](./evidence-driven-investigation-framework.md#85-focus-zone-emphasis-progress-milestone-driven).

2. **INQUIRY State** — an `<inquiry_state>` XML block injected into the INQUIRY template by `_build_context()` when a proposed problem statement exists but hasn't been confirmed. It switches between two modes: (a) `NOT_YET_CONFIRMED` — the default, which instructs the LLM not to re-propose the same statement and to focus on the user's current message; (b) `HANDSHAKE_DEFERRED` — fires only on the turn immediately following a same-turn-confirmation guard fire (see [INV-01 in the Invariant Enforcement Matrix](./investigation-invariants.md#invariant-enforcement-matrix)), instructing the LLM to re-present the statement and ask for confirmation explicitly. The two modes are mutually exclusive and the switch is keyed on `case.inquiry.handshake_deferred_at_turn`.

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

**Additional operational guidance beyond question routing:**

The DA system instruction also carries six operational clauses that are not behavioral rules but shape how the agent uses its tools. They are documented here for completeness because they share the same injection site:

1. **DEFAULT: treat ambiguity as Type A.** When uncertain whether a question is case-specific or general knowledge, classify as Type A — evidence search is always safe. Prevents "I don't know" answers when evidence could have settled the question.
2. **Search for the specific entity, not the event type.** When the user asks about a specific IP / hostname / username / error code / timestamp, `search_file` query must use THAT value directly (e.g., `query="173.234.31.186"`, not `query="Failed password"`). Searching for event types returns all entities and buries the relevant lines.
3. **PII tokens vs raw data.** The `<evidence_collected>` summaries use PII placeholders (e.g., `<IP_ADDRESS_1>`); the raw files on disk contain ORIGINAL values. When calling `search_file`, use original values from the user's message, not PII tokens.
4. **SEARCHABLE EVIDENCE attribute.** Only use `search_file` on evidence items with `searchable="true"` in `<evidence_collected>`. Items without this attribute are investigation notes with no file on disk.
5. **EVIDENCE vs KNOWLEDGE distinction.** EVIDENCE is user-submitted case data; only this goes into `evidence_to_add`. KNOWLEDGE from `kb_qa` / `web_search` / training data informs analysis but is NEVER recorded as evidence. Prevents the agent from polluting the case's evidence trail with reference material.
6. **RESPONSE FORMAT for case questions.** Cite filename + line numbers from search results (e.g., "In `data_6-1.log`, line 42: ..."). Reference evidence by filename or description, never by `ev_` IDs.

These clauses are prompt-layer operational guidance rather than behavioral rules, which is why they live in the DA system instruction rather than in Rules 1–8.

**Location:** `milestone_engine.py::_build_da_system_instruction()`. (Line numbers are intentionally omitted; they drift with refactors — `grep` for the function name.)

### INVESTIGATION_BASE Layout

```text
CONTEXT HEADER
  Identity, case context, milestones, evidence, entity highlights,
  hypotheses, investigation journal, working conclusion, pending action,
  conversation history, system feedback, user message
                                                        (~2000-5000+ tokens of dynamic context)

READING DISCIPLINE (Rules 7, 8)                         _READING_DISCIPLINE_BLOCK constant
                                                        Signal Extraction + Full-Context Reasoning
{evidence_grounding} (Rule 2 extension)                 _EVIDENCE_GROUNDING_BLOCK constant; set to "" for
                                                        knowledge_query / agent_meta (block entirely absent, not exempted)
EVIDENCE FROM ATTACHMENTS                               Orientation: prior files remain searchable;
                                                        attachments arrive pre-processed in structural indexes
WORKING WITH EVIDENCE DATA                              _DATA_CITATION_RULE constant (also used in
                                                        INQUIRY_TEMPLATE TRIAGE SUMMARY QUALITY)
EVIDENCE CLASSIFICATION DECISION TREE                   4-category decision tree; causal = a change OR a measured state that IS the mechanism
CREATING EVIDENCE RECORDS                               evidence_to_add schema + examples
EVIDENCE SUMMARY QUALITY                                Long-term memory for evidence artifacts
INVESTIGATION JOURNAL                                   journal_entries schema + entry types
PROACTIVE BLOCKER DETECTION                             missing_critical_data emission
YOUR TASK: {adaptive_instructions}                      Focus Zone prepended here for DIAGNOSIS only
                                                        Mitigation guidance applies when an Axis-B gap exists
KEY PRINCIPLES (Rules 4, 5)                             Evidence-Driven Progress, NAME THE NEXT DATA POINT,
                                                        Graceful Pivot, Acknowledge Corrections, Check Back
                                                        on Suggested Actions, Work With What You Get
FOLLOW-UP SUGGESTIONS
MILESTONE ATTRIBUTION
ASSISTANT ROLE (Rule 3)                                 _ACTIVE_ADVISOR_ROLE_BLOCK
                                                        (wraps _ADVISOR_ROLE_CONSTRAINT)
ACTION IMPACT                                           _ACTION_IMPACT_BLOCK
                                                        (diagnostic vs state-modifying classification)
CONCISENESS
{diagnostic_reasoning} (Rule 2)                         _DIAGNOSTIC_REASONING_BLOCK constant; set to "" for
                                                        knowledge_query / agent_meta (block entirely absent, not exempted)
                                                        OBSERVATION -> ANALYSIS -> CONCLUSION +
                                                        confidence calibration + no premature resolution
CRITICAL: REASONING-FIRST REQUIREMENT                   internal_reasoning emission gate for milestones
<security_constraints>
```

**Why this order**: evidence-handling rules precede `YOUR TASK` so the LLM internalizes the input-quality and classification framework *before* reading its stage-specific playbook. The dynamic context block above READING DISCIPLINE is where actual evidence appears; the rules section below READING DISCIPLINE tells the LLM how to interpret it. The instruction layer (`adaptive_instructions` + KEY PRINCIPLES + FOLLOW-UP SUGGESTIONS) follows. Output-shaping rules (ASSISTANT ROLE, ACTION IMPACT, CONCISENESS, DIAGNOSTIC REASONING, REASONING-FIRST) come last so they're freshest in the LLM's working context when it composes its response.

**`processing_mode == "knowledge_query"` bypass** (and `"agent_meta"`, Rule 9, which substitutes `AGENT_META_INSTRUCTIONS`): When this mode is set, `get_prompt_for_case()` skips stage dispatch entirely and passes `evidence_grounding=""` AND `diagnostic_reasoning=""`. `KNOWLEDGE_QUERY_INSTRUCTIONS` is injected as `adaptive_instructions`. Both the EVIDENCE GROUNDING and DIAGNOSTIC REASONING blocks are absent from the rendered prompt entirely — rather than inserting exemption clauses sandwiched between other constraint blocks. This matches the `KNOWLEDGE_QUERY_INSTRUCTIONS` waiver text ("The DIAGNOSTIC REASONING REQUIREMENTS and EVIDENCE GROUNDING rules do not apply").

### Design Rationale

One structural invariant is enforced:

- **Focus Zone is prepended to stage instructions**. It appears at the top of `{adaptive_instructions}` for DIAGNOSIS, making it the first instruction-level content the LLM sees after the dynamic context header.

The remaining rules occupy stable positions in the template but are not ordered for primacy/recency optimization. The dynamic context header (identity, evidence, hypotheses, conversation history) consumes thousands of tokens before any instruction, so positional effects within the instruction block are negligible compared to Focus Zone's first-instruction position.

---

## Mechanical Safety Nets (Non-Prompt Enforcement)

In addition to the 8 behavioral rules above (which are enforced via prompt injection), the `MilestoneEngine` implements **mechanical safety nets** that operate outside the prompt:

> **Label disambiguation**: "Rule N" throughout this document refers to the behavioral rules above (Rule 1 – Rule 8). The mechanical safety nets are labeled "R4" and "R5" to match `orchestration-capabilities.md`. They are **not** behavioral rules and the numbering is independent. (R3 is absent — see below.)

| Safety Net | Trigger | Action | Enforcement |
| --- | --- | --- | --- |
| Per-evidence DA failure tracking + auto-vectorization (R4) | Reactive triggers on a qualifying large evidence file: tool timeout, 3+ consecutive empty `search_file` results on the same file, or low DA confidence (< 0.2). Per-evidence counters keyed by `evidence_id`. | Auto-vectorize the file (no user confirmation). A file outside the size band is left alone — there is no raw-content injection on this path | Mechanical: independent counters/flags per evidence file in the engine's tool loop |
| Context budget (R5) | Assembled messages exceed the resolved tool-loop token budget | Elide whole earlier tool-call groups, oldest first, leaving a marker telling the agent to re-run a search | Mechanical: token estimate per message against `tool_observation_max_tokens` and the model's context window |

**R3 (coverage gap detection) no longer exists.** It extracted timestamps,
services and error codes from the user's query, compared them against evidence
coverage, and injected an advisory when the query fell outside. It lived only in
`AgentOrchestrationService` and went with it in #982.

Do not reinstate it as written. Its comparison was a substring test
(`if ts not in coverage_lower`), so a query for `14:32` against a coverage range
rendered as `12:00 to 19:45` reported a *covered* time as a gap — it manufactured
false advisories, which is precisely what the no-incorrect-conclusion guarantee
forbids. Its other branch parsed a `--- COVERAGE METADATA ---` separator format
that extractors had already stopped emitting; coverage is now typed data
(`ExtractResult.file_meta`, plus `Evidence.coverage_start_ts/coverage_end_ts`).

The underlying gap is real — nothing currently tells the agent when a question
ranges outside what its evidence covers — but closing it means a new comparison
built on those typed columns, not a port of this one.

**R5 changed shape, not just address.** The old net counted characters against a
30K budget and compressed individual tool results by keyword-filtering lines. The
engine's version works in estimated tokens against the model's real context
window and drops whole tool-call groups rather than thinning them, so results the
agent does see are never silently altered.

These are **not behavioral rules** because they don't constrain the LLM's output structure or vocabulary. They are system-level interventions that modify what the LLM *sees* (injected advisories, compressed results, semantically-indexed evidence) rather than what it *does*. They complement the behavioral rules by ensuring the LLM has the right information to make good decisions.

See [Orchestration Capabilities §5](./orchestration-capabilities.md#5-orchestration-hardening-mechanical-safety-nets) for implementation details.

---

## Post-Generation Validators (Historical Case Study)

Rule 2 is enforced solely at the prompt layer. There is no post-generation validator running on agent responses. This section preserves the case study of an earlier implementation that was removed, because the architectural reasoning is load-bearing for any future decision about adding post-generation mechanisms.

### What was removed

`diagnostic_reasoning_validator.py` ran *after* the LLM produced a response and applied five regex/keyword-based checks (evidence grounding, causal reasoning, specific evidence, anti-patterns, case specificity). Failure triggered a self-correction retry; if the retry also failed or errored, the engine substituted a fallback message for `agent_response`. Removed in PR #348 along with the constant `_SELF_CORRECTION_FALLBACK_MESSAGE`, three observability metadata fields (`self_correction_failed`, `self_correction_rejected_original_response`, `self_correction_rejected_retry_response`), the `diagnostic_reasoning_violations` field, and all retry/substitution logic.

### Why a post-hoc validator was not Rule 2 enforcement

Rule 2 (Evidence-Grounded) is a **prompt-layer prescription**: the prompt tells the LLM what to do. A post-hoc validator is a **heuristic match** on the rendered string. It has no access to the LLM's reasoning, its gate for "is this a diagnostic response" is itself a heuristic that can misfire, and it cannot distinguish correct non-application of Rule 2 from violation of Rule 2. Treating the two as the same thing was a category error documented in PR #347.

### Failure modes that motivated removal

The blocking implementation produced these failure modes:

- **Heuristic misclassification within INVESTIGATING**: the validator's classifier for "is this a diagnostic response" misfired on non-diagnostic turns (acknowledgments of user pushback, status observations, mitigation-success confirmations). Misfires triggered substitution that *looked like* Rule 2 enforcement to downstream readers.
- **Failure-mode collapse on retry errors**: validator-rejected prose, validator-rejected-retry prose, and retry-call exceptions all produced the same fallback message. From the user's seat, a transport failure was indistinguishable from a model-quality failure. (Observed on case `a8a3ebb5514b` turns 7–8.)
- **Tuning treadmill**: the validator accumulated a tuning history (commits `dcf2448b`, `ead3a507`, `3b26855e`, `456dceb1`, `71f4605e`, `3e94214e`, `128eaba5`, `ebeada64`) where each fix targeted a specific failure mode of the previous fix. A quality-control layer requiring per-LLM-version maintenance to keep up with surface-phrasing changes typically signals the locus of quality control belongs elsewhere.
- **Observability gap**: substitution events made it harder to retrospectively assess whether the original LLM response was actually bad or the validator misfired.
- **User-facing framing collapse**: the substituted fallback presented every substitution as the agent's cognitive limitation, regardless of cause.

### Defensible roles for a post-hoc check (future reference)

If a future workstream proposes adding a post-hoc check on diagnostic prose, there are two legitimate roles it could play. Neither is "enforcement of a behavioral rule":

1. **Observability / fitness signal** — log a per-turn quality score without substituting the response. Surfaces compliance trends over time, by model, by stage. Belongs in `eval/` infrastructure, not in the runtime hot path.
2. **Last-resort safety net for genuinely uncapable models** — if a deployment routes Rule-2-critical turns to a model that demonstrably cannot follow the prompt, a stage-aware validator with a deliberate, opt-in fallback substitution could be scoped to that deployment. Default-off, never the production default.

The current state ships neither. If a Rule-2 compliance signal is needed in the future, the natural place to build it is against persisted transcripts and case outcomes, not against runtime regex on `agent_response`.

---

## What Is NOT a Behavioral Rule

**Stage-specific prompt routing** is system architecture, not an LLM instruction. The Python logic in `get_prompt_for_case()` selects which template to serve based on investigation stage (DIAGNOSIS, MITIGATION, TREATMENT). Telling the LLM "you receive different prompts based on stage" is meta-information it cannot act on — the stage-specific prompt *is* the enforcement. This routing is documented in [Prompt Assembly Architecture](./prompt-assembly-architecture.md) and [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md).

**Post-generation validators** (regex checks, keyword matchers, semantic scoring on the rendered response, schema coercion) are not behavioral rules. A behavioral rule is a prescription injected into the prompt *before* generation; a post-generation validator is a heuristic check applied *after* generation. They have different failure modes (prompt rules fail by being ambiguous or ignored; validators fail by being brittle, phase-blind, or whack-a-mole-tuned) and different remediation paths. The system currently runs no post-generation validator on agent responses; the [Post-Generation Validators (Historical Case Study)](#post-generation-validators-historical-case-study) section preserves the architectural reasoning from a removed implementation.

**Goal direction and progress drive** are not prompt-layer concerns. The agent's job at the prompt layer is to follow these rules and read inputs well. Stall detection, pending milestone surfacing, and structured handoff when investigation exhausts its angles are handled by the progress transparency system at the orchestration layer — see [Progress Transparency](./progress-transparency.md) and `faultmaven/core/investigation/progress_monitor.py`.

Per-turn prompt rules that force commitment or progress pressure (e.g., OODA-style "observe-orient-decide-act every turn" or "state working diagnosis every turn") produce the failure pattern where the agent drives more, yields less to the user, and stagnates anyway. The boundary between prompt rules (what the agent does and reads) and orchestration (when to intervene on stall) is deliberate — if the agent has followed these rules, a stall is not an agent failure but a reality signal that the system should make visible.

---

## Adding New Rules

Before proposing a new rule, verify it passes the enforceability test:

1. **Is there a prescribed behavior?** If the rule says "be better at X", it's aspirational, not enforceable. The rule must specify what the agent *does* differently.
2. **Can it be mechanically enforced?** Through output structure (forced fields), vocabulary constraints (banned/required phrases), conditional routing (IF trigger THEN behavior), or injected context (different prompts for different states).
3. **Does it have a concrete prompt injection?** If you can't write the exact text that goes into the prompt, the rule isn't ready.
4. **Is it a per-turn constraint?** If the rule requires tracking patterns across multiple turns, it belongs in the progress transparency system, not in prompt rules.
5. **Is it distinct from existing rules?** Check whether the failure mode is already covered or is better addressed as an extension of an existing rule.
6. **Is it about drive, commitment, or progress pressure?** If so, it does NOT belong here. Goal direction is an orchestration concern handled by `progress_monitor` — see [Progress Transparency](./progress-transparency.md). Per-turn rules forcing drive produce the failure where the agent drives more, yields less to the user, and stagnates anyway. This lesson came from attempting OODA-style per-turn loops; the current rule/orchestration boundary is the considered response to that failure.
