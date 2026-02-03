# Prompt Engineering Evaluation Report

**Evaluation Date:** 2026-02-03
**Evaluator:** Claude Code
**Reference Document:** `docs/architecture/investigation-engine/prompt-engineering-guide.md`
**Status:** Comprehensive Gap Analysis

---

## Executive Summary

This evaluation compares the documented prompt engineering guidelines against the actual implementation. The analysis reveals several areas where the implementation diverges from or has not yet implemented the documented specifications.

**Overall Assessment:** The implementation covers ~60-65% of the documented guidelines. Key advanced features from sections 11-17 are largely unimplemented.

### Implementation Files Evaluated:
- `faultmaven/core/investigation/prompts/templates.py`
- `faultmaven/core/investigation/prompts/context_builder.py`
- `faultmaven/core/investigation/schemas.py`
- `faultmaven/prompts/system_prompts.py`
- `faultmaven/prompts/few_shot_examples.py`

---

## Gap Analysis Summary

| Section | Feature | Status | Priority |
|---------|---------|--------|----------|
| 3 | INQUIRY Template - Fast Track | ⚠️ Partial | Medium |
| 4.6 | Degraded Mode Instructions | ⚠️ Partial | High |
| 11 | Token Budget Management | ⚠️ Partial | High |
| 12 | XML-Based Instruction Structuring | ❌ Not Implemented | Medium |
| 13 | Reasoning-First Response Schema | ❌ Not Implemented | **Critical** |
| 14 | Negative Evidence & Blocker Detection | ❌ Not Implemented | High |
| 15 | Error Handling & Recovery | ⚠️ Partial | Medium |
| 16 | Prompt Security | ❌ Not Implemented | High |
| 17.5 | LLM vs System Responsibilities | ✅ Implemented | N/A |

---

## Detailed Findings

### 1. Reasoning-First Response Schema (Section 13) - **CRITICAL GAP**

**Guide Specification:**
- `InternalReasoning` field required BEFORE `state_updates`
- Fields: `evidence_analyzed`, `conclusions`, `milestone_justifications`, `uncertainties`
- System validation to ensure milestones have justification
- Prevents "hallucinated completion" where LLM ticks checkboxes without evidence

**Current Implementation:**
- `schemas.py` defines stage-specific schemas but **lacks `internal_reasoning` field**
- No validation that milestones are justified by evidence
- LLM can set `root_cause_identified = True` without providing reasoning chain

**Impact:** Without reasoning-first, the system cannot audit or validate milestone completions, leading to potential false positives in investigation progress.

**Recommendation:**
```python
class InternalReasoning(BaseModel):
    evidence_analyzed: List[str]
    conclusions: List[ReasoningConclusion]
    milestone_justifications: Dict[str, str]
    uncertainties: List[str]

# Add to all InvestigationResponse_* schemas:
internal_reasoning: InternalReasoning
```

---

### 2. Negative Evidence & Blocker Detection (Section 14) - **HIGH PRIORITY**

**Guide Specification:**
- `missing_critical_data` flag for immediate degraded mode entry
- `BlockerType` enum: DATA_EMPTY, DATA_CORRUPTED, DATA_INCOMPLETE, DATA_INACCESSIBLE, DATA_IRRELEVANT
- `EvidenceQualityIssue` class for quality assessment
- Proactive detection instead of waiting 3 turns

**Current Implementation:**
- **Not implemented** - No `missing_critical_data` field in schemas
- System relies on passive 3-turn detection for degraded mode
- No evidence quality assessment mechanism

**Impact:** Investigations waste 3+ turns when LLM immediately recognizes unusable data.

**Recommendation:**
Add to state update schemas:
```python
class MissingCriticalData(BaseModel):
    blocker_type: BlockerType
    description: str
    what_was_expected: str
    what_was_found: str
    impact: str
    suggested_alternatives: List[str]
    triggers_degraded_mode: bool = True

# Add to InvestigationStateUpdate:
missing_critical_data: Optional[MissingCriticalData] = None
evidence_quality_issues: List[EvidenceQualityIssue] = Field(default_factory=list)
```

