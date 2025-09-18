# Enhanced Dependency Injection System Architecture

**Document Type**: Architecture Deep-dive  
**Last Updated**: August 2025

## Overview

FaultMaven implements a comprehensive dependency injection (DI) system that manages the lifecycle and dependencies of all system components. The DI container provides centralized dependency resolution, interface-based abstraction, and runtime configuration flexibility through feature flags. The system now features advanced intelligent communication capabilities including memory management, strategic planning, and dynamic prompting.

## Enhanced Container Architecture

### Singleton Container Design with Intelligence

The `DIContainer` class follows the singleton pattern to ensure consistent dependency management across the application, now enhanced with intelligent services:

```python
class DIContainer:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._create_intelligence_layer()  # New intelligence services
            self._create_infrastructure_layer()
            self._create_tools_layer()
            self._create_service_layer()
            self._initialized = True
```

**Key Enhanced Features**:
- **Single Source of Truth**: One container instance manages all dependencies
- **Lazy Initialization**: Components created only when first requested
- **Thread Safety**: Singleton implementation handles concurrent access
- **Reset Capability**: Full container reset for testing scenarios
- **Intelligence Integration**: Memory, planning, and prompting services

### Enhanced Layered Dependency Structure

The container now organizes dependencies into logical layers with intelligence integration:

#### 1. Intelligence Layer (New)
**Purpose**: Core intelligent communication capabilities

```python
def _create_intelligence_layer(self):
    # Memory Management System
    self.memory_service: IMemoryService = MemoryService(
        vector_store=self._get_vector_store(),
        redis_store=self._get_redis_store()
    )
    
    # Strategic Planning System
    self.planning_service: IPlanningService = PlanningService(
        llm_provider=self._get_llm_provider()
    )
    
    # Advanced Prompt Engine
    self.prompt_engine: IPromptEngine = AdvancedPromptEngine(
        llm_provider=self._get_llm_provider(),
        memory_service=self.memory_service
    )
```

**Components**:
- **Memory Service**: Hierarchical memory with semantic understanding
- **Planning Service**: Strategic planning and problem decomposition
- **Prompt Engine**: Advanced prompting with optimization and versioning

#### 2. Infrastructure Layer
**Purpose**: External service integrations and technical concerns

```python
def _create_infrastructure_layer(self):
    # LLM Provider with multi-provider support and memory integration
    self.llm_provider: ILLMProvider = LLMRouter()
    
    # Data sanitization for PII protection
    self.sanitizer: ISanitizer = DataSanitizer()
    
    # Distributed tracing
    self.tracer: ITracer = OpikTracer()
    
    # Core processing interfaces with context awareness
    self.data_classifier = EnhancedDataClassifier(
        memory_service=self.memory_service
    )
    self.log_processor = EnhancedLogProcessor(
        memory_service=self.memory_service
    )
```

**Enhanced Components**:
- **LLM Provider**: Multi-provider routing with failover and memory enhancement
- **Sanitizer**: PII redaction with Presidio integration
- **Tracer**: Observability with Opik integration
- **Classifier**: Data type detection with context awareness
- **Log Processor**: Log analysis with pattern learning

#### 3. Tools Layer
**Purpose**: Agent capabilities with standardized interfaces and planning integration

```python
def _create_tools_layer(self):
    # Knowledge base tool with ingester dependency and memory integration
    knowledge_base_tool = EnhancedKnowledgeBaseTool(
        knowledge_ingester=KnowledgeIngester(
            memory_service=self.memory_service
        ),
        planning_service=self.planning_service
    )
    
    # Web search capability with planning integration
    web_search_tool = EnhancedWebSearchTool(
        planning_service=self.planning_service
    )
    
    # Create tools list with error handling
    self.tools: List[BaseTool] = [
        tool for tool in [knowledge_base_tool, web_search_tool] 
        if tool is not None
    ]
```

