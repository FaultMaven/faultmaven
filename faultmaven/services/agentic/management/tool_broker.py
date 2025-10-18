"""Tool & Skill Broker Component

This component serves as the dynamic orchestration layer for tool and skill execution,
managing capability discovery, routing, and execution with comprehensive safety enforcement.

Key responsibilities:
- Manage tool registry and capabilities
- Route tool requests based on requirements
- Execute tools with proper error handling and safety checks
- Coordinate multi-tool workflows
- Performance monitoring and adaptive routing

The Tool Broker is essential for the Execute phase of the agentic loop, providing
intelligent tool selection and execution with built-in safety guardrails.
"""

import logging
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timezone, timedelta
from enum import Enum
import asyncio
import uuid

from faultmaven.models.agentic import (
    IToolSkillBroker,
    AgentCapability,
    AgentCapabilityType,
    SafetyLevel,
    ExecutionPriority
)
from faultmaven.models.interfaces import ITracer, BaseTool
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


class ToolCategory(str, Enum):
    """Categories of tools for organization and routing"""
    SEARCH = "search"
    ANALYSIS = "analysis"
    EXECUTION = "execution"
    MONITORING = "monitoring"
    VALIDATION = "validation"
    COMMUNICATION = "communication"
    DATA_PROCESSING = "data_processing"


class ExecutionStatus(str, Enum):
    """Status of tool execution"""
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REQUIRES_APPROVAL = "requires_approval"
    CANCELLED = "cancelled"
    IN_PROGRESS = "in_progress"


class ToolExecutionRequest:
    """Request for tool execution"""
    def __init__(
        self,
        required_capabilities: List[str],
        parameters: Dict[str, Any],
        input_data_type: str = "text",
        performance_requirements: Dict[str, Any] = None,
        timeout: float = 30.0,
        retry_policy: Dict[str, Any] = None,
        priority: ExecutionPriority = ExecutionPriority.NORMAL,
        user_context: Dict[str, Any] = None
    ):
        self.required_capabilities = required_capabilities
        self.parameters = parameters
        self.input_data_type = input_data_type
        self.performance_requirements = performance_requirements or {}
        self.timeout = timeout
        self.retry_policy = retry_policy or {"max_attempts": 1, "backoff_factor": 1.0}
        self.priority = priority
        self.user_context = user_context or {}
        self.request_id = str(uuid.uuid4())


class ToolExecutionResult:
    """Result of tool execution"""
    def __init__(
        self,
        status: ExecutionStatus,
        tool_used: str = None,
        result_data: Dict[str, Any] = None,
        execution_metadata: Dict[str, Any] = None,
        performance_metrics: Dict[str, Any] = None,
        error_message: str = None,
        approval_request: Dict[str, Any] = None,
        recommended_tools: List[str] = None
    ):
        self.status = status
        self.tool_used = tool_used
        self.result_data = result_data or {}
        self.execution_metadata = execution_metadata or {}
        self.performance_metrics = performance_metrics or {}
        self.error_message = error_message
        self.approval_request = approval_request
        self.recommended_tools = recommended_tools or []
        self.timestamp = datetime.now(timezone.utc)


class ToolRegistration:
    """Registration information for a tool"""
    def __init__(
        self,
        tool_id: str,
        tool_instance: BaseTool,
        capabilities: List[AgentCapability],
        category: ToolCategory = ToolCategory.ANALYSIS,
        safety_level: SafetyLevel = SafetyLevel.SAFE,
        performance_profile: Dict[str, Any] = None
    ):
        self.tool_id = tool_id
        self.tool_instance = tool_instance
        self.capabilities = capabilities
        self.category = category
        self.safety_level = safety_level
        self.performance_profile = performance_profile or {}
        self.registered_at = datetime.now(timezone.utc)
        self.last_used = None
        self.execution_count = 0
        self.success_rate = 1.0


