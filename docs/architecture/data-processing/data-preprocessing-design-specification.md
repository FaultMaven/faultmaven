# FaultMaven Data Preprocessing Architecture v2.0

## Executive Summary

This document defines the **data preprocessing pipeline** that transforms raw user-uploaded files into structured, high-signal evidence summaries for the investigation system. Preprocessing is the **first stage** in the evidence lifecycle, preparing data for evaluation by the Evidence Architecture.

**Three-Pipeline System**:
1. **Synchronous Pipeline** (user waits): Validate → Classify → Extract → Sanitize → Return
2. **Async Background Pipeline** (fire-and-forget): Chunk → Embed → Store for long-term memory
3. **Integration Flow**: Preprocessed data → Evidence Classification → Evidence object creation → Hypothesis analysis

**Key Principles**:
1. **Fast Path First**: Default to lightweight extraction (solves 80%+ cases)
2. **User Control**: Give users choice for expensive processing
3. **Classify First, Then Process**: Rule-based classification routes to specialized extractors
4. **Maximum Signal, Minimum Noise**: Extract only high-value diagnostic information
5. **Security by Default**: Sanitize PII/secrets before LLM processing (configurable)
6. **Separation of Concerns**: Preprocessing extracts insights; Evidence Architecture evaluates them

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [System Integration](#2-system-integration)
3. [Synchronous Pipeline (Steps 1-4)](#3-synchronous-pipeline-steps-1-4)
4. [Async Background Pipeline (Step 5)](#4-async-background-pipeline-step-5)
5. [User Choice System](#5-user-choice-system)
6. [Data Type Specifications](#6-data-type-specifications)
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
│ DATA PREPROCESSING ARCHITECTURE (THIS DOCUMENT)             │
│ Transforms raw data → structured insights                   │
└─────────────────┬───────────────────────────────────────────┘
                  │ PreprocessingResult
                  ↓
┌─────────────────────────────────────────────────────────────┐
│ EVIDENCE ARCHITECTURE v1.1                                  │
│ Evaluates insights → creates Evidence objects               │
└─────────────────┬───────────────────────────────────────────┘
                  │ Evidence + Hypothesis linkage
                  ↓
┌─────────────────────────────────────────────────────────────┐
│ INVESTIGATION STATE AND CONTROL FRAMEWORK                   │
│ Updates phase, status, working conclusion                   │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Three-Pipeline Design

```
USER UPLOADS FILE (10MB max)
        ↓
┌─────────────────────────────────────────────────────────────┐
│ SYNCHRONOUS PIPELINE (User waits)                           │
│                                                              │
│ Step 1: Validate & Classify (0.1s)                         │
│   - Size check (≤ 10MB)                                    │
│   - Rule-based classification → 6 data types               │
│   - LLM Calls: 0                                           │
│                                                              │
│ Step 2: Type-Specific Extraction (0.5-30s)                 │
│   - Known types: Fast deterministic extraction             │
│   - Unknown <100KB: Auto full summarization                │
│   - Unknown ≥100KB: ASK USER (4-tier choice)               │
│   - LLM Calls: 0-25 (depends on type & user choice)       │
│                                                              │
│ Step 3: Sanitize (0.1s)                                    │
│   - PII redaction (configurable)                           │
│   - Secret scanning                                        │
│   - LLM Calls: 0                                           │
│                                                              │
│ Step 4: Package & Return (0.1s)                            │
│   - Format PreprocessingResult                             │
│   - Store raw artifact → S3                                │
│   - Return structured insights                             │
│   - LLM Calls: 0                                           │
│                                                              │
│ Total Time: 0.5s - 30s (depends on choices)               │
└──────────────────────┬──────────────────────────────────────┘
                       │ PreprocessingResult
                       ↓
┌─────────────────────────────────────────────────────────────┐
│ EVIDENCE ARCHITECTURE (NOT part of preprocessing)          │
│                                                              │
│ - Evidence Classification (6 dimensions)                    │
│ - Evidence object creation                                  │
│ - Hypothesis analysis & linkage                             │
│ - Status updates (PROPOSED → VALIDATED)                     │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ↓
┌─────────────────────────────────────────────────────────────┐
│ ASYNC BACKGROUND PIPELINE (Fire-and-forget)                 │
│                                                              │
│ Step 5: Vector DB Storage (IF user chose caching)          │
│   - Chunk preprocessed output (512 tokens)                 │
│   - Generate embeddings (BGE-M3)                           │
│   - Store in ChromaDB: case_{case_id}                      │
│   - Purpose: Long-term memory for forensic deep dives      │
│   - Time: 2-5s (user doesn't wait)                         │
│   - LLM Calls: 0 (embeddings only)                         │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. System Integration

### 2.1 Preprocessing Boundaries

**Preprocessing DOES**:
- ✅ Extract high-signal summaries from raw data
- ✅ Classify data type (logs, metrics, config, etc.)
- ✅ Apply extraction strategies (Crime Scene, Anomaly Detection, etc.)
- ✅ Sanitize PII and secrets
- ✅ Store raw artifacts in S3
- ✅ Return structured `PreprocessingResult`

**Preprocessing DOES NOT**:
- ❌ Evaluate evidence against hypotheses (Evidence Architecture's job)
- ❌ Update hypothesis status (Evidence Architecture's job)
- ❌ Create timeline events (happens during Evidence evaluation)
- ❌ Calculate confidence scores (Evidence Architecture uses qualitative status)
- ❌ Link evidence to hypotheses (Evidence Architecture's job)

### 2.2 Integration with Evidence Architecture

**Data Flow**:

```python
# 1. Preprocessing (THIS DOCUMENT)
preprocessing_result = await preprocessing_service.process_upload(
    file=uploaded_file,
    case_id=case_id,
)
# Output: PreprocessingResult with summary, insights, S3 reference

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

# 5. Async: Vector DB Storage (THIS DOCUMENT)
background_tasks.add_task(
    store_in_vector_db,
    case_id=case_id,
    preprocessed_content=preprocessing_result.full_extraction,
    evidence_id=evidence.evidence_id,
)
```

### 2.3 Data Type Mapping

**Preprocessing Data Types → Evidence Source Types**:

| Preprocessing Data Type | Evidence Source Type | Evidence Form |
|------------------------|---------------------|---------------|
| `LOGS_AND_ERRORS` | `EvidenceSourceType.LOG_FILE` | `DOCUMENT` |
| `METRICS_AND_PERFORMANCE` | `EvidenceSourceType.METRICS_DATA` | `DOCUMENT` |
| `STRUCTURED_CONFIG` | `EvidenceSourceType.CONFIG_FILE` | `DOCUMENT` |
| `SOURCE_CODE` | `EvidenceSourceType.CODE_REVIEW` | `DOCUMENT` |
| `UNSTRUCTURED_TEXT` | `EvidenceSourceType.USER_OBSERVATION` | `DOCUMENT` |
| `VISUAL_EVIDENCE` | `EvidenceSourceType.SCREENSHOT` | `DOCUMENT` |

**Note**: All file uploads have `form=DOCUMENT`. Text entered via query endpoint has `form=USER_INPUT`.

---

## 3. Synchronous Pipeline (Steps 1-4)

### 3.1 Step 1: Validation & Classification

**Purpose**: Instant, deterministic classification without LLM calls

#### File Size Limit

**Hard Limit**: **10 MB** (10,485,760 bytes)

**Rationale**:
- Aligns with LLM context windows (200K tokens ≈ 0.8 MB after preprocessing)
- Crime Scene Extraction achieves 200:1 compression (10 MB → 50 KB)
- Handles 95% of troubleshooting files:
  - Log file: ~250,000 lines
  - Metrics CSV: ~500,000 rows
  - Config file: ~250,000 lines
  - Stack trace: Unlimited depth
- Prevents timeout/processing issues

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

#### Data Type Classification (6 Types)

**Rule-based, no LLM calls**:

```python
class DataType(str, Enum):
    """Preprocessing data type classification"""
    LOGS_AND_ERRORS = "logs_and_errors"              # Event-based chronological text
    UNSTRUCTURED_TEXT = "unstructured_text"          # Human-written documents
    STRUCTURED_CONFIG = "structured_config"          # System configuration files
    METRICS_AND_PERFORMANCE = "metrics_and_performance"  # Time-series data
    SOURCE_CODE = "source_code"                      # Executable code
    VISUAL_EVIDENCE = "visual_evidence"              # Screenshots, UI captures

def classify_data_type(
    filename: str,
    content_sample: bytes,
    mime_type: str,
) -> DataType:
    """
    Classify file type using pattern matching on first 5KB.
    No LLM calls - pure rule-based.
    
    Returns data type in < 0.1s.
    """
    
    # Check MIME type first
    if mime_type.startswith("image/"):
        return DataType.VISUAL_EVIDENCE
    
    # Sample first 5KB for pattern matching
    sample = content_sample[:5000].decode('utf-8', errors='ignore')
    
    # Check for structured config
    if filename.endswith(('.yaml', '.yml', '.json', '.toml', '.ini', '.conf')):
        return DataType.STRUCTURED_CONFIG
    
    # Check for source code
    if filename.endswith(('.py', '.js', '.java', '.go', '.rb', '.cpp', '.c', '.rs')):
        return DataType.SOURCE_CODE
    
    # Check for logs
    log_patterns = [
        r'\d{4}-\d{2}-\d{2}',  # Date stamps
        r'ERROR|FATAL|CRITICAL|Exception|Traceback',  # Error keywords
        r'\[\w+\]',  # Log levels in brackets
    ]
    if any(re.search(pattern, sample) for pattern in log_patterns):
        return DataType.LOGS_AND_ERRORS
    
    # Check for metrics
    metrics_patterns = [
        r'\d+\.\d+,\d+\.\d+',  # CSV with numeric data
        r'"value":\s*\d+',  # JSON metrics
        r'cpu_usage|memory_usage|latency|throughput',  # Metric names
    ]
    if any(re.search(pattern, sample) for pattern in metrics_patterns):
        return DataType.METRICS_AND_PERFORMANCE
    
    # Default: unstructured text
    return DataType.UNSTRUCTURED_TEXT
```

**Classification Time**: < 0.1s (pattern matching on first 5KB)

---

### 3.2 Step 2: Type-Specific Extraction

**Purpose**: Create concise, high-signal summary for diagnostic agent

#### Processing Matrix

| File Size | Data Type | Default Behavior | Processing Time | LLM Calls | Cached |
|-----------|-----------|------------------|-----------------|-----------|--------|
| Any | LOGS_AND_ERRORS | ✅ Crime Scene Extraction | 0.5s | 0 | ✅ Yes |
| Any | METRICS_AND_PERFORMANCE | ✅ Anomaly Detection | 0.3s | 0 | ✅ Yes |
| Any | STRUCTURED_CONFIG | ✅ Parse & Sanitize | 0.2s | 0 | ✅ Yes |
| Any | SOURCE_CODE | ✅ AST Extraction | 0.5s | 0 | ✅ Yes |
| <100KB | UNSTRUCTURED_TEXT | ✅ Auto Summarization | 2-10s | 1-4 | ✅ Yes |
| ≥100KB | UNSTRUCTURED_TEXT | ⚠️ **ASK USER** | Varies | 0-25 | Depends |
| <5MB | VISUAL_EVIDENCE | ✅ Vision Analysis | 2-5s | 1 | ✅ Yes |
| ≥5MB | VISUAL_EVIDENCE | ⚠️ **ASK USER** | Varies | 0-1 | Depends |

#### Known Types - Auto Processing

**LOGS_AND_ERRORS - Crime Scene Extraction**:

```python
def extract_crime_scene(log_content: str, config: Config) -> ExtractionResult:
    """
    Extract critical error context from logs without LLM.
    
    Strategy:
    1. Parse lines and assign severity scores
    2. Find highest severity error (crime scene)
    3. Extract ±200 lines around crime scene
    4. If multiple high-severity errors, extract first + last
    5. If error burst detected, expand window
    
    Compression: 200:1 (10MB → 50KB)
    LLM Calls: 0
    Time: ~0.5s
    """
    
    lines = log_content.split('\n')
    
    # Score each line by severity
    scored_lines = []
    for i, line in enumerate(lines):
        severity = assign_severity(line, config.severity_keywords)
        if severity > 0:
            scored_lines.append((i, line, severity))
    
    if not scored_lines:
        # No errors found - return tail
        return ExtractionResult(
            method="tail_extraction",
            lines=lines[-config.tail_extraction_lines:],
            metadata={"reason": "no_errors_found"}
        )
    
    # Find highest severity error
    crime_scene_idx = max(scored_lines, key=lambda x: x[2])[0]
    
    # Extract context window
    start = max(0, crime_scene_idx - config.context_lines)
    end = min(len(lines), crime_scene_idx + config.context_lines + 1)
    
    # Check for error burst
    if is_error_burst(scored_lines, crime_scene_idx, config):
        # Expand window for clustered errors
        start = max(0, crime_scene_idx - config.context_lines * 2)
        end = min(len(lines), crime_scene_idx + config.context_lines * 2 + 1)
    
    extracted_lines = lines[start:end]
    
    # If file is long, also extract last error
    if len(lines) > 10000 and crime_scene_idx < len(lines) * 0.5:
        last_error_idx = scored_lines[-1][0]
        last_start = max(0, last_error_idx - 100)
        last_end = min(len(lines), last_error_idx + 100 + 1)
        extracted_lines += ["\n--- LAST ERROR ---\n"] + lines[last_start:last_end]
    
    return ExtractionResult(
        method="crime_scene_extraction",
        lines=extracted_lines,
        metadata={
            "crime_scene_line": crime_scene_idx,
            "severity": scored_lines[max(scored_lines, key=lambda x: x[2])][2],
            "total_errors": len(scored_lines),
            "compression_ratio": len(extracted_lines) / len(lines)
        }
    )

def assign_severity(line: str, keywords: Dict[str, int]) -> int:
    """Assign severity score based on keywords"""
    line_upper = line.upper()
    for keyword, score in keywords.items():
        if keyword in line_upper:
            return score
    return 0
```

**METRICS_AND_PERFORMANCE - Anomaly Detection**:

```python
def detect_anomalies(metrics_content: str, config: Config) -> ExtractionResult:
    """
    Detect statistical anomalies in metrics data without LLM.
    
    Strategy:
    1. Parse metrics (CSV, JSON, Prometheus format)
    2. Calculate z-scores for each metric
    3. Flag values > 3 standard deviations
    4. Generate natural language report
    
    Compression: 167:1 (5MB → 30KB)
    LLM Calls: 0
    Time: ~0.3s
    """
    
    # Parse metrics based on format
    metrics_df = parse_metrics(metrics_content)
    
    anomalies = []
    
    for column in metrics_df.select_dtypes(include=[np.number]).columns:
        values = metrics_df[column].dropna()
        
        if len(values) < 10:
            continue  # Need sufficient data
        
        mean = values.mean()
        std = values.std()
        
        if std == 0:
            continue  # No variance
        
        # Calculate z-scores
        z_scores = (values - mean) / std
        
        # Find anomalies
        anomaly_indices = np.where(np.abs(z_scores) > config.z_score_threshold)[0]
        
        for idx in anomaly_indices:
            anomalies.append({
                "metric": column,
                "timestamp": metrics_df.index[idx] if hasattr(metrics_df, 'index') else idx,
                "value": values.iloc[idx],
                "z_score": z_scores.iloc[idx],
                "mean": mean,
                "std": std,
                "anomaly_type": "spike" if z_scores.iloc[idx] > 0 else "drop"
            })
    
    # Generate natural language summary
    summary_lines = [
        f"Analyzed {len(metrics_df)} data points across {len(metrics_df.columns)} metrics",
        f"Detected {len(anomalies)} anomalies (z-score threshold: {config.z_score_threshold})",
        ""
    ]
    
    for anomaly in anomalies[:10]:  # Top 10
        summary_lines.append(
            f"• {anomaly['metric']}: {anomaly['value']:.2f} "
            f"({anomaly['anomaly_type']}, z={anomaly['z_score']:.2f}) "
            f"at {anomaly['timestamp']}"
        )
    
    return ExtractionResult(
        method="anomaly_detection",
        lines=summary_lines,
        metadata={
            "total_anomalies": len(anomalies),
            "anomalies": anomalies,
            "metrics_analyzed": list(metrics_df.columns)
        }
    )
```

**STRUCTURED_CONFIG - Parse & Sanitize**:

```python
def parse_and_sanitize_config(config_content: str, filename: str) -> ExtractionResult:
    """
    Parse config file and redact secrets without LLM.
    
    Strategy:
    1. Detect format (YAML/JSON/TOML/INI)
    2. Parse structure
    3. Scan for secrets (API keys, passwords)
    4. Redact sensitive values
    5. Return full config (no compression - every line matters)
    
    Compression: 1:1 (no compression)
    LLM Calls: 0
    Time: ~0.2s
    """
    
    # Detect format
    if filename.endswith(('.yaml', '.yml')):
        parsed = yaml.safe_load(config_content)
        format_type = "yaml"
    elif filename.endswith('.json'):
        parsed = json.loads(config_content)
        format_type = "json"
    elif filename.endswith('.toml'):
        parsed = toml.loads(config_content)
        format_type = "toml"
    else:
        # INI or unknown
        parsed = parse_ini(config_content)
        format_type = "ini"
    
    # Scan for secrets
    secret_patterns = {
        'api_key': r'(api[_-]?key|apikey)',
        'password': r'(password|passwd|pwd)',
        'secret': r'(secret|token)',
        'private_key': r'(private[_-]?key|privatekey)',
    }
    
    def redact_secrets(obj, path=""):
        """Recursively redact secret values"""
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = f"{path}.{key}" if path else key
                
                # Check if key matches secret pattern
                key_lower = key.lower()
                is_secret = any(
                    re.search(pattern, key_lower)
                    for pattern in secret_patterns.values()
                )
                
                if is_secret and isinstance(value, str):
                    obj[key] = "***REDACTED***"
                elif isinstance(value, (dict, list)):
                    redact_secrets(value, current_path)
        
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                redact_secrets(item, f"{path}[{i}]")
    
    redact_secrets(parsed)
    
    # Re-serialize to original format
    if format_type == "yaml":
        sanitized_content = yaml.dump(parsed, default_flow_style=False)
    elif format_type == "json":
        sanitized_content = json.dumps(parsed, indent=2)
    elif format_type == "toml":
        sanitized_content = toml.dumps(parsed)
    else:
        sanitized_content = format_ini(parsed)
    
    return ExtractionResult(
        method="parse_and_sanitize",
        lines=sanitized_content.split('\n'),
        metadata={
            "format": format_type,
            "secrets_redacted": True,
            "keys_count": count_keys(parsed)
        }
    )
```

**SOURCE_CODE - AST Extraction**:

```python
def extract_code_ast(code_content: str, filename: str) -> ExtractionResult:
    """
    Extract relevant code using AST parsing without LLM.
    
    Strategy:
    1. Parse code to AST
    2. If error trace provided, extract those functions/classes
    3. Otherwise, extract high-level structure
    4. Include imports/dependencies
    
    Compression: 50:1 (large files → key functions only)
    LLM Calls: 0
    Time: ~0.5s
    """
    
    # Detect language
    language = detect_language(filename)
    
    if language == "python":
        import ast
        tree = ast.parse(code_content)
        
        # Extract all function and class definitions
        extracted_nodes = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                extracted_nodes.append(ast.get_source_segment(code_content, node))
        
        # Include imports
        imports = [
            ast.get_source_segment(code_content, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        
        extracted_lines = imports + extracted_nodes
    
    elif language == "javascript":
        # Use JS parser (e.g., esprima)
        extracted_lines = extract_js_ast(code_content)
    
    else:
        # Fallback: return full code for short files, summarize for long
        lines = code_content.split('\n')
        if len(lines) < 500:
            extracted_lines = lines
        else:
            # Extract function signatures only
            extracted_lines = extract_function_signatures(code_content, language)
    
    return ExtractionResult(
        method="ast_extraction",
        lines=extracted_lines,
        metadata={
            "language": language,
            "functions_extracted": len([l for l in extracted_lines if 'def ' in l or 'function ' in l]),
            "compression_ratio": len(extracted_lines) / len(code_content.split('\n'))
        }
    )
```

**VISUAL_EVIDENCE - Vision Analysis**:

```python
async def analyze_visual_evidence(
    image_bytes: bytes,
    filename: str,
    config: Config,
) -> ExtractionResult:
    """
    Use vision model to describe screenshot/image.
    
    Strategy:
    1. Resize if > 5MB
    2. Call vision model (Claude 3.5 Sonnet)
    3. Extract text description
    
    Compression: N/A (image → text)
    LLM Calls: 1 (vision model)
    Time: ~2-5s
    """
    
    # Resize if needed
    if len(image_bytes) > config.max_vision_size:
        image_bytes = resize_image(image_bytes, max_size=config.max_vision_size)
    
    # Call vision model
    prompt = """
    Analyze this screenshot/image in the context of technical troubleshooting.
    
    Describe:
    1. What system/application is shown?
    2. What is the main content (error message, metric graph, UI state, etc.)?
    3. Any visible errors, warnings, or unusual states?
    4. Relevant technical details (URLs, status codes, metric values, etc.)
    
    Be concise but include all diagnostic details.
    """
    
    response = await llm_client.generate(
        model=config.vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": base64.b64encode(image_bytes).decode()}},
                    {"type": "text", "text": prompt}
                ]
            }
        ],
        max_tokens=1000,
    )
    
    description = response.content[0].text
    
    return ExtractionResult(
        method="vision_analysis",
        lines=[description],
        metadata={
            "model": config.vision_model,
            "image_size_bytes": len(image_bytes),
            "description_length": len(description)
        }
    )
```

**UNSTRUCTURED_TEXT (<100KB) - Auto Summarization**:

```python
async def summarize_text_auto(
    text_content: str,
    config: Config,
) -> ExtractionResult:
    """
    Automatically summarize small text documents.
    
    Strategy:
    1. If < 8K tokens, pass directly to LLM
    2. If 8K-25K tokens, use map-reduce (chunk → summarize → combine)
    
    LLM Calls: 1-4 (depending on size)
    Time: 2-10s
    """
    
    token_count = estimate_tokens(text_content)
    
    if token_count < 8000:
        # Single-shot summarization
        summary = await llm_client.generate(
            model=config.summarization_model,
            messages=[{
                "role": "user",
                "content": f"Summarize this document concisely, focusing on technical details relevant to troubleshooting:\n\n{text_content}"
            }],
            max_tokens=1000,
        )
        
        return ExtractionResult(
            method="single_shot_summary",
            lines=[summary.content[0].text],
            metadata={
                "token_count": token_count,
                "llm_calls": 1
            }
        )
    
    else:
        # Map-reduce summarization
        chunks = chunk_text(text_content, chunk_size=4000, overlap=200)
        
        # Map: Summarize each chunk
        chunk_summaries = await asyncio.gather(*[
            llm_client.generate(
                model=config.summarization_model,
                messages=[{
                    "role": "user",
                    "content": f"Summarize this section:\n\n{chunk}"
                }],
                max_tokens=500,
            )
            for chunk in chunks
        ])
        
        # Reduce: Combine summaries
        combined = "\n\n".join(s.content[0].text for s in chunk_summaries)
        
        final_summary = await llm_client.generate(
            model=config.summarization_model,
            messages=[{
                "role": "user",
                "content": f"Combine these summaries into one coherent summary:\n\n{combined}"
            }],
            max_tokens=1000,
        )
        
        return ExtractionResult(
            method="map_reduce_summary",
            lines=[final_summary.content[0].text],
            metadata={
                "token_count": token_count,
                "chunks": len(chunks),
                "llm_calls": len(chunks) + 1
            }
        )
```

---

### 3.3 Step 3: Sanitization

**Purpose**: Redact PII/secrets before further LLM processing

```python
def sanitize_content(
    content: str,
    config: SanitizationConfig,
    provider: str,
) -> SanitizationResult:
    """
    Redact PII and secrets from content.
    
    Configurable based on provider:
    - External LLM (OpenAI, Anthropic): Redact PII
    - Local LLM: Skip (no privacy risk)
    
    LLM Calls: 0 (regex-based)
    Time: ~0.1s
    """
    
    # Skip sanitization for local providers
    if provider == "LOCAL" and not config.force_sanitization:
        return SanitizationResult(
            content=content,
            redactions_made=0,
            skipped=True,
            reason="local_provider"
        )
    
    redacted = content
    redactions = []
    
    # Email addresses
    if config.redact_emails:
        redacted, count = re.subn(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            '***EMAIL_REDACTED***',
            redacted
        )
        if count > 0:
            redactions.append(("email", count))
    
    # Phone numbers
    if config.redact_phone_numbers:
        redacted, count = re.subn(
            r'\b(\+?1[-.]?)?\(?\d{3}\)?[-.]?\d{3}[-.]?\d{4}\b',
            '***PHONE_REDACTED***',
            redacted
        )
        if count > 0:
            redactions.append(("phone", count))
    
    # API Keys (common patterns)
    if config.redact_api_keys:
        api_key_patterns = [
            r'(sk-[A-Za-z0-9]{48})',  # OpenAI
            r'(AKIA[A-Z0-9]{16})',  # AWS Access Key
            r'(AIza[A-Za-z0-9_-]{35})',  # Google API Key
        ]
        for pattern in api_key_patterns:
            redacted, count = re.subn(pattern, '***API_KEY_REDACTED***', redacted)
            if count > 0:
                redactions.append(("api_key", count))
    
    # Passwords (in config files)
    if config.redact_passwords:
        redacted, count = re.subn(
            r'(password|passwd|pwd)\s*[:=]\s*[^\s]+',
            r'\1: ***PASSWORD_REDACTED***',
            redacted,
            flags=re.IGNORECASE
        )
        if count > 0:
            redactions.append(("password", count))
    
    # IP Addresses (optional - useful for troubleshooting)
    if config.redact_ip_addresses:
        redacted, count = re.subn(
            r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
            '***IP_REDACTED***',
            redacted
        )
        if count > 0:
            redactions.append(("ip_address", count))
    
    return SanitizationResult(
        content=redacted,
        redactions_made=sum(count for _, count in redactions),
        redactions=redactions,
        skipped=False
    )
```

---

### 3.4 Step 4: Package & Return

**Purpose**: Format output and store raw artifact

```python
async def package_preprocessing_result(
    extraction_result: ExtractionResult,
    sanitization_result: SanitizationResult,
    file_info: FileInfo,
    case_id: str,
    data_type: DataType,
) -> PreprocessingResult:
    """
    Package extraction results into PreprocessingResult format.
    
    Steps:
    1. Generate concise summary (<500 chars for Evidence.summary)
    2. Store raw artifact in S3
    3. Format PreprocessingResult
    
    LLM Calls: 0
    Time: ~0.1s
    """
    
    # Generate concise summary from extraction
    full_extraction = '\n'.join(extraction_result.lines)
    summary = generate_concise_summary(full_extraction, max_length=500)
    
    # Store raw artifact in S3
    s3_uri = await s3_client.upload(
        bucket=config.evidence_bucket,
        key=f"{case_id}/{file_info.filename}_{generate_id()}{file_info.extension}",
        content=file_info.raw_content,
        content_type=file_info.mime_type,
    )
    
    # Build insights object
    insights = PreprocessingInsights(
        method=extraction_result.method,
        compression_ratio=extraction_result.metadata.get("compression_ratio", 1.0),
        data_quality=assess_data_quality(extraction_result),
        key_findings=extract_key_findings(extraction_result),
        extraction_metadata=extraction_result.metadata,
    )
    
    return PreprocessingResult(
        temp_id=generate_temp_id(),
        data_type=data_type,
        summary=summary,
        full_extraction=sanitization_result.content,
        content_ref=s3_uri,
        content_size_bytes=len(file_info.raw_content),
        content_type=file_info.mime_type,
        extraction_method=extraction_result.method,
        compression_ratio=extraction_result.metadata.get("compression_ratio", 1.0),
        extraction_metadata=extraction_result.metadata,
        insights=insights,
        sanitization_applied=not sanitization_result.skipped,
        redactions_count=sanitization_result.redactions_made,
        required_user_choice=False,
        chosen_processing_mode=None,
        processing_time_ms=file_info.processing_time_ms,
    )

def generate_concise_summary(text: str, max_length: int = 500) -> str:
    """
    Generate concise summary without LLM.
    
    Strategy:
    - Take first 250 chars + last 250 chars
    - Or truncate to 500 chars if short
    """
    if len(text) <= max_length:
        return text
    
    # Take beginning and end
    half = max_length // 2
    return f"{text[:half]}... [truncated] ...{text[-half:]}"
```

---

## 4. Async Background Pipeline (Step 5)

**Purpose**: Store preprocessed data in ChromaDB for long-term memory without blocking user

**Trigger**: After Evidence object created (not immediately after preprocessing)

**Conditional**: Only runs if user chose a caching option

### 4.1 When to Cache

```python
def should_cache_in_vector_db(preprocessing_result: PreprocessingResult) -> bool:
    """
    Determine if preprocessed data should be cached in vector DB.
    
    Cache if:
    - User chose caching mode (overview_cached, full_cached)
    - File is large and may need deep dives later
    - NOT cached if user chose preview_no_cache
    """
    
    # User explicitly chose no caching
    if preprocessing_result.chosen_processing_mode == "preview_no_cache":
        return False
    
    # User chose caching modes
    if preprocessing_result.chosen_processing_mode in ["overview_cached", "full_cached"]:
        return True
    
    # Auto-processed known types: always cache
    if preprocessing_result.data_type in [
        DataType.LOGS_AND_ERRORS,
        DataType.METRICS_AND_PERFORMANCE,
        DataType.STRUCTURED_CONFIG,
        DataType.SOURCE_CODE,
    ]:
        return True
    
    # Small unstructured text: cache if auto-summarized
    if (preprocessing_result.data_type == DataType.UNSTRUCTURED_TEXT
        and preprocessing_result.content_size_bytes < 100000):
        return True
    
    # Visual evidence: cache description
    if preprocessing_result.data_type == DataType.VISUAL_EVIDENCE:
        return True
    
    return False
```

### 4.2 Implementation Strategy

**Current Implementation (v3.1.0)**:

ChromaDB handles chunking and embedding automatically server-side. No manual chunking required.

```python
async def store_in_vector_db_background(
    case_id: str,
    data_id: str,
    preprocessed_content: str,
    data_type: DataType,
    metadata: Dict[str, Any],
    case_vector_store: CaseVectorStore,
):
    """
    Background task: Store evidence in ChromaDB for forensic queries.

    User has already received response - this doesn't block upload.

    ChromaDB server handles:
    - Chunking (via configured embedding function)
    - Embedding generation (all-MiniLM-L6-v2 default, or custom)
    - Vector indexing

    Args:
        case_id: Case identifier for collection scoping
        data_id: Unique evidence identifier
        preprocessed_content: Preprocessed output (NOT raw content)
        data_type: Evidence data type
        metadata: Evidence metadata (filename, upload time, etc.)
        case_vector_store: Case-scoped vector store (InMemory or ChromaDB)
    """

    try:
        # Store in case-scoped collection
        await case_vector_store.add_documents(
            case_id=case_id,
            documents=[{
                'id': data_id,
                'content': preprocessed_content,
                'metadata': {
                    'data_type': data_type.value,
                    'upload_timestamp': datetime.now(timezone.utc).isoformat(),
                    **metadata
                }
            }]
        )

        logger.info(
            f"Evidence {data_id} stored in vector DB for case {case_id}",
            extra={
                'case_id': case_id,
                'data_id': data_id,
                'content_size': len(preprocessed_content)
            }
        )

    except Exception as e:
        # Silent failure - doesn't affect user experience
        # Evidence is still stored in data storage and available via preprocessed summary
        logger.error(
            f"Failed to store evidence in vector DB: {e}",
            extra={
                'case_id': case_id,
                'data_id': data_id,
                'error': str(e)
            }
        )
```

**Key Design Decisions**:

1. **No Manual Chunking**: ChromaDB server handles chunking automatically based on its embedding function configuration
2. **Storage Agnostic**: Works with both InMemory (instant) and ChromaDB microservice (network call)
3. **Fire-and-Forget**: Background task with silent failure - doesn't block user or fail upload
4. **Preprocessed Content**: Stores preprocessed output (summaries, crime scenes) not raw logs
5. **Case-Scoped**: Each case gets its own ChromaDB collection (`case_{case_id}`)
6. **Embedding Model**: Configured server-side in ChromaDB (default: all-MiniLM-L6-v2)

### 4.3 Vector DB as Long-Term Memory

**Purpose**: "Forensic specialist" deep dives when summary isn't enough

**Use Cases**:
1. **Complex queries**: User asks detailed questions about evidence
2. **Cross-evidence correlation**: Find patterns across multiple files
3. **Historical analysis**: Post-mortem deep dives weeks later
4. **Missing context**: Agent needs more detail than summary provides

**NOT used for**:
- Primary evidence evaluation (that uses summaries in Evidence.summary)
- Hypothesis analysis (uses evidence_links with reasoning)
- Timeline creation (extracted during preprocessing)

**Retrieval Example**:

```python
async def retrieve_relevant_context(
    case_id: str,
    query: str,
    top_k: int = 5,
) -> List[RetrievalResult]:
    """
    Semantic search across case evidence for deep dives.
    
    Called when:
    - User asks specific question about evidence
    - Agent needs more context than Evidence.summary provides
    - Post-mortem analysis requires forensic detail
    """
    
    collection = chroma_client.get_collection(f"case_{case_id}")
    
    results = await collection.query(
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

### 4.4 Performance

**What gets chunked?**: Preprocessed output (NOT raw content)

Examples:
- 10MB log file → Crime scene → 50KB → **~100 chunks**
- 500KB text → Summary → 5KB → **~10 chunks**
- 2KB overview → **~4 chunks**

**Processing time**:
- Chunking: ~0.1s
- Embedding: ~0.05s per chunk (batched)
- ChromaDB: ~0.01s per chunk
- **Total**: 2-5s (background, user doesn't wait)

**Error Handling**: Silent failure with retry (doesn't affect user experience)

---

## 5. User Choice System

**Applies to**: UNSTRUCTURED_TEXT ≥ 100KB, VISUAL_EVIDENCE ≥ 5MB

### 5.1 Four Processing Options

When large unknown file uploaded, pause and ask user:

```json
{
  "status": "awaiting_user_decision",
  "temp_id": "temp_abc123",
  "file_info": {
    "filename": "troubleshooting_guide.txt",
    "size_bytes": 250000,
    "size_human": "244 KB",
    "data_type": "unstructured_text"
  },
  "preview": {
    "first_lines": "...",
    "last_lines": "...",
    "statistics": {
      "total_lines": 5000,
      "estimated_words": 40000,
      "estimated_reading_time": "200 minutes"
    }
  },
  "processing_options": [
    {
      "mode": "preview_no_cache",
      "label": "🔍 Quick Preview (no storage)",
      "description": "See structure and first/last sections. File deleted after 5 minutes.",
      "time_estimate": "< 1s",
      "cost_estimate": "$0.00",
      "will_be_cached": false,
      "best_for": "Browsing, not sure I need this"
    },
    {
      "mode": "overview_cached",
      "label": "📊 Smart Overview (cached)",
      "description": "Extract structure and key topics. Stored for future questions.",
      "time_estimate": "~2s",
      "cost_estimate": "$0.002",
      "will_be_cached": true,
      "best_for": "Might ask questions later"
    },
    {
      "mode": "full_cached",
      "label": "📖 Full Analysis (cached)",
      "description": "Comprehensive summary with full context. Deep dive enabled.",
      "time_estimate": "~30s",
      "cost_estimate": "$0.05",
      "will_be_cached": true,
      "best_for": "Need comprehensive understanding"
    },
    {
      "mode": "targeted_search",
      "label": "🎯 I'll tell you what I need",
      "description": "Search for specific information on-demand. Auto-caches after first query.",
      "time_estimate": "~3s per query",
      "cost_estimate": "$0.003",
      "will_be_cached": "after first search",
      "best_for": "Looking for something specific"
    }
  ]
}
```

### 5.2 Option Details

#### Option 1: Quick Preview (No Cache)

```python
async def process_preview_no_cache(file_content: str) -> PreprocessingResult:
    """
    Extract structure preview without LLM or caching.
    
    LLM Calls: 0
    Time: 0.2s
    Storage: 5-minute temp cache (grace period for upgrade)
    """
    
    lines = file_content.split('\n')
    
    # Extract first and last 100 lines
    preview_lines = lines[:100] + ["\n--- [CONTENT TRUNCATED] ---\n"] + lines[-100:]
    
    # Extract structure (headers, sections)
    structure = extract_structure(file_content)
    
    return PreprocessingResult(
        temp_id=generate_temp_id(),
        data_type=DataType.UNSTRUCTURED_TEXT,
        summary=f"Preview: {len(lines)} lines. Structure: {structure}",
        full_extraction='\n'.join(preview_lines),
        content_ref=None,  # Not stored in S3 yet
        extraction_method="preview_no_cache",
        required_user_choice=True,
        chosen_processing_mode="preview_no_cache",
        grace_period_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
```

**Grace Period**: 5 minutes to upgrade to cached option without re-upload

#### Option 2: Smart Overview (Cached)

```python
async def process_overview_cached(
    file_content: str,
    file_info: FileInfo,
    case_id: str,
) -> PreprocessingResult:
    """
    Generate structural overview with LLM, cache for future queries.
    
    LLM Calls: 1
    Time: 2s
    Storage: S3 + ChromaDB
    """
    
    # Call LLM for structural overview
    overview = await llm_client.generate(
        model=config.summarization_model,
        messages=[{
            "role": "user",
            "content": f"""Extract the structure and key topics from this document.
            
Focus on:
- Main sections and headings
- Key topics discussed
- Document organization

Be concise (max 2KB).

Document:
{file_content[:10000]}  # First 10K chars for context
"""
        }],
        max_tokens=500,
    )
    
    overview_text = overview.content[0].text
    
    # Store raw in S3
    s3_uri = await store_in_s3(file_content, file_info, case_id)
    
    return PreprocessingResult(
        temp_id=generate_temp_id(),
        data_type=DataType.UNSTRUCTURED_TEXT,
        summary=generate_concise_summary(overview_text, 500),
        full_extraction=overview_text,
        content_ref=s3_uri,
        content_size_bytes=len(file_content.encode('utf-8')),
        extraction_method="overview_cached",
        required_user_choice=True,
        chosen_processing_mode="overview_cached",
        # Will be cached in Step 5 (async)
    )
```

#### Option 3: Full Analysis (Cached)

```python
async def process_full_cached(
    file_content: str,
    file_info: FileInfo,
    case_id: str,
) -> PreprocessingResult:
    """
    Comprehensive map-reduce summarization, cache for deep dives.
    
    LLM Calls: 25-50
    Time: 30s
    Storage: S3 + ChromaDB
    """
    
    # Use map-reduce summarization (from Step 2)
    extraction_result = await summarize_text_auto(file_content, config)
    
    # Store raw in S3
    s3_uri = await store_in_s3(file_content, file_info, case_id)
    
    return PreprocessingResult(
        temp_id=generate_temp_id(),
        data_type=DataType.UNSTRUCTURED_TEXT,
        summary=generate_concise_summary(extraction_result.lines[0], 500),
        full_extraction=extraction_result.lines[0],
        content_ref=s3_uri,
        content_size_bytes=len(file_content.encode('utf-8')),
        extraction_method="full_cached",
        extraction_metadata=extraction_result.metadata,
        required_user_choice=True,
        chosen_processing_mode="full_cached",
        # Will be cached in Step 5 (async)
    )
```

#### Option 4: Targeted Search

```python
async def process_targeted_search(
    file_content: str,
    file_info: FileInfo,
    case_id: str,
) -> PreprocessingResult:
    """
    Store raw content only, wait for user queries.
    
    LLM Calls: 0 (until first query)
    Time: 0.5s
    Storage: S3 only (ChromaDB after first query)
    """
    
    # Store raw in S3
    s3_uri = await store_in_s3(file_content, file_info, case_id)
    
    # Generate preview
    lines = file_content.split('\n')
    preview = '\n'.join(lines[:100] + ["\n--- STORED FOR SEARCH ---\n"] + lines[-100:])
    
    return PreprocessingResult(
        temp_id=generate_temp_id(),
        data_type=DataType.UNSTRUCTURED_TEXT,
        summary=f"Document stored for targeted search. {len(lines)} lines available.",
        full_extraction=preview,
        content_ref=s3_uri,
        content_size_bytes=len(file_content.encode('utf-8')),
        extraction_method="targeted_search",
        required_user_choice=True,
        chosen_processing_mode="targeted_search",
        # Will NOT be cached until first query
    )

async def handle_targeted_search_query(
    evidence_id: str,
    query: str,
    case_id: str,
) -> str:
    """
    Execute targeted search and auto-upgrade to cached.
    """
    
    # Get evidence
    evidence = await evidence_service.get_evidence(evidence_id)
    
    # Retrieve raw content from S3
    raw_content = await s3_client.get_object(evidence.content_ref)
    
    # Search content
    answer = await search_content(raw_content, query)
    
    # Auto-upgrade: Embed in background for future queries
    background_tasks.add_task(
        store_in_vector_db_background,
        case_id=case_id,
        evidence_id=evidence_id,
        preprocessed_content=raw_content,
        data_type=evidence.source_type,
        extraction_method="targeted_search_auto_upgraded",
    )
    
    return answer
```

### 5.3 Cost Optimization

**Scenario**: 100 users upload 200KB unknown documents

| Strategy | Users | Cost |
|----------|-------|------|
| Preview (no cache) | 40 | $0.00 |
| Overview (cached) | 30 | $0.06 |
| Full (cached) | 20 | $1.00 |
| Targeted search | 10 | $0.03 |
| **Total (user choice)** | **100** | **$1.09** |
| **Force full analysis (old)** | **100** | **$5.00** |
| **Savings** | | **78%** |

---

## 6. Data Type Specifications

### Summary Table

| Data Type | Extraction Strategy | LLM Calls | Compression | Speed | Always Cached |
|-----------|-------------------|-----------|-------------|-------|---------------|
| **LOGS_AND_ERRORS** | Crime Scene ±200 lines | 0 | 200:1 | ⚡ 0.5s | ✅ Yes |
| **METRICS_AND_PERFORMANCE** | Anomaly Detection | 0 | 167:1 | ⚡ 0.3s | ✅ Yes |
| **STRUCTURED_CONFIG** | Parse + Sanitize | 0 | 1:1 | ⚡ 0.2s | ✅ Yes |
| **SOURCE_CODE** | AST Extraction | 0 | 50:1 | ⚡ 0.5s | ✅ Yes |
| **UNSTRUCTURED_TEXT <100KB** | Auto Summarization | 1-4 | 10:1 | ⚡ 2-10s | ✅ Yes |
| **UNSTRUCTURED_TEXT ≥100KB** | **User Choice** | 0-50 | Varies | Varies | Depends |
| **VISUAL_EVIDENCE <5MB** | Vision Analysis | 1 | N/A | 🐢 2-5s | ✅ Yes |
| **VISUAL_EVIDENCE ≥5MB** | **User Choice** | 0-1 | N/A | Varies | Depends |

---

## 7. Output Formats

### 7.1 PreprocessingResult Schema

```python
class PreprocessingResult(BaseModel):
    """
    Output from preprocessing pipeline.
    Fed into Evidence Architecture for classification and evaluation.
    """
    
    # Identity
    temp_id: str = Field(
        description="Temporary ID (before Evidence object created)"
    )
    
    # Classification
    data_type: DataType = Field(
        description="Preprocessing data type classification"
    )
    
    # Content - Two levels
    summary: str = Field(
        max_length=500,
        description="Concise summary for Evidence.summary (<500 chars)"
    )
    full_extraction: str = Field(
        description="Complete extraction output (for agent analysis)"
    )
    
    # Storage
    content_ref: Optional[str] = Field(
        None,
        description="S3 URI to raw artifact (if stored)"
    )
    content_size_bytes: int = Field(
        description="Size of raw artifact"
    )
    content_type: str = Field(
        description="MIME type"
    )
    
    # Extraction metadata
    extraction_method: str = Field(
        description="Method used: crime_scene_extraction, anomaly_detection, etc."
    )
    compression_ratio: float = Field(
        ge=0.0,
        description="Ratio of extracted to raw (0.005 = 200:1 compression)"
    )
    extraction_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific metadata (error counts, anomaly details, etc.)"
    )
    
    # Insights
    insights: Optional[PreprocessingInsights] = Field(
        None,
        description="Structured insights extracted from data"
    )
    
    # Sanitization
    sanitization_applied: bool = Field(
        default=False,
        description="Whether PII/secrets were redacted"
    )
    redactions_count: int = Field(
        default=0,
        description="Number of redactions made"
    )
    
    # User choice (if applicable)
    required_user_choice: bool = Field(
        default=False,
        description="Whether user was asked to choose processing mode"
    )
    chosen_processing_mode: Optional[str] = Field(
        None,
        description="preview_no_cache | overview_cached | full_cached | targeted_search"
    )
    grace_period_expires_at: Optional[datetime] = Field(
        None,
        description="For preview_no_cache: when temp file will be deleted"
    )
    
    # Performance
    processing_time_ms: int = Field(
        description="Total preprocessing time (milliseconds)"
    )

class PreprocessingInsights(BaseModel):
    """Structured insights extracted during preprocessing"""
    
    method: str = Field(
        description="Extraction method used"
    )
    
    compression_ratio: float = Field(
        description="Extraction compression ratio"
    )
    
    data_quality: str = Field(
        description="high | medium | low - assessed quality of input data"
    )
    
    key_findings: List[str] = Field(
        default_factory=list,
        description="Bullet points of key diagnostic findings"
    )
    
    # Type-specific insights
    error_summary: Optional[ErrorSummary] = None  # For LOGS_AND_ERRORS
    anomaly_summary: Optional[AnomalySummary] = None  # For METRICS_AND_PERFORMANCE
    config_summary: Optional[ConfigSummary] = None  # For STRUCTURED_CONFIG

class ErrorSummary(BaseModel):
    """Insights from log analysis"""
    total_errors: int
    severity_distribution: Dict[str, int]
    first_error_line: int
    last_error_line: int
    error_burst_detected: bool
    unique_error_types: List[str]

class AnomalySummary(BaseModel):
    """Insights from metrics analysis"""
    total_anomalies: int
    metrics_analyzed: List[str]
    anomaly_types: Dict[str, int]  # "spike": 5, "drop": 3
    most_anomalous_metric: str
    time_range: str

class ConfigSummary(BaseModel):
    """Insights from config analysis"""
    format: str  # yaml, json, toml, ini
    total_keys: int
    secrets_found: int
    secrets_redacted: bool
    validation_status: str  # valid | invalid | partial
```

### 7.2 Integration with Evidence Architecture

**Mapping**:

```python
# After preprocessing completes
preprocessing_result = await preprocessing_service.process_upload(...)

# Evidence Architecture uses these fields:
evidence = Evidence(
    summary=preprocessing_result.summary,  # <500 chars
    content_ref=preprocessing_result.content_ref,  # S3 URI
    content_size_bytes=preprocessing_result.content_size_bytes,
    content_type=preprocessing_result.content_type,
    source_type=map_data_type_to_source_type(preprocessing_result.data_type),
    form=EvidenceForm.DOCUMENT,  # All uploads are documents
    file_metadata=FileMetadata(
        filename=original_filename,
        content_type=preprocessing_result.content_type,
        size_bytes=preprocessing_result.content_size_bytes,
        upload_timestamp=datetime.now(timezone.utc),
        file_id=preprocessing_result.temp_id,
    ),
    preprocessed=True,
)

# Full extraction used for hypothesis analysis
# (Not stored in Evidence object - used transiently by agent)
full_context = preprocessing_result.full_extraction
```

---

## 8. Configuration

### Environment Variables

```bash
# ============================================================
# FILE INGESTION & VALIDATION
# ============================================================

MAX_UPLOAD_SIZE_MB=10                    # 10 MB hard limit
ALLOWED_MIME_TYPES=text/plain,text/csv,application/json,application/yaml,image/png,image/jpeg
BLOCKED_EXTENSIONS=.exe,.dll,.zip,.bin

# ============================================================
# DATA CLASSIFICATION
# ============================================================

CLASSIFICATION_SAMPLE_SIZE=5000          # Bytes for pattern matching

# Severity keywords for LOGS_AND_ERRORS
LOGS_SEVERITY_FATAL=100
LOGS_SEVERITY_CRITICAL=90
LOGS_SEVERITY_ERROR=50
LOGS_SEVERITY_WARN=10

# Anomaly detection for METRICS_AND_PERFORMANCE
METRICS_ANOMALY_Z_SCORE_THRESHOLD=3.0

# ============================================================
# SYNCHRONOUS PROCESSING
# ============================================================

# Crime Scene Extraction
CRIME_SCENE_CONTEXT_LINES=200
TAIL_EXTRACTION_LINES=500
ERROR_BURST_WINDOW=50
ERROR_BURST_THRESHOLD=10

# User Choice Thresholds
USER_CHOICE_TEXT_THRESHOLD_BYTES=100000  # 100KB for UNSTRUCTURED_TEXT
USER_CHOICE_IMAGE_THRESHOLD_BYTES=5242880  # 5MB for VISUAL_EVIDENCE

# Summarization
SUMMARIZATION_MODEL=claude-sonnet-4-5-20250929
SUMMARIZATION_CHUNK_SIZE_TOKENS=4000
SUMMARIZATION_OVERLAP_TOKENS=200

# Vision Model
VISION_MODEL=claude-3-5-sonnet-20241022
VISION_MODEL_MAX_TOKENS=1000
VISION_MAX_IMAGE_SIZE_BYTES=5242880

# ============================================================
# ASYNC BACKGROUND PROCESSING
# ============================================================

# Vector DB Chunking (for embeddings)
EMBEDDING_CHUNK_SIZE_TOKENS=512
EMBEDDING_CHUNK_OVERLAP_TOKENS=50
EMBEDDING_MODEL=BGE-M3
EMBEDDING_BATCH_SIZE=10

# ChromaDB
CHROMADB_HOST=localhost
CHROMADB_PORT=8000
CHROMADB_COLLECTION_PREFIX=case_

# ============================================================
# USER CHOICE SYSTEM
# ============================================================

PREVIEW_GRACE_PERIOD_SECONDS=300         # 5 minutes
TARGETED_SEARCH_AUTO_EMBED=true

# ============================================================
# SANITIZATION
# ============================================================

SANITIZE_PII=true
AUTO_SANITIZE_BASED_ON_PROVIDER=true     # Skip for LOCAL

PII_REDACT_EMAILS=true
PII_REDACT_PHONE_NUMBERS=true
PII_REDACT_IP_ADDRESSES=false            # Keep for troubleshooting
PII_REDACT_API_KEYS=true
PII_REDACT_PASSWORDS=true

# ============================================================
# STORAGE
# ============================================================

S3_BUCKET_EVIDENCE=faultmaven-evidence
S3_REGION=us-east-1

# ============================================================
# ERROR HANDLING
# ============================================================

BACKGROUND_TASK_MAX_RETRIES=3
BACKGROUND_TASK_RETRY_BACKOFF=exponential
BACKGROUND_TASK_FAILURE_ALERT=silent
```

---

## 9. Implementation Guide

### 9.1 Service Architecture

```python
# services/preprocessing_service.py
class PreprocessingService:
    """Main preprocessing orchestrator"""
    
    async def process_upload(
        self,
        file: UploadFile,
        case_id: str,
        user_id: str,
    ) -> Union[PreprocessingResult, UserChoiceRequest]:
        """
        Main entry point for file uploads.
        
        Returns either:
        - PreprocessingResult (if auto-processed)
        - UserChoiceRequest (if needs user decision)
        """
        
        # Step 1: Validate
        await self._validate_file(file)
        
        # Read content
        raw_content = await file.read()
        
        # Step 1: Classify
        data_type = classify_data_type(
            filename=file.filename,
            content_sample=raw_content[:5000],
            mime_type=file.content_type,
        )
        
        # Step 2: Check if user choice needed
        if self._requires_user_choice(data_type, len(raw_content)):
            return await self._request_user_choice(
                file=file,
                data_type=data_type,
                raw_content=raw_content,
            )
        
        # Step 2: Extract
        extraction_result = await self._extract_by_type(
            data_type=data_type,
            content=raw_content.decode('utf-8'),
            filename=file.filename,
        )
        
        # Step 3: Sanitize
        sanitization_result = sanitize_content(
            content='\n'.join(extraction_result.lines),
            config=self.sanitization_config,
            provider=self.llm_provider,
        )
        
        # Step 4: Package
        preprocessing_result = await package_preprocessing_result(
            extraction_result=extraction_result,
            sanitization_result=sanitization_result,
            file_info=FileInfo(
                filename=file.filename,
                mime_type=file.content_type,
                raw_content=raw_content,
                extension=Path(file.filename).suffix,
            ),
            case_id=case_id,
            data_type=data_type,
        )
        
        return preprocessing_result
    
    async def process_user_choice(
        self,
        temp_id: str,
        chosen_mode: str,
    ) -> PreprocessingResult:
        """
        Process file after user selects processing mode.
        """
        
        # Retrieve temp file
        temp_file = await self.temp_storage.get(temp_id)
        
        if chosen_mode == "preview_no_cache":
            return await process_preview_no_cache(temp_file.content)
        
        elif chosen_mode == "overview_cached":
            return await process_overview_cached(
                temp_file.content,
                temp_file.file_info,
                temp_file.case_id,
            )
        
        elif chosen_mode == "full_cached":
            return await process_full_cached(
                temp_file.content,
                temp_file.file_info,
                temp_file.case_id,
            )
        
        elif chosen_mode == "targeted_search":
            return await process_targeted_search(
                temp_file.content,
                temp_file.file_info,
                temp_file.case_id,
            )
```

### 9.2 API Endpoints

```python
# api/preprocessing.py
@router.post("/cases/{case_id}/upload")
async def upload_file(
    case_id: str,
    file: UploadFile,
    current_user: User = Depends(get_current_user),
):
    """
    Upload file for preprocessing.
    
    Returns either:
    - Preprocessed result (if auto-processed)
    - User choice request (if large unknown file)
    """
    
    result = await preprocessing_service.process_upload(
        file=file,
        case_id=case_id,
        user_id=current_user.id,
    )
    
    if isinstance(result, UserChoiceRequest):
        # Large file - needs user decision
        return {
            "status": "awaiting_user_decision",
            "temp_id": result.temp_id,
            "preview": result.preview,
            "options": result.processing_options,
        }
    
    # Auto-processed - continue to Evidence Architecture
    # (See Evidence Architecture for next steps)
    
    return {
        "status": "completed",
        "data_id": result.temp_id,
        "preprocessing": {
            "data_type": result.data_type,
            "method": result.extraction_method,
            "summary": result.summary,
            "insights": result.insights,
        }
    }

@router.post("/preprocessing/{temp_id}/choice")
async def submit_processing_choice(
    temp_id: str,
    choice: ProcessingChoice,
    current_user: User = Depends(get_current_user),
):
    """
    Submit user's processing mode choice for large file.
    """
    
    preprocessing_result = await preprocessing_service.process_user_choice(
        temp_id=temp_id,
        chosen_mode=choice.mode,
    )
    
    return {
        "status": "completed",
        "data_id": preprocessing_result.temp_id,
        "preprocessing": {
            "data_type": preprocessing_result.data_type,
            "method": preprocessing_result.extraction_method,
            "summary": preprocessing_result.summary,
        }
    }
```

---

## 10. Examples

### Example 1: Log File (Auto-Processed)

```python
# User uploads 5MB application.log

# Step 1: Classify
data_type = DataType.LOGS_AND_ERRORS  # Detected from timestamps and ERROR keywords

# Step 2: Crime Scene Extraction
extraction_result = extract_crime_scene(log_content, config)
# Output: 400 lines (±200 around highest severity error)
# Compression: 5MB → 25KB (200:1)

# Step 3: Sanitize
sanitization_result = sanitize_content(...)
# No PII found, 0 redactions

# Step 4: Package
preprocessing_result = PreprocessingResult(
    temp_id="temp_abc123",
    data_type=DataType.LOGS_AND_ERRORS,
    summary="Application log: 847 entries, 23 NullPointerExceptions in auth-service",
    full_extraction="[400 lines of context around errors]",
    content_ref="s3://bucket/case_123/application_log_xyz.log",
    content_size_bytes=5242880,
    content_type="text/plain",
    extraction_method="crime_scene_extraction",
    compression_ratio=0.005,
    extraction_metadata={
        "total_errors": 23,
        "crime_scene_line": 12450,
        "severity": 50,
    },
    insights=PreprocessingInsights(
        method="crime_scene_extraction",
        compression_ratio=0.005,
        data_quality="high",
        key_findings=[
            "23 NullPointerExceptions in auth-service",
            "All errors after deployment at 14:10 UTC",
            "Error burst detected around line 12450",
        ],
        error_summary=ErrorSummary(
            total_errors=23,
            severity_distribution={"ERROR": 23},
            first_error_line=12450,
            last_error_line=18920,
            error_burst_detected=True,
            unique_error_types=["NullPointerException"],
        ),
    ),
    processing_time_ms=520,
)

# Step 5: Async - Vector DB
# Background task chunks 25KB and stores ~50 chunks in ChromaDB
```

### Example 2: Large Unknown Document (User Choice)

```python
# User uploads 250KB troubleshooting_guide.txt

# Step 1: Classify
data_type = DataType.UNSTRUCTURED_TEXT

# Step 2: Check size
if size >= 100KB:
    # Request user choice
    return UserChoiceRequest(
        temp_id="temp_def456",
        file_info={...},
        preview={...},
        processing_options=[
            {"mode": "preview_no_cache", ...},
            {"mode": "overview_cached", ...},
            {"mode": "full_cached", ...},
            {"mode": "targeted_search", ...},
        ]
    )

# User selects "overview_cached"

# Step 2 (after choice): Smart Overview
overview = await llm_client.generate(...)  # 1 LLM call, 2s

# Step 3: Sanitize
# No PII found

# Step 4: Package
preprocessing_result = PreprocessingResult(
    temp_id="temp_def456",
    data_type=DataType.UNSTRUCTURED_TEXT,
    summary="Troubleshooting guide with 5 main sections: Setup, Common Issues, ...",
    full_extraction="[2KB structural overview from LLM]",
    content_ref="s3://bucket/case_123/troubleshooting_guide_xyz.txt",
    content_size_bytes=256000,
    extraction_method="overview_cached",
    chosen_processing_mode="overview_cached",
    processing_time_ms=2100,
)

# Step 5: Async - Vector DB
# Background task chunks 2KB overview and stores ~4 chunks
```

### Example 3: Config File

```python
# User uploads database.yaml (5KB)

# Step 1: Classify
data_type = DataType.STRUCTURED_CONFIG

# Step 2: Parse & Sanitize
parsed_config = yaml.safe_load(content)
redact_secrets(parsed_config)  # Redacts passwords, API keys

# Step 3: Already sanitized in Step 2

# Step 4: Package
preprocessing_result = PreprocessingResult(
    temp_id="temp_ghi789",
    data_type=DataType.STRUCTURED_CONFIG,
    summary="Database config: PostgreSQL connection settings, 3 secrets redacted",
    full_extraction="[Full YAML with secrets redacted]",
    content_ref="s3://bucket/case_123/database_yaml_xyz.yaml",
    content_size_bytes=5120,
    extraction_method="parse_and_sanitize",
    compression_ratio=1.0,  # No compression for configs
    insights=PreprocessingInsights(
        method="parse_and_sanitize",
        compression_ratio=1.0,
        data_quality="high",
        key_findings=[
            "PostgreSQL connection settings",
            "Pool size: 100 connections",
            "Timeout: 30s",
        ],
        config_summary=ConfigSummary(
            format="yaml",
            total_keys=47,
            secrets_found=3,
            secrets_redacted=True,
            validation_status="valid",
        ),
    ),
    sanitization_applied=True,
    redactions_count=3,
    processing_time_ms=230,
)

# Step 5: Async - Vector DB
# Background task chunks 5KB and stores ~10 chunks
```

---

**END OF DOCUMENT**

**Version**: 2.0  
**Date**: 2025-11-01  
**Status**: Production Ready  
**Integration**: Works with Evidence Architecture v1.1 + Investigation State Framework + Prompt Engineering Architecture  
**Key Changes from v1.0**:
- Aligned with Evidence Architecture (preprocessing → classification → evidence creation flow)
- Removed duplicate evidence/hypothesis tracking (now in Evidence Architecture)
- Clarified ChromaDB as long-term memory (not primary evidence evaluation)
- Enhanced PreprocessingResult schema with insights
- Clear data type → evidence source type mapping
- Separated preprocessing (extract) from evidence evaluation (analyze)