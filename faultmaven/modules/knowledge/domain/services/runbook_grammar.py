"""Canonical v4 runbook (causal-chain) markdown **parse** grammar — the single
in-repo source for turning a ``## Causes`` section into structured records.

This module mirrors the upstream producer grammar
(``kb_toolkit/core/runbook_grammar.py``) verbatim: the regexes and sub-field
parser here are byte-for-byte the ones the KB pack builder uses to emit each
runbook's ``metadata["causes"]`` graph record. Anchoring the in-repo extractor
(``runbook_cause_extractor``) on this shared grammar is what keeps the
**produce** side (a case→runbook conversion, extracted here on verify) and the
**consume** side (built-in runbooks, extracted upstream in the pack) structurally
symmetric — a converted runbook re-enters diagnosis as the same shape of
candidate a shipped one does.

LAYER NOTE — three grammar surfaces exist and must agree:

  1. ``cause_grammar`` — the AUTHORING vocabulary (sub-field labels, quadrant
     tags, fallback token), a manual mirror of the kb-toolkit config defaults;
  2. this module — the PARSE grammar (regexes + ``parse_cause_subfields``), a
     manual mirror of ``kb_toolkit/core/runbook_grammar.py``;
  3. ``runbook_validator`` — the GATE. It anchors its cause ENUMERATION and
     sub-field parsing on THIS module (the same ``CAUSE_HEADING_RE`` /
     ``CAUSES_SECTION_RE`` / ``parse_cause_subfields`` the extractor uses), so a
     draft the gate passes is exactly one the extractor can parse — the gate can
     no longer be looser than the parser it fronts. Only its message-oriented
     present-vs-empty wording is validator-private; the grammar is shared.

The two repos cannot import one another, so (2) is a **manual mirror**. Three
guards keep it honest. Two are in-repo and see only this checkout:
``test_runbook_grammar`` (frozen-literal drift-guard — trips if a pattern here is
edited without updating the test) and the golden cross-check in
``test_runbook_cause_extractor`` (pins the extractor's output to records computed
by the kb-toolkit builder for vendored fixtures). Neither can see the upstream
grammar, so a corpus-invariant change upstream (e.g. a widened regex no vendored
fixture exercises) slips both. The third guard closes that: kb-toolkit's
``scripts/check_grammar_cross_repo.py`` CI job checks out BOTH repos and compares
the shared regex primitives (pattern + flags) and ``CONVERGES_REF`` across the
two ``runbook_grammar.py`` files. A change to the upstream grammar MUST be
mirrored here and vice-versa.
"""

import re

# The ``## Causes`` H2 section body — everything up to the next H2 or EOF.
CAUSES_SECTION_RE = re.compile(r"(?ms)^##\s+Causes\s*\n(.*?)(?=^##\s+|\Z)")

# A ``### Cause X: <name>`` heading inside a markdown block (captures letter + name).
CAUSE_HEADING_RE = re.compile(r"^### Cause ([A-Z]):\s*(.+?)\s*$", re.MULTILINE)

# A ``### Step N: <title>`` diagnostic-step heading — the step numbers an
# Indicator's ``[Step N]`` token must resolve to.
STEP_HEADING_RE = re.compile(r"^### Step (\d+):", re.MULTILINE)

# An Indicator token: ``[Step N]`` (an operator step), ``[Symptom]`` (an observed
# condition), or ``[Default]`` (the fallback Cause marker).
INDICATOR_TOKEN_RE = re.compile(r"\[(Step \d+|Symptom|Default)\]")

# A ``[Step N]`` reference inside an Indicator entry (captures N so the validator
# can resolve it against ``## Diagnostic Steps``).
STEP_REF_RE = re.compile(r"\[Step (\d+)\]")

# Strip HTML comments (``<!-- match: ... -->`` directives etc.) before parsing
# rung text — they are authoring annotations, not chain content.
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# A v4 intervention bullet: ``- **<quadrant>** (<ref>): ...`` (indent-tolerant).
INTERVENTION_RE = re.compile(
    r"^\s*[-*+]\s*\*\*([A-Za-z_]+)\*\*\s*\(([^)]*)\)", re.MULTILINE
)

# A v4 chain / indicator rung: ``- <ref>: <statement>`` (ref = root|sN|D, or the
# ``converges`` directive). Indent-tolerant.
CHAIN_RUNG_RE = re.compile(
    r"^\s*[-*+]\s*([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*)$", re.MULTILINE
)

# The ``converges: <Cause>.<ref>`` directive — a rung that maps onto another
# Cause's node rather than introducing a new one.
CONVERGES_REF = "converges"


def parse_cause_subfields(cause_content: str, field_names: list[str]) -> dict[str, str]:
    """Parse ``**Field:**`` blocks out of a Cause body.

    Each field's value runs until the next ``**Field:**`` label (drawn from
    ``field_names``) or end of content. Callers pass the schema's sub-field set
    (``required + optional`` from :mod:`cause_grammar`) so the boundary set stays
    in lockstep with the validator's contract.
    """
    fields: dict[str, str] = {}
    for field in field_names:
        others = "|".join(re.escape(f"**{f}:**") for f in field_names if f != field)
        pattern = (
            rf"\*\*{re.escape(field)}:\*\*\s*(.*?)(?=(?:{others})|\Z)"
            if others
            else rf"\*\*{re.escape(field)}:\*\*\s*(.*?)\Z"
        )
        m = re.search(pattern, cause_content, re.DOTALL)
        if m:
            fields[field] = m.group(1).strip()
    return fields
