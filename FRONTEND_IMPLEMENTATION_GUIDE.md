# Frontend Implementation Guide: Session-Based Case Creation

## Executive Summary

**Problem:** Frontend was using optimistic IDs (`opt_case_123`) which caused race conditions and duplicate case creation.

**Solution:** Use the existing session-based case creation endpoint that was already implemented in the backend.

**Key Architectural Fact:** FaultMaven Copilot is a **browser extension side panel** - there is ONE instance per browser (not per tab). The side panel persists across tab switches.

**Impact:**
- ✅ No more race conditions
- ✅ No more duplicate cases
- ✅ No more 404 errors on upload
- ✅ Simpler frontend code (no reconciliation logic needed)
- ✅ Case persists across tab switches (stored in localStorage)

---

## Architecture Clarification

### Browser Extension Side Panel Model

```
Browser (Chrome/Firefox)
├─ Tab 1 (user browsing)
├─ Tab 2 (user browsing)
├─ Tab 3 (user browsing)
└─ FaultMaven Copilot Side Panel ← ONE instance shared across ALL tabs
    ├─ Same session_id
    ├─ Same active case_id
    └─ Persists when user switches tabs
```

**Important:**
- Side panel does NOT reload when switching tabs
- State persists using `localStorage` (browser-wide storage)
- Only scenario for multiple instances: User opens multiple BROWSERS (Chrome + Firefox)

---

## Addressing Frontend Concerns

### Concern #1: "New Chat" Button - Should It Call Backend?

**Answer: NO** - "New Chat" button should only clear local UI state.

**Reasoning:**
- User requirement: Case should be created when user takes FIRST ACTION (upload or query)
- "New Chat" just prepares UI for new conversation
- Backend call happens lazily when user actually does something

**Implementation:**
```typescript
// ✅ CORRECT: "New Chat" button
function handleNewChat() {
  // Clear local state only - NO backend call
  setCurrentCaseId(null);
  setMessages([]);
  localStorage.removeItem('active_case_id');
}
```

**Exception:** If user clicks "New Chat" while actively working in a case AND you want to force a new case:
```typescript
// Only if you want to explicitly force new case while one is active
async function handleNewChatForceNew() {
  const {case_id} = await POST(
    `/api/v1/cases/sessions/${sessionId}/case?force_new=true`,
    {
      headers: {
        'idempotency-key': `new_chat_${Date.now()}` // Prevents duplicates on retry
      }
    }
  );
  setCurrentCaseId(case_id);
  localStorage.setItem('active_case_id', case_id);
  setMessages([]);
}
```

---

### Concern #2: Performance Issue (2 Sequential Calls)

**Answer: ACCEPTED** - This is the correct trade-off.

**Current Flow:**
```
Call 1: POST /cases/sessions/{sessionId}/case  → ~200ms
Call 2: POST /cases/{case_id}/data             → ~2000ms
Total: ~2200ms
```

**Why this is acceptable:**

1. **Reliability > Speed**: The broken optimistic ID approach was faster but caused errors, duplicates, and required retry logic
2. **Real bottleneck is upload processing** (2000ms), not case creation (200ms)
3. **Happens once per session**: User creates a case once, then uploads/queries multiple times to same case
4. **Backend can optimize later**: A combined endpoint can be added if performance becomes critical

**Comparison:**

| Approach | Requests | Performance | Reliability | Complexity |
|----------|----------|-------------|-------------|------------|
| Optimistic IDs (broken) | 2 parallel | 2000ms | ❌ Race conditions, 404s | High |
| Session-based (correct) | 2 sequential | 2200ms | ✅ No errors | Low |
| Combined endpoint (future) | 1 | 2000ms | ✅ No errors | Low |

**Future optimization path:**
Backend can add `/sessions/{session_id}/case-with-data` endpoint that combines both operations. This can be done later without breaking existing frontend code.

---

### Concern #3: Multiple Tabs Confusion

**Answer: NOT APPLICABLE** - Side panel is ONE instance per browser.

**Why this concern doesn't apply:**

Your architecture is a **browser extension side panel**, not a popup extension:
- Side panel = ONE instance that persists across tab switches
- User switches from Tab A to Tab B → Same side panel, same state, same case
- No "Tab 1 has its own copilot, Tab 2 has its own copilot" scenario

**Only multi-instance scenario:**
```
User opens Chrome → Copilot instance 1 (session_1, case_A)
User opens Firefox → Copilot instance 2 (session_2, case_B)
Different browsers = Different sessions = Different cases ✅
```

