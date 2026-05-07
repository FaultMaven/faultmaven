"""Unit tests for the classification-clarification suggestion injector.

Per PLAN-classifier-cooperative-clarification.md §Step 2 and D1: when an
attachment hits `classification_failed=True`, the InvestigationService
appends COOPERATIVE suggestions to the turn response so the user can
clarify the file type with a single click.

Run with:
    pytest tests/unit/modules/agent/test_classification_clarification_suggestions.py -v
"""

from __future__ import annotations

import pytest

from faultmaven.modules.agent.domain.services.investigation_service import (
    _build_classification_clarification_suggestions,
    _PreprocessedAttachment,
)
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)

# ============================================================
# Fixtures
# ============================================================


def _make_evidence() -> Evidence:
    """Minimal Evidence for constructing _PreprocessedAttachment."""
    return Evidence(
        category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
        primary_purpose="user_submitted_data",
        summary="test",
        source_type=EvidenceSourceType.LOGS,
        form=EvidenceForm.DOCUMENT,
        collected_by="user",
        collected_at_turn=1,
    )


def _make_failed_attachment(
    *,
    suggested_types: list[str] | None,
    filename: str = "foo.txt",
) -> _PreprocessedAttachment:
    return _PreprocessedAttachment(
        evidence=_make_evidence(),
        classification_failed=True,
        suggested_types=suggested_types,
        attachment_filename=filename,
    )


def _make_successful_attachment() -> _PreprocessedAttachment:
    return _PreprocessedAttachment(
        evidence=_make_evidence(),
        classification_failed=False,
    )


# ============================================================
# Tests
# ============================================================


class TestClarificationSuggestionInjector:
    def test_no_classification_failure_emits_nothing(self):
        """Happy-path turns get no clarification suggestions."""
        result = _build_classification_clarification_suggestions(
            [_make_successful_attachment()]
        )
        assert result == []

    def test_empty_list_emits_nothing(self):
        assert _build_classification_clarification_suggestions([]) == []

    def test_generates_suggestions_from_suggested_types(self):
        pa = _make_failed_attachment(
            suggested_types=["logs_and_errors", "documentation"],
        )
        suggestions = _build_classification_clarification_suggestions([pa])

        # 2 from suggested_types + 1 "Something else" fallback = 3
        assert len(suggestions) == 3
        labels = [s.label for s in suggestions]
        assert labels[0] == "Application logs"
        assert labels[1] == "Documentation"
        assert labels[-1] == "Something else"

    def test_all_suggestions_are_cooperative_query_submit(self):
        pa = _make_failed_attachment(suggested_types=["logs_and_errors"])
        suggestions = _build_classification_clarification_suggestions([pa])

        for s in suggestions:
            assert s.type == "COOPERATIVE"
            assert s.cooperative_action == "query_submit"

    def test_payload_references_filename(self):
        pa = _make_failed_attachment(
            suggested_types=["logs_and_errors"],
            filename="investigation_notes.txt",
        )
        suggestions = _build_classification_clarification_suggestions([pa])

        for s in suggestions:
            assert "investigation_notes.txt" in s.payload

    def test_empty_suggested_types_emits_only_fallback(self):
        pa = _make_failed_attachment(suggested_types=[])
        suggestions = _build_classification_clarification_suggestions([pa])

        assert len(suggestions) == 1
        assert suggestions[0].label == "Something else"

    def test_none_suggested_types_emits_only_fallback(self):
        pa = _make_failed_attachment(suggested_types=None)
        suggestions = _build_classification_clarification_suggestions([pa])

        assert len(suggestions) == 1
        assert suggestions[0].label == "Something else"

    def test_unstructured_text_collapses_into_fallback_not_duplicate(self):
        # Classifier suggested unstructured_text — we should NOT emit it as a
        # separate suggestion because "Something else" already covers it.
        pa = _make_failed_attachment(
            suggested_types=["unstructured_text", "documentation"],
        )
        suggestions = _build_classification_clarification_suggestions([pa])

        labels = [s.label for s in suggestions]
        # documentation + fallback, no duplicate unstructured_text entry
        assert labels == ["Documentation", "Something else"]

    def test_caps_at_3_type_specific_plus_fallback(self):
        # More than 3 suggested types → cap at 3 + 1 fallback = 4 total
        pa = _make_failed_attachment(
            suggested_types=[
                "logs_and_errors",
                "source_code",
                "structured_config",
                "metrics_and_performance",  # fourth — should not appear
            ],
        )
        suggestions = _build_classification_clarification_suggestions([pa])

        assert len(suggestions) == 4  # 3 + fallback
        labels = [s.label for s in suggestions]
        assert "Application logs" in labels
        assert "Source code" in labels
        assert "Configuration" in labels
        assert "Metrics" not in labels  # dropped — over the cap
        assert labels[-1] == "Something else"

    def test_preserves_suggested_types_order(self):
        # The classifier orders by priority; we preserve that order.
        pa = _make_failed_attachment(
            suggested_types=["structured_config", "logs_and_errors", "source_code"],
        )
        suggestions = _build_classification_clarification_suggestions([pa])

        labels = [s.label for s in suggestions]
        # Fallback always last; everything before it in classifier order.
        assert labels == [
            "Configuration",
            "Application logs",
            "Source code",
            "Something else",
        ]

    def test_skips_unknown_data_type_values(self):
        """A DataType value with no friendly name (e.g. typo, new enum)
        should be silently skipped, not crash."""
        pa = _make_failed_attachment(
            suggested_types=["logs_and_errors", "nonsense_not_a_type"],
        )
        suggestions = _build_classification_clarification_suggestions([pa])

        labels = [s.label for s in suggestions]
        # logs + fallback. Unknown type skipped without error.
        assert labels == ["Application logs", "Something else"]

    def test_fallback_payload_mentions_unstructured_text(self):
        pa = _make_failed_attachment(suggested_types=[], filename="x.dat")
        suggestions = _build_classification_clarification_suggestions([pa])

        assert len(suggestions) == 1
        fallback = suggestions[0]
        assert "unstructured text" in fallback.payload.lower()
        assert fallback.label == "Something else"

    def test_deduplicates_suggested_types(self):
        # Defensive: if suggested_types somehow contains duplicates, only
        # emit each type once.
        pa = _make_failed_attachment(
            suggested_types=["logs_and_errors", "logs_and_errors", "documentation"],
        )
        suggestions = _build_classification_clarification_suggestions([pa])

        labels = [s.label for s in suggestions]
        # logs appears once, docs once, fallback
        assert labels.count("Application logs") == 1
        assert len(suggestions) == 3
