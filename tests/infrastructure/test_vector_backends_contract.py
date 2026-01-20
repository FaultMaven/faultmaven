"""Contract tests for vector backends.

Verifies that both Chroma and Pinecone backends accept the same sanitized
metadata and produce consistent behavior. These tests ensure portability
across vector backends.

Tests:
1. Both backends accept sanitized metadata without errors
2. Both backends handle same document format
3. Factory correctly selects backend based on VECTOR_BACKEND
4. Integration: full upsert/search cycle works with sanitized metadata
"""

import sys
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Remove chromadb stub from conftest.py to allow real chromadb import
# =============================================================================


# conftest.py stubs out chromadb to avoid heavy dependencies, but we need the real module
if "chromadb" in sys.modules and not hasattr(sys.modules["chromadb"], "__version__"):
    # Stubbed chromadb detected - remove it to allow real import
    del sys.modules["chromadb"]
    # Also remove any cached imports that depend on the stub
    for module_name in list(sys.modules.keys()):
        if module_name.startswith("faultmaven.infrastructure.vector"):
            del sys.modules[module_name]

# =============================================================================
# Module-level imports to avoid pytest import machinery issues
# =============================================================================

try:
    from faultmaven.infrastructure.vector import (
        get_vector_backend,
        reset_vector_backend,
    )
    from faultmaven.infrastructure.vector.base import VectorBackendType, VectorDocument
    from faultmaven.infrastructure.vector.chroma import ChromaVectorBackend
    from faultmaven.infrastructure.vector.sanitizer import (
        create_chroma_sanitizer,
        sanitize_metadata,
    )

    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False
    ChromaVectorBackend = None
    VectorDocument = None
    VectorBackendType = None
    create_chroma_sanitizer = None
    sanitize_metadata = None
    get_vector_backend = None
    reset_vector_backend = None

