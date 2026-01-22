# Enhanced Service Layer Patterns and Best Practices

**Document Type**: Architecture Deep-dive  
**Last Updated**: August 2025

## Overview

This document describes the enhanced patterns and best practices used in the FaultMaven service layer. Following these patterns ensures consistency, maintainability, and scalability across the codebase. The service layer now features advanced intelligent communication capabilities including memory management, strategic planning, and dynamic prompting.

## Enhanced Service Layer Principles

### 1. Single Responsibility Principle with Intelligence
Each service should have one clear purpose enhanced with intelligent capabilities:
- **EnhancedAgentService**: Agent operations and troubleshooting workflows with memory and planning
- **EnhancedDataService**: Data processing and transformation with memory integration
- **EnhancedKnowledgeService**: Knowledge base operations with memory correlation
- **EnhancedSessionService**: Session lifecycle management with memory preservation

### 2. Enhanced Dependency Injection
Services receive their dependencies through constructor injection, now including intelligence services:

```python
class EnhancedAgentService:
    def __init__(
        self,
        core_agent: FaultMavenAgent,
        data_sanitizer: DataSanitizer,
        memory_service: IMemoryService,      # New memory dependency
        planning_service: IPlanningService,  # New planning dependency
        prompt_engine: IPromptEngine,        # New prompt dependency
        logger: Optional[logging.Logger] = None,
    ):
        self.core_agent = core_agent
        self.data_sanitizer = data_sanitizer
        self.memory_service = memory_service        # Store memory service
        self.planning_service = planning_service    # Store planning service
        self.prompt_engine = prompt_engine          # Store prompt engine
        self.logger = logger or logging.getLogger(__name__)
```

### 3. Async-First Design with Intelligence
All service methods that perform I/O should be async and integrate with intelligence services:

```python
async def process_query(
    self,
    query: str,
    session_id: str,
    context: Optional[Dict[str, Any]] = None,
) -> AgentResponse:
    # Get memory context
    memory_context = await self.memory_service.retrieve_context(session_id, query)
    
    # Plan response strategy
    strategy = await self.planning_service.plan_response_strategy(query, memory_context)
    
    # Generate optimized prompt
    prompt = await self.prompt_engine.assemble_prompt(
        question=query,
        response_type=ResponseType.ANSWER,
        context=memory_context
    )
    
    # Process with intelligence
    result = await self._process_with_intelligence(query, prompt, strategy, memory_context)
    
    # Consolidate insights
    await self.memory_service.consolidate_insights(session_id, result)
    
    return result
```

## Enhanced Common Service Patterns

### 1. Memory-Enhanced Input Validation Pattern

Services validate input at the boundary with memory context:

```python
async def process_query(self, query: str, session_id: str) -> Response:
    # Validate input
    if not query or not query.strip():
        raise ValueError("Query cannot be empty")
    
    # Get memory context for enhanced validation
    memory_context = await self.memory_service.retrieve_context(session_id, query)
    
    # Enhanced validation with memory
    validation_result = await self._validate_with_memory(query, memory_context)
    if not validation_result.is_valid:
        raise ValueError(f"Enhanced validation failed: {validation_result.reason}")
    
    # Sanitize input
    sanitized_query = self.data_sanitizer.sanitize(query)
    
    # Process with enhanced context
    result = await self._process_with_memory_context(sanitized_query, memory_context)
    
    return result
```

### 2. Planning-Enhanced Error Handling Pattern

Services use structured error handling with strategic planning:

```python
async def operation(self, params: Dict, session_id: str) -> Result:
    try:
        # Get planning context
        planning_context = await self.planning_service.get_current_context(session_id)
        
        # Execute with strategic guidance
        result = await self._execute_with_planning(params, planning_context)
        
        return result
        
    except ValidationError as e:
        # Plan recovery strategy
        recovery_plan = await self.planning_service.plan_error_recovery(
            error_type="validation",
            error_details=str(e),
            context=planning_context
        )
        
        self.logger.warning(f"Validation failed: {e}, recovery plan: {recovery_plan}")
        raise ValueError(f"Invalid input: {str(e)}") from e
        
    except ExternalServiceError as e:
        # Plan fallback strategy
        fallback_plan = await self.planning_service.plan_fallback_strategy(
            error_type="external_service",
            error_details=str(e),
            context=planning_context
        )
        
        self.logger.error(f"External service failed: {e}, fallback plan: {fallback_plan}")
        raise RuntimeError(f"Service unavailable: {str(e)}") from e
        
    except Exception as e:
        # Plan general error recovery
        recovery_plan = await self.planning_service.plan_error_recovery(
            error_type="general",
            error_details=str(e),
            context=planning_context
        )
        
        self.logger.error(f"Unexpected error: {e}, recovery plan: {recovery_plan}", exc_info=True)
        raise RuntimeError("Operation failed") from e
```

