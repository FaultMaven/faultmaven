# FaultMaven Data Preprocessing Architecture v3.0

## Executive Summary

This document defines the **three-tier data preprocessing model** that transforms raw user-uploaded files into structured, queryable evidence for the investigation system.

**Three-Tier Model**:
1. **Tier 0 — Classification** (always runs, <100ms, 0 LLM calls): Rule-based data type detection
2. **Tier 1 — Mechanical Extraction** (always runs, <2s, 0 LLM calls): Type-specific structural indexing that makes data permanently queryable
3. **Tier 2 — Deep Analysis** (on-demand, pluggable external service): LLM-powered analysis triggered by agent decision or user question

**Key Principles**:
1. **Tier 1 makes data queryable, Tier 2 makes it understood**: Every file gets a structural index. Only files that matter get deep analysis.
2. **Tier 2 is a pluggable external service**: Supports Gemini file search, OpenAI assistants, local RAG, or custom backends.
3. **Lazy staging**: Raw files are stored at Tier 1. They are staged to the Tier 2 backend only when the first deep query arrives.
4. **Cost control by design**: Tier 0+1 cost ~$0. Tier 2 costs are incurred only when the investigation needs deeper analysis.
5. **Separation of concerns**: Preprocessing extracts and indexes; Evidence Architecture evaluates and links to hypotheses.

**Design Rationale — Why Tiered?**

Users submit varied data (logs, metrics, configs, code, screenshots). Files can be very large. The system must:
- Support follow-up Q&A about any uploaded file (not just extract evidence for current hypotheses)
- Retain data structure for ongoing investigation and conversational context
- Control cost — running full LLM analysis on every file is impractical

