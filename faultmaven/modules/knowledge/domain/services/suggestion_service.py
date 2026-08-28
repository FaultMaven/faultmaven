"""Knowledge Suggestion Service.

Handles extraction of knowledge from cases into suggestions,
PII scanning, and the review workflow (approve/reject).

Design Reference: Source Verification Badges Feature
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.exceptions import ConflictError
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
        case_repository: Optional[Any] = None,
        knowledge_service: Optional[Any] = None,
        sanitizer: Optional[Any] = None,
        llm_provider: Optional[Any] = None,
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
        self._suggestions_store: Dict[str, KnowledgeSuggestion] = {}

    async def extract_knowledge_from_case(
        self,
        case_id: str,
        organization_id: str,
        extracted_by: str,
        include_messages: bool = True,
        include_evidence: bool = True,
        title_suggestion: Optional[str] = None,
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
            extracted_at=datetime.now(timezone.utc),
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
                # ``generate`` returns an LLMResponse. The old code read it as a
                # dict and otherwise fell back to ``str(response)``, which would
                # have written the dataclass REPR into a knowledge suggestion —
                # inert today only because both construction sites pass no
                # provider, so this branch never runs. Corrected rather than
                # left as a trap for whoever wires the provider up, and a
                # truncated draft falls through to the template below rather
                # than being persisted half-written (#1094).
                if response is None:
                    # Not truncation — the contract says ``generate`` returns an
                    # LLMResponse, so this means a provider broke it. Reported
                    # as itself rather than folded into the truncation warning
                    # below, which would send a reader looking for an output cap
                    # that was never involved.
                    self.logger.warning(
                        "Suggestion generation returned no response; "
                        "falling back to the template"
                    )
                elif response.is_truncated:
                    self.logger.warning(
                        "Suggestion generation truncated at the output cap; "
                        "falling back to the template"
                    )
                else:
                    content = (response.content or "").strip()
                    if content:
                        return content
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
                sanitized = await self._sanitizer.asanitize(content_to_scan)

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

    async def get_suggestion(self, suggestion_id: str) -> Optional[KnowledgeSuggestion]:
        """Get a suggestion by ID — UNSCOPED.

        This is the trusted internal load: it applies no requester scope, so
        extraction and other in-process flows can read a row they just wrote.
        Anything acting on behalf of an actor must use
        :meth:`get_suggestion_visible` instead, which carries the mandatory
        tenant predicate.

        Args:
            suggestion_id: Suggestion identifier

        Returns:
            KnowledgeSuggestion or None
        """
        return self._suggestions_store.get(suggestion_id)

    async def get_suggestion_visible(
        self, suggestion_id: str, *, organization_id: str
    ) -> Optional[KnowledgeSuggestion]:
        """Get a suggestion by ID, scoped to the actor's tenant.

        The actor-facing counterpart of :meth:`get_suggestion` (the split #871
        introduced for documents). Returns None both for an absent id and for
        one belonging to another organization, so the two are indistinguishable
        to the caller and a route built on it answers 404 rather than acting as
        an existence oracle. Fail-closed: no organization, no result — never a
        deployment-wide lookup.

        Rejected alternative: scoping :meth:`get_suggestion` itself — it is the
        trusted load behind extraction, which has no actor to scope by.

        Args:
            suggestion_id: Suggestion identifier
            organization_id: Actor's tenant; REQUIRED

        Returns:
            KnowledgeSuggestion owned by ``organization_id``, or None
        """
        if not suggestion_id or not organization_id:
            return None
        suggestion = self._suggestions_store.get(suggestion_id)
        if suggestion is None or suggestion.organization_id != organization_id:
            return None
        return suggestion

    async def list_suggestions(
        self,
        organization_id: str,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List suggestions belonging to one organization.

        ``organization_id`` is REQUIRED and always applied: an unscoped listing
        would return every tenant's suggestions to a platform admin bound to
        one. Fail-closed — a falsy organization lists nothing.

        Args:
            organization_id: Actor's tenant; REQUIRED
            status: Filter by status
            limit: Max items to return
            offset: Pagination offset

        Returns:
            Dict with suggestions list and pagination info
        """
        if not organization_id:
            return {
                "suggestions": [],
                "total_count": 0,
                "limit": limit,
                "offset": offset,
            }

        suggestions = [
            s
            for s in self._suggestions_store.values()
            if s.organization_id == organization_id
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
        review_notes: Optional[str] = None,
        *,
        organization_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Approve a suggestion and create a knowledge item.

        Args:
            suggestion_id: Suggestion to approve
            reviewed_by: User ID of reviewer
            review_notes: Optional notes
            organization_id: Actor's tenant; REQUIRED — an out-of-tenant id
                resolves to None, exactly like an absent one

        Returns:
            Dict with the new ``knowledge_item_id``, or ``None`` when the
            suggestion is absent, out of tenant, or not ready for review.
            ``None`` means ONLY that — it is no longer the catch-all it was.

        Raises:
            ConflictError: the suggestion is already approved
                (``conflict_reason="already_approved"``) — checked before
                anything is published, so a repeat writes nothing. The global
                handler maps this to HTTP 409.
            RunbookQualityError: the suggested content does not meet the
                runbook quality standard (#1214). Raised by ``upload_document``
                BEFORE it writes anything, so the refusal publishes nothing;
                the global handler answers 422. LLM-extracted markdown reaches
                the corpus only after a reviewer edits it into a valid runbook.
            RuntimeError: no knowledge service is wired, so there is nothing to
                publish INTO (#1214). This used to mint a fake id and report
                ``201 {"status": "approved"}`` for an item that was never
                created — and, with ``app.state.suggestion_service`` unset, it
                was the branch every production request took.
            Exception: anything ``upload_document`` raises now PROPAGATES
                rather than being swallowed into ``None`` (#1200). A
                ``TypeError`` from that call is a programming error and an
                ingestion failure is a server-side fault; neither is a client
                error and neither is a statement about PII. The route's own
                handler logs and answers 500.
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
        if not suggestion:
            self.logger.warning(f"Suggestion {suggestion_id} not found")
            return None

        if not suggestion.is_ready_for_review():
            self.logger.warning(
                f"Suggestion {suggestion_id} not ready for review "
                f"(pii_scan_status={suggestion.pii_scan_status})"
            )
            return None

        # Refuse a SECOND approval BEFORE anything is published.
        #
        # ``is_ready_for_review`` inspects ``pii_scan_status`` only, never
        # ``status``, so nothing here used to stop a repeat. That was harmless
        # only because the call below always raised — every approval created
        # nothing. Now that it succeeds, a repeat publishes ANOTHER item into
        # the global corpus and overwrites ``knowledge_item_id``, orphaning the
        # previous one with no back-link. Measured before this guard: three
        # calls gave three knowledge items, three files and three ChromaDB
        # chunk sets, with only the last one linked.
        #
        # ``approve()`` carries the same check as a defence, but it runs AFTER
        # the publish, so the guard has to be here to prevent the write.
        if suggestion.is_approved():
            raise ConflictError(
                "Suggestion has already been approved",
                resource_type="suggestion",
                resource_id=suggestion_id,
                conflict_reason="already_approved",
            )

        # Create knowledge item.
        #
        # No knowledge service means no corpus to publish into, and the only
        # honest answer is a failure (#1214). The old ``else`` minted an id from
        # ``authored_item_id()`` and returned ``{"status": "approved"}`` for a
        # knowledge item that had never been created — the same class of claim
        # #1200 exists to remove, standing inside the function that fixes it.
        # And because ``app.state.suggestion_service`` was written NOWHERE, the
        # route always built a collaborator-less service, so that branch was the
        # one 100% of production approvals took.
        if not self._knowledge_service:
            raise RuntimeError(
                "Cannot approve suggestion "
                f"{suggestion_id}: no knowledge service is configured, so no "
                "knowledge item can be created. Approval reports success only "
                "when something was actually published."
            )

        # NO try/except around this call (#1200).
        #
        # It used to pass ``metadata={...}`` — a parameter
        # ``upload_document`` has never had. The resulting ``TypeError``
        # was caught by a broad ``except Exception`` here, logged, and
        # turned into ``return None``, which the approve route renders as
        # ``400 "Cannot approve: PII scan not complete"``. That claim is
        # false by construction: the scan had to be CLEAN or REMEDIATED to
        # get past ``is_ready_for_review`` above. So the approval step of
        # the knowledge flywheel created nothing and misreported why, and
        # the failure was shaped exactly like "nothing to approve".
        #
        # A ``TypeError`` from a call this service makes to its own
        # collaborator is a programming error, and a failed ingestion is a
        # server-side fault. Neither is a client error and neither is a
        # statement about PII. Both now propagate: the route's own
        # ``except Exception`` logs them and answers 500. ``return None``
        # is left to mean one thing only — the suggestion is not ready —
        # which is the one case that 400 is actually about.
        #
        # ``upload_document`` also enforces the runbook quality gate (#1214)
        # before its first side effect, so content that fails it raises
        # ``RunbookQualityError`` here having published NOTHING.
        result = await self._knowledge_service.upload_document(
            content=suggestion.suggested_content,
            title=suggestion.suggested_title,
            document_type=suggestion.suggested_type,
            # The platform tier, stated rather than inherited from a
            # default (#1166). Gated at the approve route by
            # require_global_authoring_allowed(); an approved
            # suggestion becomes platform-shipped knowledge, which is
            # why that gate is there and why this says "global" out
            # loud instead of taking whatever the service assumed.
            scope="global",
            category="extracted",
            tags=["extracted", "case-derived"],
            source_url=None,
            # ATTRIBUTION, on the one parameter that actually persists.
            #
            # ``owner_id`` reaches four real columns —
            # ``uploaded_files.uploaded_by``, ``conversion_jobs.user_id``,
            # ``conversion_drafts.verified_by``, and ``ingest_runbook``'s
            # own ``owner_id`` — so the approving admin is recorded in the
            # database. For an approved suggestion the approver IS the
            # verifier, which is what ``verified_by`` means.
            #
            # Safe at this scope: the only other use of ``owner_id`` is the
            # ``scope == "personal"`` directory branch, which cannot fire
            # under ``scope="global"``.
            owner_id=reviewed_by,
            # ⚠️ ``description`` is accepted by ``upload_document`` and then
            # IGNORED — referenced zero times in that method's body, so it
            # reaches no column and no ChromaDB metadata. ``category`` is
            # the same, surviving only in the transient return dict.
            #
            # Passed anyway because it is the natural sink and a future one
            # would read it, but it records NOTHING today. The
            # case/extractor/suggestion lineage the dropped ``metadata=``
            # was carrying still has no home, and neither does
            # ``verification_level: 2`` (the derive yields EXPERIMENTAL).
            # That pair IS the "where does the metadata belong" decision
            # this issue names, and #878 owns it. Do not read this argument
            # as provenance.
            description=(
                f"Extracted from case {suggestion.case_id} "
                f"by {suggestion.extracted_by} "
                f"(suggestion {suggestion_id})"
            ),
        )
        knowledge_item_id = result.get("document_id")
        if not knowledge_item_id:
            # Never mark a suggestion approved against an id we did not
            # get: the point of this fix is that approval stops claiming
            # success it cannot back.
            raise RuntimeError(
                "upload_document returned no document_id for suggestion "
                f"{suggestion_id}; nothing was linked"
            )

        # Mark suggestion as approved — COMPENSATED (#1214).
        #
        # ``approve()`` re-checks readiness, and ``update_suggestion``
        # concurrently resets ``pii_scan_status`` on any content edit, so this
        # can raise AFTER the publish has already written a knowledge_items
        # row, its ChromaDB chunks and a file on disk. Without compensation the
        # corpus keeps a published runbook that no suggestion links to, while
        # the client is told the approval failed — an orphan created by the
        # error path itself.
        #
        # ``ingest_runbook`` already applies exactly this discipline one level
        # down (SQL row deleted when the vector write fails); this is the same
        # rule for the step above it. ``delete_document`` hard-deletes an
        # authored id (``kb_<16 hex>``, which is what ``upload_document``
        # mints), removing both the row and its vectors.
        try:
            suggestion.approve(
                reviewed_by=reviewed_by,
                knowledge_item_id=knowledge_item_id,
                review_notes=review_notes,
            )
        except Exception:
            await self._rollback_published_item(knowledge_item_id, suggestion_id)
            raise

        self.logger.info(
            f"Approved suggestion {suggestion_id}, created knowledge item {knowledge_item_id}"
        )

        return {
            "suggestion_id": suggestion_id,
            "knowledge_item_id": knowledge_item_id,
            "status": "approved",
        }

    async def _rollback_published_item(
        self, knowledge_item_id: str, suggestion_id: str
    ) -> None:
        """Delete a knowledge item published for an approval that then failed.

        Best-effort and NEVER raises: it runs inside an ``except`` block whose
        original exception is what the caller must see. A rollback that itself
        raised would replace a truthful "approval failed" with an unrelated
        error and still leave the orphan — so a failed rollback is logged as the
        operator-actionable event it is (an orphaned KB item, named by id) and
        swallowed.
        """
        try:
            result = await self._knowledge_service.delete_document(knowledge_item_id)
            if not (result or {}).get("success"):
                self.logger.error(
                    "Rollback of knowledge item %s (suggestion %s) did not "
                    "delete it: %s. The knowledge base now holds an item with "
                    "no suggestion back-link.",
                    knowledge_item_id,
                    suggestion_id,
                    (result or {}).get("error", "no reason reported"),
                )
            else:
                self.logger.warning(
                    "Rolled back knowledge item %s after approval of "
                    "suggestion %s failed",
                    knowledge_item_id,
                    suggestion_id,
                )
        except Exception as rollback_error:
            self.logger.error(
                "Rollback of knowledge item %s (suggestion %s) FAILED: %s. "
                "The knowledge base now holds an item with no suggestion "
                "back-link.",
                knowledge_item_id,
                suggestion_id,
                rollback_error,
            )

    async def reject_suggestion(
        self,
        suggestion_id: str,
        reviewed_by: str,
        rejection_reason: str,
        review_notes: Optional[str] = None,
        *,
        organization_id: str,
    ) -> bool:
        """Reject a suggestion.

        Args:
            suggestion_id: Suggestion to reject
            reviewed_by: User ID of reviewer
            rejection_reason: Why rejected
            review_notes: Optional additional notes
            organization_id: Actor's tenant; REQUIRED

        Returns:
            True if rejected, False if not found or out of tenant
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
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
        title: Optional[str] = None,
        content: Optional[str] = None,
        suggested_type: Optional[str] = None,
        *,
        organization_id: str,
    ) -> Optional[KnowledgeSuggestion]:
        """Update a suggestion's content.

        Args:
            suggestion_id: Suggestion to update
            title: New title
            content: New content
            suggested_type: New type
            organization_id: Actor's tenant; REQUIRED

        Returns:
            Updated suggestion or None if not found or out of tenant
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
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
        *,
        organization_id: str,
    ) -> Optional[KnowledgeSuggestion]:
        """Mark PII as remediated after manual review.

        Args:
            suggestion_id: Suggestion that was remediated
            remediated_by: User ID who remediated
            organization_id: Actor's tenant; REQUIRED

        Returns:
            Updated suggestion or None if not found or out of tenant
        """
        suggestion = await self.get_suggestion_visible(
            suggestion_id, organization_id=organization_id
        )
        if not suggestion:
            return None

        suggestion.mark_pii_remediated(remediated_by)
        self.logger.info(f"PII remediated for suggestion {suggestion_id}")
        return suggestion

    def to_api_response(
        self, suggestion: KnowledgeSuggestion, include_content: bool = False
    ) -> Dict[str, Any]:
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
