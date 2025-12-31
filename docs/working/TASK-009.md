# TASK-009: Knowledge Item Repository Pattern

**Phase:** Week 3, Day 1-3 (Knowledge Base Evolution)
**Priority:** P1 (RAG system foundation)
**Estimated Time:** 8-10 hours
**Dependencies:** TASK-008 (Investigation Session Repository)
**Assignee:** Developer
**Reports To:** Solutions Architect

---

## Objective

Implement the Knowledge Item Repository Pattern to manage knowledge base items for the RAG (Retrieval-Augmented Generation) system. Knowledge items represent indexed documentation, troubleshooting guides, error patterns, and solution templates.

---

## Context

The knowledge base enables AI agents to retrieve relevant context during investigations. Knowledge items are indexed documents with embeddings for vector search, supporting case deflection and agent augmentation.

**Design Reference:** [EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)

---

## Requirements

### 1. Domain Model: KnowledgeItem

**File:** `faultmaven/models/knowledge_item.py`

```python
@dataclass
class KnowledgeItem:
    """Knowledge base item for RAG system.

    Represents an indexed document or knowledge snippet with embeddings
    for semantic search and retrieval.
    """
    item_id: str
    organization_id: str
    title: str
    content: str
    item_type: KnowledgeItemType

    # Categorization
    category: Optional[str] = None  # "networking", "database", "authentication"
    tags: List[str] = field(default_factory=list)

    # Vector search
    embedding_model: str = "text-embedding-3-small"  # OpenAI model used
    embedding_vector: Optional[List[float]] = None  # 1536-dim vector
    embedding_version: int = 1  # For re-indexing on model changes

    # Metadata
    source_url: Optional[str] = None  # Original documentation URL
    author: Optional[str] = None
    language: str = "en"

    # Usage tracking
    view_count: int = 0
    helpful_count: int = 0
    not_helpful_count: int = 0
    last_retrieved_at: Optional[datetime] = None

    # Lifecycle
    is_published: bool = True
    created_at: datetime
    updated_at: datetime
    metadata: Optional[Dict[str, Any]] = None
```

**KnowledgeItemType Enum:**
```python
class KnowledgeItemType(str, Enum):
    TROUBLESHOOTING_GUIDE = "troubleshooting_guide"
    ERROR_PATTERN = "error_pattern"
    SOLUTION_TEMPLATE = "solution_template"
    API_DOCUMENTATION = "api_documentation"
    CONFIGURATION_GUIDE = "configuration_guide"
    BEST_PRACTICE = "best_practice"
    FAQ = "faq"
    RUNBOOK = "runbook"
```

**Helper Methods:**
```python
def mark_retrieved(self) -> None:
    """Record that this item was retrieved."""
    self.view_count += 1
    self.last_retrieved_at = datetime.now(timezone.utc)
    self.touch()

def mark_helpful(self) -> None:
    """Mark item as helpful."""
    self.helpful_count += 1
    self.touch()

def mark_not_helpful(self) -> None:
    """Mark item as not helpful."""
    self.not_helpful_count += 1
    self.touch()

def get_helpfulness_score(self) -> float:
    """Calculate helpfulness ratio (0.0 to 1.0)."""
    total_feedback = self.helpful_count + self.not_helpful_count
    if total_feedback == 0:
        return 0.5  # Neutral
    return self.helpful_count / total_feedback

def has_embedding(self) -> bool:
    """Check if item has an embedding vector."""
    return self.embedding_vector is not None and len(self.embedding_vector) > 0

def get_embedding_dimensions(self) -> int:
    """Get embedding vector dimensions."""
    return len(self.embedding_vector) if self.embedding_vector else 0

def touch(self) -> None:
    """Update updated_at timestamp."""
    self.updated_at = datetime.now(timezone.utc)
```

