# Data Preprocessing Design Specification v4.0

**Status**: FINAL
**Date**: 2026-02-17
**Supersedes**: v3.2

---

## Change Summary from v3.2

| Area | v3.2 | v4.0 |
|------|------|------|
| **Tier count** | 3 tiers (0: Classification, 1: Mechanical Extraction, 2: Deep Analysis) | 4 tiers (0+1: Structural Indexing, 2: Mechanical Search, 3: Deep LLM Analysis, 4: Vectorization) |
| **Vectorization** | Eager background async after every upload | On-demand, triggered by user query demand |
| **`search_file` tool** | Does not exist | New agent tool for Tier 2 mechanical search |
| **Domain extractors** | Single-use at upload time | Re-runnable with different parameters |
| **Evidence form** | `USER_INPUT` (hardcoded default) | `USER_TEXT` / `SUBMITTED_DATA` (classification-driven) |
| **Evidence from search** | Not specified | Rules for when/how search results become evidence |
| **Agent escalation** | Tier 1 summary → Tier 2 deep analysis | Tier 1 → Tier 2 → Tier 3 → Tier 4 progressive ladder |
| **Classification model** | 6 unified types in spec, 12 detailed types hidden in code | Spec acknowledges 12→6 two-layer model; 11 distinct extractors |
| **Pasted text** | Bypasses classification and extraction entirely | Routed through Tier 0+1 via unified `/turns` endpoint (payload-driven, no LLM classification) |
| **Classification fallback** | Low confidence → `UNSTRUCTURED_TEXT` + user modal | Best-effort: highest-scoring candidate extractor, no user modal |

---

## 1. Architecture Overview

### 1.1 Four-Tier Processing Model

Data submitted to FaultMaven — whether uploaded files or pasted text — progresses through up to four processing tiers. Each tier is more expensive and more thorough than the previous. The system starts cheap and escalates only when cheaper tiers fail to answer the user's questions.

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
              Agent has summary +
              structural index.
              Can answer question?
                        │
                   ┌────┴────┐
                YES│         │NO
                   │         v
             ┌─────┘  ┌──────────────────────────┐
             │        │  TIER 2: Mechanical Search │  On-demand. $0. ~1s.
             │        │  search_file tool           │  Grep/regex on raw file.
             │        │  Re-run domain extractors   │  No LLM.
             │        │  → Raw excerpts             │
             │        └────────────┬─────────────────┘
             │                     │
             │           Can answer question?
             │                     │
             │                ┌────┴────┐
             │             YES│         │NO
             │                │         v
             │          ┌─────┘  ┌──────────────────────────┐
             │          │        │  TIER 3: Deep LLM Analysis│  On-demand. ~$0.01.
             │          │        │  deep_analyze_file tool    │  LLM analyzes specific
             │          │        │  → Interpreted answer +    │  data windows.
             │          │        │    supporting excerpts     │
             │          │        └────────────┬───────────────┘
             │          │                     │
             │          │           Can answer question?
             │          │                     │
             │          │               ┌─────┴────┐
             │          │            YES│          │NO + file qualifies
             │          │               │          v
             │          │         ┌─────┘  ┌──────────────────────────┐
             │          │         │        │  TIER 4: Vectorization    │  Rare. ~$0.05.
             │          │         │        │  Chunk + embed + store    │  User warned first.
             │          │         │        │  → Semantic search across │  Background async.
             │          │         │        │    all vectorized evidence│
             │          │         │        └──────────────────────────┘
             │          │         │
             v          v         v
         ┌──────────────────────────────┐
         │  AGENT RESPONSE              │
         │  → May create Evidence via   │
         │    evidence_to_add           │
         └──────────────────────────────┘
