# Knowledge Base User Scoping Security Issue

**Status**: Open
**Priority**: Medium
**Created**: 2025-10-03
**Category**: Security, Access Control

## Issue Summary

The `/api/v1/knowledge/documents` endpoint lacks user authentication and scoping, creating a security gap where:
1. Documents uploaded via the browser extension are not associated with users
2. No access control exists to prevent unauthorized viewing/modification
3. User-uploaded documents are mixed with system-wide documents from the kb-toolkit

## Current Behavior

### Endpoint: `/api/v1/knowledge/documents` (POST)

**Current Implementation** ([knowledge.py:51-80](../../faultmaven/api/v1/routes/knowledge.py#L51)):
```python
@router.post("/documents", status_code=201)
@trace("api_upload_document")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form(...),
    category: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    response: Response = Response(),
) -> dict:
    # NO authentication dependency!
    # NO user_id capture or association!
```

**Current Request Schema** ([openapi.locked.yaml:3248-3285](../api/openapi.locked.yaml#L3248)):
```yaml
Body_upload_document_api_v1_knowledge_documents_post:
  properties:
    file: ...
    title: ...
    document_type: ...
    category: ...
    tags: ...
    source_url: ...
    description: ...
  required:
    - file
    - title
    - document_type
  # NO user_id field!
  # NO authentication requirement!
```

## Security Implications

### 1. **No Access Control** ❌
- Any client can upload documents without authentication
- No way to restrict who can view/edit/delete documents
- Potential for abuse or spam uploads

### 2. **No User Attribution** ❌
- Documents uploaded via browser extension have no owner
- Cannot implement "my documents" vs "team documents" views
- No audit trail for who uploaded what

### 3. **Document Mixing** ⚠️
- User documents mixed with system-wide KB toolkit documents
- No distinction between personal knowledge and shared knowledge
- Cannot implement user-specific document filtering

## Expected Behavior

### Two-Tier Knowledge Base System

The knowledge base should have **two distinct scopes**:

#### 1. **User-Scoped Documents** (Browser Extension)
- **Uploaded via**: Browser extension UI
- **Visibility**: Private to uploading user by default
- **Authentication**: Required (`require_authentication` dependency)
- **Schema includes**: `user_id` field
- **Use case**: User's personal troubleshooting notes, runbooks, snippets

#### 2. **System-Scoped Documents** (KB Toolkit)
- **Uploaded via**: `faultmaven-kb-toolkit` CLI tool
- **Visibility**: Available to all users (global/shared)
- **Authentication**: Admin/service account
- **Schema includes**: `scope: 'system'` field
- **Use case**: Official documentation, team playbooks, curated guides

## Required Changes

### Backend Changes

#### 1. Add Authentication to Knowledge Routes
```python
@router.post("/documents", status_code=201)
@trace("api_upload_document")
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form(...),
    category: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    current_user: DevUser = Depends(require_authentication),  # ADD THIS
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    response: Response = Response(),
) -> dict:
```

#### 2. Update Document Model
Add user scoping fields to `KnowledgeDocument` model:
```python
class KnowledgeDocument(BaseModel):
    document_id: str
    title: str
    content: str
    document_type: DocumentType

    # NEW FIELDS for user scoping
    user_id: Optional[str] = None        # None = system-wide
    scope: Literal['user', 'system'] = 'user'
    is_shared: bool = False              # User can share their docs

    # Existing fields...
    category: Optional[str]
    tags: List[str]
    created_at: datetime
    updated_at: datetime
```

#### 3. Update Service Layer
Modify `KnowledgeService` to filter by user:
```python
async def list_documents(
    self,
    user_id: Optional[str] = None,
    document_type: Optional[str] = None,
    tags: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> DocumentListResponse:
    # Return system docs + user's own docs + shared docs
    filters = []
    if user_id:
        filters.append({
            "$or": [
                {"scope": "system"},
                {"user_id": user_id},
                {"is_shared": True}
            ]
        })
```

#### 4. Update Vector Store Metadata
Ensure ChromaDB metadata includes user scoping:
```python
metadata = {
    "document_id": doc_id,
    "user_id": user_id,           # NEW
    "scope": "user",              # NEW
    "document_type": doc_type,
    "title": title,
    # ... existing metadata
}
```

### API Specification Changes

#### 1. Update OpenAPI Schema
```yaml
Body_upload_document_api_v1_knowledge_documents_post:
  properties:
    file: ...
    title: ...
    document_type: ...
    # No user_id in request body - extracted from auth token
  required:
    - file
    - title
    - document_type
  security:
    - BearerAuth: []  # ADD THIS
```

#### 2. Update Response Schema
```yaml
KnowledgeDocument:
  properties:
    document_id: string
    user_id:
      type: string
      nullable: true
      description: "User ID if user-scoped, null for system-wide documents"
    scope:
      type: string
      enum: [user, system]
      description: "Document visibility scope"
    is_shared:
      type: boolean
      description: "Whether user has shared this document with others"
    # ... existing fields
```

### Frontend Changes

#### 1. Update API Client
No changes needed - authentication already handled by `authenticatedFetch()`:
```typescript
// Already works correctly!
export async function uploadKnowledgeDocument(
  file: File,
  title: string,
  documentType: DocumentType,
  // ... other params
): Promise<KnowledgeDocument> {
  const response = await authenticatedFetch(
    `${config.apiUrl}/api/v1/knowledge/documents`,
    { method: 'POST', body: formData }
  );
  // Will automatically include Authorization header
}
```

#### 2. Update UI to Show Scoping
```tsx
// In KnowledgeBaseView.tsx
<div className="document-card">
  <h3>{doc.title}</h3>
  {doc.scope === 'user' && (
    <span className="badge">Personal</span>
  )}
  {doc.scope === 'system' && (
    <span className="badge badge-primary">Team Knowledge</span>
  )}
</div>
```

## Migration Strategy

### Phase 1: Add Authentication (Non-Breaking)
1. Add `current_user: DevUser = Depends(require_authentication)` to endpoints
2. Add `user_id` field to document model (optional, defaults to None)
3. Documents without `user_id` are treated as system-scoped
4. **Frontend**: Already sends auth tokens, no changes needed

### Phase 2: Implement Filtering (Enhancement)
1. Update service layer to filter by user
2. Add `scope` field to documents
3. Update ChromaDB metadata
4. **Frontend**: Add "My Documents" / "Team Knowledge" tabs

### Phase 3: Migrate Existing Documents
1. Script to tag existing documents:
   - KB toolkit uploads → `scope: 'system', user_id: null`
   - Browser extension uploads → Would need manual review or default to system

## Breaking Change Analysis

### Is this a Breaking Change? **YES** ✅

**Impact on Frontend**:
- ❌ **Requires no code changes** - authenticatedFetch() already includes auth headers
- ✅ **New behavior**: Uploads will fail with 401 if not authenticated
- ✅ **User experience**: Must be logged in to upload (already required in practice)

**Impact on Backend**:
- ❌ **Schema change**: `user_id` added to KnowledgeDocument model
- ❌ **API behavior**: 401 responses for unauthenticated requests
- ✅ **Database migration**: Existing documents need `user_id` backfill

**Impact on KB Toolkit**:
- ❌ **Requires authentication**: Toolkit must authenticate with service account
- ❌ **Requires `scope: 'system'` flag**: Distinguish from user uploads

### API Version Considerations

Given the breaking nature, consider:
1. **Option A**: Implement in current v1 (acceptable if no external consumers)
2. **Option B**: Create v2 of knowledge endpoints with proper scoping
3. **Option C**: Add optional `user_id` first, make required in future version

## Related Documents

- [AUTHENTICATION_DESIGN.md](../architecture/AUTHENTICATION_DESIGN.md) - Authentication system design
- [KNOWLEDGE_BASE_SYSTEM.md](../KNOWLEDGE_BASE_SYSTEM.md) - Knowledge base architecture
- [auth_dependencies.py](../../faultmaven/api/v1/auth_dependencies.py) - Authentication dependencies

## Recommendation

**Priority**: Medium
**Timeline**: Next minor version (v3.3.0)
**Approach**: Phased implementation to minimize disruption

### Immediate Action Items
1. ✅ Document this issue (this file)
2. ⏳ Create tracking ticket/issue
3. ⏳ Design database migration for existing documents
4. ⏳ Update KB toolkit to support service account authentication
5. ⏳ Implement authentication on knowledge endpoints
6. ⏳ Add user scoping to document model
7. ⏳ Update frontend to show document ownership
8. ⏳ Test migration with existing documents

---

**Last Updated**: 2025-10-03
**Assignee**: TBD
**Related Issues**: TBD
