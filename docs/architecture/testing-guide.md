# FaultMaven Testing Guide

**Document Type**: Testing Guide  
**Last Updated**: January 2025  
**Status**: Production Ready

## Overview

This comprehensive testing guide covers FaultMaven's interface-based testing architecture, built around clean architecture principles and dependency injection. With 341 passing tests and 71% coverage, our testing strategy ensures reliability across all architectural layers.

## Testing Architecture

### Test Organization by Layer

FaultMaven's tests are organized to match the clean architecture structure:

```
tests/
├── api/                    # API Layer Testing
│   ├── test_data_ingestion.py      # Data upload and processing endpoints
│   ├── test_kb_management.py       # Knowledge base management endpoints  
│   ├── test_query_processing.py    # Troubleshooting query endpoints
│   └── test_sessions.py            # Session lifecycle endpoints
├── services/               # Service Layer Testing
│   ├── test_agent_service.py       # AgentService orchestration
│   ├── test_data_service.py        # DataService operations
│   └── test_knowledge_service.py   # KnowledgeService operations
├── core/                   # Core Domain Testing
│   ├── test_classifier.py          # Data classification logic
│   ├── test_core_agent.py          # Core agent functionality
│   ├── test_core_agent_errors.py   # Agent error handling
│   ├── test_doctrine.py            # SRE troubleshooting doctrine
│   ├── test_ingestion.py           # Knowledge ingestion
│   ├── test_log_processor.py       # Log analysis logic
│   └── tools/                      # Agent Tools Testing
│       ├── test_knowledge_base.py  # Knowledge base tool
│       └── test_web_search.py      # Web search tool
├── infrastructure/         # Infrastructure Layer Testing  
│   ├── test_opik_initialization_fix.py  # Observability tracing
│   ├── test_redaction.py          # Data sanitization
│   ├── test_redaction_errors.py   # Sanitization error handling
│   └── test_router.py              # LLM provider routing
├── unit/                   # Architecture Component Testing
│   ├── test_container.py           # DI container functionality
│   ├── test_dependency_injection.py # DI patterns and resolution
│   ├── test_feature_flags.py       # Feature flag management
│   ├── test_interface_compliance.py # Interface implementation validation
│   ├── test_interface_implementations.py # Interface contract testing
│   └── test_interfaces.py          # Interface definition testing
├── integration/            # End-to-End Testing
│   ├── conftest.py                 # Integration test fixtures
│   ├── mock_servers.py             # Mock API servers
│   └── test_*.py                   # Workflow integration tests
└── conftest.py             # Global test configuration and fixtures
```

## Test Categories and Markers

### Pytest Markers

Tests are categorized using pytest markers for selective execution:

```python
# Test marker definitions (pytest.ini)
[tool:pytest]
markers =
    unit: Unit tests for isolated components
    integration: Integration tests requiring external services
    security: Security and PII redaction tests  
    api: API endpoint and request/response tests
    agent: AI agent and LangGraph workflow tests
    data_processing: Data classification and log processing tests
    llm: LLM provider routing and fallback tests
    session: Session management and lifecycle tests
```

### Running Tests by Category

```bash
# All tests (341 passing)
pytest

# Unit tests only (fastest)
pytest -m unit

# Integration tests (requires Docker services)
pytest -m integration  

# Security and privacy tests
pytest -m security

# API endpoint tests
pytest -m api

# AI agent functionality tests  
pytest -m agent

# LLM provider and routing tests
pytest -m llm

# Data processing pipeline tests
pytest -m data_processing

# Session management tests
pytest -m session
```

## Interface-Based Testing Strategy

### Dependency Injection Testing

FaultMaven's interface-based architecture enables comprehensive mocking:

```python
# Test with interface mocks
@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider implementing ILLMProvider interface"""
    mock = MagicMock(spec=ILLMProvider)
    mock.generate.return_value = LLMResponse(
        content="Mocked response",
        confidence=0.9,
        provider="mock",
        model="mock-model"
    )
    mock.is_available.return_value = True
    return mock

@patch.object(DIContainer, 'get_llm_provider')
def test_agent_service_with_mock_llm(mock_get_llm, mock_llm_provider):
    """Test AgentService with mocked LLM provider"""
    mock_get_llm.return_value = mock_llm_provider
    
    # Get service with injected mock
    agent_service = container.get_agent_service()
    
    # Service uses mock implementation through interface
    result = await agent_service.process_query("test query", "test-session")
    
    # Verify mock was called through interface
    mock_llm_provider.generate.assert_called_once()
    assert "Mocked response" in result
```

