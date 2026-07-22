"""Unit tests for the short-text ambiguity guard's size-bound semantics.

The short-text ambiguity path (`_assess_short_text_ambiguity`, ISS-023) only
applies to pastes that are short in *both* dimensions — inside `_SHORT_TEXT_MAX_CHARS`
(1500) AND inside `_SHORT_TEXT_MAX_LINES` (25). A paste that exceeds *either*
bound is not "short" and must fall through to the confident UNSTRUCTURED_TEXT
path, matching data-classification-strategy.md §"classification_failed Path":
"lands inside both size bounds".

These tests pin the `or` skip-guard (a paste exceeding one bound is skipped as
soon as that bound is crossed) so the AND/OR semantics can't silently regress.

Run with:
    pytest tests/unit/modules/preprocessing/test_classifier_short_text_bounds.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.classifier import DataClassifier

# ISS-023 fixture shape: a short maintenance notice whose mixed signals (URL,
# email, version tag, key:value lines) fire ≥3 distinct ambiguity categories.
_MIXED_SIGNAL_NOTICE = (
    "Scheduled maintenance window\n"
    "When: 2026-04-30T02:00 UTC\n"
    "Contact: ops@example.com\n"
    "Runbook: https://wiki.example.com/maint\n"
    "Tag: v2.3.8\n"
)


@pytest.fixture
def classifier():
    return DataClassifier()


class TestShortTextAmbiguityBounds:
    """The guard fires only when the paste is inside BOTH size bounds."""

    def test_within_both_bounds_flags_ambiguous(self, classifier):
        """Short in chars AND lines → ambiguity path (classification_failed)."""
        result = classifier.classify("notice.txt", _MIXED_SIGNAL_NOTICE)

        assert result.classification_failed is True
        assert result.source == "rule_based_best_effort"
        assert result.data_type == DataType.UNSTRUCTURED_TEXT
        assert result.suggested_types

    def test_exceeding_char_bound_only_is_not_short(self, classifier):
        """Over the char bound but ≤25 lines → NOT short, so no ambiguity path.

        This is the case the previous `and` guard mishandled: it skipped the
        assessment only when BOTH bounds were exceeded, so a long-but-few-lines
        paste was wrongly assessed as short/ambiguous.
        """
        # Same mixed signals, padded past 1500 chars while staying under 25 lines.
        padded = _MIXED_SIGNAL_NOTICE + " ".join(["padding"] * 230) + "\n"
        assert len(padded) > DataClassifier._SHORT_TEXT_MAX_CHARS
        assert padded.count("\n") + 1 <= DataClassifier._SHORT_TEXT_MAX_LINES

        result = classifier.classify("notice.txt", padded)

        assert result.classification_failed is False
        assert result.data_type == DataType.UNSTRUCTURED_TEXT

    def test_exceeding_line_bound_only_is_not_short(self, classifier):
        """Over the line bound but ≤1500 chars → NOT short, so no ambiguity path."""
        # Same mixed signals, extended past 25 lines while staying under 1500 chars.
        many_lines = _MIXED_SIGNAL_NOTICE + "".join(
            f"note line {i}\n" for i in range(30)
        )
        assert len(many_lines) <= DataClassifier._SHORT_TEXT_MAX_CHARS
        assert many_lines.count("\n") + 1 > DataClassifier._SHORT_TEXT_MAX_LINES

        result = classifier.classify("notice.txt", many_lines)

        assert result.classification_failed is False
        assert result.data_type == DataType.UNSTRUCTURED_TEXT
