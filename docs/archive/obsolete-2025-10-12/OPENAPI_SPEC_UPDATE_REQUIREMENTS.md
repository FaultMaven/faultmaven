# OpenAPI Spec Update Requirements

**Document Type:** API Contract Update Specification
**Date:** 2025-10-11
**Version:** 1.0
**Status:** ⚠️ **REQUIRES IMMEDIATE UPDATE**
**Current OpenAPI Version:** 3.1.0 (Evidence-Centric)
**Target OpenAPI Version:** 3.2.0 (OODA Response Formats)

---

## Executive Summary

**YES - The response format requirements SIGNIFICANTLY impact the OpenAPI spec.**

**Critical Findings:**

1. ❌ **`suggested_actions` is DEPRECATED** in current spec (v3.1.0) but we need it **ACTIVE** for OODA framework
2. ❌ **Missing fields**: `suggested_commands`, `clarifying_questions`, `problem_detected`, `command_validation`
3. ⚠️ **`view_state` is undefined** - needs complete schema definition
4. ⚠️ **No schema validation** for structured response fields
5. ❌ **OODA-specific fields missing**: `phase_complete`, `hypothesis_tested`, `test_result`, `new_hypotheses`

**Impact:** Frontend cannot consume OODA responses without OpenAPI spec updates.

---

## 1. Current OpenAPI AgentResponse Schema (v3.1.0)

**Source:** `docs/api/openapi.locked.yaml:3245-3319`

```yaml
AgentResponse:
  properties:
    schema_version:
      type: string
      title: Schema Version
      default: 3.1.0
    content:
      type: string
      title: Content
    response_type:
      $ref: '#/components/schemas/ResponseType'
    session_id:
      type: string
      title: Session Id
    case_id:
      anyOf:
        - type: string
        - type: 'null'
      title: Case Id
    confidence_score:
      anyOf:
        - type: number
        - type: 'null'
      title: Confidence Score
    sources:
      items:
        $ref: '#/components/schemas/Source'
      type: array
      title: Sources
    next_action_hint:
      anyOf:
        - type: string
        - type: 'null'
      title: Next Action Hint
    view_state:
      anyOf:
        - $ref: '#/components/schemas/ViewState'  # ← UNDEFINED!
        - type: 'null'
    plan:
      anyOf:
        - items:
            $ref: '#/components/schemas/PlanStep'
          type: array
        - type: 'null'
      title: Plan
    evidence_requests:
      items:
        $ref: '#/components/schemas/EvidenceRequest'
      type: array
      title: Evidence Requests
      description: Active evidence requests for this turn
    investigation_mode:
      $ref: '#/components/schemas/InvestigationMode'
      description: Current investigation approach (speed vs depth)
      default: active_incident
    case_status:
      $ref: '#/components/schemas/faultmaven__models__evidence__CaseStatus'
      default: intake
    suggested_actions:
      anyOf:
        - items:
            type: object  # ← NO SCHEMA!
          type: array
        - type: 'null'
      title: Suggested Actions
      description: DEPRECATED in v3.1.0 - Use evidence_requests instead. Always null in new responses.
      deprecated: true  # ← MARKED DEPRECATED!
  additionalProperties: true
  type: object
  required:
    - content
    - response_type
    - session_id
  title: AgentResponse
  description: The single, unified JSON payload returned from the backend (v3.1.0 - Evidence-Centric).
```

### Problems Identified:

1. **`suggested_actions` is deprecated** but OODA framework REQUIRES it
2. **`suggested_commands` field MISSING** entirely
3. **`clarifying_questions` field MISSING** entirely
4. **`problem_detected` field MISSING** (needed for Phase 0)
5. **`command_validation` field MISSING** (needed for "Can I run X?" queries)
6. **`view_state` schema UNDEFINED** - no structure specified
7. **`suggested_actions` has no schema** - just `type: object` with no properties
8. **OODA-specific fields MISSING**: `phase_complete`, `hypothesis_tested`, `test_result`, `new_hypotheses`, `scope_assessment`

---

## 2. Required OpenAPI Updates

### 2.1 Update AgentResponse Schema to v3.2.0

**File:** `docs/api/openapi.locked.yaml`

