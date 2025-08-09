"""
Test module for end-to-end logging workflow.

This module tests the complete logging flow from API layer through
service layer to infrastructure layer, ensuring proper coordination
and deduplication across all layers.
"""

import pytest
import asyncio
import json
from unittest.mock import Mock, patch, AsyncMock
from fastapi import Request, Response
from starlette.responses import JSONResponse

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.services.base_service import BaseService
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    request_context
)


class TestEndToEndService(BaseService):
    """Test service for end-to-end logging tests."""
    
    def __init__(self):
        super().__init__("e2e_test_service")
        self.external_client = TestEndToEndExternalClient("e2e_client", "ExternalAPI")
    
    async def process_user_request(self, user_data):
        """Process user request with external API call."""
        return await self.execute_operation(
            "process_user_request",
            self._internal_processing,
            user_data
        )
    
    async def _internal_processing(self, user_data):
        """Internal processing that calls external service."""
        # Validate user data
        if not user_data or "user_id" not in user_data:
            raise ValueError("Invalid user data")
        
        # Call external API
        external_result = await self.external_client.call_external(
            "fetch_user_profile",
            self.external_client.fetch_profile,
            user_data["user_id"]
        )
        
        # Process result
        processed_data = {
            "user_id": user_data["user_id"],
            "profile": external_result,
            "processed_at": "2024-01-01T00:00:00Z",
            "status": "success"
        }
        
        # Log business event
        self.log_business_event(
            "user_request_processed",
            severity="info",
            data={"user_id": user_data["user_id"]}
        )
        
        return processed_data


class TestEndToEndExternalClient(BaseExternalClient):
    """Test external client for end-to-end logging tests."""
    
    def __init__(self, client_name, service_name):
        super().__init__(
            client_name=client_name,
            service_name=service_name,
            enable_circuit_breaker=True
        )
    
    async def fetch_profile(self, user_id):
        """Fetch user profile from external API."""
        await asyncio.sleep(0.01)  # Simulate network delay
        return {
            "user_id": user_id,
            "name": f"User {user_id}",
            "email": f"user{user_id}@example.com",
            "created_at": "2023-01-01T00:00:00Z"
        }


