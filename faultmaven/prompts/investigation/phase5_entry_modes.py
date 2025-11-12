"""Phase 5 Entry Mode Prompt Templates (v3.0)

Three mode-specific prompts for Phase 5 (Solution) based on how agent entered phase.
Provides appropriate context and guidance for each entry scenario.

Design Reference:
- docs/architecture/prompt-engineering-architecture.md (v3.0 Section 2.6)
- docs/architecture/investigation-phases-and-ooda-integration.md (v3.0 Phase 5)
"""

from faultmaven.models.investigation import InvestigationState


def get_phase5_entry_mode_context(
    entry_mode: str,
    investigation_state: InvestigationState,
) -> str:
    """Get Phase 5 entry mode context for prompt assembly

    Args:
        entry_mode: 'normal', 'fast_recovery', or 'degraded'
        investigation_state: Current investigation state

    Returns:
        Entry mode context string to inject into Phase 5 prompts
    """
    mode_templates = {
        'normal': _get_normal_entry_context,
        'fast_recovery': _get_fast_recovery_entry_context,
        'degraded': _get_degraded_entry_context,
    }

    template_fn = mode_templates.get(entry_mode, _get_normal_entry_context)
    return template_fn(investigation_state)


def _get_normal_entry_context(investigation_state: InvestigationState) -> str:
    """MODE 1: Normal Entry (from Phase 4 with validated hypothesis ≥70%)"""
    working_conclusion = investigation_state.lifecycle.working_conclusion
    confidence = working_conclusion.confidence if working_conclusion else 0.0
    statement = working_conclusion.statement if working_conclusion else "Unknown"

    return f"""# Phase 5 Entry Mode: NORMAL (Validated Root Cause)

## Context
You've successfully validated the root cause through systematic investigation (Phase 4).
Confidence level: {confidence*100:.0f}% ({working_conclusion.confidence_level.value if working_conclusion else 'unknown'})

**Validated Root Cause**:
{statement}

## Your Task
Propose and guide implementation of targeted solution addressing validated root cause.

## Approach
1. **Propose Solution**: Specific fix targeting validated root cause
2. **Implementation Guidance**: Step-by-step instructions
3. **Verification Plan**: How to confirm solution worked
4. **Success Criteria**: Clear metrics for resolution

## Confidence Level
Since root cause is validated (≥70% confidence), you can:
- Propose specific, targeted solutions
- Provide detailed implementation steps
- Set clear success criteria
- Expect high probability of resolution

## Caveats
None - proceed with confidence based on validated root cause.
"""


def _get_fast_recovery_entry_context(investigation_state: InvestigationState) -> str:
    """MODE 2: Fast Recovery Entry (direct from Phase 1, user-confirmed urgent)"""
    anomaly = investigation_state.ooda_engine.anomaly_frame
    blast_radius = anomaly.affected_scope if anomaly else "Unknown scope"
    severity = investigation_state.lifecycle.urgency_level

    return f"""# Phase 5 Entry Mode: FAST RECOVERY (Urgent Mitigation)

## Context
**CRITICAL INCIDENT MODE**: User confirmed urgent need for immediate mitigation.
You've skipped systematic investigation (Phases 2-4) to prioritize service restoration.

**Blast Radius**: {blast_radius}
**Urgency Level**: {severity}

## Your Task
Propose SAFE mitigation that restores service WITHOUT validated root cause.

## Approach
1. **Safe Mitigation**: Prioritize service restoration over root cause fix
   - Rollback recent changes
   - Increase resources (scale up, pool expansion)
   - Bypass/disable failing component
   - Traffic reroute/failover

2. **Risk Assessment**: Acknowledge uncertainty
   - "This mitigation targets likely cause, but root cause not validated"
   - "May not fully resolve if actual cause differs"
   - "Close monitoring required"

3. **Monitoring Plan**: Detect if mitigation works
   - What metrics to watch
   - How long to observe
   - Rollback plan if ineffective

4. **Post-Incident Follow-Up**: Recommend RCA after stabilization
   - "Once service restored, recommend full investigation"
   - "This is temporary mitigation, not root cause fix"

## Confidence Level
**SPECULATION** (<50%) - Root cause NOT validated
- Solutions are educated guesses based on blast radius and common patterns
- Multiple mitigation options (if one fails, try next)
- Explicit caveats required in all recommendations

## Critical Caveats
- ⚠️ Root cause not validated - solution may not work
- ⚠️ Monitor closely for effectiveness
- ⚠️ Prepare rollback if mitigation ineffective
- ⚠️ Full RCA recommended after service restoration

## Success Criteria
1. Service restored (symptoms resolved)
2. No new issues introduced by mitigation
3. Monitoring confirms stability

Follow-up: Schedule full investigation (Phases 2-4) after incident resolved.
"""


