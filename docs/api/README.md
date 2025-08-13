# FaultMaven API Reference


# FaultMaven API Documentation

AI-powered troubleshooting assistant for Engineers, SREs, and DevOps professionals.

## Architecture Overview

The FaultMaven API follows clean architecture principles with:

- **API Layer**: FastAPI routers handling HTTP requests with comprehensive middleware
- **Service Layer**: Business logic orchestration using dependency injection
- **Core Layer**: Domain logic including AI reasoning engine and data processing
- **Infrastructure Layer**: External service integrations (LLM providers, databases, security)

## Key Features

- **AI-Powered Troubleshooting**: Advanced reasoning engine using multiple LLM providers
- **Privacy-First Design**: Comprehensive PII redaction before external processing
- **Session Management**: Redis-backed session persistence for multi-turn conversations
- **Knowledge Base**: RAG-enabled document ingestion and retrieval using ChromaDB
- **Data Processing**: Intelligent log analysis and classification
- **Performance Monitoring**: Real-time metrics and health monitoring
- **Error Recovery**: Automatic error detection and recovery mechanisms

## Authentication

Currently, the API does not require authentication. This may change in future versions.
When implemented, authentication will use API key-based authentication.

## Rate Limiting

API requests are subject to rate limiting to ensure fair usage and system stability.
Current limits are applied at the infrastructure level.

## Error Handling

All endpoints return structured error responses with appropriate HTTP status codes.

### Standard Error Response Format

```json
{
    "detail": "Human-readable error description",
    "error_type": "ErrorType",
    "correlation_id": "uuid-here",
    "timestamp": "2025-01-15T10:30:00Z"
}
```

### Common HTTP Status Codes

- `200`: Success
- `400`: Bad Request - Invalid input data
- `401`: Unauthorized - Authentication required (future)
- `404`: Not Found - Resource not found
- `422`: Validation Error - Request data validation failed
- `429`: Too Many Requests - Rate limit exceeded
- `500`: Internal Server Error - Unexpected server error
- `503`: Service Unavailable - External service unavailable

## Data Privacy

All data submitted to the API is processed through privacy-first pipelines with:

- Comprehensive PII redaction using Microsoft Presidio
- Data sanitization before external LLM processing
- Session-based data isolation
- Configurable data retention policies

## Performance Characteristics

- **Response Time**: < 200ms for typical queries (excluding LLM processing)
- **Throughput**: Supports 100+ concurrent requests
- **Availability**: 99.9% uptime target with health monitoring
- **Scalability**: Horizontal scaling support via stateless design
        

**Version:** 1.0.0  
**Base URL:** `/`  
**Generated:** 2025-08-13T08:06:54.142059Z

## Authentication

Currently, the API does not require authentication. Future versions will implement API key or JWT-based authentication.

## Endpoints

### `/`

#### GET

**Root**

Root endpoint with API information.

**Responses:**

**200** - Successful Response

---

### `/api/v1/agent/health`

#### GET

**Health Check**

Health check endpoint with service delegation

Returns:
    Service health status

**Tags:** `query_processing`, `query_processing`

**Responses:**

**200** - Successful Response

---

### `/api/v1/agent/investigations/{investigation_id}`

#### GET

**Get Investigation**

Get investigation results by ID with clean delegation

Args:
    investigation_id: Investigation identifier
    session_id: Session identifier for validation
    agent_service: Injected AgentServiceRefactored
    
Returns:
    TroubleshootingResponse with investigation results

**Tags:** `query_processing`, `query_processing`

**Parameters:**

- `investigation_id` (path) ✅ - No description
- `session_id` (query) ✅ - No description

**Responses:**

**200** - Successful Response

