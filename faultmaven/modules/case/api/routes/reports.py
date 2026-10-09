"""Report generation, recommendation and closure routes (fm#1707).

The document-generation surface: report recommendations, generating and
listing case reports, downloading a report, and case closure. Split out of
``case/api/routes.py`` as its own sub-router (A6); ``dependencies.py`` holds
the DI accessors and guard every handler here uses.
"""

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_case_repository
from faultmaven.config.tenant_context import get_current_enterprise_id
from faultmaven.exceptions import CASE_TERMINAL
from faultmaven.infrastructure.base_client import CircuitBreakerError
from faultmaven.infrastructure.knowledge.runbook_kb import RESULTS_UNREADABLE_CODE
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_runbook_kb_dependency,
    check_case_service_available,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.utils.serialization import to_json_compatible

router = APIRouter(prefix="/cases", tags=["cases"])


logger = logging.getLogger(__name__)


# =============================================================================
# Document Generation and Closure Endpoints
# =============================================================================


def _recommendation_unavailable_detail(unreadable: bool) -> str:
    """The 503 body for a runbook-recommendation refusal, chosen by cause.

    The remediation differs, so the message must too. Almost everything that
    reaches the handler is a transient unavailability that retrying clears;
    ``RUNBOOK_RESULTS_UNREADABLE`` is deterministic — the knowledge base is up
    and answering, and the offending rows fail identically on every attempt
    until someone re-indexes them. Telling that operator to "retry once the
    knowledge base is available" points them at a subsystem that is not broken
    and at an action that cannot work (#912).

    A module-level function rather than an inline branch so the wording is
    reachable from a test: the branch it replaced was the only part of that
    fix nothing exercised.

    Takes a **bool**, not the exception. The caller classifies and passes the
    verdict, so no exception object ever reaches the response-building path —
    which is the invariant ``test_no_500_site_interpolates_the_exception``
    exists to keep. Handing the exception to a helper looks harmless and is not:
    the guard cannot see inside the call, so it must assume the text travels,
    and one edit to this function would make that assumption true.
    """
    if unreadable:
        # "The closest", not "only". The refusal fires whenever an unreadable
        # runbook outranks every readable one, which includes mixed result sets
        # where other runbooks were read perfectly well — so "found only
        # runbooks it could not read" would be false in exactly the case the
        # guard was widened to cover.
        return (
            "The closest-matching runbooks could not be read, so duplicate "
            "runbooks cannot be ruled out. The knowledge base is available; "
            "the affected runbooks need re-indexing. Retrying will not clear "
            "this."
        )
    return (
        "Runbook similarity search is unavailable, so duplicate runbooks "
        "cannot be ruled out. Retry once the knowledge base is available."
    )


