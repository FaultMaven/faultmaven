# FaultMaven API Documentation

This document provides an overview of FaultMaven's REST APIs. For detailed OpenAPI specifications, see the auto-generated docs for each service.

## API Gateway

**Base URL:** `http://localhost:8000`

The API Gateway is the single entry point for all client requests. All endpoints below are accessed through the gateway.

### Quick Reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Check API health |
| `/v1/meta/capabilities` | GET | Discover available features |
| `/v1/auth/register` | POST | Create new user |
| `/v1/auth/login` | POST | Authenticate user |
| `/v1/cases` | POST | Create troubleshooting case |
| `/v1/cases/{id}` | GET | Get case details |
| `/v1/agent/chat` | POST | Send message to AI agent |
| `/v1/knowledge/search` | POST | Search knowledge base |
| `/v1/evidence/upload` | POST | Upload file |

---

## Authentication

### Register User

**Endpoint:** `POST /v1/auth/register`

**Request:**
```json
{
  "email": "user@example.com",
  "password": "secure_password",
  "full_name": "John Doe"
}
```

**Response:**
```json
{
  "user_id": "usr_abc123",
  "email": "user@example.com",
  "full_name": "John Doe"
}
```

---

### Login

**Endpoint:** `POST /v1/auth/login`

**Request:**
```json
{
  "email": "user@example.com",
  "password": "secure_password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

**Use the access token in subsequent requests:**
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

## Case Management

### Create Case

**Endpoint:** `POST /v1/cases`

**Headers:**
```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request:**
```json
{
  "title": "Database connection timeout",
  "description": "Users experiencing intermittent timeouts during peak hours"
}
```

**Response:**
```json
{
  "case_id": "case_xyz789",
  "user_id": "usr_abc123",
  "title": "Database connection timeout",
  "description": "Users experiencing intermittent timeouts during peak hours",
  "status": "consulting",
  "created_at": "2025-11-20T10:30:00Z"
}
```

---

### Get Case

**Endpoint:** `GET /v1/cases/{case_id}`

**Headers:**
```
Authorization: Bearer <access_token>
```

**Response:**
```json
{
  "case_id": "case_xyz789",
  "user_id": "usr_abc123",
  "title": "Database connection timeout",
  "status": "investigating",
  "messages": [
    {
      "message_id": "msg_001",
      "role": "user",
      "content": "Database is timing out",
      "timestamp": "2025-11-20T10:31:00Z"
    },
    {
      "message_id": "msg_002",
      "role": "agent",
      "content": "Can you provide the error logs?",
      "timestamp": "2025-11-20T10:31:05Z"
    }
  ],
  "created_at": "2025-11-20T10:30:00Z",
  "updated_at": "2025-11-20T10:31:05Z"
}
```

---

### List Cases

**Endpoint:** `GET /v1/cases`

**Headers:**
```
Authorization: Bearer <access_token>
```

**Query Parameters:**
- `status` (optional): Filter by status (`consulting`, `investigating`, `resolved`, `closed`)
- `limit` (optional): Number of results (default: 50, max: 100)
- `offset` (optional): Pagination offset

**Response:**
```json
{
  "cases": [
    {
      "case_id": "case_xyz789",
      "title": "Database connection timeout",
      "status": "investigating",
      "created_at": "2025-11-20T10:30:00Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

---

## AI Agent

### Chat with Agent

**Endpoint:** `POST /v1/agent/chat`

**Headers:**
```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request:**
```json
{
  "case_id": "case_xyz789",
  "message": "The database is timing out intermittently during peak hours"
}
```

**Response:**
```json
{
  "message_id": "msg_003",
  "case_id": "case_xyz789",
  "role": "agent",
  "content": "Let's investigate this systematically. Can you tell me:\n1. What database system are you using?\n2. When did this issue start?\n3. Do you have any error logs from the timeouts?",
  "timestamp": "2025-11-20T10:32:00Z"
}
```

---

## Knowledge Base

### Upload Document

**Endpoint:** `POST /v1/knowledge/documents`

**Headers:**
```
Authorization: Bearer <access_token>
Content-Type: multipart/form-data
```

**Request (form-data):**
```
file: [PDF/TXT/MD file]
title: "PostgreSQL Performance Tuning Guide"
document_type: "playbook"
tags: "database,postgresql,performance"
```

**Response:**
```json
{
  "document_id": "doc_abc123",
  "title": "PostgreSQL Performance Tuning Guide",
  "status": "processing",
  "message": "Document uploaded successfully. Processing in background."
}
```

---

### Search Knowledge Base

**Endpoint:** `POST /v1/knowledge/search`

