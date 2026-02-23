# Data Processing

Documentation for FaultMaven's data ingestion, preprocessing, and classification systems.

## Overview

This section documents how FaultMaven ingests user-submitted data, preprocesses and classifies it, and extracts insights from various formats. The system uses a **unified DataType taxonomy** (6 types: LOGS, METRICS, CONFIGURATION, CODE, TEXT, IMAGE) shared across all components.

Two distinct but related classification tasks are performed:

1. **Data type classification** (Tier 0) — Rule-based detection producing a `DataType` enum value
2. **Evidence classification** — LLM-based categorization into SYMPTOM/CAUSAL/RESOLUTION/CONTEXTUAL/REJECTED

All user turns arrive via the **Unified Ingestion Pipeline** (`POST /cases/{id}/turns`). Attachments are preprocessed through Tier 0+1 **before** the LLM runs (Step 1), then the LLM performs inference with structural indexes included in context (Step 2). Evidence form is determined by payload context (attachments → `DOCUMENT`, agent findings → `SUBMITTED_DATA`), not by LLM classification.

---

## Four-Tier Processing Model

FaultMaven uses a **four-tier data preprocessing model** to balance cost, speed, and depth of analysis:

| Tier | Purpose | Runs | LLM Calls | Cost |
|------|---------|------|-----------|------|
| **Tier 0+1: Structural Indexing** | Classification + type-specific extraction | Always (<2s) | 0 | $0 |
| **Tier 2: Mechanical Search** | `search_file` tool — grep/regex on raw files | On-demand | 0 | $0 |
| **Tier 3: Deep LLM Analysis** | `deep_analyze_file` tool — LLM interprets data | On-demand | 1 | ~$0.01-$0.05 |
| **Tier 4: Vectorization** | `vectorize_file` tool — chunk, embed, store | On-demand (rare) | 0 (embed only) | ~$0.05-$0.50 |

---

## Unified DataType Enum

All documents in this section share a single DataType taxonomy:

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

- **[Data Preprocessing Design Specification v4.1](./data-preprocessing-design-specification.md)** — Core four-tier preprocessing architecture. Defines Tier 0+1 structural indexing (12 detailed types → 6 unified types, 11 extractors), Tier 2 mechanical search (`search_file`), Tier 3 deep LLM analysis (`deep_analyze_file`), Tier 4 on-demand vectorization, unified ingestion pipeline (`POST /cases/{id}/turns`), Context Sliding Window, and evidence form determination.

- **[Data Classification Strategy v2.0](./data-classification-strategy.md)** — Tier 0 classification rules. Multi-level pattern matching (Level 1-3 heuristics, Level 4 contextual, optional Level 5 LLM), disambiguation strategies, confidence scoring, and command output detection.

- **[Platform-Specific Extractors](./platform-specific-extractors.md)** — Future enhancement: platform-aware extraction for SRE/DevOps tools (Datadog, Grafana, PagerDuty, etc.). Can integrate as Tier 1 frontend extractors or Tier 3 backends.

### Evidence Classification

- **[Evidence Classification Design](./evidence-classification-design.md)** — Evidence taxonomy: 5 categories (SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL, REJECTED), unified DataType, payload-driven form determination (DOCUMENT/USER_TEXT/SUBMITTED_DATA), content-based classification, and milestone attribution (Option 2.5).

- **[Evidence Flow Architecture](./evidence-flow-architecture.md)** — System architecture and flow diagrams for the evidence pipeline. Covers the unified turn endpoint (`POST /cases/{id}/turns`) through two-step pipeline (preprocess attachments → LLM inference), to persistence, including sequence diagrams, state machines, and monitoring.

- **[Evidence Failure Modes](./evidence-failure-modes.md)** — Failure handling design for single-phase evidence creation. Covers LLM timeout recovery, DB insert retries, storage cleanup, and deduplication strategies. *(Deferred to post-MVP)*

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| Unified Ingestion Pipeline | **Implemented** | `POST /cases/{id}/turns` — two-step pipeline (preprocess → LLM). Old `/queries` and `/data` endpoints deleted. |
| Tier 0+1: Structural Indexing | **Implemented** | 12 detailed types, 11 extractors, best-effort fallback. Pasted text routed through same pipeline. |
| Tier 2: Mechanical Search | **Implemented** | `search_file` agent tool — keyword/regex/extractor re-run on raw files |
| Tier 3: Deep LLM Analysis | Partial | `deep_analyze_file` tool; pluggable backend interface defined, limited backends. Config renamed to `DEEP_ANALYSIS_*`. |
| Tier 4: Vectorization | **Implemented** | On-demand via `vectorize_file` tool (was eager background in v3.2) |
| Context Sliding Window | **Implemented** | Structural indexes included in LLM context (Tier A: recent full, Tier B: older summary, Tier C: user text summary) |
| Evidence Form (Payload-driven) | **Implemented** | `_determine_evidence_form()` and `SubmissionClassification` deleted. Form set by payload context. |
| Evidence Classification | **Implemented** | Single-phase creation with LLM evaluation |
| Evidence Failure Modes | Design Complete | Async retry, orphan cleanup designed; deferred to post-MVP |
| Platform-Specific Extractors | Planned | Future enhancement for SRE/DevOps tool parsing |
| Pattern Learning System | Planned | Adaptive classification from user corrections (Phase 3) |
