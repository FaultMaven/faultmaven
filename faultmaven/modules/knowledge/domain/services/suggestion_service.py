"""Knowledge Suggestion Service.

Handles extraction of knowledge from cases into suggestions,
PII scanning, and the review workflow (approve/reject).

Design Reference: Source Verification Badges Feature
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)
from faultmaven.utils.serialization import to_json_compatible


class SuggestionService:
    """Service for managing knowledge suggestions.

    Handles:
    - Extracting knowledge from case conversations
    - PII scanning before review (HITL requirement)
    - CRUD operations for suggestions
    - Approval workflow with bidirectional linking
    """

    # LLM prompt for knowledge extraction
    EXTRACTION_PROMPT = """Analyze this incident case and extract reusable knowledge.

Case Title: {case_title}
Description: {case_description}

{messages_section}

{evidence_section}

Generate a knowledge article that:
1. Describes the problem pattern (symptoms, scope, conditions)
2. Explains the root cause
3. Provides step-by-step resolution
4. Includes prevention recommendations
5. **CRITICAL**: Remove ALL incident-specific details:
   - Specific timestamps (use relative time like "after X hours")
   - Specific user names or email addresses
   - Specific hostnames, IP addresses, or internal URLs
   - Specific customer or organization names
   - Any other personally identifiable information

Format as Markdown with these sections:
## Problem
## Root Cause
## Solution
## Prevention
"""

    def __init__(
        self,
        case_repository: Any | None = None,
        knowledge_service: Any | None = None,
        sanitizer: Any | None = None,
        llm_provider: Any | None = None,
    ):
        """Initialize the suggestion service.

        Args:
            case_repository: Repository for case access
            knowledge_service: Service for creating knowledge items
            sanitizer: ISanitizer for PII detection/redaction
            llm_provider: LLM provider for extraction
        """
        self.logger = logging.getLogger(__name__)
        self._case_repository = case_repository
        self._knowledge_service = knowledge_service
        self._sanitizer = sanitizer
        self._llm_provider = llm_provider

        # In-memory store for suggestions (production would use DB)
        self._suggestions_store: dict[str, KnowledgeSuggestion] = {}

    async def extract_knowledge_from_case(
        self,
        case_id: str,
        organization_id: str,
        extracted_by: str,
        include_messages: bool = True,
        include_evidence: bool = True,
        title_suggestion: str | None = None,
    ) -> KnowledgeSuggestion:
        """Extract knowledge from a case into a suggestion.

        Args:
            case_id: Case to extract from
            organization_id: Organization context
            extracted_by: User ID triggering extraction
            include_messages: Include case conversation
            include_evidence: Include evidence summaries
            title_suggestion: Optional title for the suggestion

        Returns:
            Created KnowledgeSuggestion
        """
        self.logger.info(f"Extracting knowledge from case {case_id}")

        # Get case details
        case_title = "Unknown Case"
        case_description = ""
        messages = []
        evidence = []

        if self._case_repository:
            try:
                case = await self._case_repository.get_by_id(case_id)
                if case:
                    case_title = getattr(case, "title", case_id)
                    case_description = getattr(case, "description", "")

                    if include_messages:
                        case_messages = await self._case_repository.get_messages(
                            case_id
                        )
                        messages = case_messages or []

                    if include_evidence:
                        case_evidence = await self._case_repository.get_evidence(
                            case_id
                        )
                        evidence = case_evidence or []
            except Exception as e:
                self.logger.warning(f"Failed to fetch case details: {e}")

        # Build extraction prompt
        messages_section = ""
        if include_messages and messages:
            formatted_messages = []
            for msg in messages[:50]:  # Limit to last 50 messages
                role = getattr(msg, "role", "unknown")
                content = getattr(msg, "content", str(msg))
                formatted_messages.append(f"[{role}]: {content}")
            messages_section = "Messages:\n" + "\n".join(formatted_messages)

        evidence_section = ""
        if include_evidence and evidence:
            evidence_summaries = []
            for ev in evidence[:20]:  # Limit to 20 pieces
                ev_type = getattr(ev, "artifact_type", "unknown")
                ev_name = getattr(ev, "name", "")
                ev_summary = getattr(ev, "summary", "")
                evidence_summaries.append(f"- [{ev_type}] {ev_name}: {ev_summary}")
            evidence_section = "Evidence Summary:\n" + "\n".join(evidence_summaries)

        prompt = self.EXTRACTION_PROMPT.format(
            case_title=case_title,
            case_description=case_description,
            messages_section=messages_section or "No messages included.",
            evidence_section=evidence_section or "No evidence included.",
        )

        # Generate knowledge content using LLM
        suggested_content = await self._generate_knowledge_content(prompt)

        # Generate title if not provided
        suggested_title = title_suggestion or await self._generate_title(
            case_title, suggested_content
        )

        # Create suggestion
        suggestion_id = f"sug_{uuid.uuid4().hex[:12]}"
        suggestion = KnowledgeSuggestion(
            suggestion_id=suggestion_id,
            organization_id=organization_id,
            case_id=case_id,
            status=SuggestionStatus.PENDING_REVIEW,
            suggested_title=suggested_title,
            suggested_content=suggested_content,
            suggested_type="troubleshooting_guide",
            extracted_by=extracted_by,
            extracted_at=datetime.now(UTC),
            include_messages=include_messages,
            include_evidence=include_evidence,
            pii_scan_status=PIIScanStatus.NOT_SCANNED,
            source_case_title=case_title,
            message_count=len(messages),
            evidence_count=len(evidence),
        )

        # Trigger PII scan
        await self._scan_for_pii(suggestion)

        # Store suggestion
        self._suggestions_store[suggestion_id] = suggestion
        self.logger.info(f"Created suggestion {suggestion_id} from case {case_id}")

        return suggestion

    async def _generate_knowledge_content(self, prompt: str) -> str:
        """Generate knowledge content using LLM.

        Args:
            prompt: Extraction prompt with case context

        Returns:
            Generated markdown content
        """
        if self._llm_provider:
            try:
                response = await self._llm_provider.generate(
                    prompt=prompt,
                    max_tokens=2000,
                    temperature=0.3,
                )
                return (
                    response.get("content", "")
                    if isinstance(response, dict)
                    else str(response)
                )
            except Exception as e:
                self.logger.warning(f"LLM generation failed: {e}")

        # Fallback template
        return """## Problem