### 3. Context-Aware Tracing Pattern

Use decorators for operation tracing with memory and planning context:

```python
@trace("enhanced_service_operation")
async def traced_operation(self, params: Dict, session_id: str) -> Result:
    # Get comprehensive context
    memory_context = await self.memory_service.retrieve_context(session_id, "operation")
    planning_context = await self.planning_service.get_current_context(session_id)
    
    # Log with enhanced context
    self.logger.info(f"Starting operation with params: {params}")
    self.logger.info(f"Memory context: {memory_context.summary}")
    self.logger.info(f"Planning context: {planning_context.current_phase}")
    
    # Execute with context
    result = await self._execute_with_context(params, memory_context, planning_context)
    
    # Log completion with insights
    self.logger.info(f"Operation completed successfully, insights: {result.insights}")
    
    return result
```

### 4. Memory-Aware Result Formatting Pattern

Services format results consistently with memory integration:

```python
async def format_response(self, result: Any, session_id: str) -> FormattedResponse:
    # Get memory context for enhanced formatting
    memory_context = await self.memory_service.retrieve_context(session_id, "response_formatting")
    
    # Format with memory awareness
    formatted_result = await self._format_with_memory(result, memory_context)
    
    # Update memory with formatting insights
    await self.memory_service.consolidate_insights(session_id, {
        "operation": "response_formatting",
        "result": formatted_result,
        "memory_context_used": memory_context.summary
    })
    
    return formatted_result
```

## Enhanced Service Implementation Patterns

### 1. Memory-Enhanced Service Base Class

Create a base class for services with memory integration:

```python
class EnhancedBaseService:
    """Base class for services with memory and planning integration"""
    
    def __init__(
        self,
        memory_service: IMemoryService,
        planning_service: IPlanningService,
        logger: Optional[logging.Logger] = None
    ):
        self._memory = memory_service
        self._planning = planning_service
        self.logger = logger or logging.getLogger(self.__class__.__name__)
    
    async def get_enhanced_context(self, session_id: str, operation: str) -> EnhancedContext:
        """Get comprehensive context for operations"""
        memory_context = await self._memory.retrieve_context(session_id, operation)
        planning_context = await self._planning.get_current_context(session_id)
        
        return EnhancedContext(
            memory=memory_context,
            planning=planning_context,
            session_id=session_id,
            operation=operation
        )
    
    async def consolidate_operation_insights(self, session_id: str, operation: str, result: Any) -> bool:
        """Consolidate insights from operation execution"""
        return await self._memory.consolidate_insights(session_id, {
            "operation": operation,
            "result": result,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    async def plan_operation_strategy(self, operation: str, context: Dict) -> StrategicPlan:
        """Plan strategy for operation execution"""
        return await self._planning.plan_operation_strategy(operation, context)
```

### 2. Enhanced Agent Service Implementation

Implement the enhanced agent service with intelligence:

```python
class EnhancedAgentService(EnhancedBaseService):
    """Enhanced agent service with memory, planning, and prompting"""
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer,
        memory_service: IMemoryService,
        planning_service: IPlanningService,
        prompt_engine: IPromptEngine
    ):
        super().__init__(memory_service, planning_service)
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        self._prompt_engine = prompt_engine
    
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        """Process query with enhanced intelligence"""
        # Get enhanced context
        context = await self.get_enhanced_context(request.session_id, "query_processing")
        
        # Plan response strategy
        strategy = await self.plan_operation_strategy("query_processing", {
            "query": request.query,
            "session_id": request.session_id,
            "memory_context": context.memory
        })
        
        # Generate optimized prompt
        prompt = await self._prompt_engine.assemble_prompt(
            question=request.query,
            response_type=ResponseType.ANSWER,
            context=context.memory
        )
        
        # Execute with intelligence
        result = await self._execute_with_intelligence(request, prompt, strategy, context)
        
        # Consolidate insights
        await self.consolidate_operation_insights(
            request.session_id, 
            "query_processing", 
            result
        )
        
        return result
    
    async def _execute_with_intelligence(
        self, 
        request: QueryRequest, 
        prompt: str, 
        strategy: StrategicPlan, 
        context: EnhancedContext
    ) -> AgentResponse:
        """Execute query with strategic intelligence"""
        with self._tracer.trace("intelligent_query_execution"):
            # Execute according to strategy
            if strategy.current_phase == "analysis":
                result = await self._execute_analysis_phase(request, prompt, context)
            elif strategy.current_phase == "solution":
                result = await self._execute_solution_phase(request, prompt, context)
            else:
                result = await self._execute_default_phase(request, prompt, context)
            
            # Update planning context with results
            await self._planning.update_execution_results(
                context.session_id, 
                strategy.id, 
                result
            )
            
            return result
```

