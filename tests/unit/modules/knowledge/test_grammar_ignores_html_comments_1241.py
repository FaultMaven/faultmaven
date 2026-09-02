"""#1241 — the v4 cause grammar must be blind to HTML comments, and ONLY to them.

Measured on ``859a8d47c``, ``parse_cause_subfields`` did not strip HTML comments,
so a sub-field label written INSIDE a comment was read as the real value::

    >>> parse_cause_subfields(
    ...     "<!-- Fill this in, e.g. **Statement:** The pool was exhausted. -->\\n"
    ...     "**Statement:**\\n**Evidence:**\\n**Fix:**\\n",
    ...     ["Statement", "Evidence", "Fix"])
    {'Statement': 'The pool was exhausted. -->\\n**Statement:**', 'Evidence': '', 'Fix': ''}

Every real field is empty, yet ``Statement`` comes back non-empty. That is a GATE
BYPASS: #1214 made ``RunbookValidator`` load-bearing, so a Cause whose
``**Statement:**`` is genuinely empty must be refused — and a comment anywhere
above it satisfied the check instead.

The defect is a CLASS, not one call site, and it escaped a per-site repair
twice before this file settled:

1. **Sub-field values** — the reported instance, above.
2. **Heading enumeration** — a whole ``### Cause A:`` block written inside a
   comment was counted, validated and extracted. Not an empty runbook but a
   FABRICATED one.
3. (Removed in fm#1295.) **The chunk cause-letter stamp** — the join key
   the KB cause seeder reads, stamping a phantom letter with no
   ``metadata["causes"]`` record behind it.
4. **A comment opening BEFORE ``## Causes``** — the section was located in the
   RAW document, so the mask never saw it. A well-formed runbook with its entire
   Causes section commented out returned ``passed=True, errors=[]``.

And the first repair introduced two regressions of its own, both measured, both
pinned here: a naive ``<!--.*?-->`` sweep DELETED quoted markup out of real
content, and its span rule joined two literal tokens in different Causes and
blanked the second Cause away.

So the countermeasure is ONE function (``mask_html_comments``, code-aware) and
ONE enumerator (``iter_cause_blocks``) that every consumer routes through, with
an enumeration guard below to keep it that way.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from faultmaven.modules.knowledge.domain.services import (
    knowledge_service as knowledge_service_mod,
)
from faultmaven.modules.knowledge.domain.services import runbook_grammar as g
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
    _iter_cause_blocks,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)

pytestmark = pytest.mark.unit

# tests/unit/modules/knowledge/<file> -> parents[4] == repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
PACK_RUNBOOKS = REPO_ROOT / "resources" / "knowledge" / "pack" / "runbooks"
QUOTED_TOKENS_RUNBOOK = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "comment_tokens"
    / "quoted-comment-tokens.md"
)


def _wrap(cause_body: str) -> str:
    """A ``## Causes`` section holding one Cause A block plus a valid fallback."""
    return (
        "## Causes\n\n"
        f"### Cause A: Pool exhaustion\n{cause_body}\n"
        "### Cause Z: Unidentified\n"
        "**Statement:** None of the documented causes match.\n"
        "**Indicators:**\n- [Default]\n"
        "**Interventions:**\n"
        "- **mitigation** (D): Escalate to an SME. **Risk:** none. "
        "**Duration:** n/a. **Verification:** N/A.\n"
    )


def _runbook(causes_section: str) -> str:
    """A well-formed runbook that passes the whole gate, parameterised on its
    ``## Causes`` section — so a bypass shows up as ``passed`` flipping rather
    than as unrelated frontmatter noise."""
    return (
        "---\n"
        'id: "pg-pool-exhaustion-1241"\n'
        'title: "Postgres connection pool exhaustion"\n'
        "domain: database\n"
        "service: postgresql\n"
        "symptom_class: [service_unavailable]\n"
        "severity: high\n"
        "scope: global\n"
        'version: "1.0.0"\n'
        'last_updated: "2026-08-29"\n'
        'verified_by: "lane-1241"\n'
        "status: draft\n"
        "tags: [postgres, pool]\n"
        "difficulty: intermediate\n"
        "---\n\n"
        "## Symptom Recognition\n"
        "- Application logs show `remaining connection slots are reserved`.\n"
        "- Pool checkout latency climbs and requests time out waiting.\n\n"
        "## Applicability\n"
        "PostgreSQL 14+ behind PgBouncer, with psql access and pg_stat_activity "
        "readable by the operator.\n\n"
        "## Diagnostic Steps\n\n"
        "### Step 1: Inspect pooled sessions\n"
        "```bash\n"
        'psql -c "select state, count(*) from pg_stat_activity group by 1"\n'
        "```\n"
        "Look for a large `idle in transaction` bucket.\n\n"
        f"{causes_section}\n"
        "## Prevention\n"
        "- Set idle_in_transaction_session_timeout and alert on pool saturation.\n\n"
        "## Sources\n"
        "- Internal incident review, 2026-08.\n"
    )


