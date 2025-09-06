"""Unit tests for AgentService case integration functionality.

This module tests the new process_query_for_case() method and its integration
with conversation context retrieval and AI processing.
"""

import pytest
import uuid
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch

from faultmaven.services.agent import AgentService
from faultmaven.models import QueryRequest, AgentResponse, ResponseType, ViewState, Source, SourceType
from faultmaven.models.case import MessageType
from faultmaven.exceptions import ValidationException, ServiceException


class TestAgentServiceCaseIntegration:
    """Test cases for AgentService case integration functionality."""
    
    def _create_test_view_state(self, session_id: str, case_id: str) -> ViewState:
        """Helper to create ViewState for testing."""
        from faultmaven.models.api import User, Case as APICase
        
        return ViewState(
            session_id=session_id,
            user=User(
                user_id="test-user",
                email="user@example.com",
                name="Test User",
                created_at="2025-08-30T00:00:00Z"
            ),
            active_case=APICase(
                case_id=case_id,
                title="Test Case",
                status="active",
                created_at="2025-08-30T00:00:00Z",
                updated_at="2025-08-30T00:00:00Z",
                session_id=session_id
            ),
            cases=[],
            messages=[],
            uploaded_data=[],
            show_case_selector=False,
            show_data_upload=True,
            loading_state=None
        )
        
    @pytest.fixture
    def mock_view_state(self):
        """Fixture providing mock ViewState."""
        return self._create_test_view_state("test-session", "test-case")
    
    @pytest.fixture
    def mock_dependencies(self):
        """Fixture providing mocked dependencies for AgentService."""
        return {
            "llm_provider": Mock(),
            "tools": [],
            "tracer": Mock(),
            "sanitizer": Mock(),
            "session_service": AsyncMock(),
            "settings": Mock()
        }
    
    @pytest.fixture
    def agent_service(self, mock_dependencies):
        """Fixture providing AgentService instance with mocked dependencies."""
        # Setup default mock behaviors
        mock_dependencies["llm_provider"].generate = AsyncMock(return_value="AI response")
        
        # Setup tracer mock to support context manager protocol
        trace_context = Mock()
        trace_context.__enter__ = Mock(return_value=trace_context)
        trace_context.__exit__ = Mock(return_value=None)
        mock_dependencies["tracer"].trace = Mock(return_value=trace_context)
        
        mock_dependencies["sanitizer"].sanitize = Mock(side_effect=lambda x: x)
        
        return AgentService(**mock_dependencies)
    
    @pytest.fixture
    def valid_query_request(self):
        """Fixture providing a valid QueryRequest."""
        return QueryRequest(
            query="Why is my application crashing during login?",
            session_id="test-session-123",
            context={"source": "api"},
            priority="medium"
        )
    
    @pytest.fixture
    def sample_conversation_context(self):
        """Fixture providing sample conversation context."""
        return """Previous conversation:
User: My application is crashing
Agent: Let me help troubleshoot. Can you provide error logs?
User: Here are the logs: [ERROR] Auth service timeout"""

    # --- Core Functionality Tests ---

    @pytest.mark.asyncio
    async def test_process_query_for_case_success(self, agent_service, valid_query_request, sample_conversation_context, mock_dependencies):
        """Test successful query processing for a specific case."""
        case_id = "test-case-123"
        
        # Mock session service to return conversation context
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(
            return_value=sample_conversation_context
        )
        
        # Create a proper ViewState for testing
        view_state = self._create_test_view_state(valid_query_request.session_id, case_id)
        
        # Mock the internal query processing
        mock_agent_response = AgentResponse(
            content="Based on the conversation context, this looks like an authentication timeout issue.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=view_state,
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            result = await agent_service.process_query_for_case(case_id, valid_query_request)
            
            # Verify the result
            assert isinstance(result, AgentResponse)
            assert "authentication timeout" in result.content
            assert result.response_type == ResponseType.ANSWER
            
            # Verify conversation context was retrieved
            mock_dependencies["session_service"].format_conversation_context.assert_called_once_with(
                valid_query_request.session_id, case_id, limit=10
            )
            
            # Verify user query was recorded
            mock_dependencies["session_service"].record_case_message.assert_called()
            user_message_call = mock_dependencies["session_service"].record_case_message.call_args_list[0]
            assert user_message_call[1]["message_type"] == MessageType.USER_QUERY
            assert user_message_call[1]["message_content"] == valid_query_request.query
            
            # Verify enhanced request was passed to query processing
            mock_execute.assert_called_once()
            enhanced_request = mock_execute.call_args[0][0]
            
            # Verify conversation context was injected into the query
            assert "Case Context:" in enhanced_request.query
            assert "Previous conversation:" in enhanced_request.query
            assert enhanced_request.context["case_id"] == case_id
            assert enhanced_request.context["has_conversation_context"] is True
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_without_context(self, agent_service, valid_query_request, mock_dependencies):
        """Test query processing when no conversation context is available."""
        case_id = "test-case-456"
        
        # Mock session service to return empty conversation context
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="")
        
        # Create proper ViewState
        view_state = self._create_test_view_state(valid_query_request.session_id, case_id)
        
        mock_agent_response = AgentResponse(
            content="I'll help you troubleshoot this issue.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=view_state,
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            result = await agent_service.process_query_for_case(case_id, valid_query_request)
            
            # Verify the result
            assert isinstance(result, AgentResponse)
            assert result.content == "I'll help you troubleshoot this issue."
            
            # Verify enhanced request had no context injected
            enhanced_request = mock_execute.call_args[0][0]
            assert enhanced_request.query == valid_query_request.query  # No context injection
            assert enhanced_request.context["has_conversation_context"] is False
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_records_assistant_response(self, agent_service, valid_query_request, mock_dependencies):
        """Test that assistant responses are properly recorded to the case."""
        case_id = "test-case-789"
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="")
        
        mock_agent_response = AgentResponse(
            content="Here's the solution to your problem.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=self._create_test_view_state(valid_query_request.session_id, case_id),
            sources=[Source(type=SourceType.KNOWLEDGE_BASE, content="KB info", confidence=0.9, metadata={})],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            await agent_service.process_query_for_case(case_id, valid_query_request)
            
            # Verify both user query and assistant response were recorded
            assert mock_dependencies["session_service"].record_case_message.call_count == 2
            
            # Check user query recording
            user_call = mock_dependencies["session_service"].record_case_message.call_args_list[0]
            assert user_call[1]["message_type"] == MessageType.USER_QUERY
            
            # Check assistant response recording
            assistant_call = mock_dependencies["session_service"].record_case_message.call_args_list[1]
            assert assistant_call[1]["message_type"] == MessageType.AGENT_RESPONSE
            assert assistant_call[1]["message_content"] == "Here's the solution to your problem."
            assert assistant_call[1]["metadata"]["case_id"] == case_id
            assert assistant_call[1]["metadata"]["sources_count"] == 1
    
    # --- Input Validation Tests ---
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_invalid_case_id(self, agent_service, valid_query_request):
        """Test validation error when case_id is invalid."""
        with pytest.raises(ValueError, match="Case ID cannot be empty"):
            await agent_service.process_query_for_case("", valid_query_request)
        
        with pytest.raises(ValueError, match="Case ID cannot be empty"):
            await agent_service.process_query_for_case("   ", valid_query_request)
        
        with pytest.raises(ValueError, match="Case ID cannot be empty"):
            await agent_service.process_query_for_case(None, valid_query_request)
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_invalid_request(self, agent_service):
        """Test validation error when QueryRequest is invalid."""
        case_id = "test-case-123"
        
        with pytest.raises(ValueError, match="QueryRequest cannot be None"):
            await agent_service.process_query_for_case(case_id, None)
        
        invalid_request = QueryRequest(
            query="",
            session_id="test-session",
            context={},
            priority="medium"
        )
        with pytest.raises(ValueError, match="Query cannot be empty"):
            await agent_service.process_query_for_case(case_id, invalid_request)
        
        invalid_request = QueryRequest(
            query="valid query",
            session_id="",
            context={},
            priority="medium"
        )
        with pytest.raises(ValueError, match="Session ID cannot be empty"):
            await agent_service.process_query_for_case(case_id, invalid_request)
    
    # --- Error Handling Tests ---
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_session_service_failure(self, agent_service, valid_query_request, mock_dependencies):
        """Test graceful handling of session service failures."""
        case_id = "test-case-123"
        
        # Mock session service to raise exception
        mock_dependencies["session_service"].record_case_message.side_effect = Exception("Redis connection failed")
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="")
        
        mock_agent_response = AgentResponse(
            content="Processing completed despite session service issues.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=self._create_test_view_state(valid_query_request.session_id, case_id),
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            # Should not raise exception, but continue processing
            result = await agent_service.process_query_for_case(case_id, valid_query_request)
            
            assert isinstance(result, AgentResponse)
            assert result.content == "Processing completed despite session service issues."
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_query_processing_failure(self, agent_service, valid_query_request, mock_dependencies):
        """Test handling of query processing failures."""
        case_id = "test-case-123"
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="")
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.side_effect = ServiceException("LLM service unavailable")
            
            # Should propagate the service exception directly (BaseService now preserves FaultMaven exceptions)
            with pytest.raises(ServiceException, match="LLM service unavailable"):
                await agent_service.process_query_for_case(case_id, valid_query_request)
    
    # --- Integration with Case Service Tests ---
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_updates_case_query_count(self, agent_service, valid_query_request, mock_dependencies):
        """Test that case query count is updated when case service is available."""
        case_id = "test-case-123"
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="")
        
        mock_case_service = AsyncMock()
        
        mock_agent_response = AgentResponse(
            content="Query processed successfully.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=self._create_test_view_state(valid_query_request.session_id, case_id),
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            with patch('faultmaven.container.container') as mock_container:
                mock_container.get_case_service.return_value = mock_case_service
                mock_execute.return_value = mock_agent_response
                
                await agent_service.process_query_for_case(case_id, valid_query_request)
                
                # Verify case service was called to update query count
                mock_case_service.add_case_query.assert_called_once_with(case_id, valid_query_request.query)
    
    # --- ViewState Update Tests ---
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_updates_view_state_case_id(self, agent_service, valid_query_request, mock_dependencies):
        """Test that view_state is updated with the correct case_id."""
        case_id = "test-case-specific-id"
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="")
        
        # Create proper ViewState instead of Mock for Pydantic validation
        view_state = self._create_test_view_state(valid_query_request.session_id, case_id)
        
        mock_agent_response = AgentResponse(
            content="Response with view state.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=view_state,
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            result = await agent_service.process_query_for_case(case_id, valid_query_request)
            
            # Verify view_state active_case case_id was updated
            assert result.view_state.active_case.case_id == case_id
    
    # --- Performance Tests ---
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_performance_metrics(self, agent_service, valid_query_request, mock_dependencies):
        """Test that performance metrics are logged."""
        case_id = "test-case-123"
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="context")
        
        mock_agent_response = AgentResponse(
            content="Performance test response.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=self._create_test_view_state(valid_query_request.session_id, case_id),
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            with patch.object(agent_service, 'log_business_event') as mock_log_event:
                mock_execute.return_value = mock_agent_response
                
                await agent_service.process_query_for_case(case_id, valid_query_request)
                
                # Verify business events were logged
                mock_log_event.assert_called()
                event_calls = [call[0][0] for call in mock_log_event.call_args_list]
                
                assert "case_query_processing_started" in event_calls
                assert "case_query_processing_completed" in event_calls
    
    # --- Edge Cases ---
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_with_unicode_content(self, agent_service, mock_dependencies):
        """Test handling of Unicode content in queries and responses."""
        case_id = "test-case-unicode"
        
        unicode_query = QueryRequest(
            query="Why is my application showing 错误 (error) messages? 🚨",
            session_id="test-session-unicode",
            context={"encoding": "utf-8"},
            priority="medium"
        )
        
        unicode_context = "Previous: User reported 问题 with authentication 認証"
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value=unicode_context)
        
        mock_agent_response = AgentResponse(
            content="Unicode characters handled correctly: 处理成功 ✓",
            response_type=ResponseType.ANSWER,
            session_id=unicode_query.session_id,
            case_id=case_id,
            view_state=self._create_test_view_state(unicode_query.session_id, case_id),
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            result = await agent_service.process_query_for_case(case_id, unicode_query)
            
            assert isinstance(result, AgentResponse)
            assert "处理成功" in result.content
            
            # Verify context was properly injected with Unicode content
            enhanced_request = mock_execute.call_args[0][0]
            assert "認証" in enhanced_request.query
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_large_conversation_context(self, agent_service, valid_query_request, mock_dependencies):
        """Test handling of large conversation context."""
        case_id = "test-case-large-context"
        
        # Create a large conversation context
        large_context = "Context: " + "A very long conversation " * 1000  # ~25KB of text
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value=large_context)
        
        mock_agent_response = AgentResponse(
            content="Handled large context successfully.",
            response_type=ResponseType.ANSWER,
            session_id=valid_query_request.session_id,
            case_id=case_id,
            view_state=self._create_test_view_state(valid_query_request.session_id, case_id),
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            result = await agent_service.process_query_for_case(case_id, valid_query_request)
            
            assert isinstance(result, AgentResponse)
            
            # Verify large context was handled
            enhanced_request = mock_execute.call_args[0][0]
            assert len(enhanced_request.query) > len(valid_query_request.query)
            assert enhanced_request.context["context_length"] == len(large_context)
    
    # --- Security Tests ---
    
    @pytest.mark.asyncio
    async def test_process_query_for_case_sanitizes_input(self, agent_service, mock_dependencies):
        """Test that input is properly sanitized."""
        case_id = "test-case-security"
        
        potentially_malicious_query = QueryRequest(
            query="<script>alert('xss')</script> Why is my app broken?",
            session_id="test-session",
            context={"source": "potentially_untrusted"},
            priority="medium"
        )
        
        mock_dependencies["session_service"].record_case_message = AsyncMock()
        mock_dependencies["session_service"].format_conversation_context = AsyncMock(return_value="")
        mock_dependencies["sanitizer"].sanitize = Mock(return_value="Why is my app broken?")  # Remove script tags
        
        mock_agent_response = AgentResponse(
            content="Sanitized response.",
            response_type=ResponseType.ANSWER,
            session_id=potentially_malicious_query.session_id,
            case_id=case_id,
            view_state=self._create_test_view_state(potentially_malicious_query.session_id, case_id),
            sources=[],
            plan=None
        )
        
        with patch.object(agent_service, '_execute_query_processing', new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = mock_agent_response
            
            await agent_service.process_query_for_case(case_id, potentially_malicious_query)
            
            # Verify input was sanitized
            mock_dependencies["sanitizer"].sanitize.assert_called_with(potentially_malicious_query.query)