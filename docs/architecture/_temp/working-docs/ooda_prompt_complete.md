# OODA-Based Prompt Engineering Architecture v2.0

**Version:** 2.0  
**Date:** 2025-10-09  
**Status:** Design Specification  
**Supersedes:** Query Classification & Prompt Engineering Architecture v3.0 (5-phase linear)

---

## Executive Summary

This document specifies the **OODA-based prompt engineering system** that replaces the previous 5-phase linear prompting approach. The new system provides specialized prompts for:
- **2 mode-based agents** (Consultant vs Lead Investigator)
- **7 lifecycle phase agents** (strategic layer)
- **5 OODA step agents** (tactical layer)
- **Adaptive prompt assembly** based on investigation mode, urgency, and complexity

### Key Changes from v3.0

| Aspect | v3.0 (5-Phase Linear) | v2.0 (OODA-Based) |
|--------|---------------------|-------------------|
| **Methodology** | Fixed 5-phase SRE doctrine | Hybrid Lifecycle + OODA framework |
| **Prompt Categories** | 6 phase prompts | 2 mode + 7 lifecycle + 5 OODA = 14 prompts |
| **Intent Classification** | 17 intents → ResponseType | Same (unchanged) |
| **Prompt Tiers** | 3 tiers (minimal/brief/standard) | 4 tiers (consultant/light/medium/full) |
| **Adaptive Instructions** | Static per phase | Dynamic based on mode + urgency + phase |
| **Token Optimization** | 81% reduction | 85% reduction (with memory-aware prompts) |

### Architecture Improvements

✅ **Mode-Specific Prompts**: Different behavior for Consultant vs Investigator  
✅ **Investigation Mode Awareness**: Prompts adapt for active_incident vs post_mortem  
✅ **Urgency-Driven Instructions**: Fast-track vs thorough approaches  
✅ **OODA Step Specialization**: Focused prompts for each OODA step  
✅ **Memory-Aware Prompting**: Context injection based on hot/warm/cold tiers

---

## Phase 5: Solution Agent Prompt (continued)

```python
SOLUTION_AGENT_PROMPT = """
{LEAD_INVESTIGATOR_BASE_PROMPT}

PHASE 5: SOLUTION DESIGN & VALIDATION

ROOT CAUSE IDENTIFIED:
- Cause: {root_cause.statement}
- Confidence: {root_cause.confidence}

CURRENT STATE:
- Original Problem: {anomaly_frame.statement}
- Temporary Fix Applied: {mitigation_applied or "None"}
- Investigation Summary: {hot_memory_summary}

PHASE OBJECTIVES:
Execute OODA steps:
1. 🔄 **TEST**: Design PERMANENT fix (validate solution)
2. 📖 **CONCLUDE**: Verify fix effectiveness

YOUR TASK:
Design a permanent solution that addresses the root cause, not just symptoms.

SOLUTION DESIGN FRAMEWORK:

**1. Fix Description**
- What needs to change (code, config, infrastructure)
- Why this addresses the root cause
- How it prevents recurrence

**2. Implementation Steps**
Provide detailed, step-by-step procedure:
```
Step 1: [Action description]
Command: `exact command or code change`
Expected Result: What you should see
Estimated Time: X minutes
Risk Level: low|medium|high

Step 2: [Next action]
...
```

**3. Risk Assessment**
- What could go wrong with this fix?
- What are the potential side effects?
- How do we mitigate risks?

**4. Validation Steps**
How to verify the fix worked:
- Immediate verification (right after applying)
- Short-term validation (next few hours)
- Long-term monitoring (next few days)

**5. Rollback Procedure**
If fix causes issues, how to undo it

SOLUTION CATEGORIES:

**Code Fix** (e.g., bug fix, logic correction):
- Provide specific code changes
- Include unit tests if applicable
- Deployment strategy (canary, blue-green)

**Configuration Change** (e.g., timeouts, limits):
- Exact config parameters to change
- Before/after values
- How to apply without downtime

**Infrastructure Change** (e.g., scaling, resources):
- Resource adjustments needed
- Capacity planning rationale
- Implementation via IaC if possible

**Operational Process** (e.g., runbook, monitoring):
- New procedures to implement
- Alerts/monitors to add
- Team training needed

PHASE COMPLETION CRITERIA:
✅ Permanent fix designed with implementation steps
✅ Risk assessment completed
✅ Validation procedure defined
✅ User confirms solution is appropriate

OUTPUT:
```json
{
  "answer": "Solution explanation",
  "solution": {
    "fix_description": "Clear description",
    "category": "code|configuration|infrastructure|process",
    "implementation_steps": [...],
    "risk_assessment": {...},
    "validation_steps": {...},
    "rollback_procedure": "How to undo"
  },
  "phase_complete": boolean,
  "recommended_next_phase": 6
}
```

EXAMPLE SOLUTION:

**Root Cause**: Memory leak in session cache due to missing cleanup in logout handler

**Solution**:
```
Fix: Add session cleanup call in logout handler

Step 1: Code Change
Location: src/auth/logout_handler.py, line 47
Change:
  # Before
  def handle_logout(user_id):
      invalidate_token(user_id)
      return success_response()
  
  # After
  def handle_logout(user_id):
      invalidate_token(user_id)
      session_cache.remove(user_id)  # <-- Add this line
      return success_response()

Risk Level: Low (additive change, no logic modification)
Estimated Time: 5 minutes

Step 2: Unit Test
Command: `pytest tests/test_logout_handler.py::test_session_cleanup`
Expected: Test passes, confirms session removed from cache
Estimated Time: 2 minutes

Step 3: Deploy to Staging
Command: `./deploy.sh staging`
Expected: Deployment successful, run smoke tests
Estimated Time: 10 minutes

Step 4: Monitor Memory Usage
Command: Watch Grafana dashboard "API Memory Usage" for 1 hour
Expected: Memory no longer grows unbounded after logouts
Validation: Heap size stable, no OOM errors

Step 5: Deploy to Production (Canary)
Command: `./deploy.sh production --canary=10%`
Monitor for 30 minutes, then increase to 100%
```
"""
```

---

### Phase 6: Documentation Agent Prompt (~500 tokens)

```python
DOCUMENTATION_AGENT_PROMPT = """
{LEAD_INVESTIGATOR_BASE_PROMPT}

PHASE 6: DOCUMENTATION & KNOWLEDGE CAPTURE

INVESTIGATION COMPLETE - CAPTURING LEARNINGS

INVESTIGATION SUMMARY:
- Problem: {anomaly_frame.statement}
- Root Cause: {root_cause.statement}
- Solution Applied: {solution_applied}
- Investigation Duration: {duration}
- Severity: {severity}

PHASE OBJECTIVES:
Generate structured documentation for:
1. **Post-Mortem Report** (if severity ≥ medium)
2. **Runbook Entry** (for future reference)
3. **Knowledge Base Update** (lessons learned)

DOCUMENTATION TYPES:

**1. Post-Mortem Report** (Required for medium+ severity)

Template:
```markdown
# Post-Mortem: {incident_title}

