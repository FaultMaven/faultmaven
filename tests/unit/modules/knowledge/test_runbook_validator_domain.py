"""Controlled-vocabulary enforcement for ``domain`` in RunbookValidator.

``domain`` is a controlled taxonomy (like ``symptom_class``), owned in this repo
by ``taxonomy.RunbookDomain`` and mirrored by kb-toolkit
(``ValidationConfig.valid_domains``), which cannot import it. The cross-repo
parity gate only runs in kb-toolkit CI, so an app-side edit would otherwise pass
app CI and be caught only at the next kb-toolkit CI run. This frozen-literal pin
closes that gap on the app side, symmetric with
``test_runbook_validator_symptom_class.py``.
"""

from __future__ import annotations

import pytest

from faultmaven.modules.knowledge.contracts import (
    _DOMAIN_GLOSSES,
    describe_troubleshooting_domains,
    describe_troubleshooting_scope,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)
from faultmaven.modules.knowledge.taxonomy import RunbookDomain, vocabulary

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
    assert list(vocabulary(RunbookDomain)) == _EXPECTED_DOMAINS


def test_in_vocab_domain_passes():
    assert _domain_errors(_runbook("messaging")) == []


def test_off_vocab_domain_is_error():
    """An off-vocabulary domain is a hard error (not a silent pass)."""
    errors = _domain_errors(_runbook("kubernetes"))
    assert errors
    assert "kubernetes" in errors[0]


# ---------------------------------------------------------------------------
# Single-source property: the ingestion gate and the published territory are
# the SAME vocabulary — ``RunbookDomain`` — so there is no second copy to
# compare. The prose renderers below must still be total over it.
# ---------------------------------------------------------------------------


def test_every_domain_reaches_the_prose_renderer():
    """A prompt stating the territory cannot silently omit a domain.

    Prompts render the taxonomy through one helper so the several sites that
    name it cannot drift; that only holds if the helper is total.
    """
    rendered = describe_troubleshooting_domains()
    for domain in vocabulary(RunbookDomain):
        assert domain in rendered


# ---------------------------------------------------------------------------
# Glosses: what each domain COVERS, so the agent can map a user's words onto a
# vertical. Seven bare nouns cannot support that mapping — an agent left to
# guess whether firmware or a Windows service belongs to "compute" may guess
# no, and refusing work it should do is the expensive direction.
# ---------------------------------------------------------------------------


def test_no_domain_can_exist_without_a_gloss():
    """Every ``RunbookDomain`` member has a gloss, and nothing else does.

    A domain added to the enum without one would otherwise reach the prompts
    as a bare noun — or, since the mapping renderer indexes the glosses by
    member, fail every prompt build that states the territory.
    """
    assert set(_DOMAIN_GLOSSES) == set(RunbookDomain)
    for name, gloss in _DOMAIN_GLOSSES.items():
        assert gloss.strip(), f"{name} has no gloss"


def test_scope_rendering_is_total():
    """Every domain reaches the mapping-form rendering, with its gloss."""
    rendered = describe_troubleshooting_scope()
    for name, gloss in _DOMAIN_GLOSSES.items():
        assert name.value in rendered
        assert gloss.split("—")[0].strip() in rendered


def test_both_renderings_cover_the_same_vocabulary():
    """The short form and the mapping form cannot drift apart.

    They are used in different prompts — one where length is the cost, one
    where classification is the job — and a domain present in only one of them
    is a lane that disagrees with another about the territory.
    """
    short = describe_troubleshooting_domains()
    scope = describe_troubleshooting_scope()
    for name in vocabulary(RunbookDomain):
        assert name in short and name in scope


def test_conversion_side_domain_keywords_stay_inside_the_vocabulary():
    """The case→runbook converter stamps `domain`; the gate then validates it.

    ``_DOMAIN_KEYWORDS`` is keyed by ``RunbookDomain``, with its own
    ``application`` fallback. Before it was, its keys were a second copy of the
    vocabulary, and renaming a domain left the converter stamping a value the
    validator rejects — case-to-runbook conversion failing at ingestion.

    A subset check rather than equality: the converter needs keywords only for
    domains it can actually infer, and `application` is deliberately keyword-free
    because it is the catch-all.
    """
    from faultmaven.modules.knowledge.domain.models.conversion import (
        _DOMAIN_KEYWORDS,
        _resolve_domain,
    )

    unknown = set(_DOMAIN_KEYWORDS) - set(RunbookDomain)
    assert not unknown, f"converter can stamp domains the gate rejects: {unknown}"

    # The fallback must itself be in the vocabulary, or a case matching no
    # keyword produces a draft that cannot be ingested; and what it returns is
    # the plain value, which is what reaches the frontmatter.
    fallback = _resolve_domain("nothing here matches any keyword")
    assert fallback in vocabulary(RunbookDomain)
    assert type(fallback) is str
    assert type(_resolve_domain("kafka consumer lag")) is str
