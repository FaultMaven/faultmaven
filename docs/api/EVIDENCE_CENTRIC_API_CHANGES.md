# Evidence-Centric Design API Changes

**Version:** 2.0
**Date:** 2025-10-08
**Status:** Specification for Implementation

---

## Overview

The evidence-centric troubleshooting system replaces `suggested_actions` (clickable question templates) with `evidence_requests` (actionable diagnostic requests with HOW-TO guidance). This document specifies all API contract changes required for frontend integration.

---

## Breaking Changes Summary

### 1. **Agent Response Schema Changes**

**BEFORE (v3.0)**:
```json
{
  "schema_version": "3.0.0",
  "content": "What happened?",
  "suggested_actions": [
    {
      "label": "🔍 When did this start?",
      "query_template": "When did this start?",
      "action_type": "query"
    }
  ]
}
```

**AFTER (v3.1.0)**:
```json
{
  "schema_version": "3.1.0",
  "content": "I need diagnostic evidence to proceed.",
  "evidence_requests": [
    {
      "request_id": "er-001",
      "label": "Error timeline",
      "description": "When did errors start occurring?",
      "category": "timeline",
      "guidance": {
        "commands": ["grep 'ERROR' /var/log/app.log | head -1"],
        "file_locations": ["/var/log/app.log"],
        "ui_locations": ["Monitoring > Errors > Timeline"],
        "alternatives": ["Check Datadog for first error timestamp"],
        "prerequisites": ["SSH access to server"],
        "expected_output": "Timestamp of first error"
      },
      "status": "pending",
      "created_at_turn": 1,
      "completeness": 0.0,
      "metadata": {}
    }
  ],
  "suggested_actions": null
}
```

### 2. **Data Upload Endpoint Enhancement**

**Endpoint**: `POST /api/v1/data/upload`

**NEW Response** (immediate feedback):
```json
{
  "data_id": "uuid",
  "filename": "app.log",
  "file_metadata": {
    "filename": "app.log",
    "content_type": "text/plain",
    "size_bytes": 125000,
    "upload_timestamp": "2025-01-15T10:30:00Z",
    "file_id": "file-uuid"
  },
  "immediate_analysis": {
    "matched_requests": ["er-001", "er-003"],
    "key_findings": [
      "Found 1,247 errors in last 2 hours",
      "98% of errors from /api/v1/orders endpoint"
    ],
    "next_steps": "Phase advancing to Timeline establishment"
  }
}
```

---

## New Data Models

### EvidenceRequest

```typescript
interface EvidenceRequest {
  request_id: string;              // UUID
  label: string;                   // Max 100 chars, brief title
  description: string;             // Max 500 chars, what + why
  category: EvidenceCategory;
  guidance: AcquisitionGuidance;
  status: EvidenceStatus;
  created_at_turn: number;
  updated_at_turn: number | null;
  completeness: number;            // 0.0 - 1.0
  metadata: Record<string, any>;
}

enum EvidenceCategory {
  SYMPTOMS = "symptoms",           // Error messages, failures
  TIMELINE = "timeline",           // When did it start
  CHANGES = "changes",             // Recent deployments, config changes
  CONFIGURATION = "configuration", // Settings, env vars
  SCOPE = "scope",                 // How many affected
  METRICS = "metrics",             // Performance data
  ENVIRONMENT = "environment"      // Infrastructure state
}

enum EvidenceStatus {
  PENDING = "pending",             // User hasn't provided yet
  PARTIAL = "partial",             // Some info provided (0.3-0.7)
  COMPLETE = "complete",           // Fully answered (≥0.8)
  BLOCKED = "blocked",             // User can't provide
  OBSOLETE = "obsolete"            // No longer needed (hypothesis changed)
}
```

### AcquisitionGuidance

```typescript
interface AcquisitionGuidance {
  commands: string[];              // Max 3, shell commands to run
  file_locations: string[];        // Max 3, file paths to check
  ui_locations: string[];          // Max 3, UI navigation paths
  alternatives: string[];          // Max 3, alternative methods
  prerequisites: string[];         // Max 2, requirements
  expected_output: string | null;  // Max 200 chars, what to expect
}
```

### FileMetadata

```typescript
interface FileMetadata {
  filename: string;
  content_type: string;            // MIME type
  size_bytes: number;
  upload_timestamp: string;        // ISO 8601
  file_id: string;                 // Storage reference
}
```

### InvestigationMode

```typescript
enum InvestigationMode {
  ACTIVE_INCIDENT = "active_incident",  // Speed over certainty
  POST_MORTEM = "post_mortem"           // Depth over speed
}
```

