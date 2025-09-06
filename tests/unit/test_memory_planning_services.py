"""Test module for Memory and Planning Services

This module contains unit tests for the MemoryService and PlanningService
components that are part of the Agentic Framework integration.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timedelta
from typing import Dict, Any, List

from faultmaven.services.memory import MemoryService
from faultmaven.services.planning import PlanningService  
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.exceptions import ValidationException, ServiceException


class TestMemoryService:
    """Test cases for MemoryService functionality."""

    @pytest.fixture
    def mock_llm_provider(self):
        """Mock LLM provider for testing."""
        provider = Mock(spec=ILLMProvider)
        provider.route = AsyncMock()
        return provider

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for testing."""
        settings = Mock()
        settings.memory = Mock()
        settings.memory.max_context_length = 4000
        settings.memory.conversation_window = 10
        settings.memory.summary_threshold = 5
        return settings

    @pytest.fixture
    def memory_service(self, mock_llm_provider, mock_settings):
        """MemoryService instance for testing."""
        return MemoryService(
            llm_provider=mock_llm_provider,
            settings=mock_settings
        )

    @pytest.mark.unit
    def test_memory_service_initialization(self, memory_service):
        """Test MemoryService initialization."""
        assert memory_service is not None
        assert hasattr(memory_service, '_llm_provider')
        assert hasattr(memory_service, '_settings')
        assert hasattr(memory_service, '_conversation_buffer')

    @pytest.mark.unit
    async def test_store_conversation_context(self, memory_service):
        """Test storing conversation context."""
        session_id = "test-session-123"
        user_message = "Database connection timeout"
        agent_response = "I'll help you troubleshoot the database issue"
        
        await memory_service.store_conversation_context(
            session_id=session_id,
            user_message=user_message,
            agent_response=agent_response
        )
        
        # Verify context was stored
        context = await memory_service.get_conversation_context(session_id)
        assert context is not None
        assert session_id in memory_service._conversation_buffer

    @pytest.mark.unit
    async def test_get_conversation_context(self, memory_service):
        """Test retrieving conversation context."""
        session_id = "test-session-456"
        
        # Store some context first
        await memory_service.store_conversation_context(
            session_id=session_id,
            user_message="System is slow",
            agent_response="Let me check the performance metrics"
        )
        
        # Retrieve context
        context = await memory_service.get_conversation_context(session_id)
        
        assert context is not None
        assert 'session_id' in context
        assert 'messages' in context
        assert len(context['messages']) > 0

    @pytest.mark.unit
    async def test_conversation_context_windowing(self, memory_service):
        """Test conversation context windowing to limit memory usage."""
        session_id = "test-session-windowing"
        
        # Store more messages than the window limit
        for i in range(15):  # More than window of 10
            await memory_service.store_conversation_context(
                session_id=session_id,
                user_message=f"Message {i}",
                agent_response=f"Response {i}"
            )
        
        context = await memory_service.get_conversation_context(session_id)
        
        # Should only keep the most recent messages within window
        assert len(context['messages']) <= memory_service._settings.memory.conversation_window * 2  # user + agent messages

    @pytest.mark.unit
    async def test_context_summarization(self, memory_service, mock_llm_provider):
        """Test context summarization when threshold is reached."""
        # Mock LLM response for summarization
        mock_llm_provider.route.return_value = "Summarized conversation: User reported database issues, agent provided troubleshooting steps"
        
        session_id = "test-session-summarization"
        
        # Store messages beyond summary threshold
        for i in range(6):  # More than threshold of 5
            await memory_service.store_conversation_context(
                session_id=session_id,
                user_message=f"Database issue {i}",
                agent_response=f"Troubleshooting step {i}"
            )
        
        # Should trigger summarization
        context = await memory_service.get_conversation_context(session_id)
        
        # Verify LLM was called for summarization
        assert mock_llm_provider.route.called

    @pytest.mark.unit
    async def test_memory_cleanup(self, memory_service):
        """Test memory cleanup functionality."""
        # Store context for multiple sessions
        for i in range(5):
            session_id = f"test-session-{i}"
            await memory_service.store_conversation_context(
                session_id=session_id,
                user_message=f"Test message {i}",
                agent_response=f"Test response {i}"
            )
        
        # Verify contexts are stored
        assert len(memory_service._conversation_buffer) == 5
        
        # Cleanup old contexts
        await memory_service.cleanup_old_contexts(max_age_hours=0)  # Cleanup all
        
        # Verify cleanup occurred
        assert len(memory_service._conversation_buffer) == 0

    @pytest.mark.unit
    async def test_get_relevant_context_for_query(self, memory_service, mock_llm_provider):
        """Test getting relevant context for a specific query."""
        # Mock LLM to return relevance analysis
        mock_llm_provider.route.return_value = "Relevant context: Previous database connection issues"
        
        session_id = "test-session-relevance"
        
        # Store diverse conversation history
        conversations = [
            ("Database timeout", "Check connection pool settings"),
            ("Memory usage high", "Monitor heap size"),
            ("Connection failed", "Verify network connectivity")
        ]
        
        for user_msg, agent_msg in conversations:
            await memory_service.store_conversation_context(
                session_id=session_id,
                user_message=user_msg,
                agent_response=agent_msg
            )
        
        # Get relevant context for new query
        relevant_context = await memory_service.get_relevant_context_for_query(
            session_id=session_id,
            query="Another database connection issue"
        )
        
        assert relevant_context is not None
        assert 'relevant_history' in relevant_context

    @pytest.mark.unit
    async def test_memory_service_error_handling(self, memory_service, mock_llm_provider):
        """Test memory service error handling."""
        # Mock LLM to raise exception
        mock_llm_provider.route.side_effect = Exception("LLM error")
        
        session_id = "test-session-error"
        
        # Should handle errors gracefully
        await memory_service.store_conversation_context(
            session_id=session_id,
            user_message="Test message",
            agent_response="Test response"
        )
        
        # Should still return context even with summarization errors
        context = await memory_service.get_conversation_context(session_id)
        assert context is not None


