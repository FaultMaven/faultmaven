# Data Processing Design Review

**Reviewer:** Claude (AI-assisted architectural review)
**Date:** 2026-02-13
**Scope:** All 7 documents in `docs/architecture/data-processing/`
**Focus:** Design soundness, internal consistency, and completeness — evaluated purely as specifications, independent of any current implementation state.

---

## Executive Summary

The data processing design is architecturally ambitious and well-structured. The three-tier preprocessing model (Tier 0 Classification, Tier 1 Mechanical Extraction, Tier 2 Deep Analysis) is a strong architectural choice that balances cost, latency, and analysis depth elegantly. The evidence classification taxonomy (5 categories + unified DataType) is well-reasoned, and the single-phase evidence creation model simplifies the lifecycle considerably compared to placeholder-based alternatives.

However, this review identifies **6 design flaws** and **9 design gaps** across the 7 documents. The most significant are: contradictory confidence threshold logic that creates unreachable code paths, a deduplication design with inherent collision risks, missing concurrency handling for the evidence pipeline, and incomplete designs for text/query input, image processing at Tier 1, and preprocessing observability. These should be resolved before implementation begins.

---

## Design Flaws

These are problems within the design itself — contradictions, logic errors, or decisions that would produce incorrect behavior if implemented exactly as specified.

---

### 1. HIGH: Duplicate Dictionary Key in WEAK_INDICATORS

**Document:** data-classification-strategy.md (Level 3: Weak Indicators)

**The flaw:** `DataType.TEXT` appears as a dictionary key twice:

```python
WEAK_INDICATORS = {
    DataType.TEXT: [
        ("markdown_syntax", 0.3),
        ("prose_paragraphs", 0.2),
        ("code_blocks", 0.2),
        ("section_headings", 0.3),
    ],
    DataType.TEXT: [  # DUPLICATE KEY
        ("no_clear_structure", 0.5),
        ("mixed_formats", 0.5),
    ],
}
```

In Python, the second assignment silently overwrites the first. The intended markdown/prose/headings indicators for detecting intentional TEXT would be lost. Only the "no clear structure" fallback would survive.

**Impact:** The classification system would have no weak indicators for detecting text documents by their positive features (markdown, paragraphs, headings). All weak-level TEXT classification would fall through to the "no clear structure" fallback, reducing accuracy for documents, runbooks, and prose — exactly the kind of evidence an SRE troubleshooting tool needs to handle well.

**Recommendation:** Merge into a single `DataType.TEXT` entry containing all 6 indicators with appropriate weights, or introduce a separate `FALLBACK_INDICATORS` dict for the "no clear match" scenario.

---

### 2. HIGH: Classification Confidence Thresholds Are Contradictory

**Document:** data-classification-strategy.md (Confidence Scoring, Multi-Level Fallback)

**The flaw:** The confidence threshold system has three interacting problems:

**Problem A — Overlapping triggers:**
```python
CONFIDENCE_THRESHOLDS = {
    'auto_accept': 0.85,
    'suggest': 0.60,
    'llm_fallback': 0.50,
    'user_required': 0.50,   # Same as llm_fallback
}
```

`should_use_llm_fallback()` and `should_request_user_confirmation()` both trigger at `< 0.50`. When both are true simultaneously, the design doesn't specify precedence. The fallback chain in `classify_with_fallback()` handles this by trying LLM first (Level 5) then user (Level 6) — but the threshold functions in the "Confidence Thresholds for Actions" section suggest they're independent decisions, not sequential fallbacks.

**Problem B — Unreachable LLM fallback:**
The fallback chain returns at Level 3 (Weak) when confidence `>= 0.50`, and triggers LLM fallback when confidence `< 0.50`. This means LLM fallback can only activate when Level 3 returns confidence 0.0 (no match at all). Any weak match of 0.50 or above short-circuits the chain. The gap between 0.50 and 0.60 is awkward: Level 3 returns (confidence ≥ 0.50), but the result sits below the "suggest" threshold (0.60) — it's neither auto-accepted, nor LLM-validated, nor user-confirmed.

