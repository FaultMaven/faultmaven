## Frontend Feature Request: Knowledge Base Ingestion & Management UI

### Objective
- Implement a robust Knowledge Base upload and management UI that captures document metadata (type, tags, etc.), persists it via existing API, and enables users to browse, filter, edit, delete, and search documents.

### Scope (Phase 1)
- Upload UI: file + metadata (document_type, tags, title, optional category/source_url/description)
- Documents List: table with filters, pagination, inline actions (view, edit, delete)
- Edit Metadata: update existing records
- Search: invoke backend search
- Error handling, progress, and UX polish

### API Contract (use exactly these)
- POST `/api/v1/knowledge/documents` (multipart/form-data)
  - `file`: File (required)
  - `title`: string (required)
  - `document_type`: string (optional; defaults to "troubleshooting_guide" if omitted)
  - `category`: string (optional)
  - `tags`: string (optional; comma-separated)
  - `source_url`: string (optional)
  - `description`: string (optional)
  - Response: `{ document_id: string, job_id: string, status: string, metadata: {...} }`
- GET `/api/v1/knowledge/documents`
  - Query: `document_type?`: string, `tags?`: comma-separated string, `limit?`: number (default 50), `offset?`: number (default 0)
  - Response: `{ documents: Array<Document>, total_count: number, limit: number, offset: number, filters: {...} }`
- GET `/api/v1/knowledge/documents/{document_id}`
- PUT `/api/v1/knowledge/documents/{document_id}`
  - Body: JSON with any of: `title`, `content`, `tags`, `document_type`, `category`, `version`, `description`
  - Returns updated metadata
- DELETE `/api/v1/knowledge/documents/{document_id}`
- POST `/api/v1/knowledge/search`
  - Body: `{ query: string, limit?: number, include_metadata?: boolean, similarity_threshold?: number, filters?: { category?, document_type? } }`
  - Response: `{ query, total_results, results: Array<{ document_id, content, metadata: { title, document_type, category, tags, priority? }, similarity_score }> }`

### UI/UX Requirements

#### 1) Upload Panel (side panel “Knowledge” tab)
- Fields:
  - File picker (single file; accept text/markdown, txt, pdf/doc allowed but just pass through; backend validates)
  - Title: prefilled from first Markdown H1 if available else filename (editable)
  - Document Type: dropdown with free-form override
    - Suggested values: `playbook`, `guide`, `reference`, `troubleshooting_guide`
    - Default: `guide`
  - Tags: chip input; submit as a comma-separated string
  - Optional: Category (text), Source URL, Description (multiline)
- Behavior:
  - Validate required fields: file, title
  - Submit using FormData with the exact field names above
  - Show upload progress and a success toast
  - After success, refetch list (preserve current filters)
- Error handling:
  - Display backend detail message on 4xx/5xx
  - Handle 415/422 (file type/content) and show actionable tips

#### 2) Documents List View
- Layout:
  - Table columns: Title, Type, Tags, Category, Created (relative), Actions
- Filters:
  - Text search (client-side filter on Title; for global semantic search use the Search panel below)
  - Type dropdown (same options as upload)
  - Tags multi-select (derived from known tags; free entry allowed)
- Pagination:
  - Use `limit`/`offset` (default limit=50)
  - Display `total_count`; next/prev controls
- Actions:
  - View (opens details drawer with full metadata)
  - Edit Metadata (opens modal with same fields as upload, except file)
  - Delete (confirm dialog)
- State:
  - Persist last used filters and pagination in extension storage
  - Loading and “no results” states

#### 3) Edit Metadata Modal
- Fields: Title, Document Type, Tags, Category, Source URL, Description, Version (optional)
- Submit via `PUT /api/v1/knowledge/documents/{document_id}` with JSON
- On success: close and refresh list; optimistic update allowed

#### 4) Search Panel (Semantic)
- Form:
  - Query (required)
  - Filters (optional): `document_type`, `category`
  - Similarity threshold slider (0.0–1.0; default 0.7)
  - Limit (default 5)
- Submit via `POST /api/v1/knowledge/search`
- Results list: show title, snippet, type/tags, score; “Open document” action

#### 5) UX & Validation
- Title auto-infer: From first `# H1`, else strip extension from filename
- Tags input: chip UI, comma-separated on submit; trim whitespace
- Document Type: dropdown with free-text entry allowed
- Respect content-security-policy; API base URL comes from extension settings
- Do not send empty fields (but safe to send empty `tags` string)

#### 6) Performance & Accessibility
- Debounce client-side list filtering (300ms)
- Maintain 50 rows per page by default
- Keyboard accessible: tab order across inputs, Enter to submit, Esc to cancel
- Announce upload progress and completion via ARIA live region

#### 7) Telemetry (optional but recommended)
- Log events: `kb_upload_started`/`completed`/`failed`, `kb_list_loaded`, `kb_edit_saved`, `kb_delete_confirmed`
- Include timing metrics and response status
- Do not log file content

### TypeScript Models (recommended)
```typescript
export type DocumentType = 'playbook' | 'troubleshooting_guide' | 'reference' | 'how_to';

export interface KnowledgeDocument {
  document_id: string;
  title: string;
  content?: string;           // only present for GET by id or search snippet
  document_type: DocumentType;
  category?: string;
  tags: string[];
  source_url?: string;
  description?: string;
  status?: string;
  created_at?: string;        // ISO UTC
  updated_at?: string;        // ISO UTC
  metadata?: Record<string, any>;
}

export interface DocumentListResponse {
  documents: KnowledgeDocument[];
  total_count: number;
  limit: number;
  offset: number;
  filters: { document_type?: string; tags?: string[] };
}
```

### Submission Example
```javascript
const form = new FormData();
form.append('file', file);
form.append('title', title);
form.append('document_type', docType);        // e.g. 'playbook'
form.append('tags', tags.join(','));          // 'deployment,canary'
if (category) form.append('category', category);
if (sourceUrl) form.append('source_url', sourceUrl);
if (description) form.append('description', description);

await fetch(`${API_BASE}/api/v1/knowledge/documents`, { method: 'POST', body: form });
```

### Acceptance Criteria
- Upload panel captures and submits `title`, `document_type` (one of: playbook, troubleshooting_guide, reference, how_to), `tags`, and optional fields; backend reflects them on `GET /api/v1/knowledge/documents`.
- List view shows accurate type/tags and supports filtering and pagination based on backend `total_count`.
- Edit metadata updates fields via `PUT` and persists in list view.
- Search panel returns results via `POST /api/v1/knowledge/search` and displays title/snippet/type/tags/score.
- Errors are surfaced clearly; upload progress visible.
- Accessibility: keyboard navigation and ARIA announcements work.

### Phase 2 (optional, after Phase 1)
- Bulk edit/delete using existing bulk endpoints
- Drag-and-drop multi-file upload queue
- Inline tag editing in table
- Import/export of KB metadata (CSV/JSON)
- Source preview for markdown (sanitized)

### Notes
- Backend already supports all required operations; no backend changes needed.
- The UI should not assume `X-Total-Count`; read `total_count` from response JSON.
