# TASK-010: Vector Search Integration (ChromaDB)

**Phase:** Week 3, Day 4-5 (Vector Search Integration)
**Priority:** P1 (RAG system completion)
**Estimated Time:** 6-8 hours
**Dependencies:** TASK-009 (Knowledge Item Repository)
**Assignee:** Developer
**Reports To:** Solutions Architect

---

## Objective

Implement vector similarity search using ChromaDB to enable semantic search over knowledge base items. This completes the RAG foundation by adding embedding generation and similarity retrieval capabilities.

---

## Context

While TASK-009 created the knowledge item repository with embedding storage, this task adds:
- Embedding generation using OpenAI's text-embedding-3-small
- ChromaDB integration for fast vector similarity search
- Hybrid search combining full-text and semantic similarity
- Background job for indexing items without embeddings

**Design Reference:** [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)

---

## Requirements

### 1. Embedding Service

**File:** `faultmaven/services/embedding_service.py`

```python
class EmbeddingService:
    """Service for generating text embeddings using OpenAI."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536
    ):
        """Initialize embedding service.

        Args:
            api_key: OpenAI API key
            model: Embedding model name
            dimensions: Vector dimensions (1536 for text-embedding-3-small)
        """
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.dimensions = dimensions

    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector (1536 dimensions)

        Raises:
            EmbeddingGenerationError: If embedding generation fails
        """

    async def generate_embeddings_batch(
        self,
        texts: List[str],
        batch_size: int = 100
    ) -> List[List[float]]:
        """Generate embeddings for multiple texts in batches.

        Args:
            texts: List of texts to embed
            batch_size: Number of texts per API call

        Returns:
            List of embedding vectors
        """
```

**Error Handling:**
- Retry logic for transient API failures (max 3 retries with exponential backoff)
- Handle rate limits (429 status)
- Handle invalid input (empty text, too long text)
- Log all API calls and errors

**Token Tracking:**
- Track token usage for cost monitoring
- Expose `get_total_tokens()` method for billing

---

### 2. Vector Store Service (ChromaDB)

**File:** `faultmaven/services/vector_store_service.py`

```python
class VectorStoreService:
    """Service for managing vector embeddings using ChromaDB."""

    def __init__(
        self,
        collection_name: str = "knowledge_items",
        persist_directory: str = "./chroma_data"
    ):
        """Initialize ChromaDB client.

        Args:
            collection_name: Name of ChromaDB collection
            persist_directory: Directory for ChromaDB persistence
        """
        self.client = chromadb.PersistentClient(path=persist_directory)
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}  # Cosine similarity
        )

    async def add_item(
        self,
        item_id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str
    ) -> None:
        """Add knowledge item to vector store.

        Args:
            item_id: Unique item identifier
            embedding: Embedding vector (1536 dimensions)
            metadata: Item metadata (organization_id, item_type, category, etc.)
            document: Original text content
        """

    async def add_items_batch(
        self,
        items: List[Dict[str, Any]],
        batch_size: int = 100
    ) -> None:
        """Add multiple items in batches."""

    async def search_similar(
        self,
        query_embedding: List[float],
        organization_id: str,
        n_results: int = 10,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Search for similar items using cosine similarity.

        Args:
            query_embedding: Query embedding vector
            organization_id: Filter by organization
            n_results: Number of results to return
            filters: Additional metadata filters (item_type, category, etc.)

        Returns:
            List of results with item_id, distance, metadata
        """

    async def delete_item(self, item_id: str) -> None:
        """Delete item from vector store."""

    async def update_item(
        self,
        item_id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str
    ) -> None:
        """Update item in vector store."""

    async def get_collection_stats(self) -> Dict[str, Any]:
        """Get collection statistics (count, etc.)."""
```

**ChromaDB Configuration:**
- Use persistent client (not in-memory)
- Cosine similarity metric
- HNSW index for fast approximate nearest neighbor search
- Metadata filtering for organization isolation

---

### 3. Knowledge Search Service (Orchestration)

**File:** `faultmaven/services/knowledge_search_service.py`

This service orchestrates the full search workflow.