### 3. Enhanced Data Service Implementation

Implement the enhanced data service with memory integration:

```python
class EnhancedDataService(EnhancedBaseService):
    """Enhanced data service with memory integration"""
    
    def __init__(
        self,
        data_classifier: IDataClassifier,
        log_processor: ILogProcessor,
        sanitizer: ISanitizer,
        memory_service: IMemoryService,
        planning_service: IPlanningService
    ):
        super().__init__(memory_service, planning_service)
        self._classifier = data_classifier
        self._processor = log_processor
        self._sanitizer = sanitizer
    
    async def process_data(self, data: bytes, filename: str, session_id: str) -> ProcessedData:
        """Process data with memory integration"""
        # Get enhanced context
        context = await self.get_enhanced_context(session_id, "data_processing")
        
        # Plan processing strategy
        strategy = await self.plan_operation_strategy("data_processing", {
            "filename": filename,
            "data_size": len(data),
            "memory_context": context.memory
        })
        
        # Execute with strategic guidance
        result = await self._execute_processing_strategy(data, filename, strategy, context)
        
        # Consolidate insights
        await self.consolidate_operation_insights(
            session_id, 
            "data_processing", 
            result
        )
        
        return result
    
    async def _execute_processing_strategy(
        self, 
        data: bytes, 
        filename: str, 
        strategy: StrategicPlan, 
        context: EnhancedContext
    ) -> ProcessedData:
        """Execute data processing according to strategy"""
        # Classify data with memory context
        data_type = await self._classifier.classify(data, filename, {
            "session_id": context.session_id,
            "memory_context": context.memory
        })
        
        # Process according to type with memory correlation
        if data_type == DataType.LOG_FILE:
            result = await self._process_log_file(data, filename, context)
        elif data_type == DataType.CONFIG_FILE:
            result = await self._process_config_file(data, filename, context)
        else:
            result = await self._process_generic_file(data, filename, context)
        
        # Enhance result with memory insights
        enhanced_result = await self._enhance_with_memory_insights(result, context)
        
        return enhanced_result
```

## Enhanced Testing Patterns

### 1. Memory and Planning Service Mocking

Create comprehensive mocks for testing:

```python
class MockMemoryService(IMemoryService):
    """Comprehensive mock memory service for testing"""
    
    def __init__(self):
        self._contexts = {}
        self._insights = []
        self._user_profiles = {}
    
    async def retrieve_context(self, session_id: str, query: str) -> ConversationContext:
        # Return mock context or create new one
        if session_id not in self._contexts:
            self._contexts[session_id] = ConversationContext(
                working_memory=[],
                semantic_context=[],
                user_profile=MockUserProfile()
            )
        return self._contexts[session_id]
    
    async def consolidate_insights(self, session_id: str, result: dict) -> bool:
        self._insights.append({
            'session_id': session_id,
            'result': result,
            'timestamp': datetime.utcnow().isoformat()
        })
        return True
    
    def get_consolidated_insights(self) -> List[Dict]:
        return self._insights.copy()
    
    def set_mock_context(self, session_id: str, context: ConversationContext):
        """Set mock context for testing"""
        self._contexts[session_id] = context

class MockPlanningService(IPlanningService):
    """Comprehensive mock planning service for testing"""
    
    def __init__(self):
        self._strategies = {}
        self._current_contexts = {}
    
    async def plan_response_strategy(self, query: str, context: dict) -> StrategicPlan:
        # Return mock strategy
        strategy = StrategicPlan(
            id=str(uuid.uuid4()),
            current_phase="analysis",
            plan_components=[
                PlanComponent(phase="analysis", actions=["analyze_problem"]),
                PlanComponent(phase="solution", actions=["generate_solution"])
            ]
        )
        return strategy
    
    async def get_current_context(self, session_id: str) -> PlanningContext:
        # Return mock planning context
        if session_id not in self._current_contexts:
            self._current_contexts[session_id] = PlanningContext(
                current_phase="analysis",
                active_strategy=None,
                completed_phases=[]
            )
        return self._current_contexts[session_id]
    
    def set_mock_context(self, session_id: str, context: PlanningContext):
        """Set mock planning context for testing"""
        self._current_contexts[session_id] = context
```

