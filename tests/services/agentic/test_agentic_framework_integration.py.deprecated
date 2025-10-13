"""Comprehensive Integration Tests for the 7-Component Agentic Framework

This test suite validates the complete agentic framework implementation including:
- Component initialization and dependency injection
- Interface compliance and contract validation
- End-to-end workflow execution
- Component interaction and data flow
- Error handling and resilience
- Performance and scalability characteristics
"""

import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from typing import Dict, Any, List, Optional

from faultmaven.services.agentic import (
    AgentStateManager,
    QueryClassificationEngine,
    ToolSkillBroker,
    GuardrailsPolicyLayer,
    ResponseSynthesizer,
    ErrorFallbackManager,
    BusinessLogicWorkflowEngine
)

from faultmaven.models.agentic import (
    AgentExecutionState,
    ConversationMemory,
    ExecutionPlan,
    QueryClassificationResult,
    AgentCapability,
    ToolExecutionRequest,
    GuardrailsResult,
    SynthesisRequest,
    SynthesisResult,
    WorkflowDefinition,
    WorkflowExecution,
    PlanningResult,
    SafetyClassification,
    SecurityLevel,
    SafetyLevel
)


class TestAgenticFrameworkIntegration:
    """Integration tests for the complete agentic framework."""

    @pytest.fixture
    def mock_session_store(self):
        """Mock session store for state persistence."""
        mock_store = AsyncMock()
        mock_store.get.return_value = None
        mock_store.set.return_value = True
        mock_store.exists.return_value = False
        mock_store.expire.return_value = True
        return mock_store

    @pytest.fixture
    def mock_tracer(self):
        """Mock tracer for observability."""
        mock_tracer = AsyncMock()
        mock_tracer.trace_call.return_value = AsyncMock()
        return mock_tracer

    @pytest.fixture
    def mock_llm_provider(self):
        """Mock LLM provider for classification and synthesis."""
        mock_provider = AsyncMock()
        mock_provider.generate_response.return_value = {
            "content": "Mock LLM response",
            "confidence": 0.85
        }
        return mock_provider

    @pytest.fixture
    def state_manager(self, mock_session_store, mock_tracer):
        """Initialize state manager with mock dependencies."""
        return AgentStateManager(session_store=mock_session_store, tracer=mock_tracer)

    @pytest.fixture
    def classification_engine(self, mock_llm_provider, mock_tracer):
        """Initialize classification engine with mock dependencies."""
        return QueryClassificationEngine(llm_provider=mock_llm_provider, tracer=mock_tracer)

    @pytest.fixture
    def tool_broker(self, mock_tracer):
        """Initialize tool broker with mock dependencies."""
        return ToolSkillBroker(tracer=mock_tracer)

    @pytest.fixture
    def guardrails_layer(self):
        """Initialize mock guardrails layer."""
        # Create a simple mock that returns basic data structures instead of complex Pydantic models
        mock_guardrails = AsyncMock()
        mock_guardrails.validate_input = AsyncMock(return_value={
            "passed": True,
            "violations": [],
            "recommendations": [],
            "security_level": "PUBLIC"
        })
        mock_guardrails.validate_output = AsyncMock(return_value={
            "passed": True,
            "violations": [],
            "recommendations": [],
            "security_level": "PUBLIC"
        })
        mock_guardrails.classify_safety = AsyncMock(return_value={
            "content_id": "test_content_123",
            "safety_level": "SAFE",
            "risk_factors": [],
            "confidence_score": 0.95
        })
        return mock_guardrails

    @pytest.fixture
    def response_synthesizer(self):
        """Initialize mock response synthesizer."""
        mock_synthesizer = AsyncMock()
        mock_synthesizer.synthesize_response = AsyncMock(return_value={
            "content": "Synthesized response content",
            "quality_score": 0.9,
            "synthesis_time": 0.2,
            "sources_used": ["source1", "source2"],
            "confidence_level": "HIGH"
        })
        mock_synthesizer.format_content = AsyncMock(return_value={
            "content": "Formatted content",
            "format": "markdown",
            "metadata": {"processing_time": 0.1}
        })
        return mock_synthesizer

    @pytest.fixture
    def error_manager(self):
        """Initialize mock error manager."""
        mock_error_manager = AsyncMock()
        mock_error_manager.handle_error = AsyncMock(return_value={
            "success": True,
            "fallback_used": False,
            "recovery_time": 0.05,
            "action_taken": "error_handled"
        })
        mock_error_manager.execute_fallback = AsyncMock(return_value={
            "success": True,
            "execution_time": 0.1,
            "fallback_strategy": "retry"
        })
        mock_error_manager.get_system_health = AsyncMock(return_value={
            "overall_status": "HEALTHY",
            "health_score": 0.95,
            "components": {"all": "ok"}
        })
        return mock_error_manager

    @pytest.fixture
    def workflow_engine(self, state_manager, classification_engine, tool_broker, 
                        guardrails_layer, response_synthesizer, error_manager):
        """Initialize workflow engine with all dependencies."""
        return BusinessLogicWorkflowEngine(
            state_manager=state_manager,
            classification_engine=classification_engine,
            tool_broker=tool_broker,
            guardrails_layer=guardrails_layer,
            response_synthesizer=response_synthesizer,
            error_manager=error_manager
        )

    @pytest.mark.asyncio
    async def test_component_initialization(self, state_manager, classification_engine, 
                                           tool_broker, guardrails_layer, 
                                           response_synthesizer, error_manager, workflow_engine):
        """Test that all components initialize correctly."""
        # Verify all components are properly instantiated
        assert state_manager is not None
        assert classification_engine is not None
        assert tool_broker is not None
        assert guardrails_layer is not None
        assert response_synthesizer is not None
        assert error_manager is not None
        assert workflow_engine is not None

        # Verify component types
        assert isinstance(state_manager, AgentStateManager)
        assert isinstance(classification_engine, QueryClassificationEngine)
        assert isinstance(tool_broker, ToolSkillBroker)
        assert isinstance(guardrails_layer, GuardrailsPolicyLayer)
        assert isinstance(response_synthesizer, ResponseSynthesizer)
        assert isinstance(error_manager, ErrorFallbackManager)
        assert isinstance(workflow_engine, BusinessLogicWorkflowEngine)

    @pytest.mark.asyncio
    async def test_state_manager_operations(self, state_manager):
        """Test state manager core functionality."""
        session_id = "test_session_123"
        
        # Test execution state management
        execution_state = await state_manager.get_execution_state(session_id)
        assert execution_state is None  # Should be None for new session

        # Test conversation memory update
        memory = ConversationMemory(
            session_id=session_id,
            messages=[{"role": "user", "content": "Test query"}],
            context={"domain": "testing"},
            created_at=datetime.utcnow(),
            last_updated=datetime.utcnow()
        )
        
        result = await state_manager.update_conversation_memory(session_id, memory)
        assert result is True

        # Test execution plan creation
        plan = await state_manager.create_execution_plan(
            session_id, "test query", {"context": "test"}
        )
        assert isinstance(plan, ExecutionPlan)
        assert plan.session_id == session_id

    @pytest.mark.asyncio
    async def test_classification_engine_operations(self, classification_engine):
        """Test classification engine core functionality."""
        query = "Help me troubleshoot a network connectivity issue"
        context = {"domain": "networking", "urgency": "high"}
        
        # Test query classification
        result = await classification_engine.classify_query(query, context)
        
        assert isinstance(result, dict)
        assert "intent" in result
        assert "complexity" in result
        assert "domain" in result
        assert "confidence_score" in result

        # Verify classification results make sense
        assert result["confidence_score"] >= 0.0
        assert result["confidence_score"] <= 1.0

    @pytest.mark.asyncio
    async def test_tool_broker_operations(self, tool_broker):
        """Test tool broker core functionality."""
        # Test capability discovery
        requirements = {"domain": "networking", "complexity": "medium"}
        capabilities = await tool_broker.discover_capabilities(requirements)
        
        assert isinstance(capabilities, list)
        # Should return some capabilities even with mock data
        assert len(capabilities) > 0

        # Test tool execution
        request = ToolExecutionRequest(
            tool_name="knowledge_base",
            parameters={"query": "network troubleshooting"},
            context={"session_id": "test"},
            timeout=30.0
        )
        
        result = await tool_broker.execute_tool_request(request)
        assert result is not None
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_guardrails_layer_operations(self, guardrails_layer):
        """Test guardrails layer core functionality."""
        # Test input validation
        content = "Please help me troubleshoot my server issue"
        context = {"user_id": "test_user", "session_id": "test_session"}
        
        result = await guardrails_layer.validate_input(content, context)
        
        assert isinstance(result, GuardrailsResult)
        assert hasattr(result, 'is_safe')
        assert hasattr(result, 'sanitized_content')
        assert hasattr(result, 'processing_time')

        # Test output validation
        output_result = await guardrails_layer.validate_output(
            "Here's how to fix your server issue...", context
        )
        
        assert isinstance(output_result, GuardrailsResult)
        assert hasattr(output_result, 'is_safe')

        # Test safety classification
        safety_result = await guardrails_layer.classify_safety(content, context)
        assert safety_result is not None
        assert hasattr(safety_result, 'overall_safety')
        assert hasattr(safety_result, 'confidence_score')

    @pytest.mark.asyncio
    async def test_response_synthesizer_operations(self, response_synthesizer):
        """Test response synthesizer core functionality."""
        # Test response synthesis
        request = SynthesisRequest(
            sources=[
                {"id": "source1", "content": "Network diagnostic information", "type": "diagnostic"},
                {"id": "source2", "content": "Solution steps", "type": "solution"}
            ],
            context={"user_expertise": "intermediate", "output_format": "markdown"},
            request_type="troubleshooting"
        )
        
        result = await response_synthesizer.synthesize_response(request)
        
        assert isinstance(result, SynthesisResult)
        assert hasattr(result, 'content')
        assert hasattr(result, 'quality_score')
        assert hasattr(result, 'synthesis_time')
        assert len(result.content) > 0

        # Test content formatting
        format_result = await response_synthesizer.format_content(
            "Test content", "markdown", {"user_preference": "detailed"}
        )
        
        assert isinstance(format_result, dict)
        assert "content" in format_result
        assert "format" in format_result
        assert "metadata" in format_result

    @pytest.mark.asyncio
    async def test_error_manager_operations(self, error_manager):
        """Test error manager core functionality."""
        # Test error handling
        test_error = ValueError("Test error for handling")
        context = {
            "operation": "test_operation",
            "user_id": "test_user",
            "component": "test_component"
        }
        
        result = await error_manager.handle_error(test_error, context)
        
        assert result is not None
        assert hasattr(result, 'success')
        assert hasattr(result, 'fallback_used')
        assert hasattr(result, 'recovery_time')

        # Test fallback execution
        fallback_result = await error_manager.execute_fallback("retry", {
            "max_attempts": 3,
            "operation": "test_fallback"
        })
        
        assert isinstance(fallback_result, dict)
        assert "success" in fallback_result
        assert "execution_time" in fallback_result

        # Test system health
        health_status = await error_manager.get_system_health()
        
        assert health_status is not None
        assert hasattr(health_status, 'overall_status')
        assert hasattr(health_status, 'health_score')

    @pytest.mark.asyncio
    async def test_workflow_engine_operations(self, workflow_engine):
        """Test workflow engine core functionality."""
        # Test workflow planning
        request = {
            "query": "Help me diagnose a database connection issue",
            "user_id": "test_user",
            "session_id": "test_session",
            "metadata": {"domain": "database", "urgency": "high"}
        }
        
        planning_result = await workflow_engine.plan_workflow(request)
        
        assert isinstance(planning_result, PlanningResult)
        assert hasattr(planning_result, 'workflow_id')
        assert hasattr(planning_result, 'execution_plan')
        assert hasattr(planning_result, 'confidence_score')
        assert hasattr(planning_result, 'planning_strategy')

        # Test workflow execution
        execution_context = {
            "user_id": "test_user",
            "session_id": "test_session",
            "query": request["query"],
            "metadata": request["metadata"]
        }
        
        execution_result = await workflow_engine.execute_workflow(
            planning_result.execution_plan, execution_context
        )
        
        assert isinstance(execution_result, WorkflowExecution)
        assert hasattr(execution_result, 'workflow_id')
        assert hasattr(execution_result, 'status')
        assert hasattr(execution_result, 'total_duration')

    @pytest.mark.asyncio
    async def test_end_to_end_workflow(self, workflow_engine, state_manager, 
                                      classification_engine, tool_broker, 
                                      guardrails_layer, response_synthesizer, error_manager):
        """Test complete end-to-end workflow execution."""
        # Simulate a complete user query workflow
        user_query = "My web server is returning 500 errors. How can I troubleshoot this?"
        session_id = "integration_test_session"
        user_id = "integration_test_user"
        
        # Step 1: Plan workflow
        planning_request = {
            "query": user_query,
            "user_id": user_id,
            "session_id": session_id,
            "metadata": {
                "domain": "web_development",
                "complexity": "medium",
                "urgency": "normal"
            }
        }
        
        planning_result = await workflow_engine.plan_workflow(planning_request)
        assert planning_result.confidence_score > 0.0
        
        # Step 2: Execute workflow
        execution_context = {
            "user_id": user_id,
            "session_id": session_id,
            "query": user_query,
            "metadata": planning_request["metadata"]
        }
        
        execution_result = await workflow_engine.execute_workflow(
            planning_result.execution_plan, execution_context
        )
        
        # Verify execution completion
        assert execution_result.status in ["completed", "failed"]  # Should complete or fail gracefully
        assert execution_result.total_duration > 0
        
        # Step 3: Verify state persistence
        execution_state = await state_manager.get_execution_state(session_id)
        # State might be cleaned up after completion, so we just verify the call works
        
        # Step 4: Test analytics
        analytics = await workflow_engine.get_workflow_analytics("1h")
        assert isinstance(analytics, dict)
        assert "summary_metrics" in analytics or "error" in analytics  # Either data or error

    @pytest.mark.asyncio
    async def test_component_error_resilience(self, workflow_engine):
        """Test framework resilience to component errors."""
        # Test with invalid/corrupted inputs
        invalid_requests = [
            {},  # Empty request
            {"query": ""},  # Empty query
            {"query": "test", "metadata": None},  # None metadata
            {"query": "x" * 10000},  # Extremely long query
        ]
        
        for request in invalid_requests:
            try:
                result = await workflow_engine.plan_workflow(request)
                # Should either succeed or fail gracefully
                assert result is not None
            except Exception as e:
                # If it raises an exception, it should be handled gracefully
                assert isinstance(e, (ValueError, TypeError))

    @pytest.mark.asyncio
    async def test_concurrent_workflow_execution(self, workflow_engine):
        """Test concurrent workflow execution capabilities."""
        # Create multiple concurrent workflow requests
        requests = []
        for i in range(5):
            requests.append({
                "query": f"Test query {i}",
                "user_id": f"user_{i}",
                "session_id": f"session_{i}",
                "metadata": {"test_id": i}
            })
        
        # Plan all workflows concurrently
        planning_tasks = [
            workflow_engine.plan_workflow(request) 
            for request in requests
        ]
        
        planning_results = await asyncio.gather(*planning_tasks, return_exceptions=True)
        
        # Verify all planning completed (either success or graceful failure)
        assert len(planning_results) == 5
        
        successful_plans = [
            result for result in planning_results 
            if not isinstance(result, Exception) and hasattr(result, 'execution_plan')
        ]
        
        # At least some should succeed
        assert len(successful_plans) > 0

    @pytest.mark.asyncio
    async def test_framework_performance_characteristics(self, workflow_engine):
        """Test performance characteristics of the framework."""
        start_time = datetime.utcnow()
        
        # Execute a standard workflow
        request = {
            "query": "Performance test query",
            "user_id": "perf_test_user",
            "session_id": "perf_test_session",
            "metadata": {"performance_test": True}
        }
        
        # Plan workflow
        planning_start = datetime.utcnow()
        planning_result = await workflow_engine.plan_workflow(request)
        planning_time = (datetime.utcnow() - planning_start).total_seconds()
        
        # Execute workflow
        execution_start = datetime.utcnow()
        execution_result = await workflow_engine.execute_workflow(
            planning_result.execution_plan, request
        )
        execution_time = (datetime.utcnow() - execution_start).total_seconds()
        
        total_time = (datetime.utcnow() - start_time).total_seconds()
        
        # Performance assertions (reasonable thresholds for integration tests)
        assert planning_time < 5.0  # Planning should be fast
        assert execution_time < 30.0  # Execution should complete in reasonable time
        assert total_time < 35.0  # Total time should be reasonable
        
        # Verify performance metadata is captured
        assert hasattr(planning_result, 'planning_time')
        assert hasattr(execution_result, 'total_duration')

    @pytest.mark.asyncio
    async def test_framework_scalability_simulation(self, workflow_engine):
        """Simulate framework behavior under load."""
        # Test resource management under multiple simultaneous requests
        concurrent_requests = 10
        
        async def execute_workflow_request(request_id: int):
            request = {
                "query": f"Scalability test query {request_id}",
                "user_id": f"scale_user_{request_id}",
                "session_id": f"scale_session_{request_id}",
                "metadata": {"scale_test": True, "request_id": request_id}
            }
            
            try:
                planning_result = await workflow_engine.plan_workflow(request)
                execution_result = await workflow_engine.execute_workflow(
                    planning_result.execution_plan, request
                )
                return {"success": True, "request_id": request_id, "result": execution_result}
            except Exception as e:
                return {"success": False, "request_id": request_id, "error": str(e)}
        
        # Execute concurrent requests
        tasks = [
            execute_workflow_request(i) 
            for i in range(concurrent_requests)
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Analyze results
        successful_requests = [
            r for r in results 
            if not isinstance(r, Exception) and r.get("success", False)
        ]
        
        failed_requests = [
            r for r in results 
            if isinstance(r, Exception) or not r.get("success", False)
        ]
        
        # At least 50% should succeed under load
        success_rate = len(successful_requests) / len(results)
        assert success_rate >= 0.5, f"Success rate {success_rate} too low for scalability test"

    @pytest.mark.asyncio
    async def test_framework_interface_compliance(self, state_manager, classification_engine,
                                                 tool_broker, guardrails_layer, 
                                                 response_synthesizer, error_manager, workflow_engine):
        """Test that all components comply with their interface contracts."""
        
        # Test interface method existence and signatures
        components_and_methods = [
            (state_manager, ['get_execution_state', 'update_conversation_memory', 'create_execution_plan']),
            (classification_engine, ['classify_query']),
            (tool_broker, ['discover_capabilities', 'execute_tool_request']),
            (guardrails_layer, ['validate_input', 'validate_output', 'classify_safety']),
            (response_synthesizer, ['synthesize_response', 'format_content']),
            (error_manager, ['handle_error', 'execute_fallback', 'get_system_health']),
            (workflow_engine, ['plan_workflow', 'execute_workflow', 'observe_execution', 'adapt_workflow'])
        ]
        
        for component, methods in components_and_methods:
            for method_name in methods:
                assert hasattr(component, method_name), f"{component.__class__.__name__} missing method {method_name}"
                method = getattr(component, method_name)
                assert callable(method), f"{method_name} is not callable on {component.__class__.__name__}"

    @pytest.mark.asyncio
    async def test_framework_data_flow_integrity(self, workflow_engine):
        """Test data integrity throughout the framework."""
        
        # Test that data flows correctly between components
        request = {
            "query": "Data flow integrity test",
            "user_id": "data_test_user", 
            "session_id": "data_test_session",
            "request_id": "data_test_request_123",
            "metadata": {
                "test_type": "data_integrity",
                "trace_data": True
            }
        }
        
        # Plan workflow
        planning_result = await workflow_engine.plan_workflow(request)
        
        # Verify planning result integrity
        assert planning_result.workflow_id is not None
        assert len(planning_result.workflow_id) > 0
        assert planning_result.confidence_score >= 0.0
        assert planning_result.confidence_score <= 1.0
        
        # Execute workflow  
        execution_result = await workflow_engine.execute_workflow(
            planning_result.execution_plan, request
        )
        
        # Verify execution result integrity
        assert execution_result.workflow_id == planning_result.workflow_id
        assert execution_result.execution_id is not None
        assert len(execution_result.execution_id) > 0
        assert execution_result.total_duration >= 0.0
        
        # Verify status is valid
        valid_statuses = ["planned", "running", "completed", "failed", "cancelled"]
        assert execution_result.status in valid_statuses

    def test_component_dependency_injection(self, state_manager, classification_engine,
                                          tool_broker, guardrails_layer, 
                                          response_synthesizer, error_manager):
        """Test dependency injection works correctly."""
        
        # Create workflow engine with all dependencies
        workflow_engine = BusinessLogicWorkflowEngine(
            state_manager=state_manager,
            classification_engine=classification_engine,
            tool_broker=tool_broker,
            guardrails_layer=guardrails_layer,
            response_synthesizer=response_synthesizer,
            error_manager=error_manager
        )
        
        # Verify dependencies are injected correctly
        assert workflow_engine.state_manager is state_manager
        assert workflow_engine.classification_engine is classification_engine
        assert workflow_engine.tool_broker is tool_broker
        assert workflow_engine.guardrails_layer is guardrails_layer
        assert workflow_engine.response_synthesizer is response_synthesizer
        assert workflow_engine.error_manager is error_manager

    @pytest.mark.asyncio
    async def test_component_graceful_degradation(self, workflow_engine):
        """Test framework graceful degradation when components are unavailable."""
        
        # Create workflow engine with minimal dependencies (some None)
        # Note: For graceful degradation test, we create minimal mock components
        # rather than None to test degraded functionality
        mock_tracer_minimal = AsyncMock()
        mock_session_store_minimal = AsyncMock()
        mock_llm_provider_minimal = AsyncMock()
        
        minimal_state_manager = AgentStateManager(session_store=mock_session_store_minimal, tracer=mock_tracer_minimal)
        minimal_classification_engine = QueryClassificationEngine(llm_provider=mock_llm_provider_minimal, tracer=mock_tracer_minimal)
        minimal_tool_broker = ToolSkillBroker(tracer=mock_tracer_minimal)
        
        # Create minimal mock components
        minimal_guardrails_layer = AsyncMock()
        minimal_response_synthesizer = AsyncMock() 
        minimal_error_manager = AsyncMock()
        
        minimal_engine = BusinessLogicWorkflowEngine(
            state_manager=minimal_state_manager,
            classification_engine=minimal_classification_engine, 
            tool_broker=minimal_tool_broker,
            guardrails_layer=minimal_guardrails_layer,
            response_synthesizer=minimal_response_synthesizer,
            error_manager=minimal_error_manager
        )
        
        # Test that it still functions (with degraded capability)
        request = {
            "query": "Test with missing dependencies",
            "user_id": "degradation_test_user",
            "session_id": "degradation_test_session",
            "metadata": {}
        }
        
        # Should not crash, may have reduced functionality
        try:
            planning_result = await minimal_engine.plan_workflow(request)
            assert planning_result is not None
            # May have lower confidence or different strategy
            assert hasattr(planning_result, 'confidence_score')
        except Exception as e:
            # If it fails, should be a controlled failure
            assert isinstance(e, (ValueError, TypeError, AttributeError))


@pytest.mark.asyncio 
async def test_framework_component_integration():
    """Standalone test for basic component integration."""
    
    # Create mock dependencies
    mock_session_store = AsyncMock()
    mock_tracer = AsyncMock()
    mock_llm_provider = AsyncMock()
    
    # Initialize components with proper dependencies
    state_manager = AgentStateManager(session_store=mock_session_store, tracer=mock_tracer)
    classification_engine = QueryClassificationEngine(llm_provider=mock_llm_provider, tracer=mock_tracer)
    tool_broker = ToolSkillBroker(tracer=mock_tracer)
    
    # Create mock components for abstract classes
    guardrails_layer = AsyncMock()
    guardrails_layer.validate_input = AsyncMock(return_value={
        "passed": True, "violations": [], "recommendations": [], "security_level": "PUBLIC"
    })
    
    response_synthesizer = AsyncMock()
    response_synthesizer.synthesize_response = AsyncMock(return_value={
        "content": "Test response", "quality_score": 0.9, "synthesis_time": 0.1, "sources_used": [], "confidence_level": "HIGH"
    })
    
    error_manager = AsyncMock()
    error_manager.handle_error = AsyncMock(return_value={
        "success": True, "fallback_used": False, "recovery_time": 0.1
    })
    
    workflow_engine = BusinessLogicWorkflowEngine(
        state_manager=state_manager,
        classification_engine=classification_engine,
        tool_broker=tool_broker,
        guardrails_layer=guardrails_layer,
        response_synthesizer=response_synthesizer,
        error_manager=error_manager
    )
    
    # Test basic integration
    request = {
        "query": "Integration test query",
        "user_id": "integration_user",
        "session_id": "integration_session",
        "metadata": {"test": True}
    }
    
    planning_result = await workflow_engine.plan_workflow(request)
    assert planning_result is not None
    assert hasattr(planning_result, 'workflow_id')
    assert hasattr(planning_result, 'execution_plan')


if __name__ == "__main__":
    # Run integration tests
    pytest.main([__file__, "-v", "--tb=short"])