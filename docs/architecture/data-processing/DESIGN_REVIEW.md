# Data Processing Design Review

**Reviewer:** Claude (AI-assisted architectural review)
**Date:** 2026-02-13
**Scope:** All documents in `docs/architecture/data-processing/`
**Method:** Cross-referenced 7 design documents against actual codebase implementation

---

## Executive Summary

The data processing design documentation is thorough and well-structured for a design-phase specification. The three-tier preprocessing model (Tier 0 Classification → Tier 1 Mechanical Extraction → Tier 2 Deep Analysis) is architecturally sound in its intent to balance cost, latency, and analysis depth. However, this review identifies **14 design flaws** and **11 gaps** across the 7 documents, ranging from critical schema/enum misalignments between documentation and implementation to missing designs for concurrency, multi-tenancy, and observability in the preprocessing pipeline.

The most urgent issues are the DataType enum mismatch (6 types in docs vs 12 in implementation), the conflation of two distinct tables (`evidence` vs `evidence_artifacts`), and the absence of a content-hash-based deduplication implementation despite extensive schema preparation.

---

## 1. CRITICAL: DataType Enum Mismatch — Documentation vs Implementation

**Documents affected:** All 7 documents (the taxonomy is foundational)

**The flaw:** All documentation specifies a 6-value `DataType` enum:
```
LOGS, METRICS, CONFIGURATION, CODE, TEXT, IMAGE
```

The actual codebase (`faultmaven/models/api.py:57-80`) defines a **12-value** enum:
```
LOGS_AND_ERRORS, UNSTRUCTURED_TEXT, STRUCTURED_CONFIG,
METRICS_AND_PERFORMANCE, SOURCE_CODE, VISUAL_EVIDENCE,
TRACE_DATA, PROFILING_DATA, ERROR_REPORT, DOCUMENTATION,
COMMAND_OUTPUT, UNANALYZABLE
```

**Impact:**
- The documented classification rules, disambiguation logic, and subtype metadata are all designed around 6 types. The implementation's 12-type approach flattens several things the docs handle via `metadata.subtype` (e.g., `TRACE_DATA`, `PROFILING_DATA`, `COMMAND_OUTPUT`, `ERROR_REPORT`) into first-class enum members.
- The data-classification-strategy.md documents elaborate disambiguation functions like `disambiguate_profiling_vs_trace()` that map to `LOGS` or `METRICS` subtypes — but these are unnecessary in the implementation, which has dedicated `PROFILING_DATA` and `TRACE_DATA` types.
- The enum naming conventions also differ: docs use short names (`LOGS`), implementation uses descriptive names (`LOGS_AND_ERRORS`).

**Recommendation:** Resolve which taxonomy is canonical. If the implementation's 12-type approach is preferred (it is more explicit and avoids subtype indirection), update all documentation. If the docs' 6-type approach is the target state, plan the implementation migration. The current state where neither matches the other will cause confusion for any implementer.

---

## 2. CRITICAL: Evidence Table Schema Mismatch

**Documents affected:** evidence-classification-design.md, evidence-flow-architecture.md, evidence-failure-modes.md

**The flaw:** Documentation consistently refers to an `evidence` table with columns like `category` (SYMPTOM_EVIDENCE/CAUSAL_EVIDENCE/etc.), `data_type`, `form`, `content_hash`, `collected_at_turn`, `advances_milestones`, and `related_hypotheses`. The actual database uses an `evidence_artifacts` table (defined in `faultmaven/modules/case/domain/owned_models/evidence.py`) with a substantially different schema:

| Documented Schema (`evidence`) | Actual Schema (`evidence_artifacts`) |
|---|----|
| `category` (5 evidence categories) | `evidence_type` (EvidenceArtifactType — 12 file-format types) |
| `data_type` (6 DataType values) | No equivalent column |
| `form` (DOCUMENT / USER_INPUT) | No equivalent column |
| `content_hash` (SHA-256) | `content_hash` exists in migration but not in domain model |
| `collected_at_turn` (int) | No equivalent column |
| `advances_milestones` (list) | No equivalent column |
| `related_hypotheses` (list) | No equivalent column |
| `primary_purpose` (text) | `description` (optional text) |
| `extraction_method` (text) | No equivalent column |