_REAL_CAUSES = (
    "### Cause A: Pool exhaustion\n"
    "**Statement:** Idle-in-transaction sessions hold every pooled connection "
    "open until the pool is fully checked out and new work blocks.\n"
    "**Indicators:**\n"
    "- root: [Step 1] pg_stat_activity shows sessions idle in transaction\n"
    "**Interventions:**\n"
    "- **remediation** (root): Terminate the idle sessions and set "
    "idle_in_transaction_session_timeout. **Risk:** rolls back open work. "
    "**Duration:** 2m. **Verification:** pool checkouts fall.\n\n"
    "### Cause Z: Unidentified\n"
    "**Statement:** None of the documented causes match the observed evidence.\n"
    "**Indicators:**\n- [Default]\n"
    "**Interventions:**\n"
    "- **mitigation** (D): Capture full diagnostic output and consult an SME. "
    "**Risk:** Diagnostic only. **Duration:** Until SME review. "
    "**Verification:** N/A.\n"
)


def _cause_records(text: str) -> list[dict]:
    """Grammar-level stand-in for the removed extractor: one record per Cause
    block with its letter and parsed ``Statement``."""
    from faultmaven.modules.knowledge.domain.services.cause_grammar import (
        OPTIONAL_CAUSE_SUBFIELDS,
        REQUIRED_CAUSE_SUBFIELDS,
    )

    names = list(REQUIRED_CAUSE_SUBFIELDS) + list(OPTIONAL_CAUSE_SUBFIELDS)
    return [
        {
            "cause_letter": b.letter,
            "cause_name": b.name,
            "cause_statement": g.parse_cause_subfields(b.body, names).get(
                "Statement", ""
            ),
        }
        for b in g.iter_cause_blocks(text)
    ]


# =============================================================================
# 1. The reported defect: a label inside a comment is read as the real field
# =============================================================================


class TestCommentedLabelIsNotTheRealField:
    def test_the_measured_case_from_the_issue(self):
        """Verbatim the reproduction in #1241, asserted on its exact output."""
        fields = g.parse_cause_subfields(
            "<!-- Fill this in, e.g. **Statement:** The pool was exhausted. -->\n"
            "**Statement:**\n**Evidence:**\n**Fix:**\n",
            ["Statement", "Evidence", "Fix"],
        )
        assert fields == {"Statement": "", "Evidence": "", "Fix": ""}

    def test_a_comment_declaring_every_label_leaves_every_field_empty(self):
        body = (
            "<!-- Fill in, for example:\n"
            "     **Statement:** The pool was exhausted by leaked sessions.\n"
            "     **Indicators:**\n     - root: [Step 1] pool_active == pool_max\n"
            "     **Interventions:**\n     - **remediation** (root): restart.\n"
            "-->\n"
            "**Statement:**\n**Indicators:**\n**Interventions:**\n"
        )
        fields = g.parse_cause_subfields(
            body, ["Statement", "Indicators", "Interventions", "Chain"]
        )
        assert fields == {"Statement": "", "Indicators": "", "Interventions": ""}
        # ``Chain`` is absent, not empty — the parser reports MISSING vs EMPTY and
        # the gate's wording depends on the difference.
        assert "Chain" not in fields

    def test_the_gate_refuses_a_cause_whose_labels_live_only_in_a_comment(self):
        """End-to-end: the bypass this issue is about. Pre-fix this returned
        ``passed=True`` with ``errors=[]`` on a Cause whose three required
        sub-fields are all empty."""
        content = _wrap(
            "<!-- Fill in, for example:\n"
            "     **Statement:** The pool was exhausted by leaked sessions.\n"
            "     **Indicators:**\n     - root: [Step 1] pool_active == pool_max\n"
            "     **Interventions:**\n"
            "     - **remediation** (root): Restart. **Risk:** none. "
            "**Duration:** 2m. **Verification:** pool drops.\n"
            "-->\n"
            "**Statement:**\n**Indicators:**\n**Interventions:**\n"
        )
        errors: list[str] = []
        RunbookValidator()._validate_cause_subfields(content, errors, [])
        for sub in ("Statement", "Indicators", "Interventions"):
            assert f"Cause A: **{sub}:** sub-field is empty" in errors

    def test_the_shipped_fallback_skeleton_is_still_refused(self):
        result = RunbookValidator().validate_content(
            SuggestionService.fallback_template("case-1241")
        )
        assert not result.passed
        for sub in ("Statement", "Indicators", "Interventions"):
            assert f"Cause A: **{sub}:** sub-field is empty" in result.errors


