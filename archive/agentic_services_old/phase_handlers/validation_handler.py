"""Validation Handler - Phase 4: Systematic Hypothesis Testing

Phase 4 Characteristics:
- Engagement Mode: Lead Investigator
- OODA Steps: Full cycle (Observe, Orient, Decide, Act) - full intensity
- Expected Iterations: 3-6
- Tests: Hypotheses systematically with evidence

Objectives:
- Test hypotheses with specific evidence
- Update confidence based on results
- Detect and prevent anchoring bias
- Validate root cause (≥70% confidence)

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from typing import List, Optional

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    OODAStep,
    HypothesisStatus,
)
from faultmaven.models.evidence import EvidenceProvided, EvidenceRequest
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
from faultmaven.core.investigation.ooda_engine import AdaptiveIntensityController
from faultmaven.core.investigation.investigation_coordinator import InvestigationCoordinator, InterventionType
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn_from_diagnostic,
    get_evidence_for_requests,
    check_requests_complete,
    summarize_evidence_findings,
)
from faultmaven.core.investigation.workflow_progression_detector import (
    should_suggest_escalation,
)
from faultmaven.prompts.investigation.workflow_progression_prompts import (
    parse_suggest_escalation_response,
    get_workflow_transition_confirmation,
)


class ValidationHandler(BasePhaseHandler):
    """Phase 4: Validation - Systematic hypothesis testing

    Responsibilities:
    - Test hypotheses with targeted evidence requests
    - Update hypothesis confidence based on evidence
    - Apply confidence decay to stagnant hypotheses
    - Detect and prevent anchoring bias
    - Identify validated root cause
    """

    def __init__(self, *args, **kwargs):
        """Initialize validation handler"""
        super().__init__(*args, **kwargs)
        self.hypothesis_manager = create_hypothesis_manager()
        self.intensity_controller = AdaptiveIntensityController()
        self.coordinator = InvestigationCoordinator()
        self._pending_loop_back = None  # Proposal #6: Phase loop-back tracking

    def get_phase(self) -> InvestigationPhase:
        """Return Phase 4: Validation"""
        return InvestigationPhase.VALIDATION

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
        context: Optional[dict] = None,
        evidence_provided: Optional[List[EvidenceProvided]] = None,
        evidence_requests: Optional[List[EvidenceRequest]] = None,
    ) -> PhaseHandlerResult:
        """Handle Phase 4: Validation - Full OODA cycle

        Flow:
        1. Consume new evidence if provided
        2. Check for anchoring
        3. Determine OODA step
        4. Execute step logic
        5. Check phase completion
        6. Return result

        Args:
            investigation_state: Current investigation state
            user_query: User's query
            conversation_history: Recent conversation
            evidence_provided: List of evidence provided (from diagnostic state)
            evidence_requests: List of evidence requests (from diagnostic state)

        Returns:
            PhaseHandlerResult with response and state updates
        """
        # Check for pending workflow progression confirmation (v3.0)
        if investigation_state.lifecycle.pending_workflow_progression:
            pending = investigation_state.lifecycle.pending_workflow_progression
            if pending["type"] == "escalate":
                decision, is_ambiguous = parse_suggest_escalation_response(user_query)

                if not is_ambiguous:
                    if decision == "escalate":
                        # User confirmed escalation - mark as CLOSED
                        investigation_state.lifecycle.case_status = "closed"
                        investigation_state.lifecycle.pending_workflow_progression = None
                        investigation_state.lifecycle.workflow_progression_attempts = 0

                        confirmation_msg = get_workflow_transition_confirmation(
                            "escalate",
                            pending["details"]
                        )

                        return PhaseHandlerResult(
                            response_text=confirmation_msg,
                            updated_state=investigation_state,
                            phase_complete=True,
                            should_advance=False,  # CLOSED is terminal
                            made_progress=True,
                        )
                    elif decision == "continue":
                        # User wants to keep trying despite limitations
                        investigation_state.lifecycle.pending_workflow_progression = None
                        investigation_state.lifecycle.workflow_progression_attempts = 0

                        confirmation_msg = get_workflow_transition_confirmation(
                            "continue_despite_limits",
                            {}
                        )

                        return PhaseHandlerResult(
                            response_text=confirmation_msg,
                            updated_state=investigation_state,
                            phase_complete=False,
                            should_advance=False,
                            made_progress=True,
                        )
                # If ambiguous, let workflow progression prompt handle clarification

        # Consume new evidence if available
        if evidence_provided:
            await self._consume_validation_evidence(
                investigation_state,
                evidence_provided,
                evidence_requests or []
            )

        # Check for stalls, anchoring, and phase completion (coordinated)
        intervention = self.coordinator.check_interventions(investigation_state)
        if intervention:
            return await self._handle_intervention(investigation_state, user_query, intervention)

        # Determine current OODA step
        current_step = self.determine_ooda_step(investigation_state)

        if current_step == OODAStep.OBSERVE:
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.DECIDE:
            return await self._execute_decide(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.ACT:
            return await self._execute_act(investigation_state, user_query, conversation_history, context)
        else:
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)

    async def _consume_validation_evidence(
        self,
        investigation_state: InvestigationState,
        evidence_provided: List[EvidenceProvided],
        evidence_requests: List[EvidenceRequest],
    ) -> None:
        """Consume evidence provided by users and update hypothesis validation status.

        Args:
            investigation_state: Current investigation state (modified in-place)
            evidence_provided: All evidence provided
            evidence_requests: All evidence requests
        """
        # Get new evidence since last OODA iteration
        last_turn = (
            investigation_state.ooda_engine.iterations[-1].turn_number
            if investigation_state.ooda_engine.iterations
            else 0
        )
        new_evidence = get_new_evidence_since_turn_from_diagnostic(evidence_provided, last_turn)

        if not new_evidence:
            self.log_phase_action("No new evidence to consume")
            return

        self.log_phase_action(
            "Consuming validation evidence",
            {"new_evidence_count": len(new_evidence)}
        )

        # Update hypothesis validation status based on evidence
        for hypothesis in investigation_state.ooda_engine.hypotheses:
            if hypothesis.status not in [HypothesisStatus.ACTIVE, HypothesisStatus.ACTIVE]:
                continue

            # Get validation request IDs for this hypothesis
            # Note: This requires hypothesis to have validation_request_ids attribute
            # If not available, we check evidence that addresses hypothesis-related requests
            validation_request_ids = getattr(hypothesis, 'validation_request_ids', [])

            if not validation_request_ids:
                # Fall back to checking if evidence mentions this hypothesis
                hypothesis_evidence = [
                    e for e in new_evidence
                    if hypothesis.hypothesis_id in e.metadata.get('for_hypothesis_id', '')
                    or hypothesis.statement[:50] in e.content
                ]
            else:
                # Get evidence for this hypothesis's validation requests
                hypothesis_evidence = get_evidence_for_requests(
                    new_evidence,
                    validation_request_ids
                )

            if not hypothesis_evidence:
                continue

            # Analyze evidence types
            supportive = [
                e for e in hypothesis_evidence
                if e.evidence_type.value == "supportive"
            ]
            refuting = [
                e for e in hypothesis_evidence
                if e.evidence_type.value == "refuting"
            ]
            neutral = [
                e for e in hypothesis_evidence
                if e.evidence_type.value == "neutral"
            ]

            # Update hypothesis status and confidence
            if len(supportive) > len(refuting):
                # More supportive evidence
                confidence_boost = min(0.2, len(supportive) * 0.1)
                hypothesis.likelihood = min(1.0, hypothesis.likelihood + confidence_boost)
                hypothesis.supporting_evidence.extend([e.evidence_id for e in supportive])

                if hypothesis.likelihood >= 0.7:
                    hypothesis.status = HypothesisStatus.VALIDATED
                    self.log_phase_action(
                        "Hypothesis validated",
                        {
                            "hypothesis_id": hypothesis.hypothesis_id,
                            "confidence": hypothesis.likelihood
                        }
                    )
                else:
                    hypothesis.status = HypothesisStatus.ACTIVE

            elif len(refuting) > len(supportive):
                # More refuting evidence
                confidence_drop = min(0.3, len(refuting) * 0.15)
                hypothesis.likelihood = max(0.0, hypothesis.likelihood - confidence_drop)
                hypothesis.refuting_evidence.extend([e.evidence_id for e in refuting])

                if hypothesis.likelihood < 0.3:
                    hypothesis.status = HypothesisStatus.REFUTED
                    self.log_phase_action(
                        "Hypothesis refuted",
                        {
                            "hypothesis_id": hypothesis.hypothesis_id,
                            "confidence": hypothesis.likelihood
                        }
                    )
                else:
                    hypothesis.status = HypothesisStatus.ACTIVE

            # Update last progress tracking
            if supportive or refuting:
                hypothesis.last_progress_at_turn = investigation_state.metadata.current_turn
                hypothesis.iterations_without_progress = 0

            self.log_phase_action(
                "Updated hypothesis based on evidence",
                {
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "supportive": len(supportive),
                    "refuting": len(refuting),
                    "neutral": len(neutral),
                    "new_confidence": hypothesis.likelihood,
                    "status": hypothesis.status.value,
                }
            )

    async def _execute_observe(self, investigation_state, user_query, conversation_history, context: Optional[dict] = None) -> PhaseHandlerResult:
        """Execute OODA Observe: Request testing evidence"""
        if not investigation_state.ooda_engine.iterations or investigation_state.ooda_engine.iterations[-1].steps_completed:
            iteration = self.start_new_ooda_iteration(investigation_state)
            investigation_state.ooda_engine.iterations.append(iteration)

        # Get testable hypotheses
        testable = self.hypothesis_manager.get_testable_hypotheses(
            investigation_state.ooda_engine.hypotheses, max_count=1
        )

        evidence_requests = []
        if testable:
            top_hypothesis = testable[0]
            evidence_requests.append(
                self.create_evidence_request(
                    label=f"Test: {top_hypothesis.statement[:50]}",
                    description=f"Evidence to test hypothesis: {top_hypothesis.statement}",
                    category="configuration",
                    for_hypothesis_id=top_hypothesis.hypothesis_id,
                    priority=1,
                )
            )

        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=self._format_state_for_prompt(investigation_state),
            conversation_history=conversation_history,
            user_query=user_query,
        )

        from faultmaven.models.responses import LeadInvestigatorResponse

        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            context=context,
            
            expected_schema=LeadInvestigatorResponse,
        )

        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.OBSERVE)
        self.log_phase_action("Observe step complete")

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.OBSERVE,
            evidence_requests_generated=evidence_requests,
            made_progress=True,
        )

    async def _execute_orient(self, investigation_state, user_query, conversation_history, context: Optional[dict] = None) -> PhaseHandlerResult:
        """Execute OODA Orient: Analyze test results"""
        # Apply confidence decay to stagnant hypotheses
        current_turn = investigation_state.metadata.current_turn
        for hypothesis in investigation_state.ooda_engine.hypotheses:
            if hypothesis.status == HypothesisStatus.ACTIVE:
                self.hypothesis_manager.apply_confidence_decay(hypothesis, current_turn)

        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=self._format_state_for_prompt(investigation_state),
            conversation_history=conversation_history,
            user_query=user_query,
        )

        from faultmaven.models.responses import LeadInvestigatorResponse

        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            context=context,
            
            expected_schema=LeadInvestigatorResponse,
        )

        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ORIENT)
        self.log_phase_action("Orient step complete")

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ORIENT,
            made_progress=True,
        )

    async def _execute_decide(self, investigation_state, user_query, conversation_history, context: Optional[dict] = None) -> PhaseHandlerResult:
        """Execute OODA Decide: Choose next hypothesis to test"""
        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=self._format_state_for_prompt(investigation_state),
            conversation_history=conversation_history,
            user_query=user_query,
        )

        from faultmaven.models.responses import LeadInvestigatorResponse

        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            context=context,
            
            expected_schema=LeadInvestigatorResponse,
        )

        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.DECIDE)
        self.log_phase_action("Decide step complete")

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.DECIDE,
            made_progress=True,
        )

    async def _execute_act(self, investigation_state, user_query, conversation_history, context: Optional[dict] = None) -> PhaseHandlerResult:
        """Execute OODA Act: Execute test"""
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ACT)
        current_iteration.completed_at_turn = investigation_state.metadata.current_turn
        current_iteration.hypotheses_tested += 1

        # Check phase completion
        is_complete, met, unmet = await self.check_completion(investigation_state)

        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=self._format_state_for_prompt(investigation_state),
            conversation_history=conversation_history,
            user_query=user_query,
        )

        from faultmaven.models.responses import LeadInvestigatorResponse

        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            context=context,
            
            expected_schema=LeadInvestigatorResponse,
        )

        self.log_phase_action("Act step complete")

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ACT,
            iteration_complete=True,
            phase_complete=is_complete,
            should_advance=is_complete,
            next_phase=InvestigationPhase.SOLUTION if is_complete else None,
            made_progress=True,
        )

    async def _check_anchoring(self, investigation_state) -> tuple[bool, str]:
        """Check for anchoring bias"""
        current_iter = investigation_state.ooda_engine.current_iteration
        is_anchored, reason, affected = self.hypothesis_manager.detect_anchoring(
            investigation_state.ooda_engine.hypotheses,
            current_iter,
        )
        return is_anchored, reason

    async def _handle_intervention(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        intervention
    ) -> PhaseHandlerResult:
        """Handle coordinated intervention (anchoring or phase completion)

        Args:
            investigation_state: Current investigation state
            user_query: User's query
            intervention: InterventionPlan from coordinator

        Returns:
            PhaseHandlerResult with intervention response
        """
        self.log_phase_action(
            "Intervention triggered",
            {
                "type": intervention.intervention_type.value,
                "reason": intervention.reason,
                "priority": intervention.priority
            }
        )

        # Handle based on intervention type
        if intervention.intervention_type == InterventionType.ANCHORING_PREVENTION:
            return await self._handle_anchoring_intervention(investigation_state, intervention.reason)

        elif intervention.intervention_type == InterventionType.PHASE_COMPLETION:
            # Phase complete → return with should_advance=True
            return PhaseHandlerResult(
                response_text="Phase 4 (Validation) complete. Root cause identified.",
                updated_state=investigation_state,
                phase_complete=True,
                should_advance=True,
                next_phase=InvestigationPhase.SOLUTION,
                made_progress=True
            )

        else:
            # Unknown intervention type → log and continue normal flow
            self.log_phase_action(
                "Unknown intervention type",
                {"type": intervention.intervention_type.value}
            )
            return await self._execute_observe(investigation_state, user_query, "", None)

    async def _handle_anchoring_intervention(
        self,
        investigation_state: InvestigationState,
        reason: str
    ) -> PhaseHandlerResult:
        """Handle anchoring intervention (existing logic)

        Args:
            investigation_state: Current investigation state
            reason: Anchoring reason

        Returns:
            PhaseHandlerResult with anchoring response
        """
        self.hypothesis_manager.force_alternative_generation(
            investigation_state.ooda_engine.hypotheses,
            investigation_state.metadata.current_turn,
        )

        investigation_state.ooda_engine.anchoring_detected = True
        investigation_state.ooda_engine.forced_alternatives_at_turn.append(
            investigation_state.metadata.current_turn
        )

        response = f"Anchoring detected: {reason}\n\nForcing alternative perspectives to break the pattern..."

        return PhaseHandlerResult(
            response_text=response,
            updated_state=investigation_state,
            made_progress=True,
        )

    async def _handle_anchoring(self, investigation_state, user_query, reason) -> PhaseHandlerResult:
        """Handle anchoring by forcing alternative generation"""
        result = self.hypothesis_manager.force_alternative_generation(
            investigation_state.ooda_engine.hypotheses,
            investigation_state.metadata.current_turn,
        )

        investigation_state.ooda_engine.anchoring_detected = True
        investigation_state.ooda_engine.forced_alternatives_at_turn.append(investigation_state.metadata.current_turn)

        response = f"Anchoring detected: {reason}\n\nForcing alternative perspectives to break the pattern..."

        self.log_phase_action("Anchoring prevention triggered", {"reason": reason})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            made_progress=True,
        )

    async def check_completion(self, investigation_state) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 4 completion criteria met (v3.0/v3.1)

        Completion Criteria (v3.0):
        1. Normal mode: Hypothesis validated with ≥70% confidence
        2. Degraded mode exception (v3.1): Advance when confidence cap reached
        3. Loop-back: All hypotheses refuted or scope/timeline wrong

        Also checks for loop-back conditions:
        - All hypotheses refuted → loop back to Phase 3
        - Scope changed → loop back to Phase 1
        - Timeline wrong → loop back to Phase 2
        """
        met_criteria = []
        unmet_criteria = []

        # Check if should suggest escalation (v3.0 workflow progression)
        should_suggest, escalation_details = should_suggest_escalation(investigation_state)

        if should_suggest:
            # Set pending workflow progression
            investigation_state.lifecycle.pending_workflow_progression = {
                "type": "escalate",
                "from_phase": investigation_state.lifecycle.current_phase,
                "to_phase": None,  # Terminal (CLOSED)
                "rationale": "Investigation blocked by limitations",
                "details": escalation_details,
                "suggested_at_turn": investigation_state.metadata.current_turn,
            }
            investigation_state.lifecycle.workflow_progression_attempts = 0

            unmet_criteria.append("⏳ User confirmation needed: Close and escalate investigation?")

        # Check for loop-back conditions first (Proposal #6, v3.0)
        from faultmaven.core.investigation.phase_loopback import should_loop_back_from_validation

        should_loop, outcome, reason = should_loop_back_from_validation(investigation_state)
        if should_loop:
            # Store loop-back information for orchestrator to handle
            investigation_state.lifecycle.phase_complete = False
            self._pending_loop_back = (outcome, reason)
            unmet_criteria.append(f"Loop-back needed: {reason.value if reason else 'unknown'}")
            return False, met_criteria, unmet_criteria

        # Get current working conclusion for confidence level
        working_conclusion = investigation_state.lifecycle.working_conclusion
        escalation_state = investigation_state.lifecycle.escalation_state

        # v3.0 NORMAL COMPLETION: ≥70% confidence threshold
        validated = self.hypothesis_manager.get_validated_hypothesis(investigation_state.ooda_engine.hypotheses)
        if validated and validated.likelihood >= 0.70:
            met_criteria.append(f"Hypothesis validated (confidence: {validated.likelihood:.0%})")
            return True, met_criteria, unmet_criteria

        # v3.1 DEGRADED MODE EXCEPTION: Can advance when confidence cap reached
        if escalation_state.operating_in_degraded_mode:
            confidence_cap = escalation_state.get_confidence_cap()
            if confidence_cap is not None and working_conclusion:
                # If we've reached the confidence cap (within 5% margin)
                if working_conclusion.confidence >= (confidence_cap - 0.05):
                    met_criteria.append(
                        f"Degraded mode: Confidence cap reached ({working_conclusion.confidence:.0%} "
                        f"at {confidence_cap:.0%} cap)"
                    )
                    met_criteria.append(
                        f"Degraded mode type: {escalation_state.degraded_mode_type.value}"
                    )
                    return True, met_criteria, unmet_criteria
                else:
                    unmet_criteria.append(
                        f"Degraded mode: Confidence {working_conclusion.confidence:.0%} "
                        f"below cap {confidence_cap:.0%}"
                    )
            else:
                unmet_criteria.append("Degraded mode: No confidence cap or working conclusion")
        else:
            # Normal mode but not validated
            if working_conclusion:
                unmet_criteria.append(
                    f"No hypothesis validated with ≥70% confidence "
                    f"(current: {working_conclusion.confidence:.0%})"
                )
            else:
                unmet_criteria.append("No hypothesis validated with ≥70% confidence")

        # Check max iterations (prevents infinite loops)
        if investigation_state.ooda_engine.current_iteration >= 6:
            met_criteria.append("Max iterations reached (6)")
            # After 6 iterations without reaching threshold, suggest degraded mode or loop-back
            return True, met_criteria, unmet_criteria

        return False, met_criteria, unmet_criteria

    def _format_state_for_prompt(self, state) -> dict:
        """Format state for prompt context"""
        return {
            "hypotheses": [{"statement": h.statement, "likelihood": h.likelihood, "status": h.status.value} for h in state.ooda_engine.hypotheses],
            "current_iteration": state.ooda_engine.current_iteration,
        }
