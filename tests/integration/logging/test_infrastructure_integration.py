"""
Test module for infrastructure layer logging integration.

This module tests the integration between the BaseExternalClient class
and the unified logging infrastructure.
"""

import pytest
import asyncio
import time
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timedelta

from faultmaven.infrastructure.base_client import (
    BaseExternalClient,
    CircuitBreaker,
    CircuitBreakerError
)
from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    request_context
)


class TestExternalClient(BaseExternalClient):
    """Test external client implementation."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.call_history = []
    
    async def test_async_call(self, data):
        """Test async external call."""
        await asyncio.sleep(0.01)
        self.call_history.append(("async", data))
        return {"response": data, "timestamp": datetime.utcnow().isoformat()}
    
    def test_sync_call(self, data):
        """Test sync external call."""
        self.call_history.append(("sync", data))
        return {"response": data, "count": len(str(data))}
    
    def failing_call(self, should_fail=True):
        """Call that can fail."""
        if should_fail:
            raise ConnectionError("External service unavailable")
        return {"success": True}
    
    def slow_call(self, delay=0.1):
        """Slow external call."""
        time.sleep(delay)
        return {"slow_response": True, "delay": delay}
    
    def validate_response(self, response):
        """Response validation function."""
        return response is not None and "response" in response
    
    def transform_response(self, response):
        """Response transformation function."""
        return {**response, "transformed": True}


class TestInfrastructureLoggingIntegration:
    """Test infrastructure layer logging integration."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_client_initialization_logging(self, mock_get_logger):
        """Test external client initialization logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        client = TestExternalClient(
            client_name="test_client",
            service_name="TestService",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3
        )
        
        # Verify initialization
        assert client.client_name == "test_client"
        assert client.service_name == "TestService"
        assert client.circuit_breaker is not None
        
        # Should have logged client initialization
        mock_logger.log_event.assert_called_once()
        call_args = mock_logger.log_event.call_args
        
        assert call_args[1]["event_type"] == "system"
        assert call_args[1]["event_name"] == "external_client_initialized"
        assert call_args[1]["severity"] == "info"
        assert call_args[1]["data"]["client_name"] == "test_client"
        assert call_args[1]["data"]["service_name"] == "TestService"
        assert call_args[1]["data"]["circuit_breaker_enabled"] is True
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_call_external_async_success(self, mock_get_logger):
        """Test successful async external call with logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        test_data = {"key": "value", "number": 42}
        
        result = await client.call_external(
            "test_async_operation",
            client.test_async_call,
            test_data,
            timeout=5.0,
            retries=2
        )
        
        # Verify result
        assert result["response"] == test_data
        assert "timestamp" in result
        
        # Verify boundary logging
        mock_logger.log_boundary.assert_any_call(
            operation="test_async_operation",
            direction="inbound",
            data={
                "client": "test_client",
                "service": "TestService",
                "args_count": 1,
                "kwargs_keys": [],
                "timeout": 5.0,
                "retries": 2
            }
        )
        
        mock_logger.log_boundary.assert_any_call(
            operation="test_async_operation",
            direction="outbound",
            data={
                "client": "test_client",
                "service": "TestService",
                "success": True,
                "duration": pytest.any(float),
                "attempts": 1
            }
        )
        
        # Verify success event logging
        mock_logger.log_event.assert_any_call(
            event_type="technical",
            event_name="external_call_success",
            severity="info",
            data={
                "client": "test_client",
                "service": "TestService",
                "operation": "test_async_operation",
                "duration": pytest.any(float),
                "attempts": 1
            }
        )
        
        # Verify metric logging
        mock_logger.log_metric.assert_called()
        metric_call = mock_logger.log_metric.call_args
        assert metric_call[1]["metric_name"] == "external_call_duration"
        assert metric_call[1]["unit"] == "seconds"
        assert metric_call[1]["tags"]["success"] == "true"
        
        coordinator.end_request()
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_call_external_sync_success(self, mock_get_logger):
        """Test successful sync external call with logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        test_data = {"key": "value"}
        
        result = client.call_external_sync(
            "test_sync_operation",
            client.test_sync_call,
            test_data,
            retries=1
        )
        
        # Verify result
        assert result["response"] == test_data
        assert result["count"] == len(str(test_data))
        
        # Verify boundary logging
        assert mock_logger.log_boundary.call_count >= 2  # Inbound and outbound
        
        # Verify success logging
        mock_logger.log_event.assert_any_call(
            event_type="technical",
            event_name="external_call_success",
            severity="info",
            data={
                "client": "test_client",
                "service": "TestService",
                "operation": "test_sync_operation",
                "duration": pytest.any(float),
                "attempts": 1
            }
        )
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_call_external_with_validation(self, mock_get_logger):
        """Test external call with response validation."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        test_data = {"key": "value"}
        
        # Test successful validation
        result = await client.call_external(
            "validated_operation",
            client.test_async_call,
            test_data,
            validate_response=client.validate_response
        )
        
        assert result["response"] == test_data
        
        # Test failed validation
        def failing_validator(response):
            return False  # Always fail
        
        with pytest.raises(RuntimeError, match="Response validation failed"):
            await client.call_external(
                "failing_validation",
                client.test_async_call,
                test_data,
                validate_response=failing_validator
            )
        
        # Should have logged validation failure
        mock_logger.error.assert_any_call(
            "Response validation failed for TestService.failing_validation",
            error=pytest.any(ValueError),
            client="test_client",
            service="TestService"
        )
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_call_external_with_transformation(self, mock_get_logger):
        """Test external call with response transformation."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        test_data = {"key": "value"}
        
        result = await client.call_external(
            "transformed_operation",
            client.test_async_call,
            test_data,
            transform_response=client.transform_response
        )
        
        # Should have transformed result
        assert result["response"] == test_data
        assert result["transformed"] is True
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_call_external_retry_logic(self, mock_get_logger):
        """Test external call retry logic and logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        
        # Mock external call to fail twice, then succeed
        call_count = 0
        
        async def failing_then_succeeding_call(data):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError(f"Failure #{call_count}")
            return {"success": True, "attempts": call_count}
        
        result = await client.call_external(
            "retry_operation",
            failing_then_succeeding_call,
            {"test": "data"},
            retries=3,
            retry_delay=0.001
        )
        
        # Should eventually succeed
        assert result["success"] is True
        assert result["attempts"] == 3
        
        # Verify retry event logging
        retry_events = [call for call in mock_logger.log_event.call_args_list
                       if call[1].get("event_name") == "external_call_retry"]
        
        assert len(retry_events) >= 2  # Should have logged retries
        
        # Verify final success
        success_events = [call for call in mock_logger.log_event.call_args_list
                         if call[1].get("event_name") == "external_call_success"]
        assert len(success_events) >= 1
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_call_external_failure_after_retries(self, mock_get_logger):
        """Test external call failure after all retries."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        
        # Test operation that always fails
        with pytest.raises(RuntimeError, match="External call to TestService.always_failing failed after 3 attempts"):
            await client.call_external(
                "always_failing",
                client.failing_call,
                should_fail=True,
                retries=2
            )
        
        # Verify failure boundary logging
        mock_logger.log_boundary.assert_any_call(
            operation="always_failing",
            direction="outbound",
            data={
                "client": "test_client",
                "service": "TestService",
                "success": False,
                "error": "External service unavailable",
                "attempts": 3
            }
        )
        
        # Verify failure event logging
        mock_logger.log_event.assert_any_call(
            event_type="technical",
            event_name="external_call_failed",
            severity="error",
            data={
                "client": "test_client",
                "service": "TestService",
                "operation": "always_failing",
                "error": "External service unavailable",
                "attempts": 3
            }
        )
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_call_external_timeout_handling(self, mock_get_logger):
        """Test external call timeout handling."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        
        # Mock call that takes too long
        async def slow_call(data):
            await asyncio.sleep(1.0)  # Longer than timeout
            return {"slow": True}
        
        with pytest.raises(TimeoutError, match="External call to TestService.timeout_test timed out after 0.1s"):
            await client.call_external(
                "timeout_test",
                slow_call,
                {"test": "data"},
                timeout=0.1
            )
        
        # Verify timeout error logging
        mock_logger.error.assert_any_call(
            "External call timed out: TestService.timeout_test",
            error=pytest.any(asyncio.TimeoutError),
            client="test_client",
            service="TestService",
            timeout=0.1
        )
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_operation_context_tracking(self, mock_get_logger):
        """Test operation context tracking through unified logger."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        
        # Mock the operation context manager to capture context updates
        captured_contexts = []
        
        original_operation = client.logger.operation
        
        async def capture_operation_context(*args, **kwargs):
            async with original_operation(*args, **kwargs) as ctx:
                captured_contexts.append(ctx.copy())
                yield ctx
        
        client.logger.operation = capture_operation_context
        
        test_data = {"test": "data"}
        await client.call_external(
            "context_tracking_op",
            client.test_async_call,
            test_data
        )
        
        # Should have captured operation context
        assert len(captured_contexts) >= 1
        
        ctx = captured_contexts[0]
        assert ctx["operation"] == "context_tracking_op"
        assert ctx["client"] == "test_client"
        assert ctx["service"] == "TestService"
        assert "call_duration" in ctx
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_health_check_functionality(self, mock_get_logger):
        """Test client health check functionality."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        client = TestExternalClient(
            "test_client",
            "TestService",
            enable_circuit_breaker=True
        )
        
        # Simulate some call history
        client.connection_metrics["successful_calls"] = 10
        client.connection_metrics["failed_calls"] = 2
        client.connection_metrics["last_success_time"] = datetime.utcnow().isoformat()
        
        health_status = await client.health_check()
        
        # Verify health check response
        assert health_status["client"] == "test_client"
        assert health_status["service"] == "TestService"
        assert health_status["layer"] == "infrastructure"
        assert "timestamp" in health_status
        
        # Verify metrics
        metrics = health_status["metrics"]
        assert metrics["successful_calls"] == 10
        assert metrics["failed_calls"] == 2
        assert metrics["last_success_time"] is not None
        
        # Verify circuit breaker info
        cb_info = health_status["circuit_breaker"]
        assert cb_info["enabled"] is True
        assert cb_info["state"] == "closed"
        assert cb_info["failure_count"] == 0


