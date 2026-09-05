# Data Processing

Documentation for FaultMaven's data ingestion, preprocessing, and classification systems.

## Overview

This section documents how FaultMaven ingests user-submitted data, preprocesses and classifies it, and extracts insights from various formats. The system uses a **unified DataType taxonomy** (6 types: LOGS, METRICS, CONFIGURATION, CODE, TEXT, IMAGE) shared across all components.

Two distinct but related classification tasks are performed:

1. **Data type classification** (Tier 0) — Rule-based detection producing a `DataType` enum value
2. **Evidence classification** — LLM-based categorization into SYMPTOM/CAUSAL/MITIGATION/SOLUTION/CONTEXTUAL/REJECTED

All user turns arrive via the **Unified Ingestion Pipeline** (`POST /cases/{id}/turns`). Attachments are preprocessed through Tier 0+1 **before** the LLM runs (Step 1), then the LLM performs inference with structural indexes included in context (Step 2). Evidence form is determined by payload context (attachments → `DOCUMENT`, agent findings → `SUBMITTED_DATA`), not by LLM classification.

---

## Scenario-Driven Processing Model

FaultMaven uses a **scenario-driven processing model** where a mechanical query classifier routes each turn to one of four processing modes (plus a mechanical semantic-search fallback):

| Mode | When | System Prompt | Vectorization |
|------|------|---------------|---------------|
| **Triage** | Generic request ("analyze this") or file drop with no question | Structural index is the answer. Summarize findings. | Not triggered |
| **Directed Analysis** | Specific question with entities (timestamps, error codes, services) | Tool loop with `search_file`, `deep_analysis`, `kb_qa`, `web_search`, `case_evidence_search`. `tool_choice="required"`. Type A/B/C question routing. | Proactive for qualifying large files at start of tool loop; reactive fallback on DA failure |
| **Knowledge Query** | General knowledge question ("What is Opik?", "How does Redis work?") without case-specific entities or references | Tool loop with `tool_choice="auto"` — LLM can invoke `kb_qa` for runbook content or answer from built-in knowledge. Evidence-grounding relaxed by omitting the EVIDENCE GROUNDING block entirely (`evidence_grounding=""`). | Not triggered |
| **Agent Meta** | A question about FaultMaven itself ("what model are you?", "how do you retrieve runbooks?") — the assistant, not the system under investigation (#1328) | `AGENT_META_INSTRUCTIONS` replaces the stage instructions: a high-level self-knowledge profile (stack, investigation engine, retrieval, multi-provider routing) plus an answer discipline — three to six sentences, point to the repository docs, say plainly that the model is not told which provider serves the deployment, no tool calls, never request FaultMaven's configuration as evidence, leave case state untouched. EVIDENCE GROUNDING and DIAGNOSTIC REASONING are waived as for Knowledge Query. Tools are never forced; the DA system instruction carries a matching Type D. A fresh evidence-bearing upload re-routes the turn to Directed Analysis in INVESTIGATING (#708) and to Triage in INQUIRY, so the file is characterised and the meta question falls to the backstop rule. A short backstop rule in the shared advisor block covers phrasings the heuristic misses. | Not triggered |
| **Semantic Search** | Fallback path after vectorization completes | N/A (mechanical, not prompt-driven) | `case_evidence_search` queries the vectorized file |

**Out-of-band lane (#1329).** Before the tenant turn cap is charged, a text-only CONVERSATION message (no attachment, no structured intent, no pending gate, not a bare greeting) is asked whether it is incident work at all. `agent_meta` is out of band by construction; `knowledge_query` and the low-confidence `directed_analysis` fall-through (≤ 0.6, no entities) go to a one-token classifier call (`modules/agent/domain/services/out_of_band.py`) that sees the assistant's previous message, so "yes"/"done" read as continuations. Typed answers to offered choices are resolved FIRST and are incident work. A lone `2` means off-topic; anything else — including a failed call — is incident work and is charged. An out-of-band turn is answered from a ~1.5 KB prompt on the synthesis role (no case context, no tools), is **not** charged, still advances the message clock, and is recorded with `TurnOutcome.OUT_OF_BAND`, which every investigative-turn counter excludes and which the EARLIER TURNS history renders as a one-line aside.

All submissions are preprocessed through **Tier 0+1 (Structural Indexing)** — classification + type-specific extraction — before mode selection. The query classifier (`classify_query()`) uses regex entity detection, knowledge phrase detection, case reference detection, and phrasing analysis. No LLM call for routing.

---

## Unified DataType Enum

All documents in this section share a single DataType taxonomy. See [Data Classification Strategy → Two-Layer Data Type Enum](./data-classification-strategy.md#two-layer-data-type-enum) for the canonical definition; the table below is a quick reference.

| DataType | Description |
|----------|-------------|
| `LOGS` | Time-ordered diagnostic output (logs, traces, command output) |
| `METRICS` | Quantitative measurements (time-series, dashboards, alerts) |
| `CONFIGURATION` | Structured system/app config (YAML, JSON, TOML, env) |
| `CODE` | Source code files |
| `TEXT` | Unstructured prose (docs, runbooks, descriptions) |
| `IMAGE` | Visual content (screenshots, diagrams) |

---

## Documents

### Data Preprocessing

- **[Data Preprocessing Design Specification](./data-preprocessing-design-specification.md)** (v5.4) — Core preprocessing architecture with scenario-driven processing modes. Defines Tier 0+1 structural indexing (12 detailed types → 6 unified types, 11 extractors returning `ExtractResult` with three parts: `file_extract` orientation content, `search_map` entity profile + search hints, and `file_meta` coverage data as a structured dict), mechanical query classifier (`classify_query()` — heuristic entity detection + phrasing analysis), mode-specific system prompts (Triage vs Directed Analysis), proactive + reactive vectorization with per-evidence DA failure tracking, DA-only gate on proactive embedding, persistent `Evidence.vectorized` lifecycle flag, in-flight task registry for cross-turn dedup, and split time-bound semantics (proactive unbounded, reactive configurable via `VECTORIZATION_REACTIVE_TIMEOUT_SECONDS`), small-file DA failure fallback, unified ingestion pipeline (`POST /cases/{id}/turns`), Context Sliding Window, evidence form determination, BGE-M3 preload at lifespan startup with `asyncio.to_thread` offload on the request hot path, and orchestration hardening (R3 coverage gap detection, R4 vectorization with proactive + reactive paths, R5 context budgeting).

- **[Data Classification Strategy](./data-classification-strategy.md)** (v3.0) — Tier 0 classification rules. 5-priority signal-source ordering (user_override / agent_hint / source_url / browser_context / rule_based), `_validate_hint` safety valve, CSV/TSV structural gate, extension-sensitive LOGS thresholds, command-output detection, and `classification_failed` cooperative-clarification path.

- **[Platform-Specific Extractors](./platform-specific-extractors.md)** — Future enhancement: platform-aware extraction for SRE/DevOps tools (Datadog, Grafana, PagerDuty, etc.). Can integrate as Tier 1 frontend extractors or Tier 3 backends.

### Evidence Pipeline

- **[Evidence Flow Architecture](./evidence-flow-architecture.md)** — System architecture and flow diagrams for the evidence pipeline. Covers the unified turn endpoint (`POST /cases/{id}/turns`) through two-step pipeline (preprocess attachments → LLM inference), to persistence, including sequence diagrams, state machines, and monitoring.

- **[Evidence Failure Modes](./evidence-failure-modes.md)** — Failure handling for evidence creation: orphan-file cleanup, deduplication, storage retries.

Evidence taxonomy and schema are defined in the investigation-engine domain:

- **Live data model:** [investigation-engine/evidence-driven-investigation-framework.md §5](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model)
- **Live DB schema:** [data-and-storage/schemas/case-schema.md](../data-and-storage/schemas/case-schema.md) §"Evidence"

---

## Canonical Ownership

Several topics appear in multiple documents in this domain. To prevent drift, each topic has a single **owning document** — the canonical source. Other documents may summarize or reference the topic, but the owning document is authoritative. When a topic is re-described elsewhere, the secondary description should defer with a link rather than restate details.

| Topic | Owning Document | Secondary (must defer, not restate) |
| --- | --- | --- |
| DataType enum (12 detailed → 6 unified) | [data-classification-strategy.md](./data-classification-strategy.md) §"Two-Layer Data Type Enum" | this README |
| Tier 0 classification rules & `classification_failed` path | [data-classification-strategy.md](./data-classification-strategy.md) | `data-preprocessing-design-specification.md` §2.5, `evidence-flow-architecture.md` |
| `extraction_method` / `strategy_name` vocabulary | [data-preprocessing-design-specification.md](./data-preprocessing-design-specification.md) Appendix B (code canonical: `core/preprocessing/models.py` → `ExtractionMethod`) | — |
| Unified ingestion pipeline (two-step preprocessing flow) | [data-preprocessing-design-specification.md](./data-preprocessing-design-specification.md) §2.4 | this README, `evidence-flow-architecture.md` |
| Page capture pipeline (Stage 1/2) | [data-preprocessing-design-specification.md](./data-preprocessing-design-specification.md) §2.4 | `platform-specific-extractors.md`, `evidence-flow-architecture.md` |
| Orchestration Hardening (R3/R4/R5) | [data-preprocessing-design-specification.md](./data-preprocessing-design-specification.md) §6.1 | `evidence-flow-architecture.md` |
| DA Tool Loop (`_tool_augmented_generate`) | [orchestration-capabilities.md §5.4](../investigation-engine/orchestration-capabilities.md#54-tool-augmented-generation-v50--v60) (cross-domain) | `data-preprocessing-design-specification.md`, `evidence-flow-architecture.md`, this README |
| Failure modes (orphan cleanup, dedup, storage retries) | [evidence-failure-modes.md](./evidence-failure-modes.md) Status table | `evidence-flow-architecture.md` |
| Evidence taxonomy + schema | [investigation-engine/evidence-driven-investigation-framework.md §5](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model) + [data-and-storage/schemas/case-schema.md](../data-and-storage/schemas/case-schema.md) §"Evidence" | `data-preprocessing-design-specification.md` |

**Rules of thumb for edits:**

1. Land substantive changes in the owning document first.
2. When adding a new topic to a non-owning document, either (a) keep it to one paragraph with a link to the canonical, or (b) propose moving it to the canonical.
3. If two docs disagree, the owning document wins — reconcile the other.

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| Unified Ingestion Pipeline | **Implemented** | `POST /cases/{id}/turns` — two-step pipeline (preprocess → LLM). Old `/queries` and `/data` endpoints deleted. |
| Tier 0+1: Structural Indexing | **Implemented** | 12 detailed types, 11 extractors, best-effort fallback. Pasted text routed through same pipeline. |
| Tier 2: Mechanical Search | **Implemented** | `search_file` agent tool — two-pass keyword search (ALL→partial fallback), regex, extractor re-run. Zero-result vocabulary recovery. |
| Interpreted Search (formerly Tier 3) | **Implemented** | `deep_analysis` tool (to be merged into `search_file` as `interpret: true`). Default backend changed from `disabled` to `local` — uses configured CHAT_PROVIDER, no additional setup. |
| Vectorization (auto-triggered) | **Implemented** | Proactive for DA-mode turns: background vectorization starts in `_tool_augmented_generate()` for qualifying large files (size ≥ min, ≤ 50MB, not already vectorized) before the tool loop begins. Reactive fallback triggers on the primary `/turns` path: tool timeout, 3+ consecutive empty `search_file` results, `deep_analysis` confidence < 0.2. `da_call_count >= 3` removed in v5.2. No user confirmation. Size gates enforced. |
| Query Classifier | **Implemented** | `classify_query()` — heuristic entity detection + phrasing analysis. Routes to Triage, Knowledge Query, Agent Meta, or Directed Analysis. Knowledge Query uses 3-gate detection (knowledge phrase + no hard entities + no case references). Agent Meta (#1328) is checked first and uses the same two blocking gates with self-reference patterns each bound to the assistant ("what model are you", "your architecture", "how do you work?" clause-final, "generating these responses", "what is FaultMaven?"); error keywords also gate. Bare "you"/"your" ("could you check the logs", "your stack trace"), an unbound model/provider/vector-DB noun ("which model is serving the /predict endpoint"), and FaultMaven named in a case question ("what does FaultMaven think caused it") do not qualify. Precision-first: a miss falls through to the prompt-level backstop rule, a false positive would pull a diagnostic question off the evidence path. |
| Mode-Specific System Prompts | **Implemented** | `DATA_ACCESS_TRIAGE` and `DATA_ACCESS_DIRECTED_ANALYSIS` injected via `{data_access_strategy}` placeholder. Knowledge Query sets `adaptive_instructions=KNOWLEDGE_QUERY_INSTRUCTIONS` and `evidence_grounding=""`, so the EVIDENCE GROUNDING block is absent from the rendered prompt entirely. Agent Meta sets `adaptive_instructions=AGENT_META_INSTRUCTIONS` with the same waiver; in INQUIRY (no stage slot) the same block renders through `{agent_meta_instructions}`, which is empty for every other mode. |
| Per-Evidence DA Failure Tracking | **Implemented** | `milestone_engine._tool_augmented_generate()`: simple per-evidence counters (`da_empty_search_counts`, deep-analysis confidence) track empty searches, confidence, and timeouts. **Within a turn only** — there is no cross-turn DA history. The `EvidenceDAState` structure and the `/sessions/execute` path that held it were removed with the agent orchestration service. The `da_invocation_count` Evidence field that was to carry the count across turns never had a DB column, so every save silently dropped it; it was deleted as dead Pydantic decoration on 2026-04-25. Cross-turn state that does persist: `Evidence.vectorized`, which is what stops repeat work. |
| DA Tool Loop | **Implemented** | Tool-augmented generation (`_tool_augmented_generate()`) for all turns when tools are registered. Tools: `search_file`, `deep_analysis`, `kb_qa`, `web_search`, `case_evidence_search` + schema tool, up to 4 iterations. DA turns use `tool_choice="required"`; other turns use `tool_choice="auto"`. Type A/B/C question routing + evidence-vs-knowledge rule in system instruction. See [Orchestration Capabilities §5.4](../investigation-engine/orchestration-capabilities.md#54-tool-augmented-generation-v50--v60). |
| Evidence `original_filename` | **Implemented** | Set during `_preprocess_attachment()`, displayed by `search_file` tool instead of opaque evidence ID. |
| Context Sliding Window | **Implemented** | File extracts in LLM context as `<file_extract>`, `<search_map>`, and `<file_meta>` XML elements (Tier A: recent data with all three elements, Tier B: older summary, Tier C: user text summary). `role="orientation"` in DA mode. `searchable="true"` attribute on evidence with raw files on disk. |
| Evidence Form (Payload-driven) | **Implemented** | `_determine_evidence_form()` and `SubmissionClassification` deleted. Form set by payload context. |
| Evidence Classification | **Implemented** | Single-phase creation with LLM evaluation |
| Evidence Failure Modes | Design Complete | Async retry, orphan cleanup designed; deferred to post-MVP |
| Page Capture Pipeline | **Implemented** | Stage 1: Semantic DOM extraction via `htmlToStructuredText` (copilot), backend pass-through for `source_type=page_capture`. Stage 2: Query-time section reranking in `context_builder.py` — scores page capture sections against user query, promotes relevant content before char-cap truncation. |
| Platform-Specific Extractors | Planned | Future enhancement for SRE/DevOps tool parsing. Generic extraction (Stage 1) handles most dashboard patterns via tryKeyValue/tryStatValue. |
| Coverage Metadata (Tier 1) | **Implemented** | Coverage data is now the `file_meta` field of `ExtractResult` — a structured dict returned alongside `file_extract` and `search_map`. The `--- COVERAGE METADATA ---` separator text is removed. |
| Orchestration Hardening | **Implemented** | R3: coverage gap detection, R4: per-evidence DA failure tracking + auto-vectorization, R5: 30K char context budget with compression |
| Pattern Learning System | Planned | Adaptive classification from user corrections (Phase 3) |