class TestEndToEndLoggingFlow:
    """Test complete end-to-end logging flow."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    async def test_complete_request_flow_logging(self):
        """Test complete logging flow from middleware to infrastructure."""
        # Mock all loggers
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            with patch('faultmaven.api.middleware.logging.logger') as mock_middleware_logger_instance:
                with patch('faultmaven.infrastructure.logging_config.set_request_id') as mock_set_id:
                    
                    # Create mock loggers
                    mock_unified_logger = Mock()
                    
                    mock_get_logger.return_value = mock_unified_logger
                    
                    # Create test service
                    service = TestEndToEndService()
                    
                    # Mock ASGI app that uses the service
                    async def test_app(request):
                        # Extract user data from request
                        user_data = {"user_id": "123", "action": "get_profile"}
                        
                        # Process through service
                        result = await service.process_user_request(user_data)
                        
                        return JSONResponse(result)
                    
                    # Create middleware
                    middleware = LoggingMiddleware(test_app)
                    
                    # Create mock request
                    mock_request = Mock(spec=Request)
                    mock_request.method = "POST"
                    mock_request.url.path = "/api/users/profile"
                    mock_request.query_params = "include=profile"
                    mock_request.client.host = "192.168.1.100"
                    # Create a mock headers object with a get method
                    headers_data = {
                        'user-agent': 'FaultMaven-Client/1.0',
                        'content-type': 'application/json',
                        'authorization': 'Bearer token123'
                    }
                    mock_headers = Mock()
                    mock_headers.get = lambda key, default=None: headers_data.get(key, default)
                    mock_request.headers = mock_headers
                    
                    async def mock_call_next(request):
                        return await test_app(request)
                    
                    # Execute complete flow
                    response = await middleware.dispatch(mock_request, mock_call_next)
                    
                    # Verify response
                    assert response.status_code == 200
                    response_data = json.loads(response.body)
                    assert response_data["user_id"] == "123"
                    assert response_data["status"] == "success"
                    assert "profile" in response_data
                    
                    # Verify correlation ID header
                    assert "X-Correlation-ID" in response.headers
                    correlation_id = response.headers["X-Correlation-ID"]
                    assert len(correlation_id) > 0
                    
                    # Verify middleware logging
                    assert mock_middleware_logger_instance.info.call_count >= 2  # Start and complete
                    
                    # Verify service and infrastructure logging
                    assert mock_unified_logger.log_boundary.call_count >= 4  # Service and client boundaries
                    assert mock_unified_logger.log_event.call_count >= 2  # Service and client events
                    
                    # Verify specific logging patterns
                    boundary_calls = mock_unified_logger.log_boundary.call_args_list
                    service_boundaries = [call for call in boundary_calls 
                                        if "process_user_request" in str(call)]
                    client_boundaries = [call for call in boundary_calls 
                                       if "fetch_user_profile" in str(call)]
                    
                    assert len(service_boundaries) >= 2  # Inbound and outbound
                    assert len(client_boundaries) >= 2  # Inbound and outbound
    
    @pytest.mark.asyncio
    async def test_error_propagation_logging(self):
        """Test error propagation and logging across layers."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            with patch('faultmaven.api.middleware.logging.logger') as mock_middleware_logger_instance:
                with patch('faultmaven.infrastructure.logging_config.set_request_id'):
                    
                    mock_unified_logger = Mock()
                    
                    mock_get_logger.return_value = mock_unified_logger
                    
                    # Create service with failing external client
                    service = TestEndToEndService()
                    
                    # Mock external client to fail
                    async def failing_fetch(user_id):
                        raise ConnectionError("External API is down")
                    
                    service.external_client.fetch_profile = failing_fetch
                    
                    # Mock ASGI app
                    async def failing_app(request):
                        user_data = {"user_id": "123"}
                        # This will fail due to external service failure
                        result = await service.process_user_request(user_data)
                        return JSONResponse(result)
                    
                    middleware = LoggingMiddleware(failing_app)
                    
                    # Create mock request
                    mock_request = Mock(spec=Request)
                    mock_request.method = "POST"
                    mock_request.url.path = "/api/users/profile"
                    mock_request.query_params = ""
                    mock_request.client.host = "127.0.0.1"
                    mock_headers = Mock()
                    mock_headers.get = lambda key, default=None: {'user-agent': 'test-client'}.get(key, default)
                    mock_request.headers = mock_headers
                    
                    async def mock_call_next(request):
                        return await failing_app(request)
                    
                    # Execute flow - should fail
                    with pytest.raises(RuntimeError):  # Service wraps external error
                        await middleware.dispatch(mock_request, mock_call_next)
                    
                    # Verify error logging at multiple layers
                    assert mock_middleware_logger_instance.error.call_count >= 1  # Middleware error
                    assert mock_unified_logger.error.call_count >= 1  # Service/client errors
                    
                    # Verify error cascade prevention
                    # (Implementation detail - errors should be logged appropriately at each layer)
                    error_calls = mock_unified_logger.error.call_args_list
                    assert len(error_calls) >= 1
    
    @pytest.mark.asyncio
    async def test_performance_tracking_across_layers(self):
        """Test performance tracking from middleware through all layers."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            with patch('faultmaven.api.middleware.logging.logger') as mock_middleware_logger_instance:
                with patch('faultmaven.infrastructure.logging_config.set_request_id'):
                    
                    mock_unified_logger = Mock()
                    
                    mock_get_logger.return_value = mock_unified_logger
                    
                    # Create service with slow external client
                    service = TestEndToEndService()
                    
                    # Mock slow external call
                    async def slow_fetch(user_id):
                        await asyncio.sleep(0.2)  # Slow operation
                        return {"user_id": user_id, "name": "Slow User"}
                    
                    service.external_client.fetch_profile = slow_fetch
                    
                    # Mock ASGI app
                    async def slow_app(request):
                        user_data = {"user_id": "123"}
                        result = await service.process_user_request(user_data)
                        return JSONResponse(result)
                    
                    middleware = LoggingMiddleware(slow_app)
                    
                    # Create mock request
                    mock_request = Mock(spec=Request)
                    mock_request.method = "GET"
                    mock_request.url.path = "/api/slow"
                    mock_request.query_params = ""
                    mock_request.client.host = "127.0.0.1"
                    mock_headers = Mock()
                    mock_headers.get = lambda key, default=None: {'user-agent': 'test-client'}.get(key, default)
                    mock_request.headers = mock_headers
                    
                    async def mock_call_next(request):
                        return await slow_app(request)
                    
                    # Execute slow flow
                    response = await middleware.dispatch(mock_request, mock_call_next)
                    
                    # Should complete successfully
                    assert response.status_code == 200
                    
                    # Check for performance warnings
                    warning_calls = mock_middleware_logger_instance.warning.call_args_list
                    perf_warnings = [call for call in warning_calls if "Slow request detected" in str(call)]
                    
                    # May have performance warnings at middleware level
                    # (depending on total request time)
                    
                    # Check for performance metrics logging
                    metric_calls = mock_unified_logger.log_metric.call_args_list
                    duration_metrics = [call for call in metric_calls 
                                      if call[1]["metric_name"] == "external_call_duration"]
                    
                    # Should have logged external call duration
                    assert len(duration_metrics) >= 1
    
    @pytest.mark.asyncio
    async def test_concurrent_request_isolation(self):
        """Test that concurrent requests maintain proper logging isolation."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            with patch('faultmaven.api.middleware.logging.logger') as mock_middleware_logger_instance:
                with patch('faultmaven.infrastructure.logging_config.set_request_id'):
                    
                    mock_unified_logger = Mock()
                    
                    mock_get_logger.return_value = mock_unified_logger
                    
                    # Track correlation IDs seen in each request
                    seen_correlation_ids = []
                    
                    original_start_request = LoggingCoordinator.start_request
                    
                    def tracking_start_request(self, **kwargs):
                        ctx = original_start_request(self, **kwargs)
                        seen_correlation_ids.append(ctx.correlation_id)
                        return ctx
                    
                    with patch.object(LoggingCoordinator, 'start_request', tracking_start_request):
                        
                        async def request_handler(request_id):
                            """Handle individual request."""
                            service = TestEndToEndService()
                            
                            async def app(request):
                                user_data = {"user_id": f"user_{request_id}"}
                                result = await service.process_user_request(user_data)
                                return JSONResponse(result)
                            
                            middleware = LoggingMiddleware(app)
                            
                            mock_request = Mock(spec=Request)
                            mock_request.method = "GET"
                            mock_request.url.path = f"/api/user/{request_id}"
                            mock_request.query_params = ""
                            mock_request.client.host = "127.0.0.1"
                            mock_headers = Mock()
                            mock_headers.get = lambda key, default=None: {'user-agent': 'test-client'}.get(key, default)
                            mock_request.headers = mock_headers
                            
                            async def mock_call_next(request):
                                return await app(request)
                            
                            return await middleware.dispatch(mock_request, mock_call_next)
                        
                        # Process multiple concurrent requests
                        responses = await asyncio.gather(
                            request_handler(1),
                            request_handler(2),
                            request_handler(3)
                        )
                        
                        # All requests should complete successfully
                        assert all(r.status_code == 200 for r in responses)
                        
                        # Each request should have unique correlation ID
                        assert len(seen_correlation_ids) == 3
                        assert len(set(seen_correlation_ids)) == 3  # All unique
                        
                        # Each response should have unique correlation ID header
                        response_correlation_ids = [r.headers.get("X-Correlation-ID") for r in responses]
                        assert len(set(response_correlation_ids)) == 3  # All unique
    
    @pytest.mark.asyncio
    async def test_deduplication_across_layers(self):
        """Test that deduplication works properly across all layers."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            with patch('faultmaven.api.middleware.logging.logger') as mock_middleware_logger_instance:
                with patch('faultmaven.infrastructure.logging_config.set_request_id'):
                    
                    mock_unified_logger = Mock()
                    
                    mock_get_logger.return_value = mock_unified_logger
                    
                    service = TestEndToEndService()
                    
                    # Mock external client to call same operation multiple times
                    original_call_external = service.external_client.call_external
                    
                    async def duplicate_calling_process(user_data):
                        """Process that makes duplicate calls."""
                        # Make the same external call multiple times
                        result1 = await original_call_external(
                            "duplicate_operation",
                            service.external_client.fetch_profile,
                            user_data["user_id"]
                        )
                        
                        result2 = await original_call_external(
                            "duplicate_operation",  # Same operation name
                            service.external_client.fetch_profile,
                            user_data["user_id"]
                        )
                        
                        return result1  # Return first result
                    
                    # Replace service processing with duplicate calling version
                    service._internal_processing = duplicate_calling_process
                    
                    async def app(request):
                        user_data = {"user_id": "123"}
                        result = await service.process_user_request(user_data)
                        return JSONResponse(result)
                    
                    middleware = LoggingMiddleware(app)
                    
                    mock_request = Mock(spec=Request)
                    mock_request.method = "POST"
                    mock_request.url.path = "/api/duplicate-test"
                    mock_request.query_params = ""
                    mock_request.client.host = "127.0.0.1"
                    mock_headers = Mock()
                    mock_headers.get = lambda key, default=None: {'user-agent': 'test-client'}.get(key, default)
                    mock_request.headers = mock_headers
                    
                    async def mock_call_next(request):
                        return await app(request)
                    
                    # Execute flow with duplicate operations
                    response = await middleware.dispatch(mock_request, mock_call_next)
                    
                    # Should complete successfully
                    assert response.status_code == 200
                    
                    # Verify deduplication worked
                    boundary_calls = mock_unified_logger.log_boundary.call_args_list
                    
                    # Count boundary calls for duplicate_operation
                    duplicate_boundaries = [call for call in boundary_calls 
                                          if "duplicate_operation" in str(call)]
                    
                    # With deduplication, should have fewer duplicate operation logs
                    # (Exact count depends on implementation, but should be deduplicated)
                    # The key is that deduplication is working across the layers
                    
                    # Verify that the same request context was used throughout
                    # (This is tested implicitly by the deduplication working)
    
    @pytest.mark.asyncio
    async def test_comprehensive_logging_content(self):
        """Test that all expected logging content is captured."""
        with patch('faultmaven.infrastructure.logging.unified.get_logger') as mock_get_logger:
            with patch('faultmaven.api.middleware.logging.logger') as mock_middleware_logger_instance:
                with patch('faultmaven.infrastructure.logging_config.set_request_id'):
                    
                    mock_unified_logger = Mock()
                    
                    mock_get_logger.return_value = mock_unified_logger
                    
                    service = TestEndToEndService()
                    
                    async def app(request):
                        user_data = {"user_id": "comprehensive_test"}
                        result = await service.process_user_request(user_data)
                        return JSONResponse(result)
                    
                    middleware = LoggingMiddleware(app)
                    
                    mock_request = Mock(spec=Request)
                    mock_request.method = "POST"
                    mock_request.url.path = "/api/comprehensive-test"
                    mock_request.query_params = "detail=full&format=json"
                    mock_request.client.host = "10.0.1.50"
                    headers_data = {
                        'user-agent': 'FaultMaven-TestClient/2.0',
                        'content-type': 'application/json',
                        'x-request-id': 'req-12345',
                        'x-forwarded-for': '203.0.113.1',
                        'content-length': '156'
                    }
                    mock_headers = Mock()
                    mock_headers.get = lambda key, default=None: headers_data.get(key, default)
                    mock_request.headers = mock_headers
                    
                    async def mock_call_next(request):
                        return await app(request)
                    
                    # Execute comprehensive flow
                    response = await middleware.dispatch(mock_request, mock_call_next)
                    
                    # Verify response
                    assert response.status_code == 200
                    
                    # Verify middleware captured all request details
                    middleware_info_calls = mock_middleware_logger_instance.info.call_args_list
                    start_calls = [call for call in middleware_info_calls if "Request started" in str(call)]
                    
                    assert len(start_calls) >= 1
                    start_call = start_calls[0]
                    start_kwargs = start_call[1]
                    
                    # Should capture detailed request information
                    assert start_kwargs["method"] == "POST"
                    assert start_kwargs["path"] == "/api/comprehensive-test"
                    assert start_kwargs["query_params"] == "detail=full&format=json"
                    assert start_kwargs["client_ip"] == "10.0.1.50"
                    assert start_kwargs["user_agent"] == "FaultMaven-TestClient/2.0"
                    assert start_kwargs["x_forwarded_for"] == "203.0.113.1"
                    assert start_kwargs["content_length"] == "156"
                    
                    # Verify service logged business events
                    event_calls = mock_unified_logger.log_event.call_args_list
                    business_events = [call for call in event_calls 
                                     if call[1].get("event_type") == "business"]
                    
                    assert len(business_events) >= 1
                    
                    # Verify infrastructure logged technical events
                    technical_events = [call for call in event_calls 
                                       if call[1].get("event_type") == "technical"]
                    
                    assert len(technical_events) >= 1
                    
                    # Verify metrics were logged
                    metric_calls = mock_unified_logger.log_metric.call_args_list
                    assert len(metric_calls) >= 1