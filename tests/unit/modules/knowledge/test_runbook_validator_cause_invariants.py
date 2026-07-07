"""Cause-Statement match-surface invariants in the backend runbook validator (#545).

The Statement is the load-bearing match surface (per runbook-cause-matching.md
§2.1); these guard it: no operator-step marker leaking in, and siblings must be
discriminative. Bias is hard toward WARN — only unambiguous mechanical tells
block (verified never to fire on the 91 shipped runbooks).
"""

import pytest

from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
    check_cause_statement_invariants,
)

pytestmark = pytest.mark.unit


class TestCheckCauseStatementInvariants:
    def test_clean_discriminative_statements_pass(self):
        errs, warns = check_cause_statement_invariants(
            [
                ("A", "Idle transactions hold connections, exhausting the pool."),
                ("B", "A slow analytical query holds row locks blocking writers."),
            ]
        )
        assert errs == [] and warns == []

    def test_step_marker_in_statement_blocks(self):
        errs, warns = check_cause_statement_invariants(
            [("A", "[Step 3] kubectl describe job shows BackoffLimitExceeded")]
        )
        assert any("operator-step marker" in e for e in errs)

    def test_exact_duplicate_siblings_block(self):
        errs, _ = check_cause_statement_invariants(
            [("A", "The disk volume is full."), ("B", "the disk volume is full")]
        )
        assert any("identical Statements" in e for e in errs)

    def test_near_duplicate_siblings_warn_not_block(self):
        # High lexical overlap but not identical → warn, never block.
        errs, warns = check_cause_statement_invariants(
            [
                (
                    "A",
                    "The connection pool is exhausted by idle transactions left open",
                ),
                (
                    "B",
                    "The connection pool is exhausted by idle transactions not closed",
                ),
            ]
        )
        assert errs == []
        assert any("near-duplicate" in w for w in warns)

    def test_empty_statement_is_not_flagged_here(self):
        # Empty is a separate required-field concern (the caller blocks it); the
        # invariant function skips it rather than double-reporting.
        errs, warns = check_cause_statement_invariants([("A", "  "), ("B", "real")])
        assert errs == [] and warns == []

    def test_lowercase_step_marker_blocks(self):
        # Case-insensitive: a lowercased [step N] leak must not escape the block.
        errs, _ = check_cause_statement_invariants([("A", "[step 3] kubectl get x")])
        assert any("operator-step marker" in e for e in errs)

    def test_all_punctuation_statements_not_flagged_as_duplicate(self):
        # Both normalize to "" — must NOT collide as a false exact-dup.
        errs, warns = check_cause_statement_invariants([("A", "..."), ("B", "???")])
        assert errs == [] and warns == []

    def test_n_identical_statements_single_grouped_error(self):
        errs, _ = check_cause_statement_invariants(
            [("A", "disk full"), ("B", "disk full"), ("C", "disk full")]
        )
        dup = [e for e in errs if "identical Statements" in e]
        assert len(dup) == 1
        assert all(x in dup[0] for x in ("A", "B", "C"))


class TestBackendCauseStatementParsing:
    def _check(self, content):
        v = RunbookValidator()
        errors, warnings = [], []
        v._validate_cause_statements(content, errors, warnings)
        return errors, warnings

    def test_parses_and_blocks_step_marker(self):
        content = (
            "## Causes\n\n"
            "### Cause A: Hook failure\n"
            "**Statement:** [Step 1] argocd app get shows phase Failed\n"
            "**Indicators:**\n- [Step 1] x\n"
        )
        errors, _ = self._check(content)
        assert any("operator-step marker" in e for e in errors)

    def test_fallback_cause_statement_excluded(self):
        # The [Default] fallback Cause is not a match surface — never flagged,
        # even if its Statement is degenerate.
        content = (
            "## Causes\n\n"
            "### Cause Z: Unidentified\n"
            "**Statement:** [Step 9] none of the above\n"
            "**Indicators:**\n- [Default]\n"
        )
        errors, warnings = self._check(content)
        assert errors == [] and warnings == []

    def test_clean_runbook_causes_pass(self):
        content = (
            "## Causes\n\n"
            "### Cause A: Pool exhaustion\n"
            "**Statement:** Idle transactions exhaust the connection pool.\n"
            "**Indicators:**\n- [Step 1] x\n\n"
            "### Cause B: Lock contention\n"
            "**Statement:** A long write transaction blocks other writers on a hot row.\n"
            "**Indicators:**\n- [Step 2] y\n"
        )
        errors, warnings = self._check(content)
        assert errors == [] and warnings == []

    def test_empty_statement_not_miscaptured_as_step_marker(self):
        # Regression: an empty Statement followed by Indicators with [Step N] must
        # NOT mis-capture Indicators as the Statement and falsely cry "operator-step
        # marker". The invariants method skips the empty Statement; the EMPTY report
        # itself is owned by _validate_cause_subfields (see TestBackendCauseSubfields).
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            "**Statement:**\n"
            "**Indicators:**\n- [Step 1] foo\n"
        )
        errors, _ = self._check(content)
        assert not any("operator-step marker" in e for e in errors)

    def test_fallback_detected_via_indicators_not_prose(self):
        # A real cause that merely MENTIONS [Default] in its Statement prose is
        # still checked (fallback is detected only via the Indicators field).
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            "**Statement:** Traffic uses the [Default] route. [Step 2] leak here\n"
            "**Indicators:**\n- [Step 2] y\n"
        )
        errors, _ = self._check(content)
        assert any("operator-step marker" in e for e in errors)

    def test_inline_bold_in_statement_not_truncated(self):
        # An inline **bold** word must not terminate the Statement capture (only a
        # **Field:** label does), so a [Step N] after it is still seen.
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            "**Statement:** The **primary** replica diverged [Step 3] leak\n"
            "**Indicators:**\n- [Step 3] y\n"
        )
        errors, _ = self._check(content)
        assert any("operator-step marker" in e for e in errors)

    def test_out_of_section_cause_not_collected_for_invariants(self):
        # A ### Cause heading outside ## Causes must NOT feed the duplicate/MECE
        # check — otherwise an illustrative example reusing a real cause's Statement
        # would spuriously trip "identical Statements".
        content = (
            "## Diagnostic Steps\n\n"
            "### Cause A: example\n"
            "**Statement:** Idle transactions exhaust the pool.\n\n"
            "## Causes\n\n"
            "### Cause A: Real\n"
            "**Statement:** Idle transactions exhaust the pool.\n"
            "**Indicators:**\n- [Step 1] x\n"
        )
        errors, _ = self._check(content)
        assert not any("identical Statements" in e for e in errors)


