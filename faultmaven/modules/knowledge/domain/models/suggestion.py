"""Knowledge Suggestion domain model.

Represents a suggested knowledge item extracted from a case that is pending
review before being promoted to the knowledge base.

Design Reference: Source Verification Badges Feature
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import ConflictError


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

        # Runbook quality gate (#1226)
        validation_passed: Whether the content passes RunbookValidator (None =
            not yet evaluated)
        validation_errors: Blocking errors from the last validation
        validation_warnings: Non-blocking warnings from the last validation

        # Timestamps
        created_at: When suggestion was created
        updated_at: When suggestion was last modified
        metadata: Additional metadata (JSON)
        version: Optimistic-concurrency token; owned by the repository
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
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    include_messages: bool = True
    include_evidence: bool = True

    # PII scanning (HITL requirement per refinement #2)
    pii_scan_status: PIIScanStatus = PIIScanStatus.NOT_SCANNED
    pii_scan_result: Optional[Dict[str, Any]] = None
    pii_remediated_by: Optional[str] = None
    pii_remediated_at: Optional[datetime] = None

    # Lineage (for Review Inbox)
    source_case_title: str = ""
    message_count: int = 0
    evidence_count: int = 0

    # Review metadata
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None
    rejection_reason: Optional[str] = None

    # Final document link (bidirectional lineage per refinement #4)
    knowledge_item_id: Optional[str] = None

    # Runbook quality gate (#1226).
    #
    # ``upload_document`` enforces ``RunbookValidator`` before its first side
    # effect (#1214), so approving content the gate refuses answers 422 and
    # publishes nothing. A reviewer who only sees "422" cannot act; these carry
    # the SAME structured verdict forward from extraction time, and are
    # recomputed on every content edit, so the review inbox can show what is
    # wrong and the reviewer can watch it clear as they edit.
    #
    # ``None`` means NOT YET EVALUATED — distinct from ``False`` (evaluated and
    # refused). An older suggestion, or one whose content was just replaced and
    # not yet re-checked, is unknown rather than passing.
    validation_passed: Optional[bool] = None
    validation_errors: List[str] = field(default_factory=list)
    validation_warnings: List[str] = field(default_factory=list)

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Optional[Dict[str, Any]] = None

    # Optimistic-concurrency token (#1227). Carried on the domain object because
    # the store hands out DETACHED COPIES: the value a caller loaded is what its
    # write must be checked against, so it has to travel with the object rather
    # than live in the repository. Set by the repository on load and bumped by
    # it on save; nothing in the domain should assign it. A never-persisted
    # suggestion starts at 1, matching the column's server default.
    version: int = 1

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
        review_notes: Optional[str] = None,
    ) -> None:
        """Approve the suggestion and link to created knowledge item.

        Args:
            reviewed_by: User ID of the reviewer
            knowledge_item_id: ID of the created KnowledgeItem
            review_notes: Optional notes from the reviewer

        Raises:
            ConflictError: PII scan has not completed (or failed) so the
                suggestion is not eligible for approval. The global
                handler maps this to HTTP 409 with
                ``conflict_reason="not_ready_for_review"``.
            ConflictError: the suggestion is ALREADY approved. The documented
                lifecycle is ``PENDING_REVIEW -> APPROVED``; approving twice
                is not a re-run, it publishes a SECOND knowledge item and
                overwrites ``knowledge_item_id``, orphaning the first with no
                back-link. ``conflict_reason="already_approved"``.
        """
        if not self.is_ready_for_review():
            raise ConflictError(
                "Suggestion must pass PII scan before approval",
                resource_type="suggestion",
                resource_id=self.suggestion_id,
                conflict_reason="not_ready_for_review",
            )
        if self.is_approved():
            raise ConflictError(
                "Suggestion has already been approved",
                resource_type="suggestion",
                resource_id=self.suggestion_id,
                conflict_reason="already_approved",
            )

        self.status = SuggestionStatus.APPROVED
        self.reviewed_by = reviewed_by
        self.reviewed_at = datetime.now(timezone.utc)
        self.review_notes = review_notes
        self.knowledge_item_id = knowledge_item_id
        self.touch()

    def reject(
        self,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: Optional[str] = None,
    ) -> None:
        """Reject the suggestion.

        Args:
            reviewed_by: User ID of the reviewer
            rejection_reason: Why the suggestion was rejected
            review_notes: Optional additional notes

        Raises:
            ConflictError: the suggestion is already APPROVED. The documented
                lifecycle forks — ``PENDING_REVIEW`` goes to APPROVED *or*
                REJECTED — and approval has already published a knowledge item.
                Rejecting afterwards flipped the status to REJECTED while
                leaving ``knowledge_item_id`` set and the item live in the
                corpus, so the inbox said "rejected" about content every tenant
                could still retrieve. ``conflict_reason="already_approved"``.
        """
        if self.is_approved():
            raise ConflictError(
                "Suggestion has already been approved and cannot be rejected",
                resource_type="suggestion",
                resource_id=self.suggestion_id,
                conflict_reason="already_approved",
            )

        self.status = SuggestionStatus.REJECTED
        self.reviewed_by = reviewed_by
        self.reviewed_at = datetime.now(timezone.utc)
        self.rejection_reason = rejection_reason
        self.review_notes = review_notes
        self.touch()

    def mark_pii_scan_complete(
        self,
        status: PIIScanStatus,
        result: Optional[Dict[str, Any]] = None,
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

        Raises:
            ConflictError: PII scan did not detect PII, so there is
                nothing to remediate. The global handler maps this to
                HTTP 409 with ``conflict_reason="no_pii_detected"``.
        """
        if self.pii_scan_status != PIIScanStatus.PII_DETECTED:
            raise ConflictError(
                "Can only remediate if PII was detected",
                resource_type="suggestion",
                resource_id=self.suggestion_id,
                conflict_reason="no_pii_detected",
            )

        self.pii_scan_status = PIIScanStatus.REMEDIATED
        self.pii_remediated_by = remediated_by
        self.pii_remediated_at = datetime.now(timezone.utc)
        self.touch()

    def update_content(self, title: str, content: str) -> None:
        """Update the suggested content (e.g., after admin edits).

        Args:
            title: New title
            content: New content

        Raises:
            ConflictError: the suggestion is already APPROVED. The edit resets
                ``pii_scan_status`` to NOT_SCANNED, which made an approved
                suggestion simultaneously "approved" and not ready for review,
                still linked to a published knowledge item whose content no
                longer matched what the inbox showed. An approved suggestion is
                finished; edit the published runbook instead.
                ``conflict_reason="already_approved"``.
        """
        if self.is_approved():
            raise ConflictError(
                "Suggestion has already been approved and cannot be edited; "
                "edit the published knowledge item instead",
                resource_type="suggestion",
                resource_id=self.suggestion_id,
                conflict_reason="already_approved",
            )

        self.suggested_title = title
        self.suggested_content = content
        # Reset PII scan since content changed
        self.pii_scan_status = PIIScanStatus.NOT_SCANNED
        self.pii_scan_result = None
        # The stored verdict was about the PREVIOUS content, so it is now a
        # claim about text that no longer exists (#1226). Cleared to "not yet
        # evaluated" rather than left standing; ``SuggestionService`` re-runs the
        # gate immediately after this call and sets the fresh one.
        self.validation_passed = None
        self.validation_errors = []
        self.validation_warnings = []
        self.touch()

    def set_validation(
        self,
        passed: bool,
        errors: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
    ) -> None:
        """Record the runbook quality gate's verdict on the current content.

        The same ``RunbookValidator`` verdict ``upload_document`` will reach on
        approval (#1214), captured at extraction time and after every edit so a
        reviewer sees WHAT blocks approval instead of only a 422 (#1226).

        Deliberately does NOT ``touch()``: validating is an observation of the
        content, not a modification of the suggestion, so a read-only re-check
        should not make the row look freshly decided. (The original reason was
        that ``updated_at`` ordered an eviction pool; #1227 removed the
        eviction, but ``updated_at`` still means "when this was last changed"
        and a verdict does not change it.)

        Args:
            passed: Whether the content clears the gate
            errors: Blocking errors (empty when ``passed``)
            warnings: Non-blocking warnings
        """
        self.validation_passed = bool(passed)
        self.validation_errors = list(errors or [])
        self.validation_warnings = list(warnings or [])

    def touch(self) -> None:
        """Update the updated_at timestamp to current time."""
        self.updated_at = datetime.now(timezone.utc)

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
