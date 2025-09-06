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

**Last Updated**: 2025-09-02  
**Total Active Items**: 2  
**Next Review**: [Date for next technical debt review]