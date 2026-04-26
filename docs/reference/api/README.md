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
**Generated:** 2026-02-11T07:42:01.351963Z

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

### `/admin/optimization/trigger-cleanup`

#### GET

**Trigger System Cleanup**

Trigger comprehensive system cleanup and optimization.

**Responses:**

**200** - Successful Response

---

### `/api/v1/auth/config`

#### GET

**Get Auth Config**

Auth configuration discovery endpoint.

Returns the authentication configuration for the current deployment.
Frontend uses this to determine which auth flow to implement.

**Local Mode Response:**
```json
{
  "auth_mode": "local",
  "login_endpoint": "/api/v1/auth/login",
  "register_endpoint": "/api/v1/auth/register",
  "supports_registration": true,
  "oauth": null
}
```

**Cloud Mode Response:**
```json
{
  "auth_mode": "oauth",
  "login_endpoint": null,
  "register_endpoint": null,
  "supports_registration": false,
  "oauth": {
    "authorize_url": "/auth/oauth/authorize",
    "token_url": "/auth/oauth/token",
    "client_id": "faultmaven-copilot",
    "scopes": ["openid", "profile", "email", "cases:read", "cases:write"]
  }
}
```

**Tags:** `authentication`

**Responses:**

**200** - Successful Response

---

### `/api/v1/auth/dev-delete-user/{username}`

#### DELETE

**Dev Delete User**

Development endpoint to delete a user by username.

Deletes (soft delete) a user by username for development/debugging.
This endpoint is only available in development environments.

**Security**: Gated by require_development_environment dependency.

**Tags:** `authentication`

**Parameters:**

- `username` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/auth/dev-list-users`

#### GET

**Dev List Users**

Development endpoint to list all users.

Returns a list of all users in the system for development/debugging.
This endpoint is only available in development environments.

**Security**: Gated by require_development_environment dependency.

**Tags:** `authentication`

**Responses:**

**200** - Successful Response

---

### `/api/v1/auth/dev-login`

#### POST

**Local Login**

Deprecated: Use /login instead

**Tags:** `authentication`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

```json
{
  "access_token": "550e8400-e29b-41d4-a716-446655440000",
  "expires_in": 86400,
  "session_id": "session-550e8400-e29b-41d4-a716-446655440000",
  "token_type": "bearer",
  "user": {
    "created_at": "2025-01-15T10:00:00Z",
    "display_name": "John Doe",
    "email": "john.doe@faultmaven.local",
    "is_dev_user": true,
    "roles": [
      "user",
      "admin"
    ],
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "john.doe"
  }
}
```

**422** - Validation Error

---

### `/api/v1/auth/dev-register`

#### POST

**Local Register**

Deprecated: Use /register instead

**Tags:** `authentication`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

```json
{
  "access_token": "550e8400-e29b-41d4-a716-446655440000",
  "expires_in": 86400,
  "session_id": "session-550e8400-e29b-41d4-a716-446655440000",
  "token_type": "bearer",
  "user": {
    "created_at": "2025-01-15T10:00:00Z",
    "display_name": "John Doe",
    "email": "john.doe@faultmaven.local",
    "is_dev_user": true,
    "roles": [
      "user",
      "admin"
    ],
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "john.doe"
  }
}
```

**422** - Validation Error

---

### `/api/v1/auth/dev/revoke-all-tokens`

#### POST

**Dev Revoke All User Tokens**

Development endpoint: Revoke all tokens for current user.

This endpoint is only available in development environments.

**Security**: Gated by require_development_environment dependency.

**Tags:** `authentication`

**Parameters:**

- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

```json
{
  "message": "Logged out successfully",
  "revoked_tokens": 1
}
```

**422** - Validation Error

---

### `/api/v1/auth/health`

#### GET

**Auth Health Check**

Authentication system health check

Returns the status of authentication services including token management
and user storage systems.

**Tags:** `authentication`

**Responses:**

**200** - Successful Response

---

### `/api/v1/auth/login`

#### POST

**Local Login**

Internal login implementation for local mode.

Authenticates users and generates JWT tokens.

**Important:** Users must be created before login. Use `./faultmaven.sh create-user`
to create accounts.

**Flow:**
1. Validate username format
2. Find existing user
3. If user doesn't exist: Return 401 (user must be created first)
4. Generate JWT access token
5. Return token with user profile

**Security:**
- Users must exist before login (no auto-creation)
- JWT tokens (not opaque tokens) for middleware uniformity
- Input validation and sanitization
- Proper OAuth2-compatible error responses

**Tags:** `authentication`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

```json
{
  "access_token": "550e8400-e29b-41d4-a716-446655440000",
  "expires_in": 86400,
  "session_id": "session-550e8400-e29b-41d4-a716-446655440000",
  "token_type": "bearer",
  "user": {
    "created_at": "2025-01-15T10:00:00Z",
    "display_name": "John Doe",
    "email": "john.doe@faultmaven.local",
    "is_dev_user": true,
    "roles": [
      "user",
      "admin"
    ],
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "john.doe"
  }
}
```

**422** - Validation Error

---

### `/api/v1/auth/logout`

#### POST

**Logout**

Logout current user

Revokes the current authentication token. The user will need to login
again to access protected resources.

**Flow:**
1. Validate current authentication
2. Revoke the current token
3. Return confirmation

**Tags:** `authentication`

**Parameters:**

- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

```json
{
  "message": "Logged out successfully",
  "revoked_tokens": 1
}
```

**422** - Validation Error

---

### `/api/v1/auth/me`

#### GET

**Get Current User Profile**

Get current user profile

Returns detailed information about the currently authenticated user,
including profile data and token statistics.

**Tags:** `authentication`

**Parameters:**

- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

```json
{
  "created_at": "2025-01-15T10:00:00Z",
  "display_name": "John Doe",
  "email": "john.doe@faultmaven.local",
  "is_dev_user": true,
  "last_login": "2025-01-15T14:30:00Z",
  "roles": [
    "user",
    "admin"
  ],
  "token_count": 2,
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "john.doe"
}
```

**422** - Validation Error

---

### `/api/v1/auth/register`

#### POST

**Local Register**

Local mode registration endpoint.

Creates a new user account and generates a JWT token.
Available only when AUTH_MODE=local.

**Flow:**
1. Validate username format
2. Check if user already exists (returns 409 if exists)
3. Create new user account
4. Generate JWT access token
5. Return token with user profile

**Security:**
- Prevents duplicate account creation
- JWT tokens (not opaque tokens) for middleware uniformity
- Input validation and sanitization
- Auto-generates email and display name if not provided

**Tags:** `authentication`

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

```json
{
  "access_token": "550e8400-e29b-41d4-a716-446655440000",
  "expires_in": 86400,
  "session_id": "session-550e8400-e29b-41d4-a716-446655440000",
  "token_type": "bearer",
  "user": {
    "created_at": "2025-01-15T10:00:00Z",
    "display_name": "John Doe",
    "email": "john.doe@faultmaven.local",
    "is_dev_user": true,
    "roles": [
      "user",
      "admin"
    ],
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "john.doe"
  }
}
```

**422** - Validation Error

---

### `/api/v1/cases`

#### GET

**List Cases**

List user's cases with pagination (v2.0 milestone-based)

Returns CaseListResponse with:
- List of CaseSummary objects (with milestone progress)
- Total count for pagination
- has_more flag

Default Filtering Behavior:
- INCLUDES empty cases (current_turn == 0) - newly created cases are visible
- EXCLUDES archived/closed cases unless include_archived=true
- Use include_empty=false to hide cases with no conversation yet
- Use status filter to further refine results

**Tags:** `cases`

**Parameters:**

- `status` (query) ❌ - Filter by status
- `limit` (query) ❌ - Items per page
- `offset` (query) ❌ - Number of items to skip
- `include_empty` (query) ❌ - Include cases with current_turn == 0 (newly created)
- `include_archived` (query) ❌ - Include archived/closed cases
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Create Case**

Create a new troubleshooting case (v2.0 milestone-based)

Creates a new case with milestone-based investigation tracking.
Initial status is INQUIRY (problem definition phase).

Returns CaseSummary with basic case info and milestone progress.

**Tags:** `cases`

**Parameters:**

- `Authorization` (header) ❌ - No description

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

**Tags:** `cases`

**Responses:**

**200** - Successful Response

---

### `/api/v1/cases/search`

#### POST

**Search Cases**

Search cases by content

Searches case titles, descriptions, case IDs, and optionally message content
for the specified query terms.

**Tags:** `cases`

**Parameters:**

- `Authorization` (header) ❌ - No description

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

**Title Auto-Generation**: If title is not provided or empty, the backend
automatically generates a unique title in the format: Case-MMDD-N
(e.g., Case-1028-1, Case-1028-2). The sequence counter resets daily.

Supports idempotency via 'idempotency-key' header to prevent duplicate case
creation on retry when using force_new=true.

**Tags:** `cases`

**Parameters:**

- `session_id` (path) ✅ - No description
- `title` (query) ❌ - Case title (optional, auto-generated if not provided)
- `force_new` (query) ❌ - Force creation of new case
- `Authorization` (header) ❌ - No description

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

**Tags:** `cases`

**Parameters:**

- `session_id` (path) ✅ - No description
- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}`

#### GET

**Get Case**

Get a specific case by ID (v2.0 milestone-based)

Returns full case details with milestone progress, investigation stage,
and completion percentage.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Case**

Update case details

Updates case metadata such as title, description, status, priority, and tags.
Requires edit permissions on the case.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

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

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**204** - Case deleted successfully

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/access-check`

#### GET

**Check Case Access**

Check if current user has access to this case.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/analytics`

#### GET

**Get Case Analytics**

Get case analytics and metrics

Returns analytics data including message counts, participant activity,
resolution time, and other case metrics.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/close`

#### POST

**Close Case**

Close case and archive with reports.

Marks all latest reports as linked to case closure and transitions
case to CLOSED state.

Returns:
    CaseClosureResponse with list of archived reports

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

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

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `limit` (query) ❌ - Maximum number of items to return
- `offset` (query) ❌ - Number of items to skip
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

> **REMOVED (2026-02-22)**: This endpoint has been replaced by `POST /cases/{case_id}/turns`.
> Returns `410 Gone`. See the unified turns endpoint below.

---

### `/api/v1/cases/{case_id}/data/{data_id}`

#### GET

**Get Case Data**

Get specific data file details for a case.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `data_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Case Data**

Remove data file from a case. Returns 204 No Content on success.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `data_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**204** - Data deleted successfully

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/diff`

#### GET

**Diff two case states**

Compute the semantic difference between two turns of a case.

Returns a dictionary describing added, removed, and modified fields.

**Tags:** `cases`, `replay`

**Parameters:**

- `case_id` (path) ✅ - No description
- `from` (query) ✅ - Start turn number
- `to` (query) ✅ - End turn number
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/evidence/{evidence_id}`

#### GET

**Get evidence details with source file**

Retrieve detailed evidence information including source file reference and hypothesis linkage.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `evidence_id` (path) ✅ - Evidence ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/extract-knowledge`

#### POST

**Extract Knowledge from Case**

Extract reusable knowledge from a case into a suggestion for the knowledge base.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID to extract knowledge from
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/messages`

#### GET

**Get Case Messages Enhanced**

Retrieve conversation messages for a case with enhanced debugging info.
Supports pagination and includes metadata about message retrieval status.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `limit` (query) ❌ - Maximum number of messages to return
- `offset` (query) ❌ - Offset for pagination
- `include_debug` (query) ❌ - Include debug information for troubleshooting
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/participants`

#### GET

**Get Case Participants**

Get all participants who have access to this case.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/queries`

#### GET

**List Case Queries**

List queries for a specific case with pagination.

CRITICAL: Must return 200 [] for empty results, NOT 404

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

> **REMOVED (2026-02-22)**: This endpoint has been replaced by `POST /cases/{case_id}/turns`.
> Returns `410 Gone`. See the unified turns endpoint below.

---

### `/api/v1/cases/{case_id}/turns`

#### POST

**Submit Turn**

Submit a turn to a case investigation. Replaces the old `/queries` and `/data` endpoints.

A turn consists of an optional query and/or optional attachments. Attachments are
preprocessed through Tier 0+1 before the LLM sees them. If no query is provided
with attachments, an implicit query is generated.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case identifier
- `Authorization` (header) ✅ - Bearer token

**Request Body:**

Content-Type: `multipart/form-data`

- `query` (string, optional) - User's message text
- `files` (file[], optional) - File attachments
- `pasted_content` (string, optional) - Pasted text data (treated as attachment)
- `intent_type` (string, optional) - Intent type (conversation, status_transition, confirmation, hypothesis_action)
- `intent_data` (string, optional) - JSON-encoded intent metadata

**Responses:**

**200** - TurnResponse with:
- `agent_response`: Agent's response text
- `turn_number`: Current turn number
- `milestones_completed`: List of completed milestone names
- `case_status`: Current case status
- `progress_made`: Whether investigation progressed
- `is_stuck`: Whether investigation is stuck
- `attachments_processed`: List of AttachmentResult objects

**404** - Case not found

**422** - Validation Error

**503** - Investigation service unavailable

---

### `/api/v1/cases/{case_id}/report-recommendations`

#### GET

**Get Report Recommendations**

Get intelligent report recommendations for a resolved case.

Returns recommendations for which reports to generate, including
intelligent runbook suggestions based on similarity search of existing
runbooks (both incident-driven and document-driven sources).

Recommendation Logic:
- Always available: Resolution Summary / Closure Summary (auto-generated at terminal transition)
- Conditional: Runbook (based on readiness + similarity search)
    - ≥85% similarity: Recommend reuse existing runbook
    - 70-84% similarity: Offer both review OR generate options
    - <70% similarity: Recommend generate new runbook

Args:
    case_id: Case identifier
    case_service: Injected case service
    current_user: Authenticated user

Returns:
    ReportRecommendation with available types and runbook suggestion

Raises:
    400: Case not in resolved state
    404: Case not found or access denied
    500: Internal server error

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/reports`

#### GET

**Get Case Reports**

Retrieve generated reports for a case.

Args:
    case_id: Case identifier
    include_history: If True, return all report versions; if False, only current
    report_type: Optional filter by report type (incident_report, runbook, post_mortem)

Returns:
    List of CaseReport objects

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `include_history` (query) ❌ - No description
- `report_type` (query) ❌ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Generate Case Reports**

Generate case documentation reports.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/reports/{report_id}/download`

#### GET

**Download Case Report**

Download case report in specified format.

Args:
    case_id: Case identifier
    report_id: Report identifier
    format: Output format (markdown or pdf) - currently only markdown supported

Returns:
    File response with report content

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `report_id` (path) ✅ - No description
- `format` (query) ❌ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions`

#### GET

