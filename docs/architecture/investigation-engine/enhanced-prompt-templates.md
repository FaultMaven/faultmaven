# Enhanced Prompt Templates v2.1

**Version**: 2.2 (Enhancement Proposal - Revised)
**Date**: 2026-01-31
**Status**: Draft for Review
**Base Version**: prompt-templates.md v2.0
**Revision Notes**: Addressed feedback on audit trail, semantic urgency, and tool constraints

---

## Overview

This document provides enhanced prompt templates incorporating the improvements identified in `investigation-workflow-improvements.md`. Key enhancements:

1. **Knowledge Pre-Check** in INQUIRY phase with Fast-Track resolution path
2. **Tiered Prompt System** with dynamic token budgeting
3. **Semantic Urgency Assessment** (business impact, not keywords)
4. **Context-Aware Evidence Prioritization**
5. **Proactive Tool Usage with Negative Constraints**
6. **Single-Shot Validation** pattern (preserves audit trail)

---

## 1. Enhanced INQUIRY Template

### 1.1 Template Structure

```python
def build_enhanced_inquiry_prompt(case: Case, user_message: str) -> str:
    """
    Enhanced INQUIRY template with knowledge pre-check and early urgency.

    Key additions:
    - Knowledge base search before formalization
    - Preliminary urgency assessment
    - Instant resolution path for known issues
    """

    # Get knowledge context (pre-computed by system)
    kb_context = get_knowledge_context(case, user_message)

    prompt = f"""<!-- Prompt Version: 2.1.0 -->
<!-- Architecture: Investigation v2.1 -->

You are FaultMaven, an SRE troubleshooting copilot.

═══════════════════════════════════════════════════════════
STATUS: INQUIRY (Pre-Investigation)
═══════════════════════════════════════════════════════════

Turn: {case.current_turn}

{_build_knowledge_context_section(kb_context)}

{_build_previous_statement_section(case)}

═══════════════════════════════════════════════════════════
CURRENT USER MESSAGE
═══════════════════════════════════════════════════════════

{user_message}

═══════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════

**STEP 0: KNOWLEDGE CHECK** (Do This First!)

Before asking questions, check if you can help immediately:

{_build_knowledge_check_instructions(kb_context)}

**STEP 0.5: URGENCY PRE-ASSESSMENT** (Semantic, Not Keyword-Based)

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

🟡 MEDIUM - Partial degradation or intermittent issues
   • Some users affected, workarounds possible
   • Performance degraded but functional

🟢 LOW - Minor issues or historical investigation
   • Cosmetic issues, no functional impact
   • Problem occurred in the past (post-mortem)

Also assess: Is this ONGOING (happening now) or HISTORICAL?

If CRITICAL/HIGH + ONGOING:
→ Set preliminary_urgency in state_updates (with impact_assessment)
→ Offer: "This sounds like it's actively impacting users. Should I focus
   on quick mitigation first, then investigate root cause after?"

**STEP 1: ANSWER USER'S QUESTION**

Provide helpful, accurate response to their immediate query.

**STEP 2: PROBLEM DETECTION & FORMALIZATION**

{_build_problem_formalization_instructions()}

═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════

Return JSON:
{{
  "agent_response": "<natural response>",
  "state_updates": {{
    "problem_confirmation": {{...}} or null,
    "proposed_problem_statement": "<statement>" or null,
    "preliminary_urgency": {{
      "level": "critical|high|medium|low",
      "is_ongoing": true|false,
      "impact_assessment": "<brief description of business impact>",
      "mitigation_hint": "<quick fix if obvious>" or null
    }} or null,
    "knowledge_match": {{
      "match_type": "past_case|runbook|documentation",
      "match_confidence": 0.0-1.0,
      "match_summary": "<what matched>",
      "suggested_solution": "<potential quick fix>"
    }} or null,
    "knowledge_resolution": {{
      "match_id": "<id of case/runbook that solved it>",
      "match_type": "past_case|runbook|documentation",
      "solution_applied": "<what user did>",
      "user_confirmation": "<user's message confirming fix>"
    }} or null,
    "quick_suggestions": [...]
  }}
}}

**Fast-Track Resolution**: If user confirms a knowledge_match solution worked,
set knowledge_resolution to trigger INQUIRY → RESOLVED transition (skip INVESTIGATING).
"""

    return prompt


def _build_knowledge_context_section(kb_context: KnowledgeContext) -> str:
    """Build knowledge context section if relevant matches found."""

    if not kb_context or not kb_context.has_relevant_matches:
        return ""

    section = """═══════════════════════════════════════════════════════════
KNOWLEDGE BASE CONTEXT (Relevant Matches Found)
═══════════════════════════════════════════════════════════

"""

    if kb_context.similar_cases:
        section += "**Similar Past Cases:**\n"
        for case_match in kb_context.similar_cases[:3]:
            section += f"- [{case_match.title}] Resolved: {case_match.solution_summary} "
            section += f"(similarity: {case_match.similarity:.0%})\n"
        section += "\n"

    if kb_context.relevant_runbooks:
        section += "**Relevant Runbooks:**\n"
        for runbook in kb_context.relevant_runbooks[:2]:
            section += f"- {runbook.title}: {runbook.summary}\n"
        section += "\n"

    section += """
⚡ If a past case or runbook matches with >70% confidence, offer the
   known solution FIRST before starting new investigation.
"""

    return section


def _build_knowledge_check_instructions(kb_context: KnowledgeContext) -> str:
    """Build knowledge check instructions based on available context."""

    if kb_context and kb_context.has_high_confidence_match:
        return """
**HIGH CONFIDENCE MATCH DETECTED!**

A past case or runbook closely matches this symptom. In your response:

1. Acknowledge the user's issue
2. Say: "This looks similar to [past case/runbook]. The solution was [X]."
3. Ask: "Would you like to try that first, or should I investigate fresh?"

If user wants to try known solution → Set knowledge_match in state_updates
If user wants fresh investigation → Proceed to problem formalization
"""

    return """
No high-confidence matches in knowledge base. Proceed with normal
problem understanding. (You can still search KB if user provides more details)
"""
```

