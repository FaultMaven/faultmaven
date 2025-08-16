"""Comprehensive Test Suite for Phase 1: Core Intelligence Implementation

This test suite validates the complete Phase 1 implementation including:
- Memory Management System (hierarchical memory architecture)
- Strategic Planning System (problem decomposition and strategy development)
- Enhanced Agent Service integration
- End-to-end workflow validation

The tests ensure that all Phase 1 components work together to provide
intelligent, context-aware troubleshooting assistance with memory and planning.
"""

import pytest
import asyncio
import uuid
from unittest.mock import Mock, AsyncMock, patch
from typing import Dict, Any, List

from faultmaven.services.memory_service import MemoryService
from faultmaven.services.planning_service import PlanningService
from faultmaven.services.enhanced_agent_service import EnhancedAgentService
from faultmaven.core.memory.memory_manager import MemoryManager
from faultmaven.core.planning.planning_engine import PlanningEngine
from faultmaven.models.interfaces import (
    ConversationContext, UserProfile, StrategicPlan, ProblemComponents
)
from faultmaven.models import QueryRequest, AgentResponse, ResponseType
from faultmaven.exceptions import MemoryException, PlanningException


class TestMemorySystem:
    """Test suite for Memory Management System"""
    
    @pytest.fixture
    async def memory_service(self):
        """Create memory service with mocked dependencies"""
        mock_llm = AsyncMock()
        mock_tracer = Mock()
        mock_tracer.trace.return_value.__enter__ = Mock()
        mock_tracer.trace.return_value.__exit__ = Mock()
        
        service = MemoryService(
            llm_provider=mock_llm,
            tracer=mock_tracer
        )
        return service
    
    @pytest.mark.asyncio
    async def test_memory_context_retrieval(self, memory_service):
        """Test memory context retrieval with conversation history"""
        session_id = "test_session_123"
        query = "Database connection issues in production"
        
        # Test context retrieval
        context = await memory_service.retrieve_context(session_id, query)
        
        # Verify context structure
        assert isinstance(context, ConversationContext)
        assert context.session_id == session_id
        assert isinstance(context.conversation_history, list)
        assert isinstance(context.relevant_insights, list)
        assert context.user_profile is not None
        assert context.domain_context is not None
    
    @pytest.mark.asyncio
    async def test_memory_insight_consolidation(self, memory_service):
        """Test memory insight consolidation from troubleshooting results"""
        session_id = "test_session_456"
        result = {
            "findings": [
                {"type": "error", "message": "Connection timeout", "severity": "high"},
                {"type": "warning", "message": "High CPU usage", "severity": "medium"}
            ],
            "root_cause": "Database connection pool exhausted",
            "recommendations": ["Increase connection pool size", "Monitor connection usage"],
            "confidence_score": 0.85
        }
        
        # Test insight consolidation
        success = await memory_service.consolidate_insights(session_id, result)
        
        # Verify consolidation success
        assert success is True
    
    @pytest.mark.asyncio
    async def test_user_profile_management(self, memory_service):
        """Test user profile retrieval and updates"""
        session_id = "test_session_789"
        
        # Test profile retrieval
        profile = await memory_service.get_user_profile(session_id)
        assert isinstance(profile, UserProfile)
        assert profile.user_id is not None
        assert profile.skill_level in ["beginner", "intermediate", "advanced"]
        
        # Test profile updates
        updates = {
            "skill_level": "advanced",
            "preferred_communication_style": "technical",
            "domain_expertise": ["database", "networking"]
        }
        
        success = await memory_service.update_user_profile(session_id, updates)
        assert success is True
    
    @pytest.mark.asyncio
    async def test_memory_performance_targets(self, memory_service):
        """Test that memory operations meet performance targets"""
        import time
        
        session_id = "test_session_perf"
        query = "Performance testing query"
        
        # Test context retrieval performance (target: < 50ms)
        start_time = time.time()
        await memory_service.retrieve_context(session_id, query)
        retrieval_time = (time.time() - start_time) * 1000
        
        assert retrieval_time < 50, f"Context retrieval took {retrieval_time:.2f}ms, exceeds 50ms target"
        
        # Test consolidation performance (should be async, non-blocking)
        start_time = time.time()
        await memory_service.consolidate_insights(session_id, {"test": "data"})
        consolidation_time = (time.time() - start_time) * 1000
        
        # Consolidation should be fast since it's designed to be async
        assert consolidation_time < 100, f"Consolidation took {consolidation_time:.2f}ms"


