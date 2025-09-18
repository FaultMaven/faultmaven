# Enhanced Dependency Injection Container Usage Guide

**Document Type**: Developer Guide  
**Last Updated**: August 2025

## Overview

This guide provides practical instructions for using FaultMaven's enhanced dependency injection (DI) container system. The `DIContainer` class manages all service dependencies through interface-based design, now including advanced intelligent communication capabilities with memory management, strategic planning, and dynamic prompting. The container provides centralized dependency resolution, health monitoring, graceful degradation, and intelligence service integration.

## Quick Start

### Basic Container Usage

```python
from faultmaven.container import container

# Get core services through interface resolution
agent_service = container.get_agent_service()
data_service = container.get_data_service()
knowledge_service = container.get_knowledge_service()
session_service = container.get_session_service()
llm_provider = container.get_llm_provider()          # Returns ILLMProvider implementation

# Get agentic framework components
workflow_engine = container.get_agentic_workflow_engine()        # Main orchestrator
state_manager = container.get_agentic_state_manager()            # Memory & state
classification_engine = container.get_agentic_classification_engine()  # Query analysis
tool_broker = container.get_agentic_tool_broker()               # Tool orchestration
guardrails = container.get_agentic_guardrails()                 # Security layer
response_synthesizer = container.get_agentic_response_synthesizer()  # Response assembly
error_manager = container.get_agentic_error_manager()           # Error handling

# Check container health
health = container.health_check()
print(f"Container status: {health.get('status', 'unknown')}")
print(f"Agentic components available: {health.get('components', {}).get('agentic_workflow_engine', False)}")
```

### Enhanced FastAPI Integration

```python
# api/v1/dependencies.py
from faultmaven.container import container
from faultmaven.services.agentic.orchestration.agent_service import AgentService

def get_agent_service() -> AgentService:
    """FastAPI dependency for agent service"""
    return container.get_agent_service()

def get_agentic_workflow_engine():
    """FastAPI dependency for agentic workflow engine"""
    return container.get_agentic_workflow_engine()

def get_agentic_state_manager():
    """FastAPI dependency for agentic state manager"""
    return container.get_agentic_state_manager()

# api/v1/routes/agent.py
from fastapi import APIRouter, Depends

router = APIRouter()

@router.post("/query")
async def process_query(
    query: QueryRequest,
    agent_service: AgentService = Depends(get_agent_service),
    workflow_engine = Depends(get_agentic_workflow_engine)
):
    # Agent service with agentic framework integration
    result = await agent_service.process_query(query)
    
    # Access agentic workflow engine for advanced processing if needed
    if workflow_engine:
        enhanced_result = await workflow_engine.process_query(query.query, query.session_id)
        result.update(enhanced_result)
    
    return result
```

## Enhanced Container Architecture

### Singleton Pattern with Intelligence

The container follows a singleton pattern ensuring consistent dependency management with intelligence services:

```python
from faultmaven.container import DIContainer

# All instances return the same container
container1 = DIContainer()
container2 = DIContainer()
assert container1 is container2  # True

# Global proxy for convenience
from faultmaven.container import container
assert container() is container1  # True

# Intelligence services are globally accessible
memory_service = container.get_memory_service()
planning_service = container.get_planning_service()
prompt_engine = container.get_prompt_engine()
```

### Enhanced Layered Initialization

The container now initializes dependencies in layers with intelligence services:

```python
class DIContainer:
    def initialize(self):
        """Initialize all dependencies in dependency order with intelligence"""
        try:
            # Layer 1: Intelligence (core intelligent capabilities)
            self._create_intelligence_layer()
            
            # Layer 2: Infrastructure (external services)
            self._create_infrastructure_layer()
            
            # Layer 3: Tools (agent capabilities with intelligence integration)
            self._create_tools_layer()
            
            # Layer 4: Enhanced Services (with intelligence dependencies)
            self._create_service_layer()
            
            self._initialized = True
        except Exception as e:
            # Graceful degradation with mock intelligence implementations
            self._create_minimal_container()
```

