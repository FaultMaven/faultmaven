# OpenAPI Spec Proposal: Evidence-Centric API v3.1.0

**Date**: 2025-10-08
**Status**: ✅ **APPROVED BY BACKEND**
**Requested By**: Frontend Team
**Reference**: [EVIDENCE_CENTRIC_API_CHANGES.md](./EVIDENCE_CENTRIC_API_CHANGES.md)

**Backend Review Notes**:
- ✅ Schemas reviewed and approved
- ⚠️ Minor issues fixed: Added missing `UserIntent`, `CompletenessLevel` enums and `EvidenceProvided` schema
- ✅ Ready for implementation

---

## Overview

This document proposes specific OpenAPI schema changes to support the evidence-centric troubleshooting system. These changes update the API contract from v3.0.0 to v3.1.0.

**Key Changes**:
1. Add new evidence-related schemas to `components/schemas`
2. Update `AgentResponse` schema with new fields
3. Enhance `DataUploadResponse` with immediate analysis
4. Update `Case` schema with evidence tracking
5. Deprecate `suggested_actions` field

---

## Required Schema Additions

### 1. EvidenceCategory Enum

```yaml
EvidenceCategory:
  type: string
  enum:
    - symptoms
    - timeline
    - changes
    - configuration
    - scope
    - metrics
    - environment
  description: Categories of diagnostic evidence
```

### 2. EvidenceStatus Enum

```yaml
EvidenceStatus:
  type: string
  enum:
    - pending
    - partial
    - complete
    - blocked
    - obsolete
  description: Status of evidence request fulfillment
```

### 3. InvestigationMode Enum

```yaml
InvestigationMode:
  type: string
  enum:
    - active_incident
    - post_mortem
  description: How the agent operates (speed vs depth)
```

### 4. CaseStatus Enum

```yaml
CaseStatus:
  type: string
  enum:
    - intake
    - in_progress
    - resolved
    - mitigated
    - stalled
    - abandoned
    - closed
  description: Current case investigation state
```

### 5. UserIntent Enum

```yaml
UserIntent:
  type: string
  enum:
    - providing_evidence
    - asking_question
    - reporting_unavailable
    - reporting_status
    - clarifying
    - off_topic
  description: User's intent when submitting input
```

### 6. CompletenessLevel Enum

```yaml
CompletenessLevel:
  type: string
  enum:
    - partial
    - complete
    - over_complete
  description: How completely evidence satisfies a request
```

### 7. AcquisitionGuidance Schema

```yaml
AcquisitionGuidance:
  type: object
  properties:
    commands:
      type: array
      items:
        type: string
      maxItems: 3
      description: Shell commands to run
      default: []
    file_locations:
      type: array
      items:
        type: string
      maxItems: 3
      description: File paths to check
      default: []
    ui_locations:
      type: array
      items:
        type: string
      maxItems: 3
      description: UI navigation paths
      default: []
    alternatives:
      type: array
      items:
        type: string
      maxItems: 3
      description: Alternative methods to obtain evidence
      default: []
    prerequisites:
      type: array
      items:
        type: string
      maxItems: 2
      description: Requirements to obtain evidence
      default: []
    expected_output:
      type: string
      nullable: true
      maxLength: 200
      description: What the user should expect to see
  required:
    - commands
    - file_locations
    - ui_locations
    - alternatives
    - prerequisites
```

### 8. EvidenceRequest Schema

```yaml
EvidenceRequest:
  type: object
  properties:
    request_id:
      type: string
      format: uuid
      description: Unique identifier for this evidence request
    label:
      type: string
      maxLength: 100
      description: Brief title for the request
    description:
      type: string
      maxLength: 500
      description: What evidence is needed and why
    category:
      $ref: '#/components/schemas/EvidenceCategory'
    guidance:
      $ref: '#/components/schemas/AcquisitionGuidance'
    status:
      $ref: '#/components/schemas/EvidenceStatus'
    created_at_turn:
      type: integer
      minimum: 1
      description: Turn number when request was created
    updated_at_turn:
      type: integer
      nullable: true
      description: Turn number when last updated
    completeness:
      type: number
      format: float
      minimum: 0.0
      maximum: 1.0
      description: Fulfillment completeness score
    metadata:
      type: object
      additionalProperties: true
      description: Additional context
      default: {}
  required:
    - request_id
    - label
    - description
    - category
    - guidance
    - status
    - created_at_turn
    - completeness
    - metadata
```