class TestCircuitBreakerIntegration:
    """Test circuit breaker integration with logging."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    def test_circuit_breaker_creation(self):
        """Test circuit breaker creation and initial state."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30)
        
        assert cb.failure_threshold == 3
        assert cb.recovery_timeout == 30
        assert cb.failure_count == 0
        assert cb.state == "closed"
        assert cb.last_failure_time is None
        assert cb.can_execute() is True
    
    def test_circuit_breaker_failure_tracking(self):
        """Test circuit breaker failure tracking and state transitions."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        
        # Record first failure
        cb.record_failure(ConnectionError("Service down"))
        assert cb.failure_count == 1
        assert cb.state == "closed"
        assert cb.can_execute() is True
        
        # Record second failure - should open circuit
        cb.record_failure(ConnectionError("Still down"))
        assert cb.failure_count == 2
        assert cb.state == "open"
        assert cb.can_execute() is False
        
        # Record success - should close circuit
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == "closed"
        assert cb.can_execute() is True
    
    def test_circuit_breaker_recovery_timeout(self):
        """Test circuit breaker recovery timeout logic."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=1)  # 1 second timeout
        
        # Open circuit
        cb.record_failure(ConnectionError("Service down"))
        assert cb.state == "open"
        assert cb.can_execute() is False
        
        # Wait for recovery timeout
        time.sleep(1.1)
        
        # Should allow execution (half-open)
        assert cb.can_execute() is True
        assert cb.state == "half-open"
        
        # Success should close circuit
        cb.record_success()
        assert cb.state == "closed"
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_circuit_breaker_prevents_calls(self, mock_get_logger):
        """Test circuit breaker prevents calls and logs appropriately."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient(
            "test_client",
            "TestService",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=1
        )
        
        # Open circuit by failing
        with pytest.raises(RuntimeError):
            await client.call_external(
                "failing_call",
                client.failing_call,
                should_fail=True
            )
        
        # Next call should be blocked by circuit breaker
        with pytest.raises(CircuitBreakerError, match="Circuit breaker is open for TestService"):
            await client.call_external(
                "blocked_call",
                client.test_async_call,
                {"test": "data"}
            )
        
        # Verify circuit breaker event logging
        cb_events = [call for call in mock_logger.log_event.call_args_list
                    if call[1].get("event_name") == "circuit_breaker_open"]
        assert len(cb_events) >= 1
        
        cb_event = cb_events[0]
        assert cb_event[1]["event_type"] == "technical"
        assert cb_event[1]["severity"] == "warning"
        assert cb_event[1]["data"]["client"] == "test_client"
        assert cb_event[1]["data"]["service"] == "TestService"
        assert cb_event[1]["data"]["operation"] == "blocked_call"
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_circuit_breaker_metrics_tracking(self, mock_get_logger):
        """Test circuit breaker metrics are tracked correctly."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        client = TestExternalClient(
            "test_client",
            "TestService",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=1
        )
        
        # Successful call
        await client.call_external(
            "success_call",
            client.test_async_call,
            {"test": "data"}
        )
        
        assert client.connection_metrics["successful_calls"] == 1
        assert client.connection_metrics["failed_calls"] == 0
        assert client.connection_metrics["circuit_breaker_trips"] == 0
        
        # Failed call (opens circuit)
        with pytest.raises(RuntimeError):
            await client.call_external(
                "failing_call",
                client.failing_call,
                should_fail=True
            )
        
        assert client.connection_metrics["successful_calls"] == 1
        assert client.connection_metrics["failed_calls"] == 1
        
        # Circuit breaker trip
        with pytest.raises(CircuitBreakerError):
            await client.call_external(
                "blocked_call",
                client.test_async_call,
                {"test": "data"}
            )
        
        assert client.connection_metrics["circuit_breaker_trips"] == 1