The actual `EvidenceArtifactType` enum tracks file format (`SCREENSHOT`, `LOG_FILE`, `NETWORK_TRACE`, `CODE_SNIPPET`, `HAR_FILE`, `CRASH_DUMP`, etc.), which is an orthogonal concern to the documented `EvidenceCategory` (investigation purpose: `SYMPTOM_EVIDENCE`, `CAUSAL_EVIDENCE`, etc.).

**Impact:** An implementer following the documentation would build against a schema that doesn't exist. The recent migration (`20260211_0532_evidence_classification_redesign.py`) adds `content_hash` and `collected_at_turn` columns to an existing schema, but the business logic to populate them is not yet implemented. The documents describe a system where `evidence` is a single table for "analyzed submissions" (both accepted and rejected), but the implementation's `evidence_artifacts` is structured as a file-storage metadata table.

**Recommendation:** Decide whether the documented `evidence` table should replace or augment `evidence_artifacts`. If augmenting, document the relationship between the two explicitly. The most likely resolution is that `evidence_artifacts` handles physical file storage metadata while a new `evidence` table (or additional columns on `evidence_artifacts`) handles the investigation-semantic fields (`category`, `advances_milestones`, `related_hypotheses`).

---

## 3. HIGH: EvidenceCategory Enum Confusion

**Documents affected:** evidence-classification-design.md, evidence-flow-architecture.md

**The flaw:** There are three different "evidence category" enum definitions across docs and code:

1. **Documentation** (`evidence-classification-design.md`): 5 values — `SYMPTOM_EVIDENCE`, `CAUSAL_EVIDENCE`, `RESOLUTION_EVIDENCE`, `CONTEXTUAL_EVIDENCE`, `REJECTED`
2. **Domain model** (`modules/case/domain/models.py`): Same 5 values — matches docs
3. **Persistence layer** (`infrastructure/persistence/models.py`): `EvidenceCategoryEnum` with 7 different values — `LOGS_AND_ERRORS`, `STRUCTURED_CONFIG`, `METRICS_AND_PERFORMANCE`, `UNSTRUCTURED_TEXT`, `SOURCE_CODE`, `VISUAL_EVIDENCE`, `UNKNOWN`

The persistence-layer enum is actually a **DataType** classification masquerading as an evidence category. It classifies *what kind of data* rather than *what investigation purpose the evidence serves*.

**Impact:** The two concepts (data type vs investigation purpose) are conflated in the persistence layer. Queries against the database using `EvidenceCategoryEnum.LOGS_AND_ERRORS` will not return "all symptom evidence" — they return "all log files." This breaks the documented analytics queries (e.g., "acceptance rate", "evidence breakdown by category").

**Recommendation:** The persistence-layer `EvidenceCategoryEnum` should be either (a) renamed to `EvidenceDataTypeEnum` if it's tracking data type, or (b) replaced with the documented 5-value `EvidenceCategory` if it's tracking investigation purpose. Likely both enums are needed — one for data classification, one for evidence classification.

---

## 4. HIGH: Preprocessing Output Model Mismatch

**Documents affected:** data-preprocessing-design-specification.md

**The flaw:** The documented `PreprocessingResult` model includes fields like `structural_index`, `content_ref`, `content_hash`, `extraction_method`, and `compression_ratio`. The actual implementation (`faultmaven/models/api.py`) defines `PreprocessedData` with a different structure:

| Documented `PreprocessingResult` | Actual `PreprocessedData` |
|---|---|
| `structural_index` (full extraction) | `content` (extracted/formatted content) |
| `summary` (<500 chars) | No dedicated summary field |
| `content_ref` (raw file storage ref) | Not present |
| `content_hash` (SHA-256) | Not present |
| `compression_ratio` (float) | Not present |
| `extraction_method` (string) | Via `metadata.extraction_strategy` |
| `temp_id` (temporary ID) | Not present |

