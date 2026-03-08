# Orchestration Capabilities

This document details the core orchestration capabilities of the FaultMaven Investigation Engine, specifically focusing on state management, debugging, execution control, and real-time feedback.

These capabilities are implemented in the `AgentOrchestrationService` and supported by the `CaseRepository`.

## 1. State Checkpointing

> **Implementation Status: DEFERRED**
>
> The checkpointing system described below is designed but not yet implemented in code.
> `CaseCheckpoint` is defined in contracts but not instantiated in the investigation flow.
> Turn-level state is currently tracked via `TurnProgress` records in `case.turn_history`,
> which provides partial auditability. Full checkpoint/snapshot/diff functionality is
> planned for a future release.

FaultMaven's design includes a **Turn-Based Checkpointing** system to ensure investigation state is durable, auditable, and restorable.

### 1.1 Mechanism (Design)
*   **Trigger**: A checkpoint would be automatically created at the end of every agent execution turn (`turn_complete`).
*   **Storage**: The full state of the `Case` object would be serialized and stored in the `CaseCheckpoint` table.
    *   **PostgreSQL**: Uses `JSONB` for efficient querying.
    *   **SQLite** (Dev): Uses `Text` (JSON string) for compatibility.
*   **Immutability**: Checkpoints are append-only. Once a turn is completed, its state record is permanent.

### 1.2 Current State
Turn progress is recorded via `TurnProgress` entries in `case.turn_history`, which captures gate milestones, progress milestones, evidence added, hypotheses generated, and turn outcomes. This provides basic auditability but does not support full state snapshots or time travel.

## 2. Replay & Debugging (Time Travel)

> **Implementation Status: DEFERRED**
>
> Time travel and semantic diffing are designed but not yet implemented.
> The API endpoints described below do not exist yet. See Section 1 for
> current state tracking via `TurnProgress`.

The design includes **Read-Only Time Travel**, allowing users and developers to inspect the exact state of an investigation at any previous turn.

### 2.1 State Restoration (Design)
*   **Functionality**: Would reconstruct a full `Case` object from a historical checkpoint.
*   **API Endpoint**: `GET /cases/{case_id}/snapshot/{turn_number}` (not yet implemented)
*   **Frontend Use Case**:
    *   "View History": Allow users to click on a past message and see the "Context" (Hypotheses, Evidence, Status) as it existed *at that moment*.
    *   **Read-Only**: Restored cases are for inspection only. You cannot "resume" from a past state (no forking supported in v1).

### 2.2 Semantic Diffing (Design)
*   **Functionality**: Would compute the semantic difference between two investigation states (e.g., Turn 2 vs Turn 5).
*   **Logic**: Recursive comparison of fields, highlighting added hypotheses, status changes, or modified evidence.
*   **API Endpoint**: `GET /cases/{case_id}/diff?from={t1}&to={t2}` (not yet implemented)
*   **Frontend Use Case**:
    *   "What Changed?": detailed view showing exactly what the agent concluded between two points in time.

## 3. Interrupt & Resume (Human-in-the-Loop)

Human-in-the-Loop (HIL) is enforced via **Design Pattern** rather than explicit "Suspend/Resume" backend states.

### 3.1 The "Read-Only" Agent
*   **Current State**: The Agent has **zero autonomy** to execute destructive actions (e.g., restarting servers, modifying code, deleting data). All available tools (`read_file`, `list_evidence`, `search_knowledge`) are **safe** and **read-only**.
*   **Future Remediation**: When write-capable remediation tools are added, the **"Recommend-Verify-Act"** pattern (Section 3.2) becomes the primary safety enforcement mechanism. Prompt engineering must strictly enforce this pattern to prevent autonomous destructive actions.

### 3.2 The "Recommend-Verify-Act" Workflow
Instead of the Agent executing a fix (especially with future write-capable tools), the workflow forces user interaction:
1.  **Recommend**: Agent proposes a specific action during DIAGNOSIS (e.g., "Run `kubectl rollout undo deployment/payment-api`").
2.  **Verify**: The Agent provides the reasoning and evidence for this recommendation.
3.  **Act**: The **User** must physically perform the action and submit results. User compliance (executing and pasting output) triggers inference-based stage transitions (DIAGNOSIS → TREATMENT or DIAGNOSIS → MITIGATION).

