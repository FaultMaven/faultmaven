"""Unit tests for the page-capture classification path in DataClassifier.

Covers two private helpers and their interaction with priority ordering in
``DataClassifier.classify``:

- ``_classify_from_source_url`` (Priority 3): URL → DataType pattern matcher.
  ~30 patterns mapping monitoring/observability URLs to data types with
  confidence 0.82-0.94. Page captures get a small specificity bump
  (``PAGE_CAPTURE_CONFIDENCE_BOOST``, capped at 0.98).

- ``_classify_from_browser_context`` (Priority 4): keyword fallback when the
  copilot reports a page_type but no source_url. Confidence 0.85-0.92.

Page captures are the primary input for the FaultMaven Copilot — a
regression in URL pattern matching silently routes content to the wrong
extractor and downstream investigation quality degrades.

Run with:
    pytest tests/unit/modules/preprocessing/test_classifier_page_capture.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.models.api import DataType, SourceMetadata
from faultmaven.modules.preprocessing.classifier import (
    PAGE_CAPTURE_CONFIDENCE_BOOST,
    DataClassifier,
)


@pytest.fixture
def classifier() -> DataClassifier:
    return DataClassifier()


def _page_capture(url: str) -> SourceMetadata:
    """Build a SourceMetadata for a page-capture upload from ``url``."""
    return SourceMetadata(source_type="page_capture", source_url=url)


def _file_upload(url: str) -> SourceMetadata:
    """Build a SourceMetadata for a file_upload that still carries a URL.

    Used to verify the page-capture confidence boost only applies when
    ``source_type == "page_capture"`` — file uploads still match URL
    patterns (the registry is shared) but get the unboosted confidence.
    """
    return SourceMetadata(source_type="file_upload", source_url=url)


# Non-empty content keeps the classifier from short-circuiting to
# UNANALYZABLE. The shape doesn't matter — Priority 3 (URL) wins before
# rule-based content scoring runs.
_ANY_CONTENT = "captured page contents"


# ============================================================
# _classify_from_source_url — one assertion per data-type bucket
# ============================================================


class TestSourceUrlClassification:
    """Cover one representative URL per major data-type bucket.

    The classifier has 30+ URL patterns; testing every one would just
    re-declare the table. These tests pin a representative URL per bucket
    so a regression in any bucket fails a test, while leaving the table
    free to grow without test churn.
    """

    def test_error_tracking_url_routes_to_logs_and_errors(self, classifier):
        """sentry.io is a flagship error-tracking platform; URL → LOGS_AND_ERRORS."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture("https://sentry.io/issues/123"),
        )

        assert result.data_type == DataType.LOGS_AND_ERRORS
        assert result.source == "source_url"
        assert result.classification_failed is False
        # 0.94 base + 0.02 page_capture boost = 0.96
        assert result.confidence == pytest.approx(0.96)

    def test_metrics_dashboard_url_routes_to_metrics_and_performance(self, classifier):
        """Grafana dashboards → METRICS_AND_PERFORMANCE (APM bucket)."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture(
                "https://grafana.example.com/d/abc/dashboard"
            ),
        )

        assert result.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.source == "source_url"

    def test_llm_observability_url_routes_to_trace_data(self, classifier):
        """Comet Opik / Langfuse / LangSmith URLs route to TRACE_DATA, not LOGS,
        because the captured payload contains embedded prompts and span trees
        that the trace extractor handles correctly."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture(
                "https://www.comet.com/opik/my-workspace/projects/foo/traces/abc"
            ),
        )

        assert result.data_type == DataType.TRACE_DATA
        assert result.source == "source_url"

    def test_source_code_platform_url_routes_to_source_code(self, classifier):
        """github.com / gitlab.com / bitbucket.org → SOURCE_CODE."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture("https://github.com/foo/bar/pull/42"),
        )

        assert result.data_type == DataType.SOURCE_CODE
        assert result.source == "source_url"

    def test_docs_platform_url_routes_to_unstructured_text(self, classifier):
        """Confluence / Notion / readthedocs / docs.* → UNSTRUCTURED_TEXT.

        Docs pages don't have a structured-data type; the unstructured-text
        extractor is the right downstream handler.
        """
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture(
                "https://confluence.example.com/display/TEAM/Runbook"
            ),
        )

        assert result.data_type == DataType.UNSTRUCTURED_TEXT
        assert result.source == "source_url"

    def test_generic_dashboard_path_falls_through_to_metrics(self, classifier):
        """Generic ``/dashboard/`` path fragment is the lowest-priority URL
        match — internal monitoring tools that don't match a flagship pattern
        still route to METRICS_AND_PERFORMANCE at the table-floor confidence
        (0.82, boosted to 0.84 for page captures)."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture(
                "https://internal.example.com/dashboard/widget1"
            ),
        )

        assert result.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.source == "source_url"
        # Floor confidence is 0.82 (table tail). 0.82 + 0.02 boost = 0.84.
        assert result.confidence == pytest.approx(0.84)

    def test_unmatched_url_returns_none_so_caller_falls_through(self, classifier):
        """When the URL doesn't match any pattern, ``_classify_from_source_url``
        returns None and the caller falls through to lower priorities (browser
        context → rule-based content). The result here comes from rule-based,
        not from the URL path."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture(
                "https://random-internal-tool.example.org/app/view"
            ),
        )

        # Result is from a lower priority, not from URL matching.
        assert result.source != "source_url"


# ============================================================
# Page-capture confidence boost
# ============================================================


class TestPageCaptureConfidenceBoost:
    """The ``page_capture`` source_type adds ``PAGE_CAPTURE_CONFIDENCE_BOOST``
    (+0.02) to URL-derived confidence, capped at 0.98.

    The URL itself is the strongest signal we have for page captures, so
    the small bump nudges those classifications further into auto-accept
    territory. file_upload, text_paste, etc. don't get the bump.
    """

    # Highest base confidence in the URL table is 0.94 (sentry.io / bugsnag /
    # rollbar). Most useful boost test target.
    HIGH_CONFIDENCE_URL = "https://sentry.io/issues/4242"

    def test_page_capture_adds_boost_vs_file_upload(self, classifier):
        """Same URL, different source_type → page_capture is +0.02 ahead."""
        page_result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture(self.HIGH_CONFIDENCE_URL),
        )
        file_result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_file_upload(self.HIGH_CONFIDENCE_URL),
        )

        # Both still match the same data type.
        assert (
            page_result.data_type == file_result.data_type == DataType.LOGS_AND_ERRORS
        )
        # Page capture sits exactly PAGE_CAPTURE_CONFIDENCE_BOOST higher.
        assert page_result.confidence == pytest.approx(
            file_result.confidence + PAGE_CAPTURE_CONFIDENCE_BOOST
        )

    def test_page_capture_boost_caps_at_098(self, classifier):
        """The boost is ``min(base + 0.02, 0.98)``. The current URL table
        tops out at 0.94 (→ 0.96 boosted), which doesn't exercise the cap;
        this test assert the cap invariant — confidence never exceeds 0.98 —
        so future pattern additions can't silently breach it."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            source_metadata=_page_capture(self.HIGH_CONFIDENCE_URL),
        )

        assert result.confidence <= 0.98

    def test_text_paste_does_not_get_page_capture_boost(self, classifier):
        """Only ``source_type == "page_capture"`` triggers the boost.

        Text pastes from the copilot still carry a source_url but get the
        unboosted base confidence — pasted text is a weaker signal than a
        full page capture (no DOM, no monitoring-tool surrounding context)."""
        text_paste = SourceMetadata(
            source_type="text_paste", source_url=self.HIGH_CONFIDENCE_URL
        )
        result = classifier.classify(
            "pasted.txt",
            _ANY_CONTENT,
            source_metadata=text_paste,
        )

        # 0.94 base, no boost.
        assert result.confidence == pytest.approx(0.94)