```yaml
AgentResponse:
  properties:
    # ===== EXISTING FIELDS (PRESERVED) =====
    schema_version:
      type: string
      title: Schema Version
      default: 3.2.0  # ← UPDATE VERSION
      description: API schema version (3.2.0 adds OODA response formats)

    content:
      type: string
      title: Content
      description: Natural language response to user (ALWAYS present)

    response_type:
      $ref: '#/components/schemas/ResponseType'

    session_id:
      type: string
      title: Session Id

    case_id:
      anyOf:
        - type: string
        - type: 'null'
      title: Case Id

    confidence_score:
      anyOf:
        - type: number
        - type: 'null'
      title: Confidence Score

    sources:
      items:
        $ref: '#/components/schemas/Source'
      type: array
      title: Sources
      default: []

    view_state:
      anyOf:
        - $ref: '#/components/schemas/ViewState'  # ← MUST DEFINE BELOW
        - type: 'null'
      description: UI state and investigation progress

    evidence_requests:
      items:
        $ref: '#/components/schemas/EvidenceRequest'
      type: array
      title: Evidence Requests
      default: []

    investigation_mode:
      $ref: '#/components/schemas/InvestigationMode'
      default: active_incident

    case_status:
      $ref: '#/components/schemas/faultmaven__models__evidence__CaseStatus'
      default: intake

    # ===== NEW FIELDS (OODA v3.2.0) =====

    # GUIDANCE FIELDS
    clarifying_questions:
      type: array
      items:
        type: string
        maxLength: 300
      maxItems: 3
      title: Clarifying Questions
      description: Questions to better understand user's intent (max 3)
      default: []

    suggested_actions:
      type: array
      items:
        $ref: '#/components/schemas/SuggestedAction'  # ← MUST DEFINE BELOW
      maxItems: 6
      title: Suggested Actions
      description: Clickable action suggestions to guide conversation (2-4 ideal). RE-ENABLED in v3.2.0 for OODA framework.
      default: []

    suggested_commands:
      type: array
      items:
        $ref: '#/components/schemas/CommandSuggestion'  # ← MUST DEFINE BELOW
      maxItems: 5
      title: Suggested Commands
      description: Diagnostic commands user can run (troubleshooting mode only)
      default: []

    command_validation:
      anyOf:
        - $ref: '#/components/schemas/CommandValidation'  # ← MUST DEFINE BELOW
        - type: 'null'
      title: Command Validation
      description: Validation response when user asks "Can I run X?"

    # PROBLEM DETECTION (Phase 0)
    problem_detected:
      type: boolean
      title: Problem Detected
      description: Whether problem signals detected in user query
      default: false

    problem_summary:
      anyOf:
        - type: string
          maxLength: 200
        - type: 'null'
      title: Problem Summary
      description: Brief problem summary if detected

    severity:
      anyOf:
        - type: string
          enum: [low, medium, high, critical]
        - type: 'null'
      title: Severity
      description: Problem severity if detected

    # PHASE CONTROL
    phase_complete:
      type: boolean
      title: Phase Complete
      description: Whether current phase objectives are met
      default: false

    should_advance:
      type: boolean
      title: Should Advance
      description: Whether to advance to next phase
      default: false

    # HYPOTHESIS TRACKING (Phase 3-4)
    new_hypotheses:
      type: array
      items:
        $ref: '#/components/schemas/Hypothesis'  # ← MUST DEFINE BELOW
      maxItems: 4
      title: New Hypotheses
      description: New hypotheses generated this turn (Phase 3)
      default: []

    hypothesis_tested:
      anyOf:
        - type: string
          maxLength: 200
        - type: 'null'
      title: Hypothesis Tested
      description: Hypothesis being tested (Phase 4 - Validation)

    test_result:
      anyOf:
        - $ref: '#/components/schemas/TestResult'  # ← MUST DEFINE BELOW
        - type: 'null'
      title: Test Result
      description: Test result with outcome and confidence impact (Phase 4)

    # SCOPE ASSESSMENT (Phase 1)
    scope_assessment:
      anyOf:
        - $ref: '#/components/schemas/ScopeAssessment'  # ← MUST DEFINE BELOW
        - type: 'null'
      title: Scope Assessment
      description: Blast radius assessment (Phase 1)

    # METADATA
    response_metadata:
      type: object
      additionalProperties: true
      title: Response Metadata
      description: Additional response metadata (tools used, confidence, etc.)
      default: {}

    timestamp:
      type: string
      format: date-time
      title: Timestamp
      description: Response generation time

  additionalProperties: true
  type: object
  required:
    - content
    - response_type
    - session_id
    - schema_version
  title: AgentResponse
  description: Unified response from OODA Investigation Framework (v3.2.0)
```

