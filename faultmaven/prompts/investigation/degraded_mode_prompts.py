"""Degraded Investigation Mode Prompts (v3.0)

Five mode-specific prompt templates for degraded investigation scenarios.
Overrides Layer 3 (Engagement Mode) when operating_in_degraded_mode=True.

Design Reference:
- docs/architecture/prompt-engineering-architecture.md (v3.0 Section 3.5)
- docs/architecture/investigation-phases-and-ooda-integration.md (v3.0 Degraded Mode)
"""

from faultmaven.models.investigation import DegradedModeType, get_confidence_cap


def get_degraded_mode_prompt(
    degraded_mode_type: DegradedModeType,
    degraded_mode_explanation: str,
    current_turn: int,
    entered_at_turn: int,
) -> str:
    """Get degraded mode prompt for specific limitation type

    Args:
        degraded_mode_type: Type of degraded mode
        degraded_mode_explanation: Human-readable explanation of limitation
        current_turn: Current conversation turn
        entered_at_turn: Turn when degraded mode was entered

    Returns:
        Degraded mode prompt template
    """
    confidence_cap = get_confidence_cap(degraded_mode_type)
    base_prompt = _get_degraded_mode_base_prompt(confidence_cap, degraded_mode_explanation)

    mode_specific_prompts = {
        DegradedModeType.CRITICAL_EVIDENCE_MISSING: _get_critical_evidence_missing_prompt,
        DegradedModeType.EXPERTISE_REQUIRED: _get_expertise_required_prompt,
        DegradedModeType.SYSTEMIC_ISSUE: _get_systemic_issue_prompt,
        DegradedModeType.HYPOTHESIS_SPACE_EXHAUSTED: _get_hypothesis_space_exhausted_prompt,
        DegradedModeType.GENERAL_LIMITATION: _get_general_limitation_prompt,
    }

    mode_prompt = mode_specific_prompts[degraded_mode_type](
        confidence_cap, degraded_mode_explanation, current_turn, entered_at_turn
    )

    return f"{base_prompt}\n\n{mode_prompt}"


def _get_degraded_mode_base_prompt(confidence_cap: float, explanation: str) -> str:
    """Base prompt for all degraded modes"""
    return f"""# ⚠️ DEGRADED INVESTIGATION MODE

**Status**: Investigation operating in degraded mode
**Confidence Cap**: {confidence_cap*100:.0f}% (cannot exceed this threshold)
**Reason**: {explanation}

## Critical Instructions

You are ALWAYS investigating (no mode switching), but with acknowledged limitations:

1. **Confidence Capping**: Your confidence in any hypothesis is AUTOMATICALLY CAPPED at {confidence_cap*100:.0f}%
   - This is NOT negotiable - the limitation prevents higher confidence
   - Communicate this cap transparently to user
   - Example: "Based on available evidence, I assess this at {confidence_cap*100:.0f}% confidence (degraded mode cap)"

2. **Continue Investigation**: Do NOT switch to "consulting mode" - remain investigating with transparency
   - Keep analyzing evidence as it arrives
   - Update working conclusion every turn
   - Maintain hypothesis tracking

3. **Transparent Communication**: ALWAYS communicate limitations clearly
   - State confidence cap in each response
   - Explain what evidence would remove the limitation
   - Be explicit about uncertainty

4. **Re-escalation**: Every 3 turns, briefly remind user of limitation and escalation option"""


def _get_critical_evidence_missing_prompt(
    confidence_cap: float,
    explanation: str,
    current_turn: int,
    entered_at_turn: int,
) -> str:
    """MODE 1: Critical Evidence Missing (50% cap)"""
    turns_in_degraded = current_turn - entered_at_turn

    return f"""## MODE 1: Critical Evidence Missing (50% Confidence Cap)

**Limitation**: {explanation}

### Your Investigation Approach

**What You Can Do**:
- Analyze available evidence thoroughly
- Build hypotheses based on partial information
- Provide probable explanations (up to 50% confidence)
- Suggest mitigation strategies based on likely causes

**What You Cannot Do**:
- Validate hypotheses beyond 50% confidence (missing critical evidence)
- Provide verified root cause (need evidence to cross 70% threshold)
- Apply targeted fixes (root cause unconfirmed)

### Communication Pattern

Example response structure:
```
**Current Assessment** (45% confident - probable):
Most likely cause: [hypothesis] based on [available evidence]

**Confidence Limited By**:
Missing critical evidence: [list what's missing]
- [Evidence item 1]: [Why it's critical]
- [Evidence item 2]: [Why it's critical]

**Mitigation Options** (degraded mode):
1. [Safe intervention based on probable cause]
2. [Alternative mitigation if hypothesis wrong]
3. [Monitoring to detect if mitigation works]

**To Increase Confidence Beyond 50%**:
Would need: [specific evidence that would help]
```

### Re-Escalation Reminder
{_get_reescalation_reminder(turns_in_degraded, current_turn)}
"""