The actual model uses a nested `ExtractionMetadata` object and a `SourceMetadata` object that don't exist in the documentation.

**Impact:** The integration contract between preprocessing and evidence creation described in Section 2.2 of the spec cannot work as documented. The `process_upload()` → `classify_user_input()` → `create_evidence()` flow depends on `PreprocessingResult` fields that the actual `PreprocessedData` doesn't provide.

**Recommendation:** Align the output model. The documented `PreprocessingResult` is richer and better designed (explicit `structural_index` + `summary` separation, `content_hash` for dedup). Consider it the target and update the implementation to match.

---

## 5. HIGH: Duplicate WEAK_INDICATORS Key in Classification Strategy

**Document affected:** data-classification-strategy.md

**The flaw:** In the Level 3: Weak Indicators section, `DataType.TEXT` appears as a dictionary key twice:

```python
WEAK_INDICATORS = {
    DataType.TEXT: [
        ("markdown_syntax", 0.3),
        ("prose_paragraphs", 0.2),
        ("code_blocks", 0.2),
        ("section_headings", 0.3),
    ],
    DataType.TEXT: [  # DUPLICATE KEY - overwrites the first!
        ("no_clear_structure", 0.5),
        ("mixed_formats", 0.5),
    ],
}
```

In Python, the second assignment silently overwrites the first, so the markdown/prose/headings indicators for TEXT would be lost at runtime.

**Recommendation:** Merge into a single `DataType.TEXT` entry or introduce a separate fallback key (e.g., `DataType.TEXT` for intentional text detection and a `"FALLBACK"` key for "no clear structure").

---

## 6. HIGH: Deduplication Design Has Silent Collision Risk

**Documents affected:** evidence-failure-modes.md, evidence-classification-design.md

**The flaw:** The deduplication strategy hashes the *entire raw user message* for text submissions and *file content* for uploads. However, the `UNIQUE (case_id, content_hash)` constraint combined with the `UNIQUE (case_id, collected_at_turn)` constraint creates a problem:

1. **Turn constraint blocks multi-file turns:** The `UNIQUE (case_id, collected_at_turn)` constraint limits evidence to one piece per turn. If a future design allows multiple file uploads per turn (common UX pattern), this constraint breaks.

2. **Hash collision across cases is not prevented:** The deduplication is scoped per-case (`case_id, content_hash`). If the same log file is uploaded to two different cases, it's stored and analyzed twice. This is likely intentional but isn't discussed.

3. **Mixed-content hashing is too coarse:** Hashing the entire user message means "Here are my logs: [10000 lines]" and "Here are my logs: [10000 lines] please check" produce different hashes despite containing identical data. The document acknowledges this ("different message = different submission") but this defeats the primary deduplication goal — avoiding re-analysis of the same data.

**Recommendation:**
- Document the multi-file-per-turn limitation explicitly as a known constraint.
- For mixed content, consider a two-hash approach: hash the full message for submission tracking, and hash the extracted data portion for content deduplication. This prevents re-analysis while preserving submission-level uniqueness.
- Add a cross-case deduplication strategy for enterprise deployments where the same evidence may be relevant to multiple incidents.

---

## 7. HIGH: Missing Tier 2 Implementation in Codebase

**Documents affected:** data-preprocessing-design-specification.md (Section 6)

**The flaw:** The `ITier2AnalysisService` interface, `DeepAnalysisResult`, `AnalysisContext`, `DataExcerpt`, and all three backend implementations (`ExternalTier2Client`, `LocalTier2Service`, `BasicTier2Service`) are fully designed in documentation but have **no implementation** in the codebase. No `deep_analyze_file` agent tool exists. No `TIER2_BACKEND` configuration variable exists in settings.

The documentation describes this as a current capability ("Tier 2 is invoked by the investigation agent as a tool call during `process_turn()`"), but it's entirely unimplemented.