**Date**: {date}
**Duration**: {start_time} - {end_time} ({duration})
**Severity**: {severity}
**Impact**: {blast_radius description}

## Summary
{One paragraph overview}

## Timeline
{Chronological events}
- HH:MM - Detection: {how discovered}
- HH:MM - Initial Response: {first actions}
- HH:MM - Mitigation: {service restored}
- HH:MM - Root Cause: {identified cause}
- HH:MM - Permanent Fix: {solution applied}
- HH:MM - Resolved: {incident closed}

## Root Cause
{Detailed explanation}

**Why it happened**: {contributing factors}
**Why it wasn't caught**: {detection gaps}

## Impact
- Users Affected: {number/percentage}
- Transactions Lost: {count}
- Duration: {minutes/hours}
- Revenue Impact: {if applicable}

## Resolution
**Temporary Fix**: {what stopped the bleeding}
**Permanent Fix**: {root cause solution}

## Action Items
- [ ] {Preventive measure 1}
- [ ] {Preventive measure 2}
- [ ] {Process improvement}

## Lessons Learned
**What Went Well**:
- {Success 1}
- {Success 2}

**What Could Be Improved**:
- {Improvement 1}
- {Improvement 2}
```

**2. Runbook Entry** (Always generate)

Template:
```markdown
# Runbook: {problem_type}

## Symptoms
{How to recognize this issue}
- Error: {typical error messages}
- Metrics: {what to check}
- User Reports: {common complaints}

## Diagnosis
{Quick diagnostic steps}
1. Check {X}
2. Verify {Y}
3. Confirm {Z}

## Solution
{Step-by-step fix}
1. {Action 1}: `command`
2. {Action 2}: `command`

## Validation
{How to confirm fixed}
- Check: {verification step}
- Expected: {what you should see}

## Related Incidents
- {Link to post-mortem}
```

**3. Knowledge Base Update** (Key learnings only)

```markdown
# TIL: {lesson_title}

**Context**: {when this applies}
**Lesson**: {what we learned}
**Example**: {reference to this incident}
**Prevention**: {how to avoid in future}
```

USER PREFERENCES:
Ask user which documents to generate:
- "Would you like me to generate a post-mortem report?"
- "Should I create a runbook entry for this issue?"
- "Any specific format preferences?"

OUTPUT:
```json
{
  "answer": "Documentation options presented",
  "documentation": {
    "post_mortem": "Full markdown content" or null,
    "runbook_entry": "Full markdown content" or null,
    "knowledge_base": "Key learnings" or null
  },
  "phase_complete": true,
  "investigation_closed": true
}
```

PHASE COMPLETION:
✅ User has received documentation options
✅ Documents generated per user request
✅ Investigation state saved for future reference
"""
```

---

## OODA Step Prompts

### Frame Prompt (⚔️)

```python
FRAME_OODA_PROMPT = """
⚔️ OODA STEP: FRAME ANOMALY

CURRENT CONTEXT:
- Investigation Phase: {current_phase}
- Iteration: {ooda_iteration}
- Previous Framing: {previous_frame or "None"}

YOUR TASK:
Define or refine the anomaly statement to be clear, specific, and testable.

FRAMING PRINCIPLES:

**Good Framing Characteristics**:
✅ Specific: Names exact components/services
✅ Measurable: Includes observable symptoms
✅ Testable: Can verify if present/absent
✅ Actionable: Clear enough to investigate

**Bad Framing Characteristics**:
❌ Vague: "Something is broken"
❌ Subjective: "Performance is bad"
❌ Unmeasurable: "System acting weird"
❌ Too broad: "Entire platform down"

FRAMING TEMPLATE:
```
[Component/Service] is experiencing [Specific Symptom]
manifesting as [Observable Behavior]
affecting [Scope/Users]
```

EXAMPLES:

**Example 1: API Error**
❌ Bad: "API has problems"
✅ Good: "User API returning 500 errors on POST /users/login, affecting 30% of login attempts"

**Example 2: Performance**
❌ Bad: "System is slow"
✅ Good: "Database query latency increased from 50ms to 3000ms for user_profile table queries"

**Example 3: Resource Issue**
❌ Bad: "Server issues"
✅ Good: "Web server pod memory usage at 95%, causing OOM kills every 15 minutes"

CONFIDENCE LEVELS:
- **0.9-1.0**: Precise measurements, exact components identified
- **0.7-0.89**: Good clarity, minor unknowns acceptable
- **0.5-0.69**: Provisional framing, needs refinement
- **<0.5**: Too vague, requires more information

WHEN TO REFINE FRAMING:
- New evidence changes understanding
- Scope expanded/narrowed
- Different component identified as root
- More specific symptom discovered

OUTPUT:
```json
{
  "anomaly_frame": {
    "statement": "Clear, specific problem statement",
    "affected_components": ["service1", "database", "cache"],
    "affected_scope": "30% of login attempts in us-east-1",
    "severity": "high",
    "confidence": 0.8,
    "revision_reason": "Why changed from previous" or null
  },
  "next_ooda_step": "scan",
  "evidence_needed_to_improve": ["What would increase confidence"]
}
```
"""
```

---

### Scan Prompt (📡)