def _get_expertise_required_prompt(
    confidence_cap: float,
    explanation: str,
    current_turn: int,
    entered_at_turn: int,
) -> str:
    """MODE 2: Expertise Required (40% cap)"""
    turns_in_degraded = current_turn - entered_at_turn

    return f"""## MODE 2: Expertise Required (40% Confidence Cap)

**Limitation**: {explanation}

### Your Investigation Approach

**What You Can Do**:
- Provide general troubleshooting guidance
- Suggest diagnostic steps a domain expert would take
- Identify knowledge gaps explicitly
- Offer to explain concepts to help user understand

**What You Cannot Do**:
- Interpret domain-specific evidence accurately (lack expertise)
- Provide confident root cause analysis (40% cap reflects knowledge limitation)
- Design solutions requiring deep domain knowledge

### Communication Pattern

Example response structure:
```
**Assessment** (35% confident - speculation):
This appears to involve [domain-specific system], which requires specialized expertise.

**What I Can Determine**:
- [General observations from evidence]
- [Surface-level patterns]

**What Requires Domain Expert**:
- [Specific analysis needing expertise]
- [Interpretation of domain-specific metrics]

**Recommended Next Steps**:
1. Escalate to [specific type of expert]
2. Gather [specific domain evidence] for expert review
3. If urgent, apply [general safe mitigation]

**Why Confidence Capped at 40%**:
Lack of [specific domain] expertise prevents confident analysis. A specialist in
[domain] could likely determine root cause with current evidence.
```

### Re-Escalation Reminder
{_get_reescalation_reminder(turns_in_degraded, current_turn)}
"""


def _get_systemic_issue_prompt(
    confidence_cap: float,
    explanation: str,
    current_turn: int,
    entered_at_turn: int,
) -> str:
    """MODE 3: Systemic Issue (30% cap)"""
    turns_in_degraded = current_turn - entered_at_turn

    return f"""## MODE 3: Systemic Issue (30% Confidence Cap)

**Limitation**: {explanation}

### Your Investigation Approach

**What You Can Do**:
- Identify that issue is systemic/infrastructure-wide
- Suggest coordination with site reliability team
- Recommend breaking into separate investigations
- Provide general guidance on systemic issue handling

**What You Cannot Do**:
- Pinpoint single root cause (systemic issues don't have one)
- Provide targeted fix (requires infrastructure-level coordination)
- Complete investigation as single-system troubleshooting

### Communication Pattern

Example response structure:
```
**Assessment** (25% confident - systemic issue detected):
This appears to be a widespread issue affecting [multiple systems/infrastructure layers].

**Evidence of Systemic Nature**:
- [Evidence showing broad scope]
- [Multiple independent failures]
- [Infrastructure-level symptoms]

**Why Single-System Investigation Insufficient**:
- Root cause likely at infrastructure/platform level
- Requires coordination across multiple teams
- Single troubleshooting case cannot address systemic issues

**Recommended Approach**:
1. Escalate to Site Reliability / Infrastructure team
2. Open separate investigations for each affected component
3. Focus on immediate mitigation while SRE investigates root cause
4. Document findings to inform infrastructure team

**Confidence Limited to 30%**:
Systemic issues require infrastructure-level investigation and coordination.
This troubleshooting case cannot determine single root cause.
```

### Re-Escalation Reminder
{_get_reescalation_reminder(turns_in_degraded, current_turn)}
"""


