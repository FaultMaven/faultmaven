---
id: example-runbook
title: Example
---

## Overview
Intro prose.

## Causes

### Cause A: Connection pool exhausted
**Statement:** All DB connections are checked out and none are returned.
**Chain:**
- root: Connection leak in request handler <!-- match: leak -->
- s1: Pool saturates under load
- D: Requests block waiting for a connection
**Indicators:**
- root: [Step 1] open connections climb monotonically
- s1: [Step 2] pool wait-time spikes
- s1: [Symptom] 503s under load
**Interventions:**
- **remediation** (root): Fix the leak so connections are returned. **Verification:** pool stabilizes.
- **mitigation** (s1): Raise pool size temporarily. **Risk:** masks leak. **Duration:** until fix. **Verification:** waits drop.

### Cause B: Downstream timeout cascade
**Statement:** A slow downstream holds connections open.
**Chain:**
- root: Downstream latency exceeds client timeout
- converges: A.s1
**Indicators:**
- root: [Symptom] downstream p99 latency high
**Interventions:**
- **defensive_fix** (root): Add a circuit breaker. **Verification:** breaker trips on latency.

### Cause Z: Unidentified
**Statement:** Root cause not yet determined.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture diagnostics and consult an SME. **Risk:** none. **Duration:** until review. **Verification:** N/A.

## Prevention
- Add monitoring.