### 1.2 Enhanced InquiryStateUpdate Schema

```python
class InquiryStateUpdate(BaseModel):
    """Enhanced state update for INQUIRY with knowledge and urgency."""

    # Existing fields
    problem_confirmation: Optional[ProblemConfirmation] = None
    proposed_problem_statement: Optional[str] = None
    quick_suggestions: List[str] = Field(default_factory=list)

    # NEW: Early urgency assessment
    preliminary_urgency: Optional[PreliminaryUrgency] = Field(
        default=None,
        description="Early urgency assessment from problem description"
    )

    # NEW: Knowledge base match
    knowledge_match: Optional[KnowledgeMatch] = Field(
        default=None,
        description="High-confidence match from knowledge base"
    )


class PreliminaryUrgency(BaseModel):
    """Early urgency signal for faster path selection (semantic, not keyword-based)."""

    level: UrgencyLevel  # CRITICAL | HIGH | MEDIUM | LOW
    is_ongoing: bool = Field(
        description="True if problem appears to be happening now"
    )
    impact_assessment: str = Field(
        description="Brief description of business impact (e.g., 'Complete API outage affecting all customers')"
    )
    mitigation_hint: Optional[str] = Field(
        default=None,
        description="Quick mitigation suggestion if obvious (e.g., 'rollback recent deploy')"
    )


class KnowledgeMatch(BaseModel):
    """Knowledge base match for instant resolution path."""

    match_type: str = Field(
        description="past_case | runbook | documentation"
    )
    match_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the match"
    )
    match_summary: str = Field(
        description="Brief summary of what matched"
    )
    match_id: Optional[str] = Field(
        default=None,
        description="ID of matched case/runbook for reference"
    )
    suggested_solution: Optional[str] = Field(
        default=None,
        description="Potential quick fix from the match"
    )


class KnowledgeResolution(BaseModel):
    """Records instant resolution via knowledge base match (Fast-Track path)."""

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

**State Transition**: When `knowledge_resolution` is set, system transitions
directly from INQUIRY → RESOLVED, skipping INVESTIGATING entirely.

---

## 2. Enhanced INVESTIGATING Template (Tiered System)

### 2.1 Core Template (Always Included, ~800 tokens)

```python
def build_investigating_core(case: Case, user_message: str) -> str:
    """
    Core INVESTIGATING prompt - always included.
    Target: ~800 tokens
    """

    return f"""<!-- Prompt Version: 2.1.0 -->
<!-- Architecture: Investigation v2.1 -->

You are FaultMaven, an SRE troubleshooting copilot.

═══════════════════════════════════════════════════════════
STATUS: INVESTIGATING | Turn: {case.current_turn}
Path: {case.path_selection.path if case.path_selection else "Determining..."}
═══════════════════════════════════════════════════════════

**PROBLEM**: {case.problem_verification.symptom_statement if case.problem_verification else "Verifying..."}

**MILESTONES**:
{_format_milestones_compact(case.progress)}

**DATA**: {len(case.evidence)} evidence | {len(case.hypotheses)} hypotheses | {len(case.solutions)} solutions

{_format_working_conclusion_compact(case.working_conclusion)}

═══════════════════════════════════════════════════════════
USER MESSAGE
═══════════════════════════════════════════════════════════

{user_message}

═══════════════════════════════════════════════════════════
CORE RULES
═══════════════════════════════════════════════════════════

**Evidence**: Create from objective data only (uploads, command output).
User descriptions → Request actual data.

**Confidence**: <50%="speculate", 50-69%="probably", 70-89%="confident", 90%+="verified"

**Milestones**: Set True only with evidence. Complete multiple per turn if data supports.

**Style**: Acknowledge provided data, then request more if needed.
Never mention: milestones, stages, phases, framework.
"""


