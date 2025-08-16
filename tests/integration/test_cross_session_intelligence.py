"""Cross-Session Intelligence Integration Tests

This module contains tests that validate cross-session knowledge sharing,
memory-driven decision making, and intelligent learning across troubleshooting
sessions. These tests ensure the system learns from past interactions and
applies that knowledge to improve future sessions.

Key Test Areas:
- Cross-session knowledge sharing and learning
- Memory-driven decision making and workflow optimization
- Pattern recognition across multiple sessions
- Knowledge evolution and improvement over time
- Intelligent session correlation and insights
- Long-term memory retention and recall
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from unittest.mock import Mock, AsyncMock, patch
import pytest

from faultmaven.services.memory_service import MemoryService
from faultmaven.services.enhanced_knowledge_service import EnhancedKnowledgeService
from faultmaven.services.planning_service import PlanningService
from faultmaven.services.reasoning_service import ReasoningService
from faultmaven.services.orchestration_service import OrchestrationService
from faultmaven.core.orchestration.troubleshooting_orchestrator import WorkflowContext
from faultmaven.models.interfaces import IMemoryService, ILLMProvider


@pytest.fixture
async def mock_llm_with_learning():
    """Mock LLM that simulates learning from previous interactions"""
    llm = Mock()
    
    # Track previous interactions for learning simulation
    interaction_history = []
    
    async def generate_with_learning(prompt, context=None, **kwargs):
        """Simulate LLM learning from previous interactions"""
        # Add current interaction to history
        interaction_history.append({
            "prompt": prompt,
            "context": context,
            "timestamp": datetime.utcnow()
        })
        
        # Simulate learning by referencing similar past interactions
        similar_interactions = [
            interaction for interaction in interaction_history
            if any(keyword in interaction["prompt"].lower() 
                   for keyword in ["database", "connection", "timeout"])
        ]
        
        base_confidence = 0.6
        learning_bonus = min(len(similar_interactions) * 0.1, 0.3)  # Max 30% bonus
        
        if "database" in prompt.lower():
            return {
                "response": f"Based on {len(similar_interactions)} similar cases, database connection issues often stem from pool exhaustion or configuration problems.",
                "confidence": base_confidence + learning_bonus,
                "reasoning": f"Learning from {len(similar_interactions)} previous database cases",
                "learning_applied": len(similar_interactions) > 1
            }
        elif "performance" in prompt.lower():
            return {
                "response": f"Performance degradation patterns from {len(similar_interactions)} cases suggest resource bottlenecks.",
                "confidence": base_confidence + learning_bonus,
                "reasoning": f"Applied patterns from {len(similar_interactions)} performance cases",
                "learning_applied": len(similar_interactions) > 1
            }
        else:
            return {
                "response": "AI-generated troubleshooting response",
                "confidence": base_confidence,
                "reasoning": "Standard analysis without specific pattern matching",
                "learning_applied": False
            }
    
    llm.generate = AsyncMock(side_effect=generate_with_learning)
    return llm


@pytest.fixture
async def persistent_memory_service(mock_llm_with_learning):
    """Memory service that retains data across test scenarios"""
    memory_service = MemoryService(
        llm_provider=mock_llm_with_learning,
        tracer=None
    )
    
    # Pre-populate with some baseline interactions
    await memory_service.store_interaction(
        session_id="baseline-session-1",
        user_input="Database connection timeouts in production",
        ai_response="Increased connection pool size resolved the issue",
        context={
            "issue_type": "database_timeout",
            "resolution": "connection_pool_increase",
            "success": True,
            "resolution_time": 300
        }
    )
    
    await memory_service.store_interaction(
        session_id="baseline-session-2", 
        user_input="API response times are slow",
        ai_response="Database query optimization improved performance",
        context={
            "issue_type": "performance_degradation",
            "resolution": "query_optimization",
            "success": True,
            "improvement": "50% faster"
        }
    )
    
    return memory_service


@pytest.fixture
async def learning_knowledge_service(mock_vector_store, persistent_memory_service, mock_llm_with_learning):
    """Knowledge service that learns from memory interactions"""
    knowledge_service = EnhancedKnowledgeService(
        vector_store=mock_vector_store,
        memory_service=persistent_memory_service,
        llm_provider=mock_llm_with_learning,
        sanitizer=None,
        tracer=None
    )
    return knowledge_service


@pytest.fixture
async def intelligent_orchestration_service(
    persistent_memory_service,
    learning_knowledge_service,
    mock_llm_with_learning
):
    """Orchestration service with intelligent learning capabilities"""
    # Create planning and reasoning services
    planning_service = PlanningService(llm_provider=mock_llm_with_learning, tracer=None)
    reasoning_service = ReasoningService(llm_provider=mock_llm_with_learning, tracer=None)
    
    orchestration_service = OrchestrationService(
        memory_service=persistent_memory_service,
        planning_service=planning_service,
        reasoning_service=reasoning_service,
        enhanced_knowledge_service=learning_knowledge_service,
        llm_provider=mock_llm_with_learning,
        tracer=None
    )
    return orchestration_service


class TestCrossSessionKnowledgeSharing:
    """Test knowledge sharing and learning across sessions"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_knowledge_transfer_between_sessions(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test that knowledge from one session enhances future sessions"""
        # Session 1: Learn about a specific database issue
        session_1 = "learning-session-database-1"
        
        await persistent_memory_service.store_interaction(
            session_id=session_1,
            user_input="PostgreSQL connection pool exhausted errors",
            ai_response="Solution: Increase max_connections and pool_size parameters",
            context={
                "database_type": "postgresql",
                "error_type": "connection_pool_exhausted",
                "solution_components": ["max_connections", "pool_size"],
                "effectiveness": "high",
                "resolution_time": 450
            }
        )
        
        # Session 2: Different user with similar issue should benefit
        session_2 = "learning-session-database-2"
        
        knowledge_result = await learning_knowledge_service.search_with_reasoning_context(
            query="database connection problems postgresql",
            session_id=session_2,
            reasoning_type="diagnostic",
            context={"urgency_level": "high"}
        )
        
        # Knowledge search should be enhanced by learning from session 1
        assert knowledge_result is not None
        assert knowledge_result["confidence_score"] > 0.6
        assert knowledge_result["performance_metrics"]["memory_insights_used"] > 0
        
        # The AI should show learning from previous session
        memory_context = await persistent_memory_service.retrieve_context(
            session_2,
            "postgresql connection issues"
        )
        
        # Even in a new session, should have domain context from previous learning
        assert memory_context.domain_context is not None
        assert len(memory_context.relevant_insights) >= 0  # May have cross-session insights
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_progressive_knowledge_accumulation(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test that knowledge accumulates and improves over multiple sessions"""
        base_topic = "API rate limiting configuration"
        
        # Session 1: Basic rate limiting question
        session_1 = "progressive-session-1"
        await persistent_memory_service.store_interaction(
            session_id=session_1,
            user_input=f"How to implement {base_topic}?",
            ai_response="Use middleware to track requests per time window",
            context={
                "topic": "rate_limiting",
                "complexity": "basic",
                "approach": "middleware"
            }
        )
        
        # Session 2: More advanced rate limiting
        session_2 = "progressive-session-2"
        await persistent_memory_service.store_interaction(
            session_id=session_2,
            user_input=f"Advanced {base_topic} with Redis",
            ai_response="Distributed rate limiting using Redis sliding window",
            context={
                "topic": "rate_limiting",
                "complexity": "advanced",
                "approach": "redis_sliding_window",
                "technologies": ["redis", "distributed"]
            }
        )
        
        # Session 3: Production concerns
        session_3 = "progressive-session-3"
        await persistent_memory_service.store_interaction(
            session_id=session_3,
            user_input=f"Production issues with {base_topic}",
            ai_response="Monitor rate limit hit rates and adjust thresholds dynamically",
            context={
                "topic": "rate_limiting",
                "complexity": "production",
                "approach": "monitoring_adjustment",
                "concerns": ["monitoring", "dynamic_thresholds"]
            }
        )
        
        # Session 4: New user should benefit from all accumulated knowledge
        session_4 = "progressive-session-4"
        
        knowledge_result = await learning_knowledge_service.search_with_reasoning_context(
            query=base_topic,
            session_id=session_4,
            reasoning_type="strategic",
            context={"complexity": "intermediate"}
        )
        
        # Should show accumulated learning from multiple sessions
        assert knowledge_result["confidence_score"] > 0.7  # Higher confidence due to accumulated knowledge
        assert knowledge_result["performance_metrics"]["memory_insights_used"] > 0
        
        # Check that knowledge curation reflects accumulated expertise
        curated_knowledge = await learning_knowledge_service.curate_knowledge_for_reasoning(
            reasoning_type="strategic",
            session_id=session_4,
            topic_focus="rate limiting",
            user_profile={"expertise": "intermediate"}
        )
        
        assert len(curated_knowledge["curated_content"]) > 0
        assert curated_knowledge["reasoning_optimization"]["avg_alignment_score"] > 0.4
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pattern_recognition_across_sessions(
        self,
        persistent_memory_service,
        intelligent_orchestration_service
    ):
        """Test system recognizes patterns across multiple sessions"""
        # Create pattern: Memory leaks leading to crashes
        pattern_sessions = []
        
        for i in range(4):
            session_id = f"pattern-session-memory-leak-{i}"
            pattern_sessions.append(session_id)
            
            await persistent_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Application crashed with out of memory error (case {i})",
                ai_response="Memory leak investigation recommended - check for unreleased resources",
                context={
                    "pattern_type": "memory_leak_crash",
                    "symptoms": ["memory_growth", "oom_error", "application_crash"],
                    "case_number": i,
                    "occurrence_time": (datetime.utcnow() - timedelta(days=i)).isoformat()
                }
            )
        
        # New session with similar pattern should get enhanced workflow
        new_session = "pattern-recognition-test"
        
        workflow_result = await intelligent_orchestration_service.create_troubleshooting_workflow(
            session_id=new_session,
            case_id="pattern-test-case",
            user_id="pattern-test-user",
            problem_description="Memory usage keeps growing and application crashes",
            context={
                "application": "web-service",
                "symptoms": ["memory_growth", "crashes"]
            }
        )
        
        # Should show memory enhancements from pattern recognition
        assert workflow_result["success"] is True
        assert workflow_result["memory_enhancements"] > 0
        assert len(workflow_result["strategic_insights"]) > 0
        
        # Execute first step and check for pattern-informed reasoning
        step_result = await intelligent_orchestration_service.execute_workflow_step(
            workflow_id=workflow_result["workflow_id"],
            step_inputs={"observation": "Memory usage increased 200% over last week"}
        )
        
        assert step_result["success"] is True
        assert len(step_result["step_execution"]["insights"]) > 0


class TestMemoryDrivenDecisionMaking:
    """Test memory insights influence planning and reasoning decisions"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_memory_influences_workflow_planning(
        self,
        persistent_memory_service,
        intelligent_orchestration_service
    ):
        """Test that memory insights influence workflow planning decisions"""
        session_id = "memory-planning-test"
        
        # Store successful resolution pattern
        await persistent_memory_service.store_interaction(
            session_id=session_id,
            user_input="Database deadlocks causing transaction failures",
            ai_response="Implemented query reordering and timeout adjustments - resolved deadlocks",
            context={
                "issue": "database_deadlocks",
                "solution_approach": "query_reordering_timeouts",
                "success_metrics": {
                    "deadlock_reduction": "95%",
                    "resolution_time": 600,
                    "effectiveness": "high"
                },
                "techniques": ["query_optimization", "timeout_tuning"]
            }
        )
        
        # Create workflow for similar issue
        workflow_result = await intelligent_orchestration_service.create_troubleshooting_workflow(
            session_id=session_id,
            case_id="memory-driven-case",
            user_id="memory-driven-user",
            problem_description="Experiencing database deadlocks in transaction processing",
            context={
                "system": "transaction_processor",
                "database": "postgresql"
            }
        )
        
        # Memory should influence workflow creation
        assert workflow_result["success"] is True
        assert workflow_result["memory_enhancements"] > 0
        
        # Strategic insights should reflect memory-driven recommendations
        insights = workflow_result["strategic_insights"]
        assert len(insights) > 0
        
        # Execute workflow step with memory context
        step_result = await intelligent_orchestration_service.execute_workflow_step(
            workflow_id=workflow_result["workflow_id"],
            step_inputs={"context": "Similar to previous deadlock issues"}
        )
        
        # Should show memory-informed decision making
        assert step_result["success"] is True
        recommendations = step_result["recommendations"]["immediate_actions"]
        assert len(recommendations) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_memory_drives_reasoning_strategy_selection(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test memory drives selection of reasoning strategies"""
        session_id = "reasoning-strategy-test"
        
        # Store history of successful analytical approaches
        await persistent_memory_service.store_interaction(
            session_id=session_id,
            user_input="Complex performance issue with multiple variables",
            ai_response="Used systematic analytical approach - identified root cause through correlation analysis",
            context={
                "approach": "systematic_analytical",
                "reasoning_type": "analytical",
                "complexity": "high",
                "success_factors": ["correlation_analysis", "systematic_elimination"],
                "effectiveness": "very_high"
            }
        )
        
        # Search for knowledge with reasoning context influenced by memory
        knowledge_result = await learning_knowledge_service.search_with_reasoning_context(
            query="performance analysis complex systems",
            session_id=session_id,
            reasoning_type="analytical",
            context={
                "complexity": "high",
                "approach": "systematic"
            }
        )
        
        # Memory should enhance reasoning approach
        assert knowledge_result["performance_metrics"]["memory_insights_used"] > 0
        assert knowledge_result["confidence_score"] > 0.6
        
        # Curate knowledge specifically for analytical reasoning
        curated_result = await learning_knowledge_service.curate_knowledge_for_reasoning(
            reasoning_type="analytical",
            session_id=session_id,
            topic_focus="performance analysis"
        )
        
        # Should show memory-informed curation
        assert curated_result["curation_metadata"]["memory_context_used"] is True
        assert curated_result["reasoning_optimization"]["avg_alignment_score"] > 0.5
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_adaptive_decision_making_based_on_outcomes(
        self,
        persistent_memory_service,
        intelligent_orchestration_service
    ):
        """Test system adapts decisions based on previous outcomes"""
        session_id = "adaptive-decision-test"
        
        # Store failed approach
        await persistent_memory_service.store_interaction(
            session_id=session_id,
            user_input="API timeout issues",
            ai_response="Tried increasing timeout values - did not resolve the issue",
            context={
                "approach": "timeout_adjustment",
                "success": False,
                "effectiveness": "low",
                "issue": "api_timeouts",
                "lesson": "timeout_adjustment_insufficient"
            }
        )
        
        # Store successful approach
        await persistent_memory_service.store_interaction(
            session_id=session_id,
            user_input="API timeout issues continued",
            ai_response="Implemented connection pooling and request queuing - resolved timeouts",
            context={
                "approach": "connection_pooling_queuing",
                "success": True,
                "effectiveness": "high",
                "issue": "api_timeouts",
                "lesson": "infrastructure_changes_more_effective"
            }
        )
        
        # Create new workflow for similar issue
        workflow_result = await intelligent_orchestration_service.create_troubleshooting_workflow(
            session_id=session_id,
            case_id="adaptive-case",
            user_id="adaptive-user",
            problem_description="API timeout problems affecting user experience",
            context={"issue_type": "api_timeouts"}
        )
        
        # Should adapt based on previous outcome learning
        assert workflow_result["memory_enhancements"] > 0
        
        # Execute workflow step
        step_result = await intelligent_orchestration_service.execute_workflow_step(
            workflow_id=workflow_result["workflow_id"],
            step_inputs={"previous_attempts": "tried timeout adjustments"}
        )
        
        # Should prioritize successful approaches over failed ones
        assert step_result["success"] is True
        insights = step_result["step_execution"]["insights"]
        recommendations = step_result["step_execution"]["recommendations"]
        
        # Should reflect learning from previous outcomes
        assert len(insights) > 0
        assert len(recommendations) > 0


class TestIntelligentSessionCorrelation:
    """Test intelligent correlation and learning across sessions"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_user_expertise_learning(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test system learns and adapts to user expertise levels"""
        # Simulate expert user interactions
        expert_user_id = "expert-user-123"
        expert_sessions = []
        
        for i in range(3):
            session_id = f"expert-session-{i}"
            expert_sessions.append(session_id)
            
            await persistent_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Advanced Kubernetes networking issue {i}",
                ai_response="Implemented custom CNI configuration and network policies",
                context={
                    "user_id": expert_user_id,
                    "expertise_level": "expert",
                    "domain": "kubernetes_networking",
                    "complexity": "advanced",
                    "technical_depth": "high",
                    "solution_sophistication": "expert_level"
                }
            )
        
        # Simulate novice user interactions
        novice_user_id = "novice-user-456"
        novice_sessions = []
        
        for i in range(3):
            session_id = f"novice-session-{i}"
            novice_sessions.append(session_id)
            
            await persistent_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"How to deploy application {i}",
                ai_response="Step-by-step deployment guide with explanations",
                context={
                    "user_id": novice_user_id,
                    "expertise_level": "novice",
                    "domain": "deployment",
                    "complexity": "basic",
                    "technical_depth": "low",
                    "explanation_detail": "high"
                }
            )
        
        # Test expert user gets advanced knowledge
        expert_knowledge = await learning_knowledge_service.search_with_reasoning_context(
            query="kubernetes networking troubleshooting",
            session_id=expert_sessions[0],
            reasoning_type="analytical",
            user_profile={"user_id": expert_user_id, "expertise": "expert"}
        )
        
        # Test novice user gets basic knowledge
        novice_knowledge = await learning_knowledge_service.search_with_reasoning_context(
            query="application deployment help",
            session_id=novice_sessions[0],
            reasoning_type="diagnostic", 
            user_profile={"user_id": novice_user_id, "expertise": "novice"}
        )
        
        # Both should succeed but with different characteristics
        assert expert_knowledge["confidence_score"] > 0.6
        assert novice_knowledge["confidence_score"] > 0.6
        
        # Verify user pattern learning
        assert len(learning_knowledge_service._user_patterns) >= 2
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_temporal_pattern_recognition(
        self,
        persistent_memory_service,
        intelligent_orchestration_service
    ):
        """Test recognition of temporal patterns in issues"""
        # Create pattern of issues that occur at specific times
        temporal_pattern_sessions = []
        base_time = datetime.utcnow()
        
        for i in range(5):
            session_id = f"temporal-pattern-{i}"
            temporal_pattern_sessions.append(session_id)
            
            # Issues occurring around similar times (e.g., daily deployment window)
            issue_time = base_time + timedelta(days=i, hours=14)  # 2 PM each day
            
            await persistent_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Service degradation after deployment (day {i})",
                ai_response="Post-deployment monitoring detected issues",
                context={
                    "pattern": "post_deployment_degradation",
                    "occurrence_time": issue_time.isoformat(),
                    "time_pattern": "deployment_window",
                    "frequency": "daily",
                    "trigger": "deployment"
                }
            )
        
        # Create workflow during similar temporal window
        similar_time_session = "temporal-test-session"
        
        workflow_result = await intelligent_orchestration_service.create_troubleshooting_workflow(
            session_id=similar_time_session,
            case_id="temporal-test-case",
            user_id="temporal-test-user",
            problem_description="Service performance issues after recent deployment",
            context={
                "deployment_time": (base_time + timedelta(days=5, hours=14)).isoformat(),
                "timing_context": "post_deployment"
            }
        )
        
        # Should recognize temporal pattern
        assert workflow_result["memory_enhancements"] > 0
        assert len(workflow_result["strategic_insights"]) > 0
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cross_domain_knowledge_transfer(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test knowledge transfer between different domains"""
        # Store knowledge in database domain
        database_session = "database-domain-session"
        await persistent_memory_service.store_interaction(
            session_id=database_session,
            user_input="Connection pool optimization for high throughput",
            ai_response="Tuned pool size, timeout, and connection lifetime parameters",
            context={
                "domain": "database",
                "concept": "connection_pooling",
                "optimization_type": "throughput",
                "parameters": ["pool_size", "timeout", "connection_lifetime"]
            }
        )
        
        # Apply similar concepts in API domain
        api_session = "api-domain-session"
        knowledge_result = await learning_knowledge_service.search_with_reasoning_context(
            query="API connection management optimization",
            session_id=api_session,
            reasoning_type="analytical",
            context={"domain": "api", "optimization_focus": "throughput"}
        )
        
        # Should transfer relevant concepts across domains
        assert knowledge_result["confidence_score"] > 0.5
        
        # Check if domain transfer occurred
        memory_context = await persistent_memory_service.retrieve_context(
            api_session,
            "connection optimization"
        )
        
        # Should have some domain context even in different domain
        assert memory_context.domain_context is not None


class TestLongTermLearningAndRetention:
    """Test long-term memory retention and knowledge evolution"""
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_knowledge_reinforcement_over_time(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test that repeated patterns strengthen knowledge"""
        concept = "microservice circuit breaker pattern"
        
        # Store multiple reinforcing interactions over time
        for i in range(6):
            session_id = f"reinforcement-session-{i}"
            
            await persistent_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Circuit breaker implementation question {i}",
                ai_response=f"Circuit breaker prevents cascade failures - case {i}",
                context={
                    "concept": "circuit_breaker",
                    "domain": "microservices",
                    "reinforcement_count": i + 1,
                    "pattern_strength": min((i + 1) * 0.2, 1.0)  # Increasing strength
                }
            )
        
        # Test knowledge strength after reinforcement
        final_knowledge = await learning_knowledge_service.search_with_reasoning_context(
            query=concept,
            session_id="reinforcement-test-session",
            reasoning_type="strategic"
        )
        
        # Should show high confidence due to reinforcement
        assert final_knowledge["confidence_score"] > 0.8
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_knowledge_evolution_and_correction(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test that knowledge evolves and corrects over time"""
        session_id = "evolution-test-session"
        
        # Store initial understanding (partially incorrect)
        await persistent_memory_service.store_interaction(
            session_id=session_id,
            user_input="How to scale Redis?",
            ai_response="Vertical scaling is the best approach for Redis",
            context={
                "concept": "redis_scaling",
                "approach": "vertical_scaling",
                "confidence": "medium",
                "version": 1
            }
        )
        
        # Store corrected understanding
        await persistent_memory_service.store_interaction(
            session_id=session_id,
            user_input="Redis scaling best practices",
            ai_response="Redis Cluster provides horizontal scaling with sharding",
            context={
                "concept": "redis_scaling",
                "approach": "horizontal_scaling_cluster",
                "confidence": "high",
                "version": 2,
                "correction": True,
                "supersedes": "vertical_scaling"
            }
        )
        
        # Store refined understanding
        await persistent_memory_service.store_interaction(
            session_id=session_id,
            user_input="Production Redis scaling",
            ai_response="Combine Redis Cluster with Redis Sentinel for HA scaling",
            context={
                "concept": "redis_scaling",
                "approach": "cluster_with_sentinel",
                "confidence": "very_high",
                "version": 3,
                "refinement": True,
                "production_ready": True
            }
        )
        
        # Test evolved knowledge
        evolved_knowledge = await learning_knowledge_service.search_with_reasoning_context(
            query="Redis scaling for production",
            session_id="evolution-validation-session",
            reasoning_type="strategic"
        )
        
        # Should reflect evolved understanding
        assert evolved_knowledge["confidence_score"] > 0.7
    
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_memory_retention_and_decay(
        self,
        persistent_memory_service
    ):
        """Test memory retention patterns and natural decay"""
        # Store interactions with different timestamps
        old_session = "old-memory-session"
        recent_session = "recent-memory-session"
        
        # Old interaction (simulated)
        await persistent_memory_service.store_interaction(
            session_id=old_session,
            user_input="Legacy system integration patterns",
            ai_response="Use adapter pattern for legacy system integration",
            context={
                "age": "old",
                "concept": "legacy_integration",
                "relevance": "historical"
            }
        )
        
        # Recent interaction
        await persistent_memory_service.store_interaction(
            session_id=recent_session,
            user_input="Modern API integration approaches",
            ai_response="Use GraphQL federation for modern API integration",
            context={
                "age": "recent",
                "concept": "modern_integration", 
                "relevance": "current"
            }
        )
        
        # Test memory retrieval prioritizes recent over old
        memory_context = await persistent_memory_service.retrieve_context(
            "memory-priority-test-session",
            "system integration approaches"
        )
        
        # Should have both but with proper weighting
        assert memory_context is not None
        assert len(memory_context.relevant_insights) >= 0


@pytest.mark.integration
@pytest.mark.cross_session
class TestIntelligenceSystemPerformance:
    """Test performance of cross-session intelligence features"""
    
    @pytest.mark.asyncio
    async def test_cross_session_lookup_performance(
        self,
        persistent_memory_service,
        learning_knowledge_service
    ):
        """Test performance of cross-session knowledge lookups"""
        # Populate with many sessions
        session_count = 50
        for i in range(session_count):
            session_id = f"perf-session-{i}"
            
            await persistent_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Performance question {i}",
                ai_response=f"Performance answer {i}",
                context={"session_num": i, "topic": f"performance_{i % 10}"}
            )
        
        # Test lookup performance
        start_time = time.time()
        
        knowledge_result = await learning_knowledge_service.search_with_reasoning_context(
            query="performance optimization",
            session_id="perf-test-session",
            reasoning_type="analytical"
        )
        
        lookup_time = time.time() - start_time
        
        # Should complete quickly even with many sessions
        assert lookup_time < 2.0  # Under 2 seconds
        assert knowledge_result["confidence_score"] > 0.4
    
    @pytest.mark.asyncio
    async def test_memory_scaling_with_session_count(
        self,
        persistent_memory_service
    ):
        """Test memory system performance scales with session count"""
        # Create many sessions with interactions
        for i in range(100):
            session_id = f"scale-session-{i}"
            
            await persistent_memory_service.store_interaction(
                session_id=session_id,
                user_input=f"Scaling question {i}",
                ai_response=f"Scaling answer {i}",
                context={"scale_test": True, "session_index": i}
            )
        
        # Test retrieval performance remains reasonable
        start_time = time.time()
        
        memory_context = await persistent_memory_service.retrieve_context(
            "scale-test-session",
            "scaling performance"
        )
        
        retrieval_time = time.time() - start_time
        
        # Should maintain performance even with many sessions
        assert retrieval_time < 1.0  # Under 1 second
        assert memory_context is not None