# Implementation Plan: Unified Ingestion Pipeline v4.1

**Status**: IN PROGRESS (Phase 1-2 complete, Phase 3+ pending)
**Date**: 2026-02-21
**Design Spec**: `docs/architecture/data-processing/data-preprocessing-design-specification.md` v4.1
**Branch**: `feat/unified-ingestion-pipeline`

### Completion Tracker

| Phase | Status | Commit |
|-------|--------|--------|
| Phase 1: Schema & Data Model Changes | DONE | `d57fe09` |
| Phase 2: Unified Endpoint & Pipeline | DONE | `17e4afe` |
| Tests: Phase 1-2 test updates | DONE | `ad802c1` |
| Phase 3: Milestone Engine Cleanup | NOT STARTED | — |
| Phase 4: Context Sliding Window | NOT STARTED | — |
| Phase 5: Prompt Template Updates | NOT STARTED | — |
| Phase 6: Test Rewrite | NOT STARTED | — |
| Phase 7: Frontend (Copilot) | NOT STARTED | — |

---

## Guiding Principle

**Clean break. No backward compatibility.** This is not a migration — it is a replacement. Old endpoints, old schemas, old classification logic, and old code paths are deleted, not deprecated. The codebase after implementation should read as if the old design never existed.

---

## Executive Summary

This plan implements the Unified Ingestion Pipeline — a fundamental restructuring of how FaultMaven processes user turns. The core change: every turn becomes `{query?, attachments?[]}`, processed through a strict two-step pipeline (preprocess attachments → LLM inference).

### What Changes

| Area | What Happens |
|------|-------------|
| **Endpoint** | `/queries` and `/data` are deleted. Replaced by `POST /cases/{id}/turns` |
| **Query classification** | `SubmissionClassification` schema deleted. `_determine_evidence_form()` deleted. `mixed` and `submitted_data` types deleted from prompts and schemas |
| **Pipeline** | `process_turn()` rewritten to accept `TurnPayload`. Two-step: preprocess attachments (Tier 0+1) → LLM inference |
| **Context** | Context Sliding Window: structural index included in LLM prompt for recent evidence. Fixes "I don't have access to file content" bug |
| **Prompts** | All classification instructions removed. Anti-hallucination rules updated. Data Access Strategy updated |
| **Config** | `TIER2_*` renamed to `DEEP_ANALYSIS_*`. New `EVIDENCE_CONTEXT_*` vars added |
| **Frontend** | Copilot calls `/turns`. Paste Data scratchpad added. Old API functions replaced |
| **Tests** | Old tests rewritten. New tests for unified pipeline, context window, and endpoint |

### Estimated File Changes

| Category | Files | Lines (est.) |
|----------|-------|-------------|
| Backend (new/modified) | ~12 | ~800 |
| Prompt templates | 1 | ~200 |
| Schemas/models | 3 | ~150 |
| Config | 2 | ~30 |
| Tests (rewrite existing) | ~8 | ~400 |
| Tests (new) | ~3 | ~600 |
| Frontend (copilot) | ~5 | ~300 |
| **Total** | **~34** | **~2,480** |

---

## Dependency Graph

```
Phase 1 ──→ Phase 2 ──→ Phase 3 ──→ Phase 5
  │              │           │
  │              ▼           │
  │         Phase 4 ────────→│
  │                          │
  └──────────────────────────┴──→ Phase 6 ──→ Phase 7
```

- Phase 1 (Schema) blocks everything
- Phase 2 (Endpoint + Pipeline) blocks Phase 3 (Milestone Engine) and Phase 5 (Prompts)
- Phase 4 (Context Window) can run in parallel with Phase 3
- Phase 6 (Tests) needs Phases 1-5 complete
- Phase 7 (Frontend) can start after Phase 2 (endpoint exists)

---

## Phase 1: Schema & Data Model Changes

**Goal**: Replace schemas and data models. Delete old classification types.

### 1.1 Create TurnPayload and Attachment

**File**: `faultmaven/core/investigation/schemas.py`

