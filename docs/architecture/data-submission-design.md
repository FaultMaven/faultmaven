# Data Submission Handling - Implementation Specification (v3.0)

## Problem Statement

**Scenario:** Users submit data (files, logs, error traces, metrics) either through dedicated upload UI or by pasting into the query box.

**Evolution:**
- **v1.0**: Classification engine treated pasted data as queries, overwhelming LLM context
- **v2.0**: Automatic detection via length/pattern matching, routed to data ingestion
- **v3.0** (Current): Dual submission paths with conversational AI responses

**Current Solution (v3.0):**
- **Explicit Upload**: Dedicated UI for file/text/page uploads via `POST /api/v1/cases/{case_id}/data`
- **Implicit Detection**: Pasted data auto-detected via `POST /api/v1/cases/{case_id}/queries` with smart routing
- **Conversational UX**: All data submissions appear as conversation turns with AI analysis responses

---

## Design Philosophy

### Core Principles

1. **Conversational Flow**: Data uploads are conversation messages, not separate UI elements
2. **Backend-Driven Analysis**: Backend analyzes data and generates insights, not just "upload successful"
3. **Context-Aware**: Data is processed in the context of the ongoing conversation
4. **Unified Experience**: File upload, text paste, and page capture all work the same way

### User Experience

**User uploads data** → **Frontend shows upload message** → **Backend processes & analyzes** → **AI responds with insights**

```
User: 📎 Uploaded: application.log (45KB)

AI: I've analyzed your application log file. I found 127 error entries, with the most critical being:
- 45 database connection timeouts starting at 14:23 UTC
- 12 out-of-memory exceptions in the cache service
Would you like me to help diagnose the connection timeout issue first?
```

---

## Architecture: Dual Submission Paths

### Path 1: Explicit Data Upload (Designed for Files)

**Endpoint**: `POST /api/v1/cases/{case_id}/data`

**Use Case**: User uses dedicated "Upload" UI in browser extension

**Why Designed for Files:**
This path is architecturally designed for potentially large data submissions because it includes:
1. **Classification First**: Data type is identified before LLM involvement
2. **Intelligent Preprocessing**: Large files are condensed into LLM-digestible summaries
3. **Structured Analysis**: Domain-specific processors extract key information
4. **LLM-Ready Context**: Only preprocessed summaries (not raw data) sent to LLM

**Pipeline**: `Upload → Classify → Preprocess → Generate Summary → LLM Analysis → Response`

