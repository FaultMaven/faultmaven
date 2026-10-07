"""ConversionService: Orchestrates document-to-runbook conversion.

Pipeline:
1. Preprocess uploaded document (6 stages)
2. Analyze document for failure modes (LLM)
3. Convert each failure mode to a runbook (LLM, parallel or sequential)
4. Validate and score each draft
5. Persist drafts to disk and database
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
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
from faultmaven.modules.auth.contracts import is_team_member
from faultmaven.modules.knowledge.domain.global_authoring import (
    ensure_global_authoring_allowed,
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
    _scope_dir,
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
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.utils.frontmatter import match_frontmatter
from faultmaven.utils.line_endings import normalize_line_endings
from faultmaven.utils.runbook_id import (
    RunbookPathEscape,
    draft_filename,
    knowledge_root,
    resolve_runbook_path,
    runbook_id_from_parts,
    write_runbook_file,
)

logger = logging.getLogger(__name__)

# Single-tenant default enterprise. It is the *contextvar's* default (see
# ``config.tenant_context``), not a fallback any writer here applies directly:
# stamping this constant on a write is only correct in a single-tenant
# deployment, and under ``TENANT_PROVIDER=multi`` it is the sentinel enterprise,
# which no tenant session may write (#1143). Writers resolve the tenant through
# :func:`writable_enterprise_id` instead. No production code reads this any more;
# it stays exported because the tests name the single-tenant enterprise by it.
DEFAULT_ENTERPRISE_ID = SingleTenantProvider.DEFAULT_ENTERPRISE_ID


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
    ):
        self._llm_router = llm_router
        self._settings = settings
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
        self._preprocessor = DocumentPreprocessor(llm_router, settings)
        self._scan_lock = asyncio.Lock()
        # In-flight case-conversion dedup. Keyed by case_id; the value is
        # the running asyncio.Task that other concurrent callers can await.
        # Mirrors `_inflight_vectorize` on MilestoneEngine. Prevents
        # duplicate drafts when the user clicks the runbook affordance
        # twice in rapid succession. See convert_from_case().
        self._inflight_runbook: Dict[str, asyncio.Task] = {}

    @property
    def _data_dir(self) -> Path:
        # Delegates so this service and ``KnowledgeService`` cannot drift apart
        # on where the knowledge tree is; that agreement is what every
        # containment check below is anchored on.
        return knowledge_root()

    #: ``conflict_reason`` for a draft row whose ``file_path`` is not inside the
    #: knowledge tree. Structured so ``verify_batch`` and any client can key on
    #: the field rather than parse a message (the same rule #784 established for
    #: ``already_verified``).
    PATH_ESCAPE_CONFLICT_REASON = "path_outside_knowledge_tree"

    def _refuse_escaping_draft(
        self, draft_id: str, exc: RunbookPathEscape
    ) -> ConflictError:
        """Translate a containment refusal into the module's typed exception.

        Two jobs, and the split between them is the point:

        * the **log** gets ``exc`` in full — the resolved absolute paths are
          what an operator needs to repair the row; and
        * the **client** gets a message naming the draft id and the refusal
          class and nothing else. ``str()`` of this exception reaches a response
          body two ways — the 409 handler's ``detail``, and ``verify_batch``'s
          per-item ``error`` — and echoing a server filesystem path into either
          is the disclosure #866 closed for this module.

        ``ConflictError`` rather than a bare ``ValueError``: the row is in a
        state the operation cannot proceed from, which is what 409 means, and
        ``verify_draft`` already documents that every failure shape here is a
        typed exception (a raw ``ValueError`` would surface as an unmapped 500).
        """
        logger.error("refusing a filesystem operation on draft %s: %s", draft_id, exc)
        return ConflictError(
            f"Draft {draft_id} references a runbook file outside the knowledge "
            "tree and cannot be read or modified. The stored path must be "
            "repaired by an operator; see the server log for details.",
            resource_type="draft",
            resource_id=draft_id,
            conflict_reason=self.PATH_ESCAPE_CONFLICT_REASON,
        )

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
            service=request.service,
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
                "service": request.service,
            },
        )

        # Convert using the same pipeline as document-driven
        draft_or_error = await self._convert_single_failure_mode(
            text=source_text,
            failure_mode=failure_mode,
            scope=request.scope,
            filename=source_filename,
            conversion_id=conversion_id,
            user_id=user_id,
            team_id=team_id,
            enterprise_id=enterprise_id,
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
        # race, FINDS the winner's drafts — two replicas converting one case
        # mint the same ``(service, title)`` ids — so it reports a runbook_id
        # duplicate for what is really the live-case race. Catching only
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
        # pre-write guard and still keys on the resolved file path as well as
        # the id; it also closes the window between this query and the write,
        # which is exactly the cross-replica race migration 046 backstops. This
        # is a cost pre-filter in front of it, and the two agree because both
        # ask ``_find_live_draft_owning``.
        unique_modes, taken_errors = await _refuse_modes_whose_id_is_taken(
            self._db_session_factory, unique_modes, enterprise_id
        )
        errors.extend(taken_errors)

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
                    text=text,
                    failure_mode=fm,
                    scope=scope,
                    filename=filename,
                    conversion_id=conversion_id,
                    user_id=user_id,
                    enterprise_id=enterprise_id,
                    team_id=team_id,
                )
                for fm in unique_modes
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, RunbookPathEscape):
                    # Re-raised, never laundered into a ``ConversionError``.
                    # ``return_exceptions=True`` turns the deliberate
                    # ``except RunbookPathEscape: raise`` in
                    # ``_convert_single_failure_mode`` into a returned value,
                    # and appending ``str(result)`` here would put the resolved
                    # SERVER PATHS the message carries into ``response.warnings``
                    # — a 200/201 body (#866), which is precisely what that
                    # bare re-raise exists to prevent. The sequential branch
                    # below propagates it, so without this the same event
                    # behaved differently purely on failure-mode count.
                    raise result
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
            for fm in unique_modes:
                result = await self._convert_single_failure_mode(
                    text=text,
                    failure_mode=fm,
                    scope=scope,
                    filename=filename,
                    conversion_id=conversion_id,
                    user_id=user_id,
                    enterprise_id=enterprise_id,
                    team_id=team_id,
                )
                if isinstance(result, ConversionError):
                    errors.append(result)
                else:
                    drafts.append(result)

        return drafts, errors

    async def _convert_single_failure_mode(
        self,
        text: str,
        failure_mode: FailureModeAnalysis,
        scope: str,
        filename: str,
        conversion_id: str,
        user_id: str,
        enterprise_id: Optional[str],
        team_id: Optional[str] = None,
    ) -> ConversionDraft | ConversionError:
        """Convert a single failure mode to a runbook draft."""
        try:
            knowledge_model = self._settings.llm.get_knowledge_model()
            today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Pre-compute the runbook_id so we can pass the exact kebab-case
            # value to the LLM. Without this, the LLM is left to derive `id`
            # from the failure-mode title and routinely uses the title verbatim
            # (e.g. "Case-260526-4"), which fails the kebab-case validator.
            runbook_id = generate_runbook_id(failure_mode)

            user_message = (
                f"Convert the following source material into a runbook for this specific "
                f"failure mode:\n\n"
                f"RUNBOOK_ID: {runbook_id}\n"
                f"FAILURE MODE: {failure_mode.title}\n"
                f"DOMAIN: {failure_mode.domain}\n"
                f"SERVICE: {failure_mode.service}\n"
                f"SYMPTOM_CLASS: {', '.join(failure_mode.symptom_class) or '(none supplied — classify from the controlled vocabulary in rule 9)'}\n"
                f"SEVERITY: {failure_mode.severity or '(not assessed — choose one of critical, high, medium, low from the source material)'}\n"
                f"SCOPE: {scope}\n"
                f"SOURCE FILENAME: {filename}\n"
                f"TODAY: {today_iso}\n\n"
                f"The frontmatter `id` field MUST be exactly: {runbook_id}\n"
                f"(lowercase, kebab-case; do not derive a different id from "
                f"the title).\n\n"
                f"--- SOURCE MATERIAL ---\n{text}\n--- END SOURCE MATERIAL ---"
            )

            async def _convert(cap: int):
                return await self._llm_router.route(
                    messages=[
                        {"role": "system", "content": CONVERSION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
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

            runbook_content = response.content.strip()

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

            # Validate LLM output before writing to disk
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

            # Belt-and-suspenders: prompt instructions don't fully constrain
            # the LLM, so rewrite the frontmatter `id` to the kebab-case
            # value we computed. The filename + DB row + frontmatter all
            # share this single source of truth.
            runbook_content = _force_frontmatter_id(runbook_content, runbook_id)

            # `runbook_id` was computed before the LLM call so it could be
            # passed in the prompt; re-using it here keeps the on-disk
            # filename, the prompt-injected `id`, and the row's runbook_id
            # in sync.
            draft_id = generate_draft_id()

            # Write draft to disk. Through the shared helper: it validates
            # containment against the ROOT of the knowledge tree and does so
            # BEFORE creating the scope directory. ``runbook_id`` is minted
            # from an allowlist so an escape is unconstructible today — the
            # guard is what keeps that true if the mint rule is loosened or a
            # new caller assembles its own name (#1213 follow-up).
            draft_path = _scope_dir(
                self._data_dir, scope, team_id, user_id
            ) / draft_filename(runbook_id)

            # BEFORE the write. The path is derived from ``runbook_id``, so a
            # duplicate lands on the EXISTING draft's file and would replace
            # its content on the way to an INSERT migration 046 rejects. See
            # ``refuse_if_draft_slot_taken``.
            await refuse_if_draft_slot_taken(
                self._db_session_factory, enterprise_id, runbook_id, str(draft_path)
            )

            write_runbook_file(
                draft_path,
                runbook_content,
                source=f"converted draft (runbook_id={runbook_id})",
                root=self._data_dir,
            )

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
                title=failure_mode.title,
                scope=scope,
                status=DraftStatus.DRAFT,
                validation=validation,
                quality_score=quality,
                file_path=str(draft_path),
                content_preview=runbook_content[:500],
                content=runbook_content,
                quality_warning=quality_warning,
            )

        except RunbookPathEscape:
            # Never laundered into a generic ConversionError: that would put the
            # resolved server paths into ``error`` (a 200 response body, see
            # #866) and would report a containment refusal as a retryable
            # conversion failure. Today the mint makes this unreachable — which
            # is exactly why it must not be swallowed if that ever changes.
            raise
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
            # what lands here is foreign: a provider error, or an ``OSError``
            # from the write whose text names the server path. ``error`` is
            # returned in ``/convert``'s warnings and persisted with the job,
            # so the detail goes to the log line instead.
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
                # Read content from disk. Guarded like the write paths — an
                # escaped row here would put an arbitrary file's contents into
                # the API response. This is the one caller that degrades rather
                # than refuses: one bad row must not deny the whole listing, so
                # the escape is logged and that draft's content is omitted
                # (which is already what an unreadable file does here).
                content = None
                try:
                    resolved = resolve_runbook_path(
                        dm.file_path,
                        source=f"conversion_drafts.file_path (draft_id={dm.id})",
                        root=self._data_dir,
                    )
                    content = resolved.read_text(encoding="utf-8")
                except RunbookPathEscape as exc:
                    logger.error(
                        "refusing to read draft %s; omitting its content: %s",
                        dm.id,
                        exc,
                    )
                except Exception:
                    pass

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
                        quality_score=QualityScore(**(dm.quality_details or {})),
                        file_path=dm.file_path,
                        content_preview=(content or "")[:500],
                        content=content,
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

            # Line endings, before the write AND before the re-validate below
            # (#1403). ``content`` is a JSON body field from
            # ``PUT /knowledge/conversions/{id}/drafts/{draft_id}``, so nothing
            # upstream has decoded it through ``Path.read_text``; a CRLF edit
            # from any non-browser client used to be written to disk and then
            # scored 15 points lower for its line endings alone. Before the
            # write specifically, because this method persists first and
            # validates second — normalising after the write would leave the
            # file and the verdict disagreeing.
            content = normalize_line_endings(content)

            # Write updated content to disk.
            #
            # ``dm.file_path`` comes straight back out of the database. Every
            # mint point that produces it is sanitised now, but a row persisted
            # BEFORE #1215 was written by a mint that could escape, and this
            # edit path would re-open and rewrite it without ever re-checking.
            # Re-validate on use: containment is a property of the path at the
            # moment it is used, not of the code that happened to create it.
            # Refuses as a typed 409 naming the row; the resolved paths go to
            # the log, never to the client (#1213 follow-up, see #866).
            # Containment is CHECKED here and the file WRITTEN in session two,
            # below. Checking early keeps an escaping row from costing a full
            # gate run before it is refused; writing late is what keeps the file
            # and the row in step (see the ordering note before the gate).
            try:
                resolve_runbook_path(
                    dm.file_path,
                    source=f"conversion_drafts.file_path (draft_id={dm.id})",
                    root=self._data_dir,
                )
            except RunbookPathEscape as exc:
                raise self._refuse_escaping_draft(dm.id, exc) from exc
            draft_file_path = dm.file_path

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
        # And BEFORE the write, which is the other half of the ordering. This
        # ``await`` is a cancellation point that did not exist on main: there the
        # write, the gate and the commit sat in one synchronous stretch, so the
        # loop could not interleave and the window was zero. Put the write ahead
        # of the gate and a client disconnect, a timeout or a shutdown during
        # those 0.7-8.3 s leaves the file rewritten while the row keeps the
        # PREVIOUS verdict — permanently, and a reviewer then reads a green
        # verdict about text the gate never saw. Gating first makes a
        # cancellation here change nothing at all, and puts the write back
        # beside the commit with no await between them, which is the property
        # main had.
        validation, quality = await avalidate_and_score(content)

        async with self._db_session_factory() as session:
            # RE-READ rather than reusing ``dm``: splitting the sessions widened
            # the read-modify-write window to cover the whole gate, so the row is
            # re-fetched and the verdict applied to that fresh copy. Without this
            # a concurrent discard would be overwritten by a stale snapshot.
            draft_result = await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.id == draft_id,
                    ConversionDraftModel.conversion_id == conversion_id,
                )
            )
            dm = draft_result.scalar_one_or_none()
            if not dm or dm.status == DraftStatus.DISCARDED.value:
                # Discarded while we were scoring. The file on disk has already
                # been rewritten — that was true before this split too, since
                # the write preceded the gate and a failed commit never unwrote
                # it — but no verdict is recorded for a draft nobody wants.
                return None

            try:
                write_runbook_file(
                    draft_file_path,
                    content,
                    source=f"conversion_drafts.file_path (draft_id={dm.id})",
                    root=self._data_dir,
                )
            except RunbookPathEscape as exc:
                raise self._refuse_escaping_draft(dm.id, exc) from exc

            dm.validation_passed = validation.passed
            dm.validation_errors = validation.errors
            dm.validation_warnings = validation.warnings
            dm.quality_score = quality.overall
            dm.quality_details = quality.model_dump()

            await session.commit()

            quality_warning = None
            if quality.overall < QUALITY_WARNING_THRESHOLD:
                quality_warning = (
                    "Quality score is below 50. Manual editing recommended."
                )

            return ConversionDraft(
                draft_id=dm.id,
                runbook_id=dm.runbook_id,
                title=dm.title,
                scope=job_scope,
                status=DraftStatus(dm.status),
                validation=validation,
                quality_score=quality,
                file_path=dm.file_path,
                content_preview=content[:500],
                content=content,
                quality_warning=quality_warning,
            )

    async def verify_batch(
        self,
        draft_refs: list[tuple[str, str]],  # (conversion_id, draft_id)
        user_id: str,
        username: str,
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
                    username=username,
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
        username: str,
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
                raise ConflictError(
                    "This runbook has already been verified and ingested",
                    resource_type="draft",
                    resource_id=draft_id,
                    conflict_reason="already_verified",
                )
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

            # Update frontmatter on disk using python-frontmatter.
            #
            # Resolved through the shared guard FIRST, before any read or write:
            # this path is a database value, and on this method it is read back
            # into the response as well as rewritten, so an escaping row would
            # both leak an arbitrary file and be overwritten. Refuses as a typed
            # 409 naming the row — the same exception contract every other
            # failure shape on this method already follows (#1213 follow-up).
            try:
                file_path = resolve_runbook_path(
                    dm.file_path,
                    source=f"conversion_drafts.file_path (draft_id={dm.id})",
                    root=self._data_dir,
                )
            except RunbookPathEscape as exc:
                raise self._refuse_escaping_draft(dm.id, exc) from exc
            try:
                import frontmatter

                post = frontmatter.load(str(file_path))
                post.metadata["status"] = "verified"
                post.metadata["verified_by"] = username
                frontmatter.dump(post, str(file_path))
            except Exception as e:
                logger.error(f"Failed to update frontmatter: {e}")
                # Fallback: just update the file content with regex
                content = file_path.read_text(encoding="utf-8")
                content = content.replace("status: draft", "status: verified", 1)
                content = content.replace(
                    'verified_by: ""', f'verified_by: "{username}"', 1
                )
                file_path.write_text(content, encoding="utf-8")

            # Populate metadata from frontmatter (safe to set pre-ingest; the
            # frontmatter on disk was already updated above).
            content = file_path.read_text(encoding="utf-8")
            from faultmaven.utils.frontmatter import extract_frontmatter_metadata

            fm_meta = extract_frontmatter_metadata(content)
            dm.domain = fm_meta.get("domain")
            dm.service = fm_meta.get("service")
            dm.severity = fm_meta.get("severity")
            dm.document_type = "runbook"

            import yaml

            fm_match = match_frontmatter(content)
            if fm_match:
                try:
                    raw_fm = yaml.safe_load(fm_match.group(1)) or {}
                    raw_tags = raw_fm.get("tags", [])
                    # ConversionDraftModel.tags is a TagsArray TypeDecorator
                    # expecting list[str]; pass the list shape directly.
                    if isinstance(raw_tags, list):
                        dm.tags = [str(t) for t in raw_tags] or None
                    elif isinstance(raw_tags, str) and raw_tags:
                        dm.tags = [raw_tags]
                except Exception:
                    pass

            # Ingest into ChromaDB (chunk + embed + store). The verified
            # status is committed ONLY after ingestion succeeds — verifying
            # without indexed embeddings is the bug history this guards
            # against. The previous half-state (status=verified,
            # knowledge_item_id=NULL) was then "repaired" by a subsequent
            # KB-page scan that downgraded the row back to draft on every
            # visit, corrupting user-verified runbooks.
            # All KB writes land in the single shared collection, regardless
            # of scope — scope is a per-row metadata field, not a collection
            # split. Report the real collection name so the verify response
            # matches the store the chunks actually went to.
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

            # 16-hex authored id — must NOT match the 12-hex built-in pattern, or
            # the bootstrap orphan-prune would delete this user runbook on redeploy.
            knowledge_item_id = authored_item_id()

            # Transfer the job's team publish target (a share row on the
            # conversion_job) to the promoted knowledge_item — ingest_runbook
            # creates the item's own share row. Replaces the retired
            # conversion_jobs.team_id column (ADR-013 §D4).
            team_id = await self._resolve_job_team_id(conversion_id)
            # Membership was checked when the target was minted (#854), but
            # THIS is where it takes effect as a knowledge_item share — and
            # the verifier may differ from the minter ("system" jobs) or have
            # left the team since. Re-check fail-closed at the point of effect.
            if team_id and not await is_team_member(
                self._team_service, user_id, team_id
            ):
                raise AuthorizationError(
                    "You can only publish a runbook to a team you belong to"
                )
            try:
                chunks_created = await self._knowledge_service.ingest_runbook(
                    document_id=knowledge_item_id,
                    title=dm.title,
                    content=content,
                    enterprise_id=job.enterprise_id,
                    document_type="runbook",
                    source_url=f"conversion:{conversion_id}",
                    scope=job.scope,
                    owner_id=user_id,
                    team_id=team_id,
                    verified_by=user_id,
                )
            except Exception as e:
                # `ingest_runbook` cleaned up its own SQL row before raising.
                # The draft stays in DRAFT state; the caller gets a 500 and
                # can retry. No half-state in either store.
                logger.error(f"Ingestion failed for draft {draft_id}: {e}")
                raise

            if chunks_created <= 0:
                # Defence-in-depth: `ingest_runbook` should raise on 0-chunk
                # results. If it doesn't, treat this as a contract violation
                # and refuse to mark the draft verified.
                raise RuntimeError(
                    f"Vector indexing produced 0 chunks for draft {draft_id}. "
                    f"Draft remains in DRAFT state."
                )

            # Ingestion succeeded — NOW commit the verified status.
            now = datetime.now(timezone.utc)
            dm.status = DraftStatus.VERIFIED.value
            dm.verified_at = now
            dm.verified_by = user_id
            dm.knowledge_item_id = knowledge_item_id

            await session.commit()

            return VerifyResponse(
                draft_id=dm.id,
                runbook_id=dm.runbook_id,
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

        # Write to disk through the shared containment-checked helper — same
        # anchor, same before-mkdir ordering as every other runbook write.
        draft_path = _scope_dir(
            self._data_dir, scope, team_id, user_id
        ) / draft_filename(runbook_id)

        # BEFORE the write, for the reason on ``refuse_if_draft_slot_taken``.
        # Unlike the LLM path this does NOT degrade to a per-mode error: a
        # manual create is one runbook, so refusing it IS the answer, and the
        # caller gets the 409.
        await refuse_if_draft_slot_taken(
            self._db_session_factory, enterprise_id, runbook_id, str(draft_path)
        )

        write_runbook_file(
            draft_path,
            content,
            source=f"manually created runbook (runbook_id={runbook_id})",
            root=self._data_dir,
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
            file_path=str(draft_path),
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
        """Scan data/knowledge/ for .md files not tracked in the database.

        Discovers runbooks created by the KB Toolkit or dropped on disk manually.
        Creates draft records so they appear in the Dashboard Drafts tab.

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
                global draft is platform-tier authoring (#770, R4). Personal/team
                discovery is unaffected. Defaults ``False`` (fail-closed).

        Returns:
            {"discovered": N, "skipped": N, "errors": [...], "drafts": [...]}
        """
        async with self._scan_lock:
            return await _scan_for_runbooks_impl(
                self._data_dir,
                self._db_session_factory,
                self._share_repo,
                user_id,
                enterprise_id,
                is_platform_admin,
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
        """Soft-delete a draft and remove the file from disk.

        ``is_platform_admin`` gates deletion at ``global`` scope: destroying a
        global draft (file unlinked from disk) is platform-corpus authoring,
        the same policy as :meth:`update_draft` / :meth:`verify_draft` on the
        adjacent verbs. Defaults ``False`` (fail-closed).
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

            # Remove file from disk. Same guard as the write paths, for the
            # same reason and with more at stake: this is an ``unlink`` driven
            # by a database value, so an escaping row would delete an arbitrary
            # file.
            #
            # Unlike the write paths this does NOT abort the operation. The
            # dangerous half is the unlink, and it must never keep the ROW from
            # being discarded — the "permanently undeletable row" this design
            # exists to prevent. So BOTH the containment refusal AND a failure
            # of the unlink itself degrade: the soft-delete below always runs.
            #
            # ``unlink`` can raise ``OSError`` independently of containment — a
            # read-only or full filesystem, a permission denial, or a TOCTOU
            # race where the file vanishes between ``exists()`` and ``unlink()``
            # (``FileNotFoundError`` is an ``OSError``). Catching only
            # ``RunbookPathEscape`` here left every one of those propagating
            # before the status flip. Same degrade-and-continue shape as
            # ``get_conversion``'s read (#1213 follow-up).
            try:
                file_path = resolve_runbook_path(
                    dm.file_path,
                    source=f"conversion_drafts.file_path (draft_id={dm.id})",
                    root=self._data_dir,
                )
                if file_path.exists():
                    file_path.unlink()
            except RunbookPathEscape as exc:
                logger.error(
                    "refusing to unlink for draft %s; discarding the row anyway: %s",
                    dm.id,
                    exc,
                )
            except OSError as exc:
                logger.error(
                    "failed to unlink the file for draft %s; discarding the "
                    "row anyway: %s",
                    dm.id,
                    exc,
                )

            # Soft delete in database
            dm.status = DraftStatus.DISCARDED.value
            await _release_live_case_key_if_drained(session, job, dm.id)
            await session.commit()

            return True
