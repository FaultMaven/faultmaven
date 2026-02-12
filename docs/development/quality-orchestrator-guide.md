# Quality Orchestrator Guide

This guide explains how to use the **quality-orchestrator** agent to systematically test and improve FaultMaven through coordinated multi-agent workflows.

## Overview

The quality-orchestrator is a meta-agent that coordinates other specialized agents to:

- **Test** the application from multiple perspectives (user, API, security, performance)
- **Identify** quality gaps and systemic issues
- **Coordinate** fixes through appropriate specialists
- **Validate** improvements iteratively
- **Report** comprehensive quality metrics

## Quick Start

### Basic Usage

Invoke the quality-orchestrator via slash command:

```bash
/quality-orchestrator [your request]
```

Or programmatically from main Claude:

```python
Task(
    subagent_type='quality-orchestrator',
    prompt='Your detailed testing request'
)
```

## Common Use Cases

### 1. Pre-Release Validation

Validate the application is ready for release:

```bash
/quality-orchestrator Validate the application is ready for v1.5 release
```

**What happens:**

1. **Parallel Testing Launch**:
   - `sre-agent`: 3-5 realistic user scenarios (critical outage, performance issue, post-mortem)
   - `qa-test-agent`: Comprehensive API validation (auth, cases, agent, knowledge, evidence)
   - `security-auditor`: Security audit of critical paths (auth, file upload, API access)

2. **Analysis & Aggregation**:
   - Collects all findings
   - Categorizes by severity (Critical/High/Medium/Low)
   - Identifies patterns and root causes
   - Calculates quality metrics

3. **Improvement Coordination**:
   - Routes critical issues to `solutions-architect`
   - Routes LLM/RAG issues to `ai-specialist`
   - Routes missing tests to `test-engineer`

4. **Validation**:
   - Re-runs failed tests after fixes
   - Verifies no regressions introduced
   - Confirms quality gates pass

5. **Reporting**:
   - Generates comprehensive quality report
   - Provides release readiness decision (✅ Ready | ⚠️ Needs Attention | ❌ Block)

### 2. Feature Validation

Test a new feature end-to-end:

```bash
/quality-orchestrator Test the new milestone-based investigation lifecycle comprehensively
```

**What happens:**

1. **User Testing** (`sre-agent`):
   - Simulates realistic troubleshooting scenarios
   - Tests milestone transitions (INQUIRY → INVESTIGATING → RESOLVED)
   - Validates both MITIGATION_FIRST and ROOT_CAUSE paths

2. **API Validation** (`qa-test-agent`):
   - Tests milestone tracking endpoints
   - Validates status transition logic
   - Checks progress indicators and edge cases

3. **Analysis**:
   - Identifies workflow gaps
   - Validates AI behavior quality
   - Checks milestone completion logic

4. **Improvements**:
   - Routes AI quality issues to `ai-specialist`
   - Routes API bugs to `test-engineer`
   - Routes architectural issues to `solutions-architect`

### 3. Security Hardening

Comprehensively harden application security:

```bash
/quality-orchestrator Harden security across the entire application
```

**What happens:**

1. **Security Audit** (`security-auditor`):
   - Auth/authz review across all modules
   - Input validation audit (SQL injection, XSS, path traversal)
   - File upload security
   - LLM prompt injection testing

2. **API Security Testing** (`qa-test-agent`):
   - IDOR (Insecure Direct Object Reference) testing
   - Cross-organization data leakage tests
   - Rate limiting validation
   - Error message information disclosure

3. **User-Level Testing** (`sre-agent`):
   - Attempts to access other users' cases
   - Tests malicious file uploads
   - Tests prompt injection in AI interactions

4. **Aggregation & Fixing**:
   - Routes all critical security issues to `solutions-architect`
   - Routes prompt injection to `ai-specialist`
   - Routes infrastructure issues to `devops-engineer`

### 4. Continuous Quality Improvement

Focus on systematic improvement of specific modules:

```bash
/quality-orchestrator Run continuous quality testing for the case management module
```

**What happens:**

1. **Deep Testing** of focus area:
   - `sre-agent`: User scenarios specific to case management
   - `qa-test-agent`: Comprehensive case API testing
   - `test-engineer`: Coverage analysis

2. **Identify Top Issues**:
   - Rank by severity and user impact
   - Focus on top 3-5 issues

3. **Coordinate Fixes**:
   - Assign to appropriate specialists
   - Track progress

4. **Validate & Iterate**:
   - Re-test after fixes
   - Move to next module

## Orchestration Patterns

The quality-orchestrator uses different patterns based on the request:

### Pattern: Comprehensive Audit

**Used for**: Release validation, major feature launches

**Agents coordinated**:

- `sre-agent` (user perspective)
- `qa-test-agent` (API validation)
- `security-auditor` (security review)
- `test-engineer` (coverage analysis)