**API Spec** ([openapi.locked.yaml:2294-2350](../api/openapi.locked.yaml#L2294)):
```yaml
POST /api/v1/cases/{case_id}/data
  Parameters:
    - case_id: path (required)
    - description: query (optional)
    - expected_type: query (optional)
  Request Body: multipart/form-data
    - file: binary (required)
    - session_id: string (required)
  Response: 201 Created
    Headers:
      - Location: /api/v1/cases/{case_id}/data/{data_id}
      - X-Correlation-ID: <uuid>
    Body:
      {
        "data_id": "data_...",
        "filename": "application.log",
        "file_size": 45000,
        "data_type": "log_file",
        "processing_status": "completed",
        "agent_response": {
          "response_type": "ANSWER",
          "content": "I've analyzed your application log...",
          "confidence_score": 0.85,
          "sources": [...]
        }
      }
```

**Processing Flow**:
1. User selects file/pastes text/captures page content
2. Frontend converts to File object, uploads via FormData
3. **Backend classifies data type** (log_file, metrics_data, error_report, etc.)
4. **Backend preprocesses** - Extracts key information, reduces size for LLM
5. **Backend analyzes** - LLM processes preprocessed summary (not raw data)
6. Backend generates conversational AI response with insights
7. Frontend displays user message + AI response in conversation

**Key Advantage**: Large files (50K+ lines) are preprocessed into ~8K char summaries before LLM analysis, preventing context overflow while preserving critical information.

**Current Implementation Status**:
- ✅ Endpoint exists ([case.py:2094-2149](../../faultmaven/api/v1/routes/case.py#L2094))
- ⚠️ Returns mock data (not real file processing)
- ❌ Does not return `agent_response` field (needs implementation)

---

### Path 2: Implicit Detection (Paste in Query Box)

**Endpoint**: `POST /api/v1/cases/{case_id}/queries`

**Use Case**: User pastes large log/data into the regular query box

**Why Different from Path 1:**
This path is designed for **query-like submissions** that may contain data. It uses pattern detection to decide whether to:
- Route to Path 1's preprocessing pipeline (if detected as data submission)
- Process as a normal query with code/context (if below threshold)

**Detection → Route**: If patterns indicate data submission, internally routes to same preprocessing pipeline as Path 1.

**API Spec** ([openapi.locked.yaml:2062-2125](../api/openapi.locked.yaml#L2062)):
```yaml
POST /api/v1/cases/{case_id}/queries
  Request Body:
    {
      "session_id": "...",
      "query": "<user pasted large log content>",
      "context": {...}
    }
  Response:
    - 201 Created (sync processing)
    - 202 Accepted (async processing for >10K chars)
```

**Detection Logic** ([classification_engine.py:498-661](../../faultmaven/services/agentic/engines/classification_engine.py)):
```python
# Hard limit threshold
HARD_LIMIT = 10000  # Characters

if len(query) > HARD_LIMIT:
    # Auto-route to data ingestion
    return {
        "should_route_to_upload": True,
        "confidence": 1.0,
        "reason": f"Message length {len(query)} exceeds hard limit"
    }
else:
    # Pattern detection for hints
    detected_patterns = detect_patterns(query)
    return {
        "should_route_to_upload": len(detected_patterns) >= 3,
        "data_indicators": detected_patterns,
        "confidence": calculate_confidence(detected_patterns)
    }
```

**Flow**:
1. User pastes large content into query box
2. Classification engine detects data submission
3. Routes to `data_service.ingest_data()` instead of normal query processing
4. Async analysis if >10K chars, sync if smaller
5. Returns AgentResponse with analysis results

**Implementation Status**:
- ✅ Classification engine detection implemented
- ✅ Routing logic in `submit_case_query()`
- ✅ Async/sync processing based on size
- ✅ Returns conversational responses

---

## Frontend Implementation

### Current State

**File**: `faultmaven-copilot/src/shared/ui/SidePanelApp.tsx:1717-1767`

**Issues**:
- ✅ Converts text/page to File objects
- ✅ Calls `uploadDataToCase()`
- ❌ Shows toast message instead of conversation message
- ❌ Has separate "Session Data" UI section (bad UX)

### Required Changes

#### 1. Add Conversation Messages for Uploads

```typescript
const handleDataUpload = async (
  data: string | File,
  dataSource: "text" | "file" | "page"
): Promise<{ success: boolean; message: string }> => {
  // Convert to File
  const fileToUpload = data instanceof File
    ? data
    : new File([new Blob([data])], `${dataSource}-content.txt`);

  // Upload to backend
  const uploadResponse = await uploadDataToCase(
    activeCaseId,
    sessionId,
    fileToUpload
  );

  // Add USER message to conversation
  const userMessage: ConversationItem = {
    id: `upload-${Date.now()}`,
    question: `📎 Uploaded: ${uploadResponse.filename} (${formatFileSize(uploadResponse.file_size)})`,
    timestamp: new Date().toISOString(),
  };

  // Add AI RESPONSE message from backend
  const aiMessage: ConversationItem = {
    id: `response-${Date.now()}`,
    response: uploadResponse.agent_response?.content || "Data received and processed.",
    timestamp: new Date().toISOString(),
    responseType: uploadResponse.agent_response?.response_type,
    confidenceScore: uploadResponse.agent_response?.confidence_score,
    sources: uploadResponse.agent_response?.sources,
  };

  // Update conversation
  setConversation(prev => [...prev, userMessage, aiMessage]);

  return { success: true, message: "" }; // No toast needed
};
```

#### 2. Remove "Session Data" Section

Delete lines 419-441 in `ChatWindow.tsx` - no longer needed.

#### 3. Update Type Definitions

```typescript
export interface DataUploadResponse {
  data_id: string;
  filename: string;
  file_size: number;
  data_type: string;
  processing_status: string;
  uploaded_at: string;
  agent_response?: AgentResponse;  // NEW: AI analysis
}
```

---

## Backend Implementation Requirements

### Endpoint: POST /api/v1/cases/{case_id}/data

**Current**: Stub implementation ([case.py:2094-2149](../../faultmaven/api/v1/routes/case.py#L2094))

**Required**:

```python
@router.post("/{case_id}/data", status_code=status.HTTP_201_CREATED)
@trace("api_upload_case_data")
async def upload_case_data(
    case_id: str,
    file: UploadFile = File(...),                    # NEW: Accept actual file
    session_id: str = Form(...),                     # NEW: From form data
    description: Optional[str] = Form(None),
    case_service: ICaseService = Depends(...),
    data_service: DataService = Depends(...),
    agent_service: AgentService = Depends(...),      # NEW: For AI response
    current_user: DevUser = Depends(require_authentication)
):
    """Upload data file to case and generate AI analysis."""

    # 1. Verify case exists and user has access
    case = await case_service.get_case(case_id, current_user.user_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # 2. Upload and process file
    file_content = await file.read()
    uploaded_data = await data_service.upload_data(
        session_id=session_id,
        case_id=case_id,
        file_content=file_content,
        file_name=file.filename,
        content_type=file.content_type,
        description=description
    )

    # 3. Classify data type and analyze
    data_analysis = await data_service.analyze_data(
        data_id=uploaded_data.data_id,
        session_id=session_id
    )

    # 4. Generate conversational AI response
    query_request = QueryRequest(
        session_id=session_id,
        query=f"I've uploaded {file.filename}. Please analyze it.",
        context={
            "uploaded_data_ids": [uploaded_data.data_id],
            "case_id": case_id,
            "data_insights": data_analysis
        }
    )

    agent_response = await agent_service.process_query_for_case(
        case_id,
        query_request
    )

    # 5. Return upload metadata + AI response
    return DataUploadResponse(
        data_id=uploaded_data.data_id,
        filename=file.filename,
        file_size=len(file_content),
        data_type=uploaded_data.data_type,
        processing_status="completed",
        uploaded_at=datetime.utcnow().isoformat() + 'Z',
        agent_response=agent_response  # NEW: Include AI analysis
    )
```

---

## Pattern Detection (Implicit Submission)

**File**: `classification_engine.py` lines 498-546

**Categories Implemented**:
```python
data_submission_patterns = {
    "timestamps": [
        (r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}', 2.0),
        (r'\[\d{4}-\d{2}-\d{2}.*?\]', 2.0),
        (r'\d{2}:\d{2}:\d{2}\.\d{3}', 2.0)
    ],
    "log_levels": [
        (r'(ERROR|WARN|INFO|DEBUG|TRACE).*\n.*\1', 2.0),
        (r'\b(ERROR|WARNING|INFO|DEBUG)\b.*\n.*\b(ERROR|WARNING|INFO|DEBUG)\b', 1.8)
    ],
    "stack_traces": [
        (r'at\s+[\w.$]+\(.*?:\d+\)', 2.0),  # Java
        (r'File ".*?", line \d+', 2.0),      # Python
        (r'^\s+at\s+.*\(.*:\d+:\d+\)$', 2.0), # JavaScript
        (r'Traceback \(most recent call last\)', 2.5),
        (r'Exception in thread', 2.0)
    ],
    "structured_data": [
        (r'^\s*\{[\s\S]*"[\w]+":\s*[\[\{"][\s\S]*\}\s*$', 1.8),
        (r'^<\?xml', 1.8),
        (r'^\w+:\s*\S+\s*\n\w+:\s*\S+', 1.5)
    ],
    "server_logs": [
        (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b.*\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', 1.5),
        (r'(GET|POST|PUT|DELETE|PATCH)\s+/\S+\s+HTTP/\d\.\d', 1.8)
    ],
    "metrics_data": [
        (r'(\d+\.\d+|\d+)\s+(cpu|memory|disk|network|latency|throughput)', 1.8),
        (r'\b(metric|gauge|counter|histogram)\s*:\s*\d+', 1.8),
        (r'(\d+\s*%|GB|MB|KB|ms|µs|ns)\s*.*\n.*(\d+\s*%|GB|MB|KB|ms|µs|ns)', 1.5)
    ]
}
```

---

## Testing Scenarios

### Scenario 1: Explicit File Upload (New Conversation)
```
User opens extension, no active case
User: [Clicks "Upload File", selects application.log]

Frontend creates case automatically
Frontend: 📎 Uploaded: application.log (45KB)

Backend analyzes file
AI: I've analyzed your application log file. I found 127 error entries...
```

### Scenario 2: Explicit Text Upload (Mid-Conversation)
```
User: My database is slow

AI: Let me help you diagnose. Could you share your slow query log?

User: [Pastes 3KB of slow query log text, clicks "Submit Data"]
Frontend: 📎 Uploaded: text-data.txt (3KB)

AI: Thank you. I've analyzed your slow query log. The main bottleneck is...
```

### Scenario 3: Implicit Detection (Paste Large Data)
```
User: [Pastes 15KB of application logs directly into query box]

Backend detects >10K chars
Backend: Auto-routes to data ingestion
Returns 202 Accepted, processes in background

User sees: "Analyzing your data submission..."
[Polls for completion]

AI: I've analyzed your 15,000 character log submission. Found...
```

### Scenario 4: Normal Query with Code (No Upload)
```
User: Why is this code failing? [pastes 500 char function]

Backend: Detects code but below threshold
Processes as normal query with code context

AI: The issue in your code is on line 12...
```

---

## Benefits of v3.0 Design

✅ **Natural Conversation**: Uploads appear inline, not in separate UI section
✅ **Immediate AI Feedback**: User sees analysis results, not just "upload successful"
✅ **Context Preservation**: Upload is part of conversation history
✅ **Cleaner UI**: No "Session Data" clutter
✅ **Dual Path Support**: Works for both explicit uploads and paste detection
✅ **Backend-Driven**: AI generates insights, frontend just displays
✅ **Consistent UX**: File, text, and page capture all work the same way
✅ **Scalable Processing**: Preprocessing handles large files without overwhelming LLM context
✅ **Intelligent Analysis**: Domain-specific processors extract relevant information per data type

---

## Source Metadata Enhancement (PROPOSED)

### Motivation

**Current Design**: Backend receives data without knowing its source (paste/page/file).

**Proposed Enhancement**: Include optional source metadata to provide richer context for AI analysis and better user experience.

### API Extension

#### Request Schema (Backend)

**Add optional `source_metadata` field to data upload**:

```python
# In faultmaven/api/v1/routes/case.py

from pydantic import BaseModel, Field
from typing import Optional, Literal

class SourceMetadata(BaseModel):
    """Metadata about where the data originated"""
    source_type: Literal["file_upload", "text_paste", "page_capture"]
    source_url: Optional[str] = Field(None, description="URL if from page capture")
    captured_at: Optional[str] = Field(None, description="Timestamp if from page capture")
    user_description: Optional[str] = Field(None, description="User's description of the data")

@router.post("/{case_id}/data", status_code=status.HTTP_201_CREATED)
async def upload_case_data(
    case_id: str,
    file: UploadFile = File(...),
    session_id: str = Form(...),
    source_metadata: Optional[str] = Form(None),  # JSON string
    description: Optional[str] = Form(None),
    # ... other params
):
    """Upload data with optional source context."""
    
    # Parse source metadata if provided
    metadata = None
    if source_metadata:
        try:
            metadata = SourceMetadata.parse_raw(source_metadata)
        except Exception:
            # Invalid metadata - ignore gracefully
            pass
    
    # Pass to data service
    uploaded_data = await data_service.upload_data(
        session_id=session_id,
        case_id=case_id,
        file_content=file_content,
        file_name=file.filename,
        source_metadata=metadata  # NEW
    )
```

#### Frontend Implementation

**Update data upload to include source metadata**:

```typescript
// In faultmaven-copilot/src/lib/api.ts

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
  sourceMetadata?: SourceMetadata  // NEW parameter
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

**Update data source handlers**:

```typescript
// In SidePanelApp.tsx

// 1. File Upload
const handleFileUpload = async (file: File) => {
  await uploadDataToCase(caseId, sessionId, file, {
    source_type: "file_upload",
    user_description: "User selected local file"
  });
};

// 2. Text Paste
const handleTextPaste = async (text: string) => {
  const file = new File([new Blob([text])], "pasted-text.txt");
  await uploadDataToCase(caseId, sessionId, file, {
    source_type: "text_paste",
    user_description: "User pasted text content"
  });
};

// 3. Page Capture
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

### Backend Processing

**How source metadata enhances analysis**:

```python
# In data_service.py

async def upload_data(
    self,
    session_id: str,
    case_id: str,
    file_content: bytes,
    file_name: str,
    source_metadata: Optional[SourceMetadata] = None  # NEW
) -> UploadedData:
    """Upload data with optional source context."""
    
    # 1. Classify data type (content-based)
    data_type = await self.classifier.classify(content, file_name)
    
    # 2. Enhance context with source metadata
    context = {
        "case_id": case_id,
        "data_type": data_type,
    }
    
    if source_metadata:
        context["source_type"] = source_metadata.source_type
        
        # Page capture gets URL context
        if source_metadata.source_type == "page_capture":
            context["source_url"] = source_metadata.source_url
            context["is_live_page"] = True
            # Could extract domain/service name from URL
            context["service_hint"] = self._extract_service_from_url(
                source_metadata.source_url
            )
    
    # 3. Preprocess with enhanced context
    preprocessed = await self.prepare_data_for_llm_analysis(
        data_id=data_id,
        data_type=data_type,
        raw_content=content,
        context=context  # Enhanced with source info
    )
    
    return uploaded_data
```

**Agent prompt enhancement**:

```python
# In agent prompts

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
"""
```

### Benefits of Source Metadata

| Benefit | Example |
|---------|---------|
| **Better Context** | "Looking at the status page from api.myapp.com..." vs "Looking at this HTML..." |
| **Service Discovery** | URL reveals service name for better knowledge base search |
| **Temporal Context** | Page captures include timestamp for timeline analysis |
| **Intent Understanding** | Paste vs file vs page indicates user's mental model |
| **Richer Responses** | Agent can reference the source: "The page you captured shows..." |
| **Audit Trail** | Track where data originated for compliance/debugging |

### Implementation Scope

#### Frontend Changes (Low Effort)
- ✅ Already creates File objects from all sources
- ➕ Add `SourceMetadata` interface
- ➕ Update 3 handler functions to include metadata
- ➕ Pass metadata to `uploadDataToCase()`

#### Backend Changes (Medium Effort)
- ➕ Add `SourceMetadata` model to `models/api.py`
- ➕ Update `upload_case_data()` to accept `source_metadata` form field
- ➕ Update `DataService.upload_data()` signature
- ➕ Enhance preprocessing context with source info
- ➕ Update agent prompts to mention source when available
- ➕ Optional: Extract service hints from URLs

### Rollout Strategy

**Phase 1: Optional Field** (Recommended)
- Add as optional field - backward compatible
- Frontend sends when available
- Backend gracefully handles absence
- No breaking changes

**Phase 2: Enhanced Processing**
- Use source metadata in preprocessing decisions
- Enhance agent prompts with source context
- Add service discovery from URLs

**Phase 3: Full Integration**
- Use source in analytics and insights
- Track which sources produce best data
- Optimize UX based on source patterns

### Testing Requirements

```python
# Backend tests
async def test_upload_with_page_capture_metadata():
    metadata = {
        "source_type": "page_capture",
        "source_url": "https://status.myapp.com",
        "captured_at": "2025-10-12T10:30:00Z"
    }
    
    response = await upload_case_data(
        case_id="case_123",
        file=mock_file,
        session_id="sess_456",
        source_metadata=json.dumps(metadata)
    )
    
    # Verify source context used in agent response
    assert "status page" in response.agent_response.content.lower()
    assert "myapp.com" in response.agent_response.content.lower()

async def test_upload_without_metadata():
    # Should work without source_metadata (backward compatible)
    response = await upload_case_data(
        case_id="case_123",
        file=mock_file,
        session_id="sess_456"
    )
    assert response.data_id is not None
```

```typescript
// Frontend tests
test("handlePageCapture includes source metadata", async () => {
  const url = "https://dashboard.myapp.com/metrics";
  const content = "<html>...</html>";
  
  await handlePageCapture(content, url);
  
  // Verify uploadDataToCase called with metadata
  expect(mockUpload).toHaveBeenCalledWith(
    expect.anything(),
    expect.anything(),
    expect.anything(),
    expect.objectContaining({
      source_type: "page_capture",
      source_url: url
    })
  );
});
```

### Decision

**Recommendation**: ✅ **Implement as optional enhancement**

**Pros**:
- ✅ Richer context for AI analysis
- ✅ Better user experience ("the page you captured...")
- ✅ Service discovery from URLs
- ✅ Enhanced audit trail
- ✅ No breaking changes (optional field)

**Cons**:
- ⚠️ Slightly more complex frontend code
- ⚠️ Backend needs to handle optional field gracefully
- ⚠️ Minimal benefit if not used in prompts/preprocessing

**Status**: 🔲 **Proposed Enhancement** - Not yet implemented, but architecturally sound and backward compatible.

---

## Data Preprocessing Implementation (TODO)

### Problem Statement

**Current State**: Data is ingested and basic insights are extracted, but **large data files cannot be sent directly to LLM** due to context limits.

**Why Path 1 is Designed for Files**: The explicit data upload endpoint includes a **preprocessing pipeline** that is essential for handling large files. This is why it's not just "recommended" but architecturally designed for file submissions.

**Required Preprocessing Layer**:
1. Extracts key information from uploaded data
2. Summarizes large datasets into LLM-digestible format (~8K chars max)
3. Preserves critical details (errors, anomalies, patterns)
4. Enables AI to provide meaningful analysis without context overflow
5. Domain-specific processing per data type (logs, metrics, traces, configs)

### Preprocessing Requirements by Data Type

#### 1. Log Files (DataType.LOG_FILE)

**Input**: Application logs (potentially 100K+ lines)

**Preprocessing Steps**:
```python
# TODO: Implement in data_service.py or new preprocessing module
async def preprocess_log_file(content: str, data_insights: Dict) -> str:
    """
    Extract key information from log file for LLM analysis

    Returns condensed summary with:
    - Error statistics (count, rate, top errors)
    - Timeline of critical events
    - Anomaly patterns detected
    - Sample error messages (5-10 examples)
    """

    # 1. Extract error statistics from insights
    error_summary = f"""
    Total lines: {insights['line_count']}
    Error count: {insights['error_count']} ({insights['error_rate']:.1f}%)
    Critical errors: {insights['critical_errors']}
    Time range: {insights['first_timestamp']} to {insights['last_timestamp']}
    """

    # 2. Get top error patterns
    top_errors = insights['error_patterns'][:10]  # Top 10 unique errors

    # 3. Extract timeline of anomalies
    anomaly_timeline = insights['anomalies_detected']

    # 4. Sample representative error messages
    error_samples = insights['error_samples'][:5]

    # 5. Build condensed context for LLM
    preprocessed = f"""
    LOG FILE ANALYSIS SUMMARY

    {error_summary}

    TOP ERROR PATTERNS:
    {format_error_patterns(top_errors)}

    ANOMALIES DETECTED:
    {format_anomalies(anomaly_timeline)}

    SAMPLE ERROR MESSAGES:
    {format_samples(error_samples)}
    """

    return preprocessed[:8000]  # Keep under token limit
```

#### 2. Metrics/Time-Series Data

**Input**: Performance metrics, resource utilization data

**Preprocessing Steps**:
```python
async def preprocess_metrics_data(content: str, data_insights: Dict) -> str:
    """
    Summarize metrics data for LLM analysis

    Returns:
    - Statistical summary (min, max, avg, p95, p99)
    - Anomaly detection results
    - Trend analysis (increasing, decreasing, stable)
    - Correlation patterns
    """
    # Extract key statistics
    # Identify spikes/drops
    # Correlate related metrics
    # Format for LLM consumption
```

#### 3. Stack Traces (DataType.ERROR_REPORT)

**Input**: Exception stack traces

**Preprocessing Steps**:
```python
async def preprocess_stack_trace(content: str, data_insights: Dict) -> str:
    """
    Extract critical information from stack trace

    Returns:
    - Exception type and message
    - Root cause frame (deepest relevant frame)
    - Call chain summary (top 5-10 frames)
    - Context variables if available
    """
    # Parse stack trace structure
    # Identify root cause
    # Extract relevant frames
    # Format for LLM
```

### Implementation Location

**Option 1: Extend DataService** (Recommended)
```python
# In data_service.py

async def prepare_data_for_llm_analysis(
    self,
    data_id: str,
    data_type: DataType,
    raw_content: str,
    data_insights: Dict
) -> str:
    """
    Preprocess data for LLM analysis based on data type

    Args:
        data_id: Data identifier
        data_type: Classified data type
        raw_content: Original uploaded content
        data_insights: Extracted insights from processor

    Returns:
        Preprocessed summary suitable for LLM (< 8K chars)
    """
    preprocessors = {
        DataType.LOG_FILE: self._preprocess_log_file,
        DataType.ERROR_REPORT: self._preprocess_stack_trace,
        # Add metrics, config, etc.
    }

    preprocessor = preprocessors.get(data_type, self._preprocess_generic)
    return await preprocessor(raw_content, data_insights)
```

**Option 2: New Preprocessing Module**
```python
# New file: faultmaven/core/preprocessing/data_preprocessor.py

class DataPreprocessor:
    """Intelligent data preprocessing for LLM analysis"""

    async def preprocess(
        self,
        data_type: DataType,
        content: str,
        insights: Dict
    ) -> PreprocessedData:
        """Route to appropriate preprocessor"""
```

### Integration Points

**Step 1: After data ingestion** (`data.py` line 268-272)
```python
# 3. Classify data type and analyze
data_analysis = await data_service.analyze_data(
    data_id=uploaded_data.data_id,
    session_id=session_id
)

# NEW: Preprocess data for LLM analysis
preprocessed_summary = await data_service.prepare_data_for_llm_analysis(
    data_id=uploaded_data.data_id,
    data_type=uploaded_data.data_type,
    raw_content=file_content,
    data_insights=data_analysis
)
```

**Step 2: Pass to agent** (line 275-288)
```python
# 4. Generate conversational AI response
query_request = QueryRequest(
    session_id=session_id,
    query=f"I've uploaded {file.filename}. Please analyze it and tell me what issues you found.",
    context={
        "uploaded_data_ids": [uploaded_data.data_id],
        "case_id": case_id,
        "data_type": uploaded_data.data_type,
        "preprocessed_data": preprocessed_summary,  # NEW: LLM-ready summary
        "full_insights": data_analysis  # Keep full insights for reference
    }
)

agent_response = await agent_service.process_query_for_case(
    case_id,
    query_request
)
```

### Agent Prompt Enhancement

**Update agent system prompt to handle preprocessed data**:
```python
# In agent prompts
"""
When the user uploads data (logs, metrics, traces), you will receive:
1. preprocessed_data: A condensed summary of key findings
2. data_type: The type of data uploaded
3. full_insights: Complete analysis results for reference

Your task:
- Analyze the preprocessed summary
- Identify root causes of errors/issues
- Provide actionable recommendations
- Ask clarifying questions if needed

DO NOT summarize the data - it's already summarized.
Focus on DIAGNOSIS and SOLUTIONS.
"""
```

### Testing Strategy

**Test Cases**:
1. Large log file (50K lines) → Verify preprocessing reduces to <8K chars
2. Metrics with anomalies → Verify anomalies are highlighted in summary
3. Complex stack trace → Verify root cause is identified
4. Multiple data types → Verify correct preprocessor is used

### Performance Considerations

- **Async Processing**: Preprocessing should be async for large files
- **Caching**: Cache preprocessed results keyed by data_id
- **Streaming**: Consider streaming for very large files
- **Timeouts**: Set reasonable timeouts (5-10s for preprocessing)

---

## Case Working Memory Integration

### Overview

All data uploaded through either submission path (explicit upload or implicit detection) is stored in the **Case Working Memory** vector store system. This is a critical architectural distinction that must be understood.

### Three Distinct Vector Store Systems

FaultMaven implements **three completely separate vector storage systems** (see [knowledge-base-architecture.md](./knowledge-base-architecture.md)):

1. **User Knowledge Base** (`user_{user_id}_kb`) - NOT YET IMPLEMENTED
   - Per-user permanent runbooks and procedures
   - Accessed via Knowledge Management UI (separate from troubleshooting)
   - Lives with user account indefinitely

2. **Global Knowledge Base** (`faultmaven_kb`) - IMPLEMENTED
   - System-wide documentation (admin-managed)
   - Shared across all users
   - Permanent system reference

3. **Case Working Memory** (`case_{case_id}`) - IMPLEMENTED ✅
   - **This is where uploaded data goes**
   - Troubleshooting evidence uploaded during active case
   - Ephemeral - tied to case lifecycle

### Data Upload → Case Working Memory Flow

When a user uploads data (file, text, or page capture) in the troubleshooting chat:

```
User Uploads Data
    ↓
POST /api/v1/cases/{case_id}/data
    ↓
CaseVectorStore.add_documents(case_id, documents)
    ↓
ChromaDB collection: case_{case_id}
    ↓
Documents available for Q&A via answer_from_document tool
    ↓
Case closes → Collection automatically deleted
```

### Key Characteristics

| Aspect | Details |
|--------|---------|
| **Collection Name** | `case_{case_id}` (e.g., `case_abc123`) |
| **Lifecycle** | Tied to case - deleted when case closes or archives |
| **Scope** | Case-specific - isolated per case_id |
| **Access Pattern** | QA sub-agent for detailed document questions |
| **LLM Provider** | SYNTHESIS_PROVIDER (dedicated for document Q&A) |
| **UI Context** | Active troubleshooting chat (document upload in chat) |

### Use Cases

**Case Working Memory is for:**
- ✅ Log files uploaded during troubleshooting
- ✅ Configuration files from affected system
- ✅ Stack traces and error dumps
- ✅ Performance metrics and traces
- ✅ Screenshots and diagnostic output
- ✅ Captured page content from error pages

**Case Working Memory is NOT for:**
- ❌ Permanent runbooks (use User KB when implemented)
- ❌ General reference documentation (use Global KB)
- ❌ Long-term knowledge storage
- ❌ Cross-case information sharing

### Example Workflow

```
1. User opens troubleshooting case for "Database timeout errors"

2. User uploads server.log (50KB) via chat upload button
   → Stored in case_abc123 collection
   → Preprocessed summary created
   → AI analyzes: "I found 127 timeout errors starting at 14:23 UTC..."

3. User asks: "What error is on line 1045?"
   → answer_from_document tool queries case_abc123 collection
   → QA sub-agent synthesizes answer from uploaded log

4. User resolves issue and closes case
   → Case status changes to CLOSED
   → case_abc123 collection automatically deleted via case lifecycle hook
   → Storage reclaimed
```

### Lifecycle Management

**Current Implementation** (as of 2025-10-16):

The Case Working Memory lifecycle is **tied to case status**, not time-based TTL:

```python
# Case states
ACTIVE → INVESTIGATING → RESOLVED → CLOSED
  ↓                                    ↓
Documents available            Documents deleted
```

**Deletion Triggers**:
- Case status transition to `CLOSED`
- Case status transition to `ARCHIVED`
- Case deleted by user

**NOT deleted when**:
- Case is still `ACTIVE` (even if weeks/months old)
- Case is in `INVESTIGATING` state
- Case is in `RESOLVED` state (awaiting final closure)

### Implementation Components

**Backend**:
- [case_vector_store.py](../../faultmaven/infrastructure/persistence/case_vector_store.py) - Case-specific document management
- [answer_from_document.py](../../faultmaven/tools/answer_from_document.py) - QA sub-agent for document queries
- [case_cleanup.py](../../faultmaven/infrastructure/tasks/case_cleanup.py) - Lifecycle-based cleanup

**Configuration**:
```bash
SYNTHESIS_PROVIDER=openai  # Dedicated LLM for QA sub-agent
CHROMADB_URL=http://chromadb.faultmaven.local:30080
# Lifecycle: Tied to case status (deleted when case closes/archives)
# Cleanup: Triggered by case state transitions, not time-based
```

### Relationship to Data Submission Paths

Both data submission paths (explicit upload and implicit detection) ultimately store documents in Case Working Memory:

```
Path 1: Explicit Upload
  POST /api/v1/cases/{case_id}/data
    → Classify → Preprocess → Store in case_{case_id}
    → Generate AI response with insights

Path 2: Implicit Detection
  POST /api/v1/cases/{case_id}/queries
    → Detect large paste → Route to data ingestion
    → Classify → Preprocess → Store in case_{case_id}
    → Generate AI response with insights
```

**Key Point**: Both paths result in the same outcome:
1. Data is preprocessed into LLM-ready summary
2. Documents stored in `case_{case_id}` collection
3. AI analyzes and responds with insights
4. User can ask follow-up questions via answer_from_document tool
5. Collection deleted when case closes

### Storage Architecture

```
ChromaDB Instance (chromadb.faultmaven.local:30080)
│
├── faultmaven_kb                    # Global KB (permanent)
│   └── [system-wide documentation]
│
├── case_abc123                      # Case Working Memory (active case)
│   └── [server.log uploaded by user]
│   └── [config.yaml uploaded by user]
│   └── [Lifecycle: deleted when case closes]
│
└── case_xyz789                      # Case Working Memory (active case)
    └── [stack_trace.txt uploaded by user]
    └── [Lifecycle: deleted when case closes]
```

### Access Control

| User Action | Case Working Memory |
|-------------|---------------------|
| **Upload document** | ✅ Own cases only |
| **Query documents** | ✅ Own cases only (via answer_from_document tool) |
| **Delete document** | ✅ Own cases only |
| **Access other user's data** | ❌ Forbidden (case isolation) |
| **Access after case closes** | ❌ Documents deleted with case |

### Future Enhancement: User Knowledge Base

When User Knowledge Base is implemented (Phase 2 - planned):

**Users will have TWO upload destinations**:

1. **Knowledge Management UI** (separate from troubleshooting)
   - Upload to `user_{user_id}_kb` (permanent)
   - For runbooks, procedures, best practices
   - Accessible across all cases

2. **Troubleshooting Chat UI** (current implementation)
   - Upload to `case_{case_id}` (ephemeral)
   - For incident-specific evidence
   - Deleted when case closes

**Critical Design Principle**: These must **never be confused** in implementation. They serve completely different purposes with different lifecycles.

---

## Implementation Checklist

### Backend (Priority: HIGH)

#### Core Data Processing (CRITICAL)
- [ ] **CRITICAL**: Implement data preprocessing layer in `data_service.py`
  - [ ] Add `prepare_data_for_llm_analysis()` method
  - [ ] Implement log file preprocessor
  - [ ] Implement metrics data preprocessor
  - [ ] Implement stack trace preprocessor
  - [ ] Add generic fallback preprocessor
- [ ] Update `upload_case_data()` to accept actual file uploads
- [ ] Integrate with `DataService.upload_data()` for file processing
- [ ] **CRITICAL**: Call preprocessing before passing to agent
- [ ] Integrate with `AgentService` to generate AI responses
- [ ] Return `agent_response` field in response body
- [ ] Update agent prompts to handle preprocessed data context
- [ ] Test file upload with various file types (.log, .txt, .json)

#### Source Metadata Enhancement (OPTIONAL - Proposed)
- [ ] Add `SourceMetadata` model to `models/api.py`
- [ ] Update `upload_case_data()` to accept optional `source_metadata` form field
- [ ] Update `DataService.upload_data()` to accept and store source metadata
- [ ] Enhance preprocessing context with source information
- [ ] Update agent prompts to mention source when available (e.g., "the page you captured from...")
- [ ] Optional: Implement URL-based service discovery
- [ ] Test with and without source metadata (backward compatibility)

### Frontend (Priority: HIGH)

#### Core Conversation Integration (CRITICAL)

- [x] Convert text/page to File objects in `handleDataUpload()`
- [ ] Add user message to conversation on upload
- [ ] Add AI response message from `uploadResponse.agent_response`
- [ ] Remove "Session Data" UI section from ChatWindow
- [ ] Update `DataUploadResponse` interface with `agent_response` field
- [ ] Test all three data sources (file, text, page)

#### Source Metadata Enhancement (OPTIONAL - Proposed)
- [ ] Add `SourceMetadata` interface to types
- [ ] Update `uploadDataToCase()` to accept optional `sourceMetadata` parameter
- [ ] Update `handleFileUpload()` to pass `{source_type: "file_upload"}`
- [ ] Update `handleTextPaste()` to pass `{source_type: "text_paste"}`
- [ ] Update `handlePageCapture()` to pass `{source_type: "page_capture", source_url, captured_at}`
- [ ] Ensure backward compatibility (metadata is optional)
- [ ] Test with and without metadata

### Testing

#### Core Functionality (CRITICAL)

- [ ] File upload → conversational response
- [ ] Text paste (via upload UI) → conversational response
- [ ] Page capture → conversational response
- [ ] Large paste (>10K) in query box → auto-detection + analysis
- [ ] Normal query with code snippet → processes normally

#### Source Metadata Testing (OPTIONAL)
- [ ] File upload with source metadata → metadata stored and used
- [ ] Page capture with URL → AI mentions source URL in response
- [ ] Text paste without metadata → works normally (backward compatible)
- [ ] Invalid metadata JSON → gracefully ignored
- [ ] Verify service discovery from URLs (e.g., "api.myapp.com" → "MyApp API")

---

## Files Modified

### Backend
1. **`faultmaven/api/v1/routes/case.py`**
   - Lines 2094-2149: `upload_case_data()` - needs real implementation
   - Lines 116-168: `_process_data_analysis()` - async processing function
   - Lines 1330-1416: Data submission routing in `submit_case_query()`

2. **`faultmaven/services/agentic/engines/classification_engine.py`**
   - Lines 498-546: Pattern detection dictionaries
   - Lines 939-989: `_detect_data_submission()` method
   - Lines 585-661: Integration into `classify_query()`

### Frontend
1. **`faultmaven-copilot/src/shared/ui/SidePanelApp.tsx`**
   - Lines 1717-1767: `handleDataUpload()` - needs conversation integration

2. **`faultmaven-copilot/src/shared/ui/components/ChatWindow.tsx`**
   - Lines 419-441: Remove "Session Data" section
   - Lines 151-183: `handleDataSubmit()` - updated for async response

3. **`faultmaven-copilot/src/lib/api.ts`**
   - Lines 1046-1074: `uploadDataToCase()` - update return type
   - Lines 190-200: `UploadedData` interface - add `agent_response` field

---

## API Specification Alignment

### OpenAPI Spec Status

✅ **`POST /api/v1/cases/{case_id}/data`** - Defined ([openapi.locked.yaml:2294](../api/openapi.locked.yaml#L2294))
- Security: BearerAuth required
- Request: multipart/form-data with file
- Response: 201 Created with Location header
- ⚠️ Response schema needs `agent_response` field added

✅ **`POST /api/v1/cases/{case_id}/queries`** - Defined ([openapi.locked.yaml:2062](../api/openapi.locked.yaml#L2062))
- Supports both 201 (sync) and 202 (async) responses
- Data detection handled transparently
- Returns AgentResponse format

---

## Conclusion

The v3.0 design provides a seamless conversational experience where data uploads are treated as natural conversation turns, with the backend responsible for analyzing data and generating insights. This eliminates UI clutter, provides immediate feedback, and maintains conversation context.

**Status**:
- ✅ Backend classification & routing implemented
- ⚠️ Backend file upload endpoint needs real implementation
- ⚠️ Backend preprocessing layer needs implementation (CRITICAL)
- ⚠️ Frontend needs conversation integration
- 🔲 Source metadata enhancement proposed (OPTIONAL)

**Next Steps**: 
1. **High Priority**: Implement backend preprocessing layer and frontend conversation updates
2. **Optional Enhancement**: Add source metadata support for richer context

**Implementation Order**:
1. Core preprocessing pipeline (enables basic file analysis)
2. Conversational integration (improves UX)
3. Source metadata (enhances context - can be added later)

---

**Last Updated**: 2025-10-12  
**Version**: 3.1 (Updated with source metadata proposal and preprocessing status clarification)  
**Authors**: System Architecture Team

## Appendix: Current Implementation Status

### Implementation Status: 3-Step Pipeline

**See**: [`data-preprocessing-design.md`](./data-preprocessing-design.md) for **authoritative design specification** (v4.0).

This document consolidates all data submission and preprocessing design decisions into a single comprehensive blueprint ready for implementation.

```
Step 1: Classify → Step 2: Preprocess → Step 3: LLM Analysis
    ✅ Done         ⚠️ Partial           ✅ Ready
```

#### Step 1: Classification ✅ DONE
- **Tool**: DataClassifier (`core/processing/classifier.py`)
- **Status**: Fully functional
- Routes to one of 4 main preprocessors: LOG_FILE, METRICS_DATA, ERROR_REPORT, CONFIG_FILE

#### Step 2: Preprocessing ⚠️ PARTIAL (CRITICAL GAP)

Each preprocessor is self-contained and produces LLM-ready summary:

| Preprocessor | Input | Output | Status |
|--------------|-------|--------|--------|
| `preprocess_logs()` | 50KB raw logs | 8K summary | ⚠️ Partial: has analysis, missing formatting |
| `preprocess_metrics()` | Metrics data | 6K summary | ❌ Not implemented |
| `preprocess_errors()` | Stack traces | 5K summary | ❌ Not implemented |
| `preprocess_config()` | Config files | 6K summary | ❌ Not implemented |

**Current Reality for LOG_FILE** (most common):
- ✅ LogProcessor extracts insights (errors, anomalies, patterns)
- ❌ Missing formatter to convert insights → LLM-ready summary
- **Result**: Can't send data to LLM yet (either overflows context or loses critical data)

**Libraries Needed**:
- Logs: None (use existing LogProcessor + string formatting)
- Metrics: pandas ✅, numpy ✅, scipy ✅ (all available)
- Errors: traceback ✅ (built-in)
- Config: pyyaml ➕, json ✅, configparser ✅

#### Step 3: LLM Analysis ✅ READY
- **Tool**: AgentService + AI Agent
- **Status**: Ready, waiting for Step 2 preprocessed summaries
- Receives LLM-ready summary (not raw data)
- Generates AgentResponse with diagnosis and recommendations

### What's Missing

**CRITICAL GAP**: Step 2 preprocessing formatters

**When you upload a 50KB log file today:**
```
1. Step 1: Classification ✅ → DataType.LOG_FILE
2. Step 2: LogProcessor extracts insights ✅
3. Step 2: Formatter missing ❌ → Can't create LLM summary
4. Step 3: Agent service ready ⚠️ → But receives no preprocessed data
5. Result: No conversational AI response ❌
```

**What needs to happen:**
```
1. Step 1: Classification ✅ → DataType.LOG_FILE
2. Step 2: preprocess_logs() ❌ → Parse + Analyze + Format → 8K summary
3. Step 3: Agent analyzes summary ✅ → Conversational AI response
4. Result: User gets diagnosis and recommendations ✅
```

### Next Steps

**Priority 1**: Implement log preprocessor (highest value)
- Reuse existing LogProcessor for analysis
- Add formatter to create LLM-ready summary
- ~4-6 hours effort

**Priority 2**: Implement error preprocessor
- Parse stack traces, format for LLM
- ~4-6 hours effort

**Priority 3+**: Metrics and config preprocessors
- More complex, lower priority
- ~8-10 hours each

**Critical Gap**: The preprocessing layer that formats insights into LLM-ready summaries is **documented but not implemented**.