### CaseStatus

```typescript
enum CaseStatus {
  INTAKE = "intake",               // Problem confirmation
  IN_PROGRESS = "in_progress",     // Active investigation
  RESOLVED = "resolved",           // Root cause found + fixed
  MITIGATED = "mitigated",         // Temporary fix, escalated
  STALLED = "stalled",             // Blocked evidence, can't proceed
  ABANDONED = "abandoned",         // User gave up / escalated
  CLOSED = "closed"                // Final state
}
```

---

## Updated Endpoints

### 1. Query Endpoint (Agent Chat)

**Endpoint**: `POST /api/v1/cases/{case_id}/query`

**Response Schema v3.1.0**:
```typescript
interface AgentResponse {
  schema_version: "3.1.0";
  content: string;
  response_type: ResponseType;
  session_id: string;

  // NEW: Evidence-centric fields
  evidence_requests: EvidenceRequest[];        // Replaces suggested_actions
  investigation_mode: InvestigationMode;       // How agent is operating
  case_status: CaseStatus;                     // Current case state

  // Existing fields
  view_state: ViewState;
  sources: Source[];
  plan: Plan | null;

  // DEPRECATED (still present for backward compat, will be null)
  suggested_actions: null;
}
```

**Example Response**:
```json
{
  "schema_version": "3.1.0",
  "content": "I need more diagnostic information to identify the root cause. Here's what would help:",
  "response_type": "NEEDS_MORE_DATA",
  "session_id": "sess-123",
  "investigation_mode": "active_incident",
  "case_status": "in_progress",
  "evidence_requests": [
    {
      "request_id": "er-001",
      "label": "Error rate metrics",
      "description": "Current error rate vs baseline to quantify severity",
      "category": "metrics",
      "guidance": {
        "commands": [
          "kubectl logs -l app=api --since=2h | grep '500' | wc -l"
        ],
        "file_locations": [],
        "ui_locations": ["Datadog > API Errors Dashboard"],
        "alternatives": ["Check New Relic error rate graph"],
        "prerequisites": ["kubectl access"],
        "expected_output": "Error count (baseline: 2-3/hour)"
      },
      "status": "pending",
      "created_at_turn": 1,
      "completeness": 0.0,
      "metadata": {}
    }
  ],
  "view_state": { /* ... */ },
  "sources": [],
  "plan": null,
  "suggested_actions": null
}
```

### 2. Data Upload Endpoint

**Endpoint**: `POST /api/v1/data/upload`

**Request** (unchanged):
```
POST /api/v1/data/upload
Content-Type: multipart/form-data

file: <binary>
session_id: string
case_id: string
description: string (optional)
```

**Response** (ENHANCED):
```typescript
interface DataUploadResponse {
  data_id: string;
  filename: string;
  file_metadata: FileMetadata;

  // NEW: Immediate classification and feedback
  immediate_analysis: {
    matched_requests: string[];      // Which evidence_request IDs this satisfies
    completeness_scores: Record<string, number>;  // request_id → score (0-1)
    key_findings: string[];          // Top 3-5 findings from file
    evidence_type: "supportive" | "refuting" | "neutral" | "absence";
    next_steps: string;              // What happens next
  };

  // OPTIONAL: Conflict detected
  conflict_detected?: {
    contradicted_hypothesis: string;
    reason: string;
    confirmation_required: true;
  };
}
```

**Example Response**:
```json
{
  "data_id": "data-456",
  "filename": "api_logs.txt",
  "file_metadata": {
    "filename": "api_logs.txt",
    "content_type": "text/plain",
    "size_bytes": 125000,
    "upload_timestamp": "2025-01-15T10:30:00Z",
    "file_id": "file-789"
  },
  "immediate_analysis": {
    "matched_requests": ["er-001", "er-002"],
    "completeness_scores": {
      "er-001": 0.9,
      "er-002": 1.0
    },
    "key_findings": [
      "98% of errors from /api/v1/orders endpoint",
      "Other endpoints functioning normally",
      "Started at 14:03 PM exactly"
    ],
    "evidence_type": "supportive",
    "next_steps": "Impact scope confirmed. Moving to Phase 2 (Timeline)."
  }
}
```

### 3. Case Details Endpoint

**Endpoint**: `GET /api/v1/cases/{case_id}`