**List Sessions**

List sessions for case.

Retrieves all investigation sessions for a case with optional filtering.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Query Parameters:
    status: Filter by session status (active, paused, completed, abandoned)
    limit: Maximum number of results (1-100, default 50)
    offset: Pagination offset (default 0)

Args:
    case_id: Case to list sessions for
    current_user: Authenticated user from JWT
    status_filter: Optional status filter
    limit: Page size
    offset: Pagination offset
    session_service: Injected session service

Returns:
    List of sessions for the case

Raises:
    401: Authentication required
    404: Case not found

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `status` (query) ❌ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Create Session**

Create investigation session for case.

Creates a new investigation session for the specified case.
Only one active session is allowed per case at a time.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Args:
    case_id: Case to create session for
    request: Session creation request
    current_user: Authenticated user from JWT
    session_service: Injected session service

Returns:
    Created session details

Raises:
    401: Authentication required
    404: Case not found
    409: Active session already exists
    422: Validation error

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/active`

#### GET

**Get Active Session**

Get currently active session for case.

Returns the currently active investigation session for a case,
if one exists. Each case can have at most one active session.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Args:
    case_id: Case to get active session for
    current_user: Authenticated user from JWT
    session_service: Injected session service

Returns:
    Active session if exists, null otherwise

Raises:
    401: Authentication required
    404: Case not found

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}`

#### GET

**Get Session**

Get session by ID.

Retrieves a specific investigation session by its ID.
The session must belong to a case owned by the organization.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Args:
    case_id: Case the session belongs to
    session_id: Unique session identifier
    current_user: Authenticated user from JWT
    session_service: Injected session service

Returns:
    Session details if found and authorized

Raises:
    401: Authentication required
    404: Session not found or case not found

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `session_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PATCH

**Update Session**

Update session.

Updates specified fields of an investigation session.
Only session_goal, token_budget_limit, and metadata can be updated.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Args:
    case_id: Case the session belongs to
    session_id: Unique session identifier
    request: Fields to update
    current_user: Authenticated user from JWT
    session_service: Injected session service

Returns:
    Updated session details

Raises:
    401: Authentication required
    404: Session not found
    422: Validation error

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `session_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}/complete`

#### POST

**Complete Session**

Complete session with findings.

Completes an investigation session with a findings summary.
This is a terminal action - completed sessions cannot be modified.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Body:
    findings_summary: Summary of investigation findings

Args:
    case_id: Case the session belongs to
    session_id: Unique session identifier
    findings_summary: Summary of investigation findings
    current_user: Authenticated user from JWT
    session_service: Injected session service

Returns:
    Updated session with completed status

Raises:
    401: Authentication required
    404: Session not found
    400: Session already in terminal state
    422: Missing findings summary

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `session_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}/execute`

#### POST

**Execute AI agent for troubleshooting investigation**

Execute an AI agent to analyze the case and generate recommendations.
Supports streaming (SSE) or non-streaming mode.

**Authentication:**
- JWT Bearer token: Authorization: Bearer <token>

**Streaming Mode (stream=true, default):**
Returns Server-Sent Events (SSE) with real-time updates including:
- `started`: Execution has begun
- `thinking`: Agent is reasoning/processing
- `tool_call`: Tool invocation requested
- `tool_result`: Tool execution completed
- `response`: Incremental response chunk
- `error`: Error occurred
- `completed`: Execution finished

**Non-Streaming Mode (stream=false):**
Returns complete AgentExecutionResponse when done.

The agent will:
- Analyze case context and previous conversation
- Use available tools (read evidence, search knowledge)
- Generate hypotheses and recommendations
- Stream thinking process in real-time

Token usage is tracked and the session will auto-pause if budget is exceeded.

**Tags:** `Agent Execution`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `session_id` (path) ✅ - Investigation session ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Agent execution completed (non-streaming) or SSE stream (streaming)

**404** - Session not found

**403** - Forbidden - wrong organization

**409** - Conflict - session not active or budget exceeded

**422** - Validation error

**500** - LLM or tool execution error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}/executions`

#### GET

**List executions for case**

List all agent executions for the case.

**Note**: Executions are stored at the case level, not the session level.
The session_id in the path is for URL consistency with the execute endpoint,
but filtering is done by case_id. All executions for the case are returned
regardless of which session initiated them.

**Tags:** `Agent Execution`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `session_id` (path) ✅ - Session ID (for URL consistency, not used for filtering)
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}/executions/{execution_id}`

#### GET

**Get execution by ID**

Get details of a specific agent execution.

**Tags:** `Agent Execution`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `session_id` (path) ✅ - Session ID (for URL consistency)
- `execution_id` (path) ✅ - Execution ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}/executions/{execution_id}/cancel`

#### POST

**Cancel running execution**

Cancel a running agent execution.

**Tags:** `Agent Execution`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `session_id` (path) ✅ - Session ID (for URL consistency)
- `execution_id` (path) ✅ - Execution ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}/pause`

#### POST

**Pause Session**

Pause active session.

Pauses an active investigation session. Only active sessions
can be paused. Paused sessions can be resumed later.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Args:
    case_id: Case the session belongs to
    session_id: Unique session identifier
    current_user: Authenticated user from JWT
    session_service: Injected session service

Returns:
    Updated session with paused status

Raises:
    401: Authentication required
    404: Session not found
    400: Session not active (cannot pause)

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `session_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/sessions/{session_id}/resume`

#### POST

**Resume Session**

Resume paused session.

Resumes a paused investigation session. Only paused sessions
can be resumed.

Authentication:
    - JWT Bearer token: Authorization: Bearer <token>

Args:
    case_id: Case the session belongs to
    session_id: Unique session identifier
    current_user: Authenticated user from JWT
    session_service: Injected session service

Returns:
    Updated session with active status

Raises:
    401: Authentication required
    404: Session not found
    400: Session not paused (cannot resume)

**Tags:** `Sessions`

**Parameters:**

- `case_id` (path) ✅ - No description
- `session_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/share`

#### POST

**Share Case**

Share a case with another user. Requires owner or collaborator permission.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/share/{target_user_id}`

#### DELETE

**Unshare Case**

Unshare a case from a user. Requires owner permission.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `target_user_id` (path) ✅ - User ID to unshare from
- `Authorization` (header) ❌ - No description

**Responses:**

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/snapshot/{turn_number}`

#### GET

**Get case snapshot at specific turn**

Get the full state of a case at a specific turn number.

This is a read-only operation that reconstructs the case from the checkpoint.

**Tags:** `cases`, `replay`

**Parameters:**

- `case_id` (path) ✅ - No description
- `turn_number` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

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

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `force` (query) ❌ - Only overwrite non-default titles when true
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/ui`

#### GET

**Get Case Ui**

Get phase-adaptive UI-optimized case response.

Returns different response schemas based on case status:
- INQUIRY: Focus on problem understanding, clarifying questions
- INVESTIGATING: Milestone progress, hypotheses, evidence, working conclusion
- RESOLVED: Root cause, solution, verification, resolution summary

This endpoint eliminates multiple API calls by returning all UI state
in a single response optimized for the current investigation phase.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/uploaded-files`

#### GET

**List uploaded files with evidence counts**

Get all uploaded files for a case with metadata and evidence linkage counts.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/cases/{case_id}/uploaded-files/{file_id}`

#### GET

**Get uploaded file details with derived evidence**

Retrieve detailed information about an uploaded file including all evidence derived from it and hypothesis linkage.

**Tags:** `cases`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `file_id` (path) ✅ - File ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/evidence`

#### GET

**List Evidence**

List evidence with optional filters.

Args:
    case_id: Filter by case UUID
    uploaded_by: Filter by uploader user ID
    tags: Filter by tags (comma-separated)
    filename_contains: Filter by filename substring
    limit: Max results (default 50, max 200)
    offset: Pagination offset
    current_user: Authenticated user
    service: Evidence service

Returns:
    List of evidence records

**Tags:** `evidence`

**Parameters:**

- `case_id` (query) ❌ - No description
- `uploaded_by` (query) ❌ - No description
- `tags` (query) ❌ - No description
- `filename_contains` (query) ❌ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Upload Evidence**

Upload evidence file.

Args:
    file: Evidence file to upload
    description: Optional description of the evidence
    tags: Optional comma-separated tags
    case_id: Optional case UUID to auto-link
    current_user: Authenticated user
    service: Evidence service

Returns:
    Created evidence record

**Tags:** `evidence`

**Request Body:**

Content-Type: `multipart/form-data`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/evidence/case/{case_id}`

#### GET

**Get Evidence For Case**

Get all evidence linked to a specific case.

Args:
    case_id: Case UUID
    current_user: Authenticated user
    service: Evidence service

Returns:
    List of evidence records for the case

**Tags:** `evidence`

**Parameters:**

- `case_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/evidence/{evidence_id}`

#### GET

**Get Evidence**

Get evidence details by ID.

Args:
    evidence_id: Evidence UUID
    current_user: Authenticated user
    service: Evidence service

Returns:
    Evidence record

Raises:
    HTTPException: 404 if evidence not found, 503 if service unavailable

**Tags:** `evidence`

**Parameters:**

- `evidence_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Evidence**

Delete evidence file and record.

Args:
    evidence_id: Evidence UUID
    current_user: Authenticated user
    service: Evidence service

Raises:
    HTTPException: 404 if evidence not found, 503 if service unavailable

**Tags:** `evidence`

**Parameters:**

- `evidence_id` (path) ✅ - No description

**Responses:**

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/evidence/{evidence_id}/download`

#### GET

**Download Evidence**

Download evidence file.

Args:
    evidence_id: Evidence UUID
    current_user: Authenticated user
    service: Evidence service

Returns:
    Redirect to download URL

Raises:
    HTTPException: 404 if evidence not found, 503 if service unavailable

**Tags:** `evidence`

**Parameters:**

- `evidence_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/evidence/{evidence_id}/link`

#### POST

**Link Evidence To Case**

Link evidence to a case.

Args:
    evidence_id: Evidence UUID
    link_request: Case ID to link to
    current_user: Authenticated user
    service: Evidence service

Returns:
    Updated evidence record

Raises:
    HTTPException: 404 if evidence not found

**Tags:** `evidence`

**Parameters:**

- `evidence_id` (path) ✅ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/analytics/search`

#### GET

**Get Search Analytics**

Get search analytics and insights.

**Tags:** `knowledge_base`

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

**Tags:** `knowledge_base`

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

**Tags:** `knowledge_base`

**Parameters:**

- `Authorization` (header) ❌ - No description

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

**Tags:** `knowledge_base`

**Parameters:**

- `Authorization` (header) ❌ - No description

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

**Tags:** `knowledge_base`

**Parameters:**

- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/documents/search`

#### POST

**Fulltext Search Documents**

Full-text search for knowledge base documents (Microservices Parity)

Implements full-text search complementing the semantic search at /knowledge/search.
This endpoint provides simple keyword-based text matching across document titles
and content, useful when semantic understanding is not required.

**Differences from /knowledge/search:**
- `/knowledge/search` - Semantic vector search using embeddings (similarity-based)
- `/documents/search` - Full-text keyword search (exact/partial word matching)

**Use Cases:**
- Searching for specific error codes or identifiers
- Finding documents with exact phrases
- Faster search when semantic understanding not needed
- Filtering by document_type, category, tags

**Request Body:**
```json
{
    "query": "PostgreSQL connection timeout",
    "document_type": "kb_article",
    "category": "database",
    "tags": "postgresql,timeout",
    "limit": 20,
    "similarity_threshold": 0.5
}
```

**Returns:**
```json
{
    "query": "...",
    "total_results": 5,
    "results": [
        {
            "document_id": "...",
            "content": "...",
            "metadata": {
                "title": "...",
                "document_type": "...",
                "category": "...",
                "tags": [...],
                "priority": "..."
            },
            "similarity_score": 0.85
        }
    ]
}
```

**Tags:** `knowledge_base`

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

**Tags:** `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Document**

Update document metadata and content.

**Tags:** `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

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

**Tags:** `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/documents/{document_id}/snippet`

#### GET

**Get Document Snippet**

Get a snippet/preview of a knowledge base document for hover cards.

Supports two modes:
1. **Line-based extraction**: Extract lines from line_start to line_end (or max_lines)
2. **Semantic extraction**: If query_string is provided, returns the most relevant
   snippet based on vector similarity (more robust than line numbers after edits)

Args:
    document_id: Document identifier
    line_start: Starting line number (1-indexed, default: 1)
    line_end: Ending line number (optional, computed from max_lines if not provided)
    max_lines: Maximum lines to return (default: 5, max: 50)
    query_string: Query for semantic snippet extraction (optional)

Returns:
    Document snippet with verification status for badge display

**Tags:** `knowledge_base`

**Parameters:**

- `document_id` (path) ✅ - No description
- `line_start` (query) ❌ - Starting line number
- `line_end` (query) ❌ - Ending line number
- `max_lines` (query) ❌ - Maximum lines to return
- `query_string` (query) ❌ - Query for semantic snippet extraction

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

**Tags:** `knowledge_base`

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

**Tags:** `knowledge_base`

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

**Tags:** `knowledge_base`

**Responses:**

**200** - Successful Response

---

### `/api/v1/knowledge/suggestions`

#### GET

**List Suggestions**

List knowledge suggestions with optional filtering.

Returns suggestions extracted from cases that are pending review.
Includes lineage information for each suggestion (source case, extractor, timestamp).

Args:
    status: Filter by status (pending_review, approved, rejected)
    limit: Maximum suggestions to return (default: 20)
    offset: Pagination offset (default: 0)

Returns:
    SuggestionListResponse with paginated suggestions

**Tags:** `knowledge_base`

**Parameters:**

- `status` (query) ❌ - Filter by status: pending_review, approved, rejected
- `limit` (query) ❌ - Maximum items to return
- `offset` (query) ❌ - Pagination offset
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/suggestions/{suggestion_id}`

#### GET

**Get Suggestion**

Get a specific knowledge suggestion by ID.

Returns full suggestion details including content, PII scan status,
and lineage information.

Args:
    suggestion_id: Suggestion identifier

Returns:
    KnowledgeSuggestionDetail

**Tags:** `knowledge_base`

**Parameters:**

- `suggestion_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Suggestion**

Update a suggestion's content.

Allows editing the suggested title, content, or type before approval.
Content changes trigger a new PII scan.

Args:
    suggestion_id: Suggestion to update
    update_data: Fields to update (title, content, suggested_type)

Returns:
    Updated suggestion details

**Tags:** `knowledge_base`

**Parameters:**

- `suggestion_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/suggestions/{suggestion_id}/approve`

#### POST

**Approve Suggestion**

Approve a suggestion and create a knowledge item.

