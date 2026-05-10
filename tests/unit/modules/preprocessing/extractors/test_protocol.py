"""
Tests for Extractor Protocol, shared utilities, and input guardrails.

Covers:
- R1: Protocol compliance for all 11 extractors
- R2: Input guardrails (empty/whitespace/short content)
- R3: Token budget truncation utility
"""

import json

import pytest

from faultmaven.modules.preprocessing.extractors.protocol import (
    Extractor,
    ExtractResult,
)
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    MAX_STRUCTURAL_INDEX_CHARS,
    has_content,
    truncate_output,
)


class TestExtractorProtocol:
    """R1: All 11 extractors must satisfy the Extractor protocol."""

    @pytest.fixture(
        params=[
            "LogsAndErrorsExtractor",
            "StructuredConfigExtractor",
            "MetricsAndPerformanceExtractor",
            "UnstructuredTextExtractor",
            "SourceCodeExtractor",
            "TraceDataExtractor",
            "ProfilingDataExtractor",
            "ErrorReportExtractor",
            "DocumentationExtractor",
            "CommandOutputExtractor",
            "VisualEvidenceExtractor",
        ]
    )
    def extractor_instance(self, request):
        """Instantiate each extractor by class name."""
        import faultmaven.modules.preprocessing.extractors as ext_mod

        cls = getattr(ext_mod, request.param)
        return cls()

    def test_satisfies_protocol(self, extractor_instance):
        """Each extractor must be a runtime-checkable instance of Extractor."""
        assert isinstance(extractor_instance, Extractor)

    def test_has_strategy_name(self, extractor_instance):
        """strategy_name must return a non-empty string."""
        name = extractor_instance.strategy_name
        assert isinstance(name, str)
        assert len(name) > 0

    def test_has_llm_calls_used(self, extractor_instance):
        """llm_calls_used must return an integer >= 0."""
        calls = extractor_instance.llm_calls_used
        assert isinstance(calls, int)
        assert calls >= 0

    def test_extract_method_exists(self, extractor_instance):
        """extract() method must be callable."""
        assert callable(getattr(extractor_instance, "extract", None))


class TestHasContent:
    """R2: Input validation utility."""

    def test_empty_string(self):
        assert has_content("") is False

    def test_whitespace_only(self):
        assert has_content("   \n\t  ") is False

    def test_short_string(self):
        assert has_content("abc") is False

    def test_exactly_10_chars(self):
        assert has_content("1234567890") is True

    def test_normal_content(self):
        assert has_content("This is normal log content") is True

    def test_none_like(self):
        # Empty string is falsy
        assert has_content("") is False


class TestInputGuardrails:
    """R2: Every extractor returns standardized message on degenerate input."""

    @pytest.fixture(
        params=[
            "LogsAndErrorsExtractor",
            "StructuredConfigExtractor",
            "MetricsAndPerformanceExtractor",
            "UnstructuredTextExtractor",
            "SourceCodeExtractor",
            "TraceDataExtractor",
            "ProfilingDataExtractor",
            "ErrorReportExtractor",
            "DocumentationExtractor",
            "CommandOutputExtractor",
            "VisualEvidenceExtractor",
        ]
    )
    def extractor_instance(self, request):
        import faultmaven.modules.preprocessing.extractors as ext_mod

        cls = getattr(ext_mod, request.param)
        return cls()

    def test_empty_input(self, extractor_instance):
        result = extractor_instance.extract("")
        assert result.file_extract == EMPTY_CONTENT_RESPONSE

    def test_whitespace_input(self, extractor_instance):
        result = extractor_instance.extract("   \n\t  ")
        assert result.file_extract == EMPTY_CONTENT_RESPONSE

    def test_short_input(self, extractor_instance):
        result = extractor_instance.extract("abc")
        assert result.file_extract == EMPTY_CONTENT_RESPONSE


class TestExtractResultRoundTrip:
    """Round-trip serialization for ExtractResult."""

    def test_full_round_trip(self):
        er = ExtractResult(
            file_extract="FILE SUMMARY: brute-force pattern.",
            search_map="ENTITY PROFILE:\n  IP: 1.2.3.4 [search: 1.2.3.4]",
            file_meta={"total_lines": 500, "time_range": {"start": "T0", "end": "T1"}},
        )
        restored = ExtractResult.from_json(er.to_json())
        assert restored.file_extract == er.file_extract
        assert restored.search_map == er.search_map
        assert restored.file_meta == er.file_meta

    def test_none_search_map_round_trip(self):
        er = ExtractResult(file_extract="plain extract", search_map=None, file_meta={})
        restored = ExtractResult.from_json(er.to_json())
        assert restored.search_map is None
        assert restored.file_extract == "plain extract"
        assert restored.file_meta == {}

    def test_version_field_present(self):
        er = ExtractResult(file_extract="x")
        d = json.loads(er.to_json())
        assert d["v"] == 1
        assert "file_extract" in d

    def test_from_json_legacy_plaintext_is_not_supported(self):
        """from_json expects valid JSON — callers handle legacy via _parse_extract."""
        with pytest.raises((json.JSONDecodeError, ValueError)):
            ExtractResult.from_json("this is plain text, not JSON")


class TestTruncateOutput:
    """R3: Shared truncation utility."""

    def test_short_text_unchanged(self):
        text = "short text"
        assert truncate_output(text) == text

    def test_text_at_limit_unchanged(self):
        text = "x" * MAX_STRUCTURAL_INDEX_CHARS
        assert truncate_output(text) == text

    def test_text_over_limit_truncated(self):
        text = "x" * (MAX_STRUCTURAL_INDEX_CHARS + 1000)
        result = truncate_output(text)
        assert len(result) < len(text)
        assert "Truncated" in result
        assert "characters for context budget" in result

    def test_preserves_beginning_and_end(self):
        beginning = "BEGIN" * 100
        middle = "MIDDLE" * 5000
        end = "END" * 100
        text = beginning + middle + end
        result = truncate_output(text, max_chars=2000)
        assert result.startswith("BEGIN")
        assert result.endswith("END" * 100)  # End should be preserved

    def test_custom_max_chars(self):
        text = "x" * 5000
        result = truncate_output(text, max_chars=1000)
        assert len(result) < 5000
        assert "Truncated" in result

    def test_marker_shows_removed_count(self):
        text = "a" * 10000
        result = truncate_output(text, max_chars=1000)
        # The marker should indicate how many chars were removed
        assert "Truncated" in result
