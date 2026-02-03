# Orchestration Capabilities

This document details the core orchestration capabilities of the FaultMaven Investigation Engine, specifically focusing on state management, debugging, execution control, and real-time feedback.

These capabilities are implemented in the `AgentOrchestrationService` and supported by the `CaseRepository`.

## 1. State Checkpointing

FaultMaven implements a robust **Turn-Based Checkpointing** system to ensure investigation state is durable, auditable, and restorable.

### 1.1 Mechanism
*   **Trigger**: A checkpoint is automatically created at the end of every agent execution turn (`turn_complete`).
*   **Storage**: The full state of the `Case` object is serialized and stored in the `CaseCheckpoint` table.
    *   **PostgreSQL**: Uses `JSONB` for efficient querying.
    *   **SQLite** (Dev): Uses `Text` (JSON string) for compatibility.
*   **Immutability**: Checkpoints are append-only. Once a turn is completed, its state record is permanent.

### 1.2 usage
This happens automatically. No frontend intervention is required.
*   **Backend Hook**: `AgentOrchestrationService.execute_agent` -> Step 9b.

## 2. Replay & Debugging (Time Travel)

The system supports **Read-Only Time Travel**, allowing users and developers to inspect the exact state of an investigation at any previous turn.

### 2.1 State Restoration
*   **Functionality**: Reconstructs a full `Case` object from a historical checkpoint.
*   **API Endpoint**: `GET /cases/{case_id}/snapshot/{turn_number}`
*   **Frontend Use Case**:
    *   "View History": Allow users to click on a past message and see the "Context" (Hypotheses, Evidence, Status) as it existed *at that moment*.
    *   **Read-Only**: Restored cases are for inspection only. You cannot "resume" from a past state (no forking supported in v1).

### 2.2 Semantic Diffing
*   **Functionality**: Computes the semantic difference between two investigation states (e.g., Turn 2 vs Turn 5).
*   **Logic**: Recursive comparison of fields, highlighting added hypotheses, status changes, or modified evidence.
*   **API Endpoint**: `GET /cases/{case_id}/diff?from={t1}&to={t2}`
*   **Frontend Use Case**:
    *   "What Changed?": detailed view showing exactly what the agent concluded between two points in time.

## 3. Interrupt & Resume (Human-in-the-Loop)

Human-in-the-Loop (HIL) is enforced via **Design Pattern** rather than explicit "Suspend/Resume" backend states.

### 3.1 The "Read-Only" Agent
*   **Current State**: The Agent has **zero autonomy** to execute destructive actions (e.g., restarting servers, modifying code, deleting data). All available tools (`read_file`, `list_evidence`, `search_knowledge`) are **safe** and **read-only**.
*   **Future Remediation**: When write-capable remediation tools are added, the **"Recommend-Verify-Act"** pattern (Section 3.2) becomes the primary safety enforcement mechanism. Prompt engineering must strictly enforce this pattern to prevent autonomous destructive actions.

### 3.2 The "Recommend-Verify-Act" Workflow
Instead of the Agent executing a fix (especially with future write-capable tools), the workflow forces user interaction:
1.  **Recommend**: Agent proposes a solution (e.g., "I recommend rolling back deployment X").
2.  **Verify**: The Agent provides the reasoning and evidence for this recommendation.
3.  **Act**: The **User** must physically perform the action (or click a "Confirm" button in the UI that triggers a separate execution flow).

### 3.3 Frontend Implementation
*   **No "Resume" API needed**: The frontend does not need to "resume" a suspended thread.
*   **Confirmation UI**: If the Agent asks for confirmation (e.g., "Shall I close the case?"), the UI simply sends the user's "Yes" or "No" as a standard user message in the next turn.

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
