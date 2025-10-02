"""
Phase-Specific Prompts for FaultMaven Five-Phase Doctrine

This module contains prompt templates for each phase of the SRE troubleshooting methodology.
These prompts guide the agent through structured troubleshooting phases.
"""

from typing import Dict, Optional

# Phase 1: Define Blast Radius
PHASE_1_BLAST_RADIUS = """## Current Phase: 1️⃣ Define Blast Radius

**Objective:** Understand the scope and impact of the issue before diving into details.

**Your Focus:**
- Identify which systems/services are affected
- Assess user impact (how many users, which operations)
- Determine severity level (critical/high/medium/low)
- Gather initial symptoms and observations
- Check for recent changes (deployments, configs, infrastructure)

**Key Questions to Ask:**
1. What specific symptoms are you observing? (errors, slowness, downtime)
2. Which services or components are affected?
3. Is this impacting all users or a subset?
4. When did you first notice this issue?
5. Have there been any recent changes or deployments?

**Output Format:**
```
## Blast Radius Assessment

**Affected Systems:**
- [List of affected services/components]

**Impact:**
- Severity: [Critical/High/Medium/Low]
- User Impact: [All users / Subset / Internal only]
- Business Impact: [Revenue impact, SLA breach, etc.]

**Timeline:**
- First Noticed: [Timestamp or timeframe]
- Duration: [How long has this been happening]

**Recent Changes:**
- [Any deployments, config changes, infrastructure updates]
```

**Transition:** Once blast radius is clear, move to Phase 2 (Establish Timeline)."""


# Phase 2: Establish Timeline
PHASE_2_TIMELINE = """## Current Phase: 2️⃣ Establish Timeline

**Objective:** Create a chronological timeline of events to identify correlation and causation.

**Your Focus:**
- Pinpoint exact incident start time
- Identify last known good state
- Correlate with system events (deployments, traffic changes, infrastructure)
- Look for patterns (time of day, specific operations, load-related)
- Map symptom progression

**Key Questions to Ask:**
1. What is the exact timestamp when the issue started?
2. When was the system last known to be healthy?
3. What events occurred around that time? (Check deployment history, monitoring alerts)
4. Are there any patterns? (Happens at specific times, during specific operations)
5. Has the issue gotten worse, better, or stayed the same?

**Data to Gather:**
- Deployment logs (what was deployed and when)
- Infrastructure change logs (scaling events, configuration updates)
- Monitoring alerts history
- Traffic patterns (request rate, user load)
- Related incidents or issues

**Output Format:**
```
## Timeline of Events

**T-2h:** [Last known good state]
**T-1h:** [Events leading up to incident]
**T-0:** [Issue first observed - exact timestamp]
**T+30m:** [Symptom progression or additional observations]
**Current:** [Current state]

**Correlated Events:**
- [Deployment at T-15m: service-api v2.3.0]
- [Traffic spike at T+10m: +300% request rate]
- [Alert fired at T+5m: High error rate]

**Patterns Identified:**
- [e.g., Errors only occur during peak traffic]
- [e.g., Issue started immediately after deployment]
```

**Transition:** Once timeline is established, move to Phase 3 (Formulate Hypothesis)."""


# Phase 3: Formulate Hypothesis
PHASE_3_HYPOTHESIS = """## Current Phase: 3️⃣ Formulate Hypothesis

**Objective:** Generate ranked hypotheses for root cause based on evidence gathered.

**Your Focus:**
- Synthesize information from Phases 1 and 2
- Generate multiple possible root causes
- Rank by likelihood (most probable first)
- Provide supporting evidence for each hypothesis
- Identify quick tests to validate/invalidate

**Hypothesis Framework:**
For each hypothesis, provide:
1. **What:** Clear statement of potential root cause
2. **Why:** Supporting evidence (from symptoms, timeline, changes)
3. **Test:** How to quickly validate or rule out
4. **Likelihood:** High/Medium/Low based on evidence

**Common Root Cause Categories:**
- **Code Issues:** Bugs, memory leaks, logic errors in new deployment
- **Configuration:** Incorrect settings, environment variables, feature flags
- **Infrastructure:** Resource exhaustion, network issues, hardware failures
- **Dependencies:** Upstream/downstream service failures, database issues
- **Capacity:** Traffic spike exceeding capacity, resource limits hit
- **Data:** Corrupt data, unexpected data format, database migrations

**Output Format:**
```
## Hypotheses (Ranked by Likelihood)

### 🔴 Most Likely: [Hypothesis Name]
**What:** [Clear description of potential cause]
**Supporting Evidence:**
- [Evidence from Phase 1: e.g., errors started after deployment]
- [Evidence from Phase 2: e.g., timing coincides with release]
- [Pattern: e.g., similar to past incident #123]

**How to Test:**
```bash
[Specific command or check]
```
**Expected Result if Confirmed:** [What you'd see]

---

### 🟡 Possible: [Hypothesis Name]
**What:** [Description]
**Supporting Evidence:**
- [Evidence 1]
- [Evidence 2]

**How to Test:**
```bash
[Command or check]
```
**Expected Result if Confirmed:** [What you'd see]

---

### 🟢 Less Likely: [Hypothesis Name]
**What:** [Description]
**Supporting Evidence:**
- [Weak evidence or edge case scenario]

**How to Test:**
```bash
[Command or check]
```
**Expected Result if Confirmed:** [What you'd see]
```

**Transition:** Once hypotheses are ranked, move to Phase 4 (Validate Hypothesis) starting with the most likely."""


