# Handover: Document-to-Runbook Conversion — Dashboard Frontend

**Date:** 2026-03-22
**From:** Backend implementation (commit `8f0b8cf2`)
**To:** Frontend implementation agent
**Status:** Backend complete, frontend ready to build

---

## What Exists (Backend — Done)

The full backend pipeline is implemented and tested (117 tests). Conversion endpoints are always active — no feature flag required.

### API Endpoints (all under `/api/v1/knowledge`)

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `POST` | `/convert` | Upload file + scope → conversion | Any user (personal), admin (global) |
| `GET` | `/conversions` | List user's conversion jobs | Owner only |
| `GET` | `/conversions/{conversion_id}` | Get job details with all drafts | Owner only |
| `PUT` | `/conversions/{conversion_id}/drafts/{draft_id}` | Edit draft, re-validates | Owner only |
| `POST` | `/conversions/{conversion_id}/drafts/{draft_id}/verify` | Promote to verified, trigger ingestion | Owner only |
| `DELETE` | `/conversions/{conversion_id}/drafts/{draft_id}` | Soft-delete draft | Owner only |

### API Contracts

**POST /convert** — Multipart form data:

```
file: File (PDF, DOCX, TXT, MD, HTML)
scope: "global" | "team" | "personal"
team_id?: string (required if scope="team")
```

Response (201):
```json
{
  "conversion_id": "conv_a1b2c3d4e5f6",
  "status": "completed",
  "source_file": {
    "filename": "postgres-troubleshooting.pdf",
    "size_bytes": 45230,
    "content_type": "application/pdf",
    "retained_path": "data/knowledge/sources/conv_.../file.pdf"
  },
  "analysis": {
    "failure_modes_detected": 3,
    "is_actionable": true,
    "source_assessment": {
      "content_type": "troubleshooting_guide",
      "actionability_rating": "high",
      "missing_information": []
    }
  },
  "drafts": [
    {
      "draft_id": "draft_x1y2z3a4b5c6",
      "runbook_id": "pg-connection-pool-exhaustion",
      "title": "PostgreSQL Connection Pool Exhaustion",
      "scope": "global",
      "status": "draft",
      "validation": { "passed": true, "errors": [], "warnings": ["No external refs"] },
      "quality_score": { "overall": 72.5, "grade": "C", "completeness": 85.0, "clarity": 70.0, "actionability": 65.0, "comprehensiveness": 68.0 },
      "file_path": "data/knowledge/global/pg-connection-pool-exhaustion.md",
      "content_preview": "---\nid: pg-connection-pool-exhaustion\ntitle: ...",
      "content": "full markdown content...",
      "quality_warning": null
    }
  ],
  "warnings": [],
  "created_at": "2026-03-22T14:30:00Z"
}
```

**Error responses:**

| Status | When |
|--------|------|
| 400 | Missing scope, team scope without team_id |
| 403 | Non-admin attempting global scope |
| 413 | File too large OR document exceeds 30K tokens |
| 415 | Unsupported file type |
| 422 | Document not actionable, all drafts failed validation |
| 500 | LLM failure |
| 503 | Conversion service not available (feature flag off) |

**PUT .../drafts/{draft_id}** — JSON body:
```json
{ "content": "full markdown including frontmatter (min 100 chars)" }
```
Returns: Updated draft with re-run validation and quality score.

**POST .../drafts/{draft_id}/verify** — No body.
Returns:
```json
{
  "draft_id": "...",
  "runbook_id": "...",
  "status": "verified",
  "knowledge_item_id": "ki_abc123",
  "ingested": true,
  "ingested_at": "2026-03-22T15:00:00Z",
  "collection": "global_kb",
  "chunks_created": 8
}
```
Returns 400 if draft not found, already verified, or validation not passed.

**GET /conversions** — Query params: `limit` (default 20), `offset` (default 0).
Returns: `[{ conversion_id, status, source_filename, failure_modes_detected, scope, created_at }]`

