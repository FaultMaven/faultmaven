"""#1241 — the v4 cause grammar must be blind to HTML comments.

Measured on ``main`` (859a8d47c), ``parse_cause_subfields`` did not strip HTML
comments, so a sub-field label written INSIDE a comment was read as the real
value::

    >>> parse_cause_subfields(
    ...     "<!-- Fill this in, e.g. **Statement:** The pool was exhausted. -->\\n"
    ...     "**Statement:**\\n**Evidence:**\\n**Fix:**\\n",
    ...     ["Statement", "Evidence", "Fix"])
    {'Statement': 'The pool was exhausted. -->\\n**Statement:**', 'Evidence': '', 'Fix': ''}

Every real field is empty, yet ``Statement`` comes back non-empty. That is a
GATE BYPASS, not a formatting nit: #1214 made ``RunbookValidator`` load-bearing
(LLM-extracted content may not enter the corpus ungated), and a Cause whose
``**Statement:**`` is genuinely empty must be refused. A comment anywhere above
it satisfied the check instead — so any authoring template carrying
commented-out worked examples, which is the natural way to write a template,
handed the author a runbook that passed while being empty. Found exactly that
way during #1226 (PR #1238), which worked around it at its own call site.

The same blindness reached ENUMERATION: a whole ``### Cause A:`` block written
inside a comment was counted as a real Cause and extracted into the corpus as
real knowledge. Both are fixed in ``runbook_grammar`` so the repair lives in one
place — deletion for sub-field VALUES, a length-preserving MASK for the callers
that need heading offsets to keep matching the raw markdown the chunker sees.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from faultmaven.modules.knowledge.domain.services import (
    knowledge_service as knowledge_service_mod,
)
from faultmaven.modules.knowledge.domain.services import (
    runbook_cause_extractor as extractor_mod,
)
from faultmaven.modules.knowledge.domain.services import runbook_grammar as g
from faultmaven.modules.knowledge.domain.services import (
    runbook_validator as validator_mod,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    _matched_cause_letters,
    _read_stamped_cause_letters,
)
from faultmaven.modules.knowledge.domain.services.runbook_cause_extractor import (
    extract_causes,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
    _iter_cause_blocks,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)

pytestmark = pytest.mark.unit

# tests/unit/modules/knowledge/<file> -> parents[4] == repo root
PACK_RUNBOOKS = (
    Path(__file__).resolve().parents[4]
    / "resources"
    / "knowledge"
    / "pack"
    / "runbooks"
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


# =============================================================================
# The reported defect: a label inside a comment is read as the real field
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
        """The skeleton whose hint comment now spells the labels (the #1226
        workaround, removed here) must still fail the whole gate."""
        result = RunbookValidator().validate_content(
            SuggestionService.fallback_template("case-1241")
        )
        assert not result.passed
        for sub in ("Statement", "Indicators", "Interventions"):
            assert f"Cause A: **{sub}:** sub-field is empty" in result.errors


# =============================================================================
# The comment must not corrupt content that IS real
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
            "**Statement:** The pool was exhausted.\n"
            "<!-- todo: cite the ticket -->\n",
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
# Enumeration: a Cause written inside a comment is not a Cause
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
        assert [r["cause_letter"] for r in extract_causes(self._DOC)] == ["Z"]

    def test_a_section_holding_only_a_commented_cause_fails_the_structure_gate(self):
        """It parses to zero Causes, so the gate must say so rather than count
        the comment as a subsection."""
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
# The mask is length-preserving — the chunk-size gate must keep seeing raw text
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
        """Load-bearing, and the reason the enumeration path masks rather than
        deletes: ``_check_chunk_bounds`` measures this block against
        ``ContentChunker``'s size bounds and split-boundary pattern, and the
        chunker sees the raw markdown. A deleting strip would shift every offset
        and hand the gate a block that is not what gets chunked."""
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
# Corpus audit — the shipped pack must parse exactly as it did before the fix
# =============================================================================


def _pack_runbooks() -> list[Path]:
    if not PACK_RUNBOOKS.exists():
        return []
    return sorted(PACK_RUNBOOKS.rglob("*.md"))


class TestShippedPackIsUnchanged:
    """Audited before landing #1241: no shipped runbook relied on the bug.

    ``test_the_fix_changes_nothing_in_the_shipped_pack`` proves it the only way
    that stays true as the corpus grows — by re-running the corpus with the fix
    DISABLED (the pre-#1241 code path, reconstructed by neutralising the two
    additions rather than by re-encoding the grammar in a test) and demanding
    the same verdicts and the same extracted records.
    """

    def test_every_shipped_runbook_still_passes_the_gate(self):
        runbooks = _pack_runbooks()
        assert runbooks, f"no shipped runbooks found under {PACK_RUNBOOKS}"
        v = RunbookValidator()
        failures = [
            (md.name, v.validate_content(md.read_text(encoding="utf-8")).errors)
            for md in runbooks
        ]
        failures = [(n, e) for n, e in failures if e]
        assert not failures, f"{len(failures)} shipped runbook(s) now fail: {failures}"

    def test_the_fix_changes_nothing_in_the_shipped_pack(self, monkeypatch):
        runbooks = _pack_runbooks()
        assert runbooks, f"no shipped runbooks found under {PACK_RUNBOOKS}"
        texts = [md.read_text(encoding="utf-8") for md in runbooks]

        v = RunbookValidator()

        def snapshot() -> list[tuple]:
            out = []
            for t in texts:
                r = v.validate_content(t)
                out.append((r.passed, tuple(r.errors), extract_causes(t)))
            return out

        after = snapshot()

        # Neutralise the fix: the sub-field strip (a module-level regex the
        # parser dereferences at call time) and the enumeration mask (imported
        # by name into each consumer, so patched in each).
        monkeypatch.setattr(g, "HTML_COMMENT_RE", re.compile(r"(?!x)x"))
        monkeypatch.setattr(extractor_mod, "mask_html_comments", lambda s: s)
        monkeypatch.setattr(validator_mod, "mask_html_comments", lambda s: s)
        before = snapshot()

        assert before == after

    def test_neutralising_the_fix_really_does_restore_the_bug(self, monkeypatch):
        """Guard on the guard above: if the monkeypatches stopped reaching the
        code, ``test_the_fix_changes_nothing_in_the_shipped_pack`` would compare
        the fixed corpus with itself and pass vacuously forever."""
        probe = (
            "<!-- Fill this in, e.g. **Statement:** The pool was exhausted. -->\n"
            "**Statement:**\n**Evidence:**\n**Fix:**\n"
        )
        names = ["Statement", "Evidence", "Fix"]
        assert g.parse_cause_subfields(probe, names)["Statement"] == ""

        monkeypatch.setattr(g, "HTML_COMMENT_RE", re.compile(r"(?!x)x"))
        monkeypatch.setattr(extractor_mod, "mask_html_comments", lambda s: s)
        monkeypatch.setattr(validator_mod, "mask_html_comments", lambda s: s)

        assert g.parse_cause_subfields(probe, names)["Statement"] == (
            "The pool was exhausted. -->\n**Statement:**"
        )
        assert [
            r["cause_letter"]
            for r in extract_causes(TestCommentedCauseIsNotEnumerated._DOC)
        ] == ["A", "Z"]


# =============================================================================
# The chunk-stamping path — the fourth CAUSE_HEADING_RE consumer
# =============================================================================


class TestChunkStampingIsCommentBlind:
    """``_matched_cause_letters`` mints the join key the KB cause seeder reads.

    It ran ``CAUSE_HEADING_RE.findall`` on raw chunk text, so a commented-out
    ``### Cause A:`` example sitting beside a real Cause stamped a phantom ``A``
    and the seeder read "retrieval surfaced Cause A" for a cause with no
    ``metadata["causes"]`` record to join to. Nothing shipped reaches it — no
    runbook in the corpus carries such a heading — but it is the same class,
    found while closing it.
    """

    _CHUNK = (
        "### Cause Z: Unidentified\n**Statement:** unknown\n\n"
        "<!--\n### Cause A: commented-out example\n"
        "**Statement:** never real\n-->\n"
    )

    def test_a_commented_heading_does_not_stamp_a_phantom_letter(self):
        assert _matched_cause_letters(self._CHUNK) == ["Z"]

    def test_a_real_heading_beside_a_comment_still_stamps(self):
        """The complement: masking must not cost a letter that is genuinely
        there, or the seeder loses the join it exists to make."""
        chunk = (
            "<!-- authoring note about the cause below -->\n"
            "### Cause D: OOMKilled\n**Statement:** the container exceeded "
            "its memory limit.\n"
        )
        assert _matched_cause_letters(chunk) == ["D"]

    def test_the_stamp_identity_moved_so_old_stamps_are_re_derived(self):
        """The source fix alone repairs nothing already stamped.

        ``_read_stamped_cause_letters`` falls back to parsing on key ABSENCE
        only, so a present-but-wrong ``cause_letters`` stamp is read forever.
        ``chunk_stamp_identity`` is derived from the pattern (unchanged here)
        plus ``CHUNK_STAMP_SCHEMA``, so the schema bump is the only thing that
        marks those rows stale for the pack gate and the fm#1108 restamp sweep.
        """
        assert knowledge_service_mod.CHUNK_STAMP_SCHEMA >= 2

    def test_a_present_but_wrong_stamp_is_never_re_parsed(self):
        """Why the bump above is load-bearing rather than tidy — pin the read
        path's key-absence rule, which is what makes a bad stamp permanent."""
        phantom = {"cause_letters": "Z,A"}
        assert _read_stamped_cause_letters(phantom, self._CHUNK) == ["Z", "A"]
        # Only key ABSENCE re-parses (and then gets the corrected answer).
        assert _read_stamped_cause_letters({}, self._CHUNK) == ["Z"]