**Validation:**
- Required: `item_id`, `organization_id`, `title`, `content`, `item_type`
- `view_count`, `helpful_count`, `not_helpful_count` >= 0
- `embedding_version` >= 1
- `embedding_vector` must be 1536 dimensions if present (OpenAI text-embedding-3-small)
- `language` must be valid ISO 639-1 code

---

### 2. Database Migration

**File:** `alembic/versions/20251229_2200_006_add_knowledge_items.py`

**Table: knowledge_items**

| Column | Type | Constraints |
|--------|------|-------------|
| item_id | VARCHAR(64) | PRIMARY KEY |
| organization_id | VARCHAR(64) | NOT NULL, indexed |
| title | VARCHAR(512) | NOT NULL |
| content | TEXT | NOT NULL |
| item_type | VARCHAR(64) | NOT NULL |
| category | VARCHAR(128) | NULL |
| tags | JSONB (PostgreSQL) / TEXT (SQLite) | DEFAULT '[]' |
| embedding_model | VARCHAR(128) | NOT NULL |
| embedding_vector | VECTOR(1536) (PostgreSQL+pgvector) / TEXT (SQLite) | NULL |
| embedding_version | INTEGER | NOT NULL, DEFAULT 1 |
| source_url | VARCHAR(2048) | NULL |
| author | VARCHAR(255) | NULL |
| language | VARCHAR(8) | NOT NULL, DEFAULT 'en' |
| view_count | INTEGER | NOT NULL, DEFAULT 0 |
| helpful_count | INTEGER | NOT NULL, DEFAULT 0 |
| not_helpful_count | INTEGER | NOT NULL, DEFAULT 0 |
| last_retrieved_at | TIMESTAMPTZ | NULL |
| is_published | BOOLEAN | NOT NULL, DEFAULT TRUE |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() |
| metadata | JSONB (PostgreSQL) / TEXT (SQLite) | DEFAULT '{}' |

**Indexes:**
- `idx_knowledge_items_organization_id` (organization_id)
- `idx_knowledge_items_item_type` (item_type)
- `idx_knowledge_items_category` (category)
- `idx_knowledge_items_is_published` (is_published)
- `idx_knowledge_items_created_at` (created_at DESC)
- `idx_knowledge_items_last_retrieved_at` (last_retrieved_at DESC)
- `idx_knowledge_items_tags` (tags) using GIN (PostgreSQL only)
- **Vector index** (if pgvector available): `idx_knowledge_items_embedding_vector` using HNSW

**Check Constraints (PostgreSQL):**
```sql
CONSTRAINT knowledge_items_item_type_valid CHECK (
    item_type IN (
        'troubleshooting_guide', 'error_pattern', 'solution_template',
        'api_documentation', 'configuration_guide', 'best_practice',
        'faq', 'runbook'
    )
)
CONSTRAINT knowledge_items_view_count_non_negative CHECK (view_count >= 0)
CONSTRAINT knowledge_items_helpful_count_non_negative CHECK (helpful_count >= 0)
CONSTRAINT knowledge_items_not_helpful_count_non_negative CHECK (not_helpful_count >= 0)
CONSTRAINT knowledge_items_embedding_version_positive CHECK (embedding_version >= 1)
```

**PostgreSQL Trigger:**
Auto-update `updated_at` timestamp on UPDATE.

**pgvector Extension (PostgreSQL only):**
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

**Note:** For SQLite (development), `embedding_vector` is stored as JSON text. For production PostgreSQL with pgvector, use native `VECTOR(1536)` type with HNSW index for fast similarity search.

---

### 3. ORM Model

**File:** `faultmaven/infrastructure/persistence/models.py`

Add `KnowledgeItemModel` class:

```python
class KnowledgeItemModel(Base):
    """Knowledge item ORM model."""
    __tablename__ = "knowledge_items"

    item_id = Column(String(64), primary_key=True)
    organization_id = Column(String(64), nullable=False, index=True)
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    item_type = Column(String(64), nullable=False, index=True)
    category = Column(String(128), nullable=True, index=True)
    tags = Column(Text, nullable=False, default="[]")  # JSON array
    embedding_model = Column(String(128), nullable=False)
    embedding_vector = Column(Text, nullable=True)  # VECTOR(1536) for PostgreSQL+pgvector
    embedding_version = Column(Integer, nullable=False, default=1)
    source_url = Column(String(2048), nullable=True)
    author = Column(String(255), nullable=True)
    language = Column(String(8), nullable=False, default="en")
    view_count = Column(Integer, nullable=False, default=0)
    helpful_count = Column(Integer, nullable=False, default=0)
    not_helpful_count = Column(Integer, nullable=False, default=0)
    last_retrieved_at = Column(DateTime(timezone=True), nullable=True, index=True)
    is_published = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    knowledge_metadata = Column("metadata", Text, default="{}")

    __table_args__ = (
        CheckConstraint(
            "item_type IN ('troubleshooting_guide', 'error_pattern', 'solution_template', "
            "'api_documentation', 'configuration_guide', 'best_practice', 'faq', 'runbook')",
            name="knowledge_items_item_type_check"
        ),
        CheckConstraint("view_count >= 0", name="knowledge_items_view_count_check"),
        CheckConstraint("helpful_count >= 0", name="knowledge_items_helpful_count_check"),
        CheckConstraint("not_helpful_count >= 0", name="knowledge_items_not_helpful_count_check"),
        CheckConstraint("embedding_version >= 1", name="knowledge_items_embedding_version_check"),
    )
```

**Note:** For pgvector support, the migration should detect PostgreSQL and create the vector column type dynamically. SQLite fallback uses TEXT storage for embeddings.

---

### 4. Repository Interface

**File:** `faultmaven/infrastructure/persistence/knowledge_item_repository.py`

```python
class KnowledgeItemRepository(ABC):
    """Abstract repository for knowledge items."""

    @abstractmethod
    async def create(self, item: KnowledgeItem) -> KnowledgeItem:
        """Create a new knowledge item."""

    @abstractmethod
    async def get_by_id(self, item_id: str) -> Optional[KnowledgeItem]:
        """Get knowledge item by ID."""

    @abstractmethod
    async def update(self, item: KnowledgeItem) -> KnowledgeItem:
        """Update existing knowledge item."""

    @abstractmethod
    async def delete(self, item_id: str) -> bool:
        """Delete knowledge item by ID."""

    @abstractmethod
    async def list_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None,
        category: Optional[str] = None,
        is_published: bool = True,
        limit: int = 50,
        offset: int = 0
    ) -> List[KnowledgeItem]:
        """List knowledge items with filtering and pagination."""

    @abstractmethod
    async def search_by_text(
        self,
        organization_id: str,
        query: str,
        item_type: Optional[KnowledgeItemType] = None,
        limit: int = 10
    ) -> List[KnowledgeItem]:
        """Full-text search for knowledge items."""

    @abstractmethod
    async def search_by_tags(
        self,
        organization_id: str,
        tags: List[str],
        match_all: bool = False,
        limit: int = 50
    ) -> List[KnowledgeItem]:
        """Search knowledge items by tags.

        Args:
            match_all: If True, item must have all tags. If False, any tag matches.
        """

    @abstractmethod
    async def get_items_without_embeddings(
        self,
        organization_id: str,
        limit: int = 100
    ) -> List[KnowledgeItem]:
        """Get items that need embedding generation."""

    @abstractmethod
    async def count_by_organization_id(
        self,
        organization_id: str,
        item_type: Optional[KnowledgeItemType] = None
    ) -> int:
        """Count knowledge items for an organization."""

    @abstractmethod
    async def get_most_helpful(
        self,
        organization_id: str,
        limit: int = 10
    ) -> List[KnowledgeItem]:
        """Get most helpful items sorted by helpfulness score."""
```

**Note:** Vector similarity search will be added in TASK-010 (Vector Search Integration) as a separate method once ChromaDB integration is complete.