[Describe the problem pattern observed in this incident]

## Root Cause
[Explain the underlying cause]

## Solution
1. [Step 1]
2. [Step 2]
3. [Step 3]

## Prevention
- [Recommendation 1]
- [Recommendation 2]
"""

    async def _generate_title(self, case_title: str, content: str) -> str:
        """Generate a knowledge article title.

        Args:
            case_title: Original case title
            content: Generated content

        Returns:
            Suggested title
        """
        # Simple title extraction - in production, use LLM
        if case_title and case_title != "Unknown Case":
            # Clean up the case title for reuse
            title = case_title
            # Remove incident-specific prefixes
            prefixes_to_remove = [
                "Incident:",
                "Alert:",
                "Issue:",
                "[P1]",
                "[P2]",
                "[P3]",
                "[SEV1]",
                "[SEV2]",
            ]
            for prefix in prefixes_to_remove:
                if title.startswith(prefix):
                    title = title[len(prefix) :].strip()
            return f"Troubleshooting: {title}"

        return "Troubleshooting Guide"

    async def _scan_for_pii(self, suggestion: KnowledgeSuggestion) -> None:
        """Scan suggestion content for PII.

        Uses ISanitizer to detect PII entities. If PII is found,
        marks the suggestion for manual remediation (HITL).

        Args:
            suggestion: Suggestion to scan
        """
        suggestion.pii_scan_status = PIIScanStatus.SCANNING

        if self._sanitizer:
            try:
                # Scan content for PII
                content_to_scan = (
                    f"{suggestion.suggested_title}\n\n{suggestion.suggested_content}"
                )
                sanitized = self._sanitizer.sanitize(content_to_scan)

                # If content was modified, PII was found
                if sanitized != content_to_scan:
                    suggestion.mark_pii_scan_complete(
                        status=PIIScanStatus.PII_DETECTED,
                        result={
                            "original_length": len(content_to_scan),
                            "sanitized_length": len(sanitized),
                            "pii_removed": True,
                            "message": "PII detected. Manual review required before approval.",
                        },
                    )
                    # Store sanitized version as suggestion
                    suggestion.suggested_content = sanitized
                else:
                    suggestion.mark_pii_scan_complete(
                        status=PIIScanStatus.CLEAN,
                        result={"pii_detected": False},
                    )
            except Exception as e:
                self.logger.error(f"PII scan failed: {e}")
                suggestion.mark_pii_scan_complete(
                    status=PIIScanStatus.SCAN_FAILED,
                    result={"error": str(e)},
                )
        else:
            # No sanitizer available, mark as clean (development mode)
            suggestion.mark_pii_scan_complete(
                status=PIIScanStatus.CLEAN,
                result={"pii_detected": False, "note": "No sanitizer configured"},
            )

    async def get_suggestion(self, suggestion_id: str) -> KnowledgeSuggestion | None:
        """Get a suggestion by ID.

        Args:
            suggestion_id: Suggestion identifier

        Returns:
            KnowledgeSuggestion or None
        """
        return self._suggestions_store.get(suggestion_id)

    async def list_suggestions(
        self,
        organization_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List suggestions with optional filtering.

        Args:
            organization_id: Filter by organization
            status: Filter by status
            limit: Max items to return
            offset: Pagination offset

        Returns:
            Dict with suggestions list and pagination info
        """
        suggestions = list(self._suggestions_store.values())

        # Apply filters
        if organization_id:
            suggestions = [
                s for s in suggestions if s.organization_id == organization_id
            ]

        if status:
            suggestions = [s for s in suggestions if s.status.value == status]

        # Sort by created_at descending
        suggestions.sort(key=lambda s: s.created_at, reverse=True)

        total_count = len(suggestions)

        # Apply pagination
        suggestions = suggestions[offset : offset + limit]

        return {
            "suggestions": suggestions,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    async def approve_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        review_notes: str | None = None,
    ) -> dict[str, Any] | None:
        """Approve a suggestion and create a knowledge item.

        Args:
            suggestion_id: Suggestion to approve
            reviewed_by: User ID of reviewer
            review_notes: Optional notes

        Returns:
            Dict with new knowledge_item_id or None if failed
        """
        suggestion = await self.get_suggestion(suggestion_id)
        if not suggestion:
            self.logger.warning(f"Suggestion {suggestion_id} not found")
            return None

        if not suggestion.is_ready_for_review():
            self.logger.warning(
                f"Suggestion {suggestion_id} not ready for review "
                f"(pii_scan_status={suggestion.pii_scan_status})"
            )
            return None

        # Create knowledge item
        knowledge_item_id = None
        if self._knowledge_service:
            try:
                result = await self._knowledge_service.upload_document(
                    content=suggestion.suggested_content,
                    title=suggestion.suggested_title,
                    document_type=suggestion.suggested_type,
                    category="extracted",
                    tags=["extracted", "case-derived"],
                    source_url=None,
                    description=f"Extracted from case {suggestion.case_id}",
                    metadata={
                        "source_suggestion_id": suggestion_id,
                        "source_case_id": suggestion.case_id,
                        "extracted_by": suggestion.extracted_by,
                        "verification_level": 2,  # Admin verified
                    },
                )
                knowledge_item_id = result.get("document_id")
            except Exception as e:
                self.logger.error(f"Failed to create knowledge item: {e}")
                return None
        else:
            # Mock ID for testing
            knowledge_item_id = f"kb_{uuid.uuid4().hex[:12]}"

        # Mark suggestion as approved
        suggestion.approve(
            reviewed_by=reviewed_by,
            knowledge_item_id=knowledge_item_id,
            review_notes=review_notes,
        )

        self.logger.info(
            f"Approved suggestion {suggestion_id}, created knowledge item {knowledge_item_id}"
        )

        return {
            "suggestion_id": suggestion_id,
            "knowledge_item_id": knowledge_item_id,
            "status": "approved",
        }

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: str | None = None,
    ) -> bool:
        """Reject a suggestion.

        Args:
            suggestion_id: Suggestion to reject
            reviewed_by: User ID of reviewer
            rejection_reason: Why rejected
            review_notes: Optional additional notes

        Returns:
            True if rejected, False if not found
        """
        suggestion = await self.get_suggestion(suggestion_id)
        if not suggestion:
            return False

        suggestion.reject(
            reviewed_by=reviewed_by,
            rejection_reason=rejection_reason,
            review_notes=review_notes,
        )

        self.logger.info(f"Rejected suggestion {suggestion_id}: {rejection_reason}")
        return True

    async def update_suggestion(
        self,
        suggestion_id: str,
        title: str | None = None,
        content: str | None = None,
        suggested_type: str | None = None,
    ) -> KnowledgeSuggestion | None:
        """Update a suggestion's content.

        Args:
            suggestion_id: Suggestion to update
            title: New title
            content: New content
            suggested_type: New type

        Returns:
            Updated suggestion or None if not found
        """
        suggestion = await self.get_suggestion(suggestion_id)
        if not suggestion:
            return None

        if title or content:
            suggestion.update_content(
                title=title or suggestion.suggested_title,
                content=content or suggestion.suggested_content,
            )
            # Re-scan for PII since content changed
            await self._scan_for_pii(suggestion)

        if suggested_type:
            suggestion.suggested_type = suggested_type
            suggestion.touch()

        self.logger.info(f"Updated suggestion {suggestion_id}")
        return suggestion

    async def remediate_pii(
        self,
        suggestion_id: str,
        remediated_by: str,
    ) -> KnowledgeSuggestion | None:
        """Mark PII as remediated after manual review.

        Args:
            suggestion_id: Suggestion that was remediated
            remediated_by: User ID who remediated

        Returns:
            Updated suggestion or None if not found
        """
        suggestion = await self.get_suggestion(suggestion_id)
        if not suggestion:
            return None

        suggestion.mark_pii_remediated(remediated_by)
        self.logger.info(f"PII remediated for suggestion {suggestion_id}")
        return suggestion

    def to_api_response(
        self, suggestion: KnowledgeSuggestion, include_content: bool = False
    ) -> dict[str, Any]:
        """Convert suggestion to API response format.

        Args:
            suggestion: Suggestion to convert
            include_content: Include full content (for detail view)

        Returns:
            Dict suitable for API response
        """
        lineage = {
            "case_id": suggestion.case_id,
            "case_title": suggestion.source_case_title,
            "extracted_by": suggestion.extracted_by,
            "extracted_at": to_json_compatible(suggestion.extracted_at),
        }

        if include_content:
            return {
                "suggestion_id": suggestion.suggestion_id,
                "organization_id": suggestion.organization_id,
                "case_id": suggestion.case_id,
                "status": suggestion.status.value,
                "suggested_title": suggestion.suggested_title,
                "suggested_content": suggestion.suggested_content,
                "suggested_type": suggestion.suggested_type,
                "extracted_by": suggestion.extracted_by,
                "extracted_at": to_json_compatible(suggestion.extracted_at),
                "include_messages": suggestion.include_messages,
                "include_evidence": suggestion.include_evidence,
                "pii_scan_status": suggestion.pii_scan_status.value,
                "pii_scan_result": suggestion.pii_scan_result,
                "pii_remediated_by": suggestion.pii_remediated_by,
                "pii_remediated_at": (
                    to_json_compatible(suggestion.pii_remediated_at)
                    if suggestion.pii_remediated_at
                    else None
                ),
                "lineage": lineage,
                "reviewed_by": suggestion.reviewed_by,
                "reviewed_at": (
                    to_json_compatible(suggestion.reviewed_at)
                    if suggestion.reviewed_at
                    else None
                ),
                "review_notes": suggestion.review_notes,
                "rejection_reason": suggestion.rejection_reason,
                "knowledge_item_id": suggestion.knowledge_item_id,
                "created_at": to_json_compatible(suggestion.created_at),
                "updated_at": to_json_compatible(suggestion.updated_at),
                "metadata": suggestion.metadata,
            }
        else:
            # Summary view
            return {
                "suggestion_id": suggestion.suggestion_id,
                "title": suggestion.suggested_title,
                "content_preview": suggestion.get_content_preview(200),
                "status": suggestion.status.value,
                "verification_status": "experimental",  # Always experimental until approved
                "pii_scan_status": suggestion.pii_scan_status.value,
                "suggested_type": suggestion.suggested_type,
                "created_at": to_json_compatible(suggestion.created_at),
                "lineage": lineage,
            }