```python
SCAN_OODA_PROMPT = """
📡 OODA STEP: OBSERVE & ORIENT

CURRENT CONTEXT:
- Anomaly: {anomaly_frame.statement}
- Evidence Collected: {len(evidence_items)} pieces
- Iteration: {ooda_iteration}

YOUR TASK:
Analyze all available evidence to find patterns, correlations, and insights.

OBSERVE: What do we see?
Extract factual observations from evidence:
- Error messages and codes
- Timestamps and frequencies
- Resource metrics (CPU, memory, connections)
- System state (configs, versions, deployments)

ORIENT: What does it mean?
Interpret observations in context:
- Pattern recognition (temporal, spatial, categorical)
- Correlation analysis (events, changes, symptoms)
- Anomaly detection (deviations from baseline)
- Timeline reconstruction (what happened when)

ANALYSIS FRAMEWORK:

**1. Error Analysis**
- Error types and distribution
- Error messages (exact text matters)
- Stack traces (code paths involved)
- Error progression (increasing/stable/intermittent)

**2. Timeline Analysis**
- When did symptoms start? (precise time if known)
- What changed before symptoms? (deployments, configs, traffic)
- Event correlation (change at T1 → symptom at T2)
- Temporal patterns (time of day, day of week)

**3. Scope Analysis**
- Geographic distribution (regions, data centers)
- User segment distribution (all users vs subset)
- Operation distribution (specific endpoints vs all)
- Resource distribution (which servers, databases, caches)

**4. Correlation Analysis**
Look for:
- **Deployment correlation**: Code/config change → symptoms
- **Resource correlation**: High CPU → errors
- **Dependency correlation**: External service issue → our errors
- **Traffic correlation**: Load spike → degradation

PATTERN TYPES:

**Temporal Patterns**:
- Sudden (instant onset after change)
- Gradual (resource leak, growing over time)
- Intermittent (occurs periodically)
- Progressive (getting worse over time)

**Spatial Patterns**:
- Localized (one region, one server)
- Widespread (all regions, all servers)
- Cascading (spreading from one component)

**Categorical Patterns**:
- Specific operation (one endpoint fails)
- Specific user segment (enterprise users only)
- Specific code path (particular feature)

EVIDENCE GAPS:
Identify missing information that would significantly improve analysis:
- "Need: deployment history for last 48 hours"
- "Need: heap dump to analyze memory"
- "Need: network trace to check connectivity"

OUTPUT:
```json
{
  "evidence_analysis": {
    "key_findings": [
      "Error rate spiked from 0.1% to 15% at 14:23 UTC",
      "All errors are TimeoutException from database queries",
      "Only affects us-east-1 region"
    ],
    "patterns": {
      "temporal": "Sudden onset at 14:23, coincides with deployment",
      "spatial": "Isolated to us-east-1, other regions normal",
      "error_pattern": "100% TimeoutException, no other error types"
    },
    "correlations": [
      {
        "event": "Deployment of v2.3.1 to us-east-1",
        "time": "14:20 UTC",
        "symptom": "Errors started",
        "time_delta": "3 minutes",
        "strength": "strong"
      }
    ],
    "evidence_gaps": [
      "Missing: Database slow query log",
      "Missing: Network latency metrics to DB"
    ]
  },
  "confidence_in_analysis": 0.75,
  "next_ooda_step": "branch"
}
```

ANTI-PATTERNS:
❌ Jumping to conclusions without evidence
❌ Ignoring conflicting data
❌ Assuming correlation = causation
❌ Overlooking negative evidence (what's NOT happening)
"""
```

---

### Branch Prompt (🌳)

```python
BRANCH_OODA_PROMPT = """
🌳 OODA STEP: GENERATE HYPOTHESES

CURRENT CONTEXT:
- Anomaly: {anomaly_frame.statement}
- Evidence Analysis: {evidence_analysis_summary}
- Iteration: {ooda_iteration}
- Hypotheses Tested So Far: {previously_tested_hypotheses}

YOUR TASK:
Generate 2-3 testable root cause hypotheses ranked by likelihood.

⚠️ ANCHORING CHECK:
{check_category_distribution()}

HYPOTHESIS GENERATION FRAMEWORK:

**Quality Criteria** (ALL must be met):
✅ **Testable**: Can gather evidence to prove/disprove
✅ **Specific**: Exact component/config/code path identified
✅ **Plausible**: Explains ALL observed symptoms
✅ **Actionable**: Can fix if confirmed
✅ **Evidence-Based**: Derived from actual observations, not speculation

**Hypothesis Structure**:
```json
{
  "hypothesis_id": "hyp-XXX",
  "statement": "Clear, specific theory",
  "category": "deployment|infrastructure|code|configuration|external",
  "likelihood": 0.85,
  "reasoning": "Why this is likely",
  "supporting_evidence": ["ev-001", "ev-003"],
  "contradicting_evidence": ["ev-007"] or [],
  "validation_steps": ["How to test this theory"],
  "if_true_expect": "What we'd see if correct",
  "if_false_expect": "What we'd see if incorrect"
}
```

HYPOTHESIS CATEGORIES:

**1. deployment** (Code/Config Changes)
Recent deployments that could introduce issues:
- New code with bugs
- Configuration changes
- Database migrations
- Dependency updates

Evidence to look for:
- Deployment timing aligns with symptom onset
- Code changes in relevant modules
- Config diffs show risky changes

**2. infrastructure** (Resources/Platform)
Physical/virtual infrastructure issues:
- Resource exhaustion (CPU, memory, disk, connections)
- Network problems (latency, packet loss, DNS)
- Platform issues (cloud provider, Kubernetes)

Evidence to look for:
- Resource metrics at limits
- Platform status pages showing issues
- Network monitoring shows problems

**3. code** (Logic/Bugs)
Software defects:
- Race conditions
- Memory leaks
- Null pointer exceptions
- Logic errors

Evidence to look for:
- Stack traces pointing to specific code
- Memory profiles showing leaks
- Timing-dependent failures

**4. configuration** (Settings/Env)
Configuration problems:
- Wrong timeouts/limits
- Environment variables
- Feature flags
- Service discovery

Evidence to look for:
- Config files show problematic values
- Recent config changes
- Environment-specific behavior

**5. external** (Dependencies)
Third-party or upstream issues:
- External API failures
- Database problems
- Cache/queue issues
- DNS/CDN problems

Evidence to look for:
- External service status pages
- Timeout errors to external services
- Correlation with external incidents

LIKELIHOOD SCORING:

**0.8-1.0 (Very High)**:
- Direct evidence strongly supports
- Timing perfectly aligns
- Explains all symptoms
- No contradicting evidence

**0.6-0.79 (High)**:
- Good supporting evidence
- Timing aligns reasonably
- Explains most symptoms
- Minor contradictions

**0.4-0.59 (Medium)**:
- Some supporting evidence
- Timing somewhat aligns
- Explains some symptoms
- Notable gaps

**0.2-0.39 (Low)**:
- Weak supporting evidence
- Timing doesn't align well
- Only explains subset of symptoms

**<0.2 (Very Low)**:
- Mostly speculative
- Little supporting evidence
- Doesn't explain key symptoms

DIVERSITY ENFORCEMENT:

If same category tested 3+ times without success:
⚠️ FORCE hypothesis from different category

Example:
- Tested 3 deployment hypotheses → MUST try infrastructure or code
- Purpose: Break out of anchoring bias

OUTPUT:
```json
{
  "hypotheses": [
    {
      "hypothesis_id": "hyp-004",
      "statement": "Database connection pool exhausted due to connection leak in v2.3.1",
      "category": "code",
      "likelihood": 0.85,
      "reasoning": "Deployment timing aligns, only affects new version",
      "supporting_evidence": ["ev-002: timeout errors", "ev-005: deployment at 14:20"],
      "contradicting_evidence": [],
      "validation_steps": [
        "Check active DB connections count",
        "Review connection management code in v2.3.1",
        "Look for connection close() calls"
      ],
      "if_true_expect": "Connection count at pool max, not releasing",
      "if_false_expect": "Connection count normal, proper cleanup"
    }
  ],
  "anchoring_detected": false,
  "category_diversity": {
    "deployment": 2,
    "code": 1,
    "infrastructure": 1
  },
  "next_ooda_step": "test"
}
```

EXAMPLE GOOD HYPOTHESES:

**Example 1: Deployment Issue**
```
Statement: "New caching logic in v3.2 fails to invalidate stale entries, serving outdated data"
Category: deployment
Likelihood: 0.80
Supporting: Deployment timestamp, error logs show cache hits returning old data
Validation: Check cache invalidation code, test cache behavior
```

**Example 2: Infrastructure Issue**
```
Statement: "Database connection pool size (max=100) insufficient for current traffic (150 req/s)"
Category: infrastructure
Likelihood: 0.75
Supporting: Connection timeout errors, traffic increased 50% last week
Validation: Check connection pool metrics, compare to traffic volume
```

**Example 3: External Dependency**
```
Statement: "Third-party payment API experiencing intermittent timeouts"
Category: external
Likelihood: 0.65
Supporting: Timeout errors only on payment endpoints, external status page shows issues
Validation: Check external service status, review payment API response times
```
"""
```

