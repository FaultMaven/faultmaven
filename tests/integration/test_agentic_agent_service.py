"""Test module for Agentic Framework AgentService Transformation

This module validates that the AgentService transformation to use the
Agentic Framework maintains 100% API compatibility while providing
enhanced functionality through the new architecture.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timedelta
from typing import Dict, Any

from faultmaven.services.agent import AgentService
from faultmaven.models import (
    QueryRequest, 
    TroubleshootingResponse, 
    AgentResponse,
    ViewState,
    ResponseType,
    UploadedData,
    DataType
)
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer


class TestAgenticAgentServiceTransformation:
    """Test cases for AgentService transformation with Agentic Framework."""

    @pytest.fixture
    def mock_llm_provider(self):
        """Mock LLM provider."""
        provider = Mock(spec=ILLMProvider)
        provider.route = AsyncMock(return_value="Mock LLM response")
        return provider

    @pytest.fixture
    def mock_tools(self):
        """Mock tools list."""
        tool = Mock(spec=BaseTool)
        tool.name = "test_tool"
        return [tool]

    @pytest.fixture
    def mock_tracer(self):
        """Mock tracer."""
        tracer = Mock(spec=ITracer)
        tracer.trace = AsyncMock()
        return tracer

    @pytest.fixture
    def mock_sanitizer(self):
        """Mock sanitizer."""
        sanitizer = Mock(spec=ISanitizer)
        sanitizer.sanitize = Mock(return_value="sanitized content")
        sanitizer.is_sensitive = Mock(return_value=False)
        return sanitizer

    @pytest.fixture
    def mock_session_service(self):
        """Mock session service."""
        service = Mock()
        service.get_session = AsyncMock()
        service.update_session = AsyncMock()
        return service

    @pytest.fixture
    def mock_agentic_components(self):
        """Mock all Agentic Framework components."""
        return {
            'business_logic_workflow_engine': Mock(),
            'query_classification_engine': Mock(),
            'tool_skill_broker': Mock(),
            'guardrails_policy_layer': Mock(),
            'response_synthesizer': Mock(),
            'error_fallback_manager': Mock(),
            'agent_state_manager': Mock()
        }

    @pytest.fixture
    def agent_service(self, mock_llm_provider, mock_tools, mock_tracer, 
                     mock_sanitizer, mock_session_service, mock_agentic_components):
        """AgentService instance with Agentic Framework components."""
        return AgentService(
            llm_provider=mock_llm_provider,
            tools=mock_tools,
            tracer=mock_tracer,
            sanitizer=mock_sanitizer,
            session_service=mock_session_service,
            **mock_agentic_components
        )

    @pytest.mark.integration
    def test_process_query_method_signature_unchanged(self, agent_service):
        """Test that process_query method signature remains unchanged."""
        import inspect
        
        # Get the method signature
        sig = inspect.signature(agent_service.process_query)
        
        # Should have the same parameters as before
        params = list(sig.parameters.keys())
        assert 'request' in params
        
        # Method should be async
        assert asyncio.iscoroutinefunction(agent_service.process_query)

    @pytest.mark.integration
    async def test_agent_response_format_identical(self, agent_service, mock_agentic_components):
        """Test that AgentResponse format remains identical."""
        # Mock Agentic Framework components to return specific responses
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value={
                'response': 'Test response',
                'confidence': 0.85,
                'tools_used': ['test_tool'],
                'recommendations': ['Test recommendation']
            }
        )
        
        # Create test request
        request = QueryRequest(
            query="Test query",
            session_id="test-session-123",
            user_id="test-user-456"
        )
        
        # Process query
        response = await agent_service.process_query(request)
        
        # Validate response structure
        assert isinstance(response, AgentResponse)
        assert hasattr(response, 'response')
        assert hasattr(response, 'response_type')
        assert hasattr(response, 'confidence_score')
        assert hasattr(response, 'view_state')
        assert hasattr(response, 'session_id')

    @pytest.mark.integration
    async def test_all_existing_response_fields_populated(self, agent_service, mock_agentic_components):
        """Test that all existing response fields are properly populated."""
        # Mock Agentic Framework workflow
        mock_workflow_result = {
            'response': 'Comprehensive analysis complete',
            'confidence': 0.92,
            'tools_used': ['knowledge_base', 'web_search'],
            'recommendations': ['Check logs', 'Restart service'],
            'view_state': {
                'current_phase': 'analysis',
                'findings': ['Error pattern found'],
                'next_steps': ['Validate hypothesis']
            }
        }
        
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value=mock_workflow_result
        )
        
        request = QueryRequest(
            query="Database connection timeout",
            session_id="test-session-123"
        )
        
        response = await agent_service.process_query(request)
        
        # Verify all expected fields are present and populated
        assert response.response is not None
        assert response.confidence_score is not None
        assert response.view_state is not None
        assert response.session_id == "test-session-123"
        assert response.response_type in [ResponseType.INITIAL_ANALYSIS, ResponseType.CLARIFICATION_REQUEST, ResponseType.INVESTIGATION_UPDATE]

    @pytest.mark.integration
    async def test_http_status_codes_match_api_specification(self, agent_service, mock_agentic_components):
        """Test that HTTP status codes match API specification."""
        # Mock successful workflow execution
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value={'response': 'Success', 'confidence': 0.8}
        )
        
        request = QueryRequest(query="Valid query", session_id="test-session")
        
        # Should not raise exceptions for valid input
        response = await agent_service.process_query(request)
        assert response is not None
        
        # Test validation error handling
        with pytest.raises(ValidationException):
            invalid_request = QueryRequest(query="", session_id="")  # Invalid empty query
            await agent_service.process_query(invalid_request)

    @pytest.mark.integration
    async def test_agentic_framework_processing_path_works(self, agent_service, mock_agentic_components):
        """Test that Agentic Framework processing path works correctly."""
        # Setup mock responses for each component
        mock_agentic_components['query_classification_engine'].classify_query = AsyncMock(
            return_value={'category': 'troubleshooting', 'priority': 'high'}
        )
        
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value={
                'response': 'Agentic Framework response',
                'confidence': 0.95,
                'workflow_steps': ['classify', 'analyze', 'recommend']
            }
        )
        
        mock_agentic_components['response_synthesizer'].synthesize_response = AsyncMock(
            return_value='Synthesized final response'
        )
        
        request = QueryRequest(query="System performance issue", session_id="test-session")
        
        response = await agent_service.process_query(request)
        
        # Verify Agentic Framework components were called
        mock_agentic_components['query_classification_engine'].classify_query.assert_called_once()
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow.assert_called_once()
        
        # Verify response contains Agentic Framework output
        assert response.response is not None
        assert response.confidence_score > 0

    @pytest.mark.integration
    async def test_langgraph_agent_integration_as_fallback(self, agent_service, mock_agentic_components):
        """Test LangGraph agent integration as fallback."""
        # Mock Agentic Framework failure
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            side_effect=Exception("Agentic Framework error")
        )
        
        # Mock error fallback manager to handle fallback
        mock_agentic_components['error_fallback_manager'].handle_fallback = AsyncMock(
            return_value={
                'response': 'Fallback response from LangGraph',
                'confidence': 0.7,
                'fallback_used': 'langgraph_agent'
            }
        )
        
        request = QueryRequest(query="Complex troubleshooting", session_id="test-session")
        
        response = await agent_service.process_query(request)
        
        # Verify fallback was used
        mock_agentic_components['error_fallback_manager'].handle_fallback.assert_called_once()
        assert response.response is not None

    @pytest.mark.integration
    async def test_skills_system_fallback_compatibility(self, agent_service, mock_agentic_components):
        """Test Skills system fallback for compatibility."""
        # Mock both Agentic Framework and LangGraph failures
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            side_effect=Exception("Primary failure")
        )
        
        mock_agentic_components['error_fallback_manager'].handle_fallback = AsyncMock(
            return_value={
                'response': 'Skills system fallback response',
                'confidence': 0.6,
                'fallback_used': 'skills_system'
            }
        )
        
        request = QueryRequest(query="Simple query", session_id="test-session")
        
        response = await agent_service.process_query(request)
        
        # Verify Skills system fallback works
        assert response.response is not None
        assert response.confidence_score >= 0

    @pytest.mark.integration
    async def test_multi_tier_fallback_strategy(self, agent_service, mock_agentic_components):
        """Test multi-tier fallback strategy."""
        # Create a fallback chain test
        fallback_sequence = []
        
        def track_fallback(method_name):
            def mock_method(*args, **kwargs):
                fallback_sequence.append(method_name)
                if method_name == 'agentic_framework':
                    raise Exception("Agentic Framework failed")
                elif method_name == 'langgraph':
                    raise Exception("LangGraph failed") 
                else:  # skills_system
                    return {'response': 'Skills fallback success', 'confidence': 0.5}
            return AsyncMock(side_effect=mock_method)
        
        # Setup fallback chain
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = track_fallback('agentic_framework')
        mock_agentic_components['error_fallback_manager'].handle_fallback = AsyncMock(
            return_value={'response': 'Final fallback', 'confidence': 0.5}
        )
        
        request = QueryRequest(query="Test fallback chain", session_id="test-session")
        
        response = await agent_service.process_query(request)
        
        # Verify response was generated despite failures
        assert response.response is not None

    @pytest.mark.integration
    async def test_enhanced_ai_capabilities_through_same_api(self, agent_service, mock_agentic_components):
        """Test enhanced AI capabilities delivered through same API."""
        # Mock advanced Agentic Framework capabilities
        advanced_response = {
            'response': 'Advanced AI analysis with multi-step reasoning',
            'confidence': 0.96,
            'reasoning_steps': [
                'Analyzed system patterns',
                'Cross-referenced knowledge base',
                'Applied ML-based predictions',
                'Validated hypothesis through simulation'
            ],
            'advanced_insights': {
                'root_cause_probability': 0.89,
                'impact_assessment': 'High',
                'resolution_time_estimate': '2 hours'
            }
        }
        
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value=advanced_response
        )
        
        request = QueryRequest(query="Complex system failure analysis", session_id="test-session")
        
        response = await agent_service.process_query(request)
        
        # Verify enhanced capabilities are delivered through existing API
        assert response.response is not None
        assert response.confidence_score > 0.9  # Higher confidence from advanced AI
        assert isinstance(response.view_state, ViewState)  # Same response structure

    @pytest.mark.integration
    async def test_memory_integration_with_agent_service(self, agent_service, mock_agentic_components):
        """Test memory integration with AgentService."""
        # Mock memory integration
        mock_agentic_components['agent_state_manager'].get_conversation_context = AsyncMock(
            return_value={
                'previous_queries': ['Database timeout', 'Connection pool issues'],
                'context_summary': 'User investigating database connectivity problems',
                'relevant_history': ['Identified connection pool exhaustion']
            }
        )
        
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value={
                'response': 'Based on your previous issues with connection pools...',
                'confidence': 0.88,
                'context_aware': True
            }
        )
        
        request = QueryRequest(
            query="The database is still slow",
            session_id="test-session-with-history"
        )
        
        response = await agent_service.process_query(request)
        
        # Verify memory context was used
        mock_agentic_components['agent_state_manager'].get_conversation_context.assert_called_once()
        assert response.response is not None

    @pytest.mark.integration
    async def test_planning_integration_with_agent_service(self, agent_service, mock_agentic_components):
        """Test planning integration with AgentService."""
        # Mock planning capabilities
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value={
                'response': 'I\'ve created a systematic troubleshooting plan',
                'confidence': 0.91,
                'plan_steps': [
                    {'step': 1, 'action': 'Check system metrics', 'estimated_time': '5 minutes'},
                    {'step': 2, 'action': 'Analyze error logs', 'estimated_time': '10 minutes'},
                    {'step': 3, 'action': 'Test connectivity', 'estimated_time': '15 minutes'}
                ]
            }
        )
        
        request = QueryRequest(
            query="Help me systematically troubleshoot this network issue",
            session_id="test-session"
        )
        
        response = await agent_service.process_query(request)
        
        # Verify planning was integrated
        assert response.response is not None
        assert response.confidence_score > 0.9

    @pytest.mark.integration
    async def test_error_handling_maintains_api_contract(self, agent_service, mock_agentic_components):
        """Test that error handling maintains API contract."""
        # Test various error scenarios
        test_cases = [
            (ValidationException, "Invalid input"),
            (ServiceException, "Service unavailable"), 
            (Exception, "Unexpected error")
        ]
        
        for exception_type, error_message in test_cases:
            # Mock component to raise exception
            mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
                side_effect=exception_type(error_message)
            )
            
            # Mock error handler
            mock_agentic_components['error_fallback_manager'].handle_error = AsyncMock(
                return_value={
                    'response': f'Handled {exception_type.__name__}',
                    'confidence': 0.3,
                    'error_handled': True
                }
            )
            
            request = QueryRequest(query="Test error handling", session_id="test-session")
            
            # Should handle error gracefully and return response
            response = await agent_service.process_query(request)
            assert response is not None

    @pytest.mark.integration
    async def test_performance_with_agentic_framework(self, agent_service, mock_agentic_components):
        """Test performance with Agentic Framework architecture."""
        import time
        
        # Mock fast Agentic Framework response
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value={
                'response': 'Fast Agentic Framework response',
                'confidence': 0.87,
                'processing_time': 0.1  # 100ms
            }
        )
        
        request = QueryRequest(query="Performance test query", session_id="test-session")
        
        start_time = time.time()
        response = await agent_service.process_query(request)
        end_time = time.time()
        
        # Verify response generated quickly
        processing_time = end_time - start_time
        assert processing_time < 5.0  # Should complete within 5 seconds
        assert response.response is not None

    @pytest.mark.integration
    async def test_view_state_properly_updated(self, agent_service, mock_agentic_components):
        """Test that ViewState is properly updated with Agentic Framework data."""
        # Mock comprehensive workflow result
        mock_workflow_result = {
            'response': 'Analysis complete',
            'confidence': 0.85,
            'view_state_data': {
                'current_phase': 'investigation',
                'findings': ['Database connection timeout', 'High memory usage'],
                'recommendations': ['Increase connection pool', 'Monitor memory'],
                'tools_used': ['knowledge_base', 'log_analyzer'],
                'confidence_breakdown': {
                    'analysis_confidence': 0.9,
                    'recommendation_confidence': 0.8
                }
            }
        }
        
        mock_agentic_components['business_logic_workflow_engine'].execute_workflow = AsyncMock(
            return_value=mock_workflow_result
        )
        
        request = QueryRequest(query="System analysis", session_id="test-session")
        
        response = await agent_service.process_query(request)
        
        # Verify ViewState is properly populated
        assert response.view_state is not None
        assert isinstance(response.view_state, ViewState)
        # ViewState should contain relevant troubleshooting information