# =============================================================================
# 2. The comment must not corrupt content that IS real
# =============================================================================


class TestRealContentSurvives:
    def test_a_comment_carrying_prose_does_not_corrupt_a_real_value(self):
        fields = g.parse_cause_subfields(
            "**Statement:** The pool was exhausted by leaked sessions.\n"
            "<!-- reviewer: confirm this against the 2026-03 incident -->\n"
            "**Indicators:**\n- root: [Step 1] pool_active == pool_max\n",
            ["Statement", "Indicators"],
        )
        assert fields["Statement"] == "The pool was exhausted by leaked sessions."
        assert fields["Indicators"] == "- root: [Step 1] pool_active == pool_max"

    def test_a_comment_after_a_real_value_is_not_eaten_into_it(self):
        """Pre-fix the trailing comment was appended to the value verbatim —
        ``'The pool was exhausted.\\n<!-- todo: cite the ticket -->'``."""
        fields = g.parse_cause_subfields(
            "**Statement:** The pool was exhausted.\n<!-- todo: cite the ticket -->\n",
            ["Statement", "Indicators"],
        )
        assert fields["Statement"] == "The pool was exhausted."

    def test_a_multi_line_comment_is_stripped_whole(self):
        fields = g.parse_cause_subfields(
            "**Statement:** Sessions leak.\n"
            "<!--\n"
            "  a note that runs\n"
            "  across several lines\n"
            "  and mentions **Indicators:** in passing\n"
            "-->\n"
            "**Indicators:**\n- root: [Step 1] climbing\n",
            ["Statement", "Indicators"],
        )
        assert fields["Statement"] == "Sessions leak."
        assert fields["Indicators"] == "- root: [Step 1] climbing"

    def test_an_inline_comment_mid_value_leaves_the_prose_either_side(self):
        fields = g.parse_cause_subfields(
            "**Chain:**\n- root: Connection leak <!-- match: leak --> in the handler\n",
            ["Statement", "Chain"],
        )
        assert "Connection leak" in fields["Chain"]
        assert "in the handler" in fields["Chain"]
        assert "<!--" not in fields["Chain"]


# =============================================================================
# 3. Regression witnesses: a quoted marker is CONTENT, not a comment
# =============================================================================