### 9. FileMetadata Schema

```yaml
FileMetadata:
  type: object
  properties:
    filename:
      type: string
      description: Original filename
    content_type:
      type: string
      description: MIME type
    size_bytes:
      type: integer
      minimum: 0
      description: File size in bytes
    upload_timestamp:
      type: string
      format: date-time
      description: When file was uploaded (ISO 8601)
    file_id:
      type: string
      description: Storage reference ID
  required:
    - filename
    - content_type
    - size_bytes
    - upload_timestamp
    - file_id
```

### 10. EvidenceProvided Schema

```yaml
EvidenceProvided:
  type: object
  properties:
    evidence_id:
      type: string
      format: uuid
      description: Unique identifier for this evidence submission
    turn_number:
      type: integer
      minimum: 1
      description: Turn when evidence was provided
    timestamp:
      type: string
      format: date-time
      description: When evidence was submitted (ISO 8601)
    form:
      type: string
      enum:
        - user_input
        - document
      description: How evidence was provided
    content:
      type: string
      description: Evidence content or file reference
    file_metadata:
      $ref: '#/components/schemas/FileMetadata'
      nullable: true
      description: Present if form is document
    addresses_requests:
      type: array
      items:
        type: string
      description: Evidence request IDs this addresses
      default: []
    completeness:
      $ref: '#/components/schemas/CompletenessLevel'
    evidence_type:
      type: string
      enum:
        - supportive
        - refuting
        - neutral
        - absence
      description: How this evidence relates to hypotheses
    user_intent:
      $ref: '#/components/schemas/UserIntent'
    key_findings:
      type: array
      items:
        type: string
      description: Key findings extracted from evidence
      default: []
    confidence_impact:
      type: number
      format: float
      nullable: true
      minimum: -1.0
      maximum: 1.0
      description: Impact on case confidence (-1 to 1)
  required:
    - evidence_id
    - turn_number
    - timestamp
    - form
    - content
    - addresses_requests
    - completeness
    - evidence_type
    - user_intent
    - key_findings
```

### 11. ImmediateAnalysis Schema

```yaml
ImmediateAnalysis:
  type: object
  properties:
    matched_requests:
      type: array
      items:
        type: string
      description: Evidence request IDs this data satisfies
      default: []
    completeness_scores:
      type: object
      additionalProperties:
        type: number
        format: float
        minimum: 0.0
        maximum: 1.0
      description: Map of request_id to completeness score
      default: {}
    key_findings:
      type: array
      items:
        type: string
      maxItems: 5
      description: Top findings from the uploaded data
      default: []
    evidence_type:
      type: string
      enum:
        - supportive
        - refuting
        - neutral
        - absence
      description: How this evidence relates to current hypotheses
    next_steps:
      type: string
      description: What the agent will do next
  required:
    - matched_requests
    - completeness_scores
    - key_findings
    - evidence_type
    - next_steps
```

### 12. ConflictDetection Schema

```yaml
ConflictDetection:
  type: object
  properties:
    contradicted_hypothesis:
      type: string
      description: Which hypothesis is contradicted
    reason:
      type: string
      description: Why this is a conflict
    confirmation_required:
      type: boolean
      const: true
      description: User must confirm refutation
  required:
    - contradicted_hypothesis
    - reason
    - confirmation_required
```

---

## Schema Updates

### 1. Update AgentResponse Schema

**Location**: `components/schemas/AgentResponse`

