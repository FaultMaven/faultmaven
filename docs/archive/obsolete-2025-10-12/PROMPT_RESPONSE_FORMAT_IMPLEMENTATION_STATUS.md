# Prompt Response Format Implementation Status

**Document Type:** Implementation Status Report
**Version:** 1.0
**Last Updated:** 2025-10-11
**Status:** Prompts Updated, Parser Implementation Pending

---

## Executive Summary

Response format specifications have been **successfully integrated** into both Consultant Mode and Lead Investigator Mode prompts. However, the **parsing layer** to extract structured responses is **NOT YET IMPLEMENTED**.

**Current Status:**
- ✅ Prompt files updated with complete response format specifications
- ✅ Phase-specific schemas defined in prompts
- ✅ Examples provided for all 7 phases
- ❌ **Response parsing NOT implemented** - phase handlers still return raw strings
- ❌ **Function calling NOT configured** - no JSON schema enforcement
- ❌ **Three-tier fallback NOT implemented** - no parsing/validation logic

---

## What Was Completed

### 1. Consultant Mode Prompts Updated

**File:** `faultmaven/prompts/investigation/consultant_mode.py`

**Added Section:** "# Response Format" (lines 106-219)

**Specifications:**
```python
class ConsultantResponse(BaseModel):
    """Consultant Mode response structure"""

    # ALWAYS PRESENT
    answer: str = Field(..., description="Natural language response")

    # OPTIONAL: GUIDANCE FIELDS
    clarifying_questions: List[str] = Field(default_factory=list, max_length=3)
    suggested_actions: List[SuggestedAction] = Field(default_factory=list, max_length=6)
    suggested_commands: List[CommandSuggestion] = Field(default_factory=list, max_length=5)
    command_validation: Optional[CommandValidation] = None

    # OPTIONAL: PROBLEM DETECTION
    problem_detected: bool = Field(default=False)
    problem_summary: Optional[str] = None
    severity: Optional[Literal["low", "medium", "high", "critical"]] = None
```

**Key Features:**
- Clear schema with all fields documented
- Guidelines for when to use each field
- 2 complete examples (simple question, problem detected)
- Reminder to "ALWAYS return structured JSON"

### 2. Lead Investigator Mode Prompts Updated

**File:** `faultmaven/prompts/investigation/lead_investigator.py`

**Added Section:** "# Response Format" (lines 142-365)

**Specifications:**
```python
class LeadInvestigatorResponse(BaseModel):
    """Lead Investigator base response structure"""

    # BASE SCHEMA (ALL PHASES)
    answer: str
    clarifying_questions: List[str]
    suggested_actions: List[SuggestedAction]
    suggested_commands: List[CommandSuggestion]
    evidence_request: Optional[EvidenceRequest]
    phase_complete: bool = False
    should_advance: bool = False
    advancement_rationale: Optional[str] = None

    # PHASE-SPECIFIC FIELDS (conditionally included)
    # Phase 1: scope_assessment
    # Phase 2: timeline_update
    # Phase 3: new_hypotheses
    # Phase 4: hypothesis_tested, test_result
    # Phase 5: solution_proposal
    # Phase 6: case_summary
```

**Key Features:**
- Base schema for all phases
- 6 phase-specific field sets defined
- Guidelines for evidence requests, commands, actions
- Phase completion and advancement logic
- 3 complete examples (Phase 1, Phase 4, phase completion)

### 3. Schema Coverage

All 7 phases have response format specifications:

| Phase | Response Schema | Status |
|-------|----------------|--------|
| Phase 0 (Intake) | ConsultantResponse | ✅ Complete |
| Phase 1 (Blast Radius) | LeadInvestigatorResponse + scope_assessment | ✅ Complete |
| Phase 2 (Timeline) | LeadInvestigatorResponse + timeline_update | ✅ Complete |
| Phase 3 (Hypothesis) | LeadInvestigatorResponse + new_hypotheses | ✅ Complete |
| Phase 4 (Validation) | LeadInvestigatorResponse + hypothesis_tested, test_result | ✅ Complete |
| Phase 5 (Solution) | LeadInvestigatorResponse + solution_proposal | ✅ Complete |
| Phase 6 (Document) | LeadInvestigatorResponse + case_summary | ✅ Complete |