@router.get(
    "/{case_id}/report-recommendations", dependencies=[Depends(require_authentication)]
)
@trace("api_get_report_recommendations")
async def get_report_recommendations(
    case_id: str,
    request: Request,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
    runbook_kb=Depends(_di_get_runbook_kb_dependency),
):
    """
    Get intelligent report recommendations for a resolved case.

    Returns recommendations for which reports to generate, including runbook
    suggestions based on a similarity search of the published runbooks the
    REQUESTER can read (global ∪ their own ∪ shared to their teams). The
    requester is the principal who will act on the answer, so their scope
    governs here; the engine's terminal-turn dedup scopes on the case owner
    (fm#1030).

    Recommendation Logic:
    - Always available: the case's terminal summary
    - Runbook: a ≥70% best-chunk match is surfaced by title and score for the
      user to judge; below that, generation is recommended. There is no
      auto-"reuse" verdict.

    Args:
        case_id: Case identifier
        case_service: Injected case service
        current_user: Authenticated user

    Returns:
        ReportRecommendation with available types and runbook suggestion

    Raises:
        400: Case not in resolved state
        404: Case not found or access denied
        500: Internal server error
    """
    from faultmaven.modules.case.contracts import ReportRecommendation
    from faultmaven.modules.report.domain.services.report_recommendation_service import (
        ReportRecommendationService,
    )

    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404, detail="Case not found or access denied"
            )

        # Validate case is in resolved state (terminal state with solution)
        # Note: Only RESOLVED is valid - CLOSED is terminal without solution
        if case.state != CaseState.RESOLVED:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_case_state",
                    "message": f"Cannot get report recommendations for case in {case.state.value} state. Case must be RESOLVED.",
                    "current_state": case.state.value,
                    "required_state": CaseState.RESOLVED.value,
                },
            )

        if runbook_kb is None:
            raise HTTPException(
                status_code=503,
                detail="Vector store not available. Check ChromaDB configuration.",
            )
        # Requester-scope dependencies: None in standalone (no teams exist —
        # the team arm is empty by construction, not a narrowed search).
        recommendation_service = ReportRecommendationService(
            runbook_kb=runbook_kb,
            team_service=getattr(request.app.state, "team_service", None),
            share_repository=getattr(request.app.state, "share_repository", None),
        )

        # Get intelligent recommendations, scoped to the requester.
        #
        # The tenant term here is the ENTERPRISE the request is bound to, not the
        # caller's organization claim. The team arm matches the share row's own
        # ``enterprise_id`` (ADR-017 D1/D4), so feeding it an organization made
        # the arm empty in every configuration — absent in standalone and for a
        # cloud account in no organization, and a billing id where present — and
        # a runbook shared to a common team stopped counting as a duplicate.
        recommendations = await recommendation_service.get_available_report_types(
            case=case,
            requester_user_id=current_user.user_id,
            requester_enterprise_id=get_current_enterprise_id(),
        )

        logger.info(
            f"Report recommendations generated for case {case_id}",
            extra={
                "case_id": case_id,
                "runbook_action": recommendations.runbook_recommendation.action,
                "available_types": [
                    t.value for t in recommendations.available_for_generation
                ],
            },
        )

        # Return recommendations
        return recommendations.model_dump()

    except HTTPException:
        raise
    except (KnowledgeBaseError, TimeoutError, CircuitBreakerError) as e:
        # Refuse rather than recommend. The runbook recommendation IS a claim
        # about what the knowledge base holds, so when the similarity search
        # cannot run there is no honest recommendation to give — answering
        # anyway is what produced a permanent "generate" and let duplicate
        # runbooks accumulate (#944). 503 matches the sibling refusal above
        # for a missing vector store, and keeps the response contract intact.
        #
        # TimeoutError and CircuitBreakerError are caught alongside the typed
        # error because call_external raises those two unwrapped — they are
        # the commonest unavailability modes, and routing them to the generic
        # handler below would answer a generic 500 instead. (Its body is static
        # — no text leaks — but the refusal is erased: the caller loses both the
        # 503 and the remediation this handler chooses below.)
        logger.warning(
            f"Report recommendations unavailable for case {case_id}: {e}",
            extra={"case_id": case_id},
            exc_info=True,
        )
        # The comparison, not the exception, is what crosses into the response.
        # ``ast.Compare`` yields a bool, so no exception text can travel through
        # it — the same shape the leak guard recognises elsewhere in the app.
        results_unreadable = getattr(e, "error_code", None) == RESULTS_UNREADABLE_CODE
        raise HTTPException(
            status_code=503,
            detail=_recommendation_unavailable_detail(results_unreadable),
        )
    except Exception as e:
        logger.error(
            f"Failed to get report recommendations for case {case_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to get report recommendations"
        )


@router.post("/{case_id}/reports", dependencies=[Depends(require_authentication)])
@trace("api_generate_case_reports")
async def generate_case_reports(
    case_id: str,
    fastapi_request: Request,
    request_body: Dict[str, Any] = Body(...),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
):
    """Regenerate reports for a terminal case.

    Reports are auto-generated when a case reaches terminal state.
    This endpoint allows regeneration if the original was missing or needs refresh.
    """
    from faultmaven.modules.case.contracts import (
        ReportGenerationRequest,
        ReportType,
        is_default_case_title,
    )

    case_service = check_case_service_available(case_service)

    try:
        # ``owner_only``: regeneration flips ``is_current`` across the case's
        # reports, so it is a WRITE on rows a read share never covered (ADR-013
        # D4, as amended 2026-10-09). Inside one enterprise nothing else separates a teammate from the
        # owner, so this flag is the whole of the boundary here.
        case = await case_service.get_case(
            case_id, current_user.user_id, owner_only=True
        )
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Only terminal cases can have reports
        if not case.is_terminal:
            raise HTTPException(
                status_code=400,
                detail="Reports can only be generated for resolved or closed cases",
            )

        # Get the report service from app.state (Composition Root)
        report_service = getattr(
            fastapi_request.app.state, "report_generation_service", None
        )
        if not report_service:
            raise HTTPException(
                status_code=503,
                detail="Report generation service not available",
            )

        request = ReportGenerationRequest(
            report_types=[ReportType(t) for t in request_body["report_types"]]
        )
        response = await report_service.generate_reports(case, request.report_types)
        return response.model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Report generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate reports")


