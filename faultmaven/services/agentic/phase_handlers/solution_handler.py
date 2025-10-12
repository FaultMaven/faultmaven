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

from typing import List

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    OODAStep,
)
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt


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
    ) -> PhaseHandlerResult:
        """Handle Phase 5: Solution

        Flow:
        1. Determine OODA step (Decide, Act, or Orient)
        2. Execute step logic
        3. Check phase completion
        4. Return result

        Args:
            investigation_state: Current investigation state
            user_query: User's query
            conversation_history: Recent conversation

        Returns:
            PhaseHandlerResult with response and state updates
        """
        # Determine current OODA step
        current_step = self.determine_ooda_step(investigation_state)

        if current_step == OODAStep.DECIDE:
            return await self._execute_decide(investigation_state, user_query, conversation_history)
        elif current_step == OODAStep.ACT:
            return await self._execute_act(investigation_state, user_query, conversation_history)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history)
        else:
            return await self._execute_decide(investigation_state, user_query, conversation_history)

    async def _execute_decide(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
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
            max_tokens=600,
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
            max_tokens=500,
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
            max_tokens=500,
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

        # Check case status
        if investigation_state.lifecycle.case_status in ["resolved", "mitigated"]:
            met_criteria.append("Problem resolved/mitigated")
        else:
            unmet_criteria.append("Solution not yet verified")

        # Check iterations
        if investigation_state.ooda_engine.current_iteration >= 2:
            met_criteria.append("Solution implementation attempted")

        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria

    def _format_state_for_prompt(self, state, validated_hypothesis=None) -> dict:
        """Format state for prompt context"""
        result = {
            "case_status": state.lifecycle.case_status,
            "current_iteration": state.ooda_engine.current_iteration,
        }
        if validated_hypothesis:
            result["root_cause"] = validated_hypothesis.statement
        return result
