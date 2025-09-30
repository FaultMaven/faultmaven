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

The API uses **Bearer token authentication** for all protected endpoints. Obtain tokens via the `/api/v1/auth/dev-login` endpoint.

### Authentication Headers

```http
Authorization: Bearer 550e8400-e29b-41d4-a716-446655440000
```

### Token Lifecycle
- **Format**: UUID v4 tokens
- **Expiration**: 24 hours from creation
- **Endpoints**: All case operations require authentication
- **Development**: Use `/api/v1/auth/dev-login` with username to obtain tokens

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
- `401`: Unauthorized - Missing or invalid Bearer token
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
**Generated:** 2025-09-14T10:48:35.595364Z

## Authentication

The API requires Bearer token authentication for protected endpoints. See the Authentication section above for details on obtaining and using tokens.

## Endpoints

### `/`

#### GET

**Root**

Root endpoint with API information.

**Responses:**

**200** - Successful Response

---

### `/admin/optimization/trigger-cleanup`

#### GET

**Trigger System Cleanup**

Trigger comprehensive system cleanup and optimization.

**Responses:**

**200** - Successful Response

---

### `/api/v1/auth/dev-login`

#### POST

**Dev Login**

Developer login mock endpoint.

Creates or authenticates a user with minimal validation (username/email only).
Returns complete ViewState for immediate UI rendering.

Args:
    request: DevLoginRequest with username (email)
    session_service: Injected session service
    
Returns:
    AuthResponse with ViewState containing user context and initial data

**Tags:** `authentication`, `authentication`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/auth/logout`

#### POST

**Logout**

Logout endpoint to clean up session.

Returns:
    Success confirmation

**Tags:** `authentication`, `authentication`

**Responses:**

**200** - Successful Response

---

### `/api/v1/auth/session/{session_id}`

#### GET

**Verify Session**

Verify existing session and return current ViewState.

Used by frontend to restore session on app startup.

Args:
    session_id: Session ID to verify
    session_service: Injected session service
    
Returns:
    AuthResponse with current ViewState if session is valid

**Tags:** `authentication`, `authentication`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases`

#### GET

**List Cases**

List cases with pagination

Returns a list of cases accessible to the authenticated user.
Always returns 200 with raw array (Case[]); returns [] when no results.
Supports pagination via page/limit parameters with X-Total-Count and Link headers.

Default Filtering Behavior:
- Excludes empty cases (message_count == 0) unless include_empty=true
- Excludes archived cases unless include_archived=true  
- Excludes deleted cases unless include_deleted=true (admin only)
- Only returns active cases with messages by default

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `page` (query) ❌ - Page number
- `limit` (query) ❌ - Items per page
- `include_empty` (query) ❌ - Include cases with message_count == 0
- `include_archived` (query) ❌ - Include archived cases
- `include_deleted` (query) ❌ - Include deleted cases (admin only)

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Create Case**

Create a new troubleshooting case

Creates a new case for tracking troubleshooting sessions and conversations.
The case will persist beyond individual session lifetimes.

**Tags:** `case_persistence`, `cases`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/health`

#### GET

**Get Case Service Health**

Get case service health status

Returns health information about the case persistence system,
including connectivity and performance metrics.

**Tags:** `case_persistence`, `cases`

**Responses:**

**200** - Successful Response

---

### `/api/v1/cases/search`

#### POST

**Search Cases**

Search cases by content

Searches case titles, descriptions, and optionally message content
for the specified query terms.

**Tags:** `case_persistence`, `cases`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/sessions/{session_id}/case`

#### POST

**Create Case For Session**

Create or get case for a session

Associates a case with the given session. If no case exists, creates a new one.
If force_new is true, always creates a new case.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `session_id` (path) ✅ - No description
- `title` (query) ❌ - Case title
- `force_new` (query) ❌ - Force creation of new case

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/sessions/{session_id}/resume/{case_id}`

#### POST

**Resume Case In Session**

Resume an existing case in a session

Links the session to an existing case, allowing the user to continue
a previous troubleshooting conversation.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `session_id` (path) ✅ - No description
- `case_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}`

#### GET

**Get Case**

Get a specific case by ID

Returns the full case details including conversation history,
participants, and context information.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Case**

Update case details

Updates case metadata such as title, description, status, priority, and tags.
Requires edit permissions on the case.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Case**

Permanently delete a case and all associated data.

This endpoint provides hard delete functionality. Once deleted, 
the case and all associated data are permanently removed.

The operation is idempotent - subsequent requests will return 
204 No Content even if the case has already been deleted.

Returns 204 No Content on success.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description

**Responses:**

**204** - Case deleted successfully

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/analytics`