class TestBackendCauseSubfields:
    """Per-Cause required sub-fields + Statement length (Gate 2a — per-cause ERROR,
    parity with the kb-toolkit generator/validator)."""

    def _check(self, content):
        v = RunbookValidator()
        errors, warnings = [], []
        v._validate_cause_subfields(content, errors, warnings)
        return errors, warnings

    def _cause(self, extra_fields: str, letter: str = "A", name: str = "x") -> str:
        return f"## Causes\n\n### Cause {letter}: {name}\n{extra_fields}"

    def test_clean_cause_with_all_fields_passes(self):
        content = self._cause(
            "**Statement:** Idle transactions exhaust the pool.\n"
            "**Indicators:**\n- [Step 1] x\n"
            "**Interventions:**\n- **remediation** (root): raise the pool size.\n"
        )
        errors, _ = self._check(content)
        assert errors == []

    def test_missing_interventions_blocks_per_cause(self):
        content = self._cause(
            "**Statement:** Idle transactions exhaust the pool.\n"
            "**Indicators:**\n- [Step 1] x\n"
        )
        errors, _ = self._check(content)
        assert any("missing required **Interventions:**" in e for e in errors)

    def test_missing_indicators_blocks(self):
        content = self._cause(
            "**Statement:** Idle transactions exhaust the pool.\n"
            "**Interventions:**\n- **remediation** (root): fix.\n"
        )
        errors, _ = self._check(content)
        assert any("missing required **Indicators:**" in e for e in errors)

    def test_empty_statement_reported_as_empty(self):
        content = self._cause(
            "**Statement:**\n"
            "**Indicators:**\n- [Step 1] x\n"
            "**Interventions:**\n- **mitigation** (D): escalate.\n"
        )
        errors, _ = self._check(content)
        assert any("**Statement:** sub-field is empty" in e for e in errors)

    def test_overlong_statement_blocks(self):
        long_stmt = "x" * 301
        content = self._cause(
            f"**Statement:** {long_stmt}\n"
            "**Indicators:**\n- [Step 1] x\n"
            "**Interventions:**\n- **remediation** (root): fix.\n"
        )
        errors, _ = self._check(content)
        assert any("Statement is 301 chars" in e for e in errors)

    def test_statement_at_limit_passes(self):
        content = self._cause(
            f"**Statement:** {'x' * 300}\n"
            "**Indicators:**\n- [Step 1] x\n"
            "**Interventions:**\n- **remediation** (root): fix.\n"
        )
        errors, _ = self._check(content)
        assert not any("chars (>300)" in e for e in errors)

    def test_chain_is_optional(self):
        # No **Chain:** — a degenerate root->D cause is valid, no error.
        content = self._cause(
            "**Statement:** A single-step failure.\n"
            "**Indicators:**\n- [Step 1] x\n"
            "**Interventions:**\n- **remediation** (root): fix.\n"
        )
        errors, _ = self._check(content)
        assert not any("Chain" in e for e in errors)

    def test_fallback_cause_also_requires_interventions(self):
        # The fallback Cause Z is not a match surface, but it still must carry the
        # required sub-fields (its Interventions is the safe escalation path).
        content = (
            "## Causes\n\n"
            "### Cause Z: Unidentified\n"
            "**Statement:** None of the documented causes match.\n"
            "**Indicators:**\n- [Default]\n"
        )
        errors, _ = self._check(content)
        assert any("missing required **Interventions:**" in e for e in errors)

    def test_missing_subfield_not_masked_by_later_section(self):
        # Regression: the last cause's block must NOT bleed into ## Prevention /
        # ## Sources. A required label appearing in a trailing section must not mask
        # a genuinely-missing sub-field on the final (fallback) cause.
        content = (
            "## Causes\n\n"
            "### Cause Z: Unidentified\n"
            "**Statement:** None of the documented causes match.\n"
            "**Indicators:**\n- [Default]\n\n"
            "## Prevention\n"
            "Review pool sizing. **Interventions:** monitor pool weekly.\n\n"
            "## Sources\n- vendor docs\n"
        )
        errors, _ = self._check(content)
        assert any("missing required **Interventions:**" in e for e in errors)

    def test_cause_heading_outside_causes_section_ignored(self):
        # A ### Cause-style heading in another section (e.g. an example under
        # ## Diagnostic Steps) is NOT a real cause and must not be validated.
        content = (
            "## Diagnostic Steps\n\n"
            "### Cause A: (example) pool exhaustion\n"
            "An illustrative example with no sub-fields.\n\n"
            "## Causes\n\n"
            "### Cause A: Real cause\n"
            "**Statement:** Idle transactions exhaust the pool.\n"
            "**Indicators:**\n- [Step 1] x\n"
            "**Interventions:**\n- **remediation** (root): fix.\n"
        )
        errors, _ = self._check(content)
        assert errors == []