```python
@dataclass
class Attachment:
    """A file or pasted data submitted with a turn."""
    content: bytes
    filename: str
    content_type: str
    source_metadata: Optional[Dict[str, Any]] = None

@dataclass
class TurnPayload:
    """Universal turn payload — canonical input to the investigation pipeline.

    Every user turn is represented as an optional query + optional attachments.
    At least one must be provided. If attachments are present, they are preprocessed
    through Tier 0+1 before the LLM sees them. If no query is provided, an implicit
    system query is injected.
    """
    query: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)
    intent: Optional[QueryIntent] = None

    @property
    def has_attachments(self) -> bool:
        return len(self.attachments) > 0

    @property
    def has_query(self) -> bool:
        return self.query is not None and self.query.strip() != ""
```

### 1.2 Delete SubmissionClassification

**File**: `faultmaven/core/investigation/schemas.py`

- Delete the `SubmissionClassification` class entirely (lines 98-141)
- Remove `submission_classification` field from all 5 response state_update classes:
  - `InquiryResponse.InquiryStateUpdate` (line 538)
  - `InvestigationResponse_Diagnosis.DiagnosisStateUpdate` (line 585)
  - `InvestigationResponse_Mitigation.MitigationStateUpdate` (line 621)
  - `InvestigationResponse_Treatment.TreatmentStateUpdate` (line 651)
  - `InvestigationResponse_General.GeneralStateUpdate` (line 686)

### 1.3 Update EvidenceForm

**File**: `faultmaven/modules/case/domain/models.py` (lines 1388-1405)

Update docstring to reflect payload-driven semantics:

```python
class EvidenceForm(str, Enum):
    """How evidence entered the system.

    Form is determined by payload context:
    - DOCUMENT: Turn had attachments (file upload or pasted data)
    - USER_TEXT: Query-only turn, no attachments
    - SUBMITTED_DATA: Evidence created by agent tools (search_file, deep_analyze_file)
    """
    DOCUMENT = "document"
    USER_TEXT = "user_text"
    SUBMITTED_DATA = "submitted_data"
```

### 1.4 Create TurnResponse model

**File**: `faultmaven/models/api_models.py`

```python
class AttachmentResult(BaseModel):
    """Result of preprocessing a single attachment."""
    evidence_id: str
    filename: str
    data_type: str
    file_size: int
    processing_status: str

class TurnResponse(BaseModel):
    """Response for POST /cases/{id}/turns."""
    agent_response: str
    turn_number: int
    milestones_completed: List[str]
    case_status: CaseStatus
    progress_made: bool
    is_stuck: bool
    attachments_processed: List[AttachmentResult] = Field(default_factory=list)
```

### 1.5 Delete old request/response models

**File**: `faultmaven/models/api_models.py`

- Delete `CaseQueryRequest` (lines 405-429) — replaced by `TurnPayload`
- Delete `CaseQueryResponse` (lines 431-443) — replaced by `TurnResponse`

**File**: `faultmaven/models/api.py`

- Delete `DataUploadResponse` (lines 436-482) — replaced by `TurnResponse`

### 1.6 Delete old intent models if unused elsewhere

**File**: `faultmaven/models/api_models.py`

Review `QueryIntent` and `IntentType` (lines 346-403). Keep them — they're reused in `TurnPayload.intent`. But remove any fields only relevant to the old request model.

---

## Phase 2: Unified Endpoint & Pipeline

**Goal**: Create `POST /cases/{id}/turns`. Rewrite `process_turn()` with the two-step pipeline. Delete old endpoints.

### 2.1 Create unified endpoint

**File**: `faultmaven/modules/case/api/routes.py`

```python
@router.post("/{case_id}/turns", response_model=TurnResponse)
@trace("api_submit_turn")
async def submit_turn(
    case_id: str,
    query: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
    pasted_content: Optional[str] = Form(None),
    intent_type: Optional[str] = Form(None),
    intent_data: Optional[str] = Form(None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    investigation_service=Depends(get_investigation_service),
    current_user: UserDTO = Depends(require_authentication),
) -> TurnResponse:
    """Submit a turn to a case investigation.

    A turn consists of an optional query and/or optional attachments.
    Attachments are preprocessed through Tier 0+1 before the LLM sees them.
    If no query is provided with attachments, an implicit query is generated.
    """
    # Validate case access
    case = await case_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Build TurnPayload
    attachments = []
    for f in files:
        content = await f.read()
        attachments.append(Attachment(
            content=content,
            filename=f.filename,
            content_type=f.content_type or "application/octet-stream",
        ))
    if pasted_content:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        attachments.append(Attachment(
            content=pasted_content.encode("utf-8"),
            filename=f"pasted-content-{ts}.txt",
            content_type="text/plain",
        ))

    intent = None
    if intent_type:
        data = json.loads(intent_data) if intent_data else {}
        intent = QueryIntent(type=IntentType(intent_type), **data)

    payload = TurnPayload(query=query, attachments=attachments, intent=intent)

    # Process turn
    response = await investigation_service.process_turn(
        case_id=case_id, user_id=current_user.user_id, payload=payload
    )
    return response
```

