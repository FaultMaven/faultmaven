# Working Memory Feature: Session-Specific RAG

**Version**: 1.0
**Status**: Implemented
**Last Updated**: 2025-10-16

---

## Overview

The **Working Memory** feature implements Session-Specific RAG (Retrieval-Augmented Generation) for handling detailed follow-up questions about user-uploaded documents. It uses a dedicated QA sub-agent with a separate synthesis LLM to prevent context pollution in the main diagnostic agent.

---

## Architecture

### Components

```
User Upload (PDF/logs/config)
    ↓
[CaseVectorStore]
    ↓ (creates)
ChromaDB Collection: case_{case_id}
    ↓ (stores chunks)
Document Embeddings (BGE-M3)
    ↓
[answer_from_document Tool]
    ↓ (retrieves chunks)
Semantic Search (top-k=5)
    ↓
[QA Sub-Agent]
    ↓ (synthesis call)
Synthesis LLM (SYNTHESIS_PROVIDER)
    ↓
Concise Answer with Citations
```

### Key Design Decisions

1. **Separate Collections Per Case**
   - Each case gets its own ChromaDB collection: `case_{case_id}`
   - Isolated from global knowledge base
   - Automatically cleaned up after 7 days (TTL)

2. **Dedicated Synthesis LLM**
   - Uses `SYNTHESIS_PROVIDER` config (separate from `CHAT_PROVIDER`)
   - Recommended: Fast, cost-effective models (gpt-4o-mini, claude-haiku, llama-3.1-8b)
   - Prevents pollution of main agent's context

3. **TTL-Based Cleanup**
   - Background scheduler runs every 6 hours
   - Deletes collections older than 7 days
   - No manual cleanup required

---

## Configuration

### Environment Variables

```bash
# Synthesis provider for QA sub-agent (answer_from_document tool)
# Used for RAG-based question answering on uploaded documents
# If not specified, falls back to CHAT_PROVIDER
# Recommended: Fast, cost-effective models (gpt-4o-mini, claude-3-haiku, llama-3.1-8b)
SYNTHESIS_PROVIDER=openai

# ChromaDB Configuration (used by CaseVectorStore)
CHROMADB_HOST=chromadb.faultmaven.local
CHROMADB_PORT=30080
CHROMADB_URL=http://chromadb.faultmaven.local:30080
```

### Code Configuration

**Case Vector Store TTL** (in [container.py](../../faultmaven/container.py)):
```python
self.case_vector_store = CaseVectorStore(ttl_days=7)
```

**Cleanup Scheduler Interval** (in [main.py](../../faultmaven/main.py)):
```python
case_cleanup_scheduler = start_case_cleanup_scheduler(
    case_vector_store=case_vector_store,
    interval_hours=6  # Run cleanup every 6 hours
)
```

---

## Usage Examples

### 1. Upload Document and Ask Questions

```python
# User uploads server.log to case abc123
# → Creates ChromaDB collection: case_abc123
# → Chunks document using preprocessing pipeline
# → Stores chunks with embeddings

# User asks: "What error occurred at 10:45 AM?"
result = await answer_from_document_tool.answer_question(
    case_id="abc123",
    question="What error occurred at 10:45 AM?",
    k=5  # Retrieve top 5 chunks
)

# Returns:
{
    "answer": "At 10:45 AM, a ConnectionRefusedError occurred when trying to connect to database server at 192.168.1.100:5432. The error message was: 'Connection refused by server'.",
    "sources": ["server.log"],
    "chunk_count": 5,
    "confidence": 0.87
}
```

### 2. Follow-Up Questions

```python
# User asks: "What happened right before that error?"
# → Searches case_abc123 collection
# → Retrieves chronologically earlier chunks
# → Synthesis LLM generates contextual answer

# User asks: "Does the config file mention that IP address?"
# → Searches across all documents in case_abc123
# → Includes both server.log and config.yml chunks
# → Synthesis LLM correlates information
```

### 3. Case Expiration

```python
# After 7 days of inactivity:
# → Background scheduler runs cleanup_expired_cases()
# → Deletes ChromaDB collection: case_abc123
# → Frees up storage space
```

---

## API Reference

### CaseVectorStore

**Location**: [faultmaven/infrastructure/persistence/case_vector_store.py](../../faultmaven/infrastructure/persistence/case_vector_store.py)

