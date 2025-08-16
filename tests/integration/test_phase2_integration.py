"""Comprehensive Phase 2 Integration Tests

This module contains comprehensive integration tests that validate all Phase 2 
components (memory, planning, reasoning, knowledge, and orchestration) work 
together properly as a cohesive intelligent troubleshooting platform.

Key Test Areas:
- End-to-end workflow integration
- Service-to-service communication
- Cross-session knowledge sharing
- Memory-driven decision making
- Performance under load
- Error recovery and resilience
- Data flow validation
- API endpoint integration
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from unittest.mock import Mock, AsyncMock, patch
import pytest

from faultmaven.services.orchestration_service import OrchestrationService
from faultmaven.services.enhanced_knowledge_service import EnhancedKnowledgeService
from faultmaven.services.memory_service import MemoryService
from faultmaven.services.planning_service import PlanningService
from faultmaven.services.reasoning_service import ReasoningService
from faultmaven.core.orchestration.troubleshooting_orchestrator import (
    TroubleshootingOrchestrator, WorkflowContext, TroubleshootingPhase
)
from faultmaven.models.interfaces import (
    IMemoryService, IPlanningService, ILLMProvider, ITracer, IVectorStore
)
from faultmaven.exceptions import ServiceException, ValidationException


@pytest.fixture
async def mock_vector_store():
    """Mock vector store for testing"""
    vector_store = Mock()
    vector_store.search = AsyncMock(return_value=[
        {
            "id": "doc_1",
            "content": "Database connection timeout troubleshooting guide",
            "metadata": {"source": "troubleshooting.md", "type": "guide"},
            "score": 0.9
        },
        {
            "id": "doc_2", 
            "content": "Performance optimization best practices",
            "metadata": {"source": "performance.md", "type": "guide"},
            "score": 0.8
        }
    ])
    return vector_store


@pytest.fixture
async def mock_llm_provider():
    """Mock LLM provider for testing"""
    llm = Mock()
    llm.generate = AsyncMock(return_value={
        "response": "AI-generated troubleshooting analysis",
        "confidence": 0.85,
        "reasoning": "Based on symptoms, likely database connectivity issue"
    })
    return llm


@pytest.fixture
async def mock_tracer():
    """Mock tracer for testing"""
    tracer = Mock()
    tracer.start_span = Mock(return_value=Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=None)))
    return tracer


@pytest.fixture
async def integrated_memory_service(mock_llm_provider):
    """Memory service with mocked dependencies"""
    memory_service = MemoryService(
        llm_provider=mock_llm_provider,
        tracer=None
    )
    return memory_service


@pytest.fixture 
async def integrated_planning_service(mock_llm_provider):
    """Planning service with mocked dependencies"""
    planning_service = PlanningService(
        llm_provider=mock_llm_provider,
        tracer=None
    )
    return planning_service


@pytest.fixture
async def integrated_reasoning_service(mock_llm_provider):
    """Reasoning service with mocked dependencies"""
    reasoning_service = ReasoningService(
        llm_provider=mock_llm_provider,
        tracer=None
    )
    return reasoning_service


@pytest.fixture
async def integrated_knowledge_service(mock_vector_store, integrated_memory_service, mock_llm_provider):
    """Enhanced knowledge service with mocked dependencies"""
    knowledge_service = EnhancedKnowledgeService(
        vector_store=mock_vector_store,
        memory_service=integrated_memory_service,
        llm_provider=mock_llm_provider,
        sanitizer=None,
        tracer=None
    )
    return knowledge_service


@pytest.fixture
async def integrated_orchestration_service(
    integrated_memory_service,
    integrated_planning_service, 
    integrated_reasoning_service,
    integrated_knowledge_service,
    mock_llm_provider,
    mock_tracer
):
    """Orchestration service with all integrated dependencies"""
    orchestration_service = OrchestrationService(
        memory_service=integrated_memory_service,
        planning_service=integrated_planning_service,
        reasoning_service=integrated_reasoning_service,
        enhanced_knowledge_service=integrated_knowledge_service,
        llm_provider=mock_llm_provider,
        tracer=mock_tracer
    )
    return orchestration_service


@pytest.fixture
def sample_workflow_context():
    """Sample workflow context for testing"""
    return WorkflowContext(
        session_id="test-session-123",
        case_id="case-456",
        user_id="user-789",
        problem_description="Database connections are timing out frequently",
        initial_context={
            "service_name": "user-api",
            "environment": "production",
            "component": "database",
            "technology": "postgres"
        },
        priority_level="high",
        domain_expertise="intermediate",
        time_constraints=3600,  # 1 hour
        available_tools=["enhanced_knowledge_search", "knowledge_discovery", "web_search"]
    )


class TestPhase2EndToEndIntegration:
    """Test complete end-to-end workflow integration"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_complete_troubleshooting_workflow(
        self, 
        integrated_orchestration_service, 
        sample_workflow_context
    ):
        """Test a complete troubleshooting workflow from creation to completion"""
        # Create workflow
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id=sample_workflow_context.session_id,
            case_id=sample_workflow_context.case_id,
            user_id=sample_workflow_context.user_id,
            problem_description=sample_workflow_context.problem_description,
            context=sample_workflow_context.initial_context,
            priority_level=sample_workflow_context.priority_level,
            domain_expertise=sample_workflow_context.domain_expertise,
            time_constraints=sample_workflow_context.time_constraints
        )
        
        # Validate workflow creation
        assert workflow_result["success"] is True
        assert "workflow_id" in workflow_result
        assert workflow_result["workflow_details"]["total_steps"] > 0
        assert workflow_result["strategic_insights"] is not None
        
        workflow_id = workflow_result["workflow_id"]
        
        # Execute first step
        step_result = await integrated_orchestration_service.execute_workflow_step(
            workflow_id=workflow_id,
            step_inputs={"user_input": "The timeouts started yesterday after deployment"}
        )
        
        # Validate step execution
        assert step_result["success"] is True
        assert step_result["workflow_id"] == workflow_id
        assert "step_execution" in step_result
        assert "workflow_progress" in step_result
        assert step_result["execution_time"] > 0
        
        # Check workflow status
        status_result = await integrated_orchestration_service.get_workflow_status(workflow_id)
        
        # Validate status
        assert status_result["success"] is True
        assert status_result["status"] in ["initialized", "in_progress", "step_completed"]
        assert status_result["progress"]["current_step"] > 0
        assert status_result["findings_summary"]["total_findings"] >= 0
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_workflow_with_memory_enhancement(
        self,
        integrated_orchestration_service,
        integrated_memory_service,
        sample_workflow_context
    ):
        """Test workflow creation and execution with memory enhancement"""
        # Store some memory context first
        await integrated_memory_service.store_interaction(
            session_id=sample_workflow_context.session_id,
            user_input="We had similar database issues last month",
            ai_response="Previous issue was resolved by increasing connection pool size",
            context={
                "issue_type": "database_timeout",
                "resolution": "connection_pool_adjustment",
                "success": True
            }
        )
        
        # Create workflow that should leverage memory
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id=sample_workflow_context.session_id,
            case_id=sample_workflow_context.case_id,
            user_id=sample_workflow_context.user_id,
            problem_description="Database timeouts again, similar to last month",
            context=sample_workflow_context.initial_context,
            priority_level=sample_workflow_context.priority_level,
            domain_expertise=sample_workflow_context.domain_expertise
        )
        
        # Validate memory enhancement
        assert workflow_result["success"] is True
        assert workflow_result["memory_enhancements"] > 0
        assert len(workflow_result["strategic_insights"]) > 0
        
        # Execute step and verify memory context is used
        step_result = await integrated_orchestration_service.execute_workflow_step(
            workflow_id=workflow_result["workflow_id"]
        )
        
        # Check that memory insights influence the workflow
        assert step_result["success"] is True
        assert "recommendations" in step_result
        
    @pytest.mark.asyncio
    @pytest.mark.integration 
    async def test_workflow_pause_resume_cycle(
        self,
        integrated_orchestration_service,
        sample_workflow_context
    ):
        """Test workflow pause and resume functionality"""
        # Create and start workflow
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id=sample_workflow_context.session_id,
            case_id=sample_workflow_context.case_id,
            user_id=sample_workflow_context.user_id,
            problem_description=sample_workflow_context.problem_description,
            context=sample_workflow_context.initial_context
        )
        
        workflow_id = workflow_result["workflow_id"]
        
        # Execute one step
        await integrated_orchestration_service.execute_workflow_step(workflow_id)
        
        # Pause workflow
        pause_result = await integrated_orchestration_service.pause_workflow(
            workflow_id, 
            reason="Taking a break for lunch"
        )
        
        assert pause_result["success"] is True
        assert pause_result["status"] == "paused"
        
        # Check status shows paused
        status_result = await integrated_orchestration_service.get_workflow_status(workflow_id)
        assert "paused" in status_result["status"] or "suspended" in status_result["status"]
        
        # Resume workflow
        resume_result = await integrated_orchestration_service.resume_workflow(workflow_id)
        
        assert resume_result["success"] is True
        assert resume_result["status"] == "resumed"
        
        # Continue execution after resume
        step_result = await integrated_orchestration_service.execute_workflow_step(workflow_id)
        assert step_result["success"] is True


