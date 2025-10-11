"""
Investigation Stall Detection

Detects when troubleshooting investigation has stalled and cannot progress,
preventing infinite loops and providing graceful termination.

Stall Conditions:
1. Multiple critical evidence blocked (≥3)
2. All hypotheses refuted (Phase 4)
3. No phase progress for extended period (≥5 turns)
4. Unable to formulate hypotheses (Phase 3, 0 hypotheses after 3 turns)

Design Reference: docs/architecture/EVIDENCE_CENTRIC_TROUBLESHOOTING_DESIGN.md
"""

import logging
from typing import Optional

from faultmaven.models.evidence import EvidenceStatus, EvidenceCategory
from faultmaven.models.case import CaseDiagnosticState

logger = logging.getLogger(__name__)


def check_for_stall(state: CaseDiagnosticState) -> Optional[str]:
    """
    Determine if investigation has stalled, return reason if so.

    NOTE: Phase numbering follows Doctor-Patient Architecture:
    - Phase 0: Intake (problem confirmation)
    - Phase 1: Blast Radius (scope impact)
    - Phase 2: Timeline (when did it start)
    - Phase 3: Hypothesis (formulate theories)
    - Phase 4: Validation (test hypotheses)
    - Phase 5: Solution (propose fix)

    See: docs/architecture/DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md

    Args:
        state: Current case diagnostic state

    Returns:
        Stall reason string if stalled, None if investigation can continue

    Raises:
        ValueError: If phase number is invalid (not 0-5)
    """
    # Validate phase bounds
    if not (0 <= state.current_phase <= 5):
        raise ValueError(f"Invalid phase number: {state.current_phase}. Must be 0-5.")

    # Check 1: Multiple critical evidence blocked
    blocked_critical = [
        req for req in state.evidence_requests
        if req.status == EvidenceStatus.BLOCKED
        and req.category in [
            EvidenceCategory.SYMPTOMS,
            EvidenceCategory.CONFIGURATION,
            EvidenceCategory.METRICS  # Added metrics as critical
        ]
    ]

    if len(blocked_critical) >= 3:
        blocked_labels = [req.label for req in blocked_critical]
        logger.warning(f"Stall detected: {len(blocked_critical)} critical evidence sources blocked")
        return (
            f"Multiple critical evidence sources blocked (cannot access logs, configs, metrics). "
            f"Blocked items: {', '.join(blocked_labels[:3])}"
        )

    # Check 2: All hypotheses refuted (Phase 4: Validation)
    if state.current_phase == 4:
        # Check if we have hypotheses and they're all refuted
        hypotheses = state.hypotheses or []
        if len(hypotheses) >= 3:
            # Count how many are refuted
            refuted_count = sum(
                1 for h in hypotheses
                if isinstance(h, dict) and h.get("status") == "refuted"
            )
            if refuted_count >= len(hypotheses):
                logger.warning(f"Stall detected: All {len(hypotheses)} hypotheses refuted")
                return (
                    f"All formulated hypotheses ({len(hypotheses)}) have been refuted by evidence. "
                    f"Unable to identify root cause with available information."
                )

    # Check 3: No progress for extended period
    turns_without_progress = state.turns_without_phase_advance
    if turns_without_progress >= 5:
        logger.warning(f"Stall detected: No phase advancement for {turns_without_progress} turns")
        return (
            f"No investigation progress after {turns_without_progress} turns. "
            f"Possible evidence loop or dead end."
        )

    # Check 4: Phase 3 (Hypothesis) with no hypotheses after 3 turns
    if state.current_phase == 3:
        hypotheses = state.hypotheses or []
        turns_in_phase = state.turns_in_current_phase
        if len(hypotheses) == 0 and turns_in_phase >= 3:
            logger.warning(f"Stall detected: Phase 3 with 0 hypotheses after {turns_in_phase} turns")
            return (
                f"Unable to formulate hypotheses with available evidence after {turns_in_phase} turns. "
                f"May need additional diagnostic data or subject matter expertise."
            )

    # No stall detected
    return None