---

### 3. Prompt Security (Section 16) - **HIGH PRIORITY**

**Guide Specification:**
- Input sanitization for prompt injection patterns
- State manipulation detection
- Output validation (confidence bounds, milestone regression)
- Security reinforcement in system prompt

**Current Implementation:**
- **Not implemented** - No sanitization in `context_builder.py`
- No security constraints in prompt templates
- No output validation for security issues

**Impact:** System vulnerable to prompt injection and state manipulation attacks.

**Recommendation:**
1. Add `sanitize_user_input()` before including in prompt
2. Add `SECURITY_REINFORCEMENT` section to all templates
3. Implement `validate_llm_output_security()` post-processing

---

### 4. Token Budget Management (Section 11) - **PARTIAL IMPLEMENTATION**

**Guide Specification:**
- Target distribution: 5% identity, 15% state, 25% history, 30% instructions, 15% schema, 10% buffer
- Provider-specific limits table
- Dynamic context loading based on investigation stage
- State Summary + Last Turn instead of raw history

**Current Implementation (`context_builder.py`):**
- ✅ Has `TokenBudget` class with character-based estimation
- ✅ Default 8000 token limit
- ⚠️ Loads all context sections regardless of stage (no dynamic loading)
- ⚠️ Uses full message history (last 20 messages) instead of summarized format

**Gap Analysis:**
| Feature | Documented | Implemented |
|---------|------------|-------------|
| Token estimation | ✅ | ✅ (1 token ≈ 4 chars) |
| Budget allocation | ✅ Detailed % | ❌ Not enforced |
| Stage-specific loading | ✅ | ❌ All sections loaded |
| History summarization | ✅ State + Last Turn | ❌ Full messages |
| Provider limits | ✅ Table | ❌ Not used |

**Recommendation:**
Implement `build_context()` from Section 11.4:
```python
def build_context(case: Case, stage: InvestigationStage) -> str:
    if stage == InvestigationStage.SYMPTOM_VERIFICATION:
        # Skip hypothesis_history, solution_history
        pass
    elif stage == InvestigationStage.HYPOTHESIS_VALIDATION:
        # Focus on active hypotheses and links
        pass
    # etc.
```

---

### 5. XML-Based Instruction Structuring (Section 12.1) - **NOT IMPLEMENTED**

**Guide Specification:**
- Use XML-style tags for precise boundary parsing
- Avoid decorative markdown headers (wastes tokens)
- Example: `<task_guidance stage="{computed_stage}">...</task_guidance>`

**Current Implementation (`templates.py`):**
```python
# Current format (lines 52-79):
INVESTIGATION_BASE = """You are FaultMaven, the Lead Investigator...
STATUS: INVESTIGATING
...
CONVERSATION HISTORY:
{conversation_history}
...
YOUR TASK:
{adaptive_instructions}
"""
```

**Issue:** Uses plain text/markdown formatting instead of XML tags.

**Recommendation:**
Refactor templates to use XML structure:
```python
INVESTIGATION_BASE = """
<system_identity>
You are FaultMaven, the Lead Investigator for this case.
</system_identity>

<case_status>
status: INVESTIGATING
stage: {stage}
</case_status>

<state_summary>
{compressed_state_summary}
</state_summary>

<task_guidance stage="{computed_stage}">
{stage_specific_instructions}
</task_guidance>
"""
```

---

### 6. Degraded Mode Instructions (Section 4.6) - **PARTIAL IMPLEMENTATION**

**Guide Specification:**
- Detailed degraded mode template with:
  - Mode type and reason display
  - Behavior changes (transparent communication, lower confidence, fallback options)
  - Example response format
  - Escalation offers every 2 turns