---

## What Needs to Be Built (Frontend)

### Entry Point

Add a **"Convert Document"** button to the existing `KBPage.tsx`, next to the existing upload functionality. Visible only when the user has write access to the current KB tier. The button opens the conversion flow (either inline or as a separate page).

### UI Flow (4 Steps)

**Step 1: Upload**
- File drop zone (reuse `<UploadZone>` component)
- Scope picker: Personal / Team / Global radio buttons
- Team selector dropdown (shown when Team selected)
- "Convert" button → `POST /knowledge/convert`
- Accepted formats: PDF, DOCX, TXT, Markdown, HTML (max 10 MB)

**Step 2: Processing**
- Spinner with status text: "Analyzing document...", "Found N failure modes...", "Generating runbook N of M..."
- V1 is a simple spinner — the API call blocks until complete. No polling needed.
- Disable the Convert button after submission.

**Step 3: Review Drafts**
- Card for each draft showing:
  - Title and runbook ID
  - Quality score badge (overall/grade, e.g., "73/C")
  - Validation status (PASSED with warning count, or FAILED with error count)
  - Quality warning banner if score < 50
  - Actions: [Edit] [Verify & Ingest] [Delete]
- "Verify & Ingest" button disabled when `validation.passed === false`
- Source file info shown at top (filename, size, assessment)

**Step 4: Edit Draft (inline markdown editor)**
- Full markdown content in a text editor (textarea or a markdown editor component)
- On save → `PUT .../drafts/{draft_id}` with full content
- Response updates validation and quality score in real-time
- Show validation errors/warnings alongside the editor
- "Save Draft" / "Verify & Ingest" / "Cancel" buttons

**Step 5: Verify Confirmation (modal)**
- Reuse `<ConfirmDialog>` component
- Explain what verification does: sets status to verified, sets verified_by, ingests into ChromaDB, makes runbook searchable
- "Confirm" / "Cancel"
- On confirm → `POST .../drafts/{draft_id}/verify`

### Files to Create

```
src/lib/knowledge/conversion.ts        # API client for conversion endpoints
src/components/ConvertUpload.tsx        # File selection + scope picker + convert trigger
src/components/ConversionResults.tsx    # Draft cards with scores and actions
src/components/DraftEditor.tsx          # Markdown editor with live validation display
```

### Files to Modify

```
src/pages/KBPage.tsx                   # Add "Convert Document" button/flow
src/lib/api.ts                         # Re-export conversion functions
src/types/index.ts                     # Add TypeScript interfaces
```

---

## Dashboard Codebase Context

### API Client Pattern

All API calls use native `fetch()` via shared utilities in `src/lib/knowledge/client.ts`:

```typescript
import { makeAuthenticatedRequest, buildQueryParams } from './client';
import { config } from '../config';

export async function convertDocument(
  file: File,
  scope: string,
  teamId?: string
): Promise<ConversionResponse> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('scope', scope);
  if (teamId) formData.append('team_id', teamId);

  const response = await makeAuthenticatedRequest(
    `${config.apiUrl}/api/v1/knowledge/convert`,
    { method: 'POST', body: formData }
    // NOTE: Do NOT set Content-Type header — browser sets it with boundary for FormData
  );
  return handleAPIResponse<ConversionResponse>(response);
}
```

Auth is automatically injected by `makeAuthenticatedRequest` (Bearer token + X-User-ID + X-User-Roles headers).

### Existing Components to Reuse

| Component | File | Use For |
|-----------|------|---------|
| `UploadZone` | `src/components/UploadZone.tsx` | File picker in step 1 |
| `ConfirmDialog` | `src/components/ConfirmDialog.tsx` | Verify confirmation modal |
| `UploadModal` | `src/components/UploadModal.tsx` | Pattern reference for modal forms |
| `DocumentCard` | `src/components/DocumentCard.tsx` | Pattern reference for draft cards |
| `PaginationControls` | `src/components/PaginationControls.tsx` | If listing conversions |