---

### 5. Database Implementation

**Class:** `DatabaseKnowledgeItemRepository`

**Implementation Requirements:**
- Use async SQLAlchemy with `AsyncSession`
- Map between `KnowledgeItemModel` (ORM) and `KnowledgeItem` (domain)
- Handle JSON serialization for tags, metadata, embedding_vector
- Proper error handling and logging
- Transaction management

**Key Methods:**

**`search_by_text()`:**
- PostgreSQL: Use `ts_vector` and `ts_query` for full-text search on title + content
- SQLite: Use `LIKE` operator (less efficient, but functional)
- Order by relevance (PostgreSQL) or alphabetically (SQLite)

**`search_by_tags()`:**
- Parse JSON tags array
- PostgreSQL: Use JSONB operators (`@>` for contains, `?|` for any)
- SQLite: Parse JSON and filter in-memory or use JSON_EXTRACT

**`get_items_without_embeddings()`:**
- Query where `embedding_vector IS NULL` OR `embedding_vector = ''`
- Useful for background embedding generation jobs

**`get_most_helpful()`:**
- Calculate helpfulness score: `helpful_count / (helpful_count + not_helpful_count)`
- Order by score DESC
- Filter items with minimum feedback threshold (e.g., >= 5 total votes)

---

### 6. In-Memory Implementation

**Class:** `InMemoryKnowledgeItemRepository`

**Purpose:** Testing and local development

**Implementation:**
- Store items in `Dict[str, KnowledgeItem]`
- Implement all interface methods
- Deep copy on read/write
- Text search: Simple substring matching on title/content
- Tag search: Set intersection/union
- Helpfulness calculation in-memory

---

### 7. Factory Integration

**File:** `faultmaven/infrastructure/persistence/repository_factory.py`

```python
def create_knowledge_item_repository(
    db_session: Optional[AsyncSession] = None,
    use_in_memory: bool = False
) -> KnowledgeItemRepository:
    """Create knowledge item repository."""
    if use_in_memory:
        return InMemoryKnowledgeItemRepository()
    return DatabaseKnowledgeItemRepository(db_session)
```

---

## Testing Requirements

### 1. Domain Model Tests (50+ tests)

**File:** `tests/unit/models/test_knowledge_item.py`

**Test Coverage:**
- ✅ Model creation and validation
- ✅ Required field validation
- ✅ Usage tracking methods:
  - `mark_retrieved()` - increments view_count, updates last_retrieved_at
  - `mark_helpful()` - increments helpful_count
  - `mark_not_helpful()` - increments not_helpful_count
- ✅ `get_helpfulness_score()` calculation (various ratios)
- ✅ `has_embedding()` - checks for non-empty vector
- ✅ `get_embedding_dimensions()` - returns vector length
- ✅ Edge cases:
  - Negative counts validation
  - Invalid embedding dimensions (not 1536)
  - Empty required fields
  - Invalid item_type values
  - Helpfulness score with zero feedback
- ✅ KnowledgeItemType enum (8 types)

---

### 2. Repository Unit Tests (45+ tests)

**File:** `tests/unit/infrastructure/persistence/test_knowledge_item_repository.py`

**Test Coverage:**
- ✅ CRUD operations (create, get, update, delete)
- ✅ `list_by_organization_id()` with filters (item_type, category, is_published)
- ✅ `search_by_text()` - full-text search
- ✅ `search_by_tags()` - tag-based search (match_all=True/False)
- ✅ `get_items_without_embeddings()` - finds items needing embeddings
- ✅ `count_by_organization_id()` with filters
- ✅ `get_most_helpful()` - sorted by helpfulness score
- ✅ Pagination (limit/offset)
- ✅ Error handling (not found, duplicates, etc.)
- ✅ Both implementations (Database + InMemory)
- ✅ JSON serialization (tags, metadata, embedding_vector)

---

### 3. Integration Tests (30+ tests)

