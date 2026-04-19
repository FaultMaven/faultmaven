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

FaultMaven uses a **scenario-driven processing model** where a mechanical query classifier routes each turn to one of three processing modes:

| Mode | When | System Prompt | Vectorization |
|------|------|---------------|---------------|
| **Triage** | Generic request ("analyze this") or file drop with no question | Structural index is the answer. Summarize findings. | Not triggered |
| **Directed Analysis** | Specific question with entities (timestamps, error codes, services) | Tool loop with `search_file`, `deep_analysis`, `kb_qa`, `web_search`, `case_evidence_search`. `tool_choice="required"`. Type A/B/C question routing. | Proactive for qualifying large files at start of tool loop; reactive fallback on DA failure |
| **Knowledge Query** | General knowledge question ("What is Opik?", "How does Redis work?") without case-specific entities or references | Tool loop with `tool_choice="auto"` — LLM can invoke `kb_qa` for runbook content or answer from built-in knowledge. Evidence-grounding relaxed via KNOWLEDGE QUERY OVERRIDE. | Not triggered |
| **Semantic Search** | Fallback path after vectorization completes | N/A (mechanical, not prompt-driven) | `case_evidence_search` queries the vectorized file |

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

- **[Data Preprocessing Design Specification](./data-preprocessing-design-specification.md)** (v5.3) — Core preprocessing architecture with scenario-driven processing modes. Defines Tier 0+1 structural indexing (12 detailed types → 6 unified types, 11 extractors with coverage metadata), mechanical query classifier (`classify_query()` — heuristic entity detection + phrasing analysis), mode-specific system prompts (Triage vs Directed Analysis), proactive + reactive vectorization with per-evidence DA failure tracking, small-file DA failure fallback, unified ingestion pipeline (`POST /cases/{id}/turns`), Context Sliding Window, evidence form determination, and orchestration hardening (R3 coverage gap detection, R4 vectorization with proactive + reactive paths, R5 context budgeting).

- **[Data Classification Strategy](./data-classification-strategy.md)** (v3.0) — Tier 0 classification rules. 5-priority signal-source ordering (user_override / agent_hint / source_url / browser_context / rule_based), `_validate_hint` safety valve, CSV/TSV structural gate, extension-sensitive LOGS thresholds, command-output detection, and `classification_failed` cooperative-clarification path.

- **[Platform-Specific Extractors](./platform-specific-extractors.md)** — Future enhancement: platform-aware extraction for SRE/DevOps tools (Datadog, Grafana, PagerDuty, etc.). Can integrate as Tier 1 frontend extractors or Tier 3 backends.

### Evidence Classification

- **[Evidence Classification Design](./evidence-classification-design.md)** — Evidence taxonomy: 6 categories (SYMPTOM, CAUSAL, MITIGATION, SOLUTION, CONTEXTUAL, REJECTED — 5 valid + 1 rejected), unified DataType, payload-driven form determination (DOCUMENT/USER_TEXT/SUBMITTED_DATA), content-based classification, and milestone attribution (Option 2.5).

- **[Evidence Flow Architecture](./evidence-flow-architecture.md)** — System architecture and flow diagrams for the evidence pipeline. Covers the unified turn endpoint (`POST /cases/{id}/turns`) through two-step pipeline (preprocess attachments → LLM inference), to persistence, including sequence diagrams, state machines, and monitoring.

