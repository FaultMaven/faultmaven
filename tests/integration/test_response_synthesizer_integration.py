"""Integration test for ResponseSynthesizer in AgentService

This test validates the second Agentic Framework replacement:
- ResponseSynthesizer replaces basic content formatting
- Proper fallback to original _generate_content method
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
        query="I need help with database troubleshooting",
        context={}
    )


class TestResponseSynthesizerIntegration:
    """Test ResponseSynthesizer integration in AgentService"""
    
    @pytest.mark.asyncio
    async def test_response_synthesizer_success(self, agent_service, sample_query_request):
        """Test successful ResponseSynthesizer processing"""
        
        # Mock ResponseSynthesizer
        mock_response_synthesizer = AsyncMock()
        mock_response_synthesizer.synthesize_response.return_value = {
            "content": "Enhanced response content from ResponseSynthesizer",
            "confidence": 0.9,
            "reasoning": "Applied advanced synthesis patterns"
        }
        
        # Mock container to return ResponseSynthesizer and other components
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_response_synthesizer.return_value = mock_response_synthesizer
            mock_container.get_query_classification_engine.return_value = None  # Avoid first replacement
            mock_container.confidence.score.return_value = 0.8
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            # Mock ViewState creation
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                # Execute query processing
                response = await agent_service.process_query(sample_query_request)
                
                # Verify ResponseSynthesizer was called
                mock_response_synthesizer.synthesize_response.assert_called_once()
                call_args = mock_response_synthesizer.synthesize_response.call_args
                
                # Verify synthesis context includes required fields
                context = call_args[1]["context"]
                assert context["query"] == "test query"
                assert context["session_id"] == "test-session"
                assert context["case_id"] == "test-case-id"
                assert "agent_result" in context
                assert "processing_time" in context
                assert "response_type" in context
                
                # Verify sources and intent parameters
                assert "sources" in call_args[1]
                assert "intent" in call_args[1]
                
                # Verify response contains enhanced content
                assert response.content == "Enhanced response content from ResponseSynthesizer"
                assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_response_synthesizer_fallback(self, agent_service, sample_query_request):
        """Test fallback to original content generation when ResponseSynthesizer fails"""
        
        # Mock ResponseSynthesizer to raise exception
        mock_response_synthesizer = AsyncMock()
        mock_response_synthesizer.synthesize_response.side_effect = Exception("Synthesis failed")
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_response_synthesizer.return_value = mock_response_synthesizer
            mock_container.get_query_classification_engine.return_value = None
            mock_container.confidence.score.return_value = 0.7
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                with patch.object(agent_service, '_generate_content') as mock_generate_content:
                    mock_generate_content.return_value = "Original fallback content"
                    
                    response = await agent_service.process_query(sample_query_request)
                    
                    # Verify ResponseSynthesizer was attempted
                    mock_response_synthesizer.synthesize_response.assert_called_once()
                    
                    # Verify fallback was used
                    mock_generate_content.assert_called_once()
                    
                    # Verify response contains fallback content
                    assert response.content == "Original fallback content"
                    assert response.session_id == "test-session"
    
    @pytest.mark.asyncio 
    async def test_response_synthesizer_unavailable(self, agent_service, sample_query_request):
        """Test behavior when ResponseSynthesizer is not available"""
        
        with patch('faultmaven.container.container') as mock_container:
            # Return None for ResponseSynthesizer (not available)
            mock_container.get_response_synthesizer.return_value = None
            mock_container.get_query_classification_engine.return_value = None
            mock_container.confidence.score.return_value = 0.5
            mock_container.confidence.get_band.return_value = "medium"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                with patch.object(agent_service, '_generate_content') as mock_generate_content:
                    mock_generate_content.return_value = "Basic content generation"
                    
                    response = await agent_service.process_query(sample_query_request)
                    
                    # Verify ResponseSynthesizer getter was called
                    mock_container.get_response_synthesizer.assert_called_once()
                    
                    # Verify original content generation was used
                    mock_generate_content.assert_called_once()
                    
                    # Verify response with basic content
                    assert response.content == "Basic content generation"
                    assert response.session_id == "test-session"
    
    @pytest.mark.asyncio
    async def test_api_compatibility_maintained(self, agent_service, sample_query_request):
        """Test that API response format is unchanged with ResponseSynthesizer"""
        
        mock_response_synthesizer = AsyncMock()
        mock_response_synthesizer.synthesize_response.return_value = {
            "content": "API compatible enhanced response",
            "metadata": {"synthesis_type": "advanced"}
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_response_synthesizer.return_value = mock_response_synthesizer
            mock_container.get_query_classification_engine.return_value = None
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
                
                # Verify enhanced content is present
                assert "API compatible enhanced response" in response.content
                
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
    async def test_enhanced_content_synthesis(self, agent_service, sample_query_request):
        """Test that ResponseSynthesizer provides enhanced content synthesis"""
        
        mock_response_synthesizer = AsyncMock()
        mock_response_synthesizer.synthesize_response.return_value = {
            "content": "# Troubleshooting Analysis\n\n**Issue**: Database connectivity problems\n\n**Recommendations**:\n1. Check connection pools\n2. Verify credentials\n3. Test network connectivity",
            "synthesis_metadata": {
                "structure_applied": True,
                "context_enriched": True,
                "recommendations_prioritized": True
            }
        }
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_response_synthesizer.return_value = mock_response_synthesizer
            mock_container.get_query_classification_engine.return_value = None
            mock_container.confidence.score.return_value = 0.95
            mock_container.confidence.get_band.return_value = "high"
            mock_container.loop_guard.check.return_value = {"status": "ok"}
            
            with patch.object(agent_service, '_create_view_state') as mock_view_state:
                mock_view_state.return_value = Mock()
                
                response = await agent_service.process_query(sample_query_request)
                
                # Verify enhanced structured content
                assert "# Troubleshooting Analysis" in response.content
                assert "**Issue**:" in response.content
                assert "**Recommendations**:" in response.content
                assert "1. Check connection pools" in response.content
                
                # Verify ResponseSynthesizer received proper context
                call_args = mock_response_synthesizer.synthesize_response.call_args
                context = call_args[1]["context"]
                assert context["query"] == "test query"
                assert context["case_id"] == "test-case-id"