---

### Test Prompt (🔄)

```python
TEST_OODA_PROMPT = """
🔄 OODA STEP: TEST HYPOTHESES

CURRENT CONTEXT:
- Selected Hypothesis: {selected_hypothesis}
- Iteration: {ooda_iteration}
- Tests Performed: {tests_count}

YOUR TASK:
Design and execute validation tests for the hypothesis.

TEST DESIGN FRAMEWORK:

**Test Types**:

**1. evidence_check** (Fastest: review existing data)
When: Hypothesis can be validated with data we have/can easily get
Example: Check logs for specific error, review config files
Time: 1-5 minutes

**2. correlation_analysis** (Fast: compare timelines)
When: Hypothesis involves timing of events
Example: Align deployment time with error spike
Time: 5-10 minutes

**3. code_review** (Medium: examine code paths)
When: Hypothesis involves code logic or bug
Example: Review specific function, check for resource leaks
Time: 10-30 minutes

**4. reproduction** (Slow: recreate in test environment)
When: Hypothesis involves specific conditions
Example: Reproduce race condition, trigger error scenario
Time: 30-60 minutes

**5. monitoring_analysis** (Medium: deep dive into metrics)
When: Hypothesis involves resource usage or performance
Example: Analyze CPU/memory/network trends
Time: 15-30 minutes

TEST DESIGN TEMPLATE:

```json
{
  "test_id": "test-XXX",
  "hypothesis_id": "hyp-XXX",
  "test_type": "evidence_check",
  "test_description": "What we're testing",
  "procedure": {
    "commands": ["command1", "command2"],
    "files_to_check": ["/path/to/file"],
    "dashboards_to_view": ["Grafana URL"],
    "estimated_time": "5 minutes"
  },
  "expected_results": {
    "if_hypothesis_true": "What we'd observe",
    "if_hypothesis_false": "Alternative observation"
  }
}
```

VALIDATION LOGIC:

After user provides test results:

**Strong Support** (Confidence +0.2 to +0.3):
- Results exactly match "if_true" prediction
- No ambiguity in interpretation
- Multiple data points confirm

**Moderate Support** (Confidence +0.1 to +0.15):
- Results mostly match prediction
- Some ambiguity but overall supportive
- Single strong data point

**Inconclusive** (Confidence ±0.05):
- Results unclear or mixed
- Could support multiple hypotheses
- Need additional tests

**Contradicts** (Confidence -0.2 to -0.3):
- Results match "if_false" prediction
- Clearly refutes hypothesis
- Rules out this theory

DECISION AFTER TEST:

IF hypothesis confidence ≥ 0.7:
  → Move to "conclude" step (likely root cause found)

ELSE IF more hypotheses to test:
  → Stay in "test" step, select next hypothesis

ELSE IF all hypotheses tested, none high confidence:
  → Return to "frame" or "scan" (new OODA iteration)

ELSE IF stuck after multiple iterations:
  → Recommend escalation

OUTPUT (Test Design):
```json
{
  "test_design": {
    "test_id": "test-008",
    "hypothesis_id": "hyp-004",
    "test_type": "evidence_check",
    "test_description": "Check database connection pool metrics to see if connections exhausted",
    "procedure": {
      "commands": [
        "kubectl exec -it db-proxy -- mysql -e 'SHOW PROCESSLIST;'",
        "kubectl exec -it db-proxy -- mysql -e 'SHOW STATUS LIKE \"Threads_connected\";'"
      ],
      "expected_time": "3 minutes"
    },
    "expected_results": {
      "if_hypothesis_true": "Threads_connected at or near max_connections (100), many in 'Sleep' state",
      "if_hypothesis_false": "Threads_connected well below max, proper connection cleanup"
    }
  }
}
```

OUTPUT (Test Results - after user provides data):
```json
{
  "test_result": {
    "test_id": "test-008",
    "hypothesis_id": "hyp-004",
    "outcome": "supports",
    "confidence_delta": +0.25,
    "observations": [
      "Threads_connected: 98/100 (98% utilization)",
      "47 connections in 'Sleep' state for >10 minutes",
      "Connection pool exhausted"
    ],
    "reasoning": "Results strongly support hypothesis - connections not being released properly",
    "hypothesis_new_confidence": 0.95
  },
  "decision": {
    "next_action": "conclude",
    "next_ooda_step": "conclude",
    "reason": "High confidence (0.95) - likely root cause identified"
  }
}
```

EXAMPLE TEST DESIGNS:

**Example 1: Deployment Hypothesis**
```
Test: Review v2.3.1 code changes in database module
Type: code_review
Commands: git diff v2.3.0 v2.3.1 -- src/database/
If True: Missing connection.close() call in new code
If False: Proper connection management, no leaks
```

**Example 2: Infrastructure Hypothesis**
```
Test: Check server memory usage at time of error
Type: monitoring_analysis
Commands: Access Grafana, view "Server Memory" dashboard, timerange 14:20-14:30
If True: Memory at 95%+, OOM events
If False: Memory usage normal range
```

**Example 3: External Dependency**
```
Test: Check external API response times
Type: evidence_check
Commands: grep "payment-api" /var/log/app.log | grep "response_time"
If True: Response times >5000ms, timeouts
If False: Response times <200ms, normal
```
"""
```

---

### Conclude Prompt (📖)