### 2.2 Delete old endpoints

**File**: `faultmaven/modules/case/api/routes.py`

- Delete `submit_case_query()` handler (lines 2015-2216) — the entire `/queries` endpoint
- Delete `upload_case_data()` handler (lines 2408-2550) — the entire `/data` endpoint
- Delete any helper functions used only by those endpoints

### 2.3 Rewrite process_turn()

**File**: `faultmaven/modules/agent/domain/services/investigation_service.py`

Replace the existing `process_turn(case_id, user_id, request: CaseQueryRequest)` with:

```python
async def process_turn(
    self, case_id: str, user_id: str, payload: TurnPayload
) -> TurnResponse:
    """Process a user turn through the two-step pipeline.

    Step 1: Preprocess any attachments through Tier 0+1 (before LLM).
    Step 2: LLM inference with query + evidence context.
    """
    case = await self.case_repository.get(case_id)
    self._verify_access(case, user_id)

    # ── STEP 1: PRE-LLM DATA INGESTION ──
    evidence_created = []
    if payload.has_attachments:
        for attachment in payload.attachments:
            evidence = await self._preprocess_attachment(case, attachment)
            evidence_created.append(evidence)
            case.evidence.append(evidence)

    # Determine query (explicit or implicit)
    query = payload.query
    if not payload.has_query and payload.has_attachments:
        query = generate_implicit_query(payload.attachments, evidence_created)

    # Save user message to conversation history
    self._save_user_message(case, query, payload)
    await self.case_repository.save(case)

    # ── STEP 2: LLM INFERENCE ──
    intent_type = payload.intent.type if payload.intent else IntentType.CONVERSATION
    if intent_type == IntentType.CONVERSATION:
        result = await self.engine.process_turn(
            case=case,
            user_message=query,
            attachments=[self._to_attachment_metadata(e) for e in evidence_created],
            intent_type=intent_type.value,
            intent_data=payload.intent.model_dump(exclude_unset=True) if payload.intent else {},
        )
    elif intent_type == IntentType.STATUS_TRANSITION:
        result = await self._handle_status_transition(case, query, payload.intent)
    elif intent_type == IntentType.CONFIRMATION:
        result = await self._handle_confirmation(case, query, payload.intent)
    elif intent_type == IntentType.HYPOTHESIS_ACTION:
        result = await self._handle_hypothesis_action(case, query, payload.intent)
    else:
        result = await self._handle_greeting(case, query)

    # Save agent response and build TurnResponse
    case.current_turn += 1
    agent_response = result.get("agent_response", "")
    self._save_agent_message(case, agent_response)
    await self.case_repository.save(case)

    return TurnResponse(
        agent_response=agent_response,
        turn_number=case.current_turn,
        milestones_completed=result.get("metadata", {}).get("milestones_completed", []),
        case_status=case.status,
        progress_made=result.get("metadata", {}).get("progress_made", False),
        is_stuck=result.get("metadata", {}).get("is_stuck", False),
        attachments_processed=[
            AttachmentResult(
                evidence_id=ev.evidence_id,
                filename=ev.original_filename or "",
                data_type=ev.data_type or "",
                file_size=ev.content_size_bytes or 0,
                processing_status="completed",
            )
            for ev in evidence_created
        ],
    )
```

### 2.4 Add _preprocess_attachment()

**File**: `faultmaven/modules/agent/domain/services/investigation_service.py`

