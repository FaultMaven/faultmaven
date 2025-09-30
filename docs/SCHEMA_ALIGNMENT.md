# Schema Alignment Documentation

**Date:** 2025-09-29
**Issue:** Schema mismatches causing validation failures between API and domain layers

## Problems Identified

### 1. Message Role Mismatch (FIXED)
**Issue:** API `Message` model only accepted `role: Literal["user", "agent"]`, but Redis/infrastructure returned `"assistant"` for AI responses.

**Root Cause:** Hardcoded Literal in API model didn't match infrastructure layer mapping.

**Fix Applied:**
- Updated `Message.role` to: `Literal["user", "agent", "assistant", "system"]`
- Location: `/faultmaven/models/api.py:343`

**Impact:** AI responses now properly persist and display in frontend.

---

### 2. CasePriority "normal" Value Mismatch (FIXED)
**Issue:** `QueryRequest.priority` in API layer used `Literal["low", "normal", "medium", "high", "critical"]` but `CasePriority` enum only had `LOW, MEDIUM, HIGH, CRITICAL`.

**Root Cause:** `AgenticPriority` and `QueryUrgency` enums use `NORMAL` value, but `CasePriority` didn't include it.

**Fix Applied:**
- Added `NORMAL = "normal"` to `CasePriority` enum
- Location: `/faultmaven/models/case.py:35`
- Comment added for clarity

**Impact:** Prevents validation errors when "normal" priority is used in queries.

---

### 3. CaseStatus "resolved" vs "solved" Mismatch (FIXED)
**Issue:** API `Case` model used `status: Literal["active", "resolved", "archived"]` but `CaseStatus` enum had `SOLVED` instead of `RESOLVED`.

**Root Cause:** Frontend/API layer expects "resolved" terminology, but domain model used "solved".

**Fix Applied:**
- Added `RESOLVED = "resolved"` to `CaseStatus` enum
- Location: `/faultmaven/models/case.py:26`
- Kept `SOLVED` for backward compatibility
- Added comment: "Alias for solved - used by frontend/API"

**Impact:** API and domain models now aligned on status terminology.

---

## Schema Alignment Principles

### 1. **Enum Truth**: Domain enums are source of truth
- All valid values should be defined in domain enum classes
- API Literals should reference or match enum values
- Infrastructure layer mappings should align with enum values

### 2. **Backward Compatibility**: Add, don't remove
- When adding values for compatibility, keep existing values
- Use aliases/comments to document relationships
- Consider deprecation path if values need to change

### 3. **Layer Consistency**: API ↔ Domain ↔ Infrastructure
- API models (Pydantic) should validate against domain enums
- Infrastructure layer should return values that match domain enums
- Conversion/mapping happens at layer boundaries, not within layers

### 4. **Explicit Over Implicit**: Document mappings
- When roles/statuses map differently (e.g., "assistant" vs "agent"), document why
- Add comments explaining frontend/backend terminology differences
- Keep a schema alignment document (this file) updated

---

## Current Enum Definitions

### CaseStatus
```python
ACTIVE = "active"
INVESTIGATING = "investigating"
SOLVED = "solved"
RESOLVED = "resolved"  # Alias for solved - frontend/API uses this
STALLED = "stalled"
ARCHIVED = "archived"
SHARED = "shared"
```

### CasePriority
```python
LOW = "low"
NORMAL = "normal"  # Added for API compatibility with AgenticPriority
MEDIUM = "medium"
HIGH = "high"
CRITICAL = "critical"
```

### MessageType
```python
USER_QUERY = "user_query"
AGENT_RESPONSE = "agent_response"
SYSTEM_EVENT = "system_event"
DATA_UPLOAD = "data_upload"
CASE_NOTE = "case_note"
STATUS_CHANGE = "status_change"
```

### Message Role Values (API Layer)
```python
role: Literal["user", "agent", "assistant", "system"]
```

**Mapping:**
- `MessageType.USER_QUERY` → `role="user"`
- `MessageType.AGENT_RESPONSE` → `role="assistant"` (Redis) or `role="agent"` (alternative)
- `MessageType.CASE_NOTE` → `role="user"`
- `MessageType.SYSTEM_EVENT` → `role="system"`

---

## Testing Checklist

When adding new enum values or API models:

- [ ] Check all Literal types in API models match domain enums
- [ ] Verify infrastructure layer returns values that pass API validation
- [ ] Test round-trip: API → Service → Domain → Infrastructure → Storage → Retrieval → API
- [ ] Ensure frontend expectations are met (check OpenAPI spec comments)
- [ ] Update this document with any new mappings or aliases

---

## Related Files

### Domain Models
- `/faultmaven/models/case.py` - Core enums (CaseStatus, CasePriority, MessageType)
- `/faultmaven/models/agentic.py` - Agentic enums (AgenticPriority, QueryUrgency)

### API Models
- `/faultmaven/models/api.py` - API request/response models with Literal types

### Infrastructure
- `/faultmaven/infrastructure/persistence/redis_case_store.py` - Redis message role mapping
- `/faultmaven/services/domain/case_service.py` - Service layer message conversion

---

## Future Improvements

1. **Use Enums in API Models**: Replace `Literal` types with actual enum references where possible
2. **Centralized Validation**: Create a validation layer that ensures enum consistency
3. **Automated Testing**: Add schema validation tests that catch mismatches early
4. **OpenAPI Generation**: Ensure generated OpenAPI spec reflects actual enum values