class TestQuotedCommentTokensAreNotComments:
    """The first repair deleted every ``<!--…-->`` span unconditionally. Both of
    these were measured on that build and are why the mask is code-aware."""

    def test_a_code_span_quoting_an_opener_keeps_the_whole_statement(self):
        text = (
            "**Statement:** A stray `<!--` opener with no matching `-->` "
            "swallows the rest of the config.\n"
        )
        fields = g.parse_cause_subfields(text, ["Statement", "Indicators"])
        assert fields["Statement"] == (
            "A stray `<!--` opener with no matching `-->` swallows the rest of "
            "the config."
        )

    def test_literal_tokens_in_two_causes_do_not_blank_the_second(self):
        """The span rule, not an input edge case: a naive mask runs from the
        first ``<!--`` to the next ``-->`` wherever they are, so Cause Z's
        heading was masked away and its body folded into Cause A's block."""
        doc = _wrap(
            "**Statement:** A config line containing `<!--` is not a comment.\n"
            "**Indicators:**\n- [Symptom] x\n"
            "**Interventions:**\n"
            "- **remediation** (root): fix. **Verification:** ok.\n"
        ).replace(
            "**Statement:** None of the documented causes match.",
            "**Statement:** A config line containing `-->` is not a comment either.",
            1,
        )
        assert [letter for letter, _n, _b in _iter_cause_blocks(doc)] == ["A", "Z"]
        assert [b.letter for b in g.iter_cause_blocks(doc)] == ["A", "Z"]

    def test_a_fenced_block_containing_markers_is_not_a_comment(self):
        doc = _wrap(
            "**Statement:** Templates emit stray markers.\n"
            "**Indicators:**\n- [Symptom] markers render\n"
            "**Interventions:**\n"
            "- **remediation** (root): balance them.\n"
            "  ```bash\n"
            "  grep -rn -e '<!--' -e '-->' templates/\n"
            "  ```\n"
            "  **Verification:** counts balance.\n"
        )
        assert [letter for letter, _n, _b in _iter_cause_blocks(doc)] == ["A", "Z"]

    def test_an_unterminated_opener_swallows_nothing(self):
        """Markdown renders the remainder as text when a comment is never
        closed; swallowing the document is the one failure worse than not
        masking at all."""
        text = "**Statement:** An opener <!-- that never closes.\n"
        fields = g.parse_cause_subfields(text, ["Statement"])
        assert fields["Statement"] == "An opener <!-- that never closes."

    def test_a_real_comment_survives_a_quoted_closer_before_it(self):
        """The closer search must skip guarded positions on BOTH sides, or a
        real comment is truncated early by a ``-->`` someone quoted."""
        masked = g.mask_html_comments("a `-->` b <!-- hidden --> c")
        assert "`-->`" in masked
        assert "hidden" not in masked
        assert masked.endswith(" c")


# =============================================================================
# 4. Enumeration: a Cause written inside a comment is not a Cause
# =============================================================================


class TestCommentedCauseIsNotEnumerated:
    _DOC = (
        "## Causes\n\n"
        "<!-- Example of a whole cause block, delete once you have written yours:\n"
        "### Cause A: The pool was exhausted\n"
        "**Statement:** Leaked sessions consumed the pool.\n"
        "**Indicators:**\n- root: [Step 1] pool_active == pool_max\n"
        "**Interventions:**\n- **remediation** (root): Restart.\n"
        "-->\n\n"
        "### Cause Z: Unidentified\n"
        "**Statement:** None of the documented causes match.\n"
        "**Indicators:**\n- [Default]\n"
        "**Interventions:**\n- **mitigation** (D): Escalate. **Verification:** N/A.\n"
    )

    def test_the_validator_does_not_enumerate_it(self):
        assert [letter for letter, _n, _b in _iter_cause_blocks(self._DOC)] == ["Z"]

    def test_the_extractor_does_not_write_it_to_the_corpus(self):
        assert [b.letter for b in g.iter_cause_blocks(self._DOC)] == ["Z"]

    def test_a_section_holding_only_a_commented_cause_fails_the_structure_gate(self):
        only_commented = self._DOC[: self._DOC.index("### Cause Z:")]
        content = (
            "## Symptom Recognition\n- x\n\n## Applicability\nx\n\n"
            "## Diagnostic Steps\n\n### Step 1: x\n\n"
            f"{only_commented}\n## Prevention\n- x\n\n## Sources\n- x\n"
        )
        errors: list[str] = []
        RunbookValidator()._validate_structure(content, errors, [])
        assert (
            "## Causes section must contain at least one ### Cause subsection" in errors
        )


# =============================================================================
# 5. A comment that opens BEFORE the section heading — the full bypass
# =============================================================================


