"""Controlled-vocabulary enforcement for ``domain`` in RunbookValidator.

``domain`` is a controlled taxonomy (like ``symptom_class``), mirrored by hand
between kb-toolkit (``ValidationConfig.valid_domains``) and the app
(``VALID_DOMAINS``) since the repos can't import each other. The cross-repo parity
gate keeps the two copies byte-equal, but it only runs in kb-toolkit CI — so an
app-side edit to ``VALID_DOMAINS`` would otherwise pass app CI and be caught only
at the next kb-toolkit CI run. This frozen-literal pin closes that gap on the app
side, symmetric with ``test_runbook_validator_symptom_class.py`` (which already
pins ``VALID_SYMPTOM_CLASSES``).
"""

from __future__ import annotations

import pytest

from faultmaven.modules.knowledge.contracts import (
    TROUBLESHOOTING_DOMAINS,
    describe_troubleshooting_domains,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    VALID_DOMAINS,
    RunbookValidator,
)

pytestmark = pytest.mark.unit


# The curated domain set, in order. Frozen literal that catches an accidental
# in-repo edit; it MUST equal the kb-toolkit producer
# (``ValidationConfig.valid_domains``). Grow both, in lock-step with the taxonomy
# design rule, never by loosening the gate.
_EXPECTED_DOMAINS = [
    "database",
    "networking",
    "compute",
    "application",
    "security",
    "storage",
    "messaging",
]


def _runbook(domain: str) -> str:
    """A minimal, otherwise-valid runbook whose ``domain`` is under test."""
    return f"""---
id: sample-runbook
title: Sample Runbook For Domain
domain: {domain}
service: postgresql
symptom_class: [latency]
severity: high
scope: global
version: 1.0.0
last_updated: 2026-07-24
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


def _domain_errors(content: str) -> list[str]:
    result = RunbookValidator().validate_content(content)
    return [e for e in result.errors if "domain" in e.lower()]


def test_domains_are_the_frozen_curated_set():
    """The app copy matches the curated kb-toolkit set exactly, in order."""
    assert VALID_DOMAINS == _EXPECTED_DOMAINS


def test_in_vocab_domain_passes():
    assert _domain_errors(_runbook("messaging")) == []


def test_off_vocab_domain_is_error():
    """An off-vocabulary domain is a hard error (not a silent pass)."""
    errors = _domain_errors(_runbook("kubernetes"))
    assert errors
    assert "kubernetes" in errors[0]


# ---------------------------------------------------------------------------
# Single-source property: the ingestion gate and the published territory are
# the SAME vocabulary. Before this, the agent side had no access to the
# taxonomy and carried four improvised prose versions of it instead; a second
# literal here is how that starts again.
# ---------------------------------------------------------------------------


def test_ingestion_gate_derives_from_the_published_territory():
    """``VALID_DOMAINS`` is the contract's tuple, not a second literal.

    The property, not an instance: re-literalising either side fails this
    regardless of which domains the taxonomy happens to hold.
    """
    assert VALID_DOMAINS == list(TROUBLESHOOTING_DOMAINS)


def test_every_domain_reaches_the_prose_renderer():
    """A prompt stating the territory cannot silently omit a domain.

    Prompts render the taxonomy through one helper so the several sites that
    name it cannot drift; that only holds if the helper is total.
    """
    rendered = describe_troubleshooting_domains()
    for domain in TROUBLESHOOTING_DOMAINS:
        assert domain in rendered
