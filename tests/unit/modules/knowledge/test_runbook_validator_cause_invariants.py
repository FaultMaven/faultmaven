"""Cause-Statement match-surface invariants in the backend runbook validator (#545).

The Statement is the load-bearing summary of each Cause (RAG discriminability +
sibling MECE, per runbook-content-architecture.md §3); these guard it: no
operator-step marker leaking in, and siblings must be discriminative. Bias is hard toward WARN — only unambiguous mechanical tells
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


class TestSectionHeaderAnchoring:
    """The required-section gate is exact-anchored so a non-canonical header
    (``## Causes (RCA)``, ``### Causes``) cannot pass presence-checking yet be
    skipped by the exact-anchored per-Cause section parse — the silent-skip the
    per-Cause ERRORs exist to avoid (parity with kb-toolkit)."""

    def _structure_errors(self, content):
        v = RunbookValidator()
        errors, warnings = [], []
        v._validate_structure(content, errors, warnings)
        return errors

    def _full_runbook(self, causes_header: str) -> str:
        return (
            "## Symptom Recognition\nx\n## Applicability\nx\n"
            "## Diagnostic Steps\nx\n"
            f"{causes_header}\n### Cause A: x\n**Statement:** A cause.\n"
            "## Prevention\nx\n## Sources\n- x\n"
        )

    def test_suffixed_causes_header_flagged_not_silently_skipped(self):
        # '## Causes (RCA)' used to pass the loose presence gate while the
        # exact-anchored per-Cause parse found nothing → per-Cause ERRORs skipped.
        errors = self._structure_errors(self._full_runbook("## Causes (RCA)"))
        assert any("Missing required section: Causes" in e for e in errors)

    def test_h3_causes_header_flagged(self):
        errors = self._structure_errors(self._full_runbook("### Causes"))
        assert any("Missing required section: Causes" in e for e in errors)

    def test_exact_causes_header_accepted(self):
        errors = self._structure_errors(self._full_runbook("## Causes"))
        assert not any("Missing required section" in e for e in errors)


# =============================================================================
# R1 validator hardening — the gate must reject what the extractor silently
# drops, and carry the upstream kb-toolkit ERRORS the backend gate was missing.
# =============================================================================


def _graph_check(content):
    v = RunbookValidator()
    errors, warnings = [], []
    v._validate_cause_graph(content, errors, warnings)
    return errors, warnings


def _causes(body: str) -> str:
    """A minimal ``## Causes`` section wrapping ``body`` for the cause-graph gate."""
    return f"## Causes\n\n{body}"


class TestMalformedCauseHeadingsBlock:
    """A near-miss heading the extractor drops must be surfaced, not silently
    passed. The strict form is ``### Cause X: <name>`` (H3, single uppercase A-Z,
    colon immediately after the letter, non-empty name)."""

    @pytest.mark.parametrize(
        "heading",
        [
            "#### Cause A: Wrong heading level",  # H4, extractor is H3-exact
            "##### Cause A: Even deeper",  # H5
            "### Cause AA: Double letter",  # multi-char letter
            "### Cause a: Lowercase letter",  # lowercase
            "### Cause 1: Numeric letter",  # digit
            "### Cause A : Space before colon",  # space before colon
            "### Cause A:",  # empty name
        ],
    )
    def test_near_miss_heading_flagged(self, heading):
        errors, _ = _graph_check(
            _causes(f"{heading}\n**Statement:** x\n**Indicators:**\n- [Symptom] x\n")
        )
        assert any("Malformed Cause heading" in e for e in errors), heading

    def test_strict_heading_not_flagged(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: Pool exhaustion\n"
                "**Statement:** Idle transactions exhaust the pool.\n"
                "**Indicators:**\n- [Symptom] connections climb\n"
                "**Interventions:**\n- **remediation** (root): fix. **Verification:** ok.\n\n"
                "### Cause Z: Unidentified\n"
                "**Statement:** None match.\n**Indicators:**\n- [Default]\n"
                "**Interventions:**\n- **mitigation** (D): escalate. "
                "**Risk:** low. **Duration:** short. **Verification:** n/a.\n"
            )
        )
        assert not any("Malformed Cause heading" in e for e in errors)

    def test_heading_inside_code_fence_not_flagged(self):
        # An illustrative ``#### Cause`` inside an Intervention command example must
        # NOT trip the malformed-heading block (false positive).
        errors, _ = _graph_check(
            _causes(
                "### Cause A: Real\n**Statement:** x.\n**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\n- **remediation** (root): apply this.\n\n"
                "  ```bash\n  # example: #### Cause of the stall\n  echo fix\n  ```\n\n"
                "  **Verification:** ok.\n\n"
                "### Cause Z: Unidentified\n**Statement:** None.\n**Indicators:**\n- [Default]\n"
                "**Interventions:**\n- **mitigation** (D): esc. "
                "**Risk:** l. **Duration:** s. **Verification:** n/a.\n"
            )
        )
        assert not any("Malformed Cause heading" in e for e in errors)

    def test_causes_section_header_not_flagged_as_malformed(self):
        # 'Causes' has no word boundary after 'Cause' → the loose detector must
        # not mistake the ## Causes section header for a malformed cause heading.
        errors, _ = _graph_check(
            _causes(
                "### Cause A: Real\n**Statement:** x.\n**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\n- **remediation** (root): fix. **Verification:** ok.\n\n"
                "### Cause Z: Unidentified\n**Statement:** None.\n**Indicators:**\n- [Default]\n"
                "**Interventions:**\n- **mitigation** (D): esc. "
                "**Risk:** l. **Duration:** s. **Verification:** n/a.\n"
            )
        )
        assert not any("Malformed Cause heading" in e for e in errors)