@router.get("/{case_id}/reports", dependencies=[Depends(require_authentication)])
@trace("api_get_case_reports")
async def get_case_reports(
    request: Request,
    case_id: str,
    include_history: bool = Query(default=False),
    report_type: Optional[str] = Query(default=None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    case_repository: Optional[CaseRepository] = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Retrieve generated reports for a case.

    Composite response: includes both the SQL-persisted ``reports`` rows
    (resolution_summary / closure_summary) and a projected view of any
    conversion drafts linked to this case (returned as synthetic
    ``report_type=runbook`` entries). The case Report tab uses the
    runbook entries to drive the KB-link banner; the underlying runbook
    content lives in ``conversion_drafts`` (the KB Drafts editor), not
    ``reports``.

    Args:
        case_id: Case identifier
        include_history: If True, return all report versions; if False, only current
        report_type: Optional filter by report type (resolution_summary, closure_summary, runbook)

    Returns:
        List of CaseReport objects (summaries plus any case-linked runbook drafts)
    """
    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Check if case_repository is available
        if not case_repository:
            logger.warning("Case repository not available - returning empty list")
            return []

        # Retrieve reports from storage via CaseRepository (TD-001)
        from faultmaven.modules.case.contracts import ReportType

        filter_type = ReportType(report_type) if report_type else None
        reports = await case_repository.get_reports(
            case_id=case_id, include_history=include_history, report_type=filter_type
        )

        # Project case-linked conversion drafts into the same shape so the
        # Report tab's runbook banner has something to count. Skipped when
        # the conversion service isn't wired (e.g., minimal deployments) or
        # the caller filtered to summary-only types.
        if filter_type is None or filter_type == ReportType.RUNBOOK:
            conversion_service = getattr(request.app.state, "conversion_service", None)
            if conversion_service is not None:
                draft_rows = await conversion_service.list_drafts_for_case(case_id)
                reports = list(reports) + [
                    _draft_to_runbook_report(case_id, d) for d in draft_rows
                ]

        logger.info(
            f"Retrieved {len(reports)} reports for case",
            extra={
                "case_id": case_id,
                "include_history": include_history,
                "report_count": len(reports),
            },
        )

        return reports

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to retrieve reports for case {case_id}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to retrieve reports")


def _draft_to_runbook_report(case_id: str, draft: dict) -> dict:
    """Project a conversion-draft row into the CaseReport shape used by the
    Report tab. Returns a dict (not a CaseReport instance) because some draft
    titles violate the model's min_length=10 constraint — the projection is a
    presentation-layer concern, not a domain artifact.

    Only the fields the frontend reads are populated; everything else is left
    out so this stays an unambiguous projection rather than a fake report row.
    """
    return {
        "report_id": draft["draft_id"],
        "case_id": case_id,
        "report_type": "runbook",
        "title": draft["title"],
        "content": "",
        "format": "markdown",
        "generation_status": (
            "completed" if draft.get("knowledge_item_id") else "draft"
        ),
        "generated_at": draft.get("created_at"),
        "is_current": True,
        "version": 1,
        # Pass-through fields used by the deep-link banner.
        "draft_id": draft["draft_id"],
        "conversion_id": draft["conversion_id"],
        "runbook_id": draft["runbook_id"],
        "scope": draft.get("scope"),
        "knowledge_item_id": draft.get("knowledge_item_id"),
    }


@router.get(
    "/{case_id}/reports/{report_id}/download",
    dependencies=[Depends(require_authentication)],
)
@trace("api_download_case_report")
async def download_case_report(
    case_id: str,
    report_id: str,
    format: str = Query(default="markdown"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    case_repository: Optional[CaseRepository] = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Download case report in specified format.

    Args:
        case_id: Case identifier
        report_id: Report identifier
        format: Output format (markdown or pdf) - currently only markdown supported

    Returns:
        File response with report content
    """
    from fastapi.responses import Response

    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Check if case_repository is available
        if not case_repository:
            raise HTTPException(
                status_code=503,
                detail="Report storage not available (case repository not initialized)",
            )

        # Retrieve report from storage via CaseRepository (TD-001)
        report = await case_repository.get_report(report_id)

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Verify report belongs to this case
        if report.case_id != case_id:
            raise HTTPException(
                status_code=403, detail="Report does not belong to this case"
            )

        # Determine content type and filename
        if format == "pdf":
            # TODO: PDF conversion not implemented yet
            raise HTTPException(
                status_code=501,
                detail="PDF format not yet supported - use markdown format",
            )
        else:
            # Return markdown format
            content_type = "text/markdown"
            filename = f"{report.report_type.value}_{case_id}_{report.version}.md"

        logger.info(
            f"Serving report download: {filename}",
            extra={
                "case_id": case_id,
                "report_id": report_id,
                "download_format": format,
            },
        )

        return Response(
            content=report.content,
            media_type=content_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download report {report_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to download report")


@router.post(
    "/{case_id}/close",
    responses={
        409: {
            "description": (
                f"`x-error-code: {CASE_TERMINAL}`: the case is already "
                "resolved or closed. Unlabelled, with `conflict_reason: "
                "concurrent_update`: the case changed while closing; reload "
                "and retry."
            ),
            "headers": {
                "x-error-code": {
                    "description": "Which conflict; absent for a concurrent update.",
                    "schema": {"type": "string", "enum": [CASE_TERMINAL]},
                }
            },
        }
    },
    dependencies=[Depends(require_authentication)],
)
@trace("api_close_case")
async def close_case(
    case_id: str,
    request_body: Optional[Dict[str, Any]] = Body(default=None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    case_repository: Optional[CaseRepository] = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Close case and archive with reports.

    Transitions the case to CLOSED through the engine's terminal executor
    (CaseService.close_case → execute_user_closure): closure_reason is
    engine-derived, closed_at is stamped, and an action-history entry is
    recorded — the same closure rule as the chat-confirmed flow (#915; the
    previous body set ``case.state`` directly, which the terminal-state
    validator rejects, and then called a service method that didn't exist).

    Then marks all latest reports as linked to the closure. Close-first
    ordering: a refused close (404 unknown/not-owner, 409 already
    terminal — mapped by the global exception handlers) must never mark
    reports as closure-linked.

    Returns:
        CaseClosureResponse with list of archived reports
    """
    from faultmaven.modules.case.contracts import CaseClosureResponse

    case_service = check_case_service_available(case_service)

    closed_case = await case_service.close_case(case_id, current_user.user_id)

    # Get current reports for closure (TD-001: via CaseRepository)
    archived_reports = []
    if case_repository:
        try:
            # Get only current reports (latest version of each type)
            latest_reports = await case_repository.get_reports(
                case_id=case_id,
                only_current=True,  # Only current reports (latest version per type)
            )

            if latest_reports:
                # Mark each report as linked to closure
                for report in latest_reports:
                    # Update report to mark as linked to closure
                    updated_report = report.model_copy(
                        update={"linked_to_closure": True}
                    )
                    await case_repository.update_report(updated_report)

                    # The linked CaseReport is the closure response payload
                    # (CaseClosureResponse.archived_reports is List[CaseReport]).
                    archived_reports.append(updated_report)

                logger.info(
                    f"Linked {len(latest_reports)} reports to case closure",
                    extra={"case_id": case_id, "report_count": len(latest_reports)},
                )
            else:
                logger.info(
                    f"No reports to link for case closure",
                    extra={"case_id": case_id},
                )

        except Exception as e:
            logger.warning(
                f"Failed to link reports to closure, case is already closed: {e}",
                extra={"case_id": case_id},
            )
            # The close itself committed; report linking is best-effort.

    logger.info(
        f"Case closed successfully",
        extra={"case_id": case_id, "archived_report_count": len(archived_reports)},
    )

    response = CaseClosureResponse(
        case_id=case_id,
        closed_at=to_json_compatible(closed_case.closed_at),
        archived_reports=archived_reports,
        download_available_until=(
            closed_case.closed_at + timedelta(days=90)
        ).isoformat()
        + "Z",
    )

    return response.model_dump()