**State persistence:**
Use `localStorage` (not `sessionStorage`) because:
- Side panel persists across tab switches
- `localStorage` is browser-wide (perfect for side panel)
- `sessionStorage` is tab-specific (would lose data on tab switch)

---

### Concern #4: Idempotency for force_new=true

**Answer: IMPLEMENTED** - Backend now supports idempotency keys.

**Backend support added** (line 1224-1231 in case.py):
```python
idempotency_key = request.headers.get("idempotency-key")

if idempotency_key and force_new:
    existing_result = await case_service.check_idempotency_key(idempotency_key)
    if existing_result:
        return existing_result  # Return cached result on retry
```

**Frontend usage:**
```typescript
async function createNewCase() {
  const idempotencyKey = `new_chat_${sessionId}_${Date.now()}`;

  const response = await fetch(
    `/api/v1/cases/sessions/${sessionId}/case?force_new=true`,
    {
      method: 'POST',
      headers: {
        'idempotency-key': idempotencyKey
      }
    }
  );

  // If network fails and frontend retries with same idempotency key,
  // backend returns same case_id (no duplicate created)
}
```

---

## Backend Endpoints Reference

### 1. Get or Create Case for Session

**Endpoint:** `POST /api/v1/cases/sessions/{session_id}/case`

**Authentication:** Optional - if Bearer token not provided, user is derived from session

**Query Parameters:**
- `title` (optional): Case title (default: auto-generated timestamp)
- `force_new` (optional): Force creation of new case even if one exists

**Headers:**
- `Authorization: Bearer <token>` (optional): If not provided, user_id is taken from session
- `idempotency-key` (optional): Prevent duplicate case creation on retry (only with force_new=true)

**Response:**
```json
{
  "case_id": "uuid",
  "created_new": false,
  "success": true
}
```

**Behavior:**
- `force_new=false` (default): Returns existing case if session has one, creates new if not
- `force_new=true`: Always creates new case
- Idempotent: Safe to call multiple times (with or without idempotency key)

---

### 2. Upload Data to Existing Case

**Endpoint:** `POST /api/v1/cases/{case_id}/data`

**Requirements:** Case MUST exist (returns 404 if not found)

**Request:**
```http
POST /api/v1/cases/{case_id}/data
Content-Type: multipart/form-data

{
  file: <File>,
  session_id: string,
  description?: string,
  source_metadata?: string (JSON)
}
```

**Response:** Upload metadata and processing results

**Error:** 404 if case doesn't exist

---

### 3. Submit Query to Existing Case

**Endpoint:** `POST /api/v1/cases/{case_id}/queries`

**Requirements:** Case MUST exist (returns 404 if not found)

**Request:**
```json
{
  "query": "What caused the error?",
  "session_id": "uuid"
}
```

**Response:** AI agent response with analysis

**Error:** 404 if case doesn't exist

---

## Recommended Implementation

### Complete React/TypeScript Example