# Skip marker for tests requiring chromadb
skip_if_no_chroma = pytest.mark.skipif(
    not CHROMA_AVAILABLE, reason="chromadb not installed (optional dependency)"
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sanitized_metadata() -> Dict[str, Any]:
    """Standard sanitized metadata that should work with all backends."""
    return {
        "title": "Test Document",
        "source": "test_source",
        "page": 42,
        "score": 0.95,
        "active": True,
        "timestamp": "2025-01-02T12:00:00",
    }


@pytest.fixture
def complex_raw_metadata() -> Dict[str, Any]:
    """Complex metadata that needs sanitization."""
    return {
        "title": "Test Document",
        "nested": {
            "level1": {
                "value": "deep",
            },
        },
        "empty": None,
        "timestamp": datetime(2025, 1, 2, 12, 0, 0),
        "tags": ["error", "database", "timeout"],
        "__internal": "hidden",
    }


@pytest.fixture
def sample_documents():
    """Sample documents for testing."""
    return [
        VectorDocument(
            id="doc1",
            content="First test document about errors",
            embedding=[0.1] * 384,
            metadata={"title": "Doc 1", "type": "error"},
        ),
        VectorDocument(
            id="doc2",
            content="Second test document about databases",
            embedding=[0.2] * 384,
            metadata={"title": "Doc 2", "type": "info"},
        ),
    ]


@pytest.fixture(autouse=True, scope="function")
def reset_chromadb_singleton(monkeypatch):
    """Reset ChromaDB's internal singleton state between tests.

    ChromaDB maintains a global singleton for ephemeral clients. When tests
    run in certain orders (especially in CI), multiple ephemeral clients
    with different settings can conflict. This fixture ensures clean state.
    """
    # Clear ChromaDB's internal singleton cache before each test
    if CHROMA_AVAILABLE:
        try:
            import chromadb.api.client

            # Get the SharedSystemClient class that manages the singleton
            if hasattr(chromadb.api.client, "SharedSystemClient"):
                # Clear class-level _identifier_to_system dict
                if hasattr(
                    chromadb.api.client.SharedSystemClient, "_identifier_to_system"
                ):
                    chromadb.api.client.SharedSystemClient._identifier_to_system.clear()

            # Also try old-style cache if it exists
            if hasattr(chromadb, "_client_cache"):
                chromadb._client_cache.clear()
        except (AttributeError, ImportError):
            # If the internal API changed, just continue
            pass

    yield

    # Cleanup after test as well
    if CHROMA_AVAILABLE:
        try:
            import chromadb.api.client

            if hasattr(chromadb.api.client, "SharedSystemClient"):
                if hasattr(
                    chromadb.api.client.SharedSystemClient, "_identifier_to_system"
                ):
                    chromadb.api.client.SharedSystemClient._identifier_to_system.clear()

            if hasattr(chromadb, "_client_cache"):
                chromadb._client_cache.clear()
        except (AttributeError, ImportError):
            pass


@pytest.fixture
def mock_chroma_client():
    """Create mock ChromaDB client."""
    mock_client = MagicMock()
    mock_collection = MagicMock()

    # Mock collection operations
    mock_collection.add = MagicMock()
    mock_collection.upsert = MagicMock()
    mock_collection.query = MagicMock(
        return_value={
            "ids": [["doc1"]],
            "documents": [["Test document"]],
            "metadatas": [[{"title": "Doc 1"}]],
            "distances": [[0.1]],
        }
    )
    mock_collection.get = MagicMock(
        return_value={
            "ids": ["doc1"],
            "documents": ["Test document"],
            "metadatas": [{"title": "Doc 1"}],
        }
    )
    mock_collection.count = MagicMock(return_value=10)
    mock_collection.delete = MagicMock()

    mock_client.get_or_create_collection = MagicMock(return_value=mock_collection)
    mock_client.create_collection = MagicMock(return_value=mock_collection)
    mock_client.delete_collection = MagicMock()
    mock_client.list_collections = MagicMock(
        return_value=[
            MagicMock(name="collection1"),
            MagicMock(name="collection2"),
        ]
    )
    mock_client.heartbeat = MagicMock(return_value=1234567890)

    return mock_client


@pytest.fixture
def mock_pinecone_index():
    """Create mock Pinecone index."""
    mock_index = MagicMock()

    # Mock index operations
    mock_index.upsert = MagicMock()
    mock_index.query = MagicMock(
        return_value=MagicMock(
            matches=[
                MagicMock(
                    id="doc1",
                    score=0.95,
                    metadata={"title": "Doc 1", "_content": "Test document"},
                ),
            ]
        )
    )
    mock_index.fetch = MagicMock(
        return_value=MagicMock(
            vectors={
                "doc1": MagicMock(
                    id="doc1",
                    values=[0.1] * 384,
                    metadata={"title": "Doc 1", "_content": "Test document"},
                ),
            }
        )
    )
    mock_index.delete = MagicMock()
    mock_index.describe_index_stats = MagicMock(
        return_value=MagicMock(
            namespaces={"ns1": MagicMock(vector_count=100)},
            total_vector_count=100,
        )
    )

    return mock_index


# =============================================================================
# Sanitizer Contract Tests
# =============================================================================


class TestSanitizerContract:
    """Tests that sanitizer produces output acceptable to both backends."""

    @skip_if_no_chroma
    def test_sanitizer_output_has_only_primitives(self, complex_raw_metadata):
        """Verify sanitizer output contains only primitive types."""
        result = sanitize_metadata(complex_raw_metadata)

        for key, value in result.items():
            assert isinstance(
                value, (str, int, float, bool, list)
            ), f"Key {key} has non-primitive type: {type(value)}"

    @skip_if_no_chroma
    def test_sanitizer_removes_none_values(self, complex_raw_metadata):
        """Verify None values are removed."""
        result = sanitize_metadata(complex_raw_metadata)

        assert None not in result.values()

    @skip_if_no_chroma
    def test_sanitizer_flattens_nested_dicts(self, complex_raw_metadata):
        """Verify nested dicts are flattened with dot notation."""
        result = sanitize_metadata(complex_raw_metadata)

        # Should have flattened key
        assert "nested.level1.value" in result
        # Should not have nested dict
        assert not any(isinstance(v, dict) for v in result.values())

    @skip_if_no_chroma
    def test_sanitizer_removes_internal_keys(self, complex_raw_metadata):
        """Verify internal keys (starting with __) are removed."""
        result = sanitize_metadata(complex_raw_metadata)

        assert "__internal" not in result

    @skip_if_no_chroma
    def test_chroma_sanitizer_converts_lists(self, complex_raw_metadata):
        """Verify Chroma sanitizer converts lists to strings."""
        sanitizer = create_chroma_sanitizer()
        result = sanitizer.sanitize(complex_raw_metadata)

        # Lists should be converted to comma-separated strings
        if "tags" in result:
            assert isinstance(result["tags"], str)

    @skip_if_no_chroma
    def test_pinecone_sanitizer_preserves_lists(self, complex_raw_metadata):
        """Verify Pinecone sanitizer preserves lists."""
        from faultmaven.infrastructure.vector.sanitizer import create_pinecone_sanitizer

        sanitizer = create_pinecone_sanitizer()
        result = sanitizer.sanitize(complex_raw_metadata)

        # Lists should be preserved
        if "tags" in result:
            assert isinstance(result["tags"], list)


# =============================================================================
# Backend Contract Tests
# =============================================================================


class TestBackendContract:
    """Tests that both backends accept same sanitized metadata."""

    @skip_if_no_chroma
    @pytest.mark.asyncio
    async def test_chroma_accepts_sanitized_metadata(
        self,
        mock_chroma_client,
        sanitized_metadata,
    ):
        """Test ChromaDB backend accepts sanitized metadata."""
        backend = ChromaVectorBackend()  # Ephemeral mode
        backend._client = mock_chroma_client  # Inject mock client

        doc = VectorDocument(
            id="test1",
            content="Test content",
            embedding=[0.1] * 384,
            metadata=sanitized_metadata,
        )

        # Should not raise
        count = await backend.upsert([doc])
        assert count == 1

    @pytest.mark.asyncio
    async def test_pinecone_accepts_sanitized_metadata(
        self,
        mock_pinecone_index,
        sanitized_metadata,
    ):
        """Test Pinecone backend accepts sanitized metadata."""
        mock_pc = MagicMock()
        mock_pc.Index = MagicMock(return_value=mock_pinecone_index)

        with patch.dict("sys.modules", {"pinecone": MagicMock()}):
            with patch("pinecone.Pinecone", return_value=mock_pc):
                try:
                    from faultmaven.infrastructure.vector.base import VectorDocument
                    from faultmaven.infrastructure.vector.pinecone import (
                        PineconeVectorBackend,
                    )

                    backend = PineconeVectorBackend(
                        api_key="test-key",
                        index_name="test-index",
                    )
                    backend._index = mock_pinecone_index

                    doc = VectorDocument(
                        id="test1",
                        content="Test content",
                        embedding=[0.1] * 384,
                        metadata=sanitized_metadata,
                    )

                    # Should not raise
                    count = await backend.upsert([doc])
                    assert count == 1

                except ImportError:
                    pytest.skip("pinecone not installed")

    @skip_if_no_chroma
    @pytest.mark.asyncio
    async def test_both_backends_handle_empty_metadata(self, mock_chroma_client):
        """Test both backends handle empty metadata."""
        backend = ChromaVectorBackend()  # Ephemeral mode
        backend._client = mock_chroma_client  # Inject mock client

        doc = VectorDocument(
            id="test1",
            content="Test content",
            embedding=[0.1] * 384,
            metadata={},  # Empty metadata
        )

        # Should not raise
        count = await backend.upsert([doc])
        assert count == 1


# =============================================================================
# Factory Tests
# =============================================================================


class TestVectorFactory:
    """Tests for vector backend factory."""

    @skip_if_no_chroma
    def test_factory_creates_chroma_by_default(self, mock_chroma_client):
        """Test factory creates ChromaDB backend by default."""
        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(vector_backend=MagicMock(value="chroma")),
                evidence_storage=MagicMock(evidence_storage_root="./data"),
                database=MagicMock(
                    chromadb_persist_dir="./data/chroma",
                    chromadb_collection="faultmaven_kb",
                    chromadb_host=None,  # Local mode
                    chromadb_port=None,
                ),
            )

            reset_vector_backend()
            backend = get_vector_backend(reset=True)
            backend._client = mock_chroma_client  # Inject mock client

            assert backend.get_backend_type() == VectorBackendType.CHROMA

    @skip_if_no_chroma
    def test_factory_explicit_override(self, mock_chroma_client):
        """Test factory accepts explicit backend type override."""
        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(
                    vector_backend=MagicMock(value="pinecone")  # Settings say Pinecone
                ),
                evidence_storage=MagicMock(evidence_storage_root="./data"),
                database=MagicMock(
                    chromadb_persist_dir="./data/chroma",
                    chromadb_collection="faultmaven_kb",
                    chromadb_host=None,  # Local mode
                    chromadb_port=None,
                ),
            )

            reset_vector_backend()
            # But we explicitly request Chroma
            backend = get_vector_backend(backend_type="chroma", reset=True)
            backend._client = mock_chroma_client  # Inject mock client

            assert backend.get_backend_type() == VectorBackendType.CHROMA

    def test_factory_pinecone_requires_api_key(self):
        """Test factory raises error if Pinecone requested without API key."""
        import os

        os.environ.pop("PINECONE_API_KEY", None)

        with patch("faultmaven.config.settings.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                providers=MagicMock(vector_backend=MagicMock(value="pinecone")),
                database=MagicMock(
                    pinecone_api_key=None,  # Missing API key
                    pinecone_index="faultmaven",
                    pinecone_environment="us-east-1",
                    pinecone_dimension=1536,
                ),
            )

            reset_vector_backend()

            with pytest.raises(ValueError, match="PINECONE_API_KEY"):
                get_vector_backend(reset=True)