**Enhanced Features**:
- **Interface Compliance**: All tools implement `BaseTool` interface with context support
- **Error Resilience**: Failed tool initialization doesn't break container
- **Dynamic Loading**: Tools can be enabled/disabled based on environment
- **Standardized Execution**: Consistent `execute()` and `get_schema()` methods
- **Planning Integration**: Tools execute with strategic guidance
- **Memory Integration**: Tools access and update memory context

#### 4. Enhanced Service Layer
**Purpose**: Business logic orchestration with interface dependencies and intelligence

```python
def _create_service_layer(self):
    # Agent Service - Core troubleshooting orchestration with pure agentic framework
    self.agent_service = AgentService(
        llm_provider=self.llm_provider,    # Interface injection
        tools=self.tools,                  # Interface list injection
        tracer=self.tracer,               # Interface injection
        sanitizer=self.sanitizer          # Interface injection
    )
    
    # Data Service - Enhanced with memory integration
    self.data_service = EnhancedDataService(
        data_classifier=self.data_classifier,
        log_processor=self.log_processor,
        sanitizer=self.sanitizer,
        memory_service=self.memory_service  # New memory dependency
    )
    
    # Knowledge Service - Enhanced with memory integration
    self.knowledge_service = EnhancedKnowledgeService(
        vector_store=self._get_vector_store(),
        memory_service=self.memory_service  # New memory dependency
    )
    
    # Session Service - Enhanced with memory preservation
    self.session_service = EnhancedSessionService(
        session_store=self._get_session_store(),
        memory_service=self.memory_service  # New memory dependency
    )
```

**Enhanced Services**:
- **Agent Service**: AI reasoning workflow orchestration with memory and planning
- **Data Service**: File upload and processing with memory integration
- **Knowledge Service**: Document ingestion and retrieval with memory correlation
- **Session Service**: Multi-turn conversation state management with memory preservation

## Enhanced Dependency Resolution

### Intelligence-Aware Resolution

The container now resolves dependencies with intelligence context:

```python
def get_agent_service(self) -> AgentService:
    """Get agent service with pure agentic framework implementation"""
    if not hasattr(self, 'agent_service'):
        # Ensure intelligence layer is initialized first
        if not hasattr(self, 'memory_service'):
            self._create_intelligence_layer()
        
        # Create agent service with pure agentic framework
        self.agent_service = AgentService(
            llm_provider=self.llm_provider,
            tools=self.tools,
            tracer=self.tracer,
            sanitizer=self.sanitizer
        )
    
    return self.agent_service
```

### Context-Aware Dependency Creation

Dependencies are created with awareness of their context and relationships:

```python
def _get_vector_store(self) -> IVectorStore:
    """Get vector store with memory integration"""
    if not hasattr(self, '_vector_store'):
        # Create base vector store
        base_store = ChromaDBVectorStore()
        
        # Wrap with memory enhancement
        self._vector_store = MemoryEnhancedVectorStore(
            base_store=base_store,
            memory_service=self.memory_service
        )
    
    return self._vector_store

def _get_redis_store(self) -> ISessionStore:
    """Get Redis store with memory preservation"""
    if not hasattr(self, '_redis_store'):
        # Create base Redis store
        base_store = RedisSessionStore()
        
        # Wrap with memory preservation
        self._redis_store = MemoryPreservingSessionStore(
            base_store=base_store,
            memory_service=self.memory_service
        )
    
    return self._redis_store
```

## Enhanced Service Dependencies

### Memory-Enhanced Services

Services now receive memory dependencies for context-aware operation:

```python
# Pure Agent Service with Agentic Framework
class AgentService:
    def __init__(
        self,
        llm_provider: ILLMProvider,
        tools: List[BaseTool],
        tracer: ITracer,
        sanitizer: ISanitizer
    ):
        self._llm = llm_provider
        self._tools = tools
        self._tracer = tracer
        self._sanitizer = sanitizer
        # Agentic framework components are internally managed
    
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        # Get memory context
        context = await self._memory.retrieve_context(
            request.session_id, 
            request.query
        )
        
        # Plan response strategy
        strategy = await self._planning.plan_response_strategy(
            request.query, 
            context
        )
        
        # Generate optimized prompt
        prompt = await self._prompt_engine.assemble_prompt(
            question=request.query,
            response_type=ResponseType.ANSWER,
            context=context
        )
        
        # Execute with intelligence
        result = await self._llm.generate_with_context(prompt, context)
        
        # Consolidate insights
        await self._memory.consolidate_insights(
            request.session_id, 
            result
        )
        
        return result
```

