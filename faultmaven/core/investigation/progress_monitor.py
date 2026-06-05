"""Progress Monitor for Investigation Engine

Tracks investigation progress per stage and surfaces milestone dependencies
to the user when progress stalls.

Design Philosophy:
- The agent follows behavioral rules every turn. It should not be the reason
  for stalled investigations.
- When progress stalls, make the situation visible: what milestone is pending,
  what evidence would advance it, and (from the agent) what that means for
  this specific case.
- Influence the user through visibility, not steering. Like a light: it draws
  attention and lets you see what's there.
- Never detect or judge user behavior. The user controls the conversation.

Two Modes:
- Silent (default): progress tracked internally, nothing extra surfaced.
- Transparent: activated after N investigative turns without a milestone
  completing. The agent provides case-specific guidance on what's needed.

Agent State Repair Patterns (checked while in transparent mode):
1. HYPOTHESIS_ANCHORING - Same hypothesis category tested repeatedly
2. HYPOTHESIS_DEADLOCK - All hypotheses inconclusive
3. EXHAUSTED - Broad diagnostic coverage, no convergence
4. FIX_FAILURE_CYCLE - Multiple fix attempts applied and failed
5. ACTION_LOOP - Agent repeating identical execution paths

Design Reference:
- docs/architecture/investigation-engine/progress-transparency.md
- docs/architecture/investigation-engine/agent-behavioral-rules.md

Usage:
    monitor = ProgressMonitor()
    result = monitor.check_progress(case)
    if result:
        # result.prompt_injection -> inject into system_feedback
        # result.repair_type -> specific repair pattern detected (if any)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    HypothesisState,
    InvestigationStage,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Stage-Gate Milestones (trigger stage transitions)
# =============================================================================
#
# These four milestones drive stage transitions (per
# docs/architecture/investigation-engine/evidence-driven-investigation-framework.md §3).
# When any of them fires in a turn, the stage changes. Because they are also
# included in TurnProgress.milestones_completed, the investigative-turn counter
# resets on stage transitions through the general milestone-reset path — the
# counter is effectively "turns since the last milestone OR stage change,"
# matching progress-transparency.md §Transitions.
STAGE_GATE_MILESTONES: frozenset[str] = frozenset(
    {
        "mitigation_accepted",
        "mitigation_verified",
        "solution_accepted",
        "solution_verified",
    }
)


# =============================================================================
# Milestone Dependency Map (static)
# =============================================================================

MILESTONE_DEPENDENCIES: Dict[str, Dict[str, str]] = {
    "symptom_verified": {
        "stage": "DIAGNOSIS",
        "description": "Verify the reported symptoms with evidence",
        "needs": "Evidence showing error patterns, symptoms, or anomalies (logs, error reports, metrics)",
    },
    "root_cause_identified": {
        "stage": "DIAGNOSIS",
        "description": "Validate a hypothesis with supporting causal evidence",
        "needs": "Validated hypothesis with supporting causal evidence",
    },
    "solution_proposed": {
        "stage": "DIAGNOSIS",
        "description": "Propose a concrete fix based on identified root cause",
        "needs": "Sufficient confidence in root cause to propose a concrete fix",
    },
    "mitigation_verified": {
        "stage": "MITIGATION",
        "description": "Verify that the temporary fix stabilized the situation",
        "needs": "User confirmation that temporary fix stabilized the situation",
    },
    "solution_verified": {
        "stage": "TREATMENT",
        "description": "Verify that the permanent fix resolved the issue",
        "needs": "User confirmation that permanent fix resolved the issue",
    },
}


# =============================================================================
# Enums and Data Classes
# =============================================================================


class RepairType(str, Enum):
    """Agent state repair patterns detected during transparent mode."""

    HYPOTHESIS_ANCHORING = "hypothesis_anchoring"
    HYPOTHESIS_DEADLOCK = "hypothesis_deadlock"
    EXHAUSTED = "exhausted"
    FIX_FAILURE_CYCLE = "fix_failure_cycle"
    ACTION_LOOP = "action_loop"


@dataclass
class ProgressCheckResult:
    """Result of a progress check.

    Returned by ProgressMonitor.check_progress() when the investigation
    needs attention (transparent mode activated or repair pattern detected).
    Returns None when progress is normal (silent mode).
    """

    transparent_mode: bool
    pending_milestone: Optional[str] = None
    milestone_description: Optional[str] = None
    milestone_needs: Optional[str] = None
    repair_type: Optional[RepairType] = None
    repair_action: Optional[str] = None
    repair_message: Optional[str] = None
    prompt_injection: Optional[str] = None


@dataclass
class RepairAction:
    """Structural action taken to repair agent state."""

    repair_type: RepairType
    action: str
    message: str
    prompt_injection: str


# =============================================================================
# Progress Monitor
# =============================================================================


class ProgressMonitor:
    """
    Tracks investigation progress per stage and surfaces milestone
    dependencies when progress stalls.

    Two responsibilities:
    1. Progress transparency: detect when investigative turns pass without
       milestone advancement and build case-aware prompt injection.
    2. Agent state repair: detect specific internal failure patterns
       (deadlock, anchoring, etc.) and take structural action.
    """

    def __init__(
        self,
        transparency_threshold: int = 5,
        category_anchoring_threshold: int = 4,
        action_loop_threshold: int = 5,
        exhaustion_min_turns: int = 8,
        exhaustion_stall_threshold: int = 5,
        fix_failure_threshold: int = 2,
    ):
        """
        Initialize progress monitor with thresholds.

        Args:
            transparency_threshold: Investigative turns without milestone
                progress before transparent mode activates (default 5)
            category_anchoring_threshold: Failed hypotheses in same category
                before ANCHORING fires (default 4)
            action_loop_threshold: Turns with identical structural output
                before ACTION_LOOP fires (default 5)
            exhaustion_min_turns: Minimum total turns before EXHAUSTED
                can fire (default 8)
            exhaustion_stall_threshold: Turns without progress needed for
                exhaustion (default 5)
            fix_failure_threshold: Accepted-but-unverified fix attempts
                before FIX_FAILURE_CYCLE fires (default 2)
        """
        self.transparency_threshold = transparency_threshold
        self.category_anchoring_threshold = category_anchoring_threshold
        self.action_loop_threshold = action_loop_threshold
        self.exhaustion_min_turns = exhaustion_min_turns
        self.exhaustion_stall_threshold = exhaustion_stall_threshold
        self.fix_failure_threshold = fix_failure_threshold

    # =========================================================================
    # Main Entry Point
    # =========================================================================

    def check_progress(self, case: Case) -> Optional[ProgressCheckResult]:
        """
        Check investigation progress and determine if transparency is needed.

        Called after each turn is processed. Returns None if progress is
        normal (silent mode). Returns a ProgressCheckResult if transparent
        mode should activate.

        Args:
            case: Current investigation case

        Returns:
            ProgressCheckResult if transparent mode should activate, None otherwise
        """
        # Only during active investigation
        if case.state != CaseState.INVESTIGATING:
            return None

        # Compute transparency state from turn history (stateless — no
        # persistence needed, fully reconstructable each turn)
        investigative_turns = self._count_investigative_turns_since_milestone(case)

        if investigative_turns < self.transparency_threshold:
            return None

        # Transparent mode — find pending milestone and check for repair patterns
        stage = case.current_stage or InvestigationStage.DIAGNOSIS
        pending = self._find_pending_milestone(case, stage)

        if not pending:
            return None

        milestone_name, milestone_info = pending

        # Check for repair patterns (only in transparent mode)
        repair = self._check_repair_patterns(case, stage)

        # Build the prompt injection
        prompt_injection = self._build_prompt_injection(
            stage=stage,
            milestone_name=milestone_name,
            milestone_info=milestone_info,
            investigative_turns=investigative_turns,
            repair=repair,
        )

        result = ProgressCheckResult(
            transparent_mode=True,
            pending_milestone=milestone_name,
            milestone_description=milestone_info["description"],
            milestone_needs=milestone_info["needs"],
            prompt_injection=prompt_injection,
        )

        if repair:
            result.repair_type = repair.repair_type
            result.repair_action = repair.action
            result.repair_message = repair.message

        return result

    # =========================================================================
    # Investigative Turn Counter
    # =========================================================================

    def _count_investigative_turns_since_milestone(self, case: Case) -> int:
        """
        Count investigative turns since the last milestone was completed.

        An investigative turn is one where:
        - The user submitted data (evidence was added), OR
        - The agent executed diagnostic tools (outcome is not 'conversation')

        Conversational turns (questions, off-topic chat) are excluded.

        **Stage-scoping:** The counter resets on any entry in
        ``milestones_completed``. This includes both progress milestones (e.g.,
        ``symptom_verified``) AND stage-gate milestones
        (``STAGE_GATE_MILESTONES``: ``mitigation_accepted``,
        ``mitigation_verified``, ``solution_accepted``, ``solution_verified``).
        Since stage transitions are always driven by a stage-gate milestone in
        the same turn, a stage transition always resets the counter. This
        matches progress-transparency.md §Transitions ("A stage transition
        resets the counter. A milestone completion in the current stage turns
        the light off") — the two clauses describe the same mechanism from
        different angles.

        Args:
            case: Current investigation case

        Returns:
            Number of investigative turns since last milestone completion
        """
        if not case.turn_history:
            return 0

        count = 0
        for turn in reversed(case.turn_history):
            # Reset on any milestone — progress OR stage-gate. Stage
            # transitions come through here because STAGE_GATE_MILESTONES
            # are recorded in turn.milestones_completed when they fire.
            if turn.milestones_completed:
                break

            # Count as investigative if evidence was added or agent did
            # diagnostic work (anything other than pure conversation)
            is_investigative = bool(turn.evidence_added) or (
                turn.outcome and turn.outcome.value not in ("conversation", "other")
            )

            if is_investigative:
                count += 1

        return count

    # =========================================================================
    # Pending Milestone Detection
    # =========================================================================

    def _find_pending_milestone(
        self, case: Case, stage: InvestigationStage
    ) -> Optional[tuple]:
        """
        Find the next pending milestone for the current stage.

        Returns the first milestone in the dependency map that:
        - Belongs to the current stage
        - Has not been completed

        Args:
            case: Current investigation case
            stage: Current investigation stage

        Returns:
            Tuple of (milestone_name, milestone_info) or None
        """
        if not case.progress:
            return None

        stage_name = stage.value.upper()

        for milestone_name, info in MILESTONE_DEPENDENCIES.items():
            if info["stage"] != stage_name:
                continue

            # Check if this milestone is already completed
            milestone_value = getattr(case.progress, milestone_name, None)
            if not milestone_value:
                return (milestone_name, info)

        return None

    # =========================================================================
    # Prompt Injection Builder
    # =========================================================================

    def _build_prompt_injection(
        self,
        stage: InvestigationStage,
        milestone_name: str,
        milestone_info: dict,
        investigative_turns: int,
        repair: Optional[RepairAction],
    ) -> str:
        """
        Build the prompt injection text for transparent mode.

        Combines the progress transparency message with any repair
        pattern prompt injection.

        Args:
            stage: Current investigation stage
            milestone_name: Name of the pending milestone
            milestone_info: Milestone dependency info
            investigative_turns: Number of investigative turns since last milestone
            repair: Repair action if a pattern was detected

        Returns:
            Prompt injection text
        """
        parts = []

        # Progress transparency injection
        parts.append(
            f"PROGRESS TRANSPARENCY: The investigation has been in "
            f"{stage.value.upper()} for {investigative_turns} investigative turns "
            f"without reaching the next milestone.\n"
            f"Pending milestone: {milestone_name} — {milestone_info['description']}.\n"
            f"In your response, provide case-specific guidance on what evidence "
            f"would advance this milestone. Be concrete: name specific files, "
            f"services, time ranges, or commands based on what you know about "
            f"this case."
        )

        # Repair pattern injection (if detected)
        if repair:
            parts.append(repair.prompt_injection)

        return "\n\n".join(parts)

    # =========================================================================
    # Agent State Repair Patterns
    # =========================================================================

    def _check_repair_patterns(
        self, case: Case, stage: InvestigationStage
    ) -> Optional[RepairAction]:
        """
        Check for agent-internal failure patterns that require structural
        intervention. Only checked while in transparent mode.

        Args:
            case: Current investigation case
            stage: Current investigation stage

        Returns:
            RepairAction if a pattern is detected, None otherwise
        """
        if stage == InvestigationStage.DIAGNOSIS:
            return self._check_diagnosis_repairs(case)
        elif stage == InvestigationStage.MITIGATION:
            return self._check_mitigation_repairs(case)
        elif stage == InvestigationStage.TREATMENT:
            return self._check_treatment_repairs(case)
        return None

    def _check_diagnosis_repairs(self, case: Case) -> Optional[RepairAction]:
        """Check repair patterns relevant to DIAGNOSIS stage."""
        if self._detect_category_anchoring(case):
            return self._repair_anchoring(case)

        if self._detect_hypothesis_deadlock(case):
            return self._repair_deadlock(case)

        if self._detect_exhaustion(case):
            return self._repair_exhaustion(case)

        if self._detect_action_loop(case):
            return self._repair_action_loop(case)

        return None

    def _check_mitigation_repairs(self, case: Case) -> Optional[RepairAction]:
        """Check repair patterns relevant to MITIGATION stage."""
        if self._detect_fix_failure_cycle(case, action_type="mitigation"):
            return self._repair_fix_failure_cycle(case)

        if self._detect_action_loop(case):
            return self._repair_action_loop(case)

        return None

    def _check_treatment_repairs(self, case: Case) -> Optional[RepairAction]:
        """Check repair patterns relevant to TREATMENT stage."""
        if self._detect_fix_failure_cycle(case, action_type="solution"):
            return self._repair_fix_failure_cycle(case)

        if self._detect_category_anchoring(case):
            return self._repair_anchoring(case)

        if self._detect_hypothesis_deadlock(case):
            return self._repair_deadlock(case)

        if self._detect_action_loop(case):
            return self._repair_action_loop(case)

        return None

    # =========================================================================
    # Detection Methods
    # =========================================================================

    def _detect_category_anchoring(self, case: Case) -> bool:
        """
        Detect if agent is stuck testing same hypothesis category.

        Triggers if 4+ hypotheses in same category are REFUTED or INCONCLUSIVE.
        """
        category_counts: dict[str, int] = {}

        for hypothesis in case.hypotheses.values():
            if hypothesis.state in (
                HypothesisState.REFUTED,
                HypothesisState.INCONCLUSIVE,
            ):
                cat = (
                    hypothesis.category.value
                    if hasattr(hypothesis.category, "value")
                    else str(hypothesis.category)
                )
                category_counts[cat] = category_counts.get(cat, 0) + 1

        for category, count in category_counts.items():
            if count >= self.category_anchoring_threshold:
                logger.warning(
                    f"Category anchoring detected: {count} failed hypotheses "
                    f"in '{category}'"
                )
                return True

        return False

    def _detect_hypothesis_deadlock(self, case: Case) -> bool:
        """
        Detect if all hypotheses are inconclusive.

        Triggers when 3+ hypotheses exist and all are INCONCLUSIVE.
        """
        if not case.hypotheses or len(case.hypotheses) < 3:
            return False

        all_inconclusive = all(
            h.state == HypothesisState.INCONCLUSIVE for h in case.hypotheses.values()
        )

        if all_inconclusive:
            logger.warning(
                f"Hypothesis deadlock detected: all {len(case.hypotheses)} "
                f"hypotheses inconclusive"
            )
            return True

        return False

    def _detect_action_loop(self, case: Case) -> bool:
        """
        Detect if agent is repeating identical execution paths.

        Triggers if identical structural output across N consecutive turns.
        """
        if len(case.turn_history) < self.action_loop_threshold:
            return False

        recent_turns = case.turn_history[-self.action_loop_threshold :]

        fingerprints = []
        for turn in recent_turns:
            fp = (
                tuple(sorted(turn.milestones_completed)),
                tuple(sorted(turn.evidence_added)),
                tuple(sorted(turn.hypotheses_generated)),
                tuple(sorted(turn.hypotheses_validated)),
                turn.outcome.value if turn.outcome else "none",
            )
            fingerprints.append(fp)

        if len(set(fingerprints)) == 1:
            logger.warning(
                f"Action loop detected: identical structural output "
                f"in last {self.action_loop_threshold} turns"
            )
            return True

        return False

    def _detect_exhaustion(self, case: Case) -> bool:
        """
        Detect genuine dead end: broad diagnostic coverage, no convergence.

        The agent has done substantive work but can't reach a validated
        root cause.
        """
        if case.current_turn < self.exhaustion_min_turns:
            return False

        if case.turns_without_progress < self.exhaustion_stall_threshold:
            return False

        categories_explored = set()
        refuted_count = 0
        for h in case.hypotheses.values():
            cat = h.category.value if hasattr(h.category, "value") else str(h.category)
            categories_explored.add(cat)
            if h.state in (HypothesisState.REFUTED, HypothesisState.INCONCLUSIVE):
                refuted_count += 1

        evidence_count = len(case.evidence)

        if (
            len(categories_explored) >= 2
            and refuted_count >= 2
            and evidence_count >= 2
            and not any(
                h.state == HypothesisState.VALIDATED for h in case.hypotheses.values()
            )
        ):
            logger.info(
                f"Investigation exhaustion detected: {len(categories_explored)} "
                f"categories explored, {refuted_count} hypotheses refuted, "
                f"{evidence_count} evidence items, no validated hypothesis "
                f"after {case.current_turn} turns"
            )
            return True

        return False

    def _detect_fix_failure_cycle(self, case: Case, action_type: str) -> bool:
        """
        Detect repeated fix attempts that don't work.

        Triggers when N+ proposed actions of the given type have been accepted
        but the corresponding verification milestone has not been set.
        """
        if not case.proposed_actions:
            return False

        accepted_count = sum(
            1
            for action in case.proposed_actions
            if action.state == "accepted"
            and action.action_type.value.lower() == action_type
        )

        if accepted_count < self.fix_failure_threshold:
            return False

        if not case.progress:
            return False

        if action_type == "mitigation":
            # Post-redesign the mitigation verify gate lives on the
            # stabilization record, not a progress boolean.
            _stab = case.progress.stabilization
            verified = bool(_stab is not None and _stab.verified)
        elif action_type == "solution":
            verified = case.progress.solution_verified
        else:
            return False

        if not verified:
            logger.warning(
                f"Fix failure cycle detected: {accepted_count} {action_type} "
                f"attempts accepted but {action_type}_verified not set"
            )
            return True

        return False

    # =========================================================================
    # Repair Actions
    # =========================================================================

    # All hypothesis categories for suggesting alternatives
    ALL_CATEGORIES: List[str] = [
        "code",
        "config",
        "environment",
        "network",
        "data",
        "database",
        "hardware",
        "security",
        "external",
        "human",
        "other",
    ]

    def _repair_anchoring(self, case: Case) -> RepairAction:
        """
        Repair hypothesis category anchoring.

        Structural action: adds a category ban the LLM doesn't have.
        """
        anchored = self._find_anchored_category(case)
        alternatives = [c for c in self.ALL_CATEGORIES if c.lower() != anchored.lower()]
        alt_str = ", ".join(alternatives[:4])

        return RepairAction(
            repair_type=RepairType.HYPOTHESIS_ANCHORING,
            action="force_alternative_category",
            message=f"Category anchoring on '{anchored}'. Exploring: {alt_str}.",
            prompt_injection=(
                f"IMPORTANT: Do NOT propose hypotheses in '{anchored}' category. "
                f"This category has been explored extensively without success. "
                f"Try different categories like: {alt_str}"
            ),
        )

    def _repair_deadlock(self, case: Case) -> RepairAction:
        """
        Repair hypothesis deadlock.

        Structural action: retires all inconclusive hypotheses.
        """
        retired_count = 0
        for hypothesis in case.hypotheses.values():
            if hypothesis.state == HypothesisState.INCONCLUSIVE:
                hypothesis.state = HypothesisState.RETIRED
                retired_count += 1

        return RepairAction(
            repair_type=RepairType.HYPOTHESIS_DEADLOCK,
            action="reset_hypotheses",
            message=f"All hypotheses inconclusive. Retired {retired_count}.",
            prompt_injection=(
                "All previous hypotheses were inconclusive and have been retired. "
                "Generate new hypotheses grounded in specific evidence already "
                "collected. Each new hypothesis must cite specific findings from "
                "the evidence. Consider exploring different root cause categories."
            ),
        )

    def _repair_exhaustion(self, case: Case) -> RepairAction:
        """
        Handle genuine investigation exhaustion.

        Not a behavioral correction — the agent has done good work but
        evidence is insufficient.
        """
        categories_explored = set()
        refuted_count = 0
        for h in case.hypotheses.values():
            cat = h.category.value if hasattr(h.category, "value") else str(h.category)
            categories_explored.add(cat)
            if h.state in (HypothesisState.REFUTED, HypothesisState.INCONCLUSIVE):
                refuted_count += 1

        return RepairAction(
            repair_type=RepairType.EXHAUSTED,
            action="structured_handoff",
            message=(
                f"Investigation exhausted: {len(categories_explored)} categories "
                f"explored, {refuted_count} hypotheses refuted."
            ),
            prompt_injection=(
                "You have explored multiple diagnostic angles without reaching a "
                "definitive root cause. This is not a failure — produce a structured "
                "handoff: (1) Summarize what is established — the problem, scope, "
                "and key findings. (2) State what remains uncertain and why. "
                "(3) Present the user with options: specific data that would resolve "
                "the ambiguity, alternative diagnostic angles, escalation to a "
                "specialist, or pausing to resume later."
            ),
        )

    def _repair_fix_failure_cycle(self, case: Case) -> RepairAction:
        """
        Handle repeated fix attempts that don't work.
        """
        stage = case.current_stage
        stage_name = stage.value if stage else "current"

        failed_count = (
            sum(1 for a in case.proposed_actions if a.state == "accepted")
            if case.proposed_actions
            else 0
        )

        return RepairAction(
            repair_type=RepairType.FIX_FAILURE_CYCLE,
            action="structured_handoff",
            message=f"Fix failure cycle: {failed_count} attempts in {stage_name}.",
            prompt_injection=(
                f"Multiple {stage_name} attempts have been applied without success. "
                "Do not propose another fix of the same kind without new evidence. "
                "Produce a structured summary: (1) What was tried and what happened. "
                "(2) What these failures tell us about the root cause. "
                "(3) Options: different approach, additional diagnostic data needed, "
                "or escalation to a specialist."
            ),
        )

    def _repair_action_loop(self, case: Case) -> RepairAction:
        """
        Handle agent repeating identical execution paths.

        Current: prompt injection. Future: tool blocking.
        """

        return RepairAction(
            repair_type=RepairType.ACTION_LOOP,
            action="break_action_loop",
            message="Agent stuck in execution loop.",
            prompt_injection=(
                "You have produced identical output for multiple consecutive turns. "
                "Your reasoning may be stuck in a loop. Take a different approach: "
                "try a different tool, examine different evidence, or state what "
                "is blocking you."
            ),
        )

    # =========================================================================
    # Helpers
    # =========================================================================

    def _find_anchored_category(self, case: Case) -> str:
        """Find the category with most failed hypotheses."""
        category_counts: dict[str, int] = {}

        for hypothesis in case.hypotheses.values():
            if hypothesis.state in (
                HypothesisState.REFUTED,
                HypothesisState.INCONCLUSIVE,
            ):
                cat = (
                    hypothesis.category.value
                    if hasattr(hypothesis.category, "value")
                    else str(hypothesis.category)
                )
                category_counts[cat] = category_counts.get(cat, 0) + 1

        if not category_counts:
            return "unknown"

        return max(category_counts, key=category_counts.get)
