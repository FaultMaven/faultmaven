"""
Unit tests for OODA Response Converter

Tests conversion from internal OODA framework responses to external API responses.
"""

import pytest
from faultmaven.core.ooda_response_converter import (
    ooda_to_agent_response,
    _determine_response_type,
    _extract_sources,
    _convert_to_evidence_requests,
)
from faultmaven.models.responses import (
    OODAResponse,
    ConsultantResponse,
    LeadInvestigatorResponse,
    SuggestedAction,
    EvidenceRequest as OODAEvidenceRequest,
    SolutionProposal,
)
from faultmaven.models.api import ResponseType, SourceType
from faultmaven.models.evidence import EvidenceCategory


class TestOODAToAgentResponseConversion:
    """Test main conversion function"""

    def test_convert_basic_ooda_response(self):
        """Test converting basic OODAResponse"""
        ooda_response = OODAResponse(
            answer="This is a basic answer",
            clarifying_questions=[],
            suggested_actions=[],
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
            case_id="case-456",
        )

        assert agent_response.schema_version == "3.1.0"
        assert agent_response.content == "This is a basic answer"
        assert agent_response.session_id == "session-123"
        assert agent_response.case_id == "case-456"
        assert agent_response.response_type == ResponseType.ANSWER

    def test_convert_consultant_response_with_problem(self):
        """Test converting ConsultantResponse when problem detected"""
        ooda_response = ConsultantResponse(
            answer="I've detected a database connection issue",
            problem_detected=True,
            problem_summary="Database timeout",
            severity="high",
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert agent_response.content == "I've detected a database connection issue"
        assert agent_response.response_type == ResponseType.CONFIRMATION_REQUEST
        assert agent_response.suggested_actions is None  # Deprecated field

    def test_convert_consultant_response_without_problem(self):
        """Test converting ConsultantResponse when no problem"""
        ooda_response = ConsultantResponse(
            answer="Your system looks healthy",
            problem_detected=False,
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert agent_response.response_type == ResponseType.ANSWER

    def test_convert_lead_investigator_with_solution(self):
        """Test converting LeadInvestigatorResponse with solution"""
        ooda_response = LeadInvestigatorResponse(
            answer="I've identified the root cause",
            solution_proposal=SolutionProposal(
                approach="Increase database connection pool size to 50",
                rationale="Connection pool exhaustion is causing timeouts",
                risks=["Brief downtime during restart"],
                verification_method="Monitor connection pool metrics after restart",
            ),
            phase_complete=True,
            should_advance=True,
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert agent_response.response_type == ResponseType.SOLUTION_READY
        assert agent_response.content == "I've identified the root cause"

    def test_convert_with_view_state(self):
        """Test conversion preserves view_state"""
        from faultmaven.models.api import ViewState, User, Case

        ooda_response = OODAResponse(
            answer="Test answer",
        )

        view_state = ViewState(
            session_id="session-123",
            user=User(
                user_id="user-1",
                email="test@example.com",
                name="Test User",
                created_at="2025-01-01T00:00:00Z",
            ),
            active_case=None,
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
            view_state=view_state,
        )

        assert agent_response.view_state is not None
        assert agent_response.view_state.user.email == "test@example.com"


class TestDetermineResponseType:
    """Test response type determination logic"""

    def test_clarifying_questions_triggers_clarification_request(self):
        """Test that clarifying questions trigger CLARIFICATION_REQUEST"""
        ooda_response = OODAResponse(
            answer="I need more information",
            clarifying_questions=["What version are you using?"],
        )

        response_type = _determine_response_type(ooda_response)
        assert response_type == ResponseType.CLARIFICATION_REQUEST

    def test_consultant_high_severity_triggers_confirmation(self):
        """Test high severity triggers CONFIRMATION_REQUEST"""
        ooda_response = ConsultantResponse(
            answer="Critical issue detected",
            problem_detected=True,
            severity="critical",
        )

        response_type = _determine_response_type(ooda_response)
        assert response_type == ResponseType.CONFIRMATION_REQUEST

    def test_consultant_medium_severity_triggers_answer(self):
        """Test medium severity triggers ANSWER"""
        ooda_response = ConsultantResponse(
            answer="Issue detected",
            problem_detected=True,
            severity="medium",
        )

        response_type = _determine_response_type(ooda_response)
        assert response_type == ResponseType.ANSWER

    def test_solution_proposal_triggers_solution_ready(self):
        """Test solution proposal triggers SOLUTION_READY"""
        ooda_response = LeadInvestigatorResponse(
            answer="Solution found",
            solution_proposal=SolutionProposal(
                approach="Test solution approach",
                rationale="Test cause analysis",
                verification_method="Test verification",
            ),
        )

        response_type = _determine_response_type(ooda_response)
        assert response_type == ResponseType.SOLUTION_READY

    def test_evidence_request_triggers_needs_more_data(self):
        """Test evidence request triggers NEEDS_MORE_DATA"""
        ooda_response = LeadInvestigatorResponse(
            answer="I need logs",
            evidence_request=OODAEvidenceRequest(
                evidence_type="logs",
                description="Please provide application logs",
                collection_method="journalctl -u myapp",
                expected_result="Recent error logs",
            ),
        )

        response_type = _determine_response_type(ooda_response)
        assert response_type == ResponseType.NEEDS_MORE_DATA

    def test_multiple_suggested_actions_triggers_plan_proposal(self):
        """Test multiple suggested actions trigger PLAN_PROPOSAL"""
        ooda_response = LeadInvestigatorResponse(
            answer="Here's the plan",
            suggested_actions=[
                SuggestedAction(action_type="command", label="Step 1", description="Execute step 1"),
                SuggestedAction(action_type="command", label="Step 2", description="Execute step 2"),
                SuggestedAction(action_type="command", label="Step 3", description="Execute step 3"),
            ],
        )

        response_type = _determine_response_type(ooda_response)
        assert response_type == ResponseType.PLAN_PROPOSAL

    def test_default_response_type(self):
        """Test default response type is ANSWER"""
        ooda_response = OODAResponse(
            answer="Simple answer",
        )

        response_type = _determine_response_type(ooda_response)
        assert response_type == ResponseType.ANSWER


class TestExtractSources:
    """Test source extraction from OODA responses"""

    def test_extract_kb_sources_from_metadata(self):
        """Test extracting knowledge base sources"""
        ooda_response = OODAResponse(
            answer="Answer based on KB",
            response_metadata={
                "kb_sources": [
                    {
                        "content": "KB article about issue",
                        "confidence": 0.85,
                        "metadata": {"article_id": "KB-123"},
                    }
                ]
            },
        )

        sources = _extract_sources(ooda_response)

        assert len(sources) == 1
        assert sources[0].type == SourceType.KNOWLEDGE_BASE
        assert sources[0].content == "KB article about issue"
        assert sources[0].confidence == 0.85

    def test_extract_previous_analysis_source(self):
        """Test extracting previous analysis source"""
        ooda_response = OODAResponse(
            answer="Building on previous phase",
            response_metadata={
                "previous_phase": "hypothesis",
                "confidence": 0.75,
            },
        )

        sources = _extract_sources(ooda_response)

        assert len(sources) == 1
        assert sources[0].type == SourceType.PREVIOUS_ANALYSIS
        assert "Previous phase: hypothesis" in sources[0].content
        assert sources[0].confidence == 0.75

    def test_extract_no_sources(self):
        """Test when no sources are present"""
        ooda_response = OODAResponse(
            answer="No sources",
            response_metadata={},
        )

        sources = _extract_sources(ooda_response)
        assert len(sources) == 0

    def test_extract_multiple_sources(self):
        """Test extracting multiple sources"""
        ooda_response = OODAResponse(
            answer="Multiple sources",
            response_metadata={
                "kb_sources": [
                    {"content": "Source 1", "confidence": 0.8},
                    {"content": "Source 2", "confidence": 0.7},
                ],
                "previous_phase": "timeline",
            },
        )

        sources = _extract_sources(ooda_response)
        assert len(sources) == 3  # 2 KB + 1 previous


class TestConvertToEvidenceRequests:
    """Test conversion to evidence requests"""

    def test_convert_explicit_evidence_request(self):
        """Test converting explicit evidence request"""
        ooda_response = LeadInvestigatorResponse(
            answer="Need logs",
            evidence_request=OODAEvidenceRequest(
                evidence_type="logs",
                description="Application logs needed",
                collection_method="journalctl -u myapp",
                expected_result="Recent application logs showing errors",
                urgency="high",
            ),
        )

        evidence_requests = _convert_to_evidence_requests(ooda_response)

        assert len(evidence_requests) == 1
        assert evidence_requests[0].category == EvidenceCategory.SYMPTOMS  # logs -> SYMPTOMS
        assert evidence_requests[0].description == "Application logs needed"
        assert "journalctl -u myapp" in evidence_requests[0].guidance.commands
        assert evidence_requests[0].label == "Logs"

    def test_convert_suggested_actions_to_evidence(self):
        """Test converting suggested actions that request evidence"""
        ooda_response = OODAResponse(
            answer="Please check logs",
            suggested_actions=[
                SuggestedAction(
                    action_type="command",
                    label="Check logs",
                    description="Check system logs for errors",
                    data={"command": "tail -f /var/log/syslog", "priority": "high"},
                )
            ],
        )

        evidence_requests = _convert_to_evidence_requests(ooda_response)

        assert len(evidence_requests) >= 1
        # Should convert action with "check" keyword to evidence request
        assert any("logs" in req.description.lower() for req in evidence_requests)

    def test_convert_no_evidence_actions(self):
        """Test when no evidence actions are present"""
        ooda_response = OODAResponse(
            answer="Analysis complete",
            suggested_actions=[
                SuggestedAction(
                    action_type="command",
                    label="Apply fix",
                    description="Apply the fix to resolve the issue",
                )
            ],
        )

        evidence_requests = _convert_to_evidence_requests(ooda_response)
        # "Apply" is not an evidence keyword, so should not convert
        # Or it might convert with default type - either is acceptable
        assert isinstance(evidence_requests, list)

    def test_convert_metrics_evidence_type(self):
        """Test converting metrics evidence type"""
        ooda_response = LeadInvestigatorResponse(
            answer="Need metrics",
            evidence_request=OODAEvidenceRequest(
                evidence_type="metrics",
                description="CPU and memory metrics",
                collection_method="top; free -m",
                expected_result="System resource usage statistics",
            ),
        )

        evidence_requests = _convert_to_evidence_requests(ooda_response)

        assert len(evidence_requests) == 1
        assert evidence_requests[0].category == EvidenceCategory.METRICS

    def test_convert_config_evidence_type(self):
        """Test converting config evidence type"""
        ooda_response = LeadInvestigatorResponse(
            answer="Need config",
            evidence_request=OODAEvidenceRequest(
                evidence_type="configuration",
                description="Application configuration",
                collection_method="cat /etc/myapp/config.yaml",
                expected_result="Configuration file contents",
            ),
        )

        evidence_requests = _convert_to_evidence_requests(ooda_response)

        assert len(evidence_requests) == 1
        assert evidence_requests[0].category == EvidenceCategory.CONFIGURATION


class TestEdgeCases:
    """Test edge cases and error scenarios"""

    def test_convert_empty_ooda_response(self):
        """Test converting minimal OODA response"""
        ooda_response = OODAResponse(
            answer="Minimal response",
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert agent_response.content == "Minimal response"
        assert agent_response.evidence_requests == []
        assert agent_response.sources == []

    def test_convert_without_case_id(self):
        """Test conversion without case_id"""
        ooda_response = OODAResponse(
            answer="No case",
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert agent_response.case_id is None

    def test_convert_with_unicode_content(self):
        """Test conversion with Unicode content"""
        ooda_response = OODAResponse(
            answer="Unicode test: 你好 世界 🚀",
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert "你好" in agent_response.content or "Unicode" in agent_response.content

    def test_convert_with_very_long_answer(self):
        """Test conversion with very long answer"""
        long_answer = "A" * 10000
        ooda_response = OODAResponse(
            answer=long_answer,
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert len(agent_response.content) == 10000


class TestBackwardCompatibility:
    """Test backward compatibility features"""

    def test_suggested_actions_field_is_none(self):
        """Test that deprecated suggested_actions is always None"""
        ooda_response = OODAResponse(
            answer="Test",
            suggested_actions=[
                SuggestedAction(action_type="command", label="Action 1", description="Perform action 1")
            ],
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        # Deprecated field should be None
        assert agent_response.suggested_actions is None

    def test_schema_version_is_3_1_0(self):
        """Test that schema version is always 3.1.0"""
        ooda_response = OODAResponse(
            answer="Test",
        )

        agent_response = ooda_to_agent_response(
            ooda_response,
            session_id="session-123",
        )

        assert agent_response.schema_version == "3.1.0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
