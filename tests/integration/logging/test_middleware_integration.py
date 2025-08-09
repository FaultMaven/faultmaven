"""
Test module for logging middleware integration.

This module tests the integration between the LoggingMiddleware
and the underlying logging infrastructure.
"""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from fastapi import Request, Response
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.infrastructure.logging.coordinator import (
    LoggingCoordinator,
    request_context
)


class TestLoggingMiddlewareIntegration:
    """Test LoggingMiddleware integration with logging infrastructure."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.fixture
    def mock_logger(self):
        """Mock logger for testing."""
        return Mock()
    
    @pytest.fixture
    def mock_app(self):
        """Mock ASGI application."""
        async def app(request):
            return JSONResponse({"message": "success", "path": request.url.path})
        return app
    
    @pytest.mark.asyncio
    async def test_middleware_request_lifecycle(self, mock_logger, mock_app):
        """Test complete request lifecycle through middleware."""
        
        # Mock the get_logger and set_request_id functions
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id') as mock_set_id:
                
                middleware = LoggingMiddleware(mock_app)
                
                # Create mock request
                mock_request = Mock(spec=Request)
                mock_request.method = "GET"
                mock_request.url.path = "/api/test"
                mock_request.query_params = "param=value"
                mock_request.client.host = "127.0.0.1"
                mock_request.headers = {
                    'user-agent': 'test-agent',
                    'content-length': '100',
                    'x-forwarded-for': '192.168.1.1',
                    'x-real-ip': '10.0.0.1'
                }
                mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                
                # Mock call_next to return successful response
                async def mock_call_next(request):
                    return Response(content="success", status_code=200)
                
                # Execute middleware
                response = await middleware.dispatch(mock_request, mock_call_next)
                
                # Verify response
                assert response.status_code == 200
                assert response.headers.get('X-Correlation-ID') is not None
                
                # Verify logging calls
                assert mock_logger.info.call_count >= 2  # Start and complete
                
                # Verify set_request_id was called
                mock_set_id.assert_called_once()
                correlation_id = mock_set_id.call_args[0][0]
                assert isinstance(correlation_id, str)
                assert len(correlation_id) > 0
    
    @pytest.mark.asyncio
    async def test_middleware_error_handling(self, mock_logger):
        """Test middleware error handling and logging."""
        
        # Mock app that raises exception
        async def failing_app(request):
            raise ValueError("Test application error")
        
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id'):
                
                middleware = LoggingMiddleware(failing_app)
                
                # Create mock request
                mock_request = Mock(spec=Request)
                mock_request.method = "POST"
                mock_request.url.path = "/api/failing"
                mock_request.query_params = ""
                mock_request.client.host = "127.0.0.1"
                mock_request.headers = {'user-agent': 'test-agent'}
                mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                
                async def mock_call_next(request):
                    raise ValueError("Test application error")
                
                # Execute middleware - should propagate exception
                with pytest.raises(ValueError, match="Test application error"):
                    await middleware.dispatch(mock_request, mock_call_next)
                
                # Verify error logging
                assert mock_logger.error.call_count >= 1
                
                # Check error log content
                error_calls = mock_logger.error.call_args_list
                error_call = error_calls[-1]  # Last error call
                
                # Should contain error information
                assert "Request failed" in str(error_call) or "Failed request summary" in str(error_call)
    
    @pytest.mark.asyncio
    async def test_middleware_performance_tracking(self, mock_logger):
        """Test middleware performance tracking and warnings."""
        
        # Mock app with slow response
        async def slow_app(request):
            await asyncio.sleep(0.2)  # Simulate slow processing
            return Response(content="slow response", status_code=200)
        
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id'):
                
                middleware = LoggingMiddleware(slow_app)
                
                # Create mock request
                mock_request = Mock(spec=Request)
                mock_request.method = "GET"
                mock_request.url.path = "/api/slow"
                mock_request.query_params = ""
                mock_request.client.host = "127.0.0.1"
                mock_request.headers = {'user-agent': 'test-agent'}
                mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                
                async def mock_call_next(request):
                    await asyncio.sleep(0.2)  # Slow operation
                    return Response(content="slow response", status_code=200)
                
                # Execute middleware
                response = await middleware.dispatch(mock_request, mock_call_next)
                
                # Verify response
                assert response.status_code == 200
                
                # Check if performance warning was logged
                warning_calls = mock_logger.warning.call_args_list
                perf_warnings = [call for call in warning_calls if "Slow request detected" in str(call)]
                
                # Should have performance warning due to slow response
                assert len(perf_warnings) > 0
    
    @pytest.mark.asyncio
    async def test_middleware_context_isolation(self, mock_logger):
        """Test that middleware provides proper context isolation."""
        
        collected_contexts = []
        
        async def context_collecting_app(request):
            # Collect the current request context
            ctx = LoggingCoordinator.get_context()
            if ctx:
                collected_contexts.append({
                    'correlation_id': ctx.correlation_id,
                    'path': getattr(ctx.attributes, 'path', None)
                })
            return Response(content="success", status_code=200)
        
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id'):
                
                middleware = LoggingMiddleware(context_collecting_app)
                
                # Create multiple mock requests
                async def make_request(path):
                    mock_request = Mock(spec=Request)
                    mock_request.method = "GET"
                    mock_request.url.path = path
                    mock_request.query_params = ""
                    mock_request.client.host = "127.0.0.1"
                    mock_request.headers = {'user-agent': 'test-agent'}
                    mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                    
                    async def mock_call_next(request):
                        return await context_collecting_app(request)
                    
                    return await middleware.dispatch(mock_request, mock_call_next)
                
                # Process multiple concurrent requests
                responses = await asyncio.gather(
                    make_request("/api/test1"),
                    make_request("/api/test2"),
                    make_request("/api/test3")
                )
                
                # All responses should be successful
                assert all(r.status_code == 200 for r in responses)
                
                # Each request should have had its own context
                assert len(collected_contexts) == 3
                
                # All correlation IDs should be unique
                correlation_ids = [ctx['correlation_id'] for ctx in collected_contexts]
                assert len(set(correlation_ids)) == 3
    
    def test_middleware_coordinator_integration(self, mock_logger):
        """Test middleware properly integrates with LoggingCoordinator."""
        
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id'):
                
                middleware = LoggingMiddleware(Mock())
                
                # Middleware should create its own coordinator
                assert isinstance(middleware.coordinator, LoggingCoordinator)
                assert middleware.coordinator.context is None  # Initially no context
    
    @pytest.mark.asyncio
    async def test_middleware_log_once_integration(self, mock_logger):
        """Test middleware uses log_once for deduplication."""
        
        # Track calls to LoggingCoordinator.log_once
        with patch('faultmaven.api.middleware.logging.LoggingCoordinator.log_once') as mock_log_once:
            with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
                with patch('faultmaven.api.middleware.logging.set_request_id'):
                    
                    middleware = LoggingMiddleware(Mock())
                    
                    # Create mock request
                    mock_request = Mock(spec=Request)
                    mock_request.method = "GET"
                    mock_request.url.path = "/api/test"
                    mock_request.query_params = ""
                    mock_request.client.host = "127.0.0.1"
                    mock_request.headers = {'user-agent': 'test-agent'}
                    mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                    
                    async def mock_call_next(request):
                        return Response(content="success", status_code=200)
                    
                    # Execute middleware
                    response = await middleware.dispatch(mock_request, mock_call_next)
                    
                    # Verify log_once was called for request start and completion
                    assert mock_log_once.call_count >= 2
                    
                    # Check that proper operation keys were used
                    call_args_list = mock_log_once.call_args_list
                    operation_keys = [call[1]['operation_key'] for call in call_args_list]
                    
                    # Should have request_start and request_complete keys
                    start_keys = [key for key in operation_keys if key.startswith('request_start:')]
                    complete_keys = [key for key in operation_keys if key.startswith('request_complete:')]
                    
                    assert len(start_keys) >= 1
                    assert len(complete_keys) >= 1
    
    @pytest.mark.asyncio
    async def test_middleware_request_summary(self, mock_logger):
        """Test middleware generates proper request summary."""
        
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id'):
                
                middleware = LoggingMiddleware(Mock())
                
                # Create mock request
                mock_request = Mock(spec=Request)
                mock_request.method = "GET"
                mock_request.url.path = "/api/test"
                mock_request.query_params = ""
                mock_request.client.host = "127.0.0.1"
                mock_request.headers = {'user-agent': 'test-agent'}
                mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                
                async def mock_call_next(request):
                    # Simulate some logging during request processing
                    ctx = LoggingCoordinator.get_context()
                    if ctx:
                        ctx.mark_logged("test_operation_1")
                        ctx.mark_logged("test_operation_2")
                    return Response(content="success", status_code=200)
                
                # Execute middleware
                response = await middleware.dispatch(mock_request, mock_call_next)
                
                # Verify final summary was logged
                info_calls = mock_logger.info.call_args_list
                summary_calls = [call for call in info_calls if "Request summary" in str(call)]
                
                assert len(summary_calls) >= 1
                
                # Check summary content
                summary_call = summary_calls[-1]
                summary_data = summary_call[1].get('extra', {})
                
                if summary_data:
                    assert 'correlation_id' in summary_data
                    assert 'duration_seconds' in summary_data
                    assert 'operations_logged' in summary_data


class TestMiddlewareErrorScenarios:
    """Test middleware behavior in various error scenarios."""
    
    def setup_method(self):
        """Setup for each test method."""
        request_context.set(None)
    
    def teardown_method(self):
        """Cleanup after each test method."""
        request_context.set(None)
    
    @pytest.mark.asyncio
    async def test_middleware_handles_coordinator_errors(self, mock_logger=None):
        """Test middleware handles LoggingCoordinator errors gracefully."""
        if mock_logger is None:
            mock_logger = Mock()
        
        # Mock coordinator that fails
        with patch('faultmaven.api.middleware.logging.LoggingCoordinator') as mock_coordinator_class:
            mock_coordinator = Mock()
            mock_coordinator.start_request.side_effect = Exception("Coordinator initialization failed")
            mock_coordinator_class.return_value = mock_coordinator
            
            with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
                with patch('faultmaven.api.middleware.logging.set_request_id'):
                    
                    # App should still work despite coordinator failure
                    async def working_app(request):
                        return Response(content="success", status_code=200)
                    
                    middleware = LoggingMiddleware(working_app)
                    
                    # Create mock request
                    mock_request = Mock(spec=Request)
                    mock_request.method = "GET"
                    mock_request.url.path = "/api/test"
                    mock_request.query_params = ""
                    mock_request.client = None  # Test with no client
                    mock_request.headers = {}
                    mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                    
                    async def mock_call_next(request):
                        return Response(content="success", status_code=200)
                    
                    # Should handle error gracefully and not crash
                    with pytest.raises(Exception, match="Coordinator initialization failed"):
                        await middleware.dispatch(mock_request, mock_call_next)
    
    @pytest.mark.asyncio
    async def test_middleware_missing_client_info(self):
        """Test middleware handles missing client information."""
        mock_logger = Mock()
        
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id'):
                
                async def working_app(request):
                    return Response(content="success", status_code=200)
                
                middleware = LoggingMiddleware(working_app)
                
                # Create request with missing client info
                mock_request = Mock(spec=Request)
                mock_request.method = "POST"
                mock_request.url.path = "/api/test"
                mock_request.query_params = ""
                mock_request.client = None  # No client info
                mock_request.headers = {}  # No headers
                mock_request.headers.get = lambda key, default=None: default
                
                async def mock_call_next(request):
                    return Response(content="success", status_code=200)
                
                # Should handle missing info gracefully
                response = await middleware.dispatch(mock_request, mock_call_next)
                
                assert response.status_code == 200
                
                # Should have logged with 'unknown' values
                info_calls = mock_logger.info.call_args_list
                start_calls = [call for call in info_calls if "Request started" in str(call)]
                
                assert len(start_calls) >= 1
                
                # Check that 'unknown' values were used
                start_call = start_calls[0]
                call_kwargs = start_call[1]
                assert call_kwargs.get('client_ip') == 'unknown'
                assert call_kwargs.get('user_agent') == 'unknown'
    
    @pytest.mark.asyncio
    async def test_middleware_large_request_handling(self):
        """Test middleware with large request data."""
        mock_logger = Mock()
        
        with patch('faultmaven.api.middleware.logging.get_logger', return_value=mock_logger):
            with patch('faultmaven.api.middleware.logging.set_request_id'):
                
                async def working_app(request):
                    return Response(content="success", status_code=200)
                
                middleware = LoggingMiddleware(working_app)
                
                # Create request with large query params
                large_query = "x=" + ("y" * 10000)  # Large query string
                
                mock_request = Mock(spec=Request)
                mock_request.method = "GET"
                mock_request.url.path = "/api/test"
                mock_request.query_params = large_query
                mock_request.client.host = "127.0.0.1"
                mock_request.headers = {
                    'user-agent': 'test-agent',
                    'content-length': '50000'
                }
                mock_request.headers.get = lambda key, default=None: mock_request.headers.get(key, default)
                
                async def mock_call_next(request):
                    return Response(content="success", status_code=200)
                
                # Should handle large requests without issues
                response = await middleware.dispatch(mock_request, mock_call_next)
                
                assert response.status_code == 200
                
                # Logging should still work
                assert mock_logger.info.call_count >= 2