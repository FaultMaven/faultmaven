# Enhanced FaultMaven Testing Guide

**Document Type**: Testing Guide  
**Last Updated**: August 2025

## Overview

This comprehensive testing guide covers FaultMaven's enhanced interface-based testing architecture, built around clean architecture principles and dependency injection with advanced intelligent communication capabilities. With 341 passing tests and 71% coverage, our testing strategy ensures reliability across all architectural layers including the new memory management, strategic planning, and advanced prompting systems.

## Enhanced Testing Architecture

### Test Organization by Layer

FaultMaven's tests are organized to match the enhanced clean architecture structure:

```
tests/
├── api/                    # API Layer Testing
│   ├── test_data_ingestion.py      # Data upload and processing endpoints
│   ├── test_kb_management.py       # Knowledge base management endpoints  
│   ├── test_query_processing.py    # Troubleshooting query endpoints
│   └── test_sessions.py            # Session lifecycle endpoints
├── services/               # Service Layer Testing
│   ├── test_agent_service.py       # EnhancedAgentService orchestration
│   ├── test_data_service.py        # EnhancedDataService operations
│   ├── test_knowledge_service.py   # EnhancedKnowledgeService operations
│   └── test_intelligence_services.py # Memory, Planning, and Prompt services
├── core/                   # Core Domain Testing
│   ├── test_classifier.py          # Enhanced data classification logic
│   ├── test_core_agent.py          # Enhanced core agent functionality
│   ├── test_core_agent_errors.py   # Enhanced agent error handling
│   ├── test_doctrine.py            # Enhanced SRE troubleshooting doctrine
│   ├── test_ingestion.py           # Enhanced knowledge ingestion
│   ├── test_log_processor.py       # Enhanced log analysis logic
│   ├── test_memory_manager.py      # Memory management system
│   ├── test_planning_engine.py     # Strategic planning engine
│   ├── test_prompt_engine.py       # Advanced prompting system
│   └── tools/                      # Enhanced Agent Tools Testing
│       ├── test_knowledge_base.py  # Enhanced knowledge base tool
│       └── test_web_search.py      # Enhanced web search tool
├── infrastructure/         # Infrastructure Layer Testing  
│   ├── test_opik_initialization_fix.py  # Observability tracing
│   ├── test_redaction.py          # Enhanced data sanitization
│   ├── test_redaction_errors.py   # Enhanced sanitization error handling
│   ├── test_router.py              # Enhanced LLM provider routing
│   ├── test_memory_stores.py      # Memory storage implementations
│   └── test_planning_stores.py    # Planning storage implementations
├── unit/                   # Architecture Component Testing
│   ├── test_container.py           # Enhanced DI container functionality
│   ├── test_dependency_injection.py # Enhanced DI patterns and resolution
│   ├── test_feature_flags.py       # Enhanced feature flag management
│   ├── test_interface_compliance.py # Enhanced interface implementation validation
│   ├── test_interface_implementations.py # Enhanced interface contract testing
│   ├── test_interfaces.py          # Enhanced interface definition testing
│   ├── test_memory_interfaces.py   # Memory service interface testing
│   ├── test_planning_interfaces.py # Planning service interface testing
│   └── test_prompt_interfaces.py   # Prompt engine interface testing
├── integration/            # End-to-End Testing
│   ├── conftest.py                 # Enhanced integration test fixtures
│   ├── mock_servers.py             # Enhanced mock API servers
│   ├── test_intelligence_workflows.py # Memory and planning workflows
│   └── test_*.py                   # Enhanced workflow integration tests
└── conftest.py             # Global test configuration and fixtures
```

## Enhanced Test Categories and Markers

### Pytest Markers

Tests are categorized using enhanced pytest markers for selective execution:

```python
# Enhanced test marker definitions (pytest.ini)
[tool:pytest]
markers =
    unit: Unit tests for isolated components
    integration: Integration tests requiring external services
    security: Security and PII redaction tests  
    api: API endpoint and request/response tests
    agent: Enhanced AI agent and LangGraph workflow tests
    data_processing: Enhanced data classification and log processing tests
    llm: Enhanced LLM provider routing and fallback tests
    session: Enhanced session management and lifecycle tests
    intelligence: Memory, planning, and prompting system tests
    memory: Memory management and consolidation tests
    planning: Strategic planning and problem decomposition tests
    prompting: Advanced prompting and optimization tests
    performance: Performance and scalability tests for intelligence services
```

### Running Enhanced Tests by Category

```bash
# All tests (including new intelligence tests)
pytest

# Unit tests only (fastest)
pytest -m unit

# Integration tests (requires Docker services)
pytest -m integration  

# Security and privacy tests
pytest -m security

# API endpoint tests
pytest -m api

# Enhanced AI agent functionality tests  
pytest -m agent

# Enhanced LLM provider and routing tests
pytest -m llm

# Data processing pipeline tests
pytest -m data_processing

# Enhanced session management tests
pytest -m session

# Intelligence system tests (new)
pytest -m intelligence

# Memory management tests (new)
pytest -m memory

# Strategic planning tests (new)
pytest -m planning

# Advanced prompting tests (new)
pytest -m prompting

# Performance tests for intelligence services (new)
pytest -m performance
```

## Enhanced Interface-Based Testing Strategy

### Intelligence Service Dependency Injection Testing

FaultMaven's enhanced interface-based architecture enables comprehensive mocking of intelligence services:

```python
# Test with enhanced interface mocks
@pytest.fixture
def mock_memory_service():
    """Mock memory service implementing IMemoryService interface"""
    mock = MagicMock(spec=IMemoryService)
    mock.retrieve_context.return_value = ConversationContext(
        working_memory=["previous_query"],
        semantic_context=[{"content": "relevant_info", "relevance": 0.9}],
        user_profile=MockUserProfile(expertise_level="intermediate")
    )
    mock.consolidate_insights.return_value = True
    mock.get_user_profile.return_value = MockUserProfile(expertise_level="intermediate")
    return mock

@pytest.fixture
def mock_planning_service():
    """Mock planning service implementing IPlanningService interface"""
    mock = MagicMock(spec=IPlanningService)
    mock.plan_response_strategy.return_value = StrategicPlan(
        id="test_strategy_123",
        current_phase="analysis",
        plan_components=[
            PlanComponent(phase="analysis", actions=["analyze_problem"]),
            PlanComponent(phase="solution", actions=["generate_solution"])
        ]
    )
    mock.get_current_context.return_value = PlanningContext(
        current_phase="analysis",
        active_strategy="test_strategy_123",
        completed_phases=[]
    )
    return mock

@pytest.fixture
def mock_prompt_engine():
    """Mock prompt engine implementing IPromptEngine interface"""
    mock = MagicMock(spec=IPromptEngine)
    mock.assemble_prompt.return_value = "Enhanced prompt with context"
    mock.optimize_prompt.return_value = "Optimized enhanced prompt"
    mock.get_performance_metrics.return_value = {
        "response_time_ms": 150,
        "quality_score": 0.95,
        "optimization_level": "high"
    }
    return mock

@patch.object(DIContainer, 'get_agent_service')
def test_enhanced_agent_service_with_intelligence(mock_get_agent, 
                                                 mock_memory_service, 
                                                 mock_planning_service, 
                                                 mock_prompt_engine):
    """Test EnhancedAgentService with mocked intelligence services"""
    mock_get_agent.return_value = EnhancedAgentService(
        llm_provider=mock_llm_provider,
        tools=[],
        tracer=mock_tracer,
        sanitizer=mock_sanitizer,
        memory_service=mock_memory_service,
        planning_service=mock_planning_service,
        prompt_engine=mock_prompt_engine
    )
    
    # Get service with injected intelligence mocks
    agent_service = container.get_agent_service()
    
    # Service uses mock implementations through interfaces
    result = await agent_service.process_query("test query", "test-session")
    
    # Verify all intelligence services were called through interfaces
    mock_memory_service.retrieve_context.assert_called_once()
    mock_planning_service.plan_response_strategy.assert_called_once()
    mock_prompt_engine.assemble_prompt.assert_called_once()
    mock_memory_service.consolidate_insights.assert_called_once()
    
    assert "Enhanced response" in result
```

