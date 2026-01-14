"""Performance benchmarks for Vector Search Operations.

Benchmarks cover:
- Embedding generation (single and batch)
- Vector store add operations
- Semantic search performance
- Hybrid search performance
- Indexing job throughput

Performance targets:
- Embedding generation (single): <500ms p95
- Embedding generation (batch of 100): <3000ms p95
- Vector store add (single): <100ms p95
- Vector store add (batch of 100): <1000ms p95
- Semantic search (1000 items): <200ms p95
- Hybrid search (1000 items): <300ms p95
- Index item (end-to-end): <600ms p95
- Indexing job (100 items): <60000ms p95
"""

import shutil
import statistics
import tempfile
import time
from datetime import datetime, timezone
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.jobs.knowledge_indexing_job import KnowledgeIndexingJob
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    EMBEDDING_DIMENSIONS,
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.embedding_service import (
    EmbeddingService,
)
from faultmaven.modules.knowledge.domain.services.search_service import (
    KnowledgeSearchService,
)
from faultmaven.modules.knowledge.domain.services.vector_store_service import (
    VectorStoreService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    InMemoryKnowledgeItemRepository,
)

# =============================================================================
# Benchmark Utilities
# =============================================================================


def create_embedding(value: float = 0.1) -> List[float]:
    """Create test embedding vector."""
    return [value] * EMBEDDING_DIMENSIONS


