"""Knowledge-extraction route (fm#1707).

Extracts reusable knowledge from a case's messages and evidence into a
knowledge-base suggestion. Split out of ``case/api/routes.py`` as its own
sub-router (A6).
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status

from faultmaven.api.v1.auth_dependencies import (
    require_actor_enterprise,
    require_authentication,
)
from faultmaven.api.v1.dependencies import (
    SUGGESTION_QUEUE_FULL,
    get_case_service,
    get_suggestion_service,
)
from faultmaven.exceptions import ServiceUnavailableException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import UserDTO

router = APIRouter(prefix="/cases", tags=["cases"])


logger = logging.getLogger(__name__)


# ============================================================
# Knowledge Extraction Endpoint
# ============================================================


@router.post(
    "/{case_id}/extract-knowledge",
    response_model=Dict[str, Any],
    summary="Extract Knowledge from Case",
    description="Extract reusable knowledge from a case into a suggestion for the knowledge base.",
    status_code=status.HTTP_201_CREATED,
)
@trace("api_extract_knowledge")
async def extract_knowledge_from_case(
    case_id: str = Path(..., description="Case ID to extract knowledge from"),
    request_body: Optional[Dict[str, Any]] = Body(default=None),
    case_service: ICaseService = Depends(get_case_service),
    suggestion_service=Depends(get_suggestion_service),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Extract knowledge from a case conversation into a suggestion.

    This endpoint uses LLM to analyze the case's messages and evidence,
    then generates a reusable knowledge article (runbook, troubleshooting guide).

    The suggestion is automatically scanned for PII and placed in a
    "pending_review" state for admin approval in the Dashboard Review Inbox.

    Answers 503 when no suggestion service is composed for this process — the
    extraction cannot be stored, and a suggestion id that resolves to nothing is
    worse than a refusal.

    Args:
        case_id: Case to extract knowledge from
        request_body: Optional configuration:
            - include_messages: Include case conversation (default: true)
            - include_evidence: Include evidence summaries (default: true)
            - title_suggestion: Optional title for the suggestion

    Returns:
        KnowledgeExtractionResponse with suggestion details
    """
    from faultmaven.models.api import KnowledgeExtractionResponse
    from faultmaven.utils.serialization import to_json_compatible

    try:
        # Verify case exists and the caller OWNS it. Extraction mints a
        # knowledge suggestion out of the case's transcript and evidence and
        # attributes it to the extractor, so it is a write on the owner's
        # material: a read share does not authorise it (ADR-013 D4, as amended
        # 2026-10-09).
        case = await case_service.get_case(
            case_id, current_user.user_id, owner_only=True
        )
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Parse request body
        include_messages = True
        include_evidence = True
        title_suggestion = None
        if request_body:
            include_messages = request_body.get("include_messages", True)
            include_evidence = request_body.get("include_evidence", True)
            title_suggestion = request_body.get("title_suggestion")

        # ``suggestion_service`` arrives via Depends(get_suggestion_service) —
        # the SAME shared dependency the knowledge-side review routes use, which
        # answers 503 when the composition root produced no singleton (#1214).
        #
        # It used to be looked up inline here, guarded by a
        # ``request: Request = None`` default FastAPI never actually passes, and
        # falling back to a fresh ``SuggestionService()``: the suggestion went
        # into that throwaway instance's private dict, the instance died with
        # the request, and the approve that followed could never find it (404).
        # As a dependency the 503 policy lives in one place and the route is
        # overridable in tests like every sibling.

        # THE TENANT THIS SUGGESTION IS STORED UNDER (#1227).
        #
        # It was ``getattr(case, "organization_id", "default")``. Two things
        # were wrong with that, and the store change turns the second from
        # cosmetic into fatal:
        #
        # 1. The suggestion has to be stamped with the SAME tenant the review
        #    routes scope by, or the reviewer never sees it. Every suggestion
        #    route — list, get, update, approve, reject, remediate — resolves
        #    its predicate with ``require_actor_enterprise``, so that is the
        #    value the write side owes them. The case's own enterprise is the
        #    same value on the success path (the case was just fetched through
        #    the caller's own scoped read), which is exactly why reading it off
        #    the case was never the SOURCE of the answer.
        # 2. ``"default"`` is not a tenant id. It was a silent placeholder while
        #    the store was a dict keyed by nothing;
        #    ``knowledge_suggestions.enterprise_id`` is a NOT NULL FK to
        #    ``enterprises`` with ``PRAGMA foreign_keys=ON``, so the same
        #    fallback fails the INSERT outright — and under PostgreSQL RLS it
        #    would fail the policy's WITH CHECK as well, because the value would
        #    not match the session's ``app.current_enterprise_id``.
        #
        # ``require_actor_enterprise`` refuses with 403 rather than handing back
        # a value to degrade with, which is the right answer: a request that
        # owns no tenant has nowhere to put the extraction.
        enterprise_id = require_actor_enterprise(current_user)

        # Extract knowledge
        suggestion = await suggestion_service.extract_knowledge_from_case(
            case_id=case_id,
            enterprise_id=enterprise_id,
            extracted_by=current_user.user_id,
            include_messages=include_messages,
            include_evidence=include_evidence,
            title_suggestion=title_suggestion,
        )

        # Build response
        return {
            "suggestion_id": suggestion.suggestion_id,
            "case_id": case_id,
            "status": suggestion.status.value,
            "suggested_title": suggestion.suggested_title,
            "suggested_content": suggestion.suggested_content,
            "pii_scan_status": suggestion.pii_scan_status.value,
            # The runbook quality gate's verdict on what was just extracted
            # (#1226). This response is the FIRST surface a reviewer sees, and
            # it is the one the extraction flow actually lands on — omitting the
            # verdict here meant they learned the draft was unpublishable only
            # by pressing approve and reading a 422, which is the whole failure
            # this issue is about. ``passed: null`` means not yet evaluated;
            # never read it as "fine".
            "validation": {
                "passed": suggestion.validation_passed,
                "errors": list(suggestion.validation_errors),
                "warnings": list(suggestion.validation_warnings),
            },
            "extracted_from": {
                "case_title": suggestion.source_case_title,
                "message_count": suggestion.message_count,
                "evidence_count": suggestion.evidence_count,
            },
            "created_at": to_json_compatible(suggestion.created_at),
        }

    except HTTPException:
        raise
    except ServiceUnavailableException as unavailable:
        # This organization's review queue is at its ceiling, and the store
        # refuses to add to it rather than deleting a decided suggestion
        # (#1227). 503 rather than 500: nothing is broken, the queue is full,
        # and the fix is to review it.
        # The static constant, never str(unavailable): this module's AST guard
        # forbids carrying a caught exception into any 5xx body, and that rule
        # does not get an exemption for a message that happens to be ours today.
        # The exception's own text goes to the log instead.
        logger.warning(
            "Knowledge extraction refused for case %s: %s", case_id, unavailable
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SUGGESTION_QUEUE_FULL,
        ) from unavailable
    except Exception as e:
        logger.error(
            f"Knowledge extraction failed for case {case_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Knowledge extraction failed",
        )
