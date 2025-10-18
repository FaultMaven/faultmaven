"""Hypothesis Handler - Phase 3: Theory Generation

Phase 3 Characteristics:
- Engagement Mode: Lead Investigator
- OODA Steps: Observe, Orient, Decide (medium intensity)
- Expected Iterations: 2-3
- Generates: 2-4 ranked hypotheses

Objectives:
- Generate plausible root cause hypotheses
- Rank hypotheses by likelihood
- Identify evidence gaps for testing
- Prepare for validation phase

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
from faultmaven.prompts.investigation.lead_investigator import get_lead_investigator_prompt
from faultmaven.services.evidence.consumption import (
    get_new_evidence_since_turn_from_diagnostic,
    summarize_evidence_findings,
)


class HypothesisHandler(BasePhaseHandler):
    """Phase 3: Hypothesis - Generate root cause theories

    Responsibilities:
    - Generate 2-4 initial hypotheses from evidence
    - Rank hypotheses by initial likelihood
    - Identify evidence needed to test each
    - Check for urgency skip (critical → Phase 5)
    """

    def __init__(self, *args, **kwargs):
        """Initialize hypothesis handler"""
        super().__init__(*args, **kwargs)
        self.hypothesis_manager = create_hypothesis_manager()

    def get_phase(self) -> InvestigationPhase:
        """Return Phase 3: Hypothesis"""
        return InvestigationPhase.HYPOTHESIS

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
        context: Optional[dict] = None,
        evidence_provided: Optional[List[EvidenceProvided]] = None,
        evidence_requests: Optional[List[EvidenceRequest]] = None,
    ) -> PhaseHandlerResult:
        """Handle Phase 3: Hypothesis

        Flow:
        1. Consume new evidence if available
        2. Check for urgency skip to Solution
        3. Determine OODA step (Observe, Orient, or Decide)
        4. Execute step logic
        5. Check if phase complete
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
        # Enrich conversation context with new evidence if available
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
                    f"\n\n## New Evidence Provided:\n{evidence_summary}\n\n"
                    f"Consider this evidence when formulating hypotheses."
                )
                self.log_phase_action(
                    "Enriching context with new evidence",
                    {"evidence_count": len(new_evidence)}
                )

        # Append additional context to conversation history
        if additional_context:
            conversation_history = conversation_history + additional_context

        # Check for urgency skip
        if investigation_state.lifecycle.urgency_level in ["high", "critical"]:
            should_skip = await self._check_urgency_skip(investigation_state)
            if should_skip:
                return await self._handle_urgency_skip(investigation_state, user_query)

        # Determine current OODA step
        current_step = self.determine_ooda_step(investigation_state)

        if current_step == OODAStep.OBSERVE:
            return await self._execute_observe(investigation_state, user_query, conversation_history)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history)
        elif current_step == OODAStep.DECIDE:
            return await self._execute_decide(investigation_state, user_query, conversation_history)
        else:
            return await self._execute_observe(investigation_state, user_query, conversation_history)

    async def _execute_observe(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Execute OODA Observe: Gather info for hypothesis generation

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

        # Generate evidence requests for hypothesis formation
        evidence_requests = []

        # Configuration evidence
        evidence_requests.append(
            self.create_evidence_request(
                label="Configuration state",
                description="Current configuration that could relate to symptoms",
                category="configuration",
                commands=["cat /etc/app/config.yaml"],
                priority=2,
            )
        )

        # Environment evidence
        evidence_requests.append(
            self.create_evidence_request(
                label="Environment details",
                description="Infrastructure state, versions, dependencies",
                category="environment",
                commands=["env | grep -E '(VERSION|ENDPOINT|URL)'"],
                priority=2,
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

        # Mark step complete
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

    async def _execute_orient(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Execute OODA Orient: Generate hypotheses from evidence

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with generated hypotheses
        """
        # Generate 2-4 hypotheses based on evidence
        # (In production, would use LLM to analyze evidence and generate)
        current_turn = investigation_state.metadata.current_turn

        if len(investigation_state.ooda_engine.hypotheses) == 0:
            # Generate initial hypotheses
            hypothesis1 = self.hypothesis_manager.create_hypothesis(
                statement="Recent deployment introduced regression",
                category="code",
                initial_likelihood=0.7,
                current_turn=current_turn,
            )

            hypothesis2 = self.hypothesis_manager.create_hypothesis(
                statement="Configuration change caused misconfiguration",
                category="config",
                initial_likelihood=0.5,
                current_turn=current_turn,
            )

            hypothesis3 = self.hypothesis_manager.create_hypothesis(
                statement="Infrastructure capacity exceeded",
                category="infrastructure",
                initial_likelihood=0.3,
                current_turn=current_turn,
            )

            investigation_state.ooda_engine.hypotheses.extend([hypothesis1, hypothesis2, hypothesis3])

            # Mark in iteration
            current_iteration = investigation_state.ooda_engine.iterations[-1]
            current_iteration.hypotheses_generated = 3

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
            max_tokens=600,
            expected_schema=LeadInvestigatorResponse,
        )

        # Mark step complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.ORIENT)

        self.log_phase_action("Orient step complete", {"hypotheses_generated": 3})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.ORIENT,
            made_progress=True,
        )

    async def _execute_decide(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Execute OODA Decide: Rank hypotheses and plan validation

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with ranked hypotheses
        """
        # Rank hypotheses (already ranked by likelihood)
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

        # Mark step complete and iteration complete
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.steps_completed.append(OODAStep.DECIDE)
        current_iteration.completed_at_turn = investigation_state.metadata.current_turn

        # Check phase completion
        is_complete, met, unmet = await self.check_completion(investigation_state)

        self.log_phase_action("Decide step complete", {"hypotheses_ranked": True})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            ooda_step_executed=OODAStep.DECIDE,
            iteration_complete=True,
            phase_complete=is_complete,
            should_advance=is_complete,
            next_phase=InvestigationPhase.VALIDATION if is_complete else None,
            made_progress=True,
        )

    async def _check_urgency_skip(self, investigation_state: InvestigationState) -> bool:
        """Check if should skip to Solution due to urgency"""
        urgency = investigation_state.lifecycle.urgency_level
        strategy = investigation_state.lifecycle.investigation_strategy

        from faultmaven.models.investigation import InvestigationStrategy
        if strategy == InvestigationStrategy.ACTIVE_INCIDENT and urgency == "critical":
            return True
        return False

    async def _handle_urgency_skip(
        self,
        investigation_state: InvestigationState,
        user_query: str,
    ) -> PhaseHandlerResult:
        """Handle urgency-based phase skip to Solution"""
        response = "Due to critical urgency, skipping deep hypothesis testing. Moving directly to mitigation..."

        self.log_phase_action("Urgency skip to Solution", {"urgency": "critical"})

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            phase_complete=True,
            should_advance=True,
            next_phase=InvestigationPhase.SOLUTION,
            made_progress=True,
        )

    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 3 completion criteria met

        Criteria:
        - 2-4 hypotheses generated
        - Hypotheses ranked by likelihood
        - Each hypothesis has category
        - Evidence gaps identified

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        hypothesis_count = len(investigation_state.ooda_engine.hypotheses)

        if hypothesis_count >= 2:
            met_criteria.append(f"{hypothesis_count} hypotheses generated")
        else:
            unmet_criteria.append(f"Need at least 2 hypotheses (have {hypothesis_count})")

        # Check if ranked
        ranked_count = sum(1 for h in investigation_state.ooda_engine.hypotheses if h.likelihood > 0)
        if ranked_count == hypothesis_count and hypothesis_count > 0:
            met_criteria.append("Hypotheses ranked")
        else:
            unmet_criteria.append("Hypotheses not yet ranked")

        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria

    def _format_state_for_prompt(self, state: InvestigationState) -> dict:
        """Format state for prompt context"""
        return {
            "anomaly_frame": state.ooda_engine.anomaly_frame.dict() if state.ooda_engine.anomaly_frame else None,
            "hypotheses": [
                {
                    "statement": h.statement,
                    "likelihood": h.likelihood,
                    "status": h.status.value,
                }
                for h in state.ooda_engine.hypotheses
            ],
            "current_iteration": state.ooda_engine.current_iteration,
        }
