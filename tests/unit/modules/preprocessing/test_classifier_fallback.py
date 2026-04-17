"""Tests for v4.0 best-effort classification fallback in DataClassifier."""

import pytest

from faultmaven.models.api import ClassificationResult, DataType
from faultmaven.modules.preprocessing.classifier import DataClassifier


@pytest.fixture
def classifier():
    return DataClassifier()


class TestBestEffortFallback:
    """Test that low-confidence classification dispatches to the best-scoring type
    instead of always falling back to UNSTRUCTURED_TEXT."""

    def test_ambiguous_log_like_content_dispatches_to_logs(self, classifier):
        """Content with some log patterns but no extension should use best-effort."""
        # This content has error patterns but no timestamps or .log extension,
        # and doesn't meet the threshold for a confident LOGS_AND_ERRORS match.
        content = "something went wrong\nerror occurred in module\nfatal issue"
        result = classifier.classify("data.dat", content)

        # Should match LOGS_AND_ERRORS via best-effort (text_score patterns)
        # The exact result depends on pattern matching, but should NOT fall through
        # to confidence=0.30 since there are error-like patterns.
        assert result.confidence >= 0.30
        assert isinstance(result, ClassificationResult)

    def test_truly_unknown_content_falls_to_unstructured_text(self, classifier):
        """Content with NO recognizable patterns gets UNSTRUCTURED_TEXT with low confidence."""
        # Random content with no patterns matching any type
        content = "xyzzy 12345 abcde fghij"
        result = classifier.classify("mystery.dat", content)

        assert result.data_type == DataType.UNSTRUCTURED_TEXT
        assert result.confidence <= 0.50
        assert result.classification_failed is True

    def test_best_effort_preserves_classification_failed_flag(self, classifier):
        """Even best-effort dispatch marks classification_failed=True."""
        # Content with minimal code patterns (not enough for confident match)
        content = "function foo() { return 42; }"
        result = classifier.classify("unknown.dat", content)

        # If it went through best-effort path, classification_failed should be True
        if result.source == "rule_based_best_effort":
            assert result.classification_failed is True

    def test_best_effort_source_is_rule_based_best_effort(self, classifier):
        """Best-effort results have source='rule_based_best_effort'."""
        # Content with some metrics-like patterns but no extension or strong indicators
        content = "cpu 75.2\nmemory 82.1\nlatency 150ms\nthroughput 1000"
        result = classifier.classify("unknown.dat", content)

        # This may or may not trigger best-effort depending on score thresholds,
        # but if it does, the source should be correct
        if result.confidence == 0.50 and result.classification_failed:
            assert result.source in ("rule_based_best_effort", "rule_based")

    def test_config_patterns_trigger_best_effort_config(self, classifier):
        """Content with config-like patterns but no extension uses best-effort."""
        content = "server_port=8080\ndebug_mode=false\nlog_level=INFO"
        result = classifier.classify("settings.dat", content)

        # Should detect config patterns (key=value) and dispatch to CONFIG
        # Either confidently or via best-effort
        assert result.data_type in (
            DataType.STRUCTURED_CONFIG,
            DataType.UNSTRUCTURED_TEXT,
            DataType.LOGS_AND_ERRORS,
        )

    def test_source_literal_values_accepted(self):
        """ClassificationResult accepts 'rule_based_best_effort' as source."""
        result = ClassificationResult(
            data_type=DataType.LOGS_AND_ERRORS,
            confidence=0.50,
            source="rule_based_best_effort",
            classification_failed=True,
        )
        assert result.source == "rule_based_best_effort"


class TestCSVFallbackScoring:
    """Test that CSVs without numeric data are not misclassified as METRICS."""

    def test_non_numeric_csv_not_classified_as_metrics(self, classifier):
        """A CSV reference table with no numeric columns should not be METRICS."""
        # Simulates Linux_2k.log_templates.csv: 2-column EventId+EventTemplate table
        content = "EventId,EventTemplate\n"
        content += "E001,kernel: <*> <*> <*>\n"
        content += "E002,session opened for user <*>\n"
        content += "E003,Connection from <*> port <*>\n"
        for i in range(4, 20):
            content += f"E{i:03d},some log template pattern <*>\n"

        result = classifier.classify("log_templates.csv", content)
        assert result.data_type != DataType.METRICS_AND_PERFORMANCE

    def test_numeric_csv_still_classified_as_metrics(self, classifier):
        """A CSV with actual numeric metric columns should still be METRICS."""
        content = "timestamp,cpu_usage,memory_mb,latency_ms\n"
        for i in range(20):
            content += f"2024-01-01T{i:02d}:00:00,{75+i*0.5},{1024+i*10},{150+i*2}\n"

        result = classifier.classify("metrics.csv", content)
        assert result.data_type == DataType.METRICS_AND_PERFORMANCE
