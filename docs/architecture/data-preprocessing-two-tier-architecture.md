# Two-Tier Data Preprocessing Architecture

**Document Type**: Architecture Design Specification
**Version**: 1.0
**Last Updated**: 2025-10-14

---

## Executive Summary

This document defines a **two-tiered preprocessing architecture** that combines the speed and efficiency of "Crime Scene Extraction" with the thoroughness of "Deep Scan" as a fallback. The design prioritizes **fast, cheap, and accurate** processing for common cases while maintaining the capability for deep analysis when needed.

### Core Principles

1. **Fast Path First**: Default to lightweight "Crime Scene" extraction (solves 80%+ of cases)
2. **Deep Scan Fallback**: Offer comprehensive analysis only when needed (user-triggered)
3. **Classify First, Then Process**: Rule-based classification routes to specialized extractors
4. **Maximum Signal, Minimum Noise**: Extract only high-value diagnostic information
5. **Security by Default**: Sanitize PII/secrets before sending to LLM (configurable)

---

## Table of Contents

1. [Two-Tier Strategy](#two-tier-strategy)
2. [Preprocessing Pipeline (4 Stages)](#preprocessing-pipeline-4-stages)
3. [Stage 1: Ingestion & Validation](#stage-1-ingestion--validation)
4. [Stage 2: Rule-Based Classification](#stage-2-rule-based-classification)
5. [Stage 3: Type-Specific Extraction](#stage-3-type-specific-extraction)
6. [Stage 4: Sanitization & Packaging](#stage-4-sanitization--packaging)
7. [Data Type Specifications](#data-type-specifications)
8. [Configuration](#configuration)

---

## Two-Tier Strategy

### Tier 1: Fast Path (Crime Scene Extraction)

**Default method for all uploads. Optimized for speed and accuracy.**

```
User uploads log file
        ↓
Scan for ERROR/Exception/panic keywords
        ↓
Extract 200 lines before + after first match
        ↓
Send snippet (~400 lines) to diagnostic LLM
        ↓
LLM analyzes and responds
        ↓
✓ Problem solved (80%+ of cases)
```

**Characteristics**:
- **Fast**: Keyword scan + extraction in milliseconds
- **Cheap**: Small snippet (~400 lines) to LLM, minimal tokens
- **Accurate**: Captures the "crime scene" with full context
- **No preprocessing LLM**: Direct to diagnostic LLM

**When it works**:
- Clear error messages in logs
- Stack traces in error reports
- Recent failures (errors near end of file)
- Simple, single-root-cause problems

---

### Tier 2: Deep Scan (Breadth-Based Fallback)

**Triggered only when Fast Path fails or is inconclusive.**

```
Fast Path attempt
        ↓
LLM: "I couldn't find a clear error in the most recent activity.
      Would you like me to perform a full, in-depth analysis?
      This may take a few moments."
        ↓
User: "Yes, please analyze the full file"
        ↓
Backend triggers Deep Scan workflow:
  1. Split file into chunks
  2. Send each chunk to preprocessing LLM (parallel)
  3. Preprocessing LLM identifies relevant sections per chunk
  4. Combine findings
  5. Send to diagnostic LLM
        ↓
Comprehensive analysis returned
```

**Characteristics**:
- **Thorough**: Analyzes entire file, not just error patterns
- **Slower**: Multiple LLM calls (preprocessing + diagnostic)
- **More expensive**: Processes full file in chunks
- **User-triggered**: Explicit opt-in for deeper analysis

**When needed**:
- No clear error patterns (subtle issues)
- Distributed problems across entire file
- Performance degradation (slow, not failing)
- Complex multi-root-cause scenarios

---

## Preprocessing Pipeline (4 Steps)

All uploaded data flows through this sequential pipeline before reaching the diagnostic agent.

```
┌─────────────────────────────────────────────────────────────┐
│ STEP 1: FAST, RULE-BASED CLASSIFICATION                     │
│ • Backend Python classify_file() function                   │
│ • Extension + content pattern matching (no LLM)             │
│ • Instant classification → 6 data types                     │
│ • LLM Calls: 0                                              │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 2: TYPE-SPECIFIC INTELLIGENT EXTRACTION                │
│ Goal: Create concise, high-signal summary                   │
│                                                              │
│ • LOGS_AND_ERRORS → Crime Scene (±200 lines) | LLM: 0      │
│ • METRICS_AND_PERFORMANCE → Anomaly detection | LLM: 0      │
│ • STRUCTURED_CONFIG → Parse + sanitize | LLM: 0             │
│ • SOURCE_CODE → AST extraction | LLM: 0                     │
│ • HTML_PAGE → Text scraping | LLM: 0                        │
│ • VISUAL_EVIDENCE → Vision model | LLM: 1 (vision)          │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 3: CHUNKING FALLBACK (Long Unstructured Text Only)     │
│ Only for: DOCUMENTATION, SLACK_MESSAGES, UNSTRUCTURED_TEXT  │
│                                                              │
│ 1. Check token count of extracted text                      │
│ 2. If < 8K tokens: Send directly | LLM: 0                  │
│ 3. If > 8K tokens: Trigger "Breadth-based Scan"            │
│    • Chunk into 4K token segments                           │
│    • Summarize each chunk in parallel (map)                 │
│    • Synthesize summaries (reduce)                          │
│    • LLM Calls: N chunks + 1 synthesis = N+1               │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 4: FINAL DIAGNOSTIC CALL                               │
│ • PII/secret sanitization (configurable)                    │
│ • Package concise output from Steps 2 or 3                  │
│ • Send to FaultMaven diagnostic agent                       │
│ • LLM Calls: 1 (diagnostic)                                 │
└─────────────────────────────────────────────────────────────┘
```

### Total LLM Calls Per File

| Scenario | Step 2 | Step 3 | Step 4 | Total |
|----------|--------|--------|--------|-------|
| **LOG_FILE (typical)** | 0 | 0 (skip) | 1 | **1 call** |
| **METRICS_DATA** | 0 | 0 (skip) | 1 | **1 call** |
| **SCREENSHOT** | 1 (vision) | 0 (skip) | 1 | **2 calls** |
| **Short text (<8K tokens)** | 0 | 0 (direct) | 1 | **1 call** |
| **Long text (>8K tokens)** | 0 | N+1 (map-reduce) | 1 | **N+2 calls** |

**Key Insight**: 80%+ of files process with just **1 LLM call** (the final diagnostic call)

---

## Step 1: Fast, Rule-Based Classification

**Purpose**: Instant, deterministic classification without any LLM calls

### 1.1 File Ingestion & Validation

**Purpose**: Gatekeeper to ensure basic viability before processing

#### File Size Limit

**Hard Limit**: 2 MB (2,097,152 bytes)

**Rationale**:
- Prevents server strain
- Aligns with realistic LLM context limits (200K tokens ≈ 0.8 MB usable)
- Forces users to provide focused data
- 2MB is sufficient for most diagnostic files:
  - Log file: ~50,000 lines
  - Metrics CSV: ~100,000 rows
  - Config file: ~50,000 lines
  - Stack trace: Unlimited depth

**Rejection Response**:
```json
{
  "error": "file_too_large",
  "file_size": 5242880,
  "max_size": 2097152,
  "message": "File exceeds 2MB limit",
  "suggestions": [
    "Upload only the relevant time range (last hour of logs)",
    "Filter to ERROR/FATAL level logs only",
    "Upload specific component logs, not entire system",
    "Compress or split the file"
  ]
}
```

#### File Type Validation

**Purpose**: Reject unsupported or dangerous file types

**Validation Methods**:

**Method 1: MIME Type Check**
```python
import mimetypes

def validate_mime_type(filename: str, content: bytes) -> bool:
    """Validate file is supported type"""

    # Allowed MIME types
    ALLOWED_MIMES = {
        'text/plain',           # .txt, .log
        'text/csv',             # .csv
        'text/html',            # .html
        'application/json',     # .json
        'application/yaml',     # .yaml
        'image/png',            # .png
        'image/jpeg',           # .jpg
    }

    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type in ALLOWED_MIMES
```

**Method 2: Extension Blocklist**
```python
BLOCKED_EXTENSIONS = {
    '.exe', '.dll', '.so',      # Executables
    '.zip', '.tar', '.gz',      # Archives (require decompression)
    '.pdf', '.doc', '.docx',    # Binary documents
    '.bin', '.dat',             # Generic binaries
}

def validate_extension(filename: str) -> bool:
    """Reject blocked extensions"""
    ext = Path(filename).suffix.lower()
    return ext not in BLOCKED_EXTENSIONS
```

**Combined Validation**:
```python
def validate_file(filename: str, content: bytes, size: int) -> ValidationResult:
    """Stage 1 validation"""

    # Size check
    if size > MAX_FILE_SIZE:
        return ValidationResult(
            valid=False,
            reason="file_too_large",
            suggestions=[...]
        )

    # Extension check
    if not validate_extension(filename):
        return ValidationResult(
            valid=False,
            reason="unsupported_file_type",
            message=f"File type {Path(filename).suffix} is not supported"
        )

    # MIME type check
    if not validate_mime_type(filename, content):
        return ValidationResult(
            valid=False,
            reason="invalid_mime_type",
            message="File content does not match expected type"
        )

    return ValidationResult(valid=True)
```

### 1.2 Classification Algorithm

**Purpose**: Fast, deterministic classification without LLM

**Why Rule-Based?**
- **Faster**: Milliseconds vs seconds (no LLM API call)
- **Cheaper**: No API costs
- **More reliable**: Deterministic, predictable results
- **Sufficient accuracy**: Pattern matching works well for file type detection

#### Data Types (Purpose-Driven Classification)

**Six classifications aligned with preprocessing strategies**:

1. **LOGS_AND_ERRORS**: Chronological event-based text (logs, stack traces, alerts)
2. **UNSTRUCTURED_TEXT**: Free-form human text (docs, Slack, tickets, reports)
3. **STRUCTURED_CONFIG**: Declarative system state (YAML, JSON, INI, TOML)
4. **METRICS_AND_PERFORMANCE**: Quantitative data (time-series, traces, profiling)
5. **SOURCE_CODE**: Executable logic (Python, Java, JavaScript, etc.)
6. **VISUAL_EVIDENCE**: Graphical snapshots (screenshots, UI captures)

**Key Principle**: Each class has a **unique preprocessing strategy**. No over-classification (same handling, different names) or under-classification (different handling, same name).

#### Implementation

**Multi-Method Decision Tree**:

```python
class RuleBasedClassifier:
    """Fast, deterministic file classification"""

    def classify(self, content: str, filename: str) -> DataType:
        """
        Classify file using pattern matching

        Decision order:
        1. Extension-based (fast path)
        2. Content patterns (first 5K chars)
        3. Structure detection
        4. Default fallback
        """

        # 1. Extension-based classification
        ext = Path(filename).suffix.lower()

        # VISUAL_EVIDENCE: Images
        if ext in {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}:
            return DataType.VISUAL_EVIDENCE

        # STRUCTURED_CONFIG: Configuration files
        if ext in {'.yaml', '.yml', '.json', '.toml', '.ini', '.env'}:
            return DataType.STRUCTURED_CONFIG

        # SOURCE_CODE: Programming languages
        if ext in {'.py', '.js', '.java', '.go', '.cpp', '.c', '.rb', '.php', '.ts', '.jsx', '.tsx'}:
            return DataType.SOURCE_CODE

        # METRICS_AND_PERFORMANCE: Data files
        if ext in {'.csv', '.tsv'}:
            return self._classify_csv_file(content)

        # LOGS_AND_ERRORS / UNSTRUCTURED_TEXT: Text files (need content analysis)
        if ext in {'.log', '.txt', '.md', '.rst', '.html', '.htm'}:
            return self._classify_text_file(content, ext)

        # 2. Content-based classification (no extension match)
        return self._classify_by_content(content)

    def _classify_text_file(self, content: str, ext: str) -> DataType:
        """Classify text files by content patterns"""

        sample = content[:5000]  # First 5K chars

        # LOGS_AND_ERRORS patterns (high priority)
        if re.search(r'\d{4}-\d{2}-\d{2}.*?(ERROR|WARN|INFO|DEBUG)', sample):
            return DataType.LOGS_AND_ERRORS

        if re.search(r'Traceback \(most recent call last\)', sample):
            return DataType.LOGS_AND_ERRORS

        if re.search(r'Exception in thread', sample):
            return DataType.LOGS_AND_ERRORS

        if re.search(r'panic:', sample):
            return DataType.LOGS_AND_ERRORS

        if re.search(r'\[ALERT\]|\[CRITICAL\]|\[FATAL\]', sample, re.IGNORECASE):
            return DataType.LOGS_AND_ERRORS

        # UNSTRUCTURED_TEXT patterns
        if ext in {'.html', '.htm'}:
            # HTML page - will be scraped to text
            return DataType.UNSTRUCTURED_TEXT

        if ext in {'.md', '.rst'}:
            # Documentation
            return DataType.UNSTRUCTURED_TEXT

        # Default for text files without clear patterns
        return DataType.UNSTRUCTURED_TEXT

    def _classify_csv_file(self, content: str) -> DataType:
        """Classify CSV files as metrics or generic text"""

        sample = content[:5000]

        # Check for time-series patterns
        if re.search(r'timestamp|time|date', sample, re.IGNORECASE):
            return DataType.METRICS_AND_PERFORMANCE

        # Check for numeric data
        lines = sample.split('\n')[:10]
        numeric_count = sum(1 for line in lines if re.search(r'\d+\.\d+', line))

        if numeric_count >= 5:
            return DataType.METRICS_AND_PERFORMANCE

        # Generic CSV - treat as unstructured
        return DataType.UNSTRUCTURED_TEXT

    def _classify_by_content(self, content: str) -> DataType:
        """Classify by content structure when extension unknown"""

        sample = content[:5000]

        # LOGS_AND_ERRORS patterns
        if re.search(r'\d{4}-\d{2}-\d{2}.*?(ERROR|WARN|INFO)', sample):
            return DataType.LOGS_AND_ERRORS

        # STRUCTURED_CONFIG patterns (JSON/YAML without extension)
        if re.match(r'\s*\{', sample) and re.search(r'[:,]', sample):
            return DataType.STRUCTURED_CONFIG

        if re.search(r'^[\w_]+:\s*[\w\d]', sample, re.MULTILINE):
            return DataType.STRUCTURED_CONFIG

        # METRICS_AND_PERFORMANCE patterns
        if re.search(r'timestamp,.*,.*', sample):
            return DataType.METRICS_AND_PERFORMANCE

        # Default
        return DataType.UNSTRUCTURED_TEXT
```

**Pattern Library**:

```python
PATTERNS = {
    DataType.LOGS_AND_ERRORS: [
        r'\d{4}-\d{2}-\d{2}.*?(ERROR|WARN|INFO|DEBUG)',  # Timestamp + log level
        r'\[\d{2}:\d{2}:\d{2}\]',                        # [HH:MM:SS]
        r'^\d+ \w+ \d{4}',                               # Syslog format
        r'Traceback \(most recent call last\)',         # Python traceback
        r'Exception in thread',                          # Java exception
        r'at [\w.$]+\(',                                 # Stack frame
        r'panic:',                                       # Go panic
        r'\[ALERT\]|\[CRITICAL\]|\[FATAL\]',           # Alert keywords
    ],

    DataType.STRUCTURED_CONFIG: [
        r'^\s*\{',                                       # JSON start
        r'^[\w_]+:\s*[\w\d]',                           # YAML key:value
        r'^\[[\w_]+\]',                                 # INI section
        r'^\w+=',                                        # ENV var
    ],

    DataType.METRICS_AND_PERFORMANCE: [
        r'timestamp,.*,.*',                              # CSV header with timestamp
        r'\d+\.\d+,\d+\.\d+,\d+\.\d+',                 # Numeric rows
        r'"metric":\s*"[\w_]+"',                        # JSON metrics
        r'cpu|memory|disk|latency|throughput',          # Metric keywords
    ],

    DataType.SOURCE_CODE: [
        r'^import |^from .* import',                    # Python imports
        r'^package |^func ',                            # Go
        r'^class |^def |^async def',                    # Python/Ruby
        r'^function |^const |^let ',                    # JavaScript
    ],

    DataType.UNSTRUCTURED_TEXT: [
        r'<!DOCTYPE html>',                              # HTML
        r'^# .+$',                                       # Markdown heading
        r'^\d+\.\s+',                                    # Numbered list
    ],

    DataType.VISUAL_EVIDENCE: [
        # Detected by file extension only
    ],
}
```

---

## Step 2: Type-Specific Intelligent Extraction

**Purpose**: Create concise, high-signal summary for diagnostic agent

**Goal**: Extract only the most relevant information, minimizing noise

**Key Design**: Most extractors use **deterministic methods** (no LLM) for speed and cost efficiency

Each data type has a tailored extraction strategy optimized for that format.

### 2.1 LOGS_AND_ERRORS: Crime Scene Extraction

**Includes**: Application logs, system logs, stack traces, error messages, alerts

**Unique Characteristic**: Time-ordered narrative of events with high signal around ERROR keywords

**Strategy**: Scan for ERROR keywords with severity prioritization, extract surrounding context (no LLM needed)

**LLM Calls**: 0

**Key Improvements**:
- Prioritize by severity (FATAL > CRITICAL > ERROR)
- Detect multiple crime scenes (extract first + last if multiple errors)
- Handle error bursts (expand window to cover clustered errors)
- Safety check (truncate if extracted snippet exceeds limits)

```python
class LogsAndErrorsExtractor:
    """Extract error "crime scene" from log files"""

    # Severity weights for error prioritization
    SEVERITY_WEIGHTS = {
        'FATAL': 100,
        'CRITICAL': 90,
        'panic': 90,
        'ERROR': 50,
        'WARN': 10,
    }

    MAX_SNIPPET_LINES = 500  # Safety limit

    def extract(self, content: str) -> ExtractedData:
        """
        Crime Scene Extraction:
        1. Scan for errors with severity prioritization
        2. Find highest-severity error (FATAL > CRITICAL > ERROR)
        3. Detect error bursts and multiple crime scenes
        4. Extract context window with adaptive sizing
        5. Safety check: truncate if output exceeds limits

        Returns:
            Snippet of ~400 lines with full context
        """

        lines = content.split('\n')

        # 1. Find all errors with severity tracking
        errors = self._find_all_errors_with_severity(lines)

        if not errors:
            # No errors found - extract tail (last 500 lines)
            return self._extract_tail(lines)

        # 2. Find highest-severity error
        primary_error = max(errors, key=lambda e: e['severity'])

        # 3. Check for multiple high-severity errors (multiple crime scenes)
        high_severity_errors = [
            e for e in errors
            if e['severity'] >= self.SEVERITY_WEIGHTS.get('ERROR', 50)
        ]

        if len(high_severity_errors) > 1:
            # Multiple crime scenes: Extract first + last
            return self._extract_multiple_crime_scenes(
                lines,
                high_severity_errors[0],
                high_severity_errors[-1]
            )

        # 4. Check for error burst around primary error
        burst_window = self._detect_error_burst(
            lines,
            primary_error['line_idx'],
            window=50
        )

        if burst_window:
            # Error burst detected - expand window to cover it
            return self._extract_burst_context(lines, burst_window, primary_error)
        else:
            # Single error - standard ±200 window
            return self._extract_single_error_context(lines, primary_error)

    def _find_all_errors_with_severity(self, lines: List[str]) -> List[dict]:
        """Find all errors and track their severity"""

        errors = []

        for idx, line in enumerate(lines):
            for pattern, severity in self.SEVERITY_WEIGHTS.items():
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append({
                        'line_idx': idx,
                        'severity': severity,
                        'pattern': pattern,
                        'line_content': line
                    })
                    break  # Only match highest severity per line

        return errors

    def _detect_error_burst(
        self,
        lines: List[str],
        error_idx: int,
        window: int = 50
    ) -> Optional[Tuple[int, int]]:
        """
        Detect error burst (multiple errors clustered together)

        Returns:
            (burst_start, burst_end) if burst detected, else None
        """

        # Check ±window lines for error density
        start = max(0, error_idx - window)
        end = min(len(lines), error_idx + window)

        burst_errors = []
        for idx in range(start, end):
            for pattern in self.SEVERITY_WEIGHTS.keys():
                if re.search(pattern, lines[idx], re.IGNORECASE):
                    burst_errors.append(idx)
                    break

        # If >10 errors in window, it's a burst
        if len(burst_errors) > 10:
            return (min(burst_errors), max(burst_errors))

        return None

    def _extract_single_error_context(
        self,
        lines: List[str],
        error: dict
    ) -> ExtractedData:
        """Extract ±200 lines around single error"""

        error_idx = error['line_idx']
        start = max(0, error_idx - 200)
        end = min(len(lines), error_idx + 200)

        snippet = lines[start:end]

        # Safety check: Truncate if too large
        snippet = self._truncate_if_needed(snippet, error_idx - start)

        return ExtractedData(
            summary=self._format_snippet(snippet, start, error_idx),
            method="crime_scene_extraction_single",
            original_size=len('\n'.join(lines)),
            extracted_size=len('\n'.join(snippet)),
            compression_ratio=len(snippet) / len(lines),
            insights={
                "error_line": error_idx,
                "severity": error['pattern'],
                "context_window": "±200 lines",
                "extraction_strategy": "single_error"
            }
        )

    def _extract_multiple_crime_scenes(
        self,
        lines: List[str],
        first_error: dict,
        last_error: dict
    ) -> ExtractedData:
        """
        Extract first + last crime scenes
        Captures error onset + current state
        """

        # Extract 100 lines around first error (onset)
        first_start = max(0, first_error['line_idx'] - 100)
        first_end = min(len(lines), first_error['line_idx'] + 100)
        first_snippet = lines[first_start:first_end]

        # Extract 100 lines around last error (current state)
        last_start = max(0, last_error['line_idx'] - 100)
        last_end = min(len(lines), last_error['line_idx'] + 100)
        last_snippet = lines[last_start:last_end]

        # Combine snippets with separator
        combined = (
            first_snippet +
            ["\n... [multiple errors occurred between crime scenes] ...\n"] +
            last_snippet
        )

        # Safety check
        combined = self._truncate_if_needed(combined, len(first_snippet))

        return ExtractedData(
            summary=self._format_multiple_snippets(
                first_snippet, last_snippet,
                first_start, last_start,
                first_error['line_idx'], last_error['line_idx']
            ),
            method="crime_scene_extraction_multiple",
            original_size=len('\n'.join(lines)),
            extracted_size=len('\n'.join(combined)),
            compression_ratio=len(combined) / len(lines),
            insights={
                "first_error_line": first_error['line_idx'],
                "last_error_line": last_error['line_idx'],
                "first_severity": first_error['pattern'],
                "last_severity": last_error['pattern'],
                "extraction_strategy": "multiple_crime_scenes"
            }
        )

    def _extract_burst_context(
        self,
        lines: List[str],
        burst_window: Tuple[int, int],
        primary_error: dict
    ) -> ExtractedData:
        """Extract full error burst + context"""

        burst_start, burst_end = burst_window

        # Add context before and after burst
        start = max(0, burst_start - 100)
        end = min(len(lines), burst_end + 100)

        snippet = lines[start:end]

        # Safety check
        snippet = self._truncate_if_needed(snippet, primary_error['line_idx'] - start)

        return ExtractedData(
            summary=self._format_snippet(snippet, start, primary_error['line_idx']),
            method="crime_scene_extraction_burst",
            original_size=len('\n'.join(lines)),
            extracted_size=len('\n'.join(snippet)),
            compression_ratio=len(snippet) / len(lines),
            insights={
                "primary_error_line": primary_error['line_idx'],
                "burst_start": burst_start,
                "burst_end": burst_end,
                "burst_size": burst_end - burst_start,
                "extraction_strategy": "error_burst"
            }
        )

    def _extract_tail(self, lines: List[str]) -> ExtractedData:
        """No errors found - extract tail (last 500 lines)"""

        tail = lines[-500:]

        return ExtractedData(
            summary=self._format_snippet(tail, len(lines) - 500, None),
            method="tail_extraction",
            original_size=len('\n'.join(lines)),
            extracted_size=len('\n'.join(tail)),
            compression_ratio=500 / len(lines),
            insights={
                "note": "No errors found, extracted tail",
                "lines_extracted": 500
            }
        )

    def _truncate_if_needed(
        self,
        snippet: List[str],
        error_offset: int
    ) -> List[str]:
        """
        Safety check: Truncate snippet if exceeds MAX_SNIPPET_LINES

        Strategy: Keep lines around error, truncate from middle
        """

        if len(snippet) <= self.MAX_SNIPPET_LINES:
            return snippet

        # Truncate from middle, keeping start and end around error
        keep_before = 200
        keep_after = 200

        return (
            snippet[:keep_before] +
            ["... [truncated for size] ..."] +
            snippet[-keep_after:]
        )

    def _format_snippet(
        self,
        lines: List[str],
        start_line: int,
        error_line: Optional[int]
    ) -> str:
        """Format extracted snippet for LLM"""

        header = f"""
LOG FILE EXCERPT
{'=' * 50}

Source: {filename}
Lines: {start_line + 1} - {start_line + len(lines)}
{f"ERROR at line {error_line + 1}" if error_line else "Tail extraction (no errors found)"}
{'=' * 50}

"""

        # Add line numbers
        numbered_lines = [
            f"{start_line + idx + 1:6d} | {line}"
            for idx, line in enumerate(lines)
        ]

        return header + '\n'.join(numbered_lines)

    def _format_multiple_snippets(
        self,
        first_snippet: List[str],
        last_snippet: List[str],
        first_start: int,
        last_start: int,
        first_error_line: int,
        last_error_line: int
    ) -> str:
        """Format multiple crime scenes for LLM"""

        header = f"""
LOG FILE EXCERPT (Multiple Crime Scenes)
{'=' * 50}

Source: {filename}
Strategy: Extracted first + last errors to show onset + current state

CRIME SCENE 1 (Error Onset)
Lines: {first_start + 1} - {first_start + len(first_snippet)}
ERROR at line {first_error_line + 1}
{'=' * 50}

"""

        # Format first crime scene
        first_numbered = [
            f"{first_start + idx + 1:6d} | {line}"
            for idx, line in enumerate(first_snippet)
        ]

        middle = f"""

{'=' * 50}
... [multiple errors occurred between crime scenes] ...
{'=' * 50}

CRIME SCENE 2 (Current State)
Lines: {last_start + 1} - {last_start + len(last_snippet)}
ERROR at line {last_error_line + 1}
{'=' * 50}

"""

        # Format last crime scene
        last_numbered = [
            f"{last_start + idx + 1:6d} | {line}"
            for idx, line in enumerate(last_snippet)
        ]

        return header + '\n'.join(first_numbered) + middle + '\n'.join(last_numbered)
```

**Output Example**:
```
LOG FILE EXCERPT
==================================================

Source: application.log
Lines: 12251 - 12651
ERROR at line 12450
==================================================

 12251 | 2025-10-14 14:22:58 INFO [ApiServer] Request received: GET /api/users
 12252 | 2025-10-14 14:22:59 INFO [Database] Query executed: SELECT * FROM users
 ...
 12450 | 2025-10-14 14:23:15 ERROR [DatabasePool] Connection timeout after 30s
 12451 | 2025-10-14 14:23:15 ERROR [DatabasePool] Failed to acquire connection (attempt 1/3)
 ...
 12651 | 2025-10-14 14:24:30 INFO [Operations] Manual intervention started
```

---

### 2.2 UNSTRUCTURED_TEXT: Direct Use or Chunking

**Includes**: User descriptions, Slack threads, documentation, incident reports, tickets, HTML pages (scraped)

**Unique Characteristic**: Prose-based, conversational text of variable length

**Strategy**: Size check → Direct use if small (<8K tokens), defer to Step 3 if large

**LLM Calls**: 0 (Step 3 handles chunking if needed)

```python
class UnstructuredTextExtractor:
    """Extract key information from free-form text"""

    SAFE_TOKEN_LIMIT = 8000  # ~32KB text

    def extract(self, content: str) -> ExtractedData:
        """
        Unstructured Text Processing (Step 2):
        1. Calculate token count
        2. If < 8K tokens: Mark for direct use
        3. If > 8K tokens: Mark for Step 3 chunking

        Note: No LLM calls in this step. Step 3 handles chunking.
        """

        token_count = self._estimate_tokens(content)

        if token_count <= self.SAFE_TOKEN_LIMIT:
            # Direct use - ready for diagnostic agent
            return ExtractedData(
                summary=content,
                method="direct_use",
                original_size=len(content),
                extracted_size=len(content),
                compression_ratio=1.0,
                needs_chunking=False
            )

        else:
            # Mark for Step 3 chunking
            return ExtractedData(
                summary=content,
                method="needs_chunking",
                original_size=len(content),
                extracted_size=len(content),
                needs_chunking=True  # Step 3 will handle this
            )

```

**Examples**:
- **Slack thread**: Direct use if short conversation, Step 3 chunking if long
- **Incident report**: Direct use if <8K tokens, Step 3 chunking if detailed
- **ServiceNow ticket**: Usually direct use (tickets are typically concise)
- **HTML page (file upload)**: Scrape HTML tags → Extract text → Check size

---

### 2.3 STRUCTURED_CONFIG: Parse + Sanitize

**Includes**: YAML, JSON, INI, TOML configuration files

**Unique Characteristic**: Dense, machine-readable blueprint where entire context is important

**Strategy**: Parse for validity, scan for secrets, pass full content (no compression)

**LLM Calls**: 0

```python
class StructuredConfigExtractor:
    """Parse and sanitize configuration files"""

    def extract(self, content: str, filename: str) -> ExtractedData:
        """
        Config File Processing:
        1. Detect format (YAML, JSON, INI, TOML)
        2. Parse to validate structure
        3. Scan for secrets (API keys, passwords)
        4. Pass full config to LLM (small files, every line matters)
        """

        # 1. Detect format
        ext = Path(filename).suffix.lower()
        config_format = self._detect_format(ext, content)

        # 2. Parse and validate
        try:
            parsed_config = self._parse_config(content, config_format)
        except Exception as e:
            return ExtractedData(
                summary=f"ERROR: Invalid {config_format} format: {e}",
                method="parse_error",
                original_size=len(content),
                extracted_size=0
            )

        # 3. Scan for secrets
        secrets_found = self._scan_for_secrets(content)

        # 4. Format for LLM (full content, small files)
        formatted = self._format_config(content, config_format, secrets_found)

        return ExtractedData(
            summary=formatted,
            method="parse_and_sanitize",
            original_size=len(content),
            extracted_size=len(formatted),
            compression_ratio=1.0,  # No compression (full config)
            insights={
                "format": config_format,
                "secrets_found": secrets_found,
                "valid": True
            }
        )

    def _scan_for_secrets(self, content: str) -> List[str]:
        """Detect hardcoded secrets"""

        patterns = [
            (r'password\s*[:=]\s*["\']?([^"\'\s]+)', 'password'),
            (r'api_key\s*[:=]\s*["\']?([^"\'\s]+)', 'api_key'),
            (r'sk-[a-zA-Z0-9]{32,}', 'openai_key'),
            (r'AKIA[0-9A-Z]{16}', 'aws_access_key'),
        ]

        found = []
        for pattern, secret_type in patterns:
            if re.search(pattern, content, re.IGNORECASE):
                found.append(secret_type)

        return found
```

**Examples**:
- **application.yaml**: Database connection, service config
- **docker-compose.json**: Container orchestration
- **.env file**: Environment variables

---

### 2.4 METRICS_AND_PERFORMANCE: Quantitative Analysis

**Includes**: Time-series metrics, distributed traces (OpenTelemetry), profiling data (flame graphs)

**Unique Characteristic**: Numerical data showing trends, anomalies, performance bottlenecks

**Strategy**: Backend script parses data (pandas), finds anomalies (z-score), generates natural language summary

**LLM Calls**: 0 (pure statistical analysis)

```python
class MetricsAnomalyExtractor:
    """Extract anomalies from time-series metrics"""

    def extract(self, content: str) -> ExtractedData:
        """
        Anomaly Detection:
        1. Parse CSV/JSON metrics
        2. Detect spikes, drops, flatlines using statistical methods
        3. Generate natural language summary

        No LLM needed - pure pandas/numpy
        """

        import pandas as pd
        from scipy import stats

        # 1. Parse metrics
        df = pd.read_csv(StringIO(content))

        # 2. Detect anomalies (z-score method)
        anomalies = []

        for column in df.select_dtypes(include=[np.number]).columns:
            z_scores = np.abs(stats.zscore(df[column].dropna()))
            anomaly_indices = np.where(z_scores > 3)[0]

            for idx in anomaly_indices:
                anomalies.append({
                    "timestamp": df.iloc[idx]["timestamp"],
                    "metric": column,
                    "value": df.iloc[idx][column],
                    "z_score": z_scores[idx],
                    "type": "spike" if df.iloc[idx][column] > df[column].mean() else "drop"
                })

        # 3. Generate natural language summary
        summary = self._format_anomaly_summary(df, anomalies)

        return ExtractedData(
            summary=summary,
            method="anomaly_detection",
            original_size=len(content),
            extracted_size=len(summary),
            compression_ratio=len(summary) / len(content),
            insights={
                "anomalies_detected": len(anomalies),
                "time_range": f"{df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}",
                "metrics_analyzed": list(df.columns)
            }
        )

    def _format_anomaly_summary(
        self,
        df: pd.DataFrame,
        anomalies: List[dict]
    ) -> str:
        """Format anomalies as natural language"""

        summary_parts = [
            "METRICS ANALYSIS",
            "=" * 50,
            "",
            f"Time Range: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}",
            f"Total Data Points: {len(df):,}",
            f"Anomalies Detected: {len(anomalies)}",
            "",
        ]

        if anomalies:
            summary_parts.append("ANOMALIES:")
            summary_parts.append("")

            for anomaly in anomalies[:10]:  # Top 10
                summary_parts.append(
                    f"• {anomaly['timestamp']}: {anomaly['metric']} "
                    f"{anomaly['type']} to {anomaly['value']} "
                    f"(z-score: {anomaly['z_score']:.2f})"
                )

        else:
            summary_parts.append("No significant anomalies detected.")

        return '\n'.join(summary_parts)
```

**Output Example**:
```
METRICS ANALYSIS
==================================================

Time Range: 2025-10-14 10:00:00 to 2025-10-14 16:30:00
Total Data Points: 23,400
Anomalies Detected: 3

ANOMALIES:

• 2025-10-14 14:23:15: cpu_usage spike to 98.5% (z-score: 4.2)
• 2025-10-14 14:23:20: api_success_rate drop to 12% (z-score: 3.8)
• 2025-10-14 14:25:00: memory_usage spike to 94.2% (z-score: 3.5)

CORRELATION: CPU spike correlates with API failure rate drop (14:23:15)
```

---

### 2.5 SOURCE_CODE: Code-Aware Snippet Extraction

**Includes**: Python, Java, JavaScript, Go, C++, etc.

**Unique Characteristic**: Structured, syntactical representation of executable logic

**Strategy**: Parse AST, extract functions/classes mentioned in error traces

**LLM Calls**: 0 (AST parsing)

```python
class SourceCodeExtractor:
    """Extract relevant code blocks based on error context"""

    def extract(
        self,
        content: str,
        filename: str,
        error_context: Optional[str] = None
    ) -> ExtractedData:
        """
        Source Code Processing:
        1. Detect programming language
        2. If error context provided: Search for mentioned functions/classes
        3. Extract relevant code blocks (functions, classes, modules)
        4. Include surrounding context for understanding
        """

        # 1. Detect language
        language = self._detect_language(filename, content)

        # 2. Parse code structure
        code_structure = self._parse_code_structure(content, language)

        # 3. Extract relevant blocks
        if error_context:
            # Extract functions/classes mentioned in error
            relevant_blocks = self._extract_error_related_blocks(
                code_structure,
                error_context
            )
        else:
            # No error context - extract entry points and recent changes
            relevant_blocks = self._extract_key_blocks(code_structure)

        # 4. Format with context
        formatted = self._format_code_blocks(relevant_blocks, language)

        return ExtractedData(
            summary=formatted,
            method="code_aware_extraction",
            original_size=len(content),
            extracted_size=len(formatted),
            insights={
                "language": language,
                "blocks_extracted": len(relevant_blocks),
                "error_driven": error_context is not None
            }
        )

    def _extract_error_related_blocks(
        self,
        code_structure: dict,
        error_context: str
    ) -> List[CodeBlock]:
        """
        Extract code blocks mentioned in error message

        Example error: "NameError: name 'calculate_total' is not defined"
        → Extract calculate_total function and its dependencies
        """

        # Find function/class names in error
        mentioned_symbols = self._extract_symbols_from_error(error_context)

        relevant_blocks = []
        for symbol in mentioned_symbols:
            # Find definition
            block = code_structure.get(symbol)
            if block:
                relevant_blocks.append(block)

                # Include dependencies
                dependencies = block.get('calls', [])
                for dep in dependencies:
                    dep_block = code_structure.get(dep)
                    if dep_block:
                        relevant_blocks.append(dep_block)

        return relevant_blocks

    def _format_code_blocks(
        self,
        blocks: List[CodeBlock],
        language: str
    ) -> str:
        """Format code blocks for LLM"""

        parts = [
            "SOURCE CODE ANALYSIS",
            "=" * 60,
            "",
            f"Language: {language}",
            f"Blocks Extracted: {len(blocks)}",
            "",
        ]

        for block in blocks:
            parts.append(f"[{block.type.upper()}: {block.name}]")
            parts.append(f"Lines {block.start_line}-{block.end_line}")
            parts.append("")
            parts.append(block.code)
            parts.append("")
            parts.append("-" * 60)
            parts.append("")

        return '\n'.join(parts)
```

**Examples**:
- **Error trace mentions `process_payment()`**: Extract `process_payment` function + dependencies
- **Stack trace shows `UserService.authenticate()`**: Extract class + method
- **No error context**: Extract entry points (main, routes, handlers)

---

### 2.6 VISUAL_EVIDENCE: Vision Model Transcription

**Includes**: Screenshots, UI captures, browser console images, dashboard screenshots

**Unique Characteristic**: Graphical data requiring vision model processing

**Strategy**: Send image to vision LLM (Gemini, GPT-4V) for textual description

**LLM Calls**: 1 (vision model)

```python
class ScreenshotTranscriber:
    """Transcribe screenshots using vision model"""

    def __init__(self, vision_llm: IVisionLLM):
        self.vision_llm = vision_llm

    async def extract(self, image_bytes: bytes) -> ExtractedData:
        """
        Screenshot Transcription:
        1. Send image to vision model (Gemini, GPT-4V)
        2. Prompt: "Describe from troubleshooting perspective"
        3. LLM returns text description
        """

        prompt = """
Describe this screenshot from a software troubleshooting perspective.

Focus on:
1. Any visible error messages (transcribe exactly)
2. UI elements (buttons, fields, labels)
3. State indicators (loading, error icons, warnings)
4. Relevant text content

Provide a concise, structured description.
"""

        response = await self.vision_llm.analyze_image(
            image=image_bytes,
            prompt=prompt
        )

        return ExtractedData(
            summary=response.text,
            method="vision_model_transcription",
            original_size=len(image_bytes),
            extracted_size=len(response.text),
            insights={
                "vision_model": "gemini-1.5-pro",
                "confidence": response.confidence
            }
        )
```

---

## Step 3: Chunking Fallback (Long Unstructured Text Only)

**Purpose**: Handle long documents that exceed safe token limit

**Applies to**: DOCUMENTATION, SLACK_MESSAGES, UNSTRUCTURED_TEXT (when >8K tokens)

**When triggered**: Only when Step 2 marks content as `needs_chunking=True`

**LLM Calls**: N+1 (N chunks + 1 synthesis)

### 3.1 Workflow

```python
class ChunkingService:
    """Handle long unstructured text via map-reduce"""

    CHUNK_SIZE_TOKENS = 4000
    MAX_PARALLEL_CHUNKS = 5

    async def process_long_text(self, content: str) -> str:
        """
        Breadth-based Scan (map-reduce):
        1. Split into intelligent chunks (on paragraphs/newlines)
        2. Summarize each chunk in parallel (map) - N LLM calls
        3. Synthesize summaries (reduce) - 1 LLM call

        Total LLM calls: N + 1
        """

        # 1. Split intelligently
        chunks = self._split_on_paragraphs(content, self.CHUNK_SIZE_TOKENS)

        # 2. Map: Parallel chunk summarization
        chunk_summaries = await asyncio.gather(*[
            self._summarize_chunk(chunk, idx)
            for idx, chunk in enumerate(chunks)
        ])

        # 3. Reduce: Synthesize final summary
        final_summary = await self._synthesize_summaries(chunk_summaries)

        return final_summary

    async def _summarize_chunk(self, chunk: str, index: int) -> str:
        """Summarize a single chunk (1 LLM call)"""

        prompt = f"""
Summarize this section (part {index + 1}) of a longer document.
Extract key facts, timeline events, actions, and problems mentioned.

{chunk}
"""

        response = await self.llm.complete(prompt)
        return response.text

    async def _synthesize_summaries(self, summaries: List[str]) -> str:
        """Synthesize chunk summaries into final summary (1 LLM call)"""

        combined = "\n\n".join([
            f"Section {i+1}:\n{summary}"
            for i, summary in enumerate(summaries)
        ])

        prompt = f"""
Synthesize these section summaries into a coherent, concise summary.
Focus on the overall narrative, key problems, and important details.

{combined}
"""

        response = await self.llm.complete(prompt)
        return response.text
```

### 3.2 When This Step is Skipped

**Most files skip Step 3** because:
- **LOGS_AND_ERRORS**: Already extracted to ~400 lines in Step 2
- **METRICS_AND_PERFORMANCE**: Already summarized to anomaly report in Step 2
- **STRUCTURED_CONFIG**: Small files (<8K tokens typically)
- **SOURCE_CODE**: Targeted extraction, not full file
- **VISUAL_EVIDENCE**: Vision model output is already text
- **UNSTRUCTURED_TEXT (short)**: <8K tokens, marked for direct use

**Only long UNSTRUCTURED_TEXT triggers Step 3**:
- Long Slack threads (100+ messages)
- Detailed incident reports (10+ pages)
- Large documentation files
- Lengthy HTML pages (after scraping)

---

## Step 4: Final Diagnostic Call

**Purpose**: Send processed data to FaultMaven diagnostic agent

**LLM Calls**: 1 (diagnostic agent)

### 4.1 PII/Secret Sanitization

**Configurable based on LLM provider** (skip for local LLMs):

```python
class DataSanitizer:
    """Redact PII and secrets from extracted data"""

    def sanitize(
        self,
        content: str,
        provider: LLMProvider
    ) -> str:
        """
        Conditional sanitization:
        - Local LLM: Skip (no privacy risk)
        - External LLM (OpenAI, Anthropic): Redact
        """

        if provider == LLMProvider.LOCAL:
            return content  # No sanitization needed

        # Redact PII patterns
        content = self._redact_emails(content)
        content = self._redact_phone_numbers(content)
        content = self._redact_ip_addresses(content)
        content = self._redact_api_keys(content)
        content = self._redact_passwords(content)

        return content
```

### 4.2 Case File Packaging

**Structure extracted data for diagnostic LLM**:

```python
class CaseFilePackager:
    """Package sanitized data into structured case file"""

    def package(
        self,
        extracted_data: ExtractedData,
        metadata: FileMetadata
    ) -> CaseFile:
        """
        Create case file ready for diagnostic LLM

        Structure:
        - Metadata header (filename, size, type, timestamp)
        - Extracted content (sanitized)
        - Processing insights (method used, compression ratio)
        """

        case_file = f"""
CASE FILE
{'=' * 60}

METADATA:
  Filename: {metadata.filename}
  Size: {metadata.size:,} bytes
  Type: {metadata.data_type}
  Uploaded: {metadata.timestamp}

PROCESSING:
  Method: {extracted_data.method}
  Original Size: {extracted_data.original_size:,} bytes
  Extracted Size: {extracted_data.extracted_size:,} bytes
  Compression: {extracted_data.compression_ratio:.1%}

{'=' * 60}

{extracted_data.summary}
"""

        return CaseFile(
            content=case_file,
            metadata=metadata,
            insights=extracted_data.insights
        )
```

---

## Data Type Specifications

### Summary Table

| Data Type | Preprocessing Strategy | LLM Used? | Typical Reduction | Speed |
|-----------|------------------------|-----------|-------------------|-------|
| **LOGS_AND_ERRORS** | Crime Scene Extraction (±200 lines around ERROR) | ❌ No | 400 lines from 50K = 99.2% | ⚡ Fast |
| **UNSTRUCTURED_TEXT** | Direct use if <8K tokens, else chunk+summarize | ✅ Conditional | 0% (small) or 90%+ (large) | ⚡/🐢 Mixed |
| **STRUCTURED_CONFIG** | Parse + sanitize, pass full content | ❌ No | 0% (no compression) | ⚡ Fast |
| **METRICS_AND_PERFORMANCE** | Anomaly detection (pandas/numpy) | ❌ No | Summary from 100K rows = 99.9% | ⚡ Fast |
| **SOURCE_CODE** | Extract functions/classes from error trace | ❌ No (AST parsing) | 70-95% (targeted extraction) | ⚡ Fast |
| **VISUAL_EVIDENCE** | Vision model transcription | ✅ Yes | Text from image = 100% | 🐢 Slow |

### Examples by Category

| Data Type | Examples |
|-----------|----------|
| **LOGS_AND_ERRORS** | Application logs, system logs, stack traces, Python tracebacks, Java exceptions, Go panics, alert messages |
| **UNSTRUCTURED_TEXT** | User descriptions, Slack threads, incident reports, ServiceNow tickets, HTML pages (scraped), Markdown docs, README files |
| **STRUCTURED_CONFIG** | YAML configs, JSON configs, INI files, TOML files, .env files, docker-compose.yml, application.properties |
| **METRICS_AND_PERFORMANCE** | CSV time-series (Prometheus/Grafana exports), OpenTelemetry traces, profiling data (flame graphs), resource usage logs |
| **SOURCE_CODE** | Python (.py), JavaScript (.js), Java (.java), Go (.go), TypeScript (.ts), C/C++ (.c/.cpp), Ruby (.rb) |
| **VISUAL_EVIDENCE** | Screenshots (.png/.jpg), UI error captures, browser console screenshots, dashboard screenshots |

---

## Configuration

### Environment Variables

```bash
# ============================================================
# INGESTION & VALIDATION
# ============================================================

MAX_FILE_SIZE_BYTES=2097152           # 2 MB hard limit

BLOCKED_EXTENSIONS=.exe,.dll,.zip,.pdf,.bin
ALLOWED_MIME_TYPES=text/plain,text/csv,text/html,application/json,application/yaml,image/png,image/jpeg

# ============================================================
# DATA CLASSIFICATION (6-Class System)
# ============================================================

# LOGS_AND_ERRORS: Chronological event-based text
LOGS_ERROR_KEYWORDS=ERROR,Exception,FATAL,panic,CRITICAL,Traceback,ALERT

# Severity weights for error prioritization
LOGS_SEVERITY_FATAL=100
LOGS_SEVERITY_CRITICAL=90
LOGS_SEVERITY_PANIC=90
LOGS_SEVERITY_ERROR=50
LOGS_SEVERITY_WARN=10

# UNSTRUCTURED_TEXT: Free-form human text
UNSTRUCTURED_SAFE_TOKEN_LIMIT=8000    # Direct use if below this

# STRUCTURED_CONFIG: Declarative system state
CONFIG_EXTENSIONS=.yaml,.yml,.json,.toml,.ini,.env
CONFIG_VALIDATE_ON_PARSE=true         # Validate structure

# METRICS_AND_PERFORMANCE: Quantitative data
METRICS_ANOMALY_Z_SCORE_THRESHOLD=3.0 # Standard deviations for anomaly detection

# SOURCE_CODE: Executable logic
CODE_EXTENSIONS=.py,.js,.java,.go,.cpp,.c,.rb,.php,.ts,.jsx,.tsx
CODE_EXTRACT_DEPENDENCIES=true        # Include function dependencies

# VISUAL_EVIDENCE: Graphical snapshots
IMAGE_EXTENSIONS=.png,.jpg,.jpeg,.gif,.bmp

# ============================================================
# TIER 1: FAST PATH (Crime Scene Extraction)
# ============================================================

# LOGS_AND_ERRORS extraction
CRIME_SCENE_CONTEXT_LINES=200         # Lines before/after single error
TAIL_EXTRACTION_LINES=500             # Lines if no errors found
CRIME_SCENE_MAX_SNIPPET_LINES=500     # Safety limit for extracted snippet
ERROR_BURST_DETECTION_WINDOW=50       # Lines to check for error clustering
ERROR_BURST_THRESHOLD=10              # Min errors in window to trigger burst mode
MULTIPLE_CRIME_SCENES_LINES=100       # Lines around each error (first + last)

# ============================================================
# TIER 2: DEEP SCAN (User-Triggered Fallback)
# ============================================================

DEEP_SCAN_ENABLED=true                # Enable Tier 2 fallback
DEEP_SCAN_USER_TRIGGERED=true         # Require user opt-in

# UNSTRUCTURED_TEXT chunking
CHUNK_SIZE_TOKENS=4000                # Chunk size for map-reduce
MAP_REDUCE_MAX_PARALLEL=5             # Parallel LLM calls

# ============================================================
# SANITIZATION
# ============================================================

SANITIZE_PII=true                     # Redact PII
AUTO_SANITIZE_BASED_ON_PROVIDER=true  # Skip for LOCAL provider

# PII patterns to redact
PII_REDACT_EMAILS=true
PII_REDACT_PHONE_NUMBERS=true
PII_REDACT_IP_ADDRESSES=false         # Keep IPs for troubleshooting
PII_REDACT_API_KEYS=true
PII_REDACT_PASSWORDS=true

# ============================================================
# VISION MODEL (VISUAL_EVIDENCE)
# ============================================================

VISION_MODEL_PROVIDER=openai          # openai, anthropic, google
VISION_MODEL_NAME=gpt-4-vision-preview
VISION_MODEL_MAX_TOKENS=500           # Transcription max tokens

# ============================================================
# PERFORMANCE
# ============================================================

CLASSIFICATION_SAMPLE_SIZE=5000       # Bytes to sample for classification
ENABLE_EXTRACTION_CACHING=true        # Cache extraction results
EXTRACTION_TIMEOUT_SECONDS=30         # Timeout per file
```

### Data Type Enum

```python
from enum import Enum

class DataType(str, Enum):
    """6 purpose-driven data classifications"""

    LOGS_AND_ERRORS = "logs_and_errors"
    UNSTRUCTURED_TEXT = "unstructured_text"
    STRUCTURED_CONFIG = "structured_config"
    METRICS_AND_PERFORMANCE = "metrics_and_performance"
    SOURCE_CODE = "source_code"
    VISUAL_EVIDENCE = "visual_evidence"
```

---

## Status

✅ **Steps 1-4 fully defined and enhanced**

### Recent Improvements (2025-10-15)

1. **Severity-Based Error Selection**: Crime Scene extraction now prioritizes FATAL > CRITICAL > ERROR
2. **Multiple Crime Scenes Detection**: Extracts first + last errors to capture onset + current state
3. **Error Burst Handling**: Detects error clustering and adapts extraction window
4. **Safety Checks**: Post-extraction size limits with intelligent truncation

### Implementation Readiness

- ✅ All 6 data types defined with unique strategies
- ✅ 4-step sequential pipeline documented
- ✅ LLM call optimization (80%+ files = 1 call total)
- ✅ Edge cases handled (bursts, multiple errors, oversized output)
- ✅ Configuration parameters defined

**Status**: Ready for implementation