## Enhanced Service Resolution

### Available Enhanced Services

The container now provides the following enhanced service getters:

```python
# Enhanced Core Services with Intelligence
agent_service = container.get_agent_service()        # Pure AgentService
data_service = container.get_data_service()          # EnhancedDataService  
knowledge_service = container.get_knowledge_service() # EnhancedKnowledgeService
session_service = container.get_session_service()    # EnhancedSessionService

# New Intelligence Services
memory_service = container.get_memory_service()      # IMemoryService
planning_service = container.get_planning_service()  # IPlanningService
prompt_engine = container.get_prompt_engine()        # IPromptEngine

# Infrastructure Components (Interface Implementations)
llm_provider = container.get_llm_provider()          # ILLMProvider
sanitizer = container.get_sanitizer()               # ISanitizer
tracer = container.get_tracer()                     # ITracer

# Enhanced Processing Components with Intelligence
classifier = container.get_data_classifier()         # EnhancedDataClassifier
log_processor = container.get_log_processor()       # EnhancedLogProcessor

# Enhanced Tools Collection with Intelligence Integration
tools = container.get_tools()                       # List[EnhancedBaseTool]
```

### Enhanced Service Dependencies

Enhanced services now receive intelligence dependencies automatically:

```python
# AgentService receives these dependencies automatically:
class AgentService:
    def __init__(
        self,
        llm_provider: ILLMProvider,      # Multi-provider routing
        tools: List[BaseTool],           # Enhanced knowledge base + web search
        tracer: ITracer,                 # Opik tracing
        sanitizer: ISanitizer,           # PII redaction
        memory_service: IMemoryService,  # NEW: Memory management
        planning_service: IPlanningService, # NEW: Strategic planning
        prompt_engine: IPromptEngine     # NEW: Advanced prompting
    ):
        # All dependencies are interface implementations
        self.llm_provider = llm_provider
        self.tools = tools
        self.tracer = tracer
        self.sanitizer = sanitizer
        self.memory_service = memory_service        # Store intelligence services
        self.planning_service = planning_service    # Store intelligence services
        self.prompt_engine = prompt_engine          # Store intelligence services
```

### Intelligence Service Usage

```python
# Get intelligence services
memory_service = container.get_memory_service()
planning_service = container.get_planning_service()
prompt_engine = container.get_prompt_engine()

# Use memory service for context retrieval
context = await memory_service.retrieve_context("session_123", "user query")
print(f"Retrieved context: {context.summary}")

# Use planning service for strategy development
strategy = await planning_service.plan_response_strategy("user query", context)
print(f"Developed strategy: {strategy.current_phase}")

# Use prompt engine for enhanced prompting
prompt = await prompt_engine.assemble_prompt(
    question="user query",
    response_type=ResponseType.ANSWER,
    context=context
)
print(f"Generated prompt: {prompt[:100]}...")

# Consolidate insights after processing
await memory_service.consolidate_insights("session_123", {"result": "success"})
```

## Enhanced Interface-Based Dependencies

### Intelligence Service Interfaces

All intelligence services are accessed through interfaces:

```python
from faultmaven.models.interfaces import (
    IMemoryService, 
    IPlanningService, 
    IPromptEngine,
    ILLMProvider, 
    ISanitizer, 
    ITracer
)

# Container returns interface implementations
memory_service: IMemoryService = container.get_memory_service()
planning_service: IPlanningService = container.get_planning_service()
prompt_engine: IPromptEngine = container.get_prompt_engine()
llm_provider: ILLMProvider = container.get_llm_provider()
sanitizer: ISanitizer = container.get_sanitizer()
tracer: ITracer = container.get_tracer()

# Can be any implementation (production, mock, etc.)
assert isinstance(memory_service, IMemoryService)      # True
assert isinstance(planning_service, IPlanningService)  # True
assert isinstance(prompt_engine, IPromptEngine)        # True
assert isinstance(llm_provider, ILLMProvider)          # True
```