class TestStatementLengthBypassClosed:
    """The Statement length gate must measure the value the EXTRACTOR sees
    (bounded only by the four schema labels), not a value truncated at a stray
    non-schema ``**Label:**``."""

    def test_stray_bold_label_no_longer_truncates_before_length_gate(self):
        v = RunbookValidator()
        errors, warnings = [], []
        # 250 x's up to a stray **Note:** (a non-schema label), then 100 y's up to
        # **Indicators:**. Old parse stopped at **Note:** (250 chars, under gate);
        # the shared parse runs to **Indicators:** (>300 chars) → blocks.
        content = (
            "## Causes\n\n"
            "### Cause A: x\n"
            f"**Statement:** {'x' * 250}\n"
            f"**Note:** {'y' * 100}\n"
            "**Indicators:**\n- [Symptom] z\n"
            "**Interventions:**\n- **remediation** (root): fix. **Verification:** ok.\n"
        )
        v._validate_cause_subfields(content, errors, warnings)
        assert any("Statement is" in e and "(>300)" in e for e in errors)


class TestSectionScopedCauseCount:
    """The '≥1 ### Cause' gate scans the Causes SECTION BODY (strict heading), so
    an example ``### Cause`` heading in another section cannot satisfy it while the
    extractor parses zero causes."""

    def _structure_errors(self, content):
        v = RunbookValidator()
        errors, warnings = [], []
        v._validate_structure(content, errors, warnings)
        return errors

    def test_out_of_section_cause_does_not_satisfy_count(self):
        content = (
            "## Symptom Recognition\nx\n## Applicability\nx\n## Diagnostic Steps\nx\n"
            "## Diagnostic Steps\n### Cause A: illustrative example\nprose\n"
            "## Causes\n(no real cause here)\n"
            "## Prevention\nx\n## Sources\n- x\n"
        )
        errors = self._structure_errors(content)
        assert any("at least one ### Cause subsection" in e for e in errors)


