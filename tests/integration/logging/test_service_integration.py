"""
Test module for service layer logging integration.

This module tests the integration between the BaseService class
and the unified logging infrastructure.
"""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime

from faultmaven.services.base_service import BaseService
from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    request_context
)


class TestService(BaseService):
    """Test service implementation."""
    
    def __init__(self, service_name=None):
        super().__init__(service_name)
        self.test_data = {}
    
    async def async_operation(self, data):
        """Test async operation."""
        await asyncio.sleep(0.01)
        return {"processed": data, "timestamp": datetime.utcnow().isoformat()}
    
    def sync_operation(self, data):
        """Test sync operation."""
        return {"processed": data, "count": len(str(data))}
    
    def failing_operation(self, should_fail=True):
        """Operation that can fail."""
        if should_fail:
            raise ValueError("Operation failed as requested")
        return {"success": True}
    
    def validate_data(self, data):
        """Validation function."""
        if not data or not isinstance(data, dict):
            raise ValueError("Data must be a non-empty dictionary")
    
    def transform_result(self, result):
        """Result transformation function."""
        return {**result, "transformed": True}


class TestServiceLoggingIntegration:
    """Test service layer logging integration."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_service_initialization_logging(self, mock_get_logger):
        """Test service initialization logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        service = TestService("custom_service_name")
        
        # Should have logged service initialization
        mock_logger.log_event.assert_called_once()
        call_args = mock_logger.log_event.call_args
        
        assert call_args[1]["event_type"] == "system"
        assert call_args[1]["event_name"] == "service_initialized"
        assert call_args[1]["severity"] == "info"
        assert call_args[1]["data"]["service_name"] == "custom_service_name"
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_service_name_generation(self, mock_get_logger):
        """Test automatic service name generation from class name."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        service = TestService()  # No custom name
        
        # Should generate snake_case name from class name
        assert service.service_name == "test_service"
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_execute_operation_async_success(self, mock_get_logger):
        """Test successful async operation execution with logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        test_data = {"key": "value", "number": 42}
        
        result = await service.execute_operation(
            "process_async_data",
            service.async_operation,
            test_data,
            log_result=True
        )
        
        # Verify result
        assert result["processed"] == test_data
        assert "timestamp" in result
        
        # Verify logging calls
        mock_logger.log_boundary.assert_any_call(
            operation="process_async_data",
            direction="inbound",
            data={
                "service": "test_service",
                "args_count": 1,
                "kwargs_keys": []
            }
        )
        
        mock_logger.log_boundary.assert_any_call(
            operation="process_async_data",
            direction="outbound",
            data={
                "service": "test_service",
                "success": True,
                "result_type": "dict"
            }
        )
        
        # Check business event logging
        mock_logger.log_event.assert_any_call(
            event_type="business",
            event_name="operation_completed",
            severity="info",
            data={
                "operation": "process_async_data",
                "service": "test_service",
                "result_type": "dict"
            }
        )
        
        coordinator.end_request()
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_execute_operation_sync_success(self, mock_get_logger):
        """Test successful sync operation execution with logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        test_data = {"key": "value"}
        
        result = service.execute_operation_sync(
            "process_sync_data",
            service.sync_operation,
            test_data
        )
        
        # Verify result
        assert result["processed"] == test_data
        assert result["count"] == len(str(test_data))
        
        # Verify boundary logging
        assert mock_logger.log_boundary.call_count >= 2  # Inbound and outbound
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_execute_operation_with_validation(self, mock_get_logger):
        """Test operation execution with input validation."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        
        # Test successful validation
        valid_data = {"valid": "data"}
        result = await service.execute_operation(
            "validated_operation",
            service.async_operation,
            valid_data,
            validate_inputs=service.validate_data
        )
        
        assert result["processed"] == valid_data
        
        # Test failed validation
        with pytest.raises(ValueError, match="Validation failed"):
            await service.execute_operation(
                "failing_validation",
                service.async_operation,
                None,  # Invalid data
                validate_inputs=service.validate_data
            )
        
        # Should have logged validation failure
        mock_logger.error.assert_called()
        error_calls = mock_logger.error.call_args_list
        validation_errors = [call for call in error_calls if "Input validation failed" in str(call)]
        assert len(validation_errors) >= 1
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_execute_operation_with_transformation(self, mock_get_logger):
        """Test operation execution with result transformation."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        test_data = {"key": "value"}
        
        result = await service.execute_operation(
            "transformed_operation",
            service.async_operation,
            test_data,
            transform_result=service.transform_result
        )
        
        # Should have transformed result
        assert result["processed"] == test_data
        assert result["transformed"] is True
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_execute_operation_failure_handling(self, mock_get_logger):
        """Test operation failure handling and logging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        
        # Test operation failure
        with pytest.raises(RuntimeError, match="Service operation failed"):
            await service.execute_operation(
                "failing_operation",
                service.failing_operation,
                should_fail=True
            )
        
        # Verify error boundary logging
        mock_logger.log_boundary.assert_any_call(
            operation="failing_operation",
            direction="outbound",
            data={
                "service": "test_service",
                "success": False,
                "error": "Operation failed as requested",
                "error_type": "ValueError"
            }
        )
        
        # Verify technical event logging
        mock_logger.log_event.assert_any_call(
            event_type="technical",
            event_name="operation_failed",
            severity="error",
            data={
                "operation": "failing_operation",
                "service": "test_service",
                "error": "Operation failed as requested",
                "error_type": "ValueError"
            }
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
        
        service = TestService()
        
        # Mock the operation context manager to capture context updates
        captured_contexts = []
        
        original_operation = service.logger.operation
        
        async def capture_operation_context(*args, **kwargs):
            async with original_operation(*args, **kwargs) as ctx:
                captured_contexts.append(ctx.copy())
                yield ctx
        
        service.logger.operation = capture_operation_context
        
        test_data = {"test": "data"}
        await service.execute_operation(
            "context_tracking_op",
            service.async_operation,
            test_data
        )
        
        # Should have captured operation context
        assert len(captured_contexts) >= 1
        
        ctx = captured_contexts[0]
        assert ctx["operation"] == "context_tracking_op"
        assert ctx["service"] == "test_service"
        assert "execution_started" in ctx
        assert "execution_completed" in ctx
        
        coordinator.end_request()
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_service_metric_logging(self, mock_get_logger):
        """Test service metric logging with automatic service tagging."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        service = TestService()
        
        service.log_metric(
            metric_name="operations_processed",
            value=100,
            unit="count",
            tags={"type": "async"}
        )
        
        # Verify metric logging with service tag
        mock_logger.log_metric.assert_called_once()
        call_args = mock_logger.log_metric.call_args
        
        assert call_args[1]["metric_name"] == "operations_processed"
        assert call_args[1]["value"] == 100
        assert call_args[1]["unit"] == "count"
        assert call_args[1]["tags"]["service"] == "test_service"
        assert call_args[1]["tags"]["type"] == "async"
    
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    def test_service_business_event_logging(self, mock_get_logger):
        """Test service business event logging with service context."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        service = TestService()
        
        event_data = {"user_id": "123", "action": "data_processed"}
        
        service.log_business_event(
            event_name="data_processing_completed",
            severity="info",
            data=event_data
        )
        
        # Verify business event logging with service context
        mock_logger.log_event.assert_called_once()
        call_args = mock_logger.log_event.call_args
        
        assert call_args[1]["event_type"] == "business"
        assert call_args[1]["event_name"] == "data_processing_completed"
        assert call_args[1]["severity"] == "info"
        assert call_args[1]["data"]["service"] == "test_service"
        assert call_args[1]["data"]["user_id"] == "123"
        assert call_args[1]["data"]["action"] == "data_processed"
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_service_health_check(self, mock_get_logger):
        """Test service health check functionality."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        service = TestService()
        
        health_status = await service.health_check()
        
        # Verify health check response
        assert health_status["service"] == "test_service"
        assert health_status["status"] == "healthy"
        assert health_status["layer"] == "service"
        assert "timestamp" in health_status
        
        # Parse timestamp to ensure it's valid
        timestamp = datetime.fromisoformat(health_status["timestamp"])
        assert isinstance(timestamp, datetime)


class TestServiceErrorHandling:
    """Test service error handling and logging integration."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_transformation_failure_handling(self, mock_get_logger):
        """Test handling of result transformation failures."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        def failing_transform(result):
            raise RuntimeError("Transformation failed")
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        test_data = {"key": "value"}
        
        # Should continue with untransformed result
        result = await service.execute_operation(
            "transform_failing_op",
            service.async_operation,
            test_data,
            transform_result=failing_transform
        )
        
        # Should have original result (untransformed)
        assert result["processed"] == test_data
        assert "transformed" not in result
        
        # Should have logged transformation error
        mock_logger.error.assert_any_call(
            "Result transformation failed for operation: transform_failing_op",
            error=pytest.any(RuntimeError),
            operation="transform_failing_op",
            service="test_service"
        )
        
        # Should have logged warning about continuing with untransformed result
        mock_logger.warning.assert_any_call(
            "Continuing with untransformed result for operation: transform_failing_op",
            operation="transform_failing_op",
            service="test_service"
        )
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_async_validation_support(self, mock_get_logger):
        """Test support for async validation functions."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        async def async_validator(data):
            await asyncio.sleep(0.001)  # Simulate async validation
            if not data or "invalid" in str(data):
                raise ValueError("Async validation failed")
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        
        # Test successful async validation
        valid_data = {"valid": "data"}
        result = await service.execute_operation(
            "async_validated_op",
            service.async_operation,
            valid_data,
            validate_inputs=async_validator
        )
        
        assert result["processed"] == valid_data
        
        # Test failed async validation
        with pytest.raises(ValueError, match="Validation failed"):
            await service.execute_operation(
                "failing_async_validation",
                service.async_operation,
                {"invalid": "data"},
                validate_inputs=async_validator
            )
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_async_transformation_support(self, mock_get_logger):
        """Test support for async transformation functions."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        async def async_transformer(result):
            await asyncio.sleep(0.001)  # Simulate async transformation
            return {**result, "async_transformed": True}
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        test_data = {"key": "value"}
        
        result = await service.execute_operation(
            "async_transformed_op",
            service.async_operation,
            test_data,
            transform_result=async_transformer
        )
        
        # Should have async transformed result
        assert result["processed"] == test_data
        assert result["async_transformed"] is True
        
        coordinator.end_request()


class TestServiceConcurrency:
    """Test service behavior in concurrent scenarios."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    @patch('faultmaven.infrastructure.logging.unified.get_logger')
    async def test_concurrent_operations_same_service(self, mock_get_logger):
        """Test concurrent operations on same service instance."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        coordinator = LoggingCoordinator()
        coordinator.start_request()
        
        service = TestService()
        
        # Run multiple concurrent operations
        async def run_operation(op_id):
            return await service.execute_operation(
                f"concurrent_op_{op_id}",
                service.async_operation,
                {"id": op_id}
            )
        
        results = await asyncio.gather(
            run_operation(1),
            run_operation(2),
            run_operation(3)
        )
        
        # All operations should complete successfully
        assert len(results) == 3
        for i, result in enumerate(results, 1):
            assert result["processed"]["id"] == i
        
        # Verify logging for all operations
        boundary_calls = mock_logger.log_boundary.call_args_list
        inbound_calls = [call for call in boundary_calls if call[1]["direction"] == "inbound"]
        outbound_calls = [call for call in boundary_calls if call[1]["direction"] == "outbound"]
        
        # Should have inbound and outbound for each operation
        assert len(inbound_calls) >= 3
        assert len(outbound_calls) >= 3
        
        coordinator.end_request()
    
    @pytest.mark.asyncio
    async def test_concurrent_service_instances(self):
        """Test concurrent operations on different service instances."""
        
        async def service_operation(service_id):
            with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
                mock_logger = Mock()
                mock_get_logger.return_value = mock_logger
                
                coordinator = LoggingCoordinator()
                coordinator.start_request()
                
                service = TestService(f"service_{service_id}")
                
                result = await service.execute_operation(
                    "shared_operation",
                    service.async_operation,
                    {"service_id": service_id}
                )
                
                coordinator.end_request()
                return result
        
        # Run operations on different service instances
        results = await asyncio.gather(
            service_operation(1),
            service_operation(2),
            service_operation(3)
        )
        
        # All should complete successfully with their own data
        assert len(results) == 3
        for i, result in enumerate(results, 1):
            assert result["processed"]["service_id"] == i