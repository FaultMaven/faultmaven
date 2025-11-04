# FaultMaven API Contract v1.0

**Purpose**: Document API endpoints and contracts that MUST NOT change during v2.0 migration
**Date**: 2025-11-04
**Status**: FROZEN - Frontend depends on these signatures

---

## Critical Principle

⚠️ **INTERNAL implementation can change, EXTERNAL API contract MUST remain unchanged**

```python
# ✅ ALLOWED: Change internal implementation
async def process_query(case_id: str, request: QueryRequest):
    # NEW: Use milestone-based engine instead of OODA
    engine = MilestoneInvestigationEngine(...)
    result = await engine.process_turn(...)
    return result

# ❌ FORBIDDEN: Change API signature
async def process_query(case_id: str, new_param: str, request: QueryRequest):
    # BREAKS FRONTEND!
```

---

## Session Management Endpoints

### POST /api/v1/sessions
**Purpose**: Create new session
**Contract**: FROZEN

```python
# Request
{
    "user_id": Optional[str],
    "timeout_minutes": Optional[int]  # Default: 180, Range: 60-480
}

# Response
{
    "session_id": str,
    "created_at": str (ISO 8601),
    "expires_at": str (ISO 8601),
    "status": "active" | "expired"
}
```

### GET /api/v1/sessions/{session_id}
**Purpose**: Get session details
**Contract**: FROZEN

```python
# Response
{
    "session_id": str,
    "user_id": Optional[str],
    "created_at": str,
    "expires_at": str,
    "last_activity_at": str,
    "status": "active" | "expired"
}
```

---

## Case Management Endpoints

### POST /api/v1/cases
**Purpose**: Create new case
**Contract**: FROZEN

```python
# Request (CaseCreateRequest)
{
    "title": str,
    "description": Optional[str],
    "priority": "low" | "normal" | "medium" | "high" | "critical",
    "tags": List[str],
    "session_id": Optional[str],
    "initial_message": Optional[str]
}

# Response (CaseResponse)
{
    "case_id": str,
    "title": str,
    "status": "consulting" | "investigating" | "resolved" | "closed",
    "priority": str,
    "created_at": str,
    "updated_at": str,
    "message_count": int,
    "participant_count": int
}
```

**Migration Notes**:
- ✅ `status` values remain same (4 states)
- ✅ No new required fields
- ⚠️ Internal: `status` now maps to milestone-based investigation

### GET /api/v1/cases/{case_id}
**Purpose**: Get case details
**Contract**: FROZEN

```python
# Response
{
    "case_id": str,
    "title": str,
    "description": Optional[str],
    "status": str,  # 4 statuses unchanged
    "priority": str,
    "created_at": str,
    "updated_at": str,
    "messages": List[Message],
    "message_count": int,
    "participants": List[Participant],
    "context": Dict[str, Any]
}
```

**Migration Notes**:
- ⚠️ Internal: `context` may include new milestone progress
- ✅ Frontend doesn't parse context structure - safe to change

### POST /api/v1/cases/{case_id}/query
**Purpose**: Send query to agent (process turn)
**Contract**: FROZEN

```python
# Request (QueryRequest)
{
    "query": str,
    "context": Optional[Dict[str, Any]],
    "stream": Optional[bool] = False,
    "async_mode": Optional[bool] = False
}

# Response (AgentResponse)
{
    "response": str,
    "response_type": "text" | "analysis" | "solution",
    "case_id": str,
    "metadata": {
        "processing_time_ms": int,
        "confidence_score": Optional[float],
        # ... other metadata (extensible)
    }
}
```

**Migration Notes**:
- ✅ Request/response unchanged
- ⚠️ Internal: Now uses `MilestoneInvestigationEngine` instead of phase orchestrator
- ✅ `metadata` is extensible - can add new fields without breaking frontend

### POST /api/v1/cases/{case_id}/data
**Purpose**: Upload data/evidence
**Contract**: FROZEN

```python
# Request (multipart/form-data)
{
    "file": UploadFile,
    "data_type": Optional[str],
    "description": Optional[str]
}

# Response (DataUploadResponse)
{
    "upload_id": str,
    "filename": str,
    "size_bytes": int,
    "status": "processing" | "completed" | "failed",
    "processing_job_id": Optional[str]
}
```

**Migration Notes**:
- ✅ Upload flow unchanged
- ⚠️ Internal: Evidence now tracked with mention_count

### GET /api/v1/cases/{case_id}/messages
**Purpose**: Get case conversation history
**Contract**: FROZEN