Validates that PII scan is complete and clean/remediated before approval.
Creates a new KnowledgeItem with verification_level=2 (admin verified).
Establishes bidirectional link between suggestion and knowledge item.

Args:
    suggestion_id: Suggestion to approve
    request_body: Optional review notes

Returns:
    Approval result with new knowledge_item_id

**Tags:** `knowledge_base`

**Parameters:**

- `suggestion_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/suggestions/{suggestion_id}/reject`

#### POST

**Reject Suggestion**

Reject a suggestion.

Marks the suggestion as rejected with the provided reason.

Args:
    suggestion_id: Suggestion to reject
    request_body: Must include rejection_reason, optional review_notes

Returns:
    Rejection confirmation

**Tags:** `knowledge_base`

**Parameters:**

- `suggestion_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/knowledge/suggestions/{suggestion_id}/remediate-pii`

#### POST

**Remediate Pii**

Mark PII as remediated after manual review.

Called when an admin has manually reviewed and cleaned up
PII-flagged content. Allows the suggestion to proceed to approval.

Args:
    suggestion_id: Suggestion with PII to remediate

Returns:
    Updated suggestion with remediated status

**Tags:** `knowledge_base`

**Parameters:**

- `suggestion_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/organizations`

#### GET

**List User Organizations**

List all organizations the authenticated user belongs to.

**Tags:** `organizations`

**Parameters:**

- `limit` (query) ❌ - Maximum results
- `offset` (query) ❌ - Pagination offset
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Create Organization**

Create a new organization. The creator becomes the organization owner.

**Tags:** `organizations`

**Parameters:**

- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/organizations/by-slug/{slug}`

#### GET

**Get Organization by Slug**

Get organization details by slug. Requires organization membership.

**Tags:** `organizations`

**Parameters:**

- `slug` (path) ✅ - Organization slug
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/organizations/{organization_id}`

#### GET

**Get Organization**

Get organization details by ID. Requires organization membership.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PATCH

**Update Organization**

Update organization details. Requires owner permission.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Organization**

Soft delete an organization. Requires owner permission.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/organizations/{organization_id}/members`

#### GET

**List Organization Members**

List all members of an organization. Requires organization membership.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `role` (query) ❌ - Filter by role: owner, admin, member
- `limit` (query) ❌ - Maximum results
- `offset` (query) ❌ - Pagination offset
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Add Member**

Add user to organization by email. Requires owner or admin permission.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/organizations/{organization_id}/members/{user_id}`

#### PATCH

**Update Member Role**

Update user's role in organization. Requires owner permission.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `user_id` (path) ✅ - User ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Remove Member**

Remove user from organization. Owner can remove anyone except self, admin can remove members only.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `user_id` (path) ✅ - User ID to remove
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/organizations/{organization_id}/permissions/check`

#### POST

**Check Permission**

Check if user has specific permission in organization.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/organizations/{organization_id}/settings`

#### GET

**Get Organization Settings**

Get organization settings and plan limits. Requires organization membership.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PATCH

**Update Organization Settings**

Update organization settings. Requires owner permission.

**Tags:** `organizations`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/reports/case/{case_id}`

#### GET

**List reports for case**

Get all reports associated with a specific case

**Tags:** `reports`

**Parameters:**

- `case_id` (path) ✅ - Case UUID
- `include_history` (query) ❌ - Include all versions or only current
- `report_type` (query) ❌ - Filter by report type
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/reports/generate`

#### POST

**Generate reports for a case**

Generate post-mortem, executive summary, or technical analysis reports using LLM

**Tags:** `reports`

**Parameters:**

- `case_id` (query) ✅ - Case ID to generate reports for
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/reports/recommendations/{case_id}`

#### GET

**Get report recommendations for a case**

Get intelligent recommendations for which reports to generate

**Tags:** `reports`

**Parameters:**

- `case_id` (path) ✅ - Case ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/reports/{report_id}`

#### GET

**Get report by ID**

Retrieve a specific report by its UUID

**Tags:** `reports`

**Parameters:**

- `report_id` (path) ✅ - Report UUID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update report**

Update report title or content

**Tags:** `reports`

**Parameters:**

- `report_id` (path) ✅ - Report UUID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete report**

Permanently delete a report (runbooks cannot be deleted)

**Tags:** `reports`

**Parameters:**

- `report_id` (path) ✅ - Report UUID
- `Authorization` (header) ❌ - No description

**Responses:**

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/reports/{report_id}/link-case`

#### POST

**Link report to case closure**

Mark case as closed and link final report

**Tags:** `reports`

**Parameters:**

- `report_id` (path) ✅ - Report UUID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/reports/{report_id}/versions`

#### GET

**Get report version history**

Retrieve all versions of a report

**Tags:** `reports`

**Parameters:**

- `report_id` (path) ✅ - Report UUID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions`

#### GET

**List Sessions**

List all sessions with optional filtering.

Args:
    user_id: Optional user ID filter
    session_type: Optional session type filter
    limit: Maximum number of sessions to return
    offset: Number of sessions to skip

Returns:
    List of sessions

**Tags:** `session_management`

**Parameters:**

- `user_id` (query) ❌ - No description
- `session_type` (query) ❌ - No description
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

**User ID Resolution:**
- Priority 1: `user_id` query parameter (explicit override)
- Priority 2: Authenticated user from JWT token (prevents anonymous session creation)
- Priority 3: Auto-generated anonymous user (development/unauthenticated only)

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
    current_user: Optional authenticated user from JWT token

Returns:
    Session creation/resumption response with expiration information

**Tags:** `session_management`

**Parameters:**

- `user_id` (query) ❌ - No description
- `Authorization` (header) ❌ - No description

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

**Tags:** `session_management`

**Responses:**

**200** - Successful Response

---

### `/api/v1/sessions/search`

#### POST

**Search Sessions**

Search user's sessions with filters.

Implements microservices parity with fm-session-service.
Searches only the authenticated user's sessions.

Request body:
    {
        "query": "optional text search",
        "status": "optional status filter (active, archived)",
        "limit": 50
    }

Returns:
    {
        "sessions": [...],
        "total": int
    }

**Tags:** `session_management`

**Parameters:**

- `Authorization` (header) ❌ - No description

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

**Tags:** `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Session**

Update session metadata.

Implements microservices parity with fm-session-service.
Updates authentication-related metadata only (not case data).

Args:
    session_id: Session identifier
    updates: Dict of fields to update (metadata, timeout_minutes, etc.)

Returns:
    Updated session information

Raises:
    404: Session not found
    403: User not authorized to update this session
    400: Invalid update fields (trying to update case data)

**Tags:** `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

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

**Tags:** `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/sessions/{session_id}/archive`

#### POST

**Archive Session**

Archive a session.

Implements microservices parity with fm-session-service.
Sets session status to 'archived' while preserving all data.

Args:
    session_id: Session identifier

Returns:
    {
        "session_id": str,
        "status": "archived",
        "message": "Session archived successfully"
    }

Raises:
    404: Session not found
    403: User not authorized to archive this session

**Tags:** `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

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

**Tags:** `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description
- `limit` (query) ❌ - No description
- `offset` (query) ❌ - No description
- `include_empty` (query) ❌ - Include cases with message_count == 0
- `include_terminal` (query) ❌ - Include terminal state cases (resolved/closed)
- `include_deleted` (query) ❌ - Include deleted cases (admin only)
- `Authorization` (header) ❌ - No description

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

**Tags:** `session_management`

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

**Tags:** `session_management`

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

**Tags:** `session_management`

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

**Tags:** `session_management`

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

**Tags:** `session_management`

**Parameters:**

- `session_id` (path) ✅ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/teams`

#### POST

**Create Team**

Create a new team within an organization. The creator becomes the team lead.

**Tags:** `teams`

**Parameters:**

- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/teams/organization/{organization_id}`

#### GET

**List Organization Teams**

List all teams in an organization.

**Tags:** `teams`

**Parameters:**

- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/teams/user/{target_user_id}/organization/{organization_id}`

#### GET

**List User Teams**

List all teams a user belongs to in an organization.

**Tags:** `teams`

**Parameters:**

- `target_user_id` (path) ✅ - User ID
- `organization_id` (path) ✅ - Organization ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/teams/{team_id}`

#### GET

**Get Team**

Get team details by ID.

**Tags:** `teams`

**Parameters:**

- `team_id` (path) ✅ - Team ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### PUT

**Update Team**

Update team details. Requires 'teams.write' permission.

**Tags:** `teams`

**Parameters:**

- `team_id` (path) ✅ - Team ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### DELETE

**Delete Team**

Soft delete a team. Requires 'teams.manage' permission.

**Tags:** `teams`

**Parameters:**

- `team_id` (path) ✅ - Team ID
- `Authorization` (header) ❌ - No description

**Responses:**

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/teams/{team_id}/members`

#### GET

**List Team Members**

List all members of a team.

**Tags:** `teams`

**Parameters:**

- `team_id` (path) ✅ - Team ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

#### POST

**Add Team Member**

Add user to team. Requires 'teams.write' permission.

**Tags:** `teams`

**Parameters:**

- `team_id` (path) ✅ - Team ID
- `Authorization` (header) ❌ - No description

**Request Body:**

Content-Type: `application/json`

**Responses:**

**201** - Successful Response

**422** - Validation Error

---

### `/api/v1/teams/{team_id}/members/{target_user_id}`

#### DELETE

**Remove Team Member**

Remove user from team. Requires 'teams.write' permission.

**Tags:** `teams`

**Parameters:**

- `team_id` (path) ✅ - Team ID
- `target_user_id` (path) ✅ - User ID to remove
- `Authorization` (header) ❌ - No description

**Responses:**

**204** - Successful Response

**422** - Validation Error

---

### `/api/v1/teams/{team_id}/members/{target_user_id}/is-member`

#### GET

**Check Team Membership**

Check if user is member of team.

**Tags:** `teams`

**Parameters:**

- `team_id` (path) ✅ - Team ID
- `target_user_id` (path) ✅ - User ID
- `Authorization` (header) ❌ - No description

**Responses:**

**200** - Successful Response

**422** - Validation Error

---

### `/api/v1/admin/llm/config`

#### GET

**Get LLM Configuration**

Returns the current primary provider, fallback chain, and per-provider status including health, connectivity, and available models. API keys are never exposed — only a boolean indicating whether one is configured.

Available to any authenticated user. Dashboard-side route guard handles deployment-aware access control.

**Auth:** Bearer token (any authenticated user)

**Responses:**

**200** - `LLMConfigResponse` with `primary_provider`, `strict_mode`, `fallback_chain`, and `providers` (map of provider name → `LLMProviderDetail`)

**401** - Unauthorized

**503** - LLM provider not initialized

---

### `/api/v1/admin/llm/config/test`

#### POST

**Test LLM Provider Connection**

Sends a minimal prompt to the specified provider to verify API key validity, endpoint reachability, and model response. Does NOT use the fallback chain — tests the specific provider directly.

**Auth:** Bearer token (any authenticated user)

**Request Body:** `LLMConnectionTestRequest` — `{ "provider": "anthropic" }`

**Responses:**

**200** - `LLMConnectionTestResponse` with `connected`, `response_time_ms`, `model_used`, `error_message`

**401** - Unauthorized

**422** - Unknown provider name

**503** - LLM provider not initialized

---

### `/api/v1/admin/config/status`

#### GET

**Get Environment Configuration Status**

Returns the current deployment configuration including auth mode, storage backends, and security settings. Read-only — configuration changes require editing environment variables and restarting.

**Auth:** Bearer token (any authenticated user)

**Responses:**

**200** - `EnvConfigStatusResponse` with `auth_mode`, `environment`, `db_backend`, `session_storage`, `vector_storage`, `llm_provider`, `pii_redaction_enabled`, `rate_limit_enabled`

**401** - Unauthorized

---

### `/debug/config`

#### GET

**Debug Config** *(development only)*

Get current configuration summary including active preset. Not available in production — use `GET /api/v1/admin/config/status` instead.

**Responses:**

**200** - Successful Response

---

### `/debug/health`

#### GET

**Debug Health** *(development only)*

Minimal debug health endpoint.

**Responses:**

**200** - Successful Response

---

### `/debug/llm-providers`

#### GET

**Debug LLM Providers** *(development only)*

Get current LLM provider status and fallback chain. Not available in production — use `GET /api/v1/admin/llm/config` instead.

**Responses:**

**200** - Successful Response

---

### `/debug/routes`

#### GET

**Debug Routes** *(development only)*

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

### `/v1/meta/capabilities`

#### GET

**Get Capabilities**

Return backend capabilities for browser extension configuration.

This endpoint is called by the FaultMaven Copilot browser extension
to detect the deployment mode and configure itself accordingly.

Returns:
    Backend capabilities including deployment mode, dashboard URL, and feature flags

**Responses:**

**200** - Successful Response

---

## Data Models

### AgentExecutionRequest

Request model for executing an AI agent.

This model defines the input for agent execution requests,
supporting both streaming and non-streaming modes.

**Properties:**

- `user_message` (string) ✅ - User's question or request for the agent
- `agent_type` (string) ❌ - Type of agent to execute (investigator, debugger, researcher, validator, reporter)
- `stream` (boolean) ❌ - Whether to stream response events (SSE)

**Example:**

```json
{
  "agent_type": "investigator",
  "stream": true,
  "user_message": "What is causing the 500 errors in the API?"
}
```

---

### AgentExecutionResponse

Response model for completed agent execution (non-streaming).

Used when stream=false in the request.

**Properties:**

- `execution_id` (string) ✅ - No description
- `status` (string) ✅ - No description
- `agent_response` (string) ✅ - No description
- `tokens_used` (integer) ✅ - No description
- `started_at` (string) ✅ - No description
- `completed_at` (unknown) ❌ - No description
- `tool_calls` (array) ❌ - No description

---

### AgentResponse

The single, unified JSON payload returned from the backend (v3.1.0 - Evidence-Centric).

**Properties:**

- `schema_version` (string) ❌ - No description
- `content` (string) ✅ - No description
- `response_type` (unknown) ✅ - No description
- `session_id` (string) ✅ - No description
- `case_id` (unknown) ❌ - No description
- `likelihood` (unknown) ❌ - No description
- `sources` (array) ❌ - No description
- `next_action_hint` (unknown) ❌ - No description
- `view_state` (unknown) ❌ - No description
- `plan` (unknown) ❌ - No description
- `evidence_requests` (array) ❌ - Active evidence requests for this turn
- `investigation_mode` (unknown) ❌ - Current investigation approach (speed vs depth)
- `case_status` (unknown) ❌ - Current case investigation state

---

### AuthConfigResponse

Auth configuration discovery response.

Allows frontend to determine which authentication flow to use
based on deployment configuration.

**Properties:**