### 2. Enhanced Service Testing

Test services with intelligence integration:

```python
class TestEnhancedAgentService:
    @pytest.fixture
    def mock_services(self):
        return {
            'memory': MockMemoryService(),
            'planning': MockPlanningService(),
            'prompt_engine': Mock(spec=IPromptEngine),
            'llm': Mock(spec=ILLMProvider),
            'tools': [Mock(spec=BaseTool)],
            'tracer': Mock(spec=ITracer),
            'sanitizer': Mock(spec=ISanitizer)
        }
    
    @pytest.fiide
    def enhanced_agent_service(self, mock_services):
        return EnhancedAgentService(
            llm_provider=mock_services['llm'],
            tools=mock_services['tools'],
            tracer=mock_services['tracer'],
            sanitizer=mock_services['sanitizer'],
            memory_service=mock_services['memory'],
            planning_service=mock_services['planning'],
            prompt_engine=mock_services['prompt_engine']
        )
    
    async def test_process_query_with_intelligence(self, enhanced_agent_service, mock_services):
        # Setup mocks
        mock_services['prompt_engine'].assemble_prompt.return_value = "Enhanced prompt"
        mock_services['llm'].generate_with_context.return_value = "Intelligent response"
        
        # Set mock context
        mock_context = ConversationContext(
            working_memory=["previous_query"],
            semantic_context=[{"content": "relevant_info"}],
            user_profile=MockUserProfile()
        )
        mock_services['memory'].set_mock_context("test_session", mock_context)
        
        # Execute
        request = QueryRequest(session_id="test_session", query="test query")
        result = await enhanced_agent_service.process_query(request)
        
        # Verify intelligence features were used
        mock_services['memory'].retrieve_context.assert_called_once()
        mock_services['planning'].plan_operation_strategy.assert_called_once()
        mock_services['prompt_engine'].assemble_prompt.assert_called_once()
        mock_services['llm'].generate_with_context.assert_called_once()
        mock_services['memory'].consolidate_insights.assert_called_once()
        
        assert result is not None
```

## Enhanced Performance Patterns

### 1. Memory-Aware Caching

Implement caching with memory context:

```python
class MemoryAwareCache:
    """Cache that considers memory context for optimization"""
    
    def __init__(self, memory_service: IMemoryService):
        self._memory = memory_service
        self._cache = {}
        self._access_patterns = {}
    
    async def get(self, key: str, session_id: str) -> Optional[Any]:
        # Check cache first
        if key in self._cache:
            # Update access patterns
            await self._update_access_patterns(session_id, key)
            return self._cache[key]
        
        # Get memory context for optimization
        memory_context = await self._memory.retrieve_context(session_id, "cache_access")
        
        # Use memory patterns for cache optimization
        if self._should_pre_warm_cache(key, memory_context):
            await self._pre_warm_cache(key, memory_context)
        
        return None
    
    async def _update_access_patterns(self, session_id: str, key: str):
        """Update access patterns for optimization"""
        if session_id not in self._access_patterns:
            self._access_patterns[session_id] = {}
        
        if key not in self._access_patterns[session_id]:
            self._access_patterns[session_id][key] = 0
        
        self._access_patterns[session_id][key] += 1
    
    def _should_pre_warm_cache(self, key: str, memory_context: ConversationContext) -> bool:
        """Determine if cache should be pre-warmed based on memory context"""
        # Check if key is frequently accessed
        if hasattr(memory_context, 'frequently_accessed'):
            return key in memory_context.frequently_accessed
        
        return False
```

### 2. Planning-Driven Resource Allocation

Optimize resource usage based on planning context:

```python
class PlanningAwareResourceManager:
    """Resource manager that considers planning context"""
    
    def __init__(self, planning_service: IPlanningService):
        self._planning = planning_service
    
    async def allocate_resources(self, operation: str, session_id: str) -> ResourceAllocation:
        # Get planning context
        planning_context = await self._planning.get_current_context(session_id)
        
        # Allocate based on planning phase
        if planning_context.current_phase == "analysis":
            # Allocate more resources for analysis
            return ResourceAllocation(cpu=2, memory=4, priority='high')
        elif planning_context.current_phase == "solution":
            # Allocate resources for solution execution
            return ResourceAllocation(cpu=1, memory=2, priority='medium')
        else:
            # Default allocation
            return ResourceAllocation(cpu=1, memory=1, priority='normal')
```

## Enhanced Error Recovery Patterns

### 1. Memory-Enhanced Error Recovery

Use memory context for intelligent error recovery:

```python
class MemoryEnhancedErrorRecovery:
    """Error recovery that considers memory context"""
    
    def __init__(self, memory_service: IMemoryService):
        self._memory = memory_service
    
    async def attempt_recovery(self, error: Exception, session_id: str, operation: str) -> RecoveryResult:
        # Get memory context for recovery
        memory_context = await self._memory.retrieve_context(session_id, "error_recovery")
        
        # Check if similar errors occurred before
        similar_errors = await self._find_similar_errors(error, memory_context)
        
        if similar_errors:
            # Use successful recovery strategies from memory
            recovery_strategy = await self._select_recovery_strategy(similar_errors)
            return await self._execute_recovery_strategy(recovery_strategy, error)
        else:
            # Try standard recovery strategies
            return await self._try_standard_recovery(error)
    
    async def _find_similar_errors(self, error: Exception, memory_context: ConversationContext) -> List[Dict]:
        """Find similar errors from memory context"""
        # Search memory for similar error patterns
        error_patterns = memory_context.get('error_patterns', [])
        
        similar_errors = []
        for pattern in error_patterns:
            if self._errors_are_similar(error, pattern['error']):
                similar_errors.append(pattern)
        
        return similar_errors
```

### 2. Planning-Enhanced Error Recovery

Use planning context for strategic error recovery:

```python
class PlanningEnhancedErrorRecovery:
    """Error recovery that considers planning context"""
    
    def __init__(self, planning_service: IPlanningService):
        self._planning = planning_service
    
    async def plan_error_recovery(self, error: Exception, session_id: str) -> RecoveryPlan:
        # Get planning context
        planning_context = await self._planning.get_current_context(session_id)
        
        # Plan recovery based on current phase
        if planning_context.current_phase == "analysis":
            # Analysis phase errors - focus on data validation
            return RecoveryPlan(
                strategy="data_validation",
                steps=["validate_input", "check_data_format", "retry_analysis"]
            )
        elif planning_context.current_phase == "solution":
            # Solution phase errors - focus on alternative approaches
            return RecoveryPlan(
                strategy="alternative_solution",
                steps=["identify_alternatives", "validate_alternatives", "execute_alternative"]
            )
        else:
            # Default recovery strategy
            return RecoveryPlan(
                strategy="standard_recovery",
                steps=["retry_operation", "fallback_method", "escalate_error"]
            )
```

## Conclusion

The enhanced service layer patterns provide FaultMaven with sophisticated intelligent communication capabilities while maintaining the core principles of consistency, maintainability, and scalability. The new memory management, strategic planning, and advanced prompting integration creates services that learn, adapt, and provide increasingly effective assistance to users over time.

**Key Benefits of Enhanced Service Patterns**:
- **Intelligent Context Awareness**: All operations consider memory and planning context
- **Strategic Execution**: Planning-driven approach to service operations
- **Continuous Learning**: Services learn from interactions and improve over time
- **Enhanced Error Recovery**: Context-aware error handling and recovery
- **Performance Optimization**: Memory-aware caching and planning-driven resource allocation
- **Comprehensive Testing**: Mock implementations for all intelligence services

**Next Steps**:
1. **Implement Enhanced Services**: Create the actual enhanced service implementations
2. **Update Existing Services**: Apply enhanced patterns to current services
3. **Test and Validate**: Ensure performance and reliability of intelligent features
4. **Gradual Rollout**: Enable features incrementally with feature flags

These enhanced patterns position FaultMaven services for intelligent operation while maintaining all the benefits of the existing clean architecture.