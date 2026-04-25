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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_pipeline import generate_implicit_query
from faultmaven.exceptions import (
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
    ValidationException,
)
from faultmaven.infrastructure.observability.evidence_metrics import (
    EVIDENCE_DEDUP_HITS_TOTAL,
    EVIDENCE_RECLASSIFICATION_TOTAL,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.models.api import DataType
from faultmaven.models.api_models import (
    AttachmentResult,
    IntentType,
    ProgressTransparencyInfo,
    QueryIntent,
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

_DATA_TYPE_TO_SOURCE_TYPE: dict[DataType, EvidenceSourceType] = {
    DataType.LOGS_AND_ERRORS: EvidenceSourceType.LOGS,
    DataType.ERROR_REPORT: EvidenceSourceType.LOGS,
    DataType.COMMAND_OUTPUT: EvidenceSourceType.LOGS,
    DataType.TRACE_DATA: EvidenceSourceType.LOGS,
    DataType.METRICS_AND_PERFORMANCE: EvidenceSourceType.METRICS,
    DataType.PROFILING_DATA: EvidenceSourceType.METRICS,
    DataType.STRUCTURED_CONFIG: EvidenceSourceType.CONFIGURATION,
    DataType.SOURCE_CODE: EvidenceSourceType.CODE,
    DataType.DOCUMENTATION: EvidenceSourceType.TEXT,
    DataType.UNSTRUCTURED_TEXT: EvidenceSourceType.TEXT,
    DataType.VISUAL_EVIDENCE: EvidenceSourceType.IMAGE,
    DataType.UNANALYZABLE: EvidenceSourceType.TEXT,
}


def _infer_source_type(data_type: DataType) -> EvidenceSourceType:
    return _DATA_TYPE_TO_SOURCE_TYPE.get(data_type, EvidenceSourceType.TEXT)


# DataType string value → human-friendly phrasing for cooperative clarification
# suggestions. Users see `label` on the suggestion card; `long` goes into the
# pre-composed query_submit payload. UNANALYZABLE is intentionally omitted —
# not a choice users can meaningfully select. UNSTRUCTURED_TEXT is handled as
# the "Something else" fallback separately to avoid duplicate suggestions.
_CLARIFICATION_FRIENDLY_NAMES: Dict[str, Dict[str, str]] = {
    "logs_and_errors": {"label": "Application logs", "long": "application logs"},
    "error_report": {
        "label": "Error report",
        "long": "an error report or stack trace",
    },
    "trace_data": {"label": "Trace data", "long": "trace data"},
    "metrics_and_performance": {
        "label": "Metrics",
        "long": "metrics or performance data",
    },
    "profiling_data": {"label": "Profiling data", "long": "profiling data"},
    "command_output": {"label": "Command output", "long": "command output"},
    "structured_config": {"label": "Configuration", "long": "configuration"},
    "source_code": {"label": "Source code", "long": "source code"},
    "documentation": {"label": "Documentation", "long": "documentation or notes"},
    "visual_evidence": {"label": "Screenshot", "long": "a screenshot or image"},
}

# Fallback phrasing used for the "Something else" option and applied when the
# classifier's only suggested type is UNSTRUCTURED_TEXT.
_CLARIFICATION_FALLBACK_LABEL = "Something else"
_CLARIFICATION_FALLBACK_LONG = "unstructured text"


def _build_classification_clarification_suggestions(
    preprocess_results: List["_PreprocessedAttachment"],
) -> List[SuggestedActionResponse]:
    """Emit COOPERATIVE suggestions when an attachment hit classification_failed.

    Per-turn file limit is 1, so we expect at most one classification_failed
    result. Generates up to 3 type-specific suggestions from the classifier's
    `suggested_types` plus a "Something else" fallback. Always emits at least
    the fallback when any classification_failed is present.

    Returns an empty list when no classification failure occurred this turn.
    """
    failed = [r for r in preprocess_results if r.classification_failed]
    if not failed:
        return []

    target = failed[0]
    filename = target.attachment_filename or "the uploaded file"

    suggestions: List[SuggestedActionResponse] = []
    seen: set = set()

    for dt_value in target.suggested_types or []:
        if dt_value in seen:
            continue
        seen.add(dt_value)
        # unstructured_text collapses into the fallback — don't surface twice
        if dt_value == "unstructured_text":
            continue
        friendly = _CLARIFICATION_FRIENDLY_NAMES.get(dt_value)
        if friendly is None:
            continue
        suggestions.append(
            SuggestedActionResponse(
                label=friendly["label"],
                type="COOPERATIVE",
                cooperative_action="query_submit",
                payload=(
                    f'Treat the previously uploaded file ("{filename}") as '
                    f'{friendly["long"]} and analyze it.'
                ),
                body=f'Treat as {friendly["long"]}.',
            )
        )
        if len(suggestions) >= 3:
            break

    # Always include the "Something else" fallback — last position.
    suggestions.append(
        SuggestedActionResponse(
            label=_CLARIFICATION_FALLBACK_LABEL,
            type="COOPERATIVE",
            cooperative_action="query_submit",
            payload=(
                f'Treat the previously uploaded file ("{filename}") as '
                f"{_CLARIFICATION_FALLBACK_LONG} and try to analyze it."
            ),
            body=f"Treat as {_CLARIFICATION_FALLBACK_LONG}.",
        )
    )

    return suggestions


@dataclass
class _PreprocessedAttachment:
    """Internal result of `_preprocess_attachment`.

    Carries both the Evidence (new or existing-on-duplicate) and dedup signals
    the caller needs to decide whether to append to `case.evidence` and how to
    populate `AttachmentResult.duplicate_of`. Also carries classification
    clarification hints when the heuristic classifier couldn't confidently
    classify the attachment — see `_build_classification_clarification_suggestions`.
    """

    evidence: Evidence
    duplicate_of: Optional[str] = None
    duplicate_turn: Optional[int] = None
    # Classification clarification — populated only when the preprocessing
    # result had extraction_method="classification_failed". Contains 0–3
    # DataType enum values (as strings) suggested by the classifier for
    # cooperative-clarification UX. Empty/None when classification succeeded.
    classification_failed: bool = False
    suggested_types: Optional[List[str]] = None
    attachment_filename: Optional[str] = None


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
        self.intent_resolver = IntentResolver(milestone_engine.llm_provider)

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
            preprocess_results: List[_PreprocessedAttachment] = []
            if payload.has_attachments:
                for attachment in payload.attachments:
                    result = await self._preprocess_attachment(
                        case,
                        attachment,
                        user_id,
                        next_turn,
                        processing_mode=processing_mode,
                    )
                    preprocess_results.append(result)
                    evidence_created.append(result.evidence)
                    # On dedup, the existing Evidence is already on `case.evidence`
                    # from the DB load — don't double-append.
                    if result.duplicate_of is None:
                        case.evidence.append(result.evidence)

            # Determine query (explicit or implicit)
            query = payload.query
            if not payload.has_query and payload.has_attachments:
                query = generate_implicit_query(payload.attachments, evidence_created)

            # 2. Build user message and update case in-memory (NOT persisted yet).
            #    Deferring the save until after LLM processing ensures atomic
            #    persistence of both user and agent messages.  If the LLM fails,
            #    the database is untouched — no orphaned user message, no inflated
            #    turn count — so the client can cleanly retry the same turn.
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

            # ── STEP 2: LLM INFERENCE ──
            # Heuristic check for greetings if intent is CONVERSATION (default)
            if intent_type == IntentType.CONVERSATION and query:
                heuristic_intent = self._detect_intent_heuristic(query)
                if heuristic_intent:
                    intent_type = heuristic_intent
                    logger.info(
                        f"Heuristic detected intent {intent_type.value} for message: '{query}'"
                    )

            # Intent resolution: match typed text against last turn's suggestions.
            # Only runs when no structured intent and the case has suggestions
            # with intent metadata from the previous turn.
            if (
                intent_type == IntentType.CONVERSATION
                and query
                and not payload.has_attachments
                and case.last_suggestions
            ):
                resolved_intent = await self.intent_resolver.resolve(
                    user_message=query,
                    last_suggestions=case.last_suggestions,
                )
                if resolved_intent:
                    try:
                        resolved_qi = QueryIntent(**resolved_intent)
                        intent = resolved_qi
                        intent_type = resolved_qi.type
                        logger.info(
                            f"Intent resolved from suggestions: {intent_type.value} "
                            f"for message: '{query[:50]}...'"
                        )
                    except Exception:
                        logger.warning(
                            "Failed to parse resolved intent, "
                            "falling back to conversation",
                            exc_info=True,
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

            # Reverse-substitute PII placeholders so user sees real values.
            # The LLM worked with redacted content; the user should not.
            redaction_ctx = result.get("redaction_ctx")
            if redaction_ctx:
                agent_response_text = redaction_ctx.reverse(agent_response_text)

            # 3b. Store suggestions with intent metadata for next turn's
            #      intent resolver (bounded choice matching).
            raw_follow_ups = result.get("suggested_follow_ups", [])
            updated_case.last_suggestions = [
                s for s in raw_follow_ups if s.get("intent")
            ] or None

            # 4. Save agent response AND user message atomically.
            #    The user message was appended in-memory at step 2 but not
            #    persisted.  This single save commits both messages together,
            #    guaranteeing no half-completed turns in the database.
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
            suggested_actions = [
                SuggestedActionResponse(
                    label=f["label"],
                    type=f["action_type"],
                    payload=f["payload"],
                    body=f.get("body"),
                    cooperative_action=f.get("cooperative_action"),
                    hints=f.get("hints"),
                    intent=f.get("intent"),
                )
                for f in raw_follow_ups
            ]

            # Prepend classification-clarification suggestions when this turn's
            # attachment hit classification_failed. User-in-the-loop guidance
            # takes priority over generic follow-up suggestions from the engine.
            clarification = _build_classification_clarification_suggestions(
                preprocess_results
            )
            if clarification:
                suggested_actions = clarification + suggested_actions

            response = TurnResponse(
                agent_response=agent_response_text,
                turn_number=updated_case.current_turn,
                milestones_completed=result.get("metadata", {}).get(
                    "milestones_completed", []
                ),
                case_status=updated_case.status,
                progress_made=result.get("metadata", {}).get("progress_made", False),
                attachments_processed=[
                    AttachmentResult(
                        evidence_id=res.evidence.evidence_id,
                        filename=att.filename,
                        data_type=res.evidence.data_type or "",
                        file_size=res.evidence.content_size_bytes,
                        processing_status=(
                            "duplicate" if res.duplicate_of else "completed"
                        ),
                        uploaded_at=datetime.now(timezone.utc).isoformat(),
                        source_type=(
                            att.source_metadata.get("source_type", "file_upload")
                            if att.source_metadata
                            else "file_upload"
                        ),
                        duplicate_of=res.duplicate_of,
                        duplicate_turn=res.duplicate_turn,
                    )
                    for att, res in zip(payload.attachments, preprocess_results)
                ],
                suggested_actions=suggested_actions,
                progress_transparency=self._build_progress_transparency(
                    result.get("metadata", {})
                ),
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
            - current_turn

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
    ) -> _PreprocessedAttachment:
        """Preprocess a single attachment through classification and extraction.

        Args:
            case: Case entity (for case_id context)
            attachment: Raw attachment from turn payload
            user_id: User who submitted the attachment
            turn_number: Current turn number
            processing_mode: Processing mode from query classification
                (triage or directed_analysis)

        Returns:
            `_PreprocessedAttachment` wrapping the Evidence plus optional
            dedup metadata. On content-hash duplicate within the same case,
            the returned Evidence is the existing row and `duplicate_of` /
            `duplicate_turn` are populated; no new Evidence is created and
            no raw file is re-stored.

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

        # Per-case content-hash dedup short-circuit.
        # An attachment whose content_hash already exists on this case returns
        # the existing Evidence instead of creating a new row. No raw file
        # re-storage either — storage already has the bytes under the original
        # content_ref.
        existing = None
        if preprocessing_result.content_hash:
            try:
                existing = await self.repository.find_by_content_hash(
                    case.case_id, preprocessing_result.content_hash
                )
            except AttributeError:
                # Repository doesn't implement dedup lookup — graceful fallback.
                # Happens in legacy test doubles. Silently skip dedup.
                existing = None
        if existing is not None:
            logger.info(
                "Duplicate upload detected: file '%s' matches %s (turn %s) "
                "in case %s — reusing existing evidence",
                attachment.filename,
                existing.evidence_id,
                existing.collected_at_turn,
                case.case_id,
            )
            EVIDENCE_DEDUP_HITS_TOTAL.inc()
            return _PreprocessedAttachment(
                evidence=existing,
                duplicate_of=existing.evidence_id,
                duplicate_turn=existing.collected_at_turn,
            )

        # Lift the namespaced evidence_metadata block out of the
        # preprocessing result (classifier confidence in Phase 1, extractor
        # attempts in Phase 2, entity overflow markers in Phase 4). Absent
        # for any PreprocessingResult produced before the Phase 1 wiring,
        # hence the defensive extraction below. See
        # docs/architecture/data-and-storage/schemas/case-schema.md §4.3.
        #
        # Accept only dict-shaped metadata — test doubles may return a
        # MagicMock from `.get()` which would otherwise fail Evidence's
        # Dict[str, Any] validator downstream.
        pp_metadata = preprocessing_result.extraction_metadata
        evidence_metadata: Optional[Dict[str, Any]] = None
        if isinstance(pp_metadata, dict):
            candidate = pp_metadata.get("evidence_metadata")
            if isinstance(candidate, dict):
                evidence_metadata = candidate

        # Create evidence record (form=DOCUMENT for all turn attachments)
        evidence = Evidence(
            evidence_id=f"ev_{uuid4().hex[:12]}",
            form=EvidenceForm.DOCUMENT,
            category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
            source_type=_infer_source_type(preprocessing_result.data_type),
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
            metadata=evidence_metadata,
            # Phase 3 — populate coverage timestamps when the preprocessor
            # parsed them out of the raw content. NULL for timeless
            # evidence (configs, code, short pastes); the Phase 3b query
            # filters these out of time-window queries naturally.
            coverage_start_ts=preprocessing_result.coverage_start_ts,
            coverage_end_ts=preprocessing_result.coverage_end_ts,
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

            # Flip the sidecar `linked` flag so the orphan-cleanup job knows
            # this file has an Evidence row referencing it. Best-effort —
            # `mark_linked` swallows failures and returns False. Legacy
            # storage services without this method are handled gracefully.
            mark_linked = getattr(self.file_storage_service, "mark_linked", None)
            if mark_linked is not None and evidence.content_ref:
                try:
                    await mark_linked(evidence.content_ref)
                except Exception as e:
                    logger.warning(
                        "mark_linked failed for %s (non-fatal, file stays "
                        "as orphan candidate until TTL): %s",
                        evidence.content_ref,
                        e,
                    )

        # Phase 4 — persist extracted entities into the case-level
        # registry. Best-effort: an entity upsert failure must not
        # poison evidence persistence. Runs only when the preprocessor
        # emitted observations (the feature flag is checked there).
        entities_payload = getattr(preprocessing_result, "entities", None) or []
        if entities_payload:
            try:
                from faultmaven.modules.case.domain.models import (
                    CaseEntity,
                    EntityType,
                )

                case_entities: list[CaseEntity] = []
                for obs in entities_payload:
                    try:
                        entity_type = EntityType(obs["entity_type"])
                    except (KeyError, ValueError):
                        continue
                    value = obs.get("entity_value")
                    if not isinstance(value, str) or not value:
                        continue
                    case_entities.append(
                        CaseEntity(
                            case_id=case.case_id,
                            entity_type=entity_type,
                            entity_value=value[:255],
                            evidence_id=evidence.evidence_id,
                            mention_count=max(1, int(obs.get("mention_count", 1) or 1)),
                            in_error_context=bool(obs.get("in_error_context", False)),
                            # Phase 3a coverage start doubles as the
                            # earliest timestamp for entities extracted
                            # from this evidence. NULL for timeless
                            # content (configs, short pastes).
                            first_seen_ts=preprocessing_result.coverage_start_ts,
                        )
                    )
                if case_entities:
                    upsert = getattr(self.repository, "upsert_case_entities", None)
                    if upsert is not None:
                        await upsert(case.case_id, evidence.evidence_id, case_entities)
            except Exception as exc:
                logger.warning(
                    "upsert_case_entities failed for %s (non-fatal): %s",
                    evidence.evidence_id,
                    exc,
                )

        # Surface classification clarification hints when the heuristic
        # classifier produced a low-confidence result. Suggested types are
        # propagated by PreprocessingService via extraction_metadata as a
        # list of DataType string values.
        is_classification_failed = (
            preprocessing_result.extraction_method == "classification_failed"
        )
        suggested_types: Optional[List[str]] = None
        if is_classification_failed:
            suggested_types = (
                preprocessing_result.extraction_metadata.get("suggested_types") or []
            )

        return _PreprocessedAttachment(
            evidence=evidence,
            classification_failed=is_classification_failed,
            suggested_types=suggested_types,
            attachment_filename=attachment.filename,
        )

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

    def _build_progress_transparency(
        self, metadata: Dict[str, Any]
    ) -> Optional[ProgressTransparencyInfo]:
        """Build ProgressTransparencyInfo from turn metadata.

        Returns None if progress transparency is not active (silent mode).
        """
        if not metadata.get("progress_transparent"):
            return None

        return ProgressTransparencyInfo(
            active=True,
            pending_milestone=metadata.get("pending_milestone"),
            milestone_description=metadata.get("milestone_description"),
            repair_type=metadata.get("stagnation_type"),
        )

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
            "Hello! I'm FaultMaven, your AI-powered troubleshooting copilot. "
            "I can help you diagnose issues, analyze logs, and verify solutions. "
            "Please describe the problem you're observing."
        )

        # No engine call needed - manually construct result
        return {
            "agent_response": agent_response,
            "suggested_follow_ups": [
                {
                    "label": "Describe your issue",
                    "action_type": "FREE_SPEECH",
                    "payload": "What problem are you experiencing?",
                    "hints": [
                        "symptoms",
                        "error messages",
                        "timeline",
                        "affected services",
                    ],
                },
                {
                    "label": "Share error logs",
                    "action_type": "EVIDENCE",
                    "payload": "Application error logs from the affected service",
                    "body": "Error logs will help identify the root cause faster.",
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

    @trace("investigation_service_reclassify_evidence")
    async def reclassify_evidence(
        self,
        case_id: str,
        evidence_id: str,
        user_id: str,
        data_type: DataType,
        trigger: str = "api",
    ) -> Evidence:
        """Re-run preprocessing on an existing evidence row under a
        user-specified data type.

        Phase 1.5 — implements the "escape hatch" for confident
        misclassification. The caller (PATCH endpoint or
        ``reclassify_evidence`` agent tool) provides the new data type;
        this method fetches the stored raw bytes, re-runs extraction
        under ``user_override=data_type``, and overwrites the
        ``preprocessed_content`` / ``data_type`` / ``metadata`` fields
        on the same evidence row.

        Args:
            case_id: Case owning the evidence.
            evidence_id: Evidence being reclassified.
            user_id: User making the request (authorisation check).
            data_type: Target data type (DataType enum value).
            trigger: Where the request came from — ``api`` (direct
                PATCH) or ``agent_tool`` (reclassify_evidence tool).
                Labels the observability counter.

        Returns:
            The updated Evidence row with new preprocessed_content and
            metadata.

        Raises:
            NotFoundError: case or evidence not found.
            PermissionDeniedException: user does not own the case.
            ValidationException: evidence has no ``content_ref`` —
                reclassification requires stored raw bytes to re-extract.
            ServiceException: any other failure (storage fetch,
                preprocessing).
        """
        case = await self.repository.get(case_id)
        if not case:
            raise NotFoundError("Case", case_id)
        if case.user_id != user_id:
            raise PermissionDeniedException(
                f"User {user_id} not authorized for case {case_id}"
            )

        evidence_index: Optional[int] = None
        for i, ev in enumerate(case.evidence or []):
            if ev.evidence_id == evidence_id:
                evidence_index = i
                break
        if evidence_index is None:
            raise NotFoundError("Evidence", evidence_id)

        evidence = case.evidence[evidence_index]
        if not evidence.content_ref:
            raise ValidationException(
                f"Evidence {evidence_id} has no stored raw file — "
                "reclassification requires re-running the extractor "
                "over the original content, which is not available for "
                "evidence that was created without file storage."
            )
        if not self.file_storage_service:
            raise ServiceException(
                "File storage service unavailable; cannot re-extract"
            )
        if not self.preprocessing_service:
            raise ServiceException(
                "Preprocessing service unavailable; cannot reclassify"
            )

        # Fetch raw bytes + decode. Storage returns bytes; extractors
        # operate on strings (UTF-8 is the convention per the upload path).
        raw_bytes = await self.file_storage_service.retrieve_file(evidence.content_ref)
        content = raw_bytes.decode("utf-8", errors="replace")
        filename = evidence.original_filename or "evidence"

        previous_metadata = evidence.metadata

        preprocessing_result = await self.preprocessing_service.reclassify_evidence(
            content=content,
            filename=filename,
            user_override=data_type,
            previous_metadata=previous_metadata,
        )

        # Lift the updated evidence_metadata block from the result.
        pp_metadata = preprocessing_result.extraction_metadata
        new_evidence_metadata: Optional[Dict[str, Any]] = None
        if isinstance(pp_metadata, dict):
            candidate = pp_metadata.get("evidence_metadata")
            if isinstance(candidate, dict):
                new_evidence_metadata = candidate

        previous_type = evidence.data_type
        new_type = preprocessing_result.data_type.value

        # Update the row in place via model_copy to bypass cross-field
        # validators, then write back the list so Case's own validators
        # see a consistent state.
        updated_evidence = evidence.model_copy(
            update={
                "data_type": new_type,
                "preprocessed_content": preprocessing_result.structural_index,
                "summary": preprocessing_result.summary,
                "source_type": _infer_source_type(preprocessing_result.data_type),
                "preprocessing_method": preprocessing_result.extraction_method,
                "extraction_method": preprocessing_result.extraction_method,
                "metadata": new_evidence_metadata,
            },
            deep=True,
        )
        new_evidence_list = list(case.evidence)
        new_evidence_list[evidence_index] = updated_evidence
        updated_case = case.model_copy(
            update={"evidence": new_evidence_list}, deep=True
        )

        await self.repository.save(updated_case)

        EVIDENCE_RECLASSIFICATION_TOTAL.labels(
            from_type=str(previous_type or "unknown"),
            to_type=new_type,
            trigger=trigger,
        ).inc()

        logger.info(
            "Reclassified evidence %s in case %s: %s -> %s (trigger=%s)",
            evidence_id,
            case_id,
            previous_type,
            new_type,
            trigger,
        )

        return updated_evidence

    @trace("investigation_service_close_case")
    async def close_case(self, case_id: str, user_id: str, closure_reason: str) -> Case:
        """
        Close a case.

        Wrapped in ``update_case_with_retry`` so a concurrent save
        (OCC conflict) reloads and re-applies the closure rather than
        silently losing it. The mutator is idempotent — setting
        ``status=CLOSED`` + ``closure_reason`` on a fresh Case produces
        the same result.

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
        from faultmaven.modules.case.utils import update_case_with_retry

        # One up-front access check; user_id doesn't change across retry
        # attempts so re-checking inside the loop buys nothing.
        existing = await self.repository.get(case_id)
        if not existing:
            raise NotFoundError("Case", case_id)
        if existing.user_id != user_id:
            raise PermissionDeniedException(
                f"User {user_id} not authorized for case {case_id}"
            )

        async def apply(case: Case) -> None:
            # Cross-field validators on Case require object.__setattr__
            # when setting multiple interdependent terminal fields.
            now = datetime.now(timezone.utc)
            object.__setattr__(case, "status", CaseStatus.CLOSED)
            object.__setattr__(case, "closure_reason", closure_reason)
            object.__setattr__(case, "closed_at", now)

        try:
            updated_case = await update_case_with_retry(self.repository, case_id, apply)
            logger.info(f"Closed case {case_id}, reason: {closure_reason}")
            return updated_case

        except (NotFoundError, PermissionDeniedException):
            raise
        except Exception as e:
            logger.error(f"Failed to close case {case_id}: {e}")
            raise ServiceException(f"Case closure failed: {str(e)}") from e
