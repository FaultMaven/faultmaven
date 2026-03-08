"""Investigation Service - Manages milestone-based troubleshooting workflow

Purpose: Orchestrate investigation turns and milestone progress tracking

This service wraps the MilestoneEngine and provides:
- Access control for investigations
- Case retrieval and persistence
- Turn creation and processing
- Progress tracking and reporting
- Integration with session management
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.exceptions import (
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.persistence.case_repository import CaseRepository
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_pipeline import generate_implicit_query
from faultmaven.models.api_models import (
    AttachmentResult,
    IntentType,
    SuggestedActionResponse,
    TurnResponse,
)

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.case.contracts import Case, CaseStatus
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)
from faultmaven.utils.serialization import to_json_compatible

logger = logging.getLogger(__name__)


class InvestigationService:
    """
    Service for managing investigation turns and milestone progress.

    Coordinates between:
    - MilestoneEngine (core investigation logic)
    - CaseRepository (persistence)
    - Access control (user permissions)
    """

    def __init__(
        self,
        milestone_engine: MilestoneEngine,
        case_repository: CaseRepository,
        preprocessing_service=None,
        file_storage_service=None,
    ):
        """
        Initialize investigation service.

        Args:
            milestone_engine: Core investigation engine with LLM integration
            case_repository: Case persistence layer
            preprocessing_service: Classification and extraction pipeline
            file_storage_service: Raw file storage (local/S3)
        """
        self.engine = milestone_engine
        self.repository = case_repository
        self.preprocessing_service = preprocessing_service
        self.file_storage_service = file_storage_service

    @trace("investigation_service_process_turn")
    async def process_turn(
        self, case_id: str, user_id: str, payload: TurnPayload
    ) -> TurnResponse:
        """
        Process a user turn through the two-step pipeline.

        Step 1: Preprocess any attachments (classify + extract, before LLM).
        Step 2: LLM inference with query + evidence context.

        Args:
            case_id: Case identifier
            user_id: User making the request
            payload: Turn payload with optional query and/or attachments

        Returns:
            TurnResponse with agent response, milestones, progress, and attachment results

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If turn processing fails
        """
        try:
            # 1. Retrieve case and verify access
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            if case.user_id != user_id:
                logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            next_turn = case.current_turn + 1

            # ── STEP 1: PRE-LLM DATA INGESTION ──
            # Classify query for scenario-driven processing mode
            from faultmaven.modules.agent.domain.services.query_classifier import (
                classify_query,
            )

            classification = classify_query(
                payload.query or "",
                has_attachments=payload.has_attachments,
            )
            processing_mode = classification.mode.value

            evidence_created: List["Evidence"] = []
            if payload.has_attachments:
                for attachment in payload.attachments:
                    evidence = await self._preprocess_attachment(
                        case,
                        attachment,
                        user_id,
                        next_turn,
                        processing_mode=processing_mode,
                    )
                    evidence_created.append(evidence)
                    case.evidence.append(evidence)

            # Determine query (explicit or implicit)
            query = payload.query
            if not payload.has_query and payload.has_attachments:
                query = generate_implicit_query(payload.attachments, evidence_created)

            # 2. Save user message to conversation history BEFORE LLM processing
            from uuid import uuid4

            intent = payload.intent
            intent_type = intent.type if intent else IntentType.CONVERSATION

            user_message_obj = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": next_turn,
                "role": "user",
                "message_type": "user_query",
                "content": query or "",
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "author_id": user_id,
                "token_count": None,
                "metadata": {
                    "has_attachments": payload.has_attachments,
                    "attachment_count": len(payload.attachments),
                    "intent_type": intent_type.value,
                    "intent_metadata": (
                        intent.model_dump(exclude_unset=True, exclude={"type"})
                        if intent
                        else {}
                    ),
                },
            }
            case.messages.append(user_message_obj)
            case.message_count += 1
            case.current_turn = next_turn
            await self.repository.save(case)

            # ── STEP 2: LLM INFERENCE ──
            # Heuristic check for greetings if intent is CONVERSATION (default)
            if intent_type == IntentType.CONVERSATION and query:
                heuristic_intent = self._detect_intent_heuristic(query)
                if heuristic_intent:
                    intent_type = heuristic_intent
                    logger.info(
                        f"Heuristic detected intent {intent_type.value} for message: '{query}'"
                    )

            if intent_type == IntentType.STATUS_TRANSITION:
                result = await self._handle_status_transition(
                    case=case,
                    user_message=query or "",
                    from_status=intent.from_status if intent else None,
                    to_status=intent.to_status if intent else None,
                    user_confirmed=(
                        (intent.user_confirmed or False) if intent else False
                    ),
                )
            elif intent_type == IntentType.CONFIRMATION:
                result = await self._handle_confirmation(
                    case=case,
                    user_message=query or "",
                    confirmation_value=intent.confirmation_value if intent else None,
                )
            elif intent_type == IntentType.HYPOTHESIS_ACTION:
                result = await self._handle_hypothesis_action(
                    case=case,
                    user_message=query or "",
                    hypothesis_id=intent.hypothesis_id if intent else None,
                    action=intent.action if intent else None,
                )
            elif intent_type == IntentType.CONVERSATION:
                # Build attachment metadata for the engine
                attachment_metadata = []
                for att, ev in zip(payload.attachments, evidence_created):
                    is_paste = att.filename.startswith("pasted-content-")
                    source = "paste" if is_paste else "file_upload"
                    # UploadedFile.file_id requires ^(file_|data_)[a-f0-9]{12,16}$
                    ev_hex = ev.evidence_id.removeprefix("ev_")
                    file_id = f"data_{ev_hex}" if is_paste else f"file_{ev_hex}"
                    attachment_metadata.append(
                        {
                            "evidence_id": ev.evidence_id,
                            "file_id": file_id,
                            "filename": att.filename,
                            "data_type": ev.data_type,
                            "size": ev.content_size_bytes,
                            "source_type": source,
                            "summary": ev.summary,
                            "s3_uri": ev.content_ref,
                        }
                    )
                # DA evidence search is handled inside MilestoneEngine's tool loop.
                # The same LLM that tracks hypotheses searches evidence directly
                # during generation — no pre-fetch or separate gathering step needed.

                result = await self.engine.process_turn(
                    case=case,
                    user_message=query or "",
                    attachments=attachment_metadata or None,
                    intent_type=intent_type.value,
                    intent_data={
                        **(intent.model_dump(exclude_unset=True) if intent else {}),
                        "query_mode": classification.mode.value,
                    },
                )
            elif intent_type == IntentType.GREETING:
                result = await self._handle_greeting(case=case)
            else:
                raise ValueError(f"Unknown intent type: {intent_type}")

            # 3. Processing succeeded — extract updated case
            updated_case = result["case_updated"]
            agent_response_text = result["agent_response"]

            # 4. Save agent response to conversation history
            from uuid import uuid4

            agent_message = {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": updated_case.current_turn,
                "role": "agent",
                "message_type": "agent_response",
                "content": agent_response_text,
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "author_id": None,
                "token_count": None,
                "metadata": {},
            }
            updated_case.messages.append(agent_message)
            updated_case.message_count += 1
            await self.repository.save(updated_case)

            # 5. Build TurnResponse
            raw_follow_ups = result.get("suggested_follow_ups", [])
            suggested_actions = [
                SuggestedActionResponse(
                    label=f["label"],
                    type=f["action_type"],
                    payload=f["payload"],
                )
                for f in raw_follow_ups
            ]

            response = TurnResponse(
                agent_response=agent_response_text,
                turn_number=updated_case.current_turn,
                milestones_completed=result.get("metadata", {}).get(
                    "milestones_completed", []
                ),
                case_status=updated_case.status,
                progress_made=result.get("metadata", {}).get("progress_made", False),
                is_stuck=(
                    updated_case.is_stuck
                    if hasattr(updated_case, "is_stuck")
                    else False
                ),
                attachments_processed=[
                    AttachmentResult(
                        evidence_id=ev.evidence_id,
                        filename=att.filename,
                        data_type=ev.data_type or "",
                        file_size=ev.content_size_bytes,
                        processing_status="completed",
                    )
                    for att, ev in zip(payload.attachments, evidence_created)
                ],
                suggested_actions=suggested_actions,
            )

            logger.info(
                f"Processed turn {response.turn_number} for case {case_id}, "
                f"status={response.case_status}, milestones={len(response.milestones_completed)}, "
                f"attachments={len(evidence_created)}, messages={updated_case.message_count}"
            )

            return response

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            logger.error(f"Failed to process turn for case {case_id}: {e}")
            raise ServiceException(f"Turn processing failed: {str(e)}") from e

    @trace("investigation_service_get_progress")
    async def get_progress(self, case_id: str, user_id: str) -> Dict[str, Any]:
        """
        Get current investigation progress.

        Args:
            case_id: Case identifier
            user_id: User making the request

        Returns:
            Progress summary with:
            - case_id, status, current_stage
            - milestones_completed, pending_milestones
            - current_turn, is_stuck

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                logger.warning(
                    f"User {user_id} denied access to case {case_id} (owner: {case.user_id})"
                )
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Return progress summary
            return {
                "case_id": case.case_id,
                "status": case.status.value,
                "current_stage": (
                    case.current_stage.value if case.current_stage else None
                ),
                "milestones_completed": case.progress.completed_milestones,
                "pending_milestones": case.progress.pending_milestones,
                "current_turn": case.current_turn,
                "is_stuck": case.is_stuck if hasattr(case, "is_stuck") else False,
            }

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            logger.error(f"Failed to get progress for case {case_id}: {e}")
            raise ServiceException(f"Progress retrieval failed: {str(e)}") from e

    # ============================================================
    # Attachment Preprocessing
    # ============================================================

    async def _preprocess_attachment(
        self,
        case: "Case",
        attachment: Attachment,
        user_id: str,
        turn_number: int,
        processing_mode: str = "triage",
    ) -> Evidence:
        """Preprocess a single attachment through classification and extraction.

        Args:
            case: Case entity (for case_id context)
            attachment: Raw attachment from turn payload
            user_id: User who submitted the attachment
            turn_number: Current turn number
            processing_mode: Processing mode from query classification
                (triage or directed_analysis)

        Returns:
            Evidence record with preprocessed content

        Raises:
            ServiceException: If preprocessing or storage fails
        """
        from uuid import uuid4

        content = attachment.content.decode("utf-8", errors="replace")

        # Convert dict source_metadata to SourceMetadata for classifier compatibility
        source_meta = None
        if attachment.source_metadata:
            from faultmaven.models.api import SourceMetadata

            source_meta = SourceMetadata(**attachment.source_metadata)

        # Classify and extract structural index.
        # COLD START ORIENTATION: This extractor pass runs for EVERY file
        # regardless of processing mode. In Triage mode, the structural index
        # IS the user-facing answer. In Directed Analysis mode, it serves as
        # internal orientation — a map of the file's contents (time range,
        # services, error distribution) so the DA's LLM can formulate targeted
        # search strategies instead of searching blind. Do NOT skip this step
        # for DA-mode files.
        preprocessing_result = await self.preprocessing_service.classify_and_extract(
            content=content,
            filename=attachment.filename,
            source_metadata=source_meta,
        )

        # Create evidence record (form=DOCUMENT for all turn attachments)
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            form=EvidenceForm.DOCUMENT,
            category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
            source_type=EvidenceSourceType.LOGS,
            primary_purpose="user_submitted_data",
            summary=preprocessing_result.summary,
            preprocessed_content=preprocessing_result.structural_index,
            data_type=preprocessing_result.data_type.value,
            content_hash=preprocessing_result.content_hash,
            content_size_bytes=len(attachment.content),
            preprocessing_method=preprocessing_result.extraction_method,
            extraction_method=preprocessing_result.extraction_method,
            processing_mode=processing_mode,
            collected_by=user_id,
            collected_at_turn=turn_number,
            original_filename=attachment.filename,
        )

        # Store raw content for deep analysis / search_file access
        if self.file_storage_service:
            storage_result = await self.file_storage_service.store_file(
                file_data=attachment.content,
                original_filename=attachment.filename,
                organization_id=getattr(case, "organization_id", "default"),
                case_id=case.case_id,
                mime_type=attachment.content_type,
            )
            evidence.content_ref = storage_result.get("file_path")

        return evidence

    # ============================================================
    # Intent-Based Query Handlers
    # ============================================================

    async def _handle_status_transition(
        self,
        case: "Case",
        user_message: str,
        from_status: Optional[str],
        to_status: Optional[str],
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        """Handle status transition intent with validation.

        Args:
            case: Case entity
            user_message: User's message explaining the transition
            from_status: Expected current status
            to_status: Requested new status
            user_confirmed: Whether user confirmed the transition

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing status transition: {from_status} → {to_status} "
            f"(confirmed={user_confirmed}) for case {case.case_id}"
        )

        # Validate transition request
        if not to_status:
            raise ValueError("to_status is required for status_transition intent")

        # Delegate to milestone engine with structured intent
        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=None,
            intent_type="status_transition",
            intent_data={
                "from_status": from_status,
                "to_status": to_status,
                "user_confirmed": user_confirmed,
            },
        )

        return result

    async def _handle_confirmation(
        self, case: "Case", user_message: str, confirmation_value: Optional[bool]
    ) -> Dict[str, Any]:
        """Handle yes/no confirmation intent.

        Args:
            case: Case entity
            user_message: User's confirmation message
            confirmation_value: True for yes, False for no

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing confirmation: {confirmation_value} for case {case.case_id}"
        )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=None,
            intent_type="confirmation",
            intent_data={"value": confirmation_value},
        )

        return result

    async def _handle_hypothesis_action(
        self,
        case: "Case",
        user_message: str,
        hypothesis_id: Optional[str],
        action: Optional[str],
    ) -> Dict[str, Any]:
        """Handle hypothesis action intent (validate/refute/retire).

        Args:
            case: Case entity
            user_message: User's message about the hypothesis
            hypothesis_id: Target hypothesis ID
            action: Action to perform

        Returns:
            Result dict with agent response and updated case
        """
        logger.info(
            f"Processing hypothesis action: {action} on {hypothesis_id} for case {case.case_id}"
        )

        if not hypothesis_id or not action:
            raise ValueError(
                "hypothesis_id and action required for hypothesis_action intent"
            )

        result = await self.engine.process_turn(
            case=case,
            user_message=user_message,
            attachments=None,
            intent_type="hypothesis_action",
            intent_data={"hypothesis_id": hypothesis_id, "action": action},
        )

        return result

    async def _handle_greeting(self, case: "Case") -> Dict[str, Any]:
        """Handle greeting intent without LLM.

        Args:
            case: Case entity

        Returns:
            Result dict with static agent response and updated case
        """
        logger.info(f"Processing greeting for case {case.case_id}")

        # Static response (saving tokens and latency)
        agent_response = (
            "Hello! I'm FaultMaven, an expert SRE troubleshooting copilot. "
            "I can help you diagnose issues, analyze logs, and verify solutions. "
            "Please describe the problem you're observing."
        )

        # No engine call needed - manually construct result
        return {
            "agent_response": agent_response,
            "suggested_follow_ups": [
                {
                    "label": "Describe your issue",
                    "action_type": "question_template",
                    "payload": "I'm seeing an issue with...",
                },
                {
                    "label": "Paste error logs",
                    "action_type": "upload_data",
                    "payload": "Upload or paste your error logs",
                },
            ],
            "case_updated": case,
            "metadata": {
                "progress_made": False,
                "milestones_completed": [],
            },
        }

    def _detect_intent_heuristic(self, message: str) -> Optional[IntentType]:
        """Detect intent from message content using simple heuristics.

        Args:
            message: User message text

        Returns:
            Detected IntentType or None
        """
        clean_msg = message.strip().lower()

        # Greeting patterns (case-insensitive)
        # Matches: "Hi", "Hello", "Hi FaultMaven", "Greetings", "Help"
        # Does NOT match: "Hi, the db is down", "Hello, I have an error"
        greeting_pattern = r"^(hi|hello|hey|greetings|help)( faultmaven)?[\.!]*$"

        if re.match(greeting_pattern, clean_msg):
            return IntentType.GREETING

        return None

    @trace("investigation_service_transition_to_investigating")
    async def transition_to_investigating(
        self, case_id: str, user_id: str, confirmed_description: str
    ) -> Case:
        """
        Transition case from INQUIRY to INVESTIGATING.

        Called when user confirms the problem statement during inquiry phase.

        Args:
            case_id: Case identifier
            user_id: User making the request
            confirmed_description: Confirmed problem description

        Returns:
            Updated case

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
            ServiceException: If transition fails or invalid state
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Validate current status
            if case.status != CaseStatus.INQUIRY:
                raise ServiceException(
                    f"Cannot transition to INVESTIGATING: case is in {case.status.value} status"
                )

            # Ensure inquiry data is properly set for INVESTIGATING transition
            if not case.inquiry.proposed_problem_statement:
                # Use confirmed_description as the problem statement
                case.inquiry.proposed_problem_statement = confirmed_description

            if not case.inquiry.problem_statement_confirmed:
                case.inquiry.problem_statement_confirmed = True
                case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)

            if not case.inquiry.decided_to_investigate:
                case.inquiry.decided_to_investigate = True
                case.inquiry.decision_made_at = datetime.now(timezone.utc)

            # Update case
            case.description = confirmed_description
            case.status = CaseStatus.INVESTIGATING

            # Path selection is now DEFERRED until symptom verification (Bug #3 fix)
            # Logic moved to MilestoneEngine._process_response_structured via automatic check
            case.path_selection = None

            # Save
            updated_case = await self.repository.save(case)

            logger.info(
                f"Transitioned case {case_id} to INVESTIGATING with description: "
                f"{confirmed_description[:100]}..."
            )

            return updated_case

        except (NotFoundError, PermissionDeniedException, ServiceException):
            raise
        except Exception as e:
            logger.error(f"Failed to transition case {case_id} to INVESTIGATING: {e}")
            raise ServiceException(f"Status transition failed: {str(e)}") from e

    @trace("investigation_service_close_case")
    async def close_case(self, case_id: str, user_id: str, closure_reason: str) -> Case:
        """
        Close a case.

        Args:
            case_id: Case identifier
            user_id: User making the request
            closure_reason: Why the case is being closed
                (resolved | abandoned | escalated | inquiry_only | duplicate | other)

        Returns:
            Updated case

        Raises:
            NotFoundError: If case not found
            PermissionDeniedException: If user not authorized
        """
        try:
            # Retrieve case
            case = await self.repository.get(case_id)
            if not case:
                raise NotFoundError("Case", case_id)

            # Check permissions
            if case.user_id != user_id:
                raise PermissionDeniedException(
                    f"User {user_id} not authorized for case {case_id}"
                )

            # Update status and timestamps (use model_copy to bypass field-by-field validation)
            now = datetime.now(timezone.utc)
            updated_case_data = case.model_copy(
                update={
                    "status": CaseStatus.CLOSED,
                    "closure_reason": closure_reason,
                    "closed_at": now,
                },
                deep=True,
            )

            # Save
            updated_case = await self.repository.save(updated_case_data)

            logger.info(f"Closed case {case_id}, reason: {closure_reason}")

            return updated_case

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            logger.error(f"Failed to close case {case_id}: {e}")
            raise ServiceException(f"Case closure failed: {str(e)}") from e