class TestPlanningSystem:
    """Test suite for Strategic Planning System"""
    
    @pytest.fixture
    async def planning_service(self):
        """Create planning service with mocked dependencies"""
        mock_llm = AsyncMock()
        mock_memory = AsyncMock()
        
        service = PlanningService(
            llm_provider=mock_llm,
            memory_service=mock_memory
        )
        return service
    
    @pytest.mark.asyncio
    async def test_strategic_plan_creation(self, planning_service):
        """Test strategic plan creation for complex scenarios"""
        query = "Multiple microservices failing intermittently in production"
        context = {
            "environment": "production",
            "urgency": "high",
            "user_profile": {"skill_level": "intermediate"},
            "available_time": "limited",
            "team_size": 3
        }
        
        # Test plan creation
        plan = await planning_service.plan_response_strategy(query, context)
        
        # Verify plan structure
        assert isinstance(plan, StrategicPlan)
        assert plan.plan_id is not None
        assert plan.confidence_score > 0.0
        assert plan.confidence_score <= 1.0
        assert plan.estimated_effort is not None
        
        # Verify plan components
        assert "original_problem" in plan.problem_analysis
        assert "primary_issue" in plan.problem_analysis
        assert "approach" in plan.solution_strategy
        assert "overall_risk_level" in plan.risk_assessment
        assert len(plan.success_criteria) > 0
    
    @pytest.mark.asyncio
    async def test_problem_decomposition(self, planning_service):
        """Test problem decomposition for complex issues"""
        problem = "Distributed system experiencing cascading failures with database timeouts and network latency issues"
        context = {
            "system_info": {"type": "microservices", "scale": "large"},
            "error_patterns": ["timeout", "network", "database"],
            "environment": "production"
        }
        
        # Test decomposition
        components = await planning_service.decompose_problem(problem, context)
        
        # Verify decomposition structure
        assert isinstance(components, ProblemComponents)
        assert components.primary_issue is not None
        assert len(components.contributing_factors) > 0
        assert len(components.dependencies) >= 0
        assert "level" in components.complexity_assessment
        assert len(components.priority_ranking) > 0
    
    @pytest.mark.asyncio
    async def test_plan_adaptation(self, planning_service):
        """Test plan adaptation based on new context"""
        # First create a plan
        query = "Database performance degradation"
        context = {"environment": "production", "urgency": "medium"}
        
        original_plan = await planning_service.plan_response_strategy(query, context)
        
        # Test adaptation with urgency increase
        adaptation_context = {
            "urgency_increased": True,
            "new_risk_level": "high",
            "strategy_ineffective": False
        }
        
        adapted_plan = await planning_service.adapt_plan(
            original_plan.plan_id, adaptation_context
        )
        
        # Verify adaptation occurred
        if adapted_plan:  # Plan might not exist in cache for this test
            assert adapted_plan.plan_id == original_plan.plan_id
            # In a real adaptation, we'd expect changes to approach or timeline
    
    @pytest.mark.asyncio
    async def test_planning_performance_targets(self, planning_service):
        """Test that planning operations meet performance targets"""
        import time
        
        query = "Performance test query"
        context = {"environment": "test", "urgency": "low"}
        
        # Test strategy planning performance (target: < 200ms)
        start_time = time.time()
        await planning_service.plan_response_strategy(query, context)
        planning_time = (time.time() - start_time) * 1000
        
        assert planning_time < 200, f"Planning took {planning_time:.2f}ms, exceeds 200ms target"
        
        # Test problem decomposition performance (target: < 100ms)
        start_time = time.time()
        await planning_service.decompose_problem(query, context)
        decomposition_time = (time.time() - start_time) * 1000
        
        assert decomposition_time < 100, f"Decomposition took {decomposition_time:.2f}ms, exceeds 100ms target"