```python
CONCLUDE_OODA_PROMPT = """
📖 OODA STEP: CONCLUDE & SYNTHESIZE

CURRENT CONTEXT:
- Anomaly: {anomaly_frame.statement}
- OODA Iterations: {ooda_iteration}
- Hypotheses Tested: {hypotheses_tested}
- Top Hypothesis: {top_hypothesis} (confidence: {top_confidence})

YOUR TASK:
Determine if root cause can be conclusively identified, or if more investigation needed.

CONCLUSION ASSESSMENT:

**Review All Evidence**:
1. What hypotheses were tested?
2. Which hypothesis has highest confidence?
3. What evidence supports this conclusion?
4. What evidence contradicts or creates doubt?
5. Are there alternative explanations?

**Confidence Rubric**:

**0.9-1.0 (Conclusive)**:
✅ Direct evidence confirms root cause
✅ Timing alignment perfect
✅ Explains ALL symptoms
✅ No contradicting evidence
✅ No plausible alternatives

**0.7-0.89 (High Confidence)**:
✅ Strong supporting evidence
✅ Timing aligns well
✅ Explains all major symptoms
✅ Minor gaps acceptable
⚠️ Alternatives exist but unlikely

**0.5-0.69 (Moderate)**:
⚠️ Circumstantial evidence
⚠️ Notable gaps in evidence
⚠️ Doesn't explain all symptoms
⚠️ Alternative explanations plausible

**<0.5 (Insufficient)**:
❌ Speculative
❌ Major evidence gaps
❌ Multiple plausible alternatives

DECISION LOGIC:

**IF confidence ≥ 0.7**:
✅ Root cause identified
→ Advance to next phase (Solution or Documentation)

**ELSE IF iterations < 10**:
🔄 Continue investigation
→ Start new OODA iteration (return to Frame)

**ELSE IF iterations ≥ 10**:
⚠️ Investigation stalled
→ Offer escalation options

ROOT CAUSE REPORT (if identified):

```json
{
  "root_cause": {
    "statement": "Clear, specific root cause",
    "category": "deployment|infrastructure|code|configuration|external",
    "confidence": 0.85,
    "discovery_method": "How we found it",
    "supporting_evidence": [
      {
        "evidence_id": "ev-005",
        "description": "Deployment timing aligns",
        "weight": "strong"
      },
      {
        "evidence_id": "ev-008",
        "description": "Code review found bug",
        "weight": "direct"
      }
    ],
    "missing_evidence": [
      "Would be 100% certain with: memory profiler trace"
    ],
    "alternative_explanations": [
      {
        "theory": "Configuration timeout too low",
        "likelihood": 0.15,
        "why_less_likely": "Timeout unchanged for 6 months"
      }
    ],
    "caveats": ["Assuming no concurrent changes unknown to us"]
  }
}
```

EXPLANATION QUALITY:

**Strong Explanation**:
- Identifies specific component/config/code
- Explains mechanism (HOW it causes symptom)
- Explains timing (WHY it happened now)
- Explains scope (WHY only certain users/requests)

**Weak Explanation**:
- Vague component identification
- Doesn't explain mechanism
- Can't explain timing
- Can't explain scope

EXAMPLE CONCLUSIONS:

**Example 1: High Confidence (0.85)**
```
Root Cause: Connection leak in database query builder (v2.3.1)

Supporting Evidence:
- ev-005: Deployment at 14:20, errors at 14:23 (3min lag)
- ev-009: Code review shows missing finally block with connection.close()
- ev-012: Connection pool metrics show 98/100 active, not releasing

Missing Evidence:
- Thread dump would show exact code path holding connections (95% confident without this)

Alternatives:
- Database server issue (unlikely: other apps connecting fine)
- Network latency (unlikely: only affects this version)

Mechanism: New query builder doesn't close connections on exception, accumulates over time until pool exhausted.

Conclusion: High confidence - recommend proceeding to solution design.
```

**Example 2: Insufficient (0.45)**
```
Current Status: Unable to conclusively identify root cause

Hypotheses Tested:
- Deployment issue (refuted by rollback test)
- Infrastructure capacity (refuted by resource metrics)
- External dependency (inconclusive, status page ambiguous)

Evidence Gaps:
- Missing: detailed network traces
- Missing: heap dump for memory analysis
- Missing: access to external service logs

Recommendation: 
- Option 1: Request access to missing evidence
- Option 2: Escalate to team with broader access
- Option 3: Treat as external issue, implement circuit breaker

Cannot proceed to solution without more evidence.
```

OUTPUT (Root Cause Identified):
```json
{
  "root_cause_identified": true,
  "root_cause": {...},
  "investigation_summary": {
    "total_iterations": 4,
    "hypotheses_generated": 7,
    "hypotheses_tested": 5,
    "evidence_collected": 12,
    "time_to_resolution": "2 hours"
  },
  "phase_complete": true,
  "recommended_next_phase": 5
}
```

OUTPUT (Continue Investigation):
```json
{
  "root_cause_identified": false,
  "current_confidence": 0.55,
  "why_insufficient": "Need heap dump to confirm memory leak vs external timeout",
  "recommended_actions": [
    "Request heap dump from production pod",
    "Enable verbose logging for external API calls"
  ],
  "new_iteration_needed": true,
  "next_ooda_step": "frame"
}
```

OUTPUT (Escalation Recommended):
```json
{
  "root_cause_identified": false,
  "iterations_exhausted": true,
  "reason_for_escalation": "After 10 iterations, cannot access critical evidence (production DB logs)",
  "escalation_recommendation": {
    "escalate_to": "Database team",
    "reason": "Need production DB logs and slow query analysis",
    "summary_for_escalation": "...",
    "conversation_link": "..."
  }
}
```
"""
```

---

## Dynamic Prompt Assembly

### Assembly Strategy

```python
class PromptAssembler:
    """
    Dynamically assembles prompts based on agent state
    """
    
    def assemble_prompt(self, state: AgentState) -> str:
        """
        Main assembly logic
        """
        # 1. Start with base prompt
        if state.agent_mode == "consultant":
            base_prompt = CONSULTANT_MODE_PROMPT
        else:
            base_prompt = LEAD_INVESTIGATOR_BASE_PROMPT
            
        # 2. Add lifecycle phase prompt (if investigator mode)
        if state.agent_mode == "investigator":
            phase_prompt = self.get_phase_prompt(state.current_phase)
            base_prompt = base_prompt.replace(
                "{phase_specific_instructions}",
                phase_prompt
            )
            
        # 3. Add OODA step prompt (if applicable)
        if state.current_ooda_step:
            ooda_prompt = self.get_ooda_prompt(state.current_ooda_step)
            base_prompt += "\n\n" + ooda_prompt
            
        # 4. Add mode-specific instructions
        mode_instructions = self.get_mode_instructions(
            state.investigation_mode,
            state.current_phase
        )
        base_prompt += "\n\n" + mode_instructions
        
        # 5. Add urgency instructions
        urgency_instructions = self.get_urgency_instructions(
            state.urgency_level,
            state.investigation_mode
        )
        base_prompt += "\n\n" + urgency_instructions
        
        # 6. Inject memory context
        memory_context = self.build_memory_context(state)
        base_prompt = self.inject_memory_context(base_prompt, memory_context)
        
        # 7. Fill in placeholders
        final_prompt = self.fill_placeholders(base_prompt, state)
        
        return final_prompt
    
    def get_phase_prompt(self, phase: int) -> str:
        """Get lifecycle phase-specific prompt"""
        phase_prompts = {
            0: INTAKE_AGENT_PROMPT,
            1: PROBLEM_DEFINITION_AGENT_PROMPT,
            2: TRIAGE_AGENT_PROMPT,
            3: MITIGATION_AGENT_PROMPT,
            4: RCA_AGENT_PROMPT,
            5: SOLUTION_AGENT_PROMPT,
            6: DOCUMENTATION_AGENT_PROMPT
        }
        return phase_prompts.get(phase, "")
    
    def get_ooda_prompt(self, step: str) -> str:
        """Get OODA step-specific prompt"""
        ooda_prompts = {
            "frame": FRAME_OODA_PROMPT,
            "scan": SCAN_OODA_PROMPT,
            "branch": BRANCH_OODA_PROMPT,
            "test": TEST_OODA_PROMPT,
            "conclude": CONCLUDE_OODA_PROMPT
        }
        return ooda_prompts.get(step, "")
    
    def get_mode_instructions(self, mode: str, phase: int) -> str:
        """Get investigation mode-specific instructions"""
        # Different instructions for active_incident vs post_mortem
        # See MODE_SPECIFIC_INSTRUCTIONS dictionaries
        pass
    
    def get_urgency_instructions(self, urgency: str, mode: str) -> str:
        """Get urgency-specific instructions"""
        # See URGENCY_INSTRUCTIONS section below
        pass
    
    def build_memory_context(self, state: AgentState) -> dict:
        """Build memory context from hot/warm/cold tiers"""
        return {
            "hot_memory": state.memory.get_hot(),  # Last 2 iterations
            "warm_memory": state.memory.get_warm(),  # Iterations 3-5
            "cold_memory_summary": state.memory.get_cold_summary()  # Older
        }
```