- `auth_mode` (string) ✅ - No description
- `login_endpoint` (unknown) ❌ - No description
- `register_endpoint` (unknown) ❌ - No description
- `supports_registration` (boolean) ✅ - No description
- `oauth` (unknown) ❌ - No description

---

### AuthSessionCreateRequest

Request model for authentication session creation.

This schema is for auth sessions (user authentication), not investigation sessions.
Investigation sessions use a different schema in the case module.
See: docs/architecture/case-and-session-concepts.md for the three-tier architecture.

**Properties:**

- `timeout_minutes` (unknown) ❌ - Session timeout in minutes. Min: 60 (1 hour), Max: 480 (8 hours), Default: 180 (3 hours)
- `session_type` (unknown) ❌ - No description
- `metadata` (unknown) ❌ - No description
- `client_id` (unknown) ❌ - Client/device identifier for session resumption. If provided, existing session for this client will be resumed.

---

### AuthSessionStatus

Defines the status of authentication sessions (not investigation sessions).

For investigation session status, see faultmaven.models.investigation_session.SessionStatus

---

### AuthTokenResponse

Authentication token response

Standard OAuth2-compatible token response format.
Includes token, expiration, user information, and session ID.

**Properties:**

- `access_token` (string) ✅ - Bearer access token
- `token_type` (string) ❌ - Token type (always 'bearer')
- `expires_in` (integer) ✅ - Token expiration time in seconds
- `session_id` (string) ✅ - Session identifier for multi-turn conversations
- `user` (unknown) ✅ - Authenticated user profile

**Example:**

```json
{
  "access_token": "550e8400-e29b-41d4-a716-446655440000",
  "expires_in": 86400,
  "session_id": "session-550e8400-e29b-41d4-a716-446655440000",
  "token_type": "bearer",
  "user": {
    "created_at": "2025-01-15T10:00:00Z",
    "display_name": "John Doe",
    "email": "john.doe@faultmaven.local",
    "is_dev_user": true,
    "roles": [
      "user",
      "admin"
    ],
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "john.doe"
  }
}
```

---

### Body_complete_session_api_v1_cases__case_id__sessions__session_id__complete_post

**Properties:**

- `findings_summary` (string) ✅ - No description

---

### Body_share_case_api_v1_cases__case_id__share_post

**Properties:**

- `target_user_id` (string) ✅ - User ID to share with
- `role` (string) ❌ - Participant role: owner, collaborator, viewer

---

### Body_upload_case_data_api_v1_cases__case_id__data_post

**Properties:**

- `file` (string) ✅ - No description
- `session_id` (unknown) ❌ - No description
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

### Body_upload_evidence_api_v1_evidence_post

**Properties:**

- `file` (string) ✅ - No description
- `description` (unknown) ❌ - No description
- `tags` (unknown) ❌ - No description
- `case_id` (unknown) ❌ - No description

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
- `owner_id` (string) ✅ - No description

---

### CaseCreateRequest

Request to create a new case (v2.0).

User identity is derived from authentication token, not request body.
This ensures security and prevents user_id spoofing.

**Properties:**

- `title` (unknown) ❌ - Case title (optional, auto-generated if not provided)
- `description` (unknown) ❌ - Initial problem description
- `initial_message` (unknown) ❌ - First user message (for INQUIRY phase)
- `session_id` (unknown) ❌ - Session ID for authentication and case association (restored from old implementation)

---

### CaseDetail

Detailed case information for single case view.

**Properties:**

- `case_id` (string) ✅ - No description
- `title` (string) ✅ - No description
- `description` (string) ✅ - No description
- `status` (unknown) ✅ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description
- `last_activity_at` (string) ✅ - No description
- `resolved_at` (unknown) ✅ - No description
- `closed_at` (unknown) ✅ - No description
- `user_id` (string) ✅ - No description
- `organization_id` (string) ✅ - No description
- `closure_reason` (unknown) ✅ - No description
- `current_turn` (integer) ✅ - No description
- `turns_without_progress` (integer) ✅ - No description
- `current_stage` (unknown) ✅ - No description
- `milestones_completed` (array) ✅ - No description
- `pending_milestones` (array) ✅ - No description
- `evidence_count` (integer) ✅ - No description
- `hypothesis_count` (integer) ✅ - No description
- `solution_count` (integer) ✅ - No description
- `is_stuck` (boolean) ✅ - No description
- `is_terminal` (boolean) ✅ - No description
- `degraded_mode_active` (boolean) ✅ - No description
- `escalated` (boolean) ✅ - No description
- `valid_next_states` (array) ❌ - Allowed status transitions from current state for user-initiated changes

---

### CaseListResponse

Response for case listing.

**Properties:**

- `cases` (array) ✅ - No description
- `total_count` (integer) ✅ - No description
- `limit` (integer) ✅ - No description
- `offset` (integer) ✅ - No description
- `has_more` (boolean) ✅ - No description

---

### CaseMessagesResponse

Enhanced response model for case message retrieval with debugging support.

**Properties:**

- `messages` (array) ✅ - Array of conversation messages
- `total_count` (integer) ✅ - Total number of messages in the case
- `retrieved_count` (integer) ✅ - Number of messages successfully retrieved
- `has_more` (boolean) ✅ - Whether more messages are available for pagination
- `next_offset` (unknown) ❌ - Offset for next page (null if no more pages)
- `debug_info` (unknown) ❌ - Debug information (only when include_debug=true)

---

### ~~CaseQueryRequest~~ (REMOVED)

> Removed in Unified Ingestion Pipeline (2026-02-22). Replaced by `TurnPayload` (internal dataclass).
> Clients now use `POST /cases/{case_id}/turns` with multipart form data.

---

### ~~CaseQueryResponse~~ (REMOVED)

> Removed in Unified Ingestion Pipeline (2026-02-22). Replaced by `TurnResponse`.

---

### TurnResponse

Response for turn submission.

Returned by `POST /cases/{case_id}/turns` endpoint.

**Properties:**

- `agent_response` (string) ✅ - Agent's response text
- `turn_number` (integer) ✅ - Current turn number
- `milestones_completed` (array) ✅ - Completed milestone names
- `case_status` (CaseStatus) ✅ - Current case status
- `progress_made` (boolean) ✅ - Whether investigation progressed
- `is_stuck` (boolean) ✅ - Whether investigation is stuck
- `attachments_processed` (AttachmentResult[]) ❌ - Results of preprocessed attachments

---

### AttachmentResult

Result of preprocessing a single attachment.

**Properties:**

- `evidence_id` (string) ✅ - Evidence ID created from the attachment
- `filename` (string) ✅ - Original filename
- `data_type` (string) ✅ - Classified data type (logs, metrics, etc.)
- `file_size` (integer) ✅ - File size in bytes
- `processing_status` (string) ✅ - Processing status (completed/failed)

---

### CaseReport

Generated case documentation report (DR-005).
Supports DUAL runbook sources:
- Incident-driven: Generated from case resolution
- Document-driven: Generated from uploaded documentation

**Properties:**

- `report_id` (string) ❌ - Unique report identifier (UUID v4)
- `case_id` (string) ✅ - Foreign key to parent case (or 'doc-derived' for document-driven)
- `report_type` (unknown) ✅ - Type of report
- `title` (string) ✅ - Human-readable title
- `content` (string) ✅ - Full report content in Markdown format
- `format` (string) ❌ - Report format
- `generation_status` (unknown) ✅ - Generation status
- `generated_at` (string) ❌ - ISO 8601 timestamp when report was first generated
- `updated_at` (unknown) ❌ - ISO 8601 timestamp when report was last updated (None for new reports, set on update)
- `generation_time_ms` (integer) ✅ - Generation time (ms)
- `is_current` (boolean) ❌ - Latest version for this report_type
- `version` (integer) ❌ - Version number
- `linked_to_closure` (boolean) ❌ - Linked to case closure
- `metadata` (unknown) ❌ - Runbook-specific metadata

---

### CaseSearchRequest

Request to search cases.

**Properties:**

- `query` (string) ✅ - Search query
- `user_id` (unknown) ❌ - Limit to user's cases
- `organization_id` (unknown) ❌ - Limit to organization's cases
- `status` (unknown) ❌ - Filter by status
- `limit` (integer) ❌ - Maximum results

---

### CaseStatus

Case lifecycle status.

Lifecycle Flow:
  INQUIRY -> INVESTIGATING -> RESOLVED (terminal)
                             -> CLOSED (terminal)
           -> CLOSED (terminal)

Terminal States: RESOLVED, CLOSED (no further transitions)

---

### CaseStatusTransition

Record of one status change.
Provides audit trail for case lifecycle.

**Properties:**

- `from_status` (unknown) ✅ - Status before transition
- `to_status` (unknown) ✅ - Status after transition
- `triggered_at` (string) ❌ - When transition occurred
- `triggered_by` (string) ✅ - Who triggered: user_id or 'system' for automatic transitions
- `reason` (string) ✅ - Human-readable reason for transition

---

### CaseSummary

Minimal case information for list views.

**Properties:**

- `case_id` (string) ✅ - No description
- `title` (string) ✅ - No description
- `description` (string) ✅ - No description
- `status` (unknown) ✅ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description
- `last_activity_at` (string) ✅ - No description
- `resolved_at` (unknown) ✅ - No description
- `closed_at` (unknown) ✅ - No description
- `user_id` (string) ✅ - No description
- `organization_id` (string) ✅ - No description
- `closure_reason` (unknown) ✅ - No description
- `current_turn` (integer) ✅ - No description
- `milestones_completed` (integer) ✅ - No description
- `total_milestones` (integer) ❌ - No description
- `is_stuck` (boolean) ✅ - No description
- `is_terminal` (boolean) ✅ - No description
- `valid_next_states` (array) ❌ - Allowed status transitions from current state for user-initiated changes

---

### CaseUIResponse_Inquiry

UI response for INQUIRY phase.

Focus: Understanding the problem, asking clarifying questions.
User hasn't committed to full investigation yet.

**Properties:**

- `case_id` (string) ✅ - Case identifier
- `status` (string) ❌ - Always 'inquiry' for this response type
- `title` (string) ✅ - Case title
- `current_turn` (integer) ✅ - Current turn counter
- `created_at` (string) ✅ - When case was created
- `updated_at` (string) ✅ - Last update timestamp
- `uploaded_files_count` (integer) ✅ - Total files uploaded
- `valid_next_states` (array) ❌ - Allowed status transitions from current state for user-initiated changes
- `inquiry` (unknown) ✅ - Nested inquiry phase data

---

### CaseUIResponse_Investigating

UI response for INVESTIGATING phase.

Focus: Active investigation, milestone progress, hypothesis testing.
User has committed to investigation and agent is working through milestones.

**Properties:**

- `case_id` (string) ✅ - Case identifier
- `status` (string) ❌ - Always 'investigating' for this response type
- `title` (string) ✅ - Case title
- `current_turn` (integer) ✅ - Current turn counter
- `created_at` (string) ✅ - When case was created
- `updated_at` (string) ✅ - Last update timestamp
- `valid_next_states` (array) ❌ - Allowed status transitions from current state for user-initiated changes
- `working_conclusion` (unknown) ❌ - Agent's current understanding of the problem
- `progress` (unknown) ✅ - Milestone-based progress tracking
- `active_hypotheses` (array) ❌ - Hypotheses currently being tested
- `latest_evidence` (array) ❌ - Most recent evidence collected (last 5)
- `next_actions` (array) ❌ - Suggested next steps for investigation
- `agent_status` (string) ✅ - What agent is currently doing
- `is_stuck` (boolean) ❌ - Whether investigation is stuck (no progress for 3+ turns)
- `degraded_mode` (boolean) ❌ - Whether investigation is in degraded mode
- `investigation_strategy` (unknown) ❌ - Investigation strategy with approach and next steps
- `problem_verification` (unknown) ❌ - Problem verification details (urgency, severity, impact)

---

### CaseUIResponse_Resolved

UI response for RESOLVED phase.

Focus: Resolution summary, root cause, solution applied, verification.
Investigation complete, case closed with solution.

**Properties:**

- `case_id` (string) ✅ - Case identifier
- `status` (string) ✅ - Case terminal status: 'resolved' (with solution) or 'closed' (without investigation)
- `title` (string) ✅ - Case title
- `current_turn` (integer) ✅ - Current turn counter
- `created_at` (string) ✅ - When case was created
- `updated_at` (string) ✅ - Last update timestamp
- `resolved_at` (string) ✅ - When case was resolved
- `valid_next_states` (array) ❌ - Allowed status transitions from current state for user-initiated changes
- `root_cause` (unknown) ✅ - What caused the problem
- `solution_applied` (unknown) ✅ - Solution that fixed the problem
- `verification_status` (unknown) ✅ - How solution effectiveness was verified
- `resolution_summary` (unknown) ✅ - Overall resolution metrics and insights
- `reports_available` (array) ❌ - Available reports (incident report, post-mortem, runbook)

---

### CaseUpdateRequest

Request to update an existing case.

**Properties:**

- `title` (unknown) ❌ - Updated title
- `description` (unknown) ❌ - Updated description
- `status` (unknown) ❌ - Updated status (admin only)

---

### Change

Recent change that may be relevant to the problem.

**Properties:**

- `description` (string) ✅ - What changed
- `occurred_at` (string) ✅ - When the change occurred
- `change_type` (string) ✅ - Type of change: deployment | config | scaling | code | infrastructure | data | other
- `changed_by` (unknown) ❌ - Who made the change (user, system, team)
- `details` (unknown) ❌ - Additional structured details (version numbers, config values, etc.)

---

### ConfidenceLevel

Categorical confidence levels.
Maps to numeric confidence scores.

---

### Correlation

Correlation between a change and the symptom.

**Properties:**

- `change_description` (string) ✅ - Description of the change
- `timing_description` (string) ✅ - Temporal relationship: '2 minutes before', 'immediately after', 'coincides with', etc.
- `confidence` (number) ✅ - Confidence in this correlation (0.0 = weak, 1.0 = strong)
- `correlation_type` (string) ✅ - Type: temporal | causal | coincidental | other
- `evidence` (unknown) ❌ - Evidence supporting this correlation

---

### DataType

12 purpose-driven data classifications for preprocessing pipeline.

---

### ~~DataUploadResponse~~ (REMOVED)

> Removed in Unified Ingestion Pipeline (2026-02-22). File uploads are now handled
> through `POST /cases/{case_id}/turns` with multipart form data. Results are returned
> in `TurnResponse.attachments_processed` as `AttachmentResult` objects.

---

### DegradedMode

Investigation is blocked or struggling.
Agent offers fallback options.

**Properties:**

