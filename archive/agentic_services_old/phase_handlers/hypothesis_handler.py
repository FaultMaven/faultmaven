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
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.ORIENT:
            return await self._execute_orient(investigation_state, user_query, conversation_history, context)
        elif current_step == OODAStep.DECIDE:
            return await self._execute_decide(investigation_state, user_query, conversation_history, context)
        else:
            return await self._execute_observe(investigation_state, user_query, conversation_history, context)

    async def _execute_observe(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        context: Optional[dict] = None,
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
            context=context,
            
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
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Execute OODA Orient: Generate hypotheses from evidence

        Args:
            investigation_state: Current state
            user_query: User query
            conversation_history: Recent history

        Returns:
            PhaseHandlerResult with generated hypotheses
        """
        # Phase 3: Review opportunistic hypotheses and generate systematic coverage (Proposal #2)
        from faultmaven.services.agentic.hypothesis.systematic_generation import (
            systematic_hypothesis_generation,
            get_active_hypotheses,
        )

        current_turn = investigation_state.metadata.current_turn

        # Call systematic generation (reviews captured hypotheses, identifies gaps, generates coverage)
        new_hypotheses = systematic_hypothesis_generation(
            state=investigation_state,
            llm_generate_hypotheses_callback=self._llm_generate_hypotheses,
        )

        # Mark in iteration
        current_iteration = investigation_state.ooda_engine.iterations[-1]
        current_iteration.hypotheses_generated = len(new_hypotheses)

        # Log summary
        active_hypotheses = get_active_hypotheses(investigation_state)
        self.log_phase_action(
            "Systematic hypothesis generation complete",
            {
                "new_hypotheses": len(new_hypotheses),
                "total_active": len(active_hypotheses),
                "total_all": len(investigation_state.ooda_engine.hypotheses),
            },
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
        current_iteration.steps_completed.append(OODAStep.ORIENT)

        self.log_phase_action(
            "Orient step complete",
            {
                "new_hypotheses": len(new_hypotheses),
                "total_active": len(active_hypotheses),
            },
        )

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
        context: Optional[dict] = None,
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
            context=context,
            
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
        """Check if Phase 3 completion criteria met (v3.0)

        Structured Output Requirements (v3.0):
        - 2-4 hypotheses generated
        - Each hypothesis MUST have `required_evidence` array (2-5 items)
        - Each evidence item MUST have:
          * Priority: critical/important/optional
          * Acquisition guidance: source_type, query_pattern, interpretation
        - Hypotheses ranked by likelihood
        - Diverse categories (not all same type)

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        hypotheses = investigation_state.ooda_engine.hypotheses
        hypothesis_count = len(hypotheses)

        # Criterion 1: Hypothesis count (2-4)
        if 2 <= hypothesis_count <= 4:
            met_criteria.append(f"✓ {hypothesis_count} hypotheses generated (target: 2-4)")
        elif hypothesis_count > 4:
            met_criteria.append(f"⚠️  {hypothesis_count} hypotheses (more than recommended 4)")
        else:
            unmet_criteria.append(f"✗ Need at least 2 hypotheses (have {hypothesis_count})")

        if hypothesis_count > 0:
            # Criterion 2: Required evidence arrays (v3.0 NEW)
            hypotheses_with_evidence = []
            hypotheses_missing_evidence = []

            for hyp in hypotheses:
                if hasattr(hyp, 'required_evidence') and len(hyp.required_evidence) >= 2:
                    hypotheses_with_evidence.append(hyp.statement[:50])
                else:
                    hypotheses_missing_evidence.append(hyp.statement[:50])

            if len(hypotheses_with_evidence) == hypothesis_count:
                met_criteria.append(f"✓ All hypotheses have required_evidence (2-5 items each)")
            elif len(hypotheses_with_evidence) > 0:
                met_criteria.append(
                    f"⚠️  {len(hypotheses_with_evidence)}/{hypothesis_count} hypotheses "
                    f"have required_evidence"
                )
                unmet_criteria.append(
                    f"✗ Missing required_evidence for: {', '.join(hypotheses_missing_evidence)}"
                )
            else:
                unmet_criteria.append(
                    "✗ No hypotheses have required_evidence arrays (v3.0 requirement)"
                )

            # Criterion 3: Hypothesis ranking
            ranked_count = sum(1 for h in hypotheses if h.likelihood > 0)
            if ranked_count == hypothesis_count:
                met_criteria.append(f"✓ All hypotheses ranked by likelihood")
            else:
                unmet_criteria.append(f"✗ {hypothesis_count - ranked_count} hypotheses not ranked")

            # Criterion 4: Category diversity (nice to have)
            categories = set()
            for hyp in hypotheses:
                if hasattr(hyp, 'category') and hyp.category:
                    categories.add(hyp.category)

            if len(categories) >= 2:
                met_criteria.append(f"✓ Diverse categories ({len(categories)} types)")
            elif len(categories) == 1 and hypothesis_count > 2:
                met_criteria.append(
                    f"⚠️  All hypotheses same category ({list(categories)[0]}) - consider diversity"
                )

        # Check iterations (at least 1)
        if investigation_state.ooda_engine.current_iteration >= 1:
            met_criteria.append("✓ At least 1 OODA iteration complete")

        # v3.0: Strict completion requires all criteria met (no warnings allowed)
        warning_count = sum(1 for c in met_criteria if '⚠️' in c)
        failure_count = len(unmet_criteria)

        if failure_count == 0 and warning_count == 0:
            is_complete = True
        else:
            is_complete = False

        return is_complete, met_criteria, unmet_criteria

    def _llm_generate_hypotheses(self, state, target_categories):
        """LLM callback for systematic hypothesis generation

        Generates hypotheses for untested categories using placeholder logic.
        In production, this would use the LLM to analyze evidence and generate
        plausible hypotheses for each category.

        Args:
            state: Investigation state
            target_categories: List of categories needing coverage

        Returns:
            List of (statement, category) tuples
        """
        # Placeholder: In production, would call LLM with context
        # For now, generate simple placeholder hypotheses
        generated = []

        category_templates = {
            "infrastructure": "Infrastructure failure in component",
            "code": "Code defect introduced in recent change",
            "configuration": "Configuration mismatch or error",
            "external_dependency": "External service degradation",
            "client_side": "Client-side compatibility issue",
            "data": "Data corruption or schema mismatch",
            "network": "Network connectivity problem",
            "security": "Security misconfiguration",
            "resource_exhaustion": "Resource limit exceeded",
        }

        for category in target_categories:
            if category in category_templates:
                statement = category_templates[category]
                generated.append((statement, category))
                logger.info(f"Generated systematic hypothesis for category '{category}': {statement}")

        return generated

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