**Output**: Full quality report with release decision

### Pattern: Feature Validation

**Used for**: New feature testing, major changes

**Agents coordinated**:

- `sre-agent` (user workflows)
- `qa-test-agent` (API endpoints)
- `security-auditor` (security implications)

**Output**: Feature readiness report

### Pattern: Security Hardening

**Used for**: Security-focused improvements

**Agents coordinated**:

- `security-auditor` (vulnerability scanning)
- `qa-test-agent` (security testing)
- `sre-agent` (user-level security testing)

**Output**: Security audit report with vulnerability list

### Pattern: Continuous Improvement

**Used for**: Ongoing quality improvement

**Agents coordinated**:

- `sre-agent` (focus area testing)
- `qa-test-agent` (targeted validation)
- Specialists as needed for fixes

**Output**: Improvement cycle report

## Quality Metrics

The orchestrator tracks and reports on:

### Test Coverage

- **Current**: Measured via pytest coverage
- **Target**: 71% baseline, 80%+ for critical modules
- **Action**: `test-engineer` adds missing tests

### API Quality

- **Success Rate**: >99% for happy path scenarios
- **Error Handling**: Proper status codes and messages
- **Performance**: <200ms read, <500ms write, <2s AI

### User Experience

- **Workflow Completeness**: All journeys complete successfully
- **Investigation Lifecycle**: Proper milestone transitions
- **AI Quality**: Relevant, helpful responses

### Security

- **Auth Coverage**: 100% of protected endpoints require JWT
- **Data Isolation**: Zero cross-organization leakage
- **Input Validation**: No injection vulnerabilities

## Example Workflows

### Workflow 1: Weekly Quality Check

Run weekly quality validation:

```bash
# Week 1: Case Management Module
/quality-orchestrator Deep quality testing for case management module with user workflows and API validation

# Week 2: Knowledge Base Module
/quality-orchestrator Deep quality testing for knowledge base module including RAG quality and search performance

# Week 3: Agent/AI Module
/quality-orchestrator Deep quality testing for AI agent module including prompt quality and LLM behavior

# Week 4: Auth & Security
/quality-orchestrator Security audit of authentication, authorization, and data isolation
```

### Workflow 2: Pre-Release Checklist

Before each release:

```bash
# Step 1: Comprehensive validation
/quality-orchestrator Validate the application is ready for release with full testing across all modules

# Step 2: Review report and fix critical/high issues
[Review quality report from orchestrator]
[Let orchestrator coordinate fixes via specialists]

# Step 3: Regression validation
/quality-orchestrator Re-test all previously failed scenarios to validate fixes

# Step 4: Final approval
[Review final report and make release decision]
```

### Workflow 3: Feature Launch

After implementing a new feature:

```bash
# Step 1: Feature validation
/quality-orchestrator Validate the new [feature name] feature end-to-end with user testing and API validation

# Step 2: Security review
/quality-orchestrator Security review of the new [feature name] feature

# Step 3: Performance testing
/quality-orchestrator Performance testing of [feature name] under load

# Step 4: Documentation validation
/quality-orchestrator Validate documentation is complete and accurate for [feature name]
```

## Understanding Reports

The quality-orchestrator generates structured reports:

### Executive Summary

```markdown
## Executive Summary
- **Tests Executed**: 47
- **Issues Found**: 12 (Critical: 1, High: 3, Medium: 5, Low: 3)
- **Quality Score**: 87% (based on metrics)
- **Status**: ⚠️ Needs Attention
```

**Status meanings**:

- ✅ **Release Ready**: No critical/high issues, all quality gates pass
- ⚠️ **Needs Attention**: Some high priority issues, should fix before release
- ❌ **Block Release**: Critical issues present, must fix before release

### Testing Activities

Summary of what each agent tested:

```markdown
### User Experience Testing (sre-agent)
- **Scenarios Tested**: 5 (critical outage, performance issue, post-mortem, etc.)
- **Findings**: Investigation lifecycle works but AI sometimes asks redundant questions
- **Key Issues**: Milestone transition delays, unclear error messages

### API Validation (qa-test-agent)
- **Endpoints Tested**: 23
- **Test Pass Rate**: 91%
- **Findings**: Case creation works but evidence upload has validation bug
- **Key Issues**: Missing field validation, slow response times
```

### Priority Issues

Issues organized by severity:

```markdown
### Critical (Block Release)
1. **Auth bypass in case access endpoint**
   - Category: Security
   - Impact: Users can access other organizations' cases
   - Agent: security-auditor
   - Status: Assigned to solutions-architect

### High (Should Fix)
2. **Evidence upload validation missing**
   - Category: Functional
   - Impact: Malicious files can be uploaded
   - Agent: qa-test-agent
   - Status: Assigned to test-engineer
```