### Planning-Enhanced Services

Services receive planning dependencies for strategic execution:

```python
# Enhanced Data Service with Planning
class EnhancedDataService:
    def __init__(
        self,
        data_classifier: IDataClassifier,
        log_processor: ILogProcessor,
        sanitizer: ISanitizer,
        memory_service: IMemoryService,
        planning_service: IPlanningService  # New planning dependency
    ):
        self._classifier = data_classifier
        self._processor = log_processor
        self._sanitizer = sanitizer
        self._memory = memory_service
        self._planning = planning_service    # Store planning service
    
    async def process_data(self, data: bytes, filename: str, session_id: str) -> ProcessedData:
        # Get processing context
        context = await self._memory.retrieve_context(session_id, "data_processing")
        
        # Plan processing strategy
        strategy = await self._planning.plan_processing_strategy(
            filename, 
            context
        )
        
        # Execute with strategic guidance
        result = await self._execute_with_strategy(data, filename, strategy)
        
        # Learn from processing
        await self._memory.consolidate_insights(
            session_id, 
            {"processing_result": result, "strategy_used": strategy}
        )
        
        return result
```

## Enhanced Health Monitoring

### Intelligence Service Health

The container now monitors the health of intelligence services:

```python
def get_health_status(self) -> Dict[str, Any]:
    """Get comprehensive health status including intelligence services"""
    health = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "components": {}
    }
    
    # Check infrastructure components
    health["components"]["infrastructure"] = self._check_infrastructure_health()
    
    # Check intelligence components (new)
    health["components"]["intelligence"] = self._check_intelligence_health()
    
    # Check service components
    health["components"]["services"] = self._check_service_health()
    
    # Determine overall status
    if any(comp.get("status") == "degraded" for comp in health["components"].values()):
        health["status"] = "degraded"
    elif any(comp.get("status") == "unhealthy" for comp in health["components"].values()):
        health["status"] = "unhealthy"
    
    return health

def _check_intelligence_health(self) -> Dict[str, Any]:
    """Check health of intelligence services"""
    intelligence_health = {
        "status": "healthy",
        "services": {}
    }
    
    # Check memory service
    try:
        memory_health = await self.memory_service.get_health_status()
        intelligence_health["services"]["memory"] = memory_health
    except Exception as e:
        intelligence_health["services"]["memory"] = {"status": "unhealthy", "error": str(e)}
    
    # Check planning service
    try:
        planning_health = await self.planning_service.get_health_status()
        intelligence_health["services"]["planning"] = planning_health
    except Exception as e:
        intelligence_health["services"]["planning"] = {"status": "unhealthy", "error": str(e)}
    
    # Check prompt engine
    try:
        prompt_health = await self.prompt_engine.get_health_status()
        intelligence_health["services"]["prompt_engine"] = prompt_health
    except Exception as e:
        intelligence_health["services"]["prompt_engine"] = {"status": "unhealthy", "error": str(e)}
    
    # Determine intelligence status
    if any(svc.get("status") == "unhealthy" for svc in intelligence_health["services"].values()):
        intelligence_health["status"] = "unhealthy"
    elif any(svc.get("status") == "degraded" for svc in intelligence_health["services"].values()):
        intelligence_health["status"] = "degraded"
    
    return intelligence_health
```

## Enhanced Configuration Management

### Intelligence Feature Flags

The container supports feature flags for intelligence capabilities:

```python
def _should_enable_intelligence(self) -> bool:
    """Check if intelligence features should be enabled"""
    return os.getenv('ENABLE_INTELLIGENT_FEATURES', 'true').lower() == 'true'

def _should_enable_memory(self) -> bool:
    """Check if memory features should be enabled"""
    return os.getenv('ENABLE_MEMORY_FEATURES', 'true').lower() == 'true'

def _should_enable_planning(self) -> bool:
    """Check if planning features should be enabled"""
    return os.getenv('ENABLE_PLANNING_FEATURES', 'true').lower() == 'true'

def _should_enable_advanced_prompting(self) -> bool:
    """Check if advanced prompting should be enabled"""
    return os.getenv('ENABLE_ADVANCED_PROMPTING', 'true').lower() == 'true'
```

### Conditional Intelligence Initialization

Intelligence services are initialized conditionally based on configuration:

```python
def _create_intelligence_layer(self):
    """Create intelligence layer based on configuration"""
    if not self._should_enable_intelligence():
        # Create mock intelligence services
        self.memory_service = MockMemoryService()
        self.planning_service = MockPlanningService()
        self.prompt_engine = MockPromptEngine()
        return
    
    # Create real intelligence services
    if self._should_enable_memory():
        self.memory_service = MemoryService(
            vector_store=self._get_vector_store(),
            redis_store=self._get_redis_store()
        )
    else:
        self.memory_service = MockMemoryService()
    
    if self._should_enable_planning():
        self.planning_service = PlanningService(
            llm_provider=self._get_llm_provider()
        )
    else:
        self.planning_service = MockPlanningService()
    
    if self._should_enable_advanced_prompting():
        self.prompt_engine = AdvancedPromptEngine(
            llm_provider=self._get_llm_provider(),
            memory_service=self.memory_service
        )
    else:
        self.prompt_engine = MockPromptEngine()
```

## Enhanced Testing Support

### Intelligence Service Mocking

The container provides mock implementations for testing:

```python
class MockMemoryService(IMemoryService):
    """Mock memory service for testing"""
    
    def __init__(self):
        self._mock_context = ConversationContext(
            working_memory=[],
            semantic_context=[],
            user_profile=MockUserProfile()
        )
        self._consolidated_insights = []
    
    async def retrieve_context(self, session_id: str, query: str) -> ConversationContext:
        return self._mock_context
    
    async def consolidate_insights(self, session_id: str, result: dict) -> bool:
        self._consolidated_insights.append({
            'session_id': session_id,
            'result': result
        })
        return True
    
    def get_consolidated_insights(self) -> List[Dict]:
        return self._consolidated_insights

class MockPlanningService(IPlanningService):
    """Mock planning service for testing"""
    
    def __init__(self):
        self._mock_strategy = StrategicPlan(
            plan_components=[
                PlanComponent(phase="analysis", actions=["analyze_problem"]),
                PlanComponent(phase="solution", actions=["generate_solution"])
            ]
        )
    
    async def plan_response_strategy(self, query: str, context: dict) -> StrategicPlan:
        return self._mock_strategy

class MockPromptEngine(IPromptEngine):
    """Mock prompt engine for testing"""
    
    async def assemble_prompt(self, question: str, response_type: ResponseType, context: dict) -> str:
        return f"Mock prompt for {response_type}: {question}"
    
    async def optimize_prompt(self, prompt: str, context: dict) -> str:
        return f"Optimized: {prompt}"
```

### Testing Container Initialization

Test the container with intelligence services:

```python
class TestDIContainerIntelligence:
    @pytest.fixture
    def container(self):
        # Reset container for testing
        container = DIContainer()
        container._reset()
        return container
    
    async def test_intelligence_services_initialized(self, container):
        # Enable intelligence features
        os.environ['ENABLE_INTELLIGENT_FEATURES'] = 'true'
        
        # Initialize container
        container.initialize()
        
        # Verify intelligence services exist
        assert container.memory_service is not None
        assert container.planning_service is not None
        assert container.prompt_engine is not None
        
        # Verify they implement correct interfaces
        assert isinstance(container.memory_service, IMemoryService)
        assert isinstance(container.planning_service, IPlanningService)
        assert isinstance(container.prompt_engine, IPromptEngine)
    
    async def test_agent_service_with_intelligence(self, container):
        # Enable intelligence features
        os.environ['ENABLE_INTELLIGENT_FEATURES'] = 'true'
        
        # Initialize container
        container.initialize()
        
        # Get agent service
        agent_service = container.get_agent_service()
        
        # Verify it has intelligence dependencies
        assert hasattr(agent_service, '_memory')
        assert hasattr(agent_service, '_planning')
        assert hasattr(agent_service, '_prompt_engine')
        
        # Verify dependencies are properly injected
        assert agent_service._memory is container.memory_service
        assert agent_service._planning is container.planning_service
        assert agent_service._prompt_engine is container.prompt_engine
```

