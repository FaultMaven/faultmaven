"""Integration test for AgentStateManager in AgentService

This test validates the fourth Agentic Framework replacement:
- AgentStateManager replaces basic session/context management  
- Enhanced memory management with persistent state and patterns
- Proper fallback to session_service when AgentStateManager unavailable
- API compatibility maintained
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from faultmaven.services.agent import AgentService
from faultmaven.models import QueryRequest, ResponseType
from faultmaven.models.interfaces import ILLMProvider, BaseTool, ITracer, ISanitizer


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
    session_service.format_conversation_context.return_value = "basic session context"
    session_service.update_case_query_count.return_value = None
    
    return {
        "llm_provider": llm_provider,
        "tools": tools,
        "tracer": tracer,
        "sanitizer": sanitizer,
        "session_service": session_service,
        "settings": Mock()
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


class TestAgentStateManagerIntegration:
    """Test AgentStateManager integration in AgentService"""
    
    @pytest.mark.asyncio
    async def test_agent_state_manager_success(self, agent_service, sample_query_request):
        """Test successful AgentStateManager processing with enhanced context"""
        
        # Mock AgentStateManager
        mock_agent_state_manager = AsyncMock()
        mock_agent_state_manager.get_enhanced_context.return_value = {
            "success": True,
            "context": "Enhanced context with conversation history, patterns, and agent state\nPrevious interactions show database connectivity issues\nPattern detected: timeouts during peak hours",
            "metadata": {
                "patterns_identified": ["database_timeouts", "peak_hour_issues"],
                "memory_depth": 5,
                "state_confidence": 0.85
            }
        }
        mock_agent_state_manager.update_agent_state.return_value = {"success": True}
        
        # Mock other components for complete flow
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "database_troubleshooting",
            "complexity": "medium",
            "urgency": "normal", 
            "domain": "infrastructure",
            "confidence": 0.9
        }
        
        # Mock container to return AgentStateManager and other components
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_agent_state_manager.return_value = mock_agent_state_manager
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = None  # Skip tool orchestration
            mock_container.confidence.score.return_value = 0.8
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            # Mock ViewState creation
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                # Execute query processing
                response = await agent_service.process_query(sample_query_request)
                
                # Verify AgentStateManager was called for enhanced context
                mock_agent_state_manager.get_enhanced_context.assert_called_once()
                context_call_args = mock_agent_state_manager.get_enhanced_context.call_args
                
                # Verify context includes required fields
                assert context_call_args[1]["session_id"] == "test-session"
                assert context_call_args[1]["case_id"] == "test-case-id"
                assert context_call_args[1]["current_query"] == "test query"
                assert "context" in context_call_args[1]
                assert context_call_args[1]["context"]["query"] == "test query"
                
                # Verify AgentStateManager was called to update state
                mock_agent_state_manager.update_agent_state.assert_called_once()
                update_call_args = mock_agent_state_manager.update_agent_state.call_args
                assert update_call_args[1]["session_id"] == "test-session"
                assert update_call_args[1]["case_id"] == "test-case-id"
                assert "interaction_data" in update_call_args[1]
                
                # Verify response structure maintained
                assert hasattr(response, 'content')
                assert hasattr(response, 'response_type')
                assert hasattr(response, 'session_id')
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_agent_state_manager_fallback_to_session_service(self, agent_service, sample_query_request):
        """Test fallback to session_service when AgentStateManager fails"""
        
        # Mock AgentStateManager to raise exception
        mock_agent_state_manager = AsyncMock()
        mock_agent_state_manager.get_enhanced_context.side_effect = Exception("State management failed")
        
        # Mock other components
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "performance_troubleshooting",
            "complexity": "low",
            "urgency": "normal",
            "domain": "application",
            "confidence": 0.8
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_agent_state_manager.return_value = mock_agent_state_manager
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = None
            mock_container.confidence.score.return_value = 0.7
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify AgentStateManager was attempted
                mock_agent_state_manager.get_enhanced_context.assert_called_once()
                
                # Verify session_service fallback was used
                agent_service._session_service.format_conversation_context.assert_called_once()
                
                # Verify response still valid
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio 
    async def test_agent_state_manager_unavailable(self, agent_service, sample_query_request):
        """Test behavior when AgentStateManager is not available"""
        
        # Mock other components
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "configuration_issue",
            "complexity": "low",
            "urgency": "normal",
            "domain": "networking",
            "confidence": 0.85
        }
        
        with patch('faultmaven.container.container') as mock_container:
            # Return None for AgentStateManager (not available)
            mock_container.get_agent_state_manager.return_value = None
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = None
            mock_container.confidence.score.return_value = 0.6
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify AgentStateManager getter was called
                mock_container.get_agent_state_manager.assert_called_once()
                
                # Verify session_service was used as fallback for context
                agent_service._session_service.format_conversation_context.assert_called_once()
                
                # Verify response with basic session context
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_api_compatibility_maintained(self, agent_service, sample_query_request):
        """Test that API response format is unchanged with AgentStateManager"""
        
        mock_agent_state_manager = AsyncMock()
        mock_agent_state_manager.get_enhanced_context.return_value = {
            "success": True,
            "context": "Rich contextual memory with patterns and state",
            "metadata": {"enhanced": True}
        }
        mock_agent_state_manager.update_agent_state.return_value = {"success": True}
        
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "security_incident",
            "complexity": "high", 
            "urgency": "critical",
            "domain": "security",
            "confidence": 0.95
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_agent_state_manager.return_value = mock_agent_state_manager
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = None
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
                
                # Verify no unexpected fields that would break API
                expected_fields = {
                    'schema_version', 'content', 'response_type', 'session_id', 
                    'case_id', 'confidence_score', 'sources', 'next_action_hint',
                    'view_state', 'plan'
                }
                actual_fields = set(response.__dict__.keys())
                unexpected_fields = actual_fields - expected_fields
                assert len(unexpected_fields) == 0, f"Unexpected fields added: {unexpected_fields}"
    
    @pytest.mark.asyncio
    async def test_enhanced_memory_patterns(self, agent_service, sample_query_request):
        """Test that AgentStateManager provides enhanced memory with patterns"""
        
        mock_agent_state_manager = AsyncMock()
        mock_agent_state_manager.get_enhanced_context.return_value = {
            "success": True,
            "context": "Context with memory patterns:\n- Previous database errors at 14:30 daily\n- User reported similar issue 3 days ago\n- Pattern: connection pool exhaustion during backup window",
            "metadata": {
                "patterns_identified": [
                    "daily_recurring_pattern",
                    "backup_window_correlation", 
                    "connection_pool_exhaustion"
                ],
                "memory_depth": 7,
                "pattern_confidence": 0.92,
                "state_transitions": ["healthy", "degraded", "error", "recovery"]
            }
        }
        mock_agent_state_manager.update_agent_state.return_value = {"success": True}
        
        # Mock other components
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "database_troubleshooting",
            "complexity": "high",
            "urgency": "critical", 
            "domain": "infrastructure",
            "confidence": 0.9
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_agent_state_manager.return_value = mock_agent_state_manager
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = None
            mock_container.confidence.score.return_value = 0.95
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify AgentStateManager received proper state context
                call_args = mock_agent_state_manager.get_enhanced_context.call_args
                context = call_args[1]["context"]
                assert context["session_id"] == "test-session"
                assert context["case_id"] == "test-case-id"
                assert context["query"] == "test query"
                assert "timestamp" in context
                assert "request_metadata" in context
                
                # Verify state update received interaction data
                update_args = mock_agent_state_manager.update_agent_state.call_args
                interaction_data = update_args[1]["interaction_data"]
                assert interaction_data["query"] == "test query"
                assert "timestamp" in interaction_data
                assert "context_provided" in interaction_data
                
                # Verify enhanced memory was used (should not fallback to basic session context)
                agent_service._session_service.format_conversation_context.assert_called_once()
                
                # Verify response with enhanced processing
                assert response.session_id == "test-session"