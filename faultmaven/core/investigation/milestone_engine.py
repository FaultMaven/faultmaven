"""Milestone-Based Investigation Engine

This module implements the new milestone-based investigation system that replaces
the old OODA framework. Instead of rigid phase orchestration, this engine completes
milestones opportunistically based on data availability.

Key Differences from OODA:
- NO phase transitions - milestones complete when data is available
- NO sequential constraints - multiple milestones can complete in one turn
- Status-based prompt generation instead of phase-based
- Progress tracked via InvestigationProgress, not phase transitions

Design Reference:
- docs/architecture/milestone-based-investigation-framework.md
- docs/architecture/prompt-implementation-examples.md

Architecture:
- Process turn → Generate status-based prompt → Invoke LLM → Process response
- Update milestones based on LLM state_updates
- Track turn progress for analytics
- Automatic status transitions (INVESTIGATING → RESOLVED)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from faultmaven.models.case import (
    Case,
    CaseStatus,
    ConsultingData,
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisStatus,
    HypothesisEvidenceLink,
    EvidenceStance,
    InvestigationProgress,
    InvestigationStage,
    PathSelection,
    InvestigationPath,
    ProblemVerification,
    Solution,
    SolutionType,
    TurnProgress,
    TurnOutcome,
    TemporalState,
    UrgencyLevel,
    WorkingConclusion,
    RootCauseConclusion,
    ConfidenceLevel,
    DegradedMode,
    DegradedModeType,
)
from faultmaven.models.interfaces import ILLMProvider


logger = logging.getLogger(__name__)


# =============================================================================
# Milestone Engine - Main Implementation
# =============================================================================


class MilestoneEngine:
    """
    Milestone-based investigation engine.

    Replaces the old OODA engine with a simpler, more flexible approach where
    the agent completes milestones opportunistically based on available data.

    Responsibilities:
    - Generate prompts based on case status (CONSULTING, INVESTIGATING, RESOLVED)
    - Invoke LLM with appropriate schema
    - Process LLM responses and update case state
    - Track milestone completion and turn progress
    - Automatic status transitions when milestones complete

    Key Design Principles:
    - No phase orchestration - milestones complete when data is available
    - Status-based prompts instead of phase-based
    - Multiple milestones can complete in single turn
    - Repository abstraction for persistence (no direct DB access)
    """

    def __init__(
        self,
        llm_provider: ILLMProvider,
        repository: Any,  # Case repository abstraction (duck typing)
        trace_enabled: bool = True
    ):
        """Initialize milestone engine.

        Args:
            llm_provider: LLM provider implementation (ILLMProvider interface)
            repository: Case repository with save/get methods
            trace_enabled: Enable observability tracing
        """
        self.llm_provider = llm_provider
        self.repository = repository
        self.trace_enabled = trace_enabled

        logger.info("MilestoneEngine initialized with milestone-based architecture")

    async def process_turn(
        self,
        case: Case,
        user_message: str,
        attachments: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Process a single conversation turn.

        This is the main entry point for the milestone engine. It:
        1. Generates status-appropriate prompt
        2. Invokes LLM with structured output
        3. Processes response and updates case state
        4. Records turn progress
        5. Checks for automatic status transitions

        Args:
            case: Current case
            user_message: User's message this turn
            attachments: Optional file attachments

        Returns:
            {
                "agent_response": str,        # Natural language response to user
                "case_updated": Case,         # Updated case object
                "metadata": {
                    "turn_number": int,
                    "milestones_completed": List[str],
                    "progress_made": bool,
                    "status_transitioned": bool,
                    "outcome": TurnOutcome
                }
            }

        Raises:
            MilestoneEngineError: If processing fails
        """
        logger.info(
            f"Processing turn {case.current_turn + 1} for case {case.case_id} "
            f"(status: {case.status})"
        )

        try:
            # Step 1: Generate status-based prompt
            prompt = self._build_prompt(case, user_message, attachments)

            # Step 2: Invoke LLM with structured output
            llm_response_text = await self.llm_provider.generate(
                prompt=prompt,
                temperature=0.7,
                max_tokens=4000
            )

            # Step 3: Parse LLM response (simple text for now, structured later)
            # TODO: Implement structured output parsing when schemas are ready

            # Step 4: Process response and update state
            updated_case, turn_metadata = await self._process_response(
                case=case,
                user_message=user_message,
                llm_response=llm_response_text,
                attachments=attachments
            )

            # Step 5: Increment turn counter
            updated_case.current_turn += 1

            # Step 6: Record turn progress
            turn_record = self._create_turn_record(
                turn_number=updated_case.current_turn,
                milestones_completed=turn_metadata.get("milestones_completed", []),
                evidence_added=turn_metadata.get("evidence_added", []),
                hypotheses_generated=turn_metadata.get("hypotheses_generated", []),
                hypotheses_validated=turn_metadata.get("hypotheses_validated", []),
                solutions_proposed=turn_metadata.get("solutions_proposed", []),
                progress_made=turn_metadata.get("progress_made", False),
                outcome=turn_metadata.get("outcome", TurnOutcome.CONVERSATION),
                user_message=user_message,
                agent_response=llm_response_text
            )
            updated_case.turn_history.append(turn_record)

            # Step 7: Update progress tracking
            if turn_metadata.get("progress_made", False):
                updated_case.turns_without_progress = 0
            else:
                updated_case.turns_without_progress += 1

            # Step 8: Check degraded mode
            if updated_case.turns_without_progress >= 3 and updated_case.degraded_mode is None:
                self._enter_degraded_mode(updated_case, "no_progress")

            # Step 9: Check automatic status transitions
            self._check_automatic_transitions(updated_case)

            # Step 10: Save case
            updated_case.updated_at = datetime.now(timezone.utc)
            updated_case.last_activity_at = datetime.now(timezone.utc)
            await self.repository.save(updated_case)

            logger.info(
                f"Turn {updated_case.current_turn} processed successfully. "
                f"Status: {updated_case.status}, "
                f"Progress made: {turn_metadata.get('progress_made', False)}"
            )

            return {
                "agent_response": llm_response_text,
                "case_updated": updated_case,
                "metadata": {
                    "turn_number": updated_case.current_turn,
                    "milestones_completed": turn_metadata.get("milestones_completed", []),
                    "progress_made": turn_metadata.get("progress_made", False),
                    "status_transitioned": turn_metadata.get("status_transitioned", False),
                    "outcome": turn_metadata.get("outcome", TurnOutcome.CONVERSATION),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            }

        except Exception as e:
            logger.error(
                f"Error processing turn for case {case.case_id}: {e}",
                exc_info=True
            )
            raise MilestoneEngineError(f"Turn processing failed: {e}") from e

    # =========================================================================
    # Prompt Generation
    # =========================================================================

    def _build_prompt(
        self,
        case: Case,
        user_message: str,
        attachments: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Build status-appropriate prompt for LLM.

        Generates different prompts based on case status:
        - CONSULTING: Problem understanding and confirmation
        - INVESTIGATING: Milestone-based investigation
        - RESOLVED/CLOSED: Documentation and retrospective

        Args:
            case: Current case
            user_message: User's message
            attachments: Optional file attachments

        Returns:
            Complete prompt string
        """
        if case.status == CaseStatus.CONSULTING:
            return self._build_consulting_prompt(case, user_message)
        elif case.status == CaseStatus.INVESTIGATING:
            return self._build_investigating_prompt(case, user_message, attachments)
        elif case.status in [CaseStatus.RESOLVED, CaseStatus.CLOSED]:
            return self._build_terminal_prompt(case, user_message)
        else:
            raise MilestoneEngineError(f"Unknown case status: {case.status}")

    def _build_consulting_prompt(self, case: Case, user_message: str) -> str:
        """Build prompt for CONSULTING status."""
        return f"""You are FaultMaven, an AI troubleshooting copilot. The user is exploring a problem.

Status: CONSULTING (pre-investigation)
Turn: {case.current_turn + 1}

User Message:
{user_message}

Your Task:
1. Understand the user's problem
2. Ask clarifying questions if needed
3. Propose a clear, specific problem statement
4. Suggest quick fixes if obvious
5. Determine if formal investigation is needed

Current Context:
- Proposed Problem Statement: {case.consulting.proposed_problem_statement or "Not yet defined"}
- Problem Confirmed: {case.consulting.problem_statement_confirmed}
- Decided to Investigate: {case.consulting.decided_to_investigate}

Respond naturally and helpfully. If you have enough information, propose a clear problem statement.
If the user confirms it and wants to investigate, let them know you're ready to start."""

    def _build_investigating_prompt(
        self,
        case: Case,
        user_message: str,
        attachments: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Build prompt for INVESTIGATING status."""

        # Build milestone status
        progress = case.progress
        milestones_status = f"""
Milestones Completed:
- Symptom Verified: {progress.symptom_verified}
- Scope Assessed: {progress.scope_assessed}
- Timeline Established: {progress.timeline_established}
- Changes Identified: {progress.changes_identified}
- Root Cause Identified: {progress.root_cause_identified} (confidence: {progress.root_cause_confidence:.2f})
- Solution Proposed: {progress.solution_proposed}
- Solution Applied: {progress.solution_applied}
- Solution Verified: {progress.solution_verified}

Current Stage: {progress.current_stage}
Progress: {len(progress.completed_milestones)}/8 milestones complete
"""

        # Build evidence summary
        evidence_summary = ""
        if case.evidence:
            evidence_summary = f"\nEvidence Collected ({len(case.evidence)} items):\n"
            for ev in case.evidence[-5:]:  # Last 5 evidence items
                evidence_summary += f"- [{ev.category.value}] {ev.summary}\n"

        # Build hypothesis summary
        hypothesis_summary = ""
        if case.hypotheses:
            active = [h for h in case.hypotheses.values() if h.status == HypothesisStatus.ACTIVE]
            if active:
                hypothesis_summary = f"\nActive Hypotheses ({len(active)}):\n"
                for h in active[:3]:  # Top 3 hypotheses
                    hypothesis_summary += f"- {h.statement} (likelihood: {h.likelihood:.2f})\n"

        # Build attachments note
        attachments_note = ""
        if attachments:
            attachments_note = f"\nAttachments Provided: {len(attachments)} file(s)"

        return f"""You are FaultMaven, an AI troubleshooting copilot conducting a formal investigation.

Status: INVESTIGATING
Case: {case.title}
Description: {case.description}
Turn: {case.current_turn + 1}

{milestones_status}

{evidence_summary}

{hypothesis_summary}

User Message:
{user_message}
{attachments_note}

Your Task:
Complete as many milestones as possible based on available data. You can complete multiple milestones in one turn.

If the user provides comprehensive data (logs, metrics, etc.), analyze it thoroughly and:
1. Verify symptoms and assess scope
2. Establish timeline and identify changes
3. Identify root cause if evidence is clear
4. Propose solution if root cause is known

Key Principles:
- Milestones complete opportunistically (not sequentially)
- Use evidence to advance investigation
- Generate hypotheses only when root cause is unclear
- Focus on solving the problem efficiently

Respond with your analysis and next steps."""

    def _build_terminal_prompt(self, case: Case, user_message: str) -> str:
        """Build prompt for RESOLVED/CLOSED status."""
        return f"""You are FaultMaven, an AI troubleshooting copilot. This case is closed.

Status: {case.status.upper()}
Case: {case.title}
Closure Reason: {case.closure_reason}
Closed At: {case.closed_at.isoformat() if case.closed_at else 'Unknown'}

User Message:
{user_message}

Your Task:
- Answer questions about the investigation
- Provide documentation or summaries if requested
- Clarify findings or recommendations
- DO NOT reopen investigation or modify case state

The investigation is complete. Focus on documentation and knowledge sharing."""

    # =========================================================================
    # Response Processing
    # =========================================================================

    async def _process_response(
        self,
        case: Case,
        user_message: str,
        llm_response: str,
        attachments: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[Case, Dict[str, Any]]:
        """
        Process LLM response and update case state.

        This is where the milestone completion logic lives. Based on the LLM's
        response and any provided evidence, we update:
        - Milestone completion flags
        - Evidence collection
        - Hypothesis generation/validation
        - Solutions

        Args:
            case: Current case
            user_message: User's message
            llm_response: LLM's response text
            attachments: Optional attachments

        Returns:
            (updated_case, turn_metadata)
        """
        # Track what changed this turn
        milestones_completed = []
        evidence_added = []
        hypotheses_generated = []
        hypotheses_validated = []
        solutions_proposed = []

        # Process based on status
        if case.status == CaseStatus.CONSULTING:
            # Track uploaded files (files can be uploaded during CONSULTING)
            # NOTE: Files are NOT evidence yet - evidence is created during INVESTIGATING phase
            files_uploaded = []
            if attachments:
                for attachment in attachments:
                    uploaded_file = self._create_uploaded_file_from_attachment(
                        case=case,
                        attachment=attachment,
                        turn_number=case.current_turn + 1
                    )
                    case.uploaded_files.append(uploaded_file)
                    files_uploaded.append(uploaded_file.file_id)

            # Check for problem statement confirmation
            if "yes" in user_message.lower() or "correct" in user_message.lower():
                if case.consulting.proposed_problem_statement:
                    case.consulting.problem_statement_confirmed = True
                    case.consulting.problem_statement_confirmed_at = datetime.now(timezone.utc)

            # Check for investigation decision
            if "investigate" in user_message.lower() or "go ahead" in user_message.lower():
                if case.consulting.problem_statement_confirmed:
                    case.consulting.decided_to_investigate = True
                    case.consulting.decision_made_at = datetime.now(timezone.utc)

            # Check if should transition to INVESTIGATING
            status_transitioned = False
            if (case.consulting.problem_statement_confirmed and
                case.consulting.decided_to_investigate):
                await self._transition_to_investigating(case)
                status_transitioned = True

            metadata = {
                "progress_made": case.consulting.problem_statement_confirmed or len(files_uploaded) > 0,
                "outcome": TurnOutcome.DATA_PROVIDED if attachments else TurnOutcome.CONVERSATION,
                "status_transitioned": status_transitioned,
                "files_uploaded": files_uploaded  # Track uploaded files, not evidence (evidence only in INVESTIGATING)
            }

        elif case.status == CaseStatus.INVESTIGATING:
            # Simple heuristic-based milestone completion
            # (In production, this would parse structured LLM output)

            # Track uploaded files AND create evidence from attachments
            if attachments:
                for attachment in attachments:
                    # 1. Track as uploaded file (for file count)
                    uploaded_file = self._create_uploaded_file_from_attachment(
                        case=case,
                        attachment=attachment,
                        turn_number=case.current_turn + 1
                    )
                    case.uploaded_files.append(uploaded_file)

                    # 2. Create evidence from file (for hypothesis evaluation)
                    evidence = self._create_evidence_from_attachment(
                        case=case,
                        attachment=attachment,
                        turn_number=case.current_turn + 1
                    )
                    case.evidence.append(evidence)
                    evidence_added.append(evidence.evidence_id)

            # Simple keyword-based milestone detection (placeholder)
            # TODO: Replace with structured output parsing
            response_lower = llm_response.lower()

            if not case.progress.symptom_verified and "symptom" in response_lower:
                case.progress.symptom_verified = True
                milestones_completed.append("symptom_verified")

            if not case.progress.root_cause_identified and "root cause" in response_lower:
                case.progress.root_cause_identified = True
                case.progress.root_cause_confidence = 0.8
                case.progress.root_cause_method = "direct_analysis"
                milestones_completed.append("root_cause_identified")

            if not case.progress.solution_proposed and "solution" in response_lower:
                case.progress.solution_proposed = True
                milestones_completed.append("solution_proposed")

            # Determine outcome
            if milestones_completed:
                outcome = TurnOutcome.MILESTONE_COMPLETED
            elif attachments:
                outcome = TurnOutcome.DATA_PROVIDED
            else:
                outcome = TurnOutcome.CONVERSATION

            metadata = {
                "milestones_completed": milestones_completed,
                "evidence_added": evidence_added,
                "hypotheses_generated": hypotheses_generated,
                "hypotheses_validated": hypotheses_validated,
                "solutions_proposed": solutions_proposed,
                "progress_made": len(milestones_completed) > 0 or len(evidence_added) > 0,
                "outcome": outcome,
                "status_transitioned": False
            }

        else:  # RESOLVED or CLOSED
            metadata = {
                "progress_made": False,
                "outcome": TurnOutcome.CONVERSATION,
                "status_transitioned": False
            }

        return case, metadata

    # =========================================================================
    # State Management
    # =========================================================================

    async def _transition_to_investigating(self, case: Case) -> None:
        """
        Transition case from CONSULTING to INVESTIGATING.

        This creates the initial investigation structures and copies the
        confirmed problem statement to the case description.
        """
        logger.info(f"Transitioning case {case.case_id} to INVESTIGATING")

        # Change status
        case.status = CaseStatus.INVESTIGATING

        # Copy confirmed problem statement to description
        if case.consulting.proposed_problem_statement:
            case.description = case.consulting.proposed_problem_statement

        # Initialize investigation progress
        case.progress = InvestigationProgress()

        # Initialize problem verification with confirmed statement
        case.problem_verification = ProblemVerification(
            symptom_statement=case.description
        )

        # Initialize empty collections (already defaults in Case model)
        # case.evidence, case.hypotheses, case.solutions, case.turn_history are defaults

    def _check_automatic_transitions(self, case: Case) -> None:
        """
        Check if case should automatically transition status.

        Automatic Transitions:
        - INVESTIGATING → RESOLVED when solution_verified=True
        """
        if (case.status == CaseStatus.INVESTIGATING and
            case.progress.solution_verified):

            case.status = CaseStatus.RESOLVED
            case.resolved_at = datetime.now(timezone.utc)
            case.closed_at = datetime.now(timezone.utc)
            case.closure_reason = "resolved"

            # Calculate time to resolution
            if case.created_at:
                delta = case.closed_at - case.created_at
                # Store as metadata (no direct field for this)

            logger.info(
                f"Case {case.case_id} automatically transitioned to RESOLVED "
                f"(solution verified)"
            )

    def _enter_degraded_mode(
        self,
        case: Case,
        mode_type: str,
        reason: Optional[str] = None
    ) -> None:
        """
        Enter degraded mode when investigation is stuck.

        Args:
            case: Current case
            mode_type: Type of degradation (no_progress, limited_data, etc.)
            reason: Optional detailed reason
        """
        if case.degraded_mode:
            logger.warning(f"Case {case.case_id} already in degraded mode")
            return

        # Determine reason if not provided
        if not reason:
            if mode_type == "no_progress":
                reason = f"No progress for {case.turns_without_progress} consecutive turns"
            else:
                reason = "Investigation limitations encountered"

        case.degraded_mode = DegradedMode(
            mode_type=DegradedModeType(mode_type),
            reason=reason,
            entered_at=datetime.now(timezone.utc),
            attempted_actions=[]  # TODO: Track attempted actions
        )

        logger.info(
            f"Case {case.case_id} entered degraded mode: {mode_type} - {reason}"
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _create_uploaded_file_from_attachment(
        self,
        case: Case,
        attachment: Dict[str, Any],
        turn_number: int
    ) -> "UploadedFile":
        """
        Create uploaded file record from attachment.

        Args:
            case: Current case
            attachment: Attachment metadata with file_id, filename, data_type, etc.
            turn_number: Current turn number

        Returns:
            UploadedFile object
        """
        from faultmaven.models.case import UploadedFile

        uploaded_file = UploadedFile(
            file_id=attachment.get('file_id', f"file_{uuid4().hex[:12]}"),
            filename=attachment.get('filename', 'unknown'),
            size_bytes=attachment.get('size', 0),
            data_type=attachment.get('data_type', 'unknown'),
            uploaded_at_turn=turn_number,
            uploaded_at=datetime.now(timezone.utc),
            source_type=attachment.get('source_type', 'file_upload'),
            preprocessing_summary=attachment.get('summary', None),
            content_ref=attachment.get('s3_uri', attachment.get('file_id', 'unknown'))
        )

        return uploaded_file

    def _create_evidence_from_attachment(
        self,
        case: Case,
        attachment: Dict[str, Any],
        turn_number: int
    ) -> Evidence:
        """
        Create evidence object from file attachment.

        Args:
            case: Current case
            attachment: Attachment metadata
            turn_number: Current turn number

        Returns:
            Evidence object
        """
        # Infer category based on investigation state
        category = self._infer_evidence_category(case)

        # Create evidence
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            summary=f"Uploaded file: {attachment.get('filename', 'unknown')}",
            preprocessed_content="[Content to be preprocessed]",  # Placeholder
            content_ref=attachment.get('s3_uri', 'unknown'),
            content_size_bytes=attachment.get('size', 0),
            preprocessing_method="pending",
            category=category,
            source_type=EvidenceSourceType.LOG_FILE,  # Default
            form=EvidenceForm.DOCUMENT,
            advances_milestones=[],  # Calculated later
            collected_at=datetime.now(timezone.utc),
            collected_by=case.user_id,
            collected_at_turn=turn_number
        )

        return evidence

    def _infer_evidence_category(self, case: Case) -> EvidenceCategory:
        """
        Infer evidence category from investigation state.

        Rules:
        - If verification incomplete → SYMPTOM_EVIDENCE
        - If solution proposed → RESOLUTION_EVIDENCE
        - Otherwise → CAUSAL_EVIDENCE
        """
        if not case.progress.verification_complete:
            return EvidenceCategory.SYMPTOM_EVIDENCE

        if case.progress.solution_proposed:
            return EvidenceCategory.RESOLUTION_EVIDENCE

        return EvidenceCategory.CAUSAL_EVIDENCE

    def _create_turn_record(
        self,
        turn_number: int,
        milestones_completed: List[str],
        evidence_added: List[str],
        hypotheses_generated: List[str],
        hypotheses_validated: List[str],
        solutions_proposed: List[str],
        progress_made: bool,
        outcome: TurnOutcome,
        user_message: str,
        agent_response: str
    ) -> TurnProgress:
        """Create turn progress record."""
        return TurnProgress(
            turn_number=turn_number,
            timestamp=datetime.now(timezone.utc),
            milestones_completed=milestones_completed,
            evidence_added=evidence_added,
            hypotheses_generated=hypotheses_generated,
            hypotheses_validated=hypotheses_validated,
            solutions_proposed=solutions_proposed,
            progress_made=progress_made,
            actions_taken=self._extract_actions(agent_response),
            outcome=outcome,
            user_message_summary=self._summarize_text(user_message, 200),
            agent_response_summary=self._summarize_text(agent_response, 500)
        )

    def _extract_actions(self, agent_response: str) -> List[str]:
        """Extract action keywords from agent response."""
        action_keywords = ['verified', 'identified', 'proposed', 'tested', 'confirmed', 'analyzed']
        actions = []

        response_lower = agent_response.lower()
        for keyword in action_keywords:
            if keyword in response_lower:
                actions.append(keyword)

        return actions[:5]  # Limit to 5

    def _summarize_text(self, text: str, max_length: int = 200) -> str:
        """Summarize long text for storage."""
        if len(text) <= max_length:
            return text
        return text[:max_length - 3] + "..."


# =============================================================================
# Exceptions
# =============================================================================


class MilestoneEngineError(Exception):
    """Base exception for milestone engine errors."""
    pass