### Enhanced Implementation Selection

The container automatically selects appropriate implementations with intelligence:

```python
def _create_intelligence_layer(self):
    """Create intelligence layer with environment-based selection"""
    
    # Memory service with Redis and ChromaDB
    if os.getenv("ENABLE_MEMORY_FEATURES") == "true":
        try:
            self.memory_service = MemoryService(
                vector_store=self._get_vector_store(),
                redis_store=self._get_redis_store()
            )
            logger.info("✅ Memory service initialized")
        except Exception as e:
            logger.warning(f"⚠️ Memory service failed: {e}")
            self.memory_service = MockMemoryService()
    else:
        self.memory_service = MockMemoryService()
    
    # Planning service with LLM integration
    if os.getenv("ENABLE_PLANNING_FEATURES") == "true":
        try:
            self.planning_service = PlanningService(
                llm_provider=self._get_llm_provider()
            )
            logger.info("✅ Planning service initialized")
        except Exception as e:
            logger.warning(f"⚠️ Planning service failed: {e}")
            self.planning_service = MockPlanningService()
    else:
        self.planning_service = MockPlanningService()
    
    # Advanced prompt engine
    if os.getenv("ENABLE_ADVANCED_PROMPTING") == "true":
        try:
            self.prompt_engine = AdvancedPromptEngine(
                llm_provider=self._get_llm_provider(),
                memory_service=self.memory_service
            )
            logger.info("✅ Advanced prompt engine initialized")
        except Exception as e:
            logger.warning(f"⚠️ Advanced prompt engine failed: {e}")
            self.prompt_engine = MockPromptEngine()
    else:
        self.prompt_engine = MockPromptEngine()
```

## Enhanced Health Monitoring

### Enhanced Container Health Check

```python
# Comprehensive health status including intelligence services
health = container.get_health_status()

print(f"Status: {health['status']}")  # healthy | degraded | unhealthy
print("Components:")
for layer, layer_health in health['components'].items():
    print(f"\n{layer.title()} Layer:")
    if isinstance(layer_health, dict) and 'services' in layer_health:
        for service, service_health in layer_health['services'].items():
            icon = "✅" if service_health.get('status') == 'healthy' else "❌"
            print(f"  {icon} {service}: {service_health.get('status', 'unknown')}")
    else:
        icon = "✅" if layer_health else "❌"
        print(f"  {icon} {layer}: {layer_health}")

# Example output:
# Status: healthy
# Components:
# Infrastructure Layer:
#   ✅ llm_provider: True
#   ✅ sanitizer: True
#   ✅ tracer: True
# 
# Intelligence Layer:
#   ✅ memory: healthy
#   ✅ planning: healthy
#   ✅ prompt_engine: healthy
# 
# Services Layer:
#   ✅ agent_service: True
#   ✅ data_service: True
#   ✅ knowledge_service: True
```

### Enhanced HTTP Health Endpoint

```python
# Enhanced health endpoint integration (main.py)
@app.get("/health")
async def check_health():
    health = container.get_health_status()
    status_code = 200 if health["status"] == "healthy" else 503
    return JSONResponse(content=health, status_code=status_code)

@app.get("/health/intelligence")
async def check_intelligence_health():
    health = container.get_health_status()
    intelligence_health = health["components"]["intelligence"]
    status_code = 200 if intelligence_health["status"] == "healthy" else 503
    return JSONResponse(content=intelligence_health, status_code=status_code)
```

```bash
# Check enhanced container health via API
curl http://localhost:8000/health

# Check intelligence services specifically
curl http://localhost:8000/health/intelligence

# Response example:
{
  "status": "healthy",
  "services": {
    "memory": {
      "status": "healthy",
      "memory_usage_mb": 128,
      "active_sessions": 5
    },
    "planning": {
      "status": "healthy",
      "active_strategies": 3,
      "cache_hit_rate": 0.85
    },
    "prompt_engine": {
      "status": "healthy",
      "optimization_level": "high",
      "quality_score": 0.95
    }
  }
}
```