---

## What Is NOT Implemented

### 1. Response Parsing Layer

**Current Implementation:**
```python
# faultmaven/services/agentic/phase_handlers/base.py:243-280
async def generate_llm_response(
    self,
    system_prompt: str,
    user_query: str,
    context: Dict[str, Any] = None,
    max_tokens: int = 500,
) -> str:  # ❌ Returns raw string
    """Generate LLM response for phase"""
    # ...
    response = await self.llm_provider.generate(
        prompt=full_prompt,
        max_tokens=max_tokens,
        temperature=0.7,
    )

    return response.strip()  # ❌ Raw string, no parsing
```

**What's Missing:**
1. No JSON parsing of LLM response
2. No validation against Pydantic schemas
3. No fallback handling if parsing fails
4. Response data not accessible as structured objects

### 2. Function Calling Configuration

**Current Implementation:**
- LLM provider calls do NOT specify JSON schema
- No function calling parameters passed
- LLM can return any format (typically follows prompt, but not enforced)

**What's Missing:**
```python
# Should be:
response = await self.llm_provider.generate(
    prompt=full_prompt,
    max_tokens=max_tokens,
    temperature=0.7,
    response_format={"type": "json_object"},  # ❌ NOT configured
    functions=[CONSULTANT_RESPONSE_SCHEMA],    # ❌ NOT configured
)
```

### 3. Three-Tier Fallback Strategy

**From Design Spec:**
1. **Function Calling** (99% reliable) - JSON schema enforcement
2. **JSON Parsing** (90% reliable) - Parse JSON from markdown
3. **Heuristic Extraction** (70% reliable) - Extract from natural language

**Current Implementation:**
- ❌ Tier 1 not configured
- ❌ Tier 2 not implemented
- ❌ Tier 3 not implemented
- Overall reliability: **0%** (no structured output extraction)

### 4. Pydantic Response Models

**What's Missing:**
- `OODAResponse` base model (not created)
- `ConsultantResponse` model (not created)
- `LeadInvestigatorResponse` model (not created)
- Phase-specific field models (not created)