```python
async def _preprocess_attachment(self, case: Case, attachment: Attachment) -> Evidence:
    """Preprocess a single attachment through Tier 0+1."""
    content = attachment.content.decode("utf-8", errors="replace")

    # Tier 0+1: Classify and extract
    preprocessing_result = await self.preprocessing_service.classify_and_extract(
        content=content,
        filename=attachment.filename,
        source_metadata=attachment.source_metadata,
    )

    # Create evidence record
    evidence = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        form=EvidenceForm.DOCUMENT,
        category=EvidenceCategory.CONTEXTUAL,
        source_type=EvidenceSourceType.DATA_UPLOAD,
        summary=preprocessing_result.summary,
        preprocessed_content=preprocessing_result.structural_index,
        data_type=preprocessing_result.data_type.value,
        content_hash=preprocessing_result.content_hash,
        content_size_bytes=len(content.encode("utf-8")),
        preprocessing_method=preprocessing_result.extraction_method,
        original_filename=attachment.filename,
    )

    # Store raw content
    content_ref = await self.storage_service.store(content, attachment.filename)
    evidence.content_ref = content_ref

    return evidence
```

### 2.5 Create implicit query generation

**File**: `faultmaven/core/investigation/turn_pipeline.py` (new file)

```python
def generate_implicit_query(attachments: List[Attachment], evidence: List[Evidence]) -> str:
    """Generate a system query when data is submitted without a question."""
    if len(evidence) == 1:
        ev = evidence[0]
        return (
            f"I've submitted {ev.original_filename or 'data'} "
            f"(classified as {ev.data_type}). "
            f"Analyze this data and tell me what you find."
        )
    filenames = ", ".join(ev.original_filename or f"file {i+1}" for i, ev in enumerate(evidence))
    return (
        f"I've submitted {len(evidence)} files: {filenames}. "
        f"Analyze this data and tell me what you find."
    )
```

---

## Phase 3: Milestone Engine Cleanup

**Goal**: Remove all submission_classification logic from the milestone engine. Evidence form is now determined before the LLM runs.

### 3.1 Delete _determine_evidence_form()

**File**: `faultmaven/core/investigation/milestone_engine.py` (lines 333-346)

Delete the entire function. It mapped `submission_classification.type` to `EvidenceForm` — this mapping no longer exists.

### 3.2 Remove preprocessing from milestone_engine

**File**: `faultmaven/core/investigation/milestone_engine.py`

In `_apply_inquiry_updates()` (lines 2221-2241) and `_apply_investigation_updates()` (lines 2479-2491):

- Delete the blocks that check `submission_classification.type in ("submitted_data", "mixed")` and call `preprocessing_service.classify_and_extract()`
- Preprocessing now happens in Step 1 of `process_turn()`, before the LLM ever runs

### 3.3 Simplify evidence creation from evidence_to_add

Evidence created from `evidence_to_add` (LLM output) in `_apply_inquiry_updates()` and `_apply_investigation_updates()`:

- Remove all `_determine_evidence_form(submission_classification)` calls
- Evidence from `evidence_to_add` always gets `form=EvidenceForm.SUBMITTED_DATA` (agent-derived findings)
- Evidence from attachments was already created in Step 1 with `form=EvidenceForm.DOCUMENT`

### 3.4 Remove submission_classification reads

Search the milestone engine for any remaining reads of `submission_classification` from the LLM response. Delete them all. The LLM response no longer contains this field.

### 3.5 Clean up data ingestion service

**File**: `faultmaven/modules/case/domain/services/case_data_ingestion_service.py`

Review `ingest_data()` (lines 177-223). This was called by the old `/data` endpoint. If it's no longer called from anywhere, delete it. If other code depends on it (e.g., background jobs), keep it but remove any references to the old endpoint flow.

### 3.6 Clean up docstrings

**Files**: `services/preprocessing/classifier.py`, `services/preprocessing/preprocessing_service.py`

Remove references to "submission_classification", "mixed", and "pasted text when LLM classifies" from all docstrings. The classifier and preprocessing service are now called directly from `_preprocess_attachment()`, not triggered by LLM classification.

---

## Phase 4: Context Sliding Window

**Goal**: Fix the "I don't have access to file content" bug. Include structural index in LLM context.

### 4.1 Implement Context Sliding Window

**File**: `faultmaven/core/investigation/prompts/context_builder.py` (lines 398-407)

Replace the evidence section in `build_investigation_context()` with the three-tier system (Section 6 of spec):

- **Tier A**: Last N data evidence items (`form == DOCUMENT` or `form == SUBMITTED_DATA`) → include `preprocessed_content` (structural index), capped at `EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM`. If truncated, append `[TRUNCATED: X more characters. Use search_file or read_file to query the full content.]`
- **Tier B**: Older data evidence → summary only
- **Tier C**: USER_TEXT evidence → summary only, always