**Response** (ADDED FIELDS):
```typescript
interface Case {
  // Existing fields
  case_id: string;
  title: string;
  created_at: string;
  // ...

  // NEW: Evidence tracking
  evidence_requests: EvidenceRequest[];      // Current active requests
  evidence_provided: EvidenceProvided[];     // User's submitted evidence
  investigation_mode: InvestigationMode;
  case_status: CaseStatus;
  confidence_score: number | null;           // 0-1, required for post_mortem

  // NEW: Deliverables (when resolved)
  deliverables?: {
    case_report_url: string | null;
    runbook_url: string | null;
  };
}

interface EvidenceProvided {
  evidence_id: string;
  turn_number: number;
  timestamp: string;
  form: "user_input" | "document";
  content: string;                           // Text or file reference
  file_metadata: FileMetadata | null;
  addresses_requests: string[];              // Evidence request IDs
  completeness: "partial" | "complete" | "over_complete";
  evidence_type: "supportive" | "refuting" | "neutral" | "absence";
  user_intent: UserIntent;
  key_findings: string[];
  confidence_impact: number | null;
}

enum UserIntent {
  PROVIDING_EVIDENCE = "providing_evidence",
  ASKING_QUESTION = "asking_question",
  REPORTING_UNAVAILABLE = "reporting_unavailable",
  REPORTING_STATUS = "reporting_status",
  CLARIFYING = "clarifying",
  OFF_TOPIC = "off_topic"
}
```

---

## Frontend UI Requirements

### 1. Evidence Request Display

**Replace** suggestion buttons with evidence request cards:

```tsx
// OLD: Suggested Actions
{response.suggested_actions?.map(action => (
  <button onClick={() => sendQuery(action.query_template)}>
    {action.label}
  </button>
))}

// NEW: Evidence Requests
{response.evidence_requests?.map(request => (
  <EvidenceRequestCard
    request={request}
    onProvideEvidence={(requestId) => handleEvidenceProvision(requestId)}
    onMarkBlocked={(requestId, reason) => handleBlocked(requestId, reason)}
  />
))}
```

**Evidence Request Card Design**:
```tsx
interface EvidenceRequestCardProps {
  request: EvidenceRequest;
  onProvideEvidence: (requestId: string) => void;
  onMarkBlocked: (requestId: string, reason: string) => void;
}

function EvidenceRequestCard({ request, onProvideEvidence, onMarkBlocked }: Props) {
  const [showGuidance, setShowGuidance] = useState(false);

  return (
    <Card className={getStatusColor(request.status)}>
      <CardHeader>
        <Badge>{request.category}</Badge>
        <StatusIcon status={request.status} />
      </CardHeader>

      <CardTitle>{request.label}</CardTitle>
      <CardDescription>{request.description}</CardDescription>

      <ProgressBar value={request.completeness * 100} />

      <Button onClick={() => setShowGuidance(!showGuidance)}>
        {showGuidance ? 'Hide' : 'Show'} How to Obtain
      </Button>

      {showGuidance && (
        <GuidanceSection>
          {request.guidance.commands.length > 0 && (
            <Section title="Commands to Run">
              {request.guidance.commands.map(cmd => (
                <CodeBlock>{cmd}</CodeBlock>
              ))}
            </Section>
          )}

          {request.guidance.file_locations.length > 0 && (
            <Section title="File Locations">
              {request.guidance.file_locations.map(path => (
                <FilePath>{path}</FilePath>
              ))}
            </Section>
          )}

          {request.guidance.ui_locations.length > 0 && (
            <Section title="UI Navigation">
              {request.guidance.ui_locations.map(loc => (
                <UIPath>{loc}</UIPath>
              ))}
            </Section>
          )}

          {request.guidance.alternatives.length > 0 && (
            <Section title="Alternatives">
              {request.guidance.alternatives.map(alt => (
                <Alternative>{alt}</Alternative>
              ))}
            </Section>
          )}
        </GuidanceSection>
      )}

      <CardFooter>
        <Button onClick={() => onProvideEvidence(request.request_id)}>
          Provide Evidence
        </Button>
        <Button variant="secondary" onClick={() => onMarkBlocked(request.request_id)}>
          Can't Access
        </Button>
      </CardFooter>
    </Card>
  );
}
```

### 2. Evidence Provision Flow

**Two paths** for providing evidence:

#### A. Text Input
```tsx
function ProvideEvidenceModal({ requestId }: { requestId: string }) {
  const [text, setText] = useState('');

  const handleSubmit = async () => {
    await fetch(`/api/v1/cases/${caseId}/query`, {
      method: 'POST',
      body: JSON.stringify({
        query: text,
        metadata: {
          responding_to_request: requestId  // Link to evidence request
        }
      })
    });
  };

  return (
    <Modal>
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Paste command output, error message, or other evidence..."
      />
      <Button onClick={handleSubmit}>Submit Evidence</Button>
    </Modal>
  );
}
```

