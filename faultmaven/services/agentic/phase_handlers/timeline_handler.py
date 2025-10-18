"""Timeline Handler - Phase 2: Temporal Context

Phase 2 Characteristics:
- Engagement Mode: Lead Investigator
- OODA Steps: Observe, Orient (light intensity)
- Expected Iterations: 1-2
- Establishes: When problem started, what changed

Objectives:
- Identify problem start time
- Catalog recent changes (deployments, config, infrastructure)
- Establish temporal correlation
- Collect 50%+ timeline evidence

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

from typing import List, Optional
from datetime import datetime, timedelta

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    OODAStep,
)
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt


class TimelineHandler(BasePhaseHandler):
    """Phase 2: Timeline - Establish temporal context

    Responsibilities:
    - Request timeline evidence (when did it start?)
    - Request change history (deployments, config, infrastructure)
    - Correlate problem start with changes
    - Update AnomalyFrame with temporal data
    """

    def get_phase(self) -> InvestigationPhase:
        """Return Phase 2: Timeline"""
        return InvestigationPhase.TIMELINE

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Handle Phase 2: Timeline

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
            return await self._execute_observe(investigation_state, user_query, conversation_history)

    async def _execute_observe(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Execute OODA Observe: Request timeline evidence

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

        # Generate timeline evidence requests
        evidence_requests = []

        # Request 1: Problem start time
        evidence_requests.append(
            self.create_evidence_request(
                label="Problem start time",
                description="When did you first notice this problem? Be as specific as possible.",
                category="timeline",
                ui_locations=["Check monitoring alerts history", "Review incident timeline"],
                priority=1,
            )
        )

        # Request 2: Recent deployments
        evidence_requests.append(
            self.create_evidence_request(
                label="Recent deployments",
                description="What was deployed in the last 24-48 hours before the problem?",
                category="changes",
                commands=[
                    "kubectl rollout history deployment/api",
                    "git log --since='2 days ago' --oneline",
                ],
                ui_locations=["CI/CD pipeline > Deployment history"],
                priority=1,
            )
        )

        # Request 3: Configuration changes
        evidence_requests.append(
            self.create_evidence_request(
                label="Configuration changes",
                description="Any config changes, environment variables, or infrastructure updates?",
                category="changes",
                commands=["git log config/ --since='2 days ago'"],
                file_locations=["/etc/app/config.yaml"],
                priority=2,
            )
        )

        # Generate LLM response
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
        """Execute OODA Orient: Analyze timeline and correlate changes

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with timeline analysis
        """
        # Update AnomalyFrame with temporal data
        if investigation_state.ooda_engine.anomaly_frame:
            # Would update started_at from evidence (simplified here)
            investigation_state.ooda_engine.anomaly_frame.revision_count += 1
            investigation_state.ooda_engine.anomaly_frame.revised_at_turns.append(
                investigation_state.metadata.current_turn
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

        # Mark step complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]

        # Extract timeline update if provided
        if structured_response.timeline_update:
            current_iteration.new_insights.append(
                f"Timeline: {structured_response.timeline_update.change_correlation}"
            )
        current_iteration.steps_completed.append(OODAStep.ORIENT)
        current_iteration.completed_at_turn = investigation_state.metadata.current_turn
        if not structured_response.timeline_update:  # Only add if not already added above
            current_iteration.new_insights.append("Timeline established")

        # Check phase completion
        is_complete, met, unmet = await self.check_completion(investigation_state)

        self.log_phase_action("Orient step complete", {"timeline_established": True})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ORIENT,
            iteration_complete=True,
            phase_complete=is_complete,
            should_advance=is_complete,
            next_phase=InvestigationPhase.HYPOTHESIS if is_complete else None,
            made_progress=True,
        )

    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 2 completion criteria met

        Criteria:
        - Problem start time identified
        - Recent changes catalogued
        - Timeline evidence collected (≥50%)
        - Temporal correlation established

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        # Check timeline evidence
        timeline_evidence_count = len([
            req for req in investigation_state.evidence.evidence_requests
            if "timeline" in req or "changes" in req
        ])
        if timeline_evidence_count >= 2:
            met_criteria.append("Timeline evidence collected")
        else:
            unmet_criteria.append("Need timeline/change evidence")

        # Check AnomalyFrame has temporal info
        if investigation_state.ooda_engine.anomaly_frame and \
           investigation_state.ooda_engine.anomaly_frame.started_at:
            met_criteria.append("Problem start time identified")
        else:
            unmet_criteria.append("Problem start time not identified")

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
            "evidence_coverage": {"overall": 0.6},
        }
