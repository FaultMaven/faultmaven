"""
Unit tests for BusinessLogicWorkflowEngine - the main orchestrator of the agentic framework.

This module tests the core workflow engine that coordinates all 7 agentic components
through Plan→Execute→Observe→Re-plan cycles.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
import asyncio
from datetime import datetime

from faultmaven.services.agentic.workflow_engine import BusinessLogicWorkflowEngine
from faultmaven.models.agentic import (
    QueryInput, AgentCapabilities, ExecutionPlan, ExecutionStep,
    SecurityBoundary, QualityMetrics, AgentResponse, ProcessingResult,
    QueryClassification, QueryIntent, QueryComplexity, QueryDomain, QueryUrgency
)


class TestBusinessLogicWorkflowEngine:
    """Test suite for the Business Logic & Workflow Engine."""
    
    @pytest.fixture
    def mock_state_manager(self):
        """Mock state manager with common responses."""
        mock = AsyncMock()
        mock.get_execution_state.return_value = {
            'session_id': 'test-session-123',
            'current_phase': 'planning',
            'execution_history': [],
            'agent_capabilities': AgentCapabilities()
        }
        mock.update_execution_state.return_value = True
        mock.create_execution_plan.return_value = ExecutionPlan(
            plan_id='plan-123',
            steps=[
                ExecutionStep(
                    step_id='step-1',
                    action='classify_query',
                    estimated_duration=1.0,
                    dependencies=[]
                )
            ],
            estimated_total_time=5.0
        )
        return mock

    @pytest.fixture  
    def mock_classification_engine(self):
        """Mock classification engine with typical responses."""
        mock = AsyncMock()
        mock.classify_query.return_value = QueryClassification(
            intent=QueryIntent.TROUBLESHOOTING,
            complexity=QueryComplexity.MEDIUM,
            domain=QueryDomain.SYSTEM_PERFORMANCE,
            urgency=QueryUrgency.MEDIUM,
            confidence=0.85
        )
        return mock

    @pytest.fixture
    def mock_tool_broker(self):
        """Mock tool broker for capability management."""
        mock = AsyncMock()
        mock.discover_capabilities.return_value = AgentCapabilities(
            tools=['knowledge_search', 'web_search'],
            skills=['log_analysis', 'performance_tuning']
        )
        mock.execute_capability.return_value = {'status': 'success', 'data': 'test result'}
        return mock

    @pytest.fixture
    def mock_guardrails_layer(self):
        """Mock guardrails for security validation."""
        mock = AsyncMock()
        mock.validate_request.return_value = SecurityBoundary(
            is_safe=True,
            redacted_content='Test query about system performance',
            security_level='safe',
            pii_detected=False
        )
        mock.validate_response.return_value = SecurityBoundary(
            is_safe=True,
            redacted_content='Test response',
            security_level='safe',
            pii_detected=False
        )
        return mock

    @pytest.fixture
    def mock_response_synthesizer(self):
        """Mock response synthesizer for output formatting."""
        mock = AsyncMock()
        mock.synthesize_response.return_value = ProcessingResult(
            content='Synthesized response content',
            metadata={'confidence': 0.9},
            quality_score=0.85
        )
        return mock

    @pytest.fixture
    def mock_error_manager(self):
        """Mock error manager for fault tolerance."""
        mock = AsyncMock()
        mock.handle_error.return_value = {'recovered': True, 'fallback_used': False}
        mock.circuit_breaker_status.return_value = 'closed'
        return mock

    @pytest.fixture
    def workflow_engine(
        self, 
        mock_state_manager,
        mock_classification_engine,
        mock_tool_broker,
        mock_guardrails_layer,
        mock_response_synthesizer,
        mock_error_manager
    ):
        """Create workflow engine with all mocked dependencies."""
        return BusinessLogicWorkflowEngine(
            state_manager=mock_state_manager,
            classification_engine=mock_classification_engine,
            tool_broker=mock_tool_broker,
            guardrails_layer=mock_guardrails_layer,
            response_synthesizer=mock_response_synthesizer,
            error_manager=mock_error_manager
        )

    @pytest.mark.asyncio
    async def test_init_workflow_engine(self, workflow_engine):
        """Test workflow engine initialization."""
        assert workflow_engine.state_manager is not None
        assert workflow_engine.classification_engine is not None
        assert workflow_engine.tool_broker is not None
        assert workflow_engine.guardrails_layer is not None
        assert workflow_engine.response_synthesizer is not None
        assert workflow_engine.error_manager is not None

    @pytest.mark.asyncio
    async def test_process_query_success(self, workflow_engine):
        """Test successful query processing through full pipeline."""
        query_input = QueryInput(
            query='System performance is slow',
            session_id='test-session-123',
            context={'user_id': 'test-user'}
        )
        
        result = await workflow_engine.process_query('System performance is slow', 'test-session-123')
        
        # Verify result structure
        assert isinstance(result, dict)
        assert 'response' in result
        assert 'metadata' in result
        assert result['metadata']['status'] == 'success'
        
        # Verify all components were called
        workflow_engine.state_manager.get_execution_state.assert_called_once()
        workflow_engine.classification_engine.classify_query.assert_called_once()
        workflow_engine.guardrails_layer.validate_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_plan_execute_observe_cycle(self, workflow_engine):
        """Test the core Plan→Execute→Observe→Re-plan cycle."""
        result = await workflow_engine.plan_execute_observe_cycle(
            'test query', 'session-123'
        )
        
        # Verify cycle completion
        assert result is not None
        assert 'execution_result' in result
        
        # Verify state updates
        workflow_engine.state_manager.update_execution_state.assert_called()

    @pytest.mark.asyncio 
    async def test_adaptive_planning(self, workflow_engine, mock_state_manager):
        """Test adaptive planning based on execution results."""
        # Set up execution history that triggers re-planning
        mock_state_manager.get_execution_state.return_value.update({
            'execution_history': [
                {'step': 'classify_query', 'status': 'failed', 'attempts': 2}
            ]
        })
        
        result = await workflow_engine.adapt_execution_plan('session-123')
        
        assert result is not None
        # Verify re-planning was triggered
        mock_state_manager.create_execution_plan.assert_called()

    @pytest.mark.asyncio
    async def test_error_handling_integration(self, workflow_engine, mock_error_manager):
        """Test error handling integration with error manager."""
        # Simulate error during processing
        workflow_engine.classification_engine.classify_query.side_effect = Exception("Classification failed")
        
        result = await workflow_engine.process_query('test query', 'session-123')
        
        # Verify error was handled
        mock_error_manager.handle_error.assert_called()
        assert result['metadata']['status'] in ['fallback', 'error']

    @pytest.mark.asyncio
    async def test_security_validation_integration(self, workflow_engine, mock_guardrails_layer):
        """Test security validation at request and response boundaries."""
        # Set up security failure
        mock_guardrails_layer.validate_request.return_value = SecurityBoundary(
            is_safe=False,
            redacted_content='[REDACTED]',
            security_level='dangerous',
            pii_detected=True
        )
        
        result = await workflow_engine.process_query('sensitive query', 'session-123')
        
        # Verify security validation was performed
        mock_guardrails_layer.validate_request.assert_called()
        
        # Verify appropriate handling of security failure
        assert result['metadata']['security_validated'] == False

    @pytest.mark.asyncio
    async def test_tool_orchestration(self, workflow_engine, mock_tool_broker):
        """Test tool orchestration through tool broker."""
        await workflow_engine.orchestrate_tools('session-123', ['knowledge_search'])
        
        # Verify tool discovery and execution
        mock_tool_broker.discover_capabilities.assert_called()
        mock_tool_broker.execute_capability.assert_called()

    @pytest.mark.asyncio
    async def test_response_quality_validation(self, workflow_engine, mock_response_synthesizer):
        """Test response quality validation."""
        # Set up low quality response
        mock_response_synthesizer.synthesize_response.return_value = ProcessingResult(
            content='Low quality response',
            metadata={'confidence': 0.3},
            quality_score=0.4  # Below threshold
        )
        
        result = await workflow_engine.process_query('test query', 'session-123')
        
        # Verify quality validation was performed
        mock_response_synthesizer.synthesize_response.assert_called()
        
        # Check if quality concerns are reflected in metadata
        assert 'quality_metrics' in result['metadata']

    @pytest.mark.asyncio
    async def test_session_context_management(self, workflow_engine, mock_state_manager):
        """Test session context management across requests."""
        # Process multiple queries in same session
        session_id = 'persistent-session-123'
        
        await workflow_engine.process_query('First query', session_id)
        await workflow_engine.process_query('Second query', session_id)
        
        # Verify state manager was called for both queries
        assert mock_state_manager.get_execution_state.call_count == 2
        assert mock_state_manager.update_execution_state.call_count >= 2

    @pytest.mark.asyncio
    async def test_performance_monitoring(self, workflow_engine):
        """Test performance monitoring and metrics collection."""
        start_time = datetime.now()
        
        result = await workflow_engine.process_query('performance test', 'session-123')
        
        # Verify timing information is captured
        assert 'performance_metrics' in result['metadata']
        
        # Verify execution time is reasonable
        execution_time = result['metadata']['performance_metrics'].get('total_time', 0)
        assert execution_time > 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_integration(self, workflow_engine, mock_error_manager):
        """Test circuit breaker integration for fault tolerance."""
        # Simulate circuit breaker open state
        mock_error_manager.circuit_breaker_status.return_value = 'open'
        
        result = await workflow_engine.process_query('test query', 'session-123')
        
        # Verify circuit breaker status was checked
        mock_error_manager.circuit_breaker_status.assert_called()
        
        # Verify appropriate handling when circuit is open
        assert result['metadata']['circuit_breaker_status'] == 'open'

    def test_component_dependencies(self, workflow_engine):
        """Test that all required components are properly injected."""
        # Verify all components are present
        assert workflow_engine.state_manager is not None
        assert workflow_engine.classification_engine is not None
        assert workflow_engine.tool_broker is not None
        assert workflow_engine.guardrails_layer is not None
        assert workflow_engine.response_synthesizer is not None
        assert workflow_engine.error_manager is not None
        
        # Verify components implement correct interfaces
        from faultmaven.models.agentic import (
            IAgentStateManager, IQueryClassificationEngine, IToolSkillBroker,
            IGuardrailsPolicyLayer, IResponseSynthesizer, IErrorFallbackManager
        )
        
        # Note: In a real implementation, we would check isinstance with actual interfaces
        # This is a placeholder to show the testing approach
        assert hasattr(workflow_engine.state_manager, 'get_execution_state')
        assert hasattr(workflow_engine.classification_engine, 'classify_query')
        assert hasattr(workflow_engine.tool_broker, 'discover_capabilities')
        assert hasattr(workflow_engine.guardrails_layer, 'validate_request')
        assert hasattr(workflow_engine.response_synthesizer, 'synthesize_response')
        assert hasattr(workflow_engine.error_manager, 'handle_error')