### Enhanced Container Testing Patterns

```python
@pytest.fixture(autouse=True)
def reset_enhanced_container():
    """Reset enhanced DI container state between tests"""
    container = DIContainer()
    container.reset()
    yield
    container.reset()

def test_enhanced_container_health_monitoring():
    """Test enhanced container health check functionality"""
    # Initialize enhanced container
    container = DIContainer()
    
    # Check enhanced health status
    health = container.get_health_status()
    
    assert health["status"] in ["healthy", "degraded", "unhealthy"]
    assert "infrastructure" in health["components"]
    assert "intelligence" in health["components"]  # New intelligence section
    assert "services" in health["components"]
    
    # Check intelligence services specifically
    intelligence_health = health["components"]["intelligence"]
    assert "memory" in intelligence_health["services"]
    assert "planning" in intelligence_health["services"]
    assert "prompt_engine" in intelligence_health["services"]

def test_enhanced_container_graceful_degradation():
    """Test enhanced container fallback to minimal implementation"""
    # Set environment for testing mode
    os.environ["TESTING_MODE"] = "true"
    os.environ["ENABLE_INTELLIGENT_FEATURES"] = "false"
    
    container = DIContainer()
    
    # Should get mock intelligence services in testing mode
    assert isinstance(container.memory_service, MockMemoryService)
    assert isinstance(container.planning_service, MockPlanningService)
    assert isinstance(container.prompt_engine, MockPromptEngine)
```

## Enhanced Testing Different Architecture Layers

### 1. Enhanced API Layer Testing

```python
# Enhanced FastAPI endpoint testing with intelligence dependency injection
@pytest.mark.api
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_enhanced_query_endpoint(async_client, mock_intelligence_services):
    """Test enhanced query endpoint with mocked intelligence services"""
    
    # Mock the enhanced dependency injection
    with patch('faultmaven.api.v1.dependencies.get_enhanced_agent_service',
               return_value=mock_intelligence_services['agent_service']):
        
        response = await async_client.post(
            "/api/v1/agent/query",
            json={
                "query": "Server is down",
                "session_id": "test-session"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify enhanced response structure
        assert data["schema_version"] == "3.1.0"
        assert "view_state" in data
        assert "memory_context" in data["view_state"]
        assert "planning_state" in data["view_state"]
        
        # Verify intelligence features were used
        mock_intelligence_services['memory_service'].retrieve_context.assert_called_once()
        mock_intelligence_services['planning_service'].plan_response_strategy.assert_called_once()
        mock_intelligence_services['prompt_engine'].assemble_prompt.assert_called_once()
```

### 2. Enhanced Service Layer Testing

```python
# Enhanced service testing with intelligence integration
@pytest.mark.services
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_enhanced_agent_service_intelligence_workflow(mock_intelligence_services):
    """Test complete intelligence workflow in enhanced agent service"""
    
    agent_service = EnhancedAgentService(
        llm_provider=mock_intelligence_services['llm_provider'],
        tools=[],
        tracer=mock_intelligence_services['tracer'],
        sanitizer=mock_intelligence_services['sanitizer'],
        memory_service=mock_intelligence_services['memory_service'],
        planning_service=mock_intelligence_services['planning_service'],
        prompt_engine=mock_intelligence_services['prompt_engine']
    )
    
    # Test complete intelligence workflow
    request = QueryRequest(session_id="test_session", query="test query")
    result = await agent_service.process_query(request)
    
    # Verify memory context retrieval
    mock_intelligence_services['memory_service'].retrieve_context.assert_called_once_with(
        "test_session", "test query"
    )
    
    # Verify strategic planning
    mock_intelligence_services['planning_service'].plan_response_strategy.assert_called_once()
    
    # Verify enhanced prompting
    mock_intelligence_services['prompt_engine'].assemble_prompt.assert_called_once()
    
    # Verify memory consolidation
    mock_intelligence_services['memory_service'].consolidate_insights.assert_called_once()
    
    # Verify result structure
    assert result is not None
    assert hasattr(result, 'view_state')
    assert hasattr(result.view_state, 'memory_context')
    assert hasattr(result.view_state, 'planning_state')
```