### Quality Metrics

Current vs target metrics:

```markdown
| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| Test Coverage | 73% | 80% | 🟡 Needs Improvement |
| API Success Rate | 98% | 99% | 🟡 Needs Improvement |
| Security Issues | 2 | 0 | 🔴 Action Required |
```

### Improvement Actions

Clear next steps:

```markdown
### Immediate (This Sprint)
- [ ] Fix critical auth bypass (solutions-architect)
- [ ] Add evidence upload validation (test-engineer)
- [ ] Improve AI question quality (ai-specialist)

### Planned (Next Sprint)
- [ ] Improve error handling in knowledge module
- [ ] Add integration tests for agent execution
```

## Best Practices

### When to Use Quality Orchestrator

**Do use for**:

- Pre-release validation (always)
- New feature validation (recommended)
- Security audits (quarterly or after security changes)
- Quality improvement cycles (weekly/bi-weekly)
- After major refactoring or architectural changes

**Don't use for**:

- Single-agent tasks (use specific agent directly)
- Simple bug fixes (use `test-engineer` directly)
- Documentation-only changes (use `tech-writer` directly)

### How to Get Best Results

1. **Be specific in requests**: "Validate investigation lifecycle" is better than "test the app"
2. **Provide context**: Mention what changed, what you're worried about, release timeline
3. **Review reports thoroughly**: The orchestrator provides detailed findings - read them
4. **Iterate**: First run identifies issues, second run validates fixes
5. **Track metrics over time**: Compare quality scores across releases

### Coordinating with Specialists

The orchestrator routes issues but doesn't fix them directly:

- **Critical/High architectural issues** → `solutions-architect`
- **LLM/RAG/prompt issues** → `ai-specialist`
- **Missing tests/coverage** → `test-engineer`
- **Security vulnerabilities** → `security-auditor`
- **Documentation gaps** → `tech-writer`
- **Infrastructure issues** → `devops-engineer`

## Advanced Usage

### Custom Testing Scenarios

Provide detailed scenarios:

```bash
/quality-orchestrator Test the following scenarios:
1. Critical production outage with MITIGATION_FIRST path
2. Historical post-mortem with ROOT_CAUSE path
3. Medium urgency issue where user chooses path
4. Edge case: User changes mind mid-investigation
5. Performance: 5 concurrent investigations
```

### Targeted Module Testing

Focus on specific modules:

```bash
/quality-orchestrator Deep testing of the knowledge base module including:
- Document upload and chunking quality
- Semantic search accuracy
- RAG context quality
- Performance under load (1000+ documents)
- Security (malicious file uploads)
```

### Integration Testing

Test cross-module workflows:

```bash
/quality-orchestrator Test the complete workflow:
1. User registers and logs in (auth module)
2. Uploads knowledge base documents (knowledge module)
3. Creates troubleshooting case (case module)
4. AI investigates referencing knowledge base (agent + knowledge modules)
5. User uploads evidence (evidence module)
6. Investigation reaches resolution (agent module)
Validate data flows correctly between all modules
```

## Troubleshooting

### Issue: Too many findings to fix

**Solution**: Focus on critical/high first, defer medium/low to backlog

```bash
/quality-orchestrator Re-run testing focusing only on critical and high priority areas
```

### Issue: Tests passing but user experience poor

**Solution**: Add more `sre-agent` scenarios

```bash
/quality-orchestrator Run 10 diverse sre-agent scenarios covering different urgency levels and problem types
```

### Issue: Coverage not improving

**Solution**: Target specific modules

```bash
/quality-orchestrator Identify coverage gaps in [module name] and coordinate with test-engineer to add tests
```

## Integration with Development Workflow

### Sprint Planning

- Review previous orchestrator reports
- Identify quality debt to address
- Schedule orchestrator runs (weekly)

### Pull Request Reviews

- Run targeted validation for changed modules
- Ensure no regressions in affected areas

### Release Process

- Mandatory orchestrator validation before release
- Block releases with critical/high issues
- Track quality metrics across releases

## Conclusion

The quality-orchestrator is your **systematic quality improvement driver**. Use it regularly to:

- Maintain high quality standards
- Catch issues before production
- Coordinate improvements across teams
- Track progress over time

Start with pre-release validation, then add continuous improvement cycles to your workflow.

## Related Documentation

- [Agent Principles](../../.claude/commands/agent-principles.md)
- [Testing Standards](../../.claude/standards/TESTING_STANDARDS.md)
- [SRE Agent Guide](../../.claude/agents/sre-agent.md)
- [QA Test Agent Guide](../../.claude/agents/qa-test-agent.md)
- [Investigation Lifecycle Logic](../architecture/investigation-engine/investigation-lifecycle-logic.md)