# Phase 4: Validate Hypothesis
PHASE_4_VALIDATION = """## Current Phase: 4️⃣ Validate Hypothesis

**Objective:** Systematically test hypotheses using evidence to confirm or rule out root cause.

**Your Focus:**
- Test most likely hypothesis first
- Guide user to collect relevant evidence
- Analyze evidence objectively
- Conclude: CONFIRMED, RULED OUT, or INCONCLUSIVE
- Move to next hypothesis if current one is ruled out

**Validation Methods:**
1. **Logs Analysis**
   - Application logs, error logs, system logs
   - Look for specific error messages, stack traces, patterns

2. **Metrics Review**
   - CPU, memory, network, disk utilization
   - Request rates, error rates, latency percentiles
   - Resource quotas and limits

3. **Configuration Verification**
   - Environment variables, config files
   - Feature flags, database connection strings
   - Compare current with last known good configuration

4. **Dependency Checks**
   - Upstream service health
   - Database connectivity and performance
   - External API availability

5. **Code Review**
   - Recent code changes
   - Diff between working and broken versions
   - Known bugs or issues in changelog

**Validation Process Template:**
```
## Validating: [Hypothesis Name]

### Step 1: [Data Collection Action]
**Command:**
```bash
[Exact command to run]
```

**What This Tells Us:**
[Explanation of what to look for in output]

**User: Please share the output above**

---

### Step 2: [Analysis Action]
**Based on your output:**
[Analysis of what the data shows]

**Conclusion for Step 2:**
- ✅ Supports hypothesis if: [Condition]
- ❌ Rules out hypothesis if: [Condition]
- ⚠️ Inconclusive if: [Condition]

---

### Step 3: [Confirmation Action]
**Final verification:**
```bash
[Command for final confirmation]
```

## Validation Result

**Status:** [CONFIRMED ✅ / RULED OUT ❌ / INCONCLUSIVE ⚠️]

**Evidence Summary:**
- [Key finding 1]
- [Key finding 2]
- [Key finding 3]

**Next Action:**
- If CONFIRMED: Proceed to Phase 5 (Propose Solution)
- If RULED OUT: Test next hypothesis from Phase 3
- If INCONCLUSIVE: Gather additional data or try different test
```

**Transition:** Once root cause is confirmed, move to Phase 5 (Propose Solution)."""


# Phase 5: Propose Solution
PHASE_5_SOLUTION = """## Current Phase: 5️⃣ Propose Solution

**Objective:** Provide actionable resolution with clear steps, verification, and prevention strategies.

**Your Focus:**
- Immediate fix to restore service (if down)
- Root cause resolution
- Step-by-step instructions with exact commands
- Verification steps to confirm fix
- Prevention strategies to avoid recurrence

**Solution Structure:**
1. **Immediate Fix** (Stop the Bleeding) - if service is down
2. **Root Cause Resolution** (Permanent Fix)
3. **Verification** (Confirm it worked)
4. **Rollback Plan** (If fix doesn't work)
5. **Prevention** (Avoid future occurrences)

**Output Format:**
```
## Resolution Plan

### 🚨 Immediate Fix (Restore Service)
**Objective:** Get the service back online quickly

**Steps:**
1. [Action 1 with exact command]
   ```bash
   [command]
   ```
   **Why:** [Explanation of what this does]

2. [Action 2]
   ```bash
   [command]
   ```
   **Why:** [Explanation]

**Expected Result:** Service restored within [timeframe]

---

### 🔧 Root Cause Resolution (Permanent Fix)
**Objective:** Fix the underlying issue

**Steps:**
1. [Action 1]
   ```bash
   [command]
   ```
   **Why:** [Explanation]
   **Risk Level:** [Low/Medium/High]

2. [Action 2]
   ```bash
   [command]
   ```
   **Why:** [Explanation]
   **Risk Level:** [Low/Medium/High]

**Timeline:** [Estimated time to complete]

---

### ✅ Verification Steps
**Confirm the fix worked:**

1. **Check service health:**
   ```bash
   [command to verify service is healthy]
   ```
   **Expected:** [What "healthy" looks like]

2. **Monitor metrics for [duration]:**
   - [Metric 1]: Should be [expected value/range]
   - [Metric 2]: Should be [expected value/range]

3. **Test functionality:**
   ```bash
   [command to test actual functionality]
   ```
   **Expected:** [Successful output]

**Sign-off Criteria:**
- [ ] Service responding to requests
- [ ] Error rate below [threshold]
- [ ] No alerts firing
- [ ] Metrics within normal range

---

### ⏪ Rollback Plan (If Fix Doesn't Work)
**If the fix causes issues or doesn't resolve the problem:**

**Steps:**
1. [Rollback action 1]
   ```bash
   [command]
   ```

2. [Rollback action 2]
   ```bash
   [command]
   ```

**When to Rollback:**
- If error rate increases
- If new errors appear
- If metrics degrade further

---

### 🛡️ Prevention (Avoid Future Occurrences)

**Short-term (Do Today):**
1. [Action 1] - [Why this prevents recurrence]
2. [Action 2] - [Why this prevents recurrence]

**Medium-term (This Week):**
1. [Action 1] - [Improvement to processes/systems]
2. [Action 2] - [Improvement to monitoring/alerting]

**Long-term (This Month):**
1. [Action 1] - [Architectural or systemic improvement]
2. [Action 2] - [Documentation or training]

**Monitoring & Alerts:**
- Add alert for: [Metric/condition] threshold: [value]
- Create dashboard for: [Key metrics to watch]
- Set up automated test for: [Scenario that caused issue]

---

## Post-Incident Follow-up

**Documentation:**
- [ ] Update runbook with this incident and resolution
- [ ] Document root cause in incident log
- [ ] Share lessons learned with team

**Questions to Answer:**
- What went well in this incident response?
- What could have been detected earlier?
- What processes should change?
```

**Completion:** Issue is resolved, documented, and preventive measures are in place."""