The tiered model solves this: Tier 1 produces a cheap structural index that's rich enough for the agent to answer "what's in this file?" and decide which files deserve Tier 2 deep analysis.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [System Integration](#2-system-integration)
3. [Tier 0: Classification](#3-tier-0-classification)
4. [Tier 1: Mechanical Extraction](#4-tier-1-mechanical-extraction)
5. [Vector DB Storage](#5-vector-db-storage)
6. [Tier 2: Deep Analysis Service](#6-tier-2-deep-analysis-service)
7. [Output Formats](#7-output-formats)
8. [Configuration](#8-configuration)
9. [Implementation Guide](#9-implementation-guide)
10. [Examples](#10-examples)

---

## 1. Architecture Overview

### 1.1 Role in System Architecture

**Preprocessing is the first stage in the evidence lifecycle**:

```
USER UPLOADS FILE
       ↓
┌─────────────────────────────────────────────────────────────┐
│ TIER 0 + TIER 1 (THIS DOCUMENT — Synchronous)               │
│ Classify → Extract structural index → Sanitize → Store       │
│ Time: <2s | LLM Calls: 0 | Cost: ~$0                        │
└─────────────────┬───────────────────────────────────────────┘
                  │ PreprocessingResult
                  ↓
┌─────────────────────────────────────────────────────────────┐
│ EVIDENCE ARCHITECTURE v1.1                                   │
│ Classify evidence → Create Evidence object → Link hypotheses │
└─────────────────┬───────────────────────────────────────────┘
                  │ Evidence + Hypothesis linkage
                  ↓
┌─────────────────────────────────────────────────────────────┐
│ AGENT RESPONSE GENERATION                                    │
│ Generate conversational analysis from Tier 1 structural index│
└─────────────────────────────────────────────────────────────┘

        ┌── PARALLEL ASYNC (Fire-and-forget) ──┐
        ↓                                       ↓
┌──────────────────────┐    ┌──────────────────────────────────┐
│ VECTOR DB STORAGE     │    │ RAW FILE STORAGE                  │
│ Store structural index│    │ Store raw file for Tier 2 access  │
│ for semantic search   │    │ (local filesystem or S3)          │
└──────────────────────┘    └──────────────────────────────────┘

                ... Later, when agent or user needs deeper analysis ...

┌─────────────────────────────────────────────────────────────┐
│ TIER 2: DEEP ANALYSIS SERVICE (THIS DOCUMENT — On-Demand)    │
│ External pluggable service called via ITier2AnalysisService  │
│                                                              │
│ Backends:                                                    │
│   Cloud SaaS:  Gemini file search, OpenAI assistants         │
│   Local + LLM: In-process RAG with Ollama/vLLM              │
│   Local basic: Keyword/regex search, no LLM                 │
│   Disabled:    Agent works from Tier 1 index only            │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Three-Tier Processing Model

```
USER UPLOADS FILE (10MB max)
        ↓
┌─────────────────────────────────────────────────────────────┐
│ TIER 0: CLASSIFICATION (<100ms, 0 LLM calls)                │
│                                                              │
│ • Size validation (≤10MB)                                    │
│ • Rule-based classification → 6 data types                   │
│ • Pattern matching on first 5KB of content                   │
│ • Output: DataType enum + confidence score                   │
└──────────────────────┬──────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────────┐
│ TIER 1: MECHANICAL EXTRACTION (<2s, 0 LLM calls)            │
│                                                              │
│ Type-specific structural indexing:                            │
│ • LOGS → Structural index (all error clusters,    │
│   timeline, severity distribution, state transitions)        │
│ • METRICS → Statistical profile (anomalies,  │
│   min/max/mean, threshold breaches)                          │
│ • CONFIGURATION → Parse structure, redact secrets         │
│ • CODE → AST extraction (functions, classes, imports)  │
│ • TEXT → Structure extraction (headings,         │
│   sections, key sentences via TF-IDF)                        │
│ • IMAGE → Metadata extraction (format, dimensions)  │
│                                                              │
│ Then: Sanitize PII/secrets → Store raw file → Package result │
│                                                              │
│ Output: PreprocessingResult with structural index             │
└──────────────────────┬──────────────────────────────────────┘
                       │
              ┌────────┴─────────┐
              ↓                  ↓
     Evidence Creation    Vector DB + Raw Storage
     (synchronous)        (async background)
              │
              ↓
     Agent Response from Tier 1 index
              │
              ↓
    ... time passes, user asks follow-up ...
              │
              ↓
┌─────────────────────────────────────────────────────────────┐
│ TIER 2: DEEP ANALYSIS (on-demand, external service)          │
│                                                              │
│ Triggered when:                                              │
│ • User asks a question that Tier 1 index can't answer        │
│ • Agent decides deeper analysis is needed for a hypothesis   │
│ • Investigation reaches a stage where this data is relevant  │
│                                                              │
│ Process:                                                     │
│ 1. Retrieve raw file from storage                            │
│ 2. Stage to Tier 2 backend (lazy, first query only)          │
│ 3. Send query + context to backend                           │
│ 4. Return answer + supporting excerpts                       │
│                                                              │
│ Backends: Gemini | OpenAI | Local RAG | Basic search         │
└─────────────────────────────────────────────────────────────┘
```

### 1.3 Cost Model

| Tier | When | LLM Calls | Cost per File | Example (10 files uploaded) |
|------|------|-----------|---------------|----------------------------|
| Tier 0 | Always | 0 | $0.00 | $0.00 (all 10) |
| Tier 1 | Always | 0 | $0.00 | $0.00 (all 10) |
| Tier 2 | On-demand | 1-25 | $0.003-$0.05 | $0.01-$0.10 (2-3 files queried) |

**Compared to processing all files deeply**: 10 files × $0.05 = $0.50 → reduced to $0.01-$0.10 (80-98% savings).

---

## 2. System Integration

### 2.1 Preprocessing Boundaries

**Preprocessing DOES** (Tier 0 + Tier 1):
- ✅ Classify data type (logs, metrics, config, etc.)
- ✅ Extract structural index (error clusters, anomalies, parse trees)
- ✅ Sanitize PII and secrets
- ✅ Store raw file for later Tier 2 access
- ✅ Return structured `PreprocessingResult`
- ✅ Store structural index in Vector DB (async)

**Preprocessing DOES** (Tier 2 — on-demand):
- ✅ Answer specific questions about raw data
- ✅ Perform LLM-based summarization when requested
- ✅ Extract targeted sections from large files
- ✅ Run vision analysis on images when referenced

**Preprocessing DOES NOT**:
- ❌ Evaluate evidence against hypotheses (Evidence Architecture's job)
- ❌ Update hypothesis status (Evidence Architecture's job)
- ❌ Link evidence to hypotheses (Evidence Architecture's job)
- ❌ Calculate confidence scores (Evidence Architecture's job)

### 2.2 Integration with Evidence Architecture

**Data Flow**:

```python
# 1. Tier 0 + Tier 1 Preprocessing (THIS DOCUMENT)
preprocessing_result = await preprocessing_service.process_upload(
    file=uploaded_file,
    case_id=case_id,
)
# Output: PreprocessingResult with structural index, stored raw file

# 2. Evidence Classification (EVIDENCE ARCHITECTURE)
classification = await classification_service.classify_user_input(
    user_input=preprocessing_result.summary,
    case=case,
)
# Output: EvidenceClassification (6 dimensions)

# 3. Evidence Creation (EVIDENCE ARCHITECTURE)
evidence = await evidence_service.create_evidence(
    preprocessing_result=preprocessing_result,
    case_id=case_id,
    phase=case.current_phase,
    classification=classification,
    uploaded_by=current_user,
)
# Output: Evidence object stored in DB

# 4. Hypothesis Analysis (EVIDENCE ARCHITECTURE)
case = await hypothesis_analysis_service.analyze_evidence_impact(
    evidence=evidence,
    classification=classification,
    case=case,
)
# Output: Updated hypothesis evidence_links, status changes

# 5. Async: Vector DB + Raw Storage (THIS DOCUMENT)
background_tasks.add_task(
    store_in_vector_db,
    case_id=case_id,
    structural_index=preprocessing_result.structural_index,
    evidence_id=evidence.evidence_id,
)

# ... Later, when deeper analysis is needed ...

# 6. Tier 2 Deep Analysis (THIS DOCUMENT — On-Demand)
deep_result = await tier2_service.analyze(
    file_ref=evidence.content_ref,
    query="What happened between 14:00 and 14:15?",
    context={"case_summary": case.summary, "hypotheses": case.hypotheses},
    data_type=preprocessing_result.data_type,
)
# Output: DeepAnalysisResult with answer + excerpts
```

### 2.3 Unified DataType

Preprocessing and evidence share a single `DataType` enum. No mapping is needed — the classification result flows directly through the evidence pipeline.

| DataType | Description | Evidence Form |
|----------|-------------|---------------|
| `LOGS` | Time-ordered diagnostic output (logs, traces, command output) | `DOCUMENT` |
| `METRICS` | Quantitative measurements (time-series, dashboards, alerts) | `DOCUMENT` |
| `CONFIGURATION` | Structured system/app config (YAML, JSON, TOML, env) | `DOCUMENT` |
| `CODE` | Source code files | `DOCUMENT` |
| `TEXT` | Unstructured prose (docs, runbooks, descriptions) | `DOCUMENT` |
| `IMAGE` | Visual content (screenshots, diagrams) | `DOCUMENT` |

All file uploads have `form=DOCUMENT`. Text entered via query endpoint has `form=USER_TEXT` or `form=SUBMITTED_DATA` (determined by LLM's `submission_classification`).

---

## 3. Tier 0: Classification

**Purpose**: Instant, deterministic classification without LLM calls.

**Time budget**: <100ms. **LLM calls**: 0. **Cost**: $0.

### 3.1 File Size Limit

**Hard Limit**: **10 MB** (10,485,760 bytes)

**Rationale**:
- Tier 1 structural extraction achieves high compression (200:1 for logs)
- Handles 95% of troubleshooting files (250K log lines, 500K metric rows)
- Prevents memory exhaustion and timeout issues

**Rejection Response**:
```json
{
  "error": "file_too_large",
  "file_size": 15728640,
  "max_size": 10485760,
  "message": "File exceeds 10MB limit",
  "suggestions": [
    "Upload only the relevant time range (last hour of logs)",
    "Filter to ERROR/FATAL level logs only",
    "Upload specific component logs, not entire system",
    "Split the file into smaller chunks"
  ]
}
```

### 3.2 Data Type Classification (6 Types)

Rule-based, no LLM calls. See [Data Classification Strategy](./data-classification-strategy.md) for the full classification algorithm, disambiguation rules, and fallback chain.

```python
class DataType(str, Enum):
    """Data type classification — shared across preprocessing and evidence"""
    LOGS = "logs"                    # Time-ordered diagnostic output
    METRICS = "metrics"              # Quantitative measurements
    CONFIGURATION = "configuration"  # Structured system/app config
    CODE = "code"                    # Source code
    TEXT = "text"                    # Unstructured prose
    IMAGE = "image"                  # Visual content

def classify_data_type(
    filename: str,
    content_sample: bytes,
    mime_type: str,
) -> ClassificationResult:
    """
    Classify file type using pattern matching on first 5KB.
    No LLM calls - pure rule-based.

    Returns ClassificationResult with data_type and confidence in <100ms.
    """

    # Check MIME type first (images)
    if mime_type.startswith("image/"):
        return ClassificationResult(
            data_type=DataType.IMAGE, confidence=0.99
        )

    sample = content_sample[:5000].decode('utf-8', errors='ignore')

    # Check for structured config (by extension)
    if filename.endswith(('.yaml', '.yml', '.json', '.toml', '.ini', '.conf')):
        return ClassificationResult(
            data_type=DataType.CONFIGURATION, confidence=0.92
        )

    # Check for source code (by extension)
    if filename.endswith(('.py', '.js', '.java', '.go', '.rb', '.cpp', '.c', '.rs')):
        return ClassificationResult(
            data_type=DataType.CODE, confidence=0.95
        )

    # Check for logs (content patterns)
    log_patterns = [
        r'\d{4}-\d{2}-\d{2}',
        r'ERROR|FATAL|CRITICAL|Exception|Traceback',
        r'\[\w+\]',
    ]
    if any(re.search(pattern, sample) for pattern in log_patterns):
        return ClassificationResult(
            data_type=DataType.LOGS, confidence=0.85
        )

    # Check for metrics (content patterns)
    metrics_patterns = [
        r'\d+\.\d+,\d+\.\d+',
        r'"value":\s*\d+',
        r'cpu_usage|memory_usage|latency|throughput',
    ]
    if any(re.search(pattern, sample) for pattern in metrics_patterns):
        return ClassificationResult(
            data_type=DataType.METRICS, confidence=0.80
        )

    # Default: unstructured text
    return ClassificationResult(
        data_type=DataType.TEXT, confidence=0.50
    )
```

**Classification Time**: <100ms (pattern matching on first 5KB)

---

## 4. Tier 1: Mechanical Extraction

**Purpose**: Create a structural index that makes the data permanently queryable — rich enough for the agent to answer "what's in this file?" and decide which files need Tier 2.

**Time budget**: <2s. **LLM calls**: 0. **Cost**: $0.

**Design principle**: Tier 1 must produce enough signal that the agent can determine which files are relevant to the current investigation without ever running an LLM over them.

### 4.1 Processing Matrix

| Data Type | Extraction Strategy | Output | Compression | Speed |
|-----------|-------------------|--------|-------------|-------|
| **LOGS** | Structural Index | Timeline, error clusters, state transitions | 200:1 | 0.5s |
| **METRICS** | Statistical Profile | Anomalies, distributions, thresholds | 167:1 | 0.3s |
| **CONFIGURATION** | Parse & Sanitize | Full structure with secrets redacted | 1:1 | 0.2s |
| **CODE** | AST Extraction | Functions, classes, imports, signatures | 50:1 | 0.5s |
| **TEXT** | Structure Extraction | Headings, sections, key sentences | 20:1 | 0.3s |
| **IMAGE** | Metadata Extraction | Format, dimensions, file size | N/A | 0.1s |

### 4.2 LOGS — Structural Index

The structural index is a comprehensive profile of the entire log file that the agent can query and that enables targeted Tier 2 retrieval.

```python
def build_log_structural_index(
    log_content: str, config: Config
) -> ExtractionResult:
    """
    Build a comprehensive structural index of the entire log file.

    The index captures:
    1. All error clusters (not just the worst one)
    2. Timeline with key events
    3. Severity distribution over time
    4. State transitions (restarts, deployments, config changes)
    5. The "crime scene" window (highest severity region)

    This is still zero-LLM — pure pattern matching and aggregation.

    Compression: ~200:1 (10MB → 50KB index)
    LLM Calls: 0
    Time: ~0.5s
    """

    lines = log_content.split('\n')
    total_lines = len(lines)

    # === 1. Score every line by severity ===
    scored_lines = []
    for i, line in enumerate(lines):
        severity = assign_severity(line, config.severity_keywords)
        timestamp = extract_timestamp(line)
        if severity > 0 or timestamp:
            scored_lines.append((i, line, severity, timestamp))

    # === 2. Build error clusters ===
    # Group consecutive errors into clusters
    error_clusters = []
    current_cluster = []
    for entry in scored_lines:
        if entry[2] > 0:  # Has severity
            if current_cluster and entry[0] - current_cluster[-1][0] > config.cluster_gap:
                error_clusters.append(summarize_cluster(current_cluster, lines))
                current_cluster = []
            current_cluster.append(entry)
    if current_cluster:
        error_clusters.append(summarize_cluster(current_cluster, lines))

    # === 3. Build timeline ===
    timeline = build_timeline(scored_lines, lines, config)
    # Output: [{timestamp, line_number, event_type, summary}, ...]

    # === 4. Severity distribution ===
    severity_dist = {}
    for _, line, severity, ts in scored_lines:
        if severity > 0:
            level = classify_severity_level(severity)
            severity_dist[level] = severity_dist.get(level, 0) + 1

    # === 5. State transitions ===
    state_transitions = detect_state_transitions(lines, config)
    # Detects: service starts/stops, deployments, config reloads, restarts

    # === 6. Crime scene (highest severity window) ===
    if scored_lines:
        crime_scene_entry = max(
            [e for e in scored_lines if e[2] > 0], key=lambda x: x[2]
        )
        crime_scene_idx = crime_scene_entry[0]
        start = max(0, crime_scene_idx - config.context_lines)
        end = min(total_lines, crime_scene_idx + config.context_lines + 1)
        crime_scene_lines = lines[start:end]
    else:
        crime_scene_lines = lines[-config.tail_extraction_lines:]

    # === 7. Unique error types ===
    unique_errors = extract_unique_error_types(scored_lines)

    # === 8. Time range ===
    timestamps = [ts for _, _, _, ts in scored_lines if ts]
    time_range = {
        "first": timestamps[0] if timestamps else None,
        "last": timestamps[-1] if timestamps else None,
    }

    # === Build the structural index ===
    index_parts = []

    # Header
    index_parts.append(
        f"=== LOG STRUCTURAL INDEX ===\n"
        f"Total lines: {total_lines}\n"
        f"Time range: {time_range['first']} to {time_range['last']}\n"
        f"Total errors: {sum(severity_dist.values())}\n"
        f"Severity distribution: {severity_dist}\n"
        f"Unique error types: {len(unique_errors)}\n"
        f"Error clusters: {len(error_clusters)}\n"
        f"State transitions: {len(state_transitions)}\n"
    )

    # Error clusters (each with representative lines)
    index_parts.append("\n=== ERROR CLUSTERS ===")
    for i, cluster in enumerate(error_clusters[:20]):  # Top 20 clusters
        index_parts.append(
            f"\nCluster {i+1}: lines {cluster['start_line']}-{cluster['end_line']}"
            f" ({cluster['count']} errors, severity={cluster['max_severity']})"
            f"\n  Time: {cluster['first_timestamp']} to {cluster['last_timestamp']}"
            f"\n  Types: {', '.join(cluster['error_types'])}"
            f"\n  Representative: {cluster['representative_line']}"
        )

    # State transitions
    if state_transitions:
        index_parts.append("\n=== STATE TRANSITIONS ===")
        for transition in state_transitions:
            index_parts.append(
                f"\n  Line {transition['line']}: [{transition['timestamp']}] "
                f"{transition['type']} - {transition['description']}"
            )

    # Unique error types
    index_parts.append("\n=== UNIQUE ERROR TYPES ===")
    for error_type, count in unique_errors[:30]:
        index_parts.append(f"  {error_type}: {count} occurrences")

    # Crime scene window
    index_parts.append(
        f"\n=== CRIME SCENE (lines {start}-{end}) ===\n"
    )
    index_parts.extend(crime_scene_lines)

    return ExtractionResult(
        method="structural_index",
        content='\n'.join(index_parts),
        metadata={
            "total_lines": total_lines,
            "total_errors": sum(severity_dist.values()),
            "error_clusters": len(error_clusters),
            "severity_distribution": severity_dist,
            "unique_error_types": len(unique_errors),
            "state_transitions": len(state_transitions),
            "time_range": time_range,
            "crime_scene_line": crime_scene_idx if scored_lines else None,
            "compression_ratio": len('\n'.join(index_parts)) / max(len(log_content), 1),
        },
        error_summary=ErrorSummary(
            total_errors=sum(severity_dist.values()),
            severity_distribution=severity_dist,
            first_error_line=scored_lines[0][0] if scored_lines else 0,
            last_error_line=scored_lines[-1][0] if scored_lines else 0,
            error_burst_detected=any(c['count'] > config.burst_threshold for c in error_clusters),
            unique_error_types=[e[0] for e in unique_errors[:20]],
        ),
    )
```

**What the structural index enables**:
- Agent can answer "how many errors are there?" → from severity_distribution
- Agent can answer "what happened at 14:15?" → find relevant cluster by timestamp → trigger Tier 2 for that line range
- Agent can answer "when did the service restart?" → from state_transitions
- Agent can decide "this file is relevant to the connection timeout hypothesis" → from error clusters mentioning "connection timeout"
- Vector DB search finds this file when querying "connection pool" → from the cluster descriptions

### 4.3 METRICS — Statistical Profile

```python
def build_metrics_statistical_profile(
    metrics_content: str, config: Config
) -> ExtractionResult:
    """
    Build statistical profile with anomaly detection.

    Zero-LLM: Pure statistical analysis.
    Compression: 167:1 (5MB → 30KB)
    Time: ~0.3s
    """

    metrics_df = parse_metrics(metrics_content)

    anomalies = []
    profiles = []

    for column in metrics_df.select_dtypes(include=[np.number]).columns:
        values = metrics_df[column].dropna()
        if len(values) < 10:
            continue

        mean = values.mean()
        std = values.std()

        profile = {
            "metric": column,
            "mean": mean,
            "std": std,
            "min": values.min(),
            "max": values.max(),
            "count": len(values),
        }
        profiles.append(profile)

        if std == 0:
            continue

        z_scores = (values - mean) / std
        anomaly_indices = np.where(np.abs(z_scores) > config.z_score_threshold)[0]

        for idx in anomaly_indices:
            anomalies.append({
                "metric": column,
                "timestamp": metrics_df.index[idx] if hasattr(metrics_df, 'index') else idx,
                "value": values.iloc[idx],
                "z_score": z_scores.iloc[idx],
                "mean": mean,
                "anomaly_type": "spike" if z_scores.iloc[idx] > 0 else "drop",
            })

    # Build summary
    summary_lines = [
        f"=== METRICS STATISTICAL PROFILE ===",
        f"Data points: {len(metrics_df)} across {len(profiles)} metrics",
        f"Anomalies detected: {len(anomalies)} (z-score > {config.z_score_threshold})",
        "",
        "=== METRIC PROFILES ===",
    ]

    for p in profiles:
        summary_lines.append(
            f"  {p['metric']}: mean={p['mean']:.2f}, "
            f"std={p['std']:.2f}, range=[{p['min']:.2f}, {p['max']:.2f}]"
        )

    summary_lines.append("\n=== ANOMALIES ===")
    for a in anomalies[:20]:
        summary_lines.append(
            f"  {a['metric']}: {a['value']:.2f} ({a['anomaly_type']}, "
            f"z={a['z_score']:.2f}) at {a['timestamp']}"
        )

    return ExtractionResult(
        method="statistical_profile",
        content='\n'.join(summary_lines),
        metadata={
            "total_anomalies": len(anomalies),
            "anomalies": anomalies[:50],
            "metrics_analyzed": [p['metric'] for p in profiles],
            "profiles": profiles,
        },
    )
```

### 4.4 CONFIGURATION — Parse & Sanitize

```python
def parse_and_sanitize_config(
    config_content: str, filename: str
) -> ExtractionResult:
    """
    Parse config structure and redact secrets. No compression — every key matters.

    LLM Calls: 0
    Time: ~0.2s

    Error handling:
    - Parse failure (malformed YAML/JSON/TOML/INI) → fallback to TEXT extraction
    - Re-serialization failure → return raw content with secrets regex-redacted
    """

    # Detect format from extension
    ext = Path(filename).suffix.lower()
    format_map = {
        '.yaml': 'yaml', '.yml': 'yaml',
        '.json': 'json',
        '.toml': 'toml',
        '.ini': 'ini', '.conf': 'ini', '.cfg': 'ini', '.properties': 'ini',
    }
    format_type = format_map.get(ext, 'ini')

    # Attempt structured parse
    try:
        if format_type == "yaml":
            parsed = yaml.safe_load(config_content)
        elif format_type == "json":
            parsed = json.loads(config_content)
        elif format_type == "toml":
            parsed = toml.loads(config_content)
        else:
            parsed = parse_ini(config_content)
    except (yaml.YAMLError, json.JSONDecodeError, toml.TomlDecodeError,
            configparser.Error, Exception) as e:
        # FALLBACK: Parsing failed — treat as TEXT with regex-based secret redaction
        logger.warning(
            f"Config parse failed for {filename} ({format_type}): {e}. "
            f"Falling back to TEXT extraction with regex secret redaction."
        )
        redacted_content = regex_redact_secrets(config_content)
        return ExtractionResult(
            method="structure_extraction",  # Fallback to TEXT method
            content=redacted_content,
            metadata={
                "format": format_type,
                "parse_failed": True,
                "parse_error": str(e)[:200],
                "secrets_redacted": -1,  # Unknown (regex-based, not structural)
            },
        )

    # Guard: parsed result must be a dict or list
    if parsed is None:
        # Empty file or null document (e.g., empty YAML)
        return ExtractionResult(
            method="parse_and_sanitize",
            content="(empty configuration file)",
            metadata={"format": format_type, "secrets_redacted": 0, "keys_count": 0},
        )

    # Redact secrets (recursive, handles nested dicts/lists)
    secret_patterns = {
        'api_key': r'(api[_-]?key|apikey)',
        'password': r'(password|passwd|pwd)',
        'secret': r'(secret|token)',
        'private_key': r'(private[_-]?key|privatekey)',
        'connection_string': r'(connection[_-]?string|conn[_-]?str|dsn|database[_-]?url)',
        'certificate': r'(certificate|cert[_-]?key|ssl[_-]?key)',
    }

    secrets_found = 0

    def redact_secrets(obj, path=""):
        nonlocal secrets_found
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_lower = key.lower()
                is_secret = any(
                    re.search(p, key_lower) for p in secret_patterns.values()
                )
                if is_secret and isinstance(value, str):
                    obj[key] = "***REDACTED***"
                    secrets_found += 1
                elif isinstance(value, (dict, list)):
                    redact_secrets(value, f"{path}.{key}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                redact_secrets(item, f"{path}[{i}]")

    redact_secrets(parsed)

    # Re-serialize
    if format_type == "yaml":
        sanitized = yaml.dump(parsed, default_flow_style=False)
    elif format_type == "json":
        sanitized = json.dumps(parsed, indent=2)
    elif format_type == "toml":
        sanitized = toml.dumps(parsed)
    else:
        sanitized = format_ini(parsed)

    return ExtractionResult(
        method="parse_and_sanitize",
        content=sanitized,
        metadata={
            "format": format_type,
            "secrets_redacted": secrets_found,
            "keys_count": count_keys(parsed),
        },
    )
```

### 4.5 CODE — AST Extraction

```python
# Supported languages for AST parsing vs regex fallback
LANGUAGE_EXTENSIONS = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
    '.java': 'java', '.go': 'go', '.rb': 'ruby',
    '.cpp': 'cpp', '.c': 'c', '.rs': 'rust',
    '.sh': 'shell', '.bash': 'shell',
}

# Regex patterns for function signature extraction (non-Python languages)
FUNCTION_SIGNATURE_PATTERNS = {
    'javascript': r'^(?:export\s+)?(?:async\s+)?function\s+\w+|(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\(',
    'typescript': r'^(?:export\s+)?(?:async\s+)?function\s+\w+|(?:const|let|var)\s+\w+\s*[:=]',
    'java':       r'^\s*(?:public|private|protected)?\s*(?:static)?\s*\w+\s+\w+\s*\(',
    'go':         r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?\w+\s*\(',
    'ruby':       r'^\s*def\s+\w+',
    'rust':       r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+',
    'c':          r'^\w[\w\s\*]+\s+\w+\s*\([^)]*\)\s*\{',
    'cpp':        r'^\w[\w\s\*:]+\s+\w+\s*\([^)]*\)\s*(?:const)?\s*\{',
    'shell':      r'^\s*(?:\w+\s*\(\)|function\s+\w+)',
}


def extract_code_ast(code_content: str, filename: str) -> ExtractionResult:
    """
    Extract code structure via AST parsing. No LLM.

    Compression: 50:1 for large files → key functions only
    Time: ~0.5s

    Error handling:
    - SyntaxError (Python AST) → fallback to regex-based function signature extraction
    - Language not recognized → fallback to TEXT extraction
    - Regex extraction finds nothing → return raw preview (first+last 200 lines)
    """

    ext = Path(filename).suffix.lower()
    language = LANGUAGE_EXTENSIONS.get(ext)

    if not language:
        # Unknown language — fallback to TEXT extraction
        logger.info(f"Unknown code language for {filename}, falling back to TEXT extraction")
        return extract_text_structure(code_content)

    definitions = []

    if language == "python":
        try:
            import ast
            tree = ast.parse(code_content)
            imports = [
                ast.get_source_segment(code_content, node)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            definitions = [
                ast.get_source_segment(code_content, node)
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            ]
            extracted = imports + definitions

            return ExtractionResult(
                method="ast_extraction",
                content='\n'.join(str(e) for e in extracted if e),
                metadata={
                    "language": language,
                    "functions_extracted": len(definitions),
                    "imports_extracted": len(imports),
                    "ast_parse": "success",
                },
            )
        except SyntaxError as e:
            # Python AST parse failed (syntax error in source) — fall through to regex
            logger.warning(
                f"Python AST parse failed for {filename}: {e}. "
                f"Falling back to regex-based extraction."
            )

    # Non-Python languages OR Python AST fallback: regex-based extraction
    lines = code_content.split('\n')

    # Small files: return entire content (no compression needed)
    if len(lines) < 500:
        return ExtractionResult(
            method="ast_extraction",
            content=code_content,
            metadata={
                "language": language,
                "functions_extracted": 0,
                "full_content": True,
            },
        )

    # Large files: extract function signatures via regex
    pattern = FUNCTION_SIGNATURE_PATTERNS.get(language)
    if pattern:
        signatures = []
        for i, line in enumerate(lines):
            if re.match(pattern, line):
                # Capture signature + a few lines of context
                end = min(i + 3, len(lines))
                signatures.append(f"Line {i+1}: {' '.join(lines[i:end])}")

        if signatures:
            header = f"=== CODE STRUCTURE INDEX ({language}) ===\n"
            header += f"Total lines: {len(lines)}\n"
            header += f"Functions/definitions found: {len(signatures)}\n\n"
            content = header + '\n'.join(signatures)

            return ExtractionResult(
                method="ast_extraction",
                content=content,
                metadata={
                    "language": language,
                    "functions_extracted": len(signatures),
                    "extraction_strategy": "regex_signatures",
                },
            )

    # Last resort: preview (first+last 200 lines)
    preview = lines[:200] + ["\n--- [CONTENT TRUNCATED] ---\n"] + lines[-200:]
    return ExtractionResult(
        method="ast_extraction",
        content='\n'.join(preview),
        metadata={
            "language": language,
            "functions_extracted": 0,
            "extraction_strategy": "preview_fallback",
        },
    )
```

### 4.6 TEXT — Structure Extraction

Mechanical structure extraction only (zero LLM). This is also the **universal fallback** — when any other extractor fails (CONFIG parse error, CODE AST failure with no regex matches), the pipeline falls back to TEXT extraction.

```python
def extract_text_structure(text_content: str) -> ExtractionResult:
    """
    Extract document structure without LLM.

    Strategy:
    1. Identify headings and sections
    2. Extract key sentences (first sentence of each paragraph, max 30)
    3. Count words, lines, sections
    4. Return first/last 100 lines as preview

    This extractor is also the universal fallback for other extraction
    failures (CONFIG parse error, CODE AST failure). It always succeeds
    because it operates on raw text with no parsing assumptions.

    LLM Calls: 0
    Time: ~0.3s
    """

    lines = text_content.split('\n')

    # Extract headings (markdown, rst, or ALL-CAPS lines)
    headings = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^#{1,6}\s+\w+', stripped):  # Markdown
            headings.append((i, stripped))
        elif re.match(r'^[A-Z][A-Z\s]{5,}$', stripped):  # ALL CAPS
            headings.append((i, stripped))
        elif i + 1 < len(lines) and re.match(r'^[=\-]{3,}$', lines[i+1].strip()):
            headings.append((i, stripped))  # RST underline

    # Extract first sentence of each paragraph (max 30)
    paragraphs = text_content.split('\n\n')
    key_sentences = []
    for para in paragraphs[:50]:
        para = para.strip()
        if para and not para.startswith('#'):
            first_sentence = re.split(r'[.!?]', para)[0]
            if 20 < len(first_sentence) < 500:
                key_sentences.append(first_sentence.strip())
        if len(key_sentences) >= 30:
            break

    # Preview: first + last 100 lines
    if len(lines) > 200:
        preview = lines[:100] + ["\n--- [CONTENT TRUNCATED] ---\n"] + lines[-100:]
    else:
        preview = lines

    # Build structural index
    index_parts = [
        f"=== TEXT STRUCTURE INDEX ===",
        f"Total lines: {len(lines)}",
        f"Estimated words: {len(text_content.split())}",
        f"Sections: {len(headings)}",
        "",
    ]

    if headings:
        index_parts.append("=== SECTIONS ===")
        for line_num, heading in headings:
            index_parts.append(f"  Line {line_num}: {heading}")

    if key_sentences:
        index_parts.append("\n=== KEY SENTENCES ===")
        for sentence in key_sentences[:30]:
            index_parts.append(f"  - {sentence}")

    index_parts.append("\n=== PREVIEW ===")
    index_parts.extend(preview)

    return ExtractionResult(
        method="structure_extraction",
        content='\n'.join(index_parts),
        metadata={
            "total_lines": len(lines),
            "word_count": len(text_content.split()),
            "sections": len(headings),
            "headings": [(h[0], h[1]) for h in headings],
        },
    )
```

### 4.7 IMAGE — Metadata Extraction

Metadata only at Tier 1. Vision analysis (screenshot OCR, diagram interpretation) is handled by Tier 2 via a multimodal LLM.

```python
def extract_image_metadata(
    image_bytes: bytes, filename: str
) -> ExtractionResult:
    """
    Extract image metadata without LLM or vision model.

    At Tier 1: format, dimensions, color mode, EXIF data, and file size.
    The agent can reference this evidence and trigger Tier 2 for vision
    analysis when needed (e.g., "describe the error in this screenshot").

    Error handling:
    - Corrupted/unreadable image → metadata from filename and file size only
    - EXIF extraction failure → silently skip (EXIF is optional)

    LLM Calls: 0
    Time: ~0.1s
    """

    from PIL import Image
    from PIL.ExifTags import TAGS
    import io

    metadata = {
        "filename": filename,
        "file_size_bytes": len(image_bytes),
        "format": None,
        "width": None,
        "height": None,
        "color_mode": None,
        "exif": {},
    }

    try:
        img = Image.open(io.BytesIO(image_bytes))
        metadata["format"] = img.format
        metadata["width"] = img.width
        metadata["height"] = img.height
        metadata["color_mode"] = img.mode  # RGB, RGBA, L (grayscale), etc.

        # Extract EXIF if available (best-effort, not critical)
        try:
            exif_data = img.getexif()
            if exif_data:
                for tag_id, value in exif_data.items():
                    tag_name = TAGS.get(tag_id, str(tag_id))
                    if isinstance(value, (str, int, float)):
                        metadata["exif"][tag_name] = value
        except Exception:
            pass  # EXIF extraction is optional

    except Exception as e:
        # Corrupted image — store what we can from filename
        logger.warning(f"Image parse failed for {filename}: {e}")
        ext = Path(filename).suffix.lower()
        metadata["format"] = ext.lstrip('.')

    # Build summary
    dims = f"{metadata['width']}x{metadata['height']}" if metadata['width'] else "unknown dimensions"
    summary = (
        f"Image: {filename} ({metadata['format']}, {dims}, "
        f"{metadata['file_size_bytes']} bytes). "
        f"Vision analysis available via Tier 2 deep analysis."
    )

    return ExtractionResult(
        method="metadata_extraction",
        content=summary,
        metadata=metadata,
    )
```

### 4.8 Sanitization

Sanitization runs after extraction, before storage. Regex-based PII/secret redaction, configurable per provider.

```python
def sanitize_content(
    content: str,
    config: SanitizationConfig,
    provider: str,
) -> SanitizationResult:
    """
    Redact PII and secrets. Configurable: skip for local LLM providers.

    LLM Calls: 0
    Time: ~0.1s
    """

    if provider == "LOCAL" and not config.force_sanitization:
        return SanitizationResult(content=content, redactions_made=0, skipped=True)

    redacted = content
    redactions = []

    # Email addresses
    if config.redact_emails:
        redacted, count = re.subn(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            '***EMAIL_REDACTED***', redacted
        )
        if count: redactions.append(("email", count))

    # API Keys
    if config.redact_api_keys:
        for pattern in [
            r'(sk-[A-Za-z0-9]{48})',
            r'(AKIA[A-Z0-9]{16})',
            r'(AIza[A-Za-z0-9_-]{35})',
        ]:
            redacted, count = re.subn(pattern, '***API_KEY_REDACTED***', redacted)
            if count: redactions.append(("api_key", count))

    # Passwords in config
    if config.redact_passwords:
        redacted, count = re.subn(
            r'(password|passwd|pwd)\s*[:=]\s*[^\s]+',
            r'\1: ***PASSWORD_REDACTED***', redacted, flags=re.IGNORECASE
        )
        if count: redactions.append(("password", count))

    return SanitizationResult(
        content=redacted,
        redactions_made=sum(c for _, c in redactions),
        redactions=redactions,
        skipped=False,
    )
```

### 4.9 Tier 1 Error Handling & Timeout Enforcement

**Tier 1 must always complete within <2s and must never fail the upload.** If an extractor encounters an error, it falls back rather than propagating the exception.

#### Timeout Enforcement

```python
TIER1_TIMEOUT_SECONDS = 2.0

async def _extract_with_timeout(
    self,
    extractor: Callable,
    *args,
    **kwargs,
) -> ExtractionResult:
    """
    Run a Tier 1 extractor with timeout enforcement.

    If the extractor exceeds TIER1_TIMEOUT_SECONDS, cancel it and
    return a TEXT extraction (preview-only) as fallback.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(extractor, *args, **kwargs),
            timeout=TIER1_TIMEOUT_SECONDS,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(
            f"Tier 1 extraction timed out after {TIER1_TIMEOUT_SECONDS}s "
            f"for {kwargs.get('filename', 'unknown')}. "
            f"Falling back to TEXT preview."
        )
        # Fallback: TEXT extraction (always fast — just string splitting)
        content = args[0] if args else ""
        return extract_text_structure(content)
```

#### Fallback Chain Summary

Every DataType extractor has a defined fallback path. No extractor propagates exceptions to the caller — failures always degrade gracefully.

| DataType | Primary Extractor | Error Condition | Fallback |
|----------|-------------------|-----------------|----------|
| **LOGS** | `build_log_structural_index` | Timeout >2s (very rare for text processing) | TEXT preview (first+last 100 lines) |
| **METRICS** | `build_metrics_statistical_profile` | Parse failure (non-numeric data) | TEXT preview |
| **CONFIGURATION** | `parse_and_sanitize_config` | Parse failure (malformed YAML/JSON/TOML) | TEXT extraction with regex secret redaction |
| **CODE** | `extract_code_ast` | SyntaxError (Python) | Regex function signatures → TEXT preview |
| **CODE** | `extract_code_ast` | Unknown language | TEXT extraction (full) |
| **TEXT** | `extract_text_structure` | *(none — always succeeds)* | N/A (this IS the universal fallback) |
| **IMAGE** | `extract_image_metadata` | Corrupted image (PIL fails) | Metadata from filename + file size |

**Key invariant**: `process_upload()` never raises an extraction error. It always returns a valid `PreprocessingResult`. The only exceptions that propagate to the caller are:
- `FileTooLargeError` — file exceeds size limit (pre-extraction)
- `DuplicateFileError` — content_hash already exists (pre-extraction)

See [Evidence Failure Modes](./evidence-failure-modes.md) for post-extraction failures (storage, LLM, DB).

### 4.10 Packaging & Storage

```python
async def package_preprocessing_result(
    extraction_result: ExtractionResult,
    sanitization_result: SanitizationResult,
    file_info: FileInfo,
    case_id: str,
    data_type: DataType,
    content_hash: str,
) -> PreprocessingResult:
    """
    Package extraction results and store raw file.

    Steps:
    1. Generate concise summary (<500 chars) from structural index
    2. Store raw file (local filesystem or S3)
    3. Format PreprocessingResult
    """

    # Generate summary from structural index (no LLM — truncation)
    full_extraction = sanitization_result.content
    summary = generate_concise_summary(full_extraction, max_length=500)

    # Store raw file (NOT the extraction — the original file)
    content_ref = await storage_service.store_raw_file(
        content=file_info.raw_content,
        case_id=case_id,
        filename=file_info.filename,
        content_type=file_info.mime_type,
    )

    return PreprocessingResult(
        temp_id=generate_temp_id(),
        data_type=data_type,
        summary=summary,
        structural_index=full_extraction,
        content_ref=content_ref,
        content_size_bytes=len(file_info.raw_content),
        content_type=file_info.mime_type,
        extraction_method=extraction_result.method,
        compression_ratio=len(full_extraction) / max(len(file_info.raw_content), 1),
        extraction_metadata=extraction_result.metadata,
        content_hash=content_hash,
        sanitization_applied=not sanitization_result.skipped,
        redactions_count=sanitization_result.redactions_made,
        processing_time_ms=file_info.processing_time_ms,
    )

def generate_concise_summary(text: str, max_length: int = 500) -> str:
    """Generate concise summary without LLM — take beginning and end."""
    if len(text) <= max_length:
        return text
    half = max_length // 2
    return f"{text[:half]}... [truncated] ...{text[-half:]}"
```

---

## 5. Vector DB Storage

**Purpose**: Store the Tier 1 structural index in ChromaDB for semantic search. This enables the agent to find relevant evidence across all uploaded files when answering questions or evaluating hypotheses.

**Trigger**: After Tier 1 completes and Evidence object is created (async background task).

Stores the full structural index (error clusters, timeline, state transitions) for comprehensive semantic search.

### 5.1 What Gets Stored

The structural index from Tier 1 — not the raw content. Examples:
- 10MB log file → Structural index (error clusters, timeline) → ~50KB → ~100 chunks
- 5MB metrics CSV → Statistical profile (anomalies, distributions) → ~30KB → ~60 chunks
- 5KB config → Full parsed+sanitized config → 5KB → ~10 chunks

This means a vector search for "connection timeout" will match a log file's error cluster description even if the raw crime scene window doesn't contain that exact phrase.

### 5.2 Chunking Strategy

The structural index must be split into chunks before embedding and storage. The chunking strategy is optimized for the structured nature of Tier 1 output (section headers, clusters, profiles) rather than generic text.

#### Chunk Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| **Chunk size** | 500 tokens (~2000 chars) | Balances retrieval granularity with embedding quality. Too small (100 tokens) loses context; too large (2000 tokens) dilutes relevance. |
| **Overlap** | 50 tokens (~200 chars) | Preserves context across chunk boundaries. Ensures a cluster description split across chunks is still retrievable. |
| **Splitting strategy** | Section-aware | Split on section headers (`=== ... ===`) first, then on paragraph boundaries within sections. Never split mid-line. |

#### Section-Aware Splitting

Tier 1 structural indexes have a consistent section format (e.g., `=== ERROR CLUSTERS ===`, `=== ANOMALIES ===`). The chunker respects these boundaries:

```python
def chunk_structural_index(
    structural_index: str,
    max_chunk_tokens: int = 500,
    overlap_tokens: int = 50,
) -> List[Chunk]:
    """
    Split a Tier 1 structural index into chunks for vector DB storage.

    Strategy:
    1. Split on section headers (=== ... ===) into logical sections
    2. If a section fits in one chunk, keep it whole
    3. If a section exceeds max_chunk_tokens, split on paragraph
       boundaries (\n\n) with overlap
    4. Never split mid-line

    Each chunk carries metadata for retrieval context.
    """

    sections = re.split(r'(===\s+.+?\s+===)', structural_index)

    chunks = []
    current_section_name = "HEADER"

    for part in sections:
        part = part.strip()
        if not part:
            continue

        # Track current section name for metadata
        if re.match(r'===\s+.+?\s+===', part):
            current_section_name = part.strip('= ').strip()
            continue

        # If section fits in one chunk, emit as-is
        if estimate_tokens(part) <= max_chunk_tokens:
            chunks.append(Chunk(
                text=part,
                metadata={"section": current_section_name},
            ))
            continue

        # Section too large — split on paragraph boundaries with overlap
        paragraphs = part.split('\n\n')
        buffer = ""

        for para in paragraphs:
            if estimate_tokens(buffer + para) > max_chunk_tokens and buffer:
                chunks.append(Chunk(
                    text=buffer.strip(),
                    metadata={"section": current_section_name},
                ))
                # Overlap: keep the last ~overlap_tokens of buffer
                overlap_text = get_last_n_tokens(buffer, overlap_tokens)
                buffer = overlap_text + "\n\n" + para
            else:
                buffer = buffer + "\n\n" + para if buffer else para

        if buffer.strip():
            chunks.append(Chunk(
                text=buffer.strip(),
                metadata={"section": current_section_name},
            ))

    return chunks
```

#### Metadata Per Chunk

Each chunk stored in ChromaDB carries metadata for filtering and context:

```python
{
    "evidence_id": "ev_abc123",        # Link back to evidence record
    "case_id": "case_456",             # For case-scoped queries
    "data_type": "logs",               # DataType enum value
    "section": "ERROR CLUSTERS",       # Section within structural index
    "chunk_index": 3,                  # Position within document
    "total_chunks": 12,                # Total chunks for this evidence
    "upload_timestamp": "2025-11-01T14:00:00Z",
}
```

This enables filtered queries like "search only error cluster chunks" or "search only anomaly sections across all evidence in this case."

### 5.3 Implementation

```python
async def store_in_vector_db_background(
    case_id: str,
    evidence_id: str,
    structural_index: str,
    data_type: DataType,
    metadata: Dict[str, Any],
    case_vector_store: CaseVectorStore,
):
    """
    Background task: Chunk and store structural index in ChromaDB.

    User has already received response. This doesn't block upload.
    Silent failure — doesn't affect user experience.
    """

    try:
        # 1. Chunk the structural index
        chunks = chunk_structural_index(
            structural_index,
            max_chunk_tokens=500,
            overlap_tokens=50,
        )

        # 2. Store each chunk with metadata
        documents = []
        for i, chunk in enumerate(chunks):
            documents.append({
                'id': f"{evidence_id}_chunk_{i}",
                'content': chunk.text,
                'metadata': {
                    'evidence_id': evidence_id,
                    'case_id': case_id,
                    'data_type': data_type.value,
                    'section': chunk.metadata.get('section', ''),
                    'chunk_index': i,
                    'total_chunks': len(chunks),
                    'upload_timestamp': datetime.now(timezone.utc).isoformat(),
                    **{k: v for k, v in metadata.items()
                       if isinstance(v, (str, int, float, bool))},
                }
            })

        await case_vector_store.add_documents(
            case_id=case_id,
            documents=documents,
        )
        logger.info(
            f"Structural index stored in vector DB: {evidence_id} "
            f"({len(chunks)} chunks)"
        )
    except Exception as e:
        logger.error(
            f"Failed to store in vector DB for {evidence_id}: {e}. "
            f"Evidence is still available via the Evidence record, "
            f"but semantic search will not find this file."
        )
        # Silent failure — evidence is still available via Evidence object.
        # The agent can still use the Tier 1 structural_index passed in-memory
        # during the upload turn. Only future semantic searches are affected.
```

### 5.3 Retrieval

```python
async def retrieve_relevant_context(
    case_id: str,
    query: str,
    top_k: int = 5,
) -> List[RetrievalResult]:
    """
    Semantic search across case evidence structural indexes.

    Called when:
    - User asks a question about evidence
    - Agent needs to find which files are relevant to a hypothesis
    - Agent needs more context than Evidence.summary provides
    """

    results = await case_vector_store.query(
        case_id=case_id,
        query_texts=[query],
        n_results=top_k,
    )

    return [
        RetrievalResult(
            evidence_id=meta["evidence_id"],
            chunk_text=doc,
            similarity=distance,
            metadata=meta,
        )
        for doc, meta, distance in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )
    ]
```

### 5.4 Index Versioning

When Tier 1 extraction logic changes (e.g., new error cluster heuristics, updated anomaly detection thresholds, or additional section types), previously stored structural indexes become stale relative to the new logic.

**Strategy: Manual/Triggered Re-Indexing**

Structural indexes are **not automatically re-indexed** when extraction logic changes. Re-indexing is an explicit, operator-triggered action.

**Rationale:**
- Tier 1 changes are infrequent (tied to release cycles, not runtime)
- Automatic re-indexing on deploy could overload the Vector DB during startup
- Most extraction changes are additive (new sections) rather than corrective, so old indexes remain usable

**Re-indexing process:**
1. Operator runs a re-indexing job (CLI or admin API) targeting a specific case, a date range, or all evidence
2. For each evidence record, the job retrieves the raw file via `content_ref`, reruns Tier 1 extraction, and replaces the Vector DB chunks
3. Old chunks are deleted by `evidence_id` prefix before new chunks are inserted (atomic per-evidence swap)

**Metadata tracking:**
Each chunk stored in the Vector DB includes an `extraction_version` field (e.g., `"v3.1"`) derived from the Tier 1 extractor version. This enables:
- Querying which evidence was indexed with an outdated extractor
- Scoping re-indexing jobs to only stale records
- Audit trail of extraction lineage

```python
# Added to chunk metadata (Section 5.2)
{
    ...
    "extraction_version": "v3.1",  # Tier 1 extractor version
}
```

---

## 6. Tier 2: Deep Analysis Service

### 6.1 Purpose, Responsibility Boundary, and Invocation Logic

Tier 2 provides LLM-powered deep analysis of raw data. It runs **on-demand**, not at upload time.

#### Responsibility Boundary

**Tier 2 is NOT called by the preprocessing pipeline.** Preprocessing (Tier 0+1) runs synchronously at upload time and returns `PreprocessingResult`. Tier 2 is invoked later by the **investigation agent** during conversation turns, via an agent tool.

```
PREPROCESSING (this service):
  process_upload() → PreprocessingResult     ← Tier 0+1 only

INVESTIGATION ENGINE (separate service, in core/investigation/):
  agent tool "deep_analyze_file" → Tier 2    ← Calls ITier2AnalysisService
```

The agent accesses Tier 2 through a tool registered in `modules/agent/tools/`. The tool wraps `ITier2AnalysisService.analyze()` and is available to the LLM as a function it can call during `process_turn()`.

#### When Tier 2 Is Invoked (Agent Decision Logic)

The investigation agent decides to call Tier 2 based on its reasoning about the current query and available evidence. There is no automatic trigger — the agent uses its judgment, guided by the system prompt.

**Agent's decision criteria (encoded in system prompt)**:
1. **Tier 1 index is insufficient**: The structural index (error clusters, statistical profile, etc.) doesn't contain enough detail to answer the user's question. Example: user asks "what's in the stack trace at line 12450?" but the Tier 1 index only has a cluster summary, not the full trace.
2. **Hypothesis validation needs raw data**: The agent has a hypothesis and needs to verify it against actual log lines, metric values, or config entries — not just the summarized index.
3. **User explicitly requests detail**: The user says "show me the actual logs between 14:00 and 14:15" or "can you look at the full config file?"
4. **Image requires vision analysis**: An image was uploaded and the user asks about its visual content. Tier 1 only has metadata (dimensions, format).

**Agent's decision process (per-turn)**:
```
1. Receive user query + case context
2. Check if Tier 1 structural indexes (from evidence summaries
   and vector DB search) can answer the query
3. If YES → respond using Tier 1 data, no Tier 2 needed
4. If NO → call deep_analyze_file tool with:
   - file_ref: content_ref of the relevant evidence
   - query: what the agent needs to know
   - context: case summary, active hypotheses, investigation stage
5. Tier 2 result becomes part of agent's context for the response
```

**What the agent sees** (in its tool definition):
```python
@tool(name="deep_analyze_file")
async def deep_analyze_file(
    evidence_id: str,
    query: str,
) -> str:
    """
    Perform deep analysis on a previously uploaded file.

    Use this tool when:
    - The structural index (evidence summary) doesn't answer the question
    - You need specific lines, values, or sections from the raw file
    - The user asks about visual content in an image

    Do NOT use this tool when:
    - The evidence summary already contains the answer
    - The question is about investigation strategy, not file content
    """
    evidence = await evidence_repo.get(evidence_id)
    result = await tier2_service.analyze(
        file_ref=evidence.content_ref,
        query=query,
        context=AnalysisContext(
            case_id=case.case_id,
            case_summary=case.summary,
            active_hypotheses=[h.description for h in case.active_hypotheses],
            investigation_stage=case.current_stage.value,
        ),
        data_type=evidence.data_type,
    )
    return result.answer + "\n\nExcerpts:\n" + "\n".join(
        f"Lines {e.line_start}-{e.line_end}: {e.content}" for e in result.excerpts
    )
```

#### Tier 2 Availability

If `TIER2_BACKEND=disabled` (default), the agent tool is not registered. The agent works exclusively from Tier 1 structural indexes and cannot request deeper analysis. This is sufficient for most investigations — Tier 1 captures error clusters, anomalies, config structure, and code signatures.

#### Example End-to-End

```
Turn 1: User uploads 10 files → all get Tier 0 + Tier 1 (free)

Turn 2: User asks "what's causing the connection timeouts?"
  → Agent builds prompt with all 10 evidence summaries
  → Agent searches vector DB → finds 2 relevant log files
  → Agent reasons: "the structural index shows 201 ConnectionTimeout
    errors clustered around lines 12400-12550, but I need the actual
    stack traces to identify the root cause"
  → Agent calls deep_analyze_file(evidence_id="ev_abc", query="...")
  → Tier 2 runs on 1 file: extract connection timeout stack traces
  → Agent responds with root cause analysis

Cost: Tier 0+1 on 10 files = $0.00. Tier 2 on 1 file = ~$0.01.
The other 9 files: never deep-processed.
```

### 6.2 Service Interface Contract

```python
class ITier2AnalysisService(ABC):
    """
    Interface for Tier 2 deep analysis.

    Implementations:
    - ExternalTier2Client: HTTP call to cloud microservice
    - LocalTier2Service: In-process with local LLM
    - BasicTier2Service: In-process keyword search, no LLM
    """

    @abstractmethod
    async def analyze(
        self,
        file_ref: str,
        query: str,
        context: AnalysisContext,
        data_type: DataType,
    ) -> DeepAnalysisResult:
        """
        Analyze raw data to answer a specific question.

        Args:
            file_ref: Reference to stored raw file (S3 URI, local path, or
                      already-staged file ID for the backend)
            query: What the agent wants to know about this data
            context: Investigation context (case summary, hypotheses, stage)
            data_type: Hint from Tier 0 classification

        Returns:
            DeepAnalysisResult with answer, supporting excerpts, and metadata
        """
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the Tier 2 backend is reachable."""
        ...


class AnalysisContext(BaseModel):
    """Context passed to Tier 2 for better analysis."""
    case_id: str
    case_summary: Optional[str] = None
    active_hypotheses: Optional[List[str]] = None
    investigation_stage: Optional[str] = None


class DeepAnalysisResult(BaseModel):
    """Result from Tier 2 deep analysis."""
    answer: str
    excerpts: List[DataExcerpt] = []
    confidence: float = 0.0
    tokens_used: int = 0
    processing_time_ms: int = 0
    backend_used: str = ""


class DataExcerpt(BaseModel):
    """A relevant section from the raw data."""
    content: str
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    relevance: float = 0.0
```

### 6.3 Pluggable Backends

#### Cloud SaaS: External Microservice

```python
class ExternalTier2Client(ITier2AnalysisService):
    """Calls an external Tier 2 microservice via HTTP."""

    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url
        self.api_key = api_key

    async def analyze(self, file_ref, query, context, data_type):
        # Lazy staging: upload file to backend if not already staged
        staged_file_id = await self._ensure_staged(file_ref)

        response = await self.http_client.post(
            f"{self.base_url}/analyze",
            json={
                "file_id": staged_file_id,
                "query": query,
                "context": context.model_dump(),
                "data_type": data_type.value,
            },
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )

        return DeepAnalysisResult(**response.json())

    async def _ensure_staged(self, file_ref: str) -> str:
        """Lazy staging: upload raw file to Tier 2 backend on first use."""
        if file_ref in self._staged_files:
            return self._staged_files[file_ref]

        raw_content = await self.storage_service.retrieve(file_ref)
        staged_id = await self._upload_to_backend(raw_content)
        self._staged_files[file_ref] = staged_id
        return staged_id
```

The external microservice can be backed by:
- **Gemini file search**: Upload file via Gemini Files API, query with Gemini
- **OpenAI assistants**: Upload file to OpenAI, use file_search tool
- **Custom service**: Any service implementing the `/analyze` HTTP endpoint

#### Local with LLM: In-Process RAG

```python
class LocalTier2Service(ITier2AnalysisService):
    """In-process Tier 2 using local filesystem + local LLM (Ollama/vLLM)."""

    def __init__(self, llm_client, storage_service):
        self.llm_client = llm_client
        self.storage_service = storage_service

    async def analyze(self, file_ref, query, context, data_type):
        # Read raw file from local filesystem (no staging needed)
        raw_content = await self.storage_service.retrieve(file_ref)

        # Use Tier 1 structural index to narrow down relevant sections
        relevant_sections = self._find_relevant_sections(raw_content, query)

        # Send to local LLM
        prompt = self._build_analysis_prompt(query, relevant_sections, context)
        response = await self.llm_client.generate(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )

        return DeepAnalysisResult(
            answer=response.content,
            excerpts=[
                DataExcerpt(content=s["text"], line_start=s["start"], line_end=s["end"])
                for s in relevant_sections
            ],
            backend_used="local_llm",
        )

    def _find_relevant_sections(self, content: str, query: str) -> List[dict]:
        """Keyword/regex search to find sections relevant to the query."""
        lines = content.split('\n')
        keywords = query.lower().split()
        matches = []

        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(kw in line_lower for kw in keywords):
                start = max(0, i - 20)
                end = min(len(lines), i + 20)
                matches.append({
                    "text": '\n'.join(lines[start:end]),
                    "start": start,
                    "end": end,
                })

        # Deduplicate overlapping windows
        return self._merge_overlapping(matches)[:5]
```

#### Local without LLM: Basic Search

```python
class BasicTier2Service(ITier2AnalysisService):
    """In-process keyword search, no LLM. Returns raw excerpts."""

    async def analyze(self, file_ref, query, context, data_type):
        raw_content = await self.storage_service.retrieve(file_ref)

        # Keyword search
        relevant_sections = self._keyword_search(raw_content, query)

        # No LLM — return raw excerpts for the agent to interpret
        answer = f"Found {len(relevant_sections)} matching sections."

        return DeepAnalysisResult(
            answer=answer,
            excerpts=[
                DataExcerpt(content=s["text"], line_start=s["start"], line_end=s["end"])
                for s in relevant_sections
            ],
            backend_used="basic_search",
        )
```

### 6.4 File Staging

**Lazy staging**: Raw files are staged to the Tier 2 backend only when the first deep query arrives. This avoids paying for staging files that are never queried.

- **External backends** (Gemini, OpenAI): File must be uploaded via their API. Staged on first query, cached for subsequent queries.
- **Local backends**: No staging needed — file is already on the local filesystem.

### 6.5 Deployment Models

| Deployment | Tier 2 Backend | Configuration |
|------------|---------------|---------------|
| **Cloud SaaS** | External microservice (Gemini, OpenAI, custom) | `TIER2_BACKEND=external`, `TIER2_URL=https://...` |
| **Local + LLM** | In-process with Ollama/vLLM | `TIER2_BACKEND=local` |
| **Local basic** | In-process keyword search | `TIER2_BACKEND=basic` |
| **Disabled** | No Tier 2, agent uses Tier 1 only | `TIER2_BACKEND=disabled` |

**Upgrade path**: User starts with `basic` → tries `local` with Ollama → moves to `external` for production. No code changes, just configuration.

### 6.6 Configuration

```bash
# Tier 2 Deep Analysis
TIER2_BACKEND=disabled              # external | local | basic | disabled
TIER2_URL=                          # URL for external backend
TIER2_API_KEY=                      # API key for external backend
TIER2_TIMEOUT_SECONDS=30            # Timeout for Tier 2 calls
TIER2_MAX_FILE_SIZE_MB=50           # Max file size for Tier 2 staging
```

---

## 7. Output Formats

### 7.1 Tier 1 Output: PreprocessingResult

```python
class PreprocessingResult(BaseModel):
    """Output from Tier 0 + Tier 1 preprocessing."""

    # Identity
    temp_id: str = Field(description="Temporary ID before Evidence object created")

    # Classification (Tier 0)
    data_type: DataType = Field(description="Classified data type")

    # Structural Index (Tier 1) — Two levels
    summary: str = Field(
        max_length=500,
        description="Concise summary for Evidence.summary (<500 chars)"
    )
    structural_index: str = Field(
        description="Complete structural index (for agent analysis and vector DB)"
    )

    # Raw File Storage
    content_ref: Optional[str] = Field(
        None, description="Reference to stored raw file (for Tier 2 access)"
    )
    content_size_bytes: int = Field(description="Size of raw file")
    content_type: str = Field(description="MIME type")

    # Extraction metadata
    extraction_method: str = Field(
        description="Method: structural_index, statistical_profile, parse_and_sanitize, etc."
    )
    compression_ratio: float = Field(
        ge=0.0, description="Ratio of index size to raw size"
    )
    extraction_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific metadata (error counts, anomaly details, etc.)"
    )

    # Deduplication
    content_hash: str = Field(description="SHA-256 hash of raw file content")

    # Sanitization
    sanitization_applied: bool = Field(default=False)
    redactions_count: int = Field(default=0)

    # Performance
    processing_time_ms: int = Field(description="Total Tier 0+1 time in milliseconds")


class ErrorSummary(BaseModel):
    """Structured insights from log structural index."""
    total_errors: int
    severity_distribution: Dict[str, int]
    first_error_line: int
    last_error_line: int
    error_burst_detected: bool
    unique_error_types: List[str]


class AnomalySummary(BaseModel):
    """Structured insights from metrics statistical profile."""
    total_anomalies: int
    metrics_analyzed: List[str]
    anomaly_types: Dict[str, int]
    most_anomalous_metric: str
    time_range: str


class ConfigSummary(BaseModel):
    """Structured insights from config parsing."""
    format: str
    total_keys: int
    secrets_found: int
    secrets_redacted: bool
    validation_status: str
```

### 7.2 Tier 2 Output: DeepAnalysisResult

```python
class DeepAnalysisResult(BaseModel):
    """Output from Tier 2 deep analysis."""

    answer: str = Field(description="LLM-generated analysis answering the query")
    excerpts: List[DataExcerpt] = Field(
        default_factory=list,
        description="Relevant raw data sections with line numbers"
    )
    confidence: float = Field(default=0.0, description="Analysis confidence 0-1")
    tokens_used: int = Field(default=0)
    processing_time_ms: int = Field(default=0)
    backend_used: str = Field(default="", description="Which Tier 2 backend was used")


class DataExcerpt(BaseModel):
    """A section from the raw data supporting the analysis."""
    content: str
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    relevance: float = 0.0
```

### 7.3 Integration with Evidence Architecture

**PreprocessingResult → Evidence field mapping:**

| PreprocessingResult Field | Evidence Field | Notes |
|--------------------------|----------------|-------|
| `summary` | `summary` | <500 char summary for display |
| `data_type` | `data_type` | Unified DataType enum |
| `content_ref` | `content_ref` | Raw file reference (S3 URI or local path) |
| `content_size_bytes` | `content_size_bytes` | Size of raw file |
| `content_type` | *(not stored)* | MIME type available from content_ref metadata |
| `content_hash` | `content_hash` | SHA-256 for deduplication |
| `extraction_method` | `extraction_method` | Tier 1 method name |
| `structural_index` | *(vector DB only)* | Too large for DB — stored in ChromaDB async |
| `compression_ratio` | *(not stored)* | Preprocessing metric, not needed in evidence |
| `extraction_metadata` | *(not stored)* | Available via vector DB metadata |
| `sanitization_applied` | *(not stored)* | Preprocessing metric, logged but not persisted |
| `redactions_count` | *(not stored)* | Logged for audit trail, not in evidence table |
| `processing_time_ms` | *(not stored)* | Preprocessing metric |

**Fields set by LLM evaluation (not from PreprocessingResult):**

| Evidence Field | Source |
|----------------|--------|
| `category` | LLM classification (SYMPTOM/CAUSAL/RESOLUTION/CONTEXTUAL/REJECTED) |
| `primary_purpose` | LLM-generated description |
| `related_hypotheses` | LLM + system inference |
| `advances_milestones` | System-inferred from category + completed milestones |
| `form` | Input channel: `DOCUMENT` (file upload), `USER_TEXT` (typed text), or `SUBMITTED_DATA` (pasted data) |

```python
# After Tier 1 preprocessing completes:
preprocessing_result = await preprocessing_service.process_upload(...)

# Evidence Architecture uses these fields:
evidence = Evidence(
    # From PreprocessingResult:
    summary=preprocessing_result.summary,                    # <500 chars
    content_ref=preprocessing_result.content_ref,            # Raw file reference
    content_size_bytes=preprocessing_result.content_size_bytes,
    data_type=preprocessing_result.data_type,                # Unified DataType enum
    content_hash=preprocessing_result.content_hash,          # SHA-256 for dedup
    extraction_method=preprocessing_result.extraction_method, # Tier 1 method
    form=EvidenceForm.DOCUMENT,                              # File upload; text uses USER_TEXT or SUBMITTED_DATA
    source_file_id=uploaded_file.file_id,                    # Link to UploadedFile
    # From LLM evaluation:
    category=llm_result.category,                            # Set by LLM
    primary_purpose=llm_result.primary_purpose,              # Set by LLM
)

# The structural index goes to vector DB (async, does NOT block response):
# 1. Agent uses it for initial response generation (passed in-memory)
# 2. Stored in ChromaDB for semantic search across case evidence
# 3. NOT stored in the evidence table (too large — 50KB+ for logs)

# The content_ref enables:
# 1. Tier 2 deep analysis on-demand
# 2. File download by the user
# 3. Re-processing if needed
```

---

## 8. Configuration

```bash
# ============================================================
# FILE INGESTION & VALIDATION (Tier 0)
# ============================================================
MAX_UPLOAD_SIZE_MB=10
ALLOWED_MIME_TYPES=text/plain,text/csv,application/json,application/yaml,image/png,image/jpeg
BLOCKED_EXTENSIONS=.exe,.dll,.zip,.bin
CLASSIFICATION_SAMPLE_SIZE=5000

# ============================================================
# TIER 1: MECHANICAL EXTRACTION
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
# VECTOR DB STORAGE (Post Tier 1)
# ============================================================
CHROMADB_HOST=localhost
CHROMADB_PORT=8000
CHROMADB_COLLECTION_PREFIX=case_

# ============================================================
# RAW FILE STORAGE
# ============================================================
STORAGE_BACKEND=local                   # local | s3
S3_BUCKET_EVIDENCE=faultmaven-evidence
S3_REGION=us-east-1

# ============================================================
# TIER 2: DEEP ANALYSIS SERVICE
# ============================================================
TIER2_BACKEND=disabled                  # external | local | basic | disabled
TIER2_URL=                              # URL for external backend
TIER2_API_KEY=                          # API key for external backend
TIER2_TIMEOUT_SECONDS=30
TIER2_MAX_FILE_SIZE_MB=50

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

## 9. Implementation Guide

### 9.1 Deduplication Scope

Deduplication uses `content_hash` (SHA-256) but the hashing scope differs by input channel:

| Input Channel | What Gets Hashed | Scope | Rationale |
|---------------|-----------------|-------|-----------|
| **File upload** (`/data` endpoint) | Raw file bytes | Per-case | Same file uploaded twice → rejected. Different files are always unique regardless of accompanying message text. |
| **Text message** (`/queries` endpoint) | Entire raw user message | Per-case | Same message resubmitted → rejected. Rephrased message with same data → accepted (intentionally different submission). |

**File uploads** (this document's scope): `content_hash = SHA-256(raw_file_bytes)`. Computed early in `process_upload()`, before classification or extraction. The accompanying chat message is NOT included in the hash — deduplication is based purely on file content.

**Text messages**: `content_hash = SHA-256(raw_message_text)`. Handled by the investigation service, not preprocessing. See [Evidence Failure Modes](./evidence-failure-modes.md) for the rationale on hashing the entire message vs. extracted data portions.

**Key design choice**: Hashing raw content (not extracted content) ensures deduplication is stable even if extraction algorithms change. See the "Content Hash Strategy for Deduplication" section in [Evidence Failure Modes](./evidence-failure-modes.md) for detailed analysis.

### 9.2 Service Architecture

```python
class PreprocessingService:
    """
    Tier 0 + Tier 1 preprocessing orchestrator.

    This service handles file upload preprocessing only. For post-preprocessing
    failures (LLM timeout, DB insert, orphaned files), see:
    - Evidence Failure Modes: ./evidence-failure-modes.md
    - Evidence Flow Architecture: ./evidence-flow-architecture.md

    Exceptions that propagate from this service:
    - FileTooLargeError: File exceeds MAX_UPLOAD_SIZE_MB
    - DuplicateFileError: content_hash already exists for this case

    All other errors (extraction failure, sanitization failure) are handled
    internally via fallback strategies (see Section 4.9).
    """

    async def process_upload(
        self,
        file: UploadFile,
        case_id: str,
        source_metadata: Optional[SourceMetadata] = None,
    ) -> PreprocessingResult:
        """
        Main entry point. Always synchronous, always cheap.

        Returns PreprocessingResult with structural index.
        Never returns a "needs user decision" response —
        Tier 1 always completes without user interaction.

        Guarantees:
        - Completes in <2s (timeout-enforced extraction)
        - Never fails due to extraction errors (fallback chain)
        - Raises only FileTooLargeError or DuplicateFileError
        """

        # Step 0: Read file and validate size
        raw_content = await file.read()

        if len(raw_content) > self.config.max_upload_size:
            raise FileTooLargeError(len(raw_content), self.config.max_upload_size)

        # Step 1: Compute content_hash early for deduplication
        # Hashes raw file bytes (not extracted content). See Section 9.1.
        content_hash = hashlib.sha256(raw_content).hexdigest()

        # Step 2: Check for duplicate (early exit before any extraction work)
        existing = await self.evidence_repo.find_by_content_hash(
            case_id, content_hash
        )
        if existing:
            raise DuplicateFileError(existing.evidence_id, content_hash)

        # Tier 0: Classify (always <100ms, rule-based)
        classification = classify_data_type(
            filename=file.filename,
            content_sample=raw_content[:5000],
            mime_type=file.content_type,
        )

        # Tier 1: Extract structural index (timeout-enforced, with fallback)
        if classification.data_type == DataType.IMAGE:
            extraction = extract_image_metadata(raw_content, file.filename)
        else:
            content_str = raw_content.decode('utf-8', errors='replace')
            extraction = await self._extract_with_timeout(
                self._get_extractor(classification.data_type),
                content_str,
                file.filename,
            )

        # Sanitize (PII/secret redaction)
        sanitization = sanitize_content(
            content=extraction.content,
            config=self.sanitization_config,
            provider=self.llm_provider,
        )

        # Package & store raw file
        result = await package_preprocessing_result(
            extraction_result=extraction,
            sanitization_result=sanitization,
            file_info=FileInfo(
                filename=file.filename,
                mime_type=file.content_type,
                raw_content=raw_content,
                extension=Path(file.filename).suffix,
            ),
            case_id=case_id,
            data_type=classification.data_type,
            content_hash=content_hash,
        )

        return result

    def _get_extractor(self, data_type: DataType) -> Callable:
        """Get the appropriate Tier 1 extractor for a data type."""
        extractors = {
            DataType.LOGS: lambda c, f: build_log_structural_index(c, self.config),
            DataType.METRICS: lambda c, f: build_metrics_statistical_profile(c, self.config),
            DataType.CONFIGURATION: lambda c, f: parse_and_sanitize_config(c, f),
            DataType.CODE: lambda c, f: extract_code_ast(c, f),
            DataType.TEXT: lambda c, f: extract_text_structure(c),
        }
        return extractors.get(data_type, lambda c, f: extract_text_structure(c))

    async def _extract_with_timeout(
        self, extractor: Callable, content: str, filename: str,
    ) -> ExtractionResult:
        """
        Run extractor with 2s timeout. Falls back to TEXT extraction on timeout.
        See Section 4.9 for the full fallback chain.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(extractor, content, filename),
                timeout=TIER1_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Tier 1 timed out for {filename}, falling back to TEXT")
            return extract_text_structure(content)
```

### 9.3 Tier 2 Client Factory

```python
def create_tier2_service(config: Settings) -> Optional[ITier2AnalysisService]:
    """Factory: create Tier 2 service based on configuration."""

    if config.tier2_backend == "disabled":
        return None

    if config.tier2_backend == "external":
        return ExternalTier2Client(
            base_url=config.tier2_url,
            api_key=config.tier2_api_key,
        )

    if config.tier2_backend == "local":
        return LocalTier2Service(
            llm_client=create_local_llm_client(config),
            storage_service=create_storage_service(config),
        )

    if config.tier2_backend == "basic":
        return BasicTier2Service(
            storage_service=create_storage_service(config),
        )

    raise ValueError(f"Unknown TIER2_BACKEND: {config.tier2_backend}")
```

---

## 10. Examples

### Example 1: Log File — Tier 1 Structural Index

```python
# User uploads 5MB application.log

# Tier 0: Classify → LOGS (confidence 0.85)
# Tier 1: Build structural index

preprocessing_result = PreprocessingResult(
    temp_id="temp_abc123",
    data_type=DataType.LOGS,
    summary="Application log: 250K lines, 23 error clusters, 847 total errors. "
            "Highest severity: NullPointerException burst at line 12450.",
    structural_index="""=== LOG STRUCTURAL INDEX ===
Total lines: 250000
Time range: 2025-11-01T14:00:00 to 2025-11-01T15:30:00
Total errors: 847
Severity distribution: {'ERROR': 823, 'FATAL': 2, 'CRITICAL': 22}
Error clusters: 23
State transitions: 3

=== ERROR CLUSTERS ===
Cluster 1: lines 12400-12550 (45 errors, severity=100)
  Time: 14:23:01 to 14:23:45
  Types: NullPointerException, ConnectionTimeout
  Representative: ERROR NullPointerException in auth-service.AuthManager.validate()

Cluster 2: lines 18900-19100 (12 errors, severity=50)
  Time: 14:45:10 to 14:45:22
  Types: OutOfMemoryError
  Representative: ERROR OutOfMemoryError in cache-service.CacheManager.put()
...

=== STATE TRANSITIONS ===
  Line 1: [14:00:00] SERVICE_START - Application started (v2.3.1)
  Line 12350: [14:22:55] DEPLOYMENT - New deployment detected (v2.3.2)
  Line 200000: [15:15:00] SERVICE_RESTART - Application restarted

=== UNIQUE ERROR TYPES ===
  NullPointerException: 423 occurrences
  ConnectionTimeout: 201 occurrences
  OutOfMemoryError: 12 occurrences
...

=== CRIME SCENE (lines 12250-12650) ===
[400 lines of context around highest severity error]
""",
    content_ref="s3://faultmaven/case_123/application_log_abc.log",
    content_size_bytes=5242880,
    content_type="text/plain",
    extraction_method="structural_index",
    compression_ratio=0.005,
    extraction_metadata={
        "total_lines": 250000,
        "total_errors": 847,
        "error_clusters": 23,
        "severity_distribution": {"ERROR": 823, "FATAL": 2, "CRITICAL": 22},
        "unique_error_types": 15,
        "state_transitions": 3,
        "time_range": {"first": "2025-11-01T14:00:00", "last": "2025-11-01T15:30:00"},
        "crime_scene_line": 12450,
    },
    content_hash="a1b2c3d4e5f6...sha256...of_raw_file_content",
    sanitization_applied=True,
    redactions_count=2,
    processing_time_ms=520,
)
```

### Example 2: Later — Tier 2 Deep Analysis

```python
# User asks: "What happened between 14:45 and 14:46?"
# Agent checks structural index → Cluster 2 (OOM errors) is in that range
# Agent triggers Tier 2 for targeted extraction

deep_result = await tier2_service.analyze(
    file_ref="s3://faultmaven/case_123/application_log_abc.log",
    query="Extract all log entries between 14:45:00 and 14:46:00 with full context",
    context=AnalysisContext(
        case_id="case_123",
        case_summary="Investigating application crashes after v2.3.2 deployment",
        active_hypotheses=["Memory leak in cache service"],
    ),
    data_type=DataType.LOGS,
)

# deep_result.answer:
# "Between 14:45:00 and 14:46:00, the cache-service experienced 12
#  OutOfMemoryError exceptions. The heap usage climbed from 85% to 99%
#  over 22 seconds, suggesting a memory leak triggered by the new
#  CacheManager.put() method introduced in v2.3.2..."
#
# deep_result.excerpts: [relevant log lines with line numbers]
```

### Example 3: Config File — Tier 1 Only

```python
# User uploads database.yaml (5KB) — small enough, no Tier 2 needed

preprocessing_result = PreprocessingResult(
    temp_id="temp_ghi789",
    data_type=DataType.CONFIGURATION,
    summary="Database config (YAML): PostgreSQL connection, pool_size=100, timeout=30s, 3 secrets redacted",
    structural_index="[Full YAML with secrets redacted]",
    content_ref="local://data/case_123/database_yaml_ghi.yaml",
    content_size_bytes=5120,
    content_type="application/yaml",
    extraction_method="parse_and_sanitize",
    compression_ratio=1.0,
    extraction_metadata={
        "format": "yaml",
        "secrets_redacted": 3,
        "keys_count": 42,
    },
    content_hash="f7e8d9c0...sha256...of_raw_yaml_content",
    sanitization_applied=True,
    redactions_count=3,
    processing_time_ms=230,
)
```

---

**Document Version**: 3.2
**Last Updated**: 2026-02-12
**Status**: Design Specification