def _format_milestones_compact(progress) -> str:
    """Compact milestone display."""

    verification = []
    if progress.symptom_verified:
        verification.append("✅ symptom")
    else:
        verification.append("⏳ symptom")

    if progress.scope_assessed:
        verification.append("✅ scope")
    else:
        verification.append("⏳ scope")

    if progress.timeline_established:
        verification.append("✅ timeline")
    else:
        verification.append("⏳ timeline")

    if progress.changes_identified:
        verification.append("✅ changes")
    else:
        verification.append("⏳ changes")

    investigation = "✅ root_cause" if progress.root_cause_identified else "⏳ root_cause"

    resolution = []
    resolution.append("✅" if progress.solution_proposed else "⏳")
    resolution.append("proposed")
    if progress.solution_applied:
        resolution.append("→ ✅ applied")
    if progress.solution_verified:
        resolution.append("→ ✅ verified")

    return f"""Verify: {' | '.join(verification)}
Investigate: {investigation}
Resolve: {' '.join(resolution)}"""
```

### 2.2 Stage-Specific Sections (Conditionally Included)

```python
def get_stage_section(stage: InvestigationStage, case: Case) -> str:
    """
    Stage-specific instructions - only include for current stage.
    Target: ~400 tokens per stage
    """

    if stage == InvestigationStage.SYMPTOM_VERIFICATION:
        return _get_verification_section(case)
    elif stage == InvestigationStage.HYPOTHESIS_FORMULATION:
        return _get_hypothesis_section(case)
    elif stage == InvestigationStage.HYPOTHESIS_VALIDATION:
        return _get_validation_section(case)
    elif stage == InvestigationStage.SOLUTION:
        return _get_solution_section(case)


def _get_verification_section(case: Case) -> str:
    """Verification stage instructions - checklist format."""

    return """
═══════════════════════════════════════════════════════════
STAGE: SYMPTOM VERIFICATION
═══════════════════════════════════════════════════════════

**Goal**: Confirm problem exists, understand context

**Checklist**:
□ symptom_verified - Error/issue confirmed with logs/metrics?
□ scope_assessed - Who/what affected? (users, services, regions)
□ timeline_established - When started? Still ongoing?
□ changes_identified - Recent deployments, config changes?

**Determine**:
□ temporal_state: ONGOING (happening now) or HISTORICAL (past)
□ urgency_level: CRITICAL / HIGH / MEDIUM / LOW

**Tool Check** (BEFORE requesting user data):
□ Search knowledge base for similar symptoms
□ Check if service had recent issues
□ If tools return no results → Proceed silently (don't mention failure)

**Jump-Ahead**: If evidence reveals root cause → Use Single-Shot Validation

**Evidence Priority** (by diagnostic value, not assumed effort):
1. Error logs/stack traces - Direct indicators of what failed
2. Timeline correlation - What changed before failure?
3. Metrics at failure time - Quantifiable symptoms

Prefer data sources user has already demonstrated access to.
Always offer alternatives: "If you can't access X, Y would also help."
"""