class TestCommentOpeningBeforeTheSectionHeading:
    """Masking the section BODY is not enough: ``CAUSES_SECTION_RE`` has to run
    over the masked document, or a comment opening earlier leaves the whole
    section live. Measured — this was a complete gate bypass, not merely an
    extractor defect: a well-formed runbook with its entire ``## Causes``
    section commented out returned ``passed=True, errors=[]``."""

    _COMMENTED = _runbook(
        "<!-- DRAFT, do not publish yet:\n## Causes\n\n" + _REAL_CAUSES + "-->\n"
    )

    def test_the_control_runbook_really_does_pass(self):
        """Without this the bypass test below would also pass on a fixture that
        was simply invalid for unrelated reasons."""
        result = RunbookValidator().validate_content(
            _runbook("## Causes\n\n" + _REAL_CAUSES)
        )
        assert result.passed, result.errors

    def test_the_gate_refuses_a_runbook_whose_causes_are_all_commented(self):
        result = RunbookValidator().validate_content(self._COMMENTED)
        assert not result.passed
        assert (
            "## Causes section must contain at least one ### Cause subsection"
            in result.errors
        )

    def test_the_extractor_seeds_nothing_from_it(self):
        assert g.iter_cause_blocks(self._COMMENTED) == []


# =============================================================================
# 6. The mask is length-preserving — the chunk-size gate keeps seeing raw text
# =============================================================================


class TestMaskPreservesOffsets:
    def test_mask_is_the_identity_on_comment_free_text(self):
        text = "## Causes\n\n### Cause A: x\n**Statement:** y\n"
        assert g.mask_html_comments(text) == text

    def test_mask_preserves_length_and_line_structure(self):
        text = "a <!-- one\ntwo\nthree --> b\n"
        masked = g.mask_html_comments(text)
        assert len(masked) == len(text)
        assert masked.count("\n") == text.count("\n")
        assert [len(line) for line in masked.split("\n")] == [
            len(line) for line in text.split("\n")
        ]
        assert "<!--" not in masked

    def test_mask_hides_a_label_without_moving_the_text_after_it(self):
        text = "<!-- **Statement:** example -->\n**Statement:** real\n"
        masked = g.mask_html_comments(text)
        assert masked.index("**Statement:** real") == text.index("**Statement:** real")
        assert "**Statement:** example" not in masked

    def test_the_yielded_cause_block_is_the_raw_text_comments_included(self):
        """Load-bearing, and the reason enumeration masks rather than deletes:
        ``_check_chunk_bounds`` measures this block against ``ContentChunker``'s
        size bounds and split-boundary pattern, and the chunker sees the raw
        markdown. A deleting strip would shift every offset and hand the gate a
        block that is not what gets chunked."""
        content = _wrap(
            "<!-- a fairly long authoring note that occupies real characters -->\n"
            "**Statement:** Sessions leak.\n"
            "**Indicators:**\n- root: [Step 1] climbing\n"
            "**Interventions:**\n- **remediation** (root): restart. "
            "**Verification:** ok.\n"
        )
        blocks = {
            letter: block
            for letter, _n, block in _iter_cause_blocks(content, include_heading=True)
        }
        start = content.index("### Cause A:")
        end = content.index("### Cause Z:")
        assert blocks["A"] == content[start:end]
        assert "<!-- a fairly long authoring note" in blocks["A"]


# =============================================================================
# 7. The malformed-heading guard must agree with the enumerator
# =============================================================================


def test_a_heading_the_enumerator_drops_is_never_dropped_silently():
    """``_flag_malformed_cause_headings`` exists to turn a silent drop into an
    error, so it must see the same text the enumerator does.

    Measured on the first repair, where it still DELETED comments while
    enumeration masked them: deletion splices the lines either side together,
    the heading stopped starting a line, and the Cause was dropped by the
    enumerator with no diagnostic from the guard — ``enumerated: []`` and
    ``malformed-heading errors: []``.
    """
    doc = (
        "## Causes\n\n"
        "prose <!--\n"
        "--> ### Cause A: Joined by deletion\n"
        "**Statement:** x.\n**Indicators:**\n- [Symptom] y\n"
        "**Interventions:**\n- **remediation** (root): z. **Verification:** ok.\n"
    )
    errors: list[str] = []
    RunbookValidator()._validate_cause_graph(doc, errors, [])

    assert [letter for letter, _n, _b in _iter_cause_blocks(doc)] == []
    assert any("Malformed Cause heading" in e for e in errors), errors