### Markdown Rendering

`react-markdown` (v10.1) with `remark-gfm` and `rehype-highlight` is already available. Used in `KnowledgeTab.tsx` for preview. For editing, there is no existing markdown editor — use a `<textarea>` with a preview tab (same pattern as the suggestion editor in `KnowledgeTab.tsx`).

### Styling

All colors use `fm-*` design tokens. Never use raw Tailwind colors. Key tokens:
- `text-fm-primary`, `text-fm-secondary`, `text-fm-accent`
- `bg-fm-surface`, `bg-fm-base`, `bg-fm-hover`
- `border-fm-border`, `border-fm-accent`
- `bg-fm-critical-bg`, `border-fm-critical-border` (for errors)
- `bg-fm-success-bg`, `border-fm-success-border` (for success)
- `rounded-fm-card`, `rounded-fm-btn`, `rounded-fm-input`

### Routing

If the conversion flow lives as a separate page rather than inline in KBPage:
- Add route in `src/App.tsx`
- Follow the pattern of other protected routes
- Consider `/kb/convert` as the path

### State Management

No Zustand or TanStack Query — the dashboard uses direct `fetch()` + `useState`. Follow the same pattern. The conversion flow is self-contained: upload → get response → manage drafts. No global store needed.

---

## TypeScript Interfaces to Add

```typescript
// src/types/index.ts (or src/lib/knowledge/conversion.ts)

interface ConversionResponse {
  conversion_id: string;
  status: 'processing' | 'completed' | 'partial' | 'failed';
  source_file: SourceFileInfo;
  analysis: AnalysisResult;
  drafts: ConversionDraft[];
  warnings: string[];
  created_at: string;
}

interface SourceFileInfo {
  filename: string;
  size_bytes: number;
  content_type: string;
  retained_path: string;
}

interface AnalysisResult {
  is_actionable: boolean;
  failure_modes: FailureModeAnalysis[];
  source_assessment: {
    content_type: string;
    actionability_rating: string;
    missing_information: string[];
  };
}

interface FailureModeAnalysis {
  id: string;
  title: string;
  domain: string;
  service: string;
  symptom_class: string[];
  severity: string;
  symptoms_summary: string;
  resolution_summary: string;
}

interface ConversionDraft {
  draft_id: string;
  runbook_id: string;
  title: string;
  scope: string;
  status: 'draft' | 'verified' | 'deleted';
  validation: ValidationResult;
  quality_score: QualityScore;
  file_path: string;
  content_preview: string;
  content: string | null;
  quality_warning: string | null;
}

interface ValidationResult {
  passed: boolean;
  errors: string[];
  warnings: string[];
}

interface QualityScore {
  overall: number;
  grade: string;
  completeness: number;
  clarity: number;
  actionability: number;
  comprehensiveness: number;
}

interface VerifyResponse {
  draft_id: string;
  runbook_id: string;
  status: string;
  knowledge_item_id: string;
  ingested: boolean;
  ingested_at: string | null;
  collection: string;
  chunks_created: number;
}

interface DraftUpdateRequest {
  content: string;
}

interface ConversionJobSummary {
  conversion_id: string;
  status: string;
  source_filename: string;
  failure_modes_detected: number;
  scope: string;
  created_at: string;
}
```

---

## What NOT to Change

- The backend conversion service or routes — those are done.
- The existing `POST /knowledge/documents` upload endpoint — that's the direct upload path, separate from conversion.
- The KB Toolkit codebase (`faultmaven-kb-toolkit/`).
- The runbook template or taxonomy.

## Questions?

Refer to the full design spec at `faultmaven/docs/architecture/knowledge-and-ai/document-to-runbook-conversion.md` (sections 7 and 7.4 cover the Dashboard UI wireframes and API client functions in detail).