class TestServiceToServiceIntegration:
    """Test integration between individual services"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_memory_planning_integration(
        self,
        integrated_memory_service,
        integrated_planning_service
    ):
        """Test memory service integration with planning service"""
        session_id = "test-session-memory-planning"
        
        # Store memory context
        await integrated_memory_service.store_interaction(
            session_id=session_id,
            user_input="API latency has increased by 50% since deployment",
            ai_response="Typical causes include database performance and resource constraints",
            context={
                "issue_type": "performance_degradation",
                "metric": "api_latency",
                "change_percentage": 50
            }
        )
        
        # Get memory context for planning
        memory_context = await integrated_memory_service.retrieve_context(
            session_id, 
            "Plan response for API performance issue"
        )
        
        assert memory_context is not None
        assert len(memory_context.relevant_insights) > 0
        
        # Use memory context in planning
        plan_result = await integrated_planning_service.plan_response_strategy(
            query="API response times are slow",
            context={
                "memory_context": {
                    "insights": memory_context.relevant_insights,
                    "domain": memory_context.domain_context
                },
                "urgency": "high"
            }
        )
        
        assert plan_result is not None
        assert hasattr(plan_result, 'insights')
        assert len(plan_result.insights) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reasoning_knowledge_integration(
        self,
        integrated_reasoning_service,
        integrated_knowledge_service
    ):
        """Test reasoning service integration with knowledge service"""
        session_id = "test-session-reasoning-knowledge"
        
        # Execute reasoning workflow that should use knowledge
        reasoning_result = await integrated_reasoning_service.execute_reasoning_workflow(
            reasoning_type="diagnostic",
            session_id=session_id,
            context={
                "step_context": {
                    "phase": "formulate_hypothesis",
                    "step_title": "Generate Root Cause Hypotheses",
                    "step_description": "Develop potential root cause theories"
                },
                "workflow_context": {
                    "problem_description": "Database connection failures",
                    "priority_level": "high"
                }
            }
        )
        
        assert reasoning_result is not None
        assert "findings" in reasoning_result
        
        # Search knowledge with reasoning context
        knowledge_result = await integrated_knowledge_service.search_with_reasoning_context(
            query="database connection failures troubleshooting",
            session_id=session_id,
            reasoning_type="diagnostic",
            context={
                "phase": "formulate_hypothesis",
                "urgency_level": "high"
            }
        )
        
        assert knowledge_result is not None
        assert "results" in knowledge_result
        assert len(knowledge_result["results"]) > 0
        assert "reasoning_insights" in knowledge_result
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_knowledge_memory_cross_enhancement(
        self,
        integrated_knowledge_service,
        integrated_memory_service
    ):
        """Test knowledge and memory services enhancing each other"""
        session_id = "test-session-knowledge-memory"
        
        # Store interaction in memory
        await integrated_memory_service.store_interaction(
            session_id=session_id,
            user_input="How to fix SSL certificate errors?",
            ai_response="SSL errors often require certificate renewal or configuration updates",
            context={
                "topic": "ssl_certificates",
                "issue_type": "security",
                "resolution_type": "configuration"
            }
        )
        
        # Knowledge search should be enhanced by memory context
        knowledge_result = await integrated_knowledge_service.search_with_reasoning_context(
            query="SSL certificate configuration",
            session_id=session_id,
            reasoning_type="diagnostic",
            context={"urgency_level": "medium"}
        )
        
        assert knowledge_result is not None
        assert knowledge_result["performance_metrics"]["memory_insights_used"] > 0
        
        # Store knowledge findings back to memory
        await integrated_memory_service.store_interaction(
            session_id=session_id,
            user_input="Found SSL certificate documentation",
            ai_response="Certificate renewal process documented",
            context={
                "knowledge_sources": [doc["metadata"]["source"] for doc in knowledge_result["results"][:2]],
                "search_effectiveness": knowledge_result["confidence_score"]
            }
        )
        
        # Verify memory was updated
        memory_context = await integrated_memory_service.retrieve_context(
            session_id,
            "SSL certificate management"
        )
        
        assert len(memory_context.relevant_insights) > 1


class TestCrossSessionKnowledgeSharing:
    """Test knowledge sharing and learning across sessions"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cross_session_learning(
        self,
        integrated_memory_service,
        integrated_knowledge_service
    ):
        """Test that insights from one session enhance future sessions"""
        # Session 1: Solve a database problem
        session_1 = "session-database-problem-1"
        
        await integrated_memory_service.store_interaction(
            session_id=session_1,
            user_input="Database connection pool exhausted",
            ai_response="Increased pool size from 10 to 50 connections",
            context={
                "issue": "connection_pool_exhaustion",
                "solution": "increase_pool_size",
                "original_value": 10,
                "new_value": 50,
                "success": True,
                "resolution_time": 300  # 5 minutes
            }
        )
        
        # Session 2: Similar problem should benefit from session 1 learning
        session_2 = "session-database-problem-2"
        
        knowledge_result = await integrated_knowledge_service.search_with_reasoning_context(
            query="database connection issues performance",
            session_id=session_2,
            reasoning_type="diagnostic",
            context={"urgency_level": "high"}
        )
        
        # Even though it's a different session, knowledge should be enhanced
        assert knowledge_result is not None
        assert knowledge_result["confidence_score"] > 0.5
        
        # Memory retrieval in session 2 might have cross-session insights
        memory_context = await integrated_memory_service.retrieve_context(
            session_2,
            "database connection problems"
        )
        
        # Should have domain context even in new session
        assert memory_context.domain_context is not None
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pattern_recognition_across_sessions(
        self,
        integrated_memory_service,
        integrated_orchestration_service
    ):
        """Test pattern recognition across multiple sessions"""
        # Create multiple sessions with similar problems
        problem_pattern = "Memory usage spikes before application crash"
        
        for i in range(3):
            session_id = f"session-memory-pattern-{i}"
            
            # Store similar pattern in each session
            await integrated_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"App crashed again - {problem_pattern}",
                ai_response="Memory leak investigation recommended",
                context={
                    "pattern": "memory_leak_crash",
                    "session_number": i,
                    "crash_time": datetime.utcnow().isoformat()
                }
            )
        
        # New session should benefit from pattern recognition
        new_session = "session-memory-pattern-new"
        
        workflow_context = WorkflowContext(
            session_id=new_session,
            case_id=f"case-pattern-test",
            user_id="user-pattern-test",
            problem_description=problem_pattern,
            initial_context={"pattern_test": True}
        )
        
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id=workflow_context.session_id,
            case_id=workflow_context.case_id,
            user_id=workflow_context.user_id,
            problem_description=workflow_context.problem_description,
            context=workflow_context.initial_context
        )
        
        # Should show memory enhancements from pattern recognition
        assert workflow_result["success"] is True
        assert workflow_result["memory_enhancements"] > 0


