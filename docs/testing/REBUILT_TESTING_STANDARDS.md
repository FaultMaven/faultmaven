# **FaultMaven Rebuilt Testing Standards**

## **Overview**

This document establishes the new testing architecture and standards for FaultMaven following the comprehensive test suite audit and logging integration rebuild. These standards replace the previous over-mocked, brittle testing patterns with a robust, maintainable approach.

---

## **Core Testing Principles**

### **1. Minimal Mocking Philosophy**
- **Mock only external boundaries**: APIs, databases, file systems, network calls
- **Use real interactions**: Service-to-service, internal component communication
- **Prefer lightweight test doubles** over complex mock configurations
- **Mock behavior, not implementation details**

### **2. Clear Test Categories**

#### **Unit Tests** (`tests/unit/`)
- Test single functions/methods in isolation
- Mock all dependencies except value objects
- Fast execution (< 1ms per test)
- Focus on business logic and edge cases

#### **Integration Tests** (`tests/integration/`)
- Test component interactions with real behavior
- Mock only truly external systems
- Verify cross-layer communication
- Test error propagation and coordination

#### **API Tests** (`tests/api/`)
- Test HTTP endpoints with real service layer
- Mock only infrastructure dependencies
- Verify request/response contracts
- Test authentication and validation

#### **End-to-End Tests** (`tests/e2e/`)
- Full system workflow testing
- Minimal mocking of external systems
- Real database/cache integration where feasible
- User journey validation

---

## **Testing Architecture Standards**

### **Test Structure**

```python
# Good: Clear, focused test structure
class TestServiceIntegration:
    """Test service behavior with real dependencies."""
    
    @pytest.fixture
    def service_with_real_deps(self):
        """Service with real internal dependencies."""
        return ServiceClass(
            real_dependency=RealDependency(),
            external_client=MockExternalClient()  # Only mock external
        )
    
    async def test_business_operation(self, service_with_real_deps):
        """Test business operation with real internal flow."""
        # Arrange - real data, minimal setup
        request = BusinessRequest(valid_data)
        
        # Act - call actual service method
        result = await service_with_real_deps.process(request)
        
        # Assert - verify behavior, not implementation
        assert result.status == "success"
        assert result.data.processed_correctly()
```

### **Fixture Guidelines**

#### **Lightweight Test Infrastructure**
```python
@pytest.fixture
def log_capture():
    """Real log capture without mocking logging framework."""
    return LogCapture()  # Custom test utility

@pytest.fixture
def test_service():
    """Service with real dependencies except external systems."""
    return ServiceClass(
        llm_client=MockLLMClient(),    # External API
        database=TestDatabase(),       # Lightweight test DB
        processor=RealProcessor()      # Real internal component
    )
```

#### **Avoid Complex Mock Hierarchies**
```python
# Bad: Complex mock setup
@pytest.fixture
def over_mocked_service():
    with patch('module.ComponentA') as mock_a:
        with patch('module.ComponentB') as mock_b:
            with patch('module.ComponentC') as mock_c:
                mock_a.return_value.method.return_value = Mock()
                mock_b.side_effect = lambda x: Mock()
                # ... 20 more lines of mock configuration
                yield ServiceClass(mock_a, mock_b, mock_c)

# Good: Minimal external mocking
@pytest.fixture
def service_with_test_doubles():
    return ServiceClass(
        external_api=MockExternalAPI(),  # Only external system
        internal_deps=create_real_dependencies()  # Real components
    )
```

---

## **Logging Integration Testing Standards**

### **Real Log Verification**
```python
def test_operation_logging(logging_setup, service):
    """Test actual log output, not mock calls."""
    log_capture = logging_setup
    
    # Execute operation
    result = service.process_data(test_data)
    
    # Verify real log content
    log_capture.assert_logged(
        message_contains="operation_completed",
        level=logging.INFO,
        min_count=1
    )
    
    # Verify log structure
    business_logs = log_capture.get_logs(message_contains="business_event")
    assert all("correlation_id" in log.getMessage() for log in business_logs)
```

### **Cross-Layer Coordination Testing**
```python
async def test_request_lifecycle_coordination(
    logging_setup, coordinator, service, external_client
):
    """Test coordination across layers with real log output."""
    log_capture = logging_setup
    
    with coordinator.start_request() as ctx:
        correlation_id = ctx.correlation_id
        
        # Execute multi-layer operation
        result = await service.process_with_external_call(data)
        
        # Verify coordination
        correlation_logs = log_capture.get_logs(
            message_contains=correlation_id
        )
        assert len(correlation_logs) >= 4  # All layers logged
```

---

## **Service Layer Testing Standards**

### **Real Business Logic Testing**
```python
class TestAgentService:
    """Test agent service with real AI processing flow."""
    
    async def test_troubleshooting_workflow(self, agent_service):
        """Test complete troubleshooting workflow."""
        # Use real service with mocked LLM only
        request = TroubleshootingRequest(
            query="Database timeout errors",
            context={"environment": "production"}
        )
        
        # Execute real business logic
        response = await agent_service.process_query(request)
        
        # Verify business outcomes
        assert response.findings
        assert response.confidence_score > 0.5
        assert response.recommendations
```

### **Error Handling Integration**
```python
async def test_error_propagation(self, service):
    """Test real error handling without mocking exceptions."""
    # Configure external dependency to fail
    service.external_client.configure_failure_mode()
    
    with pytest.raises(ServiceException) as exc_info:
        await service.process_data(valid_data)
    
    # Verify proper error wrapping and context
    assert "External service unavailable" in str(exc_info.value)
    assert exc_info.value.correlation_id
```