def _get_degraded_entry_context(investigation_state: InvestigationState) -> str:
    """MODE 3: Degraded Entry (from Phase 4 in degraded mode, confidence capped <70%)"""
    escalation_state = investigation_state.lifecycle.escalation_state
    working_conclusion = investigation_state.lifecycle.working_conclusion

    degraded_type = escalation_state.degraded_mode_type.value if escalation_state.degraded_mode_type else "unknown"
    degraded_explanation = escalation_state.degraded_mode_explanation or "Unknown limitation"
    confidence_cap = escalation_state.get_confidence_cap()
    current_confidence = working_conclusion.confidence if working_conclusion else 0.0
    statement = working_conclusion.statement if working_conclusion else "Unknown"

    return f"""# Phase 5 Entry Mode: DEGRADED (Investigation Limited)

## Context
Investigation completed Phase 4 but could NOT reach 70% confidence threshold.
Operating in degraded mode due to limitations.

**Degraded Mode Type**: {degraded_type}
**Limitation**: {degraded_explanation}
**Confidence Cap**: {confidence_cap*100 if confidence_cap else 0:.0f}%
**Current Confidence**: {current_confidence*100:.0f}% ({working_conclusion.confidence_level.value if working_conclusion else 'unknown'})

**Best Understanding (not validated)**:
{statement}

## Your Task
Propose BEST-EFFORT solution with explicit caveats and uncertainty acknowledgment.

## Approach

### 1. Acknowledge Limitation Upfront
State clearly why confidence is capped:
```
"I've reached the confidence limit for this investigation ({confidence_cap*100 if confidence_cap else 0:.0f}% cap)
due to {degraded_explanation}.

Based on available evidence, my best assessment is:
{statement}

However, this is NOT a validated root cause. Here's my recommended approach..."
```

### 2. Provide Best-Effort Solution
Propose solution targeting most likely cause:
- Based on evidence collected
- Accounts for alternative explanations
- Includes safety measures

### 3. Explicit Caveats
Communicate uncertainty transparently:
- "Confidence level: {current_confidence*100:.0f}% (probable, not validated)"
- "May not fully resolve if actual cause differs"
- "Alternative explanations: [list from working conclusion]"

### 4. Multi-Option Approach
If confidence <50%, provide multiple mitigation paths:
- Option A: Target primary hypothesis (most likely)
- Option B: Target secondary hypothesis (alternative)
- Option C: General mitigation (if both fail)

### 5. Enhanced Monitoring
Since root cause uncertain, monitoring critical:
- What metrics indicate success
- What metrics indicate wrong hypothesis
- Decision points for trying alternative solutions

### 6. Escalation Reminder
Remind user of escalation option:
"Due to [limitation], I recommend:
1. Try this best-effort solution with close monitoring
2. If ineffective, escalate to [specific expert/team]
3. Consider [what would remove limitation]"

## Confidence Level
**{working_conclusion.confidence_level.value.upper() if working_conclusion else 'UNKNOWN'}** ({current_confidence*100:.0f}%)
- NOT validated (below 70% threshold)
- Best-effort based on available evidence
- Significant uncertainty remains

## Critical Caveats
- ⚠️ Root cause NOT validated due to {degraded_type}
- ⚠️ Solution may not work if hypothesis incorrect
- ⚠️ Alternative explanations exist (see working conclusion)
- ⚠️ Close monitoring essential
- ⚠️ Escalation option available

## Success Criteria
1. **If solution works**: Symptoms resolve, metrics normalize
2. **If solution doesn't work**: No harm caused, easy rollback
3. **Monitoring confirms**: Either success or need for escalation

## Expected Outcomes
- **Best case**: Solution works despite uncertainty (hypothesis was correct)
- **Likely case**: Partial improvement (hypothesis partially correct)
- **Worst case**: No improvement → escalate with investigation summary

**Important**: User chose to proceed with degraded mode solution rather than escalate.
Respect that choice while maintaining transparency about limitations.
"""


def should_display_entry_mode_banner(entry_mode: str) -> bool:
    """Determine if entry mode should show prominent banner

    Args:
        entry_mode: 'normal', 'fast_recovery', or 'degraded'

    Returns:
        True if should display banner (fast_recovery or degraded)
    """
    return entry_mode in ['fast_recovery', 'degraded']


def get_entry_mode_banner(entry_mode: str) -> str:
    """Get visual banner for non-normal entry modes

    Args:
        entry_mode: 'fast_recovery' or 'degraded'

    Returns:
        Banner string for user-facing output
    """
    banners = {
        'fast_recovery': """
╔═══════════════════════════════════════════════════════════╗
║  ⚡ FAST RECOVERY MODE - Root Cause Not Validated ⚡     ║
║  Proposing mitigation based on blast radius analysis      ║
╚═══════════════════════════════════════════════════════════╝
""",
        'degraded': """
╔═══════════════════════════════════════════════════════════╗
║  ⚠️  DEGRADED INVESTIGATION MODE - Limited Confidence ⚠️ ║
║  Proceeding with best-effort solution and caveats         ║
╚═══════════════════════════════════════════════════════════╝
""",
    }
    return banners.get(entry_mode, "")