```

### 1.2 Design Principles

1. **Start cheap, escalate on demand.** Tier 0+1 runs on every submission. Tiers 2-4 run only when the user's questions can't be answered by cheaper tiers.

2. **Payload-driven evidence form.** Evidence form (`USER_TEXT` vs `SUBMITTED_DATA` vs `DOCUMENT`) is determined by payload context: attachments present → `DOCUMENT`, agent tool findings → `SUBMITTED_DATA`, query-only → `USER_TEXT`. *(Updated v4.1: was classification-driven via `submission_classification`, now payload-driven via unified turn pipeline.)*

3. **Single unified endpoint.** All turns arrive via `POST /cases/{id}/turns` as `{query?, attachments?[]}`. Preprocessing runs before LLM inference (Step 1), not after. *(Updated v4.1: was endpoint-agnostic with `/queries` and `/data`.)*

4. **Re-runnable extractors.** Domain-specific extractors (Crime Scene, Anomaly Detection, etc.) can be re-invoked with different parameters on follow-up queries, not just at upload time.

5. **Agent decides, user approves.** The agent decides when to escalate. For Tier 4 (vectorization), the agent warns the user about cost/time before proceeding.

### 1.3 Cost Matrix

| Tier | Trigger | Cost | Latency | LLM Calls |
|------|---------|------|---------|-----------|
| **0+1** | Every submission | $0.00 | <2s | 0 |
| **2** | Agent needs specific data | $0.00 | ~0.5-2s | 0 |
| **3** | Agent needs interpreted analysis | ~$0.01-0.05 | 3-15s | 1 |
| **4** | Agent needs semantic search; file qualifies | ~$0.05-0.50 | 10-60s | 0 (embedding only) |

---

## 2. Tier 0+1: Structural Indexing (Updated)

Runs synchronously on every submission that contains data (file uploads always; pasted text when classified as `submitted_data` or `mixed`). Two sub-phases:

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

| # | DetailedDataType | Extractor | Strategy | What It Produces | Unified |
|---|---|---|---|---|---|
| 1 | `LOGS_AND_ERRORS` | `LogsAndErrorsExtractor` | `crime_scene` | Severity-weighted error clusters, crime scene window (±200 lines around highest-severity error), burst detection (10+ errors in 50-line window), state transitions, timeline | **LOGS** |
| 2 | `ERROR_REPORT` | `ErrorReportExtractor` | `exception_context` | Stack frame parsing (Python/Java/JS/Go), exception type + message, user-code vs library-code filtering, prescriptive fix suggestions (e.g., NullPointer → None check) | **LOGS** |
| 3 | `TRACE_DATA` | `TraceDataExtractor` | `trace_correlation` | OTel + Jaeger span parsing, service call chain, critical path (top 3 slowest operations), slow spans (>20% of trace duration), error spans, duration normalization (ns/μs → ms) | **LOGS** |
| 4 | `COMMAND_OUTPUT` | `CommandOutputExtractor` | `command_parsing` | Format-specific parsing for `top`/`ps`/`netstat`/`df`/`free`/`iostat`/`vmstat`, CPU/memory/disk saturation thresholds (CPU >70%, mem >80%, disk >85%), offending process identification by PID | **LOGS** |
| 5 | `METRICS_AND_PERFORMANCE` | `MetricsAndPerformanceExtractor` | `statistical` | Auto-detect JSON/CSV/Prometheus format, per-metric stats (min/max/mean/std/p50/p95/p99), z-score anomaly detection (spikes >3σ, drops >50% below mean) | **METRICS** |
| 6 | `PROFILING_DATA` | `ProfilingDataExtractor` | `profiling_hotspot` | cProfile/flame graph/perf stat parsing, hotspot detection (>5% total time), recursive call flags, I/O function classification, optimization suggestions (memoization for recursion, async I/O for file/network) | **METRICS** |
| 7 | `STRUCTURED_CONFIG` | `StructuredConfigExtractor` | `direct` | YAML/JSON/TOML/INI/.env parsing, dual-layer secret redaction (key-name patterns + value patterns), hierarchical text output | **CONFIGURATION** |
| 8 | `SOURCE_CODE` | `SourceCodeExtractor` | `ast_parse` | Python AST (imports, class hierarchy, function signatures with return types, async markers), multi-language regex fallback (JS/TS/Java/Go/Rust/C/C++), TODO/FIXME extraction | **CODE** |
| 9 | `UNSTRUCTURED_TEXT` | `UnstructuredTextExtractor` | `direct` | Embedded error/code extraction from mixed text, markdown + plain text dual path, paragraph-based sections, error-keyword lines with ±2-line context | **TEXT** |
| 10 | `DOCUMENTATION` | `DocumentationExtractor` | `documentation_structure` | Section classification (troubleshooting/procedure/configuration), operational command filtering (kubectl, docker, systemctl, etc.), TOC generation | **TEXT** |
| 11 | `VISUAL_EVIDENCE` | `VisualEvidenceExtractor` | `vision` | Metadata only: format, dimensions, byte size (placeholder for Phase 3 multimodal LLM vision analysis) | **IMAGE** |
| 12 | `UNANALYZABLE` | *(none — fallback)* | — | Truncation to 10,000 chars | **TEXT** |

**Key insight**: The 4 extractors under LOGS produce fundamentally different output. A distributed trace parsed by `trace_correlation` (service call chain, critical path) is nothing like a log file parsed by `crime_scene` (error clusters, timeline). The 12→6 unification is a lossy compression for the external API; internally, the system leverages all 11 extractors.

**All 11 extractors use stdlib only** (re, ast, json). No external dependencies except optional `yaml`/`tomli` for config parsing. This means zero external risk, deterministic output, and fast execution.

### 2.3 Classification Fallback: Best-Effort Dispatch

When the classifier has low confidence (< 0.60), it still has partial pattern scores from its rule-based analysis. Instead of always falling back to `UNSTRUCTURED_TEXT`, v4.0 uses the highest-scoring candidate type.

**v3.2 behavior**: Low confidence → `UNSTRUCTURED_TEXT` with `classification_failed=True` → user modal for file uploads, no modal for pasted text → TEXT extractor (headings + key sentences — minimal value).

**v4.0 behavior**: Low confidence → highest-scoring candidate type → that type's extractor gets a chance → if the extractor produces useful output, we keep it; if not, the extractor's own fallback degrades gracefully.

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
    return ClassificationResult(
        data_type=best_type,
        confidence=0.50,
        source="rule_based_best_effort",
        classification_failed=True,  # Still flagged as uncertain
    )

# True fallback: nothing matched at all → UNSTRUCTURED_TEXT
return ClassificationResult(
    data_type=DataType.UNSTRUCTURED_TEXT,
    confidence=0.30,
    source="rule_based",
    classification_failed=True,
)
```

