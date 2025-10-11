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

### Path 1: Explicit Data Upload (Recommended for Files)

**Endpoint**: `POST /api/v1/cases/{case_id}/data`

**Use Case**: User uses dedicated "Upload" UI in browser extension

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

**Flow**:
1. User selects file/pastes text/captures page content
2. Frontend converts to File object, uploads via FormData
3. Backend ingests file, analyzes content, generates AI response
4. Frontend displays user message + AI response in conversation
5. No separate "Session Data" UI needed

**Current Implementation Status**:
- ✅ Endpoint exists ([case.py:2094-2149](../../faultmaven/api/v1/routes/case.py#L2094))
- ⚠️ Returns mock data (not real file processing)
- ❌ Does not return `agent_response` field (needs implementation)

---

### Path 2: Implicit Detection (Paste in Query Box)

**Endpoint**: `POST /api/v1/cases/{case_id}/queries`

**Use Case**: User pastes large log/data into the regular query box

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

---

## Data Preprocessing Implementation (TODO)

### Problem Statement

**Current State**: Data is ingested and basic insights are extracted, but **large data files cannot be sent directly to LLM** due to context limits.

**Required**: Intelligent preprocessing layer that:
1. Extracts key information from uploaded data
2. Summarizes large datasets into LLM-digestible format
3. Preserves critical details (errors, anomalies, patterns)
4. Enables AI to provide meaningful analysis

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

## Implementation Checklist

### Backend (Priority: HIGH)

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

### Frontend (Priority: HIGH)

- [x] Convert text/page to File objects in `handleDataUpload()`
- [ ] Add user message to conversation on upload
- [ ] Add AI response message from `uploadResponse.agent_response`
- [ ] Remove "Session Data" UI section from ChatWindow
- [ ] Update `DataUploadResponse` interface with `agent_response` field
- [ ] Test all three data sources (file, text, page)

### Testing

- [ ] File upload → conversational response
- [ ] Text paste (via upload UI) → conversational response
- [ ] Page capture → conversational response
- [ ] Large paste (>10K) in query box → auto-detection + analysis
- [ ] Normal query with code snippet → processes normally

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
- ⚠️ Frontend needs conversation integration

**Next Steps**: Implement backend file processing and frontend conversation updates per checklist above.

---

**Last Updated**: 2025-10-04
**Version**: 3.0
**Authors**: System Architecture Team
