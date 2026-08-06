# Orchestration Capabilities

This document details the core orchestration capabilities of the FaultMaven Investigation Engine, specifically focusing on state management, debugging, execution control, and real-time feedback.

These capabilities are implemented in the `MilestoneEngine` and supported by the `CaseRepository`.

## 1. State Checkpointing

FaultMaven snapshots case state immediately **before each state transition**, so a
transition that goes wrong leaves the prior state recoverable from the database.
This is a pre-transition recovery record, not a replay log: it is written on
transitions only, and it has no online reader (see §2).

### 1.1 Mechanism

- **Construction & persistence**: [`checkpoint_service.py:57`](../../../faultmaven/core/investigation/checkpoint_service.py) builds a `CaseCheckpoint` from `case.model_dump()`, computes a SHA-256 hash of the JSON snapshot, and persists via `case_repo.create_checkpoint(...)`.
- **Storage**: `CaseCheckpoint` rows live in `case_checkpoints`. PostgreSQL uses `JSONB` for efficient querying; SQLite (dev) uses `Text` for compatibility.
- **Immutability**: Checkpoints are append-only. The checkpoint_id is `{case_id}:turn:{current_turn}:{trigger}`, so a given `(case, turn, trigger)` tuple is unique.

### 1.2 Trigger Sites

Checkpoints fire at three sites, all in `milestone_engine.py`, and all with
trigger `pre_case_action`. Every site is guarded by `if self.checkpoint_service:`
so the engine degrades safely when the service is not wired.