XML format:
```xml
<evidence_collected>
  <evidence id="ev_a1b2c3" form="DOCUMENT" data_type="LOGS">
    <summary>Crime scene extraction: Error burst detected...</summary>
    <structural_index>
============================================================
CRIME SCENE EXTRACTION: Error burst detected...
============================================================
...
[TRUNCATED: 18,420 more characters. Use search_file or read_file to query the full content.]
    </structural_index>
  </evidence>
</evidence_collected>
```

### 4.2 Add config constants

**File**: `faultmaven/core/investigation/prompts/context_builder.py`

```python
EVIDENCE_CONTEXT_RECENT_COUNT = int(os.getenv("EVIDENCE_CONTEXT_RECENT_COUNT", "3"))
EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM = int(os.getenv("EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM", "4000"))
EVIDENCE_CONTEXT_MAX_TOTAL_CHARS = int(os.getenv("EVIDENCE_CONTEXT_MAX_TOTAL_CHARS", "16000"))
```

### 4.3 Token budget integration

Evidence context operates within the existing `TokenBudget` system. Dedicated allocation: ~4000 tokens. Worst case: 3 Tier A items x 4000 chars = 12,000 chars (~3000 tokens).

---

## Phase 5: Prompt Template Updates

**Goal**: Remove all classification instructions. Update evidence handling and anti-hallucination rules.

### 5.1 Remove classification instructions from all templates

**File**: `faultmaven/core/investigation/prompts/templates.py`

Delete from INQUIRY template (lines 75-96), INVESTIGATION_BASE (lines 209-220), and all other templates:
- "For EVERY user message, classify using submission_classification"
- All `submission_classification` YAML examples
- "When submitted_data or mixed, classify evidence by what the data contains"
- The entire "Submission Classification" section from each template

Replace with:

```
## Evidence from Attachments

Data submitted as attachments has already been preprocessed and appears in your
<evidence_collected> context as structural indexes (crime scene extractions,
statistical profiles, parsed configs). Focus on analyzing what's provided.

If you need more detail than the structural index shows, use these tools:
- search_file: grep/regex on raw file content (free, fast)
- read_file: read full file content (free)
- deep_analyze_file: LLM interpretation of specific sections (~$0.01)

When your analysis discovers NEW findings not in the structural index, create
evidence records via evidence_to_add with appropriate category and summary.
```

### 5.2 Remove submission_classification from output schema

**File**: `faultmaven/core/investigation/prompts/templates.py` (lines 511-606)

Delete `submission_classification` from the output schema documentation. The LLM output no longer includes this field.

### 5.3 Update anti-hallucination rules

**File**: `faultmaven/core/investigation/prompts/templates.py` (lines 465-508)

Replace the EVIDENCE GROUNDING section:

```
EVIDENCE GROUNDING (CRITICAL - Anti-Hallucination):

You must ONLY reference data from these sources:
1. Evidence context: Data in the <evidence_collected> section (summaries and structural indexes)
2. Tool results: Data retrieved via your tools (search_file, read_file, deep_analyze_file)
3. Conversation history: Past dialogue with the user
4. Knowledge base: Results from knowledge_base_search

ABSOLUTELY FORBIDDEN:
- NEVER claim to have accessed logs, metrics, services, or systems not provided
  via the sources above
- NEVER claim to have "looked at" or "checked" data you did not receive in
  evidence context or retrieve via a tool call
- NEVER infer specific system details not mentioned in any source above
- If you need data not available from any source: ASK the user to provide it
```

### 5.4 Update agent system prompt

**File**: `faultmaven/modules/agent/domain/services/agent_orchestration_service.py` (lines 94-108)

Update Data Access Strategy:

```
## Data Access Strategy

When a user asks about uploaded data, follow this escalation order:

1. **Check your context first** (free, instant): Recent evidence includes
   structural indexes (crime scene extractions, statistical profiles, parsed
   configs) directly in the <evidence_collected> section. Check these first.
   If a structural_index shows [TRUNCATED], the full content is available
   via search_file or read_file.

2. **search_file** (free, fast): If the structural index lacks detail or
   was truncated, use search_file to grep for specific keywords, patterns,
   or timestamps in the raw file.

3. **deep_analyze_file** (low cost, slower): If you need LLM interpretation
   of specific data sections — root cause analysis, correlation detection,
   or synthesizing findings across file sections.

4. **vectorize_file** (higher cost, rare): Only suggest when the user is
   repeatedly asking questions about a large file and point queries are
   insufficient. Always ask the user before vectorizing.

Never skip tiers. Always try the cheaper option first.
```

