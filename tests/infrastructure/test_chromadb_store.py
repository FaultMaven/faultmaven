"""
Unit tests for ChromaDBVectorStore implementation.

Tests use a mock ChromaDB client injected via constructor —
same pattern as production (Principle 5: Composition Root).
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.models.interfaces import IVectorStore


@pytest.fixture
def mock_client():
    """Create a mock ChromaDB client."""
    client = MagicMock()
    collection = MagicMock()
    client.get_or_create_collection.return_value = collection
    return client, collection


@pytest.fixture
def vector_store(mock_client):
    """Create ChromaDBVectorStore with mock client."""
    client, collection = mock_client

    with patch(
        "faultmaven.infrastructure.persistence.chromadb_store.BaseExternalClient.call_external"
    ) as mock_call:

        async def side_effect(operation_name, call_func, **kwargs):
            return await call_func()

        mock_call.side_effect = side_effect

        store = ChromaDBVectorStore(client=client, collection_name="test_collection")
        return store, client, collection


class TestChromaDBVectorStore:
    """Test suite for ChromaDBVectorStore implementation"""

    def test_implements_ivectorstore_interface(self, mock_client):
        """Test that ChromaDBVectorStore properly implements IVectorStore interface"""
        client, _ = mock_client
        store = ChromaDBVectorStore(client=client)
        assert isinstance(store, IVectorStore)

        assert callable(store.add_documents)
        assert callable(store.search)
        assert callable(store.delete_documents)

    def test_initialization_success(self, mock_client):
        """Test successful initialization with injected client"""
        client, collection = mock_client

        store = ChromaDBVectorStore(client=client, collection_name="my_kb")

        assert store.client is client
        assert store.collection is collection
        assert store.collection_name == "my_kb"

        client.get_or_create_collection.assert_called_once_with(
            name="my_kb",
            metadata={"description": "FaultMaven knowledge base"},
        )

    def test_initialization_default_collection_name(self, mock_client):
        """Test default collection name"""
        client, _ = mock_client

        store = ChromaDBVectorStore(client=client)
        assert store.collection_name == "faultmaven_kb"

    def test_initialization_failure(self, mock_client):
        """Test initialization failure when collection creation fails"""
        client, _ = mock_client
        client.get_or_create_collection.side_effect = Exception("Connection failed")

        with pytest.raises(Exception, match="Connection failed"):
            ChromaDBVectorStore(client=client)

    @pytest.mark.asyncio
    async def test_add_documents_success(self, vector_store):
        """Test successful document addition"""
        store, client, collection = vector_store

        documents = [
            {
                "id": "doc1",
                "content": "Test content 1",
                "metadata": {"document_type": "test", "title": "Test Doc 1"},
            },
            {
                "id": "doc2",
                "content": "Test content 2",
                "metadata": {"document_type": "example", "title": "Test Doc 2"},
            },
        ]

        await store.add_documents(documents)

        collection.add.assert_called_once()
        call_args = collection.add.call_args
        assert call_args.kwargs["ids"] == ["doc1", "doc2"]
        assert call_args.kwargs["documents"] == ["Test content 1", "Test content 2"]
        assert len(call_args.kwargs["metadatas"]) == 2

    @pytest.mark.asyncio
    async def test_add_documents_with_empty_metadata(self, vector_store):
        """Test document addition with missing metadata"""
        store, client, collection = vector_store

        documents = [{"id": "doc1", "content": "Test content"}]

        await store.add_documents(documents)

        collection.add.assert_called_once_with(
            ids=["doc1"],
            documents=["Test content"],
            metadatas=[{}],
        )

    @pytest.mark.asyncio
    async def test_search_success(self, vector_store):
        """Test successful document search using explicit BGE-M3 embeddings"""
        store, client, collection = vector_store

        collection.query.return_value = {
            "ids": [["doc1", "doc2"]],
            "documents": [["Content 1", "Content 2"]],
            "metadatas": [[{"type": "test"}, {"type": "example"}]],
            "distances": [[0.1, 0.3]],
        }

        with patch("faultmaven.infrastructure.model_cache.model_cache") as mock_cache:
            mock_cache.aembed_query = AsyncMock(return_value=[0.1] * 1024)
            results = await store.search("test query", k=2)

        collection.query.assert_called_once_with(
            query_embeddings=[[0.1] * 1024],
            n_results=2,
            include=["documents", "metadatas", "distances"],
        )

        assert len(results) == 2
        assert results[0]["id"] == "doc1"
        assert results[0]["content"] == "Content 1"
        assert results[0]["score"] == 0.9  # 1.0 - 0.1
        assert results[1]["score"] == 0.7  # 1.0 - 0.3

    @pytest.mark.asyncio
    async def test_search_with_filters(self, vector_store):
        """Test search with metadata filters using explicit BGE-M3 embeddings"""
        store, client, collection = vector_store

        collection.query.return_value = {
            "ids": [[]],
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }

        with patch("faultmaven.infrastructure.model_cache.model_cache") as mock_cache:
            mock_cache.aembed_query = AsyncMock(return_value=[0.1] * 1024)
            await store.search("query", filters={"type": "runbook"})

        collection.query.assert_called_once_with(
            query_embeddings=[[0.1] * 1024],
            n_results=5,
            include=["documents", "metadatas", "distances"],
            where={"type": "runbook"},
        )

    @pytest.mark.asyncio
    async def test_search_empty_results(self, vector_store):
        """Test search with no results"""
        store, client, collection = vector_store

        collection.query.return_value = {
            "ids": [[]],
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }

        results = await store.search("nonexistent query")
        assert results == []

    @pytest.mark.asyncio
    async def test_delete_documents_success(self, vector_store):
        """Test successful document deletion"""
        store, client, collection = vector_store

        await store.delete_documents(["doc1", "doc2", "doc3"])

        collection.delete.assert_called_once_with(ids=["doc1", "doc2", "doc3"])

    @pytest.mark.asyncio
    async def test_query_by_embedding(self, vector_store):
        """Test query by pre-computed embedding"""
        store, client, collection = vector_store

        collection.query.return_value = {
            "ids": [["doc1"]],
            "documents": [["Content"]],
            "metadatas": [[{"type": "test"}]],
            "distances": [[0.05]],
        }

        results = await store.query_by_embedding(query_embedding=[0.1] * 768, top_k=3)

        collection.query.assert_called_once()
        call_args = collection.query.call_args
        assert call_args.kwargs["n_results"] == 3
        assert "query_embeddings" in call_args.kwargs

    @pytest.mark.asyncio
    async def test_error_handling(self, vector_store):
        """Test error handling in operations"""
        store, client, collection = vector_store

        collection.add.side_effect = Exception("ChromaDB error")

        with pytest.raises(Exception):
            await store.add_documents([{"id": "test", "content": "test"}])


@pytest.mark.integration
class TestChromaDBVectorStoreIntegration:
    """Integration tests"""

    def test_interface_compliance_comprehensive(self, mock_client):
        """Comprehensive test of interface compliance"""
        client, _ = mock_client
        store = ChromaDBVectorStore(client=client)

        assert hasattr(store, "add_documents")
        assert hasattr(store, "search")
        assert hasattr(store, "delete_documents")
        assert hasattr(store, "query_by_embedding")

        search_sig = inspect.signature(store.search)
        assert "query" in search_sig.parameters
        assert "k" in search_sig.parameters
        assert search_sig.parameters["k"].default == 5