class TestPerformanceUnderLoad:
    """Test system performance under concurrent operations"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.performance
    async def test_concurrent_workflow_creation(
        self,
        integrated_orchestration_service
    ):
        """Test system performance with concurrent workflow creation"""
        async def create_workflow(workflow_num):
            return await integrated_orchestration_service.create_troubleshooting_workflow(
                session_id=f"concurrent-session-{workflow_num}",
                case_id=f"concurrent-case-{workflow_num}",
                user_id=f"concurrent-user-{workflow_num}",
                problem_description=f"Performance issue #{workflow_num} in production",
                context={"test": "concurrent_creation", "workflow_num": workflow_num}
            )
        
        # Create 10 concurrent workflows
        start_time = time.time()
        tasks = [create_workflow(i) for i in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        end_time = time.time()
        
        # Validate all workflows were created successfully
        successful_results = [r for r in results if isinstance(r, dict) and r.get("success")]
        assert len(successful_results) == 10
        
        # Performance validation (should complete within reasonable time)
        total_time = end_time - start_time
        assert total_time < 10.0  # Should complete within 10 seconds
        
        # Validate average creation time
        avg_time = total_time / 10
        assert avg_time < 1.0  # Each workflow should create in < 1 second
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.performance
    async def test_concurrent_memory_operations(
        self,
        integrated_memory_service
    ):
        """Test memory service performance under concurrent operations"""
        async def memory_operation(op_num):
            session_id = f"memory-perf-{op_num}"
            
            # Store interaction
            await integrated_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Performance test operation {op_num}",
                ai_response=f"Response for operation {op_num}",
                context={"operation": op_num, "test": "concurrent_memory"}
            )
            
            # Retrieve context
            context = await integrated_memory_service.retrieve_context(
                session_id,
                f"Query for operation {op_num}"
            )
            
            return context is not None
        
        # Execute 20 concurrent memory operations
        start_time = time.time()
        tasks = [memory_operation(i) for i in range(20)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        end_time = time.time()
        
        # Validate all operations succeeded
        successful_ops = [r for r in results if r is True]
        assert len(successful_ops) == 20
        
        # Performance validation
        total_time = end_time - start_time
        assert total_time < 5.0  # Should complete within 5 seconds
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.performance
    async def test_concurrent_knowledge_searches(
        self,
        integrated_knowledge_service
    ):
        """Test knowledge service performance under concurrent searches"""
        async def knowledge_search(search_num):
            return await integrated_knowledge_service.search_with_reasoning_context(
                query=f"Database performance issue search {search_num}",
                session_id=f"search-session-{search_num}",
                reasoning_type="diagnostic",
                context={"search_num": search_num}
            )
        
        # Execute 15 concurrent searches
        start_time = time.time()
        tasks = [knowledge_search(i) for i in range(15)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        end_time = time.time()
        
        # Validate all searches succeeded
        successful_searches = [r for r in results if isinstance(r, dict) and "results" in r]
        assert len(successful_searches) == 15
        
        # Performance validation
        total_time = end_time - start_time
        assert total_time < 8.0  # Should complete within 8 seconds


class TestErrorRecoveryAndResilience:
    """Test system recovery when components fail"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_workflow_with_memory_service_failure(
        self,
        integrated_orchestration_service,
        sample_workflow_context
    ):
        """Test workflow continues when memory service fails"""
        # Mock memory service to fail
        with patch.object(
            integrated_orchestration_service._orchestrator._memory,
            'retrieve_context',
            side_effect=Exception("Memory service unavailable")
        ):
            # Workflow should still be created despite memory failure
            workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
                session_id=sample_workflow_context.session_id,
                case_id=sample_workflow_context.case_id,
                user_id=sample_workflow_context.user_id,
                problem_description=sample_workflow_context.problem_description,
                context=sample_workflow_context.initial_context
            )
            
            # Should succeed even without memory enhancement
            assert workflow_result["success"] is True
            assert workflow_result["memory_enhancements"] == 0  # No memory enhancement due to failure
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_workflow_with_knowledge_service_failure(
        self,
        integrated_orchestration_service,
        sample_workflow_context
    ):
        """Test workflow continues when knowledge service fails"""
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id=sample_workflow_context.session_id,
            case_id=sample_workflow_context.case_id,
            user_id=sample_workflow_context.user_id,
            problem_description=sample_workflow_context.problem_description,
            context=sample_workflow_context.initial_context
        )
        
        workflow_id = workflow_result["workflow_id"]
        
        # Mock knowledge service to fail during step execution
        with patch.object(
            integrated_orchestration_service._orchestrator._knowledge,
            'search_with_reasoning_context',
            side_effect=Exception("Knowledge service unavailable")
        ):
            # Step should still execute despite knowledge failure
            step_result = await integrated_orchestration_service.execute_workflow_step(
                workflow_id=workflow_id
            )
            
            assert step_result["success"] is True
            # Workflow continues even without knowledge enhancement
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_service_health_monitoring(
        self,
        integrated_orchestration_service
    ):
        """Test comprehensive service health monitoring"""
        health_result = await integrated_orchestration_service.health_check()
        
        assert health_result is not None
        assert "status" in health_result
        assert "orchestrator" in health_result
        assert "service_metrics" in health_result
        assert "capabilities" in health_result
        
        # Check orchestrator health details
        orchestrator_health = health_result["orchestrator"]
        assert "dependencies" in orchestrator_health
        assert "active_workflows" in orchestrator_health
        assert "performance_metrics" in orchestrator_health


