# Investigation Workflow Improvements

**Version**: 1.1
**Date**: 2026-01-31
**Status**: Proposal (Revised)
**Author**: Architecture Review
**Revision Notes**: Addressed feedback on audit trail preservation, semantic urgency detection, and context-aware effort estimation

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

**Proposed Change**: Assess urgency during INQUIRY using semantic business impact definitions, not keyword matching.

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

        LLM assesses based on BUSINESS IMPACT, not keywords.
        """
    )

class PreliminaryUrgency(BaseModel):
    level: UrgencyLevel  # CRITICAL | HIGH | MEDIUM | LOW
    is_ongoing: bool     # True if problem appears active
    impact_assessment: str  # Brief description of business impact
    mitigation_hint: Optional[str]  # Quick mitigation if obvious
```

**Prompt Addition** (INQUIRY template):

```
**Step 0.5: URGENCY PRE-ASSESSMENT** (Check Every Turn)

Assess urgency based on BUSINESS IMPACT described by the user:

🔴 CRITICAL - Complete service unavailability or data loss/corruption
   • Users cannot access the system at all
   • Data is being lost or corrupted
   • Security breach in progress
   • Revenue-generating functionality completely blocked

🟠 HIGH - Significant degradation affecting most users
   • Core functionality severely impaired
   • Large portion of users experiencing failures
   • SLA breach imminent or occurring
   • Customer-facing impact with complaints

🟡 MEDIUM - Partial degradation or intermittent issues
   • Some users affected, workarounds possible
   • Non-critical features unavailable
   • Performance degraded but functional
   • Internal-only impact

🟢 LOW - Minor issues or historical investigation
   • Cosmetic issues, no functional impact
   • Problem occurred in the past (post-mortem)
   • Affects only development/staging
   • User is investigating proactively

ALSO ASSESS: Is this ONGOING (happening now) or HISTORICAL (happened in past)?

IF CRITICAL/HIGH + ONGOING:
→ Set preliminary_urgency with level and impact_assessment
→ If obvious quick mitigation exists: mention in response
→ Offer: "This sounds like it's actively impacting users. Should I focus
   on quick mitigation first, then investigate root cause after?"

This enables faster path selection without waiting for full verification.
```

### 3.3 Evidence Prioritization Framework

**Proposed Change**: Guide evidence requests by diagnostic value, with context-aware effort consideration.

**Key Design Decision**: Effort estimation should NOT be hardcoded (e.g., "logs = low effort").
What's easy in one organization may require tickets and approvals in another. Instead:

1. **Prioritize by diagnostic value first** (what's most likely to reveal root cause)
2. **Prefer data sources the user has already demonstrated access to**
3. **Always offer alternatives** when requesting potentially difficult data
4. **Let user indicate effort** rather than assuming

```
EVIDENCE PRIORITIZATION PRINCIPLES:

**Priority 1: Diagnostic Value** (What's most likely to reveal root cause?)

HIGH VALUE evidence (request first):
• Error logs/stack traces - Direct indicators of what failed
• Timeline correlation data - What changed before failure?
• Metrics at failure time - Quantifiable symptoms

MEDIUM VALUE evidence (request if high value inconclusive):
• Configuration files - Could reveal misconfigurations
• Service dependencies - Could reveal cascade failures
• Historical patterns - Could reveal recurring issues

LOW VALUE evidence (request only if stuck):
• General system health - Rarely diagnostic for specific issues
• Unrelated service logs - Fishing expedition

**Priority 2: User Context Awareness**

PREFER data the user has already shown access to:
• If user mentioned "I checked the dashboard" → Request dashboard data
• If user pasted a log snippet → Request more from same log
• If user mentioned a specific tool → Use that tool's outputs

AVOID assuming access:
❌ "Run this kubectl command" (user may not have cluster access)
✅ "Do you have access to view pod logs? If so, [command].
    If not, a dashboard screenshot would also help."

**Priority 3: Always Offer Alternatives**

When requesting evidence that might be difficult:

"To diagnose this, the most useful would be [PRIMARY REQUEST].
If that's difficult to obtain, [ALTERNATIVE] would also help narrow it down."
```

**Implementation Note**: User/organization profiles could include `easy_access_data_types`
to inform evidence prioritization, but this is optional. The default should be to ask
rather than assume.

### 3.4 Root Cause Identification Patterns

**Proposed Change**: Replace arbitrary 70/30 guideline with explicit decision criteria.
Use "Single-Shot Validation" pattern to preserve audit trail even when root cause is obvious.

**Key Design Decision**: Never skip hypothesis creation. Even when root cause is obvious,
create the hypothesis record to maintain full audit trail (Evidence → Hypothesis → Resolution).
The speed gain comes from validating in the same turn, not from skipping structured records.

```
ROOT CAUSE IDENTIFICATION - DECISION TREE:

┌─────────────────────────────────────────────────────────┐
│ Is root cause OBVIOUS from current evidence?            │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ SINGLE-SHOT VALIDATION criteria (ALL must be true):     │
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
│ IF ALL TRUE → Use SINGLE-SHOT VALIDATION                │
│   In ONE turn, do ALL of the following:                 │
│   1. Create hypothesis (hypotheses_to_add)              │
│      - statement: The identified root cause             │
│      - category: Appropriate HypothesisCategory         │
│      - initial_likelihood: 0.90+ (high confidence)      │
│   2. Link evidence (hypothesis_evidence_links)          │
│      - Link existing evidence to hypothesis             │
│      - stance: SUPPORTS with high confidence            │
│   3. Mark hypothesis VALIDATED                          │
│      - status: VALIDATED                                │
│   4. Set root_cause_identified = True                   │
│   5. Fill root_cause_conclusion                         │
│      - root_cause_method: "single_shot_validation"      │
│                                                         │
│   Result: Full audit trail preserved, same speed as     │
│   skipping hypothesis generation                        │
│                                                         │
│ IF ANY FALSE → Use MULTI-HYPOTHESIS TESTING             │
│   - Generate 2-4 hypotheses across DIFFERENT categories │
│   - Ensure at least 2 different HypothesisCategory      │
│   - Request diagnostic evidence to differentiate        │
│   - Validate/refute based on evidence over turns        │
│                                                         │
└─────────────────────────────────────────────────────────┘

AUDIT TRAIL RATIONALE:

The hypothesis record serves as structured documentation of WHY the agent
concluded the root cause. Without it, you have a "magic answer" that can't
be programmatically audited later. The single-shot pattern provides:

• Traceability: Evidence → Hypothesis → Root Cause → Solution
• Accountability: Explicit reasoning in hypothesis statement
• Learning: Hypothesis records feed the knowledge base
• Validation: Post-incident review can verify reasoning
```

### 3.5 Proactive Tool Usage

**Proposed Change**: Add explicit tool consideration to each stage, with negative constraints
to prevent noise when tools return no results.

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

**CRITICAL: Negative Constraints (What NOT to Do)**

```
TOOL RESULT HANDLING - SILENCE ON FAILURE:

When tool searches return NO RESULTS or LOW CONFIDENCE (<50%):

❌ DON'T say: "I checked the knowledge base but found nothing relevant."
❌ DON'T say: "I searched for similar cases but couldn't find any."
❌ DON'T say: "Unfortunately, our documentation doesn't cover this."

These statements:
• Add noise without value
• Reduce user confidence in the system
• Waste conversation turns

✅ DO: Proceed silently to the next step (investigation, evidence request)
✅ DO: Only mention tool results when they ARE helpful

EXCEPTION: If user EXPLICITLY asks "Have you seen this before?" or
"Is this a known issue?", then you MAY say you found no matches.

EXAMPLE:

Tool search returns: 0 results, confidence 0.2

BAD Response:
"I searched our knowledge base but couldn't find similar cases.
Let me start by understanding your environment..."

GOOD Response:
"Let me start by understanding your environment. Can you share..."
(Tool failure not mentioned - proceeds to investigation)
```

### 3.6 Fast-Track State Transitions (Instant Resolution Path)

**Gap Addressed**: The Knowledge Pre-Check (Section 3.1) allows offering a solution
during INQUIRY, but the state transition for "instant resolution" wasn't defined.

**Proposed Change**: Define explicit state transition for when knowledge base match
resolves the issue without formal investigation.

```
FAST-TRACK RESOLUTION PATH:

┌─────────────────────────────────────────────────────────┐
│ INQUIRY → RESOLVED (Skipping INVESTIGATING)             │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ Trigger: Knowledge base match + User confirms fix works │
│                                                         │
│ Flow:                                                   │
│ 1. INQUIRY: KB search finds high-confidence match       │
│ 2. INQUIRY: Agent offers known solution to user         │
│ 3. INQUIRY: User tries solution, confirms "Yes, fixed!" │
│ 4. System: Transition directly to RESOLVED              │
│                                                         │
│ Required State Updates (in final INQUIRY turn):         │
│ • knowledge_resolution: {                               │
│     match_id: "<case_id or runbook_id>",                │
│     solution_applied: "<what user did>",                │
│     user_confirmation: "<user's confirmation message>"  │
│   }                                                     │
│ • case_status: RESOLVED                                 │
│ • closure_reason: "knowledge_base_resolution"           │
│ • time_to_resolution: <turn count> (typically 1-2)      │
│                                                         │
│ What Gets Skipped:                                      │
│ • INVESTIGATING status entirely                         │
│ • Formal hypothesis generation                          │
│ • Evidence collection beyond KB match                   │
│ • Solution proposal stage                               │
│                                                         │
│ What Gets Preserved:                                    │
│ • Full conversation history                             │
│ • Knowledge match reference (for learning/attribution)  │
│ • Resolution timestamp and metrics                      │
│                                                         │
└─────────────────────────────────────────────────────────┘

METRICS IMPLICATIONS:

Cases resolved via Fast-Track should be tracked separately:
• time_to_resolution: Extremely low (1-2 turns)
• resolution_type: "knowledge_base" vs "investigation"
• knowledge_attribution: Which KB item resolved it

This keeps investigation metrics clean while highlighting KB value.
```

**Schema Addition**:

```python
class KnowledgeResolution(BaseModel):
    """Records instant resolution via knowledge base match."""

    match_id: str = Field(
        description="ID of the case or runbook that provided the solution"
    )
    match_type: str = Field(
        description="past_case | runbook | documentation"
    )
    solution_applied: str = Field(
        description="What the user actually did based on the suggestion"
    )
    user_confirmation: str = Field(
        description="User's message confirming the fix worked"
    )
    resolution_turn: int = Field(
        description="Turn number when resolution was confirmed"
    )
```

**State Transition Logic** (system-side):

```python
def process_inquiry_response(case: Case, response: InquiryResponse) -> Case:
    """Process INQUIRY response, potentially fast-tracking to RESOLVED."""

    # Check for fast-track resolution
    if response.state_updates.knowledge_resolution:
        kr = response.state_updates.knowledge_resolution

        # Validate we have confirmation
        if kr.user_confirmation and kr.solution_applied:
            # Fast-track to RESOLVED
            case.status = CaseStatus.RESOLVED
            case.closure_reason = "knowledge_base_resolution"
            case.closed_at = datetime.now(timezone.utc)
            case.knowledge_resolution = kr

            # Calculate time to resolution
            case.time_to_resolution = calculate_resolution_time(case)

            # Do NOT enter INVESTIGATING at all
            return case

    # Normal INQUIRY processing continues...
    return process_normal_inquiry(case, response)
```

---

## 4. Prompt Engineering Improvements

### 4.1 Tiered Prompt System with Dynamic Token Budgeting

**Problem**: Current INVESTIGATING prompt is ~3000+ tokens every turn.

**Solution**: Split into core instructions (always included) and expanded sections (included when relevant),
with dynamic budgeting based on user message size.

```python
def build_investigating_prompt(case: Case, user_message: str) -> str:
    """
    Tiered prompt system with dynamic budgeting:
    - CORE: ~800 tokens (always included)
    - STAGE-SPECIFIC: ~400 tokens (based on current stage)
    - EXPANDED: ~500 tokens (included only when relevant)

    Dynamic budgeting adjusts based on user_message length to prevent
    context window overflow.
    """

    # Calculate available budget
    user_message_tokens = estimate_tokens(user_message)
    budget = calculate_prompt_budget(user_message_tokens)

    sections = []

    # CORE SECTION (always included)
    sections.append(build_core_header(case))
    sections.append(build_state_context(case))
    sections.append(build_user_message_section(user_message))
    sections.append(build_core_instructions())  # Condensed essential rules

    # STAGE-SPECIFIC (only current stage)
    sections.append(build_stage_instructions(case.progress.current_stage))

    # EXPANDED SECTIONS (conditional, budget-aware)
    if budget.allows_expanded:
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

**Dynamic Token Budgeting**:

```python
@dataclass
class PromptBudget:
    """Dynamic budget based on user message size."""

    total_context_limit: int = 8000  # Conservative limit for response room
    core_budget: int = 800
    stage_budget: int = 400
    output_format_budget: int = 200
    allows_expanded: bool = True


def calculate_prompt_budget(user_message_tokens: int) -> PromptBudget:
    """
    Calculate available prompt budget based on user message size.

    When user pastes a large log file (e.g., 2000+ tokens), we need to
    collapse expanded sections to prevent context window overflow.
    """

    budget = PromptBudget()

    # Reserved space for: user message + system prompt + response
    used_tokens = user_message_tokens + budget.core_budget + budget.stage_budget + budget.output_format_budget

    # If user message is large, disable expanded sections
    if user_message_tokens > 1500:
        budget.allows_expanded = False

    # If extremely large, use minimal prompt
    if user_message_tokens > 3000:
        budget.core_budget = 400  # Use ultra-condensed core
        budget.stage_budget = 200  # Use minimal stage instructions

    return budget


# Thresholds for prompt tier selection
PROMPT_TIERS = {
    "full": {
        "max_user_tokens": 1000,
        "core": "full",
        "stage": "full",
        "expanded": True
    },
    "standard": {
        "max_user_tokens": 2000,
        "core": "full",
        "stage": "full",
        "expanded": False  # Skip expanded sections
    },
    "minimal": {
        "max_user_tokens": float("inf"),
        "core": "condensed",
        "stage": "condensed",
        "expanded": False
    }
}
```

**Rationale**: When a user pastes a 2000-token error log, the system should automatically
use a more condensed prompt to leave room for:
1. The full user message in context
2. Adequate response generation space
3. Safety margin for the model

This prevents context window errors and ensures the model focuses on the evidence
rather than lengthy instructions it's seen many times before.

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