**Why this works**: Each extractor already handles content that doesn't match its expectations:
- `LogsAndErrorsExtractor`: No errors found → extracts last 500 lines (tail). No worse than TEXT.
- `MetricsAndPerformanceExtractor`: Non-numeric data → falls back to TEXT preview.
- `StructuredConfigExtractor`: Parse failure → TEXT extraction with regex secret redaction.
- `SourceCodeExtractor`: No language detected → TEXT extraction.

The extractor fallback chain (Section 4.9 of v3.2) ensures no extractor propagates errors. Best-effort dispatch gives the specialized extractor a *chance* to find something valuable, with the same safety net as before.

### 2.4 Pasted Text Processing

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
  │           → DataClassifier.classify(content, filename)
  │           → extractor.extract(content)
  │           → sanitize(result)
  │           → Evidence(form=DOCUMENT, preprocessed_content=structural_index)
  │
  ├─ STEP 2: LLM INFERENCE
  │   Context includes structural indexes via Context Sliding Window
  │   (Tier A: recent data evidence with full structural_index)
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
| **Deduplication** | SHA-256 of file bytes | SHA-256 of content |
| **Extractors used** | Same 11 | Same 11 |
| **Form** | `DOCUMENT` | `DOCUMENT` |
| **Preprocessing** | Step 1 (before LLM) | Step 1 (before LLM) |