def _get_hypothesis_section(case: Case) -> str:
    """Hypothesis formulation instructions - Single-Shot Validation pattern."""

    return f"""
═══════════════════════════════════════════════════════════
STAGE: ROOT CAUSE IDENTIFICATION
═══════════════════════════════════════════════════════════

✅ Verification complete
Symptom: {case.problem_verification.symptom_statement}
Temporal: {case.problem_verification.temporal_state}
Urgency: {case.problem_verification.urgency_level}

**Decision**: Is root cause OBVIOUS from current evidence?

SINGLE-SHOT VALIDATION (use if ALL true):
□ Single clear error pointing to specific cause
□ Strong timing correlation (change → error within minutes)
□ Mechanism is understandable (you can explain HOW)
□ No conflicting evidence

→ If ALL true, do ALL of the following in ONE turn:
  1. CREATE hypothesis (hypotheses_to_add) with statement = root cause
  2. LINK evidence (hypothesis_evidence_links) with stance = SUPPORTS
  3. SET hypothesis status = VALIDATED
  4. SET root_cause_identified = True
  5. SET root_cause_method = "single_shot_validation"

This preserves the full audit trail (Evidence → Hypothesis → Resolution)
while achieving the same speed as skipping hypothesis generation.

MULTI-HYPOTHESIS TESTING (use if ANY above is false):
→ Generate 2-4 hypotheses across DIFFERENT categories
→ Ensure diversity: CODE, CONFIG, NETWORK, ENVIRONMENT, DATA
→ Each hypothesis needs: evidence_requirements (what to request)
→ Validate/refute based on evidence over multiple turns

**Tool Check** (BEFORE requesting user data):
□ Search KB for this error message / symptom pattern
□ Check documentation for known issues with affected service
□ If tools return no results → Proceed silently (don't mention failure)

**Evidence Request Format**:
"To diagnose this, the most useful would be [PRIMARY].
If that's difficult to obtain, [ALTERNATIVE] would also help.
Why: [diagnostic value]"
"""


def _get_validation_section(case: Case) -> str:
    """Hypothesis validation instructions."""

    active_hyps = [h for h in case.hypotheses.values() if h.status == "ACTIVE"]

    return f"""
═══════════════════════════════════════════════════════════
STAGE: HYPOTHESIS VALIDATION
═══════════════════════════════════════════════════════════

✅ Verification complete
✅ Hypotheses generated

**Active Hypotheses**: {len(active_hyps)}

**Your Task**:
1. When user provides evidence → Evaluate against ALL active hypotheses
2. For each hypothesis, determine:
   - stance: SUPPORTS | NEUTRAL | REFUTES
   - stance_confidence: 0.0-1.0
   - reasoning: Why this evidence has this stance

3. Update hypothesis status based on evidence:
   - SUPPORTS + high confidence → Consider VALIDATED
   - REFUTES + high confidence → Mark REFUTED
   - NEUTRAL → Keep TESTING, request different evidence

**Root Cause Criteria** (when to set root_cause_identified):
□ ONE hypothesis reaches 70%+ confidence
□ Supporting evidence from 2+ independent sources
□ No strong contradicting evidence

**Anchoring Detection**:
If 3+ hypotheses in same category failed → Try different category
If same evidence requested 3+ times → Pivot approach
"""