### Container Testing Patterns

```python
@pytest.fixture(autouse=True)
def reset_container():
    """Reset DI container state between tests"""
    container = DIContainer()
    container.reset()
    yield
    container.reset()

def test_container_health_monitoring():
    """Test container health check functionality"""
    # Initialize container
    container = DIContainer()
    
    # Check health status
    health = container.health_check()
    
    assert health["status"] in ["healthy", "degraded", "not_initialized"]
    assert "llm_provider" in health["components"]
    assert "agent_service" in health["components"]

def test_container_graceful_degradation():
    """Test container fallback to minimal implementation"""
    # Set environment for testing mode
    os.environ["TESTING_MODE"] = "true"
    os.environ["USE_MOCK_LLM"] = "true"
    
    container = DIContainer()
    llm_provider = container.get_llm_provider()
    
    # Should get mock implementation in testing mode
    assert isinstance(llm_provider, MockLLMProvider)
```

## Testing Different Architecture Layers

### 1. API Layer Testing

```python
# FastAPI endpoint testing with dependency injection
@pytest.mark.api
@pytest.mark.asyncio
async def test_troubleshoot_endpoint(async_client, mock_agent_service):
    """Test troubleshooting endpoint with mocked service"""
    
    # Mock the dependency injection
    with patch('faultmaven.api.v1.dependencies.get_agent_service',
               return_value=mock_agent_service):
        
        response = await async_client.post(
            "/api/v1/agent/troubleshoot",
            json={
                "query": "Server is down",
                "session_id": "test-session"
            }
        )
        
    assert response.status_code == 200
    result = response.json()
    assert "reasoning" in result
    assert "recommendations" in result
    
    # Verify service was called
    mock_agent_service.process_troubleshooting_query.assert_called_once()
```

### 2. Service Layer Testing

```python
# Service orchestration testing with interface mocks
@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_service_orchestration():
    """Test AgentService orchestrates all dependencies correctly"""
    
    # Create interface mocks
    mock_llm = MagicMock(spec=ILLMProvider)
    mock_sanitizer = MagicMock(spec=ISanitizer)  
    mock_tracer = MagicMock(spec=ITracer)
    mock_tools = [MagicMock(spec=BaseTool)]
    
    # Set up mock responses
    mock_sanitizer.sanitize.return_value = "sanitized query"
    mock_llm.generate.return_value = LLMResponse(
        content="Analysis complete", confidence=0.9
    )
    mock_tools[0].execute.return_value = ToolResult(
        success=True, content="Knowledge base results"
    )
    
    # Create service with injected mocks
    agent_service = AgentService(
        llm_provider=mock_llm,
        tools=mock_tools,
        tracer=mock_tracer,
        sanitizer=mock_sanitizer
    )
    
    # Execute workflow
    result = await agent_service.process_troubleshooting_query(
        "Test issue", "test-session"
    )
    
    # Verify orchestration flow
    mock_sanitizer.sanitize.assert_called_once()
    mock_tracer.start_trace.assert_called_once()
    mock_tools[0].execute.assert_called_once()
    mock_llm.generate.assert_called_once()
    
    assert result.status == "completed"
```

### 3. Core Domain Testing

```python
# Business logic testing with minimal dependencies
@pytest.mark.unit
def test_data_classifier_business_logic():
    """Test data classification without external dependencies"""
    classifier = DataClassifier()
    
    # Test different data types
    log_data = "2024-01-15 12:00:00 ERROR Database connection failed"
    metric_data = "cpu_usage:85% memory_usage:70%"
    config_data = "server.port=8080\ndb.host=localhost"
    
    assert classifier.classify(log_data) == DataType.SYSTEM_LOGS
    assert classifier.classify(metric_data) == DataType.METRICS
    assert classifier.classify(config_data) == DataType.CONFIGURATION

@pytest.mark.agent
@pytest.mark.asyncio
async def test_troubleshooting_doctrine():
    """Test 5-phase SRE troubleshooting doctrine"""
    doctrine = TroubleshootingDoctrine()
    
    # Test phase progression
    state = AgentState(query="Server outage")
    
    # Phase 1: Define Blast Radius
    state = await doctrine.define_blast_radius(state)
    assert state.current_phase == Phase.BLAST_RADIUS
    assert state.blast_radius is not None
    
    # Phase 2: Establish Timeline
    state = await doctrine.establish_timeline(state)
    assert state.current_phase == Phase.TIMELINE
    assert state.timeline is not None
    
    # Continue through all phases
    assert len(state.phase_history) == 2
```