#### B. File Upload
```tsx
function FileUploadModal({ requestId }: { requestId: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [description, setDescription] = useState('');

  const handleUpload = async () => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('case_id', caseId);
    formData.append('session_id', sessionId);
    formData.append('description', description);
    formData.append('responding_to_request', requestId);  // Link

    const response = await fetch('/api/v1/data/upload', {
      method: 'POST',
      body: formData
    });

    const result = await response.json();

    // Show immediate feedback
    showFeedback(result.immediate_analysis);
  };

  return (
    <Modal>
      <FileInput onChange={(e) => setFile(e.target.files[0])} />
      <Input
        placeholder="Optional: Describe what this file contains"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <Button onClick={handleUpload}>Upload Evidence</Button>
    </Modal>
  );
}
```

### 3. Status Indicators

Show case status and investigation mode:

```tsx
function CaseStatusBadge({ status, mode }: { status: CaseStatus, mode: InvestigationMode }) {
  return (
    <div className="flex gap-2">
      <Badge variant={getStatusVariant(status)}>
        {status}
      </Badge>
      {mode === 'active_incident' && (
        <Badge variant="warning">🚨 Active Incident</Badge>
      )}
      {mode === 'post_mortem' && (
        <Badge variant="info">🔍 Post-Mortem Analysis</Badge>
      )}
    </div>
  );
}
```

### 4. Evidence Conflict Handling

When refuting evidence detected:

```tsx
function ConflictAlert({ conflict }: { conflict: ConflictDetection }) {
  return (
    <Alert variant="warning">
      <AlertTitle>⚠️ Evidence Conflict Detected</AlertTitle>
      <AlertDescription>
        {conflict.reason}
      </AlertDescription>
      <AlertActions>
        <Button onClick={() => confirmRefutation()}>
          ✅ Confirm - Evidence is accurate
        </Button>
        <Button variant="secondary" onClick={() => disputeRefutation()}>
          ❌ Dispute - Evidence may be incorrect
        </Button>
        <Button variant="ghost" onClick={() => requestVerification()}>
          🤔 Uncertain - Need to verify
        </Button>
      </AlertActions>
    </Alert>
  );
}
```

---

## Migration Strategy

### Phase 1: Backward Compatibility (Current Sprint)

**Backend**: Return BOTH `suggested_actions` and `evidence_requests`
```json
{
  "schema_version": "3.1.0",
  "suggested_actions": null,           // Always null (deprecated)
  "evidence_requests": [ /* NEW */ ]
}
```

**Frontend**: Check for `evidence_requests` first, fall back to `suggested_actions`
```tsx
const requests = response.evidence_requests || response.suggested_actions || [];
```

### Phase 2: Evidence Request UI (Next Sprint)

- Implement `EvidenceRequestCard` component
- Add "Provide Evidence" and "Can't Access" flows
- Show immediate feedback after file upload

### Phase 3: Remove Deprecated Fields (Sprint +2)

- Backend stops sending `suggested_actions`
- Frontend removes fallback logic
- OpenAPI spec updated to remove deprecated fields

---

## Testing Checklist

### Backend
- [ ] Agent generates evidence_requests instead of suggested_actions
- [ ] File upload returns immediate_analysis
- [ ] Classification detects refuting evidence
- [ ] Case status transitions correctly (INTAKE → IN_PROGRESS → RESOLVED/STALLED/ABANDONED)
- [ ] Evidence lifecycle updates (PENDING → PARTIAL → COMPLETE)

### Frontend
- [ ] Evidence request cards render correctly
- [ ] Guidance sections expand/collapse
- [ ] Text evidence submission works
- [ ] File upload shows immediate feedback
- [ ] "Can't Access" marks request as BLOCKED
- [ ] Status badges show correct mode and state
- [ ] Conflict alerts display when refuting evidence detected

---

## Questions for Frontend Team

1. **UI Layout**: Should evidence requests be displayed in a sidebar, modal, or inline cards?
2. **Guidance Display**: Expand all by default or collapsed?
3. **Progress Tracking**: Show completeness bar for each request or overall case progress?
4. **Notifications**: Should we show toast notifications for immediate file analysis feedback?
5. **Mobile**: How should evidence request cards adapt for mobile view?

---

## Support

For questions or clarifications, contact:
- **Backend Team**: implementation questions
- **Design Review**: UX/UI guidance decisions
- **This Spec**: [Evidence-Centric Design Document](../architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md)
