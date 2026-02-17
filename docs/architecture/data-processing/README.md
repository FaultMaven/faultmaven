# Data Processing

Documentation for FaultMaven's data ingestion, preprocessing, and classification systems.

## Overview

This section documents how FaultMaven ingests user-submitted data, preprocesses and classifies it, and extracts insights from various formats. The system uses a **unified DataType taxonomy** (6 types: LOGS, METRICS, CONFIGURATION, CODE, TEXT, IMAGE) shared across all components.

Two distinct but related classification tasks are performed:

1. **Data type classification** (Tier 0) — Rule-based detection producing a `DataType` enum value
2. **Evidence classification** — LLM-based categorization into SYMPTOM/CAUSAL/RESOLUTION/CONTEXTUAL/REJECTED

The tiered preprocessing model ensures every file is instantly queryable (Tier 1) while controlling costs by only running deep LLM analysis (Tier 2) on files that the investigation actually needs.

---

## Three-Tier Processing Model

FaultMaven uses a **three-tier data preprocessing model** to balance cost, speed, and depth of analysis:

| Tier | Purpose | Runs | LLM Calls | Cost |
|------|---------|------|-----------|------|
| **Tier 0: Classification** | Rule-based data type detection | Always (<100ms) | 0 | $0 |
| **Tier 1: Mechanical Extraction** | Type-specific structural indexing | Always (<2s) | 0 | $0 |
| **Tier 2: Deep Analysis** | LLM-powered analysis of raw files | On-demand | 1-25 | $0.003-$0.05 |

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

- **[Data Preprocessing Design Specification v3.1](./data-preprocessing-design-specification.md)** — Core three-tier preprocessing architecture. Defines Tier 0 classification, Tier 1 mechanical extraction (structural index, statistical profile, AST extraction), Tier 2 pluggable deep analysis service, and output schemas including the unified DataType enum.

- **[Data Classification Strategy v2.0](./data-classification-strategy.md)** — Tier 0 classification rules. Multi-level pattern matching (Level 1-3 heuristics, Level 4 contextual, optional Level 5 LLM), disambiguation strategies, confidence scoring, and command output detection.

- **[Platform-Specific Extractors](./platform-specific-extractors.md)** — Future enhancement: platform-aware extraction for SRE/DevOps tools (Datadog, Grafana, PagerDuty, etc.). Can integrate as Tier 1 frontend extractors or Tier 2 backends.

### Evidence Classification

- **[Evidence Classification Design](./evidence-classification-design.md)** — Evidence taxonomy: 5 categories (SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL, REJECTED), unified DataType, single-phase creation flow, content-based classification, and milestone attribution (Option 2.5).

- **[Evidence Flow Architecture](./evidence-flow-architecture.md)** — System architecture and flow diagrams for the evidence pipeline. Covers file upload through Tier 0+1 preprocessing, LLM evaluation, to persistence, including sequence diagrams, state machines, and monitoring.

- **[Evidence Failure Modes](./evidence-failure-modes.md)** — Failure handling design for single-phase evidence creation. Covers LLM timeout recovery, DB insert retries, storage cleanup, and deduplication strategies. *(Deferred to post-MVP)*

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| Three-Tier Processing Model (Tier 0+1) | Implemented | Classification + mechanical extraction at upload |
| Tier 2: Deep Analysis | Partial | On-demand LLM analysis; pluggable backend interface defined, limited backends |
| Evidence Classification | Implemented | Single-phase creation with LLM evaluation |
| Evidence Flow Architecture | Implemented | File upload through persistence pipeline operational |
| Evidence Failure Modes | Design Complete | Async retry, orphan cleanup designed; deferred to post-MVP |
| Platform-Specific Extractors | Planned | Future enhancement for SRE/DevOps tool parsing |
| Pattern Learning System | Planned | Adaptive classification from user corrections (Phase 3) |