**Problem C — Level 2 threshold is always satisfied:**
The `ConfidenceScorer` assigns Level 2 (Strong) a base confidence of 0.90, with bonuses up to 0.25. The fallback chain checks `result.confidence >= 0.85` for Level 2. Since the base already exceeds the threshold, every Level 2 result passes. The threshold check is dead code.

**Impact:** As designed, the LLM fallback (Level 5) is effectively unreachable for any input that triggers even a single weak indicator. This undermines the stated goal of using LLM classification for ambiguous cases — the design routes ambiguous cases (0.50-0.59 confidence) directly to the user without LLM validation.

**Recommendation:** Redesign the threshold chain with non-overlapping ranges:
- ≥ 0.85: auto-accept
- 0.60-0.84: suggest to user with override option
- 0.35-0.59: invoke LLM classification
- < 0.35: require user input

Adjust the `ConfidenceScorer` base values to align (Level 2 base could be 0.85 instead of 0.90 to make the threshold meaningful, or remove the separate check since Level 2 is inherently high-confidence by definition).

---

### 3. HIGH: Deduplication Design Has Inherent Tension

**Documents:** evidence-failure-modes.md, evidence-classification-design.md, data-preprocessing-design-specification.md

**The flaw:** The deduplication strategy has three interconnected issues:

**Issue A — One-evidence-per-turn constraint vs multi-file UX:**
The `UNIQUE (case_id, collected_at_turn)` constraint limits the system to exactly one evidence record per turn. This is stated as a "UI constraint." However:
- The design supports `MIXED` submissions (chat + data in one turn). If a mixed submission produces one evidence record, and the user also attaches a file in the same turn, the constraint would reject the second.
- The browser extension use case (page injection) could naturally produce multi-artifact turns (e.g., injecting a dashboard + attaching a related config file).
- Batch upload workflows (common in incident response: "here are all 5 relevant log files") would require 5 separate turns.

The constraint is never justified in terms of why one-per-turn is architecturally necessary vs just a current UI simplification.

**Issue B — Mixed-content hashing defeats data deduplication:**
For text submissions, the design hashes the *entire raw message*. The failure-modes doc explicitly states "different message = different submission" and calls this "correct behavior." But consider:
```
Turn 3: "Here are the logs from prod: [5000 lines of logs]"
Turn 7: "Same logs from earlier, adding context: [identical 5000 lines]"
```
These produce different hashes despite containing the same data. The LLM will analyze the same 5000 lines twice, spending tokens on duplicate analysis. The design accepts this trade-off but doesn't quantify the cost. For file uploads the design correctly hashes raw file bytes, but the text submission path provides no data-level deduplication.

**Issue C — Cross-case deduplication not addressed:**
In multi-tenant enterprise deployments, the same incident might generate multiple cases (e.g., one per team investigating). The same log file uploaded to Case A and Case B is stored, processed, and analyzed independently. For organizations with many active incidents, this can be significant. The design scopes deduplication to `(case_id, content_hash)` without discussing whether cross-case dedup is intentionally excluded or simply unaddressed.

**Recommendation:**
- For Issue A: Either remove the one-per-turn constraint (use a sequence number instead) or explicitly document it as a deliberate UX simplification with the known limitation that batch uploads require sequential turns. Explain why this is acceptable for the target workflow.
- For Issue B: Consider a two-hash approach — `submission_hash` (full message, for exact resubmission detection) and `data_hash` (extracted data portion, for content deduplication). The `data_hash` could be used for a softer "similar content already analyzed" warning rather than hard rejection.
- For Issue C: Add a section discussing cross-case dedup strategy. Even if deferred, the design should state whether cross-case dedup is out-of-scope or planned.

---

### 4. MEDIUM: Failure Modes Document Presents Contradictory Recommendations

**Document:** evidence-failure-modes.md

