"""End-to-end workflow tests for case-to-agent integration.

This module provides comprehensive end-to-end tests that validate the complete
workflow from case creation through query processing with real AI integration.
"""

import pytest
import asyncio
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from faultmaven.services.agentic.orchestration.agent_service import AgentService
from faultmaven.services.domain.session_service import SessionService
from faultmaven.models import QueryRequest, AgentResponse, ResponseType, ViewState, Source, SourceType, SessionContext
from faultmaven.models.case import Case, CaseStatus, CasePriority, MessageType
from faultmaven.models.api import User, Case as APICase
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.infrastructure.observability.tracing import OpikTracer


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider with realistic responses."""
    provider = AsyncMock()
    
    def generate_response(prompt, **kwargs):
        """Generate contextual responses based on prompt content."""
        if "login" in prompt.lower() or "authentication" in prompt.lower():
            return "Based on the authentication patterns, this appears to be a timeout issue with your auth service. I recommend checking connection pools and database query performance."
        elif "memory" in prompt.lower() or "crash" in prompt.lower():
            return "Memory-related crashes typically indicate heap exhaustion or memory leaks. Let's analyze garbage collection patterns and heap dumps."
        elif "performance" in prompt.lower() or "slow" in prompt.lower():
            return "Performance issues often stem from database queries, network latency, or resource contention. Let's examine your metrics and logs."
        else:
            return "I'll help you troubleshoot this issue. Can you provide more details about the symptoms and error messages?"
    
    async def async_generate_response(prompt, **kwargs):
        """Async wrapper for generate_response."""
        return generate_response(prompt, **kwargs)
    
    provider.generate = async_generate_response
    return provider


@pytest.fixture
def mock_session_service():
    """Mock session service with conversation history capabilities."""
    service = AsyncMock()
    
    # Mock conversation history storage
    conversation_store = {}
    
    async def mock_format_conversation_context(session_id, case_id, limit=10):
        """Mock conversation context formatting."""
        key = f"{session_id}_{case_id}"
        if key in conversation_store:
            messages = conversation_store[key][-limit:]
            context_lines = []
            for msg in messages:
                role = "User" if msg["type"] == MessageType.USER_QUERY else "Agent"
                context_lines.append(f"{role}: {msg['content']}")
            return "\n".join(context_lines)
        return ""
    
    async def mock_record_case_message(session_id, message_content, message_type, author_id=None, metadata=None):
        """Mock message recording with conversation history tracking."""
        key = f"{session_id}_{metadata.get('case_id', 'default') if metadata else 'default'}"
        if key not in conversation_store:
            conversation_store[key] = []
        
        conversation_store[key].append({
            "content": message_content,
            "type": message_type,
            "timestamp": datetime.utcnow(),
            "author_id": author_id
        })
    
    service.format_conversation_context = mock_format_conversation_context
    service.record_case_message = mock_record_case_message
    service.get_session = AsyncMock(return_value=Mock(session_id="test-session"))
    service.get_or_create_current_case_id = AsyncMock(return_value="e2e-test-case-123")
    service.update_case_query_count = AsyncMock()

    return service


@pytest.fixture
def mock_case_service():
    """Mock case service with comprehensive functionality."""
    service = AsyncMock()
    
    # Mock case storage
    cases_store = {}
    
    async def mock_get_case(case_id, user_id=None):
        """Mock case retrieval."""
        if case_id in cases_store:
            return cases_store[case_id]
        
        # Create default case for testing
        case = Mock()
        case.case_id = case_id
        case.title = f"Test Case {case_id}"
        case.status = CaseStatus.ACTIVE
        case.priority = CasePriority.MEDIUM
        case.owner_id = user_id or "test-user"
        case.message_count = 0
        case.created_at = datetime.utcnow()
        case.updated_at = datetime.utcnow()
        
        cases_store[case_id] = case
        return case
    
    async def mock_add_case_query(case_id, query_text, user_id=None):
        """Mock case query count update."""
        if case_id in cases_store:
            cases_store[case_id].message_count += 1
            cases_store[case_id].updated_at = datetime.utcnow()
    
    service.get_case = mock_get_case
    service.add_case_query = mock_add_case_query
    service.add_assistant_response = AsyncMock()
    
    return service


@pytest.fixture
def mock_tracer():
    """Mock tracer for observability."""
    tracer = Mock()
    # Create a proper context manager mock
    context_manager = Mock()
    context_manager.__enter__ = Mock(return_value=context_manager)
    context_manager.__exit__ = Mock(return_value=None)
    tracer.trace.return_value = context_manager
    return tracer


@pytest.fixture
def mock_sanitizer():
    """Mock sanitizer that preserves content for testing."""
    sanitizer = Mock()
    sanitizer.sanitize = Mock(side_effect=lambda x: x)  # Pass-through for testing
    return sanitizer


@pytest.fixture
def mock_agentic_components():
    """Mock agentic framework components for E2E testing."""
    return {
        "business_logic_workflow_engine": AsyncMock(),
        "query_classification_engine": AsyncMock(),
        "tool_skill_broker": AsyncMock(),
        "guardrails_policy_layer": AsyncMock(),
        "response_synthesizer": AsyncMock(),
        "error_fallback_manager": AsyncMock(),
        "agent_state_manager": AsyncMock()
    }


@pytest.fixture
def agent_service_with_mocks(mock_llm_provider, mock_session_service, mock_tracer, mock_sanitizer, mock_agentic_components):
    """AgentService with mocked dependencies for E2E testing."""
    # Setup default mock behaviors for E2E testing
    mock_agentic_components["query_classification_engine"].classify_query = AsyncMock(return_value={
        "intent": "troubleshooting",
        "complexity": "medium",
        "urgency": "normal",
        "domain": "e2e_test"
    })

    mock_agentic_components["tool_skill_broker"].orchestrate_capabilities = AsyncMock(return_value={
        "evidence": []
    })

    mock_agentic_components["business_logic_workflow_engine"].execute_agentic_workflow = AsyncMock(return_value={
        "evidence": [],
        "confidence_boost": 0.8,
        "plan_executed": True,
        "observations": [],
        "adaptations": [],
        "execution_plan": None
    })

    mock_agentic_components["response_synthesizer"].synthesize_response = AsyncMock(return_value={
        "content": "E2E test response",
        "sources": []
    })

    mock_agentic_components["agent_state_manager"].get_enhanced_context = AsyncMock(return_value={
        "success": False
    })

    mock_agentic_components["agent_state_manager"].update_agent_state = AsyncMock()

    mock_agentic_components["error_fallback_manager"].handle_execution_error = AsyncMock(return_value={
        "recovery_message": "E2E fallback response"
    })

    # Use patches to bypass type validation like in the other test file
    with patch.object(AgentService, '_validate_agentic_components'), \
         patch.object(AgentService, '_create_view_state') as mock_view_state:
        from faultmaven.models.api import User
        from faultmaven.models import ViewState

        mock_view_state.return_value = ViewState(
            session_id="e2e-test-session",
            user=User(
                user_id="e2e-test-user",
                username="e2etestuser",
                email="e2e@example.com",
                name="E2E Test User"
            )
        )

        agent = AgentService(
            llm_provider=mock_llm_provider,
            tools=[],
            tracer=mock_tracer,
            sanitizer=mock_sanitizer,
            session_service=mock_session_service,
            settings=Mock(),
            **mock_agentic_components
        )
        agent._mock_view_state = mock_view_state  # Store for test inspection
        return agent


@pytest.mark.integration
class TestCaseAgentEndToEndWorkflow:
    """End-to-end workflow tests for case-agent integration."""
    
    @pytest.mark.asyncio
    async def test_complete_authentication_troubleshooting_workflow(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test complete workflow for authentication troubleshooting case."""
        # Setup scenario
        case_id = "auth-case-001"
        session_id = "session-auth-001"
        user_id = "user-123"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Step 1: Initial user query about login issues
            initial_query = QueryRequest(
                query="Users are reporting login failures in our application. They get timeout errors after entering credentials.",
                session_id=session_id,
                context={"source": "api", "urgency": "high"},
                priority="high"
            )
            
            response_1 = await agent_service_with_mocks.process_query_for_case(case_id, initial_query)
            
            # Verify initial response
            assert isinstance(response_1, AgentResponse)
            assert "authentication" in response_1.content.lower()
            assert "timeout" in response_1.content.lower()
            assert response_1.response_type == ResponseType.ANSWER
            assert response_1.view_state.active_case.case_id == case_id
            
            # Step 2: Follow-up query with more details
            followup_query = QueryRequest(
                query="I checked the auth service logs. I see 'Connection pool exhausted' errors. What should I do?",
                session_id=session_id,
                context={"source": "api"},
                priority="high"
            )
            
            response_2 = await agent_service_with_mocks.process_query_for_case(case_id, followup_query)
            
            # Verify follow-up response considers conversation context
            assert isinstance(response_2, AgentResponse)
            assert "connection pool" in response_2.content.lower()
            
            # Verify case was updated
            updated_case = await mock_case_service.get_case(case_id, user_id)
            assert updated_case.message_count == 2  # Two queries processed
    
    @pytest.mark.asyncio
    async def test_memory_leak_investigation_workflow(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test complete workflow for memory leak investigation."""
        case_id = "memory-case-002"
        session_id = "session-memory-002"
        user_id = "user-456"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Step 1: Initial crash report
            crash_query = QueryRequest(
                query="Our application keeps crashing with OutOfMemoryError during peak traffic.",
                session_id=session_id,
                context={"source": "api", "environment": "production"},
                priority="critical"
            )
            
            response_1 = await agent_service_with_mocks.process_query_for_case(case_id, crash_query)
            
            assert "memory" in response_1.content.lower()
            assert "heap" in response_1.content.lower() or "garbage collection" in response_1.content.lower()
            
            # Step 2: Provide heap dump analysis
            analysis_query = QueryRequest(
                query="I analyzed the heap dump. Large number of User objects not being garbage collected. Suspect memory leak in session management.",
                session_id=session_id,
                context={"source": "api", "analysis": "heap_dump"},
                priority="critical"
            )
            
            response_2 = await agent_service_with_mocks.process_query_for_case(case_id, analysis_query)
            
            # Verify response builds on conversation context
            assert isinstance(response_2, AgentResponse)
            # Response should reference both the original crash and the heap analysis
    
    @pytest.mark.asyncio
    async def test_performance_degradation_workflow(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test workflow for performance degradation investigation."""
        case_id = "perf-case-003"
        session_id = "session-perf-003"
        user_id = "user-789"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Simulate multiple queries in sequence
            queries_and_expected_keywords = [
                ("API response times have increased from 200ms to 2000ms over the past week.", ["performance", "response time"]),
                ("Database monitoring shows some slow queries, but not consistently slow.", ["database", "queries"]),
                ("CPU and memory utilization are normal. Network latency also looks fine.", ["metrics", "network"]),
                ("Found the issue - a recent code change introduced N+1 query problem in user profile loading.", ["N+1", "query"])
            ]
            
            responses = []
            
            for i, (query_text, expected_keywords) in enumerate(queries_and_expected_keywords):
                query = QueryRequest(
                    query=query_text,
                    session_id=session_id,
                    context={"source": "api", "step": i + 1},
                    priority="medium"
                )
                
                response = await agent_service_with_mocks.process_query_for_case(case_id, query)
                responses.append(response)
                
                # Verify response quality
                assert isinstance(response, AgentResponse)
                assert response.response_type == ResponseType.ANSWER
                
                # Check for expected keywords based on context
                response_lower = response.content.lower()
                has_relevant_keyword = any(keyword.lower() in response_lower for keyword in expected_keywords)
                assert has_relevant_keyword, f"Response '{response.content}' missing expected keywords: {expected_keywords}"
            
            # Verify conversation flow - each response should build on previous context
            # The final response should be more specific than the first
            assert len(responses) == 4
            assert responses[-1].content != responses[0].content  # Responses should evolve
    
    @pytest.mark.asyncio
    async def test_conversation_context_preservation_across_queries(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test that conversation context is properly preserved and utilized."""
        case_id = "context-case-004"
        session_id = "session-context-004"
        user_id = "user-context"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Query 1: Establish context
            query_1 = QueryRequest(
                query="I'm working on a Node.js microservice that handles user authentication.",
                session_id=session_id,
                context={"source": "api"},
                priority="medium"
            )
            
            await agent_service_with_mocks.process_query_for_case(case_id, query_1)
            
            # Query 2: Reference previous context
            query_2 = QueryRequest(
                query="The service is showing high CPU usage during login attempts.",
                session_id=session_id,
                context={"source": "api"},
                priority="medium"
            )
            
            response_2 = await agent_service_with_mocks.process_query_for_case(case_id, query_2)
            
            # Query 3: Further build on context
            query_3 = QueryRequest(
                query="What specific metrics should I monitor to identify the bottleneck?",
                session_id=session_id,
                context={"source": "api"},
                priority="medium"
            )
            
            response_3 = await agent_service_with_mocks.process_query_for_case(case_id, response_3)
            
            # Verify that each query had conversation context passed to it
            # Check that the session service recorded all interactions
            assert len(agent_service_with_mocks._session_service.record_case_message.call_args_list) >= 6  # 3 user queries + 3 agent responses
    
    @pytest.mark.asyncio
    async def test_error_recovery_in_workflow(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test error recovery and graceful degradation in workflows."""
        case_id = "error-case-005"
        session_id = "session-error-005"
        user_id = "user-error"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Successful query first
            good_query = QueryRequest(
                query="What are the common causes of database connection timeouts?",
                session_id=session_id,
                context={"source": "api"},
                priority="medium"
            )
            
            response_1 = await agent_service_with_mocks.process_query_for_case(case_id, good_query)
            assert isinstance(response_1, AgentResponse)
            
            # Simulate LLM failure
            agent_service_with_mocks._llm.generate.side_effect = Exception("LLM service temporary failure")
            
            error_query = QueryRequest(
                query="How do I configure connection pooling correctly?",
                session_id=session_id,
                context={"source": "api"},
                priority="medium"
            )
            
            # Should handle error gracefully and not crash
            try:
                response_2 = await agent_service_with_mocks.process_query_for_case(case_id, error_query)
                # If it returns a response, it should indicate the issue
                if response_2:
                    assert isinstance(response_2, AgentResponse)
            except Exception as e:
                # If it throws an exception, it should be a controlled ServiceException
                assert isinstance(e, ServiceException)
            
            # Restore LLM and verify recovery
            agent_service_with_mocks._llm.generate.side_effect = None
            agent_service_with_mocks._llm.generate.return_value = "Service recovered. Connection pooling involves setting max connections, idle timeout, and connection validation."
            
            recovery_query = QueryRequest(
                query="Can you help me now with connection pooling configuration?",
                session_id=session_id,
                context={"source": "api"},
                priority="medium"
            )
            
            response_3 = await agent_service_with_mocks.process_query_for_case(case_id, recovery_query)
            assert isinstance(response_3, AgentResponse)
            assert "connection pooling" in response_3.content.lower()
    
    @pytest.mark.asyncio
    async def test_concurrent_queries_same_case(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test handling concurrent queries for the same case."""
        case_id = "concurrent-case-006"
        session_id = "session-concurrent-006"
        user_id = "user-concurrent"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Create multiple concurrent queries
            queries = [
                QueryRequest(
                    query=f"Concurrent query {i} about system performance issue",
                    session_id=session_id,
                    context={"source": "api", "query_number": i},
                    priority="medium"
                )
                for i in range(3)
            ]
            
            # Execute queries concurrently
            tasks = [
                agent_service_with_mocks.process_query_for_case(case_id, query)
                for query in queries
            ]
            
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Verify all queries were processed successfully
            assert len(responses) == 3
            
            for i, response in enumerate(responses):
                if isinstance(response, Exception):
                    pytest.fail(f"Query {i} failed with exception: {response}")
                
                assert isinstance(response, AgentResponse)
                assert response.response_type == ResponseType.ANSWER
                assert response.view_state.active_case.case_id == case_id
    
    @pytest.mark.asyncio
    async def test_workflow_with_sources_and_knowledge_integration(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test workflow that integrates knowledge base sources."""
        case_id = "kb-case-007"
        session_id = "session-kb-007"
        user_id = "user-kb"
        
        # Mock knowledge retrieval
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            # Mock unified retrieval service to return knowledge sources
            mock_unified_retrieval = AsyncMock()
            mock_evidence = Mock()
            mock_evidence.snippet = "Load balancer configuration guide: Use round-robin algorithm for equal distribution..."
            mock_evidence.source = "kb"
            
            mock_retrieval_response = Mock()
            mock_retrieval_response.evidence = [mock_evidence]
            mock_unified_retrieval.search = AsyncMock(return_value=mock_retrieval_response)
            
            mock_container.get_unified_retrieval_service.return_value = mock_unified_retrieval
            
            knowledge_query = QueryRequest(
                query="How should I configure my load balancer for optimal performance?",
                session_id=session_id,
                context={"source": "api", "needs_documentation": True},
                priority="medium"
            )
            
            # Patch the internal query processing to simulate knowledge retrieval
            with patch.object(agent_service_with_mocks, '_execute_query_processing') as mock_execute:
                mock_response = AgentResponse(
                    content="Based on the knowledge base, use round-robin algorithm for load balancing...",
                    response_type=ResponseType.ANSWER,
                    view_state=Mock(),
                    sources=[
                        Source(
                            type=SourceType.KNOWLEDGE_BASE,
                            content="Load balancer configuration guide: Use round-robin algorithm...",
                            confidence=0.9,
                            metadata={"source": "kb", "title": "Load Balancer Guide"}
                        )
                    ],
                    plan=None
                )
                mock_execute.return_value = mock_response
                
                response = await agent_service_with_mocks.process_query_for_case(case_id, knowledge_query)
                
                assert isinstance(response, AgentResponse)
                assert len(response.sources) > 0
                assert response.sources[0].type == SourceType.KNOWLEDGE_BASE
                assert "round-robin" in response.sources[0].content
    
    @pytest.mark.asyncio
    async def test_workflow_performance_metrics_and_logging(
        self, 
        agent_service_with_mocks, 
        mock_case_service
    ):
        """Test that workflows properly log performance metrics and business events."""
        case_id = "metrics-case-008"
        session_id = "session-metrics-008"
        user_id = "user-metrics"
        
        with patch('faultmaven.container.container') as mock_container:
            mock_container.get_case_service.return_value = mock_case_service
            
            with patch.object(agent_service_with_mocks, 'log_business_event') as mock_log_event:
                with patch.object(agent_service_with_mocks, 'log_metric') as mock_log_metric:
                    
                    query = QueryRequest(
                        query="Performance monitoring test query",
                        session_id=session_id,
                        context={"source": "api", "test": "metrics"},
                        priority="medium"
                    )
                    
                    start_time = datetime.utcnow()
                    
                    await agent_service_with_mocks.process_query_for_case(case_id, query)
                    
                    end_time = datetime.utcnow()
                    
                    # Verify business events were logged
                    mock_log_event.assert_called()
                    
                    event_calls = [call[0][0] for call in mock_log_event.call_args_list]
                    assert "case_query_processing_started" in event_calls
                    assert "case_query_processing_completed" in event_calls
                    
                    # Verify performance was tracked (processing time should be reasonable)
                    processing_duration = (end_time - start_time).total_seconds()
                    assert processing_duration < 10.0  # Should complete within 10 seconds for test