# =============================================================================
# Structural guard — what stops a FIFTH call site reintroducing this
# =============================================================================
#
# ``CAUSE_HEADING_RE`` is comment-blind on its own and must stay exported: the
# cross-repo drift guard pins its ``.pattern``/``.flags``, ``chunk_stamp_identity``
# hashes them, and one site legitimately matches a string it built itself. So the
# safety cannot live in the primitive; today it lives in each caller remembering
# to mask, which is a convention — and this lane found a caller that had not.
#
# A masking WRAPPER was considered and rejected. It would make a new caller
# correct by default, but it cannot stop one reaching for the bare regex that has
# to remain exported, so it improves the odds without closing the hole. It would
# also HIDE the raw-vs-masked distinction that ``_iter_cause_blocks`` and
# ``extract_causes`` depend on being visible: they deliberately find headings in
# the masked copy and slice the RAW text, because the chunk-size gate must
# measure exactly what the chunker sees.
#
# So the guard is an enumeration instead: every ``CAUSE_HEADING_RE.<matcher>()``
# in the shipped tree must either mask its input syntactically, or be classified
# below with a reason. A new site is neither, and fails.

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
    "knowledge_service::_letter_can_head_a_cause": (
        "matches a heading string the function builds itself from a letter "
        '(f"### Cause {letter}: name"); there is no document text in it, so '
        "there is no comment for one to hide in."
    ),
    "runbook_validator::_flag_malformed_cause_headings": (
        "its input is a line taken from ``scan``, which the same function "
        'already built as ``_CODE_FENCE_RE.sub("", HTML_COMMENT_RE.sub("", '
        "causes_body))`` — comments are gone before the loose scan that "
        "produces the line, so masking again would be a no-op."
    ),
}

