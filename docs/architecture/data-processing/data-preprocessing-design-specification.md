# Data Preprocessing Design Specification v5.3

**Status**: FINAL
**Date**: 2026-04-18
**Supersedes**: v5.2

---

## Change Summary

### v5.2 → v5.3 (Design/Code Drift Closure)

| Area | v5.2 | v5.3 |
|------|------|------|
| **Entry point** | Multiple preprocessing entry paths (legacy `process_upload`, `ChunkingService`, `PreprocessingService.preprocess`) cohabited with `classify_and_extract` | Single entry point in §2.4: `PreprocessingService.classify_and_extract()`. Legacy paths deleted (violated Tier 0+1 zero-LLM guarantee or were unreachable). |
| **Appendix B** | Stub / incomplete extractor reference | Rewritten: per-extractor `strategy_name` table, runtime markers (`page_capture_passthrough`, `structure_extraction`, `none`, `classification_failed`), uniform output budget (`MAX_STRUCTURAL_INDEX_TOKENS=2500`, `MAX_STRUCTURAL_INDEX_CHARS=10000`), shared utilities (`extract_timestamp`, `format_coverage_metadata`), 21 enumerated detect-secrets plugins as a security contract. |
| **Tier-0 command detection** | Spec implied broad command coverage; code matched only 7 commands with single-pattern detection | Spec and code aligned on the 13-command `COMMAND_OUTPUTS` dict with ≥2-pattern requirement (top/ps/vmstat/iostat/netstat/free/df/lsof → COMMAND_OUTPUT; dmesg/journalctl/strace/ltrace → LOGS_AND_ERRORS; perf → PROFILING_DATA; lscpu → STRUCTURED_CONFIG). Documented in [data-classification-strategy.md v3.0](./data-classification-strategy.md#command-output-classification). |
| **Companion: classification strategy** | Documented as 6-level cascade with `AdaptiveClassifier` / `PatternLearner` / `fallback_level` (none of which existed in code) | Rewritten as v3.0: 5-priority signal-source ordering (user_override / agent_hint / source_url / browser_context / rule_based), CSV/TSV structural gate, extension-sensitive LOGS thresholds, `_validate_hint` safety valve. |
| **Removed dead fields** | `sanitization_applied`, `redactions_count`, `security_flags`, `EXTRACTION_VERSION`, `CONFIDENCE_HIGH/MEDIUM/LOW_THRESHOLD` referenced in spec | Removed — PII redaction runs at the LLM boundary, not at extraction time; replaced with `CONFIDENCE_THRESHOLDS` + `FILE_UPLOAD_CONFIDENCE_BOOST` constants. |

### v5.1 → v5.2 (Proactive Vectorization)

| Area | v5.1 | v5.2 |
|------|------|------|
| **Vectorization trigger** | Reactive: auto-vectorize after 3+ DA calls, 3+ empty searches, timeout, or low confidence | Proactive: background vectorization starts immediately for DA-mode large files. Reactive triggers retained as fallback (timeout, empty searches, low confidence). `da_call_count >= 3` trigger removed. |
| **Integration point** | `agent_orchestration_service._execute_with_streaming()` (secondary `/sessions/execute` path only) | `milestone_engine._tool_augmented_generate()` (primary `/turns` path). Tracking lives where tools execute — same pattern as `deep_analysis_count` / `MAX_DEEP_ANALYSIS` enforcement already in the method. |
| **Cross-turn DA init** | `EvidenceDAState` initialized with `da_call_count=0` every turn; persistent count synced only after a DA call completes | `da_invocation_count` persisted on Evidence model via `repository.save(case)` after each `deep_analysis` call. |
| **Reactive triggers** | 4 triggers: timeout, 3+ empty searches, 3+ DA calls, low confidence | 3 triggers: timeout, 3+ empty searches, low confidence. DA call count trigger removed (replaced by proactive vectorization). |
| **Small-file fallback condition** | Redundant `not self._should_auto_vectorize(state)` check | Simplified to direct size check: `content_size_bytes < vectorization_min_size_bytes`. |

### v5.0 → v5.1 (Page Capture Pipeline)

| Area | v5.0 | v5.1 |
|------|------|------|
| **Page capture** | Raw HTML capture → UNSTRUCTURED_TEXT extractor | Semantic DOM extraction (frontend) → structured markdown → pass-through (backend). Error-first priority ordering, stat panel detection, ARIA alert promotion. |
| **ClassificationResult** | No source origin tracking | Added `source_type` field propagated from `source_metadata.source_type` through all 5 classification priority tiers. |
| **Preprocessing service** | All content routed through Tier 1 extractors | Page captures (`source_type="page_capture"`) bypass UnstructuredTextExtractor via `page_capture_passthrough` branch. |
| **Evidence filename** | Page captures saved as `.html` | Page captures saved as `.txt` with `content_type="text/plain"` (content is structured markdown, not HTML). |
| **System prompt** | No page capture format guidance | Added "WORKING WITH EVIDENCE DATA" section describing page capture markdown format (headings = panels, label:value = metrics, error-promoted sections, captured_at timestamp). |
| **Future work** | - | Stage 2 (query-time reranking) implemented in v5.2. Stage 3 (platform-specific extraction), Stage 4 (viewport sync) documented in Deferred Items. |

### v4.2 → v5.0 (Scenario-Driven Processing)

| Area | v4.2 | v5.0 |
|------|------|------|
| **Processing model** | Linear 4-tier escalation ("Never skip tiers. Always try the cheaper option first.") | Scenario-driven: **Triage**, **Directed Analysis**, **Semantic Search**. Mode selected by mechanical query classifier, not LLM judgment. |
| **Query routing** | LLM-driven tier selection via system prompt | Heuristic `classify_query()` — regex entity detection (timestamps, HTTP status codes, error keywords, service names, IPs) + generic phrase detection. No LLM call. |
| **System prompt** | Single "Data Access Strategy" with tier ordering | Mode-specific: `DATA_ACCESS_TRIAGE` (structural index is the answer) vs `DATA_ACCESS_DIRECTED_ANALYSIS` (structural index as orientation map, deep_analysis as primary tool) |
| **Structural index role** | Always presented the same way in LLM context | Tagged `<structural_index role="orientation">` in DA mode; plain `<structural_index>` in Triage mode |
| **Vectorization trigger** | Agent proposes → user approves | Auto-triggered mechanically on DA failure signals (no user confirmation) |
| **Failure tracking** | Global `consecutive_empty_searches` counter with escalation advisory after 2 empties | Per-evidence `EvidenceDAState` with 4 independent triggers: tool timeout, 3+ empty searches, 3+ DA invocations, low confidence (<0.2). Cross-turn persistence via `da_invocation_count` on Evidence model. |
| **Small-file DA failure** | Not addressed | Raw file content injected directly into LLM context when DA fails on files below vectorization threshold |
| **Evidence model** | No processing mode tracking | New fields: `processing_mode` (triage\|directed_analysis), `da_invocation_count` (cross-turn DA counter) |
| **Orchestration R4** | After 2 consecutive empty searches → `[ESCALATION ADVISORY]` | **Replaced** by per-evidence DA failure tracking with auto-vectorization (Section 6) |

### v4.1 → v4.2 (Tier-Escalation Hardening)

| Area | v4.1 | v4.2 |
|------|------|------|
| **Coverage metadata** | Extractors produce structural index only | All 10 extractors append `--- COVERAGE METADATA ---` with key-value pairs (Lines, Time range, Format, etc.) for downstream gap detection |
| **Keyword search** | Single-pass: ANY keyword matches | Two-pass: Pass 1 requires ALL keywords (high relevance). Pass 2 falls back to individual keywords with `partial_match: True` (capped at 5 results). |
| **Zero-result recovery** | No recovery path when search returns 0 results | Vocabulary extraction returns patterns (HTTP errors, exceptions, IPs, host:port, file paths) + frequent tokens + suggestion string |
| **Orchestration R3** | No coverage gap detection | Query entity extraction (timestamps, services, error codes, IPs) compared against evidence coverage metadata → advisory injected into LLM context |
| **Orchestration R4** | No escalation on repeated failures | After 2 consecutive empty `search_file` results → `[ESCALATION ADVISORY]` injected into LLM context with recovery options |
| **Orchestration R5** | No context budget tracking | 30K char budget for tool results. Standard compression (first 3 + signal lines + last 2) and aggressive compression (first + signal lines only) |

### v4.0 → v4.1 (Unified Ingestion Pipeline Implementation)

| Area | v4.0 | v4.1 |
|------|------|------|
| **Endpoint** | Endpoint-agnostic (`/queries` and `/data` both exist) | Unified `POST /cases/{id}/turns`. Old endpoints deleted. |
| **Evidence form** | `USER_TEXT` / `SUBMITTED_DATA` (classification-driven via `submission_classification`) | Payload-driven: attachments → `DOCUMENT`, agent tools → `SUBMITTED_DATA`. `_determine_evidence_form()` and `SubmissionClassification` deleted. |
| **Pipeline** | Preprocessing triggered by LLM classification for pasted text | Two-step: preprocess all attachments (Step 1, before LLM) → LLM inference (Step 2) |
| **Context** | Evidence summaries only in LLM context | Context Sliding Window: structural indexes included for recent evidence (Tier A/B/C) |
| **Config** | `TIER2_*` config names | Renamed to `DEEP_ANALYSIS_*`. New `EVIDENCE_CONTEXT_*` constants in `context_builder.py`. |
| **Pasted text** | Routed through Tier 0+1 (designed but endpoint-agnostic) | Submitted as `pasted_content` form field on `/turns`, preprocessed as attachment in Step 1 |

### v3.2 → v4.0

| Area | v3.2 | v4.0 |
|------|------|------|
| **Tier count** | 3 tiers (0: Classification, 1: Mechanical Extraction, 2: Deep Analysis) | 4 tiers (0+1: Structural Indexing, 2: Mechanical Search, 3: Deep LLM Analysis, 4: Vectorization) |
| **Vectorization** | Eager background async after every upload | On-demand, triggered by user query demand |
| **`search_file` tool** | Does not exist | New agent tool for Tier 2 mechanical search |
| **Domain extractors** | Single-use at upload time | Re-runnable with different parameters |
| **Evidence from search** | Not specified | Rules for when/how search results become evidence |
| **Agent escalation** | Tier 1 summary → Tier 2 deep analysis | Tier 1 → Tier 2 → Tier 3 → Tier 4 progressive ladder |
| **Classification model** | 6 unified types in spec, 12 detailed types hidden in code | Spec acknowledges 12→6 two-layer model; 11 distinct extractors |
| **Classification fallback** | Low confidence → `UNSTRUCTURED_TEXT` + user modal | Best-effort: highest-scoring candidate extractor, no user modal |

---

## 1. Architecture Overview

### 1.1 Scenario-Driven Processing Model

Data submitted to FaultMaven — whether uploaded files or pasted text — is always preprocessed through Tier 0+1 (structural indexing). Subsequent processing is determined by a **mechanical query classifier** that routes to one of three processing modes based on heuristic entity detection and phrasing analysis. No LLM is involved in mode selection.

```
                    SUBMISSION
                        |
                        v
         ┌──────────────────────────────┐
         │  TIER 0+1: Structural Index  │  Always. $0. <2s.
         │  Classification + Extraction │  Domain-specific extractors.
         │  → Summary + structural_index│  No LLM.
         └──────────────┬───────────────┘
                        │
                        v
         ┌──────────────────────────────┐
         │  QUERY CLASSIFIER            │  Mechanical. No LLM.
         │  classify_query(message,     │  Regex entity detection +
         │    has_attachments)           │  phrasing analysis.
         └──────────────┬───────────────┘
                        │
           ┌────────────┼────────────┬─────────────┐
           v            v            v             v
  ┌────────────┐ ┌───────────┐ ┌──────────────┐ ┌──────────────┐
  │  TRIAGE    │ │ DIRECTED  │ │  KNOWLEDGE   │ │  SEMANTIC    │
  │            │ │ ANALYSIS  │ │  QUERY       │ │  SEARCH      │
  │ Structural │ │           │ │              │ │              │
  │ index IS   │ │ Two       │ │ LLM answers  │ │ Reactive     │
  │ the answer.│ │ parallel  │ │ from built-in│ │ fallback     │
  │ Summarize  │ │ paths:    │ │ knowledge.   │ │ when DA fails│
  │ key        │ │           │ │ No tool loop.│ │ on large     │
  │ findings.  │ │ 1. BG     │ │ Evidence     │ │ files.       │
  │            │ │ vectorize │ │ grounding    │ │ vectorize →  │
  │            │ │ (large    │ │ relaxed.     │ │ kb_search.   │
  │            │ │  files)   │ │              │ │              │
  │            │ │           │ │              │ │              │
  │            │ │ 2. Tool   │ │              │ │              │
  │            │ │ loop with │ │              │ │              │
  │            │ │ 5 tools   │ │              │ │              │
  └────────────┘ └─────┬─────┘ └──────────────┘ └──────────────┘
                       │
              ┌────────┴────────┐
              │                 │
              v                 v
   ┌──────────────────┐  ┌──────────────────────────┐
   │  TOOL LOOP       │  │  PROACTIVE VECTORIZATION  │
   │  search_file,    │  │  Background task for      │
   │  deep_analysis,  │  │  large files (>50KB).      │
   │  web_search,     │  │  Runs concurrently with   │
   │  kb_qa,          │  │  tool loop. kb_search     │
   │  case_evidence   │  │  available when done.     │
   └────────┬─────────┘  └──────────────────────────┘
            │
   Reactive fallback?
   (timeout, empty searches,
    low confidence)
            │
       ┌────┴────┐
    NO │         │ YES + file qualifies
       │         v
       │  ┌──────────────────────────┐
       │  │  REACTIVE VECTORIZATION  │  Fallback trigger.
       │  │  No user confirmation.   │  Per-evidence tracking.
       │  │  → Semantic search via   │
       │  │    case_evidence_search │
       │  └──────────────────────────┘
       │         │
       v         v
   ┌──────────────────────────────┐
   │  AGENT RESPONSE              │
   │  → May create Evidence via   │
   │    evidence_to_add           │
   └──────────────────────────────┘
```

**Mode selection (heuristic, no LLM):**

| Signal | Result | Example |
|--------|--------|---------|
| Knowledge-seeking phrasing ("what is", "how does", "explain") WITHOUT case references or hard entities | **Knowledge Query** (confidence 0.85) | "What is Opik?", "How does Redis clustering work?" |
| Specific entities (timestamps, HTTP status codes, error keywords, service names, IPs) + interrogative structure | **Directed Analysis** (high confidence) | "what caused the 502 errors at 14:00?" |
| Entities + non-generic phrasing | **Directed Analysis** | "investigate 502s at 14:00" |
| Generic phrasing ("analyze this", "what's in here") without entities | **Triage** | "analyze this log file" |
| No message + attachments | **Triage** | User drops file with no question |
| Ambiguous (no entities, no generic phrasing, no question) | **Directed Analysis** (default) | "performance degradation" |

**Knowledge Query detection (3-gate system):**

Knowledge questions are detected by matching ALL three gates:
1. **Knowledge phrase match**: Message matches knowledge-seeking patterns ("what is X?", "how does X work?", "explain X", "best practices for X")
2. **No hard case-specific entities**: No timestamps, HTTP status codes, or IP addresses. Service names and error keywords are allowed (they can be the *subject* of a knowledge question, e.g., "How does Redis clustering work?")
3. **No case references**: No possessive/locational references to case data ("the error", "in the logs", "we're seeing"). All case reference patterns require a prefix (the/this/our/in/from) to avoid matching bare nouns like "What is a null pointer exception?"

Knowledge queries still get tool access (with `tool_choice="auto"`) so the LLM can invoke `kb_qa` when the question is about runbooks or documented procedures. The LLM decides whether to use a tool based on the question. Diagnostic reasoning validation is skipped, and a KNOWLEDGE QUERY OVERRIDE escape clause relaxes evidence-grounding requirements in the prompt.

### 1.2 Design Principles

1. **Mechanical mode selection.** The query classifier (`classify_query()`) determines the processing mode using regex entity detection and phrasing analysis. No LLM call for routing — deterministic and auditable.

2. **Payload-driven evidence form.** Evidence form (`USER_TEXT` vs `SUBMITTED_DATA` vs `DOCUMENT`) is determined by payload context: attachments present → `DOCUMENT`, agent tool findings → `SUBMITTED_DATA`, query-only → `USER_TEXT`. *(Updated v4.1: was classification-driven via `submission_classification`, now payload-driven via unified turn pipeline.)*

3. **Single unified endpoint.** All turns arrive via `POST /cases/{id}/turns` as `{query?, attachments?[]}`. Preprocessing runs before LLM inference (Step 1), not after. *(Updated v4.1: was endpoint-agnostic with `/queries` and `/data`.)*

4. **Re-runnable extractors.** Domain-specific extractors (Crime Scene, Anomaly Detection, etc.) can be re-invoked with different parameters on follow-up queries, not just at upload time.

5. **Proactive vectorization for DA-mode large files.** When a DA-mode query targets evidence above the vectorization size threshold, vectorization starts as a background task before the tool loop begins. Reactive fallback triggers (timeout, empty searches, low confidence) handle edge cases. No user confirmation required. *(Updated v5.2: was reactive-only via DA failure signals. v5.0: was "Agent decides, user approves".)*

### 1.3 Tool Cost Matrix

| Tool | Cost | Latency | LLM? | When Used |
|------|------|---------|------|-----------|
| *(structural index in context)* | $0.00 | <2s | No | Always — Tier 0+1 on every submission |
| `search_file` | $0.00 | ~0.5-2s | No | Agent needs specific data from raw file |
| `deep_analysis` | ~$0.01-0.05 | 3-15s | Yes | Agent needs interpreted analysis |
| `web_search` | $0.00 | 1-2s | No | Error messages, external docs (Google CSE or Tavily) |
| `kb_qa` | ~$0.01 | 0.5-2s | Yes (synthesis) | Unified KB: all scopes (global + personal + team) |
| `vectorize_file` | ~$0.05-0.50 | 10-60s | Embed only | Auto-triggered: proactively at the start of DA-mode turns for qualifying large files, reactively on DA failure signals. File must pass size gates. |
| `case_evidence_search` | $0.00 | ~0.5s | No | After vectorization, semantic search |

---

## 2. Tier 0+1: Structural Indexing (Updated)

Runs synchronously on every submission that contains attachments (file uploads and pasted text, both submitted via `POST /cases/{id}/turns`). Two sub-phases:

- **Tier 0 (Classification)**: Rule-based data type detection. <100ms. Zero LLM calls.
- **Tier 1 (Extraction)**: Domain-specific structural indexing. <2s with timeout enforcement and fallback chain. Zero LLM calls.

### 2.1 Two-Layer Classification: 12 DetailedDataTypes → 6 UnifiedDataTypes

The classification system has two layers. The **detailed layer** (12 types) drives extractor dispatch — each type has a distinct extractor producing meaningfully different output. The **unified layer** (6 types) is the external interface stored on `PreprocessingResult` and `Evidence`.

```
                        Content
                          │
                    ┌─────┴─────┐
                    │  Tier 0   │  DataClassifier.classify()
                    │  12 types │  Rule-based, <100ms
                    └─────┬─────┘
                          │ DetailedDataType
                    ┌─────┴─────┐
                    │  Tier 1   │  11 extractors dispatched by DetailedDataType
                    │  Extract  │  Zero LLM, <2s timeout
                    └─────┬─────┘
                          │ ExtractionResult
                    ┌─────┴─────┐
                    │  Unify    │  to_unified_data_type()
                    │  6 types  │  For PreprocessingResult / Evidence
                    └───────────┘
```

### 2.2 Detailed Classification → Extractor Mapping

The first table below lists the **12 DetailedDataTypes** that drive Tier 0→Tier 1 extractor dispatch. The second table lists **orchestrator runtime markers** that bypass the standard Tier 0→Tier 1 path — these are **not** DetailedDataTypes and they do not appear in the §2.1 diagram's Tier 0 output (they are set downstream by the preprocessing orchestrator).

**Table A — 12 DetailedDataTypes (classifier → extractor dispatch):**

| # | DetailedDataType | Extractor | Strategy | What It Produces | Unified |
|---|---|---|---|---|---|
| 1 | `LOGS_AND_ERRORS` | `LogsAndErrorsExtractor` | `crime_scene` | Severity-weighted error clusters, crime scene window (±200 lines around highest-severity error), burst detection (10+ errors in 50-line window), state transitions, timeline | **LOGS** |
| 2 | `ERROR_REPORT` | `ErrorReportExtractor` | `exception_context` | Stack frame parsing (Python/Java/JS/Go), exception type + message, user-code vs library-code filtering, prescriptive fix suggestions (e.g., NullPointer → None check) | **LOGS** |
| 3 | `TRACE_DATA` | `TraceDataExtractor` | `trace_correlation` | OTel + Jaeger span parsing, service call chain, critical path (top 3 slowest operations), slow spans (>20% of trace duration), error spans, duration normalization (ns/μs → ms) | **LOGS** |
| 4 | `COMMAND_OUTPUT` | `CommandOutputExtractor` | `command_parsing` | Format-specific parsing for `top`/`ps`/`netstat`/`df`/`free`/`iostat`/`vmstat`, CPU/memory/disk saturation thresholds (CPU >70%, mem >80%, disk >85%), offending process identification by PID | **LOGS** |
| 5 | `METRICS_AND_PERFORMANCE` | `MetricsAndPerformanceExtractor` | `statistical` | Auto-detect JSON/CSV/Prometheus format, per-metric stats (min/max/mean/std/p50/p95/p99), z-score anomaly detection (spikes >3σ, drops >50% below mean) | **METRICS** |
| 6 | `PROFILING_DATA` | `ProfilingDataExtractor` | `profiling_hotspot` | cProfile/flame graph/perf stat parsing, hotspot detection (>5% total time), recursive call flags, I/O function classification, optimization suggestions (memoization for recursion, async I/O for file/network) | **METRICS** |
| 7 | `STRUCTURED_CONFIG` | `StructuredConfigExtractor` | `direct` | YAML/JSON/TOML/INI/.env parsing, dual-layer secret redaction (suffix-anchored key patterns + value patterns + detect-secrets), non-secret value bypass, hierarchical text output | **CONFIGURATION** |
| 8 | `SOURCE_CODE` | `SourceCodeExtractor` | `ast_parse` | Python AST (imports, class hierarchy, function signatures with return types, async markers), multi-language regex fallback (JS/TS/Java/Go/Rust/C/C++), TODO/FIXME extraction | **CODE** |
| 9 | `UNSTRUCTURED_TEXT` | `UnstructuredTextExtractor` | `direct` | Embedded error/code extraction from mixed text, markdown + plain text dual path, paragraph-based sections, error-keyword lines with ±2-line context | **TEXT** |
| 10 | `DOCUMENTATION` | `DocumentationExtractor` | `documentation_structure` | Section classification (troubleshooting/procedure/configuration), operational command filtering (kubectl, docker, systemctl, etc.), TOC generation | **TEXT** |
| 11 | `VISUAL_EVIDENCE` | `VisualEvidenceExtractor` | `vision` | Metadata only: format, dimensions, byte size (placeholder for Phase 3 multimodal LLM vision analysis) | **IMAGE** |
| 12 | `UNANALYZABLE` | *(none — fallback)* | — | Truncation to 10,000 chars | **TEXT** |

**Table B — Orchestrator runtime markers (not DetailedDataTypes):**

| Marker                     | Triggered when                                                                                        | Extractor   | What It Produces                                                                                                                                                                                             | Unified  |
|----------------------------|-------------------------------------------------------------------------------------------------------|-------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------|
| `page_capture_passthrough` | `source_metadata.source_type == "page_capture"` — content is already structured markdown from copilot | *(skipped)* | Pre-structured markdown from frontend (htmlToStructuredText): headings, tables, lists, code blocks, forms, stat panels, ARIA alerts, error-first priority ordering, `[captured_at]` timestamp                | **TEXT** |

For the full runtime-marker vocabulary (including `structure_extraction`, `none`, `classification_failed`), see [Appendix B — Strategy names](#appendix-b-extractor-reference).

**Page Capture Pass-Through:** When `source_metadata.source_type` = `"page_capture"`, the preprocessing service bypasses the UnstructuredTextExtractor entirely. The content arrives as structured markdown from the browser extension's `htmlToStructuredText()` function (semantic DOM extraction with visibility checks, stat panel detection, ARIA alert promotion, and error-first static priority pass), so no additional extraction is needed. The pass-through branch sets `method="page_capture_passthrough"` and preserves the structured markdown output.

**Key insight**: The 4 extractors under LOGS produce fundamentally different output. A distributed trace parsed by `trace_correlation` (service call chain, critical path) is nothing like a log file parsed by `crime_scene` (error clusters, timeline). The 12→6 unification is a lossy compression for the external API; internally, the system leverages all 11 extractors.

**All 11 extractors use stdlib only** (re, ast, json). No external dependencies except optional `yaml`/`tomli` for config parsing. This means zero external risk, deterministic output, and fast execution.

#### Coverage Metadata (v4.2)

All 10 active extractors (excluding VisualEvidence) append **coverage metadata** after a `--- COVERAGE METADATA ---` separator. This metadata enables downstream systems — particularly the orchestration layer's coverage gap detection (Section 6.1) — to identify what an extractor *did* and *didn't* process.

**Format:**
```
[... structural index output ...]

--- COVERAGE METADATA ---
Lines: 847 of 12453
Time range: 2024-01-15T10:30:00 to 2024-01-15T10:45:23
Severity distribution: ERROR=23, WARN=45, CRITICAL=2
```

**Per-extractor metadata fields (as actually emitted by current code):**

| Extractor | Coverage Fields |
| --- | --- |
| `LogsAndErrorsExtractor` | `Lines` processed/total, severity distribution, time range |
| `ErrorReportExtractor` | `Language`, `Exception`, `Stack frames`, `Root cause` |
| `TraceDataExtractor` | `Spans`, `Services`, `Error spans`, `Total duration ms` |
| `CommandOutputExtractor` | `Command`, `Lines` |
| `MetricsAndPerformanceExtractor` | `Format`, `Metrics` (count), `Metric names`, `Anomalies found` |
| `ProfilingDataExtractor` | `Format`, `Functions profiled`, `Top function` (per-format — see note below) |
| `StructuredConfigExtractor` | `Format`, top-level keys, secrets redacted count |
| `SourceCodeExtractor` (Python path) | `Language`, `Lines`, `Functions`, `Classes`, `Error handlers` |
| `SourceCodeExtractor` (fallback path, non-Python) | `Language`, `Lines` |
| `UnstructuredTextExtractor` | `Lines`, `Structure`, `Error mentions`, `Code blocks` |
| `DocumentationExtractor` | `Format`, `Sections`, `Code blocks`, `Commands` |
| `VisualEvidenceExtractor` | `Format`, `Filename`, `Size bytes` (metadata-only placeholder until Phase 3 multimodal vision) |

> **`ProfilingDataExtractor` — what the per-format fields mean:**
>
> - **cProfile**: `Functions profiled` = count of parsed call sites; `Top function` = highest-cumtime location.
> - **Flame graph**: `Functions profiled` = count of unique leaf frames (the functions on CPU when sampled — the R3-useful signal for "is function X present?"); `Top function` = leaf of the highest-sampled stack.
> - **perf report**: `Functions profiled` = count of parsed symbols; `Top function` = highest-overhead symbol.
> - **perf stat** and **unknown-format fallback**: neither field is emitted, because these formats carry no function-level data. `format_coverage_metadata` drops `None`-valued keys, so the absence is genuine (not a lie by omission).

Coverage metadata is additive — appended after the separator, never modifying existing output. Utility functions in `faultmaven/modules/preprocessing/extractors/utils.py` provide `COVERAGE_SEPARATOR`, `format_coverage_metadata()`, `extract_timestamp()`, and `extract_time_range()`.

### 2.3 Classification Fallback: Best-Effort Dispatch

When the classifier has low confidence (< 0.60), it still has partial pattern scores from its rule-based analysis. Instead of always falling back to `UNSTRUCTURED_TEXT`, v4.0 uses the highest-scoring candidate type.

**v3.2 behavior**: Low confidence → `UNSTRUCTURED_TEXT` with `classification_failed=True` → TEXT extractor (headings + key sentences — minimal value).

**v4.0+ behavior**: Low confidence → highest-scoring candidate type → that type's extractor gets a chance → if the extractor produces useful output, we keep it; if not, the extractor's own fallback degrades gracefully. Every `classification_failed=True` path also populates `suggested_types` to drive the cooperative-clarification UX described in §2.5.

```python
# In DataClassifier._classify_with_rules(), before the final fallback:

scores = {
    DataType.LOGS_AND_ERRORS: text_score + structured_score,
    DataType.METRICS_AND_PERFORMANCE: metrics_score,
    DataType.STRUCTURED_CONFIG: config_score,
    DataType.SOURCE_CODE: code_score,
}
best_type, best_score = max(scores.items(), key=lambda x: x[1])

if best_score >= 1:  # At least one pattern matched something
    scored_suggestions = _top_suggested_types(scores, n=3)
    return ClassificationResult(
        data_type=best_type,
        confidence=0.50,
        source="rule_based_best_effort",
        classification_failed=True,  # Still flagged as uncertain
        suggested_types=scored_suggestions,  # Drives cooperative clarification
    )

# True fallback: nothing matched at all → UNSTRUCTURED_TEXT
return ClassificationResult(
    data_type=DataType.UNSTRUCTURED_TEXT,
    confidence=0.30,
    source="rule_based",
    classification_failed=True,
    suggested_types=[DataType.UNSTRUCTURED_TEXT, DataType.DOCUMENTATION],
)
```

**Why this works**: Each extractor already handles content that doesn't match its expectations:
- `LogsAndErrorsExtractor`: No errors found → extracts last 500 lines (tail). No worse than TEXT.
- `MetricsAndPerformanceExtractor`: Non-numeric data → falls back to TEXT preview.
- `StructuredConfigExtractor`: Parse failure → TEXT extraction with regex secret redaction.
- `SourceCodeExtractor`: No language detected → TEXT extraction.

The extractor fallback chain (Section 4.9 of v3.2) ensures no extractor propagates errors. Best-effort dispatch gives the specialized extractor a *chance* to find something valuable, with the same safety net as before.

### 2.4 Pasted Text and Page Capture Processing

> **Updated v5.1**: Page captures arrive as pre-structured markdown from the copilot extension via
> the `pasted_content` form field with `source_metadata.source_type = "page_capture"`. They bypass
> the UnstructuredTextExtractor via a pass-through branch in the preprocessing service.

> **Updated v4.1**: Pasted text is now submitted as an attachment via the unified
> `POST /cases/{id}/turns` endpoint (using the `pasted_content` form field). It is
> preprocessed in Step 1 of `process_turn()` alongside file uploads — **before** the
> LLM runs. The old `submission_classification`-driven routing was removed.

#### Flow: All Attachments Through Tier 0+1 (Unified Pipeline)

```
User submits turn via POST /cases/{id}/turns
  │  {query?, files[]?, pasted_content?}
  ▼
investigation_service.process_turn(payload: TurnPayload)
  │
  ├─ STEP 1: PRE-LLM PREPROCESSING
  │   for attachment in payload.attachments:
  │       _preprocess_attachment(case, attachment)
  │           → preprocessing_service.classify_and_extract(content, filename)
  │                 → DataClassifier.classify(...)
  │                 → extractor.extract(content)   [with 2s timeout]
  │                 → PreprocessingResult
  │           → Evidence(form=DOCUMENT, preprocessed_content=structural_index)
  │
  │   PII redaction is NOT applied at extraction time.
  │   It runs at the LLM boundary (MilestoneEngine), so structural indexes
  │   are stored raw and the redaction map can be kept consistent per-case.
  │
  ├─ STEP 2: LLM INFERENCE
  │   Context includes structural indexes via Context Sliding Window
  │   (Tier A: recent data with full structural_index, searchable="true")
  │   Tools access raw files via ToolContext.in_memory_case
  │   → LLM responds with evidence_to_add (agent findings → SUBMITTED_DATA)
  │
  └─ Result: TurnResponse
```

#### Pasted Text vs File Upload: Same Pipeline, Same Extractors

| Aspect | File Upload | Pasted Text |
|--------|-------------|-------------|
| **Entry point** | `files[]` form field | `pasted_content` form field |
| **Filename** | Real filename (e.g., `app.log`) | Synthetic: `pasted-content-{ts}.txt` |
| **Extension hints** | Available (`.log`, `.yaml`, `.csv`) | Not available — classifier relies on content patterns |
| **Raw file storage** | Stored via `content_ref` | Stored via `content_ref` |
| **Content hash** | SHA-256 of UTF-8 text | SHA-256 of UTF-8 text |
| **Extractors used** | Same 11 | Same 11 |
| **Form** | `DOCUMENT` | `DOCUMENT` |
| **Preprocessing** | Step 1 (before LLM) | Step 1 (before LLM) |

**Single entry point**: `PreprocessingService.classify_and_extract(content, filename, source_metadata)`. The service short-circuits extraction for three special paths:

- `source_type == "page_capture"` → `page_capture_passthrough` (copilot already produced structured markdown).
- `data_type == UNANALYZABLE` → placeholder with `extraction_method="none"`.
- `classification_failed=True` (confidence < 0.50) → placeholder with `extraction_method="classification_failed"` plus `suggested_types` in `extraction_metadata` for the cooperative-clarification UX (see §2.5 below).

Otherwise the type-specific extractor runs under a 2-second timeout (see §4.9). Output is a `PreprocessingResult` — see §7.1 for the full field list.

**Deduplication is live.** Per-case content-hash deduplication short-circuits duplicate uploads: before creating a new `Evidence` row, `_preprocess_attachment` calls `ICaseRepository.find_by_content_hash(case_id, content_hash)`. A match returns the existing `Evidence` and skips storage (no re-write of the raw file). The per-attachment turn response carries `duplicate_of` and `duplicate_turn` so the frontend can render a non-blocking toast. Scope is per-case only — the same content can be uploaded to different cases without interference. Implementation: `ICaseRepository.find_by_content_hash` contract with implementations on `SessionlessCaseRepository` (production), `SQLiteCaseRepository`, `PostgreSQLHybridCaseRepository`, and `InMemoryCaseRepository`.

### 2.5 Cooperative Clarification on Low-Confidence Classification

When the heuristic classifier returns `classification_failed=True`, every failure path populates `ClassificationResult.suggested_types` with up to 3 candidate types from its scoring pass. `PreprocessingService` forwards these via `extraction_metadata["suggested_types"]`.

The agent still attempts the investigation in the same turn — classification uncertainty is not a hard block. After the turn runs, `InvestigationService` injects **COOPERATIVE clarification suggestions** ahead of the engine's follow-ups in `TurnResponse.suggested_actions`:

- Up to 3 pre-composed `query_submit` messages like *"Treat the previously uploaded file (`foo.txt`) as application logs and analyze it."* — one per suggested type.
- Plus a **"Something else"** fallback that submits *"Treat the previously uploaded file as unstructured text and try to analyze it."*

The user clicks a card; the next turn runs normally with the pre-composed message. The agent reads the raw file via its existing tools (`search_file`, `deep_analysis`) using the user-provided type hint. There is no re-classification at the classifier layer, no evidence mutation, no new LLM integration — just the existing COOPERATIVE suggestion plumbing driving a deterministic post-turn injector in `InvestigationService`.

Supersedes the rejected **Classifier LLM rescue** proposal (cheaper, user-authoritative, no telemetry prerequisite).

---

## 3. Tier 2: Mechanical Search (NEW)

### 3.1 Purpose

Tier 2 provides fast, zero-LLM search over raw file content. When the Tier 1 structural index doesn't contain enough detail to answer a question, the agent can search the raw file directly using keyword/regex patterns or re-run domain extractors with different parameters.

### 3.2 Agent Tool: `search_file`

```python
@tool(name="search_file")
async def search_file(
    evidence_id: str,
    query: str,
    search_type: str = "keyword",       # "keyword" | "regex" | "extractor"
    extractor_params: Optional[dict] = None,
) -> str:
    """
    Search a previously uploaded file for specific information.

    Use this tool when:
    - The evidence summary mentions something relevant but lacks detail
    - You need specific lines, values, or patterns from the raw file
    - You want to re-run an extractor with different parameters
      (e.g., different time window, severity filter)

    Do NOT use this tool when:
    - The evidence summary already contains the answer
    - The question is about investigation strategy, not file content
    - You need LLM interpretation (use deep_analysis instead)

    search_type options:
    - "keyword": Split query into keywords, find matching lines with context
    - "regex": Treat query as a regex pattern
    - "extractor": Re-run domain extractor with extractor_params
    """
```

#### Dual-Path Evidence Resolution

The `search_file` tool resolves evidence content through two paths:

- **Path 1 (standalone)**: Query `evidence_artifacts` table by evidence ID → read raw content from `content_ref`. Used when the evidence exists as a standalone artifact.
- **Path 2 (case-embedded)**: Load case via `case_repo.get()` → find matching `Evidence` object → read content from `content_ref`. Used when evidence is embedded in the case object.

The `Evidence.original_filename` field (set during `_preprocess_attachment()`) provides the display filename in search results instead of the opaque evidence ID. This gives users meaningful context about which file a search result came from (e.g., `app.log` instead of `ev_abc123`).

#### DA Tool Loop Integration

In Directed Analysis turns, `search_file` is available inside the bounded DA Tool Loop (`_tool_augmented_generate()` in `milestone_engine.py`) alongside the other investigation tools (`deep_analysis`, `kb_qa`, `web_search`, `case_evidence_search`) and the terminating `schema_tool`. The LLM iterates up to 4 times with an iteration-0 guardrail that forces at least one investigation-tool call before the structured response is generated. See [Orchestration Capabilities §5.4](../investigation-engine/orchestration-capabilities.md#54-tool-augmented-generation-v50--v60) for full details.

### 3.3 Search Modes

#### A. Keyword Search (Two-Pass Strategy — v4.2)

The two-pass strategy described here is implemented in the **`search_file` agent tool** (`faultmaven/modules/agent/tools/search_file_tool.py`), not in `BasicTier2Service._keyword_search` — which is the in-process Tier 3 backend and implements a simpler single-pass partial-match scorer (see §3.5 and §4). Callers that go through `search_file` get the two-pass behaviour; `BasicTier2Service` invoked directly does not.

Tokenizes the query into keywords (>2 chars), then uses a two-pass strategy for high-precision results with partial-match recovery:

**Pass 1 — Full match (high relevance):** Find lines matching ALL keywords. Returns results with `relevance: 1.0`. Merge overlapping windows, cap at `max_results`.

**Pass 2 — Partial match fallback:** If Pass 1 returns nothing and multiple keywords exist, try individual keywords. Results are marked `partial_match: True` with `relevance: 1/len(keywords)`. Capped at 5 results (lower than Pass 1).

```python
def _keyword_search(self, content: str, query: str) -> list[dict[str, Any]]:
    """Two-pass keyword search with partial match fallback."""
    keywords = [kw.lower() for kw in query.split() if len(kw) > 2]
    lines = content.split("\n")

    # Pass 1: lines matching ALL keywords
    for i, line in enumerate(lines):
        matched = [kw for kw in keywords if kw in line.lower()]
        if len(matched) == len(keywords):  # ALL must match
            # ... context window, relevance=1.0

    if merged:
        return merged  # High-quality results found

    # Pass 2: individual keyword fallback (only when >1 keyword)
    if len(keywords) > 1:
        for kw in keywords:
            # ... context window, partial_match=True, relevance=1/len(keywords)
        return partial_matches[:5]  # Lower cap
```

#### B. Regex Search

Treats the query as a regex pattern. Useful for structured searches like timestamps, error codes, IP addresses.

```python
async def _regex_search(
    raw_content: str,
    pattern: str,
    context_lines: int = 10,
    max_results: int = 10,
) -> List[DataExcerpt]:
    """
    Regex search: compile pattern, find all matches, return with context.
    """
    import re
    regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    lines = raw_content.split('\n')
    matches = []

    for i, line in enumerate(lines):
        if regex.search(line):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            matches.append(DataExcerpt(
                content='\n'.join(f"{j+1}: {lines[j]}" for j in range(start, end)),
                line_start=start + 1,
                line_end=end,
            ))

    return merge_overlapping(matches)[:max_results]
```

#### C. Extractor Re-run

Re-runs the domain-specific extractor selected by the evidence's `DetailedDataType`. The extractor runs with its default configuration — **extractors do not currently accept per-call parameter overrides**. The `extract(content: str) -> str` contract is uniform across all 11 extractors.

When a different slice of the file is needed (narrower time window, lower severity threshold, different z-score), use **keyword** or **regex** search to reach the same data rather than re-parametrizing the extractor.

```python
async def _rerun_extractor(
    raw_content: str,
    detailed_data_type: DetailedDataType,
) -> ExtractionResult:
    """
    Re-run a domain-specific extractor on the raw content.

    Uses the evidence's DetailedDataType (12 types) to select the exact
    extractor that originally processed the file. Returns the same
    structural index shape as Tier 1.
    """
    extractor = preprocessing_service.extractors.get(detailed_data_type)
    if not extractor:
        return extract_text_structure(raw_content)

    return extractor.extract(raw_content)
```

**Why no parameterization?** The agent can already reach any slice of the file through keyword/regex search. Parameterizing 11 extractors would fragment their API for marginal gain over the existing search paths.

### 3.4 Zero-Result Recovery: Vocabulary Extraction (v4.2)

Implemented in the **`search_file` agent tool**, not in `BasicTier2Service`. When any search mode exposed by `search_file` returns 0 results, the tool extracts vocabulary from the file to help the agent reformulate its query. The response includes a `vocabulary` object and a `suggestion` string. `BasicTier2Service` directly returns a plain `"No matching sections found for the query."` message without vocabulary — callers wanting vocabulary recovery must go through `search_file`.

**Three-pass heuristic (on first 100KB of content):**

1. **Known patterns** — Compiled regex for HTTP errors (`[45]\d{2}`), exception names (`ConnectionTimeout`, `NullPointerException`), host:port pairs, IP addresses, file paths. Up to 30 patterns.

2. **Frequent tokens** — Statistical: split on whitespace/delimiters, filter stop words and log noise, return tokens appearing 2-10 times (not too rare, not too common). Top 20.

3. **Suggestion string** — Concatenates top 10 terms from patterns + frequent tokens into a human-readable hint: `"No matches found. File contains these terms: 503, ConnectionTimeout, 10.0.0.5, api-server:8080, kafka-consumer:9092"`.

**Zero-result response format:**
```json
{
  "evidence_id": "ev_abc",
  "filename": "app.log",
  "search_type": "keyword",
  "query": "database_migration",
  "results_count": 0,
  "results": [],
  "vocabulary": {
    "patterns": ["503", "ConnectionTimeout", "10.0.0.5", "api-server:8080"],
    "frequent_tokens": ["request", "batch", "processing", "handler"]
  },
  "suggestion": "No matches found. File contains these terms: 503, ConnectionTimeout, 10.0.0.5, ..."
}
```

**Performance:** Vocabulary extraction completes in <500ms on ~1MB content (compiled regex + Counter-based frequency analysis).

### 3.5 Relationship to Existing Services

The `search_file` agent tool is a standalone implementation that coexists with other components; it does **not** replace or wrap `BasicTier2Service`:

| Existing Component | Role | Relationship to `search_file` |
| --- | --- | --- |
| `BasicTier2Service._keyword_search()` | Single-pass partial-match scorer, returned to the agent via the Tier 3 `deep_analysis` tool when backend is `basic` | Independent; neither two-pass nor vocabulary-recovery. `search_file` reimplements the richer two-pass + vocab-recovery flow directly on raw file content. |
| `ReadFileTool` (`read_file`) | Read file content by evidence ID | Remains for reading; `search_file` adds search |
| Domain extractors (Tier 1) | Single-use at upload | Made re-runnable via `search_file` extractor mode |
| `DeepAnalysisTool` (`deep_analysis`) | LLM-powered analysis (backed by `ITier2AnalysisService` — basic / local / external / disabled) | Tier 3 interpreted search; orthogonal to `search_file` |

### 3.6 When to Use `search_file` vs `deep_analysis`

| Scenario | Tool | Why |
|----------|------|-----|
| "Find all lines containing 'timeout'" | `search_file` (keyword) | Pure text match, no interpretation needed |
| "Show me errors between 14:00-14:15" | `search_file` (regex: `14:0[0-9].*ERROR\|14:1[0-5].*ERROR`) | Pattern matching on timestamps |
| "What anomalies are there at z>2?" | `search_file` (extractor, `z_score_threshold=2.0`) | Re-run extractor with different params |
| "What's causing the connection timeouts?" | `deep_analysis` | Needs LLM interpretation |
| "Summarize the error patterns after the deployment" | `deep_analysis` | Needs LLM synthesis |

---

## 4. Tier 3: Deep LLM Analysis (Renamed from Tier 2)

> **Functionally unchanged from v3.2 Section 6.** Renumbered from Tier 2 → Tier 3.

The agent tool is `deep_analysis` (was already defined in v3.2). It calls `ITier2AnalysisService.analyze()` with one of the pluggable backends (external, local LLM, or basic search).

**Key distinction from v4.0 Tier 2**: Tier 3 uses an LLM to *interpret* the data and generate an answer. Tier 2 returns raw excerpts for the agent to interpret itself.

**When agent uses Tier 3:**
1. Tier 1 structural index is insufficient
2. Tier 2 keyword/regex search returns matches but the agent needs interpretation
3. Hypothesis validation needs raw data analysis
4. User explicitly asks for deeper analysis
5. Image requires vision analysis (multimodal LLM)

**Pluggable backends** (unchanged):
- `ExternalTier2Client`: HTTP call to cloud microservice (Gemini, OpenAI, custom)
- `LocalTier2Service`: In-process with local LLM (Ollama/vLLM)
- `BasicTier2Service`: In-process keyword search, no LLM (fallback)

**Configuration** (updated Phase 5):
```bash
DEEP_ANALYSIS_BACKEND=disabled    # external | local | basic | disabled
DEEP_ANALYSIS_URL=                # URL for external backend
DEEP_ANALYSIS_API_KEY=            # API key for external backend
DEEP_ANALYSIS_TIMEOUT_SECONDS=30
```

> **Note**: The old `TIER2_*` config names are no longer supported. Phase 5 performed a clean break — use `DEEP_ANALYSIS_*` exclusively. In the codebase, the service is still called `ITier2AnalysisService`. The "Tier 3" naming is a spec-level concept for the processing model; in v5.0, tool selection is mode-driven rather than tier-driven.

---

## 5. Vectorization (Redesigned v5.0)

### 5.1 Key Changes: Eager → On-Demand → Auto-Triggered → Proactive

**v3.2**: After every file upload, the Tier 1 structural index is chunked, embedded, and stored in ChromaDB as a background async task. Every file gets vectorized.

**v4.0**: Vectorization only when the agent proposes and user approves.

**v5.0**: Vectorization is auto-triggered mechanically when directed analysis fails on qualifying files. No user confirmation required.

**v5.2**: For DA-mode queries, vectorization starts **proactively as a background task** for qualifying large files at the beginning of the tool loop — before any DA failures occur. The agent tool loop runs concurrently, and `case_evidence_search` becomes available as soon as vectorization completes. Reactive fallback triggers (timeout, empty searches, low confidence) are retained for edge cases. The `da_call_count >= 3` reactive trigger is removed — it conflated thorough investigation with failure.

### 5.2 Qualification: Size Gates

Two size gates must pass before vectorization (either auto-triggered or manual):

#### Size Minimum (Configurable)

Files below the size threshold are fully representable by their structural index. Vectorizing them adds no retrieval value.

```python
# Configurable via settings (AgentSettings.vectorization_min_size_bytes)
# Default: 50,000 bytes (50KB). Range: 1,000 - 10,000,000.
min_size = get_settings().agent.vectorization_min_size_bytes
```

**Rationale**: A 5KB config file's Tier 1 output is the full parsed config. A 50KB log file's Tier 1 output is a structural index that may omit details. When DA fails on a file below this threshold, the system injects raw file content directly into the LLM context instead (see Section 6.1).

#### Size Maximum (Hard Cap)

Files above the max size cap are too expensive to vectorize (embedding cost scales linearly with content size).

```python
VECTORIZATION_MAX_SIZE_BYTES = 50_000_000  # 50MB hard cap
```

For files above the cap, the agent should use `search_file` (targeted search) and `deep_analysis` (windowed LLM analysis) instead.

### 5.3 Proactive + Reactive Vectorization (v5.2)

Vectorization uses a two-layer strategy: **proactive** for DA-mode queries with large files, **reactive** as a fallback when the proactive path wasn't taken or failed.

#### Proactive Vectorization (Primary — v5.2)

When the query classifier routes to DIRECTED_ANALYSIS and evidence exceeds the vectorization size threshold, vectorization starts as a **background task** before the DA tool loop begins. The tool loop runs concurrently — the agent can use `search_file` and `deep_analysis` immediately. When vectorization completes, `case_evidence_search` becomes available.

```python
# In milestone_engine._tool_augmented_generate() — called only for DA turns
async def _start_proactive_vectorization(
    self, case: Case, tool_context: ToolContext,
) -> dict[str, asyncio.Task]:
    """Start background vectorization for qualifying DA-mode evidence."""
    settings = get_settings()
    tasks = {}
    for ev in case.evidence:
        size = getattr(ev, "content_size_bytes", 0) or 0
        if (
            size >= settings.agent.vectorization_min_size_bytes
            and size <= VECTORIZATION_MAX_SIZE_BYTES
            and not getattr(ev, "vectorized", False)
        ):
            tasks[ev.evidence_id] = asyncio.create_task(
                self._vectorize_evidence(ev.evidence_id, tool_context)
            )
    return tasks
```

**Why proactive works:** Vectorization cost (~$0.05 for embedding) is comparable to a single DA call ($0.01-0.05). Starting it in parallel with the tool loop means semantic search is typically available by the time the agent finishes its first DA call — no wasted wait time. If DA succeeds immediately, the vectorization cost is negligible.

**When proactive vectorization is NOT started:**
- Triage mode (structural index is the answer — no DA tools used)
- Knowledge query mode (no evidence search needed)
- Files below size threshold (structural index is sufficient)
- Files above 50MB hard cap (too expensive)
- Evidence already vectorized (idempotency check)

#### Reactive Vectorization (Fallback — v5.0, updated v5.2)

Three independent fallback trigger signals remain for cases where proactive vectorization wasn't started or failed. Tracked per-evidence via simple counters in `_tool_augmented_generate()` (same pattern as `deep_analysis_count`):

| Signal | Threshold | Rationale |
|--------|-----------|-----------|
| **Tool timeout** | Any `deep_analysis` or `search_file` call times out | File is too large for point queries |
| **Repeated empty searches** | 3+ consecutive `search_file` calls return 0 results | Agent's keyword strategy is failing; semantic search may find what keywords miss |
| **Low confidence** | Any `deep_analysis` returns confidence < 0.2 | Analysis produced unreliable results |

> **Removed in v5.2:** The `da_call_count >= 3` trigger. Calling DA 3 times on a file is legitimate thorough investigation (e.g., error patterns, timeline, root cause), not failure. This trigger conflated investigation depth with tool inadequacy.

**Cross-turn persistence**: `da_invocation_count` is stored on the Evidence model and persisted via `repository.save(case)` after each `deep_analysis` call. The persisted counter is **not** itself a vectorization trigger in v5.2 — it exists so the secondary `/sessions/execute` path (`agent_orchestration_service._get_persisted_da_call_count()`) can reconstruct per-evidence DA history across turns for its `EvidenceDAState` tracking.

**Reactive vectorization** calls `_reactive_vectorize()` which checks the size gate, calls `_vectorize_evidence()`, and injects the `[SYSTEM]` message on success. Each trigger fires independently — whichever fires first vectorizes the file.

When auto-vectorization fires (proactive or reactive), a `[SYSTEM]` message is injected into the tool result context:

```text
[SYSTEM] This file has been automatically indexed for semantic search.
Use case_evidence_search to find content by meaning rather than keywords.
```

**Early advisory**: When `empty_search_count` reaches 3 (before the auto-vectorization size gate is checked), a `[SYSTEM]` advisory is injected into the tool result to redirect the agent toward `deep_analysis`:

```text
[SYSTEM] Last {N} search_file calls on this file returned zero results.
Consider using deep_analysis with a different query approach.
```

This advisory fires regardless of file size and is independent of the auto-vectorization trigger.

### 5.4 Small-File DA Failure Fallback (v5.0)

When DA fails on a file **below** the vectorization size threshold, vectorization is not an option. Instead, the system injects the raw file content (up to 50KB safety cap) directly into the LLM context:

```text
[SYSTEM] Full file content (small file, {size} bytes):
{raw_content}
```

This ensures the LLM has full visibility into small files when point queries fail, without the overhead of vectorization.

### 5.5 Tool Interface

```python
@tool(name="vectorize_file")
async def vectorize_file(
    evidence_id: str,
) -> str:
    """
    Vectorize a previously uploaded evidence file for semantic search.

    Chunks the file content, generates embeddings, and stores them in
    ChromaDB. After vectorization, use case_evidence_search to find
    content semantically. Triggered automatically when directed analysis
    fails on large files.

    Prerequisites (system-enforced):
    - File must exceed configured minimum size (default 50KB)
    - File must be <50MB (VECTORIZATION_MAX_SIZE_BYTES)
    """
```

### 5.6 What Gets Vectorized

The Tier 1 **structural index** — not the raw file content. This is unchanged from v3.2 Section 5.

- 10MB log file → Structural index (~50KB) → ~100 chunks
- 5MB metrics CSV → Statistical profile (~30KB) → ~60 chunks
- 5KB config → Full parsed config → skipped (below size minimum)

#### 5.6.1 Chunking Algorithm (`chunk_structural_index`)

Implemented in `faultmaven/core/preprocessing/vector_storage.py`. Section-aware splitter:

1. **Split on section headers.** The input is split on the regex `===\s+.+?\s+===` which preserves section headers as separator tokens. Text before the first header is attributed to a default section named `HEADER`.
2. **Whole sections first.** If a section fits under `max_chunk_tokens`, it becomes a single chunk with `metadata["section"]` set to the section name.
3. **Oversize sections split on paragraph boundaries.** For sections larger than `max_chunk_tokens`, the text is split on `\n\n` and accumulated into a running buffer. When adding the next paragraph would overflow the budget, the buffer is emitted as a chunk and the next buffer is seeded with the **last `overlap_tokens` worth** of the previous chunk (`_get_last_n_tokens`) to preserve cross-chunk context.
4. **Never split mid-line.**

Token counts are estimated via `_estimate_tokens(text) = len(text) // 4` — a heuristic that avoids a per-chunk tokenizer call.

#### 5.6.2 Per-Chunk Metadata Schema

Each chunk stored in ChromaDB carries:

| Key | Source | Purpose |
| --- | --- | --- |
| `evidence_id` | Caller-provided | Links the chunk back to its Evidence row |
| `case_id` | Caller-provided | Scoping for per-case retrieval |
| `data_type` | `UnifiedDataType.value` | Filtering by logs/metrics/text/etc. |
| `section` | Section-header text (or `HEADER`) | Retrieval context |
| `chunk_index` | 0-based position | Deterministic ordering within evidence |
| `total_chunks` | Total emitted for this evidence | Completeness signals |
| `upload_timestamp` | `datetime.now(timezone.utc).isoformat()` at store-time | Temporal filtering / TTL |

Any additional scalar keys passed in `metadata=…` by the caller are forwarded as-is.

#### 5.6.3 Embedding Fallback

Embeddings are produced by the in-process BGE-M3 model via `model_cache.get_bge_m3_model()`. If the model is unavailable at call time (model load failure, missing weights), `store_in_vector_db_background` **logs a warning and falls back to ChromaDB's default embedding function** rather than failing the store — so vectorization always succeeds, at reduced retrieval quality.

#### 5.6.4 Silent-Failure Semantics

`store_in_vector_db_background` catches all exceptions from chunking, embedding, and Chroma upsert, logs them, and returns silently. Rationale: vectorization is a background task run after the user has already received their turn response, and the raw Evidence object is always retrievable from the Case repository regardless of vector-index state. See also [Evidence Failure Modes](./evidence-failure-modes.md) for adjacent failure handling.

### 5.7 Configuration

```bash
# Tier 4: Vectorization (on-demand)
VECTORIZATION_MIN_SIZE_BYTES=50000       # Skip files smaller than this
VECTORIZATION_MAX_SIZE_BYTES=50000000    # Skip files larger than this
CHROMADB_HOST=localhost
CHROMADB_PORT=8000
CHROMADB_COLLECTION_PREFIX=case_

# Chunking (read at startup via faultmaven/config/settings.py)
VECTOR_CHUNK_SIZE_TOKENS=500
VECTOR_CHUNK_OVERLAP_TOKENS=50
```

> **One-shot deployment knobs.** `VECTOR_CHUNK_SIZE_TOKENS` and `VECTOR_CHUNK_OVERLAP_TOKENS` are read at startup via `get_settings()` and threaded into `store_in_vector_db_background`. Changing either of them **after** the vector DB has been populated requires deleting the ChromaDB collection and re-ingesting all evidence + KB content from source — mixing chunk sizes in the same collection silently degrades retrieval quality (embeddings computed at different chunk sizes are not directly comparable). FaultMaven retains raw evidence + runbooks on disk, so re-indexing is a mechanical rebuild, never data recovery.

### 5.8 Migration from v3.2

In v3.2, `store_in_vector_db_background()` is called automatically after every upload. In v4.0, this background call is removed from the upload path. Instead, `vectorize_file` is called explicitly by the agent.

**Code change**: Remove the `store_in_vector_db_background()` call from the upload flow. The function itself remains available for the `vectorize_file` tool.

---

## 6. Scenario-Driven Processing Modes (v5.0)

The investigation agent receives a **mode-specific system prompt** based on the query classifier's output. This replaces the old linear escalation decision tree and "Never skip tiers" instruction.

### 6.0 Query Classifier

The `classify_query()` function in `modules/agent/domain/services/query_classifier.py` performs mechanical routing:

```python
class ProcessingMode(str, Enum):
    TRIAGE = "triage"
    DIRECTED_ANALYSIS = "directed_analysis"
    KNOWLEDGE_QUERY = "knowledge_query"
    SEMANTIC_SEARCH = "semantic_search"

@dataclass
class QueryClassification:
    mode: ProcessingMode
    detected_entities: dict[str, list[str]]  # timestamps, status_codes, etc.
    confidence: float  # 0.0-1.0

def classify_query(user_message: str, has_attachments: bool) -> QueryClassification:
    """Heuristic classification — no LLM call."""
```

**Entity detection** (compiled regex): timestamps (`\d{1,2}:\d{2}`, ISO dates, month-day), HTTP status codes (`[45]\d{2}`), error keywords (OOM, segfault, timeout, connection refused, etc.), service names (nginx, redis, postgres, etc.), IP addresses.

**Classification logic:**

1. No message + attachments → **TRIAGE** (confidence 0.95)
2. Knowledge-seeking phrasing WITHOUT hard entities or case references → **KNOWLEDGE_QUERY** (confidence 0.85)
3. Specific entities + interrogative structure ("what", "why", "how") → **DIRECTED_ANALYSIS** (confidence 0.9)
4. Entities + non-generic phrasing → **DIRECTED_ANALYSIS** (confidence 0.75)
5. Generic phrasing without entities → **TRIAGE** (confidence 0.85)
6. Generic phrasing WITH entities → entities win → **DIRECTED_ANALYSIS** (confidence 0.65)
7. Interrogative without entities → **DIRECTED_ANALYSIS** (confidence 0.6)
8. Ambiguous → **DIRECTED_ANALYSIS** (confidence 0.5, DA subsumes Triage)

**Knowledge query detection** uses a 3-gate system (see Section 1.1 for details). The check runs *before* entity-based routing to prevent knowledge questions from falling through to DIRECTED_ANALYSIS.

### Mode-Specific System Prompts

Two `DATA_ACCESS_*` constants are injected into the INVESTIGATOR system prompt via the `{data_access_strategy}` placeholder:

**Triage mode** (`DATA_ACCESS_TRIAGE`):

```text
## Data Access Strategy

The structural indexes in <evidence_collected> are your primary source.
Summarize the key findings: errors by severity, anomalies, misconfigurations,
notable patterns.
If a structural_index is [TRUNCATED], use search_file to retrieve specific sections.
Do NOT call deep_analysis or vectorize_file in triage mode — the structural
index is the answer.
```

**Directed Analysis mode** (`DATA_ACCESS_DIRECTED_ANALYSIS`):

```text
## Data Access Strategy

The user has a specific question. The <structural_index role="orientation"> for
each evidence file is a map of the file's contents (time range, services, error
distribution).

Use this map to formulate targeted queries:
- **deep_analysis** — Primary tool. Ask a focused question about specific evidence.
- **search_file** — Supplementary. Use for exact keyword, regex, or timestamp matching.

You do NOT need to try search_file before deep_analysis. Use whichever is
appropriate for the question. If your analysis is insufficient, the system will
automatically index large files for semantic search — you do not need to manage this.
```

**Knowledge Query mode**: Knowledge queries get tool access with `tool_choice="auto"` — the LLM can invoke `kb_qa` for runbook content or answer from built-in knowledge. The investigation prompt is appended with a `KNOWLEDGE QUERY OVERRIDE` escape clause that relaxes evidence-grounding and diagnostic reasoning requirements.

**System Instruction (Type A/B/C routing)**: The system instruction includes question routing guidance:

- **TYPE A — Case question**: Questions about THIS case's evidence (IPs, errors, timestamps). Agent MUST search evidence before responding.
- **TYPE B — Knowledge question**: General technical questions not answerable from case evidence. Agent answers from knowledge, optionally using `web_search` or `kb_qa`.
- **TYPE C — Hybrid**: Questions bridging case data and external knowledge. Agent searches evidence first, then applies knowledge/KB context.

**Evidence vs Knowledge rule**: Evidence is user-submitted case data only — `evidence_to_add` entries must come from user-submitted files, pasted text, or user statements. Knowledge from `kb_qa`, `web_search`, or LLM training data informs analysis but is never recorded as evidence.

Default is Type A (evidence search is always safe).

**Structural index tagging**: In DA mode, structural indexes are tagged `<structural_index role="orientation">` to signal they are orientation data, not the primary output. In Triage mode, plain `<structural_index>` is used.

### 6.1 Orchestration Hardening: Mechanical Safety Nets (v4.2, updated v5.2)

Three mechanical safety nets in `AgentOrchestrationService`: coverage gap detection (R3), vectorization with proactive + reactive paths (R4), and context budgeting (R5).

#### R3: Coverage Gap Detection

Before each LLM call, the orchestration service extracts entities from the user's message and compares them against evidence coverage metadata:

1. **Query entity extraction** — Compiled regex extracts timestamps (`14:00`, `2024-01-15`), service names (words after `in`/`from`/`on`), HTTP error codes (`4xx`/`5xx`), error codes (`E1234`), and IP addresses.

2. **Coverage gap detection** — For each evidence artifact, parse the `--- COVERAGE METADATA ---` section. Compare query timestamps against evidence time ranges, query services against evidence source fields. Gaps produce advisory strings.

3. **Advisory injection** — Gap descriptions are appended to the LLM system prompt as `[COVERAGE ADVISORY]` blocks. Example: `"User asks about 14:00 but evidence ev_abc only covers 13:42-13:57. Agent should acknowledge the gap or search for additional data."`

#### R4: Proactive Vectorization + Per-Evidence Reactive Fallback (v5.2)

> **v5.2 change**: Proactive background vectorization for DA-mode large files. `da_call_count >= 3` reactive trigger removed. Cross-turn DA count initialized from persisted value at state creation.
> **v5.0 change**: Replaced v4.2 global `consecutive_empty_searches` counter with per-evidence `EvidenceDAState`.

**Proactive path (v5.2):** At the start of `_tool_augmented_generate()` in `milestone_engine.py`, `_start_proactive_vectorization()` starts `asyncio.create_task()` for each qualifying evidence file (above size threshold, not already vectorized). These tasks run concurrently with the DA tool loop. Since `_tool_augmented_generate()` is only called for DA-mode turns, no mode check is needed.

**Reactive fallback:** The tool loop also tracks DA failure signals per-evidence using simple counters (same pattern as `deep_analysis_count`):

```python
# Per-evidence tracking in _tool_augmented_generate()
da_empty_search_counts: dict[str, int] = {}   # evidence_id → consecutive empties
da_vectorized: set[str] = set()               # evidence IDs already vectorized
```

**Tracking rules** (in `_track_da_result()`, called after each tool execution):

- `search_file` returns 0 results → `da_empty_search_counts[evidence_id] += 1`
- `search_file` returns results → `da_empty_search_counts[evidence_id] = 0` (reset)
- `deep_analysis` completes → `da_invocation_count` persisted on Evidence model
- Any tool times out → triggers reactive vectorization immediately

**Reactive vectorization**: When any reactive trigger fires AND the file passes the size gate AND proactive vectorization hasn't already completed, `_reactive_vectorize()` is called. It checks size gates, calls `_vectorize_evidence()`, and injects the `[SYSTEM]` message on success.

**Cross-turn persistence**: `da_invocation_count` on the Evidence model is incremented and persisted via `repository.save(case)` after each `deep_analysis` call in `_track_da_result()`:

```python
# In _track_da_result() — after deep_analysis completes:
for ev in case.evidence:
    if ev.evidence_id == evidence_id:
        ev.da_invocation_count = getattr(ev, "da_invocation_count", 0) + 1
        break
await self.repository.save(case)
```

#### R5: Context Budget Tracking and Tool Result Compression

Tool results can flood the LLM context window with log noise. The orchestration service tracks cumulative tool result size (in characters) against a 30K char budget (`TOOL_RESULT_BUDGET`):

| Budget Usage | Compression Level | Behavior |
| --- | --- | --- |
| < 80% (< 24K chars) | None | Full tool result passed through |
| 80-100% (24K-30K) | Standard | First 3 lines + high-signal keyword lines + last 2 lines |
| > 100% (> 30K) | Aggressive | First line + high-signal keyword lines only |

**High-signal keywords** (15 terms): `error`, `exception`, `fail`, `timeout`, `refused`, `denied`, `critical`, `fatal`, `panic`, `crash`, `kill`, `oom`, `traceback`, `stacktrace`, `caused by`.

Compression modifies only what the LLM sees — the full uncompressed content is preserved in the `AgentToolCall` database record for audit purposes.

---

## 7. Evidence Creation from Search Results

When the agent uses Tier 2 or Tier 3 tools, the results may warrant creating new Evidence records. Evidence is created through the same `evidence_to_add` mechanism used for all evidence creation.

### 7.1 When to Create Evidence

The agent creates evidence from search/analysis results when:

| Condition | Create? | Example |
|-----------|---------|---------|
| Finding is **new** (not already captured in existing evidence) | Yes | "Found a connection pool exhaustion pattern not mentioned in the structural index" |
| Finding is **specific** (concrete data, not vague) | Yes | "OOM errors correlate with cache size >2GB starting at 14:23" |
| Finding **advances the investigation** (supports/refutes hypothesis) | Yes | "Config shows pool_size=5, which explains the connection exhaustion under load" |
| Finding **repeats** existing evidence | No | Same error pattern already in the structural index summary |
| Finding is **speculative** without supporting data | No | "This might be a memory leak" with no excerpts |
| Finding is **intermediate** (stepping stone, not a conclusion) | No | "Found 15 matching lines" without interpretation |

**System prompt guidance for agent:**
```
After using search_file or deep_analysis, create evidence ONLY when
you've found something new, specific, and investigation-advancing.

DO NOT create evidence for:
- Search results that merely confirm what the summary already says
- Intermediate search steps (the search itself is not evidence)
- Speculative observations without supporting data excerpts
```

### 7.2 Evidence Field Mapping

#### From Tier 2 (`search_file`) Results

When the agent creates evidence from mechanical search results:

| Evidence Field | Value | Source |
|----------------|-------|--------|
| `summary` | Agent-written description of the finding | LLM generates from search excerpts |
| `category` | LLM-classified (SYMPTOM/CAUSAL/etc.) | LLM determines from finding nature |
| `source_type` | Same as original evidence's `source_type` | Inherited from searched file |
| `form` | `EvidenceForm.SUBMITTED_DATA` | Data was re-analyzed from submitted file |
| `source_file_id` | Original evidence's `source_file_id` | Links back to the source file |
| `preprocessed_content` | Key excerpts from search results | Agent selects relevant excerpts |
| `content_size_bytes` | Size of excerpts | Computed from excerpts |
| `preprocessing_method` | `"search_file_keyword"` or `"search_file_regex"` or `"search_file_extractor"` | Search mode used |
| `primary_purpose` | LLM-generated description | Why this finding matters |

#### From Tier 3 (`deep_analysis`) Results

| Evidence Field | Value | Source |
|----------------|-------|--------|
| `summary` | Agent-written description of the analysis finding | LLM generates |
| `category` | LLM-classified | LLM determines |
| `source_type` | Same as original evidence's `source_type` | Inherited |
| `form` | `EvidenceForm.SUBMITTED_DATA` | Data was analyzed from submitted file |
| `source_file_id` | Original evidence's `source_file_id` | Links back |
| `preprocessed_content` | `DeepAnalysisResult.answer` + formatted excerpts | From Tier 3 output |
| `content_size_bytes` | Size of answer + excerpts | Computed |
| `preprocessing_method` | `"deep_analysis_{backend}"` (e.g., `"deep_analysis_local_llm"`) | Backend used |
| `primary_purpose` | LLM-generated | Why this finding matters |

### 7.3 Provenance Chain

Evidence created from search/analysis results maintains a provenance chain back to the original submission:

```
Original upload → Evidence A (form=DOCUMENT, source_file_id=file_123)
                      |
                Agent uses search_file on Evidence A
                      |
                      v
                  Evidence B (form=SUBMITTED_DATA,
                              source_file_id=file_123,
                              primary_purpose="Tier 2 search finding: ...")
```

This allows the UI to show: "This finding was derived from [original_file.log], discovered during search."

---

## 8. Evidence Form Classification (Updated)

### 8.1 EvidenceForm Values

```python
class EvidenceForm(str, Enum):
    DOCUMENT = "document"
    """Data submitted as attachment via /turns endpoint — file uploads AND pasted data."""

    USER_TEXT = "user_text"
    """Query-only turn with no attachments (questions, descriptions, observations)."""

    SUBMITTED_DATA = "submitted_data"
    """Evidence derived from agent tool use (search_file, deep_analysis results).
    Not used for direct user submissions — those are DOCUMENT."""
```

### 8.2 Classification Logic

> **v4.1 Update:** The `_determine_evidence_form()` function and `submission_classification` field have been **removed**. Evidence form is now determined by payload context (which code path creates the evidence), not by LLM classification. See Section 2.4 for the unified pipeline flow.

Evidence form is assigned deterministically based on how evidence enters the system:

- **Attachments** (file uploads, pasted data processed through `_preprocess_attachment()`) → `DOCUMENT`
- **Agent findings** (LLM `evidence_to_add` applied in milestone engine) → `SUBMITTED_DATA`
- **User text** (conversational messages without data) → not created as evidence; stays in `case.messages[]`

### 8.3 Form Assignment by Context

| Context | EvidenceForm | How Determined |
|---------|-------------|----------------|
| File upload attachment | `DOCUMENT` | Set in `_preprocess_attachment()` during Step 1 |
| Pasted data attachment | `DOCUMENT` | Set in `_preprocess_attachment()` during Step 1 |
| LLM-identified evidence (`evidence_to_add`) | `SUBMITTED_DATA` | Hardcoded in milestone engine evidence creation |
| Evidence from Tier 2/3 search results | `SUBMITTED_DATA` | Derived from submitted file data |
| User types a question (no data) | N/A | No evidence created; message only |

---

## 9. Integration with Existing Architecture

### 9.1 Files to Modify for v4.0

| File | Change |
|------|--------|
| `modules/agent/tools/` | Add `search_file_tool.py`, add `vectorize_file_tool.py` |
| `core/preprocessing/tier2/basic.py` | Expose `_keyword_search` for Tier 2 tool |
| `core/preprocessing/vector_storage.py` | Remove auto-call from upload path; keep as tool target |
| `core/investigation/milestone_engine.py` | v4.1: Removed `_determine_evidence_form()` and `submission_classification` reads; evidence form is payload-driven |
| `modules/preprocessing/classifier.py` | Add best-effort fallback (highest-scoring candidate instead of always UNSTRUCTURED_TEXT) |
| `modules/preprocessing/preprocessing_service.py` | Expose `classify_and_extract(content, filename)` for pasted text path |
| `modules/agent/tools/deep_analysis_tool.py` | No change (becomes Tier 3 tool) |
| `container/providers/tools.py` | Register `search_file` and `vectorize_file` tools |
| System prompt | Add escalation strategy guidance |

### 9.2 Backward Compatibility

- Existing `deep_analysis` tool continues to work unchanged (renamed conceptually to Tier 3)
- `TIER2_*` config keys renamed to `DEEP_ANALYSIS_*` (clean break, no backward compat)
- Existing vectorized data in ChromaDB remains searchable
- No database migration needed (Tier 4 uses same ChromaDB schema)

### 9.3 Agent Tool Summary

See §1.3 [Tool Cost Matrix](#13-tool-cost-matrix) for the canonical cost/latency/usage table. This section previously duplicated that table.

---

## 10. Deferred Items

These items are out of scope for the initial v4.0 implementation but are documented for future consideration:

| Item | Description | When |
|------|-------------|------|
| **Frontend paste detection** | `onPaste` handler in `UnifiedInputBar.tsx` to send structured `{typed, pasted}` segments | After backend stabilizes |
| ~~**Unified endpoint processing**~~ | ~~Merge `/queries` and `/data` into single endpoint~~ | **Done in v4.1** — `POST /cases/{id}/turns` with two-step pipeline. Old endpoints deleted. |
| **Cross-file correlation** | Tier 3 analysis across multiple files simultaneously | Requires multi-file context windowing |
| **Vectorization cost tracking** | Track per-case vectorization costs for billing | Enterprise feature |
| **Deep analysis file size cap** | `DEEP_ANALYSIS_MAX_FILE_SIZE_MB` config to reject oversized files before sending to Tier 3 backend | When large file uploads are common |
| ~~**Content-hash deduplication**~~ | **Done.** Per-case dedup via `ICaseRepository.find_by_content_hash()` short-circuits duplicate uploads. Turn response carries `duplicate_of` + `duplicate_turn`. See §2.4. | Done |
| ~~**Classifier cooperative clarification**~~ | **Done.** On `classification_failed=True`, `_build_classification_clarification_suggestions` in `InvestigationService` injects COOPERATIVE choices built from the classifier's `suggested_types` + a "Something else" fallback. No LLM call. See §2.5. | Done |
| **Evidence failure modes** | Orphan-file cleanup (M1) and monitoring scaffolding (M2) **done**. Scenario 2 (async LLM timeout recovery at turn processing) **deferred** — current error-path UX already provides specific error codes + `Retry-After` headers; revisit only if production telemetry shows user harm. See [evidence-failure-modes.md](./evidence-failure-modes.md). | Partial done / revisit on telemetry signal |
| **DIFF_PATCH extractor** | Parse unified diffs / git patches — files changed, lines added/removed | When deployment-change investigations are common |
| **THREAD_DUMP extractor** | JVM thread dump parsing — deadlock detection, lock contention | When Java-heavy user base emerges |
| ~~**Page Capture Stage 2: Query-Time Reranking**~~ | **Implemented in v5.2.** `_rerank_page_capture_sections()` in `context_builder.py` splits page capture structural indexes on `\n##` headings, scores each section against user query via normalised keyword overlap (stopwords excluded), reorders so query-relevant content appears first. Preamble (`[captured_at: …]` + page title) pinned at position 0. Runs before per-item char cap so relevant sections survive truncation. Triggered only for `extraction_method="page_capture_passthrough"` evidence. | ~~Post-v5.1~~ Done |
| **Page Capture Stage 3: Platform-Specific Extraction** | Tool-specific DOM heuristics for Grafana, Datadog, PagerDuty, etc. CSS-in-JS makes CSS-selector-based extraction fragile — prefer DOM structure + ARIA attribute heuristics. Generic `htmlToStructuredText` already handles most dashboards via tryKeyValue/tryStatValue; platform extractors would add precision, not coverage. Related to `platform-specific-extractors.md`. | Post-v5.1 |
| **Page Capture Stage 4: Viewport Sync / Real-Time Capture** | Current capture is one-shot snapshot — stale for live dashboards (Grafana auto-refresh). Options: periodic re-capture, MutationObserver for DOM changes, or explicit "refresh capture" button. Trade-off: bandwidth vs freshness. | Post-v5.1 |

---

## Appendix A: Configuration Reference

```bash
# ============================================================
# FILE INGESTION & VALIDATION (Tier 0)
# ============================================================
MAX_UPLOAD_SIZE_MB=10
ALLOWED_MIME_TYPES=text/plain,text/csv,application/json,application/yaml,image/png,image/jpeg
BLOCKED_EXTENSIONS=.exe,.dll,.zip,.bin
CLASSIFICATION_SAMPLE_SIZE=5000

# ============================================================
# TIER 0+1: STRUCTURAL INDEXING
# ============================================================

# Log Structural Index
LOG_CONTEXT_LINES=200
LOG_TAIL_EXTRACTION_LINES=500
LOG_CLUSTER_GAP=50
LOG_BURST_THRESHOLD=10
LOGS_SEVERITY_FATAL=100
LOGS_SEVERITY_CRITICAL=90
LOGS_SEVERITY_ERROR=50
LOGS_SEVERITY_WARN=10

# Metrics Statistical Profile
METRICS_ANOMALY_Z_SCORE_THRESHOLD=3.0

# ============================================================
# TIER 2: MECHANICAL SEARCH
# ============================================================
SEARCH_FILE_MAX_RESULTS=10
SEARCH_FILE_CONTEXT_LINES=20

# ============================================================
# TIER 3: DEEP LLM ANALYSIS
# ============================================================
DEEP_ANALYSIS_BACKEND=disabled      # external | local | basic | disabled
DEEP_ANALYSIS_URL=                  # URL for external backend
DEEP_ANALYSIS_API_KEY=              # API key for external backend
DEEP_ANALYSIS_TIMEOUT_SECONDS=30
# NOTE: Old TIER2_* names are no longer supported (clean break in Phase 5)

# ============================================================
# VECTORIZATION (proactive primary, reactive fallback)
# ============================================================
# VECTORIZATION_MIN_SIZE_BYTES is now configurable via AgentSettings
# (settings.agent.vectorization_min_size_bytes). Default: 50000 (50KB).
# Range: 1000-10000000. Set via env var VECTORIZATION_MIN_SIZE_BYTES.
VECTORIZATION_MAX_SIZE_BYTES=50000000    # 50MB hard cap (not configurable)
CHROMADB_HOST=localhost
CHROMADB_PORT=8000
CHROMADB_COLLECTION_PREFIX=case_
VECTOR_CHUNK_SIZE_TOKENS=500
VECTOR_CHUNK_OVERLAP_TOKENS=50

# ============================================================
# RAW FILE STORAGE
# ============================================================
STORAGE_BACKEND=local               # local | s3
S3_BUCKET_EVIDENCE=faultmaven-evidence
S3_REGION=us-east-1

# ============================================================
# SANITIZATION
# ============================================================
SANITIZE_PII=true
AUTO_SANITIZE_BASED_ON_PROVIDER=true
PII_REDACT_EMAILS=true
PII_REDACT_PHONE_NUMBERS=true
PII_REDACT_IP_ADDRESSES=false
PII_REDACT_API_KEYS=true
PII_REDACT_PASSWORDS=true
```

---

## Appendix B: Extractor Reference

All 11 extractors share the uniform `extract(content: str) -> str` contract. They are stateless, produce a structural index with appended coverage metadata, and run under a 2-second Tier 1 timeout. None accept per-call parameter overrides; when the agent needs a different slice of the data it uses keyword or regex search via `search_file`.

### Strategy names

Each extractor exposes `strategy_name` which flows into `ExtractionResult.method` and `PreprocessingResult.extraction_method` (both are `ExtractionMethod` Literals):

| Extractor | `strategy_name` | DetailedDataType |
| --- | --- | --- |
| `LogsAndErrorsExtractor` | `crime_scene` | `LOGS_AND_ERRORS` |
| `ErrorReportExtractor` | `exception_context` | `ERROR_REPORT` |
| `TraceDataExtractor` | `trace_correlation` | `TRACE_DATA` |
| `CommandOutputExtractor` | `command_parsing` | `COMMAND_OUTPUT` |
| `MetricsAndPerformanceExtractor` | `statistical` | `METRICS_AND_PERFORMANCE` |
| `ProfilingDataExtractor` | `profiling_hotspot` | `PROFILING_DATA` |
| `StructuredConfigExtractor` | `direct` | `STRUCTURED_CONFIG` |
| `SourceCodeExtractor` | `ast_parse` | `SOURCE_CODE` |
| `UnstructuredTextExtractor` | `direct` | `UNSTRUCTURED_TEXT` |
| `DocumentationExtractor` | `documentation_structure` | `DOCUMENTATION` |
| `VisualEvidenceExtractor` | `vision` | `VISUAL_EVIDENCE` |

Runtime markers (set by `PreprocessingService`, not by any extractor):

| Marker | Triggered when |
| --- | --- |
| `page_capture_passthrough` | `source_type == "page_capture"` — content is already structured markdown from the copilot; extractor is skipped |
| `structure_extraction` | Tier 1 timed out or raised — falls back to a truncated preview with `timeout_fallback=True` or `error_fallback=True` in metadata |
| `none` | `detailed_data_type == UNANALYZABLE` — placeholder returned, no extractor runs |
| `classification_failed` | `confidence < 0.50` — placeholder returned with `suggested_types` in metadata; frontend shows modal |

### Output budget (uniform across extractors)

- `MAX_STRUCTURAL_INDEX_TOKENS = 2500`
- `MAX_STRUCTURAL_INDEX_CHARS = 10000`
- **Two truncation strategies, applied at different stages:**
  - `truncate_output()` (extractors, `extractors/utils.py`) — preserves the first 40 % + last 40 % of the produced structural index so both file headers and tails remain visible. Applied when an extractor's output exceeds the cap.
  - `_fallback_direct_extraction()` (`PreprocessingService`, `preprocessing_service.py`) — **head-only** cap at `max_chars=10000` with a trailing `... [Truncated N chars]` marker. Used by the orchestrator when no extractor runs (`page_capture_passthrough`, `UNANALYZABLE`, `classification_failed`, Tier 1 timeout/error `structure_extraction` fallback) — there's no extractor output to preserve a tail from, just raw content.

### Shared utilities (`extractors/utils.py`)

- `extract_timestamp(line)` / `extract_time_range(content)` — recognise ISO-8601 (with/without `T`), syslog BSD, epoch seconds, epoch milliseconds. Scan only the first 10 and last 10 lines to stay within the Tier 1 latency budget.
- `format_coverage_metadata(**kwargs)` — appends `--- COVERAGE METADATA ---` with key-value pairs (Lines, Time range, Format, etc.) so downstream tooling can reason about what the extractor saw.
- `has_content()` + `EMPTY_CONTENT_RESPONSE` — uniform empty-input guard.

### Extractor-specific notes

- **LogsAndErrorsExtractor** — entity profile (services, hostnames, frequent identifiers) is **prepended** to the structural index so it survives context-builder truncation.
- **TraceDataExtractor** — embedded-JSON recovery: when the content is not pure JSON, scans for `{[\s\S]*}` to salvage trace structures from mixed-format payloads.
- **ConfigExtractor (StructuredConfigExtractor)** — secret redaction is always-on (not gated by `sanitize_pii`) since structural indexes are persisted and may be sent to LLMs. Two layers:
  1. **Key-based** — suffix-anchored patterns match the terminal key segment only (e.g., `_password$`, `_token$`, `_secret$`). Keys where the secret word is a prefix (e.g., `token_type`, `auth_method`) are NOT redacted. A non-secret value bypass skips redaction for obvious enum/boolean values.
  2. **Value-based** — `detect-secrets` with 21 plugins enabled: Artifactory, AWS, Azure, BasicAuth, Cloudant, Discord, GitHub, GitLab, IBM Cloud, IBM COS, JWT, Mailchimp, Npm, OpenAI, SendGrid, Slack, Softlayer, SquareOAuth, Stripe, Telegram, Twilio. Adding a detector means updating `config_extractor.py` and noting it here — this list is a security contract.
- **SourceCodeExtractor** — tries `tree-sitter` first for AST fidelity on Python / JS / TS / Java / Go / Rust / C when the library is installed, and falls back to a multi-language regex scanner otherwise.

---

**Document Version**: 5.3
**Last Updated**: 2026-04-18
**Status**: FINAL
**Predecessor**: v5.2 (data-preprocessing-design-specification.md)
