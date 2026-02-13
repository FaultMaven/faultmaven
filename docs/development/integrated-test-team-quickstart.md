# Integrated Test Team - Quick Start

Use the `/test-team` command to run coordinated testing with realistic user simulation and iterative improvement.

## Quick Invocation

```bash
/test-team Test critical API outage scenario with MITIGATION_FIRST path
```

Or use the full version:

```bash
/integrated-test-team [your test scenario]
```

## What You Get

A coordinated testing team where:

- **YOU** = Project Coordinator & QA Lead
- **sre-agent** = Real SRE user who interacts with the app
- **solutions-architect** = Fixes API server bugs
- **ai-specialist** = Improves investigation process and prompts
- **test-engineer** = Adds tests for bugs found
- **tech-writer** = Updates documentation

## Simple Workflow

1. **Define** a test scenario (critical outage, performance issue, etc.)
2. **Launch** sre-agent to act as real user
3. **Review** API responses with sre-agent
4. **Identify** issues (bugs, poor AI responses, wrong transitions, etc.)
5. **Coordinate** specialists to fix issues
6. **Re-test** same scenario to validate fixes
7. **Iterate** until all success criteria met

## Example Scenarios

### Scenario 1: Critical Production Outage

```bash
/test-team

Test Scenario: Critical Production API Outage

**Objective**: Validate MITIGATION_FIRST path with urgent production issue

**Setup**:
- Urgency: CRITICAL
- Problem: Complete API outage (all 500 errors)
- Expected Path: MITIGATION_FIRST

**Expected Behavior**:
1. AI should quickly ask about recent changes/deploys
2. AI should prioritize mitigation (rollback) over deep RCA
3. Status: INQUIRY → INVESTIGATING after problem confirmed
4. Milestones: symptom_verified, mitigation_applied, then root_cause
5. Resolution: After mitigation confirmed and proper fix applied

**Success Criteria**:
- AI prioritizes mitigation over understanding (urgency-aware)
- AI suggests rollback before deep investigation
- Status transitions correctly
- Milestones follow MITIGATION_FIRST order
- User successfully mitigates then gets proper fix
```

### Scenario 2: Performance Investigation

```bash
/test-team

Test Scenario: API Latency Spike Investigation

**Objective**: Validate ROOT_CAUSE path with performance issue

**Setup**:
- Urgency: MEDIUM
- Problem: API latency spiked from 200ms to 8 seconds
- Expected Path: ROOT_CAUSE

**Expected Behavior**:
1. AI should ask about metrics, correlations, recent changes
2. AI should systematically diagnose root cause
3. Status: INQUIRY → INVESTIGATING after problem scoped
4. Milestones: symptom_verified, root_cause_identified, solution_applied
5. Resolution: After permanent fix applied and validated

**Success Criteria**:
- AI asks diagnostic questions (metrics, timeline, changes)
- Investigation is systematic and thorough
- Root cause identified before solution
- Proper fix applied (not just restart/workaround)
- Metrics confirm issue resolved
```

### Scenario 3: Post-Mortem Analysis

```bash
/test-team

Test Scenario: Post-Mortem for Last Week's Outage

**Objective**: Validate ROOT_CAUSE path with historical issue

**Setup**:
- Urgency: LOW
- Problem: 2-hour outage last Tuesday (already fixed via restart)
- Expected Path: ROOT_CAUSE

**Expected Behavior**:
1. AI should focus on understanding what happened
2. AI should ask for logs, metrics, timeline
3. Systematic root cause analysis
4. Proper fix recommendation (not just "restart pods")
5. Prevention strategies suggested

**Success Criteria**:
- Thorough investigation despite low urgency
- Root cause identified from historical data
- Permanent fix recommended
- Prevention strategies provided
```

## What to Evaluate

When reviewing API responses:

### ✅ Good Responses

- Asks relevant questions that help diagnose
- Builds on previous answers (contextual)
- Matches urgency (quick for critical, thorough for low)
- References knowledge base when appropriate
- Transitions status at right times
- Tracks milestones correctly

### ❌ Issues to Fix

- Asks redundant questions (context not maintained)
- Generic questions not specific to symptoms
- Wrong urgency handling (treats critical like low)
- Doesn't use knowledge base when should
- Status stuck or transitions too early
- Milestones missing or in wrong order

## Common Issues & Fixes

### Issue: Status Stuck in INQUIRY

**You observe**: User confirmed problem but status didn't transition to INVESTIGATING

**Action**: Check server logs → Instruct solutions-architect to fix milestone engine

### Issue: AI Asks Redundant Questions

**You observe**: User says "I already told you X"

**Action**: Instruct ai-specialist to improve context management in prompts

### Issue: Wrong Investigation Path

**You observe**: CRITICAL urgency but AI does ROOT_CAUSE instead of MITIGATION_FIRST

**Action**: Instruct ai-specialist to improve urgency detection and path selection

### Issue: Knowledge Base Not Used

**You observe**: Relevant KB articles exist but AI doesn't reference them

**Action**: Instruct ai-specialist to improve RAG search and retrieval

### Issue: API Errors

**You observe**: sre-agent gets 500 errors instead of AI responses

**Action**: Check logs → Instruct solutions-architect to fix the bug → Instruct test-engineer to add regression test

## Tips for Success

1. **Be specific** in test scenarios - define expected behavior clearly
2. **Let sre-agent be natural** - they act as real user, not QA tester
3. **Review holistically** - question quality, UX, technical correctness, all matter
4. **Iterate persistently** - don't settle for "good enough", iterate until excellent
5. **Document findings** - track what worked, what didn't, what was fixed

## Full Documentation

For complete details, see:

- [Integrated Test Team Full Command](../../.claude/commands/integrated-test-team.md)
- [Quality Orchestrator Guide](quality-orchestrator-guide.md)
- [SRE Agent Documentation](../../.claude/agents/sre-agent.md)
- [Investigation Lifecycle Logic](../architecture/investigation-engine/investigation-lifecycle-logic.md)

## Quick Reference

| Command | Use Case |
|---------|----------|
| `/test-team Critical outage scenario` | Test MITIGATION_FIRST path |
| `/test-team Performance investigation` | Test ROOT_CAUSE path |
| `/test-team Post-mortem analysis` | Test historical investigation |
| `/test-team [custom scenario]` | Test specific workflow |

Start with one of the example scenarios above, then create your own based on what you need to validate!