```typescript
import { useState, useEffect } from 'react';

export function useCaseManagement(sessionId: string) {
  // Initialize from localStorage (persists across tab switches)
  const [currentCaseId, setCurrentCaseId] = useState<string | null>(() => {
    return localStorage.getItem('active_case_id');
  });

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * Get or create case for the session
   * Checks localStorage first, then backend if needed
   */
  async function ensureCaseExists(): Promise<string> {
    // Already have case in memory
    if (currentCaseId) {
      return currentCaseId;
    }

    // Check localStorage (may exist from previous session/tab)
    const storedCaseId = localStorage.getItem('active_case_id');
    if (storedCaseId) {
      setCurrentCaseId(storedCaseId);
      return storedCaseId;
    }

    // Create new case via backend
    try {
      const response = await fetch(
        `/api/v1/cases/sessions/${sessionId}/case`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' }
        }
      );

      if (!response.ok) {
        throw new Error(`Failed to get/create case: ${response.statusText}`);
      }

      const data = await response.json();
      const caseId = data.case_id;

      // Store in state and localStorage
      setCurrentCaseId(caseId);
      localStorage.setItem('active_case_id', caseId);

      return caseId;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      throw err;
    }
  }

  /**
   * Upload file to current case
   * Creates case if it doesn't exist yet
   */
  async function uploadFile(file: File, description?: string) {
    setLoading(true);
    setError(null);

    try {
      // Step 1: Ensure case exists
      const caseId = await ensureCaseExists();

      // Step 2: Upload file to case
      const formData = new FormData();
      formData.append('file', file);
      formData.append('session_id', sessionId);
      if (description) {
        formData.append('description', description);
      }

      const response = await fetch(`/api/v1/cases/${caseId}/data`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        throw new Error(`Upload failed: ${response.statusText}`);
      }

      return await response.json();
    } finally {
      setLoading(false);
    }
  }

  /**
   * Submit query to current case
   * Creates case if it doesn't exist yet
   */
  async function submitQuery(query: string) {
    setLoading(true);
    setError(null);

    try {
      // Step 1: Ensure case exists
      const caseId = await ensureCaseExists();

      // Step 2: Submit query to case
      const response = await fetch(`/api/v1/cases/${caseId}/queries`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query,
          session_id: sessionId,
        }),
      });

      if (!response.ok) {
        throw new Error(`Query failed: ${response.statusText}`);
      }

      return await response.json();
    } finally {
      setLoading(false);
    }
  }

  /**
   * Clear current case (for "New Chat" button)
   * NO backend call - just clears local state
   */
  function clearCase() {
    setCurrentCaseId(null);
    localStorage.removeItem('active_case_id');
    setError(null);
  }

  /**
   * Force create new case (optional - for explicit "start fresh" action)
   * Uses idempotency to prevent duplicates on retry
   */
  async function createNewCase(title?: string) {
    setLoading(true);
    setError(null);

    try {
      const idempotencyKey = `new_chat_${sessionId}_${Date.now()}`;
      const params = new URLSearchParams();
      if (title) params.append('title', title);
      params.append('force_new', 'true');

      const response = await fetch(
        `/api/v1/cases/sessions/${sessionId}/case?${params}`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'idempotency-key': idempotencyKey,
          },
        }
      );

      if (!response.ok) {
        throw new Error(`Failed to create case: ${response.statusText}`);
      }

      const data = await response.json();
      const caseId = data.case_id;

      setCurrentCaseId(caseId);
      localStorage.setItem('active_case_id', caseId);

      return caseId;
    } finally {
      setLoading(false);
    }
  }

  return {
    currentCaseId,
    loading,
    error,
    uploadFile,
    submitQuery,
    clearCase,
    createNewCase,
    ensureCaseExists,
  };
}
```

---

## Usage Examples

### Example 1: "New Chat" Button

```typescript
function ChatHeader() {
  const { clearCase } = useCaseManagement(sessionId);

  return (
    <button onClick={clearCase}>
      New Chat
    </button>
  );
}
```

**Behavior:** Clears local state, NO backend call

---

### Example 2: User Uploads File

```typescript
function FileUploader() {
  const { uploadFile } = useCaseManagement(sessionId);

  async function handleUpload(file: File) {
    try {
      // Automatically gets/creates case, then uploads
      const result = await uploadFile(file);
      console.log('Uploaded:', result);
    } catch (err) {
      console.error('Upload failed:', err);
    }
  }

  return <input type="file" onChange={(e) => handleUpload(e.target.files[0])} />;
}
```

**Behavior:**
1. Checks if case exists (localStorage or backend)
2. Creates case if needed
3. Uploads file to case

---

### Example 3: User Submits Query

```typescript
function QueryInput() {
  const { submitQuery } = useCaseManagement(sessionId);

  async function handleSubmit(query: string) {
    try {
      // Automatically gets/creates case, then submits query
      const response = await submitQuery(query);
      console.log('Response:', response.content);
    } catch (err) {
      console.error('Query failed:', err);
    }
  }

  return <input onSubmit={handleSubmit} />;
}
```

**Behavior:**
1. Checks if case exists (localStorage or backend)
2. Creates case if needed
3. Submits query to case

---

## Migration Checklist

### Phase 1: Remove Optimistic ID Pattern ✅

- [ ] Remove all `opt_case_*` ID generation code
- [ ] Remove ID reconciliation logic
- [ ] Remove retry logic for 404 errors
- [ ] Remove code that sends multiple parallel requests

### Phase 2: Implement Session-Based Pattern ✅

- [ ] Add `useCaseManagement()` hook (see example above)
- [ ] Update "New Chat" button to call `clearCase()` (no backend call)
- [ ] Update file upload to use `uploadFile()` helper
- [ ] Update query submit to use `submitQuery()` helper
- [ ] Use `localStorage` for case persistence (browser-wide)

### Phase 3: Update State Management ✅

- [ ] Store real `case_id` from backend (not optimistic ID)
- [ ] Initialize case_id from `localStorage` on startup
- [ ] Update case list to use real IDs only

### Phase 4: Testing ✅

- [ ] Test: Click "New Chat" → No backend call, UI clears
- [ ] Test: Upload file first → Case created, upload succeeds
- [ ] Test: Submit query first → Case created, query succeeds
- [ ] Test: Switch browser tabs → Same case persists (localStorage)
- [ ] Test: Upload multiple files → Same case used
- [ ] Test: No 404 errors on upload
- [ ] Test: No duplicate cases created
- [ ] Test: Network retry doesn't create duplicates (idempotency)