class TestPlanningService:
    """Test cases for PlanningService functionality."""

    @pytest.fixture
    def mock_llm_provider(self):
        """Mock LLM provider for testing."""
        provider = Mock(spec=ILLMProvider)
        provider.route = AsyncMock()
        return provider

    @pytest.fixture
    def mock_memory_service(self):
        """Mock MemoryService for testing."""
        service = Mock()
        service.get_conversation_context = AsyncMock(return_value={
            'session_id': 'test-session',
            'messages': [],
            'summary': 'Previous troubleshooting context'
        })
        return service

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for testing."""
        settings = Mock()
        settings.planning = Mock()
        settings.planning.max_plan_steps = 10
        settings.planning.planning_timeout = 30
        return settings

    @pytest.fixture
    def planning_service(self, mock_llm_provider, mock_memory_service, mock_settings):
        """PlanningService instance for testing."""
        return PlanningService(
            llm_provider=mock_llm_provider,
            memory_service=mock_memory_service,
            settings=mock_settings
        )

    @pytest.mark.unit
    def test_planning_service_initialization(self, planning_service):
        """Test PlanningService initialization."""
        assert planning_service is not None
        assert hasattr(planning_service, '_llm_provider')
        assert hasattr(planning_service, '_memory_service')
        assert hasattr(planning_service, '_settings')
        assert hasattr(planning_service, '_active_plans')

    @pytest.mark.unit
    async def test_create_troubleshooting_plan(self, planning_service, mock_llm_provider):
        """Test creating a troubleshooting plan."""
        # Mock LLM response with structured plan
        mock_plan_response = """
        {
            "plan_id": "plan-123",
            "steps": [
                {
                    "step_id": 1,
                    "action": "Check system logs",
                    "description": "Review recent error logs for patterns",
                    "estimated_time": "5 minutes",
                    "tools": ["log_analyzer"]
                },
                {
                    "step_id": 2,
                    "action": "Test connectivity",
                    "description": "Verify database connectivity",
                    "estimated_time": "10 minutes", 
                    "tools": ["connectivity_test"]
                }
            ],
            "estimated_total_time": "15 minutes",
            "priority": "high"
        }
        """
        
        mock_llm_provider.route.return_value = mock_plan_response
        
        session_id = "test-session-123"
        problem_description = "Database connection timeout issues"
        
        plan = await planning_service.create_troubleshooting_plan(
            session_id=session_id,
            problem_description=problem_description
        )
        
        assert plan is not None
        assert 'plan_id' in plan
        assert 'steps' in plan
        assert len(plan['steps']) > 0
        
        # Verify plan was stored
        assert session_id in planning_service._active_plans

    @pytest.mark.unit
    async def test_get_current_plan(self, planning_service):
        """Test retrieving current plan for a session."""
        session_id = "test-session-456"
        
        # Create and store a plan
        test_plan = {
            'plan_id': 'test-plan-456',
            'steps': [
                {'step_id': 1, 'action': 'Test step', 'status': 'pending'}
            ],
            'status': 'active'
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Retrieve plan
        current_plan = await planning_service.get_current_plan(session_id)
        
        assert current_plan is not None
        assert current_plan['plan_id'] == 'test-plan-456'

    @pytest.mark.unit
    async def test_update_plan_step_status(self, planning_service):
        """Test updating plan step status."""
        session_id = "test-session-update"
        
        # Setup plan with steps
        test_plan = {
            'plan_id': 'test-plan-update',
            'steps': [
                {'step_id': 1, 'action': 'First step', 'status': 'pending'},
                {'step_id': 2, 'action': 'Second step', 'status': 'pending'}
            ],
            'current_step': 1
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Update step status
        await planning_service.update_plan_step_status(
            session_id=session_id,
            step_id=1,
            status='completed',
            results='Step completed successfully'
        )
        
        # Verify status was updated
        updated_plan = await planning_service.get_current_plan(session_id)
        step_1 = next(s for s in updated_plan['steps'] if s['step_id'] == 1)
        assert step_1['status'] == 'completed'

    @pytest.mark.unit
    async def test_advance_to_next_step(self, planning_service):
        """Test advancing to next step in plan."""
        session_id = "test-session-advance"
        
        # Setup multi-step plan
        test_plan = {
            'plan_id': 'test-plan-advance',
            'steps': [
                {'step_id': 1, 'action': 'First step', 'status': 'completed'},
                {'step_id': 2, 'action': 'Second step', 'status': 'pending'},
                {'step_id': 3, 'action': 'Third step', 'status': 'pending'}
            ],
            'current_step': 1
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Advance to next step
        next_step = await planning_service.advance_to_next_step(session_id)
        
        assert next_step is not None
        assert next_step['step_id'] == 2
        
        # Verify current step was updated
        updated_plan = await planning_service.get_current_plan(session_id)
        assert updated_plan['current_step'] == 2

    @pytest.mark.unit
    async def test_adapt_plan_based_on_results(self, planning_service, mock_llm_provider):
        """Test adapting plan based on intermediate results."""
        # Mock LLM response for plan adaptation
        adapted_plan_response = """
        {
            "adapted_steps": [
                {
                    "step_id": 3,
                    "action": "Check memory usage", 
                    "description": "Based on log analysis, check memory consumption",
                    "priority": "high"
                }
            ],
            "modifications": ["Added memory check based on findings"]
        }
        """
        
        mock_llm_provider.route.return_value = adapted_plan_response
        
        session_id = "test-session-adapt"
        step_results = {
            'step_id': 2,
            'findings': 'High memory usage detected in logs',
            'status': 'completed'
        }
        
        # Setup existing plan
        test_plan = {
            'plan_id': 'test-plan-adapt',
            'steps': [
                {'step_id': 1, 'status': 'completed'},
                {'step_id': 2, 'status': 'completed'}
            ]
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Adapt plan
        adapted_plan = await planning_service.adapt_plan_based_on_results(
            session_id=session_id,
            step_results=step_results
        )
        
        assert adapted_plan is not None
        assert 'adapted_steps' in adapted_plan

    @pytest.mark.unit
    async def test_get_plan_progress(self, planning_service):
        """Test getting plan progress and completion status."""
        session_id = "test-session-progress"
        
        # Setup plan with mixed completion status
        test_plan = {
            'plan_id': 'test-plan-progress',
            'steps': [
                {'step_id': 1, 'status': 'completed'},
                {'step_id': 2, 'status': 'completed'},
                {'step_id': 3, 'status': 'in_progress'},
                {'step_id': 4, 'status': 'pending'}
            ],
            'current_step': 3
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Get progress
        progress = await planning_service.get_plan_progress(session_id)
        
        assert progress is not None
        assert 'completed_steps' in progress
        assert 'total_steps' in progress
        assert 'completion_percentage' in progress
        assert progress['completed_steps'] == 2
        assert progress['total_steps'] == 4
        assert progress['completion_percentage'] == 50.0

    @pytest.mark.unit
    async def test_complete_plan(self, planning_service):
        """Test marking plan as completed."""
        session_id = "test-session-complete"
        
        # Setup plan
        test_plan = {
            'plan_id': 'test-plan-complete',
            'status': 'active',
            'steps': [
                {'step_id': 1, 'status': 'completed'},
                {'step_id': 2, 'status': 'completed'}
            ]
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Complete plan
        completion_summary = await planning_service.complete_plan(
            session_id=session_id,
            final_results="All troubleshooting steps completed successfully"
        )
        
        assert completion_summary is not None
        
        # Verify plan status updated
        completed_plan = await planning_service.get_current_plan(session_id)
        assert completed_plan['status'] == 'completed'

    @pytest.mark.unit
    async def test_planning_service_integration_with_memory(self, planning_service, mock_memory_service):
        """Test integration between PlanningService and MemoryService."""
        session_id = "test-session-memory-integration"
        
        # Mock memory service to return relevant context
        mock_memory_service.get_conversation_context.return_value = {
            'session_id': session_id,
            'summary': 'User previously reported database timeouts',
            'relevant_history': ['connection pool issues', 'network latency']
        }
        
        # Create plan that should use memory context
        await planning_service.create_troubleshooting_plan(
            session_id=session_id,
            problem_description="Database performance issues"
        )
        
        # Verify memory service was consulted
        mock_memory_service.get_conversation_context.assert_called_once_with(session_id)

    @pytest.mark.unit
    async def test_planning_service_error_handling(self, planning_service, mock_llm_provider):
        """Test planning service error handling."""
        # Mock LLM to raise exception
        mock_llm_provider.route.side_effect = Exception("LLM planning error")
        
        session_id = "test-session-error"
        
        # Should handle errors gracefully
        with pytest.raises(ServiceException):
            await planning_service.create_troubleshooting_plan(
                session_id=session_id,
                problem_description="Test problem"
            )

    @pytest.mark.unit
    async def test_plan_state_management(self, planning_service):
        """Test plan state management across multiple operations."""
        session_id = "test-session-state"
        
        # Create plan
        test_plan = {
            'plan_id': 'test-plan-state',
            'steps': [
                {'step_id': 1, 'status': 'pending'},
                {'step_id': 2, 'status': 'pending'}
            ],
            'current_step': 1,
            'status': 'active'
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Perform multiple state changes
        await planning_service.update_plan_step_status(session_id, 1, 'in_progress')
        await planning_service.update_plan_step_status(session_id, 1, 'completed')
        await planning_service.advance_to_next_step(session_id)
        
        # Verify final state
        final_plan = await planning_service.get_current_plan(session_id)
        assert final_plan['current_step'] == 2
        step_1 = next(s for s in final_plan['steps'] if s['step_id'] == 1)
        assert step_1['status'] == 'completed'

    @pytest.mark.unit
    async def test_planning_data_integration_with_view_state(self, planning_service):
        """Test planning data integration with ViewState."""
        session_id = "test-session-viewstate"
        
        # Setup plan with rich data
        test_plan = {
            'plan_id': 'test-plan-viewstate',
            'steps': [
                {
                    'step_id': 1,
                    'action': 'Analyze logs',
                    'status': 'completed',
                    'results': 'Found 5 errors in last hour'
                },
                {
                    'step_id': 2, 
                    'action': 'Check metrics',
                    'status': 'in_progress'
                }
            ],
            'insights': ['High error rate detected', 'Performance degradation pattern'],
            'recommendations': ['Scale up resources', 'Check network connectivity']
        }
        planning_service._active_plans[session_id] = test_plan
        
        # Get view state data
        view_state_data = await planning_service.get_plan_view_state_data(session_id)
        
        assert view_state_data is not None
        assert 'current_step' in view_state_data
        assert 'progress' in view_state_data
        assert 'insights' in view_state_data
        assert 'recommendations' in view_state_data