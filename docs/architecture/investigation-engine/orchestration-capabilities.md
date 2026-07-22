# Orchestration Capabilities

This document details the core orchestration capabilities of the FaultMaven Investigation Engine, specifically focusing on state management, debugging, execution control, and real-time feedback.

These capabilities are implemented in the `AgentOrchestrationService` and supported by the `CaseRepository`.

## 1. State Checkpointing

FaultMaven uses a **Turn-Based Checkpointing** system to keep investigation state durable and auditable. The write path is implemented; the read-side surfaces (time travel, semantic diff — see §2) are still deferred.

### 1.1 Mechanism

- **Construction & persistence**: [`checkpoint_service.py:57`](../../../faultmaven/core/investigation/checkpoint_service.py) builds a `CaseCheckpoint` from `case.model_dump()`, computes a SHA-256 hash of the JSON snapshot, and persists via `case_repo.create_checkpoint(...)`.
- **Storage**: `CaseCheckpoint` rows live in `case_checkpoints`. PostgreSQL uses `JSONB` for efficient querying; SQLite (dev) uses `Text` for compatibility.
- **Immutability**: Checkpoints are append-only. The checkpoint_id is `{case_id}:turn:{current_turn}:{trigger}`, so a given `(case, turn, trigger)` tuple is unique.

### 1.2 Trigger Sites

Checkpoints fire at four sites in the investigation flow. All sites are guarded by `if self.checkpoint_service:` so the engine degrades safely when the service is not wired.

