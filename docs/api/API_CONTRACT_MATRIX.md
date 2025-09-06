# API Contract Matrix - FaultMaven

**Status**: 🔒 FROZEN - Single Source of Truth  
**Version**: 3.1.0  
**Last Updated**: 2025-08-28  

## Core Operations Summary

| Operation | Method | Path | Status Codes | Response Body | Required Headers | Auth Behavior |
|-----------|--------|------|--------------|---------------|------------------|---------------|
| **List Cases** | GET | `/api/v1/cases` | 200 | `CaseSummary[]` (array) | `X-Total-Count`, `Link` | 401 if service unavailable |
| **Create Case** | POST | `/api/v1/cases` | 201 | `CaseResponse` (object) | `Location` | 401 if service unavailable |
| **Get Case** | GET | `/api/v1/cases/{id}` | 200, 404 | `Case` (object) | - | 401 if service unavailable |
| **Submit Query (Sync)** | POST | `/api/v1/cases/{id}/queries` | 201 | `AgentResponse` (object) | `Location` | 401 if service unavailable |
| **Submit Query (Async)** | POST | `/api/v1/cases/{id}/queries` | 202 | `QueryJobStatus` (object) | `Location`, `Retry-After` | 401 if service unavailable |
| **Get Query Status** | GET | `/api/v1/cases/{id}/queries/{qid}` | 200, 303 | `QueryJobStatus` (processing) or 303 See Other | `Retry-After` (on 200 processing), `Location` (on 303) | 401 if service unavailable |
| **Archive Case (UI Delete)** | POST | `/api/v1/cases/{id}/archive` | 200 | `{ case_id, success, message, reason? }` | - | UI “Delete” maps here |
| **Delete Case (Admin Only)** | DELETE | `/api/v1/cases/{id}` | 204 | - | - | Not exposed in normal UI |
| **List Case Queries** | GET | `/api/v1/cases/{id}/queries` | 200 | `CaseQuerySummary[]` (array) | `X-Total-Count`, `Link` | 401 if service unavailable |
| **List Session Cases** | GET | `/api/v1/sessions/{sid}/cases` | 200 | `CaseSummary[]` (array) | `X-Total-Count`, `Link` | 401 if service unavailable |

## Critical Flow Validation Points

### 1. Dev-Login Flow
- **Pre-auth probe**: Protected endpoints → `401` + `ErrorResponse` (NEVER `500`)
- **Authentication mechanism**: Optional `X-User-Id` header or `user_id` query param
- **Anonymous access**: Supported with limited functionality

### 2. POST /cases Flow
- **Success**: `201 Created` + `Location: /api/v1/cases/{case_id}`
- **Body**: `CaseResponse` object with `case.case_id` field
- **Session linkage**: Atomic - immediately visible in session cases list
- **Validation errors**: `400` with `ErrorResponse`

### 3. GET /cases Flow  
- **Success**: `200 OK` with `CaseSummary[]` array (NEVER envelope object)
- **Empty results**: `200 OK` with `[]` (NEVER `404` or `500`)
- **Pagination**: `X-Total-Count` header + RFC 5988 `Link` header
- **Filtering**: Query params for status, priority, owner

### 4. GET /sessions/{sid}/cases Flow
- **Success**: `200 OK` with `CaseSummary[]` array
- **Just-created cases**: Immediately visible (atomic session↔case linking)
- **Empty results**: `200 OK` with `[]`
- **Pagination**: `X-Total-Count` and `Link` headers required

### 5. POST /cases/{cid}/queries Flow
- **Sync Response (201)**:
  - Status: `201 Created`
  - Body: `AgentResponse` with `content` and `response_type` fields
  - Headers: `Location: /api/v1/cases/{cid}/queries/{query_id}`
  
- **Async Response (202)**:
  - Status: `202 Accepted` 
  - Body: `QueryJobStatus` with job details
  - Headers: `Location: /api/v1/cases/{cid}/queries/{job_id}`, `Retry-After: 5`

### 6. GET /cases/{cid}/queries/{qid} Flow
- **Processing**: `200 OK` with `QueryJobStatus` + `Retry-After` header
- **Completed**: `200 OK` with `AgentResponse` 
- **Alternative**: `303 See Other` with `Location` header to final result

