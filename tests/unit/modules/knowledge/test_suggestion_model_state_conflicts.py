"""Typed-exception contract tests for KnowledgeSuggestion state transitions.

``Suggestion.approve()`` and ``Suggestion.mark_pii_remediated()`` are
state-transition methods that fail when the suggestion isn't in the
right shape (PII scan not run, no PII detected, etc.). They used to
raise raw ``ValueError`` which the API routes caught and returned as
HTTP 400 — collapsing semantically distinct state-conflict failures
into the same bucket as malformed-input validation.

After the refactor both raise ``ConflictError`` with structured
``resource_type`` / ``resource_id`` / ``conflict_reason`` fields. The
global exception handler in ``api/exception_handlers.py`` maps that to
HTTP 409 with the structured metadata surfaced in the response body so
clients can branch on ``conflict_reason`` instead of regex-matching the
human-readable detail string.

These tests pin both the exception type and the carried metadata.
"""

from __future__ import annotations

import pytest

from faultmaven.exceptions import ConflictError
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)


def _make_suggestion(
    *,
    suggestion_id: str = "sug_abc123",
    pii_scan_status: PIIScanStatus = PIIScanStatus.NOT_SCANNED,
    status: SuggestionStatus = SuggestionStatus.PENDING_REVIEW,
) -> KnowledgeSuggestion:
    return KnowledgeSuggestion(
        suggestion_id=suggestion_id,
        organization_id="org_001",
        case_id="case_001",
        status=status,
        suggested_title="Test",
        suggested_content="Body",
        suggested_type="runbook",
        extracted_by="user_001",
        pii_scan_status=pii_scan_status,
    )


class TestApprove:
    def test_approve_requires_pii_scan_clean_or_remediated(self):
        """Approving while PII scan has not run raises ConflictError
        with ``conflict_reason="not_ready_for_review"`` → HTTP 409.
        """
        suggestion = _make_suggestion(pii_scan_status=PIIScanStatus.NOT_SCANNED)

        with pytest.raises(ConflictError) as exc:
            suggestion.approve(
                reviewed_by="admin_001",
                knowledge_item_id="ki_001",
            )

        assert exc.value.resource_type == "suggestion"
        assert exc.value.resource_id == "sug_abc123"
        assert exc.value.conflict_reason == "not_ready_for_review"

    def test_approve_rejected_when_pii_detected_and_not_remediated(self):
        """PII detected but not yet remediated also blocks approval."""
        suggestion = _make_suggestion(pii_scan_status=PIIScanStatus.PII_DETECTED)

        with pytest.raises(ConflictError) as exc:
            suggestion.approve(
                reviewed_by="admin_001",
                knowledge_item_id="ki_001",
            )

        assert exc.value.conflict_reason == "not_ready_for_review"

    def test_approve_succeeds_when_clean(self):
        """Clean scan: state transitions normally, no exception."""
        suggestion = _make_suggestion(pii_scan_status=PIIScanStatus.CLEAN)

        suggestion.approve(
            reviewed_by="admin_001",
            knowledge_item_id="ki_001",
        )

        assert suggestion.status == SuggestionStatus.APPROVED
        assert suggestion.knowledge_item_id == "ki_001"


class TestMarkPiiRemediated:
    def test_remediate_requires_pii_detected(self):
        """Remediating when no PII was detected raises ConflictError
        with ``conflict_reason="no_pii_detected"`` → HTTP 409. There is
        nothing to remediate so the operation is a logical conflict,
        not a validation error.
        """
        suggestion = _make_suggestion(pii_scan_status=PIIScanStatus.CLEAN)

        with pytest.raises(ConflictError) as exc:
            suggestion.mark_pii_remediated(remediated_by="admin_001")

        assert exc.value.resource_type == "suggestion"
        assert exc.value.resource_id == "sug_abc123"
        assert exc.value.conflict_reason == "no_pii_detected"

    def test_remediate_succeeds_when_pii_detected(self):
        suggestion = _make_suggestion(pii_scan_status=PIIScanStatus.PII_DETECTED)

        suggestion.mark_pii_remediated(remediated_by="admin_001")

        assert suggestion.pii_scan_status == PIIScanStatus.REMEDIATED
        assert suggestion.pii_remediated_by == "admin_001"
