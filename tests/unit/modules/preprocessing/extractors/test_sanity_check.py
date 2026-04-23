"""Phase 2a — Extraction sanity checks.

Each check is a pure function of (raw_content, structural_index).
Tests pin:

- The degenerate cases each check is designed to catch (would fail the
  check, triggering the Phase 2b retry loop).
- The non-degenerate cases each check must accept (would pass, no retry).
- The thin-case safeguard — sparse input (< MIN_CONTENT_CHARS) always
  passes, regardless of data type.
- The permissive default for unregistered types.
"""

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.extractors.sanity_check import (
    SanityResult,
    run_sanity_check,
)

# ---------------------------------------------------------------------------
# Thin-case safeguard (applies across all types)
# ---------------------------------------------------------------------------


class TestThinCaseSafeguard:
    """Sparse input can't be meaningfully sanity-checked — any extractor
    output on a 1-word paste is naturally thin. Retrying with a different
    extractor produces equally-thin output. The thin-case safeguard
    short-circuits the check so we never spiral on sparse content."""

    @pytest.mark.parametrize(
        "data_type",
        [
            DataType.METRICS_AND_PERFORMANCE,
            DataType.LOGS_AND_ERRORS,
            DataType.STRUCTURED_CONFIG,
            DataType.SOURCE_CODE,
        ],
    )
    def test_short_content_always_passes(self, data_type):
        result = run_sanity_check(data_type, "short", "whatever")
        assert result.passed is True

    def test_empty_content_passes(self):
        result = run_sanity_check(DataType.METRICS_AND_PERFORMANCE, "", "whatever")
        assert result.passed is True

    def test_none_content_passes(self):
        result = run_sanity_check(DataType.METRICS_AND_PERFORMANCE, None, "whatever")
        assert result.passed is True


# ---------------------------------------------------------------------------
# METRICS sanity check
# ---------------------------------------------------------------------------


_LONG_CONTENT = "x" * 200  # above the MIN_CONTENT_CHARS threshold