### 2.2 Define New Component Schemas

All these schemas must be added to `components/schemas` section:

#### SuggestedAction Schema

```yaml
SuggestedAction:
  type: object
  properties:
    label:
      type: string
      maxLength: 100
      title: Label
      description: Display text for button (e.g., "I have a Redis issue")
      example: "🔧 I have a Redis issue"
    type:
      type: string
      enum: [question_template, command, upload_data, transition, create_runbook]
      title: Type
      description: Action type determining behavior
    payload:
      type: string
      maxLength: 500
      title: Payload
      description: The actual question/command to submit when clicked
      example: "I'm experiencing Redis connection issues"
    icon:
      anyOf:
        - type: string
          maxLength: 10
        - type: 'null'
      title: Icon
      description: UI icon hint (emoji or icon name)
      example: "🔧"
    metadata:
      type: object
      additionalProperties: true
      title: Metadata
      description: Additional action metadata
      default: {}
  required:
    - label
    - type
    - payload
  title: SuggestedAction
  description: User-clickable action suggestion for UI guidance
```

#### CommandSuggestion Schema

```yaml
CommandSuggestion:
  type: object
  properties:
    command:
      type: string
      maxLength: 500
      title: Command
      description: The command to run
      example: "kubectl get pods -n production"
    description:
      type: string
      maxLength: 200
      title: Description
      description: Brief description of what command does
      example: "Check pod status in production namespace"
    why:
      type: string
      maxLength: 300
      title: Why
      description: Explanation of why this command is useful
      example: "This will show if any pods are failing or restarting"
    safety:
      type: string
      enum: [safe, read_only, caution]
      default: safe
      title: Safety
      description: Safety classification of command
    expected_output:
      anyOf:
        - type: string
          maxLength: 300
        - type: 'null'
      title: Expected Output
      description: What to look for in output
      example: "All pods should show Running with 1/1 ready"
  required:
    - command
    - description
    - why
  title: CommandSuggestion
  description: Diagnostic command suggestion with safety classification
```

#### CommandValidation Schema

```yaml
CommandValidation:
  type: object
  properties:
    command:
      type: string
      title: Command
      description: The command user wants to validate
    is_safe:
      type: boolean
      title: Is Safe
      description: Overall safety assessment
    safety_level:
      type: string
      enum: [safe, read_only, caution, dangerous]
      title: Safety Level
      description: Safety classification
    explanation:
      type: string
      maxLength: 500
      title: Explanation
      description: What the command does and its effects
    concerns:
      type: array
      items:
        type: string
      maxItems: 5
      title: Concerns
      description: Potential risks or issues
      default: []
    safer_alternative:
      anyOf:
        - type: string
        - type: 'null'
      title: Safer Alternative
      description: Alternative command if risky
    conditions_for_safety:
      type: array
      items:
        type: string
      maxItems: 5
      title: Conditions For Safety
      description: Conditions under which command is safe
      default: []
    should_diagnose_first:
      type: boolean
      default: false
      title: Should Diagnose First
      description: Whether user should diagnose root cause before running
  required:
    - command
    - is_safe
    - safety_level
    - explanation
  title: CommandValidation
  description: Response when user asks to validate a command
```

#### Hypothesis Schema

```yaml
Hypothesis:
  type: object
  properties:
    statement:
      type: string
      maxLength: 200
      title: Statement
      description: Hypothesis statement
      example: "Database connection pool exhausted"
    likelihood:
      type: number
      minimum: 0.0
      maximum: 1.0
      title: Likelihood
      description: Probability/confidence score (0.0-1.0)
      example: 0.75
    supporting_evidence:
      type: array
      items:
        type: string
      title: Supporting Evidence
      description: Evidence supporting this hypothesis
      default: []
    category:
      type: string
      enum: [configuration, code, infrastructure, dependency, data]
      title: Category
      description: Hypothesis category
    testing_strategy:
      type: string
      maxLength: 300
      title: Testing Strategy
      description: How to test this hypothesis
      example: "Check connection pool status: SHOW STATUS LIKE 'Threads_connected';"
    status:
      type: string
      enum: [pending, testing, validated, refuted]
      default: pending
      title: Status
      description: Current hypothesis status
  required:
    - statement
    - likelihood
    - testing_strategy
  title: Hypothesis
  description: Root cause hypothesis with testing strategy
```

