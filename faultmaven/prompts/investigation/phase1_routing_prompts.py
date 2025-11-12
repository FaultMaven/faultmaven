"""Phase 1 Routing Confirmation Prompts (v3.0)

Prompts for user confirmation when Phase 1 detects critical/high urgency incidents.
Agent must get explicit user confirmation before routing to Phase 5 (fast recovery)
vs. Phase 2 (full investigation).

Design Reference:
- docs/architecture/prompt-engineering-architecture.md (v3.0 Section 2.2)
- docs/architecture/investigation-phases-and-ooda-integration.md (v3.0 Phase 1)
"""

from faultmaven.models.investigation import InvestigationState, AnomalyFrame


def get_routing_confirmation_prompt(
    investigation_state: InvestigationState,
    urgency_level: str,
) -> str:
    """Get routing confirmation prompt based on urgency level

    Args:
        investigation_state: Current investigation state
        urgency_level: "critical" or "high"

    Returns:
        Routing confirmation prompt
    """
    anomaly_frame = investigation_state.ooda_engine.anomaly_frame

    if urgency_level == "critical":
        return _get_critical_routing_prompt(anomaly_frame)
    elif urgency_level == "high":
        return _get_high_urgency_routing_prompt(anomaly_frame)
    else:
        return ""  # No routing confirmation needed for medium/low


def _get_critical_routing_prompt(anomaly_frame: AnomalyFrame) -> str:
    """Routing prompt for CRITICAL urgency (suggest fast recovery)"""
    affected = anomaly_frame.affected_scope if anomaly_frame else "Unknown scope"
    symptoms = anomaly_frame.symptoms if anomaly_frame else "Unknown symptoms"

    return f"""# ⚠️ CRITICAL INCIDENT DETECTED - Routing Decision Required

## Blast Radius Assessment Complete

**Affected Scope**: {affected}
**Symptoms**: {', '.join(symptoms) if isinstance(symptoms, list) else symptoms}
**Urgency Level**: CRITICAL

## Investigation Path Decision

I've completed the blast radius assessment. Given the critical severity, I recommend:

### Option 1: **FAST RECOVERY** (Recommended for Critical)
- **Path**: Skip to immediate mitigation (Phase 5)
- **Timeline**: Fastest path to service restoration
- **Approach**: Apply safe mitigation based on blast radius assessment
  - Rollback recent changes
  - Scale resources
  - Reroute traffic
  - Disable failing component
- **Trade-off**: Root cause not validated (will recommend full RCA after recovery)
- **Risk**: Mitigation may not work if actual cause differs from assessment
- **Best for**: Service down, customers impacted, every minute counts

### Option 2: **FULL INVESTIGATION**
- **Path**: Continue systematic investigation (Phases 2 → 3 → 4 → 5)
- **Timeline**: 30-60 minutes for validated root cause
- **Approach**: Timeline → Hypothesis → Validation → Targeted fix
- **Trade-off**: Takes longer, but root cause validated (≥70% confidence)
- **Best for**: Issue contained, can afford time for thorough analysis

## Your Decision

**Which path do you want to take?**

Type:
- **"fast recovery"** or **"skip to solution"** → I'll propose immediate mitigation
- **"full investigation"** or **"find root cause"** → I'll continue systematic troubleshooting

If you're unsure, I recommend **fast recovery** for critical incidents. We can always do a thorough post-mortem after service is restored.

**What would you like to do?**
"""