| Site | Trigger | When | Metadata captured |
|---|---|---|---|
| [`milestone_engine.py:1923`](../../../faultmaven/core/investigation/milestone_engine.py) | `pre_case_action` | Confirmed case-status transition initiated by the engine's pending_transition path | `from_status`, `to_status` |
| [`milestone_engine.py:5791`](../../../faultmaven/core/investigation/milestone_engine.py) | `pre_case_action` | Just before INQUIRY → INVESTIGATING transition (Gap #6) | `from_status`, `to_status="investigating"` |
| [`milestone_engine.py:6065`](../../../faultmaven/core/investigation/milestone_engine.py) | `pre_case_action` | Just before user-confirmed terminal transition (Gap #6) | `from_status`, `to_status` |
| [`agent_orchestration_service.py:548`](../../../faultmaven/modules/agent/domain/services/agent_orchestration_service.py) | `turn_complete` | Fail-safe snapshot at the end of every successful turn (Step 9b) | none |

The `pre_case_action` snapshots make every state change reversible at the data layer (the prior snapshot is still on disk). The `turn_complete` snapshots give per-turn auditability without depending on the engine's narrower transition paths.

### 1.3 Auditability Today

`TurnProgress` entries in `case.turn_history` still capture per-turn gate/progress milestones, evidence added, hypotheses generated, and turn outcomes — that surface is unchanged and is the primary feed for in-product turn-by-turn UI. Checkpoints sit underneath it as the durable snapshot store; they become observable to the user only when the time-travel surfaces in §2 land.

## 2. Replay & Debugging (Time Travel)

> **Implementation Status: DEFERRED**
>
> Time travel and semantic diffing are designed but not yet implemented.
> The API endpoints described below do not exist yet. See Section 1 for
> current state tracking via `TurnProgress`.

The design includes **Read-Only Time Travel**, allowing users and developers to inspect the exact state of an investigation at any previous turn.

### 2.1 State Restoration (Design)

- **Functionality**: Would reconstruct a full `Case` object from a historical checkpoint.
- **API Endpoint**: `GET /cases/{case_id}/snapshot/{turn_number}` (not yet implemented)
- **Frontend Use Case**:
  - "View History": Allow users to click on a past message and see the "Context" (Hypotheses, Evidence, Status) as it existed *at that moment*.
  - **Read-Only**: Restored cases are for inspection only. You cannot "resume" from a past state (no forking supported in v1).

### 2.2 Semantic Diffing (Design)

- **Functionality**: Would compute the semantic difference between two investigation states (e.g., Turn 2 vs Turn 5).
- **Logic**: Recursive comparison of fields, highlighting added hypotheses, status changes, or modified evidence.
- **API Endpoint**: `GET /cases/{case_id}/diff?from={t1}&to={t2}` (not yet implemented)
- **Frontend Use Case**:
  - "What Changed?": detailed view showing exactly what the agent concluded between two points in time.

## 3. Interrupt & Resume (Human-in-the-Loop)

Human-in-the-Loop (HIL) is enforced via **Design Pattern** rather than explicit "Suspend/Resume" backend states.

### 3.1 The "Read-Only" Agent

- **Current State**: The Agent has **zero autonomy** to execute destructive actions (e.g., restarting servers, modifying code, deleting data). All available tools (`read_file`, `list_evidence`, `search_knowledge`) are **safe** and **read-only**.
- **Future Remediation**: When write-capable remediation tools are added, the **"Recommend-Verify-Act"** pattern (Section 3.2) becomes the primary safety enforcement mechanism. Prompt engineering must strictly enforce this pattern to prevent autonomous destructive actions.

### 3.2 The "Recommend-Verify-Act" Workflow

Instead of the Agent executing a fix (especially with future write-capable tools), the workflow forces user interaction:

1. **Recommend**: Agent proposes a specific action during DIAGNOSIS (e.g., "Run `kubectl rollout undo deployment/payment-api`").
2. **Verify**: The Agent provides the reasoning and evidence for this recommendation.
3. **Act**: The **User** must physically perform the action and submit results. User compliance (executing and pasting output) triggers inference-based stage transitions (DIAGNOSIS → TREATMENT or DIAGNOSIS → MITIGATION).

### 3.3 Frontend Implementation

- **No "Resume" API needed**: The frontend does not need to "resume" a suspended thread.
- **Confirmation UI**: Disposition case actions (RESOLVED/CLOSED) use the User-Agent Handshake pattern — the agent proposes resolution and the user confirms via standard message. Intermediate stage transitions (DIAGNOSIS → TREATMENT, DIAGNOSIS → MITIGATION) are inference-based — the user's compliance (executing an action and submitting results) IS acceptance, no explicit confirmation step required.

## 4. Streaming Support

The engine supports real-time streaming of agent execution events to provide a responsive UI.

### 4.1 Event Stream

The `execute_agent` method yields `ExecutionEvent` objects via an `AsyncGenerator`.

| Event Type | Description | Frontend Handling |
| :--- | :--- | :--- |
| `thinking` | Agent's internal thought process | Show "Thinking..." indicator or collapsible "Thoughts" section. |
| `tool_use` | Agent calling a tool | Show "Running tool: [Name]..." spinner. |
| `tool_result` | Result of tool execution | Update spinner to "Done" or show brief result summary. |
| `text_chunk` | Token of the final response | Append to the message bubble in real-time (typewriter effect). |
| `error` | Execution failure | Show error toast or inline error message. |
| `completed` | Turn finished | Finalize UI state, enable input. |

### 4.2 Integration

- **Protocol**: Server-Sent Events (SSE) or standard HTTP streaming response.
- **Route**: `POST /sessions/{session_id}/message?stream=true`

## 5. Orchestration Hardening (Mechanical Safety Nets)

The `AgentOrchestrationService` includes three mechanical safety nets that improve the agent's data access decisions without requiring prompt changes. These are non-blocking advisories and automated actions — hints injected into the LLM context and mechanical triggers, not hard gates.

### 5.1 Coverage Gap Detection (R3)

**Problem**: The LLM doesn't know its Tier 1 structural index only covers a specific time range or set of services. It may answer questions about uncovered data with incomplete evidence.

**Mechanism**:

1. **Query entity extraction**: Regex-based extraction of timestamps, service names, HTTP error codes, E-codes, and IP addresses from the user's message.
2. **Coverage comparison**: Extracted entities are compared against evidence coverage metadata (appended by Tier 1 extractors as `--- COVERAGE METADATA ---` blocks).
3. **Advisory injection**: When query entities fall outside evidence coverage (e.g., user asks about 14:00 but evidence covers 13:42-13:57), a coverage advisory is injected into the LLM context before the system prompt.

### 5.2 Per-Evidence DA Failure Tracking + Auto-Vectorization (R4, v5.0)

**Proactive pre-vectorization**: At DA-mode turn entry, before the tool loop begins, the engine starts background vectorization tasks for large unvectorized evidence files via `_start_proactive_vectorization()`. These run concurrently with the LLM's first iteration so semantic search (`case_evidence_search`) is available by the time the agent needs it. This optimization is gated on `force_tool_use=True` (Directed Analysis turns only) — triage and knowledge query turns don't use case evidence via semantic search, so pre-vectorization on those turns would be wasted work. See [Data Preprocessing §5](../data-processing/data-preprocessing-design-specification.md) (vectorization is scoped to DA-mode turns).

**Reactive auto-vectorization**: The agent may call `search_file` or `deep_analysis` repeatedly on the same evidence file without resolution, hitting empty results or low-confidence answers.

**Mechanism** (replaces v4.2 global `consecutive_empty_searches` counter):

1. Track DA failure signals **independently per evidence file** via `EvidenceDAState` (empty search count, DA call count, last DA confidence, timeout flag).
2. When **any single trigger** fires on a qualifying file (size above `vectorization_min_size_bytes`), auto-vectorize the file via `vectorize_file` — no user confirmation needed.
3. Four independent triggers: tool timeout, 3+ consecutive empty searches, 3+ DA invocations, low confidence (<0.2).
4. For files below the vectorization threshold, inject raw file content into the LLM context instead.
5. `da_invocation_count` is persisted on the Evidence model for cross-turn accumulation.

See [Data Preprocessing v5.0](../data-processing/data-preprocessing-design-specification.md) Section 6.1 for full implementation details.

### 5.3 Context Budget Tracking (R5)

**Problem**: Multiple tool results can fill the context window with low-signal log noise, pushing out high-value information.

**Mechanism**:

1. Track cumulative tool result characters via a `tool_result_chars` counter.
2. **Budget**: 30K characters (`TOOL_RESULT_BUDGET`).
3. At 80% budget: apply **standard compression** — keep first 3 lines + high-signal keyword lines + last 2 lines.
4. Over budget: apply **aggressive compression** — keep first line + high-signal keyword lines only.
5. High-signal keywords: `error`, `exception`, `fail`, `timeout`, `refused`, `denied`, `critical`, `fatal`, `panic`, `crash`, `kill`, `oom`, `traceback`, `stacktrace`, `caused by`.

**Key**: Compression only affects what the LLM sees. The uncompressed content is preserved in the `AgentToolCall` record for audit and debugging.

### 5.4 Tool-Augmented Generation (v5.0 → v6.0)

**Problem**: The LLM needs access to case evidence (search_file, deep_analysis) and knowledge base (kb_qa) to produce grounded responses. Without tool access, the LLM either hallucinates details or answers from training data instead of runbook content.

**Mechanism** (`_tool_augmented_generate()` in `milestone_engine.py`):

1. All turns get tools when `investigation_tools` is registered. The LLM decides which tool to invoke based on the user's question and tool descriptions.
2. `tool_choice` varies by query context:
   - **Directed Analysis + evidence**: `tool_choice="required"` — LLM must search evidence before answering
   - **All other turns** (knowledge queries, triage, terminal Q&A): `tool_choice="auto"` — LLM decides whether to use tools
3. The LLM receives investigation tools plus the schema tool as function-calling tools, with a system instruction that includes Type A/B/C question routing, evidence-vs-knowledge distinction, and OBSERVATION + ANALYSIS response format.
4. The loop runs for up to 4 iterations. Each iteration either:
   - Executes a tool call and feeds the result back to the LLM
   - Terminates when the LLM returns a structured response via `schema_tool`
5. If the LLM returns no tool calls (auto mode), its text response is captured and the next iteration forces the schema tool.

**Investigation Tools** (registered in `_create_investigation_tools()`):

| Tool | Purpose | Cost |
|------|---------|------|
| `search_file` | Keyword/regex search on raw evidence files | $0 |
| `deep_analysis` | LLM-interpreted analysis of evidence sections (1/turn limit) | ~$0.01 |
| `web_search` | Search trusted technical domains (Google CSE or Tavily provider) | $0 |
| `kb_qa` | Unified KB Q&A — searches all accessible scopes (global + personal + team) | ~$0.01 |
| `case_evidence_search` | Case-scoped forensic Q&A via semantic search on vectorized evidence | ~$0.01 |

Tools are registered conditionally — only available tools appear in the LLM's function-calling schema. If no search provider API key is configured, `web_search` is omitted. If KB vector stores aren't populated, `kb_qa` is omitted.

**DA System Instruction (Type A/B/C question routing)**: The system instruction injected for DA turns includes:

- **TYPE A — Case question**: Questions about THIS case's evidence (IPs, errors, timestamps, patterns). Agent MUST search evidence (`search_file`, `deep_analysis`) before responding. The structural indexes are summaries — they lack specific values needed for grounded analysis.
- **TYPE B — Knowledge question**: General technical questions not answerable from case evidence. Agent answers from own knowledge, optionally using `web_search` or `kb_qa` for supplementary detail. Connect to case context when relevant.
- **TYPE C — Hybrid**: Questions bridging case data and external knowledge (e.g., "Is our Redis config following best practices?"). Agent searches evidence first, then applies knowledge/KB context for the reference baseline.
- **Default**: When uncertain, treat as Type A — evidence search is always safe. Only skip evidence search when the question clearly cannot be answered from log files, configs, or other submitted data.

**Tool priority guidance** in the DA system instruction:

1. Start with case evidence (`search_file`, `deep_analysis`) — ground analysis in THIS case's data first
2. Check knowledge base (`kb_qa`) for documented solutions when evidence alone doesn't explain the issue
3. Use `web_search` as a last resort when evidence and KB have no answers

**Additional DA system instruction elements:**

- **Searchable evidence flag**: Items in `<evidence_collected>` are tagged with `searchable="true"` when there is a raw file to search — an evidence item when it is backed by an uploaded file (`source_file_id` set and the referenced `UploadedFile` row found on the case), an `<uploaded_file>` entry when its `structural_index` passes `structural_index_is_searchable()` (`context_builder.py`, threshold shared with the engine's force_tools guards — #708). Chat-extracted evidence (no source file) is never searchable. The instruction tells the LLM to only call `search_file` on items with this flag — other evidence items are investigation notes with no file to search.
- **Entity-first search**: Extract specific entities (IPs, usernames, timestamps, error codes) from the user's question and search for those exact terms
- **Keyword-first search mode**: Use `search_type: "keyword"` by default; fall back to regex only for pattern matching
- **PII token warnings**: Explicit instruction that PII tokens (IPs, hostnames, usernames) in uploaded evidence are NOT real PII and must not be redacted from tool calls
- **RESPONSE FORMAT**: OBSERVATION section must cite filename and line numbers from search results (e.g., "In data_6-1.log, line 42: ..."). ANALYSIS section explains significance with causal language.

**Evidence Resolution** (3 paths, tried in order): The `search_file` tool resolves evidence content through:

1. **In-memory case**: Check `ToolContext.in_memory_case` for evidence not yet persisted to DB (fixes race condition on first-turn uploads)
2. **Standalone table**: Query `evidence_artifacts` table by evidence ID → read raw content from `content_ref`
3. **Case-embedded (DB)**: Load case via `case_repo.get()` → find matching `Evidence` object → read content from `content_ref`

The `Evidence.original_filename` field (set during `_preprocess_attachment()`) provides the display filename in search results instead of the opaque evidence ID.

**Tool result formatting**: `search_file` results append citation guidance ("In filename, line 42: ..."). `kb_qa` results are wrapped with relay instruction and source citation guidance.

**Stage-aware context**: `_build_tool_context()` injects the current investigation stage and the in-memory `case` object into `ToolContext`, enabling stage-appropriate query enrichment and in-memory evidence access.

**Evidence vs Knowledge rule** in the DA system instruction:

Evidence is user-submitted case data (logs, metrics, configs) — only user-submitted data goes in `evidence_to_add`. Knowledge from `kb_qa`, `web_search`, or LLM training data informs analysis but is NEVER recorded as evidence.

**Key characteristics:**

- Zero additional LLM calls for mechanical tools (search_file, web_search are $0)
- KB synthesis costs ~$0.01 per call (vector search + LLM synthesis, max_tokens=2000)
- `tool_choice="auto"` for non-DA turns adds zero overhead when LLM doesn't need tools
- Falls back gracefully to single-shot on loop exhaustion or provider incompatibility
- `kb_qa` results are formatted with relay instructions and source citation guidance

See [Data Preprocessing Design §5.0](../data-processing/data-preprocessing-design-specification.md) for the scenario-driven processing model that determines when DA mode is selected.

## 6. Terminal Observability

When a case reaches a terminal state (RESOLVED or CLOSED), today's emission surface is:

- **Invariant-scoped Prometheus counters** in `faultmaven/core/investigation/lifecycle_metrics.py`. These measure outcomes, follow the bounded-label discipline (no case_id, user_id, or other unbounded identifiers), and are no-ops unless `ENABLE_METRICS=true` and `prometheus_client` is installed (see `shims/metrics.py`). The terminal-relevant counter is `faultmaven_resolution_cause_leg_total{provider, leg}`, incremented at the top of `terminal_transitions.finalize_resolution_truth_surface` — the INV-41 backstop-reliance gate (see [Investigation Invariants](./investigation-invariants.md)).
- **Standard log lines** from `terminal_transitions.py` recording each transition (e.g. `transitioned to RESOLVED (terminal state)`).

A broader case-analytics suite — terminal-outcome counters by `closure_reason`, duration/turn-count/evidence-count histograms, an active-cases gauge, and a `case.terminal` structured log event — was designed in an earlier revision of this section but never implemented. That spec, with the schema corrections it now needs (current `VALID_CLOSURE_REASONS`, no `path_selection`), is tracked in [#791](https://github.com/FaultMaven/faultmaven/issues/791).
