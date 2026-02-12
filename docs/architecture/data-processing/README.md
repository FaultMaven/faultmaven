# Data Processing

Documentation for FaultMaven's data ingestion, preprocessing, and classification systems.

## Evidence Classification Redesign (v2.0) ✅ IMPLEMENTED

**Implementation Date:** 2026-02-11

### Core Documents

- **[Evidence Classification - Final Design](./EVIDENCE-CLASSIFICATION-FINAL-DESIGN.md)** - Complete design specification (IMPLEMENTED)
- **[Evidence Flow Architecture](./EVIDENCE-FLOW-ARCHITECTURE.md)** - System architecture and flow diagrams (IMPLEMENTED)
- **[Evidence Redesign Changelog](./EVIDENCE-REDESIGN-CHANGELOG.md)** - Implementation summary and breaking changes
- **[Evidence Redesign Implementation Plan](./EVIDENCE-REDESIGN-IMPLEMENTATION-PLAN.md)** - 8-phase implementation plan
- **[Evidence Creation Failure Modes](./EVIDENCE-CREATION-FAILURE-MODES.md)** - Failure handling design (deferred to post-MVP)

### Key Changes in v2.0

- **Single-phase evidence creation** - Evidence created after LLM evaluation (no UNCLASSIFIED placeholders)
- **5 evidence categories** - SYMPTOM, CAUSAL, RESOLUTION, CONTEXTUAL, REJECTED
- **5 simplified source types** - LOGS, METRICS, CONFIGURATION, VISUAL, USER_DESCRIPTION (down from 12)
- **Option 2.5 milestone attribution** - System-inferred with optional LLM override
- **Content-based classification** - Classify based on data content, not investigation phase

### Archive

- **[Archive (2026-02)](./archive/2026-02/)** - Implementation summaries and design discussions

---

## Three-Tier Processing Model

FaultMaven uses a **three-tier data preprocessing model** to balance cost, speed, and depth of analysis:

| Tier | Purpose | Runs | LLM Calls | Cost |
|------|---------|------|-----------|------|
| **Tier 0: Classification** | Rule-based data type detection | Always (<100ms) | 0 | $0 |
| **Tier 1: Mechanical Extraction** | Type-specific structural indexing | Always (<2s) | 0 | $0 |
| **Tier 2: Deep Analysis** | LLM-powered analysis of raw files | On-demand | 1-25 | $0.003-$0.05 |

## Documents

- **[Data Preprocessing Design Specification v3.0](./data-preprocessing-design-specification.md)** — Core three-tier preprocessing architecture. Defines Tier 0 classification, Tier 1 mechanical extraction (structural index, statistical profile, AST extraction), Tier 2 pluggable deep analysis service, and output schemas.

- **[Data Submission Design v4.1](./data-submission-design.md)** — API/UX layer for user data submission. Defines the two submission paths (explicit upload via `/data`, implicit paste detection via `/queries`), frontend conversation integration, and backend pipeline wiring.

- **[Data Classification Strategy v1.2](./data-classification-strategy.md)** — Tier 0 classification rules. Comprehensive pattern matching, disambiguation strategies, confidence scoring, command output detection, and multi-tier fallback chain for determining data types.

- **[Platform-Specific Extractors](./platform-specific-extractors.md)** — Future enhancement: platform-aware extraction for SRE/DevOps tools (Datadog, Grafana, PagerDuty, etc.). Can integrate as Tier 1 frontend extractors or Tier 2 backends.

---

## Purpose

This section documents how FaultMaven ingests user-submitted data, preprocesses and classifies it, and extracts insights from various formats (logs, metrics, configs, code, images, etc.). The system performs two distinct but related classification tasks:

1. **Data type classification** (Tier 0) - Automatic detection of data types (logs, metrics, configs, etc.)
2. **Evidence classification** (v2.0) - LLM-based categorization into SYMPTOM/CAUSAL/RESOLUTION/CONTEXTUAL/REJECTED

The tiered preprocessing model ensures every file is instantly queryable (Tier 1) while controlling costs by only running deep LLM analysis (Tier 2) on files that the investigation actually needs.