**File:** `tests/integration/test_knowledge_item_integration.py`

**Critical Tests:**

**Full-Text Search:**
```python
async def test_full_text_search_relevance():
    """Test full-text search returns relevant results."""
    # Create items with different content
    # Search by query
    # Verify relevant items returned
    # Verify ordering (if PostgreSQL)
```

**Tag Search:**
```python
async def test_tag_search_match_all():
    """Test tag search with match_all=True."""
    # Create items with various tag combinations
    # Search with multiple tags, match_all=True
    # Verify only items with ALL tags returned

async def test_tag_search_match_any():
    """Test tag search with match_all=False."""
    # Search with multiple tags, match_all=False
    # Verify items with ANY tag returned
```

**Embedding Management:**
```python
async def test_items_without_embeddings():
    """Test retrieval of items needing embeddings."""
    # Create items with and without embeddings
    # Query items without embeddings
    # Verify correct items returned
```

**Helpfulness Scoring:**
```python
async def test_most_helpful_ranking():
    """Test most helpful items ranking."""
    # Create items with various helpful/not_helpful ratios
    # Query most helpful
    # Verify correct ordering by score
```

**Usage Tracking:**
```python
async def test_usage_tracking_persistence():
    """Test usage tracking persists correctly."""
    # Create item
    # Mark as retrieved, helpful
    # Reload from database
    # Verify counts and timestamps updated
```

---

### 4. Performance Benchmarks (12+ benchmarks)

**File:** `tests/benchmarks/test_knowledge_item_operations.py`

**Benchmarks:**
- Create item (target: <200ms p95)
- Retrieve item (target: <100ms p95)
- Update item (target: <150ms p95)
- List by organization (1000 items, target: <300ms p95)
- Full-text search (1000 items, target: <200ms p95)
- Tag search (1000 items, target: <200ms p95)
- Get items without embeddings (target: <150ms p95)
- Count operations (target: <100ms p95)
- Bulk create (100 items, target: <1000ms p95)

---

## Acceptance Criteria

- ✅ Domain model implemented with usage tracking methods
- ✅ Database migration with indexes and constraints
- ✅ Repository interface with 10 methods
- ✅ Database implementation (async SQLAlchemy)
- ✅ In-memory implementation (testing)
- ✅ Factory integration
- ✅ 135+ tests (50 model + 45 repository + 30 integration + 12 benchmarks)
- ✅ 80%+ test coverage
- ✅ All tests pass
- ✅ Performance benchmarks meet targets

---

## Definition of Done

- [ ] All code implemented per specification
- [ ] Database migration runs cleanly (PostgreSQL + SQLite)
- [ ] All tests passing (unit + integration + benchmarks)
- [ ] Test coverage ≥80%
- [ ] Code follows established patterns from TASK-002/003/006/007/008
- [ ] PR created with test review results
- [ ] Test-engineer approval
- [ ] Solutions-architect approval
- [ ] No regressions in existing tests

---

## Notes

**pgvector Integration:**
- Migration should detect PostgreSQL and check for pgvector extension
- If pgvector available: Use `VECTOR(1536)` column type + HNSW index
- If pgvector unavailable: Use TEXT and log warning (vector search disabled)
- SQLite always uses TEXT storage

**Embedding Strategy:**
- Items created without embeddings initially (`embedding_vector = NULL`)
- Background job uses `get_items_without_embeddings()` to generate embeddings
- Embedding generation will be implemented in TASK-010 (Vector Search Integration)

**No CASCADE Delete:**
- Knowledge items are NOT deleted when organizations are deleted
- This is intentional: knowledge persists for compliance/audit
- Soft delete via `is_published = false` is preferred

**Evolution Path:**
```
TASK-009: Knowledge Item Repository (foundation)
TASK-010: Vector Search Integration (ChromaDB + similarity search)
TASK-011: Knowledge Ingestion Pipeline (document processing + chunking)
```