- `mode_type` (unknown) ✅ - Why investigation degraded
- `entered_at` (string) ❌ - When degraded mode was entered
- `reason` (string) ✅ - Detailed explanation of why investigation degraded
- `attempted_actions` (array) ❌ - What agent tried before degrading
- `fallback_offered` (unknown) ❌ - Fallback option presented to user
- `user_choice` (unknown) ❌ - How user responded: 'accept_fallback' | 'provide_more_data' | 'escalate' | 'abandon'
- `exited_at` (unknown) ❌ - When degraded mode was exited (if recovered)
- `exit_reason` (unknown) ❌ - How investigation recovered from degraded mode

---

### DegradedModeType

Reason for entering degraded mode

---

### DeleteResponse

Generic delete response

**Properties:**

- `message` (string) ✅ - No description
- `organization_id` (unknown) ❌ - No description
- `user_id` (unknown) ❌ - No description

---

### DerivedEvidenceSummary

Summary of evidence derived from an uploaded file.

**Properties:**

- `evidence_id` (string) ✅ - No description
- `summary` (string) ✅ - No description
- `category` (string) ✅ - SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | RESOLUTION_EVIDENCE | OTHER
- `collected_at_turn` (integer) ✅ - No description
- `related_hypothesis_ids` (array) ❌ - No description

---

### DevLoginRequest

Request model for development login

Validates user input for the dev-login endpoint.
Supports username-based login with optional user details.

**Properties:**

- `username` (string) ✅ - Username or email address (3-50 chars)
- `email` (unknown) ❌ - Optional email address (will auto-generate if not provided)
- `display_name` (unknown) ❌ - Optional display name (will auto-generate if not provided)

**Example:**

```json
{
  "display_name": "John Doe",
  "email": "john.doe@faultmaven.local",
  "username": "john.doe"
}
```

---

### DocumentSnippetResponse

Response model for document snippet (hover card preview).

Supports both line-based and semantic snippet extraction.

**Properties:**

- `document_id` (string) ✅ - No description
- `title` (string) ✅ - No description
- `snippet` (string) ✅ - No description
- `line_range` (unknown) ❌ - No description
- `total_lines` (integer) ✅ - No description
- `document_type` (string) ✅ - No description
- `verification_status` (string) ❌ - No description
- `verification_level` (integer) ❌ - No description
- `relevance_score` (unknown) ❌ - No description

---

### DocumentType

Type of generated document

---

### DocumentationData

Documentation generated when case closes.
Captures lessons learned and artifacts.

**Properties:**

- `documents_generated` (array) ❌ - All documents generated for this case
- `runbook_entry` (unknown) ❌ - Runbook entry created from this case
- `post_mortem_id` (unknown) ❌ - Link to post-mortem doc if created
- `lessons_learned` (array) ❌ - Key takeaways from investigation
- `what_went_well` (array) ❌ - Positive aspects of investigation
- `what_could_improve` (array) ❌ - Areas for improvement
- `preventive_measures` (array) ❌ - How to prevent recurrence
- `monitoring_recommendations` (array) ❌ - Monitoring/alerts to add
- `generated_at` (unknown) ❌ - When documentation was generated
- `generated_by` (string) ❌ - Who generated: 'agent' or user_id

---

### EscalationState

Investigation escalated to human expert.
Tracks escalation lifecycle.

**Properties:**

- `escalation_type` (unknown) ✅ - Why escalation was needed
- `reason` (string) ✅ - Detailed explanation of escalation reason
- `escalated_to` (unknown) ❌ - Team or person escalated to
- `escalated_at` (string) ❌ - When escalation occurred
- `context_summary` (string) ✅ - Summary of investigation so far for escalation recipient
- `key_findings` (array) ❌ - Key findings to communicate to expert
- `resolution` (unknown) ❌ - How escalation was resolved
- `resolved_at` (unknown) ❌ - When escalation was resolved

---

### EscalationType

Reason for escalation

---

### Evidence

Evidence collected during investigation.
Categorized by purpose to drive milestone advancement.

NOTE: Evidence.category is SYSTEM-INFERRED, not LLM-specified!
System categorizes based on:
- Which milestones are incomplete (if symptom not verified -> SYMPTOM_EVIDENCE)
- Hypothesis evaluation results (if creates hypothesis_evidence links -> CAUSAL_EVIDENCE)
- Solution state (if solution proposed -> RESOLUTION_EVIDENCE)

LLM provides: summary, analysis
LLM evaluates: stance per hypothesis (creates hypothesis_evidence links)
System infers: category, advances_milestones

**Properties:**

- `evidence_id` (string) ❌ - Unique evidence identifier
- `category` (unknown) ✅ - System-inferred category: SYMPTOM_EVIDENCE | CAUSAL_EVIDENCE | RESOLUTION_EVIDENCE | OTHER
- `primary_purpose` (string) ✅ - What this evidence validates (milestone name or hypothesis ID)
- `summary` (string) ✅ - Brief summary of evidence content (<500 chars) for UI display and quick scanning
- `preprocessed_content` (string) ✅ -
        Extracted relevant diagnostic information from preprocessing pipeline.

        This is what the agent uses for hypothesis evaluation and evidence analysis.
        Contains only the high-signal portions extracted from raw files.

        Examples:
        - Logs: Crime scene extraction (approx. 200 lines around errors)
        - Metrics: Anomaly detection results with statistical analysis
        - Config: Parsed configuration with secrets redacted
        - Code: AST-extracted functions and classes
        - Text: LLM-generated summary
        - Images: Vision model description

        Size: Typically 5 to 50 KB (compressed from larger raw files).
        Compression ratios: 200:1 for logs, 167:1 for metrics, 50:1 for code.

        This field is REQUIRED for all evidence. Raw files remain in S3 for audit/deep dive.

- `content_ref` (unknown) ❌ - S3 URI to original raw file (1-10MB) for audit, compliance, and deep dive analysis. May be None for user-typed evidence.
- `content_size_bytes` (integer) ✅ - Size of original raw file in bytes
- `preprocessing_method` (string) ✅ -
        Preprocessing method used to extract preprocessed_content from raw file.
        Examples: crime_scene_extraction, anomaly_detection, parse_and_sanitize,
        ast_extraction, vision_analysis, single_shot_summary, map_reduce_summary

- `compression_ratio` (unknown) ❌ - Ratio of preprocessed to raw content size (e.g., 0.005 = 200:1 compression)
- `analysis` (unknown) ❌ - Agent analysis of this evidence and its significance to the investigation
- `source_type` (unknown) ✅ - Type of evidence source
- `form` (unknown) ✅ - How evidence was provided: DOCUMENT (uploaded) or USER_INPUT (typed)
- `advances_milestones` (array) ❌ - Which milestones this evidence helped complete
- `collected_at` (string) ❌ - When evidence was collected
- `collected_by` (string) ✅ - Who collected: user_id or 'system' for automated collection
- `collected_at_turn` (integer) ✅ - Turn number when evidence was collected

---

### EvidenceArtifact

**Properties:**

- `evidence_id` (string) ✅ - No description
- `case_id` (string) ✅ - No description
- `user_id` (string) ✅ - No description
- `organization_id` (string) ✅ - No description
- `original_filename` (string) ✅ - No description
- `stored_filename` (string) ✅ - No description
- `file_path` (string) ✅ - No description
- `evidence_type` (unknown) ✅ - No description
- `mime_type` (string) ✅ - No description
- `file_size` (integer) ✅ - No description
- `storage_backend` (unknown) ❌ - No description
- `created_at` (string) ❌ - No description
- `updated_at` (string) ❌ - No description
- `metadata` (unknown) ❌ - No description
- `description` (unknown) ❌ - No description
- `is_primary` (boolean) ❌ - No description
- `tags` (array) ❌ - No description
- `linked_case_ids` (array) ❌ - No description

---

### EvidenceArtifactType

Types of evidence artifacts.

Categorizes the kind of evidence artifact stored.

---

### EvidenceCategory

Evidence classification by investigation purpose.

Post-redesign (2026-02-11):
- UNCLASSIFIED removed (single-phase evidence creation)
- OTHER renamed to CONTEXTUAL_EVIDENCE (clearer purpose)
- REJECTED added (track rejected submissions for deduplication)

Evidence is created AFTER LLM evaluation with complete classification.

---

### EvidenceDetailsResponse

Detailed evidence information with source and hypothesis linkage.

**Properties:**

- `evidence_id` (string) ✅ - No description
- `case_id` (string) ✅ - No description
- `summary` (string) ✅ - No description
- `category` (string) ✅ - No description
- `primary_purpose` (string) ✅ - No description
- `collected_at_turn` (integer) ✅ - No description
- `collected_at` (string) ✅ - No description
- `collected_by` (string) ✅ - No description
- `source_file` (unknown) ❌ - Source file this evidence was derived from (null if from user input)
- `related_hypotheses` (array) ❌ - No description
- `preprocessed_content` (string) ✅ - No description
- `content_size_bytes` (integer) ✅ - No description
- `analysis` (unknown) ❌ - No description

---

### EvidenceForm

How evidence was provided by user

---

### EvidenceLinkRequest

Request to link evidence to a case.

**Properties:**

- `case_id` (string) ✅ - No description

---

### EvidenceRequestToAdd

Evidence request the LLM wants to make to the user.

Example: "Please upload logs from the API gateway between 10:00-10:30 UTC"

**Properties:**

- `request_text` (string) ✅ - What evidence is requested
- `priority` (string) ❌ - How critical this evidence is
- `purpose` (string) ✅ - Why this evidence is needed

---

### EvidenceSourceType

Fundamental type of data source.

Post-redesign (2026-02-11): Simplified from 12 types to 5 clear categories.

Migration mapping:
- log_file, command_output, trace_data, api_response, other → LOGS
- metrics_data, monitoring_alert → METRICS
- config_file, code_review, database_query → CONFIGURATION
- screenshot → VISUAL
- user_report → USER_DESCRIPTION

---

### EvidenceStance

How evidence relates to a hypothesis.
Evaluated by LLM after evidence submission against ALL active hypotheses.
One evidence can have different stances for different hypotheses.

---

### EvidenceSummary

Summary of evidence for INVESTIGATING phase UI.

**Properties:**

- `evidence_id` (string) ✅ - Evidence identifier
- `type` (string) ✅ - Evidence type: log_file | metrics_data | config_file | etc.
- `summary` (string) ✅ - Brief summary of evidence content
- `timestamp` (string) ✅ - When evidence was collected
- `relevance_score` (number) ✅ - Relevance to current investigation (0.0-1.0)

---

### FileAnalysis

Detailed AI analysis of file.

**Properties:**

- `key_findings` (array) ❌ - No description
- `timeline_events` (array) ❌ - No description
- `relevance` (unknown) ❌ - No description

---

### GeneratedDocument

A generated document artifact.

**Properties:**

- `document_id` (string) ❌ - Unique document identifier
- `document_type` (unknown) ✅ - Type of document
- `title` (string) ✅ - Document title
- `content_ref` (string) ✅ - Reference to document content (S3 URI, file path, etc.)
- `generated_at` (string) ❌ - When document was generated
- `format` (string) ✅ - Document format: markdown | pdf | html | json | other
- `size_bytes` (unknown) ❌ - Document size in bytes

---

### HTTPValidationError

**Properties:**

- `detail` (array) ❌ - No description

---

### Hypothesis

Hypothesis for systematic root cause exploration.

Philosophy: Hypotheses are OPTIONAL. Agent may:
- Identify root cause directly from evidence (no hypotheses)
- OR generate hypotheses for systematic testing (when unclear)

**Properties:**

- `hypothesis_id` (string) ❌ - Unique hypothesis identifier
- `statement` (string) ✅ - Hypothesis statement (what we think caused the problem)
- `category` (unknown) ✅ - Hypothesis category (for anchoring detection)
- `status` (unknown) ❌ - Current hypothesis status
- `likelihood` (number) ❌ - Estimated likelihood this hypothesis is correct (0.0-1.0)
- `initial_likelihood` (number) ❌ - Original likelihood when hypothesis was generated
- `evidence_links` (object) ❌ -
        Maps evidence_id to relationship details.

        ONE evidence can:
        - STRONGLY_SUPPORTS hypothesis A
        - REFUTES hypothesis B
        - Be IRRELEVANT to hypothesis C

        Backed by hypothesis_evidence junction table in database.
        LLM evaluates each evidence against ALL active hypotheses after submission.

- `generated_at_turn` (integer) ✅ - Turn number when hypothesis was generated
- `last_updated_turn` (integer) ❌ - Turn number when hypothesis was last updated
- `last_progress_at_turn` (integer) ❌ - Turn number when hypothesis last showed progress
- `iterations_without_progress` (integer) ❌ - Count of consecutive iterations without progress
- `generation_mode` (unknown) ✅ - No description
- `retirement_reason` (unknown) ❌ - Reason if hypothesis was retired
- `rationale` (string) ✅ - Why this hypothesis was generated
- `tested_at` (unknown) ❌ - When hypothesis testing began
- `concluded_at` (unknown) ❌ - When hypothesis was validated/refuted/retired

---

### HypothesisCategory

Hypothesis categories for anchoring detection.

If agent tests 4+ hypotheses in same category without validation,
it is "anchored" and should try different category.

---

### HypothesisEvidenceLink

Many-to-many relationship between hypothesis and evidence.

ONE evidence can have DIFFERENT stances for DIFFERENT hypotheses:
- Evidence "Pool at 95%" -> STRONGLY_SUPPORTS "pool exhausted" hypothesis
- Evidence "Pool at 95%" -> REFUTES "network latency" hypothesis
- Evidence "Pool at 95%" -> IRRELEVANT to "memory leak" hypothesis

Stored in hypothesis_evidence junction table.
LLM evaluates evidence against ALL active hypotheses after submission.

**Properties:**

- `hypothesis_id` (string) ✅ - Hypothesis being evaluated
- `evidence_id` (string) ✅ - Evidence being evaluated
- `stance` (unknown) ✅ - How this evidence relates to THIS hypothesis (including IRRELEVANT)
- `reasoning` (string) ✅ - LLM's explanation of the relationship
- `stance_confidence` (number) ✅ - Confidence in the stance assessment (0.0-1.0). Use for granularity instead of STRONGLY_ variants.
- `analyzed_at` (string) ❌ - When this relationship was established

---

### HypothesisGenerationMode

How hypothesis was generated

---

### HypothesisRelationship

How a file relates to a hypothesis.

**Properties:**

- `hypothesis_id` (string) ✅ - No description
- `hypothesis_description` (string) ✅ - No description
- `stance` (string) ✅ - strongly_supports | supports | neutral | contradicts | strongly_contradicts | irrelevant
- `reasoning` (string) ✅ - No description

---

### HypothesisStatus

Hypothesis lifecycle status

---

### HypothesisSummary

Summary of a hypothesis for INVESTIGATING phase UI.

**Properties:**