---

## Phase 6: Tests

**Goal**: Rewrite tests that reference old classification. Add new tests for the unified pipeline.

### 6.1 Rewrite existing tests

| Test File | Changes |
|-----------|---------|
| `tests/unit/core/investigation/test_evidence_form_determination.py` | Rewrite entirely — test payload-driven form assignment (`has_attachments` / `from_agent_tool`) |
| `tests/unit/core/investigation/test_evidence_classification.py` | Delete all `submission_classification` references. Test evidence creation from `evidence_to_add` with correct form |
| `tests/unit/core/investigation/test_milestone_engine_evidence_redesign.py` | Remove `submission_classification` from all mock LLM responses. Remove `mixed` and `external_data` type assertions |
| `tests/integration/core/test_investigation_lifecycle.py` | Remove `submission_classification` from all ~10 mock response objects. Update evidence form assertions |

### 6.2 New test: Unified endpoint

**File**: `tests/integration/api/test_unified_turns_endpoint.py` (~200 lines)

Test cases:
- Query-only turn → no preprocessing, LLM processes query
- File upload → Tier 0+1 preprocessing, implicit query, LLM with evidence context
- Pasted text → same as file upload (via `pasted_content` form field)
- Query + file → preprocessing + explicit query, both in LLM context
- Multiple files + query → all preprocessed, all in evidence context
- Intent routing (status_transition, confirmation) with attachments
- Missing both query and attachments → 400 error
- Old `/queries` and `/data` endpoints → 404 (deleted)

### 6.3 New test: Context Sliding Window

**File**: `tests/unit/core/investigation/test_context_sliding_window.py` (~200 lines)

Test cases:
- 0 evidence → "No formal evidence collected yet."
- 1 recent data evidence → Tier A with full structural_index
- 3 recent + 2 older data evidence → 3 Tier A + 2 Tier B (summary only)
- Structural index exceeds 4000 chars → truncation with `[TRUNCATED]` marker
- Total budget exceeded → oldest Tier B items dropped
- USER_TEXT evidence → always Tier C (summary only)
- Mixed forms → correct tier assignment

### 6.4 New test: Two-step pipeline

**File**: `tests/unit/agent/test_turn_pipeline.py` (~200 lines)

Test cases:
- `_preprocess_attachment()` creates Evidence with correct fields (form=DOCUMENT, structural_index populated)
- `generate_implicit_query()` for single file and multiple files
- Evidence form is always DOCUMENT for attachments
- Preprocessing failure → graceful fallback (TEXT extraction)
- Step 1 completes before Step 2 (verify ordering)

### 6.5 Verify import-linter contracts

Run `lint-imports` after all changes. `TurnPayload` in `core/investigation/schemas.py` is within the investigation boundary — no contract violations expected.

---

## Phase 7: Frontend Updates

**Goal**: Update copilot to use `/turns` endpoint. Add paste scratchpad. Delete old API functions.

### 7.1 Replace API client functions

**File**: `faultmaven-copilot/src/lib/api/services/case-service.ts`

Delete `submitQueryToCase()` (lines 410-571) and `uploadDataToCase()` (lines 573-613). Replace with:

```typescript
export async function submitTurn(
  caseId: string,
  request: TurnRequest
): Promise<TurnResponse> {
  const form = new FormData();
  if (request.query) form.append('query', request.query);
  if (request.pastedContent) form.append('pasted_content', request.pastedContent);
  if (request.intentType) form.append('intent_type', request.intentType);
  if (request.intentData) form.append('intent_data', JSON.stringify(request.intentData));
  for (const file of request.files || []) {
    form.append('files', file);
  }

  const response = await authenticatedFetchWithRetry(
    `${await getApiUrl()}/api/v1/cases/${caseId}/turns`,
    { method: 'POST', body: form, credentials: 'include' }
  );
  // Handle 200/201/202 responses...
}
```

