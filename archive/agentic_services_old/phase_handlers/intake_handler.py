"""Intake Handler - Phase 0: Problem Confirmation

Phase 0 Characteristics:
- Engagement Mode: Consultant (reactive, helpful colleague)
- No OODA cycles (conversational mode)
- Detect problem signals in user queries
- Offer systematic investigation when appropriate
- Obtain user consent for Lead Investigator mode

Objectives:
- Confirm technical problem exists
- Create ProblemConfirmation structure
- Get user consent to proceed with investigation
- Determine initial urgency level

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from faultmaven.models.investigation import (
    InvestigationPhase,
    InvestigationState,
    EngagementMode,
    ProblemConfirmation,
)
from faultmaven.services.agentic.phase_handlers.base import BasePhaseHandler, PhaseHandlerResult
from faultmaven.core.investigation.engagement_modes import (
    create_engagement_mode_manager,
    ProblemSignalStrength,
)
from faultmaven.prompts.investigation.consultant_mode import (
    get_consultant_mode_prompt,
    get_consent_confirmation_prompt,
    TRANSITION_TO_LEAD_INVESTIGATOR,
)
from faultmaven.core.investigation.workflow_progression_detector import (
    should_suggest_start_investigation,
)
from faultmaven.prompts.investigation.workflow_progression_prompts import (
    get_start_investigation_prompt,
    parse_start_investigation_response,
)


logger = logging.getLogger(__name__)


class IntakeHandler(BasePhaseHandler):
    """Phase 0: Intake - Problem confirmation in Consultant mode

    Responsibilities:
    - Act as helpful technical consultant
    - Detect problem signals in user queries
    - Answer questions thoroughly
    - Offer investigation support when appropriate
    - Create ProblemConfirmation and obtain consent
    - Transition to Lead Investigator mode when ready
    """

    def __init__(self, *args, **kwargs):
        """Initialize intake handler"""
        super().__init__(*args, **kwargs)
        self.engagement_manager = create_engagement_mode_manager()

    def get_phase(self) -> InvestigationPhase:
        """Return Phase 0: Intake"""
        return InvestigationPhase.INTAKE

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str = "",
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Handle Phase 0: Intake

        Flow:
        1. Analyze user query for problem signals
        2. Generate response (answer question or offer investigation)
        3. If consent given, create ProblemConfirmation and prepare transition
        4. Update state accordingly

        Args:
            investigation_state: Current investigation state
            user_query: User's query
            conversation_history: Recent conversation
            context: Optional context dict (e.g., file upload metadata)

        Returns:
            PhaseHandlerResult with response and state updates
        """
        start_time = datetime.now(timezone.utc)

        # Analyze query for problem signals
        analysis = self.engagement_manager.analyze_initial_query(user_query)
        signal_strength = ProblemSignalStrength(analysis["signal_strength"])

        self.log_phase_action(
            "Analyzing query",
            {
                "signal_strength": signal_strength.value,
                "keywords": analysis["detected_keywords"],
            },
        )

        # Check for pending workflow progression confirmation (v3.0)
        if investigation_state.lifecycle.pending_workflow_progression:
            pending = investigation_state.lifecycle.pending_workflow_progression
            if pending["type"] == "start_investigation":
                decision, is_ambiguous = parse_start_investigation_response(user_query)

                if not is_ambiguous:
                    if decision == "start":
                        # User confirmed - proceed with investigation
                        return await self._handle_consent(investigation_state, user_query)
                    elif decision == "decline":
                        # User declined - stay in consulting
                        return await self._handle_decline(investigation_state, user_query)
                # If ambiguous, let workflow progression prompt handle clarification
                # (synthesizer will show clarification prompt)

        # Check if user is giving consent (legacy detection for backward compatibility)
        is_consent = self._detect_consent(user_query)

        if is_consent and investigation_state.problem_confirmation:
            # User consenting to investigation
            return await self._handle_consent(investigation_state, user_query)

        # Check if user is declining investigation (legacy detection)
        is_decline = self._detect_decline(user_query)

        if is_decline:
            return await self._handle_decline(investigation_state, user_query)

        # Generate appropriate response
        if signal_strength in [ProblemSignalStrength.MODERATE, ProblemSignalStrength.STRONG]:
            # Problem detected - create confirmation and offer investigation
            return await self._handle_problem_detection(
                investigation_state,
                user_query,
                conversation_history,
                signal_strength,
                context,
            )
        else:
            # Normal question - answer in consultant mode
            return await self._handle_general_query(
                investigation_state,
                user_query,
                conversation_history,
                context,
            )

    async def _handle_problem_detection(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        signal_strength: ProblemSignalStrength,
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Handle detected problem signal

        Creates ProblemConfirmation and offers systematic investigation

        Args:
            investigation_state: Current state
            user_query: User query with problem signal
            conversation_history: Recent history
            signal_strength: Strength of problem signal
            context: Optional context dict (e.g., file upload metadata, case evidence)

        Returns:
            PhaseHandlerResult with problem confirmation offer
        """
        # Create ProblemConfirmation structure
        problem_confirmation = self.engagement_manager.create_problem_confirmation(
            user_query,
            conversation_history,
        )

        # Store in state
        investigation_state.problem_confirmation = problem_confirmation
        investigation_state.lifecycle.urgency_level = problem_confirmation.severity

        # Check if should suggest starting investigation (v3.0 workflow progression)
        should_suggest, indicators = should_suggest_start_investigation(
            investigation_state,
            investigation_state.metadata.current_turn,
        )

        if should_suggest:
            # Set pending workflow progression
            investigation_state.lifecycle.pending_workflow_progression = {
                "type": "start_investigation",
                "from_phase": InvestigationPhase.INTAKE,
                "to_phase": InvestigationPhase.BLAST_RADIUS,
                "rationale": "Complexity detected, systematic approach recommended",
                "details": {
                    "problem_summary": problem_confirmation.problem_statement,
                    "complexity_indicators": indicators,
                    "estimated_time": "45-65 minutes",
                },
                "suggested_at_turn": investigation_state.metadata.current_turn,
            }
            investigation_state.lifecycle.workflow_progression_attempts = 0

            # Generate workflow progression prompt (will be appended by synthesizer)
            consent_prompt = get_start_investigation_prompt(
                problem_summary=problem_confirmation.problem_statement,
                complexity_indicators=indicators,
                estimated_time_range="45-65 minutes",
            )
        else:
            # Use legacy consent confirmation prompt
            consent_prompt = get_consent_confirmation_prompt(
                problem_statement=problem_confirmation.problem_statement,
                severity=problem_confirmation.severity,
                investigation_approach=problem_confirmation.investigation_approach,
            )

        # Generate response with LLM
        from faultmaven.models.responses import ConsultantResponse

        system_prompt = get_consultant_mode_prompt(
            conversation_history=conversation_history,
            user_query=user_query,
            problem_signals_detected=True,
            signal_strength=signal_strength.value,
        )

        # Build context including file upload data and case evidence (if provided)
        llm_context = {"consent_prompt": consent_prompt}
        if context:
            llm_context.update(context)

        # Get LLM to craft structured response
        # Note: case_evidence is automatically included in context by ooda_integration.py
        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            context=llm_context,
            
            expected_schema=ConsultantResponse,
        )

        self.log_phase_action(
            "Problem detected, offering investigation",
            {
                "severity": problem_confirmation.severity,
                "problem_detected": structured_response.problem_detected,
                "suggested_actions_count": len(structured_response.suggested_actions),
            },
        )

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            phase_complete=False,  # Waiting for consent
            should_advance=False,
            ooda_step_executed=None,  # No OODA in Phase 0
            made_progress=True,
        )

    async def _handle_consent(
        self,
        investigation_state: InvestigationState,
        user_query: str,
    ) -> PhaseHandlerResult:
        """Handle user consent to proceed with investigation

        Transitions to Lead Investigator mode and Phase 1 (Blast Radius).
        Investigation strategy selection deferred until after Phase 1 OODA assessment.

        Args:
            investigation_state: Current state
            user_query: Consent message

        Returns:
            PhaseHandlerResult with transition to Phase 1
        """
        # Clear pending workflow progression (v3.0)
        investigation_state.lifecycle.pending_workflow_progression = None
        investigation_state.lifecycle.workflow_progression_attempts = 0

        # Change case status to INVESTIGATING (Phase 0 → Phases 1-5)
        investigation_state.lifecycle.case_status = "investigating"

        # Switch engagement mode
        investigation_state.metadata.engagement_mode = EngagementMode.LEAD_INVESTIGATOR

        # Defer investigation strategy selection to Phase 1 (after OODA assessment)
        investigation_state.lifecycle.investigation_strategy = None

        # Activate OODA
        investigation_state.ooda_engine.ooda_active = True

        # Always start at Phase 1 (Blast Radius) for scope assessment
        # Phase routing based on urgency happens after Phase 1 OODA completes
        entry_phase = InvestigationPhase.BLAST_RADIUS
        investigation_state.lifecycle.entry_phase = entry_phase

        self.log_phase_action(
            "User consented to investigation",
            {
                "engagement_mode": EngagementMode.LEAD_INVESTIGATOR.value,
                "strategy": "deferred_to_phase_1",  # Will be set after OODA assessment
                "entry_phase": entry_phase.name,
            },
        )

        return PhaseHandlerResult(
            response_text=TRANSITION_TO_LEAD_INVESTIGATOR,
            updated_state=investigation_state,
            phase_complete=True,
            should_advance=True,
            next_phase=entry_phase,
            made_progress=True,
        )

    async def _handle_decline(
        self,
        investigation_state: InvestigationState,
        user_query: str,
    ) -> PhaseHandlerResult:
        """Handle user declining systematic investigation

        Stay in Consultant mode, continue answering questions

        Args:
            investigation_state: Current state
            user_query: Decline message

        Returns:
            PhaseHandlerResult staying in Phase 0
        """
        from faultmaven.prompts.investigation.consultant_mode import (
            DECLINED_INVESTIGATION_ACKNOWLEDGMENT,
        )
        from faultmaven.prompts.investigation.workflow_progression_prompts import (
            get_workflow_transition_confirmation,
        )

        # Clear pending workflow progression (v3.0)
        investigation_state.lifecycle.pending_workflow_progression = None
        investigation_state.lifecycle.workflow_progression_attempts = 0

        self.log_phase_action("User declined investigation, staying in consultant mode")

        # Use workflow transition confirmation if available, otherwise legacy message
        response_text = get_workflow_transition_confirmation("declined_investigation", {})

        return PhaseHandlerResult(
            response_text=response_text,
            updated_state=investigation_state,
            phase_complete=False,
            should_advance=False,
            made_progress=True,
        )

    async def _handle_general_query(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
        context: Optional[dict] = None,
    ) -> PhaseHandlerResult:
        """Handle general informational query

        Answer question in consultant mode without offering investigation

        Args:
            investigation_state: Current state
            user_query: User question
            conversation_history: Recent history
            context: Optional context dict (e.g., file upload metadata, case evidence)

        Returns:
            PhaseHandlerResult with answer
        """
        from faultmaven.models.responses import ConsultantResponse

        # Generate consultant mode response
        system_prompt = get_consultant_mode_prompt(
            conversation_history=conversation_history,
            user_query=user_query,
            problem_signals_detected=False,
        )

        # Note: case_evidence is automatically included in context by ooda_integration.py
        structured_response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            context=context,  # Pass through context which includes case evidence
            
            expected_schema=ConsultantResponse,
        )

        self.log_phase_action("Answered general query in consultant mode")

        return PhaseHandlerResult(
            response_text=structured_response.answer,
            structured_response=structured_response,
            updated_state=investigation_state,
            phase_complete=False,
            should_advance=False,
            made_progress=True,
        )

    async def check_completion(
        self,
        investigation_state: InvestigationState,
    ) -> tuple[bool, List[str], List[str]]:
        """Check if Phase 0 completion criteria met

        Completion criteria:
        - Problem confirmation created
        - User consented to investigation
        - Engagement mode switched to Lead Investigator

        Args:
            investigation_state: Current state

        Returns:
            Tuple of (is_complete, criteria_met, criteria_unmet)
        """
        met_criteria = []
        unmet_criteria = []

        # Check problem confirmation
        if investigation_state.problem_confirmation:
            met_criteria.append("Problem confirmation created")
        else:
            unmet_criteria.append("No problem confirmation yet")

        # Check engagement mode
        if investigation_state.metadata.engagement_mode == EngagementMode.LEAD_INVESTIGATOR:
            met_criteria.append("User consented to investigation")
        else:
            unmet_criteria.append("Awaiting user consent for Lead Investigator mode")

        # Check OODA activation
        if investigation_state.ooda_engine.ooda_active:
            met_criteria.append("OODA framework activated")
        else:
            unmet_criteria.append("OODA not yet activated")

        is_complete = len(unmet_criteria) == 0
        return is_complete, met_criteria, unmet_criteria

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _detect_consent(self, user_query: str) -> bool:
        """Detect if user is giving consent to investigation

        Args:
            user_query: User query

        Returns:
            True if consent detected
        """
        query_lower = user_query.lower().strip()

        consent_phrases = [
            "yes",
            "yeah",
            "yep",
            "sure",
            "ok",
            "okay",
            "let's do it",
            "go ahead",
            "proceed",
            "sounds good",
            "please do",
            "help me investigate",
            "let's investigate",
        ]

        return any(phrase in query_lower for phrase in consent_phrases)

    def _detect_decline(self, user_query: str) -> bool:
        """Detect if user is declining investigation

        Args:
            user_query: User query

        Returns:
            True if decline detected
        """
        query_lower = user_query.lower().strip()

        decline_phrases = [
            "no thanks",
            "no thank you",
            "not now",
            "maybe later",
            "just answer",
            "no need",
            "don't need",
            "skip",
        ]

        return any(phrase in query_lower for phrase in decline_phrases)