**Impact:** The design spec reads as if Tier 2 is an existing feature. Any implementer or stakeholder reading these docs would believe the system can perform on-demand deep analysis of raw files via LLM, when in fact the agent can only work from whatever content preprocessing extracts into memory during upload.

**Recommendation:** Clearly mark Section 6 as "Design Only — Not Yet Implemented" at the top, not just via the future-enhancement status of the platform-specific extractors document. Add a "Current State" section that explains what happens today when Tier 1 is insufficient (answer: the agent works from truncated content only).

---

## 8. MEDIUM: Classification Confidence Thresholds Are Inconsistent

**Documents affected:** data-classification-strategy.md

**The flaw:** The confidence thresholds define contradictory behavior:

```python
CONFIDENCE_THRESHOLDS = {
    'auto_accept': 0.85,
    'suggest': 0.60,
    'llm_fallback': 0.50,
    'user_required': 0.50,
}
```

Problems:
- `llm_fallback` and `user_required` are both 0.50, but the `should_use_llm_fallback()` function triggers at `< 0.50` while `should_request_user_confirmation()` also triggers at `< 0.50`. They can never both activate — if confidence is below 0.50, both conditions are true simultaneously. The flow doesn't define which takes priority.
- The multi-level fallback chain returns as soon as confidence ≥ threshold at each level. But Level 3 (Weak) returns at ≥ 0.50 while the LLM fallback triggers at < 0.50. This means LLM fallback can only trigger if Level 3 returns exactly 0.0 (no match), because any match ≥ 0.50 already returns. The "gap" between 0.50-0.59 is handled by Level 4 (Contextual) with a ≥ 0.60 threshold, but a result at 0.50-0.59 from Level 3 is returned directly without LLM or user confirmation.
- The confidence scoring model (`ConfidenceScorer`) adds bonuses (`pattern_bonus` up to 0.10, `strength_bonus` up to 0.15) on top of a base confidence. For Level 2 (base 0.90), the max is 1.15 (capped to 1.0). For Level 5 (LLM, base 0.75), the max is 1.0. But the Level 2 threshold check is at ≥ 0.85, which is below the base of 0.90 — so Level 2 will *always* pass if it produces any result, making the threshold check meaningless.

**Recommendation:** Redesign the threshold logic to be mutually exclusive and non-overlapping. Consider a single `confidence` threshold chain: ≥ 0.85 auto-accept, 0.60-0.84 suggest with option to override, 0.40-0.59 trigger LLM, < 0.40 require user input.

---

## 9. MEDIUM: Classifier Does Not Handle Concurrent Uploads

**Documents affected:** data-preprocessing-design-specification.md, evidence-flow-architecture.md

**The flaw:** The preprocessing pipeline described in the documents is purely sequential: file upload → Tier 0 → Tier 1 → store → evidence creation. There is no discussion of:

- **Concurrent uploads to the same case**: If a user uploads 5 files simultaneously, the `UNIQUE (case_id, collected_at_turn)` constraint will cause 4 of 5 inserts to fail (all would have the same turn number).
- **Race conditions on content_hash deduplication**: Two identical files uploaded simultaneously could both pass the "check for duplicate" step before either has been inserted, leading to a constraint violation on `(case_id, content_hash)`.
- **Turn number assignment**: The documents show `collected_at_turn: case.current_turn + 1` but don't explain how concurrent requests determine turn order.

**Recommendation:** Document the concurrency model explicitly. Options include: (a) serialize all uploads per case via a lock/queue, (b) use database-level `INSERT ... ON CONFLICT` for idempotent handling, (c) remove the one-evidence-per-turn constraint if it's too restrictive. The `evidence-failure-modes.md` document only covers sequential failure scenarios.

---

## 10. MEDIUM: Evidence Failure Modes Document Recommends Both Options Without Resolution

**Document affected:** evidence-failure-modes.md