#### TestResult Schema

```yaml
TestResult:
  type: object
  properties:
    test_description:
      type: string
      title: Test Description
      description: Description of test performed
    outcome:
      type: string
      enum: [supports, refutes, inconclusive]
      title: Outcome
      description: Test outcome
    confidence_impact:
      type: number
      minimum: -1.0
      maximum: 1.0
      title: Confidence Impact
      description: Impact on hypothesis confidence (+value increases, -value decreases)
    evidence_summary:
      type: string
      maxLength: 300
      title: Evidence Summary
      description: Summary of evidence collected
  required:
    - test_description
    - outcome
    - confidence_impact
  title: TestResult
  description: Result of hypothesis validation test
```

#### ScopeAssessment Schema

```yaml
ScopeAssessment:
  type: object
  properties:
    affected_scope:
      type: string
      enum: [all_users, user_subset, specific_users, unknown]
      title: Affected Scope
      description: Who is affected by the problem
    affected_components:
      type: array
      items:
        type: string
      title: Affected Components
      description: List of affected services/components
      default: []
    severity:
      type: string
      enum: [low, medium, high, critical]
      title: Severity
      description: Problem severity level
    impact_percentage:
      anyOf:
        - type: number
          minimum: 0
          maximum: 100
        - type: 'null'
      title: Impact Percentage
      description: Estimated percentage of impact
    impact_description:
      anyOf:
        - type: string
          maxLength: 300
        - type: 'null'
      title: Impact Description
      description: Textual description of impact
  required:
    - affected_scope
    - severity
  title: ScopeAssessment
  description: Blast radius assessment (Phase 1)
```

#### ViewState Schema (Complete Definition)

```yaml
ViewState:
  type: object
  properties:
    session_id:
      type: string
      title: Session Id
    user:
      $ref: '#/components/schemas/UserProfile'
    active_case:
      anyOf:
        - $ref: '#/components/schemas/Case'
        - type: 'null'
      title: Active Case
    cases:
      type: array
      items:
        $ref: '#/components/schemas/Case'
      title: Cases
      default: []
    messages:
      type: array
      items:
        $ref: '#/components/schemas/Message'
      title: Messages
      default: []
    uploaded_data:
      type: array
      items:
        $ref: '#/components/schemas/UploadedData'
      title: Uploaded Data
      default: []
    show_case_selector:
      type: boolean
      default: true
      title: Show Case Selector
    show_data_upload:
      type: boolean
      default: true
      title: Show Data Upload
    loading_state:
      anyOf:
        - type: string
        - type: 'null'
      title: Loading State
    memory_context:
      anyOf:
        - type: object
          additionalProperties: true
        - type: 'null'
      title: Memory Context
    planning_state:
      anyOf:
        - type: object
          additionalProperties: true
        - type: 'null'
      title: Planning State

    # OODA FRAMEWORK (v3.2.0)
    investigation_progress:
      anyOf:
        - $ref: '#/components/schemas/InvestigationProgress'  # ← MUST DEFINE BELOW
        - type: 'null'
      title: Investigation Progress
      description: OODA investigation progress tracking (v3.2.0)

  required:
    - session_id
    - user
  title: ViewState
  description: Complete view state including OODA investigation progress
```

#### InvestigationProgress Schema

