"""Phase Loop-Back Mechanism (Proposal #6, v3.0)

Handles phase progression outcomes including loop-back patterns when validation fails,
scope changes, or timeline revisions are needed.

Design Principle:
- Phase progression is not strictly unidirectional
- Failed validation should loop back to hypothesis generation
- New information may require revisiting earlier phases
- Safety limits prevent infinite loops
- Escalation via degraded mode (v3.0) when progress blocked

Design Reference:
- docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from faultmaven.models.investigation import InvestigationState, InvestigationPhase

from faultmaven.models.investigation import InvestigationPhase

logger = logging.getLogger(__name__)


class PhaseOutcome(str, Enum):
    """Phase completion outcomes that determine next phase (v3.0)"""

    COMPLETED = "completed"                  # Normal completion → advance to next phase
    HYPOTHESIS_REFUTED = "hypothesis_refuted"  # All hypotheses refuted → loop to Phase 3
    SCOPE_CHANGED = "scope_changed"          # New info expanded blast radius → loop to Phase 1
    TIMELINE_WRONG = "timeline_wrong"        # Timeline analysis incorrect → loop to Phase 2
    NEED_MORE_DATA = "need_more_data"        # Insufficient evidence → stay in current phase
    STALLED = "stalled"                      # No progress → enter degraded mode (v3.0)
    ESCALATION_NEEDED = "escalation_needed"  # Requires human guidance


class LoopBackReason(str, Enum):
    """Detailed reasons for loop-back"""

    ALL_HYPOTHESES_REFUTED = "all_hypotheses_refuted"
    INSUFFICIENT_HYPOTHESES = "insufficient_hypotheses"
    SCOPE_EXPANSION = "scope_expansion"
    SCOPE_REDUCTION = "scope_reduction"
    TIMELINE_REVISION_NEEDED = "timeline_revision_needed"
    CORRELATION_FOUND = "correlation_found"
    EVIDENCE_GAP_IDENTIFIED = "evidence_gap_identified"
    MAX_LOOPS_EXCEEDED = "max_loops_exceeded"


class PhaseLoopBackHandler:
    """Handles phase outcomes and loop-back decisions

    Safety Features:
    - Maximum 3 loop-backs per investigation
    - Escalation options when max exceeded
    - Loop-back tracking and metrics
    """

    MAX_LOOP_BACKS = 3

    def __init__(self):
        self.loop_back_history = []

    def handle_phase_outcome(
        self,
        current_phase: InvestigationPhase,
        outcome: PhaseOutcome,
        state: "InvestigationState",
        reason: Optional[LoopBackReason] = None,
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle phase outcome and determine next phase

        Args:
            current_phase: Current investigation phase
            outcome: Phase completion outcome
            state: Investigation state
            reason: Optional detailed reason for outcome

        Returns:
            Tuple of (next_phase, is_loop_back, message)
        """
        logger.info(
            f"Handling phase outcome: {current_phase.name} → {outcome.value}",
            extra={"reason": reason.value if reason else None},
        )

        # Check for normal completion
        if outcome == PhaseOutcome.COMPLETED:
            return self._handle_completed(current_phase, state)

        # Check for loop-back outcomes
        elif outcome == PhaseOutcome.HYPOTHESIS_REFUTED:
            return self._handle_hypothesis_refuted(current_phase, state, reason)

        elif outcome == PhaseOutcome.SCOPE_CHANGED:
            return self._handle_scope_changed(current_phase, state, reason)

        elif outcome == PhaseOutcome.TIMELINE_WRONG:
            return self._handle_timeline_wrong(current_phase, state, reason)

        elif outcome == PhaseOutcome.NEED_MORE_DATA:
            return self._handle_need_more_data(current_phase, state)

        elif outcome == PhaseOutcome.STALLED:
            return self._handle_stalled(current_phase, state)

        elif outcome == PhaseOutcome.ESCALATION_NEEDED:
            return self._handle_escalation(current_phase, state)

        else:
            logger.warning(f"Unknown phase outcome: {outcome}")
            return current_phase, False, None

    def _handle_completed(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle normal phase completion → advance to next phase"""

        next_phase = self._get_next_phase(current_phase)

        logger.info(
            f"Phase completed normally: {current_phase.name} → {next_phase.name}",
            extra={"loop_back_count": state.lifecycle.loop_back_count},
        )

        return next_phase, False, None

    def _handle_hypothesis_refuted(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
        reason: Optional[LoopBackReason],
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle all hypotheses refuted → loop back to Phase 3"""

        # Check loop-back limit
        if state.lifecycle.loop_back_count >= self.MAX_LOOP_BACKS:
            return self._handle_max_loops_exceeded(current_phase, state)

        # Record loop-back
        state.lifecycle.loop_back_count += 1
        self.loop_back_history.append({
            "from_phase": current_phase,
            "to_phase": InvestigationPhase.HYPOTHESIS,
            "reason": reason,
            "turn": state.metadata.current_turn,
        })

        message = (
            f"All hypotheses refuted. Looping back to Phase 3 (Hypothesis) "
            f"to generate alternative theories. Loop-back {state.lifecycle.loop_back_count}/{self.MAX_LOOP_BACKS}"
        )

        logger.info(
            f"Loop-back triggered: {current_phase.name} → HYPOTHESIS",
            extra={
                "reason": reason.value if reason else None,
                "loop_back_count": state.lifecycle.loop_back_count,
            },
        )

        return InvestigationPhase.HYPOTHESIS, True, message

    def _handle_scope_changed(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
        reason: Optional[LoopBackReason],
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle scope change → loop back to Phase 1"""

        # Check loop-back limit
        if state.lifecycle.loop_back_count >= self.MAX_LOOP_BACKS:
            return self._handle_max_loops_exceeded(current_phase, state)

        # Record loop-back
        state.lifecycle.loop_back_count += 1
        self.loop_back_history.append({
            "from_phase": current_phase,
            "to_phase": InvestigationPhase.BLAST_RADIUS,
            "reason": reason,
            "turn": state.metadata.current_turn,
        })

        message = (
            f"Blast radius changed significantly. Looping back to Phase 1 (Blast Radius) "
            f"to reassess scope. Loop-back {state.lifecycle.loop_back_count}/{self.MAX_LOOP_BACKS}"
        )

        logger.info(
            f"Loop-back triggered: {current_phase.name} → BLAST_RADIUS",
            extra={
                "reason": reason.value if reason else None,
                "loop_back_count": state.lifecycle.loop_back_count,
            },
        )

        return InvestigationPhase.BLAST_RADIUS, True, message

    def _handle_timeline_wrong(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
        reason: Optional[LoopBackReason],
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle timeline revision needed → loop back to Phase 2"""

        # Check loop-back limit
        if state.lifecycle.loop_back_count >= self.MAX_LOOP_BACKS:
            return self._handle_max_loops_exceeded(current_phase, state)

        # Record loop-back
        state.lifecycle.loop_back_count += 1
        self.loop_back_history.append({
            "from_phase": current_phase,
            "to_phase": InvestigationPhase.TIMELINE,
            "reason": reason,
            "turn": state.metadata.current_turn,
        })

        message = (
            f"Timeline analysis needs revision. Looping back to Phase 2 (Timeline) "
            f"to re-establish event sequence. Loop-back {state.lifecycle.loop_back_count}/{self.MAX_LOOP_BACKS}"
        )

        logger.info(
            f"Loop-back triggered: {current_phase.name} → TIMELINE",
            extra={
                "reason": reason.value if reason else None,
                "loop_back_count": state.lifecycle.loop_back_count,
            },
        )

        return InvestigationPhase.TIMELINE, True, message

    def _handle_need_more_data(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle insufficient data → stay in current phase"""

        message = (
            f"Insufficient evidence to complete {current_phase.name}. "
            f"Continuing OODA iterations to gather more data."
        )

        logger.info(
            f"Staying in phase: {current_phase.name}",
            extra={"reason": "need_more_data"},
        )

        return current_phase, False, message

    def _handle_stalled(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle stalled investigation → enter degraded mode (v3.0)

        Note: v3.0 uses degraded mode instead of stall recovery.
        This signals that progress is blocked and degraded mode should activate.
        """

        message = (
            f"Investigation progress blocked in {current_phase.name}. "
            f"Entering degraded mode (v3.0)."
        )

        logger.warning(
            f"Investigation progress blocked: {current_phase.name}",
            extra={"loop_back_count": state.lifecycle.loop_back_count},
        )

        # Return current phase - degraded mode will be handled by working conclusion generator
        return current_phase, False, message

    def _handle_escalation(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle escalation needed → requires human guidance"""

        message = (
            f"Investigation in {current_phase.name} requires human guidance. "
            f"Please provide direction or additional context."
        )

        logger.warning(
            f"Escalation needed: {current_phase.name}",
            extra={"loop_back_count": state.lifecycle.loop_back_count},
        )

        return current_phase, False, message

    def _handle_max_loops_exceeded(
        self,
        current_phase: InvestigationPhase,
        state: "InvestigationState",
    ) -> tuple[InvestigationPhase, bool, Optional[str]]:
        """Handle maximum loop-backs exceeded → force progress or escalate

        Options:
        1. Force mitigation - Give up on RCA, focus on workaround
        2. Escalate to human - Ask for user guidance
        3. Document partial findings - Close with what we know
        """

        logger.error(
            f"Maximum loop-backs exceeded ({self.MAX_LOOP_BACKS})",
            extra={
                "current_phase": current_phase.name,
                "loop_history": self.loop_back_history,
            },
        )

        # Default: Force progress to Solution phase with partial findings
        message = (
            f"Maximum loop-backs ({self.MAX_LOOP_BACKS}) exceeded. "
            f"Proceeding to Solution phase with partial findings. "
            f"Root cause analysis incomplete - focusing on mitigation options."
        )

        logger.info(
            "Forcing progress to Solution phase",
            extra={"reason": "max_loops_exceeded"},
        )

        return InvestigationPhase.SOLUTION, False, message

    def _get_next_phase(self, current_phase: InvestigationPhase) -> InvestigationPhase:
        """Get next phase in normal progression"""

        phase_order = [
            InvestigationPhase.INTAKE,
            InvestigationPhase.BLAST_RADIUS,
            InvestigationPhase.TIMELINE,
            InvestigationPhase.HYPOTHESIS,
            InvestigationPhase.VALIDATION,
            InvestigationPhase.SOLUTION,
            InvestigationPhase.DOCUMENT,
        ]

        try:
            current_index = phase_order.index(current_phase)
            if current_index < len(phase_order) - 1:
                return phase_order[current_index + 1]
            else:
                # Already at last phase
                return current_phase
        except ValueError:
            logger.error(f"Unknown phase: {current_phase}")
            return current_phase

    def get_loop_back_statistics(self, state: "InvestigationState") -> dict:
        """Get loop-back statistics for investigation

        Returns:
            Dictionary with loop-back metrics
        """
        return {
            "total_loop_backs": state.lifecycle.loop_back_count,
            "max_allowed": self.MAX_LOOP_BACKS,
            "loop_back_history": self.loop_back_history,
            "loops_remaining": max(0, self.MAX_LOOP_BACKS - state.lifecycle.loop_back_count),
        }


def should_loop_back_from_validation(state: "InvestigationState") -> tuple[bool, Optional[PhaseOutcome], Optional[LoopBackReason]]:
    """Check if Phase 4 (Validation) should loop back

    Checks:
    - All active hypotheses refuted?
    - Insufficient hypotheses remaining?

    Returns:
        Tuple of (should_loop, outcome, reason)
    """
    from faultmaven.models.investigation import HypothesisStatus

    hypotheses = state.ooda_engine.hypotheses

    # Count hypothesis states
    active_count = sum(1 for h in hypotheses if h.status == HypothesisStatus.ACTIVE)
    validated_count = sum(1 for h in hypotheses if h.status == HypothesisStatus.VALIDATED)
    refuted_count = sum(1 for h in hypotheses if h.status == HypothesisStatus.REFUTED)

    # Check if we have a validated hypothesis
    if validated_count > 0:
        # Success! We have a root cause
        return False, None, None

    # Check if all hypotheses are refuted
    if refuted_count > 0 and active_count == 0:
        logger.warning(
            "All hypotheses refuted in Phase 4",
            extra={
                "total_hypotheses": len(hypotheses),
                "refuted": refuted_count,
            },
        )
        return True, PhaseOutcome.HYPOTHESIS_REFUTED, LoopBackReason.ALL_HYPOTHESES_REFUTED

    # Check if insufficient hypotheses remaining
    if active_count < 2 and validated_count == 0:
        logger.warning(
            "Insufficient active hypotheses in Phase 4",
            extra={
                "active": active_count,
                "refuted": refuted_count,
            },
        )
        return True, PhaseOutcome.HYPOTHESIS_REFUTED, LoopBackReason.INSUFFICIENT_HYPOTHESES

    # All good, continue validation
    return False, None, None