### 3.3 Frontend Implementation
*   **No "Resume" API needed**: The frontend does not need to "resume" a suspended thread.
*   **Confirmation UI**: Disposition case actions (RESOLVED/CLOSED) use the User-Agent Handshake pattern — the agent proposes resolution and the user confirms via standard message. Intermediate stage transitions (DIAGNOSIS → TREATMENT, DIAGNOSIS → MITIGATION) are inference-based — the user's compliance (executing an action and submitting results) IS acceptance, no explicit confirmation step required.

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
*   **Protocol**: Server-Sent Events (SSE) or standard HTTP streaming response.
*   **Route**: `POST /sessions/{session_id}/message?stream=true`

## 5. Orchestration Hardening (Mechanical Safety Nets)

The `AgentOrchestrationService` includes three mechanical safety nets that improve the agent's data access decisions without requiring prompt changes. These are non-blocking advisories and automated actions — hints injected into the LLM context and mechanical triggers, not hard gates.

### 5.1 Coverage Gap Detection (R3)

**Problem**: The LLM doesn't know its Tier 1 structural index only covers a specific time range or set of services. It may answer questions about uncovered data with incomplete evidence.

**Mechanism**:

1. **Query entity extraction**: Regex-based extraction of timestamps, service names, HTTP error codes, E-codes, and IP addresses from the user's message.
2. **Coverage comparison**: Extracted entities are compared against evidence coverage metadata (appended by Tier 1 extractors as `--- COVERAGE METADATA ---` blocks).
3. **Advisory injection**: When query entities fall outside evidence coverage (e.g., user asks about 14:00 but evidence covers 13:42-13:57), a coverage advisory is injected into the LLM context before the system prompt.

### 5.2 Per-Evidence DA Failure Tracking + Auto-Vectorization (R4, v5.0)

**Problem**: The agent may call `search_file` or `deep_analysis` repeatedly on the same evidence file without resolution, hitting empty results or low-confidence answers.

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

### 5.4 DA Tool Loop: Bounded Tool-Calling (v5.0)

**Problem**: In Directed Analysis turns, the LLM needs to search evidence files for specific data (timestamps, error codes, usernames) before generating a grounded response. Without tool access during generation, the LLM either hallucinates details or produces generic answers not grounded in case evidence.

**Mechanism** (`_tool_augmented_generate()` in `milestone_engine.py`):

1. When `query_mode == "directed_analysis"`, the milestone engine routes inference through a bounded tool-calling loop instead of single-shot generation.
2. The LLM receives `search_file` and `schema_tool` as function-calling tools, with a DA-specific system instruction that includes keyword-first search guidance and a required OBSERVATION + ANALYSIS response format.
3. **Iteration-0 guardrail**: At iteration 0, only investigation tools (`search_file`) are available — the schema tool is withheld. This forces the LLM to perform at least one search before attempting to generate a structured response.
4. The loop runs for up to 4 iterations. Each iteration either:
   - Executes a tool call (search_file or schema_tool) and feeds the result back to the LLM
   - Terminates when the LLM returns a structured response via `schema_tool`
5. If the loop exhausts all iterations without a structured response, it falls back to single-shot `_generate_structured_output()`.

**DA System Instruction**: The system instruction injected for DA turns includes:

* **Entity-first search**: Extract specific entities (IPs, usernames, timestamps, error codes) from the user's question and search for those exact terms
* **Keyword-first search mode**: Use `search_type: "keyword"` by default; fall back to regex only for pattern matching
* **PII token warnings**: Explicit instruction that PII tokens (IPs, hostnames, usernames) in uploaded evidence are NOT real PII and must not be redacted from tool calls
* **RESPONSE FORMAT**: Requires OBSERVATION section (cite specific data from at least 2 different categories — timestamps, error messages, IPs/usernames, metrics/counts) followed by ANALYSIS section (explain WHY using causal language)

**Dual-Path Evidence Resolution**: The `search_file` tool resolves evidence content through two paths:

* **Path 1 (standalone)**: Query `evidence_artifacts` table by evidence ID → read raw content from `content_ref`
* **Path 2 (case-embedded)**: Load case via `case_repo.get()` → find matching `Evidence` object → read content from `content_ref`

The `Evidence.original_filename` field (set during `_preprocess_attachment()`) provides the display filename in search results instead of the opaque evidence ID.

**Key characteristics:**

* Zero additional LLM calls for search (search_file is mechanical, $0)
* At most 1 additional LLM call per tool iteration (for the loop's inference step)
* Falls back gracefully to single-shot on loop exhaustion
* Iteration-0 guardrail ensures at least one evidence search before response

See [Data Preprocessing Design §5.0](../data-processing/data-preprocessing-design-specification.md) for the scenario-driven processing model that determines when DA mode is selected.
