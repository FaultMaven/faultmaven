# Data Preprocessing Quick Reference

**Document Type**: Quick Reference Guide
**Version**: 1.0
**Last Updated**: 2025-10-15

---

## 6 Purpose-Driven Data Classes

Each class has a **unique preprocessing strategy** aligned with its characteristics.

### 1. LOGS_AND_ERRORS

**What**: Chronological event-based text
**Examples**: App logs, system logs, stack traces, error messages, alerts
**Strategy**: Crime Scene Extraction with severity prioritization
**Processing**:
- Scan for ERROR keywords with severity weights (FATAL=100, CRITICAL=90, ERROR=50)
- Find highest-severity error (not just first error)
- Detect multiple crime scenes → Extract first + last (onset + current state)
- Detect error bursts → Expand window to cover clustering
- Safety check → Truncate if output >500 lines
- No LLM needed

**Output**: ~400 line snippet with full context (single error) or ~200 lines × 2 (multiple crimes scenes)

---

### 2. UNSTRUCTURED_TEXT

**What**: Free-form human text
**Examples**: Slack threads, incident reports, tickets, HTML pages (scraped), docs
**Strategy**: Size check → Direct use or defer to Step 3 chunking
**Processing** (Step 2):
- Check token count
- If <8K tokens (~32KB): Mark for direct use
- If >8K tokens: Mark for Step 3 chunking (no LLM in Step 2)

**Output**: Original text (small) or summary from Step 3 (large)

---

### 3. STRUCTURED_CONFIG

**What**: Declarative system state
**Examples**: YAML, JSON, INI, TOML, .env files
**Strategy**: Parse + sanitize, pass full content
**Processing**:
- Parse to validate structure
- Scan for secrets (API keys, passwords)
- Pass full config to diagnostic LLM
- No compression (every line matters)

**Output**: Full config with secrets flagged

---

### 4. METRICS_AND_PERFORMANCE

**What**: Quantitative data
**Examples**: CSV time-series, OpenTelemetry traces, profiling data
**Strategy**: Deterministic anomaly detection
**Processing**:
- Parse CSV/JSON with pandas
- Detect spikes/drops using z-score (threshold: 3.0)
- Generate natural language summary
- No LLM needed (pure statistical)

**Output**: Anomaly summary with time ranges

---

### 5. SOURCE_CODE

**What**: Executable logic
**Examples**: Python, JavaScript, Java, Go, TypeScript, C++
**Strategy**: Code-aware snippet extraction
**Processing**:
- Detect language
- Parse AST to find structure
- If error context: Extract mentioned functions/classes
- Include dependencies for context
- No LLM needed (AST parsing)

**Output**: Relevant code blocks with line numbers

---

### 6. VISUAL_EVIDENCE

**What**: Graphical snapshots
**Examples**: Screenshots, UI captures, browser console images
**Strategy**: Vision model transcription
**Processing**:
- Send to vision LLM (GPT-4V, Gemini)
- Prompt: Describe from troubleshooting perspective
- Focus on error messages, UI state, visible text

**Output**: Text description of screenshot

---

## Processing Decision Flow (4 Steps)

```
File Upload
    ↓
[Step 1] Fast, Rule-Based Classification
         Backend classify_file() - instant, no LLM
         Extension + content patterns → 6 data types
         LLM Calls: 0
    ↓
[Step 2] Type-Specific Intelligent Extraction
         Goal: Create concise, high-signal summary
    ↓
┌───────────────┬──────────────────┬────────────────────┬─────────────────────┬──────────────┬───────────────┐
│ LOGS_AND_     │ UNSTRUCTURED_    │ STRUCTURED_        │ METRICS_AND_        │ SOURCE_CODE  │ VISUAL_       │
│ ERRORS        │ TEXT             │ CONFIG             │ PERFORMANCE         │              │ EVIDENCE      │
├───────────────┼──────────────────┼────────────────────┼─────────────────────┼──────────────┼───────────────┤
│ Crime Scene   │ Size check:      │ Parse + validate   │ Pandas/numpy        │ AST parsing  │ Vision LLM    │
│ ±200 lines    │ <8K → direct     │ Scan secrets       │ Z-score anomaly     │ Extract      │ GPT-4V/Gemini │
│ around ERROR  │ >8K → mark for   │ Pass full content  │ Natural summary     │ functions    │ Transcription │
│               │ Step 3           │                    │                     │              │               │
│ LLM: 0        │ LLM: 0           │ LLM: 0             │ LLM: 0              │ LLM: 0       │ LLM: 1        │
└───────────────┴──────────────────┴────────────────────┴─────────────────────┴──────────────┴───────────────┘
    ↓
[Step 3] Chunking Fallback (Long Unstructured Text Only)
         ⚠️  MOST FILES SKIP THIS STEP
         Only triggered if Step 2 marked needs_chunking=True
         • Split into 4K token chunks
         • Map: Summarize each chunk in parallel (N LLM calls)
         • Reduce: Synthesize summaries (1 LLM call)
         • LLM Calls: N+1
    ↓
[Step 4] Final Diagnostic Call
         • PII/secret sanitization (configurable)
         • Package concise output from Step 2 or Step 3
         • Send to FaultMaven diagnostic agent
         • LLM Calls: 1
```