**Changes**:
```yaml
AgentResponse:
  type: object
  properties:
    schema_version:
      type: string
      default: "3.1.0"  # UPDATED from 3.0.0
    content:
      type: string
    response_type:
      $ref: '#/components/schemas/ResponseType'
    session_id:
      type: string
    case_id:
      type: string
      nullable: true
    confidence_score:
      type: number
      nullable: true
    sources:
      type: array
      items:
        $ref: '#/components/schemas/Source'
      default: []
    next_action_hint:
      type: string
      nullable: true
    view_state:
      $ref: '#/components/schemas/ViewState'
      nullable: true
    plan:
      type: array
      items:
        $ref: '#/components/schemas/PlanStep'
      nullable: true

    # NEW FIELDS
    evidence_requests:
      type: array
      items:
        $ref: '#/components/schemas/EvidenceRequest'
      description: Active evidence requests for this turn
      default: []
    investigation_mode:
      $ref: '#/components/schemas/InvestigationMode'
      description: Current investigation approach
    case_status:
      $ref: '#/components/schemas/CaseStatus'
      description: Current case state

    # DEPRECATED (kept for backward compatibility)
    suggested_actions:
      type: array
      nullable: true
      deprecated: true
      description: "DEPRECATED in v3.1.0 - Use evidence_requests instead. Always null in new responses."

  required:
    - schema_version
    - content
    - response_type
    - session_id
    - evidence_requests  # NEW REQUIRED FIELD
    - investigation_mode  # NEW REQUIRED FIELD
    - case_status  # NEW REQUIRED FIELD
```

### 2. Create DataUploadResponse Schema

**Location**: `components/schemas/DataUploadResponse`

**New Schema**:
```yaml
DataUploadResponse:
  type: object
  properties:
    data_id:
      type: string
      description: Unique identifier for uploaded data
    filename:
      type: string
      description: Original filename
    file_metadata:
      $ref: '#/components/schemas/FileMetadata'
    immediate_analysis:
      $ref: '#/components/schemas/ImmediateAnalysis'
    conflict_detected:
      $ref: '#/components/schemas/ConflictDetection'
      nullable: true
      description: Present only if refuting evidence detected
  required:
    - data_id
    - filename
    - file_metadata
    - immediate_analysis
```

### 3. Update Case Schema

**Location**: `components/schemas/Case`

**Add Fields**:
```yaml
Case:
  type: object
  properties:
    # ... existing fields ...

    # NEW FIELDS
    evidence_requests:
      type: array
      items:
        $ref: '#/components/schemas/EvidenceRequest'
      description: Current active evidence requests
      default: []
    evidence_provided:
      type: array
      items:
        $ref: '#/components/schemas/EvidenceProvided'
      description: User's submitted evidence
      default: []
    investigation_mode:
      $ref: '#/components/schemas/InvestigationMode'
      description: Investigation approach for this case
    case_status:
      $ref: '#/components/schemas/CaseStatus'
      description: Current case state
    confidence_score:
      type: number
      format: float
      nullable: true
      minimum: 0.0
      maximum: 1.0
      description: Overall confidence in findings (required for post_mortem mode)

  required:
    - case_id
    - title
    - created_at
    - updated_at
    - status
    - priority
    - investigation_mode  # NEW REQUIRED
    - case_status  # NEW REQUIRED
```

---

## Endpoint Response Updates

### 1. POST /api/v1/cases/{case_id}/queries

**Response Schema**: Update to use new `AgentResponse` schema (already defined above)

**No endpoint signature changes needed** - response schema change only.

### 2. POST /api/v1/data/upload

**Current Response** (lines 127-131 in openapi.locked.yaml):
```yaml
responses:
  '201':
    description: Successful Response
    content:
      application/json:
        schema: {}  # Currently untyped
```

**Proposed Update**:
```yaml
responses:
  '201':
    description: Data uploaded and analyzed successfully
    content:
      application/json:
        schema:
          $ref: '#/components/schemas/DataUploadResponse'
    headers:
      X-Correlation-ID:
        description: Request correlation ID
        schema:
          type: string
```

### 3. GET /api/v1/cases/{case_id}

**Response Schema**: Update to use modified `Case` schema (already defined above)

**No endpoint signature changes needed** - response schema change only.

---

## Backward Compatibility Strategy

### Phase 1: Dual Field Support (Weeks 1-2)

Backend sends BOTH old and new fields:
```json
{
  "schema_version": "3.1.0",
  "suggested_actions": null,  // Always null but present
  "evidence_requests": [...]   // New field populated
}
```

Frontend checks for new field first:
```typescript
const requests = response.evidence_requests || response.suggested_actions || [];
```

