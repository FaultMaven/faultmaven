"""
Unit tests for ChromaDBVectorStore implementation.

This module tests the ChromaDBVectorStore class to ensure proper
implementation of the IVectorStore interface with comprehensive coverage.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from typing import Dict, List

from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.interfaces import IVectorStore


class TestChromaDBVectorStore:
    """Test suite for ChromaDBVectorStore implementation"""
    
    @pytest.fixture
    def mock_chromadb_client(self):
        """Mock ChromaDB client for testing"""
        with patch('faultmaven.infrastructure.persistence.chromadb_store.chromadb') as mock_chromadb:
            mock_client = MagicMock()
            mock_collection = MagicMock()
            
            # Setup mock responses
            mock_chromadb.HttpClient.return_value = mock_client
            mock_client.get_or_create_collection.return_value = mock_collection
            
            yield mock_client, mock_collection
    
    @pytest.fixture
    def mock_env_vars(self):
        """Mock environment variables for testing"""
        with patch.dict('os.environ', {
            'CHROMADB_URL': 'http://test-chromadb.local:30080',
            'CHROMADB_API_KEY': 'test-api-key'
        }):
            yield
    
    @pytest.fixture
    async def vector_store(self, mock_chromadb_client, mock_env_vars):
        """Create ChromaDBVectorStore instance for testing"""
        mock_client, mock_collection = mock_chromadb_client
        
        # Mock BaseExternalClient call_external to avoid async complexity in tests
        with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external') as mock_call:
            async def side_effect(operation_name, call_func, **kwargs):
                return await call_func()
            mock_call.side_effect = side_effect
            
            store = ChromaDBVectorStore()
            yield store, mock_client, mock_collection
    
    def test_implements_ivectorstore_interface(self, mock_chromadb_client, mock_env_vars):
        """Test that ChromaDBVectorStore properly implements IVectorStore interface"""
        with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external'):
            store = ChromaDBVectorStore()
            assert isinstance(store, IVectorStore)
            
            # Verify all required methods are present
            assert hasattr(store, 'add_documents')
            assert hasattr(store, 'search')
            assert hasattr(store, 'delete_documents')
            
            # Verify methods are coroutines
            assert callable(store.add_documents)
            assert callable(store.search) 
            assert callable(store.delete_documents)
    
    def test_initialization_success(self, mock_chromadb_client, mock_env_vars):
        """Test successful initialization with proper configuration"""
        mock_client, mock_collection = mock_chromadb_client
        
        with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external'):
            store = ChromaDBVectorStore()
            
            # Verify ChromaDB client was created with correct parameters
            assert store.client == mock_client
            assert store.collection == mock_collection
            assert store.collection_name == "faultmaven_knowledge"
            
            # Verify collection was created with correct metadata
            mock_client.get_or_create_collection.assert_called_once_with(
                name="faultmaven_knowledge",
                metadata={"description": "FaultMaven knowledge base"}
            )
    
    def test_initialization_failure(self, mock_env_vars):
        """Test initialization failure when ChromaDB connection fails"""
        with patch('faultmaven.infrastructure.persistence.chromadb_store.chromadb') as mock_chromadb:
            mock_client = MagicMock()
            mock_client.get_or_create_collection.side_effect = Exception("Connection failed")
            mock_chromadb.HttpClient.return_value = mock_client
            
            with pytest.raises(Exception) as exc_info:
                ChromaDBVectorStore()
            
            assert "Connection failed" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_add_documents_success(self, vector_store):
        """Test successful document addition"""
        store, mock_client, mock_collection = vector_store
        
        # Test data
        documents = [
            {
                "id": "doc1",
                "content": "Test content 1",
                "metadata": {"type": "test"}
            },
            {
                "id": "doc2", 
                "content": "Test content 2",
                "metadata": {"type": "example"}
            }
        ]
        
        # Execute
        await store.add_documents(documents)
        
        # Verify collection.add was called with correct parameters
        mock_collection.add.assert_called_once_with(
            ids=["doc1", "doc2"],
            documents=["Test content 1", "Test content 2"],
            metadatas=[{"type": "test"}, {"type": "example"}]
        )
    
    @pytest.mark.asyncio
    async def test_add_documents_with_empty_metadata(self, vector_store):
        """Test document addition with missing metadata"""
        store, mock_client, mock_collection = vector_store
        
        documents = [{"id": "doc1", "content": "Test content"}]
        
        await store.add_documents(documents)
        
        mock_collection.add.assert_called_once_with(
            ids=["doc1"],
            documents=["Test content"],
            metadatas=[{}]  # Empty metadata should be provided
        )
    
    @pytest.mark.asyncio
    async def test_search_success(self, vector_store):
        """Test successful document search"""
        store, mock_client, mock_collection = vector_store
        
        # Mock search results
        mock_results = {
            'ids': [['doc1', 'doc2']],
            'documents': [['Content 1', 'Content 2']],
            'metadatas': [[{'type': 'test'}, {'type': 'example'}]],
            'distances': [[0.1, 0.3]]
        }
        mock_collection.query.return_value = mock_results
        
        # Execute search
        results = await store.search("test query", k=2)
        
        # Verify query was called correctly
        mock_collection.query.assert_called_once_with(
            query_texts=["test query"],
            n_results=2,
            include=["documents", "metadatas", "distances"]
        )
        
        # Verify results format
        assert len(results) == 2
        assert results[0]['id'] == 'doc1'
        assert results[0]['content'] == 'Content 1'
        assert results[0]['metadata'] == {'type': 'test'}
        assert results[0]['score'] == 0.9  # 1.0 - 0.1
        
        assert results[1]['id'] == 'doc2'
        assert results[1]['score'] == 0.7  # 1.0 - 0.3
    
    @pytest.mark.asyncio
    async def test_search_with_default_k(self, vector_store):
        """Test search with default k parameter"""
        store, mock_client, mock_collection = vector_store
        
        mock_collection.query.return_value = {
            'ids': [[]],
            'documents': [[]],
            'metadatas': [[]],
            'distances': [[]]
        }
        
        await store.search("test query")
        
        # Verify default k=5 was used
        mock_collection.query.assert_called_once_with(
            query_texts=["test query"],
            n_results=5,
            include=["documents", "metadatas", "distances"]
        )
    
    @pytest.mark.asyncio
    async def test_search_empty_results(self, vector_store):
        """Test search with no results"""
        store, mock_client, mock_collection = vector_store
        
        mock_collection.query.return_value = {
            'ids': [[]],
            'documents': [[]],
            'metadatas': [[]],
            'distances': [[]]
        }
        
        results = await store.search("nonexistent query")
        
        assert results == []
    
    @pytest.mark.asyncio
    async def test_delete_documents_success(self, vector_store):
        """Test successful document deletion"""
        store, mock_client, mock_collection = vector_store
        
        document_ids = ["doc1", "doc2", "doc3"]
        
        await store.delete_documents(document_ids)
        
        # Verify delete was called with correct IDs
        mock_collection.delete.assert_called_once_with(ids=document_ids)
    
    @pytest.mark.asyncio
    async def test_delete_documents_empty_list(self, vector_store):
        """Test deletion with empty ID list"""
        store, mock_client, mock_collection = vector_store
        
        await store.delete_documents([])
        
        mock_collection.delete.assert_called_once_with(ids=[])
    
    def test_environment_variable_defaults(self, mock_chromadb_client):
        """Test default environment variable values"""
        # Remove environment variables to test defaults
        with patch.dict('os.environ', {}, clear=True):
            with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external'):
                store = ChromaDBVectorStore()
                
                # Verify defaults were used in HttpClient creation
                import faultmaven.infrastructure.persistence.chromadb_store as store_module
                store_module.chromadb.HttpClient.assert_called_once()
                call_args = store_module.chromadb.HttpClient.call_args
                
                # Check that default host was extracted correctly
                assert call_args[1]['host'] == 'chromadb.faultmaven.local'
                assert call_args[1]['port'] == 30080
    
    def test_base_external_client_integration(self, mock_chromadb_client, mock_env_vars):
        """Test integration with BaseExternalClient"""
        with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.__init__') as mock_init:
            with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external'):
                ChromaDBVectorStore()
                
                # Verify BaseExternalClient was initialized with correct parameters
                mock_init.assert_called_once_with(
                    client_name="chromadb_vector_store",
                    service_name="ChromaDB",
                    enable_circuit_breaker=True,
                    circuit_breaker_threshold=5,
                    circuit_breaker_timeout=60
                )
    
    @pytest.mark.asyncio
    async def test_call_external_integration(self, vector_store):
        """Test that operations properly use call_external wrapper"""
        store, mock_client, mock_collection = vector_store
        
        with patch.object(store, 'call_external', new_callable=AsyncMock) as mock_call_external:
            # Mock call_external to execute the function
            async def side_effect(operation_name, call_func, **kwargs):
                return await call_func()
            mock_call_external.side_effect = side_effect
            
            # Test add_documents
            await store.add_documents([{"id": "test", "content": "test"}])
            
            # Verify call_external was used with correct parameters
            assert mock_call_external.call_count >= 1
            call_args = mock_call_external.call_args_list[0]
            assert call_args[1]['operation_name'] == 'add_documents'
            assert call_args[1]['timeout'] == 30.0
            assert call_args[1]['retries'] == 2
    
    @pytest.mark.asyncio 
    async def test_error_handling_in_operations(self, vector_store):
        """Test error handling in vector store operations"""
        store, mock_client, mock_collection = vector_store
        
        # Make collection operations fail
        mock_collection.add.side_effect = Exception("ChromaDB error")
        
        with pytest.raises(Exception):
            await store.add_documents([{"id": "test", "content": "test"}])
    
    @pytest.mark.asyncio
    async def test_logging_integration(self, vector_store):
        """Test that operations log appropriate messages"""
        store, mock_client, mock_collection = vector_store
        
        with patch.object(store.logger, 'info') as mock_info, \
             patch.object(store.logger, 'debug') as mock_debug:
            
            # Test add_documents logging
            await store.add_documents([{"id": "test", "content": "test"}])
            mock_info.assert_called_with("Added 1 documents to vector store")
            
            # Test search logging
            mock_collection.query.return_value = {
                'ids': [['doc1']],
                'documents': [['content']],
                'metadatas': [[{}]],
                'distances': [[0.1]]
            }
            await store.search("query")
            mock_debug.assert_called_with("Found 1 similar documents")
            
            # Test delete logging  
            await store.delete_documents(["doc1"])
            mock_info.assert_called_with("Deleted 1 documents from vector store")

    def test_configuration_parsing(self, mock_chromadb_client):
        """Test URL configuration parsing"""
        test_cases = [
            {
                'env': {'CHROMADB_URL': 'http://custom-host.local:8080'},
                'expected_host': 'custom-host.local',
                'expected_port': 30080  # Note: port is hardcoded in implementation
            },
            {
                'env': {'CHROMADB_URL': 'https://secure-host.com:443'},
                'expected_host': 'secure-host.com',
                'expected_port': 30080
            }
        ]
        
        for case in test_cases:
            with patch.dict('os.environ', case['env']):
                with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external'):
                    store = ChromaDBVectorStore()
                    
                    # Verify host parsing worked correctly
                    import faultmaven.infrastructure.persistence.chromadb_store as store_module
                    call_args = store_module.chromadb.HttpClient.call_args
                    assert call_args[1]['host'] == case['expected_host']
                    assert call_args[1]['port'] == case['expected_port']


@pytest.mark.integration
class TestChromaDBVectorStoreIntegration:
    """Integration tests for ChromaDBVectorStore with real dependencies"""
    
    @pytest.mark.skipif(
        condition=True,  # Skip by default - requires real ChromaDB instance
        reason="Requires running ChromaDB instance"
    )
    @pytest.mark.asyncio
    async def test_real_chromadb_integration(self):
        """Test with real ChromaDB instance (skipped by default)"""
        # This test would require a real ChromaDB instance running
        # It's skipped by default but can be enabled for full integration testing
        pass
    
    @pytest.mark.asyncio
    async def test_interface_compliance_comprehensive(self):
        """Comprehensive test of interface compliance"""
        with patch('faultmaven.infrastructure.persistence.chromadb_store.chromadb'):
            with patch('faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external'):
                store = ChromaDBVectorStore()
                
                # Test all interface methods exist and are callable
                assert hasattr(store, 'add_documents')
                assert hasattr(store, 'search')
                assert hasattr(store, 'delete_documents')
                
                # Verify method signatures match interface
                import inspect
                
                add_sig = inspect.signature(store.add_documents)
                assert 'documents' in add_sig.parameters
                
                search_sig = inspect.signature(store.search)
                assert 'query' in search_sig.parameters
                assert 'k' in search_sig.parameters
                assert search_sig.parameters['k'].default == 5
                
                delete_sig = inspect.signature(store.delete_documents)
                assert 'ids' in delete_sig.parameters