**The flaw:** For LLM timeout recovery, the document presents two mutually exclusive options:
- **Option A:** Cleanup on failure (delete file, user retries)
- **Option B:** Async retry with background job

It states "Recommended: Option B for production." However, the complete failure handling flow at the end of the document implements a **hybrid** that isn't discussed as a distinct option:

| Failure | Behavior | Which Option? |
|---------|----------|---------------|
| LLM timeout | Queue async retry, keep file | Option B |
| LLM error (non-timeout) | Delete file, user retries | Option A |
| DB insert failure | Queue async retry, keep LLM result | Option B |
| Unexpected error | Delete file (best-effort), raise error | Option A |

This hybrid is arguably better than either pure option — transient failures (timeout) get retries while permanent failures (LLM errors) get immediate cleanup. But the document doesn't name it, explain the rationale for the split, or discuss why timeout deserves retry but other LLM errors don't (some non-timeout LLM errors, like rate limiting, are also transient).

Additionally, the document is labeled "Deferred to post-MVP" yet contains implementation-ready code with specific retry counts, backoff delays, and cleanup schedules. This creates ambiguity about which parts of the failure handling are MVP scope.

**Recommendation:**
- Name the hybrid approach explicitly (e.g., "Option C: Transient Retry, Permanent Cleanup").
- Define which LLM errors are transient (timeout, rate limit, 5xx) vs permanent (invalid input, auth failure, model unavailable).
- Split the document into "MVP Failure Handling" (basic try/catch, user retry, no background jobs) and "Production Failure Handling" (the full async retry infrastructure).

---

### 5. MEDIUM: Milestone Inference Logic Has an Attribution Accuracy Problem

**Document:** evidence-classification-design.md (Milestone Advancement Attribution)

**The flaw:** The `CATEGORY_MILESTONE_MAP` maps evidence categories to milestones they *could* advance:

```python
CATEGORY_MILESTONE_MAP = {
    EvidenceCategory.SYMPTOM_EVIDENCE: [
        "symptom_verified", "scope_assessed",
        "timeline_established", "changes_identified",
    ],
    EvidenceCategory.CAUSAL_EVIDENCE: [
        "changes_identified", "root_cause_identified",
        "solution_proposed",
    ],
    ...
}
```

The inference logic then intersects this with milestones completed in the current turn:
```python
advances_milestones = intersection(eligible_for_category, completed_this_turn)
```

