"""Extraction sanity checks — Phase 2.

Tier 1 extractors emit a structural_index string; the string *looks*
plausible even when the extractor was given content of the wrong type
(a CSV of HTTP error codes misclassified as METRICS gets percentiles
computed over status codes and "p50=404, p95=503, spike detected"
ships to the agent as fact). Sanity checks are a lightweight
post-extraction filter that answers "does this output look degenerate
for the claimed type?" per extractor. On failure, Phase 2's retry loop
in PreprocessingService tries the next candidate type.

Design notes:

- Sanity checks are pure functions of ``(raw_content, structural_index)``
  with no IO, so they're cheap to run on every extraction and trivially
  testable.
- They return a ``SanityResult`` (passed + optional reason) rather than
  a bool so observability can distinguish *why* a check failed.
- They are intentionally **permissive** on sparse input — a 1-word
  paste must not trigger a retry. The "Scenario: thin case" constraint
  in the plan's Flexibility Contract bounds how strict each check can be.
- Per-type checks are registered in a dispatch table indexed by
  ``DataType``. Types without an explicit check get the permissive
  default (non-empty output → passes).

Design Reference:
    docs/working/WIP-data-processing-improvement-plan.md Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

from faultmaven.models.api import DataType


@dataclass(frozen=True)
class SanityResult:
    """Outcome of a sanity check on an extractor's structural_index.

    ``passed`` is the only field the retry loop consumes. ``reason`` is
    optional observability — when the check fails, it records which
    invariant broke. ``None`` reason on a failed check means "the
    extractor output was obviously degenerate" without further detail.
    """

    passed: bool
    reason: Optional[str] = None


# Content-length below which ALL sanity checks unconditionally pass.
# Pathologically short input (a 1-word paste, a 3-line snippet) can't be
# meaningfully sanity-checked — the extractor's output will naturally be
# thin, and retrying with a different extractor only produces equally
# thin output. Avoid spiralling on thin cases by cutting retries off at
# ingest size. Threshold chosen to be below typical log entries but
# above single-word pastes.
_MIN_CONTENT_CHARS_FOR_SANITY_CHECK = 50


def _permissive_default(raw_content: str, structural_index: str) -> SanityResult:
    """Default check: output must be non-empty and not an explicit error
    placeholder. Applied to every data type that doesn't register its
    own check, and as the last-resort approval when no more-specific
    invariant applies.
    """
    if not structural_index or not structural_index.strip():
        return SanityResult(passed=False, reason="empty_output")
    # Extractors emit these placeholders when they can't work with the
    # content (e.g. 50MB cap tripped, content too short). Treat as a
    # non-degenerate outcome — the extractor correctly refused to lie.
    if structural_index.startswith("[File exceeds"):
        return SanityResult(passed=True)
    if structural_index.startswith("[No content to analyze"):
        return SanityResult(passed=True)
    if structural_index.startswith("[Failed to parse"):
        return SanityResult(passed=False, reason="parse_failed_placeholder")
    return SanityResult(passed=True)


def _metrics_check(raw_content: str, structural_index: str) -> SanityResult:
    """METRICS_AND_PERFORMANCE extractor output is degenerate when it
    ran on non-metric content.

    Pass when EITHER:

    - The output contains a "METRICS ANALYSIS SUMMARY" block AND reports
      at least 2 data points in coverage metadata. A 1-point "metric"
      usually means the extractor mistook a categorical column for
      numeric.
    - The output is a "csv (non-numeric)" structural summary — the
      extractor correctly refused to compute stats and returned the
      CSV structure summary instead. That's a valid outcome.

    Failing this catches:

    - CSV of HTTP error codes → METRICS fires → produces "p50=404,
      spike detected". Detected because total data points is 1 per
      series and no real metric name appears.
    - Configuration file → METRICS fires on a key=value line → noise.
    """
    idx = structural_index.lower()
    if "csv (non-numeric)" in idx:
        return SanityResult(passed=True)
    if "metrics analysis summary" not in idx:
        # Extractor ran but produced no analysis block — suggests it
        # couldn't find numeric data and silently produced a placeholder.
        return SanityResult(passed=False, reason="no_metrics_analysis_block")
    # The coverage section reports "Total data points: N". Anything
    # under 2 means a degenerate single-point "series".
    total_points = _extract_total_data_points(structural_index)
    if total_points is not None and total_points < 2:
        return SanityResult(passed=False, reason="single_data_point")
    return SanityResult(passed=True)


def _logs_check(raw_content: str, structural_index: str) -> SanityResult:
    """LOGS_AND_ERRORS extractor is degenerate when it found no severity
    match AND the tail fallback returned suspiciously-little content
    (< 10 lines of recognizable log-shape).

    The extractor's "no errors" output already signals this cleanly via
    the ``No errors detected - showing last N lines`` header. If N < 10
    the tail is probably non-log content (bytes of a config file, for
    example) and the extractor should not have been chosen.
    """
    if "No errors detected" in structural_index:
        # Extractor found no errors and fell back to tail. The header
        # carries the line count we can compare against.
        tail_count = _extract_no_errors_tail_count(structural_index)
        if tail_count is not None and tail_count < 10:
            return SanityResult(passed=False, reason="tail_too_short")
    return _permissive_default(raw_content, structural_index)


def _config_check(raw_content: str, structural_index: str) -> SanityResult:
    """STRUCTURED_CONFIG extractor is degenerate when the parser fell
    all the way through to the key=value fallback on content that isn't
    config. The extractor labels this path explicitly with
    ``Format=key-value`` in its coverage metadata; if the fallback
    produced zero keys, the content wasn't config.
    """
    if "Format: key-value" in structural_index:
        total_keys = _extract_total_keys(structural_index)
        if total_keys is not None and total_keys == 0:
            return SanityResult(passed=False, reason="key_value_fallback_zero_keys")
    return _permissive_default(raw_content, structural_index)


def _source_code_check(raw_content: str, structural_index: str) -> SanityResult:
    """SOURCE_CODE extractor is degenerate when it produced a
    "pattern-based" output with neither classes nor functions
    extracted — code files always contain *something* structural, so
    zero findings usually means the content wasn't code.
    """
    if "SOURCE CODE ANALYSIS (Pattern-based)" in structural_index:
        # Regex fallback ran (tree-sitter + AST both declined). If neither
        # classes nor functions were found, the content almost certainly
        # isn't code. Coverage metadata's per-extractor shape varies, so
        # check the section headers in the body.
        has_classes = "## Classes" in structural_index
        has_functions = "## Functions" in structural_index
        if not has_classes and not has_functions:
            return SanityResult(passed=False, reason="no_code_structures_found")
    return _permissive_default(raw_content, structural_index)


# ---------------------------------------------------------------------------
# Helpers for reading values out of coverage metadata / extractor output
# ---------------------------------------------------------------------------


def _extract_total_data_points(structural_index: str) -> Optional[int]:
    """Parse ``Total data points: N`` from METRICS coverage metadata."""
    marker = "Total data points:"
    idx = structural_index.find(marker)
    if idx < 0:
        return None
    rest = structural_index[idx + len(marker) :].lstrip()
    num = []
    for ch in rest:
        if ch.isdigit():
            num.append(ch)
        else:
            break
    if not num:
        return None
    return int("".join(num))


def _extract_no_errors_tail_count(structural_index: str) -> Optional[int]:
    """Parse N from ``No errors detected - showing last N lines``."""
    marker = "No errors detected - showing last"
    idx = structural_index.find(marker)
    if idx < 0:
        return None
    rest = structural_index[idx + len(marker) :].lstrip()
    num = []
    for ch in rest:
        if ch.isdigit():
            num.append(ch)
        else:
            break
    if not num:
        return None
    return int("".join(num))


def _extract_total_keys(structural_index: str) -> Optional[int]:
    """Parse ``Total keys: N`` from STRUCTURED_CONFIG coverage metadata."""
    marker = "Total keys:"
    idx = structural_index.find(marker)
    if idx < 0:
        return None
    rest = structural_index[idx + len(marker) :].lstrip()
    num = []
    for ch in rest:
        if ch.isdigit():
            num.append(ch)
        else:
            break
    if not num:
        return None
    return int("".join(num))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


SanityCheckFn = Callable[[str, str], SanityResult]


_SANITY_CHECKS: Dict[DataType, SanityCheckFn] = {
    DataType.METRICS_AND_PERFORMANCE: _metrics_check,
    DataType.LOGS_AND_ERRORS: _logs_check,
    DataType.STRUCTURED_CONFIG: _config_check,
    DataType.SOURCE_CODE: _source_code_check,
}


def run_sanity_check(
    data_type: DataType,
    raw_content: str,
    structural_index: str,
) -> SanityResult:
    """Dispatch to the per-type sanity check and return its result.

    Returns ``SanityResult(passed=True)`` immediately for:

    - Content shorter than ``_MIN_CONTENT_CHARS_FOR_SANITY_CHECK``
      (thin-case constraint — don't retry on sparse input).
    - Data types without a registered check (they get the permissive
      default, which only fails on empty / explicit-failure output).
    """
    if raw_content is None or len(raw_content) < _MIN_CONTENT_CHARS_FOR_SANITY_CHECK:
        return SanityResult(passed=True, reason="below_min_content_threshold")

    check = _SANITY_CHECKS.get(data_type, _permissive_default)
    return check(raw_content, structural_index)