### Phase 2: Deprecation Notice (Week 3)

- Update OpenAPI spec with `deprecated: true` on `suggested_actions`
- Backend logs warning when old field is accessed
- Frontend migrated to use only `evidence_requests`

### Phase 3: Removal (Week 4+)

- Remove `suggested_actions` from OpenAPI spec
- Backend stops sending the field
- Frontend removes fallback logic

---

## Migration Impact

### Breaking Changes
- ❌ **None initially** - dual field support maintains compatibility
- ⚠️ **Future breaking change**: `suggested_actions` removal (Phase 3)

### Non-Breaking Changes
- ✅ New optional fields in `AgentResponse`
- ✅ New response type for data upload endpoint
- ✅ New enum types
- ✅ Enhanced `Case` schema with backward-compatible additions

### API Version
- **Current**: 3.0.0
- **Proposed**: 3.1.0
- **Reasoning**: New fields added, deprecated field marked, no immediate breaking changes

---

## Validation Rules

### Evidence Requests
1. `request_id` must be unique within a case
2. `completeness` must be between 0.0 and 1.0
3. `created_at_turn` must be ≥ 1
4. `updated_at_turn` must be ≥ `created_at_turn` if present
5. At least one guidance field must be non-empty

### Investigation Mode
1. `active_incident` mode may have `confidence_score` as null
2. `post_mortem` mode MUST have `confidence_score` when `case_status` is `resolved`

### Case Status Transitions
Valid transitions:
- `intake` → `in_progress` | `abandoned`
- `in_progress` → `resolved` | `mitigated` | `stalled` | `abandoned`
- `resolved` → `closed`
- `mitigated` → `closed`
- `stalled` → `in_progress` | `abandoned`
- `abandoned` → `closed`

---

## Testing Requirements

### Backend Schema Validation
- [ ] All new schemas pass OpenAPI validation
- [ ] `AgentResponse` serializes correctly with new fields
- [ ] `DataUploadResponse` returns valid `immediate_analysis`
- [ ] Enum values match specification exactly
- [ ] Required fields are enforced

### Contract Testing
- [ ] OpenAPI spec validates against actual responses
- [ ] Frontend TypeScript types match OpenAPI schema
- [ ] Backward compatibility maintained (Phase 1)
- [ ] Deprecated field warnings logged (Phase 2)

---

## Approval Checklist

**Backend Team** must verify:
- [ ] Schema definitions are accurate and complete
- [ ] Field types and constraints are correct
- [ ] Enum values match backend implementation
- [ ] Validation rules are implementable
- [ ] Migration strategy is feasible
- [ ] Performance impact is acceptable

**Sign-Off Required**:
- [ ] Backend Lead: ________________ (Date: ________)
- [ ] API Architect: ________________ (Date: ________)

---

## Questions for Backend Team

1. **Schema Accuracy**: Are all field types, constraints, and enum values correct?
2. **Required Fields**: Are the new required fields (`evidence_requests`, `investigation_mode`, `case_status`) acceptable?
3. **Validation Rules**: Are the proposed validation rules (completeness 0-1, status transitions) correct?
4. **Performance**: What is the expected performance impact of immediate file analysis?
5. **Migration Timeline**: Is the 4-week migration timeline realistic?
6. **Backward Compatibility**: Any concerns with the dual-field support approach?

---

## Next Steps

1. **Backend Review**: Backend team reviews and approves this proposal
2. **OpenAPI Update**: Merge approved changes into `openapi.locked.yaml`
3. **Backend Implementation**: Implement schema changes and validation
4. **Frontend Implementation**: Generate TypeScript types from updated spec
5. **Integration Testing**: Verify contract compliance
6. **Production Rollout**: Deploy with Phase 1 backward compatibility

---

**Status**: ✅ **APPROVED - READY FOR IMPLEMENTATION**

**Approval Sign-Off**:
- [x] Backend Team: Approved with minor corrections (2025-10-08)
- [x] Schema Completeness: All 12 schemas defined
- [x] Validation Rules: Verified and approved

**Contact**: Frontend Team
**Document Version**: 1.1 (Corrected)
**Last Updated**: 2025-10-08