### Intelligence Service Status Details

```python
# Detailed intelligence service inspection
if health["status"] == "degraded":
    # Check intelligence services specifically
    intelligence_health = health["components"]["intelligence"]
    
    if intelligence_health["status"] == "degraded":
        failed_intelligence_services = [
            name for name, service_health in intelligence_health["services"].items() 
            if service_health.get('status') != 'healthy'
        ]
        print(f"Failed intelligence services: {failed_intelligence_services}")
        
        # Check specific intelligence service details
        for service_name in failed_intelligence_services:
            service = getattr(container, f"{service_name}_service")
            if hasattr(service, 'get_health_status'):
                service_health = await service.get_health_status()
                print(f"❌ {service_name} health: {service_health}")
```

## Enhanced Testing Integration

### Enhanced Container Reset

```python
import pytest
from faultmaven.container import DIContainer

@pytest.fixture(autouse=True)
def reset_enhanced_container():
    """Reset enhanced container state between tests"""
    container = DIContainer()
    container.reset()
    yield
    container.reset()  # Cleanup after test
```

### Enhanced Mock Implementations

```python
# Test with enhanced mock dependencies
def test_enhanced_agent_service_with_intelligence_mocks():
    # Reset container
    container = DIContainer()
    container.reset()
    
    # Set test environment for intelligence features
    os.environ["ENABLE_INTELLIGENT_FEATURES"] = "true"
    os.environ["ENABLE_MEMORY_FEATURES"] = "true"
    os.environ["ENABLE_PLANNING_FEATURES"] = "true"
    os.environ["ENABLE_ADVANCED_PROMPTING"] = "true"
    
    # Get enhanced service with intelligence dependencies
    agent_service = container.get_agent_service()
    
    # Verify enhanced service has intelligence dependencies
    assert hasattr(agent_service, '_memory_service')
    assert hasattr(agent_service, '_planning_service')
    assert hasattr(agent_service, '_prompt_engine')
    
    # Verify mock intelligence implementations
    assert isinstance(agent_service._memory_service, MockMemoryService)
    assert isinstance(agent_service._planning_service, MockPlanningService)
    assert isinstance(agent_service._prompt_engine, MockPromptEngine)
```

### Enhanced Custom Test Configuration

```python
# Override specific intelligence dependencies for testing
class TestEnhancedContainer(DIContainer):
    def _create_intelligence_layer(self):
        # Custom test intelligence implementations
        self.memory_service = TestMemoryService()
        self.planning_service = TestPlanningService() 
        self.prompt_engine = TestPromptEngine()
        
        # Real implementations for other components
        super()._create_infrastructure_layer()
        super()._create_tools_layer()
        super()._create_service_layer()

# Use in enhanced tests
def test_with_custom_intelligence_container():
    original_container = DIContainer._instance
    DIContainer._instance = TestEnhancedContainer()
    
    try:
        agent_service = container.get_agent_service()
        memory_service = container.get_memory_service()
        
        # Test with custom intelligence implementations
        assert isinstance(memory_service, TestMemoryService)
        # ... test enhanced functionality
    finally:
        DIContainer._instance = original_container
```

## Enhanced Configuration and Environment

### Enhanced Environment-Based Configuration

The container adapts to different environments with intelligence features:

```python
# Development environment with full intelligence
ENVIRONMENT=development
ENABLE_INTELLIGENT_FEATURES=true
ENABLE_MEMORY_FEATURES=true
ENABLE_PLANNING_FEATURES=true
ENABLE_ADVANCED_PROMPTING=true
OPIK_ENABLED=true

# Testing environment with mock intelligence
ENVIRONMENT=testing
ENABLE_INTELLIGENT_FEATURES=true
ENABLE_MEMORY_FEATURES=true
ENABLE_PLANNING_FEATURES=true
ENABLE_ADVANCED_PROMPTING=true
USE_MOCK_LLM=true
TESTING_MODE=true

# Production environment with full intelligence
ENVIRONMENT=production
ENABLE_INTELLIGENT_FEATURES=true
ENABLE_MEMORY_FEATURES=true
ENABLE_PLANNING_FEATURES=true
ENABLE_ADVANCED_PROMPTING=true
OPIK_ENABLED=true
```

### Enhanced Feature Flag Integration

```python
from faultmaven.config.enhanced_feature_flags import (
    ENABLE_INTELLIGENT_FEATURES,
    ENABLE_MEMORY_FEATURES,
    ENABLE_PLANNING_FEATURES,
    ENABLE_ADVANCED_PROMPTING
)

def get_enhanced_service_implementation():
    """Get enhanced service based on feature flags"""
    if (ENABLE_INTELLIGENT_FEATURES and 
        ENABLE_MEMORY_FEATURES and 
        ENABLE_PLANNING_FEATURES and 
        ENABLE_ADVANCED_PROMPTING):
        # Use enhanced services with intelligence from container
        return container.get_agent_service()
    else:
        # Legacy service instantiation
        return LegacyAgentService()
```

### Enhanced Custom Configuration

```python
# Custom enhanced container configuration
class CustomEnhancedContainer(DIContainer):
    def _create_intelligence_layer(self):
        # Custom memory service configuration
        if os.getenv("CUSTOM_MEMORY_PROVIDER"):
            self.memory_service = CustomMemoryService()
        else:
            super()._create_intelligence_layer()
        
        # Custom planning service configuration
        if os.getenv("CUSTOM_PLANNING_STRATEGY"):
            self.planning_service = CustomPlanningService()
        else:
            super()._create_intelligence_layer()
        
        # Custom prompt engine configuration
        if os.getenv("CUSTOM_PROMPT_OPTIMIZATION"):
            self.prompt_engine = CustomPromptEngine()
        else:
            super()._create_intelligence_layer()
    
    def _create_service_layer(self):
        # Pure interface-based service configuration
        self.agent_service = AgentService(
            llm_provider=self.llm_provider,
            tools=self.tools,
            tracer=self.tracer,
            sanitizer=self.sanitizer
        )
```

## Enhanced Error Handling and Resilience

### Enhanced Graceful Degradation

The container handles intelligence service failures gracefully:

```python
def initialize(self):
    try:
        # Attempt full enhanced initialization
        self._create_intelligence_layer()
        self._create_infrastructure_layer()
        self._create_tools_layer()
        self._create_service_layer()
        self._initialized = True
        logger.info("✅ Enhanced DI Container initialized successfully")
        
    except Exception as e:
        logger.error(f"❌ Enhanced DI Container initialization failed: {e}")
        
        # Fallback to minimal container with mock intelligence
        try:
            self._create_minimal_enhanced_container()
            self._initialized = True
            logger.warning("⚠️ Using minimal enhanced container with mock intelligence")
        except Exception:
            self._initialized = False
            raise
```

### Enhanced Minimal Container for Testing

```python
def _create_minimal_enhanced_container(self):
    """Create minimal enhanced container with mock intelligence implementations"""
    from unittest.mock import MagicMock
    
    # Mock intelligence layer
    self.memory_service = MockMemoryService()
    self.planning_service = MockPlanningService()
    self.prompt_engine = MockPromptEngine()
    
    # Mock infrastructure layer
    self.llm_provider = MagicMock(spec=ILLMProvider)
    self.sanitizer = MagicMock(spec=ISanitizer)
    self.tracer = MagicMock(spec=ITracer)
    
    # Empty tools list
    self.tools = []
    
    # Mock enhanced service layer
    self.agent_service = MagicMock(spec=AgentService)
    self.data_service = MagicMock(spec=EnhancedDataService)
    self.knowledge_service = MagicMock(spec=EnhancedKnowledgeService)
    
    logger.info("Created minimal enhanced container with mock intelligence for testing environment")
```