class TestDataFlowValidation:
    """Test proper data flow between system components"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_end_to_end_data_flow(
        self,
        integrated_orchestration_service,
        sample_workflow_context
    ):
        """Test data flows correctly through entire system"""
        # Create workflow with specific context
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id=sample_workflow_context.session_id,
            case_id=sample_workflow_context.case_id,
            user_id=sample_workflow_context.user_id,
            problem_description=sample_workflow_context.problem_description,
            context=sample_workflow_context.initial_context
        )
        
        workflow_id = workflow_result["workflow_id"]
        
        # Execute step with specific inputs
        step_inputs = {
            "user_feedback": "Issue started after database upgrade",
            "additional_context": "Error rate increased 5x"
        }
        
        step_result = await integrated_orchestration_service.execute_workflow_step(
            workflow_id=workflow_id,
            step_inputs=step_inputs
        )
        
        # Validate data flow through components
        assert step_result["success"] is True
        assert "step_execution" in step_result
        
        # Check that inputs are preserved and processed
        step_execution = step_result["step_execution"]
        assert "findings" in step_execution
        assert "insights" in step_execution
        
        # Get workflow status to verify data persistence
        status_result = await integrated_orchestration_service.get_workflow_status(workflow_id)
        assert status_result["findings_summary"]["total_findings"] > 0
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_context_propagation_across_steps(
        self,
        integrated_orchestration_service,
        sample_workflow_context
    ):
        """Test context propagates correctly across workflow steps"""
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id=sample_workflow_context.session_id,
            case_id=sample_workflow_context.case_id,
            user_id=sample_workflow_context.user_id,
            problem_description=sample_workflow_context.problem_description,
            context=sample_workflow_context.initial_context
        )
        
        workflow_id = workflow_result["workflow_id"]
        
        # Execute multiple steps to test context propagation
        step1_inputs = {"initial_observation": "Database timeouts started at 2PM"}
        step1_result = await integrated_orchestration_service.execute_workflow_step(
            workflow_id=workflow_id,
            step_inputs=step1_inputs
        )
        
        # Second step should have access to findings from first step
        step2_inputs = {"follow_up": "Checked logs, found connection pool errors"}
        step2_result = await integrated_orchestration_service.execute_workflow_step(
            workflow_id=workflow_id,
            step_inputs=step2_inputs
        )
        
        # Validate context propagation
        assert step1_result["success"] is True
        assert step2_result["success"] is True
        
        # Status should show accumulated findings
        status_result = await integrated_orchestration_service.get_workflow_status(workflow_id)
        assert status_result["findings_summary"]["total_findings"] >= 2
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_memory_knowledge_data_consistency(
        self,
        integrated_memory_service,
        integrated_knowledge_service
    ):
        """Test data consistency between memory and knowledge services"""
        session_id = "test-data-consistency"
        
        # Store knowledge-related interaction in memory
        knowledge_topic = "API rate limiting configuration"
        await integrated_memory_service.store_interaction(
            session_id=session_id,
            user_input=f"How to configure {knowledge_topic}?",
            ai_response="Rate limiting prevents API abuse and ensures fair usage",
            context={
                "topic": "api_rate_limiting",
                "category": "configuration",
                "user_expertise": "intermediate"
            }
        )
        
        # Search for related knowledge
        knowledge_result = await integrated_knowledge_service.search_with_reasoning_context(
            query=knowledge_topic,
            session_id=session_id,
            reasoning_type="strategic"
        )
        
        # Memory should enhance knowledge search
        assert knowledge_result["performance_metrics"]["memory_insights_used"] > 0
        
        # Knowledge findings should be consistent with memory context
        memory_context = await integrated_memory_service.retrieve_context(
            session_id,
            knowledge_topic
        )
        
        assert memory_context.domain_context is not None
        assert len(memory_context.relevant_insights) > 0


@pytest.mark.integration
class TestAPIEndpointIntegration:
    """Test API endpoints work correctly together"""
    
    @pytest.mark.asyncio
    async def test_orchestration_api_workflow(
        self,
        integrated_orchestration_service
    ):
        """Test orchestration API endpoints in sequence"""
        # This would typically test actual HTTP endpoints
        # For now, we test the service layer that powers the API
        
        # Create workflow (POST /orchestration/workflows)
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id="api-test-session",
            case_id="api-test-case",
            user_id="api-test-user",
            problem_description="API response times degraded",
            context={"api": "user-service", "environment": "production"}
        )
        
        assert workflow_result["success"] is True
        workflow_id = workflow_result["workflow_id"]
        
        # Execute step (POST /orchestration/workflows/{id}/steps) 
        step_result = await integrated_orchestration_service.execute_workflow_step(
            workflow_id=workflow_id
        )
        assert step_result["success"] is True
        
        # Get status (GET /orchestration/workflows/{id}/status)
        status_result = await integrated_orchestration_service.get_workflow_status(workflow_id)
        assert status_result["success"] is True
        
        # Get recommendations (GET /orchestration/workflows/{id}/recommendations)
        recommendations_result = await integrated_orchestration_service.get_workflow_recommendations(workflow_id)
        assert recommendations_result["success"] is True
        
        # Pause workflow (POST /orchestration/workflows/{id}/pause)
        pause_result = await integrated_orchestration_service.pause_workflow(
            workflow_id, 
            reason="Testing pause functionality"
        )
        assert pause_result["success"] is True
        
        # Resume workflow (POST /orchestration/workflows/{id}/resume)
        resume_result = await integrated_orchestration_service.resume_workflow(workflow_id)
        assert resume_result["success"] is True
        
        # List workflows (GET /orchestration/workflows)
        list_result = await integrated_orchestration_service.list_active_workflows()
        assert list_result["success"] is True
        
        # Health check (GET /orchestration/health)
        health_result = await integrated_orchestration_service.health_check()
        assert health_result is not None
        assert "status" in health_result


# Performance benchmarks
@pytest.mark.benchmark
@pytest.mark.integration  
class TestPerformanceBenchmarks:
    """Performance benchmarks for integrated system"""
    
    @pytest.mark.asyncio
    async def test_workflow_creation_benchmark(
        self,
        integrated_orchestration_service,
        benchmark
    ):
        """Benchmark workflow creation performance"""
        async def create_workflow():
            return await integrated_orchestration_service.create_troubleshooting_workflow(
                session_id="benchmark-session",
                case_id="benchmark-case",
                user_id="benchmark-user", 
                problem_description="Benchmark performance test",
                context={"benchmark": True}
            )
        
        # Run benchmark
        result = await benchmark(create_workflow)
        assert result["success"] is True
    
    @pytest.mark.asyncio
    async def test_step_execution_benchmark(
        self,
        integrated_orchestration_service,
        benchmark
    ):
        """Benchmark step execution performance"""
        # Setup - create workflow first
        workflow_result = await integrated_orchestration_service.create_troubleshooting_workflow(
            session_id="benchmark-step-session",
            case_id="benchmark-step-case", 
            user_id="benchmark-step-user",
            problem_description="Step execution benchmark",
            context={"benchmark_step": True}
        )
        
        workflow_id = workflow_result["workflow_id"]
        
        async def execute_step():
            return await integrated_orchestration_service.execute_workflow_step(
                workflow_id=workflow_id
            )
        
        # Run benchmark
        result = await benchmark(execute_step)
        assert result["success"] is True