def create_knowledge_item(
    item_id: str,
    organization_id: str = "org_1",
) -> KnowledgeItem:
    """Create test knowledge item."""
    return KnowledgeItem(
        item_id=item_id,
        organization_id=organization_id,
        title=f"Test Item {item_id}",
        content=f"Test content for item {item_id}. " * 10,
        item_type=KnowledgeItemType.FAQ,
        category="general",
        is_published=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class BenchmarkResult:
    """Container for benchmark results."""

    def __init__(self, name: str, times: List[float], target_p95_ms: float):
        self.name = name
        self.times = times
        self.target_p95_ms = target_p95_ms

    @property
    def min_ms(self) -> float:
        return min(self.times) * 1000

    @property
    def max_ms(self) -> float:
        return max(self.times) * 1000

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.times) * 1000

    @property
    def p95_ms(self) -> float:
        sorted_times = sorted(self.times)
        idx = int(len(sorted_times) * 0.95)
        return sorted_times[min(idx, len(sorted_times) - 1)] * 1000

    @property
    def meets_target(self) -> bool:
        return self.p95_ms <= self.target_p95_ms

    def __repr__(self) -> str:
        status = "PASS" if self.meets_target else "FAIL"
        return (
            f"{self.name}: min={self.min_ms:.2f}ms, mean={self.mean_ms:.2f}ms, "
            f"p95={self.p95_ms:.2f}ms (target: {self.target_p95_ms}ms) [{status}]"
        )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_chroma_dir():
    """Create temporary directory for ChromaDB."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_embedding_service():
    """Create mock embedding service for benchmarking."""
    service = MagicMock(spec=EmbeddingService)
    service.model = "text-embedding-3-small"
    service.dimensions = EMBEDDING_DIMENSIONS

    # Simulate realistic latency
    async def mock_generate(text):
        import asyncio

        await asyncio.sleep(0.01)  # 10ms simulated latency
        return create_embedding()

    async def mock_generate_batch(texts):
        import asyncio

        await asyncio.sleep(0.01 * len(texts) / 10)  # Batch efficiency
        return [create_embedding() for _ in texts]

    service.generate_embedding = AsyncMock(side_effect=mock_generate)
    service.generate_embeddings_batch = AsyncMock(side_effect=mock_generate_batch)
    service.get_total_tokens = MagicMock(return_value=0)
    service.health_check = AsyncMock(return_value={"api_status": "healthy"})
    return service


@pytest.fixture
def vector_store(temp_chroma_dir):
    """Create vector store for benchmarking."""
    return VectorStoreService(
        collection_name="benchmark_test",
        persist_directory=temp_chroma_dir,
    )


@pytest.fixture
def knowledge_repo():
    """Create knowledge repository."""
    return InMemoryKnowledgeItemRepository()


@pytest.fixture
def search_service(knowledge_repo, mock_embedding_service, vector_store):
    """Create search service for benchmarking."""
    return KnowledgeSearchService(
        knowledge_repo=knowledge_repo,
        embedding_service=mock_embedding_service,
        vector_store=vector_store,
    )


# =============================================================================
# Benchmark: Vector Store Add Operations
# =============================================================================


@pytest.mark.benchmark
class TestVectorStoreAddBenchmarks:
    """Benchmarks for vector store add operations."""

    @pytest.mark.asyncio
    async def test_benchmark_add_single_item(self, vector_store):
        """Benchmark: Vector store add single item.

        Target: <100ms p95
        """
        times = []
        iterations = 50

        for i in range(iterations):
            start = time.perf_counter()
            await vector_store.add_item(
                item_id=f"bench_add_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_1"},
                document=f"Test document {i}",
            )
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Vector Store Add (single)",
            times=times,
            target_p95_ms=100,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"

    @pytest.mark.asyncio
    async def test_benchmark_add_batch_100(self, vector_store):
        """Benchmark: Vector store batch add (100 items).

        Target: <1000ms p95
        """
        times = []
        iterations = 10

        for run in range(iterations):
            items = [
                {
                    "item_id": f"batch_{run}_{i}",
                    "embedding": create_embedding(),
                    "metadata": {"organization_id": "org_1"},
                    "document": f"Batch document {i}",
                }
                for i in range(100)
            ]

            start = time.perf_counter()
            await vector_store.add_items_batch(items)
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Vector Store Add Batch (100)",
            times=times,
            target_p95_ms=1000,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"


# =============================================================================
# Benchmark: Semantic Search Operations
# =============================================================================


@pytest.mark.benchmark
class TestSemanticSearchBenchmarks:
    """Benchmarks for semantic search operations."""

    @pytest.mark.asyncio
    async def test_benchmark_semantic_search_100_items(
        self, search_service, knowledge_repo, vector_store
    ):
        """Benchmark: Semantic search with 100 items.

        Target: <100ms p95
        """
        # Setup: Add 100 items
        for i in range(100):
            item = create_knowledge_item(item_id=f"search100_{i}")
            await knowledge_repo.create(item)
            await vector_store.add_item(
                item_id=item.item_id,
                embedding=create_embedding(i / 100),
                metadata={"organization_id": "org_1"},
                document=item.content,
            )

        # Benchmark
        times = []
        iterations = 50

        for _ in range(iterations):
            start = time.perf_counter()
            await search_service.semantic_search(
                query="test query",
                organization_id="org_1",
                n_results=10,
                mark_retrieved=False,
            )
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Semantic Search (100 items)",
            times=times,
            target_p95_ms=100,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"

    @pytest.mark.asyncio
    async def test_benchmark_semantic_search_1000_items(
        self, search_service, knowledge_repo, vector_store
    ):
        """Benchmark: Semantic search with 1000 items.

        Target: <200ms p95
        """
        # Setup: Add 1000 items
        for i in range(1000):
            item = create_knowledge_item(item_id=f"search1000_{i}")
            await knowledge_repo.create(item)
            await vector_store.add_item(
                item_id=item.item_id,
                embedding=create_embedding(i / 1000),
                metadata={"organization_id": "org_1"},
                document=item.content,
            )

        # Benchmark
        times = []
        iterations = 30

        for _ in range(iterations):
            start = time.perf_counter()
            await search_service.semantic_search(
                query="test query",
                organization_id="org_1",
                n_results=10,
                mark_retrieved=False,
            )
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Semantic Search (1000 items)",
            times=times,
            target_p95_ms=200,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"


# =============================================================================
# Benchmark: Hybrid Search Operations
# =============================================================================


@pytest.mark.benchmark
class TestHybridSearchBenchmarks:
    """Benchmarks for hybrid search operations."""

    @pytest.mark.asyncio
    async def test_benchmark_hybrid_search_100_items(
        self, search_service, knowledge_repo, vector_store
    ):
        """Benchmark: Hybrid search with 100 items.

        Target: <150ms p95
        """
        # Setup
        for i in range(100):
            item = create_knowledge_item(item_id=f"hybrid100_{i}")
            await knowledge_repo.create(item)
            await vector_store.add_item(
                item_id=item.item_id,
                embedding=create_embedding(i / 100),
                metadata={"organization_id": "org_1"},
                document=item.content,
            )

        # Benchmark
        times = []
        iterations = 30

        for _ in range(iterations):
            start = time.perf_counter()
            await search_service.hybrid_search(
                query="test query",
                organization_id="org_1",
                n_results=10,
                mark_retrieved=False,
            )
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Hybrid Search (100 items)",
            times=times,
            target_p95_ms=150,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"

    @pytest.mark.asyncio
    async def test_benchmark_hybrid_search_1000_items(
        self, search_service, knowledge_repo, vector_store
    ):
        """Benchmark: Hybrid search with 1000 items.

        Target: <300ms p95
        """
        # Setup
        for i in range(1000):
            item = create_knowledge_item(item_id=f"hybrid1000_{i}")
            await knowledge_repo.create(item)
            await vector_store.add_item(
                item_id=item.item_id,
                embedding=create_embedding(i / 1000),
                metadata={"organization_id": "org_1"},
                document=item.content,
            )

        # Benchmark
        times = []
        iterations = 20

        for _ in range(iterations):
            start = time.perf_counter()
            await search_service.hybrid_search(
                query="test query",
                organization_id="org_1",
                n_results=10,
                mark_retrieved=False,
            )
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Hybrid Search (1000 items)",
            times=times,
            target_p95_ms=300,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"


# =============================================================================
# Benchmark: Index Operations
# =============================================================================


@pytest.mark.benchmark
class TestIndexingBenchmarks:
    """Benchmarks for indexing operations."""

    @pytest.mark.asyncio
    async def test_benchmark_index_single_item(self, search_service, knowledge_repo):
        """Benchmark: Index single item (end-to-end).

        Target: <600ms p95
        """
        times = []
        iterations = 30

        for i in range(iterations):
            item = create_knowledge_item(item_id=f"index_bench_{i}")
            await knowledge_repo.create(item)

            start = time.perf_counter()
            await search_service.index_item(item)
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Index Item (end-to-end)",
            times=times,
            target_p95_ms=600,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"

    @pytest.mark.asyncio
    async def test_benchmark_index_batch_50(self, search_service, knowledge_repo):
        """Benchmark: Index batch of 50 items.

        Target: <5000ms p95
        """
        times = []
        iterations = 5

        for run in range(iterations):
            items = []
            for i in range(50):
                item = create_knowledge_item(item_id=f"batch_bench_{run}_{i}")
                await knowledge_repo.create(item)
                items.append(item)

            start = time.perf_counter()
            await search_service.index_items_batch(items)
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Index Batch (50 items)",
            times=times,
            target_p95_ms=5000,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"


# =============================================================================
# Benchmark: Indexing Job
# =============================================================================


@pytest.mark.benchmark
class TestIndexingJobBenchmarks:
    """Benchmarks for indexing job."""

    @pytest.mark.asyncio
    async def test_benchmark_indexing_job_100_items(
        self, search_service, knowledge_repo
    ):
        """Benchmark: Indexing job for 100 items.

        Target: <60000ms p95 (1 minute)
        """
        job = KnowledgeIndexingJob(
            knowledge_repo=knowledge_repo,
            search_service=search_service,
            batch_size=50,
        )

        times = []
        iterations = 3

        for run in range(iterations):
            # Setup: Create 100 items
            for i in range(100):
                item = create_knowledge_item(item_id=f"job_bench_{run}_{i}")
                await knowledge_repo.create(item)

            start = time.perf_counter()
            await job.run(organization_id="org_1")
            times.append(time.perf_counter() - start)

            # Cleanup for next run
            knowledge_repo.clear()

        result = BenchmarkResult(
            name="Indexing Job (100 items)",
            times=times,
            target_p95_ms=60000,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"


# =============================================================================
# Benchmark: Vector Store Delete Operations
# =============================================================================


@pytest.mark.benchmark
class TestVectorStoreDeleteBenchmarks:
    """Benchmarks for vector store delete operations."""

    @pytest.mark.asyncio
    async def test_benchmark_delete_single_item(self, vector_store):
        """Benchmark: Vector store delete single item.

        Target: <50ms p95
        """
        # Setup: Add items
        for i in range(100):
            await vector_store.add_item(
                item_id=f"del_bench_{i}",
                embedding=create_embedding(),
                metadata={"organization_id": "org_1"},
                document=f"Document {i}",
            )

        times = []
        for i in range(100):
            start = time.perf_counter()
            await vector_store.delete_item(f"del_bench_{i}")
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Vector Store Delete (single)",
            times=times,
            target_p95_ms=50,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"

    @pytest.mark.asyncio
    async def test_benchmark_delete_by_organization(self, vector_store):
        """Benchmark: Delete all items for organization.

        Target: <500ms p95
        """
        times = []
        iterations = 5

        for run in range(iterations):
            # Setup: Add 100 items
            for i in range(100):
                await vector_store.add_item(
                    item_id=f"org_del_{run}_{i}",
                    embedding=create_embedding(),
                    metadata={"organization_id": f"bench_org_{run}"},
                    document=f"Document {i}",
                )

            start = time.perf_counter()
            await vector_store.delete_by_organization(f"bench_org_{run}")
            times.append(time.perf_counter() - start)

        result = BenchmarkResult(
            name="Delete by Organization (100 items)",
            times=times,
            target_p95_ms=500,
        )
        print(f"\n{result}")
        assert result.meets_target, f"Benchmark failed: {result}"