### 3. Enhanced Core Domain Testing

```python
# Enhanced core domain testing with intelligence
@pytest.mark.core
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_memory_manager_hierarchical_storage(mock_storage_services):
    """Test hierarchical memory storage and retrieval"""
    
    memory_manager = MemoryManager(
        redis_store=mock_storage_services['redis'],
        vector_store=mock_storage_services['chromadb']
    )
    
    # Test working memory storage
    working_memory = ConversationContext(
        working_memory=["current_query"],
        semantic_context=[],
        user_profile=MockUserProfile()
    )
    
    await memory_manager.store_working_memory("test_session", working_memory)
    
    # Test retrieval with semantic search
    retrieved_context = await memory_manager.retrieve_context("test_session", "current query")
    
    assert retrieved_context is not None
    assert len(retrieved_context.working_memory) > 0
    assert "current_query" in retrieved_context.working_memory[0]

@pytest.mark.core
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_planning_engine_strategic_decomposition(mock_llm_provider):
    """Test strategic problem decomposition and planning"""
    
    planning_engine = PlanningEngine(llm_provider=mock_llm_provider)
    
    # Test problem decomposition
    problem = "Database connection timeout causing 500 errors"
    components = await planning_engine.decompose_problem(problem, {})
    
    assert components is not None
    assert len(components.sub_problems) > 0
    assert components.complexity_score > 0
    assert components.urgency_level in ["low", "medium", "high", "critical"]
    
    # Test strategy development
    strategy = await planning_engine.develop_strategy(components, {})
    
    assert strategy is not None
    assert strategy.current_phase == "analysis"
    assert len(strategy.plan_components) > 0
    assert strategy.risk_assessment is not None

@pytest.mark.core
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_advanced_prompt_engine_optimization(mock_llm_provider, mock_memory_service):
    """Test advanced prompt assembly and optimization"""
    
    prompt_engine = AdvancedPromptEngine(
        llm_provider=mock_llm_provider,
        memory_service=mock_memory_service
    )
    
    # Test multi-layer prompt assembly
    prompt = await prompt_engine.assemble_prompt(
        question="How do I fix this error?",
        response_type=ResponseType.ANSWER,
        context={"session_id": "test_session"}
    )
    
    assert prompt is not None
    assert "system" in prompt.lower()
    assert "context" in prompt.lower()
    assert "domain" in prompt.lower()
    assert "task" in prompt.lower()
    
    # Test prompt optimization
    optimized_prompt = await prompt_engine.optimize_prompt(prompt, {"session_id": "test_session"})
    
    assert optimized_prompt is not None
    assert len(optimized_prompt) <= len(prompt)  # Should be optimized
    
    # Test performance tracking
    metrics = await prompt_engine.get_performance_metrics()
    
    assert metrics is not None
    assert "response_time_ms" in metrics
    assert "quality_score" in metrics
    assert "optimization_level" in metrics
```

## Enhanced Integration Testing

### Intelligence Service Integration Testing

```python
# Enhanced integration testing with intelligence services
@pytest.mark.integration
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_complete_intelligence_workflow(enhanced_container):
    """Test complete intelligence workflow integration"""
    
    # Get enhanced services from container
    agent_service = enhanced_container.get_agent_service()
    memory_service = enhanced_container.get_memory_service()
    planning_service = enhanced_container.get_planning_service()
    prompt_engine = enhanced_container.get_prompt_engine()
    
    # Test complete workflow
    request = QueryRequest(session_id="integration_test", query="integration test query")
    
    # Process query with intelligence
    result = await agent_service.process_query(request)
    
    # Verify result structure
    assert result is not None
    assert result.schema_version == "3.1.0"
    assert result.view_state.memory_context is not None
    assert result.view_state.planning_state is not None
    
    # Verify memory was updated
    memory_context = await memory_service.retrieve_context("integration_test", "follow-up query")
    assert memory_context is not None
    assert len(memory_context.working_memory) > 0
    
    # Verify planning context was updated
    planning_context = await planning_service.get_current_context("integration_test")
    assert planning_context is not None
    assert planning_context.current_phase in ["analysis", "solution", "execution"]
    
    # Verify prompt engine was used
    prompt_metrics = await prompt_engine.get_performance_metrics()
    assert prompt_metrics is not None
    assert prompt_metrics["response_time_ms"] > 0
```

