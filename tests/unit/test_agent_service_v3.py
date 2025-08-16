"""Test module for v3.1.0 AgentService implementation."""

import pytest
import uuid
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from typing import Dict, Any, List

from faultmaven.services.agent_service import AgentService
from faultmaven.models.api import (
    QueryRequest,
    AgentResponse,
    ResponseType,
    SourceType,
    Source,
    PlanStep,
    ViewState,
    UploadedData
)
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer
from faultmaven.exceptions import ValidationException


# Module-level fixtures accessible to all test classes

@pytest.fixture
def mock_llm_provider():
    """Fixture providing mocked LLM provider."""
    llm = Mock(spec=ILLMProvider)
    llm.generate_response = AsyncMock(return_value="Mock LLM response")
    return llm

@pytest.fixture
def mock_tools():
    """Fixture providing mocked tools."""
    kb_tool = Mock(spec=BaseTool)
    kb_tool.name = "knowledge_base"
    kb_tool.execute = AsyncMock(return_value={
        "documents": ["Test document"],
        "metadata": [{"source": "test.md"}]
    })
    
    web_tool = Mock(spec=BaseTool)
    web_tool.name = "web_search"
    web_tool.execute = AsyncMock(return_value={
        "results": ["Web search result"]
    })
    
    return [kb_tool, web_tool]

@pytest.fixture
def mock_tracer():
    """Fixture providing mocked tracer."""
    tracer = Mock(spec=ITracer)
    tracer.trace = MagicMock()
    tracer.trace.return_value.__enter__ = Mock()
    tracer.trace.return_value.__exit__ = Mock()
    return tracer

@pytest.fixture
def mock_sanitizer():
    """Fixture providing mocked sanitizer."""
    sanitizer = Mock(spec=ISanitizer)
    sanitizer.sanitize = Mock(side_effect=lambda x: f"sanitized_{x}" if isinstance(x, str) else x)
    return sanitizer

@pytest.fixture
def mock_session_service():
    """Fixture providing mocked session service."""
    session_service = Mock()
    session_service.get_session = AsyncMock(return_value=Mock(
        session_id="test-session",
        data_uploads=["data1", "data2"]
    ))
    session_service.record_query_operation = AsyncMock()
    return session_service

@pytest.fixture
def agent_service(mock_llm_provider, mock_tools, mock_tracer, mock_sanitizer, mock_session_service):
    """Fixture providing AgentService instance."""
    service = AgentService(
        llm_provider=mock_llm_provider,
        tools=mock_tools,
        tracer=mock_tracer,
        sanitizer=mock_sanitizer,
        session_service=mock_session_service
    )
    
    # Mock the logger with async context manager support
    mock_logger = Mock()
    mock_logger.warning = Mock()
    mock_logger.log_boundary = Mock()
    mock_logger.log_event = Mock()
    
    # Create async context manager for logger.operation
    mock_context = Mock()
    mock_context.__aenter__ = AsyncMock(return_value={})
    mock_context.__aexit__ = AsyncMock(return_value=None)
    mock_logger.operation = Mock(return_value=mock_context)
    
    service.logger = mock_logger
    service.log_business_event = Mock()
    service.log_metric = Mock()
    
    return service

@pytest.fixture
def sample_query_request():
    """Fixture providing sample QueryRequest."""
    return QueryRequest(
        session_id="test-session-123",
        query="What is causing the database connection errors?"
    )

@pytest.fixture
def mock_agent_result():
    """Fixture providing mock agent execution result."""
    return {
            "findings": [
                {
                    "type": "error",
                    "message": "Database connection timeout detected",
                    "severity": "high",
                    "timestamp": "2024-01-01T12:00:00Z",
                    "source": "log_analysis"
                }
            ],
            "recommendations": [
                "Increase connection timeout",
                "Check network connectivity"
            ],
            "next_steps": [
                "Monitor connection pool",
                "Review database logs",
                "Test network latency"
            ],
            "root_cause": "Network latency causing connection timeouts",
            "confidence_score": 0.85,
            "estimated_mttr": "30 minutes",
            "knowledge_base_results": [
                {
                    "title": "Database Connection Guide",
                    "content": "How to troubleshoot database connections...",
                    "snippet": "Connection troubleshooting involves..."
                }
            ],
            "tool_results": [
                {
                    "tool_name": "web_search",
                    "source": "Stack Overflow",
                    "content": "Common database timeout solutions..."
                }
            ]
        }