#### GET

**Get Case Analytics**

Get case analytics and metrics

Returns analytics data including message counts, participant activity,
resolution time, and other case metrics.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/archive`

#### POST

**Archive Case**

Archive a case

Archives the case, marking it as completed and removing it from active lists.
Requires owner or collaborator permissions.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `reason` (query) ❌ - Reason for archiving

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/conversation`

#### GET

**Get Case Conversation Context**

Get conversation messages for a case

Returns conversation history as JSON array of messages.
Each message: { message_id, role: user|agent, content, created_at }

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `limit` (query) ❌ - Maximum number of messages to include

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/data`

#### GET

**List Case Data**

List data files associated with a case.

Returns array of data records with pagination headers.
Always returns 200 with empty array if no data exists.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `limit` (query) ❌ - Maximum number of items to return
- `offset` (query) ❌ - Number of items to skip

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Upload Case Data**

Upload data file to a specific case.

Associates uploaded data with the case for context-aware troubleshooting.
Returns 201 with Location header pointing to the created data resource.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `description` (query) ❌ - Description of uploaded data
- `expected_type` (query) ❌ - Expected data type

**Responses:**

**201** - Data uploaded successfully

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/data/{data_id}`

#### GET

**Get Case Data**

Get specific data file details for a case.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `data_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Case Data**

Remove data file from a case. Returns 204 No Content on success.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `data_id` (path) ✅ - No description

**Responses:**

**204** - Data deleted successfully

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/messages`

#### GET

**List Case Messages**

Return conversation messages for a case in a UI-friendly format.
Each item: { message_id, role: user|agent, content, created_at }

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/queries`

#### GET

**List Case Queries**

List queries for a specific case with pagination.

CRITICAL: Must return 200 [] for empty results, NOT 404

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Submit Case Query**

Submit a query to a case.

CRITICAL: Must return 201 (sync) or 202 (async) per OpenAPI spec, NOT 404

Args:
    case_id: Case identifier  
    request: FastAPI request containing query data

Returns:
    201 with immediate result OR 202 with job Location for async processing

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**201** - Query processed synchronously

**202** - Query processing asynchronously

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/queries/{query_id}`

#### GET

**Get Case Query**

Get query status and result (for async polling).

Returns 200 with AgentResponse when completed, or 202 with QueryJobStatus while processing.
Supports Retry-After header for polling guidance.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `query_id` (path) ✅ - No description

**Responses:**

**200** - Query completed - returns AgentResponse

**202** - Query still processing - returns job status

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/queries/{query_id}/result`

#### GET

**Get Case Query Result**

Return the final AgentResponse for a completed query.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `query_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/share`

#### POST

**Share Case**

Share a case with another user

Grants access to the case for the specified user with the given role.
Requires share permissions on the case.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/title`

#### POST

**Generate Case Title**

Generate a concise, case-specific title from case messages and metadata.

**Request body (optional):**
- `max_words`: integer (3–12, default 8) - Maximum words in generated title
- `hint`: string - Optional hint to guide title generation
- `force`: boolean (default false) - Only overwrite non-default titles when true

**Returns:**
- 200: TitleResponse with X-Correlation-ID header
- 422: ErrorResponse with code INSUFFICIENT_CONTEXT and X-Correlation-ID header

**Description:** Returns 422 when insufficient meaningful context; clients SHOULD keep 
existing title unchanged and may retry later.

**Tags:** `case_persistence`, `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `force` (query) ❌ - Only overwrite non-default titles when true

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/data`

#### POST

**Upload Data Compat**

Upload and process log files, configuration files, or other diagnostic data

**Tags:** `data_ingestion`, `data_processing`

**Request Body:**

Content-Type: `multipart/form-data`

**Example: Application Log File**

Upload application logs for analysis

```json
{
  "file": "[Binary log file content]",
  "file_type": "application_logs",
  "description": "Production API server logs from the last 24 hours"
}
```

**Example: Kubernetes Configuration**

Upload Kubernetes YAML for configuration analysis