### Performance Testing for Intelligence Services

```python
# Performance testing for intelligence services
@pytest.mark.performance
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_memory_service_performance(enhanced_container):
    """Test memory service performance characteristics"""
    
    memory_service = enhanced_container.get_memory_service()
    
    # Test memory retrieval performance
    start_time = time.time()
    context = await memory_service.retrieve_context("perf_test", "performance test query")
    end_time = time.time()
    
    # Should complete within 50ms
    assert (end_time - start_time) < 0.05
    assert context is not None
    
    # Test memory consolidation performance
    start_time = time.time()
    success = await memory_service.consolidate_insights("perf_test", {"test": "data"})
    end_time = time.time()
    
    # Should complete within 100ms
    assert (end_time - start_time) < 0.1
    assert success is True

@pytest.mark.performance
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_planning_service_performance(enhanced_container):
    """Test planning service performance characteristics"""
    
    planning_service = enhanced_container.get_planning_service()
    
    # Test strategy planning performance
    start_time = time.time()
    strategy = await planning_service.plan_response_strategy("performance test", {})
    end_time = time.time()
    
    # Should complete within 100ms
    assert (end_time - start_time) < 0.1
    assert strategy is not None
    
    # Test problem decomposition performance
    start_time = time.time()
    components = await planning_service.decompose_problem("performance test problem", {})
    end_time = time.time()
    
    # Should complete within 150ms
    assert (end_time - start_time) < 0.15
    assert components is not None

@pytest.mark.performance
@pytest.mark.intelligence
@pytest.mark.asyncio
async def test_prompt_engine_performance(enhanced_container):
    """Test prompt engine performance characteristics"""
    
    prompt_engine = enhanced_container.get_prompt_engine()
    
    # Test prompt assembly performance
    start_time = time.time()
    prompt = await prompt_engine.assemble_prompt(
        question="performance test question",
        response_type=ResponseType.ANSWER,
        context={"session_id": "perf_test"}
    )
    end_time = time.time()
    
    # Should complete within 50ms
    assert (end_time - start_time) < 0.05
    assert prompt is not None
    
    # Test prompt optimization performance
    start_time = time.time()
    optimized_prompt = await prompt_engine.optimize_prompt(prompt, {"session_id": "perf_test"})
    end_time = time.time()
    
    # Should complete within 75ms
    assert (end_time - start_time) < 0.075
    assert optimized_prompt is not None
```

## Enhanced Mock Services and Fixtures

### Comprehensive Intelligence Service Mocks