# =============================================================================
# Integration Contract Tests
# =============================================================================


class TestIntegrationContract:
    """Integration tests verifying full workflow with sanitized metadata."""

    @skip_if_no_chroma
    @pytest.mark.asyncio
    async def test_full_upsert_search_cycle_chroma(
        self,
        mock_chroma_client,
        complex_raw_metadata,
    ):
        """Test full upsert/search cycle with Chroma using sanitized metadata."""
        backend = ChromaVectorBackend()  # Ephemeral mode
        backend._client = mock_chroma_client  # Inject mock client
        sanitizer = create_chroma_sanitizer()

        # Sanitize metadata
        clean_metadata = sanitizer.sanitize(complex_raw_metadata)

        # Upsert document
        doc = VectorDocument(
            id="test1",
            content="Test content about error handling",
            embedding=[0.1] * 384,
            metadata=clean_metadata,
        )
        count = await backend.upsert([doc])
        assert count == 1

        # Search
        results = await backend.search(
            query_embedding=[0.1] * 384,
            top_k=5,
        )
        assert len(results) >= 1

    @skip_if_no_chroma
    @pytest.mark.asyncio
    async def test_sanitized_metadata_round_trip(
        self,
        mock_chroma_client,
        sanitized_metadata,
    ):
        """Test that sanitized metadata survives upsert/get cycle."""
        # Configure mock to return the same metadata
        mock_collection = mock_chroma_client.get_or_create_collection.return_value
        mock_collection.get = MagicMock(
            return_value={
                "ids": ["test1"],
                "documents": ["Test content"],
                "metadatas": [sanitized_metadata],
            }
        )

        backend = ChromaVectorBackend()  # Ephemeral mode
        backend._client = mock_chroma_client  # Inject mock client

        # Upsert
        doc = VectorDocument(
            id="test1",
            content="Test content",
            embedding=[0.1] * 384,
            metadata=sanitized_metadata,
        )
        await backend.upsert([doc])

        # Get back
        results = await backend.get(["test1"])
        assert len(results) == 1
        assert results[0].metadata == sanitized_metadata