```python
# Response (CaseMessagesResponse)
{
    "case_id": str,
    "messages": List[{
        "message_id": str,
        "message_type": "user_query" | "agent_response" | "system_event",
        "content": str,
        "timestamp": str,
        "author_id": Optional[str],
        "metadata": Dict[str, Any]
    }],
    "total_count": int
}
```

---

## Agent-Specific Endpoints

### POST /api/v1/agent/process
**Purpose**: Process agent turn (if separate from /cases/{id}/query)
**Contract**: FROZEN

```python
# Request
{
    "case_id": str,
    "user_message": str,
    "attachments": Optional[List[str]]
}

# Response
{
    "response": str,
    "case_id": str,
    "status": str,
    "metadata": Dict[str, Any]
}
```

**Migration Notes**:
- ⚠️ **CRITICAL**: This is the main agent interaction endpoint
- ✅ Request/response signatures MUST NOT change
- ⚠️ Internal: Entire OODA → Milestone migration happens here
- ✅ Frontend never sees internal investigation state

---

## Status Transition Contract

### Case Status Values (FROZEN)
```python
class CaseStatus(str, Enum):
    CONSULTING = "consulting"      # ✅ Keep
    INVESTIGATING = "investigating" # ✅ Keep
    RESOLVED = "resolved"          # ✅ Keep
    CLOSED = "closed"              # ✅ Keep

# ❌ DO NOT ADD: "intake", "blast_radius", "hypothesis", etc.
# These are internal OODA phases, not user-facing statuses
```

### Valid Transitions (Unchanged)
```
CONSULTING → INVESTIGATING → RESOLVED
CONSULTING → INVESTIGATING → CLOSED
CONSULTING → CLOSED (abandon)
```

---

## Response Metadata Extensions (SAFE)

Frontend ignores unknown metadata fields, so these are **safe to add**:

```python
# ✅ SAFE: Add new metadata fields
{
    "metadata": {
        # Existing fields (keep)
        "processing_time_ms": 1234,
        "confidence_score": 0.85,

        # NEW fields (safe to add)
        "milestones_completed": ["symptom_verified", "root_cause_identified"],
        "investigation_stage": "diagnosing",  # Computed from milestones
        "progress_percentage": 0.625,
        "turn_number": 5,
        "evidence_requests_pending": 2
    }
}
```

---

## Authentication Endpoints (FROZEN)

### POST /api/v1/auth/login
**Contract**: FROZEN

### POST /api/v1/auth/logout
**Contract**: FROZEN

### GET /api/v1/auth/me
**Contract**: FROZEN

---

## Breaking Change Detection

### How to Verify API Contract Preserved

```bash
# 1. Run API tests (must pass 100%)
pytest tests/api/ -v

# 2. Generate OpenAPI spec and compare
# Before migration
python -m faultmaven.api.generate_openapi > api_spec_v1.json

# After migration
python -m faultmaven.api.generate_openapi > api_spec_v2.json

# Compare
diff api_spec_v1.json api_spec_v2.json
# Should show: No changes to endpoint signatures
```

### API Contract Tests

```python
# tests/api/test_contract_preservation.py

def test_case_query_endpoint_signature_unchanged():
    """Test POST /cases/{case_id}/query contract preserved"""
    response = client.post(
        "/api/v1/cases/case_123/query",
        json={"query": "Test message"}
    )

    # Response structure unchanged
    assert response.status_code == 200
    data = response.json()
    assert 'response' in data
    assert 'case_id' in data
    assert 'metadata' in data

def test_case_status_values_unchanged():
    """Test CaseStatus enum has exactly 4 values"""
    from faultmaven.models.case import CaseStatus

    statuses = [s.value for s in CaseStatus]
    assert len(statuses) == 4
    assert "consulting" in statuses
    assert "investigating" in statuses
    assert "resolved" in statuses
    assert "closed" in statuses

    # No phase-based statuses
    assert "intake" not in statuses
    assert "blast_radius" not in statuses
```

---

## Migration Checklist

Before merging v2.0 migration:

- [ ] ✅ All API tests pass (`pytest tests/api/ -v`)
- [ ] ✅ OpenAPI spec diff shows no signature changes
- [ ] ✅ Status enum has exactly 4 values
- [ ] ✅ Request/response schemas unchanged
- [ ] ✅ Frontend integration tests pass
- [ ] ✅ No new required fields added to requests
- [ ] ✅ Metadata extensions only (no removals)

---

## Contact for API Changes

If API contract changes are ABSOLUTELY necessary:
1. Document breaking change with clear migration path
2. Frontend team approval required
3. API versioning (v2 endpoints) recommended
4. Deprecation period (minimum 1 month)

---

**Last Updated**: 2025-11-04
**Next Review**: After Phase 7 (Final Testing)
**Status**: Active Contract - DO NOT BREAK