### 7.2 Replace TypeScript types

**File**: `faultmaven-copilot/src/lib/api/types.ts`

Delete `CaseQueryRequest`, `QueryRequest`, `UploadedData` types. Add:

```typescript
interface TurnRequest {
  query?: string;
  files?: File[];
  pastedContent?: string;
  intentType?: IntentType;
  intentData?: Record<string, unknown>;
}

interface TurnResponse {
  agent_response: string;
  turn_number: number;
  milestones_completed: string[];
  case_status: CaseStatus;
  progress_made: boolean;
  is_stuck: boolean;
  attachments_processed: AttachmentResult[];
}

interface AttachmentResult {
  evidence_id: string;
  filename: string;
  data_type: string;
  file_size: number;
  processing_status: string;
}
```

### 7.3 Add Paste Data scratchpad

**File**: `faultmaven-copilot/src/shared/ui/components/PasteDataScratchpad.tsx` (new, ~100 lines)

Textarea component for pasting data. Submits via `submitTurn()` with `pastedContent` field. Optional query input alongside.

### 7.4 Update UnifiedInputBar

**File**: `faultmaven-copilot/src/shared/ui/components/UnifiedInputBar.tsx`

- Add "Paste Data" button/toggle to open scratchpad
- When data mode detected (≥100 lines), offer to submit as data attachment
- Support query + attachment in single submission
- All submissions go through `submitTurn()`

### 7.5 Rewrite hooks

**File**: `faultmaven-copilot/src/shared/ui/hooks/useMessageSubmission.ts`

Replace `submitQueryToCase()` calls with `submitTurn()`. Handle `TurnResponse.attachments_processed` for evidence tracking.

**File**: `faultmaven-copilot/src/shared/ui/hooks/useDataUpload.ts`

Replace `uploadDataToCase()` calls with `submitTurn()`. Support optional query with uploads.

### 7.6 Update tests

**File**: `faultmaven-copilot/src/test/api/services/case-service.test.ts`

Rewrite to test `submitTurn()` instead of `submitQueryToCase()` and `uploadDataToCase()`.

---

## Config Changes

**Files**: `.env.example`, `faultmaven/infrastructure/config/settings.py`

### Rename (delete old names)
- `TIER2_BACKEND` → `DEEP_ANALYSIS_BACKEND`
- `TIER2_URL` → `DEEP_ANALYSIS_URL`
- `TIER2_API_KEY` → `DEEP_ANALYSIS_API_KEY`
- `TIER2_TIMEOUT_SECONDS` → `DEEP_ANALYSIS_TIMEOUT_SECONDS`

### Add new
- `EVIDENCE_CONTEXT_RECENT_COUNT=3`
- `EVIDENCE_CONTEXT_MAX_CHARS_PER_ITEM=4000`
- `EVIDENCE_CONTEXT_MAX_TOTAL_CHARS=16000`

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Structural index too large for some providers' context windows | Medium | Token budget system + truncation with `[TRUNCATED]` marker. Configurable per-item cap. |
| Preprocessing timeout delays turn response | Low | Existing 2s timeout mechanism unchanged. Fallback to TEXT extraction on timeout. |
| LLM still tries to output `submission_classification` despite prompt removal | Low | Field deleted from schema — Pydantic ignores unknown fields. No harm. |
| Frontend and backend must deploy together | Medium | Coordinate deployment. Frontend calls `/turns` which must exist. |

---

## Validation Checklist

Run after each phase:
- [ ] `pytest` passes (all markers)
- [ ] `lint-imports` passes (13 contracts)
- [ ] `ruff check` + `black --check` + `isort --check` pass

Run after all phases:
- [ ] Manual test: upload file → agent sees structural index → responds with analysis
- [ ] Manual test: paste data via scratchpad → same flow as file upload
- [ ] Manual test: query + file in single turn → both processed correctly
- [ ] Manual test: query-only → no preprocessing, normal conversation
- [ ] Verify: no remaining references to `submission_classification`, `mixed`, `/queries`, or `/data` in codebase (excluding test assertions about 404s)

---

## Files Summary

### Deleted Files / Endpoints