### Enhanced Individual Component Failure Handling

```python
def _create_intelligence_layer(self):
    """Create intelligence layer with error isolation"""
    
    # Memory Service (may fail if Redis/ChromaDB unavailable)
    try:
        self.memory_service = MemoryService(
            vector_store=self._get_vector_store(),
            redis_store=self._get_redis_store()
        )
        logger.info("✅ Memory service initialized")
    except Exception as e:
        logger.warning(f"⚠️ Memory service failed: {e}")
        self.memory_service = MockMemoryService()
    
    # Planning Service (may fail if LLM provider unavailable)
    try:
        self.planning_service = PlanningService(
            llm_provider=self._get_llm_provider()
        )
        logger.info("✅ Planning service initialized")
    except Exception as e:
        logger.warning(f"⚠️ Planning service failed: {e}")
        self.planning_service = MockPlanningService()
    
    # Prompt Engine (may fail if LLM provider unavailable)
    try:
        self.prompt_engine = AdvancedPromptEngine(
            llm_provider=self._get_llm_provider(),
            memory_service=self.memory_service
        )
        logger.info("✅ Advanced prompt engine initialized")
    except Exception as e:
        logger.warning(f"⚠️ Advanced prompt engine failed: {e}")
        self.prompt_engine = MockPromptEngine()
    
    logger.info(f"Intelligence layer initialized with {sum([1 for s in [self.memory_service, self.planning_service, self.prompt_engine] if not isinstance(s, MockService)])} real services")
```

## Enhanced Performance Considerations

### Intelligence Service Caching

```python
def get_memory_service(self):
    """Get memory service with caching"""
    if not hasattr(self, '_cached_memory_service'):
        self.initialize()
        self._cached_memory_service = self.memory_service
    return self._cached_memory_service

def get_planning_service(self):
    """Get planning service with caching"""
    if not hasattr(self, '_cached_planning_service'):
        self.initialize()
        self._cached_planning_service = self.planning_service
    return self._cached_planning_service

def get_prompt_engine(self):
    """Get prompt engine with caching"""
    if not hasattr(self, '_cached_prompt_engine'):
        self.initialize()
        self._cached_prompt_engine = self.prompt_engine
    return self._cached_prompt_engine
```

### Enhanced Singleton Benefits

- **Memory Efficiency**: Single instance of each intelligence component
- **Consistent State**: All consumers use same intelligence component instances
- **Reduced Overhead**: No repeated intelligence service initialization costs
- **Predictable Behavior**: Consistent intelligence behavior across application
- **Context Sharing**: Memory and planning context shared across services

### Enhanced Lazy Initialization Benefits

- **Faster Startup**: Only create intelligence components when needed
- **Memory Conservation**: Don't load unused intelligence dependencies  
- **Error Isolation**: Failed intelligence components don't prevent application start
- **Development Speed**: Developers can work without all intelligence dependencies
- **Feature Toggle Support**: Enable/disable intelligence features at runtime

## Enhanced Best Practices

### Enhanced Container Usage Guidelines

1. **Intelligence Service Access**: Use dedicated getters for each intelligence service
2. **Interface Returns**: Always return interface types, not concrete classes  
3. **Error Handling**: Gracefully handle missing intelligence dependencies
4. **Health Monitoring**: Include health checks for all intelligence services
5. **Testing Support**: Provide comprehensive mock implementations for testing
6. **Feature Flags**: Use feature flags to control intelligence capabilities
7. **Performance Monitoring**: Monitor intelligence service performance

### Enhanced Development Workflow