```yaml
InvestigationProgress:
  type: object
  properties:
    phase:
      type: object
      properties:
        current:
          type: string
          enum: [INTAKE, BLAST_RADIUS, TIMELINE, HYPOTHESIS, VALIDATION, SOLUTION, DOCUMENT]
          title: Current Phase
        number:
          type: integer
          minimum: 0
          maximum: 6
          title: Phase Number
      required:
        - current
        - number
      title: Phase
      description: Current investigation phase

    engagement_mode:
      type: string
      enum: [consultant, lead_investigator]
      title: Engagement Mode
      description: Current engagement mode

    ooda_iteration:
      type: integer
      minimum: 0
      title: OODA Iteration
      description: Current OODA iteration within phase

    turn_count:
      type: integer
      minimum: 0
      title: Turn Count
      description: Total conversation turns

    case_status:
      type: string
      title: Case Status
      description: Current case status

    hypotheses:
      type: object
      properties:
        total:
          type: integer
          minimum: 0
          title: Total
        validated:
          anyOf:
            - type: string
            - type: 'null'
          title: Validated
        validated_confidence:
          anyOf:
            - type: number
              minimum: 0.0
              maximum: 1.0
            - type: 'null'
          title: Validated Confidence
      title: Hypotheses
      description: Hypothesis tracking summary

    evidence_collected:
      type: integer
      minimum: 0
      title: Evidence Collected
      description: Number of evidence items collected

    evidence_requested:
      type: integer
      minimum: 0
      title: Evidence Requested
      description: Number of pending evidence requests

    anomaly_frame:
      anyOf:
        - type: object
          properties:
            statement:
              type: string
              title: Statement
            severity:
              type: string
              title: Severity
            affected_components:
              type: array
              items:
                type: string
              title: Affected Components
          required:
            - statement
            - severity
        - type: 'null'
      title: Anomaly Frame
      description: Detected anomaly information

  required:
    - phase
    - engagement_mode
    - ooda_iteration
    - turn_count
  title: InvestigationProgress
  description: OODA investigation progress for frontend display
```

---

## 3. Impact on Frontend

### 3.1 TypeScript Type Generation

Frontend TypeScript types will be auto-generated from OpenAPI spec:

**Before (v3.1.0):**
```typescript
interface AgentResponse {
  content: string;
  response_type: ResponseType;
  session_id: string;
  // ... existing fields
  suggested_actions?: object[] | null;  // ← Deprecated, no schema
}
```

**After (v3.2.0):**
```typescript
interface AgentResponse {
  content: string;
  response_type: ResponseType;
  session_id: string;

  // NEW: Guidance fields
  clarifying_questions?: string[];
  suggested_actions?: SuggestedAction[];
  suggested_commands?: CommandSuggestion[];
  command_validation?: CommandValidation | null;

  // NEW: Problem detection
  problem_detected?: boolean;
  problem_summary?: string | null;
  severity?: "low" | "medium" | "high" | "critical" | null;

  // NEW: Phase control
  phase_complete?: boolean;
  should_advance?: boolean;

  // NEW: Hypothesis tracking
  new_hypotheses?: Hypothesis[];
  hypothesis_tested?: string | null;
  test_result?: TestResult | null;

  // NEW: Scope assessment
  scope_assessment?: ScopeAssessment | null;
}

interface SuggestedAction {
  label: string;
  type: "question_template" | "command" | "upload_data" | "transition" | "create_runbook";
  payload: string;
  icon?: string | null;
  metadata?: Record<string, any>;
}

interface CommandSuggestion {
  command: string;
  description: string;
  why: string;
  safety: "safe" | "read_only" | "caution";
  expected_output?: string | null;
}

// ... other interfaces
```

### 3.2 Frontend Impact Summary

**Breaking Changes:** ❌ NO (all new fields are optional)

**Frontend Must:**
1. ✅ Regenerate TypeScript types from updated OpenAPI spec
2. ✅ Update UI components to render `suggested_actions` (un-deprecated)
3. ✅ Add UI components for `suggested_commands`
4. ✅ Add UI components for `clarifying_questions`
5. ✅ Update `InvestigationProgressIndicator` to use `view_state.investigation_progress`
6. ✅ Handle phase-specific fields (hypotheses, test results, scope assessment)

