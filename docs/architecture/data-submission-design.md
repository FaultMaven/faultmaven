# FaultMaven Data Submission Design v4.0

## Executive Summary

This document specifies how users submit diagnostic data (logs, metrics, traces, configs) to FaultMaven through two complementary paths:

1. **Explicit Upload Path**: Dedicated "Upload" UI for files/text/pages via `POST /api/v1/cases/{case_id}/data`
2. **Implicit Detection Path**: Intelligent paste detection in query box via `POST /api/v1/cases/{case_id}/queries`

**Key Principle**: Both paths converge into the same processing pipeline, appearing as natural conversation turns with AI-generated analysis responses.

**Architecture Layers**:
```
Data Submission (THIS DOC) → Data Preprocessing → Evidence Evaluation → Agent Response
    ↓                              ↓                      ↓                  ↓
API/UX Layer              Transformation Layer      Analysis Layer     Generation Layer
```

**Related Documents**:
- [Data Preprocessing Architecture v2.0](./data-preprocessing-architecture.md) - How raw data transforms into insights
- [Evidence Architecture v1.1](./evidence-architecture.md) - How insights link to hypotheses
- [API Specification](../api/openapi.locked.yaml) - OpenAPI endpoint definitions

---

## Table of Contents

1. [System Context](#1-system-context)
2. [Core Principles](#2-core-principles)
3. [Architecture Overview](#3-architecture-overview)
4. [Submission Path 1: Explicit Upload](#4-submission-path-1-explicit-upload)
5. [Submission Path 2: Implicit Detection](#5-submission-path-2-implicit-detection)
6. [API Specifications](#6-api-specifications)
7. [Frontend Integration](#7-frontend-integration)
8. [Backend Integration](#8-backend-integration)
9. [Source Metadata Enhancement](#9-source-metadata-enhancement)
10. [Testing Requirements](#10-testing-requirements)
11. [Cross-References](#11-cross-references)

---

## 1. System Context

### 1.1 Role in System Architecture

Data Submission is the **API and UX layer** that handles user interactions with the data ingestion system. It does not implement data processing—that's delegated to specialized architectures.

**Three-Layer Architecture**:

```
┌──────────────────────────────────────────────────────────────┐
│ DATA SUBMISSION DESIGN v4.0 (THIS DOCUMENT)                 │
│ Layer: API & UX                                              │
│ Responsibility: User interaction, routing, conversation UX   │
│                                                              │
│ • POST /data endpoint for explicit uploads                   │
│ • POST /queries endpoint with paste detection                │
│ • Query classification (hints → patterns → heuristics)       │
│ • Conversational response formatting                         │
│ • Frontend conversation integration                          │
└──────────────────────┬───────────────────────────────────────┘
                       │ Routes to
                       ↓
┌──────────────────────────────────────────────────────────────┐
│ DATA PREPROCESSING ARCHITECTURE v2.0                         │
│ Layer: Transformation                                        │
│ Responsibility: Extract insights from raw data               │
│                                                              │
│ • Validate & classify data type (6 types)                    │
│ • Type-specific extraction (Crime Scene, Anomaly Detection)  │
│ • Sanitize PII/secrets                                       │
│ • Output: PreprocessingResult                                │
└──────────────────────┬───────────────────────────────────────┘
                       │ PreprocessingResult
                       ↓
┌──────────────────────────────────────────────────────────────┐
│ EVIDENCE ARCHITECTURE v1.1                                   │
│ Layer: Analysis                                              │
│ Responsibility: Link evidence to hypotheses                  │
│                                                              │
│ • 6-dimensional evidence classification                      │
│ • Create Evidence objects                                    │
│ • Update hypothesis links and status                         │
│ • Output: Evidence + Updated Case                            │
└──────────────────────┬───────────────────────────────────────┘
                       │ Evidence + Case
                       ↓
┌──────────────────────────────────────────────────────────────┐
│ AGENT SERVICE                                                │
│ Layer: Generation                                            │
│ Responsibility: Generate conversational AI responses         │
│                                                              │
│ • Analyze evidence in case context                           │
│ • Generate insights and recommendations                      │
│ • Output: AgentResponse                                      │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 Scope Boundaries

**This Document Covers**:
- ✅ API endpoint definitions (`/data`, `/queries`)
- ✅ Request/response schemas
- ✅ Query classification algorithm (3-tier)
- ✅ Frontend conversation UX
- ✅ Integration points with other layers
- ✅ Source metadata flow

**This Document Does NOT Cover** (See Other Docs):
- ❌ Data type classification algorithms → See Data Preprocessing v2.0
- ❌ Extraction strategies (Crime Scene, Anomaly Detection) → See Data Preprocessing v2.0
- ❌ Evidence-hypothesis linkage → See Evidence Architecture v1.1
- ❌ Hypothesis status updates → See Evidence Architecture v1.1
- ❌ Agent prompt engineering → See Prompt Engineering Architecture

---

## 2. Core Principles

### 2.1 Conversational Flow First

**Principle**: Data uploads are conversation messages, not separate UI elements.

**User Experience**:
```
User: 📎 Uploaded: application.log (45KB)

AI: I've analyzed your application log. I found 127 error entries, 
    with the most critical being:
    - 45 database connection timeouts starting at 14:23 UTC
    - 12 out-of-memory exceptions in the cache service
    
    Would you like me to help diagnose the connection timeout issue first?
```

**Not This**:
```
✓ File uploaded successfully
[Separate "Session Data" UI section shows uploaded files]
```

### 2.2 Dual Path Convergence

**Principle**: Explicit uploads and implicit detection (paste) use the same processing pipeline.

```
Path 1: Explicit Upload          Path 2: Implicit Paste
POST /data                       POST /queries
     ↓                                ↓
     ↓                         Query Classifier
     ↓                         (if machine data)
     ↓                                ↓
     └────────────┬───────────────────┘
                  ↓
        Same Processing Pipeline
        (Preprocessing → Evidence → Agent)
```

### 2.3 Backend-Driven Analysis

**Principle**: Backend generates insights, frontend displays them. No "upload successful" toasts.

**Backend Responsibility**:
- Extract key findings from data
- Link to relevant hypotheses
- Generate actionable recommendations
- Format as conversational response

**Frontend Responsibility**:
- Display upload + AI response in conversation
- No separate "uploaded files" UI section
- Show processing state gracefully

### 2.4 Context Preservation

**Principle**: Data submissions are part of conversation history, not separate system state.

**Maintained Context**:
- Evidence links to hypotheses
- Timeline events extracted from data
- Previous analyses reference uploaded evidence
- Agent can say: "Based on the log you uploaded earlier..."

---

## 3. Architecture Overview

### 3.1 End-to-End Flow

```
┌─────────────────────────────────────────────────────────────┐
│ USER ACTION                                                  │
└─────────────────────┬───────────────────────────────────────┘
                      │
        ┌─────────────┴──────────────┐
        │                            │
        ↓ Explicit                   ↓ Implicit
  POST /data                    POST /queries
  (upload button)               (paste in chat)
        │                            │
        │                            ↓
        │                       QueryClassifier
        │                       (3-tier detection)
        │                            │
        │                            ↓
        │                    if is_machine_data:
        └─────────────┬──────────────┘
                      │
                      ↓
┌─────────────────────────────────────────────────────────────┐
│ DATA PREPROCESSING (See: data-preprocessing-architecture.md)│
│                                                              │
│ 1. Validate (size ≤10MB, type allowed)                      │
│ 2. Classify data type (LOGS_AND_ERRORS, METRICS, etc.)      │
│ 3. Extract insights (Crime Scene, Anomaly Detection, etc.)  │
│ 4. Sanitize (PII/secrets redacted)                          │
│ 5. Package → PreprocessingResult                             │
│    - summary: <500 chars (for Evidence.summary)             │
│    - full_extraction: complete insights                      │
│    - content_ref: s3://bucket/case/file                     │
│    - extraction_metadata: {method, compression_ratio, ...}   │
│                                                              │
│ Time: 0.5-30s depending on type/size/user choices           │
└─────────────────────┬───────────────────────────────────────┘
                      │ PreprocessingResult
                      ↓
┌─────────────────────────────────────────────────────────────┐
│ EVIDENCE EVALUATION (See: evidence-architecture.md)         │
│                                                              │
│ 1. Multi-dimensional classification (6 dimensions)          │
│    - Request matching (which EvidenceRequirements?)         │
│    - Hypothesis matching (relevant hypotheses)              │
│    - Completeness, Form, Type, Intent                       │
│                                                              │
│ 2. Create Evidence object                                   │
│    Evidence(                                                │
│        summary=preprocessing_result.summary,                │
│        content_ref=preprocessing_result.content_ref,        │
│        source_type=LOGS_AND_ERRORS,  # Mapped from DataType│
│        form=DOCUMENT,                                       │
│    )                                                        │
│                                                              │
│ 3. Hypothesis analysis (function calling)                   │
│    For each hypothesis:                                     │
│    - Update evidence_links with stance & reasoning          │
│    - Update status (PROPOSED → TESTING → VALIDATED)         │
│                                                              │
│ Time: 1-3s                                                  │
└─────────────────────┬───────────────────────────────────────┘
                      │ Evidence + Updated Hypotheses
                      ↓
┌─────────────────────────────────────────────────────────────┐
│ AGENT RESPONSE GENERATION                                    │
│                                                              │
│ Generate conversational analysis response:                  │
│ "I've analyzed your application log. I found 127 errors..." │
│                                                              │
│ Time: 2-4s                                                  │
└─────────────────────┬───────────────────────────────────────┘
                      │ AgentResponse
                      ↓
┌─────────────────────────────────────────────────────────────┐
│ FRONTEND CONVERSATION UPDATE                                 │
│                                                              │
│ Add two messages to chat:                                   │
│ 1. User message: "📎 Uploaded: application.log (45KB)"      │
│ 2. AI message: [agent response content]                    │
└─────────────────────────────────────────────────────────────┘

                ┌─ PARALLEL ASYNC (Fire-and-forget) ─┐
                ↓
┌─────────────────────────────────────────────────────────────┐
│ VECTOR DB BACKGROUND STORAGE (Optional)                     │
│                                                              │
│ IF user chose caching mode:                                 │
│   - Chunk full_extraction (512 tokens)                      │
│   - Generate embeddings (BGE-M3)                            │
│   - Store in case_{case_id} collection                      │
│                                                              │
│ Purpose: Long-term memory for forensic deep dives           │
│ NOT for: Primary evidence storage (uses Evidence objects)   │
│                                                              │
│ Time: 2-5s (user doesn't wait)                              │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Data Type Flow

**Preprocessing Classification → Evidence Source Type Mapping**:

From [Data Preprocessing v2.0 - Section 2.3](./data-preprocessing-architecture.md#23-data-type-mapping):

| Preprocessing DataType | Evidence SourceType | Example |
|------------------------|---------------------|---------|
| `LOGS_AND_ERRORS` | `LOG_FILE` | application.log, syslog |
| `METRICS_AND_PERFORMANCE` | `METRICS_DATA` | prometheus.txt, metrics.csv |
| `STRUCTURED_CONFIG` | `CONFIG_FILE` | database.yaml, app.json |
| `SOURCE_CODE` | `CODE_REVIEW` | auth_service.py |
| `UNSTRUCTURED_TEXT` | `USER_OBSERVATION` | incident_report.txt |
| `VISUAL_EVIDENCE` | `SCREENSHOT` | error_page.png |

All file uploads have `form=DOCUMENT` (vs `USER_INPUT` for typed text in query box).

---

## 4. Submission Path 1: Explicit Upload

### 4.1 Overview

**Endpoint**: `POST /api/v1/cases/{case_id}/data`

**Use Case**: User clicks "Upload" button in browser extension UI

**User Flow**:
1. User clicks upload button
2. Selects file OR pastes text OR captures page
3. Frontend converts to File object, uploads via FormData
4. Backend processes through full pipeline
5. User sees upload message + AI analysis in conversation

**Why This Path Exists**: Provides explicit, intentional data submission with clear UI affordance.

### 4.2 API Specification

**Request**:
```http
POST /api/v1/cases/{case_id}/data HTTP/1.1
Content-Type: multipart/form-data
Authorization: Bearer <token>

--boundary
Content-Disposition: form-data; name="file"; filename="application.log"
Content-Type: text/plain

[file content]
--boundary
Content-Disposition: form-data; name="session_id"

sess_abc123
--boundary
Content-Disposition: form-data; name="source_metadata"

{"source_type": "file_upload", "user_description": "Application logs from server-1"}
--boundary--
```

**Parameters**:
- `file` (required): Binary file data
- `session_id` (required): Active session identifier
- `source_metadata` (optional): JSON string with source context (see Section 9)
- `description` (optional): User's description of the file

**Response** (201 Created):
```http
HTTP/1.1 201 Created
Location: /api/v1/cases/case_123/evidence/ev_abc
X-Correlation-ID: <uuid>
Content-Type: application/json

{
  "evidence_id": "ev_abc",
  "preprocessing": {
    "data_type": "logs_and_errors",
    "extraction_method": "crime_scene_extraction",
    "compression_ratio": 0.005,
    "summary": "Application log: 847 entries, 23 NullPointerExceptions in auth-service"
  },
  "evidence": {
    "source_type": "log_file",
    "form": "document",
    "content_ref": "s3://faultmaven-evidence/case_123/ev_abc.log",
    "content_size_bytes": 5242880,
    "collected_at": "2025-11-01T14:30:00Z"
  },
  "agent_response": {
    "content": "I've analyzed your application log. I found 23 NullPointerExceptions...",
    "response_type": "ANSWER",
    "confidence_score": 0.85,
    "sources": [
      {
        "type": "uploaded_evidence",
        "evidence_id": "ev_abc",
        "relevance": 0.95
      }
    ]
  }
}
```

**Error Responses**:
```json
// 400 Bad Request - File too large
{
  "error": "file_too_large",
  "file_size": 15728640,
  "max_size": 10485760,
  "message": "File exceeds 10MB limit",
  "suggestions": [
    "Upload only the relevant time range",
    "Filter to ERROR/FATAL level logs only"
  ]
}

// 415 Unsupported Media Type
{
  "error": "unsupported_file_type",
  "content_type": "application/exe",
  "allowed_types": ["text/plain", "text/csv", "application/json", ...]
}
```

### 4.3 Processing Pipeline Integration

**Backend Implementation**:
```python
@router.post("/{case_id}/data", status_code=status.HTTP_201_CREATED)
async def upload_case_data(
    case_id: str,
    file: UploadFile = File(...),
    session_id: str = Form(...),
    source_metadata: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    case_service: ICaseService = Depends(...),
    preprocessing_service: PreprocessingService = Depends(...),
    evidence_service: EvidenceService = Depends(...),
    agent_service: AgentService = Depends(...),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Upload data file to case and generate AI analysis.
    
    Pipeline:
    1. Preprocessing (Data Preprocessing v2.0)
    2. Evidence creation (Evidence Architecture v1.1)
    3. Agent response generation
    """
    
    # 1. Verify case access
    case = await case_service.get_case(case_id, current_user.user_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    # 2. Read file content
    file_content = await file.read()
    
    # 3. Data Preprocessing (delegates to Data Preprocessing Architecture)
    preprocessing_result = await preprocessing_service.process_upload(
        file=file,
        case_id=case_id,
        source_metadata=parse_source_metadata(source_metadata),
    )
    # Returns: PreprocessingResult with summary, full_extraction, content_ref
    
    # 4. Evidence Classification (delegates to Evidence Architecture)
    classification = await evidence_service.classify_evidence(
        user_input=preprocessing_result.summary,
        case=case,
    )
    # Returns: EvidenceClassification with 6 dimensions
    
    # 5. Evidence Creation (delegates to Evidence Architecture)
    evidence = await evidence_service.create_evidence(
        preprocessing_result=preprocessing_result,
        classification=classification,
        case_id=case_id,
        phase=case.current_phase,
        uploaded_by=current_user.email,
    )
    # Returns: Evidence object stored in DB
    
    # 6. Hypothesis Analysis (delegates to Evidence Architecture)
    case = await evidence_service.analyze_evidence_impact(
        evidence=evidence,
        classification=classification,
        case=case,
    )
    # Updates: Hypothesis evidence_links and status
    
    # 7. Agent Response Generation
    agent_response = await agent_service.generate_evidence_analysis_response(
        evidence=evidence,
        case=case,
    )
    # Returns: AgentResponse with conversational insights
    
    # 8. Background: Vector DB storage (fire-and-forget)
    if should_cache_in_vector_db(preprocessing_result):
        background_tasks.add_task(
            store_in_vector_db,
            case_id=case_id,
            evidence_id=evidence.evidence_id,
            preprocessed_content=preprocessing_result.full_extraction,
        )
    
    # 9. Return response
    return DataUploadResponse(
        evidence_id=evidence.evidence_id,
        preprocessing=PreprocessingSummary(
            data_type=preprocessing_result.data_type,
            extraction_method=preprocessing_result.extraction_method,
            compression_ratio=preprocessing_result.compression_ratio,
            summary=preprocessing_result.summary,
        ),
        evidence=EvidenceSummary(
            source_type=evidence.source_type,
            form=evidence.form,
            content_ref=evidence.content_ref,
            content_size_bytes=evidence.content_size_bytes,
            collected_at=evidence.collected_at,
        ),
        agent_response=agent_response,
    )
```

**Key Integration Points**:

1. **Preprocessing Service** (Data Preprocessing v2.0)
   - Handles: Validation, classification, extraction, sanitization
   - Input: Raw file + metadata
   - Output: `PreprocessingResult`

2. **Evidence Service** (Evidence Architecture v1.1)
   - Handles: Classification, evidence creation, hypothesis analysis
   - Input: `PreprocessingResult`
   - Output: `Evidence` object + updated `Case`

3. **Agent Service**
   - Handles: Conversational response generation
   - Input: `Evidence` + `Case`
   - Output: `AgentResponse`

---

## 5. Submission Path 2: Implicit Detection

### 5.1 Overview

**Endpoint**: `POST /api/v1/cases/{case_id}/queries`

**Use Case**: User pastes large data into query box, or UI explicitly marks content as machine data

**User Flow**:
1. User pastes content into query input (or UI marks as machine data)
2. QueryClassifier analyzes content (3-tier system)
3. If detected as machine data → routes to preprocessing pipeline
4. Otherwise → processes as normal query
5. User sees their input + AI analysis in conversation

**Why This Path Exists**: Enables friction-free data submission without requiring explicit upload button clicks.

### 5.2 Query Classification (3-Tier System)

**Classification Tiers**:

```python
# Tier 1: Explicit UI Hints (Confidence: 1.0)
if request.query_type == "machine_data" or request.is_raw_content:
    return ClassificationResult(
        is_machine_data=True,
        confidence=1.0,
        source="explicit_hint"
    )

# Tier 2: Pattern Matching (Confidence: 0.70-0.90)
patterns = detect_patterns(query)  # Timestamps, errors, metrics
severity = calculate_severity(patterns)
confidence = min(0.90, 0.50 + severity/100)

if confidence >= 0.70:
    return ClassificationResult(
        is_machine_data=True,
        confidence=confidence,
        data_type=infer_data_type(patterns),
        source="pattern_matching"
    )

# Tier 3: Fallback Heuristics (Confidence: 0.50)
if len(query) > 500 and not is_conversational(query):
    return ClassificationResult(
        is_machine_data=True,
        confidence=0.50,
        source="heuristics"
    )

# Default: Human question
return ClassificationResult(
    is_machine_data=False,
    confidence=0.80,
    source="default"
)
```

**Pattern Categories**:

| Pattern | Regex | Example Match |
|---------|-------|---------------|
| Timestamps | `\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}` | `2025-11-01T14:30:00` |
| Log Levels | `\b(FATAL\|ERROR\|WARN\|INFO)\b` | `ERROR` |
| Stack Traces | `at\s+[\w.$]+\(.*?:\d+\)` | `at com.app.Service(Service.java:42)` |
| Error Keywords | `\b(exception\|error\|failure\|timeout)\b` | `NullPointerException` |
| Metrics | `\d+\.\d+\s*(ms\|MB\|%\|req/s)` | `45.2 ms` |

**Severity Scoring**:
```python
SEVERITY_WEIGHTS = {
    "FATAL": 100,
    "CRITICAL": 90,
    "ERROR": 50,
    "WARN": 10,
    "INFO": 1
}

severity_score = sum(
    SEVERITY_WEIGHTS[level] * count
    for level, count in detected_levels.items()
)
```

### 5.3 API Specification (Enhanced v3.2)

**Request**:
```http
POST /api/v1/cases/{case_id}/queries HTTP/1.1
Content-Type: application/json
Authorization: Bearer <token>

{
  "session_id": "sess_abc123",
  "query": "[Large pasted content or question]",
  
  // v3.2 enhancements
  "query_type": "question" | "machine_data",  // Optional UI hint
  "content_type": "LOGS_AND_ERRORS" | "METRICS_AND_PERFORMANCE",  // Optional
  "is_raw_content": false,  // True if known machine data
  
  "context": {
    "current_phase": 4,
    "active_hypothesis_ids": ["hyp_001"]
  }
}
```

**Response Options**:

**Option A: Sync Processing** (201 Created - for small data or questions):
```json
{
  "response_id": "resp_xyz",
  "session_id": "sess_abc123",
  "response_type": "ANSWER",
  "content": "I've analyzed your data submission. Found 23 errors...",
  "confidence_score": 0.85,
  "sources": [
    {
      "type": "uploaded_evidence",
      "evidence_id": "ev_def",
      "relevance": 0.95
    }
  ],
  "context": {
    "classification": {
      "is_machine_data": true,
      "confidence": 0.85,
      "data_type": "logs_and_errors",
      "source": "pattern_matching"
    },
    "evidence_created": {
      "evidence_id": "ev_def",
      "source_type": "log_file"
    }
  }
}
```

**Option B: Async Processing** (202 Accepted - for large data >10K chars):
```json
{
  "status": "processing",
  "processing_id": "proc_ghi",
  "estimated_completion_seconds": 30,
  "poll_url": "/api/v1/cases/case_123/processing/proc_ghi/status"
}
```

### 5.4 Processing Logic

```python
@router.post("/{case_id}/queries", status_code=status.HTTP_201_CREATED)
async def submit_case_query(
    case_id: str,
    query_request: QueryRequest,
    query_classifier: QueryClassifier = Depends(...),
    preprocessing_service: PreprocessingService = Depends(...),
    agent_service: AgentService = Depends(...),
):
    """
    Process query with intelligent machine data detection.
    
    Routes to preprocessing if machine data detected.
    """
    
    # 1. Classify query (3-tier system)
    classification = await query_classifier.classify(
        query=query_request.query,
        query_type=query_request.query_type,  # UI hint
        is_raw_content=query_request.is_raw_content,  # UI hint
        content_type=query_request.content_type,  # UI hint
    )
    
    # 2. Route based on classification
    if classification.is_machine_data and classification.confidence >= 0.70:
        # Route to preprocessing pipeline (Path 1 convergence)
        preprocessing_result = await preprocessing_service.process_text(
            text=query_request.query,
            case_id=case_id,
            data_type_hint=classification.data_type,
        )
        
        # Create evidence (same as explicit upload)
        evidence = await evidence_service.create_from_preprocessing(
            preprocessing_result=preprocessing_result,
            case_id=case_id,
        )
        
        # Generate response with evidence context
        agent_response = await agent_service.generate_response(
            query=f"[Machine data detected and processed]",
            evidence=evidence,
            case_id=case_id,
        )
        
        # Include classification metadata in response
        agent_response.context["classification"] = classification
        agent_response.context["evidence_created"] = {
            "evidence_id": evidence.evidence_id,
            "source_type": evidence.source_type.value
        }
        
        return agent_response
    
    else:
        # Process as normal query
        return await agent_service.process_query_for_case(
            case_id=case_id,
            query_request=query_request,
        )
```

**Key Difference from Path 1**:
- Path 1 (explicit): Always treats input as data
- Path 2 (implicit): Classifies first, then routes

**Convergence Point**: Both paths end up calling the same preprocessing and evidence services.

---

## 6. API Specifications

### 6.1 Request Schemas

**DataUploadRequest** (Path 1):
```typescript
interface DataUploadRequest {
  // Multipart form data
  file: File;
  session_id: string;
  source_metadata?: string;  // JSON-serialized SourceMetadata
  description?: string;
}
```

**QueryRequest** (Path 2 - Enhanced v3.2):
```typescript
interface QueryRequest {
  session_id: string;
  query: string;
  
  // v3.2 UI hints
  query_type?: "question" | "machine_data";
  content_type?: DataType;  // LOGS_AND_ERRORS, METRICS_AND_PERFORMANCE, etc.
  is_raw_content?: boolean;
  
  context?: {
    current_phase?: number;
    active_hypothesis_ids?: string[];
    [key: string]: any;
  };
}
```

### 6.2 Response Schemas

**DataUploadResponse** (Path 1):
```typescript
interface DataUploadResponse {
  evidence_id: string;
  
  preprocessing: {
    data_type: DataType;  // From Data Preprocessing v2.0
    extraction_method: string;
    compression_ratio: number;
    summary: string;
  };
  
  evidence: {
    source_type: EvidenceSourceType;  // From Evidence Architecture v1.1
    form: "document";
    content_ref: string;  // S3 URI
    content_size_bytes: number;
    collected_at: string;  // ISO 8601
  };
  
  agent_response: AgentResponse;
}
```

**AgentResponse** (Both Paths):
```typescript
interface AgentResponse {
  response_id: string;
  session_id: string;
  response_type: ResponseType;  // ANSWER, NEEDS_MORE_DATA, etc.
  content: string;  // Main conversational text
  confidence_score?: number;
  
  sources?: Array<{
    type: "uploaded_evidence" | "knowledge_base" | "case_context";
    evidence_id?: string;
    relevance: number;
  }>;
  
  context?: {
    classification?: QueryClassification;  // Path 2 only
    evidence_created?: {
      evidence_id: string;
      source_type: string;
    };
    [key: string]: any;
  };
}
```

### 6.3 Enum Definitions

**DataType** (from Data Preprocessing v2.0):
```typescript
enum DataType {
  LOGS_AND_ERRORS = "logs_and_errors",
  METRICS_AND_PERFORMANCE = "metrics_and_performance",
  STRUCTURED_CONFIG = "structured_config",
  SOURCE_CODE = "source_code",
  UNSTRUCTURED_TEXT = "unstructured_text",
  VISUAL_EVIDENCE = "visual_evidence"
}
```

**EvidenceSourceType** (from Evidence Architecture v1.1):
```typescript
enum EvidenceSourceType {
  LOG_FILE = "log_file",
  METRICS_DATA = "metrics_data",
  CONFIG_FILE = "config_file",
  CODE_REVIEW = "code_review",
  USER_OBSERVATION = "user_observation",
  SCREENSHOT = "screenshot",
  COMMAND_OUTPUT = "command_output",
  DATABASE_QUERY = "database_query",
  TRACE_DATA = "trace_data",
  API_RESPONSE = "api_response"
}
```

---

## 7. Frontend Integration

### 7.1 Conversation UX Implementation

**Goal**: Display uploads as natural conversation turns with AI responses.

**Current Issue**: Frontend shows toast notifications instead of conversation messages.

**Required Changes**:

```typescript
// src/shared/ui/SidePanelApp.tsx

interface ConversationItem {
  id: string;
  question?: string;  // User message
  response?: string;  // AI message
  timestamp: string;
  responseType?: string;
  confidenceScore?: number;
  sources?: Array<any>;
}

const handleDataUpload = async (
  data: string | File,
  dataSource: "text" | "file" | "page"
): Promise<void> => {
  // 1. Convert to File if needed
  const fileToUpload = data instanceof File
    ? data
    : new File(
        [new Blob([data])],
        `${dataSource}-content.txt`,
        { type: "text/plain" }
      );
  
  // 2. Upload to backend
  const uploadResponse = await uploadDataToCase(
    activeCaseId,
    sessionId,
    fileToUpload,
    // NEW: Include source metadata (optional)
    {
      source_type: dataSource === "file" ? "file_upload" : 
                   dataSource === "text" ? "text_paste" : 
                   "page_capture",
      source_url: dataSource === "page" ? currentPageUrl : undefined,
    }
  );
  
  // 3. Add USER message to conversation
  const userMessage: ConversationItem = {
    id: `upload-${Date.now()}`,
    question: `📎 Uploaded: ${uploadResponse.preprocessing.data_type} - ${fileToUpload.name} (${formatFileSize(uploadResponse.evidence.content_size_bytes)})`,
    timestamp: new Date().toISOString(),
  };
  
  // 4. Add AI RESPONSE message
  const aiMessage: ConversationItem = {
    id: `response-${Date.now()}`,
    response: uploadResponse.agent_response.content,
    timestamp: new Date().toISOString(),
    responseType: uploadResponse.agent_response.response_type,
    confidenceScore: uploadResponse.agent_response.confidence_score,
    sources: uploadResponse.agent_response.sources,
  };
  
  // 5. Update conversation state
  setConversation(prev => [...prev, userMessage, aiMessage]);
  
  // 6. Scroll to bottom
  scrollToBottom();
  
  // NO toast notification - conversation message is the feedback
};
```

### 7.2 Upload Button Handler

```typescript
// src/shared/ui/components/ChatWindow.tsx

const handleFileUpload = async (file: File) => {
  try {
    await handleDataUpload(file, "file");
    // Success - conversation updated automatically
  } catch (error) {
    // Only show toast on ERROR
    toast.error("Failed to upload file: " + error.message);
  }
};
```

### 7.3 Paste Detection (Optional Enhancement)

**Frontend can hint machine data to backend**:

```typescript
const handlePaste = async (e: ClipboardEvent) => {
  const pastedText = e.clipboardData?.getData("text");
  
  if (!pastedText || pastedText.length < 500) {
    // Short paste - let user submit normally
    return;
  }
  
  // Large paste - check if looks like machine data
  const looksMachineData = detectMachineDataPatterns(pastedText);
  
  if (looksMachineData) {
    // Show confirmation dialog
    const confirmed = await confirm(
      "This looks like log data. Submit as data file for analysis?"
    );
    
    if (confirmed) {
      // Submit via data upload path with hint
      await handleDataUpload(pastedText, "text");
      e.preventDefault();
      return;
    }
  }
  
  // Otherwise, let normal paste happen
};

function detectMachineDataPatterns(text: string): boolean {
  // Basic client-side detection (backend does full classification)
  const hasTimestamps = /\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}/.test(text);
  const hasLogLevels = /\b(ERROR|FATAL|WARN|INFO)\b/.test(text);
  const hasStackTrace = /at\s+[\w.$]+\(.*?:\d+\)/.test(text);
  
  return hasTimestamps || hasLogLevels || hasStackTrace;
}
```

### 7.4 Remove Legacy UI Elements

**Delete**: "Session Data" section in ChatWindow.tsx

**Before**:
```typescript
// Lines 419-441 (REMOVE THIS)
<div className="session-data">
  <h3>Uploaded Files</h3>
  {sessionData.map(data => (
    <div key={data.id}>{data.filename}</div>
  ))}
</div>
```

**After**: No separate section - uploads appear in conversation.

---

## 8. Backend Integration

### 8.1 Service Dependencies

**Upload Endpoint Dependencies**:
```python
from faultmaven.services.preprocessing.preprocessing_service import PreprocessingService
from faultmaven.services.evidence.evidence_service import EvidenceService
from faultmaven.services.agentic.orchestration.agent_service import AgentService
from faultmaven.services.case.case_service import ICaseService
```

**Query Endpoint Dependencies**:
```python
from faultmaven.services.preprocessing.query_classifier import QueryClassifier
from faultmaven.services.preprocessing.preprocessing_service import PreprocessingService
from faultmaven.services.agentic.orchestration.agent_service import AgentService
```

### 8.2 Integration Flow Example

**Complete Backend Flow**:

```python
# In routes/case.py

@router.post("/{case_id}/data")
async def upload_case_data(
    case_id: str,
    file: UploadFile,
    session_id: str = Form(...),
    source_metadata: Optional[str] = Form(None),
    # ... dependencies ...
):
    """Upload data with full pipeline integration."""
    
    # Step 1: Preprocessing (delegates to Data Preprocessing Architecture)
    preprocessing_result = await preprocessing_service.process_upload(
        file=file,
        case_id=case_id,
        source_metadata=parse_source_metadata(source_metadata),
    )
    # Output: PreprocessingResult
    #   - data_type: DataType enum (LOGS_AND_ERRORS, etc.)
    #   - summary: <500 chars
    #   - full_extraction: complete insights
    #   - content_ref: s3://...
    
    # Step 2: Evidence Classification (delegates to Evidence Architecture)
    case = await case_service.get_case_with_relations(case_id)
    
    classification = await evidence_service.classify_evidence(
        user_input=preprocessing_result.summary,
        case=case,
    )
    # Output: EvidenceClassification
    #   - matched_requirement_ids: []
    #   - relevant_hypothesis_ids: ["hyp_001"]
    #   - hypothesis_support: {"hyp_001": "strongly_supports"}
    #   - completeness: "complete"
    #   - form: "document"
    #   - evidence_type: "strongly_supports"
    #   - user_intent: "providing_evidence"
    
    # Step 3: Evidence Creation (delegates to Evidence Architecture)
    evidence = await evidence_service.create_evidence(
        preprocessing_result=preprocessing_result,
        classification=classification,
        case_id=case_id,
        phase=case.current_phase,
        uploaded_by=current_user.email,
    )
    # Output: Evidence object
    #   - evidence_id: "ev_abc"
    #   - summary: preprocessing_result.summary
    #   - content_ref: preprocessing_result.content_ref
    #   - source_type: LOG_FILE (mapped from DataType.LOGS_AND_ERRORS)
    #   - form: DOCUMENT
    #   - fulfills_requirement_ids: classification.matched_requirement_ids
    
    # Step 4: Hypothesis Analysis (delegates to Evidence Architecture)
    case = await evidence_service.analyze_evidence_impact(
        evidence=evidence,
        classification=classification,
        case=case,
    )
    # Updates: Hypothesis objects
    #   - hypothesis.evidence_links["ev_abc"] = EvidenceLink(
    #       stance="strongly_supports",
    #       reasoning="Pool at 95% confirms theory",
    #       completeness=0.9,
    #     )
    #   - hypothesis.status = VALIDATED (if strong evidence)
    
    # Step 5: Agent Response Generation
    agent_response = await agent_service.generate_evidence_analysis_response(
        evidence=evidence,
        case=case,
    )
    # Output: AgentResponse
    #   - content: "I've analyzed your log file. I found..."
    #   - response_type: ANSWER
    #   - confidence_score: 0.85
    
    # Step 6: Background Storage (fire-and-forget)
    if should_cache_in_vector_db(preprocessing_result):
        background_tasks.add_task(
            store_in_vector_db,
            case_id=case_id,
            evidence_id=evidence.evidence_id,
            preprocessed_content=preprocessing_result.full_extraction,
        )
    
    # Step 7: Return combined response
    return DataUploadResponse(
        evidence_id=evidence.evidence_id,
        preprocessing=PreprocessingSummary.from_result(preprocessing_result),
        evidence=EvidenceSummary.from_evidence(evidence),
        agent_response=agent_response,
    )
```

### 8.3 Error Handling

**Preprocessing Errors**:
```python
try:
    preprocessing_result = await preprocessing_service.process_upload(...)
except FileTooLargeError as e:
    raise HTTPException(
        status_code=400,
        detail={
            "error": "file_too_large",
            "file_size": e.file_size,
            "max_size": e.max_size,
            "suggestions": e.suggestions
        }
    )
except UnsupportedFileTypeError as e:
    raise HTTPException(
        status_code=415,
        detail={
            "error": "unsupported_file_type",
            "content_type": e.content_type,
            "allowed_types": e.allowed_types
        }
    )
```

**Evidence Creation Errors**:
```python
try:
    evidence = await evidence_service.create_evidence(...)
except CaseNotFoundError:
    raise HTTPException(status_code=404, detail="Case not found")
except PhaseValidationError as e:
    # Evidence collection not allowed in current phase
    raise HTTPException(
        status_code=400,
        detail=f"Cannot collect evidence in Phase {e.current_phase}"
    )
```

---

## 9. Source Metadata Enhancement

### 9.1 Overview

**Status**: Optional enhancement (backward compatible)

**Purpose**: Provide richer context about data origin for better AI analysis.

**Use Cases**:
- Page capture: AI knows it's from live status page
- File upload: AI knows it's local vs remote file
- Text paste: AI knows it was manually extracted

### 9.2 Schema

```python
class SourceMetadata(BaseModel):
    """Optional metadata about data origin"""
    
    source_type: Literal["file_upload", "text_paste", "page_capture"]
    
    # For page captures
    source_url: Optional[str] = Field(
        None,
        description="URL if from page capture"
    )
    captured_at: Optional[str] = Field(
        None,
        description="ISO 8601 timestamp if from page capture"
    )
    
    # For all sources
    user_description: Optional[str] = Field(
        None,
        max_length=200,
        description="User's description of the data"
    )
```

### 9.3 Frontend Implementation

```typescript
// In lib/api.ts

interface SourceMetadata {
  source_type: "file_upload" | "text_paste" | "page_capture";
  source_url?: string;
  captured_at?: string;
  user_description?: string;
}

export async function uploadDataToCase(
  caseId: string,
  sessionId: string,
  file: File,
  sourceMetadata?: SourceMetadata  // NEW: Optional parameter
): Promise<DataUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("session_id", sessionId);
  
  // NEW: Include source metadata if provided
  if (sourceMetadata) {
    formData.append("source_metadata", JSON.stringify(sourceMetadata));
  }
  
  const response = await fetch(
    `${API_BASE_URL}/api/v1/cases/${caseId}/data`,
    {
      method: "POST",
      body: formData,
      headers: {
        "Authorization": `Bearer ${token}`,
      },
    }
  );
  
  return response.json();
}
```

**Usage in Handlers**:

```typescript
// File Upload
const handleFileUpload = async (file: File) => {
  await uploadDataToCase(caseId, sessionId, file, {
    source_type: "file_upload",
    user_description: "User selected local file"
  });
};

// Text Paste
const handleTextPaste = async (text: string) => {
  const file = new File([new Blob([text])], "pasted-text.txt");
  await uploadDataToCase(caseId, sessionId, file, {
    source_type: "text_paste",
    user_description: "User pasted text content"
  });
};

// Page Capture
const handlePageCapture = async (pageContent: string, url: string) => {
  const file = new File([new Blob([pageContent])], "captured-page.html");
  await uploadDataToCase(caseId, sessionId, file, {
    source_type: "page_capture",
    source_url: url,
    captured_at: new Date().toISOString(),
    user_description: `Captured from ${url}`
  });
};
```

### 9.4 Backend Integration

```python
async def upload_case_data(
    case_id: str,
    file: UploadFile,
    session_id: str = Form(...),
    source_metadata: Optional[str] = Form(None),  # NEW: JSON string
    # ...
):
    # Parse source metadata
    metadata = None
    if source_metadata:
        try:
            metadata = SourceMetadata.parse_raw(source_metadata)
        except Exception:
            # Invalid JSON - ignore gracefully
            pass
    
    # Pass to preprocessing
    preprocessing_result = await preprocessing_service.process_upload(
        file=file,
        case_id=case_id,
        source_metadata=metadata,  # NEW: Pass through
    )
```

**Enhancement in Preprocessing** (optional):

```python
# In preprocessing_service.py

async def process_upload(
    self,
    file: UploadFile,
    case_id: str,
    source_metadata: Optional[SourceMetadata] = None,
) -> PreprocessingResult:
    """Process upload with optional source context."""
    
    # Extract content
    content = await file.read()
    
    # Classify data type
    data_type = self.classifier.classify(content, file.filename)
    
    # Build enhanced context
    context = {"case_id": case_id, "data_type": data_type}
    
    if source_metadata:
        context["source_type"] = source_metadata.source_type
        
        # Page capture gets URL context
        if source_metadata.source_type == "page_capture":
            context["source_url"] = source_metadata.source_url
            context["is_live_page"] = True
            # Extract service hint from URL
            context["service_hint"] = self._extract_service_from_url(
                source_metadata.source_url
            )
    
    # Extract with enhanced context
    result = await self.extractor.extract(content, data_type, context)
    
    # Store source metadata in result
    result.source_metadata = source_metadata
    
    return result
```

**Agent Prompt Enhancement**:

```python
# When source_metadata is available:
"""
The user has captured the status page from https://api.myapp.com/status.
This appears to be a live API status page showing current system health.

[Preprocessed page content here...]

Based on the status information shown on the page, what issues do you see?
"""

# vs without source metadata:
"""
Here is some HTML content the user provided:

[Preprocessed content here...]

What issues do you see in this data?
```

### 9.5 Benefits

| Benefit | Example |
|---------|---------|
| **Better Context** | "Looking at the status page from api.myapp.com..." vs "Looking at this HTML..." |
| **Service Discovery** | URL reveals service name for knowledge base search |
| **Temporal Context** | Page captures include timestamp for timeline correlation |
| **Intent Understanding** | Paste vs file vs page indicates user's mental model |
| **Richer Responses** | Agent can reference: "The page you captured shows..." |

---

## 10. Testing Requirements

### 10.1 Scenario-Based Tests

**Test Scenario 1: Explicit File Upload (New Case)**
```
GIVEN: User opens extension, no active case
WHEN: User clicks "Upload File", selects application.log (45KB)
THEN:
  1. Frontend creates case automatically
  2. POST /data uploads file
  3. Preprocessing extracts crime scene (200:1 compression)
  4. Evidence created with source_type=LOG_FILE
  5. Hypothesis updated with evidence link
  6. Agent generates response: "Found 127 errors..."
  7. Frontend shows two messages:
     - User: "📎 Uploaded: application.log (45KB)"
     - AI: "I've analyzed your application log..."
```

**Test Scenario 2: Explicit Text Upload (Mid-Conversation)**
```
GIVEN: Active case with conversation history
WHEN: User pastes 3KB query log, clicks "Submit Data"
THEN:
  1. POST /data uploads text
  2. Preprocessing summarizes query patterns
  3. Evidence created with source_type=DATABASE_QUERY
  4. Agent response references earlier conversation
  5. Frontend appends to conversation (not new case)
```

**Test Scenario 3: Implicit Detection (Large Paste)**
```
GIVEN: Active case
WHEN: User pastes 15KB of application logs into query box
THEN:
  1. POST /queries with large content
  2. QueryClassifier detects machine data (confidence 0.85)
  3. Routes to preprocessing pipeline (same as explicit)
  4. Evidence created
  5. Agent response: "I've analyzed your log submission..."
  6. Frontend shows as conversation messages
```

**Test Scenario 4: Normal Query with Code (No Upload)**
```
GIVEN: Active case
WHEN: User pastes 500 char function with question
THEN:
  1. POST /queries
  2. QueryClassifier: NOT machine data (confidence 0.80)
  3. Processes as normal query (no evidence creation)
  4. Agent analyzes code in query context
  5. Agent response: "The issue in your code is on line 12..."
```

**Test Scenario 5: Page Capture with Source Metadata**
```
GIVEN: Active case
WHEN: User captures page from https://status.myapp.com
THEN:
  1. POST /data with source_metadata.source_url
  2. Preprocessing extracts page content
  3. Agent prompt includes: "status page from myapp.com"
  4. Agent response references the source
  5. Frontend shows: "📎 Captured: https://status.myapp.com"
```

### 10.2 Unit Tests

**QueryClassifier Tests**:
```python
def test_explicit_hint_overrides_patterns():
    """UI hint should always take precedence"""
    result = classifier.classify(
        query="What's the weather?",
        query_type="machine_data"  # Explicit hint
    )
    assert result.is_machine_data == True
    assert result.confidence == 1.0

def test_pattern_detection_logs():
    """Strong log patterns should detect machine data"""
    log_content = """
    2025-11-01 14:30:00 ERROR NullPointerException
    2025-11-01 14:30:01 ERROR Connection timeout
    2025-11-01 14:30:02 FATAL Database unavailable
    """
    result = classifier.classify(query=log_content)
    assert result.is_machine_data == True
    assert result.confidence >= 0.70
    assert result.data_type == DataType.LOGS_AND_ERRORS

def test_short_question_not_detected():
    """Short conversational text should not be detected"""
    result = classifier.classify(query="Why is my app slow?")
    assert result.is_machine_data == False
    assert result.confidence >= 0.80
```

**Integration Tests**:
```python
async def test_upload_creates_evidence_and_updates_hypothesis():
    """Full pipeline: upload → evidence → hypothesis update"""
    # Upload log file
    response = await client.post(
        f"/api/v1/cases/{case_id}/data",
        files={"file": ("app.log", log_content)},
        data={"session_id": session_id}
    )
    
    assert response.status_code == 201
    data = response.json()
    
    # Verify evidence created
    assert data["evidence_id"].startswith("ev_")
    assert data["preprocessing"]["data_type"] == "logs_and_errors"
    
    # Verify hypothesis updated
    case = await case_service.get_case(case_id)
    hypothesis = case.theories[0]
    assert data["evidence_id"] in hypothesis.evidence_links
    assert hypothesis.evidence_links[data["evidence_id"]].stance in [
        "strongly_supports", "supports"
    ]
```

### 10.3 Frontend Tests

```typescript
test("file upload adds conversation messages", async () => {
  const file = new File(["log content"], "app.log");
  
  await handleDataUpload(file, "file");
  
  // Verify two messages added
  expect(conversation.length).toBe(2);
  
  // User message
  expect(conversation[0].question).toContain("Uploaded: app.log");
  
  // AI response
  expect(conversation[1].response).toContain("analyzed");
});

test("large paste triggers detection", async () => {
  const largeLog = "ERROR ".repeat(1000);  // 6000 chars
  
  await handlePaste({clipboardData: {getData: () => largeLog}});
  
  // Verify detection dialog shown
  expect(mockConfirm).toHaveBeenCalledWith(
    expect.stringContaining("log data")
  );
});
```

---

## 11. Cross-References

### 11.1 Related Documents

**Primary Dependencies**:
- [Data Preprocessing Architecture v2.0](./data-preprocessing-architecture.md)
  - Section 3: Synchronous Pipeline (Steps 1-4)
  - Section 4: Async Background Pipeline (Vector DB)
  - Section 6: Data Type Specifications
  - Section 7: Output Formats (PreprocessingResult)

- [Evidence Architecture v1.1](./evidence-architecture.md)
  - Section 4: Data Models - Layer 2 (Collection Workflow)
  - Section 6: Evidence Evaluation (Classification, Analysis)
  - Section 7: Investigation Strategies (Active Incident vs Post-Mortem)
  - Section 9: Integration Points

**Secondary References**:
- [Investigation State and Control Framework](./investigation-phases-and-ooda-integration.md)
  - Phase definitions and transitions
  - Working conclusion and progress tracking

- [API Specification](../api/openapi.locked.yaml)
  - POST /api/v1/cases/{case_id}/data (line 2294)
  - POST /api/v1/cases/{case_id}/queries (line 2062)

### 11.2 Key Integration Points

**Data Flow Summary**:
```
User Submits Data (THIS DOC)
    ↓
Preprocessing Extracts Insights (Data Preprocessing v2.0)
    ↓ PreprocessingResult
Evidence Classification & Creation (Evidence Architecture v1.1)
    ↓ Evidence + Updated Hypotheses
Agent Response Generation (Agent Service)
    ↓ AgentResponse
Frontend Conversation Update (THIS DOC)
```

**Schema Flow**:
```
File/Text Input (THIS DOC)
    ↓
PreprocessingResult (Data Preprocessing v2.0)
    - data_type: DataType enum
    - summary: <500 chars
    - full_extraction: complete insights
    - content_ref: S3 URI
    ↓
Evidence (Evidence Architecture v1.1)
    - summary: from PreprocessingResult.summary
    - content_ref: from PreprocessingResult.content_ref
    - source_type: mapped from DataType
    - evidence_links: updated via hypothesis analysis
    ↓
AgentResponse (Agent Service)
    - content: conversational insights
    - sources: includes evidence_id
```

### 11.3 Design Consistency Checklist

✅ **Data Type Names**: Uses canonical `DataType` enum from Data Preprocessing v2.0  
✅ **Evidence Source Types**: Maps to `EvidenceSourceType` enum from Evidence v1.1  
✅ **PreprocessingResult Schema**: References Data Preprocessing v2.0 Section 7  
✅ **Evidence Evaluation**: Delegates to Evidence Architecture v1.1 Section 6  
✅ **Vector DB Role**: Clarified as async background (Data Preprocessing v2.0 Section 4)  
✅ **No Duplicate Specs**: References other docs instead of redefining algorithms  

---

## 12. Summary

### 12.1 Key Design Decisions

1. **Dual Path Convergence**: Explicit upload and implicit detection use the same processing pipeline
2. **Conversational UX**: Data submissions appear as conversation messages, not separate UI
3. **Layered Architecture**: Clear separation between API/UX, transformation, and analysis layers
4. **Backend-Driven**: AI generates insights; frontend displays them
5. **Context Preservation**: Evidence persists in case state and links to hypotheses
6. **Optional Enhancements**: Source metadata enriches analysis without breaking compatibility

### 12.2 Implementation Priorities

**High Priority**:
1. ✅ Frontend conversation integration (display AI responses in chat)
2. ✅ Backend preprocessing integration (wire to endpoint)
3. ✅ Evidence creation pipeline (link preprocessing → evidence → hypothesis)
4. ✅ Agent response generation with evidence context

**Medium Priority**:
5. ✅ Query classification (3-tier: hints → patterns → heuristics)
6. ✅ Frontend UI hint support (query_type, is_raw_content)
7. ⚠️ Remove legacy "Session Data" UI section

**Low Priority (Optional)**:
8. 🔲 Source metadata support (backward compatible enhancement)
9. 🔲 Client-side paste detection with confirmation dialog
10. 🔲 Vision model integration for screenshots (stub exists)

### 12.3 Success Criteria

**User Experience**:
- ✅ Data upload appears as natural conversation turn
- ✅ AI provides immediate analysis (not just "upload successful")
- ✅ No separate "uploaded files" UI clutter
- ✅ Both explicit upload and paste detection work seamlessly

**Technical Integration**:
- ✅ Preprocessing correctly extracts insights (Crime Scene, Anomaly Detection)
- ✅ Evidence objects link to hypotheses with reasoning
- ✅ Hypothesis status updates based on evidence (PROPOSED → VALIDATED)
- ✅ Agent responses reference uploaded evidence

**API Compliance**:
- ✅ Response schemas match OpenAPI specification
- ✅ Uses canonical enum values from other designs
- ✅ Proper error handling with user-friendly messages
- ✅ Backward compatible (optional fields don't break existing clients)

---

**Document Version**: 4.0  
**Last Updated**: 2025-11-01  
**Status**: Production Ready  
**Authors**: System Architecture Team  
**Related Documents**: Data Preprocessing v2.0, Evidence Architecture v1.1

---

**Changes from v3.2**:
1. ✅ Aligned with Data Preprocessing Architecture v2.0 schemas
2. ✅ Aligned with Evidence Architecture v1.1 evaluation pipeline
3. ✅ Removed redundant preprocessing implementation specs
4. ✅ Clarified two-stage classification (data type vs evidence)
5. ✅ Updated architecture diagrams with correct layer separation
6. ✅ Added proper cross-references to authoritative sources
7. ✅ Clarified vector DB role as async background storage
8. ✅ Used canonical enum values throughout (DataType, EvidenceSourceType)
9. ✅ Enhanced source metadata as optional backward-compatible feature
10. ✅ Improved testing scenarios with complete pipeline validation