```json
{
  "file": "[YAML configuration content]",
  "file_type": "kubernetes_config",
  "description": "Deployment configuration showing resource issues"
}
```

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/data/batch-upload`

#### POST

**Batch Upload Data**

Batch upload multiple files with clean delegation

Args:
    files: List of files to upload
    session_id: Session identifier
    data_service: Injected DataService
    
Returns:
    List of UploadedData results

**Tags:** `data_ingestion`, `data_processing`

**Request Body:**

Content-Type: `multipart/form-data`

**Responses:**

**201** - Successful Response

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
    data_service: Injected DataService
    
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
    data_service: Injected DataService from DI container
    
Returns:
    UploadedData with processing results
    
Raises:
    HTTPException: On service layer errors (400, 404, 413, 500)

**Tags:** `data_ingestion`, `data_processing`

**Request Body:**

Content-Type: `multipart/form-data`

**Responses:**

**201** - Successful Response

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
    session_id: Session identifier for access control (query parameter)
    data_service: Injected DataService
    
Returns:
    Success confirmation

**Tags:** `data_ingestion`, `data_processing`

**Parameters:**

- `data_id` (path) ✅ - No description
- `session_id` (query) ✅ - No description

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
    data_service: Injected DataService
    
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

### `/api/v1/jobs`

#### GET

**List Jobs**

List jobs with optional filtering and pagination

Returns a paginated list of jobs with proper pagination headers.
Supports filtering by job status.

**Tags:** `job_management`, `job_management`

**Parameters:**

- `status_filter` (query) ❌ - Filter by job status
- `limit` (query) ❌ - Maximum number of results
- `offset` (query) ❌ - Result offset for pagination

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/jobs/health`

#### GET

**Get Job Service Health**

Get job service health status

Returns health information about the job management system,
including connectivity and performance metrics.

**Tags:** `job_management`, `job_management`

**Responses:**

**200** - Successful Response

---

### `/api/v1/jobs/{job_id}`

#### GET

**Get Job Status**

Get job status with proper polling semantics

Implements consistent job polling with appropriate headers:
- 200 OK for running/pending jobs with Retry-After header
- 303 See Other redirect for completed jobs with results
- 200 OK for failed/cancelled jobs (terminal states)

**Tags:** `job_management`, `job_management`

**Parameters:**

- `job_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Cancel Job**

Cancel a running job

Attempts to cancel a job if it's still in a cancellable state.
Returns 204 No Content on success.

**Tags:** `job_management`, `job_management`

**Parameters:**

- `job_id` (path) ✅ - No description

**Responses:**

**204** - Successful Response

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

**201** - Successful Response

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

**204** - Successful Response

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

**201** - Successful Response

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

**204** - Successful Response

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

### `/api/v1/protection/config`

#### GET

**Get Protection Config**

Get current protection system configuration

Returns sanitized configuration information (no sensitive data)
for both basic and intelligent protection components.

Returns:
    Dict with protection configuration including enabled features,
    rate limits, timeouts, and security settings (sanitized)
    
Raises:
    HTTPException: On service layer errors (503, 500)

**Tags:** `protection`, `protection`

**Responses:**

**200** - Successful Response

---

### `/api/v1/protection/health`

#### GET

**Get Protection Health**

Get protection system health status

Returns comprehensive health information for both basic and intelligent
protection components including middleware status and configuration validation.

Returns:
    Dict with protection system health status, active components, and validation results
    
Raises:
    HTTPException: On service layer errors (503, 500)

**Tags:** `protection`, `protection`

**Responses:**

**200** - Successful Response

---

### `/api/v1/protection/metrics`

#### GET

**Get Protection Metrics**

Get protection system metrics for monitoring

Returns detailed metrics for rate limiting, request deduplication, 
behavioral analysis, ML anomaly detection, and reputation system.

Returns:
    Dict with protection system metrics including request counts, 
    protection rates, and component-specific statistics
    
Raises:
    HTTPException: On service layer errors (503, 500)

**Tags:** `protection`, `protection`

**Responses:**

**200** - Successful Response

---

### `/api/v1/sessions`

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

Create or resume a troubleshooting session.

**Session Creation & Resumption:**
- If `client_id` is provided and matches an active session, that session is resumed
- If `client_id` matches an expired session, returns 404/410 error (frontend creates new session)
- If `client_id` is new or not provided, creates fresh session

**Session Timeout:**
- Sessions automatically expire after `timeout_minutes` of inactivity
- Default timeout: 180 minutes (3 hours)
- Min timeout: 60 minutes, Max timeout: 480 minutes
- Expired sessions cannot be resumed and return 404/410 errors

**Frontend Crash Recovery:**
- Browser crashes: Session resumes if within timeout window
- Extended downtime: Session expires, new session created automatically

Args:
    request: Session creation parameters including optional client_id and timeout
    user_id: Optional user identifier (query param)

Returns:
    Session creation/resumption response with expiration information

**Tags:** `session_management`, `session_management`

**Parameters:**

- `user_id` (query) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Example: Create New Session**

Start a new troubleshooting session

```json
{
  "timeout_minutes": 60,
  "session_type": "troubleshooting",
  "metadata": {
    "environment": "production",
    "team": "platform-team",
    "incident_priority": "high"
  }
}
```

**Example: Resume Session with Client ID**

Resume existing session using client identifier for session continuity

```json
{
  "timeout_minutes": 60,
  "session_type": "troubleshooting",
  "client_id": "browser-client-abc123",
  "metadata": {
    "environment": "production",
    "team": "platform-team"
  }
}
```

**Responses:**

**201** - Session created or resumed successfully

**404** - Session expired or not found (when resuming with client_id)

**410** - Session gone (alternative to 404 for expired sessions)

**422** - Validation error (invalid timeout_minutes)

---

### `/api/v1/sessions/cleanup`

#### POST

**Cleanup Expired Sessions**

Clean up expired sessions (admin/testing endpoint).

This endpoint triggers immediate cleanup of expired sessions.
In production, this runs automatically every 30 minutes.

Returns:
    Number of sessions cleaned up

**Tags:** `session_management`, `session_management`

**Responses:**

**200** - Successful Response

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

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}/cases`