| What | Where |
|------|-------|
| `submit_case_query()` endpoint | `modules/case/api/routes.py` lines 2015-2216 |
| `upload_case_data()` endpoint | `modules/case/api/routes.py` lines 2408-2550 |
| `SubmissionClassification` class | `core/investigation/schemas.py` lines 98-141 |
| `_determine_evidence_form()` function | `core/investigation/milestone_engine.py` lines 333-346 |
| `CaseQueryRequest` model | `models/api_models.py` lines 405-429 |
| `CaseQueryResponse` model | `models/api_models.py` lines 431-443 |
| `DataUploadResponse` model | `models/api.py` lines 436-482 |
| `submitQueryToCase()` function | `faultmaven-copilot/src/lib/api/services/case-service.ts` lines 410-571 |
| `uploadDataToCase()` function | `faultmaven-copilot/src/lib/api/services/case-service.ts` lines 573-613 |

### New Files

| File | Phase | Purpose |
|------|-------|---------|
| `core/investigation/turn_pipeline.py` | 2 | `generate_implicit_query()` and payload utilities |
| `tests/integration/api/test_unified_turns_endpoint.py` | 6 | Endpoint integration tests |
| `tests/unit/core/investigation/test_context_sliding_window.py` | 6 | Context window unit tests |
| `tests/unit/agent/test_turn_pipeline.py` | 6 | Pipeline unit tests |
| `faultmaven-copilot/.../PasteDataScratchpad.tsx` | 7 | Paste data UI component |

### Modified Files

| File | Phase | Change |
|------|-------|--------|
| `core/investigation/schemas.py` | 1 | Add TurnPayload/Attachment. Delete SubmissionClassification. Remove field from 5 response classes |
| `modules/case/domain/models.py` | 1 | Update EvidenceForm docstring |
| `models/api_models.py` | 1 | Add TurnResponse/AttachmentResult. Delete CaseQueryRequest/CaseQueryResponse |
| `models/api.py` | 1 | Delete DataUploadResponse |
| `modules/case/api/routes.py` | 2 | Add `/turns` endpoint. Delete `/queries` and `/data` endpoints |
| `modules/agent/domain/services/investigation_service.py` | 2 | Rewrite `process_turn()`. Add `_preprocess_attachment()` |
| `core/investigation/milestone_engine.py` | 3 | Delete `_determine_evidence_form()`. Remove submission_classification reads. Remove preprocessing calls. Simplify evidence creation |
| `modules/case/domain/services/case_data_ingestion_service.py` | 3 | Delete `ingest_data()` if unused. Otherwise remove old endpoint references |
| `services/preprocessing/classifier.py` | 3 | Remove classification docstring references |
| `services/preprocessing/preprocessing_service.py` | 3 | Remove classification docstring references |
| `core/investigation/prompts/context_builder.py` | 4 | Implement Context Sliding Window. Add config constants |
| `core/investigation/prompts/templates.py` | 5 | Delete classification instructions. Update anti-hallucination. Update output schema docs |
| `modules/agent/domain/services/agent_orchestration_service.py` | 5 | Update Data Access Strategy prompt |
| `infrastructure/config/settings.py` | 5 | Rename TIER2_* → DEEP_ANALYSIS_*. Add EVIDENCE_CONTEXT_* |
| `tests/unit/.../test_evidence_form_determination.py` | 6 | Rewrite for payload-driven form |
| `tests/unit/.../test_evidence_classification.py` | 6 | Delete submission_classification refs |
| `tests/unit/.../test_milestone_engine_evidence_redesign.py` | 6 | Remove submission_classification from mocks |
| `tests/integration/.../test_investigation_lifecycle.py` | 6 | Remove submission_classification (~10 instances) |
| `faultmaven-copilot/.../case-service.ts` | 7 | Delete old functions. Add `submitTurn()` |
| `faultmaven-copilot/.../types.ts` | 7 | Delete old types. Add TurnRequest/TurnResponse |
| `faultmaven-copilot/.../UnifiedInputBar.tsx` | 7 | Add paste data toggle. Route all submissions through `submitTurn()` |
| `faultmaven-copilot/.../useMessageSubmission.ts` | 7 | Use `submitTurn()` |
| `faultmaven-copilot/.../useDataUpload.ts` | 7 | Use `submitTurn()` |
| `faultmaven-copilot/.../case-service.test.ts` | 7 | Rewrite for `submitTurn()` |

---

**Document Version**: 2.0
**Last Updated**: 2026-02-21