```json
{
  "investigation_id": "inv_789",
  "status": "completed",
  "findings": [
    {
      "type": "root_cause",
      "message": "Database connection pool exhausted due to connection leak",
      "severity": "high",
      "confidence": 0.9,
      "evidence": [
        "Connection pool size: 20, Active connections: 20",
        "No idle connections available",
        "Long-running transactions detected"
      ]
    }
  ],
  "recommendations": [
    {
      "action": "Increase database connection pool size to 50",
      "priority": "immediate",
      "impact": "Should restore service within 5 minutes",
      "effort": "low"
    },
    {
      "action": "Review application code for connection leaks",
      "priority": "high",
      "impact": "Prevents future occurrences",
      "effort": "medium"
    }
  ],
  "session_id": "session_db_123",
  "reasoning_trace": [
    {
      "step": "symptom_analysis",
      "reasoning": "HTTP 500 errors correlate with database timeout errors",
      "data_sources": [
        "application_logs",
        "database_metrics"
      ]
    },
    {
      "step": "hypothesis_formation",
      "reasoning": "Connection pool exhaustion is most likely cause given metrics",
      "data_sources": [
        "connection_pool_metrics",
        "transaction_logs"
      ]
    }
  ]
}
```

**422** - Validation Error

---

### `/api/v1/agent/query`

#### POST

**Troubleshoot**

Process troubleshooting query with clean delegation pattern

This endpoint follows the thin controller pattern:
1. Minimal input validation (handled by Pydantic models)
2. Pure delegation to service layer
3. Clean error boundary handling

Args:
    request: QueryRequest with query, session_id, context, priority
    agent_service: Injected AgentServiceRefactored from DI container
    
Returns:
    TroubleshootingResponse with findings and recommendations
    
Raises:
    HTTPException: On service layer errors (404, 500, etc.)

**Tags:** `query_processing`, `query_processing`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

```json
{
  "investigation_id": "inv_789",
  "status": "completed",
  "findings": [
    {
      "type": "root_cause",
      "message": "Database connection pool exhausted due to connection leak",
      "severity": "high",
      "confidence": 0.9,
      "evidence": [
        "Connection pool size: 20, Active connections: 20",
        "No idle connections available",
        "Long-running transactions detected"
      ]
    }
  ],
  "recommendations": [
    {
      "action": "Increase database connection pool size to 50",
      "priority": "immediate",
      "impact": "Should restore service within 5 minutes",
      "effort": "low"
    },
    {
      "action": "Review application code for connection leaks",
      "priority": "high",
      "impact": "Prevents future occurrences",
      "effort": "medium"
    }
  ],
  "session_id": "session_db_123",
  "reasoning_trace": [
    {
      "step": "symptom_analysis",
      "reasoning": "HTTP 500 errors correlate with database timeout errors",
      "data_sources": [
        "application_logs",
        "database_metrics"
      ]
    },
    {
      "step": "hypothesis_formation",
      "reasoning": "Connection pool exhaustion is most likely cause given metrics",
      "data_sources": [
        "connection_pool_metrics",
        "transaction_logs"
      ]
    }
  ]
}
```

**422** - Validation Error

---

### `/api/v1/agent/sessions/{session_id}/investigations`

#### GET

**List Session Investigations**

List investigations for a session with clean delegation

Args:
    session_id: Session identifier
    limit: Maximum number of results
    offset: Pagination offset
    agent_service: Injected AgentServiceRefactored
    
Returns:
    List of investigation summaries

**Tags:** `query_processing`, `query_processing`

**Parameters:**