class TestMetricsCheck:
    def test_csv_non_numeric_summary_passes(self):
        """The extractor correctly refused to compute stats and returned
        the CSV structure summary. Valid outcome — must not retry."""
        idx = (
            "=== CSV STRUCTURE SUMMARY ===\n"
            "Rows: 500\n"
            "\n"
            "--- COVERAGE METADATA ---\n"
            "Format: csv (non-numeric)\n"
        )
        result = run_sanity_check(DataType.METRICS_AND_PERFORMANCE, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_healthy_metrics_output_passes(self):
        """Real metrics output with multiple data points — not degenerate."""
        idx = (
            "=== METRICS ANALYSIS SUMMARY ===\n"
            "Analyzed 1 metric(s)\n"
            "\n"
            "--- COVERAGE METADATA ---\n"
            "Format: csv\n"
            "Total data points: 100\n"
        )
        result = run_sanity_check(DataType.METRICS_AND_PERFORMANCE, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_single_data_point_fails(self):
        """Extractor mistook a categorical column for numeric and
        produced a 1-point 'series'. This is the degenerate case the
        retry loop must catch."""
        idx = (
            "=== METRICS ANALYSIS SUMMARY ===\n"
            "Analyzed 1 metric(s)\n"
            "\n"
            "--- COVERAGE METADATA ---\n"
            "Format: csv\n"
            "Total data points: 1\n"
        )
        result = run_sanity_check(DataType.METRICS_AND_PERFORMANCE, _LONG_CONTENT, idx)
        assert result.passed is False
        assert result.reason == "single_data_point"

    def test_missing_analysis_block_fails(self):
        """Extractor ran but produced no analysis block — probably
        found nothing numeric and silently emitted a placeholder."""
        idx = "=== SOME UNRELATED OUTPUT ===\n\n--- COVERAGE METADATA ---\nFormat: unknown\n"
        result = run_sanity_check(DataType.METRICS_AND_PERFORMANCE, _LONG_CONTENT, idx)
        assert result.passed is False
        assert result.reason == "no_metrics_analysis_block"


# ---------------------------------------------------------------------------
# LOGS sanity check
# ---------------------------------------------------------------------------


class TestLogsCheck:
    def test_real_error_extraction_passes(self):
        """Crime-scene extraction fired; passes permissively since the
        extractor found something to report."""
        idx = (
            "ENTITY PROFILE (full file scan):\n"
            "  Event types:\n"
            "    failed_password: 42\n"
            "\n"
            "CRIME SCENE EXTRACTION: Single ERROR at line 500\n"
        )
        result = run_sanity_check(DataType.LOGS_AND_ERRORS, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_no_errors_with_thick_tail_passes(self):
        """No errors found but fallback returned a reasonable tail —
        this is a valid outcome for a clean log with no issues."""
        idx = (
            "CRIME SCENE EXTRACTION: "
            "No errors detected - showing last 500 lines\n"
            "line1\nline2\n"
        )
        result = run_sanity_check(DataType.LOGS_AND_ERRORS, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_no_errors_with_thin_tail_fails(self):
        """Extractor found no errors AND tail is suspiciously short.
        Suggests the content wasn't log-shaped — e.g. a binary file
        that happened to get routed to LOGS."""
        idx = (
            "CRIME SCENE EXTRACTION: "
            "No errors detected - showing last 5 lines\n"
            "a\nb\nc\nd\ne\n"
        )
        result = run_sanity_check(DataType.LOGS_AND_ERRORS, _LONG_CONTENT, idx)
        assert result.passed is False
        assert result.reason == "tail_too_short"


# ---------------------------------------------------------------------------
# STRUCTURED_CONFIG sanity check
# ---------------------------------------------------------------------------


class TestConfigCheck:
    def test_valid_parsed_config_passes(self):
        """YAML/TOML/JSON successfully parsed — no key-value fallback fired."""
        idx = (
            "service:\n  port: 8080\n\n"
            "--- COVERAGE METADATA ---\n"
            "Format: yaml\n"
            "Total keys: 4\n"
        )
        result = run_sanity_check(DataType.STRUCTURED_CONFIG, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_key_value_fallback_with_keys_passes(self):
        """Key-value fallback actually found some key=value pairs —
        content was probably env-file-shaped, not garbage."""
        idx = (
            "foo: bar\n\n"
            "--- COVERAGE METADATA ---\n"
            "Format: key-value\n"
            "Total keys: 3\n"
        )
        result = run_sanity_check(DataType.STRUCTURED_CONFIG, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_key_value_fallback_zero_keys_fails(self):
        """Key-value fallback is the last-resort parser. Zero keys
        parsed out of non-trivial content means the content isn't
        config at all."""
        idx = (
            "\n\n" "--- COVERAGE METADATA ---\n" "Format: key-value\n" "Total keys: 0\n"
        )
        result = run_sanity_check(DataType.STRUCTURED_CONFIG, _LONG_CONTENT, idx)
        assert result.passed is False
        assert result.reason == "key_value_fallback_zero_keys"


# ---------------------------------------------------------------------------
# SOURCE_CODE sanity check
# ---------------------------------------------------------------------------


class TestSourceCodeCheck:
    def test_python_ast_output_passes(self):
        """Python AST succeeded — the output header differs from the
        pattern-based fallback, and that alone is good enough."""
        idx = "=== PYTHON CODE ANALYSIS ===\n\n## Functions (3)\n"
        result = run_sanity_check(DataType.SOURCE_CODE, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_tree_sitter_output_passes(self):
        """Tree-sitter picked a language — valid structural extraction."""
        idx = "=== SOURCE CODE ANALYSIS (tree-sitter) ===\n\n## Functions (5)\n"
        result = run_sanity_check(DataType.SOURCE_CODE, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_regex_fallback_with_functions_passes(self):
        """Pattern-based fallback but found functions — probably code."""
        idx = (
            "=== SOURCE CODE ANALYSIS (Pattern-based) ===\n\n"
            "## Functions (2)\n  - foo\n  - bar\n"
        )
        result = run_sanity_check(DataType.SOURCE_CODE, _LONG_CONTENT, idx)
        assert result.passed is True

    def test_regex_fallback_zero_structures_fails(self):
        """Pattern-based fallback ran, neither classes nor functions
        found — content almost certainly isn't code."""
        idx = (
            "=== SOURCE CODE ANALYSIS (Pattern-based) ===\n\n"
            "Detected Language: Unknown\n"
        )
        result = run_sanity_check(DataType.SOURCE_CODE, _LONG_CONTENT, idx)
        assert result.passed is False
        assert result.reason == "no_code_structures_found"


# ---------------------------------------------------------------------------
# Permissive default (unregistered types)
# ---------------------------------------------------------------------------


class TestPermissiveDefault:
    def test_unregistered_type_accepts_non_empty_output(self):
        result = run_sanity_check(
            DataType.UNSTRUCTURED_TEXT,
            _LONG_CONTENT,
            "Some text extractor output",
        )
        assert result.passed is True

    def test_unregistered_type_rejects_empty_output(self):
        result = run_sanity_check(DataType.UNSTRUCTURED_TEXT, _LONG_CONTENT, "")
        assert result.passed is False

    def test_file_too_large_placeholder_accepted(self):
        """The extractor correctly refused because the file exceeded
        its cap — that's not a degenerate outcome."""
        result = run_sanity_check(
            DataType.UNSTRUCTURED_TEXT,
            _LONG_CONTENT,
            "[File exceeds 50MB maximum size limit for extraction]",
        )
        assert result.passed is True

    def test_explicit_failed_to_parse_placeholder_rejected(self):
        """An explicit failure placeholder *is* degenerate — the
        extractor gave up. Retry should fire."""
        result = run_sanity_check(
            DataType.UNSTRUCTURED_TEXT,
            _LONG_CONTENT,
            "[Failed to parse metrics data - unsupported format]",
        )
        assert result.passed is False