---

## **Infrastructure Testing Standards**

### **External Client Testing**
```python
class TestExternalClientIntegration:
    """Test external clients with real retry/timeout logic."""
    
    async def test_retry_behavior(self, external_client):
        """Test actual retry logic with simulated failures."""
        call_count = 0
        
        def failing_then_succeeding(*args):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("Simulated failure")
            return {"success": True}
        
        # Test real retry logic
        external_client.api_call = failing_then_succeeding
        result = await external_client.call_with_retries(data, retries=3)
        
        assert result["success"]
        assert call_count == 3  # Verified actual retry behavior
```

---

## **Performance Testing Integration**

### **Real Performance Measurement**
```python
@pytest.mark.performance
async def test_operation_performance(self, service):
    """Test actual operation performance."""
    import time
    
    start_time = time.time()
    
    results = await asyncio.gather(*[
        service.process_data(f"test_{i}") 
        for i in range(100)
    ])
    
    duration = time.time() - start_time
    
    # Verify performance requirements
    assert len(results) == 100
    assert duration < 5.0  # 100 operations in under 5 seconds
    assert all(result.success for result in results)
```

---

## **Test Quality Standards**

### **Assertion Quality**
```python
# Bad: Implementation-focused assertions
mock_service.process.assert_called_once_with(data)
assert mock_logger.info.call_count == 3

# Good: Behavior-focused assertions  
assert result.status == "completed"
assert result.findings[0].confidence > 0.8
assert log_capture.has_business_event("operation_completed")
```

### **Test Naming Standards**
```python
# Good test names explain business value
def test_agent_produces_accurate_recommendations_for_database_errors():
def test_request_correlation_maintained_across_service_boundaries():
def test_external_api_failures_retry_with_exponential_backoff():

# Avoid implementation-focused names
def test_mock_called_with_correct_parameters():
def test_service_calls_dependency_method():
```

### **Test Documentation**
```python
def test_complex_business_scenario(self):
    """
    Test complete user troubleshooting workflow.
    
    Verifies that when a user submits a query about database issues:
    1. Query is processed through AI reasoning
    2. Knowledge base is consulted for similar issues  
    3. External monitoring is checked for system status
    4. Recommendations are generated with confidence scores
    5. Results are returned with proper correlation tracking
    
    This test uses real service integration with mocked external APIs only.
    """
```

---

## **Anti-Patterns to Avoid**

### **1. Over-Mocking Internal Components**
```python
# Bad: Mocking everything
with patch('service.component_a') as mock_a:
    with patch('service.component_b') as mock_b:
        with patch('service.logger') as mock_log:
            # Test becomes meaningless
            
# Good: Mock only boundaries
external_api_mock = Mock()
service = ServiceClass(external_api=external_api_mock)
result = service.real_business_logic(data)
```

### **2. Testing Mock Configurations**
```python
# Bad: Testing the test setup
def test_mock_configuration():
    mock.configure_mock(return_value="test")
    assert mock.return_value == "test"  # Meaningless
    
# Good: Testing business behavior
def test_business_logic_handles_external_failures():
    service.external_client.will_fail = True
    result = service.process(data)
    assert result.status == "fallback_success"
```

### **3. Brittle Implementation Testing**
```python
# Bad: Tests break with refactoring
def test_service_calls_components_in_order():
    service.process(data)
    assert mock_a.call_count == 1
    assert mock_b.call_count == 1
    mock_a.assert_called_before(mock_b)
    
# Good: Test observable behavior
def test_service_produces_correct_results():
    result = service.process(data)
    assert result.validates_correctly()
    assert result.meets_business_requirements()
```

---

## **Implementation Roadmap**

### **Phase 1: Logging Integration (COMPLETED)**
- ✅ Rebuild logging integration tests
- ✅ Create lightweight test infrastructure
- ✅ Implement real log verification

### **Phase 2: Service Layer Rebuild (NEXT)**
- Rebuild core service tests with minimal mocking
- Implement real business logic testing
- Create service integration test patterns

### **Phase 3: API Layer Enhancement**
- Enhance API tests with real service integration
- Implement end-to-end request/response testing
- Add authentication and authorization testing

### **Phase 4: Infrastructure Integration**
- Rebuild infrastructure tests with real retry/timeout logic
- Implement external client integration patterns
- Add performance testing integration

### **Phase 5: Unit Test Cleanup**
- Clean up over-complex unit tests
- Separate true unit tests from integration tests
- Implement proper isolation patterns

---

## **Migration Guidelines**

### **Identifying Tests for Rebuild**
1. **Heavy Mock Usage**: > 5 patches or mocks per test
2. **Complex Setup**: > 20 lines of fixture/setup code
3. **Implementation Testing**: Tests that break with refactoring
4. **Meaningless Assertions**: Tests that verify mock calls only

### **Migration Process**
1. **Analyze Current Test**: What business behavior is being validated?
2. **Identify Real Dependencies**: What components should interact naturally?
3. **Mock Only Boundaries**: External APIs, databases, file systems
4. **Implement Real Verification**: Test actual behavior and output
5. **Add Performance Validation**: Ensure tests run efficiently

### **Success Metrics**
- **Reduced Mock Usage**: < 2 external mocks per test
- **Improved Coverage**: Business behavior coverage > implementation coverage
- **Better Reliability**: Tests pass consistently across environments
- **Faster Feedback**: Test failures point to actual issues, not test problems

---

This standards document serves as the foundation for all future testing in FaultMaven, ensuring tests provide real value and confidence in the system's behavior.