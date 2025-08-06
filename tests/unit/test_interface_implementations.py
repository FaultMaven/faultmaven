"""
Test module for interface implementations in FaultMaven.

This module tests that all components properly implement their assigned interfaces
and that the interface methods work correctly with proper type checking and
error handling.
"""

import asyncio
import os
from contextlib import contextmanager
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.infrastructure.observability.tracing import OpikTracer
from faultmaven.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.tools.web_search import WebSearchTool
from faultmaven.models.interfaces import (
    ILLMProvider,
    ISanitizer,
    ITracer,
    BaseTool,
    ToolResult
)


class TestLLMRouterInterfaceImplementation:
    """Test LLMRouter implementation of ILLMProvider interface."""

    @pytest.fixture
    def llm_router(self):
        """Create LLMRouter instance with mocked dependencies."""
        # Mock environment variables
        with patch.dict(os.environ, {
            "FIREWORKS_API_KEY": "test-fireworks-key",
            "OPENAI_API_KEY": "test-openai-key", 
            "CHAT_PROVIDER": "fireworks"
        }):
            return LLMRouter()

    def test_llm_router_implements_illmprovider(self, llm_router):
        """Test LLMRouter properly implements ILLMProvider interface."""
        assert isinstance(llm_router, ILLMProvider)
        
        # Test interface method exists
        assert hasattr(llm_router, 'generate')
        assert callable(llm_router.generate)

    @pytest.mark.asyncio
    async def test_llm_router_generate_method(self, llm_router):
        """Test LLMRouter.generate() interface method works."""
        # Mock the route method since we're testing interface compliance
        with patch.object(llm_router, 'route') as mock_route:
            mock_response = Mock()
            mock_response.content = "Test response"
            mock_route.return_value = mock_response
            
            result = await llm_router.generate("test prompt", model="test-model")
            assert isinstance(result, str)
            assert result == "Test response"
            
            # Verify route was called with correct parameters
            mock_route.assert_called_once_with(
                prompt="test prompt",
                model="test-model",
                max_tokens=1000,
                temperature=0.7,
                data_type=None
            )

    @pytest.mark.asyncio
    async def test_llm_router_generate_with_kwargs(self, llm_router):
        """Test generate() method with various kwargs."""
        with patch.object(llm_router, 'route') as mock_route:
            mock_response = Mock()
            mock_response.content = "Response with kwargs"
            mock_route.return_value = mock_response
            
            result = await llm_router.generate(
                "test prompt",
                model="custom-model",
                max_tokens=500,
                temperature=0.3,
                data_type="log"
            )
            
            assert result == "Response with kwargs"
            mock_route.assert_called_once_with(
                prompt="test prompt",
                model="custom-model",
                max_tokens=500,
                temperature=0.3,
                data_type="log"
            )

    @pytest.mark.asyncio
    async def test_llm_router_generate_error_handling(self, llm_router):
        """Test generate() method error handling."""
        with patch.object(llm_router, 'route') as mock_route:
            mock_route.side_effect = Exception("LLM provider error")
            
            with pytest.raises(Exception, match="LLM provider error"):
                await llm_router.generate("test prompt")

    @pytest.mark.asyncio
    async def test_llm_router_route_method_still_works(self, llm_router):
        """Test existing route() method functionality is preserved."""
        with patch.object(llm_router.registry, 'route_request') as mock_registry:
            mock_response = Mock()
            mock_response.content = "Route response"
            mock_response.confidence = 0.9
            mock_registry.return_value = mock_response
            
            result = await llm_router.route("test prompt")
            assert hasattr(result, 'content')
            assert result.content == "Route response"


