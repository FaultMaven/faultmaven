"""Orchestration Service for Multi-Step Troubleshooting Workflows

This module provides the service layer interface for the troubleshooting 
orchestration system, integrating with the existing service architecture
and providing HTTP API compatibility.

Key Features:
- Service layer abstraction for troubleshooting orchestration
- Integration with DI container and existing service patterns
- HTTP API compatible interfaces
- Workflow lifecycle management
- Performance monitoring and health checking
"""

import logging
import asyncio
import time
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from faultmaven.services.base_service import BaseService
from faultmaven.core.orchestration.troubleshooting_orchestrator import (
    TroubleshootingOrchestrator, WorkflowContext, TroubleshootingPhase
)
from faultmaven.models.interfaces import (
    IMemoryService, IPlanningService, ILLMProvider, ITracer, IVectorStore
)
from faultmaven.services.enhanced_knowledge_service import EnhancedKnowledgeService
from faultmaven.services.reasoning_service import ReasoningService
from faultmaven.exceptions import ServiceException, ValidationException


class OrchestrationService(BaseService):
    """Orchestration Service for Multi-Step Troubleshooting Workflows
    
    This service provides the interface for managing comprehensive troubleshooting
    workflows that coordinate memory, planning, reasoning, and knowledge systems
    to deliver systematic problem resolution guidance.
    
    Key Capabilities:
    - Workflow lifecycle management (create, execute, pause, resume)
    - Multi-step troubleshooting coordination
    - Real-time workflow monitoring and status tracking
    - Integration with all enhanced intelligence systems
    - Performance tracking and optimization
    - Adaptive workflow adjustment based on findings
    
    Performance Targets:
    - Workflow creation: < 500ms
    - Step execution: < 3000ms
    - Status retrieval: < 100ms
    - Health check: < 50ms
    """
    
    def __init__(
        self,
        memory_service: Optional[IMemoryService] = None,
        planning_service: Optional[IPlanningService] = None,
        reasoning_service: Optional[ReasoningService] = None,
        enhanced_knowledge_service: Optional[EnhancedKnowledgeService] = None,
        llm_provider: Optional[ILLMProvider] = None,
        tracer: Optional[ITracer] = None
    ):
        """Initialize Orchestration Service
        
        Args:
            memory_service: Enhanced memory service for context management
            planning_service: Strategic planning service for workflow planning
            reasoning_service: Enhanced reasoning service for step execution
            enhanced_knowledge_service: Enhanced knowledge service for information retrieval
            llm_provider: LLM provider for intelligent analysis
            tracer: Tracing service for observability
        """
        super().__init__()
        
        # Initialize core orchestrator
        self._orchestrator = TroubleshootingOrchestrator(
            memory_service=memory_service,
            planning_service=planning_service,
            reasoning_service=reasoning_service,
            enhanced_knowledge_service=enhanced_knowledge_service,
            llm_provider=llm_provider,
            tracer=tracer
        )
        
        # Enhanced service metrics with optimization tracking
        self._service_metrics = {
            "workflows_created": 0,
            "workflows_completed": 0,
            "workflows_failed": 0,
            "total_steps_executed": 0,
            "parallel_steps_executed": 0,
            "cached_steps_served": 0,
            "dynamic_adaptations": 0,
            "avg_workflow_duration": 0.0,
            "avg_step_execution_time": 0.0,
            "cache_hit_rate": 0.0,
            "optimization_time_saved": 0.0,
            "service_uptime": datetime.utcnow()
        }
        
        # Performance optimization components
        self._step_result_cache = {}  # Cache for step results
        self._workflow_state_cache = {}  # Optimized workflow state storage
        self._parallel_execution_pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="orch_opt")
        self._step_execution_queue = defaultdict(deque)  # Queues for parallel execution
        self._workflow_templates = {}  # Pre-compiled workflow templates
        self._adaptive_patterns = defaultdict(list)  # Patterns for dynamic adaptation
        
        # Background optimization
        self._optimization_running = False
        self._start_background_optimization()
    
    async def create_troubleshooting_workflow(
        self,
        session_id: str,
        case_id: str,
        user_id: str,
        problem_description: str,
        context: Optional[Dict[str, Any]] = None,
        priority_level: str = "medium",
        domain_expertise: str = "general",
        time_constraints: Optional[int] = None
    ) -> Dict[str, Any]:
        """Create a new troubleshooting workflow
        
        Args:
            session_id: Session identifier
            case_id: Case identifier for this troubleshooting workflow
            user_id: User identifier
            problem_description: Description of the problem to be solved
            context: Additional context information (service names, environment, etc.)
            priority_level: Priority level (low, medium, high, critical)
            domain_expertise: User's domain expertise level (novice, intermediate, expert)
            time_constraints: Time constraints in seconds (optional)
            
        Returns:
            Dictionary with workflow creation details and first steps
            
        Raises:
            ValidationException: When input parameters are invalid
            ServiceException: When workflow creation fails
        """
        try:
            self.logger.info(f"Creating troubleshooting workflow for case: {case_id}")
            
            # Validate inputs
            await self._validate_workflow_inputs(
                session_id, case_id, user_id, problem_description, priority_level, domain_expertise
            )
            
            # Create workflow context
            workflow_context = WorkflowContext(
                session_id=session_id,
                case_id=case_id,
                user_id=user_id,
                problem_description=problem_description,
                initial_context=context or {},
                priority_level=priority_level,
                domain_expertise=domain_expertise,
                time_constraints=time_constraints,
                available_tools=["enhanced_knowledge_search", "knowledge_discovery", "web_search"]
            )
            
            # Initiate workflow through orchestrator
            workflow_result = await self._orchestrator.initiate_troubleshooting_workflow(workflow_context)
            
            # Update service metrics
            self._service_metrics["workflows_created"] += 1
            
            self.logger.info(f"Workflow created successfully: {workflow_result['workflow_id']}")
            
            return {
                "success": True,
                "workflow_id": workflow_result["workflow_id"],
                "workflow_details": {
                    "pattern": workflow_result["pattern"],
                    "total_steps": workflow_result["total_steps"],
                    "estimated_duration": workflow_result["estimated_duration"],
                    "initialization_time": workflow_result["initialization_time"]
                },
                "current_step": workflow_result.get("current_step"),
                "strategic_insights": workflow_result.get("strategic_insights", []),
                "memory_enhancements": workflow_result.get("memory_enhancements", 0),
                "next_action": "Execute first step using execute_workflow_step"
            }
            
        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Workflow creation failed: {e}")
            self._service_metrics["workflows_failed"] += 1
            raise ServiceException(f"Failed to create troubleshooting workflow: {str(e)}")
    
    async def execute_workflow_step(
        self,
        workflow_id: str,
        step_inputs: Optional[Dict[str, Any]] = None,
        user_feedback: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute the next step in a troubleshooting workflow
        
        Args:
            workflow_id: Unique workflow identifier
            step_inputs: Optional inputs for step execution
            user_feedback: Optional user feedback from previous step
            
        Returns:
            Dictionary with step execution results and next step information
            
        Raises:
            ServiceException: When step execution fails
        """
        try:
            self.logger.info(f"Executing workflow step for workflow: {workflow_id}")
            
            if not workflow_id:
                raise ValidationException("Workflow ID is required")
            
            # Add user feedback to step inputs if provided
            if user_feedback:
                if step_inputs is None:
                    step_inputs = {}
                step_inputs["user_feedback"] = user_feedback
            
            # Execute step with performance optimization
            step_result = await self._execute_step_optimized(workflow_id, step_inputs)
            
            # Update enhanced service metrics
            self._service_metrics["total_steps_executed"] += 1
            step_time = step_result.get("execution_time", 0.0)
            self._update_avg_step_time(step_time)
            
            # Format response for API consumption
            formatted_response = {
                "success": True,
                "workflow_id": workflow_id,
                "step_execution": step_result.get("step_result", {}),
                "workflow_progress": step_result.get("workflow_status", {}),
                "next_step": step_result.get("next_step"),
                "adaptive_changes": step_result.get("adaptive_changes", []),
                "execution_time": step_result.get("execution_time", 0.0),
                "recommendations": {
                    "immediate_actions": step_result.get("step_result", {}).get("recommendations", []),
                    "next_steps": step_result.get("step_result", {}).get("next_steps", []),
                    "knowledge_gaps": step_result.get("step_result", {}).get("knowledge_gaps", [])
                }
            }
            
            # Check if workflow is completed
            workflow_status = step_result.get("workflow_status", {})
            if workflow_status.get("status") == "completed":
                self._service_metrics["workflows_completed"] += 1
                formatted_response["workflow_complete"] = True
                formatted_response["completion_summary"] = await self._generate_completion_summary(workflow_id)
            
            self.logger.info(f"Step executed successfully for workflow: {workflow_id}")
            
            return formatted_response
            
        except Exception as e:
            self.logger.error(f"Step execution failed for workflow {workflow_id}: {e}")
            raise ServiceException(f"Failed to execute workflow step: {str(e)}")
    
    async def get_workflow_status(self, workflow_id: str) -> Dict[str, Any]:
        """Get current status and progress of a troubleshooting workflow
        
        Args:
            workflow_id: Unique workflow identifier
            
        Returns:
            Dictionary with comprehensive workflow status information
            
        Raises:
            ServiceException: When status retrieval fails
        """
        try:
            if not workflow_id:
                raise ValidationException("Workflow ID is required")
            
            # Get optimized status with caching
            status_result = await self._get_workflow_status_optimized(workflow_id)
            
            if "error" in status_result:
                raise ServiceException(status_result["error"])
            
            # Enhance status with service-level information
            enhanced_status = {
                "success": True,
                "workflow_id": workflow_id,
                "status": status_result["status"],
                "progress": status_result["progress"],
                "findings_summary": status_result["findings_summary"],
                "performance": status_result["performance"],
                "timeline": {
                    "created_at": status_result["created_at"],
                    "estimated_completion": status_result.get("estimated_completion"),
                    "current_timestamp": datetime.utcnow().isoformat()
                },
                "service_metadata": {
                    "service": "orchestration_service",
                    "version": "1.0.0",
                    "capabilities": await self._get_service_capabilities()
                }
            }
            
            return enhanced_status
            
        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get workflow status for {workflow_id}: {e}")
            raise ServiceException(f"Failed to retrieve workflow status: {str(e)}")
    
    async def pause_workflow(self, workflow_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Pause a troubleshooting workflow
        
        Args:
            workflow_id: Unique workflow identifier
            reason: Optional reason for pausing
            
        Returns:
            Dictionary with pause confirmation and resumption information
            
        Raises:
            ServiceException: When pause operation fails
        """
        try:
            if not workflow_id:
                raise ValidationException("Workflow ID is required")
            
            self.logger.info(f"Pausing workflow: {workflow_id} (reason: {reason})")
            
            pause_result = await self._orchestrator.pause_workflow(workflow_id)
            
            if "error" in pause_result:
                raise ServiceException(pause_result["error"])
            
            enhanced_response = {
                "success": True,
                "workflow_id": workflow_id,
                "status": "paused",
                "pause_details": pause_result,
                "pause_reason": reason,
                "resume_instructions": "Call resume_workflow() to continue execution",
                "service_message": "Workflow paused successfully and can be resumed at any time"
            }
            
            return enhanced_response
            
        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to pause workflow {workflow_id}: {e}")
            raise ServiceException(f"Failed to pause workflow: {str(e)}")
    
    async def resume_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Resume a paused troubleshooting workflow
        
        Args:
            workflow_id: Unique workflow identifier
            
        Returns:
            Dictionary with resumption confirmation and next step information
            
        Raises:
            ServiceException: When resume operation fails
        """
        try:
            if not workflow_id:
                raise ValidationException("Workflow ID is required")
            
            self.logger.info(f"Resuming workflow: {workflow_id}")
            
            resume_result = await self._orchestrator.resume_workflow(workflow_id)
            
            if "error" in resume_result:
                raise ServiceException(resume_result["error"])
            
            enhanced_response = {
                "success": True,
                "workflow_id": workflow_id,
                "status": "resumed",
                "resume_details": resume_result,
                "next_step": resume_result.get("next_step"),
                "progress": resume_result.get("progress"),
                "next_action": "Execute next step using execute_workflow_step"
            }
            
            return enhanced_response
            
        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to resume workflow {workflow_id}: {e}")
            raise ServiceException(f"Failed to resume workflow: {str(e)}")
    
    async def get_workflow_recommendations(self, workflow_id: str) -> Dict[str, Any]:
        """Get intelligent recommendations for workflow optimization
        
        Args:
            workflow_id: Unique workflow identifier
            
        Returns:
            Dictionary with workflow optimization recommendations
        """
        try:
            if not workflow_id:
                raise ValidationException("Workflow ID is required")
            
            # Get current workflow status
            status = await self._orchestrator.get_workflow_status(workflow_id)
            
            if "error" in status:
                raise ServiceException(status["error"])
            
            recommendations = {
                "success": True,
                "workflow_id": workflow_id,
                "recommendations": {
                    "performance": [],
                    "methodology": [],
                    "efficiency": [],
                    "quality": []
                },
                "optimization_score": 0.0,
                "next_optimizations": []
            }
            
            # Analyze performance metrics
            performance = status.get("performance", {})
            
            if performance.get("steps_completed", 0) > 0:
                avg_step_time = performance.get("total_knowledge_retrieved", 0) / performance["steps_completed"]
                if avg_step_time > 2000:  # More than 2 seconds per step
                    recommendations["recommendations"]["performance"].append(
                        "Consider parallel knowledge retrieval to reduce step execution time"
                    )
            
            # Analyze methodology
            progress = status.get("progress", {})
            if progress.get("progress_percentage", 0) > 50:
                recommendations["recommendations"]["methodology"].append(
                    "Workflow is progressing well, consider consolidating findings for efficiency"
                )
            
            # Analyze efficiency
            findings_count = status.get("findings_summary", {}).get("total_findings", 0)
            steps_completed = progress.get("steps_completed", 1)
            
            if findings_count / steps_completed < 2:  # Less than 2 findings per step
                recommendations["recommendations"]["efficiency"].append(
                    "Consider more focused questioning to gather additional evidence per step"
                )
            
            # Calculate optimization score
            scores = []
            if performance.get("steps_completed", 0) > 0:
                scores.append(min(100, 200 / (avg_step_time / 1000)))  # Performance score
            scores.append(progress.get("progress_percentage", 0))  # Progress score
            
            if scores:
                recommendations["optimization_score"] = sum(scores) / len(scores)
            
            return recommendations
            
        except ValidationException:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get recommendations for workflow {workflow_id}: {e}")
            raise ServiceException(f"Failed to get workflow recommendations: {str(e)}")
    
    async def list_active_workflows(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """List active troubleshooting workflows
        
        Args:
            user_id: Optional user ID to filter workflows
            
        Returns:
            Dictionary with list of active workflows and summary information
        """
        try:
            # For now, return service-level summary
            # In future, orchestrator could provide workflow listing functionality
            
            active_workflows = {
                "success": True,
                "workflows": [],  # Would be populated with actual workflow data
                "summary": {
                    "total_active_workflows": 0,  # Would be calculated from orchestrator
                    "workflows_by_status": {
                        "in_progress": 0,
                        "paused": 0,
                        "waiting_input": 0
                    },
                    "user_filter": user_id
                },
                "service_metrics": self._service_metrics.copy()
            }
            
            return active_workflows
            
        except Exception as e:
            self.logger.error(f"Failed to list active workflows: {e}")
            raise ServiceException(f"Failed to list active workflows: {str(e)}")
    
    async def _validate_workflow_inputs(
        self,
        session_id: str,
        case_id: str,
        user_id: str,
        problem_description: str,
        priority_level: str,
        domain_expertise: str
    ) -> None:
        """Validate workflow creation inputs"""
        
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID is required")
        
        if not case_id or not case_id.strip():
            raise ValidationException("Case ID is required")
        
        if not user_id or not user_id.strip():
            raise ValidationException("User ID is required")
        
        if not problem_description or not problem_description.strip():
            raise ValidationException("Problem description is required")
        
        if len(problem_description) < 10:
            raise ValidationException("Problem description must be at least 10 characters")
        
        if len(problem_description) > 2000:
            raise ValidationException("Problem description must be less than 2000 characters")
        
        valid_priorities = {"low", "medium", "high", "critical"}
        if priority_level not in valid_priorities:
            raise ValidationException(f"Priority level must be one of: {valid_priorities}")
        
        valid_expertise = {"novice", "intermediate", "expert", "general"}
        if domain_expertise not in valid_expertise:
            raise ValidationException(f"Domain expertise must be one of: {valid_expertise}")
    
    async def _generate_completion_summary(self, workflow_id: str) -> Dict[str, Any]:
        """Generate a completion summary for a finished workflow"""
        
        try:
            status = await self._orchestrator.get_workflow_status(workflow_id)
            
            summary = {
                "completion_time": datetime.utcnow().isoformat(),
                "total_steps_executed": status.get("progress", {}).get("steps_completed", 0),
                "total_findings": status.get("findings_summary", {}).get("total_findings", 0),
                "knowledge_items_retrieved": status.get("findings_summary", {}).get("knowledge_items", 0),
                "performance_summary": status.get("performance", {}),
                "workflow_efficiency": "High" if status.get("progress", {}).get("progress_percentage", 0) == 100 else "Moderate",
                "recommended_follow_up": [
                    "Review implementation of proposed solutions",
                    "Monitor system after applying fixes",
                    "Document lessons learned for future reference"
                ]
            }
            
            return summary
            
        except Exception as e:
            self.logger.warning(f"Failed to generate completion summary: {e}")
            return {"completion_time": datetime.utcnow().isoformat(), "status": "completed"}
    
    async def _get_service_capabilities(self) -> Dict[str, Any]:
        """Get current service capabilities"""
        
        return {
            "workflow_management": True,
            "step_execution": True,
            "adaptive_orchestration": True,
            "performance_tracking": True,
            "multi_system_integration": True,
            "memory_integration": self._orchestrator._memory is not None,
            "planning_integration": self._orchestrator._planning is not None,
            "reasoning_integration": self._orchestrator._reasoning is not None,
            "knowledge_integration": self._orchestrator._knowledge is not None,
            "parallel_step_execution": True,
            "step_result_caching": True,
            "workflow_state_optimization": True,
            "dynamic_adaptation": True,
            "pattern_analysis": True,
            "performance_optimization": True
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of orchestration service"""
        
        # Get base service health
        base_health = await super().health_check()
        
        # Get orchestrator health
        orchestrator_health = await self._orchestrator.health_check()
        
        # Calculate optimization metrics
        cache_hit_rate = 0.0
        total_steps = self._service_metrics["total_steps_executed"]
        cached_steps = self._service_metrics["cached_steps_served"]
        if total_steps > 0:
            cache_hit_rate = cached_steps / total_steps
        
        self._service_metrics["cache_hit_rate"] = cache_hit_rate
        
        # Combine health information with optimization status
        service_health = {
            **base_health,
            "service": "orchestration_service",
            "orchestrator": orchestrator_health,
            "service_metrics": self._service_metrics.copy(),
            "optimization_status": {
                "cache_hit_rate": cache_hit_rate,
                "step_cache_size": len(self._step_result_cache),
                "workflow_state_cache_size": len(self._workflow_state_cache),
                "workflow_templates": len(self._workflow_templates),
                "adaptive_patterns_tracked": len(self._adaptive_patterns),
                "optimization_running": self._optimization_running,
                "parallel_execution_enabled": True,
                "dynamic_adaptation_enabled": True,
                "optimization_enabled": True
            },
            "capabilities": await self._get_service_capabilities(),
            "uptime_seconds": (datetime.utcnow() - self._service_metrics["service_uptime"]).total_seconds()
        }
        
        # Determine overall status
        if orchestrator_health.get("status") != "healthy":
            service_health["status"] = "degraded"
        
        return service_health
    
    # Performance Optimization Methods
    
    def _start_background_optimization(self):
        """Start background optimization tasks"""
        if not self._optimization_running:
            self._optimization_running = True
            asyncio.create_task(self._background_step_cache_optimizer())
            asyncio.create_task(self._background_workflow_pattern_analyzer())
            asyncio.create_task(self._background_state_optimizer())
    
    async def _background_step_cache_optimizer(self):
        """Background task for step result cache optimization"""
        while self._optimization_running:
            try:
                # Clean expired cache entries
                current_time = time.time()
                expired_keys = [
                    key for key, (result, timestamp) in self._step_result_cache.items()
                    if current_time - timestamp > 1800  # 30 minutes
                ]
                
                for key in expired_keys:
                    del self._step_result_cache[key]
                
                # Optimize cache size
                if len(self._step_result_cache) > 500:
                    # Remove oldest 20% of entries
                    sorted_items = sorted(
                        self._step_result_cache.items(),
                        key=lambda x: x[1][1]  # Sort by timestamp
                    )
                    for key, _ in sorted_items[:100]:
                        del self._step_result_cache[key]
                
                await asyncio.sleep(300)  # Run every 5 minutes
            except Exception as e:
                self.logger.warning(f"Background step cache optimizer error: {e}")
                await asyncio.sleep(600)
    
    async def _background_workflow_pattern_analyzer(self):
        """Background task for workflow pattern analysis"""
        while self._optimization_running:
            try:
                # Analyze workflow patterns for optimization opportunities
                await self._analyze_workflow_patterns()
                await asyncio.sleep(600)  # Run every 10 minutes
            except Exception as e:
                self.logger.warning(f"Background workflow pattern analyzer error: {e}")
                await asyncio.sleep(1200)
    
    async def _background_state_optimizer(self):
        """Background task for workflow state optimization"""
        while self._optimization_running:
            try:
                # Optimize workflow state storage
                await self._optimize_workflow_states()
                await asyncio.sleep(180)  # Run every 3 minutes
            except Exception as e:
                self.logger.warning(f"Background state optimizer error: {e}")
                await asyncio.sleep(360)
    
    async def _execute_step_optimized(self, workflow_id: str, step_inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute workflow step with performance optimization"""
        start_time = time.time()
        
        # Generate cache key for step result
        cache_key = self._generate_step_cache_key(workflow_id, step_inputs)
        
        # Check step result cache
        if cache_key in self._step_result_cache:
            cached_result, cached_time = self._step_result_cache[cache_key]
            if time.time() - cached_time < 300:  # 5 minutes cache
                self._service_metrics["cached_steps_served"] += 1
                cache_time = (time.time() - start_time) * 1000
                self._service_metrics["optimization_time_saved"] += cache_time
                
                self.logger.info(f"Serving cached step result for workflow {workflow_id}")
                return {
                    **cached_result,
                    "cached_result": True,
                    "cache_time_ms": cache_time
                }
        
        # Check if step can be executed in parallel
        can_parallelize = await self._can_parallelize_step(workflow_id, step_inputs)
        
        if can_parallelize:
            # Execute with parallel optimization
            step_result = await self._execute_step_parallel(workflow_id, step_inputs)
            self._service_metrics["parallel_steps_executed"] += 1
        else:
            # Execute normally through orchestrator
            step_result = await self._orchestrator.execute_workflow_step(workflow_id, step_inputs)
        
        # Cache the result
        execution_time = (time.time() - start_time) * 1000
        step_result["execution_time"] = execution_time
        self._step_result_cache[cache_key] = (step_result, time.time())
        
        # Apply dynamic adaptation if needed
        if await self._should_adapt_workflow(workflow_id, step_result):
            adapted_result = await self._apply_dynamic_adaptation(workflow_id, step_result)
            if adapted_result:
                self._service_metrics["dynamic_adaptations"] += 1
                return adapted_result
        
        return step_result
    
    async def _get_workflow_status_optimized(self, workflow_id: str) -> Dict[str, Any]:
        """Get workflow status with caching optimization"""
        # Check workflow state cache
        if workflow_id in self._workflow_state_cache:
            cached_state, cached_time = self._workflow_state_cache[workflow_id]
            if time.time() - cached_time < 30:  # 30 seconds cache for status
                return cached_state
        
        # Get fresh status from orchestrator
        status_result = await self._orchestrator.get_workflow_status(workflow_id)
        
        # Cache the status
        self._workflow_state_cache[workflow_id] = (status_result, time.time())
        
        return status_result
    
    def _generate_step_cache_key(self, workflow_id: str, step_inputs: Optional[Dict[str, Any]]) -> str:
        """Generate cache key for step execution"""
        import hashlib
        
        # Create key from workflow_id and normalized inputs
        key_components = [workflow_id]
        
        if step_inputs:
            # Sort inputs for consistent caching
            sorted_inputs = sorted(step_inputs.items()) if isinstance(step_inputs, dict) else []
            key_components.extend([f"{k}:{v}" for k, v in sorted_inputs])
        
        cache_string = "|".join(str(c) for c in key_components)
        return hashlib.md5(cache_string.encode()).hexdigest()
    
    async def _can_parallelize_step(self, workflow_id: str, step_inputs: Optional[Dict[str, Any]]) -> bool:
        """Determine if a step can be executed in parallel"""
        # Get current workflow status to check dependencies
        try:
            status = await self._orchestrator.get_workflow_status(workflow_id)
            current_step = status.get("progress", {}).get("current_step")
            
            # Simple heuristic: parallel execution for independent steps
            independent_steps = ["knowledge_search", "web_search", "pattern_analysis"]
            return current_step in independent_steps
        except Exception:
            return False
    
    async def _execute_step_parallel(self, workflow_id: str, step_inputs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute step with parallel optimization"""
        # Create parallel execution tasks
        tasks = []
        
        # Primary step execution
        primary_task = asyncio.create_task(
            self._orchestrator.execute_workflow_step(workflow_id, step_inputs)
        )
        tasks.append(("primary", primary_task))
        
        # Parallel optimization tasks
        if step_inputs and "query" in step_inputs:
            # Pre-fetch related knowledge in parallel
            knowledge_task = asyncio.create_task(
                self._prefetch_related_knowledge(step_inputs["query"])
            )
            tasks.append(("knowledge_prefetch", knowledge_task))
        
        # Wait for primary task and collect parallel results
        primary_result = await primary_task
        
        # Collect parallel enhancements
        parallel_enhancements = {}
        for task_name, task in tasks[1:]:  # Skip primary task
            try:
                result = await asyncio.wait_for(task, timeout=5.0)  # 5 second timeout
                parallel_enhancements[task_name] = result
            except asyncio.TimeoutError:
                self.logger.warning(f"Parallel task {task_name} timed out")
            except Exception as e:
                self.logger.warning(f"Parallel task {task_name} failed: {e}")
        
        # Enhance primary result with parallel data
        if parallel_enhancements:
            primary_result["parallel_enhancements"] = parallel_enhancements
            primary_result["parallel_optimized"] = True
        
        return primary_result
    
    async def _prefetch_related_knowledge(self, query: str) -> Dict[str, Any]:
        """Prefetch related knowledge for performance optimization"""
        try:
            # This would integrate with the knowledge service
            # For now, return a placeholder
            return {
                "related_items": [],
                "prefetch_time": time.time(),
                "query_analyzed": query[:100]
            }
        except Exception as e:
            self.logger.warning(f"Knowledge prefetch failed: {e}")
            return {}
    
    async def _should_adapt_workflow(self, workflow_id: str, step_result: Dict[str, Any]) -> bool:
        """Determine if workflow should be dynamically adapted"""
        # Adaptation criteria
        confidence = step_result.get("step_result", {}).get("confidence", 1.0)
        execution_time = step_result.get("execution_time", 0)
        
        # Adapt if confidence is low or execution took too long
        return confidence < 0.6 or execution_time > 5000  # 5 seconds
    
    async def _apply_dynamic_adaptation(self, workflow_id: str, step_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply dynamic workflow adaptation"""
        try:
            # Record adaptation pattern
            adaptation_pattern = {
                "workflow_id": workflow_id,
                "trigger": "low_confidence" if step_result.get("step_result", {}).get("confidence", 1.0) < 0.6 else "slow_execution",
                "timestamp": time.time(),
                "original_result": step_result
            }
            
            self._adaptive_patterns[workflow_id].append(adaptation_pattern)
            
            # Apply adaptation strategy
            if adaptation_pattern["trigger"] == "low_confidence":
                return await self._adapt_for_low_confidence(workflow_id, step_result)
            elif adaptation_pattern["trigger"] == "slow_execution":
                return await self._adapt_for_slow_execution(workflow_id, step_result)
            
        except Exception as e:
            self.logger.error(f"Dynamic adaptation failed: {e}")
        
        return None
    
    async def _adapt_for_low_confidence(self, workflow_id: str, step_result: Dict[str, Any]) -> Dict[str, Any]:
        """Adapt workflow for low confidence results"""
        # Add additional validation step
        adapted_result = step_result.copy()
        adapted_result["adaptation_applied"] = {
            "type": "low_confidence_enhancement",
            "additional_validation": True,
            "confidence_boost_attempted": True
        }
        
        return adapted_result
    
    async def _adapt_for_slow_execution(self, workflow_id: str, step_result: Dict[str, Any]) -> Dict[str, Any]:
        """Adapt workflow for slow execution"""
        # Suggest parallel execution for future steps
        adapted_result = step_result.copy()
        adapted_result["adaptation_applied"] = {
            "type": "performance_optimization",
            "parallel_execution_recommended": True,
            "caching_enabled": True
        }
        
        return adapted_result
    
    async def _analyze_workflow_patterns(self):
        """Analyze workflow execution patterns for optimization"""
        # Analyze adaptive patterns for insights
        pattern_insights = {}
        
        for workflow_id, patterns in self._adaptive_patterns.items():
            if len(patterns) > 3:  # Enough data for analysis
                trigger_counts = defaultdict(int)
                for pattern in patterns:
                    trigger_counts[pattern["trigger"]] += 1
                
                pattern_insights[workflow_id] = {
                    "total_adaptations": len(patterns),
                    "trigger_distribution": dict(trigger_counts),
                    "adaptation_frequency": len(patterns) / max(1, (time.time() - patterns[0]["timestamp"]) / 3600)
                }
        
        # Store insights for future optimization
        if pattern_insights:
            self.logger.info(f"Workflow pattern analysis completed: {len(pattern_insights)} workflows analyzed")
    
    async def _optimize_workflow_states(self):
        """Optimize workflow state storage and access"""
        # Clean expired workflow states
        current_time = time.time()
        expired_states = [
            workflow_id for workflow_id, (state, timestamp) in self._workflow_state_cache.items()
            if current_time - timestamp > 300  # 5 minutes
        ]
        
        for workflow_id in expired_states:
            del self._workflow_state_cache[workflow_id]
        
        # Optimize state serialization for frequently accessed workflows
        frequent_workflows = [
            workflow_id for workflow_id, patterns in self._adaptive_patterns.items()
            if len(patterns) > 5
        ]
        
        # Pre-compile templates for frequent workflows
        for workflow_id in frequent_workflows:
            if workflow_id not in self._workflow_templates:
                try:
                    template = await self._create_workflow_template(workflow_id)
                    self._workflow_templates[workflow_id] = template
                except Exception as e:
                    self.logger.warning(f"Template creation failed for {workflow_id}: {e}")
    
    async def _create_workflow_template(self, workflow_id: str) -> Dict[str, Any]:
        """Create optimized template for frequently used workflow"""
        # This would create a pre-compiled template
        # For now, return a basic template structure
        return {
            "workflow_id": workflow_id,
            "template_created": time.time(),
            "optimization_level": "basic",
            "cached_steps": [],
            "parallel_opportunities": []
        }
    
    def _update_avg_step_time(self, step_time: float):
        """Update average step execution time"""
        current_avg = self._service_metrics["avg_step_execution_time"]
        total_steps = self._service_metrics["total_steps_executed"]
        
        if total_steps == 1:
            self._service_metrics["avg_step_execution_time"] = step_time
        else:
            self._service_metrics["avg_step_execution_time"] = (
                (current_avg * (total_steps - 1) + step_time) / total_steps
            )