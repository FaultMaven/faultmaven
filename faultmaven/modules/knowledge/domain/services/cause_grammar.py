"""Canonical v4 cause **authoring-grammar** vocabulary — single source of truth.

The markdown grammar for a v4 runbook's ``## Causes`` (the per-Cause sub-field
labels, the intervention quadrant tags, and the fallback token) is encoded in
several places that MUST agree:

  1. this module — referenced by the backend validator
     (``runbook_validator``); the single backend definition;
  2. the backend authoring prompt
     (``conversion_service.CONVERSION_SYSTEM_PROMPT``) — prose that instructs an
     LLM to emit this grammar; guarded (not rewritten) by
     ``test_cause_grammar_vocab`` asserting the prompt covers every term here;
  3. the kb-toolkit grammar SOURCE (``kb_toolkit/core/runbook_grammar.py`` +
     ``config.py``'s ``required_cause_subfields`` / ``optional_cause_subfields`` /
     ``valid_quadrants``) — the upstream PRODUCER side.

The two repos cannot import one another, so (1) is a **manual mirror** of the
kb-toolkit config defaults (3). A change here MUST be mirrored there and
vice-versa. Two guards keep them honest: each repo's frozen-literal drift-guard
test trips if that side's literal is edited without updating its own test; and
the kb-toolkit ``golden-cross-repo`` CI job
(``scripts/check_vocab_cross_repo.py``) mechanically asserts the two repos'
vocabularies are equal. (A field-level pack-record golden once pinned the
parsed record on both sides; the app half went with the cause record in
fm#1295, the toolkit keeps its own.)

LAYER NOTE: these are the AUTHORING markdown sub-fields — a *different layer*
from the parsed per-cause records in the built KB pack. ``chain_edges`` is
*derived* from ``Chain:`` rung order, so it is not an authored sub-field and
does not appear here.

The intervention quadrant VALUES are owned by the ``InterventionQuadrant`` enum
(``modules.case.domain.models``); ``INTERVENTION_QUADRANTS`` mirrors them for the
authoring layer and is pinned to the enum by the drift-guard test (kept a literal
here so this leaf module takes no cross-module import).
"""

# Required per-Cause markdown sub-fields — every non-fallback ``### Cause`` carries
# these (order is the authoring/display order).
REQUIRED_CAUSE_SUBFIELDS = ("Statement", "Indicators", "Interventions")

# Optional sub-field — omitted for a simple one-step cause (its absence yields a
# degenerate ``root → D`` chain on ingestion).
OPTIONAL_CAUSE_SUBFIELDS = ("Chain",)

# Intervention quadrant tags — each intervention bullet is tagged with exactly one.
# Mirrors ``InterventionQuadrant`` (methodology §7.4); pinned by the drift-guard.
INTERVENTION_QUADRANTS = ("remediation", "defensive_fix", "mitigation", "loop_break")

# The fallback Cause: its indicator token and its conventional letter.
FALLBACK_INDICATOR_TOKEN = "[Default]"
FALLBACK_CAUSE_LETTER = "Z"

# Legacy v3 per-Cause sub-fields, no longer valid in v4 (flagged on sight).
LEGACY_V3_CAUSE_SUBFIELDS = ("Mechanism", "Mitigation", "Resolution")

# A ``(a|b|c)`` regex alternation of the quadrant tags, for reuse in validator
# patterns so the list lives in exactly one place.
QUADRANT_ALTERNATION = "|".join(INTERVENTION_QUADRANTS)