**Output**: `PreprocessingResult` with `summary` (<500 chars) and `structural_index` (full extraction). Raw content stored via `content_ref`.

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
    - You need LLM interpretation (use deep_analyze_file instead)

    search_type options:
    - "keyword": Split query into keywords, find matching lines with context
    - "regex": Treat query as a regex pattern
    - "extractor": Re-run domain extractor with extractor_params
    """
```

### 3.3 Search Modes

#### A. Keyword Search

Tokenizes the query into keywords, scans lines for matches, returns context windows. This is the existing `BasicTier2Service._keyword_search()` logic, now directly accessible as an agent tool.

```python
async def _keyword_search(
    raw_content: str,
    query: str,
    context_lines: int = 20,
    max_results: int = 5,
) -> List[DataExcerpt]:
    """
    Keyword search: tokenize query, find matching lines, extract ±context windows.
    Merge overlapping windows. Return up to max_results excerpts.
    """
    keywords = [kw.lower() for kw in query.split() if len(kw) > 2]
    lines = raw_content.split('\n')
    matches = []

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(kw in line_lower for kw in keywords):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines)
            matches.append(DataExcerpt(
                content='\n'.join(f"{j+1}: {lines[j]}" for j in range(start, end)),
                line_start=start + 1,
                line_end=end,
            ))

    return merge_overlapping(matches)[:max_results]
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

Re-runs a domain-specific extractor with different parameters. The extractor is selected based on the evidence's `data_type`.

**Use cases:**
- Log file was initially processed with default crime scene window (±200 lines around highest severity error). User asks about a different time range → re-run with `time_window=("14:45:00", "14:46:00")`.
- Metrics file was profiled with default z-score threshold (3.0). User wants to see more anomalies → re-run with `z_score_threshold=2.0`.
- Log file's initial extraction focused on errors. User asks about warnings → re-run with `min_severity="WARN"`.

```python
async def _rerun_extractor(
    raw_content: str,
    detailed_data_type: DetailedDataType,
    params: dict,
) -> ExtractionResult:
    """
    Re-run a domain-specific extractor with overridden parameters.

    Uses the evidence's DetailedDataType (12 types) to select the exact
    extractor that originally processed the file, not the unified type.
    See Appendix B for supported params per extractor.
    """
    # Dispatch by DetailedDataType → same extractor that ran at upload
    extractor = preprocessing_service.extractors.get(detailed_data_type)
    if not extractor:
        return extract_text_structure(raw_content)

    # Apply parameter overrides (extractor-specific)
    return extractor.extract(raw_content, **params)
```

### 3.4 Relationship to Existing Services

The `search_file` tool consolidates functionality that partially exists today:

| Existing Component | Role | In v4.0 |
|-------------------|------|---------|
| `BasicTier2Service._keyword_search()` | Keyword search on raw content | Promoted to `search_file` keyword mode |
| `ReadFileTool` (`read_file`) | Read file content by evidence ID | Remains for reading; `search_file` adds search |
| Domain extractors (Tier 1) | Single-use at upload | Made re-runnable via `search_file` extractor mode |
| `DeepAnalysisTool` (`deep_analysis`) | LLM-powered analysis | Moves to Tier 3 (unchanged) |

### 3.5 When to Use `search_file` vs `deep_analyze_file`

| Scenario | Tool | Why |
|----------|------|-----|
| "Find all lines containing 'timeout'" | `search_file` (keyword) | Pure text match, no interpretation needed |
| "Show me errors between 14:00-14:15" | `search_file` (regex: `14:0[0-9].*ERROR\|14:1[0-5].*ERROR`) | Pattern matching on timestamps |
| "What anomalies are there at z>2?" | `search_file` (extractor, `z_score_threshold=2.0`) | Re-run extractor with different params |
| "What's causing the connection timeouts?" | `deep_analyze_file` | Needs LLM interpretation |
| "Summarize the error patterns after the deployment" | `deep_analyze_file` | Needs LLM synthesis |

---

## 4. Tier 3: Deep LLM Analysis (Renamed from Tier 2)

> **Functionally unchanged from v3.2 Section 6.** Renumbered from Tier 2 → Tier 3.

The agent tool is `deep_analyze_file` (was already defined in v3.2). It calls `ITier2AnalysisService.analyze()` with one of the pluggable backends (external, local LLM, or basic search).

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

> **Note**: The old `TIER2_*` config names are no longer supported. Phase 5 performed a clean break — use `DEEP_ANALYSIS_*` exclusively. In the codebase, the service is still called `ITier2AnalysisService`. The "Tier 3" naming is a spec-level concept for the four-tier processing model.

---

## 5. Tier 4: Vectorization (Redesigned)

### 5.1 Key Change: Eager → On-Demand

**v3.2 behavior**: After every file upload, the Tier 1 structural index is chunked, embedded, and stored in ChromaDB as a background async task. Every file gets vectorized.

**v4.0 behavior**: Vectorization only happens when:
1. The file qualifies (meets size and type criteria)
2. The user demonstrates continued interest (cheaper tiers failed)
3. The agent proposes vectorization and the user approves

### 5.2 Three Qualification Factors

Vectorization is gated by three factors. All three must pass.

#### Factor 1: Size Minimum

Files below the size threshold are fully representable by their Tier 1 structural index. Vectorizing them adds no retrieval value.

```python
VECTORIZATION_MIN_SIZE_BYTES = 50_000  # 50KB of raw content

def passes_size_minimum(evidence: Evidence) -> bool:
    return evidence.content_size_bytes >= VECTORIZATION_MIN_SIZE_BYTES
```

**Rationale**: A 5KB config file's Tier 1 output is the full parsed config. A 50KB log file's Tier 1 output is a structural index that may omit details. The threshold is configurable.

#### Factor 2: User Query Demand

The user must have asked questions that cheaper tiers could not answer. Vectorization happens reactively, not speculatively.

**Decision flow:**
```
User asks question about file X
  → Agent tries Tier 1 (structural index)
    → Sufficient? → Respond. No vectorization.
    → Insufficient?
      → Agent tries Tier 2 (search_file)
        → Sufficient? → Respond. No vectorization.
        → Insufficient?
          → Agent tries Tier 3 (deep_analyze_file)
            → Sufficient? → Respond. No vectorization.
            → Insufficient for *this* query but user keeps asking about
              this file across multiple turns?
              → Agent proposes vectorization.
```

The agent proposes vectorization when it detects a pattern: the user is repeatedly asking about the same large file, and point queries (Tier 2-3) are insufficient because the questions span the entire file (e.g., "find all places where X correlates with Y").

**Agent prompt guidance:**
```
When you've used search_file and deep_analyze_file on the same file
across multiple turns and the user's questions require scanning the
entire file rather than specific sections, consider suggesting
vectorization:

"This file is large (X MB) and your questions require searching
across the entire document. I can vectorize this file for faster
semantic search, which will take about Y seconds. Shall I proceed?"

Only suggest vectorization when:
- The file passes the size minimum (>50KB)
- You've already tried Tier 2 and/or Tier 3
- The user's question pattern suggests full-file search is needed
- The file does not exceed the max size cap
```

#### Factor 3: Max Size Cap

Files above the max size cap are too expensive to vectorize (embedding cost scales linearly with content size).

```python
VECTORIZATION_MAX_SIZE_BYTES = 50_000_000  # 50MB

def passes_size_cap(evidence: Evidence) -> bool:
    return evidence.content_size_bytes <= VECTORIZATION_MAX_SIZE_BYTES
```

For files above the cap, the agent should use Tier 2 (targeted search) and Tier 3 (windowed LLM analysis) instead.

### 5.3 Vectorization Trigger: Agent Tool

```python
@tool(name="vectorize_file")
async def vectorize_file(
    evidence_id: str,
) -> str:
    """
    Vectorize a previously uploaded file for semantic search.

    This chunks the file's structural index, generates embeddings, and
    stores them in ChromaDB. After vectorization, you can search this
    file's content via knowledge_base_search.

    IMPORTANT: Only call this after confirming with the user. Vectorization
    is a heavier operation that takes 10-60 seconds depending on file size.

    Prerequisites (system-enforced):
    - File must be >50KB (VECTORIZATION_MIN_SIZE_BYTES)
    - File must be <50MB (VECTORIZATION_MAX_SIZE_BYTES)
    """
```

### 5.4 What Gets Vectorized

The Tier 1 **structural index** — not the raw file content. This is unchanged from v3.2 Section 5.

- 10MB log file → Structural index (~50KB) → ~100 chunks
- 5MB metrics CSV → Statistical profile (~30KB) → ~60 chunks
- 5KB config → Full parsed config → skipped (below size minimum)

**Chunking strategy**: Section-aware splitting (unchanged from v3.2 Section 5.2).

### 5.5 Configuration

```bash
# Tier 4: Vectorization (on-demand)
VECTORIZATION_MIN_SIZE_BYTES=50000       # Skip files smaller than this
VECTORIZATION_MAX_SIZE_BYTES=50000000    # Skip files larger than this
CHROMADB_HOST=localhost
CHROMADB_PORT=8000
CHROMADB_COLLECTION_PREFIX=case_

# Chunking
VECTOR_CHUNK_SIZE_TOKENS=500
VECTOR_CHUNK_OVERLAP_TOKENS=50
```

### 5.6 Migration from v3.2

In v3.2, `store_in_vector_db_background()` is called automatically after every upload. In v4.0, this background call is removed from the upload path. Instead, `vectorize_file` is called explicitly by the agent.

**Code change**: Remove the `store_in_vector_db_background()` call from the upload flow. The function itself remains available for the `vectorize_file` tool.

---

## 6. Agent Escalation Decision Tree

The investigation agent follows this decision tree when a user asks a question about submitted data:

```python
async def decide_data_access_tier(
    query: str,
    relevant_evidence: List[Evidence],
) -> str:
    """
    Agent's internal reasoning for data access tier selection.
    This is encoded in the system prompt, not as executable code.
    """

    # Step 1: Check Tier 1 (always available)
    # Agent reviews evidence.summary and structural_index
    # (structural_index is included in evidence context for the current turn,
    #  and retrievable via vector DB if previously vectorized)
    if structural_index_answers_query(query, relevant_evidence):
        return "respond_from_tier1"

    # Step 2: Try Tier 2 (search_file — zero cost)
    # Agent calls search_file to grep/regex the raw file
    if query_is_about_specific_pattern_or_value():
        return "call_search_file"

    # Step 3: Try Tier 3 (deep_analyze_file — low cost)
    # Agent calls deep_analyze_file for LLM-interpreted analysis
    if query_needs_interpretation_or_synthesis():
        return "call_deep_analyze_file"

    # Step 4: Consider Tier 4 (vectorize_file — higher cost)
    # Only if: large file + repeated queries + questions span full file
    if (file_passes_size_checks()
        and user_has_asked_multiple_questions_about_this_file()
        and questions_require_full_file_search()):
        return "propose_vectorization_to_user"

    # Fallback: respond with what we have
    return "respond_with_available_data"
```

**System prompt excerpt for agent:**

```
## Data Access Strategy

When a user asks about uploaded data, follow this escalation order:

1. **Check your context first** (free, instant): Recent evidence includes
   structural indexes (crime scene extractions, statistical profiles, parsed
   configs) directly in the <evidence_collected> section. Check these first.
   If a structural_index shows [TRUNCATED], the full content is available
   via search_file or read_file.

2. **search_file** (free, fast): If the structural index lacks detail or
   was truncated, use search_file to grep for specific keywords, patterns,
   or timestamps in the raw file.

3. **deep_analyze_file** (low cost, slower): If you need LLM interpretation
   of specific data sections — root cause analysis, correlation detection,
   or synthesizing findings across file sections.

4. **vectorize_file** (higher cost, rare): Only suggest when the user is
   repeatedly asking questions about a large file and point queries are
   insufficient. Always ask the user before vectorizing.

Never skip tiers. Always try the cheaper option first.
```

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
After using search_file or deep_analyze_file, create evidence ONLY when
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

#### From Tier 3 (`deep_analyze_file`) Results

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
    """Evidence derived from agent tool use (search_file, deep_analyze_file results).
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
| `services/preprocessing/classifier.py` | Add best-effort fallback (highest-scoring candidate instead of always UNSTRUCTURED_TEXT) |
| `services/preprocessing/preprocessing_service.py` | Expose `classify_and_extract(content, filename)` for pasted text path |
| `modules/agent/tools/deep_analysis_tool.py` | No change (becomes Tier 3 tool) |
| `container/providers/tools.py` | Register `search_file` and `vectorize_file` tools |
| System prompt | Add escalation strategy guidance |