### Total LLM Calls Per File

| Scenario | Step 1 | Step 2 | Step 3 | Step 4 | Total |
|----------|--------|--------|--------|--------|-------|
| **LOG_FILE** | 0 | 0 | 0 (skip) | 1 | **1 call** |
| **METRICS_DATA** | 0 | 0 | 0 (skip) | 1 | **1 call** |
| **CONFIG** | 0 | 0 | 0 (skip) | 1 | **1 call** |
| **SOURCE_CODE** | 0 | 0 | 0 (skip) | 1 | **1 call** |
| **SCREENSHOT** | 0 | 1 | 0 (skip) | 1 | **2 calls** |
| **Short text (<8K)** | 0 | 0 | 0 (direct) | 1 | **1 call** |
| **Long text (>8K)** | 0 | 0 | N+1 | 1 | **N+2 calls** |

**Key Insight**: 80%+ of files process with just **1 LLM call** (the final diagnostic call)

---

## LLM Usage Summary

| Data Type | Step 2 LLM? | Step 3 LLM? | When? |
|-----------|-------------|-------------|-------|
| LOGS_AND_ERRORS | ❌ No | N/A (skip) | Never (keyword scan) |
| UNSTRUCTURED_TEXT | ❌ No | ✅ If >8K tokens | Only if >8K tokens (Step 3) |
| STRUCTURED_CONFIG | ❌ No | N/A (skip) | Never (parse + validate) |
| METRICS_AND_PERFORMANCE | ❌ No | N/A (skip) | Never (pandas/numpy) |
| SOURCE_CODE | ❌ No | N/A (skip) | Never (AST parsing) |
| VISUAL_EVIDENCE | ✅ Yes (1 call) | N/A (skip) | Always (vision model) |

**Result**: Most files (80%+) process with **0 LLM calls** in Steps 1-3 → Fast + cheap

---

## Key Design Principles

1. **4-Step Sequential Pipeline**: Classification → Extraction → Chunking (if needed) → Diagnostic
2. **Minimize LLM Calls**: Use deterministic methods (keyword scan, pandas, AST parsing) in Steps 1-2
3. **Step 3 is Rare**: Chunking only for long unstructured text (>8K tokens)
4. **Purpose-Driven Classes**: Each class = unique strategy, no overlap
5. **80%+ Fast Path**: Most files process with just 1 LLM call (Step 4: diagnostic agent)

---

## File Extension Mapping

### Quick Classification Guide

| Extension | Data Type | Strategy |
|-----------|-----------|----------|
| `.log`, `.txt` (with ERROR) | LOGS_AND_ERRORS | Crime Scene |
| `.txt`, `.md`, `.rst` (no ERROR) | UNSTRUCTURED_TEXT | Direct/Chunk |
| `.html`, `.htm` | UNSTRUCTURED_TEXT | Scrape + Direct/Chunk |
| `.yaml`, `.yml`, `.json`, `.toml`, `.ini`, `.env` | STRUCTURED_CONFIG | Parse + Sanitize |
| `.csv`, `.tsv` (time-series) | METRICS_AND_PERFORMANCE | Anomaly Detection |
| `.py`, `.js`, `.java`, `.go`, `.ts`, `.cpp` | SOURCE_CODE | AST Extract |
| `.png`, `.jpg`, `.jpeg` | VISUAL_EVIDENCE | Vision Model |

---

## Configuration Highlights

```bash
# Step 1: Classification
CLASSIFICATION_SAMPLE_SIZE=5000  # Bytes to sample for content patterns

# Step 2: Extraction (LOGS_AND_ERRORS)
CRIME_SCENE_CONTEXT_LINES=200           # ±200 lines around single error
TAIL_EXTRACTION_LINES=500               # Last 500 lines if no errors
CRIME_SCENE_MAX_SNIPPET_LINES=500       # Safety limit
ERROR_BURST_DETECTION_WINDOW=50         # Lines to check for clustering
ERROR_BURST_THRESHOLD=10                # Min errors to trigger burst mode
MULTIPLE_CRIME_SCENES_LINES=100         # Lines around first + last

# Severity weights
LOGS_SEVERITY_FATAL=100
LOGS_SEVERITY_CRITICAL=90
LOGS_SEVERITY_ERROR=50

# Step 2: Extraction (Other Types)
METRICS_ANOMALY_Z_SCORE_THRESHOLD=3.0
VISION_MODEL_PROVIDER=openai
VISION_MODEL_NAME=gpt-4-vision-preview

# Step 3: Chunking (Long Text Only)
UNSTRUCTURED_SAFE_TOKEN_LIMIT=8000  # Direct use if below this
CHUNK_SIZE_TOKENS=4000              # Chunk size for map-reduce
MAP_REDUCE_MAX_PARALLEL=5           # Parallel LLM calls

# Step 4: Diagnostic
SANITIZE_PII=true
AUTO_SANITIZE_BASED_ON_PROVIDER=true  # Skip for LOCAL provider
```

---

**See Also**: [Two-Tier Data Preprocessing Architecture](./preprocessing-two-tier-architecture.md) (complete spec)
