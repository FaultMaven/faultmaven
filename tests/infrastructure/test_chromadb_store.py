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
from faultmaven.models.vector_metadata import VectorMetadata


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
    async def test_add_documents_refuses_a_key_the_schema_would_drop(
        self, vector_store
    ):
        """An undeclared metadata key fails the write instead of vanishing.

        ``VectorMetadata`` is an allowlist, and it used to discard undeclared
        keys in silence: the writer saw a successful write, ChromaDB stored
        none of the key, and the loss surfaced much later as a wrong value at
        read time. That is exactly how ``index_runbook`` stamped four identity
        keys on every runbook and stored none of them (#912).

        Nothing may be written when a key is refused — a partial row is the
        silent-drop failure with extra steps.
        """
        store, client, collection = vector_store

        documents = [
            {
                "id": "doc1",
                "content": "Test content",
                "metadata": {"title": "Test Doc", "not_a_schema_field": "value"},
            }
        ]

        with pytest.raises(ValueError, match="not_a_schema_field"):
            await store.add_documents(documents)

        collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_documents_refusal_does_not_reach_the_circuit_breaker(
        self, mock_client
    ):
        """The refusal is raised before ``call_external``, deliberately.

        A malformed metadata dict is a programming error, not a ChromaDB
        failure. Raised inside the wrapper it would consume the retry budget on
        a deterministic failure and count towards the circuit breaker — five of
        them would open it and start failing the *healthy* KB writes sharing
        this store. Pinned by asserting the external-call machinery is never
        entered at all.
        """
        client, collection = mock_client
        store = ChromaDBVectorStore(client=client, collection_name="test_collection")

        documents = [
            {"id": "doc1", "content": "c", "metadata": {"not_a_schema_field": "v"}}
        ]

        with patch.object(
            ChromaDBVectorStore, "call_external", new=AsyncMock()
        ) as mock_call:
            with pytest.raises(ValueError, match="not_a_schema_field"):
                await store.add_documents(documents)

        mock_call.assert_not_called()

    def test_normalize_metadata_keeps_every_key_the_schema_declares(self):
        """The allowlist stores everything it declares — the refusal's other half.

        The refusal above only covers keys the schema does NOT declare. The
        opposite defect — a field declared on the model but never emitted by
        ``to_chroma_metadata`` — passes it and still drops the value on every
        write. That is not hypothetical: it is exactly the state ``report_type``
        and the runbook identity keys were in (#912).

        Asserted over EVERY field rather than a sample, because a sample only
        protects the fields someone thought to list: with eight of twenty-four
        covered, dropping ``severity`` and ``symptom_class`` from the emitter
        left the whole suite green while every KB row silently lost them. The
        coverage assertion below makes adding a schema field force a decision
        here rather than silently widening the gap.
        """
        # Every value is UNIQUE, and that is load-bearing rather than tidy: a
        # cross-wire mutation is only detectable if the two fields it confuses
        # hold different values. An earlier version of this sample reused
        # "runbook" for document_type and report_type, and "doc-77" for
        # original_document_id and parent_document_id — so swapping either pair
        # in to_chroma_metadata passed the whole suite. The uniqueness assertion
        # below keeps that from creeping back.
        sample = {
            "title": "Runbook: Connection pool exhaustion",
            "document_type": "operational_runbook",
            "tags": "postgresql,database",
            "source_url": "https://example.invalid/runbook",
            "scope": "global",
            "owner_id": "user-1",
            "organization_id": "org-a",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "domain": "database",
            "service": "postgresql",
            "last_updated": "2026-01-01",
            "status": "verified",
            "severity": "high",
            "symptom_class": "resource_exhaustion",
            "chunk_index": 0,
            "total_chunks": 3,
            "parent_document_id": "parentdoc-99",
        }

        values = [str(v) for v in sample.values()]
        assert len(set(values)) == len(values), (
            "two fields share a value, so a mutation swapping them would be "
            "invisible here — give every field a distinct one"
        )

        assert set(sample) == set(VectorMetadata.model_fields), (
            "every declared field needs a sample value here — a new field "
            "without one is a field this test does not protect"
        )

        stored = ChromaDBVectorStore._normalize_metadata(sample)

        # Proves the NORMAL path ran. If the sample ever stops validating, the
        # ValidationError fallback copies every key verbatim and would satisfy
        # the key-set assertion below while never calling to_chroma_metadata at
        # all — the test would pass having checked nothing. Only the normal path
        # comma-joins tags.
        assert stored["tags"] == "postgresql,database", (
            "sample fell through to the sanitizing fallback — this test is "
            "vacuous until the sample validates again"
        )

        missing = set(sample) - set(stored)
        assert not missing, (
            f"declared but not emitted by to_chroma_metadata: {sorted(missing)} — "
            f"these are written by callers and silently dropped"
        )

        # VALUES, not just keys. A key-presence assertion cannot tell a correct
        # emitter from one that cross-wires fields (`data["severity"] =
        # self.status`), and a declared key carrying another field's value is
        # the same "wrong value stated as fact" defect this issue is about —
        # arguably worse, since the row looks complete. The uniqueness
        # assertion above is what makes this able to see a swap at all.
        expected = dict(sample)
        expected["tags"] = "postgresql,database"  # list -> comma-joined
        expected["created_at"] = "2026-01-01T00:00:00Z"  # datetime -> ISO-8601
        expected["updated_at"] = "2026-01-02T00:00:00Z"
        assert stored == expected

    @pytest.mark.parametrize(
        "retired_key",
        [
            "report_type",
            "case_id",
            "case_title",
            "runbook_source",
            "document_title",
            "original_document_id",
        ],
    )
    def test_a_retired_runbook_identity_key_is_refused_not_dropped(self, retired_key):
        """The keys the dead runbook writer stamped now fail loudly (fm#1030).

        #912 declared these for ``RunbookKnowledgeBase.index_runbook``, a
        writer with no live caller; fm#1030 deleted that write half and
        repointed dedup at the live writer's keys. Removal is fail-loud by
        construction — ``reject_undeclared_keys`` refuses them — which is the
        safe direction: a writer that still believes in these keys finds out
        at its first write, not as a silent drop read back arbitrarily later.
        """
        with pytest.raises(ValueError, match=retired_key):
            ChromaDBVectorStore._normalize_metadata({retired_key: "value"})

    def test_normalize_metadata_degrades_rather_than_writing_a_bare_row(self):
        """The fallback keeps the allowlisted keys when a VALUE fails validation.

        This path is the guard's own fail-open surface, and it was untested:
        gutting it to `return {}` writes rows carrying no metadata at all —
        unreachable by every scoped search, scope predicate included — which is
        precisely the #912 failure class the refusal exists to prevent, arrived
        at from the other direction.

        `total_chunks` is declared `Optional[int]`; a non-numeric value fails
        validation and sends the whole dict down the fallback.
        """
        stored = ChromaDBVectorStore._normalize_metadata(
            {
                "document_type": "runbook",
                "scope": "global",
                "parent_document_id": "kb-42",
                "tags": ["postgresql", "database"],
                "owner_id": None,
                "total_chunks": "not-an-int",
            }
        )

        assert stored, "a validation failure must not erase the row's metadata"
        # The scope + type predicates are what every scoped search filters on.
        assert stored["document_type"] == "runbook"
        assert stored["scope"] == "global"
        assert stored["parent_document_id"] == "kb-42"

        # It must SANITIZE, not just copy. ChromaDB rejects list and None
        # values outright, so a fallback that passed the dict through unchanged
        # would fail the entire write — the opposite of the degradation it
        # exists to provide. Asserting only the keys above cannot tell those
        # two implementations apart.
        assert stored["tags"] == "['postgresql', 'database']"
        assert "owner_id" not in stored
        assert all(
            isinstance(v, (str, int, float, bool)) for v in stored.values()
        ), f"non-primitive survived the fallback: {stored}"

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
        # Collections use ChromaDB's default `l2` space, whose distance is
        # SQUARED euclidean, so cosine is `1 - d/2` and not `1 - d` (#1072).
        assert results[0]["score"] == 0.95  # 1.0 - 0.1 / 2
        assert results[1]["score"] == 0.85  # 1.0 - 0.3 / 2

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