## Enhanced Error Handling

### Intelligence Service Error Recovery

The container handles intelligence service failures gracefully:

```python
def _create_intelligence_layer(self):
    """Create intelligence layer with error handling"""
    try:
        if self._should_enable_memory():
            self.memory_service = MemoryService(
                vector_store=self._get_vector_store(),
                redis_store=self._get_redis_store()
            )
        else:
            self.memory_service = MockMemoryService()
    except Exception as e:
        logger.warning(f"Failed to initialize memory service: {e}")
        self.memory_service = MockMemoryService()
    
    try:
        if self._should_enable_planning():
            self.planning_service = PlanningService(
                llm_provider=self._get_llm_provider()
            )
        else:
            self.planning_service = MockPlanningService()
    except Exception as e:
        logger.warning(f"Failed to initialize planning service: {e}")
        self.planning_service = MockPlanningService()
    
    try:
        if self._should_enable_advanced_prompting():
            self.prompt_engine = AdvancedPromptEngine(
                llm_provider=self._get_llm_provider(),
                memory_service=self.memory_service
            )
        else:
            self.prompt_engine = MockPromptEngine()
    except Exception as e:
        logger.warning(f"Failed to initialize prompt engine: {e}")
        self.prompt_engine = MockPromptEngine()
```

## Performance Optimization

### Intelligence Service Caching

The container optimizes intelligence service performance:

```python
def _get_memory_service(self) -> IMemoryService:
    """Get memory service with caching"""
    if not hasattr(self, '_memory_service_cache'):
        self._memory_service_cache = {}
    
    cache_key = f"memory_service_{id(self)}"
    if cache_key not in self._memory_service_cache:
        self._memory_service_cache[cache_key] = self._create_memory_service()
    
    return self._memory_service_cache[cache_key]

def _get_planning_service(self) -> IPlanningService:
    """Get planning service with caching"""
    if not hasattr(self, '_planning_service_cache'):
        self._planning_service_cache = {}
    
    cache_key = f"planning_service_{id(self)}"
    if cache_key not in self._planning_service_cache:
        self._planning_service_cache[cache_key] = self._create_planning_service()
    
    return self._planning_service_cache[cache_key]
```

## Conclusion

The enhanced dependency injection system provides FaultMaven with sophisticated intelligent communication capabilities while maintaining the core principles of loose coupling, high testability, and flexible deployment. The new memory management, strategic planning, and advanced prompting services are seamlessly integrated into the existing DI architecture, enabling the system to learn, adapt, and provide increasingly effective assistance to users over time.

**Key Benefits of Enhanced DI System**:
- **Intelligent Service Integration**: Memory, planning, and prompting services seamlessly integrated
- **Context-Aware Dependencies**: All services receive context and intelligence capabilities
- **Graceful Degradation**: Fallback to mock services when intelligence features fail
- **Performance Optimization**: Caching and lazy initialization for intelligence services
- **Comprehensive Testing**: Mock implementations for all intelligence services
- **Configuration Flexibility**: Feature flags control intelligence capabilities

**Next Steps**:
1. **Implement Intelligence Services**: Create the actual memory, planning, and prompting services
2. **Enhance Existing Services**: Update current services to use intelligence dependencies
3. **Test and Validate**: Ensure performance and reliability of intelligent features
4. **Gradual Rollout**: Enable features incrementally with feature flags

This enhanced DI system positions FaultMaven for intelligent operation while maintaining all the benefits of the existing clean architecture.