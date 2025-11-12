"""Phase 3 Structured Output Template (v3.0)

JSON schema template for hypothesis generation with required_evidence arrays.
Each hypothesis MUST include 2-5 evidence items with acquisition guidance.

Design Reference:
- docs/architecture/prompt-engineering-architecture.md (v3.0 Section 2.4)
"""


def get_phase3_structured_output_template() -> str:
    """Get structured output template for Phase 3 hypothesis generation

    Returns:
        JSON schema template with examples
    """
    return """# Phase 3: Hypothesis Generation - Structured Output Required

## Output Format (v3.0 REQUIRED)

You MUST generate hypotheses using this exact JSON structure:

```json
{
  "hypotheses": [
    {
      "statement": "Clear, specific root cause hypothesis",
      "category": "resource_exhaustion|network|memory|database|configuration|code_bug|external_dependency",
      "likelihood": 0.65,
      "reasoning": "Why this is likely based on evidence",
      "required_evidence": [
        {
          "description": "What evidence is needed",
          "priority": "critical|important|optional",
          "acquisition_guidance": {
            "source_type": "logs|metrics|config|database|code_inspection|external_api",
            "query_pattern": "Specific query, command, or location",
            "interpretation": "What to look for in the results"
          }
        }
      ]
    }
  ]
}
```

## Field Requirements

### Hypothesis Fields
- **statement** (required): Clear, testable root cause theory (1-2 sentences)
- **category** (required): Classification for diversity tracking
- **likelihood** (required): Initial confidence 0.0-1.0 based on evidence fit
- **reasoning** (required): Why this hypothesis fits the evidence (2-3 sentences)
- **required_evidence** (required): Array of 2-5 evidence items needed to test

### Evidence Item Fields (v3.0 CRITICAL)
Each evidence item MUST include:

1. **description** (required): What evidence to collect
2. **priority** (required):
   - `critical`: Must have to validate/refute hypothesis
   - `important`: Strongly supports validation
   - `optional`: Nice to have, provides additional confidence

3. **acquisition_guidance** (required): How to get this evidence
   - **source_type**: Where evidence comes from
   - **query_pattern**: Specific command/query/location
   - **interpretation**: What patterns to look for

## Complete Example

```json
{
  "hypotheses": [
    {
      "statement": "Database connection pool exhausted due to connection leak in user service",
      "category": "resource_exhaustion",
      "likelihood": 0.70,
      "reasoning": "Symptoms (slow response, timeouts) match connection exhaustion. Timeline shows gradual degradation typical of resource leaks. Blast radius limited to user service aligns with connection pool scope.",
      "required_evidence": [
        {
          "description": "Database connection pool metrics over last 24 hours",
          "priority": "critical",
          "acquisition_guidance": {
            "source_type": "metrics",
            "query_pattern": "SELECT pool_active, pool_idle, pool_wait_time FROM db_metrics WHERE service='user' AND timestamp >= NOW() - INTERVAL '24 hours'",
            "interpretation": "Look for: (1) active connections trending upward, (2) pool_wait_time increasing, (3) correlation with error rate"
          }
        },
        {
          "description": "Application logs showing connection acquisition/release patterns",
          "priority": "critical",
          "acquisition_guidance": {
            "source_type": "logs",
            "query_pattern": "grep -E '(Connection acquired|Connection released|Connection timeout)' /var/log/user-service/*.log | tail -1000",
            "interpretation": "Count 'acquired' vs 'released'. If acquired > released consistently, confirms leak. Look for stack traces on timeout errors."
          }
        },
        {
          "description": "Recent code changes to database access layer",
          "priority": "important",
          "acquisition_guidance": {
            "source_type": "code_inspection",
            "query_pattern": "git log --since='7 days ago' --grep='database\\|connection' -- src/database/ src/repositories/",
            "interpretation": "Look for changes that: (1) added new queries without try-finally blocks, (2) modified connection handling, (3) introduced async operations"
          }
        },
        {
          "description": "Thread dump during high load",
          "priority": "optional",
          "acquisition_guidance": {
            "source_type": "diagnostics",
            "query_pattern": "jstack <user-service-pid> > thread_dump.txt",
            "interpretation": "Look for threads blocked on connection acquisition. Count threads in 'WAITING' state on connection pool locks."
          }
        }
      ]
    },
    {
      "statement": "Network latency spike from upstream payment service causing cascading timeouts",
      "category": "network",
      "likelihood": 0.55,
      "reasoning": "Timeline shows sudden onset which matches network issues. Symptoms include timeouts. If payment service is upstream dependency, latency would cascade to user service.",
      "required_evidence": [
        {
          "description": "Network latency metrics between user service and payment service",
          "priority": "critical",
          "acquisition_guidance": {
            "source_type": "metrics",
            "query_pattern": "SELECT AVG(request_duration_ms), P95(request_duration_ms) FROM service_metrics WHERE from='user-service' AND to='payment-service' GROUP BY time(5m)",
            "interpretation": "Look for: (1) sudden spike in P95 latency around symptom start time, (2) sustained elevated latency, (3) correlation with user service error rate"
          }
        },
        {
          "description": "Payment service health and response times",
          "priority": "critical",
          "acquisition_guidance": {
            "source_type": "logs",
            "query_pattern": "curl http://payment-service:8080/health && grep 'response_time' /var/log/payment-service/access.log | tail -500",
            "interpretation": "Check: (1) health endpoint status, (2) response times in access logs, (3) any error patterns indicating payment service degradation"
          }
        },
        {
          "description": "Network route changes or infrastructure events",
          "priority": "important",
          "acquisition_guidance": {
            "source_type": "external_api",
            "query_pattern": "Check infrastructure event log or cloud provider status page for network events in region",
            "interpretation": "Look for: (1) route table changes, (2) load balancer issues, (3) cross-AZ latency increases, (4) provider-side network incidents"
          }
        }
      ]
    }
  ]
}
```

## Evidence Requirements

**Minimum per hypothesis**: 2 evidence items
**Maximum per hypothesis**: 5 evidence items
**At least one CRITICAL priority** per hypothesis

## Category Guidelines

Choose diverse categories to avoid anchoring:
- `resource_exhaustion`: Connection pools, memory, threads, file descriptors
- `network`: Latency, DNS, connectivity, routing
- `memory`: Leaks, GC pressure, OOM
- `database`: Queries, locks, replication lag, corruption
- `configuration`: Wrong settings, environment variables, feature flags
- `code_bug`: Logic errors, race conditions, edge cases
- `external_dependency`: Third-party APIs, upstream services, infrastructure

## Likelihood Guidance

- **0.70-0.90**: Strong evidence fit, direct cause-effect relationship
- **0.50-0.69**: Moderate fit, plausible but alternatives exist
- **0.30-0.49**: Weak fit, speculation based on partial evidence
- **< 0.30**: Long-shot hypothesis, exploratory

## Quality Checklist

Before submitting, verify:
- [ ] 2-4 hypotheses generated
- [ ] Each has 2-5 required_evidence items
- [ ] Each evidence item has all 3 fields (description, priority, acquisition_guidance)
- [ ] Acquisition guidance is specific and actionable
- [ ] Categories are diverse (not all same type)
- [ ] At least one CRITICAL priority per hypothesis
- [ ] Likelihood values sum to reasonable total (not all >0.80)

## Token Budget

Target: ~2,500 tokens for complete hypothesis set
- ~600 tokens per hypothesis including evidence
- Prioritize quality over quantity (2 excellent hypotheses > 4 mediocre ones)
"""


def get_hypothesis_validation_prompt() -> str:
    """Get validation prompt to check hypothesis structure

    Returns:
        Validation checklist prompt
    """
    return """## Hypothesis Validation

Before advancing to Phase 4, verify your hypotheses meet v3.0 requirements:

**Structural Requirements**:
- [ ] Each hypothesis has `required_evidence` array with 2-5 items
- [ ] Each evidence item has description, priority, and acquisition_guidance
- [ ] Acquisition guidance includes source_type, query_pattern, and interpretation

**Quality Requirements**:
- [ ] Evidence requests are specific and actionable (not vague like "check logs")
- [ ] Priorities correctly assigned (critical = must have to validate)
- [ ] Interpretation explains what patterns to look for
- [ ] Query patterns are copy-paste ready (actual commands/queries)

**Missing any of these?** You cannot advance to Phase 4 (Validation) until hypotheses
include complete required_evidence arrays. Phase 4 will use these to request evidence
systematically.
"""