#### Methods

```python
async def add_documents(
    case_id: str,
    documents: List[Dict[str, Any]]
) -> None:
    """
    Add documents to case-specific collection.

    Args:
        case_id: Case identifier
        documents: List of dicts with keys:
            - id: Document ID (required)
            - content: Document text (required)
            - metadata: Optional metadata dict
    """

async def search(
    case_id: str,
    query: str,
    k: int = 5,
    where: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Search for similar documents in case-specific collection.

    Args:
        case_id: Case identifier
        query: Search query text
        k: Number of results to return
        where: Optional metadata filters

    Returns:
        List of dicts with keys:
            - id: Document ID
            - content: Document text
            - metadata: Metadata dict
            - score: Similarity score (0.0-1.0)
    """

async def delete_case(case_id: str) -> None:
    """Delete entire case collection."""

async def cleanup_expired_cases() -> int:
    """
    Clean up case collections that have exceeded TTL.
    Returns: Number of collections deleted
    """

async def get_case_document_count(case_id: str) -> int:
    """Get number of documents in case collection."""
```

### AnswerFromDocumentTool

**Location**: [faultmaven/tools/answer_from_document.py](../../faultmaven/tools/answer_from_document.py)

#### Methods

```python
async def answer_question(
    case_id: str,
    question: str,
    k: int = 5
) -> Dict[str, Any]:
    """
    Answer a question using case-specific documents.

    Args:
        case_id: Case identifier
        question: User question
        k: Number of chunks to retrieve (default: 5)

    Returns:
        Dict with:
            - answer: Generated answer text
            - sources: List of source document IDs
            - chunk_count: Number of chunks used
            - confidence: Answer confidence (0.0-1.0)
    """
```

### Background Tasks

**Location**: [faultmaven/infrastructure/tasks/case_cleanup.py](../../faultmaven/infrastructure/tasks/case_cleanup.py)

#### Functions

```python
def start_case_cleanup_scheduler(
    case_vector_store: CaseVectorStore,
    interval_hours: int = 6
) -> Optional[BackgroundScheduler]:
    """
    Start background scheduler for case collection cleanup.

    Args:
        case_vector_store: CaseVectorStore instance
        interval_hours: Cleanup interval in hours (default: 6)

    Returns:
        BackgroundScheduler instance (or None if initialization fails)
    """

def stop_case_cleanup_scheduler(
    scheduler: Optional[BackgroundScheduler]
) -> None:
    """Stop the case cleanup scheduler."""
```

---

## Integration with Main Agent

The `answer_from_document` tool is available to the main diagnostic agent but is **not** automatically invoked. The agent should use it when:

1. User asks specific questions about uploaded files
2. Question requires detailed document content (not just summaries)
3. Question involves cross-referencing multiple documents

**Tool Description for Agent**:
```python
"""
Answer a question using case-specific uploaded documents.

Use this tool when the user asks detailed questions about files they've uploaded
in the current case. This tool searches through their documents and provides
accurate answers based on the content.

Args:
    case_id: Case identifier (from session context)
    question: The user's question about their documents
    k: Number of document chunks to retrieve (default: 5)

Returns:
    Answer string with source citations
"""
```

---

## Performance Characteristics

### Token Usage

- **Retrieval**: 0 LLM calls (semantic search only)
- **Synthesis**: 1 LLM call per question
  - Context: ~2,000 tokens (5 chunks × ~400 tokens)
  - Answer: ~500 tokens
  - **Total per question**: ~2,500 tokens

### Latency

- **Retrieval**: ~200ms (ChromaDB semantic search)
- **Synthesis**: ~2-4s (depends on LLM provider)
- **Total**: ~2.2-4.2s per question

### Storage

- **Per document**: ~100-500 chunks (depends on size)
- **Per chunk**: ~256-dimensional embedding (BGE-M3)
- **Collection overhead**: ~1-10 MB per case
- **Automatic cleanup**: After 7 days TTL

---

## Monitoring

### Logs

```bash
# Case collection creation
INFO: Collection ready: case_abc123

# Document addition
INFO: Added 47 documents to case abc123

# Search operations
DEBUG: Case abc123 search returned 5 results

# Cleanup scheduler
INFO: Case cleanup scheduler started (interval: 6 hours)
INFO: Cleanup complete: deleted 3 expired case collections
```

