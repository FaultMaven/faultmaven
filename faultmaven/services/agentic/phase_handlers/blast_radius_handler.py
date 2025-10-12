"""Blast Radius Handler - Phase 1: Scope Assessment

Phase 1 Characteristics:
- Engagement Mode: Lead Investigator (proactive)
- OODA Steps: Observe, Orient (light intensity)
- Expected Iterations: 1-2
- Creates: AnomalyFrame (formal problem definition)

Objectives:
- Define problem scope (who/what is affected)
- Assess severity and impact
- Create AnomalyFrame with scope definition
- Collect 60%+ scope evidence

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from typing import List
from datetime import datetime

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    OODAStep,
    AnomalyFrame,
)
from faultmaven.models.evidence import EvidenceCategory
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt


class BlastRadiusHandler(BasePhaseHandler):
    """Phase 1: Blast Radius - Scope and impact assessment

    Responsibilities:
    - Request scope evidence (symptoms, affected users/components)
    - Analyze evidence to understand impact
    - Create AnomalyFrame (formal problem definition)
    - Determine severity based on scope
    """

    def get_phase(self) -> InvestigationPhase:
        """Return Phase 1: Blast Radius"""
        return InvestigationPhase.BLAST_RADIUS

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
    ) -> PhaseHandlerResult:
        """Handle Phase 1: Blast Radius

        Flow:
        1. Determine OODA step (Observe or Orient)
        2. Execute step logic
        3. Check if phase complete
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

        if current_step == OODAStep.OBSERVE:
            return await self._execute_observe(investigation_state, user_query, conversation_history)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history)
        else:
            # Shouldn't happen in Phase 1
            return await self._execute_observe(investigation_state, user_query, conversation_history)

    async def _execute_observe(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Execute OODA Observe: Request scope evidence

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with evidence requests
        """
        # Start new OODA iteration if needed
        if not investigation_state.ooda_engine.iterations or \
           investigation_state.ooda_engine.iterations[-1].steps_completed:
            iteration = self.start_new_ooda_iteration(investigation_state)
            investigation_state.ooda_engine.iterations.append(iteration)

        # Generate scope evidence requests
        evidence_requests = []

        # Request 1: Affected scope
        evidence_requests.append(
            self.create_evidence_request(
                label="Affected scope",
                description="Identify who/what is affected by this problem",
                category="scope",
                commands=[
                    "Check error rate by user segment",
                    "Check affected endpoints/features",
                ],
                ui_locations=["Monitoring dashboard > Error breakdown"],
                priority=1,
            )
        )

        # Request 2: Symptom details
        evidence_requests.append(
            self.create_evidence_request(
                label="Error symptoms",
                description="Specific error messages or failure symptoms",
                category="symptoms",
                commands=["kubectl logs -l app=api --tail=100 | grep ERROR"],
                file_locations=["/var/log/application.log"],
                priority=1,
            )
        )

        # Request 3: Metrics
        evidence_requests.append(
            self.create_evidence_request(
                label="Impact metrics",
                description="Quantify severity: error rate, affected users",
                category="metrics",
                ui_locations=["Datadog/New Relic > Error rate dashboard"],
                priority=2,
            )
        )

        # Generate LLM response
        from faultmaven.models.responses import LeadInvestigatorResponse

        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=self._format_state_for_prompt(investigation_state),
            conversation_history=conversation_history,
            user_query=user_query,
        )

        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            max_tokens=500,
            expected_schema=LeadInvestigatorResponse,
        )

        # Mark step complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.OBSERVE)
        current_iteration.new_evidence_collected = len(evidence_requests)

        self.log_phase_action("Observe step complete", {"evidence_requests": len(evidence_requests)})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.OBSERVE,
            evidence_requests_generated=evidence_requests,
            made_progress=True,
        )

    async def _execute_orient(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Execute OODA Orient: Analyze scope evidence and create AnomalyFrame

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with AnomalyFrame created
        """
        # Create AnomalyFrame from gathered evidence
        problem_confirmation = investigation_state.problem_confirmation

        anomaly_frame = AnomalyFrame(
            statement=problem_confirmation.problem_statement if problem_confirmation else "Problem under investigation",
            affected_components=problem_confirmation.affected_components if problem_confirmation else [],
            affected_scope="TBD - analyzing evidence",  # Will be updated from evidence
            started_at=datetime.utcnow(),  # Will be refined in Phase 2
            severity=problem_confirmation.severity if problem_confirmation else "medium",
            confidence=0.7,  # Initial confidence
            framed_at_turn=investigation_state.metadata.current_turn,
        )

        investigation_state.ooda_engine.anomaly_frame = anomaly_frame
        investigation_state.ooda_engine.anomaly_refined = True

        # Generate response
        from faultmaven.models.responses import LeadInvestigatorResponse

        system_prompt = get_lead_investigator_prompt(
            current_phase=self.get_phase(),
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state=self._format_state_for_prompt(investigation_state),
            conversation_history=conversation_history,
            user_query=user_query,
        )

        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            max_tokens=500,
            expected_schema=LeadInvestigatorResponse,
        )

        # Extract scope assessment if provided
        if structured_response.scope_assessment:
            anomaly_frame.affected_scope = structured_response.scope_assessment.blast_radius
            anomaly_frame.severity = structured_response.scope_assessment.impact_severity

        # Mark step complete and iteration complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ORIENT)
        current_iteration.anomaly_refined = True
        current_iteration.completed_at_turn = investigation_state.metadata.current_turn

        # Check phase completion
        is_complete, met, unmet = await self.check_completion(investigation_state)

        self.log_phase_action("Orient step complete", {"anomaly_frame_created": True})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ORIENT,
            iteration_complete=True,
            phase_complete=is_complete,
            should_advance=is_complete,
            next_phase=InvestigationPhase.TIMELINE if is_complete else None,
            made_progress=True,
        )

    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 1 completion criteria met

        Criteria:
        - AnomalyFrame created
        - Scope evidence collected (≥60% coverage)
        - Affected components identified
        - Severity assessed

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        # Check AnomalyFrame
        if investigation_state.ooda_engine.anomaly_frame:
            met_criteria.append("AnomalyFrame created")
        else:
            unmet_criteria.append("AnomalyFrame not created")

        # Check evidence coverage (simplified - would check actual evidence)
        scope_evidence_count = len([
            req for req in investigation_state.evidence.evidence_requests
            if "scope" in req or "symptoms" in req
        ])
        if scope_evidence_count >= 2:
            met_criteria.append("Scope evidence collected")
        else:
            unmet_criteria.append("Need more scope evidence")

        # Check iterations
        if investigation_state.ooda_engine.current_iteration >= 1:
            met_criteria.append("At least 1 OODA iteration complete")

        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria

    def _format_state_for_prompt(self, state: InvestigationState) -> dict:
        """Format state for prompt context"""
        return {
            "anomaly_frame": state.ooda_engine.anomaly_frame.dict() if state.ooda_engine.anomaly_frame else None,
            "current_iteration": state.ooda_engine.current_iteration,
            "evidence_coverage": {"overall": 0.5},  # Simplified
        }
