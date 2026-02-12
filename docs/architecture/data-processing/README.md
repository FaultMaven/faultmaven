# Data Processing

Documentation for FaultMaven's data ingestion, preprocessing, and classification systems.

## Overview

This section documents how FaultMaven ingests user-submitted data, preprocesses and classifies it, and extracts insights from various formats (logs, metrics, configs, code, images, etc.). The system performs two distinct but related classification tasks:

1. **Data type classification** (Tier 0) — Automatic detection of data types (logs, metrics, configs, etc.)
2. **Evidence classification** (v2.0) — LLM-based categorization into SYMPTOM/CAUSAL/RESOLUTION/CONTEXTUAL/REJECTED

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

## Documents

### Data Preprocessing

- **[Data Preprocessing Design Specification v3.0](./data-preprocessing-design-specification.md)** — Core three-tier preprocessing architecture. Defines Tier 0 classification, Tier 1 mechanical extraction (structural index, statistical profile, AST extraction), Tier 2 pluggable deep analysis service, and output schemas.

- **[Data Classification Strategy v1.2](./data-classification-strategy.md)** — Tier 0 classification rules. Comprehensive pattern matching, disambiguation strategies, confidence scoring, command output detection, and multi-tier fallback chain for determining data types.

- **[Platform-Specific Extractors](./platform-specific-extractors.md)** — Future enhancement: platform-aware extraction for SRE/DevOps tools (Datadog, Grafana, PagerDuty, etc.). Can integrate as Tier 1 frontend extractors or Tier 2 backends.

### Evidence Classification (v2.0)

- **[Evidence Classification Design](./evidence-classification-design.md)** — Complete design specification for evidence classification. Defines the 5 evidence categories (SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL, REJECTED), 5 source types, single-phase creation flow, content-based classification, and milestone attribution (Option 2.5).

- **[Evidence Flow Architecture](./evidence-flow-architecture.md)** — System architecture and flow diagrams for the evidence pipeline. Covers file upload through LLM evaluation to persistence, including sequence diagrams, state machines, and monitoring.

- **[Evidence Failure Modes](./evidence-failure-modes.md)** — Failure handling design for single-phase evidence creation. Covers LLM timeout recovery, DB insert retries, storage cleanup, and deduplication strategies. *(Deferred to post-MVP)*
