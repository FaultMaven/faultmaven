# Runbook Creation Feature

## Overview

When a troubleshooting case is successfully resolved, FaultMaven offers to create a **runbook** - a structured document that captures the diagnosis process and solution for future reference.

## Purpose

**Problem**: Organizations repeatedly troubleshoot the same issues, wasting time rediscovering solutions.

**Solution**: Runbooks capture institutional knowledge, enabling faster resolution of recurring problems.

## User Experience

### Trigger
After a case is marked as resolved (solution confirmed working), FaultMaven responds:

> "Glad we solved it! To help with future troubleshooting, would you like me to create a runbook for this issue?"

### User Options
1. **Accept**: "Yes, create a runbook"
   - FaultMaven generates structured runbook
   - Saves to knowledge base for future reference

2. **Decline**: "No thanks" or ignore
   - Case closed without runbook
   - Can still be created later from case history

## Runbook Structure

A runbook should include:

### 1. Problem Summary
- **Title**: Brief description (e.g., "Redis Cluster OOM Kills")
- **Symptoms**: Observable error messages and indicators
- **Affected Systems**: Which services/components were impacted
- **Severity**: Production outage, degraded performance, etc.

### 2. Diagnosis Steps
- **Phase 1 - Blast Radius**: What was checked to understand impact
- **Phase 2 - Timeline**: When problem started, what changed
- **Phase 3 - Hypothesis**: Theories that were considered
- **Phase 4 - Validation**: Tests performed to validate theories

### 3. Root Cause
- **Identified Cause**: What actually caused the problem
- **Evidence**: Data that confirmed the root cause
- **Contributing Factors**: Secondary issues that exacerbated problem

### 4. Solution
- **Fix Applied**: Exact commands/changes made
- **Validation**: How to verify the fix worked
- **Rollback**: How to undo if fix causes issues

### 5. Prevention
- **Monitoring**: Metrics/alerts to detect recurrence
- **Automation**: Scripts/tools to prevent future occurrence
- **Process Improvements**: Policy/workflow changes

## Implementation (Future)

### Phase 1: Prompt-Based Runbook Generation
**Status**: ✅ Prompts updated

The LLM will offer runbook creation and generate structured markdown when user accepts.

**Files Modified**:
- [standard.py](../../faultmaven/prompts/doctor_patient/standard.py) - Added case resolution guidance
- [minimal.py](../../faultmaven/prompts/doctor_patient/minimal.py) - Added runbook offer
- [detailed.py](../../faultmaven/prompts/doctor_patient/detailed.py) - Added detailed runbook instructions
- [doctor_patient.py](../../faultmaven/models/doctor_patient.py) - Added `CREATE_RUNBOOK` action type

### Phase 2: Runbook Storage & Retrieval
**Status**: 🔲 Not implemented

**Components Needed**:
1. **Runbook Model**: Pydantic model for structured runbook data
2. **Runbook Store**: ChromaDB collection for runbook search
3. **Runbook Service**: CRUD operations for runbooks
4. **API Endpoints**: Create, retrieve, search runbooks

**API Design**:
```python
POST /api/v1/runbooks
GET /api/v1/runbooks/{runbook_id}
GET /api/v1/runbooks/search?q=redis+oom
POST /api/v1/cases/{case_id}/runbook  # Generate from case
```

### Phase 3: Runbook Suggestions
**Status**: 🔲 Not implemented

When a new case starts, search existing runbooks for similar problems:

```python
if similar_runbook_found:
    suggest_action = SuggestedAction(
        label="📖 View similar runbook",
        type=ActionType.QUESTION_TEMPLATE,
        payload=f"Show me runbook: {runbook.title}"
    )
```

### Phase 4: Runbook Analytics
**Status**: 🔲 Not implemented

Track runbook effectiveness:
- How often runbooks are accessed
- Resolution time with vs. without runbook
- Runbook accuracy (did it solve the problem?)
- Update frequency (are runbooks kept current?)

## Example Runbook

```markdown
# Runbook: Redis Cluster OOM Kills

**Created**: 2025-10-05
**Last Updated**: 2025-10-05
**Severity**: Critical
**Average Resolution Time**: 15 minutes

## Problem Summary

Redis pods repeatedly restart with OOM (Out of Memory) kills in production.

**Symptoms**:
- Pods restarting every 5-10 minutes
- Error: "connection refused on port 6379"
- K8s events: "OOMKilled"

**Affected Systems**:
- Redis cluster (3 pods)
- API services depending on Redis
- Session management

## Diagnosis Steps

### 1. Blast Radius Assessment
- Checked pod status: `kubectl get pods -n production`
- Verified API health: All APIs degraded due to cache unavailability

### 2. Timeline Analysis
- Problem started: 2025-10-05 14:30 UTC
- Recent changes: Deployed new feature with larger session objects

### 3. Hypothesis Formation
- **Theory 1**: Memory leak in Redis
- **Theory 2**: Insufficient memory limits
- **Theory 3**: Session objects too large (VALIDATED ✓)

### 4. Validation
```bash
# Check memory usage
kubectl top pod redis-0 -n production
# Output: 2.1Gi / 2Gi (105% - OOM threshold)

# Check session size
redis-cli --scan --pattern "session:*" | head -5
redis-cli DEBUG OBJECT session:abc123
# Output: serialized length: 5242880 (5MB per session!)
```

## Root Cause

Session objects grew from 100KB to 5MB after deploying new user preference feature.
Redis memory limit (2Gi) could only hold ~400 sessions before OOM.

## Solution

### Immediate Fix (10 mins)
```bash
# Increase Redis memory limit
kubectl patch statefulset redis -n production --patch '
  spec:
    template:
      spec:
        containers:
        - name: redis
          resources:
            limits:
              memory: 8Gi
'

# Restart pods
kubectl rollout restart statefulset/redis -n production
```

### Long-term Fix (1 day)
1. Optimize session serialization (compress large fields)
2. Implement session field lazy-loading
3. Add TTL to session keys (expire after 7 days)

### Verification
```bash
# Confirm pods stable
kubectl get pods -n production -w

# Monitor memory
kubectl top pod redis-0 -n production
# Target: < 50% memory usage
```

## Prevention

### Monitoring
```yaml
# Alert on high memory usage
alert: RedisMemoryHigh
expr: container_memory_usage_bytes{pod=~"redis-.*"} > 0.8 * container_spec_memory_limit_bytes
for: 5m
```

### Automation
- Pre-deployment check: Measure session size in staging
- Automatic session compression for objects > 1MB
- Weekly report on session size trends

### Process
- Add memory impact assessment to feature review checklist
- Load testing required for session-related changes
```

## Benefits

1. **Faster Resolution**: Team members can follow proven steps
2. **Knowledge Transfer**: New team members learn from past incidents
3. **Reduced MTTR**: Mean Time To Resolution decreases over time
4. **Pattern Recognition**: Identify recurring issues for permanent fixes
5. **Compliance**: Documentation for post-mortems and audits

## Future Enhancements

- **Runbook Templates**: Pre-filled templates for common problem types
- **Runbook Versioning**: Track changes and improvements over time
- **Runbook Collaboration**: Multiple users can improve runbooks
- **Runbook Testing**: Simulate problems to validate runbook accuracy
- **Integration with Knowledge Base**: Link runbooks to relevant documentation