| Site | When | Metadata captured |
|---|---|---|
| [`milestone_engine.py:3852`](../../../faultmaven/core/investigation/milestone_engine.py) | Confirmed case-state transition via the `pending_transition` path | `from_state`, `to_state` |
| [`milestone_engine.py:9291`](../../../faultmaven/core/investigation/milestone_engine.py) | Just before INQUIRY → INVESTIGATING (Gap #6) | `from_state`, `to_state="investigating"` |
| [`milestone_engine.py:9703`](../../../faultmaven/core/investigation/milestone_engine.py) | Just before a user-confirmed terminal transition (Gap #6) | `from_state`, `to_state` |

These snapshots make every state change reversible at the data layer — the prior
state is still on disk, recoverable by an operator reading `case_checkpoints`.
That is the whole of what checkpoints promise.

**There is no per-turn checkpoint, by decision.** A fourth site took a
`turn_complete` snapshot at the end of every successful turn. It lived in
`AgentOrchestrationService` — on the `/sessions/execute` surface no frontend
called — and was deleted with it in #982; it was never on the `/turns` path, so
per-turn coverage has never existed for a case a user actually ran.

It was not reinstated, because a checkpoint is a **full `case.model_dump()`**. One
snapshot per turn makes storage grow with turns × case size, and a case's snapshot
grows as its own evidence and hypotheses accumulate — so the cost is quadratic in
the length of an investigation, not linear. Turning that on responsibly needs a
retention and pruning policy that does not exist. Transition-shaped checkpointing
is bounded (a handful per case lifetime) and buys the property actually claimed
above.

`CheckpointService.create_checkpoint` still defaults `trigger` to
`"turn_complete"`. That default now has no caller; it is left as the seam a future
per-turn implementation would use, once retention is designed.

### 1.3 Auditability Today

`TurnProgress` entries in `case.turn_history` capture per-turn gate/progress
milestones, evidence added, hypotheses generated, and turn outcomes. **That — not
checkpoints — is the per-turn audit surface**, and it is the primary feed for the
in-product turn-by-turn UI. Checkpoints sit beneath it as a transition-time
recovery record with no user-facing surface.

## 2. Replay & Debugging (Time Travel) — RETIRED

> **Removed.** `GET /cases/{case_id}/snapshot/{turn_number}` and
> `GET /cases/{case_id}/diff` existed, were mounted, and were published in the
> OpenAPI spec. They were deleted along with `CaseReplayer`, and dropped from the
> spec, because they could not honour the contract they advertised.

### 2.1 Why

`restore_at_turn` matched a checkpoint by exact turn number, with no
nearest-preceding fallback. Checkpoints are written only at state transitions
(§1.2), so most turn numbers had no snapshot and the endpoint answered
`NotFoundError`. It was not a degraded experience; for the substance of an
investigation — turns that add evidence and hypotheses without changing state —
it was a guaranteed 404.

Nothing called it. Neither the dashboard nor the copilot has a hand-written caller;
both only carried the generated type declarations derived from the spec. So this
was a mounted, advertised, permanently-failing surface with no consumer — the same
shape as the shadow stack removed in #982, and removed for the same reason.

### 2.2 What it would take to bring back

Replay needs per-turn checkpoints, and per-turn checkpoints need a retention
policy first — a checkpoint is a full `case.model_dump()`, so writing one per turn
costs turns × case size, and the case grows as the investigation does. Designing
that is the prerequisite; the API is the easy part and should be rebuilt against
whatever the retention model turns out to be, not restored from git.

Two things survive to build on: `CheckpointService.create_checkpoint` still takes a
`trigger` (defaulting to the now-callerless `"turn_complete"`), and
`case_checkpoints` still stores immutable, hash-stamped snapshots.

### 2.3 What still answers "what happened on turn N"

`case.turn_history` (§1.3). It is per-turn, it is already the feed for the
turn-by-turn UI, and it costs a row rather than a full case snapshot.

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

> **Implementation Status: NOT IMPLEMENTED**
>
> Streaming was served by `AgentOrchestrationService.execute_agent`, which yielded
> `ExecutionEvent` objects over SSE from `POST /sessions/{session_id}/message?stream=true`.
> That service, that route and the `ExecutionEventSSE` wire model were deleted as
> dead code in #982 — the frontends had already stopped calling them.

### 4.1 Current Behaviour

A turn is a single request/response. `POST /cases/{case_id}/turns` runs the
milestone engine to completion — including the whole tool loop — and returns the
finished response in one body. Nothing is emitted mid-turn, so the UI shows an
indeterminate pending state for the turn's full duration rather than per-tool
progress.

This is why the turn timeout ladder matters: server 120s < copilot 300s <
ingress 600s. Each inner rung must stay below its client, because a caller has no
partial output to fall back on.

### 4.2 Event Vocabulary (retained, unused)

`domain/events/execution_events.py` still defines `ExecutionEvent`,
`ExecutionEventType`, `LLMEvent`, `ToolCall`, `ToolResult`, `Message` and
`AgentContext`. Only `Tool` has a live importer (`tools/base.py`, which converts
an `AgentTool` into the LLM function-calling schema). The rest is the vocabulary a
future streaming implementation would emit; it is not wired to anything today.

Any future streaming work should be designed against the engine's tool loop
(`_tool_augmented_generate()`), not restored from the deleted service — the
tool-choice, budget and vectorization behaviour there has since diverged.

## 5. Orchestration Hardening (Mechanical Safety Nets)

The `MilestoneEngine` includes two mechanical safety nets that improve the agent's data access decisions without requiring prompt changes. These are non-blocking advisories and automated actions — hints injected into the LLM context and mechanical triggers, not hard gates.

### 5.1 Coverage Gap Detection (R3) — REMOVED

> **Not implemented, and not to be restored as written.** This net lived only in
> `AgentOrchestrationService` and was deleted with it in #982. Its gap check was
> a substring test — `if ts not in coverage_lower`, where `coverage_lower` is the
> rendered coverage strings joined together — so a query for `14:32` against a
> range rendered `12:00 to 19:45` reported a *covered* time as a gap. It
> manufactured false advisories, which is what the no-incorrect-conclusion
> guarantee forbids. The gap it aimed at is real; the mechanism was not sound.

**Problem**: The LLM doesn't know its Tier 1 structural index only covers a specific time range or set of services. It may answer questions about uncovered data with incomplete evidence.

**Mechanism (as it worked)**:

1. **Query entity extraction**: Regex-based extraction of timestamps, service names, HTTP error codes, E-codes, and IP addresses from the user's message.
2. **Coverage comparison**: Extracted entities were compared against `file_meta.time_range` read from each evidence record's `structural_index`, plus a legacy branch that parsed a `--- COVERAGE METADATA ---` separator format the extractors had already stopped emitting. The comparison itself was a substring test against those strings joined together.
3. **Advisory injection**: When query entities appeared to fall outside evidence coverage, an advisory was injected into the LLM context before the system prompt.

**Current state**: nothing tells the agent when a question ranges outside what its
evidence covers. Coverage itself is intact and better-typed than when R3 was
written — `ExtractResult.file_meta` carries it as a structured dict, and
`Evidence.coverage_start_ts` / `coverage_end_ts` are real columns with real
writers. Closing the gap means an interval comparison against those columns,
wired into `_tool_augmented_generate()` — not a port of the substring check.

### 5.2 Per-Evidence DA Failure Tracking + Auto-Vectorization (R4, v5.0)

**Proactive pre-vectorization**: At DA-mode turn entry, before the tool loop begins, the engine starts background vectorization tasks for large unvectorized evidence files via `_start_proactive_vectorization()`. These run concurrently with the LLM's first iteration so semantic search (`case_evidence_search`) is available by the time the agent needs it. This optimization is gated on `force_tool_use=True` (Directed Analysis turns only) — triage and knowledge query turns don't use case evidence via semantic search, so pre-vectorization on those turns would be wasted work. See [Data Preprocessing §5](../data-processing/data-preprocessing-design-specification.md) (vectorization is scoped to DA-mode turns).

**Reactive auto-vectorization**: The agent may call `search_file` or `deep_analysis` repeatedly on the same evidence file without resolution, hitting empty results or low-confidence answers.

**Mechanism** (`_track_da_result()` and `_reactive_vectorize()` in `milestone_engine.py`):

1. Track DA failure signals **independently per evidence file**, keyed by `evidence_id` in turn-local dicts (`da_empty_search_counts`) plus the tool result's own timeout and confidence signals. State is **in-turn only**: persisting it across turns would need a backing column on Evidence, which nothing else wants yet.
2. When **any single trigger** fires on a qualifying file, auto-vectorize it — no user confirmation needed. Qualifying means `vectorization_min_size_bytes <= size <= VECTORIZATION_MAX_SIZE_BYTES`; files outside that band are left alone.
3. Three independent triggers: tool timeout, 3+ consecutive empty `search_file` results on the same file, and `deep_analysis` confidence below 0.2.
4. After 3 consecutive empty searches the tool result also carries a `[SYSTEM]` advisory suggesting a different `deep_analysis` query.
5. Reactive vectorization runs inside the tool loop, so it is bounded by `vectorization_reactive_timeout_seconds`; on timeout the agent proceeds without semantic search for that turn. Proactive vectorization (above) is unbounded because it runs concurrently with the LLM rather than blocking it.

> **Changed in #982.** The named `EvidenceDAState` dataclass, the fourth trigger
> (3+ DA invocations) and the cross-turn `da_invocation_count` persisted on the
> Evidence model all belonged to `AgentOrchestrationService` and were removed with
> it. The engine's equivalent is the three-trigger, in-turn version above. Raw
> content injection for small files does not exist on this path either — a file
> below the threshold simply gets no vectorization.

See [Data Preprocessing](../data-processing/data-preprocessing-design-specification.md) Section 6.1 for related detail.

### 5.3 Context Budget (R5)

**Problem**: Multiple tool results can fill the context window with low-signal log noise, pushing out high-value information.

**Mechanism** (message elision in `milestone_engine.py`):

1. Resolve a token budget per turn from `prompt_budget.tool_observation_max_tokens`, floored by the model's real context window via `resolve_model_budget()` — so the ceiling tracks the model actually in use.
2. Estimate tokens per assembled message. If the total fits the budget, pass the messages through untouched.
3. Otherwise keep the head (system + base task) and re-add tool-call groups newest-first while they fit, dropping whole groups rather than thinning them.
4. Insert one marker in place of what was dropped: *"[Earlier tool calls and their results were elided to stay within the context budget. Re-run a search if you need those specifics.]"*

**Key**: elision affects only what the LLM sees on this call, and it never alters
the text of a result the agent does see — a tool result is either present in full
or absent with the marker accounting for it.

> **Changed in #982.** The previous net counted characters against a fixed 30K
> `TOOL_RESULT_BUDGET` and compressed individual results by keyword-filtering
> lines (keeping `error`, `exception`, `timeout`, `traceback` and similar). That
> lived in `AgentOrchestrationService`. The engine's version is token-based,
> model-aware, and drops at group granularity, which is why there is no longer a
> high-signal keyword list. Note the audit consequence: the old net preserved
> uncompressed content in the `AgentToolCall` record, and nothing writes those
> records now (see the dormancy note on `ICaseRepository`).

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