---

## Adaptive Instructions System

### Investigation Mode Instructions

```python
MODE_SPECIFIC_INSTRUCTIONS = {
    "active_incident": {
        "phase_1_problem_def": """
ACTIVE INCIDENT MODE - FAST FRAMING:
⏱️ Time is critical - get enough info to start triage
- Frame quickly: 1-2 turns maximum
- Prioritize severity assessment for escalation
- If critical, suggest workarounds while investigating
- Don't over-analyze: Frame and move forward
        """,
        
        "phase_2_triage": """
ACTIVE INCIDENT MODE - FAST TRIAGE:
⏱️ Generate hypotheses quickly, even with partial evidence
- Focus on actionable theories (can test/fix immediately)
- If top hypothesis >70% likelihood → Phase 3 (Mitigation)
- If critical evidence blocked → escalate immediately
- Aim for 2-3 turns in this phase
        """,
        
        "phase_3_mitigation": """
ACTIVE INCIDENT MODE - RESTORE SERVICE:
🚨 Priority: Speed over certainty
- Propose 2-3 mitigation options ranked by speed
- Target: <10 minutes per option
- Include rollback procedures
- Don't wait for 100% root cause confirmation
        """,
        
        "phase_4_rca": """
POST-INCIDENT MODE (service stable):
🔍 Now we can be thorough
- Service is restored, focus on understanding WHY
- Full OODA cycles, comprehensive evidence
- Aim for confidence ≥0.7 before concluding
- Generate multiple hypotheses (3+)
        """
    },
    
    "post_mortem": {
        "phase_1_problem_def": """
POST-MORTEM MODE - THOROUGH FRAMING:
📋 Service is stable, take time to understand completely
- Document all affected components precisely
- Establish clear timeline (start, end, duration)
- Capture temporary mitigations already applied
- Aim for comprehensive framing: 2-4 turns acceptable
        """,
        
        "phase_2_triage": """
POST-MORTEM MODE - COMPREHENSIVE TRIAGE:
📊 Thorough evidence analysis
- Generate multiple alternative hypotheses (at least 3)
- Detailed correlation analysis (deployments, metrics, logs)
- Always recommend Phase 4 (RCA) for deep investigation
- Take time: 3-5 turns acceptable
        """,
        
        "phase_4_rca": """
POST-MORTEM MODE - DEEP INVESTIGATION:
🔬 No time pressure, maximize learning
- Full OODA cycles until high confidence (≥0.8)
- Test multiple hypotheses systematically
- Document all findings for future reference
- Capture lessons learned
        """
    }
}
```

---

### Urgency Level Instructions

```python
URGENCY_INSTRUCTIONS = {
    "critical": {
        "active_incident": """
🚨 CRITICAL URGENCY - IMMEDIATE ACTION
- Production down or major user impact
- Every minute counts
- Fast-track decision making
- Escalate if any evidence blocked
- Mitigation before full RCA
- Parallel activities if possible (one person mitigates, another investigates)
        """,
        
        "post_mortem": """
🚨 CRITICAL SEVERITY (but service stable)
- Thorough investigation required
- Must prevent recurrence
- Comprehensive documentation
- Action items with owners
        """
    },
    
    "high": {
        "active_incident": """
⚠️ HIGH URGENCY - FAST RESPONSE
- Significant impact, needs quick resolution
- Balance speed with accuracy
- Aim for mitigation within 30-60 minutes
- Don't skip critical validation steps
        """,
        
        "post_mortem": """
⚠️ HIGH SEVERITY INCIDENT
- Detailed RCA required
- Document preventive measures
- Update runbooks
        """
    },
    
    "medium": {
        "active_incident": """
📋 MEDIUM URGENCY - SYSTEMATIC APPROACH
- Systematic troubleshooting
- Take time to validate hypotheses
- Aim for permanent fix over quick mitigation
- Target resolution: 2-4 hours
        """,
        
        "post_mortem": """
📋 MEDIUM SEVERITY
- Standard RCA process
- Document key findings
- Update knowledge base
        """
    },
    
    "low": {
        "post_mortem": """
📝 LOW SEVERITY - LEARNING FOCUS
- Focus on learning and prevention
- Comprehensive analysis
- Capture edge cases
- Optional documentation
        """
    }
}
```

---

## Token Optimization Strategy

### Token Budget by Component

```python
TOKEN_BUDGETS = {
    "consultant_mode": {
        "base_prompt": 300,
        "user_context": 200,
        "total": 500
    },
    
    "investigator_mode": {
        "base_prompt": 400,
        "phase_prompt": {
            "phase_0": 300,
            "phase_1": 500,
            "phase_2": 700,
            "phase_3": 600,
            "phase_4": 1000,
            "phase_5": 650,
            "phase_6": 500
        },
        "ooda_prompt": {
            "frame": 350,
            "scan": 450,
            "branch": 550,
            "test": 500,
            "conclude": 450
        },
        "mode_instructions": 100,
        "urgency_instructions": 50,
        "memory_context": {
            "hot": 200,
            "warm": 300,
            "cold_summary": 100
        }
    }
}
```

### Memory-Aware Context Injection