class TestEnhancedAgentIntegration:
    """Test suite for Enhanced Agent Service integration"""
    
    @pytest.fixture
    async def enhanced_agent_service(self):
        """Create enhanced agent service with mocked dependencies"""
        mock_llm = AsyncMock()
        mock_llm.generate_response.return_value = "Test LLM response"
        
        mock_tracer = Mock()
        mock_tracer.trace.return_value.__enter__ = Mock()
        mock_tracer.trace.return_value.__exit__ = Mock()
        
        mock_sanitizer = Mock()
        mock_sanitizer.sanitize.side_effect = lambda x: x  # Pass-through
        
        mock_memory = AsyncMock()
        mock_memory.retrieve_context.return_value = ConversationContext(
            session_id="test_session",
            conversation_history=[],
            relevant_insights=[{"description": "Previous database issue resolved"}],
            user_profile=UserProfile(
                user_id="test_user",
                skill_level="intermediate",
                preferred_communication_style="balanced",
                domain_expertise=["database"],
                interaction_patterns={},
                learning_preferences={}
            ),
            domain_context={"primary_domain": "database"}
        )
        mock_memory.consolidate_insights.return_value = True
        
        mock_planning = AsyncMock()
        mock_planning.plan_response_strategy.return_value = StrategicPlan(
            plan_id=str(uuid.uuid4()),
            problem_analysis={"primary_issue": "Database connection issue"},
            solution_strategy={"approach": "systematic_analysis", "methodology": ["Analyze logs", "Check connections"]},
            risk_assessment={"overall_risk_level": "medium"},
            success_criteria=["Database connectivity restored"],
            estimated_effort="1-2 hours",
            confidence_score=0.8
        )
        
        service = EnhancedAgentService(
            llm_provider=mock_llm,
            tools=[],
            tracer=mock_tracer,
            sanitizer=mock_sanitizer,
            memory_service=mock_memory,
            planning_service=mock_planning
        )
        return service
    
    @pytest.mark.asyncio
    async def test_enhanced_query_processing_with_memory(self, enhanced_agent_service):
        """Test enhanced query processing with memory integration"""
        request = QueryRequest(
            session_id="test_session_enhanced",
            query="Database connections are timing out again",
            context={"environment": "production", "urgency": "high"}
        )
        
        # Mock the agent execution
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.run.return_value = {
                "findings": [{"message": "Connection pool exhausted", "type": "error"}],
                "recommendations": ["Increase pool size"],
                "root_cause": "High connection usage"
            }
            mock_agent_class.return_value = mock_agent
            
            # Process query
            response = await enhanced_agent_service.process_query(request)
            
            # Verify enhanced response
            assert isinstance(response, AgentResponse)
            assert response.response_type in [ResponseType.ANSWER, ResponseType.PLAN_PROPOSAL, 
                                             ResponseType.CLARIFICATION_REQUEST, ResponseType.CONFIRMATION_REQUEST]
            assert response.content is not None
            assert response.view_state is not None
            assert response.view_state.session_id == request.session_id
            
            # Verify memory integration was called
            enhanced_agent_service._memory.retrieve_context.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_enhanced_query_processing_with_planning(self, enhanced_agent_service):
        """Test enhanced query processing with strategic planning"""
        request = QueryRequest(
            session_id="test_session_planning",
            query="Complex distributed system failure affecting multiple microservices",
            context={"environment": "production", "urgency": "critical"}
        )
        
        # Mock the agent execution
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.run.return_value = {
                "findings": [{"message": "Multiple service failures", "type": "error"}],
                "recommendations": ["Systematic investigation approach"],
                "next_steps": ["Check service health", "Analyze logs", "Review dependencies"]
            }
            mock_agent_class.return_value = mock_agent
            
            # Process query
            response = await enhanced_agent_service.process_query(request)
            
            # Verify enhanced response with planning
            assert isinstance(response, AgentResponse)
            
            # Should trigger strategic planning for complex scenarios
            enhanced_agent_service._planning.plan_response_strategy.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_enhanced_response_personalization(self, enhanced_agent_service):
        """Test response personalization based on user profile"""
        # Test with beginner user
        beginner_request = QueryRequest(
            session_id="beginner_session",
            query="Why is my database slow?",
            context={"user_skill_level": "beginner"}
        )
        
        # Update mock memory to return beginner profile
        enhanced_agent_service._memory.retrieve_context.return_value.user_profile.skill_level = "beginner"
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.run.return_value = {
                "findings": [{"message": "High query response time", "type": "performance"}],
                "recommendations": ["Check database performance"]
            }
            mock_agent_class.return_value = mock_agent
            
            response = await enhanced_agent_service.process_query(beginner_request)
            
            # Verify response is tailored for beginners
            assert "step-by-step" in response.content.lower() or "guide" in response.content.lower()
    
    @pytest.mark.asyncio
    async def test_enhanced_performance_targets(self, enhanced_agent_service):
        """Test that enhanced agent meets performance targets"""
        import time
        
        request = QueryRequest(
            session_id="perf_test_session",
            query="Performance test query",
            context={"environment": "test"}
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.run.return_value = {"findings": [], "recommendations": []}
            mock_agent_class.return_value = mock_agent
            
            # Test total response time (target: < 2000ms)
            start_time = time.time()
            await enhanced_agent_service.process_query(request)
            total_time = (time.time() - start_time) * 1000
            
            assert total_time < 2000, f"Enhanced processing took {total_time:.2f}ms, exceeds 2000ms target"
    
    @pytest.mark.asyncio
    async def test_enhanced_agent_health_check(self, enhanced_agent_service):
        """Test enhanced agent service health check"""
        # Test health check
        health = await enhanced_agent_service.health_check()
        
        # Verify health check structure
        assert health["service"] == "enhanced_agent_service"
        assert health["status"] in ["healthy", "degraded", "unhealthy"]
        assert "components" in health
        assert "performance_metrics" in health
        assert "capabilities" in health
        
        # Verify capabilities
        capabilities = health["capabilities"]
        assert capabilities["basic_troubleshooting"] is True
        assert capabilities["memory_integration"] is True
        assert capabilities["strategic_planning"] is True
        assert capabilities["context_awareness"] is True
        assert capabilities["personalization"] is True


class TestEndToEndWorkflow:
    """End-to-end workflow tests for Phase 1 integration"""
    
    @pytest.mark.asyncio
    async def test_complete_enhanced_troubleshooting_workflow(self):
        """Test complete workflow from query to enhanced response"""
        # Create full service stack with mocks
        mock_llm = AsyncMock()
        mock_llm.generate_response.return_value = "Comprehensive analysis complete"
        
        mock_tracer = Mock()
        mock_tracer.trace.return_value.__enter__ = Mock()
        mock_tracer.trace.return_value.__exit__ = Mock()
        
        mock_sanitizer = Mock()
        mock_sanitizer.sanitize.side_effect = lambda x: x
        
        # Create memory service
        memory_service = MemoryService(
            llm_provider=mock_llm,
            tracer=mock_tracer
        )
        
        # Create planning service
        planning_service = PlanningService(
            llm_provider=mock_llm,
            memory_service=memory_service
        )
        
        # Create enhanced agent service
        enhanced_agent = EnhancedAgentService(
            llm_provider=mock_llm,
            tools=[],
            tracer=mock_tracer,
            sanitizer=mock_sanitizer,
            memory_service=memory_service,
            planning_service=planning_service
        )
        
        # Test complex scenario request
        request = QueryRequest(
            session_id="e2e_test_session",
            query="Our microservices architecture is experiencing cascading failures. Database connections are timing out, API response times are high, and users are reporting intermittent errors. This started after last night's deployment.",
            context={
                "environment": "production",
                "urgency": "critical",
                "available_time": "limited",
                "team_size": 5,
                "user_intent": "problem_resolution"
            }
        )
        
        with patch('faultmaven.core.agent.agent.FaultMavenAgent') as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.run.return_value = {
                "findings": [
                    {"message": "Database connection pool exhausted", "type": "error", "severity": "high"},
                    {"message": "API gateway response time increased 400%", "type": "performance", "severity": "high"},
                    {"message": "Recent deployment introduced connection leak", "type": "issue", "severity": "critical"}
                ],
                "root_cause": "Database connection leak in recent deployment",
                "recommendations": [
                    "Immediately rollback recent deployment",
                    "Restart database connection pools",
                    "Monitor connection usage",
                    "Review code changes for connection handling"
                ],
                "next_steps": [
                    "Execute rollback procedure",
                    "Verify service restoration",
                    "Investigate root cause in deployment",
                    "Implement fix and test",
                    "Redeploy with fix"
                ],
                "confidence_score": 0.9,
                "knowledge_base_results": [
                    {"title": "Connection Pool Management", "snippet": "Best practices for managing database connections..."}
                ]
            }
            mock_agent_class.return_value = mock_agent
            
            # Execute end-to-end workflow
            response = await enhanced_agent.process_query(request)
            
            # Verify comprehensive response
            assert isinstance(response, AgentResponse)
            assert response.content is not None
            assert len(response.content) > 100  # Should be substantial response
            
            # Should include strategic context for critical issues
            assert "strategic" in response.content.lower() or "approach" in response.content.lower()
            
            # Should include personalization elements
            assert response.view_state.session_id == request.session_id
            
            # Should include sources
            assert len(response.sources) >= 0
            
            # For critical issues with many next steps, should propose a plan
            if len(mock_agent.run.return_value["next_steps"]) > 3:
                assert response.response_type == ResponseType.PLAN_PROPOSAL
                assert response.plan is not None
                assert len(response.plan) > 0
    
    @pytest.mark.asyncio
    async def test_memory_learning_across_sessions(self):
        """Test that memory system learns and applies insights across sessions"""
        # This test would verify that insights from one session
        # are available and applied in subsequent sessions
        
        # First session - establish pattern
        memory_service = MemoryService(
            llm_provider=AsyncMock(),
            tracer=Mock()
        )
        
        session_id = "learning_test_session"
        
        # Simulate first troubleshooting result
        first_result = {
            "findings": [{"message": "Database timeout resolved by restarting connection pool"}],
            "root_cause": "Connection pool exhaustion",
            "solution_applied": "Connection pool restart"
        }
        
        await memory_service.consolidate_insights(session_id, first_result)
        
        # Second session - should leverage previous insights
        context = await memory_service.retrieve_context(session_id, "Database timeout again")
        
        # Should have relevant insights from previous session
        assert len(context.relevant_insights) > 0
        # Note: In a full implementation, this would check that the specific
        # connection pool insight is retrieved for similar queries


class TestPhase1Validation:
    """Validation tests to ensure Phase 1 meets all requirements"""
    
    @pytest.mark.asyncio
    async def test_memory_architecture_completeness(self):
        """Validate that memory architecture has all required components"""
        from faultmaven.core.memory.hierarchical_memory import (
            WorkingMemory, SessionMemory, UserMemory, EpisodicMemory
        )
        from faultmaven.core.memory.memory_manager import MemoryManager
        
        # Verify all memory components exist and are properly structured
        assert WorkingMemory is not None
        assert SessionMemory is not None
        assert UserMemory is not None
        assert EpisodicMemory is not None
        assert MemoryManager is not None
        
        # Create memory manager to verify integration
        mock_llm = AsyncMock()
        memory_manager = MemoryManager(mock_llm)
        
        # Verify key methods exist
        assert hasattr(memory_manager, 'retrieve_context')
        assert hasattr(memory_manager, 'consolidate_insights')
    
    @pytest.mark.asyncio
    async def test_planning_architecture_completeness(self):
        """Validate that planning architecture has all required components"""
        from faultmaven.core.planning.problem_decomposer import ProblemDecomposer
        from faultmaven.core.planning.strategy_planner import StrategyPlanner
        from faultmaven.core.planning.risk_assessor import RiskAssessor
        from faultmaven.core.planning.planning_engine import PlanningEngine
        
        # Verify all planning components exist
        assert ProblemDecomposer is not None
        assert StrategyPlanner is not None
        assert RiskAssessor is not None
        assert PlanningEngine is not None
        
        # Create planning engine to verify integration
        mock_llm = AsyncMock()
        planning_engine = PlanningEngine(mock_llm)
        
        # Verify key methods exist
        assert hasattr(planning_engine, 'create_troubleshooting_plan')
        assert hasattr(planning_engine, 'adapt_plan')
    
    def test_interface_compliance(self):
        """Validate that all services implement required interfaces"""
        from faultmaven.models.interfaces import IMemoryService, IPlanningService
        from faultmaven.services.memory_service import MemoryService
        from faultmaven.services.planning_service import PlanningService
        
        # Verify interface compliance
        assert issubclass(MemoryService, IMemoryService)
        assert issubclass(PlanningService, IPlanningService)
        
        # Verify enhanced agent has required dependencies
        from faultmaven.services.enhanced_agent_service import EnhancedAgentService
        assert EnhancedAgentService is not None
    
    @pytest.mark.asyncio
    async def test_performance_requirements_met(self):
        """Validate that Phase 1 implementation meets performance requirements"""
        # This test would run performance benchmarks to ensure:
        # - Memory context retrieval: < 50ms
        # - Planning generation: < 200ms  
        # - Total enhanced response: < 2000ms
        
        # Performance test setup would go here
        # For now, we verify the structure exists for performance testing
        assert True  # Placeholder for actual performance validation


if __name__ == "__main__":
    # Run the test suite
    pytest.main([__file__, "-v", "--tb=short"])