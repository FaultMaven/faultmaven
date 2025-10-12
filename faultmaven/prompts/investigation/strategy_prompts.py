"""Strategy-Specific Prompt Guidance

This module provides strategy-specific guidance for Active Incident vs Post-Mortem investigations.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from typing import Dict, Any
from faultmaven.models.investigation import InvestigationStrategy


# =============================================================================
# Active Incident Strategy Guidance
# =============================================================================

ACTIVE_INCIDENT_GUIDANCE = """
# Active Incident Investigation Strategy

## Primary Goal: Rapid Mitigation

You are investigating a live incident. Speed matters:
- **Mitigate first, deep-dive later**
- **70% confidence is sufficient to proceed**
- **Practical evidence over perfect evidence**
- **Can skip phases if critical urgency**

## Decision Making

**Hypothesis Validation**: Accept 70%+ confidence
- Don't need exhaustive testing
- Practical tests that confirm/refute quickly
- Move forward with "good enough" certainty

**Evidence Gathering**: Sufficient, not comprehensive
- Focus on evidence that changes decisions
- Skip "nice to have" evidence if time-pressured
- Accept reasonable inferences from partial data

**Phase Progression**: Flexible
- Can skip Hypothesis phase (3) if obvious cause
- Can skip deep Validation (4) if urgent mitigation needed
- Must still validate solution works (Phase 5)

## Time Pressure

If critical urgency:
- Spend max 5-10 minutes per phase
- Make decisions with available evidence
- Document what you skipped for post-mortem

## After Mitigation

Once problem is mitigated:
**Offer Post-Mortem**: "Your service is restored. Would you like me to conduct a thorough post-mortem to understand the complete root cause and prevent recurrence?"

## Example Approach

**Fast Path** (Critical Urgency):
Phase 0: Intake (confirm problem)
Phase 1: Blast Radius (quick scope check)
Phase 2: Timeline (when/what changed?)
Phase 5: Solution (apply likely fix, verify)

**Standard Path** (High Urgency):
Phase 0: Intake
Phase 1: Blast Radius
Phase 2: Timeline
Phase 3: Hypothesis (2-3 theories)
Phase 4: Validation (test top hypothesis)
Phase 5: Solution
"""


# =============================================================================
# Post-Mortem Strategy Guidance
# =============================================================================

POST_MORTEM_GUIDANCE = """
# Post-Mortem Investigation Strategy

## Primary Goal: Complete Root Cause Analysis

You are conducting thorough analysis. Accuracy matters:
- **Understand fully, not just fix**
- **85%+ confidence required for conclusions**
- **Comprehensive evidence gathering**
- **Never skip phases**

## Decision Making

**Hypothesis Validation**: Require 85%+ confidence
- Test hypotheses exhaustively
- Don't accept "probably" - need "definitely"
- Gather multiple evidence types to confirm

**Evidence Gathering**: Comprehensive
- Collect all relevant evidence
- Don't skip evidence even if hypothesis seems obvious
- Document evidence chain carefully

**Phase Progression**: Sequential
- Complete all phases 0-6
- Never skip phases regardless of urgency
- Take time needed for thoroughness

## Time Investment

No time pressure:
- Spend time needed per phase
- Re-visit earlier phases if new evidence emerges
- Multiple OODA iterations per phase is normal

## Documentation Required

Must complete Phase 6:
- Generate comprehensive case report
- Create runbook for future incidents
- Document prevention strategies

## Example Approach

**Full Investigation** (All Cases):
Phase 0: Intake (comprehensive problem understanding)
Phase 1: Blast Radius (complete scope analysis)
Phase 2: Timeline (detailed change history)
Phase 3: Hypothesis (3-5 theories, ranked)
Phase 4: Validation (test all hypotheses systematically)
Phase 5: Solution (implement with verification)
Phase 6: Document (case report + runbook)

## Quality Standards

**Evidence Chain**: Every conclusion backed by evidence
**Alternative Hypotheses**: Must consider and refute alternatives
**Confidence Scoring**: Track and justify confidence levels
**Prevention Focus**: Identify how to prevent recurrence
"""


# =============================================================================
# Strategy Selector
# =============================================================================


def get_strategy_specific_guidance(
    strategy: InvestigationStrategy,
    current_phase: int = None,
    urgency_level: str = "medium",
) -> str:
    """Get strategy-specific guidance for current context

    Args:
        strategy: Investigation strategy
        current_phase: Optional current phase number
        urgency_level: Current urgency level

    Returns:
        Strategy-specific guidance string
    """
    if strategy == InvestigationStrategy.ACTIVE_INCIDENT:
        base_guidance = ACTIVE_INCIDENT_GUIDANCE

        # Add urgency-specific guidance
        if urgency_level == "critical":
            base_guidance += """

## CRITICAL URGENCY OVERRIDE

Service is down. Use fast path:
1. Quick scope check (Phase 1) - 2 min
2. When did it start? (Phase 2) - 2 min
3. Most likely cause from timeline? - 1 min
4. Apply fix (Phase 5) - implement immediately
5. Verify resolution

Skip deep hypothesis generation and testing. Fix first, RCA later.
"""

        return base_guidance

    else:  # POST_MORTEM
        return POST_MORTEM_GUIDANCE


# =============================================================================
# Strategy Transition Messages
# =============================================================================


STRATEGY_TRANSITION_MESSAGES = {
    "active_to_postmortem": """
# Transitioning to Post-Mortem Analysis

The immediate issue has been mitigated. Now let's conduct a thorough root cause analysis to:
1. Fully understand what happened
2. Identify all contributing factors
3. Prevent recurrence

This will be more rigorous than the initial incident response. I'll:
- Re-examine evidence systematically
- Test alternative hypotheses
- Require higher confidence (85%+)
- Generate comprehensive documentation

Ready to begin the post-mortem?
""",

    "postmortem_to_active": """
# Urgency Escalated - Switching to Active Incident Mode

A critical issue has emerged. Pausing thorough analysis to focus on rapid mitigation.

New priority: Stop the bleeding first, complete RCA later.

What's the most critical symptom right now?
""",
}


def get_strategy_transition_message(
    from_strategy: InvestigationStrategy,
    to_strategy: InvestigationStrategy,
) -> str:
    """Get transition message when strategy changes

    Args:
        from_strategy: Current strategy
        to_strategy: New strategy

    Returns:
        Transition message
    """
    if from_strategy == InvestigationStrategy.ACTIVE_INCIDENT and to_strategy == InvestigationStrategy.POST_MORTEM:
        return STRATEGY_TRANSITION_MESSAGES["active_to_postmortem"]
    elif from_strategy == InvestigationStrategy.POST_MORTEM and to_strategy == InvestigationStrategy.ACTIVE_INCIDENT:
        return STRATEGY_TRANSITION_MESSAGES["postmortem_to_active"]
    else:
        return ""