```python
# Enhanced mock services for testing
class MockMemoryService(IMemoryService):
    """Comprehensive mock memory service for testing"""
    
    def __init__(self):
        self._contexts = {}
        self._insights = []
        self._user_profiles = {}
        self._performance_metrics = {
            "retrieval_time_ms": 25,
            "consolidation_time_ms": 50,
            "storage_usage_mb": 128
        }
    
    async def retrieve_context(self, session_id: str, query: str) -> ConversationContext:
        # Return mock context or create new one
        if session_id not in self._contexts:
            self._contexts[session_id] = ConversationContext(
                working_memory=[],
                semantic_context=[],
                user_profile=MockUserProfile(expertise_level="intermediate")
            )
        return self._contexts[session_id]
    
    async def consolidate_insights(self, session_id: str, result: dict) -> bool:
        self._insights.append({
            'session_id': session_id,
            'result': result,
            'timestamp': datetime.utcnow().isoformat()
        })
        return True
    
    async def get_user_profile(self, session_id: str) -> UserProfile:
        if session_id not in self._user_profiles:
            self._user_profiles[session_id] = MockUserProfile(expertise_level="intermediate")
        return self._user_profiles[session_id]
    
    def get_consolidated_insights(self) -> List[Dict]:
        return self._insights.copy()
    
    def set_mock_context(self, session_id: str, context: ConversationContext):
        """Set mock context for testing"""
        self._contexts[session_id] = context
    
    async def get_performance_metrics(self) -> Dict[str, Any]:
        return self._performance_metrics.copy()

class MockPlanningService(IPlanningService):
    """Comprehensive mock planning service for testing"""
    
    def __init__(self):
        self._strategies = {}
        self._current_contexts = {}
        self._performance_metrics = {
            "strategy_development_time_ms": 75,
            "problem_decomposition_time_ms": 100,
            "risk_assessment_time_ms": 50
        }
    
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
    
    async def decompose_problem(self, problem: str, context: dict) -> ProblemComponents:
        return ProblemComponents(
            sub_problems=["sub_problem_1", "sub_problem_2"],
            complexity_score=0.7,
            urgency_level="medium",
            risk_factors=["risk_1", "risk_2"]
        )
    
    def set_mock_context(self, session_id: str, context: PlanningContext):
        """Set mock planning context for testing"""
        self._current_contexts[session_id] = context
    
    async def get_performance_metrics(self) -> Dict[str, Any]:
        return self._performance_metrics.copy()

class MockPromptEngine(IPromptEngine):
    """Comprehensive mock prompt engine for testing"""
    
    def __init__(self):
        self._performance_metrics = {
            "assembly_time_ms": 30,
            "optimization_time_ms": 45,
            "quality_score": 0.95,
            "optimization_level": "high"
        }
    
    async def assemble_prompt(self, question: str, response_type: ResponseType, context: dict) -> str:
        return f"Mock enhanced prompt for {response_type}: {question}"
    
    async def optimize_prompt(self, prompt: str, context: dict) -> str:
        return f"Optimized: {prompt}"
    
    async def get_performance_metrics(self) -> Dict[str, Any]:
        return self._performance_metrics.copy()
    
    async def get_quality_metrics(self) -> Dict[str, Any]:
        return {
            "response_quality": 0.95,
            "user_satisfaction": 0.9,
            "optimization_effectiveness": 0.85
        }
```

### Enhanced Test Fixtures

```python
# Enhanced test fixtures with intelligence services
@pytest.fixture
def mock_intelligence_services():
    """Provide comprehensive mock intelligence services"""
    return {
        'memory_service': MockMemoryService(),
        'planning_service': MockPlanningService(),
        'prompt_engine': MockPromptEngine(),
        'llm_provider': Mock(spec=ILLMProvider),
        'tracer': Mock(spec=ITracer),
        'sanitizer': Mock(spec=ISanitizer),
        'agent_service': None  # Will be created with mocks
    }

@pytest.fixture
def enhanced_container():
    """Provide enhanced container with intelligence services"""
    container = DIContainer()
    container.reset()
    
    # Enable intelligence features
    os.environ['ENABLE_INTELLIGENT_FEATURES'] = 'true'
    os.environ['ENABLE_MEMORY_FEATURES'] = 'true'
    os.environ['ENABLE_PLANNING_FEATURES'] = 'true'
    os.environ['ENABLE_ADVANCED_PROMPTING'] = 'true'
    
    container.initialize()
    return container

@pytest.fixture
def mock_storage_services():
    """Provide mock storage services for testing"""
    return {
        'redis': Mock(spec=ISessionStore),
        'chromadb': Mock(spec=IVectorStore)
    }
```

## Enhanced Test Execution and Reporting

### Running Enhanced Test Suites

```bash
# Run all enhanced tests
pytest --verbose

# Run intelligence tests with coverage
pytest -m intelligence --cov=faultmaven.services.intelligence --cov-report=html

# Run performance tests
pytest -m performance --durations=10

# Run specific intelligence service tests
pytest tests/services/test_intelligence_services.py -v

# Run memory service tests
pytest -m memory --tb=short

# Run planning service tests
pytest -m planning --tb=short

# Run prompt engine tests
pytest -m prompting --tb=short
```

