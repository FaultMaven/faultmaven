# Task: Knowledge Base Scope Isolation

**Date:** 2026-03-23
**Priority:** High — foundational for multi-user deployments
**Depends on:** Auth module RBAC (already implemented)
**Spec reference:** `knowledge-and-ai/knowledge-base-architecture.md` (3-tier KB design)

---

## Problem

All knowledge documents (runbooks, guides) are currently stored in a single ChromaDB collection (`faultmaven_kb`) with no ownership or scope metadata. Every user sees every document, and the AI retrieves from the full corpus regardless of who uploaded it or who should have access.

The conversion pipeline already writes `scope` (personal/team/global) into runbook frontmatter and the `conversion_jobs` table, but the downstream knowledge service ignores it.

## Objective

Implement scope-aware knowledge storage, retrieval, and access control so that:
- **Personal** runbooks are only visible to and retrievable by the owner
- **Team** runbooks are visible to all members of the team
- **Global** runbooks are visible to all users
- The AI's RAG retrieval respects these boundaries
- The Dashboard documents list filters by the user's accessible scopes

## Scope of Work

### 1. Data Model Changes

**`KnowledgeItem` / `knowledge_items` table:**
- Add `scope` field: `personal` | `team` | `global`
- Add `owner_id` field: user ID of the creator
- Add `team_id` field: team ID (when scope = team)
- Add `organization_id` field: org context

**ChromaDB metadata:**
- Add `scope`, `owner_id`, `team_id` to document metadata stored in ChromaDB
- These fields are used for metadata filtering during retrieval

### 2. Storage Strategy

**Use metadata filtering within one collection (single `faultmaven_kb`).**

- Add `scope`, `owner_id`, `team_id` to document metadata in ChromaDB
- Filter at query time with a single query:
  ```python
  where={"$or": [
      {"scope": "global"},
      {"owner_id": user_id},
      {"team_id": {"$in": user_team_ids}}
  ]}
  ```

Do NOT use separate collections per scope/user/team. Reasons:

1. **N+1 query problem** — A user in 5 teams would require 7 separate ChromaDB queries (global + personal + 5 teams), then manual merge/dedup/sort in Python. This defeats the HNSW graph optimization.
2. **Collection overhead** — ChromaDB is optimized for millions of vectors in few collections, not thousands of tiny collections. At SaaS scale (1,000 users, 100 teams), Option B would maintain 1,101 separate HNSW index graphs in memory.
3. **ChromaDB metadata filtering uses Roaring Bitmaps** — pre-filtering metadata before graph traversal is highly efficient. One query, one graph, one sorted top-K result.

Note: the knowledge-base-architecture.md mentions per-tier collections, but that was written before evaluating ChromaDB's actual performance characteristics. This task supersedes that recommendation.

### 3. Ingestion Pipeline Changes

**`KnowledgeService.add_document()` / ingestion:**
- Accept `scope`, `owner_id`, `team_id` parameters
- Store scope metadata in ChromaDB alongside embeddings
- The conversion pipeline's `verify_draft` already passes scope — wire it through

**`KnowledgeIngester`:**
- Tag all ingested documents with scope metadata
- For bulk ingestion (toolkit CLI), infer scope from directory path:
  - `data/knowledge/global/*.md` → scope=global
  - `data/knowledge/team_{id}/*.md` → scope=team
  - `data/knowledge/personal_{id}/*.md` → scope=personal

### 4. Retrieval Changes

**`KnowledgeService.search()`:**
- Accept `user_id` and resolve accessible scopes:
  - Always: user's personal scope
  - If user belongs to teams: those team scopes
  - Always: global scope
- Filter ChromaDB query by accessible scopes
- Return scope badge in search results

**Agent tools (`global_kb_qa`, `user_kb_qa`, `case_evidence_qa`):**
- `global_kb_qa` → searches global scope only
- `user_kb_qa` → searches personal + team scopes
- Both already exist as separate tools — update their collection/filter parameters

### 5. API Changes

**`GET /knowledge/documents`:**
- Add `scope` query parameter (optional filter)
- Return `scope` and `owner_id` fields in response
- Default: return all documents accessible to the authenticated user

**`POST /knowledge/documents` (upload):**
- Accept `scope` parameter (default: personal)
- Validate permissions: global requires admin, team requires team membership

**`DELETE /knowledge/documents/{id}` (archive):**
- Verify user has permission to archive (owner, team admin, or platform admin)

### 6. Dashboard Changes

**Documents tab:**
- Add scope filter dropdown (All / Personal / Team / Global)
- Show scope badge on each document card
- Archive button only visible if user has permission

### 7. RBAC Integration

The auth module already provides:
- `DevUser.user_id` — for personal scope ownership
- `DevUser.roles` — for admin checks (global scope access)
- `DevUser.organization_id` — for org context
- Team membership — via `team_members` table

Wire these into the knowledge service:

```python
def resolve_accessible_scopes(user: DevUser) -> ScopeFilter:
    """Determine which scopes a user can access."""
    scopes = [
        {"scope": "personal", "owner_id": user.user_id},
        {"scope": "global"},
    ]
    # Add team scopes for user's teams
    for team_id in get_user_team_ids(user.user_id):
        scopes.append({"scope": "team", "team_id": team_id})
    return ScopeFilter(scopes)
```

## Architecture Context

Read these before starting:
- `knowledge-and-ai/knowledge-base-architecture.md` — 3-tier design, federated search
- `security/iam-design.md` — RBAC model, team/org hierarchy
- `data-and-storage/repository-pattern.md` — storage abstraction

## What NOT to Change

- The conversion pipeline — it already writes scope correctly
- The runbook template or frontmatter schema
- The auth module internals — use its existing contracts
- ChromaDB embedding model or chunking parameters

## Testing

- Unit tests: scope filtering in search queries
- Unit tests: permission checks for upload/archive
- Integration tests: user A's personal runbooks not visible to user B
- Integration tests: global runbooks visible to all
- Integration tests: team runbooks visible only to team members

## Migration

Existing documents in ChromaDB have no scope metadata. Migration strategy:
- Or default to `scope=personal, owner_id=admin` if the deployment is single-user
- Add a migration script that backfills scope metadata based on file paths

---

## Implementation Summary (Completed 2026-03-24)

The scope isolation architecture has been successfully implemented and tested according to this specification.

### Key Completions:
1. **Data Model Updates**: Added `scope`, `owner_id`, and `team_id` throughout the `KnowledgeItem` data constraints and vector database `VectorMetadata` wrapper.
2. **Access Control (RBAC)**: Implemented the `resolve_accessible_scopes` dependency which returns a ChromaDB-native `$or` JSON metadata filter matching the authenticated user's permissions.
3. **Knowledge Retrieval Layer**: Both `KnowledgeService.list_documents` (fetching from Redis/Memory) and `KnowledgeService.search_documents` (fetching from ChromaDB) natively filter out inaccessible data at the source.
4. **Agent Integration**: Upgraded the `DocumentQATool`, `user_kb_qa`, and `global_kb_qa` tools to transparently apply these strict JSON scope filters to their background semantic searches.
5. **Architectural Decisions Sustained**:
    - **Vector Storage**: All documents are stored in the same single collection (`faultmaven_kb`), utilizing highly optimized HNSW graph metadata filtering rather than separated database collections.
    - **Physical Storage**: All source markdown files dropped onto the API server are completely discarded immediately after runbook generation, relying fully on the DB representation and scoped isolation parameters.

All systems are now fully multi-tenant across Personal, Team, and Global boundaries!
