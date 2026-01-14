"""
OODA Response Converter

Converts structured OODAResponse from phase handlers into AgentResponse for API layer.
This maintains separation between internal OODA framework and external API contracts.
"""

from typing import Dict, Any
from faultmaven.models.responses import (
    OODAResponse,
    ConsultantResponse,
    LeadInvestigatorResponse,
    OODAEvidenceRequest,
)
from faultmaven.models.api import AgentResponse, ResponseType, Source, SourceType
from faultmaven.models.llm_schemas import EvidenceRequestToAdd


def ooda_to_agent_response(
    ooda_response: OODAResponse,
    session_id: str,
    case_id: str = None,
    view_state: Any = None,
) -> AgentResponse:
    """
    Convert OODAResponse to AgentResponse (API contract).

    Args:
        ooda_response: Internal OODA framework response
        session_id: Session identifier
        case_id: Optional case identifier
        view_state: Optional view state for frontend rendering

    Returns:
        AgentResponse suitable for API serialization
    """

    # Determine response_type based on OODA response characteristics
    response_type = _determine_response_type(ooda_response)

    # Extract sources from suggested_actions and response metadata
    sources = _extract_sources(ooda_response)

    # Convert suggested actions to evidence requests (v3.1.0 format)
    evidence_requests = _convert_to_evidence_requests(ooda_response)

    # Build AgentResponse
    agent_response = AgentResponse(
        schema_version="3.1.0",
        content=ooda_response.answer,
        response_type=response_type,
        session_id=session_id,
        case_id=case_id,
        sources=sources,
        view_state=view_state,
        evidence_requests=evidence_requests,
    )

    return agent_response


def _determine_response_type(ooda_response: OODAResponse) -> ResponseType:
    """
    Determine appropriate ResponseType based on OODA response characteristics.

    Maps OODA framework responses to v3.0 Response-Format-Driven Design types.
    """

    # Check for clarifying questions
    if (
        ooda_response.clarifying_questions
        and len(ooda_response.clarifying_questions) > 0
    ):
        return ResponseType.CLARIFICATION_REQUEST

    # Check for ConsultantResponse problem detection
    if isinstance(ooda_response, ConsultantResponse):
        if ooda_response.problem_detected:
            # Consultant detected a problem, should ask if user wants investigation
            if ooda_response.severity in ["high", "critical"]:
                return ResponseType.CONFIRMATION_REQUEST
            else:
                return ResponseType.ANSWER
        else:
            # Simple question answered
            return ResponseType.ANSWER

    # Check for LeadInvestigatorResponse phase-specific responses
    if isinstance(ooda_response, LeadInvestigatorResponse):
        # Check for solution proposal (Phase 5)
        if ooda_response.solution_proposal:
            return ResponseType.SOLUTION_READY

        # Check for evidence requests
        if ooda_response.evidence_request:
            return ResponseType.NEEDS_MORE_DATA

        # Check for plan proposals (Phase 1-2)
        if (
            ooda_response.suggested_actions
            and len(ooda_response.suggested_actions) >= 3
        ):
            return ResponseType.PLAN_PROPOSAL

        # Default for investigation responses
        return ResponseType.ANSWER

    # Default response type
    return ResponseType.ANSWER


def _extract_sources(ooda_response: OODAResponse) -> list[Source]:
    """
    Extract sources from OODA response for citation.

    Converts response metadata and context into Source objects for user trust.
    """
    sources = []

    # Extract from response metadata
    metadata = ooda_response.response_metadata or {}

    # Add knowledge base sources if referenced
    if "kb_sources" in metadata:
        for kb_source in metadata.get("kb_sources", []):
            sources.append(
                Source(
                    type=SourceType.KNOWLEDGE_BASE,
                    content=kb_source.get("content", ""),
                    confidence=kb_source.get("confidence", 0.0),
                    metadata=kb_source.get("metadata", {}),
                )
            )

    # Add previous analysis if this is a follow-up
    if "previous_phase" in metadata:
        sources.append(
            Source(
                type=SourceType.PREVIOUS_ANALYSIS,
                content=f"Previous phase: {metadata['previous_phase']}",
                confidence=metadata.get("confidence", 0.8),
                metadata={"phase": metadata["previous_phase"]},
            )
        )

    return sources


def _convert_to_evidence_requests(
    ooda_response: OODAResponse,
) -> list[EvidenceRequestToAdd]:
    """
    Convert OODA evidence requests to canonical EvidenceRequestToAdd.
    """
    evidence_requests = []

    # Check for explicit evidence request (LeadInvestigatorResponse)
    if (
        isinstance(ooda_response, LeadInvestigatorResponse)
        and ooda_response.evidence_request
    ):
        ev_req = ooda_response.evidence_request

        urgency = getattr(ev_req, "urgency", "normal") or "normal"
        priority_map = {
            "immediate": "high",
            "high": "high",
            "normal": "medium",
            "low": "low",
        }
        priority = priority_map.get(str(urgency).lower(), "medium")

        request_text_parts = [ev_req.description]
        if getattr(ev_req, "collection_method", None):
            request_text_parts.append(f"How to collect: {ev_req.collection_method}")
        if getattr(ev_req, "expected_result", None):
            request_text_parts.append(f"Expected result: {ev_req.expected_result}")

        request_text = "\n".join([p for p in request_text_parts if p]).strip()[:500]
        purpose = (
            ev_req.description or "Evidence requested for investigation"
        ).strip()[:500]

        evidence_requests.append(
            EvidenceRequestToAdd(
                request_text=request_text,
                purpose=purpose,
                priority=priority,  # type: ignore[arg-type]
            )
        )

    # Convert suggested_actions to evidence requests (if they request data)
    for action in ooda_response.suggested_actions or []:
        if _is_evidence_action(action):
            description = getattr(action, "description", "") or ""
            request_text = (
                description.strip()[:500]
                or "Please provide additional diagnostic evidence."
            )
            evidence_requests.append(
                EvidenceRequestToAdd(
                    request_text=request_text,
                    purpose="Gather additional diagnostic evidence",
                    priority="medium",
                )
            )

    return evidence_requests


def _is_evidence_action(action: Any) -> bool:
    """Determine if a suggested action is requesting evidence."""
    # Check if action description indicates data/evidence request
    evidence_keywords = [
        "check",
        "show",
        "provide",
        "upload",
        "share",
        "run",
        "collect",
        "gather",
        "inspect",
        "review",
        "analyze",
    ]
    desc_lower = action.description.lower()
    return any(keyword in desc_lower for keyword in evidence_keywords)
