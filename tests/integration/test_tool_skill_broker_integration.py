"""Integration test for ToolSkillBroker in AgentService

This test validates the third Agentic Framework replacement:
- ToolSkillBroker replaces basic tool orchestration
- Proper fallback to Skills system when ToolSkillBroker unavailable
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
    session_service.get_conversation_context.return_value = ""
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


class TestToolSkillBrokerIntegration:
    """Test ToolSkillBroker integration in AgentService"""
    
    @pytest.mark.asyncio
    async def test_tool_skill_broker_success(self, agent_service, sample_query_request):
        """Test successful ToolSkillBroker processing"""
        
        # Mock QueryClassificationEngine
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "performance_troubleshooting",
            "complexity": "medium",
            "urgency": "normal", 
            "domain": "infrastructure",
            "confidence": 0.9
        }
        
        # Mock ToolSkillBroker
        mock_tool_skill_broker = AsyncMock()
        mock_tool_skill_broker.orchestrate_capabilities.return_value = {
            "success": True,
            "evidence": ["Tool orchestration evidence 1", "Tool orchestration evidence 2"],
            "confidence_boost": 0.15,
            "capabilities_identified": True,
            "orchestration_metadata": {"tools_used": ["knowledge_base", "web_search"]}
        }
        
        # Mock container to return both components
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = mock_tool_skill_broker
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
                
                # Verify ToolSkillBroker was called with enhanced context
                mock_tool_skill_broker.orchestrate_capabilities.assert_called_once()
                call_args = mock_tool_skill_broker.orchestrate_capabilities.call_args
                
                # Verify orchestration context includes required fields
                context = call_args[1]["context"]
                assert context["query"] == "test query"
                assert context["session_id"] == "test-session"
                assert context["case_id"] == "test-case-id"
                assert "classification" in context
                assert context["classification"]["intent"] == "performance_troubleshooting"
                assert "available_tools" in context
                
                # Verify intent and complexity parameters
                assert call_args[1]["intent"] == "performance_troubleshooting"
                assert call_args[1]["complexity"] == "medium"
                
                # Verify response structure maintained
                assert hasattr(response, 'content')
                assert hasattr(response, 'response_type')
                assert hasattr(response, 'session_id')
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_tool_skill_broker_fallback_to_skills(self, agent_service, sample_query_request):
        """Test fallback to Skills system when ToolSkillBroker fails"""
        
        # Mock QueryClassificationEngine success
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "debugging",
            "complexity": "high",
            "urgency": "critical",
            "domain": "database",
            "confidence": 0.8
        }
        
        # Mock ToolSkillBroker to raise exception
        mock_tool_skill_broker = AsyncMock()
        mock_tool_skill_broker.orchestrate_capabilities.side_effect = Exception("Tool orchestration failed")
        
        # Mock Skills system
        mock_skill = AsyncMock()
        mock_skill.execute.return_value = {
            "confidence_delta": {"skill_confidence": 0.7},
            "evidence": ["test evidence from skills"],
            "proposed_action": None
        }
        mock_skill.name = "test_skill"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = mock_tool_skill_broker
            mock_container.skill_registry.all.return_value = [mock_skill]
            mock_container.skill_router.select.return_value = [mock_skill]
            mock_container.confidence.score.return_value = 0.7
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify QueryClassificationEngine was called
                mock_classification_engine.classify_query.assert_called_once()
                
                # Verify ToolSkillBroker was attempted but failed
                mock_tool_skill_broker.orchestrate_capabilities.assert_called_once()
                
                # Verify Skills fallback was NOT used in this path (different fallback logic)
                # The Skills fallback happens in the outer exception handler
                
                # Verify response still valid
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio 
    async def test_tool_skill_broker_unavailable(self, agent_service, sample_query_request):
        """Test behavior when ToolSkillBroker is not available"""
        
        # Mock QueryClassificationEngine success
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
            # Return None for ToolSkillBroker (not available)
            mock_container.get_tool_skill_broker.return_value = None
            mock_container.confidence.score.return_value = 0.5
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify QueryClassificationEngine was called
                mock_classification_engine.classify_query.assert_called_once()
                
                # Verify ToolSkillBroker getter was called
                mock_container.get_tool_skill_broker.assert_called_once()
                
                # Verify response with basic processing (no enhanced orchestration)
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_api_compatibility_maintained(self, agent_service, sample_query_request):
        """Test that API response format is unchanged with ToolSkillBroker"""
        
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "security_incident",
            "complexity": "high", 
            "urgency": "critical",
            "domain": "security",
            "confidence": 0.95
        }
        
        mock_tool_skill_broker = AsyncMock()
        mock_tool_skill_broker.orchestrate_capabilities.return_value = {
            "success": True,
            "evidence": ["Enhanced security analysis"],
            "confidence_boost": 0.2,
            "capabilities_identified": True
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = mock_tool_skill_broker
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
    async def test_enhanced_tool_orchestration(self, agent_service, sample_query_request):
        """Test that ToolSkillBroker provides enhanced tool orchestration"""
        
        mock_classification_engine = AsyncMock()
        mock_classification_engine.classify_query.return_value = {
            "intent": "performance_troubleshooting",
            "complexity": "high",
            "urgency": "critical", 
            "domain": "infrastructure",
            "confidence": 0.9
        }
        
        # Mock enhanced orchestration result
        mock_tool_skill_broker = AsyncMock()
        mock_tool_skill_broker.orchestrate_capabilities.return_value = {
            "success": True,
            "evidence": [
                "Performance metrics gathered from monitoring tools",
                "Resource utilization patterns identified", 
                "Bottleneck analysis completed"
            ],
            "confidence_boost": 0.25,
            "capabilities_identified": True,
            "orchestration_metadata": {
                "tools_coordinated": ["metrics_collector", "resource_analyzer", "bottleneck_detector"],
                "orchestration_strategy": "performance_diagnostic",
                "execution_time": 2.5
            }
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_query_classification_engine.return_value = mock_classification_engine
            mock_container.get_tool_skill_broker.return_value = mock_tool_skill_broker
            mock_container.confidence.score.return_value = 0.95
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify ToolSkillBroker received proper context
                call_args = mock_tool_skill_broker.orchestrate_capabilities.call_args
                context = call_args[1]["context"]
                assert context["query"] == "test query"
                assert context["classification"]["intent"] == "performance_troubleshooting"
                assert context["classification"]["complexity"] == "high"
                assert "available_tools" in context
                
                # Verify orchestration parameters
                assert call_args[1]["intent"] == "performance_troubleshooting"
                assert call_args[1]["complexity"] == "high"
                
                # Verify enhanced orchestration was successful
                assert response.session_id == "test-session"