import pytest
import inspect
import asyncio
from abc import ABC
from typing import Any, Dict, Optional, List, ContextManager
from unittest.mock import Mock, AsyncMock
from contextlib import contextmanager

from faultmaven.models.interfaces import (
    ToolResult,
    BaseTool,
    ILLMProvider,
    ITracer,
    ISanitizer,
    IVectorStore,
    ISessionStore
)
from pydantic import ValidationError


def test_interfaces_exist():
    """Verify all interfaces are properly defined"""
    # Tool interfaces
    assert hasattr(BaseTool, 'execute')
    assert hasattr(BaseTool, 'get_schema')
    
    # Infrastructure interfaces
    assert hasattr(ILLMProvider, 'generate')
    assert hasattr(ITracer, 'trace')
    assert hasattr(ISanitizer, 'sanitize')
    assert hasattr(IVectorStore, 'add_documents')
    assert hasattr(IVectorStore, 'search')
    assert hasattr(ISessionStore, 'get')
    assert hasattr(ISessionStore, 'set')


def test_tool_result_model():
    """Test ToolResult Pydantic model"""
    # Test successful result
    result = ToolResult(success=True, data={"key": "value"})
    assert result.success is True
    assert result.data == {"key": "value"}
    assert result.error is None
    
    # Test failed result
    result = ToolResult(success=False, data=None, error="Test error")
    assert result.success is False
    assert result.data is None
    assert result.error == "Test error"
    
    # Test with different data types
    result = ToolResult(success=True, data=[1, 2, 3])
    assert result.data == [1, 2, 3]
    
    result = ToolResult(success=True, data="string data")
    assert result.data == "string data"
    
    result = ToolResult(success=True, data=42)
    assert result.data == 42
    
    # Test model validation
    with pytest.raises(ValidationError):
        ToolResult()  # Missing required fields
    
    with pytest.raises(ValidationError):
        ToolResult(success="not a boolean", data="test")  # Invalid type
    
    # Test serialization
    result = ToolResult(success=True, data={"nested": {"value": 123}})
    serialized = result.model_dump()
    assert serialized == {
        "success": True,
        "data": {"nested": {"value": 123}},
        "error": None
    }
    
    # Test from dict
    result_from_dict = ToolResult.model_validate({
        "success": False,
        "data": "some data",
        "error": "Something went wrong"
    })
    assert result_from_dict.success is False
    assert result_from_dict.data == "some data"
    assert result_from_dict.error == "Something went wrong"


def test_abstract_classes_cannot_be_instantiated():
    """Verify that abstract base classes cannot be instantiated directly"""
    from faultmaven.models.interfaces import (
        BaseTool, ILLMProvider, ITracer, ISanitizer, IVectorStore, ISessionStore
    )
    
    # All these should raise TypeError when trying to instantiate
    with pytest.raises(TypeError):
        BaseTool()
    
    with pytest.raises(TypeError):
        ILLMProvider()
    
    with pytest.raises(TypeError):
        ITracer()
    
    with pytest.raises(TypeError):
        ISanitizer()
    
    with pytest.raises(TypeError):
        IVectorStore()
    
    with pytest.raises(TypeError):
        ISessionStore()


def test_interface_methods_are_abstract():
    """Verify that all interface methods are properly marked as abstract"""
    # Check BaseTool has abstract methods
    assert hasattr(BaseTool, '__abstractmethods__')
    assert 'execute' in BaseTool.__abstractmethods__
    assert 'get_schema' in BaseTool.__abstractmethods__
    
    # Check ILLMProvider has abstract methods
    assert hasattr(ILLMProvider, '__abstractmethods__')
    assert 'generate' in ILLMProvider.__abstractmethods__
    
    # Check ITracer has abstract methods
    assert hasattr(ITracer, '__abstractmethods__')
    assert 'trace' in ITracer.__abstractmethods__
    
    # Check ISanitizer has abstract methods
    assert hasattr(ISanitizer, '__abstractmethods__')
    assert 'sanitize' in ISanitizer.__abstractmethods__
    
    # Check IVectorStore has abstract methods
    assert hasattr(IVectorStore, '__abstractmethods__')
    assert 'add_documents' in IVectorStore.__abstractmethods__
    assert 'search' in IVectorStore.__abstractmethods__
    
    # Check ISessionStore has abstract methods
    assert hasattr(ISessionStore, '__abstractmethods__')
    assert 'get' in ISessionStore.__abstractmethods__
    assert 'set' in ISessionStore.__abstractmethods__


def test_all_interfaces_importable():
    """Test that all interfaces can be imported successfully"""
    try:
        from faultmaven.models.interfaces import (
            ToolResult,
            BaseTool,
            ILLMProvider,
            ITracer,
            ISanitizer,
            IVectorStore,
            ISessionStore
        )
        # If we get here without exception, the import was successful
        assert True
    except ImportError as e:
        pytest.fail(f"Failed to import interfaces: {e}")