### 9.2 Backward Compatibility

- Existing `deep_analyze_file` tool continues to work unchanged (renamed conceptually to Tier 3)
- `TIER2_*` config keys renamed to `DEEP_ANALYSIS_*` (clean break, no backward compat)
- Existing vectorized data in ChromaDB remains searchable
- No database migration needed (Tier 4 uses same ChromaDB schema)

### 9.3 Agent Tool Summary

| Tool | Tier | Cost | LLM? | When |
|------|------|------|------|------|
| *(evidence context)* | 1 | $0 | No | Always available |
| `search_file` | 2 | $0 | No | Agent needs specific data from raw file |
| `deep_analyze_file` | 3 | ~$0.01 | Yes | Agent needs interpreted analysis |
| `vectorize_file` | 4 | ~$0.05+ | Embed only | Agent proposes, user approves |
| `knowledge_base_search` | Post-4 | $0 | No | After vectorization, semantic search |

---

## 10. Deferred Items

These items are out of scope for the initial v4.0 implementation but are documented for future consideration:

| Item | Description | When |
|------|-------------|------|
| **Frontend paste detection** | `onPaste` handler in `UnifiedInputBar.tsx` to send structured `{typed, pasted}` segments | After backend stabilizes |
| ~~**Unified endpoint processing**~~ | ~~Merge `/queries` and `/data` into single endpoint~~ | **Done in v4.1** — unified `process_turn()` pipeline (Phase 1-2) |
| **Cross-file correlation** | Tier 3 analysis across multiple files simultaneously | Requires multi-file context windowing |
| **Vectorization cost tracking** | Track per-case vectorization costs for billing | Enterprise feature |
| **DIFF_PATCH extractor** | Parse unified diffs / git patches — files changed, lines added/removed | When deployment-change investigations are common |
| **THREAD_DUMP extractor** | JVM thread dump parsing — deadlock detection, lock contention | When Java-heavy user base emerges |

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
DEEP_ANALYSIS_MAX_FILE_SIZE_MB=50
# NOTE: Old TIER2_* names are no longer supported (clean break in Phase 5)