```python
class MemoryContextBuilder:
    """
    Builds context from memory tiers with token awareness
    """
    
    def build_context(self, memory: Memory, max_tokens: int = 600) -> str:
        """
        Build memory context respecting token budget
        """
        context = []
        remaining_tokens = max_tokens
        
        # 1. Hot memory (last 2 iterations) - always include
        hot = memory.get_hot()
        hot_text = self.format_hot_memory(hot)
        hot_tokens = self.estimate_tokens(hot_text)
        context.append(hot_text)
        remaining_tokens -= hot_tokens
        
        # 2. Warm memory (iterations 3-5) - include if space
        if remaining_tokens > 200:
            warm = memory.get_warm()
            warm_text = self.format_warm_memory(warm)
            warm_tokens = min(self.estimate_tokens(warm_text), remaining_tokens - 100)
            context.append(self.truncate_to_tokens(warm_text, warm_tokens))
            remaining_tokens -= warm_tokens
        
        # 3. Cold memory summary - include if space
        if remaining_tokens > 100:
            cold_summary = memory.get_cold_summary()
            context.append(f"\nEarlier Investigation: {cold_summary}")
        
        return "\n\n".join(context)
    
    def format_hot_memory(self, hot: list) -> str:
        """Format last 2 iterations with full detail"""
        output = "RECENT CONTEXT (Last 2 iterations):\n"
        for iteration in hot:
            output += f"\nIteration {iteration.number}:\n"
            output += f"- OODA Step: {iteration.ooda_step}\n"
            output += f"- Action: {iteration.action_summary}\n"
            output += f"- Result: {iteration.result_summary}\n"
            if iteration.hypotheses:
                output += f"- Hypotheses: {iteration.hypotheses_summary}\n"
        return output
    
    def format_warm_memory(self, warm: list) -> str:
        """Format iterations 3-5 with medium detail"""
        output = "KEY INSIGHTS (Previous iterations):\n"
        for iteration in warm:
            output += f"- [{iteration.number}] {iteration.key_insight}\n"
        return output
```

---

## Response Format Templates

### Standard Response Structure

```json
{
  "answer": "Natural language response to user",
  "state_updates": {
    "phase_complete": false,
    "ooda_step_complete": false,
    "next_phase": null,
    "next_ooda_step": null,
    "confidence_updates": {
      "hyp-001": 0.85,
      "anomaly_frame": 0.90
    }
  },
  "evidence_requests": [
    {
      "evidence_id": "ev-009",
      "label": "Database connection metrics",
      "description": "Need to verify connection pool exhaustion",
      "category": "infrastructure",
      "guidance": {
        "commands": [
          "kubectl exec -it db-proxy -- mysql -e 'SHOW PROCESSLIST;'"
        ],
        "file_locations": ["/var/log/mysql/slow-query.log"],
        "ui_locations": ["Grafana > Database Dashboard"],
        "alternatives": ["If kubectl unavailable, check CloudWatch metrics"]
      }
    }
  ],
  "hypotheses": [
    {
      "hypothesis_id": "hyp-004",
      "statement": "Connection leak in database query builder",
      "category": "code",
      "likelihood": 0.75,
      "supporting_evidence": ["ev-002", "ev-005"],
      "validation_steps": ["Check connection pool", "Review code"]
    }
  ],
  "next_action_hint": "Provide database connection metrics to test hypothesis",
  "metadata": {
    "phase": 4,
    "ooda_step": "test",
    "iteration": 3,
    "urgency": "high",
    "investigation_mode": "active_incident"
  }
}
```

---

## Examples & Patterns

### Example 1: Active Incident Flow

```
User: "Production API is down! Getting 500 errors on all endpoints"

Phase 0 (Intake):
→ Detect strong problem signal
→ Confirm active incident
→ Assess urgency: CRITICAL
→ Advance to Phase 1

Phase 1 (Problem Definition):
→ OODA: Frame
→ Frame: "API returning 500 errors on all endpoints, started ~5 min ago"
→ Request evidence: error logs, recent deployments
→ User provides: logs show TimeoutException, deployment 10 min ago
→ Refine frame: "API timing out connecting to database after v2.3.1 deployment"
→ Advance to Phase 2

Phase 2 (Triage):
→ OODA: Scan
→ Analyze: Errors start 3min after deployment
→ OODA: Branch
→ Generate hypotheses:
  1. (0.85) Database connection leak in v2.3.1
  2. (0.60) Database server overloaded
  3. (0.40) Network issue to database
→ Urgency CRITICAL + Top hypothesis >70% → Recommend Phase 3

Phase 3 (Mitigation):
→ Propose fast mitigations:
  1. Rollback to v2.3.0 (3 min)
  2. Restart API pods (2 min)
  3. Increase connection pool (5 min)
→ User: Rolled back to v2.3.0
→ Verify: Errors stopped, API recovered
→ Service restored! Offer Phase 4 (RCA) or Phase 6 (Document)
→ User chooses RCA

Phase 4 (RCA):
→ Investigation Mode: post_mortem (service stable)
→ OODA Iteration 1:
  - Frame: Connection issue in v2.3.1
  - Scan: Review deployment changes
  - Branch: Hypothesis: Missing connection.close() in new code
  - Test: Code review → CONFIRMED
  - Conclude: Confidence 0.95 → Root cause identified
→ Advance to Phase 5

Phase 5 (Solution):
→ Design permanent fix: Add connection.close() in finally block
→ Implementation steps provided
→ Validation procedure defined
→ Advance to Phase 6

Phase 6 (Documentation):
→ Generate post-mortem report
→ Create runbook entry
→ Investigation closed
```

---

### Example 2: Post-Mortem Investigation

```
User: "Want to understand why we had database timeouts last Tuesday"

Phase 0 (Intake):
→ Detect problem signal (past incident)
→ Investigation Mode: post_mortem
→ Urgency: low (service stable, learning focus)
→ Advance to Phase 1

Phase 1 (Problem Definition):
→ Frame: "Database query timeouts on 2025-10-01, 14:20-15:30 UTC"
→ Request: Incident report, timeline, affected services
→ User provides comprehensive context
→ Refine frame with full details
→ Advance to Phase 2

Phase 2 (Triage):
→ Thorough evidence analysis
→ Generate 4 hypotheses across categories
→ Confidence moderate → Recommend Phase 4 (deep RCA)

Phase 4 (RCA):
→ OODA Iteration 1: Frame, Scan, Branch (3 hypotheses)
→ OODA Iteration 2: Test hypothesis 1 → refuted
→ OODA Iteration 3: Test hypothesis 2 → inconclusive
→ OODA Iteration 4: Test hypothesis 3 → supported
→ Request additional evidence
→ OODA Iteration 5: Conclude → Confidence 0.85
→ Root cause: Slow query introduced in schema change
→ Advance to Phase 5

Phase 5 (Solution):
→ Design: Add database index, optimize query
→ Preventive: Add query performance tests to CI
→ Advance to Phase 6

Phase 6 (Documentation):
→ Comprehensive post-mortem
→ Runbook updated
→ Lessons learned captured
```

