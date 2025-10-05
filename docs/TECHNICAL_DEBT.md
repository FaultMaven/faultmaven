# FaultMaven Technical Debt Registry

This document tracks technical debt items that need future attention, organized by priority and impact.

---

## 🔴 **HIGH PRIORITY**

### #001 - Async Query Processing Failure
**Status**: ❌ Active Issue  
**Date Identified**: 2025-09-02  
**Impact**: Complex queries (>1000 chars or with keywords like "analyze logs") fail to process  

**Problem**: Background async processing fails with `name 'request' is not defined` error when calling `agent_service.process_query_for_case()` from asyncio background task context.

**Root Cause**: Agent service call chain references FastAPI `Request` object that doesn't exist in background task context.

**Current Workaround**: Simple queries work perfectly via synchronous processing. Users can rephrase complex queries to avoid trigger keywords.

**Solution Options**:
1. **Fix Request Reference** (Recommended) - Find and fix the `request` object reference in agent service chain
2. **Simplify to Sync-Only** - Remove async processing entirely for simpler architecture  
3. **Context-Aware Injection** - Pass necessary request data to background task

**Files**: `/home/swhouse/projects/FaultMaven/faultmaven/api/v1/routes/case.py:59-98`, agent service chain

**Test Commands**:
```bash
# Triggers the error
curl -X POST "http://localhost:8000/api/v1/cases/{case_id}/queries" \
  -d '{"session_id": "test", "query": "analyze logs in detail"}'
```

---

## 🟡 **MEDIUM PRIORITY**

### #002 - Overly Simplistic Greeting Detection  
**Status**: 🔄 Design Issue  
**Date Identified**: 2025-09-02  
**Impact**: Any query starting with common greetings returns hardcoded response instead of LLM processing

**Problem**: Greeting pattern `^(hi|hello|hey|yo|howdy|sup|good\s*(morning|afternoon|evening))\b` is too broad and catches legitimate troubleshooting queries that happen to start with greetings.

**Root Cause**: Simple regex pattern in `/home/swhouse/projects/FaultMaven/faultmaven/core/gateway/gateway.py:34`

**Current Behavior**: Query "hello, my server is down" returns canned greeting instead of troubleshooting help

**Solution Options**:
1. **Make Pattern More Restrictive** - Only match queries that are ONLY greetings
2. **Context-Aware Detection** - Consider query length and additional content
3. **Remove Greeting Logic** - Process all queries through LLM for consistent behavior

**Files**: `/home/swhouse/projects/FaultMaven/faultmaven/core/gateway/gateway.py`, `/home/swhouse/projects/FaultMaven/faultmaven/services/agent.py:345-375`

---

## 🟢 **LOW PRIORITY**

### #003 - Knowledge Base Lacks User Scoping and Authentication
**Status**: 🔄 Security & Design Issue
**Date Identified**: 2025-10-03
**Impact**: Documents uploaded via browser extension are not user-scoped, creating security and organizational issues

**Problem**: The `/api/v1/knowledge/documents` endpoint lacks authentication and user scoping. All documents are stored globally without owner attribution, making it impossible to:
- Restrict access to user's own documents
- Distinguish between personal notes and team knowledge
- Implement proper access control
- Maintain audit trails

**Root Cause**: Knowledge base was designed for system-wide documents (uploaded via kb-toolkit) before browser extension user uploads were added. Authentication dependency was never implemented.

**Current Behavior**:
- Anyone can upload documents without authentication
- No user_id association on documents
- User documents mixed with system-wide toolkit documents
- No "my documents" filtering capability

**Solution Design**: Two-tier knowledge base system:
1. **User-Scoped Documents** (browser extension) - Private to user, require authentication
2. **System-Scoped Documents** (kb-toolkit) - Shared globally, uploaded by admin/service account

**Required Changes**:
- Backend: Add `current_user: DevUser = Depends(require_authentication)` to knowledge routes
- Backend: Add `user_id`, `scope`, `is_shared` fields to KnowledgeDocument model
- Backend: Update service layer to filter by user context
- Backend: Update ChromaDB metadata for user scoping
- Frontend: No code changes (already uses `authenticatedFetch()`)
- Frontend: Add UI to show document ownership (personal vs team)
- API Spec: Update OpenAPI schema with security requirement
- Migration: Backfill existing documents with appropriate scope

**Impact**: Breaking change - requires schema updates and API behavior changes

**Detailed Design**: See [KNOWLEDGE_BASE_USER_SCOPING_ISSUE.md](security/KNOWLEDGE_BASE_USER_SCOPING_ISSUE.md)

**Files**:
- `/home/swhouse/projects/FaultMaven/faultmaven/api/v1/routes/knowledge.py:51-80`
- `/home/swhouse/projects/FaultMaven/docs/api/openapi.locked.yaml:3248-3285`
- `/home/swhouse/projects/faultmaven-copilot/src/lib/api.ts:484-565`

**Recommendation**: Target v3.3.0 with phased implementation to minimize disruption

---

## 🔧 **RESOLVED**

---

## **Template for New Entries**

### #XXX - [Issue Title]
**Status**: [❌ Active Issue | 🔄 Design Issue | ✅ Resolved]  
**Date Identified**: YYYY-MM-DD  
**Impact**: [Brief description of user/system impact]

**Problem**: [Clear description of the issue]

**Root Cause**: [Technical explanation of why it happens]

**Current Workaround**: [Any temporary solutions]

**Solution Options**:
1. **Option 1** - Description
2. **Option 2** - Description

**Files**: [List of affected files with line numbers if relevant]

**Test Commands/Steps**: [Commands to reproduce or test the issue]

---

**Last Updated**: 2025-10-03
**Total Active Items**: 3
**Next Review**: [Date for next technical debt review]