### 4. Infrastructure Layer Testing

```python
# Infrastructure interface implementation testing
@pytest.mark.infrastructure
@pytest.mark.asyncio
async def test_llm_router_fallback_chain():
    """Test LLM router fallback behavior"""
    
    # Mock multiple providers with different availability
    mock_primary = MagicMock(spec=ILLMProvider)
    mock_primary.generate.side_effect = Exception("Provider unavailable")
    
    mock_fallback = MagicMock(spec=ILLMProvider)
    mock_fallback.generate.return_value = LLMResponse(
        content="Fallback response", confidence=0.8, provider="fallback"
    )
    
    # Create router with mock registry
    router = LLMRouter()
    router._providers = {
        "primary": mock_primary,
        "fallback": mock_fallback
    }
    router._fallback_chain = ["primary", "fallback"]
    
    # Test fallback behavior
    response = await router.generate("test prompt")
    
    assert response.provider == "fallback"
    assert response.content == "Fallback response"
    
    # Verify fallback was attempted
    mock_primary.generate.assert_called_once()
    mock_fallback.generate.assert_called_once()

@pytest.mark.security
@pytest.mark.asyncio
async def test_pii_redaction_interface():
    """Test PII redaction through ISanitizer interface"""
    
    # Test data with PII
    sensitive_text = "Error in user john.doe@company.com session 123-45-6789"
    
    # Test with Presidio implementation
    presidio_sanitizer = DataSanitizer()  # Implements ISanitizer
    clean_text = await presidio_sanitizer.sanitize(sensitive_text)
    
    assert "john.doe@company.com" not in clean_text
    assert "123-45-6789" not in clean_text
    assert "Error in user" in clean_text  # Context preserved
    
    # Test interface compliance
    assert isinstance(presidio_sanitizer, ISanitizer)
    assert hasattr(presidio_sanitizer, 'sanitize')
```

## Mock Infrastructure Testing

### Mock API Servers

FaultMaven includes sophisticated mock servers for testing without external dependencies:

```python
# Mock LLM provider server
class MockLLMServer:
    """OpenAI-compatible mock LLM server"""
    
    def __init__(self):
        self.app = Flask(__name__)
        self.setup_routes()
        
    def setup_routes(self):
        @self.app.route('/chat/completions', methods=['POST'])
        def chat_completions():
            request_data = request.json
            
            # Context-aware mock responses
            if "troubleshooting" in request_data.get("messages", [{}])[-1].get("content", "").lower():
                content = "Based on the symptoms, this appears to be a database connectivity issue..."
            else:
                content = "Mock LLM response for general query"
                
            return jsonify({
                "choices": [{
                    "message": {"content": content},
                    "finish_reason": "stop"
                }],
                "usage": {"total_tokens": 150}
            })

# Usage in tests
@pytest.fixture(scope="session")
def mock_llm_server():
    """Start mock LLM server for integration tests"""
    server = MockLLMServer()
    thread = threading.Thread(
        target=lambda: server.app.run(host='localhost', port=5001, debug=False)
    )
    thread.daemon = True
    thread.start()
    
    # Wait for server to start
    time.sleep(1)
    
    yield f"http://localhost:5001"
```

### Integration Test Patterns

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_end_to_end_troubleshooting_workflow(mock_llm_server):
    """Test complete troubleshooting workflow with mock infrastructure"""
    
    # Configure for integration testing
    os.environ["OPENAI_API_BASE"] = mock_llm_server
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["CHAT_PROVIDER"] = "openai"
    
    # Reset container to pick up new configuration
    container = DIContainer()
    container.reset()
    
    # Get agent service with real dependencies (but mock LLM)
    agent_service = container.get_agent_service()
    
    # Execute full workflow
    result = await agent_service.process_troubleshooting_query(
        "Our web application is responding slowly and users are complaining",
        "integration-test-session"
    )
    
    # Verify workflow completion
    assert result.status == "completed"
    assert "analysis" in result.data
    assert "recommendations" in result.data
    assert len(result.data["reasoning_steps"]) == 5  # 5-phase doctrine
    
    # Verify tracing occurred
    traces = get_test_traces("integration-test-session")
    assert len(traces) > 0
    assert any("llm_call" in trace.operation for trace in traces)
