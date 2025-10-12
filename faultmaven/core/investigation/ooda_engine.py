"""OODA Engine - Observe-Orient-Decide-Act Execution Manager

This module implements the OODA (Observe-Orient-Decide-Act) tactical execution
engine for FaultMaven's investigation framework. It manages iteration cycles,
adaptive intensity control, and step-by-step execution within each phase.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md

OODA Framework:
- Observe: Gather information and evidence (generate evidence requests)
- Orient: Analyze and contextualize data (process evidence, update hypotheses)
- Decide: Choose action or hypothesis (select test, prioritize)
- Act: Execute test or apply solution (verify, implement)

Adaptive Intensity:
- 1-2 iterations: Light intensity (simple problems)
- 3-5 iterations: Medium intensity (typical investigations)
- 6+ iterations: Full intensity (complex root causes)
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from faultmaven.models.investigation import (
    InvestigationPhase,
    OODAStep,
    OODAIteration,
    InvestigationState,
    Hypothesis,
    HypothesisStatus,
    HypothesisTest,
    PhaseOODAMapping,
)
from faultmaven.core.investigation.phases import (
    get_phase_definition,
    get_ooda_steps_for_phase,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Adaptive Intensity Controller
# =============================================================================


class AdaptiveIntensityController:
    """Controls investigation intensity based on iteration count and complexity

    Intensity levels determine thoroughness of investigation:
    - Light: Quick assessment, 1-2 iterations
    - Medium: Standard investigation, 3-5 iterations
    - Full: Deep analysis, 6+ iterations with anchoring prevention
    """

    @staticmethod
    def get_intensity_level(iteration_count: int, phase: InvestigationPhase) -> str:
        """Determine current intensity level

        Args:
            iteration_count: Number of OODA iterations in current phase
            phase: Current investigation phase

        Returns:
            Intensity level: "light", "medium", or "full"
        """
        # Phase 0 has no OODA
        if phase == InvestigationPhase.INTAKE:
            return "none"

        # Phases 1-2: Always light (1-2 iterations expected)
        if phase in [InvestigationPhase.BLAST_RADIUS, InvestigationPhase.TIMELINE]:
            return "light"

        # Phase 3: Medium (2-3 iterations)
        if phase == InvestigationPhase.HYPOTHESIS:
            if iteration_count <= 2:
                return "light"
            return "medium"

        # Phase 4: Full intensity (3-6+ iterations)
        if phase == InvestigationPhase.VALIDATION:
            if iteration_count <= 2:
                return "medium"
            return "full"

        # Phase 5: Medium (2-4 iterations)
        if phase == InvestigationPhase.SOLUTION:
            return "medium"

        # Phase 6: Light (1 iteration)
        if phase == InvestigationPhase.DOCUMENT:
            return "light"

        return "medium"  # Default

    @staticmethod
    def should_trigger_anchoring_prevention(
        iteration_count: int,
        hypotheses: List[Hypothesis],
    ) -> Tuple[bool, Optional[str]]:
        """Check if anchoring prevention should be triggered

        Anchoring conditions:
        1. 4+ hypotheses in same category
        2. 3 iterations without confidence improvement
        3. Repeated testing of same hypothesis

        Args:
            iteration_count: Current OODA iteration count
            hypotheses: List of active hypotheses

        Returns:
            Tuple of (should_trigger, reason)
        """
        if iteration_count < 3:
            return False, None

        # Condition 1: Too many hypotheses in same category
        category_counts: Dict[str, int] = {}
        for h in hypotheses:
            if h.status not in [HypothesisStatus.RETIRED, HypothesisStatus.REFUTED]:
                category_counts[h.category] = category_counts.get(h.category, 0) + 1

        for category, count in category_counts.items():
            if count >= 4:
                return True, f"Anchoring detected: {count} hypotheses in '{category}' category"

        # Condition 2: No progress in 3+ iterations
        stalled_hypotheses = [
            h
            for h in hypotheses
            if h.iterations_without_progress >= 3
            and h.status == HypothesisStatus.TESTING
        ]
        if stalled_hypotheses:
            return True, f"Anchoring detected: {len(stalled_hypotheses)} hypotheses stalled"

        # Condition 3: Check if top hypothesis hasn't changed in 3 iterations
        active_hypotheses = [
            h
            for h in hypotheses
            if h.status not in [HypothesisStatus.RETIRED, HypothesisStatus.REFUTED]
        ]
        if active_hypotheses:
            sorted_by_likelihood = sorted(
                active_hypotheses, key=lambda h: h.likelihood, reverse=True
            )
            if sorted_by_likelihood:
                top_hypothesis = sorted_by_likelihood[0]
                iterations_as_top = iteration_count - top_hypothesis.last_updated_turn
                if iterations_as_top >= 3 and top_hypothesis.likelihood < 0.7:
                    return True, "Anchoring detected: Top hypothesis unchanged for 3+ iterations"

        return False, None


# =============================================================================
# OODA Engine
# =============================================================================


class OODAEngine:
    """OODA (Observe-Orient-Decide-Act) execution engine

    Manages tactical execution within each investigation phase:
    - Starts new OODA iterations
    - Executes individual OODA steps
    - Tracks progress and detects stalls
    - Applies adaptive intensity control
    - Triggers anchoring prevention when needed
    """

    def __init__(self):
        """Initialize OODA engine"""
        self.intensity_controller = AdaptiveIntensityController()
        self.logger = logging.getLogger(__name__)

    def start_new_iteration(
        self,
        investigation_state: InvestigationState,
    ) -> OODAIteration:
        """Start a new OODA iteration in current phase

        Args:
            investigation_state: Current investigation state

        Returns:
            New OODAIteration object
        """
        current_phase = investigation_state.lifecycle.current_phase
        iteration_num = investigation_state.ooda_engine.current_iteration + 1

        # Get active OODA steps for this phase
        active_steps = get_ooda_steps_for_phase(current_phase)

        iteration = OODAIteration(
            iteration_number=iteration_num,
            phase=current_phase,
            started_at_turn=investigation_state.metadata.current_turn,
            steps_completed=[],
            steps_skipped=[step for step in OODAStep if step not in active_steps],
        )

        self.logger.info(
            f"Started OODA iteration {iteration_num} in phase {current_phase.name}, "
            f"active steps: {[s.value for s in active_steps]}"
        )

        return iteration

    def execute_observe_step(
        self,
        investigation_state: InvestigationState,
    ) -> Dict[str, Any]:
        """Execute OODA Observe step - Gather information and evidence

        During Observe:
        - Generate evidence requests based on current needs
        - Identify information gaps
        - Prioritize critical evidence

        Args:
            investigation_state: Current investigation state

        Returns:
            Step execution result with evidence requests generated
        """
        phase = investigation_state.lifecycle.current_phase
        self.logger.info(f"Executing OODA Observe step in phase {phase.name}")

        result = {
            "step": OODAStep.OBSERVE.value,
            "evidence_requests_generated": [],
            "information_gaps": [],
            "observations": [],
        }

        # Phase-specific observation logic
        if phase == InvestigationPhase.BLAST_RADIUS:
            result["information_gaps"] = ["scope", "affected_components", "severity"]

        elif phase == InvestigationPhase.TIMELINE:
            result["information_gaps"] = ["start_time", "recent_changes", "deployment_history"]

        elif phase == InvestigationPhase.HYPOTHESIS:
            result["information_gaps"] = ["configuration", "environment", "metrics"]

        elif phase == InvestigationPhase.VALIDATION:
            # Generate evidence requests for hypothesis testing
            for hypothesis in investigation_state.ooda_engine.hypotheses:
                if hypothesis.status == HypothesisStatus.TESTING:
                    result["information_gaps"].append(f"test_evidence_for_{hypothesis.hypothesis_id}")

        return result

    def execute_orient_step(
        self,
        investigation_state: InvestigationState,
        evidence_collected: List[str],
    ) -> Dict[str, Any]:
        """Execute OODA Orient step - Analyze and contextualize data

        During Orient:
        - Process collected evidence
        - Update AnomalyFrame if needed
        - Refine hypotheses based on new information
        - Detect patterns and correlations

        Args:
            investigation_state: Current investigation state
            evidence_collected: List of evidence IDs collected in Observe step

        Returns:
            Step execution result with analysis and insights
        """
        phase = investigation_state.lifecycle.current_phase
        self.logger.info(f"Executing OODA Orient step in phase {phase.name}")

        result = {
            "step": OODAStep.ORIENT.value,
            "anomaly_updated": False,
            "hypotheses_updated": 0,
            "insights": [],
            "patterns_detected": [],
        }

        # Phase-specific orientation logic
        if phase == InvestigationPhase.BLAST_RADIUS:
            # Create or update AnomalyFrame
            if investigation_state.ooda_engine.anomaly_frame is None:
                result["insights"].append("AnomalyFrame creation needed")
            else:
                result["anomaly_updated"] = True

        elif phase == InvestigationPhase.TIMELINE:
            # Correlate timeline with changes
            result["patterns_detected"].append("temporal_correlation")

        elif phase == InvestigationPhase.HYPOTHESIS:
            # Analyze evidence to refine hypothesis confidence
            result["hypotheses_updated"] = len(investigation_state.ooda_engine.hypotheses)

        elif phase == InvestigationPhase.VALIDATION:
            # Update hypothesis confidence based on test evidence
            for hypothesis in investigation_state.ooda_engine.hypotheses:
                if hypothesis.status == HypothesisStatus.TESTING:
                    # Check if evidence supports or refutes
                    result["hypotheses_updated"] += 1

        return result

    def execute_decide_step(
        self,
        investigation_state: InvestigationState,
    ) -> Dict[str, Any]:
        """Execute OODA Decide step - Choose action or hypothesis

        During Decide:
        - Select hypothesis to test (Phase 4)
        - Choose solution approach (Phase 5)
        - Prioritize next actions
        - Make strategic choices

        Args:
            investigation_state: Current investigation state

        Returns:
            Step execution result with decision made
        """
        phase = investigation_state.lifecycle.current_phase
        self.logger.info(f"Executing OODA Decide step in phase {phase.name}")

        result = {
            "step": OODAStep.DECIDE.value,
            "decision": None,
            "selected_hypothesis": None,
            "action_plan": [],
        }

        if phase == InvestigationPhase.HYPOTHESIS:
            # Rank hypotheses by likelihood
            active_hypotheses = [
                h
                for h in investigation_state.ooda_engine.hypotheses
                if h.status not in [HypothesisStatus.RETIRED, HypothesisStatus.REFUTED]
            ]
            if active_hypotheses:
                sorted_hyp = sorted(active_hypotheses, key=lambda h: h.likelihood, reverse=True)
                result["decision"] = "hypothesis_ranking_complete"
                result["selected_hypothesis"] = sorted_hyp[0].hypothesis_id

        elif phase == InvestigationPhase.VALIDATION:
            # Select next hypothesis to test
            testing_candidates = [
                h
                for h in investigation_state.ooda_engine.hypotheses
                if h.status == HypothesisStatus.PENDING
            ]
            if testing_candidates:
                # Select highest likelihood hypothesis
                sorted_candidates = sorted(
                    testing_candidates, key=lambda h: h.likelihood, reverse=True
                )
                result["selected_hypothesis"] = sorted_candidates[0].hypothesis_id
                result["decision"] = "test_hypothesis"

        elif phase == InvestigationPhase.SOLUTION:
            # Decide on solution implementation approach
            result["decision"] = "solution_approach_selected"
            result["action_plan"] = ["propose_solution", "verify_fix"]

        return result

    def execute_act_step(
        self,
        investigation_state: InvestigationState,
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute OODA Act step - Execute test or apply solution

        During Act:
        - Execute hypothesis test (Phase 4)
        - Apply solution (Phase 5)
        - Verify results
        - Collect feedback

        Args:
            investigation_state: Current investigation state
            decision: Decision from Decide step

        Returns:
            Step execution result with action taken
        """
        phase = investigation_state.lifecycle.current_phase
        self.logger.info(f"Executing OODA Act step in phase {phase.name}")

        result = {
            "step": OODAStep.ACT.value,
            "action_taken": None,
            "test_executed": False,
            "solution_applied": False,
            "verification_result": None,
        }

        if phase == InvestigationPhase.VALIDATION:
            # Execute hypothesis test
            if decision.get("selected_hypothesis"):
                result["action_taken"] = "hypothesis_test_executed"
                result["test_executed"] = True

        elif phase == InvestigationPhase.SOLUTION:
            # Apply solution (user action, agent verifies)
            result["action_taken"] = "solution_proposed"
            result["solution_applied"] = True

        return result

    def complete_iteration(
        self,
        investigation_state: InvestigationState,
        iteration: OODAIteration,
    ) -> Tuple[bool, List[str]]:
        """Complete current OODA iteration and assess progress

        Args:
            investigation_state: Current investigation state
            iteration: Completed iteration

        Returns:
            Tuple of (made_progress, insights)
        """
        iteration.completed_at_turn = investigation_state.metadata.current_turn
        iteration.duration_turns = (
            iteration.completed_at_turn - iteration.started_at_turn
        )

        # Assess if progress was made
        progress_indicators = []

        # Check if new evidence collected
        if iteration.new_evidence_collected > 0:
            progress_indicators.append(f"Collected {iteration.new_evidence_collected} evidence")

        # Check if hypotheses changed
        if iteration.hypotheses_generated > 0:
            progress_indicators.append(f"Generated {iteration.hypotheses_generated} hypotheses")

        if iteration.hypotheses_tested > 0:
            progress_indicators.append(f"Tested {iteration.hypotheses_tested} hypotheses")

        # Check confidence improvement
        if iteration.confidence_delta > 0:
            progress_indicators.append(f"Confidence improved by {iteration.confidence_delta:.2f}")

        # Check for anomaly refinement
        if iteration.anomaly_refined:
            progress_indicators.append("AnomalyFrame updated")

        made_progress = len(progress_indicators) > 0
        iteration.made_progress = made_progress

        if not made_progress:
            iteration.stall_reason = "No measurable progress in this iteration"

        self.logger.info(
            f"Iteration {iteration.iteration_number} complete: "
            f"progress={made_progress}, indicators={progress_indicators}"
        )

        return made_progress, iteration.new_insights

    def should_continue_iterations(
        self,
        investigation_state: InvestigationState,
    ) -> Tuple[bool, str]:
        """Determine if more OODA iterations needed in current phase

        Args:
            investigation_state: Current investigation state

        Returns:
            Tuple of (should_continue, reason)
        """
        phase = investigation_state.lifecycle.current_phase
        current_iter = investigation_state.ooda_engine.current_iteration
        phase_def = get_phase_definition(phase)

        min_iters, max_iters = phase_def.expected_iterations

        # Check if max iterations reached
        if current_iter >= max_iters:
            return False, f"Max iterations ({max_iters}) reached for phase {phase.name}"

        # Check if minimum iterations not yet met
        if current_iter < min_iters:
            return True, f"Minimum iterations ({min_iters}) not yet reached"

        # Check for anchoring
        should_trigger, reason = self.intensity_controller.should_trigger_anchoring_prevention(
            current_iter, investigation_state.ooda_engine.hypotheses
        )
        if should_trigger:
            investigation_state.ooda_engine.anchoring_detected = True
            return True, f"Continue to address anchoring: {reason}"

        # Check phase-specific completion
        if phase == InvestigationPhase.VALIDATION:
            # Continue until hypothesis validated
            validated = any(
                h.status == HypothesisStatus.VALIDATED
                and h.likelihood >= 0.7
                for h in investigation_state.ooda_engine.hypotheses
            )
            if not validated:
                return True, "No validated hypothesis yet"

        # Phase completion criteria met
        return False, "Phase objectives achieved"


# =============================================================================
# Utility Functions
# =============================================================================


def create_ooda_engine() -> OODAEngine:
    """Factory function to create OODA engine instance

    Returns:
        Configured OODAEngine instance
    """
    return OODAEngine()