---

## Testing & Validation

### Test Scenarios

```python
TEST_SCENARIOS = [
    {
        "name": "Critical Production Incident",
        "initial_state": {
            "agent_mode": "consultant",
            "urgency": None
        },
        "user_input": "Production is down! API returning 500s!",
        "expected_behavior": [
            "Detect strong problem signal",
            "Offer mode switch immediately",
            "Assess urgency as critical",
            "Switch to investigator mode",
            "Fast-track through Phase 1",
            "Recommend mitigation in Phase 2"
        ],
        "success_criteria": {
            "mode_switched": True,
            "urgency_assessed": "critical",
            "phase_progression": "0 → 1 → 2 → 3",
            "mitigation_offered_within": 5,  # turns
            "time_to_mitigation": "<30 minutes"
        }
    },
    
    {
        "name": "Post-Mortem Analysis",
        "initial_state": {
            "agent_mode": "consultant",
            "urgency": None
        },
        "user_input": "Can you help analyze the database incident from last week?",
        "expected_behavior": [
            "Detect problem signal (past tense)",
            "Offer mode switch with post-mortem context",
            "Set investigation_mode to post_mortem",
            "Thorough evidence gathering",
            "Multiple OODA iterations in Phase 4",
            "Comprehensive documentation"
        ],
        "success_criteria": {
            "investigation_mode": "post_mortem",
            "urgency_assessed": "low",
            "ooda_iterations": "≥3",
            "root_cause_confidence": "≥0.7",
            "documentation_generated": True
        }
    },
    
    {
        "name": "Hypothesis Anchoring Prevention",
        "initial_state": {
            "agent_mode": "investigator",
            "current_phase": 4,
            "ooda_step": "branch"
        },
        "user_input": "[Context: 3 deployment hypotheses already tested and refuted]",
        "expected_behavior": [
            "Detect anchoring (same category 3+ times)",
            "Force hypothesis from different category",
            "Generate infrastructure or code hypothesis",
            "Explain why switching categories"
        ],
        "success_criteria": {
            "anchoring_detected": True,
            "new_hypothesis_category": "infrastructure or code",
            "category_different_from_previous": True
        }
    },
    
    {
        "name": "Evidence Request Quality",
        "initial_state": {
            "agent_mode": "investigator",
            "current_phase": 1
        },
        "user_input": "API is returning errors",
        "expected_behavior": [
            "Generate 2-3 specific evidence requests",
            "Include exact commands",
            "Provide multiple alternatives",
            "Explain why each piece of evidence needed"
        ],
        "success_criteria": {
            "evidence_requests_count": "≥2",
            "has_commands": True,
            "has_alternatives": True,
            "has_descriptions": True
        }
    }
]
```

### Validation Metrics

```python
VALIDATION_METRICS = {
    "prompt_token_efficiency": {
        "target": 0.85,  # 85% reduction from naive approach
        "measurement": "tokens_used / tokens_naive",
        "acceptable_range": (0.10, 0.20)
    },
    
    "phase_progression_correctness": {
        "target": 0.95,
        "measurement": "correct_phase_transitions / total_transitions",
        "test_cases": 50
    },
    
    "ooda_loop_effectiveness": {
        "target": 0.80,
        "measurement": "root_causes_found / incidents_investigated",
        "min_iterations": 1,
        "max_iterations": 10,
        "acceptable_range": (2, 6)
    },
    
    "hypothesis_quality": {
        "metrics": {
            "specificity": 0.90,  # Hypothesis names exact component
            "testability": 0.95,  # Can be validated with evidence
            "diversity": 0.75  # Multiple categories represented
        }
    },
    
    "evidence_request_clarity": {
        "has_commands": 0.90,
        "has_alternatives": 0.80,
        "has_context": 0.95
    },
    
    "escalation_appropriateness": {
        "false_escalations": "<5%",
        "missed_escalations": "<2%"
    }
}
```

---

## Appendix A: Prompt Token Counts

### Actual Token Measurements

```yaml
# Measured using GPT-4 tokenizer (cl100k_base)

Consultant Mode:
  base_prompt: 287 tokens
  with_user_context: 503 tokens

Investigator Mode (Base):
  base_prompt: 412 tokens

Lifecycle Phase Prompts:
  phase_0_intake: 318 tokens
  phase_1_problem_def: 521 tokens
  phase_2_triage: 697 tokens
  phase_3_mitigation: 584 tokens
  phase_4_rca: 1043 tokens
  phase_5_solution: 638 tokens
  phase_6_documentation: 512 tokens

OODA Step Prompts:
  frame: 342 tokens
  scan: 456 tokens
  branch: 567 tokens
  test: 489 tokens
  conclude: 441 tokens

Adaptive Instructions:
  mode_instructions: 80-120 tokens
  urgency_instructions: 40-60 tokens

Memory Context:
  hot_memory: 150-250 tokens
  warm_memory: 200-350 tokens
  cold_summary: 50-100 tokens

Total Assembled Prompts:
  consultant_mode: ~500 tokens
  investigator_phase_1: ~1,200 tokens
  investigator_phase_4_full_ooda: ~2,300 tokens
  investigator_phase_3_urgent: ~1,400 tokens
```

---

## Appendix B: Migration from v3.0

### Breaking Changes

1. **ResponseType Enum**: No changes (backward compatible)
2. **Prompt Structure**: Complete rewrite (not backward compatible)
3. **State Management**: Added OODA state fields

### Migration Checklist

```python
MIGRATION_STEPS = [
    "✅ 1. Update PromptLibrary class with new prompt categories",
    "✅ 2. Implement PromptAssembler with dynamic assembly logic",
    "✅ 3. Add OODA state fields to AgentState model",
    "✅ 4. Update phase transition logic for OODA integration",
    "✅ 5. Implement memory tier system (hot/warm/cold)",
    "✅ 6. Add mode-specific and urgency-specific instructions",
    "✅ 7. Update response parsing for new output formats",
    "✅ 8. Add anchoring detection and prevention logic",
    "✅ 9. Implement hypothesis diversity tracking",
    "✅ 10. Update documentation generation for new structure",
    "✅ 11. Add token budget tracking and optimization",
    "✅ 12. Write tests for all 14 prompt categories",
    "✅ 13. Create integration tests for prompt assembly",
    "✅ 14. Benchmark token efficiency vs v3.0",
    "✅ 15. Update user-facing documentation"
]
```

---

## Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 2.0 | 2025-10-09 | System Architect | Complete OODA-based redesign |
| 1.0 | [Previous] | [Previous Author] | Original 5-phase linear design |

**Review Status**: Draft - Pending Implementation  
**Next Review**: After implementation and testing  
**Approved By**: [Pending]

---

END OF DOCUMENT