**The flaw:** For LLM timeout recovery, the document presents Option A (cleanup on failure) and Option B (async retry with background job), then states "Recommended: Option B for production." However, the complete failure handling flow at the end of the document implements a **hybrid** approach that doesn't match either option cleanly:

- LLM timeout → queue retry (Option B behavior)
- LLM error (non-timeout) → delete file and fail (Option A behavior)
- DB insert failure → queue retry (Option B behavior)

This hybrid is reasonable but isn't explicitly identified as a distinct Option C. The "Recommended: Option B" statement is misleading because Option A is also used for non-timeout LLM errors.

Additionally, the document is labeled "Deferred to post-MVP" but contains detailed implementation code that reads as current design. This creates ambiguity about what should be implemented now vs later.

**Recommendation:** Explicitly define the hybrid strategy as the recommended approach. Separate the document into "MVP" (basic try/catch with user retry) and "Post-MVP" (async retry infrastructure, background jobs, orphaned file cleanup) sections.

---

## 11. MEDIUM: No Versioning Strategy for Structural Indexes

**Documents affected:** data-preprocessing-design-specification.md

**The flaw:** When the classification algorithm or extraction logic changes (e.g., adding new error cluster patterns, changing severity scoring), previously processed files have stale structural indexes in the vector DB. The design has no mechanism for:

- **Re-indexing**: Triggering re-extraction when extraction logic changes
- **Schema versioning**: Tracking which version of the extractor produced each index
- **Migration**: Updating old indexes to match new extraction schemas
- **Staleness detection**: Knowing that a file was processed with v1 extractor when v2 is current

**Recommendation:** Add an `extractor_version` field to both the `PreprocessingResult` and the vector DB metadata. Define a re-indexing strategy (e.g., background batch job on version mismatch, lazy re-index on next query).

---

## 12. MEDIUM: Image Processing Is Effectively a No-Op Until Tier 2

**Documents affected:** data-preprocessing-design-specification.md, evidence-classification-design.md

**The flaw:** Tier 1 image processing extracts only metadata (format, dimensions, EXIF). Since Tier 2 is not implemented, uploaded screenshots and diagrams contribute almost nothing to the investigation. The agent receives "Image: screenshot.png (PNG, 1920x1080, 245760 bytes). Vision analysis available via Tier 2 deep analysis." — which is not actionable.

This is particularly problematic because screenshots of dashboards, error messages, and stack traces are common troubleshooting evidence in the SRE/DevOps domain this tool targets.

**Recommendation:** For MVP, consider a lightweight OCR step in Tier 1 (e.g., `pytesseract` for text extraction from screenshots) that runs within the 2-second budget. This would make screenshots of error messages and terminal output immediately useful without requiring Tier 2. Document this as a Tier 1 enhancement rather than waiting for full Tier 2 vision support.

---

## 13. MEDIUM: Platform-Specific Extractors Document Lacks Integration Design

**Document affected:** platform-specific-extractors.md

**The flaw:** The document describes frontend extraction from SRE platforms (Datadog, Grafana, PagerDuty) but doesn't address how this structured data integrates with the three-tier model:

- How does platform-extracted structured data map to `DataType`? Is Datadog dashboard data `METRICS`, `IMAGE`, or a new type?
- How does the `structured_data` field in the proposed payload interact with Tier 0 classification? Is classification skipped when platform data is pre-structured?
- How are platform-specific fields (dashboard_id, widget alerts) stored in the evidence schema?
- What happens when the frontend extractor fails (platform DOM changes)? The document mentions this risk but doesn't design a fallback.

**Recommendation:** If this is deferred to post-MVP, that's fine — but add a "Future Integration Points" section to the preprocessing spec that identifies where platform extractors would plug in. Specifically: (a) platform-extracted data bypasses Tier 0, (b) platform-specific metadata stored in `extraction_metadata`, (c) fallback to generic HTML extraction if platform extractor fails.

---

## 14. LOW: Learning/Adaptation System Is Over-Designed for MVP

**Document affected:** data-classification-strategy.md (Section 10)