- `session_id` (path) ✅ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/agent/troubleshoot`

#### POST

**Troubleshoot**

Process troubleshooting query with clean delegation pattern

This endpoint follows the thin controller pattern:
1. Minimal input validation (handled by Pydantic models)
2. Pure delegation to service layer
3. Clean error boundary handling

Args:
    request: QueryRequest with query, session_id, context, priority
    agent_service: Injected AgentServiceRefactored from DI container
    
Returns:
    TroubleshootingResponse with findings and recommendations
    
Raises:
    HTTPException: On service layer errors (404, 500, etc.)

**Tags:** `query_processing`, `query_processing`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

```json
{
  "investigation_id": "inv_789",
  "status": "completed",
  "findings": [
    {
      "type": "root_cause",
      "message": "Database connection pool exhausted due to connection leak",
      "severity": "high",
      "confidence": 0.9,
      "evidence": [
        "Connection pool size: 20, Active connections: 20",
        "No idle connections available",
        "Long-running transactions detected"
      ]
    }
  ],
  "recommendations": [
    {
      "action": "Increase database connection pool size to 50",
      "priority": "immediate",
      "impact": "Should restore service within 5 minutes",
      "effort": "low"
    },
    {
      "action": "Review application code for connection leaks",
      "priority": "high",
      "impact": "Prevents future occurrences",
      "effort": "medium"
    }
  ],
  "session_id": "session_db_123",
  "reasoning_trace": [
    {
      "step": "symptom_analysis",
      "reasoning": "HTTP 500 errors correlate with database timeout errors",
      "data_sources": [
        "application_logs",
        "database_metrics"
      ]
    },
    {
      "step": "hypothesis_formation",
      "reasoning": "Connection pool exhaustion is most likely cause given metrics",
      "data_sources": [
        "connection_pool_metrics",
        "transaction_logs"
      ]
    }
  ]
}
```

**422** - Validation Error

---

### `/api/v1/data/`

#### POST

**Upload Data Compat**

Compatibility endpoint for legacy tests - delegates to main upload function

This endpoint maintains backward compatibility for existing tests that
expect POST to /data instead of /data/upload.

**Tags:** `data_ingestion`, `data_processing`

**Request Body:**

Content-Type: `multipart/form-data`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/data/batch-upload`

#### POST

**Batch Upload Data**

Batch upload multiple files with clean delegation

Args:
    files: List of files to upload
    session_id: Session identifier
    data_service: Injected DataServiceRefactored
    
Returns:
    List of UploadedData results

**Tags:** `data_ingestion`, `data_processing`

**Request Body:**

Content-Type: `multipart/form-data`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/data/health`

#### GET

**Health Check**

Health check endpoint with service delegation

Returns:
    Service health status

**Tags:** `data_ingestion`, `data_processing`

**Responses:**

**200** - Successful Response

---

### `/api/v1/data/sessions/{session_id}`

#### GET

**Get Session Data**

Get all data for a session with clean delegation

Args:
    session_id: Session identifier
    limit: Maximum number of results
    offset: Pagination offset
    data_service: Injected DataServiceRefactored
    
Returns:
    List of UploadedData for the session

**Tags:** `data_ingestion`, `data_processing`

**Parameters:**

- `session_id` (path) ✅ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/data/sessions/{session_id}/batch-process`

#### POST

**Batch Process Session Data**

Batch process data for a session

Args:
    session_id: Session identifier
    batch_request: Request with data_ids to process
    data_service: Injected DataService
    
Returns:
    Batch processing job information

**Tags:** `data_ingestion`, `data_processing`

**Parameters:**

- `session_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/data/upload`

#### POST

**Upload Data**

Upload and process data with clean delegation pattern

This endpoint follows the thin controller pattern:
1. Basic input validation (file size, type)
2. Pure delegation to service layer for all business logic
3. Clean error boundary handling

Args:
    file: File to upload
    session_id: Session identifier 
    description: Optional description of the data
    data_service: Injected DataServiceRefactored from DI container
    
Returns:
    UploadedData with processing results
    
Raises:
    HTTPException: On service layer errors (400, 404, 413, 500)

**Tags:** `data_ingestion`, `data_processing`

**Request Body:**

Content-Type: `multipart/form-data`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/data/{data_id}`

#### GET

**Get Data**

Get data by ID

Args:
    data_id: Data identifier
    data_service: Injected DataService
    
Returns:
    Data information

**Tags:** `data_ingestion`, `data_processing`

**Parameters:**

- `data_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Data**

Delete uploaded data with clean delegation

Args:
    data_id: Data identifier to delete
    data_service: Injected DataServiceRefactored
    