```python
# 1. Define enhanced interfaces first
class IEnhancedService(ABC):
    @abstractmethod
    def process_with_intelligence(self, data: str, session_id: str) -> EnhancedResult: ...

# 2. Implement enhanced concrete class with intelligence
class EnhancedService(IEnhancedService):
    def __init__(
        self, 
        dependency: ISomeDependency,
        memory_service: IMemoryService,
        planning_service: IPlanningService
    ):
        self.dependency = dependency
        self.memory_service = memory_service
        self.planning_service = planning_service
    
    async def process_with_intelligence(self, data: str, session_id: str) -> EnhancedResult:
        # Get memory context
        context = await self.memory_service.retrieve_context(session_id, data)
        
        # Plan processing strategy
        strategy = await self.planning_service.plan_processing_strategy(data, context)
        
        # Process with intelligence
        result = self.dependency.transform(data)
        
        # Consolidate insights
        await self.memory_service.consolidate_insights(session_id, {"result": result})
        
        return EnhancedResult(data=result, context=context, strategy=strategy)

# 3. Add to enhanced container
def get_enhanced_service(self) -> IEnhancedService:
    self.initialize()
    return self.enhanced_service

def _create_service_layer(self):
    # ... existing enhanced services ...
    self.enhanced_service = EnhancedService(
        dependency=self.some_dependency,
        memory_service=self.memory_service,
        planning_service=self.planning_service
    )
```

### Enhanced Testing Strategies

```python
# 1. Enhanced interface-based mocking
@patch.object(DIContainer, 'get_memory_service')
@patch.object(DIContainer, 'get_planning_service')
def test_with_mock_intelligence(mock_get_planning, mock_get_memory):
    mock_memory = MagicMock(spec=IMemoryService)
    mock_planning = MagicMock(spec=IPlanningService)
    mock_get_memory.return_value = mock_memory
    mock_get_planning.return_value = mock_planning
    
    service = container.get_enhanced_service()
    # Test with mock intelligence implementations

# 2. Enhanced container state isolation
def test_with_clean_enhanced_container():
    container.reset()
    os.environ["ENABLE_INTELLIGENT_FEATURES"] = "true"
    os.environ["TEST_MODE"] = "true"
    
    service = container.get_enhanced_service()
    # Test with fresh enhanced container state

# 3. Enhanced health validation
def test_enhanced_container_health():
    health = container.get_health_status()
    assert health["status"] in ["healthy", "degraded", "unhealthy"]
    assert "intelligence" in health["components"]
    
    intelligence_health = health["components"]["intelligence"]
    assert "memory" in intelligence_health["services"]
    assert "planning" in intelligence_health["services"]
    assert "prompt_engine" in intelligence_health["services"]
```

## Enhanced Troubleshooting

### Enhanced Common Issues

**Intelligence Services Not Initialized**:
```python
# Problem: Accessing intelligence services before initialization
memory_service = container.get_memory_service()  # May fail

# Solution: Check initialization status
if not container._initialized:
    container.initialize()
memory_service = container.get_memory_service()
```

**Intelligence Feature Flags Disabled**:
```python
# Problem: Intelligence services not available
# Solution: Check feature flags

if not os.getenv("ENABLE_INTELLIGENT_FEATURES", "false").lower() == "true":
    print("Intelligence features are disabled")
    print("Set ENABLE_INTELLIGENT_FEATURES=true to enable")

if not os.getenv("ENABLE_MEMORY_FEATURES", "false").lower() == "true":
    print("Memory features are disabled")
    print("Set ENABLE_MEMORY_FEATURES=true to enable")
```

**Mock Intelligence Services in Tests**:
```python
# Problem: Container returns cached real intelligence instances
# Solution: Reset container before each test

@pytest.fixture(autouse=True)
def reset_enhanced_container():
    DIContainer._instance = None
    yield
    DIContainer._instance = None
```

### Enhanced Debug Information