**The flaw:** The `AdaptiveClassifier` and `PatternLearner` classes describe a feedback-driven system where:
- User corrections are stored with similarity search
- LLM extracts new regex patterns from misclassified data
- Learned patterns are added to the classification database at lower weight
- Pattern effectiveness is A/B tested

This is a significant system (feedback storage, similarity search, LLM-based pattern extraction, pattern versioning, A/B testing) that is not implemented and has no clear path to implementation. It's also potentially dangerous: LLM-generated regex patterns injected into classification rules could cause regressions if not carefully validated.

**Recommendation:** Move this entire section to a separate "Future: Adaptive Classification" design document. For MVP, a simpler feedback mechanism (logging misclassifications for manual review, periodic batch updates to rules) would be more appropriate.

---

## 15. LOW: No Design for Text/Query Input Preprocessing

**Documents affected:** data-preprocessing-design-specification.md, data-classification-strategy.md

**The flaw:** The entire preprocessing pipeline is designed for file uploads (`form=DOCUMENT`). The `form=USER_INPUT` path (typed text via the `/queries` endpoint) is mentioned in passing but has no preprocessing design:

- How is typed text classified? The `QueryClassifier` is referenced as "not yet implemented as a standalone module."
- Does typed text go through Tier 0 + Tier 1? The documents imply no (`Evidence.extraction_method = "none" for USER_INPUT form`).
- If a user pastes a 5000-line log into the chat input (vs uploading it as a file), does it receive the same structural indexing?

The `evidence-classification-design.md` shows `SubmissionClassification` with types `USER_CHAT`, `EXTERNAL_DATA`, and `MIXED` — but the preprocessing pipeline has no concept of `MIXED` content processing.

**Recommendation:** Add a section on text/query preprocessing, even if minimal. At minimum: (a) define the `QueryClassifier` interface, (b) specify what happens when pasted text exceeds a threshold (treat as implicit file upload?), (c) explain how `MIXED` submissions are split into chat + data portions.

---

## 16. LOW: Mermaid Diagram References Incorrect Document Version

**Document affected:** data-classification-strategy.md

**The flaw:** The document references "Data Preprocessing v3.0" throughout, but the actual preprocessing specification file is titled "v3.0" in its header while the README references "v3.1". Minor version inconsistency across cross-references.

**Recommendation:** Standardize version references across all documents after this review is complete.

---

## 17. LOW: Evidence Flow Architecture Has Inconsistent HTTP Status Codes

**Document affected:** evidence-flow-architecture.md

**The flaw:** The LLM timeout scenario sequence diagram shows the API returning `202 Accepted` for async processing, which is correct. But the complete failure handling flow in `evidence-failure-modes.md` returns a `TurnResponse` object with `status="analyzing"` through the normal `200 OK` path. There's no documented contract for how the frontend distinguishes between:
- `200` with evidence (success)
- `200` with `status="duplicate"` (duplicate detected)
- `200` with `status="analyzing"` (async processing)
- `200` with `status="processing"` (DB failure, retry queued)

**Recommendation:** Either use distinct HTTP status codes (`200` for success, `202` for async, `409` for duplicate) or define a consistent response envelope with a `status` field. Document this API contract explicitly.

---

## 18. MEDIUM: No Multi-Tenancy Consideration in Preprocessing

**Documents affected:** data-preprocessing-design-specification.md, evidence-flow-architecture.md

**The flaw:** The preprocessing pipeline has no multi-tenancy design. The actual `EvidenceArtifact` model includes `user_id` and `organization_id` fields, but the documented preprocessing flow and vector DB storage don't account for organization-scoped isolation:

- Vector DB queries use `case_id` for scoping, but don't filter by `organization_id`. A bug in case_id assignment could leak evidence across organizations.
- The S3 storage paths don't include organization-level prefixes for data isolation.
- The deduplication hash check (`case_id, content_hash`) could theoretically be extended to organization-level dedup, but this isn't discussed.

