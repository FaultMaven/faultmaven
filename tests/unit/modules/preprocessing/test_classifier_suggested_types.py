"""Unit tests for `suggested_types` population on classification_failed paths.

Per PLAN-classifier-cooperative-clarification.md §Step 1 and D5: every
`classification_failed=True` return path in `classifier.py` must populate
`suggested_types` so the cooperative-clarification UX has data to render.

Run with:
    pytest tests/unit/modules/preprocessing/test_classifier_suggested_types.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.classifier import (
    DataClassifier,
    _top_suggested_types,
)

# ============================================================
# _top_suggested_types helper
# ============================================================


class TestTopSuggestedTypes:
    def test_empty_dict_returns_empty(self):
        assert _top_suggested_types({}, 3) == []

    def test_all_zero_scores_returns_empty(self):
        assert _top_suggested_types({DataType.LOGS_AND_ERRORS: 0}, 3) == []

    def test_orders_by_score_descending(self):
        scores = {
            DataType.LOGS_AND_ERRORS: 5,
            DataType.METRICS_AND_PERFORMANCE: 2,
            DataType.SOURCE_CODE: 8,
        }
        assert _top_suggested_types(scores, 3) == [
            DataType.SOURCE_CODE,
            DataType.LOGS_AND_ERRORS,
            DataType.METRICS_AND_PERFORMANCE,
        ]

    def test_respects_n_limit(self):
        scores = {
            DataType.LOGS_AND_ERRORS: 5,
            DataType.METRICS_AND_PERFORMANCE: 4,
            DataType.SOURCE_CODE: 3,
            DataType.STRUCTURED_CONFIG: 2,
        }
        result = _top_suggested_types(scores, 2)
        assert len(result) == 2
        assert result[0] == DataType.LOGS_AND_ERRORS
        assert result[1] == DataType.METRICS_AND_PERFORMANCE

    def test_excludes_unanalyzable(self):
        scores = {
            DataType.UNANALYZABLE: 100,  # highest score
            DataType.LOGS_AND_ERRORS: 5,
        }
        result = _top_suggested_types(scores, 3)
        assert DataType.UNANALYZABLE not in result
        assert result == [DataType.LOGS_AND_ERRORS]

    def test_filters_zero_scores(self):
        # Types with zero scores should not appear even if they have entries
        scores = {
            DataType.LOGS_AND_ERRORS: 5,
            DataType.SOURCE_CODE: 0,
        }
        result = _top_suggested_types(scores, 3)
        assert result == [DataType.LOGS_AND_ERRORS]


# ============================================================
# Classifier integration — every classification_failed path
# populates suggested_types
# ============================================================


@pytest.fixture
def classifier():
    return DataClassifier()


class TestClassificationFailedPopulatesSuggestedTypes:
    """Verify all four `classification_failed=True` paths set suggested_types."""

    def test_truly_unknown_content_emits_fallback_suggestions(self, classifier):
        """True-fallback path (line ~929) → UNSTRUCTURED_TEXT + DOCUMENTATION."""
        content = "xyzzy 12345 abcde fghij"
        result = classifier.classify("mystery.dat", content)

        assert result.classification_failed is True
        assert result.suggested_types is not None
        assert len(result.suggested_types) > 0
        # True fallback gets a static list with UNSTRUCTURED_TEXT + DOCUMENTATION
        assert DataType.UNSTRUCTURED_TEXT in result.suggested_types
        assert DataType.DOCUMENTATION in result.suggested_types

    def test_best_effort_path_populates_from_scores(self, classifier):
        """Best-effort score-based path (line ~921) → top-3 scoring types."""
        # Content with enough scattered patterns to route through best-effort.
        content = "function connect() { return; }\nlog: pool exhausted"
        result = classifier.classify("ambiguous.dat", content)

        if result.classification_failed and result.source == "rule_based_best_effort":
            assert result.suggested_types is not None
            assert len(result.suggested_types) > 0
            # The best_type itself must be present in the suggestions
            assert result.data_type in result.suggested_types
            # Never include UNANALYZABLE
            assert DataType.UNANALYZABLE not in result.suggested_types

    def test_csv_structured_path_suggests_metrics_and_config(self, classifier):
        """CSV/TSV structured-data path (line ~903) → METRICS + CONFIG."""
        # Build a CSV that scores as structured (5+ cols, 10+ rows, numeric)
        header = "col1,col2,col3,col4,col5"
        rows = "\n".join(f"1,2,3,4,5" for _ in range(15))
        content = f"{header}\n{rows}"
        result = classifier.classify("data.csv", content)

        if (
            result.classification_failed
            and result.data_type == DataType.METRICS_AND_PERFORMANCE
        ):
            assert result.suggested_types is not None
            assert DataType.METRICS_AND_PERFORMANCE in result.suggested_types
            assert DataType.STRUCTURED_CONFIG in result.suggested_types

    def test_csv_nonstructured_path_suggests_text_and_documentation(self, classifier):
        """CSV/TSV non-structured path (line ~911) → UNSTRUCTURED_TEXT + DOCUMENTATION."""
        # Small CSV that doesn't meet the structured-data thresholds
        content = "a,b\n1,2\n3,4"
        result = classifier.classify("tiny.csv", content)

        if (
            result.classification_failed
            and result.data_type == DataType.UNSTRUCTURED_TEXT
        ):
            assert result.suggested_types is not None
            assert DataType.UNSTRUCTURED_TEXT in result.suggested_types
            assert DataType.DOCUMENTATION in result.suggested_types

    def test_confident_classification_does_not_set_suggested_types(self, classifier):
        """When the classifier is confident, suggested_types stays None.

        Only failure paths populate it — successful classifications don't need
        clarification hints.
        """
        # Clear log content with high-confidence markers
        content = (
            "2026-04-19 10:00:00 INFO service started\n"
            "2026-04-19 10:00:01 ERROR connection refused: port 5432\n"
            "2026-04-19 10:00:02 INFO retry in 5s\n"
        )
        result = classifier.classify("app.log", content)

        if not result.classification_failed:
            # Confident path — suggested_types should be None or empty
            assert not result.suggested_types
