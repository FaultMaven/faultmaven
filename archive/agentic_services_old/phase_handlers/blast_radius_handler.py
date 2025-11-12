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

from typing import List, Optional, Any
from datetime import datetime, timezone

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    OODAStep,
    AnomalyFrame,
)
from faultmaven.models.evidence import EvidenceCategory, EvidenceProvided, EvidenceRequest
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn_from_diagnostic,
    summarize_evidence_findings,
)


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
        context: Optional[dict] = None,
        evidence_provided: Optional[List[EvidenceProvided]] = None,
        evidence_requests: Optional[List[EvidenceRequest]] = None,
    ) -> PhaseHandlerResult:
        """Handle Phase 1: Blast Radius

        Flow:
        1. Consume scope evidence if available
        2. Determine OODA step (Observe or Orient)
        3. Execute step logic
        4. Check if phase complete
        5. Return result

        Args:
            investigation_state: Current investigation state
            user_query: User's query
            conversation_history: Recent conversation
            evidence_provided: List of evidence provided (from diagnostic state)
            evidence_requests: List of evidence requests (from diagnostic state)

        Returns:
            PhaseHandlerResult with response and state updates
        """
        # Consume scope/impact evidence and adjust anomaly frame if available
        if evidence_provided:
            await self._consume_scope_evidence(investigation_state, evidence_provided)

        # Determine current OODA step
        current_step = self.determine_ooda_step(investigation_state)

        if current_step == OODAStep.OBSERVE:
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history, context)
        else:
            # Shouldn't happen in Phase 1
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)

    async def _consume_scope_evidence(
        self,
        investigation_state: InvestigationState,
        evidence_provided: List[EvidenceProvided],
    ) -> None:
        """Consume scope/impact evidence and update anomaly frame confidence.

        Args:
            investigation_state: Current investigation state (modified in-place)
            evidence_provided: All evidence provided
        """
        # Get new evidence since last iteration
        last_turn = (
            investigation_state.ooda_engine.iterations[-1].turn_number
            if investigation_state.ooda_engine.iterations
            else 0
        )
        new_evidence = get_new_evidence_since_turn_from_diagnostic(evidence_provided, last_turn)

        if not new_evidence:
            return

        self.log_phase_action(
            "Consuming scope evidence",
            {"new_evidence_count": len(new_evidence)}
        )

        # Adjust anomaly frame confidence based on evidence
        if investigation_state.ooda_engine.anomaly_frame:
            anomaly_frame = investigation_state.ooda_engine.anomaly_frame

            for evidence in new_evidence:
                # Supportive evidence increases confidence in scope assessment
                if evidence.evidence_type.value == "supportive":
                    # Boost confidence by up to 10% per supportive evidence
                    anomaly_frame.confidence = min(1.0, anomaly_frame.confidence * 1.1)
                    self.log_phase_action(
                        "Increased anomaly frame confidence",
                        {
                            "evidence_id": evidence.evidence_id,
                            "new_confidence": anomaly_frame.confidence
                        }
                    )

                elif evidence.evidence_type.value == "refuting":
                    # Refuting evidence decreases confidence - scope may be misunderstood
                    anomaly_frame.confidence = max(0.0, anomaly_frame.confidence * 0.9)
                    self.log_phase_action(
                        "Decreased anomaly frame confidence",
                        {
                            "evidence_id": evidence.evidence_id,
                            "new_confidence": anomaly_frame.confidence
                        }
                    )

                # Extract scope insights from key findings
                if evidence.key_findings:
                    for finding in evidence.key_findings:
                        # Look for scope-related keywords
                        scope_keywords = ["users", "affected", "scope", "impact", "components"]
                        if any(keyword in finding.lower() for keyword in scope_keywords):
                            # This finding relates to scope - could update affected_components
                            self.log_phase_action(
                                "Scope insight identified",
                                {"finding": finding[:100]}
                            )

    async def _execute_observe(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        context: Optional[dict] = None,
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
            started_at=datetime.now(timezone.utc),  # Will be refined in Phase 2
            severity=problem_confirmation.severity if problem_confirmation else "medium",
            confidence=0.7,  # Initial confidence
            framed_at_turn=investigation_state.metadata.current_turn,
        )

        investigation_state.ooda_engine.anomaly_frame = anomaly_frame
        # Note: anomaly_refined field removed from OODAEngineState

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
            context=context,
            
            expected_schema=LeadInvestigatorResponse,
        )

        # Extract scope assessment if provided
        if structured_response.scope_assessment:
            anomaly_frame.affected_scope = structured_response.scope_assessment.blast_radius
            anomaly_frame.severity = structured_response.scope_assessment.impact_severity

        # Refine urgency level based on OODA assessment
        refined_urgency = self._assess_severity(
            anomaly_frame=anomaly_frame,
            problem_confirmation=problem_confirmation,
            scope_assessment=structured_response.scope_assessment,
        )
        investigation_state.lifecycle.urgency_level = refined_urgency

        # Select investigation strategy now that we have OODA-refined urgency
        from faultmaven.core.investigation.engagement_modes import create_engagement_mode_manager
        engagement_manager = create_engagement_mode_manager()

        strategy = engagement_manager.select_investigation_strategy(
            problem_confirmation=problem_confirmation,
            urgency_level=refined_urgency,
        )
        investigation_state.lifecycle.investigation_strategy = strategy

        self.log_phase_action(
            "Urgency refined and strategy selected",
            {
                "refined_urgency": refined_urgency,
                "investigation_strategy": strategy.value,
            },
        )

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

        # Mark step complete and iteration complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ORIENT)
        # Note: anomaly_refined field removed from OODAIteration
        current_iteration.completed_at_turn = investigation_state.metadata.current_turn

        # Check phase completion
        is_complete, met, unmet = await self.check_completion(investigation_state)

        # Determine next phase based on refined urgency
        if is_complete:
            next_phase = self._determine_next_phase(refined_urgency)
        else:
            next_phase = None

        self.log_phase_action(
            "Orient step complete",
            {
                "anomaly_frame_created": True,
                "next_phase": next_phase.name if next_phase else None,
            },
        )

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ORIENT,
            iteration_complete=True,
            phase_complete=is_complete,
            should_advance=is_complete,
            next_phase=next_phase,
            made_progress=True,
        )

    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 1 completion criteria met (v3.0)

        Completion Scenarios:
        1. Normal: AnomalyFrame + evidence → Phase 2 (Timeline)
        2. Fast Recovery: Critical incident + user confirmed → Phase 5 (Solution)

        Criteria:
        - AnomalyFrame created
        - Scope evidence collected (≥60% coverage)
        - Affected components identified
        - Severity assessed
        - v3.0: User confirmation if routing to Phase 5

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
            return False, met_criteria, unmet_criteria

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

        # v3.0: Check if this is potential fast recovery scenario
        anomaly_frame = investigation_state.ooda_engine.anomaly_frame
        urgency_level = investigation_state.lifecycle.urgency_level

        # Three scenarios for routing decision:
        # 1. CRITICAL: Auto-suggest fast recovery, require user confirmation
        # 2. HIGH: Offer choice between fast recovery and full investigation
        # 3. MEDIUM/LOW: Normal flow to Phase 2 (Timeline)

        if urgency_level in ["critical", "high"]:
            # Check if user has confirmed routing preference
            user_confirmed_routing = investigation_state.lifecycle.__dict__.get(
                'phase1_routing_confirmed', False
            )

            if not user_confirmed_routing:
                # Need user confirmation before completing Phase 1
                unmet_criteria.append(
                    "User confirmation needed: Fast recovery (skip to solution) "
                    "or full investigation (timeline → hypothesis → validation)?"
                )
                return False, met_criteria, unmet_criteria
            else:
                met_criteria.append("User confirmed routing preference")

        # All criteria met
        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria

    def _assess_severity(
        self,
        anomaly_frame: AnomalyFrame,
        problem_confirmation: Optional[Any],
        scope_assessment: Optional[Any],
    ) -> str:
        """Assess refined severity based on OODA findings

        Combines:
        - Initial urgency hint from Phase 0
        - OODA-assessed scope from evidence
        - Impact severity from scope assessment

        Args:
            anomaly_frame: Created AnomalyFrame with OODA findings
            problem_confirmation: Phase 0 problem confirmation
            scope_assessment: Scope assessment from LLM response

        Returns:
            Refined urgency level: "critical"|"high"|"medium"|"low"
        """
        # Start with AnomalyFrame severity (from OODA assessment)
        base_severity = anomaly_frame.severity

        # Check initial urgency hint from Phase 0 signal detection
        initial_hint = None
        if problem_confirmation and problem_confirmation.urgency_signals:
            initial_hint = problem_confirmation.urgency_signals.get("urgency_hint")

        # Check scope from OODA evidence
        scope = anomaly_frame.affected_scope.lower() if anomaly_frame.affected_scope else ""

        # Upgrade severity if scope indicates total/widespread impact
        if any(keyword in scope for keyword in ["all users", "entire", "global", "complete", "total"]):
            if base_severity in ["medium", "high"]:
                return "critical"

        # Use initial hint if it was critical and OODA confirms high severity
        if initial_hint == "critical" and base_severity in ["high", "critical"]:
            return "critical"

        # Otherwise use OODA-assessed severity
        return base_severity

    def _determine_next_phase(self, urgency_level: str) -> InvestigationPhase:
        """Determine next phase based on refined urgency

        Phase routing logic after Phase 1 completion:
        - critical: Jump to Solution (skip Timeline/Hypothesis/Validation)
        - high: Continue to Timeline (normal flow, flagged for fast-track)
        - medium/low: Continue to Timeline (systematic investigation)

        Args:
            urgency_level: Refined urgency from OODA assessment

        Returns:
            Next investigation phase
        """
        if urgency_level == "critical":
            # Critical urgency → mitigation first
            self.log_phase_action(
                "Critical urgency detected - skipping to Solution phase",
                {"urgency": urgency_level},
            )
            return InvestigationPhase.SOLUTION

        # All other urgencies → systematic investigation starting with Timeline
        return InvestigationPhase.TIMELINE

    def _format_state_for_prompt(self, state: InvestigationState) -> dict:
        """Format state for prompt context"""
        return {
            "anomaly_frame": state.ooda_engine.anomaly_frame.dict() if state.ooda_engine.anomaly_frame else None,
            "current_iteration": state.ooda_engine.current_iteration,
            "evidence_coverage": {"overall": 0.5},  # Simplified
        }
