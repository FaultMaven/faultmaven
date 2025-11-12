"""Solution Handler - Phase 5: Fix Implementation and Verification

Phase 5 Characteristics:
- Engagement Mode: Lead Investigator
- OODA Steps: Decide, Act, Orient (medium intensity)
- Expected Iterations: 2-4
- Implements: Solution and verifies fix

Objectives:
- Propose solution based on validated root cause
- Guide implementation
- Verify problem resolved
- Confirm success criteria met

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from typing import List, Optional

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    OODAStep,
)
from faultmaven.models.evidence import EvidenceProvided, EvidenceRequest
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn_from_diagnostic,
    summarize_evidence_findings,
)
from faultmaven.core.investigation.workflow_progression_detector import (
    should_suggest_escalation,
    should_suggest_mark_complete,
)
from faultmaven.prompts.investigation.workflow_progression_prompts import (
    parse_mark_complete_response,
    parse_suggest_escalation_response,
    get_workflow_transition_confirmation,
)


class SolutionHandler(BasePhaseHandler):
    """Phase 5: Solution - Implement fix and verify

    Responsibilities:
    - Propose solution addressing root cause
    - Provide implementation guidance
    - Request verification evidence
    - Confirm problem resolved
    """

    def get_phase(self) -> InvestigationPhase:
        """Return Phase 5: Solution"""
        return InvestigationPhase.SOLUTION

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
        context: Optional[dict] = None,
        evidence_provided: Optional[List[EvidenceProvided]] = None,
        evidence_requests: Optional[List[EvidenceRequest]] = None,
    ) -> PhaseHandlerResult:
        """Handle Phase 5: Solution (v3.0 - THREE ENTRY MODES)

        Entry Modes (v3.0/v3.1):
        1. MODE 1 (Normal): From Phase 4 with validated hypothesis (≥70% confidence)
        2. MODE 2 (Fast Recovery): Direct from Phase 1 (user confirmed urgent mitigation)
        3. MODE 3 (Degraded): From Phase 4 in degraded mode (confidence capped <70%)

        Flow:
        1. Detect entry mode based on phase_entry_history
        2. Check for solution verification evidence
        3. Determine OODA step (Decide, Act, or Orient)
        4. Execute step logic with mode-appropriate context
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

            # Handle escalation confirmation
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

            # Handle mark complete confirmation
            elif pending["type"] == "mark_complete":
                decision, is_ambiguous = parse_mark_complete_response(user_query)

                if not is_ambiguous:
                    if decision == "complete":
                        # User confirmed - mark as RESOLVED and advance to Phase 6
                        investigation_state.lifecycle.case_status = "resolved"
                        investigation_state.lifecycle.pending_workflow_progression = None
                        investigation_state.lifecycle.workflow_progression_attempts = 0

                        confirmation_msg = get_workflow_transition_confirmation(
                            "mark_complete",
                            pending["details"]
                        )

                        return PhaseHandlerResult(
                            response_text=confirmation_msg,
                            updated_state=investigation_state,
                            phase_complete=True,
                            should_advance=True,
                            next_phase=InvestigationPhase.DOCUMENT,
                            made_progress=True,
                        )
                    elif decision == "more_verification":
                        # User wants more verification - continue Phase 5
                        investigation_state.lifecycle.pending_workflow_progression = None
                        investigation_state.lifecycle.workflow_progression_attempts = 0

                        confirmation_msg = get_workflow_transition_confirmation(
                            "more_verification",
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
                # (orchestrator will show clarification prompt)

        # v3.0: Detect entry mode
        entry_mode = self._detect_entry_mode(investigation_state)

        # Store entry mode in context for prompt assembly
        if context is None:
            context = {}
        context['phase5_entry_mode'] = entry_mode

        # Enrich context with solution feedback if available
        additional_context = ""
        if evidence_provided:
            last_turn = (
                investigation_state.ooda_engine.iterations[-1].turn_number
                if investigation_state.ooda_engine.iterations
                else 0
            )
            new_evidence = get_new_evidence_since_turn_from_diagnostic(evidence_provided, last_turn)

            if new_evidence:
                evidence_summary = summarize_evidence_findings(new_evidence)
                additional_context = (
                    f"\n\n## Solution Feedback:\n{evidence_summary}\n\n"
                    f"Adjust solution based on this feedback."
                )
                self.log_phase_action(
                    "Enriching context with solution feedback",
                    {"evidence_count": len(new_evidence)}
                )

        # Append additional context to conversation history
        if additional_context:
            conversation_history = conversation_history + additional_context

        # Determine current OODA step
        current_step = self.determine_ooda_step(investigation_state)

        if current_step == OODAStep.DECIDE:
            return await self._execute_decide(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.ACT:
            return await self._execute_act(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history, context)
        else:
            return await self._execute_decide(investigation_state, user_query, conversation_history, context)

    async def _execute_decide(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Execute OODA Decide: Propose solution

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with solution proposal
        """
        # Start new OODA iteration if needed
        if not investigation_state.ooda_engine.iterations or \
           investigation_state.ooda_engine.iterations[-1].steps_completed:
            iteration = self.start_new_ooda_iteration(investigation_state)
            investigation_state.ooda_engine.iterations.append(iteration)

        # Get validated root cause
        from faultmaven.core.investigation.hypothesis_manager import create_hypothesis_manager
        hypothesis_manager = create_hypothesis_manager()
        validated = hypothesis_manager.get_validated_hypothesis(investigation_state.ooda_engine.hypotheses)

        # Generate solution proposal
        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=self._format_state_for_prompt(investigation_state, validated),
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

        # Mark step complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.DECIDE)
        current_iteration.new_insights.append("Solution proposed")

        self.log_phase_action("Decide step complete", {"solution_proposed": True})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.DECIDE,
            made_progress=True,
        )

    async def _execute_act(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Execute OODA Act: User implements solution

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult confirming implementation
        """
        # Generate response
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

        # Mark step complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ACT)

        self.log_phase_action("Act step complete", {"solution_applied": True})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ACT,
            made_progress=True,
        )

    async def _execute_orient(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Execute OODA Orient: Verify solution worked

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with verification result
        """
        # Generate verification request
        evidence_requests = []
        evidence_requests.append(
            self.create_evidence_request(
                label="Solution verification",
                description="Confirm the problem is resolved",
                category="metrics",
                commands=["Check error rate after fix"],
                ui_locations=["Monitoring dashboard > Error rate"],
                priority=1,
            )
        )

        # Generate response
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

        # Mark step and iteration complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ORIENT)
        current_iteration.completed_at_turn = investigation_state.metadata.current_turn

        # Update case status
        investigation_state.lifecycle.case_status = "resolved"

        # Check phase completion
        is_complete, met, unmet = await self.check_completion(investigation_state)

        self.log_phase_action("Orient step complete", {"solution_verified": True})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ORIENT,
            evidence_requests_generated=evidence_requests,
            iteration_complete=True,
            phase_complete=is_complete,
            should_advance=is_complete,
            next_phase=InvestigationPhase.DOCUMENT if is_complete else None,
            made_progress=True,
        )

    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 5 completion criteria met

        Criteria:
        - Solution proposed
        - User confirmed implementation
        - Verification performed
        - Problem resolved (case status)

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        # Check if should suggest escalation (v3.0 workflow progression)
        should_suggest_esc, escalation_details = should_suggest_escalation(investigation_state)

        if should_suggest_esc:
            # Set pending workflow progression for escalation
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

        # Check if should suggest marking complete (v3.0 workflow progression)
        # This happens BEFORE case status changes to RESOLVED
        should_suggest_complete, completion_details = should_suggest_mark_complete(investigation_state)

        if should_suggest_complete and not should_suggest_esc:
            # Set pending workflow progression for completion
            # This will trigger INVESTIGATING → RESOLVED status change
            investigation_state.lifecycle.pending_workflow_progression = {
                "type": "mark_complete",
                "from_phase": InvestigationPhase.SOLUTION,
                "to_phase": InvestigationPhase.DOCUMENT,  # Will advance to Phase 6 after
                "rationale": "Solution verified successfully",
                "details": completion_details,
                "suggested_at_turn": investigation_state.metadata.current_turn,
            }
            investigation_state.lifecycle.workflow_progression_attempts = 0

            unmet_criteria.append("⏳ User confirmation needed: Mark investigation as complete?")

        # Check case status (this should be True AFTER user confirms mark_complete)
        if investigation_state.lifecycle.case_status == "resolved":
            met_criteria.append("Problem resolved")
        else:
            unmet_criteria.append("Solution not yet verified")

        # Check iterations
        if investigation_state.ooda_engine.current_iteration >= 2:
            met_criteria.append("Solution implementation attempted")

        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria

    def _detect_entry_mode(self, investigation_state: InvestigationState) -> str:
        """Detect Phase 5 entry mode based on phase history (v3.0)

        Three entry modes:
        1. MODE 1 (Normal): From Phase 4 with validated hypothesis (≥70%)
        2. MODE 2 (Fast Recovery): Direct from Phase 1 (critical incident skip)
        3. MODE 3 (Degraded): From Phase 4 in degraded mode (<70% confidence cap)

        Args:
            investigation_state: Current investigation state

        Returns:
            Entry mode string: 'normal', 'fast_recovery', or 'degraded'
        """
        phase_history = investigation_state.lifecycle.phase_entry_history
        escalation_state = investigation_state.lifecycle.escalation_state
        working_conclusion = investigation_state.lifecycle.working_conclusion

        # Check if in degraded mode (MODE 3)
        if escalation_state.operating_in_degraded_mode:
            return 'degraded'

        # Check if came directly from Phase 1 (MODE 2)
        if phase_history and phase_history[-1] == InvestigationPhase.BLAST_RADIUS:
            return 'fast_recovery'

        # Check if came from Phase 4 with validated hypothesis (MODE 1)
        if phase_history and phase_history[-1] == InvestigationPhase.VALIDATION:
            if working_conclusion and working_conclusion.confidence >= 0.70:
                return 'normal'
            else:
                # Came from Phase 4 but confidence <70% - should be degraded mode
                return 'degraded'

        # Default to normal (shouldn't normally reach here)
        return 'normal'

    def _format_state_for_prompt(self, state, validated_hypothesis=None) -> dict:
        """Format state for prompt context"""
        result = {
            "case_status": state.lifecycle.case_status,
            "current_iteration": state.ooda_engine.current_iteration,
        }
        if validated_hypothesis:
            result["root_cause"] = validated_hypothesis.statement
        return result