**Recommendation:** Add organization-scoping to the vector DB storage metadata and query filters. Document the storage isolation model (organization-prefixed S3 paths or separate ChromaDB collections per organization).

---

## 19. HIGH: Missing Observability in Preprocessing Pipeline

**Documents affected:** data-preprocessing-design-specification.md

**The flaw:** The documents describe performance targets for Tier 0 (<100ms) and Tier 1 (<2s) but specify no metrics, logging, or alerting for the preprocessing pipeline itself. The `evidence-flow-architecture.md` defines metrics for evidence creation (e.g., `evidence.created.total`) but nothing for preprocessing:

- No metric for classification accuracy or confidence distribution
- No metric for extraction time by data type
- No metric for fallback rate (how often extractors fall back to TEXT)
- No metric for sanitization redaction rates
- No metric for vector DB storage failures
- No alerting for extractor timeout rates

The `processing_time_ms` field exists in the output models but is never described as being aggregated or monitored.

**Recommendation:** Add a "Preprocessing Observability" section defining:
- `preprocessing.classification.confidence{data_type}` — histogram
- `preprocessing.extraction.duration_ms{data_type, method}` — histogram
- `preprocessing.extraction.fallback_rate{data_type}` — counter
- `preprocessing.sanitization.redactions{type}` — counter
- `preprocessing.vectordb.storage_failures` — counter
- Alert: extraction fallback rate > 10% (indicates classifier or extractor degradation)

---

## Summary of Findings

| # | Severity | Category | Title |
|---|----------|----------|-------|
| 1 | CRITICAL | Schema Mismatch | DataType enum: 6 types (docs) vs 12 types (code) |
| 2 | CRITICAL | Schema Mismatch | Evidence table: `evidence` (docs) vs `evidence_artifacts` (code) |
| 3 | HIGH | Schema Mismatch | EvidenceCategory confusion across 3 different enums |
| 4 | HIGH | Schema Mismatch | PreprocessingResult (docs) vs PreprocessedData (code) |
| 5 | HIGH | Code Bug | Duplicate `DataType.TEXT` key in WEAK_INDICATORS |
| 6 | HIGH | Design Flaw | Deduplication collision risk and single-file-per-turn constraint |
| 7 | HIGH | Implementation Gap | Tier 2 described as current capability but unimplemented |
| 8 | MEDIUM | Design Flaw | Classification confidence thresholds are inconsistent/overlapping |
| 9 | MEDIUM | Design Gap | No concurrent upload handling |
| 10 | MEDIUM | Documentation | Failure modes doc recommends Option B but implements hybrid |
| 11 | MEDIUM | Design Gap | No structural index versioning strategy |
| 12 | MEDIUM | Design Gap | Image processing is no-op without Tier 2 |
| 13 | MEDIUM | Design Gap | Platform extractors lack integration design |
| 14 | LOW | Over-Design | Adaptive classification system premature for MVP |
| 15 | LOW | Design Gap | No text/query input preprocessing design |
| 16 | LOW | Documentation | Inconsistent version references across documents |
| 17 | LOW | Design Flaw | Inconsistent HTTP status code contract for async responses |
| 18 | MEDIUM | Design Gap | No multi-tenancy consideration in preprocessing |
| 19 | HIGH | Design Gap | No observability design for preprocessing pipeline |

### Recommended Priority Order

1. **Resolve DataType enum mismatch** (#1) — blocks all downstream work
2. **Resolve evidence table identity** (#2) — critical for implementation
3. **Clarify EvidenceCategory vs persistence-layer enums** (#3)
4. **Align PreprocessingResult output model** (#4)
5. **Fix duplicate dict key** (#5) — trivial fix
6. **Add preprocessing observability design** (#19)
7. **Address concurrent upload handling** (#9)
8. **Design structural index versioning** (#11)
9. **Add lightweight image OCR to Tier 1** (#12)
10. Remaining items in severity order

---

**Review Version:** 1.0
**Review Date:** 2026-02-13