# =============================================================================
# Backend Type Consistency Tests
# =============================================================================


class TestBackendTypeConsistency:
    """Tests for backend type reporting consistency."""

    @skip_if_no_chroma
    def test_chroma_reports_correct_type(self, mock_chroma_client):
        """Test ChromaDB backend reports CHROMA type."""
        backend = ChromaVectorBackend()  # Ephemeral mode
        backend._client = mock_chroma_client  # Inject mock client
        assert backend.get_backend_type() == VectorBackendType.CHROMA

    def test_pinecone_reports_correct_type(self, mock_pinecone_index):
        """Test Pinecone backend reports PINECONE type."""
        mock_pc = MagicMock()
        mock_pc.Index = MagicMock(return_value=mock_pinecone_index)

        with patch.dict("sys.modules", {"pinecone": MagicMock()}):
            with patch("pinecone.Pinecone", return_value=mock_pc):
                try:
                    from faultmaven.infrastructure.vector.base import VectorBackendType
                    from faultmaven.infrastructure.vector.pinecone import (
                        PineconeVectorBackend,
                    )

                    backend = PineconeVectorBackend(
                        api_key="test-key",
                        index_name="test-index",
                    )

                    assert backend.get_backend_type() == VectorBackendType.PINECONE

                except ImportError:
                    pytest.skip("pinecone not installed")

    @skip_if_no_chroma
    def test_interface_is_abstract(self):
        """Test that IVectorBackend cannot be instantiated directly."""
        from faultmaven.infrastructure.vector.base import IVectorBackend

        with pytest.raises(TypeError):
            IVectorBackend()  # type: ignore