def increment_stall_counters(state: CaseDiagnosticState, phase_advanced: bool) -> None:
    """
    Update stall detection counters after each turn.

    Args:
        state: Current case diagnostic state (modified in-place)
        phase_advanced: Whether the phase advanced this turn
    """
    if phase_advanced:
        # Reset counters when phase advances
        state.turns_without_phase_advance = 0
        state.turns_in_current_phase = 1  # First turn in new phase
        logger.info(f"Phase advanced to {state.current_phase}, reset stall counters")
    else:
        # Increment both counters
        state.turns_without_phase_advance += 1
        state.turns_in_current_phase += 1


def should_escalate(state: CaseDiagnosticState, stall_reason: str) -> bool:
    """
    Determine if case should be escalated vs abandoned.

    Escalation criteria:
    - Blocked evidence that infrastructure team could access
    - Complex issue requiring SME expertise
    - Security/access restrictions preventing diagnosis

    Abandonment criteria:
    - User stopped responding (not a stall, but timeout)
    - User explicitly gave up
    - Too many refuted hypotheses (likely not solvable)

    Args:
        state: Current case diagnostic state
        stall_reason: Reason for stall

    Returns:
        True if should escalate, False if should abandon
    """
    # Evidence blocking suggests escalation (need higher privileges)
    if "blocked" in stall_reason.lower():
        return True

    # All hypotheses refuted suggests we need SME help
    if "refuted" in stall_reason.lower():
        return True

    # No progress without blocking suggests issue is too complex
    if "no investigation progress" in stall_reason.lower():
        # Check if evidence was actively provided
        recent_evidence = [
            ev for ev in state.evidence_provided
            if ev.turn_number >= (state.current_phase - 3)  # Last 3 turns
        ]
        if len(recent_evidence) > 0:
            # User is engaged, escalate for help
            return True
        else:
            # User not providing evidence, likely abandoned
            return False

    # Default: escalate (be optimistic)
    return True


def generate_stall_message(
    stall_reason: str,
    escalate: bool,
    state: CaseDiagnosticState
) -> str:
    """
    Generate user-facing message for stalled investigation.

    Args:
        stall_reason: Technical reason for stall
        escalate: Whether recommending escalation vs abandonment
        state: Current case diagnostic state

    Returns:
        Formatted message for user
    """
    if escalate:
        return f"""⚠️  **Investigation Stalled**

{stall_reason}

**Recommendation**: Escalate to infrastructure/SRE team

**What we know**:
- Problem: {state.problem_statement or 'Not specified'}
- Phase: {_phase_name(state.current_phase)}
- Evidence collected: {len(state.evidence_provided)} items
- Hypotheses tested: {len(state.hypotheses or [])}

**Next steps**:
1. Generate escalation report with all findings
2. Provide report to team with necessary access/expertise
3. Alternative: Re-engage with different evidence sources

Would you like me to generate an escalation report?"""
    else:
        return f"""⚠️  **Investigation Incomplete**

{stall_reason}

**Status**: Unable to proceed with current information

**What was collected**:
- Evidence provided: {len(state.evidence_provided)} items
- Phase reached: {_phase_name(state.current_phase)}

**Options**:
1. Provide additional evidence if available
2. Close case as unresolved
3. Retry investigation with different approach

How would you like to proceed?"""


def _phase_name(phase: int) -> str:
    """Get human-readable phase name"""
    phase_names = {
        0: "Intake (Problem Identification)",
        1: "Blast Radius (Impact Assessment)",
        2: "Timeline (Change Analysis)",
        3: "Hypothesis (Root Cause Theories)",
        4: "Validation (Testing & Verification)",
        5: "Solution (Resolution & Prevention)"
    }
    return phase_names.get(phase, f"Unknown Phase {phase}")
