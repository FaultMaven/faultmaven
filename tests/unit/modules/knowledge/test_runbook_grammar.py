"""Frozen-literal drift-guard for the in-repo v4 parse grammar.

``runbook_grammar`` is a manual mirror of the upstream producer grammar
(``kb_toolkit/core/runbook_grammar.py``): the two repos cannot import one
another, so the regexes and the sub-field parser are duplicated. This test pins
each pattern's source so an edit here can't silently diverge from the upstream
grammar without also updating this guard — at which point the golden cross-check
(``test_runbook_cause_extractor``) is the second line of defence that the two
sides still agree on real markdown.

If you change a pattern, mirror it in kb-toolkit AND update the frozen literal
below in the same commit.
"""

import pytest

from faultmaven.modules.knowledge.domain.services import runbook_grammar as g

pytestmark = pytest.mark.unit


def test_grammar_patterns_frozen():
    assert g.CAUSES_SECTION_RE.pattern == r"(?ms)^##\s+Causes\s*\n(.*?)(?=^##\s+|\Z)"
    assert g.CAUSE_HEADING_RE.pattern == r"^### Cause ([A-Z]):\s*(.+?)\s*$"
    assert g.HTML_COMMENT_RE.pattern == r"<!--.*?-->"
    assert (
        g.INTERVENTION_RE.pattern == r"^\s*[-*+]\s*\*\*([A-Za-z_]+)\*\*\s*\(([^)]*)\)"
    )
    assert g.CHAIN_RUNG_RE.pattern == r"^\s*[-*+]\s*([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*)$"
    assert g.CONVERGES_REF == "converges"


def test_parse_cause_subfields_boundaries():
    body = (
        "**Statement:** the statement\n"
        "**Chain:**\n- root: r\n"
        "**Indicators:**\n- root: [Symptom] x\n"
    )
    fields = g.parse_cause_subfields(body, ["Statement", "Indicators", "Chain"])
    assert fields["Statement"] == "the statement"
    assert fields["Chain"] == "- root: r"
    assert fields["Indicators"] == "- root: [Symptom] x"


def test_parse_cause_subfields_absent_field_omitted():
    fields = g.parse_cause_subfields("**Statement:** only", ["Statement", "Chain"])
    assert fields == {"Statement": "only"}