class TestInfrastructureErrorHandling:
    """Test infrastructure error handling scenarios."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_transformation_failure_handling(self, mock_get_logger):
        """Test handling of response transformation failures."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        def failing_transform(response):
            raise RuntimeError("Transformation failed")
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        test_data = {"key": "value"}
        
        # Should continue with untransformed response
        result = await client.call_external(
            "transform_failing_op",
            client.test_async_call,
            test_data,
            transform_response=failing_transform
        )
        
        # Should have original result (untransformed)
        assert result["response"] == test_data
        assert "transformed" not in result
        
        # Should have logged transformation error
        mock_logger.error.assert_any_call(
            "Response transformation failed for TestService.transform_failing_op",
            error=pytest.any(RuntimeError),
            client="test_client",
            service="TestService"
        )
        
        # Should have logged warning about continuing with untransformed response
        mock_logger.warning.assert_any_call(
            "Continuing with untransformed response for TestService.transform_failing_op",
            client="test_client",
            service="TestService"
        )
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_async_transformation_support(self, mock_get_logger):
        """Test support for async transformation functions."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        async def async_transformer(response):
            await asyncio.sleep(0.001)  # Simulate async transformation
            return {**response, "async_transformed": True}
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        client = TestExternalClient("test_client", "TestService")
        test_data = {"key": "value"}
        
        result = await client.call_external(
            "async_transformed_op",
            client.test_async_call,
            test_data,
            transform_response=async_transformer
        )
        
        # Should have async transformed result
        assert result["response"] == test_data
        assert result["async_transformed"] is True
        
        coordinator.end_request()