**Current Implementation (`templates.py`):**
- Line 253: Checks `case.path_selection.path == "mitigation_first"` for MITIGATION_FIRST note
- **No degraded mode instructions** in the template
- `context_builder.py` references `case.degraded_mode` but doesn't build degraded instructions

**Recommendation:**
Add `get_degraded_mode_instructions()` function as specified in Section 4.6:
```python
def get_degraded_mode_instructions(case: Case) -> str:
    if not case.degraded_mode:
        return ""
    return f"""
<degraded_mode type="{case.degraded_mode.mode_type}">
**BEHAVIOR CHANGES:**
1. Transparent Communication - Prefix responses with limitations
2. Lower Confidence Assessment - Based only on available evidence
3. Offer Fallback Options - Every 2 turns offer escalation
4. Continue Investigation - Don't give up
</degraded_mode>
"""
```

---

### 7. Error Handling & Recovery (Section 15) - **PARTIAL IMPLEMENTATION**

**Guide Specification:**
- JSON repair strategies (extract from markdown, fix trailing commas, etc.)
- Retry policy with execution profiles (interactive vs background)
- `TurnOutcome.SYSTEM_ERROR` for graceful degradation
- Fallback response generation

**Current Implementation:**
- ✅ `templates.py` has fallback templates (`FALLBACK_INQUIRY_TEMPLATE`, etc.)
- ⚠️ No JSON repair strategy in prompt-related code
- ⚠️ No retry policy with profiles visible in these files
- ❌ `TurnOutcome` enum in `contracts.py` doesn't include `SYSTEM_ERROR`

**Recommendation:**
1. Implement `repair_json_response()` from Section 15.2
2. Add `SYSTEM_ERROR = "system_error"` to `TurnOutcome` enum
3. Implement retry policy with interactive/background profiles

---

### 8. INQUIRY Template - Fast Track Resolution (Section 3) - **PARTIAL IMPLEMENTATION**

**Guide Specification:**
- Knowledge pre-check before asking questions
- `PreliminaryUrgency` for semantic urgency assessment
- `KnowledgeMatch` and `KnowledgeResolution` for fast-track
- Fast-Track path: INQUIRY → RESOLVED (skip INVESTIGATING)

**Current Implementation:**
- ✅ `schemas.py` defines `PreliminaryUrgency`, `KnowledgeMatch`, `KnowledgeResolution`
- ⚠️ `INQUIRY_TEMPLATE` in `templates.py` mentions KB results but instructions are brief
- ❌ Detailed workflow from Section 3.3 not fully reflected in template

**Recommendation:**
Enhance INQUIRY_TEMPLATE with detailed fast-track instructions:
```python
INQUIRY_TEMPLATE = """...
<knowledge_workflow>
**Step 0: KNOWLEDGE PRE-CHECK** (Before Asking Questions)
- Search KB for similar past cases
- IF HIGH-CONFIDENCE MATCH (>70%): Set knowledge_match
- IF user confirms KB solution worked: Set knowledge_resolution (triggers Fast-Track)
- IF NO/LOW-CONFIDENCE MATCH: Proceed silently
</knowledge_workflow>
...
"""
```

---

### 9. Conversation History Strategy (Section 11.5) - **NOT FOLLOWING GUIDE**

**Guide Specification:**
- Replace raw history with State Summary + Last Turn (~200 tokens)
- Avoid raw history that consumes 500+ tokens per turn

**Current Implementation (`context_builder.py:107-144`):**
```python
# Current: Full messages (last 20 messages)
recent_messages = case.messages[-20:]
for msg in recent_messages:
    recent_history += f"{role}: {content}\n"
```

**Issue:** Uses full message content instead of summarized format.