def test_infrastructure_interfaces_backward_compatibility():
    """Test that interfaces can be imported from infrastructure module for backward compatibility"""
    try:
        from faultmaven.infrastructure.interfaces import (
            ILLMProvider,
            ITracer,
            ISanitizer,
            IVectorStore,
            ISessionStore
        )
        # Verify they are the same classes
        from faultmaven.models.interfaces import (
            ILLMProvider as ModelsILLMProvider,
            ITracer as ModelsITracer,
            ISanitizer as ModelsISanitizer,
            IVectorStore as ModelsIVectorStore,
            ISessionStore as ModelsISessionStore
        )
        
        assert ILLMProvider is ModelsILLMProvider
        assert ITracer is ModelsITracer
        assert ISanitizer is ModelsISanitizer
        assert IVectorStore is ModelsIVectorStore
        assert ISessionStore is ModelsISessionStore
        
    except ImportError as e:
        pytest.fail(f"Failed to import interfaces from infrastructure module: {e}")


def test_interface_inheritance():
    """Test that all interfaces properly inherit from ABC"""
    assert issubclass(BaseTool, ABC)
    assert issubclass(ILLMProvider, ABC)
    assert issubclass(ITracer, ABC)
    assert issubclass(ISanitizer, ABC)
    assert issubclass(IVectorStore, ABC)
    assert issubclass(ISessionStore, ABC)


def test_interface_method_signatures():
    """Test that interface methods have correct signatures"""
    # Test BaseTool method signatures
    execute_sig = inspect.signature(BaseTool.execute)
    assert 'params' in execute_sig.parameters
    assert execute_sig.parameters['params'].annotation == Dict[str, Any]
    assert execute_sig.return_annotation == ToolResult
    
    schema_sig = inspect.signature(BaseTool.get_schema)
    assert schema_sig.return_annotation == Dict[str, Any]
    
    # Test ILLMProvider method signatures
    generate_sig = inspect.signature(ILLMProvider.generate)
    assert 'prompt' in generate_sig.parameters
    assert generate_sig.parameters['prompt'].annotation == str
    assert generate_sig.return_annotation == str
    
    # Test ITracer method signatures
    trace_sig = inspect.signature(ITracer.trace)
    assert 'operation' in trace_sig.parameters
    assert trace_sig.parameters['operation'].annotation == str
    assert trace_sig.return_annotation == ContextManager
    
    # Test ISanitizer method signatures
    sanitize_sig = inspect.signature(ISanitizer.sanitize)
    assert 'data' in sanitize_sig.parameters
    assert sanitize_sig.parameters['data'].annotation == Any
    assert sanitize_sig.return_annotation == Any
    
    # Test IVectorStore method signatures
    add_docs_sig = inspect.signature(IVectorStore.add_documents)
    assert 'documents' in add_docs_sig.parameters
    assert add_docs_sig.parameters['documents'].annotation == List[Dict]
    
    search_sig = inspect.signature(IVectorStore.search)
    assert 'query' in search_sig.parameters
    assert search_sig.parameters['query'].annotation == str
    assert 'k' in search_sig.parameters
    assert search_sig.parameters['k'].annotation == int
    assert search_sig.parameters['k'].default == 5
    assert search_sig.return_annotation == List[Dict]
    
    # Test ISessionStore method signatures
    get_sig = inspect.signature(ISessionStore.get)
    assert 'key' in get_sig.parameters
    assert get_sig.parameters['key'].annotation == str
    assert get_sig.return_annotation == Optional[Dict]
    
    set_sig = inspect.signature(ISessionStore.set)
    assert 'key' in set_sig.parameters
    assert set_sig.parameters['key'].annotation == str
    assert 'value' in set_sig.parameters
    assert set_sig.parameters['value'].annotation == Dict
    assert 'ttl' in set_sig.parameters
    assert set_sig.parameters['ttl'].annotation == Optional[int]
    assert set_sig.parameters['ttl'].default is None