def _get_solution_section(case: Case) -> str:
    """Solution stage instructions."""

    root_cause = case.root_cause_conclusion.root_cause if case.root_cause_conclusion else "Not identified"
    confidence = case.root_cause_conclusion.confidence_score if case.root_cause_conclusion else 0

    path_guidance = ""
    if case.path_selection and case.path_selection.path == "MITIGATION_FIRST":
        path_guidance = """
**Path: MITIGATION_FIRST**
Focus on immediate_action first (stop the bleeding), then longterm_fix.
"""
    else:
        path_guidance = """
**Path: ROOT_CAUSE**
Provide both immediate_action and longterm_fix for comprehensive solution.
"""

    return f"""
═══════════════════════════════════════════════════════════
STAGE: SOLUTION
═══════════════════════════════════════════════════════════

✅ Verification complete
✅ Root cause identified

**Root Cause**: {root_cause}
**Confidence**: {confidence * 100:.0f}%
{path_guidance}

**Solution Checklist**:
□ solution_proposed - Provide: title, type, immediate_action, implementation_steps
□ solution_applied - User confirms they applied it
□ solution_verified - Metrics/logs confirm fix worked

**Solution Format**:
{{
  "title": "Brief description",
  "solution_type": "rollback|config_change|code_fix|restart|scaling|other",
  "immediate_action": "What to do NOW (specific commands)",
  "longterm_fix": "How to prevent recurrence",
  "implementation_steps": ["Step 1...", "Step 2..."],
  "risks": ["Potential side effect...", "Rollback plan..."]
}}

**Verification Criteria** (adapt to problem type):
- Crash/Error: Error rate returns to 0% for 10+ minutes
- Performance: Latency returns to baseline for 15+ minutes
- Intermittent: No recurrence for 30+ minutes
- Data issue: Data integrity confirmed

If verified → outcome = "case_resolved"
"""
```

### 2.3 Expanded Sections (Included When Relevant)

```python
def get_expanded_sections(case: Case) -> List[str]:
    """
    Expanded sections - only included when relevant.
    Reduces prompt size for simple cases.
    """

    sections = []

    # Include hypothesis evaluation guidance if hypotheses exist
    if case.hypotheses and len(case.hypotheses) > 0:
        sections.append(get_hypothesis_evaluation_guidance())

    # Include degraded mode if active
    if case.degraded_mode:
        sections.append(get_degraded_mode_section(case.degraded_mode))

    # Include stall warning if approaching stall
    if case.turns_without_progress >= 2:
        sections.append(get_stall_warning(case.turns_without_progress))

    # Include tool reminder if no tool usage in last 3 turns
    if should_remind_tool_usage(case):
        sections.append(get_tool_reminder())

    return sections


def get_hypothesis_evaluation_guidance() -> str:
    """Detailed hypothesis evaluation - only when hypotheses exist."""

    return """
═══════════════════════════════════════════════════════════
HYPOTHESIS EVALUATION GUIDE
═══════════════════════════════════════════════════════════

When user provides CAUSAL evidence, evaluate against ALL active hypotheses:

For EACH hypothesis:
{
  "hypothesis_id": "hyp_xxx",
  "stance": "SUPPORTS | NEUTRAL | REFUTES",
  "stance_confidence": 0.0-1.0,
  "reasoning": "Why this evidence has this stance for THIS hypothesis"
}

**Key**: One piece of evidence can have DIFFERENT stances for different hypotheses!

Example:
Evidence: "Connection pool at 95% capacity"
- Hypothesis A (pool exhaustion): SUPPORTS (0.85)
- Hypothesis B (memory leak): NEUTRAL (0.3) - doesn't test this
- Hypothesis C (slow queries): SUPPORTS (0.6) - could cause pool buildup
"""


def get_tool_reminder() -> str:
    """Remind about tool usage if not used recently, with negative constraints."""

    return """
═══════════════════════════════════════════════════════════
💡 TOOL REMINDER
═══════════════════════════════════════════════════════════

Before requesting more data from user, consider using available tools:

□ Knowledge Base: Search for similar symptoms, past cases, runbooks
□ Document QA: Query system documentation for configurations
□ Web Search: Check for known issues with external services (if enabled)

These may have the answer without requiring user to fetch data.

**CRITICAL: Silence on No Results**

If tool searches return NO RESULTS or LOW CONFIDENCE (<50%):
❌ DON'T say: "I checked the knowledge base but found nothing."
❌ DON'T say: "Unfortunately, our documentation doesn't cover this."

✅ DO: Proceed silently to the next step (evidence request, hypothesis)
✅ DO: Only mention tool results when they ARE helpful

Exception: If user explicitly asks "Is this a known issue?", you may say no matches found.
"""
```

### 2.4 Condensed Output Format

```python
def get_output_format_condensed() -> str:
    """Condensed output format instructions."""

    return """
═══════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════

{
  "agent_response": "<natural response>",
  "state_updates": {
    "milestones": {"<milestone>": true, ...} or null,
    "verification_updates": {...} or null,
    "evidence_to_add": [{summary, analysis, tests_hypothesis_id?, stance?}] or [],
    "hypotheses_to_add": [...] or [],
    "hypothesis_evidence_links": [...] or [],
    "solutions_to_add": [...] or [],
    "working_conclusion": {statement, confidence, reasoning, caveats} or null,
    "root_cause_conclusion": {...} or null,
    "outcome": "milestone_completed|data_provided|data_requested|hypothesis_tested|case_resolved|conversation|other"
  }
}

Only include fields that CHANGE. Be opportunistic - complete everything data supports.
"""
```

---

## 3. Complete Prompt Assembly with Dynamic Token Budgeting

```python
def build_enhanced_investigating_prompt(case: Case, user_message: str) -> str:
    """
    Assemble complete INVESTIGATING prompt using tiered system with
    dynamic token budgeting based on user message size.

    Token budget (baseline):
    - Core: ~800 tokens
    - Stage-specific: ~400 tokens
    - Expanded (conditional): 0-500 tokens
    - Output format: ~200 tokens

    Total: 1400-1900 tokens (vs. ~3000 in v2.0)

    Dynamic adjustment:
    - Large user message (>1500 tokens): Skip expanded sections
    - Very large user message (>3000 tokens): Use condensed core/stage
    """

    # Calculate available budget based on user message size
    user_message_tokens = estimate_tokens(user_message)
    budget = calculate_prompt_budget(user_message_tokens)

    sections = []

    # CORE (always, but may use condensed version)
    if budget.use_condensed_core:
        sections.append(build_investigating_core_condensed(case, user_message))
    else:
        sections.append(build_investigating_core(case, user_message))

    # STAGE-SPECIFIC (current stage only)
    stage = case.progress.current_stage
    if budget.use_condensed_stage:
        sections.append(get_stage_section_condensed(stage, case))
    else:
        sections.append(get_stage_section(stage, case))

    # EXPANDED (conditional, budget-aware)
    if budget.allows_expanded:
        expanded = get_expanded_sections(case)
        sections.extend(expanded)

    # OUTPUT FORMAT (always, condensed)
    sections.append(get_output_format_condensed())

    return "\n".join(sections)