### Enhanced Coverage Reporting

```bash
# Generate comprehensive coverage report
pytest --cov=faultmaven --cov-report=html --cov-report=term-missing

# Generate intelligence service specific coverage
pytest --cov=faultmaven.services.memory_service \
       --cov=faultmaven.services.planning_service \
       --cov=faultmaven.services.prompt_engine \
       --cov-report=html \
       --cov-report=term-missing

# Generate coverage for new intelligence interfaces
pytest --cov=faultmaven.models.interfaces \
       --cov-report=html \
       --cov-report=term-missing
```

## Enhanced Test Data Management

### Intelligence Service Test Data

```python
# Enhanced test data for intelligence services
@pytest.fixture
def sample_conversation_context():
    """Provide sample conversation context for testing"""
    return ConversationContext(
        working_memory=[
            "User asked about database connection issues",
            "Previous solution involved checking connection pool",
            "User reported intermittent timeouts"
        ],
        semantic_context=[
            {
                "content": "Database connection pool configuration",
                "relevance": 0.95,
                "source": "knowledge_base"
            },
            {
                "content": "Connection timeout troubleshooting",
                "relevance": 0.88,
                "source": "log_analysis"
            }
        ],
        user_profile=UserProfile(
            expertise_level="intermediate",
            preferred_communication_style="technical",
            previous_successful_solutions=["connection_pool_tuning"]
        )
    )

@pytest.fixture
def sample_strategic_plan():
    """Provide sample strategic plan for testing"""
    return StrategicPlan(
        id="test_plan_123",
        current_phase="analysis",
        plan_components=[
            PlanComponent(
                phase="analysis",
                actions=["analyze_connection_pool", "check_timeout_settings"],
                estimated_duration_minutes=15,
                success_criteria=["identify_root_cause"]
            ),
            PlanComponent(
                phase="solution",
                actions=["adjust_pool_size", "modify_timeout_values"],
                estimated_duration_minutes=30,
                success_criteria=["connection_stability_improved"]
            )
        ],
        risk_assessment=RiskAssessment(
            risk_level="medium",
            potential_issues=["service_restart_required", "temporary_downtime"],
            mitigation_strategies=["backup_configuration", "gradual_rollout"]
        )
    )

@pytest.fixture
def sample_enhanced_prompt():
    """Provide sample enhanced prompt for testing"""
    return {
        "system_layer": "You are an expert SRE troubleshooting assistant",
        "context_layer": "User has intermediate expertise, prefers technical solutions",
        "domain_layer": "Database infrastructure and connection management",
        "task_layer": "Analyze and resolve connection timeout issues",
        "safety_layer": "Ensure no destructive actions without confirmation",
        "adaptation_layer": "Adapt communication style to user expertise level"
    }
```

## Conclusion

This enhanced testing guide provides comprehensive coverage for FaultMaven's new intelligent communication capabilities while maintaining all the benefits of the existing interface-based testing architecture. The new memory management, strategic planning, and advanced prompting systems are thoroughly tested with comprehensive mocking, performance testing, and integration testing.

**Key Testing Benefits**:
- **Comprehensive Intelligence Testing**: All intelligence services thoroughly tested
- **Performance Validation**: Performance characteristics validated for intelligence services
- **Integration Testing**: Complete intelligence workflows tested end-to-end
- **Mock Services**: Comprehensive mock implementations for all intelligence services
- **Enhanced Coverage**: Increased test coverage for new architectural components

**Next Steps**:
1. **Implement Enhanced Tests**: Create the actual enhanced test implementations
2. **Add Performance Tests**: Implement performance testing for intelligence services
3. **Expand Integration Tests**: Add more comprehensive integration test scenarios
4. **Monitor Test Coverage**: Track coverage improvements for intelligence services

This enhanced testing strategy positions FaultMaven for reliable intelligent operation while maintaining the high quality standards of the existing testing architecture.