- **[Evidence Failure Modes](./evidence-failure-modes.md)** — Failure handling design for single-phase evidence creation. Covers LLM timeout recovery, DB insert retries, storage cleanup, and deduplication strategies. *(Deferred to post-MVP)*

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| Unified Ingestion Pipeline | **Implemented** | `POST /cases/{id}/turns` — two-step pipeline (preprocess → LLM). Old `/queries` and `/data` endpoints deleted. |
| Tier 0+1: Structural Indexing | **Implemented** | 12 detailed types, 11 extractors, best-effort fallback. Pasted text routed through same pipeline. |
| Tier 2: Mechanical Search | **Implemented** | `search_file` agent tool — two-pass keyword search (ALL→partial fallback), regex, extractor re-run. Zero-result vocabulary recovery. |
| Interpreted Search (formerly Tier 3) | **Implemented** | `deep_analysis` tool (to be merged into `search_file` as `interpret: true`). Default backend changed from `disabled` to `local` — uses configured CHAT_PROVIDER, no additional setup. |
| Vectorization (auto-triggered) | **Implemented** | Proactive for DA-mode turns: background vectorization starts in `_tool_augmented_generate()` for qualifying large files (size ≥ min, ≤ 50MB, not already vectorized) before the tool loop begins. Reactive fallback triggers on the primary `/turns` path: tool timeout, 3+ consecutive empty `search_file` results, `deep_analysis` confidence < 0.2. `da_call_count >= 3` removed in v5.2. No user confirmation. Size gates enforced. |
| Query Classifier | **Implemented** | `classify_query()` — heuristic entity detection + phrasing analysis. Routes to Triage, Knowledge Query, or Directed Analysis. Knowledge Query uses 3-gate detection (knowledge phrase + no hard entities + no case references). |
| Mode-Specific System Prompts | **Implemented** | `DATA_ACCESS_TRIAGE` and `DATA_ACCESS_DIRECTED_ANALYSIS` injected via `{data_access_strategy}` placeholder. Knowledge Query appends `KNOWLEDGE QUERY OVERRIDE` escape clause. |
| Per-Evidence DA Failure Tracking | **Implemented** | Primary `/turns` path (`milestone_engine._tool_augmented_generate()`): simple per-evidence counters (`da_empty_search_counts`, `da_vectorized`) track empty searches, confidence, and timeouts. Secondary `/sessions/execute` path: `EvidenceDAState` retained. Cross-turn DA history reconstructed via persisted `da_invocation_count` on the Evidence model. |
| DA Tool Loop | **Implemented** | Tool-augmented generation (`_tool_augmented_generate()`) for all turns when tools are registered. Tools: `search_file`, `deep_analysis`, `kb_qa`, `web_search`, `case_evidence_search` + schema tool, up to 4 iterations. DA turns use `tool_choice="required"`; other turns use `tool_choice="auto"`. Type A/B/C question routing + evidence-vs-knowledge rule in system instruction. See [Orchestration Capabilities §5.4](../investigation-engine/orchestration-capabilities.md#54-tool-augmented-generation-v50--v60). |
| Evidence `original_filename` | **Implemented** | Set during `_preprocess_attachment()`, displayed by `search_file` tool instead of opaque evidence ID. |
| Diagnostic Reasoning Validator | **Implemented** | Validates agent responses for OBSERVATION + ANALYSIS structure, evidence grounding (≥2 of 4 categories), causal reasoning. Self-correction retry with single attempt. DA causal reasoning downgrade. Knowledge queries skip validation entirely. See [Error Handling §3.2](../investigation-engine/error-handling-and-recovery.md#32-reasoning-validation-with-self-correction). |
| Context Sliding Window | **Implemented** | Structural indexes in LLM context (Tier A: recent full, Tier B: older summary, Tier C: user text summary). `role="orientation"` in DA mode. `searchable="true"` attribute on evidence with raw files on disk. |
| Evidence Form (Payload-driven) | **Implemented** | `_determine_evidence_form()` and `SubmissionClassification` deleted. Form set by payload context. |
| Evidence Classification | **Implemented** | Single-phase creation with LLM evaluation |
| Evidence Failure Modes | Design Complete | Async retry, orphan cleanup designed; deferred to post-MVP |
| Page Capture Pipeline | **Implemented** | Stage 1: Semantic DOM extraction via `htmlToStructuredText` (copilot), backend pass-through for `source_type=page_capture`. Stage 2: Query-time section reranking in `context_builder.py` — scores page capture sections against user query, promotes relevant content before char-cap truncation. |
| Platform-Specific Extractors | Planned | Future enhancement for SRE/DevOps tool parsing. Generic extraction (Stage 1) handles most dashboard patterns via tryKeyValue/tryStatValue. |
| Coverage Metadata (Tier 1) | **Implemented** | All 10 extractors append `--- COVERAGE METADATA ---` with key-value pairs (Lines, Time range, Format, etc.) |
| Orchestration Hardening | **Implemented** | R3: coverage gap detection, R4: per-evidence DA failure tracking + auto-vectorization, R5: 30K char context budget with compression |
| Pattern Learning System | Planned | Adaptive classification from user corrections (Phase 3) |
