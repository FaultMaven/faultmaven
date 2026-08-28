"""A minimal runbook that passes the publication gate, for tests that publish.

Since #1214 ``KnowledgeService.upload_document`` enforces ``RunbookValidator``
before its first side effect, so any test that drives the publish path for some
OTHER reason — on-disk layout, filename containment, scope routing — needs
content the gate accepts. Deriving it here keeps those tests about what they are
about, and means one edit when the runbook schema moves.

Shape is the v4 causal-chain schema: YAML frontmatter carrying every field in
``REQUIRED_METADATA``, the six ``REQUIRED_SECTIONS``, one real ``### Cause`` and
the ``[Default]`` fallback. ``tests/unit/modules/knowledge/test_runbook_samples.py``
asserts it actually passes, so a schema change breaks the sample loudly instead
of turning every consumer's failure into a puzzle.
"""

from __future__ import annotations


def valid_runbook(title: str = "Sample Runbook For Publication") -> str:
    """Return runbook markdown that ``RunbookValidator`` passes.

    Args:
        title: Frontmatter title. Kept under the 100-char cap and at least 10
            characters, or the validator warns/errors about it.
    """
    return f"""---
id: sample-runbook
title: {title}
domain: database
service: postgresql
symptom_class: [latency]
severity: high
scope: global
version: 1.0.0
last_updated: 2026-08-28
verified_by: ""
status: draft
---

# Runbook: Sample

## Symptom Recognition
- "ERROR: something failed"

## Applicability
PostgreSQL 14+. Requires pg_monitor role. Tools: psql.

## Diagnostic Steps

### Step 1: Check state
```bash
psql -c "SELECT 1"
```
Look for a non-empty result.

## Causes

### Cause A: Example root cause
**Statement:** The single root cause of the failure.
**Indicators:**
- root: [Step 1] the observable that confirms the root
**Interventions:**
- **remediation** (root): apply the durable fix.
  **Verification:** Re-run Step 1; the result is non-empty.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture full diagnostic output and consult an SME.
  **Risk:** Diagnostic only. **Duration:** Until SME review. **Verification:** N/A.

## Prevention
- Add an alert on the failing metric.

## Sources
- sample.md -- primary source document for this runbook
"""