**Backward Compatibility:** ✅ YES
- All new fields are optional
- Old clients continue working (just won't see new fields)
- `content` field still present (primary response)

---

## 4. Version Migration Strategy

### 4.1 Version Bump: 3.1.0 → 3.2.0

**Semantic Versioning:**
- **Major (3)**: Core architecture version
- **Minor (2)**: New features added (OODA response formats) - ← INCREMENT THIS
- **Patch (0)**: Bug fixes

**Change Summary:**
```yaml
# Version 3.1.0 (Evidence-Centric)
- Introduced evidence_requests
- Deprecated suggested_actions
- Added investigation_mode and case_status

# Version 3.2.0 (OODA Response Formats) ← NEW
- Re-enabled suggested_actions with full schema
- Added suggested_commands for diagnostic guidance
- Added clarifying_questions for ambiguous queries
- Added command_validation for safety checks
- Added phase_complete and should_advance for phase control
- Added OODA-specific fields (hypotheses, test_result, scope_assessment)
- Defined complete ViewState schema with investigation_progress
- Added InvestigationProgress schema for frontend display
```

### 4.2 Deployment Strategy

**Phase 1: Backend Implementation (Week 1-3)**
- Implement structured response generation (as specified in RESPONSE_FORMAT_INTEGRATION_SPEC.md)
- Update models to match new schemas
- Test with Postman/curl

**Phase 2: OpenAPI Spec Update (Week 3)**
- Update `openapi.locked.yaml` with all new schemas
- Regenerate OpenAPI documentation
- Publish updated spec

**Phase 3: Frontend Update (Week 4-5)**
- Regenerate TypeScript types from updated spec
- Implement UI components for new fields
- Test end-to-end with backend

**Phase 4: Validation (Week 5)**
- Validate all response formats against OpenAPI schema
- Integration testing
- Production deployment

---

## 5. Implementation Checklist

### Week 3: OpenAPI Spec Update

- [ ] Update `AgentResponse.schema_version` to `3.2.0`
- [ ] Remove `deprecated: true` from `suggested_actions`
- [ ] Add `clarifying_questions` field to `AgentResponse`
- [ ] Update `suggested_actions` with `$ref: '#/components/schemas/SuggestedAction'`
- [ ] Add `suggested_commands` field
- [ ] Add `command_validation` field
- [ ] Add `problem_detected`, `problem_summary`, `severity` fields
- [ ] Add `phase_complete`, `should_advance` fields
- [ ] Add `new_hypotheses`, `hypothesis_tested`, `test_result` fields
- [ ] Add `scope_assessment` field
- [ ] Add `response_metadata` field

### Component Schemas

- [ ] Define `SuggestedAction` schema
- [ ] Define `CommandSuggestion` schema
- [ ] Define `CommandValidation` schema
- [ ] Define `Hypothesis` schema
- [ ] Define `TestResult` schema
- [ ] Define `ScopeAssessment` schema
- [ ] Complete `ViewState` schema (currently just a reference)
- [ ] Define `InvestigationProgress` schema

### Documentation

- [ ] Update API documentation with v3.2.0 changes
- [ ] Add migration guide (3.1.0 → 3.2.0)
- [ ] Update README with new response fields
- [ ] Add examples for each new field

### Testing

- [ ] Validate updated spec with OpenAPI validator
- [ ] Generate TypeScript types from spec
- [ ] Create Postman collection with v3.2.0 examples
- [ ] Integration tests with frontend

---

## 6. Summary

**Question:** Does the response format requirements have any impact on OpenAPI spec?

**Answer:** **YES - SIGNIFICANT IMPACT**

**Required Changes:**
1. ✅ Version bump: `3.1.0` → `3.2.0`
2. ✅ Un-deprecate `suggested_actions` and add full schema
3. ✅ Add 4 new guidance fields (commands, questions, validation)
4. ✅ Add 3 new problem detection fields
5. ✅ Add 2 new phase control fields
6. ✅ Add 4 new hypothesis tracking fields
7. ✅ Add 1 new scope assessment field
8. ✅ Define 7 new component schemas
9. ✅ Complete `ViewState` and `InvestigationProgress` definitions

**Timeline:** Week 3 of implementation (after backend implements structured responses)

**Breaking Changes:** ❌ NONE (all fields optional, backward compatible)

**Frontend Impact:** ✅ Must regenerate types and implement UI for new fields

---

**Document Status:** ✅ Complete
**Next Action:** Update openapi.locked.yaml (Week 3 of migration)
**Related Documents:**
- [Response Format Specification Gap Analysis](./RESPONSE_FORMAT_SPECIFICATION_GAP_ANALYSIS.md)
- [Response Format Integration Specification](./RESPONSE_FORMAT_INTEGRATION_SPEC.md)
- [Prompt Engineering Architecture](./prompt-engineering-architecture.md)
- [OODA Frontend Requirements](./OODA_FRONTEND_REQUIREMENTS.md)