# =============================================================================
# 9. Structural guard — what stops a FIFTH call site reintroducing this
# =============================================================================
#
# ``CAUSE_HEADING_RE`` is comment-blind on its own and must stay exported: the
# cross-repo drift guard pins its ``.pattern``/``.flags``. So the safety cannot
# live in the primitive.
#
# A masking WRAPPER was considered and rejected as the whole answer. It cannot
# stop a new caller reaching for the bare regex that has to remain exported, so
# it improves the odds without closing the hole. What IS in place is stronger
# than a wrapper: the head->terminus walk now lives once, in
# ``runbook_grammar.iter_cause_blocks``, and both consumers delegate — so a new
# consumer inherits the masking by using the enumerator rather than by
# remembering a rule. This guard is what keeps that true: every remaining
# ``CAUSE_HEADING_RE.<matcher>()`` in the shipped tree must mask its input
# syntactically or be classified below with a reason, and ``CAUSES_SECTION_RE``
# — the section scope — may not be matched outside the grammar module at all.

_MATCHER_METHODS = {
    "search",
    "match",
    "fullmatch",
    "finditer",
    "findall",
    "split",
    "sub",
    "subn",
}

_COMMENT_SAFE_BY_CONSTRUCTION = {
    "runbook_grammar::iter_cause_blocks": (
        "the masking enumerator itself — its ``masked_body`` comes from "
        "``causes_section``, which masks the whole document before locating the "
        "section. This is the one site allowed to hold the walk."
    ),
    "runbook_validator::_flag_malformed_cause_headings": (
        "its input is a line from ``scan``, built from the already-masked "
        "``_causes_section_body``. Masking again would be a no-op, and DELETING "
        "again is the measured regression this guard's own test pins."
    ),
}

# Positive control for the walk (see the vacuity test): the sites that exist
# today. A walk that finds FEWER is broken.
_EXPECTED_MATCHER_SITES_AT_LEAST = 2

_PACKAGE_ROOT = REPO_ROOT / "faultmaven"
_GRAMMAR_MODULE = "runbook_grammar"


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the name of the function that lexically contains it."""
    owner: dict[ast.AST, str] = {}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            owner[child] = name
            walk(child, name)

    walk(tree, "<module>")
    return owner


def _matcher_sites(regex_name: str) -> list[tuple[str, int, str, bool]]:
    """Every ``<regex_name>.<matcher>(...)`` in the shipped package.

    Returns ``(key, lineno, relpath, masked)`` where ``key`` is
    ``"<module stem>::<enclosing function>"`` — line numbers move, that key does
    not — and ``masked`` says whether the first argument is syntactically
    ``mask_html_comments(...)``.

    KNOWN LIMIT, stated rather than papered over: the match is on the NAME, so
    an import alias (``CAUSES_SECTION_RE as _STRAY``) slips it. That is an
    evasion, not a mistake — a developer adding a stray walk writes the symbol
    directly and is caught, which is the case this guards. Widening to resolve
    aliases would mean tracking import bindings per module for no gain against
    the failure mode that actually occurred twice in #1241. If a stray walk ever
    does arrive under an alias, resolve bindings then.
    """
    sites: list[tuple[str, int, str, bool]] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or fn.attr not in _MATCHER_METHODS:
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == regex_name):
                continue
            masked = bool(
                node.args
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Name)
                and node.args[0].func.id == "mask_html_comments"
            )
            key = f"{path.stem}::{owner.get(node, '<module>')}"
            sites.append(
                (key, node.lineno, str(path.relative_to(_PACKAGE_ROOT)), masked)
            )
    return sites


class TestEveryCauseHeadingConsumerMasks:
    def test_the_walk_actually_finds_the_call_sites(self):
        """Positive control. Every pin below asserts a filtered list is empty,
        and an empty list is exactly what a broken walk produces."""
        sites = _matcher_sites("CAUSE_HEADING_RE")
        assert len(sites) >= _EXPECTED_MATCHER_SITES_AT_LEAST, (
            f"the AST walk found only {len(sites)} CAUSE_HEADING_RE matcher "
            f"call(s), expected at least {_EXPECTED_MATCHER_SITES_AT_LEAST} — "
            "the walk is broken, so the pins below pass vacuously"
        )

    def test_no_consumer_matches_unmasked_text(self):
        offenders = [
            f"{rel}:{line} ({key})"
            for key, line, rel, masked in _matcher_sites("CAUSE_HEADING_RE")
            if not masked and key not in _COMMENT_SAFE_BY_CONSTRUCTION
        ]
        assert not offenders, (
            "CAUSE_HEADING_RE is blind to HTML comments (#1241), so a "
            "commented-out '### Cause X:' example is enumerated as a real "
            "Cause. Prefer routing through runbook_grammar.iter_cause_blocks, "
            "which masks for you; otherwise pass mask_html_comments() "
            "explicitly, or — if the input cannot contain a comment — add the "
            "site to _COMMENT_SAFE_BY_CONSTRUCTION with the reason why. "
            f"Unclassified: {offenders}"
        )

    def test_the_section_walk_lives_only_in_the_grammar(self):
        """The stronger half of the guard, and the one the class kept escaping:
        a consumer that scopes its own ``## Causes`` section re-decides where
        masking happens. Two sites did, and #1241 escaped both times."""
        strays = [
            f"{rel}:{line} ({key})"
            for key, line, rel, _masked in _matcher_sites("CAUSES_SECTION_RE")
            if not key.startswith(f"{_GRAMMAR_MODULE}::")
        ]
        assert not strays, (
            "CAUSES_SECTION_RE is matched outside runbook_grammar. The "
            "head->terminus walk lives once, in iter_cause_blocks, so the gate "
            "and the extractor cannot disagree and the comment mask is applied "
            f"in exactly one place. Route through it instead. Strays: {strays}"
        )

    def test_no_stale_exemptions(self):
        """An exemption naming no live site is a claim nobody checks any more —
        and it would silently cover a NEW function that later takes the name."""
        live = {key for key, _line, _rel, _masked in _matcher_sites("CAUSE_HEADING_RE")}
        stale = sorted(set(_COMMENT_SAFE_BY_CONSTRUCTION) - live)
        assert not stale, f"exemptions naming no live call site: {stale}"


