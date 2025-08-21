### Knowledge Base (KB) Persistence Design

This document describes how FaultMaven persists Knowledge Base (KB) data across restarts using Redis for fast metadata retrieval and ChromaDB for vector search. It details data structures, control flow, configuration, failure modes, and observability.

### Goals

- Persist KB uploads across API restarts
- Support fast list/get/filter operations via Redis
- Support semantic retrieval via ChromaDB (vectors)
- Avoid silent downgrades; fail fast on critical dependencies

### Architecture Overview

- Redis persists KB document records and indexes.
- ChromaDB stores vector embeddings for semantic search.
- API returns sources via the OpenAPI-compliant `Source` shape: `content` + `metadata` (e.g., `metadata.title`).

### Redis Data Model

- Keys
  - `kb:doc:{document_id}` (Hash)
    - Field: `data` (JSON-encoded document record)
  - `kb:docs` (Set)
    - All `document_id` members
  - `kb:index:type:{document_type}` (Set)
    - `document_id` members for this type
  - `kb:index:tag:{tag}` (Set)
    - `document_id` members for this tag

- Document record (stored in `kb:doc:{id}` → `data` JSON)
  - `document_id`, `title`, `content`, `document_type`, `category`, `tags`, `source_url`, `description`, `status`, `created_at`, `updated_at`, `metadata`
  - Note: At present, the full `content` is persisted alongside metadata to simplify get/search. This can be reduced later to minimize footprint and move content out of Redis if needed.

### ChromaDB Storage Model

- Collection: `faultmaven_knowledge` (configurable)
- For each upload, 1 embedding is added with:
  - `id`: `document_id`
  - `documents`: raw text `content`
  - `metadatas`: `{ title, document_type, tags, source_url, created_at, updated_at }` (nulls removed)

### Request Flows

- Upload (POST `/api/v1/knowledge/documents`)
  1. Validate and read file
  2. Persist KB record in Redis:
     - `HSET kb:doc:{id} data=<JSON>`
     - `SADD kb:docs {id}`
     - `SADD kb:index:type:{type} {id}`
     - `SADD kb:index:tag:{tag}` for each tag
  3. Index content in ChromaDB (vector store)
  4. Return `{ document_id, job_id, status, metadata }`

- List (GET `/api/v1/knowledge/documents`)
  1. Resolve candidate IDs from Redis sets
  2. Apply optional `document_type` and `tags` filters using index sets
  3. `HGET kb:doc:{id} data`, decode, paginate
  4. Return `{ documents, total_count, limit, offset }`

- Get (GET `/api/v1/knowledge/documents/{id}`)
  - `HGET kb:doc:{id} data`

- Update Metadata (PUT `/api/v1/knowledge/documents/{id}`)
  - Load doc → apply updates → `HSET kb:doc:{id} data=<JSON>`
  - Maintain type/tag index sets (remove old, add new)
  - Re-index vectors if `content` changed

- Delete (DELETE `/api/v1/knowledge/documents/{id}`)
  - `DEL kb:doc:{id}`; `SREM kb:docs {id}`; remove from type/tag indexes
  - Optionally delete vectors (API exists in adapter; wiring pending)

### Configuration

- Redis
  - Preferred: `REDIS_URL=redis://:PASSWORD@HOST:PORT/0`
  - Or: `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`
  - Client created with `decode_responses=true` (string I/O)

- ChromaDB
  - `CHROMADB_URL` (NodePort/ingress for external, in-cluster `http://chromadb:8000`)
  - `CHROMADB_API_KEY` optional; if set, client attempts token auth
  - `CHROMADB_COLLECTION` (default: `faultmaven_knowledge`)

### Failure Modes & Degradation

- App startup
  - Redis: Session store init is fail-fast; app aborts if Redis is unreachable (prevents silent fallback to memory)
  - ChromaDB: Client creation errors abort initialization (vectors required for RAG)

- Runtime (KnowledgeService)
  - Redis operations log warnings and may fall back to in-memory for listing/get in development scenarios. This path is logged and considered degraded; not intended for production.
  - ChromaDB `add_documents` sanitizes metadata (drops nulls, coerces non-primitives to strings) to match server schema.

### Observability

- INFO logs
  - `KB metadata persisted to Redis` (document count, type, tag count)
  - `ChromaDB collection ready`
  - `Added documents to vector store`

- DEBUG logs
  - Vector indexing details (ids)
  - Retriever adapter summaries (e.g., `KBAdapter.search completed`)

### API Contract Compliance

- ResponseType: UPPERCASE (e.g., `ANSWER`)
- Source objects: `content: string` (no `snippet/name`), with `metadata.title` as needed
- PII artifacts: sanitized and placeholder tags (e.g., `<PERSON>`) removed before returning to client

### Security & Data Handling

- PII redaction occurs before external calls
- Returned content is passed through sanitizer; placeholder tags are replaced with `[redacted]` for user display

### Rationale for Redis + Chroma Split

- Redis provides low-latency metadata listing and filtering (type/tag indexes)
- ChromaDB provides persistent semantic search capabilities
- This split avoids overloading the vector store for basic CRUD and supports horizontal scaling

### Future Enhancements

- Reduce Redis footprint by persisting a metadata-only record (omit full `content`) and using Chroma/secondary store for full text retrieval
- Add vector delete integration for document removal
- Redis SCAN-based pagination for very large collections

### Testing

- Upload → List → Restart API → List again (documents persist)
- Tag/type filters return correct subsets
- Retrieval for known connectivity queries returns sources populated from KB and vectors


