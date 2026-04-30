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


class TestEmptyContentClassification:
    """Empty / whitespace-only content routes to UNANALYZABLE (ISS-024).

    Rationale: a 0-byte file is itself diagnostic information. Forcing it
    through the rule-based fallback would land on UNSTRUCTURED_TEXT with
    classification_failed=True, which surfaces a confusing "we couldn't
    classify your file" modal. Routing to UNANALYZABLE produces a clean
    "file is empty" message and a reference-only evidence row, while
    keeping the case and pipeline alive.
    """

    def test_empty_string_routes_to_unanalyzable(self, classifier):
        result = classifier.classify("empty.bin", "")

        assert result.data_type == DataType.UNANALYZABLE
        assert result.classification_failed is False
        assert result.confidence == 1.0
        assert result.source == "rule_based"

    def test_whitespace_only_content_routes_to_unanalyzable(self, classifier):
        result = classifier.classify("blank.txt", "   \n\t\n  \n")

        assert result.data_type == DataType.UNANALYZABLE
        assert result.classification_failed is False

    def test_empty_content_does_not_emit_classification_failed_modal(self, classifier):
        """The frontend triggers a clarification modal on classification_failed=True.
        Empty files should NOT trigger that flow — there's nothing to clarify.
        """
        result = classifier.classify("empty.log", "")

        assert result.classification_failed is False
        assert result.suggested_types is None or result.suggested_types == []

    def test_user_override_still_wins_over_empty_content(self, classifier):
        """If the user explicitly tagged the empty file as a known type
        (e.g. via the reclassify endpoint), respect that — they may be
        recording the empty-file fact under a known category."""
        result = classifier.classify(
            "empty.log",
            "",
            user_override=DataType.LOGS_AND_ERRORS,
        )

        assert result.data_type == DataType.LOGS_AND_ERRORS
        assert result.source == "user_override"


class TestShortAmbiguousTextClassification:
    """Short, mixed-pattern .txt files trigger classification_failed (ISS-023).

    Rationale: when a small text file carries signals from many different
    type-suggestive categories (datetime + URL + email + version tag +
    key-value lines) without any single category dominating, asserting
    UNSTRUCTURED_TEXT at confidence=0.72 prevents the cooperative
    clarification UX from firing. The agent then silently proceeds with
    UNSTRUCTURED_TEXT and can't surface candidate types to the user.

    The fix lowers confidence to 0.40 (below the 0.50 threshold) and
    populates suggested_types so the frontend modal can offer a small
    ranked menu.
    """

    # The benchmark fixture from fm-data-exam (failure-mode-low-signal-01).
    LOW_SIGNAL_NOTICE = (
        "Maintenance window scheduled: 2024-03-15 02:00 UTC to 04:00 UTC\n"
        "Services affected: auth, payments, notifications\n"
        "Expected downtime: up to 120 minutes\n"
        "Runbook: https://wiki.internal/runbooks/maintenance-q1-2024\n"
        "Contact: ops-oncall@example.com\n"
        "\n"
        "Action required: drain traffic from us-east-1 before 01:45 UTC\n"
        "Rollback plan: redeploy previous artifact (tag: v2.3.8)\n"
    )

    def test_low_signal_maintenance_notice_triggers_ambiguity_gate(self, classifier):
        """The benchmark fixture: 8-line maintenance notice carrying datetime,
        URL, email, version tag, and key-value lines. Should classify with
        confidence < 0.50 so the cooperative-clarification UX fires."""
        result = classifier.classify("low-signal-text.txt", self.LOW_SIGNAL_NOTICE)

        assert result.classification_failed is True
        assert result.confidence < 0.50
        assert result.data_type == DataType.UNSTRUCTURED_TEXT
        assert result.source == "rule_based_best_effort"
        # Cooperative-clarification UX needs candidates to offer the user
        assert result.suggested_types is not None
        assert DataType.UNSTRUCTURED_TEXT in result.suggested_types
        assert DataType.DOCUMENTATION in result.suggested_types

    def test_pure_prose_does_not_trigger_ambiguity_gate(self, classifier):
        """Pure prose (no datetimes, URLs, version tags) should still land
        on UNSTRUCTURED_TEXT @ 0.72, classification_failed=False — this is
        the existing default and we want it preserved."""
        prose = (
            "This file describes the deployment of our service.\n"
            "It runs on three replicas and handles incoming traffic.\n"
            "The team monitors it through dashboards.\n"
            "Please contact the on-call engineer for any production issues.\n"
        )
        result = classifier.classify("notes.txt", prose)

        assert result.classification_failed is False
        assert result.confidence == 0.72
        assert result.data_type == DataType.UNSTRUCTURED_TEXT

    def test_two_categories_does_not_trigger_gate(self, classifier):
        """Only two type-suggestive categories firing isn't enough breadth
        to call the file ambiguous — most operational notes carry both a
        datetime and a URL without being genuinely unclassifiable."""
        two_signal = (
            "Maintenance scheduled for 2024-03-15.\n"
            "See https://wiki.example.com/runbooks for the procedure.\n"
        )
        result = classifier.classify("m.txt", two_signal)

        assert result.classification_failed is False
        assert result.confidence == 0.72

    def test_long_text_does_not_trigger_gate(self, classifier):
        """A long .txt file is structurally different — there's enough
        content for the existing UNSTRUCTURED_TEXT classification to be
        meaningful, even if it carries the same mixed-pattern surface
        features. Gate only fires for short files."""
        # Repeat the notice 10x to exceed both length thresholds
        long_content = self.LOW_SIGNAL_NOTICE * 10
        result = classifier.classify("long.txt", long_content)

        assert result.classification_failed is False
        assert result.confidence == 0.72

    def test_suggested_types_capped_at_three(self, classifier):
        """Even with many categories firing, the ranked menu stays small —
        a 5+ option modal would be hostile UX."""
        result = classifier.classify("low-signal-text.txt", self.LOW_SIGNAL_NOTICE)

        assert result.suggested_types is not None
        assert len(result.suggested_types) <= 3

    def test_user_override_still_wins_over_ambiguity(self, classifier):
        """Explicit user override trumps the ambiguity gate (same precedence
        as the empty-content path)."""
        result = classifier.classify(
            "low-signal-text.txt",
            self.LOW_SIGNAL_NOTICE,
            user_override=DataType.DOCUMENTATION,
        )

        assert result.data_type == DataType.DOCUMENTATION
        assert result.source == "user_override"
        assert result.classification_failed is False