# =============================================================================
# 10. Corpus — the shipped pack, and the quoted-markers witness
# =============================================================================


def _pack_runbooks() -> list[Path]:
    if not PACK_RUNBOOKS.exists():
        pytest.skip(f"shipped pack not present at {PACK_RUNBOOKS} (partial checkout)")
    found = sorted(PACK_RUNBOOKS.rglob("*.md"))
    assert found, f"{PACK_RUNBOOKS} exists but holds no runbooks"
    return found


class TestShippedPackIsUnchanged:
    """Audited before landing: no shipped runbook relied on the bug.

    ``test_the_fix_changes_nothing_in_the_shipped_pack`` proves it the only way
    that stays true as the corpus grows — by re-running the corpus with the fix
    DISABLED (the pre-#1241 code path, reconstructed by neutralising the mask
    rather than by re-encoding the grammar in a test) and demanding the same
    verdicts and the same extracted records.
    """

    def test_every_shipped_runbook_still_passes_the_gate(self):
        v = RunbookValidator()
        failures = [
            (md.name, v.validate_content(md.read_text(encoding="utf-8")).errors)
            for md in _pack_runbooks()
        ]
        failures = [(n, e) for n, e in failures if e]
        assert not failures, f"{len(failures)} shipped runbook(s) now fail: {failures}"

    def test_the_fix_changes_nothing_in_the_shipped_pack(self, monkeypatch):
        texts = [md.read_text(encoding="utf-8") for md in _pack_runbooks()]
        v = RunbookValidator()

        def snapshot() -> list[tuple]:
            return [
                (r.passed, tuple(r.errors), _cause_records(t))
                for r, t in ((v.validate_content(t), t) for t in texts)
            ]

        after = snapshot()
        # Neutralise the fix at its single source: every consumer routes through
        # ``mask_html_comments``, so making it the identity restores the
        # pre-#1241 reading exactly.
        monkeypatch.setattr(g, "mask_html_comments", lambda s: s)
        before = snapshot()

        assert before == after

    def test_neutralising_the_fix_really_does_restore_the_bug(self, monkeypatch):
        """Guard on the guard above: if the monkeypatch stopped reaching the
        code, the comparison would be the fixed corpus against itself and would
        pass vacuously forever."""
        probe = (
            "<!-- Fill this in, e.g. **Statement:** The pool was exhausted. -->\n"
            "**Statement:**\n**Evidence:**\n**Fix:**\n"
        )
        names = ["Statement", "Evidence", "Fix"]
        assert g.parse_cause_subfields(probe, names)["Statement"] == ""

        monkeypatch.setattr(g, "mask_html_comments", lambda s: s)

        assert g.parse_cause_subfields(probe, names)["Statement"] == (
            "The pool was exhausted. -->\n**Statement:**"
        )
        assert [
            r["cause_letter"]
            for r in _cause_records(TestCommentedCauseIsNotEnumerated._DOC)
        ] == ["A", "Z"]