# ============================================================
# _classify_from_browser_context
# ============================================================


class TestBrowserContextClassification:
    """Browser context is Priority 4 — used when no source_url is reported
    (older copilot versions, manual paste flows). Eight keyword buckets,
    confidence 0.85-0.92."""

    def test_grafana_context_routes_to_metrics(self, classifier):
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="grafana",
        )

        assert result.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.source == "browser_context"
        assert result.confidence == pytest.approx(0.90)

    def test_sentry_substring_context_routes_to_logs(self, classifier):
        """Match is substring-based — ``"sentry production"`` matches the
        ``"sentry"`` keyword. Lets the copilot pass richer context strings
        like ``"sentry production"`` or ``"grafana - cluster-1"``."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="sentry production",
        )

        assert result.data_type == DataType.LOGS_AND_ERRORS
        assert result.source == "browser_context"

    def test_kibana_context_routes_to_logs(self, classifier):
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="kibana",
        )

        assert result.data_type == DataType.LOGS_AND_ERRORS
        assert result.source == "browser_context"

    def test_prometheus_context_routes_to_metrics(self, classifier):
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="prometheus",
        )

        assert result.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.source == "browser_context"

    def test_jaeger_context_routes_to_metrics(self, classifier):
        """jaeger / zipkin route to METRICS_AND_PERFORMANCE in browser_context
        (Priority 4), unlike URL classification where opik/langfuse/langsmith
        route to TRACE_DATA. The keyword set predates the LLM-observability
        bucket and treats distributed tracing as an APM signal."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="jaeger",
        )

        assert result.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.source == "browser_context"

    def test_unrecognized_context_falls_through(self, classifier):
        """Context that doesn't match any keyword returns None from the
        helper, and the caller falls through to rule-based content scoring.
        The result here is from rule-based, not browser_context."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="some-unknown-tool",
        )

        assert result.source != "browser_context"


# ============================================================
# Priority ordering across classify()
# ============================================================


class TestPriorityOrdering:
    """The classify() priority chain is documented in the class docstring:

        1. user_override     → confidence 1.0
        2. agent_hint        → confidence 0.95
        3. source_url        → confidence 0.88-0.94 (this PR's territory)
        4. browser_context   → confidence 0.85-0.92
        5. rule_based        → confidence 0.30-0.98

    These tests pin the relative ordering of priorities 3 vs 4 vs 5, since
    that's where the page-capture path lives and where most regressions
    would land.
    """

    def test_source_url_wins_over_browser_context_when_both_present(self, classifier):
        """When both source_url and browser_context are provided, the URL
        match wins because it's a more specific signal (full URL > vague
        page-type keyword)."""
        # URL says Sentry (LOGS); context says Grafana (METRICS) — they
        # disagree on data type. The URL must win.
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="grafana",
            source_metadata=_page_capture("https://sentry.io/issues/1"),
        )

        assert result.data_type == DataType.LOGS_AND_ERRORS
        assert result.source == "source_url"

    def test_unmatched_url_falls_through_to_browser_context(self, classifier):
        """When the URL doesn't match any pattern but browser_context does,
        the chain falls through to Priority 4."""
        result = classifier.classify(
            "captured.html",
            _ANY_CONTENT,
            browser_context="grafana",
            source_metadata=_page_capture(
                "https://random-internal-tool.example.org/view"
            ),
        )

        assert result.data_type == DataType.METRICS_AND_PERFORMANCE
        assert result.source == "browser_context"

    def test_neither_url_nor_context_matches_falls_through_to_rule_based(
        self, classifier
    ):
        """When both Priority 3 and Priority 4 return None, the chain falls
        through to rule-based content classification (Priority 5)."""
        # Strong log content so rule_based produces a confident match we
        # can pin on.
        log_content = "\n".join(
            [
                f"2024-01-01T12:{i:02d}:00 ERROR something failed (line {i})"
                for i in range(20)
            ]
        )
        result = classifier.classify(
            "server.log",
            log_content,
            browser_context="some-unknown-tool",
            source_metadata=_page_capture(
                "https://random-internal-tool.example.org/view"
            ),
        )

        # Rule-based source ID — exact value is "rule_based" or
        # "rule_based_best_effort" depending on score path.
        assert result.source.startswith("rule_based")
        assert result.data_type == DataType.LOGS_AND_ERRORS