#### GET

**List Session Cases**

List all cases associated with a session.

CRITICAL: Must return 200 [] for empty results, NOT 404

Args:
    session_id: Session identifier
    limit: Maximum number of cases to return (1-100)
    offset: Number of cases to skip for pagination

Returns:
    List of cases (empty list if no cases found)

**Tags:** `session_management`, `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description
- `include_empty` (query) ❌ - Include cases with message_count == 0
- `include_archived` (query) ❌ - Include archived cases
- `include_deleted` (query) ❌ - Include deleted cases (admin only)

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

### `/debug/health`

#### GET

**Debug Health**

Minimal debug health endpoint.

**Responses:**

**200** - Successful Response

---

### `/debug/routes`

#### GET

**Debug Routes**

List all registered routes (path + methods).

**Responses:**

**200** - Successful Response

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

### `/metrics/optimization`

#### GET

**Get System Optimization Metrics**

Get comprehensive system optimization metrics.

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

### `/readiness`

#### GET

**Readiness**

Readiness probe: return unready if Redis or ChromaDB are unavailable.

**Responses:**

**200** - Successful Response

---

## Data Models

### AgentResponse

The single, unified JSON payload returned from the backend.

**Properties:**

- `schema_version` (string) ❌ - No description
- `content` (string) ✅ - No description
- `response_type` (unknown) ✅ - No description
- `session_id` (string) ✅ - No description
- `case_id` (unknown) ❌ - No description
- `confidence_score` (unknown) ❌ - No description
- `sources` (array) ❌ - No description
- `next_action_hint` (unknown) ❌ - No description
- `view_state` (unknown) ❌ - No description
- `plan` (unknown) ❌ - No description

---

### AuthResponse

Response payload for authentication operations with ViewState.

**Properties:**

- `schema_version` (string) ❌ - No description
- `success` (boolean) ❌ - No description
- `view_state` (unknown) ✅ - No description

---

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

### Body_upload_data_compat_api_v1_data_post

**Properties:**

- `file` (string) ✅ - No description
- `session_id` (string) ✅ - No description
- `description` (unknown) ❌ - No description

---

### Body_upload_document_api_v1_knowledge_documents_post

**Properties:**

- `file` (string) ✅ - No description
- `title` (string) ✅ - No description
- `document_type` (string) ✅ - No description
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

### Case

Represents a troubleshooting case.

**Properties:**

- `case_id` (string) ✅ - No description
- `title` (string) ✅ - No description
- `description` (unknown) ❌ - No description
- `status` (string) ❌ - No description
- `priority` (string) ❌ - No description
- `created_at` (string) ❌ - No description
- `updated_at` (string) ❌ - No description
- `message_count` (integer) ❌ - No description
- `session_id` (unknown) ❌ - No description
- `owner_id` (unknown) ❌ - No description

---

### CaseCreateRequest

Request model for creating a new case

**Properties:**

- `title` (string) ✅ - Case title
- `description` (unknown) ❌ - Case description
- `priority` (unknown) ❌ - Case priority
- `tags` (array) ❌ - Case tags
- `session_id` (unknown) ❌ - Associated session ID
- `initial_message` (unknown) ❌ - Initial case message

---

### CaseListFilter

Filter criteria for listing cases

**Properties:**

- `user_id` (unknown) ❌ - Filter by participant user ID
- `status` (unknown) ❌ - Filter by case status
- `priority` (unknown) ❌ - Filter by case priority
- `owner_id` (unknown) ❌ - Filter by case owner
- `tags` (unknown) ❌ - Filter by tags (any match)
- `created_after` (unknown) ❌ - Filter by creation date
- `created_before` (unknown) ❌ - Filter by creation date
- `include_empty` (boolean) ❌ - Include cases with message_count == 0
- `include_archived` (boolean) ❌ - Include archived cases
- `include_deleted` (boolean) ❌ - Include deleted cases (admin only)
- `limit` (integer) ❌ - Maximum number of results
- `offset` (integer) ❌ - Result offset for pagination

---

### CasePriority

Case priority levels

---

### CaseResponse

Response payload for case creation.

**Properties:**

- `schema_version` (string) ❌ - No description
- `case` (unknown) ✅ - No description

---

### CaseSearchRequest

Request model for searching cases

**Properties:**

- `query` (string) ✅ - Search query
- `filters` (unknown) ❌ - Additional filters
- `search_in_messages` (boolean) ❌ - Search in message content
- `search_in_context` (boolean) ❌ - Search in case context

---

### CaseShareRequest

Request model for sharing a case with other users

**Properties:**

- `user_id` (string) ✅ - User ID to share with
- `role` (unknown) ❌ - Role to assign
- `message` (unknown) ❌ - Optional message to include

---

### CaseStatus

Case lifecycle status enumeration

---

### CaseSummary

Summary view of a case for list operations

**Properties:**

- `case_id` (string) ✅ - No description
- `title` (string) ✅ - No description
- `status` (unknown) ✅ - No description
- `priority` (unknown) ✅ - No description
- `owner_id` (unknown) ✅ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description
- `last_activity_at` (string) ✅ - No description
- `message_count` (integer) ✅ - No description
- `participant_count` (integer) ✅ - No description
- `tags` (array) ✅ - No description

---

### CaseUpdateRequest

Request model for updating case details

**Properties:**

- `title` (unknown) ❌ - Updated case title
- `description` (unknown) ❌ - Updated case description
- `status` (unknown) ❌ - Updated case status
- `priority` (unknown) ❌ - Updated case priority
- `tags` (unknown) ❌ - Updated case tags

---

### DataType

Defines the type of data uploaded by users.

---

### DevLoginRequest

Request payload for developer login.

**Properties:**

- `username` (string) ✅ - No description

---

### HTTPValidationError

**Properties:**

- `detail` (array) ❌ - No description

---

### JobStatus

Async job status tracking model.

**Properties:**

- `job_id` (string) ✅ - No description
- `status` (string) ✅ - No description
- `progress` (unknown) ❌ - No description
- `result` (unknown) ❌ - No description
- `error` (unknown) ❌ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description

---

### KnowledgeBaseDocument

Response model for knowledge base document operations.

**Properties:**

- `document_id` (string) ✅ - No description
- `title` (string) ✅ - No description
- `content` (string) ✅ - No description
- `document_type` (string) ✅ - No description
- `category` (unknown) ❌ - No description
- `status` (string) ❌ - No description
- `tags` (array) ❌ - No description
- `source_url` (unknown) ❌ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description
- `metadata` (unknown) ❌ - No description

---

### Message

Message model for conversation endpoints.

**Properties:**

- `message_id` (string) ✅ - No description
- `role` (string) ✅ - No description
- `content` (string) ✅ - No description
- `created_at` (string) ✅ - ISO 8601 datetime string

---

### ParticipantRole

Participant roles in case collaboration

---

### PlanStep

Represents one step in a multi-step plan.

**Properties:**

- `description` (string) ✅ - No description

---

### ProcessingStatus

Defines the status of data processing operations.

---

### QueryJobStatus

Case-scoped query job status tracking model.

**Properties:**

- `query_id` (string) ✅ - No description
- `case_id` (string) ✅ - No description
- `status` (string) ✅ - No description
- `progress_percentage` (unknown) ❌ - Processing progress percentage
- `started_at` (unknown) ❌ - Job start time (UTC ISO 8601)
- `last_updated_at` (string) ❌ - No description
- `error` (unknown) ❌ - Error details if status is failed
- `result` (unknown) ❌ - Final result if completed

---

### QueryRequest

The JSON payload sent from the frontend when the user asks a question.
Note: case_id is provided in the URL path, not in the request body.

**Properties:**

- `session_id` (string) ✅ - No description
- `query` (string) ✅ - No description
- `context` (unknown) ❌ - No description
- `priority` (string) ❌ - No description
- `timestamp` (string) ❌ - No description

---

### ResponseType

Defines the agent's primary intent for this turn.

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

- `timeout_minutes` (unknown) ❌ - Session timeout in minutes. Min: 60 (1 hour), Max: 480 (8 hours), Default: 180 (3 hours)
- `session_type` (unknown) ❌ - No description
- `metadata` (unknown) ❌ - No description
- `client_id` (unknown) ❌ - Client/device identifier for session resumption. If provided, existing session for this client will be resumed.

---

### SessionResponse

**Properties:**

- `session_id` (string) ✅ - Unique session identifier
- `user_id` (string) ❌ - Associated user identifier
- `client_id` (string) ❌ - Client/device identifier for session resumption
- `status` (string) ✅ - Current session status
- `created_at` (string) ❌ - Session creation timestamp
- `session_resumed` (boolean) ❌ - Indicates if this was an existing session resumed
- `session_type` (string) ❌ - Type of session (e.g., troubleshooting)
- `message` (string) ❌ - Status message about session creation/resumption
- `metadata` (object) ❌ - Session metadata and context

---

### SessionRestoreRequest

Request model for session restoration.

**Properties:**

- `restore_point` (string) ✅ - No description
- `include_data` (boolean) ❌ - No description
- `type` (unknown) ❌ - No description

---

### SessionStatus

Defines the status of user sessions.

---

### Source

Represents a single piece of citable evidence to build user trust.

**Properties:**

- `type` (unknown) ✅ - No description
- `content` (string) ✅ - No description
- `confidence` (unknown) ❌ - No description
- `metadata` (unknown) ❌ - No description

---

### SourceType

Defines the origin of a piece of evidence.

---

### TitleResponse

Simplified title response schema per API spec.

**Properties:**

- `schema_version` (string) ❌ - No description
- `title` (string) ✅ - No description

---

### UploadedData

A strongly-typed model for data uploaded by the user.

**Properties:**

- `id` (string) ✅ - No description
- `name` (string) ✅ - No description
- `type` (unknown) ✅ - No description
- `size_bytes` (integer) ✅ - No description
- `upload_timestamp` (string) ✅ - No description
- `processing_status` (unknown) ✅ - No description
- `processing_summary` (unknown) ❌ - No description
- `confidence_score` (unknown) ❌ - No description

---

### User

Represents a user in the system.

**Properties:**

- `user_id` (string) ✅ - No description
- `email` (string) ✅ - No description
- `name` (string) ✅ - No description
- `created_at` (string) ❌ - No description
- `last_login` (unknown) ❌ - No description

---

### ValidationError

**Properties:**

- `loc` (array) ✅ - No description
- `msg` (string) ✅ - No description
- `type` (string) ✅ - No description

---

### ViewState

Comprehensive view state representing the complete frontend rendering state.
This is the single source of truth for what the frontend should display.

**Properties:**

- `session_id` (string) ✅ - No description
- `user` (unknown) ✅ - No description
- `active_case` (unknown) ❌ - No description
- `cases` (array) ❌ - No description
- `messages` (array) ❌ - No description
- `uploaded_data` (array) ❌ - No description
- `show_case_selector` (boolean) ❌ - No description
- `show_data_upload` (boolean) ❌ - No description
- `loading_state` (unknown) ❌ - No description
- `memory_context` (unknown) ❌ - No description
- `planning_state` (unknown) ❌ - No description

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