```python
class KnowledgeSearchService:
    """Service for searching knowledge base with hybrid search."""

    def __init__(
        self,
        knowledge_repo: KnowledgeItemRepository,
        embedding_service: EmbeddingService,
        vector_store: VectorStoreService
    ):
        """Initialize knowledge search service."""
        self.knowledge_repo = knowledge_repo
        self.embedding_service = embedding_service
        self.vector_store = vector_store

    async def semantic_search(
        self,
        query: str,
        organization_id: str,
        n_results: int = 10,
        item_type: Optional[KnowledgeItemType] = None,
        category: Optional[str] = None
    ) -> List[KnowledgeItem]:
        """Semantic search using vector similarity.

        Workflow:
        1. Generate embedding for query
        2. Search vector store for similar items
        3. Fetch full KnowledgeItem objects from repository
        4. Mark items as retrieved (usage tracking)
        5. Return sorted by similarity

        Args:
            query: Search query text
            organization_id: Organization to search within
            n_results: Number of results to return
            item_type: Optional filter by item type
            category: Optional filter by category

        Returns:
            List of KnowledgeItems sorted by similarity
        """

    async def hybrid_search(
        self,
        query: str,
        organization_id: str,
        n_results: int = 10,
        semantic_weight: float = 0.7,
        text_weight: float = 0.3
    ) -> List[KnowledgeItem]:
        """Hybrid search combining semantic + full-text search.

        Uses weighted score combination:
        - Semantic similarity: 70% (default)
        - Full-text relevance: 30% (default)

        Args:
            semantic_weight: Weight for semantic similarity (0.0-1.0)
            text_weight: Weight for full-text search (0.0-1.0)

        Returns:
            List of KnowledgeItems sorted by combined score
        """

    async def index_item(self, item: KnowledgeItem) -> None:
        """Index a knowledge item (generate embedding + add to vector store).

        Workflow:
        1. Generate embedding for item.content
        2. Update item.embedding_vector
        3. Save item to repository
        4. Add to vector store with metadata
        """

    async def reindex_item(self, item_id: str) -> None:
        """Regenerate embedding and reindex an item."""

    async def delete_item(self, item_id: str) -> None:
        """Delete item from repository and vector store."""

    async def get_indexing_stats(self) -> Dict[str, Any]:
        """Get indexing statistics (total items, indexed, pending)."""
```

---

### 4. Background Indexing Job

**File:** `faultmaven/jobs/knowledge_indexing_job.py`

```python
class KnowledgeIndexingJob:
    """Background job for indexing knowledge items without embeddings."""

    def __init__(
        self,
        knowledge_repo: KnowledgeItemRepository,
        search_service: KnowledgeSearchService,
        batch_size: int = 50
    ):
        """Initialize indexing job."""
        self.knowledge_repo = knowledge_repo
        self.search_service = search_service
        self.batch_size = batch_size

    async def run(self, organization_id: Optional[str] = None) -> Dict[str, Any]:
        """Run indexing job.

        Workflow:
        1. Fetch items without embeddings
        2. Index items in batches
        3. Track success/failure counts
        4. Return summary statistics

        Args:
            organization_id: Optional filter by organization

        Returns:
            Job statistics (processed, succeeded, failed, duration)
        """
```

**Celery Integration (Optional):**
If Celery is available, create a task wrapper:

```python
@celery_app.task
def index_knowledge_items(organization_id: Optional[str] = None):
    """Celery task for knowledge indexing."""
    job = KnowledgeIndexingJob(...)
    return asyncio.run(job.run(organization_id))
```

---

### 5. Configuration

**File:** `faultmaven/config/settings.py`

Add settings for vector search:

```python
# OpenAI Embeddings
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS: int = 1536

# ChromaDB
CHROMA_PERSIST_DIRECTORY: str = os.getenv("CHROMA_PERSIST_DIRECTORY", "./chroma_data")
CHROMA_COLLECTION_NAME: str = os.getenv("CHROMA_COLLECTION_NAME", "knowledge_items")

# Indexing
INDEXING_BATCH_SIZE: int = int(os.getenv("INDEXING_BATCH_SIZE", "50"))
```

---

### 6. Repository Extension

**File:** `faultmaven/infrastructure/persistence/knowledge_item_repository.py`

Add method to `KnowledgeItemRepository` interface:

```python
@abstractmethod
async def search_by_similarity(
    self,
    query_embedding: List[float],
    organization_id: str,
    n_results: int = 10,
    item_type: Optional[KnowledgeItemType] = None
) -> List[Tuple[KnowledgeItem, float]]:
    """Search by vector similarity (requires vector store integration).

    Returns:
        List of (KnowledgeItem, distance) tuples sorted by similarity
    """
```

This method delegates to the vector store service but provides a repository-level abstraction.

---

## Testing Requirements

### 1. Embedding Service Tests (25+ tests)

**File:** `tests/unit/services/test_embedding_service.py`

**Test Coverage:**
- ✅ `generate_embedding()` success
- ✅ `generate_embedding()` with empty text (error)
- ✅ `generate_embedding()` with very long text (chunking/error)
- ✅ `generate_embeddings_batch()` success
- ✅ Batch processing with multiple batches
- ✅ Retry logic on transient failures
- ✅ Rate limit handling (429 error)
- ✅ Token usage tracking
- ✅ API error handling (400, 401, 500)
- ✅ Mock OpenAI client for unit tests

---

### 2. Vector Store Service Tests (30+ tests)

**File:** `tests/unit/services/test_vector_store_service.py`

**Test Coverage:**
- ✅ `add_item()` success
- ✅ `add_items_batch()` success
- ✅ `search_similar()` returns relevant results
- ✅ `search_similar()` with organization filter
- ✅ `search_similar()` with metadata filters (item_type, category)
- ✅ `delete_item()` success
- ✅ `update_item()` success
- ✅ `get_collection_stats()` returns correct counts
- ✅ Cosine similarity metric verification
- ✅ HNSW index performance
- ✅ Empty collection handling
- ✅ Duplicate item handling (update vs create)