# ============================================================
# TIER 4: VECTORIZATION (on-demand)
# ============================================================
VECTORIZATION_MIN_SIZE_BYTES=50000       # 50KB minimum
VECTORIZATION_MAX_SIZE_BYTES=50000000    # 50MB maximum
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

## Appendix B: Extractor Re-run Parameters

When the agent calls `search_file` with `search_type="extractor"`, the extractor is selected based on the evidence's `DetailedDataType` (not the unified type). This ensures re-runs use the same specialized extractor that processed the file originally.

### LOGS_AND_ERRORS — `LogsAndErrorsExtractor`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `time_window` | `tuple[str, str]` | None | Start/end timestamps to filter log lines |
| `min_severity` | `str` | `"ERROR"` | Minimum severity to include (`WARN`, `ERROR`, `CRITICAL`, `FATAL`) |
| `context_lines` | `int` | 200 | Lines of context around error clusters |
| `cluster_gap` | `int` | 50 | Max lines between errors to form a cluster |
| `burst_threshold` | `int` | 10 | Errors within cluster_gap to trigger burst detection |
| `tail_lines` | `int` | 500 | Lines from end to extract if no errors found |

### ERROR_REPORT — `ErrorReportExtractor`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `language` | `str` | auto-detect | Override language detection (python/java/javascript/go) |
| `include_library_frames` | `bool` | False | Include library/framework frames in call path (default: user code only) |