```

## Testing Best Practices

### 1. Interface Compliance Testing

```python
def test_interface_compliance():
    """Ensure all providers implement required interfaces"""
    
    # Test all LLM providers implement ILLMProvider
    registry = get_registry()
    for provider_name in registry.get_all_provider_names():
        provider = registry.get_provider(provider_name)
        if provider:
            assert isinstance(provider, ILLMProvider)
            assert hasattr(provider, 'generate')
            assert hasattr(provider, 'is_available')
            assert hasattr(provider, 'get_supported_models')

def test_tool_interface_compliance():
    """Ensure all tools implement BaseTool interface"""
    tools = container.get_tools()
    
    for tool in tools:
        assert isinstance(tool, BaseTool)
        assert hasattr(tool, 'execute')
        assert hasattr(tool, 'get_schema')
        
        # Test schema format
        schema = tool.get_schema()
        assert isinstance(schema, dict)
        assert "name" in schema
        assert "description" in schema
```

### 2. Error Handling Testing

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_graceful_error_handling():
    """Test system behavior when components fail"""
    
    # Test LLM provider failure
    mock_llm = MagicMock(spec=ILLMProvider)
    mock_llm.generate.side_effect = Exception("LLM provider down")
    mock_llm.is_available.return_value = False
    
    with patch.object(DIContainer, 'get_llm_provider', return_value=mock_llm):
        agent_service = container.get_agent_service()
        
        # Should handle failure gracefully
        result = await agent_service.process_query("test", "session")
        
        assert result.status == "error"
        assert "LLM provider unavailable" in result.error_message

@pytest.mark.unit
def test_container_partial_failure():
    """Test container behavior when some components fail"""
    
    # Simulate ChromaDB unavailable
    with patch('faultmaven.infrastructure.persistence.chromadb.ChromaDBClient.__init__',
               side_effect=Exception("ChromaDB connection failed")):
        
        container = DIContainer()
        health = container.health_check()
        
        # Container should be degraded but functional
        assert health["status"] == "degraded"
        assert not health["components"]["knowledge_service"]
        
        # Other services should still work
        agent_service = container.get_agent_service()
        assert agent_service is not None
```

### 3. Performance Testing

```python
@pytest.mark.performance
@pytest.mark.asyncio
async def test_provider_routing_performance():
    """Test LLM provider routing performance"""
    import time
    
    # Measure provider selection time
    start_time = time.time()
    
    registry = get_registry()
    provider = registry.get_provider("fireworks")
    
    selection_time = time.time() - start_time
    
    # Provider selection should be fast (< 1ms)
    assert selection_time < 0.001
    
    # Measure fallback chain execution
    start_time = time.time()
    
    # Simulate primary provider failure
    with patch.object(provider, 'generate', side_effect=Exception("Failed")):
        try:
            await registry.route_request("test prompt")
        except Exception:
            pass  # Expected when all providers fail
            
    fallback_time = time.time() - start_time
    
    # Fallback execution should be reasonable (< 5s)
    assert fallback_time < 5.0

@pytest.mark.performance 
def test_container_initialization_time():
    """Test DI container initialization performance"""
    import time
    
    # Reset container
    container = DIContainer()
    container.reset()
    
    # Measure initialization time
    start_time = time.time()
    container.initialize()
    init_time = time.time() - start_time
    
    # Container should initialize quickly (< 2s)
    assert init_time < 2.0
    
    # Subsequent access should be fast (cached)
    start_time = time.time()
    agent_service = container.get_agent_service()
    access_time = time.time() - start_time
    
    assert access_time < 0.01  # < 10ms for cached access
```

## Continuous Integration Testing

### GitHub Actions Integration

```yaml
# .github/workflows/tests.yml
name: Test Suite

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    services:
      redis:
        image: redis:latest
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 6379:6379
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v3
      with:
        python-version: '3.11'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
        pip install -r requirements-test.txt
        python -m spacy download en_core_web_lg
    
    - name: Run unit tests
      run: pytest -m unit --cov=faultmaven --cov-report=xml
    
    - name: Run integration tests
      env:
        USE_MOCK_LLM: true
        TESTING_MODE: true
      run: pytest -m integration
    
    - name: Run security tests  
      run: pytest -m security
    
    - name: Upload coverage
      uses: codecov/codecov-action@v3
```

### Test Coverage Requirements

```bash
# Coverage configuration (.coveragerc)
[run]
source = faultmaven
omit = 
    */tests/*
    */venv/*
    */migrations/*
    
[report]
exclude_lines =
    pragma: no cover
    def __repr__
    raise AssertionError
    raise NotImplementedError
    
# Minimum coverage thresholds
fail_under = 70
show_missing = True
```

