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

## Data Preprocessing & Classification

### Active Documents

- **[Data Classification Strategy](./data-classification-strategy.md)** - Automatic data type classification algorithms
- **[Data Preprocessing Design Specification](./data-preprocessing-design-specification.md)** - Data preprocessing pipeline architecture
- **[Data Submission Design](./data-submission-design.md)** - File upload and submission processing (10K limit, async/sync)
- **[Platform-Specific Extractors](./platform-specific-extractors.md)** - Platform-specific data extraction patterns

---

## Purpose

This section documents how FaultMaven:

1. **Ingests user submissions** - File uploads and text submissions with preprocessing
2. **Classifies evidence** - LLM-based categorization into SYMPTOM/CAUSAL/RESOLUTION/CONTEXTUAL/REJECTED
3. **Processes data** - Extraction, transformation, and indexing for investigation
4. **Handles failures** - Retry strategies, deduplication, and error recovery
