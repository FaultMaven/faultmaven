# Data Preprocessing System - Complete Design Specification

**Document Type**: Authoritative Design Blueprint
**Status**: ✅ Final Design - Ready for Implementation
**Version**: 4.0
**Last Updated**: 2025-10-13

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [System Context](#system-context)
3. [Architecture Overview](#architecture-overview)
4. [Data Type Taxonomy](#data-type-taxonomy)
5. [Preprocessing Pipeline](#preprocessing-pipeline)
6. [Data Models](#data-models)
7. [Preprocessor Specifications](#preprocessor-specifications)
8. [LLM Integration](#llm-integration)
9. [Security & Privacy](#security--privacy)
10. [Implementation Roadmap](#implementation-roadmap)
11. [Dependencies](#dependencies)

---

## Executive Summary

### Purpose

Define the **complete data preprocessing system** for FaultMaven that transforms uploaded data (logs, metrics, traces, configs) into LLM-ready summaries for AI-powered troubleshooting analysis.

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **3-Step Pipeline** | Classify → Preprocess → LLM Analysis (simple, clear boundaries) |
| **8 Data Types** | LOG_FILE, ERROR_REPORT, CONFIG_FILE, METRICS_DATA, PROFILING_DATA, TRACE_DATA, DOCUMENTATION, SCREENSHOT |
| **Plain Text Summaries** | Max information density, LLM-friendly, no token overhead from markup |
| **Direct Context Injection** | Lowest latency, simplest approach, no additional infrastructure |
| **Custom Preprocessors** | Full control over output, domain-specific knowledge extraction, no external tools |
| **String Summary Format** | 5-8K characters per data type, human-readable, structured plain text |

### Design Philosophy

1. **Data as Case Context**: Uploaded data is part of a troubleshooting case, providing evidence for AI analysis
2. **Conversational UX**: Data uploads appear as conversation turns with AI responses
3. **Privacy-First**: All data sanitized through PII redaction before LLM processing
4. **LLM-Optimized**: Large files condensed to ~8K char summaries preserving critical information
5. **Domain-Specific**: Each data type has specialized preprocessing logic

---

## System Context

### Relationship to Case Architecture

```
Case (Troubleshooting Session)
├── Queries (User questions)
│   └── AgentResponse (AI answers)
└── Data (Evidence files)
    ├── Upload → Classification → Preprocessing
    └── Preprocessed Summary → LLM Analysis → AgentResponse
```

**Key Insight**: Data is **evidence** in a case, not standalone. The preprocessing system prepares this evidence for the AI agent to analyze in context of the troubleshooting conversation.

### Data Submission Paths

#### Path 1: Explicit Upload
- **Endpoint**: `POST /api/v1/cases/{case_id}/data`
- **Use Case**: User uploads file via dedicated UI
- **Flow**: Upload → Classify → Preprocess → LLM Analysis → AgentResponse

#### Path 2: Implicit Detection
- **Endpoint**: `POST /api/v1/cases/{case_id}/queries`
- **Use Case**: User pastes large data (>10K chars) into query box
- **Flow**: Auto-detect → Route to Path 1 pipeline

### Integration Points

```
┌─────────────────────────────────────────────────────────────┐
│                    DATA FLOW ARCHITECTURE                    │
└─────────────────────────────────────────────────────────────┘

Browser Extension
      │
      ├─ Upload File/Text/Page
      │
      ↓
API Endpoint (case.py)
      │
      ├─ Classify Data Type ────────────┐
      │                                   │
      ↓                                   ↓
PreprocessingService              DataClassifier
      │                          (classifier.py)
      ├─ Route by DataType               │
      │                                   │
      ↓                                   │
Preprocessor (domain-specific)           │
      │                                   │
      ├─ Extract Key Info                │
      ├─ Generate Summary (8K chars)     │
      ├─ Security Scan                   │
      │                                   │
      ↓                                   ↓
PreprocessedData ─────────────────> DataSanitizer
      │                              (redaction.py)
      ↓
AgentService (agent_service.py)
      │
      ├─ Build Prompt with Summary
      │
      ↓
LLM Router → OpenAI/Anthropic/Fireworks
      │
      ↓
AgentResponse (conversational AI analysis)
      │
      ↓
Frontend (displays in conversation)
```

---

## Architecture Overview

### 3-Step Pipeline

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   STEP 1:    │ →  │   STEP 2:    │ →  │   STEP 3:    │
│   Classify   │    │  Preprocess  │    │ LLM Analysis │
│              │    │              │    │              │
│ DataType     │    │ LLM-ready    │    │ AgentResponse│
│ enum         │    │ summary      │    │ with insights│
└──────────────┘    └──────────────┘    └──────────────┘
      ↓                     ↓                    ↓
 LOG_FILE            8K summary          "I found 127 errors,
 METRICS_DATA        (plain text)         the main issue is..."
 ERROR_REPORT
```

### Step 1: Classification ✅ IMPLEMENTED

**Component**: `DataClassifier` ([faultmaven/core/processing/classifier.py](../../faultmaven/core/processing/classifier.py))

**Input**: Raw file content + filename hint

**Process**:
```python
data_type = await classifier.classify(content, filename)
```

**Output**: `DataType` enum value

**Classification Methods**:
1. **Filename extension** (`.log`, `.yaml`, `.csv`, etc.)
2. **Pattern matching** (timestamps, log levels, stack traces, etc.)
3. **Structural validation** (JSON parsing, YAML parsing, etc.)
4. **LLM fallback** (if confidence < 0.5)

**Status**: ✅ Fully implemented with weighted pattern matching

### Step 2: Preprocessing ⚠️ TO BE IMPLEMENTED

**Component**: `PreprocessingService` (NEW - to be created)

**Input**: Raw content (potentially 50KB+)

**Output**: `PreprocessedData` with LLM-ready summary (~8K chars)

**Architecture**:
```python
class PreprocessingService:
    """Orchestrates data preprocessing pipeline"""

    def __init__(self):
        self.classifier = DataClassifier()
        self.sanitizer = DataSanitizer()
        self.preprocessors = {
            DataType.LOG_FILE: LogPreprocessor(),
            DataType.ERROR_REPORT: ErrorPreprocessor(),
            DataType.CONFIG_FILE: ConfigPreprocessor(),
            DataType.METRICS_DATA: MetricsPreprocessor(),
            DataType.PROFILING_DATA: ProfilingPreprocessor(),
            DataType.TRACE_DATA: TracePreprocessor(),
            DataType.DOCUMENTATION: DocumentationPreprocessor(),
            DataType.SCREENSHOT: ScreenshotPreprocessor(),
        }

    async def preprocess(
        self,
        content: str,
        filename: Optional[str] = None,
        case_id: Optional[str] = None,
        session_id: Optional[str] = None,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Main preprocessing pipeline entry point

        1. Classify data type
        2. Route to appropriate preprocessor
        3. Sanitize output
        4. Return PreprocessedData with LLM-ready summary
        """
```

### Step 3: LLM Analysis ✅ READY

**Component**: `AgentService` ([faultmaven/services/agentic/orchestration/agent_service.py](../../faultmaven/services/agentic/orchestration/agent_service.py))

**Input**: `PreprocessedData.summary` (not raw data)

**Process**:
```python
query_request = QueryRequest(
    session_id=session_id,
    query=f"Analyze uploaded file: {filename}",
    context={
        "case_id": case_id,
        "preprocessed_data": preprocessed.summary,  # 8K summary
        "data_type": preprocessed.data_type.value,
        "security_flags": preprocessed.security_flags
    }
)

agent_response = await agent_service.process_query_for_case(
    case_id, query_request
)
```

**Output**: `AgentResponse` with conversational AI analysis

**Status**: ✅ Ready (just needs preprocessed input from Step 2)

---

## Data Type Taxonomy

### Enum Definition

**Location**: [faultmaven/models/api.py](../../faultmaven/models/api.py:50-58)

**Current State**: Missing 3 types (METRICS_DATA, PROFILING_DATA, TRACE_DATA)

**Required Definition**:
```python
class DataType(str, Enum):
    """Data types for classification and preprocessing routing"""

    # Core types (existing) ✅
    LOG_FILE = "log_file"                # Application/system logs
    CONFIG_FILE = "config_file"          # YAML, JSON, INI, TOML configs
    ERROR_REPORT = "error_report"        # Stack traces, exceptions
    DOCUMENTATION = "documentation"       # Markdown, HTML, RST docs
    SCREENSHOT = "screenshot"             # PNG, JPEG images

    # Performance data types (ADD THESE) ➕
    METRICS_DATA = "metrics_data"        # Time-series metrics, CSV
    PROFILING_DATA = "profiling_data"    # cProfile, flame graphs
    TRACE_DATA = "trace_data"            # OpenTelemetry, Jaeger traces

    # Catch-all
    OTHER = "other"
```

### Data Type Specifications

| Data Type | Description | Common Formats | Preprocessing Complexity | Priority |
|-----------|-------------|----------------|-------------------------|----------|
| **LOG_FILE** | Application/system logs | `.log`, `.txt`, syslog | Medium | P1 (Most common) |
| **ERROR_REPORT** | Exceptions, crashes | Stack traces, error dumps | Low | P1 (High value) |
| **CONFIG_FILE** | Configuration files | YAML, JSON, INI, TOML, ENV | Medium | P2 (Security value) |
| **METRICS_DATA** | Time-series metrics | CSV, Prometheus, InfluxDB | High | P2 (Performance analysis) |
| **PROFILING_DATA** | Code execution profiles | cProfile, py-spy, flame | High | P3 (Optimization) |
| **TRACE_DATA** | Distributed traces | OpenTelemetry, Jaeger | High | P3 (Microservices) |
| **DOCUMENTATION** | Documentation files | Markdown, HTML, RST | Low | P4 (Context) |
| **SCREENSHOT** | Images/screenshots | PNG, JPEG, GIF | High | P5 (Needs vision LLM) |

---

## Preprocessing Pipeline

### Pipeline Interface

```python
# Base interface for all preprocessors
class IPreprocessor(ABC):
    """Interface for data type-specific preprocessors"""

    @abstractmethod
    async def process(
        self,
        content: str,
        filename: str,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Process raw content into LLM-ready summary

        Returns:
            PreprocessedData with summary field (~8K chars max)
        """
        pass
```

### PreprocessingService Orchestrator

**Location**: Create new file `faultmaven/services/preprocessing/preprocessing_service.py`

```python
from typing import Optional, Dict, Any
from faultmaven.models.api import DataType, PreprocessedData, SourceMetadata
from faultmaven.core.processing.classifier import DataClassifier
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.services.preprocessing.preprocessors import (
    LogPreprocessor,
    ErrorPreprocessor,
    ConfigPreprocessor,
    MetricsPreprocessor,
    # ... other preprocessors
)


class PreprocessingService:
    """
    Orchestrates data preprocessing pipeline

    Routes data to appropriate preprocessors based on DataType,
    returns unified PreprocessedData format for LLM consumption.
    """

    def __init__(self):
        self.classifier = DataClassifier()
        self.sanitizer = DataSanitizer()

        # Preprocessor registry
        self.preprocessors: Dict[DataType, IPreprocessor] = {
            DataType.LOG_FILE: LogPreprocessor(),
            DataType.ERROR_REPORT: ErrorPreprocessor(),
            DataType.CONFIG_FILE: ConfigPreprocessor(),
            DataType.METRICS_DATA: MetricsPreprocessor(),
            DataType.PROFILING_DATA: ProfilingPreprocessor(),
            DataType.TRACE_DATA: TracePreprocessor(),
            DataType.DOCUMENTATION: DocumentationPreprocessor(),
            DataType.SCREENSHOT: ScreenshotPreprocessor(),
        }

        # Fallback for OTHER type
        self.default_preprocessor = GenericPreprocessor()

    async def preprocess(
        self,
        content: str,
        filename: Optional[str] = None,
        case_id: Optional[str] = None,
        session_id: Optional[str] = None,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Main preprocessing pipeline

        1. Classify data type
        2. Route to appropriate preprocessor
        3. Sanitize output
        4. Return LLM-ready PreprocessedData
        """
        # Step 1: Classify
        data_type = await self.classifier.classify(content, filename)

        # Step 2: Route and preprocess
        preprocessor = self.preprocessors.get(
            data_type,
            self.default_preprocessor
        )

        preprocessed = await preprocessor.process(
            content=content,
            filename=filename or "unknown",
            source_metadata=source_metadata
        )

        # Step 3: Sanitize (remove PII, secrets)
        preprocessed.summary = self.sanitizer.sanitize(preprocessed.summary)

        # Add context metadata
        preprocessed.data_type = data_type
        if source_metadata:
            preprocessed.source_metadata = source_metadata.dict()

        return preprocessed
```

---

## Data Models

### PreprocessedData Model

**Location**: Add to [faultmaven/models/api.py](../../faultmaven/models/api.py) after line 428

```python
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime


class PreprocessedData(BaseModel):
    """
    Output from preprocessing pipeline - LLM-ready format

    This is the bridge between raw uploaded data and LLM analysis.
    The 'summary' field contains the condensed, structured representation
    of the original data, optimized for LLM context consumption.
    """

    # Identifiers
    data_id: str = Field(..., description="Unique data identifier")
    data_type: DataType = Field(..., description="Classified data type")

    # LLM-ready content (THIS IS THE KEY OUTPUT)
    summary: str = Field(
        ...,
        description="LLM-ready summary (5-8K chars max, plain text)",
        max_length=10000  # Hard limit for safety
    )

    # Metadata
    original_size: int = Field(..., description="Original content size in bytes")
    summary_size: int = Field(..., description="Summary size in characters")
    compression_ratio: float = Field(
        ...,
        description="Compression ratio (original_size / summary_size)"
    )

    # Processing info
    processed_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When preprocessing completed"
    )
    processing_time_ms: float = Field(
        ...,
        description="Time taken for preprocessing in milliseconds"
    )

    # Structured insights (for reference, not sent to LLM)
    insights: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured data extracted during preprocessing"
    )

    # Security flags
    security_flags: List[str] = Field(
        default_factory=list,
        description="Security issues detected (pii_detected, secrets_found, etc.)"
    )

    # Source information (optional)
    source_metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Metadata about data source (file, text paste, page capture)"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "data_id": "data_abc123",
                "data_type": "log_file",
                "summary": "LOG FILE ANALYSIS\n\nOVERVIEW:\nTotal: 45,234 entries...",
                "original_size": 45000,
                "summary_size": 7800,
                "compression_ratio": 5.77,
                "processed_at": "2025-10-13T10:30:00Z",
                "processing_time_ms": 234.5,
                "insights": {"error_count": 127, "anomalies": 3},
                "security_flags": ["api_key_detected"],
                "source_metadata": {"source_type": "file_upload"}
            }
        }


from typing import Literal, Optional
from pydantic import BaseModel, Field


class SourceMetadata(BaseModel):
    """
    Metadata about where the data originated

    Used to enhance preprocessing and LLM prompts with context
    about the data source (e.g., "the status page you captured from...")
    """
    source_type: Literal["file_upload", "text_paste", "page_capture"]
    source_url: Optional[str] = Field(
        None,
        description="URL if from page capture"
    )
    captured_at: Optional[str] = Field(
        None,
        description="ISO 8601 timestamp if from page capture"
    )
    user_description: Optional[str] = Field(
        None,
        description="User's description of the data"
    )
```

### DataUploadResponse Enhancement

**Location**: Update existing model in [faultmaven/models/api.py](../../faultmaven/models/api.py)

```python
class DataUploadResponse(BaseModel):
    """Response payload for data upload"""
    schema_version: Literal["3.1.0"] = "3.1.0"  # API schema version (not document version)
    data_id: str
    filename: str = Field(..., description="Uploaded filename")
    file_size: int = Field(..., description="File size in bytes")
    data_type: str = Field(..., description="Classified data type")
    processing_status: ProcessingStatus
    uploaded_at: str = Field(..., description="Upload timestamp (ISO 8601)")

    # NEW: AI analysis response
    agent_response: Optional[AgentResponse] = Field(
        None,
        description="Conversational AI analysis of the uploaded data"
    )

    # Classification metadata
    classification: Optional[Dict[str, Any]] = Field(
        None,
        description="Classification confidence and metadata"
    )
```

---

## Preprocessor Specifications

### 1. LogPreprocessor (LOG_FILE)

**Priority**: P1 (Highest - most common data type)

**Status**: ⚠️ Partial (LogProcessor exists, needs formatting layer)

**Libraries**: None needed (reuse existing LogProcessor)

**Architecture**:
```python
class LogPreprocessor(IPreprocessor):
    """Preprocessor for application and system logs"""

    def __init__(self):
        self.log_processor = LogProcessor()  # Existing component

    async def process(
        self,
        content: str,
        filename: str,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Process log file into LLM-ready summary

        Steps:
        1. Use existing LogProcessor for insight extraction
        2. Extract sample error messages from raw content
        3. Format into structured plain text summary
        4. Return PreprocessedData with summary
        """
        start_time = time.time()

        # 1. Extract insights using existing processor
        insights = await self.log_processor.process(content)

        # 2. Extract sample error lines
        error_samples = self._extract_error_samples(
            content,
            insights.get('top_errors', [])
        )

        # 3. Format into LLM-ready summary
        summary = self._format_log_summary(
            insights=insights,
            error_samples=error_samples,
            source_metadata=source_metadata
        )

        # 4. Build PreprocessedData
        processing_time = (time.time() - start_time) * 1000

        return PreprocessedData(
            data_id=f"data_{uuid.uuid4().hex[:12]}",
            data_type=DataType.LOG_FILE,
            summary=summary[:8000],  # Truncate to 8K chars
            original_size=len(content),
            summary_size=len(summary[:8000]),
            compression_ratio=len(content) / len(summary[:8000]),
            processing_time_ms=processing_time,
            insights=insights,
            security_flags=self._detect_security_issues(content)
        )

    def _format_log_summary(
        self,
        insights: Dict[str, Any],
        error_samples: List[str],
        source_metadata: Optional[SourceMetadata]
    ) -> str:
        """
        Format insights into LLM-ready plain text summary

        Target: ~8,000 characters
        Structure: Human-readable sections with clear headings
        """
        sections = []

        # Header
        sections.append("LOG FILE ANALYSIS SUMMARY")
        sections.append("=" * 50)
        sections.append("")

        # Overview section
        sections.append("OVERVIEW:")
        sections.append(f"Total entries: {insights.get('total_entries', 0):,}")

        time_range = insights.get('time_range')
        if time_range:
            sections.append(
                f"Time range: {time_range['start']} to {time_range['end']}"
            )
            sections.append(
                f"Duration: {time_range.get('duration_hours', 0):.1f} hours"
            )

        error_summary = insights.get('error_summary', {})
        sections.append(
            f"Error count: {error_summary.get('total_errors', 0)} "
            f"({error_summary.get('error_rate', 0):.2%} error rate)"
        )

        # Log level distribution
        log_levels = insights.get('log_level_distribution', {})
        if log_levels:
            level_str = ", ".join(
                f"{level}={count}" for level, count in log_levels.items()
            )
            sections.append(f"Log levels: {level_str}")

        sections.append("")

        # Top error patterns
        sections.append("TOP ERROR PATTERNS:")
        top_errors = insights.get('top_errors', [])[:10]
        for i, error_code in enumerate(top_errors, 1):
            sections.append(f"{i}. {error_code}")
        sections.append("")

        # Anomalies
        anomalies = insights.get('anomalies', [])
        if anomalies:
            sections.append("ANOMALIES DETECTED:")
            for anomaly in anomalies[:5]:  # Top 5 anomalies
                sections.append(
                    f"• {anomaly['type']}: {anomaly['description']} "
                    f"(severity: {anomaly.get('severity', 'unknown')})"
                )
            sections.append("")

        # Sample error messages
        if error_samples:
            sections.append("SAMPLE ERROR MESSAGES:")
            for sample in error_samples[:5]:  # Top 5 samples
                sections.append(sample)
            sections.append("")

        # Performance metrics
        perf_metrics = insights.get('performance_metrics')
        if perf_metrics:
            sections.append("PERFORMANCE METRICS:")
            sections.append(
                f"Average response time: "
                f"{perf_metrics.get('avg_response_time_ms', 0):.2f}ms"
            )
            sections.append(
                f"P95 response time: "
                f"{perf_metrics.get('p95_response_time_ms', 0):.2f}ms"
            )
            sections.append("")

        # Source context
        if source_metadata:
            sections.append("SOURCE:")
            sections.append(f"Type: {source_metadata.source_type}")
            if source_metadata.source_url:
                sections.append(f"URL: {source_metadata.source_url}")
            sections.append("")

        return "\n".join(sections)
```

**Output Example**:
```text
LOG FILE ANALYSIS SUMMARY
==================================================

OVERVIEW:
Total entries: 45,234
Time range: 2025-10-12 14:20:15 to 2025-10-12 16:45:32
Duration: 2.4 hours
Error count: 127 (0.28% error rate)
Log levels: ERROR=127, WARN=450, INFO=44,657

TOP ERROR PATTERNS:
1. Connection timeout to database (45 occurrences)
2. Out of memory error in cache service (12 occurrences)
3. HTTP 503 Service Unavailable (8 occurrences)
4. Failed to acquire lock (5 occurrences)
5. SSL certificate verification failed (3 occurrences)

ANOMALIES DETECTED:
• 14:23:15 - error_spike (severity: high)
• 15:30:00 - unusual_memory_pattern (severity: medium)
• 16:00:00 - log_frequency_drop (severity: low)

SAMPLE ERROR MESSAGES:
[2025-10-12 14:23:15] ERROR Database connection timeout after 30s
[2025-10-12 14:25:30] ERROR Database connection timeout after 30s
[2025-10-12 15:30:42] ERROR Out of memory: Failed to allocate 256MB
...

⚠️ SECURITY: api_key_detected, ip_addresses_in_logs
```

### 2. ErrorPreprocessor (ERROR_REPORT)

**Priority**: P1 (High value for debugging)

**Status**: ❌ Not implemented

**Libraries**: `traceback` (built-in), `re` (built-in)

**Implementation**:
```python
class ErrorPreprocessor(IPreprocessor):
    """Preprocessor for stack traces and error reports"""

    async def process(
        self,
        content: str,
        filename: str,
        source_metadata: Optional[SourceMetadata] = None
    ) -> PreprocessedData:
        """
        Process stack trace into LLM-ready summary

        Steps:
        1. Detect language (Python, Java, JavaScript, Go)
        2. Parse stack trace structure
        3. Extract exception type, message, root cause
        4. Build call chain summary
        5. Format into plain text
        """
        start_time = time.time()

        # Detect programming language
        language = self._detect_language(content)

        # Parse stack trace
        parsed = self._parse_stack_trace(content, language)

        # Format summary
        summary = self._format_error_summary(parsed, source_metadata)

        processing_time = (time.time() - start_time) * 1000

        return PreprocessedData(
            data_id=f"data_{uuid.uuid4().hex[:12]}",
            data_type=DataType.ERROR_REPORT,
            summary=summary[:5000],  # 5K chars for errors
            original_size=len(content),
            summary_size=len(summary[:5000]),
            compression_ratio=len(content) / len(summary[:5000]),
            processing_time_ms=processing_time,
            insights=parsed,
            security_flags=[]
        )

    def _detect_language(self, content: str) -> str:
        """Detect programming language from stack trace patterns"""
        patterns = {
            "python": r"Traceback \(most recent call last\):|File \".*?\", line \d+",
            "java": r"Exception in thread|at [\w.$]+\(",
            "javascript": r"Error:|at .+? \(.+?:\d+:\d+\)",
            "go": r"panic:|goroutine \d+",
        }

        for lang, pattern in patterns.items():
            if re.search(pattern, content):
                return lang

        return "unknown"

    def _parse_stack_trace(
        self,
        content: str,
        language: str
    ) -> Dict[str, Any]:
        """Parse stack trace based on detected language"""
        # Language-specific parsing logic
        # Returns: {exception_type, message, frames, root_cause}
        pass

    def _format_error_summary(
        self,
        parsed: Dict[str, Any],
        source_metadata: Optional[SourceMetadata]
    ) -> str:
        """Format parsed error into LLM-ready summary"""
        sections = []

        sections.append("ERROR REPORT ANALYSIS")
        sections.append("=" * 50)
        sections.append("")

        sections.append("EXCEPTION:")
        sections.append(f"Type: {parsed.get('exception_type', 'Unknown')}")
        sections.append(f"Message: {parsed.get('message', 'No message')}")
        sections.append(f"Language: {parsed.get('language', 'unknown')}")
        sections.append("")

        root_cause = parsed.get('root_cause')
        if root_cause:
            sections.append("ROOT CAUSE:")
            sections.append(f"{root_cause}")
            sections.append("")

        frames = parsed.get('frames', [])[:10]
        if frames:
            sections.append("CALL STACK (top 10 frames):")
            for i, frame in enumerate(frames, 1):
                sections.append(f"{i}. {frame}")
            sections.append("")

        return "\n".join(sections)
```

**Output Example**:
```text
ERROR REPORT ANALYSIS
==================================================

EXCEPTION:
Type: ConnectionTimeoutException
Message: Connection to database timed out after 30s
Language: python

ROOT CAUSE:
File "/app/database.py", line 45, in connect()

CALL STACK (top 10 frames):
1. File "/app/main.py", line 12, in main()
2. File "/app/service.py", line 78, in process_request()
3. File "/app/database.py", line 23, in query()
4. File "/app/database.py", line 45, in connect()
5. File "/lib/psycopg2/__init__.py", line 122, in connect()
...

CONTEXT VARIABLES:
  dsn = "host=db-primary.local port=5432"
  timeout = 30
  retry_count = 3
```

### 3. ConfigPreprocessor (CONFIG_FILE)

**Priority**: P2 (Security scanning value)

**Status**: ❌ Not implemented

**Libraries**: `pyyaml` (NEW - must add), `json` (built-in), `configparser` (built-in)

**Implementation**: Full specification included in this document (see Phase 2.3 and Phase 2.4 tasks in Implementation Roadmap)

**Key Features**:
- Parse YAML, JSON, INI, TOML formats
- Extract key configuration settings
- Scan for hardcoded secrets (passwords, API keys, AWS keys)
- Validate best practices
- Target: ~6K char summary

### 4. MetricsPreprocessor (METRICS_DATA)

**Priority**: P2 (Performance analysis)

**Status**: ❌ Not implemented

**Libraries**: `pandas` ✅, `numpy` ✅, `scipy` ✅ (all available)

**Implementation**: Full specification included in this document (see Phase 2.3 and Phase 2.4 tasks in Implementation Roadmap)

**Key Features**:
- Parse CSV, Prometheus, JSON, InfluxDB formats
- Calculate statistics (min, max, mean, p50, p95, p99)
- Detect anomalies (spikes, drops)
- Identify trends (increasing, decreasing, cyclical)
- Target: ~6K char summary

### 5-8. Advanced Preprocessors

**Preprocessors**: ProfilingPreprocessor, TracePreprocessor, DocumentationPreprocessor, ScreenshotPreprocessor

**Priority**: P3-P5 (Lower priority)

**Status**: ❌ Not implemented (design documented in original specification)

---

## LLM Integration

### Prompt Structure

**Location**: Update agent system prompts in [faultmaven/services/agentic/orchestration/agent_service.py](../../faultmaven/services/agentic/orchestration/agent_service.py)

```python
def _build_system_prompt_with_data(
    self,
    data_type: str,
    summary: str,
    security_flags: List[str]
) -> str:
    """
    Build system prompt with preprocessed data context

    Args:
        data_type: Type of data uploaded
        summary: Preprocessed LLM-ready summary
        security_flags: Security issues detected

    Returns:
        System prompt with data context
    """
    prompt_parts = [
        "# SYSTEM ROLE",
        "",
        "You are an expert troubleshooting assistant helping users diagnose issues.",
        "",
        "# UPLOADED DATA",
        "",
        f"The user has uploaded a {data_type.replace('_', ' ')}.",
        "Below is the preprocessed analysis summary:",
        "",
        "---",
        summary,  # Preprocessed summary goes here
        "---",
        "",
        "# YOUR TASK",
        "",
        "Analyze the data summary above and:",
        "1. Identify root causes of errors/issues",
        "2. Explain the timeline of events",
        "3. Provide specific, actionable recommendations",
        "4. Ask clarifying questions if needed",
        "",
        "IMPORTANT:",
        "- DO NOT simply summarize the data (it's already summarized)",
        "- FOCUS ON diagnosis and solutions",
        "- Reference specific errors and patterns from the data",
        "- Provide technical depth appropriate for the user's expertise"
    ]

    # Add security warnings if present
    if security_flags:
        prompt_parts.extend([
            "",
            "⚠️ SECURITY NOTICE:",
            f"Security issues detected: {', '.join(security_flags)}",
            "Address these in your response if relevant to the troubleshooting."
        ])

    return "\n".join(prompt_parts)
```

### Context Injection Flow

```python
# In upload endpoint (case.py)

# 1. Preprocess data
preprocessed = await preprocessing_service.preprocess(
    content=file_content.decode('utf-8'),
    filename=file.filename,
    case_id=case_id,
    session_id=session_id,
    source_metadata=source_metadata
)

# 2. Build query with preprocessed context
query_request = QueryRequest(
    session_id=session_id,
    query=f"Analyze uploaded file: {file.filename}",
    context={
        "case_id": case_id,
        "data_id": preprocessed.data_id,
        "data_type": preprocessed.data_type.value,
        "preprocessed_summary": preprocessed.summary,  # ← Goes to LLM
        "security_flags": preprocessed.security_flags,
        "source_metadata": preprocessed.source_metadata
    }
)

# 3. Process with agent
agent_response = await agent_service.process_query_for_case(
    case_id, query_request
)

# 4. Return combined response
return DataUploadResponse(
    data_id=preprocessed.data_id,
    filename=file.filename,
    file_size=len(file_content),
    data_type=preprocessed.data_type.value,
    processing_status="completed",
    uploaded_at=datetime.utcnow().isoformat() + 'Z',
    agent_response=agent_response  # ← Conversational AI analysis
)
```

### Token Budget

| Component | Tokens | Characters |
|-----------|--------|------------|
| System prompt | ~500 | ~1,000 |
| Preprocessed data summary | 4,000-6,000 | 8,000 |
| Conversation history | ~2,000 | ~4,000 |
| User query | ~200 | ~400 |
| **Total input** | **~7,000-9,000** | **~13,400** |
| **Available for response** | **23,000-25,000** | **46,000-50,000** |

**Fits comfortably in**: Claude Sonnet 3.5 (200K context), GPT-4 (128K context)

---

## Security & Privacy

### PII Redaction

**Component**: `DataSanitizer` ([faultmaven/infrastructure/security/redaction.py](../../faultmaven/infrastructure/security/redaction.py))

**Status**: ✅ Already implemented

**Integration**: Called in `PreprocessingService.preprocess()` after summary generation

**Patterns Detected**:
- Email addresses
- Phone numbers
- Social Security Numbers
- Credit card numbers
- IP addresses (internal ranges)
- API keys, tokens, passwords
- AWS keys, JWT tokens
- Bearer tokens, session IDs

**Process**:
```python
# In PreprocessingService.preprocess()

preprocessed = await preprocessor.process(content, filename, source_metadata)

# Sanitize summary
preprocessed.summary = self.sanitizer.sanitize(preprocessed.summary)

# Add security flags
preprocessed.security_flags = self._detect_security_flags(content)

return preprocessed
```

### Security Flags

**Purpose**: Alert user and agent about security issues in uploaded data

**Common Flags**:
- `pii_detected` - Personal information found
- `api_key_detected` - API keys in logs/configs
- `password_hardcoded` - Hardcoded passwords in configs
- `aws_key_detected` - AWS credentials exposed
- `private_key_detected` - Private keys in content

**Usage**: Passed to agent in context, displayed in response

---

## Implementation Roadmap

### Phase 0: Critical Fixes (1-2 hours)

**Priority**: P0 (Blocking)

**Tasks**:
- [ ] Add `METRICS_DATA`, `PROFILING_DATA`, `TRACE_DATA` to DataType enum ([models/api.py:50-58](../../faultmaven/models/api.py:50-58))
- [ ] Update classifier patterns to detect new types ([classifier.py:994](../../faultmaven/core/processing/classifier.py:994))
- [ ] Remove workaround at [classifier.py:1040](../../faultmaven/core/processing/classifier.py:1040)

**Deliverable**: All 8 data types properly defined

### Phase 1: Foundation (8-12 hours)

**Priority**: P1 (High)

**Tasks**:
- [ ] Create `PreprocessedData` model in [models/api.py](../../faultmaven/models/api.py)
- [ ] Create `SourceMetadata` model in [models/api.py](../../faultmaven/models/api.py)
- [ ] Create `IPreprocessor` interface in [models/interfaces.py](../../faultmaven/models/interfaces.py)
- [ ] Create `PreprocessingService` orchestrator in `services/preprocessing/preprocessing_service.py`
- [ ] Create base preprocessor implementations skeleton
- [ ] Add `preprocessing_service` to dependency injection ([container.py](../../faultmaven/container.py))

**Deliverable**: Preprocessing infrastructure ready

### Phase 2: Core Preprocessors (20-30 hours)

**Priority**: P1-P2 (High to Medium)

#### Task 2.1: LogPreprocessor (6 hours)
- [ ] Create `LogPreprocessor` class
- [ ] Implement `_extract_error_samples()` method
- [ ] Implement `_format_log_summary()` method
- [ ] Implement `_detect_security_issues()` method
- [ ] Add unit tests for log formatting
- [ ] Test with 10+ real log files (various formats)

#### Task 2.2: ErrorPreprocessor (6 hours)
- [ ] Create `ErrorPreprocessor` class
- [ ] Implement `_detect_language()` (Python, Java, JS, Go)
- [ ] Implement `_parse_stack_trace()` for each language
- [ ] Implement `_format_error_summary()` method
- [ ] Add unit tests for stack trace parsing
- [ ] Test with real stack traces from each language

#### Task 2.3: MetricsPreprocessor (8 hours)
- [ ] Create `MetricsPreprocessor` class
- [ ] Implement format detection (CSV, Prometheus, JSON, InfluxDB)
- [ ] Implement statistical analysis (pandas, numpy)
- [ ] Implement anomaly detection (scipy)
- [ ] Implement trend analysis
- [ ] Implement `_format_metrics_summary()` method
- [ ] Add unit tests
- [ ] Test with various metrics formats

#### Task 2.4: ConfigPreprocessor (8 hours)
- [ ] Add `pyyaml` to `requirements.txt`
- [ ] Create `ConfigPreprocessor` class
- [ ] Implement format detection (YAML, JSON, INI, TOML, ENV)
- [ ] Implement config parsing and validation
- [ ] Implement security scanning (hardcoded secrets)
- [ ] Implement `_format_config_summary()` method
- [ ] Add unit tests
- [ ] Test with various config formats

**Deliverable**: 4 core preprocessors fully functional

### Phase 3: Integration (6-8 hours)

**Priority**: P1 (High)

**Tasks**:
- [ ] Update `upload_case_data()` endpoint to use `PreprocessingService` ([case.py:2094](../../faultmaven/api/v1/routes/case.py:2094))
- [ ] Update `DataService.upload_data()` to return preprocessed data
- [ ] Update agent system prompts to handle preprocessed data ([agent_service.py](../../faultmaven/services/agentic/orchestration/agent_service.py))
- [ ] Update `DataUploadResponse` to include `agent_response` field
- [ ] Add integration tests for upload → preprocess → LLM flow
- [ ] Test end-to-end with frontend

**Deliverable**: Complete data upload → AI analysis flow working

### Phase 4: Advanced Preprocessors (15-20 hours)

**Priority**: P3 (Medium)

**Tasks**:
- [ ] Implement `ProfilingPreprocessor` (8 hours)
- [ ] Implement `TracePreprocessor` (8 hours)
- [ ] Implement `DocumentationPreprocessor` (2 hours)
- [ ] Implement `GenericPreprocessor` for OTHER type (2 hours)

**Deliverable**: Complete preprocessor coverage for all data types

### Phase 5: Advanced Features (Optional - 10 hours)

**Priority**: P4 (Low)

**Tasks**:
- [ ] Implement `ScreenshotPreprocessor` (requires vision LLM integration)
- [ ] Add preprocessing result caching (Redis)
- [ ] Add preprocessing performance metrics
- [ ] Add async preprocessing for very large files (>10MB)

**Deliverable**: Production-grade preprocessing with advanced features

---

## Dependencies

### Current Dependencies (Available) ✅

- `pandas` - DataFrame operations, time-series analysis
- `numpy` - Numerical computations
- `scipy` - Statistical analysis, anomaly detection
- `scikit-learn` - ML algorithms (IsolationForest)
- `json` - JSON parsing (built-in)
- `configparser` - INI parsing (built-in)
- `traceback` - Stack trace parsing (built-in)
- `pstats` - Python profiling (built-in)
- `re` - Regular expressions (built-in)

### New Dependencies Required ➕

| Dependency | Purpose | Priority | Installation |
|-----------|---------|----------|--------------|
| `pyyaml` | YAML parsing for config files | HIGH (P2) | `pip install pyyaml` |
| `toml` | TOML parsing (optional) | LOW (P4) | `pip install toml` |
| `pillow` | Image processing for screenshots | LOW (P5) | `pip install pillow` |
| `pytesseract` | OCR for screenshots (optional) | LOW (P5) | `pip install pytesseract` |

**Critical Path**: Only `pyyaml` is needed for P1-P2 features

**Installation Command**:
```bash
pip install pyyaml
```

---

## Appendix A: File Structure

### New Files to Create

```
faultmaven/
├── services/
│   └── preprocessing/
│       ├── __init__.py
│       ├── preprocessing_service.py      # Orchestrator (NEW)
│       └── preprocessors/
│           ├── __init__.py
│           ├── base.py                    # IPreprocessor interface (NEW)
│           ├── log_preprocessor.py       # LOG_FILE (NEW)
│           ├── error_preprocessor.py     # ERROR_REPORT (NEW)
│           ├── config_preprocessor.py    # CONFIG_FILE (NEW)
│           ├── metrics_preprocessor.py   # METRICS_DATA (NEW)
│           ├── profiling_preprocessor.py # PROFILING_DATA (NEW)
│           ├── trace_preprocessor.py     # TRACE_DATA (NEW)
│           ├── documentation_preprocessor.py # DOCUMENTATION (NEW)
│           ├── screenshot_preprocessor.py    # SCREENSHOT (NEW)
│           └── generic_preprocessor.py   # OTHER type fallback (NEW)
```

### Files to Modify

```
faultmaven/
├── models/
│   └── api.py                         # Add PreprocessedData, SourceMetadata models
├── api/v1/routes/
│   └── case.py                        # Update upload_case_data() endpoint
├── services/agentic/orchestration/
│   └── agent_service.py               # Update prompt building with data context
├── core/processing/
│   └── classifier.py                  # Add METRICS_DATA, PROFILING_DATA, TRACE_DATA
└── container.py                       # Add preprocessing_service to DI
```

---

## Appendix B: Testing Strategy

### Unit Tests

**Location**: `tests/unit/services/preprocessing/`

**Test Cases**:
- Classification accuracy for each data type
- Preprocessor output format validation
- Summary length constraints (max 8K chars)
- Security flag detection
- PII redaction verification
- Processing time performance

### Integration Tests

**Location**: `tests/integration/`

**Test Cases**:
- Upload → Classify → Preprocess → LLM flow
- Multiple data types in same case
- Large file handling (50KB+)
- Error handling and fallbacks
- Agent response quality

### End-to-End Tests

**Location**: `tests/e2e/`

**Test Cases**:
- Frontend upload → Backend preprocess → AI response
- Conversation context preservation
- Multi-turn troubleshooting with data uploads
- Security flag display in UI

---

## Appendix C: Success Criteria

### Functional Requirements

- ✅ Classify 8 data types with >90% accuracy
- ✅ Preprocess files up to 50MB within 10 seconds
- ✅ Generate summaries within token limits (8K chars max)
- ✅ Provide conversational AI responses based on preprocessed data
- ✅ Detect security issues (PII, secrets) with >95% recall

### Non-Functional Requirements

- ✅ **Performance**: P95 latency < 5 seconds for preprocessing
- ✅ **Reliability**: 99% success rate for preprocessing
- ✅ **Scalability**: Handle 100 concurrent uploads
- ✅ **Maintainability**: Each preprocessor independently testable
- ✅ **Usability**: Users get actionable insights, not just summaries

---

## Document Change Log

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-10-12 | Initial design document |
| 2.0 | 2025-10-12 | Added enum fix and preprocessor specs |
| 3.0 | 2025-10-12 | Consolidated with 3-step pipeline design |
| 4.0 | 2025-10-13 | Final design with complete implementation blueprint |

---

## References

- [Data Submission Design](./data-submission-design.md) - Upload flow and API endpoints
- [OpenAPI Specification](../api/openapi.locked.yaml) - API contracts
- [Architecture Overview](./architecture-overview.md) - System architecture and integration context

---

**Status**: ✅ **FINAL DESIGN - READY FOR IMPLEMENTATION**

This document serves as the authoritative blueprint for implementing the data preprocessing system in FaultMaven. All implementation should follow this specification.

**Next Action**: Begin Phase 0 (Critical Fixes) followed by Phase 1 (Foundation).