## Advanced Testing Scenarios

### Multi-Provider Testing

```python
@pytest.mark.llm
@pytest.mark.asyncio
async def test_multi_provider_consistency():
    """Test consistent behavior across different LLM providers"""
    
    providers = ["fireworks", "openai", "anthropic"]
    test_prompt = "Explain the concept of microservices"
    results = {}
    
    for provider_name in providers:
        provider = registry.get_provider(provider_name)
        if provider and provider.is_available():
            try:
                response = await provider.generate(test_prompt)
                results[provider_name] = response
            except Exception as e:
                results[provider_name] = f"Error: {e}"
    
    # Verify all providers returned responses
    successful_responses = [
        r for r in results.values() 
        if isinstance(r, LLMResponse)
    ]
    
    assert len(successful_responses) > 0, "At least one provider should work"
    
    # Check response consistency  
    for response in successful_responses:
        assert len(response.content) > 50  # Substantial response
        assert response.confidence > 0.5   # Reasonable confidence
```

### Load Testing

```python
@pytest.mark.load
@pytest.mark.asyncio
async def test_concurrent_requests():
    """Test system behavior under concurrent load"""
    import asyncio
    
    async def make_request(session_id):
        agent_service = container.get_agent_service()
        return await agent_service.process_query(
            f"Test query from session {session_id}",
            f"load-test-{session_id}"
        )
    
    # Create 10 concurrent requests
    tasks = [make_request(i) for i in range(10)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Check results
    successful_results = [r for r in results if not isinstance(r, Exception)]
    failed_results = [r for r in results if isinstance(r, Exception)]
    
    # Should handle concurrent load gracefully
    assert len(successful_results) >= 8  # At least 80% success rate
    assert len(failed_results) <= 2      # No more than 20% failures
```

## Debugging and Troubleshooting Tests

### Test Debugging Tools

```python
# Debug utilities for test development
def debug_container_state():
    """Debug helper to inspect container state"""
    health = container.health_check()
    print(f"\nContainer Status: {health['status']}")
    print("Component Status:")
    
    for component, status in health['components'].items():
        icon = "✅" if status else "❌"
        print(f"  {icon} {component}: {status}")
    
    if hasattr(container, 'llm_provider'):
        llm_provider = container.get_llm_provider()
        if hasattr(llm_provider, 'get_provider_status'):
            print("\nLLM Provider Status:")
            status = llm_provider.get_provider_status()
            for name, info in status.items():
                available = "✅" if info['available'] else "❌"
                print(f"  {available} {name}: {info}")

def debug_test_environment():
    """Debug helper to check test environment"""
    print("\nTest Environment:")
    print(f"  TESTING_MODE: {os.getenv('TESTING_MODE', 'false')}")
    print(f"  USE_MOCK_LLM: {os.getenv('USE_MOCK_LLM', 'false')}")
    print(f"  CHAT_PROVIDER: {os.getenv('CHAT_PROVIDER', 'local')}")
    
    # Check service availability
    try:
        import redis
        r = redis.Redis(host='localhost', port=6379)
        r.ping()
        print("  ✅ Redis: Available")
    except:
        print("  ❌ Redis: Unavailable")
```

### Common Test Debugging

```bash
# Run tests with debug output
pytest -v -s tests/unit/test_container.py::test_container_health_check

# Run specific test with full output
pytest --tb=long -v tests/services/test_agent_service.py::test_agent_service_orchestration

# Run with coverage and HTML report
pytest --cov=faultmaven --cov-report=html tests/unit/

# Debug failing integration tests
pytest -v -s --log-cli-level=DEBUG tests/integration/

# Check interface compliance
pytest -v tests/unit/test_interface_compliance.py
```

## Summary

FaultMaven's testing architecture provides:

1. **Complete Coverage**: 341 tests across all architectural layers
2. **Interface-Based Mocking**: Clean separation enabling comprehensive unit testing
3. **Integration Testing**: End-to-end workflow validation with mock infrastructure
4. **Performance Testing**: Load testing and performance benchmarking
5. **Security Testing**: PII redaction and data sanitization validation
6. **CI/CD Integration**: Automated testing in GitHub Actions
7. **Health Monitoring**: Container and component health validation
8. **Multi-Provider Testing**: LLM provider routing and fallback validation

The interface-based architecture ensures that all components can be thoroughly tested in isolation while still validating the complete system integration through comprehensive end-to-end tests.