### TRACE_DATA — `TraceDataExtractor`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `slow_span_threshold` | `float` | 0.20 | Fraction of total trace duration to flag as slow (default: 20%) |
| `service_filter` | `list[str]` | None | Only analyze spans from these services |

### COMMAND_OUTPUT — `CommandOutputExtractor`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cpu_threshold` | `int` | 70 | CPU % to flag as hog |
| `mem_threshold` | `int` | 80 | Memory % to flag as hog |
| `disk_threshold` | `int` | 85 | Disk % to flag as full |

### METRICS_AND_PERFORMANCE — `MetricsAndPerformanceExtractor`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `z_score_threshold` | `float` | 3.0 | Z-score threshold for anomaly detection |
| `columns` | `list[str]` | None | Specific metrics/columns to analyze (None = all) |
| `time_range` | `tuple[str, str]` | None | Filter to specific time range |

### PROFILING_DATA — `ProfilingDataExtractor`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hotspot_threshold` | `float` | 0.05 | Fraction of total time to flag as hotspot (default: 5%) |
| `top_n` | `int` | 5 | Number of top hotspots to report |

### STRUCTURED_CONFIG — `StructuredConfigExtractor`

No re-run parameters. Config extraction is deterministic — same input always produces same output.

### SOURCE_CODE — `SourceCodeExtractor`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `language` | `str` | auto-detect | Override language detection |
| `include_bodies` | `bool` | False | Include function bodies (not just signatures) |

### UNSTRUCTURED_TEXT, DOCUMENTATION, VISUAL_EVIDENCE

No re-run parameters. These extractors have no tunable thresholds.

---

**Document Version**: 4.0
**Last Updated**: 2026-02-17
**Status**: FINAL
**Predecessor**: v3.2 (data-preprocessing-design-specification.md, Sections 1-10)