### Metrics (Future Enhancement)

- `case_vector_store_collections_total`: Total active case collections
- `case_vector_store_documents_total`: Total documents across all cases
- `case_vector_store_search_latency_ms`: Search operation latency
- `case_cleanup_expired_total`: Total expired collections deleted
- `answer_from_document_calls_total`: Total QA tool invocations
- `synthesis_llm_token_usage_total`: Token usage for synthesis calls

---

## Error Handling

### No Documents Uploaded

```python
{
    "answer": "I don't have any documents uploaded for this case yet. Please upload relevant files first.",
    "sources": [],
    "chunk_count": 0,
    "confidence": 0.0
}
```

### ChromaDB Connection Failure

```python
{
    "answer": "Error retrieving documents: Connection to vector store failed",
    "sources": [],
    "chunk_count": 0,
    "confidence": 0.0
}
```

### Synthesis LLM Failure

```python
{
    "answer": "Error generating answer: LLM provider unavailable",
    "sources": [],
    "chunk_count": 5,
    "confidence": 0.0
}
```

---

## Testing

### Unit Tests

```bash
# Test CaseVectorStore operations
pytest tests/infrastructure/persistence/test_case_vector_store.py -v

# Test AnswerFromDocumentTool
pytest tests/tools/test_answer_from_document.py -v

# Test background cleanup
pytest tests/infrastructure/tasks/test_case_cleanup.py -v
```

### Integration Tests

```bash
# Test full workflow
pytest tests/integration/test_working_memory.py -v
```

### Manual Testing

```bash
# Start server with ChromaDB
./run_faultmaven.sh

# Upload test document via API
curl -X POST http://localhost:8000/api/v1/case/abc123/upload \
  -F "file=@test_data/server.log"

# Ask question via answer_from_document tool
curl -X POST http://localhost:8000/api/v1/case/abc123/answer \
  -H "Content-Type: application/json" \
  -d '{"question": "What error occurred at 10:45?"}'

# Check case document count
curl http://localhost:8000/api/v1/case/abc123/documents/count
```

---

## Future Enhancements

### Phase 2: Advanced Features

1. **Multi-Document Synthesis**
   - Cross-reference information across multiple uploaded files
   - Generate comprehensive summaries combining multiple sources

2. **Incremental Learning**
   - Update case collection when new documents are added
   - Maintain version history of document changes

3. **Smart Chunking**
   - Context-aware chunking based on document type
   - Overlap optimization for better retrieval

### Phase 3: Production Optimizations

1. **Caching Layer**
   - Cache frequently asked questions per case
   - Reduce synthesis LLM calls by 30-50%

2. **Batch Processing**
   - Process multiple questions in parallel
   - Optimize chunk retrieval for related queries

3. **Advanced TTL Management**
   - Dynamic TTL based on case activity
   - Manual retention for important cases

---

## Security Considerations

1. **Data Isolation**
   - Each case has its own isolated collection
   - No cross-case data leakage

2. **PII Redaction**
   - Documents are sanitized before embedding
   - Synthesis LLM never sees raw PII

3. **Access Control** (Future)
   - Verify user has access to case_id before retrieval
   - Audit log for all document access

---

## Troubleshooting

### Case collection not found

**Symptom**: Search returns 0 results even after upload
**Cause**: Collection creation failed or TTL expired
**Fix**: Check ChromaDB logs, verify connection settings

### Synthesis LLM timeout

**Symptom**: "Error generating answer" after long delay
**Cause**: SYNTHESIS_PROVIDER model too slow or overloaded
**Fix**: Switch to faster model (gpt-4o-mini, claude-haiku)

### Background cleanup not running

**Symptom**: Old case collections not deleted
**Cause**: Scheduler failed to start or crashed
**Fix**: Check main.py logs during startup, restart server

---

## References

- [User Proposal: Working Memory Feature](../../docs/proposals/working-memory-feature.md)
- [CaseVectorStore Implementation](../../faultmaven/infrastructure/persistence/case_vector_store.py)
- [AnswerFromDocumentTool Implementation](../../faultmaven/tools/answer_from_document.py)
- [Background Cleanup Task](../../faultmaven/infrastructure/tasks/case_cleanup.py)
- [Container Integration](../../faultmaven/container.py)
- [Main App Lifecycle](../../faultmaven/main.py)