---

### 3. Knowledge Search Service Tests (35+ tests)

**File:** `tests/unit/services/test_knowledge_search_service.py`

**Test Coverage:**
- ✅ `semantic_search()` full workflow
- ✅ `semantic_search()` with filters
- ✅ `semantic_search()` marks items as retrieved
- ✅ `hybrid_search()` combines scores correctly
- ✅ `hybrid_search()` weight parameters work
- ✅ `index_item()` generates embedding and indexes
- ✅ `reindex_item()` regenerates embedding
- ✅ `delete_item()` removes from both repo and vector store
- ✅ `get_indexing_stats()` returns correct counts
- ✅ Error handling (embedding failure, vector store failure)
- ✅ Mock dependencies (embedding service, vector store)

---

### 4. Integration Tests (25+ tests)

**File:** `tests/integration/test_vector_search_integration.py`

**Critical Tests:**

**End-to-End Semantic Search:**
```python
async def test_e2e_semantic_search():
    """Test complete semantic search workflow."""
    # Create knowledge items
    # Index items (generate embeddings + add to vector store)
    # Search by query
    # Verify relevant results returned
    # Verify similarity scores
```

**Hybrid Search:**
```python
async def test_hybrid_search_combines_results():
    """Test hybrid search merges semantic + text results."""
    # Create items with varying semantic/text relevance
    # Search with hybrid search
    # Verify both types of results included
    # Verify score combination correct
```

**Indexing Job:**
```python
async def test_indexing_job_processes_unindexed_items():
    """Test background indexing job."""
    # Create items without embeddings
    # Run indexing job
    # Verify all items now have embeddings
    # Verify items added to vector store
```

**Organization Isolation:**
```python
async def test_search_filters_by_organization():
    """Test organization-level isolation in search."""
    # Create items for multiple organizations
    # Search for org A
    # Verify only org A results returned
```

---

### 5. Performance Benchmarks (10+ benchmarks)

**File:** `tests/benchmarks/test_vector_search_operations.py`

**Benchmarks:**
- Embedding generation (single, target: <500ms p95)
- Embedding generation (batch of 100, target: <3000ms p95)
- Vector store add (single, target: <100ms p95)
- Vector store add (batch of 100, target: <1000ms p95)
- Semantic search (1000 items, target: <200ms p95)
- Hybrid search (1000 items, target: <300ms p95)
- Index item (end-to-end, target: <600ms p95)
- Indexing job (100 items, target: <60000ms p95)

---

## Dependencies

**New Python Packages:**

```toml
# pyproject.toml
[tool.poetry.dependencies]
chromadb = "^0.4.22"  # Vector database
openai = "^1.10.0"    # OpenAI embeddings
```

**System Dependencies:**
- None (ChromaDB uses embedded SQLite + HNSW)

---

## Acceptance Criteria

- ✅ Embedding service implemented with retry logic and error handling
- ✅ ChromaDB integration with cosine similarity and HNSW index
- ✅ Knowledge search service with semantic and hybrid search
- ✅ Background indexing job for items without embeddings
- ✅ Repository extension for similarity search
- ✅ Configuration settings for OpenAI and ChromaDB
- ✅ 125+ tests (25 embedding + 30 vector store + 35 search + 25 integration + 10 benchmarks)
- ✅ 80%+ test coverage
- ✅ All tests pass
- ✅ Performance benchmarks meet targets

---

## Definition of Done

- [ ] All code implemented per specification
- [ ] OpenAI embeddings working (requires API key)
- [ ] ChromaDB persistence working
- [ ] All tests passing (unit + integration + benchmarks)
- [ ] Test coverage ≥80%
- [ ] Code follows established patterns
- [ ] PR created with test review results
- [ ] Test-engineer approval
- [ ] Solutions-architect approval
- [ ] No regressions in existing tests
- [ ] Documentation updated (configuration guide)

---

## Notes

**OpenAI API Key:**
- Required for embedding generation
- Set via `OPENAI_API_KEY` environment variable
- Tests should use mocked OpenAI client (not real API calls)
- Integration tests may use real API (optional, with cost warnings)

**ChromaDB Persistence:**
- Uses local filesystem persistence by default (`./chroma_data`)
- For production: consider dedicated ChromaDB server or cloud hosting
- Collection is created on first use (idempotent)

**Cost Monitoring:**
- Track OpenAI API token usage
- Log embedding generation counts
- Expose metrics for monitoring

**Hybrid Search Strategy:**
- Default weights: 70% semantic, 30% text
- Configurable per-query
- Score normalization required for fair combination

**Evolution Path:**
```
TASK-009: Knowledge Item Repository (foundation) ✅
TASK-010: Vector Search Integration (ChromaDB) ← Current
TASK-011: Knowledge Ingestion Pipeline (document processing + chunking)
```

**No Database Migration Required:**
- All vector storage in ChromaDB (separate from PostgreSQL)
- Knowledge items already have `embedding_vector` field (TASK-009)
- This task purely adds service layer on top of existing schema
