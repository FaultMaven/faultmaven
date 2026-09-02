"""Frozen-literal drift-guard for the in-repo v4 parse grammar.

``runbook_grammar`` is a manual mirror of the upstream producer grammar
(``kb_toolkit/core/runbook_grammar.py``): the two repos cannot import one
another, so the regexes and the sub-field parser are duplicated. This test pins
each pattern's source so an edit here can't silently diverge from the upstream
grammar without also updating this guard; kb-toolkit's cross-repo grammar check
is the second line of defence that the two sides still agree.

If you change a pattern, mirror it in kb-toolkit AND update the frozen literal
below in the same commit.
"""

import pytest

from faultmaven.modules.knowledge.domain.services import runbook_grammar as g

pytestmark = pytest.mark.unit


def test_grammar_patterns_frozen():
    assert g.CAUSES_SECTION_RE.pattern == r"(?ms)^##\s+Causes\s*\n(.*?)(?=^##\s+|\Z)"
    assert g.CAUSE_HEADING_RE.pattern == r"^### Cause ([A-Z]):\s*(.+?)\s*$"
    assert g.STEP_HEADING_RE.pattern == r"^### Step (\d+):"
    assert g.INDICATOR_TOKEN_RE.pattern == r"\[(Step \d+|Symptom|Default)\]"
    assert g.STEP_REF_RE.pattern == r"\[Step (\d+)\]"
    assert g.HTML_COMMENT_RE.pattern == r"<!--.*?-->"
    # The code-awareness the mask needs (#1241). Frozen here with the rest
    # because a mirror LACKING these is exactly the mirror that reintroduces the
    # two regressions a naive strip caused, and upstream's ``_REGEX_SYMBOLS``
    # allowlist is what would force them to exist there.
    assert g.CODE_FENCE_LINE_RE.pattern == r"^[ \t]{0,3}(`{3,}|~{3,})"
    assert g.CODE_SPAN_RE.pattern == r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)"
    assert g.COMMENT_BODY_CHAR_RE.pattern == r"[^\n]"
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


def test_comment_countermeasures_are_part_of_the_mirrored_grammar():
    """#1241 — both halves of the comment fix must be mirrored upstream.

    The cross-repo checker compares regex primitives and ``CONVERGES_REF`` only,
    so it cannot see either of these; this is the in-repo half of the guard.
    Behaviour is pinned in full by
    ``test_grammar_ignores_html_comments_1241.py``.
    """
    # Sub-field VALUES: a label inside a comment is not the field.
    assert g.parse_cause_subfields(
        "<!-- **Statement:** example -->\n**Statement:**\n", ["Statement"]
    ) == {"Statement": ""}
    # Heading ENUMERATION: comments blanked length-preservingly, so offsets into
    # the raw markdown (which is what the chunker measures) still line up.
    text = "<!-- ### Cause A: x -->\n### Cause A: real\n"
    masked = g.mask_html_comments(text)
    assert len(masked) == len(text)
    assert [m.group(2) for m in g.CAUSE_HEADING_RE.finditer(masked)] == ["real"]
    # …and ONLY comments: a marker quoted as content is not one.
    assert g.mask_html_comments("`<!--` and `-->`") == "`<!--` and `-->`"
    # The shared walk, which is what routes every consumer through the mask.
    assert [
        c.letter
        for c in g.iter_cause_blocks(
            "## Causes\n\n<!--\n### Cause A: hidden\n-->\n### Cause Z: real\n"
        )
    ] == ["Z"]