class TestQuotedMarkersRunbookIsInTheCorpus:
    """A shipped-shaped document that quotes ``<!--`` and ``-->`` as content.

    Deliberately NOT part of the "fix changes nothing" set above: it is the
    witness for the two regressions the first repair introduced, so it is
    exactly a document whose parse the naive strip DID change. Nothing in the
    suite carried such a document before, which is why neither regression was
    caught by tests.
    """

    def _text(self) -> str:
        if not QUOTED_TOKENS_RUNBOOK.exists():
            pytest.skip(f"fixture missing: {QUOTED_TOKENS_RUNBOOK}")
        return QUOTED_TOKENS_RUNBOOK.read_text(encoding="utf-8")

    def test_it_passes_the_gate(self):
        result = RunbookValidator().validate_content(self._text())
        assert result.passed, result.errors

    def test_every_cause_survives_and_keeps_its_markers(self):
        records = {r["cause_letter"]: r for r in _cause_records(self._text())}
        assert sorted(records) == ["A", "B", "Z"]
        assert "`<!--`" in records["A"]["cause_statement"]
        assert "`-->`" in records["A"]["cause_statement"]
        assert records["A"]["cause_statement"].endswith("never rendered.")
        assert "`-->`" in records["B"]["cause_statement"]

    def test_its_real_authoring_comment_is_still_ignored(self):
        assert "<!-- Authoring note" in self._text()
        masked = g.mask_html_comments(self._text())
        assert "Authoring note" not in masked
        # …while the quoted markers in content are untouched.
        assert "`<!--`" in masked and "`-->`" in masked

    def test_a_naive_strip_visibly_destroys_it(self, monkeypatch):
        """The positive control for this whole fixture.

        A document that passes either way would prove nothing about the
        code-awareness. Substituting the naive ``<!--.*?-->`` sweep the first
        repair used — no fence or code-span exclusion — must visibly wreck it,
        or this fixture is decoration.
        """
        text = self._text()
        naive = re.compile(r"<!--.*?-->", re.DOTALL)
        chars = re.compile(r"[^\n]")
        monkeypatch.setattr(
            g,
            "mask_html_comments",
            lambda t: naive.sub(lambda m: chars.sub(" ", m.group(0)), t),
        )

        records = {r["cause_letter"]: r for r in _cause_records(text)}
        # Cause B is swallowed: the span runs from the ``<!--`` quoted in Cause
        # A's Statement to the ``-->`` quoted in Cause B's.
        assert sorted(records) == ["A", "Z"]
        # …and what is left of Cause A's Statement has had its middle blanked.
        assert "`<!--`" not in records["A"]["cause_statement"]
        assert not RunbookValidator().validate_content(text).passed


def test_the_new_regexes_are_pinned_against_drift():
    """``test_runbook_grammar`` freezes every mirrored pattern; the two the mask
    added must not be the exception. Kept here as well as there because these
    two are what a mirror lacking the countermeasure would be missing."""
    assert g.CODE_FENCE_LINE_RE.pattern == r"^[ \t]{0,3}(`{3,}|~{3,})"
    assert g.CODE_SPAN_RE.pattern == r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)"
    assert g.CODE_SPAN_RE.flags & re.DOTALL
    assert g.COMMENT_BODY_CHAR_RE.pattern == r"[^\n]"
