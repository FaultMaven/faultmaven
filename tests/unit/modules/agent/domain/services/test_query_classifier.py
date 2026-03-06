"""Tests for the query classifier — scenario-driven processing mode routing.

Validates that classify_query() correctly routes user messages to
TRIAGE or DIRECTED_ANALYSIS based on heuristic entity detection
and phrasing analysis. No LLM involved — this is deterministic.
"""

import pytest

from faultmaven.modules.agent.domain.services.query_classifier import (
    ProcessingMode,
    QueryClassification,
    _extract_entities,
    _is_generic_request,
    _has_interrogative_structure,
    classify_query,
)

# ============================================================
# Entity Extraction
# ============================================================


class TestExtractEntities:
    """Tests for _extract_entities() regex-based entity detection."""

    def test_extracts_hh_mm_timestamps(self):
        entities = _extract_entities("what happened at 14:00?")
        assert "timestamps" in entities
        assert "14:00" in entities["timestamps"]

    def test_extracts_hh_mm_ss_timestamps(self):
        entities = _extract_entities("error at 14:30:15")
        assert "14:30:15" in entities["timestamps"]

    def test_extracts_iso_dates(self):
        entities = _extract_entities("check logs for 2024-01-15")
        assert "2024-01-15" in entities["timestamps"]

    def test_extracts_month_day_dates(self):
        entities = _extract_entities("crash on Jan 15 was bad")
        assert "Jan 15" in entities["timestamps"]

    def test_extracts_http_status_codes(self):
        entities = _extract_entities("why are we getting 503 and 404 errors?")
        assert "status_codes" in entities
        assert "503" in entities["status_codes"]
        assert "404" in entities["status_codes"]

    def test_ignores_non_error_status_codes(self):
        """Only 4xx and 5xx should be captured."""
        entities = _extract_entities("we got 200 OK and 301 redirect")
        assert "status_codes" not in entities

    def test_extracts_error_keywords(self):
        entities = _extract_entities("the process was killed due to OOM")
        assert "error_keywords" in entities
        assert "oom" in entities["error_keywords"]
        assert "killed" in entities["error_keywords"]

    def test_extracts_multi_word_error_keywords(self):
        entities = _extract_entities("seeing connection refused from the backend")
        assert "connection refused" in entities["error_keywords"]

    def test_extracts_service_names(self):
        entities = _extract_entities("check logs from nginx and errors from redis")
        assert "services" in entities
        assert "nginx" in entities["services"]
        assert "redis" in entities["services"]

    def test_extracts_ip_addresses(self):
        entities = _extract_entities("connection from 10.0.0.5 is blocked")
        assert "ip_addresses" in entities
        assert "10.0.0.5" in entities["ip_addresses"]

    def test_no_entities_from_plain_text(self):
        entities = _extract_entities("what is the root cause?")
        assert not any(entities.values())

    def test_multiple_entity_types(self):
        entities = _extract_entities("why did nginx return 502 at 14:00 from 10.0.0.1?")
        assert "timestamps" in entities
        assert "status_codes" in entities
        assert "services" in entities
        assert "ip_addresses" in entities


# ============================================================
# Generic Request Detection
# ============================================================


class TestIsGenericRequest:
    """Tests for _is_generic_request() phrasing detection."""

    @pytest.mark.parametrize(
        "message",
        [
            "analyze this log file",
            "check this for me",
            "look at the errors",
            "what's in here",
            "what is going on",
            "anything wrong with this?",
            "see any issues?",
            "can you look at this",
            "here's the log file",
            "tell me what's happening",
            "what do you see",
            "review my application logs",
            "examine this config",
        ],
    )
    def test_detects_generic_phrases(self, message):
        assert _is_generic_request(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "why did nginx return 502 at 14:00?",
            "what caused the OOM kill?",
            "show me errors from redis between 13:00 and 14:00",
            "is the connection timeout related to DNS?",
        ],
    )
    def test_specific_questions_are_not_generic(self, message):
        assert _is_generic_request(message) is False


# ============================================================
# Interrogative Structure Detection
# ============================================================


class TestHasInterrogativeStructure:
    """Tests for _has_interrogative_structure()."""

    def test_detects_question_with_interrogative(self):
        assert _has_interrogative_structure("what caused the error?") is True

    def test_detects_why_question(self):
        assert _has_interrogative_structure("why is the app slow?") is True

    def test_no_question_mark_returns_false(self):
        assert _has_interrogative_structure("check the logs") is False

    def test_question_mark_without_interrogative(self):
        """A lone '?' without interrogative words is not detected."""
        assert _has_interrogative_structure("hmm?") is False


# ============================================================
# classify_query — Core Routing Logic
# ============================================================


class TestClassifyQuery:
    """Tests for classify_query() end-to-end routing decisions."""

    # --- TRIAGE cases ---

    def test_no_message_with_attachments_is_triage(self):
        """User dropped a file with no question → Triage."""
        result = classify_query("", has_attachments=True)
        assert result.mode == ProcessingMode.TRIAGE
        assert result.confidence >= 0.9

    def test_no_message_no_attachments_is_triage(self):
        """Empty turn → Triage (low confidence)."""
        result = classify_query("")
        assert result.mode == ProcessingMode.TRIAGE

    def test_generic_phrasing_no_entities_is_triage(self):
        """Generic request without technical entities → Triage."""
        result = classify_query("analyze this log file")
        assert result.mode == ProcessingMode.TRIAGE
        assert result.confidence >= 0.8

    def test_whitespace_only_is_triage(self):
        result = classify_query("   ")
        assert result.mode == ProcessingMode.TRIAGE

    # --- DIRECTED_ANALYSIS cases ---

    def test_entities_plus_question_is_da(self):
        """Specific entities + interrogative structure → DA (high confidence)."""
        result = classify_query("what caused the 502 errors at 14:00?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence >= 0.9
        assert "status_codes" in result.detected_entities
        assert "timestamps" in result.detected_entities

    def test_entities_without_question_not_generic_is_da(self):
        """Entities + non-generic phrasing → DA."""
        result = classify_query("investigate 502s at 14:00")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence >= 0.7

    def test_question_without_entities_is_da(self):
        """Question with interrogative but no technical entities → DA (lower confidence)."""
        result = classify_query("why is the app slow?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence >= 0.5

    def test_ambiguous_defaults_to_da(self):
        """Ambiguous input (no entities, no generic phrasing, no question) → DA."""
        result = classify_query("performance degradation")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert result.confidence == 0.5

    def test_generic_with_entities_routes_to_da(self):
        """Generic phrasing BUT with entities → entities win, route to DA."""
        result = classify_query("check this for OOM errors")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert "error_keywords" in result.detected_entities

    def test_service_name_triggers_da(self):
        """Mentioning a service name with a question → DA."""
        result = classify_query("what errors is nginx throwing?")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert "services" in result.detected_entities

    def test_error_keyword_triggers_da(self):
        result = classify_query("investigate the connection timeout")
        assert result.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert "error_keywords" in result.detected_entities

    # --- Classification dataclass ---

    def test_classification_has_correct_fields(self):
        result = classify_query("what happened at 14:00?")
        assert isinstance(result, QueryClassification)
        assert isinstance(result.mode, ProcessingMode)
        assert isinstance(result.detected_entities, dict)
        assert 0.0 <= result.confidence <= 1.0

    def test_processing_mode_enum_values(self):
        assert ProcessingMode.TRIAGE.value == "triage"
        assert ProcessingMode.DIRECTED_ANALYSIS.value == "directed_analysis"
        assert ProcessingMode.SEMANTIC_SEARCH.value == "semantic_search"