- `hypothesis_id` (string) ✅ - Hypothesis identifier
- `text` (string) ✅ - Hypothesis statement
- `likelihood` (number) ✅ - Likelihood score (0.0-1.0)
- `status` (unknown) ✅ - Status: CAPTURED | ACTIVE | VALIDATED | REFUTED | INCONCLUSIVE | RETIRED
- `evidence_count` (integer) ✅ - Number of evidence items related to this hypothesis

---

### ImpactData

Impact assessment for problem scope.

**Properties:**

- `affected_services` (unknown) ❌ - List of affected services
- `affected_users` (unknown) ❌ - User impact description (e.g., 'All users in US region')
- `affected_regions` (unknown) ❌ - List of affected geographical regions

---

### InquiryData

Pre-investigation INQUIRY status data.
Captures early problem exploration before formal investigation commitment.

**Properties:**

- `problem_confirmation` (unknown) ❌ - Agent initial understanding of the problem
- `proposed_problem_statement` (unknown) ❌ -
        Agent formalized problem statement (clear, specific, actionable) - ITERATIVE REFINEMENT pattern.

        UI Display:
        - When None: Display "To be defined" or blank (no problem detected yet)
        - When set: Display the statement text

        Lifecycle:
        1. LLM creates initial formalization from conversation context
        2. LLM can UPDATE iteratively based on user corrections/refinements
        3. Becomes IMMUTABLE once problem_statement_confirmed = True
        4. Copied to case.description when investigation starts

        Pattern: Iterative Refinement - refine until user confirms without reservation

- `problem_statement_confirmed` (boolean) ❌ - User confirmed the formalized problem statement
- `problem_statement_confirmed_at` (unknown) ❌ - When user confirmed the problem statement
- `decided_to_investigate` (boolean) ❌ - Whether user committed to formal investigation
- `decision_made_at` (unknown) ❌ - When user decided to investigate (or not)
- `inquiry_turns` (integer) ❌ - Number of turns spent in INQUIRY status
- `knowledge_matches` (array) ❌ - Potential solutions found in KB
- `knowledge_resolution` (unknown) ❌ - Resolution details if fixed via KB match
- `preliminary_urgency` (unknown) ❌ - Early urgency assessment

---

### InquiryResponseData

Nested inquiry data for INQUIRY phase response.

**Properties:**

- `proposed_problem_statement` (unknown) ❌ - Agent's formalized problem statement (if ready)
- `problem_statement_confirmed` (boolean) ❌ - Whether user confirmed the problem statement
- `decided_to_investigate` (boolean) ❌ - Whether agent has enough info to start investigation
- `inquiry_turns` (integer) ❌ - Number of conversation turns during inquiry phase
- `problem_confirmation` (unknown) ❌ - Problem type and severity guess

---

### IntentType

Intent types for query routing.

Enables reliable intent detection without keyword matching.
Each type routes to specialized handling logic.

---

### InvestigationMomentum

Investigation momentum indicator for progress tracking.

Used to signal overall investigation health and guide agent behavior.
Calculated from recent progress patterns (evidence collection, hypothesis updates).

---

### InvestigationPath

Investigation routing strategy (4-stage workflow).

IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state x urgency_level).
LLM provides inputs (temporal_state, urgency_level) during verification.
System calls determine_investigation_path() to select path deterministically.

INVESTIGATING phase has 4 stages:
- Stage 1: Symptom verification (where and when)
- Stage 2: Hypotheses formulation (why)
- Stage 3: Hypothesis validation (why really)
- Stage 4: Solution (how)

Two paths based on urgency:
- MITIGATION_FIRST: 1-4-2-3-4 (quick mitigation, then RCA)
- ROOT_CAUSE: 1-2-3-4 (traditional RCA)

---

### InvestigationProgress

Milestone-based progress tracking.

Philosophy: Track what's completed, not what phase we're in.
Agent completes milestones opportunistically based on data availability.

**Properties:**

- `symptom_verified` (boolean) ❌ - Symptom confirmed with concrete evidence (logs, metrics, user reports)
- `root_cause_identified` (boolean) ❌ - Root cause determined (directly or via hypothesis validation)
- `root_cause_likelihood` (number) ❌ - Likelihood in root cause identification (0.0 = unknown, 1.0 = certain)
- `root_cause_method` (unknown) ❌ - How root cause was identified: direct_analysis | hypothesis_validation | single_shot_validation | correlation | user_provided | other
- `solution_proposed` (boolean) ❌ - Solution or mitigation has been proposed
- `solution_applied` (boolean) ❌ - Solution has been applied by user
- `solution_verified` (boolean) ❌ - Solution effectiveness verified (error rate decreased, metrics improved)
- `mitigation_applied` (boolean) ❌ -
        MITIGATION_FIRST path: Quick mitigation applied (stage 1 -> 4 complete).

        Used to track progress in MITIGATION_FIRST path (1-4-2-3-4):
        - Stage 1: Symptom verified
        - Stage 4: Quick mitigation applied (mitigation_applied = True)
        - Stage 2: Return to hypothesis formulation for RCA
        - Stage 3: Hypothesis validation
        - Stage 4: Permanent solution applied (solution_applied = True)

        When True: Agent should return to stage 2 (hypothesis formulation) for full RCA
        When False: Either ROOT_CAUSE path, or MITIGATION_FIRST has not applied mitigation yet

        Note: Different from solution_applied - mitigation is quick correlation-based fix,
        solution is comprehensive permanent fix after RCA.

- `mitigation_verified` (boolean) ❌ - Mitigation effectiveness confirmed (problem stopped)
- `mitigation_effectiveness` (unknown) ❌ - How well mitigation worked: 1.0 = fully resolved, 0.5 = partially, 0.0 = ineffective
- `mitigation_solution_id` (unknown) ❌ - Solution ID of applied mitigation (links to case.solutions)
- `verification_completed_at` (unknown) ❌ - When all verification milestones (symptom, scope, timeline, changes) were completed
- `investigation_completed_at` (unknown) ❌ - When root cause was identified
- `resolution_completed_at` (unknown) ❌ - When solution was verified

---

### InvestigationProgressSummary

Progress metrics for INVESTIGATING phase.

**Properties:**

- `milestones_completed` (integer) ✅ - Number of milestones completed
- `total_milestones` (integer) ✅ - Total milestones (always 8)
- `completed_milestone_ids` (array) ❌ - IDs of completed milestones
- `current_stage` (unknown) ✅ - Current stage: UNDERSTANDING | DIAGNOSING | RESOLVING

---

### InvestigationStage

Investigation stage within INVESTIGATING phase (4 stages).

Purpose: User-facing progress label computed from completed milestones.
NOT used for workflow control - milestones drive advancement opportunistically.
Only relevant when case status = INVESTIGATING.

Stage Progression (Path-Dependent):
- MITIGATION_FIRST: 1 -> 4 -> 2 -> 3 -> 4 (quick mitigation, then return for RCA)
- ROOT_CAUSE: 1 -> 2 -> 3 -> 4 (traditional RCA)

Stage determines the investigation focus based on what has been completed:
- Stage 1: Where and when (symptom verification)
- Stage 2: Why (hypothesis formulation)
- Stage 3: Why really (hypothesis validation)
- Stage 4: How (solution application)

---

### InvestigationStrategy

Investigation approach mode.
Affects decision thresholds, workflow behavior, and agent prompts.

---

### InvestigationStrategyData

Investigation strategy details for INVESTIGATING phase.

**Properties:**

- `approach` (unknown) ❌ - Investigation approach description (e.g., 'Speed priority - rapid mitigation')
- `next_steps` (unknown) ❌ - Recommended next steps in investigation

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
- `verification_level` (integer) ❌ - No description
- `verification_status` (unknown) ❌ - No description
- `verification_reason` (unknown) ❌ - No description
- `source_suggestion_id` (unknown) ❌ - No description

---

### KnowledgeMatch

Records a potential KB match during INQUIRY.

**Properties:**

- `match_id` (string) ✅ - No description
- `match_type` (string) ✅ - No description
- `relevance_score` (number) ✅ - No description
- `summary` (string) ✅ - No description
- `potential_solution` (unknown) ❌ - No description

---

### KnowledgeResolution

Records instant resolution via KB match during INQUIRY phase.

**Properties:**

- `match_id` (string) ✅ - No description
- `match_type` (string) ✅ - No description
- `solution_applied` (string) ✅ - No description
- `user_confirmation` (string) ✅ - No description
- `resolution_turn` (integer) ✅ - No description

---

### LinkCaseRequest

Request to link report to case closure.

**Properties:**

- `closure_note` (unknown) ❌ - No description

---

### LinkCaseResponse

Response after linking report to case closure.

**Properties:**

- `status` (string) ✅ - No description
- `message` (string) ✅ - No description
- `report_id` (string) ✅ - No description
- `case_id` (string) ✅ - No description
- `linked_at` (string) ✅ - No description

---

### LogoutResponse

Logout response model

**Properties:**

- `message` (string) ❌ - Logout confirmation message
- `revoked_tokens` (integer) ✅ - Number of tokens that were revoked

**Example:**

```json
{
  "message": "Logged out successfully",
  "revoked_tokens": 1
}
```

---

### MemberAddRequest

Request to add member to organization

**Properties:**

- `email` (string) ✅ - Email of user to invite
- `role` (unknown) ❌ - Role to assign (member, admin)

---

### MemberAddResponse

Response for adding a member

**Properties:**

- `user_id` (string) ✅ - No description
- `email` (string) ✅ - No description
- `full_name` (string) ✅ - No description
- `role` (string) ✅ - No description
- `joined_at` (string) ✅ - No description
- `invitation_sent` (boolean) ✅ - No description

---

### MemberListResponse

Response for listing organization members

**Properties:**

- `members` (array) ✅ - No description
- `total` (integer) ✅ - No description
- `limit` (integer) ✅ - No description
- `offset` (integer) ✅ - No description

---

### MemberResponse

Organization member response

**Properties:**

- `user_id` (string) ✅ - No description
- `email` (string) ✅ - No description
- `full_name` (string) ✅ - No description
- `role` (string) ✅ - No description
- `joined_at` (string) ✅ - No description

---

### MemberRoleUpdateRequest

Request to update member role

**Properties:**

- `role` (string) ✅ - New role to assign (member, admin)

---

### MemberRoleUpdateResponse

Response for updating member role

**Properties:**