---

## API Flow Diagrams

### Flow 1: User Clicks "New Chat"

```
User clicks "New Chat"
  ↓
Frontend: clearCase()
  ├─ setCurrentCaseId(null)
  ├─ localStorage.removeItem('active_case_id')
  └─ Clear messages UI

NO BACKEND CALL
```

---

### Flow 2: User Uploads File (First Action)

```
User uploads file
  ↓
Frontend: uploadFile(file)
  ↓
Check: case_id exists?
  ├─ No → POST /cases/sessions/{session_id}/case
  │        ↓
  │        Backend: Returns {case_id: "uuid"}
  │        ↓
  │        Store in localStorage
  └─ Yes → Use existing case_id
  ↓
POST /cases/{case_id}/data with file
  ↓
Backend: Processes upload
  ↓
Frontend: Display upload result
```

---

### Flow 3: User Submits Query (First Action)

```
User types query
  ↓
Frontend: submitQuery(query)
  ↓
Check: case_id exists?
  ├─ No → POST /cases/sessions/{session_id}/case
  │        ↓
  │        Backend: Returns {case_id: "uuid"}
  │        ↓
  │        Store in localStorage
  └─ Yes → Use existing case_id
  ↓
POST /cases/{case_id}/queries with query
  ↓
Backend: Processes query with AI
  ↓
Frontend: Display AI response
```

---

### Flow 4: User Switches Browser Tabs

```
User on Tab A (copilot side panel visible)
  ↓
User switches to Tab B
  ↓
Side panel remains visible (same instance)
  ├─ State persists (React state)
  ├─ case_id persists (localStorage)
  └─ NO reload, NO state loss

NO BACKEND CALL
```

---

## Performance Characteristics

### First Action Performance

**Upload File (cold start):**
```
GET case from localStorage: 0ms (cache miss)
  ↓
POST /cases/sessions/{session_id}/case: 200ms
  ↓
POST /cases/{case_id}/data: 2000ms
  ↓
Total: 2200ms
```

**Upload File (warm start - case exists):**
```
GET case from localStorage: 0ms (cache hit)
  ↓
POST /cases/{case_id}/data: 2000ms
  ↓
Total: 2000ms (saved 200ms by skipping case creation)
```

### Subsequent Actions

After first action, all subsequent uploads/queries use existing case:
- No case creation call needed
- Direct upload/query to known case_id
- Performance: 2000ms (upload processing time)

---

## Error Handling

### Common Errors

**Error: 404 - Case not found**
```typescript
// This shouldn't happen if using ensureCaseExists()
// But if it does, recreate the case:
try {
  await uploadFile(file);
} catch (err) {
  if (err.status === 404) {
    // Case was deleted or session expired
    localStorage.removeItem('active_case_id');
    setCurrentCaseId(null);

    // Retry will create new case
    await uploadFile(file);
  }
}
```

**Error: 401 - Session expired**
```typescript
try {
  await submitQuery(query);
} catch (err) {
  if (err.status === 401) {
    // Session expired - refresh and retry
    await refreshSession();
    await submitQuery(query);
  }
}
```

**Error: Network failure during case creation**
```typescript
// Idempotency key prevents duplicates on retry
const idempotencyKey = `action_${Date.now()}`;

async function retryableAction() {
  for (let i = 0; i < 3; i++) {
    try {
      const response = await fetch(
        `/api/v1/cases/sessions/${sessionId}/case?force_new=true`,
        {
          headers: { 'idempotency-key': idempotencyKey }
        }
      );
      return await response.json();
    } catch (err) {
      if (i === 2) throw err; // Final retry failed
      await sleep(1000 * Math.pow(2, i)); // Exponential backoff
    }
  }
}
```

---

## Summary

**Key Takeaways:**

1. ✅ **"New Chat" = Clear local state only** (no backend call)
2. ✅ **First action = Create case lazily** (upload or query triggers creation)
3. ✅ **Use localStorage** (browser-wide, persists across tab switches)
4. ✅ **Session-based endpoint** is idempotent and safe
5. ✅ **2-call performance** is acceptable trade-off for reliability
6. ✅ **Side panel = ONE instance** per browser (no multi-tab complexity)
7. ✅ **Idempotency support** prevents duplicates on retry

**Migration:**
- Remove optimistic ID code
- Implement `useCaseManagement()` hook
- Update UI components to use new pattern
- Test thoroughly

**Questions?**
Contact backend team for support during implementation.