**Recommended Format (from Section 11.5):**
```python
"""
<state_summary>
Investigation: API timeout errors (10% failure rate)
Stage: HYPOTHESIS_VALIDATION
Verified: symptom, timeline (14:23 UTC), scope (all /api/v1/* endpoints)
Active Hypothesis: Connection pool exhaustion (65% confidence)
Evidence: 3 artifacts analyzed, 2 support hypothesis
Turns: 7 total, 2 since last milestone
</state_summary>

<previous_turn>
User provided: Connection pool metrics showing 95/100 connections
Agent requested: Application connection lifecycle code
</previous_turn>

<current_turn>
User: "Here's the connection handling code from UserService.java"
</current_turn>
"""
```

---

### 10. Schema Reference vs Inline Definition (Section 12.4)

**Guide Specification:**
- Use schema references (~150 tokens) instead of full inline schemas (~800 tokens)
- Reference cached schema definitions

**Current Implementation:**
- Templates don't include schema definitions inline (good)
- But also don't include schema references or key field reminders

**Recommendation:**
Add schema reference section:
```python
"""
<output_schema ref="InvestigationResponse_{stage}">
Required fields:
- agent_response: Your natural response to user
- state_updates.milestones: Set newly completed milestones to true
- state_updates.outcome: One of [milestone_completed, data_requested, ...]
- internal_reasoning: Your analysis BEFORE state changes (required first)
</output_schema>
"""
```

---

## Positive Findings (Implemented Correctly)

1. **Three-Template System (Section 2.1):** ✅ Implemented with INQUIRY, INVESTIGATING, TERMINAL templates
2. **Stage-Specific Schemas (Section 2.3):** ✅ `schemas.py` has `InvestigationResponse_Verification`, `_Hypothesis`, `_Resolution`
3. **Adaptive Instructions (Section 4.4):** ✅ `STAGE_INSTRUCTIONS` dict provides stage-specific guidance
4. **Fallback Templates:** ✅ Fallback versions for token limits/errors
5. **System Feedback:** ✅ `context_builder.py` includes system feedback from previous turn
6. **Knowledge Base Results:** ✅ KB results are included in context
7. **Hypothesis-Evidence Links:** ✅ `HypothesisEvidenceLinkToAdd` schema defined
8. **Path Selection Note:** ✅ MITIGATION_FIRST path added to instructions when applicable

---

## Prioritized Recommendations

### Critical (Implement First)
1. **Reasoning-First Schema** - Prevents hallucinated milestone completion
2. **Prompt Security** - Prevents injection attacks

### High Priority
3. **Blocker Detection** - Eliminates 3-turn waste on unusable data
4. **Token Budget Optimization** - Stage-specific context loading
5. **Degraded Mode Instructions** - Proper handling when stuck

### Medium Priority
6. **XML-Based Structure** - Better parsing, token efficiency
7. **History Summarization** - Reduce token usage
8. **INQUIRY Fast-Track** - Complete the workflow
9. **Error Recovery** - JSON repair, retry policies

### Low Priority
10. **Schema References** - Minor token optimization
11. **Caching Strategy** - Performance optimization

---

## Implementation Effort Estimates

| Feature | Effort | Files Affected |
|---------|--------|----------------|
| Reasoning-First Schema | Medium | schemas.py, templates.py, milestone_engine.py |
| Prompt Security | Medium | context_builder.py, new security module |
| Blocker Detection | Medium | schemas.py, templates.py |
| Token Budget | Low | context_builder.py |
| Degraded Mode | Low | templates.py |
| XML Structure | Medium | templates.py |
| History Summary | Low | context_builder.py |
| Error Recovery | Medium | Separate retry/recovery module |

---

## Conclusion

The current implementation provides a solid foundation with the three-template system, stage-specific schemas, and adaptive instructions. However, significant gaps exist in:

1. **Quality Assurance** (reasoning-first, validation)
2. **Security** (input/output sanitization)
3. **Efficiency** (token budget, history summarization)
4. **Error Handling** (blocker detection, recovery)

Addressing these gaps will improve investigation quality, security, and performance while aligning the implementation with the comprehensive guidelines documented in the prompt engineering guide.
