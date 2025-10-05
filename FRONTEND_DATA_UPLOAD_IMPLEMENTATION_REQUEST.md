# Frontend Implementation Request: Data Upload API Update

**Date**: 2025-10-03
**Backend Version**: v3.2.0
**Priority**: High
**Breaking Change**: Yes (Required Parameter Addition)

---

## Executive Summary

The backend `/api/v1/data/upload` endpoint has been updated to **require** a `case_id` parameter to ensure all uploaded data is properly associated with troubleshooting cases and user ownership. This change ensures data submitted via direct file upload follows the same ownership model as queries submitted through the query interface.

---

## API Contract Changes

### Endpoint: `POST /api/v1/data/upload`

#### Previous Contract (Deprecated)
```
Content-Type: multipart/form-data

Required Fields:
- file: binary
- session_id: string

Optional Fields:
- description: string
```

#### New Contract (Required Implementation)
```
Content-Type: multipart/form-data

Required Fields:
- file: binary
- session_id: string
- case_id: string          ← NEW REQUIRED FIELD

Optional Fields:
- description: string
```

---

## Why This Change Was Made

### Problem
Previously, data uploaded via `/api/v1/data/upload` had no case association, meaning:
- ❌ Uploaded data wasn't linked to specific troubleshooting cases
- ❌ No user ownership tracking for uploaded files
- ❌ Data couldn't be retrieved as part of case context
- ❌ Inconsistent with query submission flow

### Solution
By requiring `case_id`, we ensure:
- ✅ All uploaded data is associated with a specific case
- ✅ User ownership is tracked (via case ownership)
- ✅ Data becomes part of case context for AI analysis
- ✅ Consistent API design across query and data submission

---

## Frontend Implementation Requirements

### 1. Update API Client

**File**: Browser extension API client (likely `src/api/data.ts` or similar)

**Before**:
```typescript
async function uploadData(
  file: File,
  sessionId: string,
  description?: string
): Promise<UploadedData> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('session_id', sessionId);
  if (description) {
    formData.append('description', description);
  }

  return await fetch('/api/v1/data/upload', {
    method: 'POST',
    body: formData
  });
}
```

**After**:
```typescript
async function uploadData(
  file: File,
  sessionId: string,
  caseId: string,        // ← NEW REQUIRED PARAMETER
  description?: string
): Promise<UploadedData> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('session_id', sessionId);
  formData.append('case_id', caseId);  // ← NEW REQUIRED FIELD
  if (description) {
    formData.append('description', description);
  }

  return await fetch('/api/v1/data/upload', {
    method: 'POST',
    body: formData
  });
}
```

### 2. Update UI Components

**All file upload components must provide `case_id`:**

#### Scenario A: Upload from within a case view
```typescript
// Component has access to current case
const currentCase = useCurrentCase(); // or similar hook

const handleFileUpload = async (file: File) => {
  const result = await uploadData(
    file,
    sessionId,
    currentCase.id,  // ← Use current case ID
    description
  );
};
```

#### Scenario B: Upload from global view (no case context)
```typescript
// Create a new case first, then upload
const handleFileUpload = async (file: File) => {
  // 1. Create new case
  const newCase = await createCase({
    title: `Data upload: ${file.name}`,
    userId: currentUser.id
  });

  // 2. Upload data to the new case
  const result = await uploadData(
    file,
    sessionId,
    newCase.id,  // ← Use newly created case ID
    description
  );
};
```

### 3. Error Handling

Update error handling for missing `case_id`:

```typescript
try {
  await uploadData(file, sessionId, caseId, description);
} catch (error) {
  if (error.status === 422 && error.detail?.includes('case_id')) {
    // Handle missing case_id validation error
    showError('Please select or create a case before uploading data');
  }
}
```

---

## Implementation Checklist

### Required Changes

- [ ] **Update API client function signatures**
  - [ ] Add `caseId: string` parameter to `uploadData()`
  - [ ] Add `case_id` to FormData submission
  - [ ] Update TypeScript types/interfaces

- [ ] **Update all UI components that call upload**
  - [ ] File upload dialogs
  - [ ] Drag-and-drop upload handlers
  - [ ] Paste handlers (if applicable)

- [ ] **Handle case context**
  - [ ] If in case view → use current case ID
  - [ ] If no case context → create new case first
  - [ ] Add UI to select existing case (optional enhancement)

- [ ] **Update error handling**
  - [ ] Handle 422 validation errors for missing `case_id`
  - [ ] Show user-friendly error messages

- [ ] **Update tests**
  - [ ] Unit tests for API client
  - [ ] Integration tests for upload flow
  - [ ] E2E tests for file upload scenarios