class TestProcessQueryV3:
    """Test the main process_query method with v3.1.0 schema."""
    
    @pytest.mark.asyncio
    async def test_process_query_standard_answer(self, agent_service, sample_query_request, mock_agent_result):
        """Test process_query returns ANSWER response for standard queries."""
        # Mock agent execution
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            
            # Execute
            response = await agent_service.process_query(sample_query_request)
            
            # Verify response structure
            assert isinstance(response, AgentResponse)
            assert response.schema_version == "3.1.0"
            assert response.response_type == ResponseType.ANSWER
            assert response.view_state.session_id == sample_query_request.session_id
            assert response.view_state.case_id is not None
            assert len(response.sources) > 0
            assert response.plan is None
            
            # Verify content includes findings
            assert "Database connection timeout" in response.content
            assert "Network latency" in response.content
    
    @pytest.mark.asyncio
    async def test_process_query_plan_proposal(self, agent_service, sample_query_request):
        """Test process_query returns PLAN_PROPOSAL response for multi-step solutions."""
        # Mock agent result with multiple next steps
        multi_step_result = {
            "findings": [{"message": "Complex issue detected"}],
            "recommendations": ["Multi-step approach needed"],
            "next_steps": [
                "Step 1: Analyze logs",
                "Step 2: Check configuration", 
                "Step 3: Restart service",
                "Step 4: Monitor results"
            ],
            "root_cause": "Configuration drift",
            "confidence_score": 0.9
        }
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=multi_step_result)
            
            # Execute
            response = await agent_service.process_query(sample_query_request)
            
            # Verify plan proposal response
            assert response.response_type == ResponseType.PLAN_PROPOSAL
            assert response.plan is not None
            assert len(response.plan) == 4
            assert response.plan[0].description == "Step 1: Analyze logs"
            assert response.plan[3].description == "Step 4: Monitor results"
    
    @pytest.mark.asyncio
    async def test_process_query_clarification_request(self, agent_service, sample_query_request):
        """Test process_query returns CLARIFICATION_REQUEST when agent needs more info."""
        # Mock agent result indicating need for clarification
        clarification_result = {
            "findings": [],
            "recommendations": [
                "Need to clarify which database system",
                "Please specify the error message"
            ],
            "next_steps": ["Gather more information"],
            "root_cause": "Insufficient information to diagnose",
            "confidence_score": 0.3
        }
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=clarification_result)
            
            # Execute
            response = await agent_service.process_query(sample_query_request)
            
            # Verify clarification request response
            assert response.response_type == ResponseType.CLARIFICATION_REQUEST
            assert "clarify" in response.content.lower()
            assert response.plan is None
    
    @pytest.mark.asyncio
    async def test_process_query_confirmation_request(self, agent_service, sample_query_request):
        """Test process_query returns CONFIRMATION_REQUEST when action needs approval."""
        # Mock agent result indicating need for confirmation
        confirmation_result = {
            "findings": [{"message": "Critical system detected"}],
            "recommendations": [
                "Confirm before proceeding with database restart",
                "Verify maintenance window"
            ],
            "next_steps": ["Await user confirmation"],
            "root_cause": "Service restart required",
            "confidence_score": 0.95
        }
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=confirmation_result)
            
            # Execute
            response = await agent_service.process_query(sample_query_request)
            
            # Verify confirmation request response
            assert response.response_type == ResponseType.CONFIRMATION_REQUEST
            assert "confirm" in response.content.lower()
            assert response.plan is None
    
    @pytest.mark.asyncio
    async def test_process_query_case_id_generation(self, agent_service, sample_query_request, mock_agent_result):
        """Test that each query generates a unique case_id."""
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            
            # Execute multiple queries
            response1 = await agent_service.process_query(sample_query_request)
            response2 = await agent_service.process_query(sample_query_request)
            
            # Verify unique case IDs
            assert response1.view_state.case_id != response2.view_state.case_id
            
            # Verify case IDs are valid UUIDs
            uuid.UUID(response1.view_state.case_id)  # Should not raise
            uuid.UUID(response2.view_state.case_id)  # Should not raise
    
    @pytest.mark.asyncio
    async def test_process_query_sanitization(self, agent_service, mock_sanitizer, mock_agent_result):
        """Test that input and output are properly sanitized."""
        query_request = QueryRequest(
            session_id="test-session",
            query="Query with PII: john.doe@example.com"
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            
            # Execute
            response = await agent_service.process_query(query_request)
            
            # Verify sanitization was called
            mock_sanitizer.sanitize.assert_called()
            
            # Verify content is sanitized
            assert response.content.startswith("sanitized_")
    
    @pytest.mark.asyncio
    async def test_process_query_session_recording(self, agent_service, sample_query_request, mock_session_service, mock_agent_result):
        """Test that query operations are recorded in session."""
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            
            # Execute
            response = await agent_service.process_query(sample_query_request)
            
            # Verify session recording was called
            mock_session_service.record_query_operation.assert_called_once()
            
            # Verify the call arguments
            call_args = mock_session_service.record_query_operation.call_args
            assert call_args[1]["session_id"] == sample_query_request.session_id
            assert call_args[1]["query"] == sample_query_request.query
            assert call_args[1]["investigation_id"] == response.view_state.case_id


class TestValidationMethods:
    """Test validation methods."""
    
    @pytest.mark.asyncio
    async def test_validate_request_valid(self, agent_service, sample_query_request):
        """Test _validate_request with valid input."""
        # Should not raise any exceptions
        await agent_service._validate_request(sample_query_request)
    
    @pytest.mark.asyncio
    async def test_validate_request_empty_query(self, agent_service):
        """Test _validate_request with empty query."""
        invalid_request = QueryRequest(session_id="test-session", query="")
        
        with pytest.raises(ValidationException) as exc_info:
            await agent_service._validate_request(invalid_request)
        
        assert "Query cannot be empty" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_validate_request_empty_session_id(self, agent_service):
        """Test _validate_request with empty session_id."""
        invalid_request = QueryRequest(session_id="", query="test query")
        
        with pytest.raises(ValidationException) as exc_info:
            await agent_service._validate_request(invalid_request)
        
        assert "Session ID cannot be empty" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_validate_request_session_not_found(self, agent_service, mock_session_service):
        """Test _validate_request when session does not exist."""
        mock_session_service.get_session.return_value = None
        
        invalid_request = QueryRequest(session_id="nonexistent", query="test query")
        
        with pytest.raises(FileNotFoundError) as exc_info:
            await agent_service._validate_request(invalid_request)
        
        assert "Session nonexistent not found" in str(exc_info.value)


class TestResponseTypeLogic:
    """Test response type determination logic."""
    
    def test_determine_response_type_answer(self, agent_service):
        """Test _determine_response_type returns ANSWER for standard results."""
        result = {
            "findings": [{"message": "Standard finding"}],
            "recommendations": ["Standard recommendation"],
            "next_steps": ["Single step"]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.ANSWER
    
    def test_determine_response_type_clarification(self, agent_service):
        """Test _determine_response_type returns CLARIFICATION_REQUEST."""
        result = {
            "findings": [],
            "recommendations": ["Need to clarify the database type"],
            "next_steps": []
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CLARIFICATION_REQUEST
    
    def test_determine_response_type_confirmation(self, agent_service):
        """Test _determine_response_type returns CONFIRMATION_REQUEST."""
        result = {
            "findings": [{"message": "Critical issue"}],
            "recommendations": ["Confirm before proceeding with restart"],
            "next_steps": ["Await confirmation"]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.CONFIRMATION_REQUEST
    
    def test_determine_response_type_plan(self, agent_service):
        """Test _determine_response_type returns PLAN_PROPOSAL for multi-step solutions."""
        result = {
            "findings": [{"message": "Complex issue"}],
            "recommendations": ["Multi-step approach"],
            "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"]
        }
        
        response_type = agent_service._determine_response_type(result)
        assert response_type == ResponseType.PLAN_PROPOSAL
    
    def test_needs_clarification_keywords(self, agent_service):
        """Test _needs_clarification detects clarification keywords."""
        # Test with clarification keywords
        result = {"recommendations": ["Please clarify which database"]}
        assert agent_service._needs_clarification(result) is True
        
        result = {"findings": [{"message": "This is unclear, need more information"}]}
        assert agent_service._needs_clarification(result) is True
        
        # Test without clarification keywords
        result = {"recommendations": ["Restart the service"]}
        assert agent_service._needs_clarification(result) is False
    
    def test_needs_confirmation_keywords(self, agent_service):
        """Test _needs_confirmation detects confirmation keywords."""
        # Test with confirmation keywords
        result = {"recommendations": ["Confirm before proceeding"]}
        assert agent_service._needs_confirmation(result) is True
        
        result = {"recommendations": ["Please verify this action"]}
        assert agent_service._needs_confirmation(result) is True
        
        # Test without confirmation keywords
        result = {"recommendations": ["Check the logs"]}
        assert agent_service._needs_confirmation(result) is False
    
    def test_has_plan_multi_step(self, agent_service):
        """Test _has_plan detects multi-step plans."""
        # Test with multi-step plan (> 2 steps)
        result = {"next_steps": ["Step 1", "Step 2", "Step 3"]}
        assert agent_service._has_plan(result) is True
        
        # Test with single step
        result = {"next_steps": ["Single step"]}
        assert agent_service._has_plan(result) is False
        
        # Test with two steps
        result = {"next_steps": ["Step 1", "Step 2"]}
        assert agent_service._has_plan(result) is False
        
        # Test with no steps
        result = {"next_steps": []}
        assert agent_service._has_plan(result) is False


class TestSourceExtraction:
    """Test source extraction from agent results."""
    
    @pytest.mark.asyncio
    async def test_extract_sources_knowledge_base(self, agent_service):
        """Test _extract_sources from knowledge base results."""
        agent_result = {
            "knowledge_base_results": [
                {
                    "title": "Database Troubleshooting Guide",
                    "content": "This guide covers database troubleshooting...",
                    "snippet": "Database connections can fail due to..."
                },
                {
                    "title": "Network Configuration",
                    "snippet": "Network latency can cause timeouts..."
                }
            ]
        }
        
        sources = await agent_service._extract_sources(agent_result)
        
        assert len(sources) == 2
        assert sources[0].type == SourceType.KNOWLEDGE_BASE
        assert sources[0].name == "Database Troubleshooting Guide"
        assert "Database connections" in sources[0].snippet
        
        assert sources[1].type == SourceType.KNOWLEDGE_BASE
        assert sources[1].name == "Network Configuration"
    
    @pytest.mark.asyncio
    async def test_extract_sources_web_search(self, agent_service):
        """Test _extract_sources from web search results."""
        agent_result = {
            "tool_results": [
                {
                    "tool_name": "web_search_tool",
                    "source": "Stack Overflow",
                    "content": "Common solutions for database timeouts include..."
                }
            ]
        }
        
        sources = await agent_service._extract_sources(agent_result)
        
        assert len(sources) == 1
        assert sources[0].type == SourceType.WEB_SEARCH
        assert sources[0].name == "Stack Overflow"
        assert "Common solutions" in sources[0].snippet
    
    @pytest.mark.asyncio
    async def test_extract_sources_log_files(self, agent_service):
        """Test _extract_sources from log file results."""
        agent_result = {
            "tool_results": [
                {
                    "tool_name": "log_analyzer",
                    "filename": "application.log",
                    "content": "2024-01-01 ERROR: Connection timeout after 30s..."
                }
            ]
        }
        
        sources = await agent_service._extract_sources(agent_result)
        
        assert len(sources) == 1
        assert sources[0].type == SourceType.LOG_FILE
        assert sources[0].name == "application.log"
        assert "Connection timeout" in sources[0].snippet
    
    @pytest.mark.asyncio
    async def test_extract_sources_mixed(self, agent_service):
        """Test _extract_sources with mixed source types."""
        agent_result = {
            "knowledge_base_results": [
                {"title": "KB Document", "snippet": "KB content"}
            ],
            "tool_results": [
                {
                    "tool_name": "web_search",
                    "source": "Documentation",
                    "content": "Web content"
                },
                {
                    "tool_name": "log_parser", 
                    "filename": "error.log",
                    "content": "Log content"
                }
            ]
        }
        
        sources = await agent_service._extract_sources(agent_result)
        
        assert len(sources) == 3
        
        # Check knowledge base source
        kb_source = next(s for s in sources if s.type == SourceType.KNOWLEDGE_BASE)
        assert kb_source.name == "KB Document"
        
        # Check web search source
        web_source = next(s for s in sources if s.type == SourceType.WEB_SEARCH)
        assert web_source.name == "Documentation"
        
        # Check log file source
        log_source = next(s for s in sources if s.type == SourceType.LOG_FILE)
        assert log_source.name == "error.log"
    
    @pytest.mark.asyncio
    async def test_extract_sources_limit(self, agent_service):
        """Test _extract_sources limits to 10 sources."""
        # Create 15 knowledge base results
        kb_results = [
            {"title": f"Document {i}", "snippet": f"Content {i}"}
            for i in range(15)
        ]
        
        agent_result = {"knowledge_base_results": kb_results}
        
        sources = await agent_service._extract_sources(agent_result)
        
        # Should be limited to 10
        assert len(sources) == 10


class TestViewStateCreation:
    """Test ViewState creation."""
    
    @pytest.mark.asyncio
    async def test_create_view_state_basic(self, agent_service):
        """Test _create_view_state with basic information."""
        case_id = str(uuid.uuid4())
        session_id = "test-session-123"
        
        view_state = await agent_service._create_view_state(case_id, session_id)
        
        assert view_state.case_id == case_id
        assert view_state.session_id == session_id
        assert case_id[:8] in view_state.running_summary
        assert isinstance(view_state.uploaded_data, list)
    
    @pytest.mark.asyncio
    async def test_create_view_state_with_uploads(self, agent_service, mock_session_service):
        """Test _create_view_state includes uploaded data from session."""
        case_id = str(uuid.uuid4())
        session_id = "test-session-123"
        
        # Mock session with uploads
        mock_session = Mock()
        mock_session.data_uploads = ["upload1", "upload2", "upload3"]
        mock_session_service.get_session.return_value = mock_session
        
        view_state = await agent_service._create_view_state(case_id, session_id)
        
        assert len(view_state.uploaded_data) == 3
        assert view_state.uploaded_data[0].id == "upload1"
        assert view_state.uploaded_data[0].name == "data_upload1"
        assert view_state.uploaded_data[0].type == "unknown"
    
    @pytest.mark.asyncio
    async def test_create_view_state_session_error(self, agent_service, mock_session_service):
        """Test _create_view_state handles session service errors gracefully."""
        mock_session_service.get_session.side_effect = Exception("Session service error")
        
        case_id = str(uuid.uuid4())
        session_id = "test-session-123"
        
        # Should not raise, but handle gracefully
        view_state = await agent_service._create_view_state(case_id, session_id)
        
        assert view_state.case_id == case_id
        assert view_state.session_id == session_id
        assert view_state.uploaded_data == []


class TestContentGeneration:
    """Test content generation from agent results."""
    
    def test_generate_content_with_root_cause(self, agent_service):
        """Test _generate_content includes root cause."""
        agent_result = {
            "root_cause": "Database connection pool exhausted",
            "findings": [{"message": "High connection count detected"}],
            "recommendations": ["Increase pool size"]
        }
        
        content = agent_service._generate_content(agent_result, "test query")
        
        assert "Root Cause: Database connection pool exhausted" in content
        assert "High connection count detected" in content
        assert "Increase pool size" in content
    
    def test_generate_content_findings_only(self, agent_service):
        """Test _generate_content with findings only."""
        agent_result = {
            "findings": [
                {"message": "Error rate increased"},
                {"description": "Memory usage high"},
                "Simple finding string"
            ]
        }
        
        content = agent_service._generate_content(agent_result, "test query")
        
        assert "Key Findings:" in content
        assert "Error rate increased" in content
        assert "Memory usage high" in content
        assert "Simple finding string" in content
    
    def test_generate_content_recommendations_only(self, agent_service):
        """Test _generate_content with recommendations only."""
        agent_result = {
            "recommendations": [
                "Restart the application",
                "Check system resources",
                "Review configuration"
            ]
        }
        
        content = agent_service._generate_content(agent_result, "test query")
        
        assert "Recommendations:" in content
        assert "Restart the application" in content
        assert "Check system resources" in content
        assert "Review configuration" in content
    
    def test_generate_content_limits_items(self, agent_service):
        """Test _generate_content limits findings and recommendations to 3 each."""
        agent_result = {
            "findings": [{"message": f"Finding {i}"} for i in range(5)],
            "recommendations": [f"Recommendation {i}" for i in range(6)]
        }
        
        content = agent_service._generate_content(agent_result, "test query")
        
        # Should only include first 3 findings
        assert "Finding 0" in content
        assert "Finding 1" in content
        assert "Finding 2" in content
        assert "Finding 3" not in content
        assert "Finding 4" not in content
        
        # Should only include first 3 recommendations
        assert "Recommendation 0" in content
        assert "Recommendation 1" in content
        assert "Recommendation 2" in content
        assert "Recommendation 3" not in content
    
    def test_generate_content_empty_result(self, agent_service):
        """Test _generate_content with empty agent result."""
        agent_result = {}
        query = "What's wrong with the system?"
        
        content = agent_service._generate_content(agent_result, query)
        
        assert query in content
        assert "analyzed your query" in content


class TestPlanExtraction:
    """Test plan step extraction."""
    
    def test_extract_plan_steps_strings(self, agent_service):
        """Test _extract_plan_steps with string steps."""
        agent_result = {
            "next_steps": [
                "Check database connectivity",
                "Review connection pool settings", 
                "Restart database service",
                "Monitor performance"
            ]
        }
        
        plan = agent_service._extract_plan_steps(agent_result)
        
        assert len(plan) == 4
        assert all(isinstance(step, PlanStep) for step in plan)
        assert plan[0].description == "Check database connectivity"
        assert plan[3].description == "Monitor performance"
    
    def test_extract_plan_steps_dicts(self, agent_service):
        """Test _extract_plan_steps with dictionary steps."""
        agent_result = {
            "next_steps": [
                {"description": "First step description"},
                {"step": "Second step using 'step' key"},
                {"other_key": "Third step with other key"}
            ]
        }
        
        plan = agent_service._extract_plan_steps(agent_result)
        
        assert len(plan) == 3
        assert plan[0].description == "First step description"
        assert plan[1].description == "Second step using 'step' key"
        assert "Third step" in plan[2].description
    
    def test_extract_plan_steps_mixed(self, agent_service):
        """Test _extract_plan_steps with mixed types."""
        agent_result = {
            "next_steps": [
                "String step",
                {"description": "Dict step"},
                123  # Non-string, non-dict
            ]
        }
        
        plan = agent_service._extract_plan_steps(agent_result)
        
        assert len(plan) == 3
        assert plan[0].description == "String step"
        assert plan[1].description == "Dict step"
        assert plan[2].description == "123"
    
    def test_extract_plan_steps_empty(self, agent_service):
        """Test _extract_plan_steps with empty steps."""
        agent_result = {"next_steps": []}
        
        plan = agent_service._extract_plan_steps(agent_result)
        
        assert len(plan) == 0
        assert plan == []


class TestErrorHandling:
    """Test error handling scenarios."""
    
    @pytest.mark.asyncio
    async def test_process_query_agent_failure(self, agent_service, sample_query_request):
        """Test process_query handles agent execution failures."""
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(side_effect=Exception("Agent execution failed"))
            
            # Should propagate the exception
            with pytest.raises(Exception) as exc_info:
                await agent_service.process_query(sample_query_request)
            
            assert "Agent execution failed" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_process_query_none_result(self, agent_service, sample_query_request):
        """Test process_query handles None agent result gracefully."""
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=None)
            
            # Should handle None result
            response = await agent_service.process_query(sample_query_request)
            
            assert response.response_type == ResponseType.ANSWER
            assert "Processing error occurred" in response.content
    
    @pytest.mark.asyncio 
    async def test_session_recording_failure(self, agent_service, sample_query_request, mock_session_service, mock_agent_result):
        """Test that session recording failures don't break the flow."""
        mock_session_service.record_query_operation.side_effect = Exception("Session recording failed")
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            
            # Should complete successfully despite session recording failure
            response = await agent_service.process_query(sample_query_request)
            
            assert isinstance(response, AgentResponse)
            assert response.response_type == ResponseType.ANSWER


class TestUtilityMethods:
    """Test utility methods."""
    
    def test_safe_float_valid(self, agent_service):
        """Test _safe_float with valid inputs."""
        assert agent_service._safe_float(3.14) == 3.14
        assert agent_service._safe_float("2.5") == 2.5
        assert agent_service._safe_float(42) == 42.0
    
    def test_safe_float_invalid(self, agent_service):
        """Test _safe_float with invalid inputs."""
        assert agent_service._safe_float(None) == 0.0
        assert agent_service._safe_float("not a number") == 0.0
        assert agent_service._safe_float([]) == 0.0
        assert agent_service._safe_float({}) == 0.0
    
    def test_safe_float_custom_default(self, agent_service):
        """Test _safe_float with custom default."""
        assert agent_service._safe_float(None, default=5.0) == 5.0
        assert agent_service._safe_float("invalid", default=-1.0) == -1.0


class TestAsyncOperations:
    """Test async operation patterns."""
    
    @pytest.mark.asyncio
    async def test_concurrent_queries(self, agent_service, mock_agent_result):
        """Test handling of concurrent query processing."""
        import asyncio
        
        query1 = QueryRequest(session_id="session-1", query="Query 1")
        query2 = QueryRequest(session_id="session-2", query="Query 2")
        query3 = QueryRequest(session_id="session-3", query="Query 3")
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = AsyncMock(return_value=mock_agent_result)
            
            # Execute queries concurrently
            results = await asyncio.gather(
                agent_service.process_query(query1),
                agent_service.process_query(query2),
                agent_service.process_query(query3)
            )
            
            # Verify all queries completed successfully
            assert len(results) == 3
            assert all(isinstance(r, AgentResponse) for r in results)
            
            # Verify unique case IDs
            case_ids = [r.view_state.case_id for r in results]
            assert len(set(case_ids)) == 3  # All unique
    
    @pytest.mark.asyncio
    async def test_timeout_handling(self, agent_service, sample_query_request):
        """Test query processing with timeouts."""
        import asyncio
        
        async def slow_agent_run(*args, **kwargs):
            await asyncio.sleep(2)  # Simulate slow processing
            return {"findings": [], "recommendations": []}
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            mock_agent.run = slow_agent_run
            
            # Should complete (no timeout in this test, but demonstrates pattern)
            response = await agent_service.process_query(sample_query_request)
            assert isinstance(response, AgentResponse)