Returns:
    Success confirmation

**Tags:** `data_ingestion`, `data_processing`

**Parameters:**

- `data_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/data/{data_id}/analyze`

#### POST

**Analyze Data**

Analyze uploaded data with clean delegation

Args:
    data_id: Data identifier to analyze
    analysis_request: Request containing session_id and analysis parameters
    data_service: Injected DataServiceRefactored
    
Returns:
    DataInsightsResponse with analysis results

**Tags:** `data_ingestion`, `data_processing`

**Parameters:**

- `data_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/kb/analytics/search`

#### GET

**Get Search Analytics Kb**

Get search analytics (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Responses:**

**200** - Successful Response

---

### `/api/v1/kb/documents`

#### GET

**List Documents Kb**

List knowledge base documents (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_type` (query) ❌ - No description
- `tags` (query) ❌ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Upload Document Kb**

Upload a document to the knowledge base (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `multipart/form-data`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/kb/documents/bulk-delete`

#### POST

**Bulk Delete Documents Kb**

Bulk delete documents (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/kb/documents/bulk-update`

#### POST

**Bulk Update Documents Kb**

Bulk update documents (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/kb/documents/{document_id}`

#### GET

**Get Document Kb**

Get a specific document (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Document Kb**

Update document (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Document Kb**

Delete a document (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/kb/jobs/{job_id}`

#### GET

**Get Job Status Kb**

Get job status (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `job_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/kb/search`

#### POST

**Search Documents Kb**

Search documents (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/kb/stats`

#### GET

**Get Knowledge Stats Kb**

Get knowledge stats (kb prefix)

**Tags:** `knowledge_base`, `knowledge_base`

**Responses:**

**200** - Successful Response

---

### `/api/v1/knowledge/analytics/search`

#### GET

**Get Search Analytics**

Get search analytics and insights.

**Tags:** `knowledge_base`, `knowledge_base`

**Responses:**

**200** - Successful Response

---

### `/api/v1/knowledge/documents`

#### GET

**List Documents**

List knowledge base documents with optional filtering

Args:
    document_type: Filter by document type
    tags: Filter by tags (comma-separated)
    limit: Maximum number of documents to return
    offset: Number of documents to skip

Returns:
    List of documents

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_type` (query) ❌ - No description
- `tags` (query) ❌ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Upload Document**

Upload a document to the knowledge base

Args:
    file: Document file to upload
    title: Document title
    document_type: Type of document
    tags: Comma-separated tags
    source_url: Source URL if applicable

Returns:
    Upload job information

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `multipart/form-data`

**Example: Troubleshooting Runbook**

Upload team runbook for knowledge base

```json
{
  "file": "[PDF or Markdown runbook content]",
  "document_type": "runbook",
  "tags": [
    "database",
    "troubleshooting",
    "postgresql"
  ],
  "description": "Database troubleshooting procedures and common fixes"
}
```

**Example: System Documentation**

Upload system architecture documentation

```json
{
  "file": "[Documentation content]",
  "document_type": "architecture_doc",
  "tags": [
    "architecture",
    "microservices",
    "system_design"
  ],
  "description": "Microservices architecture overview and dependencies"
}
```

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/documents/bulk-delete`

#### POST

**Bulk Delete Documents**

Bulk delete documents.

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/documents/bulk-update`

#### POST

**Bulk Update Documents**

Bulk update document metadata.

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/documents/{document_id}`

#### GET

**Get Document**

Get a specific knowledge base document

Args:
    document_id: Document identifier

Returns:
    Document details

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Document**

Update document metadata and content.

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Document**

Delete a knowledge base document

Args:
    document_id: Document identifier

Returns:
    Deletion confirmation

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/jobs/{job_id}`

#### GET

**Get Job Status**

Get the status of a knowledge base ingestion job

Args:
    job_id: Job identifier

Returns:
    Job status information

**Tags:** `knowledge_base`, `knowledge_base`

**Parameters:**

- `job_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/search`

#### POST

**Search Documents**

Search knowledge base documents

Args:
    request: Search request with query and filters

Returns:
    Search results

**Tags:** `knowledge_base`, `knowledge_base`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/stats`

#### GET

**Get Knowledge Stats**

Get knowledge base statistics.

**Tags:** `knowledge_base`, `knowledge_base`

**Responses:**

**200** - Successful Response

---

### `/api/v1/sessions/`

#### GET

**List Sessions**

List all sessions with optional filtering.

Args:
    user_id: Optional user ID filter
    session_type: Optional session type filter
    usage_type: Optional usage type filter (alias for session_type)
    limit: Maximum number of sessions to return
    offset: Number of sessions to skip

Returns:
    List of sessions

**Tags:** `session_management`, `session_management`

**Parameters:**

- `user_id` (query) ❌ - No description
- `session_type` (query) ❌ - No description
- `usage_type` (query) ❌ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Create Session**

Create a new troubleshooting session.

Args:
    request: Session creation parameters
    user_id: Optional user identifier (query param)

Returns:
    Session creation response

**Tags:** `session_management`, `session_management`

**Parameters:**

- `user_id` (query) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}`

#### GET

**Get Session**

Retrieve a specific session by ID.

Args:
    session_id: Session identifier

Returns:
    Session details

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Session**

Delete a specific session.

Args:
    session_id: Session identifier

Returns:
    Deletion confirmation

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}/cleanup`

#### POST

**Cleanup Session**

Clean up session data and temporary files.

Args:
    session_id: Session identifier

Returns:
    Cleanup confirmation

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}/heartbeat`

#### POST

**Session Heartbeat**

Update session activity timestamp (heartbeat).

Args:
    session_id: Session identifier

Returns:
    Heartbeat confirmation

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}/recovery-info`

#### GET

**Get Session Recovery Info**

Get session recovery information for restoring lost sessions.

Args:
    session_id: Session identifier

Returns:
    Recovery information

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}/restore`

#### POST

**Restore Session**

Restore a session from backup or recovery state.

Args:
    session_id: Session identifier
    restore_request: Restoration parameters

Returns:
    Restoration confirmation

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}/stats`

#### GET

**Get Session Stats**

Get session statistics and activity summary.

Args:
    session_id: Session identifier

Returns:
    Session statistics

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/health`

#### GET

**Health Check**

Enhanced health check endpoint with component-specific metrics and SLA monitoring.

**Responses:**

**200** - Successful Response

---

### `/health/components/{component_name}`

#### GET

**Health Check Component**

Get detailed health information for a specific component.

**Parameters:**

- `component_name` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/health/dependencies`

#### GET

**Health Check Dependencies**

Enhanced detailed health check for all dependencies with SLA metrics

**Responses:**

**200** - Successful Response

---

### `/health/logging`

#### GET

**Logging Health Check**

Get logging system health status.

**Responses:**

**200** - Successful Response

---

### `/health/patterns`

#### GET

**Health Check Error Patterns**

Get error patterns and recovery information from enhanced error context.

**Responses:**

**200** - Successful Response

---

### `/health/sla`

#### GET

**Health Check Sla**

Get SLA status and metrics for all components.

**Responses:**

**200** - Successful Response

---

### `/metrics/alerts`

#### GET

**Get Alert Status**

Get current alert status and statistics.

**Responses:**

**200** - Successful Response

---

### `/metrics/performance`

#### GET

**Get Performance Metrics**

Get comprehensive performance metrics.

**Responses:**

**200** - Successful Response

---

### `/metrics/realtime`

#### GET

**Get Realtime Metrics**

Get real-time performance metrics.

**Parameters:**

- `time_window_minutes` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

## Data Models

### Body_batch_upload_data_api_v1_data_batch_upload_post

**Properties:**

- `files` (array) ✅ - No description
- `session_id` (string) ✅ - No description

---

### Body_upload_data_api_v1_data_upload_post

**Properties:**

- `file` (string) ✅ - No description
- `session_id` (string) ✅ - No description
- `description` (unknown) ❌ - No description

---

### Body_upload_data_compat_api_v1_data__post

**Properties:**

- `file` (string) ✅ - No description
- `session_id` (string) ✅ - No description
- `description` (unknown) ❌ - No description

---

### Body_upload_document_api_v1_knowledge_documents_post

**Properties:**

- `file` (string) ✅ - No description
- `title` (string) ✅ - No description
- `document_type` (string) ❌ - No description
- `category` (unknown) ❌ - No description
- `tags` (unknown) ❌ - No description
- `source_url` (unknown) ❌ - No description
- `description` (unknown) ❌ - No description

---

### Body_upload_document_kb_api_v1_kb_documents_post

**Properties:**

- `file` (string) ✅ - No description
- `title` (string) ✅ - No description
- `document_type` (string) ❌ - No description
- `category` (unknown) ❌ - No description
- `tags` (unknown) ❌ - No description
- `source_url` (unknown) ❌ - No description
- `description` (unknown) ❌ - No description

---

### DataType

Enumeration of supported data types for classification

---

### HTTPValidationError

**Properties:**

- `detail` (array) ❌ - No description

---

### KnowledgeBaseDocument

Model for knowledge base documents

**Properties:**

- `document_id` (string) ✅ - Unique document identifier
- `title` (string) ✅ - Document title
- `content` (string) ✅ - Document content
- `document_type` (string) ✅ - Type of document (e.g., troubleshooting guide, FAQ)
- `category` (unknown) ❌ - Document category for organization
- `status` (string) ❌ - Document processing status
- `tags` (array) ❌ - Tags for categorization
- `source_url` (unknown) ❌ - Source URL if applicable
- `created_at` (string) ❌ - Document creation timestamp
- `updated_at` (string) ❌ - Last update timestamp
- `metadata` (unknown) ❌ - Additional document metadata

---

### QueryRequest

Request model for troubleshooting queries

**Properties:**

- `session_id` (string) ✅ - Session identifier
- `query` (string) ✅ - User's troubleshooting query
- `context` (unknown) ❌ - Additional context for the query
- `priority` (string) ❌ - Query priority level

---

### SearchRequest

Request model for knowledge base search

**Properties:**

- `query` (string) ✅ - Search query
- `document_type` (unknown) ❌ - Filter by document type
- `category` (unknown) ❌ - Filter by document category
- `tags` (unknown) ❌ - Filter by tags (comma-separated)
- `filters` (unknown) ❌ - Advanced filters for search
- `similarity_threshold` (unknown) ❌ - Minimum similarity score threshold (0.0-1.0)
- `rank_by` (unknown) ❌ - Field to rank results by (e.g., priority)
- `limit` (integer) ❌ - Maximum number of results

---

### SessionCreateRequest

Request model for session creation.

**Properties:**

- `timeout_minutes` (unknown) ❌ - No description
- `session_type` (unknown) ❌ - No description
- `metadata` (unknown) ❌ - No description

---

### SessionRestoreRequest

Request model for session restoration.

**Properties:**

- `restore_point` (string) ✅ - No description
- `include_data` (boolean) ❌ - No description
- `type` (unknown) ❌ - No description

---

### TroubleshootingResponse

**Properties:**

- `investigation_id` (string) ✅ - Unique identifier for this troubleshooting investigation
- `status` (string) ✅ - Current status of the investigation
- `findings` (array) ❌ - List of findings from the investigation
- `recommendations` (array) ❌ - Recommended actions based on findings
- `session_id` (string) ✅ - Session ID for this troubleshooting session
- `reasoning_trace` (array) ❌ - AI reasoning process trace for transparency

**Example:**

```json
{
  "investigation_id": "inv_789",
  "status": "completed",
  "findings": [
    {
      "type": "root_cause",
      "message": "Database connection pool exhausted due to connection leak",
      "severity": "high",
      "confidence": 0.9,
      "evidence": [
        "Connection pool size: 20, Active connections: 20",
        "No idle connections available",
        "Long-running transactions detected"
      ]
    }
  ],
  "recommendations": [
    {
      "action": "Increase database connection pool size to 50",
      "priority": "immediate",
      "impact": "Should restore service within 5 minutes",
      "effort": "low"
    },
    {
      "action": "Review application code for connection leaks",
      "priority": "high",
      "impact": "Prevents future occurrences",
      "effort": "medium"
    }
  ],
  "session_id": "session_db_123",
  "reasoning_trace": [
    {
      "step": "symptom_analysis",
      "reasoning": "HTTP 500 errors correlate with database timeout errors",
      "data_sources": [
        "application_logs",
        "database_metrics"
      ]
    },
    {
      "step": "hypothesis_formation",
      "reasoning": "Connection pool exhaustion is most likely cause given metrics",
      "data_sources": [
        "connection_pool_metrics",
        "transaction_logs"
      ]
    }
  ]
}
```

---

### UploadedData

Model for uploaded data processing

**Properties:**

- `data_id` (string) ✅ - Unique identifier for the uploaded data
- `session_id` (string) ✅ - Session this data belongs to
- `data_type` (unknown) ✅ - Classified type of the data
- `content` (string) ✅ - Raw content of the uploaded data
- `file_name` (unknown) ❌ - Original filename if applicable
- `file_size` (unknown) ❌ - File size in bytes
- `uploaded_at` (string) ❌ - Upload timestamp
- `processing_status` (string) ❌ - Processing status
- `insights` (unknown) ❌ - Extracted insights from the data

---

### ValidationError

**Properties:**

- `loc` (array) ✅ - No description
- `msg` (string) ✅ - No description
- `type` (string) ✅ - No description

---

### ErrorResponse

**Properties:**

- `detail` (string) ✅ - Human-readable error description
- `error_type` (string) ❌ - Machine-readable error classification
- `correlation_id` (string) ❌ - Unique identifier for request tracing and support
- `timestamp` (string) ❌ - Error occurrence timestamp in ISO format
- `context` (object) ❌ - Additional error context for debugging

**Example:**

```json
{
  "detail": "Invalid session ID provided",
  "error_type": "ValidationError",
  "correlation_id": "123e4567-e89b-12d3-a456-426614174000",
  "timestamp": "2025-01-15T10:30:00Z",
  "context": {
    "session_id": "invalid_session_123",
    "validation_errors": [
      "Session ID format invalid"
    ]
  }
}
```

---

### DataIngestionResponse

**Properties:**

- `ingestion_id` (string) ✅ - Unique identifier for this data ingestion
- `status` (string) ✅ - Current processing status
- `file_info` (object) ❌ - Information about the uploaded file
- `processing_results` (object) ❌ - Results of data processing

**Example:**

```json
{
  "ingestion_id": "ingest_456",
  "status": "completed",
  "file_info": {
    "filename": "app.log",
    "size_bytes": 1048576,
    "file_type": "application/log",
    "detected_format": "json_logs"
  },
  "processing_results": {
    "lines_processed": 15420,
    "errors_found": 23,
    "insights_extracted": 8,
    "processing_time_ms": 2340
  }
}
```

---

### SessionResponse

**Properties:**

- `session_id` (string) ✅ - Unique session identifier
- `status` (string) ✅ - Current session status
- `created_at` (string) ❌ - Session creation timestamp
- `last_activity` (string) ❌ - Last activity timestamp
- `metadata` (object) ❌ - Session metadata and context

**Example:**

```json
{
  "session_id": "session_abc123",
  "status": "active",
  "created_at": "2025-01-15T10:00:00Z",
  "last_activity": "2025-01-15T10:25:00Z",
  "metadata": {
    "user_id": "user_123",
    "environment": "production",
    "investigations_count": 3
  }
}
```

---