### Optional Enhancements

- [ ] **Case selection UI**
  - [ ] Allow users to select from existing cases when uploading
  - [ ] Show case dropdown in upload dialog

- [ ] **Auto-case creation**
  - [ ] Automatically create case with meaningful title from filename
  - [ ] Notify user when new case is created

---

## Testing Instructions

### Test Case 1: Upload from Case View
1. Navigate to an existing case
2. Click "Upload File" button
3. Select a file
4. Verify upload succeeds
5. Verify file appears in case context

**Expected**: Upload succeeds with case association

### Test Case 2: Upload without Case Context
1. Navigate to home/global view (no active case)
2. Click "Upload File" button
3. Select a file
4. Verify system creates new case OR prompts for case selection
5. Verify upload succeeds

**Expected**: Upload succeeds after case creation/selection

### Test Case 3: Error Handling
1. Attempt upload with malformed `case_id`
2. Verify 422 error is caught
3. Verify user-friendly error message shown

**Expected**: Graceful error handling with clear message

---

## Response Format (Unchanged)

The response format remains the same:

```typescript
interface UploadedData {
  data_id: string;
  session_id: string;
  data_type: string;
  file_name: string;
  file_size: number;
  processing_status: 'completed' | 'processing' | 'failed';
  insights: {
    error_count: number;
    patterns: Array<any>;
    recommendations: Array<any>;
  };
  context: {
    case_id: string;        // ← Now included in response
    user_id: string;        // ← Now included in response
    source: string;
    description?: string;
  };
}
```

**New fields in response**:
- `context.case_id` - The associated case ID
- `context.user_id` - The owner user ID
- `context.source` - Always `"direct_file_upload"` for this endpoint

---

## Migration Strategy

### Phase 1: Immediate (Required)
1. Update API client to require `case_id`
2. Update all upload UI components
3. Deploy to dev environment
4. Test all upload flows

### Phase 2: Validation (Required)
1. Test with QA team
2. Verify case associations working correctly
3. Verify error handling
4. Deploy to staging

### Phase 3: Production (Required)
1. Deploy to production
2. Monitor for upload errors
3. Verify data associations in database

---

## Support & Questions

### Backend Contact
- **Team**: FaultMaven Backend Team
- **Slack**: #faultmaven-backend
- **Documentation**: `/docs/api/openapi.locked.yaml`

### API Endpoints Reference

**Data Upload**:
```
POST /api/v1/data/upload
Content-Type: multipart/form-data

Required: file, session_id, case_id
Optional: description
```

**Query Submission** (for comparison):
```
POST /api/v1/cases/{case_id}/queries
Content-Type: application/json

Body: { "query": "...", "session_id": "..." }
```

Both endpoints now follow consistent case association patterns.

---

## Timeline

- **Specification Complete**: 2025-10-03
- **Frontend Implementation Deadline**: TBD (coordinate with PM)
- **Testing Window**: 3-5 days
- **Production Deployment**: TBD

---

## Appendix: Full Example

### Complete Frontend Implementation Example

```typescript
// src/api/data.ts
export interface UploadDataParams {
  file: File;
  sessionId: string;
  caseId: string;
  description?: string;
}

export async function uploadData({
  file,
  sessionId,
  caseId,
  description
}: UploadDataParams): Promise<UploadedData> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('session_id', sessionId);
  formData.append('case_id', caseId);

  if (description) {
    formData.append('description', description);
  }

  const response = await fetch('/api/v1/data/upload', {
    method: 'POST',
    body: formData,
    headers: {
      // Don't set Content-Type - browser will set it with boundary
    }
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Upload failed');
  }

  return await response.json();
}

// src/components/FileUpload.tsx
export function FileUploadDialog({ currentCase }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [description, setDescription] = useState('');
  const sessionId = useSessionId();

  const handleUpload = async () => {
    if (!file || !currentCase) return;

    try {
      const result = await uploadData({
        file,
        sessionId,
        caseId: currentCase.id,
        description
      });

      showSuccess(`File uploaded successfully: ${result.data_id}`);
      onUploadComplete(result);
    } catch (error) {
      showError(`Upload failed: ${error.message}`);
    }
  };

  return (
    <Dialog>
      <input type="file" onChange={e => setFile(e.target.files?.[0])} />
      <textarea
        placeholder="Description (optional)"
        value={description}
        onChange={e => setDescription(e.target.value)}
      />
      <button onClick={handleUpload}>Upload</button>
    </Dialog>
  );
}
```

---

**END OF DOCUMENT**

Please confirm receipt and estimated implementation timeline.
