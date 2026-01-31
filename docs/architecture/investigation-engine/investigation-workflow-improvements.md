# Investigation Workflow Improvements

**Version**: 1.0
**Date**: 2026-01-31
**Status**: Proposal
**Author**: Architecture Review

---

## Executive Summary

This document presents a comprehensive analysis of FaultMaven's investigation workflow and prompt engineering, identifying key improvement opportunities to make the troubleshooting process more effective, efficient, and user-friendly.

**Key Findings**:
- Strong foundation with opportunistic milestone-based architecture
- Several gaps in knowledge integration and proactive tool usage
- Prompt engineering can be optimized for reduced cognitive load and better guidance
- Path selection timing and evidence prioritization need refinement

---

## Table of Contents

1. [Framework Strengths](#1-framework-strengths)
2. [Identified Gaps](#2-identified-gaps)
3. [Workflow Improvements](#3-workflow-improvements)
4. [Prompt Engineering Improvements](#4-prompt-engineering-improvements)
5. [Implementation Recommendations](#5-implementation-recommendations)

---

## 1. Framework Strengths

### 1.1 Opportunistic Milestone-Based Architecture

The framework correctly moves away from rigid sequential phases to a data-driven model where the agent completes tasks based on data availability rather than artificial phase barriers.

**Why This Works**:
- Eliminates "waiting for phase transition" delays
- Enables one-turn resolution when comprehensive data is available
- Mimics how expert human investigators actually work

### 1.2 Clear Three-Template System

The INQUIRY → INVESTIGATING → TERMINAL structure provides:
- Appropriate schema complexity per state
- Clear separation of concerns
- Simplified maintenance vs. many micro-templates

### 1.3 LLM vs System Responsibility Separation

The division where LLM determines observables (summary, analysis, stance) and system infers calculations (category, advances_milestones) is architecturally sound:
- Reduces LLM hallucination risk on structured fields
- Allows system-level consistency enforcement
- Enables schema evolution without prompt changes

### 1.4 Working Conclusion Pattern

Maintaining continuous working conclusion with transparent confidence is excellent:
- Builds user trust through transparency
- Provides investigation coherence across turns
- Enables graceful degradation with honest caveats

### 1.5 Degraded Mode Handling

Thoughtful degraded mode design with:
- Explicit confidence constraints
- Transparent limitation communication
- Fallback options rather than hard failures

---

## 2. Identified Gaps

### 2.1 Missing Knowledge Base Integration in Inquiry

**Current State**: INQUIRY phase focuses on problem formalization and user commitment without leveraging the knowledge base.

**Gap**: Before asking users for clarification or committing to investigation, the agent should:
1. Search for similar past cases (pattern matching)
2. Check relevant documentation/runbooks
3. Identify potential "instant resolution" opportunities

**Impact**: Users may wait through multi-turn investigations for problems already solved in the knowledge base.

### 2.2 Late Path Selection

**Current State**: Path selection (MITIGATION_FIRST vs ROOT_CAUSE) happens AFTER verification milestones complete.

**Gap**: By verification completion, the user may have already experienced delay. Urgency signals are often present in the initial problem description.

**Impact**: Critical ongoing issues may not get immediate mitigation when early indicators are clear.

### 2.3 Evidence Request Prioritization

**Current State**: Prompts emphasize requesting "objective data" but lack guidance on:
- Prioritizing which evidence to request first
- Estimating user effort to obtain evidence
- Offering alternatives when primary evidence is difficult
- Defining "minimum viable evidence" for progress

**Impact**: Users may be asked for difficult-to-obtain evidence when simpler alternatives exist.

### 2.4 Arbitrary Hypothesis Generation Guidelines

**Current State**: "70% of cases should identify root cause directly, 30% need hypothesis testing" guideline.

**Gap**: This ratio is arbitrary and doesn't provide clear decision criteria:
- When exactly should the agent skip hypothesis generation?
- How to recognize "obvious" vs. "unclear" root causes?
- Missing proactive diversity guidance (avoid category anchoring)

### 2.5 Prompt Cognitive Load

**Current State**: INVESTIGATING template is very long (estimated 3000+ tokens of instructions).

**Gaps**:
- No tiered prompt system (core vs. expanded instructions)
- GENERAL INSTRUCTIONS section repeats every turn
- Stage-specific guidance could be more concise

### 2.6 Missing Tool Usage Guidance

**Current State**: Framework describes tools (knowledge_base, web_search, document_qa) but prompts don't guide proactive usage.

**Gap**: Agent should consider tool usage BEFORE requesting data from user:
- Search knowledge base for similar symptoms
- Check documentation for known issues
- Use web search for external service issues

### 2.7 Solution Verification Rigidity

**Current State**: Generic verification criteria ("stable for 15-30 minutes").

**Gaps**:
- Doesn't adapt to problem type (immediate crash vs. gradual degradation)
- No guidance on partial success (symptom reduced but not eliminated)
- Missing re-verification guidance if user reports recurrence

---

## 3. Workflow Improvements

### 3.1 Enhanced Inquiry Phase with Knowledge Pre-Check

**Proposed Change**: Add knowledge base consultation before problem formalization.

```
INQUIRY FLOW (Enhanced):

┌─────────────────────────────────────────────────────────┐
│ Step 0: KNOWLEDGE PRE-CHECK (New!)                      │
├─────────────────────────────────────────────────────────┤
│ When user describes any symptom:                        │
│                                                         │
│ 1. Search knowledge base for similar symptoms           │
│    → Query: symptom keywords, error messages, services  │
│                                                         │
│ 2. Check recent resolved cases (last 30 days)           │
│    → Pattern: Same symptom + same service?              │
│                                                         │
│ 3. Search runbooks for matching scenarios               │
│    → Query: symptom + affected component                │
│                                                         │
│ IF MATCH FOUND with confidence > 70%:                   │
│    → Present: "This looks similar to [past case]. The   │
│       solution was [X]. Would you like to try that      │
│       first, or proceed with full investigation?"       │
│                                                         │
│ IF NO STRONG MATCH:                                     │
│    → Proceed to Step A (problem formalization)          │
└─────────────────────────────────────────────────────────┘
```

**Benefits**:
- Faster resolution for known issues (potential 1-turn fix)
- Builds knowledge flywheel value
- Reduces redundant investigations

### 3.2 Early Urgency Assessment and Preliminary Path Signal

**Proposed Change**: Assess urgency during INQUIRY, not just after verification.

```python
class InquiryData(BaseModel):
    # ... existing fields ...

    # NEW: Early urgency signal
    preliminary_urgency: Optional[PreliminaryUrgency] = Field(
        default=None,
        description="""
        Early urgency assessment from problem description.
        Used to signal potential MITIGATION_FIRST path before
        full verification completes.

        LLM assesses from keywords like:
        - "production is down" → CRITICAL
        - "users are complaining" → HIGH
        - "noticed yesterday" → LOW (historical)
        """
    )

class PreliminaryUrgency(BaseModel):
    level: UrgencyLevel  # CRITICAL | HIGH | MEDIUM | LOW
    is_ongoing: bool     # True if problem appears active
    urgency_signals: List[str]  # Keywords that triggered assessment
    mitigation_hint: Optional[str]  # Quick mitigation if obvious
```

**Prompt Addition** (INQUIRY template):

```
**Step 0.5: URGENCY PRE-ASSESSMENT** (Check Every Turn)

Scan user's message for urgency indicators:

🔴 CRITICAL signals: "down", "outage", "can't access", "data loss"
🟠 HIGH signals: "affecting users", "production", "urgent", "ASAP"
🟡 MEDIUM signals: "intermittent", "slow", "degraded"
🟢 LOW signals: "noticed yesterday", "happened last week", "historical"

IF CRITICAL/HIGH + ONGOING detected:
→ Set preliminary_urgency with level and is_ongoing
→ If obvious mitigation (rollback recent deploy): mention in response
→ Offer: "This sounds urgent. Should I focus on quick mitigation first?"

This enables faster path selection without waiting for full verification.
```

### 3.3 Evidence Prioritization Framework

**Proposed Change**: Guide evidence requests by effort and diagnostic value.

```
EVIDENCE PRIORITIZATION MATRIX:

┌─────────────────┬───────────────────┬─────────────────────┐
│ Diagnostic Value │ Low User Effort   │ High User Effort    │
├─────────────────┼───────────────────┼─────────────────────┤
│ HIGH            │ ✅ Request First   │ 🔄 Request if needed │
│ (Likely to show │ Examples:         │ Examples:           │
│ root cause)     │ • Error logs      │ • Production DB     │
│                 │ • Recent deploys  │   access            │
│                 │ • Basic metrics   │ • Heap dumps        │
├─────────────────┼───────────────────┼─────────────────────┤
│ MEDIUM          │ 🔄 Request second │ ⏳ Request last     │
│ (Helps narrow   │ Examples:         │ Examples:           │
│ down)           │ • Config files    │ • Profiling data    │
│                 │ • Service status  │ • Network traces    │
├─────────────────┼───────────────────┼─────────────────────┤
│ LOW             │ ⚠️ Usually skip   │ ❌ Avoid unless     │
│ (Nice to have)  │                   │ stuck               │
└─────────────────┴───────────────────┴─────────────────────┘

EVIDENCE REQUEST GUIDANCE:

When requesting evidence, always:
1. Start with HIGH value + LOW effort items
2. Offer alternatives: "If you can't access X, Y would also help"
3. Explain WHY: "Error logs will show whether this is a code bug or config issue"
4. Estimate effort: "This usually takes ~2 minutes to retrieve"
```

### 3.4 Hypothesis Decision Criteria

**Proposed Change**: Replace arbitrary 70/30 guideline with explicit decision criteria.

```
ROOT CAUSE IDENTIFICATION - DECISION TREE:

┌─────────────────────────────────────────────────────────┐
│ Can you identify root cause DIRECTLY from evidence?     │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ DIRECT IDENTIFICATION criteria (ALL must be true):      │
│                                                         │
│ □ Single clear error/exception pointing to specific     │
│   code/config/resource                                  │
│                                                         │
│ □ Timing correlation is strong (change at T, error at   │
│   T+minutes, not T+hours)                               │
│                                                         │
│ □ Error mechanism is understandable (you can explain    │
│   HOW the cause produces the symptom)                   │
│                                                         │
│ □ No conflicting evidence suggesting other causes       │
│                                                         │
│ IF ALL TRUE → Use DIRECT IDENTIFICATION                 │
│   - Set root_cause_identified = True                    │
│   - Set root_cause_method = "direct_analysis"           │
│   - Skip hypothesis generation                          │
│                                                         │
│ IF ANY FALSE → Use HYPOTHESIS TESTING                   │
│   - Generate 2-4 hypotheses across DIFFERENT categories │
│   - Ensure at least 2 different HypothesisCategory      │
│   - Request diagnostic evidence to differentiate        │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 3.5 Proactive Tool Usage

**Proposed Change**: Add explicit tool consideration to each stage.

```
TOOL USAGE CHECKLIST (Check Before Requesting User Data):

□ KNOWLEDGE BASE
  - Search for similar symptoms in past cases
  - Check if same service had recent issues
  - Look for relevant runbook entries

  Trigger: Any new symptom description or error message

□ DOCUMENT QA
  - Query system documentation for configuration guidance
  - Check architecture docs for service dependencies

  Trigger: Configuration-related symptoms, dependency issues

□ WEB SEARCH (if enabled)
  - Search for known issues with external services
  - Check vendor status pages
  - Find community solutions for common errors

  Trigger: External service errors, third-party library issues

GOLDEN RULE: Check tools BEFORE asking user for data you might
already have access to through knowledge base or documentation.
```

---

## 4. Prompt Engineering Improvements

### 4.1 Tiered Prompt System

**Problem**: Current INVESTIGATING prompt is ~3000+ tokens every turn.

**Solution**: Split into core instructions (always included) and expanded sections (included when relevant).

```python
def build_investigating_prompt(case: Case, user_message: str) -> str:
    """
    Tiered prompt system:
    - CORE: ~800 tokens (always included)
    - STAGE-SPECIFIC: ~400 tokens (based on current stage)
    - EXPANDED: ~500 tokens (included only when relevant)
    """

    sections = []

    # CORE SECTION (always included)
    sections.append(build_core_header(case))
    sections.append(build_state_context(case))
    sections.append(build_user_message_section(user_message))
    sections.append(build_core_instructions())  # Condensed essential rules

    # STAGE-SPECIFIC (only current stage)
    sections.append(build_stage_instructions(case.progress.current_stage))

    # EXPANDED SECTIONS (conditional)
    if case.degraded_mode:
        sections.append(build_degraded_mode_section(case))

    if case.hypotheses and len(case.hypotheses) > 0:
        sections.append(build_hypothesis_evaluation_guidance())

    if case.progress.solution_proposed and not case.progress.solution_verified:
        sections.append(build_verification_guidance())

    # OUTPUT FORMAT (always included, condensed)
    sections.append(build_output_format_condensed())

    return "\n\n".join(sections)
```

### 4.2 Condensed Core Instructions

**Current**: Long "GENERAL INSTRUCTIONS" section with detailed examples.

**Proposed**: Condensed core with example bank referenced by ID.

```
═══════════════════════════════════════════════════════════
CORE RULES (Apply Always)
═══════════════════════════════════════════════════════════

**Evidence**: Create only from objective data (uploads, command output).
User descriptions → Request actual data.

**Confidence**: < 50% = "speculate", 50-69% = "probably",
70-89% = "confident", 90%+ = "verified"

**Milestones**: Set True only with evidence. Can complete multiple per turn.
Never set False.

**Hypotheses**: Evaluate new evidence against ALL active hypotheses.
One evidence can have different stances for different hypotheses.

**Response Style**: Acknowledge provided data before requesting more.
Never mention: milestones, stages, phases, framework.
```

### 4.3 Structured Stage Instructions

**Current**: Stage instructions as prose paragraphs.

**Proposed**: Checklist format for clarity.

```
**STAGE: SYMPTOM_VERIFICATION**

Goal: Confirm problem exists, understand context

Checklist:
□ symptom_verified - Error/issue confirmed with logs/metrics?
□ scope_assessed - Who/what affected? (users, services, regions)
□ timeline_established - When started? Still ongoing?
□ changes_identified - Recent deployments, config changes?

Determine:
□ temporal_state: ONGOING (happening now) or HISTORICAL (past)
□ urgency_level: CRITICAL / HIGH / MEDIUM / LOW

Jump-Ahead: If evidence reveals root cause → Set root_cause_identified

Stage Exit: All verification milestones True → System selects path
```

### 4.4 Enhanced Evidence Request Format

**Current**: Generic guidance to request "objective data".

**Proposed**: Structured request format with alternatives.

```
EVIDENCE REQUEST FORMAT:

When requesting evidence, use this structure:

"To verify [WHAT YOU'RE CHECKING], I need [SPECIFIC DATA].

**Primary request**: [Specific command or file]
   Example: `kubectl logs deployment/api -n production --since=1h`

**Alternative** (if primary unavailable): [Easier alternative]
   Example: Dashboard screenshot showing error rate

**Why this helps**: [Brief explanation of diagnostic value]

**Estimated effort**: [Quick/Moderate/Significant]"
```

### 4.5 Confidence Communication Templates

**Current**: Generic guidance on confidence language.

**Proposed**: Explicit templates for each confidence tier.

```
CONFIDENCE COMMUNICATION:

< 50% (Speculation):
"Based on limited evidence, I can only speculate that [theory].
This is an educated guess - [what would increase confidence]."

50-69% (Probable):
"This is probably [theory] (confidence: ~X%).
The [evidence] suggests this, but [caveat].
[What would confirm] would increase my confidence."

70-89% (Confident):
"I'm confident that [theory] (confidence: ~X%).
The evidence shows [key points].
[Minor uncertainty] is the main remaining question."

90%+ (Verified):
"Verified: [conclusion]. The [evidence] definitively shows [mechanism]."
```

---

## 5. Implementation Recommendations

### 5.1 Priority 1: Knowledge Pre-Check (High Impact, Medium Effort)

**Changes Required**:
1. Add `knowledge_pre_check` step to INQUIRY prompt
2. Add `similar_cases_found` field to InquiryStateUpdate
3. Add tool call for knowledge base search in inquiry handler
4. Update prompt to offer instant resolution when match found

**Expected Impact**: 20-30% of cases resolved in 1-2 turns via knowledge retrieval

### 5.2 Priority 2: Tiered Prompt System (High Impact, High Effort)

**Changes Required**:
1. Refactor prompt templates into modular sections
2. Implement conditional section inclusion logic
3. Create condensed versions of each section
4. Add prompt token tracking for optimization

**Expected Impact**: 40-50% reduction in prompt tokens, improved LLM focus

### 5.3 Priority 3: Early Urgency Assessment (Medium Impact, Low Effort)

**Changes Required**:
1. Add `preliminary_urgency` to InquiryData model
2. Update INQUIRY prompt with urgency detection guidance
3. Use preliminary urgency to inform early path hints

**Expected Impact**: Faster mitigation for critical ongoing issues

### 5.4 Priority 4: Evidence Prioritization (Medium Impact, Medium Effort)

**Changes Required**:
1. Add evidence prioritization matrix to prompts
2. Update evidence request format with alternatives
3. Add effort estimation guidance

**Expected Impact**: Reduced user friction, faster evidence collection

### 5.5 Priority 5: Proactive Tool Usage (Medium Impact, Medium Effort)

**Changes Required**:
1. Add tool usage checklist to stage prompts
2. Implement automatic knowledge base search before requests
3. Add tool usage to response schema for tracking

**Expected Impact**: Better knowledge utilization, reduced redundant data requests

---

## Appendix A: Prompt Section Templates

### A.1 Knowledge Pre-Check Section

```
═══════════════════════════════════════════════════════════
KNOWLEDGE PRE-CHECK (Before Problem Formalization)
═══════════════════════════════════════════════════════════

Before asking for more details, search your knowledge:

1. **Similar Past Cases**: Search for cases with similar symptoms
   Query: [symptom keywords] + [affected service if mentioned]

2. **Recent Issues**: Check if this service had issues in last 30 days
   Pattern: Same symptom + same component = potential repeat

3. **Runbooks**: Search for matching troubleshooting procedures
   Query: [symptom] + [error message if present]

IF STRONG MATCH (confidence > 70%):
→ In your response, say:
  "This looks similar to a case we resolved recently. The solution
  was [X]. Would you like to try that first, or should I investigate
  from scratch?"

IF WEAK/NO MATCH:
→ Proceed with normal problem formalization
```

### A.2 Condensed Output Format

```
═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════

Return JSON:
{
  "agent_response": "<natural response>",
  "state_updates": {
    "milestones": { "<milestone>": true, ... } or null,
    "evidence_to_add": [...] or [],
    "working_conclusion": {...} or null,
    "outcome": "<turn_outcome>"
  }
}

Rules:
- Only include fields that CHANGE this turn
- Be opportunistic - complete everything data supports
- outcome: milestone_completed | data_provided | data_requested |
          hypothesis_tested | case_resolved | conversation | other
```

---

## Appendix B: Metrics for Improvement Tracking

| Metric | Current Baseline | Target | Measurement |
|--------|------------------|--------|-------------|
| Avg turns to resolution | TBD | -30% | Case analytics |
| Knowledge base hit rate in INQUIRY | 0% | 25%+ | Tool usage logs |
| Critical case time to first mitigation | TBD | -50% | Case timeline |
| Prompt tokens per turn | ~3000 | ~1500 | Prompt analytics |
| User data requests before resolution | TBD | -20% | Evidence request count |
| Working conclusion accuracy | TBD | 85%+ | Post-resolution review |

---

**END OF DOCUMENT**
