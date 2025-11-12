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
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history, context)
        else:
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)

    async def _execute_observe(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        context: Optional[dict] = None,
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
            context=context,
            
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
        context: Optional[dict] = None,
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
            context=context,
            
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

        # Check for opportunistic hypothesis in LLM response (Proposal #2)
        from faultmaven.services.agentic.hypothesis.opportunistic_capture import capture_if_present
        captured_hypo = capture_if_present(structured_response.answer, investigation_state)
        if captured_hypo:
            self.log_phase_action(
                "Opportunistic hypothesis captured",
                {
                    "hypothesis": captured_hypo.statement[:50],
                    "category": captured_hypo.category,
                    "confidence": captured_hypo.likelihood,
                },
            )

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
        """Check if Phase 2 completion criteria met (v3.0)

        Three MUST-HAVE Items (v3.0):
        1. Problem start time (when symptoms first appeared)
        2. Change correlation (what changed around that time)
        3. Temporal pattern (sudden vs gradual, intermittent vs constant)

        Can proceed with partial/uncertain timeline IF limitations documented.

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        anomaly_frame = investigation_state.ooda_engine.anomaly_frame

        # MUST-HAVE 1: Problem start time
        if anomaly_frame and anomaly_frame.started_at:
            met_criteria.append("✓ Problem start time identified")
        else:
            # Check if user indicated timeline unavailable
            timeline_unavailable = investigation_state.lifecycle.__dict__.get(
                'timeline_unavailable', False
            )
            if timeline_unavailable:
                met_criteria.append("⚠️  Timeline unavailable (documented limitation)")
            else:
                unmet_criteria.append("✗ Problem start time not identified")

        # MUST-HAVE 2: Change correlation (deployments, config, infrastructure)
        # Look for change-related evidence or documentation of no changes
        changes_identified = False
        no_changes = False

        if investigation_state.evidence.evidence_provided:
            # Check if any evidence relates to changes
            for evidence_id in investigation_state.evidence.evidence_provided:
                if any(keyword in evidence_id.lower() for keyword in ['deploy', 'change', 'config', 'release']):
                    changes_identified = True
                    break

        # Check if explicitly documented no changes
        no_changes = investigation_state.lifecycle.__dict__.get('no_recent_changes', False)

        if changes_identified:
            met_criteria.append("✓ Recent changes identified and correlated")
        elif no_changes:
            met_criteria.append("⚠️  No recent changes (documented)")
        else:
            unmet_criteria.append("✗ Change history not collected")

        # MUST-HAVE 3: Temporal pattern (sudden vs gradual)
        if anomaly_frame and hasattr(anomaly_frame, 'temporal_pattern'):
            met_criteria.append("✓ Temporal pattern identified")
        else:
            # Check if pattern documented in state
            pattern_documented = investigation_state.lifecycle.__dict__.get(
                'temporal_pattern_documented', False
            )
            if pattern_documented:
                met_criteria.append("⚠️  Temporal pattern documented")
            else:
                unmet_criteria.append("✗ Temporal pattern (sudden/gradual) not established")

        # Check iterations (at least 1)
        if investigation_state.ooda_engine.current_iteration >= 1:
            met_criteria.append("✓ At least 1 OODA iteration complete")

        # v3.0: Can proceed with partial timeline IF documented
        # Count warnings (⚠️) vs failures (✗)
        warning_count = sum(1 for c in met_criteria if '⚠️' in c)
        failure_count = len(unmet_criteria)

        # Can proceed if:
        # - All MUST-HAVEs met (0 failures), OR
        # - Some limitations but documented (failures == 0, warnings > 0)
        if failure_count == 0:
            if warning_count > 0:
                met_criteria.append(
                    f"⚠️  Proceeding with partial timeline ({warning_count} limitations documented)"
                )
            is_complete = True
        else:
            is_complete = False

        return is_complete, met_criteria, unmet_criteria

    def _format_state_for_prompt(self, state: InvestigationState) -> dict:
        """Format state for prompt context"""
        return {
            "anomaly_frame": state.ooda_engine.anomaly_frame.dict() if state.ooda_engine.anomaly_frame else None,
            "current_iteration": state.ooda_engine.current_iteration,
            "evidence_coverage": {"overall": 0.6},
        }