# Positive control for the walk (see the vacuity test): the sites that exist
# today. Raising this is fine; a walk that finds FEWER is broken.
_EXPECTED_MATCHER_SITES_AT_LEAST = 6

_PACKAGE_ROOT = Path(__file__).resolve().parents[4] / "faultmaven"


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


def _cause_heading_matcher_sites() -> list[tuple[str, int, str, bool]]:
    """Every ``CAUSE_HEADING_RE.<matcher>(...)`` in the shipped package.

    Returns ``(key, lineno, relpath, masked)`` where ``key`` is
    ``"<module stem>::<enclosing function>"`` — line numbers move, that key does
    not — and ``masked`` says whether the first argument is syntactically
    ``mask_html_comments(...)``.
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
            if not (
                isinstance(fn.value, ast.Name) and fn.value.id == "CAUSE_HEADING_RE"
            ):
                continue
            masked = bool(
                node.args
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Name)
                and node.args[0].func.id == "mask_html_comments"
            )
            key = f"{path.stem}::{owner.get(node, '<module>')}"
            rel = str(path.relative_to(_PACKAGE_ROOT))
            sites.append((key, node.lineno, rel, masked))
    return sites


class TestEveryCauseHeadingConsumerMasks:
    def test_the_walk_actually_finds_the_call_sites(self):
        """Positive control. The pin below asserts a filtered list is empty, and
        an empty list is exactly what a broken walk produces."""
        sites = _cause_heading_matcher_sites()
        assert len(sites) >= _EXPECTED_MATCHER_SITES_AT_LEAST, (
            f"the AST walk found only {len(sites)} CAUSE_HEADING_RE matcher "
            f"call(s), expected at least {_EXPECTED_MATCHER_SITES_AT_LEAST} — "
            "the walk is broken, so the pin below passes vacuously"
        )

    def test_no_consumer_matches_unmasked_text(self):
        offenders = [
            f"{rel}:{line} ({key})"
            for key, line, rel, masked in _cause_heading_matcher_sites()
            if not masked and key not in _COMMENT_SAFE_BY_CONSTRUCTION
        ]
        assert not offenders, (
            "CAUSE_HEADING_RE is blind to HTML comments (#1241), so a "
            "commented-out '### Cause X:' example is enumerated as a real "
            "Cause. Every consumer must pass its input through "
            "mask_html_comments() — or, if the input cannot contain a comment, "
            "be added to _COMMENT_SAFE_BY_CONSTRUCTION with the reason why. "
            f"Unclassified: {offenders}"
        )

    def test_no_stale_exemptions(self):
        """An exemption naming no live site is a claim nobody checks any more —
        and it would silently cover a NEW function that later takes the name."""
        live = {key for key, _line, _rel, _masked in _cause_heading_matcher_sites()}
        stale = sorted(set(_COMMENT_SAFE_BY_CONSTRUCTION) - live)
        assert not stale, f"exemptions naming no live call site: {stale}"