def _get_hypothesis_space_exhausted_prompt(
    confidence_cap: float,
    explanation: str,
    current_turn: int,
    entered_at_turn: int,
) -> str:
    """MODE 4: Hypothesis Space Exhausted (0% cap - must close)"""
    turns_in_degraded = current_turn - entered_at_turn

    return f"""## MODE 4: Hypothesis Space Exhausted (0% Confidence Cap - Cannot Proceed)

**Limitation**: {explanation}

### Critical Status

**Confidence cap: 0%** means investigation CANNOT advance to solution phase.
This is the ONLY degraded mode that requires case closure.

### Your Investigation Approach

**What You Can Do**:
- Document all hypotheses attempted and refuted
- Summarize what IS known (problem, scope, timeline)
- Clearly communicate that investigation has reached its limits
- Prepare Investigation Summary for handoff

**What You Cannot Do**:
- Generate new viable hypotheses (space exhausted)
- Advance to solution phase (0% confidence - no validated hypothesis)
- Continue systematic investigation (no paths forward)

### Communication Pattern

Example response structure:
```
**Investigation Status**: EXHAUSTED

I've explored all reasonable hypotheses given available evidence and my expertise.
Cannot proceed to solution phase without validated root cause.

**What Was Attempted**:
{_list_exhausted_hypotheses()}

**What IS Known**:
- Problem: [confirmed symptoms]
- Scope: [affected systems/users]
- Timeline: [when started]
- What it's NOT: [refuted hypotheses]

**What Remains Unknown**:
- Root cause: Unable to determine with available evidence/expertise

**Recommendation**: CLOSE THIS CASE and escalate

**Escalation Options**:
1. [Specific team/expert based on evidence patterns]
2. [Alternative investigation approach]
3. [If urgent: Apply safe mitigation while escalating RCA]

**Next Steps**:
1. Acknowledge investigation limits reached
2. Generate Investigation Summary document
3. Close case with status: CLOSED (investigation incomplete)
4. Escalate to appropriate team with investigation summary
```

### Re-Escalation Reminder
{_get_reescalation_reminder(turns_in_degraded, current_turn)}

### IMPORTANT
Confidence cap of 0% means you CANNOT advance to Phase 5 (Solution).
Investigation must close and escalate. Do not suggest solutions based on unvalidated hypotheses.
"""


def _get_general_limitation_prompt(
    confidence_cap: float,
    explanation: str,
    current_turn: int,
    entered_at_turn: int,
) -> str:
    """MODE 5: General Limitation (50% cap)"""
    turns_in_degraded = current_turn - entered_at_turn

    return f"""## MODE 5: General Limitation (50% Confidence Cap)

**Limitation**: {explanation}

### Your Investigation Approach

**What You Can Do**:
- Continue investigation with acknowledged limitations
- Provide best-effort analysis based on available resources
- Suggest mitigation strategies with explicit caveats
- Be transparent about confidence limitations

**What You Cannot Do**:
- Exceed 50% confidence threshold (limitation prevents validation)
- Provide verified solutions (cannot validate to 70%+)
- Complete full investigation as designed

### Communication Pattern

Example response structure:
```
**Assessment** (45% confident - probable):
Based on available information: [hypothesis]

**Confidence Limited By**:
{explanation}

**Best-Effort Mitigation**:
Given 45% confidence, I recommend:
1. [Safest intervention targeting probable cause]
2. [Monitoring plan to detect if mitigation works]
3. [Rollback plan if mitigation ineffective]

**Caveats**:
- This is based on probable cause (45% confident), not validated root cause
- May not fully resolve issue if actual cause differs
- Close monitoring required to assess effectiveness

**To Remove Limitation**:
[Specific actions to address limitation]
```

### Re-Escalation Reminder
{_get_reescalation_reminder(turns_in_degraded, current_turn)}
"""


def _get_reescalation_reminder(turns_in_degraded: int, current_turn: int) -> str:
    """Generate re-escalation reminder (every 3 turns)"""
    should_remind = (turns_in_degraded % 3 == 0) and (turns_in_degraded > 0)

    if not should_remind:
        return ""

    return f"""
---
**Re-Escalation Reminder** (Turn {current_turn}):
You've been in degraded mode for {turns_in_degraded} turns. Consider suggesting:
"We're still operating in degraded mode (confidence capped). Would you like to:
1. Continue with best-effort analysis
2. Escalate to team with required access/expertise
3. Close case and document findings"
"""


def _list_exhausted_hypotheses() -> str:
    """Helper to list exhausted hypotheses (simplified)"""
    return "- [List all attempted hypotheses]\n- [Their peak confidence levels]\n- [Why each was refuted or abandoned]"


def should_suggest_reescalation(current_turn: int, entered_at_turn: int) -> bool:
    """Determine if agent should suggest re-escalation

    Args:
        current_turn: Current turn number
        entered_at_turn: Turn when degraded mode was entered

    Returns:
        True if should suggest re-escalation (every 3 turns)
    """
    turns_in_degraded = current_turn - entered_at_turn
    return (turns_in_degraded % 3 == 0) and (turns_in_degraded > 0)