- `user_id` (string) ✅ - No description
- `email` (string) ✅ - No description
- `full_name` (string) ✅ - No description
- `role` (string) ✅ - No description
- `joined_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description

---

### Message

Message model for conversation endpoints.

Schema matches docs/architecture/data-and-storage/schemas/case-schema.md Section 4.7 (case_messages table).

**Properties:**

- `message_id` (string) ✅ - No description
- `turn_number` (integer) ✅ - Turn number in conversation (user messages increment turn)
- `role` (string) ✅ - No description
- `content` (string) ✅ - No description
- `created_at` (string) ✅ - ISO 8601 datetime string (matches SQL schema)
- `author_id` (unknown) ❌ - User who created the message
- `token_count` (unknown) ❌ - Number of tokens in content
- `metadata` (unknown) ❌ - Sources, tools used, etc.

---

### MessageRetrievalDebugInfo

Debug information for message retrieval operations.

**Properties:**

- `redis_key` (string) ✅ - Redis key used for message storage
- `redis_operation_time_ms` (number) ✅ - Time taken for Redis operation
- `storage_errors` (array) ❌ - Any storage-related errors encountered
- `message_parsing_errors` (integer) ❌ - Number of messages that failed to parse

---

### OAuthConfigResponse

OAuth configuration for cloud mode.

**Properties:**

- `authorize_url` (string) ✅ - No description
- `token_url` (string) ✅ - No description
- `client_id` (string) ✅ - No description
- `scopes` (array) ✅ - No description

---

### OrganizationCreateRequest

Request to create a new organization

**Properties:**

- `name` (string) ✅ - Organization name
- `slug` (string) ✅ - URL-friendly identifier
- `description` (unknown) ❌ - Organization description
- `plan_tier` (unknown) ❌ - Subscription plan tier

---

### OrganizationListItem

Organization list item with user role

**Properties:**

- `organization_id` (string) ✅ - No description
- `name` (string) ✅ - No description
- `slug` (string) ✅ - No description
- `plan_tier` (string) ✅ - No description
- `role` (string) ✅ - No description
- `member_since` (string) ✅ - No description

---

### OrganizationListResponse

Response for listing user's organizations

**Properties:**

- `organizations` (array) ✅ - No description
- `total` (integer) ✅ - No description
- `limit` (integer) ✅ - No description
- `offset` (integer) ✅ - No description

---

### OrganizationResponse

Organization details response

**Properties:**

- `organization_id` (string) ✅ - No description
- `name` (string) ✅ - No description
- `slug` (string) ✅ - No description
- `description` (unknown) ✅ - No description
- `plan_tier` (string) ✅ - No description
- `max_members` (integer) ✅ - No description
- `current_member_count` (integer) ❌ - No description
- `owner_user_id` (unknown) ❌ - No description
- `settings` (object) ❌ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description

---

### OrganizationUpdateRequest

Request to update organization details

**Properties:**

- `name` (unknown) ❌ - Updated organization name
- `description` (unknown) ❌ - Updated description

---

### PathSelection

Path selection details.
Records how investigation path was chosen.

IMPORTANT: Path is SYSTEM-DETERMINED from matrix (temporal_state x urgency_level).
LLM provides inputs (temporal_state, urgency_level) during verification.
System calls determine_investigation_path() to select path deterministically.
LLM does NOT choose the path directly!

**Properties:**

- `path` (unknown) ✅ - Selected investigation path (system-determined from matrix)
- `auto_selected` (boolean) ✅ - True if system auto-selected, False if user chose
- `rationale` (string) ✅ - Why this path was selected
- `alternate_path` (unknown) ❌ - Alternative path user could have chosen (if auto-selected)
- `selected_at` (string) ❌ - When path was selected
- `selected_by` (string) ❌ - Who selected: 'system' for auto, or user_id for manual
- `temporal_state` (unknown) ❌ - Temporal state used in decision
- `urgency_level` (unknown) ❌ - Urgency level used in decision

---

### PermissionCheckRequest

Request to check user permission

**Properties:**

- `permission` (string) ✅ - Permission to check (e.g., 'cases.write')

---

### PermissionCheckResponse

Permission check result

**Properties:**

- `has_permission` (boolean) ✅ - No description
- `permission` (string) ✅ - No description
- `user_id` (string) ✅ - No description
- `organization_id` (string) ✅ - No description

---

### PlanStep

Represents one step in a multi-step plan.

**Properties:**

- `description` (string) ✅ - No description

---

### PreliminaryUrgency

Early urgency assessment using semantic business impact.

**Properties:**

- `level` (unknown) ✅ - No description
- `impact_assessment` (string) ✅ - No description
- `assessed_at_turn` (integer) ✅ - No description

---

### ProblemConfirmation

Agents initial problem understanding during inquiry.

**Properties:**

- `problem_type` (string) ✅ - Classified problem type: error | slowness | unavailability | data_issue | other
- `severity_guess` (string) ✅ - Initial severity assessment: critical | high | medium | low | unknown
- `preliminary_guidance` (string) ✅ - Initial guidance or suggestions
- `created_at` (string) ❌ - When this confirmation was created

---

### ProblemVerification

Consolidated problem verification data.

Contains all data gathered during verification phase:
- Symptom details
- Scope assessment
- Timeline
- Recent changes
- Correlations

**Properties:**

- `symptom_statement` (string) ✅ - Clear statement of the problem symptom
- `symptom_indicators` (array) ❌ - Specific metrics/observations confirming symptom (e.g., 'Error rate: 15%', 'P99 latency: 5s')
- `affected_services` (array) ❌ - Services/components affected
- `affected_users` (unknown) ❌ - User impact description: 'all users' | '10% of users' | 'premium tier' | etc.
- `affected_regions` (array) ❌ - Geographic regions affected
- `severity` (string) ✅ - Assessed severity: CRITICAL | HIGH | MEDIUM | LOW
- `user_impact` (unknown) ❌ - Description of user-facing impact
- `started_at` (unknown) ❌ - When problem began (best estimate)
- `noticed_at` (unknown) ❌ - When problem was noticed/reported
- `resolved_naturally_at` (unknown) ❌ - If problem resolved on its own, when?
- `duration` (unknown) ❌ - How long problem lasted (for historical problems)
- `temporal_state` (unknown) ❌ - ONGOING | HISTORICAL
- `recent_changes` (array) ❌ - Recent changes that may be relevant (deployments, configs, etc.)
- `correlations` (array) ❌ - Identified correlations between changes and symptom
- `correlation_confidence` (number) ❌ - Confidence in change-symptom correlation (0.0 = no correlation, 1.0 = certain)
- `urgency_level` (unknown) ❌ - Urgency classification for path routing
- `urgency_factors` (array) ❌ - Factors contributing to urgency assessment
- `verified_at` (unknown) ❌ - When verification was completed
- `verification_confidence` (number) ❌ - Overall confidence in verification accuracy

---

### ProblemVerificationData

Problem verification details for INVESTIGATING phase.

**Properties:**

- `urgency_level` (unknown) ❌ - Urgency: critical | high | medium | low | unknown
- `severity` (unknown) ❌ - Severity: critical | high | medium | low
- `temporal_state` (unknown) ❌ - When the problem occurred and its temporal pattern
- `impact` (unknown) ❌ - Scope of impact (services, users, regions)
- `user_impact` (unknown) ❌ - Human-readable user impact summary

---

### ProcessingStatus

Defines the status of data processing operations.

---

### QueryIntent

Structured intent metadata for programmatic query handling.

Enables reliable intent detection without keyword matching.
All queries must specify their intent type for proper routing.

Design Reference: Intent-based routing eliminates ambiguity in pattern matching
and provides single code path for all interactions (conversation history unified).

**Properties:**

- `type` (unknown) ✅ - Intent type for routing - determines which handler processes this query
- `from_status` (unknown) ❌ - For status_transition: source status (validation)
- `to_status` (unknown) ❌ - For status_transition: target status (REQUIRED for status_transition)
- `user_confirmed` (unknown) ❌ - User explicitly confirmed action via UI button/dialog
- `hypothesis_id` (unknown) ❌ - For hypothesis_action: target hypothesis ID
- `action` (unknown) ❌ - Action to perform: validate | refute | retire
- `evidence_id` (unknown) ❌ - For evidence_request: target evidence ID
- `confirmation_value` (unknown) ❌ - For confirmation: yes/no value

---

### RelatedHypothesis

Hypothesis linked to this evidence.

**Properties:**

- `hypothesis_id` (string) ✅ - No description
- `statement` (string) ✅ - No description
- `stance` (string) ✅ - SUPPORTS | REFUTES | NEUTRAL

---

### ReportAvailability

Report generation availability status for RESOLVED phase.

**Properties:**

- `report_type` (string) ✅ - Type: incident_report | post_mortem | runbook | timeline
- `status` (string) ✅ - Status: available | recommended | in_progress | not_applicable
- `reason` (unknown) ❌ - Reason for status (e.g., why recommended)

---

### ReportGenerationRequest

Request to generate case documentation reports

**Properties:**

- `report_types` (array) ✅ - Types of reports to generate

---

### ReportGenerationResponse

Response after generating reports

**Properties:**

- `case_id` (string) ✅ - Case identifier
- `reports` (array) ✅ - Generated reports
- `remaining_regenerations` (integer) ✅ - Number of regenerations remaining (max 5 per report type)

---

### ReportListResponse

API response for list of reports.

**Properties:**

- `reports` (array) ✅ - No description
- `total` (integer) ✅ - No description
- `case_id` (string) ✅ - No description

---

### ReportRecommendationResponse

API response for report recommendations.

**Properties:**

- `case_id` (string) ✅ - No description
- `available_for_generation` (array) ✅ - No description
- `runbook_recommendation` (object) ✅ - No description

---

### ReportResponse

API response model for a report.

**Properties:**

- `report_id` (string) ✅ - No description
- `case_id` (string) ✅ - No description
- `report_type` (string) ✅ - No description
- `title` (string) ✅ - No description
- `content` (string) ✅ - No description
- `format` (string) ✅ - No description
- `generation_status` (string) ✅ - No description
- `generated_at` (string) ✅ - No description
- `generation_time_ms` (integer) ✅ - No description
- `is_current` (boolean) ✅ - No description
- `version` (integer) ✅ - No description
- `linked_to_closure` (boolean) ✅ - No description
- `metadata` (unknown) ❌ - No description

---

### ReportStatus

Report generation status

---

### ReportType

Type of case documentation report

---

### ReportUpdateRequest

Request model for updating a report.

**Properties:**

- `title` (unknown) ❌ - No description
- `content` (unknown) ❌ - Updated report content in markdown

---

### ReportVersionListResponse

List of report versions.

**Properties:**

- `versions` (array) ✅ - No description
- `total` (integer) ✅ - No description

---

### ReportVersionResponse

Version history entry for a report.

**Properties:**

- `report_id` (string) ✅ - No description
- `version` (integer) ✅ - No description
- `title` (string) ✅ - No description
- `generated_at` (string) ✅ - No description
- `is_current` (boolean) ✅ - No description
- `linked_to_closure` (boolean) ✅ - No description

---

### ResolutionSummary

Overall resolution metrics for RESOLVED phase.

**Properties:**

- `total_duration_minutes` (integer) ✅ - Total time from case creation to resolution
- `milestones_completed` (integer) ✅ - Total milestones completed (should be 8)
- `hypotheses_tested` (integer) ✅ - Number of hypotheses tested
- `evidence_collected` (integer) ✅ - Total evidence items collected
- `key_insights` (array) ❌ - Key learnings from this investigation

---

### ResponseType

Defines the agent's primary intent for this turn - v3.0 Response-Format-Driven Design

9 response formats designed to serve 16 QueryIntent categories (1.8:1 ratio).
Each format has strict structural requirements for frontend parsing.

---

### RootCauseConclusion

Final determination of root cause.
More authoritative than WorkingConclusion.

**Properties:**

- `root_cause` (string) ✅ - Definitive statement of root cause
- `confidence_level` (unknown) ✅ - Categorical confidence level
- `likelihood` (number) ✅ - Numeric likelihood score (0.0-1.0)
- `mechanism` (string) ✅ - How this root cause led to the symptom
- `evidence_basis` (array) ❌ - Evidence IDs supporting this conclusion
- `validated_hypothesis_id` (unknown) ❌ - If identified via hypothesis validation, the hypothesis ID
- `contributing_factors` (array) ❌ - Secondary factors that made the problem worse or more likely
- `determined_at` (string) ❌ - When root cause was determined
- `determined_by` (string) ❌ - Who determined: 'agent' or user_id

---

### RootCauseSummary

Root cause information for RESOLVED phase.

**Properties:**

- `description` (string) ✅ - What caused the problem
- `root_cause_id` (string) ✅ - Root cause identifier
- `category` (string) ✅ - Category: code | config | environment | network | data | hardware | external | human | other
- `severity` (string) ✅ - Severity: critical | high | medium | low

---

### RunbookMetadata

Metadata for runbook reports supporting dual sources.
Tracks origin (incident vs document) for transparency.

**Properties:**

- `source` (unknown) ✅ - Origin of runbook
- `case_context` (unknown) ❌ - Case investigation context (incident-driven only)
- `document_title` (unknown) ❌ - Source document title (document-driven only)
- `original_document_id` (unknown) ❌ - Reference to uploaded document (document-driven only)
- `domain` (string) ✅ - Technology domain for filtering
- `tags` (array) ❌ - Classification tags
- `llm_model` (unknown) ❌ - LLM model used for generation
- `embedding_model` (unknown) ❌ - Embedding model for vector search

---

### RunbookSource

Origin of runbook content

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

Request model for creating investigation session.

**Properties:**

- `session_goal` (unknown) ❌ - No description
- `token_budget_limit` (unknown) ❌ - No description
- `metadata` (unknown) ❌ - No description

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

Investigation session status.

Tracks the lifecycle of an investigation session from active to completion.

---

### SessionUpdateRequest

Request model for updating session.

**Properties:**

- `session_goal` (unknown) ❌ - No description
- `token_budget_limit` (unknown) ❌ - No description
- `metadata` (unknown) ❌ - No description

---

### SettingsResponse

Organization settings response

**Properties:**

- `organization_id` (string) ✅ - No description
- `plan_tier` (string) ✅ - No description
- `max_members` (integer) ✅ - No description
- `current_member_count` (integer) ❌ - No description
- `max_cases_per_month` (unknown) ❌ - No description
- `max_storage_gb` (integer) ✅ - No description
- `features` (object) ✅ - No description
- `settings` (object) ✅ - No description

---

### SettingsUpdateRequest

Request to update organization settings

**Properties:**

- `allow_public_cases` (unknown) ❌ - No description
- `require_2fa` (unknown) ❌ - No description
- `session_timeout_minutes` (unknown) ❌ - No description
- `default_case_priority` (unknown) ❌ - No description

---

### SettingsUpdateResponse

Response for updating organization settings

**Properties:**

- `organization_id` (string) ✅ - No description
- `settings` (object) ✅ - No description
- `updated_at` (string) ✅ - No description

---

### Solution

Proposed or applied solution/mitigation.

**Properties:**

- `solution_id` (string) ❌ - Unique solution identifier
- `solution_type` (unknown) ✅ - Type of solution
- `title` (string) ✅ - Short solution title
- `immediate_action` (unknown) ❌ - Quick fix or mitigation (temporary)
- `longterm_fix` (unknown) ❌ - Permanent solution (comprehensive)
- `implementation_steps` (array) ❌ - Step-by-step implementation instructions
- `commands` (array) ❌ - Specific commands to execute
- `risks` (array) ❌ - Risks or side effects of this solution
- `proposed_at` (string) ❌ - When solution was proposed
- `proposed_by` (string) ❌ - Who proposed: 'agent' or user_id
- `applied_at` (unknown) ❌ - When solution was applied
- `applied_by` (unknown) ❌ - Who applied the solution
- `verified_at` (unknown) ❌ - When solution effectiveness was verified
- `verification_method` (unknown) ❌ - How effectiveness was verified
- `verification_evidence_id` (unknown) ❌ - Evidence ID proving solution worked
- `effectiveness` (unknown) ❌ - How well solution worked (0.0 = failed, 1.0 = perfect)

---

### SolutionSummary

Solution information for RESOLVED phase.

**Properties:**

- `description` (string) ✅ - What was done to fix the problem
- `applied_at` (string) ✅ - When solution was applied
- `applied_by` (string) ✅ - Who applied the solution (user_id or 'agent')

---

### SolutionType

Type of solution/mitigation

---

### Source

Represents a single piece of citable evidence to build user trust.

**Properties:**

- `type` (unknown) ✅ - No description
- `content` (string) ✅ - No description
- `confidence` (unknown) ❌ - No description
- `metadata` (unknown) ❌ - No description
- `verification_status` (unknown) ❌ - No description
- `verification_reason` (unknown) ❌ - No description

---

### SourceFileReference

Reference to source file that evidence was derived from.

**Properties:**

- `file_id` (string) ✅ - No description
- `filename` (string) ✅ - No description
- `uploaded_at_turn` (integer) ✅ - No description

---

### SourceType

Defines the origin of a piece of evidence.

---

### StorageBackend

Storage backend types.

Defines where evidence files are stored.

---

### TeamCreateRequest

Request to create a new team

**Properties:**

- `organization_id` (string) ✅ - Organization ID
- `name` (string) ✅ - Team name
- `description` (unknown) ❌ - Team description

---

### TeamMemberAddRequest

Request to add member to team

**Properties:**

- `user_id` (string) ✅ - User ID to add
- `team_role` (unknown) ❌ - Team role ('lead' or 'member')

---

### TeamMemberResponse

Team member response

**Properties:**

- `user_id` (string) ✅ - No description
- `team_id` (string) ✅ - No description
- `team_role` (unknown) ✅ - No description
- `joined_at` (string) ✅ - No description

---

### TeamResponse

Team details response

**Properties:**

- `team_id` (string) ✅ - No description
- `organization_id` (string) ✅ - No description
- `name` (string) ✅ - No description
- `description` (unknown) ✅ - No description
- `settings` (object) ✅ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description

---

### TeamUpdateRequest

Request to update team details

**Properties:**

- `name` (unknown) ❌ - Updated team name
- `description` (unknown) ❌ - Updated description
- `settings` (unknown) ❌ - Team settings

---

### TemporalState

Problem temporal classification.
Used for investigation path routing.

---

### TemporalStateData

Temporal information about problem occurrence.

**Properties:**

- `started_at` (unknown) ❌ - When the problem started
- `last_occurrence_at` (unknown) ❌ - Most recent occurrence of the problem
- `state` (unknown) ❌ - Temporal state: ongoing | historical | intermittent

---

### TimelineEvent

Timeline event extracted from file.

**Properties:**

- `timestamp` (string) ✅ - No description
- `event` (string) ✅ - No description

---

### TitleResponse

Simplified title response schema per API spec.

**Properties:**

- `schema_version` (string) ❌ - No description
- `title` (string) ✅ - No description

---

### ToolCallResponse

Response model for a tool call within an execution.

**Properties:**

- `tool_call_id` (string) ✅ - No description
- `tool_name` (string) ✅ - No description
- `arguments` (object) ✅ - No description
- `result` (unknown) ❌ - No description
- `status` (string) ✅ - No description

---

### TurnOutcome

Turn outcome classification.

NOTE: Outcomes are LLM-observable only (what happened this turn).
Workflow control uses direct metrics (turns_without_progress, degraded_mode).
Outcomes are for analytics and prompt context, not control flow.

---

### TurnProgress

Record of what happened in one turn.
Turn = one user message + one agent response.

**Properties:**

- `turn_number` (integer) ✅ - Sequential turn number
- `timestamp` (string) ❌ - When turn occurred
- `milestones_completed` (array) ❌ - Milestone names completed this turn (e.g., 'symptom_verified')
- `evidence_added` (array) ❌ - Evidence IDs added this turn
- `hypotheses_generated` (array) ❌ - Hypothesis IDs generated this turn
- `hypotheses_validated` (array) ❌ - Hypothesis IDs validated this turn
- `solutions_proposed` (array) ❌ - Solution IDs proposed this turn
- `progress_made` (boolean) ✅ - Did investigation advance this turn?
- `actions_taken` (array) ❌ - Agent actions: 'verified_symptom', 'requested_logs', 'generated_hypothesis', etc.
- `outcome` (unknown) ✅ - Turn outcome classification
- `user_message_summary` (unknown) ❌ - Summary of user message
- `agent_response_summary` (unknown) ❌ - Summary of agent response
- `system_feedback` (unknown) ❌ - Instruction or error from system to agent (e.g., 'Invalid evidence ID')
- `momentum` (unknown) ❌ - Investigation momentum indicator for this turn
- `blocked_reasons` (array) ❌ - Reasons why investigation is blocked or progressing slowly
- `next_steps` (array) ❌ - Suggested next steps for the investigation
- `stagnation_detected` (unknown) ❌ - Stagnation type detected this turn: no_progress, hypothesis_anchoring, action_loop, hypothesis_deadlock
- `validation_repairs` (array) ❌ - State repairs made by StateValidator this turn (e.g., 'Fixed milestone ordering')

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
- `likelihood` (unknown) ❌ - No description

---

### UploadedFile

Raw file metadata for files uploaded to a case.

Key Distinction:
- UploadedFile: Raw file metadata, exists in ANY case phase (INQUIRY or INVESTIGATING)
- Evidence: Investigation-linked data derived from files, ONLY exists in INVESTIGATING phase

Files uploaded during INQUIRY are tracked here but do NOT become evidence until
the case transitions to INVESTIGATING and hypotheses are formulated.

**Properties:**

- `file_id` (string) ❌ - Unique file identifier (same as data_id in data service)
- `filename` (string) ✅ - Original filename
- `size_bytes` (integer) ✅ - File size in bytes
- `data_type` (string) ✅ - Detected data type from preprocessing (log, metric, config, code, text, image, etc.)
- `uploaded_at_turn` (integer) ✅ - Turn number when file was uploaded
- `uploaded_at` (string) ❌ - Upload timestamp
- `source_type` (string) ❌ - file_upload | paste | screenshot | page_injection | agent_generated
- `preprocessing_summary` (unknown) ❌ - Brief summary from preprocessing pipeline (<500 chars)
- `content_ref` (unknown) ❌ - Reference to stored file content (S3 URI or data_id). May be None if processing pending.

---

### UploadedFileDetails

Detailed file information including analysis.

**Properties:**

- `file_id` (string) ✅ - Evidence/File identifier
- `filename` (string) ✅ - Original or generated filename
- `size_bytes` (integer) ✅ - File size in bytes
- `size_display` (string) ✅ - Human-readable size (e.g., '2.3 MB')
- `uploaded_at_turn` (integer) ✅ - Turn when file was uploaded
- `uploaded_at` (string) ✅ - Upload timestamp
- `source_type` (string) ✅ - file_upload | paste | screenshot | page_injection | agent_generated
- `analysis_status` (string) ✅ - pending | processing | completed | failed
- `summary` (unknown) ❌ - AI-generated summary (1-2 sentences)
- `source_metadata` (unknown) ❌ - Additional metadata for page injections
- `full_analysis` (unknown) ❌ - Detailed AI analysis
- `hypothesis_relationships` (unknown) ❌ - How this file relates to hypotheses (investigating phase only)

---

### UploadedFileDetailsResponse

Detailed information about an uploaded file with evidence linkage.

**Properties:**

- `file_id` (string) ✅ - No description
- `filename` (string) ✅ - No description
- `size_bytes` (integer) ✅ - No description
- `size_display` (string) ✅ - No description
- `uploaded_at_turn` (integer) ✅ - No description
- `uploaded_at` (string) ✅ - No description
- `source_type` (string) ✅ - No description
- `data_type` (string) ✅ - No description
- `summary` (unknown) ❌ - No description
- `derived_evidence` (array) ❌ - No description
- `evidence_count` (integer) ✅ - No description

---

### UploadedFileMetadata

Metadata for uploaded files (evidence) - List view.

**Properties:**

- `file_id` (string) ✅ - Evidence/File identifier
- `filename` (string) ✅ - Original or generated filename
- `size_bytes` (integer) ✅ - File size in bytes
- `size_display` (string) ✅ - Human-readable size (e.g., '2.3 MB')
- `uploaded_at_turn` (integer) ✅ - Turn when file was uploaded
- `uploaded_at` (string) ✅ - Upload timestamp
- `source_type` (string) ✅ - file_upload | paste | screenshot | page_injection | agent_generated
- `analysis_status` (string) ✅ - pending | processing | completed | failed
- `summary` (unknown) ❌ - AI-generated summary (1-2 sentences)
- `source_metadata` (unknown) ❌ - Additional metadata for page injections

---

### UploadedFilesList

Paginated list of uploaded files.

**Properties:**

- `files` (array) ✅ - No description
- `total_count` (integer) ✅ - Total number of files
- `limit` (integer) ✅ - No description
- `offset` (integer) ✅ - No description

---

### UploadedFilesListResponse

List of uploaded files with evidence counts.

**Properties:**

- `case_id` (string) ✅ - No description
- `total_count` (integer) ✅ - No description
- `files` (array) ✅ - No description

---

### UrgencyLevel

Urgency classification for path routing.

Used with TemporalState to determine investigation path:
- ONGOING + HIGH/CRITICAL -> MITIGATION
- HISTORICAL + LOW/MEDIUM -> ROOT_CAUSE
- Other combinations -> USER_CHOICE

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

### UserInfoResponse

Extended user information response

Includes additional metadata for the current user.

**Properties:**

- `user_id` (string) ✅ - Unique user identifier
- `username` (string) ✅ - Username
- `email` (string) ✅ - Email address
- `display_name` (string) ✅ - Display name
- `created_at` (string) ✅ - Account creation timestamp (ISO format)
- `is_dev_user` (boolean) ❌ - Development user flag
- `roles` (array) ❌ - User roles for access control (e.g., ['user'], ['user', 'admin'])
- `last_login` (unknown) ❌ - Last login timestamp (ISO format)
- `token_count` (integer) ❌ - Number of active tokens for this user

**Example:**

```json
{
  "created_at": "2025-01-15T10:00:00Z",
  "display_name": "John Doe",
  "email": "john.doe@faultmaven.local",
  "is_dev_user": true,
  "last_login": "2025-01-15T14:30:00Z",
  "roles": [
    "user",
    "admin"
  ],
  "token_count": 2,
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "john.doe"
}
```

---

### UserProfile

Public user profile information

Represents user information safe for API responses.
Excludes sensitive information like hashed passwords.

**Properties:**

- `user_id` (string) ✅ - Unique user identifier
- `username` (string) ✅ - Username
- `email` (string) ✅ - Email address
- `display_name` (string) ✅ - Display name
- `created_at` (string) ✅ - Account creation timestamp (ISO format)
- `is_dev_user` (boolean) ❌ - Development user flag
- `roles` (array) ❌ - User roles for access control (e.g., ['user'], ['user', 'admin'])

**Example:**

```json
{
  "created_at": "2025-01-15T10:00:00Z",
  "display_name": "John Doe",
  "email": "john.doe@faultmaven.local",
  "is_dev_user": true,
  "roles": [
    "user",
    "admin"
  ],
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "john.doe"
}
```

---

### ValidationError

**Properties:**

- `loc` (array) ✅ - No description
- `msg` (string) ✅ - No description
- `type` (string) ✅ - No description

---

### VerificationStatus

Solution verification status for RESOLVED phase.

**Properties:**

- `verified` (boolean) ✅ - Whether solution effectiveness was verified
- `verification_method` (string) ✅ - How verification was done
- `details` (string) ✅ - Verification details and metrics

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
- `investigation_progress` (unknown) ❌ - Investigation progress (milestones, evidence, hypotheses)

---

### WorkingConclusion

Agent current best understanding of the problem.
Updated iteratively as investigation progresses.

Less authoritative than RootCauseConclusion.

**Properties:**

- `statement` (string) ✅ - Current conclusion statement
- `likelihood` (number) ✅ - Likelihood of this conclusion (0.0-1.0)
- `reasoning` (string) ✅ - Why agent believes this conclusion
- `supporting_evidence_ids` (array) ❌ - Evidence IDs supporting this conclusion
- `caveats` (array) ❌ - Limitations or uncertainties
- `updated_at` (string) ❌ - When this conclusion was formed/updated
- `supersedes_conclusion_at` (unknown) ❌ - Timestamp of previous conclusion this replaces

---

### WorkingConclusionSummary

Agent's current understanding during INVESTIGATING phase.

**Properties:**

- `summary` (string) ✅ - Current best theory about the problem
- `confidence` (number) ✅ - Confidence level (0.0-1.0)
- `last_updated` (string) ✅ - When this conclusion was last updated

---

### faultmaven__api__models__SessionResponse

Response model for investigation session.

**Properties:**

- `session_id` (string) ✅ - No description
- `case_id` (string) ✅ - No description
- `user_id` (string) ✅ - No description
- `organization_id` (string) ✅ - No description
- `status` (unknown) ✅ - No description
- `started_at` (string) ✅ - No description
- `ended_at` (unknown) ❌ - No description
- `last_activity_at` (string) ✅ - No description
- `total_duration_ms` (unknown) ❌ - No description
- `session_goal` (unknown) ❌ - No description
- `findings_summary` (unknown) ❌ - No description
- `total_token_usage` (integer) ✅ - No description
- `total_agent_executions` (integer) ✅ - No description
- `token_budget_limit` (unknown) ❌ - No description
- `created_at` (string) ✅ - No description
- `updated_at` (string) ✅ - No description

---

### faultmaven__modules__case__domain__models__Case

Root case entity.
Represents one complete troubleshooting investigation.

**Properties:**

- `case_id` (string) ❌ - Unique case identifier
- `user_id` (string) ✅ - User who created the case
- `organization_id` (string) ✅ - Organization this case belongs to
- `title` (string) ✅ - Short case title for list views and headers (e.g., 'API Performance Issue')
- `description` (string) ❌ -
        Confirmed problem description - canonical, user-facing, displayed prominently in UI.

        Lifecycle:
        1. Empty initially during INQUIRY (while agent formalizes problem)
        2. Set when user confirms proposed_problem_statement and decides to investigate
        3. Immutable after status becomes INVESTIGATING (provides stable reference)
        4. Used for UI display, search, and documentation

        Example: "API experiencing slowness with 30% of requests taking >5s response time
                  across all US regions, started 2 hours ago coinciding with v2.1.3 deployment"

- `status` (unknown) ❌ - Current lifecycle status
- `status_history` (array) ❌ - Complete history of status changes
- `closure_reason` (unknown) ❌ - Why case was closed: resolved | abandoned | escalated | inquiry_only | duplicate | other
- `progress` (unknown) ❌ - Milestone-based progress tracking
- `current_turn` (integer) ❌ - Current turn number (increments with each user-agent exchange)
- `turns_without_progress` (integer) ❌ - Consecutive turns with no milestone advancement (for stuck detection)
- `turn_history` (array) ❌ - Complete history of all turns
- `messages` (array) ❌ -
        Complete conversation history (user queries + agent responses).

        Per docs/architecture/data-and-storage/schemas/case-schema.md Section 4.7, each message contains:
        - message_id: str - Unique identifier
        - case_id: str - Case this message belongs to
        - turn_number: int - Which turn this message belongs to
        - role: str - "user" | "assistant" | "system"
        - content: str - The actual message text
        - created_at: datetime - When message was created (ISO format)
        - token_count: Optional[int] - Number of tokens in content
        - metadata: dict - Additional data (sources, tools used, etc.)

        NOTE: Does NOT contain session_id (per case-and-session-concepts.md)
        Sessions provide authentication only, not message ownership.

        Relationship to turn_history:
        - messages[i].turn_number references turn_history[j].turn_number
        - Provides the "what was said" to complement turn_history's "what happened"

- `message_count` (integer) ❌ - Total number of messages (user + agent combined)
- `path_selection` (unknown) ❌ - Selected investigation path (MITIGATION vs ROOT_CAUSE)
- `investigation_strategy` (unknown) ❌ - Investigation approach: ACTIVE_INCIDENT (speed) vs POST_MORTEM (thoroughness)
- `inquiry` (unknown) ❌ - Pre-investigation INQUIRY status data
- `problem_verification` (unknown) ❌ - Consolidated verification data (symptom, scope, timeline, changes)
- `uploaded_files` (array) ❌ -
        All files uploaded to this case (raw file metadata).

        Files can be uploaded at ANY phase (INQUIRY or INVESTIGATING).
        Evidence is DERIVED from uploaded files after analysis during INVESTIGATING phase.

        Difference from evidence:
        - uploaded_files: Raw file metadata (file_id, filename, size, upload time)
        - evidence: Investigation data linked to hypotheses (only in INVESTIGATING phase)

- `evidence` (array) ❌ - All evidence collected during investigation
- `hypotheses` (object) ❌ - Generated hypotheses (key = hypothesis_id)
- `solutions` (array) ❌ - Proposed and applied solutions
- `working_conclusion` (unknown) ❌ - Agent current best understanding (updated iteratively)
- `root_cause_conclusion` (unknown) ❌ - Final root cause determination
- `degraded_mode` (unknown) ❌ - Investigation is stuck or blocked
- `escalation_state` (unknown) ❌ - Escalated to human expert
- `documentation` (unknown) ❌ - Generated documentation and lessons learned
- `created_at` (string) ❌ - When case was created
- `updated_at` (string) ❌ - Last modification timestamp
- `last_activity_at` (string) ❌ - Most recent user/agent interaction (for 'updated Xm ago' display)
- `resolved_at` (unknown) ❌ - When case reached RESOLVED status
- `closed_at` (unknown) ❌ - When case reached terminal state (RESOLVED or CLOSED)

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
