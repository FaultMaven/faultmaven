"""ConversionService: Orchestrates document-to-runbook conversion.

Pipeline:
1. Preprocess uploaded document (6 stages)
2. Analyze document for failure modes (LLM)
3. Convert each failure mode to a runbook (LLM, parallel or sequential)
4. Validate and score each draft
5. Persist drafts to the database — a draft's text is a column of its row
   (``conversion_drafts.content``, #1950), never a file on a pod's disk
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.infrastructure.llm.truncation import generate_with_truncation_retry
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    UploadedFileModel,
)
from faultmaven.infrastructure.security.case_redaction import model_boundary_redaction
from faultmaven.modules.auth.contracts import IAccountDirectory, is_team_member
from faultmaven.modules.knowledge.domain.case_authoring import (
    CASE_ID_RULE,
    DE_IDENTIFICATION_RULES,
    TECHNOLOGY_RULE,
    draft_title,
    mint_case_runbook_id,
)
from faultmaven.modules.knowledge.domain.global_authoring import (
    ensure_global_authoring_allowed,
)
from faultmaven.modules.knowledge.domain.item_title import (
    fit_to_column,
    sanitize_item_title,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    CaseConversionRequest,
    ConversionDraft,
    ConversionError,
    ConversionErrorCode,
    ConversionResponse,
    ConversionStatus,
    DraftStatus,
    FailureModeAnalysis,
    QualityScore,
    SourceAssessment,
    SourceFileInfo,
    SourceType,
    ValidationResult,
    VerifyResponse,
    generate_conversion_id,
    generate_draft_id,
    generate_runbook_id,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.draft_slots import (
    _refuse_modes_whose_id_is_taken,
    claim_case_draft_slot,
    refuse_if_draft_slot_taken,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.errors import (
    ConversionRejectedError,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.failure_modes import (
    _force_frontmatter_id,
    _partition_failure_modes,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.job_persistence import (
    _persist_job,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.pipeline import (
    _analyze_document,
    _knowledge_route_kwargs,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.prompts import (
    CONVERSION_SYSTEM_PROMPT,
    PARALLEL_THRESHOLD,
    RUNBOOK_MAX_TOKENS,
    RUNBOOK_MAX_TOKENS_CEILING,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.runbook_scan import (
    QUALITY_WARNING_THRESHOLD,
    _release_live_case_key_if_drained,
    _scan_for_runbooks_impl,
)
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    DocumentPreprocessor,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    avalidate_and_score,
)
from faultmaven.modules.knowledge.draft_content import (
    DRAFT_CONTENT_MAX_CHARS,
    draft_content_too_long_message,
    ensure_draft_content_within_bound,
)
from faultmaven.modules.knowledge.taxonomy import RunbookSeverity, render_vocabulary
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)
from faultmaven.utils.frontmatter import match_frontmatter
from faultmaven.utils.line_endings import normalize_line_endings
from faultmaven.utils.runbook_id import (
    knowledge_root,
    runbook_id_from_parts,
)

logger = logging.getLogger(__name__)

#: The scan's refusal under ``TENANT_PROVIDER=multi`` (#1950): the replicas
#: share no disk, so there is no runbook directory to import from.
SCAN_UNAVAILABLE_MULTI_MSG = (
    "This deployment has no runbook directory; upload or convert instead"
)


def _already_verified(draft_id: str) -> ConflictError:
    """The 409 for a draft that is no longer a draft. One wording for verify
    and edit; ``verify_batch`` and clients key on ``conflict_reason``."""
    return ConflictError(
        "This runbook has already been verified and ingested",
        resource_type="draft",
        resource_id=draft_id,
        conflict_reason="already_verified",
    )


def _verification_lost(
    draft_id: str,
    current_status: Optional[str],
    knowledge_item_id: str,
    withdrawn: bool,
) -> ConflictError:
    """The 409 for a verify whose compare-and-swap missed, naming what won.

    ``already_verified`` — another verify won (``verify_batch`` records this
    one as skipped); ``discarded`` — a delete won; otherwise the text was
    edited during ingestion. When the item this verify published could not be
    withdrawn, the sentence says so rather than claiming nothing was published.
    """
    if current_status == DraftStatus.VERIFIED.value:
        reason, sentence = (
            "already_verified",
            "This runbook was verified by another request meanwhile",
        )
    elif current_status is None or current_status == DraftStatus.DISCARDED.value:
        reason, sentence = (
            "discarded",
            "This draft was discarded while it was being verified",
        )
    else:
        reason, sentence = (
            "changed_during_verification",
            "This draft changed while it was being verified",
        )
    if withdrawn:
        outcome = (
            "; nothing was published by this request. Review the latest version "
            "and verify again if needed."
        )
    else:
        outcome = (
            f"; the copy this request published (knowledge item "
            f"{knowledge_item_id}) could not be withdrawn and must be removed by "
            "an operator."
        )
    return ConflictError(
        sentence + outcome,
        resource_type="draft",
        resource_id=draft_id,
        conflict_reason=reason,
    )


@dataclass(frozen=True)
class _PreparedConversion:
    """One failure mode's generation request, built and REDACTED, not yet sent.

    Only :meth:`ConversionService._prepare_conversion` constructs one, and
    ``redacted_messages`` is what came back from the model-boundary redaction — the send
    step takes nothing else, so it cannot reach the router with unredacted text
    (#1901). Preparing every mode before sending any is what makes a refusal
    fail the whole conversion with nothing sent and nothing written.
    """

    knowledge_model: str
    #: Named for what they are — and not ``messages``, which the #1660 census
    #: reads as a case's message rows.
    redacted_messages: List[dict]
    #: The document path's pre-computed id; None on the case path, whose id is
    #: minted from the frontmatter the model writes.
    runbook_id: Optional[str]


# =============================================================================
# ConversionService
# =============================================================================


class ConversionService:
    """Orchestrates the document-to-runbook conversion pipeline."""

    def __init__(
        self,
        llm_router,
        settings,
        db_session_factory=None,
        knowledge_service=None,
        share_repository=None,
        team_service=None,
        sanitizer=None,
        account_reader: Optional[IAccountDirectory] = None,
    ):
        self._llm_router = llm_router
        self._settings = settings
        # The SAME sanitizer instance the investigation engine is handed (the
        # container gives both one). Every model call this service makes —
        # triage, analysis, conversion — redacts what it sends with it at this
        # layer, under ``should_redact`` (#1901): the router's own pass is a
        # property of the default router, which ``LLM_ROUTER_CLASS`` can
        # substitute. None → redaction disabled at DI level, as on the engine.
        self._sanitizer = sanitizer
        self._db_session_factory = db_session_factory
        self._knowledge_service = knowledge_service
        # Source of truth for team visibility (ADR-013 §D4). A team publish
        # target is recorded as a share row on the conversion_job and, on
        # verify, transferred to the promoted knowledge_item. None → team
        # publishing is inert (standalone).
        self._share_repo = share_repository
        # Membership resolver for the team publish target (#854). None →
        # teams don't exist in this deployment, so a team-scoped publish is
        # refused rather than silently minting an unresolvable share target.
        self._team_service = team_service
        # The account store's directory read. A verified runbook's
        # ``verified_by`` frontmatter names the verifier by the display name on
        # their users ROW (#1924): the request's principal carries the JWT's
        # username there, which for an SSO account is the email's local part.
        # None → ``verified_by`` stays empty.
        self._account_reader = account_reader
        self._preprocessor = DocumentPreprocessor(llm_router, settings, sanitizer)
        self._scan_lock = asyncio.Lock()
        # In-flight case-conversion dedup. Keyed by case_id; the value is
        # the running asyncio.Task that other concurrent callers can await.
        # Mirrors `_inflight_vectorize` on MilestoneEngine. Prevents
        # duplicate drafts when the user clicks the runbook affordance
        # twice in rapid succession. See convert_from_case().
        self._inflight_runbook: Dict[str, asyncio.Task] = {}

    @property
    def _data_dir(self) -> Path:
        # The directory the standalone scan imports from, and nothing else: no
        # draft is read from or written to disk (#1950).
        return knowledge_root()

    async def _ensure_team_publish_allowed(
        self, scope: str, team_id: Optional[str], user_id: str
    ) -> None:
        """Refuse a team publish target the caller may not use (#854).

        A ``team_id`` becomes a ``resource_shares`` row on verify — content
        injected into that team's knowledge scope — so it must name a team the
        caller belongs to, the same rule ``CaseService.share_case_with_team``
        enforces (via the shared ``is_team_member`` predicate). Runs at mint
        time, the single point the caller-supplied value enters the pipeline.

        Raises:
            ValidationException: teams are unavailable in this deployment
                (no team service wired — standalone), so a team-scoped
                publish cannot be honored.
            AuthorizationError: the caller is not a member of ``team_id``.
        """
        if scope != "team":
            return
        if not self._team_service:
            raise ValidationException(
                "Team publishing is not available in this deployment"
            )
        if not await is_team_member(self._team_service, user_id, team_id):
            raise AuthorizationError(
                "You can only publish a runbook to a team you belong to"
            )

    # =========================================================================
    # Main Conversion Pipeline
    # =========================================================================

    async def convert_document(
        self,
        file_path: Path,
        content_type: str,
        original_filename: str,
        scope: str,
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
    ) -> ConversionResponse:
        """Full conversion pipeline: preprocess → analyze → convert → validate → persist."""
        await self._ensure_team_publish_allowed(scope, team_id, user_id)

        # Step 0: Verify LLM provider is available
        try:
            knowledge_model = self._settings.llm.get_knowledge_model()
            if not knowledge_model:
                raise ConversionRejectedError(
                    "No LLM provider is configured. Set CHAT_PROVIDER in your .env file "
                    "or configure a provider in Dashboard > LLM Settings.",
                    error_code=ConversionErrorCode.LLM_UNAVAILABLE,
                )
        except AttributeError:
            raise ConversionRejectedError(
                "No LLM provider is configured. Set CHAT_PROVIDER in your .env file "
                "or configure a provider in Dashboard > LLM Settings.",
                error_code=ConversionErrorCode.LLM_UNAVAILABLE,
            )

        conversion_id = generate_conversion_id()
        created_at = datetime.now(timezone.utc)
        warnings: List[str] = []

        # Step 1: Preprocess
        logger.info(
            "document_conversion_started",
            extra={
                "conversion_id": conversion_id,
                "source_filename": original_filename,
                "content_type": content_type,
                "scope": scope,
            },
        )

        preprocessing = await self._preprocessor.preprocess(file_path, content_type)

        if preprocessing.is_rejected:
            raise ConversionRejectedError(
                preprocessing.rejection_reason or "Document rejected",
                error_code=preprocessing.error_code
                or ConversionErrorCode.NOT_ACTIONABLE,
            )

        warnings.extend(preprocessing.warnings)

        # Step 2: Source file information (files are NOT retained on disk, per architectural design)
        source_file = SourceFileInfo(
            filename=original_filename,
            size_bytes=file_path.stat().st_size,
            content_type=content_type,
        )

        # Step 3: Analyze for failure modes
        analysis = await _analyze_document(
            self._llm_router,
            self._settings,
            preprocessing.extracted_text,
            original_filename,
            model_boundary_redaction(self._sanitizer),
        )

        if not analysis.is_actionable or len(analysis.failure_modes) == 0:
            raise ConversionRejectedError(
                "Source document does not contain actionable failure modes. "
                "Runbooks require specific symptoms, diagnostics, and resolution steps.",
                error_code=ConversionErrorCode.NO_FAILURE_MODES,
            )

        # Step 4: Convert each failure mode to a runbook
        drafts, errors = await self._convert_all_failure_modes(
            text=preprocessing.extracted_text,
            failure_modes=analysis.failure_modes,
            scope=scope,
            filename=original_filename,
            conversion_id=conversion_id,
            user_id=user_id,
            enterprise_id=enterprise_id,
            team_id=team_id,
        )

        if errors:
            for err in errors:
                warnings.append(
                    f"Failed to convert '{err.failure_mode_id}': {err.error}"
                )

        # Determine overall status
        if len(drafts) == 0:
            status = ConversionStatus.FAILED
        elif len(errors) > 0:
            status = ConversionStatus.PARTIAL
        else:
            status = ConversionStatus.COMPLETED

        # Step 5: Persist to database
        await _persist_job(
            self._db_session_factory,
            self._share_repo,
            conversion_id=conversion_id,
            user_id=user_id,
            enterprise_id=enterprise_id,
            scope=scope,
            team_id=team_id,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            created_at=created_at,
            warnings=warnings,
            sanitizer=self._sanitizer,
        )

        logger.info(
            "document_conversion_completed",
            extra={
                "conversion_id": conversion_id,
                "failure_modes_detected": len(analysis.failure_modes),
                "drafts_generated": len(drafts),
                "drafts_passed_validation": sum(
                    1 for d in drafts if d.validation.passed
                ),
            },
        )

        return ConversionResponse(
            conversion_id=conversion_id,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            warnings=warnings,
            created_at=created_at,
        )

    # =========================================================================
    # Case-to-Runbook Conversion
    # =========================================================================

    async def convert_from_case(
        self,
        request: "CaseConversionRequest",
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
    ) -> ConversionResponse:
        """Generate a runbook draft from a resolved case using the canonical template.

        Skips preprocessing and analysis (case data is already structured).
        Reuses _convert_single_failure_mode() for LLM generation, validation,
        scoring, and persistence — same pipeline as document-driven conversion.

        Dedup: if a conversion is already in flight for this case, the
        in-flight Task is awaited rather than a duplicate started. Prevents
        duplicate drafts when the user clicks the runbook affordance twice
        in rapid succession (chat-triggered) or when the chat-triggered and
        HTTP-triggered paths race. Mirrors the `_inflight_vectorize`
        pattern on MilestoneEngine.
        """
        # Short-circuit if a conversion for this case is already running.
        inflight = self._inflight_runbook.get(request.case_id)
        if inflight is not None and not inflight.done():
            logger.info(
                "case_conversion_dedup_hit",
                extra={"case_id": request.case_id},
            )
            return await inflight

        # Wrap the actual work in a Task so other concurrent callers can
        # await the same result.
        task = asyncio.create_task(
            self._convert_from_case_impl(request, user_id, enterprise_id, team_id)
        )
        self._inflight_runbook[request.case_id] = task
        try:
            return await task
        finally:
            # Defensive cleanup: only remove if this is still our task,
            # in case a subsequent caller has already overwritten the entry.
            if self._inflight_runbook.get(request.case_id) is task:
                self._inflight_runbook.pop(request.case_id, None)

    async def _convert_from_case_impl(
        self,
        request: "CaseConversionRequest",
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
    ) -> ConversionResponse:
        """Internal: the actual conversion pipeline. Always called via
        `convert_from_case`, which wraps this with the dedup registry."""
        await self._ensure_team_publish_allowed(request.scope, team_id, user_id)

        # Verify LLM provider is available
        try:
            knowledge_model = self._settings.llm.get_knowledge_model()
            if not knowledge_model:
                raise ConversionRejectedError(
                    "No LLM provider is configured.",
                    error_code=ConversionErrorCode.LLM_UNAVAILABLE,
                )
        except AttributeError:
            raise ConversionRejectedError(
                "No LLM provider is configured.",
                error_code=ConversionErrorCode.LLM_UNAVAILABLE,
            )

        # Trust-boundary guard (defense-in-depth for #698). This funnel is the
        # single point every case→runbook caller passes through; it must not
        # trust callers to have gated. The service holds the extracted
        # ``CaseConversionRequest`` DTO, not the Case, so it cannot re-evaluate
        # the cause-assurance grade here (that stays at the case-holding sites
        # via ``runbook_conversion_ready``); but it CAN enforce the record half:
        # a request with no root_cause text would otherwise generate a runbook
        # whose Resolution silently degrades to a "See solutions below" stub.
        # Refuse instead — a runbook with no root cause is not reusable knowledge.
        if not (request.root_cause and request.root_cause.strip()):
            raise ConversionRejectedError(
                "This case has no recorded root cause, so it can't be converted "
                "into a runbook.",
                error_code=ConversionErrorCode.MISSING_ROOT_CAUSE,
            )

        # Idempotence guard: never generate a second runbook from a case that
        # already produced one. A prior conversion with at least one live draft
        # (DRAFT or VERIFIED) blocks regeneration; a case whose only drafts were
        # discarded — or whose prior attempt failed with no drafts — is free to
        # regenerate. The in-flight lock in ``convert_from_case`` covers the
        # concurrent double-fire race; this covers the sequential repeat (the
        # first run's persisted job is visible here).
        existing = await self.get_conversion_by_case(request.case_id, user_id)
        if existing and existing.has_live_draft():
            raise ConversionRejectedError(
                "A runbook draft already exists for this case. View or update it "
                "in the Dashboard under Knowledge Base > Drafts.",
                error_code=ConversionErrorCode.CASE_RUNBOOK_EXISTS,
            )

        conversion_id = generate_conversion_id()
        created_at = datetime.now(timezone.utc)
        warnings: List[str] = []

        # Construct a FailureModeAnalysis from the case data
        failure_mode = FailureModeAnalysis(
            id=f"case-{request.case_id}",
            title=request.title,
            domain=request.domain,
            # A case supplies no technology (#1880): the model infers it from
            # the source material, and the id is minted from what it wrote
            # (``_convert_single_failure_mode``'s case branch). Empty, never a
            # placeholder like ``unknown``, which the id would carry.
            service="",
            # A case carries no symptom_class taxonomy, so leave it empty when the
            # request omits it — the conversion prompt classifies into the
            # controlled vocabulary (rule 9). Never inject an off-vocab placeholder
            # like ``["unknown"]``: it fails the RunbookValidator symptom_class gate.
            symptom_class=request.symptom_class or [],
            severity=request.severity,
            symptoms_summary=request.description,
            # Guaranteed non-empty by the trust-boundary guard above.
            resolution_summary=request.root_cause,
        )

        # Assemble source material text from case context.
        # Each section maps to a Case domain model field — see CaseConversionRequest docstring.
        source_parts = [f"CASE TITLE: {request.title}"]
        if request.description:
            source_parts.append(f"PROBLEM: {request.description}")
        if request.root_cause:
            source_parts.append(f"ROOT CAUSE: {request.root_cause}")
        if request.root_cause_conditions:
            # Co-necessary with the cause above, not optional extras (#1096):
            # the runbook's Cause must record every condition the problem
            # required, or the next investigator reads a one-factor cause.
            conditions = "\n".join(f"- {c}" for c in request.root_cause_conditions)
            source_parts.append(f"CONDITIONS THE CAUSE ALSO REQUIRED:\n{conditions}")
        if request.root_cause_mechanism:
            source_parts.append(f"CAUSAL MECHANISM: {request.root_cause_mechanism}")
        if request.solutions:
            # Each block is outcome-tagged (applied vs proposed) by
            # CaseConversionRequest.from_case, which also drops superseded/failed
            # attempts. A neutral header keeps that per-block outcome authoritative
            # instead of asserting every listed fix was applied.
            solutions_text = "\n\n".join(request.solutions)
            source_parts.append(f"SOLUTIONS:\n{solutions_text}")
        if request.hypotheses_summary:
            source_parts.append(f"VALIDATED HYPOTHESES: {request.hypotheses_summary}")
        if request.evidence_summary:
            source_parts.append(f"KEY EVIDENCE:\n{request.evidence_summary}")

        source_text = "\n\n".join(source_parts)

        source_filename = f"Case {request.case_id}"

        logger.info(
            "case_conversion_started",
            extra={
                "conversion_id": conversion_id,
                "case_id": request.case_id,
                "domain": request.domain,
            },
        )

        # Convert using the same pipeline as document-driven: redact, then send.
        prepared = await self._prepare_conversion(
            text=source_text,
            failure_mode=failure_mode,
            scope=request.scope,
            filename=source_filename,
            conversion_id=conversion_id,
            case_id=request.case_id,
        )
        draft_or_error = await self._convert_single_failure_mode(
            prepared=prepared,
            failure_mode=failure_mode,
            scope=request.scope,
            user_id=user_id,
            team_id=team_id,
            enterprise_id=enterprise_id,
            case_id=request.case_id,
        )

        drafts: List[ConversionDraft] = []
        if isinstance(draft_or_error, ConversionError):
            warnings.append(f"Conversion failed: {draft_or_error.error}")
            status = ConversionStatus.FAILED
        else:
            # Tag the draft with case source info
            draft_or_error.source_type = SourceType.CASE
            draft_or_error.case_id = request.case_id
            drafts.append(draft_or_error)
            status = ConversionStatus.COMPLETED

        # Build analysis result (single failure mode, always actionable)
        analysis = AnalysisResult(
            is_actionable=True,
            failure_modes=[failure_mode],
            source_assessment=SourceAssessment(
                content_type="resolved_case",
                actionability_rating="high",
                missing_information=[],
            ),
        )

        source_file = SourceFileInfo(
            filename=source_filename,
            size_bytes=len(source_text.encode("utf-8")),
            content_type="application/x-faultmaven-case",
        )

        # Persist to database with source_type and case_id. The unique index on
        # ``conversion_jobs.live_case_id`` is the cross-replica dedup backstop:
        # if another replica committed a live case-conversion for this case while
        # this one ran the LLM, the commit raises IntegrityError. ``_persist_job``
        # writes the upload row + job + drafts in ONE session, so that failed
        # commit rolls the whole unit back — no orphan rows. Mirror the
        # in-process dedup semantics (a concurrent caller awaits the winner's
        # task and receives the winner's response) by returning the winner's
        # conversion. The typed exception plus a confirming re-read is the
        # discriminator — never classify by matching the exception message.
        #
        # ``ConflictError`` is caught alongside ``IntegrityError`` because
        # migration 046 made the two indistinguishable at the commit:
        # ``_persist_job`` re-reads on any IntegrityError and, in exactly this
        # race, can FIND the winner's drafts — two replicas converting one case
        # mint their ids from what each model wrote, which can coincide. Ids
        # from DIFFERENT cases can coincide too, and ``claim_case_draft_slot``
        # separates those with the case stem; one case's two replicas share the
        # stem, so it cannot separate them — and ``_persist_job`` then reports
        # a runbook_id duplicate for what is really the live-case
        # race. Catching only
        # IntegrityError would have handed the loser a 409 instead of the
        # winner's conversion. The re-read below is the discriminator that does
        # tell them apart; anything it cannot confirm is re-raised unchanged,
        # so a genuine duplicate from a DIFFERENT case still surfaces as its
        # 409.
        try:
            await _persist_job(
                self._db_session_factory,
                self._share_repo,
                conversion_id=conversion_id,
                user_id=user_id,
                enterprise_id=enterprise_id,
                scope=request.scope,
                team_id=team_id,
                status=status,
                source_file=source_file,
                analysis=analysis,
                drafts=drafts,
                created_at=created_at,
                source_type="case",
                case_id=request.case_id,
                warnings=warnings,
                sanitizer=self._sanitizer,
            )
        except (IntegrityError, ConflictError):
            logger.warning(
                "case_conversion_cross_replica_dedup",
                extra={"case_id": request.case_id},
            )
            existing = await self.get_conversion_by_case(request.case_id, user_id)
            if existing and existing.has_live_draft():
                return existing
            raise

        logger.info(
            "case_conversion_completed",
            extra={
                "conversion_id": conversion_id,
                "case_id": request.case_id,
                "drafts_generated": len(drafts),
                "status": status.value,
            },
        )

        return ConversionResponse(
            conversion_id=conversion_id,
            status=status,
            source_file=source_file,
            analysis=analysis,
            drafts=drafts,
            warnings=warnings,
            created_at=created_at,
        )

    # =========================================================================
    # Analysis Phase
    # =========================================================================

    # =========================================================================
    # Conversion Phase
    # =========================================================================

    async def _convert_all_failure_modes(
        self,
        text: str,
        failure_modes: List[FailureModeAnalysis],
        scope: str,
        filename: str,
        conversion_id: str,
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
    ) -> Tuple[List[ConversionDraft], List[ConversionError]]:
        """Convert all failure modes, parallel for <=5, sequential for 6+."""
        drafts: List[ConversionDraft] = []
        errors: List[ConversionError] = []

        # Which modes can yield a runbook at all — both intra-job keys, with a
        # per-mode ``ConversionError`` for every one that cannot. See
        # ``_partition_failure_modes``.
        unique_modes, partition_errors = _partition_failure_modes(failure_modes)
        errors.extend(partition_errors)

        # And which of the survivors are refused by a draft that is ALREADY
        # COMMITTED. The same argument that moved the intra-job check ahead of
        # the LLM calls applies verbatim here: the ids are knowable up front, so
        # a mode whose id some live draft already holds should not spend a
        # generation first. Re-converting a document whose modes all already
        # have drafts used to burn one full generation per mode before refusing
        # each one; this refuses them all in a single query.
        #
        # This does NOT replace ``refuse_if_draft_slot_taken`` inside
        # ``_convert_single_failure_mode``. That one is the authoritative
        # pre-persist guard; it narrows the window between this query and the
        # commit, which is exactly the cross-replica race the unique index
        # backstops. This is a cost pre-filter in front of it, and the two agree
        # because both ask ``_find_live_draft_owning``.
        unique_modes, taken_errors = await _refuse_modes_whose_id_is_taken(
            self._db_session_factory, unique_modes, enterprise_id
        )
        errors.extend(taken_errors)

        # Every survivor's request is built and REDACTED before any is sent
        # (#1901). A redaction that is required and cannot run is a refusal to
        # send: raised here, before the first generation, it fails the whole
        # conversion with nothing sent to the model and nothing persisted — not
        # after the earlier modes went out in their redacted form. Sequential, as
        # the work is the sanitizer's, not the provider's. Failures AFTER this
        # point (the router, the write) keep their per-mode ``ConversionError``
        # and the PARTIAL status.
        prepared = [
            await self._prepare_conversion(
                text=text,
                failure_mode=fm,
                scope=scope,
                filename=filename,
                conversion_id=conversion_id,
            )
            for fm in unique_modes
        ]

        # The concurrency decision is a property of the DOCUMENT — how many
        # failure modes it analysed into — not of how many of them turned out to
        # be duplicates. Keying it on the survivor count let a collision flip a
        # 6-mode document from the sequential rate-limit-avoiding path to
        # concurrent dispatch, which is a provider-load decision made by the
        # model's choice of titles. ``len(failure_modes)`` is also the
        # conservative direction: it is never smaller than the survivor count,
        # so this can only ever choose sequential where the old expression chose
        # parallel.
        if len(failure_modes) < PARALLEL_THRESHOLD:
            # Parallel conversion
            tasks = [
                self._convert_single_failure_mode(
                    prepared=request,
                    failure_mode=fm,
                    scope=scope,
                    user_id=user_id,
                    enterprise_id=enterprise_id,
                    team_id=team_id,
                )
                for fm, request in zip(unique_modes, prepared)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    errors.append(
                        ConversionError(
                            failure_mode_id=unique_modes[i].id,
                            error=str(result),
                            retryable=False,
                        )
                    )
                elif isinstance(result, ConversionError):
                    errors.append(result)
                else:
                    drafts.append(result)
        else:
            # Sequential conversion (avoid rate limits)
            for fm, request in zip(unique_modes, prepared):
                result = await self._convert_single_failure_mode(
                    prepared=request,
                    failure_mode=fm,
                    scope=scope,
                    user_id=user_id,
                    enterprise_id=enterprise_id,
                    team_id=team_id,
                )
                if isinstance(result, ConversionError):
                    errors.append(result)
                else:
                    drafts.append(result)

        return drafts, errors

    async def _prepare_conversion(
        self,
        text: str,
        failure_mode: FailureModeAnalysis,
        scope: str,
        filename: str,
        conversion_id: str,
        case_id: Optional[str] = None,
    ) -> _PreparedConversion:
        """Build one failure mode's generation request and redact it.

        Nothing is sent. Split from :meth:`_convert_single_failure_mode` so a
        multi-mode conversion redacts every mode before it sends any (#1901).

        ``case_id`` selects the case authoring policy (#1880): a case names
        an incident, so the model writes a de-identified title and infers the
        technology, and the id and the draft's title are read from the
        frontmatter it produced. Without it, the failure mode came from a
        document's analysis pass and its ``(service, title)`` is the id.

        Raises:
            RedactionUnavailableError: redaction is required and could not
                run. Nothing has been sent.
        """
        knowledge_model = self._settings.llm.get_knowledge_model()
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if case_id is None:
            # Pre-compute the runbook_id so we can pass the exact kebab-case
            # value to the LLM. Without this, the LLM is left to derive `id`
            # from the failure-mode title and routinely uses the title verbatim
            # (e.g. "Case-260526-4"), which fails the kebab-case validator.
            runbook_id = generate_runbook_id(failure_mode)
            identity = f"RUNBOOK_ID: {runbook_id}\nFAILURE MODE: {failure_mode.title}\n"
            service = failure_mode.service
            id_rules = (
                f"The frontmatter `id` field MUST be exactly: {runbook_id}\n"
                f"(lowercase, kebab-case; do not derive a different id from "
                f"the title)."
            )
        else:
            # Nothing to pre-compute: the case title names the incident, so
            # neither it nor an id minted from it may reach the runbook. The
            # id is minted after the write, from the produced frontmatter.
            runbook_id = None
            identity = (
                "FAILURE MODE: (not supplied for a case — write the title "
                "yourself, under the rules below)\n"
            )
            service = "(not supplied for a case — infer it from the source material)"
            id_rules = "\n\n".join(
                [CASE_ID_RULE, TECHNOLOGY_RULE, DE_IDENTIFICATION_RULES]
            )

        user_message = (
            f"Convert the following source material into a runbook for this specific "
            f"failure mode:\n\n"
            f"{identity}"
            f"DOMAIN: {failure_mode.domain}\n"
            f"SERVICE: {service}\n"
            f"SYMPTOM_CLASS: {', '.join(failure_mode.symptom_class) or '(none supplied — classify from the controlled vocabulary in rule 9)'}\n"
            f"SEVERITY: {failure_mode.severity or f'(not assessed — choose one of {render_vocabulary(RunbookSeverity)} from the source material)'}\n"
            f"SCOPE: {scope}\n"
            f"SOURCE FILENAME: {filename}\n"
            f"TODAY: {today_iso}\n\n"
            f"{id_rules}\n\n"
            f"--- SOURCE MATERIAL ---\n{text}\n--- END SOURCE MATERIAL ---"
        )

        # Redacted at the model boundary, under the investigation path's rule
        # (#1901). Here, outside the send step's ``try`` and its retry
        # closure, so the truncation retry resends the redacted messages and a
        # ``RedactionUnavailableError`` keeps its class — the broad ``except``
        # there would launder it into one failure mode's ``ConversionError``.
        # The output is never reversed: the runbook is meant to be
        # de-identified, so the placeholders the model writes are what is
        # persisted.
        messages = await model_boundary_redaction(self._sanitizer).asanitize_messages(
            [
                {"role": "system", "content": CONVERSION_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ]
        )

        return _PreparedConversion(
            knowledge_model=knowledge_model,
            redacted_messages=messages,
            runbook_id=runbook_id,
        )

    async def _convert_single_failure_mode(
        self,
        prepared: _PreparedConversion,
        failure_mode: FailureModeAnalysis,
        scope: str,
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
        case_id: Optional[str] = None,
    ) -> ConversionDraft | ConversionError:
        """Send one prepared failure mode and turn the answer into a draft.

        ``prepared`` comes from :meth:`_prepare_conversion` — the redacted
        messages are the only thing this sends. ``case_id`` selects the case
        branch of the id mint and the draft's title (see there).
        """
        knowledge_model = prepared.knowledge_model
        messages = prepared.redacted_messages
        runbook_id = prepared.runbook_id

        try:

            async def _convert(cap: int):
                return await self._llm_router.route(
                    messages=messages,
                    model=knowledge_model,
                    max_tokens=cap,
                    temperature=0.3,
                    # Same KNOWLEDGE_PROVIDER routing as _analyze_document.
                    **_knowledge_route_kwargs(self._settings),
                )

            response = await generate_with_truncation_retry(
                _convert,
                max_tokens=RUNBOOK_MAX_TOKENS,
                ceiling=RUNBOOK_MAX_TOKENS_CEILING,
                label=f"runbook conversion ({failure_mode.id})",
            )

            # LF, decided here: this text is what the row stores and what the
            # gate below judges, so the two cannot disagree on line endings
            # (#1403).
            runbook_content = normalize_line_endings(response.content.strip())

            # A runbook cut mid-procedure is complete-or-nothing.
            #
            # This is the one consumer where a partial is worse than an error.
            # The output is PERSISTED as a KB document and later retrieved to
            # drive other investigations, so a half-procedure ships as an
            # authoritative one — the reader has no way to tell that step 4 of 7
            # is missing rather than absent by design. And it passes every
            # validator below: a cut body still has frontmatter delimiters, is
            # far longer than 100 characters, and (since the sections are
            # written in order) still carries the required headings. Nothing
            # after this point can catch it, which is precisely why the check
            # belongs here (#1094).
            if response.is_truncated:
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error=(
                        "LLM response was truncated at the output limit "
                        f"({RUNBOOK_MAX_TOKENS_CEILING} tokens) — refusing to "
                        "persist an incomplete runbook"
                    ),
                    retryable=True,
                )

            # Validate LLM output before it becomes a draft
            if not runbook_content or len(runbook_content) < 100:
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error="LLM returned empty or too-short response",
                    retryable=True,
                )
            if "---" not in runbook_content:
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error="LLM response missing frontmatter delimiters",
                    retryable=True,
                )
            if not any(
                h in runbook_content
                for h in [
                    "## Symptom Recognition",
                    "## Diagnostic Steps",
                    "## Causes",
                ]
            ):
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error="LLM response missing required runbook sections",
                    retryable=True,
                )

            if case_id is not None:
                # The case path's id, minted now from the frontmatter the model
                # wrote (``case_authoring.mint_case_runbook_id``, #1880) — and
                # claimed here: a slot another case holds gets this case's stem
                # appended. Everything below keys on the id this returns: the
                # frontmatter and the row.
                runbook_id = await claim_case_draft_slot(
                    self._db_session_factory,
                    enterprise_id,
                    mint_case_runbook_id(runbook_content, case_id),
                    case_id,
                )
            else:
                # The document path's id was fixed before the model call; a
                # live draft holding it refuses this mode now, before the row
                # is built. See ``refuse_if_draft_slot_taken``.
                await refuse_if_draft_slot_taken(
                    self._db_session_factory, enterprise_id, runbook_id
                )

            # Belt-and-suspenders: prompt instructions don't fully constrain
            # the LLM, so rewrite the frontmatter `id` to the kebab-case
            # value we computed. The row and the frontmatter share this single
            # source of truth.
            runbook_content = _force_frontmatter_id(runbook_content, runbook_id)

            # The draft bound, on the text the row will hold: refused here, per
            # mode — one oversized runbook must not fail the rest of the
            # document (#1950). Not retryable: the same source yields the same
            # size.
            if len(runbook_content) > DRAFT_CONTENT_MAX_CHARS:
                return ConversionError(
                    failure_mode_id=failure_mode.id,
                    error=draft_content_too_long_message(len(runbook_content)),
                    retryable=False,
                )

            draft_id = generate_draft_id()

            # Off the event loop, and validating ONCE (#1417). This runs
            # inside the conversion request, so the several seconds this costs
            # on a large draft were paid by every other request the process was
            # serving, not just this one.
            validation, quality = await avalidate_and_score(runbook_content)

            quality_warning = None
            if quality.overall < QUALITY_WARNING_THRESHOLD:
                quality_warning = (
                    "Quality score is below 50. The source material may lack sufficient "
                    "diagnostic commands, resolution steps, or verification procedures. "
                    "Manual editing is recommended before verification."
                )

            return ConversionDraft(
                draft_id=draft_id,
                runbook_id=runbook_id,
                # A case-built draft is named by the title the model wrote, not
                # the case's, which names the incident; the minted id stands in
                # when the draft carries no usable title.
                title=(
                    failure_mode.title
                    if case_id is None
                    else draft_title(runbook_content) or runbook_id
                ),
                scope=scope,
                status=DraftStatus.DRAFT,
                validation=validation,
                quality_score=quality,
                content_preview=runbook_content[:500],
                content=runbook_content,
                quality_warning=quality_warning,
            )

        except ConflictError as exc:
            # Deliberately the OPPOSITE call to the one above, and spelled out
            # rather than left to fall through: a taken runbook id is a fact
            # about ONE failure mode, and a document analysed into five of them
            # should still yield the other four. So it degrades to this mode's
            # ``ConversionError`` — which the response already carries per mode
            # — instead of failing the whole conversion. Not retryable: nothing
            # about retrying frees the id.
            logger.warning(
                "conversion_draft_id_taken",
                extra={"failure_mode_id": failure_mode.id, "reason": str(exc)},
            )
            return ConversionError(
                failure_mode_id=failure_mode.id, error=str(exc), retryable=False
            )
        except Exception as e:
            # The exception's CLASS, never its text (#836). Every hand-written
            # failure in this method returns its ``ConversionError`` above, so
            # what lands here is foreign: a provider error, whose text is the
            # provider's. ``error`` is returned in ``/convert``'s warnings and
            # persisted with the job, so the detail goes to the log line
            # instead.
            logger.error(f"Conversion failed for {failure_mode.id}: {e}")
            return ConversionError(
                failure_mode_id=failure_mode.id,
                error=f"Runbook generation failed ({type(e).__name__})",
                retryable=getattr(e, "retryable", False),
            )

    # =========================================================================
    # Persistence
    # =========================================================================

    async def _resolve_job_team_id(self, conversion_id: str) -> Optional[str]:
        """Return the team a conversion job is shared to, or None.

        Reads the job's share rows (ADR-013 §D4) — replaces the retired
        ``conversion_jobs.team_id`` column. A job is shared to at most one team
        in v1; the first team share wins.
        """
        if not self._share_repo:
            return None
        shares = await self._share_repo.list_scopes_for_resource(
            "conversion_job", conversion_id
        )
        for s in shares:
            if s.scope_type == "team":
                return s.scope_id
        return None

    # =========================================================================
    # Draft Management (Phase 2)
    # =========================================================================

    async def get_conversion(
        self, conversion_id: str, user_id: str
    ) -> Optional[ConversionResponse]:
        """Get conversion job with all drafts."""
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            # Allow access if user owns the job OR it was created by system
            result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            job = result.scalar_one_or_none()
            if not job:
                return None

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.conversion_id == conversion_id,
                    ConversionDraftModel.status != DraftStatus.DISCARDED.value,
                )
            )
            draft_models = draft_result.scalars().all()

            drafts = []
            for dm in draft_models:
                # A KB upload's draft row carries no score (the upload runs the
                # quality GATE, not the scorer), and ``QualityScore(**{})``
                # failed the whole read with a 500. Score its text instead: the
                # same verdict an edit of it would record.
                if dm.quality_details:
                    quality = QualityScore(**dm.quality_details)
                else:
                    _validation, quality = await avalidate_and_score(dm.content)
                # The text is the row's own column (#1950): the same on every
                # replica, whatever disk this pod has.
                drafts.append(
                    ConversionDraft(
                        draft_id=dm.id,
                        runbook_id=dm.runbook_id,
                        title=dm.title,
                        scope=job.scope,
                        status=DraftStatus(dm.status),
                        source_type=SourceType(dm.source_type or "document"),
                        case_id=job.case_id,
                        validation=ValidationResult(
                            passed=dm.validation_passed,
                            errors=dm.validation_errors or [],
                            warnings=dm.validation_warnings or [],
                        ),
                        quality_score=quality,
                        content_preview=dm.content[:500],
                        content=dm.content,
                    )
                )

            analysis = AnalysisResult(
                **(
                    job.analysis_result
                    or {
                        "is_actionable": True,
                        "failure_modes": [],
                        "source_assessment": {
                            "content_type": "unknown",
                            "actionability_rating": "low",
                            "missing_information": [],
                        },
                    }
                )
            )

            # Source file metadata lives on ``uploaded_files``; traverse
            # via the ``source_file_id`` FK to read filename / size /
            # content_type. Not ``storage_ref``: a conversion source's is NULL,
            # and no location is the client's to see (#836).
            upload = await session.get(UploadedFileModel, job.source_file_id)
            source_file = (
                SourceFileInfo(
                    filename=upload.filename,
                    size_bytes=upload.size_bytes,
                    content_type=upload.content_type,
                )
                if upload
                else SourceFileInfo(
                    filename="<source upload missing>",
                    size_bytes=0,
                    content_type="",
                )
            )

            return ConversionResponse(
                conversion_id=job.id,
                status=ConversionStatus(job.status),
                source_file=source_file,
                analysis=analysis,
                drafts=drafts,
                # NULL (a pre-048 row, or a job with nothing to report) becomes
                # ``[]`` here because the response field is non-optional. This
                # is the one place that distinction is collapsed; see migration
                # 048. Without this the field was ALWAYS ``[]`` on read-back, so
                # the reason a job was PARTIAL survived exactly one response.
                warnings=job.warnings or [],
                created_at=job.created_at,
            )

    async def get_conversion_by_case(
        self, case_id: str, user_id: str
    ) -> Optional[ConversionResponse]:
        """Get conversion job for a specific case."""
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionJobModel)
                .where(
                    ConversionJobModel.case_id == case_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
                .order_by(ConversionJobModel.created_at.desc())
                # A case can accrue more than one job (e.g. regenerated after the
                # prior draft was discarded), so take the latest — a bare
                # scalar_one_or_none() would raise MultipleResultsFound.
                .limit(1)
            )
            job = result.scalar_one_or_none()
            if not job:
                return None

            # Delegate to get_conversion for consistent draft loading
            return await self.get_conversion(job.id, user_id)

    async def list_conversions(
        self, user_id: str, limit: int = 20, offset: int = 0
    ) -> List[dict]:
        """List user's conversion jobs (summary, no draft content)."""
        if not self._db_session_factory:
            return []

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionJobModel)
                .where(
                    (ConversionJobModel.user_id == user_id)
                    | (ConversionJobModel.user_id == "system")
                )
                .order_by(ConversionJobModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            jobs = result.scalars().all()

            # Bulk-fetch source uploads for all jobs in this page so we can
            # resolve source_filename without an N+1 query. Filename lives
            # on ``uploaded_files.filename``, reachable via
            # ``conversion_jobs.source_file_id``.
            file_ids = [j.source_file_id for j in jobs if j.source_file_id]
            uploads_by_id: dict[str, str] = {}
            if file_ids:
                uploads_result = await session.execute(
                    select(UploadedFileModel).where(
                        UploadedFileModel.file_id.in_(file_ids)
                    )
                )
                uploads_by_id = {
                    u.file_id: u.filename for u in uploads_result.scalars().all()
                }

            return [
                {
                    "conversion_id": job.id,
                    "status": job.status,
                    "source_filename": uploads_by_id.get(job.source_file_id, ""),
                    "failure_modes_detected": job.failure_modes_detected,
                    "scope": job.scope,
                    "created_at": (
                        job.created_at.isoformat() if job.created_at else None
                    ),
                }
                for job in jobs
            ]

    async def list_drafts_for_case(self, case_id: str) -> List[dict]:
        """Return non-discarded drafts whose parent job links to ``case_id``.

        Used by the case Report tab to surface case-derived runbook drafts
        alongside the auto-generated resolution/closure summaries. Returns an
        empty list when no DB session factory is wired up or no drafts match.
        """
        if not self._db_session_factory:
            return []

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionDraftModel, ConversionJobModel)
                .join(
                    ConversionJobModel,
                    ConversionDraftModel.conversion_id == ConversionJobModel.id,
                )
                .where(
                    ConversionJobModel.case_id == case_id,
                    ConversionDraftModel.status != DraftStatus.DISCARDED.value,
                )
                .order_by(ConversionDraftModel.created_at.desc())
            )
            rows = result.all()
            return [
                {
                    "draft_id": dm.id,
                    "conversion_id": job.id,
                    "runbook_id": dm.runbook_id,
                    "title": dm.title,
                    "status": dm.status,
                    "scope": job.scope,
                    "knowledge_item_id": dm.knowledge_item_id,
                    "validation_passed": dm.validation_passed,
                    "created_at": (
                        dm.created_at.isoformat() if dm.created_at else None
                    ),
                    "verified_at": (
                        dm.verified_at.isoformat() if dm.verified_at else None
                    ),
                }
                for dm, job in rows
            ]

    async def list_all_drafts(self, user_id: str) -> List[dict]:
        """List all non-deleted drafts the user can access.

        Returns drafts where:
        - User owns the conversion job (personal/team scope), OR
        - Draft scope is 'global' (visible to all users — global KB is shared)
        """
        if not self._db_session_factory:
            return []

        async with self._db_session_factory() as session:
            from sqlalchemy import or_

            result = await session.execute(
                select(ConversionDraftModel, ConversionJobModel)
                .join(
                    ConversionJobModel,
                    ConversionDraftModel.conversion_id == ConversionJobModel.id,
                )
                .where(
                    or_(
                        ConversionJobModel.user_id == user_id,
                        ConversionJobModel.scope == "global",
                    ),
                    ConversionDraftModel.status != DraftStatus.DISCARDED.value,
                )
                .order_by(ConversionDraftModel.created_at.desc())
            )
            rows = result.all()

            return [
                {
                    "conversion_id": job.id,
                    "draft_id": dm.id,
                    "runbook_id": dm.runbook_id,
                    "title": dm.title,
                    "scope": job.scope,
                    "status": dm.status,
                    "source_type": dm.source_type or "document",
                    "case_id": job.case_id,
                    "validation_passed": dm.validation_passed,
                    "quality_score": (
                        float(dm.quality_score) if dm.quality_score else None
                    ),
                    "quality_details": dm.quality_details,
                    "created_at": (
                        dm.created_at.isoformat() if dm.created_at else None
                    ),
                    "verified_at": (
                        dm.verified_at.isoformat() if dm.verified_at else None
                    ),
                }
                for dm, job in rows
            ]

    async def update_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
        content: str,
        is_platform_admin: bool = False,
    ) -> Optional[ConversionDraft]:
        """Update draft content, re-validate, and re-score.

        ``is_platform_admin`` gates editing at ``global`` scope (#785): a global
        draft is pre-verification platform-corpus content, and letting any
        authenticated user shape what an admin later verifies is the hardening
        hole this closes. Same policy and placement as :meth:`verify_draft` —
        the scope is only known once the job row is loaded, and the gate applies
        regardless of job ownership (a "system"-owned global draft from a disk
        scan included). Defaults ``False`` (fail-closed).
        """
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            # Verify ownership (system-created jobs accessible to any user)
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            job = job_result.scalar_one_or_none()
            if not job:
                return None

            if job.scope == "global":
                ensure_global_authoring_allowed(is_platform_admin)
            # Captured before the session closes: ``job`` is bound to it, and
            # the response below is built after the gate, outside it.
            job_scope = job.scope

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm or dm.status == DraftStatus.DISCARDED.value:
                return None
            # Only a DRAFT is edited. A verified runbook is published; editing
            # its row would leave the row's text and the published text apart.
            if dm.status != DraftStatus.DRAFT.value:
                raise _already_verified(draft_id)
            # For the response, built after the write session.
            runbook_id, title = dm.runbook_id, dm.title

            # Line endings, before the gate below and before the text is stored
            # (#1403). ``content`` is a JSON body field from
            # ``PUT /knowledge/conversions/{id}/drafts/{draft_id}``; a CRLF edit
            # from any non-browser client would otherwise be stored as sent and
            # scored 15 points lower for its line endings alone.
            content = ensure_draft_content_within_bound(normalize_line_endings(content))

            # The gate runs OUTSIDE the transaction. Deliberately, and the
            # reason it is worth the restructure below (#1417 review).
            #
            # Holding the session across the hop looked like a strict
            # improvement — the loop is freed, one connection is held — and it
            # is not. Inline, the blocked loop made a SECOND concurrent
            # ``update_draft`` impossible, so exactly one connection was ever
            # held; freeing the loop makes concurrent holds possible for the
            # first time. The pool is ``database_pool_size=5`` plus
            # ``database_max_overflow=10``, so fifteen concurrent edits of large
            # drafts check out every slot for the gate's duration and every other
            # database operation in the process then blocks for
            # ``database_pool_timeout=30`` s and raises. The hop would have
            # converted a loop stall into pool exhaustion.
            #
            # Same shape and same remedy as ``KnowledgeService.update_document``,
            # which holds no transaction across its re-index for exactly this
            # reason and re-reads the row in the write session.

        # --- outside the session: no connection is held while the gate runs ---
        #
        # And BEFORE the text is stored, which is the other half of the
        # ordering. This ``await`` is a cancellation point: a client disconnect,
        # a timeout or a shutdown during those 0.7-8.3 s changes nothing at all,
        # because the new text and its verdict are committed together below, in
        # one transaction, or not at all. A reviewer can never read a verdict
        # about text the gate never saw.
        validation, quality = await avalidate_and_score(content)

        # The text and its verdict, written as ONE CONDITIONAL UPDATE: only
        # while the row is still a draft. Splitting the sessions widened the
        # read-modify-write window to cover the whole gate, and a read-then-write
        # in the second session would not close it: SQLAlchemy drops ``FOR
        # UPDATE`` on SQLite, and pysqlite's deferred transaction does not
        # cover a SELECT, so a verify could commit between that read and this
        # write — and the row would then say verified while holding text that
        # was never published. The predicate is evaluated by the database at
        # write time, on both dialects: a verify that committed first leaves
        # nothing to update (409 below); one that commits after finds its
        # compare-and-swap on the text missing and publishes nothing. A Core
        # statement, so the content validator does not see it — the bound is
        # applied here explicitly.
        content = ensure_draft_content_within_bound(content)
        async with self._db_session_factory() as session:
            written = await session.execute(
                update(ConversionDraftModel)
                .where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                    ConversionDraftModel.status == DraftStatus.DRAFT.value,
                )
                .values(
                    content=content,
                    validation_passed=validation.passed,
                    validation_errors=validation.errors,
                    validation_warnings=validation.warnings,
                    quality_score=quality.overall,
                    quality_details=quality.model_dump(),
                )
                .execution_options(synchronize_session=False)
            )
            if written.rowcount == 1:
                await session.commit()
            else:
                await session.rollback()
                # Nothing written. Why: discarded (or gone) while we were
                # scoring — a draft nobody wants, 404 as before — or verified
                # meanwhile, whose published text stands.
                status = (
                    await session.execute(
                        select(ConversionDraftModel.status).where(
                            ConversionDraftModel.id == draft_id,
                            ConversionDraftModel.conversion_id == conversion_id,
                        )
                    )
                ).scalar_one_or_none()
                if status is None or status == DraftStatus.DISCARDED.value:
                    return None
                raise _already_verified(draft_id)

        quality_warning = None
        if quality.overall < QUALITY_WARNING_THRESHOLD:
            quality_warning = "Quality score is below 50. Manual editing recommended."

        return ConversionDraft(
            draft_id=draft_id,
            runbook_id=runbook_id,
            title=title,
            scope=job_scope,
            status=DraftStatus.DRAFT,
            validation=validation,
            quality_score=quality,
            content_preview=content[:500],
            content=content,
            quality_warning=quality_warning,
        )

    async def _withdraw_published_item(self, knowledge_item_id: str) -> bool:
        """Remove what a verify published but could not record. True when the
        item is gone; on failure an ERROR names the orphaned item for an
        operator, and the caller says so instead of "nothing was published"."""
        try:
            outcome = await self._knowledge_service.delete_document(knowledge_item_id)
        except Exception as exc:
            logger.error(
                "verify_draft: could not withdraw orphaned knowledge_item %s: %s",
                knowledge_item_id,
                exc,
            )
            return False
        if not (outcome or {}).get("success"):
            logger.error(
                "verify_draft: could not withdraw orphaned knowledge_item %s: %s",
                knowledge_item_id,
                (outcome or {}).get("error", "no success reported"),
            )
            return False
        return True

    async def _verifier_display_name(
        self, user_id: str, enterprise_id: Optional[str]
    ) -> str:
        """The verifier's display name, read from their users row (#1924).

        ``verified_by`` is published with the runbook to everyone at its scope,
        so it carries the stored display name, never the username: an SSO
        username is the email's local part. An account the read does not find
        (or no reader wired) leaves the field empty, as the draft had it.
        """
        if self._account_reader is None or not enterprise_id:
            return ""
        accounts = await self._account_reader.get_many_in_enterprise(
            enterprise_id, [user_id]
        )
        for account in accounts:
            if account.user_id == user_id:
                return account.display_name or ""
        logger.warning(
            "verify_draft: verifier %s has no account in enterprise %s; "
            "verified_by left empty",
            user_id,
            enterprise_id,
        )
        return ""

    async def verify_batch(
        self,
        draft_refs: list[tuple[str, str]],  # (conversion_id, draft_id)
        user_id: str,
        is_platform_admin: bool = False,
    ) -> dict:
        """Verify multiple drafts sequentially. Returns summary with per-item status.

        ``is_platform_admin`` is threaded into each :meth:`verify_draft` so the global-tier
        authoring gate applies per item: a global draft the caller may not author
        is recorded ``forbidden`` (never published) while the rest of the batch
        proceeds (#770, R4).
        """
        results = []
        verified = 0
        failed = 0
        skipped = 0
        forbidden = 0

        for conversion_id, draft_id in draft_refs:
            try:
                response = await self.verify_draft(
                    conversion_id=conversion_id,
                    draft_id=draft_id,
                    user_id=user_id,
                    is_platform_admin=is_platform_admin,
                )
                results.append(
                    {
                        "conversion_id": conversion_id,
                        "draft_id": draft_id,
                        "status": "verified",
                        "error": None,
                        "knowledge_item_id": (
                            response.knowledge_item_id if response else None
                        ),
                    }
                )
                verified += 1
            except AuthorizationError as e:
                # Global-tier authoring denial (multi-tenant / non-admin). Not a
                # failure of THIS draft — a policy refusal; record it distinctly
                # and never treat it as retryable. The draft stays unpublished.
                results.append(
                    {
                        "conversion_id": conversion_id,
                        "draft_id": draft_id,
                        "status": "forbidden",
                        "error": str(e),
                        "knowledge_item_id": None,
                    }
                )
                forbidden += 1
            except ConflictError as e:
                # An already-verified draft is idempotent, not broken: the
                # runbook is already published, so re-verifying it is a no-op
                # to SKIP, not a failure (#784). ``verify_draft`` signals this
                # with the typed ``ConflictError`` and a structured
                # ``conflict_reason`` — classify on that field, never on the
                # message string (which the exception-contract migration made a
                # dead match). Every other conflict (draft discarded, or in an
                # unexpected state) genuinely cannot be verified → ``failed``.
                if e.conflict_reason == "already_verified":
                    results.append(
                        {
                            "conversion_id": conversion_id,
                            "draft_id": draft_id,
                            "status": "skipped",
                            "error": str(e),
                            "knowledge_item_id": None,
                        }
                    )
                    skipped += 1
                else:
                    results.append(
                        {
                            "conversion_id": conversion_id,
                            "draft_id": draft_id,
                            "status": "failed",
                            "error": str(e),
                            "knowledge_item_id": None,
                        }
                    )
                    failed += 1
            except (NotFoundError, ValidationException) as e:
                # ``verify_draft``'s own refusals — "Draft not found", "Draft has
                # validation errors that must be fixed before verification" — and
                # the publication gate's ``RunbookQualityError``, which lists the
                # validator's findings about the content. Hand-written and
                # pathless, so the sentence is what the caller gets, as it was
                # on main.
                results.append(
                    {
                        "conversion_id": conversion_id,
                        "draft_id": draft_id,
                        "status": "failed",
                        "error": str(e),
                        "knowledge_item_id": None,
                    }
                )
                failed += 1
            except Exception as e:
                # The exception's CLASS, never its text (#836). The typed arms
                # above — AuthorizationError, ConflictError, NotFoundError,
                # ValidationException — carry this codebase's hand-written
                # sentences and keep them. What lands here is anything else: a
                # missing file's ``FileNotFoundError`` names its absolute path,
                # and an ingestion failure carries the vector store's own
                # message. The detail goes to the log line.
                logger.error(f"Batch verify failed for {draft_id}: {e}")
                results.append(
                    {
                        "conversion_id": conversion_id,
                        "draft_id": draft_id,
                        "status": "failed",
                        "error": f"Verification failed ({type(e).__name__})",
                        "knowledge_item_id": None,
                    }
                )
                failed += 1

        return {
            "total": len(draft_refs),
            "verified": verified,
            "failed": failed,
            "skipped": skipped,
            "forbidden": forbidden,
            "results": results,
        }

    async def verify_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
        is_platform_admin: bool = False,
    ) -> Optional[VerifyResponse]:
        """Promote draft to verified status, update frontmatter, trigger ingestion.

        ``is_platform_admin`` gates publication at ``global`` scope: verifying a draft
        ingests it into the KB at ``job.scope``, and a global runbook is the
        org-free platform corpus (readable by every tenant), so
        publishing one is a platform-operator action. The draft's scope is only
        known once the job row is loaded, so this gate lives here rather than at
        the route. Defaults ``False`` (fail-closed) so a caller that forgets to
        pass it can never publish global content by omission (#770, R4).
        """
        if not self._db_session_factory:
            return None

        async with self._db_session_factory() as session:
            # Verify ownership (system-created jobs accessible to any user)
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            job = job_result.scalar_one_or_none()
            if not job:
                raise NotFoundError(
                    resource_type="conversion_job",
                    resource_id=conversion_id,
                    message="Conversion job not found",
                )

            # Global-tier authoring gate: forbidden from any tenant session under
            # multi, admin-only single-tenant. Enforced before the draft is
            # loaded or any side effect runs (frontmatter mutation / ingestion),
            # and regardless of job ownership — a "system"-owned global draft
            # (e.g. from a disk scan) must not be publishable by a non-admin.
            if job.scope == "global":
                ensure_global_authoring_allowed(is_platform_admin)

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm:
                raise NotFoundError(
                    resource_type="draft",
                    resource_id=draft_id,
                    message="Draft not found",
                )
            if dm.status == DraftStatus.VERIFIED.value:
                raise _already_verified(draft_id)
            if dm.status == DraftStatus.DISCARDED.value:
                raise ConflictError(
                    "This draft has been discarded",
                    resource_type="draft",
                    resource_id=draft_id,
                    conflict_reason="discarded",
                )
            if dm.status != DraftStatus.DRAFT.value:
                raise ConflictError(
                    f"Draft is in unexpected state: {dm.status}",
                    resource_type="draft",
                    resource_id=draft_id,
                    conflict_reason="unexpected_state",
                )

            if not dm.validation_passed:
                raise ValidationException(
                    "Draft has validation errors that must be fixed before verification"
                )

            # Everything the publication needs, read now: the session closes
            # before ingestion, so no connection (and, on SQLite, no lock) is
            # held while the vectors are written, and a concurrent edit can
            # commit meanwhile — which the compare-and-swap below then sees.
            read_content = dm.content
            title = dm.title
            runbook_id = dm.runbook_id
            enterprise_id = job.enterprise_id
            scope = job.scope

        # The verified frontmatter, written into the draft's text (#1950). The
        # validation gate above passed, so the frontmatter parses. This text can
        # be a few characters longer than the draft (``status: verified``, the
        # verifier's name); the edit bound does not apply to it, because the
        # user was allowed to save the text it grows from (see
        # ``faultmaven.modules.knowledge.draft_content``).
        import frontmatter
        import yaml

        from faultmaven.utils.frontmatter import extract_frontmatter_metadata

        verifier_name = await self._verifier_display_name(user_id, enterprise_id)
        post = frontmatter.loads(read_content)
        post.metadata["status"] = "verified"
        post.metadata["verified_by"] = verifier_name
        # ``scope`` in the frontmatter is the tier the knowledge item is
        # published at (``taxonomy.KnowledgeScope``), and that tier is the
        # job's, not the file's: a scan infers it from the directory, and a
        # ``team_*`` file is published personal. The stored text says what the
        # row says, so a reader of the runbook is not told a tier it is not at
        # (#1986). A draft whose frontmatter already agrees is unchanged.
        post.metadata["scope"] = scope
        content = frontmatter.dumps(post)

        fm_meta = extract_frontmatter_metadata(content)
        tags: Optional[List[str]] = None
        fm_match = match_frontmatter(content)
        if fm_match:
            try:
                raw_fm = yaml.safe_load(fm_match.group(1)) or {}
                raw_tags = raw_fm.get("tags", [])
                # ConversionDraftModel.tags is a TagsArray TypeDecorator
                # expecting list[str]; pass the list shape directly.
                if isinstance(raw_tags, list):
                    tags = [str(t) for t in raw_tags] or None
                elif isinstance(raw_tags, str) and raw_tags:
                    tags = [raw_tags]
            except Exception:
                pass

        # Ingest into ChromaDB (chunk + embed + store). The verified status is
        # committed ONLY after ingestion succeeds — verifying without indexed
        # embeddings is the bug history this guards against. The previous
        # half-state (status=verified, knowledge_item_id=NULL) was then
        # "repaired" by a subsequent KB-page scan that downgraded the row back
        # to draft on every visit, corrupting user-verified runbooks. All KB
        # writes land in the single shared collection, regardless of scope —
        # scope is a per-row metadata field, not a collection split. Report the
        # real collection name so the verify response matches the store the
        # chunks actually went to.
        from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
            KB_COLLECTION,
        )

        collection = KB_COLLECTION

        if not self._knowledge_service:
            raise RuntimeError(
                "KnowledgeService unavailable — cannot verify draft "
                "without ingestion. Aborting with no status mutation."
            )

        from faultmaven.utils.runbook_id import authored_item_id

        # The published title, and the draft's own to match it. A draft written
        # before titles were scrubbed (or one a path other than conversion's
        # persistence wrote) still holds its raw title, and the draft row is
        # listed (``GET /knowledge/drafts``) after publication as before
        # (#1981). Nothing is written yet, so a refusal leaves nothing behind.
        title = await sanitize_item_title(self._sanitizer, title)

        # 16-hex authored id — must NOT match the 12-hex built-in pattern, or
        # the bootstrap orphan-prune would delete this user runbook on redeploy.
        knowledge_item_id = authored_item_id()

        # Transfer the job's team publish target (a share row on the
        # conversion_job) to the promoted knowledge_item — ingest_runbook
        # creates the item's own share row. Replaces the retired
        # conversion_jobs.team_id column (ADR-013 §D4).
        team_id = await self._resolve_job_team_id(conversion_id)
        # Membership was checked when the target was minted (#854), but THIS is
        # where it takes effect as a knowledge_item share — and the verifier may
        # differ from the minter ("system" jobs) or have left the team since.
        # Re-check fail-closed at the point of effect.
        if team_id and not await is_team_member(self._team_service, user_id, team_id):
            raise AuthorizationError(
                "You can only publish a runbook to a team you belong to"
            )
        try:
            chunks_created = await self._knowledge_service.ingest_runbook(
                document_id=knowledge_item_id,
                title=title,
                content=content,
                enterprise_id=enterprise_id,
                document_type="runbook",
                source_url=f"conversion:{conversion_id}",
                scope=scope,
                owner_id=user_id,
                team_id=team_id,
                verified_by=user_id,
            )
        except Exception as e:
            # `ingest_runbook` cleaned up its own SQL row before raising. The
            # draft stays in DRAFT state; the caller gets a 500 and can retry.
            # No half-state in either store.
            logger.error(f"Ingestion failed for draft {draft_id}: {e}")
            raise

        if chunks_created <= 0:
            # Defence-in-depth: `ingest_runbook` should raise on 0-chunk
            # results. If it doesn't, treat this as a contract violation and
            # refuse to mark the draft verified.
            raise RuntimeError(
                f"Vector indexing produced 0 chunks for draft {draft_id}. "
                f"Draft remains in DRAFT state."
            )

        # Ingestion succeeded — NOW commit the verified text and status, as a
        # COMPARE-AND-SWAP on the text that was published: only if the row is
        # still a draft holding exactly ``read_content``. An edit committed
        # during ingestion changed the text (and its verdict), so publishing
        # the pre-edit text over it would lose the edit while the row kept the
        # edit's verdict. Then nothing is verified: the published item is
        # removed the way a failed ingestion leaves none, and the caller hears
        # that the draft changed.
        now = datetime.now(timezone.utc)
        async with self._db_session_factory() as session:
            swapped = await session.execute(
                update(ConversionDraftModel)
                .where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                    ConversionDraftModel.status == DraftStatus.DRAFT.value,
                    ConversionDraftModel.content == read_content,
                )
                .values(
                    title=fit_to_column(title, ConversionDraftModel.title),
                    content=content,
                    status=DraftStatus.VERIFIED.value,
                    verified_at=now,
                    verified_by=user_id,
                    knowledge_item_id=knowledge_item_id,
                    domain=fm_meta.get("domain"),
                    service=fm_meta.get("service"),
                    severity=fm_meta.get("severity"),
                    document_type="runbook",
                    tags=tags,
                )
                .execution_options(synchronize_session=False)
            )
            if swapped.rowcount == 1:
                await session.commit()
                current_status = DraftStatus.VERIFIED.value
            else:
                await session.rollback()
                # Why the swap missed: another verify won, a delete won, or an
                # edit changed the text.
                current_status = (
                    await session.execute(
                        select(ConversionDraftModel.status).where(
                            ConversionDraftModel.id == draft_id,
                            ConversionDraftModel.conversion_id == conversion_id,
                        )
                    )
                ).scalar_one_or_none()

        if swapped.rowcount != 1:
            logger.warning(
                "verify_draft: draft %s is no longer the draft that was published "
                "(status now %s); withdrawing published item %s",
                draft_id,
                current_status,
                knowledge_item_id,
            )
            withdrawn = await self._withdraw_published_item(knowledge_item_id)
            raise _verification_lost(
                draft_id, current_status, knowledge_item_id, withdrawn
            )

        return VerifyResponse(
            draft_id=draft_id,
            runbook_id=runbook_id,
            status="verified",
            knowledge_item_id=knowledge_item_id,
            ingested=True,
            ingested_at=now,
            collection=collection,
            chunks_created=chunks_created,
        )

    # =========================================================================
    # Manual Runbook Creation
    # =========================================================================

    async def create_runbook_from_template(
        self,
        title: str,
        domain: str,
        service_name: str,
        symptom_class: List[str],
        severity: str,
        scope: str,
        tags: List[str],
        difficulty: str,
        symptom_recognition: str,
        applicability: str,
        diagnostic_steps: str,
        causes: str,
        prevention: str,
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
    ) -> ConversionDraft:
        """Create a v4 causal-chain runbook from user-provided template fields (no LLM).

        causes should be pre-formatted markdown containing one or more
        ### Cause N: <name> subsections (one ROOT each), with Statement, an optional
        Chain (root->D rungs), per-rung Indicators ([Step N]-anchored), and
        quadrant-tagged Interventions (remediation/defensive_fix/mitigation/
        loop_break), plus a ### Cause Z: Unidentified fallback with [Default] indicator.
        """
        await self._ensure_team_publish_allowed(scope, team_id, user_id)

        # The title the user typed, redacted once, ahead of everything derived
        # from it (#1981): the runbook's id slug (``password=hunter2`` would
        # become ``...-password-hunter2``, listed with the draft), the
        # frontmatter ``title:`` and ``# Runbook:`` heading in its text, the
        # draft row's title and the source file's name. Scrubbing only at the
        # draft row would leave the rest carrying it.
        title = await sanitize_item_title(self._sanitizer, title)

        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Generate kebab-case ID. Shared mint point with the LLM conversion
        # path's ``generate_runbook_id`` (#1213 follow-up); the inline copy this
        # replaces was byte-identical, which a differential test pins.
        runbook_id = runbook_id_from_parts(service_name, title)

        symptom_str = ", ".join(symptom_class)
        tags_str = ", ".join(tags) if tags else ""

        content = f"""---
id: {runbook_id}
title: "{title}"
domain: {domain}
service: {service_name}
symptom_class: [{symptom_str}]
scope: {scope}
tags: [{tags_str}]
difficulty: {difficulty}
severity: {severity}
version: "1.0.0"
last_updated: "{today_iso}"
verified_by: ""
status: draft
---

# Runbook: {title}

## Symptom Recognition
{symptom_recognition}

## Applicability
{applicability}

## Diagnostic Steps
{diagnostic_steps}

## Causes
{causes}

## Prevention
{prevention}

## Sources
- Manually authored runbook
"""

        # Normalised on the COMPOSED document, not per field (#1403). This
        # method has no document to receive — it has fifteen JSON body values
        # interpolated into an LF template, so a CRLF client produces a document
        # with mixed endings. Normalising the named free-text fields was the
        # first shape of this fix and it was wrong: it covered five of the
        # fifteen and left ``title``, ``domain``, ``service_name``,
        # ``symptom_class``, ``severity``, ``tags`` and ``difficulty`` carrying
        # CR into the frontmatter and the H1, while the validate/score calls
        # below judged the normalised twin — re-creating the verdict-vs-storage
        # split this whole change exists to remove. One call on ``content``
        # cannot be partial, and is less code than five that can.
        content = normalize_line_endings(content)
        # The edit bound, before anything is claimed: a template draft over it
        # could never be saved back (#1950). ``ValidationException`` -> 422.
        ensure_draft_content_within_bound(content)

        # See ``refuse_if_draft_slot_taken``. Unlike the LLM path this does NOT
        # degrade to a per-mode error: a manual create is one runbook, so
        # refusing it IS the answer, and the caller gets the 409.
        await refuse_if_draft_slot_taken(
            self._db_session_factory, enterprise_id, runbook_id
        )

        # Off the event loop, and validating ONCE (#1417).
        validation_result, quality = await avalidate_and_score(content)

        draft_id = generate_draft_id()

        quality_warning = None
        if quality.overall < QUALITY_WARNING_THRESHOLD:
            quality_warning = (
                "Quality score is below 50. Consider adding more detailed "
                "diagnostic commands, resolution steps, or verification procedures."
            )

        draft = ConversionDraft(
            draft_id=draft_id,
            runbook_id=runbook_id,
            title=title,
            scope=scope,
            status=DraftStatus.DRAFT,
            validation=validation_result,
            quality_score=quality,
            content_preview=content[:500],
            content=content,
            quality_warning=quality_warning,
        )

        # Persist to database using a synthetic conversion job
        conversion_id = generate_conversion_id()
        await _persist_job(
            self._db_session_factory,
            self._share_repo,
            conversion_id=conversion_id,
            user_id=user_id,
            enterprise_id=enterprise_id,
            scope=scope,
            team_id=team_id,
            status=ConversionStatus.COMPLETED,
            source_file=SourceFileInfo(
                filename=title,
                size_bytes=len(content.encode()),
                content_type="text/markdown",
            ),
            analysis=AnalysisResult(
                is_actionable=True,
                failure_modes=[],
                source_assessment=SourceAssessment(
                    content_type="manual",
                    actionability_rating="high",
                    missing_information=[],
                ),
            ),
            drafts=[draft],
            created_at=datetime.now(timezone.utc),
            sanitizer=self._sanitizer,
        )

        return {"conversion_id": conversion_id, "draft": draft}

    # =========================================================================
    # File Discovery Scan
    # =========================================================================

    async def scan_for_runbooks(
        self,
        user_id: str,
        enterprise_id: Optional[str] = None,
        is_platform_admin: bool = False,
    ) -> dict:
        """Import the runbook files placed under ``data/knowledge/`` as drafts.

        STANDALONE ONLY, and an import, never a reconciliation (#1950): each
        ``.md`` file not already a live draft or a published runbook becomes a
        draft row holding the file's text, so it appears in the Dashboard Drafts
        tab. Nothing about an existing draft is decided by what is on disk.

        Under ``TENANT_PROVIDER=multi`` the service refuses: the API runs as
        replicas with no shared disk, so there is no runbook directory to read,
        and a scan answered from one pod's empty filesystem means nothing.

        Uses an async lock to prevent concurrent scans from creating duplicate
        drafts (e.g., React StrictMode fires the mount effect twice).

        Args:
            user_id: User triggering the scan (recorded as conversion job owner).
            enterprise_id: The enterprise the conversion job + source upload
                are isolated to (ADR-017 D1). Falls back to the tenant the
                database session is bound to when None
                (``writable_enterprise_id``) — the Standalone enterprise in a
                standalone deployment, and the caller's own under multi.
            is_platform_admin: Whether the caller may author global-scope KB. A file whose
                inferred scope is ``global`` (the platform corpus, billed to no
                organization) is
                SKIPPED when the caller is not a platform operator (any tenant
                session under multi, or a non-admin single-tenant) — minting a
                global draft is platform-tier authoring (#770, R4). Personal
                (``personal_*`` / ``user_*``) and ``team_*`` discovery is
                unaffected. Defaults ``False`` (fail-closed).

        Returns:
            {"discovered": N, "skipped": N, "errors": [...], "notes": [...],
            "drafts": [...]}. ``errors`` names the files that were NOT imported;
            ``notes`` names imported files whose draft is not what their
            directory asked for — a ``team_*`` file is imported as ``personal``,
            because the deployments this runs on have no teams (#1986).

        Raises:
            ValidationException: the deployment is multi-tenant (422 at the
                API boundary), the same shape as the team-publishing refusal.
        """
        if requested_tenant_provider() == BUILTIN_MULTI:
            raise ValidationException(SCAN_UNAVAILABLE_MULTI_MSG)
        async with self._scan_lock:
            return await _scan_for_runbooks_impl(
                self._data_dir,
                self._db_session_factory,
                self._share_repo,
                user_id,
                enterprise_id,
                is_platform_admin,
                sanitizer=self._sanitizer,
            )

    async def discard_by_knowledge_item_id(self, knowledge_item_id: str) -> bool:
        """Discard the draft that was activated into the given knowledge item.

        Called by KnowledgeService when a verified runbook is deleted from
        the KB. Clears knowledge_item_id and sets status to DISCARDED so
        the draft no longer appears in the pending or verified list.

        Args:
            knowledge_item_id: The knowledge_item_id (or runbook_id) stored
                               on the ConversionDraftModel row.

        Returns:
            True if a matching draft was found and discarded, False otherwise.
        """
        if not self._db_session_factory:
            return False

        async with self._db_session_factory() as session:
            result = await session.execute(
                select(ConversionDraftModel).where(
                    (ConversionDraftModel.knowledge_item_id == knowledge_item_id)
                    | (ConversionDraftModel.runbook_id == knowledge_item_id)
                )
            )
            dm = result.scalar_one_or_none()
            if not dm:
                return False

            dm.status = DraftStatus.DISCARDED.value
            dm.knowledge_item_id = None
            job = await session.get(ConversionJobModel, dm.conversion_id)
            if job is not None:
                await _release_live_case_key_if_drained(session, job, dm.id)
            await session.commit()
            return True

    async def delete_draft(
        self,
        conversion_id: str,
        draft_id: str,
        user_id: str,
        is_platform_admin: bool = False,
    ) -> bool:
        """Soft-delete a draft: its row becomes DISCARDED.

        ``is_platform_admin`` gates deletion at ``global`` scope: discarding a
        global draft is platform-corpus authoring, the same policy as
        :meth:`update_draft` / :meth:`verify_draft` on the adjacent verbs.
        Defaults ``False`` (fail-closed).
        """
        if not self._db_session_factory:
            return False

        async with self._db_session_factory() as session:
            # Verify ownership (system-created jobs accessible to any user)
            job_result = await session.execute(
                select(ConversionJobModel).where(
                    ConversionJobModel.id == conversion_id,
                    (
                        (ConversionJobModel.user_id == user_id)
                        | (ConversionJobModel.user_id == "system")
                    ),
                )
            )
            job = job_result.scalar_one_or_none()
            if not job:
                return False

            if job.scope == "global":
                ensure_global_authoring_allowed(is_platform_admin)

            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm:
                return False

            # Soft delete in database
            dm.status = DraftStatus.DISCARDED.value
            await _release_live_case_key_if_drained(session, job, dm.id)
            await session.commit()

            return True