class TestPortedCauseGraphErrors:
    """The upstream kb-toolkit cause-graph ERRORS, previously missing from the
    backend gate."""

    def test_cause_z_without_default_is_reserved_error(self):
        # Cause Z with a NON-[Default] indicator is a real cause on the reserved
        # letter — the extractor would seed it as a candidate root.
        errors, _ = _graph_check(
            _causes(
                "### Cause A: Real\n**Statement:** x.\n**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\n- **remediation** (root): fix. **Verification:** ok.\n\n"
                "### Cause Z: Not actually the fallback\n"
                "**Statement:** A real cause mis-lettered Z.\n"
                "**Indicators:**\n- [Symptom] z\n"
                "**Interventions:**\n- **remediation** (root): fix. **Verification:** ok.\n"
            )
        )
        assert any("reserved for the [Default] fallback" in e for e in errors)

    def test_duplicate_cause_letter_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: First\n**Statement:** a.\n**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\n- **remediation** (root): f. **Verification:** ok.\n\n"
                "### Cause A: Second\n**Statement:** b.\n**Indicators:**\n- [Symptom] z\n"
                "**Interventions:**\n- **remediation** (root): f. **Verification:** ok.\n"
            )
        )
        assert any("Duplicate Cause letter 'A'" in e for e in errors)

    def test_missing_fallback_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: Only cause\n**Statement:** a.\n**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\n- **remediation** (root): f. **Verification:** ok.\n"
            )
        )
        assert any("must contain a fallback Cause" in e for e in errors)

    def test_multiple_fallback_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: One fallback\n**Statement:** a.\n**Indicators:**\n- [Default]\n"
                "**Interventions:**\n- **mitigation** (D): esc. "
                "**Risk:** l. **Duration:** s. **Verification:** n/a.\n\n"
                "### Cause Z: Another fallback\n**Statement:** b.\n**Indicators:**\n- [Default]\n"
                "**Interventions:**\n- **mitigation** (D): esc. "
                "**Risk:** l. **Duration:** s. **Verification:** n/a.\n"
            )
        )
        assert any("exactly one fallback is required" in e for e in errors)

    def test_no_real_cause_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause Z: Unidentified\n**Statement:** None match.\n"
                "**Indicators:**\n- [Default]\n"
                "**Interventions:**\n- **mitigation** (D): esc. "
                "**Risk:** l. **Duration:** s. **Verification:** n/a.\n"
            )
        )
        assert any("at least one real ### Cause" in e for e in errors)

    def test_chain_present_but_unparseable_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: x\n**Statement:** a.\n"
                "**Chain:**\nThis is prose with no ref rungs.\n"
                "**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\n- **remediation** (root): f. **Verification:** ok.\n"
            )
        )
        assert any("**Chain** is present but has no" in e for e in errors)

    def test_invalid_intervention_quadrant_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: x\n**Statement:** a.\n**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\n- **not_a_quadrant** (root): f. **Verification:** ok.\n"
            )
        )
        assert any("quadrant 'not_a_quadrant' is not valid" in e for e in errors)

    def test_interventions_without_quadrant_entry_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: x\n**Statement:** a.\n**Indicators:**\n- [Symptom] y\n"
                "**Interventions:**\nJust prose, no quadrant-tagged bullet.\n"
            )
        )
        assert any("no quadrant-tagged entry" in e for e in errors)

    def test_indicator_without_token_blocks(self):
        errors, _ = _graph_check(
            _causes(
                "### Cause A: x\n**Statement:** a.\n"
                "**Indicators:**\n- an observation with no bracketed token\n"
                "**Interventions:**\n- **remediation** (root): f. **Verification:** ok.\n"
            )
        )
        assert any("has no [Step N] / [Symptom] / [Default] token" in e for e in errors)

    def test_indicator_step_reference_must_resolve(self):
        content = (
            "## Diagnostic Steps\n\n### Step 1: Check\nlook\n\n"
            "## Causes\n\n### Cause A: x\n**Statement:** a.\n"
            "**Indicators:**\n- [Step 5] references a step that does not exist\n"
            "**Interventions:**\n- **remediation** (root): f. **Verification:** ok.\n"
        )
        errors, _ = _graph_check(content)
        assert any("[Step 5] which does not exist" in e for e in errors)


# A well-formed conversion-shaped runbook (mirrors the conversion prompt template)
# — the guard that R1 hardening did NOT over-tighten the gate.
_VALID_RUNBOOK = """---
id: db-pool-exhaustion
title: Database Connection Pool Exhaustion
domain: database
service: postgresql
symptom_class:
  - connection-errors
severity: high
scope: global
version: 1.0.0
last_updated: 2026-07-17
verified_by: sterlanyu
status: verified
tags:
  - postgresql
---

## Symptom Recognition
Applications report connection timeouts under load.

## Applicability
PostgreSQL-backed services using a bounded connection pool.

## Diagnostic Steps
### Step 1: Check active connections
```bash
psql -c "select count(*) from pg_stat_activity"
```

## Causes

### Cause A: Idle transactions exhaust the pool
**Statement:** Idle-in-transaction sessions hold connections and exhaust the pool.
**Indicators:**
- root: [Step 1] open connection count climbs monotonically
**Interventions:**
- **remediation** (root): Set a session timeout.

  ```bash
  ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
  ```

  **Verification:** Re-run Step 1; the connection count stabilizes.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture full diagnostics and consult an SME.

  **Risk:** Diagnostic only. **Duration:** Until SME review. **Verification:** N/A.

## Prevention
- Add connection-pool saturation monitoring.

## Sources
- vendor docs -- https://example.com/postgres
"""


class TestWellFormedDraftStillPasses:
    def test_valid_conversion_shaped_runbook_passes(self):
        result = RunbookValidator().validate_content(_VALID_RUNBOOK)
        assert result.passed, result.errors
        assert result.errors == []

    def test_h4_cause_heading_fails_end_to_end(self):
        broken = _VALID_RUNBOOK.replace(
            "### Cause A: Idle transactions exhaust the pool",
            "#### Cause A: Idle transactions exhaust the pool",
        )
        result = RunbookValidator().validate_content(broken)
        assert not result.passed
        assert any("Malformed Cause heading" in e for e in result.errors)