def test_concrete_implementations():
    """Test that concrete implementations can be created and work properly"""
    
    # Test concrete BaseTool implementation
    class ConcreteTool(BaseTool):
        async def execute(self, params: Dict[str, Any]) -> ToolResult:
            return ToolResult(success=True, data=params.get('result', 'test'))
        
        def get_schema(self) -> Dict[str, Any]:
            return {'name': 'test_tool', 'description': 'A test tool'}
    
    tool = ConcreteTool()
    assert asyncio.run(tool.execute({'result': 'success'})).success
    assert tool.get_schema()['name'] == 'test_tool'
    
    # Test concrete ILLMProvider implementation
    class ConcreteLLMProvider(ILLMProvider):
        async def generate(self, prompt: str, **kwargs) -> str:
            return f"Response to: {prompt}"
    
    provider = ConcreteLLMProvider()
    result = asyncio.run(provider.generate("test prompt"))
    assert result == "Response to: test prompt"
    
    # Test concrete ITracer implementation
    class ConcreteTracer(ITracer):
        def trace(self, operation: str) -> ContextManager:
            @contextmanager
            def trace_context():
                yield f"tracing_{operation}"
            return trace_context()
    
    tracer = ConcreteTracer()
    with tracer.trace("test_operation") as span:
        assert span == "tracing_test_operation"
    
    # Test concrete ISanitizer implementation
    class ConcreteSanitizer(ISanitizer):
        def sanitize(self, data: Any) -> Any:
            if isinstance(data, str):
                return data.replace('sensitive', '[REDACTED]')
            return data
    
    sanitizer = ConcreteSanitizer()
    assert sanitizer.sanitize("sensitive data") == "[REDACTED] data"
    assert sanitizer.sanitize(42) == 42
    
    # Test concrete IVectorStore implementation
    class ConcreteVectorStore(IVectorStore):
        def __init__(self):
            self.documents = []
        
        async def add_documents(self, documents: List[Dict]) -> None:
            self.documents.extend(documents)
        
        async def search(self, query: str, k: int = 5) -> List[Dict]:
            return self.documents[:k]
    
    store = ConcreteVectorStore()
    docs = [{'id': 1, 'text': 'test doc'}]
    asyncio.run(store.add_documents(docs))
    results = asyncio.run(store.search('test', k=1))
    assert len(results) == 1
    assert results[0]['id'] == 1
    
    # Test concrete ISessionStore implementation
    class ConcreteSessionStore(ISessionStore):
        def __init__(self):
            self.store = {}
        
        async def get(self, key: str) -> Optional[Dict]:
            return self.store.get(key)
        
        async def set(self, key: str, value: Dict, ttl: Optional[int] = None) -> None:
            self.store[key] = value
    
    session_store = ConcreteSessionStore()
    asyncio.run(session_store.set('test_key', {'data': 'value'}))
    result = asyncio.run(session_store.get('test_key'))
    assert result == {'data': 'value'}
    
    # Test non-existent key
    result = asyncio.run(session_store.get('non_existent'))
    assert result is None


def test_incomplete_implementations_fail():
    """Test that incomplete concrete implementations cannot be instantiated"""
    
    # Test incomplete BaseTool (missing get_schema)
    class IncompleteTool(BaseTool):
        async def execute(self, params: Dict[str, Any]) -> ToolResult:
            return ToolResult(success=True, data={})
        # Missing get_schema method
    
    with pytest.raises(TypeError):
        IncompleteTool()
    
    # Test incomplete ILLMProvider (missing generate)
    class IncompleteLLMProvider(ILLMProvider):
        # Missing generate method
        pass
    
    with pytest.raises(TypeError):
        IncompleteLLMProvider()
    
    # Test incomplete IVectorStore (missing one method)
    class IncompleteVectorStore(IVectorStore):
        async def add_documents(self, documents: List[Dict]) -> None:
            pass
        # Missing search method
    
    with pytest.raises(TypeError):
        IncompleteVectorStore()


def test_interface_docstrings():
    """Test that all interfaces and their methods have proper docstrings"""
    interfaces = [BaseTool, ILLMProvider, ITracer, ISanitizer, IVectorStore, ISessionStore]
    
    for interface in interfaces:
        # Check interface has docstring
        assert interface.__doc__ is not None
        assert len(interface.__doc__.strip()) > 0
        
        # Check all abstract methods have docstrings
        for method_name in interface.__abstractmethods__:
            method = getattr(interface, method_name)
            assert method.__doc__ is not None
            assert len(method.__doc__.strip()) > 0


def test_tool_result_edge_cases():
    """Test ToolResult with edge cases and special values"""
    # Test with None data
    result = ToolResult(success=True, data=None)
    assert result.data is None
    
    # Test with empty containers
    result = ToolResult(success=True, data=[])
    assert result.data == []
    
    result = ToolResult(success=True, data={})
    assert result.data == {}
    
    # Test with complex nested data
    complex_data = {
        'list': [1, 2, {'nested': True}],
        'dict': {'a': 1, 'b': [2, 3]},
        'none_value': None,
        'boolean': False
    }
    result = ToolResult(success=True, data=complex_data)
    assert result.data == complex_data
    
    # Test error field validation
    result = ToolResult(success=False, data=None, error="")
    assert result.error == ""
    
    # Test JSON serialization and deserialization
    result = ToolResult(success=True, data={'test': 'value'}, error=None)
    json_data = result.model_dump_json()
    reconstructed = ToolResult.model_validate_json(json_data)
    assert reconstructed.success == result.success
    assert reconstructed.data == result.data
    assert reconstructed.error == result.error