@dataclass
class PromptBudget:
    """Dynamic budget based on user message size."""

    allows_expanded: bool = True
    use_condensed_core: bool = False
    use_condensed_stage: bool = False


def calculate_prompt_budget(user_message_tokens: int) -> PromptBudget:
    """
    Calculate available prompt budget based on user message size.

    When user pastes a large log file (e.g., 2000+ tokens), collapse
    expanded sections to prevent context window overflow.
    """

    budget = PromptBudget()

    # Large user message: skip expanded sections
    if user_message_tokens > 1500:
        budget.allows_expanded = False

    # Very large user message: use condensed everything
    if user_message_tokens > 3000:
        budget.use_condensed_core = True
        budget.use_condensed_stage = True

    return budget


def estimate_tokens(text: str) -> int:
    """Estimate token count (rough approximation: 4 chars = 1 token)."""
    return len(text) // 4
```

---

## 4. Migration Notes

### 4.1 Backward Compatibility

The enhanced templates maintain backward compatibility with existing:
- InvestigationResponse schema (new fields are Optional)
- State management logic
- Milestone system

New fields (preliminary_urgency, knowledge_match) are additive.

### 4.2 Gradual Rollout

Recommended rollout:
1. Deploy tiered prompt system (immediate token savings)
2. Add knowledge pre-check infrastructure
3. Enable preliminary urgency assessment
4. Monitor and tune based on metrics

### 4.3 Testing Checklist

- [ ] Verify token count reduction (target: 40-50% reduction)
- [ ] Test knowledge pre-check accuracy
- [ ] Validate semantic urgency assessment (not keyword-based)
- [ ] Confirm milestone completion still works correctly
- [ ] Test Single-Shot Validation creates hypothesis record (audit trail)
- [ ] Test Fast-Track resolution (INQUIRY → RESOLVED)
- [ ] Verify tool silence on no results (negative constraints)
- [ ] Test dynamic token budgeting with large log pastes
- [ ] Verify degraded mode triggers appropriately

### 4.4 Key Design Decisions Summary

| Decision | Rationale |
|----------|-----------|
| Single-Shot Validation (not skip) | Preserves audit trail: Evidence → Hypothesis → Resolution |
| Semantic urgency (not keywords) | Prevents false positives ("not down" ≠ CRITICAL) |
| Context-aware evidence requests | Effort varies by org; prefer what user has access to |
| Silence on tool failure | Reduces noise, maintains user confidence |
| Fast-Track INQUIRY → RESOLVED | Keeps metrics clean for known issues |
| Dynamic token budgeting | Handles large log pastes gracefully |

---

**END OF DOCUMENT**