# Phase transition prompts
PHASE_TRANSITIONS = {
    "1_to_2": "Blast radius is now clear. Let's establish a detailed timeline of events (Phase 2).",
    "2_to_3": "Timeline is established. Now let's formulate hypotheses for the root cause (Phase 3).",
    "3_to_4": "Hypotheses are ranked. Let's validate them systematically starting with the most likely (Phase 4).",
    "4_to_5": "Root cause is confirmed. Now let's propose a comprehensive solution (Phase 5).",
    "5_complete": "Resolution complete! Issue resolved with preventive measures in place."
}


# Phase-specific prompt registry
PHASE_PROMPTS: Dict[int, str] = {
    1: PHASE_1_BLAST_RADIUS,
    2: PHASE_2_TIMELINE,
    3: PHASE_3_HYPOTHESIS,
    4: PHASE_4_VALIDATION,
    5: PHASE_5_SOLUTION,
}


def get_phase_prompt(phase: int, context: Optional[str] = None) -> str:
    """
    Get phase-specific prompt for current troubleshooting phase.

    Args:
        phase: Phase number (1-5)
        context: Optional additional context about current troubleshooting state

    Returns:
        str: Phase-specific prompt

    Examples:
        >>> get_phase_prompt(1)  # Returns PHASE_1_BLAST_RADIUS
        >>> get_phase_prompt(3, "User mentioned recent deployment")
    """
    if phase not in PHASE_PROMPTS:
        phase = 1  # Default to phase 1

    prompt = PHASE_PROMPTS[phase]

    if context:
        prompt = f"{prompt}\n\n## Current Context\n\n{context}"

    return prompt


def get_phase_transition(from_phase: int, to_phase: int) -> str:
    """
    Get transition message when moving between phases.

    Args:
        from_phase: Current phase number
        to_phase: Next phase number

    Returns:
        str: Transition message

    Examples:
        >>> get_phase_transition(1, 2)
        "Blast radius is now clear. Let's establish a detailed timeline..."
    """
    transition_key = f"{from_phase}_to_{to_phase}"

    if to_phase == 6:  # Completion
        return PHASE_TRANSITIONS.get("5_complete", "Resolution complete!")

    return PHASE_TRANSITIONS.get(
        transition_key,
        f"Moving from Phase {from_phase} to Phase {to_phase}"
    )


def get_phase_summary() -> str:
    """
    Get summary of all five phases for reference.

    Returns:
        str: Summary of five-phase doctrine
    """
    return """
## Five-Phase SRE Troubleshooting Doctrine

1️⃣ **Define Blast Radius** - Understand scope and impact
   - What's affected? How many users? Severity?
   - Recent changes? When did it start?

2️⃣ **Establish Timeline** - Create event chronology
   - Exact start time? Last known good state?
   - Correlated events? Patterns?

3️⃣ **Formulate Hypothesis** - Generate possible causes
   - Rank by likelihood (most probable first)
   - Supporting evidence for each
   - Quick tests to validate

4️⃣ **Validate Hypothesis** - Test systematically
   - Collect evidence (logs, metrics, configs)
   - Analyze objectively
   - Confirm, rule out, or gather more data

5️⃣ **Propose Solution** - Actionable resolution
   - Immediate fix (restore service)
   - Root cause resolution
   - Verification steps
   - Prevention strategies
"""
