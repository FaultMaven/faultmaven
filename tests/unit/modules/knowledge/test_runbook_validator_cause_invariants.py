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

    def test_empty_statement_blocks_cleanly_not_as_step_marker(self):
        # Regression: an empty Statement followed by Indicators with [Step N] must
        # report the EMPTY statement, not mis-capture Indicators as the Statement
        # and falsely cry "operator-step marker".
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            "**Statement:**\n"
            "**Indicators:**\n- [Step 1] foo\n"
        )
        errors, _ = self._check(content)
        assert any("is empty" in e for e in errors)
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