```python
# Enhanced container debug information
def debug_enhanced_container():
    health = container.get_health_status()
    print(f"Enhanced Container Status: {health['status']}")
    print(f"Initialized: {container._initialized}")
    
    # Check intelligence services
    if "intelligence" in health["components"]:
        intelligence_health = health["components"]["intelligence"]
        print(f"Intelligence Status: {intelligence_health['status']}")
        
        for service_name, service_health in intelligence_health["services"].items():
            status_icon = "✅" if service_health.get('status') == 'healthy' else "❌"
            print(f"  {status_icon} {service_name}: {service_health.get('status', 'unknown')}")
            
            # Show additional metrics if available
            if 'memory_usage_mb' in service_health:
                print(f"    Memory Usage: {service_health['memory_usage_mb']}MB")
            if 'active_strategies' in service_health:
                print(f"    Active Strategies: {service_health['active_strategies']}")
            if 'quality_score' in service_health:
                print(f"    Quality Score: {service_health['quality_score']}")
    
    # Check LLM provider status
    if hasattr(container, 'llm_provider'):
        llm_provider = container.get_llm_provider()
        print(f"LLM Provider: {type(llm_provider).__name__}")
        
        if hasattr(llm_provider, 'get_provider_status'):
            status = llm_provider.get_provider_status()
            print("LLM Provider Status:")
            for name, info in status.items():
                print(f"  {name}: {'✅' if info['available'] else '❌'}")
```

## Enhanced Migration Guide

### From Legacy Services to Enhanced Services

```python
# Before (legacy service instantiation)
llm_router = LLMRouter()
sanitizer = DataSanitizer()
tracer = OpikTracer()
tools = [KnowledgeBaseTool(), WebSearchTool()]

agent_service = AgentService(
    llm_provider=llm_router,
    sanitizer=sanitizer,
    tracer=tracer,
    tools=tools
)

# After (enhanced container-based with intelligence)
agent_service = container.get_agent_service()
# All dependencies including intelligence services automatically injected

# Access intelligence services directly if needed
memory_service = container.get_memory_service()
planning_service = container.get_planning_service()
prompt_engine = container.get_prompt_engine()
```

### Gradual Intelligence Feature Adoption

```python
# Mixed approach during intelligence migration
def get_enhanced_agent_service():
    if (ENABLE_INTELLIGENT_FEATURES and 
        ENABLE_MEMORY_FEATURES and 
        ENABLE_PLANNING_FEATURES and 
        ENABLE_ADVANCED_PROMPTING):
        # Use enhanced services with intelligence from container
        return container.get_agent_service()
    else:
        # Legacy service instantiation
        return create_legacy_agent_service()

# Enable features incrementally
# Phase 1: Enable memory features
os.environ["ENABLE_MEMORY_FEATURES"] = "true"

# Phase 2: Enable planning features  
os.environ["ENABLE_PLANNING_FEATURES"] = "true"

# Phase 3: Enable advanced prompting
os.environ["ENABLE_ADVANCED_PROMPTING"] = "true"

# Phase 4: Enable all intelligence features
os.environ["ENABLE_INTELLIGENT_FEATURES"] = "true"
```

### Intelligence Service Configuration Migration

```python
# Before: No intelligence configuration
# After: Enhanced intelligence configuration

# Memory service configuration
MEMORY_HIERARCHY_LEVELS=4
MEMORY_MAX_WORKING_SIZE_MB=512
MEMORY_MAX_SESSION_SIZE_MB=256
MEMORY_MAX_USER_SIZE_MB=1024
MEMORY_MAX_EPISODIC_SIZE_MB=2048

# Planning service configuration
PLANNING_MAX_PHASES=7
PLANNING_STRATEGY_CACHE_SIZE=100
PLANNING_RISK_ASSESSMENT_ENABLED=true

# Advanced prompting configuration
PROMPT_MAX_LAYERS=6
PROMPT_OPTIMIZATION_ENABLED=true
PROMPT_VERSIONING_ENABLED=true
```

This enhanced dependency injection container provides the foundation for FaultMaven's intelligent, maintainable, testable, and flexible architecture while supporting smooth migration paths and operational excellence with advanced communication capabilities.