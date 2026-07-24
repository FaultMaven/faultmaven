"""Controlled-vocabulary enforcement for ``symptom_class`` in RunbookValidator.

``symptom_class`` is a controlled failure-mode taxonomy (like ``domain``), not
free text (unlike ``service``). The producer side lives in
``faultmaven-kb-toolkit`` (``ValidationConfig.valid_symptom_classes``); the app
mirrors it by hand in ``VALID_SYMPTOM_CLASSES`` (the repos can't import each
other). These tests pin the app copy and the gate that rejects off-vocabulary
values — the metadata-drift the whole controlled-vocabulary rule exists to
prevent (``latency`` vs. ``latency-issue`` vs. ``high_latency``).
"""

from __future__ import annotations

import pytest

from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    VALID_SYMPTOM_CLASSES,
    RunbookValidator,
)

pytestmark = pytest.mark.unit


# The exact 16-value curated set, in order. This is the frozen literal that
# catches an accidental in-repo edit; it MUST equal the kb-toolkit producer
# (``ValidationConfig.valid_symptom_classes``). Grow both, in lock-step with the
# taxonomy design rule, never by loosening the gate.
_EXPECTED_VOCAB = [
    "auth_failure",
    "connection_refused",
    "cpu_saturation",
    "crash_loop",
    "data_loss",
    "deployment_failure",
    "disk_full",
    "image_pull_failure",
    "latency",
    "node_failure",
    "oom",
    "replication_lag",
    "scheduling_failure",
    "service_unavailable",
    "throughput_degradation",
    "timeout",
]


def _runbook(symptom_class_yaml: str) -> str:
    """A minimal, otherwise-valid runbook whose ``symptom_class`` is under test."""
    return f"""---
id: sample-runbook
title: Sample Runbook For Symptom Class
domain: database
service: postgresql
symptom_class: {symptom_class_yaml}
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


def _symptom_class_errors(content: str) -> list[str]:
    result = RunbookValidator().validate_content(content)
    return [e for e in result.errors if "symptom_class" in e]


def test_vocab_is_the_frozen_curated_set():
    """The app copy matches the curated kb-toolkit set exactly, in order."""
    assert VALID_SYMPTOM_CLASSES == _EXPECTED_VOCAB


def test_in_vocab_value_passes():
    assert _symptom_class_errors(_runbook("[replication_lag]")) == []


def test_multiple_in_vocab_values_pass():
    assert _symptom_class_errors(_runbook("[latency, timeout]")) == []


def test_off_vocab_value_is_error():
    """An off-vocabulary value is a hard error (not a silent pass)."""
    errors = _symptom_class_errors(_runbook("[high_latency]"))
    assert errors, "off-vocab symptom_class should produce an error"
    assert "high_latency" in errors[0]


def test_unknown_placeholder_is_error():
    """The old ``["unknown"]`` conversion default is off-vocabulary and rejected —
    the gate that makes the produce-path reconcile necessary."""
    errors = _symptom_class_errors(_runbook("[unknown]"))
    assert errors
    assert "unknown" in errors[0]


def test_hyphenated_value_is_error():
    """A hyphenated value (format-legal but off-vocab) is now rejected — the
    controlled vocabulary subsumes the old format-only check."""
    errors = _symptom_class_errors(_runbook("[throughput-degradation]"))
    assert errors
    assert "throughput-degradation" in errors[0]


def test_malformed_value_still_format_errors():
    """Uppercase/space (never a vocab token) trips the format check first."""
    errors = _symptom_class_errors(_runbook("[Connection Refused]"))
    assert errors
    assert "lowercase" in errors[0]


def test_scalar_symptom_class_is_error():
    """A YAML scalar (``symptom_class: unknown``) must NOT slip past a list-only
    gate — it is the same off-vocab drift arriving through the front door."""
    errors = _symptom_class_errors(_runbook("unknown"))
    assert errors
    assert "must be a list" in errors[0]


def test_non_string_item_is_error():
    """A non-string list item is rejected, not silently skipped."""
    errors = _symptom_class_errors(_runbook("[123]"))
    assert errors
    assert "must be strings" in errors[0]


def test_empty_list_warns_not_errors():
    """An empty ``symptom_class`` is a warning (no taxonomy), never a hard error."""
    result = RunbookValidator().validate_content(_runbook("[]"))
    assert [e for e in result.errors if "symptom_class" in e] == []
    assert any("No symptom classes specified" in w for w in result.warnings)