### 7. Pre-Auth Probe Flow
- **Any protected endpoint** without auth → `401 Unauthorized` + `ErrorResponse`
- **Unknown resource IDs** → `404 Not Found` + `ErrorResponse`
- **NEVER**: `500 Internal Server Error` for auth/missing resource issues

## Response Schema Requirements

### Arrays vs Objects
- ✅ **Arrays**: GET operations returning lists → direct array `[...]`
- ✅ **Objects**: POST/PUT operations and single item GETs → object `{...}`
- ❌ **Envelopes**: No wrapping objects for arrays (e.g. `{data: [...]}`)

### Required Fields
- **AgentResponse**: `content`, `response_type`, `view_state`
- **CaseSummary**: `case_id`, `title`, `status`, `created_at`
- **ErrorResponse**: `detail`, `error_type`, `correlation_id`, `timestamp`
- **QueryJobStatus**: `job_id`, `case_id`, `query`, `status`, `created_at`

### Headers Contract
- **Location**: Must be full path, not null (e.g. `/api/v1/cases/{id}/queries/{qid}`)
- **X-Total-Count**: String representation of total items for pagination
- **Link**: RFC 5988 format with `rel="next|prev|first|last"`
- **Retry-After**: Integer seconds for async polling guidance

## Error Response Standards

| Status | Use Case | Body Schema | Example |
|--------|----------|-------------|---------|
| 200 | Success with data | Resource schema | `AgentResponse`, `CaseSummary[]` |
| 201 | Resource created | Resource schema + Location header | `CaseResponse` |
| 202 | Accepted for async processing | Job status + Location + Retry-After | `QueryJobStatus` |
| 400 | Bad request/validation | `ErrorResponse` | Invalid query text |
| 401 | Authentication required | `ErrorResponse` | Service unavailable pre-auth |
| 403 | Forbidden | `ErrorResponse` | Access denied to resource |
| 404 | Resource not found | `ErrorResponse` | Case/query not found |
| 422 | Validation error | `HTTPValidationError` | Schema validation failed |
| 500 | Unexpected server error | `ErrorResponse` | Last resort only |

## CORS Requirements

### Exposed Headers
All these headers MUST be accessible to browser clients:
- `Location` - Resource creation/redirection
- `X-Total-Count` - Pagination metadata  
- `Link` - Pagination links
- `Retry-After` - Async polling guidance

### Allow Origins
- `chrome-extension://*` (Browser extension)
- `http://localhost:3000` (Development)
- `https://faultmaven.ai` (Production)

## Contract Compliance Checklist

### Happy Path Tests
- [x] `POST /cases` → `201` + `Location` + `case.case_id` in body
- [x] `GET /cases` → `200` + array + `X-Total-Count` + `Link`
- [x] `GET /sessions/{sid}/cases` → `200` + array (includes just-created)
- [x] `POST /cases/{cid}/queries` sync → `201` + `AgentResponse` + `Location`
- [x] `POST /cases/{cid}/queries` async → `202` + `QueryJobStatus` + `Location` + `Retry-After`
- [x] UI Delete → `POST /cases/{id}/archive` (200)
- [x] Admin hard delete → `DELETE /cases/{id}` (204)

### Error Path Tests  
- [ ] Pre-auth protected endpoint → `401` + `ErrorResponse` (NEVER `500`)
- [ ] Unknown case ID → `404` + `ErrorResponse`
- [ ] Empty results → `200` + `[]` (NEVER `404`)

### Header Tests
- [ ] All Location headers non-null and properly formatted
- [ ] CORS exposes required headers (`Location`, `X-Total-Count`, `Link`, `Retry-After`)
- [ ] Pagination headers present on array responses

### Shape Tests
- [ ] Arrays are arrays, objects are objects (no envelope confusion)
- [ ] Required fields present in all response schemas
- [ ] Error responses follow `ErrorResponse` schema consistently

---

**⚠️ CRITICAL**: This matrix is the contract. Any deviation requires explicit approval and frontend coordination.