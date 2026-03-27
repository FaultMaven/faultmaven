"""Knowledge Suggestion domain model.

Represents a suggested knowledge item extracted from a case that is pending
review before being promoted to the knowledge base.

Design Reference: Source Verification Badges Feature
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class SuggestionStatus(str, Enum):
    """Status of a knowledge suggestion in the review workflow.

    Lifecycle:
        PENDING_REVIEW -> APPROVED (creates KnowledgeItem)
        PENDING_REVIEW -> REJECTED (archived)
        PENDING_REVIEW -> DRAFT (needs more work)
    """

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    DRAFT = "draft"


class PIIScanStatus(str, Enum):
    """Status of PII scanning for the suggestion content.

    All suggestions must pass PII scanning before admin review.
    """

    NOT_SCANNED = "not_scanned"
    SCANNING = "scanning"
    CLEAN = "clean"  # No PII detected
    PII_DETECTED = "pii_detected"  # PII found, needs remediation
    REMEDIATED = "remediated"  # PII was found and removed
    SCAN_FAILED = "scan_failed"  # Scan couldn't complete


@dataclass
class SuggestionLineage:
    """Tracks the origin of a knowledge suggestion.

    Used for the "Extracted from Case #402 (prod-db-latency) • 2h ago" footer.
    """

    case_id: str
    case_title: str
    extracted_by: str  # User ID who triggered extraction
    extracted_at: datetime
    message_count: int = 0  # Number of messages used
    evidence_count: int = 0  # Number of evidence artifacts used


@dataclass
class KnowledgeSuggestion:
    """Knowledge suggestion extracted from a case, pending review.

    Represents knowledge that was extracted from an incident case
    and is awaiting admin review before being added to the knowledge base.

    Attributes:
        suggestion_id: Unique identifier for the suggestion
        organization_id: Organization that owns this suggestion
        case_id: Source case from which knowledge was extracted
        status: Current review status
        suggested_title: Proposed title for the knowledge item
        suggested_content: Proposed content (markdown)
        suggested_type: Proposed type (runbook, troubleshooting_guide, etc.)

        # Extraction metadata
        extracted_by: User ID who triggered extraction
        extracted_at: When extraction was performed
        include_messages: Whether case messages were included
        include_evidence: Whether evidence was included

        # PII scanning (HITL requirement)
        pii_scan_status: Status of PII scanning
        pii_scan_result: Details of PII scan (entities found, etc.)
        pii_remediated_by: User who remediated PII issues
        pii_remediated_at: When PII was remediated

        # Lineage (for Review Inbox footer)
        source_case_title: Title of the source case
        message_count: Number of messages extracted from
        evidence_count: Number of evidence artifacts extracted from

        # Review metadata
        reviewed_by: User ID who reviewed
        reviewed_at: When review was completed
        review_notes: Notes from reviewer
        rejection_reason: If rejected, why

        # Final document link (when approved)
        knowledge_item_id: ID of created KnowledgeItem (bidirectional link)

        # Timestamps
        created_at: When suggestion was created
        updated_at: When suggestion was last modified
        metadata: Additional metadata (JSON)
    """

    suggestion_id: str
    organization_id: str
    case_id: str
    status: SuggestionStatus = SuggestionStatus.PENDING_REVIEW

    # Suggested content
    suggested_title: str = ""
    suggested_content: str = ""
    suggested_type: str = "troubleshooting_guide"

    # Extraction metadata
    extracted_by: str = ""
    extracted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    include_messages: bool = True
    include_evidence: bool = True

    # PII scanning (HITL requirement per refinement #2)
    pii_scan_status: PIIScanStatus = PIIScanStatus.NOT_SCANNED
    pii_scan_result: dict[str, Any] | None = None
    pii_remediated_by: str | None = None
    pii_remediated_at: datetime | None = None

    # Lineage (for Review Inbox)
    source_case_title: str = ""
    message_count: int = 0
    evidence_count: int = 0

    # Review metadata
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None
    rejection_reason: str | None = None

    # Final document link (bidirectional lineage per refinement #4)
    knowledge_item_id: str | None = None

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate suggestion data."""
        if not self.suggestion_id:
            raise ValueError("suggestion_id is required")
        if not self.organization_id:
            raise ValueError("organization_id is required")
        if not self.case_id:
            raise ValueError("case_id is required")
        if not isinstance(self.status, SuggestionStatus):
            # Try to convert string to enum
            if isinstance(self.status, str):
                self.status = SuggestionStatus(self.status)
            else:
                raise ValueError(
                    f"status must be a SuggestionStatus, got {type(self.status)}"
                )
        if not isinstance(self.pii_scan_status, PIIScanStatus):
            if isinstance(self.pii_scan_status, str):
                self.pii_scan_status = PIIScanStatus(self.pii_scan_status)
            else:
                raise ValueError(
                    f"pii_scan_status must be a PIIScanStatus, got {type(self.pii_scan_status)}"
                )

    def get_lineage(self) -> SuggestionLineage:
        """Get lineage information for display in Review Inbox."""
        return SuggestionLineage(
            case_id=self.case_id,
            case_title=self.source_case_title,
            extracted_by=self.extracted_by,
            extracted_at=self.extracted_at,
            message_count=self.message_count,
            evidence_count=self.evidence_count,
        )

    def is_ready_for_review(self) -> bool:
        """Check if suggestion is ready for admin review.

        PII scan must be complete and clean/remediated before review.
        """
        return self.pii_scan_status in (PIIScanStatus.CLEAN, PIIScanStatus.REMEDIATED)

    def is_pending(self) -> bool:
        """Check if suggestion is pending review."""
        return self.status == SuggestionStatus.PENDING_REVIEW

    def is_approved(self) -> bool:
        """Check if suggestion was approved."""
        return self.status == SuggestionStatus.APPROVED

    def is_rejected(self) -> bool:
        """Check if suggestion was rejected."""
        return self.status == SuggestionStatus.REJECTED

    def approve(
        self,
        reviewed_by: str,
        knowledge_item_id: str,
        review_notes: str | None = None,
    ) -> None:
        """Approve the suggestion and link to created knowledge item.

        Args:
            reviewed_by: User ID of the reviewer
            knowledge_item_id: ID of the created KnowledgeItem
            review_notes: Optional notes from the reviewer
        """
        if not self.is_ready_for_review():
            raise ValueError("Suggestion must pass PII scan before approval")

        self.status = SuggestionStatus.APPROVED
        self.reviewed_by = reviewed_by
        self.reviewed_at = datetime.now(UTC)
        self.review_notes = review_notes
        self.knowledge_item_id = knowledge_item_id
        self.touch()

    def reject(
        self,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: str | None = None,
    ) -> None:
        """Reject the suggestion.

        Args:
            reviewed_by: User ID of the reviewer
            rejection_reason: Why the suggestion was rejected
            review_notes: Optional additional notes
        """
        self.status = SuggestionStatus.REJECTED
        self.reviewed_by = reviewed_by
        self.reviewed_at = datetime.now(UTC)
        self.rejection_reason = rejection_reason
        self.review_notes = review_notes
        self.touch()

    def mark_pii_scan_complete(
        self,
        status: PIIScanStatus,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Mark PII scan as complete.

        Args:
            status: Result of the PII scan
            result: Detailed scan results (entities found, etc.)
        """
        self.pii_scan_status = status
        self.pii_scan_result = result
        self.touch()

    def mark_pii_remediated(self, remediated_by: str) -> None:
        """Mark PII as remediated after manual review.

        Args:
            remediated_by: User ID who remediated the PII
        """
        if self.pii_scan_status != PIIScanStatus.PII_DETECTED:
            raise ValueError("Can only remediate if PII was detected")

        self.pii_scan_status = PIIScanStatus.REMEDIATED
        self.pii_remediated_by = remediated_by
        self.pii_remediated_at = datetime.now(UTC)
        self.touch()

    def update_content(self, title: str, content: str) -> None:
        """Update the suggested content (e.g., after admin edits).

        Args:
            title: New title
            content: New content
        """
        self.suggested_title = title
        self.suggested_content = content
        # Reset PII scan since content changed
        self.pii_scan_status = PIIScanStatus.NOT_SCANNED
        self.pii_scan_result = None
        self.touch()

    def touch(self) -> None:
        """Update the updated_at timestamp to current time."""
        self.updated_at = datetime.now(UTC)

    def get_content_preview(self, max_chars: int = 200) -> str:
        """Get a preview of the content for list views.

        Args:
            max_chars: Maximum characters to include

        Returns:
            Truncated content with ellipsis if needed
        """
        if len(self.suggested_content) <= max_chars:
            return self.suggested_content
        return self.suggested_content[:max_chars].rsplit(" ", 1)[0] + "..."

    def __repr__(self) -> str:
        return (
            f"KnowledgeSuggestion(suggestion_id={self.suggestion_id!r}, "
            f"case_id={self.case_id!r}, "
            f"status={self.status.value!r}, "
            f"pii_scan_status={self.pii_scan_status.value!r})"
        )