class TestKnowledgeBaseToolInterfaceImplementation:
    """Test KnowledgeBaseTool implementation of BaseTool interface."""

    @pytest.fixture
    def knowledge_base_tool(self):
        """Create KnowledgeBaseTool with mocked ingester."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[
            {
                "document": "Test knowledge document",
                "metadata": {"source": "test.md", "type": "guide"},
                "relevance_score": 0.9
            }
        ])
        return KnowledgeBaseTool(mock_ingester)

    def test_knowledge_base_tool_implements_basetool(self, knowledge_base_tool):
        """Test KnowledgeBaseTool properly implements BaseTool interface."""
        assert isinstance(knowledge_base_tool, BaseTool)
        
        # Test interface methods exist
        assert hasattr(knowledge_base_tool, 'execute')
        assert callable(knowledge_base_tool.execute)
        assert hasattr(knowledge_base_tool, 'get_schema')
        assert callable(knowledge_base_tool.get_schema)

    @pytest.mark.asyncio
    async def test_knowledge_base_tool_execute_method(self, knowledge_base_tool):
        """Test KnowledgeBaseTool.execute() interface method works."""
        result = await knowledge_base_tool.execute({
            "query": "test query",
            "context": {"service_name": "test-service"}
        })
        
        assert isinstance(result, ToolResult)
        assert result.success == True
        assert isinstance(result.data, str)
        assert "Test knowledge document" in result.data
        assert result.error is None

    @pytest.mark.asyncio
    async def test_knowledge_base_tool_execute_error_handling(self, knowledge_base_tool):
        """Test execute() method error handling."""
        # Test with missing query
        result = await knowledge_base_tool.execute({})
        
        assert isinstance(result, ToolResult)
        assert result.success == False
        assert result.data is None
        assert "No query provided" in result.error

    @pytest.mark.asyncio
    async def test_knowledge_base_tool_execute_empty_query(self, knowledge_base_tool):
        """Test execute() method with empty query."""
        result = await knowledge_base_tool.execute({"query": ""})
        
        assert isinstance(result, ToolResult)
        assert result.success == False
        assert "No query provided" in result.error

    def test_knowledge_base_tool_get_schema_method(self, knowledge_base_tool):
        """Test KnowledgeBaseTool.get_schema() method."""
        schema = knowledge_base_tool.get_schema()
        
        assert isinstance(schema, dict)
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        assert schema["name"] == "knowledge_base_search"
        
        # Test parameters structure
        params = schema["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert "query" in params["properties"]
        assert "required" in params
        assert "query" in params["required"]

    @pytest.mark.asyncio
    async def test_knowledge_base_tool_langchain_compatibility(self, knowledge_base_tool):
        """Test that LangChain methods still work."""
        # Test that existing _arun method works
        result = await knowledge_base_tool._arun("test query")
        assert isinstance(result, str)
        assert "Test knowledge document" in result


class TestWebSearchToolInterfaceImplementation:
    """Test WebSearchTool implementation of BaseTool interface."""

    @pytest.fixture
    def web_search_tool(self):
        """Create WebSearchTool with mocked API."""
        return WebSearchTool(
            api_key="test-api-key",
            api_endpoint="https://test-search-api.com",
            trusted_domains=["stackoverflow.com", "docs.microsoft.com"]
        )

    def test_web_search_tool_implements_basetool(self, web_search_tool):
        """Test WebSearchTool properly implements BaseTool interface."""
        assert isinstance(web_search_tool, BaseTool)
        
        # Test interface methods exist
        assert hasattr(web_search_tool, 'execute')
        assert callable(web_search_tool.execute)
        assert hasattr(web_search_tool, 'get_schema')
        assert callable(web_search_tool.get_schema)

    @pytest.mark.asyncio
    async def test_web_search_tool_execute_method(self, web_search_tool):
        """Test WebSearchTool.execute() interface method works."""
        # Mock the HTTP request
        mock_response_data = {
            "items": [
                {
                    "title": "Test Result",
                    "link": "https://stackoverflow.com/test",
                    "snippet": "Test snippet"
                }
            ]
        }
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = Mock()
            mock_response.json.return_value = mock_response_data
            mock_response.raise_for_status.return_value = None
            
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            result = await web_search_tool.execute({
                "query": "test search query",
                "context": {"phase": "formulate_hypothesis"}
            })
            
            assert isinstance(result, ToolResult)
            assert result.success == True
            assert isinstance(result.data, str)
            assert "Test Result" in result.data
            assert result.error is None

    @pytest.mark.asyncio
    async def test_web_search_tool_execute_no_api_key(self):
        """Test execute() method when no API key is configured."""
        tool = WebSearchTool()  # No API key
        
        result = await tool.execute({"query": "test query"})
        
        assert isinstance(result, ToolResult)
        assert result.success == True  # Still successful, just returns unavailable message
        assert "not available" in result.data

    @pytest.mark.asyncio
    async def test_web_search_tool_execute_error_handling(self, web_search_tool):
        """Test execute() method error handling."""
        # Test with missing query
        result = await web_search_tool.execute({})
        
        assert isinstance(result, ToolResult)
        assert result.success == False
        assert result.data is None
        assert "No query provided" in result.error

    def test_web_search_tool_get_schema_method(self, web_search_tool):
        """Test WebSearchTool.get_schema() method."""
        schema = web_search_tool.get_schema()
        
        assert isinstance(schema, dict)
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        assert schema["name"] == "web_search"
        
        # Test parameters structure
        params = schema["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert "query" in params["properties"]
        assert "required" in params
        assert "query" in params["required"]

    @pytest.mark.asyncio
    async def test_web_search_tool_langchain_compatibility(self, web_search_tool):
        """Test that LangChain methods still work."""
        # Mock HTTP request for LangChain compatibility test
        with patch('httpx.AsyncClient') as mock_client:
            mock_response = Mock()
            mock_response.json.return_value = {"items": []}
            mock_response.raise_for_status.return_value = None
            
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            # Test that existing _arun method works
            result = await web_search_tool._arun("test query")
            assert isinstance(result, str)


class TestDataSanitizerInterfaceImplementation:
    """Test DataSanitizer implementation of ISanitizer interface."""

    @pytest.fixture
    def data_sanitizer(self):
        """Create DataSanitizer instance."""
        return DataSanitizer()

    def test_data_sanitizer_implements_isanitizer(self, data_sanitizer):
        """Test DataSanitizer properly implements ISanitizer interface."""
        assert isinstance(data_sanitizer, ISanitizer)
        
        # Test interface method exists
        assert hasattr(data_sanitizer, 'sanitize')
        assert callable(data_sanitizer.sanitize)

    def test_data_sanitizer_sanitize_method(self, data_sanitizer):
        """Test DataSanitizer.sanitize() interface method works with different data types."""
        # Test with string
        result = data_sanitizer.sanitize("test string")
        assert isinstance(result, str)
        
        # Test with dict
        test_dict = {"key": "value", "nested": {"inner": "data"}}
        result = data_sanitizer.sanitize(test_dict)
        assert isinstance(result, dict)
        
        # Test with list
        test_list = ["item1", "item2", {"nested": "value"}]
        result = data_sanitizer.sanitize(test_list)
        assert isinstance(result, list)
        
        # Test with primitive types
        assert data_sanitizer.sanitize(123) == 123
        assert data_sanitizer.sanitize(True) == True
        assert data_sanitizer.sanitize(None) is None

    def test_data_sanitizer_text_sanitization_preserved(self, data_sanitizer):
        """Test existing text sanitization functionality."""
        # Test that it can still handle strings as before
        result = data_sanitizer.sanitize("Some text with potential PII")
        assert isinstance(result, str)

    def test_data_sanitizer_sensitive_data_detection(self, data_sanitizer):
        """Test sanitization of sensitive data patterns."""
        # Test API key pattern
        api_key_text = "api_key_1234567890abcdef1234567890abcdef"
        result = data_sanitizer.sanitize(api_key_text)
        assert "[API_KEY_REDACTED]" in result or api_key_text != result

    def test_data_sanitizer_complex_data_structures(self, data_sanitizer):
        """Test sanitization of complex nested data structures."""
        complex_data = {
            "user": "john_doe",
            "credentials": {
                "api_key": "api_key_1234567890abcdef1234567890abcdef",
                "tokens": ["token1", "token2"]
            },
            "logs": [
                "Normal log entry",
                {"level": "ERROR", "message": "Connection failed"}
            ]
        }
        
        result = data_sanitizer.sanitize(complex_data)
        assert isinstance(result, dict)
        assert "user" in result
        assert "credentials" in result

    def test_data_sanitizer_edge_cases(self, data_sanitizer):
        """Test sanitizer with edge cases."""
        # Empty string
        assert data_sanitizer.sanitize("") == ""
        
        # Empty dict
        assert data_sanitizer.sanitize({}) == {}
        
        # Empty list
        assert data_sanitizer.sanitize([]) == []


class TestOpikTracerInterfaceImplementation:
    """Test OpikTracer implementation of ITracer interface."""

    @pytest.fixture
    def opik_tracer(self):
        """Create OpikTracer instance."""
        return OpikTracer()

    def test_opik_tracer_implements_itracer(self, opik_tracer):
        """Test OpikTracer properly implements ITracer interface."""
        assert isinstance(opik_tracer, ITracer)
        
        # Test interface method exists
        assert hasattr(opik_tracer, 'trace')
        assert callable(opik_tracer.trace)

    def test_opik_tracer_trace_method(self, opik_tracer):
        """Test OpikTracer.trace() interface method works."""
        # Test that trace returns a context manager
        context = opik_tracer.trace("test_operation")
        assert hasattr(context, '__enter__')
        assert hasattr(context, '__exit__')
        
        # Test usage as context manager
        with context as span:
            # Should not raise any errors
            pass

    def test_opik_tracer_trace_context_manager_behavior(self, opik_tracer):
        """Test trace context manager behavior."""
        operation_name = "test_operation"
        
        # Test normal operation
        with opik_tracer.trace(operation_name) as span:
            # Span can be None if Opik is not available, which is fine
            assert span is None or hasattr(span, '__dict__')

    def test_opik_tracer_trace_with_exception(self, opik_tracer):
        """Test trace context manager handles exceptions."""
        operation_name = "test_operation_with_error"
        
        # Test that exceptions are properly handled
        try:
            with opik_tracer.trace(operation_name) as span:
                raise ValueError("Test exception")
        except ValueError:
            # Exception should propagate but context manager should handle cleanup
            pass

    def test_trace_decorator_still_works(self):
        """Test existing @trace decorator functionality."""
        from faultmaven.infrastructure.observability.tracing import trace
        
        @trace("test_operation")
        async def test_function():
            return "success"
        
        # Should be callable and decorated
        assert callable(test_function)
        # Function should have been wrapped
        assert hasattr(test_function, '__wrapped__') or test_function.__name__ != 'test_function'


class TestInterfaceBackwardCompatibility:
    """Test backward compatibility of interface implementations."""

    @pytest.fixture
    def llm_router(self):
        """Create LLMRouter for compatibility testing."""
        with patch.dict(os.environ, {
            "FIREWORKS_API_KEY": "test-key",
            "OPENAI_API_KEY": "test-key",
            "CHAT_PROVIDER": "fireworks"
        }):
            return LLMRouter()

    @pytest.fixture
    def knowledge_tool(self):
        """Create KnowledgeBaseTool for compatibility testing."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[])
        return KnowledgeBaseTool(mock_ingester)

    @pytest.fixture
    def web_search_tool(self):
        """Create WebSearchTool for compatibility testing."""
        return WebSearchTool(api_key="test-key")

    @pytest.mark.asyncio
    async def test_llm_router_backward_compatibility(self, llm_router):
        """Test LLMRouter maintains backward compatibility."""
        # Original route() method should still exist and work
        assert hasattr(llm_router, 'route')
        assert callable(llm_router.route)
        
        # New generate() method should exist
        assert hasattr(llm_router, 'generate')
        assert callable(llm_router.generate)

    def test_tools_backward_compatibility(self, knowledge_tool, web_search_tool):
        """Test tools maintain LangChain compatibility."""
        # Both tools should have LangChain methods
        for tool in [knowledge_tool, web_search_tool]:
            assert hasattr(tool, '_run')
            assert hasattr(tool, '_arun')
            assert hasattr(tool, 'name')
            assert hasattr(tool, 'description')
            
            # New interface methods
            assert hasattr(tool, 'execute')
            assert hasattr(tool, 'get_schema')

    def test_data_sanitizer_backward_compatibility(self):
        """Test DataSanitizer maintains existing functionality."""
        sanitizer = DataSanitizer()
        
        # Should still work with simple text sanitization
        text = "Some sample text"
        result = sanitizer.sanitize(text)
        assert isinstance(result, str)

    def test_tracer_backward_compatibility(self):
        """Test tracing maintains existing functionality."""
        from faultmaven.infrastructure.observability.tracing import trace, OpikTracer
        
        # Decorator should still exist
        assert callable(trace)
        
        # OpikTracer should work
        tracer = OpikTracer()
        context = tracer.trace("test")
        assert context is not None