**Location:** Should be in `faultmaven/models/responses.py` (doesn't exist)

### 5. OpenAPI Schema Updates

**From OPENAPI_SPEC_UPDATE_REQUIREMENTS.md:**
- Version bump needed: 3.1.0 → 3.2.0
- Un-deprecate `suggested_actions`
- Add 11 new fields to AgentResponse
- Define 7 new component schemas
- Complete ViewState definition

**Status:** ❌ NOT updated

---

## Implementation Roadmap

### Week 1: Response Models and Parsing

**Priority: CRITICAL**

1. **Create Pydantic Response Models** (1 day)
   - Location: `faultmaven/models/responses.py`
   - Models:
     - `OODAResponse` (base)
     - `ConsultantResponse` (Phase 0)
     - `LeadInvestigatorResponse` (Phases 1-6)
     - Field models: `SuggestedAction`, `CommandSuggestion`, `EvidenceRequest`, etc.

2. **Implement Three-Tier Fallback Parser** (2 days)
   - Location: `faultmaven/core/response_parser.py`
   - Functions:
     - `parse_ooda_response(raw_response: str, expected_schema: Type[BaseModel]) -> OODAResponse`
     - `_tier1_function_calling(response: dict) -> Optional[OODAResponse]`
     - `_tier2_json_parsing(response: str) -> Optional[OODAResponse]`
     - `_tier3_heuristic_extraction(response: str) -> Optional[OODAResponse]`

3. **Update BasePhaseHandler** (1 day)
   - Change `generate_llm_response` signature:
     ```python
     async def generate_llm_response(
         self,
         system_prompt: str,
         user_query: str,
         context: Dict[str, Any] = None,
         max_tokens: int = 500,
     ) -> OODAResponse:  # ✅ Return structured response
     ```
   - Integrate response parser
   - Add error handling and fallback logic

4. **Test Response Parsing** (1 day)
   - Unit tests for all 7 phase schemas
   - Integration tests with mock LLM responses
   - Fallback strategy tests (Tier 1, 2, 3)

### Week 2: LLM Provider Function Calling

**Priority: HIGH**

1. **Add Function Calling Support to LLM Provider** (2 days)
   - Update `faultmaven/infrastructure/llm/base.py`
   - Add `response_format` parameter
   - Add `functions` parameter for OpenAI-style function calling
   - Test with OpenAI and Anthropic providers

2. **Generate JSON Schemas from Pydantic Models** (1 day)
   - Create utility: `faultmaven/utils/schema_converter.py`
   - Function: `pydantic_to_json_schema(model: Type[BaseModel]) -> dict`
   - Auto-generate function calling schemas

3. **Update All Phase Handlers** (1 day)
   - Pass expected response schema to `generate_llm_response`
   - Verify structured responses work end-to-end
   - Update tests

### Week 3: OpenAPI Schema Updates

**Priority: HIGH**

1. **Update openapi.locked.yaml** (1 day)
   - Version bump: 3.1.0 → 3.2.0
   - Un-deprecate `suggested_actions`
   - Add 11 new AgentResponse fields
   - Define 7 component schemas

2. **Update API Layer** (2 days)
   - Location: `faultmaven/api/v1/routes/agent.py`
   - Convert `OODAResponse` to `AgentResponse` (API schema)
   - Ensure backward compatibility (all new fields optional)

3. **API Integration Tests** (1 day)
   - Test full request → OODA → AgentResponse flow
   - Verify schema compliance
   - Test all 7 phases return correct schema

### Week 4: Frontend Updates

**Priority: MEDIUM**

1. **Update TypeScript Interfaces** (1 day)
   - Location: `faultmaven-copilot/src/types/api.ts`
   - Add new AgentResponse fields
   - Add component schemas

2. **Update UI Components** (2 days)
   - Render `suggested_actions` as clickable buttons
   - Display `suggested_commands` with safety indicators
   - Show `clarifying_questions` as expandable section
   - Handle phase-specific fields (hypotheses, timeline, etc.)

3. **Frontend Integration Tests** (1 day)
   - Test UI rendering for all response types
   - Test user interactions with suggested actions/commands

### Week 5: End-to-End Testing and Documentation

**Priority: MEDIUM**

1. **End-to-End Testing** (2 days)
   - Complete investigation flows (all 7 phases)
   - Test with real LLM providers
   - Validate response format consistency

2. **Update Documentation** (1 day)
   - Update `RESPONSE_FORMAT_INTEGRATION_SPEC.md` to "Implemented"
   - Update `prompt-engineering-architecture.md` with parsing details
   - Create developer guide for adding new response fields

3. **Performance Testing** (1 day)
   - Measure token usage with structured outputs
   - Verify 81% token reduction from prompt optimization
   - Benchmark response parsing speed

---

## Risk Assessment

### High Risk

1. **LLM Provider Compatibility**
   - **Risk:** Anthropic's Claude may not support function calling identically to OpenAI
   - **Mitigation:** Test both providers, rely on Tier 2 (JSON parsing) as fallback
   - **Impact:** May need provider-specific function calling implementations

2. **Breaking Changes**
   - **Risk:** Changing `generate_llm_response` signature breaks existing phase handlers
   - **Mitigation:** Update all 7 phase handlers simultaneously
   - **Impact:** 1-2 day effort to update all handlers

### Medium Risk

1. **Parsing Reliability**
   - **Risk:** Three-tier fallback may still fail for malformed responses
   - **Mitigation:** Extensive testing, graceful degradation to answer-only response
   - **Impact:** User sees plain text instead of structured UI elements

2. **Frontend Compatibility**
   - **Risk:** Browser extension may not handle new response fields
   - **Mitigation:** Make all new fields optional, backward compatible
   - **Impact:** Frontend works but doesn't show enhanced UI until updated

### Low Risk

1. **Performance Impact**
   - **Risk:** JSON parsing adds latency
   - **Mitigation:** Parsing is <10ms, negligible compared to LLM call (2-5s)
   - **Impact:** Minimal user-visible impact

---

## Success Metrics

### Technical Metrics

- **Response Parsing Success Rate:** ≥99% (via three-tier fallback)
- **Schema Validation Rate:** 100% of parsed responses validate against Pydantic models
- **API Schema Compliance:** 100% of AgentResponse objects match OpenAPI 3.2.0 spec
- **Test Coverage:** ≥80% for response parsing and validation code

### User Experience Metrics

- **Structured Actions Delivered:** ≥90% of responses include `suggested_actions` or `suggested_commands`
- **UI Element Rendering:** 100% of structured fields render correctly in frontend
- **User Interaction Rate:** ≥40% of users click suggested actions/commands
- **Error Recovery:** ≥95% of parsing failures gracefully degrade to plain text

---

## Current State vs. Design Intent

### Design Intent (from RESPONSE_FORMAT_INTEGRATION_SPEC.md)

> "The LLM responds with the right data structure and format will make the classification unnecessary."

**What This Means:**
- No rule-based query classification layer needed
- LLM directly returns structured JSON matching phase requirements
- Frontend can immediately render structured elements (buttons, commands, questions)
- Investigation state updates extracted from response structure

### Current State

**Prompts:** ✅ Tell LLM what structure to return
**LLM Generation:** ⚠️ LLM probably returns JSON (following prompt), but NOT enforced
**Parsing:** ❌ System ignores structure, treats as plain string
**State Updates:** ❌ Investigation state not updated from response fields
**Frontend:** ❌ Receives plain text, can't render structured UI elements

**Gap:** The system prompts the LLM correctly but **throws away** the structured response.

---

## Recommendations

### Immediate Action Items

1. **Create Response Models** - Pydantic models must exist before parsing can begin
2. **Implement Tier 2 Parser** - JSON parsing is simplest and works with all providers
3. **Update BasePhaseHandler** - Single point of change affects all phase handlers
4. **Add Integration Test** - Verify structured responses work end-to-end

### Deferred Items

1. **Tier 1 Function Calling** - Can be added after Tier 2 works (optimization)
2. **Tier 3 Heuristics** - Last resort fallback, implement if Tier 2 reliability <90%
3. **Frontend Updates** - Can proceed in parallel but not blocking backend work

---

## Related Documents

- **Design Specification:** [RESPONSE_FORMAT_INTEGRATION_SPEC.md](./RESPONSE_FORMAT_INTEGRATION_SPEC.md)
- **OpenAPI Updates:** [OPENAPI_SPEC_UPDATE_REQUIREMENTS.md](./OPENAPI_SPEC_UPDATE_REQUIREMENTS.md)
- **Gap Analysis:** [RESPONSE_FORMAT_SPECIFICATION_GAP_ANALYSIS.md](./RESPONSE_FORMAT_SPECIFICATION_GAP_ANALYSIS.md)
- **Prompt Architecture:** [prompt-engineering-architecture.md](./prompt-engineering-architecture.md)
- **Frontend Requirements:** [OODA_FRONTEND_REQUIREMENTS.md](./OODA_FRONTEND_REQUIREMENTS.md)

---

## Document Metadata

**Version History:**
- v1.0 (2025-10-11): Initial status report after prompt updates

**Audience:** Development team, product manager, technical lead

**Prerequisites:** Understanding of OODA Investigation Framework v3.2.0

**Maintained By:** Architecture Team
**Review Cycle:** Weekly during implementation
**Next Review:** 2025-10-18