**The problem:** If Turn 5 includes both SYMPTOM_EVIDENCE (new log file) and a milestone update of `root_cause_identified` (from the LLM's reasoning about existing evidence), the intersection would NOT attribute `root_cause_identified` to the symptom evidence — which is correct. But consider:

- Turn 5: User uploads a config file showing a misconfigured connection pool
- LLM classifies as `CAUSAL_EVIDENCE`
- LLM also advances `changes_identified` AND `root_cause_identified`
- Inference: `advances_milestones = ["changes_identified", "root_cause_identified"]`

But what if the `root_cause_identified` milestone was actually advanced by the LLM's synthesis of ALL evidence so far (the config file was the final piece), not solely by this one piece of evidence? The inference attributes it entirely to the latest upload, losing the contribution of prior evidence. The design acknowledges this by saying "system infers" covers "90%" of cases, but doesn't define what happens to the accuracy of the attribution in the other 10%.

More critically: the `CATEGORY_MILESTONE_MAP` includes `solution_proposed` under `CAUSAL_EVIDENCE`. A config file showing the root cause doesn't propose a solution — the LLM proposes the solution. Attributing `solution_proposed` to causal evidence is a category error.

**Recommendation:**
- Remove `solution_proposed` from `CAUSAL_EVIDENCE`'s eligible milestones. Solution proposals come from agent reasoning, not evidence.
- Add `solution_proposed` and `solution_verified` only to `RESOLUTION_EVIDENCE`.
- Document that attribution is *correlation* not *causation* — the system records "this evidence was present when this milestone completed" not "this evidence caused this milestone to complete."
- Consider adding a `contributed_by` list to milestones (instead of `advances_milestones` on evidence) for more accurate multi-evidence attribution.

---

### 6. LOW: Inconsistent HTTP Status Codes Across Failure Scenarios

**Documents:** evidence-flow-architecture.md, evidence-failure-modes.md

**The flaw:** The two documents describe different HTTP responses for the same scenarios:

- **evidence-flow-architecture.md** (Sequence Diagram: LLM Timeout): API returns `202 Accepted`
- **evidence-failure-modes.md** (Complete Failure Handling Flow): Returns `TurnResponse(status="analyzing")` via normal `200 OK`

For duplicate detection:
- **evidence-flow-architecture.md**: Returns `200 OK` with `{status: "duplicate", evidence_ref}`
- **evidence-failure-modes.md**: Raises `DuplicateFileError` or returns `200` with `TurnResponse(status="duplicate")`

There's no single API contract that specifies the response envelope. A frontend developer would need to reconcile:
- `200` + `status="success"` + evidence object
- `200` + `status="duplicate"` + evidence reference
- `200` + `status="analyzing"` + no evidence
- `200` + `status="processing"` + no evidence
- `4xx/5xx` for hard failures

**Recommendation:** Define a single `TurnResponse` contract with clear status codes:
- `200` for synchronous success (evidence created)
- `202` for accepted-but-processing (async retry queued)
- `409` for duplicate (content already exists)
- `422` for validation failure (file too large, blocked type)
- `5xx` for server errors

Or alternatively, commit to a response-envelope approach where `200` is always returned with a `status` discriminator, and document all possible status values and their semantics.

---

## Design Gaps

These are areas where the design is incomplete — important scenarios that are not addressed, not contradicted but simply missing.

---

### 7. HIGH: No Concurrency Model for the Evidence Pipeline

**Documents:** data-preprocessing-design-specification.md, evidence-flow-architecture.md, evidence-failure-modes.md

**The gap:** The entire pipeline is designed as a sequential flow: upload → preprocess → LLM evaluate → create evidence. No document addresses what happens when multiple operations target the same case concurrently:

- **Concurrent uploads to the same case:** If a user uploads 5 files in quick succession (or multiple team members upload simultaneously), 5 `process_turn()` calls run in parallel. The `UNIQUE (case_id, collected_at_turn)` constraint will reject 4 of 5. Even without the constraint, turn number assignment is racy (`case.current_turn + 1` read by 5 threads produces the same value).

- **Dedup race condition:** Two identical files uploaded simultaneously both pass the `find_by_content_hash()` check (neither has been inserted yet), then both attempt INSERT, one fails on the `UNIQUE (case_id, content_hash)` constraint.

- **LLM evaluation with stale context:** When `process_turn()` loads case state and sends it to the LLM, a concurrent upload may have already changed that state (new evidence, new hypotheses). The LLM reasons about stale context.

- **Milestone advancement conflicts:** Two concurrent turns both try to advance the same milestone. The first succeeds; the second either double-advances (idempotent?) or fails.

**Recommendation:** Add a "Concurrency Model" section to the evidence flow architecture document. At minimum:
1. Define whether case-level operations are serialized (pessimistic locking, queue per case) or concurrent with conflict resolution.
2. Specify the turn number assignment strategy (database sequence? optimistic lock with retry?).
3. Address the content_hash race condition (use `INSERT ... ON CONFLICT DO NOTHING` or pre-insertion lock).
4. State whether milestone advancement is idempotent by design.

---

### 8. HIGH: No Design for Text/Query Input Preprocessing

**Documents:** data-preprocessing-design-specification.md, data-classification-strategy.md, evidence-classification-design.md

**The gap:** The preprocessing pipeline is designed exclusively for file uploads (`form=DOCUMENT`). The text input path (`form=USER_INPUT` via `/queries` endpoint) is mentioned in passing but has no design:

- **QueryClassifier is a placeholder:** The data-classification-strategy.md references a "QueryClassifier" for determining if text is machine data vs human question, and says "The QueryClassifier is not yet implemented as a standalone module." No interface, algorithm, or design exists.

- **MIXED submissions have no splitting strategy:** The `SubmissionClassification` enum defines `MIXED` (chat + data in one message), but no document specifies how to split a mixed submission into its chat and data portions. Is this regex-based? LLM-based? Does the LLM see the entire message and decide internally?

- **Pasted data gets no structural indexing:** A user who pastes 5000 lines of logs into the chat input doesn't get the Tier 1 structural index (error clusters, timeline, severity distribution). The data goes directly to the LLM as raw text, losing the compression and structure that file uploads receive.

- **No size limits for text input:** File uploads have a 10MB hard limit. Text input has no documented limit. A user could paste megabytes of data into the chat, bypassing the preprocessing pipeline entirely.

**Recommendation:** Add a "Text Input Preprocessing" section covering:
1. The `QueryClassifier` design (at minimum: above N lines and matches machine-data patterns → treat as implicit file upload, triggering Tier 0+1).
2. The MIXED content handling strategy (recommend: LLM-based, since the distinction between "chat about data" and "data with context" requires understanding intent).
3. Text size thresholds (e.g., >200 lines → route through preprocessing pipeline; >50KB → reject with "please use file upload" guidance).

---

### 9. HIGH: No Preprocessing Observability Design

**Documents:** data-preprocessing-design-specification.md, evidence-flow-architecture.md

**The gap:** The design specifies performance targets (Tier 0 <100ms, Tier 1 <2s) and defines `processing_time_ms` in the output model, but there are no metrics, dashboards, or alerts defined for the preprocessing pipeline. The evidence-flow-architecture.md defines evidence-level metrics (`evidence.created.total`, `evidence.rejection_rate`, etc.) but nothing for the preprocessing stage that feeds into it.

Missing:
- **Classification metrics:** Confidence score distribution, data type distribution, how often each fallback level is used
- **Extraction metrics:** Time per data type, fallback rate (how often extractors fall back to TEXT), compression ratios achieved
- **Sanitization metrics:** Redaction counts by type (emails, API keys, passwords), false positive rates
- **Vector DB metrics:** Storage success/failure rates, chunk counts, storage latency
- **Pipeline health:** SLA compliance (% of uploads processed within 2s), error rates, timeout rates

Without these, the system has no way to detect degradation. For example, if a regex change in Tier 0 causes 40% of log files to be misclassified as TEXT, the only signal would be degraded investigation quality — discovered reactively through user complaints, not proactively through monitoring.

**Recommendation:** Add a "Preprocessing Observability" section defining at minimum:
- `preprocessing.classification.confidence` — histogram by data_type, fallback_level
- `preprocessing.classification.distribution` — counter by data_type
- `preprocessing.extraction.duration_ms` — histogram by data_type, method
- `preprocessing.extraction.fallback_rate` — counter (extractor fell back to TEXT)
- `preprocessing.sanitization.redactions` — counter by redaction_type
- `preprocessing.vectordb.store_failures` — counter
- Alert: extraction fallback rate > 10% (classifier or extractor degradation)
- Alert: P95 extraction time > 1.5s (approaching 2s timeout)

---

### 10. MEDIUM: Image Processing Is a Dead End Without Tier 2

**Documents:** data-preprocessing-design-specification.md, evidence-classification-design.md

**The gap:** Tier 1 image processing extracts only metadata (format, dimensions, color mode, EXIF). The agent receives:
```
"Image: screenshot.png (PNG, 1920x1080, 245760 bytes).
Vision analysis available via Tier 2 deep analysis."
```

This is not actionable for investigation purposes. Screenshots of error messages, dashboard alerts, terminal output, and architecture diagrams are common troubleshooting evidence in the SRE/DevOps domain. The design treats images as opaque blobs until Tier 2 is available.

The platform-specific extractors document also identifies this: Grafana dashboards and Datadog monitors are often captured as screenshots. Without any text extraction from images, these become dead evidence — tracked in the system but unable to contribute to hypothesis evaluation or milestone advancement.

The design explicitly states "Vision analysis (screenshot OCR, diagram interpretation) is handled by Tier 2 via a multimodal LLM" — but Tier 2 is labeled as on-demand and optional (`TIER2_BACKEND=disabled` is the default).

**Recommendation:** Consider adding a lightweight OCR step to Tier 1 for image files:
1. Use `pytesseract` or a similar library for text extraction from screenshots
2. Budget: fits within 2s Tier 1 window for typical screenshots
3. Output: append extracted text to the image metadata as `ocr_text`
4. The agent then has at least raw text content from screenshots to work with
5. Tier 2 vision analysis remains available for interpretation of diagrams, charts, and visual patterns that OCR can't capture

This bridges the gap between "images are useless" and "full Tier 2 vision analysis" without changing the architectural model.

---

### 11. MEDIUM: No Structural Index Versioning Strategy

**Document:** data-preprocessing-design-specification.md

**The gap:** Structural indexes are stored in the vector DB and persist across sessions. When the classification algorithm or extraction logic changes (new error patterns, improved severity scoring, additional state transition detection), previously processed files retain stale indexes. The design has no mechanism for:

- **Re-indexing:** No way to trigger re-extraction when extractor logic improves
- **Schema versioning:** No tracking of which extractor version produced each index
- **Staleness detection:** No way to know that an index was produced by v1.0 when v2.0 is current
- **Migration:** No strategy for updating old indexes to match new extraction schemas

Over time, the vector DB will contain indexes produced by different extractor versions, with different section formats, different error classification thresholds, and different severity scores. Semantic search across these will produce inconsistent results.

**Recommendation:** Add an `extractor_version` field to:
1. `PreprocessingResult.extraction_metadata`
2. Vector DB chunk metadata
3. Evidence record

Define a re-indexing strategy:
- **Lazy:** When evidence is queried and its extractor_version < current_version, re-run Tier 1 and update the vector DB asynchronously. User gets current results; future queries get updated index.
- **Batch:** Background job scans for evidence with `extractor_version < current_version` and re-processes. Useful after a major extractor upgrade.

---

### 12. MEDIUM: No Multi-Tenancy Consideration in Preprocessing

**Documents:** data-preprocessing-design-specification.md, evidence-flow-architecture.md

**The gap:** The preprocessing pipeline and vector DB storage have no multi-tenancy design. All evidence operations are scoped by `case_id`, but there's no organization-level isolation:

- **Vector DB:** Queries filter by `case_id` but not `organization_id`. If case IDs were ever non-unique across organizations (unlikely but possible with bugs), evidence would leak. More practically, there's no organization-level search ("find all evidence about connection timeouts across all our cases").
- **Storage:** S3 paths are `s3://faultmaven/case_{id}/filename`. No organization prefix. This means:
  - No per-organization storage quotas
  - No per-organization data retention policies
  - No organization-level data deletion (GDPR right-to-erasure requires scanning all case paths)
- **Deduplication:** Per-case only. No organization-level dedup for enterprises where the same runbook or config is uploaded to multiple cases.

**Recommendation:** Add organization-scoping to:
1. Vector DB metadata (add `organization_id` to chunk metadata)
2. Storage paths (`s3://faultmaven/{org_id}/case_{id}/filename`)
3. Deduplication (optionally, a secondary `UNIQUE (organization_id, content_hash)` for cross-case dedup)

Even if these are deferred to enterprise features, the design should document the isolation model and identify where tenant boundaries exist.

---

### 13. MEDIUM: Platform-Specific Extractors Lack Integration Points

**Document:** platform-specific-extractors.md

**The gap:** The document describes platform-aware extraction for Datadog, Grafana, PagerDuty, etc. but doesn't specify how this integrates with the three-tier model:

- **DataType mapping:** Is Datadog dashboard data `METRICS`, `IMAGE`, or a new type? The document shows `"data_type": "metrics"` in the example payload, but a screenshot of a Datadog dashboard would be `IMAGE` while its structured extraction is `METRICS`. Which wins?
- **Classification bypass:** When the browser extension provides `"platform": "datadog"` and pre-structured data, does Tier 0 classification still run? The answer should be no (the platform already told us the type), but this isn't stated.
- **Extraction pipeline:** Does platform-specific structured data replace or supplement Tier 1 extraction? If the extension provides `structured_data.widgets[].metric`, does Tier 1 still run its statistical profile extractor on the raw HTML?
- **Fallback:** The document notes "Platform UIs change frequently" as a maintenance risk, but doesn't design a fallback for when a platform extractor fails. Does the system fall back to generic HTML processing?

**Recommendation:** Add a "Three-Tier Integration" section specifying:
1. Platform-detected uploads bypass Tier 0 (classification comes from platform hint)
2. Tier 1 runs a platform-specific extractor (not the generic DataType extractor)
3. If platform extraction fails, fall back to Tier 0 (reclassify as generic HTML → TEXT) + generic Tier 1
4. `extraction_metadata` stores `{"platform": "datadog", "dashboard_id": "abc-123"}` for platform-aware retrieval

---

### 14. LOW: Adaptive Classification System Is Under-Specified

**Document:** data-classification-strategy.md (Learning and Adaptation)

**The gap:** The `AdaptiveClassifier` and `PatternLearner` describe a system where:
1. User corrections are stored with semantic similarity search
2. LLM extracts new regex patterns from misclassified data
3. Learned patterns are added to the classification database at lower weight
4. Pattern effectiveness is A/B tested

This is the most complex subsystem described in any of the documents, yet it has the least specification:
- **Pattern safety:** LLM-generated regex patterns could be overly broad (matching too much) or computationally expensive (catastrophic backtracking). The `_is_too_broad()` check only flags exact matches against `.*`, `\w+`, `\d+`, `\s+` — not patterns like `.*foo.*` that are technically more specific but practically match everything.
- **Pattern rollback:** No mechanism to disable or roll back a learned pattern that causes regressions.
- **Feedback loop delay:** No specification for how quickly learned patterns take effect (immediate? after N corrections? after manual review?).
- **Conflict resolution:** If a learned pattern conflicts with a built-in pattern (different confidence levels for the same content), which wins?

**Recommendation:** Either:
(a) Fully specify the system with safety guardrails (pattern sandbox testing, manual approval gate, rollback mechanism, conflict resolution rules), or
(b) Simplify to "log misclassifications for manual review; periodically update built-in rules based on patterns observed in the logs." This achieves the learning goal without the complexity and risk of automated pattern injection.

---

### 15. LOW: No Design for Evidence Reclassification

**Documents:** evidence-classification-design.md, evidence-flow-architecture.md

**The gap:** The evidence lifecycle is designed as create-once: the LLM classifies evidence at creation time, and the category persists forever. The document mentions "Can be 'un-rejected' if investigation context changes" for REJECTED submissions, but there's no design for:

- **Reclassification workflow:** How does a user or the system change an evidence category? Is there an API endpoint? Does it re-run LLM analysis?
- **Milestone impact:** If CONTEXTUAL_EVIDENCE is reclassified to SYMPTOM_EVIDENCE, should its `advances_milestones` be recalculated? Does this retroactively change milestone attribution?
- **Audit trail:** Is the original classification preserved when reclassification occurs?
- **When the investigation reveals early evidence was misclassified:** In a common scenario, evidence uploaded during INQUIRY is classified as CONTEXTUAL (clean-looking config). Later in the investigation, the LLM realizes the config contains the root cause. There's no mechanism to reclassify it to CAUSAL_EVIDENCE.

**Recommendation:** Add a reclassification design covering:
1. API endpoint for manual reclassification
2. Automatic reclassification proposal (LLM can suggest reclassification in later turns, system presents to user)
3. Milestone recalculation on reclassification
4. Audit trail (`original_category`, `reclassified_at`, `reclassification_reason`)

---

## Summary

| # | Severity | Type | Title |
|---|----------|------|-------|
| 1 | HIGH | Design Flaw | Duplicate `DataType.TEXT` key in WEAK_INDICATORS silently drops indicators |
| 2 | HIGH | Design Flaw | Classification confidence thresholds are contradictory; LLM fallback unreachable |
| 3 | HIGH | Design Flaw | Deduplication has one-per-turn collision, mixed-content hash fragility, no cross-case strategy |
| 4 | MEDIUM | Design Flaw | Failure modes document presents contradictory recovery recommendations |
| 5 | MEDIUM | Design Flaw | Milestone inference incorrectly maps `solution_proposed` to CAUSAL_EVIDENCE |
| 6 | LOW | Design Flaw | Inconsistent HTTP status codes between flow architecture and failure modes docs |
| 7 | HIGH | Design Gap | No concurrency model for concurrent uploads, turn assignment, or dedup races |
| 8 | HIGH | Design Gap | No text/query input preprocessing design; QueryClassifier is a placeholder |
| 9 | HIGH | Design Gap | No preprocessing observability (metrics, alerts, dashboards) |
| 10 | MEDIUM | Design Gap | Image processing produces no actionable content without Tier 2 |
| 11 | MEDIUM | Design Gap | No structural index versioning or re-indexing strategy |
| 12 | MEDIUM | Design Gap | No multi-tenancy scoping in preprocessing or vector storage |
| 13 | MEDIUM | Design Gap | Platform-specific extractors lack integration points with three-tier model |
| 14 | LOW | Design Gap | Adaptive classification system under-specified; safety concerns |
| 15 | LOW | Design Gap | No evidence reclassification workflow |

### Recommended Priority for Resolution

**Before implementation begins:**
1. Fix WEAK_INDICATORS duplicate key (#1) — trivial
2. Redesign confidence threshold chain (#2) — affects classifier architecture
3. Resolve deduplication strategy tensions (#3) — affects schema design
4. Add concurrency model (#7) — affects evidence pipeline architecture
5. Design text/query preprocessing (#8) — affects pipeline scope

**Before production deployment:**
6. Add preprocessing observability (#9)
7. Add image OCR to Tier 1 (#10)
8. Add structural index versioning (#11)
9. Clarify failure mode recovery strategy (#4)
10. Fix milestone inference mapping (#5)

**Can be deferred:**
11. Multi-tenancy scoping (#12)
12. Platform extractor integration points (#13)
13. HTTP status code contract (#6)
14. Adaptive classification specification (#14)
15. Evidence reclassification workflow (#15)

---

## What the Design Gets Right

For completeness, the design makes several strong architectural decisions worth preserving:

1. **Three-tier cost model** — Tier 0+1 at $0 per file, Tier 2 on-demand only, is an excellent cost control mechanism. The 80-98% savings estimate is credible.
2. **Lazy staging for Tier 2** — Not uploading raw files to Tier 2 backends until the first query avoids unnecessary cloud API costs.
3. **Single-phase evidence creation** — No placeholder/promotion lifecycle. Evidence is created once with complete data. Simpler and less error-prone than two-phase alternatives.
4. **Content-based classification during INQUIRY** — Classifying evidence by what the data contains (not the investigation phase) is the right call. It prevents reclassification churn when the investigation status changes.
5. **Universal fallback chain in Tier 1** — Every extractor has a defined fallback path to TEXT extraction. The guarantee that `process_upload()` never fails due to extraction errors is a strong reliability property.
6. **Structural index in Vector DB, not in evidence table** — Keeping 50KB+ indexes out of the relational DB and in ChromaDB is architecturally sound.
7. **Hybrid milestone attribution (Option 2.5)** — System inference for 90% of cases + optional LLM override for edge cases is a pragmatic balance of automation and accuracy.

---

**Review Version:** 2.0
**Review Date:** 2026-02-13
