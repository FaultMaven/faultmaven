"""Integration test for QueryClassificationEngine in AgentService

This test validates the first Agentic Framework replacement:
- QueryClassificationEngine replaces Skills-based query processing
- Proper fallback to Skills system when Agentic Framework unavailable
- API compatibility maintained
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from faultmaven.services.agentic.orchestration.agent_service import AgentService
from faultmaven.models import QueryRequest, ResponseType
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer

# Import the agentic classes to create proper mocks
from faultmaven.services.agentic.engines.workflow_engine import BusinessLogicWorkflowEngine
from faultmaven.services.agentic.engines.classification_engine import QueryClassificationEngine
from faultmaven.services.agentic.engines.response_synthesizer import ResponseSynthesizer
from faultmaven.services.agentic.management.tool_broker import ToolSkillBroker
from faultmaven.services.agentic.management.state_manager import AgentStateManager
from faultmaven.services.agentic.safety.guardrails_layer import GuardrailsPolicyLayer
from faultmaven.services.agentic.safety.error_manager import ErrorFallbackManager


@pytest.fixture
def mock_dependencies():
    """Create mock dependencies for AgentService"""
    llm_provider = Mock(spec=ILLMProvider)
    tools = []
    tracer = Mock(spec=ITracer)
    tracer.trace.return_value.__enter__ = Mock()
    tracer.trace.return_value.__exit__ = Mock()
    sanitizer = Mock(spec=ISanitizer)
    sanitizer.sanitize.return_value = "test query"
    session_service = AsyncMock()
    session_service.get_or_create_current_case_id.return_value = "test-case-id"
    session_service.record_case_message.return_value = None
    session_service.get_conversation_context.return_value = ""
    session_service.update_case_query_count.return_value = None

    # Agentic framework components - use spec_set to make them pass isinstance checks
    business_logic_workflow_engine = Mock(spec=BusinessLogicWorkflowEngine)
    query_classification_engine = Mock(spec=QueryClassificationEngine)
    tool_skill_broker = Mock(spec=ToolSkillBroker)
    guardrails_policy_layer = Mock(spec=GuardrailsPolicyLayer)
    response_synthesizer = Mock(spec=ResponseSynthesizer)
    error_fallback_manager = Mock(spec=ErrorFallbackManager)
    agent_state_manager = Mock(spec=AgentStateManager)

    return {
        "llm_provider": llm_provider,
        "tools": tools,
        "tracer": tracer,
        "sanitizer": sanitizer,
        "session_service": session_service,
        "settings": Mock(),
        "business_logic_workflow_engine": business_logic_workflow_engine,
        "query_classification_engine": query_classification_engine,
        "tool_skill_broker": tool_skill_broker,
        "guardrails_policy_layer": guardrails_policy_layer,
        "response_synthesizer": response_synthesizer,
        "error_fallback_manager": error_fallback_manager,
        "agent_state_manager": agent_state_manager
    }


@pytest.fixture
def agent_service(mock_dependencies):
    """Create AgentService with mocked dependencies"""
    return AgentService(**mock_dependencies)


@pytest.fixture
def sample_query_request():
    """Sample QueryRequest for testing - avoid gateway early returns"""
    return QueryRequest(
        session_id="test-session",
        query="I'm having an issue with my database connections",  # Won't trigger performance pattern
        context={}
    )


class TestQueryClassificationIntegration:
    """Test QueryClassificationEngine integration in AgentService"""
    
    @pytest.mark.asyncio
    async def test_query_classification_success(self, agent_service, sample_query_request):
        """Test successful QueryClassificationEngine processing"""
        
        # Mock QueryClassificationEngine
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "performance_troubleshooting",
            "complexity": "medium",
            "urgency": "normal", 
            "domain": "infrastructure",
            "confidence": 0.9
        }
        
        # Mock container to return QueryClassificationEngine
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.confidence.score.return_value = 0.8
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            # Mock ViewState creation
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                # Execute query processing
                response = await agent_service.process_query(sample_query_request)
                
                # Verify QueryClassificationEngine was called
                mock_classification_engine.classify_query.assert_called_once()
                call_args = mock_classification_engine.classify_query.call_args
                assert call_args[1]["query"] == "test query"
                assert call_args[1]["context"]["session_id"] == "test-session"
                assert call_args[1]["context"]["case_id"] == "test-case-id"
                
                # Verify response structure maintained
                assert hasattr(response, 'content')
                assert hasattr(response, 'response_type')
                assert hasattr(response, 'session_id')
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_query_classification_fallback_to_skills(self, agent_service, sample_query_request):
        """Test fallback to Skills system when QueryClassificationEngine fails"""
        
        # Mock QueryClassificationEngine to raise exception
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.side_effect = Exception("Classification failed")
        
        # Mock Skills system
        mock_skill = AsyncMock()
        mock_skill.execute.return_value = {
            "confidence_delta": {"skill_confidence": 0.7},
            "evidence": ["test evidence"],
            "proposed_action": None
        }
        mock_skill.name = "test_skill"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.skill_registry.all.return_value = [mock_skill]
            mock_container.skill_router.select.return_value = [mock_skill]
            mock_container.confidence.score.return_value = 0.7
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify QueryClassificationEngine was attempted
                mock_classification_engine.classify_query.assert_called_once()
                
                # Verify Skills fallback was used
                mock_container.skill_registry.all.assert_called_once()
                mock_skill.execute.assert_called_once()
                
                # Verify response still valid
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio 
    async def test_query_classification_engine_unavailable(self, agent_service, sample_query_request):
        """Test behavior when QueryClassificationEngine is not available"""
        
        with patch('faultmaven.container.container') as mock_container:
            # Return None for QueryClassificationEngine (not available)
            mock_container.get_query_classification_engine.return_value = None
            mock_container.confidence.score.return_value = 0.5
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify QueryClassificationEngine getter was called
                mock_container.get_query_classification_engine.assert_called_once()
                
                # Verify response with fallback basic features
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_api_compatibility_maintained(self, agent_service, sample_query_request):
        """Test that API response format is unchanged"""
        
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "debugging", 
            "complexity": "high",
            "urgency": "critical",
            "domain": "database",
            "confidence": 0.95
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.confidence.score.return_value = 0.9
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify exact AgentResponse schema maintained
                assert hasattr(response, 'schema_version')
                assert hasattr(response, 'content')
                assert hasattr(response, 'response_type')
                assert isinstance(response.response_type, ResponseType)
                assert hasattr(response, 'session_id')
                assert hasattr(response, 'case_id')
                assert hasattr(response, 'confidence_score')
                assert hasattr(response, 'sources')
                assert hasattr(response, 'next_action_hint')
                assert hasattr(response, 'view_state')
                assert hasattr(response, 'plan')
                
                # Verify no new fields added that would break API
                expected_fields = {
                    'schema_version', 'content', 'response_type', 'session_id', 
                    'case_id', 'confidence_score', 'sources', 'next_action_hint',
                    'view_state', 'plan'
                }
                actual_fields = set(response.__dict__.keys())
                unexpected_fields = actual_fields - expected_fields
                assert len(unexpected_fields) == 0, f"Unexpected fields added: {unexpected_fields}"
    
    @pytest.mark.asyncio
    async def test_logging_and_observability(self, agent_service, sample_query_request):
        """Test that QueryClassificationEngine integration adds proper logging"""
        
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "configuration_issue",
            "complexity": "low", 
            "urgency": "normal",
            "domain": "networking",
            "confidence": 0.85
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.confidence.score.return_value = 0.8
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                with patch.object(agent_service.logger, 'info') as mock_log_info:
                    response = await agent_service.process_query(sample_query_request)
                    
                    # Verify logging includes intent information
                    mock_log_info.assert_called()
                    log_calls = [call[0][0] for call in mock_log_info.call_args_list]
                    intent_logged = any("QueryClassificationEngine processed query with intent: configuration_issue" in call for call in log_calls)
                    assert intent_logged, f"Expected intent logging not found in: {log_calls}"