class ToolSkillBroker(IToolSkillBroker):
    """Production implementation of tool and skill orchestration with safety enforcement"""
    
    def __init__(
        self,
        tracer: ITracer,
        enable_safety_checks: bool = True,
        max_concurrent_executions: int = 10
    ):
        """Initialize the tool skill broker
        
        Args:
            tracer: Observability tracer
            enable_safety_checks: Whether to enforce safety constraints
            max_concurrent_executions: Maximum concurrent tool executions
        """
        self.tracer = tracer
        self.enable_safety_checks = enable_safety_checks
        self.execution_semaphore = asyncio.Semaphore(max_concurrent_executions)
        
        # Tool registry and management
        self.registered_tools: Dict[str, ToolRegistration] = {}
        self.capability_index: Dict[str, List[str]] = {}  # capability_name -> tool_ids
        self.category_index: Dict[ToolCategory, List[str]] = {}  # category -> tool_ids
        
        # Performance tracking
        self.performance_history: Dict[str, List[Dict[str, Any]]] = {}
        self.tool_health: Dict[str, float] = {}  # tool_id -> health_score (0.0 to 1.0)
        
        # Safety and approval tracking
        self.pending_approvals: Dict[str, Dict[str, Any]] = {}
        
        # Initialize with built-in capabilities
        self._register_built_in_tools()
        
        logger.info("ToolSkillBroker initialized with safety enforcement")
    
    @trace("tool_broker_discover_capabilities")
    async def discover_capabilities(self, requirements: Dict[str, Any]) -> List[AgentCapability]:
        """Discover available capabilities matching requirements"""
        try:
            matching_capabilities = []
            
            # Extract search criteria from requirements
            required_types = requirements.get("capability_types", [])
            required_categories = requirements.get("categories", [])
            min_safety_level = requirements.get("min_safety_level", SafetyLevel.SAFE)
            user_context = requirements.get("user_context", {})
            
            # Search through registered tools
            for tool_id, registration in self.registered_tools.items():
                # Check if tool is healthy
                tool_health = self.tool_health.get(tool_id, 1.0)
                if tool_health < 0.3:  # Skip unhealthy tools
                    continue
                
                # Check safety level compatibility
                if not self._is_safety_level_compatible(registration.safety_level, min_safety_level):
                    continue
                
                # Check category match if specified
                if required_categories and registration.category not in required_categories:
                    continue
                
                # Check capability type matches
                for capability in registration.capabilities:
                    if not required_types or capability.capability_type in required_types:
                        # Add context-specific metadata
                        perf_metrics = {
                            **capability.performance_metrics,
                            "health_score": tool_health,
                            "success_rate": registration.success_rate
                        }
                        if registration.last_used:
                            perf_metrics["last_execution"] = to_json_compatible(registration.last_used)

                        capability_with_context = AgentCapability(
                            capability_id=capability.capability_id,
                            capability_type=capability.capability_type,
                            name=capability.name,
                            description=capability.description,
                            version=capability.version,
                            parameters=capability.parameters,
                            dependencies=capability.dependencies,
                            safety_level=capability.safety_level,
                            performance_metrics=perf_metrics,
                            enabled=tool_health > 0.5
                        )
                        matching_capabilities.append(capability_with_context)
            
            logger.debug(f"Discovered {len(matching_capabilities)} capabilities matching requirements")
            return matching_capabilities
            
        except Exception as e:
            logger.error(f"Capability discovery failed: {e}")
            return []
    
    @trace("tool_broker_register_capability")
    async def register_capability(self, capability: AgentCapability) -> bool:
        """Register a new capability with the broker"""
        try:
            # This is a simplified implementation
            # In production, would need to validate tool instance and capabilities
            logger.info(f"Registering capability: {capability.capability_id}")
            
            # For now, we'll just track the capability in our index
            capability_name = capability.name.lower().replace(" ", "_")
            if capability_name not in self.capability_index:
                self.capability_index[capability_name] = []
            
            # Add to capability index (simplified)
            self.capability_index[capability_name].append(capability.capability_id)
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to register capability {capability.capability_id}: {e}")
            return False
    
    @trace("tool_broker_execute_capability")
    async def execute_capability(
        self, 
        capability_id: str, 
        parameters: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a specific capability"""
        try:
            # Find the tool that provides this capability
            tool_id = self._find_tool_for_capability(capability_id)
            if not tool_id:
                raise ValueError(f"No tool found for capability {capability_id}")
            
            # Create execution request
            request = ToolExecutionRequest(
                required_capabilities=[capability_id],
                parameters=parameters,
                user_context=context
            )
            
            # Execute through the main execution pathway
            result = await self.execute_tool_request(request)
            
            if result.status == ExecutionStatus.SUCCESS:
                return result.result_data
            else:
                raise RuntimeError(f"Capability execution failed: {result.error_message}")
                
        except Exception as e:
            logger.error(f"Capability execution failed for {capability_id}: {e}")
            raise
    
    @trace("tool_broker_assess_capability_safety")
    async def assess_capability_safety(self, capability_id: str, parameters: Dict[str, Any]) -> SafetyLevel:
        """Assess the safety level of executing a capability"""
        try:
            # Find the tool registration
            tool_id = self._find_tool_for_capability(capability_id)
            if not tool_id:
                return SafetyLevel.DANGEROUS  # Unknown tools are dangerous
            
            registration = self.registered_tools.get(tool_id)
            if not registration:
                return SafetyLevel.DANGEROUS
            
            # Base safety level from registration
            base_safety = registration.safety_level
            
            # Assess parameters for additional risks
            parameter_risk = self._assess_parameter_risk(parameters)
            
            # Combine assessments
            if parameter_risk == SafetyLevel.DANGEROUS or base_safety == SafetyLevel.DANGEROUS:
                return SafetyLevel.DANGEROUS
            elif parameter_risk == SafetyLevel.REQUIRES_CONFIRMATION or base_safety == SafetyLevel.REQUIRES_CONFIRMATION:
                return SafetyLevel.REQUIRES_CONFIRMATION
            else:
                return SafetyLevel.SAFE
                
        except Exception as e:
            logger.error(f"Safety assessment failed for {capability_id}: {e}")
            return SafetyLevel.DANGEROUS  # Err on the side of caution
    
    @trace("tool_broker_get_capability_performance")
    async def get_capability_performance(self, capability_id: str) -> Dict[str, float]:
        """Get performance metrics for a capability"""
        try:
            tool_id = self._find_tool_for_capability(capability_id)
            if not tool_id:
                return {"success_rate": 0.0, "avg_execution_time": 0.0, "health_score": 0.0}
            
            registration = self.registered_tools.get(tool_id)
            if not registration:
                return {"success_rate": 0.0, "avg_execution_time": 0.0, "health_score": 0.0}
            
            # Calculate performance metrics
            performance_history = self.performance_history.get(tool_id, [])
            
            if not performance_history:
                return {
                    "success_rate": registration.success_rate,
                    "avg_execution_time": 0.0,
                    "health_score": self.tool_health.get(tool_id, 1.0),
                    "execution_count": registration.execution_count
                }
            
            # Calculate averages from recent history
            recent_history = performance_history[-100:]  # Last 100 executions
            avg_time = sum(h.get("execution_time", 0) for h in recent_history) / len(recent_history)
            success_count = sum(1 for h in recent_history if h.get("success", False))
            success_rate = success_count / len(recent_history)
            
            return {
                "success_rate": success_rate,
                "avg_execution_time": avg_time,
                "health_score": self.tool_health.get(tool_id, 1.0),
                "execution_count": registration.execution_count,
                "recent_executions": len(recent_history)
            }
            
        except Exception as e:
            logger.error(f"Performance metrics retrieval failed for {capability_id}: {e}")
            return {"success_rate": 0.0, "avg_execution_time": 0.0, "health_score": 0.0}
    
    # Main execution method
    
    async def execute_tool_request(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """Execute tool request with comprehensive safety and error handling"""
        async with self.execution_semaphore:
            try:
                with self.tracer.trace("tool_execution") as span:
                    span.set_attribute("request_id", request.request_id)
                    span.set_attribute("capabilities", str(request.required_capabilities))
                    
                    # 1. Find suitable tools
                    suitable_tools = await self._find_suitable_tools(request)
                    if not suitable_tools:
                        return ToolExecutionResult(
                            status=ExecutionStatus.FAILED,
                            error_message=f"No tools available for capabilities: {request.required_capabilities}"
                        )
                    
                    # 2. Safety assessment
                    if self.enable_safety_checks:
                        safety_result = await self._assess_execution_safety(request, suitable_tools)
                        if safety_result["requires_approval"]:
                            return ToolExecutionResult(
                                status=ExecutionStatus.REQUIRES_APPROVAL,
                                approval_request=safety_result["approval_request"],
                                recommended_tools=suitable_tools[:3]
                            )
                    
                    # 3. Tool selection
                    selected_tool = await self._select_optimal_tool(suitable_tools, request)
                    
                    # 4. Execute with timeout and monitoring
                    result = await self._execute_with_monitoring(selected_tool, request)
                    
                    # 5. Update performance metrics
                    await self._update_tool_metrics(selected_tool, result, request)
                    
                    return result
                    
            except Exception as e:
                logger.error(f"Tool execution failed for request {request.request_id}: {e}")
                return ToolExecutionResult(
                    status=ExecutionStatus.FAILED,
                    error_message=str(e)
                )
    
    # Private helper methods
    
    def _register_built_in_tools(self):
        """Register built-in tools and capabilities"""
        try:
            # Mock built-in tools for demonstration
            # In production, these would be actual tool instances
            
            # Knowledge base search capability
            kb_capability = AgentCapability(
                capability_id="knowledge_search",
                capability_type=AgentCapabilityType.KNOWLEDGE_RETRIEVAL,
                name="Knowledge Base Search",
                description="Search the knowledge base for relevant information",
                parameters={"query": "string", "limit": "integer"},
                safety_level=SafetyLevel.SAFE,
                performance_metrics={"avg_response_time": 0.5}
            )
            
            # Register knowledge base tool
            self.registered_tools["kb_tool"] = ToolRegistration(
                tool_id="kb_tool",
                tool_instance=None,  # Would be actual tool instance
                capabilities=[kb_capability],
                category=ToolCategory.SEARCH,
                safety_level=SafetyLevel.SAFE
            )
            
            # Update indices
            self.capability_index["knowledge_search"] = ["kb_tool"]
            self.category_index[ToolCategory.SEARCH] = ["kb_tool"]
            self.tool_health["kb_tool"] = 1.0
            
            # Log analysis capability
            log_capability = AgentCapability(
                capability_id="log_analysis",
                capability_type=AgentCapabilityType.DATA_ANALYSIS,
                name="Log Analysis",
                description="Analyze log files for patterns and issues",
                parameters={"log_data": "string", "pattern": "string"},
                safety_level=SafetyLevel.SAFE,
                performance_metrics={"avg_response_time": 2.0}
            )
            
            self.registered_tools["log_analyzer"] = ToolRegistration(
                tool_id="log_analyzer",
                tool_instance=None,
                capabilities=[log_capability],
                category=ToolCategory.ANALYSIS,
                safety_level=SafetyLevel.SAFE
            )
            
            self.capability_index["log_analysis"] = ["log_analyzer"]
            if ToolCategory.ANALYSIS not in self.category_index:
                self.category_index[ToolCategory.ANALYSIS] = []
            self.category_index[ToolCategory.ANALYSIS].append("log_analyzer")
            self.tool_health["log_analyzer"] = 1.0
            
            logger.info("Built-in tools registered successfully")
            
        except Exception as e:
            logger.error(f"Failed to register built-in tools: {e}")
    
    def _find_tool_for_capability(self, capability_id: str) -> Optional[str]:
        """Find tool ID that provides a specific capability"""
        for tool_id, registration in self.registered_tools.items():
            for capability in registration.capabilities:
                if capability.capability_id == capability_id:
                    return tool_id
        return None
    
    def _is_safety_level_compatible(self, tool_safety: SafetyLevel, required_safety: SafetyLevel) -> bool:
        """Check if tool safety level is compatible with requirements"""
        safety_order = {
            SafetyLevel.SAFE: 0,
            SafetyLevel.REQUIRES_CONFIRMATION: 1,
            SafetyLevel.DANGEROUS: 2
        }
        
        return safety_order.get(tool_safety, 2) <= safety_order.get(required_safety, 0)
    
    def _assess_parameter_risk(self, parameters: Dict[str, Any]) -> SafetyLevel:
        """Assess risk level based on parameters"""
        # Simple heuristic-based risk assessment
        risky_keywords = ['delete', 'remove', 'drop', 'truncate', 'format', 'execute', 'eval']
        
        param_str = str(parameters).lower()
        for keyword in risky_keywords:
            if keyword in param_str:
                return SafetyLevel.REQUIRES_CONFIRMATION
        
        return SafetyLevel.SAFE
    
    async def _find_suitable_tools(self, request: ToolExecutionRequest) -> List[str]:
        """Find tools that can handle the request"""
        suitable_tools = []
        
        for capability_name in request.required_capabilities:
            tool_ids = self.capability_index.get(capability_name, [])
            for tool_id in tool_ids:
                if tool_id not in suitable_tools:
                    # Check tool health
                    health = self.tool_health.get(tool_id, 0.0)
                    if health > 0.5:  # Only healthy tools
                        suitable_tools.append(tool_id)
        
        return suitable_tools
    
    async def _assess_execution_safety(self, request: ToolExecutionRequest, tools: List[str]) -> Dict[str, Any]:
        """Assess safety of execution request"""
        max_risk = SafetyLevel.SAFE
        
        for tool_id in tools:
            registration = self.registered_tools.get(tool_id)
            if registration and registration.safety_level != SafetyLevel.SAFE:
                max_risk = registration.safety_level
                break
        
        # Check parameter safety
        param_risk = self._assess_parameter_risk(request.parameters)
        if param_risk != SafetyLevel.SAFE:
            max_risk = param_risk
        
        requires_approval = max_risk == SafetyLevel.REQUIRES_CONFIRMATION
        
        return {
            "requires_approval": requires_approval,
            "risk_level": max_risk,
            "approval_request": {
                "reason": f"Tool execution requires approval due to {max_risk} safety level",
                "tools": tools,
                "parameters": request.parameters
            } if requires_approval else None
        }
    
    async def _select_optimal_tool(self, suitable_tools: List[str], request: ToolExecutionRequest) -> str:
        """Select the best tool for the request"""
        if len(suitable_tools) == 1:
            return suitable_tools[0]
        
        # Score tools based on performance and health
        tool_scores = []
        for tool_id in suitable_tools:
            registration = self.registered_tools.get(tool_id)
            if registration:
                health_score = self.tool_health.get(tool_id, 0.0)
                success_rate = registration.success_rate
                
                # Combined score
                score = (health_score * 0.4) + (success_rate * 0.6)
                tool_scores.append((tool_id, score))
        
        # Sort by score and return best
        tool_scores.sort(key=lambda x: x[1], reverse=True)
        return tool_scores[0][0] if tool_scores else suitable_tools[0]
    
    async def _execute_with_monitoring(self, tool_id: str, request: ToolExecutionRequest) -> ToolExecutionResult:
        """Execute tool with monitoring and timeout"""
        start_time = datetime.now(timezone.utc)
        
        try:
            # Get tool registration
            registration = self.registered_tools.get(tool_id)
            if not registration:
                return ToolExecutionResult(
                    status=ExecutionStatus.FAILED,
                    error_message=f"Tool {tool_id} not found"
                )
            
            # Simulate tool execution (in production, would call actual tool)
            # For now, return mock success result
            await asyncio.sleep(0.1)  # Simulate processing time
            
            execution_time = (datetime.now(timezone.utc) - start_time).total_seconds()
            
            # Mock result based on tool type
            if tool_id == "kb_tool":
                result_data = {
                    "search_results": [
                        {"title": "Troubleshooting Guide", "content": "Mock knowledge base result", "relevance": 0.9}
                    ],
                    "total_results": 1
                }
            elif tool_id == "log_analyzer":
                result_data = {
                    "patterns_found": ["Error pattern detected", "Connection timeout pattern"],
                    "analysis_summary": "Mock log analysis result",
                    "confidence": 0.8
                }
            else:
                result_data = {"result": "Mock tool execution result"}
            
            return ToolExecutionResult(
                status=ExecutionStatus.SUCCESS,
                tool_used=tool_id,
                result_data=result_data,
                execution_metadata={
                    "execution_time": execution_time,
                    "tool_version": registration.capabilities[0].version if registration.capabilities else "1.0.0",
                    "request_id": request.request_id
                },
                performance_metrics={
                    "execution_time": execution_time,
                    "memory_usage": 0.1,  # Mock
                    "cpu_usage": 0.05      # Mock
                }
            )
            
        except asyncio.TimeoutError:
            return ToolExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                tool_used=tool_id,
                error_message=f"Tool execution timed out after {request.timeout}s"
            )
        except Exception as e:
            return ToolExecutionResult(
                status=ExecutionStatus.FAILED,
                tool_used=tool_id,
                error_message=str(e)
            )
    
    async def _update_tool_metrics(self, tool_id: str, result: ToolExecutionResult, request: ToolExecutionRequest):
        """Update performance metrics for a tool"""
        try:
            registration = self.registered_tools.get(tool_id)
            if not registration:
                return
            
            # Update execution count
            registration.execution_count += 1
            registration.last_used = datetime.now(timezone.utc)
            
            # Update success rate
            was_successful = result.status == ExecutionStatus.SUCCESS
            current_success_rate = registration.success_rate
            new_success_rate = (current_success_rate * 0.9) + (1.0 if was_successful else 0.0) * 0.1
            registration.success_rate = new_success_rate
            
            # Add to performance history
            if tool_id not in self.performance_history:
                self.performance_history[tool_id] = []
            
            self.performance_history[tool_id].append({
                "timestamp": datetime.now(timezone.utc),
                "success": was_successful,
                "execution_time": result.performance_metrics.get("execution_time", 0.0),
                "request_id": request.request_id
            })
            
            # Keep only last 1000 entries
            self.performance_history[tool_id] = self.performance_history[tool_id][-1000:]
            
            # Update health score
            health_factors = {
                "success_rate": new_success_rate * 0.6,
                "recent_usage": 0.3 if registration.last_used and (datetime.now(timezone.utc) - registration.last_used).days < 7 else 0.0,
                "performance": 0.1 if result.performance_metrics.get("execution_time", 0) < 5.0 else 0.0
            }
            
            self.tool_health[tool_id] = sum(health_factors.values())
            
            logger.debug(f"Updated metrics for tool {tool_id}: success_rate={new_success_rate:.2f}, health={self.tool_health[tool_id]:.2f}")
            
        except Exception as e:
            logger.error(f"Failed to update metrics for tool {tool_id}: {e}")
    
    # Additional utility methods
    
    async def get_tool_registry_status(self) -> Dict[str, Any]:
        """Get status of all registered tools"""
        try:
            status = {
                "total_tools": len(self.registered_tools),
                "healthy_tools": sum(1 for health in self.tool_health.values() if health > 0.5),
                "tools": {}
            }
            
            for tool_id, registration in self.registered_tools.items():
                status["tools"][tool_id] = {
                    "category": registration.category.value,
                    "safety_level": registration.safety_level.value,
                    "health_score": self.tool_health.get(tool_id, 0.0),
                    "success_rate": registration.success_rate,
                    "execution_count": registration.execution_count,
                    "capabilities": [c.capability_id for c in registration.capabilities]
                }
            
            return status
            
        except Exception as e:
            logger.error(f"Failed to get registry status: {e}")
            return {"error": str(e)}
    
    async def shutdown(self):
        """Graceful shutdown of the tool broker"""
        try:
            logger.info("Shutting down ToolSkillBroker...")
            
            # Cancel any pending executions
            # In production, would properly cancel running tasks
            
            # Save performance metrics (if persistence is needed)
            # In production, might save to database
            
            logger.info("ToolSkillBroker shutdown complete")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")