def _get_high_urgency_routing_prompt(anomaly_frame: AnomalyFrame) -> str:
    """Routing prompt for HIGH urgency (offer both options equally)"""
    affected = anomaly_frame.affected_scope if anomaly_frame else "Unknown scope"
    symptoms = anomaly_frame.symptoms if anomaly_frame else "Unknown symptoms"

    return f"""# ⚠️ HIGH URGENCY INCIDENT - Routing Decision Required

## Blast Radius Assessment Complete

**Affected Scope**: {affected}
**Symptoms**: {', '.join(symptoms) if isinstance(symptoms, list) else symptoms}
**Urgency Level**: HIGH

## Investigation Path Decision

I've completed the blast radius assessment. The urgency is high, so I want to confirm your preference:

### Option 1: **FAST RECOVERY** (Quick Mitigation)
- **Path**: Skip to immediate mitigation (Phase 5)
- **Timeline**: 5-15 minutes to apply mitigation
- **Approach**: Best-effort mitigation based on blast radius
  - Target common causes for this symptom pattern
  - Apply safe interventions (rollback, scale, reroute)
  - Monitor for effectiveness
- **Confidence**: ~40-50% (not validated root cause)
- **Risk**: May need multiple attempts if first mitigation doesn't work
- **Follow-up**: Recommend full RCA after stabilization
- **Best for**: Service degraded, customer complaints, want quick action

### Option 2: **FULL INVESTIGATION** (Validated Root Cause)
- **Path**: Continue systematic investigation (Phases 2 → 3 → 4 → 5)
- **Timeline**: 20-45 minutes for validated root cause
- **Approach**: Timeline → Hypothesis → Validation → Targeted fix
- **Confidence**: ≥70% (validated before solution)
- **Benefit**: Higher likelihood of correct fix on first try
- **Best for**: Can afford investigation time, want confident solution

## Your Decision

**Which approach fits your situation better?**

Type:
- **"fast recovery"** → Quick mitigation, accept moderate confidence
- **"full investigation"** → Systematic troubleshooting, high confidence solution

Both are valid choices for high-urgency incidents. Your call based on business impact and time constraints.

**What would you prefer?**
"""


def parse_user_routing_response(user_response: str) -> tuple[str, bool]:
    """Parse user's routing decision from response

    Args:
        user_response: User's response to routing prompt

    Returns:
        Tuple of (routing_decision, is_ambiguous)
        - routing_decision: "fast_recovery", "full_investigation", or "ambiguous"
        - is_ambiguous: True if response unclear
    """
    response_lower = user_response.lower().strip()

    # Fast recovery keywords
    fast_keywords = [
        "fast recovery", "skip to solution", "skip", "mitigation", "quick fix",
        "immediate", "urgent", "asap", "now", "fast", "quick"
    ]

    # Full investigation keywords
    full_keywords = [
        "full investigation", "find root cause", "root cause", "rca",
        "systematic", "thorough", "investigate", "timeline", "hypothesis"
    ]

    # Check for fast recovery indicators
    if any(keyword in response_lower for keyword in fast_keywords):
        return "fast_recovery", False

    # Check for full investigation indicators
    if any(keyword in response_lower for keyword in full_keywords):
        return "full_investigation", False

    # Check for yes/no responses (context-dependent)
    if response_lower in ["yes", "y", "ok", "sure", "proceed"]:
        # Assume user agrees with recommendation (which is context-dependent)
        # This should be used with context from previous prompt
        return "ambiguous", True

    if response_lower in ["no", "n"]:
        return "ambiguous", True

    # Couldn't determine clear preference
    return "ambiguous", True


def get_ambiguous_response_clarification() -> str:
    """Get clarification prompt when user response is ambiguous"""
    return """I'm not sure which path you'd like to take. Could you clarify?

Please respond with one of:
- **"fast recovery"** - Skip to immediate mitigation (faster, lower confidence)
- **"full investigation"** - Continue systematic troubleshooting (slower, high confidence)

Or just tell me in your own words what you'd prefer. For example:
- "Let's try to fix it quickly" → fast recovery
- "I want to understand the root cause first" → full investigation
"""


def get_routing_decision_confirmation(routing_decision: str, anomaly_frame: AnomalyFrame) -> str:
    """Get confirmation message after routing decision made

    Args:
        routing_decision: "fast_recovery" or "full_investigation"
        anomaly_frame: Current anomaly frame

    Returns:
        Confirmation message
    """
    affected = anomaly_frame.affected_scope if anomaly_frame else "Unknown scope"

    if routing_decision == "fast_recovery":
        return f"""✅ **Confirmed: Fast Recovery Path**

I'll proceed directly to mitigation (Phase 5) based on blast radius assessment.

**Next steps**:
1. Propose safe mitigation targeting likely cause
2. Guide implementation
3. Monitor for effectiveness
4. Adjust if first mitigation doesn't resolve

**Note**: Root cause not validated. I'll recommend full RCA after service restoration.

Proceeding with fast recovery for: {affected}
"""
    else:  # full_investigation
        return f"""✅ **Confirmed: Full Investigation Path**

I'll continue systematic investigation to validate root cause before solution.

**Next steps**:
1. Phase 2 (Timeline): When did symptoms start?
2. Phase 3 (Hypothesis): Generate root cause theories
3. Phase 4 (Validation): Test hypotheses with evidence
4. Phase 5 (Solution): Apply validated fix

**Benefit**: High-confidence solution (≥70%) reduces risk of incorrect fix.

Proceeding with full investigation for: {affected}
"""