class TestInterfaceErrorHandling:
    """Test error handling for interface implementations."""

    @pytest.mark.asyncio
    async def test_llm_router_interface_error_propagation(self):
        """Test that LLM router properly propagates errors through interface."""
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "test-key"}):
            router = LLMRouter()
            
            with patch.object(router, 'route') as mock_route:
                mock_route.side_effect = Exception("Provider failure")
                
                with pytest.raises(Exception, match="Provider failure"):
                    await router.generate("test prompt")

    @pytest.mark.asyncio
    async def test_tool_interface_error_handling(self):
        """Test that tools handle errors gracefully through interface."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(side_effect=Exception("Search failed"))
        tool = KnowledgeBaseTool(mock_ingester)
        
        result = await tool.execute({"query": "test query"})
        
        assert isinstance(result, ToolResult)
        # KnowledgeBaseTool handles errors gracefully by returning them in data instead of failing
        assert result.success == True
        assert result.data is not None
        assert "Error searching knowledge base: Search failed" in result.data

    def test_sanitizer_interface_error_handling(self):
        """Test that sanitizer handles errors gracefully."""
        sanitizer = DataSanitizer()
        
        # Should handle None gracefully
        result = sanitizer.sanitize(None)
        assert result is None
        
        # Should handle unusual data types
        class CustomObject:
            def __str__(self):
                return "custom object"
        
        custom_obj = CustomObject()
        result = sanitizer.sanitize(custom_obj)
        assert isinstance(result, str)

    def test_tracer_interface_error_handling(self):
        """Test that tracer handles errors gracefully."""
        tracer = OpikTracer()
        
        # Should work even if Opik is not available
        with tracer.trace("test_operation") as span:
            # Should not raise errors regardless of span state
            pass


class TestInterfaceMethodSignatures:
    """Test that interface method signatures are correctly implemented."""

    def test_llm_provider_interface_signature(self):
        """Test ILLMProvider.generate signature compliance."""
        router = LLMRouter()
        
        # Check that generate method accepts required parameters
        import inspect
        sig = inspect.signature(router.generate)
        
        assert 'prompt' in sig.parameters
        assert sig.parameters['prompt'].annotation == str
        
        # Should accept **kwargs
        assert any(param.kind == param.VAR_KEYWORD for param in sig.parameters.values())

    def test_base_tool_interface_signature(self):
        """Test BaseTool interface method signatures."""
        mock_ingester = Mock()
        tool = KnowledgeBaseTool(mock_ingester)
        
        import inspect
        
        # Test execute signature
        execute_sig = inspect.signature(tool.execute)
        assert 'params' in execute_sig.parameters
        assert execute_sig.parameters['params'].annotation == Dict[str, Any]
        
        # Test get_schema signature
        schema_sig = inspect.signature(tool.get_schema)
        assert schema_sig.return_annotation == Dict[str, Any]

    def test_sanitizer_interface_signature(self):
        """Test ISanitizer.sanitize signature compliance."""
        sanitizer = DataSanitizer()
        
        import inspect
        sig = inspect.signature(sanitizer.sanitize)
        
        assert 'data' in sig.parameters
        assert sig.parameters['data'].annotation == Any

    def test_tracer_interface_signature(self):
        """Test ITracer.trace signature compliance."""
        tracer = OpikTracer()
        
        import inspect
        sig = inspect.signature(tracer.trace)
        
        assert 'operation' in sig.parameters
        assert sig.parameters['operation'].annotation == str


class TestInterfaceConcurrency:
    """Test interface implementations under concurrent conditions."""

    @pytest.mark.asyncio
    async def test_llm_router_concurrent_generate_calls(self):
        """Test LLM router handles concurrent generate calls."""
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "test-key"}):
            router = LLMRouter()
            
            with patch.object(router, 'route') as mock_route:
                mock_response = Mock()
                mock_response.content = "Concurrent response"
                mock_route.return_value = mock_response
                
                # Run multiple concurrent generate calls
                tasks = [
                    router.generate(f"prompt {i}")
                    for i in range(5)
                ]
                
                results = await asyncio.gather(*tasks)
                
                assert len(results) == 5
                assert all(result == "Concurrent response" for result in results)

    @pytest.mark.asyncio
    async def test_tool_concurrent_execute_calls(self):
        """Test tools handle concurrent execute calls."""
        mock_ingester = Mock()
        mock_ingester.search = AsyncMock(return_value=[])
        tool = KnowledgeBaseTool(mock_ingester)
        
        # Run multiple concurrent execute calls
        tasks = [
            tool.execute({"query": f"query {i}"})
            for i in range(3)
        ]
        
        results = await asyncio.gather(*tasks)
        
        assert len(results) == 3
        assert all(isinstance(result, ToolResult) for result in results)

    def test_sanitizer_concurrent_calls(self):
        """Test sanitizer handles concurrent calls."""
        sanitizer = DataSanitizer()
        
        # Simulate concurrent sanitization
        import threading
        results = []
        
        def sanitize_data(data):
            result = sanitizer.sanitize(f"data {data}")
            results.append(result)
        
        threads = []
        for i in range(5):
            thread = threading.Thread(target=sanitize_data, args=(i,))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        assert len(results) == 5
        assert all(isinstance(result, str) for result in results)