**Headers:**
```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request:**
```json
{
  "query": "How to fix database connection timeouts?",
  "limit": 5
}
```

**Response:**
```json
{
  "results": [
    {
      "document_id": "doc_abc123",
      "title": "PostgreSQL Performance Tuning Guide",
      "snippet": "...connection pool settings can cause timeouts during peak load. Increase max_connections and connection_pool_size...",
      "relevance_score": 0.87,
      "document_type": "playbook"
    }
  ],
  "query": "How to fix database connection timeouts?",
  "total_results": 1
}
```

---

## Evidence Management

### Upload File

**Endpoint:** `POST /v1/evidence/upload`

**Headers:**
```
Authorization: Bearer <access_token>
Content-Type: multipart/form-data
```

**Request (form-data):**
```
file: [log file, screenshot, config file]
case_id: "case_xyz789"
evidence_type: "log"
description: "Database error logs from production"
```

**Response:**
```json
{
  "evidence_id": "evd_def456",
  "case_id": "case_xyz789",
  "filename": "database.log",
  "file_size": 15234,
  "evidence_type": "log",
  "uploaded_at": "2025-11-20T10:35:00Z"
}
```

---

### Get File

**Endpoint:** `GET /v1/evidence/{evidence_id}`

**Headers:**
```
Authorization: Bearer <access_token>
```

**Response:** Binary file download

---

## Capabilities Discovery

### Get Capabilities

**Endpoint:** `GET /v1/meta/capabilities`

**No authentication required**

**Response:**
```json
{
  "deploymentMode": "self-hosted",
  "version": "2.0.0",
  "features": {
    "chat": true,
    "knowledgeBase": true,
    "cases": true,
    "organizations": false,
    "teams": false,
    "sso": false
  },
  "auth": {
    "type": "jwt",
    "loginUrl": "http://localhost:8000/v1/auth/login"
  },
  "dashboardUrl": "http://localhost:3000"
}
```

---

## Health Check

### Check API Health

**Endpoint:** `GET /health`

**No authentication required**

**Response:**
```json
{
  "status": "healthy",
  "version": "2.0.0",
  "services": {
    "auth": "healthy",
    "session": "healthy",
    "case": "healthy",
    "knowledge": "healthy",
    "evidence": "healthy",
    "agent": "healthy"
  },
  "timestamp": "2025-11-20T10:40:00Z"
}
```

---

## Error Responses

All errors follow this format:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid email format",
    "details": {
      "field": "email",
      "reason": "Must be a valid email address"
    }
  }
}
```

### Common HTTP Status Codes

| Code | Meaning | When |
|------|---------|------|
| 200 | OK | Request succeeded |
| 201 | Created | Resource created successfully |
| 400 | Bad Request | Invalid input data |
| 401 | Unauthorized | Missing or invalid access token |
| 403 | Forbidden | Valid token but insufficient permissions |
| 404 | Not Found | Resource doesn't exist |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server-side error |

---

## Rate Limiting

**Current Limits:**
- 100 requests per minute per user
- 10 document uploads per hour
- 50 knowledge searches per minute

**Rate Limit Headers:**
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 87
X-RateLimit-Reset: 1732104000
```

---

## Auto-Generated API Docs

Each service provides interactive API documentation:

- **API Gateway:** http://localhost:8000/docs
- **Auth Service:** http://localhost:8001/docs
- **Case Service:** http://localhost:8003/docs
- **Knowledge Service:** http://localhost:8004/docs
- **Agent Service:** http://localhost:8006/docs

These are **auto-generated from code** using FastAPI's OpenAPI integration.

---

## Code Examples

### Python

```python
import requests

# Login
response = requests.post(
    "http://localhost:8000/v1/auth/login",
    json={
        "email": "user@example.com",
        "password": "secure_password"
    }
)
if response.status_code != 200:
    raise Exception(f"Login failed: {response.text}")
token = response.json()["access_token"]

# Create case
headers = {"Authorization": f"Bearer {token}"}
response = requests.post(
    "http://localhost:8000/v1/cases",
    headers=headers,
    json={
        "title": "Database timeout",
        "description": "Timeouts during peak hours"
    }
)
if response.status_code not in [200, 201]:
    raise Exception(f"Failed to create case: {response.text}")
case_id = response.json()["case_id"]

# Chat with agent
response = requests.post(
    "http://localhost:8000/v1/agent/chat",
    headers=headers,
    json={
        "case_id": case_id,
        "message": "Database is timing out"
    }
)
if response.status_code != 200:
    raise Exception(f"Chat failed: {response.text}")
print(response.json()["content"])
```

### cURL

```bash
# Login
curl -X POST http://localhost:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"secure_password"}'

# Create case
curl -X POST http://localhost:8000/v1/cases \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"Database timeout","description":"Timeouts during peak"}'

# Chat with agent
curl -X POST http://localhost:8000/v1/agent/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"case_id":"case_xyz789","message":"Database is timing out"}'
```

---

**Last Updated:** 2025-11-20
**Version:** 2.0
